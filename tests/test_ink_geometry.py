"""Tests for the ink-to-ink gap primitives used by gap-balanced centering.

Covers :func:`plt_optimizer.generate.geometry.polyline_circle_gap`,
:func:`plt_optimizer.generate.geometry.ink_circle_gap` and
:meth:`plt_optimizer.generate.text_geometry.TextBlock.as_ink`.

The metric contract these pin (docs/plans/balanced-hole-centering.md, Stage 6):
distance A is the minimum distance between a drill hole and any stroke of a text
line, so it must measure real sampled ink -- never a bounding box, and never an
arc's chord.
"""

from __future__ import annotations

import math

import pytest

from plt_optimizer.generate.collision.balance import top_and_bottom_line_ink
from plt_optimizer.generate.geometry import ink_circle_gap, polyline_circle_gap
from plt_optimizer.generate.text_geometry import (
    ARC_FLATTEN_CHORD_TOL_INCHES,
    ArcSeg,
    LineSeg,
    Stroke,
    TextBlock,
)

# A 1-inch-radius circle at the origin, the shape every case measures against.
CENTER = (0.0, 0.0)
RADIUS = 1.0


class TestPolylineCircleGap:
    """Signed clearance between a circle's perimeter and one ink chain."""

    def test_centre_aligned_segment_measures_from_the_perimeter(self) -> None:
        """A segment straight below the circle reports distance-minus-radius."""
        # A horizontal run at y = -2, i.e. 2in from the centre -> 1in clearance.
        chain = ((-3.0, -2.0), (3.0, -2.0))
        assert polyline_circle_gap(chain, CENTER, RADIUS) == pytest.approx(1.0)

    def test_tangent_segment_is_zero(self) -> None:
        """A segment touching the perimeter tangentially has zero clearance."""
        chain = ((-2.0, -1.0), (2.0, -1.0))
        assert polyline_circle_gap(chain, CENTER, RADIUS) == pytest.approx(0.0, abs=1e-12)

    def test_crossing_segment_is_negative(self) -> None:
        """Ink reaching inside the perimeter reports a negative clearance."""
        # A chord through the centre: the nearest ink is at distance 0 from the
        # centre, so the signed clearance is 0 - radius.
        chain = ((-2.0, 0.0), (2.0, 0.0))
        assert polyline_circle_gap(chain, CENTER, RADIUS) == pytest.approx(-RADIUS)

    def test_closest_point_is_an_interior_vertex(self) -> None:
        """A V shape's apex decides, not either endpoint."""
        # The apex (0, -1.5) is 1.5 from the centre; the arms end far away.
        chain = ((-5.0, -5.0), (0.0, -1.5), (5.0, -5.0))
        assert polyline_circle_gap(chain, CENTER, RADIUS) == pytest.approx(0.5)

    def test_diagonal_segment(self) -> None:
        """A 45-degree run measures the true perpendicular distance."""
        # The line x + y = -2 * sqrt(2) sits 2in from the centre along its
        # normal, whichever of its points is closest.
        d = 2.0 * math.sqrt(2.0)
        chain = ((-d, 0.0), (0.0, -d))
        assert polyline_circle_gap(chain, CENTER, RADIUS) == pytest.approx(1.0, abs=1e-9)

    def test_single_point_chain_is_a_dot_of_ink(self) -> None:
        """A degenerate chain (an ``i``'s tittle) still measures."""
        assert polyline_circle_gap(((0.0, -3.0),), CENTER, RADIUS) == pytest.approx(2.0)

    def test_empty_chain_has_no_ink(self) -> None:
        """An empty chain reports ``inf`` so it can never bind a minimum."""
        assert polyline_circle_gap((), CENTER, RADIUS) == math.inf

    def test_ink_beyond_the_radius_on_the_far_side_is_positive(self) -> None:
        """The sign tracks inside/outside, not which side of the circle."""
        chain = ((0.0, 4.0), (1.0, 4.0))
        assert polyline_circle_gap(chain, CENTER, RADIUS) == pytest.approx(3.0)


class TestInkCircleGap:
    """The circle gap over a whole line's ink: the closest chain binds."""

    def test_closest_chain_wins(self) -> None:
        """The minimum is taken across chains, in any order."""
        near = ((-1.0, -1.5), (1.0, -1.5))
        far = ((-5.0, -5.0), (-4.0, -5.0))
        assert ink_circle_gap((near, far), CENTER, RADIUS) == pytest.approx(0.5)
        assert ink_circle_gap((far, near), CENTER, RADIUS) == pytest.approx(0.5)

    def test_penetrating_chain_wins_over_distant_ones(self) -> None:
        """A chain through the circle overrides every separated one."""
        ink = (
            ((-9.0, -9.0), (-8.0, -9.0)),
            ((-1.0, 0.0), (1.0, 0.0)),
        )
        assert ink_circle_gap(ink, CENTER, RADIUS) == pytest.approx(-RADIUS)

    def test_empty_ink(self) -> None:
        """An empty line has nothing to measure."""
        assert ink_circle_gap((), CENTER, RADIUS) == math.inf


