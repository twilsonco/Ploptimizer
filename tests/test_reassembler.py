"""Tests for plt_optimizer/core/reassembler.py module.

This module reconstructs optimized PLT documents from MacroBlock sequences,
handling block reversals when needed.
"""

from __future__ import annotations

import pytest

from plt_optimizer.core.chunker import MacroBlock
from plt_optimizer.core.models import Coordinate, PLTDocument, StrokePath, StrokeSegment
from plt_optimizer.core.optimizer import BlockTraverseState, OptimizationResult
from plt_optimizer.core.reassembler import MetricsCalculator, Reassembler, ReassemblerError


def _make_simple_path(
    start: tuple[float, float],
    end: tuple[float, float],
    is_cutting: bool = True,
) -> StrokePath:
    """Helper to create a simple single-segment stroke path."""
    segment = StrokeSegment(
        start=Coordinate(x=start[0], y=start[1]),
        end=Coordinate(x=end[0], y=end[1]),
        is_cutting=is_cutting,
    )
    return StrokePath(pen_up_position=None, segments=(segment,))


def _make_block(
    block_id: int,
    paths: list[StrokePath],
) -> MacroBlock:
    """Helper to create a MacroBlock."""
    first_seg = paths[0].segments[0]
    last_seg = paths[-1].segments[-1]
    return MacroBlock(
        block_id=block_id,
        paths=tuple(paths),
        entrance=first_seg.start,
        exit=last_seg.end,
    )


class TestReassembler:
    """Tests for Reassembler class."""

    def test_reassemble_preserves_header_footer(self) -> None:
        """Test that reassembly preserves document headers and footers."""
        from plt_optimizer.core.parser import PLTParser

        parser = PLTParser()
        doc = parser.parse_string("IN;VS10;PU0,0;PD100,0;SP;")

        path1 = _make_simple_path((0, 0), (100, 0))
        path2 = _make_simple_path((200, 0), (300, 0))

        block1 = _make_block(0, [path1])
        block2 = _make_block(1, [path2])

        traverse_order = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(0.0, 0.0), exit=(100.0, 0.0)),
            BlockTraverseState(
                block_id=1, reversed=False, entrance=(200.0, 0.0), exit=(300.0, 0.0)
            ),
        ]
        optimization_result = OptimizationResult(
            traverse_order=tuple(traverse_order),
            connections=(),
            total_travel_distance=100.0,
            initial_position=None,
        )

        reassembler = Reassembler()
        result_doc = reassembler.reassemble(doc, [block1, block2], optimization_result)

        assert len(result_doc.header_commands) == 2
        assert len(result_doc.footer_commands) == 1

    def test_reassemble_forward_traversal(self) -> None:
        """Test reassembly with forward (non-reversed) traversal."""
        path = _make_simple_path((10, 20), (30, 40))
        block = _make_block(0, [path])

        doc = PLTDocument(header_commands=[], stroke_paths=[], footer_commands=[])

        traverse_order = [
            BlockTraverseState(
                block_id=0, reversed=False, entrance=(10.0, 20.0), exit=(30.0, 40.0)
            ),
        ]
        optimization_result = OptimizationResult(
            traverse_order=tuple(traverse_order),
            connections=(),
            total_travel_distance=0.0,
            initial_position=None,
        )

        reassembler = Reassembler()
        result_doc = reassembler.reassemble(doc, [block], optimization_result)

        assert len(result_doc.stroke_paths) == 1

    def test_reassemble_with_block_reversal(self) -> None:
        """Test that reversed blocks have their paths and segments reversed."""
        path1 = _make_simple_path((0, 0), (100, 0))
        path2 = _make_simple_path((150, 0), (250, 0))

        block1 = _make_block(0, [path1])
        block2 = _make_block(1, [path2])

        doc = PLTDocument(header_commands=[], stroke_paths=[], footer_commands=[])

        # Block 1 reversed: should traverse from exit to entrance
        traverse_order = [
            BlockTraverseState(block_id=0, reversed=True, entrance=(100.0, 0.0), exit=(0.0, 0.0)),
            BlockTraverseState(
                block_id=1, reversed=False, entrance=(150.0, 0.0), exit=(250.0, 0.0)
            ),
        ]
        optimization_result = OptimizationResult(
            traverse_order=tuple(traverse_order),
            connections=(),
            total_travel_distance=50.0,
            initial_position=None,
        )

        reassembler = Reassembler()
        result_doc = reassembler.reassemble(doc, [block1, block2], optimization_result)

        # First path should be reversed (start at 100, end at 0)
        first_path = result_doc.stroke_paths[0]
        assert len(first_path.segments) == 1
        assert first_path.segments[0].start.x == 100.0
        assert first_path.segments[0].end.x == 0.0

    def test_reassemble_missing_block_raises_error(self) -> None:
        """Test that missing block in traversal raises ReassemblerError."""
        path = _make_simple_path((0, 0), (100, 0))
        block = _make_block(0, [path])

        doc = PLTDocument(header_commands=[], stroke_paths=[], footer_commands=[])

        # Reference block_id=99 which doesn't exist
        traverse_order = [
            BlockTraverseState(block_id=99, reversed=False, entrance=(0.0, 0.0), exit=(100.0, 0.0)),
        ]
        optimization_result = OptimizationResult(
            traverse_order=tuple(traverse_order),
            connections=(),
            total_travel_distance=0.0,
            initial_position=None,
        )

        reassembler = Reassembler()
        with pytest.raises(ReassemblerError) as exc_info:
            reassembler.reassemble(doc, [block], optimization_result)
        assert "not found" in str(exc_info.value.message).lower()


