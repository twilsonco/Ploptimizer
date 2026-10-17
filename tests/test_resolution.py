"""Tests for the resolution engine that flattens JobSpec into ResolvedLabel."""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from plt_optimizer.generate.job_config import (
    JobConfig,
    JobDefaults,
    apply_job_config_defaults,
)
from plt_optimizer.generate.resolution import (
    DEFAULT_AUTO_DESCENDING_CHARACTER_ADJUSTMENT,
    DEFAULT_BOLD_BOLDNESS_MULTIPLIER,
    DEFAULT_BOUNDARY_HOLE_CUTTER,
    DEFAULT_CUTTER_DOWNSIZE,
    DEFAULT_CUTTER_DOWNSIZE_GLOBAL,
    DEFAULT_CUTTER_SIZE,
    DEFAULT_DESCENDING_CHARACTER_CLEARANCE,
    DEFAULT_DESCENDING_CHARACTER_RAISE,
    DEFAULT_DESCENDING_CHARACTER_RECIPROCAL_SCALE,
    DEFAULT_DESCENDING_CHARACTER_V_SCALE,
    DEFAULT_ENFORCE_CAP_LINE,
    DEFAULT_FALLBACK_ADVANCE_FRACTION,
    DEFAULT_FONT,
    DEFAULT_H_COMPRESS_GLOBAL,
    DEFAULT_HOLE_ADJUST_FOR_CUTTER,
    DEFAULT_HOLE_MARGIN,
    DEFAULT_HOLE_TEXT_COLLISION_DISTANCE,
    DEFAULT_HTML_BRACKETS,
    DEFAULT_ITALIC_ANGLE,
    DEFAULT_KERNING_MIN_GAP,
    DEFAULT_KERNING_PENETRATION_SCALE,
    DEFAULT_KERNING_RECESSION_SCALE,
    DEFAULT_KERNING_WINDOW_FRACTION,
    DEFAULT_LABEL_SHAPE,
    DEFAULT_ROUNDED_RECTANGLE_FRACTION,
    DEFAULT_ROUNDED_RECTANGLE_PROPORTIONAL,
    DEFAULT_LINE_SPACING,
    DEFAULT_MARGIN,
    DEFAULT_MAX_CUTTER_DOWNSIZES,
    DEFAULT_MAX_H_COMPRESS,
    DEFAULT_MIN_GLYPH_WIDTH,
    DEFAULT_MIN_HOLE_MARGIN,
    DEFAULT_MULTISTROKE_OVERLAP_RATIO,
    DEFAULT_OPTIMIZE_LINE_CONTENT,
    DEFAULT_OPTIMIZE_LINE_CONTENT_MAX_LINES,
    DEFAULT_SC_SCALE,
    DEFAULT_SPACE_WIDTH_FRACTION,
    DEFAULT_STRIKETHROUGH_BREAK_SPACES,
    DEFAULT_STRIKETHROUGH_OFFSET,
    DEFAULT_SUPERSUBSCRIPT_DOWNSCALE,
    DEFAULT_TARGET_BOLDNESS_FACTOR,
    DEFAULT_TEXT_COLOR,
    DEFAULT_TEXT_H_ALIGNMENT,
    DEFAULT_TEXT_HEIGHT,
    DEFAULT_UNDERLINE_BREAK_CLEARANCE,
    DEFAULT_UNDERLINE_BREAK_SPACES,
    DEFAULT_UNDERLINE_OFFSET,
    DEFAULT_USE_BASELINE_SPACING,
    DEFAULT_V_CENTER_BY_CONTENT,
    DESIGN_MULTISTROKE_OVERLAP,
    IDEAL_CUTTER_MAP,
    MAX_TARGET_BOLDNESS_FACTOR,
    MIN_HOLE_EMITTED_RADIUS_INCHES,
    CutterSizeError,
    LineContentConfigError,
    MultiStrokeCutter,
    ResolvedHoleSpec,
    ResolvedLabel,
    ResolvedTextLine,
    baseline_block_height,
    baseline_offsets,
    baseline_pitch,
    build_cutter_pen_map,
    compute_horizontal_offset,
    compute_horizontal_scale,
    ellipse_content_inset,
    design_pass_spacing,
    fit_baseline_spacing_to_margins,
    fit_line_spacing_to_margins,
    get_cutter_diameter,
    glyph_render_plan,
    hole_engraved_radius,
    memoize_extents_probe,
    multi_stroke_cutter_diameter,
    next_larger_cutter,
    next_smaller_cutter,
    resolve_job_spec,
    scale_cutter_ladder,
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


class TestMarginAgainstConfigAxisMargins:
    """A spec-declared margin must apply on every axis, config notwithstanding.

    Regression: a shop config shipping ``h_margin`` was injected at the job
    layer, where the margin cascade checks it *before* ``margin``, so a
    YAML ``margin`` resolved vertically but lost horizontally.
    """

    def test_spec_margin_wins_over_config_h_margin(self, tmp_path: Path) -> None:
        """margin=2.0 + config h_margin=0.3 resolves 2.0in on both axes."""
        job_data = {
            "job_name": "Coolsys",
            "width": 24.0,
            "height": 16.0,
            "text_height": 1.0,
            "margin": 2.0,
            "labels": [{"id": "a", "content": [{"text": "Hello"}]}],
        }
        config = JobConfig(path=tmp_path / "job-config.json", defaults=JobDefaults(h_margin=0.3))
        filled = apply_job_config_defaults(job_data, config)
        label = resolve_job_spec(JobSpec(**filled))[0]
        assert math.isclose(label.h_margin, 2.0)
        assert math.isclose(label.v_margin, 2.0)

    def test_config_axis_margin_applies_without_spec_margin(self, tmp_path: Path) -> None:
        """Without a spec margin the configured h_margin keeps working."""
        job_data = {
            "job_name": "Shop",
            "width": 24.0,
            "height": 16.0,
            "text_height": 1.0,
            "labels": [{"id": "a", "content": [{"text": "Hello"}]}],
        }
        config = JobConfig(path=tmp_path / "job-config.json", defaults=JobDefaults(h_margin=0.3))
        filled = apply_job_config_defaults(job_data, config)
        label = resolve_job_spec(JobSpec(**filled))[0]
        assert math.isclose(label.h_margin, 0.3)
        assert math.isclose(label.v_margin, DEFAULT_MARGIN)


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


class TestSmallCapsScaleCascade:
    """``sc_scale`` must cascade line -> label -> job -> default."""

    @staticmethod
    def _job(**line_kwargs: object) -> JobSpec:
        """Build a one-label/one-line job carrying optional line overrides."""
        return JobSpec(
            job_name="J",
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X", **line_kwargs)],  # type: ignore[arg-type]
                )
            ],
        )

    def test_default_when_all_omit(self) -> None:
        """All levels omitting yields the 0.7 small-caps default."""
        label = resolve_job_spec(self._job())[0]
        assert math.isclose(label.content[0].sc_scale, DEFAULT_SC_SCALE)
        assert DEFAULT_SC_SCALE == 0.7

    def test_job_value_used_when_label_omits(self) -> None:
        """Job-level value cascades to the line."""
        job = self._job().model_copy(update={"sc_scale": 0.3})
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.content[0].sc_scale, 0.3)

    def test_label_overrides_job(self) -> None:
        """Label-level value overrides the job-level value."""
        job = JobSpec(
            job_name="J",
            sc_scale=0.3,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    sc_scale=0.9,
                    content=[TextLine(text="X")],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.content[0].sc_scale, 0.9)

    def test_line_overrides_label(self) -> None:
        """Line-level value overrides the label-level value."""
        job = JobSpec(
            job_name="J",
            sc_scale=0.3,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    sc_scale=0.9,
                    content=[TextLine(text="X", sc_scale=0.5)],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.content[0].sc_scale, 0.5)

    def test_explicit_one_is_honored(self) -> None:
        """An explicit 1.0 (base-identical rendering) beats the parents."""
        job = JobSpec(
            job_name="J",
            sc_scale=0.3,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    sc_scale=0.9,
                    content=[TextLine(text="X", sc_scale=1.0)],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        assert math.isclose(label.content[0].sc_scale, 1.0)

    def test_scale_never_touches_the_cutter(self) -> None:
        """The small-caps scale must not change the resolved heights/cutter."""
        job = JobSpec(
            job_name="J",
            text_height=0.5,
            font="Dino SC",
            sc_scale=0.4,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="Ab")],
                )
            ],
        )
        label = resolve_job_spec(job)[0]
        line = label.content[0]
        assert line.font == "Dino SC"
        assert math.isclose(line.nominal_text_height, 0.5)
        assert line.toolpath_text_height == pytest.approx(0.5 - line.cutter_diameter)

    def test_out_of_bounds_rejected(self) -> None:
        """The scale is bounded to [0.2, 1.0] at every level."""
        with pytest.raises(ValueError):
            TextLine(text="X", sc_scale=0.19)
        with pytest.raises(ValueError):
            TextLine(text="X", sc_scale=1.01)
        with pytest.raises(ValueError):
            JobSpec(job_name="J", sc_scale=0.0)


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


class TestNextLargerCutter:
    """next_larger_cutter walks the inventory up (the downsize ladder's mirror)."""

    def test_returns_next_up(self) -> None:
        assert next_larger_cutter(0.02, [0.01, 0.015, 0.02, 0.03, 0.045]) == 0.03

    def test_none_when_largest(self) -> None:
        assert next_larger_cutter(0.25, [0.01, 0.06, 0.25]) is None

    def test_none_without_inventory(self) -> None:
        assert next_larger_cutter(0.02, None) is None
        assert next_larger_cutter(0.02, []) is None

    def test_tie_is_not_larger(self) -> None:
        """A tool within tolerance of current is not 'larger' (no self-bump)."""
        assert next_larger_cutter(0.02, [0.02, 0.03]) == 0.03


class TestMultiStrokeCutterMath:
    """The pure boldness ladder: pass spacing, cutter, floor, implicit overlap."""

    def test_defaults_reproduce_the_classic_formula(self) -> None:
        """At the defaults the ladder equals text_height * 0.14 / U (N-1)*0.75."""
        # The TODO's rules of thumb: 3/5/7-stroke fonts at 25% overlap.
        for strokes, fraction in ((3, 0.056), (5, 0.035), (7, 0.025)):
            ladder = multi_stroke_cutter_diameter(1.0, strokes)
            units = 1.0 + (strokes - 1) * (1.0 - DESIGN_MULTISTROKE_OVERLAP)
            assert math.isclose(ladder.cutter, 0.14 / units, rel_tol=1e-9)
            assert math.isclose(ladder.cutter, fraction, rel_tol=0.02)
            # The design point sits exactly on the gap-free floor.
            assert not ladder.clamped
            assert math.isclose(ladder.implicit_overlap, 0.25, abs_tol=1e-9)

    def test_pass_spacing_scales_with_height(self) -> None:
        """The baked spacing is linear in the text height and zero for N<=1."""
        assert design_pass_spacing(0.5, 1) == 0.0
        assert math.isclose(design_pass_spacing(1.0, 5), 2.0 * design_pass_spacing(0.5, 5))

    def test_boldness_escape_grows_cutter_and_overlap(self) -> None:
        """A heavier factor grows the cutter and the implicit overlap past the floor."""
        ladder = multi_stroke_cutter_diameter(0.5, 5, boldness_factor=0.20)
        # W = 0.20*0.5 - 4*d; d = 0.75*0.14*0.5/4 = 0.013125.
        assert math.isclose(ladder.cutter, 0.0475, rel_tol=1e-9)
        assert not ladder.clamped
        assert math.isclose(ladder.implicit_overlap, 1.0 - 0.013125 / 0.0475, rel_tol=1e-9)
        assert ladder.implicit_overlap > 0.25

    def test_thin_target_clamps_to_the_floor(self) -> None:
        """A light factor wants a thinner cutter than the floor allows -> clamped."""
        ladder = multi_stroke_cutter_diameter(0.5, 5, boldness_factor=0.08)
        assert ladder.clamped
        assert math.isclose(ladder.cutter, ladder.floor, rel_tol=1e-9)
        # The floor is d / (1 - 0.25) with d = 0.013125.
        assert math.isclose(ladder.floor, 0.013125 / 0.75, rel_tol=1e-9)
        assert math.isclose(ladder.implicit_overlap, 0.25, abs_tol=1e-9)

    def test_higher_floor_lowers_the_floor_bound(self) -> None:
        """A lower overlap floor permits a thinner cutter (thinning)."""
        strict = multi_stroke_cutter_diameter(0.5, 5, boldness_factor=0.08, overlap_floor=0.25)
        loose = multi_stroke_cutter_diameter(0.5, 5, boldness_factor=0.08, overlap_floor=0.1)
        assert loose.floor < strict.floor

    def test_floor_at_half_is_two_pass_spacings(self) -> None:
        """At floor 0.5 the cutter floor is exactly 2 * pass spacing."""
        ladder = multi_stroke_cutter_diameter(0.5, 3, boldness_factor=0.14, overlap_floor=0.5)
        spacing = design_pass_spacing(0.5, 3)
        assert math.isclose(ladder.floor, 2.0 * spacing, rel_tol=1e-9)
        assert ladder.clamped  # the 0.14 target sits below this heavier floor

    def test_single_stroke_spacing_is_zero(self) -> None:
        assert design_pass_spacing(0.5, 1) == 0.0


class TestMultistrokeCascade:
    """The two multi-stroke fields cascade line -> label -> job -> default."""

    @staticmethod
    def _job(**levels: float | None) -> JobSpec:
        """Build a single-line job applying both fields at the given levels."""
        job_kwargs: dict[str, float | None] = {}
        label_kwargs: dict[str, float | None] = {}
        line_kwargs: dict[str, float | None] = {}
        for key, value in levels.items():
            field, tier = key.split("__")
            {"job": job_kwargs, "label": label_kwargs, "line": line_kwargs}[tier][field] = value
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

    def test_defaults(self) -> None:
        """Unset fields resolve to the module defaults."""
        assert DEFAULT_TARGET_BOLDNESS_FACTOR == 0.14
        assert DEFAULT_MULTISTROKE_OVERLAP_RATIO == 0.25
        line = resolve_job_spec(self._job())[0].content[0]
        assert math.isclose(line.target_boldness_factor, 0.14)
        assert math.isclose(line.multistroke_overlap_ratio, 0.25)

    def test_job_tier(self) -> None:
        line = resolve_job_spec(
            self._job(**{"target_boldness_factor__job": 0.2, "multistroke_overlap_ratio__job": 0.4})
        )[0].content[0]
        assert math.isclose(line.target_boldness_factor, 0.2)
        assert math.isclose(line.multistroke_overlap_ratio, 0.4)

    def test_label_overrides_job(self) -> None:
        line = resolve_job_spec(
            self._job(
                **{
                    "target_boldness_factor__job": 0.2,
                    "target_boldness_factor__label": 0.1,
                    "multistroke_overlap_ratio__job": 0.4,
                    "multistroke_overlap_ratio__label": 0.15,
                }
            )
        )[0].content[0]
        assert math.isclose(line.target_boldness_factor, 0.1)
        assert math.isclose(line.multistroke_overlap_ratio, 0.15)

    def test_line_overrides_label(self) -> None:
        line = resolve_job_spec(
            self._job(
                **{
                    "target_boldness_factor__label": 0.1,
                    "target_boldness_factor__line": 0.25,
                    "multistroke_overlap_ratio__label": 0.15,
                    "multistroke_overlap_ratio__line": 0.5,
                }
            )
        )[0].content[0]
        assert math.isclose(line.target_boldness_factor, 0.25)
        assert math.isclose(line.multistroke_overlap_ratio, 0.5)

    def test_explicit_bounds_honored(self) -> None:
        """Explicit 0.05/0.1 (the field floors) are honored, not treated unset."""
        line = resolve_job_spec(
            self._job(
                **{
                    "target_boldness_factor__line": 0.05,
                    "multistroke_overlap_ratio__line": 0.1,
                }
            )
        )[0].content[0]
        assert math.isclose(line.target_boldness_factor, 0.05)
        assert math.isclose(line.multistroke_overlap_ratio, 0.1)


