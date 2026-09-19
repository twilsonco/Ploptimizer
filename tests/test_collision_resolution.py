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

from plt_optimizer.generate.label_renderer import (
    LabelRenderError,
    _collision_threshold,
    _detect_text_hole_collisions,
    _render_text_local_with_bounds,
    _resolve_collision_via_compression,
    _resolve_collision_via_margin_adjustment,
    assert_no_collisions,
    log_text_hole_collisions,
    render_label_to_plt,
)
from plt_optimizer.generate.resolution import (
    ResolvedHoleSpec,
    ResolvedLabel,
    ResolvedTextLine,
)

LOGGER_NAME = "plt_optimizer.generate.label_renderer"

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
        # The emitted PLT must come from the adjusted label: re-detecting
        # collisions on the adjusted geometry finds none.
        _lc, entries = _render_text_local_with_bounds(adjusted)
        assert _detect_text_hole_collisions(adjusted, entries) == []

    def test_margin_adjustment_respects_min_hole_margin_floor(self) -> None:
        """The reduced margin never drops below the configured floor."""
        # 1.25in label: gap = 0.14 - hole_margin, so the collision
        # clears around 0.125in -- above a 0.1in floor but below the
        # default 0.1875in margin.
        label = _label(text="HELLO", holes=BOTTOM_HOLE, height=1.25, min_hole_margin=0.1)
        _lc, entries = _render_text_local_with_bounds(label)
        resolved = _resolve_collision_via_margin_adjustment(label, entries)

        assert resolved is not None
        assert resolved.hole_margin >= 0.1 - 1e-9
        assert resolved.hole_margin < label.hole_margin

    def test_floor_too_high_blocks_resolution(self) -> None:
        """A floor above the clearing margin leaves the collision in place."""
        label = _label(text="HELLO", holes=BOTTOM_HOLE, min_hole_margin=0.16)
        _lc, entries = _render_text_local_with_bounds(label)
        assert _resolve_collision_via_margin_adjustment(label, entries) is None

        rendered = render_label_to_plt(label)
        assert rendered.has_collisions is True

    def test_no_floor_configured_skips_phase2(self) -> None:
        """Without min_hole_margin, margin adjustment is not attempted."""
        label = _label(text="HELLO", holes=BOTTOM_HOLE, min_hole_margin=None)
        _lc, entries = _render_text_local_with_bounds(label)
        assert _resolve_collision_via_margin_adjustment(label, entries) is None

    def test_no_budget_when_margin_at_floor(self) -> None:
        """hole_margin already at the floor leaves nothing to shrink."""
        label = _label(text="HELLO", holes=BOTTOM_HOLE, hole_margin=0.05, min_hole_margin=0.05)
        _lc, entries = _render_text_local_with_bounds(label)
        assert _resolve_collision_via_margin_adjustment(label, entries) is None

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
        assert _resolve_collision_via_margin_adjustment(label, entries) is None

    def test_margin_already_at_floor_flags_collision(self) -> None:
        """With hole_margin pinned at the floor, Phase 2 has no budget to shrink."""
        label = _label(text="HELLO", holes=BOTTOM_HOLE, hole_margin=0.05, min_hole_margin=0.05)
        rendered = render_label_to_plt(label)
        assert rendered.has_collisions is True

    def test_log_text_hole_collisions_no_holes_returns_empty(self) -> None:
        """The observational helper short-circuits for hole-free labels."""
        label = _label(text="HELLO", holes=[])
        assert log_text_hole_collisions(label) == []


