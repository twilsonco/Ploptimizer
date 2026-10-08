"""Tests for the resolution engine that flattens JobSpec into ResolvedLabel."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from plt_optimizer.generate.resolution import (
    DEFAULT_BOUNDARY_HOLE_CUTTER,
    DEFAULT_CUTTER_DOWNSIZE,
    DEFAULT_CUTTER_DOWNSIZE_GLOBAL,
    DEFAULT_CUTTER_SIZE,
    DEFAULT_FALLBACK_ADVANCE_FRACTION,
    DEFAULT_FONT,
    DEFAULT_H_COMPRESS_GLOBAL,
    DEFAULT_HOLE_MARGIN,
    DEFAULT_HOLE_TEXT_COLLISION_DISTANCE,
    DEFAULT_KERNING_MIN_GAP,
    DEFAULT_KERNING_PENETRATION_SCALE,
    DEFAULT_KERNING_RECESSION_SCALE,
    DEFAULT_KERNING_WINDOW_FRACTION,
    DEFAULT_LINE_SPACING,
    DEFAULT_MARGIN,
    DEFAULT_MAX_CUTTER_DOWNSIZES,
    DEFAULT_MAX_H_COMPRESS,
    DEFAULT_MIN_GLYPH_WIDTH,
    DEFAULT_MIN_HOLE_MARGIN,
    DEFAULT_OPTIMIZE_LINE_CONTENT,
    DEFAULT_OPTIMIZE_LINE_CONTENT_MAX_LINES,
    DEFAULT_SPACE_WIDTH_FRACTION,
    DEFAULT_TEXT_COLOR,
    DEFAULT_TEXT_H_ALIGNMENT,
    DEFAULT_TEXT_HEIGHT,
    DEFAULT_USE_BASELINE_SPACING,
    IDEAL_CUTTER_MAP,
    CutterSizeError,
    LineContentConfigError,
    ResolvedHoleSpec,
    ResolvedLabel,
    ResolvedTextLine,
    baseline_block_height,
    baseline_offsets,
    baseline_pitch,
    build_cutter_pen_map,
    compute_horizontal_offset,
    compute_horizontal_scale,
    fit_baseline_spacing_to_margins,
    fit_line_spacing_to_margins,
    get_cutter_diameter,
    memoize_extents_probe,
    next_smaller_cutter,
    resolve_job_spec,
    should_downsize_cutter,
    snap_boundary_hole_cutter,
    solve_baseline_spacing_to_fill,
    solve_baseline_spacing_to_ratio,
)
from plt_optimizer.generate.schema import (
    DEFAULT_HOLE_DIAMETER,
    HoleLocation,
    HoleSpec,
    JobSpec,
    LabelSpec,
    TextColor,
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

    def test_root_level_generates_unique_ids(self) -> None:
        """Each root-level job should get a unique synthetic ID."""
        job = JobSpec(
            job_name="Batch",
            width=2.0,
            height=1.0,
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
        # line_spacing defaults to "auto" (string), which for a single line resolves to 0.0
        # (no inter-line spacing for a single line)
        assert math.isclose(labels[0].content[0].line_spacing, 0.0)
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
                    holes=[HoleSpec(location="left")],
                    content=[TextLine(text="X")],
                ),
            ],
        )
        labels = resolve_job_spec(job)
        assert len(labels[0].holes) == 1
        assert labels[0].holes[0].diameter == 0.125
        assert labels[0].holes[0].location == "left"

    def test_location_only_hole_resolves_default_diameter(self) -> None:
        """A location-only hole resolves with the 0.125in default diameter."""
        job = JobSpec(
            job_name="Holes",
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=2.0,
                    height=1.0,
                    holes=[HoleSpec(location="bottom-right")],
                    content=[TextLine(text="X")],
                ),
            ],
        )
        labels = resolve_job_spec(job)
        assert len(labels[0].holes) == 1
        assert labels[0].holes[0].diameter == DEFAULT_HOLE_DIAMETER
        assert labels[0].holes[0].location == "bottom-right"

    def test_group_holes_resolve_to_atomic_locations(self) -> None:
        """``corners`` / ``sides`` groups resolve into atomic hole locations."""
        job = JobSpec(
            job_name="Holes",
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=2.0,
                    height=1.0,
                    holes=[
                        HoleSpec(location="corners", diameter=0.25),
                        HoleSpec(location="sides"),
                    ],
                    content=[TextLine(text="X")],
                ),
            ],
        )
        labels = resolve_job_spec(job)
        assert [h.location for h in labels[0].holes] == [
            HoleLocation.TOP_LEFT.value,
            HoleLocation.TOP_RIGHT.value,
            HoleLocation.BOTTOM_LEFT.value,
            HoleLocation.BOTTOM_RIGHT.value,
            HoleLocation.LEFT.value,
            HoleLocation.RIGHT.value,
        ]
        assert all(h.diameter == 0.25 for h in labels[0].holes[:4])
        assert all(h.diameter == DEFAULT_HOLE_DIAMETER for h in labels[0].holes[4:])

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
            h_margin=0.1,
            v_margin=0.1,
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
        label = ResolvedLabel(
            id="x", count=1, width=1.0, height=1.0, margin=0.1, h_margin=0.1, v_margin=0.1
        )
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

    def test_default_line_spacing(self) -> None:
        """DEFAULT_LINE_SPACING should be either 'auto' or a non-negative float."""
        assert isinstance(DEFAULT_LINE_SPACING, (str, float))
        if isinstance(DEFAULT_LINE_SPACING, float):
            assert DEFAULT_LINE_SPACING >= 0
        elif isinstance(DEFAULT_LINE_SPACING, str):
            assert DEFAULT_LINE_SPACING == "auto"

    def test_default_font_is_valid_registry_name(self) -> None:
        """DEFAULT_FONT must resolve in the font registry (never stale)."""
        from plt_optimizer.generate.font_registry import resolve_font

        assert isinstance(DEFAULT_FONT, str)
        assert resolve_font(DEFAULT_FONT).name == DEFAULT_FONT


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
        """ "left" must place the line's left-most point exactly at the margin."""
        assert compute_horizontal_offset(1.5, 3.0, 0.2, "left") == pytest.approx(0.2)

    def test_right_anchors_right_edge_at_margin(self) -> None:
        """ "right" must place the line's right-most point at the right margin."""
        # Right inner edge = margin + available_width = 3.2; minus width 1.5.
        assert compute_horizontal_offset(1.5, 3.0, 0.2, "right") == pytest.approx(1.7)

    def test_center_centers_within_span(self) -> None:
        """ "center" must center the line within the inner content span."""
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


