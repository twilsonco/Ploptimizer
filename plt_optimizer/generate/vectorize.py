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

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Optional, Sequence

if TYPE_CHECKING:
    from plt_optimizer.generate.job_config import JobConfig
    from plt_optimizer.generate.schema import JobSpec

from plt_optimizer.core.optimizer import OptimizationStrategy
from plt_optimizer.generate.label_renderer import (
    PLT_FOOTER,
    PLT_HEADER,
    RenderedLabel,
    extract_bounds_from_plt,
)
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
    TextColor,
    material_key,
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
            :attr:`~plt_optimizer.generate.layout.PackedPlate.material`).
    """

    plt_paths: list[Path] = field(default_factory=list)
    pdf_paths: list[Path] = field(default_factory=list)
    combined_by_plate: dict[int, str] = field(default_factory=dict)
    output_dir: Path = field(default_factory=Path)
    job_id: str = "job"
    default_pdf_paths: list[Path] = field(default_factory=list)
    rendered_labels: dict[str, RenderedLabel] = field(default_factory=dict)
    material_by_plate: dict[int, Optional[str]] = field(default_factory=dict)


def _format_cutter(cutter_diameter: float) -> str:
    """Format a cutter diameter for file names (3-decimal inches).

    Args:
        cutter_diameter: Cutter diameter in inches.

    Returns:
        Fixed-precision string, e.g. ``0.03`` -> ``"0.030"``.
    """
    return f"{cutter_diameter:.3f}"


def _format_text_layer(cutter_diameter: float, text_color: str) -> str:
    """Format a ``(cutter, color)`` text layer for file names.

    The implicit ``"none"`` color keeps the historical cutter-only name
    (bit-identical output for color-less jobs); a real stroke color gains
    its single-letter tag so color-split layers never collide
    (``0.04`` + ``magenta`` -> ``"0.040_m"``).

    Args:
        cutter_diameter: Cutter diameter in inches.
        text_color: Resolved stroke-color value (``"none"`` or a
            :class:`~plt_optimizer.generate.schema.TextColor` value).

    Returns:
        File-name component, e.g. ``"0.030"`` or ``"0.030_m"``.
    """
    cutter_str = _format_cutter(cutter_diameter)
    if text_color == TextColor.NONE.value:
        return cutter_str
    try:
        abbreviation = TextColor(text_color).abbreviation
    except ValueError:
        # Unknown colors (manually constructed labels) fall back to the
        # full sanitized value so file names stay filesystem-safe and can
        # never collide with a real color's 1-letter abbreviation.
        abbreviation = re.sub(r"[^0-9a-zA-Z]", "", text_color) or "x"
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

    Material names are free-form (``"wb"``, ``"wb(uv)"``), so every
    non-alphanumeric character is stripped to keep names filesystem-safe
    (``"wb(uv)"`` -> ``"wbuv"``). A name that sanitizes to nothing (e.g.
    ``"---"``) falls back to ``"x"`` so the tag is never empty.

    Args:
        material: The plate's resolved material name, or ``None``
            (material-agnostic).

    Returns:
        File-name component, or ``""`` when ``material`` is ``None``.
    """
    if material is None:
        return ""
    return re.sub(r"[^0-9a-zA-Z]", "", material) or "x"


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
        ``"wbuv_"`` or ``"02_wbuv_"``.
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

# Cutter file-name token: fixed 3-decimal inches (see _format_cutter).
_CUTTER_TOKEN_RE = re.compile(r"^\d+\.\d{3,}$")


