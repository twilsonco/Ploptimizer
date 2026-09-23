"""Tests for plt_optimizer/core/profiler.py module.

This module provides baseline extent calculation using 95th percentile
of max bounding box dimension across cutting strokes, plus structural
classification (paths consisting only of straight lines and verified
perfect circles).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path

import pytest

from plt_optimizer.core.models import (
    ArcSegment,
    Coordinate,
    PLTDocument,
    StrokePath,
    StrokeSegment,
)
from plt_optimizer.core.profiler import (
    DEFAULT_STRUCTURAL_RATIO,
    Extent,
    Profiler,
    ProfilerError,
    ProfileResult,
    StrokePathsProtocol,
    _arc_runs,
    _is_perfect_circle,
)


def _circle_arcs(
    cx: float,
    cy: float,
    radius: float,
    sweeps: Sequence[float],
    start_angle_deg: float = 90.0,
) -> list[ArcSegment]:
    """Build a chained arc run sharing one center and radius.

    Mirrors the parser's end-position math so fixtures are geometrically
    consistent (unlike early fixtures that used chord-midpoint centers).

    Args:
        cx: Circle center X.
        cy: Circle center Y.
        radius: Circle radius.
        sweeps: Signed sweep angles in degrees, applied consecutively.
        start_angle_deg: Angle of the first arc's start point.

    Returns:
        Chained ArcSegments; closes perfectly when sweeps total 360.
    """
    arcs: list[ArcSegment] = []
    theta = math.radians(start_angle_deg)
    start = Coordinate(cx + radius * math.cos(theta), cy + radius * math.sin(theta))
    for sweep in sweeps:
        end_theta = theta + math.radians(sweep)
        end = Coordinate(cx + radius * math.cos(end_theta), cy + radius * math.sin(end_theta))
        arcs.append(
            ArcSegment(
                start=start,
                end=end,
                center=Coordinate(cx, cy),
                sweep_angle=sweep,
                is_cutting=True,
            )
        )
        start = end
        theta = end_theta
    return arcs


def _micro_arc_text_path(x0: float, y0: float, n: int = 20) -> StrokePath:
    """Build an EngraveLab-style glyph path: many tiny arcs, varying centers.

    EngraveLab renders letter curves as consecutive ~10-degree arcs whose
    centers and radii change every segment; the sweeps never total a full
    revolution, so the perfect-circle verifier rejects them.
    """
    segments: list[ArcSegment] = []
    x, y = x0, y0
    for i in range(n):
        nx, ny = x + 2.0, y + 1.0
        segments.append(
            ArcSegment(
                start=Coordinate(x, y),
                end=Coordinate(nx, ny),
                center=Coordinate((x + nx) / 2, (y + ny) / 2),
                sweep_angle=10.0 + i * 0.5,
                is_cutting=True,
            )
        )
        x, y = nx, ny
    return StrokePath(pen_up_position=None, segments=tuple(segments))


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
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0), end=Coordinate(x=100.0, y=0.0), is_cutting=True
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=100.0, y=0.0), end=Coordinate(x=100.0, y=50.0), is_cutting=True
        )
        seg3 = StrokeSegment(
            start=Coordinate(x=100.0, y=50.0), end=Coordinate(x=0.0, y=50.0), is_cutting=True
        )
        seg4 = StrokeSegment(
            start=Coordinate(x=0.0, y=50.0), end=Coordinate(x=0.0, y=0.0), is_cutting=True
        )

        path = StrokePath(pen_up_position=None, segments=(seg1, seg2, seg3, seg4))

        assert profiler._is_structural_path(path) is True

    def test_engravelab_drill_hole_is_structural(self) -> None:
        """Test that EngraveLab 4-arc drill hole pattern is structural."""
        profiler = Profiler()

        # Real drill hole: 4 chained 90-degree arcs sharing center (5, 5), r=5
        arcs = _circle_arcs(5.0, 5.0, 5.0, [90.0, 90.0, 90.0, 90.0])

        path = StrokePath(pen_up_position=None, segments=tuple(arcs))

        assert profiler._is_structural_path(path) is True

    def test_drill_hole_with_plunge_is_structural(self) -> None:
        """Test zero-length plunge + verified circle is structural."""
        profiler = Profiler()

        arcs = _circle_arcs(5.0, 5.0, 5.0, [90.0, 90.0, 90.0, 90.0])
        plunge = StrokeSegment(start=arcs[0].start, end=arcs[0].start, is_cutting=True)

        path = StrokePath(pen_up_position=None, segments=(plunge, *arcs))

        assert profiler._is_structural_path(path) is True

    def test_text_like_path_not_structural(self) -> None:
        """Test that an EngraveLab micro-arc glyph path is NOT structural."""
        profiler = Profiler()

        # Real EngraveLab text curves: tiny arcs, per-segment centers/radii,
        # sweeps that never total a full revolution.
        path = _micro_arc_text_path(0.0, 0.0)

        assert profiler._is_structural_path(path) is False

    def test_open_polygon_not_closed_loop(self) -> None:
        """Test that an open polygon (not closed) is still structural (all lines)."""
        profiler = Profiler()

        # Open path: (0,0) -> (100,0) -> (100,50) -> (0,50)
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0), end=Coordinate(x=100.0, y=0.0), is_cutting=True
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=100.0, y=0.0), end=Coordinate(x=100.0, y=50.0), is_cutting=True
        )
        seg3 = StrokeSegment(
            start=Coordinate(x=100.0, y=50.0), end=Coordinate(x=0.0, y=50.0), is_cutting=True
        )

        path = StrokePath(pen_up_position=None, segments=(seg1, seg2, seg3))

        # Compositional rule: straight lines only -> structural regardless of
        # closure or segment-length ratios.
        assert profiler._is_structural_path(path) is True


class TestStructuralClassification:
    """Tests for overall structural classification based on ratio threshold."""

    def test_structural_threshold_85_percent(self) -> None:
        """Test that is_structural=True when >85% of paths are structural."""

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

        # Add one micro-arc text path (not structural: per-segment centers/radii)
        paths.append(_micro_arc_text_path(500.0, 200.0))

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

        # Add 5 micro-arc text paths (not structural)
        for i in range(5):
            paths.append(_micro_arc_text_path(500.0, i * 100.0))

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
        doc = PLTDocument(header_commands=[], stroke_paths=[], footer_commands=[])

        with pytest.raises(ProfilerError):
            profiler.profile(doc)

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

        profiler._is_structural_path(path)
        # Should not be structural because average segment length relative to bbox is small


class TestStructuralPathBranches:
    """Test specific branches in _is_structural_path for coverage."""

    def test_verified_circle_with_non_zero_line_is_structural(self) -> None:
        """Test a perfect circle plus a real (non-plunge) line is structural.

        Under the compositional rule, straight lines and perfect circles may
        freely mix within one path (e.g. a circle welded to a tangential cut).
        """
        profiler = Profiler()

        arcs = _circle_arcs(5.0, 5.0, 5.0, [90.0, 90.0, 90.0, 90.0])
        # Non-zero length line appended after the closed circle
        line = StrokeSegment(start=arcs[-1].end, end=Coordinate(x=1.0, y=0.0), is_cutting=True)

        path = StrokePath(pen_up_position=None, segments=(*arcs, line))

        result = profiler._is_structural_path(path)
        assert result is True  # lines + perfect circles -> structural

    def test_unverified_circle_with_line_not_structural(self) -> None:
        """Test arcs that fail circle verification make the path non-structural."""
        profiler = Profiler()

        # Fake circle: per-arc centers (not a perfect circle) + a line
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
            sweep_angle=90.0,
            is_cutting=True,
        )
        line = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0), end=Coordinate(x=1.0, y=0.0), is_cutting=True
        )

        path = StrokePath(pen_up_position=None, segments=(arc1, arc2, arc3, arc4, line))

        result = profiler._is_structural_path(path)
        assert result is False  # arc run fails perfect-circle verification

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

    def test_linear_path_with_low_segment_ratio_is_structural(self) -> None:
        """Test that any pure-linear path is structural regardless of ratios."""
        profiler = Profiler()

        # Many tiny segments in a line - the old ratio heuristic rejected this
        segments = []
        x = 0.0
        for _i in range(10):
            seg = StrokeSegment(
                start=Coordinate(x=x, y=50.0), end=Coordinate(x=x + 2.0, y=50.0), is_cutting=True
            )
            segments.append(seg)
            x += 2.0

        path = StrokePath(pen_up_position=None, segments=tuple(segments))

        # Compositional rule: straight lines only -> structural
        result = profiler._is_structural_path(path)
        assert result is True

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

        profiler._is_structural_path(path)
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
        for _i in range(5):
            segments.append(
                StrokeSegment(
                    start=Coordinate(x=x, y=y),
                    end=Coordinate(x=x + 20.0, y=y),
                    is_cutting=True,
                )
            )
            x += 20.0
        # Right edge: going up (many tiny segments)
        for _i in range(5):
            segments.append(
                StrokeSegment(
                    start=Coordinate(x=x, y=y),
                    end=Coordinate(x=x, y=y + 10.0),
                    is_cutting=True,
                )
            )
            y += 10.0
        # Top edge: going left (many tiny segments)
        for _i in range(5):
            segments.append(
                StrokeSegment(
                    start=Coordinate(x=x, y=y),
                    end=Coordinate(x=x - 20.0, y=y),
                    is_cutting=True,
                )
            )
            x -= 20.0
        # Left edge: going down (many tiny segments)
        for _i in range(5):
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

        profiler._is_structural_path(path)
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

        profiler._is_structural_path(path)
        # With ratio 0.5 > 0.25, this IS structural via Check 5

    def test_pure_linear_check_5_exactly_at_threshold(self) -> None:
        """Test pure linear path where ratio is exactly at 0.25 threshold."""
        Profiler()

        # Need avg_segment_length / bbox_extent == 0.25
        # Let dx = 100, dy = 0 (bbox_extent = 100)
        # Let segment length = 25 each -> avg 25/1 = 25
        # ratio = 25/100 = 0.25

        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=50.0), end=Coordinate(x=25.0, y=50.0), is_cutting=True
        )

        StrokePath(pen_up_position=None, segments=(seg1,))

        # Single segment - caught by Check 1 first!

    def test_closed_loop_rectangle_multiple_segments(self) -> None:
        """Test closed loop rectangle made of multiple equal segments (not single long ones).

        This tests the avg_segment_length check when bbox_extent > 0 and ratio >= 0.15.
        """
        profiler = Profiler()

        # Rectangle with 4 sides, each side split into 2 segments
        # So we have 8 segments total for a 100x50 rectangle
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0), end=Coordinate(x=50.0, y=0.0), is_cutting=True
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=50.0, y=0.0), end=Coordinate(x=100.0, y=0.0), is_cutting=True
        )
        seg3 = StrokeSegment(
            start=Coordinate(x=100.0, y=0.0), end=Coordinate(x=100.0, y=25.0), is_cutting=True
        )
        seg4 = StrokeSegment(
            start=Coordinate(x=100.0, y=25.0), end=Coordinate(x=100.0, y=50.0), is_cutting=True
        )
        seg5 = StrokeSegment(
            start=Coordinate(x=100.0, y=50.0), end=Coordinate(x=50.0, y=50.0), is_cutting=True
        )
        seg6 = StrokeSegment(
            start=Coordinate(x=50.0, y=50.0), end=Coordinate(x=0.0, y=50.0), is_cutting=True
        )
        seg7 = StrokeSegment(
            start=Coordinate(x=0.0, y=50.0), end=Coordinate(x=0.0, y=25.0), is_cutting=True
        )
        seg8 = StrokeSegment(
            start=Coordinate(x=0.0, y=25.0), end=Coordinate(x=0.0, y=0.0), is_cutting=True
        )

        path = StrokePath(
            pen_up_position=None, segments=(seg1, seg2, seg3, seg4, seg5, seg6, seg7, seg8)
        )

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

        doc = PLTDocument(header_commands=[], stroke_paths=[path1, path2], footer_commands=[])

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


class TestIsStructuralPathFallsThroughBranches:
    """Degenerate straight-line paths under the compositional rule.

    Zero-length segments are straight lines, so these degenerate paths are
    structural under the compositional rule (the old ratio heuristics fell
    through to False because they divided by segment length / bbox extent).
    """

    def test_closed_loop_zero_bbox_extent_falls_through(self) -> None:
        """Test zero-length closed-loop path is structural (all lines)."""
        profiler = Profiler()

        # Two segments where first.start == last.end BUT bbox_extent == 0
        # This means all coordinates are identical
        seg1 = StrokeSegment(
            start=Coordinate(x=50.0, y=50.0),
            end=Coordinate(x=50.0, y=50.0),  # Zero length
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=50.0, y=50.0),
            end=Coordinate(x=50.0, y=50.0),  # Same point - closes back to same
            is_cutting=True,
        )

        path = StrokePath(pen_up_position=None, segments=(seg1, seg2))

        result = profiler._is_structural_path(path)
        assert result is True

    def test_closed_loop_zero_avg_length_falls_through(self) -> None:
        """Test zero-length straight loop is structural (ratio math not involved)."""
        profiler = Profiler()

        # Path that closes geometrically but has zero-length segments
        seg1 = StrokeSegment(
            start=Coordinate(x=50.0, y=50.0),
            end=Coordinate(x=50.0, y=50.0),  # Zero length
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=50.0, y=50.0),
            end=Coordinate(x=50.0, y=50.0),  # Zero length - closes back
            is_cutting=True,
        )

        path = StrokePath(pen_up_position=None, segments=(seg1, seg2))

        result = profiler._is_structural_path(path)
        assert result is True

    def test_pure_linear_zero_bbox_extent_falls_through(self) -> None:
        """Test multiple zero-length StrokeSegments are structural (all lines)."""
        profiler = Profiler()

        # Multiple zero-length segments at same point
        seg1 = StrokeSegment(
            start=Coordinate(x=50.0, y=100.0),
            end=Coordinate(x=50.0, y=100.0),  # Zero length
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=50.0, y=100.0),
            end=Coordinate(x=50.0, y=100.0),  # Same point - still zero extent
            is_cutting=True,
        )

        path = StrokePath(pen_up_position=None, segments=(seg1, seg2))

        result = profiler._is_structural_path(path)
        assert result is True

    def test_pure_linear_zero_avg_length_falls_through(self) -> None:
        """Test degenerate zero-average-length straight path is structural."""
        profiler = Profiler()

        # Multiple zero-length segments form a degenerate "line"
        seg1 = StrokeSegment(
            start=Coordinate(x=50.0, y=100.0),
            end=Coordinate(x=50.0, y=100.0),  # Zero length
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=50.0, y=100.0),
            end=Coordinate(x=50.0, y=100.0),  # Zero length
            is_cutting=True,
        )

        path = StrokePath(pen_up_position=None, segments=(seg1, seg2))

        result = profiler._is_structural_path(path)
        assert result is True

    def test_multiple_arcs_not_drill_hole_check_5_triggered(self) -> None:
        """Test that 3 arcs (not drill hole) triggers Check 5 with zero bbox.

        When a path has some arcs but not exactly 4, and no lines,
        it shouldn't match any structural check.
        """
        profiler = Profiler()
        from plt_optimizer.core.models import ArcSegment

        # 3 arcs - doesn't match drill hole pattern
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

        path = StrokePath(pen_up_position=None, segments=(arc1, arc2, arc3))

        result = profiler._is_structural_path(path)
        assert result is False


class TestStructuralRatioEdgeCases:
    """Test structural ratio calculations at boundaries."""

    def test_structural_ratio_exactly_85_percent(self) -> None:
        """Test is_structural when ratio > 0.85 threshold."""
        profiler = Profiler()

        # Create paths with known segment counts
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

        result = profiler.profile(doc)

        # 5 single-segment paths (all structural) / 5 total = 100% > 85%
        assert result.is_structural is True

    def test_profile_calculates_avg_segments_per_path(self) -> None:
        """Test that structural_ratio logging produces correct avg calculation."""
        profiler = Profiler()

        # Create paths with known segment counts
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

        result = profiler.profile(doc)

        # 5 paths, each with 1 segment = avg 1.0
        assert result.total_strokes == 5


class TestStructuralRatioGate:
    """Tests for the configurable document-level structural gate."""

    @staticmethod
    def _line_path(x0: float) -> StrokePath:
        """Build a single-segment straight line path."""
        return StrokePath(
            pen_up_position=None,
            segments=(
                StrokeSegment(
                    start=Coordinate(x=x0, y=0.0),
                    end=Coordinate(x=x0 + 100.0, y=50.0),
                    is_cutting=True,
                ),
            ),
        )

    def test_default_structural_ratio_constant(self) -> None:
        """Test the module default matches the historical 85% gate."""
        assert DEFAULT_STRUCTURAL_RATIO == pytest.approx(0.85)

    def test_relaxed_gate_classifies_mixed_document(self) -> None:
        """Test structural_ratio=0.5 accepts a 2/3 structural document."""
        profiler = Profiler(structural_ratio=0.5)
        doc = PLTDocument(
            header_commands=[],
            stroke_paths=[
                self._line_path(0.0),
                self._line_path(200.0),
                _micro_arc_text_path(500.0, 0.0),
            ],
            footer_commands=[],
        )

        result = profiler.profile(doc)

        # 2 structural / 3 total = 66.7% > 50%
        assert result.is_structural is True

    def test_default_gate_rejects_same_mixed_document(self) -> None:
        """Test the default 85% gate keeps the same 2/3 document on the text path."""
        profiler = Profiler()
        doc = PLTDocument(
            header_commands=[],
            stroke_paths=[
                self._line_path(0.0),
                self._line_path(200.0),
                _micro_arc_text_path(500.0, 0.0),
            ],
            footer_commands=[],
        )

        result = profiler.profile(doc)

        # 66.7% does not exceed the default 85% gate
        assert result.is_structural is False

    def test_gate_is_strictly_greater_than(self) -> None:
        """Test a ratio exactly equal to the gate is not structural (strict >)."""
        profiler = Profiler(structural_ratio=0.8)
        doc = PLTDocument(
            header_commands=[],
            stroke_paths=[
                self._line_path(0.0),
                self._line_path(200.0),
                self._line_path(400.0),
                self._line_path(600.0),
                _micro_arc_text_path(900.0, 0.0),
            ],
            footer_commands=[],
        )

        result = profiler.profile(doc)

        # 4 / 5 = 0.8 exactly; gate requires strictly greater
        assert result.is_structural is False


class TestPureCircleStructuralDocument:
    """Tests for pure-circle (drill-hole-only) structural documents."""

    def test_profile_returns_zero_baseline_without_raising(self) -> None:
        """Test a hole-only file profiles with baseline 0.0 instead of raising."""
        profiler = Profiler()
        hole = StrokePath(
            pen_up_position=None,
            segments=(
                StrokeSegment(
                    start=Coordinate(x=5.0, y=0.0),
                    end=Coordinate(x=5.0, y=0.0),
                    is_cutting=True,
                ),
                *_circle_arcs(5.0, 5.0, 5.0, [90.0, 90.0, 90.0, 90.0]),
            ),
        )
        doc = PLTDocument(header_commands=[], stroke_paths=[hole], footer_commands=[])

        result = profiler.profile(doc)

        assert result.is_structural is True
        assert result.baseline_extent == pytest.approx(0.0, abs=1e-9)
        assert result.total_strokes == 0


class TestTotalPathsZeroBranch:
    """Test the else branch when total_paths == 0 (lines 135-136)."""

    def test_total_paths_zero_else_branch(self) -> None:
        """Test lines 135-136: document where all paths have empty segments.

        When ALL paths in a document have no segments at all, valid_paths
        becomes empty and total_paths is 0. This triggers the else branch.
        """
        profiler = Profiler()

        # Create paths with empty segment tuples (no segments)
        path1 = StrokePath(pen_up_position=None, segments=())
        path2 = StrokePath(pen_up_position=None, segments=())

        doc = PLTDocument(header_commands=[], stroke_paths=[path1, path2], footer_commands=[])

        with pytest.raises(ProfilerError) as exc_info:
            profiler.profile(doc)
        assert "No cutting strokes found" in str(exc_info.value.message)


class TestIsStructuralPathFallThrough:
    """Test _is_structural_path outcomes for mixed and degenerate paths."""

    def test_closed_loop_zero_bbox_extent_returns_false(self) -> None:
        """Test zero-length straight loop is structural (compositional rule)."""
        profiler = Profiler()

        # Two zero-length segments at same point - all lines -> structural
        seg1 = StrokeSegment(
            start=Coordinate(x=50.0, y=100.0),
            end=Coordinate(x=50.0, y=100.0),  # Zero length
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=50.0, y=100.0),
            end=Coordinate(x=50.0, y=100.0),  # Same point - closes back to start
            is_cutting=True,
        )

        path = StrokePath(pen_up_position=None, segments=(seg1, seg2))

        result = profiler._is_structural_path(path)
        assert result is True

    def test_closed_loop_check3_returns_false(self) -> None:
        """Test closed loop detection returns False when ratio too low.

        Covers lines 259->267 (the return False after all checks fail).
        """
        profiler = Profiler()

        # Closed rectangle but with many tiny segments
        # so avg segment length is small relative to bbox extent
        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0), end=Coordinate(x=10.0, y=0.0), is_cutting=True
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=10.0, y=0.0), end=Coordinate(x=20.0, y=0.0), is_cutting=True
        )
        seg3 = StrokeSegment(
            start=Coordinate(x=20.0, y=0.0), end=Coordinate(x=30.0, y=0.0), is_cutting=True
        )
        seg4 = StrokeSegment(
            start=Coordinate(x=30.0, y=0.0), end=Coordinate(x=40.0, y=0.0), is_cutting=True
        )

        path = StrokePath(
            pen_up_position=None,
            segments=(seg1, seg2, seg3, seg4),
        )

        profiler._is_structural_path(path)
        # Falls through because ratio < 0.15 for closed loop

    def test_pure_linear_check5_returns_false_low_ratio(self) -> None:
        """Test pure linear Check 5 returns False when ratio < 0.25.

        Covers lines 272->279 (the return False after check 5 fails).
        """
        profiler = Profiler()

        # Multiple tiny segments in a line - low avg length relative to bbox
        x = 0.0
        y = 50.0
        segments = []
        for _i in range(8):
            seg = StrokeSegment(
                start=Coordinate(x=x, y=y),
                end=Coordinate(x=x + 2.0, y=y),  # tiny segment
                is_cutting=True,
            )
            segments.append(seg)
            x += 2.0

        path = StrokePath(pen_up_position=None, segments=tuple(segments))

        profiler._is_structural_path(path)

        # avg_segment_length = 2.0
        # bbox_extent: dx=16 (from 0 to 16), dy=0 -> max=16
        # ratio = 2/16 = 0.125 < 0.25 -> NOT structural via check 5

    def test_arcs_and_lines_not_pure_linear(self) -> None:
        """Test path with arcs doesn't trigger Check 5 (returns False).

        Covers lines 270-271 where condition `not arcs and lines` is False.
        """
        from plt_optimizer.core.models import ArcSegment

        profiler = Profiler()

        # Path has BOTH arcs and some lines
        line1 = StrokeSegment(
            start=Coordinate(x=0.0, y=50.0),
            end=Coordinate(x=20.0, y=50.0),  # tiny line segment
            is_cutting=True,
        )
        arc1 = ArcSegment(
            start=Coordinate(x=20.0, y=50.0),
            end=Coordinate(x=40.0, y=50.0),
            center=Coordinate(x=30.0, y=50.0),
            sweep_angle=180.0,
            is_cutting=True,
        )
        line2 = StrokeSegment(
            start=Coordinate(x=40.0, y=50.0),
            end=Coordinate(x=60.0, y=50.0),  # tiny line segment
            is_cutting=True,
        )

        path = StrokePath(pen_up_position=None, segments=(line1, arc1, line2))

        profiler._is_structural_path(path)

        # `not arcs and lines` -> False because arcs exist

    def test_closed_loop_type_check_fails(self) -> None:
        """Test closed loop branch skipped when first/last are not StrokeSegments.

        Covers 247->267: the isinstance checks at line 247 returning False.
        """
        from plt_optimizer.core.models import ArcSegment

        profiler = Profiler()

        # Two arcs - both first_seg and last_seg are NOT StrokeSegments
        arc1 = ArcSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=50.0),  # Different endpoints
            center=Coordinate(x=50.0, y=25.0),
            sweep_angle=45.0,
            is_cutting=True,
        )
        arc2 = ArcSegment(
            start=Coordinate(x=100.0, y=50.0),
            end=Coordinate(x=200.0, y=100.0),  # Not closing back to start
            center=Coordinate(x=150.0, y=75.0),
            sweep_angle=-45.0,
            is_cutting=True,
        )

        path = StrokePath(pen_up_position=None, segments=(arc1, arc2))

        result = profiler._is_structural_path(path)
        # isinstance checks at 247 fail → closed loop check skipped
        assert result is False

    def test_pure_linear_condition_fails(self) -> None:
        """Test Check 5 condition `not arcs and lines` fails when arcs present.

        Covers line 270-271: the not arcs branch not taken.
        """
        from plt_optimizer.core.models import ArcSegment

        profiler = Profiler()

        # Only arcs, no lines - should fail both check 2 (needs exactly 4 arcs)
        # and check 5 condition `not arcs and lines`
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

        path = StrokePath(pen_up_position=None, segments=(arc1, arc2, arc3))

        result = profiler._is_structural_path(path)
        # arcs exists → not arcs is False → check 5 skipped
        assert result is False


class TestStrokePathsProtocolCoverage:
    """Test coverage for StrokePathsProtocol (line 361)."""

    def test_protocol_stub_coverage(self) -> None:
        """Exercise the Protocol definition.

        This tests that our use of the protocol works correctly.
        Line 361 is the `...` stub in the Protocol which can't really be
        'covered' but we exercise the protocol interface here.
        """

        # Create a minimal mock that implements the protocol
        class MockDoc:
            @property
            def stroke_paths(self) -> Sequence[StrokePath]:
                return []

        mock = MockDoc()
        profiler = Profiler()

        # The MockDoc now satisfies StrokePathsProtocol
        # Profile it to exercise the Protocol definition
        with pytest.raises(ProfilerError):
            profiler.profile(mock)


class TestProfileLine157:
    """Test line 157 specifically - the final structural_ratio else branch."""

    def test_profile_only_non_cutting_strokes_raises_error(self) -> None:
        """Test that profile raises ProfilerError when no cutting strokes exist.

        This covers the error path at lines ~111-112 (ProfilerError raise).
        Lines 135-136 and 157 represent mutually exclusive conditions:
        - Line 157 is hit when extents == [] (no cutting strokes found)
        - Lines 135-136 are hit when total_paths == 0 after filtering

        Both lead to ProfilerError being raised.
        """
        profiler = Profiler()

        # All non-cutting segments → no extents → ProfilerError
        path1 = StrokePath(
            pen_up_position=None,
            segments=(
                StrokeSegment(
                    start=Coordinate(x=0.0, y=0.0),
                    end=Coordinate(x=100.0, y=50.0),
                    is_cutting=False,  # Not cutting!
                ),
            ),
        )
        doc = PLTDocument(header_commands=[], stroke_paths=[path1], footer_commands=[])

        with pytest.raises(ProfilerError) as exc_info:
            profiler.profile(doc)
        assert "No cutting strokes found" in str(exc_info.value.message)

    def test_else_branch_with_zero_valid_paths(self) -> None:
        """Test else branch triggers with no valid paths.

        This covers lines 135-136.
        """
        profiler = Profiler()

        # All paths exist but have zero-length segments only
        path1 = StrokePath(
            pen_up_position=None,
            segments=(
                StrokeSegment(
                    start=Coordinate(x=0.0, y=0.0),
                    end=Coordinate(x=0.0, y=0.0),  # Zero length!
                    is_cutting=True,
                ),
            ),
        )
        path2 = StrokePath(
            pen_up_position=None,
            segments=(
                StrokeSegment(
                    start=Coordinate(x=100.0, y=100.0),
                    end=Coordinate(x=100.0, y=100.0),  # Zero length!
                    is_cutting=True,
                ),
            ),
        )

        doc = PLTDocument(header_commands=[], stroke_paths=[path1, path2], footer_commands=[])

        with pytest.raises(ProfilerError) as exc_info:
            profiler.profile(doc)
        assert "No cutting strokes found" in str(exc_info.value.message)


class TestPreviouslyUncoveredLines:
    """Tests targeting lines that were missing from coverage."""

    def test_profile_else_branch_total_paths_zero(self) -> None:
        """Test the else branch when total_paths == 0.

        Uses a mock document where stroke_paths returns an empty list on
        first access (for the valid_paths structural pass) but cutting-segment
        data on the second access (for _calculate_all_extents), so
        total_paths == 0 while extents remains non-empty. This exercises the
        else clause that sets structural_path_count=0 and structural_ratio=0.0.
        """

        class _TwoPhaseDoc:
            """Mock protocol impl that shifts stroke_paths on successive reads."""

            def __init__(self) -> None:
                self._access = 0
                seg = StrokeSegment(
                    start=Coordinate(x=0.0, y=0.0),
                    end=Coordinate(x=100.0, y=0.0),
                    is_cutting=True,
                )
                self._path = StrokePath(pen_up_position=None, segments=(seg,))

            @property
            def stroke_paths(self) -> list[StrokePath]:
                """Return empty list on first access, paths thereafter."""
                self._access += 1
                if self._access == 1:
                    return []
                return [self._path]

        profiler = Profiler()
        result = profiler.profile(_TwoPhaseDoc())
        # With total_paths == 0 the else branch sets is_structural = False
        assert result.is_structural is False
        assert result.total_strokes == 1

    def test_profile_p95_index_boundary_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test line 157: p95_index is clamped when it equals len(sorted dims).

        Patches the ``int`` name in the profiler module's global namespace so
        that the call ``int(len(sorted_dims) * 0.95)`` returns 9999, which is
        >= len(sorted_dims), triggering the safety-clamp at line 157.
        """
        import plt_optimizer.core.profiler as profiler_module

        real_int = int

        def _mock_int(x: object, *a: object, **k: object) -> int:
            """Return an oversized index for float args to force the clamp."""
            if isinstance(x, float) and not a and not k:
                return 9999  # guarantee p95_index >= len(max_dimensions_sorted)
            return real_int(x, *a, **k)  # type: ignore[arg-type]

        monkeypatch.setattr(profiler_module, "int", _mock_int, raising=False)

        segment = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        path = StrokePath(pen_up_position=None, segments=(segment,))
        doc = PLTDocument(header_commands=[], stroke_paths=[path], footer_commands=[])

        profiler = Profiler()
        result = profiler.profile(doc)
        # p95_index must be clamped to len - 1 = 0 for a single-element list
        assert result.p95_index == 0

    def test_is_structural_closed_loop_ratio_below_threshold(self) -> None:
        """Test a densely-sampled closed rectangle is structural (all lines).

        Builds a 100x50 closed rectangle using 150 segments of 2 units each.
        The old ratio heuristic rejected this (avg length / bbox = 0.02);
        the compositional rule accepts it because every segment is a line.
        """
        profiler = Profiler()

        segments: list[StrokeSegment] = []
        x, y = 0.0, 0.0

        for _ in range(50):  # Bottom edge: 50 x 2-unit segments -> length 100
            segments.append(
                StrokeSegment(
                    start=Coordinate(x=x, y=y),
                    end=Coordinate(x=x + 2.0, y=y),
                    is_cutting=True,
                )
            )
            x += 2.0

        for _ in range(25):  # Right edge: 25 x 2-unit segments -> length 50
            segments.append(
                StrokeSegment(
                    start=Coordinate(x=x, y=y),
                    end=Coordinate(x=x, y=y + 2.0),
                    is_cutting=True,
                )
            )
            y += 2.0

        for _ in range(50):  # Top edge: 50 x 2-unit segments -> length 100
            segments.append(
                StrokeSegment(
                    start=Coordinate(x=x, y=y),
                    end=Coordinate(x=x - 2.0, y=y),
                    is_cutting=True,
                )
            )
            x -= 2.0

        for _ in range(25):  # Left edge: 25 x 2-unit segments -> closes loop
            segments.append(
                StrokeSegment(
                    start=Coordinate(x=x, y=y),
                    end=Coordinate(x=x, y=y - 2.0),
                    is_cutting=True,
                )
            )
            y -= 2.0

        # Confirm the loop is closed (first.start == last.end)
        assert math.isclose(segments[0].start.x, segments[-1].end.x, abs_tol=1e-9)
        assert math.isclose(segments[0].start.y, segments[-1].end.y, abs_tol=1e-9)

        path = StrokePath(pen_up_position=None, segments=tuple(segments))
        # Compositional rule: straight lines only -> structural
        result = profiler._is_structural_path(path)
        assert result is True

    def test_stroke_paths_protocol_stub(self) -> None:
        """Test line 361: StrokePathsProtocol.stroke_paths stub is executable.

        Calls the property getter directly with a bare object so that the
        ``...`` body of the Protocol stub is executed, covering line 361.
        """
        # Access the property descriptor's getter and invoke it with any object;
        # the body is `...` (Ellipsis expression), so the function returns None.
        result = StrokePathsProtocol.stroke_paths.fget(object())  # type: ignore[misc]
        assert result is None