class TestTextColorResolution:
    """text_color must resolve line -> label -> default (never job)."""

    def test_default_when_all_omit(self) -> None:
        """All levels omitting yields the implicit 'none' color."""
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
        assert label.content[0].text_color == DEFAULT_TEXT_COLOR

    def test_label_value_used_when_line_omits(self) -> None:
        """Label-level color applies to its lines."""
        job = JobSpec(
            job_name="J",
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    text_color="m",
                    content=[TextLine(text="X")],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert label.content[0].text_color == "magenta"

    def test_line_overrides_label(self) -> None:
        """Line-level color overrides the label-level color."""
        job = JobSpec(
            job_name="J",
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    text_color="magenta",
                    content=[TextLine(text="X", text_color=TextColor.BLACK)],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert label.content[0].text_color == "black"

    def test_per_line_color_within_one_label(self) -> None:
        """Different lines in one label may carry different colors."""
        job = JobSpec(
            job_name="J",
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[
                        TextLine(text="A", text_color="m"),
                        TextLine(text="B"),
                        TextLine(text="C", text_color="black"),
                    ],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert [line.text_color for line in label.content] == [
            "magenta",
            "none",
            "black",
        ]

    def test_never_cascades_from_job(self) -> None:
        """A job-level color is rejected outright, so nothing cascades."""
        with pytest.raises(ValidationError):
            JobSpec(
                job_name="J",
                text_color="red",
                labels=[
                    LabelSpec(
                        id="lbl",
                        width=2.0,
                        height=1.0,
                        content=[TextLine(text="X")],
                    )
                ],
            )

    def test_resolved_line_default_when_constructed_manually(self) -> None:
        """Manually constructed ResolvedTextLine defaults to 'none'."""
        line = ResolvedTextLine(
            text="X",
            nominal_text_height=0.25,
            toolpath_text_height=0.22,
            cutter_diameter=0.03,
            character_spacing=0.0,
            line_spacing=0.0,
        )
        assert line.text_color == "none"


class TestFontCascade:
    """font must resolve line -> label -> job -> DEFAULT_FONT."""

    def test_default_when_all_omit(self) -> None:
        """All levels omitting yields DEFAULT_FONT."""
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
        assert label.content[0].font == DEFAULT_FONT

    def test_job_value_used_when_lower_levels_omit(self) -> None:
        """Job-level font applies to labels and lines that omit it."""
        job = JobSpec(
            job_name="J",
            font="dino",
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
        assert label.content[0].font == "Dino"

    def test_label_overrides_job(self) -> None:
        """Label-level font overrides the job-level font."""
        job = JobSpec(
            job_name="J",
            font="dino",
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    font="jhanuni",
                    content=[TextLine(text="X")],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert label.content[0].font == "Jhanuni"

    def test_line_overrides_label_and_job(self) -> None:
        """Line-level font wins over both parent tiers."""
        job = JobSpec(
            job_name="J",
            font="dino",
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    font="jhanuni",
                    content=[TextLine(text="X", font="reliefsinglelinecad-regular")],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert label.content[0].font == "ReliefSingleLineCAD-Regular"

    def test_per_line_font_within_one_label(self) -> None:
        """Different lines in one label may carry different fonts."""
        job = JobSpec(
            job_name="J",
            font="dino",
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[
                        TextLine(text="A"),
                        TextLine(text="B", font="jhanuni"),
                        TextLine(text="C", font="ReliefSingleLineCAD-Regular"),
                    ],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert [line.font for line in label.content] == [
            "Dino",
            "Jhanuni",
            "ReliefSingleLineCAD-Regular",
        ]

    def test_resolved_line_default_when_constructed_manually(self) -> None:
        """Manually constructed ResolvedTextLine defaults to DEFAULT_FONT."""
        line = ResolvedTextLine(
            text="X",
            nominal_text_height=0.25,
            toolpath_text_height=0.22,
            cutter_diameter=0.03,
            character_spacing=0.0,
            line_spacing=0.0,
        )
        assert line.font == DEFAULT_FONT


class TestMinHoleMarginCascade:
    """min_hole_margin must cascade label -> job -> default.

    The value is a label-container property (like ``hole_margin``): it is
    accepted on text lines for schema parity but resolved at label level.
    """

    def test_default_is_none_when_unset(self) -> None:
        """Unset min_hole_margin falls back to the global default (None)."""
        job = JobSpec(
            job_name="MHM",
            labels=[
                LabelSpec(id="lbl", count=1, width=2.0, height=1.0, content=[TextLine(text="X")]),
            ],
        )
        label = resolve_job_spec(job)[0]
        assert label.min_hole_margin == DEFAULT_MIN_HOLE_MARGIN
        assert label.min_hole_margin is None

    def test_job_value_used_when_label_omits(self) -> None:
        """Job-level min_hole_margin applies when the label omits it."""
        job = JobSpec(
            job_name="MHM",
            min_hole_margin=0.05,
            labels=[
                LabelSpec(id="lbl", count=1, width=2.0, height=1.0, content=[TextLine(text="X")]),
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.min_hole_margin, 0.05)

    def test_label_overrides_job(self) -> None:
        """Label-level min_hole_margin takes precedence over the job."""
        job = JobSpec(
            job_name="MHM",
            min_hole_margin=0.05,
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=2.0,
                    height=1.0,
                    min_hole_margin=0.1,
                    content=[TextLine(text="X")],
                ),
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.min_hole_margin, 0.1)

    def test_explicit_zero_min_hole_margin_is_honored(self) -> None:
        """An explicit min_hole_margin of 0.0 must not fall through to the job.

        Zero means collision avoidance may shrink the hole margin all the
        way to tangent, which is a valid intentional configuration.
        """
        job = JobSpec(
            job_name="MHM",
            min_hole_margin=0.05,
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=2.0,
                    height=1.0,
                    min_hole_margin=0.0,
                    content=[TextLine(text="X")],
                ),
            ],
        )
        label = resolve_job_spec(job)[0]
        assert label.min_hole_margin == 0.0

    def test_root_level_job_cascades(self) -> None:
        """Root-level single-label jobs inherit the job-level value."""
        job = JobSpec(
            job_name="MHM",
            min_hole_margin=0.075,
            width=2.0,
            height=1.0,
            content=[TextLine(text="X")],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.min_hole_margin, 0.075)

    def test_negative_min_hole_margin_rejected(self) -> None:
        """Negative min_hole_margin values must fail schema validation."""
        with pytest.raises(ValidationError):
            JobSpec(
                job_name="MHM",
                min_hole_margin=-0.1,
                labels=[
                    LabelSpec(
                        id="lbl", count=1, width=2.0, height=1.0, content=[TextLine(text="X")]
                    ),
                ],
            )

    def test_resolved_label_defaults(self) -> None:
        """Manually constructed ResolvedLabel defaults for the new fields."""
        label = ResolvedLabel(
            id="x", count=1, width=1.0, height=1.0, margin=0.1, h_margin=0.1, v_margin=0.1
        )
        assert label.min_hole_margin is None
        assert label.collision_compress_by_line == {}  # Empty dict means no per-line compression


class TestHoleTextCollisionDistanceCascade:
    """hole_text_collision_distance must cascade label -> job -> 0.15.

    Like ``min_hole_margin`` the field is accepted on text lines for
    schema parity but resolved once at label level.
    """

    def test_default_is_015_when_unset(self) -> None:
        """Unset collision distance falls back to the 0.15in default."""
        job = JobSpec(
            job_name="HTCD",
            labels=[
                LabelSpec(id="lbl", count=1, width=2.0, height=1.0, content=[TextLine(text="X")]),
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.hole_text_collision_distance, 0.15)
        assert math.isclose(DEFAULT_HOLE_TEXT_COLLISION_DISTANCE, 0.15)

    def test_job_value_used_when_label_omits(self) -> None:
        """Job-level collision distance applies when the label omits it."""
        job = JobSpec(
            job_name="HTCD",
            hole_text_collision_distance=0.25,
            labels=[
                LabelSpec(id="lbl", count=1, width=2.0, height=1.0, content=[TextLine(text="X")]),
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.hole_text_collision_distance, 0.25)

    def test_label_overrides_job(self) -> None:
        """Label-level collision distance takes precedence over the job."""
        job = JobSpec(
            job_name="HTCD",
            hole_text_collision_distance=0.25,
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=2.0,
                    height=1.0,
                    hole_text_collision_distance=0.3,
                    content=[TextLine(text="X")],
                ),
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.hole_text_collision_distance, 0.3)

    def test_explicit_zero_is_honored(self) -> None:
        """An explicit 0.0 (strokes may touch) must not fall through.

        Zero is a valid intentional configuration: the engraved strokes
        are allowed to touch (stroke floor only, no air gap).
        """
        job = JobSpec(
            job_name="HTCD",
            hole_text_collision_distance=0.25,
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=2.0,
                    height=1.0,
                    hole_text_collision_distance=0.0,
                    content=[TextLine(text="X")],
                ),
            ],
        )
        label = resolve_job_spec(job)[0]
        assert label.hole_text_collision_distance == 0.0

    def test_root_level_job_cascades(self) -> None:
        """Root-level single-label jobs inherit the job-level value."""
        job = JobSpec(
            job_name="HTCD",
            hole_text_collision_distance=0.2,
            width=2.0,
            height=1.0,
            content=[TextLine(text="X")],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.hole_text_collision_distance, 0.2)

    def test_resolved_label_defaults(self) -> None:
        """Manually constructed ResolvedLabel defaults for the new fields."""
        label = ResolvedLabel(
            id="x", count=1, width=1.0, height=1.0, margin=0.1, h_margin=0.1, v_margin=0.1
        )
        assert math.isclose(label.hole_text_collision_distance, 0.15)
        assert math.isclose(label.hole_cutter_diameter, DEFAULT_BOUNDARY_HOLE_CUTTER)
        assert math.isclose(DEFAULT_BOUNDARY_HOLE_CUTTER, 0.015)


class TestSnapBoundaryHoleCutter:
    """snap_boundary_hole_cutter prefers exact/next-down, then next-up."""

    INVENTORY = [0.015, 0.02, 0.03, 0.06, 0.125, 0.25]

    def test_exact_match_kept(self) -> None:
        """A requested size present in the inventory is returned as-is."""
        assert snap_boundary_hole_cutter(0.03, self.INVENTORY) == 0.03

    def test_next_size_down_preferred(self) -> None:
        """Between two tools, the next size down wins."""
        assert snap_boundary_hole_cutter(0.025, self.INVENTORY) == 0.02
        assert snap_boundary_hole_cutter(0.1, self.INVENTORY) == 0.06

    def test_next_size_up_when_no_smaller(self) -> None:
        """Below the smallest tool, the smallest available is used."""
        assert snap_boundary_hole_cutter(0.005, self.INVENTORY) == 0.015

    def test_largest_when_no_wider_needed(self) -> None:
        """Above the largest tool, the largest available is used."""
        assert snap_boundary_hole_cutter(0.5, self.INVENTORY) == 0.25

    def test_no_inventory_returns_requested(self) -> None:
        """Without an inventory the requested size passes through."""
        assert snap_boundary_hole_cutter(0.017, None) == 0.017
        assert snap_boundary_hole_cutter(0.017, []) == 0.017

    def test_resolution_threads_and_snaps_boundary_cutter(self) -> None:
        """resolve_job_spec stores the snapped boundary/hole cutter."""
        job = JobSpec(
            job_name="BHC",
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=2.0,
                    height=1.0,
                    holes=[HoleSpec(location="top-left")],
                    content=[TextLine(text="X")],
                ),
            ],
        )
        inventory = [0.015, 0.02, 0.03]
        # Requested 0.017 is not in stock: snaps down to 0.015.
        label = resolve_job_spec(job, available_cutters=inventory, boundary_hole_cutter_size=0.017)[
            0
        ]
        assert math.isclose(label.hole_cutter_diameter, 0.015)
        # Requested 0.005 has no smaller tool: snaps up to 0.015.
        label = resolve_job_spec(job, available_cutters=inventory, boundary_hole_cutter_size=0.005)[
            0
        ]
        assert math.isclose(label.hole_cutter_diameter, 0.015)
        # Omitted: default 0.015 snapped against inventory.
        label = resolve_job_spec(job, available_cutters=inventory)[0]
        assert math.isclose(label.hole_cutter_diameter, DEFAULT_BOUNDARY_HOLE_CUTTER)
        # No inventory at all: requested value passes through unsnapped.
        label = resolve_job_spec(job, boundary_hole_cutter_size=0.02)[0]
        assert math.isclose(label.hole_cutter_diameter, 0.02)


class TestBuildCutterPenMap:
    """build_cutter_pen_map assigns one pen per distinct (cutter, color)."""

    @staticmethod
    def _label(label_id: str, cutters: list[float]) -> ResolvedLabel:
        content = [
            ResolvedTextLine(
                text=f"L{i}",
                nominal_text_height=0.3,
                toolpath_text_height=0.27,
                cutter_diameter=cutter,
                character_spacing=0.0,
                line_spacing=0.0,
            )
            for i, cutter in enumerate(cutters)
        ]
        return ResolvedLabel(
            id=label_id,
            count=1,
            width=2.0,
            height=1.0,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
            content=content,
        )

    def test_empty_labels(self) -> None:
        """No labels (or no content) yields an empty map."""
        assert build_cutter_pen_map([]) == {}
        assert build_cutter_pen_map([self._label("a", [])]) == {}

    def test_single_cutter_keeps_pen_one(self) -> None:
        """A lone cutter keeps the historical text pen 1."""
        assert build_cutter_pen_map([self._label("a", [0.03])]) == {(0.03, "none"): 1}

    def test_sorted_ascending_pen_assignment(self) -> None:
        """Smallest layer -> SP1, then SP4+ (SP2/SP3 reserved)."""
        labels = [
            self._label("a", [0.06]),
            self._label("b", [0.03]),
            self._label("c", [0.015, 0.06]),
        ]
        assert build_cutter_pen_map(labels) == {
            (0.015, "none"): 1,
            (0.03, "none"): 4,
            (0.06, "none"): 5,
        }

    def test_duplicate_cutters_share_pen(self) -> None:
        """The same diameter/color across labels/lines maps to a single pen."""
        labels = [self._label("a", [0.03, 0.03]), self._label("b", [0.03])]
        assert build_cutter_pen_map(labels) == {(0.03, "none"): 1}

    def test_many_cutters_skip_reserved_pens(self) -> None:
        """With 4+ cutters, pens never collide with 2 (borders) or 3 (holes)."""
        labels = [self._label("a", [0.01, 0.02, 0.03, 0.06, 0.125])]
        pen_map = build_cutter_pen_map(labels)
        assert pen_map == {
            (0.01, "none"): 1,
            (0.02, "none"): 4,
            (0.03, "none"): 5,
            (0.06, "none"): 6,
            (0.125, "none"): 7,
        }
        assert 2 not in pen_map.values()
        assert 3 not in pen_map.values()

    def test_same_cutter_different_colors_split_pens(self) -> None:
        """Lines sharing a cutter but differing in color get distinct pens."""
        content = [
            ResolvedTextLine(
                text="A",
                nominal_text_height=0.3,
                toolpath_text_height=0.27,
                cutter_diameter=0.03,
                character_spacing=0.0,
                line_spacing=0.0,
                text_color="magenta",
            ),
            ResolvedTextLine(
                text="B",
                nominal_text_height=0.3,
                toolpath_text_height=0.27,
                cutter_diameter=0.03,
                character_spacing=0.0,
                line_spacing=0.0,
                text_color="black",
            ),
            ResolvedTextLine(
                text="C",
                nominal_text_height=0.3,
                toolpath_text_height=0.27,
                cutter_diameter=0.03,
                character_spacing=0.0,
                line_spacing=0.0,
            ),
        ]
        label = ResolvedLabel(
            id="colors",
            count=1,
            width=2.0,
            height=1.0,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
            content=content,
        )
        pen_map = build_cutter_pen_map([label])
        assert len(pen_map) == 3
        assert len(set(pen_map.values())) == 3
        assert {cutter for cutter, _color in pen_map} == {0.03}
        # (cutter, color) sort order: black < magenta < none.
        assert pen_map[(0.03, "black")] == 1
        assert pen_map[(0.03, "magenta")] == 4
        assert pen_map[(0.03, "none")] == 5


class TestTextChunkModeCascade:
    """text_chunk_mode must cascade job -> label -> 'line' (job-level field)."""

    def test_default_is_line(self) -> None:
        """Unset chunk mode resolves to 'line' (backward compatible)."""
        job = JobSpec(
            job_name="TCM",
            labels=[
                LabelSpec(id="lbl", count=1, width=2.0, height=1.0, content=[TextLine(text="X")]),
            ],
        )
        label = resolve_job_spec(job)[0]
        assert label.text_chunk_mode == "line"

    def test_job_word_value_cascades(self) -> None:
        """Job-level 'word' applies to every label."""
        job = JobSpec(
            job_name="TCM",
            text_chunk_mode="word",
            labels=[
                LabelSpec(id="lbl", count=1, width=2.0, height=1.0, content=[TextLine(text="X")]),
            ],
        )
        label = resolve_job_spec(job)[0]
        assert label.text_chunk_mode == "word"

    def test_root_level_job_cascades(self) -> None:
        """Root-level single-label jobs carry their own chunk mode."""
        job = JobSpec(
            job_name="TCM",
            text_chunk_mode="word",
            width=2.0,
            height=1.0,
            content=[TextLine(text="X")],
        )
        label = resolve_job_spec(job)[0]
        assert label.text_chunk_mode == "word"

    def test_resolved_label_defaults_to_line(self) -> None:
        """Manually constructed ResolvedLabel defaults to 'line'."""
        label = ResolvedLabel(
            id="x", count=1, width=1.0, height=1.0, margin=0.1, h_margin=0.1, v_margin=0.1
        )
        assert label.text_chunk_mode == "line"


class TestSpaceWidthFractionCascade:
    """space_width_fraction must cascade line -> label -> job -> default."""

    def test_default_when_all_omit(self) -> None:
        """All levels omitting yields the 0.3 default."""
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
        assert math.isclose(label.content[0].space_width_fraction, DEFAULT_SPACE_WIDTH_FRACTION)

    def test_job_value_used_when_label_omits(self) -> None:
        """Job-level value cascades to the line."""
        job = JobSpec(
            job_name="J",
            space_width_fraction=0.45,
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
        assert math.isclose(label.content[0].space_width_fraction, 0.45)

    def test_label_overrides_job(self) -> None:
        """Label-level value overrides the job-level value."""
        job = JobSpec(
            job_name="J",
            space_width_fraction=0.45,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    space_width_fraction=0.6,
                    content=[TextLine(text="X")],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.content[0].space_width_fraction, 0.6)

    def test_line_overrides_label(self) -> None:
        """Line-level value overrides the label-level value."""
        job = JobSpec(
            job_name="J",
            space_width_fraction=0.45,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    space_width_fraction=0.6,
                    content=[TextLine(text="X", space_width_fraction=0.2)],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.content[0].space_width_fraction, 0.2)

    def test_explicit_zero_at_label_beats_job(self) -> None:
        """An explicit 0.0 must win over a parent value, not fall through."""
        job = JobSpec(
            job_name="J",
            space_width_fraction=0.5,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    space_width_fraction=0.0,
                    content=[TextLine(text="X")],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.content[0].space_width_fraction, 0.0)


class TestMinGlyphWidthCascade:
    """min_glyph_width must cascade line -> label -> job -> default."""

    def test_default_when_all_omit(self) -> None:
        """All levels omitting yields the pure-envelope default (0.0)."""
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
        assert math.isclose(label.content[0].min_glyph_width, DEFAULT_MIN_GLYPH_WIDTH)
        assert DEFAULT_MIN_GLYPH_WIDTH == 0.0

    def test_job_value_used_when_label_omits(self) -> None:
        """Job-level value cascades to the line."""
        job = JobSpec(
            job_name="J",
            min_glyph_width=0.1,
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
        assert math.isclose(label.content[0].min_glyph_width, 0.1)

    def test_label_overrides_job(self) -> None:
        """Label-level value overrides the job-level value."""
        job = JobSpec(
            job_name="J",
            min_glyph_width=0.1,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    min_glyph_width=0.2,
                    content=[TextLine(text="X")],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.content[0].min_glyph_width, 0.2)

    def test_line_overrides_label(self) -> None:
        """Line-level value overrides the label-level value."""
        job = JobSpec(
            job_name="J",
            min_glyph_width=0.1,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    min_glyph_width=0.2,
                    content=[TextLine(text="X", min_glyph_width=0.05)],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.content[0].min_glyph_width, 0.05)

    def test_explicit_zero_at_line_beats_parents(self) -> None:
        """An explicit 0.0 must win over a parent value, not fall through."""
        job = JobSpec(
            job_name="J",
            min_glyph_width=0.1,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    min_glyph_width=0.2,
                    content=[TextLine(text="X", min_glyph_width=0.0)],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.content[0].min_glyph_width, 0.0)


class TestKerningWindowFractionCascade:
    """kerning_window_fraction must cascade line -> label -> job -> default."""

    def test_default_when_all_omit(self) -> None:
        """All levels omitting yields the 0.05 window default."""
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
        assert math.isclose(
            label.content[0].kerning_window_fraction, DEFAULT_KERNING_WINDOW_FRACTION
        )
        assert DEFAULT_KERNING_WINDOW_FRACTION == 0.05

    def test_job_value_used_when_label_omits(self) -> None:
        """Job-level value cascades to the line."""
        job = JobSpec(
            job_name="J",
            kerning_window_fraction=0.2,
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
        assert math.isclose(label.content[0].kerning_window_fraction, 0.2)

    def test_label_overrides_job(self) -> None:
        """Label-level value overrides the job-level value."""
        job = JobSpec(
            job_name="J",
            kerning_window_fraction=0.2,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    kerning_window_fraction=0.4,
                    content=[TextLine(text="X")],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.content[0].kerning_window_fraction, 0.4)

    def test_line_overrides_label(self) -> None:
        """Line-level value overrides the label-level value."""
        job = JobSpec(
            job_name="J",
            kerning_window_fraction=0.2,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    kerning_window_fraction=0.4,
                    content=[TextLine(text="X", kerning_window_fraction=0.1)],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.content[0].kerning_window_fraction, 0.1)

    def test_explicit_zero_at_line_beats_parents(self) -> None:
        """An explicit 0.0 (historical same-height kerning) must win."""
        job = JobSpec(
            job_name="J",
            kerning_window_fraction=0.2,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    kerning_window_fraction=0.4,
                    content=[TextLine(text="X", kerning_window_fraction=0.0)],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.content[0].kerning_window_fraction, 0.0)

    def test_above_one_rejected(self) -> None:
        """The fraction is bounded by 1.0 at every level."""
        with pytest.raises(ValueError):
            TextLine(text="X", kerning_window_fraction=1.01)
        with pytest.raises(ValueError):
            JobSpec(job_name="J", kerning_window_fraction=1.5)


class TestKerningSpacingKnobCascades:
    """The three spacing knobs cascade line -> label -> job -> default."""

    def test_module_defaults_are_neutral(self) -> None:
        """The shipped defaults leave the kerning math unchanged."""
        assert DEFAULT_KERNING_PENETRATION_SCALE == 1.0
        assert DEFAULT_KERNING_RECESSION_SCALE == 1.0
        assert DEFAULT_KERNING_MIN_GAP == 0.0
        assert DEFAULT_FALLBACK_ADVANCE_FRACTION == 1.0

    @pytest.mark.parametrize(
        "field",
        [
            "kerning_penetration_scale",
            "kerning_recession_scale",
            "kerning_min_gap",
            "fallback_advance_fraction",
        ],
    )
    def test_cascade_precedence(self, field: str) -> None:
        """Job fills the line; label beats job; line beats label; 0.0 wins."""

        def resolve(**levels: float | None) -> float:
            job_kwargs: dict[str, float | None] = {}
            label_kwargs: dict[str, float | None] = {}
            line_kwargs: dict[str, float | None] = {}
            if "job" in levels:
                job_kwargs[field] = levels["job"]
            if "label" in levels:
                label_kwargs[field] = levels["label"]
            if "line" in levels:
                line_kwargs[field] = levels["line"]
            job = JobSpec(
                job_name="J",
                **job_kwargs,  # type: ignore[arg-type]
                labels=[
                    LabelSpec(
                        id="lbl",
                        width=2.0,
                        height=1.0,
                        **label_kwargs,  # type: ignore[arg-type]
                        content=[TextLine(text="X", **line_kwargs)],  # type: ignore[arg-type]
                    )
                ],
            )
            return float(getattr(resolve_job_spec(job)[0].content[0], field))

        # All levels omit -> module default (checked via the resolved value).
        job = JobSpec(
            job_name="J",
            labels=[LabelSpec(id="lbl", width=2.0, height=1.0, content=[TextLine(text="X")])],
        )
        defaults = {
            "kerning_penetration_scale": DEFAULT_KERNING_PENETRATION_SCALE,
            "kerning_recession_scale": DEFAULT_KERNING_RECESSION_SCALE,
            "kerning_min_gap": DEFAULT_KERNING_MIN_GAP,
            "fallback_advance_fraction": DEFAULT_FALLBACK_ADVANCE_FRACTION,
        }
        assert math.isclose(getattr(resolve_job_spec(job)[0].content[0], field), defaults[field])
        assert math.isclose(resolve(job=0.3), 0.3)
        assert math.isclose(resolve(job=0.3, label=0.5), 0.5)
        assert math.isclose(resolve(job=0.3, label=0.5, line=0.7), 0.7)
        # An explicit 0.0 at the line is honored (only None means unset).
        assert math.isclose(resolve(job=0.3, label=0.5, line=0.0), 0.0)

    @pytest.mark.parametrize(
        "field",
        [
            "kerning_penetration_scale",
            "kerning_recession_scale",
            "kerning_min_gap",
            "fallback_advance_fraction",
        ],
    )
    def test_negative_rejected(self, field: str) -> None:
        """The knobs are non-negative (ge=0.0) at every level."""
        with pytest.raises(ValidationError):
            TextLine(text="X", **{field: -0.1})  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            JobSpec(job_name="J", **{field: -0.1})  # type: ignore[arg-type]


class TestCutterSizeCascade:
    """cutter_size cascades line -> label -> job -> auto (None default)."""

    @staticmethod
    def _job(**levels: float | None) -> JobSpec:
        """Build a single-line job applying cutter_size at the given levels."""
        job_kwargs: dict[str, float | None] = {}
        label_kwargs: dict[str, float | None] = {}
        line_kwargs: dict[str, float | None] = {}
        if "job" in levels:
            job_kwargs["cutter_size"] = levels["job"]
        if "label" in levels:
            label_kwargs["cutter_size"] = levels["label"]
        if "line" in levels:
            line_kwargs["cutter_size"] = levels["line"]
        return JobSpec(
            job_name="J",
            text_height=0.5,
            **job_kwargs,  # type: ignore[arg-type]
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    **label_kwargs,  # type: ignore[arg-type]
                    content=[TextLine(text="X", **line_kwargs)],  # type: ignore[arg-type]
                )
            ],
        )

    def test_default_module_constant_is_none(self) -> None:
        """The shipped default keeps automatic cutter selection."""
        assert DEFAULT_CUTTER_SIZE is None

    def test_auto_selection_when_all_omit(self) -> None:
        """All levels omitting keeps the height-based cutter and cutter_size None."""
        line = resolve_job_spec(self._job())[0].content[0]
        assert line.cutter_size is None
        assert math.isclose(line.cutter_diameter, get_cutter_diameter(0.5))
        assert math.isclose(line.toolpath_text_height, 0.5 - get_cutter_diameter(0.5))

    def test_job_value_used_when_label_omits(self) -> None:
        """Job-level cutter_size overrides the height-based lookup."""
        line = resolve_job_spec(self._job(job=0.09))[0].content[0]
        assert math.isclose(line.cutter_size, 0.09)
        assert math.isclose(line.cutter_diameter, 0.09)
        assert math.isclose(line.toolpath_text_height, 0.5 - 0.09)

    def test_label_overrides_job(self) -> None:
        """Label-level cutter_size beats the job-level value."""
        line = resolve_job_spec(self._job(job=0.09, label=0.06))[0].content[0]
        assert math.isclose(line.cutter_diameter, 0.06)

    def test_line_overrides_label(self) -> None:
        """Line-level cutter_size beats the label-level value."""
        line = resolve_job_spec(self._job(job=0.09, label=0.06, line=0.045))[0].content[0]
        assert math.isclose(line.cutter_diameter, 0.045)

    def test_nominal_text_height_unchanged(self) -> None:
        """An explicit cutter never changes the nominal height (vertical fit)."""
        line = resolve_job_spec(self._job(line=0.09))[0].content[0]
        assert math.isclose(line.nominal_text_height, 0.5)

    def test_character_spacing_fallback_uses_explicit_cutter(self) -> None:
        """The omitted character_spacing fallback tracks the resolved cutter."""
        line = resolve_job_spec(self._job(line=0.09))[0].content[0]
        assert math.isclose(line.character_spacing, 0.09 * 1.5)

    def test_snaps_down_to_inventory(self) -> None:
        """An off-inventory cutter snaps to the next size down."""
        job = self._job(line=0.05)
        line = resolve_job_spec(job, available_cutters=[0.03, 0.045, 0.06])[0].content[0]
        assert math.isclose(line.cutter_diameter, 0.045)
        assert math.isclose(line.cutter_size, 0.05)

    def test_snaps_up_when_no_smaller_tool(self, caplog: pytest.LogCaptureFixture) -> None:
        """With no smaller tool the cutter snaps up, logging a WARNING."""
        with caplog.at_level("WARNING"):
            line = resolve_job_spec(self._job(line=0.005), available_cutters=[0.02, 0.03])[
                0
            ].content[0]
        assert math.isclose(line.cutter_diameter, 0.02)
        assert "snapped" in caplog.text

    def test_no_inventory_uses_verbatim(self) -> None:
        """Without an inventory the requested cutter is used exactly."""
        line = resolve_job_spec(self._job(line=0.05))[0].content[0]
        assert math.isclose(line.cutter_diameter, 0.05)

    def test_cutter_at_or_above_height_raises(self) -> None:
        """A cutter >= text_height leaves no material and aborts the job."""
        with pytest.raises(CutterSizeError, match="no material to engrave"):
            resolve_job_spec(self._job(line=0.5))
        with pytest.raises(CutterSizeError, match="no material to engrave"):
            resolve_job_spec(self._job(line=0.75))

    def test_mixed_explicit_and_auto_lines(self) -> None:
        """A label can mix an explicit-cutter line with an auto-selected one."""
        job = JobSpec(
            job_name="J",
            text_height=0.5,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=4.0,
                    height=2.0,
                    content=[
                        TextLine(text="A", cutter_size=0.09),
                        TextLine(text="B"),
                    ],
                )
            ],
        )
        content = resolve_job_spec(job)[0].content
        assert math.isclose(content[0].cutter_diameter, 0.09)
        assert content[1].cutter_size is None
        assert math.isclose(content[1].cutter_diameter, get_cutter_diameter(0.5))


class TestCutterDownsizeCascade:
    """cutter_downsize cascades line -> label -> job -> True (explicit-None)."""

    @staticmethod
    def _job(**levels: bool | None) -> JobSpec:
        """Build a single-line job applying cutter_downsize at the given levels."""
        job_kwargs: dict[str, bool | None] = {}
        label_kwargs: dict[str, bool | None] = {}
        line_kwargs: dict[str, bool | None] = {}
        if "job" in levels:
            job_kwargs["cutter_downsize"] = levels["job"]
        if "label" in levels:
            label_kwargs["cutter_downsize"] = levels["label"]
        if "line" in levels:
            line_kwargs["cutter_downsize"] = levels["line"]
        return JobSpec(
            job_name="J",
            text_height=0.5,
            **job_kwargs,  # type: ignore[arg-type]
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    **label_kwargs,  # type: ignore[arg-type]
                    content=[TextLine(text="X", **line_kwargs)],  # type: ignore[arg-type]
                )
            ],
        )

    def test_default_module_constant_is_true(self) -> None:
        """The shipped default enables the compression-driven reduction."""
        assert DEFAULT_CUTTER_DOWNSIZE is True

    def test_default_resolves_true(self) -> None:
        """All levels omitting resolves to the True fallback."""
        line = resolve_job_spec(self._job())[0].content[0]
        assert line.cutter_downsize is True

    def test_job_false_used_when_label_omits(self) -> None:
        """Job-level False cascades to labels and lines that omit the field."""
        line = resolve_job_spec(self._job(job=False))[0].content[0]
        assert line.cutter_downsize is False

    def test_label_overrides_job(self) -> None:
        """Label-level True beats the job-level False."""
        line = resolve_job_spec(self._job(job=False, label=True))[0].content[0]
        assert line.cutter_downsize is True

    def test_line_overrides_label(self) -> None:
        """Line-level False beats the label-level True."""
        line = resolve_job_spec(self._job(job=True, label=True, line=False))[0].content[0]
        assert line.cutter_downsize is False


