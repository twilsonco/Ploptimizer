"""Unit tests for text-hole collision resolution (Phases 2 and 3).

Phase 2 reduces ``hole_margin`` toward ``min_hole_margin``; Phase 3
compresses text horizontally within the ``max_h_compress`` budget. These
tests drive ``render_label_to_plt`` end-to-end and assert on the adjusted
``source_label`` returned inside each ``RenderedLabel``.

Fixture geometry (verified against the bundled ReliefSingleLine font):
a 3x1in label with 0.1in margin, 0.5in text (0.47in toolpath), and 0.25in
holes at the default 0.1875in hole margin. Vertically centered text spans
y ~= [0.265, 0.735]:

- A ``bottom`` hole's circle top sits at ``hole_margin + 0.25``, so the
  collision clears once ``hole_margin`` drops below ~0.015 -- fixable by
  Phase 2 but NOT by horizontal compression.
- ``left``/``right`` holes are overlapped by any full-width line; centered
  text clears them only by narrowing -- fixable by Phase 3 but NOT by
  margin reduction (the circles stay inside the text box at any margin).
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from plt_optimizer.generate.collision.detection import (
    LabelRenderError,
    assert_no_collisions,
    collision_threshold,
    detect_text_hole_collisions,
    flush_siblings_to_switched_edge,
    format_unresolvable_collision,
    resolve_via_compression,
    resolve_via_edge_margin,
    resolve_via_margin_adjustment,
)
from plt_optimizer.generate.label_renderer import (
    _render_text_local_with_bounds,
    render_label_to_plt,
)
from plt_optimizer.generate.resolution import (
    ResolvedHoleSpec,
    ResolvedLabel,
    ResolvedTextLine,
)

LOGGER_NAME = "plt_optimizer.generate.collision.detection"

BOTTOM_HOLE = [ResolvedHoleSpec(diameter=0.25, location="bottom")]
SIDE_HOLES = [
    ResolvedHoleSpec(diameter=0.25, location="left"),
    ResolvedHoleSpec(diameter=0.25, location="right"),
]


def _make_line(text: str, height: float = 0.5, max_h_compress: float = 0.0) -> ResolvedTextLine:
    """Build a cutter-compensated ResolvedTextLine."""
    cutter_dia = 0.03
    return ResolvedTextLine(
        text=text,
        nominal_text_height=height,
        toolpath_text_height=height - cutter_dia,
        cutter_diameter=cutter_dia,
        character_spacing=0.0,
        line_spacing=0.0,
        max_h_compress=max_h_compress,
    )


def _label(
    text: str = "WIDE LABEL TEXT",
    holes: list[ResolvedHoleSpec] | None = None,
    width: float = 3.0,
    height: float = 1.0,
    margin: float = 0.1,
    hole_margin: float = 0.1875,
    min_hole_margin: float | None = None,
    max_h_compress: float = 0.0,
    hole_text_collision_distance: float = 0.0,
    hole_cutter_diameter: float = 0.0,
) -> ResolvedLabel:
    """Build a label whose centered text overlaps the given drill holes.

    The collision distance and hole cutter default to ``0.0`` so the
    margin/compression mechanics exercised here run under strict
    penetration semantics, independently of the production 0.15in
    tolerance. :class:`TestStrokeAwareThreshold` opts into non-zero values
    to cover the stroke floor.
    """
    if holes is None:
        holes = SIDE_HOLES
    return ResolvedLabel(
        id="resolve_label",
        count=1,
        width=width,
        height=height,
        margin=margin,
        h_margin=margin,
        v_margin=margin,
        hole_margin=hole_margin,
        holes=holes,
        content=[_make_line(text, max_h_compress=max_h_compress)],
        min_hole_margin=min_hole_margin,
        hole_text_collision_distance=hole_text_collision_distance,
        hole_cutter_diameter=hole_cutter_diameter,
    )


class TestMarginAdjustment:
    """Phase 2: hole-margin reduction toward min_hole_margin."""

    def test_margin_reduction_resolves_collision(self) -> None:
        """A bottom hole clearable at a smaller margin resolves via Phase 2."""
        label = _label(text="HELLO", holes=BOTTOM_HOLE, min_hole_margin=0.0)
        rendered = render_label_to_plt(label)

        assert rendered.has_collisions is False
        assert rendered.collision_detected is True
        adjusted = rendered.source_label
        assert adjusted is not label
        assert 0.0 <= adjusted.hole_margin < label.hole_margin
        # Margin-only avoidance leaves the text at natural width: the
        # effective compression report stays empty.
        assert rendered.compression_by_line == {}
        # The emitted PLT must come from the adjusted label: re-detecting
        # collisions on the adjusted geometry finds none.
        _lc, entries = _render_text_local_with_bounds(adjusted)
        assert detect_text_hole_collisions(adjusted, entries) == []

    def test_margin_adjustment_respects_min_hole_margin_floor(self) -> None:
        """The reduced margin never drops below the configured floor."""
        # 1.25in label: gap = 0.14 - hole_margin, so the collision
        # clears around 0.125in -- above a 0.1in floor but below the
        # default 0.1875in margin.
        label = _label(text="HELLO", holes=BOTTOM_HOLE, height=1.25, min_hole_margin=0.1)
        _lc, entries = _render_text_local_with_bounds(label)
        resolved = resolve_via_margin_adjustment(label, entries)

        assert resolved is not None
        assert resolved.hole_margin >= 0.1 - 1e-9
        assert resolved.hole_margin < label.hole_margin

    def test_floor_too_high_blocks_resolution(self) -> None:
        """A floor above the clearing margin leaves the collision in place."""
        label = _label(text="HELLO", holes=BOTTOM_HOLE, min_hole_margin=0.16)
        _lc, entries = _render_text_local_with_bounds(label)
        assert resolve_via_margin_adjustment(label, entries) is None

        rendered = render_label_to_plt(label)
        assert rendered.has_collisions is True

    def test_no_floor_configured_skips_phase2(self) -> None:
        """Without min_hole_margin, margin adjustment is not attempted."""
        label = _label(text="HELLO", holes=BOTTOM_HOLE, min_hole_margin=None)
        _lc, entries = _render_text_local_with_bounds(label)
        assert resolve_via_margin_adjustment(label, entries) is None

    def test_no_budget_when_margin_at_floor(self) -> None:
        """hole_margin already at the floor leaves nothing to shrink."""
        label = _label(text="HELLO", holes=BOTTOM_HOLE, hole_margin=0.05, min_hole_margin=0.05)
        _lc, entries = _render_text_local_with_bounds(label)
        assert resolve_via_margin_adjustment(label, entries) is None

    def test_adjustment_logs_before_and_after_margins(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A successful adjustment logs WARNING with both margin values."""
        label = _label(text="HELLO", holes=BOTTOM_HOLE, min_hole_margin=0.0)
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            rendered = render_label_to_plt(label)

        messages = [r.message for r in caplog.records if "adjusted hole_margin" in r.message]
        assert messages, "No WARNING logged for margin adjustment"
        assert "from 0.1875in" in messages[0]
        assert f"to {rendered.source_label.hole_margin:.4f}in" in messages[0]

    def test_side_holes_not_fixable_by_margin(self) -> None:
        """Full-width centered text over side holes ignores margin reduction."""
        label = _label(min_hole_margin=0.0)
        _lc, entries = _render_text_local_with_bounds(label)
        assert resolve_via_margin_adjustment(label, entries) is None

    def test_margin_already_at_floor_flags_collision(self) -> None:
        """With hole_margin pinned at the floor, Phase 2 has no budget to shrink."""
        label = _label(text="HELLO", holes=BOTTOM_HOLE, hole_margin=0.05, min_hole_margin=0.05)
        rendered = render_label_to_plt(label)
        assert rendered.has_collisions is True