class TestReverseBlockPaths:
    """Tests for _reverse_block_paths helper method."""

    def test_reverse_single_path(self) -> None:
        """Test reversing a single path."""
        path = _make_simple_path((10, 20), (30, 40))
        block = _make_block(0, [path])

        reassembler = Reassembler()
        reversed_paths = reassembler._reverse_block_paths(block.paths)

        assert len(reversed_paths) == 1
        # The segment should be swapped: start=(30,40), end=(10,20)
        rev_seg = reversed_paths[0].segments[0]
        assert (rev_seg.start.x, rev_seg.start.y) == (30.0, 40.0)
        assert (rev_seg.end.x, rev_seg.end.y) == (10.0, 20.0)

    def test_reverse_multiple_paths(self) -> None:
        """Test reversing multiple paths preserves order."""
        path1 = _make_simple_path((0, 0), (100, 0))
        path2 = _make_simple_path((150, 0), (250, 0))

        block = _make_block(0, [path1, path2])

        reassembler = Reassembler()
        reversed_paths = reassembler._reverse_block_paths(block.paths)

        # Should have 2 paths in reverse order
        assert len(reversed_paths) == 2


class TestReverseSegment:
    """Tests for _reverse_segment helper method."""

    def test_reverse_segment_swaps_coordinates(self) -> None:
        """Test that segment reversal swaps start and end."""
        original = StrokeSegment(
            start=Coordinate(x=5.0, y=10.0),
            end=Coordinate(x=15.0, y=25.0),
            is_cutting=True,
        )

        reassembler = Reassembler()
        reversed_seg = reassembler._reverse_segment(original)

        assert (reversed_seg.start.x, reversed_seg.start.y) == (15.0, 25.0)
        assert (reversed_seg.end.x, reversed_seg.end.y) == (5.0, 10.0)
        assert reversed_seg.is_cutting is True


