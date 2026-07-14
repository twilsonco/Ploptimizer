"""Schema definitions for YAML job specification parsing and validation.

This module provides Pydantic models that define the data contract for
job specifications used by the generate pipeline. It handles:
- Parsing YAML files into typed Python objects
- Validating text height inheritance rules
- Auto-sizing label dimensions when omitted

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
from pydantic import BaseModel, Field, field_validator


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


class StyleMixin(BaseModel):
    """Optional styling constraints that can be defined at the Job, Label, or Line level.

    By standardizing these fields in a single mixin, the YAML hierarchy
    supports top-down inheritance: values defined at a higher level (Job)
    propagate down to lower levels (Label, TextLine) unless overridden
    locally. Cross-level resolution is handled by the generation pipeline
    rather than at schema validation time.

    Attributes:
        text_height: Optional font height in inches.
        margin: Optional margin in inches (label or layout context).
        character_spacing: Optional extra spacing between characters in inches.
        line_spacing: Optional extra spacing between text lines in inches.
        holes: Optional list of hole specifications.
    """

    text_height: Optional[float] = None
    margin: Optional[float] = None
    character_spacing: Optional[float] = None
    line_spacing: Optional[float] = None
    holes: Optional[list[HoleSpec]] = None


class TextLine(StyleMixin):
    """A single line of text content within a label.

    Attributes:
        text: The actual text string to render.
        text_height: Optional font height in inches. Inherits from parent
            LabelSpec.text_height or JobSpec.text_height if not set locally.
    """

    text: str


class LabelSpec(StyleMixin):
    """Specification for a label to be generated.

    Inherits optional styling fields (text_height, margin, character_spacing,
    line_spacing, holes) from StyleMixin so they can be set at the Label
    level and overridden at the TextLine level.

    Attributes:
        id: Unique identifier for this label specification.
        count: Number of instances to produce.
        width: Width in inches. If None, auto-calculated by the generation
            pipeline based on content.
        height: Height in inches. If None, auto-calculated by the generation
            pipeline based on content.
        content: List of text lines to render on the label.
    """

    id: str
    count: int = Field(ge=1, description="Number of instances to produce (must be >= 1).")
    width: Optional[float] = Field(default=None, ge=0.0)
    height: Optional[float] = Field(default=None, ge=0.0)
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


class JobSpec(StyleMixin):
    """Top-level specification for a batch label generation job.

    Inherits optional styling fields from StyleMixin so they can be set at
    the Job level and inherited down to Label and TextLine levels.

    Attributes:
        job_name: Human-readable name for this job.
        plates: Optional list of plate specifications. If omitted, the
            generation pipeline auto-allocates default 24x16 sheets.
        labels: List of unique label specifications to produce.
    """

    job_name: str
    plates: Optional[list[PlateSpec]] = None
    labels: list[LabelSpec]


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
