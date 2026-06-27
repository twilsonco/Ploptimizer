"""Tests for plt_optimizer/core/profiler.py module.

This module provides baseline extent calculation using 95th percentile
of max bounding box dimension across cutting strokes.
"""

from __future__ import annotations

import math
import pytest

from plt_optimizer.core.models import Coordinate, PLTDocument, StrokePath, StrokeSegment
from plt_optimizer.core.profiler import Extent, ProfileResult, Profiler, ProfilerError


class TestExtent:
    """Tests for the Extent dataclass."""

    def test_max_dimension_width_greater(self) -> None:
        """Test max_dimension when width > height."""
        extent = Extent(dx=100.0, dy=50.0)
        assert extent.max_dimension == 100.0

    def test_max_dimension_height_greater(self) -> None:
        """Test max_dimension when height > width."""
        extent = Extent(dx=30.0, dy=80.0)
        assert extent.max_dimension == 80.0

    def test_max_dimension_equal(self) -> None:
        """Test max_dimension when width == height."""
        extent = Extent(dx=50.0, dy=50.0)
        assert extent.max_dimension == 50.0

    def test_euclidean_size(self) -> None:
        """Test euclidean_size returns diagonal length."""
        extent = Extent(dx=3.0, dy=4.0)
        # sqrt(9 + 16) = sqrt(25) = 5
        assert extent.euclidean_size == 5.0

    def test_euclidean_size_zero(self) -> None:
        """Test euclidean_size with zero dimensions."""
        extent = Extent(dx=0.0, dy=0.0)
        assert extent.euclidean_size == 0.0


class TestProfilerProfile:
    """Tests for Profiler.profile() method."""

    def test_profile_single_stroke(self) -> None:
        """Test profiling a document with single cutting stroke."""
        segment = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=50.0),
            is_cutting=True,
        )
        path = StrokePath(pen_up_position=None, segments=(segment,))
        doc = PLTDocument(header_commands=[], stroke_paths=[path], footer_commands=[])

        profiler = Profiler()
        result = profiler.profile(doc)

        assert isinstance(result, ProfileResult)
        assert result.baseline_extent > 0
        assert result.total_strokes == 1

    def test_profile_multiple_strokes(self) -> None:
        """Test profiling document with multiple cutting strokes."""
        segments = [
            StrokeSegment(
                start=Coordinate(x=i * 100.0, y=0.0),
                end=Coordinate(x=(i + 1) * 100.0, y=50.0),
                is_cutting=True,
            )
            for i in range(5)
        ]
        paths = [StrokePath(pen_up_position=None, segments=(seg,)) for seg in segments]
        doc = PLTDocument(header_commands=[], stroke_paths=paths, footer_commands=[])

        profiler = Profiler()
        result = profiler.profile(doc)

        assert result.total_strokes == 5
        # Should use 95th percentile of max dimensions

    def test_profile_only_rapid_moves_raises_error(self) -> None:
        """Test that profile raises error when no cutting strokes found."""
        segment = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=50.0),
            is_cutting=False,  # Rapid move
        )
        path = StrokePath(pen_up_position=None, segments=(segment,))
        doc = PLTDocument(header_commands=[], stroke_paths=[path], footer_commands=[])

        profiler = Profiler()
        with pytest.raises(ProfilerError) as exc_info:
            profiler.profile(doc)
        assert "No cutting strokes found" in str(exc_info.value.message)

    def test_profile_empty_document_raises_error(self) -> None:
        """Test profiling empty document raises error."""
        doc = PLTDocument(header_commands=[], stroke_paths=[], footer_commands=[])

        profiler = Profiler()
        with pytest.raises(ProfilerError):
            profiler.profile(doc)

    def test_profile_zero_length_segments_ignored(self) -> None:
        """Test that zero-length segments are not counted."""
        # Create a path with one valid segment and one zero-length
        valid_segment = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=50.0),
            is_cutting=True,
        )
        zero_segment = StrokeSegment(
            start=Coordinate(x=100.0, y=50.0),
            end=Coordinate(x=100.0, y=50.0),  # Same point - zero length
            is_cutting=True,
        )
        path = StrokePath(pen_up_position=None, segments=(valid_segment, zero_segment))
        doc = PLTDocument(header_commands=[], stroke_paths=[path], footer_commands=[])

        profiler = Profiler()
        result = profiler.profile(doc)
        # Should only count 1 (the non-zero segment)
        assert result.total_strokes == 1

    def test_profile_95th_percentile_calculation(self) -> None:
        """Test that baseline_extent uses 95th percentile of max dimensions."""
        # Create strokes with known extents
        segments = [
            StrokeSegment(
                start=Coordinate(x=i * 10.0, y=i * 10.0),
                end=Coordinate(x=i * 10.0 + 100.0, y=i * 10.0),
                is_cutting=True,
            )
            for i in range(20)
        ]
        paths = [StrokePath(pen_up_position=None, segments=(seg,)) for seg in segments]
        doc = PLTDocument(header_commands=[], stroke_paths=paths, footer_commands=[])

        profiler = Profiler()
        result = profiler.profile(doc)

        # p95_index should be around 19 (0.95 * 20 - 1)
        assert result.p95_index == 19
        # Baseline extent is the value at that index in sorted dimensions

    def test_profile_arc_segments_skipped(self) -> None:
        """Test that arc segments are skipped by profiler."""
        from plt_optimizer.core.models import ArcSegment

        # Create an arc segment - should be skipped
        arc_segment = ArcSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=50.0),
            center=Coordinate(x=50.0, y=25.0),
            sweep_angle=45.0,
            is_cutting=True,
        )
        path = StrokePath(pen_up_position=None, segments=(arc_segment,))
        doc = PLTDocument(header_commands=[], stroke_paths=[path], footer_commands=[])

        profiler = Profiler()
        with pytest.raises(ProfilerError):
            # No cutting strokes counted because arcs are skipped
            profiler.profile(doc)


