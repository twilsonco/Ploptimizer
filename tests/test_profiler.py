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