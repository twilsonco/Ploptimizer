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
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class HoleLocation(str, Enum):
    """Enumeration of valid hole locations on a label.

    Besides the eight atomic positions (four edges and four corners), two
    group shorthands are accepted for the most common drilling patterns:
    ``corners`` (all four corner holes) and ``sides`` (the left and right
    edge holes). Groups are expanded into their atomic members at schema
    validation time (see :data:`HOLE_LOCATION_GROUPS`), so downstream
    consumers only ever see the eight atomic locations.

    Attributes:
        left: Hole on the left edge.
        right: Hole on the right edge.
        top: Hole on the top edge.
        bottom: Hole on the bottom edge.
        top_left: Hole at the top-left corner.
        top_right: Hole at the top-right corner.
        bottom_left: Hole at the bottom-left corner.
        bottom_right: Hole at the bottom-right corner.
        corners: Group shorthand for all four corner holes.
        sides: Group shorthand for the left and right edge holes.
    """

    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BOTTOM = "bottom"
    TOP_LEFT = "top-left"
    TOP_RIGHT = "top-right"
    BOTTOM_LEFT = "bottom-left"
    BOTTOM_RIGHT = "bottom-right"
    CORNERS = "corners"
    SIDES = "sides"


# Group shorthand locations mapped to the atomic locations they expand to.
# Expansion preserves the order listed here and replaces the group entry
# in place within the ``holes`` list (see :meth:`HoleSpec.expand`).
HOLE_LOCATION_GROUPS: dict[HoleLocation, tuple[HoleLocation, ...]] = {
    HoleLocation.CORNERS: (
        HoleLocation.TOP_LEFT,
        HoleLocation.TOP_RIGHT,
        HoleLocation.BOTTOM_LEFT,
        HoleLocation.BOTTOM_RIGHT,
    ),
    HoleLocation.SIDES: (HoleLocation.LEFT, HoleLocation.RIGHT),
}


# Default drill-hole diameter (inches) used when a hole specification omits
# ``diameter``. 0.125" is the common drill size across the example jobspecs.
DEFAULT_HOLE_DIAMETER: float = 0.125


class HoleSpec(BaseModel):
    """Specification for a hole to be drilled in a label.

    ``location`` is the primary (and only required) field: the common case
    is a standard 0.125" drill hole, which can be written as ``location``
    alone. ``diameter`` is an optional override for non-standard sizes.

    Group locations (``corners`` / ``sides``) are expanded into their
    atomic member holes at validation time by the ``holes`` field
    validator on :class:`LabelAttributes`; each expanded member inherits
    this spec's ``diameter``.

    Attributes:
        location: The position of the hole on the label edge. Group
            shorthands ``corners`` (all four corners) and ``sides``
            (left + right) are accepted and expanded into their members.
        diameter: The diameter of the hole in inches. Defaults to
            :data:`DEFAULT_HOLE_DIAMETER` (0.125"); must be positive.
    """

    location: HoleLocation
    diameter: float = Field(default=DEFAULT_HOLE_DIAMETER, gt=0.0)

    def expand(self) -> list[HoleSpec]:
        """Expand a group location into its atomic hole specifications.

        Returns:
            A single-element list containing this spec for atomic
            locations; for group locations (``corners`` / ``sides``), one
            clone per member location (in :data:`HOLE_LOCATION_GROUPS`
            order), each preserving this spec's diameter.
        """
        members = HOLE_LOCATION_GROUPS.get(self.location)
        if members is None:
            return [self]
        return [self.model_copy(update={"location": member}) for member in members]


class TextHAlignment(str, Enum):
    """Enumeration of valid horizontal text alignment modes for a text line.

    The alignment anchors the rendered line within the label's inner content
    area (label width minus both margins). ``left`` places the line's
    left-most point precisely at the left margin; ``right`` places the
    line's right-most point precisely at the right margin; ``center``
    (the default) centers the line horizontally.

    Attributes:
        LEFT: Left edge of the line sits at the left margin.
        CENTER: Line is horizontally centered within the inner content area.
        RIGHT: Right edge of the line sits at the right margin.
    """

    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"