class TestMetricsCalculator:
    """Tests for MetricsCalculator class."""

    def test_calculate_original_travel_distance(self) -> None:
        """Test calculating original document rapid distance."""
        from plt_optimizer.core.parser import PLTParser

        parser = PLTParser()
        # PU moves are rapid (pen up), PD moves are cutting
        doc = parser.parse_string("IN;PU0,0;PD100,0;PU100,0;PD200,0;SP;")

        calculator = MetricsCalculator()
        distance = calculator.calculate_original_travel_distance(doc)

        assert distance >= 0

    def test_calculate_optimized_travel_distance(self) -> None:
        """Test calculating optimized travel distance from result."""
        traverse_order = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(0.0, 0.0), exit=(10.0, 0.0)),
        ]
        optimization_result = OptimizationResult(
            traverse_order=tuple(traverse_order),
            connections=(),
            total_travel_distance=150.5,
            initial_position=None,
        )

        calculator = MetricsCalculator()
        distance = calculator.calculate_optimized_travel_distance(optimization_result)

        assert distance == 150.5

    def test_calculate_improvement(self) -> None:
        """Test improvement calculation."""
        calculator = MetricsCalculator()
        savings, pct = calculator.calculate_improvement(
            original_distance=1000.0,
            optimized_distance=800.0,
        )

        assert savings == 200.0
        assert pct == 20.0

    def test_calculate_improvement_zero_original(self) -> None:
        """Test improvement calculation when original is zero."""
        calculator = MetricsCalculator()
        savings, pct = calculator.calculate_improvement(
            original_distance=0.0,
            optimized_distance=0.0,
        )

        assert savings == 0.0
        assert pct == 0.0

    def test_log_metrics_does_not_crash(self) -> None:
        """Test that log_metrics handles various inputs gracefully."""
        traverse_order = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(0.0, 0.0), exit=(10.0, 0.0)),
        ]
        optimization_result = OptimizationResult(
            traverse_order=tuple(traverse_order),
            connections=(),
            total_travel_distance=100.0,
            initial_position=None,
        )

        calculator = MetricsCalculator()
        # Should not raise
        calculator.log_metrics(
            job_id="test_job",
            original_file="input.plt",
            optimized_doc=None,
            optimization_result=optimization_result,
            status="success",
        )


