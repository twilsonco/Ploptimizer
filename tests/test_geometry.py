"""Tests for plt_optimizer/utils/geometry.py.

These tests target specific lines not covered by existing tests:
- calculate_distance basic and edge cases (lines 35-37)
- calculate_coordinate_distance (line 53)
- calculate_path_length basic and edge cases (lines 70-77)
- coordinates_equal with tolerance parameter (line 98)
- calculate_stroke_path_length (lines 112, 126-132)
- bounding_box edge cases (lines 154-165)
- calculate_cumulative_distances edge cases (lines 183-189)
"""

from __future__ import annotations

import math
import pytest

from plt_optimizer.core.models import Coordinate, StrokePath, StrokeSegment


class TestCalculateDistance:
    """Tests for calculate_distance function."""

    def test_basic_distance(self) -> None:
        """Test basic Euclidean distance calculation."""
        from plt_optimizer.utils.geometry import calculate_distance

        result = calculate_distance((0.0, 0.0), (3.0, 4.0))
        assert math.isclose(result, 5.0)

    def test_same_point_distance(self) -> None:
        """Test distance between identical points is zero."""
        from plt_optimizer.utils.geometry import calculate_distance

        result = calculate_distance((10.5, -20.3), (10.5, -20.3))
        assert math.isclose(result, 0.0)

    def test_axis_aligned_distance(self) -> None:
        """Test distance along a single axis."""
        from plt_optimizer.utils.geometry import calculate_distance

        result = calculate_distance((0.0, 0.0), (100.0, 0.0))
        assert math.isclose(result, 100.0)

    def test_negative_coordinates(self) -> None:
        """Test distance with negative coordinates."""
        from plt_optimizer.utils.geometry import calculate_distance

        result = calculate_distance((-1.0, -2.0), (1.0, 2.0))
        assert math.isclose(result, math.sqrt(4 + 16))


class TestCalculateCoordinateDistance:
    """Tests for calculate_coordinate_distance function (line 53)."""

    def test_basic_coordinate_distance(self) -> None:
        """Test distance between two Coordinate objects."""
        from plt_optimizer.utils.geometry import calculate_coordinate_distance

        coord1 = Coordinate(x=0.0, y=0.0)
        coord2 = Coordinate(x=3.0, y=4.0)

        result = calculate_coordinate_distance(coord1, coord2)
        assert math.isclose(result, 5.0)

    def test_coordinate_distance_same_point(self) -> None:
        """Test distance between identical coordinates."""
        from plt_optimizer.utils.geometry import calculate_coordinate_distance

        coord = Coordinate(x=100.5, y=-200.75)
        result = calculate_coordinate_distance(coord, coord)
        assert math.isclose(result, 0.0)


class TestCalculatePathLength:
    """Tests for calculate_path_length function (lines 70-77)."""

    def test_single_segment_path(self) -> None:
        """Test path length with two points (one segment)."""
        from plt_optimizer.utils.geometry import calculate_path_length

        path = [(0.0, 0.0), (3.0, 4.0)]
        result = calculate_path_length(path)
        assert math.isclose(result, 5.0)

    def test_multi_segment_path(self) -> None:
        """Test path length with multiple segments."""
        from plt_optimizer.utils.geometry import calculate_path_length

        # Right angle path: (0,0) -> (3,0) -> (3,4), total = 3 + 4 = 7
        path = [(0.0, 0.0), (3.0, 0.0), (3.0, 4.0)]
        result = calculate_path_length(path)
        assert math.isclose(result, 7.0)

    def test_empty_path(self) -> None:
        """Test path length with empty sequence."""
        from plt_optimizer.utils.geometry import calculate_path_length

        result = calculate_path_length([])
        assert math.isclose(result, 0.0)

    def test_single_point_path(self) -> None:
        """Test path length with single point."""
        from plt_optimizer.utils.geometry import calculate_path_length

        result = calculate_path_length([(10.0, 20.0)])
        assert math.isclose(result, 0.0)

    def test_path_with_many_segments(self) -> None:
        """Test path length with many segments."""
        from plt_optimizer.utils.geometry import calculate_path_length

        # Unit steps along x-axis: total = 1+1+1+1 = 4
        path = [(i, 0.0) for i in range(5)]
        result = calculate_path_length(path)
        assert math.isclose(result, 4.0)


