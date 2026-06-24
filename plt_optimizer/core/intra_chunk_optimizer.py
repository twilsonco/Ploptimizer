"""Intra-chunk path optimization for stroke paths within a MacroBlock.

This module implements optimization of stroke path order and direction *within*
a single MacroBlock, while keeping the block's entrance and exit points fixed.
This reduces internal rapid travel without affecting inter-chunk routing.

The constraint of fixed entrance/exit makes this a Graphical Traveling Salesperson
Problem with fixed endpoints - tractable with nearest-neighbor + 2-opt approach.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass

from plt_optimizer.core.chunker import MacroBlock
from plt_optimizer.core.models import Coordinate, StrokePath
from plt_optimizer.utils.logging import get_text_logger


class IntraChunkError(Exception):
    """Exception raised when intra-chunk optimization fails.

    Attributes:
        message: Human-readable error description.
    """

    def __init__(self, message: str) -> None:
        """Initialize an IntraChunkError.

        Args:
            message: Error description.
        """
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class PathTraverseState:
    """Represents whether a StrokePath should be traversed forward or reverse.

    Attributes:
        path_index: Index of the path in the original block's paths tuple.
        reversed: True if this path's segments should be traversed in reverse order.
        entrance: The actual coordinate where we enter this path (may differ from
            original first segment start when reversed).
        exit: The actual coordinate where we exit this path (may differ from
            original last segment end when reversed).
    """

    path_index: int
    reversed: bool
    entrance: Coordinate
    exit: Coordinate


@dataclass(frozen=True)
class IntraChunkResult:
    """Results from optimizing paths within a single MacroBlock.

    Attributes:
        traverse_order: List of PathTraverseState describing how each path
            should be traversed (direction and entry point).
        total_internal_distance: Sum of rapid travel distances between paths
            in the optimized internal order.
    """

    traverse_order: tuple[PathTraverseState, ...]
    total_internal_distance: float

    @property
    def path_count(self) -> int:
        """Return number of paths in the optimized traversal."""
        return len(self.traverse_order)


class IntraChunkStrategy(ABC):
    """Abstract base class for intra-chunk optimization strategies.

    Strategies are responsible for determining both the sequence and direction
    of stroke path traversal within a single MacroBlock to minimize internal
    rapid travel distance while respecting fixed entrance/exit constraints.
    """

    def __init__(self) -> None:
        """Initialize the intra-chunk strategy."""
        self._logger = get_text_logger()

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the human-readable name of this strategy.

        Returns:
            Strategy name for logging and identification.
        """
        ...

    @abstractmethod
    def optimize_block(
        self,
        paths: tuple[StrokePath, ...],
        fixed_entrance: Coordinate,
        fixed_exit: Coordinate,
    ) -> IntraChunkResult:
        """Optimize path order/direction within a block.

        Args:
            paths: Tuple of StrokePaths in original chronological order.
            fixed_entrance: Fixed entry coordinate (must be start of first path).
            fixed_exit: Fixed exit coordinate (must be end of last path).

        Returns:
            IntraChunkResult with optimized traverse order and internal distance.

        Raises:
            IntraChunkError: If optimization cannot be completed.
        """
        ...


class NoOpIntraStrategy(IntraChunkStrategy):
    """No-operation strategy that returns paths in original order.

    This strategy serves as a baseline for benchmarking and testing.
    It returns paths exactly as they were chunked, with no internal optimization.
    """

    @property
    def name(self) -> str:
        """Return the strategy name."""
        return "NoOp-Intra (Baseline)"

    def optimize_block(
        self,
        paths: tuple[StrokePath, ...],
        fixed_entrance: Coordinate,
        fixed_exit: Coordinate,
    ) -> IntraChunkResult:
        """Return paths in original order without optimization.

        Args:
            paths: Tuple of StrokePaths to process.
            fixed_entrance: Ignored for NoOp strategy.
            fixed_exit: Ignored for NoOp strategy.

        Returns:
            IntraChunkResult with original ordering and zero improvement.
        """
        self._logger.debug(f"Running {self.name} on {len(paths)} paths")

        traverse_order: list[PathTraverseState] = []

        for i, path in enumerate(paths):
            if not path.segments:
                continue

            first_seg = path.segments[0]
            last_seg = path.segments[-1]

            state = PathTraverseState(
                path_index=i,
                reversed=False,
                entrance=first_seg.start,
                exit=last_seg.end,
            )
            traverse_order.append(state)

        return IntraChunkResult(
            traverse_order=tuple(traverse_order),
            total_internal_distance=0.0,
        )


