"""Unit tests for the pure descending-character relief solver.

:mod:`plt_optimizer.generate.descender_adjust` is matplotlib-free pure math:
the per-pair gap predicate, the closed-form relief ramp, and the one-way ramp
merge that drives the renderer's fixpoint. These tests exercise the math
directly (no rendering), mirroring how the baseline-spacing helpers in
``test_resolution.py`` pin the stacking arithmetic.
"""

from __future__ import annotations

import math
import random
from dataclasses import replace

import pytest

from plt_optimizer.generate.descender_adjust import (
    DescenderFix,
    GlyphBox,
    PairGap,
    apply_ramp,
    colliding_partners,
    measure_glyph_gaps,
    measure_pairs,
    merge_ramps,
    ramp_for,
    ramp_for_measured,
    ramp_ink,
    ramps_changed,
    recip_ink,
    reciprocal_scale_y,
    required_gap,
    worst_deficit,
    worst_glyph_deficit,
)
from plt_optimizer.generate.geometry import ink_gap


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


class TestReciprocalScaleY:
    """The following line's top-down scale, driven by the SAME ramp."""

    def test_zero_ramp_is_the_identity(self) -> None:
        """t = 0 leaves the lower glyph exactly as rendered."""
        assert reciprocal_scale_y(0.0, 0.5) == 1.0

    def test_full_ramp_spends_the_allowance(self) -> None:
        """t = 1 reaches the reciprocal_scale floor."""
        assert reciprocal_scale_y(1.0, 0.5) == pytest.approx(0.5)

    def test_ramp_is_proportional(self) -> None:
        """Half the ramp is half the way from 1.0 to the floor."""
        assert reciprocal_scale_y(0.5, 0.4) == pytest.approx(0.7)

    def test_scale_one_forbids_the_reciprocal_half(self) -> None:
        """reciprocal_scale 1.0 (the opt-out) keeps the glyph at full size."""
        assert reciprocal_scale_y(1.0, 1.0) == 1.0

    def test_ramp_is_clamped_into_unit_range(self) -> None:
        """Out-of-range ramps clamp; the allowance is never overspent."""
        assert reciprocal_scale_y(-1.0, 0.5) == 1.0
        assert reciprocal_scale_y(5.0, 0.5) == pytest.approx(0.5)


class TestRecipInk:
    """The stacked-frame top-down scale: the lowest ink point is the anchor."""

    def test_lowest_point_never_moves(self) -> None:
        """Scaling about the anchor leaves the anchor vertex exactly in place."""
        ink = (((0.0, 1.0), (0.1, 1.4)),)
        scaled = recip_ink(ink, anchor_y=1.0, scale_y=0.5)
        assert scaled[0][0] == pytest.approx((0.0, 1.0))
        # The top descends toward the anchor: 1.0 + (1.4 - 1.0) * 0.5.
        assert scaled[0][1] == pytest.approx((0.1, 1.2))

    def test_identity_scale_is_a_noop(self) -> None:
        """scale 1.0 leaves the ink exactly where it is."""
        ink = (((0.0, 1.0), (0.1, 1.4)),)
        scaled = recip_ink(ink, anchor_y=1.0, scale_y=1.0)
        assert scaled[0][0] == pytest.approx((0.0, 1.0))
        assert scaled[0][1] == pytest.approx((0.1, 1.4))

    def test_x_is_untouched(self) -> None:
        """The reciprocal half is a Y transform only."""
        ink = (((0.25, 1.0), (0.75, 1.4)),)
        scaled = recip_ink(ink, anchor_y=1.0, scale_y=0.3)
        assert [x for x, _y in scaled[0]] == [0.25, 0.75]

    def test_no_ink_moves_below_the_anchor(self) -> None:
        """The whole glyph shrinks toward its bottom; nothing overshoots."""
        ink = (((0.0, 1.0), (0.1, 2.0), (0.2, 1.5)),)
        scaled = recip_ink(ink, anchor_y=1.0, scale_y=0.25)
        assert min(y for _, y in scaled[0]) == pytest.approx(1.0)