class TestMultiStrokeCutterResolution:
    """The ladder drives the resolved cutter for a multi-stroke PLT font.

    Uses the shipped library's real multi-stroke fonts (Avenge5 = S5,
    Helter2 = S2) so the stroke_count lookup is exercised end-to-end.
    """

    @staticmethod
    def _job(font: str, **line_kwargs: object) -> JobSpec:
        return JobSpec(
            job_name="J",
            text_height=0.5,
            font=font,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=4.0,
                    height=2.0,
                    content=[TextLine(text="X", **line_kwargs)],  # type: ignore[arg-type]
                )
            ],
        )

    def test_single_stroke_font_uses_auto_selection(self) -> None:
        """A S1 font (Dino) keeps the height-based cutter exactly (bit-identical)."""
        line = resolve_job_spec(self._job("Dino"))[0].content[0]
        assert math.isclose(line.cutter_diameter, get_cutter_diameter(0.5))

    def test_multi_stroke_default_cutter_no_inventory(self) -> None:
        """Avenge5 (S5) at the defaults resolves to 3.5% of the text height."""
        line = resolve_job_spec(self._job("Avenge5"))[0].content[0]
        # W = 0.14*0.5 / (1 + 4*0.75) = 0.0175, no inventory -> verbatim.
        assert math.isclose(line.cutter_diameter, 0.0175, rel_tol=1e-9)
        assert math.isclose(line.toolpath_text_height, 0.5 - 0.0175, rel_tol=1e-9)
        assert math.isclose(line.nominal_text_height, 0.5)

    def test_multi_stroke_snaps_up_to_the_floor(self, caplog: pytest.LogCaptureFixture) -> None:
        """The snap-down tool breaks the gap-free floor, so the ladder steps up."""
        with caplog.at_level("WARNING"):
            line = resolve_job_spec(
                self._job("Avenge5"), available_cutters=[0.01, 0.015, 0.02, 0.03]
            )[0].content[0]
        # Ideal 0.0175 snaps down to 0.015 (< floor 0.0175) -> bumped to 0.02.
        assert math.isclose(line.cutter_diameter, 0.02)
        assert "stepped up" in caplog.text

    def test_multi_stroke_logs_implicit_overlap(self, caplog: pytest.LogCaptureFixture) -> None:
        """The INFO line names the font, stroke count and implicit overlap."""
        with caplog.at_level("INFO"):
            resolve_job_spec(self._job("Avenge5"))
        assert "multi-stroke font 'Avenge5' (S5)" in caplog.text
        assert "implicit pass overlap" in caplog.text

    def test_multi_stroke_boldness_escape(self) -> None:
        """A heavier factor grows the cutter above the design point."""
        line = resolve_job_spec(self._job("Avenge5", target_boldness_factor=0.2))[0].content[0]
        assert math.isclose(line.cutter_diameter, 0.0475, rel_tol=1e-9)

    def test_multi_stroke_thin_target_clamps(self, caplog: pytest.LogCaptureFixture) -> None:
        """A light factor clamps to the gap-free floor with a WARNING."""
        with caplog.at_level("WARNING"):
            line = resolve_job_spec(
                self._job("Avenge5", target_boldness_factor=0.08)
            )[0].content[0]
        assert math.isclose(line.cutter_diameter, 0.0175, rel_tol=1e-9)
        assert "gap-free floor" in caplog.text

    def test_explicit_cutter_size_wins_over_ladder(self) -> None:
        """An explicit cutter_size bypasses the multi-stroke ladder entirely."""
        line = resolve_job_spec(self._job("Avenge5", cutter_size=0.09))[0].content[0]
        assert math.isclose(line.cutter_diameter, 0.09)

    def test_ttf_font_uses_auto_selection(self) -> None:
        """A TTF font is single-stroke: the ladder never fires."""
        line = resolve_job_spec(self._job(DEFAULT_FONT))[0].content[0]
        assert math.isclose(line.cutter_diameter, get_cutter_diameter(0.5))

    def test_multi_stroke_cutter_always_leaves_material(self) -> None:
        """The ladder cutter stays below the text height at every bound.

        With the factor bounded to 0.30 and the pass spacing subtracted, the
        derived cutter is always a fraction of the height, so the
        ``CutterSizeError`` guard on the multi-stroke path is a defensive
        net rather than a reachable abort - pinned so a future widening of
        the schema bounds notices.
        """
        line = resolve_job_spec(
            self._job("Avenge5", target_boldness_factor=0.30)
        )[0].content[0]
        assert line.toolpath_text_height > 0.0

    def test_multi_stroke_cutter_is_frozen_dataclass(self) -> None:
        """The ladder result is an immutable value object."""
        ladder = multi_stroke_cutter_diameter(0.5, 5)
        assert isinstance(ladder, MultiStrokeCutter)
        with pytest.raises(Exception):
            ladder.cutter = 0.0  # type: ignore[misc]


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


class TestEnforceCapLineCascade:
    """enforce_cap_line cascades line -> label -> job -> True (explicit-None)."""

    @staticmethod
    def _job(**levels: bool | None) -> JobSpec:
        """Build a single-line job applying enforce_cap_line at the given levels."""
        job_kwargs: dict[str, bool | None] = {}
        label_kwargs: dict[str, bool | None] = {}
        line_kwargs: dict[str, bool | None] = {}
        if "job" in levels:
            job_kwargs["enforce_cap_line"] = levels["job"]
        if "label" in levels:
            label_kwargs["enforce_cap_line"] = levels["label"]
        if "line" in levels:
            line_kwargs["enforce_cap_line"] = levels["line"]
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
        """The shipped default clamps alphanumeric glyphs to the capline."""
        assert DEFAULT_ENFORCE_CAP_LINE is True

    def test_default_resolves_true(self) -> None:
        """All levels omitting resolves to the True fallback."""
        line = resolve_job_spec(self._job())[0].content[0]
        assert line.enforce_cap_line is True

    def test_job_false_used_when_label_omits(self) -> None:
        """Job-level False cascades to labels and lines that omit the field."""
        line = resolve_job_spec(self._job(job=False))[0].content[0]
        assert line.enforce_cap_line is False

    def test_label_overrides_job(self) -> None:
        """Label-level True beats the job-level False."""
        line = resolve_job_spec(self._job(job=False, label=True))[0].content[0]
        assert line.enforce_cap_line is True

    def test_line_overrides_label(self) -> None:
        """Line-level False beats the label-level True."""
        line = resolve_job_spec(self._job(job=True, label=True, line=False))[0].content[0]
        assert line.enforce_cap_line is False

    def test_resolved_dataclass_default_is_true(self) -> None:
        """Direct test callers constructing ResolvedTextLine get clamping on."""
        assert _make_line().enforce_cap_line is True


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

    def test_band_block_height_excludes_descenders(self) -> None:
        """charge_descenders=False measures the baseline-to-capline band."""
        heights = [0.5, 0.5]
        band = baseline_block_height(
            heights, [0.5, 0.5], [0.0, 0.12], [0.1], charge_descenders=False
        )
        ink = baseline_block_height(heights, [0.5, 0.5], [0.0, 0.12], [0.1])
        # The band stops at the lowest baseline, so it is the descender shorter.
        assert math.isclose(band, 0.5 + 0.1 + 0.5, abs_tol=1e-12)
        assert math.isclose(ink - band, 0.12, abs_tol=1e-12)

    def test_band_block_height_ignores_interior_descenders(self) -> None:
        """The band model is content-independent below the baselines."""
        heights = [0.5, 0.5, 0.5]
        band = baseline_block_height(
            heights, [0.5] * 3, [0.2, 0.0, 0.3], [0.4, 0.4], charge_descenders=False
        )
        assert math.isclose(band, 0.5 + 0.4 + 0.5 + 0.4 + 0.5, abs_tol=1e-12)


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

    def test_band_mode_does_not_charge_descenders(self) -> None:
        """charge_descenders=False fits against the cap band, keeping spacing.

        The same label that loses spacing when descenders are charged keeps
        its full spacing when the block is measured as its baseline-to-capline
        band -- the descender hangs into the bottom margin instead. The band
        spans 1.3in here and the descender-extended ink 1.5in, so an inner
        height of 1.4in sits between the two.
        """
        heights, asc, desc = [0.5, 0.5], [0.5, 0.5], [0.0, 0.2]
        charged = fit_baseline_spacing_to_margins(heights, asc, desc, [0.3], 1.4)
        band = fit_baseline_spacing_to_margins(
            heights, asc, desc, [0.3], 1.4, charge_descenders=False
        )
        assert band == [0.3]
        assert charged[0] < 0.3

    def test_band_mode_still_clamps_a_band_overflow(self) -> None:
        """The band model clamps spacing when the band itself overflows."""
        adjusted = fit_baseline_spacing_to_margins(
            [0.5, 0.5], [0.5, 0.5], [0.0, 0.0], [0.9], 1.05, charge_descenders=False
        )
        assert adjusted[0] < 0.9

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
        assert resolve_job_spec(self._job(job=False, label=True))[0].use_baseline_spacing is True

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


class TestVCenterByContentCascade:
    """v_center_by_content cascades label -> job -> False (explicit-None)."""

    @staticmethod
    def _job(**levels: bool | None) -> JobSpec:
        """Build a one-label job applying v_center_by_content at the given levels."""
        job_kwargs: dict[str, bool | None] = {}
        label_kwargs: dict[str, bool | None] = {}
        if "job" in levels:
            job_kwargs["v_center_by_content"] = levels["job"]
        if "label" in levels:
            label_kwargs["v_center_by_content"] = levels["label"]
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

    def test_default_module_constant_is_false(self) -> None:
        """The shipped default centers by band (ink centering is the opt-in)."""
        assert DEFAULT_V_CENTER_BY_CONTENT is False

    def test_default_resolves_false(self) -> None:
        """All levels omitting resolves to the False fallback."""
        assert resolve_job_spec(self._job())[0].v_center_by_content is False

    def test_job_true_used_when_label_omits(self) -> None:
        """Job-level True cascades onto labels that omit the field."""
        assert resolve_job_spec(self._job(job=True))[0].v_center_by_content is True

    def test_label_overrides_job(self) -> None:
        """Label-level False beats the job-level True."""
        assert resolve_job_spec(self._job(job=True, label=False))[0].v_center_by_content is False

    def test_label_true_is_honored(self) -> None:
        """An intentional True (ink centering) never falls through."""
        assert resolve_job_spec(self._job(job=False, label=True))[0].v_center_by_content is True

    def test_resolved_label_defaults_false(self) -> None:
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
        assert label.v_center_by_content is DEFAULT_V_CENTER_BY_CONTENT


class TestCenterTextByBandGate:
    """Band centering needs the descending-character relief enabled."""

    @staticmethod
    def _label(**kwargs: object) -> ResolvedLabel:
        base = ResolvedLabel(
            id="l",
            count=1,
            width=2.0,
            height=1.0,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
        )
        return replace(base, **kwargs)

    def test_band_centering_is_the_default(self) -> None:
        """Relief on + ink-centering off centers by band."""
        assert self._label().center_text_by_band is True

    def test_ink_centering_opts_out(self) -> None:
        """v_center_by_content=True restores the historical ink centering."""
        assert self._label(v_center_by_content=True).center_text_by_band is False

    def test_relief_off_forces_ink_centering(self) -> None:
        """With the relief off nothing adjusts descenders, so ink centers."""
        label = self._label(v_center_by_content=False, auto_descending_character_adjustment=False)
        assert label.center_text_by_band is False

    def test_param_inert_when_relief_off(self) -> None:
        """The flag is parity-only while the relief is disabled."""
        off = self._label(auto_descending_character_adjustment=False)
        assert off.center_text_by_band is False
        assert (
            self._label(
                v_center_by_content=True, auto_descending_character_adjustment=False
            ).center_text_by_band
            is False
        )


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
        label = resolve_job_spec(self._job(v_margin=0.3), extents_probe=self._probe)[0]
        heights = [line.toolpath_text_height for line in label.content]
        asc = [h for h in heights]
        desc = [0.20, 0.0]
        block = baseline_block_height(heights, asc, desc, [label.content[0].line_spacing])
        assert math.isclose(block, 2.0 - 2 * 0.3, abs_tol=1e-6)

    def test_margin_fit_clamps_explicit_spacing(self) -> None:
        """Explicit spacing is clamped against the descender-extended block."""
        label = resolve_job_spec(self._job(line_spacing=1.0), extents_probe=self._probe)[0]
        plain = resolve_job_spec(self._job(line_spacing=1.0))[0]
        # Inner height 2.0 - 2 * 0.2 = 1.6. The probe run stacks on cap heights
        # (0.44): the first line's descender hangs into the gap, so the block
        # is A0 + pitch = 0.44 + (0.44 + s) = 1.6 -> s = 0.72. The probe-less
        # run divides the nominal heights (0.5): 0.5 + (0.5 + s) = 1.6 -> 0.60.
        assert math.isclose(label.content[0].line_spacing, 0.72, abs_tol=1e-6)
        assert math.isclose(plain.content[0].line_spacing, 0.6, abs_tol=1e-6)


