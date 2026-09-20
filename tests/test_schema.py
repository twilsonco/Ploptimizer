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
    DEFAULT_HOLE_DIAMETER,
    HoleLocation,
    HoleSpec,
    JobSpec,
    LabelAttributes,
    LabelSpec,
    PlateSpec,
    TextAttributes,
    TextHAlignment,
    TextLine,
    parse_yaml,
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
            HoleSpec(location="center")  # type: ignore

    def test_location_only_hole_uses_default_diameter(self) -> None:
        """A hole specifying only ``location`` gets the default diameter."""
        hole = HoleSpec(location="top-left")
        assert hole.location.value == "top-left"
        assert hole.diameter == DEFAULT_HOLE_DIAMETER == 0.125

    def test_explicit_diameter_overrides_default(self) -> None:
        """An explicit ``diameter`` wins over the default."""
        hole = HoleSpec(location="left", diameter=0.25)
        assert hole.diameter == 0.25

    def test_location_is_required(self) -> None:
        """Omitting ``location`` should raise ValidationError."""
        with pytest.raises(ValidationError):
            HoleSpec(diameter=0.125)  # type: ignore

    def test_non_positive_diameter_rejected(self) -> None:
        """Zero or negative diameters should raise ValidationError."""
        with pytest.raises(ValidationError):
            HoleSpec(location="left", diameter=0.0)
        with pytest.raises(ValidationError):
            HoleSpec(location="left", diameter=-0.125)

    def test_group_locations_accepted(self) -> None:
        """The ``corners`` and ``sides`` group shorthands are valid locations."""
        assert HoleSpec(location="corners").location is HoleLocation.CORNERS
        assert HoleSpec(location="sides").location is HoleLocation.SIDES