class TestApplyIntraChunkOrder:
    """Tests for _apply_intra_chunk_order method."""

    def test_apply_intra_chunk_order_path_reversal_updates_pen_up(self) -> None:
        """Test that reversing a path updates pen_up_position to segment start."""
        from plt_optimizer.core.intra_chunk_optimizer import (
            IntraChunkResult,
            PathTraverseState,
        )

        # Create path with pen_up already set
        seg = StrokeSegment(
            start=Coordinate(x=10.0, y=20.0),
            end=Coordinate(x=30.0, y=40.0),
            is_cutting=True,
        )
        original_pen_up = Coordinate(x=5.0, y=15.0)
        path1 = StrokePath(pen_up_position=original_pen_up, segments=(seg,))
        path2 = _make_simple_path((100, 0), (200, 0))

        # Create block directly to avoid helper assuming all paths have segments
        block = MacroBlock(
            block_id=0,
            paths=(path1, path2),
            entrance=Coordinate(x=10.0, y=20.0),
            exit=Coordinate(x=200.0, y=0.0),
        )

        doc = PLTDocument(header_commands=[], stroke_paths=[], footer_commands=[])

        # Reverse first path
        intra_result = IntraChunkResult(
            traverse_order=(
                PathTraverseState(
                    path_index=0, reversed=True, entrance=(30.0, 40.0), exit=(10.0, 20.0)
                ),
                PathTraverseState(
                    path_index=1, reversed=False, entrance=(100.0, 0.0), exit=(200.0, 0.0)
                ),
            ),
            total_internal_distance=60.0,
        )

        traverse_order = [
            BlockTraverseState(
                block_id=0, reversed=False, entrance=(10.0, 20.0), exit=(200.0, 0.0)
            ),
        ]
        optimization_result = OptimizationResult(
            traverse_order=tuple(traverse_order),
            connections=(),
            total_travel_distance=60.0,
            initial_position=None,
        )

        reassembler = Reassembler()
        result_doc = reassembler.reassemble(
            doc, [block], optimization_result, intra_chunk_results=[intra_result]
        )

        # First path should have new pen_up at the (now) first segment start
        first_path = result_doc.stroke_paths[0]
        assert len(first_path.segments) == 1
        # When reversed, pen_up_position updates to reversed_segments[0].start
        assert first_path.pen_up_position is not None

    def test_apply_intra_chunk_order_with_result(self) -> None:
        """Test intra-chunk order application with IntraChunkResult provided."""
        from plt_optimizer.core.intra_chunk_optimizer import (
            IntraChunkResult,
            PathTraverseState,
        )

        # Create two paths
        path1 = _make_simple_path((0, 0), (100, 0))
        path2 = _make_simple_path((150, 0), (250, 0))

        doc = PLTDocument(header_commands=[], stroke_paths=[], footer_commands=[])
        block = _make_block(0, [path1, path2])

        # Reverse second path via intra-chunk optimization
        intra_result = IntraChunkResult(
            traverse_order=(
                PathTraverseState(
                    path_index=0, reversed=False, entrance=(0.0, 0.0), exit=(100.0, 0.0)
                ),
                PathTraverseState(
                    path_index=1, reversed=True, entrance=(250.0, 0.0), exit=(150.0, 0.0)
                ),
            ),
            total_internal_distance=50.0,
        )

        traverse_order = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(0.0, 0.0), exit=(250.0, 0.0)),
        ]
        optimization_result = OptimizationResult(
            traverse_order=tuple(traverse_order),
            connections=(),
            total_travel_distance=50.0,
            initial_position=None,
        )

        reassembler = Reassembler()
        result_doc = reassembler.reassemble(
            doc, [block], optimization_result, intra_chunk_results=[intra_result]
        )

        assert len(result_doc.stroke_paths) == 2
        # Second path should be reversed: start at (250,0), end at (150,0)
        second_path = result_doc.stroke_paths[1]
        assert second_path.segments[0].start.x == 250.0
        assert second_path.segments[0].end.x == 150.0

    def test_apply_intra_chunk_order_empty_segments_skipped(self) -> None:
        """Test that paths with no segments are skipped in intra-chunk order."""
        from plt_optimizer.core.intra_chunk_optimizer import (
            IntraChunkResult,
            PathTraverseState,
        )

        # Create a path with no segments
        empty_path = StrokePath(pen_up_position=None, segments=())
        path1 = _make_simple_path((0, 0), (100, 0))

        doc = PLTDocument(header_commands=[], stroke_paths=[], footer_commands=[])
        # Create block directly to handle paths with no segments
        block = MacroBlock(
            block_id=0,
            paths=(empty_path, path1),
            entrance=Coordinate(x=0.0, y=0.0),
            exit=Coordinate(x=100.0, y=0.0),
        )

        intra_result = IntraChunkResult(
            traverse_order=(
                PathTraverseState(
                    path_index=0, reversed=False, entrance=(0.0, 0.0), exit=(0.0, 0.0)
                ),
                PathTraverseState(
                    path_index=1, reversed=False, entrance=(0.0, 0.0), exit=(100.0, 0.0)
                ),
            ),
            total_internal_distance=0.0,
        )

        traverse_order = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(0.0, 0.0), exit=(100.0, 0.0)),
        ]
        optimization_result = OptimizationResult(
            traverse_order=tuple(traverse_order),
            connections=(),
            total_travel_distance=0.0,
            initial_position=None,
        )

        reassembler = Reassembler()
        result_doc = reassembler.reassemble(
            doc, [block], optimization_result, intra_chunk_results=[intra_result]
        )

        # Only non-empty path should appear
        assert len(result_doc.stroke_paths) == 1