class TestCollidingPartners:
    """Which following-line glyphs share a descending glyph's collision."""

    def test_only_partners_inside_the_clearance_are_returned(self) -> None:
        """The tight partner is selected; the roomy one is left alone."""
        upper = _ink_box(0, "p", 0.0, -0.3, 0.2, 0.3, ink=(((0.0, 0.0), (0.1, -0.3)),))
        tight = _ink_box(1, "R", 0.0, -0.8, 0.2, -0.4, ink=(((0.0, -0.4), (0.2, -0.4)),))
        roomy = _ink_box(2, "S", 3.0, -0.8, 3.2, -0.4, ink=(((3.0, -0.4), (3.2, -0.4)),))
        partners = colliding_partners(upper, [tight, roomy], required=0.15)
        assert partners == (1,)

    def test_tangent_partner_is_safe(self) -> None:
        """A gap exactly equal to the requirement is tangency, not a collision."""
        # A vertical descender at x = 0 down to y = -0.3; the partner's ink is
        # the horizontal run at y = 0.2 starting at x = 0, so the closest
        # points are (0, 0) and (0, 0.2): a gap of exactly 0.2in.
        upper = _ink_box(0, "p", 0.0, -0.3, 0.1, 0.3, ink=(((0.0, 0.0), (0.0, -0.3)),))
        partner = _ink_box(1, "R", 0.0, 0.2, 0.2, 0.5, ink=(((0.0, 0.2), (0.2, 0.2)),))
        assert colliding_partners(upper, [partner], required=0.2) == ()
        # A hair more demanded and the same partner shares the collision.
        assert colliding_partners(upper, [partner], required=0.21) == (1,)

    def test_glyph_without_sub_baseline_ink_has_no_partners(self) -> None:
        """Nothing reaches into the gap, so nothing shares a collision."""
        upper = GlyphBox(
            glyph_index=0,
            char="H",
            bounds=(0.0, 0.0, 0.2, 0.3),
            descender=0.0,
            ink=(((0.0, 0.0), (0.2, 0.3)),),
            sub_baseline_ink=(),
        )
        partner = _ink_box(1, "R", 0.0, -0.5, 0.2, 0.0, ink=(((0.0, -0.4), (0.2, -0.4)),))
        assert colliding_partners(upper, [partner], required=0.5) == ()

    def test_bbox_prefilter_preserves_partner_selection(self) -> None:
        """The exact box prefilter must select exactly the ink_gap partners.

        A long line of partners at mixed distances: the prefilter skips the
        far glyphs wholesale, so the result must equal the unfiltered
        per-partner ink_gap scan for every required clearance.
        """
        rng = random.Random(7)
        upper = _ink_box(0, "p", 0.0, -0.3, 0.1, 0.3, ink=(((0.0, 0.0), (0.0, -0.3)),))
        lowers = []
        for index in range(60):
            x = rng.uniform(-30.0, 30.0)
            y = rng.uniform(-0.45, 0.45)
            lowers.append(
                _ink_box(
                    index,
                    "x",
                    x,
                    y,
                    x + 0.2,
                    y + 0.3,
                    ink=(((x, y), (x + 0.2, y + 0.3)),),
                )
            )
        for required in (0.05, 0.15, 0.3, 1.0):
            limit = required - 1e-6
            expected = tuple(
                box.glyph_index
                for box in lowers
                if ink_gap(upper.sub_baseline_ink, box.ink) < limit
            )
            assert colliding_partners(upper, lowers, required=required) == expected

    def test_sideways_hook_pulls_in_its_crook_partner(self) -> None:
        """The pair the box model hides is a partner for the reciprocal half.

        The hook ends beside the tittle (X-disjoint boxes), so the box model
        reports ``inf`` -- yet the true 2D distance sits inside the clearance
        and the tittle must share the relief.
        """
        upper = _ink_box(0, "g", 0.0, -0.1, 0.1, 0.3, ink=(((0.0, 0.0), (0.1, -0.1)),))
        dot = _ink_box(5, "i", 0.15, -0.25, 0.2, -0.1, ink=(((0.15, -0.15), (0.2, -0.15)),))
        assert colliding_partners(upper, [dot], required=0.1) == (5,)


