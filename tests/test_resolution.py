"""Tests for the resolution engine that flattens JobSpec into ResolvedLabel."""

from __future__ import annotations

import math

import pytest

from plt_optimizer.generate.resolution import (
    DEFAULT_CHAR_SPACING,
    DEFAULT_HOLE_MARGIN,
    DEFAULT_LINE_SPACING,
    DEFAULT_MARGIN,
    DEFAULT_MAX_H_COMPRESS,
    DEFAULT_TEXT_H_ALIGNMENT,
    DEFAULT_TEXT_HEIGHT,
    IDEAL_CUTTER_MAP,
    ResolvedHoleSpec,
    ResolvedLabel,
    ResolvedTextLine,
    calculate_label_dimensions,
    compute_horizontal_offset,
    compute_horizontal_scale,
    fit_line_spacing_to_margins,
    get_cutter_diameter,
    resolve_job_spec,
)
from plt_optimizer.generate.schema import (
    HoleSpec,
    JobSpec,
    LabelSpec,
    TextHAlignment,
    TextLine,
)


def _make_line(
    text: str = "X",
    nominal_text_height: float = 0.5,
    character_spacing: float = 0.0,
    line_spacing: float = 0.0,
) -> ResolvedTextLine:
    """Helper to create a ResolvedTextLine with cutter compensation applied."""
    cutter_dia = get_cutter_diameter(nominal_text_height)
    return ResolvedTextLine(
        text=text,
        nominal_text_height=nominal_text_height,
        toolpath_text_height=nominal_text_height - cutter_dia,
        cutter_diameter=cutter_dia,
        character_spacing=character_spacing,
        line_spacing=line_spacing,
    )


class TestGetCutterDiameter:
    """Tests for the cutter lookup and inventory matching."""

    def test_ideal_cutter_for_known_height(self) -> None:
        """Known nominal heights should return the ideal cutter."""
        assert get_cutter_diameter(0.25) == 0.03
        assert get_cutter_diameter(0.125) == 0.015
        assert get_cutter_diameter(0.5) == 0.06
        assert get_cutter_diameter(1.0) == 0.125

    def test_closest_match_for_unknown_height(self) -> None:
        """Unknown heights should snap to the closest nominal in the table."""
        # 0.26 is closest to 0.25
        assert get_cutter_diameter(0.26) == 0.03
        # 0.13 is closest to 0.125
        assert get_cutter_diameter(0.13) == 0.015

    def test_no_inventory_returns_ideal(self) -> None:
        """No inventory should return the ideal cutter."""
        assert get_cutter_diameter(0.25, None) == 0.03
        assert get_cutter_diameter(0.25, []) == 0.03

    def test_prefers_narrower_cutter(self) -> None:
        """When both narrower and wider cutters are available, prefer narrower."""
        inventory = [0.015, 0.035]
        # Ideal for 0.25 is 0.03; narrower is 0.015 (dist 0.015), wider is 0.035 (dist 0.005)
        # dist_narrower (0.015) > 3 * dist_wider (0.015)? No, equal, so prefer narrower
        assert get_cutter_diameter(0.25, inventory) == 0.015

    def test_wider_cutter_when_narrower_too_far(self) -> None:
        """When narrower cutter is too far, switch to wider cutter."""
        inventory = [0.01, 0.035]
        # Ideal for 0.25 is 0.03; narrower is 0.01 (dist 0.02), wider is 0.035 (dist 0.005)
        # dist_narrower (0.02) > 3 * dist_wider (0.015)? Yes, so use wider
        assert get_cutter_diameter(0.25, inventory) == 0.035

    def test_exact_match_preferred(self) -> None:
        """An exact match in inventory should be selected."""
        inventory = [0.01, 0.03, 0.05]
        # Ideal for 0.25 is 0.03; exact match available
        assert get_cutter_diameter(0.25, inventory) == 0.03

    def test_only_wider_cutters_available(self) -> None:
        """When only wider cutters are available, use the smallest wider."""
        inventory = [0.04, 0.05, 0.06]
        # Ideal for 0.25 is 0.03; no narrower cutters available
        assert get_cutter_diameter(0.25, inventory) == 0.04

    def test_only_narrower_cutters_available(self) -> None:
        """When only narrower cutters are available, use the largest narrower."""
        inventory = [0.005, 0.01, 0.015]
        # Ideal for 0.25 is 0.03; no wider cutters available
        assert get_cutter_diameter(0.25, inventory) == 0.015

    def test_tolerance_factor_override(self) -> None:
        """Custom tolerance factor should override the default behavior."""
        inventory = [0.01, 0.035]
        # With factor=1.0: dist_narrower (0.02) > 1.0 * dist_wider (0.005)? Yes, use wider
        assert get_cutter_diameter(0.25, inventory, tolerance_factor=1.0) == 0.035
        # With factor=5.0: dist_narrower (0.02) > 5.0 * dist_wider (0.025)? No, use narrower
        assert get_cutter_diameter(0.25, inventory, tolerance_factor=5.0) == 0.01

    def test_ideal_cutter_map_keys(self) -> None:
        """IDEAL_CUTTER_MAP should contain expected nominal heights."""
        assert 0.25 in IDEAL_CUTTER_MAP
        assert 0.125 in IDEAL_CUTTER_MAP
        assert 0.5 in IDEAL_CUTTER_MAP
        assert 1.0 in IDEAL_CUTTER_MAP