class TestProfileResult:
    """Tests for ProfileResult dataclass."""

    def test_profile_result_fields(self) -> None:
        """Test ProfileResult has all expected fields."""
        result = ProfileResult(
            baseline_extent=100.0,
            median_dx=50.0,
            median_dy=40.0,
            total_strokes=10,
            p95_index=9,
            is_structural=False,
        )

        assert result.baseline_extent == 100.0
        assert result.median_dx == 50.0
        assert result.median_dy == 40.0
        assert result.total_strokes == 10
        assert result.p95_index == 9

    def test_profile_result_frozen(self) -> None:
        """Test ProfileResult is immutable."""
        from dataclasses import FrozenInstanceError

        result = ProfileResult(
            baseline_extent=100.0,
            median_dx=50.0,
            median_dy=40.0,
            total_strokes=10,
            p95_index=9,
            is_structural=False,
        )

        with pytest.raises(FrozenInstanceError):
            result.baseline_extent = 200.0


class TestProfilerEdgeCases:
    """Tests for edge cases in Profiler."""

    def test_profile_single_element(self) -> None:
        """Test profiling document with exactly one cutting stroke."""
        segment = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=42.0, y=42.0),
            is_cutting=True,
        )
        path = StrokePath(pen_up_position=None, segments=(segment,))
        doc = PLTDocument(header_commands=[], stroke_paths=[path], footer_commands=[])

        profiler = Profiler()
        result = profiler.profile(doc)

        assert result.total_strokes == 1
        # p95_index should be 0 for single element (len*0.95 = 0.95, truncated to 0)
        assert result.p95_index == 0

    def test_profile_multiple_paths_single_segment_each(self) -> None:
        """Test profiling with multiple paths each having one segment."""
        paths = []
        for i in range(3):
            segment = StrokeSegment(
                start=Coordinate(x=i * 100.0, y=0.0),
                end=Coordinate(x=(i + 1) * 100.0, y=50.0),
                is_cutting=True,
            )
            path = StrokePath(pen_up_position=None, segments=(segment,))
            paths.append(path)

        doc = PLTDocument(header_commands=[], stroke_paths=paths, footer_commands=[])

        profiler = Profiler()
        result = profiler.profile(doc)

        assert result.total_strokes == 3


