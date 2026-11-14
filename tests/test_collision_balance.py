"""Unit tests for gap-balanced vertical centering (``collision/balance.py``).

Frame convention (the same one the module documents): every geometry is
label-local, y-up, with the label's **bottom edge at y = 0** and x centred on
the label (the stacked frame the renderer builds). The band-centred line boxes
are lifted by ``origin``. ``origin = height / 2`` places a band-centred block on
the label's own centre.

Shared fixture: a 2in square label whose bottom border edge is ``y = 0``, a text
block 0.5in tall centred on the label (band-centred boxes ``[0, 0.25]`` for the
top line, ``[-0.25, 0]`` for the bottom line, 1.5in wide), and a lone
``center/top`` hole whose engraved edge sits at ``y = 1.65``. At the shipped
placement the top line is 0.40in from the hole and the bottom line 0.75in from
the border -- the imbalance the rule removes.
"""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from typing import Optional, Sequence, Tuple

import pytest

from plt_optimizer.core.units import HPGL_UNITS_PER_INCH
from plt_optimizer.generate.collision.balance import (
    BalanceInputs,
    GapPair,
    box_loop,
    lone_vertical_hole,
    measure_gaps,
    outermost_hole_radii,
    solve_balance,
    vertical_band,
)
from plt_optimizer.generate.resolution import ResolvedHoleSpec, ResolvedLabel

U = HPGL_UNITS_PER_INCH
UNIT = 1.0 / U


def _flip_y(box: Tuple[float, float, float, float]) -> Tuple[float, float, float, float]:
    """Return ``box`` mirrored about y = 0 (the band-centred frame)."""
    return (box[0], -box[3], box[2], -box[1])


HEIGHT = 2.0
TOP_BOX: Tuple[float, float, float, float] = (-0.75, 0.0, 0.75, 0.25)
BOTTOM_BOX: Tuple[float, float, float, float] = (-0.75, -0.25, 0.75, 0.0)
HOLE: Tuple[float, float, float] = (0.0, 1.75, 0.10)  # engraved edge y = 1.65
BOTTOM_EDGE: Tuple[Tuple[float, float], ...] = ((-1.0, 0.0), (1.0, 0.0))
CAP_DOWN = -0.75  # band bottom (-0.25) sits on the border at origin 1.0
CAP_UP = 0.50  # top box's top edge (1.25) reaches the hole centre (1.75)


def _label(holes: Sequence[ResolvedHoleSpec] = (), height: float = HEIGHT) -> ResolvedLabel:
    """Return a resolved label carrying the given holes."""
    return ResolvedLabel(
        id="L1",
        count=1,
        width=2.0,
        height=height,
        margin=0.125,
        h_margin=0.125,
        v_margin=0.2,
        holes=list(holes),
    )


def _inputs(
    *,
    border_ink: Sequence[Tuple[Tuple[float, float], ...]] = (BOTTOM_EDGE,),
    holes: Optional[Sequence[Tuple[float, float, float]]] = None,
    origin: float = HEIGHT / 2.0,
    min_delta: float = CAP_DOWN,
    max_delta: float = CAP_UP,
    top_box: Tuple[float, float, float, float] = TOP_BOX,
    bottom_box: Tuple[float, float, float, float] = BOTTOM_BOX,
) -> BalanceInputs:
    """Return solver inputs over the shared fixture."""
    return BalanceInputs(
        top_box=top_box,
        bottom_box=bottom_box,
        origin=origin,
        top_holes=tuple([HOLE] if holes is None else holes),
        border_ink=tuple(border_ink),
        min_delta=min_delta,
        max_delta=max_delta,
    )


def _arc(
    radius: float = 1.0, half_width: float = 1.0, steps: int = 400
) -> Tuple[Tuple[float, float], ...]:
    """Return the lower arc of a round label's border, centred at ``(0, radius)``.

    Sampled by chord, so the polyline lies inside the true circle by the
    sagitta -- how the renderer samples its own border ink.
    """
    limit = min(half_width, radius - 1e-9)
    stop = math.acos(-limit / radius) if limit < radius else math.pi
    return tuple(
        (
            radius * math.cos(math.pi + stop * i / steps),
            radius + radius * math.sin(math.pi + stop * i / steps),
        )
        for i in range(steps + 1)
    )


class TestBoxLoop:
    """The band-centred box -> closed polyline lift."""

    def test_lifts_and_closes(self) -> None:
        loop = box_loop((0.0, 1.0, 2.0, 3.0), 10.0)
        assert loop[0] == (0.0, 11.0)
        assert loop[2] == (2.0, 13.0)  # the whole box lifts, not just y_min
        assert loop[-1] == loop[0]
        assert len(loop) == 5

    def test_zero_lift_is_the_box_outline(self) -> None:
        loop = box_loop(BOTTOM_BOX, 0.0)
        assert loop[0] == (BOTTOM_BOX[0], BOTTOM_BOX[1])
        assert loop[2] == (BOTTOM_BOX[2], BOTTOM_BOX[3])