class TestTextBlockAsInk:
    """``TextBlock.as_ink`` flattens a rendered line into measurable polylines."""

    def test_straight_strokes_become_chains(self) -> None:
        """Each pen-down run is one chain of its own vertices."""
        block = TextBlock(
            strokes=(
                Stroke(
                    pen_up=0j,
                    segments=(LineSeg(0j, 1 + 0j), LineSeg(1 + 0j, 1 + 1j)),
                ),
            )
        )
        ink = block.as_ink()
        assert len(ink) == 1
        assert ink[0] == ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0))

    def test_arcs_are_sampled_not_chorded(self) -> None:
        """An arc contributes many points that lie on the true circle.

        The chord-only implementation would return the two endpoints and
        measure every gap to the arc as the distance to that chord -- off by the
        sagitta, which for a 90-degree unit arc is ~0.29in.
        """
        arc = ArcSeg(start=1 + 0j, center=0j, sweep_deg=90.0)
        ink = TextBlock(strokes=(Stroke(pen_up=1 + 0j, segments=(arc,)),)).as_ink()

        assert len(ink) == 1
        chain = ink[0]
        assert len(chain) > 2, "an arc must be sampled, not reduced to a chord"
        for x, y in chain:
            assert math.hypot(x, y) == pytest.approx(1.0, abs=ARC_FLATTEN_CHORD_TOL_INCHES)
        # The endpoints are preserved exactly, so the chain chains tip-to-tail.
        assert chain[0] == pytest.approx((1.0, 0.0))
        assert chain[-1] == pytest.approx((0.0, 1.0))
        # Every sampled point is at or beyond the midpoint's chord: the sagitta
        # check that proves this is the arc and not its chord.
        midpoint_on_arc = (math.sqrt(0.5), math.sqrt(0.5))
        assert min(
            math.hypot(x - midpoint_on_arc[0], y - midpoint_on_arc[1]) for x, y in chain
        ) == pytest.approx(0.0, abs=ARC_FLATTEN_CHORD_TOL_INCHES)

    def test_empty_block_has_no_ink(self) -> None:
        """A line with nothing engraved returns an empty ink tuple."""
        assert TextBlock.empty().as_ink() == ()

    def test_stroke_without_segments_contributes_no_ink(self) -> None:
        """A pen-down move with no segments yields no chain."""
        block = TextBlock(strokes=(Stroke(pen_up=0j, segments=()),))
        assert block.as_ink() == ()

    def test_ink_of_a_one_line_label_serves_both_gaps(self) -> None:
        """One line: the hole-side and border-side gaps measure the same ink.

        This is the contract's one-line case -- no special path, the same ink
        set is simply read twice.
        """
        block = TextBlock(
            strokes=(Stroke(pen_up=0j, segments=(LineSeg(0j, 2 + 0j),)),),
        )
        ink = block.as_ink()

        radius = 0.5
        above, below = (0.0, radius), (0.0, -radius)
        assert ink_circle_gap(ink, above, radius) == pytest.approx(0.0, abs=1e-12)
        assert ink_circle_gap(ink, below, radius) == pytest.approx(0.0, abs=1e-12)


class TestTopAndBottomLineInk:
    """The shared line selector both centering adapters feed the solve."""

    def test_extremes_by_ink_extent_not_by_position(self) -> None:
        """Lines are ranked on what they engrave, however they are ordered."""
        low = (((0.0, 0.0), (1.0, 0.0)),)
        mid = (((0.0, 0.5), (1.0, 0.5)),)
        high = (((0.0, 1.0), (1.0, 1.0)),)
        ctx = (mid, high, low)
        assert top_and_bottom_line_ink(ctx) == (high, low)

    def test_one_line_answers_both_sides(self) -> None:
        """A one-line label hands the same ink to both gaps."""
        only = (((0.0, 0.0), (1.0, 0.0)),)
        assert top_and_bottom_line_ink([only]) == (only, only)

    def test_no_ink_leaves_the_box_fallback(self) -> None:
        """Nothing measurable returns two empties, which selects the box path."""
        assert top_and_bottom_line_ink([(), ()]) == ((), ())

    def test_empty_polylines_do_not_rank_a_line(self) -> None:
        """A line carrying only empty polylines cannot win either extreme."""
        real = (((0.0, 0.0), (1.0, 0.0)),)
        assert top_and_bottom_line_ink([real, ((),)]) == (real, real)
