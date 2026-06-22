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

import math
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


class TextLine(BaseModel):
    """A single line of text content within a label.

    Attributes:
        text: The actual text string to render.
        height: Optional font height in inches. If None, inherits from
            parent LabelSpec.height or another defined line's height.
    """

    text: str
    height: Optional[float] = None


class LabelSpec(BaseModel):
    """Specification for a label to be generated.

    Attributes:
        id: Unique identifier for this label specification.
        count: Number of instances to produce.
        width: Width in inches. If None, auto-calculated from content.
        height: Height in inches. Required if no TextLine specifies height.
        content: List of text lines to render on the label.
        holes: Optional list of hole specifications.

    Validators:
        - Text heights must be consistent across all lines (inheritance).
        - Missing dimensions trigger auto-sizing calculations.
    """

    id: str
    count: int = Field(ge=1, description="Number of instances to produce (must be >= 1).")
    width: Optional[float] = Field(default=None, ge=0.0)
    height: Optional[float] = Field(default=None, ge=0.0)
    content: list[TextLine] = Field(min_length=1, description="At least one text line is required.")
    holes: Optional[list[HoleSpec]] = None

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

    @model_validator(mode="after")
    def validate_text_heights(self) -> LabelSpec:
        """Validate and propagate text heights across all TextLine objects.

        Rules:
        - If no lines specify height, label.height must be defined.
        - If exactly one line specifies height, it propagates to all lines.
        - If multiple lines specify height, all lines must define it.

        Raises:
            ValueError: If height rules are violated.

        Returns:
            Self for method chaining.
        """
        # Count how many lines have explicit heights
        lines_with_height = [line for line in self.content if line.height is not None]
        count_defined = len(lines_with_height)

        if count_defined == 0:
            # No lines define height - label must provide it
            if self.height is None:
                raise ValueError(
                    "If no text line specifies a height, the label.height "
                    "attribute must be defined."
                )
        elif count_defined == 1:
            # One line defines height - propagate to all lines
            inherited_height = lines_with_height[0].height
            for line in self.content:
                if line.height is None:
                    line.height = inherited_height
        else:
            # Multiple lines define height - must be consistent or all defined
            all_defined = all(line.height is not None for line in self.content)

            if not all_defined:
                raise ValueError("If multiple lines specify height, all lines must specify it.")

        return self

    @model_validator(mode="after")
    def validate_auto_sizing(self) -> LabelSpec:
        """Auto-calculate width and height when omitted from specification.

        This validator applies a temporary auto-sizing calculation based on
        text content. The actual implementation will use vpype text engine
        bounding boxes in Phase 3 for proper typographic calculations.

        Current stub formula:
            width ≈ max(len(text) * height * 0.6) + 0.5

        Dimensions are rounded up to the nearest 0.25 inch increment.

        Returns:
            Self with populated width and height if previously None.
        """
        # Calculate height if not defined
        if self.height is None:
            # Use first text line's height as reference (already validated)
            self.height = self.content[0].height
            if self.height is None:
                raise ValueError("Label height could not be determined for auto-sizing.")

        # Calculate width if not defined
        if self.width is None:
            max_char_width = 0.0
            for line in self.content:
                char_count = len(line.text)
                line_height = line.height if line.height is not None else self.height
                estimated_width = char_count * line_height * 0.6 + 0.5
                max_char_width = max(max_char_width, estimated_width)

            # Round up to nearest 0.25
            self.width = math.ceil(max_char_width * 4) / 4

        return self


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


class JobSpec(BaseModel):
    """Top-level specification for a batch label generation job.

    Attributes:
        job_name: Human-readable name for this job.
        plates: List of plate specifications defining the cutting substrate.
        labels: List of unique label specifications to produce.
    """

    job_name: str
    plates: list[PlateSpec]
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