class TestIsStructuralPath:
    """Tests for structural path classification with geometric analysis."""

    def test_single_segment_is_structural(self) -> None:
        """Test that a single straight segment is classified as structural."""
        from plt_optimizer.core.models import ArcSegment

        profiler = Profiler()

        # Single line segment - should be structural (score/cut line)
        segment = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=50.0),
            is_cutting=True,
        )
        path = StrokePath(pen_up_position=None, segments=(segment,))

        assert profiler._is_structural_path(path) is True

    def test_closed_loop_rectangle_is_structural(self) -> None:
        """Test that a closed loop rectangle with long segments is structural."""
        profiler = Profiler()

        # Rectangle: (0,0) -> (100,0) -> (100,50) -> (0,50) -> (0,0)
        seg1 = StrokeSegment(start=Coordinate(x=0.0, y=0.0), end=Coordinate(x=100.0, y=0.0), is_cutting=True)
        seg2 = StrokeSegment(start=Coordinate(x=100.0, y=0.0), end=Coordinate(x=100.0, y=50.0), is_cutting=True)
        seg3 = StrokeSegment(start=Coordinate(x=100.0, y=50.0), end=Coordinate(x=0.0, y=50.0), is_cutting=True)
        seg4 = StrokeSegment(start=Coordinate(x=0.0, y=50.0), end=Coordinate(x=0.0, y=0.0), is_cutting=True)

        path = StrokePath(pen_up_position=None, segments=(seg1, seg2, seg3, seg4))

        assert profiler._is_structural_path(path) is True

    def test_engravelab_drill_hole_is_structural(self) -> None:
        """Test that EngraveLab 4-arc drill hole pattern is structural."""
        from plt_optimizer.core.models import ArcSegment

        profiler = Profiler()

        # Create a mock drill hole: 4 arcs of 90 degrees each
        arc1 = ArcSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=0.0),
            center=Coordinate(x=5.0, y=0.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        arc2 = ArcSegment(
            start=Coordinate(x=10.0, y=0.0),
            end=Coordinate(x=10.0, y=10.0),
            center=Coordinate(x=10.0, y=5.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        arc3 = ArcSegment(
            start=Coordinate(x=10.0, y=10.0),
            end=Coordinate(x=0.0, y=10.0),
            center=Coordinate(x=5.0, y=10.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        arc4 = ArcSegment(
            start=Coordinate(x=0.0, y=10.0),
            end=Coordinate(x=0.0, y=0.0),
            center=Coordinate(x=0.0, y=5.0),
            sweep_angle=-90.0,
            is_cutting=True,
        )

        path = StrokePath(pen_up_position=None, segments=(arc1, arc2, arc3, arc4))

        assert profiler._is_structural_path(path) is True

    def test_text_like_path_not_structural(self) -> None:
        """Test that a path with many small segments (text-like) is NOT structural."""
        profiler = Profiler()

        # Create text-like path: multiple tiny segments
        # Simulating character "e" or similar - many short strokes
        segments = []
        x, y = 0.0, 0.0
        for i in range(20):
            seg = StrokeSegment(
                start=Coordinate(x=x, y=y),
                end=Coordinate(x=x + 2.0, y=y + 1.0),  # tiny segment
                is_cutting=True,
            )
            segments.append(seg)
            x += 2.0
            y += 1.0

        path = StrokePath(pen_up_position=None, segments=tuple(segments))

        # Text-like paths should NOT be classified as structural
        assert profiler._is_structural_path(path) is False

    def test_open_polygon_not_closed_loop(self) -> None:
        """Test that an open polygon (not closed) is not a closed loop structural."""
        profiler = Profiler()

        # Open path: (0,0) -> (100,0) -> (100,50) -> (0,50)
        seg1 = StrokeSegment(start=Coordinate(x=0.0, y=0.0), end=Coordinate(x=100.0, y=0.0), is_cutting=True)
        seg2 = StrokeSegment(start=Coordinate(x=100.0, y=0.0), end=Coordinate(x=100.0, y=50.0), is_cutting=True)
        seg3 = StrokeSegment(start=Coordinate(x=100.0, y=50.0), end=Coordinate(x=0.0, y=50.0), is_cutting=True)

        path = StrokePath(pen_up_position=None, segments=(seg1, seg2, seg3))

        # This should not be classified as closed-loop structural
        # (Note: it might still pass Check 5 if segment length ratio is high enough)
        result = profiler._is_structural_path(path)

    def test_calculate_average_segment_length(self) -> None:
        """Test average segment length calculation."""
        seg1 = StrokeSegment(start=Coordinate(x=0.0, y=0.0), end=Coordinate(x=3.0, y=4.0), is_cutting=True)
        seg2 = StrokeSegment(start=Coordinate(x=3.0, y=4.0), end=Coordinate(x=6.0, y=4.0), is_cutting=True)

        path = StrokePath(pen_up_position=None, segments=(seg1, seg2))

        profiler = Profiler()
        avg_length = profiler._calculate_average_segment_length(path)

        # First segment length = 5 (3-4-5 triangle)
        # Second segment length = 3
        # Average = 4
        assert math.isclose(avg_length, 4.0)

    def test_calculate_bounding_box_extent(self) -> None:
        """Test bounding box extent calculation."""
        seg1 = StrokeSegment(start=Coordinate(x=10.0, y=20.0), end=Coordinate(x=110.0, y=70.0), is_cutting=True)
        seg2 = StrokeSegment(start=Coordinate(x=110.0, y=70.0), end=Coordinate(x=-30.0, y=50.0), is_cutting=True)

        path = StrokePath(pen_up_position=None, segments=(seg1, seg2))

        profiler = Profiler()
        extent = profiler._calculate_bounding_box_extent(path)

        # min_x = -30, max_x = 110 -> dx = 140
        # min_y = 20, max_y = 70 -> dy = 50
        # extent = max(140, 50) = 140
        assert math.isclose(extent, 140.0)


class TestStructuralClassification:
    """Tests for overall structural classification based on ratio threshold."""

    def test_structural_threshold_85_percent(self) -> None:
        """Test that is_structural=True when >85% of paths are structural."""
        from plt_optimizer.core.models import ArcSegment

        profiler = Profiler()

        # Create 10 single-segment paths (all structural)
        paths = []
        for i in range(9):
            segment = StrokeSegment(
                start=Coordinate(x=i * 100.0, y=0.0),
                end=Coordinate(x=(i + 1) * 100.0, y=50.0),
                is_cutting=True,
            )
            paths.append(StrokePath(pen_up_position=None, segments=(segment,)))

        # Add one text-like path with many small segments (not structural)
        tiny_segments = tuple(
            StrokeSegment(
                start=Coordinate(x=j * 2.0, y=200.0),
                end=Coordinate(x=(j + 1) * 2.0, y=201.0),  # tiny segment
                is_cutting=True,
            )
            for j in range(20)
        )
        paths.append(StrokePath(pen_up_position=None, segments=tiny_segments))

        doc = PLTDocument(header_commands=[], stroke_paths=paths, footer_commands=[])

        result = profiler.profile(doc)

        # 9/10 = 90% structural > 85%, so is_structural should be True
        assert result.is_structural is True

    def test_mixed_file_not_structural(self) -> None:
        """Test that a mixed file (not >85% structural) returns False."""
        profiler = Profiler()

        # Create 5 single-segment paths (structural)
        paths = []
        for i in range(5):
            segment = StrokeSegment(
                start=Coordinate(x=i * 100.0, y=0.0),
                end=Coordinate(x=(i + 1) * 100.0, y=50.0),
                is_cutting=True,
            )
            paths.append(StrokePath(pen_up_position=None, segments=(segment,)))

        # Add 5 text-like paths (not structural)
        for i in range(5):
            tiny_segments = tuple(
                StrokeSegment(
                    start=Coordinate(x=j * 2.0 + 500, y=i * 100.0),
                    end=Coordinate(x=(j + 1) * 2.0 + 500, y=i * 100.0 + 1.0),
                    is_cutting=True,
                )
                for j in range(20)
            )
            paths.append(StrokePath(pen_up_position=None, segments=tiny_segments))

        doc = PLTDocument(header_commands=[], stroke_paths=paths, footer_commands=[])

        result = profiler.profile(doc)

        # 5/10 = 50% structural < 85%, so is_structural should be False
        assert result.is_structural is False


class TestProfilerEdgeCasesCoverage:
    """Additional tests to improve code coverage for edge cases."""

    def test_profile_paths_with_empty_segments_filtered(self) -> None:
        """Test profiling when valid_paths filters out empty paths (total_paths calculation)."""
        profiler = Profiler()

        # Create one path with segments and one without
        valid_path = StrokePath(
            pen_up_position=None,
            segments=(
                StrokeSegment(
                    start=Coordinate(x=0.0, y=0.0),
                    end=Coordinate(x=100.0, y=50.0),
                    is_cutting=True,
                ),
            ),
        )
        empty_path = StrokePath(pen_up_position=None, segments=())  # Empty - filtered out

        doc = PLTDocument(
            header_commands=[], stroke_paths=[valid_path, empty_path], footer_commands=[]
        )

        result = profiler.profile(doc)

        # Should only count the valid path
        assert result.total_strokes == 1

    def test_profile_empty_segments_in_path(self) -> None:
        """Test that paths with no segments are handled (lines 135-136)."""
        profiler = Profiler()

        # Create a document where all paths have empty segments list
        # This triggers the total_paths=0 branch for structural_ratio calculation
        doc = PLTDocument(
            header_commands=[], stroke_paths=[], footer_commands=[]
        )

        with pytest.raises(ProfilerError):
            profiler.profile(doc)

    def test_calculate_average_segment_length_with_zero_segments(self) -> None:
        """Test _calculate_average_segment_length returns 0 for empty path."""
        profiler = Profiler()

        path = StrokePath(pen_up_position=None, segments=())
        result = profiler._calculate_average_segment_length(path)
        assert result == 0.0

    def test_calculate_bounding_box_extent_with_zero_segments(self) -> None:
        """Test _calculate_bounding_box_extent returns 0 for empty path."""
        profiler = Profiler()

        path = StrokePath(pen_up_position=None, segments=())
        result = profiler._calculate_bounding_box_extent(path)
        assert result == 0.0

    def test_closed_loop_with_zero_bbox_extent_not_structural(self) -> None:
        """Test closed loop detection when bbox extent is 0 (line ~157 branch)."""
        profiler = Profiler()

        # Create a path where first and last segments close but have zero extent
        # This tests the avg_segment_length > 0 / bbox_extent > 0 branches
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0), end=Coordinate(x=100.0, y=0.0), is_cutting=True
        )
        # End matches start of first - but zero height
        path = StrokePath(pen_up_position=None, segments=(seg1,))

        result = profiler._is_structural_path(path)
        assert result is True  # Single segment is always structural

    def test_closed_loop_with_small_segment_ratio_not_structural(self) -> None:
        """Test closed loop with small segment-length-to-extent ratio."""
        profiler = Profiler()

        # Create a rectangle but each side has multiple tiny segments
        # This reduces avg segment length relative to bounding box extent
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0), end=Coordinate(x=10.0, y=0.0), is_cutting=True
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=10.0, y=0.0), end=Coordinate(x=20.0, y=0.0), is_cutting=True
        )
        seg3 = StrokeSegment(
            start=Coordinate(x=20.0, y=0.0), end=Coordinate(x=30.0, y=0.0), is_cutting=True
        )
        # ... many tiny segments to reduce avg segment length

        path = StrokePath(pen_up_position=None, segments=(seg1, seg2, seg3))

        result = profiler._is_structural_path(path)
        # Should not be structural because average segment length relative to bbox is small


