"""Tests for stroke simplification functions.

These tests cover:
- is_point_on_segment() function (lines 207-242 in geometry.py)
- remove_redundant_strokes() function (lines 248-332 in geometry.py)
"""

from __future__ import annotations

import math
import pytest

from plt_optimizer.core.models import (
    Coordinate,
    FooterCommand,
    HeaderCommand,
    PLTDocument,
    StrokePath,
    StrokeSegment,
)


class TestIsPointOnSegment:
    """Tests for is_point_on_segment function."""

    def test_point_on_horizontal_segment(self) -> None:
        """Test point on horizontal segment."""
        from plt_optimizer.utils.geometry import is_point_on_segment

        a = Coordinate(x=0.0, y=0.0)
        b = Coordinate(x=10.0, y=0.0)
        p = Coordinate(x=5.0, y=0.0)

        assert is_point_on_segment(p, a, b) is True

    def test_point_on_vertical_segment(self) -> None:
        """Test point on vertical segment."""
        from plt_optimizer.utils.geometry import is_point_on_segment

        a = Coordinate(x=5.0, y=-10.0)
        b = Coordinate(x=5.0, y=20.0)
        p = Coordinate(x=5.0, y=5.0)

        assert is_point_on_segment(p, a, b) is True

    def test_point_at_start_endpoint(self) -> None:
        """Test point at segment start endpoint."""
        from plt_optimizer.utils.geometry import is_point_on_segment

        a = Coordinate(x=1.0, y=2.0)
        b = Coordinate(x=10.0, y=20.0)
        p = Coordinate(x=1.0, y=2.0)

        assert is_point_on_segment(p, a, b) is True

    def test_point_at_end_endpoint(self) -> None:
        """Test point at segment end endpoint."""
        from plt_optimizer.utils.geometry import is_point_on_segment

        a = Coordinate(x=1.0, y=2.0)
        b = Coordinate(x=10.0, y=20.0)
        p = Coordinate(x=10.0, y=20.0)

        assert is_point_on_segment(p, a, b) is True

    def test_point_outside_segment_bounds_collinear(self) -> None:
        """Test point collinear but beyond segment bounds."""
        from plt_optimizer.utils.geometry import is_point_on_segment

        a = Coordinate(x=0.0, y=0.0)
        b = Coordinate(x=10.0, y=0.0)
        p = Coordinate(x=15.0, y=0.0)

        assert is_point_on_segment(p, a, b) is False

    def test_point_before_start_collinear(self) -> None:
        """Test point before start of segment but collinear."""
        from plt_optimizer.utils.geometry import is_point_on_segment

        a = Coordinate(x=5.0, y=5.0)
        b = Coordinate(x=10.0, y=10.0)
        p = Coordinate(x=0.0, y=0.0)

        assert is_point_on_segment(p, a, b) is False

    def test_point_not_collinear(self) -> None:
        """Test point not on line (different slope)."""
        from plt_optimizer.utils.geometry import is_point_on_segment

        a = Coordinate(x=0.0, y=0.0)
        b = Coordinate(x=10.0, y=0.0)
        p = Coordinate(x=5.0, y=1.0)

        assert is_point_on_segment(p, a, b) is False

    def test_zero_length_segment_point(self) -> None:
        """Test with zero-length segment (point)."""
        from plt_optimizer.utils.geometry import is_point_on_segment

        p = Coordinate(x=5.0, y=5.0)
        a = Coordinate(x=5.0, y=5.0)
        b = Coordinate(x=5.0, y=5.0)

        result = is_point_on_segment(p, a, b)
        assert result is True

    def test_zero_length_segment_different_point(self) -> None:
        """Test zero-length segment with different point."""
        from plt_optimizer.utils.geometry import is_point_on_segment

        a = Coordinate(x=5.0, y=5.0)
        b = Coordinate(x=5.0, y=5.0)
        p = Coordinate(x=10.0, y=10.0)

        assert is_point_on_segment(p, a, b) is False


