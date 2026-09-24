"""Phase 3 assembly/export engine for the label generation pipeline.

This module bridges the gap between the virtual layout and the physical
machine: it assembles independently rendered labels (see
``label_renderer.render_label_to_plt``) onto packed plates, splits the
assembly into per-cutter PLT files, runs them through the PLT
optimization utility, and writes optional PDF previews.

Pen (layer) mapping of the assembled plate:
- Pen 1: Text (legacy default text pen)
- Pen 2: Boundaries (score/cut lines)
- Pen 3: Drill holes
- Pens 4+: Text lines grouped by cutter diameter (see
  ``resolution.build_cutter_pen_map``)

Example:
    >>> from plt_optimizer.generate.resolution import resolve_job_spec
    >>> from plt_optimizer.generate.schema import parse_yaml
    >>> from plt_optimizer.generate.vectorize import export_per_cutter_plts
    >>> job = parse_yaml("tests_deps/sample_spec.yaml")
    >>> labels = resolve_job_spec(job)
    >>> result = export_per_cutter_plts(labels, job.plates, output_dir="output")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from plt_optimizer.core.optimizer import OptimizationStrategy
from plt_optimizer.generate.label_renderer import RenderedLabel, extract_bounds_from_plt
from plt_optimizer.generate.layout import PackedPlate
from plt_optimizer.generate.plate_optimizer import (
    PlateOptimization,
    StrategyFactory,
    optimize_structural_layer,
    optimize_text_layer,
)
from plt_optimizer.generate.resolution import (
    DEFAULT_BOUNDARY_HOLE_CUTTER,
    ResolvedLabel,
    build_cutter_pen_map,
)
from plt_optimizer.generate.schema import (
    DEFAULT_LAYOUT_MODE,
    LayoutMode,
    PlateSpec,
)
from plt_optimizer.utils.logging import TextLogger

# ---------------------------------------------------------------------------
# Layer assignments (structural pens of the assembled plate)
# ---------------------------------------------------------------------------
LAYER_BOUNDARY: int = 2
LAYER_HOLES: int = 3


# ---------------------------------------------------------------------------
# Phase 3: PLT Assembly and Export (three-phase architecture)
# ---------------------------------------------------------------------------
def translate_plt_coordinates(plt_content: str, dx: float, dy: float) -> str:
    """Translate all coordinates in a PLT file by an offset.

    Parses HPGL PA/PU/PD commands and applies x/y offsets, converting
    from inches to plotter units (1 inch = 1000 units).

    Args:
        plt_content: Raw HPGL PLT text content.
        dx: X offset in inches.
        dy: Y offset in inches.

    Returns:
        Modified PLT content with translated coordinates (without header/footer).
    """
    # Convert offsets from inches to plotter units
    dx_units = int(round(dx * 1000.0))
    dy_units = int(round(dy * 1000.0))

    if dx_units == 0 and dy_units == 0:
        # No translation needed
        return plt_content

    import re

    def translate_coordinates(match: re.Match[str]) -> str:
        """Translate coordinates within a PA/PU/PD/AA command."""
        cmd = match.group(1)
        coords_str = match.group(2)
        parts = coords_str.split(",")

        try:
            translated_parts = []
            for i, part in enumerate(parts):
                val = int(part)
                # AA carries a trailing sweep angle that must not be shifted.
                if cmd == "AA" and i >= 2:
                    translated_parts.append(str(val))
                elif i % 2 == 0:  # x coordinate
                    translated_parts.append(str(val + dx_units))
                else:  # y coordinate
                    translated_parts.append(str(val + dy_units))
            return f"{cmd}{','.join(translated_parts)}"
        except (ValueError, IndexError):
            return match.group(0)

    coord_pattern = r"(PA|PU|PD|AA)([\d,\-]+)"
    return re.sub(coord_pattern, translate_coordinates, plt_content)


def rotate_plt_content_90cw(plt_content: str) -> str:
    """Rotate all coordinates in PLT content 90 degrees clockwise.

    Used when the bin packer places a label rotated (see
    ``layout.generate_layout``): the whole rendered content — text,
    boundary and drill-hole arcs alike — rotates rigidly as one unit.

    The transform is applied in device coordinates (+y downward, the
    convention of rendered label content) and maps each point to::

        (x, y) -> (y_max - y, x - x_min)

    where ``(x_min, y_min, x_max, y_max)`` are the content bounds. The
    rotated content therefore spans ``[0, height] x [0, width]`` with its
    minimum at the origin, so translating it by the packed ``(x, y)``
    lands it exactly inside the packer's swapped slot with non-negative
    coordinates.

    Arc (``AA``) centers are mapped like any point. The sweep angle is
    preserved verbatim: a pure rotation has positive determinant, so the
    circles' orientation is unchanged (unlike the Y-axis mirror, which
    negates it). Arc radii are implicit (pen position to center) and a
    rigid transform preserves that distance.

    Args:
        plt_content: Raw HPGL PLT text content (plotter units,
            1 inch = 1000 units).

    Returns:
        Rotated PLT content. Content without any coordinates is returned
        unchanged.
    """
    try:
        x_min, _y_min, _x_max, y_max = extract_bounds_from_plt(plt_content)
    except ValueError:
        # No coordinates at all: nothing to rotate.
        return plt_content

    y_max_units = int(round(y_max * 1000.0))
    x_min_units = int(round(x_min * 1000.0))

    def rotate_coordinates(match: re.Match[str]) -> str:
        """Rotate coordinates within one PA/PU/PD/AA command."""
        cmd = match.group(1)
        parts = match.group(2).split(",")

        try:
            values = [int(part) for part in parts]
        except ValueError:
            return match.group(0)

        if cmd == "AA":
            if len(values) < 3:
                return match.group(0)
            cx, cy, angle = values[0], values[1], values[2]
            return f"AA{y_max_units - cy},{cx - x_min_units},{angle}"

        if len(values) % 2 != 0:
            # Malformed coordinate list (pairs expected): leave untouched.
            return match.group(0)

        rotated_parts: list[str] = []
        for i in range(0, len(values), 2):
            x, y = values[i], values[i + 1]
            rotated_parts.append(str(y_max_units - y))
            rotated_parts.append(str(x - x_min_units))
        return f"{cmd}{','.join(rotated_parts)}"

    return re.sub(r"(PA|PU|PD|AA)([\d,\-]+)", rotate_coordinates, plt_content)


def assemble_plt_from_rendered_labels(
    plate: PackedPlate,
    rendered_labels_map: dict[str, RenderedLabel],
) -> str:
    """Assemble a complete PLT for a plate from rendered labels.

    Takes all labels on a packed plate and combines their rendered PLT
    content, applying coordinate offsets to position each label at its
    final location on the plate.

    Args:
        plate: A packed plate containing positioned labels.
        rendered_labels_map: Cache of rendered labels by label ID.

    Returns:
        Complete HPGL/PLT content for the plate (with header and footer).
    """
    import re

    # Start with HPGL header
    plt_lines = ["IN;DF;PS0;"]

    # Process each label on the plate
    for packed_label in plate.labels:
        label_id = packed_label.source_label.id
        rendered = rendered_labels_map[label_id]

        # Get the rendered PLT content (strip header/footer)
        plt_content = rendered.plt_content
        # Remove ONLY the initial header, not the SP command that follows it
        plt_content = re.sub(r"^IN;DF;PS0;", "", plt_content)
        # Remove final footer (pen off and end commands)
        # Handle both: "SP0;IN;%" (new format) and "PU...;SP0;IN;%" (old format)
        plt_content = re.sub(r"(?:PU[^;]*;)?SP0;IN;%?$", "", plt_content)
        plt_content = plt_content.strip()
        if plt_content.endswith(";"):
            plt_content = plt_content[:-1]

        # Rotate first if the packer placed this label sideways: the whole
        # content (text, border, holes) turns 90 degrees clockwise and is
        # normalized to the origin, so the slot translation below lands it
        # inside the packer's swapped [x, x+H] x [y, y+W] slot.
        if packed_label.rotated:
            plt_content = rotate_plt_content_90cw(plt_content)

        # Translate coordinates to position on plate
        translated = translate_plt_coordinates(plt_content, packed_label.x, packed_label.y)

        # Remove any leading PU commands to avoid duplication
        # Assembly will add a single PU0,0; before each label
        translated = re.sub(r"^(?:PU[^;]*;)+", "", translated)

        # Add to assembly with pen-up command between labels
        if translated:
            plt_lines.append("PU0,0;")  # Pen up to safe position
            plt_lines.append(translated)
            plt_lines.append(";")

    # Add footer
    plt_lines.append("SP0;IN;%")

    return "".join(plt_lines)


def extract_pens_from_plt_text(plt_content: str, pen_ids: Sequence[int]) -> str:
    """Extract one or more pens (layers) from PLT text content.

    Parses HPGL PLT text and filters commands to only include those
    issued while one of the requested pen IDs was selected. Arc (``AA``)
    commands are preserved, so drill-hole layers (pen 3) survive
    extraction.

    Args:
        plt_content: Raw HPGL PLT text content.
        pen_ids: The pen IDs to extract (e.g. ``[2, 3]`` for the
            borders+holes structural layer, or a single text pen).

    Returns:
        PLT text content containing only commands for the specified pens
        (with a fresh header/footer). The result may be empty of geometry
        when none of the pens appear in ``plt_content``; callers should
        check :func:`plt_has_geometry` before writing.
    """
    wanted = {int(pen_id) for pen_id in pen_ids}

    lines = []
    lines.append("IN;DF;PS0;")

    # Parse and filter commands
    in_target_layer = False
    commands = plt_content.split(";")

    for cmd in commands:
        if not cmd.strip():
            continue

        # Check for pen select command
        if cmd.startswith("SP"):
            try:
                pen_id = int(cmd[2:])
                in_target_layer = pen_id in wanted
                if in_target_layer and pen_id > 0:
                    lines.append(f"SP{pen_id};")
            except (ValueError, IndexError):
                pass
        # Include drawing commands only if we're in target layer
        elif in_target_layer and cmd.strip() and not cmd.startswith("IN"):
            lines.append(f"{cmd};")

    lines.append("SP0;IN;%")
    return "".join(lines)


# Matches any drawable coordinate command (pen moves, pen-down strokes,
# absolute moves, or arcs). Header/footer tokens (IN/DF/PS/SP) never match.
_GEOMETRY_COMMAND_PATTERN = re.compile(r"(?:PU|PD|PA|AA)[\d,\-]+")


def plt_has_geometry(plt_content: str) -> bool:
    """Return True when PLT content contains at least one drawable command.

    Used as the empty-layer check when splitting assembled plates into
    per-cutter files: a layer with no PU/PD/PA/AA commands carries no
    toolpath and should not be written.

    Args:
        plt_content: Raw HPGL PLT text content.

    Returns:
        True when any PU/PD/PA/AA coordinate command is present.
    """
    return _GEOMETRY_COMMAND_PATTERN.search(plt_content) is not None


@dataclass
class PerCutterExport:
    """Result of a per-cutter PLT/PDF export.

    Attributes:
        plt_paths: Written (and optionally optimized) per-cutter PLT file
            paths. The combined per-plate PLT is intentionally NOT written
            to disk (it only exists in memory for plotting).
        pdf_paths: Written simple-outline PDF previews. One per written PLT
            plus one combined ``*_all_*.pdf`` per plate (when plotting is
            enabled).
        combined_by_plate: In-memory combined PLT content keyed by
            1-based plate number (all pens, unoptimized), for callers
            that want to render color/combined diagnostics plots.
        output_dir: Absolute base output directory (``plt/`` and ``pdf/``
            live inside it).
        job_id: Job identifier embedded in the written file names
            (``<plate number>_<kind>_<cutter>_<job_id>``), so callers can
            mirror the naming for any extra artifacts (e.g. color-coded
            combined plots).
        default_pdf_paths: Color-coded ``*_default.pdf`` diagnostic plots
            with rapid-travel visualization (written only when
            ``default_plots`` was requested).
    """

    plt_paths: list[Path] = field(default_factory=list)
    pdf_paths: list[Path] = field(default_factory=list)
    combined_by_plate: dict[int, str] = field(default_factory=dict)
    output_dir: Path = field(default_factory=Path)
    job_id: str = "job"
    default_pdf_paths: list[Path] = field(default_factory=list)


def _format_cutter(cutter_diameter: float) -> str:
    """Format a cutter diameter for file names (3-decimal inches).

    Args:
        cutter_diameter: Cutter diameter in inches.

    Returns:
        Fixed-precision string, e.g. ``0.03`` -> ``"0.030"``.
    """
    return f"{cutter_diameter:.3f}"


def _format_plate_number(plate_number: int) -> str:
    """Format a plate number for file names (2-digit zero-padded).

    Args:
        plate_number: 1-based plate index.

    Returns:
        Zero-padded string, e.g. ``1`` -> ``"01"``, ``12`` -> ``"12"``.
        Values beyond 99 simply widen (``100`` -> ``"100"``).
    """
    return f"{plate_number:02d}"


def export_per_cutter_plts(
    resolved_labels: list[ResolvedLabel],
    provided_plates: list[PlateSpec] | None = None,
    output_dir: str | Path = "output",
    job_id: str = "job",
    optimize: bool = True,
    plots: bool = True,
    default_plots: bool = False,
    allow_rotation: bool = True,
    fast_mode: bool = False,
    logger: Optional[TextLogger] = None,
    layout: LayoutMode = DEFAULT_LAYOUT_MODE,
) -> PerCutterExport:
    """Export plates as per-cutter PLT files (and optional simple PDFs).

    Implements the three-phase pipeline (render -> pack -> assemble) and
    then splits each assembled plate by CUTTER rather than by logical
    layer:

    - **borders + holes** share one file (same tool, engraved together),
      named ``<plate number>_bh_<cutter>_<job_id>.plt`` where ``<cutter>``
      is the boundary/hole cutter diameter (``bh`` = borders-holes).
    - **text** gets one file per distinct cutter diameter, named
      ``<plate number>_text_<cutter>_<job_id>.plt``. A text cutter equal
      to the boundary/hole cutter still gets its own file (separate run).
      The plate number is the 1-based packing-order index, zero-padded to
      two digits (``01``, ``02``, ...).
    - The combined per-plate PLT is assembled **in memory only** (never
      written) and exposed via :attr:`PerCutterExport.combined_by_plate`;
      when ``plots`` is enabled it also drives the combined
      ``<plate number>_all_<job_id>.pdf`` preview.

    PLT files are written under ``output_dir/plt/`` and PDFs under
    ``output_dir/pdf/``. Cutter diameters are formatted with 3 decimals
    (inches). Empty pen groups are skipped.

    Args:
        resolved_labels: List of resolved labels from Phase 2 resolution.
        provided_plates: Optional list of PlateSpec objects. If None, uses
            standard A3 paper (11" x 8.5").
        output_dir: Base output directory; ``plt/`` and ``pdf/``
            subdirectories are created inside it.
        job_id: Job identifier used as the file-name prefix (should be
            filesystem-safe; the CLI sanitizes the job name).
        optimize: If True, run the PLT optimizer on each written file.
        plots: If True, write simple-outline PDF previews for every
            written PLT plus a combined ``*_all_*`` PDF per plate.
        default_plots: If True, additionally write color-coded
            ``*_default.pdf`` diagnostic plots (rapid-travel view) for
            every written PLT and a combined ``*_all_*_default.pdf`` per
            plate. Opt-in only; independent of ``plots``.
        allow_rotation: If True (the default), the bin packer may rotate
            label instances 90 degrees for tighter layouts; rotated
            labels have their whole content (text, border, holes)
            rotated clockwise during assembly.
        fast_mode: When optimizing, route with
            :class:`~plt_optimizer.core.optimizer.NearestNeighbor2OptStrategy`
            exclusively instead of the default
            :class:`~plt_optimizer.core.optimizer.ParallelEnsembleStrategy`
            (mirrors the ``optimize`` CLI's ``--fast-mode``).
        logger: Optional text logger receiving per-layer optimization
            reports (method, baseline/optimized rapid travel).
        layout: Preferential plate fill order passed to the bin packer.
            ``columns`` (the default) fills each plate's height before
            extending rightward; ``rows`` fills width before extending
            downward (the historical behaviour). Individual plates may
            override it via ``PlateSpec.layout``.

    Returns:
        A :class:`PerCutterExport` with written PLT paths, PDF paths, and
        the in-memory combined content per plate.
    """
    from plt_optimizer.generate.layout import generate_layout_with_bounds

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Use default plates if not provided
    if provided_plates is None:
        provided_plates = [
            PlateSpec(
                id="default",
                width=11.0,
                height=8.5,
                clearance_padding=0.0,
            )
        ]

    # Pen == cutter: assign one HPGL pen per distinct text cutter so the
    # assembled plate can be split into per-cutter files after assembly.
    pen_map = build_cutter_pen_map(resolved_labels)
    cutter_by_pen = {pen: cutter for cutter, pen in pen_map.items()}

    # Job-level boundary/hole cutter (all labels share the job-resolved
    # value; fall back to the default for content-less jobs).
    hole_cutter = max(
        (label.hole_cutter_diameter for label in resolved_labels),
        default=DEFAULT_BOUNDARY_HOLE_CUTTER,
    )

    # Phase 2: Generate layout with rendered bounds (labels rendered onto
    # their cutter pens).
    packed_plates, rendered_labels_map = generate_layout_with_bounds(
        resolved_labels,
        provided_plates,
        pen_map=pen_map,
        allow_rotation=allow_rotation,
        layout=layout,
    )

    plt_dir = output_dir / "plt"
    plt_dir.mkdir(parents=True, exist_ok=True)

    result = PerCutterExport(output_dir=output_dir.resolve(), job_id=job_id)

    # Plate-space optimization strategy (only built when optimizing):
    # ParallelEnsemble by default, NN2Opt under fast_mode -- mirroring the
    # optimize CLI. The factory receives each layer's unoptimized rapid
    # travel so ensemble strategies can report improvement percentages.
    strategy_factory: Optional[StrategyFactory] = None
    if optimize:
        from plt_optimizer.core.optimizer import (
            NearestNeighbor2OptStrategy,
            ParallelEnsembleStrategy,
        )

        if fast_mode:

            def strategy_factory(baseline_distance: float) -> OptimizationStrategy:
                return NearestNeighbor2OptStrategy()

        else:

            def strategy_factory(baseline_distance: float) -> OptimizationStrategy:
                return ParallelEnsembleStrategy(baseline_distance=baseline_distance)

    def _report(layer: str, optimization: PlateOptimization) -> None:
        """Log one layer's optimization outcome (dual-logging topology)."""
        if logger is None:
            return
        outcome = optimization.outcome
        logger.info(
            f"Plate {layer}: optimized {optimization.node_count} node(s) via "
            f"{outcome.method_name} -- rapid travel "
            f"{optimization.baseline_distance:.3f} -> "
            f"{outcome.optimized_distance:.3f} plotter units"
        )

    # Phase 3: Assemble each plate in memory, then split by pen group.
    # Files are named <plate number>_<kind>_<cutter>_<job_id>, where the
    # plate number is the 1-based packing-order index (2-digit padded).
    for plate_no, plate in enumerate(packed_plates, start=1):
        plate_str = _format_plate_number(plate_no)
        combined = assemble_plt_from_rendered_labels(plate, rendered_labels_map)
        result.combined_by_plate[plate_no] = combined

        # Structural group: borders (SP2) + holes (SP3) share one run.
        structure_content = extract_pens_from_plt_text(combined, [LAYER_BOUNDARY, LAYER_HOLES])
        if plt_has_geometry(structure_content):
            bh_content = structure_content
            if optimize and strategy_factory is not None:
                optimization = optimize_structural_layer(
                    structure_content,
                    strategy_factory,
                    logger=logger,
                    log_prefix=f"[plate {plate_str} bh]",
                )
                if optimization is not None:
                    bh_content = optimization.content
                    _report(f"{plate_str} bh", optimization)
            structure_path = plt_dir / f"{plate_str}_bh_{_format_cutter(hole_cutter)}_{job_id}.plt"
            structure_path.write_text(bh_content, encoding="utf-8")
            result.plt_paths.append(structure_path)

        # Text group: one file per distinct text cutter pen with content.
        # With optimization enabled the layer is routed directly from the
        # rendered chunk records in plate space (parser/profiler skipped);
        # otherwise the pen layer is extracted from the assembly as-is.
        for pen_id in sorted(cutter_by_pen):
            cutter = cutter_by_pen[pen_id]
            written_content: Optional[str] = None
            if optimize and strategy_factory is not None:
                optimization = optimize_text_layer(
                    plate.labels,
                    rendered_labels_map,
                    pen_id,
                    strategy_factory,
                    logger=logger,
                    log_prefix=f"[plate {plate_str} text {_format_cutter(cutter)}]",
                )
                if optimization is not None:
                    written_content = optimization.content
                    _report(f"{plate_str} text {_format_cutter(cutter)}", optimization)
            if written_content is None:
                text_content = extract_pens_from_plt_text(combined, [pen_id])
                if not plt_has_geometry(text_content):
                    continue
                written_content = text_content
            text_path = plt_dir / f"{plate_str}_text_{_format_cutter(cutter)}_{job_id}.plt"
            text_path.write_text(written_content, encoding="utf-8")
            result.plt_paths.append(text_path)

    if plots:
        result.pdf_paths = _write_simple_plots(output_dir, job_id, result)
    if default_plots:
        result.default_pdf_paths = write_default_plots(output_dir, job_id, result)

    return result


def _write_simple_plots(
    output_dir: Path,
    job_id: str,
    result: PerCutterExport,
) -> list[Path]:
    """Write simple-outline PDF previews for a per-cutter export.

    Parses every written PLT and renders a simple-mode (black cutting
    lines only) PDF into ``output_dir/pdf/`` mirroring the PLT file names.
    Additionally renders one combined ``<plate number>_all_<job_id>.pdf``
    per plate from the in-memory combined content (text + borders + holes
    together).

    Stroke styling follows the toolpath kind: purely structural files
    (``bh`` = borders + holes) plot thick and semi-transparent, while text
    files and the mixed combined plots render thin and fully opaque (see
    :func:`~plt_optimizer.diagnostics.plotter.plot_plt_document`).

    matplotlib is imported lazily through the plotter module so headless
    optimizer-only environments never pay the import cost.

    Args:
        output_dir: Base output directory (``pdf/`` is created inside).
        job_id: Job identifier used in combined PDF names.
        result: The export result whose ``plt_paths`` and
            ``combined_by_plate`` drive the plots.

    Returns:
        List of written PDF paths.
    """
    from plt_optimizer.core.parser import PLTParser
    from plt_optimizer.diagnostics.plotter import plot_plt_document

    pdf_dir = output_dir / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    parser = PLTParser()
    pdf_paths: list[Path] = []

    for plt_path in result.plt_paths:
        document = parser.parse_file(plt_path)
        pdf_path = pdf_dir / f"{plt_path.stem}.pdf"
        # File-name shape: <plate number>_<kind>_<cutter>_<job_id>; the bh
        # (borders + holes) kind is purely structural, text is not.
        is_structural = plt_path.stem.split("_")[1:2] == ["bh"]
        plot_plt_document(
            document,
            output_path=pdf_path,
            show_plot=False,
            simple_mode=True,
            is_structural=is_structural,
        )
        pdf_paths.append(pdf_path)

    for plate_no, combined in result.combined_by_plate.items():
        document = parser.parse_string(combined)
        pdf_path = pdf_dir / f"{_format_plate_number(plate_no)}_all_{job_id}.pdf"
        # Combined plots mix text + borders + holes: thin opaque strokes.
        plot_plt_document(document, output_path=pdf_path, show_plot=False, simple_mode=True)
        pdf_paths.append(pdf_path)

    return pdf_paths


def write_default_plots(
    output_dir: Path,
    job_id: str,
    result: PerCutterExport,
) -> list[Path]:
    """Write color-coded ``*_default.pdf`` diagnostic plots for an export.

    Renders the plotter's default (color-coded, rapid-travel) view of
    every written per-cutter PLT as ``<plt-stem>_default.pdf`` plus one
    combined ``<plate number>_all_<job_id>_default.pdf`` per plate from
    the in-memory combined content (text + borders + holes together). These
    diagnostics are strictly opt-in; the simple-outline previews remain
    the standard output.

    matplotlib is imported lazily through the plotter module so headless
    optimizer-only environments never pay the import cost.

    Args:
        output_dir: Base output directory (``pdf/`` is created inside).
        job_id: Job identifier used in combined PDF names.
        result: The export result whose ``plt_paths`` and
            ``combined_by_plate`` drive the plots.

    Returns:
        List of written PDF paths.
    """
    from plt_optimizer.core.parser import PLTParser
    from plt_optimizer.diagnostics.plotter import plot_plt_document

    pdf_dir = output_dir / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    parser = PLTParser()
    pdf_paths: list[Path] = []

    for plt_path in result.plt_paths:
        document = parser.parse_file(plt_path)
        pdf_path = pdf_dir / f"{plt_path.stem}_default.pdf"
        plot_plt_document(document, output_path=pdf_path, show_plot=False, simple_mode=False)
        pdf_paths.append(pdf_path)

    for plate_no, combined in result.combined_by_plate.items():
        document = parser.parse_string(combined)
        pdf_path = pdf_dir / f"{_format_plate_number(plate_no)}_all_{job_id}_default.pdf"
        plot_plt_document(document, output_path=pdf_path, show_plot=False, simple_mode=False)
        pdf_paths.append(pdf_path)

    return pdf_paths
