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

import math
import uuid
from dataclasses import dataclass, field
from typing import Optional

from plt_optimizer.generate.schema import JobSpec, LabelSpec

# ---------------------------------------------------------------------------
# Global fallback constants
# ---------------------------------------------------------------------------
DEFAULT_TEXT_HEIGHT: float = 0.25
DEFAULT_MARGIN: float = 0.125
DEFAULT_CHAR_SPACING: float = 0.05
DEFAULT_LINE_SPACING: float = 0.1

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
    """

    text: str
    nominal_text_height: float
    toolpath_text_height: float
    cutter_diameter: float
    character_spacing: float
    line_spacing: float


@dataclass(frozen=True)
class ResolvedLabel:
    """A fully resolved label specification.

    Attributes:
        id: Unique identifier for this label.
        count: Number of instances to produce.
        width: Label width in inches (never None).
        height: Label height in inches (never None).
        margin: Label margin in inches.
        holes: List of resolved hole specifications.
        content: List of resolved text lines.
    """

    id: str
    count: int
    width: float
    height: float
    margin: float
    holes: list[ResolvedHoleSpec] = field(default_factory=list)
    content: list[ResolvedTextLine] = field(default_factory=list)


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

        resolved_content.append(
            ResolvedTextLine(
                text=line.text,
                nominal_text_height=nominal_height,
                toolpath_text_height=toolpath_height,
                cutter_diameter=cutter_dia,
                character_spacing=char_spacing,
                line_spacing=line_spacing,
            )
        )
    return resolved_content


def _resolve_label(
    label_input: LabelSpec | JobSpec,
    job: JobSpec,
    available_cutters: Optional[list[float]] = None,
    tolerance_factor: float = 3.0,
) -> ResolvedLabel:
    """Resolve a single label (or root-level job) into a ResolvedLabel.

    Args:
        label_input: The label (or root-level job) being processed.
        job: The outer JobSpec providing fallback values.
        available_cutters: Optional list of cutter diameters available in
            the shop.
        tolerance_factor: The multiplier used to decide between narrower and
            wider cutters. See ``get_cutter_diameter`` for details.

    Returns:
        A fully resolved label with all dimensions guaranteed non-None.
    """
    # Generate ID if this is a root-level job masquerading as a label
    label_id: str = getattr(label_input, "id", None) or f"label_{uuid.uuid4().hex[:8]}"
    label_count: int = getattr(label_input, "count", 1) or 1

    # Resolve label-level styles (Label -> Job -> Fallback)
    label_margin: float = label_input.margin or job.margin or DEFAULT_MARGIN

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

    return ResolvedLabel(
        id=label_id,
        count=label_count,
        width=final_width,
        height=final_height,
        margin=label_margin,
        holes=resolved_holes,
        content=resolved_content,
    )


def resolve_job_spec(
    job: JobSpec,
    available_cutters: Optional[list[float]] = None,
    tolerance_factor: float = 3.0,
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
        _resolve_label(label_input, job, available_cutters, tolerance_factor)
        for label_input in labels_to_process
    ]