class TestMeasureGaps:
    """The two gaps the rule equalizes, measured with the pipeline's metrics."""

    def test_fixture_gaps_at_the_shipped_placement(self) -> None:
        pair = measure_gaps(_inputs(), 0.0)
        assert pair.to_hole == pytest.approx(0.40, abs=1e-12)
        assert pair.to_border == pytest.approx(0.75, abs=1e-12)

    def test_hole_gap_shrinks_and_border_gap_grows_with_delta(self) -> None:
        inputs = _inputs()
        up = measure_gaps(inputs, 0.1)
        assert up.to_hole == pytest.approx(0.30, abs=1e-12)
        assert up.to_border == pytest.approx(0.85, abs=1e-12)

    def test_hole_gap_is_the_nearest_of_several(self) -> None:
        near = (0.0, 1.35, 0.10)  # engraved edge 1.25: the top box's own ceiling
        far = (-0.5, 3.0, 0.05)
        pair = measure_gaps(_inputs(holes=[far, near]), 0.0)
        assert pair.to_hole == pytest.approx(measure_gaps(_inputs(holes=[near]), 0.0).to_hole)

    def test_border_gap_is_a_true_2d_distance_not_a_vertical_drop(self) -> None:
        """The nearest point of the arc sits diagonally out from the corner.

        The bottom line's lower-right corner (-0.75, 0.75) is 0.25 above the
        circle's centre height, so the closest arc point is not straight below
        it: a y-band projection would report 0.92 - 0.75 = 0.17 while the true
        clearance is the 0.209 gap to the arc along the corner's own radial.
        """
        inputs = _inputs(border_ink=(_arc(half_width=0.8),), min_delta=-1.5, max_delta=0.5)
        pair = measure_gaps(inputs, 0.0)
        assert pair.to_border == pytest.approx(1.0 - math.hypot(0.75, 0.25), abs=2e-3)
        assert pair.to_border < 0.75  # the flat-edge answer, which overstates it

    def test_binding_is_the_smaller_gap(self) -> None:
        assert GapPair(to_hole=0.2, to_border=0.5).binding == 0.2
        assert GapPair(to_hole=0.7, to_border=0.3).binding == 0.3

    def test_missing_border_ink_leaves_the_hole_binding(self) -> None:
        pair = measure_gaps(_inputs(border_ink=()), 0.0)
        assert math.isinf(pair.to_border)
        assert pair.binding == pytest.approx(pair.to_hole)

    def test_border_ink_with_no_loop_is_unmeasurable(self) -> None:
        """An empty border loop has no outline to measure: the gap is infinite."""
        pair = measure_gaps(_inputs(border_ink=((),)), 0.0)
        assert pair.to_border == float("inf")
        assert pair.binding == pair.to_hole