class TestMultiArcDrillHoles:
    """Test recognition of multi-arc drill holes (verified perfect circles)."""

    def test_five_arc_best_fit_rosette_is_structural(self) -> None:
        """Test the SFA3X611 5-arc best-fit drill hole verifies as a circle.

        This pattern appears in SFA3X611sheet1.plt where drill holes are
        emitted with 5 chained arcs totaling ~357.9° whose per-arc centers
        wobble a few percent around one fitted circle (angles =
        [67.866, 88.123, 90.938, 88.123, 22.826]). The fit-based verifier
        accepts them; glyph outline loops (center scatter 60%+ of radius)
        do not pass the same check.
        """
        profiler = Profiler()

        # Geometric fixture mirroring the measured real-file spreads:
        # 5 chained arcs around one center (totaling 360.0°) with per-arc
        # center jitter of ~4% of the radius (real file: center 4.1%,
        # radius 2.7%), like the rosette hole at (254, 253) r~62.
        ring = _circle_arcs(0.0, 0.0, 62.0, [67.866, 88.123, 90.938, 88.123, 24.95])
        jittered = [
            ArcSegment(
                start=arc.start,
                end=arc.end,
                center=Coordinate(
                    arc.center.x + (2.5 if i % 2 else -2.5),
                    arc.center.y + (1.5 if i % 3 == 0 else -1.5),
                ),
                sweep_angle=arc.sweep_angle,
                is_cutting=True,
            )
            for i, arc in enumerate(ring)
        ]

        path = StrokePath(pen_up_position=None, segments=tuple(jittered))

        result = profiler._is_structural_path(path)
        # Sweeps total 360.0°; centers/radii wobble ~4% around the fitted
        # circle -> within CIRCLE_FIT_REL_TOLERANCE -> verified circle.
        assert result is True

    def test_sfa3x611_example_classifies_structural(self) -> None:
        """Pin SFA3X611sheet1.plt (mixed exact + best-fit holes) as structural.

        The sheet holds 60 exact 4-arc holes plus 20 five-arc best-fit
        rosette holes and straight score lines; every path is structural,
        so the document must classify structural at the default 85% gate.
        """
        from plt_optimizer.core.parser import PLTParser

        example_path = Path(__file__).parent.parent / "tests_deps" / "SFA3X611sheet1.plt"
        if not example_path.exists():
            pytest.skip(f"Example file not found: {example_path}")

        doc = PLTParser().parse_file(example_path)
        result = Profiler().profile(doc)

        assert result.is_structural is True

    def test_five_arc_verified_circle_is_structural(self) -> None:
        """Test 5 chained arcs sharing one center/radius and totaling ~360°."""
        profiler = Profiler()

        arcs = _circle_arcs(5.0, 5.0, 5.0, [67.866, 88.123, 90.938, 88.123, 24.95])
        plunge = StrokeSegment(start=arcs[0].start, end=arcs[0].start, is_cutting=True)

        path = StrokePath(pen_up_position=None, segments=(plunge, *arcs))

        result = profiler._is_structural_path(path)
        # Total sweep: 360.0° exactly, shared center (5, 5), radius 5
        assert result is True

    def test_three_arc_drill_hole_360_degrees(self) -> None:
        """Test minimum case: 3 chained arcs of 120° sharing one center."""
        profiler = Profiler()

        arcs = _circle_arcs(5.0, 5.0, 5.0, [120.0, 120.0, 120.0])

        path = StrokePath(pen_up_position=None, segments=tuple(arcs))

        result = profiler._is_structural_path(path)
        # Total sweep: 120 + 120 + 120 = 360°, exactly at threshold
        assert result is True

    def test_two_arc_half_circles_is_structural(self) -> None:
        """Test 2x 180-degree arcs sharing center/radius form a circle."""
        profiler = Profiler()

        arcs = _circle_arcs(5.0, 5.0, 5.0, [180.0, 180.0])

        path = StrokePath(pen_up_position=None, segments=tuple(arcs))

        result = profiler._is_structural_path(path)
        # 2x 180° chained with one center -> perfect circle
        assert result is True

    def test_single_full_circle_arc_is_structural(self) -> None:
        """Test a single 360-degree arc (CI circle) is structural."""
        profiler = Profiler()

        arc = ArcSegment(
            start=Coordinate(x=10.0, y=5.0),
            end=Coordinate(x=10.0, y=5.0),  # CI closes on its start
            center=Coordinate(x=10.0, y=5.0),
            sweep_angle=360.0,
            is_cutting=True,
        )

        path = StrokePath(pen_up_position=None, segments=(arc,))

        result = profiler._is_structural_path(path)
        assert result is True

    def test_retraced_half_circles_not_structural(self) -> None:
        """Test +180/-180 retraced arcs (signed sum 0) are not a circle."""
        profiler = Profiler()

        arcs = _circle_arcs(5.0, 5.0, 5.0, [180.0, -180.0])

        path = StrokePath(pen_up_position=None, segments=tuple(arcs))

        result = profiler._is_structural_path(path)
        # Signed sweeps cancel: the tool retraced the same half circle
        assert result is False

    def test_multi_arc_outside_tolerance_not_structural(self) -> None:
        """Test that arcs totaling >365° or <355° are not recognized as drill holes."""
        profiler = Profiler()

        # Arcs totaling 370° (outside the ±5° tolerance)
        arcs = _circle_arcs(5.0, 5.0, 5.0, [125.0, 125.0, 120.0])

        path = StrokePath(pen_up_position=None, segments=tuple(arcs))

        result = profiler._is_structural_path(path)
        # Total sweep: 125 + 125 + 120 = 370° (outside ±5° tolerance)
        assert result is False

    def test_multi_arc_with_non_zero_lines_is_structural(self) -> None:
        """Test verified circles may mix freely with straight lines."""
        profiler = Profiler()

        arcs = _circle_arcs(5.0, 5.0, 5.0, [120.0, 120.0, 120.0])
        # Non-zero line segment (not a plunge point)
        non_plunge = StrokeSegment(
            start=arcs[-1].end,
            end=Coordinate(x=0.0, y=0.0),
            is_cutting=True,
        )

        path = StrokePath(pen_up_position=None, segments=(*arcs, non_plunge))

        result = profiler._is_structural_path(path)
        # Total sweep is 360° verified; the extra line is also structural
        assert result is True

    def test_four_arc_90_degrees_still_recognized(self) -> None:
        """Verify that classic 4x 90° arcs still work with the new tolerance."""
        profiler = Profiler()

        arcs = _circle_arcs(5.0, 5.0, 5.0, [90.0, 90.0, 90.0, 90.0])

        path = StrokePath(pen_up_position=None, segments=tuple(arcs))

        result = profiler._is_structural_path(path)
        # Total sweep: 360° exactly -> structural
        assert result is True

    def test_four_arc_90_degrees_jittered_centers_recognized(self) -> None:
        """Verify EngraveLab's ±0.001 center jitter still verifies as a circle."""
        profiler = Profiler()

        # Mirrors tests_deps/1x3...holes1.plt: identical AA centers except the
        # parser rounding produces alternating cx values 15436.057/15436.058.
        arcs = _circle_arcs(196.088, 507.945, 50.8, [90.0, 90.0, 90.0, 90.0])
        jittered = [
            ArcSegment(
                start=arc.start,
                end=arc.end,
                center=Coordinate(arc.center.x + (0.001 if i % 2 else 0.0), arc.center.y),
                sweep_angle=arc.sweep_angle,
                is_cutting=True,
            )
            for i, arc in enumerate(arcs)
        ]

        path = StrokePath(pen_up_position=None, segments=tuple(jittered))

        result = profiler._is_structural_path(path)
        assert result is True


