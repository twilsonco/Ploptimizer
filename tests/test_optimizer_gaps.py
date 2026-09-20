"""Gap-closing tests for plt_optimizer/core/optimizer.py.

These tests target the residual statement/branch coverage gaps in the core
optimizer module (insertion heuristic, Christofides candidate pruning,
simulated annealing improvement tracking, genetic algorithm edge cases, and
the parallel ensemble result-selection logic). The parallel ensemble tests
fake the process pool entirely so no real subprocesses are spawned.
"""

from __future__ import annotations

import itertools
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest

from plt_optimizer.core.chunker import MacroBlock
from plt_optimizer.core.models import Coordinate, StrokePath, StrokeSegment
from plt_optimizer.core.optimizer import (
    BlockTraverseState,
    ChristofidesStrategy,
    GeneticAlgorithmStrategy,
    InsertionHeuristicStrategy,
    OptimizationResult,
    ParallelEnsembleStrategy,
    SimulatedAnnealingStrategy,
    StrategyBenchmarkResult,
)


def _make_block(
    block_id: int,
    paths: List[StrokePath],
) -> MacroBlock:
    """Helper to create a MacroBlock with entrance/exit from first/last segments.

    Args:
        block_id: Identifier for the block.
        paths: Stroke paths composing the block.

    Returns:
        A MacroBlock spanning the provided paths.
    """
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
    """Helper to create a simple single-segment MacroBlock.

    Args:
        block_id: Identifier for the block.
        start: Entrance coordinate.
        end: Exit coordinate.

    Returns:
        A single straight-cut MacroBlock from start to end.
    """
    segment = StrokeSegment(
        start=Coordinate(x=start[0], y=start[1]),
        end=Coordinate(x=end[0], y=end[1]),
        is_cutting=True,
    )
    path = StrokePath(pen_up_position=None, segments=(segment,))
    return _make_block(block_id, [path])


def _make_state(
    block_id: int,
    reversed_flag: bool,
    entrance: Tuple[float, float],
    exit_pos: Tuple[float, float],
) -> BlockTraverseState:
    """Build a BlockTraverseState with explicit endpoints.

    Args:
        block_id: Identifier of the referenced block.
        reversed_flag: Whether the block is traversed in reverse.
        entrance: Effective entrance coordinate.
        exit_pos: Effective exit coordinate.

    Returns:
        The constructed traverse state.
    """
    return BlockTraverseState(
        block_id=block_id,
        reversed=reversed_flag,
        entrance=entrance,
        exit=exit_pos,
    )


class TestInsertionHeuristicGapBranches:
    """Branch gaps in InsertionHeuristicStrategy (lines 889, 1096, 1262)."""

    def test_optimize_rejects_worse_candidate_in_insertion_round(self) -> None:
        """Cover 889->885: a later unvisited block has worse insertion cost.

        With a 2-block seed tour and two remaining far blocks, the second
        candidate evaluated in the round cannot beat the first, taking the
        False side of the best-insertion comparison.
        """
        blocks = [
            _make_simple_block(0, (0, 0), (1, 0)),
            _make_simple_block(1, (2, 0), (3, 0)),
            _make_simple_block(2, (100, 0), (101, 0)),
            _make_simple_block(3, (200, 0), (201, 0)),
        ]
        strategy = InsertionHeuristicStrategy()

        result = strategy.optimize(blocks)

        assert result.block_count == 4
        assert {s.block_id for s in result.traverse_order} == {0, 1, 2, 3}

    def test_build_initial_tour_from_start_skips_farther_second_candidates(self) -> None:
        """Cover 1096->1101: a later block's exit is not closer than the running min."""
        blocks = [
            _make_simple_block(0, (0, 0), (1, 0)),
            _make_simple_block(1, (2, 0), (3, 0)),
            _make_simple_block(2, (500, 0), (600, 0)),
        ]
        strategy = InsertionHeuristicStrategy()

        tour = strategy._build_initial_tour_from_start(blocks, (0.0, 0.0))

        assert len(tour) == 2
        assert tour[0].block_id == 0
        assert tour[1].block_id == 1

    def test_find_best_insertion_position_rejects_worse_position(self) -> None:
        """Cover 1262->1259: a later insertion position has cost >= current best."""
        blocks = [
            _make_simple_block(0, (-1, 0), (0, 0)),
            _make_simple_block(1, (10, 0), (20, 0)),
            _make_simple_block(2, (5, 0), (6, 0)),
        ]
        strategy = InsertionHeuristicStrategy()
        tour = [
            _make_state(0, False, (-1.0, 0.0), (0.0, 0.0)),
            _make_state(1, False, (10.0, 0.0), (20.0, 0.0)),
        ]

        best_pos, best_state, best_cost = strategy._find_best_insertion_position(
            blocks[2], tour, blocks
        )

        # Position 0 (between the two tour states) costs 5 + 4 = 9 in the
        # best orientation, while appending at position 1 costs at least 14,
        # so the second position takes the False side of the comparison.
        assert best_pos == 0
        assert best_cost == pytest.approx(9.0)
        assert best_state.block_id == 2
        assert best_state.reversed is False


