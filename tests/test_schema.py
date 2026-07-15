"""Tests for the YAML job specification schema module.

This test suite validates:
- Successful parsing of YAML specification files
- HoleLocation enum string validation
- Two-tier mixin inheritance (TextAttributes, LabelAttributes)
- Optional plates field on JobSpec
- Root-level single-label job support
- Mutual exclusion of `labels` and root-level `content`
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from plt_optimizer.generate.schema import (
    HoleLocation,
    HoleSpec,
    JobSpec,
    LabelAttributes,
    LabelSpec,
    parse_yaml,
    PlateSpec,
    TextAttributes,
    TextLine,
)


class TestHoleLocationEnum:
    """Tests for the HoleLocation enumeration."""

    def test_valid_locations(self) -> None:
        """All valid location strings should be accepted."""
        valid_locations = [
            "left",
            "right",
            "top",
            "bottom",
            "top-left",
            "top-right",
            "bottom-left",
            "bottom-right",
        ]
        for loc in valid_locations:
            hole = HoleSpec(diameter=0.125, location=loc)
            assert hole.location.value == loc

    def test_invalid_location_rejected(self) -> None:
        """Invalid location strings should raise ValidationError."""
        with pytest.raises(ValidationError):
            HoleSpec(diameter=0.125, location="center")  # type: ignore


class TestTextAttributes:
    """Tests for the TextAttributes mixin and TextLine inheritance."""

    def test_text_line_inherits_text_attributes(self) -> None:
        """TextLine should expose all TextAttributes fields."""
        line = TextLine(text="HELLO", text_height=0.5, character_spacing=0.05, line_spacing=0.1)
        assert line.text == "HELLO"
        assert line.text_height == 0.5
        assert line.character_spacing == 0.05
        assert line.line_spacing == 0.1

    def test_text_line_does_not_have_label_attributes(self) -> None:
        """TextLine must NOT inherit label-container fields."""
        line = TextLine(text="X")
        assert "width" not in line.model_fields
        assert "height" not in line.model_fields
        assert "margin" not in line.model_fields
        assert "holes" not in line.model_fields


class TestLabelAttributes:
    """Tests for the LabelAttributes mixin and LabelSpec inheritance."""

    def test_label_inherits_text_attributes(self) -> None:
        """LabelSpec should expose TextAttributes fields."""
        label = LabelSpec(
            id="lbl",
            count=1,
            content=[TextLine(text="X")],
            text_height=0.4,
            character_spacing=0.05,
            line_spacing=0.1,
        )
        assert label.text_height == 0.4
        assert label.character_spacing == 0.05
        assert label.line_spacing == 0.1

    def test_label_inherits_label_attributes(self) -> None:
        """LabelSpec should expose LabelAttributes fields."""
        label = LabelSpec(
            id="lbl",
            count=1,
            width=2.0,
            height=1.0,
            margin=0.1,
            holes=[HoleSpec(diameter=0.125, location="left")],
            content=[TextLine(text="X")],
        )
        assert label.width == 2.0
        assert label.height == 1.0
        assert label.margin == 0.1
        assert label.holes is not None and len(label.holes) == 1

    def test_label_count_defaults_to_one(self) -> None:
        """LabelSpec.count should default to 1."""
        label = LabelSpec(
            id="lbl",
            content=[TextLine(text="X")],
        )
        assert label.count == 1


class TestJobSpec:
    """Tests for JobSpec model."""

    def test_valid_job_with_labels(self) -> None:
        """A complete job specification with labels should parse."""
        job = JobSpec(
            job_name="Test Job",
            plates=[
                PlateSpec(
                    id="p1",
                    width=24.0,
                    height=12.0,
                    margin=0.25,
                    clearance_padding=0.125,
                ),
            ],
            labels=[
                LabelSpec(
                    id="l1",
                    count=5,
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="Test")],
                ),
            ],
        )
        assert job.job_name == "Test Job"
        assert job.plates is not None and len(job.plates) == 1
        assert job.labels is not None and len(job.labels) == 1

    def test_valid_job_with_root_content(self) -> None:
        """A job with root-level content/count should parse without labels."""
        job = JobSpec(
            job_name="Root Content Job",
            width=3.0,
            height=1.5,
            text_height=0.25,
            count=10,
            content=[
                TextLine(text="DANGER", text_height=0.5),
                TextLine(text="HIGH VOLTAGE"),
            ],
        )
        assert job.job_name == "Root Content Job"
        assert job.labels is None
        assert job.count == 10
        assert job.content is not None and len(job.content) == 2
        assert job.width == 3.0
        assert job.height == 1.5
        assert job.text_height == 0.25

    def test_job_inherits_label_attributes(self) -> None:
        """JobSpec should expose LabelAttributes fields."""
        job = JobSpec(
            job_name="Test",
            width=24.0,
            height=12.0,
            margin=0.25,
            holes=[HoleSpec(diameter=0.125, location="top")],
            labels=[
                LabelSpec(
                    id="l1",
                    count=1,
                    content=[TextLine(text="X")],
                ),
            ],
        )
        assert job.width == 24.0
        assert job.height == 12.0
        assert job.margin == 0.25
        assert job.holes is not None and len(job.holes) == 1

    def test_plates_optional(self) -> None:
        """Plates list is optional; backend can auto-allocate defaults."""
        job = JobSpec(
            job_name="No Plates Job",
            labels=[
                LabelSpec(
                    id="l1",
                    count=1,
                    content=[TextLine(text="Test")],
                ),
            ],
        )
        assert job.plates is None
        assert job.labels is not None and len(job.labels) == 1

    def test_neither_labels_nor_content_fails(self) -> None:
        """Job must define either labels or root-level content."""
        with pytest.raises(ValidationError) as exc_info:
            JobSpec(job_name="Empty Job")
        assert "Job must define either 'labels' or root-level 'content'" in str(exc_info.value)

    def test_both_labels_and_content_fails(self) -> None:
        """Job cannot define both labels and root-level content."""
        with pytest.raises(ValidationError) as exc_info:
            JobSpec(
                job_name="Conflict Job",
                labels=[
                    LabelSpec(
                        id="l1",
                        count=1,
                        content=[TextLine(text="X")],
                    ),
                ],
                content=[TextLine(text="Y")],
            )
        assert "Job cannot define both 'labels' and root-level 'content'" in str(exc_info.value)

    def test_empty_labels_and_content_fails(self) -> None:
        """Empty labels and empty content should fail."""
        with pytest.raises(ValidationError):
            JobSpec(
                job_name="Empty Job",
                labels=[],
                content=[],
            )

    def test_multiple_labels(self) -> None:
        """Job can contain multiple label specifications."""
        job = JobSpec(
            job_name="Multi-Label Job",
            plates=[
                PlateSpec(
                    id="p1",
                    width=24.0,
                    height=12.0,
                    margin=0.25,
                    clearance_padding=0.125,
                ),
            ],
            labels=[
                LabelSpec(
                    id="l1",
                    count=3,
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="Label 1")],
                ),
                LabelSpec(
                    id="l2",
                    count=7,
                    width=3.0,
                    height=1.5,
                    content=[
                        TextLine(text="Label 2 Line 1", text_height=0.6),
                        TextLine(text="Line 2 Here"),
                    ],
                ),
            ],
        )
        assert job.labels is not None and len(job.labels) == 2


class TestParseYaml:
    """Tests for YAML file parsing."""

    def test_parse_sample_spec_success(self) -> None:
        """The sample specification should parse successfully."""
        spec_path = Path("examples/sample_spec.yaml")
        job = parse_yaml(spec_path)

        assert job.job_name == "Control Panel Tags - Batch 01"
        assert job.plates is not None and len(job.plates) == 1
        assert job.labels is not None and len(job.labels) == 1

    def test_parse_plate_properties(self) -> None:
        """Plate properties should be correctly parsed."""
        spec_path = Path("examples/sample_spec.yaml")
        job = parse_yaml(spec_path)

        plate = job.plates[0]
        assert plate.id == "plate_1"
        assert plate.width == 24.0
        assert plate.height == 12.0
        assert plate.margin == 0.25
        assert plate.clearance_padding == 0.125

    def test_parse_label_with_holes(self) -> None:
        """Label with holes should parse correctly."""
        spec_path = Path("examples/sample_spec.yaml")
        job = parse_yaml(spec_path)

        label = job.labels[0]
        assert label.id == "pump_warn_01"
        assert label.count == 5
        assert len(label.content) == 2
        assert label.holes is not None and len(label.holes) == 2

    def test_parse_nonexistent_file_raises(self) -> None:
        """Parsing non-existent file should raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            parse_yaml("nonexistent/path/spec.yaml")

    def test_text_height_parsed_from_sample_spec(self) -> None:
        """Text lines in sample spec should expose text_height."""
        spec_path = Path("examples/sample_spec.yaml")
        job = parse_yaml(spec_path)

        label = job.labels[0]
        # First line has explicit text_height, second has no height (no inheritance at schema level)
        assert label.content[0].text_height == 0.5
        assert label.content[1].text_height is None