class TestRampForReciprocalSlope:
    """The box-model closed form with the following line sharing the load."""

    def test_defaults_reproduce_the_upper_only_math(self) -> None:
        """Omitting the reciprocal arguments is bit-identical to the old form."""
        plain = ramp_for(-0.02, 0.06, 0.1, 0.5, 0.7)
        explicit = ramp_for(-0.02, 0.06, 0.1, 0.5, 0.7, 0.0, 1.0)
        assert plain == explicit

    def test_reciprocal_shares_the_load_so_the_ramp_shrinks(self) -> None:
        """A partner that can shrink top-down needs a smaller ramp."""
        upper_only = ramp_for(-0.02, 0.06, 0.1, 0.5, 0.7)
        shared = ramp_for(-0.02, 0.06, 0.1, 0.5, 0.7, 0.4, 0.5)
        assert shared < upper_only
        # The slope gains lower_height * (1 - reciprocal_scale) = 0.4 * 0.5.
        assert shared == pytest.approx(0.08 / (0.07 + 0.05 + 0.2))

    def test_reciprocal_alone_clears_when_the_upper_is_inert(self) -> None:
        """With the upper allowances zero, the partner's squeeze carries it."""
        inert = ramp_for(-0.02, 0.06, 0.1, 1.0, 0.0, 0.4, 1.0)
        assert inert == 0.0
        shared = ramp_for(-0.02, 0.06, 0.1, 1.0, 0.0, 0.4, 0.5)
        assert 0.0 < shared < 1.0


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


def _ink_box(
    glyph_index: int,
    char: str,
    x_min: float,
    y_min: float,
    x_max: float,
    y_max: float,
    ink: tuple,
    baseline_y: float = 0.0,
) -> GlyphBox:
    """Build a :class:`GlyphBox` carrying explicit swept ink."""
    return GlyphBox(
        glyph_index=glyph_index,
        char=char,
        bounds=(x_min, y_min, x_max, y_max),
        descender=max(0.0, baseline_y - y_min),
        ink=ink,
        sub_baseline_ink=tuple(tuple((x, y) for x, y in polyline if y <= 0.0) for polyline in ink),
    )