class TestChristofidesCandidatePruning:
    """Statement gap in ChristofidesStrategy candidate loop (line 1385)."""

    def test_optimize_skips_same_block_start_and_end_candidate(self) -> None:
        """Cover 1385: continue when start and end candidates share one block.

        Block 0 owns both the closest endpoint to origin (0.5, 0) and the
        farthest endpoint (1000, 0), so it appears in both candidate lists
        and the (start=block0, end=block0) combination must be skipped.
        """
        blocks = [
            _make_simple_block(0, (0.5, 0), (1000, 0)),
            _make_simple_block(1, (1, 0), (2, 0)),
            _make_simple_block(2, (3, 0), (4, 0)),
        ]
        strategy = ChristofidesStrategy()

        result = strategy.optimize(blocks, start_point=(0.0, 0.0), end_point=(1000.0, 0.0))

        assert result.block_count == 3
        assert {s.block_id for s in result.traverse_order} == {0, 1, 2}


class TestSimulatedAnnealingGapBranches:
    """Branch/statement gaps in SimulatedAnnealingStrategy (2449, 2680)."""

    def test_optimize_from_start_tracks_improving_accepted_neighbor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cover 2449-2450: an accepted neighbor improves on the best cost.

        The greedy initial tour is deliberately sub-optimal (entering the
        long block first strands the cursor at x=1000). ``_generate_neighbor``
        is patched to always return the brute-force best permutation of the
        greedy states, guaranteeing a strictly improving first move without
        relying on RNG luck.
        """
        blocks = [
            _make_simple_block(0, (1, 0), (1000, 0)),
            _make_simple_block(1, (2, 0), (3, 0)),
            _make_simple_block(2, (4, 0), (5, 0)),
            _make_simple_block(3, (6, 0), (7, 0)),
        ]
        strategy = SimulatedAnnealingStrategy(
            initial_temperature=1.0,
            cooling_rate=0.5,
            iterations_per_temp=2,
            min_temperature=0.4,
        )
        start_pos = (0.0, 0.0)

        greedy_tour = strategy._generate_initial_tour(blocks, start_pos)
        greedy_cost = strategy._calculate_tour_distance(greedy_tour, blocks)
        best_perm = min(
            itertools.permutations(greedy_tour),
            key=lambda perm: strategy._calculate_tour_distance(list(perm), blocks),
        )
        best_cost = strategy._calculate_tour_distance(list(best_perm), blocks)
        assert best_cost < greedy_cost

        monkeypatch.setattr(strategy, "_generate_neighbor", lambda tour: list(best_perm))

        result = strategy._optimize_from_start(blocks, start_pos)

        assert result.total_travel_distance == pytest.approx(best_cost)

    def test_find_nearest_origin_endpoint_skips_farther_later_block(self) -> None:
        """Cover 2680->2686: a later block's entrance is not closer than the min."""
        blocks = [
            _make_simple_block(0, (1, 0), (2, 0)),
            _make_simple_block(1, (5, 0), (6, 0)),
        ]
        strategy = SimulatedAnnealingStrategy()

        pos, idx, is_exit = strategy._find_nearest_origin_endpoint(blocks)

        assert idx == 0
        assert is_exit is False
        assert pos[0] == pytest.approx(1.0)
        assert pos[1] == pytest.approx(0.0)


