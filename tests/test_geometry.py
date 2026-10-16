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
        from plt_optimizer.core.models import Coordinate, StrokeSegment
        from plt_optimizer.utils.geometry import calculate_stroke_path_length

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
        from plt_optimizer.core.models import Coordinate, StrokeSegment
        from plt_optimizer.utils.geometry import calculate_stroke_path_length

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
        from plt_optimizer.core.models import Coordinate, StrokeSegment
        from plt_optimizer.utils.geometry import calculate_stroke_path_length

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
        assert math.isclose(result[2], 5.0)  # max_x
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
        assert math.isclose(result[2], 100.0)  # max_x
        assert math.isclose(result[3], 200.0)  # max_y

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
        from plt_optimizer.utils.geometry import Coordinate, calculate_cumulative_distances

        start = Coordinate(x=0.0, y=0.0)
        end = Coordinate(x=3.0, y=4.0)

        result = calculate_cumulative_distances([(start, end)])
        assert len(result) == 1
        assert math.isclose(result[0], 5.0)

    def test_multiple_segments(self) -> None:
        """Test cumulative distances with multiple segments."""
        from plt_optimizer.utils.geometry import Coordinate, calculate_cumulative_distances

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
        from plt_optimizer.utils.geometry import Coordinate, calculate_cumulative_distances

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
        from plt_optimizer.utils.geometry import Coordinate, calculate_cumulative_distances

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
        from plt_optimizer.core.models import PLTDocument, StrokePath
        from plt_optimizer.utils.geometry import fracture_linear_paths

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
        """Test that a closed rectangle is preserved as a single path (not fractured)."""
        from plt_optimizer.core.models import PLTDocument, StrokePath
        from plt_optimizer.utils.geometry import fracture_linear_paths

        # Rectangle: (0,0) -> (100,0) -> (100,50) -> (0,50) -> (0,0)
        # This is a closed path, so it should be preserved as one block
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

        # Closed path should be preserved as a single path with all 4 segments
        assert len(result.stroke_paths) == 1
        assert len(result.stroke_paths[0].segments) == 4

    def test_fracture_preserves_arc_paths(self) -> None:
        """Test that paths containing arcs are preserved intact."""
        from plt_optimizer.core.models import ArcSegment, PLTDocument, StrokePath
        from plt_optimizer.utils.geometry import fracture_linear_paths

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
        from plt_optimizer.core.models import PLTDocument
        from plt_optimizer.utils.geometry import fracture_linear_paths

        doc = PLTDocument(header_commands=[], stroke_paths=[], footer_commands=[])
        result = fracture_linear_paths(doc)

        assert len(result.stroke_paths) == 0

    def test_fracture_mixed_paths(self) -> None:
        """Test fracturing a document with both linear and arc paths."""
        from plt_optimizer.core.models import ArcSegment, PLTDocument, StrokePath
        from plt_optimizer.utils.geometry import fracture_linear_paths

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
            header_commands=[], stroke_paths=[linear_path, arc_path], footer_commands=[]
        )

        result = fracture_linear_paths(doc)

        # 1 fractured linear path + 1 preserved arc path
        assert len(result.stroke_paths) == 2

    def test_fracture_polygon(self) -> None:
        """Test that a closed polygon is preserved as a single path (not fractured)."""
        from plt_optimizer.core.models import PLTDocument, StrokePath
        from plt_optimizer.utils.geometry import fracture_linear_paths

        # Triangle: (0,0) -> (50,100) -> (100,0) -> (0,0)
        # This is a closed path, so it should be preserved as one block
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

        # Closed path should be preserved as a single path with all 3 segments
        assert len(result.stroke_paths) == 1
        assert len(result.stroke_paths[0].segments) == 3