class TestInkModelMeasurement:
    """The rolling-circle test: true 2D swept-ink distance, not boxes."""

    def test_x_disjoint_near_pair_measures_its_real_distance(self) -> None:
        """The relief's blind spot is gone: a sideways near-miss triggers.

        A hook ending at x=0.1, y=0 with a tittle at (0.15, -0.1): the box
        model reports ``inf`` (disjoint X spans) and the pair hides; the ink
        model measures the true distance (~0.112in) and the pair collides
        against a 0.135in requirement.
        """
        upper = _ink_box(0, "g", 0.0, -0.1, 0.1, 0.0, ink=(((0.0, 0.0), (0.1, 0.0)),))
        lower = _ink_box(0, "i", 0.15, -0.15, 0.15, -0.05, ink=(((0.15, -0.15), (0.15, -0.05)),))
        (pair,) = measure_pairs([upper], [lower], required=0.135)
        # Closest points: the hook's tip (0.1, 0) and the tittle's top (0.15, -0.05).
        assert pair.gap == pytest.approx(math.hypot(0.05, 0.05))
        assert pair.collides is True

    def test_sub_baseline_ink_is_the_rolling_surface(self) -> None:
        """Only the sub-baseline part of the upper glyph drives the gap.

        The upper glyph's ink above the baseline sits right on top of the
        lower glyph's box; the rolling circle travels the sub-baseline part,
        so the measured gap is the sub-baseline distance.
        """
        upper = _ink_box(
            0,
            "g",
            0.0,
            -0.1,
            0.2,
            0.4,
            ink=(((0.0, 0.4), (0.2, 0.4)), ((0.0, 0.0), (0.2, -0.1))),
        )
        lower = _ink_box(0, "N", 0.0, -0.5, 0.2, -0.3, ink=(((0.0, -0.5), (0.2, -0.5)),))
        (pair,) = measure_pairs([upper], [lower], required=0.5)
        # Sub-baseline ink ends at (0.2, -0.1); the lower ink is the y=-0.5
        # run: the distance from that endpoint to the run is 0.4. The required
        # clearance (the grid cell) covers the distance, keeping it exact.
        assert pair.gap == pytest.approx(0.4)

    def test_glyphs_without_ink_fall_back_to_the_box_model(self) -> None:
        """Hand-built boxes (no ink) keep the vertical box gap semantics."""
        upper = _box(0, "p", 0.0, -0.1, 0.2, 0.3)
        lower = _box(0, "R", 0.0, -0.4, 0.2, 0.0)
        (pair,) = measure_pairs([upper], [lower], required=0.05)
        assert pair.gap == pytest.approx(-0.1)

    def test_mixed_ink_and_box_glyphs_measure_infinity(self) -> None:
        """A box-only upper glyph against ink-carrying lowers is unmeasurable.

        Mixing models in one line would compare boxes against ink (the box
        is a superset of the ink: it invents collisions), so the pair
        reports ``inf`` and never triggers.
        """
        upper = _box(0, "p", 0.0, -0.1, 0.2, 0.3)
        lower = _ink_box(0, "N", 0.0, -0.4, 0.2, 0.0, ink=(((0.0, -0.4), (0.2, 0.0)),))
        (pair,) = measure_pairs([upper], [lower], required=0.05)
        assert math.isinf(pair.gap)
        assert pair.collides is False

    def test_grid_and_brute_force_agree(self) -> None:
        """The grid-accelerated path returns the same gap as ink_gap."""
        from plt_optimizer.generate.geometry import ink_gap

        upper = _ink_box(0, "g", 0.0, -0.3, 0.5, 0.3, ink=(((0.0, 0.3), (0.3, 0.0), (0.5, -0.3)),))
        lowers = [
            _ink_box(0, "i", 0.55, -0.15, 0.6, -0.1, ink=(((0.55, -0.15), (0.6, -0.1)),)),
            _ink_box(1, "N", 1.0, -0.4, 1.2, 0.0, ink=(((1.0, -0.4), (1.2, 0.0)),)),
        ]
        required = 0.5
        pairs = measure_pairs([upper], lowers, required=required)
        brute = min(ink_gap(upper.sub_baseline_ink, lower.ink) for lower in lowers)
        assert min(pair.gap for pair in pairs) == pytest.approx(brute)


class TestMeasureGlyphGaps:
    """The per-glyph aggregate driving the relief's ramp and spacing growth."""

    def test_only_descending_glyphs_are_measured(self) -> None:
        """Plain capitals never trigger the mechanism."""
        upper = _ink_box(0, "H", 0.0, 0.0, 0.2, 0.3, ink=(((0.0, 0.0), (0.2, 0.3)),))
        lower = _ink_box(0, "R", 0.0, -0.4, 0.2, 0.0, ink=(((0.0, -0.4), (0.2, 0.0)),))
        assert measure_glyph_gaps([upper], [lower], required=0.05) == []

    def test_tightest_partner_wins(self) -> None:
        """The glyph's gap is the minimum over the whole line below."""
        upper = _ink_box(0, "y", 0.0, -0.2, 0.2, 0.0, ink=(((0.0, 0.0), (0.2, -0.2)),))
        lowers = [
            _ink_box(0, "R", 0.0, -0.5, 0.2, -0.3, ink=(((0.0, -0.5), (0.2, -0.3)),)),
            _ink_box(1, "O", 0.3, -0.35, 0.5, -0.15, ink=(((0.3, -0.35), (0.5, -0.15)),)),
        ]
        (gap,) = measure_glyph_gaps([upper], lowers, required=0.5)
        from plt_optimizer.generate.geometry import ink_gap

        assert gap.gap == pytest.approx(
            min(ink_gap(upper.sub_baseline_ink, lower.ink) for lower in lowers)
        )

    def test_worst_glyph_deficit_ignores_unmeasurable(self) -> None:
        """An unmeasurable glyph (inf gap) never drives spacing growth."""
        # Ink entirely above the baseline: the rolling circle has no surface
        # to travel, so the glyph is unmeasurable (inf).
        upper = _ink_box(0, "p", 0.0, -0.1, 0.2, 0.3, ink=(((0.05, 0.05), (0.2, 0.3)),))
        lower = _ink_box(0, "R", 0.0, -0.4, 0.2, 0.0, ink=(((0.0, -0.4), (0.2, 0.0)),))
        (gap,) = measure_glyph_gaps([upper], [lower], required=0.05)
        assert math.isinf(gap.gap)
        assert worst_glyph_deficit([gap]) == 0.0