class TestMaxCutterDownsizesCascade:
    """max_cutter_downsizes cascades line -> label -> job -> 1 (explicit-None)."""

    @staticmethod
    def _job(**levels: int | None) -> JobSpec:
        """Build a single-line job applying max_cutter_downsizes at the given levels."""
        job_kwargs: dict[str, int | None] = {}
        label_kwargs: dict[str, int | None] = {}
        line_kwargs: dict[str, int | None] = {}
        if "job" in levels:
            job_kwargs["max_cutter_downsizes"] = levels["job"]
        if "label" in levels:
            label_kwargs["max_cutter_downsizes"] = levels["label"]
        if "line" in levels:
            line_kwargs["max_cutter_downsizes"] = levels["line"]
        return JobSpec(
            job_name="J",
            text_height=0.5,
            **job_kwargs,  # type: ignore[arg-type]
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    **label_kwargs,  # type: ignore[arg-type]
                    content=[TextLine(text="X", **line_kwargs)],  # type: ignore[arg-type]
                )
            ],
        )

    def test_default_module_constant_is_one(self) -> None:
        """The shipped default allows exactly one downsizing step."""
        assert DEFAULT_MAX_CUTTER_DOWNSIZES == 1

    def test_default_resolves_one(self) -> None:
        """All levels omitting resolves to the 1 fallback."""
        line = resolve_job_spec(self._job())[0].content[0]
        assert line.max_cutter_downsizes == 1

    def test_explicit_zero_is_honored(self) -> None:
        """An explicit 0 (mechanism disabled) never falls through to a parent."""
        line = resolve_job_spec(self._job(job=3, line=0))[0].content[0]
        assert line.max_cutter_downsizes == 0

    def test_job_value_used_when_label_omits(self) -> None:
        """Job-level budget cascades to labels and lines that omit the field."""
        line = resolve_job_spec(self._job(job=2))[0].content[0]
        assert line.max_cutter_downsizes == 2

    def test_label_overrides_job(self) -> None:
        """Label-level budget beats the job-level value."""
        line = resolve_job_spec(self._job(job=2, label=0))[0].content[0]
        assert line.max_cutter_downsizes == 0

    def test_line_overrides_label(self) -> None:
        """Line-level budget beats the label-level value."""
        line = resolve_job_spec(self._job(job=1, label=2, line=3))[0].content[0]
        assert line.max_cutter_downsizes == 3


