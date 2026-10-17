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
    DEFAULT_LAYOUT_MODE,
    HoleLocation,
    HoleSpec,
    JobSpec,
    LabelAttributes,
    LabelShape,
    LabelSpec,
    LayoutMode,
    PlateSpec,
    TextAttributes,
    TextColor,
    TextHAlignment,
    TextLine,
    material_key,
    normalize_material,
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
            width=2.0,
            height=1.0,
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
        job = parse_yaml(Path("tests_deps/complex_test_job.yaml"))
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
                    left_clearance=0.25,
                    top_clearance=0.25,
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
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="Test")],
                ),
            ],
        )
        assert job.plates is None
        assert job.labels is not None and len(job.labels) == 1

    def test_neither_labels_nor_content_fails(self) -> None:
        """Job must define labels, root-level content, or a replacement file."""
        with pytest.raises(ValidationError) as exc_info:
            JobSpec(job_name="Empty Job")
        assert "must define 'labels', root-level 'content', or a" in str(exc_info.value)

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
        assert "cannot define more than one of" in str(exc_info.value)

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
                    left_clearance=0.25,
                    top_clearance=0.25,
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
        spec_path = Path("tests_deps/sample_spec.yaml")
        job = parse_yaml(spec_path)

        assert job.job_name == "Control Panel Tags - Batch 01"
        assert job.plates is not None and len(job.plates) == 1
        assert job.labels is not None and len(job.labels) == 1

    def test_parse_plate_properties(self) -> None:
        """Plate properties should be correctly parsed."""
        spec_path = Path("tests_deps/sample_spec.yaml")
        job = parse_yaml(spec_path)

        plate = job.plates[0]
        assert plate.id == "plate_1"
        assert plate.width == 24.0
        assert plate.height == 12.0
        assert math.isclose(plate.left_clearance, 0.25)
        assert math.isclose(plate.top_clearance, 0.25)

    def test_parse_label_with_holes(self) -> None:
        """Label with holes should parse correctly."""
        spec_path = Path("tests_deps/sample_spec.yaml")
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
        spec_path = Path("tests_deps/sample_spec.yaml")
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
        job = JobSpec(job_name="Rot", width=2.0, height=1.0, count=1, content=[TextLine(text="X")])
        assert job.allow_rotation is True

    def test_parsed_from_yaml(self, tmp_path: Path) -> None:
        """allow_rotation: false must be honored from YAML."""
        spec_path = tmp_path / "no_rot.yaml"
        spec_path.write_text(
            "job:\n"
            "  job_name: 'No Rotation'\n"
            "  width: 2.0\n"
            "  height: 1.0\n"
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
            "  width: 2.0\n"
            "  height: 1.0\n"
            "  allow_rotation: true\n"
            "  count: 1\n"
            "  content:\n"
            "    - text: 'X'\n",
            encoding="utf-8",
        )
        job = parse_yaml(spec_path)
        assert job.allow_rotation is True


class TestTextChunkMode:
    """Tests for the job-level text_chunk_mode optimization flag."""

    def test_defaults_to_line(self) -> None:
        """Chunk mode defaults to 'line' (fewer optimizer nodes)."""
        job = JobSpec(job_name="TCM", width=2.0, height=1.0, count=1, content=[TextLine(text="X")])
        assert job.text_chunk_mode == "line"

    def test_parsed_from_yaml(self, tmp_path: Path) -> None:
        """text_chunk_mode: word must be honored from YAML."""
        spec_path = tmp_path / "word.yaml"
        spec_path.write_text(
            "job:\n"
            "  job_name: 'Word Mode'\n"
            "  width: 2.0\n"
            "  height: 1.0\n"
            "  text_chunk_mode: word\n"
            "  count: 1\n"
            "  content:\n"
            "    - text: 'X'\n",
            encoding="utf-8",
        )
        job = parse_yaml(spec_path)
        assert job.text_chunk_mode == "word"

    def test_invalid_mode_rejected(self, tmp_path: Path) -> None:
        """Only 'line' and 'word' are valid chunk modes."""
        spec_path = tmp_path / "bad.yaml"
        spec_path.write_text(
            "job:\n"
            "  job_name: 'Bad Mode'\n"
            "  width: 2.0\n"
            "  height: 1.0\n"
            "  text_chunk_mode: glyph\n"
            "  count: 1\n"
            "  content:\n"
            "    - text: 'X'\n",
            encoding="utf-8",
        )
        with pytest.raises((ValidationError, ValueError)):
            parse_yaml(spec_path)


class TestLayoutMode:
    """Tests for the job-level layout fill-order enum."""

    def test_default_is_columns(self) -> None:
        """Fill order defaults to column-major (fill height first)."""
        job = JobSpec(job_name="LM", width=2.0, height=1.0, count=1, content=[TextLine(text="X")])
        assert job.layout is LayoutMode.COLUMNS
        assert DEFAULT_LAYOUT_MODE is LayoutMode.COLUMNS

    def test_explicit_rows_parsed(self, tmp_path: Path) -> None:
        """layout: rows must be honored from YAML."""
        spec_path = tmp_path / "rows.yaml"
        spec_path.write_text(
            "job:\n  job_name: 'Rows'\n  width: 2.0\n  height: 1.0\n  layout: rows\n  count: 1\n  content:\n    - text: 'X'\n",
            encoding="utf-8",
        )
        job = parse_yaml(spec_path)
        assert job.layout is LayoutMode.ROWS

    def test_explicit_columns_parsed(self, tmp_path: Path) -> None:
        """layout: columns parses like the default."""
        spec_path = tmp_path / "cols.yaml"
        spec_path.write_text(
            "job:\n"
            "  job_name: 'Cols'\n"
            "  width: 2.0\n"
            "  height: 1.0\n"
            "  layout: columns\n"
            "  count: 1\n"
            "  content:\n"
            "    - text: 'X'\n",
            encoding="utf-8",
        )
        job = parse_yaml(spec_path)
        assert job.layout is LayoutMode.COLUMNS

    def test_invalid_mode_rejected(self, tmp_path: Path) -> None:
        """Only 'rows' and 'columns' are valid fill orders."""
        spec_path = tmp_path / "bad_layout.yaml"
        spec_path.write_text(
            "job:\n"
            "  job_name: 'Bad'\n"
            "  layout: diagonal\n"
            "  count: 1\n"
            "  content:\n"
            "    - text: 'X'\n",
            encoding="utf-8",
        )
        with pytest.raises((ValidationError, ValueError)):
            parse_yaml(spec_path)

    def test_plate_layout_defaults_to_none(self) -> None:
        """A plate without an explicit layout inherits the job value."""
        plate = PlateSpec(id="p1", width=24.0, height=16.0)
        assert plate.layout is None

    def test_plate_layout_override_parsed(self, tmp_path: Path) -> None:
        """A per-plate layout override must be honored from YAML."""
        spec_path = tmp_path / "plate_layout.yaml"
        spec_path.write_text(
            "job:\n"
            "  job_name: 'PlateLayout'\n"
            "  width: 2.0\n"
            "  height: 1.0\n"
            "  layout: columns\n"
            "  plates:\n"
            "    - id: p1\n"
            "      width: 24.0\n"
            "      height: 16.0\n"
            "      layout: rows\n"
            "  count: 1\n"
            "  content:\n"
            "    - text: 'X'\n",
            encoding="utf-8",
        )
        job = parse_yaml(spec_path)
        assert job.layout is LayoutMode.COLUMNS
        assert job.plates is not None
        assert job.plates[0].layout is LayoutMode.ROWS


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
            left_clearance=0.25,
            top_clearance=0.25,
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
                left_clearance=0.25,
                top_clearance=0.25,
            )

    def test_clearances_default_to_zero(self) -> None:
        """left_clearance/top_clearance default to 0.0 (flush packing)."""
        plate = PlateSpec(id="p1", width=24.0, height=16.0)
        assert math.isclose(plate.left_clearance, 0.0)
        assert math.isclose(plate.top_clearance, 0.0)

    def test_clearances_are_independent(self) -> None:
        """The two clearances are set independently of each other."""
        plate = PlateSpec(
            id="p1",
            width=24.0,
            height=16.0,
            left_clearance=0.75,
        )
        assert math.isclose(plate.left_clearance, 0.75)
        assert math.isclose(plate.top_clearance, 0.0)

    def test_negative_clearances_rejected(self) -> None:
        """Both clearance fields enforce ge=0.0."""
        with pytest.raises(ValidationError):
            PlateSpec(id="p1", width=24.0, height=16.0, left_clearance=-0.1)
        with pytest.raises(ValidationError):
            PlateSpec(id="p1", width=24.0, height=16.0, top_clearance=-0.1)

    def test_plate_without_optional_fields_parses(self) -> None:
        """A plate needs nothing beyond id, width and height."""
        plate = PlateSpec(id="p1", width=24.0, height=16.0)
        assert plate.id == "p1"
        assert math.isclose(plate.left_clearance, 0.0)
        assert math.isclose(plate.top_clearance, 0.0)


