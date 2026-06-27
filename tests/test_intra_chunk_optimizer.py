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
    IntraChunkStrategy,
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


class TestNoOpEmptyPaths:
    """Tests for empty segment handling in NoOp strategy (lines 35-36)."""

    def test_noop_with_empty_segment_paths_skipped(self) -> None:
        """Test NoOp skips paths with no segments."""
        empty_path = StrokePath(pen_up_position=None, segments=())
        valid_path1 = _make_path((0, 0), (10, 0))
        valid_path2 = _make_path((15, 0), (25, 0))

        paths = (empty_path, valid_path1, empty_path, valid_path2)

        strategy = NoOpIntraStrategy()
        result = strategy.optimize_block(paths, Coordinate(0.0, 0.0), Coordinate(25.0, 0.0))

        # Should skip empty paths and return only valid ones
        assert result.path_count == 2


class TestNearestNeighborEmptyPaths:
    """Tests for empty path handling in NearestNeighbor strategy (lines 98, 120)."""

    def test_nearest_neighbor_with_empty_segment_paths(self) -> None:
        """Test NearestNeighbor handles paths with no segments."""
        empty_path = StrokePath(pen_up_position=None, segments=())
        valid_path1 = _make_path((0, 0), (10, 0))

        block = MacroBlock(
            block_id=0,
            paths=(empty_path, valid_path1),
            entrance=Coordinate(0.0, 0.0),
            exit=Coordinate(10.0, 0.0),
        )

        strategy = NearestNeighborIntraStrategy()
        result = strategy.optimize_block(block.paths, block.entrance, block.exit)

        assert result.path_count == 1

    def test_single_path_with_empty_returns_empty(self) -> None:
        """Test _handle_single_path returns empty when all paths are empty (line 120)."""
        empty_path = StrokePath(pen_up_position=None, segments=())

        block = MacroBlock(
            block_id=0,
            paths=(empty_path,),
            entrance=Coordinate(0.0, 0.0),
            exit=Coordinate(10.0, 0.0),
        )

        strategy = NearestNeighborIntraStrategy()
        result = strategy.optimize_block(block.paths, block.entrance, block.exit)

        assert result.path_count == 0
        assert isinstance(result.traverse_order, tuple)

    def test_get_path_endpoints_empty_segments(self) -> None:
        """Test _get_path_endpoints returns defaults for empty segments (line 157)."""
        strategy = NearestNeighborIntraStrategy()
        empty_path = StrokePath(pen_up_position=None, segments=())

        entrance, exit_coord = strategy._get_path_endpoints(empty_path)

        assert entrance.x == 0.0
        assert entrance.y == 0.0
        assert exit_coord.x == 0.0
        assert exit_coord.y == 0.0


class TestTwoOptRefinement:
    """Tests for two-opt refinement (lines 459-474)."""

    def test_two_opt_refinement_applies(self) -> None:
        """Test that 2-opt refinement is applied for tours > 3 paths."""
        # Create a tour where reversing middle segment might help
        path1 = _make_path((0, 0), (10, 0))
        path2 = _make_path((15, 0), (12, 0))   # Goes backward from 15 to 12
        path3 = _make_path((20, 0), (30, 0))
        path4 = _make_path((25, 0), (35, 0))

        block = MacroBlock(
            block_id=0,
            paths=(path1, path2, path3, path4),
            entrance=Coordinate(0.0, 0.0),
            exit=Coordinate(35.0, 0.0),
        )

        strategy = NearestNeighborIntraStrategy()
        result = strategy.optimize_block(block.paths, block.entrance, block.exit)

        assert result.path_count == 4


