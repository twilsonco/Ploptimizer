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
- Pens 4+: Text lines grouped by cutter diameter and stroke color (see
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

import logging
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, List, Mapping, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from plt_optimizer.generate.job_config import JobConfig
    from plt_optimizer.generate.jobset import JobProvenance
    from plt_optimizer.generate.schema import JobSpec

from plt_optimizer.core.filenames import (
    ensure_plot_filename_length,
    sanitize_filename_component,
)
from plt_optimizer.core.hpgl_lines import format_hpgl_lines
from plt_optimizer.core.models import ArcSegment, PLTDocument, Segment
from plt_optimizer.core.optimizer import OptimizationStrategy
from plt_optimizer.core.units import inches_to_units
from plt_optimizer.generate.label_renderer import (
    PLT_FOOTER,
    PLT_HEADER,
    RenderedLabel,
    extract_bounds_from_plt,
)
from plt_optimizer.generate.packing.layout import PackedPlate
from plt_optimizer.generate.plate_optimizer import (
    PlateOptimization,
    StrategyFactory,
    WidthKey,
    arc_width_key,
    build_stroke_width_map,
    line_width_key,
    optimize_structural_layer,
    optimize_text_layer,
    segment_width_key,
)
from plt_optimizer.generate.preview_colors import DEFAULT_PREVIEW_BACKGROUND_COLOR
from plt_optimizer.generate.resolution import (
    DEFAULT_BOUNDARY_HOLE_CUTTER,
    ResolvedLabel,
    build_cutter_pen_map,
)
from plt_optimizer.generate.schema import (
    DEFAULT_LAYOUT_MODE,
    LayoutMode,
    PlateSpec,
    TextColor,
    material_key,
)
from plt_optimizer.utils.logging import TextLogger
from plt_optimizer.utils.stage_timing import StageTimings, stage_timer

# Module logger for stage timing (named _LOG so the ``logger`` parameter of
# export_per_cutter_plts -- a TextLogger for operational reports -- never
# shadows it inside the function body).
_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Layer assignments (structural pens of the assembled plate)
# ---------------------------------------------------------------------------
LAYER_BOUNDARY: int = 2
LAYER_HOLES: int = 3


def _write_plt(path: Path, content: str) -> None:
    """Write per-cutter PLT content to disk in EngraveLab layout.

    The formatting boundary for the generate pipeline: the compact
    in-memory HPGL stream (one ``;``-joined line) is reflowed to one
    command per line -- a bare ``PD;``/``PU;`` glued to the arc it
    governs -- and written with CRLF terminators, matching the reference
    tool's output (see ``examples/ex_text.plt``). The transform is
    parse-identity guaranteed (see
    :func:`plt_optimizer.core.hpgl_lines.format_hpgl_lines`), so the
    engraved toolpath is byte-for-byte the same geometry.

    Args:
        path: Destination ``.plt`` file (parent must exist).
        content: Compact HPGL content (typically ending in ``SP;``).

    Raises:
        OSError: The file could not be written (disk full, a file the plotter
            controller still holds open, a read-only share). The failing path
            is named in the message - the watch service runs headless, so the
            path is the whole diagnosis - and re-raised for the CLI to record
            as a job failure; a plate that cannot be written must never be
            reported as produced.
    """
    try:
        with open(path, "w", encoding="utf-8", newline="\r\n") as handle:
            handle.write(format_hpgl_lines(content))
    except OSError as exc:
        raise OSError(f"Cannot write PLT file {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Phase 3: PLT Assembly and Export (three-phase architecture)
# ---------------------------------------------------------------------------
def translate_plt_coordinates(plt_content: str, dx: float, dy: float) -> str:
    """Translate all coordinates in a PLT file by an offset.

    Parses HPGL PA/PU/PD commands and applies x/y offsets, converting
    from inches to plotter units (``HPGL_UNITS_PER_INCH`` units = 1 inch).

    Args:
        plt_content: Raw HPGL PLT text content.
        dx: X offset in inches.
        dy: Y offset in inches.

    Returns:
        Modified PLT content with translated coordinates (without header/footer).
    """
    # Convert offsets from inches to plotter units
    dx_units = inches_to_units(dx)
    dy_units = inches_to_units(dy)

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
                val = int(float(part))
                # AA carries a trailing sweep angle that must not be shifted.
                # Keep its text verbatim (font glyph sweeps carry decimals).
                if cmd == "AA" and i >= 2:
                    translated_parts.append(part)
                elif i % 2 == 0:  # x coordinate
                    translated_parts.append(str(val + dx_units))
                else:  # y coordinate
                    translated_parts.append(str(val + dy_units))
            return f"{cmd}{','.join(translated_parts)}"
        except (ValueError, IndexError):
            return match.group(0)

    coord_pattern = r"(PA|PU|PD|AA)([\d,\.\-]+)"
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
            ``HPGL_UNITS_PER_INCH`` units = 1 inch).

    Returns:
        Rotated PLT content. Content without any coordinates is returned
        unchanged.
    """
    try:
        x_min, _y_min, _x_max, y_max = extract_bounds_from_plt(plt_content)
    except ValueError:
        # No coordinates at all: nothing to rotate.
        return plt_content

    y_max_units = inches_to_units(y_max)
    x_min_units = inches_to_units(x_min)

    def rotate_coordinates(match: re.Match[str]) -> str:
        """Rotate coordinates within one PA/PU/PD/AA command."""
        cmd = match.group(1)
        parts = match.group(2).split(",")

        try:
            values = [int(float(part)) for part in parts]
        except ValueError:
            return match.group(0)

        if cmd == "AA":
            if len(values) < 3:
                return match.group(0)
            cx, cy = values[0], values[1]
            # The sweep keeps its original text (font glyph sweeps carry
            # decimals); a rotation preserves it verbatim.
            angle_text = parts[2]
            return f"AA{y_max_units - cy},{cx - x_min_units},{angle_text}"

        if len(values) % 2 != 0:
            # Malformed coordinate list (pairs expected): leave untouched.
            return match.group(0)

        rotated_parts: list[str] = []
        for i in range(0, len(values), 2):
            x, y = values[i], values[i + 1]
            rotated_parts.append(str(y_max_units - y))
            rotated_parts.append(str(x - x_min_units))
        return f"{cmd}{','.join(rotated_parts)}"

    return re.sub(r"(PA|PU|PD|AA)([\d,\.\-]+)", rotate_coordinates, plt_content)


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

    # Start with HPGL header (tool options are inserted after IN; at write
    # time; the header terminates with PA; -- EngraveLab reference framing)
    plt_lines = [PLT_HEADER]

    # Process each label on the plate
    for packed_label in plate.labels:
        label_id = packed_label.source_label.id
        rendered = rendered_labels_map[label_id]

        # Get the rendered PLT content (strip header/footer)
        plt_content = rendered.plt_content
        # Remove ONLY the initial header, not the SP command that follows it
        plt_content = re.sub(r"^IN;PA;", "", plt_content)
        # Remove final footer (bare pen deselect)
        plt_content = re.sub(r"(?:PU[^;]*;)?SP;$", "", plt_content)
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
    plt_lines.append(PLT_FOOTER)

    return "".join(plt_lines)


def extract_pens_from_plt_text(plt_content: str, pen_ids: Sequence[int]) -> str:
    """Extract one or more pens (layers) from PLT text content.

    Parses HPGL PLT text and filters commands to only include those
    issued while one of the requested pen IDs was selected. Arc (``AA``)
    commands are preserved, so drill-hole layers (pen 3) survive
    extraction.

    The extracted content carries NO ``SP`` pen selects (final per-cutter
    files are single-tool streams, matching the EngraveLab reference
    framing). Because the ``SP`` resets are gone, a section that starts
    with a bare ``PD`` (the renderer's origin-skip optimization for closed
    boundary loops) is rewritten to ``PU{first};PD{rest}`` -- every contour
    in the output is pen-up-led, so concatenating layers can never splice a
    spurious cut between two contours (the rewrite is parser-exact: a bare
    ``PD``'s first pair is its pen-up position).

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

    lines = [PLT_HEADER]

    # Parse and filter commands
    in_target_layer = False
    # Whether the pen has a valid position in the extracted stream: after
    # entering a target layer (SP reset) the plotter position is unknown,
    # so the first command must be a move (PU), never a bare PD.
    pen_positioned = False
    commands = plt_content.split(";")

    for cmd in commands:
        if not cmd.strip():
            continue

        # Check for pen select command (filter only; never emitted)
        if cmd.startswith("SP"):
            try:
                pen_id = int(cmd[2:])
            except (ValueError, IndexError):
                continue
            in_target_layer = pen_id in wanted
            pen_positioned = False
        # Include drawing commands only if we're in target layer
        elif in_target_layer and cmd.strip() and not cmd.startswith("IN"):
            if not pen_positioned:
                cmd = _front_bare_pd_with_move(cmd)
            pen_positioned = True
            lines.append(f"{cmd};")

    lines.append(PLT_FOOTER)
    return "".join(lines)


def _front_bare_pd_with_move(command: str) -> str:
    """Rewrite a section-leading bare ``PD`` as an explicit pen-up move.

    ``PU{first};PD{rest}`` is exactly equivalent to the bare ``PD`` (its
    first coordinate pair is the path's pen-up position) and keeps every
    emitted contour pen-up-led. A single-pair ``PD`` becomes the bare move
    ``PU{p}`` (also parser-equivalent: a path with no pen-down segment is
    dropped anyway). Anything else (``PU``/``AA``/malformed) is returned
    verbatim.

    Args:
        command: The first drawable command of an extracted pen section.

    Returns:
        The PU-led rewrite, or ``command`` unchanged.
    """
    if not command.startswith("PD"):
        return command
    parts = command[2:].split(",")
    if len(parts) < 2:
        return command
    try:
        int(parts[0])
        int(parts[1])
    except ValueError:
        return command
    first = f"{parts[0]},{parts[1]}"
    rest = ",".join(parts[2:])
    return f"PU{first};PD{rest}" if rest else f"PU{first}"


# Matches any drawable coordinate command (pen moves, pen-down strokes,
# absolute moves, or arcs). Header/footer tokens (IN/PA/SP) never match.
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
            (``[<plate number>_][<material>_]<cutter>_<kind>_<job_id>``),
            so callers can mirror the naming for any extra artifacts
            (e.g. color-coded combined plots).
        default_pdf_paths: Color-coded ``*_default.pdf`` diagnostic plots
            with rapid-travel visualization (written only when
            ``default_plots`` was requested).
        rendered_labels: The render cache keyed by label ID (each
            :class:`~plt_optimizer.generate.label_renderer.RenderedLabel`
            carries measured bounds, collision flags, and the effective
            per-line ``compression_by_line`` scales). Reporting-only: lets
            callers inspect applied horizontal compression and collision
            state without re-rendering.
        material_by_plate: Stock material name per 1-based plate number
            (``None`` = material-agnostic), mirroring
            :attr:`combined_by_plate` keys. Reporting-only: labels are
            partitioned by material at packing time, so each plate carries
            at most one material (see
            :attr:`~plt_optimizer.generate.packing.layout.PackedPlate.material`).
        packed_plates: The packed plates from the layout stage, in packing
            order. Reporting-only: each plate carries its usable size, its
            material and the placement (position, size, rotation) of every
            label on it, so a report can name where each label was cut.
        plate_jobs: Jobset only: 1-based plate number -> the owning job keys
            of the labels packed on it, in jobset-file order. Empty for a
            single-job run. Drives the per-job overlay boxes on the combined
            plate previews and the ``jobset.json`` plate manifest.
        file_jobs: Jobset only: written PLT path -> the owning job keys whose
            strokes that file carries, in jobset-file order. A plate layer is
            one file per (cutter, color, header block) shared by every job
            engraving with that tool, so a merged file names all of its jobs
            and a per-job report must measure itself out of the shared file
            stroke by stroke. Empty for a single-job run.
        combined_by_plate_and_job: Jobset only: 1-based plate number ->
            ``job_key ->`` that job's own combined per-plate content (its
            labels only, at the shared plate's placements). Drives the per-job
            ``all_*`` proof PDFs a batch writes beside the whole-sheet proof,
            so each job can be approved on its own. Empty for a single-job run
            (whose whole-sheet proof already IS that job).
    """

    plt_paths: list[Path] = field(default_factory=list)
    pdf_paths: list[Path] = field(default_factory=list)
    combined_by_plate: dict[int, str] = field(default_factory=dict)
    output_dir: Path = field(default_factory=Path)
    job_id: str = "job"
    default_pdf_paths: list[Path] = field(default_factory=list)
    rendered_labels: dict[str, RenderedLabel] = field(default_factory=dict)
    material_by_plate: dict[int, Optional[str]] = field(default_factory=dict)
    packed_plates: list[PackedPlate] = field(default_factory=list)
    plate_jobs: dict[int, tuple[str, ...]] = field(default_factory=dict)
    file_jobs: dict[Path, tuple[str, ...]] = field(default_factory=dict)
    combined_by_plate_and_job: dict[int, dict[str, str]] = field(default_factory=dict)


def _format_cutter(cutter_diameter: float) -> str:
    """Format a cutter diameter for file names (integer thousandths of an inch).

    The plotter-controller filename cap (see
    :func:`plt_optimizer.core.filenames.ensure_plot_filename_length`) makes
    every character count, so the diameter is emitted in mils as a plain
    integer -- no leading zeros, no padding, no decimal point
    (``0.03`` -> ``"30"``, ``0.187`` -> ``"187"``, ``0.005`` -> ``"5"``).

    Args:
        cutter_diameter: Cutter diameter in inches.

    Returns:
        Integer-mils string, e.g. ``0.03`` -> ``"30"``.
    """
    return str(int(round(cutter_diameter * 1000)))


def _format_text_layer(cutter_diameter: float, text_color: str) -> str:
    """Format a ``(cutter, color)`` text layer for file names.

    The implicit ``"none"`` color keeps the historical cutter-only name
    (bit-identical output for color-less jobs); a real stroke color gains
    its single-letter tag so color-split layers never collide
    (``0.04`` + ``magenta`` -> ``"40_m"``).

    Args:
        cutter_diameter: Cutter diameter in inches.
        text_color: Resolved stroke-color value (``"none"`` or a
            :class:`~plt_optimizer.generate.schema.TextColor` value).

    Returns:
        File-name component, e.g. ``"30"`` or ``"30_m"``.
    """
    cutter_str = _format_cutter(cutter_diameter)
    if text_color == TextColor.NONE.value:
        return cutter_str
    try:
        abbreviation = TextColor(text_color).abbreviation
    except ValueError:
        # Unknown colors (manually constructed labels) fall back to the
        # sanitized value so file names stay filesystem-safe and can never
        # collide with a real color's 1-letter abbreviation. Underscores are
        # removed so the tag stays a single token (see _parse_plt_stem).
        abbreviation = sanitize_filename_component(text_color, fallback="x").replace("_", "")
    return f"{cutter_str}_{abbreviation}"


def _format_plate_number(plate_number: int) -> str:
    """Format a plate number for file names (2-digit zero-padded).

    Args:
        plate_number: 1-based plate index.

    Returns:
        Zero-padded string, e.g. ``1`` -> ``"01"``, ``12`` -> ``"12"``.
        Values beyond 99 simply widen (``100`` -> ``"100"``).
    """
    return f"{plate_number:02d}"


def _format_material_tag(material: Optional[str]) -> str:
    """Format a stock material name for inclusion in file names.

    Material names are free-form (``"wk"``, ``"wk(uv)"``), so the name is
    reduced to a single safe filename component (see
    :func:`plt_optimizer.core.filenames.sanitize_filename_component`):
    international characters transliterate and every run of punctuation
    collapses to one underscore (``"wk(uv)"`` -> ``"wk_uv"``,
    ``"3-layer black"`` -> ``"3_layer_black"``). A name that sanitizes to
    nothing (e.g. ``"---"``) falls back to ``"x"`` so the tag is never empty.

    Args:
        material: The plate's resolved material name, or ``None``
            (material-agnostic).

    Returns:
        File-name component, or ``""`` when ``material`` is ``None``.
    """
    if material is None:
        return ""
    return sanitize_filename_component(material, fallback="x")


def _material_plate_counts(materials: Mapping[int, Optional[str]]) -> dict[int, int]:
    """Count how many plates share each plate's material group.

    The plate number in file names disambiguates sheets *within* a
    material group (the material tag already separates groups), so a
    plate only needs its number when its group spans multiple plates.
    Material-agnostic plates (``None``) form one group, reproducing the
    historical all-plates-count rule.

    Args:
        materials: Plate number to resolved material name mapping (see
            :attr:`PerCutterExport.material_by_plate`).

    Returns:
        Mapping of plate number to the size of its material group
        (>= 1 for every key of ``materials``).
    """
    group_keys = {plate_no: material_key(material) for plate_no, material in materials.items()}
    totals = Counter(group_keys.values())
    return {plate_no: totals[key] for plate_no, key in group_keys.items()}


def _format_plate_prefix(
    plate_number: int,
    material_plate_count: int,
    material: Optional[str] = None,
) -> str:
    """Build the leading ``[<plate>_][<material>_]`` file-name prefix.

    The plate number is omitted when the plate's material group spans a
    single sheet, so its presence doubles as the signal that the material
    needs more than one plate. The material tag follows (see
    :func:`_format_material_tag`) and is omitted for material-agnostic
    plates, whose whole job forms one group (so multi-plate
    material-agnostic jobs keep their plate numbers).

    Args:
        plate_number: 1-based plate index.
        material_plate_count: Number of plates sharing this plate's
            material group.
        material: The plate's resolved material name, or ``None``.

    Returns:
        Prefix ending in an underscore, e.g. ``""``, ``"02_"``,
        ``"wk_uv_"`` or ``"02_wk_uv_"``.
    """
    parts: list[str] = []
    if material_plate_count > 1:
        parts.append(_format_plate_number(plate_number))
    material_tag = _format_material_tag(material)
    if material_tag:
        parts.append(material_tag)
    return "_".join(parts) + "_" if parts else ""


# Layer-kind tokens appearing in per-cutter file names.
_KIND_TOKENS: tuple[str, ...] = ("txt", "bh")

# Cutter file-name token: integer thousandths of an inch (see _format_cutter).
_CUTTER_TOKEN_RE = re.compile(r"^\d+$")


@dataclass(frozen=True)
class _StemParts:
    """Parsed components of a per-cutter PLT file-name stem.

    Attributes:
        plate: Plate-number token (``"02"``), or ``None`` when the plate's
            material spans a single sheet.
        material: Material tag token (``"wk_uv"``), or ``None``.
        cutter: Cutter-diameter token in integer mils (``"40"``), or
            ``None``.
        kind: Layer kind (``"txt"`` / ``"bh"``), or ``None`` when the stem
            does not follow the naming scheme.
        color: Stroke-color tag (``"m"``), or ``None``.
    """

    plate: Optional[str] = None
    material: Optional[str] = None
    cutter: Optional[str] = None
    kind: Optional[str] = None
    color: Optional[str] = None


def _parse_plt_stem(stem: str) -> _StemParts:
    """Parse a per-cutter PLT file-name stem into its components.

    The naming scheme is
    ``[<plate>_][<material>_]<cutter>[_<color>]_<kind>_<job_id>`` where
    ``<cutter>`` is an integer-mils token (see :func:`_format_cutter`).
    Because the cutter token is now plain digits -- exactly like the
    zero-padded plate number -- parsing anchors on the layer-kind token
    (``txt`` / ``bh``) and reads the cutter leftward from it:

    - the token before the kind is all digits -> that is the cutter and
      there is no color tag;
    - otherwise the token two before the kind is all digits -> that is the
      cutter and the token between is the color tag;
    - otherwise the stem carries no parsable cutter (kind only).

    Then, relative to the cutter index: index 0 -> no plate, no material;
    index >= 1 -> the leading token is the plate when it is all digits, and
    every token between (it and) the cutter is the material (a sanitized
    material tag may itself contain underscores, e.g. ``wk(uv)`` ->
    ``wk_uv``).

    Known limitations (all degrade gracefully to the thin-line plot /
    fallback title): a stem shortened by the 39-character filename cap can
    lose trailing cutter digits (``187`` -> ``18``), an unknown numeric
    color tag (``40_123_txt_job``) reads as the cutter, and a material whose
    sanitized tag is all digits (material ``"02"``) reads as a plate number.
    ``job_id`` may itself contain underscores; the kind token is the anchor
    that avoids it.

    Args:
        stem: File-name stem (``Path(...).stem``).

    Returns:
        The parsed components; ``kind`` is ``None`` for stems that carry
        no known layer token.
    """
    tokens = stem.split("_")
    kind_index = next((index for index, token in enumerate(tokens) if token in _KIND_TOKENS), None)
    if kind_index is None:
        return _StemParts()
    kind = tokens[kind_index]
    if kind_index == 0:
        return _StemParts(kind=kind)
    cutter_index: Optional[int] = None
    color: Optional[str] = None
    if _CUTTER_TOKEN_RE.match(tokens[kind_index - 1]):
        cutter_index = kind_index - 1
    elif kind_index >= 2 and _CUTTER_TOKEN_RE.match(tokens[kind_index - 2]):
        cutter_index = kind_index - 2
        color = tokens[kind_index - 1]
    if cutter_index is None:
        return _StemParts(kind=kind)
    if cutter_index >= 1 and tokens[0].isdigit():
        plate: Optional[str] = tokens[0]
        material: Optional[str] = "_".join(tokens[1:cutter_index]) or None
    elif cutter_index >= 1:
        plate, material = None, "_".join(tokens[:cutter_index])
    else:
        plate, material = None, None
    return _StemParts(
        plate=plate,
        material=material,
        cutter=tokens[cutter_index],
        kind=kind,
        color=color,
    )


def _is_structural_stem(stem: str) -> bool:
    """Return whether a PLT stem names a purely structural (``bh``) layer.

    Args:
        stem: File-name stem (``Path(...).stem``).

    Returns:
        True for borders-and-holes files, False for text files and stems
        that do not follow the naming scheme.
    """
    return _parse_plt_stem(stem).kind == "bh"


def _cutter_token_to_inches(cutter_token: str) -> float:
    """Convert an integer-mils cutter token back to inches.

    The inverse of :func:`_format_cutter` (``"40"`` -> ``0.040``).

    Args:
        cutter_token: All-digit file-name token produced by
            :func:`_format_cutter`.

    Returns:
        Cutter diameter in inches.
    """
    return int(cutter_token) / 1000.0


def _build_plot_title(job_name: str, plt_path: Path, text_height: float | None = None) -> str:
    """Build a descriptive title for a plot from the job name and PLT file path.

    The file name format is:
    - Text: ``[<plate>_][<material>_]<cutter>[_<color>]_txt_<job_id>.plt``
    - Structural: ``[<plate>_][<material>_]<cutter>_bh_<job_id>.plt``

    Args:
        job_name: The human-readable job name.
        plt_path: Path to the PLT file, used to extract plot type, material
            and cutter size.
        text_height: Text height in inches, included for text plots (optional).

    Returns:
        A descriptive title string, e.g. ``"My Job 0.5 text (0.040 cutter)"``
        or ``"My Job borders and holes (0.015 cutter)"``. The material tag,
        when present, is included as ``" [wk_uv]"``. The cutter is rendered in
        inches for readability even though the file name carries integer mils.
    """
    parts = _parse_plt_stem(plt_path.stem)
    if parts.kind is None:
        # Fallback for unexpected format
        return f"{job_name} {plt_path.stem}"

    cutter_label = (
        f"{_cutter_token_to_inches(parts.cutter):.3f} cutter"
        if parts.cutter is not None
        else "no cutter"
    )
    material_suffix = f" [{parts.material}]" if parts.material else ""
    if parts.kind == "bh":
        return f"{job_name} borders and holes ({cutter_label}){material_suffix}"
    text_height_str = f"{text_height:.3g}" if text_height is not None else ""
    if text_height_str:
        return f"{job_name} {text_height_str} text ({cutter_label}){material_suffix}"
    return f"{job_name} text ({cutter_label}){material_suffix}"


def _plate_owners(plate: PackedPlate, provenance: Optional[JobProvenance]) -> tuple[str, ...]:
    """Jobs that one plate's per-cutter files are segmented into.

    Args:
        plate: The packed plate.
        provenance: Jobset side table, or ``None`` for a single-job run.

    Returns:
        ``("",)`` for a single-job run — the empty owner means "the whole
        plate", which is exactly the pre-jobset code path, so single-job
        output stays byte-identical. Otherwise the owning job keys of the
        labels on this plate, in jobset-file order (empty for a plate that
        carries no labels, which then writes no files).
    """
    if provenance is None:
        return ("",)
    return provenance.owners_of([packed.source_label.id for packed in plate.labels])


def _segment_plate(
    plate: PackedPlate, owner: str, provenance: Optional[JobProvenance]
) -> PackedPlate:
    """Return ``plate`` carrying only ``owner``'s labels.

    Args:
        plate: The packed plate.
        owner: Owning job key, or ``""`` for the whole plate.
        provenance: Jobset side table.

    Returns:
        The same plate object when ``owner`` is empty (no copy: assembly and
        routing then run exactly as a standalone job's), else a plate whose
        ``labels`` are the owner's, at the same placements.
    """
    if not owner or provenance is None:
        return plate
    return replace(
        plate,
        labels=[
            packed
            for packed in plate.labels
            if provenance.job_of_label(packed.source_label.id) == owner
        ],
    )


def _segment_tag(owner: str, plate_str: str, kind: str) -> str:
    """Name one plate layer for log and report prefixes.

    Args:
        owner: Owning job key, or ``""`` for a single-job run.
        plate_str: The plate's file-name prefix (``"01_wk_uv"``).
        kind: Layer kind (``"bh"``, ``"text <tag>"``).

    Returns:
        ``"01_wk_uv bh"`` for a single-job run (unchanged history), else
        ``"01_wk_uv bh[seer_2963]"`` naming the job whose toolpath it is.
    """
    return f"{plate_str} {kind}" if not owner else f"{plate_str} {kind}[{owner}]"


def _bucket_tag(owners: tuple[str, ...], plate_str: str, kind: str) -> str:
    """Name one written layer for log and report prefixes.

    Args:
        owners: The jobs whose strokes the layer carries (``("",)`` for a
            single-job run).
        plate_str: The plate's file-name prefix (``"01_wk_uv"``).
        kind: Layer kind (``"bh"``, ``"text <tag>"``).

    Returns:
        ``"01_wk_uv bh"`` when no job boundary splits the layer (a single-job
        run keeps its unchanged history), else the contributing jobs joined:
        ``"01_wk_uv bh[seer_2963]"`` for one job, ``"01_wk_uv bh[seer+coolsys]"``
        for the shared toolpath.
    """
    label = "+".join(owner for owner in owners if owner)
    return f"{plate_str} {kind}" if not label else f"{plate_str} {kind}[{label}]"


def _tool_option_signature(
    spec: Optional[JobSpec], job_config: Optional[JobConfig], plt_type: str
) -> Tuple[str, ...]:
    """The engraver-header block one member's layer files carry, as a merge key.

    Jobs may share a cutter, a material and a sheet and still be unable to
    engrave in one run: a PLT file carries a single ``tool_options`` header
    block after ``IN;`` (feed rate, spindle speed, Z clearance), so the header
    block a member generates IS the merge test. Members generating identical
    blocks share one file; members generating different blocks each keep their
    own file for that layer, and the writer warns that the tool is being
    re-run.

    Args:
        spec: The member's spec (``None`` = this run prepends no headers).
        job_config: The loaded config (the ``tool_options`` registry).
        plt_type: ``"text"`` or ``"borders_holes"``.

    Returns:
        The generated header lines, or ``()`` when no headers are prepended.
    """
    if spec is None or job_config is None:
        return ()
    from plt_optimizer.generate.tool_options import generate_tool_option_headers

    return tuple(generate_tool_option_headers(spec, job_config, plt_type))


@dataclass(frozen=True)
class _LayerBucket:
    """One written PLT file's share of a plate.

    Attributes:
        kind: ``"bh"`` (borders + holes, engraved in one run) or ``"txt"``
            (one cutter/color text pen).
        owners: The jobs whose strokes the file carries, in jobset-file order;
            ``("",)`` for a single-job run (the whole plate).
        cutter: The cutter every stroke in the bucket engraves with, which is
            the filename token — so a file never names a tool its strokes were
            not compensated for.
        width: That cutter's kerf-buffered engraved width (proof ink width).
        pen_id: The text layer's HPGL pen number; ``None`` for ``"bh"``.
        spec: The spec whose ``tool_options`` header block the file carries —
            byte-identical for every owner in the bucket, by construction.
    """

    kind: str
    owners: tuple[str, ...]
    cutter: float
    width: float
    pen_id: Optional[int] = None
    spec: Optional[JobSpec] = None

    @property
    def job_keys(self) -> tuple[str, ...]:
        """The contributing members, without the single-job empty owner."""
        return tuple(owner for owner in self.owners if owner)


def _has_pen(content: str, pen_ids: Sequence[int]) -> bool:
    """Report whether assembled plate content engraves on any of ``pen_ids``.

    A layer's file must name (and be attributed to) only the jobs whose strokes
    are inside it. Membership is read from the owner's own assembled plate
    content, so it is exactly what the writer emits - not an inference from the
    pen map, which is built pool-wide.

    Args:
        content: The owner's combined plate content (``""`` = nothing).
        pen_ids: The pens to test (``2``/``3`` = borders + holes).

    Returns:
        ``True`` when ``content`` carries geometry on one of those pens.
    """
    if not content:
        return False
    return plt_has_geometry(extract_pens_from_plt_text(content, list(pen_ids)))


def _bh_buckets(
    plate: PackedPlate,
    owners: tuple[str, ...],
    provenance: Optional[JobProvenance],
    *,
    hole_cutter: float,
    hole_cutter_width: float,
    job_spec: Optional[JobSpec],
    layer_specs: Optional[Mapping[str, JobSpec]],
    job_config: Optional[JobConfig],
    per_owner: Optional[Mapping[str, str]] = None,
) -> List[_LayerBucket]:
    """Group one plate's structural strokes into files by TOOL, not by job.

    Args:
        plate: The packed plate.
        owners: The jobs present on the plate (``("",)`` for a single-job run).
        provenance: Jobset side table.
        hole_cutter: Job-level boundary/hole cutter (kept for a single job).
        hole_cutter_width: Its kerf-buffered width.
        job_spec: The run-level spec (headers + pool-wide settings).
        layer_specs: ``job_key -> member spec``, from the jobset plan.
        job_config: The loaded config (the ``tool_options`` registry).
        per_owner: ``job_key ->`` that job's assembled plate content. Given for
            a mixed plate, it drops jobs that engrave no border/hole on this
            sheet, so a file's job list names only real contributors. Empty for
            a plate one job owns outright (its whole sheet is that job's work,
            so nothing is filtered - and a single-job run has no owner at all).

    Returns:
        One bucket per distinct (boundary/hole cutter, header block), in
        jobset-file order of their first contributing job. Members agreeing on
        both land in the same bucket, which is what makes a shared sheet one
        structural run per tool.
    """
    content_map = per_owner or {}
    grouped: dict[
        tuple[float, float, Tuple[str, ...]],
        tuple[float, float, Optional[JobSpec], List[str]],
    ] = {}
    for owner in owners:
        segment = _segment_plate(plate, owner, provenance)
        cutter, width = _segment_cutters(segment, owner, hole_cutter, hole_cutter_width)
        spec = _segment_spec(owner, job_spec, layer_specs)
        if (
            owner
            and content_map
            and not _has_pen(content_map[owner], (LAYER_BOUNDARY, LAYER_HOLES))
        ):
            continue
        key = (cutter, width, _tool_option_signature(spec, job_config, "borders_holes"))
        entry = grouped.get(key)
        if entry is None:
            grouped[key] = (cutter, width, spec, [owner])
        else:
            entry[3].append(owner)
    return [
        _LayerBucket(kind="bh", owners=tuple(bucket_owners), cutter=cutter, width=width, spec=spec)
        for cutter, width, spec, bucket_owners in grouped.values()
    ]


def _text_buckets(
    owners: tuple[str, ...],
    *,
    layer_by_pen: Mapping[int, tuple[float, str]],
    pen_widths: Mapping[int, float],
    job_spec: Optional[JobSpec],
    layer_specs: Optional[Mapping[str, JobSpec]],
    job_config: Optional[JobConfig],
    per_owner: Optional[Mapping[str, str]] = None,
) -> List[_LayerBucket]:
    """Group one plate's text strokes into files by (pen, header block).

    The pen map is built over every label of the run, so a ``(cutter, color)``
    layer is already shared by every job that uses that tool and ink; jobs
    whose text headers agree merge into that layer's single file (and are
    routed together). Pen order is preserved, so a single-job run writes
    exactly its historical file sequence.

    Args:
        owners: The jobs present on the plate (``("",)`` for a single-job run).
        layer_by_pen: ``pen -> (cutter, color)`` of the run's text layers.
        pen_widths: Engraved (buffered) width per pen, for the proofs.
        job_spec: The run-level spec (headers + pool-wide settings).
        layer_specs: ``job_key -> member spec``, from the jobset plan.
        job_config: The loaded config (the ``tool_options`` registry).
        per_owner: ``job_key ->`` that job's assembled plate content. Given for
            a mixed plate, it drops jobs that engrave nothing on this pen, so a
            file's job list (and its name) names only real contributors - a pen
            only one job uses keeps that job's own filename token.

    Returns:
        One bucket per (pen, header block), in ascending pen order.
    """
    content_map = per_owner or {}
    grouped: dict[tuple[int, Tuple[str, ...]], tuple[Optional[JobSpec], List[str]]] = {}
    for pen_id in sorted(layer_by_pen):
        for owner in owners:
            if owner and not _has_pen(content_map.get(owner, ""), (pen_id,)):
                continue
            spec = _segment_spec(owner, job_spec, layer_specs)
            key = (pen_id, _tool_option_signature(spec, job_config, "text"))
            entry = grouped.get(key)
            if entry is None:
                grouped[key] = (spec, [owner])
            else:
                entry[1].append(owner)
    buckets: List[_LayerBucket] = []
    for (pen_id, _signature), (spec, bucket_owners) in grouped.items():
        cutter, _color = layer_by_pen[pen_id]
        buckets.append(
            _LayerBucket(
                kind="txt",
                owners=tuple(bucket_owners),
                pen_id=pen_id,
                cutter=cutter,
                width=pen_widths.get(pen_id, cutter),
                spec=spec,
            )
        )
    return buckets


def _bucket_plate(
    plate: PackedPlate, bucket: _LayerBucket, provenance: Optional[JobProvenance]
) -> PackedPlate:
    """Return ``plate`` carrying exactly ``bucket``'s labels.

    Args:
        plate: The packed plate.
        bucket: The layer being written.
        provenance: Jobset side table.

    Returns:
        ``plate`` itself for a single-job bucket (no copy: the assembly then
        runs exactly as a standalone job's), else the plate whose ``labels``
        are the bucket's jobs', at the same placements and order.
    """
    if not bucket.job_keys or provenance is None:
        return plate
    wanted = set(bucket.job_keys)
    return replace(
        plate,
        labels=[
            packed
            for packed in plate.labels
            if provenance.job_of_label(packed.source_label.id) in wanted
        ],
    )


def _bucket_job_id(bucket: _LayerBucket, job_id: str, provenance: Optional[JobProvenance]) -> str:
    """Filename ``<job_id>`` token of one plate layer.

    A layer carrying exactly one job's strokes is named for that job, so a
    batch names those files exactly as the member's standalone run does. A
    layer the jobs share is named for the batch (``job_id``): the file belongs
    to every job engraved on that tool, and its membership is recorded in
    :attr:`PerCutterExport.file_jobs` and the reports rather than in a name the
    39-character plotter cap would truncate.

    Args:
        bucket: The layer being written.
        job_id: The run-level filename token (the batch id for a jobset).
        provenance: Jobset side table.

    Returns:
        The lone contributing member's token, else ``job_id``.
    """
    if provenance is not None and len(bucket.job_keys) == 1:
        record = provenance.record(bucket.job_keys[0])
        if record is not None:
            return record.job_id
    return job_id


def _segment_cutters(
    segment: PackedPlate, owner: str, default: float, default_width: float
) -> tuple[float, float]:
    """Boundary/hole cutter (and kerf-buffered width) of one plate segment.

    Jobset members may declare different border/hole tools. On a shared
    plate the file must be named for — and engraved with — the tool that
    job's own strokes use, so the job-level maximum is taken per segment,
    never across the whole pool.

    Args:
        segment: The plate segment.
        owner: Owning job key, or ``""`` for a single-job run.
        default: Job-level boundary/hole cutter to keep when ``owner`` is
            empty (byte-identical names for a standalone job).
        default_width: Job-level buffered width.

    Returns:
        ``(cutter, engraved_width)`` for this segment.
    """
    if not owner:
        return default, default_width
    cutters = [packed.source_label.hole_cutter_diameter for packed in segment.labels]
    widths = [packed.source_label.effective_hole_cutter_diameter for packed in segment.labels]
    cutter = max(cutters, default=default)
    return cutter, max(widths, default=cutter)


def _segment_spec(
    owner: str,
    job_spec: Optional[JobSpec],
    layer_specs: Optional[Mapping[str, JobSpec]],
) -> Optional[JobSpec]:
    """The spec whose ``tool_options`` prefix one segment's files carry.

    A shared PLT file is assembled from per-job segments, and each segment's
    engraver parameters (feed rate, spindle, Z clearance) belong to the job
    that owns those strokes — so the header block is prepended per segment
    from that member's own spec.

    Args:
        owner: Owning job key, or ``""`` for a single-job run.
        job_spec: The run-level spec (used when ``owner`` is empty or the
            side table has no entry for it).
        layer_specs: ``job_key -> member spec``, from the jobset plan.

    Returns:
        The spec to prepend headers from.
    """
    if owner and layer_specs is not None:
        member = layer_specs.get(owner)
        if member is not None:
            return member
    return job_spec


def _warn_split_layers(
    buckets: Sequence[_LayerBucket], *, plate_str: str, logger: Optional[TextLogger]
) -> None:
    """Warn when jobs that share a tool cannot share one run.

    Two jobs on one sheet merge into a single file per cutter whenever they
    can. They cannot when their ``tool_options`` header blocks differ: one PLT
    carries one block, so the layer is written once per header block and the
    tool is physically re-run. That is a real (and avoidable) cost, so it is
    logged, not buried.

    Args:
        buckets: Every bucket of one plate (structural + text).
        plate_str: The plate's file-name prefix, for the log line.
        logger: The text logger (``None`` logs nothing).
    """
    if logger is None:
        return
    by_tool: dict[tuple[str, float], List[_LayerBucket]] = {}
    for bucket in buckets:
        by_tool.setdefault((bucket.kind, bucket.cutter), []).append(bucket)
    for (kind, cutter), group in by_tool.items():
        if len(group) < 2:
            continue
        jobs = "; ".join("+".join(bucket.job_keys) for bucket in group)
        logger.warning(
            f"Plate {plate_str}: the {kind} tool {_format_cutter(cutter)} is split "
            f"into {len(group)} files because these jobs' tool_options headers "
            f"differ, so the cutter is re-run ({jobs})."
        )


def export_per_cutter_plts(
    resolved_labels: list[ResolvedLabel],
    provided_plates: list[PlateSpec] | None = None,
    output_dir: str | Path = "output",
    job_id: str = "job",
    job_name: str = "",
    optimize: bool = True,
    plots: bool = True,
    default_plots: bool = False,
    allow_rotation: bool = True,
    fast_mode: bool = False,
    logger: Optional[TextLogger] = None,
    layout: LayoutMode = DEFAULT_LAYOUT_MODE,
    default_plate_size: Optional[tuple[float, float]] = None,
    default_plate_clearance: Optional[tuple[float, float]] = None,
    job_spec: Optional[JobSpec] = None,
    job_config: Optional[JobConfig] = None,
    available_cutters: Optional[list[float]] = None,
    stage_timings: Optional[StageTimings] = None,
    layer_specs: Optional[Mapping[str, JobSpec]] = None,
    provenance: Optional[JobProvenance] = None,
) -> PerCutterExport:
    """Export plates as per-cutter PLT files (and optional simple PDFs).

    Implements the three-phase pipeline (render -> pack -> assemble) and
    then splits each assembled plate by CUTTER rather than by logical
    layer:

    - **borders + holes** share one file (same tool, engraved together),
      named ``[<plate>_][<material>_]<cutter>_bh_<job_id>.plt`` where
      ``<cutter>`` is the boundary/hole cutter diameter (``bh`` =
      borders-holes).
    - **text** gets one file per distinct cutter/color layer, named
      ``[<plate>_][<material>_]<cutter>[_<color>]_txt_<job_id>.plt`` (the
      ``<color>`` tag appears when the layer carries a ``text_color``,
      e.g. ``wk_uv_40_m_txt_job.plt`` for magenta). A text cutter equal
      to the boundary/hole cutter still gets its own file (separate run).
      Lines sharing a cutter but differing in ``text_color`` split into
      separate files so the cutter depth can change between runs (3-layer
      material). The inline ``<tc:color>`` markup splits the same way per
      character (each colored run is its own ``(cutter, color)`` layer).
    - The leading ``[<plate>_][<material>_]`` prefix is built by
      :func:`_format_plate_prefix`: the 1-based packing-order plate number
      (zero-padded to two digits) is **omitted whenever the plate's
      material group spans a single sheet**, so its presence signals that
      *this material* needs more than one plate; the sanitized material tag
      (e.g. ``wk(uv)`` -> ``wk_uv``) is omitted for material-agnostic
      plates. Material-agnostic plates form one group, so multi-sheet
      material-less jobs always keep their plate numbers. The cutter
      precedes the kind token so the tool size is visible before the
      text/structural distinction.
    - The combined per-plate PLT is assembled **in memory only** (never
      written) and exposed via :attr:`PerCutterExport.combined_by_plate`;
      when ``plots`` is enabled it also drives the combined
      ``[<plate>_][<material>_]all_<job_id>.pdf`` preview. A jobset run
      additionally writes one **per contributing job** ``all_<member_id>.pdf``
      proof for every plate its jobs share, so each job has a proof of its own
      labels on the shared sheet (see
      :attr:`PerCutterExport.combined_by_plate_and_job`).
    - Jobset runs group each plate layer **by tool**: one file per (cutter,
      color, header block) shared by every job engraving with that tool, so a
      sheet two jobs share yields a single ``_bh_`` file per boundary/hole
      cutter and a single ``_txt_`` file per cutter/color layer - one run per
      tool, not one per job. See the ``provenance`` argument.

    PLT files are written under ``output_dir/plt/`` and PDFs under
    ``output_dir/pdf/``. Cutter diameters are formatted as integer
    thousandths of an inch, unpadded (see :func:`_format_cutter`). Every
    written PLT name is capped at 39 characters including the extension
    (the plotter controller rejects longer names; see
    :func:`plt_optimizer.core.filenames.ensure_plot_filename_length`).
    Empty pen groups are skipped.

    Args:
        resolved_labels: List of resolved labels from Phase 2 resolution.
        provided_plates: Optional list of PlateSpec objects. ``None`` (or
            empty) selects unbounded mode: the layout engine auto-allocates
            default sheets sized by ``default_plate_size`` (falling back to
            24" x 16"), overflowing onto as many sheets as the labels need.
        output_dir: Base output directory; ``plt/`` and ``pdf/``
            subdirectories are created inside it.
        job_id: Job identifier used as the file-name prefix (should be
            filesystem-safe; the CLI sanitizes the job name).
        job_name: Human-readable job name for plot titles (optional).
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
            downward (the historical behavior). Individual plates may
            override it via ``PlateSpec.layout``.
        default_plate_size: ``(width, height)`` override (inches) for the
            auto-allocated unbounded bins (from ``job-config.json``
            ``plate_width`` / ``plate_height``); the module default
            (24 x 16) applies when ``None``. Ignored when plates are given.
        default_plate_clearance: ``(left, top)`` edge clearance (inches)
            applied to every auto-allocated unbounded bin (from
            ``job-config.json`` ``left_clearance`` / ``top_clearance``).
            Ignored when plates are given (their own clearances apply).
        job_spec: Optional JobSpec for applying tool option headers to
            generated PLT files. When provided, tool options (engraver
            parameters like cutting velocity, spindle speed, etc.) are
            prepended to the PLT headers. If ``None``, no tool option
            headers are added (backwards compatible).
        job_config: Optional JobConfig with tool_options metadata (command
            strings, dual defaults, bounds). Required when ``job_spec`` is
            provided; ignored otherwise.
        available_cutters: Optional shop cutter inventory (inches, from
            ``tools.json``). When provided, enables the compression-driven
            cutter reduction pre-pass (see
            :func:`plt_optimizer.generate.cutter_downsize.apply_compression_cutter_downsize`):
            labels whose text lines were horizontally compressed swap their
            *automatic* cutter for the next smaller inventory tool **before**
            the pen map is built, so layers and file names reflect the final
            tools. ``None`` (the default) disables the pre-pass (every label
            exports exactly as before).
        stage_timings: Optional collector that receives the per-stage
            wall-clock timings directly. The stage timers normally report
            through an INFO log record, which the logging level gates; a
            consumer that needs the numbers regardless of the log level
            (the generation report) passes a collector here.
        layer_specs: Jobset only: ``job_key -> member JobSpec``, used to
            prepend **each contributing member's own** ``tool_options`` header
            block to the plate files (see :func:`_segment_spec`). Members whose
            blocks are identical share one file; members whose blocks differ
            cannot share a run, so that layer is written once per block (and
            warned about). Ignored when ``provenance`` is ``None``.
        provenance: Jobset only: the composite-id side table from
            :func:`plt_optimizer.generate.jobset.build_jobset_plan`. When
            given, each plate layer is grouped **by tool, not by job**: every
            job whose strokes engrave with the same cutter (and, for text, the
            same color and header block) shares one file and one routing, so a
            shared plate yields ONE ``01_kw_40_txt_*.plt`` carrying both jobs
            instead of one file per job. That is the point of the batch - fewer
            files, fewer tool swaps, and one travel route over all the strokes
            that tool makes. A file's ``<job_id>`` token is the batch id when
            jobs share it and the lone member's own id otherwise; every job
            contributing to a file is recorded in
            :attr:`PerCutterExport.file_jobs`, which is how the per-job reports
            measure themselves out of shared files. Mixed plates also record
            per-job combined content
            (:attr:`PerCutterExport.combined_by_plate_and_job`) so each job can
            be proofed on its own. ``None`` (the default) is the single-job
            path and produces byte-identical output to a run without this
            parameter.

    Returns:
        A :class:`PerCutterExport` with written PLT paths, PDF paths, and
        the in-memory combined content per plate.
    """
    from plt_optimizer.generate.packing.layout import generate_layout_with_bounds

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # No plates means unbounded mode: the layout engine auto-allocates
    # default sheets sized by ``default_plate_size`` (from job-config.json)
    # and overflows onto as many as the labels need. Passing ``None``
    # through (rather than synthesizing a single plate) keeps this path
    # identical to ``generate_layout``'s own unbounded behavior.
    if provided_plates is not None and len(provided_plates) == 0:
        provided_plates = None

    # List marker-column layout (pre-pass): every bullet/enumerated list line
    # gains its hanging indent (the widest marker of its list plus the
    # cascaded marker gap), measured through the same renderer dispatch the
    # label render path uses. Runs FIRST, before the reflow below, so the
    # reflow partitions an item's words against the text budget right of the
    # marker column. No-op (same list) when no label contains a list.
    from plt_optimizer.generate.list_layout import apply_list_marker_layout

    with stage_timer("list_layout", _LOG, timings=stage_timings):
        resolved_labels = apply_list_marker_layout(resolved_labels)

    # Word-level line-content reflow (pre-pass): consecutive text lines that
    # enable ``optimize_line_content`` exchange whole words (word order kept,
    # only the line breaks move) so their natural widths equalize, minimizing
    # the compression the label needs. Runs FIRST among the pre-passes, so the
    # cutter reduction and shared compression below measure the reflowed text.
    # No-op (same list) when no label enables it.
    from plt_optimizer.generate.line_content import apply_line_content_reflow

    with stage_timer("reflow", _LOG, timings=stage_timings):
        resolved_labels = apply_line_content_reflow(resolved_labels)

    # Per-label shared horizontal compression (pre-pass): when a label enables
    # ``h_compress_global``, a line's compression is measured and the group's
    # most-compressed scale is applied to its same-height siblings BEFORE the
    # pen map / packing / rendering run. No-op (same list) when no label
    # enables it. Runs BEFORE the cutter reduction, so the cutter trigger
    # measures the SHARED scale (the scale the label is actually engraved
    # at) and the same nominal height therefore gets the same compression AND
    # the same tool.
    from plt_optimizer.generate.h_compress import apply_global_h_compress

    with stage_timer("h_compress", _LOG, timings=stage_timings):
        resolved_labels = apply_global_h_compress(resolved_labels)

    # Compression-driven cutter reduction (pre-pass): render-measure the
    # labels once and downsize the *automatic* cutters of compressed lines
    # BEFORE the pen map / packing / rendering consume them, so the per-cutter
    # layers, PLT file names, kerning clearance and collision stroke floor all
    # see the final tools. No-op (same list) without an inventory. Runs after
    # the shared compression, so the midpoint trigger sees the shared scale.
    from plt_optimizer.generate.cutter_downsize import apply_compression_cutter_downsize

    with stage_timer("cutter_downsize", _LOG, timings=stage_timings):
        resolved_labels = apply_compression_cutter_downsize(resolved_labels, available_cutters)

    # Pen == (cutter, color): assign one HPGL pen per distinct text
    # cutter/color layer so the assembled plate can be split into
    # per-layer files after assembly. Relief labels pre-reserve their
    # candidate cutter pens (the ladder itself is selected after rendering,
    # see the relief stage below); unused pens stay empty and are skipped.
    from plt_optimizer.generate.relief.export import relief_pen_layers

    pen_map = build_cutter_pen_map(
        resolved_labels,
        extra_layers=relief_pen_layers(resolved_labels, available_cutters or ()),
    )
    layer_by_pen = {pen: layer for layer, pen in pen_map.items()}

    # Job-level boundary/hole cutter (all labels share the job-resolved
    # value; fall back to the default for content-less jobs).
    hole_cutter = max(
        (label.hole_cutter_diameter for label in resolved_labels),
        default=DEFAULT_BOUNDARY_HOLE_CUTTER,
    )

    # Physical cutter (inches) -> the width it engraves (tool + the widest
    # kerf buffer among the lines that use it). The per-cutter PLT file names
    # and the pen map stay keyed on the PHYSICAL tool (a 0.030in cutter always
    # writes ``30_txt_*.plt``), so the previews -- whose width the file-name
    # token supplies -- need this map to draw the real ink corridor.
    cutter_widths: dict[float, float] = {}
    for label in resolved_labels:
        for line in label.content:
            for physical, engraved in zip(line.used_cutters, line.effective_used_cutters):
                cutter_widths[physical] = max(cutter_widths.get(physical, 0.0), engraved)

    # Pen -> the width each pen ENGRAVES (tool + kerf buffer): the true stroke
    # width the simple physical-width plots draw. Text pens come from the
    # (cutter, color) pen map; the structural pens (borders, holes) all share
    # the job hole cutter. The combined plot is split by pen and every stroke
    # is expanded to this width, so the proof matches the material the machine
    # removes (a 45-degree-flank V-bit cuts wider than its nominal diameter).
    pen_widths: dict[int, float] = {
        pen: cutter_widths.get(cutter, cutter) for pen, (cutter, _color) in layer_by_pen.items()
    }
    # Job-level boundary/hole kerf buffer (all labels share the job-resolved
    # value; fall back to no buffer for content-less jobs).
    hole_cutter_width = max(
        (label.effective_hole_cutter_diameter for label in resolved_labels),
        default=hole_cutter,
    )
    pen_widths[LAYER_BOUNDARY] = hole_cutter_width
    pen_widths[LAYER_HOLES] = hole_cutter_width

    # Pen -> stroke-color tag: the colored combined-plot previews resolve
    # each text pen's ink color from its (text_color / <tc:>) layer tag,
    # falling back to the plate material (see generate/preview_colors.py).
    pen_colors: dict[int, str] = {pen: color for pen, (_cutter, color) in layer_by_pen.items()}

    # Phase 2: Generate layout with rendered bounds (labels rendered onto
    # their cutter pens).
    with stage_timer("layout_render_pack", _LOG, timings=stage_timings):
        packed_plates, rendered_labels_map = generate_layout_with_bounds(
            resolved_labels,
            provided_plates,
            pen_map=pen_map,
            allow_rotation=allow_rotation,
            layout=layout,
            default_plate_size=default_plate_size,
            default_plate_clearance=default_plate_clearance,
        )

    plt_dir = output_dir / "plt"
    plt_dir.mkdir(parents=True, exist_ok=True)

    # Relief engraving stage (after render/pack, before assembly + per-cutter
    # split): relief labels' ink becomes raised islands, the label is
    # raster-filled by a cutter ladder, and the label's render is replaced
    # by the relief chunk records (the normal text strokes are suppressed
    # and drill holes are filled over). The relief pens' preview widths are
    # merged into pen_widths so the PDF proofs draw the true engraved width.
    relief_pen_widths: dict[int, float] = {}
    if any(label.relief_engraving for label in resolved_labels):
        from plt_optimizer.generate.relief.export import apply_relief_to_plates

        with stage_timer("relief", _LOG, timings=stage_timings):
            relief_pen_widths = apply_relief_to_plates(
                packed_plates,
                rendered_labels_map,
                layer_by_pen,
                pen_map,
                available_cutters or (),
                logger=logger,
            )
    for pen, width in relief_pen_widths.items():
        pen_widths[pen] = max(pen_widths.get(pen, 0.0), width)

    result = PerCutterExport(
        output_dir=output_dir.resolve(),
        job_id=job_id,
        rendered_labels=rendered_labels_map,
        packed_plates=list(packed_plates),
    )

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

        # Named helpers ASSIGNED to the declared ``strategy_factory``: a nested
        # ``def`` of that same name would obscure the declaration above (pyright),
        # and an assignment of a ``lambda`` is banned by ruff (E731).
        def _nn2opt_factory(baseline_distance: float) -> OptimizationStrategy:
            """Fast mode: plain nearest-neighbour + 2-opt, no ensemble context."""
            return NearestNeighbor2OptStrategy()

        def _ensemble_factory(baseline_distance: float) -> OptimizationStrategy:
            """Full mode: the parallel ensemble, which wants the baseline rapid."""
            return ParallelEnsembleStrategy(baseline_distance=baseline_distance)

        strategy_factory = _nn2opt_factory if fast_mode else _ensemble_factory

    def _report(layer: str, optimization: PlateOptimization) -> None:
        """Log one layer's optimization outcome (dual-logging topology)."""
        if logger is None:
            return
        outcome = optimization.outcome
        # Compare emitted rapid travel (intra + inter) on both sides so the
        # A -> B pair is apples-to-apples; `optimized_distance` is the
        # strategies' inter-chunk-only metric and is reported separately.
        emitted_after = outcome.optimized_doc.rapid_distance()
        parts = [
            f"routing {optimization.baseline_distance:.3f} -> {emitted_after:.3f}",
            f"inter-chunk {outcome.optimized_distance:.3f}",
        ]
        if outcome.direction_sweep_travel_before is not None:
            parts.append(
                "direction sweep "
                f"{outcome.direction_sweep_travel_before:.3f} -> "
                f"{outcome.direction_sweep_travel_after:.3f} in "
                f"{outcome.direction_sweep_passes} pass(es), "
                f"{outcome.direction_sweep_flips} flip(s)"
            )
        if outcome.intra_sweep_travel_before is not None:
            parts.append(
                "intra sweep "
                f"{outcome.intra_sweep_travel_before:.3f} -> "
                f"{outcome.intra_sweep_travel_after:.3f} in "
                f"{outcome.intra_sweep_groups} group(s), "
                f"{outcome.intra_sweep_flips} flip(s)"
            )
        if outcome.merged_paths_before is not None and outcome.merges_applied > 0:
            parts.append(
                f"path merge {outcome.merged_paths_before} -> "
                f"{outcome.merged_paths_after} in {outcome.merges_applied} merge(s)"
            )
        logger.info(
            f"Plate {layer}: optimized {optimization.node_count} node(s) via "
            f"{outcome.method_name} -- rapid travel "
            f"{optimization.baseline_distance:.3f} -> {emitted_after:.3f} "
            f"plotter units ({', '.join(parts)})"
        )

    # Written PLT path -> the width its strokes ENGRAVE (inches). The mirrored
    # per-file PDF previews read their stroke width from the file-name cutter
    # token, which is the PHYSICAL tool (the tool identity must stay physical
    # so the name matches a tool the shop owns), so the kerf-buffered width is
    # tracked here per file instead. The text and structural cutters can share
    # a nominal size while carrying different buffers, which is why this is
    # keyed by path and not merged into `cutter_widths`.
    file_widths: dict[Path, float] = {}

    # Per-plate device-space stroke-width maps (see
    # :func:`plt_optimizer.generate.plate_optimizer.build_stroke_width_map`).
    # The per-file previews read theirs by written PLT path; the combined
    # previews by plate number. A job whose buffers are all zero maps every
    # stroke to its physical cutter (= the pen width), so the previews stay
    # visually identical.
    plate_stroke_widths: dict[int, dict[WidthKey, float]] = {}
    file_stroke_widths: dict[Path, dict[WidthKey, float]] = {}

    # Phase 3: Assemble each plate in memory, then split by pen group.
    # Files are named [<plate>_][<material>_]<cutter>_<kind>_<job_id>: the
    # plate number (1-based packing-order index, 2-digit padded) is
    # omitted whenever the plate's material group spans a single sheet,
    # and the sanitized material tag follows (see _format_plate_prefix).
    result.material_by_plate = {
        plate_no: plate.material for plate_no, plate in enumerate(packed_plates, start=1)
    }
    plate_counts = _material_plate_counts(result.material_by_plate)
    for plate_no, plate in enumerate(packed_plates, start=1):
        prefix = _format_plate_prefix(plate_no, plate_counts[plate_no], plate.material)
        plate_str = prefix.rstrip("_") or f"{plate_no:02d}"
        # Per-stroke engraved widths for the previews (a <cb:D> span shares
        # its pen with the surrounding text; the per-pen width alone cannot
        # show it). Empty map = every preview keeps the per-pen width.
        stroke_widths = build_stroke_width_map(plate.labels, rendered_labels_map)
        plate_stroke_widths[plate_no] = stroke_widths
        owners = _plate_owners(plate, provenance)
        if provenance is not None:
            result.plate_jobs[plate_no] = owners
        # Combined whole-plate assembly: in memory only, it drives the
        # per-plate preview, the report's combined view, and the per-job
        # proofs below.
        with stage_timer("assemble_split", _LOG, timings=stage_timings):
            combined = assemble_plt_from_rendered_labels(plate, rendered_labels_map)
        result.combined_by_plate[plate_no] = combined
        # Per-job sheet content on the plates its jobs SHARE (a sheet one job
        # owns outright already has that job as its sheet proof): it drives the
        # per-job proof PDFs, and it is the membership test that keeps a merged
        # file's job list - and its name - honest.
        per_owner: dict[str, str] = {}
        if provenance is not None and len(owners) > 1:
            with stage_timer("assemble_split", _LOG, timings=stage_timings):
                per_owner = {
                    owner: assemble_plt_from_rendered_labels(
                        _segment_plate(plate, owner, provenance), rendered_labels_map
                    )
                    for owner in owners
                }
            result.combined_by_plate_and_job[plate_no] = per_owner
        # Jobset file groups: a plate layer is written as ONE file per
        # (cutter, color, header block), shared by every job whose strokes
        # engrave with that tool - a shared sheet yields one `kw_45_txt_*`
        # file carrying both jobs, routed together. A job boundary never splits
        # a cutter; only a different tool, ink, material, sheet, or a
        # `tool_options` header the file cannot both carry and honour does. A
        # single-job run yields one bucket per layer holding the whole plate,
        # which is exactly the pre-jobset path (byte-identical output).
        bh_buckets = _bh_buckets(
            plate,
            owners,
            provenance,
            hole_cutter=hole_cutter,
            hole_cutter_width=hole_cutter_width,
            job_spec=job_spec,
            layer_specs=layer_specs,
            job_config=job_config,
            per_owner=per_owner,
        )
        text_buckets = _text_buckets(
            owners,
            layer_by_pen=layer_by_pen,
            pen_widths=pen_widths,
            job_spec=job_spec,
            layer_specs=layer_specs,
            job_config=job_config,
            per_owner=per_owner,
        )
        _warn_split_layers([*bh_buckets, *text_buckets], plate_str=plate_str, logger=logger)
        for bucket in bh_buckets:
            # A bucket covering the whole plate reuses the assembly above (no
            # second render pass); a split layer assembles its own subset.
            with stage_timer("assemble_split", _LOG, timings=stage_timings):
                bucket_combined = (
                    combined
                    if bucket.owners == owners
                    else assemble_plt_from_rendered_labels(
                        _bucket_plate(plate, bucket, provenance), rendered_labels_map
                    )
                )
                structure_content = extract_pens_from_plt_text(
                    bucket_combined, [LAYER_BOUNDARY, LAYER_HOLES]
                )
            # Structural group: borders (SP2) + holes (SP3) share one run.
            bh_tag = _bucket_tag(bucket.owners, plate_str, "bh")
            if plt_has_geometry(structure_content):
                bh_content = structure_content
                if optimize and strategy_factory is not None:
                    with stage_timer("optimize_layer", _LOG, timings=stage_timings):
                        optimization = optimize_structural_layer(
                            structure_content,
                            strategy_factory,
                            logger=logger,
                            log_prefix=f"[plate {bh_tag}]",
                        )
                    if optimization is not None:
                        bh_content = optimization.content
                        _report(bh_tag, optimization)

                # Prepend tool option headers: every job in the bucket
                # generates the identical block (that is the merge test).
                if bucket.spec is not None and job_config is not None:
                    from plt_optimizer.generate.tool_options import prepend_tool_option_headers

                    bh_content = prepend_tool_option_headers(
                        bh_content, bucket.spec, job_config, "borders_holes"
                    )

                structure_token = _bucket_job_id(bucket, job_id, provenance)
                structure_path = (
                    plt_dir / f"{prefix}{_format_cutter(bucket.cutter)}_bh_{structure_token}.plt"
                )
                # Plotter-controller cap: the machine rejects names longer than
                # 39 characters, so the name is shortened from the right (the
                # mirrored PDF previews inherit the shortened stem).
                structure_path = ensure_plot_filename_length(structure_path, _LOG)
                _write_plt(structure_path, bh_content)
                result.plt_paths.append(structure_path)
                file_widths[structure_path] = bucket.width
                job_keys = bucket.job_keys
                if job_keys:
                    result.file_jobs[structure_path] = job_keys

            # Text group: one file per distinct text cutter/color pen with
            # content. With optimization enabled the layer is routed directly
            # from the rendered chunk records in plate space (parser/profiler
            # skipped); otherwise the pen layer is extracted from the
            # assembly as-is.
            for text_bucket in text_buckets:
                pen_id = text_bucket.pen_id
                if pen_id is None:  # pragma: no cover - text buckets always carry a pen
                    continue
                cutter, text_color = layer_by_pen[pen_id]
                layer_tag = _format_text_layer(cutter, text_color)
                text_tag = _bucket_tag(text_bucket.owners, plate_str, f"text {layer_tag}")
                written_content: Optional[str] = None
                if optimize and strategy_factory is not None:
                    with stage_timer("optimize_layer", _LOG, timings=stage_timings):
                        optimization = optimize_text_layer(
                            _bucket_plate(plate, text_bucket, provenance).labels,
                            rendered_labels_map,
                            pen_id,
                            strategy_factory,
                            logger=logger,
                            log_prefix=f"[plate {text_tag}]",
                        )
                    if optimization is not None:
                        written_content = optimization.content
                        _report(text_tag, optimization)
                if written_content is None:
                    with stage_timer("assemble_split", _LOG, timings=stage_timings):
                        bucket_combined_text = (
                            combined
                            if text_bucket.owners == owners
                            else assemble_plt_from_rendered_labels(
                                _bucket_plate(plate, text_bucket, provenance),
                                rendered_labels_map,
                            )
                        )
                        text_content = extract_pens_from_plt_text(bucket_combined_text, [pen_id])
                    if not plt_has_geometry(text_content):
                        continue
                    written_content = text_content

                # Prepend tool option headers (identical for every job here).
                if text_bucket.spec is not None and job_config is not None:
                    from plt_optimizer.generate.tool_options import prepend_tool_option_headers

                    written_content = prepend_tool_option_headers(
                        written_content, text_bucket.spec, job_config, "text"
                    )

                text_token = _bucket_job_id(text_bucket, job_id, provenance)
                text_path = plt_dir / f"{prefix}{layer_tag}_txt_{text_token}.plt"
                # Plotter-controller cap (see the structural write above).
                text_path = ensure_plot_filename_length(text_path, _LOG)
                _write_plt(text_path, written_content)
                result.plt_paths.append(text_path)
                file_widths[text_path] = text_bucket.width
                file_stroke_widths[text_path] = stroke_widths
                if text_bucket.job_keys:
                    result.file_jobs[text_path] = text_bucket.job_keys

    # Extract text height from resolved labels for plot titles
    text_height: float | None = None
    if resolved_labels and resolved_labels[0].content:
        # Use the nominal text height from the first text line of the first label
        text_height = resolved_labels[0].content[0].nominal_text_height

    # Combined proof previews: the configured page background (drill
    # holes punch this same color; default light grey).
    preview_background = (
        job_config.defaults.preview_background_color if job_config is not None else None
    ) or DEFAULT_PREVIEW_BACKGROUND_COLOR
    if plots:
        with stage_timer("plots", _LOG, timings=stage_timings):
            result.pdf_paths = _write_simple_plots(
                output_dir,
                job_id,
                result,
                job_name=job_name,
                text_height=text_height,
                pen_widths=pen_widths,
                pen_colors=pen_colors,
                preview_background=preview_background,
                file_widths=file_widths,
                file_stroke_widths=file_stroke_widths,
                plate_stroke_widths=plate_stroke_widths,
                provenance=provenance,
            )
    if default_plots:
        with stage_timer("default_plots", _LOG, timings=stage_timings):
            result.default_pdf_paths = write_default_plots(
                output_dir, job_id, result, job_name=job_name, text_height=text_height
            )

    return result


def _write_simple_plots(
    output_dir: Path,
    job_id: str,
    result: PerCutterExport,
    job_name: str = "",
    text_height: float | None = None,
    pen_widths: Optional[Mapping[int, float]] = None,
    pen_colors: Optional[Mapping[int, str]] = None,
    preview_background: str = DEFAULT_PREVIEW_BACKGROUND_COLOR,
    file_widths: Optional[Mapping[Path, float]] = None,
    file_stroke_widths: Optional[Mapping[Path, Mapping[WidthKey, float]]] = None,
    plate_stroke_widths: Optional[Mapping[int, Mapping[WidthKey, float]]] = None,
    provenance: Optional[JobProvenance] = None,
) -> list[Path]:
    """Write true-stroke-width PDF previews for a per-cutter export.

    Parses every written PLT and renders a physical-width PDF into
    ``output_dir/pdf/`` mirroring the PLT file names: every cutting stroke
    is expanded into the ink corridor its cutter engraves (see
    :func:`~plt_optimizer.diagnostics.plotter.plot_physical_strokes`), so
    each stroke measures its real engraved width on the inch grid (the
    physical cutter plus the cascaded ``cutter_buffer_text`` /
    ``cutter_buffer_routing`` kerf buffer, supplied per file through
    ``file_widths``; the file-name cutter token itself stays the physical
    tool). The structural (``bh`` = borders + holes) layers keep the
    semi-transparent underlay look while text ink renders fully opaque.

    Additionally renders one combined ``[<plate>_][<material>_]all_<job_id>.pdf``
    per plate from the in-memory combined content (text + borders + holes
    together). When ``pen_widths`` is provided, the combined content is
    split per pen (the assembly keeps its ``SP`` sections) and every pen
    draws at its own engraved width; without it the combined plot falls back
    to the thin centerline preview.

    The combined plots are colored proofs (see
    :mod:`plt_optimizer.generate.preview_colors`): the page background is
    ``preview_background`` (the configured ``preview_background_color``,
    default light grey); each text pen's ink takes its resolved color (pen
    ``text_color`` / ``<tc:>`` tag first, then the plate material's core
    color, then black -- white over dark material backgrounds), and the
    label boundary rectangles (SP2) fill with the material's background
    color under the ink. Plates whose material is unset (or carries no
    recognized color) proof as ``wk`` stock: white label background, black
    text. Borders (SP2) and drill holes (SP3) share the opaque contrast
    outline (black over a white material background, white over every other
    background); the holes punch the preview background color
    ("see-through" discs). Per-cutter ``_txt_``/``_bh_`` files are never
    colored.

    Fallbacks keep the historical thin-line
    :func:`~plt_optimizer.diagnostics.plotter.plot_plt_document`
    (``simple_mode=True``) render for stems whose cutter cannot be parsed
    and for synthetic exports without ``pen_widths``.

    matplotlib is imported lazily through the plotter module so headless
    optimizer-only environments never pay the import cost.

    Args:
        output_dir: Base output directory (``pdf/`` is created inside).
        job_id: Job identifier used in combined PDF names.
        result: The export result whose ``plt_paths`` and
            ``combined_by_plate`` drive the plots.
        job_name: Human-readable job name for plot titles (optional).
        text_height: Text height in inches for text plot titles (optional).
        pen_widths: Engraved stroke width (inches) per HPGL pen number of
            the assembled plate -- the physical cutter plus its kerf buffer
            (text pens from the pen map, structural pens 2/3 at the job hole
            cutter). Enables per-stroke widths in the combined plots. ``None``
            renders combined plots thin.
        pen_colors: Stroke-color tag per HPGL pen number (the pen map's
            ``text_color`` value; ``"none"`` = implicit default). Colors
            the combined-plot text ink via
            :func:`~plt_optimizer.generate.preview_colors.resolve_text_color`
            together with each plate's material. ``None`` keeps black ink
            (material background fills still apply).
        preview_background: Page/plot background color of the combined
            proof previews (``job-config.json``
            ``preview_background_color``, default
            :data:`~plt_optimizer.generate.preview_colors.DEFAULT_PREVIEW_BACKGROUND_COLOR`).
            Drill holes punch this same color ("see-through" discs); their
            outlines share the label border's contrast color.
        file_widths: Engraved stroke width (inches) per written PLT path (the
            physical cutter token in its name plus that layer's kerf buffer).
            Overrides the width parsed from the file name for the mirrored
            per-file previews, so a buffered job proofs the real ink corridor
            while the file name keeps naming the tool the shop owns. ``None``
            (direct/synthetic callers) plots the nominal token width.
        file_stroke_widths: Per-stroke engraved widths (device endpoint key ->
            inches, see
            :func:`plt_optimizer.generate.plate_optimizer.build_stroke_width_map`)
            per written PLT path. When a parsed segment's endpoints hit the
            map, that stroke draws at its own engraved width (a ``<cb:D>``
            span visible inside a pen its neighbours share); segments the map
            does not cover keep the file's ``file_widths`` entry. ``None``
            keeps the per-file width uniform.
        plate_stroke_widths: The same per-stroke width maps keyed by plate
            number for the combined previews. ``None`` keeps each pen at its
            single ``pen_widths`` entry.
        provenance: Jobset only: the composite-id side table. When set, every
            plate that carries more than one job gets the per-job overlay
            boxes (dashed coloured rectangles plus a legend, see
            :func:`~plt_optimizer.generate.jobset.job_overlay_boxes`) drawn
            on its combined ``*_all_*`` proof, so a shared sheet says which
            labels belong to which job. ``None`` (a single-job run) draws no
            overlay and leaves the proofs byte-identical to before.

    Returns:
        List of written PDF paths.
    """
    from plt_optimizer.core.parser import PLTParser
    from plt_optimizer.diagnostics.plotter import (
        PHYSICAL_STRUCTURAL_ALPHA,
        JobOverlayBoxLike,
        PhysicalFillLayer,
        PhysicalStrokeLayer,
        plot_physical_strokes,
        plot_plt_document,
    )
    from plt_optimizer.diagnostics.stroke_geometry import closed_path_polygons
    from plt_optimizer.generate.jobset import job_overlay_boxes
    from plt_optimizer.generate.preview_colors import (
        DEFAULT_MATERIAL_COLORS,
        outline_color_for,
        parse_material_colors,
        resolve_text_color,
    )

    pdf_dir = output_dir / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    parser = PLTParser()
    pdf_paths: list[Path] = []
    combined_counts = _material_plate_counts(result.material_by_plate)
    # Jobset proofs: one overlay box per job on every mixed plate. Pure
    # geometry computed once here; the plotter only draws what it gets.
    overlays = (
        job_overlay_boxes(provenance, result.packed_plates, result.plate_jobs)
        if provenance is not None
        else {}
    )

    def _segments_of(document: PLTDocument) -> List[Segment]:
        """Flatten every stroke path's segments of a parsed document."""
        return [seg for path in document.stroke_paths for seg in path.segments]

    def _stem_cutter_inches(stem: str) -> Optional[float]:
        """Parse the cutter-diameter token of a PLT stem, if present."""
        cutter_token = _parse_plt_stem(stem).cutter
        if cutter_token is None:
            return None
        try:
            width = _cutter_token_to_inches(cutter_token)
        except ValueError:
            return None
        return width if width > 0.0 else None

    def _lookup_width(
        stroke_widths: Mapping[WidthKey, float],
        segment: Segment,
        default_width: float,
    ) -> float:
        """Look up one parsed segment's engraved width, tolerating a 1-unit drift.

        The optimized per-file stream keys exactly (the plate transform and
        the emission share their rounding). The combined proof parses the
        un-optimized assembly, whose vertical centering applies its float
        adjustment to the integer device Y (points AND ``AA`` centers), while
        the plate transform rounds it into the local -> device chain -- the
        two land up to one plotter unit apart (the Y drift propagates through
        the parser's AA chain into X too). A +/-1-unit probe recovers those:
        probes are tried in increasing total drift, and the FIRST drift level
        that hits accepts only a UNIQUE width (an ambiguous level falls back
        to the pen width, the conservative per-pen behavior -- glyph geometry
        is dense, so neighbouring strokes' keys sit within one unit and a
        union over all levels would be ambiguous almost everywhere). Lines
        probe their normalized endpoints; arcs probe their (center, |sweep|)
        key on the center -- |sweep| is carried verbatim through the
        assembly, and an arc's endpoints re-chain from the drifted centers,
        so the center is the stable identity.
        """
        width = stroke_widths.get(segment_width_key(segment))
        if width is not None:
            return width
        offsets = (-1, 0, 1)
        candidates: set[float] = set()
        probed: WidthKey
        if isinstance(segment, ArcSegment):
            center = (int(round(segment.center.x)), int(round(segment.center.y)))
            for drift in range(3):
                candidates = set()
                for dx in offsets:
                    for dy in offsets:
                        if abs(dx) + abs(dy) != drift:
                            continue
                        probed = arc_width_key(
                            (center[0] + dx, center[1] + dy), segment.sweep_angle
                        )
                        found = stroke_widths.get(probed)
                        if found is not None:
                            candidates.add(found)
                if candidates:
                    return candidates.pop() if len(candidates) == 1 else default_width
            return default_width
        start = (int(round(segment.start.x)), int(round(segment.start.y)))
        end = (int(round(segment.end.x)), int(round(segment.end.y)))
        for drift in range(5):
            candidates = set()
            for dx_start in offsets:
                for dy_start in offsets:
                    for dx_end in offsets:
                        for dy_end in offsets:
                            if abs(dx_start) + abs(dy_start) + abs(dx_end) + abs(dy_end) != drift:
                                continue
                            probed = line_width_key(
                                (start[0] + dx_start, start[1] + dy_start),
                                (end[0] + dx_end, end[1] + dy_end),
                            )
                            found = stroke_widths.get(probed)
                            if found is not None:
                                candidates.add(found)
            if candidates:
                return candidates.pop() if len(candidates) == 1 else default_width
        return default_width

    def _layers_by_width(
        segments: List[Segment],
        default_width: float,
        stroke_widths: Optional[Mapping[WidthKey, float]],
        *,
        alpha: float,
        facecolor: str,
        label: str,
    ) -> List[PhysicalStrokeLayer]:
        """Split parsed segments into consecutive constant-width layers.

        Each segment draws at its mapped engraved width (a ``<cb:D>`` span),
        falling back to ``default_width`` (the pen/file width). Consecutive
        segments sharing a width group into one layer; a pen-down run crossing
        a width boundary splits into two corridors whose round caps overlap at
        the junction. Without a width map the whole segment list is one layer
        (the historical uniform per-pen render, bit-identical).
        """
        if not stroke_widths:
            return [
                PhysicalStrokeLayer(
                    segments=segments,
                    width_inches=default_width,
                    alpha=alpha,
                    label=label,
                    facecolor=facecolor,
                )
            ]
        grouped: List[Tuple[float, List[Segment]]] = []
        for segment in segments:
            width = _lookup_width(stroke_widths, segment, default_width)
            if grouped and grouped[-1][0] == width:
                grouped[-1][1].append(segment)
            else:
                grouped.append((width, [segment]))
        return [
            PhysicalStrokeLayer(
                segments=group,
                width_inches=width,
                alpha=alpha,
                label=label,
                facecolor=facecolor,
            )
            for width, group in grouped
        ]

    for plt_path in result.plt_paths:
        document = parser.parse_file(plt_path)
        pdf_path = pdf_dir / f"{plt_path.stem}.pdf"
        # File-name shape: [<plate>_][<material>_]<cutter>[_<color>]_<kind>_
        # <job_id>; the bh (borders + holes) kind is purely structural, text
        # is not.
        is_structural = _is_structural_stem(plt_path.stem)
        title = (
            _build_plot_title(job_name, plt_path, text_height=text_height)
            if job_name
            else "PLT Toolpath Visualization"
        )
        width_inches = _stem_cutter_inches(plt_path.stem)
        if width_inches is not None and file_widths is not None:
            # The stem token is the PHYSICAL tool; the preview must draw the
            # width it engraves (tool + kerf buffer), which the export tracked
            # per written file.
            width_inches = file_widths.get(plt_path, width_inches)
        if width_inches is not None:
            # Single-tool file: every stroke engraves at the file's cutter,
            # except the strokes the per-stroke width map covers (a <cb:D>
            # span sharing this file's pen).
            plot_physical_strokes(
                _layers_by_width(
                    _segments_of(document),
                    width_inches,
                    file_stroke_widths.get(plt_path) if file_stroke_widths else None,
                    alpha=PHYSICAL_STRUCTURAL_ALPHA if is_structural else 1.0,
                    facecolor="black",
                    label=plt_path.stem,
                ),
                output_path=pdf_path,
                title=title,
                show_plot=False,
            )
        else:
            plot_plt_document(
                document,
                output_path=pdf_path,
                title=title,
                show_plot=False,
                simple_mode=True,
                is_structural=is_structural,
            )
        pdf_paths.append(pdf_path)

    def _plate_prefix(plate_no: int) -> str:
        """The file-name prefix of one plate's combined artifacts."""
        return _format_plate_prefix(
            plate_no,
            combined_counts.get(plate_no, len(result.combined_by_plate)),
            result.material_by_plate.get(plate_no),
        )

    def _combined_proof(
        plate_no: int,
        content: str,
        pdf_path: Path,
        title: str,
        overlay_boxes: Sequence[JobOverlayBoxLike],
    ) -> None:
        """Render one combined (all-pens) plate proof and record it.

        Args:
            plate_no: 1-based plate number, selecting the material and the
                per-stroke width map.
            content: The combined PLT content to draw (a whole sheet, or one
                job's labels on it for a per-job proof).
            pdf_path: Where the proof is written.
            title: The plot title.
            overlay_boxes: Per-job boundary boxes drawn over the ink (see
                :func:`plt_optimizer.generate.jobset.job_overlay_boxes`). Empty
                for a solo sheet and for the per-job proofs, which show one
                job's labels only.
        """
        parsed_material = parse_material_colors(result.material_by_plate.get(plate_no))
        # Material-agnostic (or unrecognized-name) plates proof as "wk"
        # stock: white label background, black text, black outlines.
        material_colors = (
            parsed_material if parsed_material.background is not None else DEFAULT_MATERIAL_COLORS
        )
        layers: list[PhysicalStrokeLayer] = []
        fills: list[PhysicalFillLayer] = []
        if pen_widths:
            # The assembly keeps its SP pen sections, so each pen's strokes
            # can be extracted and drawn at its own cutter width.
            for pen_id in sorted(pen_widths):
                pen_content = extract_pens_from_plt_text(content, [pen_id])
                if not plt_has_geometry(pen_content):
                    continue
                pen_document = parser.parse_string(pen_content)
                pen_segments = _segments_of(pen_document)
                # Structural pens (borders SP2, drill holes SP3) render as
                # OPAQUE contrast outlines sharing the label border's color;
                # the holes additionally punch the preview background color
                # (the "see-through" disc added to `fills` below).
                is_boundary = pen_id == LAYER_BOUNDARY
                is_hole = pen_id == LAYER_HOLES
                structural = is_boundary or is_hole
                layers.extend(
                    _layers_by_width(
                        pen_segments,
                        pen_widths[pen_id],
                        plate_stroke_widths.get(plate_no) if plate_stroke_widths else None,
                        alpha=1.0,
                        # Contrast outline for the structural pens: the
                        # label border AND its drill holes share it --
                        # black on white stock, white over every other
                        # material background.
                        facecolor=(
                            outline_color_for(material_colors.background)
                            if structural
                            else resolve_text_color(
                                pen_colors.get(pen_id) if pen_colors else None,
                                material_colors,
                            )
                        ),
                        label=f"pen {pen_id}",
                    )
                )
                if is_boundary and material_colors.background is not None:
                    # The label boundary rectangles enclose the material
                    # background (always set: the wk default fills in for
                    # material-agnostic plates): fill them under the ink.
                    polygons = closed_path_polygons(pen_segments)
                    if polygons:
                        fills.append(
                            PhysicalFillLayer(
                                polygons=polygons,
                                color=material_colors.background,
                                label="material background",
                            )
                        )
                elif is_hole:
                    # The hole circles enclose removed material: punch them
                    # with the preview background color. Appended after the
                    # background fill (pens are walked in SP order) so the
                    # disc sits on top of it.
                    polygons = closed_path_polygons(pen_segments)
                    if polygons:
                        fills.append(
                            PhysicalFillLayer(
                                polygons=polygons,
                                color=preview_background,
                                label="drill hole",
                            )
                        )
        if layers:
            plot_physical_strokes(
                layers,
                output_path=pdf_path,
                title=title,
                show_plot=False,
                gridlines=False,
                fills=fills,
                background_color=preview_background,
                overlay_boxes=overlay_boxes,
            )
        else:
            # No pen widths (synthetic export) or no per-pen geometry:
            # historical thin centerline preview.
            document = parser.parse_string(content)
            plot_plt_document(
                document,
                output_path=pdf_path,
                title=title,
                show_plot=False,
                simple_mode=True,
                gridlines=False,
            )
        pdf_paths.append(pdf_path)

    for plate_no, combined in result.combined_by_plate.items():
        _combined_proof(
            plate_no,
            combined,
            pdf_dir / f"{_plate_prefix(plate_no)}all_{job_id}.pdf",
            f"{job_name} (plate {plate_no})" if job_name else "PLT Toolpath Visualization",
            overlays.get(plate_no, ()),
        )

    # Per-job proofs of the SHARED sheets: every contributing job gets an
    # all-pens view of its own labels on the plate it shares, so a job can be
    # approved on its own even though the machine runs one merged toolpath for
    # all of them. A sheet only one job occupies is skipped - its whole-sheet
    # proof above already is that job's proof.
    for plate_no, per_job in sorted(result.combined_by_plate_and_job.items()):
        # Only the owners with ink on THIS sheet count as sharing it: the map
        # carries an entry per job per plate, and a job packed entirely onto
        # another sheet contributes empty content here.
        contributors = {owner: content for owner, content in per_job.items() if content.strip()}
        if len(contributors) < 2:
            # A solo sheet is already proved by its whole-sheet proof above. A
            # second identical PDF - titled "sheet shared with" the one job on
            # it - is noise in the shop's proof folder and false on the sheet a
            # human signs, so a sheet one job occupies is skipped (as promised
            # above).
            continue

        def _display(owner: str) -> str:
            """This job's legend text (declared name, falling back to its id)."""
            record = provenance.record(owner) if provenance is not None else None
            return record.display_name if record is not None else owner

        shared = ", ".join(_display(owner) for owner in sorted(contributors))
        for owner, content in sorted(contributors.items()):
            record = provenance.record(owner) if provenance is not None else None
            member_id = record.job_id if record is not None else owner
            _combined_proof(
                plate_no,
                content,
                pdf_dir / f"{_plate_prefix(plate_no)}all_{member_id}.pdf",
                f"{_display(owner)} (plate {plate_no}, sheet shared with {shared})",
                (),
            )

    return pdf_paths


def write_default_plots(
    output_dir: Path,
    job_id: str,
    result: PerCutterExport,
    job_name: str = "",
    text_height: float | None = None,
    include_combined: bool = True,
) -> list[Path]:
    """Write color-coded ``*_default.pdf`` diagnostic plots for an export.

    Renders the plotter's default (color-coded, rapid-travel) view of
    every written per-cutter PLT as ``<plt-stem>_default.pdf`` plus one
    combined ``[<plate>_][<material>_]all_<job_id>_default.pdf`` per plate
    from the in-memory combined content (text + borders + holes together;
    skipped when ``include_combined`` is False). These diagnostics are
    strictly opt-in; the simple-outline previews remain the standard
    output.

    matplotlib is imported lazily through the plotter module so headless
    optimizer-only environments never pay the import cost.

    Args:
        output_dir: Base output directory (``pdf/`` is created inside).
        job_id: Job identifier used in combined PDF names.
        result: The export result whose ``plt_paths`` and
            ``combined_by_plate`` drive the plots.
        job_name: Human-readable job name for plot titles (optional).
        text_height: Text height in inches for text plot titles (optional).
        include_combined: When True (the default), also render the
            combined per-plate default plot (text + borders + holes
            together). Set False to produce only the per-cutter plots.

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
        title = (
            _build_plot_title(job_name, plt_path, text_height=text_height)
            if job_name
            else "PLT Toolpath Visualization"
        )
        plot_plt_document(
            document,
            output_path=pdf_path,
            title=title,
            show_plot=False,
            simple_mode=False,
        )
        pdf_paths.append(pdf_path)

    if include_combined:
        combined_counts = _material_plate_counts(result.material_by_plate)
        for plate_no, combined in result.combined_by_plate.items():
            document = parser.parse_string(combined)
            prefix = _format_plate_prefix(
                plate_no,
                combined_counts.get(plate_no, len(result.combined_by_plate)),
                result.material_by_plate.get(plate_no),
            )
            pdf_path = pdf_dir / f"{prefix}all_{job_id}_default.pdf"
            title = f"{job_name} (plate {plate_no})" if job_name else "PLT Toolpath Visualization"
            plot_plt_document(
                document,
                output_path=pdf_path,
                title=title,
                show_plot=False,
                simple_mode=False,
            )
            pdf_paths.append(pdf_path)

    return pdf_paths