class TestGeneticAlgorithmGapBranches:
    """Branch/statement gaps in GeneticAlgorithmStrategy (2938, 2956, 2965, 3273, 3313, 3666)."""

    def test_elitism_loop_tolerates_exhausted_fitness_scores(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cover 2938->2937: elitism count exceeds population, emptying fitness scores."""
        blocks = [
            _make_simple_block(0, (0, 0), (10, 0)),
            _make_simple_block(1, (50, 0), (60, 0)),
            _make_simple_block(2, (100, 0), (110, 0)),
            _make_simple_block(3, (150, 0), (160, 0)),
        ]
        strategy = GeneticAlgorithmStrategy(
            population_size=1,
            generations=1,
            elitism_count=2,
            tournament_size=1,
        )

        result = strategy._optimize_from_start(blocks, (0.0, 0.0))

        assert result.block_count == 4

    def test_zero_generations_falls_back_to_population_min(self) -> None:
        """Cover 2956-2958: generations=0 leaves best_chromosome None; pick from population."""
        blocks = [
            _make_simple_block(0, (0, 0), (10, 0)),
            _make_simple_block(1, (50, 0), (60, 0)),
            _make_simple_block(2, (100, 0), (110, 0)),
            _make_simple_block(3, (150, 0), (160, 0)),
        ]
        strategy = GeneticAlgorithmStrategy(population_size=4, generations=0)

        result = strategy._optimize_from_start(blocks, (0.0, 0.0))

        assert result.block_count == 4

    def test_final_tour_of_exactly_three_skips_two_opt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cover 2965->2968: final tour length is exactly 3, so 2-opt is skipped.

        ``_greedy_initial_tour`` is patched to report a 4-state tour so the
        early ``len(tour) < 4`` guard passes, while the underlying block list
        only has 3 blocks; the deduplicated chromosome tour therefore has
        length 3 and the 2-opt refinement branch is not taken.
        """
        blocks = [
            _make_simple_block(0, (0, 0), (10, 0)),
            _make_simple_block(1, (50, 0), (60, 0)),
            _make_simple_block(2, (100, 0), (110, 0)),
        ]
        strategy = GeneticAlgorithmStrategy(population_size=3, generations=0)

        real_greedy = strategy._greedy_initial_tour

        def _padded_greedy(
            blks: List[MacroBlock], start: Tuple[float, float]
        ) -> List[BlockTraverseState]:
            tour = real_greedy(blks, start)
            return tour + [tour[0]]

        monkeypatch.setattr(strategy, "_greedy_initial_tour", _padded_greedy)

        result = strategy._optimize_from_start(blocks, (0.0, 0.0))

        assert len(result.traverse_order) == 3
        assert {s.block_id for s in result.traverse_order} == {0, 1, 2}

    def test_mutate_returns_copy_when_random_exceeds_rate(self) -> None:
        """Cover 3273->3285: random value above mutation_rate skips mutation."""
        strategy = GeneticAlgorithmStrategy(mutation_rate=0.5)
        chromosome = [1, 2, 3]

        with patch("random.random", return_value=1.0):
            result = strategy._mutate(chromosome)

        assert result == [1, 2, 3]
        assert result is not chromosome

    def test_create_tour_from_chromosome_skips_duplicate_genes(self) -> None:
        """Cover 3313: duplicate block index in chromosome hits the continue."""
        blocks = [
            _make_simple_block(0, (0, 0), (10, 0)),
            _make_simple_block(1, (50, 0), (60, 0)),
        ]
        strategy = GeneticAlgorithmStrategy()

        tour = strategy._create_tour_from_chromosome([0, 0, 1], blocks, (0.0, 0.0))

        assert len(tour) == 2
        assert [s.block_id for s in tour] == [0, 1]

    def test_find_nearest_origin_endpoint_skips_farther_endpoints(self) -> None:
        """Cover 3666->3672 and 3673->3662.

        Block 0's exit is farther from origin than its own entrance
        (3673->3662), and block 1's entrance is farther than the running
        minimum (3666->3672).
        """
        blocks = [
            _make_simple_block(0, (1, 0), (5, 0)),
            _make_simple_block(1, (7, 0), (9, 0)),
        ]
        strategy = GeneticAlgorithmStrategy()

        pos, idx, is_exit = strategy._find_nearest_origin_endpoint(blocks)

        assert idx == 0
        assert is_exit is False
        assert pos[0] == pytest.approx(1.0)
        assert pos[1] == pytest.approx(0.0)


class TestParallelEnsembleResultSelection:
    """Gaps in ParallelEnsembleStrategy result selection (4040, 4047, 4074).

    The process pool is replaced with an in-process fake so no subprocesses
    are spawned and completion order is fully deterministic.
    """

    @staticmethod
    def _benchmark(strategy_name: str, distance: float) -> StrategyBenchmarkResult:
        """Build a minimal StrategyBenchmarkResult for the fake pool.

        Args:
            strategy_name: Name reported by the fake strategy.
            distance: Total travel distance of the fake result.

        Returns:
            A benchmark result carrying an empty traverse order.
        """
        opt_result = OptimizationResult(
            traverse_order=(),
            connections=(),
            total_travel_distance=distance,
            initial_position=(0.0, 0.0),
        )
        return StrategyBenchmarkResult(
            strategy_name=strategy_name,
            result=opt_result,
            execution_time_seconds=0.001,
        )

    @staticmethod
    def _install_fake_pool(
        monkeypatch: pytest.MonkeyPatch,
        outcomes: Dict[str, Any],
    ) -> None:
        """Replace ProcessPoolExecutor/as_completed in the optimizer namespace.

        Args:
            monkeypatch: pytest monkeypatch fixture.
            outcomes: Mapping of strategy name to either a
                StrategyBenchmarkResult (returned) or an Exception (raised).
        """

        class _FakeFuture:
            """Future stub returning a preset value or raising a preset error."""

            def __init__(
                self, value: Optional[StrategyBenchmarkResult], error: Optional[Exception]
            ):
                self._value = value
                self._error = error

            def result(self) -> StrategyBenchmarkResult:
                """Return the preset value or raise the preset error."""
                if self._error is not None:
                    raise self._error
                assert self._value is not None
                return self._value

        class _FakeExecutor:
            """Executor stub submitting pre-resolved futures."""

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self._futures: List[Tuple[str, _FakeFuture]] = []

            def __enter__(self) -> _FakeExecutor:
                """Enter the context manager."""
                return self

            def __exit__(self, *exc: Any) -> bool:
                """Exit the context manager."""
                return False

            def submit(
                self,
                fn: Any,
                strategy_name: str,
                blocks_serialized: Any,
                initial_position: Any,
            ) -> _FakeFuture:
                """Record a submission and return its pre-resolved future."""
                outcome = outcomes[strategy_name]
                if isinstance(outcome, Exception):
                    future = _FakeFuture(value=None, error=outcome)
                else:
                    future = _FakeFuture(value=outcome, error=None)
                self._futures.append((strategy_name, future))
                return future

        def _fake_as_completed(futures: Any) -> Any:
            """Yield submitted futures in submission order."""
            return iter(list(futures))

        monkeypatch.setattr("plt_optimizer.core.optimizer.ProcessPoolExecutor", _FakeExecutor)
        monkeypatch.setattr("plt_optimizer.core.optimizer.as_completed", _fake_as_completed)

    def test_distance_selection_picks_closer_second_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cover 4047 + 4074->4081: no baseline, closer result wins, zero failures."""
        blocks = [
            _make_simple_block(0, (0, 0), (10, 0)),
            _make_simple_block(1, (50, 0), (60, 0)),
        ]
        outcomes: Dict[str, Any] = {
            "NoOp (Baseline)": self._benchmark("NoOp (Baseline)", 10.0),
            "NearestNeighbor + 2-Opt": self._benchmark("NearestNeighbor + 2-Opt", 10.0),
            "Insertion Heuristic": self._benchmark("Insertion Heuristic", 5.0),
            "Simulated Annealing": self._benchmark("Simulated Annealing", 10.0),
            "Genetic Algorithm": self._benchmark("Genetic Algorithm", 10.0),
            "Christofides-Serdyukov S-T Path (5/3 approx)": self._benchmark(
                "Christofides-Serdyukov S-T Path (5/3 approx)", 10.0
            ),
        }
        self._install_fake_pool(monkeypatch, outcomes)
        strategy = ParallelEnsembleStrategy()

        result = strategy.optimize(blocks)

        assert result.winner_name == "Insertion Heuristic"
        assert result.total_travel_distance == pytest.approx(5.0)
        assert len(result.all_benchmarks) == 6

    def test_baseline_selection_picks_higher_improvement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cover 4040: with a baseline, the higher improvement percent wins."""
        blocks = [
            _make_simple_block(0, (0, 0), (10, 0)),
            _make_simple_block(1, (50, 0), (60, 0)),
        ]
        outcomes: Dict[str, Any] = {
            "NoOp (Baseline)": self._benchmark("NoOp (Baseline)", 90.0),
            "NearestNeighbor + 2-Opt": self._benchmark("NearestNeighbor + 2-Opt", 90.0),
            "Insertion Heuristic": self._benchmark("Insertion Heuristic", 50.0),
            "Simulated Annealing": self._benchmark("Simulated Annealing", 90.0),
            "Genetic Algorithm": self._benchmark("Genetic Algorithm", 90.0),
            "Christofides-Serdyukov S-T Path (5/3 approx)": self._benchmark(
                "Christofides-Serdyukov S-T Path (5/3 approx)", 90.0
            ),
        }
        self._install_fake_pool(monkeypatch, outcomes)
        strategy = ParallelEnsembleStrategy(baseline_distance=100.0)

        result = strategy.optimize(blocks)

        assert result.winner_name == "Insertion Heuristic"
        assert result.total_travel_distance == pytest.approx(50.0)
        winner = next(b for b in result.all_benchmarks if b.strategy_name == "Insertion Heuristic")
        assert winner.improvement_percent == pytest.approx(50.0)

    def test_failed_strategy_is_skipped_and_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cover the failed-strategy warning path with a raising future."""
        blocks = [
            _make_simple_block(0, (0, 0), (10, 0)),
            _make_simple_block(1, (50, 0), (60, 0)),
        ]
        outcomes: Dict[str, Any] = {
            "NoOp (Baseline)": RuntimeError("boom"),
            "NearestNeighbor + 2-Opt": self._benchmark("NearestNeighbor + 2-Opt", 7.0),
            "Insertion Heuristic": self._benchmark("Insertion Heuristic", 8.0),
            "Simulated Annealing": self._benchmark("Simulated Annealing", 9.0),
            "Genetic Algorithm": self._benchmark("Genetic Algorithm", 9.5),
            "Christofides-Serdyukov S-T Path (5/3 approx)": self._benchmark(
                "Christofides-Serdyukov S-T Path (5/3 approx)", 9.9
            ),
        }
        self._install_fake_pool(monkeypatch, outcomes)
        strategy = ParallelEnsembleStrategy()

        result = strategy.optimize(blocks)

        assert result.winner_name == "NearestNeighbor + 2-Opt"
        assert result.total_travel_distance == pytest.approx(7.0)
        assert len(result.all_benchmarks) == 5
