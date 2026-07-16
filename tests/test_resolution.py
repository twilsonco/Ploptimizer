"""Tests for the resolution engine that flattens JobSpec into ResolvedLabel."""

from __future__ import annotations

import math

import pytest

from plt_optimizer.generate.resolution import (
    DEFAULT_CHAR_SPACING,
    DEFAULT_LINE_SPACING,
    DEFAULT_MARGIN,
    DEFAULT_TEXT_HEIGHT,
    ResolvedHoleSpec,
    ResolvedLabel,
    ResolvedTextLine,
    calculate_label_dimensions,
    resolve_job_spec,
)
from plt_optimizer.generate.schema import (
    HoleSpec,
    JobSpec,
    LabelSpec,
    TextLine,
)


class TestCalculateLabelDimensions:
    """Tests for the auto-sizing calculation helper."""

    def test_single_line(self) -> None:
        """A single line should produce a sensible bounding box."""
        content = [ResolvedTextLine(text="HELLO", text_height=0.5, character_spacing=0.0, line_spacing=0.0)]
        width, height = calculate_label_dimensions(content, margin=0.125)
        # Width: 5 * 0.5 * 0.6 + 0.25 = 1.75 -> ceil(7.0)/4 = 1.75
        # Height: 0.5 + 0.25 = 0.75 -> ceil(3.0)/4 = 0.75
        assert math.isclose(width, 1.75)
        assert math.isclose(height, 0.75)

    def test_multiple_lines_stack_height(self) -> None:
        """Multiple lines should stack heights with line spacing."""
        content = [
            ResolvedTextLine(text="A", text_height=0.5, character_spacing=0.0, line_spacing=0.1),
            ResolvedTextLine(text="B", text_height=0.5, character_spacing=0.0, line_spacing=0.0),
        ]
        width, height = calculate_label_dimensions(content, margin=0.0)
        # Height: 0.5 + 0.1 + 0.5 = 1.1 -> ceil(4.4)/4 = 1.25
        assert math.isclose(height, 1.25)

    def test_margin_applied_to_both_sides(self) -> None:
        """Margin should be added to both width and height on both sides."""
        content = [ResolvedTextLine(text="X", text_height=1.0, character_spacing=0.0, line_spacing=0.0)]
        width, height = calculate_label_dimensions(content, margin=0.25)
        # Width: 1*1*0.6 + 0.0 = 0.6 -> + 0.5 = 1.1 -> ceil(4.4)/4 = 1.25
        # Height: 1.0 + 0.5 = 1.5 -> ceil(6.0)/4 = 1.5
        assert math.isclose(width, 1.25)
        assert math.isclose(height, 1.5)

    def test_rounds_up_to_nearest_quarter(self) -> None:
        """Dimensions should round up to the nearest 0.25 inch."""
        content = [ResolvedTextLine(text="AB", text_height=0.3, character_spacing=0.0, line_spacing=0.0)]
        width, height = calculate_label_dimensions(content, margin=0.0)
        # Width: 2*0.3*0.6 = 0.36 -> ceil(1.44)/4 = 0.5
        # Height: 0.3 -> ceil(1.2)/4 = 0.5
        assert math.isclose(width, 0.5)
        assert math.isclose(height, 0.5)


class TestResolveJobSpecRootLevel:
    """Tests for root-level single-label jobs."""

    def test_root_level_uses_job_dimensions(self) -> None:
        """Root-level jobs should use job-level width/height directly."""
        job = JobSpec(
            job_name="Batch",
            width=3.0,
            height=1.5,
            count=10,
            content=[
                TextLine(text="DANGER", text_height=0.5),
                TextLine(text="HIGH VOLTAGE"),
            ],
        )
        labels = resolve_job_spec(job)
        assert len(labels) == 1
        assert math.isclose(labels[0].width, 3.0)
        assert math.isclose(labels[0].height, 1.5)
        assert labels[0].count == 10
        assert labels[0].id.startswith("label_")

    def test_root_level_auto_sizes_when_omitted(self) -> None:
        """Root-level jobs without dimensions should auto-size from content."""
        job = JobSpec(
            job_name="Batch",
            count=5,
            content=[TextLine(text="WARNING", text_height=0.5)],
        )
        labels = resolve_job_spec(job)
        assert len(labels) == 1
        assert labels[0].width > 0
        assert labels[0].height > 0
        assert labels[0].count == 5

    def test_root_level_generates_unique_ids(self) -> None:
        """Each root-level job should get a unique synthetic ID."""
        job = JobSpec(
            job_name="Batch",
            count=1,
            content=[TextLine(text="X")],
        )
        labels = resolve_job_spec(job)
        assert labels[0].id.startswith("label_")
        assert len(labels[0].id) > len("label_")