@dataclass(frozen=True)
class _StemParts:
    """Parsed components of a per-cutter PLT file-name stem.

    Attributes:
        plate: Plate-number token (``"02"``), or ``None`` when the plate's
            material spans a single sheet.
        material: Material tag token (``"wbuv"``), or ``None``.
        cutter: Cutter-diameter token (``"0.040"``), or ``None``.
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
    ``[<plate>_][<material>_]<cutter>[_<color>]_<kind>_<job_id>``. Parsing
    anchors on the cutter token (the only dotted numeric token) because
    ``job_id`` may itself contain underscores, and the plate number is
    optional:

    - cutter at index 0 -> no plate, no material;
    - cutter at index 1 -> the leading token is the plate when it is all
      digits (the plate number is zero-padded), otherwise the material;
    - cutter at index >= 2 -> index 0 is the plate, the tokens between are
      the material.

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
    cutter_index = next(
        (index for index, token in enumerate(tokens[:kind_index]) if _CUTTER_TOKEN_RE.match(token)),
        None,
    )
    if cutter_index is None:
        return _StemParts(kind=tokens[kind_index])
    if cutter_index >= 2:
        plate: Optional[str] = tokens[0]
        material: Optional[str] = "_".join(tokens[1:cutter_index])
    elif cutter_index == 1 and tokens[0].isdigit():
        plate, material = tokens[0], None
    elif cutter_index == 1:
        plate, material = None, tokens[0]
    else:
        plate, material = None, None
    color_index = cutter_index + 1
    return _StemParts(
        plate=plate,
        material=material,
        cutter=tokens[cutter_index],
        kind=tokens[kind_index],
        color=tokens[color_index] if color_index < kind_index else None,
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
        when present, is included as ``" [wbuv]"``.
    """
    parts = _parse_plt_stem(plt_path.stem)
    if parts.kind is None:
        # Fallback for unexpected format
        return f"{job_name} {plt_path.stem}"

    material_suffix = f" [{parts.material}]" if parts.material else ""
    if parts.kind == "bh":
        return f"{job_name} borders and holes ({parts.cutter} cutter){material_suffix}"
    text_height_str = f"{text_height:.3g}" if text_height is not None else ""
    if text_height_str:
        return f"{job_name} {text_height_str} text ({parts.cutter} cutter){material_suffix}"
    return f"{job_name} text ({parts.cutter} cutter){material_suffix}"


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
      e.g. ``wbuv_0.040_m_txt_job.plt`` for magenta). A text cutter equal
      to the boundary/hole cutter still gets its own file (separate run).
      Lines sharing a cutter but differing in ``text_color`` split into
      separate files so the cutter depth can change between runs (3-layer
      material).
    - The leading ``[<plate>_][<material>_]`` prefix is built by
      :func:`_format_plate_prefix`: the 1-based packing-order plate number
      (zero-padded to two digits) is **omitted whenever the plate's
      material group spans a single sheet**, so its presence signals that
      *this material* needs more than one plate; the sanitized material tag
      (e.g. ``wb(uv)`` -> ``wbuv``) is omitted for material-agnostic
      plates. Material-agnostic plates form one group, so multi-sheet
      material-less jobs always keep their plate numbers. The cutter
      precedes the kind token so the tool size is visible before the
      text/structural distinction.
    - The combined per-plate PLT is assembled **in memory only** (never
      written) and exposed via :attr:`PerCutterExport.combined_by_plate`;
      when ``plots`` is enabled it also drives the combined
      ``[<plate>_][<material>_]all_<job_id>.pdf`` preview.

    PLT files are written under ``output_dir/plt/`` and PDFs under
    ``output_dir/pdf/``. Cutter diameters are formatted with 3 decimals
    (inches). Empty pen groups are skipped.

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
            downward (the historical behaviour). Individual plates may
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

    Returns:
        A :class:`PerCutterExport` with written PLT paths, PDF paths, and
        the in-memory combined content per plate.
    """
    from plt_optimizer.generate.layout import generate_layout_with_bounds

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # No plates means unbounded mode: the layout engine auto-allocates
    # default sheets sized by ``default_plate_size`` (from job-config.json)
    # and overflows onto as many as the labels need. Passing ``None``
    # through (rather than synthesizing a single plate) keeps this path
    # identical to ``generate_layout``'s own unbounded behaviour.
    if provided_plates is not None and len(provided_plates) == 0:
        provided_plates = None

    # Compression-driven cutter reduction (pre-pass): render-measure the
    # labels once and downsize the *automatic* cutters of compressed lines
    # BEFORE the pen map / packing / rendering consume them, so the per-cutter
    # layers, PLT file names, kerning clearance and collision stroke floor all
    # see the final tools. No-op (same list) without an inventory.
    from plt_optimizer.generate.cutter_downsize import apply_compression_cutter_downsize

    resolved_labels = apply_compression_cutter_downsize(resolved_labels, available_cutters)

    # Pen == (cutter, color): assign one HPGL pen per distinct text
    # cutter/color layer so the assembled plate can be split into
    # per-layer files after assembly.
    pen_map = build_cutter_pen_map(resolved_labels)
    layer_by_pen = {pen: layer for layer, pen in pen_map.items()}

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
        default_plate_size=default_plate_size,
        default_plate_clearance=default_plate_clearance,
    )

    plt_dir = output_dir / "plt"
    plt_dir.mkdir(parents=True, exist_ok=True)

    result = PerCutterExport(
        output_dir=output_dir.resolve(),
        job_id=job_id,
        rendered_labels=rendered_labels_map,
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

            # Prepend tool option headers if job_spec and job_config are provided
            if job_spec is not None and job_config is not None:
                from plt_optimizer.generate.tool_options import prepend_tool_option_headers

                bh_content = prepend_tool_option_headers(
                    bh_content, job_spec, job_config, "borders_holes"
                )

            structure_path = plt_dir / f"{prefix}{_format_cutter(hole_cutter)}_bh_{job_id}.plt"
            structure_path.write_text(bh_content + "\n", encoding="utf-8")
            result.plt_paths.append(structure_path)

        # Text group: one file per distinct text cutter/color pen with
        # content. With optimization enabled the layer is routed directly
        # from the rendered chunk records in plate space (parser/profiler
        # skipped); otherwise the pen layer is extracted from the
        # assembly as-is.
        for pen_id in sorted(layer_by_pen):
            cutter, text_color = layer_by_pen[pen_id]
            layer_tag = _format_text_layer(cutter, text_color)
            written_content: Optional[str] = None
            if optimize and strategy_factory is not None:
                optimization = optimize_text_layer(
                    plate.labels,
                    rendered_labels_map,
                    pen_id,
                    strategy_factory,
                    logger=logger,
                    log_prefix=f"[plate {plate_str} text {layer_tag}]",
                )
                if optimization is not None:
                    written_content = optimization.content
                    _report(f"{plate_str} text {layer_tag}", optimization)
            if written_content is None:
                text_content = extract_pens_from_plt_text(combined, [pen_id])
                if not plt_has_geometry(text_content):
                    continue
                written_content = text_content

            # Prepend tool option headers if job_spec and job_config are provided
            if job_spec is not None and job_config is not None:
                from plt_optimizer.generate.tool_options import prepend_tool_option_headers

                written_content = prepend_tool_option_headers(
                    written_content, job_spec, job_config, "text"
                )

            text_path = plt_dir / f"{prefix}{layer_tag}_txt_{job_id}.plt"
            text_path.write_text(written_content + "\n", encoding="utf-8")
            result.plt_paths.append(text_path)

    # Extract text height from resolved labels for plot titles
    text_height: float | None = None
    if resolved_labels and resolved_labels[0].content:
        # Use the nominal text height from the first text line of the first label
        text_height = resolved_labels[0].content[0].nominal_text_height

    if plots:
        result.pdf_paths = _write_simple_plots(
            output_dir, job_id, result, job_name=job_name, text_height=text_height
        )
    if default_plots:
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
) -> list[Path]:
    """Write simple-outline PDF previews for a per-cutter export.

    Parses every written PLT and renders a simple-mode (black cutting
    lines only) PDF into ``output_dir/pdf/`` mirroring the PLT file names.
    Additionally renders one combined ``[<plate>_][<material>_]all_<job_id>.pdf``
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
        job_name: Human-readable job name for plot titles (optional).
        text_height: Text height in inches for text plot titles (optional).

    Returns:
        List of written PDF paths.
    """
    from plt_optimizer.core.parser import PLTParser
    from plt_optimizer.diagnostics.plotter import plot_plt_document

    pdf_dir = output_dir / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    parser = PLTParser()
    pdf_paths: list[Path] = []
    combined_counts = _material_plate_counts(result.material_by_plate)

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
        plot_plt_document(
            document,
            output_path=pdf_path,
            title=title,
            show_plot=False,
            simple_mode=True,
            is_structural=is_structural,
        )
        pdf_paths.append(pdf_path)

    for plate_no, combined in result.combined_by_plate.items():
        document = parser.parse_string(combined)
        prefix = _format_plate_prefix(
            plate_no,
            combined_counts.get(plate_no, len(result.combined_by_plate)),
            result.material_by_plate.get(plate_no),
        )
        pdf_path = pdf_dir / f"{prefix}all_{job_id}.pdf"
        # Combined plots mix text + borders + holes: thin opaque strokes.
        title = (
            f"{job_name} combined view (plate {plate_no})"
            if job_name
            else "PLT Toolpath Visualization"
        )
        plot_plt_document(
            document,
            output_path=pdf_path,
            title=title,
            show_plot=False,
            simple_mode=True,
        )
        pdf_paths.append(pdf_path)

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
            title = (
                f"{job_name} combined view (plate {plate_no})"
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

    return pdf_paths