class TestCalculateLabelDimensions:
    """Tests for the auto-sizing calculation helper."""

    def test_single_line(self) -> None:
        """A single line should produce a sensible bounding box."""
        content = [_make_line(text="HELLO", nominal_text_height=0.5)]
        width, height = calculate_label_dimensions(content, margin=0.125)
        # Width based on nominal height: 5 * 0.5 * 0.6 + 0.25 = 1.75
        # Height based on nominal height: 0.5 + 0.25 = 0.75
        assert math.isclose(width, 1.75)
        assert math.isclose(height, 0.75)

    def test_multiple_lines_stack_height(self) -> None:
        """Multiple lines should stack heights with line spacing."""
        content = [
            _make_line(text="A", nominal_text_height=0.5, line_spacing=0.1),
            _make_line(text="B", nominal_text_height=0.5),
        ]
        width, height = calculate_label_dimensions(content, margin=0.0)
        # Height: 0.5 + 0.1 + 0.5 = 1.1 -> ceil(4.4)/4 = 1.25
        assert math.isclose(height, 1.25)

    def test_margin_applied_to_both_sides(self) -> None:
        """Margin should be added to both width and height on both sides."""
        content = [_make_line(text="X", nominal_text_height=1.0)]
        width, height = calculate_label_dimensions(content, margin=0.25)
        # Width: 1*1*0.6 + 0.5 = 1.1 -> ceil(4.4)/4 = 1.25
        # Height: 1.0 + 0.5 = 1.5 -> ceil(6.0)/4 = 1.5
        assert math.isclose(width, 1.25)
        assert math.isclose(height, 1.5)

    def test_rounds_up_to_nearest_quarter(self) -> None:
        """Dimensions should round up to the nearest 0.25 inch."""
        content = [_make_line(text="AB", nominal_text_height=0.3)]
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
        assert math.isclose(labels[0].content[0].nominal_text_height, 0.8)

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
        assert math.isclose(labels[0].content[0].nominal_text_height, 0.6)

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
        assert math.isclose(labels[0].content[0].nominal_text_height, 0.35)

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
        assert math.isclose(labels[0].content[0].nominal_text_height, DEFAULT_TEXT_HEIGHT)
        # char_spacing falls back to cutter_dia * 1.5 when omitted
        expected_cutter = get_cutter_diameter(DEFAULT_TEXT_HEIGHT)
        assert math.isclose(labels[0].content[0].character_spacing, expected_cutter * 1.5)
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


