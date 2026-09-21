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
    >>> job = parse_yaml("examples/sample_spec.yaml")
    >>> labels = resolve_job_spec(job)
    >>> print(labels[0].width)
    3.0
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field, replace
from typing import Optional, Sequence

from plt_optimizer.generate.schema import JobSpec, LabelSpec

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global fallback constants
# ---------------------------------------------------------------------------
DEFAULT_TEXT_HEIGHT: float = 0.25
DEFAULT_MARGIN: float = 0.125
DEFAULT_LINE_SPACING: float = 0.1
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
# Horizontal text alignment defaults to centering (existing behaviour).
DEFAULT_TEXT_H_ALIGNMENT: str = "center"

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
    """

    text: str
    nominal_text_height: float
    toolpath_text_height: float
    cutter_diameter: float
    character_spacing: float
    line_spacing: float
    max_h_compress: float = 0.0
    text_h_alignment: str = DEFAULT_TEXT_H_ALIGNMENT


@dataclass(frozen=True)
class ResolvedLabel:
    """A fully resolved label specification.

    Attributes:
        id: Unique identifier for this label.
        count: Number of instances to produce.
        width: Label width in inches (never None).
        height: Label height in inches (never None).
        margin: Label margin in inches.
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
        collision_compress: Extra uniform horizontal scale in ``(0.0, 1.0]``
            applied to every renderable text line when the collision
            avoidance system had to compress text away from drill holes.
            ``1.0`` (the default) means no collision-driven compression was
            applied. Set by the renderer via ``dataclasses.replace``; never
            sourced from the YAML schema.
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
    """

    id: str
    count: int
    width: float
    height: float
    margin: float
    hole_margin: float = 0.0
    holes: list[ResolvedHoleSpec] = field(default_factory=list)
    content: list[ResolvedTextLine] = field(default_factory=list)
    min_hole_margin: Optional[float] = None
    collision_compress: float = 1.0
    hole_text_collision_distance: float = DEFAULT_HOLE_TEXT_COLLISION_DISTANCE
    hole_cutter_diameter: float = DEFAULT_BOUNDARY_HOLE_CUTTER
    text_chunk_mode: str = "line"


# ---------------------------------------------------------------------------
# Auto-sizing helper
# ---------------------------------------------------------------------------
def calculate_label_dimensions(
    content: list[ResolvedTextLine],
    margin: float,
) -> tuple[float, float]:
    """Calculate auto-dimensions for a label from its resolved content.

    Uses a stub width estimation based on character count and text height.
    Both dimensions are rounded up to the nearest 0.25 inch increment.

    Args:
        content: Fully resolved text lines for the label.
        margin: Resolved margin in inches (applied to both sides).

    Returns:
        A tuple of (width, height) in inches, rounded up to the nearest
        0.25 inch.
    """
    max_text_width = 0.0
    total_text_height = 0.0

    for i, line in enumerate(content):
        # Stub width estimation: char count * nominal height * ratio + char spacing
        est_width = (len(line.text) * line.nominal_text_height * 0.6) + (
            len(line.text) * line.character_spacing
        )
        max_text_width = max(max_text_width, est_width)

        total_text_height += line.nominal_text_height
        if i < len(content) - 1:
            total_text_height += line.line_spacing

    # Add margins to both sides
    raw_width = max_text_width + (margin * 2)
    raw_height = total_text_height + (margin * 2)

    # Round up to nearest 0.25 inch
    final_width = math.ceil(raw_width * 4) / 4
    final_height = math.ceil(raw_height * 4) / 4

    return final_width, final_height


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
) -> list[ResolvedTextLine]:
    """Resolve text lines with cutter compensation applied.

    Cascades values from line -> label -> job -> default, then determines
    the appropriate cutter diameter and subtracts it from the nominal
    height to produce the toolpath height.

    Args:
        label_input: The label (or root-level job) being processed.
        job: The outer JobSpec providing fallback values.
        available_cutters: Optional list of cutter diameters available in
            the shop. If provided, the cutter is snapped to the closest
            available tool.
        tolerance_factor: The multiplier used to decide between narrower and
            wider cutters. See ``get_cutter_diameter`` for details.

    Returns:
        A list of fully resolved text lines with cutter compensation.
    """
    resolved_content: list[ResolvedTextLine] = []
    content = label_input.content
    assert content is not None, "label_input.content must not be None"
    for line in content:
        # Resolve nominal height through the inheritance cascade
        nominal_height = (
            line.text_height or label_input.text_height or job.text_height or DEFAULT_TEXT_HEIGHT
        )

        # Determine cutter and compensate for toolpath
        cutter_dia = get_cutter_diameter(nominal_height, available_cutters, tolerance_factor)
        toolpath_height = nominal_height - cutter_dia

        # Resolve spacing (kerning can now dynamically rely on cutter_dia if omitted)
        char_spacing = (
            line.character_spacing
            or label_input.character_spacing
            or job.character_spacing
            or (cutter_dia * 1.5)
        )
        line_spacing = (
            line.line_spacing
            or label_input.line_spacing
            or job.line_spacing
            or DEFAULT_LINE_SPACING
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

    # Resolve text lines with cutter compensation
    resolved_content = _resolve_content(label_input, job, available_cutters, tolerance_factor)

    # Resolve holes
    resolved_holes = _resolve_holes(label_input, job)

    # Execute auto-sizing calculations (use nominal heights for sizing)
    final_width: Optional[float] = label_input.width or job.width
    final_height: Optional[float] = label_input.height or job.height

    if final_width is None or final_height is None:
        calc_width, calc_height = calculate_label_dimensions(resolved_content, label_margin)
        final_width = final_width or calc_width
        final_height = final_height or calc_height

    # At this point, final_width and final_height are guaranteed non-None
    assert final_width is not None and final_height is not None

    # Margin precedence: shrink line spacing (never margins) so the stacked
    # text block fits within the inner content area.
    resolved_content = _fit_content_to_margins(
        resolved_content, final_height, label_margin, label_id
    )

    return ResolvedLabel(
        id=label_id,
        count=label_count,
        width=final_width,
        height=final_height,
        margin=label_margin,
        hole_margin=label_hole_margin,
        holes=resolved_holes,
        content=resolved_content,
        min_hole_margin=label_min_hole_margin,
        hole_text_collision_distance=label_collision_distance,
        hole_cutter_diameter=hole_cutter,
        text_chunk_mode=label_text_chunk_mode,
    )


def build_cutter_pen_map(resolved_labels: Sequence[ResolvedLabel]) -> dict[float, int]:
    """Map each distinct text cutter diameter to an HPGL pen number.

    Per-cutter export assigns one pen (HPGL ``SP`` layer) per distinct
    text cutter diameter so every cutter ends up in its own PLT file.
    Pen numbers are reserved for the structural layers:

    - ``SP1``: the smallest text cutter (kept as pen 1 for backward
      compatibility with single-cutter jobs, where all text lands on the
      historical text layer).
    - ``SP2``: label boundaries (reserved, never a text pen).
    - ``SP3``: drill holes (reserved, never a text pen).
    - ``SP4+``: remaining text cutters, sorted by ascending diameter.

    A text cutter that happens to equal the boundary/hole cutter still
    gets its own pen (it is engraved in a separate run).

    Args:
        resolved_labels: Fully resolved labels whose text lines carry
            ``cutter_diameter`` values.

    Returns:
        Mapping of cutter diameter (inches) to pen number. Empty when no
        label has any text content.

    Example:
        >>> # cutters 0.03 and 0.06 present -> smallest keeps pen 1
        >>> build_cutter_pen_map(labels)  # doctest: +SKIP
        {0.03: 1, 0.06: 4}
    """
    cutters = sorted({line.cutter_diameter for label in resolved_labels for line in label.content})
    pen_map: dict[float, int] = {}
    for index, cutter in enumerate(cutters):
        # First (smallest) cutter keeps the historical text pen 1; the
        # remaining cutters start at SP4 because SP2 (borders) and SP3
        # (holes) are reserved for the structural layers.
        pen_map[cutter] = 1 if index == 0 else index + 3
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