class NearestNeighborIntraStrategy(IntraChunkStrategy):
    """Nearest neighbor + 2-opt for intra-chunk optimization.

    This strategy implements a two-phase approach within each block:
    1. Greedy phase: Start from fixed entrance, always visit the closest unvisited
       path (considering both forward and reverse traversal for each).
    2. Refinement phase: Apply 2-opt swaps to improve the route by reversing segments.

    The key constraint is that we must enter at fixed_entrance and exit at fixed_exit.
    """

    @property
    def name(self) -> str:
        """Return the strategy name."""
        return "NearestNeighbor-Intra + 2-Opt"

    def optimize_block(
        self,
        paths: tuple[StrokePath, ...],
        fixed_entrance: Coordinate,
        fixed_exit: Coordinate,
    ) -> IntraChunkResult:
        """Optimize using nearest neighbor greedy algorithm with 2-opt refinement.

        Args:
            paths: Tuple of StrokePaths to optimize.
            fixed_entrance: Fixed entry coordinate (must match first path's start).
            fixed_exit: Fixed exit coordinate (must match last path's end).

        Returns:
            IntraChunkResult with optimized traversal order.
        """
        self._logger.debug(f"Running {self.name} on {len(paths)} paths")

        if not paths:
            return IntraChunkResult(
                traverse_order=(),
                total_internal_distance=0.0,
            )

        path_count = len([p for p in paths if p.segments])
        if path_count < 2:
            return self._handle_single_path(paths, fixed_entrance, fixed_exit)

        tour = self._greedy_nearest_neighbor_constrained(paths, fixed_entrance, fixed_exit)

        if len(tour) > 3:
            tour = self._two_opt_refinement(tour, paths, fixed_entrance, fixed_exit)

        total_distance = self._calculate_total_internal_distance(tour, paths)

        return IntraChunkResult(
            traverse_order=tuple(tour),
            total_internal_distance=total_distance,
        )

    def _handle_single_path(
        self,
        paths: tuple[StrokePath, ...],
        fixed_entrance: Coordinate,
        fixed_exit: Coordinate,
    ) -> IntraChunkResult:
        """Handle edge case of single path in block."""
        for i, path in enumerate(paths):
            if not path.segments:
                continue
            return IntraChunkResult(
                traverse_order=(
                    PathTraverseState(
                        path_index=i,
                        reversed=False,
                        entrance=fixed_entrance,
                        exit=fixed_exit,
                    ),
                ),
                total_internal_distance=0.0,
            )
        return IntraChunkResult(traverse_order=(), total_internal_distance=0.0)

    def _get_path_endpoints(self, path: StrokePath) -> tuple[Coordinate, Coordinate]:
        """Get the entrance and exit coordinates for a path.

        Args:
            path: The stroke path.

        Returns:
            Tuple of (entrance, exit) coordinates.
        """
        if not path.segments:
            return (Coordinate(x=0.0, y=0.0), Coordinate(x=0.0, y=0.0))

        first_seg = path.segments[0]
        last_seg = path.segments[-1]

        entrance = first_seg.start
        exit_coord = last_seg.end

        return (entrance, exit_coord)

    def _calculate_path_cost(
        self,
        from_pos: Coordinate,
        to_entrance: Coordinate,
        to_exit: Coordinate,
    ) -> tuple[float, bool]:
        """Calculate cost of traveling to a path considering both entry options.

        Args:
            from_pos: Current position.
            to_entrance: Path's original entrance coordinate.
            to_exit: Path's original exit coordinate.

        Returns:
            Tuple of (minimum_cost, should_reverse). If should_reverse is True,
            the path should be entered at its exit (and traversed backward).
        """
        cost_to_entrance = math.sqrt(
            (to_entrance.x - from_pos.x) ** 2 + (to_entrance.y - from_pos.y) ** 2
        )
        cost_to_exit = math.sqrt((to_exit.x - from_pos.x) ** 2 + (to_exit.y - from_pos.y) ** 2)

        if cost_to_entrance <= cost_to_exit:
            return (cost_to_entrance, False)
        else:
            return (cost_to_exit, True)

    def _greedy_nearest_neighbor_constrained(
        self,
        paths: tuple[StrokePath, ...],
        fixed_entrance: Coordinate,
        fixed_exit: Coordinate,
    ) -> list[PathTraverseState]:
        """Build initial tour using greedy nearest neighbor with fixed endpoints.

        Args:
            paths: All stroke paths.
            fixed_entrance: Must be start of first path in result.
            fixed_exit: Must be end of last path in result.

        Returns:
            Initial traverse order from greedy construction.
        """
        valid_paths_with_idx = [(i, p) for i, p in enumerate(paths) if p.segments]

        unvisited = {i for i, _ in valid_paths_with_idx}
        tour: list[PathTraverseState] = []

        current_pos = fixed_entrance

        while unvisited:
            best_path_idx = -1
            best_cost = float("inf")
            best_reversed = False

            for path_idx in unvisited:
                path = paths[path_idx]
                entrance, exit_coord = self._get_path_endpoints(path)

                cost, reversed_flag = self._calculate_path_cost(current_pos, entrance, exit_coord)

                if cost < best_cost:
                    best_cost = cost
                    best_path_idx = path_idx
                    best_reversed = reversed_flag

            path = paths[best_path_idx]
            entrance, exit_coord = self._get_path_endpoints(path)

            if best_reversed:
                actual_entrance = exit_coord
                actual_exit = entrance
                current_pos = entrance
            else:
                actual_entrance = entrance
                actual_exit = exit_coord
                current_pos = exit_coord

            traverse_state = PathTraverseState(
                path_index=best_path_idx,
                reversed=best_reversed,
                entrance=actual_entrance,
                exit=actual_exit,
            )
            tour.append(traverse_state)
            unvisited.remove(best_path_idx)

        if not self._is_valid_tour(tour, paths, fixed_entrance, fixed_exit):
            self._logger.warning("Greedy construction violated constraints, using original order")
            return self._create_original_order_tour(paths, fixed_entrance, fixed_exit)

        return tour

    def _is_valid_tour(
        self,
        tour: list[PathTraverseState],
        paths: tuple[StrokePath, ...],
        fixed_entrance: Coordinate,
        fixed_exit: Coordinate,
    ) -> bool:
        """Check if tour satisfies fixed entrance/exit constraints."""
        if not tour:
            return True

        first_state = tour[0]
        last_state = tour[-1]

        if first_state.reversed:
            first_actual_exit = paths[first_state.path_index].segments[-1].start
            if not self._coordinates_match(first_actual_exit, fixed_entrance):
                return False
        else:
            first_actual_entrance = paths[first_state.path_index].segments[0].start
            if not self._coordinates_match(first_actual_entrance, fixed_entrance):
                return False

        if last_state.reversed:
            last_actual_entrance = paths[last_state.path_index].segments[-1].end
            if not self._coordinates_match(last_actual_entrance, fixed_exit):
                return False
        else:
            last_actual_exit = paths[last_state.path_index].segments[-1].end
            if not self._coordinates_match(last_actual_exit, fixed_exit):
                return False

        return True

    def _create_original_order_tour(
        self,
        paths: tuple[StrokePath, ...],
        fixed_entrance: Coordinate,
        fixed_exit: Coordinate,
    ) -> list[PathTraverseState]:
        """Create tour in original order when optimization fails constraints."""
        tour: list[PathTraverseState] = []

        for i, path in enumerate(paths):
            if not path.segments:
                continue

            entrance, exit_coord = self._get_path_endpoints(path)

            reversed_flag = exit_coord.x == fixed_entrance.x and exit_coord.y == fixed_entrance.y

            if reversed_flag:
                actual_entrance = exit_coord
                actual_exit = entrance
            else:
                actual_entrance = entrance
                actual_exit = exit_coord

            tour.append(
                PathTraverseState(
                    path_index=i,
                    reversed=reversed_flag,
                    entrance=actual_entrance,
                    exit=actual_exit,
                )
            )

        return tour

    def _coordinates_match(self, c1: Coordinate, c2: Coordinate) -> bool:
        """Check if two coordinates match within floating-point tolerance."""
        return math.isclose(c1.x, c2.x, abs_tol=0.001) and math.isclose(c1.y, c2.y, abs_tol=0.001)

    def _two_opt_refinement(
        self,
        tour: list[PathTraverseState],
        paths: tuple[StrokePath, ...],
        fixed_entrance: Coordinate,
        fixed_exit: Coordinate,
    ) -> list[PathTraverseState]:
        """Improve tour using 2-opt local search with fixed endpoints.

        Args:
            tour: Current traverse order.
            paths: All stroke paths for coordinate lookups.
            fixed_entrance: Fixed entry point (must be preserved).
            fixed_exit: Fixed exit point (must be preserved).

        Returns:
            Improved traverse order.
        """
        improved = True
        iterations = 0
        max_iterations = len(tour) ** 2

        while improved and iterations < max_iterations:
            improved = False
            iterations += 1

            for i in range(len(tour) - 2):
                for j in range(i + 2, len(tour)):
                    if self._two_opt_swap_improves(tour, paths, fixed_entrance, fixed_exit, i, j):
                        tour[i + 1 : j + 1] = list(reversed(tour[i + 1 : j + 1]))
                        improved = True

        self._logger.debug(f"2-opt completed in {iterations} iterations")
        return tour

    def _two_opt_swap_improves(
        self,
        tour: list[PathTraverseState],
        paths: tuple[StrokePath, ...],
        fixed_entrance: Coordinate,
        fixed_exit: Coordinate,
        i: int,
        j: int,
    ) -> bool:
        """Check if reversing segment [i+1, j] improves total distance.

        Args:
            tour: Current traverse order.
            paths: All stroke paths.
            fixed_entrance: Fixed entry point to preserve.
            fixed_exit: Fixed exit point to preserve.
            i: First edge index.
            j: Second edge index (where j > i + 1).

        Returns:
            True if swapping would improve total distance.
        """
        a = tour[i].exit
        b = tour[i + 1].entrance
        c = tour[j].exit
        d = tour[j + 1].entrance if j + 1 < len(tour) else None

        current_dist = math.sqrt((b.x - a.x) ** 2 + (b.y - a.y) ** 2)

        new_tour_end_cost = 0.0
        if d is not None:
            new_dist = math.sqrt((c.x - a.x) ** 2 + (c.y - a.y) ** 2)
            new_tour_end_cost = math.sqrt((d.x - b.x) ** 2 + (d.y - b.y) ** 2)
        else:
            if j == len(tour) - 1 and self._coordinates_match(c, fixed_exit):
                return False
            new_dist = math.sqrt((c.x - a.x) ** 2 + (c.y - a.y) ** 2)

        total_new_dist = new_dist + new_tour_end_cost

        return total_new_dist < current_dist

    def _calculate_total_internal_distance(
        self,
        tour: list[PathTraverseState],
        paths: tuple[StrokePath, ...],
    ) -> float:
        """Calculate sum of rapid travel distances between consecutive paths.

        Args:
            tour: Optimized traverse order.
            paths: All stroke paths for coordinate lookups.

        Returns:
            Sum of internal rapid travel distances.
        """
        if len(tour) < 2:
            return 0.0

        total = 0.0
        for i in range(len(tour) - 1):
            curr_exit = tour[i].exit
            next_entrance = tour[i + 1].entrance
            dist = math.sqrt(
                (next_entrance.x - curr_exit.x) ** 2 + (next_entrance.y - curr_exit.y) ** 2
            )
            total += dist

        return total