class TestHoleMarginResolution:
    """Tests for the hole_margin inheritance cascade."""

    def test_default_hole_margin_when_unset(self) -> None:
        """hole_margin should fall back to the global default."""
        job = JobSpec(
            job_name="HM",
            labels=[
                LabelSpec(id="lbl", count=1, width=2.0, height=1.0, content=[TextLine(text="X")]),
            ],
        )
        labels = resolve_job_spec(job)
        assert math.isclose(labels[0].hole_margin, DEFAULT_HOLE_MARGIN)

    def test_job_hole_margin_used_when_label_omits(self) -> None:
        """Job-level hole_margin should apply when the label omits it."""
        job = JobSpec(
            job_name="HM",
            hole_margin=0.25,
            labels=[
                LabelSpec(id="lbl", count=1, width=2.0, height=1.0, content=[TextLine(text="X")]),
            ],
        )
        labels = resolve_job_spec(job)
        assert math.isclose(labels[0].hole_margin, 0.25)

    def test_label_hole_margin_overrides_job(self) -> None:
        """Label-level hole_margin should take precedence over the job."""
        job = JobSpec(
            job_name="HM",
            hole_margin=0.25,
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=2.0,
                    height=1.0,
                    hole_margin=0.5,
                    content=[TextLine(text="X")],
                ),
            ],
        )
        labels = resolve_job_spec(job)
        assert math.isclose(labels[0].hole_margin, 0.5)

    def test_explicit_zero_hole_margin_is_honored(self) -> None:
        """An explicit hole_margin of 0.0 must not fall through to the default.

        A zero margin means the hole circle is tangent to the label edge,
        which is a valid and intentional configuration.
        """
        job = JobSpec(
            job_name="HM",
            hole_margin=0.25,
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=2.0,
                    height=1.0,
                    hole_margin=0.0,
                    content=[TextLine(text="X")],
                ),
            ],
        )
        labels = resolve_job_spec(job)
        assert math.isclose(labels[0].hole_margin, 0.0)

    def test_negative_hole_margin_rejected(self) -> None:
        """Negative hole_margin values must fail schema validation."""
        with pytest.raises(Exception):
            JobSpec(
                job_name="HM",
                hole_margin=-0.1,
                labels=[
                    LabelSpec(
                        id="lbl", count=1, width=2.0, height=1.0, content=[TextLine(text="X")]
                    ),
                ],
            )


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


class TestCutterCompensation:
    """Tests for cutter compensation in resolved text lines."""

    def test_toolpath_height_subtracts_cutter(self) -> None:
        """toolpath_text_height should equal nominal minus cutter diameter."""
        job = JobSpec(
            job_name="Cutter",
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X", text_height=0.25)],
                ),
            ],
        )
        labels = resolve_job_spec(job)
        line = labels[0].content[0]
        # Ideal cutter for 0.25 is 0.03
        assert math.isclose(line.cutter_diameter, 0.03)
        assert math.isclose(line.nominal_text_height, 0.25)
        assert math.isclose(line.toolpath_text_height, 0.25 - 0.03)

    def test_cutter_diameter_stored_on_line(self) -> None:
        """Each ResolvedTextLine should store its matched cutter diameter."""
        job = JobSpec(
            job_name="Cutter",
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=2.0,
                    height=1.0,
                    content=[
                        TextLine(text="SMALL", text_height=0.125),
                        TextLine(text="LARGE", text_height=0.5),
                    ],
                ),
            ],
        )
        labels = resolve_job_spec(job)
        assert math.isclose(labels[0].content[0].cutter_diameter, 0.015)
        assert math.isclose(labels[0].content[1].cutter_diameter, 0.06)

    def test_inventory_snapping(self) -> None:
        """When inventory is provideder should snap to closest available."""
        inventory = [0.005, 0.01, 0.05]
        job = JobSpec(
            job_name="Cutter",
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X", text_height=0.25)],
                ),
            ],
        )
        labels = resolve_job_spec(job, available_cutters=inventory)
        # Ideal for 0.25 is 0.03, closest in inventory is 0.01 (distance 0.02)
        # vs 0.05 (distance 0.02) - equidistant, accept either
        result = labels[0].content[0].cutter_diameter
        assert result in (0.01, 0.05)
        assert math.isclose(labels[0].content[0].toolpath_text_height, 0.25 - result)

    def test_char_spacing_fallback_uses_cutter(self) -> None:
        """When char_spacing is omitted, it should fall back to cutter * 1.5."""
        job = JobSpec(
            job_name="Cutter",
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X", text_height=0.25)],
                ),
            ],
        )
        labels = resolve_job_spec(job)
        # Cutter for 0.25 is 0.03, so char_spacing should be 0.03 * 1.5 = 0.045
        assert math.isclose(labels[0].content[0].character_spacing, 0.03 * 1.5)


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
        line = _make_line(text="X")
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