class TestStructuralPathBranches:
    """Test specific branches in _is_structural_path for coverage."""

    def test_drill_hole_with_non_zero_lines_not_structural(self) -> None:
        """Test EngraveLab drill hole check with non-zero lines (not structural)."""
        profiler = Profiler()

        # 4 arcs of 90 degrees but WITH non-zero line segments - should not be structural
        from plt_optimizer.core.models import ArcSegment

        arc1 = ArcSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=0.0),
            center=Coordinate(x=5.0, y=0.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        arc2 = ArcSegment(
            start=Coordinate(x=10.0, y=0.0),
            end=Coordinate(x=10.0, y=10.0),
            center=Coordinate(x=10.0, y=5.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        arc3 = ArcSegment(
            start=Coordinate(x=10.0, y=10.0),
            end=Coordinate(x=0.0, y=10.0),
            center=Coordinate(x=5.0, y=10.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        arc4 = ArcSegment(
            start=Coordinate(x=0.0, y=10.0),
            end=Coordinate(x=0.0, y=0.0),
            center=Coordinate(x=0.0, y=5.0),
            sweep_angle=-90.0,
            is_cutting=True,
        )
        # Add a non-zero length line (not a plunge point)
        line = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0), end=Coordinate(x=1.0, y=0.0), is_cutting=True
        )

        path = StrokePath(pen_up_position=None, segments=(arc1, arc2, arc3, arc4, line))

        result = profiler._is_structural_path(path)
        assert result is False  # Should not be classified as drill hole due to non-zero line

    def test_drill_hole_with_180_degree_arcs_not_structural(self) -> None:
        """Test drill hole detection with wrong arc sweep angles."""
        from plt_optimizer.core.models import ArcSegment

        profiler = Profiler()

        # 4 arcs but NOT 90 degrees each - should not be structural
        arc1 = ArcSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=0.0),
            center=Coordinate(x=5.0, y=0.0),
            sweep_angle=180.0,  # Wrong angle
            is_cutting=True,
        )
        arc2 = ArcSegment(
            start=Coordinate(x=10.0, y=0.0),
            end=Coordinate(x=10.0, y=10.0),
            center=Coordinate(x=10.0, y=5.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        arc3 = ArcSegment(
            start=Coordinate(x=10.0, y=10.0),
            end=Coordinate(x=0.0, y=10.0),
            center=Coordinate(x=5.0, y=10.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        arc4 = ArcSegment(
            start=Coordinate(x=0.0, y=10.0),
            end=Coordinate(x=0.0, y=0.0),
            center=Coordinate(x=0.0, y=5.0),
            sweep_angle=-90.0,
            is_cutting=True,
        )

        path = StrokePath(pen_up_position=None, segments=(arc1, arc2, arc3, arc4))

        result = profiler._is_structural_path(path)
        assert result is False  # Should not be drill hole

    def test_closed_loop_non_stroke_segment_not_checked(self) -> None:
        """Test closed loop check with non-StrokeSegment endpoints."""
        from plt_optimizer.core.models import ArcSegment

        profiler = Profiler()

        # First segment is Arc, so closed loop branch won't trigger
        arc1 = ArcSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=50.0),
            center=Coordinate(x=50.0, y=25.0),
            sweep_angle=45.0,
            is_cutting=True,
        )
        arc2 = ArcSegment(
            start=Coordinate(x=100.0, y=50.0),
            end=Coordinate(x=0.0, y=0.0),  # Closes back
            center=Coordinate(x=50.0, y=25.0),
            sweep_angle=-45.0,
            is_cutting=True,
        )

        path = StrokePath(pen_up_position=None, segments=(arc1, arc2))

        result = profiler._is_structural_path(path)
        # Should return False because Check 3 requires both first/last to be StrokeSegment
        assert result is False

    def test_linear_path_with_high_segment_ratio_is_structural(self) -> None:
        """Test that pure linear path with high segment/extent ratio is structural."""
        profiler = Profiler()

        # Single long line - high segment length relative to bbox extent
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=50.0), end=Coordinate(x=100.0, y=50.0), is_cutting=True
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=100.0, y=50.0), end=Coordinate(x=200.0, y=50.0), is_cutting=True
        )

        path = StrokePath(pen_up_position=None, segments=(seg1, seg2))

        # avg_segment_length = 100, bbox_extent = 200 (dx=200, dy=0)
        # ratio = 100/200 = 0.5 >= 0.25 -> structural
        result = profiler._is_structural_path(path)
        assert result is True

    def test_linear_path_with_low_segment_ratio_not_structural(self) -> None:
        """Test that linear path with low segment/extent ratio is not structural."""
        profiler = Profiler()

        # Many tiny segments in a line - low avg length relative to bbox
        segments = []
        x = 0.0
        for i in range(10):
            seg = StrokeSegment(
                start=Coordinate(x=x, y=50.0), end=Coordinate(x=x + 2.0, y=50.0), is_cutting=True
            )
            segments.append(seg)
            x += 2.0

        path = StrokePath(pen_up_position=None, segments=tuple(segments))

        # avg_segment_length = 2, bbox_extent = 20 (dx=20, dy=0)
        # ratio = 2/20 = 0.1 < 0.25 -> not structural
        result = profiler._is_structural_path(path)
        assert result is False

    def test_closed_loop_check_zero_avg_length(self) -> None:
        """Test closed loop branch when avg_segment_length <= 0."""
        profiler = Profiler()

        # Create a path where first and last match but segments have zero total length
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0), end=Coordinate(x=100.0, y=50.0), is_cutting=True
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=100.0, y=50.0),
            end=Coordinate(x=0.0, y=0.0),  # Closes back
            is_cutting=True,
        )

        path = StrokePath(pen_up_position=None, segments=(seg1, seg2))

        result = profiler._is_structural_path(path)
        # avg_segment_length > 0 so it might be structural depending on ratio

    def test_linear_check_zero_avg_length(self) -> None:
        """Test pure linear check when avg_segment_length <= 0."""
        profiler = Profiler()

        path = StrokePath(pen_up_position=None, segments=())
        result = profiler._is_structural_path(path)
        assert result is False