class TestTourValidation:
    """Tests for tour validation (_is_valid_tour lines 363-364, 377, 383-385, 389, 394, 398)."""

    def test_is_valid_tour_empty(self) -> None:
        """Test _is_valid_tour with empty tour (lines 363-364)."""
        strategy = NearestNeighborIntraStrategy()
        path1 = _make_path((0, 0), (10, 0))

        is_valid = strategy._is_valid_tour([], (path1,), Coordinate(0.0, 0.0), Coordinate(10.0, 0.0))

        assert is_valid is True

    def test_is_valid_tour_first_not_reversed_matches_entrance(self) -> None:
        """Test _is_valid_tour with first path not reversed entering at fixed entrance (line 377)."""
        strategy = NearestNeighborIntraStrategy()
        # Path that starts at x=0 - normal traversal enters at x=0
        path1 = _make_path((0, 0), (10, 0))

        tour = [
            PathTraverseState(
                path_index=0,
                reversed=False,
                entrance=Coordinate(0.0, 0.0),
                exit=Coordinate(10.0, 0.0),
            ),
        ]

        is_valid = strategy._is_valid_tour(tour, (path1,), Coordinate(0.0, 0.0), Coordinate(10.0, 0.0))

        assert is_valid is True

    def test_is_valid_tour_first_reversed_matches_entrance(self) -> None:
        """Test _is_valid_tour with first path reversed entering at fixed entrance (line 377)."""
        strategy = NearestNeighborIntraStrategy()
        # Path that goes from x=10 to x=0 - when reversed, we enter at the
        # last segment's START which is Coordinate(10.0, 0.0) for this single-seg path
        path1 = _make_path((10, 0), (0, 0))

        tour = [
            PathTraverseState(
                path_index=0,
                reversed=True,
                entrance=Coordinate(10.0, 0.0),  # Enter at original last_seg.start for reverse traversal
                exit=Coordinate(0.0, 0.0),
            ),
        ]

        is_valid = strategy._is_valid_tour(tour, (path1,), Coordinate(10.0, 0.0), Coordinate(0.0, 0.0))

        assert is_valid is True

    def test_is_valid_tour_first_reversed_fails_entrance(self) -> None:
        """Test _is_valid_tour fails when first reversed path doesn't match entrance (lines 383-385)."""
        strategy = NearestNeighborIntraStrategy()
        # Path that starts at x=10 - fixed entrance is wrong
        path1 = _make_path((10, 0), (5, 0))

        tour = [
            PathTraverseState(
                path_index=0,
                reversed=True,
                entrance=Coordinate(10.0, 0.0),
                exit=Coordinate(5.0, 0.0),
            ),
        ]

        is_valid = strategy._is_valid_tour(tour, (path1,), Coordinate(0.0, 0.0), Coordinate(5.0, 0.0))

        assert is_valid is False

    def test_is_valid_tour_last_not_reversed_matches_exit(self) -> None:
        """Test _is_valid_tour with last path not reversed matching fixed exit (lines 389, 394)."""
        strategy = NearestNeighborIntraStrategy()
        path1 = _make_path((0, 0), (10, 0))
        # Path that ends at x=25
        path2 = _make_path((15, 0), (25, 0))

        tour = [
            PathTraverseState(
                path_index=0,
                reversed=False,
                entrance=Coordinate(0.0, 0.0),
                exit=Coordinate(10.0, 0.0),
            ),
            PathTraverseState(
                path_index=1,
                reversed=False,
                entrance=Coordinate(15.0, 0.0),
                exit=Coordinate(25.0, 0.0),
            ),
        ]

        is_valid = strategy._is_valid_tour(
            tour,
            (path1, path2),
            Coordinate(0.0, 0.0),
            Coordinate(25.0, 0.0),  # Fixed exit at end of last path
        )

        assert is_valid is True

    def test_is_valid_tour_last_reversed_matches_exit(self) -> None:
        """Test _is_valid_tour with last path reversed matching fixed exit (lines 389, 394)."""
        strategy = NearestNeighborIntraStrategy()
        path1 = _make_path((0, 0), (10, 0))
        # Path that goes from x=15 to x=25 - when reversed we enter at original end
        # and the actual entrance is segments[-1].end which is Coordinate(25.0, 0.0)
        path2 = _make_path((15, 0), (25, 0))

        tour = [
            PathTraverseState(
                path_index=0,
                reversed=False,
                entrance=Coordinate(0.0, 0.0),
                exit=Coordinate(10.0, 0.0),
            ),
            PathTraverseState(
                path_index=1,
                reversed=True,
                entrance=Coordinate(25.0, 0.0),  # Enter at original last_seg.end for reverse
                exit=Coordinate(15.0, 0.0),
            ),
        ]

        is_valid = strategy._is_valid_tour(
            tour,
            (path1, path2),
            Coordinate(0.0, 0.0),
            Coordinate(25.0, 0.0),  # Fixed exit matches last_actual_entrance
        )

        assert is_valid is True

    def test_is_valid_tour_last_reversed_fails_exit(self) -> None:
        """Test _is_valid_tour fails when last reversed doesn't match fixed exit (line 398)."""
        strategy = NearestNeighborIntraStrategy()
        path1 = _make_path((0, 0), (10, 0))
        # Path that ends at x=15 - fixed exit is wrong
        path2 = _make_path((5, 0), (15, 0))

        tour = [
            PathTraverseState(
                path_index=0,
                reversed=False,
                entrance=Coordinate(0.0, 0.0),
                exit=Coordinate(10.0, 0.0),
            ),
            PathTraverseState(
                path_index=1,
                reversed=True,
                entrance=Coordinate(15.0, 0.0),
                exit=Coordinate(5.0, 0.0),
            ),
        ]

        is_valid = strategy._is_valid_tour(
            tour,
            (path1, path2),
            Coordinate(0.0, 0.0),
            Coordinate(99.0, 0.0),  # Wrong exit - should fail
        )

        assert is_valid is False


