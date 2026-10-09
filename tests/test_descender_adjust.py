"""Unit tests for the pure descending-character relief solver.

:mod:`plt_optimizer.generate.descender_adjust` is matplotlib-free pure math:
the per-pair gap predicate, the closed-form relief ramp, and the one-way ramp
merge that drives the renderer's fixpoint. These tests exercise the math
directly (no rendering), mirroring how the baseline-spacing helpers in
``test_resolution.py`` pin the stacking arithmetic.
"""

from __future__ import annotations

import math

import pytest

from plt_optimizer.generate.descender_adjust import (
    DescenderFix,
    GlyphBox,
    PairGap,
    apply_ramp,
    measure_pairs,
    merge_ramps,
    ramp_for,
    ramps_changed,
    required_gap,
    worst_deficit,
)


def _box(
    glyph_index: int,
    char: str,
    x_min: float,
    y_min: float,
    x_max: float,
    y_max: float,
    baseline_y: float = 0.0,
) -> GlyphBox:
    """Build a :class:`GlyphBox` with the descender derived from ``y_min``."""
    return GlyphBox(
        glyph_index=glyph_index,
        char=char,
        bounds=(x_min, y_min, x_max, y_max),
        descender=max(0.0, baseline_y - y_min),
    )


class TestRequiredGap:
    """The stroke-aware clearance a glyph pair must keep."""

    def test_is_stroke_floor_times_one_plus_clearance(self) -> None:
        """Half of each cutter (the touch floor) scaled by the unitless factor."""
        # floor = 0.5 * (0.06 + 0.04) = 0.05; clearance 0.5 -> 1.5x the floor.
        assert required_gap(0.06, 0.04, 0.5) == pytest.approx(0.075)

    def test_zero_clearance_is_the_touch_floor(self) -> None:
        """Clearance 0.0 means only true touching triggers the relief."""
        assert required_gap(0.06, 0.06, 0.0) == 0.06

    def test_unitless_across_text_sizes(self) -> None:
        """The same clearance demands the same FRACTION of the cutter width.

        An inch-valued clearance reads entirely differently at 2in vs 0.25in
        text; as a factor of the floor the trigger is size-invariant.
        """
        small = required_gap(0.03, 0.03, 0.3)
        large = required_gap(0.25, 0.25, 0.3)
        assert small == pytest.approx(0.03 * 1.3)
        assert large == pytest.approx(0.25 * 1.3)
        assert large / small == pytest.approx(0.25 / 0.03)

    def test_never_negative(self) -> None:
        """A negative clearance cannot push the required gap below the floor."""
        assert required_gap(0.0, 0.0, -1.0) == 0.0
        assert required_gap(0.06, 0.06, -0.5) == pytest.approx(0.06)


class TestGlyphBoxDescends:
    """The descending-glyph predicate (epsilon-gated, never a real ink loss)."""

    def test_ink_below_baseline_descends(self) -> None:
        """A box reaching below its baseline is a relief candidate."""
        assert _box(0, "p", 0.0, -0.1, 0.2, 0.3).descends is True

    def test_on_baseline_does_not_descend(self) -> None:
        """A box sitting on the baseline is never a candidate."""
        assert _box(0, "H", 0.0, 0.0, 0.2, 0.3).descends is False

    def test_rounding_noise_is_not_a_descender(self) -> None:
        """Sub-nanometre noise below the baseline must not manufacture ink."""
        assert _box(0, "H", 0.0, -1e-12, 0.2, 0.3).descends is False


class TestMeasurePairs:
    """Pairwise gap measurement against the following line."""

    def test_only_descending_glyphs_are_upper_members(self) -> None:
        """A line of plain capitals never triggers the mechanism."""
        upper = [_box(0, "H", 0.0, 0.0, 0.2, 0.4), _box(1, "O", 0.3, 0.0, 0.5, 0.4)]
        lower = [_box(0, "R", 0.0, -0.4, 0.2, 0.0)]
        assert measure_pairs(upper, lower, required=0.05) == []

    def test_every_descending_pair_is_reported(self) -> None:
        """Safe pairs are reported too, so the caller can report the tightest."""
        upper = [_box(0, "p", 0.0, -0.1, 0.2, 0.3)]
        lower = [_box(0, "R", 0.0, -0.4, 0.2, 0.0), _box(1, "A", 0.9, -0.4, 1.1, 0.0)]
        pairs = measure_pairs(upper, lower, required=0.05)
        assert len(pairs) == 2
        # Overlapping X span: the 'p' ink bottom (-0.1) sits 0.1in BELOW the
        # 'R' cap line (0.0), so the pair penetrates by that much.
        assert pairs[0].gap == pytest.approx(-0.1)
        assert pairs[0].collides is True
        # Lateral partner: no X overlap, can never collide.
        assert math.isinf(pairs[1].gap)
        assert pairs[1].collides is False

    def test_disjoint_x_spans_report_infinite_gap(self) -> None:
        """Laterally distant partners can never collide, so they are 'inf' away."""
        upper = [_box(0, "p", 0.0, -0.1, 0.2, 0.3)]
        lower = [_box(0, "R", 5.0, -0.4, 5.2, 0.0)]
        (pair,) = measure_pairs(upper, lower, required=0.05)
        assert math.isinf(pair.gap)
        assert pair.collides is False