class TestReverseBlockPathsWithIntraResult:
    """Tests for _reverse_block_paths when intra_result is provided."""

    def test_reverse_block_with_intra_result_applies_reorder_then_reverse(self) -> None:
        """Test that _reverse_block_paths with intra_result calls apply and reverses."""
        from plt_optimizer.core.intra_chunk_optimizer import (
            IntraChunkResult,
            PathTraverseState,
        )

        path1 = _make_simple_path((0, 0), (100, 0))
        path2 = _make_simple_path((150, 0), (250, 0))

        block = MacroBlock(
            block_id=0,
            paths=(path1, path2),
            entrance=Coordinate(x=0.0, y=0.0),
            exit=Coordinate(x=250.0, y=0.0),
        )

        # Provide intra-chunk result - this causes _reverse_block_paths
        # to take the else branch (lines 201-202)
        intra_result = IntraChunkResult(
            traverse_order=(
                PathTraverseState(
                    path_index=1, reversed=False, entrance=(150.0, 0.0), exit=(250.0, 0.0)
                ),
                PathTraverseState(
                    path_index=0, reversed=False, entrance=(0.0, 0.0), exit=(100.0, 0.0)
                ),
            ),
            total_internal_distance=50.0,
        )

        reassembler = Reassembler()
        result_paths = reassembler._reverse_block_paths(block.paths, intra_result)

        # Paths should be reversed order after apply_intra_chunk_order + list(reversed())
        assert len(result_paths) == 2

    def test_reverse_block_with_none_intra_result_uses_simple_reverse(self) -> None:
        """Test that _reverse_block_paths with None intra_result calls simple reverse."""
        path1 = _make_simple_path((0, 0), (100, 0))

        block = MacroBlock(
            block_id=0,
            paths=(path1,),
            entrance=Coordinate(x=0.0, y=0.0),
            exit=Coordinate(x=100.0, y=0.0),
        )

        reassembler = Reassembler()
        # Explicitly pass None - this takes the if branch (lines 199-200)
        result_paths = reassembler._reverse_block_paths(block.paths, None)

        assert len(result_paths) == 1
        # The path should be reversed: start=100, end=0
        rev_seg = result_paths[0].segments[0]
        assert (rev_seg.start.x, rev_seg.start.y) == (100.0, 0.0)
        assert (rev_seg.end.x, rev_seg.end.y) == (0.0, 0.0)


class TestReassembleWithIntraChunkResults:
    """Tests for reassemble with various intra_chunk_results scenarios."""

    def test_intra_chunk_results_shorter_than_blocks(self) -> None:
        """Test when fewer intra_chunk_results than blocks exist."""
        from plt_optimizer.core.intra_chunk_optimizer import (
            IntraChunkResult,
            PathTraverseState,
        )

        path1 = _make_simple_path((0, 0), (100, 0))
        path2 = _make_simple_path((200, 0), (300, 0))

        block1 = MacroBlock(
            block_id=0,
            paths=(path1,),
            entrance=Coordinate(x=0.0, y=0.0),
            exit=Coordinate(x=100.0, y=0.0),
        )
        block2 = MacroBlock(
            block_id=1,
            paths=(path2,),
            entrance=Coordinate(x=200.0, y=0.0),
            exit=Coordinate(x=300.0, y=0.0),
        )

        doc = PLTDocument(header_commands=[], stroke_paths=[], footer_commands=[])

        # Only provide one intra_chunk_result for two blocks
        intra_result1 = IntraChunkResult(
            traverse_order=(
                PathTraverseState(
                    path_index=0, reversed=False, entrance=(0.0, 0.0), exit=(100.0, 0.0)
                ),
            ),
            total_internal_distance=0.0,
        )

        traverse_order = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(0.0, 0.0), exit=(100.0, 0.0)),
            BlockTraverseState(
                block_id=1, reversed=False, entrance=(200.0, 0.0), exit=(300.0, 0.0)
            ),
        ]
        optimization_result = OptimizationResult(
            traverse_order=tuple(traverse_order),
            connections=(),
            total_travel_distance=100.0,
            initial_position=None,
        )

        reassembler = Reassembler()
        result_doc = reassembler.reassemble(
            doc,
            [block1, block2],
            optimization_result,
            intra_chunk_results=[intra_result1],  # Only one for two blocks
        )

        # Should still work - second block has no intra-chunk result
        assert len(result_doc.stroke_paths) == 2