class TextAttributes(BaseModel):
    """Attributes that can cascade down to individual text lines.

    These fields are safe to inherit at the TextLine level because they
    describe typographic properties that apply to rendered glyphs.

    Attributes:
        text_height: Optional font height in inches.
        character_spacing: Optional extra spacing between characters in inches.
        line_spacing: Optional extra spacing between text lines in inches.
        max_h_compress: Optional maximum horizontal compression fraction in
            ``[0.0, 1.0]``. When a rendered line is wider than the label's
            inner content area, the line may be uniformly compressed
            horizontally down to ``(1 - max_h_compress)`` of its natural
            width. ``0.0`` (the default) disables compression; ``0.5`` allows
            squeezing wide lines to 50% of their natural width. Cascades
            line -> label -> job (and is accepted on plates for schema
            parity, where it is not currently applied during rendering).
        text_h_alignment: Optional horizontal alignment of a rendered text
            line within the label's inner content area. One of ``left``,
            ``center`` or ``right``. ``left`` places the line's left-most
            point precisely at the left margin; ``right`` places the right-
            most point at the right margin; ``center`` (the default)
            centers the line. Cascades line -> label -> job (and is
            accepted on plates for schema parity, where it is not currently
            applied during rendering).
        min_hole_margin: Optional minimum hole margin in inches; during
            text-hole collision avoidance, hole margins will not shrink
            below this value. ``None`` (the default) means collision
            avoidance may reduce the hole margin all the way to ``0.0``
            (hole tangent to the label edge). Cascades line -> label ->
            job (and is accepted on plates for schema parity, where it is
            not currently applied during rendering).
        hole_text_collision_distance: Optional minimum air gap in inches
            between the *engraved* text stroke and the *engraved* drill
            hole stroke. A (line, hole) pair collides when the geometric
            gap between the text bounding box and the hole circle is
            below ``0.5 * (hole_cutter + text_cutter) +
            hole_text_collision_distance``: the first term is the stroke
            floor at which the two cut strokes just touch, and this field
            adds free air between them. Defaults to ``0.15`` inches; an
            explicit ``0.0`` is honored (strokes may touch but never
            overlap). Cascades label -> job (and is accepted on text
            lines and plates for schema parity, where it is not applied
            at that level).
    """

    text_height: Optional[float] = None
    character_spacing: Optional[float] = None
    line_spacing: Optional[float] = None
    max_h_compress: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Maximum horizontal compression fraction in [0.0, 1.0].",
    )
    text_h_alignment: Optional[TextHAlignment] = Field(
        default=None,
        description="Horizontal text alignment: left, center, or right.",
    )
    min_hole_margin: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Minimum hole margin in inches; hole margins will not shrink "
            "below this value during collision avoidance."
        ),
    )
    hole_text_collision_distance: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Minimum air gap in inches between engraved text and drill "
            "hole strokes, on top of the stroke floor "
            "0.5 * (hole_cutter + text_cutter)."
        ),
    )


