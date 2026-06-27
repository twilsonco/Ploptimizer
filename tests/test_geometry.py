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
        from plt_optimizer.utils.geometry import _segment_to_coords
        from plt_optimizer.core.models import ArcSegment

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


class TestRemoveRedundantStrokes:
    """Tests for remove_redundant_strokes function (lines 275-373)."""

    def test_no_redundant_strokes(self) -> None:
        """Test document with no redundant strokes is unchanged."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes
        from plt_optimizer.core.models import PLTDocument, StrokePath

        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=50.0, y=50.0),
            end=Coordinate(x=150.0, y=50.0),
            is_cutting=True,
        )
        path1 = StrokePath(pen_up_position=None, segments=(seg1,))
        path2 = StrokePath(pen_up_position=None, segments=(seg2,))
        doc = PLTDocument(
            header_commands=["IN;"],
            stroke_paths=[path1, path2],
            footer_commands=["SP;"],
        )

        result = remove_redundant_strokes(doc)

        assert len(result.stroke_paths) == 2
        assert list(result.header_commands) == ["IN;"]
        assert list(result.footer_commands) == ["SP;"]

    def test_rapid_segment_not_removed(self) -> None:
        """Test that rapid (non-cutting) segments are not considered redundant."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes
        from plt_optimizer.core.models import PLTDocument, StrokePath

        # This is a cutting segment on path1
        seg_cutting = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        # This is a rapid (non-cutting) segment - should be ignored in comparison
        seg_rapid = StrokeSegment(
            start=Coordinate(x=50.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=False,
        )
        path1 = StrokePath(pen_up_position=None, segments=(seg_cutting,))
        path2 = StrokePath(pen_up_position=None, segments=(seg_rapid,))
        doc = PLTDocument(header_commands=[], stroke_paths=[path1, path2], footer_commands=[])

        result = remove_redundant_strokes(doc)

        # Both should remain since rapid segments aren't checked
        assert len(result.stroke_paths) == 2

    def test_arc_segment_not_checked(self) -> None:
        """Test that arc segments are not checked for redundancy."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes
        from plt_optimizer.core.models import ArcSegment, PLTDocument, StrokePath

        # Cutting segment on line 1
        seg_line = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        # Arc segment - should be skipped in redundancy check
        arc = ArcSegment(
            start=Coordinate(x=10.0, y=0.0),
            end=Coordinate(x=90.0, y=0.0),
            center=Coordinate(x=50.0, y=-40.0),
            sweep_angle=180.0,
            is_cutting=True,
        )
        path1 = StrokePath(pen_up_position=None, segments=(seg_line,))
        path2 = StrokePath(pen_up_position=None, segments=(arc,))
        doc = PLTDocument(header_commands=[], stroke_paths=[path1, path2], footer_commands=[])

        result = remove_redundant_strokes(doc)

        # Both remain because arcs are skipped
        assert len(result.stroke_paths) == 2

    def test_endpoint_on_other_segment(self) -> None:
        """Test removal when one segment's endpoint lies on another."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes
        from plt_optimizer.core.models import PLTDocument, StrokePath

        # Seg1 is the "long" path (0,0) to (100,0)
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        # Seg2 lies entirely on seg1's path - should be redundant
        seg2 = StrokeSegment(
            start=Coordinate(x=25.0, y=0.0),
            end=Coordinate(x=75.0, y=0.0),
            is_cutting=True,
        )
        path1 = StrokePath(pen_up_position=None, segments=(seg1,))
        path2 = StrokePath(
            pen_up_position=Coordinate(x=25.0, y=0.0),  # Will be set if removed
            segments=(seg2,),
        )
        doc = PLTDocument(header_commands=[], stroke_paths=[path1, path2], footer_commands=[])

        result = remove_redundant_strokes(doc)

        # seg2 should be removed
        assert len(result.stroke_paths) == 1

    def test_both_segments_same_position_removed(self) -> None:
        """Test that when both segments are on each other and same length, one is removed."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes
        from plt_optimizer.core.models import PLTDocument, StrokePath

        # Both segments are identical (same start/end)
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=50.0, y=0.0),
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=50.0, y=0.0),
            is_cutting=True,
        )
        path1 = StrokePath(pen_up_position=None, segments=(seg1,))
        path2 = StrokePath(
            pen_up_position=Coordinate(x=0.0, y=0.0),
            segments=(seg2,),
        )
        doc = PLTDocument(header_commands=[], stroke_paths=[path1, path2], footer_commands=[])

        result = remove_redundant_strokes(doc)

        # seg2 should be removed (the second one)
        assert len(result.stroke_paths) == 1

    def test_reversed_segments_removed(self) -> None:
        """Test that reversed duplicate segments are detected and removed."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes
        from plt_optimizer.core.models import PLTDocument, StrokePath

        # Seg1 is (0,0) to (50,0)
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=50.0, y=0.0),
            is_cutting=True,
        )
        # Seg2 is reversed: (50,0) to (0,0)
        seg2 = StrokeSegment(
            start=Coordinate(x=50.0, y=0.0),
            end=Coordinate(x=0.0, y=0.0),
            is_cutting=True,
        )
        path1 = StrokePath(pen_up_position=None, segments=(seg1,))
        path2 = StrokePath(
            pen_up_position=Coordinate(x=50.0, y=0.0),
            segments=(seg2,),
        )
        doc = PLTDocument(header_commands=[], stroke_paths=[path1, path2], footer_commands=[])

        result = remove_redundant_strokes(doc)

        # seg2 should be removed (it's the reverse of seg1)
        assert len(result.stroke_paths) == 1

    def test_different_length_same_line_removes_shorter(self) -> None:
        """Test that when both on each other but different lengths, shorter is removed."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes
        from plt_optimizer.core.models import PLTDocument, StrokePath

        # Seg1 is longer (0,0) to (100,0)
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        # Seg2 is shorter but on the same line
        seg2 = StrokeSegment(
            start=Coordinate(x=25.0, y=0.0),
            end=Coordinate(x=75.0, y=0.0),  # length = 50
            is_cutting=True,
        )
        path1 = StrokePath(pen_up_position=None, segments=(seg1,))
        path2 = StrokePath(
            pen_up_position=Coordinate(x=25.0, y=0.0),
            segments=(seg2,),
        )
        doc = PLTDocument(header_commands=[], stroke_paths=[path1, path2], footer_commands=[])

        result = remove_redundant_strokes(doc)

        # seg2 (shorter) should be removed
        assert len(result.stroke_paths) == 1

    def test_multi_segment_path_with_removal(self) -> None:
        """Test removal from a path with multiple segments."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes
        from plt_optimizer.core.models import PLTDocument, StrokePath

        # Path with two cutting segments that match another path's segment
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=100.0, y=0.0),
            end=Coordinate(x=200.0, y=0.0),  # Also on path3
            is_cutting=True,
        )
        # A single segment that matches only part of the above
        seg3 = StrokeSegment(
            start=Coordinate(x=50.0, y=0.0),
            end=Coordinate(x=150.0, y=0.0),  # Overlaps with path1+seg2 combined
            is_cutting=True,
        )
        path1 = StrokePath(pen_up_position=None, segments=(seg1, seg2))
        path2 = StrokePath(
            pen_up_position=Coordinate(x=50.0, y=0.0),
            segments=(seg3,),
        )
        doc = PLTDocument(header_commands=[], stroke_paths=[path1, path2], footer_commands=[])

        result = remove_redundant_strokes(doc)

        # Should have at least one path remaining
        assert len(result.stroke_paths) >= 1

    def test_empty_document(self) -> None:
        """Test removing redundant strokes from empty document."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes
        from plt_optimizer.core.models import PLTDocument

        doc = PLTDocument(header_commands=[], stroke_paths=[], footer_commands=[])
        result = remove_redundant_strokes(doc)

        assert len(result.stroke_paths) == 0

    def test_path_with_all_segments_removed(self) -> None:
        """Test that paths with all segments removed are excluded."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes
        from plt_optimizer.core.models import PLTDocument, StrokePath

        # Both paths have segments on each other - first one keeps the longer
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),  # Longer
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=25.0, y=0.0),
            end=Coordinate(x=75.0, y=0.0),  # Shorter - will be removed
            is_cutting=True,
        )
        path1 = StrokePath(pen_up_position=None, segments=(seg1,))
        path2 = StrokePath(
            pen_up_position=Coordinate(x=25.0, y=0.0),
            segments=(seg2,),
        )
        doc = PLTDocument(header_commands=[], stroke_paths=[path1, path2], footer_commands=[])

        result = remove_redundant_strokes(doc)

        # Only seg1 should remain
        assert len(result.stroke_paths) == 1

    def test_preserves_header_footer(self) -> None:
        """Test that header and footer commands are preserved."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes
        from plt_optimizer.core.models import PLTDocument, StrokePath

        seg = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        path = StrokePath(pen_up_position=None, segments=(seg,))
        doc = PLTDocument(
            header_commands=["IN;", "VS20;"],
            stroke_paths=[path],
            footer_commands=["SP1;", "PG;"],
        )

        result = remove_redundant_strokes(doc)

        assert list(result.header_commands) == ["IN;", "VS20;"]
        assert list(result.footer_commands) == ["SP1;", "PG;"]


class TestFractureLinearPathsEdgeCases:
    """Additional edge case tests for fracture_linear_paths (line 410)."""

    def test_skip_empty_segments_path(self) -> None:
        """Test that paths with empty segments tuple are skipped."""
        from plt_optimizer.utils.geometry import fracture_linear_paths
        from plt_optimizer.core.models import PLTDocument, StrokePath

        # Create a path with no segments (empty tuple)
        empty_path = StrokePath(pen_up_position=None, segments=())
        seg = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=50.0),
            is_cutting=True,
        )
        normal_path = StrokePath(pen_up_position=None, segments=(seg,))
        doc = PLTDocument(header_commands=[], stroke_paths=[empty_path, normal_path], footer_commands=[])

        result = fracture_linear_paths(doc)

        # Empty path should be skipped, only the normal path is fractured
        assert len(result.stroke_paths) == 1

    def test_multiple_empty_paths_skipped(self) -> None:
        """Test that multiple empty paths are all skipped."""
        from plt_optimizer.utils.geometry import fracture_linear_paths
        from plt_optimizer.core.models import PLTDocument, StrokePath

        empty_path1 = StrokePath(pen_up_position=None, segments=())
        empty_path2 = StrokePath(pen_up_position=None, segments=())

        doc = PLTDocument(header_commands=[], stroke_paths=[empty_path1, empty_path2], footer_commands=[])

        result = fracture_linear_paths(doc)

        # All paths are empty, none should appear
        assert len(result.stroke_paths) == 0

    def test_pen_up_position_set_correctly(self) -> None:
        """Test that pen_up_position is set to segment start after fracturing."""
        from plt_optimizer.utils.geometry import fracture_linear_paths
        from plt_optimizer.core.models import PLTDocument, StrokePath

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


class TestRemoveRedundantStrokesBranches:
    """Tests to cover specific branches in remove_redundant_strokes."""

    def test_same_length_reversed_not_matching(self) -> None:
        """Test same-length segments that don't match even when considering reversal."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes
        from plt_optimizer.core.models import PLTDocument, StrokePath

        # Both length 50 but different positions (not matching start/end)
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=50.0, y=0.0),
            is_cutting=True,
        )
        # Same length but shifted position
        seg2 = StrokeSegment(
            start=Coordinate(x=100.0, y=10.0),  # Different from seg1.start
            end=Coordinate(x=150.0, y=10.0),    # Different from seg1.end
            is_cutting=True,
        )
        path1 = StrokePath(pen_up_position=None, segments=(seg1,))
        path2 = StrokePath(
            pen_up_position=Coordinate(x=100.0, y=10.0),
            segments=(seg2,),
        )
        doc = PLTDocument(header_commands=[], stroke_paths=[path1, path2], footer_commands=[])

        result = remove_redundant_strokes(doc)

        # They are on each other only if they're collinear and within bounds
        # These aren't so should remain
        assert len(result.stroke_paths) == 2

    def test_both_on_each_other_different_length(self) -> None:
        """Test segments where longer segment contains shorter but NOT vice versa."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes
        from plt_optimizer.core.models import PLTDocument, StrokePath

        # Long segment (100 units)
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        # Shorter segment on same line (50 units in middle)
        seg2 = StrokeSegment(
            start=Coordinate(x=25.0, y=0.0),
            end=Coordinate(x=75.0, y=0.0),  # length = 50
            is_cutting=True,
        )
        path1 = StrokePath(pen_up_position=None, segments=(seg1,))
        path2 = StrokePath(
            pen_up_position=Coordinate(x=25.0, y=0.0),
            segments=(seg2,),
        )
        doc = PLTDocument(header_commands=[], stroke_paths=[path1, path2], footer_commands=[])

        result = remove_redundant_strokes(doc)

        # Shorter one (seg2) should be removed
        assert len(result.stroke_paths) == 1

    def test_on_i_on_j_only_removes_i(self) -> None:
        """Test branch where seg_i lies on seg_j but not vice versa - removes i."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes
        from plt_optimizer.core.models import PLTDocument, StrokePath

        # seg1 is a SHORT segment (50 units)
        seg1 = StrokeSegment(
            start=Coordinate(x=25.0, y=0.0),
            end=Coordinate(x=75.0, y=0.0),  # length = 50
            is_cutting=True,
        )
        # seg2 is a LONGER segment (100 units) that contains seg1
        seg2 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),  # length = 100 - longer
            is_cutting=True,
        )
        path1 = StrokePath(pen_up_position=None, segments=(seg1,))
        path2 = StrokePath(
            pen_up_position=Coordinate(x=0.0, y=0.0),
            segments=(seg2,),
        )
        doc = PLTDocument(header_commands=[], stroke_paths=[path1, path2], footer_commands=[])

        result = remove_redundant_strokes(doc)

        # seg1 (shorter) is on seg2 (longer), so seg1 should be removed
        assert len(result.stroke_paths) == 1

    def test_same_length_shifted_no_reversed_match(self) -> None:
        """Test same-length segments that are collinear but don't match start/end or as reversals."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes
        from plt_optimizer.core.models import PLTDocument, StrokePath

        # Both segments have length 50 and lie on the same infinite line,
        # but they're at different positions (not matching forward or reverse)
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=50.0, y=0.0),  # length = 50
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=100.0, y=0.0),
            end=Coordinate(x=150.0, y=0.0),  # length = 50 but at different position
            is_cutting=True,
        )
        path1 = StrokePath(pen_up_position=None, segments=(seg1,))
        path2 = StrokePath(
            pen_up_position=Coordinate(x=100.0, y=0.0),
            segments=(seg2,),
        )
        doc = PLTDocument(header_commands=[], stroke_paths=[path1, path2], footer_commands=[])

        result = remove_redundant_strokes(doc)

        # They are on each other's infinite line extension but NOT within the
        # finite segment bounds, so neither should be removed by "both_on_each_other"
        assert len(result.stroke_paths) == 2

    def test_both_on_each_other_different_lengths_removes_shorter(self) -> None:
        """Test both-on-each-other with different lengths: shorter is always removed."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes
        from plt_optimizer.core.models import PLTDocument, StrokePath

        # seg1 is (0,0)->(100,0), length = 100
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        # seg2 is (25,0)->(75,0), length = 50 - fully inside seg1
        seg2 = StrokeSegment(
            start=Coordinate(x=25.0, y=0.0),
            end=Coordinate(x=75.0, y=0.0),
            is_cutting=True,
        )
        path1 = StrokePath(pen_up_position=None, segments=(seg1,))
        path2 = StrokePath(
            pen_up_position=Coordinate(x=25.0, y=0.0),
            segments=(seg2,),
        )
        doc = PLTDocument(header_commands=[], stroke_paths=[path1, path2], footer_commands=[])

        result = remove_redundant_strokes(doc)

        # seg2 (shorter) should be removed
        assert len(result.stroke_paths) == 1

    def test_only_one_endpoint_on_other_segment(self) -> None:
        """Test when only start_i and end_j are on segment but not both."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes
        from plt_optimizer.core.models import PLTDocument, StrokePath

        # Long horizontal line
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        # Diagonal that doesn't fully lie on the line
        seg2 = StrokeSegment(
            start=Coordinate(x=25.0, y=0.0),  # On line1
            end=Coordinate(x=75.0, y=5.0),    # NOT on line1 (y=5)
            is_cutting=True,
        )
        path1 = StrokePath(pen_up_position=None, segments=(seg1,))
        path2 = StrokePath(
            pen_up_position=Coordinate(x=25.0, y=0.0),
            segments=(seg2,),
        )
        doc = PLTDocument(header_commands=[], stroke_paths=[path1, path2], footer_commands=[])

        result = remove_redundant_strokes(doc)

        # Only one point on line, so neither is removed
        assert len(result.stroke_paths) == 2

    def test_path_split_by_removed_segment(self) -> None:
        """Test when removal creates separate stroke paths."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes
        from plt_optimizer.core.models import PLTDocument, StrokePath

        # Three segments in one path: A->B (cut), B->C (cut - will be removed),
        # C->D (cut)
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=50.0, y=0.0),
            is_cutting=True,
        )
        seg2 = StrokeSegment(  # Will be removed - same as A->B
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=50.0, y=0.0),
            is_cutting=True,
        )
        seg3 = StrokeSegment(
            start=Coordinate(x=50.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )

        # Path1 has 3 segments
        path1 = StrokePath(pen_up_position=None, segments=(seg1, seg2, seg3))
        doc = PLTDocument(header_commands=[], stroke_paths=[path1], footer_commands=[])

        result = remove_redundant_strokes(doc)

        # After removing seg2 (which is index 1), the path splits into two:
        # one with seg1 and one with seg3
        assert len(result.stroke_paths) == 2
