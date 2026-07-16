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
    """A fully resolved text line.

    Attributes:
        text: The actual text string to render.
        text_height: Font height in inches.
        character_spacing: Extra spacing between characters in inches.
        line_spacing: Extra spacing between text lines in inches.
    """

    text: str
    text_height: float
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
        # Stub width estimation: char count * text height * ratio + char spacing
        est_width = (len(line.text) * line.text_height * 0.6) + (
            len(line.text) * line.character_spacing
        )
        max_text_width = max(max_text_width, est_width)

        total_text_height += line.text_height
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
) -> list[ResolvedTextLine]:
    """Resolve text lines by cascading values from line -> label -> job -> default.

    Args:
        label_input: The label (or root-level job) being processed.
        job: The outer JobSpec providing fallback values.

    Returns:
        A list of fully resolved text lines.
    """
    resolved_content: list[ResolvedTextLine] = []
    # label_input.content is guaranteed non-None by the schema validator:
    # LabelSpec.content is required, and JobSpec.content is only None when
    # labels is provided (in which case we never reach this function with
    # a JobSpec instance).
    content = label_input.content
    assert content is not None, "label_input.content must not be None"
    for line in content:
        text_height = (
            line.text_height or label_input.text_height or job.text_height or DEFAULT_TEXT_HEIGHT
        )
        char_spacing = (
            line.character_spacing
            or label_input.character_spacing
            or job.character_spacing
            or DEFAULT_CHAR_SPACING
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
                text_height=text_height,
                character_spacing=char_spacing,
                line_spacing=line_spacing,
            )
        )
    return resolved_content


def _resolve_label(
    label_input: LabelSpec | JobSpec,
    job: JobSpec,
) -> ResolvedLabel:
    """Resolve a single label (or root-level job) into a ResolvedLabel.

    Args:
        label_input: The label (or root-level job) being processed.
        job: The outer JobSpec providing fallback values.

    Returns:
        A fully resolved label with all dimensions guaranteed non-None.
    """
    # Generate ID if this is a root-level job masquerading as a label
    label_id: str = getattr(label_input, "id", None) or f"label_{uuid.uuid4().hex[:8]}"
    label_count: int = getattr(label_input, "count", 1) or 1

    # Resolve label-level styles (Label -> Job -> Fallback)
    label_margin: float = label_input.margin or job.margin or DEFAULT_MARGIN

    # Resolve text lines
    resolved_content = _resolve_content(label_input, job)

    # Resolve holes
    resolved_holes = _resolve_holes(label_input, job)

    # Execute auto-sizing calculations
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


def resolve_job_spec(job: JobSpec) -> list[ResolvedLabel]:
    """Flatten a JobSpec into a list of fully resolved labels.

    Handles both the explicit ``labels`` form and the root-level
    ``content``/``count`` form. For root-level jobs, a synthetic ID is
    generated.

    Args:
        job: The parsed and validated JobSpec.

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

    return [_resolve_label(label_input, job) for label_input in labels_to_process]