class TestPairGapDeficit:
    """Deficit / collision predicates on a measured pair."""

    def test_tangent_pair_is_safe(self) -> None:
        """A gap exactly equal to the requirement is tangency, not a collision."""
        pair = PairGap(
            upper=_box(0, "p", 0.0, -0.1, 0.2, 0.3),
            lower=_box(0, "R", 0.0, -0.4, 0.2, 0.0),
            gap=0.05,
            required=0.05,
        )
        assert pair.deficit == 0.0
        assert pair.collides is False

    def test_short_gap_is_a_collision(self) -> None:
        """A gap below the requirement reports its shortfall."""
        pair = PairGap(
            upper=_box(0, "p", 0.0, -0.1, 0.2, 0.3),
            lower=_box(0, "R", 0.0, -0.4, 0.2, 0.0),
            gap=0.02,
            required=0.05,
        )
        assert pair.deficit == pytest.approx(0.03)
        assert pair.collides is True


class TestRampFor:
    """The closed-form smallest ramp clearing a pair."""

    def test_safe_pair_needs_no_ramp(self) -> None:
        """No deficit means no relief."""
        assert ramp_for(0.1, 0.05, 0.1, 0.5, 0.7) == 0.0

    def test_disjoint_pair_needs_no_ramp(self) -> None:
        """An infinite gap (no X overlap) never triggers relief."""
        assert ramp_for(math.inf, 0.05, 0.1, 0.5, 0.7) == 0.0

    def test_non_descending_glyph_needs_no_ramp(self) -> None:
        """A glyph with no descender has nothing to relieve."""
        assert ramp_for(-0.05, 0.05, 0.0, 0.5, 0.7) == 0.0

    def test_ramp_is_linear_in_the_gap_it_produces(self) -> None:
        """The smallest clearing t lands the gap exactly on the requirement."""
        descender, v_scale, raise_fraction, required = 0.1, 0.5, 0.7, 0.06
        gap = -0.02
        t = ramp_for(gap, required, descender, v_scale, raise_fraction)
        assert 0.0 < t < 1.0
        scale_y, raise_amount = apply_ramp(descender, v_scale, raise_fraction, t)
        # In the upper glyph's baseline frame its ink bottom starts at
        # -descender, so the measured gap places the partner's top at
        # -descender - gap. The relief moves the bottom to
        # raise - descender*scale_y, so the gap grows by exactly the ramp.
        partner_top = -descender - gap
        gap_after = (raise_amount - descender * scale_y) - partner_top
        assert gap_after == pytest.approx(required, abs=1e-12)

    def test_clamps_to_one_when_allowances_are_exhausted(self) -> None:
        """A deficit beyond both allowances returns the fully spent ramp."""
        assert ramp_for(-1.0, 0.05, 0.1, 0.5, 0.7) == 1.0

    def test_zero_allowances_cannot_help(self) -> None:
        """No downscale and no raise means the ramp is inert (spacing must grow)."""
        assert ramp_for(-0.05, 0.05, 0.1, 1.0, 0.0) == 0.0

    def test_scale_only_ramp_clears_a_small_deficit(self) -> None:
        """With raising forbidden, the squeeze alone carries the relief."""
        t = ramp_for(-0.02, 0.04, 0.1, 0.2, 0.0)
        assert 0.0 < t < 1.0
        _scale_y, raise_amount = apply_ramp(0.1, 0.2, 0.0, t)
        assert raise_amount == 0.0

    def test_raise_only_ramp_clears_a_small_deficit(self) -> None:
        """With downscaling forbidden, the lift alone carries the relief."""
        t = ramp_for(-0.02, 0.04, 0.1, 1.0, 0.7)
        assert 0.0 < t < 1.0
        scale_y, _raise = apply_ramp(0.1, 1.0, 0.7, t)
        assert scale_y == pytest.approx(1.0)


