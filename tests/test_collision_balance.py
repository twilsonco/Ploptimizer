"""Unit tests for gap-balanced vertical centering (``collision/balance.py``).

Frame convention (the same one the module documents): every geometry is
label-local, y-up, with the label's **bottom edge at y = 0** and x centred on
the label (the stacked frame the renderer builds). The band-centred line ink is
lifted by ``origin``. ``origin = height / 2`` places a band-centred block on the
label's own centre.

Shared fixture: a 2in square label whose bottom border edge is ``y = 0``, a text
block 0.5in tall centred on the label (band-centred boxes ``[0, 0.25]`` for the
top line, ``[-0.25, 0]`` for the bottom line, 1.5in wide), and a lone
``center/top`` hole whose engraved edge sits at ``y = 1.65``. At the shipped
placement the top line is 0.40in from the hole and the bottom line 0.75in from
the border -- the imbalance the rule removes.

The solver measures **ink**, so the fixture models a line as its rectangular
outline (``_outline``): outside the block an outline and a filled box measure
the same gap, which keeps these numbers comparable with the box-metric history
while every value the solver reads still comes from strokes.
"""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from typing import Optional, Sequence, Tuple

import pytest

from plt_optimizer.core.units import HPGL_UNITS_PER_INCH
from plt_optimizer.generate.collision.balance import (
    BalanceCircle,
    BalanceGeometry,
    BalanceInputs,
    BalanceSolve,
    GapPair,
    balance_shift,
    lone_vertical_hole,
    measure_gaps,
    solve_balance,
    vertical_band,
)
from plt_optimizer.generate.resolution import ResolvedHoleSpec, ResolvedLabel

U = HPGL_UNITS_PER_INCH
UNIT = 1.0 / U

Box = Tuple[float, float, float, float]
Polyline = Tuple[Tuple[float, float], ...]


def _outline(box: Box) -> Polyline:
    """Return ``box``'s outline as a closed polyline -- the fixture's line ink."""
    x0, y0, x1, y1 = box
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0))


def _stroke(y: float, half_width: float = 0.75) -> Polyline:
    """Return one pen-down stroke at height ``y``, the width of the block."""
    return ((-half_width, y), (half_width, y))