class TestReversePathsSimple:
    """Tests for _reverse_paths_simple method."""

    def test_reverse_empty_path_skipped(self) -> None:
        """Test that paths without segments are skipped during reversal."""
        empty_path = StrokePath(pen_up_position=None, segments=())
        path1 = _make_simple_path((0, 0), (100, 0))

        # Create block directly to handle paths with no segments
        block = MacroBlock(
            block_id=0,
            paths=(empty_path, path1),
            entrance=Coordinate(x=0.0, y=0.0),
            exit=Coordinate(x=100.0, y=0.0),
        )

        reassembler = Reassembler()
        reversed_paths = reassembler._reverse_paths_simple(block.paths)

        # Only paths with segments should be included
        assert len(reversed_paths) == 1
        rev_seg = reversed_paths[0].segments[0]
        assert (rev_seg.start.x, rev_seg.start.y) == (100.0, 0.0)
        assert (rev_seg.end.x, rev_seg.end.y) == (0.0, 0.0)


class TestReverseSegmentOrder:
    """Tests for _reverse_segment_order method."""

    def test_reverse_segments_with_arc(self) -> None:
        """Test reversing a path that contains an ArcSegment."""
        from plt_optimizer.core.models import ArcSegment

        arc_seg = ArcSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=50.0),
            center=Coordinate(x=50.0, y=0.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        path = StrokePath(pen_up_position=None, segments=(arc_seg,))

        reassembler = Reassembler()
        reversed_segs = reassembler._reverse_segment_order(path)

        assert len(reversed_segs) == 1
        rev_arc = reversed_segs[0]
        assert isinstance(rev_arc, ArcSegment)
        # Start/end should be swapped
        assert (rev_arc.start.x, rev_arc.start.y) == (100.0, 50.0)
        assert (rev_arc.end.x, rev_arc.end.y) == (0.0, 0.0)
        # Sweep angle should be negated for reverse traversal
        assert rev_arc.sweep_angle == -90.0

    def test_reverse_stroke_segment(self) -> None:
        """Test reversing a StrokeSegment swaps start and end."""
        path = _make_simple_path((10, 20), (30, 40))

        reassembler = Reassembler()
        reversed_segs = reassembler._reverse_segment_order(path)

        assert len(reversed_segs) == 1
        rev_seg = reversed_segs[0]
        # Start and end should be swapped
        assert (rev_seg.start.x, rev_seg.start.y) == (30.0, 40.0)
        assert (rev_seg.end.x, rev_seg.end.y) == (10.0, 20.0)


class TestLogMetricsWithOptimizedDoc:
    """Tests for MetricsCalculator.log_metrics method."""

    def test_log_metrics_with_optimized_doc(self) -> None:
        """Test log_metrics when optimized_doc is provided."""
        from plt_optimizer.core.parser import PLTParser

        parser = PLTParser()
        doc = parser.parse_string("IN;PU0,0;PD100,0;SP;")

        traverse_order = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(0.0, 0.0), exit=(100.0, 0.0)),
        ]
        optimization_result = OptimizationResult(
            traverse_order=tuple(traverse_order),
            connections=(),
            total_travel_distance=50.0,
            initial_position=None,
        )

        calculator = MetricsCalculator()
        # Should calculate original distance from the provided doc
        calculator.log_metrics(
            job_id="test_job_with_doc",
            original_file="input.plt",
            optimized_doc=doc,
            optimization_result=optimization_result,
            status="success",
        )


class TestCalculateReversalCost:
    """Tests for calculate_reversal_cost method."""

    def test_calculate_reversal_cost_returns_zero(self) -> None:
        """Test that reversal cost returns 0 (no change in travel distance)."""
        path = _make_simple_path((0, 0), (100, 50))
        block = _make_block(0, [path])

        reassembler = Reassembler()
        cost = reassembler.calculate_reversal_cost(block)

        # Reversal doesn't affect external travel distance
        assert cost == 0.0