class TestDescendingCharacterCascade:
    """The four relief fields cascade label -> job -> default (explicit-None)."""

    @staticmethod
    def _job(**levels: object) -> JobSpec:
        """Build a one-label job applying the relief fields at the given levels."""
        job_kwargs: dict[str, object] = {}
        label_kwargs: dict[str, object] = {}
        for key, value in levels.items():
            tier, _, field = key.partition("__")
            target = job_kwargs if tier == "job" else label_kwargs
            target[field] = value
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

    def test_default_module_constants(self) -> None:
        """The shipped defaults: relief on, 0.5 scale, 0.7 raise, 0.0 clearance."""
        assert DEFAULT_AUTO_DESCENDING_CHARACTER_ADJUSTMENT is True
        assert DEFAULT_DESCENDING_CHARACTER_V_SCALE == 0.5
        assert DEFAULT_DESCENDING_CHARACTER_RAISE == 0.7
        assert DEFAULT_DESCENDING_CHARACTER_CLEARANCE == 0.0
        assert DEFAULT_DESCENDING_CHARACTER_RECIPROCAL_SCALE == 0.5

    def test_defaults_resolve(self) -> None:
        """All levels omitting resolves to the shipped fallbacks."""
        label = resolve_job_spec(self._job())[0]
        assert label.auto_descending_character_adjustment is True
        assert label.descending_character_v_scale == 0.5
        assert label.descending_character_raise == 0.7
        assert label.descending_character_clearance == 0.0
        assert label.descending_character_reciprocal_scale == 0.5

    def test_job_values_used_when_label_omits(self) -> None:
        """Job-level values cascade onto labels that omit the fields."""
        label = resolve_job_spec(
            self._job(
                job__auto_descending_character_adjustment=False,
                job__descending_character_v_scale=0.3,
                job__descending_character_raise=0.25,
                job__descending_character_clearance=0.05,
                job__descending_character_reciprocal_scale=0.8,
            )
        )[0]
        assert label.auto_descending_character_adjustment is False
        assert label.descending_character_v_scale == pytest.approx(0.3)
        assert label.descending_character_raise == pytest.approx(0.25)
        assert label.descending_character_clearance == pytest.approx(0.05)
        assert label.descending_character_reciprocal_scale == pytest.approx(0.8)

    def test_label_overrides_job(self) -> None:
        """Label-level values beat the job-level ones."""
        label = resolve_job_spec(
            self._job(
                job__auto_descending_character_adjustment=False,
                job__descending_character_v_scale=0.3,
                job__descending_character_reciprocal_scale=0.3,
                label__auto_descending_character_adjustment=True,
                label__descending_character_v_scale=0.9,
                label__descending_character_reciprocal_scale=1.0,
            )
        )[0]
        assert label.auto_descending_character_adjustment is True
        assert label.descending_character_v_scale == pytest.approx(0.9)
        # The reciprocal opt-out (1.0) is a real value, not an unset fallthrough.
        assert label.descending_character_reciprocal_scale == 1.0

    def test_label_false_is_honored(self) -> None:
        """An intentional False (descenders engrave verbatim) never falls through."""
        label = resolve_job_spec(
            self._job(
                job__auto_descending_character_adjustment=True,
                label__auto_descending_character_adjustment=False,
            )
        )[0]
        assert label.auto_descending_character_adjustment is False

    def test_explicit_zero_allowances_are_honored(self) -> None:
        """Only ``None`` means unset: a 0.0 raise/clearance is a real value."""
        label = resolve_job_spec(
            self._job(
                job__descending_character_raise=0.7,
                job__descending_character_clearance=0.05,
                label__descending_character_raise=0.0,
                label__descending_character_clearance=0.0,
            )
        )[0]
        assert label.descending_character_raise == 0.0
        assert label.descending_character_clearance == 0.0

    def test_root_level_job_resolves_own_values(self) -> None:
        """A root-level job masquerading as a label resolves its own fields."""
        job = JobSpec(
            job_name="J",
            width=2.0,
            height=1.0,
            text_height=0.5,
            content=[TextLine(text="X")],
            auto_descending_character_adjustment=False,
            descending_character_v_scale=0.6,
        )
        label = resolve_job_spec(job)[0]
        assert label.auto_descending_character_adjustment is False
        assert label.descending_character_v_scale == pytest.approx(0.6)

    def test_resolved_label_defaults(self) -> None:
        """A manually constructed ResolvedLabel defaults to the shipped values."""
        label = ResolvedLabel(
            id="l",
            count=1,
            width=2.0,
            height=1.0,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
        )
        assert label.auto_descending_character_adjustment is (
            DEFAULT_AUTO_DESCENDING_CHARACTER_ADJUSTMENT
        )
        assert label.descending_character_v_scale == DEFAULT_DESCENDING_CHARACTER_V_SCALE
        assert label.descending_character_raise == DEFAULT_DESCENDING_CHARACTER_RAISE
        assert label.descending_character_clearance == DEFAULT_DESCENDING_CHARACTER_CLEARANCE
        assert (
            label.descending_character_reciprocal_scale
            == DEFAULT_DESCENDING_CHARACTER_RECIPROCAL_SCALE
        )


class TestHtmlBracketsCascade:
    """html_brackets must cascade line -> label -> job -> default."""

    def test_default_when_all_omit(self) -> None:
        """All levels omitting yields the '<>' default."""
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
        line = resolve_job_spec(job)[0].content[0]
        assert line.html_brackets == DEFAULT_HTML_BRACKETS
        assert DEFAULT_HTML_BRACKETS == "<>"

    def test_job_value_used_when_label_omits(self) -> None:
        """Job-level value cascades to the line."""
        job = JobSpec(
            job_name="J",
            html_brackets="\u00ab\u00bb",
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X")],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].html_brackets == "\u00ab\u00bb"

    def test_label_overrides_job(self) -> None:
        """Label-level value overrides the job-level value."""
        job = JobSpec(
            job_name="J",
            html_brackets="\u00ab\u00bb",
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    html_brackets="<<>>",
                    content=[TextLine(text="X")],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].html_brackets == "<<>>"

    def test_line_overrides_label_and_job(self) -> None:
        """Line-level value wins over both parents."""
        job = JobSpec(
            job_name="J",
            html_brackets="\u00ab\u00bb",
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    html_brackets="<<>>",
                    content=[TextLine(text="X", html_brackets="[[]]")],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].html_brackets == "[[]]"

    def test_brackets_drive_the_parse(self) -> None:
        """The line's own resolved pair parses its markup."""
        job = JobSpec(
            job_name="J",
            html_brackets="\u00ab\u00bb",
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="\u00abi\u00bbslanted\u00ab/i\u00bb")],
                )
            ],
        )
        line = resolve_job_spec(job)[0].content[0]
        assert line.plain_text == "slanted"
        assert [r.style.italic for r in line.styled_runs] == [True]

    def test_chevron_data_survives_a_custom_pair(self) -> None:
        """The motivating case: chevrons engrave verbatim under \u00ab\u00bb."""
        job = JobSpec(
            job_name="J",
            html_brackets="\u00ab\u00bb",
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="<i>not markup</i>")],
                )
            ],
        )
        line = resolve_job_spec(job)[0].content[0]
        assert line.plain_text == "<i>not markup</i>"
        assert line.styled_runs[0].style.italic is False