class TestHoleLocationGroupExpansion:
    """Tests for ``corners`` / ``sides`` group hole expansion."""

    def test_expand_atomic_returns_self(self) -> None:
        """An atomic location expands to exactly itself."""
        hole = HoleSpec(location="top-left")
        assert hole.expand() == [hole]

    def test_expand_corners_yields_four_atoms_in_order(self) -> None:
        """``corners`` expands to the four atomic corner locations."""
        holes = HoleSpec(location="corners").expand()
        assert [h.location for h in holes] == [
            HoleLocation.TOP_LEFT,
            HoleLocation.TOP_RIGHT,
            HoleLocation.BOTTOM_LEFT,
            HoleLocation.BOTTOM_RIGHT,
        ]
        assert all(h.diameter == DEFAULT_HOLE_DIAMETER for h in holes)

    def test_expand_sides_yields_left_right(self) -> None:
        """``sides`` expands to the left and right edge locations."""
        holes = HoleSpec(location="sides").expand()
        assert [h.location for h in holes] == [HoleLocation.LEFT, HoleLocation.RIGHT]

    def test_expand_propagates_diameter(self) -> None:
        """Every expanded member inherits the group spec's diameter."""
        holes = HoleSpec(location="corners", diameter=0.25).expand()
        assert len(holes) == 4
        assert all(h.diameter == 0.25 for h in holes)

    def test_label_holes_expand_in_place(self) -> None:
        """Group entries are replaced in place, preserving list order."""
        label = LabelSpec(
            id="lbl",
            content=[TextLine(text="X")],
            holes=[
                HoleSpec(location="top"),
                HoleSpec(location="sides"),
                HoleSpec(location="bottom"),
            ],
        )
        assert label.holes is not None
        assert [h.location for h in label.holes] == [
            HoleLocation.TOP,
            HoleLocation.LEFT,
            HoleLocation.RIGHT,
            HoleLocation.BOTTOM,
        ]

    def test_job_holes_expand_in_place(self) -> None:
        """Job-level group holes expand like label-level ones."""
        job = JobSpec(
            job_name="J",
            content=[TextLine(text="X")],
            holes=[HoleSpec(location="corners")],
        )
        assert job.holes is not None
        assert [h.location for h in job.holes] == [
            HoleLocation.TOP_LEFT,
            HoleLocation.TOP_RIGHT,
            HoleLocation.BOTTOM_LEFT,
            HoleLocation.BOTTOM_RIGHT,
        ]

    def test_atomic_and_empty_holes_unchanged(self) -> None:
        """Atomic holes and ``holes: []`` suppression pass through untouched."""
        label = LabelSpec(
            id="lbl",
            content=[TextLine(text="X")],
            holes=[HoleSpec(location="left")],
        )
        assert label.holes is not None and len(label.holes) == 1
        empty = LabelSpec(id="lbl2", content=[TextLine(text="X")], holes=[])
        assert empty.holes == []

    def test_complex_yaml_group_holes_expand(self) -> None:
        """complex_test_job.yaml group holes expand to atomic members on parse."""
        job = parse_yaml(Path("examples/complex_test_job.yaml"))
        labels = {label.id: label for label in job.labels or []}

        valve = labels["valve_tag"]
        assert valve.holes is not None
        assert [h.location for h in valve.holes] == [HoleLocation.LEFT, HoleLocation.RIGHT]

        group = labels["group_holes"]
        assert group.holes is not None
        assert [h.location for h in group.holes] == [
            HoleLocation.TOP_LEFT,
            HoleLocation.TOP_RIGHT,
            HoleLocation.BOTTOM_LEFT,
            HoleLocation.BOTTOM_RIGHT,
            HoleLocation.LEFT,
            HoleLocation.RIGHT,
        ]
        # The corners group carries an explicit 0.25in override; sides stays default.
        assert all(h.diameter == 0.25 for h in group.holes[:4])
        assert all(h.diameter == DEFAULT_HOLE_DIAMETER for h in group.holes[4:])


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
        # sample_spec.yaml uses location-only holes: default diameter applies.
        assert all(h.diameter == DEFAULT_HOLE_DIAMETER for h in label.holes)

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

    def test_empty_yaml_document_raises(self, tmp_path: Path) -> None:
        """A completely empty YAML document must raise ValueError."""
        spec_path = tmp_path / "empty.yaml"
        spec_path.write_text("", encoding="utf-8")

        with pytest.raises(ValueError, match="Empty YAML document"):
            parse_yaml(spec_path)

    def test_comment_only_yaml_document_raises(self, tmp_path: Path) -> None:
        """A comment-only document loads as None and must raise the same error."""
        spec_path = tmp_path / "comments.yaml"
        spec_path.write_text("# just a comment\n", encoding="utf-8")

        with pytest.raises(ValueError, match="Empty YAML document"):
            parse_yaml(spec_path)

    def test_missing_job_root_element_raises(self, tmp_path: Path) -> None:
        """A YAML mapping without a top-level 'job' key must raise ValueError."""
        spec_path = tmp_path / "no_job.yaml"
        spec_path.write_text("not_a_job:\n  job_name: 'Oops'\n", encoding="utf-8")

        with pytest.raises(ValueError, match="Missing 'job' root element"):
            parse_yaml(spec_path)

    def test_null_job_root_element_raises(self, tmp_path: Path) -> None:
        """An explicit null 'job' value must raise the missing-root error."""
        spec_path = tmp_path / "null_job.yaml"
        spec_path.write_text("job:\n", encoding="utf-8")

        with pytest.raises(ValueError, match="Missing 'job' root element"):
            parse_yaml(spec_path)


class TestAllowRotation:
    """Tests for the job-level allow_rotation packing flag."""

    def test_defaults_to_true(self) -> None:
        """Jobs may rotate labels unless explicitly disabled."""
        job = JobSpec(job_name="Rot", count=1, content=[TextLine(text="X")])
        assert job.allow_rotation is True

    def test_parsed_from_yaml(self, tmp_path: Path) -> None:
        """allow_rotation: false must be honored from YAML."""
        spec_path = tmp_path / "no_rot.yaml"
        spec_path.write_text(
            "job:\n"
            "  job_name: 'No Rotation'\n"
            "  allow_rotation: false\n"
            "  count: 1\n"
            "  content:\n"
            "    - text: 'X'\n",
            encoding="utf-8",
        )
        job = parse_yaml(spec_path)
        assert job.allow_rotation is False

    def test_explicit_true_parsed(self, tmp_path: Path) -> None:
        """An explicit allow_rotation: true parses like the default."""
        spec_path = tmp_path / "rot.yaml"
        spec_path.write_text(
            "job:\n"
            "  job_name: 'Rotation'\n"
            "  allow_rotation: true\n"
            "  count: 1\n"
            "  content:\n"
            "    - text: 'X'\n",
            encoding="utf-8",
        )
        job = parse_yaml(spec_path)
        assert job.allow_rotation is True


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