class IntraChunkOptimizer:
    """Main optimizer for intra-chunk path optimization.

    This class coordinates the execution of intra-chunk optimization strategies
    on MacroBlock objects, providing a clean API for optimizing stroke paths
    within each chunk while preserving block entrance/exit constraints.

    Example:
        >>> from plt_optimizer.core.chunker import Chunker
        >>> chunker = Chunker()
        >>> blocks = chunker.chunk(stroke_paths, baseline_extent)
        >>>
        >>> optimizer = IntraChunkOptimizer(strategy=NearestNeighborIntraStrategy())
        >>> for block in blocks:
        ...     result = optimizer.optimize_block(block)
    """

    def __init__(
        self,
        strategy: IntraChunkStrategy | None = None,
    ) -> None:
        """Initialize the intra-chunk optimizer.

        Args:
            strategy: The optimization strategy to use. Defaults to NoOpIntraStrategy.
        """
        self._strategy = strategy or NoOpIntraStrategy()
        self._logger = get_text_logger()

    @property
    def strategy(self) -> IntraChunkStrategy:
        """Return the currently active optimization strategy."""
        return self._strategy

    def set_strategy(self, strategy: IntraChunkStrategy) -> None:
        """Change the active optimization strategy.

        Args:
            strategy: New strategy to use for subsequent optimizations.
        """
        old_name = self._strategy.name
        new_name = strategy.name
        self._strategy = strategy
        self._logger.info(f"Switching intra-chunk strategy: {old_name} -> {new_name}")

    def optimize_block(self, block: MacroBlock) -> IntraChunkResult:
        """Optimize paths within a single MacroBlock.

        Args:
            block: The MacroBlock to optimize internally.

        Returns:
            An IntraChunkResult with optimized internal path order and direction.

        Raises:
            IntraChunkError: If optimization fails.
        """
        self._logger.debug(
            f"Running {self._strategy.name} on block {block.block_id} with {len(block.paths)} paths"
        )

        try:
            result = self._strategy.optimize_block(
                block.paths,
                block.entrance,
                block.exit,
            )
            self._logger.debug(
                f"Intra-chunk optimization complete for block {block.block_id}: "
                f"internal_distance={result.total_internal_distance:.3f}"
            )
            return result
        except Exception as e:
            raise IntraChunkError(f"Intra-chunk optimization failed: {e}") from e

    def optimize_blocks(
        self,
        blocks: list[MacroBlock],
    ) -> list[IntraChunkResult]:
        """Optimize paths within multiple MacroBlocks.

        Args:
            blocks: List of MacroBlocks to optimize.

        Returns:
            List of IntraChunkResult objects, one per block.
        """
        return [self.optimize_block(block) for block in blocks]
