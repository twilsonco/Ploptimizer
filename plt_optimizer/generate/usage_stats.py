r"""Job usage metrics for the ``generate`` pipeline's production report.

The generation report (:mod:`plt_optimizer.generate.generation_report`) answers
*what the pipeline decided* (compression, cutter downsizes, warnings). This
module answers the shop-floor question the same run leaves behind: *what did the
job consume* -- how many labels, characters, drill holes, how much material, and
how far the tool actually travelled.

One entry point, :func:`build_usage_stats`, turns the objects the export already
leaves behind into the ``usage`` block of ``report.json`` (and the ``Job usage``
section of ``summary.txt``). It measures, it does not model: nothing here is
emitted, so generated PLT/PDF bytes stay byte-identical.

Metric sources
--------------

* **Counts** (labels, characters, holes, cutters, sizes, materials) come from
  the resolved specs -- design intent, so every multi-up copy is counted --
  together with the packed placements and the per-line scale factors.
* **Material** is the plate area consumed: the plate's usable size plus its
  clearances, i.e. the sheet the shop cuts, grouped by the plate's stock
  material. The labels' nominal (spec ``width x height``) area is reported
  alongside; the gap between the two is the packer's waste, and for round
  labels the corners the packer cannot use.
* **Tool distance is measured from the emitted per-cutter PLT files** with the
  production HPGL parser, so it is exact machine truth: post-optimization rapid
  travel, relief passes, borders, and drill holes all included. Plan-derived
  distances would report pre-optimization air travel (overstated) and miss the
  relief passes, which are injected into the plate content *after* the render
  and pack stages.

Per-label attribution
---------------------

The packer records every placement as a ``PackedLabel`` rect in inches, and
:func:`~plt_optimizer.generate.vectorize.assemble_plt_from_rendered_labels`
lands that label on the plate with a plain **additive** translate of the
placement offset - no mirror, no Y flip (``[x, x + H] x [y, y + W]`` for a
sideways placement) - so a placement's slot is exactly
``[x, x + width] x [y, y + height]`` in the same device plotter-unit frame the
written files use. Rotation, the plate origin shift and the plate's Y direction
are applied by production code; nothing here re-derives a coordinate transform.

Distance is attributed **per segment**, not per path: the sweep and merge
optimizers routinely join contours that were laid out apart, so one parsed path
can carry geometry belonging to several labels, and a label whose content
overruns its reserved box straddles its neighbours. A cutting segment follows
the slot containing its midpoint, a rapid lead follows its destination (that is
the label the tool is travelling TO), and the inter-path gap the point the tool
arrived at. A segment no slot explains is reported under ``unattributed`` --
never silently dropped - so the per-label rows always sum to the file totals.

The border/hole split of a ``bh`` file comes from the combined content's pen
layers (:data:`LAYER_BOUNDARY` / :data:`LAYER_HOLES`), keyed by stroke
signature, because the per-cutter files carry no ``SP`` pen selects.

Determinism
-----------

Every bucket list is ordered by count (descending) then key (ascending), and
every float is rounded to :data:`USAGE_FLOAT_DECIMALS`, so two identical runs
produce byte-comparable JSON.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from plt_optimizer.core.models import ArcSegment, PLTDocument, StrokePath
from plt_optimizer.core.parser import PLTParser
from plt_optimizer.core.units import inches_to_units, units_to_inches
from plt_optimizer.generate.resolution import ResolvedLabel, hole_engraved_radius
from plt_optimizer.generate.vectorize import (
    LAYER_BOUNDARY,
    LAYER_HOLES,
    _cutter_token_to_inches,
    _format_material_tag,
    _parse_plt_stem,
    extract_pens_from_plt_text,
)

# --------------------------------------------------------------------------
# Public constants (part of the report's contract)
# --------------------------------------------------------------------------

#: Number of decimals every float in the ``usage`` block is rounded to. Keeps
#: two identical runs byte-comparable (mirrors the report's ``FLOAT_DECIMALS``).
USAGE_FLOAT_DECIMALS: int = 6

#: Square inches per square foot.
SQ_IN_PER_SQ_FT: float = 144.0

#: Inches per foot (distances are reported in inches with a foot conversion).
IN_PER_FT: float = 12.0

#: Stroke classes. Text-layer strokes of a relief label are
#: :data:`RELIEF_CLASS` because the relief passes share the text cutter's pen
#: layer with plain text layers -- the owner label is what tells them apart.
TEXT_CLASS: str = "text"
RELIEF_CLASS: str = "relief"
BORDER_CLASS: str = "borders"
HOLE_CLASS: str = "holes"
UNATTRIBUTED_CLASS: str = "unattributed"

#: Every stroke class, in report order. ``by_class`` always carries all of
#: them: a legitimate zero is information, a missing bucket is a bug.
DISTANCE_CLASSES: Tuple[str, ...] = (
    TEXT_CLASS,
    RELIEF_CLASS,
    BORDER_CLASS,
    HOLE_CLASS,
    UNATTRIBUTED_CLASS,
)

#: Per-cutter file layer kinds (the ``_<kind>_`` token of the file name).
KIND_TEXT: str = "txt"
KIND_STRUCTURAL: str = "bh"

#: Label placement slot padding, in inches. Relief islands, descender relief,
#: and the cutter radius can push a stroke a hair outside the nominal
#: ``width x height`` box; the padding absorbs that without ever reaching a
#: neighbouring slot (the packer keeps a larger gap than this).
SLOT_TOL_IN: float = 0.02

#: How far a segment's measured **point** may sit outside every slot and still
#: be attributed to the nearest one, in inches. A stroke can legitimately
#: overhang (a relief pass covering the whole island, a descender relief, a
#: cutter-radius bleed). Anything further out - a full-plate border, a drill
#: guide, geometry the packer never placed - is charged to
#: :data:`UNATTRIBUTED_CLASS` instead of being silently dumped on the closest
#: label.
SLOT_CENTRE_TOL_IN: float = 0.25

#: Placeholder for "no cutter size recorded" in the cutter buckets (a stem the
#: file-name cap truncated, or a file that does not follow the naming scheme).
UNKNOWN_CUTTER: str = "unknown"

#: Bucket key of a per-cutter file whose plate could not be resolved.
UNRESOLVED_PLATE: int = -1

#: Maximum number of per-label distance rows listed in ``summary.txt``
#: before the list is truncated with a pointer to ``report.json`` (mirrors
#: ``generation_report.SUMMARY_FINDING_LIMIT``).
SUMMARY_USAGE_LABEL_LIMIT: int = 50

#: Rendered for a metric that is legitimately absent (``None`` in JSON).
MISSING_TEXT: str = "n/a"


# --------------------------------------------------------------------------
# Geometry helpers (device plotter units)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Rect:
    """An axis-aligned rectangle in device plotter units.

    Attributes:
        x_min: Left edge.
        y_min: Bottom edge (the PLT Y axis runs up).
        x_max: Right edge.
        y_max: Top edge.
    """

    x_min: float
    y_min: float
    x_max: float
    y_max: float

    @property
    def area(self) -> float:
        """Return the rectangle's area in square plotter units."""
        return max(0.0, self.x_max - self.x_min) * max(0.0, self.y_max - self.y_min)

    @property
    def center(self) -> Tuple[float, float]:
        """Return the rectangle's centre point."""
        return ((self.x_min + self.x_max) / 2.0, (self.y_min + self.y_max) / 2.0)

    def padded(self, amount: float) -> _Rect:
        """Return the rectangle grown by ``amount`` on every edge.

        Args:
            amount: Padding in plotter units.

        Returns:
            The grown rectangle.
        """
        return _Rect(
            x_min=self.x_min - amount,
            y_min=self.y_min - amount,
            x_max=self.x_max + amount,
            y_max=self.y_max + amount,
        )

    def covers(self, other: _Rect) -> bool:
        """Return whether this rectangle fully contains ``other``.

        Args:
            other: The rectangle to test.

        Returns:
            True when ``other`` lies entirely inside this rectangle.
        """
        return (
            self.x_min <= other.x_min
            and self.x_max >= other.x_max
            and self.y_min <= other.y_min
            and self.y_max >= other.y_max
        )