class TestFitLineSpacingToMargins:
    """Tests for the margin-precedence line spacing fit helper."""

    def test_no_change_when_block_fits(self) -> None:
        """Spacings should be returned unchanged when the block already fits."""
        result = fit_line_spacing_to_margins([0.3, 0.3], [0.1], 1.0)
        assert result == [0.1]

    def test_spacing_shrinks_to_fit(self) -> None:
        """Excess height must be removed from spacing, not margins."""
        # Heights 0.6 + spacing 0.6 = 1.2 vs available 1.0 -> excess 0.2.
        result = fit_line_spacing_to_margins([0.3, 0.3], [0.6], 1.0)
        assert len(result) == 1
        assert math.isclose(result[0], 0.4, abs_tol=1e-9)
        total = 0.3 + 0.3 + result[0]
        assert math.isclose(total, 1.0, abs_tol=1e-9)

    def test_multiple_gaps_scale_proportionally(self) -> None:
        """Unequal gaps must shrink proportionally to the same factor."""
        # Heights 0.9, spacing 0.3 + 0.1 = 0.4 -> total 1.3 vs available 1.1.
        result = fit_line_spacing_to_margins([0.3, 0.3, 0.3], [0.3, 0.1], 1.1)
        assert math.isclose(result[0], 0.15, abs_tol=1e-9)
        assert math.isclose(result[1], 0.05, abs_tol=1e-9)
        assert math.isclose(sum(result), 0.2, abs_tol=1e-9)

    def test_spacing_collapses_to_zero_when_lines_alone_overflow(self) -> None:
        """Spacing floors at zero when line heights alone exceed the space."""
        result = fit_line_spacing_to_margins([0.6, 0.6], [0.3], 1.0)
        assert result == [0.0]

    def test_single_line_returns_empty_list(self) -> None:
        """A single line has no inter-line gaps to adjust."""
        assert fit_line_spacing_to_margins([0.5], [], 0.25) == []

    def test_spacing_is_never_increased(self) -> None:
        """The helper must only ever reduce spacing."""
        result = fit_line_spacing_to_margins([0.2, 0.2], [0.05, 0.05], 5.0)
        assert result == [0.05, 0.05]

    def test_negative_input_spacing_is_clamped(self) -> None:
        """Negative spacing input is floored at zero."""
        result = fit_line_spacing_to_margins([0.3, 0.3], [-0.2], 1.0)
        assert result == [0.0]

    def test_zero_total_spacing_is_returned_unchanged(self) -> None:
        """Nothing to scale when all spacing is already zero."""
        result = fit_line_spacing_to_margins([0.6, 0.6], [0.0], 1.0)
        assert result == [0.0]

    def test_exact_fit_is_left_alone(self) -> None:
        """A block exactly matching the available height must not change."""
        result = fit_line_spacing_to_margins([0.4, 0.4], [0.2], 1.0)
        assert result == [0.2]


