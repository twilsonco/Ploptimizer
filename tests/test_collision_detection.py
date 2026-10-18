"""Unit tests for the collision-avoidance geometric primitives.

Covers ``plt_optimizer.generate.collision.geometry`` (the signed circle/AABB
clearance used by the text-hole collision detection system) and the
swept-ink distance primitives in ``plt_optimizer.generate.geometry`` backing
the descending-character relief's rolling-circle test
(segment/polyline/ink gaps and the spatial grid accelerating them),
including edge cases (tangency, containment, adjacency, X-disjoint near
misses) that must not produce false positives or false negatives.
"""

from __future__ import annotations

import dataclasses
import math
import random
from typing import List, Tuple

import pytest

from plt_optimizer.generate.collision.geometry import (
    CollisionResult,
    circle_aabb_gap,
    circle_ellipse_aabb_gap,
    circle_ellipse_gap,
    point_ellipse_signed_distance,
)
from plt_optimizer.generate.geometry import (
    Ink,
    InkGrid,
    aabb_vertical_gap,
    arc_swept_bounds,
    bbox_gap,
    ink_bounds,
    ink_gap,
    point_segment_gap,
    polyline_bounds,
    polyline_gap,
    segment_gap,
)


def _segments(polyline: Tuple[Tuple[float, float], ...]) -> List[Tuple[float, float, float, float]]:
    """Return a polyline's segments as flat ``(x1, y1, x2, y2)`` tuples (dots included)."""
    if not polyline:
        return []
    if len(polyline) == 1:
        x, y = polyline[0]
        return [(x, y, x, y)]
    return [(x1, y1, x2, y2) for (x1, y1), (x2, y2) in zip(polyline[:-1], polyline[1:])]