class TestClosedLoopBranches:
    """Test branches in closed loop detection for coverage."""

    def test_closed_loop_small_segment_ratio_not_structural(self) -> None:
        """Test closed loop with length_to_extent_ratio < 0.15 (lines 257->267)."""
        profiler = Profiler()

        # Create a closed rectangle but with many tiny segments
        # so avg segment length is small relative to bbox extent
        segments = []
        x, y = 0.0, 0.0
        # Bottom edge: 5 segments of 20 units each (total 100)
        for i in range(5):
            segments.append(
                StrokeSegment(
                    start=Coordinate(x=x, y=y),
                    end=Coordinate(x=x + 20.0, y=y),
                    is_cutting=True,
                )
            )
            x += 20.0
        # Right edge: going up (many tiny segments)
        for i in range(5):
            segments.append(
                StrokeSegment(
                    start=Coordinate(x=x, y=y),
                    end=Coordinate(x=x, y=y + 10.0),
                    is_cutting=True,
                )
            )
            y += 10.0
        # Top edge: going left (many tiny segments)
        for i in range(5):
            segments.append(
                StrokeSegment(
                    start=Coordinate(x=x, y=y),
                    end=Coordinate(x=x - 20.0, y=y),
                    is_cutting=True,
                )
            )
            x -= 20.0
        # Left edge: going down (many tiny segments)
        for i in range(5):
            segments.append(
                StrokeSegment(
                    start=Coordinate(x=x, y=y),
                    end=Coordinate(x=x, y=y - 10.0),
                    is_cutting=True,
                )
            )
            y -= 10.0

        path = StrokePath(pen_up_position=None, segments=tuple(segments))

        # bbox_extent should be max(100, 50) = 100
        # avg_segment_length ≈ (20+10+20+10)/4 = 15
        # ratio = 15/100 = 0.15 - this is borderline at exactly 0.15

        result = profiler._is_structural_path(path)
        # Should be False because ratio is not >= 0.15 for closed loop check