class TestMarginPrecedenceInResolution:
    """Resolution must shrink line spacing rather than violate margins."""

    def test_high_line_spacing_is_reduced_to_preserve_margin(self) -> None:
        """valve_tag-style case: 2 x 0.3in lines with 0.3in spacing."""
        job = JobSpec(
            job_name="Tight",
            text_height=0.3,
            line_spacing=0.3,
            labels=[
                LabelSpec(
                    id="valve_tag",
                    count=1,
                    width=3.0,
                    height=1.0,
                    margin=0.125,
                    content=[TextLine(text="VALVE V-104"), TextLine(text="OPEN CW")],
                ),
            ],
        )
        label = resolve_job_spec(job)[0]

        # Margin is untouched.
        assert math.isclose(label.margin, 0.125)
        # Spacing reduced so the stacked block fits the 0.75in inner height.
        assert label.content[0].line_spacing < 0.3
        total = sum(line.nominal_text_height for line in label.content)
        total += sum(line.line_spacing for line in label.content[:-1])
        assert total <= (label.height - 2 * label.margin) + 1e-9

    def test_margin_wins_over_job_level_spacing(self) -> None:
        """A job-level spacing that overflows a label must be clamped."""
        job = JobSpec(
            job_name="Tight",
            text_height=0.3,
            line_spacing=0.3,
            margin=0.25,
            labels=[
                LabelSpec(
                    id="panel",
                    count=1,
                    width=6.0,
                    height=1.0,
                    content=[TextLine(text="AAA"), TextLine(text="BBB")],
                ),
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.margin, 0.25)
        assert label.content[0].line_spacing == pytest.approx(0.0, abs=1e-9)

    def test_untouched_when_spacing_fits(self) -> None:
        """Labels with room to spare keep their requested spacing."""
        job = JobSpec(
            job_name="Roomy",
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=4.0,
                    height=2.0,
                    margin=0.1,
                    text_height=0.3,
                    line_spacing=0.15,
                    content=[TextLine(text="AAA"), TextLine(text="BBB")],
                ),
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.content[0].line_spacing, 0.15)

    def test_auto_sized_label_keeps_spacing(self) -> None:
        """Auto-sizing already reserves spacing, so nothing should clamp."""
        job = JobSpec(
            job_name="Auto",
            text_height=0.3,
            line_spacing=0.3,
            margin=0.25,
            labels=[
                LabelSpec(
                    id="auto_warning",
                    count=1,
                    content=[TextLine(text="WARNING"), TextLine(text="HIGH VOLTAGE")],
                ),
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.content[0].line_spacing, 0.3)

    def test_single_line_label_is_unaffected(self) -> None:
        """Single-line labels have no spacing to clamp even when too tall."""
        job = JobSpec(
            job_name="Single",
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=5.0,
                    height=0.5,
                    margin=0.25,
                    text_height=0.5,
                    line_spacing=0.3,
                    content=[TextLine(text="OVERSIZE")],
                ),
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.content[0].line_spacing, 0.3)

    def test_per_line_spacing_override_is_respected(self) -> None:
        """A line-level spacing smaller than the label's need stays intact."""
        job = JobSpec(
            job_name="Mixed",
            text_height=0.3,
            line_spacing=0.5,
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=4.0,
                    height=1.0,
                    margin=0.125,
                    content=[
                        TextLine(text="AAA", line_spacing=0.1),
                        TextLine(text="BBB"),
                    ],
                ),
            ],
        )
        label = resolve_job_spec(job)[0]
        # Inner height 0.75, heights 0.6, requested gap 0.1 -> fits untouched.
        assert math.isclose(label.content[0].line_spacing, 0.1)


