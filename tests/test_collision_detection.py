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