class TestItalicAngleCascade:
    """italic_angle must cascade line -> label -> job -> default."""

    def test_default_when_all_omit(self) -> None:
        """All levels omitting yields the 12-degree default."""
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
        line = resolve_job_spec(job)[0].content[0]
        assert math.isclose(line.italic_angle, DEFAULT_ITALIC_ANGLE)
        assert DEFAULT_ITALIC_ANGLE == 12.0

    def test_explicit_zero_is_honored(self) -> None:
        """An intentional 0.0 (upright) is not treated as unset."""
        job = JobSpec(
            job_name="J",
            italic_angle=30.0,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X", italic_angle=0.0)],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].italic_angle == 0.0

    def test_job_value_used_when_label_omits(self) -> None:
        """Job-level value cascades to the line."""
        job = JobSpec(
            job_name="J",
            italic_angle=20.0,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X")],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].italic_angle == 20.0

    def test_label_overrides_job(self) -> None:
        """Label-level value overrides the job-level value."""
        job = JobSpec(
            job_name="J",
            italic_angle=20.0,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    italic_angle=30.0,
                    content=[TextLine(text="X")],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].italic_angle == 30.0

    def test_angle_bounds_are_enforced(self) -> None:
        """The schema rejects an angle outside [0, 45]."""
        with pytest.raises(ValidationError):
            TextLine(text="X", italic_angle=-1.0)
        with pytest.raises(ValidationError):
            TextLine(text="X", italic_angle=46.0)


class TestMarkupResolution:
    """Resolution parses each line's markup with its resolved delimiters."""

    @staticmethod
    def _job(*lines: TextLine, **job_kwargs: object) -> JobSpec:
        """Build a single-label job over ``lines``."""
        return JobSpec(
            job_name="J",
            labels=[LabelSpec(id="lbl", width=2.0, height=1.0, content=list(lines))],
            **job_kwargs,
        )

    def test_markup_resolves_to_plain_text_and_runs(self) -> None:
        """The raw text keeps the tags; plain_text and runs carry the result."""
        job = self._job(TextLine(text="Hello <i>World</i>!"))
        line = resolve_job_spec(job)[0].content[0]
        assert line.text == "Hello <i>World</i>!"
        assert line.plain_text == "Hello World!"
        assert [(r.text, r.style.italic) for r in line.styled_runs] == [
            ("Hello ", False),
            ("World", True),
            ("!", False),
        ]

    def test_markup_free_line_keeps_the_fast_path(self) -> None:
        """A tag-free line resolves to one plain run and plain_text == text."""
        line = resolve_job_spec(self._job(TextLine(text="plain")))[0].content[0]
        assert line.plain_text == "plain"
        assert line.styled_runs == (line.styled_runs[0],)
        assert line.styled_runs[0].style.italic is False

    def test_unclosed_tag_aborts_resolution(self) -> None:
        """Malformed markup is a loud ValueError at resolution time."""
        from plt_optimizer.generate.markup import MarkupError

        with pytest.raises(MarkupError, match="Unclosed tag"):
            resolve_job_spec(self._job(TextLine(text="<i>oops")))

    def test_crossing_spans_resolve_to_intersected_runs(self) -> None:
        """A crossing close is legal: runs carry the per-character intersection."""
        job = self._job(TextLine(text="<s>Always <sc>on</s> duty</sc>"))
        line = resolve_job_spec(job)[0].content[0]
        assert line.plain_text == "Always on duty"
        assert line.strikethrough_flags == (True,) * 9 + (False,) * 5
        assert line.small_caps_flags == (False,) * 7 + (True,) * 7

    def test_close_of_a_never_opened_tag_aborts_resolution(self) -> None:
        """A close whose name was never opened stays a hard error."""
        from plt_optimizer.generate.markup import MarkupError

        with pytest.raises(MarkupError, match="never opened"):
            resolve_job_spec(self._job(TextLine(text="<i>x</b>")))

    def test_direct_construction_defaults_plain_text_to_text(self) -> None:
        """A hand-built line (no parse) treats its text as markup-free."""
        line = ResolvedTextLine(
            text="3 < 5",
            nominal_text_height=0.5,
            toolpath_text_height=0.485,
            cutter_diameter=0.015,
            character_spacing=0.0,
            line_spacing=0.0,
        )
        assert line.plain_text == "3 < 5"
        assert line.styled_runs == ()

    def test_with_markup_reparses_the_new_text(self) -> None:
        """with_markup keeps plain_text/runs in sync with a text swap."""
        line = resolve_job_spec(self._job(TextLine(text="plain")))[0].content[0]
        updated = line.with_markup("a<i>b</i>")
        assert updated.text == "a<i>b</i>"
        assert updated.plain_text == "ab"
        assert [r.style.italic for r in updated.styled_runs] == [False, True]

    def test_with_markup_is_identity_for_unchanged_text(self) -> None:
        """Swapping in the same text returns the same object."""
        line = resolve_job_spec(self._job(TextLine(text="same")))[0].content[0]
        assert line.with_markup("same") is line

    def test_with_markup_reparses_with_the_line_pair(self) -> None:
        """The re-parse honors the line's own resolved delimiters."""
        job = JobSpec(
            job_name="J",
            html_brackets="\u00ab\u00bb",
            labels=[LabelSpec(id="lbl", width=2.0, height=1.0, content=[TextLine(text="x")])],
        )
        line = resolve_job_spec(job)[0].content[0]
        updated = line.with_markup("\u00abi\u00bbx\u00ab/i\u00bb")
        assert updated.plain_text == "x"
        assert [r.style.italic for r in updated.styled_runs] == [True]


class TestUnderlineOffsetCascade:
    """underline_offset must cascade line -> label -> job -> default."""

    def test_default_when_all_omit(self) -> None:
        """All levels omitting yields the 0.0 default."""
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
        line = resolve_job_spec(job)[0].content[0]
        assert math.isclose(line.underline_offset, DEFAULT_UNDERLINE_OFFSET)
        assert DEFAULT_UNDERLINE_OFFSET == 0.0

    def test_explicit_zero_is_honored(self) -> None:
        """An intentional 0.0 (strokes touch) is not treated as unset."""
        job = JobSpec(
            job_name="J",
            underline_offset=0.25,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X", underline_offset=0.0)],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].underline_offset == 0.0

    def test_job_value_used_when_label_omits(self) -> None:
        """Job-level value cascades to the line."""
        job = JobSpec(
            job_name="J",
            underline_offset=0.2,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X")],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].underline_offset == 0.2

    def test_label_overrides_job(self) -> None:
        """Label-level value overrides the job-level value."""
        job = JobSpec(
            job_name="J",
            underline_offset=0.2,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    underline_offset=0.3,
                    content=[TextLine(text="X")],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].underline_offset == 0.3

    def test_line_overrides_label(self) -> None:
        """Text-line value wins over label and job."""
        job = JobSpec(
            job_name="J",
            underline_offset=0.2,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    underline_offset=0.3,
                    content=[TextLine(text="X", underline_offset=0.05)],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].underline_offset == 0.05

    def test_negative_is_a_validation_error(self) -> None:
        """The schema rejects a negative offset."""
        with pytest.raises(ValidationError):
            TextLine(text="X", underline_offset=-0.1)


class TestUnderlineBreakClearanceCascade:
    """underline_break_clearance must cascade line -> label -> job -> default."""

    def test_default_when_all_omit(self) -> None:
        """All levels omitting yields the 0.0 default."""
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
        line = resolve_job_spec(job)[0].content[0]
        assert math.isclose(line.underline_break_clearance, DEFAULT_UNDERLINE_BREAK_CLEARANCE)
        assert DEFAULT_UNDERLINE_BREAK_CLEARANCE == 0.0

    def test_explicit_zero_is_honored(self) -> None:
        """An intentional 0.0 (tangency) is not treated as unset."""
        job = JobSpec(
            job_name="J",
            underline_break_clearance=0.25,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X", underline_break_clearance=0.0)],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].underline_break_clearance == 0.0

    def test_job_value_used_when_label_omits(self) -> None:
        """Job-level value cascades to the line."""
        job = JobSpec(
            job_name="J",
            underline_break_clearance=0.2,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X")],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].underline_break_clearance == 0.2

    def test_label_overrides_job(self) -> None:
        """Label-level value wins over job."""
        job = JobSpec(
            job_name="J",
            underline_break_clearance=0.2,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    underline_break_clearance=0.3,
                    content=[TextLine(text="X")],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].underline_break_clearance == 0.3

    def test_line_overrides_label(self) -> None:
        """Text-line value wins over label and job."""
        job = JobSpec(
            job_name="J",
            underline_break_clearance=0.2,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    underline_break_clearance=0.3,
                    content=[TextLine(text="X", underline_break_clearance=0.05)],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].underline_break_clearance == 0.05

    def test_negative_is_a_validation_error(self) -> None:
        """The schema rejects a negative clearance."""
        with pytest.raises(ValidationError):
            TextLine(text="X", underline_break_clearance=-0.1)


class TestUnderlineBreakSpacesCascade:
    """underline_break_spaces must cascade line -> label -> job -> default."""

    def test_default_when_all_omit(self) -> None:
        """All levels omitting yields the False default."""
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
        line = resolve_job_spec(job)[0].content[0]
        assert line.underline_break_spaces is DEFAULT_UNDERLINE_BREAK_SPACES
        assert DEFAULT_UNDERLINE_BREAK_SPACES is False

    def test_explicit_false_is_honored(self) -> None:
        """An intentional False (continuous underline) is not treated as unset."""
        job = JobSpec(
            job_name="J",
            underline_break_spaces=True,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X", underline_break_spaces=False)],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].underline_break_spaces is False

    def test_job_value_used_when_label_omits(self) -> None:
        """Job-level value cascades to the line."""
        job = JobSpec(
            job_name="J",
            underline_break_spaces=True,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X")],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].underline_break_spaces is True

    def test_label_overrides_job(self) -> None:
        """Label-level value wins over job."""
        job = JobSpec(
            job_name="J",
            underline_break_spaces=False,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    underline_break_spaces=True,
                    content=[TextLine(text="X")],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].underline_break_spaces is True

    def test_line_overrides_label(self) -> None:
        """Text-line value wins over label and job."""
        job = JobSpec(
            job_name="J",
            underline_break_spaces=True,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    underline_break_spaces=True,
                    content=[TextLine(text="X", underline_break_spaces=False)],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].underline_break_spaces is False


class TestStrikethroughOffsetCascade:
    """strikethrough_offset must cascade line -> label -> job -> default (None)."""

    def test_default_when_all_omit(self) -> None:
        """All levels omitting yields the None default (mid-cap centering)."""
        job = JobSpec(
            job_name="J",
            labels=[
                LabelSpec(id="lbl", width=2.0, height=1.0, content=[TextLine(text="X")])
            ],
        )
        line = resolve_job_spec(job)[0].content[0]
        assert line.strikethrough_offset is DEFAULT_STRIKETHROUGH_OFFSET
        assert DEFAULT_STRIKETHROUGH_OFFSET is None

    def test_explicit_zero_is_honored(self) -> None:
        """An intentional 0.0 (stroke on the baseline) is not treated as unset."""
        job = JobSpec(
            job_name="J",
            strikethrough_offset=0.2,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X", strikethrough_offset=0.0)],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].strikethrough_offset == 0.0

    def test_job_value_used_when_label_omits(self) -> None:
        """Job-level value cascades to the line."""
        job = JobSpec(
            job_name="J",
            strikethrough_offset=0.2,
            labels=[
                LabelSpec(id="lbl", width=2.0, height=1.0, content=[TextLine(text="X")])
            ],
        )
        assert resolve_job_spec(job)[0].content[0].strikethrough_offset == 0.2

    def test_label_overrides_job(self) -> None:
        """Label-level value wins over job."""
        job = JobSpec(
            job_name="J",
            strikethrough_offset=0.2,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    strikethrough_offset=0.3,
                    content=[TextLine(text="X")],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].strikethrough_offset == 0.3

    def test_line_overrides_label(self) -> None:
        """Text-line value wins over label and job."""
        job = JobSpec(
            job_name="J",
            strikethrough_offset=0.2,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    strikethrough_offset=0.3,
                    content=[TextLine(text="X", strikethrough_offset=0.4)],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].strikethrough_offset == 0.4


class TestStrikethroughCapRule:
    """``ResolvedTextLine.strikethrough_cap`` is the single Y rule."""

    def test_unset_is_half_the_cap_height(self) -> None:
        """Unset offset centres the stroke on the cap-band midline."""
        line = ResolvedTextLine(
            text="X",
            nominal_text_height=0.5,
            toolpath_text_height=0.44,
            cutter_diameter=0.06,
            character_spacing=0.0,
            line_spacing=0.0,
        )
        assert line.strikethrough_offset is None
        assert math.isclose(line.strikethrough_cap, 0.22)

    def test_explicit_offset_is_used_verbatim(self) -> None:
        """An explicit offset bypasses the midline (0.0 included)."""
        line = ResolvedTextLine(
            text="X",
            nominal_text_height=0.5,
            toolpath_text_height=0.44,
            cutter_diameter=0.06,
            character_spacing=0.0,
            line_spacing=0.0,
            strikethrough_offset=0.0,
        )
        assert line.strikethrough_cap == 0.0

    def test_has_strikethrough_reads_the_runs(self) -> None:
        """The gate's fast-path flag sees a struck run and nothing else."""
        plain = ResolvedTextLine(
            text="X",
            nominal_text_height=0.5,
            toolpath_text_height=0.44,
            cutter_diameter=0.06,
            character_spacing=0.0,
            line_spacing=0.0,
        )
        assert plain.has_strikethrough is False
        struck = plain.with_markup("<s>X</s>")
        assert struck.has_strikethrough is True
        assert struck.strikethrough_flags == (True,)


class TestStrikethroughBreakSpacesCascade:
    """strikethrough_break_spaces must cascade line -> label -> job -> default."""

    def test_default_when_all_omit(self) -> None:
        """All levels omitting yields the False default."""
        job = JobSpec(
            job_name="J",
            labels=[
                LabelSpec(id="lbl", width=2.0, height=1.0, content=[TextLine(text="X")])
            ],
        )
        line = resolve_job_spec(job)[0].content[0]
        assert line.strikethrough_break_spaces is DEFAULT_STRIKETHROUGH_BREAK_SPACES
        assert DEFAULT_STRIKETHROUGH_BREAK_SPACES is False

    def test_explicit_false_is_honored(self) -> None:
        """An intentional False (continuous stroke) is not treated as unset."""
        job = JobSpec(
            job_name="J",
            strikethrough_break_spaces=True,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X", strikethrough_break_spaces=False)],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].strikethrough_break_spaces is False

    def test_job_value_used_when_label_omits(self) -> None:
        """Job-level value cascades to the line."""
        job = JobSpec(
            job_name="J",
            strikethrough_break_spaces=True,
            labels=[
                LabelSpec(id="lbl", width=2.0, height=1.0, content=[TextLine(text="X")])
            ],
        )
        assert resolve_job_spec(job)[0].content[0].strikethrough_break_spaces is True

    def test_label_overrides_job(self) -> None:
        """Label-level value wins over job."""
        job = JobSpec(
            job_name="J",
            strikethrough_break_spaces=False,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    strikethrough_break_spaces=True,
                    content=[TextLine(text="X")],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].strikethrough_break_spaces is True

    def test_line_overrides_label(self) -> None:
        """Text-line value wins over label and job."""
        job = JobSpec(
            job_name="J",
            strikethrough_break_spaces=True,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    strikethrough_break_spaces=True,
                    content=[TextLine(text="X", strikethrough_break_spaces=False)],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].strikethrough_break_spaces is False


_INVENTORY = [0.01, 0.015, 0.02, 0.03, 0.045, 0.06, 0.09, 0.125, 0.25]


class TestBoldBoldnessMultiplierCascade:
    """bold_boldness_multiplier must cascade line -> label -> job -> default."""

    def test_default_when_all_omit(self) -> None:
        """All levels omitting yields the 1.5 default."""
        job = JobSpec(
            job_name="J",
            labels=[
                LabelSpec(id="lbl", width=2.0, height=1.0, content=[TextLine(text="X")])
            ],
        )
        line = resolve_job_spec(job)[0].content[0]
        assert math.isclose(line.bold_boldness_multiplier, DEFAULT_BOLD_BOLDNESS_MULTIPLIER)
        assert DEFAULT_BOLD_BOLDNESS_MULTIPLIER == 1.5

    def test_explicit_one_is_honored(self) -> None:
        """An intentional 1.0 (no boost) is not treated as unset."""
        job = JobSpec(
            job_name="J",
            bold_boldness_multiplier=3.0,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="X", bold_boldness_multiplier=1.0)],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].bold_boldness_multiplier == 1.0

    def test_job_value_used_when_label_omits(self) -> None:
        """Job-level value cascades to the line."""
        job = JobSpec(
            job_name="J",
            bold_boldness_multiplier=2.5,
            labels=[
                LabelSpec(id="lbl", width=2.0, height=1.0, content=[TextLine(text="X")])
            ],
        )
        assert resolve_job_spec(job)[0].content[0].bold_boldness_multiplier == 2.5

    def test_label_overrides_job(self) -> None:
        """Label-level value overrides the job-level value."""
        job = JobSpec(
            job_name="J",
            bold_boldness_multiplier=2.0,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=2.0,
                    height=1.0,
                    bold_boldness_multiplier=3.0,
                    content=[TextLine(text="X")],
                )
            ],
        )
        assert resolve_job_spec(job)[0].content[0].bold_boldness_multiplier == 3.0


class TestBoldCutterResolution:
    """A ``<b>`` run resolves a thicker cutter; the plain run keeps its own."""

    @staticmethod
    def _job(*lines: TextLine, **job_kwargs: object) -> JobSpec:
        """Build a single-label job over ``lines`` (0.5in text height)."""
        job_kwargs.setdefault("text_height", 0.5)
        return JobSpec(
            job_name="J",
            width=3.0,
            height=1.0,
            labels=[LabelSpec(id="lbl", width=3.0, height=1.0, content=list(lines))],
            **job_kwargs,
        )

    def _line(self, *lines: TextLine, **job_kwargs: object) -> ResolvedTextLine:
        """Resolve the job's first line against the test inventory."""
        return resolve_job_spec(
            self._job(*lines, **job_kwargs), available_cutters=_INVENTORY
        )[0].content[0]

    def test_no_markup_has_no_bold_cutter(self) -> None:
        """A markup-free line carries no bold cutter."""
        line = self._line(TextLine(text="PLAIN", font="Dino"))
        assert line.bold_cutter_diameter is None
        assert line.bold_toolpath_text_height is None
        assert line.has_bold is False

    def test_single_stroke_steps_one_inventory_rung(self) -> None:
        """Dino auto 0.060 -> bold 0.090 (the next larger rung)."""
        line = self._line(TextLine(text="a<b>B</b>", font="Dino"))
        assert math.isclose(line.cutter_diameter, 0.06)
        assert math.isclose(line.bold_cutter_diameter, 0.09)
        assert math.isclose(line.bold_toolpath_text_height, 0.5 - 0.09)

    def test_nominal_height_untouched(self) -> None:
        """The bold cutter never moves the nominal height (vertical fit)."""
        line = self._line(TextLine(text="a<b>B</b>", font="Dino"))
        assert math.isclose(line.nominal_text_height, 0.5)
        assert math.isclose(line.toolpath_text_height, 0.5 - 0.06)

    def test_bold_glyph_scale_is_the_centerline_ratio(self) -> None:
        """The uniform scale is (nominal - bold) / (nominal - plain), <= 1."""
        line = self._line(TextLine(text="a<b>B</b>", font="Dino"))
        expected = (0.5 - 0.09) / (0.5 - 0.06)
        assert line._bold_glyph_scale() == pytest.approx(expected)
        assert line._bold_glyph_scale() <= 1.0

    def test_bold_run_raises_by_the_half_decrease(self) -> None:
        """The engraved-baseline raise is (bold - plain) / 2 on the bold span."""
        line = self._line(TextLine(text="a<b>B</b>d", font="Dino"))
        assert math.isclose(line.cutter_diameter, 0.06)
        assert math.isclose(line.bold_cutter_diameter, 0.09)
        # (0.09 - 0.06) / 2 = 0.015in on the bold char, 0.0 on the plain ones.
        # This raise aligns the bold run's ENGRAVED baseline AND capline with
        # the plain run's (both alignments reduce to the same half-decrease).
        assert line.baseline_raises == (0.0, 0.015, 0.0)

    def test_no_markup_has_no_raises(self) -> None:
        """A markup-free line raises nothing (all-zero: the renderers' fast path)."""
        line = self._line(TextLine(text="PLAIN", font="Dino"))
        assert line.baseline_raises == (0.0,) * len(line.plain_text)
        # A hand-built line with no styled runs has no per-character array at all.
        bare = ResolvedTextLine(
            text="X",
            nominal_text_height=0.5,
            toolpath_text_height=0.44,
            cutter_diameter=0.06,
            character_spacing=0.0,
            line_spacing=0.0,
        )
        assert bare.baseline_raises == ()

    def test_top_of_inventory_warns_and_keeps_plain(self, caplog: pytest.LogCaptureFixture) -> None:
        """At the largest tool there is no larger rung: bold stays plain (WARNING)."""
        job = JobSpec(
            job_name="J",
            width=3.0,
            height=2.6,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=3.0,
                    height=2.6,
                    content=[TextLine(text="<b>B</b>", font="Dino", text_height=2.0)],
                )
            ],
        )
        with caplog.at_level("WARNING"):
            line = resolve_job_spec(job, available_cutters=_INVENTORY)[0].content[0]
        assert math.isclose(line.cutter_diameter, 0.25)
        assert line.bold_cutter_diameter is None
        assert "next larger cutter" in caplog.text

    def test_explicit_cutter_makes_bold_inert(self, caplog: pytest.LogCaptureFixture) -> None:
        """An explicit cutter_size is never overridden by ``<b>`` (WARNING)."""
        with caplog.at_level("WARNING"):
            line = self._line(TextLine(text="a<b>B</b>", font="Dino", cutter_size=0.09))
        assert math.isclose(line.cutter_diameter, 0.09)
        assert line.bold_cutter_diameter is None
        assert "inert on a line with an explicit cutter_size" in caplog.text

    def test_multistroke_uses_boosted_ladder(self) -> None:
        """Avenge5 (S5): plain ladder 0.020, boosted (0.14*1.5) ladder 0.045."""
        line = self._line(TextLine(text="a<b>b</b>", font="Avenge5"))
        assert math.isclose(line.cutter_diameter, 0.02)
        assert math.isclose(line.bold_cutter_diameter, 0.045)

    def test_multistroke_clamps_to_the_ceiling(self, caplog: pytest.LogCaptureFixture) -> None:
        """A boosted factor above 0.30 clamps to the ceiling (WARNING)."""
        with caplog.at_level("WARNING"):
            line = self._line(
                TextLine(text="a<b>b</b>", font="Avenge5"),
                bold_boldness_multiplier=4.0,
            )
        assert line.bold_cutter_diameter is not None
        assert line.bold_cutter_diameter > line.cutter_diameter
        assert "clamped" in caplog.text
        assert MAX_TARGET_BOLDNESS_FACTOR == 0.30

    def test_multistroke_no_rung_above_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """multiplier 1.0 resolves no rung above the plain cutter -> None (WARNING)."""
        with caplog.at_level("WARNING"):
            line = self._line(
                TextLine(text="a<b>b</b>", font="Avenge5"),
                bold_boldness_multiplier=1.0,
            )
        assert line.bold_cutter_diameter is None
        assert "not above the line cutter" in caplog.text

    def test_bold_flags_align_with_plain_text(self) -> None:
        """The per-character bold flags are aligned with plain_text."""
        line = self._line(TextLine(text="a<b>bc</b>d", font="Dino"))
        assert line.plain_text == "abcd"
        assert line.bold_flags == (False, True, True, False)
        assert line.has_bold is True

    def test_pen_map_includes_the_bold_rung(self) -> None:
        """build_cutter_pen_map keys the bold cutter as its own layer."""
        labels = resolve_job_spec(
            self._job(TextLine(text="a<b>B</b>", font="Dino")),
            available_cutters=_INVENTORY,
        )
        pen_map = build_cutter_pen_map(labels)
        assert (0.06, "none") in pen_map
        assert (0.09, "none") in pen_map

    def test_bold_glyph_scale_defaults_without_bold(self) -> None:
        """A hand-built line with no bold cutter scales at 1.0."""
        line = ResolvedTextLine(
            text="X",
            nominal_text_height=0.5,
            toolpath_text_height=0.44,
            cutter_diameter=0.06,
            character_spacing=0.0,
            line_spacing=0.0,
        )
        assert line._bold_glyph_scale() == 1.0
        assert line.bold_flags == ()
        assert line.has_bold is False

    def test_bold_glyph_scale_guards_no_material(self) -> None:
        """A bold cutter leaving no material falls back to scale 1.0."""
        line = ResolvedTextLine(
            text="X",
            nominal_text_height=0.5,
            toolpath_text_height=0.44,
            cutter_diameter=0.06,
            character_spacing=0.0,
            line_spacing=0.0,
            bold_cutter_diameter=0.5,
        )
        assert line._bold_glyph_scale() == 1.0


class TestScaleResolution:
    """``<scale:N>`` resolves the per-character factors and a material-clearing cutter."""

    @staticmethod
    def _job(*lines: TextLine, **job_kwargs: object) -> JobSpec:
        """Build a single-label job over ``lines``."""
        job_kwargs.setdefault("text_height", 0.5)
        return JobSpec(
            job_name="J",
            width=3.0,
            height=1.0,
            labels=[LabelSpec(id="lbl", width=3.0, height=1.0, content=list(lines))],
            **job_kwargs,
        )

    def _line(self, *lines: TextLine, **job_kwargs: object) -> ResolvedTextLine:
        """Resolve the job's first line against the test inventory."""
        return resolve_job_spec(
            self._job(*lines, **job_kwargs), available_cutters=_INVENTORY
        )[0].content[0]

    def test_no_markup_has_no_scale_values(self) -> None:
        """A markup-free line has no scale run (all-unity factors, fast path)."""
        line = self._line(TextLine(text="PLAIN", font="Dino"))
        assert line.has_scale is False
        assert line.scale_values == (1.0,) * len(line.plain_text)

    def test_scale_unity_is_inert(self) -> None:
        """``<scale:1>`` parses but leaves has_scale False (the fast path)."""
        line = self._line(TextLine(text="a<scale:1>b</scale>", font="Dino"))
        assert line.plain_text == "ab"
        assert line.has_scale is False
        assert line.scale_values == (1.0, 1.0)

    def test_scale_values_align_with_plain_text(self) -> None:
        """The per-character factors are aligned with plain_text (spaces included)."""
        line = self._line(TextLine(text="a<scale:0.5>b c</scale>d", font="Dino"))
        assert line.plain_text == "ab cd"
        assert line.scale_values == (1.0, 0.5, 0.5, 0.5, 1.0)
        assert line.has_scale is True

    def test_scaled_run_gets_its_effective_height_cutter(self) -> None:
        """A 0.5x run on 1in text engraves with the 0.5in text's cutter."""
        line = self._line(TextLine(text="A<scale:0.5>b</scale>", font="Dino"), text_height=1.0)
        assert math.isclose(line.cutter_diameter, 0.125)
        # The plain character keeps the line's tool; the scaled run takes the
        # ladder for 0.5in text (0.06) -- text authored at that height.
        assert math.isclose(line.per_char_cutters[0], 0.125)
        assert math.isclose(line.per_char_cutters[1], 0.06)
        assert line.used_cutters == (0.06, 0.125)

    def test_upscale_run_gets_the_ladder_cutter(self) -> None:
        """An upscaled run takes the ladder for its taller effective height."""
        line = self._line(TextLine(text="A<scale:1.5>b</scale>", font="Dino"), text_height=0.5)
        assert math.isclose(line.cutter_diameter, 0.06)
        # 1.5 * 0.5 = 0.75in of text -> the 0.09 rung (bigger glyph, bigger tool).
        assert math.isclose(line.per_char_cutters[1], 0.09)
        assert line.used_cutters == (0.06, 0.09)

    def test_line_cutter_is_never_stepped(self) -> None:
        """The scale moves the RUN's cutter, never the line's own."""
        line = self._line(TextLine(text="A<scale:0.2>b</scale>", font="Dino"), text_height=0.3)
        assert math.isclose(line.cutter_diameter, 0.03)
        assert math.isclose(line.toolpath_text_height, 0.3 - 0.03)
        # 0.2 * 0.3 = 0.06in of text -> the 0.01 rung (the ladder's floor).
        assert math.isclose(line.per_char_cutters[1], 0.01)

    def test_explicit_cutter_governs_the_plain_runs(self) -> None:
        """An explicit cutter_size keeps the plain runs; scaled runs use the ladder."""
        line = self._line(
            TextLine(text="A<scale:0.2>b</scale>", font="Dino", cutter_size=0.06),
            text_height=0.3,
        )
        assert math.isclose(line.cutter_diameter, 0.06)
        # The plain character keeps the human's tool; the scaled run is a
        # smaller glyph, so it takes the ladder for its own height.
        assert math.isclose(line.per_char_cutters[0], 0.06)
        assert math.isclose(line.per_char_cutters[1], 0.01)

    def test_scaled_run_cutter_snaps_to_the_inventory(self) -> None:
        """A run's cutter snaps into a coarse inventory like any other cutter."""
        line = resolve_job_spec(
            self._job(TextLine(text="A<scale:0.5>b</scale>", font="Dino"), text_height=1.0),
            available_cutters=[0.09, 0.125],
        )[0].content[0]
        assert math.isclose(line.cutter_diameter, 0.125)
        # 0.5in of text wants 0.06; the coarse inventory has nothing smaller, so
        # the ladder snaps to its smallest rung (0.09).
        assert math.isclose(line.per_char_cutters[1], 0.09)

    def test_scaled_run_cutter_survives_the_reflow_text_swap(self) -> None:
        """The per-run cutter follows the text through ``with_markup``.

        The regression: a positional cutter array went stale when the
        line-content reflow moved words, so a downscaled run inherited the
        neighbouring plain text's (larger) cutter -- a *downscaled* word
        engraving with a *larger* tool. The cutters are derived from each
        character's scale factor, so the swap keeps every run on its own tool.
        """
        line = self._line(
            TextLine(text="AA <scale:0.5>bb</scale> CC", font="Dino"), text_height=1.0
        )
        # The reflow's text swap: the scaled word moves to its own line.
        scaled_line = line.with_markup("<scale:0.5>bb</scale>")
        assert scaled_line.plain_text == "bb"
        assert all(math.isclose(cutter, 0.06) for cutter in scaled_line.per_char_cutters)
        # The leftover plain line keeps the line's own tool.
        plain_line = line.with_markup("AA CC")
        assert all(math.isclose(cutter, 0.125) for cutter in plain_line.per_char_cutters)

    def test_downscaled_run_drops_by_the_half_increase(self) -> None:
        """A run under the line's cutter drops (negative raise) by half the delta."""
        line = self._line(TextLine(text="A<scale:0.5>b</scale>", font="Dino"), text_height=1.0)
        # plain 0.125, run 0.06 -> (0.06 - 0.125) / 2 = -0.0325 on the run: its
        # thinner stroke's engraved floor lands on the plain stroke's floor.
        assert math.isclose(line.baseline_raises[0], 0.0)
        assert math.isclose(line.baseline_raises[1], (0.06 - 0.125) / 2)

    def test_raises_survive_the_reflow_text_swap(self) -> None:
        """The raise follows the text through ``with_markup`` (derived, not stored)."""
        line = self._line(
            TextLine(text="AA <scale:0.5>bb</scale> CC", font="Dino"), text_height=1.0
        )
        scaled_line = line.with_markup("<scale:0.5>bb</scale>")
        assert all(
            math.isclose(raise_in, (0.06 - 0.125) / 2)
            for raise_in in scaled_line.baseline_raises
        )
        plain_line = line.with_markup("AA CC")
        assert all(math.isclose(raise_in, 0.0) for raise_in in plain_line.baseline_raises)

    def test_bold_resolves_above_the_line_cutter_with_scale(self) -> None:
        """``<b>`` + ``<scale>``: the bold rung sits above the line's plain cutter."""
        line = self._line(
            TextLine(text="<b>t<scale:0.2>a</scale></b>", font="Dino"), text_height=0.3
        )
        assert math.isclose(line.cutter_diameter, 0.03)
        assert line.bold_cutter_diameter is not None
        assert line.bold_cutter_diameter > line.cutter_diameter

    def test_spacing_fallback_follows_the_line_cutter(self) -> None:
        """The cutter-derived character_spacing fallback tracks the line cutter."""
        line = self._line(TextLine(text="t<scale:0.2>a</scale>", font="Dino"), text_height=0.3)
        assert math.isclose(line.character_spacing, line.cutter_diameter * 1.5)

    def test_spacing_explicit_value_untouched(self) -> None:
        """An explicit character_spacing is never rewritten by the scale."""
        line = self._line(
            TextLine(text="t<scale:0.2>a</scale>", font="Dino", character_spacing=0.02),
            text_height=0.3,
        )
        assert math.isclose(line.character_spacing, 0.02)


class TestGlyphRenderPlan:
    """The unified per-glyph (cutter, scale) plan for ``<b>``/``<scale:N>``."""

    @staticmethod
    def _line(text: str, **kwargs: object) -> ResolvedTextLine:
        """Build a resolved line directly (no markup parsing)."""
        from plt_optimizer.generate.markup import PLAIN, Run

        nominal = float(kwargs.pop("nominal", 0.5))
        cutter = float(kwargs.pop("cutter", 0.06))
        kwargs.setdefault("plain_text", text)
        kwargs.setdefault("styled_runs", (Run(text, PLAIN),))
        return ResolvedTextLine(
            text=text,
            nominal_text_height=nominal,
            toolpath_text_height=nominal - cutter,
            cutter_diameter=cutter,
            character_spacing=0.0,
            line_spacing=0.0,
            **kwargs,  # type: ignore[arg-type]
        )

    def test_plain_line_has_no_plan(self) -> None:
        """A line with neither bold nor scale runs has no plan (fast path)."""
        line = self._line("AV")
        assert glyph_render_plan(line) is None

    def test_scale_plan_is_the_centerline_formula(self) -> None:
        """g = (s*nominal - run_cutter) / (nominal - cutter) about the baseline."""
        from plt_optimizer.generate.markup import PLAIN, Run, Style

        line = self._line(
            "aB",
            styled_runs=(Run("a", PLAIN), Run("B", Style(scale=0.5))),
        )
        plan = glyph_render_plan(line)
        assert plan is not None
        diameters, scales = plan
        # The plain char keeps the line's 0.06 tool; the 0.5x run is 0.25in of
        # text, whose ladder cutter is 0.03 -- a downscaled run engraves lighter.
        assert diameters == (0.06, 0.03)
        expected = (0.5 * 0.5 - 0.03) / (0.5 - 0.06)
        assert scales[0] == 1.0
        assert scales[1] == pytest.approx(expected)

    def test_bold_plan_reproduces_the_bold_scale(self) -> None:
        """At s=1 the plan equals _bold_glyph_scale (bold is the special case)."""
        from plt_optimizer.generate.markup import PLAIN, Run, Style

        line = self._line(
            "aB",
            styled_runs=(Run("a", PLAIN), Run("B", Style(bold=True))),
            bold_cutter_diameter=0.09,
        )
        plan = glyph_render_plan(line)
        assert plan is not None
        diameters, scales = plan
        assert diameters == (0.06, 0.09)
        assert scales[0] == 1.0
        assert scales[1] == pytest.approx(line._bold_glyph_scale())
        # The plan's companion raise: (run cutter - line cutter) / 2.
        assert line.baseline_raises == (0.0, 0.015)

    def test_bold_scaled_composes_the_silhouette_formula(self) -> None:
        """A bold+scaled glyph takes the bold rung at its effective height."""
        from plt_optimizer.generate.markup import Run, Style

        line = self._line(
            "B",
            styled_runs=(Run("B", Style(bold=True, scale=0.5)),),
            bold_cutter_diameter=0.09,
            scale_cutter_inventory=(0.015, 0.03, 0.045, 0.06, 0.09),
        )
        plan = glyph_render_plan(line)
        assert plan is not None
        diameters, scales = plan
        # 0.5 * 0.5 = 0.25in of text -> plain ladder 0.03, bold rung 0.045.
        assert diameters == (0.045,)
        assert scales[0] == pytest.approx((0.5 * 0.5 - 0.045) / (0.5 - 0.06))

    def test_degenerate_factor_clamps_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A run taller than every inventory rung clamps (WARNING).

        The per-run ladder normally guarantees a cutter that fits the run's own
        effective height, so the geometry numerator stays positive. A coarse
        inventory with no tool small enough for a heavily downscaled run is the
        one way to a non-positive factor: the geometry clamps to a minimal
        factor instead of inverting.
        """
        from plt_optimizer.generate.markup import Run, Style
        from plt_optimizer.generate.resolution import MIN_SCALED_GLYPH_FACTOR

        with caplog.at_level("WARNING"):
            line = self._line(
                "B",
                nominal=0.3,
                cutter=0.06,
                styled_runs=(Run("B", Style(scale=0.2)),),
                # 0.2 * 0.3 = 0.06in of text; the shop owns nothing below 0.09.
                scale_cutter_inventory=(0.09, 0.125),
            )
            plan = glyph_render_plan(line)
        assert plan is not None
        _diameters, scales = plan
        assert scales[0] == MIN_SCALED_GLYPH_FACTOR
        assert "leaves no material" in caplog.text

    def test_upscale_grows_the_factor_above_one(self) -> None:
        """s > 1 gives a factor > 1 (the cap rises above the plain run's)."""
        from plt_optimizer.generate.markup import Run, Style

        line = self._line("B", styled_runs=(Run("B", Style(scale=1.5)),))
        plan = glyph_render_plan(line)
        assert plan is not None
        _diameters, scales = plan
        assert scales[0] > 1.0


class TestSupersubscriptDownscaleCascade:
    """supersubscript_downscale must cascade line -> label -> job -> default."""

    @staticmethod
    def _resolved(**kwargs: object) -> ResolvedTextLine:
        """Resolve a one-line job with ``kwargs`` spread over the tiers."""
        line_kwargs = {
            k[len("line_") :]: v for k, v in kwargs.items() if k.startswith("line_")
        }
        label_kwargs = {k[len("label_") :]: v for k, v in kwargs.items() if k.startswith("label_")}
        job_kwargs = {k[len("job_") :]: v for k, v in kwargs.items() if k.startswith("job_")}
        job = JobSpec(
            job_name="J",
            width=3.0,
            height=1.0,
            text_height=0.5,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=3.0,
                    height=1.0,
                    content=[TextLine(text="X", **line_kwargs)],  # type: ignore[arg-type]
                    **label_kwargs,  # type: ignore[arg-type]
                )
            ],
            **job_kwargs,  # type: ignore[arg-type]
        )
        return resolve_job_spec(job)[0].content[0]

    def test_default_when_all_omit(self) -> None:
        """All levels omitting yields the 0.5 default."""
        assert self._resolved().supersubscript_downscale == DEFAULT_SUPERSUBSCRIPT_DOWNSCALE
        assert DEFAULT_SUPERSUBSCRIPT_DOWNSCALE == 0.5

    def test_explicit_bounds_are_honored(self) -> None:
        """An intentional 0.2 / 1.0 is not treated as unset."""
        assert self._resolved(line_supersubscript_downscale=0.2).supersubscript_downscale == 0.2
        assert self._resolved(line_supersubscript_downscale=1.0).supersubscript_downscale == 1.0

    def test_job_value_used_when_label_omits(self) -> None:
        """Job-level value cascades to the line."""
        assert self._resolved(job_supersubscript_downscale=0.8).supersubscript_downscale == 0.8

    def test_label_overrides_job(self) -> None:
        """Label-level value beats the job-level one."""
        resolved = self._resolved(
            label_supersubscript_downscale=0.3, job_supersubscript_downscale=0.8
        )
        assert resolved.supersubscript_downscale == 0.3

    def test_line_overrides_label_and_job(self) -> None:
        """Line-level value wins over both parents."""
        resolved = self._resolved(
            line_supersubscript_downscale=0.9,
            label_supersubscript_downscale=0.3,
            job_supersubscript_downscale=0.8,
        )
        assert resolved.supersubscript_downscale == 0.9

    def test_bounds_are_enforced(self) -> None:
        """The schema rejects a downscale outside [0.2, 1.0]."""
        with pytest.raises(ValidationError):
            TextLine(text="X", supersubscript_downscale=0.1)
        with pytest.raises(ValidationError):
            TextLine(text="X", supersubscript_downscale=1.1)


class TestSupSubResolution:
    """``<sup>``/``<sub>`` resolve the shift, the downscale and the run cutter."""

    @staticmethod
    def _job(*lines: TextLine, **job_kwargs: object) -> JobSpec:
        """Build a single-label job over ``lines``."""
        job_kwargs.setdefault("text_height", 0.5)
        return JobSpec(
            job_name="J",
            width=3.0,
            height=1.0,
            labels=[LabelSpec(id="lbl", width=3.0, height=1.0, content=list(lines))],
            **job_kwargs,
        )

    def _line(self, *lines: TextLine, **job_kwargs: object) -> ResolvedTextLine:
        """Resolve the job's first line against the test inventory."""
        return resolve_job_spec(
            self._job(*lines, **job_kwargs), available_cutters=_INVENTORY
        )[0].content[0]

    def test_no_markup_has_no_sup_sub(self) -> None:
        """A markup-free line has no shift (the renderers' fast path)."""
        line = self._line(TextLine(text="PLAIN", font="Dino"))
        assert line.has_sup_sub is False
        # The resolved line carries one plain run, so the per-character arrays
        # are all-neutral (a hand-built line has styled_runs == () and empty
        # arrays -- the renderers' fast-path gate reads has_sup_sub).
        assert line.sup_sub_values == (None, None, None, None, None)
        assert line.sup_sub_scales == (1.0,) * 5
        assert line.sup_sub_shifts == (0.0,) * 5

    def test_bare_sup_scales_and_shifts(self) -> None:
        """A bare <sup> downscales to d and rises 1 - d/2 cap heights (centre on capline)."""
        line = self._line(TextLine(text="1<sup>st</sup>", font="Dino"))
        assert line.plain_text == "1st"
        assert line.has_sup_sub is True
        assert line.sup_sub_scales == (1.0, 0.5, 0.5)
        assert line.sup_sub_shifts == (0.0, 0.75, 0.75)

    def test_bare_sub_scales_and_drops(self) -> None:
        """A bare <sub> downscales to d and drops d/2 cap heights (centre on baseline)."""
        line = self._line(TextLine(text="x<sub>ij</sub>", font="Dino"))
        assert line.plain_text == "xij"
        assert line.sup_sub_scales == (1.0, 0.5, 0.5)
        assert line.sup_sub_shifts == (0.0, -0.25, -0.25)

    def test_shift_values_align_with_plain_text(self) -> None:
        """The per-character arrays are aligned with plain_text (spaces included)."""
        line = self._line(TextLine(text="a<sup>b c</sup>d", font="Dino"))
        assert line.plain_text == "ab cd"
        assert line.sup_sub_scales == (1.0, 0.5, 0.5, 0.5, 1.0)
        assert line.sup_sub_shifts == (0.0, 0.75, 0.75, 0.75, 0.0)

    def test_argumented_form_shifts_only(self) -> None:
        """<sup:0.3> sets the shift explicitly and applies no scaling."""
        line = self._line(TextLine(text="1<sup:0.3>st</sup>", font="Dino"))
        assert line.sup_sub_scales == (1.0, 1.0, 1.0)
        assert line.sup_sub_shifts == (0.0, 0.3, 0.3)

    def test_argumented_form_keeps_the_line_cutter(self) -> None:
        """A shift-only run engraves with the line's own tool (no pen split)."""
        line = self._line(TextLine(text="A<sup:0.3>b</sup>", font="Dino"), text_height=1.0)
        assert math.isclose(line.cutter_diameter, 0.125)
        assert line.per_char_cutters == (0.125, 0.125)
        assert line.used_cutters == (0.125,)

    def test_bare_run_gets_the_downscaled_height_cutter(self) -> None:
        """A bare <sup> on 1in text engraves with the 0.5in text's cutter."""
        line = self._line(TextLine(text="A<sup>b</sup>", font="Dino"), text_height=1.0)
        assert math.isclose(line.cutter_diameter, 0.125)
        # d = 0.5 -> 0.5in of text -> the 0.06 tool (the <scale:0.5> ladder).
        assert math.isclose(line.per_char_cutters[0], 0.125)
        assert math.isclose(line.per_char_cutters[1], 0.06)
        assert line.used_cutters == (0.06, 0.125)

    def test_downscale_follows_the_cascaded_field(self) -> None:
        """supersubscript_downscale drives both the scale and the shift."""
        line = self._line(
            TextLine(text="A<sup>b</sup>", font="Dino"),
            text_height=1.0,
            supersubscript_downscale=0.8,
        )
        assert line.sup_sub_scales == (1.0, 0.8)
        # 1 - d/2 = 0.6: the sup's centre still lands on the capline.
        assert line.sup_sub_shifts[0] == 0.0
        assert math.isclose(line.sup_sub_shifts[1], 0.6)
        # 0.8in of text -> the 0.09 tool.
        assert math.isclose(line.per_char_cutters[1], 0.09)

    def test_scale_and_sup_compose_multiplicatively(self) -> None:
        """A <scale:N> run inside <sup> composes to N * d."""
        line = self._line(
            TextLine(text="A<sup><scale:0.5>b</scale></sup>", font="Dino"), text_height=1.0
        )
        assert line.sup_sub_scales == (1.0, 0.5)
        assert line.scale_values == (1.0, 0.5)
        # The effective silhouette factor is 0.5 * 0.5 = 0.25 -> 0.25in of text.
        assert math.isclose(line.per_char_cutters[1], 0.03)

    def test_shift_survives_the_reflow_text_swap(self) -> None:
        """with_markup() keeps every run's cutter derived from its own style.

        The line-content reflow rewrites a line's text; a positional per-
        character cutter array would stay at the old indices and hand a
        downscaled run its neighbour's (larger) tool. The cutters derive from
        each character's style, so they follow the moved words.
        """
        line = self._line(
            TextLine(text="AA<sup>b</sup>", font="Dino"), text_height=1.0
        )
        swapped = line.with_markup("AA<sup>b</sup> CC")
        assert swapped.plain_text == "AAb CC"
        # The scaled run (index 2) keeps the downscaled-height tool; the plain
        # characters (including the appended ones) keep the line's tool. The
        # space owns no glyph and carries the line's cutter.
        assert swapped.per_char_cutters == (0.125, 0.125, 0.06, 0.125, 0.125, 0.125)

    def test_explicit_cutter_size_governs_the_plain_runs(self) -> None:
        """An explicit cutter_size is a human decision, never overridden."""
        line = self._line(
            TextLine(text="A<sup>b</sup>", font="Dino", cutter_size=0.09), text_height=1.0
        )
        assert math.isclose(line.cutter_diameter, 0.09)
        # The plain run keeps the explicit tool; the bare sup run still takes
        # the ladder for its own (downscaled) height.
        assert math.isclose(line.per_char_cutters[0], 0.09)
        assert math.isclose(line.per_char_cutters[1], 0.06)

    def test_sup_with_bold_takes_the_bold_rung(self) -> None:
        """A bold + bare-sup character takes the bold rung at the effective height."""
        line = self._line(
            TextLine(text="A<b><sup>b</sup></b>", font="Dino"), text_height=1.0
        )
        assert line.bold_cutter_diameter is not None
        # 0.5in of text: plain ladder 0.06, bold rung 0.09. The bold+sup
        # character is plain_text index 1 (the tags engrave nothing).
        assert math.isclose(line.per_char_cutters[1], 0.09)
        assert 0.09 in line.used_cutters


class TestGlyphRenderPlanSupSub:
    """The per-glyph plan folds the bare <sup>/<sub> downscale into the scale."""

    @staticmethod
    def _line(text: str, **kwargs: object) -> ResolvedTextLine:
        """Build a resolved line directly (no markup parsing)."""
        from plt_optimizer.generate.markup import PLAIN, Run

        nominal = float(kwargs.pop("nominal", 0.5))
        cutter = float(kwargs.pop("cutter", 0.06))
        kwargs.setdefault("plain_text", text)
        kwargs.setdefault("styled_runs", (Run(text, PLAIN),))
        return ResolvedTextLine(
            text=text,
            nominal_text_height=nominal,
            toolpath_text_height=nominal - cutter,
            cutter_diameter=cutter,
            character_spacing=0.0,
            line_spacing=0.0,
            **kwargs,  # type: ignore[arg-type]
        )

    def test_shift_only_plan_is_all_unity(self) -> None:
        """<sup:0.3> scales nothing: the plan is all-unity (shift-only render)."""
        from plt_optimizer.generate.markup import PLAIN, Run, Style, SupSub

        line = self._line(
            "aB",
            styled_runs=(Run("a", PLAIN), Run("B", Style(sup_sub=SupSub(1, 0.3)))),
        )
        plan = glyph_render_plan(line)
        assert plan is not None
        diameters, scales = plan
        assert diameters == (0.06, 0.06)
        assert scales == (1.0, 1.0)

    def test_bare_sup_plan_is_the_centerline_formula(self) -> None:
        """A bare <sup> is exactly a <scale:d> run in the plan."""
        from plt_optimizer.generate.markup import PLAIN, Run, Style, SupSub

        line = self._line(
            "aB",
            styled_runs=(Run("a", PLAIN), Run("B", Style(sup_sub=SupSub(1)))),
            scale_cutter_inventory=tuple(_INVENTORY),
        )
        plan = glyph_render_plan(line)
        assert plan is not None
        diameters, scales = plan
        # d = 0.5 -> 0.25in of text -> the 0.03 ladder cutter.
        assert diameters == (0.06, 0.03)
        expected = (0.5 * 0.5 - 0.03) / (0.5 - 0.06)
        assert scales[0] == 1.0
        assert scales[1] == pytest.approx(expected)

    def test_downscale_of_one_is_inert(self) -> None:
        """supersubscript_downscale 1.0 leaves the bare tag unscaled."""
        from plt_optimizer.generate.markup import PLAIN, Run, Style, SupSub

        line = self._line(
            "aB",
            styled_runs=(Run("a", PLAIN), Run("B", Style(sup_sub=SupSub(-1)))),
            supersubscript_downscale=1.0,
            scale_cutter_inventory=tuple(_INVENTORY),
        )
        plan = glyph_render_plan(line)
        assert plan is not None
        _diameters, scales = plan
        assert scales == (1.0, 1.0)


class TestSmallCapsMarkupFlags:
    """The inline ``<sc>`` selection is DERIVED from the resolved runs."""

    @staticmethod
    def _job(*lines: TextLine, **job_kwargs: object) -> JobSpec:
        """Build a single-label job over ``lines``."""
        return JobSpec(
            job_name="J",
            labels=[LabelSpec(id="lbl", width=3.0, height=1.0, content=list(lines))],
            **job_kwargs,
        )

    def _line(self, *lines: TextLine, **job_kwargs: object) -> ResolvedTextLine:
        """Resolve the job's first line."""
        return resolve_job_spec(self._job(*lines, **job_kwargs))[0].content[0]

    def test_markup_free_line_has_no_small_caps(self) -> None:
        """A tag-free line resolves to all-False flags (the renderers' gate)."""
        line = self._line(TextLine(text="PLAIN", font="Dino"))
        assert line.has_small_caps is False
        assert line.small_caps_flags == (False,) * 5

    def test_flags_align_with_plain_text(self) -> None:
        """The per-character flags are aligned with plain_text (spaces included)."""
        line = self._line(TextLine(text="a<sc>b c</sc>d", font="Dino"))
        assert line.plain_text == "ab cd"
        assert line.has_small_caps is True
        assert line.small_caps_flags == (False, True, True, True, False)

    def test_sc_scale_cascades_onto_the_line(self) -> None:
        """The tag reuses the cascading ``sc_scale`` (no new field)."""
        line = self._line(TextLine(text="<sc>abc</sc>", font="Dino", sc_scale=0.42))
        assert math.isclose(line.sc_scale, 0.42)
        default = self._line(TextLine(text="<sc>abc</sc>", font="Dino"))
        assert math.isclose(default.sc_scale, DEFAULT_SC_SCALE)

    def test_small_caps_never_moves_the_cutter(self) -> None:
        """Small caps is a geometry transform: the cutter/heights stay put."""
        plain = self._line(TextLine(text="abc", font="Dino"))
        struck = self._line(TextLine(text="<sc>abc</sc>", font="Dino"))
        assert math.isclose(struck.cutter_diameter, plain.cutter_diameter)
        assert math.isclose(struck.toolpath_text_height, plain.toolpath_text_height)
        assert struck.used_cutters == plain.used_cutters
        assert glyph_render_plan(struck) is None

    def test_flags_survive_the_reflow_text_swap(self) -> None:
        """The per-character selection follows the text through ``with_markup``.

        The rule any per-glyph markup follows: the flags are DERIVED from the
        runs (never a stored positional array), so the line-content reflow's
        text swap keeps every run on its own style.
        """
        line = self._line(TextLine(text="AA <sc>bb</sc> CC", font="Dino"))
        assert line.plain_text == "AA bb CC"
        assert line.small_caps_flags == (
            False,
            False,
            False,
            True,
            True,
            False,
            False,
            False,
        )
        # The reflow's text swap: the small-caps word moves to its own line.
        moved = line.with_markup("<sc>bb</sc>")
        assert moved.plain_text == "bb"
        assert moved.small_caps_flags == (True, True)
        # The leftover plain line keeps no small caps at all.
        plain_line = line.with_markup("AA CC")
        assert plain_line.has_small_caps is False
        assert plain_line.small_caps_flags == (False,) * 5


class TestLabelShapeResolution:
    """The ``type`` cascade resolves to ``ResolvedLabel.shape_type``."""

    @staticmethod
    def _job(**levels: object) -> JobSpec:
        """Build a single-label job applying ``type`` at the given levels."""
        job_kwargs: dict[str, object] = {}
        label_kwargs: dict[str, object] = {}
        if "job" in levels:
            job_kwargs["type"] = levels["job"]
        if "label" in levels:
            label_kwargs["type"] = levels["label"]
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
                    content=[TextLine(text="X")],
                )
            ],
        )

    def test_default_is_rect(self) -> None:
        """No level declaring a shape resolves to the rect fallback."""
        assert DEFAULT_LABEL_SHAPE == "rect"
        assert resolve_job_spec(self._job())[0].shape_type == "rect"

    def test_job_level_round_cascades(self) -> None:
        """A job-level round applies to labels that omit the field."""
        assert resolve_job_spec(self._job(job="round"))[0].shape_type == "round"

    def test_label_overrides_job(self) -> None:
        """A label-level rect beats the job-level round."""
        labels = resolve_job_spec(self._job(job="round", label="rect"))
        assert labels[0].shape_type == "rect"

    def test_label_round_beats_job_rect(self) -> None:
        """A label-level round beats the job-level rect."""
        labels = resolve_job_spec(self._job(job="rect", label="round"))
        assert labels[0].shape_type == "round"


