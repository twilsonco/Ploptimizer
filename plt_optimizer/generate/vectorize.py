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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, List, Mapping, Optional, Sequence

if TYPE_CHECKING:
    from plt_optimizer.generate.job_config import JobConfig
    from plt_optimizer.generate.schema import JobSpec

from plt_optimizer.core.filenames import ensure_plot_filename_length
from plt_optimizer.core.models import PLTDocument, Segment
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

# Cutter file-name token: integer thousandths of an inch (see _format_cutter).
_CUTTER_TOKEN_RE = re.compile(r"^\d+$")


@dataclass(frozen=True)
class _StemParts:
    """Parsed components of a per-cutter PLT file-name stem.

    Attributes:
        plate: Plate-number token (``"02"``), or ``None`` when the plate's
            material spans a single sheet.
        material: Material tag token (``"wbuv"``), or ``None``.
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
    index 1 -> the leading token is the plate when it is all digits, else
    the material; index >= 2 -> index 0 is the plate and the tokens between
    are the material.

    Known limitations (both degrade gracefully to the thin-line plot /
    fallback title): a stem shortened by the 39-character filename cap can
    lose trailing cutter digits (``187`` -> ``18``), and an unknown numeric
    color tag (``40_123_txt_job``) reads as the cutter. ``job_id`` may
    itself contain underscores; the kind token is the anchor that avoids it.

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
    if cutter_index >= 2:
        plate: Optional[str] = tokens[0]
        material: Optional[str] = "_".join(tokens[1:cutter_index])
    elif cutter_index == 1 and tokens[0].isdigit():
        plate, material = tokens[0], None
    elif cutter_index == 1:
        plate, material = None, tokens[0]
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
        when present, is included as ``" [wbuv]"``. The cutter is rendered in
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
      e.g. ``wbuv_40_m_txt_job.plt`` for magenta). A text cutter equal
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
        stage_timings: Optional collector that receives the per-stage
            wall-clock timings directly. The stage timers normally report
            through an INFO log record, which the logging level gates; a
            consumer that needs the numbers regardless of the log level
            (the generation report) passes a collector here.

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
    # identical to ``generate_layout``'s own unbounded behaviour.
    if provided_plates is not None and len(provided_plates) == 0:
        provided_plates = None

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
    # per-layer files after assembly.
    pen_map = build_cutter_pen_map(resolved_labels)
    layer_by_pen = {pen: layer for layer, pen in pen_map.items()}

    # Job-level boundary/hole cutter (all labels share the job-resolved
    # value; fall back to the default for content-less jobs).
    hole_cutter = max(
        (label.hole_cutter_diameter for label in resolved_labels),
        default=DEFAULT_BOUNDARY_HOLE_CUTTER,
    )

    # Pen -> physical cutter diameter (inches): the true stroke width each
    # pen engraves with. Text pens come from the (cutter, color) pen map;
    # the structural pens (borders, holes) all share the job hole cutter.
    # The simple physical-width plots consume this to split the in-memory
    # combined plate by pen and draw every stroke at its real width.
    pen_widths: dict[int, float] = {pen: cutter for pen, (cutter, _color) in layer_by_pen.items()}
    pen_widths[LAYER_BOUNDARY] = hole_cutter
    pen_widths[LAYER_HOLES] = hole_cutter

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
        # Structural group: borders (SP2) + holes (SP3) share one run.
        with stage_timer("assemble_split", _LOG, timings=stage_timings):
            combined = assemble_plt_from_rendered_labels(plate, rendered_labels_map)
            structure_content = extract_pens_from_plt_text(combined, [LAYER_BOUNDARY, LAYER_HOLES])
        result.combined_by_plate[plate_no] = combined
        if plt_has_geometry(structure_content):
            bh_content = structure_content
            if optimize and strategy_factory is not None:
                with stage_timer("optimize_layer", _LOG, timings=stage_timings):
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
            # Plotter-controller cap: the machine rejects names longer than
            # 39 characters, so the name is shortened from the right (the
            # mirrored PDF previews inherit the shortened stem).
            structure_path = ensure_plot_filename_length(structure_path, _LOG)
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
                with stage_timer("optimize_layer", _LOG, timings=stage_timings):
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
                with stage_timer("assemble_split", _LOG, timings=stage_timings):
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
            # Plotter-controller cap (see the structural write above).
            text_path = ensure_plot_filename_length(text_path, _LOG)
            text_path.write_text(written_content + "\n", encoding="utf-8")
            result.plt_paths.append(text_path)

    # Extract text height from resolved labels for plot titles
    text_height: float | None = None
    if resolved_labels and resolved_labels[0].content:
        # Use the nominal text height from the first text line of the first label
        text_height = resolved_labels[0].content[0].nominal_text_height

    if plots:
        with stage_timer("plots", _LOG, timings=stage_timings):
            result.pdf_paths = _write_simple_plots(
                output_dir,
                job_id,
                result,
                job_name=job_name,
                text_height=text_height,
                pen_widths=pen_widths,
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
) -> list[Path]:
    """Write true-stroke-width PDF previews for a per-cutter export.

    Parses every written PLT and renders a physical-width PDF into
    ``output_dir/pdf/`` mirroring the PLT file names: every cutting stroke
    is expanded into the ink corridor its cutter engraves (see
    :func:`~plt_optimizer.diagnostics.plotter.plot_physical_strokes`), so
    each stroke measures its real cutter diameter on the inch grid. The
    per-file width comes from the cutter token of the file-name stem; the
    structural (``bh`` = borders + holes) layers keep the semi-transparent
    underlay look while text ink renders fully opaque.

    Additionally renders one combined ``[<plate>_][<material>_]all_<job_id>.pdf``
    per plate from the in-memory combined content (text + borders + holes
    together). When ``pen_widths`` is provided, the combined content is
    split per pen (the assembly keeps its ``SP`` sections) and every pen
    draws at its own cutter width; without it the combined plot falls back
    to the thin centerline preview.

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
        pen_widths: Physical cutter diameter (inches) per HPGL pen number
            of the assembled plate (text pens from the pen map, structural
            pens 2/3 at the job hole cutter). Enables per-stroke widths in
            the combined plots. ``None`` renders combined plots thin.

    Returns:
        List of written PDF paths.
    """
    from plt_optimizer.core.parser import PLTParser
    from plt_optimizer.diagnostics.plotter import (
        PHYSICAL_STRUCTURAL_ALPHA,
        PhysicalStrokeLayer,
        plot_physical_strokes,
        plot_plt_document,
    )

    pdf_dir = output_dir / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    parser = PLTParser()
    pdf_paths: list[Path] = []
    combined_counts = _material_plate_counts(result.material_by_plate)

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
        if width_inches is not None:
            # Single-tool file: every stroke engraves at the file's cutter.
            plot_physical_strokes(
                [
                    PhysicalStrokeLayer(
                        segments=_segments_of(document),
                        width_inches=width_inches,
                        alpha=PHYSICAL_STRUCTURAL_ALPHA if is_structural else 1.0,
                        label=plt_path.stem,
                    )
                ],
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

    for plate_no, combined in result.combined_by_plate.items():
        prefix = _format_plate_prefix(
            plate_no,
            combined_counts.get(plate_no, len(result.combined_by_plate)),
            result.material_by_plate.get(plate_no),
        )
        pdf_path = pdf_dir / f"{prefix}all_{job_id}.pdf"
        title = (
            f"{job_name} combined view (plate {plate_no})"
            if job_name
            else "PLT Toolpath Visualization"
        )
        layers: list[PhysicalStrokeLayer] = []
        if pen_widths:
            # The assembly keeps its SP pen sections, so each pen's strokes
            # can be extracted and drawn at its own cutter width.
            for pen_id in sorted(pen_widths):
                pen_content = extract_pens_from_plt_text(combined, [pen_id])
                if not plt_has_geometry(pen_content):
                    continue
                pen_document = parser.parse_string(pen_content)
                layers.append(
                    PhysicalStrokeLayer(
                        segments=_segments_of(pen_document),
                        width_inches=pen_widths[pen_id],
                        alpha=(
                            PHYSICAL_STRUCTURAL_ALPHA
                            if pen_id in (LAYER_BOUNDARY, LAYER_HOLES)
                            else 1.0
                        ),
                        label=f"pen {pen_id}",
                    )
                )
        if layers:
            plot_physical_strokes(
                layers,
                output_path=pdf_path,
                title=title,
                show_plot=False,
                gridlines=False,
            )
        else:
            # No pen widths (synthetic export) or no per-pen geometry:
            # historical thin centerline preview.
            document = parser.parse_string(combined)
            plot_plt_document(
                document,
                output_path=pdf_path,
                title=title,
                show_plot=False,
                simple_mode=True,
                gridlines=False,
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
