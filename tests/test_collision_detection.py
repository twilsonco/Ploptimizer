"""Unit tests for the collision-avoidance geometric primitives.

Covers ``plt_optimizer.generate.geometry``: the signed circle/AABB
clearance used by the text-hole collision detection system, the swept-ink
distance primitives backing the descending-character relief's rolling-circle
test (segment/polyline/ink gaps and the spatial grid accelerating them),
including edge cases (tangency, containment, adjacency, X-disjoint near
misses) that must not produce false positives or false negatives.
"""

from __future__ import annotations

import dataclasses
import math
import random

import pytest

from plt_optimizer.generate.geometry import (
    CollisionResult,
    Ink,
    InkGrid,
    aabb_vertical_gap,
    arc_swept_bounds,
    circle_aabb_gap,
    ink_gap,
    point_segment_gap,
    polyline_gap,
    segment_gap,
)


class TestCircleAabbGap:
    """Tests for the signed circle/AABB clearance function."""

    def test_separated_circle_reports_positive_gap(self) -> None:
        """A circle to the right of the box reports the exact clearance."""
        # Box [0,1]x[0,1]; circle center (2.0, 0.5) radius 0.25.
        gap = circle_aabb_gap((0.0, 0.0, 1.0, 1.0), (2.0, 0.5), 0.25)
        assert math.isclose(gap, 0.75)

    def test_penetrating_circle_reports_negative_gap(self) -> None:
        """A circle overlapping the box reports negative clearance."""
        # Circle center (1.1, 0.5) radius 0.25: center 0.1in from the
        # right edge, so the circle digs 0.15in into the box.
        gap = circle_aabb_gap((0.0, 0.0, 1.0, 1.0), (1.1, 0.5), 0.25)
        assert math.isclose(gap, -0.15)

    def test_tangent_circle_reports_zero_gap(self) -> None:
        """Exact tangency must report zero (not a collision)."""
        gap = circle_aabb_gap((0.0, 0.0, 1.0, 1.0), (1.25, 0.5), 0.25)
        assert math.isclose(gap, 0.0, abs_tol=1e-12)

    def test_center_inside_box_reports_negative_radius(self) -> None:
        """A circle centered inside the box reports -radius."""
        gap = circle_aabb_gap((0.0, 0.0, 1.0, 1.0), (0.5, 0.5), 0.25)
        assert math.isclose(gap, -0.25)

    def test_corner_distance_uses_diagonal(self) -> None:
        """Distance to a corner is the Euclidean diagonal distance."""
        # Box [0,1]x[0,1]; circle center (2.0, 2.0) radius 0.5. Closest
        # point is the corner (1,1): distance sqrt(2).
        gap = circle_aabb_gap((0.0, 0.0, 1.0, 1.0), (2.0, 2.0), 0.5)
        assert math.isclose(gap, math.sqrt(2.0) - 0.5)

    def test_zero_radius_point_inside_box(self) -> None:
        """A zero-radius circle (point) inside the box reports 0."""
        gap = circle_aabb_gap((0.0, 0.0, 1.0, 1.0), (0.5, 0.5), 0.0)
        assert gap == 0.0

    def test_zero_radius_point_outside_box(self) -> None:
        """A zero-radius circle outside the box reports plain distance."""
        gap = circle_aabb_gap((0.0, 0.0, 1.0, 1.0), (3.0, 0.5), 0.0)
        assert math.isclose(gap, 2.0)