class TestApplyRamp:
    """Ramp-to-(scale, raise) split and its bounds."""

    def test_zero_ramp_is_the_identity(self) -> None:
        """t = 0 leaves the glyph exactly as rendered."""
        assert apply_ramp(0.1, 0.5, 0.7, 0.0) == (1.0, 0.0)

    def test_full_ramp_spends_both_allowances(self) -> None:
        """t = 1 reaches the v_scale floor and the full raise."""
        scale_y, raise_amount = apply_ramp(0.1, 0.5, 0.7, 1.0)
        assert scale_y == pytest.approx(0.5)
        assert raise_amount == pytest.approx(0.07)

    def test_full_raise_lands_the_lowest_point_on_the_baseline(self) -> None:
        """raise_fraction 1.0 at t = 1 lifts the ink bottom exactly to y = 0."""
        descender = 0.12
        scale_y, raise_amount = apply_ramp(descender, 1.0, 1.0, 1.0)
        assert raise_amount - descender * scale_y == pytest.approx(0.0)

    def test_ramp_is_clamped_into_unit_range(self) -> None:
        """Out-of-range ramps clamp; the allowances are never overspent."""
        assert apply_ramp(0.1, 0.5, 0.7, -1.0) == (1.0, 0.0)
        scale_y, raise_amount = apply_ramp(0.1, 0.5, 0.7, 5.0)
        assert scale_y == pytest.approx(0.5)
        assert raise_amount == pytest.approx(0.07)


class TestMergeRamps:
    """The one-way ramp merge (a glyph only ever gets smaller and higher)."""

    def test_largest_value_wins_per_glyph(self) -> None:
        """A smaller proposal never shrinks an applied ramp."""
        merged = merge_ramps({0: 0.6, 1: 0.2}, {0: 0.3, 2: 0.9})
        assert merged == {0: 0.6, 1: 0.2, 2: 0.9}

    def test_empty_proposal_is_identity(self) -> None:
        """A pass measuring nothing leaves every ramp in place."""
        assert merge_ramps({0: 0.4}, {}) == {0: 0.4}


class TestRampsChanged:
    """Convergence detection for the fixpoint loop."""

    def test_growth_beyond_tolerance_is_a_change(self) -> None:
        """A real increase requests another pass."""
        assert ramps_changed({0: 0.3}, {0: 0.5}) is True

    def test_new_glyph_is_a_change(self) -> None:
        """A glyph newly entering relief requests another pass."""
        assert ramps_changed({}, {0: 0.1}) is True

    def test_shrink_is_not_a_change(self) -> None:
        """The merge never shrinks a ramp; a shrink is not progress."""
        assert ramps_changed({0: 0.5}, {0: 0.3}) is False

    def test_noise_below_tolerance_is_converged(self) -> None:
        """Float noise at the convergence point stops the loop."""
        assert ramps_changed({0: 0.5}, {0: 0.5 + 1e-15}) is False


class TestWorstDeficit:
    """Per-gap residual shortfall (the spacing-growth driver)."""

    def test_empty_gap_has_no_deficit(self) -> None:
        """A gap with no pairs is satisfied."""
        assert worst_deficit([]) == 0.0

    def test_infinite_gaps_are_ignored(self) -> None:
        """X-disjoint pairs never drive spacing growth."""
        pair = PairGap(
            upper=_box(0, "p", 0.0, -0.1, 0.2, 0.3),
            lower=_box(0, "R", 5.0, -0.4, 5.2, 0.0),
            gap=math.inf,
            required=0.05,
        )
        assert worst_deficit([pair]) == 0.0

    def test_returns_the_largest_shortfall(self) -> None:
        """The gap grows by the worst pair's deficit."""
        upper = _box(0, "p", 0.0, -0.1, 0.2, 0.3)
        lower = _box(0, "R", 0.0, -0.4, 0.2, 0.0)
        pairs = [
            PairGap(upper=upper, lower=lower, gap=0.04, required=0.05),
            PairGap(upper=upper, lower=lower, gap=-0.02, required=0.05),
        ]
        assert worst_deficit(pairs) == pytest.approx(0.07)


class TestDescenderFix:
    """The reporting record is a frozen value object."""

    def test_carries_the_measured_transition(self) -> None:
        """The record keeps the gap before and after the relief."""
        fix = DescenderFix(
            line_index=0,
            glyph_index=2,
            char="p",
            scale_y=0.6,
            raise_amount=0.07,
            gap_before=-0.077,
            gap_after=0.045,
        )
        assert (fix.line_index, fix.glyph_index, fix.char) == (0, 2, "p")
        assert fix.gap_after > fix.gap_before