def _flip_poly(poly: Polyline) -> Polyline:
    """Return ``poly`` mirrored about y = 0 (the band-centred frame)."""
    return tuple((x, -y) for x, y in poly)


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
    border_ink: Sequence[Polyline] = (BOTTOM_EDGE,),
    holes: Optional[Sequence[Tuple[float, float, float]]] = None,
    origin: float = HEIGHT / 2.0,
    min_delta: float = CAP_DOWN,
    max_delta: float = CAP_UP,
    top_box: Box = TOP_BOX,
    bottom_box: Box = BOTTOM_BOX,
    top_ink: Sequence[Polyline] = (),
    bottom_ink: Sequence[Polyline] = (),
) -> BalanceInputs:
    """Return solver inputs over the shared fixture.

    ``top_box``/``bottom_box`` describe the block's shape; they become ink via
    :func:`_outline` unless a case supplies the strokes itself.
    """
    return BalanceInputs(
        top_ink=tuple(top_ink) if top_ink else (_outline(top_box),),
        bottom_ink=tuple(bottom_ink) if bottom_ink else (_outline(bottom_box),),
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

    def test_a_line_with_no_ink_declines_the_solve(self) -> None:
        """Ink is required: a line that engraves nothing leaves nothing to balance."""
        inputs = _inputs(border_ink=((),))
        assert math.isinf(measure_gaps(inputs, 0.0).to_border)
        assert (
            balance_shift(
                _label(_TOP_HOLE),
                _geometry(block_bottom=1.0, block_top=1.4, holes=[], border_ink=()),
            )
            is None
        )

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
            top_ink=tuple(_flip_poly(poly) for poly in inputs.top_ink),
            bottom_ink=tuple(_flip_poly(poly) for poly in inputs.bottom_ink),
            origin=-inputs.origin,
            top_holes=tuple((cx, -cy, r) for cx, cy, r in inputs.top_holes),
            border_ink=tuple(_flip_poly(poly) for poly in inputs.border_ink),
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


def _geometry(
    *,
    block_bottom: float,
    block_top: float,
    holes: Sequence[BalanceCircle],
    border_ink: Sequence[Sequence[Tuple[float, float]]] = (((-1.0, 0.2), (1.0, 0.2)),),
    travel: float = 1.0,
) -> BalanceGeometry:
    """Return a :class:`BalanceGeometry` over a 2in-wide centered block.

    The block's faces carry a stroke each, so the ink the solver measures sits
    exactly where the swept faces are -- the numbers stay the hand-computable
    ones without reintroducing a box metric.
    """
    return BalanceGeometry(
        block_bottom=block_bottom,
        block_top=block_top,
        block_x_min=-0.75,
        block_x_max=0.75,
        holes=tuple(holes),
        border_ink=tuple(tuple(poly) for poly in border_ink),
        top_line_ink=(_stroke(block_top),),
        bottom_line_ink=(_stroke(block_bottom),),
        legacy_travel_inches=travel,
    )


_TOP_HOLE = [ResolvedHoleSpec(diameter=0.6, location="top")]


class TestBandLimitBindsTravelNotStart:
    """A block parked above the band must still balance (the real-job shape).

    The band caps how far the block may travel; it is not a box the shipped
    placement has to fit inside. A top-anchored layout parks the block above the
    band ceiling, so a symmetric clamp demands "top face below the ceiling"
    simultaneously with the hole-side cap, and the bracket inverts (``lo > hi``)
    -- which made the rule decline on every label of the job that motivated it.
    Each limit is therefore relaxed to the shipped face: it blocks further
    travel in its own direction, it never *demands* travel back toward the band
    (centering cannot shrink a block into a shorter band; spacing and
    compression own that).

    Fixture, label-local inches (+y up), on the 2in label of ``_label`` (band
    ``[0.2, 1.8]``): a top-anchored block ``[3.0, 3.5]``, a lone hole centred
    ``(0, 3.75)`` with r 0.15 (engraved edge y = 3.6), bottom border ink at
    y = 0.2, legacy centering travel 2.0in.

    * gap to the hole at delta 0: ``3.6 - 3.5 = 0.1``
    * gap to the border at delta 0: ``3.0 - 0.2 = 2.8``
    * equalizing: ``3.6 - 3.5 - d = 3.0 + d - 0.2`` -> ``d = -1.35``

    The un-relaxed ceiling gives ``max_delta = 1.8 - 3.5 = -1.7``, so the root
    at -1.35 lies outside the bracket and the rule declines.
    """

    # The hole's engraved edge (3.6) sits 0.1in above the block's top face.
    _HIGH_HOLE = BalanceCircle(cx=0.0, cy=3.75, radius=0.15)
    _BORDER = (((-1.0, 0.2), (1.0, 0.2)),)

    def _solve(self, *, travel: float = 2.0) -> Optional[BalanceSolve]:
        label = _label(_TOP_HOLE)
        geometry = _geometry(
            block_bottom=3.0,
            block_top=3.5,
            holes=[self._HIGH_HOLE],
            border_ink=self._BORDER,
            travel=travel,
        )
        return balance_shift(label, geometry)

    def test_parked_above_the_band_it_still_balances(self) -> None:
        """The regression: the root above the ceiling is reached, not declined."""
        solve = self._solve()
        assert solve is not None, "a block parked above the band must not disable the rule"
        assert solve.delta == pytest.approx(-1.35, abs=2 * UNIT)
        assert solve.to_hole == pytest.approx(solve.to_border, abs=2 * UNIT)
        assert solve.to_hole == pytest.approx(1.45, abs=2 * UNIT)

    def test_band_ceiling_still_blocks_ascent(self) -> None:
        """Relaxed, not removed: the block may travel down, never up past its face.

        The border ink here is 0.1in under the block's bottom face and the hole
        far above, so the equalizing root is a *rise* of +1.125in. The ceiling
        relaxes to the shipped top face -- it never demands travel, and it does
        not open a path upward either -- so no travel is available upward and
        the rule declines.
        """
        label = _label(_TOP_HOLE)
        geometry = _geometry(
            block_bottom=3.0,
            block_top=3.5,
            holes=[BalanceCircle(cx=0.0, cy=6.0, radius=0.15)],
            border_ink=(((-1.0, 2.9), (1.0, 2.9)),),
            travel=2.0,
        )
        solve = balance_shift(label, geometry)
        assert solve is None

    def test_block_inside_band_is_unchanged(self) -> None:
        """The un-violating case: identical bracket with and without the fix."""
        label = _label(_TOP_HOLE)
        geometry = _geometry(
            block_bottom=1.0,
            block_top=1.4,
            holes=[BalanceCircle(cx=0.0, cy=1.75, radius=0.10)],
            border_ink=self._BORDER,
            travel=1.0,
        )
        solve = balance_shift(label, geometry)
        assert solve is not None
        # gap to hole edge 1.65: 1.65 - 1.4 - d ; gap to border 0.2: 1.0 + d - 0.2
        # equalizing: 0.25 - d = 0.8 + d -> d = -0.275
        assert solve.delta == pytest.approx(-0.275, abs=2 * UNIT)

    def test_travel_cap_yields_the_best_effort_shift(self) -> None:
        """Root out of reach: apply the nearest cap, and report it as partial.

        The equalizing root is a -1.35in descent while the legacy centering only
        travels 0.01in, so the shift clamps to that cap: the gaps become
        ``0.11in`` to the hole and ``2.79in`` to the border, unequal by the
        ``2.68in`` residual. The shop gets the improvement the caps allow plus
        the imbalance they could not close.
        """
        solve = self._solve(travel=0.01)
        assert solve is not None
        assert solve.delta == pytest.approx(-0.01, abs=2 * UNIT)
        assert solve.partial
        assert solve.to_hole == pytest.approx(0.11, abs=2 * UNIT)
        assert solve.to_border == pytest.approx(2.79, abs=2 * UNIT)
        assert solve.residual == pytest.approx(2.68, abs=0.02)

    def test_exact_balance_is_not_reported_partial(self) -> None:
        """The ordinary case: the root inside the caps balances, not partial."""
        solve = self._solve()
        assert solve is not None
        assert not solve.partial
        assert solve.residual == pytest.approx(0.0, abs=2 * UNIT)


class TestInkMeasurement:
    """The gaps are ink distances measured on the strokes, not on the block.

    The fixture models each line as its rectangular outline; these cases use the
    line's real strokes instead, which sit just inside that outline (`0.03in`
    and `0.05in`), so the two metrics differ by exactly that inset. The numbers
    are the ones the contract names: a block's corners are not what the plotter
    cuts.
    """

    TOP_STROKE: Tuple[Tuple[float, float], ...] = ((-0.1, 0.22), (0.1, 0.22))
    BOTTOM_STROKE: Tuple[Tuple[float, float], ...] = ((-0.5, -0.20), (0.5, -0.20))

    def test_the_hole_side_measures_the_stroke_not_the_outline(self) -> None:
        """A stroke below the block's top face measures a wider hole gap."""
        outline_pair = measure_gaps(_inputs(), 0.0)
        ink_pair = measure_gaps(_inputs(top_ink=(self.TOP_STROKE,)), 0.0)
        assert outline_pair.to_hole == pytest.approx(0.40, abs=1e-9)
        assert ink_pair.to_hole == pytest.approx(0.43, abs=1e-9)

    def test_the_border_side_measures_the_stroke_not_the_outline(self) -> None:
        """A stroke above the block's bottom face measures a wider border gap."""
        outline_pair = measure_gaps(_inputs(), 0.0)
        ink_pair = measure_gaps(_inputs(bottom_ink=(self.BOTTOM_STROKE,)), 0.0)
        assert outline_pair.to_border == pytest.approx(0.75, abs=1e-9)
        assert ink_pair.to_border == pytest.approx(0.80, abs=1e-9)

    def test_solve_balances_the_ink_gaps(self) -> None:
        """The root equalizes 0.43 against 0.80, not the outline's 0.40/0.75."""
        inputs = _inputs(top_ink=(self.TOP_STROKE,), bottom_ink=(self.BOTTOM_STROKE,))
        delta = solve_balance(inputs)
        assert delta == pytest.approx(-0.185, abs=2 * UNIT)
        pair = measure_gaps(inputs, delta)
        assert pair.to_hole == pytest.approx(0.615, abs=2 * UNIT)
        assert pair.to_border == pytest.approx(0.615, abs=2 * UNIT)

    def test_each_side_measures_its_own_line(self) -> None:
        """A half-supplied input still solves: the sides never share a metric."""
        inputs = _inputs(top_ink=(self.TOP_STROKE,))
        pair = measure_gaps(inputs, 0.0)
        assert pair.to_hole == pytest.approx(0.43, abs=1e-9)
        assert pair.to_border == pytest.approx(0.75, abs=1e-9)