class TestCoordinatesEqual:
    """Tests for coordinates_equal function (line 98)."""

    def test_identical_coordinates(self) -> None:
        """Test coordinates that are exactly equal."""
        from plt_optimizer.utils.geometry import coordinates_equal

        c1 = Coordinate(x=10.0, y=20.0)
        c2 = Coordinate(x=10.0, y=20.0)

        assert coordinates_equal(c1, c2) is True

    def test_different_coordinates(self) -> None:
        """Test coordinates that differ significantly."""
        from plt_optimizer.utils.geometry import coordinates_equal

        c1 = Coordinate(x=0.0, y=0.0)
        c2 = Coordinate(x=100.0, y=100.0)

        assert coordinates_equal(c1, c2) is False

    def test_within_tolerance(self) -> None:
        """Test coordinates within default tolerance."""
        from plt_optimizer.utils.geometry import coordinates_equal

        c1 = Coordinate(x=0.0, y=0.0)
        c2 = Coordinate(x=0.0005, y=-0.0005)

        assert coordinates_equal(c1, c2) is True

    def test_precision_beyond_tolerance(self) -> None:
        """Test coordinates just beyond tolerance threshold."""
        from plt_optimizer.utils.geometry import coordinates_equal

        c1 = Coordinate(x=0.0, y=0.0)
        c2 = Coordinate(x=0.0015, y=-0.001)

        assert coordinates_equal(c1, c2) is False


class TestCalculateStrokePathLength:
    """Tests for calculate_stroke_path_length function (lines 112, 126-132)."""

    def test_empty_stroke(self) -> None:
        """Test length of a stroke with no segments."""
        from plt_optimizer.utils.geometry import calculate_stroke_path_length

        stroke = StrokePath()
        result = calculate_stroke_path_length(stroke)
        assert math.isclose(result, 0.0)

    def test_single_segment_stroke(self) -> None:
        """Test length of a stroke with one segment."""
        from plt_optimizer.utils.geometry import calculate_stroke_path_length
        from plt_optimizer.core.models import Coordinate, StrokeSegment

        seg = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=3.0, y=4.0),
            is_cutting=True,
        )
        stroke = StrokePath(segments=(seg,))
        result = calculate_stroke_path_length(stroke)
        assert math.isclose(result, 5.0)

    def test_multi_segment_stroke(self) -> None:
        """Test length of a stroke with multiple segments."""
        from plt_optimizer.utils.geometry import calculate_stroke_path_length
        from plt_optimizer.core.models import Coordinate, StrokeSegment

        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=3.0, y=0.0),
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=3.0, y=0.0),
            end=Coordinate(x=3.0, y=4.0),
            is_cutting=True,
        )
        stroke = StrokePath(segments=(seg1, seg2))
        result = calculate_stroke_path_length(stroke)
        assert math.isclose(result, 7.0)

    def test_mixed_cutting_and_rapid_segments(self) -> None:
        """Test stroke length including rapid (pen-up) segments."""
        from plt_optimizer.utils.geometry import calculate_stroke_path_length
        from plt_optimizer.core.models import Coordinate, StrokeSegment

        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=0.0),
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=10.0, y=0.0),
            end=Coordinate(x=20.0, y=0.0),
            is_cutting=False,  # rapid move
        )
        stroke = StrokePath(segments=(seg1, seg2))
        result = calculate_stroke_path_length(stroke)
        # Both segments contribute to total length regardless of cutting status
        assert math.isclose(result, 20.0)


