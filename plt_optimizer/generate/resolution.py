"""Resolution engine for flattening JobSpec into strictly typed label objects.

This module bridges the gap between the flexible, highly optional Pydantic
``JobSpec`` (which accepts many omitted fields) and the strictly typed
data structures required by downstream consumers such as the 2D bin
packer. Every dimension, margin, and spacing value is absolutely resolved
by the time a ``ResolvedLabel`` is instantiated.

Resolution order for any given parameter:
    1. ``TextLine`` (most specific)
    2. ``LabelSpec``
    3. ``JobSpec``
    4. Hardcoded fallback constant (prevents ``NoneType`` math errors)

Example:
    >>> from plt_optimizer.generate.schema import parse_yaml
    >>> from plt_optimizer.generate.resolution import resolve_job_spec
    >>> job = parse_yaml("tests_deps/sample_spec.yaml")
    >>> labels = resolve_job_spec(job)
    >>> print(labels[0].width)
    3.0
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field, replace
from typing import Optional, Sequence, Union

from plt_optimizer.generate.font_registry import DEFAULT_FONT_NAME
from plt_optimizer.generate.schema import JobSpec, LabelSpec

logger = logging.getLogger(__name__)


class CutterSizeError(ValueError):
    """Raised when an explicit ``cutter_size`` cannot be applied to a line.

    The text toolpath height is ``text_height - cutter_size``; a cutter at
    or above the line's nominal text height leaves no material to engrave
    (a zero or negative toolpath height), so the job aborts instead of
    emitting degenerate glyphs.
    """


# ---------------------------------------------------------------------------
# Global fallback constants
# ---------------------------------------------------------------------------
DEFAULT_TEXT_HEIGHT: float = 0.25
# Font selected when ``font`` is unset at line/label/job level. The value
# must be a valid registry name (PLT-extracted key or Fonts/ TTF basename);
# see ``plt_optimizer.generate.font_registry``.
DEFAULT_FONT: str = DEFAULT_FONT_NAME
DEFAULT_MARGIN: float = 0.125
# Horizontal and vertical margins fall back to margin, then the default.
# Both are None to indicate "unset"; resolution logic fills them in.
DEFAULT_H_MARGIN: Optional[float] = None
DEFAULT_V_MARGIN: Optional[float] = None
DEFAULT_LINE_SPACING: Union[str, float] = "auto"
DEFAULT_HOLE_MARGIN: float = 0.1875
# Lower bound for hole-margin shrinkage during text-hole collision
# avoidance. ``None`` (the default) means collision avoidance may reduce
# the hole margin all the way to ``0.0`` (hole tangent to the edge).
DEFAULT_MIN_HOLE_MARGIN: Optional[float] = None
# Air gap (inches) kept between the engraved text stroke and the engraved
# drill-hole stroke on top of the stroke floor
# ``0.5 * (hole_cutter + text_cutter)``. At 0.0 the two cut strokes just
# touch; the default keeps 0.15in of free air between them.
DEFAULT_HOLE_TEXT_COLLISION_DISTANCE: float = 0.15
# Default cutter diameter (inches) used to cut label boundaries and drill
# holes. Collision detection uses this to compute the stroke floor; the
# effective value is snapped to the shop inventory (next size down, else
# next size up).
DEFAULT_BOUNDARY_HOLE_CUTTER: float = 0.015
# Horizontal compression is opt-in: 0.0 disables it entirely.
DEFAULT_MAX_H_COMPRESS: float = 0.0
# Space advance (PLT-extracted fonts) as a fraction of the rendered text
# height: a space advances ``DEFAULT_SPACE_WIDTH_FRACTION * text_height +
# character_spacing``.
DEFAULT_SPACE_WIDTH_FRACTION: float = 0.3
# Global minimum glyph advance width (inches) for PLT-extracted fonts.
# The profile-envelope kerning floors each pair's advance at this value,
# capped by the left glyph's own width; 0.0 means pure envelope kerning.
DEFAULT_MIN_GLYPH_WIDTH: float = 0.0
# Kerning window (fraction of the rendered text height, in [0.0, 1.0])
# for PLT-extracted fonts. Each envelope sample compares against the
# opposite silhouette within +/- (half of this fraction) of the text
# height and the effective penetration is the worst windowed
# penetration, so staggered pokes widen the advance;
# 0.0 reproduces the historical same-height maximum-penetration kerning.
DEFAULT_KERNING_WINDOW_FRACTION: float = 0.05
# Multiplier on the detected windowed penetration (PLT-extracted fonts).
# 1.0 keeps the geometric penetration; >1.0 over-kerns tight pairs.
DEFAULT_KERNING_PENETRATION_SCALE: float = 1.0
# Multiplier on a *recessed* (negative) detected penetration (PLT-extracted
# fonts). 1.0 keeps the geometric recession (the historical linear
# behaviour); 0.0 makes a recessed pair advance by nothing beyond the
# min_glyph_width floor, so the penetration dial only tightens pairs whose
# silhouettes overlap.
DEFAULT_KERNING_RECESSION_SCALE: float = 1.0
# Extra air (inches) added to every kerned pair advance on top of the
# cutter + character-spacing clearance. 0.0 = no extra gap.
DEFAULT_KERNING_MIN_GAP: float = 0.0
# Multiplier on the bounding-box-width fallback advance (no y-overlap or
# missing envelopes). 1.0 = the left glyph's own width.
DEFAULT_FALLBACK_ADVANCE_FRACTION: float = 1.0
# Horizontal text alignment defaults to centering (existing behaviour).
DEFAULT_TEXT_H_ALIGNMENT: str = "center"
# Explicit cutter diameter (inches) override. ``None`` (the default) means
# unset: the cutter is auto-selected from the nominal text height via
# ``IDEAL_CUTTER_MAP`` (current behaviour). When set, the requested diameter
# is snapped to the shop inventory (next size down, else next size up) and
# the toolpath height is recomputed as ``text_height - cutter_size``.
# There is deliberately no job-config.json counterpart to this field.
DEFAULT_CUTTER_SIZE: Optional[float] = None
# Stroke-color layer tag default. ``"none"`` is the implicit color of
# every line that omits ``text_color``; it never cascades (the field is
# label/line-local by design, see schema.TextColor) and the resolution
# engine never reads a job-level value (the schema rejects one).
DEFAULT_TEXT_COLOR: str = "none"

# ---------------------------------------------------------------------------
# Cutter lookup table and inventory matching
# ---------------------------------------------------------------------------
# Maps nominal text height (inches) to the ideal cutter diameter (inches).
# Keys are stored as exact float representations of common fractions.
IDEAL_CUTTER_MAP: dict[float, float] = {
    0.0625: 0.005,  # 1/16
    0.09375: 0.01,  # 3/32
    0.125: 0.015,  # 1/8
    0.1875: 0.02,  # 3/16
    0.21875: 0.025,  # 7/32
    0.25: 0.03,  # 1/4
    0.3125: 0.04,  # 5/16
    0.375: 0.045,  # 3/8
    0.4375: 0.05,  # 7/16
    0.5: 0.06,  # 1/2
    0.625: 0.075,  # 5/8
    0.75: 0.09,  # 3/4
    1.0: 0.125,  # 1
    1.25: 0.15,  # 1-1/4
    1.375: 0.171,  # 1-3/8
    1.5: 0.187,  # 1-1/2
    1.75: 0.21,  # 1-3/4
    2.0: 0.25,  # 2
}


def get_cutter_diameter(
    nominal_height: float,
    available_inventory: Optional[list[float]] = None,
    tolerance_factor: float = 3.0,
) -> float:
    """Find the optimal cutter diameter, preferring a narrower tool.

    The logic defaults to a narrower cutter to prevent character bleeding,
    but switches to a wider cutter when the closest narrower tool exceeds
    the distance tolerance factor relative to the wider tool. This
    prevents using an impractically small tool that could result in
    illegible hairline text or unnecessary tool wear.

    Args:
        nominal_height: The nominal text height in inches.
        available_inventory: Optional list of cutter diameters available in
            the shop. If None or empty, the ideal cutter is returned.
        tolerance_factor: The multiplier used to decide between narrower and
            wider cutters. If the distance to the closest narrower cutter
            exceeds ``tolerance_factor`` times the distance to the closest
            wider cutter, the wider cutter is selected. Defaults to 3.0.

    Returns:
        The recommended cutter diameter in inches.
    """
    # 1. Find the ideal cutter from the lookup table
    closest_nominal = min(IDEAL_CUTTER_MAP.keys(), key=lambda k: abs(k - nominal_height))
    ideal_cutter = IDEAL_CUTTER_MAP[closest_nominal]

    # 2. If no inventory provided, return the ideal cutter
    if not available_inventory:
        return ideal_cutter

    # 3. Filter inventory into narrower (including exact match) and wider lists
    narrower_cutters = [c for c in available_inventory if c <= ideal_cutter]
    wider_cutters = [c for c in available_inventory if c > ideal_cutter]

    # 4. Handle edge cases where inventory is severely restricted
    if not narrower_cutters:
        return min(wider_cutters)  # Must use the smallest available wider cutter
    if not wider_cutters:
        return max(narrower_cutters)  # Must use the largest available narrower cutter

    # 5. Find the closest candidates
    closest_narrower = max(narrower_cutters)
    closest_wider = min(wider_cutters)

    # 6. Apply the threshold logic
    dist_narrower = ideal_cutter - closest_narrower
    dist_wider = closest_wider - ideal_cutter

    if dist_narrower > (tolerance_factor * dist_wider):
        return closest_wider
    else:
        return closest_narrower


def snap_boundary_hole_cutter(
    requested: float,
    available_inventory: Optional[list[float]] = None,
) -> float:
    """Snap the boundary/hole cutter size to the available tool inventory.

    The boundary/hole cutter (used for label borders and drill holes) is a
    single fixed tool, so selection is a plain snap rather than the
    tolerance-based text-cutter choice in :func:`get_cutter_diameter`: an
    exact (or equal) match is kept; otherwise the next size *down* is
    preferred, and only when no smaller tool exists the next size *up*.

    Args:
        requested: The requested cutter diameter in inches (e.g. from
            ``tools.json`` ``boundary_hole_cutter_size`` or the default).
        available_inventory: Optional list of cutter diameters available
            in the shop. If None or empty, ``requested`` is returned
            unchanged (the ideal tool is assumed available).

    Returns:
        The snapped cutter diameter in inches.
    """
    if not available_inventory:
        return requested

    narrower = [c for c in available_inventory if c <= requested]
    if narrower:
        return max(narrower)  # Exact match or next size down
    return min(available_inventory)  # No smaller tool: next size up


# ---------------------------------------------------------------------------
# Strictly typed target dataclasses
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ResolvedHoleSpec:
    """A fully resolved hole specification.

    Attributes:
        diameter: The diameter of the hole in inches.
        location: The position of the hole on the label edge (string).
    """

    diameter: float
    location: str


@dataclass(frozen=True)
class ResolvedTextLine:
    """A fully resolved text line with cutter compensation applied.

    Attributes:
        text: The actual text string to render.
        nominal_text_height: The requested font height in inches (before
            cutter compensation).
        toolpath_text_height: The actual toolpath height in inches (after
            subtracting cutter diameter). This is what vpype renders.
        cutter_diameter: The matched tool diameter in inches, used for
            kerf compensation and pre-job reporting.
        character_spacing: Extra spacing between characters in inches.
        line_spacing: Extra spacing between text lines in inches.
        max_h_compress: Maximum horizontal compression fraction in
            ``[0.0, 1.0]`` (cascaded line -> label -> job, default ``0.0``).
            When the rendered line is wider than the label's inner content
            area, the renderer may uniformly compress it horizontally down
            to ``(1 - max_h_compress)`` of its natural width. ``0.0``
            disables compression.
        text_h_alignment: Horizontal alignment of the rendered line within
            the label's inner content area (cascaded line -> label -> job,
            default ``"center"``). One of ``"left"``, ``"center"`` or
            ``"right"``. ``"left"`` places the line's left-most point
            precisely at the left margin; ``"right"`` places the right-most
            point precisely at the right margin.
        text_color: Stroke-color layer tag (``"none"`` default) splitting
            otherwise-identical text into separate toolpaths. Resolved
            line -> label -> default (deliberately NOT cascaded from the
            job; see :class:`~plt_optimizer.generate.schema.TextColor`).
            Consumed by the pen map so each ``(cutter, color)`` pair gets
            its own HPGL ``SP`` layer and PLT file.
        font: Canonical font name selecting the glyph outlines (cascaded
            line -> label -> job, default ``DEFAULT_FONT``). Either a
            PLT-extracted ``plt_fonts.json`` key (rendered arc-native) or a
            ``Fonts/`` TTF basename (rendered through ftext); resolve the
            kind via ``font_registry.resolve_font``.
        space_width_fraction: Space advance as a fraction of the rendered
            text height (PLT-extracted fonts): a space advances
            ``space_width_fraction * text_height + character_spacing``.
            Cascaded line -> label -> job, default
            ``DEFAULT_SPACE_WIDTH_FRACTION`` (0.3). Explicit ``0.0`` is
            honored; only ``None`` means unset.
        min_glyph_width: Global minimum glyph advance width in inches
            (PLT-extracted fonts): the profile-envelope kerning floors
            each pair's advance at this value, capped by the left glyph's
            own width, so zero-width glyphs still reserve real air without
            thin-glyph pairs being inflated past their own silhouette.
            Cascaded line ->
            label -> job, default ``DEFAULT_MIN_GLYPH_WIDTH`` (0.0 = pure
            envelope kerning). Explicit ``0.0`` is honored; only ``None``
            means unset.
        kerning_window_fraction: Kerning window as a fraction of the
            rendered text height in [0.0, 1.0] (PLT-extracted fonts):
            envelope samples compare against the opposite silhouette
            within +/- (half of this fraction) of the text height and the
            effective penetration is the worst windowed penetration, so
            staggered pokes widen the advance. Cascaded line -> label ->
            job, default ``DEFAULT_KERNING_WINDOW_FRACTION`` (0.05).
            Explicit ``0.0`` is honored (= historical same-height
            kerning); only ``None`` means unset.
        kerning_penetration_scale: Multiplier on the detected windowed
            penetration (PLT-extracted fonts): 1.0 keeps the geometric
            penetration, >1.0 over-kerns tight pairs. Cascaded line ->
            label -> job, default ``DEFAULT_KERNING_PENETRATION_SCALE``
            (1.0). Explicit ``0.0`` is honored; only ``None`` means unset.
        kerning_recession_scale: Multiplier on a *recessed* (negative)
            detected penetration (PLT-extracted fonts): 1.0 keeps the
            geometric recession, 0.0 makes a recessed pair advance by
            nothing beyond the ``min_glyph_width`` floor. Cascaded line ->
            label -> job, default ``DEFAULT_KERNING_RECESSION_SCALE``
            (1.0). Explicit ``0.0`` is honored; only ``None`` means unset.
        kerning_min_gap: Extra air in inches added to every kerned pair
            advance on top of the clearance (PLT-extracted fonts).
            Cascaded line -> label -> job, default
            ``DEFAULT_KERNING_MIN_GAP`` (0.0). Explicit ``0.0`` is honored;
            only ``None`` means unset.
        fallback_advance_fraction: Multiplier on the bounding-box-width
            fallback advance for pairs without overlapping height (or
            lacking envelopes). Cascaded line -> label -> job, default
            ``DEFAULT_FALLBACK_ADVANCE_FRACTION`` (1.0). Explicit ``0.0``
            is honored; only ``None`` means unset.
        cutter_size: The explicit cutter diameter in inches requested for
            this line (cascaded line -> label -> job), verbatim as declared,
            or ``None`` when unset. ``None`` means the cutter was
            auto-selected from ``nominal_text_height`` via
            :func:`get_cutter_diameter` (``cutter_size`` is then ``None``
            while :attr:`cutter_diameter` carries the auto-selected tool);
            when set, :attr:`cutter_diameter` carries the inventory-snapped
            value (equal to it when no inventory is in play) and
            :attr:`toolpath_text_height` is ``nominal_text_height -
            cutter_diameter``. Default ``DEFAULT_CUTTER_SIZE`` (``None``).
    """

    text: str
    nominal_text_height: float
    toolpath_text_height: float
    cutter_diameter: float
    character_spacing: float
    line_spacing: float
    max_h_compress: float = 0.0
    text_h_alignment: str = DEFAULT_TEXT_H_ALIGNMENT
    text_color: str = DEFAULT_TEXT_COLOR
    font: str = DEFAULT_FONT
    space_width_fraction: float = DEFAULT_SPACE_WIDTH_FRACTION
    min_glyph_width: float = DEFAULT_MIN_GLYPH_WIDTH
    kerning_window_fraction: float = DEFAULT_KERNING_WINDOW_FRACTION
    kerning_penetration_scale: float = DEFAULT_KERNING_PENETRATION_SCALE
    kerning_recession_scale: float = DEFAULT_KERNING_RECESSION_SCALE
    kerning_min_gap: float = DEFAULT_KERNING_MIN_GAP
    fallback_advance_fraction: float = DEFAULT_FALLBACK_ADVANCE_FRACTION
    cutter_size: Optional[float] = DEFAULT_CUTTER_SIZE


@dataclass(frozen=True)
class ResolvedLabel:
    """A fully resolved label specification.

    Attributes:
        id: Unique identifier for this label.
        count: Number of instances to produce.
        width: Label width in inches (never None).
        height: Label height in inches (never None).
        margin: Universal margin in inches (fallback for h_margin, v_margin).
        h_margin: Horizontal margin in inches (left and right edges).
            Falls back to margin when unset in the schema.
        v_margin: Vertical margin in inches (top and bottom edges).
            Falls back to margin when unset in the schema.
        hole_margin: Hole margin in inches. The closest point of a hole
            circle to the label edge sits this far from the edge. Defaults
            to 0.0 (circle tangent to the edge) for manually constructed
            labels; the resolution engine always populates the cascaded
            value.
        holes: List of resolved hole specifications.
        content: List of resolved text lines.
        min_hole_margin: Lower bound (in inches) applied to ``hole_margin``
            during text-hole collision avoidance. ``None`` (the default)
            allows shrinking the hole margin all the way to ``0.0``; the
            resolution engine always populates the cascaded value.
        collision_compress_by_line: Mapping of line index to per-line
            horizontal scale in ``(0.0, 1.0]`` when text-hole collision
            avoidance had to compress specific lines away from drill holes.
            An empty dict (the default) means no collision-driven compression
            was applied. Each line can have its own compression scale;
            lines not in the dict use scale ``1.0`` (uncompressed). Set by
            the renderer via ``dataclasses.replace``; never sourced from the
            YAML schema.
        hole_text_collision_distance: Minimum air gap in inches kept
            between the engraved text stroke and the engraved drill-hole
            stroke (cascaded label -> job, default ``0.15``). Combined
            with the stroke floor ``0.5 * (hole_cutter_diameter +
            line.cutter_diameter)`` this forms the effective collision
            threshold used by the renderer.
        hole_cutter_diameter: Cutter diameter in inches used to cut label
            boundaries and drill holes (from ``tools.json``
            ``boundary_hole_cutter_size``, snapped to the inventory;
            default ``0.015``). Used only to compute the collision stroke
            floor -- rendering itself emits pure geometry layers.
        text_chunk_mode: Plate-space text optimization granularity
            (``"line"`` or ``"word"``, cascaded from the job; default
            ``"line"``). Consumed by the renderer (chunk-record
            granularity) and the plate-space optimizer.
        plate_id: Optional id of the plate this label is pinned to (from
            ``LabelSpec.plate_id``; set by plate-level replacement
            expansion). Pinned labels pack exclusively onto that plate,
            which then accepts no other labels. ``None`` (the default)
            packs normally across all plates.
        material: Stock material name this label must be cut from (from
            the ``material`` cascade, label -> job). Labels sharing a
            material pack together and a plate never mixes materials (see
            :func:`plt_optimizer.generate.layout.generate_layout`).
            ``None`` (the default) means unset: the label groups with the
            job-level material, and a job without materials packs exactly
            like a single material group.
    """

    id: str
    count: int
    width: float
    height: float
    margin: float
    h_margin: float
    v_margin: float
    hole_margin: float = 0.0
    holes: list[ResolvedHoleSpec] = field(default_factory=list)
    content: list[ResolvedTextLine] = field(default_factory=list)
    min_hole_margin: Optional[float] = None
    collision_compress_by_line: dict[int, float] = field(default_factory=dict)
    hole_text_collision_distance: float = DEFAULT_HOLE_TEXT_COLLISION_DISTANCE
    hole_cutter_diameter: float = DEFAULT_BOUNDARY_HOLE_CUTTER
    text_chunk_mode: str = "line"
    plate_id: Optional[str] = None
    material: Optional[str] = None


# ---------------------------------------------------------------------------
# Margin precedence helper
# ---------------------------------------------------------------------------
def fit_line_spacing_to_margins(
    line_heights: Sequence[float],
    line_spacings: Sequence[float],
    available_height: float,
) -> list[float]:
    """Shrink inter-line spacing so a stacked text block fits the margin box.

    Margins take precedence over requested line spacing: when the stacked
    block (sum of line heights plus inter-line spacing) exceeds the
    available inner height, the spacings are scaled down proportionally
    (down to a floor of ``0.0``) until the block fits exactly.

    Args:
        line_heights: Rendered (or nominal) height of each text line in
            inches. Length ``n``.
        line_spacings: Extra spacing applied *after* each line except the
            last, in inches. Length ``n - 1``.
        available_height: Inner content height in inches
            (label height minus both margins).

    Returns:
        A new list of inter-line spacings. Equal to the input when the
        block already fits; proportionally reduced otherwise. Spacing is
        never increased and never negative. Note that if the line heights
        alone exceed ``available_height``, all spacings collapse to ``0.0``
        and the block still overflows (text height is never reduced).
    """
    spacings = [max(0.0, float(s)) for s in line_spacings]
    total_height = sum(float(h) for h in line_heights) + sum(spacings)
    excess = total_height - available_height
    if excess <= 0.0 or not spacings:
        return spacings

    total_spacing = sum(spacings)
    if total_spacing <= 0.0:
        return spacings

    # Proportional scale-down so the total reduction equals the excess
    # (capped at the total spacing available, i.e. a floor of zero).
    factor = max(0.0, (total_spacing - excess) / total_spacing)
    return [s * factor for s in spacings]


def compute_horizontal_scale(
    rendered_width: float,
    available_width: float,
    max_h_compress: float,
) -> float:
    """Compute the uniform horizontal scale factor for one rendered line.

    Margin precedence for width: when a rendered line is wider than the
    label's inner content area, the line is compressed horizontally (never
    stretched, never taller) down to the available width. Compression is
    bounded by the resolved ``max_h_compress`` fraction, so a line is never
    squeezed below ``(1 - max_h_compress)`` of its natural width. When the
    limit cannot fully resolve the overflow, the returned factor still
    compresses as far as allowed and the caller is responsible for logging
    the residual overflow.

    Args:
        rendered_width: Measured rendered line width in inches (must be
            positive for any scaling to apply).
        available_width: Inner content width in inches (label width minus
            both margins).
        max_h_compress: Maximum compression fraction in ``[0.0, 1.0]``.
            ``0.0`` disables compression; ``0.5`` allows squeezing to 50%
            of natural width.

    Returns:
        A scale factor in ``[1 - max_h_compress, 1.0]``. ``1.0`` means no
        compression (the line fits or compression is disabled).
    """
    limit = min(max(0.0, max_h_compress), 1.0)
    if limit <= 0.0 or rendered_width <= 0.0:
        return 1.0
    if rendered_width <= available_width:
        return 1.0
    needed = available_width / rendered_width
    floor = 1.0 - limit
    return max(needed, floor)


def compute_horizontal_offset(
    rendered_width: float,
    available_width: float,
    margin: float,
    alignment: str,
) -> float:
    """Compute the target left-edge X for a rendered line inside the margin box.

    The label's inner content area spans ``[margin, margin +
    available_width]``. The alignment anchors the rendered line within
    that span:

    - ``"left"``: the line's left-most point sits precisely at the left
      margin (``margin``).
    - ``"right"``: the line's right-most point sits precisely at the right
      margin (``margin + available_width``).
    - ``"center"`` (default): the line is centered within the span.

    When the line is wider than the available width (e.g. compression is
    disabled), ``"center"`` overflows symmetrically and ``"left"`` /
    ``"right"`` keep their respective margin edges anchored, spilling out
    the opposite side.

    Args:
        rendered_width: Measured rendered line width in inches.
        available_width: Inner content width in inches (label width minus
            both margins).
        margin: Resolved label margin in inches (left inner edge position).
        alignment: One of ``"left"``, ``"center"`` or ``"right"``. Unknown
            values fall back to ``"center"``.

    Returns:
        The X coordinate where the line's left-most point (its ``min_x``)
        should be translated to.
    """
    if alignment == "left":
        return margin
    if alignment == "right":
        return margin + available_width - rendered_width
    return margin + (available_width - rendered_width) / 2.0


def _resolve_auto_line_spacing(
    content: list[ResolvedTextLine],
    label_height: float,
    v_margin_explicit: Optional[float],
    boundary_hole_cutter: float,
    label_id: str,
    interline_to_top_bottom_ratio: float = 1.0,
) -> tuple[list[ResolvedTextLine], Optional[float]]:
    """Resolve any auto line spacing values in content.

    When ``line_spacing="auto"``, spacing is calculated such that all lines
    have equal functional spacing (or proportional spacing if a ratio > 1.0
    is provided). If ``v_margin`` is explicitly specified, it is honored and
    ``line_spacing`` is calculated to fill the remaining space. If ``v_margin``
    is not explicitly specified, the functional ``v_margin`` is set equal to
    the inter-line spacing (or scaled by the ratio).

    Cutter width is a horizontal measure (stroke width) and does not affect
    vertical line spacing. The calculation divides available vertical space
    among inter-line gaps and top/bottom margins, with the ratio controlling
    their relative sizes.

    Args:
        content: Fully resolved text lines (with line_spacing=-1.0 for auto).
        label_height: Final label height in inches (outer boundary).
        v_margin_explicit: The explicitly specified v_margin, or None if
            v_margin was determined by cascading through margins/defaults.
        boundary_hole_cutter: Cutter diameter used for hole/boundary cutting.
        label_id: Identifier used in log messages.
        interline_to_top_bottom_ratio: Ratio controlling inter-line spacing
            relative to top/bottom margins. Default 1.0 makes all gaps equal.
            Values > 1.0 increase inter-line spacing at the expense of
            top/bottom margins.

    Returns:
        A tuple of (updated_content, calculated_v_margin). calculated_v_margin
        is non-None only when v_margin was not explicitly specified (auto mode),
        in which case it should replace the default v_margin.
    """
    if len(content) < 1:
        return content, None

    # Check if any lines have auto spacing (sentinel value -1.0)
    auto_indices = [i for i, line in enumerate(content) if line.line_spacing < 0.0]
    if not auto_indices:
        return content, None

    # Single-line content with auto spacing: zero spacing, no lines after it
    if len(content) == 1:
        if auto_indices:
            updated_content = [replace(content[0], line_spacing=0.0)]
            return updated_content, None
        else:
            return content, None

    # If only some lines have auto spacing (mixed), convert all to auto for consistency
    if len(auto_indices) != len(content):
        logger.warning(
            "Label %s: mixed auto and explicit line_spacing detected; treating all lines as auto.",
            label_id,
        )
        auto_indices = list(range(len(content)))

    line_heights = [line.nominal_text_height for line in content]
    total_line_height = sum(line_heights)
    num_lines = len(content)

    calculated_v_margin: Optional[float] = None

    if v_margin_explicit is not None:
        # Explicit v_margin: calculate line_spacing to fill the remaining space
        available_height = label_height - 2.0 * v_margin_explicit
        if num_lines > 1:
            calculated_spacing = max(0.0, (available_height - total_line_height) / (num_lines - 1))
        else:
            calculated_spacing = 0.0

        logger.info(
            "Label %s: auto line_spacing calculated as %.4fin "
            "(explicit v_margin=%.4fin, available_height=%.4fin, line_height=%.4fin).",
            label_id,
            calculated_spacing,
            v_margin_explicit,
            available_height,
            total_line_height,
        )
    else:
        # Auto v_margin: calculate gaps with optional ratio adjustment
        # When ratio = 1.0, all gaps are equal (top margin = inter-line = bottom margin).
        # When ratio > 1.0, inter-line gaps are larger at the expense of top/bottom margins.
        #
        # Formula:
        #   top_bottom_gap = (height - line_total) / (2 + (num_lines - 1) * ratio)
        #   interline_gap = ratio * top_bottom_gap
        #
        available_space = label_height - total_line_height
        if num_lines > 1:
            # Multiple lines: account for (num_lines - 1) inter-line gaps
            top_bottom_gap = max(
                0.0,
                available_space / (2.0 + (num_lines - 1) * interline_to_top_bottom_ratio),
            )
            calculated_spacing = interline_to_top_bottom_ratio * top_bottom_gap
        else:
            # Single line: top and bottom gaps only
            top_bottom_gap = available_space / 2.0
            calculated_spacing = 0.0

        # Set calculated_v_margin to the top/bottom gap value
        calculated_v_margin = top_bottom_gap

        logger.info(
            "Label %s: auto line_spacing and v_margin calculated as "
            "line_spacing=%.4fin, v_margin=%.4fin "
            "(auto v_margin mode, ratio=%.2f, label_height=%.4fin, line_height=%.4fin).",
            label_id,
            calculated_spacing,
            top_bottom_gap,
            interline_to_top_bottom_ratio,
            label_height,
            total_line_height,
        )

    # Replace sentinel values with calculated spacing
    updated_content = [
        replace(line, line_spacing=calculated_spacing) if i in auto_indices else line
        for i, line in enumerate(content)
    ]

    return updated_content, calculated_v_margin


def _fit_content_to_margins(
    content: list[ResolvedTextLine],
    label_height: float,
    margin: float,
    label_id: str,
) -> list[ResolvedTextLine]:
    """Clamp resolved line spacing so text respects the label margins.

    Uses nominal text heights for the fit estimate; the renderer applies a
    final safety check against measured glyph heights.

    Args:
        content: Fully resolved text lines for the label.
        label_height: Final label height in inches (outer boundary).
        margin: Resolved label margin in inches.
        label_id: Identifier used in log messages.

    Returns:
        The original content list when no adjustment is needed, otherwise
        a new list with reduced ``line_spacing`` values.
    """
    if len(content) < 2:
        return content

    available_height = label_height - (2 * margin)
    heights = [line.nominal_text_height for line in content]
    spacings = [line.line_spacing for line in content[:-1]]

    adjusted = fit_line_spacing_to_margins(heights, spacings, available_height)
    if all(math.isclose(a, b, abs_tol=1e-9) for a, b in zip(adjusted, spacings)):
        return content

    logger.debug(
        "Label %s: line_spacing reduced from %s to %s to preserve margin "
        "%.3fin (available inner height %.3fin).",
        label_id,
        [round(s, 4) for s in spacings],
        [round(s, 4) for s in adjusted],
        margin,
        available_height,
    )
    if sum(heights) > available_height:
        logger.warning(
            "Label %s: text lines alone (%.3fin) exceed the available inner "
            "height (%.3fin); margins cannot be fully preserved without "
            "reducing text height.",
            label_id,
            sum(heights),
            available_height,
        )

    return [
        replace(line, line_spacing=adjusted[i]) if i < len(adjusted) else line
        for i, line in enumerate(content)
    ]


# ---------------------------------------------------------------------------
# Resolution engine
# ---------------------------------------------------------------------------
def _resolve_margins(
    label_input: LabelSpec | JobSpec,
    job: JobSpec,
) -> tuple[float, float]:
    """Resolve h_margin and v_margin with fallback to margin then defaults.

    Cascade order for h_margin:
        label.h_margin → job.h_margin → label.margin → job.margin → DEFAULT_MARGIN

    Cascade order for v_margin:
        label.v_margin → job.v_margin → label.margin → job.margin → DEFAULT_MARGIN

    Args:
        label_input: The label (or root-level job) being processed.
        job: The outer JobSpec providing fallback values.

    Returns:
        A tuple of (h_margin, v_margin) in inches, both guaranteed to be
        non-None and positive.
    """
    # Resolve h_margin
    h_margin = label_input.h_margin
    if h_margin is None:
        h_margin = job.h_margin
    if h_margin is None:
        h_margin = label_input.margin
    if h_margin is None:
        h_margin = job.margin
    if h_margin is None:
        h_margin = DEFAULT_MARGIN
    h_margin = float(h_margin)

    # Resolve v_margin
    v_margin = label_input.v_margin
    if v_margin is None:
        v_margin = job.v_margin
    if v_margin is None:
        v_margin = label_input.margin
    if v_margin is None:
        v_margin = job.margin
    if v_margin is None:
        v_margin = DEFAULT_MARGIN
    v_margin = float(v_margin)

    return h_margin, v_margin


def _resolve_holes(
    label_input: LabelSpec | JobSpec,
    job: JobSpec,
) -> list[ResolvedHoleSpec]:
    """Resolve holes with label-level precedence over job-level.

    Args:
        label_input: The label (or root-level job) being processed.
        job: The outer JobSpec providing fallback values.

    Returns:
        A list of fully resolved hole specifications.
    """
    raw_holes = label_input.holes if label_input.holes is not None else job.holes
    if not raw_holes:
        return []
    return [ResolvedHoleSpec(diameter=h.diameter, location=h.location.value) for h in raw_holes]


def _resolve_content(
    label_input: LabelSpec | JobSpec,
    job: JobSpec,
    available_cutters: Optional[list[float]] = None,
    tolerance_factor: float = 3.0,
    label_id: str = "",
) -> list[ResolvedTextLine]:
    """Resolve text lines with cutter compensation applied.

    Cascades values from line -> label -> job -> default, then determines
    the appropriate cutter diameter and subtracts it from the nominal
    height to produce the toolpath height.

    An explicit ``cutter_size`` (cascaded line -> label -> job) overrides
    the automatic height-based selection: the requested diameter is snapped
    to ``available_cutters`` (next size down, else next size up) and the
    nominal height is kept as the user's intent, so only the toolpath
    height (``nominal_height - cutter_size``) changes. When the explicit
    cutter leaves no material to engrave (toolpath height <= 0),
    :class:`CutterSizeError` is raised.

    Args:
        label_input: The label (or root-level job) being processed.
        job: The outer JobSpec providing fallback values.
        available_cutters: Optional list of cutter diameters available in
            the shop. If provided, the cutter is snapped to the closest
            available tool.
        tolerance_factor: The multiplier used to decide between narrower and
            wider cutters. See ``get_cutter_diameter`` for details.
        label_id: Identifier used in WARNING/ERROR messages (the label
            owning the lines being resolved).

    Returns:
        A list of fully resolved text lines with cutter compensation.

    Raises:
        CutterSizeError: If an explicit ``cutter_size`` is greater than or
            equal to the line's nominal text height (no material left to
            engrave).
    """
    resolved_content: list[ResolvedTextLine] = []
    content = label_input.content
    assert content is not None, "label_input.content must not be None"
    for line_index, line in enumerate(content):
        # Resolve nominal height through the inheritance cascade
        nominal_height = (
            line.text_height or label_input.text_height or job.text_height or DEFAULT_TEXT_HEIGHT
        )

        # Resolve the explicit cutter override with the same explicit-None
        # precedence (line -> label -> job -> unset). Deliberately no
        # job-config.json tier: the field has no shop-level default.
        if line.cutter_size is not None:
            line_cutter_size: Optional[float] = line.cutter_size
        elif label_input.cutter_size is not None:
            line_cutter_size = label_input.cutter_size
        elif job.cutter_size is not None:
            line_cutter_size = job.cutter_size
        else:
            line_cutter_size = DEFAULT_CUTTER_SIZE

        # Determine cutter and compensate for toolpath. An explicit cutter
        # replaces the height-based lookup, snapped to the shop inventory
        # (next size down, else next size up); an unset value keeps the
        # automatic selection from the nominal height (current behaviour).
        if line_cutter_size is not None:
            cutter_dia = snap_boundary_hole_cutter(line_cutter_size, available_cutters)
            if not math.isclose(cutter_dia, line_cutter_size, abs_tol=1e-9):
                logger.warning(
                    "Label %s line %d: requested cutter_size %.4fin is not in "
                    "the shop inventory; snapped to %.4fin.",
                    label_id,
                    line_index,
                    line_cutter_size,
                    cutter_dia,
                )
            toolpath_height = nominal_height - cutter_dia
            if toolpath_height <= 0.0:
                raise CutterSizeError(
                    f"label {label_id!r} line {line_index} ({line.text!r}): "
                    f"cutter_size {cutter_dia:g}in leaves no material to "
                    f"engrave (text_height {nominal_height:g}in); cutter_size "
                    f"must be smaller than text_height"
                )
        else:
            cutter_dia = get_cutter_diameter(nominal_height, available_cutters, tolerance_factor)
            toolpath_height = nominal_height - cutter_dia

        # Resolve spacing (kerning can now dynamically rely on cutter_dia if omitted)
        char_spacing = (
            line.character_spacing
            or label_input.character_spacing
            or job.character_spacing
            or (cutter_dia * 1.5)
        )

        # Resolve line_spacing, checking for "auto" at each cascade level
        line_spacing_raw = (
            line.line_spacing
            or label_input.line_spacing
            or job.line_spacing
            or DEFAULT_LINE_SPACING
        )

        # Check if the resolved value is the string "auto"
        if isinstance(line_spacing_raw, str) and line_spacing_raw == "auto":
            # Use sentinel value -1.0 to indicate auto; will be calculated later
            line_spacing: float = -1.0
        else:
            # Convert to float (handles both numeric and None cases)
            line_spacing = (
                float(line_spacing_raw) if line_spacing_raw is not None else DEFAULT_LINE_SPACING
            )

        # Resolve horizontal compression limit explicitly so an intentional
        # ``0.0`` (compression disabled) is honored instead of falling
        # through to a parent value.
        if line.max_h_compress is not None:
            line_max_h_compress: float = line.max_h_compress
        elif label_input.max_h_compress is not None:
            line_max_h_compress = label_input.max_h_compress
        elif job.max_h_compress is not None:
            line_max_h_compress = job.max_h_compress
        else:
            line_max_h_compress = DEFAULT_MAX_H_COMPRESS

        # Resolve horizontal text alignment (line -> label -> job -> default).
        if line.text_h_alignment is not None:
            line_text_h_alignment: str = line.text_h_alignment.value
        elif label_input.text_h_alignment is not None:
            line_text_h_alignment = label_input.text_h_alignment.value
        elif job.text_h_alignment is not None:
            line_text_h_alignment = job.text_h_alignment.value
        else:
            line_text_h_alignment = DEFAULT_TEXT_H_ALIGNMENT

        # Resolve the stroke-color layer tag (line -> label -> default).
        # Deliberately no job tier: text_color distinguishes otherwise-
        # equivalent text within/between labels and never cascades from
        # the job (the schema rejects a job-level value outright).
        if line.text_color is not None:
            line_text_color: str = line.text_color.value
        elif label_input.text_color is not None:
            line_text_color = label_input.text_color.value
        else:
            line_text_color = DEFAULT_TEXT_COLOR

        # Resolve the font name (line -> label -> job -> default). Schema
        # validation already canonicalized every explicit value, so the
        # cascade only picks the first tier that is set.
        if line.font is not None:
            line_font: str = line.font
        elif label_input.font is not None:
            line_font = label_input.font
        elif job.font is not None:
            line_font = job.font
        else:
            line_font = DEFAULT_FONT

        # Resolve the PLT-font space advance fraction explicitly so an
        # intentional ``0.0`` is honored instead of falling through to a
        # parent value (line -> label -> job -> default).
        if line.space_width_fraction is not None:
            line_space_width_fraction: float = line.space_width_fraction
        elif label_input.space_width_fraction is not None:
            line_space_width_fraction = label_input.space_width_fraction
        elif job.space_width_fraction is not None:
            line_space_width_fraction = job.space_width_fraction
        else:
            line_space_width_fraction = DEFAULT_SPACE_WIDTH_FRACTION

        # Resolve the PLT-font global minimum glyph width with the same
        # explicit-None precedence (line -> label -> job -> default).
        if line.min_glyph_width is not None:
            line_min_glyph_width: float = line.min_glyph_width
        elif label_input.min_glyph_width is not None:
            line_min_glyph_width = label_input.min_glyph_width
        elif job.min_glyph_width is not None:
            line_min_glyph_width = job.min_glyph_width
        else:
            line_min_glyph_width = DEFAULT_MIN_GLYPH_WIDTH

        # Resolve the PLT-font kerning window fraction with the same
        # explicit-None precedence (line -> label -> job -> default).
        if line.kerning_window_fraction is not None:
            line_kerning_window_fraction: float = line.kerning_window_fraction
        elif label_input.kerning_window_fraction is not None:
            line_kerning_window_fraction = label_input.kerning_window_fraction
        elif job.kerning_window_fraction is not None:
            line_kerning_window_fraction = job.kerning_window_fraction
        else:
            line_kerning_window_fraction = DEFAULT_KERNING_WINDOW_FRACTION

        # Resolve the PLT-font kerning spacing knobs with the same
        # explicit-None precedence (line -> label -> job -> default).
        if line.kerning_penetration_scale is not None:
            line_kerning_penetration_scale: float = line.kerning_penetration_scale
        elif label_input.kerning_penetration_scale is not None:
            line_kerning_penetration_scale = label_input.kerning_penetration_scale
        elif job.kerning_penetration_scale is not None:
            line_kerning_penetration_scale = job.kerning_penetration_scale
        else:
            line_kerning_penetration_scale = DEFAULT_KERNING_PENETRATION_SCALE

        if line.kerning_recession_scale is not None:
            line_kerning_recession_scale: float = line.kerning_recession_scale
        elif label_input.kerning_recession_scale is not None:
            line_kerning_recession_scale = label_input.kerning_recession_scale
        elif job.kerning_recession_scale is not None:
            line_kerning_recession_scale = job.kerning_recession_scale
        else:
            line_kerning_recession_scale = DEFAULT_KERNING_RECESSION_SCALE

        if line.kerning_min_gap is not None:
            line_kerning_min_gap: float = line.kerning_min_gap
        elif label_input.kerning_min_gap is not None:
            line_kerning_min_gap = label_input.kerning_min_gap
        elif job.kerning_min_gap is not None:
            line_kerning_min_gap = job.kerning_min_gap
        else:
            line_kerning_min_gap = DEFAULT_KERNING_MIN_GAP

        if line.fallback_advance_fraction is not None:
            line_fallback_advance_fraction: float = line.fallback_advance_fraction
        elif label_input.fallback_advance_fraction is not None:
            line_fallback_advance_fraction = label_input.fallback_advance_fraction
        elif job.fallback_advance_fraction is not None:
            line_fallback_advance_fraction = job.fallback_advance_fraction
        else:
            line_fallback_advance_fraction = DEFAULT_FALLBACK_ADVANCE_FRACTION

        resolved_content.append(
            ResolvedTextLine(
                text=line.text,
                nominal_text_height=nominal_height,
                toolpath_text_height=toolpath_height,
                cutter_diameter=cutter_dia,
                character_spacing=char_spacing,
                line_spacing=line_spacing,
                max_h_compress=line_max_h_compress,
                text_h_alignment=line_text_h_alignment,
                text_color=line_text_color,
                font=line_font,
                space_width_fraction=line_space_width_fraction,
                min_glyph_width=line_min_glyph_width,
                kerning_window_fraction=line_kerning_window_fraction,
                kerning_penetration_scale=line_kerning_penetration_scale,
                kerning_recession_scale=line_kerning_recession_scale,
                kerning_min_gap=line_kerning_min_gap,
                fallback_advance_fraction=line_fallback_advance_fraction,
                cutter_size=line_cutter_size,
            )
        )
    return resolved_content


def _resolve_label(
    label_input: LabelSpec | JobSpec,
    job: JobSpec,
    available_cutters: Optional[list[float]] = None,
    tolerance_factor: float = 3.0,
    boundary_hole_cutter_size: Optional[float] = None,
) -> ResolvedLabel:
    """Resolve a single label (or root-level job) into a ResolvedLabel.

    Args:
        label_input: The label (or root-level job) being processed.
        job: The outer JobSpec providing fallback values.
        available_cutters: Optional list of cutter diameters available in
            the shop.
        tolerance_factor: The multiplier used to decide between narrower and
            wider cutters. See ``get_cutter_diameter`` for details.
        boundary_hole_cutter_size: Optional requested cutter diameter in
            inches for label boundaries and drill holes (from
            ``tools.json``). Snapped to ``available_cutters`` via
            :func:`snap_boundary_hole_cutter`; ``None`` falls back to
            ``DEFAULT_BOUNDARY_HOLE_CUTTER``.

    Returns:
        A fully resolved label with all dimensions guaranteed non-None.
    """
    # Generate ID if this is a root-level job masquerading as a label
    label_id: str = getattr(label_input, "id", None) or f"label_{uuid.uuid4().hex[:8]}"
    label_count: int = getattr(label_input, "count", 1) or 1

    # Resolve label-level styles (Label -> Job -> Fallback)
    label_margin: float = label_input.margin or job.margin or DEFAULT_MARGIN

    # Resolve h_margin and v_margin with fallback to margin then defaults
    label_h_margin, label_v_margin = _resolve_margins(label_input, job)

    # Resolve hole margin explicitly so an intentional ``0.0`` (hole tangent
    # to the edge) is honored instead of falling through to the default.
    if label_input.hole_margin is not None:
        label_hole_margin: float = label_input.hole_margin
    elif job.hole_margin is not None:
        label_hole_margin = job.hole_margin
    else:
        label_hole_margin = DEFAULT_HOLE_MARGIN

    # Resolve the collision-avoidance floor for hole margin explicitly so an
    # intentional ``0.0`` (shrink all the way to tangent) is honored instead
    # of falling through to the parent value.
    if label_input.min_hole_margin is not None:
        label_min_hole_margin: Optional[float] = label_input.min_hole_margin
    elif job.min_hole_margin is not None:
        label_min_hole_margin = job.min_hole_margin
    else:
        label_min_hole_margin = DEFAULT_MIN_HOLE_MARGIN

    # Resolve the engraved-stroke air gap explicitly so an intentional
    # ``0.0`` (strokes may touch but never overlap) is honored instead of
    # falling through to the 0.15in default.
    if label_input.hole_text_collision_distance is not None:
        label_collision_distance: float = label_input.hole_text_collision_distance
    elif job.hole_text_collision_distance is not None:
        label_collision_distance = job.hole_text_collision_distance
    else:
        label_collision_distance = DEFAULT_HOLE_TEXT_COLLISION_DISTANCE

    # Snap the boundary/hole cutter to the shop inventory (next size down,
    # else next size up). Used only for the collision stroke floor.
    requested_hole_cutter = (
        DEFAULT_BOUNDARY_HOLE_CUTTER
        if boundary_hole_cutter_size is None
        else boundary_hole_cutter_size
    )
    hole_cutter = snap_boundary_hole_cutter(requested_hole_cutter, available_cutters)

    # Text chunk mode is job-level only (like allow_rotation); a root-level
    # job masquerading as a label carries the field itself.
    label_text_chunk_mode: str = (
        getattr(label_input, "text_chunk_mode", None) or job.text_chunk_mode
    )

    # Plate pinning (plate-level replacement expansion / explicit
    # LabelSpec.plate_id); a root-level job has no plate_id attribute.
    label_plate_id: Optional[str] = getattr(label_input, "plate_id", None)

    # Stock material (cascades label -> job; both tiers carry the field, so
    # a root-level job masquerading as a label resolves to its own value).
    # ``None`` stays ``None``: unset labels group with the job-level material
    # at packing time, and a fully unset job packs exactly like before.
    label_material: Optional[str] = (
        label_input.material if label_input.material is not None else job.material
    )

    # Resolve text lines with cutter compensation
    resolved_content = _resolve_content(
        label_input, job, available_cutters, tolerance_factor, label_id
    )

    # Resolve holes
    resolved_holes = _resolve_holes(label_input, job)

    # Resolve final width and height (both must be defined at schema validation time)
    # After schema validation, one of (label_width, job_width) is always non-None
    final_width: float = label_input.width if label_input.width is not None else job.width  # type: ignore[assignment]
    final_height: float = label_input.height if label_input.height is not None else job.height  # type: ignore[assignment]

    # Sanity check: schema validation guarantees both are non-None
    assert final_width is not None and final_height is not None

    # Check if v_margin was explicitly specified (not derived from cascading margins)
    v_margin_is_explicit = label_input.v_margin is not None or job.v_margin is not None

    # Resolve auto line spacing ratio (job level only)
    interline_to_top_bottom_ratio = job.auto_line_spacing_interline_to_top_bottom_ratio or 1.0

    # Resolve any auto line spacing (must happen before margin fitting)
    resolved_content, calculated_v_margin = _resolve_auto_line_spacing(
        resolved_content,
        final_height,
        label_v_margin if v_margin_is_explicit else None,
        hole_cutter,
        label_id,
        interline_to_top_bottom_ratio,
    )

    # If auto v_margin was calculated, use it instead of the default
    if calculated_v_margin is not None:
        label_v_margin = calculated_v_margin

    # Margin precedence: shrink line spacing (never margins) so the stacked
    # text block fits within the inner content area.
    # Skip this when auto line spacing was used, since the spacing was already
    # calculated to fit perfectly within the v_margin.
    if calculated_v_margin is None:
        resolved_content = _fit_content_to_margins(
            resolved_content, final_height, label_margin, label_id
        )

    return ResolvedLabel(
        id=label_id,
        count=label_count,
        width=final_width,
        height=final_height,
        margin=label_margin,
        h_margin=label_h_margin,
        v_margin=label_v_margin,
        hole_margin=label_hole_margin,
        holes=resolved_holes,
        content=resolved_content,
        min_hole_margin=label_min_hole_margin,
        hole_text_collision_distance=label_collision_distance,
        hole_cutter_diameter=hole_cutter,
        text_chunk_mode=label_text_chunk_mode,
        plate_id=label_plate_id,
        material=label_material,
    )


def build_cutter_pen_map(
    resolved_labels: Sequence[ResolvedLabel],
) -> dict[tuple[float, str], int]:
    """Map each distinct text (cutter, color) layer to an HPGL pen number.

    Per-cutter export assigns one pen (HPGL ``SP`` layer) per distinct
    text cutter diameter *and* stroke-color tag so every cutter ends up
    in its own PLT file, and lines sharing a cutter but carrying
    different ``text_color`` values still split into separate toolpaths
    (depth changes between runs on 3-layer material). Pen numbers are
    reserved for the structural layers:

    - ``SP1``: the smallest text layer (kept as pen 1 for backward
      compatibility with single-cutter jobs, where all text lands on the
      historical text layer).
    - ``SP2``: label boundaries (reserved, never a text pen).
    - ``SP3``: drill holes (reserved, never a text pen).
    - ``SP4+``: remaining text layers, sorted by ``(cutter, color)``.

    A text cutter that happens to equal the boundary/hole cutter still
    gets its own pen (it is engraved in a separate run). Jobs whose lines
    all carry the implicit ``"none"`` color produce exactly the
    historical cutter-only pen assignment.

    Args:
        resolved_labels: Fully resolved labels whose text lines carry
            ``cutter_diameter`` and ``text_color`` values.

    Returns:
        Mapping of ``(cutter diameter (inches), text color)`` to pen
        number. Empty when no label has any text content.

    Example:
        >>> # cutters 0.03 and 0.06 present -> smallest keeps pen 1
        >>> build_cutter_pen_map(labels)  # doctest: +SKIP
        {(0.03, "none"): 1, (0.06, "none"): 4}
    """
    layers = sorted(
        {
            (line.cutter_diameter, line.text_color)
            for label in resolved_labels
            for line in label.content
        }
    )
    pen_map: dict[tuple[float, str], int] = {}
    for index, layer in enumerate(layers):
        # First (smallest) layer keeps the historical text pen 1; the
        # remaining layers start at SP4 because SP2 (borders) and SP3
        # (holes) are reserved for the structural layers.
        pen_map[layer] = 1 if index == 0 else index + 3
    return pen_map


def resolve_job_spec(
    job: JobSpec,
    available_cutters: Optional[list[float]] = None,
    tolerance_factor: float = 3.0,
    boundary_hole_cutter_size: Optional[float] = None,
) -> list[ResolvedLabel]:
    """Flatten a JobSpec into a list of fully resolved labels.

    Handles both the explicit ``labels`` form and the root-level
    ``content``/``count`` form. For root-level jobs, a synthetic ID is
    generated.

    Args:
        job: The parsed and validated JobSpec.
        available_cutters: Optional list of cutter diameters available in
            the shop. If provided, the cutter matching will snap to the
            closest available tool.
        tolerance_factor: The multiplier used to decide between narrower and
            wider cutters. See ``get_cutter_diameter`` for details.
        boundary_hole_cutter_size: Optional requested cutter diameter in
            inches for label boundaries and drill holes (from
            ``tools.json`` ``boundary_hole_cutter_size``). Snapped to
            ``available_cutters`` (next size down, else next size up);
            ``None`` falls back to ``DEFAULT_BOUNDARY_HOLE_CUTTER``.
            Feeds the text-hole collision stroke floor only.

    Returns:
        A list of ResolvedLabel objects with all dimensions guaranteed
        non-None.

    Example:
        >>> job = JobSpec(
        ...     job_name="Batch",
        ...     width=3.0,
        ...     height=1.5,
        ...     count=10,
        ...     content=[TextLine(text="DANGER")],
        ... )
        >>> labels = resolve_job_spec(job)
        >>> len(labels)
        1
        >>> labels[0].width
        3.0
    """
    # Determine if job is root-level content or a list of labels
    if job.labels:
        labels_to_process: list[LabelSpec | JobSpec] = list(job.labels)
    else:
        # Root-level job: treat the JobSpec itself as a single label
        labels_to_process = [job]

    return [
        _resolve_label(
            label_input,
            job,
            available_cutters,
            tolerance_factor,
            boundary_hole_cutter_size,
        )
        for label_input in labels_to_process
    ]
