"""Unit tests for the collision-avoidance geometric primitives.

Covers ``plt_optimizer.generate.geometry``: the signed circle/AABB
clearance used by the text-hole collision detection system, including
edge cases (tangency, containment, adjacency) that must not produce
false positives or false negatives.
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from plt_optimizer.generate.geometry import (
    CollisionResult,
    arc_swept_bounds,
    circle_aabb_gap,
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