@dataclass(frozen=True)
class _Slot:
    """One label placement together with its device-unit slot rectangle.

    Attributes:
        label_id: Placement id (what the packer records -- the spec id for a
            single-copy label, the instance id for a multi-up label).
        spec_id: The resolved label's spec id.
        rect: The slot rectangle in device plotter units.
        is_relief: Whether the placed label is relief-engraved.
    """

    label_id: str
    spec_id: str
    rect: _Rect
    is_relief: bool


@dataclass
class _PlateFrame:
    """The attribution index for one plate.

    Attributes:
        plate_number: The 1-based plate number.
        material: The plate's stock material name (``None`` = the plate
            carries no material).
        slots: The placement slots, in packing order.
        stroke_class: Stroke signature to :data:`BORDER_CLASS` /
            :data:`HOLE_CLASS`, used to split a ``bh`` file's travel (the
            per-cutter files carry no ``SP`` pen selects).
    """

    plate_number: int
    material: Optional[str] = None
    slots: List[_Slot] = field(default_factory=list)
    stroke_class: Dict[Tuple[Any, ...], str] = field(default_factory=dict)


def _path_signature(path: StrokePath) -> Tuple[Any, ...]:
    """Return a hashable geometry signature of a stroke path.

    Two paths that draw the same contour carry the same signature, which is how
    a stroke read back from a written per-cutter file is matched to the layer it
    came from in the plate's combined content. The values are produced by the
    same parser on both sides, so they compare exactly.

    Args:
        path: The parsed stroke path.

    Returns:
        The path's geometry signature.
    """
    parts: List[Any] = []
    if path.pen_up_position is not None:
        parts.append(("PU", path.pen_up_position.x, path.pen_up_position.y))
    for seg in path.segments:
        if isinstance(seg, ArcSegment):
            parts.append(
                (
                    "AA",
                    seg.start.x,
                    seg.start.y,
                    seg.center.x,
                    seg.center.y,
                    seg.end.x,
                    seg.end.y,
                    seg.sweep_angle,
                    seg.is_cutting,
                )
            )
        else:
            parts.append(
                (
                    "PD",
                    seg.start.x,
                    seg.start.y,
                    seg.end.x,
                    seg.end.y,
                    seg.is_cutting,
                )
            )
    return tuple(parts)


def _structural_classes(combined: str) -> Dict[Tuple[Any, ...], str]:
    """Split a plate's combined content into border and hole stroke classes.

    Args:
        combined: The plate's combined (all pens) PLT content.

    Returns:
        A mapping of stroke signature to :data:`BORDER_CLASS` /
        :data:`HOLE_CLASS`. The border layer is read last, so a contour drawn in
        both layers keeps its border class (borders are engraved before the
        punches, and a pen-2 rectangle is never a drill hole).
    """
    parser = PLTParser()
    classes: Dict[Tuple[Any, ...], str] = {}
    for path in parser.parse_string(
        extract_pens_from_plt_text(combined, [LAYER_HOLES])
    ).stroke_paths:
        classes[_path_signature(path)] = HOLE_CLASS
    for path in parser.parse_string(
        extract_pens_from_plt_text(combined, [LAYER_BOUNDARY])
    ).stroke_paths:
        classes[_path_signature(path)] = BORDER_CLASS
    return classes


def _slot_rect(
    x: float,
    y: float,
    width: float,
    height: float,
    rotated: bool,
) -> _Rect:
    """Return a placement slot as a device-unit rectangle.

    The plate frame is fixed by the assembly, not probed: the renderer draws a
    label into its local ``[0, width] x [0, height]`` box (Y up, origin at the
    slot's lower-left) and
    :func:`~plt_optimizer.generate.vectorize.assemble_plt_from_rendered_labels`
    lands it with a plain additive
    :func:`~plt_optimizer.generate.vectorize.translate_plt_coordinates` of
    ``(placement.x, placement.y)`` -- no mirroring and no Y flip (see the
    ``[x, x+H] x [y, y+W]`` slot comment there for a rotated placement). So the
    slot of a placement is exactly ``[x, x+width] x [y, y+height]`` in the
    emitted content's frame, in inches.

    Args:
        x: Slot origin X in inches (from the plate frame's origin).
        y: Slot origin Y in inches (from the plate frame's origin).
        width: The label's nominal width in inches (unrotated).
        height: The label's nominal height in inches (unrotated).
        rotated: Whether the packer placed the label sideways.

    Returns:
        The slot rectangle in device plotter units.
    """
    origin_x = inches_to_units(x)
    origin_y = inches_to_units(y)
    span_w = inches_to_units(width)
    span_h = inches_to_units(height)
    # A sideways placement occupies the swapped slot [x, x + H] x [y, y + W].
    x_span, y_span = (span_h, span_w) if rotated else (span_w, span_h)
    return _Rect(origin_x, origin_y, origin_x + x_span, origin_y + y_span)


def _slots_for(placements: Sequence[Any]) -> List[_Slot]:
    """Build the slot list of one plate's placements.

    Args:
        placements: The plate's ``PackedLabel`` placements, in packing order.

    Returns:
        One slot per placement, in packing order.
    """
    slots: List[_Slot] = []
    for placement in placements:
        source = getattr(placement, "source_label", None)
        slots.append(
            _Slot(
                label_id=str(getattr(placement, "label_id", "") or ""),
                spec_id=str(getattr(source, "id", "") or ""),
                rect=_slot_rect(
                    float(placement.x),
                    float(placement.y),
                    float(placement.width),
                    float(placement.height),
                    bool(placement.rotated),
                ),
                is_relief=bool(getattr(source, "relief_engraving", False)),
            )
        )
    return slots


def _point_owner(
    frame: Optional[_PlateFrame],
    x: float,
    y: float,
) -> Optional[_Slot]:
    """Return the slot that owns a point of toolpath geometry.

    Args:
        frame: The owning plate's frame, or ``None`` when unresolved.
        x: Device-unit X of the point to attribute.
        y: Device-unit Y of the point to attribute.

    Returns:
        The slot containing the point (padded by :data:`SLOT_TOL_IN`, nearest
        centre winning when overlapping slots both contain it); the nearest slot
        when the point only overhangs, i.e. it stays within
        :data:`SLOT_CENTRE_TOL_IN` of a slot; ``None`` when the plate carries no
        placements or the point belongs to no label (charged to
        :data:`UNATTRIBUTED_CLASS`).
    """
    if frame is None or not frame.slots:
        return None
    point = _Rect(x, y, x, y)

    def distance(slot: _Slot) -> Tuple[float, str]:
        cx, cy = slot.rect.center
        return ((cx - x) ** 2 + (cy - y) ** 2, slot.label_id)

    inside = [
        slot for slot in frame.slots if slot.rect.padded(inches_to_units(SLOT_TOL_IN)).covers(point)
    ]
    if inside:
        return min(inside, key=distance)
    # The point sits outside every slot (a relief pass covering the whole
    # island, a descender relief, cutter-radius bleed, a label that overruns
    # its reserved box): attribute on proximity, but only while the point is
    # still close to a slot.
    nearest = min(frame.slots, key=distance)
    if nearest.rect.padded(inches_to_units(SLOT_CENTRE_TOL_IN)).covers(point):
        return nearest
    return None