class TestSolveBalance:
    """The equal-gap solve."""

    def test_equalizes_the_two_gaps(self) -> None:
        inputs = _inputs()
        delta = solve_balance(inputs)
        pair = measure_gaps(inputs, delta)
        # The hole-side gap (0.40) is the binding one, so the block moves AWAY
        # from the hole: (0.40 - 0.75) / 2 down, to the free band's midpoint.
        assert delta == pytest.approx(-0.175, abs=2 * UNIT)
        assert pair.to_hole == pytest.approx(pair.to_border, abs=2 * UNIT)
        assert pair.binding == pytest.approx(0.575, abs=2 * UNIT)

    def test_move_improves_the_binding_gap(self) -> None:
        inputs = _inputs()
        assert (
            measure_gaps(inputs, solve_balance(inputs)).binding > measure_gaps(inputs, 0.0).binding
        )

    def test_result_is_a_whole_number_of_device_units(self) -> None:
        delta = solve_balance(_inputs())
        assert delta == pytest.approx(round(delta * U) / U, abs=1e-12)

    def test_single_line_label_balances_both_sides_at_once(self) -> None:
        """Top and bottom line are the same box: the rule applies unchanged."""
        box = (-0.75, -0.25, 0.75, 0.25)
        inputs = _inputs(top_box=box, bottom_box=box)
        pair = measure_gaps(inputs, 0.0)
        assert pair.to_hole == pytest.approx(0.40, abs=1e-12)
        assert pair.to_border == pytest.approx(0.75, abs=1e-12)
        delta = solve_balance(inputs)
        solved = measure_gaps(inputs, delta)
        assert delta == pytest.approx(-0.175, abs=2 * UNIT)
        assert solved.to_hole == pytest.approx(solved.to_border, abs=2 * UNIT)

    def test_declines_without_holes(self) -> None:
        assert solve_balance(_inputs(holes=[])) == 0.0

    def test_declines_without_border_ink(self) -> None:
        assert solve_balance(_inputs(border_ink=())) == 0.0

    def test_declines_on_a_degenerate_bracket(self) -> None:
        assert solve_balance(_inputs(min_delta=0.5, max_delta=0.5)) == 0.0

    def test_declines_when_the_caps_exclude_the_root(self) -> None:
        # The root is -0.175 (away from the hole); these caps allow only upward
        # moves, which shrink an already-binding hole-side gap further.
        assert solve_balance(_inputs(min_delta=0.0, max_delta=0.5)) == 0.0

    def test_declines_when_the_border_side_binds_across_the_whole_bracket(self) -> None:
        """Every allowed move leaves the text closer to the border: no trade."""
        assert solve_balance(_inputs(min_delta=-0.75, max_delta=-0.5)) == 0.0

    def test_declines_when_the_hole_side_binds_across_the_whole_bracket(self) -> None:
        """The equal-gap point lies beyond the downward cap: no bracket to search."""
        assert solve_balance(_inputs(min_delta=0.05, max_delta=0.5)) == 0.0

    def test_declines_on_an_already_balanced_block(self) -> None:
        # Border at y = 0.35 makes both gaps 0.40 at the shipped placement, and
        # the caps stop at the border so no crossing can fake a sign change.
        inputs = _inputs(border_ink=(((-1.0, 0.35), (1.0, 0.35)),), min_delta=-0.4)
        pair = measure_gaps(inputs, 0.0)
        assert pair.to_hole == pytest.approx(pair.to_border, abs=1e-12)
        assert solve_balance(inputs) == 0.0

    def test_round_label_is_bound_by_the_corner_arc(self) -> None:
        """The same block on a round label moves the OTHER way: corners hug the arc."""
        inputs = _inputs(border_ink=(_arc(half_width=1.0),), min_delta=-1.5, max_delta=0.5)
        pair = measure_gaps(inputs, 0.0)
        # The bottom line's corners are 0.21 from the arc while its bottom edge
        # is 0.75 from the label's lowest point: the curve, not the vertex.
        assert pair.to_border == pytest.approx(0.2094, abs=2e-3)
        delta = solve_balance(inputs)
        solved = measure_gaps(inputs, delta)
        assert delta == pytest.approx(0.1555, abs=2 * UNIT)
        assert solved.to_hole == pytest.approx(solved.to_border, abs=2 * UNIT)
        # The flat-edge answer moves the block DOWN by 0.175 to a 0.575 clearance;
        # the arc inverts the direction and lands on 0.244.
        assert delta > 0.0

    def test_mirrored_label_mirrors_the_move(self) -> None:
        """A label flipped about its band centre flips the sign of the move.

        The bottom-hole case is the top-hole case seen from the other side, so
        the solve must bracket a root in either direction of the gap difference,
        not only the rising one.
        """
        inputs = _inputs()
        flipped = BalanceInputs(
            top_box=_flip_y(inputs.top_box),
            bottom_box=_flip_y(inputs.bottom_box),
            origin=-inputs.origin,
            top_holes=tuple((cx, -cy, r) for cx, cy, r in inputs.top_holes),
            border_ink=tuple(tuple((x, -y) for x, y in poly) for poly in inputs.border_ink),
            min_delta=-inputs.max_delta,
            max_delta=-inputs.min_delta,
        )
        pair = measure_gaps(flipped, 0.0)
        shipped = measure_gaps(inputs, 0.0)
        assert pair.to_hole == pytest.approx(shipped.to_hole)
        assert pair.to_border == pytest.approx(shipped.to_border)
        assert solve_balance(flipped) == pytest.approx(-solve_balance(inputs))

    def test_is_deterministic(self) -> None:
        inputs = _inputs()
        assert [solve_balance(inputs) for _ in range(5)] == [solve_balance(inputs)] * 5

    def test_logs_the_gaps_it_found(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger="plt_optimizer.generate.collision.balance")
        solve_balance(_inputs())
        balance_logs = [r.message for r in caplog.records if "hole-side gap" in r.message]
        assert len(balance_logs) == 1
        assert "0.4000in" in balance_logs[0]
        assert "0.7500in" in balance_logs[0]


