"""Tests for the YAML job specification schema module.

This test suite validates:
- Successful parsing of YAML specification files
- HoleLocation enum string validation
- Text height inheritance across lines
- Height consistency validation rules
- Auto-sizing calculations when dimensions are omitted
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from plt_optimizer.generate.schema import (
    HoleLocation,
    HoleSpec,
    JobSpec,
    LabelSpec,
    parse_yaml,
    PlateSpec,
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


class TestTextHeightInheritance:
    """Tests for text height inheritance validation."""

    def test_single_defined_height_propagates(self) -> None:
        """When exactly one line defines height, it should propagate to all."""
        label = LabelSpec(
            id="test_label",
            count=1,
            content=[
                TextLine(text="WARNING", height=0.5),
                TextLine(text="HIGH PRESSURE"),  # Should inherit 0.5
            ],
        )
        # Both lines should now have height 0.5
        assert label.content[0].height == 0.5
        assert label.content[1].height == 0.5

    def test_all_lines_define_height_no_inheritance(self) -> None:
        """When all lines define height, no inheritance needed."""
        label = LabelSpec(
            id="test_label",
            count=1,
            content=[
                TextLine(text="LINE1", height=0.3),
                TextLine(text="LINE2", height=0.4),
            ],
        )
        assert label.content[0].height == 0.3
        assert label.content[1].height == 0.4

    def test_no_lines_define_height_requires_label_height(self) -> None:
        """When no lines define height, label.height must be specified."""
        with pytest.raises(ValidationError) as exc_info:
            LabelSpec(
                id="test_label",
                count=1,
                content=[
                    TextLine(text="LINE1"),
                    TextLine(text="LINE2"),
                ],
            )
        assert "If no text line specifies a height" in str(exc_info.value)

    def test_multiple_heights_partial_definition_fails(self) -> None:
        """When multiple lines define heights but not all, should fail."""
        with pytest.raises(ValidationError) as exc_info:
            LabelSpec(
                id="test_label",
                count=1,
                content=[
                    TextLine(text="LINE1", height=0.3),
                    TextLine(text="LINE2"),  # Missing height
                    TextLine(text="LINE3", height=0.5),  # Multiple defined
                ],
            )
        assert "If multiple lines specify height" in str(exc_info.value)


class TestAutoSizing:
    """Tests for auto-sizing validation."""

    def test_missing_width_auto_calculated(self) -> None:
        """When width is missing, it should be auto-calculated."""
        label = LabelSpec(
            id="test_label",
            count=1,
            # width omitted
            height=0.5,
            content=[
                TextLine(text="WARNING", height=0.5),
                TextLine(text="HIGH PRESSURE", height=0.5),
            ],
        )
        assert label.width is not None
        # Rounded up to nearest 0.25: len("WARNING") * 0.6 + 0.5 = 7 * 0.3 + 0.5 = 2.6 -> ceil(10.4)/4 = 2.75?
        # Actually: max(len*height*0.6) where height=0.5
        # len("HIGH PRESSURE") = 12, 12*0.5*0.6+0.5 = 4.1 -> ceil(16.4)/4 = 4.25
        assert label.width > 0

    def test_missing_height_uses_first_line(self) -> None:
        """When height is missing, first line's height should be used."""
        label = LabelSpec(
            id="test_label",
            count=1,
            # height omitted (but valid since one content line has it)
            width=2.0,
            content=[
                TextLine(text="WARNING", height=0.5),
                TextLine(text="HIGH PRESSURE"),  # Should inherit
            ],
        )
        assert label.height == 0.5

    def test_width_rounded_to_nearest_quarter(self) -> None:
        """Auto-calculated width should be rounded up to nearest 0.25."""
        label = LabelSpec(
            id="test_label",
            count=1,
            height=0.4,  # Use consistent height
            content=[
                TextLine(text="ABC", height=0.4),
            ],
        )
        # len("ABC") * 0.4 * 0.6 + 0.5 = 3*0.24+0.5 = 1.22 -> ceil(4.88)/4 = 1.25
        assert label.width == 1.25


class TestParseYaml:
    """Tests for YAML file parsing."""

    def test_parse_sample_spec_success(self) -> None:
        """The sample specification should parse successfully."""
        spec_path = Path("examples/sample_spec.yaml")
        job = parse_yaml(spec_path)

        assert job.job_name == "Control Panel Tags - Batch 01"
        assert len(job.plates) == 1
        assert len(job.labels) == 1

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
        assert len(label.holes) == 2

    def test_parse_nonexistent_file_raises(self) -> None:
        """Parsing non-existent file should raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            parse_yaml("nonexistent/path/spec.yaml")

    def test_text_height_inheritance_from_sample_spec(self) -> None:
        """Text lines in sample spec should have inherited heights."""
        spec_path = Path("examples/sample_spec.yaml")
        job = parse_yaml(spec_path)

        label = job.labels[0]
        # First line has explicit height, second should inherit
        assert label.content[0].height == 0.5
        assert label.content[1].height == 0.5


class TestLabelSpecValidation:
    """Additional LabelSpec validation edge cases."""

    def test_holes_optional(self) -> None:
        """Holes list is optional and can be omitted."""
        label = LabelSpec(
            id="test_label",
            count=3,
            width=2.0,
            height=1.0,
            content=[TextLine(text="Simple", height=0.5)],
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
                content=[TextLine(text="Test", height=0.5)],
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


class TestJobSpec:
    """Tests for JobSpec model."""

    def test_valid_job_spec(self) -> None:
        """A complete job specification should parse."""
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
                    content=[TextLine(text="Test", height=0.5)],
                ),
            ],
        )
        assert job.job_name == "Test Job"
        assert len(job.plates) == 1
        assert len(job.labels) == 1

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
                    content=[TextLine(text="Label 1", height=0.5)],
                ),
                LabelSpec(
                    id="l2",
                    count=7,
                    width=3.0,
                    height=1.5,
                    content=[
                        TextLine(text="Label 2 Line 1", height=0.6),
                        TextLine(text="Line 2 Here"),
                    ],
                ),
            ],
        )
        assert len(job.labels) == 2
        # Second label should have inherited height
        assert job.labels[1].content[1].height == 0.6