# --------------------------------------------------------------------------
# JSON shaping helpers
# --------------------------------------------------------------------------


def _round(value: Optional[float]) -> Optional[float]:
    """Round a float for the report.

    Args:
        value: The value, or ``None``.

    Returns:
        The value rounded to :data:`USAGE_FLOAT_DECIMALS`, or ``None``.
    """
    if value is None:
        return None
    return round(float(value), USAGE_FLOAT_DECIMALS)


def _band(value: float) -> float:
    """Round a bucket key for comparison.

    Args:
        value: The key value.

    Returns:
        The value rounded to :data:`USAGE_FLOAT_DECIMALS`.
    """
    return round(float(value), USAGE_FLOAT_DECIMALS)


def _sort_token(key: Any) -> Tuple[int, float, str]:
    """Return a total order key for a heterogeneous bucket key.

    ``None`` sorts first, then numbers, then strings, so bucket ordering is
    stable over mixed keys (a material name and a cutter diameter never compare
    directly, and Python 3 refuses to order them).

    Args:
        key: The bucket key.

    Returns:
        A comparable tuple.
    """
    if key is None:
        return (0, 0.0, "")
    if isinstance(key, bool):
        return (1, float(key), "")
    if isinstance(key, (int, float)):
        return (1, float(key), "")
    if isinstance(key, tuple):
        # Composite keys (a size bucket) order by their rounded width.
        return (1, float(key[0]), "|".join(str(part) for part in key[1:]))
    return (2, 0.0, str(key))