class TestCollisionResult:
    """Tests for the CollisionResult dataclass contract."""

    def test_is_frozen(self) -> None:
        """CollisionResult must be immutable."""
        result = CollisionResult(
            line_index=0, line_text="X", hole_index=1, hole_location="left", gap=-0.1
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.gap = 0.0  # type: ignore[misc]

    def test_fields_round_trip(self) -> None:
        """All fields must be readable as provided."""
        result = CollisionResult(
            line_index=2, line_text="DANGER", hole_index=0, hole_location="top-left", gap=-0.25
        )
        assert result.line_index == 2
        assert result.line_text == "DANGER"
        assert result.hole_index == 0
        assert result.hole_location == "top-left"
        assert math.isclose(result.gap, -0.25)


class TestArcSweptBounds:
    """Tests for the swept-arc bounding box used by arc-native text."""

    def test_quarter_arc_first_quadrant(self) -> None:
        """A 0->90 deg quarter arc stays in the first quadrant."""
        x_min, y_min, x_max, y_max = arc_swept_bounds(0.0, 0.0, 1.0, 0.0, 90.0)
        assert x_min == pytest.approx(0.0, abs=1e-12)
        assert y_min == pytest.approx(0.0, abs=1e-12)
        assert x_max == pytest.approx(1.0, abs=1e-12)
        assert y_max == pytest.approx(1.0, abs=1e-12)

    def test_full_circle_reports_whole_circle(self) -> None:
        """A 360 deg sweep must report the full circle box."""
        x_min, y_min, x_max, y_max = arc_swept_bounds(2.0, 3.0, 1.5, 0.0, 360.0)
        assert x_min == pytest.approx(0.5)
        assert x_max == pytest.approx(3.5)
        assert y_min == pytest.approx(1.5)
        assert y_max == pytest.approx(4.5)

    def test_negative_sweep_matches_positive(self) -> None:
        """Sweep direction never changes the swept footprint."""
        forward = arc_swept_bounds(0.0, 0.0, 1.0, 0.0, 90.0)
        backward = arc_swept_bounds(0.0, 0.0, 1.0, 90.0, -90.0)
        assert forward == pytest.approx(backward, abs=1e-12)

    def test_zero_sweep_is_endpoint(self) -> None:
        """A zero sweep degenerates to the single start point."""
        box = arc_swept_bounds(1.0, 1.0, 2.0, 30.0, 0.0)
        expected_x = 1.0 + 2.0 * math.cos(math.radians(30.0))
        expected_y = 1.0 + 2.0 * math.sin(math.radians(30.0))
        assert box == pytest.approx((expected_x, expected_y, expected_x, expected_y))

    def test_shallow_huge_radius_arc_stays_tight(self) -> None:
        """A near-straight huge-radius arc must not report its full circle.

        EngraveLab approximates straight glyph strokes with arcs of radius
        thousands of plotter units; using the full circle would explode every
        glyph's footprint.
        """
        radius = 4000.0
        _x_min, y_min, _x_max, y_max = arc_swept_bounds(0.0, 0.0, radius, 90.0, 1.0)
        assert y_max - y_min < 1.0
        assert y_max <= radius + 1e-9

    def test_cardinal_angles_included(self) -> None:
        """A sweep crossing 90 deg reaches the top of the circle."""
        _x_min, _y_min, _x_max, y_max = arc_swept_bounds(0.0, 0.0, 1.0, 45.0, 90.0)
        assert y_max == pytest.approx(1.0)


class TestAabbVerticalGap:
    """Tests for the signed vertical clearance between two boxes."""

    def test_separated_boxes_report_positive_gap(self) -> None:
        """Boxes stacked with air between them report that air."""
        assert aabb_vertical_gap((0.0, 1.0, 1.0, 2.0), (0.0, 0.0, 1.0, 0.5)) == 0.5

    def test_touching_boxes_report_zero(self) -> None:
        """Tangency is a gap of exactly zero (safe, not a collision)."""
        assert aabb_vertical_gap((0.0, 1.0, 1.0, 2.0), (0.0, 0.0, 1.0, 1.0)) == 0.0

    def test_overlap_reports_negative_penetration(self) -> None:
        """An overlap reports the (negative) penetration depth."""
        assert aabb_vertical_gap((0.0, 0.5, 1.0, 2.0), (0.0, 0.0, 1.0, 1.0)) == -0.5

    def test_gap_measured_only_over_the_overlapping_x_span(self) -> None:
        """The partial-X-overlap case measures the vertical gap, not the lateral one."""
        upper = (0.0, 1.0, 2.0, 2.0)
        lower = (1.5, 0.0, 3.5, 0.75)
        assert aabb_vertical_gap(upper, lower) == pytest.approx(0.25)

    def test_disjoint_x_spans_report_infinity(self) -> None:
        """Laterally distant boxes can never collide, so they are infinitely far."""
        assert math.isinf(aabb_vertical_gap((0.0, 1.0, 1.0, 2.0), (5.0, 0.0, 6.0, 0.5)))

    def test_touching_x_edges_report_infinity(self) -> None:
        """A zero-width X overlap is no overlap at all."""
        assert math.isinf(aabb_vertical_gap((0.0, 1.0, 1.0, 2.0), (1.0, 0.0, 2.0, 0.5)))

    def test_is_frame_agnostic(self) -> None:
        """A shared translation leaves the gap unchanged (y-up and y-down alike)."""
        upper = (0.0, 1.0, 1.0, 2.0)
        lower = (0.0, 0.0, 1.0, 0.5)
        shift = -3.7
        moved = (
            (upper[0], upper[1] + shift, upper[2], upper[3] + shift),
            (lower[0], lower[1] + shift, lower[2], lower[3] + shift),
        )
        assert aabb_vertical_gap(*moved) == pytest.approx(aabb_vertical_gap(upper, lower))


class TestPointSegmentGap:
    """Tests for the point-to-segment distance primitive."""

    def test_perpendicular_foot_lands_inside_the_segment(self) -> None:
        """The closest point is the projection, not an endpoint."""
        assert point_segment_gap((0.5, 1.0), (0.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)

    def test_projection_beyond_the_end_clamps_to_the_endpoint(self) -> None:
        """A point past the segment's end measures the endpoint distance."""
        assert point_segment_gap((2.0, 0.0), (0.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)

    def test_projection_before_the_start_clamps_to_the_start(self) -> None:
        """A point before the segment's start measures the start distance."""
        assert point_segment_gap((-1.0, 0.0), (0.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)

    def test_point_on_the_segment_reports_zero(self) -> None:
        """A point on the segment has no clearance."""
        assert point_segment_gap((0.25, 0.0), (0.0, 0.0), (1.0, 0.0)) == 0.0

    def test_degenerate_segment_measures_the_point_distance(self) -> None:
        """A zero-length segment (a dot of ink) behaves like a point."""
        assert point_segment_gap((0.0, 3.0), (1.0, 3.0), (1.0, 3.0)) == pytest.approx(1.0)


class TestSegmentGap:
    """Tests for the segment-to-segment distance primitive."""

    def test_crossing_segments_report_zero(self) -> None:
        """Crossing strokes have no clearance (the endpoint-only trap).

        The minimum of the four endpoint-to-segment distances would report a
        false positive here; the closest-points solution returns 0.
        """
        assert segment_gap((0.0, 0.0), (1.0, 1.0), (0.0, 1.0), (1.0, 0.0)) == 0.0

    def test_parallel_segments_report_the_perpendicular_distance(self) -> None:
        """Two parallel runs measure the perpendicular offset."""
        gap = segment_gap((0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0))
        assert gap == pytest.approx(1.0)

    def test_diagonal_side_by_side_reports_the_perpendicular_distance(self) -> None:
        """The closest points are interior on both segments."""
        gap = segment_gap((0.0, 0.0), (1.0, 1.0), (1.0, 0.0), (2.0, 1.0))
        assert gap == pytest.approx(math.sqrt(0.5))

    def test_endpoint_nearest_pair(self) -> None:
        """A pair whose closest points are endpoints measures that distance."""
        assert segment_gap((0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (2.0, 1.0)) == pytest.approx(1.0)

    def test_x_disjoint_near_pair_is_finite(self) -> None:
        """The relief's blind spot: a sideways near-miss measures its real distance.

        ``aabb_vertical_gap`` reports ``inf`` for this pair (disjoint X spans),
        which is exactly how a ``g`` hook reaching an ``i`` dot beside it hid
        from the box model.
        """
        gap = segment_gap((0.0, 0.0), (0.1, 0.0), (0.15, -0.2), (0.15, -0.1))
        assert gap == pytest.approx(math.hypot(0.05, 0.1))
        assert math.isinf(aabb_vertical_gap((0.0, 0.0, 0.1, 0.0), (0.15, -0.2, 0.15, -0.1)))

    def test_two_degenerate_segments_measure_the_point_distance(self) -> None:
        """Two dots of ink (e.g. two tittles) measure point distance."""
        assert segment_gap((0.0, 0.0), (0.0, 0.0), (0.0, 2.0), (0.0, 2.0)) == pytest.approx(2.0)

    def test_one_degenerate_segment_measures_point_to_segment(self) -> None:
        """A dot against a run measures the point-to-segment distance."""
        assert segment_gap((0.5, 1.0), (0.5, 1.0), (0.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)

    def test_is_frame_agnostic(self) -> None:
        """A shared translation leaves the distance unchanged."""
        shift = complex(-2.5, 4.25)
        base = segment_gap((0.0, 0.0), (1.0, 1.0), (1.0, 0.0), (2.0, 1.0))
        moved = segment_gap(
            (shift.real, shift.imag),
            (1.0 + shift.real, 1.0 + shift.imag),
            (1.0 + shift.real, shift.imag),
            (2.0 + shift.real, 1.0 + shift.imag),
        )
        assert moved == pytest.approx(base)


class TestPolylineGap:
    """Tests for the polyline-to-polyline minimum distance."""

    def test_empty_polyline_reports_infinity(self) -> None:
        """A chain with no ink can never collide."""
        assert math.isinf(polyline_gap((), ((0.0, 0.0), (1.0, 1.0))))
        assert math.isinf(polyline_gap(((0.0, 0.0), (1.0, 1.0)), ()))

    def test_single_vertex_polyline_is_a_dot(self) -> None:
        """A one-vertex chain (a tittle) measures its distance to the chain."""
        gap = polyline_gap(((0.0, 1.0),), ((0.0, 0.0), (1.0, 0.0)))
        assert gap == pytest.approx(1.0)

    def test_nearest_pair_across_chains_wins(self) -> None:
        """The minimum is taken over every segment pair, not the first."""
        upper = ((0.0, 5.0), (1.0, 5.0), (2.0, 0.1), (3.0, 0.1))
        lower = ((2.0, 0.0), (3.0, 0.0))
        assert polyline_gap(upper, lower) == pytest.approx(0.1)

    def test_touching_chains_report_zero(self) -> None:
        """Chains sharing a junction vertex have no clearance."""
        upper = ((0.0, 0.0), (1.0, 0.0))
        lower = ((1.0, 0.0), (1.0, 1.0))
        assert polyline_gap(upper, lower) == 0.0


class TestInkGap:
    """Tests for the glyph-ink-to-glyph-ink minimum distance."""

    def test_empty_ink_reports_infinity(self) -> None:
        """A glyph with no ink (a space) can never collide."""
        assert math.isinf(ink_gap((), (((0.0, 0.0), (1.0, 0.0)),)))

    def test_minimum_spans_all_polyline_pairs(self) -> None:
        """The nearest pair of strokes across the two glyphs wins."""
        upper = (((0.0, 9.0), (1.0, 9.0)), ((4.0, 0.2), (5.0, 0.2)))
        lower = (((0.0, 0.0), (1.0, 0.0)), ((4.0, 0.0), (5.0, 0.0)))
        assert ink_gap(upper, lower) == pytest.approx(0.2)


class TestInkGrid:
    """Tests for the spatial-hash grid accelerating the relief's inner loop.

    The grid must never change a decision: below its cell size it reports the
    exact :func:`ink_gap`, and above it only ever reports *more* air (it tests
    a subset of the pairs, so it can never invent a collision).
    """

    @staticmethod
    def _random_ink(seed: int, region: float, polylines: int, vertices: int) -> Ink:
        """Build deterministic pseudo-random ink inside ``[0, region]^2``."""
        rng = random.Random(seed)
        return tuple(
            tuple(
                (rng.uniform(0.0, region), rng.uniform(0.0, region))
                for _ in range(rng.randint(1, vertices))
            )
            for _ in range(polylines)
        )

    def test_exact_below_the_cell_size(self) -> None:
        """Every pair closer than the cell size measures its exact distance."""
        for seed in range(60):
            lower = self._random_ink(seed, region=3.0, polylines=3, vertices=5)
            upper = self._random_ink(seed + 1000, region=3.0, polylines=2, vertices=5)
            for cell in (0.05, 0.1, 0.25, 0.5):
                true_gap = ink_gap(upper, lower)
                measured = InkGrid(lower, cell).min_gap(upper)
                if true_gap < cell:
                    assert measured == pytest.approx(true_gap, abs=1e-9), (seed, cell)

    def test_never_invents_a_collision(self) -> None:
        """Above the cell size the grid is an upper bound, never a false positive."""
        for seed in range(60):
            lower = self._random_ink(seed, region=3.0, polylines=3, vertices=5)
            upper = self._random_ink(seed + 500, region=3.0, polylines=2, vertices=5)
            for cell in (0.05, 0.25):
                true_gap = ink_gap(upper, lower)
                measured = InkGrid(lower, cell).min_gap(upper)
                assert measured >= true_gap - 1e-9, (seed, cell)

    def test_empty_sides_report_infinity(self) -> None:
        """No ink on either side is unmeasurable."""
        assert math.isinf(InkGrid((), 0.1).min_gap((((0.0, 0.0), (1.0, 0.0)),)))
        assert math.isinf(InkGrid((((0.0, 0.0), (1.0, 0.0)),), 0.1).min_gap(()))

    def test_single_vertex_ink_is_indexed(self) -> None:
        """A dot of ink (a tittle) is found by the grid."""
        grid = InkGrid((((1.0, 0.0),),), 1.0)
        assert grid.min_gap((((1.0, 0.5),),)) == pytest.approx(0.5)

    def test_beyond_the_cell_size_reports_infinity(self) -> None:
        """A pair farther apart than the cell is unmeasured (``inf``)."""
        grid = InkGrid((((1.0, 0.0),),), 0.1)
        assert math.isinf(grid.min_gap((((1.0, 0.5),),)))

    def test_non_positive_cell_size_falls_back_to_a_usable_cell(self) -> None:
        """A zero required clearance must not create zero-area cells."""
        grid = InkGrid((((0.0, 0.0), (1.0, 0.0)),), 0.0)
        assert grid.cell_size > 0.0
        assert grid.min_gap((((0.0, grid.cell_size / 2.0), (1.0, grid.cell_size / 2.0)),))
        # ...and the fallback cell measures distances inside itself exactly.
        half = grid.cell_size / 2.0
        assert grid.min_gap((((0.0, half), (1.0, half)),)) == pytest.approx(half)

    def test_negative_coordinates_index_correctly(self) -> None:
        """The grid spans the whole plane, including negative coordinates."""
        lower = (((0.0, -1.0), (1.0, -1.0)),)
        grid = InkGrid(lower, 0.5)
        assert grid.min_gap((((0.0, -0.75), (1.0, -0.75)),)) == pytest.approx(0.25)