class TestMaxHCompress:
    """Tests for the max_h_compress horizontal compression field."""

    def test_text_line_accepts_max_h_compress(self) -> None:
        """TextLine should expose max_h_compress via TextAttributes."""
        line = TextLine(text="HELLO", max_h_compress=0.5)
        assert math.isclose(line.max_h_compress, 0.5)

    def test_default_is_none(self) -> None:
        """max_h_compress defaults to None (inherit from parent)."""
        assert TextLine(text="X").max_h_compress is None
        assert LabelSpec(id="lbl", content=[TextLine(text="X")]).max_h_compress is None
        assert JobSpec(job_name="J", content=[TextLine(text="X")]).max_h_compress is None

    def test_explicit_zero_is_accepted(self) -> None:
        """max_h_compress=0.0 (compression disabled) is a valid explicit value."""
        assert math.isclose(TextLine(text="X", max_h_compress=0.0).max_h_compress, 0.0)

    def test_above_one_rejected(self) -> None:
        """Values above 1.0 must be rejected by the le=1.0 constraint."""
        with pytest.raises(ValidationError):
            TextLine(text="X", max_h_compress=1.5)

    def test_negative_rejected(self) -> None:
        """Negative values must be rejected by the ge=0.0 constraint."""
        with pytest.raises(ValidationError):
            TextLine(text="X", max_h_compress=-0.1)

    def test_plate_accepts_max_h_compress(self) -> None:
        """PlateSpec should accept max_h_compress for schema parity."""
        plate = PlateSpec(
            id="plate_1",
            width=24.0,
            height=12.0,
            margin=0.25,
            clearance_padding=0.125,
            max_h_compress=0.6,
        )
        assert math.isclose(plate.max_h_compress, 0.6)

    def test_plate_rejects_out_of_range(self) -> None:
        """PlateSpec must enforce the [0.0, 1.0] range too."""
        with pytest.raises(ValidationError):
            PlateSpec(
                id="plate_1",
                width=24.0,
                height=12.0,
                margin=0.25,
                clearance_padding=0.125,
                max_h_compress=1.2,
            )


class TestTextHAlignment:
    """Tests for the text_h_alignment horizontal alignment field."""

    def test_text_line_accepts_all_alignments(self) -> None:
        """TextLine should accept left/center/right via TextAttributes."""
        for value in ("left", "center", "right"):
            line = TextLine(text="HELLO", text_h_alignment=value)
            assert line.text_h_alignment is TextHAlignment(value)

    def test_accepts_enum_member_directly(self) -> None:
        """Enum members are accepted as well as raw strings."""
        line = TextLine(text="HELLO", text_h_alignment=TextHAlignment.RIGHT)
        assert line.text_h_alignment is TextHAlignment.RIGHT

    def test_default_is_none(self) -> None:
        """text_h_alignment defaults to None (inherit from parent)."""
        assert TextLine(text="X").text_h_alignment is None
        assert LabelSpec(id="lbl", content=[TextLine(text="X")]).text_h_alignment is None
        assert JobSpec(job_name="J", content=[TextLine(text="X")]).text_h_alignment is None

    def test_invalid_value_rejected(self) -> None:
        """Values outside the enum must fail validation."""
        with pytest.raises(ValidationError):
            TextLine(text="X", text_h_alignment="justify")

    def test_inherited_on_all_levels(self) -> None:
        """LabelSpec and JobSpec expose the field via the attribute mixins."""
        label = LabelSpec(id="lbl", text_h_alignment="left", content=[TextLine(text="X")])
        assert label.text_h_alignment is TextHAlignment.LEFT
        job = JobSpec(
            job_name="J",
            text_h_alignment="right",
            content=[TextLine(text="X")],
        )
        assert job.text_h_alignment is TextHAlignment.RIGHT

    def test_plate_accepts_text_h_alignment(self) -> None:
        """PlateSpec should accept text_h_alignment for schema parity."""
        plate = PlateSpec(
            id="plate_1",
            width=24.0,
            height=12.0,
            margin=0.25,
            clearance_padding=0.125,
            text_h_alignment="left",
        )
        assert plate.text_h_alignment is TextHAlignment.LEFT