class TestRemoveRedundantStrokes:
    """Tests for remove_redundant_strokes function."""

    def test_simple_short_stroke_inside_long(self) -> None:
        """Test short stroke completely inside longer stroke."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes

        long_seg = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=0.0),
            is_cutting=True,
        )
        short_seg = StrokeSegment(
            start=Coordinate(x=3.0, y=0.0),
            end=Coordinate(x=7.0, y=0.0),
            is_cutting=True,
        )

        doc = PLTDocument(
            header_commands=[HeaderCommand(instruction="IN")],
            stroke_paths=[
                StrokePath(segments=(long_seg,)),
                StrokePath(segments=(short_seg,)),
            ],
            footer_commands=[FooterCommand(instruction="SP")],
        )

        result = remove_redundant_strokes(doc)

        total_segs = sum(len(p.segments) for p in result.stroke_paths)
        assert total_segs == 1

    def test_identical_overlapping_strokes_keeps_first(self) -> None:
        """Test identical overlapping strokes keeps only first."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes

        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=0.0),
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=0.0),
            is_cutting=True,
        )

        doc = PLTDocument(
            header_commands=[HeaderCommand(instruction="IN")],
            stroke_paths=[
                StrokePath(segments=(seg1,)),
                StrokePath(segments=(seg2,)),
            ],
            footer_commands=[FooterCommand(instruction="SP")],
        )

        result = remove_redundant_strokes(doc)

        total_segs = sum(len(p.segments) for p in result.stroke_paths)
        assert total_segs == 1

    def test_reversed_identical_strokes_keeps_first(self) -> None:
        """Test reversed identical strokes (A->B and B->A) keeps first."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes

        seg_ab = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=0.0),
            is_cutting=True,
        )
        seg_ba = StrokeSegment(
            start=Coordinate(x=10.0, y=0.0),
            end=Coordinate(x=0.0, y=0.0),
            is_cutting=True,
        )

        doc = PLTDocument(
            header_commands=[HeaderCommand(instruction="IN")],
            stroke_paths=[
                StrokePath(segments=(seg_ab,)),
                StrokePath(segments=(seg_ba,)),
            ],
            footer_commands=[FooterCommand(instruction="SP")],
        )

        result = remove_redundant_strokes(doc)

        total_segs = sum(len(p.segments) for p in result.stroke_paths)
        assert total_segs == 1

    def test_grid_rectangles_shared_edges(self) -> None:
        """Test grid of rectangles where shared edges should be deduplicated.

        Two adjacent rectangles share an edge - the shared edge appears twice
        and one copy should be removed.
        """
        from plt_optimizer.utils.geometry import remove_redundant_strokes

        rect1_left = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=0.0, y=10.0),
            is_cutting=True,
        )
        rect1_bottom = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=0.0),
            is_cutting=True,
        )
        rect1_right = StrokeSegment(
            start=Coordinate(x=10.0, y=0.0),
            end=Coordinate(x=10.0, y=10.0),
            is_cutting=True,
        )
        rect1_top = StrokeSegment(
            start=Coordinate(x=0.0, y=10.0),
            end=Coordinate(x=10.0, y=10.0),
            is_cutting=True,
        )

        rect2_left = StrokeSegment(
            start=Coordinate(x=10.0, y=0.0),
            end=Coordinate(x=10.0, y=10.0),
            is_cutting=True,
        )
        rect2_bottom = StrokeSegment(
            start=Coordinate(x=10.0, y=0.0),
            end=Coordinate(x=20.0, y=0.0),
            is_cutting=True,
        )
        rect2_right = StrokeSegment(
            start=Coordinate(x=20.0, y=0.0),
            end=Coordinate(x=20.0, y=10.0),
            is_cutting=True,
        )

        doc = PLTDocument(
            header_commands=[HeaderCommand(instruction="IN")],
            stroke_paths=[
                StrokePath(segments=(rect1_left, rect1_bottom, rect1_right, rect1_top)),
                StrokePath(segments=(rect2_left, rect2_bottom, rect2_right)),
            ],
            footer_commands=[FooterCommand(instruction="SP")],
        )

        result = remove_redundant_strokes(doc)

        total_segs = sum(len(p.segments) for p in result.stroke_paths)
        assert total_segs == 6

    def test_no_cutting_segments_unchanged(self) -> None:
        """Test document with only rapid moves unchanged."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes

        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=0.0),
            is_cutting=False,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=10.0, y=0.0),
            end=Coordinate(x=20.0, y=0.0),
            is_cutting=False,
        )

        doc = PLTDocument(
            header_commands=[HeaderCommand(instruction="IN")],
            stroke_paths=[
                StrokePath(segments=(seg1,)),
                StrokePath(segments=(seg2,)),
            ],
            footer_commands=[FooterCommand(instruction="SP")],
        )

        result = remove_redundant_strokes(doc)

        total_segs = sum(len(p.segments) for p in result.stroke_paths)
        assert total_segs == 2

    def test_empty_path_filtered_out(self) -> None:
        """Test that paths becoming empty are filtered out."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes

        long_seg = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=0.0),
            is_cutting=True,
        )
        short_inside_long1 = StrokeSegment(
            start=Coordinate(x=2.0, y=0.0),
            end=Coordinate(x=4.0, y=0.0),
            is_cutting=True,
        )
        short_inside_long2 = StrokeSegment(
            start=Coordinate(x=6.0, y=0.0),
            end=Coordinate(x=8.0, y=0.0),
            is_cutting=True,
        )

        doc = PLTDocument(
            header_commands=[HeaderCommand(instruction="IN")],
            stroke_paths=[
                StrokePath(segments=(long_seg,)),
                StrokePath(segments=(short_inside_long1,)),
                StrokePath(segments=(short_inside_long2,)),
            ],
            footer_commands=[FooterCommand(instruction="SP")],
        )

        result = remove_redundant_strokes(doc)

        total_segs = sum(len(p.segments) for p in result.stroke_paths)
        assert total_segs == 1
        assert len(result.stroke_paths) == 1

    def test_diagonal_segment_not_removed(self) -> None:
        """Test that segments on different lines are not removed."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes

        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=10.0),
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=0.0, y=5.0),
            end=Coordinate(x=5.0, y=10.0),
            is_cutting=True,
        )

        doc = PLTDocument(
            header_commands=[HeaderCommand(instruction="IN")],
            stroke_paths=[
                StrokePath(segments=(seg1,)),
                StrokePath(segments=(seg2,)),
            ],
            footer_commands=[FooterCommand(instruction="SP")],
        )

        result = remove_redundant_strokes(doc)

        total_segs = sum(len(p.segments) for p in result.stroke_paths)
        assert total_segs == 2

    def test_preserves_header_footer(self) -> None:
        """Test that header and footer commands are preserved."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes

        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=0.0),
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=3.0, y=0.0),
            end=Coordinate(x=7.0, y=0.0),
            is_cutting=True,
        )

        doc = PLTDocument(
            header_commands=[HeaderCommand(instruction="IN"), HeaderCommand(instruction="VS", parameters=(50.0,))],
            stroke_paths=[
                StrokePath(segments=(seg1,)),
                StrokePath(segments=(seg2,)),
            ],
            footer_commands=[FooterCommand(instruction="SP")],
        )

        result = remove_redundant_strokes(doc)

        assert len(result.header_commands) == 2
        assert result.header_commands[0].instruction == "IN"
        assert len(result.footer_commands) == 1
        assert result.footer_commands[0].instruction == "SP"

    def test_middle_segment_removal_creates_fracture(self) -> None:
        """Test removing middle backtrack creates separate paths."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes

        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=0.0),
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=10.0, y=0.0),
            end=Coordinate(x=0.0, y=0.0),
            is_cutting=True,
        )
        seg3 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=0.0, y=5.0),
            is_cutting=True,
        )

        doc = PLTDocument(
            header_commands=[HeaderCommand(instruction="IN")],
            stroke_paths=[
                StrokePath(pen_up_position=Coordinate(x=0.0, y=0.0), segments=(seg1, seg2, seg3)),
            ],
            footer_commands=[FooterCommand(instruction="SP")],
        )

        result = remove_redundant_strokes(doc)

        assert len(result.stroke_paths) == 2
        assert len(result.stroke_paths[0].segments) == 1
        assert result.stroke_paths[1].pen_up_position == Coordinate(x=0.0, y=0.0)
        assert len(result.stroke_paths[1].segments) == 1

    def test_pen_up_position_propagation_after_fracture(self) -> None:
        """Test pen-up position equals first segment start after fracture."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes

        seg1 = StrokeSegment(
            start=Coordinate(x=100.0, y=200.0),
            end=Coordinate(x=300.0, y=200.0),
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=300.0, y=200.0),
            end=Coordinate(x=100.0, y=200.0),
            is_cutting=True,
        )
        seg3 = StrokeSegment(
            start=Coordinate(x=100.0, y=200.0),
            end=Coordinate(x=100.0, y=400.0),
            is_cutting=True,
        )

        doc = PLTDocument(
            header_commands=[HeaderCommand(instruction="IN")],
            stroke_paths=[
                StrokePath(pen_up_position=Coordinate(x=50.0, y=100.0), segments=(seg1, seg2, seg3)),
            ],
            footer_commands=[FooterCommand(instruction="SP")],
        )

        result = remove_redundant_strokes(doc)

        assert len(result.stroke_paths) == 2
        assert result.stroke_paths[0].pen_up_position == Coordinate(x=50.0, y=100.0)
        assert result.stroke_paths[1].pen_up_position == Coordinate(x=100.0, y=200.0)

    def test_multiple_removals_in_one_path(self) -> None:
        """Test removing multiple interior segments creates corresponding fractures."""
        from plt_optimizer.utils.geometry import remove_redundant_strokes

        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=0.0),
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=10.0, y=0.0),
            end=Coordinate(x=5.0, y=0.0),
            is_cutting=True,
        )
        seg3 = StrokeSegment(
            start=Coordinate(x=5.0, y=0.0),
            end=Coordinate(x=20.0, y=0.0),
            is_cutting=True,
        )

        doc = PLTDocument(
            header_commands=[HeaderCommand(instruction="IN")],
            stroke_paths=[
                StrokePath(pen_up_position=Coordinate(x=0.0, y=0.0), segments=(seg1, seg2, seg3)),
            ],
            footer_commands=[FooterCommand(instruction="SP")],
        )

        result = remove_redundant_strokes(doc)

        assert len(result.stroke_paths) == 2
        assert all(len(p.segments) == 1 for p in result.stroke_paths)
        assert result.stroke_paths[0].pen_up_position == Coordinate(x=0.0, y=0.0)
        assert result.stroke_paths[1].pen_up_position == Coordinate(x=5.0, y=0.0)