class TestCutterDownsizeGlobalCascade:
    """cutter_downsize_global cascades line -> label -> job -> True (explicit-None)."""

    @staticmethod
    def _job(**levels: bool | None) -> JobSpec:
        """Build a single-line job applying cutter_downsize_global at the given levels."""
        job_kwargs: dict[str, bool | None] = {}
        label_kwargs: dict[str, bool | None] = {}
        line_kwargs: dict[str, bool | None] = {}
        if "job" in levels:
            job_kwargs["cutter_downsize_global"] = levels["job"]
        if "label" in levels:
            label_kwargs["cutter_downsize_global"] = levels["label"]
        if "line" in levels:
            line_kwargs["cutter_downsize_global"] = levels["line"]
        return JobSpec(
            job_name="J",
            text_height=0.5,
            **job_kwargs,  # type: ignore[arg-type]
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    **label_kwargs,  # type: ignore[arg-type]
                    content=[TextLine(text="X", **line_kwargs)],  # type: ignore[arg-type]
                )
            ],
        )

    def test_default_module_constant_is_true(self) -> None:
        """The shipped default enables per-label downsize sharing."""
        assert DEFAULT_CUTTER_DOWNSIZE_GLOBAL is True

    def test_default_resolves_true(self) -> None:
        """All levels omitting resolves to the True fallback."""
        line = resolve_job_spec(self._job())[0].content[0]
        assert line.cutter_downsize_global is True

    def test_job_false_used_when_label_omits(self) -> None:
        """Job-level False cascades to labels and lines that omit the field."""
        line = resolve_job_spec(self._job(job=False))[0].content[0]
        assert line.cutter_downsize_global is False

    def test_label_overrides_job(self) -> None:
        """Label-level True beats the job-level False."""
        line = resolve_job_spec(self._job(job=False, label=True))[0].content[0]
        assert line.cutter_downsize_global is True

    def test_line_overrides_label(self) -> None:
        """Line-level False beats the label-level True."""
        line = resolve_job_spec(self._job(job=True, label=True, line=False))[0].content[0]
        assert line.cutter_downsize_global is False