class TestResolveJobSpecExplicitLabels:
    """Tests for jobs with explicit labels list."""

    def test_explicit_labels_preserve_ids(self) -> None:
        """Explicit labels should keep their provided IDs."""
        job = JobSpec(
            job_name="Multi",
            labels=[
                LabelSpec(
                    id="pump_warn",
                    count=3,
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="WARNING")],
                ),
                LabelSpec(
                    id="valve_tag",
                    count=5,
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="VALVE")],
                ),
            ],
        )
        labels = resolve_job_spec(job)
        assert len(labels) == 2
        assert labels[0].id == "pump_warn"
        assert labels[1].id == "valve_tag"
        assert labels[0].count == 3
        assert labels[1].count == 5

    def test_explicit_labels_use_label_dimensions(self) -> None:
        """Explicit labels should use their own width/height when provided."""
        job = JobSpec(
            job_name="Multi",
            labels=[
                LabelSpec(
                    id="big",
                    count=1,
                    width=4.0,
                    height=2.0,
                    content=[TextLine(text="X")],
                ),
                LabelSpec(
                    id="small",
                    count=1,
                    width=1.0,
                    height=0.5,
                    content=[TextLine(text="Y")],
                ),
            ],
        )
        labels = resolve_job_spec(job)
        assert math.isclose(labels[0].width, 4.0)
        assert math.isclose(labels[0].height, 2.0)
        assert math.isclose(labels[1].width, 1.0)
        assert math.isclose(labels[1].height, 0.5)


class TestCascadeResolution:
    """Tests for the top-down cascade of styling values."""

    def test_text_line_overrides_label(self) -> None:
        """Text line values should override label values."""
        job = JobSpec(
            job_name="Cascade",
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=2.0,
                    height=1.0,
                    text_height=0.4,
                    content=[TextLine(text="X", text_height=0.8)],
                ),
            ],
        )
        labels = resolve_job_spec(job)
        assert math.isclose(labels[0].content[0].text_height, 0.8)

    def test_label_overrides_job(self) -> None:
        """Label values should override job values."""
        job = JobSpec(
            job_name="Cascade",
            text_height=0.3,
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=2.0,
                    height=1.0,
                    text_height=0.6,
                    content=[TextLine(text="X")],
                ),
            ],
        )
        labels = resolve_job_spec(job)
        assert math.isclose(labels[0].content[0].text_height, 0.6)

    def test_job_value_used_when_label_omits(self) -> None:
        """Job values should be used when label omits them."""
        job = JobSpec(
            job_name="Cascade",
            text_height=0.35,
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X")],
                ),
            ],
        )
        labels = resolve_job_spec(job)
        assert math.isclose(labels[0].content[0].text_height, 0.35)

    def test_fallback_used_when_all_omit(self) -> None:
        """Fallback constants should be used when all levels omit."""
        job = JobSpec(
            job_name="Fallback",
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X")],
                ),
            ],
        )
        labels = resolve_job_spec(job)
        assert math.isclose(labels[0].content[0].text_height, DEFAULT_TEXT_HEIGHT)
        assert math.isclose(labels[0].content[0].character_spacing, DEFAULT_CHAR_SPACING)
        assert math.isclose(labels[0].content[0].line_spacing, DEFAULT_LINE_SPACING)
        assert math.isclose(labels[0].margin, DEFAULT_MARGIN)