class TestCompressionFallback:
    """Phase 3: horizontal compression when margins cannot help."""

    def test_compression_resolves_side_hole_collision(self) -> None:
        """Ample max_h_compress narrows centered text clear of side holes."""
        label = _label(max_h_compress=0.7)
        rendered = render_label_to_plt(label)

        assert rendered.has_collisions is False
        assert rendered.collision_detected is True
        adjusted = rendered.source_label
        assert adjusted.collision_compress < 1.0
        # Never compress below the configured budget floor (1 - 0.7).
        assert adjusted.collision_compress >= 1.0 - 0.7 - 1e-9

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
        assert adjusted.collision_compress < 1.0
        assert rendered.has_collisions is False
        assert rendered.collision_detected is True

    def test_compression_helper_returns_none_at_floor(self) -> None:
        """A label already at the compression floor cannot compress more."""
        label = _label(max_h_compress=1.0)
        at_floor = replace(label, collision_compress=0.0)
        assert _resolve_collision_via_compression(at_floor, 1.0) is None

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

    def test_avoidance_repaired_collision_still_aborts(self) -> None:
        """A collision repaired by avoidance is still unacceptable output."""
        label = _label(text="HELLO", holes=BOTTOM_HOLE, min_hole_margin=0.0)
        rendered = render_label_to_plt(label)
        # Avoidance succeeded (final render is clean) but the collision was
        # detected, so the jobspec must still be revised.
        assert rendered.has_collisions is False
        assert rendered.collision_detected is True
        with pytest.raises(LabelRenderError) as exc_info:
            assert_no_collisions([rendered])
        assert "resolve_label" in str(exc_info.value)

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
        from plt_optimizer.generate.layout import generate_layout_with_bounds

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
        collisions = _detect_text_hole_collisions(label, entries)
        assert len(collisions) == 1
        assert collisions[0].gap > 0.0  # near miss, not penetration
        assert collisions[0].gap < _collision_threshold(label, 0)

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
        assert _detect_text_hole_collisions(safe, entries) == []

        # Required 0.015 + 0.04 = 0.055 > gap -> collision (near miss).
        tight = _label(hole_text_collision_distance=0.04, **base)
        assert len(_detect_text_hole_collisions(tight, entries)) == 1

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
        assert len(_detect_text_hole_collisions(label, entries)) == 1

        # A zero-threshold label (no stroke floor at all) keeps the legacy
        # semantics: tangency alone is not a collision.
        zero_cutter = replace(
            label,
            content=[replace(_make_line("HELLO"), cutter_diameter=0.0)],
        )
        _lc2, zero_entries = _render_text_local_with_bounds(zero_cutter)
        assert _detect_text_hole_collisions(zero_cutter, zero_entries) == []

    def test_threshold_uses_per_line_text_cutter(self) -> None:
        """Each line's own cutter diameter feeds the stroke floor."""
        label = _label(hole_text_collision_distance=0.15, hole_cutter_diameter=0.015)
        big_cutter_line = replace(_make_line("B"), cutter_diameter=0.06)
        wide = replace(label, content=[_make_line("A"), big_cutter_line])
        base = _collision_threshold(wide, 0)
        assert base == pytest.approx(0.5 * (0.015 + 0.03) + 0.15)
        # The second line's larger cutter raises its threshold.
        assert _collision_threshold(wide, 1) == pytest.approx(0.5 * (0.015 + 0.06) + 0.15)

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
        strict_resolved = _resolve_collision_via_margin_adjustment(strict, entries)
        _lc2, entries2 = _render_text_local_with_bounds(stroke_aware)
        aware_resolved = _resolve_collision_via_margin_adjustment(stroke_aware, entries2)

        assert strict_resolved is not None
        assert aware_resolved is not None
        # The stroke floor demands more separation: the sweep must end at
        # a strictly smaller hole margin than strict penetration requires.
        assert aware_resolved.hole_margin < strict_resolved.hole_margin
        # And the resolved geometry genuinely clears the threshold.
        _lc3, aware_entries = _render_text_local_with_bounds(aware_resolved)
        assert _detect_text_hole_collisions(aware_resolved, aware_entries) == []

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
        label = ResolvedLabel(id="defaults", count=1, width=3.0, height=1.0, margin=0.1)
        assert label.hole_text_collision_distance == pytest.approx(0.15)
        assert label.hole_cutter_diameter == pytest.approx(0.015)