class TestRampInk:
    """The pure stacked-frame transform mirroring the renderer's stroke ramp."""

    def test_scales_about_the_baseline_and_raises(self) -> None:
        """A point maps to (y - baseline) * scale + raise + baseline."""
        ink = (((0.0, -0.2), (0.1, 0.1)),)
        ramped = ramp_ink(ink, baseline_y=1.5, scale_y=0.5, raise_amount=0.05)
        assert ramped[0][0] == pytest.approx((0.0, 0.7))
        assert ramped[0][1] == pytest.approx((0.1, 0.85))

    def test_identity_ramp_is_a_noop(self) -> None:
        """scale 1.0 / raise 0.0 leaves the ink exactly where it is."""
        ink = (((0.0, -0.2), (0.1, 0.1)),)
        ramped = ramp_ink(ink, baseline_y=-3.0, scale_y=1.0, raise_amount=0.0)
        assert ramped[0][0] == pytest.approx(ink[0][0])
        assert ramped[0][1] == pytest.approx(ink[0][1])

    def test_full_raise_lands_the_lowest_point_on_the_baseline(self) -> None:
        """raise == descender, scale 1.0: the lowest ink point sits on the baseline.

        The ink is in the stacked frame, so the baseline sits at ``baseline_y``
        and the descender's tip at ``baseline_y - descender``.
        """
        ink = (((0.0, 1.75), (0.1, 2.0)),)
        ramped = ramp_ink(ink, baseline_y=2.0, scale_y=1.0, raise_amount=0.25)
        assert min(y for _, y in ramped[0]) == pytest.approx(2.0)


class TestRampForMeasured:
    """The bisection solving the ink-distance ramp (no closed form)."""

    def test_safe_at_zero_needs_no_ramp(self) -> None:
        """A pair already at clearance settles at t = 0."""
        assert ramp_for_measured(lambda t: 0.1, required=0.05) == 0.0

    def test_exhausted_allowances_return_one(self) -> None:
        """A pair no ramp can clear spends everything (t = 1)."""
        assert ramp_for_measured(lambda t: 0.01, required=0.05) == 1.0

    def test_linear_gap_matches_the_closed_form(self) -> None:
        """On a linear gap the bisection reproduces ramp_for's answer.

        The bisection accepts within the emission tolerance (1e-6in), so it
        settles a hair below the exact crossing -- within a thousandth of a
        plotter unit of the closed form.
        """
        gap, required, descender, v_scale, raise_fraction = -0.05, 0.05, 0.1, 0.5, 0.7
        slope = raise_fraction * descender + descender * (1.0 - v_scale)
        measured = ramp_for_measured(
            lambda t: gap + t * slope,
            required=required,
        )
        assert measured == pytest.approx(
            ramp_for(gap, required, descender, v_scale, raise_fraction), abs=1e-5
        )

    def test_bisected_ramp_clears_the_pair(self) -> None:
        """The returned ramp is the smallest clearing value (within tolerance)."""
        gap, required = -0.05, 0.05
        ramp = ramp_for_measured(lambda t: gap + t * 0.12, required=required)
        assert gap + ramp * 0.12 >= required - 1e-6
        assert gap + (ramp - 1e-4) * 0.12 < required


def _border_box(y: float, x_min: float = 0.0, x_max: float = 10.0) -> GlyphBox:
    """Build a :class:`GlyphBox` shaped like the label's bottom border."""
    return GlyphBox(
        glyph_index=-1,
        char="",
        bounds=(x_min, y, x_max, y),
        descender=0.0,
        ink=(((x_min, y), (x_max, y)),),
        sub_baseline_ink=(),
    )


