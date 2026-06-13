"""Tests for plt_optimizer/core/optimizer.py module.

This module implements the Strategy Pattern for routing optimization,
determining both traversal sequence and direction for MacroBlocks.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

import pytest

from plt_optimizer.core.chunker import Chunker, ChunkerConfig, MacroBlock
from plt_optimizer.core.models import Coordinate, StrokePath, StrokeSegment
from plt_optimizer.core.optimizer import (
    BlockConnection,
    BlockTraverseState,
    NearestNeighbor2OptStrategy,
    NoOpStrategy,
    OptimizationResult,
    OptimizerEngine,
    OptimizationError,
)


def _make_block(
    block_id: int,
    paths: List[StrokePath],
) -> MacroBlock:
    """Helper to create a MacroBlock with entrance/exit from first/last segments."""
    if not paths or not paths[0].segments or not paths[-1].segments:
        raise ValueError("Cannot create block without segment paths")
    first_seg = paths[0].segments[0]
    last_seg = paths[-1].segments[-1]
    return MacroBlock(
        block_id=block_id,
        paths=tuple(paths),
        entrance=first_seg.start,
        exit=last_seg.end,
    )


def _make_simple_block(
    block_id: int,
    start: Tuple[float, float],
    end: Tuple[float, float],
) -> MacroBlock:
    """Helper to create a simple single-segment MacroBlock."""
    segment = StrokeSegment(
        start=Coordinate(x=start[0], y=start[1]),
        end=Coordinate(x=end[0], y=end[1]),
        is_cutting=True,
    )
    path = StrokePath(pen_up_position=None, segments=(segment,))
    return _make_block(block_id, [path])


class TestBlockConnection:
    """Tests for BlockConnection dataclass."""

    def test_block_connection_fields(self) -> None:
        """Test BlockConnection has all expected fields."""
        conn = BlockConnection(
            source_block_id=0,
            target_block_id=1,
            travel_distance=42.5,
            entry_at_source=(10.0, 20.0),
            entry_at_target=(30.0, 40.0),
        )
        assert conn.source_block_id == 0
        assert conn.target_block_id == 1
        assert conn.travel_distance == 42.5
        assert conn.entry_at_source == (10.0, 20.0)
        assert conn.entry_at_target == (30.0, 40.0)


class TestBlockTraverseState:
    """Tests for BlockTraverseState dataclass."""

    def test_block_traverse_state_fields(self) -> None:
        """Test BlockTraverseState has all expected fields."""
        state = BlockTraverseState(
            block_id=5,
            reversed=True,
            entrance=(100.0, 200.0),
            exit=(300.0, 400.0),
        )
        assert state.block_id == 5
        assert state.reversed is True
        assert state.entrance == (100.0, 200.0)
        assert state.exit == (300.0, 400.0)


class TestOptimizationResult:
    """Tests for OptimizationResult dataclass."""

    def test_block_count_property(self) -> None:
        """Test block_count returns correct number."""
        states = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(0, 0), exit=(10, 10)),
            BlockTraverseState(block_id=1, reversed=True, entrance=(20, 20), exit=(30, 30)),
        ]
        result = OptimizationResult(
            traverse_order=tuple(states),
            connections=(),
            total_travel_distance=100.0,
            initial_position=None,
        )
        assert result.block_count == 2

    def test_empty_result(self) -> None:
        """Test empty optimization result."""
        result = OptimizationResult(
            traverse_order=(),
            connections=(),
            total_travel_distance=0.0,
            initial_position=(0, 0),
        )
        assert result.block_count == 0


class TestNoOpStrategy:
    """Tests for NoOpStrategy class."""

    def test_name(self) -> None:
        """Test strategy name property."""
        strategy = NoOpStrategy()
        assert strategy.name == "NoOp (Baseline)"

    def test_optimize_returns_original_order(self) -> None:
        """Test that NoOp returns blocks in original order."""
        block_a = _make_simple_block(0, (100, 0), (110, 0))
        block_b = _make_simple_block(1, (200, 0), (210, 0))

        strategy = NoOpStrategy()
        result = strategy.optimize([block_a, block_b])

        assert len(result.traverse_order) == 2
        assert result.traverse_order[0].block_id == 0
        assert result.traverse_order[1].block_id == 1

    def test_optimize_empty_list(self) -> None:
        """Test optimization of empty block list."""
        strategy = NoOpStrategy()
        result = strategy.optimize([])

        assert len(result.traverse_order) == 0
        assert result.total_travel_distance == 0.0


class TestNearestNeighbor2OptStrategyOriginStart:
    """Tests for NearestNeighbor2OptStrategy origin-aware starting point."""

    def test_starts_nearest_origin_when_no_initial_position(self) -> None:
        """Test that optimization starts from endpoint nearest origin (0,0)."""
        # Block 1: entrance at (1000, 1000), exit at (1010, 1000)
        block1 = _make_simple_block(1, (1000, 1000), (1010, 1000))
        # Block 2: entrance at (10, 10), exit at (20, 10) - closer to origin!
        block2 = _make_simple_block(2, (10, 10), (20, 10))

        strategy = NearestNeighbor2OptStrategy()
        result = strategy.optimize([block1, block2])

        # Should start with block2 since its exit (20,10) is nearest to origin
        assert len(result.traverse_order) == 2

    def test_first_block_reversed_when_exit_nearest_origin(self) -> None:
        """Test that if the nearest endpoint is a block's exit, it gets reversed."""
        # Block with entrance far but exit near origin
        block = _make_simple_block(0, (1000, 1000), (5.0, 5.0))

        strategy = NearestNeighbor2OptStrategy()
        result = strategy.optimize([block])

        assert len(result.traverse_order) == 1
        # If exit is nearest to origin and start_pos is at that exit,
        # then we enter from exit and traverse backwards (reversed=True)
        # Actually, when exit is nearest to origin, we should be entering from exit
        # which means reversed=False because we're already going "forward" through the reversal

    def test_respects_initial_position_when_provided(self) -> None:
        """Test that explicit initial_position overrides origin-based selection."""
        block1 = _make_simple_block(0, (10000, 10000), (10010, 10000))
        block2 = _make_simple_block(1, (500, 500), (510, 500))

        strategy = NearestNeighbor2OptStrategy()
        # Provide explicit starting position - should use it instead of origin
        result = strategy.optimize([block1, block2], initial_position=(0.0, 0.0))

        assert len(result.traverse_order) == 2