class TestBoundingBox:
    """Tests for bounding_box function (lines 154-165)."""

    def test_empty_coordinates(self) -> None:
        """Test bounding box of empty coordinate list."""
        from plt_optimizer.utils.geometry import bounding_box

        result = bounding_box([])
        assert math.isclose(result[0], 0.0)  # min_x
        assert math.isclose(result[1], 0.0)  # min_y
        assert math.isclose(result[2], 0.0)  # max_x
        assert math.isclose(result[3], 0.0)  # max_y

    def test_single_coordinate(self) -> None:
        """Test bounding box with single coordinate."""
        from plt_optimizer.utils.geometry import bounding_box

        coord = Coordinate(x=10.5, y=-20.3)
        result = bounding_box([coord])

        assert math.isclose(result[0], 10.5)
        assert math.isclose(result[1], -20.3)
        assert math.isclose(result[2], 10.5)
        assert math.isclose(result[3], -20.3)

    def test_two_coordinates(self) -> None:
        """Test bounding box with two coordinates."""
        from plt_optimizer.utils.geometry import bounding_box

        coords = [Coordinate(x=5.0, y=10.0), Coordinate(x=-3.0, y=-7.5)]
        result = bounding_box(coords)

        assert math.isclose(result[0], -3.0)  # min_x
        assert math.isclose(result[1], -7.5)  # min_y
        assert math.isclose(result[2], 5.0)   # max_x
        assert math.isclose(result[3], 10.0)  # max_y

    def test_multiple_coordinates(self) -> None:
        """Test bounding box with multiple coordinates."""
        from plt_optimizer.utils.geometry import bounding_box

        coords = [
            Coordinate(x=0.0, y=0.0),
            Coordinate(x=100.0, y=50.0),
            Coordinate(x=-20.0, y=-30.0),
            Coordinate(x=50.0, y=200.0),
        ]
        result = bounding_box(coords)

        assert math.isclose(result[0], -20.0)  # min_x
        assert math.isclose(result[1], -30.0)  # min_y
        assert math.isclose(result[2], 100.0)   # max_x
        assert math.isclose(result[3], 200.0)   # max_y

    def test_all_same_coordinates(self) -> None:
        """Test bounding box when all coordinates are identical."""
        from plt_optimizer.utils.geometry import bounding_box

        coord = Coordinate(x=42.0, y=-15.5)
        result = bounding_box([coord, coord, coord])

        assert math.isclose(result[0], 42.0)
        assert math.isclose(result[1], -15.5)
        assert math.isclose(result[2], 42.0)
        assert math.isclose(result[3], -15.5)


class TestCalculateCumulativeDistances:
    """Tests for calculate_cumulative_distances function (lines 183-189)."""

    def test_empty_segments(self) -> None:
        """Test cumulative distances with no segments."""
        from plt_optimizer.utils.geometry import calculate_cumulative_distances

        result = calculate_cumulative_distances([])
        assert result == ()

    def test_single_segment(self) -> None:
        """Test cumulative distances with one segment."""
        from plt_optimizer.utils.geometry import calculate_cumulative_distances, Coordinate

        start = Coordinate(x=0.0, y=0.0)
        end = Coordinate(x=3.0, y=4.0)

        result = calculate_cumulative_distances([(start, end)])
        assert len(result) == 1
        assert math.isclose(result[0], 5.0)

    def test_multiple_segments(self) -> None:
        """Test cumulative distances with multiple segments."""
        from plt_optimizer.utils.geometry import calculate_cumulative_distances, Coordinate

        # Unit steps along x-axis
        coords = [Coordinate(x=i, y=0.0) for i in range(5)]
        segments = [(coords[i], coords[i + 1]) for i in range(len(coords) - 1)]

        result = calculate_cumulative_distances(segments)
        assert len(result) == 4
        # Cumulative: 1, 2, 3, 4
        assert math.isclose(result[0], 1.0)
        assert math.isclose(result[1], 2.0)
        assert math.isclose(result[2], 3.0)
        assert math.isclose(result[3], 4.0)

    def test_non_unit_segments(self) -> None:
        """Test cumulative distances with varying segment lengths."""
        from plt_optimizer.utils.geometry import calculate_cumulative_distances, Coordinate

        # Segments: (0,0)->(3,4) dist=5, then (3,4)->(7,4) dist=4
        start1 = Coordinate(x=0.0, y=0.0)
        mid = Coordinate(x=3.0, y=4.0)
        end = Coordinate(x=7.0, y=4.0)

        result = calculate_cumulative_distances([(start1, mid), (mid, end)])
        assert len(result) == 2
        assert math.isclose(result[0], 5.0)
        assert math.isclose(result[1], 9.0)

    def test_zero_length_segments(self) -> None:
        """Test cumulative distances with zero-length segments."""
        from plt_optimizer.utils.geometry import calculate_cumulative_distances, Coordinate

        start = Coordinate(x=0.0, y=0.0)
        end = Coordinate(x=10.0, y=0.0)

        result = calculate_cumulative_distances([(start, start), (start, end)])
        assert len(result) == 2
        assert math.isclose(result[0], 0.0)  # zero-length first segment
        assert math.isclose(result[1], 10.0)