class TestBandLimits:
    """The caps the solve searches within, and the hole sizes that set them."""

    def test_band_is_the_label_box_less_each_side_s_clearance(self) -> None:
        label = replace(
            _label(),
            hole_margin=0.2,
            hole_cutter_diameter=0.05,
            hole_adjust_for_cutter=True,
        )
        # Lower: hole_margin + kerf + the other side's hole, off the centre.
        assert vertical_band(label, 0.30, 0.10) == pytest.approx((-0.65, 0.45))

    def test_band_ignores_the_cutter_kerf_when_unadjusted(self) -> None:
        label = replace(
            _label(),
            hole_margin=0.2,
            hole_cutter_diameter=0.05,
            hole_adjust_for_cutter=False,
        )
        assert vertical_band(label, 0.30, 0.10) == pytest.approx((-0.70, 0.50))

    def test_radii_pick_the_largest_hole_on_each_side(self) -> None:
        label = _label(
            [
                ResolvedHoleSpec(diameter=0.25, location="top"),
                ResolvedHoleSpec(diameter=0.30, location="top"),
                ResolvedHoleSpec(diameter=0.10, location="bottom"),
            ]
        )
        assert outermost_hole_radii(label, "top") == (0.30, 0.10)
        assert outermost_hole_radii(label, "bottom") == (0.10, 0.30)

    def test_radii_are_zero_on_a_side_without_holes(self) -> None:
        assert outermost_hole_radii(_label(), "top") == (0.0, 0.0)


class TestLoneVerticalHole:
    """The one-sided gate the rule shares with the hole-aware shift."""

    def test_top_only(self) -> None:
        label = _label([ResolvedHoleSpec(diameter=0.125, location="top")])
        assert lone_vertical_hole(label) == "top"

    def test_bottom_only(self) -> None:
        label = _label([ResolvedHoleSpec(diameter=0.125, location="bottom")])
        assert lone_vertical_hole(label) == "bottom"

    def test_both_sides_is_not_lone(self) -> None:
        label = _label(
            [
                ResolvedHoleSpec(diameter=0.125, location="top"),
                ResolvedHoleSpec(diameter=0.125, location="bottom"),
            ]
        )
        assert lone_vertical_hole(label) is None

    def test_no_holes_is_not_lone(self) -> None:
        assert lone_vertical_hole(_label()) is None

    def test_corner_holes_are_not_axis_holes(self) -> None:
        """Corner holes take part in neither this rule nor the shift."""
        label = _label(
            [
                ResolvedHoleSpec(diameter=0.125, location="top-left"),
                ResolvedHoleSpec(diameter=0.125, location="bottom-right"),
            ]
        )
        assert lone_vertical_hole(label) is None

    def test_zero_diameter_holes_do_not_count(self) -> None:
        label = _label([ResolvedHoleSpec(diameter=0.0, location="top")])
        assert lone_vertical_hole(label) is None


class TestGeometryHelpersAgainstReferenceCircle:
    """The solver's gap pair, cross-checked against closed-form geometry."""

    @pytest.mark.parametrize("delta", [-0.4, -0.1, 0.0, 0.2, 0.45])
    def test_rect_gaps_match_hand_arithmetic(self, delta: float) -> None:
        pair = measure_gaps(_inputs(), delta)
        assert pair.to_hole == pytest.approx(1.65 - (1.0 + 0.25 + delta), abs=1e-9)
        assert pair.to_border == pytest.approx(1.0 - 0.25 + delta - 0.0, abs=1e-9)

    def test_balanced_placement_is_the_band_midpoint(self) -> None:
        """Equal gaps <=> the block centre is the midpoint of the free band."""
        inputs = _inputs()
        delta = solve_balance(inputs)
        block_centre = 1.0 + delta
        # The centre ranges from half a block above the border (0.25) to half a
        # block below the hole's engraved edge (1.40).
        free_band_mid = (0.25 + 1.40) / 2.0
        assert block_centre == pytest.approx(free_band_mid, abs=2 * UNIT)

    def test_sweep_over_a_circle_finds_the_radial_minimum(self) -> None:
        """The arc gap is minimized at the corner's radial, not below it."""
        inputs = _inputs(border_ink=(_arc(half_width=1.0),), min_delta=-1.5, max_delta=0.5)
        pair = measure_gaps(inputs, 0.0)
        corner = (0.75, 0.75)  # bottom line's lower-right corner, label-local
        radial = 1.0 - math.hypot(corner[0], corner[1] - 1.0)
        straight_down = 1.0 - math.sqrt(max(0.0, 1.0 - corner[0] ** 2)) - (corner[1] - 1.0)
        assert pair.to_border == pytest.approx(radial, abs=2e-3)
        assert straight_down > radial
