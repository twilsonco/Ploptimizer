"""Geometry utility functions for PLT-Optimizer.

Provides mathematical utilities for coordinate and path calculations,
including Euclidean distance computations with proper floating-point handling.
"""

from __future__ import annotations

import math
from typing import Sequence, Tuple

from plt_optimizer.core.models import Coordinate, StrokePath


# Tolerance for floating-point comparisons (3 decimal places = 0.001)
COORD_TOLERANCE = 1e-3


def calculate_distance(
    point1: Tuple[float, float],
    point2: Tuple[float, float],
) -> float:
    """Calculate the Euclidean distance between two points.

    Uses the Pythagorean theorem for accurate distance calculation
    without bias toward any axis.

    Args:
        point1: First point as (x, y) tuple.
        point2: Second point as (x, y) tuple.

    Returns:
        The Euclidean distance between the two points.
    """
    dx = point2[0] - point1[0]
    dy = point2[1] - point1[1]
    return math.sqrt(dx * dx + dy * dy)


def calculate_coordinate_distance(
    coord1: Coordinate,
    coord2: Coordinate,
) -> float:
    """Calculate the Euclidean distance between two Coordinate objects.

    Args:
        coord1: First coordinate.
        coord2: Second coordinate.

    Returns:
        The Euclidean distance between coordinates.
    """
    return calculate_distance((coord1.x, coord1.y), (coord2.x, coord2.y))


def calculate_path_length(
    path_points: Sequence[Tuple[float, float]],
) -> float:
    """Calculate the total length of a polyline path.

    Sums the Euclidean distances between consecutive points
    in the provided sequence.

    Args:
        path_points: Sequence of (x, y) coordinate tuples.

    Returns:
        Total length of the polyline.
    """
    if len(path_points) < 2:
        return 0.0

    total = 0.0
    for i in range(1, len(path_points)):
        total += calculate_distance(path_points[i - 1], path_points[i])

    return total


def coordinates_equal(
    coord1: Coordinate,
    coord2: Coordinate,
    tolerance: float = COORD_TOLERANCE,
) -> bool:
    """Check if two coordinates are approximately equal.

    Uses math.isclose() for floating-point comparison to avoid
    precision issues with decimal values.

    Args:
        coord1: First coordinate.
        coord2: Second coordinate.
        tolerance: Comparison tolerance (default 0.001 = 3 decimal places).

    Returns:
        True if coordinates are within tolerance of each other.
    """
    return math.isclose(coord1.x, coord2.x, abs_tol=tolerance) and math.isclose(
        coord1.y, coord2.y, abs_tol=tolerance
    )


def calculate_stroke_path_length(stroke: StrokePath) -> float:
    """Calculate the total length of a stroke path.

    Args:
        stroke: The stroke path to measure.

    Returns:
        Sum of all segment lengths in the stroke.
    """
    return sum(seg.length for seg in stroke.segments)


def bounding_box(
    coordinates: Sequence[Coordinate],
) -> Tuple[float, float, float, float]:
    """Calculate the bounding box for a set of coordinates.

    Args:
        coordinates: Sequence of Coordinate objects.

    Returns:
        A tuple of (min_x, min_y, max_x, max_y).
    """
    if not coordinates:
        return (0.0, 0.0, 0.0, 0.0)

    xs = [c.x for c in coordinates]
    ys = [c.y for c in coordinates]

    return (
        min(xs),
        min(ys),
        max(xs),
        max(ys),
    )


def calculate_cumulative_distances(
    segments: Sequence[Tuple[Coordinate, Coordinate]],
) -> Tuple[float, ...]:
    """Calculate cumulative distance traveled at each segment.

    Given a sequence of line segments (as coordinate pairs), returns
    the running total distance from the start point.

    Args:
        segments: Sequence of (start_coord, end_coord) tuples for each segment.

    Returns:
        Tuple of cumulative distances at the end of each segment.
    """
    if not segments:
        return ()

    cumulative: list[float] = []
    total = 0.0

    for start, end in segments:
        dist = calculate_coordinate_distance(start, end)
        total += dist
        cumulative.append(total)

    return tuple(cumulative)


def interpolate_point(
    point1: Coordinate,
    point2: Coordinate,
    t: float,
) -> Coordinate:
    """Linear interpolation between two coordinates.

    Args:
        point1: Start coordinate (t=0).
        point2: End coordinate (t=1).
        t: Interpolation factor (0.0 to 1.0).

    Returns:
        Interpolated Coordinate at position t.
    """
    if not 0.0 <= t <= 1.0:
        raise ValueError(f"Interpolation parameter t must be in [0, 1], got {t}")

    x = point1.x + (point2.x - point1.x) * t
    y = point1.y + (point2.y - point1.y) * t

    return Coordinate(x=x, y=y)