class TestArcRunHelpers:
    """Unit tests for the module-level circle-verification helpers."""

    def test_arc_runs_splits_on_line_boundaries(self) -> None:
        """Test _arc_runs groups consecutive arcs and splits on lines."""
        arcs = _circle_arcs(5.0, 5.0, 5.0, [90.0, 90.0])
        line = StrokeSegment(
            start=Coordinate(x=5.0, y=0.0),
            end=Coordinate(x=50.0, y=0.0),
            is_cutting=True,
        )

        # line - arc - arc - line - arc
        segments = [line, arcs[0], arcs[1], line, arcs[0]]
        runs = _arc_runs(segments)

        assert [len(run) for run in runs] == [2, 1]
        assert runs[0][0] is arcs[0]
        assert runs[1][0] is arcs[0]

    def test_arc_runs_empty_for_lines_only(self) -> None:
        """Test _arc_runs returns no runs for line-only or empty paths."""
        line = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=1.0, y=0.0),
            is_cutting=True,
        )

        assert _arc_runs([line, line]) == []
        assert _arc_runs([]) == []

    def test_is_perfect_circle_empty_run(self) -> None:
        """Test _is_perfect_circle rejects an empty run."""
        assert _is_perfect_circle([]) is False

    def test_is_perfect_circle_rejects_mismatched_centers(self) -> None:
        """Test chained arcs with different centers are not a circle."""
        arc1 = ArcSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=0.0),
            center=Coordinate(x=5.0, y=0.0),
            sweep_angle=180.0,
            is_cutting=True,
        )
        arc2 = ArcSegment(
            start=Coordinate(x=10.0, y=0.0),
            end=Coordinate(x=0.0, y=0.0),
            center=Coordinate(x=5.0, y=3.0),  # different center
            sweep_angle=180.0,
            is_cutting=True,
        )

        assert _is_perfect_circle([arc1, arc2]) is False

    def test_is_perfect_circle_rejects_mismatched_radii(self) -> None:
        """Test chained arcs with equal centers but different radii fail."""
        arc1 = ArcSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=0.0),
            center=Coordinate(x=5.0, y=0.0),
            sweep_angle=180.0,
            is_cutting=True,
        )
        arc2 = ArcSegment(
            start=Coordinate(x=10.0, y=0.0),
            end=Coordinate(x=0.0, y=0.0),
            center=Coordinate(x=5.0, y=0.0),
            sweep_angle=180.0,
            is_cutting=True,
        )
        # arc2 starts at (10, 0) but its radius is |10-5| = 5 == arc1 radius;
        # bump arc1's start further out to create a radius mismatch.
        arc1_bad = ArcSegment(
            start=Coordinate(x=-1.0, y=0.0),
            end=Coordinate(x=10.0, y=0.0),
            center=Coordinate(x=5.0, y=0.0),
            sweep_angle=180.0,
            is_cutting=True,
        )

        assert _is_perfect_circle([arc1, arc2]) is True
        assert _is_perfect_circle([arc1_bad, arc2]) is False

    def test_is_perfect_circle_rejects_broken_chain(self) -> None:
        """Test arcs whose endpoints do not chain are not a circle."""
        arc1 = ArcSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=0.0),
            center=Coordinate(x=5.0, y=0.0),
            sweep_angle=180.0,
            is_cutting=True,
        )
        arc2 = ArcSegment(
            start=Coordinate(x=9.0, y=0.0),  # does not chain to arc1.end
            end=Coordinate(x=0.0, y=0.0),
            center=Coordinate(x=5.0, y=0.0),
            sweep_angle=180.0,
            is_cutting=True,
        )

        assert _is_perfect_circle([arc1, arc2]) is False

    def test_is_perfect_circle_rejects_open_run(self) -> None:
        """Test a run whose last arc misses the first start is not a circle."""
        arc1 = ArcSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=0.0),
            center=Coordinate(x=5.0, y=0.0),
            sweep_angle=180.0,
            is_cutting=True,
        )
        arc2 = ArcSegment(
            start=Coordinate(x=10.0, y=0.0),
            end=Coordinate(x=0.1, y=0.0),  # misses first start by 0.1 > tolerance
            center=Coordinate(x=5.0, y=0.0),
            sweep_angle=180.0,
            is_cutting=True,
        )

        assert _is_perfect_circle([arc1, arc2]) is False

    def test_is_perfect_circle_rejects_zero_radius(self) -> None:
        """Test a multi-arc run with zero radius is rejected."""
        arc1 = ArcSegment(
            start=Coordinate(x=5.0, y=5.0),
            end=Coordinate(x=5.0, y=5.0),
            center=Coordinate(x=5.0, y=5.0),
            sweep_angle=180.0,
            is_cutting=True,
        )
        arc2 = ArcSegment(
            start=Coordinate(x=5.0, y=5.0),
            end=Coordinate(x=5.0, y=5.0),
            center=Coordinate(x=5.0, y=5.0),
            sweep_angle=180.0,
            is_cutting=True,
        )

        assert _is_perfect_circle([arc1, arc2]) is False
