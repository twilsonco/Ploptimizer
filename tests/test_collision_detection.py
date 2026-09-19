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
    check_text_hole_collision,
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


class TestCheckTextHoleCollision:
    """Tests for the boolean collision predicate."""

    def test_no_overlap_is_not_a_collision(self) -> None:
        """Adjacent non-overlapping geometry must not collide."""
        assert not check_text_hole_collision((0.0, 0.0, 1.0, 1.0), (1.5, 0.5), 0.25)

    def test_overlap_is_a_collision(self) -> None:
        """An overlapping circle and box must report a collision."""
        assert check_text_hole_collision((0.0, 0.0, 1.0, 1.0), (1.1, 0.5), 0.25)

    def test_tangency_is_not_a_collision(self) -> None:
        """Exact tangency (gap == 0) is NOT a collision."""
        assert not check_text_hole_collision((0.0, 0.0, 1.0, 1.0), (1.25, 0.5), 0.25)

    def test_circle_touching_corner_is_not_a_collision(self) -> None:
        """A circle just clear of a corner must not register."""
        # Corner (1,1); center (2,2) is sqrt(2) ~= 1.414 away. Radius 1.4
        # stays clear; radius 1.45 bites the corner.
        assert not check_text_hole_collision((0.0, 0.0, 1.0, 1.0), (2.0, 2.0), 1.4)
        assert check_text_hole_collision((0.0, 0.0, 1.0, 1.0), (2.0, 2.0), 1.45)

    def test_degenerate_thin_text_box(self) -> None:
        """A zero-height text box (single baseline) still intersects."""
        assert check_text_hole_collision((0.0, 0.5, 1.0, 0.5), (0.5, 0.6), 0.25)
        assert not check_text_hole_collision((0.0, 0.5, 1.0, 0.5), (0.5, 1.0), 0.25)


class TestCheckTextHoleCollisionMinClearance:
    """Tests for the stroke-clearance variant of the collision predicate."""

    # Box (0,0)-(1,1) with center (1.5,0.5), radius 0.25 -> gap exactly 0.25.
    BOX = (0.0, 0.0, 1.0, 1.0)
    CENTER = (1.5, 0.5)
    RADIUS = 0.25

    def test_default_clearance_keeps_strict_semantics(self) -> None:
        """min_clearance defaults to 0.0 (pure overlap predicate)."""
        assert not check_text_hole_collision(self.BOX, self.CENTER, self.RADIUS)
        assert not check_text_hole_collision(self.BOX, self.CENTER, self.RADIUS, min_clearance=0.0)

    def test_near_miss_below_clearance_is_a_collision(self) -> None:
        """A positive gap under the required clearance must be flagged."""
        assert check_text_hole_collision(self.BOX, self.CENTER, self.RADIUS, min_clearance=0.3)

    def test_gap_exactly_equal_to_clearance_is_safe(self) -> None:
        """A gap equal to min_clearance is safe (>= passes)."""
        assert not check_text_hole_collision(self.BOX, self.CENTER, self.RADIUS, min_clearance=0.25)

    def test_tangency_collides_under_positive_clearance(self) -> None:
        """Tangent paths (gap 0) collide whenever clearance > 0."""
        tangent_center = (1.25, 0.5)
        assert not check_text_hole_collision(self.BOX, tangent_center, 0.25)
        assert check_text_hole_collision(self.BOX, tangent_center, 0.25, min_clearance=0.01)

    def test_penetration_collides_regardless_of_clearance(self) -> None:
        """Overlapping geometry collides at any clearance."""
        assert check_text_hole_collision(self.BOX, (1.1, 0.5), 0.25, min_clearance=0.0)
        assert check_text_hole_collision(self.BOX, (1.1, 0.5), 0.25, min_clearance=0.5)


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