class TestHCompressGlobalCascade:
    """h_compress_global cascades line -> label -> job -> False (explicit-None)."""

    @staticmethod
    def _job(**levels: bool | None) -> JobSpec:
        """Build a single-line job applying h_compress_global at the given levels."""
        job_kwargs: dict[str, bool | None] = {}
        label_kwargs: dict[str, bool | None] = {}
        line_kwargs: dict[str, bool | None] = {}
        if "job" in levels:
            job_kwargs["h_compress_global"] = levels["job"]
        if "label" in levels:
            label_kwargs["h_compress_global"] = levels["label"]
        if "line" in levels:
            line_kwargs["h_compress_global"] = levels["line"]
        return JobSpec(
            job_name="J",
            text_height=0.5,
            **job_kwargs,  # type: ignore[arg-type]
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    **label_kwargs,  # type: ignore[arg-type]
                    content=[TextLine(text="X", **line_kwargs)],  # type: ignore[arg-type]
                )
            ],
        )

    def test_default_module_constant_is_false(self) -> None:
        """The shipped default keeps horizontal compression per-line."""
        assert DEFAULT_H_COMPRESS_GLOBAL is False

    def test_default_resolves_false(self) -> None:
        """All levels omitting resolves to the False fallback."""
        line = resolve_job_spec(self._job())[0].content[0]
        assert line.h_compress_global is False

    def test_job_true_used_when_label_omits(self) -> None:
        """Job-level True cascades to labels and lines that omit the field."""
        line = resolve_job_spec(self._job(job=True))[0].content[0]
        assert line.h_compress_global is True

    def test_label_overrides_job(self) -> None:
        """Label-level False beats the job-level True."""
        line = resolve_job_spec(self._job(job=True, label=False))[0].content[0]
        assert line.h_compress_global is False

    def test_line_overrides_label(self) -> None:
        """Line-level True beats the label-level False."""
        line = resolve_job_spec(self._job(job=False, label=False, line=True))[0].content[0]
        assert line.h_compress_global is True