class TestMinHoleMargin:
    """Tests for the min_hole_margin collision-avoidance floor field."""

    def test_inherited_on_all_levels(self) -> None:
        """TextLine, LabelSpec and JobSpec expose min_hole_margin."""
        assert math.isclose(TextLine(text="X", min_hole_margin=0.05).min_hole_margin, 0.05)
        label = LabelSpec(id="lbl", min_hole_margin=0.1, content=[TextLine(text="X")])
        assert math.isclose(label.min_hole_margin, 0.1)
        job = JobSpec(job_name="J", min_hole_margin=0.2, content=[TextLine(text="X")])
        assert math.isclose(job.min_hole_margin, 0.2)

    def test_default_is_none(self) -> None:
        """min_hole_margin defaults to None (no floor; inherit from parent)."""
        assert TextLine(text="X").min_hole_margin is None
        assert LabelSpec(id="lbl", content=[TextLine(text="X")]).min_hole_margin is None
        assert JobSpec(job_name="J", content=[TextLine(text="X")]).min_hole_margin is None

    def test_explicit_zero_is_accepted(self) -> None:
        """min_hole_margin=0.0 (shrink to tangent) is a valid explicit value."""
        assert math.isclose(TextLine(text="X", min_hole_margin=0.0).min_hole_margin, 0.0)

    def test_negative_rejected(self) -> None:
        """Negative values must be rejected by the ge=0.0 constraint."""
        with pytest.raises(ValidationError):
            TextLine(text="X", min_hole_margin=-0.1)

    def test_plate_accepts_min_hole_margin(self) -> None:
        """PlateSpec should accept min_hole_margin for schema parity."""
        plate = PlateSpec(
            id="plate_1",
            width=24.0,
            height=12.0,
            margin=0.25,
            clearance_padding=0.125,
            min_hole_margin=0.05,
        )
        assert math.isclose(plate.min_hole_margin, 0.05)

    def test_plate_rejects_negative(self) -> None:
        """PlateSpec must enforce the >= 0.0 range too."""
        with pytest.raises(ValidationError):
            PlateSpec(
                id="plate_1",
                width=24.0,
                height=12.0,
                margin=0.25,
                clearance_padding=0.125,
                min_hole_margin=-0.1,
            )


class TestHoleTextCollisionDistance:
    """Tests for the hole_text_collision_distance stroke-clearance field."""

    def test_inherited_on_all_levels(self) -> None:
        """TextLine, LabelSpec and JobSpec expose the field."""
        line = TextLine(text="X", hole_text_collision_distance=0.2)
        assert math.isclose(line.hole_text_collision_distance, 0.2)
        label = LabelSpec(id="lbl", hole_text_collision_distance=0.3, content=[TextLine(text="X")])
        assert math.isclose(label.hole_text_collision_distance, 0.3)
        job = JobSpec(job_name="J", hole_text_collision_distance=0.4, content=[TextLine(text="X")])
        assert math.isclose(job.hole_text_collision_distance, 0.4)

    def test_default_is_none(self) -> None:
        """Schema default is None (inherit); resolution applies 0.15."""
        assert TextLine(text="X").hole_text_collision_distance is None
        label = LabelSpec(id="lbl", content=[TextLine(text="X")])
        assert label.hole_text_collision_distance is None
        job = JobSpec(job_name="J", content=[TextLine(text="X")])
        assert job.hole_text_collision_distance is None

    def test_explicit_zero_is_accepted(self) -> None:
        """hole_text_collision_distance=0.0 (strokes may touch) is valid."""
        assert math.isclose(
            TextLine(text="X", hole_text_collision_distance=0.0).hole_text_collision_distance, 0.0
        )

    def test_negative_rejected(self) -> None:
        """Negative values must be rejected by the ge=0.0 constraint."""
        with pytest.raises(ValidationError):
            TextLine(text="X", hole_text_collision_distance=-0.1)

    def test_plate_accepts_field_for_parity(self) -> None:
        """PlateSpec should accept the field for schema parity."""
        plate = PlateSpec(
            id="plate_1",
            width=24.0,
            height=12.0,
            margin=0.25,
            clearance_padding=0.125,
            hole_text_collision_distance=0.2,
        )
        assert math.isclose(plate.hole_text_collision_distance, 0.2)

    def test_plate_rejects_negative(self) -> None:
        """PlateSpec must enforce the >= 0.0 range too."""
        with pytest.raises(ValidationError):
            PlateSpec(
                id="plate_1",
                width=24.0,
                height=12.0,
                margin=0.25,
                clearance_padding=0.125,
                hole_text_collision_distance=-0.1,
            )