class TestRoundedRectangleResolution:
    """The rounded-rectangle cascade resolves onto ResolvedLabel."""

    @staticmethod
    def _job(**levels: object) -> JobSpec:
        """Build a single-label job applying the fields at the given levels."""
        job_kwargs: dict[str, object] = {}
        label_kwargs: dict[str, object] = {}
        for key, value in levels.items():
            tier, _, field = key.partition("__")
            if tier == "job":
                job_kwargs[field] = value
            else:
                label_kwargs[field] = value
        return JobSpec(
            job_name="J",
            text_height=0.5,
            **job_kwargs,
            labels=[
                LabelSpec(
                    id="lbl",
                    width=4.0,
                    height=2.0,
                    **label_kwargs,
                    content=[TextLine(text="X")],
                )
            ],
        )

    def test_defaults_are_sharp(self) -> None:
        """No level declaring the fields resolves to the sharp rectangle."""
        assert DEFAULT_ROUNDED_RECTANGLE_FRACTION == 0.0
        assert DEFAULT_ROUNDED_RECTANGLE_PROPORTIONAL is False
        label = resolve_job_spec(self._job())[0]
        assert label.rounded_rectangle_fraction == 0.0
        assert label.rounded_rectangle_proportional is False

    def test_job_level_cascades(self) -> None:
        """Job-level values apply to labels that omit the fields."""
        label = resolve_job_spec(
            self._job(job__rounded_rectangle_fraction=0.5, job__rounded_rectangle_proportional=True)
        )[0]
        assert label.rounded_rectangle_fraction == pytest.approx(0.5)
        assert label.rounded_rectangle_proportional is True

    def test_label_overrides_job(self) -> None:
        """Label-level values beat the job level."""
        label = resolve_job_spec(
            self._job(
                job__rounded_rectangle_fraction=0.5,
                label__rounded_rectangle_fraction=0.0,
            )
        )[0]
        assert label.rounded_rectangle_fraction == 0.0

    def test_explicit_zero_is_honored(self) -> None:
        """An explicit label-level 0.0 is a value, not 'unset'."""
        label = resolve_job_spec(
            self._job(
                job__rounded_rectangle_fraction=0.25,
                label__rounded_rectangle_fraction=0.0,
            )
        )[0]
        assert label.rounded_rectangle_fraction == 0.0

    def test_round_shape_keeps_the_fraction(self) -> None:
        """Resolution carries the fraction onto round labels (the renderer
        treats it as inert there)."""
        label = resolve_job_spec(
            self._job(label__type="round", label__rounded_rectangle_fraction=0.5)
        )[0]
        assert label.shape_type == "round"
        assert label.rounded_rectangle_fraction == pytest.approx(0.5)


