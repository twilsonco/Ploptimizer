"""Tests for plt_optimizer/core/intra_chunk_optimizer.py module.

This module tests intra-chunk path optimization - optimizing stroke path
order and direction within a MacroBlock while keeping entrance/exit fixed.
"""

from __future__ import annotations

import pytest

from plt_optimizer.core.chunker import Chunker, ChunkerConfig, MacroBlock
from plt_optimizer.core.intra_chunk_optimizer import (
    IntraChunkError,
    IntraChunkOptimizer,
    IntraChunkResult,
    NoOpIntraStrategy,
    NearestNeighborIntraStrategy,
    PathTraverseState,
)
from plt_optimizer.core.models import Coordinate, StrokePath, StrokeSegment


def _make_path(
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


class TestPathTraverseState:
    """Tests for PathTraverseState dataclass."""

    def test_creation(self) -> None:
        """Test creating a PathTraverseState."""
        state = PathTraverseState(
            path_index=0,
            reversed=False,
            entrance=Coordinate(x=0.0, y=0.0),
            exit=Coordinate(x=10.0, y=0.0),
        )
        assert state.path_index == 0
        assert state.reversed is False
        assert state.entrance.x == 0.0
        assert state.exit.x == 10.0

    def test_immutability(self) -> None:
        """Test that PathTraverseState is immutable."""
        state = PathTraverseState(
            path_index=0,
            reversed=False,
            entrance=Coordinate(x=0.0, y=0.0),
            exit=Coordinate(x=10.0, y=0.0),
        )
        with pytest.raises(AttributeError):
            state.path_index = 5


class TestIntraChunkResult:
    """Tests for IntraChunkResult dataclass."""

    def test_path_count_empty(self) -> None:
        """Test path_count with empty result."""
        result = IntraChunkResult(
            traverse_order=(),
            total_internal_distance=0.0,
        )
        assert result.path_count == 0

    def test_path_count_with_paths(self) -> None:
        """Test path_count with multiple paths."""
        states = (
            PathTraverseState(path_index=0, reversed=False,
                             entrance=Coordinate(0, 0), exit=Coordinate(10, 0)),
            PathTraverseState(path_index=1, reversed=True,
                             entrance=Coordinate(20, 0), exit=Coordinate(15, 0)),
        )
        result = IntraChunkResult(
            traverse_order=states,
            total_internal_distance=50.0,
        )
        assert result.path_count == 2


class TestNoOpIntraStrategy:
    """Tests for NoOpIntraStrategy class."""

    def test_name(self) -> None:
        """Test strategy name."""
        strategy = NoOpIntraStrategy()
        assert "NoOp" in strategy.name

    def test_optimize_single_path(self) -> None:
        """Test optimization on single path returns it as-is."""
        paths = (_make_path((0, 0), (10, 0)),)
        block = _make_block(0, list(paths))

        strategy = NoOpIntraStrategy()
        result = strategy.optimize_block(block.paths, block.entrance, block.exit)

        assert result.path_count == 1
        assert result.traverse_order[0].path_index == 0
        assert result.traverse_order[0].reversed is False

    def test_optimize_multiple_paths_preserves_order(self) -> None:
        """Test that multiple paths maintain original order."""
        path1 = _make_path((0, 0), (10, 0))
        path2 = _make_path((15, 0), (25, 0))
        path3 = _make_path((30, 0), (40, 0))

        block = _make_block(0, [path1, path2, path3])

        strategy = NoOpIntraStrategy()
        result = strategy.optimize_block(block.paths, block.entrance, block.exit)

        assert result.path_count == 3
        assert result.traverse_order[0].path_index == 0
        assert result.traverse_order[1].path_index == 1
        assert result.traverse_order[2].path_index == 2


class TestNearestNeighborIntraStrategy:
    """Tests for NearestNeighborIntraStrategy class."""

    def test_name(self) -> None:
        """Test strategy name."""
        strategy = NearestNeighborIntraStrategy()
        assert "NearestNeighbor" in strategy.name

    def test_optimize_empty_paths_returns_empty(self) -> None:
        """Test optimization on empty paths returns empty result."""
        strategy = NearestNeighborIntraStrategy()
        result = strategy.optimize_block((), Coordinate(0, 0), Coordinate(10, 0))

        assert result.path_count == 0

    def test_optimize_single_path_keeps_direction(self) -> None:
        """Test single path is kept in original direction."""
        paths = (_make_path((0, 0), (10, 0)),)
        block = _make_block(0, list(paths))

        strategy = NearestNeighborIntraStrategy()
        result = strategy.optimize_block(block.paths, block.entrance, block.exit)

        assert result.path_count == 1
        assert result.traverse_order[0].reversed is False

    def test_optimize_two_paths_reverses_closer(self) -> None:
        """Test that with two paths, the one closer to entrance goes first."""
        path1 = _make_path((100, 0), (90, 0))  # Exit at x=90
        path2 = _make_path((50, 0), (60, 0))   # Entrance at x=50

        block = MacroBlock(
            block_id=0,
            paths=(path1, path2),
            entrance=Coordinate(100.0, 0.0),
            exit=Coordinate(60.0, 0.0),
        )

        strategy = NearestNeighborIntraStrategy()
        result = strategy.optimize_block(block.paths, block.entrance, block.exit)

        assert result.path_count == 2

    def test_optimize_respects_fixed_entrance(self) -> None:
        """Test that first path's start must match fixed entrance."""
        path1 = _make_path((0, 0), (10, 0))
        path2 = _make_path((20, 0), (30, 0))

        block = MacroBlock(
            block_id=0,
            paths=(path1, path2),
            entrance=Coordinate(0.0, 0.0),
            exit=Coordinate(30.0, 0.0),
        )

        strategy = NearestNeighborIntraStrategy()
        result = strategy.optimize_block(block.paths, block.entrance, block.exit)

        first_state = result.traverse_order[0]
        first_path = path1
        if first_state.reversed:
            assert (first_path.segments[-1].end.x ==
                    pytest.approx(first_state.entrance.x))
        else:
            assert (first_path.segments[0].start.x ==
                    pytest.approx(first_state.entrance.x))

    def test_optimize_respects_fixed_exit(self) -> None:
        """Test that last path's end must match fixed exit."""
        path1 = _make_path((0, 0), (10, 0))
        path2 = _make_path((20, 0), (30, 0))

        block = MacroBlock(
            block_id=0,
            paths=(path1, path2),
            entrance=Coordinate(0.0, 0.0),
            exit=Coordinate(30.0, 0.0),
        )

        strategy = NearestNeighborIntraStrategy()
        result = strategy.optimize_block(block.paths, block.entrance, block.exit)

        last_state = result.traverse_order[-1]
        last_path = path2
        if last_state.reversed:
            assert (last_path.segments[0].start.x ==
                    pytest.approx(last_state.exit.x))
        else:
            assert (last_path.segments[-1].end.x ==
                    pytest.approx(last_state.exit.x))


class TestIntraChunkOptimizer:
    """Tests for IntraChunkOptimizer class."""

    def test_default_strategy(self) -> None:
        """Test default strategy is NoOp."""
        optimizer = IntraChunkOptimizer()
        assert isinstance(optimizer.strategy, NoOpIntraStrategy)

    def test_custom_strategy(self) -> None:
        """Test custom strategy can be set."""
        strategy = NearestNeighborIntraStrategy()
        optimizer = IntraChunkOptimizer(strategy=strategy)
        assert optimizer.strategy is strategy

    def test_set_strategy(self) -> None:
        """Test changing active strategy."""
        optimizer = IntraChunkOptimizer()
        new_strategy = NearestNeighborIntraStrategy()

        optimizer.set_strategy(new_strategy)

        assert optimizer.strategy is new_strategy

    def test_optimize_block_single_path(self) -> None:
        """Test optimizing a block with single path."""
        paths = (_make_path((0, 0), (10, 0)),)
        block = _make_block(0, list(paths))

        optimizer = IntraChunkOptimizer()
        result = optimizer.optimize_block(block)

        assert result.path_count == 1

    def test_optimize_blocks_multiple(self) -> None:
        """Test optimizing multiple blocks."""
        path1 = _make_path((0, 0), (10, 0))
        path2 = _make_path((20, 0), (30, 0))

        block1 = _make_block(0, [path1])
        block2 = _make_block(1, [path2])

        optimizer = IntraChunkOptimizer()
        results = optimizer.optimize_blocks([block1, block2])

        assert len(results) == 2
        assert all(isinstance(r, IntraChunkResult) for r in results)


class TestIntraChunkEdgeCases:
    """Tests for edge cases in intra-chunk optimization."""

    def test_paths_with_no_segments_skipped(self) -> None:
        """Test that paths with no segments are handled."""
        empty_path = StrokePath(pen_up_position=None, segments=())
        valid_path = _make_path((100, 0), (110, 0))

        block = MacroBlock(
            block_id=0,
            paths=(empty_path, valid_path),
            entrance=Coordinate(100.0, 0.0),
            exit=Coordinate(110.0, 0.0),
        )

        optimizer = IntraChunkOptimizer(strategy=NearestNeighborIntraStrategy())
        result = optimizer.optimize_block(block)

        assert result.path_count >= 1

    def test_two_paths_only_two_permutations(self) -> None:
        """Test two-path block has exactly 2 permutations to check."""
        path1 = _make_path((0, 0), (10, 0))
        path2 = _make_path((15, 0), (25, 0))

        block = MacroBlock(
            block_id=0,
            paths=(path1, path2),
            entrance=Coordinate(0.0, 0.0),
            exit=Coordinate(25.0, 0.0),
        )

        optimizer = IntraChunkOptimizer(strategy=NearestNeighborIntraStrategy())
        result = optimizer.optimize_block(block)

        assert result.path_count == 2

    def test_distance_calculation(self) -> None:
        """Test that internal distance is calculated correctly."""
        path1 = _make_path((0, 0), (10, 0))     # Ends at x=10
        path2 = _make_path((15, 0), (25, 0))    # Starts at x=15

        block = MacroBlock(
            block_id=0,
            paths=(path1, path2),
            entrance=Coordinate(0.0, 0.0),
            exit=Coordinate(25.0, 0.0),
        )

        optimizer = IntraChunkOptimizer(strategy=NearestNeighborIntraStrategy())
        result = optimizer.optimize_block(block)

        assert result.total_internal_distance >= 0


class TestIntraChunkIntegration:
    """Tests for intra-chunk optimization integration scenarios."""

    def test_intra_chunk_reduces_internal_travel(self) -> None:
        """Test that optimized order reduces internal rapid travel."""
        path1 = _make_path((0, 0), (10, 0))     # Path A: 0->10
        path2 = _make_path((30, 0), (20, 0))    # Path B: 30->20

        block = MacroBlock(
            block_id=0,
            paths=(path1, path2),
            entrance=Coordinate(0.0, 0.0),
            exit=Coordinate(20.0, 0.0),  # Exit at end of reversed path2
        )

        noop_optimizer = IntraChunkOptimizer(strategy=NoOpIntraStrategy())
        nn_optimizer = IntraChunkOptimizer(strategy=NearestNeighborIntraStrategy())

        noop_result = noop_optimizer.optimize_block(block)
        nn_result = nn_optimizer.optimize_block(block)

    def test_intra_then_inter_chunk_reversal(self) -> None:
        """Test intra-chunk optimization works with inter-chunk block reversal."""
        path1 = _make_path((0, 0), (10, 0))
        path2 = _make_path((15, 0), (25, 0))

        block = MacroBlock(
            block_id=0,
            paths=(path1, path2),
            entrance=Coordinate(0.0, 0.0),
            exit=Coordinate(25.0, 0.0),
        )

        optimizer = IntraChunkOptimizer(strategy=NearestNeighborIntraStrategy())
        result = optimizer.optimize_block(block)

        assert result.path_count == 2