class TestLabelSpecValidation:
    """Additional LabelSpec validation edge cases."""

    def test_holes_optional(self) -> None:
        """Holes list is optional and can be omitted."""
        label = LabelSpec(
            id="test_label",
            count=3,
            width=2.0,
            height=1.0,
            content=[TextLine(text="Simple")],
        )
        assert label.holes is None

    def test_empty_content_fails(self) -> None:
        """Empty content list should fail validation."""
        with pytest.raises(ValidationError):
            LabelSpec(
                id="test_label",
                count=1,
                width=2.0,
                height=1.0,
                content=[],  # Empty - must have at least one line
            )

    def test_count_must_be_positive(self) -> None:
        """Count must be a positive integer."""
        with pytest.raises(ValidationError):
            LabelSpec(
                id="test_label",
                count=0,  # Must be >= 1
                width=2.0,
                height=1.0,
                content=[TextLine(text="Test")],
            )


class TestPlateSpec:
    """Tests for PlateSpec model."""

    def test_valid_plate(self) -> None:
        """A valid plate specification should parse."""
        plate = PlateSpec(
            id="plate_1",
            width=24.0,
            height=12.0,
            margin=0.25,
            clearance_padding=0.125,
        )
        assert plate.id == "plate_1"
        assert math.isclose(plate.width, 24.0)

    def test_negative_dimensions_rejected(self) -> None:
        """Negative dimensions should be rejected."""
        with pytest.raises(ValidationError):
            PlateSpec(
                id="invalid_plate",
                width=-10.0,
                height=12.0,
                margin=0.25,
                clearance_padding=0.125,
            )


class TestMixinHierarchy:
    """Tests verifying the two-tier inheritance hierarchy."""

    def test_text_attributes_is_base(self) -> None:
        """TextAttributes should be a direct BaseModel subclass."""
        assert issubclass(TextAttributes, BaseModel)

    def test_label_attributes_inherits_text_attributes(self) -> None:
        """LabelAttributes should inherit from TextAttributes."""
        assert issubclass(LabelAttributes, TextAttributes)

    def test_text_line_inherits_text_attributes(self) -> None:
        """TextLine should inherit from TextAttributes only."""
        assert issubclass(TextLine, TextAttributes)
        assert not issubclass(TextLine, LabelAttributes)

    def test_label_spec_inherits_label_attributes(self) -> None:
        """LabelSpec should inherit from LabelAttributes."""
        assert issubclass(LabelSpec, LabelAttributes)

    def test_job_spec_inherits_label_attributes(self) -> None:
        """JobSpec should inherit from LabelAttributes."""
        assert issubclass(JobSpec, LabelAttributes)