class TestRoundLabelCircleFilling:
    """A round label declaring one dimension resolves to a circle."""

    def test_width_only_fills_height(self) -> None:
        """Width-only round label resolves width == height (a circle)."""
        job = JobSpec(
            job_name="J",
            text_height=0.5,
            labels=[LabelSpec(id="c", type="round", width=2.5, content=[TextLine(text="X")])],
        )
        label = resolve_job_spec(job)[0]
        assert label.width == pytest.approx(2.5)
        assert label.height == pytest.approx(2.5)
        assert label.shape_type == "round"

    def test_height_only_fills_width(self) -> None:
        """Height-only round label resolves to the matching circle."""
        job = JobSpec(
            job_name="J",
            text_height=0.5,
            labels=[LabelSpec(id="c", type="round", height=1.5, content=[TextLine(text="X")])],
        )
        label = resolve_job_spec(job)[0]
        assert label.width == pytest.approx(1.5)
        assert label.height == pytest.approx(1.5)

    def test_both_dimensions_stay_independent(self) -> None:
        """A round label declaring both keeps the ellipse (no square-up)."""
        job = JobSpec(
            job_name="J",
            text_height=0.5,
            labels=[
                LabelSpec(id="e", type="round", width=4.0, height=2.0, content=[TextLine(text="X")])
            ],
        )
        label = resolve_job_spec(job)[0]
        assert (label.width, label.height) == (4.0, 2.0)

    def test_rect_label_unaffected(self) -> None:
        """A rect label's dimensions resolve exactly as before."""
        job = JobSpec(
            job_name="J",
            text_height=0.5,
            labels=[LabelSpec(id="r", width=4.0, height=2.0, content=[TextLine(text="X")])],
        )
        label = resolve_job_spec(job)[0]
        assert (label.width, label.height) == (4.0, 2.0)
        assert label.shape_type == "rect"