class TestNextSmallerCutter:
    """next_smaller_cutter walks the shop inventory one step down."""

    def test_empty_inventory_returns_none(self) -> None:
        """No inventory means no ladder: the mechanism is a no-op."""
        assert next_smaller_cutter(0.1, None) is None
        assert next_smaller_cutter(0.1, []) is None

    def test_returns_largest_strictly_smaller(self) -> None:
        """The next step is the biggest tool below the current one."""
        assert next_smaller_cutter(0.1, [0.03, 0.045, 0.06, 0.09, 0.125]) == 0.09
        assert next_smaller_cutter(0.062, [0.03, 0.045, 0.06, 0.09]) == 0.06

    def test_exact_match_counts_as_current_not_smaller(self) -> None:
        """A tool equal to the current cutter (within tolerance) is skipped."""
        assert next_smaller_cutter(0.06, [0.03, 0.06, 0.09]) == 0.03
        assert next_smaller_cutter(0.06, [0.0600000001, 0.09]) is None

    def test_smallest_tool_returns_none(self) -> None:
        """The smallest tool has no step down."""
        assert next_smaller_cutter(0.03, [0.03, 0.045, 0.06]) is None

    def test_below_all_tools_returns_none(self) -> None:
        """A cutter smaller than every tool has no step down."""
        assert next_smaller_cutter(0.01, [0.03, 0.045]) is None


class TestShouldDownsizeCutter:
    """should_downsize_cutter applies the midpoint rule from the TODO."""

    def test_below_midpoint_downsizes(self) -> None:
        """0.1in cutter, 0.08in next size: 89% compression takes the smaller tool."""
        assert should_downsize_cutter(0.89, 0.1, 0.08) is True

    def test_at_midpoint_keeps_current(self) -> None:
        """Exactly at the midpoint (90%) keeps the original cutter."""
        assert should_downsize_cutter(0.90, 0.1, 0.08) is False

    def test_above_midpoint_keeps_current(self) -> None:
        """Compression closer to 100% keeps the original cutter."""
        assert should_downsize_cutter(0.95, 0.1, 0.08) is False

    def test_heavily_compressed_downsizes(self) -> None:
        """Deep compression always takes the smaller tool."""
        assert should_downsize_cutter(0.5, 0.1, 0.08) is True

    def test_natural_width_keeps_current(self) -> None:
        """An uncompressed line (scale 1.0) never changes cutters."""
        assert should_downsize_cutter(1.0, 0.1, 0.08) is False

    def test_non_positive_cutters_never_downsize(self) -> None:
        """Degenerate cutter diameters are rejected defensively."""
        assert should_downsize_cutter(0.1, 0.0, 0.0) is False
        assert should_downsize_cutter(0.1, 0.1, 0.0) is False


class TestMaterialCascade:
    """material must cascade label -> job -> None (no default)."""

    def test_default_is_none(self) -> None:
        """Unset material resolves to None (material-agnostic)."""
        job = JobSpec(
            job_name="M",
            labels=[
                LabelSpec(id="lbl", count=1, width=2.0, height=1.0, content=[TextLine(text="X")]),
            ],
        )
        assert resolve_job_spec(job)[0].material is None

    def test_job_value_cascades(self) -> None:
        """Job-level material applies to every label that omits it."""
        job = JobSpec(
            job_name="M",
            material="wb",
            labels=[
                LabelSpec(id="lbl", count=1, width=2.0, height=1.0, content=[TextLine(text="X")]),
            ],
        )
        assert resolve_job_spec(job)[0].material == "wb"

    def test_label_overrides_job(self) -> None:
        """A label-level material wins over the job-level value."""
        job = JobSpec(
            job_name="M",
            material="wb",
            labels=[
                LabelSpec(
                    id="lbl",
                    count=1,
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X")],
                    material="wb(uv)",
                ),
            ],
        )
        assert resolve_job_spec(job)[0].material == "wb(uv)"

    def test_root_level_job_cascades(self) -> None:
        """Root-level single-label jobs carry their own material."""
        job = JobSpec(
            job_name="M",
            material="wb",
            width=2.0,
            height=1.0,
            content=[TextLine(text="X")],
        )
        assert resolve_job_spec(job)[0].material == "wb"

    def test_resolved_label_defaults_to_none(self) -> None:
        """Manually constructed ResolvedLabel defaults material to None."""
        label = ResolvedLabel(
            id="x", count=1, width=1.0, height=1.0, margin=0.1, h_margin=0.1, v_margin=0.1
        )
        assert label.material is None


class TestOptimizeLineContentCascade:
    """optimize_line_content cascades line -> label -> job -> False (explicit-None)."""

    @staticmethod
    def _job(**levels: bool | None) -> JobSpec:
        """Build a single-line job applying optimize_line_content at the given levels."""
        job_kwargs: dict[str, bool | None] = {}
        label_kwargs: dict[str, bool | None] = {}
        line_kwargs: dict[str, bool | None] = {}
        if "job" in levels:
            job_kwargs["optimize_line_content"] = levels["job"]
        if "label" in levels:
            label_kwargs["optimize_line_content"] = levels["label"]
        if "line" in levels:
            line_kwargs["optimize_line_content"] = levels["line"]
        return JobSpec(
            job_name="J",
            text_height=0.5,
            **job_kwargs,  # type: ignore[arg-type]
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    **label_kwargs,  # type: ignore[arg-type]
                    content=[TextLine(text="X", **line_kwargs)],  # type: ignore[arg-type]
                )
            ],
        )

    def test_default_module_constant_is_false(self) -> None:
        """The shipped default keeps every line's authored text."""
        assert DEFAULT_OPTIMIZE_LINE_CONTENT is False

    def test_default_resolves_false(self) -> None:
        """All levels omitting resolves to the False fallback."""
        line = resolve_job_spec(self._job())[0].content[0]
        assert line.optimize_line_content is False

    def test_job_true_used_when_label_omits(self) -> None:
        """Job-level True cascades to labels and lines that omit the field."""
        line = resolve_job_spec(self._job(job=True))[0].content[0]
        assert line.optimize_line_content is True

    def test_label_overrides_job(self) -> None:
        """Label-level False beats the job-level True."""
        line = resolve_job_spec(self._job(job=True, label=False))[0].content[0]
        assert line.optimize_line_content is False

    def test_line_overrides_label(self) -> None:
        """Line-level True beats the label-level False."""
        line = resolve_job_spec(self._job(job=False, label=False, line=True))[0].content[0]
        assert line.optimize_line_content is True

    def test_resolved_line_defaults_to_false(self) -> None:
        """A manually constructed ResolvedTextLine defaults to False."""
        line = ResolvedTextLine(
            text="X",
            nominal_text_height=0.5,
            toolpath_text_height=0.44,
            cutter_diameter=0.06,
            character_spacing=0.09,
            line_spacing=0.1,
        )
        assert line.optimize_line_content is False


class TestOptimizeLineContentMaxLinesCascade:
    """optimize_line_content_max_lines cascades line -> label -> job -> None."""

    @staticmethod
    def _job(cap_levels: dict[str, int | None], **flags: bool | None) -> JobSpec:
        """Build a two-line job applying the cap at the given levels.

        Args:
            cap_levels: Where to declare the cap (``job`` / ``label`` /
                ``line0`` / ``line1`` keys map to values).
            flags: Where to enable reflow (``job`` / ``label`` / ``line0`` /
                ``line1`` keys map to booleans).

        Returns:
            A validated JobSpec (single label, two content lines).
        """
        job_kwargs: dict[str, object] = {}
        label_kwargs: dict[str, object] = {}
        line0_kwargs: dict[str, object] = {}
        line1_kwargs: dict[str, object] = {}
        if "job" in cap_levels:
            job_kwargs["optimize_line_content_max_lines"] = cap_levels["job"]
        if "label" in cap_levels:
            label_kwargs["optimize_line_content_max_lines"] = cap_levels["label"]
        if "line0" in cap_levels:
            line0_kwargs["optimize_line_content_max_lines"] = cap_levels["line0"]
        if "line1" in cap_levels:
            line1_kwargs["optimize_line_content_max_lines"] = cap_levels["line1"]
        if "job" in flags:
            job_kwargs["optimize_line_content"] = flags["job"]
        if "label" in flags:
            label_kwargs["optimize_line_content"] = flags["label"]
        if "line0" in flags:
            line0_kwargs["optimize_line_content"] = flags["line0"]
        if "line1" in flags:
            line1_kwargs["optimize_line_content"] = flags["line1"]
        return JobSpec(
            job_name="J",
            text_height=0.5,
            **job_kwargs,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    **label_kwargs,
                    content=[
                        TextLine(text="AAA BBB", **line0_kwargs),
                        TextLine(text="CCC", **line1_kwargs),
                    ],
                )
            ],
        )

    def test_default_module_constant_is_none(self) -> None:
        """The shipped default is unset (no growth)."""
        assert DEFAULT_OPTIMIZE_LINE_CONTENT_MAX_LINES is None

    def test_default_resolves_none(self) -> None:
        """All levels omitting the cap resolves to None."""
        job = self._job({}, job=True)
        line = resolve_job_spec(job)[0].content[0]
        assert line.optimize_line_content_max_lines is None

    def test_job_cap_cascades(self) -> None:
        """A job-level cap lands on every enabled line."""
        job = self._job({"job": 3}, job=True)
        lines = resolve_job_spec(job)[0].content
        assert all(line.optimize_line_content_max_lines == 3 for line in lines)

    def test_label_overrides_job(self) -> None:
        """Label-level cap beats the job-level cap."""
        job = self._job({"job": 3, "label": 5}, job=True)
        line = resolve_job_spec(job)[0].content[0]
        assert line.optimize_line_content_max_lines == 5

    def test_line_overrides_label(self) -> None:
        """Line-level cap beats the label-level cap (single-line group)."""
        job = self._job({"label": 5, "line0": 2}, line0=True)
        lines = resolve_job_spec(job)[0].content
        assert lines[0].optimize_line_content_max_lines == 2

    def test_fanout_applies_group_cap_to_every_line(self) -> None:
        """One line's cap fans out to the whole enabled group."""
        job = self._job({"line0": 4}, job=True)
        lines = resolve_job_spec(job)[0].content
        assert lines[0].optimize_line_content_max_lines == 4
        assert lines[1].optimize_line_content_max_lines == 4

    def test_equal_caps_are_not_a_conflict(self) -> None:
        """Two lines declaring the same cap validate fine."""
        job = self._job({"line0": 3, "line1": 3}, job=True)
        lines = resolve_job_spec(job)[0].content
        assert all(line.optimize_line_content_max_lines == 3 for line in lines)

    def test_conflicting_group_caps_raise(self) -> None:
        """Two different caps in one group abort with LineContentConfigError."""
        job = self._job({"line0": 2, "line1": 3}, job=True)
        with pytest.raises(LineContentConfigError, match="conflicting"):
            resolve_job_spec(job)

    def test_cap_without_enable_raises(self) -> None:
        """A cap on a line whose reflow resolves False aborts."""
        job = self._job({"line0": 3})  # optimize_line_content unset -> False
        with pytest.raises(LineContentConfigError, match="not enabled"):
            resolve_job_spec(job)

    def test_cap_with_line_enable_is_valid(self) -> None:
        """A line-level cap with a line-level enable resolves cleanly."""
        job = self._job({"line0": 3}, line0=True)
        lines = resolve_job_spec(job)[0].content
        assert lines[0].optimize_line_content_max_lines == 3
        assert lines[1].optimize_line_content_max_lines is None

    def test_resolved_line_defaults_to_none(self) -> None:
        """A manually constructed ResolvedTextLine defaults the cap to None."""
        line = ResolvedTextLine(
            text="X",
            nominal_text_height=0.5,
            toolpath_text_height=0.44,
            cutter_diameter=0.06,
            character_spacing=0.09,
            line_spacing=0.1,
        )
        assert line.optimize_line_content_max_lines is None