class LabelAttributes(TextAttributes):
    """Attributes that cascade down to labels.

    Extends TextAttributes with physical label dimensions and layout
    properties. These fields must NOT be inherited by TextLine because
    they describe the label container, not individual glyphs.

    Attributes:
        width: Optional label width in inches.
        height: Optional label height in inches.
        margin: Optional margin in inches.
        hole_margin: Optional hole margin in inches. The closest point of a
            hole circle to the label edge will be this far from the edge.
            Cascades job -> plate -> label (label overrides plate overrides
            job).
        holes: Optional list of hole specifications. Group locations
            (``corners`` / ``sides``) are expanded in place into their
            atomic member holes at validation time.
    """

    width: Optional[float] = Field(default=None, ge=0.0)
    height: Optional[float] = Field(default=None, ge=0.0)
    margin: Optional[float] = Field(default=None, ge=0.0)
    hole_margin: Optional[float] = Field(default=None, ge=0.0)
    holes: Optional[list[HoleSpec]] = None

    @field_validator("holes", mode="after")
    @classmethod
    def _expand_hole_location_groups(
        cls, value: Optional[list[HoleSpec]]
    ) -> Optional[list[HoleSpec]]:
        """Expand group hole locations (``corners`` / ``sides``) into members.

        Applies to every model inheriting the ``holes`` field (LabelSpec,
        JobSpec). Group entries are replaced in place by their atomic
        member holes; atomic entries pass through unchanged, so drill
        geometry and ordering semantics for existing specs are untouched.

        Args:
            value: The validated hole list, or ``None`` (unset).

        Returns:
            The expanded hole list (or ``None`` / empty list unchanged).
        """
        if not value:
            return value
        return [hole for spec in value for hole in spec.expand()]


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

    A label may be defined in one of two ways:
    1. Statically, with a ``content`` list (and optional ``count``).
    2. As a template driven by an EngraveLab/Vision Pro-style replacement
       text file (``replacement_text_file``). Each line of the file
       produces one label instance; delimited items within a line replace
       the template's text lines. In this mode ``count`` must not be set
       (the file's line count determines it) and ``content`` is optional:
       when omitted, every instance's lines inherit the label-level text
       attributes; when present, its ``text`` values act as placeholders
       declaring the per-line attributes (text height, alignment, etc.).

    Attributes:
        id: Unique identifier for this label specification.
        count: Number of instances to produce. Defaults to 1. Mutually
            exclusive with ``replacement_text_file``.
        content: List of text lines to render on the label. Required unless
            ``replacement_text_file`` is provided.
        replacement_text_file: Path to a replacement text file (resolved
            relative to the job YAML file's directory unless absolute).
            Each line produces one label instance; items within a line are
            separated by ``replacement_text_delimiter``.
        replacement_text_delimiter: Single special or whitespace character
            separating text items within a replacement file line. Defaults
            to ``";"``. Cannot be a newline or an alphanumeric character.
    """

    id: str
    count: int = Field(
        ge=1, default=1, description="Number of instances to produce (must be >= 1)."
    )
    content: Optional[list[TextLine]] = Field(
        default=None,
        description=("Text lines to render. Required unless replacement_text_file is provided."),
    )
    replacement_text_file: Optional[str] = Field(
        default=None,
        description=(
            "Path to an EngraveLab/Vision Pro-style replacement text file. "
            "Each line produces one label instance. Mutually exclusive with count."
        ),
    )
    replacement_text_delimiter: Optional[str] = Field(
        default=None,
        description=(
            "Single-character delimiter separating text items within a "
            "replacement file line (default ';')."
        ),
    )

    @field_validator("replacement_text_delimiter")
    @classmethod
    def _validate_replacement_delimiter(cls, v: Optional[str]) -> Optional[str]:
        """Ensure the replacement delimiter is a single non-alphanumeric char.

        Args:
            v: The configured delimiter, or None (unset).

        Returns:
            The validated delimiter.

        Raises:
            ValueError: If the delimiter is not exactly one character, is a
                newline, or is alphanumeric.
        """
        if v is None:
            return v
        if len(v) != 1:
            raise ValueError("replacement_text_delimiter must be a single character")
        if v in ("\n", "\r"):
            raise ValueError("replacement_text_delimiter cannot be a newline character")
        if v.isalnum():
            raise ValueError(
                "replacement_text_delimiter must be a single special or whitespace character"
            )
        return v

    @model_validator(mode="after")
    def _validate_content_or_replacement(self) -> LabelSpec:
        """Enforce the static-content vs replacement-template contract.

        Raises:
            ValueError: If both ``count`` and ``replacement_text_file`` are
                set, if neither ``content`` nor ``replacement_text_file`` is
                set, if ``content`` is an empty list, or if
                ``replacement_text_delimiter`` is set without a file.

        Returns:
            Self for method chaining.
        """
        if self.replacement_text_file is not None and "count" in self.model_fields_set:
            raise ValueError(
                f"label '{self.id}': 'count' cannot be combined with "
                "'replacement_text_file' (the file's line count determines the count)"
            )
        if self.replacement_text_delimiter is not None and self.replacement_text_file is None:
            raise ValueError(
                f"label '{self.id}': 'replacement_text_delimiter' requires "
                "'replacement_text_file' to be set"
            )
        if self.content is None and self.replacement_text_file is None:
            raise ValueError(
                f"label '{self.id}': must define either 'content' or 'replacement_text_file'"
            )
        if self.content is not None and len(self.content) == 0:
            raise ValueError("content must contain at least one TextLine")
        return self


class PlateSpec(BaseModel):
    """Specification for a plate (material sheet) to cut labels from.

    Attributes:
        id: Unique identifier for this plate specification.
        width: Total width of the plate in inches.
        height: Total height of the plate in inches.
        margin: Safety margin around plate edges in inches.
        hole_margin: Optional hole margin in inches. Accepted for schema
            parity with the job/label ``hole_margin`` cascade. NOTE: labels
            are rendered once and cached before bin-packing (and a single
            label may span multiple plates), so a per-plate value is not
            currently applied during rendering; the effective value is
            resolved from the label -> job -> default cascade.
        clearance_padding: Padding between adjacent labels in inches.
        max_h_compress: Optional maximum horizontal compression fraction.
            Accepted for schema parity with the job/label ``max_h_compress``
            cascade. NOTE: labels are rendered once and cached before
            bin-packing (and a single label may span multiple plates), so a
            per-plate value is not currently applied during rendering; the
            effective value is resolved from the label -> job -> default
            cascade.
        text_h_alignment: Optional horizontal text alignment. Accepted for
            schema parity with the job/label ``text_h_alignment`` cascade.
            NOTE: labels are rendered once and cached before bin-packing
            (and a single label may span multiple plates), so a per-plate
            value is not currently applied during rendering; the effective
            value is resolved from the label -> job -> default cascade.
        min_hole_margin: Optional minimum hole margin in inches. Accepted
            for schema parity with the job/label ``min_hole_margin``
            cascade. NOTE: labels are rendered once and cached before
            bin-packing (and a single label may span multiple plates), so
            a per-plate value is not currently applied during rendering;
            the effective value is resolved from the label -> job ->
            default cascade.
        hole_text_collision_distance: Optional minimum engraved-stroke
            air gap in inches. Accepted for schema parity with the
            job/label ``hole_text_collision_distance`` cascade. NOTE:
            labels are rendered once and cached before bin-packing, so a
            per-plate value is not currently applied during rendering;
            the effective value is resolved from the label -> job ->
            default cascade.
    """

    id: str
    width: float = Field(ge=0.0, description="Plate width in inches (must be >= 0).")
    height: float = Field(ge=0.0, description="Plate height in inches (must be >= 0).")
    margin: float = Field(ge=0.0, description="Safety margin in inches (must be >= 0).")
    hole_margin: Optional[float] = Field(
        default=None, ge=0.0, description="Hole margin in inches (must be >= 0)."
    )
    max_h_compress: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Maximum horizontal compression fraction in [0.0, 1.0].",
    )
    text_h_alignment: Optional[TextHAlignment] = Field(
        default=None,
        description="Horizontal text alignment (schema parity; not applied at plate level).",
    )
    min_hole_margin: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Minimum hole margin in inches (schema parity; not applied at plate level).",
    )
    hole_text_collision_distance: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Minimum engraved-stroke air gap in inches (schema parity; not applied at plate level)."
        ),
    )
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
        allow_rotation: Whether the bin packer may rotate label instances
            90 degrees to improve plate utilization. When a rotated label
            is assembled onto a plate its whole content (text, border and
            drill holes) is rotated clockwise. Defaults to True.
        text_chunk_mode: Granularity at which rendered text becomes a
            plate-space optimization node: ``"line"`` (the default) routes
            each whole text line as one unit; ``"word"`` splits lines on
            whitespace for finer rapid-travel routing at the cost of more
            TSP nodes.
    """

    job_name: str
    plates: Optional[list[PlateSpec]] = None

    # Allow either a list of labels, or a root-level label definition
    labels: Optional[list[LabelSpec]] = None
    count: Optional[int] = Field(default=None, ge=1)
    content: Optional[list[TextLine]] = None

    allow_rotation: bool = Field(
        default=True,
        description=(
            "Allow the bin packer to rotate labels 90 degrees (clockwise at "
            "assembly) for tighter layouts. Set false to keep every label "
            "horizontal."
        ),
    )
    text_chunk_mode: Literal["line", "word"] = Field(
        default="line",
        description=(
            "Plate-space text optimization granularity: 'line' routes each "
            "text line as one node (default); 'word' routes each "
            "whitespace-delimited word separately."
        ),
    )

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