class TestCompressionFallback:
    """Phase 3: horizontal compression when margins cannot help."""

    def test_compression_resolves_side_hole_collision(self) -> None:
        """Ample max_h_compress narrows centered text clear of side holes."""
        label = _label(max_h_compress=0.7)
        rendered = render_label_to_plt(label)

        assert rendered.has_collisions is False
        assert rendered.collision_detected is True
        adjusted = rendered.source_label
        # Per-line compression is now stored in collision_compress_by_line dict
        assert len(adjusted.collision_compress_by_line) > 0
        # All compressed lines should respect the budget floor (1 - 0.7).
        floor = 1.0 - 0.7 - 1e-9
        for scale in adjusted.collision_compress_by_line.values():
            assert scale >= floor
        # The render reports the *effective* per-line scale: the collision
        # scale (no margin compression applies here, the line fits the
        # margin box) for exactly the colliding lines.
        assert set(rendered.compression_by_line) == set(adjusted.collision_compress_by_line)
        for line_index, scale in rendered.compression_by_line.items():
            assert scale == pytest.approx(adjusted.collision_compress_by_line[line_index])
            assert 0.0 < scale < 1.0

    def test_compression_respects_max_h_compress_limit(self) -> None:
        """A budget smaller than required cannot resolve the collision.

        Without ``min_hole_margin`` (the dedicated collision-avoidance
        opt-in) the render degrades to log-only instead of aborting, since
        ``max_h_compress`` is a shared margin-fitting knob.
        """
        label = _label(max_h_compress=0.05)
        rendered = render_label_to_plt(label)
        assert rendered.has_collisions is True
        # The unmodified render is returned: no partial compression is kept.
        assert rendered.source_label is label

    def test_compression_only_failure_never_raises(self) -> None:
        """Compression failure without min_hole_margin degrades to a warning."""
        label = _label(max_h_compress=0.0)
        # No exception even though the collision is unfixable by compression.
        render_label_to_plt(label)

    def test_compression_skipped_when_budget_disabled(self) -> None:
        """max_h_compress=0.0 on every line skips Phase 3 entirely."""
        label = _label(max_h_compress=0.0)
        rendered = render_label_to_plt(label)
        # Avoidance disabled: logged-only, unmodified render.
        assert rendered.source_label is label
        assert rendered.has_collisions is True

    def test_compression_stacks_on_top_of_margin_floor(self) -> None:
        """With both phases enabled, compression applies on top of the floor."""
        label = _label(
            holes=[
                ResolvedHoleSpec(diameter=0.25, location="bottom"),
                ResolvedHoleSpec(diameter=0.25, location="left"),
            ],
            min_hole_margin=0.0,
            max_h_compress=0.7,
        )
        rendered = render_label_to_plt(label)
        adjusted = rendered.source_label
        # The bottom hole clears only by shrinking the margin to the floor;
        # the left hole (text spans the full inner width at scale 1.0)
        # additionally requires compression. Both must have been applied.
        assert adjusted.hole_margin < 0.1875
        # Per-line compression should have been applied to the colliding line
        assert len(adjusted.collision_compress_by_line) > 0
        # At least one line should be compressed below 1.0
        assert any(scale < 1.0 for scale in adjusted.collision_compress_by_line.values())
        assert rendered.has_collisions is False
        assert rendered.collision_detected is True

    def test_compression_helper_returns_none_at_floor(self) -> None:
        """A label already at the compression floor cannot compress more."""
        label = _label(max_h_compress=1.0)
        # At floor means collision_compress_by_line already has all lines at floor
        at_floor = replace(label, collision_compress_by_line={0: 0.0})
        # No collisions detected (empty list returned), so compression returns None
        # because there are no collisions to resolve
        result = resolve_via_compression(at_floor, 1.0, render=_render_text_local_with_bounds)
        # The function returns None when there are no collisions to resolve
        # (since we're testing with a pre-compressed label that has no collisions)
        assert result is None

    def test_unresolvable_error_lists_diagnostic_information(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The ERROR log names margins, compression, and recommendations."""
        label = _label(max_h_compress=0.05, min_hole_margin=0.15)
        with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
            rendered = render_label_to_plt(label)

        assert rendered.has_collisions is True
        errors = [
            r
            for r in caplog.records
            if r.levelno >= logging.ERROR and "cannot be resolved" in r.getMessage()
        ]
        assert errors, "No ERROR logged for unresolvable collision"
        message = errors[0].getMessage()
        assert "resolve_label" in message
        assert "Hole margin" in message
        assert "min: 0.15" in message
        assert "max_h_compress=0.05" in message
        assert "clearance shortfall" in message
        assert "Recommendations" in message

    def test_avoidance_disabled_logs_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """A collision with no avoidance knobs configured is still an ERROR."""
        label = _label(min_hole_margin=None, max_h_compress=0.0)
        with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
            rendered = render_label_to_plt(label)

        assert rendered.has_collisions is True
        assert any(r.levelno >= logging.ERROR for r in caplog.records)


class TestJobLevelAbort:
    """assert_no_collisions aborts the job after all labels rendered."""

    def test_clean_labels_pass(self) -> None:
        """A job whose labels all render clean does not abort."""
        label = _label(text="HI", holes=[])
        rendered = render_label_to_plt(label)
        assert_no_collisions([rendered])

    def test_colliding_label_aborts_job(self) -> None:
        """One colliding label aborts the whole job with its id named."""
        label = _label()  # full-width text over side holes, no avoidance
        rendered = render_label_to_plt(label)
        with pytest.raises(LabelRenderError) as exc_info:
            assert_no_collisions([rendered])
        assert "resolve_label" in str(exc_info.value)
        assert "jobspec must be revised" in str(exc_info.value)

    def test_avoidance_repaired_collision_proceeds(self) -> None:
        """A collision repaired by avoidance does not abort the job."""
        label = _label(text="HELLO", holes=BOTTOM_HOLE, min_hole_margin=0.0)
        rendered = render_label_to_plt(label)
        # Avoidance succeeded (final render is clean): the collision is
        # recorded observationally but the job proceeds.
        assert rendered.has_collisions is False
        assert rendered.collision_detected is True
        assert_no_collisions([rendered])

    def test_repaired_collision_logs_warning_not_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Avoidance-resolved collisions log WARNING, never ERROR."""
        label = _label(text="HELLO", holes=BOTTOM_HOLE, min_hole_margin=0.0)
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            rendered = render_label_to_plt(label)

        assert rendered.has_collisions is False
        collision_records = [
            r for r in caplog.records if "collides with" in r.getMessage()
        ]
        assert collision_records, "No collision detection logged"
        assert all(r.levelno == logging.WARNING for r in collision_records)
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)

    def test_abort_names_each_offending_label_once(self) -> None:
        """Duplicate renders of the same label are reported once."""
        label = _label()
        rendered = render_label_to_plt(label)
        with pytest.raises(LabelRenderError) as exc_info:
            assert_no_collisions([rendered, rendered])
        message = str(exc_info.value)
        assert "1 label(s)" in message

    def test_layout_gate_aborts_before_packing(self) -> None:
        """generate_layout_with_bounds aborts when any label collides."""
        from plt_optimizer.generate.packing.layout import generate_layout_with_bounds

        clean = _label(text="HI", holes=[], width=2.0)
        colliding = replace(_label(), id="bad_label")
        with pytest.raises(LabelRenderError) as exc_info:
            generate_layout_with_bounds([clean, colliding])
        assert "bad_label" in str(exc_info.value)