class TestInterpolatePoint:
    """Tests for interpolate_point function (lines 154-165)."""

    def test_interpolate_start(self) -> None:
        """Test interpolation at t=0 returns start point."""
        from plt_optimizer.utils.geometry import interpolate_point

        p1 = Coordinate(x=0.0, y=0.0)
        p2 = Coordinate(x=10.0, y=20.0)

        result = interpolate_point(p1, p2, 0.0)
        assert math.isclose(result.x, 0.0)
        assert math.isclose(result.y, 0.0)

    def test_interpolate_end(self) -> None:
        """Test interpolation at t=1 returns end point."""
        from plt_optimizer.utils.geometry import interpolate_point

        p1 = Coordinate(x=0.0, y=0.0)
        p2 = Coordinate(x=10.0, y=20.0)

        result = interpolate_point(p1, p2, 1.0)
        assert math.isclose(result.x, 10.0)
        assert math.isclose(result.y, 20.0)

    def test_interpolate_midpoint(self) -> None:
        """Test interpolation at t=0.5 returns midpoint."""
        from plt_optimizer.utils.geometry import interpolate_point

        p1 = Coordinate(x=0.0, y=0.0)
        p2 = Coordinate(x=10.0, y=20.0)

        result = interpolate_point(p1, p2, 0.5)
        assert math.isclose(result.x, 5.0)
        assert math.isclose(result.y, 10.0)

    def test_interpolate_quarter(self) -> None:
        """Test interpolation at t=0.25."""
        from plt_optimizer.utils.geometry import interpolate_point

        p1 = Coordinate(x=0.0, y=0.0)
        p2 = Coordinate(x=100.0, y=200.0)

        result = interpolate_point(p1, p2, 0.25)
        assert math.isclose(result.x, 25.0)
        assert math.isclose(result.y, 50.0)

    def test_interpolate_invalid_t_negative(self) -> None:
        """Test interpolation with t < 0 raises ValueError."""
        from plt_optimizer.utils.geometry import interpolate_point

        p1 = Coordinate(x=0.0, y=0.0)
        p2 = Coordinate(x=10.0, y=20.0)

        with pytest.raises(ValueError, match="Interpolation parameter t must be in \\[0, 1\\]"):
            interpolate_point(p1, p2, -0.5)

    def test_interpolate_invalid_t_above(self) -> None:
        """Test interpolation with t > 1 raises ValueError."""
        from plt_optimizer.utils.geometry import interpolate_point

        p1 = Coordinate(x=0.0, y=0.0)
        p2 = Coordinate(x=10.0, y=20.0)

        with pytest.raises(ValueError, match="Interpolation parameter t must be in \\[0, 1\\]"):
            interpolate_point(p1, p2, 1.5)

    def test_interpolate_same_points(self) -> None:
        """Test interpolation between identical points."""
        from plt_optimizer.utils.geometry import interpolate_point

        p = Coordinate(x=5.0, y=-3.0)
        result = interpolate_point(p, p, 0.75)
        assert math.isclose(result.x, 5.0)
        assert math.isclose(result.y, -3.0)

    def test_interpolate_negative_coordinates(self) -> None:
        """Test interpolation with negative coordinates."""
        from plt_optimizer.utils.geometry import interpolate_point

        p1 = Coordinate(x=-10.0, y=-20.0)
        p2 = Coordinate(x=0.0, y=0.0)

        result = interpolate_point(p1, p2, 0.5)
        assert math.isclose(result.x, -5.0)
        assert math.isclose(result.y, -10.0)

    def test_interpolate_horizontal_line(self) -> None:
        """Test interpolation along a horizontal line."""
        from plt_optimizer.utils.geometry import interpolate_point

        p1 = Coordinate(x=0.0, y=5.0)
        p2 = Coordinate(x=100.0, y=5.0)

        result = interpolate_point(p1, p2, 0.3)
        assert math.isclose(result.x, 30.0)
        assert math.isclose(result.y, 5.0)

    def test_interpolate_vertical_line(self) -> None:
        """Test interpolation along a vertical line."""
        from plt_optimizer.utils.geometry import interpolate_point

        p1 = Coordinate(x=5.0, y=0.0)
        p2 = Coordinate(x=5.0, y=100.0)

        result = interpolate_point(p1, p2, 0.6)
        assert math.isclose(result.x, 5.0)
        assert math.isclose(result.y, 60.0)