def _random_ink(rng: random.Random, region: float, polylines: int, vertices: int) -> Ink:
    """Build pseudo-random ink inside ``[0, region]^2`` from ``rng``."""
    return tuple(
        tuple(
            (rng.uniform(0.0, region), rng.uniform(0.0, region))
            for _vertex in range(max(1, vertices))
        )
        for _polyline in range(max(1, polylines))
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


class TestPointEllipseSignedDistance:
    """Tests for the exact point-to-ellipse-boundary distance."""

    def test_rejects_non_positive_semi_axes(self) -> None:
        with pytest.raises(ValueError, match="semi-axes"):
            point_ellipse_signed_distance((1.0, 0.0), (0.0, 0.0), 0.0, 1.0)
        with pytest.raises(ValueError, match="semi-axes"):
            point_ellipse_signed_distance((1.0, 0.0), (0.0, 0.0), 1.0, -1.0)

    def test_circle_matches_the_analytic_radius_gap(self) -> None:
        """A circle (a == b) reduces to |distance to centre| - radius."""
        for radius in (0.5, 1.0, 2.0):
            for angle in range(0, 360, 30):
                point = (2.0 * math.cos(math.radians(angle)), 2.0 * math.sin(math.radians(angle)))
                gap = point_ellipse_signed_distance(point, (0.0, 0.0), radius, radius)
                assert math.isclose(gap, 2.0 - radius, abs_tol=1e-9)

    def test_on_the_boundary_is_zero(self) -> None:
        """Every point sampled on the ellipse reports (near) zero distance."""
        semi_a, semi_b = 2.0, 1.0
        for angle in range(0, 360, 15):
            point = (
                semi_a * math.cos(math.radians(angle)),
                semi_b * math.sin(math.radians(angle)),
            )
            gap = point_ellipse_signed_distance(point, (0.0, 0.0), semi_a, semi_b)
            assert math.isclose(gap, 0.0, abs_tol=1e-6)

    def test_axis_vertices_are_exact(self) -> None:
        """The vertices and axis points give closed-form distances."""
        center = (0.0, 0.0)
        assert point_ellipse_signed_distance((3.0, 0.0), center, 2.0, 1.0) == pytest.approx(1.0)
        # (1.75, 0) is on the major axis OUTSIDE the evolute intercept
        # (a^2-b^2)/a = 1.5, so the closest point is the vertex (2, 0).
        assert point_ellipse_signed_distance((1.75, 0.0), center, 2.0, 1.0) == pytest.approx(-0.25)
        assert point_ellipse_signed_distance((0.0, 2.5), center, 2.0, 1.0) == pytest.approx(1.5)
        assert point_ellipse_signed_distance((0.0, 0.25), center, 2.0, 1.0) == pytest.approx(-0.75)

    def test_negative_x_is_symmetric(self) -> None:
        center = (0.0, 0.0)
        right = point_ellipse_signed_distance((3.0, 1.0), center, 2.0, 1.0)
        left = point_ellipse_signed_distance((-3.0, 1.0), center, 2.0, 1.0)
        assert math.isclose(right, left, abs_tol=1e-9)

    def test_interior_point_reports_negative(self) -> None:
        """The centre's distance to the boundary is the semi-minor axis."""
        gap = point_ellipse_signed_distance((0.0, 0.0), (0.0, 0.0), 2.0, 1.0)
        assert gap == pytest.approx(-1.0)

    def test_major_axis_inside_the_evolute_uses_the_minor_vertex(self) -> None:
        """A point on the major axis inside the evolute intercept measures
        to a MINOR-axis vertex (the classic closest-point subtlety)."""
        # a=2, b=1: evolute intercept (a^2-b^2)/a = 1.5. Point (1.0, 0) is
        # inside it: closest point (0, 1), distance hypot(1, 1).
        gap = point_ellipse_signed_distance((1.0, 0.0), (0.0, 0.0), 2.0, 1.0)
        assert gap == pytest.approx(-math.hypot(1.0, 1.0))

    def test_offset_ellipse_matches_centered_case(self) -> None:
        centered = point_ellipse_signed_distance((3.0, 1.0), (0.0, 0.0), 2.0, 1.0)
        offset = point_ellipse_signed_distance((5.0, 4.0), (2.0, 3.0), 2.0, 1.0)
        assert math.isclose(centered, offset, abs_tol=1e-9)

    def test_extreme_aspect_ratio_stays_finite(self) -> None:
        """A very flat ellipse (the degenerate a >> b case) still converges."""
        gap = point_ellipse_signed_distance((5.0, 3.0), (0.0, 0.0), 10.0, 0.05)
        assert math.isfinite(gap)
        # (5, 3) sits far above a 0.05in-tall ellipse: outside, ~3in away.
        assert gap > 2.0


class TestCircleEllipseGap:
    """Tests for the circle-to-ellipse-boundary clearance."""

    def test_separated_circle_ellipse(self) -> None:
        # Ellipse a=2, b=1 at origin; circle centre (4, 0) radius 0.5.
        # Closest boundary point is the vertex (2, 0): gap = 2 - 0.5.
        gap = circle_ellipse_gap((4.0, 0.0), 0.5, (0.0, 0.0, 2.0, 1.0))
        assert math.isclose(gap, 1.5)

    def test_tangent_circle_reports_zero(self) -> None:
        gap = circle_ellipse_gap((3.0, 0.0), 1.0, (0.0, 0.0, 2.0, 1.0))
        assert math.isclose(gap, 0.0, abs_tol=1e-9)

    def test_penetrating_circle_reports_negative(self) -> None:
        gap = circle_ellipse_gap((2.5, 0.0), 1.0, (0.0, 0.0, 2.0, 1.0))
        assert math.isclose(gap, -0.5)

    def test_concentric_circle_inside(self) -> None:
        """A concentric circle reports -(semi_minor + radius)."""
        gap = circle_ellipse_gap((0.0, 0.0), 0.25, (0.0, 0.0, 2.0, 1.0))
        assert math.isclose(gap, -1.25)

    def test_zero_radius_matches_point_distance(self) -> None:
        ellipse = (1.0, -2.0, 3.0, 1.5)
        point = (7.0, 2.0)
        gap = circle_ellipse_gap(point, 0.0, ellipse)
        assert math.isclose(
            gap, point_ellipse_signed_distance(point, (ellipse[0], ellipse[1]), 3.0, 1.5)
        )


class TestCircleEllipseAabbGap:
    """Tests for the drill-hole-to-elliptical-label-boundary clearance."""

    def test_centre_inside_the_box_reports_inside_gap(self) -> None:
        # Ellipse 4 x 2 (a=2, b=1) centred at (2, 1). The box contains the
        # centre, so its nearest point IS the centre: distance to the
        # boundary is -1 (the semi-minor), minus the hole radius.
        gap = circle_ellipse_aabb_gap((1.5, 0.5, 2.5, 1.5), (2.0, 1.0, 2.0, 1.0), 0.0625)
        assert math.isclose(gap, -1.0625)

    def test_matches_circle_aabb_gap_for_a_circular_label(self) -> None:
        """A round label with a == b is a circle; the gap tracks the exact
        point-to-circle distance (the box version differs only in that it
        measures to the box, which is the point of the ellipse comparison)."""
        ellipse = (2.0, 2.0, 1.0, 1.0)
        box = (3.2, 1.5, 4.0, 2.5)
        gap = circle_ellipse_aabb_gap(box, ellipse, 0.25)
        # Nearest box point to the centre (2,2) is (3.2, 2.0): 0.2in from
        # the circle boundary, minus the hole radius.
        assert math.isclose(gap, 0.2 - 0.25, abs_tol=1e-9)

    def test_tangent_is_zero(self) -> None:
        ellipse = (0.0, 0.0, 2.0, 1.0)
        # Box whose nearest point to the centre sits exactly 0.5in outside
        # the vertex (2.5, 0); a hole of radius 0.5 is tangent.
        gap = circle_ellipse_aabb_gap((2.5, -0.5, 3.5, 0.5), ellipse, 0.5)
        assert math.isclose(gap, 0.0, abs_tol=1e-9)

    def test_hole_deep_inside_reports_negative_radius_plus_depth(self) -> None:
        # The box contains the ellipse centre: nearest point is the centre,
        # 1in (semi-minor) inside the boundary, plus the hole radius.
        ellipse = (0.0, 0.0, 2.0, 1.0)
        gap = circle_ellipse_aabb_gap((-0.5, -0.5, 0.5, 0.5), ellipse, 0.125)
        assert math.isclose(gap, -1.125)


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


class TestBboxGap:
    """Tests for the box-to-box distance used as the exact ink prefilter."""

    def test_overlapping_boxes_report_zero(self) -> None:
        """Overlapping (and touching) boxes have no clearance."""
        assert bbox_gap((0.0, 0.0, 1.0, 1.0), (0.5, 0.5, 2.0, 2.0)) == 0.0
        assert bbox_gap((0.0, 0.0, 1.0, 1.0), (1.0, 0.0, 2.0, 1.0)) == 0.0

    def test_axis_separated_reports_the_gap(self) -> None:
        """Boxes separated on one axis report that axis's distance."""
        assert bbox_gap((0.0, 2.0, 1.0, 3.0), (0.0, 0.0, 1.0, 1.0)) == pytest.approx(1.0)

    def test_corner_separated_reports_diagonal(self) -> None:
        """Boxes separated on both axes report the corner-to-corner distance."""
        assert bbox_gap((1.0, 1.0, 2.0, 2.0), (0.0, 0.0, 0.0, 0.0)) == pytest.approx(
            math.hypot(1.0, 1.0)
        )

    def test_lower_bound_on_ink_gap(self) -> None:
        """The box gap never exceeds the true ink distance of ink inside."""
        upper: Ink = (((0.0, 2.0), (1.0, 2.5)),)
        lower: Ink = (((0.0, 0.0), (1.0, 0.0)),)
        assert bbox_gap(ink_bounds(upper), ink_bounds(lower)) <= ink_gap(upper, lower)

    def test_empty_ink_has_no_box(self) -> None:
        """Empty ink / empty polylines box to None."""
        assert ink_bounds(()) is None
        assert ink_bounds(((),)) is None
        assert polyline_bounds(()) is None


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

    def test_far_apart_chains_match_brute_force(self) -> None:
        """The bbox prefilter skips far pairs without changing the minimum."""
        upper = ((0.0, 100.0), (1.0, 100.0), (2.0, 100.0))
        lower = ((0.0, 0.0), (1.0, 0.0), (2.0, 0.0))
        assert polyline_gap(upper, lower) == pytest.approx(100.0)

    def test_randomized_matches_brute_force(self) -> None:
        """Prefiltered gaps equal the unfiltered segment-pair minimum.

        The prefilter may only skip pairs whose bounding-box distance is at
        least the current best (a lower bound on their true distance), so the
        measured minimum must be bit-identical to the naive double loop.
        """
        rng = random.Random(20261018)

        def brute(first: Ink, second: Ink) -> float:
            best = math.inf
            for a_poly in first:
                for b_poly in second:
                    for ax1, ay1, ax2, ay2 in _segments(a_poly):
                        for bx1, by1, bx2, by2 in _segments(b_poly):
                            best = min(
                                best,
                                segment_gap((ax1, ay1), (ax2, ay2), (bx1, by1), (bx2, by2)),
                            )
            return best

        for seed in range(40):
            rng.seed(seed)
            upper = _random_ink(rng, region=10.0, polylines=3, vertices=6)
            lower = _random_ink(rng, region=10.0, polylines=3, vertices=6)
            assert ink_gap(upper, lower) == pytest.approx(brute(upper, lower), abs=1e-12)
            for first in upper:
                for second in lower:
                    assert polyline_gap(first, second) == pytest.approx(
                        brute((first,), (second,)), abs=1e-12
                    )


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