class TestComputeHorizontalScale:
    """Tests for the per-line horizontal compression scale computation."""

    def test_no_compression_when_disabled(self) -> None:
        """max_h_compress=0.0 must never compress, regardless of overflow."""
        assert compute_horizontal_scale(10.0, 3.0, 0.0) == 1.0

    def test_no_compression_when_line_fits(self) -> None:
        """A line narrower than the available width is left alone."""
        assert compute_horizontal_scale(2.0, 3.0, 0.5) == 1.0

    def test_exact_fit_is_left_alone(self) -> None:
        """A line exactly as wide as the available width needs no scaling."""
        assert compute_horizontal_scale(3.0, 3.0, 0.5) == 1.0

    def test_needed_scale_when_limit_allows(self) -> None:
        """When the limit permits, scale exactly to the available width."""
        # 6in line into 3in space -> 0.5; limit 0.6 allows down to 0.4.
        assert math.isclose(compute_horizontal_scale(6.0, 3.0, 0.6), 0.5)

    def test_scale_clamped_to_limit(self) -> None:
        """Compression never exceeds max_h_compress."""
        # 10in into 3in needs 0.3, but limit 0.5 floors at 0.5.
        assert math.isclose(compute_horizontal_scale(10.0, 3.0, 0.5), 0.5)

    def test_full_limit_allows_total_squeeze(self) -> None:
        """max_h_compress=1.0 permits compressing to zero width."""
        assert math.isclose(compute_horizontal_scale(10.0, 1.0, 1.0), 0.1)

    def test_zero_rendered_width_is_noop(self) -> None:
        """A degenerate zero-width line must not scale (avoids div-by-zero)."""
        assert compute_horizontal_scale(0.0, 3.0, 0.5) == 1.0

    def test_negative_limit_is_clamped(self) -> None:
        """Out-of-range limits are clamped rather than trusted."""
        assert compute_horizontal_scale(10.0, 3.0, -0.5) == 1.0

    def test_limit_above_one_is_clamped(self) -> None:
        """A limit above 1.0 behaves like 1.0 (no lower bound beyond zero)."""
        assert math.isclose(compute_horizontal_scale(10.0, 1.0, 2.0), 0.1)


