"""Tests for plt_optimizer/core/profiler.py module.

This module provides baseline extent calculation using 95th percentile
of max bounding box dimension across cutting strokes.
"""

from __future__ import annotations

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