class TestStrokeAwareThreshold:
    """Collision threshold = stroke floor + hole_text_collision_distance.

    The bottom-hole fixture (3x1in, 0.5in text, 0.25in hole) has a
    geometric gap of exactly ``0.015 - hole_margin``: at ``hole_margin =
    0.015`` the paths are tangent, below it they overlap. A taller 1.5in
    label keeps the text higher, giving positive headroom to exercise
    near-miss detection.
    """

    def test_near_miss_below_threshold_is_detected(self) -> None:
        """A positive gap under the required clearance still collides."""
        # 1.5in label: geometric gap 0.0775in at hole_margin 0.1875 -- no
        # overlap, but below 0.15in clearance + 0.0225in stroke floor.
        label = _label(
            text="HELLO",
            holes=BOTTOM_HOLE,
            height=1.5,
            hole_text_collision_distance=0.15,
            hole_cutter_diameter=0.015,
        )
        _lc, entries = _render_text_local_with_bounds(label)
        collisions = detect_text_hole_collisions(label, entries)
        assert len(collisions) == 1
        assert collisions[0].gap > 0.0  # near miss, not penetration
        assert collisions[0].gap < collision_threshold(label, 0)

        rendered = render_label_to_plt(label)
        assert rendered.collision_detected is True

    def test_gap_above_threshold_is_safe(self) -> None:
        """A gap at or above the required clearance is not a collision."""
        # 1.25in label: gap = 0.14 - hole_margin = 0.04 at margin 0.1.
        # The stroke floor is 0.015 (text cutter 0.03, hole cutter 0.0).
        base = {
            "text": "HELLO",
            "holes": BOTTOM_HOLE,
            "height": 1.25,
            "hole_margin": 0.1,
            "hole_cutter_diameter": 0.0,
        }
        # Required 0.015 + 0.02 = 0.035 < gap -> safe.
        safe = _label(hole_text_collision_distance=0.02, **base)
        _lc, entries = _render_text_local_with_bounds(safe)
        assert detect_text_hole_collisions(safe, entries) == []

        # Required 0.015 + 0.04 = 0.055 > gap -> collision (near miss).
        tight = _label(hole_text_collision_distance=0.04, **base)
        assert len(detect_text_hole_collisions(tight, entries)) == 1

    def test_tangent_paths_collide_under_stroke_floor(self) -> None:
        """Tangent toolpaths still collide: the cut strokes would overlap."""
        # hole_margin 0.015 -> geometric gap exactly 0.0 (tangency). The
        # text cutter alone (0.03) puts the stroke floor at 0.015in, so
        # even with a zero-width hole cutter the engraved strokes bleed.
        label = _label(
            text="HELLO",
            holes=BOTTOM_HOLE,
            hole_margin=0.015,
            hole_text_collision_distance=0.0,
            hole_cutter_diameter=0.0,
        )
        _lc, entries = _render_text_local_with_bounds(label)
        assert len(detect_text_hole_collisions(label, entries)) == 1

        # A zero-threshold label (no stroke floor at all) keeps the legacy
        # semantics: tangency alone is not a collision.
        zero_cutter = replace(
            label,
            content=[replace(_make_line("HELLO"), cutter_diameter=0.0)],
        )
        _lc2, zero_entries = _render_text_local_with_bounds(zero_cutter)
        assert detect_text_hole_collisions(zero_cutter, zero_entries) == []

    def test_threshold_uses_per_line_text_cutter(self) -> None:
        """Each line's own cutter diameter feeds the stroke floor."""
        label = _label(hole_text_collision_distance=0.15, hole_cutter_diameter=0.015)
        big_cutter_line = replace(_make_line("B"), cutter_diameter=0.06)
        wide = replace(label, content=[_make_line("A"), big_cutter_line])
        base = collision_threshold(wide, 0)
        assert base == pytest.approx(0.5 * (0.015 + 0.03) + 0.15)
        # The second line's larger cutter raises its threshold.
        assert collision_threshold(wide, 1) == pytest.approx(0.5 * (0.015 + 0.06) + 0.15)

    def test_phase2_sweep_stops_at_stroke_aware_threshold(self) -> None:
        """Margin reduction must clear the floor, not just tangency."""
        # 1.25in label: gap = 0.14 - hole_margin, colliding at the
        # default 0.1875in margin under both strict and aware thresholds.
        kwargs = {
            "text": "HELLO",
            "holes": BOTTOM_HOLE,
            "height": 1.25,
            "min_hole_margin": 0.0,
        }
        strict = _label(**kwargs)
        stroke_aware = _label(
            hole_text_collision_distance=0.05,
            hole_cutter_diameter=0.015,
            **kwargs,
        )

        _lc, entries = _render_text_local_with_bounds(strict)
        strict_resolved = resolve_via_margin_adjustment(strict, entries)
        _lc2, entries2 = _render_text_local_with_bounds(stroke_aware)
        aware_resolved = resolve_via_margin_adjustment(stroke_aware, entries2)

        assert strict_resolved is not None
        assert aware_resolved is not None
        # The stroke floor demands more separation: the sweep must end at
        # a strictly smaller hole margin than strict penetration requires.
        assert aware_resolved.hole_margin < strict_resolved.hole_margin
        # And the resolved geometry genuinely clears the threshold.
        _lc3, aware_entries = _render_text_local_with_bounds(aware_resolved)
        assert detect_text_hole_collisions(aware_resolved, aware_entries) == []

    def test_unresolved_collision_logs_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Collisions that survive every phase keep ERROR severity."""
        label = _label()  # side holes, no avoidance knobs -> unresolvable
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            rendered = render_label_to_plt(label)

        assert rendered.has_collisions is True
        collision_records = [
            r for r in caplog.records if "collides with" in r.getMessage()
        ]
        assert collision_records
        assert all(r.levelno >= logging.ERROR for r in collision_records)

    def test_error_log_reports_threshold_breakdown(self, caplog: pytest.LogCaptureFixture) -> None:
        """Collision ERRORs break the threshold into clearance + floor."""
        label = _label(
            text="HELLO",
            holes=BOTTOM_HOLE,
            height=1.5,
            hole_text_collision_distance=0.15,
            hole_cutter_diameter=0.015,
        )
        with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
            render_label_to_plt(label)

        messages = [r.getMessage() for r in caplog.records if "collides with" in r.getMessage()]
        assert messages
        assert "is below the required 0.1725in" in messages[0]
        assert "0.1500in clearance + 0.0225in stroke floor" in messages[0]

    def test_dataclass_defaults(self) -> None:
        """Manually built labels default to 0.15in gap and 0.015in cutter."""
        label = ResolvedLabel(
            id="defaults", count=1, width=3.0, height=1.0, margin=0.1,
            h_margin=0.1, v_margin=0.1
        )
        assert label.hole_text_collision_distance == pytest.approx(0.15)
        assert label.hole_cutter_diameter == pytest.approx(0.015)


class TestHoleAwareShiftInCollisionDetection:
    """hole_aware_vertical_shift_inches shifts the text check position.

    A top-only hole shifts text DOWN (positive shift), so the hole-aware
    shift must be subtracted from the text position when checking for
    collisions -- mirroring _center_text_layer_vertically exactly.
    Without the fix, text appears at height/2 (too high), causing false
    positives for top holes and missed collisions for bottom holes.
    """

    def test_shift_moves_text_away_from_top_hole(self) -> None:
        """Positive shift (top-only hole) moves text down; top-hole gap widens."""
        # 3x1in label, top hole, text centered at y=0.5 without shift.
        # A 0.2in shift moves text center to y=0.3 -- further from the top hole.
        top_hole = [ResolvedHoleSpec(diameter=0.4, location="top")]
        base = _label(text="HI", holes=top_hole, height=1.0,
                      hole_margin=0.05, hole_text_collision_distance=0.0,
                      hole_cutter_diameter=0.0)
        _lc, entries = _render_text_local_with_bounds(base)

        # Without shift: text is near the top hole -> collision detected.
        assert len(detect_text_hole_collisions(base, entries)) >= 1

        # With a 0.2in downward shift: text is lower -> gap to top hole is larger.
        shifted = replace(base, hole_aware_vertical_shift_inches=0.2)
        assert detect_text_hole_collisions(shifted, entries) == []

    def test_negative_shift_moves_text_away_from_bottom_hole(self) -> None:
        """Negative shift (bottom-only hole) moves text up; bottom-hole gap widens."""
        bottom_hole = [ResolvedHoleSpec(diameter=0.4, location="bottom")]
        base = _label(text="HI", holes=bottom_hole, height=1.0,
                      hole_margin=0.0, hole_text_collision_distance=0.0,
                      hole_cutter_diameter=0.0)
        _lc, entries = _render_text_local_with_bounds(base)

        assert len(detect_text_hole_collisions(base, entries)) >= 1

        shifted = replace(base, hole_aware_vertical_shift_inches=-0.2)
        assert detect_text_hole_collisions(shifted, entries) == []


class TestKerfBufferThreshold:
    """The collision stroke floor measures ENGRAVED widths, not nominal tools."""

    def test_text_buffer_widens_the_floor(self) -> None:
        """A line's cutter_buffer_text raises its own threshold."""
        label = _label(hole_text_collision_distance=0.15, hole_cutter_diameter=0.015)
        plain = replace(label, content=[replace(_make_line("A"), cutter_buffer=0.0)])
        buffered = replace(label, content=[replace(_make_line("A"), cutter_buffer=0.01)])
        # 0.5 * (0.015 + 0.03) + 0.15 vs 0.5 * (0.015 + 0.04) + 0.15
        assert collision_threshold(plain, 0) == pytest.approx(0.5 * (0.015 + 0.03) + 0.15)
        assert collision_threshold(buffered, 0) == pytest.approx(0.5 * (0.015 + 0.04) + 0.15)

    def test_routing_buffer_widens_the_floor(self) -> None:
        """The boundary/hole buffer feeds the same floor as the text buffer."""
        label = _label(hole_text_collision_distance=0.15, hole_cutter_diameter=0.015)
        buffered = replace(label, hole_cutter_buffer=0.045)
        assert collision_threshold(buffered, 0) == pytest.approx(0.5 * (0.06 + 0.03) + 0.15)

    def test_zero_buffers_reproduce_the_legacy_floor(self) -> None:
        """0.0 on both tiers is the identity (the historical geometry)."""
        label = _label(
            hole_text_collision_distance=0.15,
            hole_cutter_diameter=0.015,
        )
        legacy = replace(label, hole_cutter_buffer=0.0)
        legacy = replace(
            legacy, content=[replace(line, cutter_buffer=0.0) for line in legacy.content]
        )
        assert collision_threshold(label, 0) == pytest.approx(collision_threshold(legacy, 0))

    def test_widest_engraved_rung_sets_the_floor(self) -> None:
        """A <b> line's floor uses its widest ENGRAVED width, not the plain tool."""
        from plt_optimizer.generate.markup import PLAIN, Run, Style

        label = _label(hole_text_collision_distance=0.0, hole_cutter_diameter=0.0)
        line = replace(
            _make_line("aB"),
            cutter_diameter=0.03,
            bold_cutter_diameter=0.045,
            cutter_buffer=0.01,
            plain_text="aB",
            styled_runs=(Run("a", PLAIN), Run("B", Style(bold=True))),
        )
        assert line.used_cutters == pytest.approx((0.03, 0.045))
        assert line.effective_max_cutter_diameter == pytest.approx(0.055)
        buffered = replace(label, content=[line])
        # 0.5 * (hole 0.0 + widest engraved width 0.055)
        assert collision_threshold(buffered, 0) == pytest.approx(0.5 * 0.055)

    def test_buffer_creates_a_collision_the_legacy_floor_misses(self) -> None:
        """A gap that clears the nominal floor can bleed under the engraved one."""
        # 0.25in holes at a margin leaving a gap between the legacy floor
        # (0.5 * (0.015 + 0.03) = 0.0225) and the buffered floor
        # (0.5 * (0.06 + 0.04) = 0.05).
        kwargs = {
            "text": "HELLO",
            "holes": BOTTOM_HOLE,
            "height": 1.25,
            "hole_margin": 0.10,
            "hole_text_collision_distance": 0.0,
            "hole_cutter_diameter": 0.015,
        }
        legacy = replace(
            _label(**kwargs), content=[replace(_make_line("HELLO"), cutter_buffer=0.0)]
        )
        _lc, legacy_entries = _render_text_local_with_bounds(legacy)
        assert detect_text_hole_collisions(legacy, legacy_entries) == []

        buffered = replace(
            _label(**kwargs),
            hole_cutter_buffer=0.045,
            content=[replace(_make_line("HELLO"), cutter_buffer=0.01)],
        )
        _lc, buffered_entries = _render_text_local_with_bounds(buffered)
        assert len(detect_text_hole_collisions(buffered, buffered_entries)) >= 1


