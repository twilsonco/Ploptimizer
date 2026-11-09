"""Export glue: relief engraving for packed plates.

Called from :func:`plt_optimizer.vectorize.export_per_cutter_plts` after
layout/render/packing. For every packed label with ``relief_engraving``
enabled, the label's rendered ``text_chunks`` are REPLACED with synthetic
relief chunk records (one per contiguous raster stroke / outline ring, on
the relief cutters' pens) and its ``plt_content`` is rebuilt to carry only
the border (SP2) and the relief sections -- the label's normal text strokes
are suppressed (relief replaces them) and its drill holes (SP3) are removed
(relief fills over them). Everything downstream -- the plate-space TSP
optimization, the per-cutter file split, the combined proof, the PDF
previews -- then treats relief chunks exactly like text chunks.

Coordinates: relief geometry is computed in the label-local render frame
(inches, +y up, box ``(0, 0)..(label.width, label.height)``), so a packed
label's relief rides the normal per-label plate transform (centering,
Y-mirror, optional 90-degree rotation, slot translation) and its raster
rows rotate with the label. The plate centering shift is neutralized per
relief label through :attr:`RenderedLabel.band_center_offset_inches` (the
fill is symmetric in the label box, so it must NOT follow the descender
band offset the text ink used).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from plt_optimizer.generate.label_renderer import RenderedLabel, TextChunkRecord
from plt_optimizer.generate.relief.cutters import (
    ReliefCutter,
    bypass_relief_cutters,
    select_relief_cutters,
)
from plt_optimizer.generate.relief.pipeline import (
    ReliefPass,
    generate_relief_passes,
    pen_widths_for_label,
)
from plt_optimizer.generate.relief.regions import (
    EngraveRegions,
    build_engrave_regions,
    label_ink,
)
from plt_optimizer.generate.resolution import ResolvedLabel
from plt_optimizer.generate.text_geometry import TextBlock
from plt_optimizer.utils.logging import TextLogger

PenLayer = Tuple[float, str]


def relief_pen_layers(
    resolved_labels: Sequence[ResolvedLabel],
    available_cutters: Sequence[float],
) -> List[PenLayer]:
    """Pen layers relief engraving may need, for the pen map.

    The cutter ladder itself is selected after rendering (it needs the ink
    silhouette), so every plausible relief tool must already own a pen:
    the explicit bypass lists, plus the full inventory for preset-driven
    labels. Unused pens stay empty and the writer skips empty pen groups
    (relief labels suppress their text strokes, so the text layers only
    carry geometry for non-relief labels).

    Args:
        resolved_labels: Fully resolved labels.
        available_cutters: The shop inventory (inches), if configured.

    Returns:
        Sorted ``(cutter, "none")`` layers (empty when no label enables
        relief).
    """
    layers: set[PenLayer] = set()
    for label in resolved_labels:
        if not label.relief_engraving:
            continue
        if label.relief_engraving_cutters:
            for cutter in label.relief_engraving_cutters:
                layers.add((cutter, "none"))
        for cutter in available_cutters:
            layers.add((float(cutter), "none"))
    return sorted(layers)


def _plan_ladder(
    label: ResolvedLabel,
    regions: EngraveRegions,
    available_cutters: Sequence[float],
    logger: Optional[TextLogger],
) -> Tuple[ReliefCutter, ...]:
    """The label's relief cutter ladder: the bypass list, else the preset.

    Args:
        label: The resolved label.
        regions: The label's engrave regions (preset admissibility reads
            them; the bypass list does not).
        available_cutters: The shop inventory (inches).
        logger: Optional logger.

    Returns:
        The ladder, largest diameter first (empty when unselectable).
    """
    if label.relief_engraving_cutters:
        return bypass_relief_cutters(label.relief_engraving_cutters)
    if not available_cutters:
        if logger is not None:
            logger.warning(
                f"label {label.id}: relief engraving needs a cutter inventory "
                "(tools.json) when relief_engraving_cutters is not set; "
                "the label is left unengraved."
            )
        return ()
    return tuple(
        select_relief_cutters(
            label.relief_engraving_quality,
            regions,
            available_cutters,
            logger=logger,
        )
    )


def _relief_section(pen: int, relief_pass: ReliefPass, y_span: float, y_center: float) -> str:
    """One relief pen's ``SP`` section, in the emitted (Y-mirrored) frame.

    Args:
        pen: The pen number.
        relief_pass: The cutter's strokes (label-local +y up, centered
            frame).
        y_span: ``y_min + y_max`` of the rendered label (the mirror the
            renderer applied to every other layer).
        y_center: The centering lift onto the border box (inches) -- the
            relief rides the label box, not the text band centering.

    Returns:
        HPGL text ``SP{pen};<strokes...>``, empty when the pass has no ink.
    """
    parts: List[str] = [f"SP{pen}"]
    for relief_stroke in relief_pass.strokes:
        stroke = relief_stroke.stroke
        if stroke.is_empty:
            continue
        parts.append(stroke.translate(0.0, y_center).mirrored_y(y_span).to_hpgl())
    if len(parts) == 1:
        return ""
    return ";".join(parts)


def _ink_y_center(rendered: RenderedLabel) -> float:
    """Mid-Y (inches, render frame) of the label's rendered text ink.

    The plate transform centers the text pen on the label; the relief fill
    box and its emitted sections ride the same centering, so the fill's
    frame origin is the ink's Y center.

    Args:
        rendered: The pre-replacement render.

    Returns:
        The midpoint of the union of the text chunk bounds in Y (0.0 for
        an ink-less label).
    """
    lows: List[float] = []
    highs: List[float] = []
    for record in rendered.text_chunks:
        lows.append(record.bounds[1])
        highs.append(record.bounds[3])
    if not lows:
        return 0.0
    return (min(lows) + max(highs)) / 2.0


def _rebuild_plt_content(
    rendered: RenderedLabel,
    passes: Sequence[ReliefPass],
    pen_of: Mapping[float, int],
    y_center: float,
) -> str:
    """Drop text/holes, keep the border, append relief sections.

    Args:
        rendered: The pre-replacement render.
        passes: The relief passes.
        pen_of: Physical cutter -> pen number.
        y_center: The centering shift the text pen received (inches).

    Returns:
        The new ``plt_content``.
    """
    kept: List[str] = []
    current_is_border = False
    for raw in rendered.plt_content.split(";"):
        token = raw.strip()
        if not token:
            continue
        if token.startswith("SP"):
            current_is_border = token[2:] == "2"
            if current_is_border:
                kept.append(token)
            continue
        if current_is_border or token in ("IN", "PA"):
            kept.append(token)
    y_span = rendered.y_min + rendered.y_max
    sections = [
        s
        for s in (_relief_section(pen_of[p.cutter.physical], p, y_span, y_center) for p in passes)
        if s
    ]
    body = ";".join(kept)
    if sections:
        body = f"{body};{';'.join(sections)}"
    return f"{body};SP;"


def _relief_records(
    passes: Sequence[ReliefPass],
    pen_of: Mapping[float, int],
) -> List[TextChunkRecord]:
    """One chunk record per relief stroke (fill strokes, then outlines)."""
    records: List[TextChunkRecord] = []
    for relief_pass in passes:
        pen = pen_of[relief_pass.cutter.physical]
        for relief_stroke in relief_pass.strokes:
            bounds = relief_stroke.stroke.bounds()
            if bounds is None:
                continue
            records.append(
                TextChunkRecord(
                    line_index=-1,
                    word_index=None,
                    word_text="",
                    pen=pen,
                    blocks=(TextBlock(strokes=(relief_stroke.stroke,)),),
                    bounds=bounds,
                )
            )
    return records


def apply_relief_to_plates(
    packed_plates: Sequence[object],
    rendered_labels_map: Dict[str, RenderedLabel],
    layer_by_pen: Mapping[int, PenLayer],
    pen_map: Mapping[PenLayer, int],
    available_cutters: Sequence[float],
    logger: Optional[TextLogger] = None,
) -> Dict[int, float]:
    """Generate relief chunk records for every packed relief label.

    Mutates ``rendered_labels_map`` in place (relief labels replaced).

    Args:
        packed_plates: The layout engine's packed plates (each exposes
            ``labels``, and each packed label exposes ``source_label``).
        rendered_labels_map: Render cache keyed by label id.
        layer_by_pen: Pen -> ``(cutter, color)`` (text + relief layers).
        pen_map: ``(cutter, color)`` -> pen (must contain the relief
            layers, see :func:`relief_pen_layers`).
        available_cutters: The shop inventory (preset selection).
        logger: Optional logger (cutter ladder INFO, residual WARNINGs).

    Returns:
        Relief pen -> effective engraved width (inches) for the export's
        preview width bookkeeping.
    """
    relief_widths: Dict[int, float] = {}
    done: set[str] = set()
    for plate in packed_plates:
        for packed in plate.labels:  # type: ignore[attr-defined]
            label: ResolvedLabel = packed.source_label
            if not label.relief_engraving or label.id in done:
                continue
            done.add(label.id)
            rendered = rendered_labels_map.get(label.id)
            if rendered is None:
                continue
            ink = label_ink(rendered, pen_widths_for_label(dict(layer_by_pen)))
            # The ink's Y center in the render frame: the label box (and
            # every centering shift) rides it -- the plate transform centers
            # the text pen on the label, so the relief fill box is centered
            # where the ink is, not on the origin.
            c_text = _ink_y_center(rendered)
            # Relief rides the label box, not the text band centering: the
            # emitted sections lift the fill box (centered on the ink) to
            # the border box (centered on height/2) before the Y mirror,
            # and the plate transform is forced (via the band offset) to
            # the same lift.
            y_center = label.height / 2.0 - c_text
            largest_candidate = max(
                list(label.relief_engraving_cutters) + [float(c) for c in available_cutters],
                default=0.0,
            )
            border_band = (
                label.effective_hole_cutter_diameter / 2.0 + largest_candidate / 2.0
                if label.relief_engraving_preserve_border
                else 0.0
            )
            regions = build_engrave_regions(
                label.width,
                label.height,
                ink,
                preserve_border=label.relief_engraving_preserve_border,
                border_band=border_band,
                box_y_center=c_text,
            )
            # Relief neglects cutter_buffer_text under all circumstances:
            # the ladder runs on exact diameters (bypass list) and physical
            # inventory tools (preset selection).
            ladder = _plan_ladder(label, regions, available_cutters, logger)
            if not ladder:
                continue
            passes = generate_relief_passes(
                regions,
                ladder,
                logger=logger,
                log_prefix=f"label {label.id}: ",
            )
            if not passes:
                continue
            pen_of = {c.physical: pen_map[(c.physical, "none")] for c in ladder}
            records = _relief_records(passes, pen_of)
            rendered_labels_map[label.id] = replace(
                rendered,
                text_chunks=tuple(records),
                plt_content=_rebuild_plt_content(rendered, passes, pen_of, y_center),
                # Force the plate transform's centering shift to zero: the
                # fill box (centered on the ink) must map straight onto the
                # border box, and the symmetric fill's current center is
                # c_text -- expected + band == c_text lands shift at 0.
                band_center_offset_inches=0.0,
            )
            for relief_pass in passes:
                pen = pen_of[relief_pass.cutter.physical]
                relief_widths[pen] = relief_pass.cutter.effective
            if logger is not None:
                sizes = ", ".join(f"{p.cutter.physical:g}in" for p in passes)
                chunks = sum(len(p.strokes) for p in passes)
                logger.info(
                    f"label {label.id}: relief engraving -- cutters {sizes}, {chunks} chunks"
                )
    return relief_widths


__all__ = ["apply_relief_to_plates", "relief_pen_layers"]