class TestBaselinePitchMath:
    """Pure baseline-geometry helpers (pitch, offsets, block height)."""

    def test_pitch_is_cap_height_plus_spacing(self) -> None:
        """Each gap's pitch is the upper line's height plus its spacing."""
        assert baseline_pitch([0.5, 0.5, 0.5], [0.1, 0.2]) == [0.6, 0.7]

    def test_pitch_clamps_negative_spacing(self) -> None:
        """A negative spacing floors at 0.0 (pitch never below the height)."""
        assert baseline_pitch([0.5, 0.5], [-0.2]) == [0.5]

    def test_offsets_descend_by_one_pitch_each(self) -> None:
        """Baselines start at 0 and step down by the pitch (+y up)."""
        offsets = baseline_offsets([0.5, 0.5, 0.4], [0.1, 0.2])
        assert len(offsets) == 3
        for got, want in zip(offsets, [0.0, -0.6, -1.3]):
            assert math.isclose(got, want, abs_tol=1e-12)

    def test_single_line_offsets(self) -> None:
        """A single line has one baseline at the origin."""
        assert baseline_offsets([0.5], []) == [0.0]

    def test_block_height_without_descenders_is_ink_box(self) -> None:
        """Uniform cap-only lines reproduce the ink-box height exactly."""
        height = baseline_block_height([0.5, 0.5], [0.5, 0.5], [0.0, 0.0], [0.1])
        assert math.isclose(height, 0.5 + 0.1 + 0.5, abs_tol=1e-12)

    def test_block_height_grows_by_the_descender_depth(self) -> None:
        """A descender on the last line extends the block below its baseline."""
        heights = [0.5, 0.5]
        plain = baseline_block_height(heights, [0.5, 0.5], [0.0, 0.0], [0.1])
        with_desc = baseline_block_height(heights, [0.5, 0.5], [0.0, 0.12], [0.1])
        assert math.isclose(with_desc - plain, 0.12, abs_tol=1e-12)

    def test_interior_descender_does_not_grow_the_block(self) -> None:
        """A descender hanging into a roomy gap stays inside the block."""
        heights = [0.5, 0.5, 0.5]
        plain = baseline_block_height(heights, [0.5] * 3, [0.0] * 3, [0.4, 0.4])
        middle = baseline_block_height(heights, [0.5] * 3, [0.0, 0.1, 0.0], [0.4, 0.4])
        assert math.isclose(middle, plain, abs_tol=1e-12)


class TestFitBaselineSpacingToMargins:
    """Baseline-spacing margin clamp (bisection on the spacing factor)."""

    def test_returns_input_when_block_fits(self) -> None:
        """A block inside the inner area keeps its requested spacings."""
        spacings = fit_baseline_spacing_to_margins([0.5, 0.5], [0.5, 0.5], [0.0, 0.0], [0.1], 2.0)
        assert spacings == [0.1]

    def test_no_spacings_is_a_no_op(self) -> None:
        """Single-line content has no gaps to clamp."""
        assert fit_baseline_spacing_to_margins([0.5], [0.5], [0.0], [], 2.0) == []

    def test_descenders_are_charged_to_the_block(self) -> None:
        """Descenders reaching the inner edge shrink the spacing."""
        heights, asc = [0.5, 0.5], [0.5, 0.5]
        plain = fit_baseline_spacing_to_margins(heights, asc, [0.0, 0.0], [0.2], 1.2)
        desc = fit_baseline_spacing_to_margins(heights, asc, [0.0, 0.1], [0.2], 1.2)
        assert plain == [0.2]
        assert desc[0] < plain[0]

    def test_fits_exactly_after_clamping(self) -> None:
        """The clamped spacing makes the ink block fill the inner area."""
        heights, asc, desc = [0.5, 0.5], [0.5, 0.5], [0.0, 0.12]
        adjusted = fit_baseline_spacing_to_margins(heights, asc, desc, [0.5], 1.2)
        block = baseline_block_height(heights, asc, desc, adjusted)
        assert math.isclose(block, 1.2, abs_tol=1e-6)

    def test_never_increases_spacing(self) -> None:
        """Clamping only ever reduces (margins take precedence)."""
        adjusted = fit_baseline_spacing_to_margins([0.5, 0.5], [0.5, 0.5], [0.0, 0.0], [0.9], 1.05)
        assert adjusted[0] < 0.9

    def test_collapses_to_zero_when_heights_overflow(self) -> None:
        """When the block overflows with no spacing, every spacing hits 0.0."""
        adjusted = fit_baseline_spacing_to_margins([0.5, 0.5], [0.5, 0.5], [0.0, 0.3], [0.4], 1.0)
        assert adjusted == [0.0]

    def test_zero_total_spacing_returns_input(self) -> None:
        """No spacing to remove means nothing to scale (early exit)."""
        assert fit_baseline_spacing_to_margins([0.5, 0.5], [0.5, 0.5], [0.0, 0.0], [0.0], 0.6) == [
            0.0
        ]

    def test_matches_ink_box_math_for_cap_only_lines(self) -> None:
        """Uniform cap-only lines agree with the ink-box clamp exactly."""
        heights = [0.5, 0.5, 0.5]
        baseline = fit_baseline_spacing_to_margins(
            heights, heights, [0.0, 0.0, 0.0], [0.4, 0.2], 1.9
        )
        ink_box = fit_line_spacing_to_margins(heights, [0.4, 0.2], 1.9)
        for got, want in zip(baseline, ink_box):
            assert math.isclose(got, want, abs_tol=1e-6)


class TestSolveBaselineSpacingToFill:
    """Explicit-v_margin auto spacing under baseline stacking."""

    def test_fills_the_available_height_exactly(self) -> None:
        """The solved spacing makes the ink block equal the inner height."""
        heights, asc, desc = [0.5, 0.5, 0.5], [0.5, 0.5, 0.5], [0.0, 0.15, 0.0]
        spacing = solve_baseline_spacing_to_fill(heights, asc, desc, 2.0)
        block = baseline_block_height(heights, asc, desc, [spacing, spacing])
        assert math.isclose(block, 2.0, abs_tol=1e-6)

    def test_matches_ink_box_math_without_descenders(self) -> None:
        """Cap-only content reproduces the closed-form nominal division."""
        heights = [0.5, 0.5, 0.5]
        solved = solve_baseline_spacing_to_fill(heights, heights, [0.0, 0.0, 0.0], 2.0)
        assert math.isclose(solved, (2.0 - 1.5) / 2.0, abs_tol=1e-9)

    def test_descenders_reduce_the_spacing(self) -> None:
        """A descender claims space, so the spacing term shrinks."""
        heights = [0.5, 0.5]
        plain = solve_baseline_spacing_to_fill(heights, heights, [0.0, 0.0], 1.4)
        desc = solve_baseline_spacing_to_fill(heights, heights, [0.0, 0.2], 1.4)
        assert desc < plain

    def test_overflow_returns_zero(self) -> None:
        """No room for spacing yields 0.0 (never negative)."""
        assert solve_baseline_spacing_to_fill([0.5, 0.5], [0.5, 0.5], [0.0, 0.3], 1.0) == 0.0

    def test_single_line_returns_zero(self) -> None:
        """A single line has no gaps."""
        assert solve_baseline_spacing_to_fill([0.5], [0.5], [0.0], 2.0) == 0.0