class TestEllipseContentInset:
    """The radial content inset for round labels (inscribed centered block)."""

    def test_circle_inset_is_the_inscribed_square(self) -> None:
        """A circle's inset content block is the inscribed square (a*sqrt2)."""
        # 4in circle, 0.5in margin -> inset radius 1.5 -> square side 3/sqrt2.
        width, height = ellipse_content_inset(4.0, 4.0, 0.5, 0.5)
        expected = 2.0 * 1.5 / math.sqrt(2.0)
        assert width == pytest.approx(expected)
        assert height == pytest.approx(expected)

    def test_zero_margin_square_is_the_inscribed_square(self) -> None:
        """No margin on a square label: the inscribed block is the inscribed
        square (a 4x4 box's own corners lie OUTSIDE a radius-2 circle)."""
        width, height = ellipse_content_inset(4.0, 4.0, 0.0, 0.0)
        assert width == pytest.approx(2.0 * math.sqrt(2.0))
        assert height == pytest.approx(2.0 * math.sqrt(2.0))

    def test_flat_ellipse_scales_by_one_over_sqrt_two(self) -> None:
        """The inscribed block similar to the bounding box is always 1/sqrt2
        of it (a property of similar inscribed rectangles, flat included)."""
        width, height = ellipse_content_inset(10.0, 0.5, 0.0, 0.0)
        assert width == pytest.approx(10.0 / math.sqrt(2.0))
        assert height == pytest.approx(0.5 / math.sqrt(2.0))

    def test_strictly_smaller_than_the_box_inset(self) -> None:
        """The radial inset is strictly tighter than the rectangular inset."""
        radial_w, radial_h = ellipse_content_inset(4.0, 2.0, 0.2, 0.2)
        assert radial_w < 4.0 - 2 * 0.2
        assert radial_h < 2.0 - 2 * 0.2

    def test_preserves_the_label_aspect_ratio(self) -> None:
        """The inscribed block keeps the label's aspect ratio."""
        width, height = ellipse_content_inset(6.0, 2.0, 0.3, 0.3)
        assert width / height == pytest.approx(6.0 / 2.0)

    def test_margin_past_the_centre_collapses_to_zero(self) -> None:
        """A margin reaching past the centre leaves no content area."""
        assert ellipse_content_inset(2.0, 2.0, 1.0, 0.1) == (0.0, 0.0)
        assert ellipse_content_inset(2.0, 2.0, 0.1, 1.5) == (0.0, 0.0)

    def test_corner_lies_on_the_inset_ellipse(self) -> None:
        """The block's corner (W/2, H/2) sits exactly on the inset ellipse."""
        width, height = ellipse_content_inset(4.0, 2.0, 0.25, 0.25)
        semi_a = 4.0 / 2.0 - 0.25
        semi_b = 2.0 / 2.0 - 0.25
        assert (width / 2 / semi_a) ** 2 + (height / 2 / semi_b) ** 2 == pytest.approx(1.0)


class TestHoleAwareShiftComputation:
    """Test the _compute_hole_aware_shifts() pure function."""

    def test_vertical_shift_single_top_hole(self) -> None:
        """Single top hole only: vertical shift = +diameter/2."""
        from plt_optimizer.generate.resolution import _compute_hole_aware_shifts

        resolved = ResolvedLabel(
            id="test",
            count=1,
            width=2.0,
            height=1.5,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
            holes=[ResolvedHoleSpec(location="top", diameter=0.2)],
            content=[],
        )
        v_shift, h_shift = _compute_hole_aware_shifts(resolved)
        assert v_shift == pytest.approx(0.1)  # (0.2 - 0) / 2
        assert h_shift == pytest.approx(0.0)  # (0 - 0) / 2

    def test_vertical_shift_single_bottom_hole(self) -> None:
        """Single bottom hole only: vertical shift = -diameter/2."""
        from plt_optimizer.generate.resolution import _compute_hole_aware_shifts

        resolved = ResolvedLabel(
            id="test",
            count=1,
            width=2.0,
            height=1.5,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
            holes=[ResolvedHoleSpec(location="bottom", diameter=0.2)],
            content=[],
        )
        v_shift, h_shift = _compute_hole_aware_shifts(resolved)
        assert v_shift == pytest.approx(-0.1)  # (0 - 0.2) / 2
        assert h_shift == pytest.approx(0.0)

    def test_vertical_shift_both_holes_top_larger(self) -> None:
        """Top larger than bottom: positive shift."""
        from plt_optimizer.generate.resolution import _compute_hole_aware_shifts

        resolved = ResolvedLabel(
            id="test",
            count=1,
            width=2.0,
            height=1.5,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
            holes=[
                ResolvedHoleSpec(location="top", diameter=0.2),
                ResolvedHoleSpec(location="bottom", diameter=0.1),
            ],
            content=[],
        )
        v_shift, h_shift = _compute_hole_aware_shifts(resolved)
        assert v_shift == pytest.approx(0.05)  # (0.2 - 0.1) / 2
        assert h_shift == pytest.approx(0.0)

    def test_vertical_shift_both_holes_bottom_larger(self) -> None:
        """Bottom larger than top: negative shift."""
        from plt_optimizer.generate.resolution import _compute_hole_aware_shifts

        resolved = ResolvedLabel(
            id="test",
            count=1,
            width=2.0,
            height=1.5,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
            holes=[
                ResolvedHoleSpec(location="top", diameter=0.1),
                ResolvedHoleSpec(location="bottom", diameter=0.2),
            ],
            content=[],
        )
        v_shift, h_shift = _compute_hole_aware_shifts(resolved)
        assert v_shift == pytest.approx(-0.05)  # (0.1 - 0.2) / 2
        assert h_shift == pytest.approx(0.0)

    def test_vertical_shift_equal_holes(self) -> None:
        """Equal top and bottom holes: no shift."""
        from plt_optimizer.generate.resolution import _compute_hole_aware_shifts

        resolved = ResolvedLabel(
            id="test",
            count=1,
            width=2.0,
            height=1.5,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
            holes=[
                ResolvedHoleSpec(location="top", diameter=0.15),
                ResolvedHoleSpec(location="bottom", diameter=0.15),
            ],
            content=[],
        )
        v_shift, h_shift = _compute_hole_aware_shifts(resolved)
        assert v_shift == pytest.approx(0.0)
        assert h_shift == pytest.approx(0.0)

    def test_vertical_shift_no_holes(self) -> None:
        """No holes: no shift."""
        from plt_optimizer.generate.resolution import _compute_hole_aware_shifts

        resolved = ResolvedLabel(
            id="test",
            count=1,
            width=2.0,
            height=1.5,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
            holes=[],
            content=[],
        )
        v_shift, h_shift = _compute_hole_aware_shifts(resolved)
        assert v_shift == pytest.approx(0.0)
        assert h_shift == pytest.approx(0.0)

    def test_horizontal_shift_single_left_hole(self) -> None:
        """Single left hole only: horizontal shift = +diameter/2."""
        from plt_optimizer.generate.resolution import _compute_hole_aware_shifts

        resolved = ResolvedLabel(
            id="test",
            count=1,
            width=2.0,
            height=1.5,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
            holes=[ResolvedHoleSpec(location="left", diameter=0.2)],
            content=[],
            adjust_h_center_for_single_side_hole=True,
        )
        v_shift, h_shift = _compute_hole_aware_shifts(resolved)
        assert v_shift == pytest.approx(0.0)
        assert h_shift == pytest.approx(0.1)  # (0.2 - 0) / 2

    def test_horizontal_shift_single_right_hole(self) -> None:
        """Single right hole only: horizontal shift = -diameter/2."""
        from plt_optimizer.generate.resolution import _compute_hole_aware_shifts

        resolved = ResolvedLabel(
            id="test",
            count=1,
            width=2.0,
            height=1.5,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
            holes=[ResolvedHoleSpec(location="right", diameter=0.2)],
            content=[],
            adjust_h_center_for_single_side_hole=True,
        )
        v_shift, h_shift = _compute_hole_aware_shifts(resolved)
        assert v_shift == pytest.approx(0.0)
        assert h_shift == pytest.approx(-0.1)  # (0 - 0.2) / 2

    def test_horizontal_shift_both_holes_left_larger(self) -> None:
        """Left larger than right: positive horizontal shift."""
        from plt_optimizer.generate.resolution import _compute_hole_aware_shifts

        resolved = ResolvedLabel(
            id="test",
            count=1,
            width=2.0,
            height=1.5,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
            holes=[
                ResolvedHoleSpec(location="left", diameter=0.2),
                ResolvedHoleSpec(location="right", diameter=0.1),
            ],
            content=[],
            adjust_h_center_for_single_side_hole=True,
        )
        v_shift, h_shift = _compute_hole_aware_shifts(resolved)
        assert v_shift == pytest.approx(0.0)
        assert h_shift == pytest.approx(0.05)  # (0.2 - 0.1) / 2

    def test_horizontal_shift_both_holes_right_larger(self) -> None:
        """Right larger than left: negative horizontal shift."""
        from plt_optimizer.generate.resolution import _compute_hole_aware_shifts

        resolved = ResolvedLabel(
            id="test",
            count=1,
            width=2.0,
            height=1.5,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
            holes=[
                ResolvedHoleSpec(location="left", diameter=0.1),
                ResolvedHoleSpec(location="right", diameter=0.2),
            ],
            content=[],
            adjust_h_center_for_single_side_hole=True,
        )
        v_shift, h_shift = _compute_hole_aware_shifts(resolved)
        assert v_shift == pytest.approx(0.0)
        assert h_shift == pytest.approx(-0.05)  # (0.1 - 0.2) / 2

    def test_combined_vertical_and_horizontal_shifts(self) -> None:
        """All four holes with different sizes: both shifts apply."""
        from plt_optimizer.generate.resolution import _compute_hole_aware_shifts

        resolved = ResolvedLabel(
            id="test",
            count=1,
            width=2.0,
            height=1.5,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
            holes=[
                ResolvedHoleSpec(location="top", diameter=0.2),
                ResolvedHoleSpec(location="bottom", diameter=0.1),
                ResolvedHoleSpec(location="left", diameter=0.25),
                ResolvedHoleSpec(location="right", diameter=0.15),
            ],
            content=[],
            adjust_h_center_for_single_side_hole=True,
        )
        v_shift, h_shift = _compute_hole_aware_shifts(resolved)
        assert v_shift == pytest.approx(0.05)  # (0.2 - 0.1) / 2
        assert h_shift == pytest.approx(0.05)  # (0.25 - 0.15) / 2


class TestComputeHorizontalOffsetWithHoleShift:
    """Test compute_horizontal_offset() with hole-aware shift parameter."""

    def test_center_aligned_with_positive_shift(self) -> None:
        """Center alignment with positive shift: shifts right."""
        # available_width=2.0, rendered_width=1.0, margin=0.1
        # natural center = 0.1 + (2.0 - 1.0)/2 = 0.6
        # with shift +0.1 = 0.7
        result = compute_horizontal_offset(
            rendered_width=1.0,
            available_width=2.0,
            margin=0.1,
            alignment="center",
            hole_aware_h_shift=0.1,
        )
        assert result == pytest.approx(0.7)

    def test_center_aligned_with_negative_shift(self) -> None:
        """Center alignment with negative shift: shifts left."""
        result = compute_horizontal_offset(
            rendered_width=1.0,
            available_width=2.0,
            margin=0.1,
            alignment="center",
            hole_aware_h_shift=-0.1,
        )
        assert result == pytest.approx(0.5)

    def test_center_aligned_no_shift(self) -> None:
        """Center alignment with zero shift: same as historical."""
        result = compute_horizontal_offset(
            rendered_width=1.0,
            available_width=2.0,
            margin=0.1,
            alignment="center",
            hole_aware_h_shift=0.0,
        )
        assert result == pytest.approx(0.6)

    def test_left_aligned_ignores_shift(self) -> None:
        """Left alignment: shift is ignored."""
        result = compute_horizontal_offset(
            rendered_width=1.0,
            available_width=2.0,
            margin=0.1,
            alignment="left",
            hole_aware_h_shift=0.5,
        )
        assert result == pytest.approx(0.1)  # margin only

    def test_right_aligned_ignores_shift(self) -> None:
        """Right alignment: shift is ignored."""
        result = compute_horizontal_offset(
            rendered_width=1.0,
            available_width=2.0,
            margin=0.1,
            alignment="right",
            hole_aware_h_shift=0.5,
        )
        # right = margin + available - rendered = 0.1 + 2.0 - 1.0 = 1.1
        assert result == pytest.approx(1.1)

    def test_unknown_alignment_centers_with_shift(self) -> None:
        """Unknown alignment defaults to center: applies shift."""
        result = compute_horizontal_offset(
            rendered_width=1.0,
            available_width=2.0,
            margin=0.1,
            alignment="unknown",
            hole_aware_h_shift=0.1,
        )
        assert result == pytest.approx(0.7)

    def test_shift_with_overwide_line(self) -> None:
        """Shift applied even when line is wider than available width."""
        # rendered_width=3.0 > available_width=2.0
        # natural center = 0.1 + (2.0 - 3.0)/2 = -0.4 (overflows left)
        # with shift +0.2 = -0.2 (still overflows, but less)
        result = compute_horizontal_offset(
            rendered_width=3.0,
            available_width=2.0,
            margin=0.1,
            alignment="center",
            hole_aware_h_shift=0.2,
        )
        assert result == pytest.approx(-0.2)