class TestPureLinearWithArcs:
    """Test pure linear path Check 5 when arcs AND lines present."""

    def test_mixed_arcs_and_lines_not_pure_linear(self) -> None:
        """Test that paths with both arcs and lines don't trigger Check 5 (lines 270->279)."""
        from plt_optimizer.core.models import ArcSegment

        profiler = Profiler()

        # Path has BOTH arcs and lines - should not match Check 5
        line1 = StrokeSegment(
            start=Coordinate(x=0.0, y=50.0), end=Coordinate(x=100.0, y=50.0), is_cutting=True
        )
        arc1 = ArcSegment(
            start=Coordinate(x=100.0, y=50.0),
            end=Coordinate(x=200.0, y=50.0),
            center=Coordinate(x=150.0, y=50.0),
            sweep_angle=180.0,
            is_cutting=True,
        )
        line2 = StrokeSegment(
            start=Coordinate(x=200.0, y=50.0), end=Coordinate(x=300.0, y=50.0), is_cutting=True
        )

        path = StrokePath(pen_up_position=None, segments=(line1, arc1, line2))

        # Check 5 only triggers if `not arcs and lines` (no arcs present)
        result = profiler._is_structural_path(path)
        assert result is False

    def test_pure_linear_low_ratio_check_5(self) -> None:
        """Test pure linear path with low segment/extent ratio that fails Check 5."""
        profiler = Profiler()

        # Create multiple long segments so NOT caught by Check 1
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=50.0), end=Coordinate(x=40.0, y=50.0), is_cutting=True
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=40.0, y=50.0), end=Coordinate(x=80.0, y=50.0), is_cutting=True
        )
        # Total bounding box extent: dx=80, dy=0 -> max=80
        # Average segment length = 40
        # Ratio = 40/80 = 0.5 >= 0.25 - would be structural!

        path = StrokePath(pen_up_position=None, segments=(seg1, seg2))

        result = profiler._is_structural_path(path)
        # With ratio 0.5 > 0.25, this IS structural via Check 5

    def test_pure_linear_check_5_exactly_at_threshold(self) -> None:
        """Test pure linear path where ratio is exactly at 0.25 threshold."""
        profiler = Profiler()

        # Need avg_segment_length / bbox_extent == 0.25
        # Let dx = 100, dy = 0 (bbox_extent = 100)
        # Let segment length = 25 each -> avg 25/1 = 25
        # ratio = 25/100 = 0.25

        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=50.0), end=Coordinate(x=25.0, y=50.0), is_cutting=True
        )

        path = StrokePath(pen_up_position=None, segments=(seg1,))

        # Single segment - caught by Check 1 first!

    def test_closed_loop_rectangle_multiple_segments(self) -> None:
        """Test closed loop rectangle made of multiple equal segments (not single long ones).

        This tests the avg_segment_length check when bbox_extent > 0 and ratio >= 0.15.
        """
        profiler = Profiler()

        # Rectangle with 4 sides, each side split into 2 segments
        # So we have 8 segments total for a 100x50 rectangle
        seg1 = StrokeSegment(start=Coordinate(x=0.0, y=0.0), end=Coordinate(x=50.0, y=0.0), is_cutting=True)
        seg2 = StrokeSegment(start=Coordinate(x=50.0, y=0.0), end=Coordinate(x=100.0, y=0.0), is_cutting=True)
        seg3 = StrokeSegment(start=Coordinate(x=100.0, y=0.0), end=Coordinate(x=100.0, y=25.0), is_cutting=True)
        seg4 = StrokeSegment(start=Coordinate(x=100.0, y=25.0), end=Coordinate(x=100.0, y=50.0), is_cutting=True)
        seg5 = StrokeSegment(start=Coordinate(x=100.0, y=50.0), end=Coordinate(x=50.0, y=50.0), is_cutting=True)
        seg6 = StrokeSegment(start=Coordinate(x=50.0, y=50.0), end=Coordinate(x=0.0, y=50.0), is_cutting=True)
        seg7 = StrokeSegment(start=Coordinate(x=0.0, y=50.0), end=Coordinate(x=0.0, y=25.0), is_cutting=True)
        seg8 = StrokeSegment(start=Coordinate(x=0.0, y=25.0), end=Coordinate(x=0.0, y=0.0), is_cutting=True)

        path = StrokePath(pen_up_position=None, segments=(seg1, seg2, seg3, seg4, seg5, seg6, seg7, seg8))

        # bbox_extent: max(100, 50) = 100
        # Total length: 8 * 50 = 400
        # avg_segment_length = 50
        # ratio = 50/100 = 0.5 >= 0.15 -> structural

        result = profiler._is_structural_path(path)
        assert result is True