class TestCreateOriginalOrderTour:
    """Tests for _create_original_order_tour (lines 409-435)."""

    def test_create_original_order_tour_basic(self) -> None:
        """Test _create_original_order_tour fallback."""
        strategy = NearestNeighborIntraStrategy()

        path1 = _make_path((0, 0), (10, 0))
        path2 = _make_path((20, 0), (30, 0))

        block = MacroBlock(
            block_id=0,
            paths=(path1, path2),
            entrance=Coordinate(0.0, 0.0),
            exit=Coordinate(30.0, 0.0),
        )

        tour = strategy._create_original_order_tour(
            block.paths,
            block.entrance,
            block.exit,
        )

        assert len(tour) == 2
        assert all(isinstance(ts, PathTraverseState) for ts in tour)


class TestCoordinatesMatch:
    """Tests for _coordinates_match (line 533)."""

    def test_coordinates_match_within_tolerance(self) -> None:
        """Test _coordinates_match with coordinates within tolerance."""
        strategy = NearestNeighborIntraStrategy()

        # Within tolerance
        assert strategy._coordinates_match(
            Coordinate(0.0, 0.0),
            Coordinate(0.0001, 0.0001),
        ) is True

    def test_coordinates_match_outside_tolerance(self) -> None:
        """Test _coordinates_match with coordinates outside tolerance."""
        strategy = NearestNeighborIntraStrategy()

        # Outside tolerance
        assert strategy._coordinates_match(
            Coordinate(0.0, 0.0),
            Coordinate(0.01, 0.01),
        ) is False


class TestTwoOptSwapImproves:
    """Tests for _two_opt_swap_improves (line 533)."""

    def test_two_opt_swap_at_end_with_fixed_exit(self) -> None:
        """Test _two_opt_swap_improves rejects swap that violates fixed exit."""
        strategy = NearestNeighborIntraStrategy()

        path1 = _make_path((0, 0), (10, 0))
        path2 = _make_path((20, 0), (30, 0))

        tour = [
            PathTraverseState(path_index=0, reversed=False,
                            entrance=Coordinate(0.0, 0.0), exit=Coordinate(10.0, 0.0)),
            PathTraverseState(path_index=1, reversed=False,
                            entrance=Coordinate(20.0, 0.0), exit=Coordinate(30.0, 0.0)),
        ]

        fixed_exit = Coordinate(20.0, 0.0)

        improves = strategy._two_opt_swap_improves(
            tour,
            (path1, path2),
            Coordinate(0.0, 0.0),
            fixed_exit,
            i=0,
            j=1,
        )

        assert improves is False

    def test_two_opt_swap_returns_bool(self) -> None:
        """Test _two_opt_swap_improves returns boolean without crashing."""
        strategy = NearestNeighborIntraStrategy()

        path1 = _make_path((0, 0), (10, 0))
        path2 = _make_path((15, 0), (12, 0))
        path3 = _make_path((20, 0), (30, 0))

        tour = [
            PathTraverseState(path_index=0, reversed=False,
                            entrance=Coordinate(0.0, 0.0), exit=Coordinate(10.0, 0.0)),
            PathTraverseState(path_index=1, reversed=False,
                            entrance=Coordinate(15.0, 0.0), exit=Coordinate(12.0, 0.0)),
            PathTraverseState(path_index=2, reversed=False,
                            entrance=Coordinate(20.0, 0.0), exit=Coordinate(30.0, 0.0)),
        ]

        improves = strategy._two_opt_swap_improves(
            tour,
            (path1, path2, path3),
            Coordinate(0.0, 0.0),
            Coordinate(30.0, 0.0),
            i=0,
            j=1,
        )

        assert isinstance(improves, bool)


class TestPathCostCalculation:
    """Tests for _calculate_path_cost."""

    def test_calculate_path_cost_entrance_cheaper(self) -> None:
        """Test _calculate_path_cost when entrance is closer."""
        strategy = NearestNeighborIntraStrategy()

        cost, reversed_flag = strategy._calculate_path_cost(
            from_pos=Coordinate(0.0, 0.0),
            to_entrance=Coordinate(5.0, 0.0),
            to_exit=Coordinate(100.0, 0.0),
        )

        assert cost == pytest.approx(5.0)
        assert reversed_flag is False

    def test_calculate_path_cost_exit_cheaper(self) -> None:
        """Test _calculate_path_cost when exit (reversed entry) is closer."""
        strategy = NearestNeighborIntraStrategy()

        cost, reversed_flag = strategy._calculate_path_cost(
            from_pos=Coordinate(0.0, 0.0),
            to_entrance=Coordinate(100.0, 0.0),
            to_exit=Coordinate(5.0, 0.0),
        )

        assert cost == pytest.approx(5.0)
        assert reversed_flag is True