class TestConditionalHoleAwareCentering:
    """Test the adjust_h_center_for_single_side_hole conditional flag."""

    def test_h_shift_disabled_by_default(self) -> None:
        """Horizontal shift defaults to False: no h_shift computed."""
        from plt_optimizer.generate.resolution import _compute_hole_aware_shifts

        # Create a label with asymmetric left/right holes but flag disabled (default)
        resolved = ResolvedLabel(
            id="test",
            count=1,
            width=2.0,
            height=1.5,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
            holes=[
                ResolvedHoleSpec(location="left", diameter=0.2),
                ResolvedHoleSpec(location="right", diameter=0.1),
            ],
            content=[],
            adjust_h_center_for_single_side_hole=False,  # Explicitly False
        )
        v_shift, h_shift = _compute_hole_aware_shifts(resolved)
        assert v_shift == pytest.approx(0.0)
        assert h_shift == pytest.approx(0.0)  # NO shift even with asymmetric holes

    def test_h_shift_enabled_with_flag(self) -> None:
        """Horizontal shift enabled by flag: h_shift computed."""
        from plt_optimizer.generate.resolution import _compute_hole_aware_shifts

        # Create a label with asymmetric left/right holes and flag enabled
        resolved = ResolvedLabel(
            id="test",
            count=1,
            width=2.0,
            height=1.5,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
            holes=[
                ResolvedHoleSpec(location="left", diameter=0.2),
                ResolvedHoleSpec(location="right", diameter=0.1),
            ],
            content=[],
            adjust_h_center_for_single_side_hole=True,  # Enabled
        )
        v_shift, h_shift = _compute_hole_aware_shifts(resolved)
        assert v_shift == pytest.approx(0.0)
        assert h_shift == pytest.approx(0.05)  # (0.2 - 0.1) / 2

    def test_v_shift_always_applied(self) -> None:
        """Vertical shift always applied regardless of flag."""
        from plt_optimizer.generate.resolution import _compute_hole_aware_shifts

        # Test with flag disabled
        resolved_disabled = ResolvedLabel(
            id="test",
            count=1,
            width=2.0,
            height=1.5,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
            holes=[
                ResolvedHoleSpec(location="top", diameter=0.2),
                ResolvedHoleSpec(location="bottom", diameter=0.1),
            ],
            content=[],
            adjust_h_center_for_single_side_hole=False,  # Disabled
        )
        v_shift_disabled, h_shift_disabled = _compute_hole_aware_shifts(
            resolved_disabled
        )

        # Test with flag enabled
        resolved_enabled = ResolvedLabel(
            id="test",
            count=1,
            width=2.0,
            height=1.5,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
            holes=[
                ResolvedHoleSpec(location="top", diameter=0.2),
                ResolvedHoleSpec(location="bottom", diameter=0.1),
            ],
            content=[],
            adjust_h_center_for_single_side_hole=True,  # Enabled
        )
        v_shift_enabled, h_shift_enabled = _compute_hole_aware_shifts(resolved_enabled)

        # Vertical shift should be identical regardless of flag
        assert v_shift_disabled == pytest.approx(v_shift_enabled)
        assert v_shift_disabled == pytest.approx(0.05)  # (0.2 - 0.1) / 2


class TestFontRunResolution:
    """The <f:...> font run resolves its canonical name and its own ladder cutter."""

    @staticmethod
    def _job(*lines: TextLine, **job_kwargs: object) -> JobSpec:
        """Build a single-label Dino job over ``lines`` (inventory wired)."""
        job_kwargs.setdefault("font", "Dino")
        job_kwargs.setdefault("text_height", 0.5)
        return JobSpec(
            job_name="J",
            labels=[LabelSpec(id="lbl", width=2.0, height=1.0, content=list(lines))],
            **job_kwargs,
        )

    def test_font_run_resolves_to_the_canonical_name(self) -> None:
        """The tag argument canonicalizes through the registry at resolution."""
        job = self._job(TextLine(text="Hello <f:wingdings>abcd</f>"))
        line = resolve_job_spec(job, available_cutters=list(_INVENTORY))[0].content[0]
        assert line.plain_text == "Hello abcd"
        assert line.has_font_runs is True
        assert line.font_runs == (None,) * 6 + ("Wingdings",) * 4

    def test_font_run_sc_variant_canonicalizes_the_space(self) -> None:
        """``<f:Dino+SC>`` decodes the '+' and canonicalizes to 'Dino SC'."""
        job = self._job(TextLine(text="a<f:Dino+SC>b</f>"))
        line = resolve_job_spec(job, available_cutters=list(_INVENTORY))[0].content[0]
        assert line.font_runs == (None, "Dino SC")

    def test_font_run_steps_to_its_own_font_ladder(self) -> None:
        """A run engraves with the cutter its own font's ladder gives.

        Dino auto-selects 0.060 for 0.5in text; Wingdings (a 4-stroke font)
        re-runs the boldness ladder at 0.5in (0.0215 ideal -> 0.030 rung).
        """
        job = self._job(TextLine(text="A<f:Wingdings>B</f>"))
        line = resolve_job_spec(job, available_cutters=list(_INVENTORY))[0].content[0]
        assert line.cutter_diameter == pytest.approx(0.06)
        assert line.per_char_cutters[0] == pytest.approx(0.06)  # the plain 'A'
        assert line.per_char_cutters[1] == pytest.approx(0.03)  # the run's 'B'
        assert line.used_cutters == (0.03, 0.06)

    def test_font_run_baseline_raise_follows_the_cutter(self) -> None:
        """The run's engraved-baseline raise is (run_cutter - line_cutter)/2."""
        job = self._job(TextLine(text="A<f:Wingdings>B</f>"))
        line = resolve_job_spec(job, available_cutters=list(_INVENTORY))[0].content[0]
        assert line.baseline_raises[0] == pytest.approx(0.0)
        assert line.baseline_raises[1] == pytest.approx((0.03 - 0.06) / 2)

    def test_font_run_composes_with_scale(self) -> None:
        """A font run + <scale:N> resolves the run ladder at the scaled height."""
        job = self._job(TextLine(text="<f:Wingdings><scale:0.5>s</scale></f>"))
        line = resolve_job_spec(job, available_cutters=list(_INVENTORY))[0].content[0]
        # The run's font (Wingdings) at 0.25in: the S4 ladder -> 0.015 rung.
        assert line.font_runs == ("Wingdings",)
        assert line.scale_values == (0.5,)
        assert line.per_char_cutters[0] == pytest.approx(0.015)

    def test_font_run_composes_with_bold(self) -> None:
        """A bold font run takes the boosted ladder for its own font."""
        job = self._job(TextLine(text="<b>A<f:Wingdings>B</f></b>"))
        line = resolve_job_spec(job, available_cutters=list(_INVENTORY))[0].content[0]
        assert line.bold_cutter_diameter == pytest.approx(0.09)
        # The bold Wingdings run: the boosted S4 ladder -> 0.045 rung.
        assert line.per_char_cutters[0] == pytest.approx(0.09)  # bold plain
        assert line.per_char_cutters[1] == pytest.approx(0.045)  # bold run

    def test_font_run_respects_the_line_explicit_cutter_for_plain_runs(self) -> None:
        """An explicit cutter_size governs the plain runs; the run takes its ladder."""
        job = self._job(TextLine(text="A<f:Wingdings>B</f>", cutter_size=0.045))
        line = resolve_job_spec(job, available_cutters=list(_INVENTORY))[0].content[0]
        assert line.cutter_diameter == pytest.approx(0.045)
        assert line.per_char_cutters[0] == pytest.approx(0.045)
        assert line.per_char_cutters[1] == pytest.approx(0.03)

    def test_font_run_survives_the_with_markup_text_swap(self) -> None:
        """The run cutter DERIVES from the style, so a text swap keeps it.

        The <scale:N> staleness rule: a per-character array that must survive
        a text rewrite is derived from the style, never stored positionally.
        """
        job = self._job(TextLine(text="plain"))
        line = resolve_job_spec(job, available_cutters=list(_INVENTORY))[0].content[0]
        swapped = line.with_markup("A<f:Wingdings>B</f>")
        assert swapped.font_runs == (None, "Wingdings")
        assert swapped.per_char_cutters[0] == pytest.approx(0.06)
        assert swapped.per_char_cutters[1] == pytest.approx(0.03)

    def test_unknown_font_run_aborts_resolution(self) -> None:
        """An unknown run font is a loud MarkupError at resolution time."""
        from plt_optimizer.generate.markup import MarkupError

        with pytest.raises(MarkupError, match="NoSuchFont"):
            resolve_job_spec(
                self._job(TextLine(text="A<f:NoSuchFont>B</f>")),
                available_cutters=list(_INVENTORY),
            )

    def test_glyph_render_plan_includes_font_runs(self) -> None:
        """A font-only run (no scale/bold/sup) still produces a plan (not None)."""
        job = self._job(TextLine(text="A<f:Wingdings>B</f>"))
        line = resolve_job_spec(job, available_cutters=list(_INVENTORY))[0].content[0]
        plan = glyph_render_plan(line)
        assert plan is not None
        diameters, scales = plan
        assert diameters[1] == pytest.approx(0.03)
        # The run's centerline scale: (nominal - run_cutter)/(nominal - cutter).
        assert scales[1] == pytest.approx((0.5 - 0.03) / (0.5 - 0.06))
        assert scales[0] == pytest.approx(1.0)

    def test_markup_free_line_has_no_font_runs(self) -> None:
        """A tag-free line keeps the fast path (no active font, no plan)."""
        job = self._job(TextLine(text="plain"))
        line = resolve_job_spec(job, available_cutters=list(_INVENTORY))[0].content[0]
        assert line.has_font_runs is False
        # Every position falls back to the line's own font (None = unset).
        assert line.font_runs == (None,) * 5
        assert glyph_render_plan(line) is None
        # A resolved markup-free line carries one plain run, so the derived
        # cutters are all the line's own tool (a single-tool line: no split).
        assert line.per_char_cutters == (0.06,) * 5


class TestRunLadderCutter:
    """scale_cutter_ladder resolves a run font's own ladder (multi-stroke aware)."""

    @staticmethod
    def _line(**kwargs: object) -> ResolvedTextLine:
        """Build a resolved line carrying the ladder inputs."""
        kwargs.setdefault("scale_cutter_inventory", tuple(_INVENTORY))
        return ResolvedTextLine(
            text="x",
            nominal_text_height=0.5,
            toolpath_text_height=0.44,
            cutter_diameter=0.06,
            character_spacing=0.0,
            line_spacing=0.0,
            **kwargs,  # type: ignore[arg-type]
        )

    def test_run_ladder_uses_the_run_font_height(self) -> None:
        """A single-stroke run font takes the plain height ladder at scale."""
        line = self._line(font="Dino")
        plain, bold = scale_cutter_ladder(line, 1.0, need_bold=False, font="Dino", run_ladder=True)
        assert plain == pytest.approx(0.06)
        assert bold == pytest.approx(0.06)

    def test_run_ladder_multi_stroke_font_re_runs_boldness(self) -> None:
        """A multi-stroke (bold) run font re-runs the boldness ladder."""
        line = self._line(font="Dino")
        plain, _ = scale_cutter_ladder(
            line, 1.0, need_bold=False, font="Wingdings", run_ladder=True
        )
        # Wingdings S4 at 0.5in: 0.0215 ideal -> snapped 0.030 rung.
        assert plain == pytest.approx(0.03)

    def test_plain_ladder_defaults_to_the_line_font(self) -> None:
        """No font argument: the ladder resolves against the line's own font."""
        line = self._line(font="Dino")
        plain, _ = scale_cutter_ladder(line, 1.0, need_bold=False)
        assert plain == pytest.approx(0.06)


class TestHoleEngravedRadius:
    """hole_engraved_radius: the cutter-compensated emitted hole radius."""

    def test_adjust_shrinks_by_the_cutter_diameter(self) -> None:
        """A 0.1in hole with a 0.015in cutter emits a 0.085in circle."""
        radius = hole_engraved_radius(0.1, 0.015, True)
        assert 2.0 * radius == pytest.approx(0.085)

    def test_adjust_off_keeps_the_specified_radius(self) -> None:
        """adjust=False returns the historical diameter / 2 verbatim."""
        assert hole_engraved_radius(0.1, 0.015, False) == pytest.approx(0.05)

    def test_zero_cutter_is_a_no_op(self) -> None:
        """A zero cutter leaves the emitted diameter unchanged."""
        assert hole_engraved_radius(0.125, 0.0, True) == pytest.approx(0.0625)

    def test_degenerate_hole_clamps_to_the_emittable_floor(self) -> None:
        """A hole at/below the cutter clamps to one plotter unit radius."""
        for diameter in (0.01, 0.015, 0.0):
            radius = hole_engraved_radius(diameter, 0.015, True)
            assert radius == pytest.approx(MIN_HOLE_EMITTED_RADIUS_INCHES)

    def test_floor_is_one_plotter_unit(self) -> None:
        """The floor is ONE unit: a half-unit radius rounds to 0 in the writer."""
        from plt_optimizer.core.units import inches_to_units

        assert MIN_HOLE_EMITTED_RADIUS_INCHES == pytest.approx(1.0 / 1016.0)
        assert inches_to_units(MIN_HOLE_EMITTED_RADIUS_INCHES) == 1

    def test_just_above_the_floor_is_not_clamped(self) -> None:
        """A diameter barely above cutter + floor keeps its exact difference."""
        diameter = 0.015 + 3.0 * MIN_HOLE_EMITTED_RADIUS_INCHES
        radius = hole_engraved_radius(diameter, 0.015, True)
        assert radius == pytest.approx(1.5 * MIN_HOLE_EMITTED_RADIUS_INCHES)


class TestHoleAdjustForCutterCascade:
    """hole_adjust_for_cutter cascades label -> job -> True (explicit-None)."""

    @staticmethod
    def _job(**levels: bool | None) -> JobSpec:
        """Build a one-label job applying hole_adjust_for_cutter at the given levels."""
        job_kwargs: dict[str, bool | None] = {}
        label_kwargs: dict[str, bool | None] = {}
        if "job" in levels:
            job_kwargs["hole_adjust_for_cutter"] = levels["job"]
        if "label" in levels:
            label_kwargs["hole_adjust_for_cutter"] = levels["label"]
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
                    content=[TextLine(text="X")],
                )
            ],
        )

    def test_default_module_constant_is_true(self) -> None:
        """The shipped default enables cutter compensation."""
        assert DEFAULT_HOLE_ADJUST_FOR_CUTTER is True

    def test_default_resolves_true(self) -> None:
        """All levels omitting resolves to the True fallback."""
        assert resolve_job_spec(self._job())[0].hole_adjust_for_cutter is True

    def test_job_false_used_when_label_omits(self) -> None:
        """Job-level False cascades onto labels that omit the field."""
        assert resolve_job_spec(self._job(job=False))[0].hole_adjust_for_cutter is False

    def test_label_overrides_job(self) -> None:
        """Label-level True beats the job-level False."""
        assert resolve_job_spec(self._job(job=False, label=True))[0].hole_adjust_for_cutter is True

    def test_label_false_is_honored(self) -> None:
        """An intentional False (specified diameter emitted) never falls through."""
        assert resolve_job_spec(self._job(job=True, label=False))[0].hole_adjust_for_cutter is False

    def test_root_level_job_resolves_own_value(self) -> None:
        """A root-level job masquerading as a label resolves its own field."""
        job = JobSpec(
            job_name="J",
            width=2.0,
            height=1.0,
            text_height=0.5,
            content=[TextLine(text="X")],
            hole_adjust_for_cutter=False,
        )
        assert resolve_job_spec(job)[0].hole_adjust_for_cutter is False

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
        assert label.hole_adjust_for_cutter is DEFAULT_HOLE_ADJUST_FOR_CUTTER

    def test_hole_diameters_stay_nominal(self) -> None:
        """Resolution keeps the SPECIFIED diameters; the renderer shrinks the radius."""
        job = JobSpec(
            job_name="J",
            width=2.0,
            height=1.0,
            text_height=0.5,
            holes=[{"location": "left", "diameter": 0.1}],
            content=[TextLine(text="X")],
        )
        label = resolve_job_spec(job, boundary_hole_cutter_size=0.015)[0]
        assert label.holes[0].diameter == pytest.approx(0.1)
        assert label.hole_adjust_for_cutter is True