class TestSegmentToCoords:
    """Tests for _segment_to_coords function (line 252)."""

    def test_segment_to_coords_stroke(self) -> None:
        """Test extracting coordinates from a StrokeSegment."""
        from plt_optimizer.utils.geometry import _segment_to_coords

        start = Coordinate(x=10.0, y=20.0)
        end = Coordinate(x=30.0, y=40.0)
        seg = StrokeSegment(start=start, end=end, is_cutting=True)

        result_start, result_end = _segment_to_coords(seg)

        assert math.isclose(result_start.x, 10.0)
        assert math.isclose(result_start.y, 20.0)
        assert math.isclose(result_end.x, 30.0)
        assert math.isclose(result_end.y, 40.0)

    def test_segment_to_coords_arc(self) -> None:
        """Test extracting coordinates from an ArcSegment."""
        from plt_optimizer.core.models import ArcSegment
        from plt_optimizer.utils.geometry import _segment_to_coords

        start = Coordinate(x=5.0, y=5.0)
        end = Coordinate(x=15.0, y=10.0)
        arc = ArcSegment(
            start=start,
            end=end,
            center=Coordinate(x=10.0, y=5.0),
            sweep_angle=90.0,
            is_cutting=True,
        )

        result_start, result_end = _segment_to_coords(arc)

        assert math.isclose(result_start.x, 5.0)
        assert math.isclose(result_start.y, 5.0)
        assert math.isclose(result_end.x, 15.0)
        assert math.isclose(result_end.y, 10.0)


class TestFractureLinearPathsEdgeCases:
    """Additional edge case tests for fracture_linear_paths (line 410)."""

    def test_skip_empty_segments_path(self) -> None:
        """Test that paths with empty segments tuple are skipped."""
        from plt_optimizer.core.models import PLTDocument, StrokePath
        from plt_optimizer.utils.geometry import fracture_linear_paths

        # Create a path with no segments (empty tuple)
        empty_path = StrokePath(pen_up_position=None, segments=())
        seg = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=50.0),
            is_cutting=True,
        )
        normal_path = StrokePath(pen_up_position=None, segments=(seg,))
        doc = PLTDocument(
            header_commands=[], stroke_paths=[empty_path, normal_path], footer_commands=[]
        )

        result = fracture_linear_paths(doc)

        # Empty path should be skipped, only the normal path is fractured
        assert len(result.stroke_paths) == 1

    def test_multiple_empty_paths_skipped(self) -> None:
        """Test that multiple empty paths are all skipped."""
        from plt_optimizer.core.models import PLTDocument, StrokePath
        from plt_optimizer.utils.geometry import fracture_linear_paths

        empty_path1 = StrokePath(pen_up_position=None, segments=())
        empty_path2 = StrokePath(pen_up_position=None, segments=())

        doc = PLTDocument(
            header_commands=[], stroke_paths=[empty_path1, empty_path2], footer_commands=[]
        )

        result = fracture_linear_paths(doc)

        # All paths are empty, none should appear
        assert len(result.stroke_paths) == 0

    def test_pen_up_position_set_correctly(self) -> None:
        """Test that pen_up_position is set to segment start after fracturing."""
        from plt_optimizer.core.models import PLTDocument, StrokePath
        from plt_optimizer.utils.geometry import fracture_linear_paths

        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=50.0, y=50.0),
            is_cutting=True,
        )
        path = StrokePath(pen_up_position=None, segments=(seg1,))
        doc = PLTDocument(header_commands=[], stroke_paths=[path], footer_commands=[])

        result = fracture_linear_paths(doc)

        # After fracturing, pen_up_position should be the segment's start
        assert len(result.stroke_paths) == 1
        fractured_path = result.stroke_paths[0]
        assert math.isclose(fractured_path.pen_up_position.x, 0.0)
        assert math.isclose(fractured_path.pen_up_position.y, 0.0)


class TestIsPointOnSegmentEdgeCases:
    """Additional edge case tests for is_point_on_segment (line 233)."""

    def test_degenerate_zero_length_segment(self) -> None:
        """Test point on zero-length segment where A == B."""
        from plt_optimizer.utils.geometry import is_point_on_segment

        a = Coordinate(x=5.0, y=5.0)
        b = Coordinate(x=5.0, y=5.0)  # Same as A
        p = Coordinate(x=5.0, y=5.0)  # Point exactly at A/B

        result = is_point_on_segment(p, a, b, tol=1e-5)
        assert result is True

    def test_degenerate_segment_different_point(self) -> None:
        """Test point not on zero-length segment."""
        from plt_optimizer.utils.geometry import is_point_on_segment

        a = Coordinate(x=5.0, y=5.0)
        b = Coordinate(x=5.0, y=5.0)  # Same as A
        p = Coordinate(x=6.0, y=5.0)  # Point not at A/B

        result = is_point_on_segment(p, a, b, tol=1e-5)
        assert result is False


