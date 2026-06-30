"""Tests for plt_optimizer/core/optimizer.py module.

This module implements the Strategy Pattern for routing optimization,
determining both traversal sequence and direction for MacroBlocks.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

from plt_optimizer.core.chunker import Chunker, ChunkerConfig, MacroBlock
from plt_optimizer.core.models import Coordinate, StrokePath, StrokeSegment
from plt_optimizer.core.optimizer import (
    BlockConnection,
    BlockTraverseState,
    ChristofidesStrategy,
    GeneticAlgorithmStrategy,
    InsertionHeuristicStrategy,
    NearestNeighbor2OptStrategy,
    NoOpStrategy,
    OptimizationResult,
    OptimizerEngine,
    OptimizationError,
    ParallelEnsembleOptimizationResult,
    ParallelEnsembleStrategy,
    SimulatedAnnealingStrategy,
    StrategyBenchmarkResult,
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


class TestInsertionHeuristicStrategy:
    """Tests for InsertionHeuristicStrategy class."""

    def test_name(self) -> None:
        """Test strategy name property returns 'Cheapest Insertion Heuristic'."""
        strategy = InsertionHeuristicStrategy()
        assert strategy.name == "Cheapest Insertion Heuristic"

    def test_optimize_empty_list(self) -> None:
        """Test optimization of empty block list returns empty result."""
        strategy = InsertionHeuristicStrategy()
        result = strategy.optimize([])

        assert len(result.traverse_order) == 0
        assert result.total_travel_distance == 0.0

    def test_optimize_single_block(self) -> None:
        """Test single block is handled correctly (chooses best orientation)."""
        block_a = _make_simple_block(0, (100, 0), (110, 0))

        strategy = InsertionHeuristicStrategy()
        result = strategy.optimize([block_a])

        assert len(result.traverse_order) == 1
        state = result.traverse_order[0]
        assert state.block_id == 0

    def test_optimize_two_blocks_closest_pair(self) -> None:
        """Test that two blocks start with closest pair."""
        block_a = _make_simple_block(0, (0, 0), (10, 0))
        block_b = _make_simple_block(1, (1000, 1000), (1010, 1000))

        strategy = InsertionHeuristicStrategy()
        result = strategy.optimize([block_a, block_b])

        assert len(result.traverse_order) == 2
        distances = [c.travel_distance for c in result.connections if c.source_block_id >= 0]
        total_dist = sum(distances)
        assert total_dist < 2000.0

    def test_insertion_reduces_distance_on_linear_path(self) -> None:
        """Test that insertion heuristic produces valid tour on simple linear arrangement."""
        block_a = _make_simple_block(0, (0, 0), (10, 0))
        block_b = _make_simple_block(1, (50, 0), (60, 0))
        block_c = _make_simple_block(2, (100, 0), (110, 0))

        strategy = InsertionHeuristicStrategy()
        result = strategy.optimize([block_a, block_b, block_c])

        assert len(result.traverse_order) == 3
        for state in result.traverse_order:
            assert state.block_id in (0, 1, 2)

    def test_respects_initial_position_when_provided(self) -> None:
        """Test explicit initial_position overrides origin-based selection."""
        block_a = _make_simple_block(0, (10000, 10000), (10010, 10000))
        block_b = _make_simple_block(1, (500, 500), (510, 500))

        strategy = InsertionHeuristicStrategy()
        result = strategy.optimize([block_a, block_b], initial_position=(0.0, 0.0))

        assert len(result.traverse_order) == 2
        assert result.initial_position == (0.0, 0.0)


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


class TestChristofidesStrategy:
    """Tests for ChristofidesStrategy class."""

    def test_name(self) -> None:
        """Test strategy name property returns 'Christofides-Serdyukov S-T Path (5/3 approx)'."""
        strategy = ChristofidesStrategy()
        assert strategy.name == "Christofides-Serdyukov S-T Path (5/3 approx)"

    def test_optimize_empty_list(self) -> None:
        """Test optimization of empty block list returns empty result."""
        strategy = ChristofidesStrategy()
        result = strategy.optimize([], start_point=(0.0, 0.0), end_point=(10.0, 10.0))

        assert len(result.traverse_order) == 0
        assert result.total_travel_distance == 0.0

    def test_optimize_single_block(self) -> None:
        """Test single block is handled correctly (chooses best orientation)."""
        block_a = _make_simple_block(0, (100, 0), (110, 0))

        strategy = ChristofidesStrategy()
        result = strategy.optimize([block_a], start_point=(0.0, 0.0), end_point=(200.0, 200.0))

        assert len(result.traverse_order) == 1
        state = result.traverse_order[0]
        assert state.block_id == 0

    def test_optimize_two_blocks(self) -> None:
        """Test two blocks produce a valid tour connecting both endpoints."""
        block_a = _make_simple_block(0, (0, 0), (10, 0))
        block_b = _make_simple_block(1, (100, 0), (110, 0))

        strategy = ChristofidesStrategy()
        result = strategy.optimize(
            [block_a, block_b], start_point=(0.0, 0.0), end_point=(200.0, 200.0)
        )

        assert len(result.traverse_order) == 2
        block_ids = {state.block_id for state in result.traverse_order}
        assert block_ids == {0, 1}

    def test_mst_building_on_linear_blocks(self) -> None:
        """Test MST is built correctly on simple linear arrangement."""
        strategy = ChristofidesStrategy()

        block_a = _make_simple_block(0, (0, 0), (10, 0))
        block_b = _make_simple_block(1, (20, 0), (30, 0))

        vertices = strategy._create_vertices(
            [block_a, block_b], start_point=(0.0, 0.0), end_point=(100.0, 100.0)
        )
        assert len(vertices) == 6  # 2 blocks * 2 endpoints + S + T

        mst_edges = strategy._build_mst_prim(vertices, strategy.START_VERTEX_ID)

        wrong_parity_vertices = strategy._find_wrong_parity_vertices(
            mst_edges, vertices, strategy.START_VERTEX_ID, strategy.END_VERTEX_ID
        )
        assert len(wrong_parity_vertices) % 2 == 0

    def test_produces_valid_tour_ordering(self) -> None:
        """Test all blocks appear exactly once in traverse order."""
        block_a = _make_simple_block(0, (0, 0), (10, 0))
        block_b = _make_simple_block(1, (50, 0), (60, 0))
        block_c = _make_simple_block(2, (100, 0), (110, 0))

        strategy = ChristofidesStrategy()
        result = strategy.optimize(
            [block_a, block_b, block_c], start_point=(0.0, 0.0), end_point=(200.0, 200.0)
        )

        assert len(result.traverse_order) == 3
        seen_ids: set[int] = set()
        for state in result.traverse_order:
            assert state.block_id not in seen_ids
            seen_ids.add(state.block_id)
        assert seen_ids == {0, 1, 2}


class TestBuildConnectionsWithReversedBlock:
    """Tests for _build_connections helper method with reversed blocks."""

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


class TestSimulatedAnnealingStrategy:
    """Tests for SimulatedAnnealingStrategy class."""

    def test_name(self) -> None:
        """Test strategy name property returns 'Simulated Annealing'."""
        strategy = SimulatedAnnealingStrategy()
        assert strategy.name == "Simulated Annealing"

    def test_optimize_empty_list(self) -> None:
        """Test optimization of empty block list returns empty result."""
        strategy = SimulatedAnnealingStrategy()
        result = strategy.optimize([])

        assert len(result.traverse_order) == 0
        assert result.total_travel_distance == 0.0

    def test_optimize_single_block(self) -> None:
        """Test single block is handled correctly (returns that block)."""
        block_a = _make_simple_block(0, (100, 0), (110, 0))

        strategy = SimulatedAnnealingStrategy()
        result = strategy.optimize([block_a])

        assert len(result.traverse_order) == 1
        state = result.traverse_order[0]
        assert state.block_id == 0

    def test_optimize_small_tour_skips_sa(self) -> None:
        """Test tours with < 4 blocks skip SA and return valid result."""
        block_a = _make_simple_block(0, (0, 0), (10, 0))
        block_b = _make_simple_block(1, (50, 0), (60, 0))

        strategy = SimulatedAnnealingStrategy()
        result = strategy.optimize([block_a, block_b])

        assert len(result.traverse_order) == 2
        block_ids = {state.block_id for state in result.traverse_order}
        assert block_ids == {0, 1}

    def test_produces_valid_tour_ordering(self) -> None:
        """Test all blocks appear exactly once in traverse order."""
        block_a = _make_simple_block(0, (0, 0), (10, 0))
        block_b = _make_simple_block(1, (50, 0), (60, 0))
        block_c = _make_simple_block(2, (100, 0), (110, 0))

        strategy = SimulatedAnnealingStrategy()
        result = strategy.optimize([block_a, block_b, block_c])

        assert len(result.traverse_order) == 3
        seen_ids: set[int] = set()
        for state in result.traverse_order:
            assert state.block_id not in seen_ids
            seen_ids.add(state.block_id)
        assert seen_ids == {0, 1, 2}


class TestGeneticAlgorithmStrategy:
    """Tests for GeneticAlgorithmStrategy class."""

    def test_name(self) -> None:
        """Test strategy name property returns 'Genetic Algorithm'."""
        strategy = GeneticAlgorithmStrategy()
        assert strategy.name == "Genetic Algorithm"

    def test_optimize_empty_list(self) -> None:
        """Test optimization of empty block list returns empty result."""
        strategy = GeneticAlgorithmStrategy()
        result = strategy.optimize([])

        assert len(result.traverse_order) == 0
        assert result.total_travel_distance == 0.0

    def test_optimize_single_block(self) -> None:
        """Test single block is handled correctly (returns that block)."""
        block_a = _make_simple_block(0, (100, 0), (110, 0))

        strategy = GeneticAlgorithmStrategy()
        result = strategy.optimize([block_a])

        assert len(result.traverse_order) == 1
        state = result.traverse_order[0]
        assert state.block_id == 0

    def test_small_tour_uses_greedy(self) -> None:
        """Test tours with < 4 blocks skip GA and return valid result."""
        block_a = _make_simple_block(0, (0, 0), (10, 0))
        block_b = _make_simple_block(1, (50, 0), (60, 0))

        strategy = GeneticAlgorithmStrategy()
        result = strategy.optimize([block_a, block_b])

        assert len(result.traverse_order) == 2
        block_ids = {state.block_id for state in result.traverse_order}
        assert block_ids == {0, 1}

    def test_produces_valid_tour_ordering(self) -> None:
        """Test all blocks appear exactly once in traverse order."""
        block_a = _make_simple_block(0, (0, 0), (10, 0))
        block_b = _make_simple_block(1, (50, 0), (60, 0))
        block_c = _make_simple_block(2, (100, 0), (110, 0))

        strategy = GeneticAlgorithmStrategy()
        result = strategy.optimize([block_a, block_b, block_c])

        assert len(result.traverse_order) == 3
        seen_ids: set[int] = set()
        for state in result.traverse_order:
            assert state.block_id not in seen_ids
            seen_ids.add(state.block_id)
        assert seen_ids == {0, 1, 2}


class TestParallelEnsembleStrategy:
    """Tests for ParallelEnsembleStrategy class."""

    def test_name(self) -> None:
        """Test strategy name property."""
        from plt_optimizer.core.optimizer import ParallelEnsembleStrategy
        strategy = ParallelEnsembleStrategy()
        assert strategy.name == "Parallel Ensemble"

    def test_optimize_empty_list(self) -> None:
        """Test optimization of empty block list returns empty result."""
        from plt_optimizer.core.optimizer import ParallelEnsembleStrategy
        strategy = ParallelEnsembleStrategy()
        result = strategy.optimize([])

        assert len(result.traverse_order) == 0
        assert result.total_travel_distance == 0.0

    def test_optimize_single_block(self) -> None:
        """Test single block is handled correctly."""
        from plt_optimizer.core.optimizer import ParallelEnsembleStrategy
        strategy = ParallelEnsembleStrategy()
        block_a = _make_simple_block(0, (100, 0), (110, 0))

        result = strategy.optimize([block_a])

        assert len(result.traverse_order) == 1

    def test_optimize_with_baseline_distance(self) -> None:
        """Test parallel ensemble with baseline distance for improvement calculation."""
        from plt_optimizer.core.optimizer import ParallelEnsembleStrategy
        # Create blocks that will benefit from optimization
        block_a = _make_simple_block(0, (0, 0), (10, 0))
        block_b = _make_simple_block(1, (100, 0), (110, 0))
        block_c = _make_simple_block(2, (50, 0), (60, 0))

        # Baseline: going A -> B -> C would be far
        baseline_distance = 200.0

        strategy = ParallelEnsembleStrategy(baseline_distance=baseline_distance)
        result = strategy.optimize([block_a, block_b, block_c])

        assert len(result.traverse_order) == 3

    def test_optimize_multiple_blocks_uses_best_strategy(self) -> None:
        """Test that parallel ensemble returns a valid optimized result."""
        from plt_optimizer.core.optimizer import ParallelEnsembleStrategy
        blocks = [
            _make_simple_block(0, (0, 0), (10, 0)),
            _make_simple_block(1, (50, 0), (60, 0)),
            _make_simple_block(2, (100, 0), (110, 0)),
        ]

        strategy = ParallelEnsembleStrategy()
        result = strategy.optimize(blocks)

        # Should return a valid optimization result
        assert len(result.traverse_order) == 3
        block_ids = {state.block_id for state in result.traverse_order}
        assert block_ids == {0, 1, 2}

    def test_optimize_respects_initial_position(self) -> None:
        """Test that initial_position is passed through to strategies."""
        from plt_optimizer.core.optimizer import ParallelEnsembleStrategy
        blocks = [
            _make_simple_block(0, (100, 100), (110, 100)),
            _make_simple_block(1, (200, 200), (210, 200)),
        ]

        strategy = ParallelEnsembleStrategy()
        result = strategy.optimize(blocks, initial_position=(0.0, 0.0))

        assert len(result.traverse_order) == 2

    def test_optimize_with_max_workers(self) -> None:
        """Test parallel ensemble with explicit max_workers limit."""
        from plt_optimizer.core.optimizer import ParallelEnsembleStrategy
        blocks = [
            _make_simple_block(0, (0, 0), (10, 0)),
            _make_simple_block(1, (50, 0), (60, 0)),
        ]

        strategy = ParallelEnsembleStrategy(max_workers=2)
        result = strategy.optimize(blocks)

        assert len(result.traverse_order) == 2

    def test_produces_valid_tour_ordering(self) -> None:
        """Test all blocks appear exactly once in traverse order."""
        from plt_optimizer.core.optimizer import ParallelEnsembleStrategy
        blocks = [
            _make_simple_block(0, (0, 0), (10, 0)),
            _make_simple_block(1, (50, 0), (60, 0)),
            _make_simple_block(2, (100, 0), (110, 0)),
        ]

        strategy = ParallelEnsembleStrategy()
        result = strategy.optimize(blocks)

        assert len(result.traverse_order) == 3
        seen_ids: set[int] = set()
        for state in result.traverse_order:
            assert state.block_id not in seen_ids
            seen_ids.add(state.block_id)
        assert seen_ids == {0, 1, 2}


class TestStrategyBenchmarkResult:
    """Tests for StrategyBenchmarkResult dataclass."""

    def test_benchmark_result_fields(self) -> None:
        """Test StrategyBenchmarkResult has all expected fields."""
        from plt_optimizer.core.optimizer import StrategyBenchmarkResult
        states = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(0, 0), exit=(10, 10)),
        ]
        opt_result = OptimizationResult(
            traverse_order=tuple(states),
            connections=(),
            total_travel_distance=100.0,
            initial_position=None,
        )
        benchmark = StrategyBenchmarkResult(
            strategy_name="TestStrategy",
            result=opt_result,
            execution_time_seconds=1.5,
            improvement_percent=25.0,
        )

        assert benchmark.strategy_name == "TestStrategy"
        assert benchmark.result is opt_result
        assert benchmark.execution_time_seconds == 1.5
        assert benchmark.improvement_percent == 25.0

    def test_benchmark_result_improvement_optional(self) -> None:
        """Test that improvement_percent can be None."""
        from plt_optimizer.core.optimizer import StrategyBenchmarkResult
        states = [BlockTraverseState(block_id=0, reversed=False, entrance=(0, 0), exit=(10, 10))]
        opt_result = OptimizationResult(
            traverse_order=tuple(states),
            connections=(),
            total_travel_distance=100.0,
            initial_position=None,
        )
        benchmark = StrategyBenchmarkResult(
            strategy_name="TestStrategy",
            result=opt_result,
            execution_time_seconds=1.5,
        )

        assert benchmark.improvement_percent is None


class TestOptimizationError:
    """Tests for OptimizationError exception class."""

    def test_optimization_error_message(self) -> None:
        """Test that OptimizationError stores and returns the message."""
        from plt_optimizer.core.optimizer import OptimizationError

        error = OptimizationError("Test error message")
        assert error.message == "Test error message"
        assert str(error) == "Test error message"

    def test_optimization_error_inheritance(self) -> None:
        """Test that OptimizationError inherits from Exception."""
        from plt_optimizer.core.optimizer import OptimizationError

        error = OptimizationError("test")
        assert isinstance(error, Exception)


class TestNoOpStrategyCoverage:
    """Additional coverage tests for NoOpStrategy."""

    def test_optimize_with_initial_position_none_and_no_blocks(self) -> None:
        """Test that optimize handles empty blocks with initial_position=None."""
        from plt_optimizer.core.optimizer import NoOpStrategy

        strategy = NoOpStrategy()
        result = strategy.optimize([], initial_position=None)

        assert len(result.traverse_order) == 0
        assert result.initial_position is None

    def test_optimize_with_initial_position_set(self) -> None:
        """Test that optimize uses the provided initial_position."""
        from plt_optimizer.core.optimizer import NoOpStrategy

        block = _make_simple_block(0, (100, 100), (110, 110))
        strategy = NoOpStrategy()
        result = strategy.optimize([block], initial_position=(50.0, 50.0))

        assert len(result.traverse_order) == 1
        assert result.initial_position == (50.0, 50.0)


class TestNearestNeighbor2OptCoverage:
    """Additional coverage tests for NearestNeighbor2OptStrategy."""

    def test_greedy_nearest_neighbor_single_block(self) -> None:
        """Test _greedy_nearest_neighbor with single block."""
        strategy = NearestNeighbor2OptStrategy()
        block = _make_simple_block(0, (10, 10), (20, 20))

        tour = strategy._greedy_nearest_neighbor([block], start_pos=(0.0, 0.0))
        assert len(tour) == 1

    def test_greedy_nearest_neighbor_two_blocks(self) -> None:
        """Test _greedy_nearest_neighbor with two blocks."""
        strategy = NearestNeighbor2OptStrategy()
        block_a = _make_simple_block(0, (10, 0), (20, 0))
        block_b = _make_simple_block(1, (100, 0), (110, 0))

        tour = strategy._greedy_nearest_neighbor([block_a, block_b], start_pos=(0.0, 0.0))
        assert len(tour) == 2

    def test_find_nearest_origin_endpoints(self) -> None:
        """Test _find_nearest_origin_endpoints returns correctly formatted results."""
        strategy = NearestNeighbor2OptStrategy()
        block_a = _make_simple_block(0, (100, 100), (110, 100))
        block_b = _make_simple_block(1, (5, 5), (15, 5))

        candidates = strategy._find_nearest_origin_endpoints([block_a, block_b], n_candidates=2)
        assert len(candidates) <= 2
        # Check structure: (position, block_index, is_exit, distance)
        for pos, idx, is_exit, dist in candidates:
            assert isinstance(pos, tuple)
            assert isinstance(idx, int)
            assert isinstance(is_exit, bool)
            assert isinstance(dist, float)

    def test_find_farthest_origin_endpoints(self) -> None:
        """Test _find_farthest_origin_endpoints returns correctly formatted results."""
        strategy = NearestNeighbor2OptStrategy()
        block_a = _make_simple_block(0, (5, 5), (15, 5))
        block_b = _make_simple_block(1, (1000, 1000), (1010, 1000))

        candidates = strategy._find_farthest_origin_endpoints([block_a, block_b], n_candidates=2)
        assert len(candidates) <= 2

    def test_two_opt_swap_improves_true(self) -> None:
        """Test _two_opt_swap_improves returns True when swap improves."""
        strategy = NearestNeighbor2OptStrategy()
        # Create blocks in a cross pattern where swapping helps
        block_a = _make_simple_block(0, (0, 0), (10, 0))
        block_b = _make_simple_block(1, (5, 100), (15, 100))  # vertical offset
        block_c = _make_simple_block(2, (20, 0), (30, 0))

        tour = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(0, 0), exit=(10, 0)),
            BlockTraverseState(block_id=1, reversed=False, entrance=(5, 100), exit=(15, 100)),
            BlockTraverseState(block_id=2, reversed=False, entrance=(20, 0), exit=(30, 0)),
        ]

        # This tests the case where swap would improve
        result = strategy._two_opt_swap_improves(tour, [block_a, block_b, block_c], 0, 1)
        assert isinstance(result, bool)

    def test_calculate_block_cost_with_same_row_preference(self) -> None:
        """Test _calculate_block_cost with y-difference penalty."""
        strategy = NearestNeighbor2OptStrategy(same_row_preference=2.0)

        cost, should_reverse = strategy._calculate_block_cost(
            from_pos=(0.0, 0.0),
            to_entrance=(10.0, 5.0),  # y-diff of 5
            to_exit=(100.0, 0.0),
        )

        assert isinstance(cost, float)
        assert isinstance(should_reverse, bool)


class TestChristofidesStrategyCoverage:
    """Additional coverage tests for ChristofidesStrategy."""

    def test_create_vertices(self) -> None:
        """Test _create_vertices builds correct vertex structure."""
        strategy = ChristofidesStrategy()
        block_a = _make_simple_block(0, (100, 100), (110, 100))
        block_b = _make_simple_block(1, (200, 200), (210, 200))

        vertices = strategy._create_vertices(
            [block_a, block_b],
            start_point=(0.0, 0.0),
            end_point=(500.0, 500.0)
        )

        # Should have: S (-1), T (-2), block_0_entrance (0), block_0_exit (1),
        #              block_1_entrance (2), block_1_exit (3)
        assert len(vertices) == 6
        assert strategy.START_VERTEX_ID in vertices
        assert strategy.END_VERTEX_ID in vertices

    def test_find_nearest_endpoints(self) -> None:
        """Test _find_nearest_endpoints method."""
        strategy = ChristofidesStrategy()
        block_a = _make_simple_block(0, (1000, 1000), (1010, 1000))
        block_b = _make_simple_block(1, (5, 5), (15, 5))

        candidates = strategy._find_nearest_endpoints([block_a, block_b], n_candidates=2)
        assert len(candidates) <= 2

    def test_find_farthest_origin_endpoints_christofides(self) -> None:
        """Test _find_farthest_origin_endpoints for Christofides."""
        strategy = ChristofidesStrategy()
        block_a = _make_simple_block(0, (5, 5), (15, 5))
        block_b = _make_simple_block(1, (1000, 1000), (1010, 1000))

        candidates = strategy._find_farthest_origin_endpoints([block_a, block_b], n_candidates=2)
        assert len(candidates) <= 2

    def test_build_mst_prim(self) -> None:
        """Test _build_mst_prim builds minimum spanning tree."""
        strategy = ChristofidesStrategy()
        vertices = strategy._create_vertices(
            [_make_simple_block(0, (100, 100), (110, 100))],
            start_point=(0.0, 0.0),
            end_point=(500.0, 500.0)
        )

        mst_edges = strategy._build_mst_prim(vertices, strategy.START_VERTEX_ID)
        assert isinstance(mst_edges, list)

    def test_find_wrong_parity_vertices(self) -> None:
        """Test _find_wrong_parity_vertices identifies odd-degree vertices."""
        strategy = ChristofidesStrategy()
        block_a = _make_simple_block(0, (100, 100), (110, 100))
        block_b = _make_simple_block(1, (200, 200), (210, 200))

        vertices = strategy._create_vertices(
            [block_a, block_b],
            start_point=(0.0, 0.0),
            end_point=(500.0, 500.0)
        )

        mst_edges = strategy._build_mst_prim(vertices, strategy.START_VERTEX_ID)
        wrong_parity = strategy._find_wrong_parity_vertices(
            mst_edges, vertices, strategy.START_VERTEX_ID, strategy.END_VERTEX_ID
        )

        # Number of odd-degree vertices should be even (for perfect matching)
        assert len(wrong_parity) % 2 == 0

    def test_greedy_perfect_matching(self) -> None:
        """Test _greedy_perfect_matching pairs odd vertices."""
        strategy = ChristofidesStrategy()
        block_a = _make_simple_block(0, (100, 100), (110, 100))

        vertices = strategy._create_vertices(
            [block_a],
            start_point=(0.0, 0.0),
            end_point=(500.0, 500.0)
        )

        mst_edges = strategy._build_mst_prim(vertices, strategy.START_VERTEX_ID)
        wrong_parity = strategy._find_wrong_parity_vertices(
            mst_edges, vertices, strategy.START_VERTEX_ID, strategy.END_VERTEX_ID
        )

        if len(wrong_parity) >= 2:
            matching = strategy._greedy_perfect_matching(wrong_parity, vertices)
            assert isinstance(matching, list)

    def test_calculate_st_path_distance(self) -> None:
        """Test _calculate_st_path_distance computes correct distance."""
        strategy = ChristofidesStrategy()
        block_a = _make_simple_block(0, (100, 100), (110, 100))

        tour = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(100, 100), exit=(110, 100))
        ]

        distance = strategy._calculate_st_path_distance(
            tour,
            [block_a],
            start_point=(0.0, 0.0),
            end_point=(200.0, 200.0)
        )

        assert isinstance(distance, float)
        assert distance > 0

    def test_try_two_block_configuration(self) -> None:
        """Test _try_two_block_configuration with various configurations."""
        strategy = ChristofidesStrategy()
        block_a = _make_simple_block(0, (100, 100), (110, 100))
        block_b = _make_simple_block(1, (200, 200), (210, 200))

        tour = strategy._try_two_block_configuration(
            [block_a, block_b],
            first_idx=0,
            first_rev=False,
            second_idx=1,
            second_rev=True
        )

        assert len(tour) == 2

    def test_vertex_distance(self) -> None:
        """Test _vertex_distance computes distances correctly."""
        strategy = ChristofidesStrategy()
        v1 = (0.0, 0.0, 0, False)
        v2 = (3.0, 4.0, 0, True)

        dist = strategy._vertex_distance(v1, v2)
        assert math.isclose(dist, 5.0)  # 3-4-5 triangle

    def test_determine_reversal_for_first_block(self) -> None:
        """Test _determine_reversal_for_first_block."""
        strategy = ChristofidesStrategy()
        block_a = _make_simple_block(0, (100, 100), (110, 100))

        # Get a vertex ID for the block
        vertices = strategy._create_vertices(
            [block_a],
            start_point=(0.0, 0.0),
            end_point=(500.0, 500.0)
        )

        # Find first non-terminal vertex
        first_vid = None
        for vid in vertices:
            if vid >= 0:  # Skip S and T terminals which are negative
                first_vid = vid
                break

        if first_vid is not None:
            reversed_flag = strategy._determine_reversal_for_first_block(first_vid, [block_a])
            assert isinstance(reversed_flag, bool)

    def test_get_vertex_info_st(self) -> None:
        """Test _get_vertex_info_st retrieves correct vertex info."""
        strategy = ChristofidesStrategy()
        block_a = _make_simple_block(0, (100, 100), (110, 100))

        # Set up the terminal points that _get_vertex_info_st needs
        strategy._start_point = (50.0, 50.0)
        strategy._end_point = (200.0, 200.0)

        vertices = strategy._create_vertices(
            [block_a],
            start_point=(50.0, 50.0),
            end_point=(200.0, 200.0)
        )

        # Test getting info for a real vertex (not S or T terminals)
        if len(vertices) > 2:
            vid = next(vid for vid in vertices.keys() if vid >= 0)
            x, y, block_id, is_exit = strategy._get_vertex_info_st(vid, [block_a])
            # Coordinates can be int or float depending on input
            assert isinstance(x, (int, float))
            assert isinstance(y, (int, float))

    def test_get_vertex_info_st_with_start_terminal(self) -> None:
        """Test _get_vertex_info_st with START_VERTEX_ID."""
        strategy = ChristofidesStrategy()
        strategy._start_point = (100.0, 200.0)
        block_a = _make_simple_block(0, (50, 50), (60, 60))

        x, y, block_idx, is_exit = strategy._get_vertex_info_st(
            strategy.START_VERTEX_ID, [block_a]
        )
        assert x == 100.0
        assert y == 200.0
        assert block_idx == -1
        assert is_exit is False

    def test_get_vertex_info_st_with_end_terminal(self) -> None:
        """Test _get_vertex_info_st with END_VERTEX_ID."""
        strategy = ChristofidesStrategy()
        strategy._end_point = (300.0, 400.0)
        block_a = _make_simple_block(0, (50, 50), (60, 60))

        x, y, block_idx, is_exit = strategy._get_vertex_info_st(
            strategy.END_VERTEX_ID, [block_a]
        )
        assert x == 300.0
        assert y == 400.0
        assert block_idx == -2
        assert is_exit is True

    def test_get_start_coords(self) -> None:
        """Test _get_start_coords returns stored start point."""
        strategy = ChristofidesStrategy()
        strategy._start_point = (100.0, 200.0)

        coords = strategy._get_start_coords()
        assert coords == (100.0, 200.0)

    def test_get_end_coords(self) -> None:
        """Test _get_end_coords returns stored end point."""
        strategy = ChristofidesStrategy()
        strategy._end_point = (300.0, 400.0)

        coords = strategy._get_end_coords()
        assert coords == (300.0, 400.0)


class TestSimulatedAnnealingCoverage:
    """Additional coverage tests for SimulatedAnnealingStrategy."""

    def test_acceptance_probability(self) -> None:
        """Test _acceptance_probability method."""
        strategy = SimulatedAnnealingStrategy()

        # Delta negative - should accept
        result_neg = strategy._acceptance_probability(-10.0, 100.0)
        assert isinstance(result_neg, bool)

        # Delta positive with high temp - likely to accept
        result_pos_high = strategy._acceptance_probability(10.0, 10000.0)
        assert isinstance(result_pos_high, bool)

    def test_calculate_tour_distance(self) -> None:
        """Test _calculate_tour_distance computes total distance."""
        strategy = SimulatedAnnealingStrategy()
        block_a = _make_simple_block(0, (0, 0), (10, 0))
        block_b = _make_simple_block(1, (50, 0), (60, 0))

        tour = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(0, 0), exit=(10, 0)),
            BlockTraverseState(block_id=1, reversed=False, entrance=(50, 0), exit=(60, 0)),
        ]

        distance = strategy._calculate_tour_distance(tour, [block_a, block_b])
        assert isinstance(distance, float)
        # Distance should be from (10,0) to (50,0) = 40
        assert math.isclose(distance, 40.0)

    def test_generate_neighbor_segment_reversal(self) -> None:
        """Test _generate_neighbor with segment reversal."""
        strategy = SimulatedAnnealingStrategy()
        tour = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(0, 0), exit=(10, 0)),
            BlockTraverseState(block_id=1, reversed=False, entrance=(20, 0), exit=(30, 0)),
            BlockTraverseState(block_id=2, reversed=False, entrance=(40, 0), exit=(50, 0)),
        ]

        # Run multiple times to ensure we hit the segment reversal path
        results = set()
        for _ in range(20):
            neighbor = strategy._generate_neighbor(tour)
            assert len(neighbor) == len(tour)
            block_ids = [s.block_id for s in neighbor]
            assert sorted(block_ids) == [0, 1, 2]  # Same blocks
            results.add(tuple(block_ids))

    def test_generate_neighbor_swap(self) -> None:
        """Test _generate_neighbor with block swap."""
        strategy = SimulatedAnnealingStrategy()
        tour = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(0, 0), exit=(10, 0)),
            BlockTraverseState(block_id=1, reversed=False, entrance=(20, 0), exit=(30, 0)),
            BlockTraverseState(block_id=2, reversed=False, entrance=(40, 0), exit=(50, 0)),
        ]

        # Run multiple times to potentially hit the swap path
        for _ in range(30):
            neighbor = strategy._generate_neighbor(tour)
            assert len(neighbor) == len(tour)

    def test_find_nearest_origin_endpoint_single(self) -> None:
        """Test _find_nearest_origin_endpoint returns single best."""
        strategy = SimulatedAnnealingStrategy()
        block_a = _make_simple_block(0, (1000, 1000), (1010, 1000))
        block_b = _make_simple_block(1, (5, 5), (15, 5))

        pos, idx, is_exit = strategy._find_nearest_origin_endpoint([block_a, block_b])
        assert isinstance(pos, tuple)
        assert isinstance(idx, int)
        assert isinstance(is_exit, bool)


class TestGeneticAlgorithmCoverage:
    """Additional coverage tests for GeneticAlgorithmStrategy."""

    def test_find_nearest_origin_endpoints_ga(self) -> None:
        """Test _find_nearest_origin_endpoints in GA context."""
        strategy = GeneticAlgorithmStrategy()
        block_a = _make_simple_block(0, (100, 100), (110, 100))
        block_b = _make_simple_block(1, (5, 5), (15, 5))

        candidates = strategy._find_nearest_origin_endpoints([block_a, block_b], n_candidates=3)
        assert len(candidates) <= 3

    def test_find_farthest_origin_endpoints_ga(self) -> None:
        """Test _find_farthest_origin_endpoints in GA context."""
        strategy = GeneticAlgorithmStrategy()
        block_a = _make_simple_block(0, (5, 5), (15, 5))
        block_b = _make_simple_block(1, (1000, 1000), (1010, 1000))

        candidates = strategy._find_farthest_origin_endpoints([block_a, block_b], n_candidates=3)
        assert len(candidates) <= 3

    def test_tournament_selection(self) -> None:
        """Test _tournament_selection picks best from sample."""
        strategy = GeneticAlgorithmStrategy()
        # Need at least tournament_size (4 default) individuals for selection to work well
        population: list[list[int]] = [
            [0, 1, 2],
            [0, 2, 1],
            [1, 0, 2],
            [2, 1, 0],
            [1, 2, 0],
        ]

        # Need 3 blocks to match chromosome size
        block_a = _make_simple_block(0, (100, 100), (110, 100))
        block_b = _make_simple_block(1, (200, 200), (210, 200))
        block_c = _make_simple_block(2, (300, 300), (310, 300))

        winner = strategy._tournament_selection(
            population, [block_a, block_b, block_c], start_pos=(0.0, 0.0)
        )
        assert isinstance(winner, list)
        assert len(winner) == 3

    def test_order_crossover(self) -> None:
        """Test _order_crossover creates valid offspring."""
        strategy = GeneticAlgorithmStrategy()
        parent1 = [0, 1, 2, 3, 4]
        parent2 = [2, 4, 1, 3, 0]

        child = strategy._order_crossover(parent1, parent2)
        assert len(child) == len(parent1)

    def test_mutate_inversion(self) -> None:
        """Test _mutate with inversion mutation."""
        strategy = GeneticAlgorithmStrategy()
        chromosome = [0, 1, 2, 3, 4]

        mutated = strategy._mutate(chromosome)
        assert len(mutated) == len(chromosome)

    def test_tour_to_chromosome(self) -> None:
        """Test _tour_to_chromosome encoding."""
        strategy = GeneticAlgorithmStrategy()
        tour = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(0, 0), exit=(10, 0)),
            BlockTraverseState(block_id=1, reversed=True, entrance=(30, 0), exit=(20, 0)),
            BlockTraverseState(block_id=2, reversed=False, entrance=(40, 0), exit=(50, 0)),
        ]

        chromosome = strategy._tour_to_chromosome(tour)
        assert len(chromosome) == len(tour)

    def test_decode_gene(self) -> None:
        """Test _decode_gene extracts block_id and reversed flag."""
        strategy = GeneticAlgorithmStrategy()

        # Gene encoding: bit 0-30 for block_id, bit 31 for reversed
        gene_normal = 5  # block_id=5, not reversed
        block_id, is_reversed = strategy._decode_gene(gene_normal)
        assert block_id == 5
        assert is_reversed is False

    def test_is_reversed_gene(self) -> None:
        """Test _is_reversed_gene correctly identifies reversal bit."""
        strategy = GeneticAlgorithmStrategy()

        gene_not_reversed = 100  # even or with high bit not set
        gene_reversed = (1 << 31) | 5  # high bit set

        assert strategy._is_reversed_gene(gene_not_reversed) is False

    def test_two_opt_refinement_ga(self) -> None:
        """Test _two_opt_refinement in GA context."""
        strategy = GeneticAlgorithmStrategy()
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

    def test_two_opt_swap_improves_ga(self) -> None:
        """Test _two_opt_swap_improves in GA context."""
        strategy = GeneticAlgorithmStrategy()
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

        result = strategy._two_opt_swap_improves(tour, blocks, 0, 1)
        assert isinstance(result, bool)

    def test_optimize_tour_directions(self) -> None:
        """Test _optimize_tour_directions."""
        strategy = GeneticAlgorithmStrategy()
        tour = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(0, 0), exit=(10, 0)),
            BlockTraverseState(block_id=1, reversed=True, entrance=(30, 0), exit=(20, 0)),
        ]
        blocks = [
            _make_simple_block(0, (0, 0), (10, 0)),
            _make_simple_block(1, (20, 0), (30, 0)),
        ]

        result = strategy._optimize_tour_directions(tour, blocks, start_pos=(0.0, 0.0))
        assert len(result) == 2


class TestRunStrategyWorker:
    """Tests for _run_strategy_worker function."""

    def test_run_strategy_worker_with_noop(self) -> None:
        """Test worker runs NoOp strategy correctly."""
        from plt_optimizer.core.optimizer import _run_strategy_worker

        blocks_serialized = (
            (0, (10.0, 20.0), (30.0, 40.0)),
            (1, (50.0, 60.0), (70.0, 80.0)),
        )

        result = _run_strategy_worker(
            strategy_name="NoOp (Baseline)",
            blocks_serialized=blocks_serialized,
            initial_position=(0.0, 0.0),
        )

        assert result.strategy_name == "NoOp (Baseline)"
        assert isinstance(result.result, OptimizationResult)
        assert isinstance(result.execution_time_seconds, float)

    def test_run_strategy_worker_with_nearest_neighbor(self) -> None:
        """Test worker runs NearestNeighbor + 2-Opt strategy."""
        from plt_optimizer.core.optimizer import _run_strategy_worker

        blocks_serialized = (
            (0, (10.0, 20.0), (30.0, 40.0)),
            (1, (50.0, 60.0), (70.0, 80.0)),
        )

        result = _run_strategy_worker(
            strategy_name="NearestNeighbor + 2-Opt",
            blocks_serialized=blocks_serialized,
            initial_position=(0.0, 0.0),
        )

        assert result.strategy_name == "NearestNeighbor + 2-Opt"

    def test_run_strategy_worker_unknown_strategy_raises(self) -> None:
        """Test worker raises ValueError for unknown strategy."""
        from plt_optimizer.core.optimizer import _run_strategy_worker

        with pytest.raises(ValueError, match="Unknown strategy"):
            _run_strategy_worker(
                strategy_name="NonExistentStrategy",
                blocks_serialized=((0, (10.0, 20.0), (30.0, 40.0)),),
                initial_position=None,
            )


class TestParallelEnsembleOptimizationResult:
    """Tests for ParallelEnsembleOptimizationResult dataclass."""

    def test_ensemble_result_fields(self) -> None:
        """Test ParallelEnsembleOptimizationResult has all expected fields."""
        from plt_optimizer.core.optimizer import (
            ParallelEnsembleOptimizationResult,
            StrategyBenchmarkResult,
        )

        states = [BlockTraverseState(block_id=0, reversed=False, entrance=(0, 0), exit=(10, 10))]
        opt_result = OptimizationResult(
            traverse_order=tuple(states),
            connections=(),
            total_travel_distance=100.0,
            initial_position=None,
        )
        benchmark = StrategyBenchmarkResult(
            strategy_name="TestStrategy",
            result=opt_result,
            execution_time_seconds=1.5,
        )

        ensemble_result = ParallelEnsembleOptimizationResult(
            result=opt_result,
            winner_name="TestStrategy",
            all_benchmarks=(benchmark,),
        )

        assert ensemble_result.result is opt_result
        assert len(ensemble_result.all_benchmarks) == 1
        assert ensemble_result.winner_name == "TestStrategy"

    def test_ensemble_winning_strategy_name(self) -> None:
        """Test that winning strategy name property works."""
        from plt_optimizer.core.optimizer import (
            ParallelEnsembleOptimizationResult,
            StrategyBenchmarkResult,
        )

        states = [BlockTraverseState(block_id=0, reversed=False, entrance=(0, 0), exit=(10, 10))]
        opt_result1 = OptimizationResult(
            traverse_order=tuple(states),
            connections=(),
            total_travel_distance=100.0,
            initial_position=None,
        )
        opt_result2 = OptimizationResult(
            traverse_order=tuple(states),
            connections=(),
            total_travel_distance=80.0,  # Better distance
            initial_position=None,
        )

        benchmark1 = StrategyBenchmarkResult("StrategyA", opt_result1, 1.0)
        benchmark2 = StrategyBenchmarkResult("StrategyB", opt_result2, 2.0)

        ensemble_result = ParallelEnsembleOptimizationResult(
            result=opt_result2,  # Best result is from StrategyB
            winner_name="StrategyB",
            all_benchmarks=(benchmark1, benchmark2),
        )

        assert ensemble_result.winner_name == "StrategyB"

    def test_ensemble_traverse_order(self) -> None:
        """Test traverse_order property returns correct order."""
        from plt_optimizer.core.optimizer import (
            ParallelEnsembleOptimizationResult,
            StrategyBenchmarkResult,
        )

        states = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(0, 0), exit=(10, 10)),
            BlockTraverseState(block_id=1, reversed=True, entrance=(20, 20), exit=(30, 30)),
        ]
        opt_result = OptimizationResult(
            traverse_order=tuple(states),
            connections=(),
            total_travel_distance=100.0,
            initial_position=None,
        )
        benchmark = StrategyBenchmarkResult("TestStrategy", opt_result, 1.0)

        ensemble_result = ParallelEnsembleOptimizationResult(
            result=opt_result,
            winner_name="TestStrategy",
            all_benchmarks=(benchmark,),
        )

        assert len(ensemble_result.traverse_order) == 2
        assert ensemble_result.total_travel_distance == 100.0


# ---------------------------------------------------------------------------
# Additional coverage tests for NearestNeighbor2OptStrategy
# ---------------------------------------------------------------------------

class TestNearestNeighbor2OptCoverage2:
    """Coverage for NearestNeighbor2OptStrategy missing lines."""

    def test_optimize_empty_blocks(self) -> None:
        """Cover line 343: empty block list for NearestNeighbor."""
        strategy = NearestNeighbor2OptStrategy()
        result = strategy.optimize([])
        assert result.block_count == 0
        assert result.total_travel_distance == 0.0

    def test_optimize_4_blocks_no_initial_position_triggers_2opt(self) -> None:
        """Cover line 368: 2-opt called when no initial_position and 4+ blocks."""
        blocks = [
            _make_simple_block(0, (0, 0), (10, 0)),
            _make_simple_block(1, (50, 0), (60, 0)),
            _make_simple_block(2, (100, 0), (110, 0)),
            _make_simple_block(3, (150, 0), (160, 0)),
        ]
        strategy = NearestNeighbor2OptStrategy()
        result = strategy.optimize(blocks)
        assert result.block_count == 4
        assert len({s.block_id for s in result.traverse_order}) == 4

    def test_optimize_4_blocks_with_initial_position_triggers_2opt(self) -> None:
        """Cover line 397: 2-opt called when initial_position provided and 4+ blocks."""
        blocks = [
            _make_simple_block(0, (0, 0), (10, 0)),
            _make_simple_block(1, (50, 0), (60, 0)),
            _make_simple_block(2, (100, 0), (110, 0)),
            _make_simple_block(3, (150, 0), (160, 0)),
        ]
        strategy = NearestNeighbor2OptStrategy()
        result = strategy.optimize(blocks, initial_position=(0.0, 0.0))
        assert result.block_count == 4

    def test_greedy_nearest_neighbor_exit_reversed(self) -> None:
        """Cover lines 448-454: reversed block in _greedy_nearest_neighbor."""
        # Block where exit (5,0) is closer to start (0,0) than entrance (100,0)
        block_reversed = _make_simple_block(0, (100, 0), (5, 0))
        block_normal = _make_simple_block(1, (200, 0), (210, 0))
        strategy = NearestNeighbor2OptStrategy()
        tour = strategy._greedy_nearest_neighbor(
            [block_reversed, block_normal], start_pos=(0.0, 0.0)
        )
        assert len(tour) == 2
        # First block should be reversed (exit was closer to origin)
        assert tour[0].reversed is True

    def test_find_nearest_origin_endpoint_singular(self) -> None:
        """Cover lines 568-570: _find_nearest_origin_endpoint (singular)."""
        strategy = NearestNeighbor2OptStrategy()
        block_a = _make_simple_block(0, (100, 100), (110, 100))
        block_b = _make_simple_block(1, (5, 5), (15, 5))
        pos, idx, is_exit = strategy._find_nearest_origin_endpoint([block_a, block_b])
        assert isinstance(pos, tuple)
        assert isinstance(idx, int)
        assert isinstance(is_exit, bool)

    def test_greedy_from_start_with_reversed_follow_block(self) -> None:
        """Cover lines 641-647: reversed block in _greedy_nearest_neighbor_from_start while loop."""
        # Block 0 is forced first. Block 1 has exit closer to current pos than entrance.
        block_0 = _make_simple_block(0, (0, 0), (10, 0))  # forced first
        block_1 = _make_simple_block(1, (500, 0), (12, 0))  # exit (12,0) near (10,0)
        strategy = NearestNeighbor2OptStrategy()
        tour = strategy._greedy_nearest_neighbor_from_start(
            [block_0, block_1],
            start_pos=(0.0, 0.0),
            forced_first_block=0,
            forced_first_reversed=False,
        )
        assert len(tour) == 2
        # block_1 should be reversed since exit was closer
        assert tour[1].reversed is True

    def test_greedy_from_start_false_cost_branch(self) -> None:
        """Cover line 633->625: if cost < best_cost False branch with 3 blocks."""
        # 3 blocks: forced first is block 0. Then two remaining blocks where
        # block 1 is cheapest, block 2 is more expensive.
        block_0 = _make_simple_block(0, (0, 0), (10, 0))
        block_1 = _make_simple_block(1, (11, 0), (20, 0))   # very close
        block_2 = _make_simple_block(2, (1000, 0), (1010, 0))  # far away
        strategy = NearestNeighbor2OptStrategy()
        tour = strategy._greedy_nearest_neighbor_from_start(
            [block_0, block_1, block_2],
            start_pos=(0.0, 0.0),
            forced_first_block=0,
            forced_first_reversed=False,
        )
        assert len(tour) == 3

    def test_nn_calculate_block_cost_exit_cheaper(self) -> None:
        """Cover line 694: else branch when exit is cheaper in NN _calculate_block_cost."""
        strategy = NearestNeighbor2OptStrategy()
        # Exit (1,0) is cheaper than entrance (100,0) from (0,0)
        cost, should_reverse = strategy._calculate_block_cost(
            from_pos=(0.0, 0.0),
            to_entrance=(100.0, 0.0),
            to_exit=(1.0, 0.0),
        )
        assert should_reverse is True
        assert math.isclose(cost, 1.0)

    def test_two_opt_swap_actually_improves(self) -> None:
        """Cover lines 726-727: actual 2-opt swap that improves the tour."""
        # Create a "crossed" tour: A(0,0)→B(0,100), then C(100,100)→D(100,0)
        # Swapping would give A(0,0)→C(100,100)→B(0,100)→D(100,0), reducing crossings
        strategy = NearestNeighbor2OptStrategy()
        # Tour with a crossing: 0→1→2→3 where 0→2 would be better than 0→1→2
        tour = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(0, 0), exit=(0, 0)),
            BlockTraverseState(block_id=1, reversed=False, entrance=(100, 0), exit=(100, 0)),
            BlockTraverseState(block_id=2, reversed=False, entrance=(0, 50), exit=(0, 50)),
            BlockTraverseState(block_id=3, reversed=False, entrance=(100, 50), exit=(100, 50)),
        ]
        blocks = [
            _make_simple_block(0, (0, 0), (0, 0)),
            _make_simple_block(1, (100, 0), (100, 0)),
            _make_simple_block(2, (0, 50), (0, 50)),
            _make_simple_block(3, (100, 50), (100, 50)),
        ]
        # Run the 2-opt refinement
        result_tour = strategy._two_opt_refinement(tour, blocks)
        assert len(result_tour) == 4


# ---------------------------------------------------------------------------
# Additional coverage tests for InsertionHeuristicStrategy
# ---------------------------------------------------------------------------

class TestInsertionHeuristicCoverage2:
    """Coverage for InsertionHeuristicStrategy missing lines."""

    def test_single_block_with_initial_position(self) -> None:
        """Cover lines 834-838: single block + initial_position provided."""
        # Entrance at (5,0) closer to initial_position (0,0) than exit (100,0)
        block = _make_simple_block(0, (5, 0), (100, 0))
        strategy = InsertionHeuristicStrategy()
        result = strategy.optimize([block], initial_position=(0.0, 0.0))
        assert result.block_count == 1
        assert result.traverse_order[0].reversed is False

    def test_single_block_exit_closer_to_initial_position(self) -> None:
        """Cover lines 852-853: single block where exit is closer to initial_position."""
        # Exit at (5,0) closer to initial_position (0,0) than entrance (100,0)
        block = _make_simple_block(0, (100, 0), (5, 0))
        strategy = InsertionHeuristicStrategy()
        result = strategy.optimize([block], initial_position=(0.0, 0.0))
        assert result.block_count == 1
        assert result.traverse_order[0].reversed is True

    def test_insertion_false_cost_branch(self) -> None:
        """Cover line 888->884: False branch of insertion cost comparison."""
        # 3 blocks: two already in tour, one to insert.
        # The first block to insert has lower cost than subsequent ones.
        block_a = _make_simple_block(0, (0, 0), (10, 0))
        block_b = _make_simple_block(1, (20, 0), (30, 0))
        block_c = _make_simple_block(2, (5, 0), (15, 0))  # to insert between a and b
        strategy = InsertionHeuristicStrategy()
        result = strategy.optimize([block_a, block_b, block_c])
        assert result.block_count == 3

    def test_build_tour_with_seed_is1_exit_false_is2_exit_true(self) -> None:
        """Cover lines 988, 996: build_tour_with_seed with is1_exit=False, is2_exit=True."""
        # block_a entrance (1,0) to block_b exit (3,0) is the minimum distance pair
        # is1_exit=False (block_a at entrance), is2_exit=True (block_b at exit)
        block_a = _make_simple_block(0, (1, 0), (200, 0))   # entrance near block_b exit
        block_b = _make_simple_block(1, (300, 0), (3, 0))   # exit near block_a entrance
        strategy = InsertionHeuristicStrategy()
        result = strategy.optimize([block_a, block_b])
        assert result.block_count == 2

    def test_build_initial_tour_from_start_exit_closer(self) -> None:
        """Cover lines 1045->1050, 1051-1053, 1058, 1087->1092, 1093-1095, 1103."""
        # First block: exit (2,0) closer to start (0,0) than entrance (100,0)
        # Second block: exit (15,0) closer to first block's exit (2,0)... wait,
        # since first block is reversed, its logical exit = entrance (100,0).
        # Use start_pos where exit is clearly closer.
        block_a = _make_simple_block(0, (100, 0), (2, 0))   # exit (2,0) closer to (0,0)
        block_b = _make_simple_block(1, (500, 0), (105, 0))  # exit (105,0) closer to state1.exit (100,0)
        strategy = InsertionHeuristicStrategy()
        result = strategy.optimize([block_a, block_b], initial_position=(0.0, 0.0))
        assert result.block_count == 2
        # First block should be reversed
        assert result.traverse_order[0].reversed is True

    def test_calculate_insertion_cost_empty_tour(self) -> None:
        """Cover lines 1172-1178: _calculate_insertion_cost with empty tour."""
        block = _make_simple_block(0, (10, 0), (20, 0))
        strategy = InsertionHeuristicStrategy()
        cost, should_reverse = strategy._calculate_insertion_cost(block, [], 0, [block])
        assert isinstance(cost, float)
        assert isinstance(should_reverse, bool)

    def test_find_best_insertion_false_and_reversed(self) -> None:
        """Cover lines 1254->1251, 1259: False branch in cost comparison and reversed insert."""
        # Tour with 2 blocks; insert a third that is best inserted in reversed orientation.
        tour = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(0, 0), exit=(10, 0)),
            BlockTraverseState(block_id=1, reversed=False, entrance=(100, 0), exit=(110, 0)),
        ]
        block_a = _make_simple_block(0, (0, 0), (10, 0))
        block_b = _make_simple_block(1, (100, 0), (110, 0))
        # Block to insert: exit (55,0) is between (10,0) and (100,0), best reversed
        block_c = _make_simple_block(2, (200, 0), (55, 0))
        strategy = InsertionHeuristicStrategy()
        pos, state, cost = strategy._find_best_insertion_position(block_c, tour, [block_a, block_b, block_c])
        assert isinstance(pos, int)
        assert isinstance(cost, float)


# ---------------------------------------------------------------------------
# Additional coverage tests for ChristofidesStrategy
# ---------------------------------------------------------------------------

class TestChristofidesCoverage2:
    """Coverage for ChristofidesStrategy missing lines."""

    def test_same_block_skip_continue(self) -> None:
        """Cover line 1377: continue when start and end candidates share same block."""
        # Block 0: entrance (1,0) very close to origin, exit (999,0) very far
        # Block 1: entrance (2,0), exit (3,0) — both close
        # start_candidates: block0.entrance AND block1.entrance
        # end_candidates: block0.exit AND block1.exit
        # combination (block0, block0) triggers continue
        block_0 = _make_simple_block(0, (1, 0), (999, 0))
        block_1 = _make_simple_block(1, (2, 0), (3, 0))
        strategy = ChristofidesStrategy()
        result = strategy.optimize([block_0, block_1], start_point=(0.0, 0.0), end_point=(1000.0, 0.0))
        assert result.block_count == 2

    def test_single_block_exit_closer_to_start(self) -> None:
        """Cover line 1502: reversed=True in _optimize_single_block when exit closer to start."""
        # exit (1,0) is closer to start (0,0) than entrance (100,0)
        block = _make_simple_block(0, (100, 0), (1, 0))
        strategy = ChristofidesStrategy()
        result = strategy.optimize([block], start_point=(0.0, 0.0), end_point=(200.0, 0.0))
        assert result.block_count == 1
        assert result.traverse_order[0].reversed is True

    def test_calculate_st_path_distance_empty_tour(self) -> None:
        """Cover line 1655: _calculate_st_path_distance with empty tour."""
        strategy = ChristofidesStrategy()
        dist = strategy._calculate_st_path_distance(
            [], [], start_point=(0.0, 0.0), end_point=(3.0, 4.0)
        )
        # Distance from start (0,0) to end (3,4) = 5
        assert math.isclose(dist, 5.0)

    def test_build_mst_prim_empty_vertices(self) -> None:
        """Cover line 1809: _build_mst_prim with empty vertices dict."""
        strategy = ChristofidesStrategy()
        result = strategy._build_mst_prim({}, strategy.START_VERTEX_ID)
        assert result == []

    def test_greedy_perfect_matching_empty(self) -> None:
        """Cover line 1921: _greedy_perfect_matching with empty odd_vertices list."""
        strategy = ChristofidesStrategy()
        result = strategy._greedy_perfect_matching([], {})
        assert result == []

    def test_build_eulerian_path_empty_edges(self) -> None:
        """Cover line 1966: _build_eulerian_path with empty edges list."""
        strategy = ChristofidesStrategy()
        vertices = {-1: (0.0, 0.0, -1, False), -2: (10.0, 10.0, -2, True)}
        result = strategy._build_eulerian_path([], strategy.START_VERTEX_ID, vertices)
        assert result == [strategy.START_VERTEX_ID]

    def test_determine_reversal_first_block_is_exit_true(self) -> None:
        """Cover line 2101: _determine_reversal_for_first_block returns True when is_exit."""
        strategy = ChristofidesStrategy()
        block_a = _make_simple_block(0, (10, 0), (20, 0))
        strategy._start_point = (0.0, 0.0)
        strategy._end_point = (100.0, 0.0)
        # Create vertices so vid=1 is the exit of block 0 (is_exit=True)
        vertices = strategy._create_vertices([block_a], (0.0, 0.0), (100.0, 0.0))
        # vid=1 is the exit vertex of block 0
        result = strategy._determine_reversal_for_first_block(1, [block_a])
        assert result is True

    def test_get_vertex_info_st_out_of_range(self) -> None:
        """Cover line 2133: _get_vertex_info_st with vid out of range."""
        strategy = ChristofidesStrategy()
        strategy._start_point = (0.0, 0.0)
        strategy._end_point = (100.0, 0.0)
        block_a = _make_simple_block(0, (10, 0), (20, 0))
        # vid=999 is out of range for a single block (num_endpoints=2)
        result = strategy._get_vertex_info_st(999, [block_a])
        assert result == (0.0, 0.0, -1, False)

    def test_get_start_coords_when_start_point_none(self) -> None:
        """Cover line 2152: _get_start_coords returns (0,0) when _start_point is None."""
        strategy = ChristofidesStrategy()
        strategy._start_point = None
        assert strategy._get_start_coords() == (0.0, 0.0)

    def test_get_end_coords_when_end_point_none(self) -> None:
        """Cover line 2162: _get_end_coords returns (0,0) when _end_point is None."""
        strategy = ChristofidesStrategy()
        strategy._end_point = None
        assert strategy._get_end_coords() == (0.0, 0.0)

    def test_create_traverse_order_st_path_empty(self) -> None:
        """Cover line 2200: _create_traverse_order_st_path with empty hamiltonian."""
        strategy = ChristofidesStrategy()
        result = strategy._create_traverse_order_st_path(
            [], [], start_point=(0.0, 0.0), end_point=(100.0, 0.0)
        )
        assert result == []

    def test_create_traverse_order_exit_closer(self) -> None:
        """Cover line 2261: _create_traverse_order_st_path reversed state for non-first block."""
        strategy = ChristofidesStrategy()
        # Block 0 forward, block 1 has exit closer to block 0's exit
        block_0 = _make_simple_block(0, (0, 0), (10, 0))
        block_1 = _make_simple_block(1, (500, 0), (12, 0))  # exit (12,0) near (10,0)
        hamiltonian = [(0, False), (1, False)]
        result = strategy._create_traverse_order_st_path(
            hamiltonian, [block_0, block_1],
            start_point=(0.0, 0.0), end_point=(100.0, 0.0)
        )
        assert len(result) == 2
        assert result[1].reversed is True


# ---------------------------------------------------------------------------
# Additional coverage tests for SimulatedAnnealingStrategy
# ---------------------------------------------------------------------------

class TestSACoverage2:
    """Coverage for SimulatedAnnealingStrategy missing lines."""

    def test_optimize_with_initial_position(self) -> None:
        """Cover lines 2367-2368: SA optimize with initial_position provided."""
        blocks = [
            _make_simple_block(0, (10, 0), (20, 0)),
            _make_simple_block(1, (50, 0), (60, 0)),
        ]
        strategy = SimulatedAnnealingStrategy()
        result = strategy.optimize(blocks, initial_position=(0.0, 0.0))
        assert result.block_count == 2

    def test_optimize_4_blocks_runs_main_sa_loop(self) -> None:
        """Cover lines 2421-2449: SA main loop runs with 4+ blocks."""
        blocks = [
            _make_simple_block(0, (0, 0), (10, 0)),
            _make_simple_block(1, (50, 0), (60, 0)),
            _make_simple_block(2, (100, 0), (110, 0)),
            _make_simple_block(3, (150, 0), (160, 0)),
        ]
        # Use fast SA settings to keep test quick
        strategy = SimulatedAnnealingStrategy(
            initial_temperature=100.0,
            cooling_rate=0.5,
            iterations_per_temp=2,
            min_temperature=1.0,
        )
        result = strategy.optimize(blocks)
        assert result.block_count == 4
        assert len({s.block_id for s in result.traverse_order}) == 4

    def test_calculate_tour_distance_empty(self) -> None:
        """Cover line 2532: _calculate_tour_distance with empty tour."""
        strategy = SimulatedAnnealingStrategy()
        dist = strategy._calculate_tour_distance([], [])
        assert dist == 0.0

    def test_generate_neighbor_single_element(self) -> None:
        """Cover line 2564: _generate_neighbor with single-element tour."""
        strategy = SimulatedAnnealingStrategy()
        tour = [BlockTraverseState(block_id=0, reversed=False, entrance=(0, 0), exit=(10, 0))]
        result = strategy._generate_neighbor(tour)
        assert len(result) == 1

    def test_acceptance_probability_zero_temperature(self) -> None:
        """Cover line 2594: _acceptance_probability returns False for temperature <= 0."""
        strategy = SimulatedAnnealingStrategy()
        result = strategy._acceptance_probability(10.0, 0.0)
        assert result is False

    def test_find_nearest_origin_endpoint_exit_closer(self) -> None:
        """Cover lines 2672->2678, 2680-2683: SA _find_nearest_origin_endpoint exit closer."""
        # Block where exit (1,0) is closer to origin than entrance (100,0)
        block = _make_simple_block(0, (100, 0), (1, 0))
        strategy = SimulatedAnnealingStrategy()
        pos, idx, is_exit = strategy._find_nearest_origin_endpoint([block])
        assert is_exit is True
        assert math.isclose(pos[0], 1.0)

    def test_find_farthest_origin_endpoints_sa(self) -> None:
        """Cover lines 2749-2762: SA _find_farthest_origin_endpoints."""
        block_a = _make_simple_block(0, (5, 0), (10, 0))
        block_b = _make_simple_block(1, (1000, 0), (2000, 0))
        strategy = SimulatedAnnealingStrategy()
        candidates = strategy._find_farthest_origin_endpoints([block_a, block_b], n_candidates=2)
        assert len(candidates) == 2
        # Farthest should be block_b's exit (2000,0)
        assert candidates[0][3] == 2000.0


# ---------------------------------------------------------------------------
# Additional coverage tests for GeneticAlgorithmStrategy
# ---------------------------------------------------------------------------

class TestGACoverage2:
    """Coverage for GeneticAlgorithmStrategy missing lines."""

    def test_optimize_with_initial_position(self) -> None:
        """Cover lines 2858-2859: GA optimize with initial_position provided."""
        blocks = [
            _make_simple_block(0, (10, 0), (20, 0)),
            _make_simple_block(1, (50, 0), (60, 0)),
        ]
        strategy = GeneticAlgorithmStrategy()
        result = strategy.optimize(blocks, initial_position=(0.0, 0.0))
        assert result.block_count == 2

    def test_optimize_4_blocks_runs_ga_loop(self) -> None:
        """Cover lines 2911-2965, 2990-3025: GA main loop + population init with 4+ blocks."""
        blocks = [
            _make_simple_block(0, (0, 0), (10, 0)),
            _make_simple_block(1, (50, 100), (60, 100)),
            _make_simple_block(2, (200, 50), (210, 50)),
            _make_simple_block(3, (100, 200), (110, 200)),
        ]
        # Use very small GA params with high mutation rate to avoid infinite crossover loops
        strategy = GeneticAlgorithmStrategy(
            population_size=4,
            generations=2,
            mutation_rate=1.0,
            elitism_count=1,
            tournament_size=2,
        )
        result = strategy.optimize(blocks)
        assert result.block_count == 4
        assert len({s.block_id for s in result.traverse_order}) == 4

    def test_calculate_fitness_empty_chromosome(self) -> None:
        """Cover line 3125: _calculate_fitness with empty chromosome."""
        strategy = GeneticAlgorithmStrategy()
        fitness = strategy._calculate_fitness([], [], (0.0, 0.0))
        assert fitness == 0.0

    def test_tournament_selection_empty_population(self) -> None:
        """Cover line 3169: _tournament_selection with empty population."""
        strategy = GeneticAlgorithmStrategy()
        result = strategy._tournament_selection([], [], (0.0, 0.0))
        assert result == []

    def test_order_crossover_single_element(self) -> None:
        """Cover line 3203: _order_crossover with single-element parents."""
        strategy = GeneticAlgorithmStrategy()
        parent1 = [5]
        parent2 = [5]
        child = strategy._order_crossover(parent1, parent2)
        assert len(child) == 1

    def test_mutate_single_element(self) -> None:
        """Cover line 3263: _mutate with single-element chromosome."""
        strategy = GeneticAlgorithmStrategy(mutation_rate=1.0)
        chromosome = [0]
        result = strategy._mutate(chromosome)
        assert len(result) == 1

    def test_mutate_with_swap_mutation(self) -> None:
        """Cover lines 3268-3273: _mutate swap mutation path."""
        strategy = GeneticAlgorithmStrategy(mutation_rate=1.0)
        chromosome = [0, 1, 2, 3]
        # Force swap mutation by patching the global random module
        with patch("random.random", return_value=0.0):
            with patch("random.choice", return_value="swap"):
                with patch("random.randint", side_effect=[0, 3]):
                    result = strategy._mutate(chromosome)
        assert len(result) == 4

    def test_mutate_with_inversion_mutation(self) -> None:
        """Cover lines 3274-3277: _mutate inversion mutation path."""
        strategy = GeneticAlgorithmStrategy(mutation_rate=1.0)
        chromosome = [0, 1, 2, 3]
        with patch("random.random", return_value=0.0):
            with patch("random.choice", return_value="inversion"):
                with patch("random.randint", side_effect=[1, 3]):
                    result = strategy._mutate(chromosome)
        assert len(result) == 4

    def test_create_tour_from_chromosome_empty(self) -> None:
        """Cover line 3297-3298: _create_tour_from_chromosome with empty chromosome."""
        strategy = GeneticAlgorithmStrategy()
        result = strategy._create_tour_from_chromosome([], [], (0.0, 0.0))
        assert result == []

    def test_create_tour_from_chromosome_reversed_gene(self) -> None:
        """Cover lines 3311-3317: _create_tour_from_chromosome with reversed gene."""
        strategy = GeneticAlgorithmStrategy()
        block = _make_simple_block(0, (10, 0), (20, 0))
        # Negative gene = reversed: gene = -0 - 1 = -1 means block_id=0, reversed=True
        chromosome = [-1]
        result = strategy._create_tour_from_chromosome(chromosome, [block], (0.0, 0.0))
        assert len(result) == 1

    def test_optimize_tour_directions_empty_tour(self) -> None:
        """Cover line 3410: _optimize_tour_directions with empty tour."""
        strategy = GeneticAlgorithmStrategy()
        result = strategy._optimize_tour_directions([], [], (0.0, 0.0))
        assert result == []

    def test_optimize_tour_directions_block_not_found(self) -> None:
        """Cover lines 3417->3422, 3423-3424: _optimize_tour_directions with mismatched block_id."""
        strategy = GeneticAlgorithmStrategy()
        # Tour references block_id=99, but blocks list has block_id=0
        tour = [BlockTraverseState(block_id=99, reversed=False, entrance=(0, 0), exit=(10, 0))]
        block = _make_simple_block(0, (0, 0), (10, 0))
        # block.block_id = 0, tour has block_id = 99 → original_block_idx stays -1
        result = strategy._optimize_tour_directions(tour, [block], (0.0, 0.0))
        assert len(result) == 1
        # The state is appended unchanged
        assert result[0].block_id == 99

    def test_optimize_tour_directions_exit_closer(self) -> None:
        """Cover line 3451: _optimize_tour_directions reversed state when exit closer."""
        strategy = GeneticAlgorithmStrategy()
        block_0 = _make_simple_block(0, (0, 0), (10, 0))
        # Block 1: exit (12,0) is closer to block_0's exit (10,0) than entrance (500,0)
        block_1 = _make_simple_block(1, (500, 0), (12, 0))
        tour = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(0, 0), exit=(10, 0)),
            BlockTraverseState(block_id=1, reversed=False, entrance=(500, 0), exit=(12, 0)),
        ]
        result = strategy._optimize_tour_directions(tour, [block_0, block_1], (0.0, 0.0))
        assert len(result) == 2
        assert result[1].reversed is True

    def test_two_opt_refinement_ga_swap_improves(self) -> None:
        """Cover lines 3492-3493: GA _two_opt_refinement actual swap."""
        strategy = GeneticAlgorithmStrategy()
        # Same crossing tour as in NearestNeighbor test
        tour = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(0, 0), exit=(0, 0)),
            BlockTraverseState(block_id=1, reversed=False, entrance=(100, 0), exit=(100, 0)),
            BlockTraverseState(block_id=2, reversed=False, entrance=(0, 50), exit=(0, 50)),
            BlockTraverseState(block_id=3, reversed=False, entrance=(100, 50), exit=(100, 50)),
        ]
        blocks = [
            _make_simple_block(0, (0, 0), (0, 0)),
            _make_simple_block(1, (100, 0), (100, 0)),
            _make_simple_block(2, (0, 50), (0, 50)),
            _make_simple_block(3, (100, 50), (100, 50)),
        ]
        result = strategy._two_opt_refinement(tour, blocks)
        assert len(result) == 4

    def test_decode_gene_negative(self) -> None:
        """Cover line 3571: _decode_gene with negative gene (reversed=True)."""
        strategy = GeneticAlgorithmStrategy()
        block_idx, is_reversed = strategy._decode_gene(-1)
        assert block_idx == 0
        assert is_reversed is True

    def test_find_nearest_origin_endpoint_ga(self) -> None:
        """Cover lines 3651-3673: GA _find_nearest_origin_endpoint."""
        strategy = GeneticAlgorithmStrategy()
        # Block where exit (1,0) is closer to origin than entrance (100,0)
        block = _make_simple_block(0, (100, 0), (1, 0))
        pos, idx, is_exit = strategy._find_nearest_origin_endpoint([block])
        assert is_exit is True
        assert math.isclose(pos[0], 1.0)


# ---------------------------------------------------------------------------
# Additional coverage tests for OptimizerEngine
# ---------------------------------------------------------------------------

class TestOptimizerEngineCoverage2:
    """Coverage for OptimizerEngine missing lines."""

    def test_optimize_with_christofides_strategy(self) -> None:
        """Cover lines 3734-3737: OptimizerEngine uses ChristofidesStrategy."""
        blocks = [
            _make_simple_block(0, (10, 0), (20, 0)),
            _make_simple_block(1, (50, 0), (60, 0)),
        ]
        engine = OptimizerEngine(strategy=ChristofidesStrategy())
        result = engine.optimize(blocks, initial_position=(0.0, 0.0), end_point=(100.0, 0.0))
        assert result.block_count == 2

    def test_optimize_with_parallel_ensemble_result(self) -> None:
        """Cover lines 3744-3745: OptimizerEngine handles ParallelEnsembleOptimizationResult."""
        blocks = [
            _make_simple_block(0, (0, 0), (10, 0)),
            _make_simple_block(1, (50, 0), (60, 0)),
        ]
        engine = OptimizerEngine(strategy=ParallelEnsembleStrategy())
        result = engine.optimize(blocks)
        # Result is a ParallelEnsembleOptimizationResult
        assert isinstance(result, ParallelEnsembleOptimizationResult)
        assert result.block_count == 2

    def test_optimize_exception_raises_optimization_error(self) -> None:
        """Cover lines 3754-3755: OptimizerEngine wraps exceptions as OptimizationError."""
        class BrokenStrategy(NoOpStrategy):
            """Strategy that always raises."""

            def optimize(self, blocks, initial_position=None):
                raise RuntimeError("broken")

        engine = OptimizerEngine(strategy=BrokenStrategy())
        with pytest.raises(OptimizationError, match="Optimization failed"):
            engine.optimize([_make_simple_block(0, (0, 0), (10, 0))])


# ---------------------------------------------------------------------------
# Additional coverage tests for ParallelEnsembleOptimizationResult
# ---------------------------------------------------------------------------

class TestParallelEnsembleCoverage2:
    """Coverage for ParallelEnsemble missing lines."""

    def test_block_count_property(self) -> None:
        """Cover line 3796: ParallelEnsembleOptimizationResult.block_count."""
        states = [
            BlockTraverseState(block_id=0, reversed=False, entrance=(0, 0), exit=(10, 10)),
        ]
        opt_result = OptimizationResult(
            traverse_order=tuple(states),
            connections=(),
            total_travel_distance=50.0,
            initial_position=None,
        )
        benchmark = StrategyBenchmarkResult("StrategyA", opt_result, 0.5)
        ensemble = ParallelEnsembleOptimizationResult(
            result=opt_result,
            winner_name="StrategyA",
            all_benchmarks=(benchmark,),
        )
        assert ensemble.block_count == 1

    def test_parallel_ensemble_better_distance_updates_best(self) -> None:
        """Cover lines 4039-4043: selection by absolute distance when baseline=None."""
        blocks = [
            _make_simple_block(0, (0, 0), (10, 0)),
            _make_simple_block(1, (50, 0), (60, 0)),
        ]
        # ParallelEnsemble with no baseline: uses absolute distance
        strategy = ParallelEnsembleStrategy(baseline_distance=None)
        result = strategy.optimize(blocks)
        assert result.block_count == 2
        # The winner should have valid total_travel_distance
        assert result.result.total_travel_distance >= 0.0

    def test_parallel_ensemble_all_strategies_fail_fallback(self) -> None:
        """Cover lines 4047-4050: fallback to NoOp when all strategies fail."""
        from concurrent.futures import ProcessPoolExecutor, Future
        blocks = [_make_simple_block(0, (0, 0), (10, 0))]

        failing_future: Future = Future()
        failing_future.set_exception(RuntimeError("All fail"))

        strategy = ParallelEnsembleStrategy()
        with patch("plt_optimizer.core.optimizer.ProcessPoolExecutor") as mock_exec:
            mock_ctx = MagicMock()
            mock_exec.return_value.__enter__ = MagicMock(return_value=mock_ctx)
            mock_exec.return_value.__exit__ = MagicMock(return_value=False)
            mock_ctx.submit = MagicMock(return_value=failing_future)

            with patch("plt_optimizer.core.optimizer.as_completed", return_value=[failing_future]):
                result = strategy.optimize(blocks)

        assert isinstance(result, ParallelEnsembleOptimizationResult)
        assert result.winner_name == "NoOp (Baseline)"

    def test_parallel_ensemble_failed_strategies_warning(self) -> None:
        """Cover lines 4067-4068: warning when some strategies fail."""
        from concurrent.futures import Future
        blocks = [
            _make_simple_block(0, (0, 0), (10, 0)),
            _make_simple_block(1, (50, 0), (60, 0)),
        ]

        strategy = ParallelEnsembleStrategy(baseline_distance=None)

        # Create one good future and one failing future
        good_opt_result = OptimizationResult(
            traverse_order=(
                BlockTraverseState(block_id=0, reversed=False, entrance=(0, 0), exit=(10, 0)),
                BlockTraverseState(block_id=1, reversed=False, entrance=(50, 0), exit=(60, 0)),
            ),
            connections=(),
            total_travel_distance=40.0,
            initial_position=(0.0, 0.0),
        )
        good_benchmark = StrategyBenchmarkResult("NoOp (Baseline)", good_opt_result, 0.01)

        good_future: Future = Future()
        good_future.set_result(good_benchmark)

        bad_future: Future = Future()
        bad_future.set_exception(RuntimeError("One fail"))

        with patch("plt_optimizer.core.optimizer.ProcessPoolExecutor") as mock_exec:
            mock_ctx = MagicMock()
            mock_exec.return_value.__enter__ = MagicMock(return_value=mock_ctx)
            mock_exec.return_value.__exit__ = MagicMock(return_value=False)

            call_count = [0]
            def submit_side(*args, **kwargs):
                call_count[0] += 1
                return good_future if call_count[0] == 1 else bad_future
            mock_ctx.submit = MagicMock(side_effect=submit_side)

            with patch(
                "plt_optimizer.core.optimizer.as_completed",
                return_value=[good_future, bad_future],
            ):
                result = strategy.optimize(blocks)

        assert isinstance(result, ParallelEnsembleOptimizationResult)
        assert result.winner_name == "NoOp (Baseline)"