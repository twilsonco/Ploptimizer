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
    >>> job = parse_yaml("examples/sample_spec.yaml")
    >>> labels = resolve_job_spec(job)
    >>> result = export_per_cutter_plts(labels, job.plates, output_dir="output")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from plt_optimizer.generate.label_renderer import RenderedLabel
from plt_optimizer.generate.layout import PackedPlate
from plt_optimizer.generate.resolution import (
    DEFAULT_BOUNDARY_HOLE_CUTTER,
    ResolvedLabel,
    build_cutter_pen_map,
)
from plt_optimizer.generate.schema import PlateSpec

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
                margin=0.5,
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
        resolved_labels, provided_plates, pen_map=pen_map
    )

    plt_dir = output_dir / "plt"
    plt_dir.mkdir(parents=True, exist_ok=True)

    result = PerCutterExport(output_dir=output_dir.resolve(), job_id=job_id)

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
            structure_path = plt_dir / f"{plate_str}_bh_{_format_cutter(hole_cutter)}_{job_id}.plt"
            structure_path.write_text(structure_content, encoding="utf-8")
            result.plt_paths.append(structure_path)

        # Text group: one file per distinct text cutter pen with content.
        for pen_id in sorted(cutter_by_pen):
            text_content = extract_pens_from_plt_text(combined, [pen_id])
            if not plt_has_geometry(text_content):
                continue
            cutter = cutter_by_pen[pen_id]
            text_path = plt_dir / f"{plate_str}_text_{_format_cutter(cutter)}_{job_id}.plt"
            text_path.write_text(text_content, encoding="utf-8")
            result.plt_paths.append(text_path)

    if optimize:
        # Run the PLT optimizer on each exported file
        result.plt_paths = _run_optimizer(result.plt_paths)

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
        plot_plt_document(document, output_path=pdf_path, show_plot=False, simple_mode=True)
        pdf_paths.append(pdf_path)

    for plate_no, combined in result.combined_by_plate.items():
        document = parser.parse_string(combined)
        pdf_path = pdf_dir / f"{_format_plate_number(plate_no)}_all_{job_id}.pdf"
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


def _run_optimizer(plt_paths: list[Path]) -> list[Path]:
    """Run the PLT optimizer on a list of PLT files.

    Uses the existing PLT parser, profiler, chunker, optimizer, reassembler,
    and writer to deduplicate overlapping score lines and minimize tool-up
    travel distance.

    Args:
        plt_paths: List of paths to PLT files to optimize.

    Returns:
        A list of paths to the optimized PLT files (overwrites originals).
    """
    # Import here to avoid circular imports
    from plt_optimizer.core.chunker import Chunker, ChunkerConfig
    from plt_optimizer.core.optimizer import (
        NearestNeighbor2OptStrategy,
        OptimizerEngine,
    )
    from plt_optimizer.core.parser import PLTParser
    from plt_optimizer.core.profiler import Profiler
    from plt_optimizer.core.reassembler import Reassembler
    from plt_optimizer.core.writer import PLTWriter

    optimized_paths: list[Path] = []

    for plt_path in plt_paths:
        try:
            # Parse the exported PLT file
            parser = PLTParser()
            doc = parser.parse_file(plt_path)

            # Profile to determine document type
            profiler = Profiler()
            profile_result = profiler.profile(doc)

            # Chunk into MacroBlocks
            chunker = Chunker(config=ChunkerConfig(threshold_multiplier=2.0))
            blocks = chunker.chunk(
                doc.stroke_paths,
                profile_result.baseline_extent,
                is_structural=profile_result.is_structural,
            )

            if not blocks:
                # No blocks to optimize; keep the file as-is
                optimized_paths.append(plt_path)
                continue

            # Run the optimizer (use fast mode for generated files)
            strategy = NearestNeighbor2OptStrategy()
            optimizer = OptimizerEngine(strategy=strategy)
            optimization_result = optimizer.optimize(blocks)

            # Reassemble the optimized document
            reassembler = Reassembler()
            optimized_doc = reassembler.reassemble(doc, blocks, optimization_result)

            # Write the optimized result back
            writer = PLTWriter()
            writer.write_file(optimized_doc, plt_path)
            optimized_paths.append(plt_path)
        except Exception:
            # If optimization fails for any reason, keep the original file
            optimized_paths.append(plt_path)

    return optimized_paths
