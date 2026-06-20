"""Geometry utility functions for PLT-Optimizer.

Provides mathematical utilities for coordinate and path calculations,
including Euclidean distance computations with proper floating-point handling.
"""

from __future__ import annotations

import math
from typing import List, Sequence, Tuple

from plt_optimizer.core.models import Coordinate, Segment, StrokePath


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


def is_point_on_segment(
    p: Coordinate,
    a: Coordinate,
    b: Coordinate,
    tol: float = 1e-5,
) -> bool:
    """Check if point P lies on line segment AB.

    Uses cross product for collinearity check and dot product for bounds check.
    The tolerance here is stricter than COORD_TOLERANCE since we're comparing
    computed values rather than read coordinates.

    Args:
        p: Point to test.
        a: First endpoint of segment.
        b: Second endpoint of segment.
        tol: Tolerance for floating-point comparisons (default 1e-5).

    Returns:
        True if P lies on segment AB, False otherwise.
    """
    ab_x = b.x - a.x
    ab_y = b.y - a.y
    ap_x = p.x - a.x
    ap_y = p.y - a.y

    cross_product = ab_x * ap_y - ab_y * ap_x
    if not math.isclose(cross_product, 0.0, abs_tol=tol):
        return False

    dot_product = ab_x * ap_x + ab_y * ap_y
    ab_squared = ab_x * ab_x + ab_y * ab_y
    
    if math.isclose(ab_squared, 0.0, abs_tol=tol):
        return calculate_coordinate_distance(p, a) <= tol
    
    if dot_product < -tol or dot_product > ab_squared + tol:
        return False

    return True


def _segment_to_coords(
    seg: Segment,
) -> Tuple[Coordinate, Coordinate]:
    """Extract start and end coordinates from a segment.

    Args:
        seg: A StrokeSegment or ArcSegment.

    Returns:
        Tuple of (start, end) coordinates.
    """
    return (seg.start, seg.end)


def remove_redundant_strokes(
    doc: PLTDocument,
    tol: float = 1e-5,
) -> PLTDocument:
    """Remove redundant strokes whose endpoints lie on other strokes.

    This function identifies cutting segments where both endpoints lie on
    another longer stroke and removes them as redundant. This prevents
    duplicate cuts along the same path.

    O(N²) algorithm where N = number of cutting segments in document.
    For documents with thousands of strokes, spatial indexing may be needed.

    Args:
        doc: The input PLTDocument.
        tol: Tolerance for floating-point comparisons (default 1e-5).

    Returns:
        New PLTDocument with redundant segments removed. Empty paths are filtered out.
    """
    from plt_optimizer.core.models import (
        ArcSegment,
        FooterCommand,
        HeaderCommand,
        PLTDocument,
        StrokePath,
        StrokeSegment,
    )

    all_cutting_segments: List[Tuple[int, int, Segment]] = []
    for path_idx, path in enumerate(doc.stroke_paths):
        for seg_idx, seg in enumerate(path.segments):
            if isinstance(seg, ArcSegment) or not seg.is_cutting:
                continue
            all_cutting_segments.append((path_idx, seg_idx, seg))

    n = len(all_cutting_segments)
    indices_to_remove: set[Tuple[int, int]] = set()

    for i in range(n):
        path_idx_i, seg_idx_i, seg_i = all_cutting_segments[i]
        start_i, end_i = seg_i.start, seg_i.end

        for j in range(i + 1, n):
            path_idx_j, seg_idx_j, seg_j = all_cutting_segments[j]
            start_j, end_j = seg_j.start, seg_j.end

            on_i_on_j = (
                is_point_on_segment(start_i, start_j, end_j, tol)
                and is_point_on_segment(end_i, start_j, end_j, tol)
            )
            on_j_on_i = (
                is_point_on_segment(start_j, start_i, end_i, tol)
                and is_point_on_segment(end_j, start_i, end_i, tol)
            )

            both_on_each_other = on_i_on_j and on_j_on_i
            
            if both_on_each_other:
                seg_i_len = calculate_coordinate_distance(start_i, end_i)
                seg_j_len = calculate_coordinate_distance(start_j, end_j)
                same_length = math.isclose(seg_i_len, seg_j_len, abs_tol=tol)
                
                if same_length:
                    start_match = calculate_coordinate_distance(start_i, start_j) <= tol
                    end_match = calculate_coordinate_distance(end_i, end_j) <= tol
                    
                    if start_match and end_match:
                        indices_to_remove.add((path_idx_j, seg_idx_j))
                    else:
                        reversed_start = calculate_coordinate_distance(start_i, end_j) <= tol
                        reversed_end = calculate_coordinate_distance(end_i, start_j) <= tol
                        
                        if reversed_start and reversed_end:
                            indices_to_remove.add((path_idx_j, seg_idx_j))
                else:
                    shorter_len = min(seg_i_len, seg_j_len)
                    longer_len = max(seg_i_len, seg_j_len)
                    
                    len_diff = longer_len - shorter_len
                    
                    if math.isclose(len_diff, 0.0, abs_tol=tol) or len_diff > tol:
                        pass
                    
                    if seg_i_len <= seg_j_len:
                        indices_to_remove.add((path_idx_j, seg_idx_j))
                    else:
                        indices_to_remove.add((path_idx_i, seg_idx_i))
            else:
                if on_i_on_j:
                    indices_to_remove.add((path_idx_i, seg_idx_i))
                if on_j_on_i:
                    indices_to_remove.add((path_idx_j, seg_idx_j))

    new_stroke_paths: List[StrokePath] = []

    for path_idx, path in enumerate(doc.stroke_paths):
        current_segments: List[Segment] = []
        current_pen_up = path.pen_up_position

        for seg_idx, seg in enumerate(path.segments):
            if (path_idx, seg_idx) in indices_to_remove:
                if current_segments:
                    new_stroke_paths.append(
                        StrokePath(
                            pen_up_position=current_pen_up,
                            segments=tuple(current_segments),
                        )
                    )
                    current_segments = []
                current_pen_up = None
            else:
                if not current_segments and current_pen_up is None:
                    current_pen_up = seg.start
                current_segments.append(seg)

        if current_segments:
            new_stroke_paths.append(
                StrokePath(
                    pen_up_position=current_pen_up,
                    segments=tuple(current_segments),
                )
            )

    return PLTDocument(
        header_commands=list(doc.header_commands),
        stroke_paths=new_stroke_paths,
        footer_commands=list(doc.footer_commands),
    )