class TestMaxHCompressCascade:
    """max_h_compress must cascade line -> label -> job -> default."""

    def test_default_when_all_omit(self) -> None:
        """All levels omitting yields the no-compression default."""
        job = JobSpec(
            job_name="J",
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X")],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.content[0].max_h_compress, DEFAULT_MAX_H_COMPRESS)

    def test_job_value_used_when_label_omits(self) -> None:
        """Job-level value cascades to the line."""
        job = JobSpec(
            job_name="J",
            max_h_compress=0.4,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X")],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.content[0].max_h_compress, 0.4)

    def test_label_overrides_job(self) -> None:
        """Label-level value overrides the job-level value."""
        job = JobSpec(
            job_name="J",
            max_h_compress=0.4,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    max_h_compress=0.8,
                    content=[TextLine(text="X")],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.content[0].max_h_compress, 0.8)

    def test_line_overrides_label(self) -> None:
        """Line-level value overrides the label-level value."""
        job = JobSpec(
            job_name="J",
            max_h_compress=0.4,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    max_h_compress=0.8,
                    content=[TextLine(text="X", max_h_compress=0.25)],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.content[0].max_h_compress, 0.25)

    def test_explicit_zero_at_label_disables_job_compression(self) -> None:
        """An explicit 0.0 must win over a parent value, not fall through."""
        job = JobSpec(
            job_name="J",
            max_h_compress=0.5,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    max_h_compress=0.0,
                    content=[TextLine(text="X")],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.content[0].max_h_compress, 0.0)

    def test_explicit_zero_at_line_disables_parent(self) -> None:
        """An explicit 0.0 at the line level disables inherited compression."""
        job = JobSpec(
            job_name="J",
            max_h_compress=0.5,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X", max_h_compress=0.0)],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.content[0].max_h_compress, 0.0)

    def test_root_level_job_cascades(self) -> None:
        """Root-level single-label jobs inherit the job-level value."""
        job = JobSpec(
            job_name="J",
            max_h_compress=0.7,
            width=2.0,
            height=1.0,
            content=[TextLine(text="X")],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.content[0].max_h_compress, 0.7)


class TestComputeHorizontalOffset:
    """Unit tests for the pure horizontal alignment offset helper."""

    def test_left_anchors_at_margin(self) -> None:
        """"left" must place the line's left-most point exactly at the margin."""
        assert compute_horizontal_offset(1.5, 3.0, 0.2, "left") == pytest.approx(0.2)

    def test_right_anchors_right_edge_at_margin(self) -> None:
        """"right" must place the line's right-most point at the right margin."""
        # Right inner edge = margin + available_width = 3.2; minus width 1.5.
        assert compute_horizontal_offset(1.5, 3.0, 0.2, "right") == pytest.approx(1.7)

    def test_center_centers_within_span(self) -> None:
        """"center" must center the line within the inner content span."""
        # margin + (available - width) / 2 = 0.2 + (3.0 - 1.5) / 2 = 0.95
        assert compute_horizontal_offset(1.5, 3.0, 0.2, "center") == pytest.approx(0.95)

    def test_center_matches_legacy_formula(self) -> None:
        """The center result must equal the legacy centering formula."""
        width, available, margin = 1.5, 3.0, 0.2
        legacy = (margin + available / 2) - width / 2
        assert compute_horizontal_offset(width, available, margin, "center") == pytest.approx(
            legacy
        )

    def test_zero_margin_spans_full_width(self) -> None:
        """With margin 0 the span covers the whole label width."""
        assert compute_horizontal_offset(2.0, 6.0, 0.0, "left") == pytest.approx(0.0)
        assert compute_horizontal_offset(2.0, 6.0, 0.0, "right") == pytest.approx(4.0)
        assert compute_horizontal_offset(2.0, 6.0, 0.0, "center") == pytest.approx(2.0)

    def test_over_wide_line_keeps_margin_edge_anchored(self) -> None:
        """An over-wide line keeps its aligned edge at the margin (spills out)."""
        # Line (5.0) wider than the span (3.0): left edge stays at margin.
        assert compute_horizontal_offset(5.0, 3.0, 0.2, "left") == pytest.approx(0.2)
        # Right edge stays at the right margin -> negative left offset.
        assert compute_horizontal_offset(5.0, 3.0, 0.2, "right") == pytest.approx(-1.8)

    def test_unknown_alignment_falls_back_to_center(self) -> None:
        """Unknown alignment strings must behave like "center"."""
        assert compute_horizontal_offset(1.5, 3.0, 0.2, "justify") == pytest.approx(0.95)


class TestTextHAlignmentCascade:
    """text_h_alignment must cascade line -> label -> job -> default."""

    def test_default_when_all_omit(self) -> None:
        """All levels omitting yields the centering default."""
        job = JobSpec(
            job_name="J",
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X")],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert label.content[0].text_h_alignment == DEFAULT_TEXT_H_ALIGNMENT

    def test_job_value_used_when_label_omits(self) -> None:
        """Job-level value cascades to the line."""
        job = JobSpec(
            job_name="J",
            text_h_alignment="left",
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X")],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert label.content[0].text_h_alignment == "left"

    def test_label_overrides_job(self) -> None:
        """Label-level value overrides the job-level value."""
        job = JobSpec(
            job_name="J",
            text_h_alignment="left",
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    text_h_alignment="right",
                    content=[TextLine(text="X")],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert label.content[0].text_h_alignment == "right"

    def test_line_overrides_label(self) -> None:
        """Line-level value overrides the label-level value."""
        job = JobSpec(
            job_name="J",
            text_h_alignment="left",
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    text_h_alignment="right",
                    content=[TextLine(text="X", text_h_alignment=TextHAlignment.CENTER)],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert label.content[0].text_h_alignment == "center"

    def test_per_line_alignment_within_one_label(self) -> None:
        """Different lines in one label may carry different alignments."""
        job = JobSpec(
            job_name="J",
            text_h_alignment="center",
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[
                        TextLine(text="A", text_h_alignment="left"),
                        TextLine(text="B"),
                        TextLine(text="C", text_h_alignment="right"),
                    ],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert [line.text_h_alignment for line in label.content] == [
            "left",
            "center",
            "right",
        ]

    def test_resolved_line_default_when_constructed_manually(self) -> None:
        """Manually constructed ResolvedTextLine defaults to centering."""
        line = ResolvedTextLine(
            text="X",
            nominal_text_height=0.25,
            toolpath_text_height=0.22,
            cutter_diameter=0.03,
            character_spacing=0.0,
            line_spacing=0.0,
        )
        assert line.text_h_alignment == "center"
