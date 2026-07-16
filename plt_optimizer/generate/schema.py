"""Schema definitions for YAML job specification parsing and validation.

This module provides Pydantic models that define the data contract for
job specifications used by the generate pipeline. It handles:
- Parsing YAML files into typed Python objects
- Top-down inheritance via two-tier mixins (TextAttributes, LabelAttributes)
- Root-level single-label jobs (no explicit `labels` list required)

Example:
    >>> job = parse_yaml("examples/sample_spec.yaml")
    >>> print(job.job_name)
    'Control Panel Tags - Batch 01'
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class HoleLocation(str, Enum):
    """Enumeration of valid hole locations on a label.

    Attributes:
        left: Hole on the left edge.
        right: Hole on the right edge.
        top: Hole on the top edge.
        bottom: Hole on the bottom edge.
        top_left: Hole at the top-left corner.
        top_right: Hole at the top-right corner.
        bottom_left: Hole at the bottom-left corner.
        bottom_right: Hole at the bottom-right corner.
    """

    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BOTTOM = "bottom"
    TOP_LEFT = "top-left"
    TOP_RIGHT = "top-right"
    BOTTOM_LEFT = "bottom-left"
    BOTTOM_RIGHT = "bottom-right"


class HoleSpec(BaseModel):
    """Specification for a hole to be drilled in a label.

    Attributes:
        diameter: The diameter of the hole in inches.
        location: The position of the hole on the label edge.
    """

    diameter: float
    location: HoleLocation


class TextAttributes(BaseModel):
    """Attributes that can cascade down to individual text lines.

    These fields are safe to inherit at the TextLine level because they
    describe typographic properties that apply to rendered glyphs.

    Attributes:
        text_height: Optional font height in inches.
        character_spacing: Optional extra spacing between characters in inches.
        line_spacing: Optional extra spacing between text lines in inches.
    """

    text_height: Optional[float] = None
    character_spacing: Optional[float] = None
    line_spacing: Optional[float] = None


class LabelAttributes(TextAttributes):
    """Attributes that cascade down to labels.

    Extends TextAttributes with physical label dimensions and layout
    properties. These fields must NOT be inherited by TextLine because
    they describe the label container, not individual glyphs.

    Attributes:
        width: Optional label width in inches.
        height: Optional label height in inches.
        margin: Optional margin in inches.
        holes: Optional list of hole specifications.
    """

    width: Optional[float] = Field(default=None, ge=0.0)
    height: Optional[float] = Field(default=None, ge=0.0)
    margin: Optional[float] = Field(default=None, ge=0.0)
    holes: Optional[list[HoleSpec]] = None


class TextLine(TextAttributes):
    """A single line of text content within a label.

    Attributes:
        text: The actual text string to render.
        text_height: Optional font height in inches. Inherits from parent
            LabelSpec.text_height or JobSpec.text_height if not set locally.
    """

    text: str


class LabelSpec(LabelAttributes):
    """Specification for a label to be generated.

    Inherits optional styling fields (text_height, character_spacing,
    line_spacing, width, height, margin, holes) from LabelAttributes.

    Attributes:
        id: Unique identifier for this label specification.
        count: Number of instances to produce. Defaults to 1.
        content: List of text lines to render on the label.
    """

    id: str
    count: int = Field(
        ge=1, default=1, description="Number of instances to produce (must be >= 1)."
    )
    content: list[TextLine] = Field(min_length=1, description="At least one text line is required.")

    @field_validator("content")
    @classmethod
    def _validate_content_non_empty(cls, v: list[TextLine]) -> list[TextLine]:
        """Ensure the label has at least one text line.

        Args:
            v: The content list to validate.

        Returns:
            The validated content list.

        Raises:
            ValueError: If the content list is empty.
        """
        if len(v) == 0:
            raise ValueError("content must contain at least one TextLine")
        return v


class PlateSpec(BaseModel):
    """Specification for a plate (material sheet) to cut labels from.

    Attributes:
        id: Unique identifier for this plate specification.
        width: Total width of the plate in inches.
        height: Total height of the plate in inches.
        margin: Safety margin around plate edges in inches.
        clearance_padding: Padding between adjacent labels in inches.
    """

    id: str
    width: float = Field(ge=0.0, description="Plate width in inches (must be >= 0).")
    height: float = Field(ge=0.0, description="Plate height in inches (must be >= 0).")
    margin: float = Field(ge=0.0, description="Safety margin in inches (must be >= 0).")
    clearance_padding: float = Field(
        ge=0.0, description="Padding between labels in inches (must be >= 0)."
    )


class JobSpec(LabelAttributes):
    """Top-level specification for a batch label generation job.

    Inherits optional styling fields from LabelAttributes so they can be
    set at the Job level and inherited down to Label and TextLine levels.

    A job may be specified in one of two equivalent forms:
    1. A list of explicit labels (`labels`).
    2. A single root-level label definition (`content` + optional `count`).

    The two forms are mutually exclusive; exactly one must be provided.

    Attributes:
        job_name: Human-readable name for this job.
        plates: Optional list of plate specifications. If omitted, the
            generation pipeline auto-allocates default 24x16 sheets.
        labels: Optional list of unique label specifications to produce.
        count: Optional count for root-level single-label jobs.
        content: Optional root-level content for single-label jobs.
    """

    job_name: str
    plates: Optional[list[PlateSpec]] = None

    # Allow either a list of labels, or a root-level label definition
    labels: Optional[list[LabelSpec]] = None
    count: Optional[int] = Field(default=None, ge=1)
    content: Optional[list[TextLine]] = None

    @model_validator(mode="after")
    def validate_job_structure(self) -> JobSpec:
        """Ensure exactly one of `labels` or root-level `content` is provided.

        Raises:
            ValueError: If neither or both forms are specified.

        Returns:
            Self for method chaining.
        """
        has_labels = self.labels is not None and len(self.labels) > 0
        has_content = self.content is not None and len(self.content) > 0

        if not has_labels and not has_content:
            raise ValueError("Job must define either 'labels' or root-level 'content'.")
        if has_labels and has_content:
            raise ValueError("Job cannot define both 'labels' and root-level 'content'.")

        return self


def parse_yaml(file_path: str | Path) -> JobSpec:
    """Parse and validate a YAML job specification file.

    Args:
        file_path: Path to the YAML specification file.

    Returns:
        A validated JobSpec instance with all nested models populated.

    Raises:
        FileNotFoundError: If the specified file does not exist.
        ValueError: If the YAML content is invalid or fails validation.
        yaml.YAMLError: For malformed YAML syntax.

    Example:
        >>> job = parse_yaml("examples/sample_spec.yaml")
        >>> print(f"Loaded {job.job_name}")
    """
    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"Specification file not found: {path}")

    with open(path, encoding="utf-8") as f:
        raw_data = yaml.safe_load(f)

    # Validate top-level structure
    if raw_data is None:
        raise ValueError("Empty YAML document")

    job_data = raw_data.get("job")
    if job_data is None:
        raise ValueError("Missing 'job' root element")

    return JobSpec(**job_data)