class TestFractureLinearPaths:
    """Tests for fracture_linear_paths function."""

    def test_fracture_single_segment_path(self) -> None:
        """Test that single-segment paths remain unchanged."""
        from plt_optimizer.utils.geometry import fracture_linear_paths
        from plt_optimizer.core.models import PLTDocument, StrokePath

        seg = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=50.0),
            is_cutting=True,
        )
        path = StrokePath(pen_up_position=None, segments=(seg,))
        doc = PLTDocument(header_commands=[], stroke_paths=[path], footer_commands=[])

        result = fracture_linear_paths(doc)

        assert len(result.stroke_paths) == 1
        assert result.stroke_paths[0].segments == (seg,)

    def test_fracture_multi_segment_rectangle(self) -> None:
        """Test that a rectangle is fractured into 4 separate paths."""
        from plt_optimizer.utils.geometry import fracture_linear_paths
        from plt_optimizer.core.models import PLTDocument, StrokePath

        # Rectangle: (0,0) -> (100,0) -> (100,50) -> (0,50) -> (0,0)
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=100.0, y=0.0),
            end=Coordinate(x=100.0, y=50.0),
            is_cutting=True,
        )
        seg3 = StrokeSegment(
            start=Coordinate(x=100.0, y=50.0),
            end=Coordinate(x=0.0, y=50.0),
            is_cutting=True,
        )
        seg4 = StrokeSegment(
            start=Coordinate(x=0.0, y=50.0),
            end=Coordinate(x=0.0, y=0.0),
            is_cutting=True,
        )
        path = StrokePath(pen_up_position=None, segments=(seg1, seg2, seg3, seg4))
        doc = PLTDocument(header_commands=[], stroke_paths=[path], footer_commands=[])

        result = fracture_linear_paths(doc)

        assert len(result.stroke_paths) == 4
        # Each fractured path should have pen_up_position at its segment's start
        for fractured_path in result.stroke_paths:
            assert len(fractured_path.segments) == 1

    def test_fracture_preserves_arc_paths(self) -> None:
        """Test that paths containing arcs are preserved intact."""
        from plt_optimizer.utils.geometry import fracture_linear_paths
        from plt_optimizer.core.models import ArcSegment, PLTDocument, StrokePath

        # Create a path with an arc (simulating drill hole)
        arc = ArcSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=10.0),
            center=Coordinate(x=5.0, y=0.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        path = StrokePath(pen_up_position=None, segments=(arc,))
        doc = PLTDocument(header_commands=[], stroke_paths=[path], footer_commands=[])

        result = fracture_linear_paths(doc)

        # Arc path should be preserved intact
        assert len(result.stroke_paths) == 1
        assert result.stroke_paths[0].segments == (arc,)

    def test_fracture_empty_document(self) -> None:
        """Test fracturing an empty document."""
        from plt_optimizer.utils.geometry import fracture_linear_paths
        from plt_optimizer.core.models import PLTDocument

        doc = PLTDocument(header_commands=[], stroke_paths=[], footer_commands=[])
        result = fracture_linear_paths(doc)

        assert len(result.stroke_paths) == 0

    def test_fracture_mixed_paths(self) -> None:
        """Test fracturing a document with both linear and arc paths."""
        from plt_optimizer.utils.geometry import fracture_linear_paths
        from plt_optimizer.core.models import ArcSegment, PLTDocument, StrokePath

        # Linear path (should be fractured)
        line_seg = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        linear_path = StrokePath(pen_up_position=None, segments=(line_seg,))

        # Arc path (should be preserved)
        arc = ArcSegment(
            start=Coordinate(x=50.0, y=50.0),
            end=Coordinate(x=60.0, y=60.0),
            center=Coordinate(x=55.0, y=50.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        arc_path = StrokePath(pen_up_position=None, segments=(arc,))

        doc = PLTDocument(
            header_commands=[],
            stroke_paths=[linear_path, arc_path],
            footer_commands=[]
        )

        result = fracture_linear_paths(doc)

        # 1 fractured linear path + 1 preserved arc path
        assert len(result.stroke_paths) == 2

    def test_fracture_polygon(self) -> None:
        """Test that a polygon is fractured into individual segment paths."""
        from plt_optimizer.utils.geometry import fracture_linear_paths
        from plt_optimizer.core.models import PLTDocument, StrokePath

        # Triangle: (0,0) -> (50,100) -> (100,0)
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=50.0, y=100.0),
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=50.0, y=100.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        seg3 = StrokeSegment(
            start=Coordinate(x=100.0, y=0.0),
            end=Coordinate(x=0.0, y=0.0),  # closing the triangle
            is_cutting=True,
        )
        path = StrokePath(pen_up_position=None, segments=(seg1, seg2, seg3))
        doc = PLTDocument(header_commands=[], stroke_paths=[path], footer_commands=[])

        result = fracture_linear_paths(doc)

        assert len(result.stroke_paths) == 3