class TestIsClosedPath:
    """Tests for is_closed_path function (detects closed loops in stroke paths)."""

    def test_closed_rectangular_path(self) -> None:
        """Test a closed rectangular path (closed loop)."""
        from plt_optimizer.utils.geometry import is_closed_path

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

        assert is_closed_path(path) is True

    def test_open_path_not_closed(self) -> None:
        """Test an open path (not a closed loop)."""
        from plt_optimizer.utils.geometry import is_closed_path

        # Open path: (0,0) -> (100,0) -> (100,50) (doesn't return to start)
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
        path = StrokePath(pen_up_position=None, segments=(seg1, seg2))

        assert is_closed_path(path) is False

    def test_closed_ellipse_path(self) -> None:
        """Test a closed ellipse path (many segments)."""
        from plt_optimizer.utils.geometry import is_closed_path

        # Simulated ellipse polygon: 8 segments forming a closed loop
        start = Coordinate(x=50.0, y=0.0)
        points = [
            Coordinate(x=50.0, y=0.0),  # 0: start
            Coordinate(x=75.0, y=25.0),  # 1
            Coordinate(x=75.0, y=50.0),  # 2
            Coordinate(x=50.0, y=75.0),  # 3
            Coordinate(x=25.0, y=75.0),  # 4
            Coordinate(x=0.0, y=50.0),  # 5
            Coordinate(x=0.0, y=25.0),  # 6
            Coordinate(x=25.0, y=0.0),  # 7
            Coordinate(x=50.0, y=0.0),  # 8: back to start (closes the loop)
        ]

        segments = []
        for i in range(len(points) - 1):
            seg = StrokeSegment(start=points[i], end=points[i + 1], is_cutting=True)
            segments.append(seg)

        path = StrokePath(pen_up_position=None, segments=tuple(segments))

        assert is_closed_path(path) is True

    def test_single_segment_path_not_closed(self) -> None:
        """Test that a single-segment path is not considered closed."""
        from plt_optimizer.utils.geometry import is_closed_path

        seg = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=100.0),
            is_cutting=True,
        )
        path = StrokePath(pen_up_position=None, segments=(seg,))

        assert is_closed_path(path) is False

    def test_empty_path_not_closed(self) -> None:
        """Test that an empty path is not considered closed."""
        from plt_optimizer.utils.geometry import is_closed_path

        path = StrokePath(pen_up_position=None, segments=())

        assert is_closed_path(path) is False

    def test_closed_with_tolerance(self) -> None:
        """Test that nearly-closed paths are closed within tolerance."""
        from plt_optimizer.utils.geometry import is_closed_path

        # Triangle that almost closes (due to floating point, within default tolerance)
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=100.0, y=0.0),
            end=Coordinate(x=50.0, y=86.603),
            is_cutting=True,
        )
        seg3 = StrokeSegment(
            start=Coordinate(x=50.0, y=86.603),
            end=Coordinate(x=0.0, y=0.001),  # Nearly back to start
            is_cutting=True,
        )
        path = StrokePath(pen_up_position=None, segments=(seg1, seg2, seg3))

        # Should be closed within default tolerance (0.001)
        assert is_closed_path(path) is True