class TestHoleResolution:
    """Tests for hole cascading and resolution."""

    def test_label_holes_override_job_holes(self) -> None:
        """Label-defined holes should take precedence over job-defined holes."""
        job = JobSpec(
            job_name="Holes",
            holes=[HoleSpec(diameter=0.25, location="top")],
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=2.0,
                    height=1.0,
                    holes=[HoleSpec(diameter=0.125, location="left")],
                    content=[TextLine(text="X")],
                ),
            ],
        )
        labels = resolve_job_spec(job)
        assert len(labels[0].holes) == 1
        assert labels[0].holes[0].diameter == 0.125
        assert labels[0].holes[0].location == "left"

    def test_job_holes_used_when_label_omits(self) -> None:
        """Job-defined holes should be used when label omits them."""
        job = JobSpec(
            job_name="Holes",
            holes=[HoleSpec(diameter=0.25, location="top")],
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X")],
                ),
            ],
        )
        labels = resolve_job_spec(job)
        assert len(labels[0].holes) == 1
        assert labels[0].holes[0].diameter == 0.25
        assert labels[0].holes[0].location == "top"

    def test_no_holes_when_neither_defines(self) -> None:
        """Empty holes list when neither label nor job defines holes."""
        job = JobSpec(
            job_name="NoHoles",
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X")],
                ),
            ],
        )
        labels = resolve_job_spec(job)
        assert labels[0].holes == []


class TestAutoSizing:
    """Tests for auto-sizing when dimensions are omitted."""

    def test_label_with_no_dimensions_auto_sizes(self) -> None:
        """Labels without width/height should auto-size from content."""
        job = JobSpec(
            job_name="Auto",
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    content=[TextLine(text="WARNING", text_height=0.5)],
                ),
            ],
        )
        labels = resolve_job_spec(job)
        assert labels[0].width > 0
        assert labels[0].height > 0

    def test_only_width_auto_sized(self) -> None:
        """If only width is missing, height should be preserved."""
        job = JobSpec(
            job_name="Partial",
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    height=2.0,
                    content=[TextLine(text="X", text_height=0.5)],
                ),
            ],
        )
        labels = resolve_job_spec(job)
        assert math.isclose(labels[0].height, 2.0)
        assert labels[0].width > 0

    def test_only_height_auto_sized(self) -> None:
        """If only height is missing, width should be preserved."""
        job = JobSpec(
            job_name="Partial",
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=3.0,
                    content=[TextLine(text="X", text_height=0.5)],
                ),
            ],
        )
        labels = resolve_job_spec(job)
        assert math.isclose(labels[0].width, 3.0)
        assert labels[0].height > 0


class TestResolvedDataclasses:
    """Tests for the resolved dataclass types."""

    def test_resolved_label_is_frozen(self) -> None:
        """ResolvedLabel should be immutable."""
        label = ResolvedLabel(
            id="x",
            count=1,
            width=1.0,
            height=1.0,
            margin=0.1,
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            label.width = 2.0  # type: ignore[misc]

    def test_resolved_text_line_is_frozen(self) -> None:
        """ResolvedTextLine should be immutable."""
        line = ResolvedTextLine(text="X", text_height=0.5, character_spacing=0.0, line_spacing=0.0)
        with pytest.raises(Exception):  # FrozenInstanceError
            line.text = "Y"  # type: ignore[misc]

    def test_resolved_hole_spec_is_frozen(self) -> None:
        """ResolvedHoleSpec should be immutable."""
        hole = ResolvedHoleSpec(diameter=0.125, location="left")
        with pytest.raises(Exception):  # FrozenInstanceError
            hole.diameter = 0.25  # type: ignore[misc]

    def test_default_factories(self) -> None:
        """ResolvedLabel should have empty default lists."""
        label = ResolvedLabel(id="x", count=1, width=1.0, height=1.0, margin=0.1)
        assert label.holes == []
        assert label.content == []


class TestFallbackConstants:
    """Tests for the global fallback constants."""

    def test_default_text_height(self) -> None:
        """DEFAULT_TEXT_HEIGHT should be a positive float."""
        assert isinstance(DEFAULT_TEXT_HEIGHT, float)
        assert DEFAULT_TEXT_HEIGHT > 0

    def test_default_margin(self) -> None:
        """DEFAULT_MARGIN should be a positive float."""
        assert isinstance(DEFAULT_MARGIN, float)
        assert DEFAULT_MARGIN > 0

    def test_default_char_spacing(self) -> None:
        """DEFAULT_CHAR_SPACING should be a non-negative float."""
        assert isinstance(DEFAULT_CHAR_SPACING, float)
        assert DEFAULT_CHAR_SPACING >= 0

    def test_default_line_spacing(self) -> None:
        """DEFAULT_LINE_SPACING should be a non-negative float."""
        assert isinstance(DEFAULT_LINE_SPACING, float)
        assert DEFAULT_LINE_SPACING >= 0