LEFT_HOLE = [ResolvedHoleSpec(diameter=0.25, location="left")]
RIGHT_HOLE = [ResolvedHoleSpec(diameter=0.25, location="right")]
BOTTOM_HOLE_ONLY = [ResolvedHoleSpec(diameter=0.25, location="bottom")]
TOP_LEFT_HOLE = [ResolvedHoleSpec(diameter=0.25, location="top-left")]
BOTTOM_LEFT_HOLE = [ResolvedHoleSpec(diameter=0.25, location="bottom-left")]


def _edge_line(
    text: str,
    alignment: str = "left",
    height: float = 0.4,
    max_h_compress: float = 0.3,
) -> ResolvedTextLine:
    """Build a cutter-compensated line with an explicit horizontal alignment."""
    cutter_dia = 0.03
    return ResolvedTextLine(
        text=text,
        nominal_text_height=height,
        toolpath_text_height=height - cutter_dia,
        cutter_diameter=cutter_dia,
        character_spacing=0.0,
        line_spacing=0.0,
        max_h_compress=max_h_compress,
        text_h_alignment=alignment,
    )


def _edge_label(
    texts: list[str],
    alignments: list[str],
    holes: list[ResolvedHoleSpec],
    width: float = 4.0,
    max_h_compress: float = 0.3,
    heights: list[float] | None = None,
    **overrides: object,
) -> ResolvedLabel:
    """Build a 4x1in label whose edge-aligned text overlaps a near-side hole.

    The 0.4in ``SERIAL 42`` line spans x ~= [0.1, 2.534] on a 4in label with
    0.1in margins; a 0.25in ``left`` hole at the default 0.1875in hole margin
    (circle x = [0.1875, 0.4375]) sits under its anchored end. Compression
    cannot free it (the anchored edge stays pinned), so the fix is the
    Phase-3 edge-margin slide: +0.3525in clears the stroke floor exactly.
    """
    line_heights = heights or [0.4] * len(texts)
    return ResolvedLabel(
        id="edge_label",
        count=1,
        width=width,
        height=1.0,
        margin=0.1,
        h_margin=0.1,
        v_margin=0.1,
        hole_margin=0.1875,
        holes=holes,
        content=[
            _edge_line(text, alignment, height, max_h_compress)
            for text, alignment, height in zip(texts, alignments, line_heights)
        ],
        hole_text_collision_distance=0.0,
        hole_cutter_diameter=0.0,
        **overrides,  # type: ignore[arg-type]
    )