class TestFractureLinearPathsPreservesClosedPaths:
    """Tests for fracture_linear_paths preserving closed paths (updated behavior)."""

    def test_closed_rectangle_preserved(self) -> None:
        """Test that a closed rectangular path is preserved as-is (not fractured)."""
        from plt_optimizer.core.models import PLTDocument
        from plt_optimizer.utils.geometry import fracture_linear_paths

        # Closed rectangle: (0,0) -> (100,0) -> (100,50) -> (0,50) -> (0,0)
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

        # Closed path should NOT be fractured, should stay as one path
        assert len(result.stroke_paths) == 1
        assert len(result.stroke_paths[0].segments) == 4  # All 4 segments preserved

    def test_closed_ellipse_preserved(self) -> None:
        """Test that a closed ellipse polygon is preserved (not fractured)."""
        from plt_optimizer.core.models import PLTDocument
        from plt_optimizer.utils.geometry import fracture_linear_paths

        # Simulate a 64-segment ellipse polygon that closes
        num_points = 64
        segments = []
        for i in range(num_points):
            theta1 = 2 * math.pi * i / num_points
            theta2 = 2 * math.pi * (i + 1) / num_points
            x1 = 50 + 30 * math.cos(theta1)
            y1 = 50 + 20 * math.sin(theta1)
            x2 = 50 + 30 * math.cos(theta2)
            y2 = 50 + 20 * math.sin(theta2)
            seg = StrokeSegment(
                start=Coordinate(x=x1, y=y1),
                end=Coordinate(x=x2, y=y2),
                is_cutting=True,
            )
            segments.append(seg)

        # Close the loop by adjusting the last endpoint to match the first start
        last_seg = segments[-1]
        first_start = segments[0].start
        segments[-1] = StrokeSegment(
            start=last_seg.start, end=first_start, is_cutting=True
        )

        path = StrokePath(pen_up_position=None, segments=tuple(segments))
        doc = PLTDocument(header_commands=[], stroke_paths=[path], footer_commands=[])

        result = fracture_linear_paths(doc)

        # Closed ellipse should NOT be fractured, should stay as one path with 64 segments
        assert len(result.stroke_paths) == 1
        assert len(result.stroke_paths[0].segments) == 64

    def test_open_path_still_fractured(self) -> None:
        """Test that open paths are still fractured into individual segments."""
        from plt_optimizer.core.models import PLTDocument
        from plt_optimizer.utils.geometry import fracture_linear_paths

        # Open path: (0,0) -> (100,0) -> (100,50) (doesn't close)
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
        path = StrokePath(pen_up_position=None, segments=(seg1, seg2))
        doc = PLTDocument(header_commands=[], stroke_paths=[path], footer_commands=[])

        result = fracture_linear_paths(doc)

        # Open path should still be fractured into 2 separate single-segment paths
        assert len(result.stroke_paths) == 2
        assert len(result.stroke_paths[0].segments) == 1
        assert len(result.stroke_paths[1].segments) == 1

    def test_mixed_closed_and_open_paths(self) -> None:
        """Test that closed paths are preserved while open paths are fractured."""
        from plt_optimizer.core.models import PLTDocument
        from plt_optimizer.utils.geometry import fracture_linear_paths

        # Closed rectangle
        seg_closed1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        seg_closed2 = StrokeSegment(
            start=Coordinate(x=100.0, y=0.0),
            end=Coordinate(x=100.0, y=50.0),
            is_cutting=True,
        )
        seg_closed3 = StrokeSegment(
            start=Coordinate(x=100.0, y=50.0),
            end=Coordinate(x=0.0, y=50.0),
            is_cutting=True,
        )
        seg_closed4 = StrokeSegment(
            start=Coordinate(x=0.0, y=50.0),
            end=Coordinate(x=0.0, y=0.0),
            is_cutting=True,
        )
        closed_path = StrokePath(
            pen_up_position=None, segments=(seg_closed1, seg_closed2, seg_closed3, seg_closed4)
        )

        # Open path
        seg_open1 = StrokeSegment(
            start=Coordinate(x=200.0, y=0.0),
            end=Coordinate(x=300.0, y=0.0),
            is_cutting=True,
        )
        seg_open2 = StrokeSegment(
            start=Coordinate(x=300.0, y=0.0),
            end=Coordinate(x=300.0, y=100.0),
            is_cutting=True,
        )
        open_path = StrokePath(pen_up_position=None, segments=(seg_open1, seg_open2))

        doc = PLTDocument(
            header_commands=[],
            stroke_paths=[closed_path, open_path],
            footer_commands=[],
        )

        result = fracture_linear_paths(doc)

        # Should have 1 closed path (4 segments) + 2 fractured open segments = 3 total paths
        assert len(result.stroke_paths) == 3
        # First path is the closed rectangle (unchanged)
        assert len(result.stroke_paths[0].segments) == 4
        # Second and third are the fractured open segments
        assert len(result.stroke_paths[1].segments) == 1
        assert len(result.stroke_paths[2].segments) == 1