class TestBorderMeasurement:
    """The bottom border as a virtual 'following line' for the rolling circle.

    The grid indexing the border ink is sized to the required clearance, so it
    is exact below that radius and reports ``inf`` beyond it (both mean "safe"
    to the relief). These fixtures keep the measured distance inside the cell
    so the exact value is observable.
    """

    def test_vertical_descender_measures_the_distance_to_the_line(self) -> None:
        """A descender hanging straight down measures its gap to the border."""
        # Sub-baseline ink ends at y = -0.3; the border sits at y = -0.34.
        upper = _ink_box(0, "g", 0.0, -0.3, 0.2, 0.3, ink=(((0.0, 0.0), (0.1, -0.3)),))
        (gap,) = measure_glyph_gaps([upper], [_border_box(-0.34)], required=0.05)
        assert gap.gap == pytest.approx(0.04)
        assert gap.collides is True

    def test_clear_pair_does_not_collide(self) -> None:
        """A descender with enough air above the border is safe."""
        # Gap 0.1in against a required 0.15in: inside the grid's cell, so the
        # exact distance is observable and the pair still collides.
        upper = _ink_box(0, "g", 0.0, -0.3, 0.2, 0.3, ink=(((0.0, 0.0), (0.1, -0.3)),))
        (gap,) = measure_glyph_gaps([upper], [_border_box(-0.4)], required=0.15)
        assert gap.gap == pytest.approx(0.1)
        assert gap.collides is True
        # The same pair against a smaller requirement is safe.
        (safe,) = measure_glyph_gaps([upper], [_border_box(-0.4)], required=0.05)
        assert safe.collides is False

    def test_sideways_hook_measures_the_true_2d_distance(self) -> None:
        """A hook reaching sideways toward the border measures its real distance.

        The closest border point is diagonally below the hook's tip, so the
        gap is the hypotenuse, not the vertical drop.
        """
        # Tip at (0.1, -0.3); the border at y = -0.5 spans x in [0.15, 10],
        # so the closest border point is (0.15, -0.5): hypot(0.05, 0.2).
        upper = _ink_box(0, "g", 0.0, -0.3, 0.1, 0.3, ink=(((0.0, 0.0), (0.1, -0.3)),))
        border = GlyphBox(
            glyph_index=-1,
            char="",
            bounds=(0.15, -0.5, 10.0, -0.5),
            descender=0.0,
            ink=(((0.15, -0.5), (10.0, -0.5)),),
            sub_baseline_ink=(),
        )
        (gap,) = measure_glyph_gaps([upper], [border], required=0.3)
        assert gap.gap == pytest.approx(math.hypot(0.05, 0.2))

    def test_ink_touching_the_border_reports_zero(self) -> None:
        """Ink reaching the border centreline has no clearance."""
        upper = _ink_box(0, "g", 0.0, -0.5, 0.2, 0.3, ink=(((0.0, 0.0), (0.1, -0.5)),))
        (gap,) = measure_glyph_gaps([upper], [_border_box(-0.5)], required=0.05)
        assert gap.gap == pytest.approx(0.0)
        assert gap.collides is True

    def test_glyph_without_a_descender_never_triggers(self) -> None:
        """A cap-only glyph is not a relief candidate against the border."""
        upper = _ink_box(0, "H", 0.0, 0.0, 0.2, 0.3, ink=(((0.0, 0.0), (0.2, 0.3)),))
        assert measure_glyph_gaps([upper], [_border_box(-0.1)], required=0.05) == []

    def test_ramp_lifts_the_descender_clear_of_the_border(self) -> None:
        """Raising the descender by its depth lands it on the baseline, clear."""
        upper = _ink_box(0, "g", 0.0, -0.3, 0.2, 0.3, ink=(((0.0, 0.0), (0.1, -0.3)),))
        border = _border_box(-0.34)
        (before,) = measure_glyph_gaps([upper], [border], required=0.05)
        assert before.collides is True
        # Raising by the descender depth puts the tip on the baseline (y = 0),
        # 0.34in above the border -- beyond the grid's cell, i.e. safely clear.
        lifted = ramp_ink(upper.sub_baseline_ink, 0.0, 1.0, 0.3)
        lifted_box = replace(upper, sub_baseline_ink=lifted)
        (after,) = measure_glyph_gaps([lifted_box], [border], required=0.05)
        assert math.isinf(after.gap)
        assert after.collides is False