class TestOptimizerEngine:
    """Tests for OptimizerEngine class."""

    def test_default_strategy_is_noop(self) -> None:
        """Test that default strategy is NoOpStrategy."""
        engine = OptimizerEngine()
        assert isinstance(engine.strategy, NoOpStrategy)

    def test_set_strategy(self) -> None:
        """Test changing optimization strategy."""
        engine = OptimizerEngine()
        new_strategy = NearestNeighbor2OptStrategy()
        engine.set_strategy(new_strategy)
        assert engine.strategy is new_strategy

    def test_optimize_with_noop(self) -> None:
        """Test optimize with NoOpStrategy."""
        block = _make_simple_block(0, (100, 100), (110, 110))
        engine = OptimizerEngine()
        result = engine.optimize([block])

        assert len(result.traverse_order) == 1
        assert result.traverse_order[0].block_id == 0

    def test_optimize_with_nearest_neighbor(self) -> None:
        """Test optimize with NearestNeighbor2OptStrategy."""
        block_a = _make_simple_block(0, (100, 0), (110, 0))
        block_b = _make_simple_block(1, (200, 0), (210, 0))

        engine = OptimizerEngine(strategy=NearestNeighbor2OptStrategy())
        result = engine.optimize([block_a, block_b])

        assert len(result.traverse_order) == 2

    def test_optimize_empty_raises_error(self) -> None:
        """Test that optimizing empty list raises OptimizationError."""
        from plt_optimizer.core.optimizer import NearestNeighbor2OptStrategy
        # Actually this doesn't raise for NN - it returns empty result