class TestJobLevelClearances:
    """Job-level left_clearance/top_clearance cascade onto plates."""

    @staticmethod
    def _job(plates: list[PlateSpec], **kwargs: object) -> JobSpec:
        """Build a minimal job with the given plates and job-level overrides."""
        return JobSpec(
            job_name="Clearance Job",
            width=2.0,
            height=1.0,
            plates=plates,
            labels=[LabelSpec(id="l1", width=2.0, height=1.0, content=[TextLine(text="X")])],
            **kwargs,  # type: ignore[arg-type]
        )

    def test_job_fields_default_to_none(self) -> None:
        """Job-level clearances default to None (no job-level default)."""
        job = JobSpec(job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")])
        assert job.left_clearance is None
        assert job.top_clearance is None

    def test_negative_rejected(self) -> None:
        """Both job-level clearance fields enforce ge=0.0."""
        with pytest.raises(ValidationError):
            JobSpec(
                job_name="J",
                width=2.0,
                height=1.0,
                content=[TextLine(text="X")],
                left_clearance=-0.1,
            )
        with pytest.raises(ValidationError):
            JobSpec(
                job_name="J",
                width=2.0,
                height=1.0,
                content=[TextLine(text="X")],
                top_clearance=-0.1,
            )

    def test_job_value_cascades_to_plates_omitting_them(self) -> None:
        """Plates without explicit clearances inherit the job-level pair."""
        job = self._job(
            [PlateSpec(id="p1", width=24.0, height=16.0)],
            left_clearance=1.0,
            top_clearance=2.0,
        )
        assert job.plates is not None
        assert math.isclose(job.plates[0].left_clearance, 1.0)
        assert math.isclose(job.plates[0].top_clearance, 2.0)

    def test_explicit_plate_value_wins(self) -> None:
        """An explicit plate clearance always beats the job-level value."""
        job = self._job(
            [PlateSpec(id="p1", width=24.0, height=16.0, left_clearance=0.25)],
            left_clearance=1.0,
            top_clearance=2.0,
        )
        assert job.plates is not None
        assert math.isclose(job.plates[0].left_clearance, 0.25)
        assert math.isclose(job.plates[0].top_clearance, 2.0)

    def test_explicit_plate_zero_wins(self) -> None:
        """An explicit plate ``0.0`` is a value, not unset (job 1.0 refused)."""
        job = self._job(
            [PlateSpec(id="p1", width=24.0, height=16.0, left_clearance=0.0)],
            left_clearance=1.0,
        )
        assert job.plates is not None
        assert math.isclose(job.plates[0].left_clearance, 0.0)

    def test_plate_null_inherits_job_value(self) -> None:
        """An explicit plate ``null`` clearance is unset semantics: inherit."""
        job = JobSpec(
            job_name="J",
            width=2.0,
            height=1.0,
            left_clearance=1.5,
            plates=[{"id": "p1", "width": 24.0, "height": 16.0, "left_clearance": None}],
            labels=[LabelSpec(id="l1", width=2.0, height=1.0, content=[TextLine(text="X")])],
        )
        assert job.plates is not None
        assert math.isclose(job.plates[0].left_clearance, 1.5)
        assert math.isclose(job.plates[0].top_clearance, 0.0)

    def test_no_job_value_keeps_plate_default(self) -> None:
        """Without a job-level value, plates keep their own 0.0 default."""
        job = self._job([PlateSpec(id="p1", width=24.0, height=16.0)])
        assert job.plates is not None
        assert math.isclose(job.plates[0].left_clearance, 0.0)
        assert math.isclose(job.plates[0].top_clearance, 0.0)

    def test_input_plate_objects_not_mutated(self) -> None:
        """The cascade clones plates instead of mutating the caller's objects."""
        plate = PlateSpec(id="p1", width=24.0, height=16.0)
        self._job([plate], left_clearance=1.0)
        assert math.isclose(plate.left_clearance, 0.0)

    def test_parse_yaml_cascades_job_level_clearance(self, tmp_path: Path) -> None:
        """A YAML job-level clearance reaches clearance-less plates."""
        spec_path = tmp_path / "spec.yaml"
        spec_path.write_text(
            "job:\n"
            "  job_name: Cascade Job\n"
            "  width: 2.0\n"
            "  height: 1.0\n"
            "  left_clearance: 0.75\n"
            "  top_clearance: 0.5\n"
            "  plates:\n"
            "    - id: p1\n"
            "      width: 24.0\n"
            "      height: 16.0\n"
            "    - id: p2\n"
            "      width: 24.0\n"
            "      height: 16.0\n"
            "      left_clearance: 0.1\n"
            "  labels:\n"
            "    - id: l1\n"
            "      width: 2.0\n"
            "      height: 1.0\n"
            "      content:\n"
            "        - text: Hi\n",
            encoding="utf-8",
        )
        job = parse_yaml(spec_path)
        assert job.plates is not None
        assert math.isclose(job.plates[0].left_clearance, 0.75)
        assert math.isclose(job.plates[0].top_clearance, 0.5)
        # p2 declares its own left clearance; the job value fills only top.
        assert math.isclose(job.plates[1].left_clearance, 0.1)
        assert math.isclose(job.plates[1].top_clearance, 0.5)


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
        assert (
            JobSpec(
                job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")]
            ).max_h_compress
            is None
        )

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
            left_clearance=0.25,
            top_clearance=0.25,
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
                left_clearance=0.25,
                top_clearance=0.25,
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
        assert (
            JobSpec(
                job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")]
            ).text_h_alignment
            is None
        )

    def test_invalid_value_rejected(self) -> None:
        """Values outside the enum must fail validation."""
        with pytest.raises(ValidationError):
            TextLine(text="X", text_h_alignment="justify")

    def test_inherited_on_all_levels(self) -> None:
        """LabelSpec and JobSpec expose the field via the attribute mixins."""
        label = LabelSpec(
            id="lbl", width=2.0, height=1.0, text_h_alignment="left", content=[TextLine(text="X")]
        )
        assert label.text_h_alignment is TextHAlignment.LEFT
        job = JobSpec(
            job_name="J",
            width=2.0,
            height=1.0,
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
            left_clearance=0.25,
            top_clearance=0.25,
            text_h_alignment="left",
        )
        assert plate.text_h_alignment is TextHAlignment.LEFT


class TestTextColor:
    """Tests for the text_color stroke-color layer field."""

    def test_text_line_accepts_all_full_names(self) -> None:
        """TextLine accepts every full color name via TextAttributes."""
        expected = {
            "cyan": TextColor.CYAN,
            "magenta": TextColor.MAGENTA,
            "yellow": TextColor.YELLOW,
            "black": TextColor.BLACK,
            "red": TextColor.RED,
            "green": TextColor.GREEN,
            "blue": TextColor.BLUE,
            "violet": TextColor.VIOLET,
            "orange": TextColor.ORANGE,
            "pink": TextColor.PINK,
            "teal": TextColor.TEAL,
        }
        for value, member in expected.items():
            line = TextLine(text="HELLO", text_color=value)
            assert line.text_color is member

    def test_abbreviations_resolve_to_members(self) -> None:
        """Single-letter abbreviations map to their full-name members."""
        expected = {
            "c": TextColor.CYAN,
            "m": TextColor.MAGENTA,
            "y": TextColor.YELLOW,
            "k": TextColor.BLACK,
            "r": TextColor.RED,
            "g": TextColor.GREEN,
            "b": TextColor.BLUE,
            "v": TextColor.VIOLET,
            "o": TextColor.ORANGE,
            "p": TextColor.PINK,
            "t": TextColor.TEAL,
        }
        for value, member in expected.items():
            assert TextLine(text="X", text_color=value).text_color is member

    def test_names_and_abbreviations_case_insensitive(self) -> None:
        """Full names and abbreviations accept any capitalization."""
        assert TextLine(text="X", text_color="Cyan").text_color is TextColor.CYAN
        assert TextLine(text="X", text_color="MAGENTA").text_color is TextColor.MAGENTA
        assert TextLine(text="X", text_color="M").text_color is TextColor.MAGENTA
        assert TextLine(text="X", text_color="K").text_color is TextColor.BLACK

    def test_accepts_enum_member_directly(self) -> None:
        """Enum members are accepted as well as raw strings."""
        line = TextLine(text="HELLO", text_color=TextColor.MAGENTA)
        assert line.text_color is TextColor.MAGENTA

    def test_direct_enum_non_string_rejected(self) -> None:
        """Direct enum construction with a non-string misses every member."""
        with pytest.raises(ValueError):
            TextColor(5)  # type: ignore[call-overload]

    def test_default_is_none(self) -> None:
        """text_color defaults to None (implicit 'none' at resolution)."""
        assert TextLine(text="X").text_color is None
        assert LabelSpec(id="lbl", content=[TextLine(text="X")]).text_color is None

    def test_label_level_accepted(self) -> None:
        """LabelSpec exposes the field via the attribute mixins."""
        label = LabelSpec(id="lbl", text_color="m", content=[TextLine(text="X")])
        assert label.text_color is TextColor.MAGENTA

    def test_invalid_value_rejected(self) -> None:
        """Values outside the enum must fail validation."""
        with pytest.raises(ValidationError):
            TextLine(text="X", text_color="purple")
        with pytest.raises(ValidationError):
            TextLine(text="X", text_color="kk")

    def test_explicit_none_rejected_on_line(self) -> None:
        """An explicit 'none' (or 'n') is rejected on a text line."""
        with pytest.raises(ValidationError):
            TextLine(text="X", text_color="none")
        with pytest.raises(ValidationError):
            TextLine(text="X", text_color="n")

    def test_explicit_none_rejected_on_label(self) -> None:
        """An explicit 'none' is rejected at the label level too."""
        with pytest.raises(ValidationError):
            LabelSpec(id="lbl", text_color="none", content=[TextLine(text="X")])

    def test_job_level_rejected(self) -> None:
        """A job-level text_color is rejected unconditionally."""
        with pytest.raises(ValidationError):
            JobSpec(
                job_name="J", width=2.0, height=1.0, text_color="red", content=[TextLine(text="X")]
            )
        with pytest.raises(ValidationError):
            JobSpec(
                job_name="J",
                text_color="k",
                labels=[LabelSpec(id="lbl", content=[TextLine(text="X")])],
            )

    def test_abbreviation_property(self) -> None:
        """The abbreviation property exposes the file-name tag."""
        assert TextColor.BLACK.abbreviation == "k"
        assert TextColor.MAGENTA.abbreviation == "m"
        assert TextColor.NONE.abbreviation == "n"

    def test_parse_yaml_accepts_color(self, tmp_path: Path) -> None:
        """A YAML spec with a label/line color parses and normalizes."""
        spec_path = tmp_path / "color_job.yaml"
        spec_path.write_text(
            "job:\n"
            "  job_name: Color Test\n"
            "  labels:\n"
            "    - id: lbl\n"
            "      width: 2.0\n"
            "      height: 1.0\n"
            "      text_color: m\n"
            "      content:\n"
            "        - text: A\n"
            "        - text: B\n"
            "          text_color: K\n",
            encoding="utf-8",
        )
        job = parse_yaml(spec_path)
        assert job.labels is not None
        assert job.labels[0].text_color is TextColor.MAGENTA
        assert job.labels[0].content is not None
        assert job.labels[0].content[0].text_color is None
        assert job.labels[0].content[1].text_color is TextColor.BLACK

    def test_parse_yaml_rejects_explicit_none(self, tmp_path: Path) -> None:
        """A YAML spec explicitly setting 'none' fails validation."""
        spec_path = tmp_path / "bad_color_job.yaml"
        spec_path.write_text(
            "job:\n"
            "  job_name: Bad Color\n"
            "  labels:\n"
            "    - id: lbl\n"
            "      content:\n"
            "        - text: A\n"
            "          text_color: none\n",
            encoding="utf-8",
        )
        with pytest.raises(ValidationError):
            parse_yaml(spec_path)


class TestFontField:
    """Tests for the cascading ``font`` field (font_registry-backed)."""

    def test_default_is_none(self) -> None:
        """font defaults to None (the resolution layer applies the default)."""
        assert TextLine(text="X").font is None
        assert LabelSpec(id="lbl", content=[TextLine(text="X")]).font is None
        assert (
            JobSpec(job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")]).font is None
        )

    def test_plt_font_canonicalized(self) -> None:
        """A PLT-extracted font key canonicalizes case-insensitively."""
        assert TextLine(text="X", font="dino").font == "Dino"
        assert TextLine(text="X", font="DINO").font == "Dino"
        assert TextLine(text="X", font=" Jhanuni ").font == "Jhanuni"

    def test_ttf_basename_canonicalized(self) -> None:
        """A TTF basename (extension stripped) canonicalizes case-insensitively."""
        assert TextLine(text="X", font="reliefsinglelinecad-regular").font == (
            "ReliefSingleLineCAD-Regular"
        )

    def test_label_and_job_levels_accepted(self) -> None:
        """Label- and job-level font values canonicalize like line-level."""
        label = LabelSpec(id="lbl", font="dino", content=[TextLine(text="X")])
        assert label.font == "Dino"
        job = JobSpec(
            job_name="J",
            width=2.0,
            height=1.0,
            font="dino",
            content=[TextLine(text="X")],
        )
        assert job.font == "Dino"

    def test_unknown_font_rejected_with_choices(self) -> None:
        """An unknown font name fails validation listing the valid names."""
        with pytest.raises(ValidationError) as excinfo:
            TextLine(text="X", font="Comic Sans")
        message = str(excinfo.value)
        assert "Unknown font" in message
        # The error lists every valid name so users can pick one.
        for name in ("Dino", "Jhanuni", "ReliefSingleLineCAD-Regular"):
            assert name in message

    def test_empty_font_rejected(self) -> None:
        """An empty/whitespace font name is rejected (never a valid font)."""
        with pytest.raises(ValidationError):
            TextLine(text="X", font="")
        with pytest.raises(ValidationError):
            TextLine(text="X", font="   ")

    def test_plate_parity_field_canonicalized(self) -> None:
        """PlateSpec accepts and canonicalizes font for schema parity."""
        plate = PlateSpec(id="p1", width=24.0, height=16.0, font="dino")
        assert plate.font == "Dino"

    def test_plate_parity_field_rejects_unknown(self) -> None:
        """PlateSpec rejects unknown fonts exactly like the cascading field."""
        with pytest.raises(ValidationError):
            PlateSpec(id="p1", width=24.0, height=16.0, font="nope")

    def test_parse_yaml_accepts_font(self, tmp_path: Path) -> None:
        """A YAML spec with job/label/line fonts parses and canonicalizes."""
        spec_path = tmp_path / "font_job.yaml"
        spec_path.write_text(
            "job:\n"
            "  job_name: Font Test\n"
            "  font: dino\n"
            "  labels:\n"
            "    - id: lbl\n"
            "      width: 2.0\n"
            "      height: 1.0\n"
            "      content:\n"
            "        - text: A\n"
            "        - text: B\n"
            "          font: JHANUNI\n",
            encoding="utf-8",
        )
        job = parse_yaml(spec_path)
        assert job.font == "Dino"
        assert job.labels is not None
        assert job.labels[0].font is None
        assert job.labels[0].content is not None
        assert job.labels[0].content[0].font is None
        assert job.labels[0].content[1].font == "Jhanuni"

    def test_parse_yaml_rejects_unknown_font(self, tmp_path: Path) -> None:
        """A YAML spec with an unknown font fails validation."""
        spec_path = tmp_path / "bad_font_job.yaml"
        spec_path.write_text(
            "job:\n"
            "  job_name: Bad Font\n"
            "  labels:\n"
            "    - id: lbl\n"
            "      content:\n"
            "        - text: A\n"
            "          font: NotARealFont\n",
            encoding="utf-8",
        )
        with pytest.raises(ValidationError):
            parse_yaml(spec_path)


class TestMinHoleMargin:
    """Tests for the min_hole_margin collision-avoidance floor field."""

    def test_inherited_on_all_levels(self) -> None:
        """TextLine, LabelSpec and JobSpec expose min_hole_margin."""
        assert math.isclose(TextLine(text="X", min_hole_margin=0.05).min_hole_margin, 0.05)
        label = LabelSpec(id="lbl", min_hole_margin=0.1, content=[TextLine(text="X")])
        assert math.isclose(label.min_hole_margin, 0.1)
        job = JobSpec(
            job_name="J", width=2.0, height=1.0, min_hole_margin=0.2, content=[TextLine(text="X")]
        )
        assert math.isclose(job.min_hole_margin, 0.2)

    def test_default_is_none(self) -> None:
        """min_hole_margin defaults to None (no floor; inherit from parent)."""
        assert TextLine(text="X").min_hole_margin is None
        assert LabelSpec(id="lbl", content=[TextLine(text="X")]).min_hole_margin is None
        assert (
            JobSpec(
                job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")]
            ).min_hole_margin
            is None
        )

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
            left_clearance=0.25,
            top_clearance=0.25,
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
                left_clearance=0.25,
                top_clearance=0.25,
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
        job = JobSpec(
            job_name="J",
            width=2.0,
            height=1.0,
            hole_text_collision_distance=0.4,
            content=[TextLine(text="X")],
        )
        assert math.isclose(job.hole_text_collision_distance, 0.4)

    def test_default_is_none(self) -> None:
        """Schema default is None (inherit); resolution applies 0.15."""
        assert TextLine(text="X").hole_text_collision_distance is None
        label = LabelSpec(id="lbl", width=2.0, height=1.0, content=[TextLine(text="X")])
        assert label.hole_text_collision_distance is None
        job = JobSpec(job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")])
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
            left_clearance=0.25,
            top_clearance=0.25,
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
                left_clearance=0.25,
                top_clearance=0.25,
                hole_text_collision_distance=-0.1,
            )


class TestCutterSize:
    """Tests for the cutter_size explicit-cutter field."""

    def test_inherited_on_all_levels(self) -> None:
        """TextLine, LabelSpec and JobSpec expose cutter_size."""
        line = TextLine(text="X", cutter_size=0.03)
        assert math.isclose(line.cutter_size, 0.03)
        label = LabelSpec(id="lbl", cutter_size=0.045, content=[TextLine(text="X")])
        assert math.isclose(label.cutter_size, 0.045)
        job = JobSpec(
            job_name="J", width=2.0, height=1.0, cutter_size=0.06, content=[TextLine(text="X")]
        )
        assert math.isclose(job.cutter_size, 0.06)

    def test_default_is_none(self) -> None:
        """cutter_size defaults to None (auto selection from text_height)."""
        assert TextLine(text="X").cutter_size is None
        assert LabelSpec(id="lbl", content=[TextLine(text="X")]).cutter_size is None
        assert (
            JobSpec(job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")]).cutter_size
            is None
        )

    def test_zero_rejected(self) -> None:
        """cutter_size=0.0 is invalid: a cutter must be strictly positive (gt=0.0)."""
        with pytest.raises(ValidationError):
            TextLine(text="X", cutter_size=0.0)

    def test_negative_rejected(self) -> None:
        """Negative values must be rejected by the gt=0.0 constraint."""
        with pytest.raises(ValidationError):
            TextLine(text="X", cutter_size=-0.1)

    def test_plate_accepts_field_for_parity(self) -> None:
        """PlateSpec should accept the field for schema parity."""
        plate = PlateSpec(
            id="plate_1",
            width=24.0,
            height=12.0,
            left_clearance=0.25,
            top_clearance=0.25,
            cutter_size=0.03,
        )
        assert math.isclose(plate.cutter_size, 0.03)

    def test_plate_rejects_non_positive(self) -> None:
        """PlateSpec must enforce the > 0.0 range too."""
        with pytest.raises(ValidationError):
            PlateSpec(
                id="plate_1",
                width=24.0,
                height=12.0,
                left_clearance=0.25,
                top_clearance=0.25,
                cutter_size=0.0,
            )


class TestCutterDownsize:
    """Tests for the cutter_downsize compression-reduction permission field."""

    def test_inherited_on_all_levels(self) -> None:
        """TextLine, LabelSpec and JobSpec expose cutter_downsize."""
        assert TextLine(text="X", cutter_downsize=False).cutter_downsize is False
        label = LabelSpec(id="lbl", cutter_downsize=False, content=[TextLine(text="X")])
        assert label.cutter_downsize is False
        job = JobSpec(
            job_name="J", width=2.0, height=1.0, cutter_downsize=False, content=[TextLine(text="X")]
        )
        assert job.cutter_downsize is False
        # The opt-in direction works on every level too.
        assert TextLine(text="X", cutter_downsize=True).cutter_downsize is True

    def test_default_is_none(self) -> None:
        """Schema default is None (unset); resolution applies the True fallback."""
        assert TextLine(text="X").cutter_downsize is None
        assert LabelSpec(id="lbl", content=[TextLine(text="X")]).cutter_downsize is None
        assert (
            JobSpec(
                job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")]
            ).cutter_downsize
            is None
        )

    def test_plate_accepts_field_for_parity(self) -> None:
        """PlateSpec should accept the field for schema parity."""
        plate = PlateSpec(
            id="plate_1",
            width=24.0,
            height=12.0,
            left_clearance=0.25,
            top_clearance=0.25,
            cutter_downsize=False,
        )
        assert plate.cutter_downsize is False


class TestMaxCutterDownsizes:
    """Tests for the max_cutter_downsizes step-budget field."""

    def test_inherited_on_all_levels(self) -> None:
        """TextLine, LabelSpec and JobSpec expose max_cutter_downsizes."""
        assert TextLine(text="X", max_cutter_downsizes=2).max_cutter_downsizes == 2
        label = LabelSpec(id="lbl", max_cutter_downsizes=2, content=[TextLine(text="X")])
        assert label.max_cutter_downsizes == 2
        job = JobSpec(
            job_name="J",
            width=2.0,
            height=1.0,
            max_cutter_downsizes=3,
            content=[TextLine(text="X")],
        )
        assert job.max_cutter_downsizes == 3

    def test_default_is_none(self) -> None:
        """Schema default is None (unset); resolution applies the 1 fallback."""
        assert TextLine(text="X").max_cutter_downsizes is None
        assert LabelSpec(id="lbl", content=[TextLine(text="X")]).max_cutter_downsizes is None
        assert (
            JobSpec(
                job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")]
            ).max_cutter_downsizes
            is None
        )

    def test_explicit_zero_is_accepted(self) -> None:
        """max_cutter_downsizes=0 (mechanism disabled) is a valid explicit value."""
        assert TextLine(text="X", max_cutter_downsizes=0).max_cutter_downsizes == 0

    def test_negative_rejected(self) -> None:
        """Negative budgets must be rejected by the ge=0 constraint."""
        with pytest.raises(ValidationError):
            TextLine(text="X", max_cutter_downsizes=-1)

    def test_plate_accepts_field_for_parity(self) -> None:
        """PlateSpec should accept the field for schema parity."""
        plate = PlateSpec(
            id="plate_1",
            width=24.0,
            height=12.0,
            left_clearance=0.25,
            top_clearance=0.25,
            max_cutter_downsizes=2,
        )
        assert plate.max_cutter_downsizes == 2

    def test_plate_rejects_negative(self) -> None:
        """PlateSpec must enforce the >= 0 range too."""
        with pytest.raises(ValidationError):
            PlateSpec(
                id="plate_1",
                width=24.0,
                height=12.0,
                left_clearance=0.25,
                top_clearance=0.25,
                max_cutter_downsizes=-1,
            )


class TestCutterDownsizeGlobal:
    """Tests for the cutter_downsize_global per-label sharing field."""

    def test_inherited_on_all_levels(self) -> None:
        """TextLine, LabelSpec and JobSpec expose cutter_downsize_global."""
        assert TextLine(text="X", cutter_downsize_global=False).cutter_downsize_global is False
        label = LabelSpec(id="lbl", cutter_downsize_global=False, content=[TextLine(text="X")])
        assert label.cutter_downsize_global is False
        job = JobSpec(
            job_name="J",
            width=2.0,
            height=1.0,
            cutter_downsize_global=False,
            content=[TextLine(text="X")],
        )
        assert job.cutter_downsize_global is False
        # The opt-in direction works on every level too.
        assert TextLine(text="X", cutter_downsize_global=True).cutter_downsize_global is True

    def test_default_is_none(self) -> None:
        """Schema default is None (unset); resolution applies the True fallback."""
        assert TextLine(text="X").cutter_downsize_global is None
        assert LabelSpec(id="lbl", content=[TextLine(text="X")]).cutter_downsize_global is None
        assert (
            JobSpec(
                job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")]
            ).cutter_downsize_global
            is None
        )

    def test_plate_accepts_field_for_parity(self) -> None:
        """PlateSpec should accept the field for schema parity."""
        plate = PlateSpec(
            id="plate_1",
            width=24.0,
            height=12.0,
            left_clearance=0.25,
            top_clearance=0.25,
            cutter_downsize_global=False,
        )
        assert plate.cutter_downsize_global is False


class TestHCompressGlobal:
    """Tests for the h_compress_global per-label sharing field."""

    def test_inherited_on_all_levels(self) -> None:
        """TextLine, LabelSpec and JobSpec expose h_compress_global."""
        assert TextLine(text="X", h_compress_global=True).h_compress_global is True
        label = LabelSpec(id="lbl", h_compress_global=True, content=[TextLine(text="X")])
        assert label.h_compress_global is True
        job = JobSpec(
            job_name="J",
            width=2.0,
            height=1.0,
            h_compress_global=True,
            content=[TextLine(text="X")],
        )
        assert job.h_compress_global is True
        # The opt-out direction works on every level too.
        assert TextLine(text="X", h_compress_global=False).h_compress_global is False

    def test_default_is_none(self) -> None:
        """Schema default is None (unset); resolution applies the False fallback."""
        assert TextLine(text="X").h_compress_global is None
        assert LabelSpec(id="lbl", content=[TextLine(text="X")]).h_compress_global is None
        assert (
            JobSpec(
                job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")]
            ).h_compress_global
            is None
        )

    def test_plate_accepts_field_for_parity(self) -> None:
        """PlateSpec should accept the field for schema parity."""
        plate = PlateSpec(
            id="plate_1",
            width=24.0,
            height=12.0,
            left_clearance=0.25,
            top_clearance=0.25,
            h_compress_global=True,
        )
        assert plate.h_compress_global is True


class TestSmallCapsScaleField:
    """Tests for the sc_scale text-tier small-caps field."""

    def test_present_on_every_tier(self) -> None:
        """sc_scale cascades line -> label -> job and has plate parity."""
        assert "sc_scale" in TextAttributes.model_fields
        assert "sc_scale" in TextLine.model_fields
        assert "sc_scale" in LabelAttributes.model_fields
        assert "sc_scale" in LabelSpec.model_fields
        assert "sc_scale" in JobSpec.model_fields
        assert "sc_scale" in PlateSpec.model_fields

    def test_default_is_none(self) -> None:
        """Schema default is None (unset); resolution applies the 0.7 fallback."""
        assert TextLine(text="X").sc_scale is None
        assert LabelSpec(id="lbl", content=[TextLine(text="X")]).sc_scale is None

    def test_bounds_enforced(self) -> None:
        """The scale is bounded to [0.2, 1.0] on every tier."""
        with pytest.raises(ValueError):
            TextLine(text="X", sc_scale=0.19)
        with pytest.raises(ValueError):
            TextLine(text="X", sc_scale=1.01)
        with pytest.raises(ValueError):
            LabelSpec(id="lbl", content=[TextLine(text="X")], sc_scale=0.0)
        with pytest.raises(ValueError):
            PlateSpec(id="p", width=24.0, height=12.0, sc_scale=1.5)

    def test_plate_accepts_field_for_parity(self) -> None:
        """PlateSpec accepts the field for schema parity."""
        plate = PlateSpec(id="plate_1", width=24.0, height=12.0, sc_scale=0.5)
        assert plate.sc_scale == 0.5

    def test_font_accepts_small_caps_variant(self) -> None:
        """A ``"<base> SC"`` font name canonicalizes like any other name."""
        line = TextLine(text="X", font="dino sc")
        assert line.font == "Dino SC"


class TestUseBaselineSpacing:
    """Tests for the use_baseline_spacing label-tier stacking field."""

    def test_inherited_on_label_and_job_tiers_only(self) -> None:
        """use_baseline_spacing lives on LabelAttributes, never on TextLine."""
        assert "use_baseline_spacing" in LabelAttributes.model_fields
        assert "use_baseline_spacing" in JobSpec.model_fields
        assert "use_baseline_spacing" in PlateSpec.model_fields
        assert "use_baseline_spacing" not in TextLine.model_fields
        assert "use_baseline_spacing" not in TextAttributes.model_fields

    def test_label_and_job_accept_booleans(self) -> None:
        """Both directions validate on the tiers that carry the field."""
        label = LabelSpec(id="lbl", content=[TextLine(text="X")], use_baseline_spacing=False)
        assert label.use_baseline_spacing is False
        job = JobSpec(
            job_name="J",
            width=2.0,
            height=1.0,
            use_baseline_spacing=False,
            content=[TextLine(text="X")],
        )
        assert job.use_baseline_spacing is False

    def test_default_is_none(self) -> None:
        """Schema default is None (unset); resolution applies the True fallback."""
        assert LabelSpec(id="lbl", content=[TextLine(text="X")]).use_baseline_spacing is None
        assert (
            JobSpec(
                job_name="J",
                width=2.0,
                height=1.0,
                content=[TextLine(text="X")],
            ).use_baseline_spacing
            is None
        )

    def test_plate_accepts_field_for_parity(self) -> None:
        """PlateSpec should accept the field for schema parity."""
        plate = PlateSpec(id="plate_1", width=24.0, height=12.0, use_baseline_spacing=False)
        assert plate.use_baseline_spacing is False


class TestHoleAdjustForCutterField:
    """Tests for the hole_adjust_for_cutter label-tier field."""

    def test_inherited_on_label_and_job_tiers_only(self) -> None:
        """hole_adjust_for_cutter lives on LabelAttributes, never on TextLine."""
        assert "hole_adjust_for_cutter" in LabelAttributes.model_fields
        assert "hole_adjust_for_cutter" in JobSpec.model_fields
        assert "hole_adjust_for_cutter" in PlateSpec.model_fields
        assert "hole_adjust_for_cutter" not in TextLine.model_fields
        assert "hole_adjust_for_cutter" not in TextAttributes.model_fields

    def test_label_and_job_accept_booleans(self) -> None:
        """Both directions validate on the tiers that carry the field."""
        label = LabelSpec(id="lbl", content=[TextLine(text="X")], hole_adjust_for_cutter=False)
        assert label.hole_adjust_for_cutter is False
        job = JobSpec(
            job_name="J",
            width=2.0,
            height=1.0,
            hole_adjust_for_cutter=False,
            content=[TextLine(text="X")],
        )
        assert job.hole_adjust_for_cutter is False

    def test_default_is_none(self) -> None:
        """Schema default is None (unset); resolution applies the True fallback."""
        assert LabelSpec(id="lbl", content=[TextLine(text="X")]).hole_adjust_for_cutter is None
        assert (
            JobSpec(
                job_name="J",
                width=2.0,
                height=1.0,
                content=[TextLine(text="X")],
            ).hole_adjust_for_cutter
            is None
        )

    def test_plate_accepts_field_for_parity(self) -> None:
        """PlateSpec should accept the field for schema parity."""
        plate = PlateSpec(id="plate_1", width=24.0, height=12.0, hole_adjust_for_cutter=False)
        assert plate.hole_adjust_for_cutter is False


class TestAutoDescendingCharacterAdjustment:
    """Tests for the label-tier descending-character relief fields.

    The relief compares a line's glyphs against the *following* line's, so all
    four fields live on ``LabelAttributes`` (inherited by ``LabelSpec`` /
    ``JobSpec``) and are deliberately absent from ``TextLine`` /
    ``TextAttributes``.
    """

    _FIELDS = (
        "auto_descending_character_adjustment",
        "descending_character_v_scale",
        "descending_character_raise",
        "descending_character_clearance",
        "descending_character_reciprocal_scale",
    )

    @pytest.mark.parametrize("field", _FIELDS)
    def test_inherited_on_label_and_job_tiers_only(self, field: str) -> None:
        """The relief fields live on LabelAttributes, never on TextLine."""
        assert field in LabelAttributes.model_fields
        assert field in JobSpec.model_fields
        assert field in PlateSpec.model_fields
        assert field not in TextLine.model_fields
        assert field not in TextAttributes.model_fields

    @pytest.mark.parametrize("field", _FIELDS)
    def test_default_is_none(self, field: str) -> None:
        """Schema default is None (unset); resolution applies the fallback."""
        assert getattr(LabelSpec(id="lbl", content=[TextLine(text="X")]), field) is None

    def test_label_and_job_accept_the_values(self) -> None:
        """Every field validates on the tiers that carry it."""
        label = LabelSpec(
            id="lbl",
            content=[TextLine(text="X")],
            auto_descending_character_adjustment=False,
            descending_character_v_scale=0.3,
            descending_character_raise=0.0,
            descending_character_clearance=0.05,
            descending_character_reciprocal_scale=0.4,
        )
        assert label.auto_descending_character_adjustment is False
        assert label.descending_character_v_scale == pytest.approx(0.3)
        assert label.descending_character_raise == 0.0
        assert label.descending_character_clearance == pytest.approx(0.05)
        assert label.descending_character_reciprocal_scale == pytest.approx(0.4)

        job = JobSpec(
            job_name="J",
            width=2.0,
            height=1.0,
            auto_descending_character_adjustment=False,
            descending_character_v_scale=0.8,
            descending_character_raise=1.0,
            descending_character_clearance=0.0,
            descending_character_reciprocal_scale=1.0,
            content=[TextLine(text="X")],
        )
        assert job.auto_descending_character_adjustment is False
        assert job.descending_character_v_scale == pytest.approx(0.8)
        assert job.descending_character_raise == pytest.approx(1.0)
        assert job.descending_character_clearance == 0.0
        assert job.descending_character_reciprocal_scale == 1.0

    def test_v_scale_bounds_are_enforced(self) -> None:
        """descending_character_v_scale is bound to [0.2, 1.0]."""
        with pytest.raises(ValidationError, match="descending_character_v_scale"):
            LabelSpec(id="lbl", content=[TextLine(text="X")], descending_character_v_scale=0.1)
        with pytest.raises(ValidationError, match="descending_character_v_scale"):
            LabelSpec(id="lbl", content=[TextLine(text="X")], descending_character_v_scale=1.1)

    def test_raise_bounds_are_enforced(self) -> None:
        """descending_character_raise is bound to [0.0, 1.0]."""
        with pytest.raises(ValidationError, match="descending_character_raise"):
            LabelSpec(id="lbl", content=[TextLine(text="X")], descending_character_raise=-0.1)
        with pytest.raises(ValidationError, match="descending_character_raise"):
            LabelSpec(id="lbl", content=[TextLine(text="X")], descending_character_raise=1.5)

    def test_clearance_must_be_non_negative(self) -> None:
        """descending_character_clearance is ge=0.0 (an explicit 0.0 is legal)."""
        with pytest.raises(ValidationError, match="descending_character_clearance"):
            LabelSpec(id="lbl", content=[TextLine(text="X")], descending_character_clearance=-0.01)
        label = LabelSpec(
            id="lbl", content=[TextLine(text="X")], descending_character_clearance=0.0
        )
        assert label.descending_character_clearance == 0.0

    def test_reciprocal_scale_bounds_are_enforced(self) -> None:
        """descending_character_reciprocal_scale is bound to [0.2, 1.0]."""
        with pytest.raises(ValidationError, match="descending_character_reciprocal_scale"):
            LabelSpec(
                id="lbl", content=[TextLine(text="X")], descending_character_reciprocal_scale=0.1
            )
        with pytest.raises(ValidationError, match="descending_character_reciprocal_scale"):
            LabelSpec(
                id="lbl", content=[TextLine(text="X")], descending_character_reciprocal_scale=1.1
            )
        # 1.0 is the opt-out (reciprocal downscaling forbidden) and is legal.
        label = LabelSpec(
            id="lbl", content=[TextLine(text="X")], descending_character_reciprocal_scale=1.0
        )
        assert label.descending_character_reciprocal_scale == 1.0

    @pytest.mark.parametrize(
        "field,value",
        [
            ("auto_descending_character_adjustment", False),
            ("descending_character_v_scale", 0.4),
            ("descending_character_raise", 0.25),
            ("descending_character_clearance", 0.03),
            ("descending_character_reciprocal_scale", 0.6),
        ],
    )
    def test_plate_accepts_field_for_parity(self, field: str, value: object) -> None:
        """PlateSpec should accept the fields for schema parity."""
        plate = PlateSpec(id="plate_1", width=24.0, height=12.0, **{field: value})
        assert getattr(plate, field) == value


class TestJobLevelReplacementFile:
    """Job-level ``replacement_text_file`` (labels section optional)."""

    @staticmethod
    def _job(**kwargs: object) -> JobSpec:
        """Build a job-level replacement job with the given overrides."""
        base: dict[str, object] = {
            "job_name": "Job Repl",
            "width": 3.0,
            "height": 1.0,
            "text_height": 0.75,
            "replacement_text_file": "data.txt",
        }
        base.update(kwargs)
        return JobSpec(**base)  # type: ignore[arg-type]

    def test_job_level_file_without_labels_is_valid(self) -> None:
        """A job-level file with job dimensions needs no ``labels``."""
        job = self._job()
        assert job.labels is None
        assert job.replacement_text_file == "data.txt"

    def test_labels_section_rejected(self) -> None:
        """A job-level file is mutually exclusive with a ``labels`` list."""
        with pytest.raises(ValidationError, match="cannot define more than one"):
            self._job(labels=[LabelSpec(id="l1", content=[TextLine(text="X")])])

    def test_count_rejected(self) -> None:
        """Explicit ``count`` conflicts with the file's line count."""
        with pytest.raises(ValidationError, match="count.*cannot be combined"):
            self._job(count=5)

    def test_missing_width_rejected(self) -> None:
        """Job-level ``width`` is required to synthesize labels."""
        with pytest.raises(ValidationError, match="job-level width must be defined"):
            self._job(width=None)

    def test_missing_height_and_text_height_listed(self) -> None:
        """All missing required attributes are named in the error."""
        with pytest.raises(ValidationError) as exc_info:
            self._job(height=None, text_height=None)
        message = str(exc_info.value)
        assert "height, text_height must be defined" in message

    def test_content_is_attribute_template_not_conflict(self) -> None:
        """Root-level ``content`` alongside a job file is a template, not a
        second label source."""
        job = self._job(content=[TextLine(text="PLACEHOLDER", text_height=0.5)])
        assert job.content is not None
        assert len(job.content) == 1

    def test_delimiter_requires_file(self) -> None:
        """A job-level delimiter without a file is rejected."""
        with pytest.raises(ValidationError, match="requires 'replacement_text_file'"):
            JobSpec(
                job_name="J",
                width=3.0,
                height=1.0,
                text_height=0.5,
                content=[TextLine(text="X")],
                replacement_text_delimiter=",",
            )

    def test_invalid_delimiter_rejected(self) -> None:
        """The shared single-special-char delimiter rule applies."""
        with pytest.raises(ValidationError):
            self._job(replacement_text_delimiter="ab")

    def test_valid_delimiter_accepted(self) -> None:
        """A valid delimiter is stored verbatim."""
        job = self._job(replacement_text_delimiter=",")
        assert job.replacement_text_delimiter == ","

    def test_parse_yaml_end_to_end(self, tmp_path: Path) -> None:
        """A YAML job with only a job-level file parses successfully."""
        spec = tmp_path / "job.yaml"
        spec.write_text(
            "job:\n"
            "  job_name: J\n"
            "  width: 3.0\n"
            "  height: 1.0\n"
            "  text_height: 0.75\n"
            "  replacement_text_file: data.txt\n",
            encoding="utf-8",
        )
        job = parse_yaml(spec)
        assert job.replacement_text_file == "data.txt"
        assert job.labels is None


class TestPlateLevelReplacementFile:
    """Plate-level ``replacement_text_file`` (labels pinned to the plate)."""

    @staticmethod
    def _job(plates: list[PlateSpec], **kwargs: object) -> JobSpec:
        """Build a job with the given plates and job-level overrides."""
        base: dict[str, object] = {
            "job_name": "Plate Repl",
            "width": 3.0,
            "height": 1.0,
            "text_height": 0.75,
            "content": [TextLine(text="X")],
            "plates": plates,
        }
        base.update(kwargs)
        return JobSpec(**base)  # type: ignore[arg-type]

    def test_plate_file_accepted(self) -> None:
        """A plate may declare a replacement file."""
        plate = PlateSpec(id="p1", width=24.0, height=16.0, replacement_text_file="d.txt")
        assert plate.replacement_text_file == "d.txt"
        assert plate.replacement_text_delimiter is None

    def test_delimiter_requires_file(self) -> None:
        """A plate delimiter without a file is rejected."""
        with pytest.raises(ValidationError, match="requires 'replacement_text_file'"):
            PlateSpec(id="p1", width=24.0, height=16.0, replacement_text_delimiter=",")

    def test_invalid_delimiter_rejected(self) -> None:
        """The shared delimiter rule applies at plate level too."""
        with pytest.raises(ValidationError):
            PlateSpec(
                id="p1",
                width=24.0,
                height=16.0,
                replacement_text_file="d.txt",
                replacement_text_delimiter="a",
            )

    def test_missing_job_dimensions_rejected(self) -> None:
        """Plate files synthesize labels from job-level attributes."""
        with pytest.raises(ValidationError, match="plate-level 'replacement_text_file'"):
            self._job(
                [PlateSpec(id="p1", width=24.0, height=16.0, replacement_text_file="d.txt")],
                width=None,
            )

    def test_job_level_and_plate_files_conflict(self) -> None:
        """Job- and plate-level files cannot be combined."""
        with pytest.raises(ValidationError, match="cannot be combined with"):
            JobSpec(
                job_name="Both",
                width=3.0,
                height=1.0,
                text_height=0.75,
                replacement_text_file="job.txt",
                plates=[PlateSpec(id="p1", width=24.0, height=16.0, replacement_text_file="d.txt")],
            )

    def test_plate_file_with_job_dimensions_valid(self) -> None:
        """With job dimensions present, plate files validate cleanly."""
        plate = PlateSpec(id="p1", width=24.0, height=16.0, replacement_text_file="d.txt")
        job = self._job([plate])
        assert job.plates is not None
        assert job.plates[0].replacement_text_file == "d.txt"


class TestLabelPlateIdReference:
    """``LabelSpec.plate_id`` must reference a declared plate."""

    def test_valid_reference_accepted(self) -> None:
        """A label pinned to a declared plate parses."""
        job = JobSpec(
            job_name="J",
            plates=[PlateSpec(id="p1", width=24.0, height=16.0)],
            labels=[
                LabelSpec(
                    id="l1", width=2.0, height=1.0, content=[TextLine(text="X")], plate_id="p1"
                )
            ],
        )
        assert job.labels is not None
        assert job.labels[0].plate_id == "p1"

    def test_unknown_reference_rejected(self) -> None:
        """Pinning to an undeclared plate fails validation."""
        with pytest.raises(ValidationError, match="does not reference a declared plate"):
            JobSpec(
                job_name="J",
                plates=[PlateSpec(id="p1", width=24.0, height=16.0)],
                labels=[
                    LabelSpec(
                        id="l1",
                        width=2.0,
                        height=1.0,
                        content=[TextLine(text="X")],
                        plate_id="nope",
                    )
                ],
            )

    def test_reference_without_plates_rejected(self) -> None:
        """Pinning is impossible when no plates are declared."""
        with pytest.raises(ValidationError, match="does not reference a declared plate"):
            JobSpec(
                job_name="J",
                labels=[
                    LabelSpec(
                        id="l1", width=2.0, height=1.0, content=[TextLine(text="X")], plate_id="p1"
                    )
                ],
            )


class TestMaterialField:
    """The cascading ``material`` field (job -> plate -> label)."""

    @staticmethod
    def _job(plates: list[PlateSpec], **kwargs: object) -> JobSpec:
        """Build a minimal job with the given plates and job-level overrides."""
        return JobSpec(
            job_name="Material Job",
            width=2.0,
            height=1.0,
            plates=plates,
            labels=[LabelSpec(id="l1", width=2.0, height=1.0, content=[TextLine(text="X")])],
            **kwargs,  # type: ignore[arg-type]
        )

    def test_defaults_to_none_everywhere(self) -> None:
        """Job, label and plate all default material to None (unset)."""
        job = JobSpec(job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")])
        assert job.material is None
        assert (
            LabelSpec(id="l", width=2.0, height=1.0, content=[TextLine(text="X")]).material is None
        )
        assert PlateSpec(id="p", width=24.0, height=16.0).material is None

    def test_inherited_on_label_and_job_tiers_only(self) -> None:
        """material lives on LabelAttributes (label + job), never on TextLine."""
        assert "material" in LabelAttributes.model_fields
        assert "material" in JobSpec.model_fields
        assert "material" in PlateSpec.model_fields
        assert "material" not in TextLine.model_fields
        assert "material" not in TextAttributes.model_fields

    def test_free_form_strings_accepted(self) -> None:
        """Any non-empty string is a valid material (no vocabulary)."""
        for value in ("wb", "wb(uv)", "3-layer black", "ALUMINUM 0.060"):
            label = LabelSpec(
                id="l", width=2.0, height=1.0, content=[TextLine(text="X")], material=value
            )
            assert label.material == value

    def test_whitespace_trimmed(self) -> None:
        """Surrounding whitespace is trimmed at validation."""
        label = LabelSpec(
            id="l", width=2.0, height=1.0, content=[TextLine(text="X")], material="  wb  "
        )
        assert label.material == "wb"

    def test_blank_material_rejected(self) -> None:
        """Empty / whitespace-only material is a validation error."""
        for value in ("", "   ", "\t"):
            with pytest.raises(ValidationError, match="whitespace-only"):
                LabelSpec(
                    id="l", width=2.0, height=1.0, content=[TextLine(text="X")], material=value
                )
            with pytest.raises(ValidationError, match="whitespace-only"):
                PlateSpec(id="p", width=24.0, height=16.0, material=value)

    def test_job_value_cascades_to_plates_omitting_it(self) -> None:
        """Plates without an explicit material inherit the job-level value."""
        job = self._job([PlateSpec(id="p1", width=24.0, height=16.0)], material="wb")
        assert job.plates is not None
        assert job.plates[0].material == "wb"

    def test_explicit_plate_material_wins(self) -> None:
        """An explicit plate material always beats the job-level value."""
        job = self._job(
            [
                PlateSpec(id="p1", width=24.0, height=16.0, material="wb(uv)"),
                PlateSpec(id="p2", width=24.0, height=16.0),
            ],
            material="wb",
        )
        assert job.plates is not None
        assert job.plates[0].material == "wb(uv)"
        assert job.plates[1].material == "wb"

    def test_plate_null_material_inherits_job_value(self) -> None:
        """An explicit plate ``null`` material is unset semantics: inherit."""
        job = JobSpec(
            job_name="J",
            width=2.0,
            height=1.0,
            material="wb",
            plates=[{"id": "p1", "width": 24.0, "height": 16.0, "material": None}],
            labels=[LabelSpec(id="l1", width=2.0, height=1.0, content=[TextLine(text="X")])],
        )
        assert job.plates is not None
        assert job.plates[0].material == "wb"

    def test_no_job_material_keeps_plate_unset(self) -> None:
        """Without a job-level material, plates stay material-agnostic."""
        job = self._job([PlateSpec(id="p1", width=24.0, height=16.0)])
        assert job.plates is not None
        assert job.plates[0].material is None

    def test_input_plates_are_not_mutated(self) -> None:
        """The job -> plate cascade copies plates instead of mutating input."""
        plate = PlateSpec(id="p1", width=24.0, height=16.0)
        self._job([plate], material="wb")
        assert plate.material is None

    def test_parse_yaml_materials(self, tmp_path: Path) -> None:
        """Job- and label-level materials survive a YAML round trip."""
        spec = tmp_path / "job.yaml"
        spec.write_text(
            """
job:
  job_name: Materials
  width: 3.0
  height: 1.0
  material: wb
  plates:
    - id: p1
      width: 24.0
      height: 16.0
    - id: p2
      width: 24.0
      height: 16.0
      material: wb(uv)
  labels:
    - id: plain
      content:
        - text: A
    - id: uv
      material: wb(uv)
      content:
        - text: B
""",
            encoding="utf-8",
        )
        job = parse_yaml(spec)
        assert job.material == "wb"
        assert job.plates is not None
        assert [p.material for p in job.plates] == ["wb", "wb(uv)"]
        assert job.labels is not None
        assert job.labels[0].material is None
        assert job.labels[1].material == "wb(uv)"


class TestMaterialHelpers:
    """Unit tests for the shared material normalization helpers."""

    def test_normalize_material(self) -> None:
        """normalize_material trims and passes None through."""
        assert normalize_material(None) is None
        assert normalize_material(" wb(uv) ") == "wb(uv)"

    def test_normalize_material_rejects_blank(self) -> None:
        """Blank values raise (they would create anonymous groups)."""
        with pytest.raises(ValueError, match="whitespace-only"):
            normalize_material("   ")

    def test_material_key_is_case_insensitive(self) -> None:
        """Grouping keys trim and case-fold; None maps to None."""
        assert material_key(" WB(UV) ") == material_key("wb(uv)")
        assert material_key(None) is None
        assert material_key("wb") != material_key("wb(uv)")


class TestOptimizeLineContent:
    """Tests for the optimize_line_content word-reflow field."""

    def test_inherited_on_all_levels(self) -> None:
        """TextLine, LabelSpec and JobSpec expose optimize_line_content."""
        assert TextLine(text="X", optimize_line_content=True).optimize_line_content is True
        label = LabelSpec(id="lbl", optimize_line_content=True, content=[TextLine(text="X")])
        assert label.optimize_line_content is True
        job = JobSpec(
            job_name="J",
            width=2.0,
            height=1.0,
            optimize_line_content=True,
            content=[TextLine(text="X")],
        )
        assert job.optimize_line_content is True
        # The opt-out direction works on every level too.
        assert TextLine(text="X", optimize_line_content=False).optimize_line_content is False

    def test_default_is_none(self) -> None:
        """Schema default is None (unset); resolution applies the False fallback."""
        assert TextLine(text="X").optimize_line_content is None
        assert LabelSpec(id="lbl", content=[TextLine(text="X")]).optimize_line_content is None
        assert (
            JobSpec(
                job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")]
            ).optimize_line_content
            is None
        )

    def test_plate_accepts_field_for_parity(self) -> None:
        """PlateSpec should accept the field for schema parity."""
        plate = PlateSpec(
            id="plate_1",
            width=24.0,
            height=12.0,
            left_clearance=0.25,
            top_clearance=0.25,
            optimize_line_content=True,
        )
        assert plate.optimize_line_content is True


class TestOptimizeLineContentMaxLines:
    """Tests for the optimize_line_content_max_lines reflow line cap."""

    def test_inherited_on_all_levels(self) -> None:
        """TextLine, LabelSpec and JobSpec expose optimize_line_content_max_lines."""
        line = TextLine(text="X", optimize_line_content_max_lines=3)
        assert line.optimize_line_content_max_lines == 3
        label = LabelSpec(
            id="lbl",
            optimize_line_content_max_lines=3,
            content=[TextLine(text="X")],
        )
        assert label.optimize_line_content_max_lines == 3
        job = JobSpec(
            job_name="J",
            width=2.0,
            height=1.0,
            optimize_line_content_max_lines=3,
            content=[TextLine(text="X")],
        )
        assert job.optimize_line_content_max_lines == 3

    def test_default_is_none(self) -> None:
        """Schema default is None (unset = no growth); no fallback value."""
        assert TextLine(text="X").optimize_line_content_max_lines is None
        assert (
            LabelSpec(id="lbl", content=[TextLine(text="X")]).optimize_line_content_max_lines
            is None
        )
        assert (
            JobSpec(
                job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")]
            ).optimize_line_content_max_lines
            is None
        )

    def test_plate_accepts_field_for_parity(self) -> None:
        """PlateSpec should accept the field for schema parity."""
        plate = PlateSpec(
            id="plate_1",
            width=24.0,
            height=12.0,
            optimize_line_content=True,
            optimize_line_content_max_lines=4,
        )
        assert plate.optimize_line_content_max_lines == 4

    def test_zero_is_rejected(self) -> None:
        """ge=1: a zero cap is a validation error, not a cascade value."""
        with pytest.raises(ValidationError):
            TextLine(text="X", optimize_line_content_max_lines=0)
        with pytest.raises(ValidationError):
            PlateSpec(id="p", width=24.0, height=12.0, optimize_line_content_max_lines=0)

    def test_plate_cap_with_disabled_reflow_is_rejected(self) -> None:
        """A plate pairing the cap with an explicit reflow opt-out is contradictory."""
        with pytest.raises(ValidationError, match="requires 'optimize_line_content'"):
            JobSpec(
                job_name="J",
                width=2.0,
                height=1.0,
                content=[TextLine(text="X")],
                plates=[
                    PlateSpec(
                        id="p_disabled",
                        width=24.0,
                        height=12.0,
                        optimize_line_content=False,
                        optimize_line_content_max_lines=3,
                    )
                ],
            )

    def test_plate_cap_with_enabled_reflow_is_accepted(self) -> None:
        """The parity self-check passes when the plate enables reflow too."""
        job = JobSpec(
            job_name="J",
            width=2.0,
            height=1.0,
            content=[TextLine(text="X")],
            plates=[
                PlateSpec(
                    id="p_ok",
                    width=24.0,
                    height=12.0,
                    optimize_line_content=True,
                    optimize_line_content_max_lines=3,
                )
            ],
        )
        assert job.plates is not None
        assert job.plates[0].optimize_line_content_max_lines == 3

    def test_job_cap_with_disabled_job_reflow_is_accepted(self) -> None:
        """A job-level cap is legal with the job opt-out: a label may enable reflow."""
        job = JobSpec(
            job_name="J",
            width=2.0,
            height=1.0,
            optimize_line_content=False,
            optimize_line_content_max_lines=3,
            labels=[
                LabelSpec(
                    id="lbl",
                    optimize_line_content=True,
                    content=[TextLine(text="X")],
                )
            ],
        )
        assert job.optimize_line_content_max_lines == 3


class TestHtmlBracketsField:
    """The cascading ``html_brackets`` field (line -> label -> job + plate)."""

    def test_defaults_to_none_everywhere(self) -> None:
        """Job, label, line and plate all default html_brackets to None."""
        job = JobSpec(job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")])
        assert job.html_brackets is None
        assert (
            LabelSpec(id="l", width=2.0, height=1.0, content=[TextLine(text="X")]).html_brackets
            is None
        )
        assert TextLine(text="X").html_brackets is None
        assert PlateSpec(id="p", width=24.0, height=16.0).html_brackets is None

    def test_inherited_on_the_text_tiers(self) -> None:
        """html_brackets lives on TextAttributes (so TextLine inherits it)."""
        assert "html_brackets" in TextAttributes.model_fields
        assert "html_brackets" in TextLine.model_fields
        assert "html_brackets" in LabelSpec.model_fields
        assert "html_brackets" in JobSpec.model_fields
        assert "html_brackets" in PlateSpec.model_fields

    @pytest.mark.parametrize(
        "value",
        ["<>", "<<>>", "[[]]", "{}", "\u00ab\u00bb", "((()))", "[]", ">%", "|<", " ~"],
    )
    def test_valid_pairs_accepted(self, value: str) -> None:
        """Even-length non-alphanumeric pairs with distinct halves are legal."""
        assert TextLine(text="X", html_brackets=value).html_brackets == value

    @pytest.mark.parametrize(
        ("value", "reason"),
        [
            ("<", "odd length"),
            ("<>x", "odd length"),
            ("<<", "identical halves"),
            ("<><>", "identical halves"),
            ("%%", "identical halves"),
            ("//", "identical halves"),
            ("||", "identical halves"),
            ("ab", "alphanumeric"),
            ("<a", "alphanumeric"),
            ("a>", "alphanumeric"),
            ("<\n>", "newline"),
        ],
    )
    def test_invalid_pairs_rejected(self, value: str, reason: str) -> None:
        """The validator rejects malformed pairs (reason in the message)."""
        with pytest.raises(ValidationError) as excinfo:
            TextLine(text="X", html_brackets=value)
        assert "html_brackets" in str(excinfo.value)

    def test_plate_validates_the_same_rule(self) -> None:
        """The plate parity field enforces the identical rule."""
        assert PlateSpec(id="p", width=24.0, height=16.0, html_brackets="\u00ab\u00bb")
        with pytest.raises(ValidationError):
            PlateSpec(id="p", width=24.0, height=16.0, html_brackets="ab")

    def test_whitespace_delimiters_are_legal(self) -> None:
        """Whitespace is not alphanumeric, so a whitespace half is accepted."""
        assert TextLine(text="X", html_brackets=" ~").html_brackets == " ~"


class TestItalicAngleField:
    """The cascading ``italic_angle`` field (line -> label -> job + plate)."""

    def test_defaults_to_none_everywhere(self) -> None:
        """Every tier defaults italic_angle to None (unset)."""
        job = JobSpec(job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")])
        assert job.italic_angle is None
        assert TextLine(text="X").italic_angle is None
        assert PlateSpec(id="p", width=24.0, height=16.0).italic_angle is None

    @pytest.mark.parametrize("value", [0.0, 12.0, 45.0])
    def test_in_range_accepted(self, value: float) -> None:
        """The [0, 45] range is accepted, bounds included."""
        assert TextLine(text="X", italic_angle=value).italic_angle == value

    @pytest.mark.parametrize("value", [-0.1, 45.1, 90.0])
    def test_out_of_range_rejected(self, value: float) -> None:
        """An angle outside [0, 45] is a validation error."""
        with pytest.raises(ValidationError):
            TextLine(text="X", italic_angle=value)


class TestUnderlineOffsetField:
    """The cascading ``underline_offset`` field (line -> label -> job + plate)."""

    def test_defaults_to_none_everywhere(self) -> None:
        """Every tier defaults underline_offset to None (unset)."""
        job = JobSpec(job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")])
        assert job.underline_offset is None
        assert TextLine(text="X").underline_offset is None
        assert PlateSpec(id="p", width=24.0, height=16.0).underline_offset is None

    @pytest.mark.parametrize("value", [0.0, 0.1, 2.5])
    def test_non_negative_accepted(self, value: float) -> None:
        """Any non-negative offset is accepted, 0.0 included."""
        assert TextLine(text="X", underline_offset=value).underline_offset == value

    @pytest.mark.parametrize("value", [-0.1, -1.0])
    def test_negative_rejected(self, value: float) -> None:
        """A negative offset is a validation error."""
        with pytest.raises(ValidationError):
            TextLine(text="X", underline_offset=value)

    def test_plate_parity_accepts_the_field(self) -> None:
        """The plate tier carries the field for schema parity."""
        plate = PlateSpec(id="p", width=24.0, height=16.0, underline_offset=0.1)
        assert plate.underline_offset == 0.1


class TestUnderlineBreakClearanceField:
    """The cascading ``underline_break_clearance`` field (line -> label -> job + plate)."""

    def test_defaults_to_none_everywhere(self) -> None:
        """Every tier defaults underline_break_clearance to None (unset)."""
        job = JobSpec(job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")])
        assert job.underline_break_clearance is None
        assert TextLine(text="X").underline_break_clearance is None
        assert PlateSpec(id="p", width=24.0, height=16.0).underline_break_clearance is None

    @pytest.mark.parametrize("value", [0.0, 0.1, 2.5])
    def test_non_negative_accepted(self, value: float) -> None:
        """Any non-negative clearance is accepted, 0.0 included."""
        assert (
            TextLine(text="X", underline_break_clearance=value).underline_break_clearance == value
        )

    @pytest.mark.parametrize("value", [-0.1, -1.0])
    def test_negative_rejected(self, value: float) -> None:
        """A negative clearance is a validation error."""
        with pytest.raises(ValidationError):
            TextLine(text="X", underline_break_clearance=value)

    def test_plate_parity_accepts_the_field(self) -> None:
        """The plate tier carries the field for schema parity."""
        plate = PlateSpec(id="p", width=24.0, height=16.0, underline_break_clearance=0.1)
        assert plate.underline_break_clearance == 0.1


class TestUnderlineBreakSpacesField:
    """The cascading ``underline_break_spaces`` field (line -> label -> job + plate)."""

    def test_defaults_to_none_everywhere(self) -> None:
        """Every tier defaults underline_break_spaces to None (unset)."""
        job = JobSpec(job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")])
        assert job.underline_break_spaces is None
        assert TextLine(text="X").underline_break_spaces is None
        assert PlateSpec(id="p", width=24.0, height=16.0).underline_break_spaces is None

    @pytest.mark.parametrize("value", [True, False])
    def test_bool_accepted(self, value: bool) -> None:
        """Both True and False are accepted (explicit False honored)."""
        assert TextLine(text="X", underline_break_spaces=value).underline_break_spaces is value

    def test_plate_parity_accepts_the_field(self) -> None:
        """The plate tier carries the field for schema parity."""
        plate = PlateSpec(id="p", width=24.0, height=16.0, underline_break_spaces=True)
        assert plate.underline_break_spaces is True


class TestStrikethroughOffsetField:
    """The cascading ``strikethrough_offset`` field (line -> label -> job + plate)."""

    def test_defaults_to_none_everywhere(self) -> None:
        """Every tier defaults strikethrough_offset to None (unset = mid-cap)."""
        job = JobSpec(job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")])
        assert job.strikethrough_offset is None
        assert TextLine(text="X").strikethrough_offset is None
        assert PlateSpec(id="p", width=24.0, height=16.0).strikethrough_offset is None

    @pytest.mark.parametrize("value", [0.0, 0.1, 2.5])
    def test_non_negative_accepted(self, value: float) -> None:
        """Any non-negative height is accepted, 0.0 (on the baseline) included."""
        assert TextLine(text="X", strikethrough_offset=value).strikethrough_offset == value

    @pytest.mark.parametrize("value", [-0.1, -1.0])
    def test_negative_rejected(self, value: float) -> None:
        """A negative height is a validation error."""
        with pytest.raises(ValidationError):
            TextLine(text="X", strikethrough_offset=value)

    def test_plate_parity_accepts_the_field(self) -> None:
        """The plate tier carries the field for schema parity."""
        plate = PlateSpec(id="p", width=24.0, height=16.0, strikethrough_offset=0.1)
        assert plate.strikethrough_offset == 0.1


class TestStrikethroughBreakSpacesField:
    """The cascading ``strikethrough_break_spaces`` field (line -> label -> job + plate)."""

    def test_defaults_to_none_everywhere(self) -> None:
        """Every tier defaults strikethrough_break_spaces to None (unset)."""
        job = JobSpec(job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")])
        assert job.strikethrough_break_spaces is None
        assert TextLine(text="X").strikethrough_break_spaces is None
        assert PlateSpec(id="p", width=24.0, height=16.0).strikethrough_break_spaces is None

    @pytest.mark.parametrize("value", [True, False])
    def test_bool_accepted(self, value: bool) -> None:
        """Both True and False are accepted (explicit False honored)."""
        assert (
            TextLine(text="X", strikethrough_break_spaces=value).strikethrough_break_spaces
            is value
        )

    def test_plate_parity_accepts_the_field(self) -> None:
        """The plate tier carries the field for schema parity."""
        plate = PlateSpec(id="p", width=24.0, height=16.0, strikethrough_break_spaces=True)
        assert plate.strikethrough_break_spaces is True


class TestEnforceCapLineField:
    """The cascading ``enforce_cap_line`` field (line -> label -> job + plate)."""

    def test_defaults_to_none_everywhere(self) -> None:
        """Every tier defaults enforce_cap_line to None (unset = true fallback)."""
        job = JobSpec(job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")])
        assert job.enforce_cap_line is None
        assert TextLine(text="X").enforce_cap_line is None
        assert PlateSpec(id="p", width=24.0, height=16.0).enforce_cap_line is None

    @pytest.mark.parametrize("value", [True, False])
    def test_bool_accepted(self, value: bool) -> None:
        """Both True and False are accepted (explicit False honored)."""
        assert TextLine(text="X", enforce_cap_line=value).enforce_cap_line is value

    def test_plate_parity_accepts_the_field(self) -> None:
        """The plate tier carries the field for schema parity."""
        plate = PlateSpec(id="p", width=24.0, height=16.0, enforce_cap_line=False)
        assert plate.enforce_cap_line is False


class TestTargetBoldnessFactorField:
    """The cascading ``target_boldness_factor`` field (line -> label -> job + plate)."""

    def test_defaults_to_none_everywhere(self) -> None:
        """Every tier defaults target_boldness_factor to None (unset)."""
        job = JobSpec(job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")])
        assert job.target_boldness_factor is None
        assert TextLine(text="X").target_boldness_factor is None
        assert PlateSpec(id="p", width=24.0, height=16.0).target_boldness_factor is None

    @pytest.mark.parametrize("value", [0.05, 0.14, 0.30])
    def test_in_range_accepted(self, value: float) -> None:
        """Any factor in [0.05, 0.30] is accepted, bounds included."""
        assert TextLine(text="X", target_boldness_factor=value).target_boldness_factor == value

    @pytest.mark.parametrize("value", [0.04, 0.0, -0.1, 0.31, 1.0])
    def test_out_of_range_rejected(self, value: float) -> None:
        """A factor outside [0.05, 0.30] is a validation error."""
        with pytest.raises(ValidationError):
            TextLine(text="X", target_boldness_factor=value)

    def test_plate_parity_accepts_the_field(self) -> None:
        """The plate tier carries the field for schema parity."""
        plate = PlateSpec(id="p", width=24.0, height=16.0, target_boldness_factor=0.2)
        assert plate.target_boldness_factor == 0.2


class TestMultistrokeOverlapRatioField:
    """The cascading ``multistroke_overlap_ratio`` field (line -> label -> job + plate)."""

    def test_defaults_to_none_everywhere(self) -> None:
        """Every tier defaults multistroke_overlap_ratio to None (unset)."""
        job = JobSpec(job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")])
        assert job.multistroke_overlap_ratio is None
        assert TextLine(text="X").multistroke_overlap_ratio is None
        assert PlateSpec(id="p", width=24.0, height=16.0).multistroke_overlap_ratio is None

    @pytest.mark.parametrize("value", [0.1, 0.25, 0.5])
    def test_in_range_accepted(self, value: float) -> None:
        """Any ratio in [0.1, 0.5] is accepted, bounds included."""
        assert TextLine(text="X", multistroke_overlap_ratio=value).multistroke_overlap_ratio == value

    @pytest.mark.parametrize("value", [0.0, 0.05, 0.55, 1.0, -0.1])
    def test_out_of_range_rejected(self, value: float) -> None:
        """A ratio outside [0.1, 0.5] is a validation error."""
        with pytest.raises(ValidationError):
            TextLine(text="X", multistroke_overlap_ratio=value)

    def test_plate_parity_accepts_the_field(self) -> None:
        """The plate tier carries the field for schema parity."""
        plate = PlateSpec(id="p", width=24.0, height=16.0, multistroke_overlap_ratio=0.4)
        assert plate.multistroke_overlap_ratio == 0.4


class TestBoldBoldnessMultiplierField:
    """The cascading ``bold_boldness_multiplier`` field (line -> label -> job + plate)."""

    def test_defaults_to_none_everywhere(self) -> None:
        """Every tier defaults bold_boldness_multiplier to None (unset)."""
        job = JobSpec(job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")])
        assert job.bold_boldness_multiplier is None
        assert TextLine(text="X").bold_boldness_multiplier is None
        assert PlateSpec(id="p", width=24.0, height=16.0).bold_boldness_multiplier is None

    @pytest.mark.parametrize("value", [1.0, 1.5, 4.0])
    def test_in_range_accepted(self, value: float) -> None:
        """The [1.0, 4.0] range is accepted, bounds included."""
        assert TextLine(text="X", bold_boldness_multiplier=value).bold_boldness_multiplier == value

    @pytest.mark.parametrize("value", [0.9, 4.1, 0.0, -1.0])
    def test_out_of_range_rejected(self, value: float) -> None:
        """A multiplier outside [1.0, 4.0] is a validation error."""
        with pytest.raises(ValidationError):
            TextLine(text="X", bold_boldness_multiplier=value)

    def test_plate_parity_accepts_the_field(self) -> None:
        """The plate tier carries the field for schema parity."""
        plate = PlateSpec(id="p", width=24.0, height=16.0, bold_boldness_multiplier=2.0)
        assert plate.bold_boldness_multiplier == 2.0


class TestSupersubscriptDownscaleField:
    """The cascading ``supersubscript_downscale`` field (line -> label -> job + plate)."""

    def test_defaults_to_none_everywhere(self) -> None:
        """Every tier defaults supersubscript_downscale to None (unset)."""
        job = JobSpec(job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")])
        assert job.supersubscript_downscale is None
        assert TextLine(text="X").supersubscript_downscale is None
        assert PlateSpec(id="p", width=24.0, height=16.0).supersubscript_downscale is None

    @pytest.mark.parametrize("value", [0.2, 0.5, 1.0])
    def test_in_range_accepted(self, value: float) -> None:
        """The [0.2, 1.0] range is accepted, bounds included."""
        assert TextLine(text="X", supersubscript_downscale=value).supersubscript_downscale == value

    @pytest.mark.parametrize("value", [0.1, 1.1, 2.0])
    def test_out_of_range_rejected(self, value: float) -> None:
        """A downscale outside [0.2, 1.0] is a validation error."""
        with pytest.raises(ValidationError):
            TextLine(text="X", supersubscript_downscale=value)

    def test_plate_parity_accepts_the_field(self) -> None:
        """The plate tier carries the field for schema parity."""
        plate = PlateSpec(id="p", width=24.0, height=16.0, supersubscript_downscale=0.8)
        assert plate.supersubscript_downscale == 0.8


class TestLabelShapeField:
    """The cascading ``type`` label-shape field (label -> job + plate parity)."""

    def test_defaults_to_none_everywhere(self) -> None:
        """Job, label and plate all default ``type`` to None (resolution -> rect)."""
        job = JobSpec(job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")])
        assert job.type is None
        assert LabelSpec(id="l", width=2.0, height=1.0, content=[TextLine(text="X")]).type is None
        assert PlateSpec(id="p", width=24.0, height=16.0).type is None

    def test_lives_on_label_attributes_not_text(self) -> None:
        """Shape is a label-container property: TextLine must NOT carry it."""
        assert "type" in LabelAttributes.model_fields
        assert "type" in LabelSpec.model_fields
        assert "type" in JobSpec.model_fields
        assert "type" in PlateSpec.model_fields
        assert "type" not in TextAttributes.model_fields
        assert "type" not in TextLine.model_fields

    def test_accepts_rect_and_round(self) -> None:
        """Both enum values parse from their string form."""
        assert (
            LabelSpec(id="l", width=2.0, height=1.0, content=[TextLine(text="X")], type="round").type
            is LabelShape.ROUND
        )
        assert (
            LabelSpec(id="l", width=2.0, height=1.0, content=[TextLine(text="X")], type="rect").type
            is LabelShape.RECT
        )

    def test_accepts_the_enum_member(self) -> None:
        """The enum member itself is accepted (Python-side construction)."""
        assert (
            LabelSpec(
                id="l", width=2.0, height=1.0, content=[TextLine(text="X")], type=LabelShape.ROUND
            ).type
            is LabelShape.ROUND
        )

    def test_rejects_unknown_shape(self) -> None:
        """An unknown shape name is a validation error."""
        with pytest.raises(ValidationError):
            LabelSpec(id="l", width=2.0, height=1.0, content=[TextLine(text="X")], type="hexagon")

    def test_plate_parity_field_accepts_the_enum(self) -> None:
        """The plate parity field accepts the same values (not applied at plate level)."""
        assert PlateSpec(id="p", width=24.0, height=16.0, type="round").type is LabelShape.ROUND


class TestRoundedRectangleField:
    """The cascading rounded-rectangle fields (label -> job + plate parity)."""

    def test_defaults_to_none_everywhere(self) -> None:
        """Job, label and plate all default both fields to None (resolution -> 0.0/false)."""
        job = JobSpec(job_name="J", width=2.0, height=1.0, content=[TextLine(text="X")])
        assert job.rounded_rectangle_fraction is None
        assert job.rounded_rectangle_proportional is None
        label = LabelSpec(id="l", width=2.0, height=1.0, content=[TextLine(text="X")])
        assert label.rounded_rectangle_fraction is None
        assert label.rounded_rectangle_proportional is None
        plate = PlateSpec(id="p", width=24.0, height=16.0)
        assert plate.rounded_rectangle_fraction is None
        assert plate.rounded_rectangle_proportional is None

    def test_live_on_label_attributes_not_text(self) -> None:
        """The border is a label-container property: TextLine must NOT carry it."""
        for name in ("rounded_rectangle_fraction", "rounded_rectangle_proportional"):
            assert name in LabelAttributes.model_fields
            assert name in LabelSpec.model_fields
            assert name in JobSpec.model_fields
            assert name in PlateSpec.model_fields
            assert name not in TextAttributes.model_fields
            assert name not in TextLine.model_fields

    def test_accepts_the_declared_range(self) -> None:
        """The endpoints 0.0 and 1.0 are valid fractions."""
        for fraction in (0.0, 0.5, 1.0):
            label = LabelSpec(
                id="l",
                width=2.0,
                height=1.0,
                content=[TextLine(text="X")],
                rounded_rectangle_fraction=fraction,
            )
            assert label.rounded_rectangle_fraction == fraction

    def test_rejects_out_of_range_fraction(self) -> None:
        """A fraction outside [0, 1] is a validation error."""
        for fraction in (-0.1, 1.1):
            with pytest.raises(ValidationError):
                LabelSpec(
                    id="l",
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X")],
                    rounded_rectangle_fraction=fraction,
                )

    def test_plate_parity_fields_accept_values(self) -> None:
        """The plate parity fields accept the same values (not applied at plate level)."""
        plate = PlateSpec(
            id="p",
            width=24.0,
            height=16.0,
            rounded_rectangle_fraction=0.5,
            rounded_rectangle_proportional=True,
        )
        assert plate.rounded_rectangle_fraction == 0.5
        assert plate.rounded_rectangle_proportional is True


class TestRoundLabelDimensions:
    """Round labels relax the both-dimensions-required rule (one = circle)."""

    def test_round_label_with_only_width_is_valid(self) -> None:
        """A round label declaring only width is a circle of that diameter."""
        job = JobSpec(
            job_name="J",
            labels=[LabelSpec(id="c", type="round", width=2.5, content=[TextLine(text="X")])],
        )
        assert job.labels[0].width == 2.5
        assert job.labels[0].height is None

    def test_round_label_with_only_height_is_valid(self) -> None:
        """A round label declaring only height is equally legal."""
        LabelSpec(id="c", type="round", height=2.5, content=[TextLine(text="X")])

    def test_round_label_without_any_dimension_is_rejected(self) -> None:
        """A round label with no dimension at label or job level fails."""
        with pytest.raises(ValidationError, match="round labels require"):
            JobSpec(
                job_name="J",
                labels=[LabelSpec(id="c", type="round", content=[TextLine(text="X")])],
            )

    def test_rect_label_still_requires_both(self) -> None:
        """The rectangular rule is untouched: one dimension is still an error."""
        with pytest.raises(ValidationError, match="must be defined"):
            JobSpec(
                job_name="J",
                labels=[LabelSpec(id="r", width=2.0, content=[TextLine(text="X")])],
            )

    def test_round_label_inherits_shape_from_job(self) -> None:
        """A job-level ``type: round`` relaxes the rule for its labels."""
        job = JobSpec(
            job_name="J",
            type="round",
            width=3.0,
            labels=[LabelSpec(id="c", content=[TextLine(text="X")])],
        )
        # The label has no dimensions of its own; the job's width cascades and
        # the round rule fills the missing height at resolution.
        assert job.labels[0].width is None
        assert job.type is LabelShape.ROUND

    def test_root_level_round_content_needs_one_dimension(self) -> None:
        """A root-level round job needs width and/or height, not both."""
        JobSpec(job_name="J", type="round", width=3.0, content=[TextLine(text="X")])
        with pytest.raises(ValidationError, match="round labels require"):
            JobSpec(job_name="J", type="round", content=[TextLine(text="X")])