class TestTotalPathsZero:
    """Test when total_paths calculation results in zero."""

    def test_profile_all_empty_paths(self) -> None:
        """Test profiling document where all paths have empty segments (lines 135-136)."""
        profiler = Profiler()

        # All paths have no segments - valid_paths will be empty
        path1 = StrokePath(pen_up_position=None, segments=())
        path2 = StrokePath(pen_up_position=None, segments=())

        doc = PLTDocument(
            header_commands=[], stroke_paths=[path1, path2], footer_commands=[]
        )

        # This should raise error because no cutting strokes found
        with pytest.raises(ProfilerError):
            profiler.profile(doc)

    def test_profile_single_path_no_segments(self) -> None:
        """Test profiling single empty path."""
        profiler = Profiler()

        doc = PLTDocument(
            header_commands=[],
            stroke_paths=[StrokePath(pen_up_position=None, segments=())],
            footer_commands=[],
        )

        with pytest.raises(ProfilerError):
            profiler.profile(doc)

    def test_profile_only_non_cutting_strokes(self) -> None:
        """Test lines 135-136: total_paths==0 case with non-cutting strokes.

        This tests the else branch when all paths have segments but none are cutting.
        Lines 135-136 set structural_path_count=0, structural_ratio=0.0
        Then line 157 raises ProfilerError because no cutting strokes found.
        """
        profiler = Profiler()

        # All non-cutting (pen-up) movements - should trigger lines 135-136 before error
        path1 = StrokePath(
            pen_up_position=None,
            segments=(
                StrokeSegment(
                    start=Coordinate(x=0.0, y=0.0),
                    end=Coordinate(x=100.0, y=50.0),
                    is_cutting=False,  # NOT cutting - skipped in _calculate_all_extents
                ),
            ),
        )
        path2 = StrokePath(
            pen_up_position=None,
            segments=(
                StrokeSegment(
                    start=Coordinate(x=200.0, y=100.0),
                    end=Coordinate(x=300.0, y=150.0),
                    is_cutting=False,  # NOT cutting
                ),
            ),
        )

        doc = PLTDocument(header_commands=[], stroke_paths=[path1, path2], footer_commands=[])

        with pytest.raises(ProfilerError) as exc_info:
            profiler.profile(doc)
        assert "No cutting strokes found" in str(exc_info.value.message)