def _bucket_rows(
    counts: Mapping[Any, int],
    fields_for: Callable[[Any], Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Render a bucket counter as report rows, biggest first.

    Args:
        counts: Bucket key to count total.
        fields_for: Callable turning a bucket key into the row's fields.

    Returns:
        One row per bucket, ordered by count descending then key ascending.
    """
    return [
        dict(fields_for(key), count=count)
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], _sort_token(item[0])))
    ]


def _material_label(material: Optional[str]) -> str:
    """Return the display tag for a material name.

    Args:
        material: The stock material name, or ``None``.

    Returns:
        The name, or ``"(unset)"`` for the material-agnostic plate case.
    """
    return material if material else "(unset)"


def _zero_classes() -> Dict[str, float]:
    """Return a fresh all-zero stroke-class accumulator.

    Returns:
        Every :data:`DISTANCE_CLASSES` entry mapped to ``0.0``.
    """
    return dict.fromkeys(DISTANCE_CLASSES, 0.0)


# --------------------------------------------------------------------------
# Section builders: labels / characters / holes / material
# --------------------------------------------------------------------------


def _label_cutters(label: ResolvedLabel) -> List[float]:
    """Return the distinct cutter diameters a label engraves with.

    Args:
        label: The resolved label.

    Returns:
        Sorted distinct effective diameters (text lines, relief ladder, and the
        boundary/hole tool).
    """
    cutters: List[float] = []
    for line in label.content:
        if line.effective_cutter_diameter is not None:
            cutters.append(_band(line.effective_cutter_diameter))
    for extra in (label.effective_hole_cutter_diameter, label.hole_cutter_diameter):
        if extra is not None:
            cutters.append(_band(extra))
    for extra in getattr(label, "relief_engraving_cutters", None) or ():
        cutters.append(_band(extra))
    return sorted(set(cutters))


def _labels_usage(resolved_labels: Sequence[ResolvedLabel]) -> Dict[str, Any]:
    """Aggregate the label inventory of the job.

    Counts are physical labels (a multi-up label contributes ``count``), so the
    ``instances`` total matches the number of placements on the plates.

    Args:
        resolved_labels: The job's resolved labels, in content order.

    Returns:
        The ``usage.labels`` block.
    """
    instances = sum(int(label.count) for label in resolved_labels)
    by_shape: Counter[Any] = Counter()
    by_relief: Counter[Any] = Counter()
    by_material: Counter[Any] = Counter()
    by_size: Counter[Any] = Counter()
    by_cutter: Counter[Any] = Counter()
    for label in resolved_labels:
        count = int(label.count)
        shape = str(getattr(label, "shape_type", "rect") or "rect")
        material = label.material
        by_shape[shape] += count
        by_relief["relief" if label.relief_engraving else "plain"] += count
        by_material[material] += count
        by_size[(_band(label.width), _band(label.height), shape, material)] += count
        # One label can engrave with several cutters (per-line overrides, the
        # bold partner, the boundary/hole tool): count it once per diameter.
        for diameter in _label_cutters(label):
            by_cutter[diameter] += count

    return {
        "instances": instances,
        "specs": len(resolved_labels),
        "by_shape": _bucket_rows(by_shape, lambda key: {"shape": key}),
        "by_relief": _bucket_rows(by_relief, lambda key: {"engraving": key}),
        "by_material": _bucket_rows(
            by_material,
            lambda key: {"material": key, "material_label": _material_label(key)},
        ),
        "by_size": _bucket_rows(
            by_size,
            lambda key: {
                "width": key[0],
                "height": key[1],
                "shape": key[2],
                "material": key[3],
            },
        ),
        "by_cutter": _bucket_rows(by_cutter, lambda key: {"cutter_diameter": key}),
    }


def _countable_text(line: Any) -> str:
    """Return the characters a text line engraves, spaces excluded.

    Args:
        line: The resolved text line.

    Returns:
        ``plain_text`` with every whitespace character removed.
    """
    text = str(getattr(line, "plain_text", "") or "")
    return "".join(char for char in text if not char.isspace())


def _countable_markers(line: Any) -> str:
    """Return the glyphs a line's list marker engraves, spaces excluded.

    Args:
        line: The resolved text line.

    Returns:
        ``marker_text`` with every whitespace character removed.
    """
    text = str(getattr(line, "marker_text", "") or "")
    return "".join(char for char in text if not char.isspace())


def _line_char_bands(line: Any) -> List[Tuple[Tuple[float, float], int]]:
    """Return a line's character counts per engraved text height.

    Each band is a ``(nominal, engraved)`` pair because the shop orders by the
    nominal height while the machine cuts the toolpath height: compression, the
    cutter-diameter downsize and a bold partner all move the engraved value off
    the nominal one, and the pair is what makes ``0.25 (0.205)`` readable.

    ``<scale:N>`` / ``<sup>`` / ``<sub>`` / ``<sc>`` markup scales part of a
    line, so a character's band is its *engraved* height: the line's toolpath
    height times the per-character scale factor
    (:attr:`ResolvedTextLine.scale_values`, empty when the line carries no scale
    markup), with the nominal scaled by the same factor. A bold line reports its
    bold partner's height, which is the width the machine cuts with.

    Args:
        line: The resolved text line.

    Returns:
        ``((nominal_height, engraved_height), character_count)`` pairs, sorted,
        whitespace excluded.
    """
    text = _countable_text(line)
    if not text:
        return []
    scales: Sequence[float] = list(getattr(line, "scale_values", ()) or ())
    # The nominal side of every band: the ordered font height. There is no
    # separate bold nominal - bold_toolpath_text_height IS
    # ``nominal_text_height - the bold cutter's compensation``.
    nominal_base = float(line.nominal_text_height)
    bold_base = getattr(line, "bold_toolpath_text_height", None)
    if bold_base is not None:
        base = float(bold_base)
    else:
        # A toolpath height of 0 means the line was never laid out (a spec
        # built outside the cascade); the nominal height is the band the shop
        # orders cutters by.
        base = float(line.toolpath_text_height) or nominal_base
    nominal_base = _band(nominal_base)
    if len(scales) != len(text):
        # No (or desynchronized) per-character factors: the line engraves at its
        # own height.
        return [((nominal_base, _band(base)), len(text))]
    bands: Counter[Any] = Counter()
    for index in range(len(text)):
        scale = float(scales[index])
        bands[(_band(nominal_base * scale), _band(base * scale))] += 1
    return [(band, int(count)) for band, count in sorted(bands.items())]


def _characters_usage(resolved_labels: Sequence[ResolvedLabel]) -> Dict[str, Any]:
    """Aggregate the engraved character counts of the job.

    Spaces are excluded everywhere (they engrave nothing) and the count is
    reported per **text height band**, keyed by the ``(nominal, engraved)`` pair
    so the ordered size and the size the machine cuts are both visible (a
    0.250 in name compressed to 0.205 in is one band, ``0.25 (0.205)``). List
    markers are counted separately from the text so the two never double up.

    Args:
        resolved_labels: The job's resolved labels.

    Returns:
        The ``usage.characters`` block.
    """
    total = 0
    markers = 0
    spaces = 0
    by_height: Counter[Any] = Counter()
    by_label: Counter[Any] = Counter()
    for label in resolved_labels:
        count = int(label.count)
        for line in label.content:
            text = _countable_text(line)
            raw = str(getattr(line, "plain_text", "") or "")
            total += len(text) * count
            markers += len(_countable_markers(line)) * count
            spaces += len(raw) - len(text)
            by_label[label.id] += len(text) * count
            for band, chars in _line_char_bands(line):
                by_height[band] += chars * count

    return {
        "total": total,
        "markers": markers,
        "spaces_excluded": spaces,
        "by_text_height": _bucket_rows(
            by_height,
            lambda key: {"nominal_text_height": key[0], "text_height": key[1]},
        ),
        "by_label": _bucket_rows(by_label, lambda key: {"label_id": key}),
    }


def _holes_usage(resolved_labels: Sequence[ResolvedLabel]) -> Dict[str, Any]:
    """Aggregate the drill-hole counts of the job.

    Holes are bucketed by the diameter the spec asks for, together with the
    diameter the machine emits (the cutter-compensated circle a routed hole
    becomes when ``hole_adjust_for_cutter`` is on).

    Args:
        resolved_labels: The job's resolved labels.

    Returns:
        The ``usage.holes`` block.
    """
    total = 0
    by_diameter: Counter[Any] = Counter()
    by_label: Counter[Any] = Counter()
    engraved_by_diameter: Dict[float, float] = {}
    for label in resolved_labels:
        count = int(label.count)
        for hole in label.holes:
            specified = _band(hole.diameter)
            engraved_by_diameter[specified] = _band(
                2.0
                * hole_engraved_radius(
                    hole.diameter,
                    label.effective_hole_cutter_diameter,
                    label.hole_adjust_for_cutter,
                )
            )
            total += count
            by_diameter[specified] += count
            by_label[label.id] += count

    return {
        "total": total,
        "by_diameter": _bucket_rows(
            by_diameter,
            lambda key: {"diameter": key, "engraved_diameter": engraved_by_diameter.get(key)},
        ),
        "by_label": _bucket_rows(by_label, lambda key: {"label_id": key}),
    }


def _material_usage(
    packed_plates: Sequence[Any],
    material_by_plate: Mapping[int, Optional[str]],
    resolved_labels: Sequence[ResolvedLabel],
) -> Dict[str, Any]:
    """Aggregate the material the job consumes.

    Args:
        packed_plates: The packed plates, in packing order.
        material_by_plate: Stock material name per 1-based plate number.
        resolved_labels: The job's resolved labels (for the nominal area).

    Returns:
        The ``usage.material`` block.
    """
    plates_by_material: Counter[Any] = Counter()
    labels_by_material: Counter[Any] = Counter()
    area_by_material: Dict[Any, float] = {}
    bbox_by_material: Dict[Any, float] = {}
    total_area = 0.0
    total_bbox = 0.0
    for number, plate in enumerate(packed_plates, start=1):
        material = material_by_plate.get(number, getattr(plate, "material", None))
        span_x = float(plate.left_clearance) + float(plate.width)
        span_y = float(plate.top_clearance) + float(plate.height)
        area = span_x * span_y
        # The boxes the packer reserved, summed: wider than the nominal
        # label area (clearances + rotation) and narrower than the plate
        # when a sheet is only partly filled.
        bbox = sum(float(item.width) * float(item.height) for item in plate.labels)
        total_area += area
        total_bbox += bbox
        plates_by_material[material] += 1
        area_by_material[material] = area_by_material.get(material, 0.0) + area
        bbox_by_material[material] = bbox_by_material.get(material, 0.0) + bbox
        labels_by_material[material] += sum(
            int(placement.source_label.count) for placement in plate.labels
        )

    nominal = sum(
        float(label.width) * float(label.height) * int(label.count) for label in resolved_labels
    )
    materials = sorted(plates_by_material, key=_sort_token)

    return {
        "total": {
            "plates": len(packed_plates),
            "sq_in": _round(total_area),
            "sq_ft": _round(total_area / SQ_IN_PER_SQ_FT),
        },
        "by_material": [
            {
                "material": key,
                "material_label": _material_label(key),
                "plates": int(plates_by_material[key]),
                "sq_in": _round(float(area_by_material[key])),
                "sq_ft": _round(float(area_by_material[key]) / SQ_IN_PER_SQ_FT),
                "label_instances": int(labels_by_material[key]),
                "label_bbox_sq_in": _round(float(bbox_by_material[key])),
                "label_bbox_sq_ft": _round(float(bbox_by_material[key]) / SQ_IN_PER_SQ_FT),
            }
            for key in materials
        ],
        "label_nominal_area": {
            "sq_in": _round(nominal),
            "sq_ft": _round(nominal / SQ_IN_PER_SQ_FT),
        },
        "label_bbox_area": {
            "sq_in": _round(total_bbox),
            "sq_ft": _round(total_bbox / SQ_IN_PER_SQ_FT),
        },
    }


# --------------------------------------------------------------------------
# Distance measurement
# --------------------------------------------------------------------------


@dataclass
class _DistanceTotals:
    """Accumulator for measured toolpath distances (inches).

    Attributes:
        cutting: Total pen-down distance.
        rapid: Total pen-up (air travel) distance, inter-path gaps included.
        by_class: Cutting distance per stroke class.
        by_cutter: ``[cutting, rapid, files]`` per (cutter diameter, layer kind).
        by_material: ``[cutting, rapid]`` per stock material.
        by_plate: ``[cutting, rapid]`` per plate id.
        by_label: ``[cutting, rapid]`` per label placement id.
        class_by_label: Stroke-class cutting distance per placement id.
        files: One report row per measured file.
        unattributed_strokes: Strokes no slot could explain.
        unattributed_cutting: Cutting distance of those strokes.
        unattributed_rapid: Rapid distance of those strokes.
    """

    cutting: float = 0.0
    rapid: float = 0.0
    by_class: Dict[str, float] = field(default_factory=_zero_classes)
    by_cutter: Dict[Tuple[Any, Any], List[float]] = field(default_factory=dict)
    by_material: Dict[Any, List[float]] = field(default_factory=dict)
    by_plate: Dict[int, List[float]] = field(default_factory=dict)
    by_label: Dict[str, List[float]] = field(default_factory=dict)
    class_by_label: Dict[str, Dict[str, float]] = field(default_factory=dict)
    files: List[Dict[str, Any]] = field(default_factory=list)
    unattributed_strokes: int = 0
    unattributed_cutting: float = 0.0
    unattributed_rapid: float = 0.0

    def add_cutting(self, value: float, stroke_class: str, label_id: Optional[str]) -> None:
        """Record a cutting distance.

        Args:
            value: Distance in inches.
            stroke_class: One of :data:`DISTANCE_CLASSES`.
            label_id: The owning placement id, or ``None`` when unattributed.
        """
        self.cutting += value
        self.by_class[stroke_class] = self.by_class.get(stroke_class, 0.0) + value
        if label_id is None:
            self.unattributed_cutting += value
            return
        self.by_label.setdefault(label_id, [0.0, 0.0])[0] += value
        row = self.class_by_label.setdefault(label_id, _zero_classes())
        row[stroke_class] = row.get(stroke_class, 0.0) + value

    def add_rapid(self, value: float, label_id: Optional[str]) -> None:
        """Record a rapid (pen-up) distance, charged to its destination label.

        Args:
            value: Distance in inches.
            label_id: The owning placement id, or ``None`` when unattributed.
        """
        self.rapid += value
        if label_id is None:
            self.unattributed_rapid += value
            return
        self.by_label.setdefault(label_id, [0.0, 0.0])[1] += value

    def merge(self, other: _DistanceTotals) -> None:
        """Fold another accumulator (one file's measurements) into this one.

        Args:
            other: The measurements to add.
        """
        self.cutting += other.cutting
        self.rapid += other.rapid
        for class_name, value in other.by_class.items():
            self.by_class[class_name] = self.by_class.get(class_name, 0.0) + value
        self.unattributed_strokes += other.unattributed_strokes
        self.unattributed_cutting += other.unattributed_cutting
        self.unattributed_rapid += other.unattributed_rapid
        for label_id, (cut, rapid) in other.by_label.items():
            bucket = self.by_label.setdefault(label_id, [0.0, 0.0])
            bucket[0] += cut
            bucket[1] += rapid
        for label_id, classes in other.class_by_label.items():
            class_bucket = self.class_by_label.setdefault(label_id, _zero_classes())
            for class_name, value in classes.items():
                class_bucket[class_name] = class_bucket.get(class_name, 0.0) + value


def _measure_document(
    document: PLTDocument,
    frame: Optional[_PlateFrame],
    kind: Optional[str],
    totals: _DistanceTotals,
) -> int:
    """Measure one parsed per-cutter file into the accumulators.

    Attribution walks the path's segments (see the module docstring): the
    per-segment lengths sum to the path's own ``cutting_distance`` /
    ``rapid_distance``, so the per-file, per-cutter and per-plate rows stay
    exact while a path that spans labels is split across them.

    Args:
        document: The parsed file.
        frame: The owning plate's frame, or ``None`` when unresolved.
        kind: The file's layer kind (:data:`KIND_TEXT` / :data:`KIND_STRUCTURAL`).
        totals: The accumulator to add into.

    Returns:
        The number of strokes (paths) that carry cutting geometry.
    """
    strokes = 0
    position: Optional[Tuple[float, float]] = None
    for path in document.stroke_paths:
        layer_class = (
            frame.stroke_class.get(_path_signature(path), BORDER_CLASS)
            if kind == KIND_STRUCTURAL and frame is not None
            else None
        )
        cutting = [seg for seg in path.segments if seg.is_cutting]
        if cutting:
            strokes += 1
        # A stroke (path) is unattributed when no cutting segment of it found an
        # owner - the unit that matches the per-file "strokes" count.
        owned_cutting = 0
        # The gap the tool flew between the previous path's end and this path's
        # pen-up destination lands on the label it arrived at.
        if position is not None and path.pen_up_position is not None:
            gap = (
                (path.pen_up_position.x - position[0]) ** 2
                + (path.pen_up_position.y - position[1]) ** 2
            ) ** 0.5
            arrival = _point_owner(frame, path.pen_up_position.x, path.pen_up_position.y)
            totals.add_rapid(
                units_to_inches(gap), arrival.label_id if arrival is not None else None
            )
        for seg in path.segments:
            # Cutting travel follows the segment's midpoint; a rapid lead
            # follows its destination, which is where the next cut starts.
            if seg.is_cutting:
                owner = _point_owner(
                    frame, (seg.start.x + seg.end.x) / 2.0, (seg.start.y + seg.end.y) / 2.0
                )
            else:
                owner = _point_owner(frame, seg.end.x, seg.end.y)
            label_id = owner.label_id if owner is not None else None
            length = units_to_inches(seg.length)
            if seg.is_cutting:
                totals.add_cutting(length, _segment_class(kind, layer_class, owner), label_id)
                if label_id is not None:
                    owned_cutting += 1
            else:
                totals.add_rapid(length, label_id)
        if cutting and owned_cutting == 0:
            totals.unattributed_strokes += 1
        # Where the tool stops is where the next path starts from.
        if path.segments:
            last = path.segments[-1]
            position = (last.end.x, last.end.y)
        elif path.pen_up_position is not None:
            position = (path.pen_up_position.x, path.pen_up_position.y)
    return strokes


def _segment_class(
    kind: Optional[str],
    layer_class: Optional[str],
    owner: Optional[_Slot],
) -> str:
    """Return the stroke class of one segment.

    Args:
        kind: The file's layer kind.
        layer_class: The path's class from the plate's pen layers, or ``None``
            for a text file.
        owner: The owning placement slot, or ``None`` when unattributed.

    Returns:
        One of :data:`DISTANCE_CLASSES`.
    """
    if owner is None:
        return UNATTRIBUTED_CLASS
    if kind == KIND_STRUCTURAL:
        return layer_class if layer_class is not None else UNATTRIBUTED_CLASS
    return RELIEF_CLASS if owner.is_relief else TEXT_CLASS


def _file_plate_number(
    stem: Any,
    plate_numbers: Sequence[int],
    frames: Mapping[int, _PlateFrame],
) -> Optional[int]:
    """Resolve which plate a per-cutter file belongs to.

    The file-name plate token is authoritative when present; a single-plate job
    (and a material that spans exactly one plate) has no token, because
    :func:`~plt_optimizer.generate.vectorize.export_per_cutter_plts` appends it
    only when a material needs disambiguating.

    Args:
        stem: The parsed file-name stem.
        plate_numbers: The plate numbers in report order.
        frames: The per-plate attribution frames.

    Returns:
        The 1-based plate number, or ``None`` when the file cannot be placed.
    """
    if stem.plate is not None:
        # The stem parser only records an all-digit leading token, so this is
        # always a plate number (there is no unparsable form to guard).
        return int(stem.plate)
    if len(plate_numbers) == 1:
        return plate_numbers[0]
    if stem.material:
        matches = [
            number
            for number in plate_numbers
            if number in frames and _format_material_tag(frames[number].material) == stem.material
        ]
        if len(matches) == 1:
            return matches[0]
    return None


def _label_meta(
    packed_plates: Sequence[Any],
    material_by_plate: Mapping[int, Optional[str]],
) -> Dict[str, Tuple[str, int, Optional[str]]]:
    """Index every placement by its spec id, plate, and material.

    Args:
        packed_plates: The packed plates, in packing order.
        material_by_plate: Stock material name per 1-based plate number.

    Returns:
        Placement id to ``(spec id, plate number, material)``.
    """
    meta: Dict[str, Tuple[str, int, Optional[str]]] = {}
    for number, plate in enumerate(packed_plates, start=1):
        material = material_by_plate.get(number, getattr(plate, "material", None))
        for placement in plate.labels:
            meta[str(placement.label_id)] = (str(placement.source_label.id), number, material)
    return meta


def _distance_rows(
    totals: _DistanceTotals,
    frames: Mapping[int, _PlateFrame],
    label_meta: Mapping[str, Tuple[str, int, Optional[str]]],
) -> Dict[str, Any]:
    """Render the measured distances as the report's ``usage.distance`` block.

    Args:
        totals: The accumulators filled by the measurement pass.
        frames: The per-plate attribution frames (for plate materials).
        label_meta: Placement id to ``(spec id, plate number, material)``.

    Returns:
        The distance block, in inches, with a foot conversion on the totals.
    """
    cutting = totals.cutting
    rapid = totals.rapid
    available = bool(totals.files)

    def plate_material(key: int) -> Optional[str]:
        frame = frames.get(key)
        return frame.material if frame is not None else None

    by_cutter_keys = sorted(
        totals.by_cutter,
        key=lambda key: (-totals.by_cutter[key][0], _sort_token(key[0]), str(key[1])),
    )
    by_material_keys = sorted(
        totals.by_material,
        key=lambda key: (-totals.by_material[key][0], _sort_token(key)),
    )
    by_plate_keys = sorted(totals.by_plate, key=lambda key: (-totals.by_plate[key][0], key))
    by_label_keys = sorted(
        totals.by_label,
        key=lambda key: (-totals.by_label[key][0], key),
    )

    return {
        "available": available,
        "note": (
            ""
            if available
            else "No PLT files were found next to the report, so the tool "
            "travel of this job could not be measured."
        ),
        "units": "inches",
        "total": {
            "cutting": _round(cutting),
            "rapid": _round(rapid),
            "total": _round(cutting + rapid),
            "cutting_ft": _round(cutting / IN_PER_FT),
            "rapid_ft": _round(rapid / IN_PER_FT),
            "total_ft": _round((cutting + rapid) / IN_PER_FT),
        },
        "by_movement": {"cutting": _round(cutting), "rapid": _round(rapid)},
        "by_class": {
            class_name: _round(totals.by_class.get(class_name, 0.0))
            for class_name in DISTANCE_CLASSES
        },
        "by_cutter": [
            {
                "cutter": cutter,
                "kind": kind,
                "files": int(totals.by_cutter[key][2]),
                "cutting": _round(totals.by_cutter[key][0]),
                "rapid": _round(totals.by_cutter[key][1]),
            }
            for key in by_cutter_keys
            for cutter, kind in (key,)
        ],
        "by_material": [
            {
                "material": key,
                "material_label": _material_label(key),
                "cutting": _round(totals.by_material[key][0]),
                "rapid": _round(totals.by_material[key][1]),
            }
            for key in by_material_keys
        ],
        "by_plate": [
            {
                "plate": None if key == UNRESOLVED_PLATE else key,
                "material": plate_material(key),
                "cutting": _round(totals.by_plate[key][0]),
                "rapid": _round(totals.by_plate[key][1]),
            }
            for key in by_plate_keys
        ],
        "by_label": [
            {
                "label_id": key,
                "spec_id": label_meta.get(key, (key, UNRESOLVED_PLATE, None))[0],
                "plate": plate_or_none(label_meta.get(key, (key, UNRESOLVED_PLATE, None))[1]),
                "material": label_meta.get(key, (key, UNRESOLVED_PLATE, None))[2],
                "cutting": _round(totals.by_label[key][0]),
                "rapid": _round(totals.by_label[key][1]),
                "by_class": {
                    class_name: _round(totals.class_by_label.get(key, _zero_classes())[class_name])
                    for class_name in DISTANCE_CLASSES
                },
            }
            for key in by_label_keys
        ],
        "files": totals.files,
        "unattributed": {
            "strokes": totals.unattributed_strokes,
            "cutting": _round(totals.unattributed_cutting),
            "rapid": _round(totals.unattributed_rapid),
        },
    }


def plate_or_none(number: int) -> Optional[int]:
    """Return a plate number, or ``None`` for the unresolved sentinel.

    Args:
        number: The 1-based plate number, or :data:`UNRESOLVED_PLATE`.

    Returns:
        The plate number, or ``None``.
    """
    return None if number == UNRESOLVED_PLATE else number


def _distance_usage(
    plt_files: Sequence[Path],
    frames: Mapping[int, _PlateFrame],
    material_by_plate: Mapping[int, Optional[str]],
    label_meta: Mapping[str, Tuple[str, int, Optional[str]]],
) -> Dict[str, Any]:
    """Measure the tool travel of the emitted per-cutter PLT files.

    Args:
        plt_files: The written per-cutter PLT paths, in write order. These are
            what the travel is measured from.
        frames: The per-plate attribution frames.
        material_by_plate: Stock material name per 1-based plate number.
        label_meta: Placement id to ``(spec id, plate number, material)``.

    Returns:
        The ``usage.distance`` block.
    """
    totals = _DistanceTotals()
    plate_numbers = sorted(frames)
    parser = PLTParser()
    for plt_path in plt_files:
        path = Path(plt_path)
        stem = _parse_plt_stem(path.stem)
        plate_number = _file_plate_number(stem, plate_numbers, frames)
        frame = frames.get(plate_number) if plate_number is not None else None
        material = frame.material if frame is not None else stem.material
        cutter: Any = (
            _round(_cutter_token_to_inches(stem.cutter))
            if stem.cutter is not None
            else UNKNOWN_CUTTER
        )
        row: Dict[str, Any] = {
            "path": str(path),
            "plate": plate_number,
            "material": material,
            "cutter": cutter,
            "kind": stem.kind,
            "strokes": 0,
            "cutting": 0.0,
            "rapid": 0.0,
        }
        totals.files.append(row)
        try:
            document = parser.parse_file(path)
        except Exception as exc:  # noqa: BLE001 - metrics must never fail a job
            # A file the parser rejects is reported, not swallowed: the
            # operator sees the row with its ``error`` and an empty distance.
            row["error"] = f"{type(exc).__name__}: {exc}"
            continue
        file_totals = _DistanceTotals()
        row["strokes"] = _measure_document(document, frame, stem.kind, file_totals)
        row["cutting"] = _round(file_totals.cutting)
        row["rapid"] = _round(file_totals.rapid)
        totals.merge(file_totals)
        cutter_bucket = totals.by_cutter.setdefault((cutter, stem.kind), [0.0, 0.0, 0.0])
        cutter_bucket[0] += file_totals.cutting
        cutter_bucket[1] += file_totals.rapid
        cutter_bucket[2] += 1
        plate_bucket = totals.by_plate.setdefault(
            plate_number if plate_number is not None else UNRESOLVED_PLATE, [0.0, 0.0]
        )
        plate_bucket[0] += file_totals.cutting
        plate_bucket[1] += file_totals.rapid
        material_bucket = totals.by_material.setdefault(material, [0.0, 0.0])
        material_bucket[0] += file_totals.cutting
        material_bucket[1] += file_totals.rapid
    return _distance_rows(totals, frames, label_meta)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def build_usage_stats(
    *,
    resolved_labels: Sequence[ResolvedLabel],
    packed_plates: Sequence[Any],
    combined_by_plate: Mapping[int, str],
    plt_files: Sequence[Path],
    material_by_plate: Mapping[int, Optional[str]],
) -> Dict[str, Any]:
    """Aggregate the job's production metrics into the report's ``usage`` block.

    Args:
        resolved_labels: The job's resolved labels, in content order.
        packed_plates: The packed plates from the layout stage, in packing
            order (the same objects the export reports).
        combined_by_plate: The export's in-memory combined per-plate content,
            keyed by 1-based plate number. The per-label section split and the
            border/hole split are read from it.
        plt_files: The written per-cutter PLT paths, in write order. The tool
            travel is measured from these files.
        material_by_plate: Stock material name per 1-based plate number.

    Returns:
        The ``usage`` mapping: ``labels``, ``characters``, ``holes``,
        ``material`` and ``distance`` blocks, JSON-ready.
    """
    frames: Dict[int, _PlateFrame] = {}
    for number, plate in enumerate(packed_plates, start=1):
        combined = combined_by_plate.get(number)
        frame = _PlateFrame(
            plate_number=number,
            material=material_by_plate.get(number, getattr(plate, "material", None)),
            slots=_slots_for(list(getattr(plate, "labels", ()) or ())),
        )
        if combined:
            frame.stroke_class = _structural_classes(combined)
        frames[number] = frame
    return {
        "labels": _labels_usage(resolved_labels),
        "characters": _characters_usage(resolved_labels),
        "holes": _holes_usage(resolved_labels),
        "material": _material_usage(packed_plates, material_by_plate, resolved_labels),
        "distance": _distance_usage(
            plt_files=plt_files,
            frames=frames,
            material_by_plate=material_by_plate,
            label_meta=_label_meta(packed_plates, material_by_plate),
        ),
    }


# ==============================================================================
# summary.txt rendering
# ==============================================================================


def _fmt3(value: Any) -> str:
    """Render a measurement with three decimals.

    Args:
        value: The number, or ``None``.

    Returns:
        The value as ``0.000``, or :data:`MISSING_TEXT` when absent.
    """
    if value is None:
        return MISSING_TEXT
    return f"{float(value):.3f}"


def _fmt_cutter(value: Any) -> str:
    """Render a cutter bucket key for ``summary.txt``.

    Args:
        value: The cutter diameter, ``None`` (no cutter configured), or
            :data:`UNKNOWN_CUTTER` for a file whose cutter is not in the job.

    Returns:
        The display tag.
    """
    if value is None:
        return "none"
    if isinstance(value, str):
        return value
    return _fmt3(value)


def _rows(usage: Mapping[str, Any], section: str, key: str) -> List[Dict[str, Any]]:
    """Return one bucket list of a usage section.

    Args:
        usage: The ``usage`` block.
        section: The section name (``labels``, ``characters``, ...).
        key: The bucket list's key (``by_material``, ``by_class``, ...).

    Returns:
        The rows, empty when the section is absent.
    """
    rows: List[Dict[str, Any]] = list(usage.get(section, {}).get(key, ()) or ())
    return rows


def _join(parts: Sequence[str]) -> str:
    """Join rendered bucket entries for one line.

    Args:
        parts: The rendered entries.

    Returns:
        The parts joined by two spaces, or :data:`MISSING_TEXT` when empty.
    """
    return "  ".join(parts) if parts else MISSING_TEXT


def _plural(count: int, singular: str, plural: Optional[str] = None) -> str:
    """Render a count's noun with English pluralisation.

    Args:
        count: The number of things.
        singular: The singular noun.
        plural: The irregular plural, when the noun is not ``noun + "s"``.

    Returns:
        The noun in the number matching ``count``.
    """
    if count == 1:
        return singular
    return plural if plural is not None else f"{singular}s"


def format_usage_lines(usage: Mapping[str, Any]) -> List[str]:
    """Render the ``usage`` block as ``summary.txt`` lines.

    Every line is indented a multiple of two spaces so the section stays
    valid inside the ``=`` fenced blocks :func:`
    ~plt_optimizer.generate.generation_report.format_summary` emits. The
    per-label distance rows are capped at
    :data:`SUMMARY_USAGE_LABEL_LIMIT` with a pointer to ``report.json``,
    which carries the complete record.

    Args:
        usage: The ``usage`` block of a generation report (the JSON-shaped
            mapping built by :func:`build_usage_stats`).

    Returns:
        The lines of the "Job usage:" section, in report order.
    """
    lines: List[str] = ["Job usage:"]
    lines.extend(_labels_lines(usage))
    lines.extend(_characters_lines(usage))
    lines.extend(_holes_lines(usage))
    lines.extend(_material_lines(usage))
    lines.extend(_distance_lines(usage))
    return lines


def _counts(rows: Sequence[Dict[str, Any]], key: str) -> str:
    """Render ``name=count`` pairs from a breakdown list.

    Args:
        rows: The bucket rows.
        key: The row's bucket-name field.

    Returns:
        The pairs joined by two spaces, or :data:`MISSING_TEXT` when empty.
    """
    return _join([f"{row.get(key)}={row.get('count')}" for row in rows])


def _labels_lines(usage: Mapping[str, Any]) -> List[str]:
    """Render the ``usage.labels`` block.

    Args:
        usage: The ``usage`` block.

    Returns:
        The section's lines.
    """
    labels = usage.get("labels", {})
    lines = [
        f"  labels: {labels.get('instances', 0)} "
        f"{_plural(int(labels.get('instances', 0)), 'label')} "
        f"({labels.get('specs', 0)} {_plural(int(labels.get('specs', 0)), 'spec')})  "
        + _counts(_rows(usage, "labels", "by_shape"), "shape")
        + "  "
        + _counts(_rows(usage, "labels", "by_relief"), "engraving")
    ]
    lines.append(
        "    by material: " + _counts(_rows(usage, "labels", "by_material"), "material_label")
    )
    lines.append(
        "    by cutter: "
        + _join(
            [
                f"{_fmt_cutter(row.get('cutter_diameter'))} x{row.get('count')}"
                for row in _rows(usage, "labels", "by_cutter")
            ]
        )
    )
    lines.append(
        "    by size: "
        + _join(
            [
                f"{_fmt3(row.get('width'))}x{_fmt3(row.get('height'))} {row.get('shape')} "
                f"{_material_label(row.get('material'))} x{row.get('count')}"
                for row in _rows(usage, "labels", "by_size")
            ]
        )
    )
    return lines


def _height_band_label(row: Mapping[str, Any]) -> str:
    """Render a text-height band as ``nominal (engraved)``.

    The parenthetical is the cutter-diameter-adjusted (compressed/downsized)
    height the machine actually cuts, and it is dropped when it matches the
    nominal size - an unadjusted line reads ``0.25``, an adjusted one
    ``0.25 (0.205)``.

    Args:
        row: A ``by_text_height`` row.

    Returns:
        The display text (without the count).
    """
    engraved = _fmt3(row.get("text_height"))
    nominal = row.get("nominal_text_height")
    if nominal is None:
        return engraved
    nominal_text = _fmt3(nominal)
    if nominal_text == engraved:
        return nominal_text
    return f"{nominal_text} ({engraved})"


def _characters_lines(usage: Mapping[str, Any]) -> List[str]:
    """Render the ``usage.characters`` block.

    Args:
        usage: The ``usage`` block.

    Returns:
        The section's lines.
    """
    characters = usage.get("characters", {})
    lines = [
        f"  characters: {characters.get('total', 0)} engraved "
        f"{_plural(int(characters.get('total', 0)), 'glyph', 'glyphs')} "
        f"({characters.get('spaces_excluded', 0)} space(s) excluded; "
        f"{characters.get('markers', 0)} list-marker "
        f"{_plural(int(characters.get('markers', 0)), 'glyph', 'glyphs')} counted separately)"
    ]
    lines.append(
        "    by text height: "
        + _join(
            [
                f"{_height_band_label(row)} x{row.get('count')}"
                for row in _rows(usage, "characters", "by_text_height")
            ]
        )
    )
    return lines


def _holes_lines(usage: Mapping[str, Any]) -> List[str]:
    """Render the ``usage.holes`` block.

    Args:
        usage: The ``usage`` block.

    Returns:
        The section's lines.
    """
    holes = usage.get("holes", {})
    lines = [f"  drill holes: {holes.get('total', 0)}"]
    lines.append(
        "    by diameter: "
        + _join(
            [
                f"{_fmt3(row.get('diameter'))} (engraved {_fmt3(row.get('engraved_diameter'))}) "
                f"x{row.get('count')}"
                for row in _rows(usage, "holes", "by_diameter")
            ]
        )
    )
    return lines


def _material_lines(usage: Mapping[str, Any]) -> List[str]:
    """Render the ``usage.material`` block.

    Args:
        usage: The ``usage`` block.

    Returns:
        The section's lines.
    """
    material = usage.get("material", {})
    total = material.get("total", {})
    plates = int(total.get("plates", 0))
    lines = [
        f"  material: {plates} {_plural(plates, 'plate')}  "
        f"{_fmt3(total.get('sq_in'))} sq in ({_fmt3(total.get('sq_ft'))} sq ft)"
    ]
    for row in _rows(usage, "material", "by_material"):
        row_plates = int(row.get("plates", 0))
        lines.append(
            f"    {row.get('material_label')}: {row_plates} {_plural(row_plates, 'plate')}  "
            f"{_fmt3(row.get('sq_in'))} sq in ({_fmt3(row.get('sq_ft'))} sq ft)  "
            f"{row.get('label_instances')} label(s)  "
            f"boxes {_fmt3(row.get('label_bbox_sq_in'))} sq in"
        )
    nominal = material.get("label_nominal_area", {})
    bbox = material.get("label_bbox_area", {})
    lines.append(
        f"    label nominal area: {_fmt3(nominal.get('sq_in'))} sq in "
        f"({_fmt3(nominal.get('sq_ft'))} sq ft)"
    )
    lines.append(
        f"    label bounding-box area: {_fmt3(bbox.get('sq_in'))} sq in "
        f"({_fmt3(bbox.get('sq_ft'))} sq ft)"
    )
    return lines


def _distance_lines(usage: Mapping[str, Any]) -> List[str]:
    """Render the ``usage.distance`` block.

    Args:
        usage: The ``usage`` block.

    Returns:
        The section's lines.
    """
    distance = usage.get("distance", {})
    lines = ["  tool distance (inches, measured from the emitted PLT files):"]
    files = _rows(usage, "distance", "files")
    if not files:
        lines.append("    Not measured: the job produced no PLT files to read back.")
        return lines
    total = distance.get("total", {})
    lines.append(
        f"    total: cutting {_fmt3(total.get('cutting'))} + rapid "
        f"{_fmt3(total.get('rapid'))} = {_fmt3(total.get('total'))} "
        f"({_fmt3(total.get('total_ft'))} ft)"
    )
    by_class = distance.get("by_class", {})
    lines.append(
        "    by class: "
        + _join([f"{name} {_fmt3(by_class.get(name))}" for name in DISTANCE_CLASSES])
    )
    lines.append(
        "    by cutter: "
        + _join(
            [
                f"{_fmt_cutter(row.get('cutter'))} {row.get('kind')} "
                f"[{row.get('files')} file(s)] cut {_fmt3(row.get('cutting'))} "
                f"rapid {_fmt3(row.get('rapid'))}"
                for row in _rows(usage, "distance", "by_cutter")
            ]
        )
    )
    lines.append(
        "    by material: "
        + _join(
            [
                f"{row.get('material_label')} cut {_fmt3(row.get('cutting'))} "
                f"rapid {_fmt3(row.get('rapid'))}"
                for row in _rows(usage, "distance", "by_material")
            ]
        )
    )
    lines.append(
        "    by plate: "
        + _join(
            [
                f"{row.get('plate')} cut {_fmt3(row.get('cutting'))} "
                f"rapid {_fmt3(row.get('rapid'))}"
                for row in _rows(usage, "distance", "by_plate")
            ]
        )
    )
    per_label = _rows(usage, "distance", "by_label")
    lines.append("    per label:")
    for row in per_label[:SUMMARY_USAGE_LABEL_LIMIT]:
        row_classes = row.get("by_class", {})
        parts = [
            f"      {row.get('label_id')}"
            + ("" if row.get("spec_id") == row.get("label_id") else f" ({row.get('spec_id')})")
            + f"  plate {row.get('plate')}  {_material_label(row.get('material'))}"
            + f"  cut {_fmt3(row.get('cutting'))}  rapid {_fmt3(row.get('rapid'))}"
        ]
        parts.extend(
            f"  {name} {_fmt3(row_classes.get(name))}"
            for name in DISTANCE_CLASSES
            if float(row_classes.get(name, 0.0) or 0.0) > 0.0
        )
        lines.append("".join(parts))
    remaining = len(per_label) - SUMMARY_USAGE_LABEL_LIMIT
    if remaining > 0:
        lines.append(f"      ... and {remaining} more (see report.json)")
    unattributed = distance.get("unattributed", {})
    if int(unattributed.get("strokes", 0)) > 0:
        lines.append(
            f"    unattributed: {unattributed.get('strokes')} stroke(s)  "
            f"cut {_fmt3(unattributed.get('cutting'))}  rapid {_fmt3(unattributed.get('rapid'))}"
        )
    return lines