class TestEdgeMarginSlide:
    """Phase 3: slide edge-aligned text off a hole on its anchored side."""

    def test_left_aligned_line_slides_off_left_hole(self) -> None:
        """A left-aligned line clears a left hole by increasing its left margin."""
        label = _edge_label(["SERIAL 42"], ["left"], LEFT_HOLE)
        rendered = render_label_to_plt(label)

        assert rendered.has_collisions is False
        assert rendered.collision_detected is True
        adjusted = rendered.source_label
        assert adjusted.collision_edge_margin_by_line == {0: pytest.approx(0.3525)}
        # The slide is the whole fix: no compression, no alignment switch,
        # and the hole margin is untouched.
        assert rendered.compression_by_line == {}
        assert adjusted.collision_center_by_line == frozenset()
        assert adjusted.hole_margin == pytest.approx(label.hole_margin)
        # The emitted geometry really is collision-free.
        _lc, entries = _render_text_local_with_bounds(adjusted)
        assert detect_text_hole_collisions(adjusted, entries) == []
        # The line moved right; its width is untouched (a rigid slide).
        _lc, before = _render_text_local_with_bounds(label)
        assert entries[0].bounds[0] == pytest.approx(before[0].bounds[0] + 0.3525)
        assert (entries[0].bounds[2] - entries[0].bounds[0]) == pytest.approx(
            before[0].bounds[2] - before[0].bounds[0]
        )

    def test_right_aligned_line_slides_off_right_hole(self) -> None:
        """The mirror case: a right-aligned line slides leftward off a right hole."""
        label = _edge_label(["SERIAL 42"], ["right"], RIGHT_HOLE)
        rendered = render_label_to_plt(label)

        assert rendered.has_collisions is False
        assert rendered.source_label.collision_edge_margin_by_line == {0: pytest.approx(0.3525)}
        _lc, entries = _render_text_local_with_bounds(rendered.source_label)
        assert detect_text_hole_collisions(rendered.source_label, entries) == []
        # Anchored on the right: the line's right edge moved inward.
        _lc, before = _render_text_local_with_bounds(label)
        assert entries[0].bounds[2] == pytest.approx(before[0].bounds[2] - 0.3525)

    def test_slide_logs_warning_naming_side_and_distance(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The avoidance action logs at WARNING with the direction and margin."""
        label = _edge_label(["SERIAL 42"], ["left"], LEFT_HOLE)
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            render_label_to_plt(label)

        messages = [r.getMessage() for r in caplog.records if "edge margin" in r.getMessage()]
        assert messages
        assert "edge_label" in messages[0]
        assert "left-aligned" in messages[0]
        assert "0.3525in" in messages[0]
        assert "line 0" in messages[0]
        assert "line 0 ('SERIAL 42')" in messages[0]

    def test_gated_on_compression_budget(self) -> None:
        """max_h_compress=0 skips the slide (behaviour predates this phase)."""
        label = _edge_label(["SERIAL 42"], ["left"], LEFT_HOLE, max_h_compress=0.0)
        rendered = render_label_to_plt(label)

        assert rendered.has_collisions is True
        assert rendered.source_label is label
        assert rendered.source_label.collision_edge_margin_by_line == {}

    def test_center_aligned_text_never_gets_an_edge_margin(self) -> None:
        """A centered line has no anchored edge: compression resolves it, as before."""
        label = _edge_label(["SERIAL 42"], ["center"], SIDE_HOLES, width=3.0)
        rendered = render_label_to_plt(label)

        assert rendered.has_collisions is False
        adjusted = rendered.source_label
        assert adjusted.collision_edge_margin_by_line == {}
        assert adjusted.collision_center_by_line == frozenset()
        assert rendered.compression_by_line[0] == pytest.approx(0.85)

    def test_slide_stays_inside_the_content_area(self) -> None:
        """The slide never pushes a fitting line past the far inner margin."""
        label = _edge_label(["SERIAL 42"], ["left"], [*LEFT_HOLE, *RIGHT_HOLE])
        rendered = render_label_to_plt(label)
        adjusted = rendered.source_label

        _lc, entries = _render_text_local_with_bounds(adjusted)
        assert detect_text_hole_collisions(adjusted, entries) == []
        for entry in entries:
            assert entry.bounds[0] >= label.h_margin - 1e-9
            assert entry.bounds[2] <= label.width - label.h_margin + 1e-9

    def test_slide_stacks_on_top_of_hole_margin_floor(self) -> None:
        """Phase 2's floor applies first; the slide then clears what remains."""
        label = _edge_label(
            ["SERIAL 42"],
            ["left"],
            [*LEFT_HOLE, *BOTTOM_HOLE_ONLY],
            min_hole_margin=0.0,
        )
        rendered = render_label_to_plt(label)

        assert rendered.has_collisions is False
        adjusted = rendered.source_label
        # The bottom hole clears by dropping the margin to the floor, and the
        # left hole by the slide (which needs less travel at the floor margin).
        assert adjusted.hole_margin == pytest.approx(0.0)
        assert adjusted.collision_edge_margin_by_line[0] == pytest.approx(0.165)
        _lc, entries = _render_text_local_with_bounds(adjusted)
        assert detect_text_hole_collisions(adjusted, entries) == []

    def test_resolver_returns_none_without_edge_collisions(self) -> None:
        """A label with no edge-aligned near-side collision has nothing to slide."""
        label = _edge_label(["SERIAL 42"], ["center"], LEFT_HOLE)
        _lc, entries = _render_text_local_with_bounds(label)
        assert resolve_via_edge_margin(label, entries) is None


class TestAlignMarginAdjustGlobal:
    """Sharing the edge-margin increase with same-alignment siblings."""

    def test_disabled_moves_only_the_colliding_line(self) -> None:
        """The default (false) leaves the sibling at its declared margin."""
        label = _edge_label(
            ["SERIAL 42", "ID 7"],
            ["left", "left"],
            LEFT_HOLE,
            heights=[0.4, 0.12],
        )
        rendered = render_label_to_plt(label)
        adjusted = rendered.source_label

        assert adjusted.align_margin_adjust_global is False
        assert set(adjusted.collision_edge_margin_by_line) == {0}
        _lc, entries = _render_text_local_with_bounds(adjusted)
        by_index = {entry.line_index: entry.bounds for entry in entries}
        assert by_index[0][0] == pytest.approx(0.1 + 0.3525)
        assert by_index[1][0] == pytest.approx(0.1)
        assert detect_text_hole_collisions(adjusted, entries) == []

    def test_enabled_matches_sibling_edge(self) -> None:
        """With sharing on, the sibling takes the same margin: edges stay flush."""
        label = _edge_label(
            ["SERIAL 42", "ID 7"],
            ["left", "left"],
            LEFT_HOLE,
            heights=[0.4, 0.12],
            align_margin_adjust_global=True,
        )
        rendered = render_label_to_plt(label)
        adjusted = rendered.source_label

        assert adjusted.collision_edge_margin_by_line == {
            0: pytest.approx(0.3525),
            1: pytest.approx(0.3525),
        }
        _lc, entries = _render_text_local_with_bounds(adjusted)
        left_edges = [round(entry.bounds[0], 6) for entry in entries]
        assert left_edges == [pytest.approx(0.4525, abs=1e-6)] * 2

    def test_shared_shift_flushes_the_wide_sibling_by_compressing_it(self) -> None:
        """A sibling with less slack takes the full shift and compresses to fit.

        The content box is a compression constraint, not an alignment-stopping
        one: the wide sibling slides the full shared shift and the margin-fit
        compression shortens it so its far edge lands on the far margin, which
        keeps the anchored edges flush.
        """
        # The wide second line leaves only ~0.05in of slack on the right.
        label = _edge_label(
            ["SERIAL 42", "WIDE LABEL TE"],
            ["left", "left"],
            LEFT_HOLE,
            align_margin_adjust_global=True,
        )
        rendered = render_label_to_plt(label)
        adjusted = rendered.source_label

        # Both lines take the colliding line's full required shift.
        assert adjusted.collision_edge_margin_by_line == {
            0: pytest.approx(0.3525),
            1: pytest.approx(0.3525),
        }
        _lc, entries = _render_text_local_with_bounds(adjusted)
        by_index = {entry.line_index: entry.bounds for entry in entries}
        # Flush anchored edges, and the wide sibling's far edge respects the box.
        assert by_index[0][0] == pytest.approx(by_index[1][0], abs=1e-3)
        assert by_index[1][2] <= label.width - label.h_margin + 1e-9
        # The flush cost the wide sibling a margin-fit compression.
        assert rendered.compression_by_line[1] < 1.0
        assert detect_text_hole_collisions(adjusted, entries) == []

    def test_shared_shift_raises_when_a_receiver_budget_cannot_fit(self) -> None:
        """A non-colliding receiver whose budget cannot absorb the overflow fails.

        The flush guarantee binds a line moved purely by sharing: it must end
        flush, so a box overflow beyond its ``max_h_compress`` budget is
        unresolvable. (A *colliding* line's slide clears a hole and is allowed
        to spill -- the far-side holes, not the box, decide its escalation.)
        """
        # Line 0 (0.4in) collides with the left hole and needs +0.3525; the
        # 0.25in wide sibling does NOT collide, overflows the box by ~0.22in at
        # the shared shift, needing scale 0.940 -- below its 0.95 budget floor.
        label = _edge_label(
            ["SERIAL 42", "WIDE LABEL TEXT XXXXX"],
            ["left", "left"],
            LEFT_HOLE,
            heights=[0.4, 0.25],
            max_h_compress=0.05,
            align_margin_adjust_global=True,
        )
        _lc, entries = _render_text_local_with_bounds(label)

        with pytest.raises(LabelRenderError, match="cannot keep its alignment-locked"):
            resolve_via_edge_margin(label, entries)

    def test_sharing_never_crosses_alignment_values(self) -> None:
        """A center-aligned sibling is never given the left-aligned slide."""
        label = _edge_label(
            ["SERIAL 42", "ID 7"],
            ["left", "center"],
            LEFT_HOLE,
            heights=[0.4, 0.12],
            align_margin_adjust_global=True,
        )
        rendered = render_label_to_plt(label)

        assert set(rendered.source_label.collision_edge_margin_by_line) == {0}

    def test_sharing_iterates_the_slide_to_clear_residual_near_misses(self) -> None:
        """Phase 3 re-runs until the shared slide clears every collision.

        With sharing on, the first slide uses the tightest need among the
        colliding lines; a line that needs marginally more stays a hair under
        the threshold. Re-running the slide lets it catch up (all lines end at
        the largest need) instead of escalating to the Phase-4 center switch --
        which would move that line's anchored edge and drag the whole flush
        group far inward. Regression: a 3-line label with two corner holes
        needed three slide rounds; the single-pass phase switched a line to
        center and flushed the siblings to a 1.33in left margin.
        """
        label = _edge_label(
            ["SERIAL 42", "ID 7", "SERIAL 4"],
            ["left", "left", "left"],
            [*TOP_LEFT_HOLE, *BOTTOM_LEFT_HOLE],
            align_margin_adjust_global=True,
        )
        rendered = render_label_to_plt(label)
        adjusted = rendered.source_label

        # No center switch, no flush: the shared slide resolved it.
        assert adjusted.collision_center_by_line == frozenset()
        assert rendered.compression_by_line == {}
        assert rendered.has_collisions is False
        # Every line ends flush at the same anchored edge.
        _lc, entries = _render_text_local_with_bounds(adjusted)
        left_edges = [entry.bounds[0] for entry in entries]
        assert all(edge == pytest.approx(0.1 + 0.3525, abs=1e-3) for edge in left_edges)
        assert max(left_edges) - min(left_edges) < 1e-6
        assert detect_text_hole_collisions(adjusted, entries) == []


class TestCollisionAvoidanceProbeMode:
    """``collision_avoidance=False``: detection only, no phase fires."""

    def test_probe_mode_skips_every_phase(self) -> None:
        """A label the phases would fix renders untouched (typography only)."""
        # Both-side holes: the full render switches line 0 to center and
        # compresses it; the probe render must do neither.
        label = _edge_label(["SERIAL 42"], ["left"], SIDE_HOLES, width=2.9)
        full = render_label_to_plt(label)
        probe = render_label_to_plt(label, collision_avoidance=False)

        assert full.has_collisions is False
        assert sorted(full.source_label.collision_center_by_line) == [0]
        # The probe detects (observational) but never acts.
        assert probe.collision_detected is True
        assert probe.source_label is label
        assert probe.source_label.collision_center_by_line == frozenset()
        assert probe.source_label.collision_edge_margin_by_line == {}
        assert probe.source_label.collision_compress_by_line == {}
        assert probe.source_label.hole_margin == label.hole_margin
        # The probe's scales are pure margin-fit typography (no collision squeeze).
        assert probe.compression_by_line.get(0, 1.0) >= full.compression_by_line.get(0, 1.0)

    def test_probe_mode_clean_label_is_unchanged(self) -> None:
        """A label with no collision renders identically in probe mode."""
        label = _edge_label(["SHORT"], ["left"], [])
        full = render_label_to_plt(label)
        probe = render_label_to_plt(label, collision_avoidance=False)

        assert probe.collision_detected is False
        assert probe.plt_content == full.plt_content

    def test_sharing_probe_excludes_collision_compression(self) -> None:
        """The h_compress_global probe never bakes a collision scale in.

        Regression: the measurement render's flush compressed two lines to
        78%, and that collision scale was shared to every same-height line as
        if it were typography -- the final render (needing no flush) then paid
        21.9% compression the geometry never asked for.
        """
        from plt_optimizer.generate.h_compress import apply_global_h_compress

        label = _edge_label(
            ["SERIAL 42", "ID 7"],
            ["left", "left"],
            SIDE_HOLES,
            width=2.9,
            heights=[0.4, 0.12],
            align_margin_adjust_global=True,
        )
        # The full render switches line 0 to center and compresses it to 81%.
        full = render_label_to_plt(label)
        assert full.compression_by_line[0] < 0.9

        shared = apply_global_h_compress([label])
        # The probe (avoidance off) sees no compressed line, so nothing is
        # shared -- the collision scale never leaks into typography.
        assert shared[0].global_compress_by_line == {}


class TestFlushSiblingsToSwitchedEdge:
    """Phase-4 tail: flush unswitched siblings onto the switched line's edge.

    With ``align_margin_adjust_global`` on, a line the avoidance switched to
    ``center`` (centering + compression repositioned it) leaves its
    same-alignment siblings at the old shared margin, so the anchored edges
    are no longer flush. The flush slides each sibling to the switched lines'
    final anchored edge (the leftmost for ``left``, the rightmost for
    ``right``), compressing a sibling whose far end no longer fits.
    """

    @staticmethod
    def _switched(label: ResolvedLabel) -> ResolvedLabel:
        """The label state right after Phase 4 switched line 0 to center.

        Line 0 (``SERIAL 42`` on the 2.9in label) compresses to 0.8125 and
        centers at x ~= [0.4611, 2.4389]; the sibling sits at its declared
        margin (0.1), so the flush target is 0.4611 - 0.1 = 0.3611in.
        """
        return replace(
            label,
            collision_center_by_line=frozenset({0}),
            collision_compress_by_line={0: 0.8125},
        )

    def test_left_aligned_sibling_flushes_to_the_switched_edge(self) -> None:
        """The non-colliding sibling slides to the switched line's left edge."""
        label = _edge_label(
            ["SERIAL 42", "ID 7"],
            ["left", "left"],
            SIDE_HOLES,
            width=2.9,
            heights=[0.4, 0.12],
            align_margin_adjust_global=True,
        )
        state = self._switched(label)
        _lc, entries = _render_text_local_with_bounds(state)

        flushed = flush_siblings_to_switched_edge(
            state, entries, render=_render_text_local_with_bounds
        )

        assert flushed is not None
        # The sibling's margin equals the switched line's final left edge.
        assert flushed.collision_edge_margin_by_line == {1: pytest.approx(0.3611, abs=1e-3)}
        # It fits at the flush edge: no compression needed.
        assert flushed.collision_compress_by_line == {0: 0.8125}
        _lc, final = _render_text_local_with_bounds(flushed)
        by_index = {entry.line_index: entry.bounds for entry in final}
        assert by_index[1][0] == pytest.approx(by_index[0][0])
        assert detect_text_hole_collisions(flushed, final) == []

    def test_right_aligned_group_flushes_to_the_rightmost_edge(self) -> None:
        """The mirror case: a right-aligned sibling flushes on its right edge."""
        label = _edge_label(
            ["SERIAL 42", "ID 7"],
            ["right", "right"],
            SIDE_HOLES,
            width=2.9,
            heights=[0.4, 0.12],
            align_margin_adjust_global=True,
        )
        state = self._switched(label)
        _lc, entries = _render_text_local_with_bounds(state)

        flushed = flush_siblings_to_switched_edge(
            state, entries, render=_render_text_local_with_bounds
        )

        assert flushed is not None
        _lc, final = _render_text_local_with_bounds(flushed)
        by_index = {entry.line_index: entry.bounds for entry in final}
        # Anchored on the right: the right edges line up.
        assert by_index[1][2] == pytest.approx(by_index[0][2])
        assert detect_text_hole_collisions(flushed, final) == []

    def test_flush_compresses_a_sibling_that_no_longer_fits(self) -> None:
        """Sliding inward shrinks the far-end room: the flush compresses it."""
        # The 2.149in sibling ends at 2.249; at the flush edge it would reach
        # 2.610, past the right hole, so the sweep compresses it.
        label = _edge_label(
            ["SERIAL 42", "SERIAL 4"],
            ["left", "left"],
            SIDE_HOLES,
            width=2.9,
            heights=[0.4, 0.4],
            align_margin_adjust_global=True,
        )
        state = self._switched(label)
        _lc, entries = _render_text_local_with_bounds(state)

        flushed = flush_siblings_to_switched_edge(
            state, entries, render=_render_text_local_with_bounds
        )

        assert flushed is not None
        assert flushed.collision_edge_margin_by_line == {1: pytest.approx(0.3611, abs=1e-3)}
        # The sibling carries a compression scale on top of the flush.
        assert flushed.collision_compress_by_line[1] < 1.0
        _lc, final = _render_text_local_with_bounds(flushed)
        by_index = {entry.line_index: entry.bounds for entry in final}
        assert by_index[1][0] == pytest.approx(by_index[0][0])
        assert detect_text_hole_collisions(flushed, final) == []

    def test_flush_raises_when_the_budget_cannot_clear_it(self) -> None:
        """A sibling too wide to compress into the flush room fails the job."""
        # The 2.67in sibling needs scale ~0.62 at the flush edge; a 0.05 budget
        # floors it at 0.95, so no flush scale fits -- the flush guarantee
        # (edges must align) cannot be met, so the collision fails the job.
        label = _edge_label(
            ["SERIAL 42", "WIDE LABEL TEXT"],
            ["left", "left"],
            SIDE_HOLES,
            width=2.9,
            heights=[0.4, 0.4],
            max_h_compress=0.05,
            align_margin_adjust_global=True,
        )
        state = replace(label, collision_center_by_line=frozenset({0}))
        _lc, entries = _render_text_local_with_bounds(state)

        with pytest.raises(LabelRenderError, match="cannot keep its alignment-locked"):
            flush_siblings_to_switched_edge(state, entries, render=_render_text_local_with_bounds)

    def test_disabled_flag_flushes_nothing(self) -> None:
        """align_margin_adjust_global=false keeps the surgical per-line state."""
        label = _edge_label(
            ["SERIAL 42", "ID 7"],
            ["left", "left"],
            SIDE_HOLES,
            width=2.9,
            heights=[0.4, 0.12],
        )
        state = self._switched(label)
        _lc, entries = _render_text_local_with_bounds(state)

        assert (
            flush_siblings_to_switched_edge(state, entries, render=_render_text_local_with_bounds)
            is None
        )

    def test_no_switch_flushes_nothing(self) -> None:
        """Without a switched line there is no flush edge to align to."""
        label = _edge_label(
            ["SERIAL 42", "ID 7"],
            ["left", "left"],
            SIDE_HOLES,
            width=2.9,
            heights=[0.4, 0.12],
            align_margin_adjust_global=True,
        )
        _lc, entries = _render_text_local_with_bounds(label)

        assert (
            flush_siblings_to_switched_edge(label, entries, render=_render_text_local_with_bounds)
            is None
        )

    def test_multiple_switches_flush_to_the_leftmost_edge(self) -> None:
        """With two switched lines, the sibling flushes to the leftmost edge."""
        # Line 2 (the short one) centers at x ~= 1.344; line 0 centers at
        # 0.4611. The leftmost (0.4611) is the flush target.
        label = _edge_label(
            ["SERIAL 42", "SERIAL 4", "ID 7"],
            ["left", "left", "left"],
            SIDE_HOLES,
            width=2.9,
            heights=[0.4, 0.4, 0.12],
            align_margin_adjust_global=True,
        )
        state = replace(
            label,
            collision_center_by_line=frozenset({0, 2}),
            collision_compress_by_line={0: 0.8125},
        )
        _lc, entries = _render_text_local_with_bounds(state)

        flushed = flush_siblings_to_switched_edge(
            state, entries, render=_render_text_local_with_bounds
        )

        assert flushed is not None
        assert flushed.collision_edge_margin_by_line == {1: pytest.approx(0.3611, abs=1e-3)}
        _lc, final = _render_text_local_with_bounds(flushed)
        by_index = {entry.line_index: entry.bounds for entry in final}
        # Flush with line 0 (the leftmost switched), NOT line 2.
        assert by_index[1][0] == pytest.approx(by_index[0][0], abs=1e-3)
        assert by_index[1][0] < by_index[2][0]
        assert detect_text_hole_collisions(flushed, final) == []

    def test_mixed_alignment_groups_flush_independently(self) -> None:
        """A switched group with no unswitched siblings contributes no moves."""
        # Lines 0 (left) and 1 (right) are switched; the only unswitched
        # edge-aligned line is left, so the right group has no receivers.
        label = _edge_label(
            ["SERIAL 42", "SERIAL 4", "ID 7"],
            ["left", "right", "left"],
            SIDE_HOLES,
            width=2.9,
            heights=[0.4, 0.4, 0.12],
            align_margin_adjust_global=True,
        )
        state = replace(
            label,
            collision_center_by_line=frozenset({0, 1}),
            collision_compress_by_line={0: 0.8125, 1: 0.8125},
        )
        _lc, entries = _render_text_local_with_bounds(state)

        flushed = flush_siblings_to_switched_edge(
            state, entries, render=_render_text_local_with_bounds
        )

        assert flushed is not None
        # Only the left group moves (to line 0's edge); line 2 is the receiver.
        assert flushed.collision_edge_margin_by_line == {2: pytest.approx(0.3611, abs=1e-3)}
        _lc, final = _render_text_local_with_bounds(flushed)
        by_index = {entry.line_index: entry.bounds for entry in final}
        assert by_index[2][0] == pytest.approx(by_index[0][0], abs=1e-3)
        assert detect_text_hole_collisions(flushed, final) == []

    def test_already_flush_sibling_is_skipped(self) -> None:
        """A sibling already sitting on the flush edge needs no move."""
        label = _edge_label(
            ["SERIAL 42", "ID 7"],
            ["left", "left"],
            SIDE_HOLES,
            width=2.9,
            heights=[0.4, 0.12],
            align_margin_adjust_global=True,
        )
        # The sibling carries exactly the flush target (0.4611 - 0.1 margin).
        state = replace(
            label,
            collision_center_by_line=frozenset({0}),
            collision_compress_by_line={0: 0.8125},
            collision_edge_margin_by_line={1: 0.3610528923483456},
        )
        _lc, entries = _render_text_local_with_bounds(state)

        assert (
            flush_siblings_to_switched_edge(state, entries, render=_render_text_local_with_bounds)
            is None
        )

    def test_center_aligned_sibling_never_flushes(self) -> None:
        """A center-aligned sibling has no anchored edge to move."""
        label = _edge_label(
            ["SERIAL 42", "ID 7"],
            ["left", "center"],
            SIDE_HOLES,
            width=2.9,
            heights=[0.4, 0.12],
            align_margin_adjust_global=True,
        )
        state = self._switched(label)
        _lc, entries = _render_text_local_with_bounds(state)

        assert (
            flush_siblings_to_switched_edge(state, entries, render=_render_text_local_with_bounds)
            is None
        )

    def test_end_to_end_switch_flushes_the_sibling(self) -> None:
        """The full render: the switched line and its sibling end up flush."""
        label = _edge_label(
            ["SERIAL 42", "ID 7"],
            ["left", "left"],
            SIDE_HOLES,
            width=2.9,
            heights=[0.4, 0.12],
            align_margin_adjust_global=True,
        )
        rendered = render_label_to_plt(label)
        adjusted = rendered.source_label

        assert rendered.has_collisions is False
        assert rendered.collision_detected is True
        # Line 0 switched to center (compression finished it); the sibling
        # kept its declared alignment and flushed onto line 0's final edge.
        assert adjusted.collision_center_by_line == frozenset({0})
        assert adjusted.collision_compress_by_line[0] < 1.0
        _lc, entries = _render_text_local_with_bounds(adjusted)
        by_index = {entry.line_index: entry.bounds for entry in entries}
        assert by_index[1][0] == pytest.approx(by_index[0][0], abs=1e-3)
        assert detect_text_hole_collisions(adjusted, entries) == []

    def test_flag_off_switch_leaves_the_sibling_at_its_margin(
        self,
    ) -> None:
        """With sharing off, a switch does not move the sibling at all."""
        label = _edge_label(
            ["SERIAL 42", "ID 7"],
            ["left", "left"],
            SIDE_HOLES,
            width=2.9,
            heights=[0.4, 0.12],
        )
        rendered = render_label_to_plt(label)
        adjusted = rendered.source_label

        assert rendered.has_collisions is False
        assert adjusted.collision_center_by_line == frozenset({0})
        # No flush: the sibling keeps its declared margin (no edge margin set).
        assert adjusted.collision_edge_margin_by_line == {}
        _lc, entries = _render_text_local_with_bounds(adjusted)
        by_index = {entry.line_index: entry.bounds for entry in entries}
        assert by_index[1][0] == pytest.approx(0.1)

    def test_flush_dropped_when_the_final_render_disagrees(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A flush the verification render rejects keeps the unflushed layout.

        The flush sweep measures with the cheap text seam; the renderer
        verifies with the full label render. When the verification finds a
        collision the flush is dropped and the resolved (unflushed) render is
        returned unchanged.
        """
        label = _edge_label(
            ["SERIAL 42", "SERIAL 4"],
            ["left", "left"],
            SIDE_HOLES,
            width=2.9,
            heights=[0.4, 0.4],
            align_margin_adjust_global=True,
        )
        # Force the flush helper to hand back a label that collides: line 1
        # stays unswitched and gets a margin shoving its far end under the
        # right hole, so the verification render rejects the flush.
        monkeypatch.setattr(
            "plt_optimizer.generate.label_renderer.flush_siblings_to_switched_edge",
            lambda resolved_label, entries, **_kw: replace(
                resolved_label,
                collision_center_by_line=frozenset({0}),
                collision_compress_by_line={0: 0.8125},
                collision_edge_margin_by_line={1: 2.5},
            ),
        )
        rendered = render_label_to_plt(label)

        assert rendered.has_collisions is False
        # The rejected flush never reached the emitted label.
        assert rendered.source_label.collision_edge_margin_by_line.get(1) != 2.5

    def test_flush_logs_warning_naming_the_target(self, caplog: pytest.LogCaptureFixture) -> None:
        """The flush action logs at WARNING with the alignment and margin."""
        label = _edge_label(
            ["SERIAL 42", "ID 7"],
            ["left", "left"],
            SIDE_HOLES,
            width=2.9,
            heights=[0.4, 0.12],
            align_margin_adjust_global=True,
        )
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            render_label_to_plt(label)

        messages = [r.getMessage() for r in caplog.records if "flushed" in r.getMessage()]
        assert messages
        assert "left-aligned" in messages[0]
        assert "center-switched" in messages[0]
        assert "line 1" in messages[0]
        assert "0.3611in" in messages[0]


class TestAlignmentSwitchToCenter:
    """Phase 4: switch to center when the edge margin is exhausted."""

    def test_both_side_holes_switch_alignment_then_compress(self) -> None:
        """A line colliding on both sides centers, and compression finishes the job."""
        label = _edge_label(["SERIAL 42"], ["left"], SIDE_HOLES, width=2.9)
        _lc, entries = _render_text_local_with_bounds(label)
        assert {c.hole_location for c in detect_text_hole_collisions(label, entries)} == {
            "left",
            "right",
        }

        rendered = render_label_to_plt(label)
        adjusted = rendered.source_label

        assert rendered.has_collisions is False
        assert rendered.collision_detected is True
        # The line's anchored end sits under the left hole, so the slide is
        # capped by the right hole (no slack): the switch carries it to center,
        # where the compression acts symmetrically on both ends.
        assert adjusted.collision_center_by_line == frozenset({0})
        assert adjusted.collision_compress_by_line[0] < 1.0
        assert rendered.compression_by_line[0] == pytest.approx(
            adjusted.collision_compress_by_line[0], rel=1e-3
        )
        _lc, final = _render_text_local_with_bounds(adjusted)
        assert detect_text_hole_collisions(adjusted, final) == []

    def test_switch_logged_with_the_compression_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The resolution WARNING states that the alignment was switched."""
        label = _edge_label(["SERIAL 42"], ["left"], SIDE_HOLES, width=2.9)
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            render_label_to_plt(label)

        messages = [
            r.getMessage() for r in caplog.records if "center alignment" in r.getMessage()
        ]
        assert messages
        assert "edge_label" in messages[0]

    def test_center_switch_respects_compression_budget(self) -> None:
        """A budget too small to clear the centered line leaves it colliding."""
        label = _edge_label(["SERIAL 42"], ["left"], SIDE_HOLES, width=2.9, max_h_compress=0.05)
        rendered = render_label_to_plt(label)
        assert rendered.has_collisions is True

    def test_unresolvable_diagnostics_report_the_switch(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The ERROR breakdown names the alignment the avoidance switched."""
        label = _edge_label(["SERIAL 42"], ["left"], SIDE_HOLES, width=2.9, max_h_compress=0.05)
        with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
            rendered = render_label_to_plt(label)

        assert rendered.has_collisions is True
        messages = [
            r.getMessage() for r in caplog.records if "cannot be resolved" in r.getMessage()
        ]
        assert messages
        assert "Alignment switched to center: 0" in messages[0]
        assert "Edge-margin slide" not in messages[0]

    def test_unresolvable_diagnostics_report_the_slide(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The ERROR breakdown names the edge margin the avoidance spent."""
        label = _edge_label(
            ["SERIAL 42"],
            ["left"],
            [*LEFT_HOLE, *BOTTOM_HOLE_ONLY],
            min_hole_margin=0.1875,
        )
        with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
            rendered = render_label_to_plt(label)

        assert rendered.has_collisions is True
        messages = [
            r.getMessage() for r in caplog.records if "cannot be resolved" in r.getMessage()
        ]
        assert messages
        # The slide cleared the left hole; the bottom hole needs the margin
        # (blocked by the floor) or a shorter line, so the job is unresolvable.
        assert "Edge-margin slide: line 0 (+0.3525in)" in messages[0]
        assert "bottom hole" in messages[0]


class TestEdgeMarginSlideUnitCases:
    """Direct unit coverage for the Phase-3 helper branches."""

    def test_max_edge_shift_ignores_the_far_margin(self) -> None:
        """The content box is not a slide cap: only a far-side hole caps it."""
        from plt_optimizer.generate.collision.detection import _max_edge_shift

        # A line whose far end already sits at the far margin: the old box cap
        # returned 0; with no far-side hole the slide is uncapped.
        bounds = (0.1, 0.4, 3.9, 0.6)
        uncapped = _max_edge_shift(bounds, "left", [], 0.0)
        assert uncapped > 100.0  # math.inf in practice
        # A far-side hole on the line's band caps the slide at its near edge.
        capped = _max_edge_shift(bounds, "left", [(4.5, 0.5, 0.1)], 0.1)
        assert 0.0 < capped < 0.6
        # A vertically unreachable hole never caps a horizontal slide.
        assert _max_edge_shift(bounds, "left", [(4.5, 3.0, 0.1)], 0.1) == uncapped

    def test_no_holes_returns_none(self) -> None:
        """A label without holes has nothing to slide away from."""
        label = _edge_label(["SERIAL 42"], ["left"], [])
        _lc, entries = _render_text_local_with_bounds(label)
        assert resolve_via_edge_margin(label, entries) is None

    def test_over_wide_line_slides_and_compresses_to_its_floor(self) -> None:
        """The content box is not a slide cap: an over-wide line slides anyway.

        Only a far-side HOLE stops the slide. A line wider than the content
        box overflows before and after; the renderer's margin-fit compression
        absorbs as much of the slide as the line's ``max_h_compress`` budget
        allows, and the slide clears the near-side hole collision-free.
        """
        # 4.34in of text inside a 3.8in content box, no far-side hole.
        label = _edge_label(["WIDE LABEL TEXT"], ["left"], LEFT_HOLE, width=3.0)
        _lc, entries = _render_text_local_with_bounds(label)
        slid = resolve_via_edge_margin(label, entries)

        assert slid is not None
        assert slid.collision_edge_margin_by_line[0] == pytest.approx(0.3525)
        rendered = render_label_to_plt(label)
        assert rendered.has_collisions is False
        # The slide cost the over-wide line its full compression budget.
        assert rendered.compression_by_line[0] == pytest.approx(0.7, rel=1e-3)

    def test_rounded_rectangle_uses_the_inset_content_box(self) -> None:
        """A rounded label's slide cap follows the inset, not the raw margin."""
        label = _edge_label(["SERIAL 42"], ["left"], LEFT_HOLE)
        rounded = replace(label, rounded_rectangle_fraction=0.5)
        _lc, rounded_entries = _render_text_local_with_bounds(rounded)
        _lc, sharp_entries = _render_text_local_with_bounds(label)
        slid = resolve_via_edge_margin(rounded, rounded_entries)
        sharp = resolve_via_edge_margin(label, sharp_entries)
        assert slid is not None and sharp is not None
        # The corner inset widens the effective margin, shrinking the slack.
        assert 0.0 < slid.collision_edge_margin_by_line[0] < (
            sharp.collision_edge_margin_by_line[0]
        )

    def test_right_aligned_far_left_hole_caps_the_slide(self) -> None:
        """A right-aligned line's slide stops before it reaches a far left hole."""
        label = _edge_label(["SERIAL 42"], ["right"], [*LEFT_HOLE, *RIGHT_HOLE], width=3.0)
        rendered = render_label_to_plt(label)
        adjusted = rendered.source_label

        assert rendered.has_collisions is False
        # The far (left) hole caps the slide, so the switch + compression finish it.
        assert adjusted.collision_edge_margin_by_line[0] < 0.3525
        assert 0 in adjusted.collision_center_by_line
        _lc, entries = _render_text_local_with_bounds(adjusted)
        assert detect_text_hole_collisions(adjusted, entries) == []

    def test_center_switch_alone_can_clear_without_compression(self) -> None:
        """Centering a pinned line can clear the hole with zero compression."""
        # 4in label: the 2.434in centered line starts at x=0.783, clear of the
        # left hole's right edge (0.4375) + the stroke floor.
        label = _edge_label(["SERIAL 42"], ["left"], LEFT_HOLE, width=4.0)
        result = resolve_via_compression(
            label, 0.3, render=_render_text_local_with_bounds
        )

        assert result is not None
        assert result.collision_center_by_line == frozenset({0})
        # The switch alone resolved it: the sweep never ran.
        assert result.collision_compress_by_line == {}
        _lc, entries = _render_text_local_with_bounds(result)
        assert detect_text_hole_collisions(result, entries) == []


class TestUnresolvableDiagnosticsState:
    """The diagnostics report the label state the sweep evaluated."""

    def test_compression_state_without_an_attempted_scale(self) -> None:
        """A label carrying compression (no attempted scale) reports its average."""
        label = replace(
            _edge_label(["SERIAL 42"], ["left"], LEFT_HOLE),
            collision_compress_by_line={0: 0.8},
            collision_edge_margin_by_line={0: 0.25},
            collision_center_by_line=frozenset({0}),
        )
        _lc, entries = _render_text_local_with_bounds(label)
        collisions = detect_text_hole_collisions(label, entries)
        message = format_unresolvable_collision(label, collisions)

        assert "Compression: max_h_compress=0.30 applied" in message
        assert "most aggressive text scale 0.800" in message
        assert "Edge-margin slide: line 0 (+0.2500in)" in message
        assert "Alignment switched to center: 0" in message

    def test_over_wide_sibling_still_takes_the_shared_shift(self) -> None:
        """A sibling with no box slack is not skipped: it slides and compresses.

        The box is a compression constraint, so the over-wide sibling receives
        the full shared shift; the renderer's margin-fit compression absorbs the
        overflow up to its ``max_h_compress`` floor, and the anchored edges
        stay flush.
        """
        # The second line is wider than the content box.
        label = _edge_label(
            ["SERIAL 42", "WIDE LABEL TEXT XXXXX"],
            ["left", "left"],
            LEFT_HOLE,
            align_margin_adjust_global=True,
        )
        _lc, entries = _render_text_local_with_bounds(label)
        slid = resolve_via_edge_margin(label, entries)

        assert slid is not None
        assert slid.collision_edge_margin_by_line == {
            0: pytest.approx(0.3525),
            1: pytest.approx(0.3525),
        }
        rendered = render_label_to_plt(label)
        assert rendered.has_collisions is False
        assert rendered.compression_by_line[1] < 1.0
        _lc, final = _render_text_local_with_bounds(rendered.source_label)
        by_index = {entry.line_index: entry.bounds for entry in final}
        assert by_index[0][0] == pytest.approx(by_index[1][0], abs=1e-3)