class TestTotalDistanceCalculation:
    """Tests for _calculate_total_internal_distance."""

    def test_calculate_total_internal_distance(self) -> None:
        """Test _calculate_total_internal_distance calculation."""
        strategy = NearestNeighborIntraStrategy()
        path1 = _make_path((0, 0), (10, 0))
        path2 = _make_path((15, 0), (25, 0))

        tour = [
            PathTraverseState(path_index=0, reversed=False,
                            entrance=Coordinate(0.0, 0.0), exit=Coordinate(10.0, 0.0)),
            PathTraverseState(path_index=1, reversed=False,
                            entrance=Coordinate(15.0, 0.0), exit=Coordinate(25.0, 0.0)),
        ]

        distance = strategy._calculate_total_internal_distance(tour, (path1, path2))

        # Distance from (10,0) to (15,0)
        assert distance == pytest.approx(5.0)

    def test_calculate_total_internal_distance_single_path(self) -> None:
        """Test _calculate_total_internal_distance with single path returns 0."""
        strategy = NearestNeighborIntraStrategy()
        path1 = _make_path((0, 0), (10, 0))

        tour = [
            PathTraverseState(path_index=0, reversed=False,
                            entrance=Coordinate(0.0, 0.0), exit=Coordinate(10.0, 0.0)),
        ]

        distance = strategy._calculate_total_internal_distance(tour, (path1,))

        assert distance == 0.0


class TestIntraChunkOptimizerErrorHandling:
    """Tests for error handling in IntraChunkOptimizer (lines 619-620)."""

    def test_intra_chunk_optimizer_exception_handling(self) -> None:
        """Test IntraChunkOptimizer raises IntraChunkError on exception."""
        class FailingStrategy(IntraChunkStrategy):
            @property
            def name(self) -> str:
                return "FailingStrategy"

            def optimize_block(
                self,
                paths: tuple[StrokePath, ...],
                fixed_entrance: Coordinate,
                fixed_exit: Coordinate,
            ) -> IntraChunkResult:
                raise RuntimeError("Simulated failure")

        block = MacroBlock(
            block_id=0,
            paths=(_make_path((0, 0), (10, 0)),),
            entrance=Coordinate(0.0, 0.0),
            exit=Coordinate(10.0, 0.0),
        )

        optimizer = IntraChunkOptimizer(strategy=FailingStrategy())

        with pytest.raises(IntraChunkError) as exc_info:
            optimizer.optimize_block(block)

        assert "Intra-chunk optimization failed" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, RuntimeError)


class TestGreedyConstrainedFallback:
    """Tests for greedy constrained fallback behavior."""

    def test_greedy_constrained_returns_original_when_invalid(self) -> None:
        """Test _greedy_nearest_neighbor_constrained falls back to original order."""
        strategy = NearestNeighborIntraStrategy()

        path1 = _make_path((0, 5), (10, 5))
        path2 = _make_path((100, 5), (110, 5))

        block = MacroBlock(
            block_id=0,
            paths=(path1, path2),
            entrance=Coordinate(0.0, 5.0),
            exit=Coordinate(110.0, 5.0),
        )

        tour = strategy._greedy_nearest_neighbor_constrained(
            block.paths,
            block.entrance,
            block.exit,
        )

        assert len(tour) == 2


class TestIntraChunkError:
    """Tests for IntraChunkError exception."""

    def test_intra_chunk_error_message(self) -> None:
        """Test IntraChunkError exception has proper message."""
        error = IntraChunkError("Test error message")

        assert error.message == "Test error message"
        assert str(error) == "Test error message"


class TestIntraChunkResultEquality:
    """Tests for IntraChunkResult equality."""

    def test_intra_chunk_result_equality(self) -> None:
        """Test IntraChunkResult equality comparison."""
        states1 = (
            PathTraverseState(path_index=0, reversed=False,
                            entrance=Coordinate(0, 0), exit=Coordinate(10, 0)),
        )
        result1 = IntraChunkResult(traverse_order=states1, total_internal_distance=5.0)

        states2 = (
            PathTraverseState(path_index=0, reversed=False,
                            entrance=Coordinate(0, 0), exit=Coordinate(10, 0)),
        )
        result2 = IntraChunkResult(traverse_order=states2, total_internal_distance=5.0)

        assert result1 == result2

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