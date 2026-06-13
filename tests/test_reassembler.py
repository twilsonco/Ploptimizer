"""Tests for plt_optimizer/core/reassembler.py module.

This module reconstructs optimized PLT documents from MacroBlock sequences,
handling block reversals when needed.
"""

from __future__ import annotations

import pytest

from plt_optimizer.core.chunker import Chunker, ChunkerConfig, MacroBlock
from plt_optimizer.core.models import Coordinate, PenState, PLTDocument, StrokePath, StrokeSegment
from plt_optimizer.core.optimizer import BlockTraverseState, OptimizationResult, OptimizerEngine
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
            BlockTraverseState(block_id=0, reversed=False,
                             entrance=(0.0, 0.0), exit=(100.0, 0.0)),
            BlockTraverseState(block_id=1, reversed=False,
                             entrance=(200.0, 0.0), exit=(300.0, 0.0)),
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
            BlockTraverseState(block_id=0, reversed=False,
                             entrance=(10.0, 20.0), exit=(30.0, 40.0)),
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
            BlockTraverseState(block_id=0, reversed=True,
                             entrance=(100.0, 0.0), exit=(0.0, 0.0)),
            BlockTraverseState(block_id=1, reversed=False,
                             entrance=(150.0, 0.0), exit=(250.0, 0.0)),
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
            BlockTraverseState(block_id=99, reversed=False,
                             entrance=(0.0, 0.0), exit=(100.0, 0.0)),
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
            BlockTraverseState(block_id=0, reversed=False,
                             entrance=(0.0, 0.0), exit=(10.0, 0.0)),
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
            BlockTraverseState(block_id=0, reversed=False,
                             entrance=(0.0, 0.0), exit=(10.0, 0.0)),
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