class TestSolveBaselineSpacingToRatio:
    """Auto-v_margin auto spacing under baseline stacking."""

    def test_ratio_balances_top_and_interline_gaps(self) -> None:
        """ratio=1 makes every gap equal and fills the label exactly."""
        heights = [0.5, 0.5, 0.5]
        spacing, v_margin = solve_baseline_spacing_to_ratio(
            2.0, heights, heights, [0.0, 0.0, 0.0], 1.0
        )
        assert math.isclose(spacing, v_margin, abs_tol=1e-9)
        block = baseline_block_height(heights, heights, [0.0, 0.0, 0.0], [spacing, spacing])
        assert math.isclose(2 * v_margin + block, 2.0, abs_tol=1e-6)

    def test_matches_ink_box_math_without_descenders(self) -> None:
        """Cap-only content reproduces the closed-form ratio division."""
        heights = [0.5, 0.5, 0.5]
        spacing, v_margin = solve_baseline_spacing_to_ratio(
            2.0, heights, heights, [0.0, 0.0, 0.0], 1.3
        )
        expected_gap = (2.0 - 1.5) / (2.0 + 2 * 1.3)
        assert math.isclose(v_margin, expected_gap, abs_tol=1e-6)
        assert math.isclose(spacing, 1.3 * expected_gap, abs_tol=1e-6)

    def test_descenders_shrink_the_gaps(self) -> None:
        """The block fills the label, so descender ink eats into the gaps."""
        heights = [0.5, 0.5]
        plain = solve_baseline_spacing_to_ratio(2.0, heights, heights, [0.0, 0.0], 1.0)
        desc = solve_baseline_spacing_to_ratio(2.0, heights, heights, [0.0, 0.25], 1.0)
        assert desc[1] < plain[1]
        assert desc[0] < plain[0]

    def test_single_line_centers(self) -> None:
        """A single line has no gaps; the margin is half the label."""
        assert solve_baseline_spacing_to_ratio(2.0, [0.5], [0.5], [0.0], 1.0) == (0.0, 1.0)


class TestMemoizeExtentsProbe:
    """The production probe wrapper caches per line."""

    def test_each_line_measured_once(self) -> None:
        """Identical lines (value equality) hit the cache."""
        calls: list[str] = []

        def probe(line: ResolvedTextLine) -> tuple[float, float]:
            calls.append(line.text)
            return (0.4, 0.1)

        memoized = memoize_extents_probe(probe)
        line_a = _make_line("ABC")
        line_b = _make_line("ABC")
        assert line_a == line_b
        assert memoized(line_a) == memoized(line_b)
        assert calls == ["ABC"]


class TestUseBaselineSpacingCascade:
    """use_baseline_spacing cascades label -> job -> True (explicit-None)."""

    @staticmethod
    def _job(**levels: bool | None) -> JobSpec:
        """Build a one-label job applying use_baseline_spacing at the given levels."""
        job_kwargs: dict[str, bool | None] = {}
        label_kwargs: dict[str, bool | None] = {}
        if "job" in levels:
            job_kwargs["use_baseline_spacing"] = levels["job"]
        if "label" in levels:
            label_kwargs["use_baseline_spacing"] = levels["label"]
        return JobSpec(
            job_name="J",
            text_height=0.5,
            **job_kwargs,  # type: ignore[arg-type]
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    **label_kwargs,  # type: ignore[arg-type]
                    content=[TextLine(text="X"), TextLine(text="Y")],
                )
            ],
        )

    def test_default_module_constant_is_true(self) -> None:
        """The shipped default enables baseline spacing."""
        assert DEFAULT_USE_BASELINE_SPACING is True

    def test_default_resolves_true(self) -> None:
        """All levels omitting resolves to the True fallback."""
        assert resolve_job_spec(self._job())[0].use_baseline_spacing is True

    def test_job_false_used_when_label_omits(self) -> None:
        """Job-level False cascades onto labels that omit the field."""
        assert resolve_job_spec(self._job(job=False))[0].use_baseline_spacing is False

    def test_label_overrides_job(self) -> None:
        """Label-level True beats the job-level False."""
        assert (
            resolve_job_spec(self._job(job=False, label=True))[0].use_baseline_spacing is True
        )

    def test_label_false_is_honored(self) -> None:
        """An intentional False (ink-box stacking) never falls through."""
        assert resolve_job_spec(self._job(job=True, label=False))[0].use_baseline_spacing is False

    def test_root_level_job_resolves_own_value(self) -> None:
        """A root-level job masquerading as a label resolves its own field."""
        job = JobSpec(
            job_name="J",
            width=2.0,
            height=1.0,
            text_height=0.5,
            content=[TextLine(text="X")],
            use_baseline_spacing=False,
        )
        assert resolve_job_spec(job)[0].use_baseline_spacing is False

    def test_resolved_label_defaults_true(self) -> None:
        """A manually constructed ResolvedLabel defaults to the shipped default."""
        label = ResolvedLabel(
            id="l",
            count=1,
            width=2.0,
            height=1.0,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
        )
        assert label.use_baseline_spacing is DEFAULT_USE_BASELINE_SPACING


class TestAutoLineSpacingBaselineProbe:
    """Option B: auto spacing measures real descender extents."""

    @staticmethod
    def _job(**label_kwargs: object) -> JobSpec:
        """Build a two-line job (one with descenders) using auto line_spacing."""
        return JobSpec(
            job_name="J",
            width=4.0,
            height=2.0,
            text_height=0.5,
            margin=0.2,
            labels=[
                LabelSpec(
                    id="lbl",
                    content=[TextLine(text="gy"), TextLine(text="ABC")],
                    **label_kwargs,  # type: ignore[arg-type]
                )
            ],
        )

    @staticmethod
    def _probe(line: ResolvedTextLine) -> tuple[float, float]:
        """Fake extents: the descender line hangs 0.20in below its baseline."""
        cap = line.toolpath_text_height
        return (cap, 0.20 if "g" in line.text else 0.0)

    def test_probe_unused_without_baseline_spacing(self) -> None:
        """The ink-box path never calls the probe (no renders paid for)."""
        calls: list[str] = []

        def probe(line: ResolvedTextLine) -> tuple[float, float]:
            calls.append(line.text)
            return (line.toolpath_text_height, 0.0)

        resolve_job_spec(self._job(use_baseline_spacing=False), extents_probe=probe)
        assert calls == []

    def test_probe_unused_without_a_probe(self) -> None:
        """Baseline spacing without a probe keeps the nominal-height math."""
        label = resolve_job_spec(self._job())[0]
        assert label.use_baseline_spacing is True
        # Auto v_margin, ratio 1.0: 2 * gap + (1.0 + gap) == 2.0 -> gap = 1 / 3.
        assert math.isclose(label.content[0].line_spacing, 1.0 / 3.0, abs_tol=1e-9)

    def test_probe_changes_resolved_spacing(self) -> None:
        """Measured descenders move the resolved spacing off the nominal math."""
        plain = resolve_job_spec(self._job())[0]
        measured = resolve_job_spec(self._job(), extents_probe=self._probe)[0]
        assert not math.isclose(
            measured.content[0].line_spacing, plain.content[0].line_spacing, abs_tol=1e-6
        )

    def test_probe_resolves_uniform_spacing(self) -> None:
        """Every gap gets the same spacing, sized for the measured descender."""
        label = resolve_job_spec(self._job(), extents_probe=self._probe)[0]
        spacings = [line.line_spacing for line in label.content[:-1]]
        assert len(set(spacings)) == 1
        # ratio 1.0: interline gap == v_margin, so spacing == v_margin.
        assert math.isclose(spacings[0], label.v_margin, abs_tol=1e-6)
        # The descender-extended block plus the two margins fills the label.
        heights = [line.toolpath_text_height for line in label.content]
        block = baseline_block_height(heights, heights, [0.20, 0.0], spacings)
        assert math.isclose(2 * label.v_margin + block, 2.0, abs_tol=1e-6)

    def test_explicit_v_margin_uses_fill_solver(self) -> None:
        """An explicit v_margin fills the fixed inner area exactly."""
        label = resolve_job_spec(
            self._job(v_margin=0.3), extents_probe=self._probe
        )[0]
        heights = [line.toolpath_text_height for line in label.content]
        asc = [h for h in heights]
        desc = [0.20, 0.0]
        block = baseline_block_height(heights, asc, desc, [label.content[0].line_spacing])
        assert math.isclose(block, 2.0 - 2 * 0.3, abs_tol=1e-6)

    def test_margin_fit_clamps_explicit_spacing(self) -> None:
        """Explicit spacing is clamped against the descender-extended block."""
        label = resolve_job_spec(
            self._job(line_spacing=1.0), extents_probe=self._probe
        )[0]
        plain = resolve_job_spec(self._job(line_spacing=1.0))[0]
        # Inner height 2.0 - 2 * 0.2 = 1.6. The probe run stacks on cap heights
        # (0.44): the first line's descender hangs into the gap, so the block
        # is A0 + pitch = 0.44 + (0.44 + s) = 1.6 -> s = 0.72. The probe-less
        # run divides the nominal heights (0.5): 0.5 + (0.5 + s) = 1.6 -> 0.60.
        assert math.isclose(label.content[0].line_spacing, 0.72, abs_tol=1e-6)
        assert math.isclose(plain.content[0].line_spacing, 0.6, abs_tol=1e-6)