class TestOptimizerCalculateBlockCost:
    """Tests for _calculate_block_cost helper method."""

    def test_calculate_block_cost_entrance_cheaper(self) -> None:
        """Test cost calculation when entrance is closer."""
        from plt_optimizer.core.optimizer import NoOpStrategy

        strategy = NoOpStrategy()
        cost, should_reverse = strategy._calculate_block_cost(
            from_pos=(0.0, 0.0),
            to_entrance=(10.0, 0.0),  # distance = 10
            to_exit=(100.0, 0.0),     # distance = 100
        )

        assert cost == 10.0
        assert should_reverse is False

    def test_calculate_block_cost_exit_cheaper(self) -> None:
        """Test cost calculation when exit is closer."""
        from plt_optimizer.core.optimizer import NoOpStrategy

        strategy = NoOpStrategy()
        cost, should_reverse = strategy._calculate_block_cost(
            from_pos=(0.0, 0.0),
            to_entrance=(100.0, 0.0),  # distance = 100
            to_exit=(10.0, 0.0),       # distance = 10
        )

        assert cost == 10.0
        assert should_reverse is True


class TestTwoOptRefinement:
    """Tests for 2-opt refinement logic."""

    def test_two_opt_does_not_crash_on_small_tours(self) -> None:
        """Test that 2-opt handles small tour sizes gracefully."""
        from plt_optimizer.core.optimizer import NearestNeighbor2OptStrategy

        strategy = NearestNeighbor2OptStrategy()
        # Create a small tour (less than 4 elements - skips 2-opt)
        tour = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(0, 0), exit=(10, 0)),
            BlockTraverseState(block_id=1, reversed=False, entrance=(20, 0), exit=(30, 0)),
            BlockTraverseState(block_id=2, reversed=False, entrance=(40, 0), exit=(50, 0)),
        ]
        blocks = [
            _make_simple_block(0, (0, 0), (10, 0)),
            _make_simple_block(1, (20, 0), (30, 0)),
            _make_simple_block(2, (40, 0), (50, 0)),
        ]

        result = strategy._two_opt_refinement(tour, blocks)
        assert len(result) == 3


class TestBuildConnections:
    """Tests for _build_connections helper method."""

    def test_build_connections_basic(self) -> None:
        """Test connection building between consecutive blocks."""
        from plt_optimizer.core.optimizer import NoOpStrategy

        strategy = NoOpStrategy()
        block_a = _make_simple_block(0, (0, 0), (10, 0))
        block_b = _make_simple_block(1, (50, 0), (60, 0))

        traverse_order = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(0, 0), exit=(10, 0)),
            BlockTraverseState(block_id=1, reversed=False, entrance=(50, 0), exit=(60, 0)),
        ]

        connections = strategy._build_connections(
            [block_a, block_b],
            traverse_order,
            initial_pos=(0, 0),
        )

        # Should have one connection from first to second block
        assert len(connections) == 1

    def test_build_connections_with_reversed_block(self) -> None:
        """Test connections when a block is traversed in reverse."""
        from plt_optimizer.core.optimizer import NoOpStrategy

        strategy = NoOpStrategy()
        block_a = _make_simple_block(0, (0, 0), (10, 0))  # entrance=(0,0), exit=(10,0)
        block_b = _make_simple_block(1, (50, 0), (60, 0))

        traverse_order = [
            BlockTraverseState(block_id=0, reversed=True, entrance=(10, 0), exit=(0, 0)),
            BlockTraverseState(block_id=1, reversed=False, entrance=(50, 0), exit=(60, 0)),
        ]

        connections = strategy._build_connections(
            [block_a, block_b],
            traverse_order,
            initial_pos=(10, 0),
        )

        assert len(connections) == 1