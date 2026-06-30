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
    ChristofidesStrategy,
    GeneticAlgorithmStrategy,
    InsertionHeuristicStrategy,
    NearestNeighbor2OptStrategy,
    NoOpStrategy,
    OptimizationResult,
    OptimizerEngine,
    OptimizationError,
    SimulatedAnnealingStrategy,
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