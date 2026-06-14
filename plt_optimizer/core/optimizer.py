"""Optimizer engine for routing MacroBlocks.

This module implements the Strategy Pattern to allow seamless switching between
different routing algorithms. The optimizer operates on MacroBlock objects,
determining both the optimal traversal sequence and whether each block should
be traversed forward or in reverse.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Tuple

from plt_optimizer.core.chunker import MacroBlock
from plt_optimizer.utils.geometry import calculate_coordinate_distance
from plt_optimizer.utils.logging import get_text_logger


class OptimizationError(Exception):
    """Exception raised when optimization fails.

    Attributes:
        message: Human-readable error description.
    """

    def __init__(self, message: str) -> None:
        """Initialize an OptimizationError.

        Args:
            message: Error description.
        """
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class BlockConnection:
    """Represents a connection between two MacroBlocks in the optimized route.

    Attributes:
        source_block_id: ID of the source block.
        target_block_id: ID of the target block.
        travel_distance: Distance of the tool-up jump between blocks.
        entry_at_source: Coordinate at source where we arrive (entrance or exit).
        entry_at_target: Coordinate at target where we enter (entrance or exit).
    """
    source_block_id: int
    target_block_id: int
    travel_distance: float
    entry_at_source: Tuple[float, float]
    entry_at_target: Tuple[float, float]


@dataclass(frozen=True)
class BlockTraverseState:
    """Represents whether a MacroBlock should be traversed forward or reverse.

    Attributes:
        block_id: ID of the block.
        reversed: True if block segments should be traversed in reverse order.
        entrance: The coordinate to use as entry point (may differ from original).
        exit: The coordinate to use as exit point (may differ from original).
    """
    block_id: int
    reversed: bool
    entrance: Tuple[float, float]
    exit: Tuple[float, float]


@dataclass(frozen=True)
class OptimizationResult:
    """Results from optimizing the traversal order of MacroBlocks.

    Attributes:
        traverse_order: List of BlockTraverseState describing how each block
            should be traversed (direction and entry point).
        connections: List of BlockConnection describing tool-up jumps between blocks.
        total_travel_distance: Sum of all travel distances.
        initial_position: Starting position for the optimization run.
    """
    traverse_order: Tuple[BlockTraverseState, ...]
    connections: Tuple[BlockConnection, ...]
    total_travel_distance: float
    initial_position: Optional[Tuple[float, float]]

    @property
    def block_count(self) -> int:
        """Return number of blocks in the optimized route."""
        return len(self.traverse_order)


class OptimizationStrategy(ABC):
    """Abstract base class for optimization strategies.

    This defines the interface that all concrete strategy implementations must follow.
    Strategies are responsible for determining both the sequence and direction of
    block traversal to minimize total tool-up travel distance.

    Attributes:
        name: Human-readable name of the strategy.
    """

    def __init__(self) -> None:
        """Initialize the optimization strategy."""
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
    def optimize(
        self,
        blocks: List[MacroBlock],
        initial_position: Optional[Tuple[float, float]] = None,
    ) -> OptimizationResult:
        """Optimize the traversal order of MacroBlocks.

        Args:
            blocks: List of MacroBlocks to optimize (in original chronological order).
            initial_position: Optional starting position as (x, y) tuple.
                If None, uses the first block's entrance.

        Returns:
            An OptimizationResult with optimal traverse order and connections.

        Raises:
            OptimizationError: If optimization cannot be completed.
        """
        ...

    def _calculate_block_cost(
        self,
        from_pos: Tuple[float, float],
        to_entrance: Tuple[float, float],
        to_exit: Tuple[float, float],
    ) -> Tuple[float, bool]:
        """Calculate the cost of traveling to a block considering both entry options.

        Determines whether it is cheaper to enter a block at its entrance (traverse
        forward) or at its exit (traverse in reverse after arriving from the opposite
        side).

        Args:
            from_pos: Current position as (x, y).
            to_entrance: Block's original entrance coordinate.
            to_exit: Block's original exit coordinate.

        Returns:
            Tuple of (minimum_cost, should_reverse). If should_reverse is True,
            the block should be entered at its exit (and traversed backward).
        """
        cost_to_entrance = math.sqrt(
            (to_entrance[0] - from_pos[0]) ** 2
            + (to_entrance[1] - from_pos[1]) ** 2
        )
        cost_to_exit = math.sqrt(
            (to_exit[0] - from_pos[0]) ** 2
            + (to_exit[1] - from_pos[1]) ** 2
        )

        if cost_to_entrance <= cost_to_exit:
            return (cost_to_entrance, False)
        else:
            return (cost_to_exit, True)

    def _build_connections(
        self,
        blocks: List[MacroBlock],
        traverse_order: List[BlockTraverseState],
        initial_pos: Optional[Tuple[float, float]],
    ) -> Tuple[BlockConnection, ...]:
        """Build the connection list from a traverse order.

        Args:
            blocks: All macro blocks.
            traverse_order: Optimized traversal order.
            initial_pos: Starting position before first block.

        Returns:
            Tuple of BlockConnections between consecutive blocks.
        """
        connections: List[BlockConnection] = []

        current_pos = initial_pos

        for i, state in enumerate(traverse_order):
            target_block = blocks[state.block_id]

            # Determine entry and exit coordinates based on reversal
            if state.reversed:
                actual_entrance = (target_block.exit.x, target_block.exit.y)
                actual_exit = (target_block.entrance.x, target_block.entrance.y)
            else:
                actual_entrance = (target_block.entrance.x, target_block.entrance.y)
                actual_exit = (target_block.exit.x, target_block.exit.y)

            if current_pos is not None and i == 0:
                # First block - connect from initial position
                travel_dist = math.sqrt(
                    (actual_entrance[0] - current_pos[0]) ** 2
                    + (actual_entrance[1] - current_pos[1]) ** 2
                )
            elif i > 0 and traverse_order[i - 1].block_id != state.block_id:
                # Not first block - connect from previous block's exit
                prev_state = traverse_order[i - 1]
                travel_dist = math.sqrt(
                    (actual_entrance[0] - prev_state.exit[0]) ** 2
                    + (actual_entrance[1] - prev_state.exit[1]) ** 2
                )
            else:
                # Same block or no connection needed
                continue

            connections.append(BlockConnection(
                source_block_id=traverse_order[i - 1].block_id if i > 0 else -1,
                target_block_id=state.block_id,
                travel_distance=travel_dist,
                entry_at_source=traverse_order[i - 1].exit if i > 0 else current_pos or (0, 0),
                entry_at_target=actual_entrance,
            ))

        return tuple(connections)


class NoOpStrategy(OptimizationStrategy):
    """No-operation strategy that returns blocks in original chronological order.

    This strategy serves as a baseline for benchmarking and testing. It returns
    the blocks exactly as they were chunked, with no optimization applied.
    """

    @property
    def name(self) -> str:
        """Return the strategy name."""
        return "NoOp (Baseline)"

    def optimize(
        self,
        blocks: List[MacroBlock],
        initial_position: Optional[Tuple[float, float]] = None,
    ) -> OptimizationResult:
        """Return blocks in original order without optimization.

        Args:
            blocks: List of MacroBlocks to process.
            initial_position: Ignored for NoOp strategy.

        Returns:
            OptimizationResult with original ordering and zero improvement.
        """
        self._logger.info(f"Running {self.name} on {len(blocks)} blocks")

        traverse_order: List[BlockTraverseState] = []

        # Determine starting position
        if initial_position is not None:
            start_pos = initial_position
        elif blocks:
            first_block = blocks[0]
            start_pos = (first_block.entrance.x, first_block.entrance.y)
        else:
            start_pos = None

        for block in blocks:
            traverse_order.append(BlockTraverseState(
                block_id=block.block_id,
                reversed=False,
                entrance=(block.entrance.x, block.entrance.y),
                exit=(block.exit.x, block.exit.y),
            ))

        connections = self._build_connections(blocks, traverse_order, start_pos)
        total_distance = sum(c.travel_distance for c in connections if c.source_block_id >= 0)

        return OptimizationResult(
            traverse_order=tuple(traverse_order),
            connections=connections,
            total_travel_distance=total_distance,
            initial_position=start_pos,
        )


class NearestNeighbor2OptStrategy(OptimizationStrategy):
    """Greedy nearest neighbor followed by 2-opt local search optimization.

    This strategy implements a two-phase approach:
    1. Greedy phase: Start from initial position, always visit the closest unvisited
       block (considering both entrance and exit for each).
    2. Refinement phase: Apply 2-opt swaps to improve the route by reversing segments.

    The 2-opt algorithm iteratively reverses segments of the tour when doing so
    reduces total distance, effectively eliminating crossing paths.

    When no initial position is specified, this strategy evaluates multiple candidate
    starting points (the N closest endpoints to origin) and selects the one that
    yields the minimum total travel distance.
    """

    DEFAULT_N_CANDIDATES: int = 3

    @property
    def name(self) -> str:
        """Return the strategy name."""
        return "NearestNeighbor + 2-Opt"

    def optimize(
        self,
        blocks: List[MacroBlock],
        initial_position: Optional[Tuple[float, float]] = None,
    ) -> OptimizationResult:
        """Optimize using nearest neighbor greedy algorithm with 2-opt refinement.

        When no initial_position is specified, this method evaluates multiple
        candidate starting points (the N closest endpoints to origin) and selects
        the one that yields minimum total travel distance.

        Args:
            blocks: List of MacroBlocks to optimize.
            initial_position: Starting position for optimization.

        Returns:
            OptimizationResult with optimized traversal order.
        """
        self._logger.info(f"Running {self.name} on {len(blocks)} blocks")

        if not blocks:
            return OptimizationResult(
                traverse_order=(),
                connections=(),
                total_travel_distance=0.0,
                initial_position=None,
            )

        # Determine starting position - find endpoint closest to origin if not specified
        if initial_position is None:
            candidates = self._find_nearest_origin_endpoints(
                blocks, origin=(0.0, 0.0), n_candidates=self.DEFAULT_N_CANDIDATES
            )
            self._logger.debug(f"Evaluating {len(candidates)} starting candidates")

            best_result: Optional[OptimizationResult] = None

            for start_pos, first_block_idx, start_at_exit, _dist in candidates:
                tour = self._greedy_nearest_neighbor_from_start(
                    blocks, start_pos, forced_first_block=first_block_idx,
                    forced_first_reversed=start_at_exit
                )

                if len(tour) > 3:
                    tour = self._two_opt_refinement(tour, blocks)

                connections = self._build_connections(blocks, tour, start_pos)
                total_distance = sum(c.travel_distance for c in connections if c.source_block_id >= 0)

                candidate_result = OptimizationResult(
                    traverse_order=tuple(tour),
                    connections=connections,
                    total_travel_distance=total_distance,
                    initial_position=start_pos,
                )

                if best_result is None or candidate_result.total_travel_distance < best_result.total_travel_distance:
                    best_result = candidate_result
                    self._logger.debug(f"New best: distance={candidate_result.total_travel_distance:.3f} from candidate at {start_pos}")

            return best_result  # type: ignore[return-value]
        else:
            start_pos = initial_position
            tour = self._greedy_nearest_neighbor(blocks, start_pos)

            if len(tour) > 3:
                tour = self._two_opt_refinement(tour, blocks)

            connections = self._build_connections(blocks, tour, start_pos)
            total_distance = sum(c.travel_distance for c in connections if c.source_block_id >= 0)

            return OptimizationResult(
                traverse_order=tuple(tour),
                connections=connections,
                total_travel_distance=total_distance,
                initial_position=start_pos,
            )

    def _greedy_nearest_neighbor(
        self,
        blocks: List[MacroBlock],
        start_pos: Tuple[float, float],
    ) -> List[BlockTraverseState]:
        """Build initial tour using greedy nearest neighbor heuristic.

        Args:
            blocks: All macro blocks.
            start_pos: Starting position.

        Returns:
            Initial traverse order from greedy construction.
        """
        unvisited = set(range(len(blocks)))
        tour: List[BlockTraverseState] = []
        current_pos = start_pos

        while unvisited:
            best_block_idx = -1
            best_cost = float('inf')
            best_reversed = False

            for block_idx in unvisited:
                block = blocks[block_idx]
                cost, reversed_flag = self._calculate_block_cost(
                    current_pos,
                    (block.entrance.x, block.entrance.y),
                    (block.exit.x, block.exit.y),
                )

                if cost < best_cost:
                    best_cost = cost
                    best_block_idx = block_idx
                    best_reversed = reversed_flag

            # Add to tour
            block = blocks[best_block_idx]
            if best_reversed:
                traverse_state = BlockTraverseState(
                    block_id=block.block_id,
                    reversed=True,
                    entrance=(block.exit.x, block.exit.y),
                    exit=(block.entrance.x, block.entrance.y),
                )
                current_pos = (block.entrance.x, block.entrance.y)
            else:
                traverse_state = BlockTraverseState(
                    block_id=block.block_id,
                    reversed=False,
                    entrance=(block.entrance.x, block.entrance.y),
                    exit=(block.exit.x, block.exit.y),
                )
                current_pos = (block.exit.x, block.exit.y)

            tour.append(traverse_state)
            unvisited.remove(best_block_idx)

        return tour

    def _find_nearest_origin_endpoints(
        self,
        blocks: List[MacroBlock],
        origin: Tuple[float, float] = (0.0, 0.0),
        n_candidates: int = 5,
    ) -> List[Tuple[Tuple[float, float], int, bool, float]]:
        """Find the N block endpoints nearest to the origin.

        This ensures the optimization evaluates multiple starting candidates
        when there are ties or near-ties for closest endpoint to origin.

        Args:
            blocks: List of MacroBlocks to search.
            origin: Reference point for distance calculation (default origin).
            n_candidates: Number of closest endpoints to return.

        Returns:
            List of tuples sorted by distance: [(position, block_index, is_exit, distance), ...].
            - position: (x, y) coordinates of the endpoint
            - block_index: index of the block containing this endpoint
            - is_exit: True if endpoint is block's exit (needs reversal)
            - distance: Euclidean distance from origin
        """
        candidates: List[Tuple[float, Tuple[Tuple[float, float], int, bool]]] = []

        for i, block in enumerate(blocks):
            dist_entrance = math.sqrt(
                (block.entrance.x - origin[0]) ** 2
                + (block.entrance.y - origin[1]) ** 2
            )
            candidates.append((dist_entrance, ((block.entrance.x, block.entrance.y), i, False)))

            dist_exit = math.sqrt(
                (block.exit.x - origin[0]) ** 2
                + (block.exit.y - origin[1]) ** 2
            )
            candidates.append((dist_exit, ((block.exit.x, block.exit.y), i, True)))

        candidates.sort(key=lambda x: x[0])
        return [(pos, idx, is_exit, dist) for dist, (pos, idx, is_exit) in candidates[:n_candidates]]

    def _find_nearest_origin_endpoint(
        self,
        blocks: List[MacroBlock],
        origin: Tuple[float, float] = (0.0, 0.0),
    ) -> Tuple[Tuple[float, float], int, bool]:
        """Find the block endpoint nearest to the origin.

        This ensures the optimization always starts from the stroke end closest
        to where the tool begins (typically at machine origin).

        Args:
            blocks: List of MacroBlocks to search.
            origin: Reference point for distance calculation (default origin).

        Returns:
            Tuple of (nearest_position, block_index, is_exit).
            - nearest_position: (x, y) coordinates closest to origin
            - block_index: index of the block containing this endpoint
            - is_exit: True if nearest position is block's exit (needs reversal)
        """
        candidates = self._find_nearest_origin_endpoints(blocks, origin, n_candidates=1)
        pos, idx, is_exit, _ = candidates[0]
        return (pos, idx, is_exit)

    def _greedy_nearest_neighbor_from_start(
        self,
        blocks: List[MacroBlock],
        start_pos: Tuple[float, float],
        forced_first_block: int,
        forced_first_reversed: bool,
    ) -> List[BlockTraverseState]:
        """Build tour starting with a specific block in a specific direction.

        This variant forces the first block to be visited first (potentially
        reversed) regardless of greedy distance cost, ensuring we always start
        from the endpoint nearest the origin.

        Args:
            blocks: All macro blocks.
            start_pos: Starting position.
            forced_first_block: Index of block to visit first.
            forced_first_reversed: Whether to traverse first block in reverse.

        Returns:
            Initial traverse order with guaranteed starting point.
        """
        unvisited = set(range(len(blocks)))
        tour: List[BlockTraverseState] = []

        # Handle the forced first block
        first_block = blocks[forced_first_block]
        if forced_first_reversed:
            current_pos = (first_block.entrance.x, first_block.entrance.y)
            traverse_state = BlockTraverseState(
                block_id=first_block.block_id,
                reversed=True,
                entrance=(first_block.exit.x, first_block.exit.y),
                exit=(first_block.entrance.x, first_block.entrance.y),
            )
        else:
            current_pos = (first_block.exit.x, first_block.exit.y)
            traverse_state = BlockTraverseState(
                block_id=first_block.block_id,
                reversed=False,
                entrance=(first_block.entrance.x, first_block.entrance.y),
                exit=(first_block.exit.x, first_block.exit.y),
            )

        tour.append(traverse_state)
        unvisited.remove(forced_first_block)

        # Continue with standard greedy for remaining blocks
        while unvisited:
            best_block_idx = -1
            best_cost = float('inf')
            best_reversed = False

            for block_idx in unvisited:
                block = blocks[block_idx]
                cost, reversed_flag = self._calculate_block_cost(
                    current_pos,
                    (block.entrance.x, block.entrance.y),
                    (block.exit.x, block.exit.y),
                )

                if cost < best_cost:
                    best_cost = cost
                    best_block_idx = block_idx
                    best_reversed = reversed_flag

            # Add to tour
            block = blocks[best_block_idx]
            if best_reversed:
                traverse_state = BlockTraverseState(
                    block_id=block.block_id,
                    reversed=True,
                    entrance=(block.exit.x, block.exit.y),
                    exit=(block.entrance.x, block.entrance.y),
                )
                current_pos = (block.entrance.x, block.entrance.y)
            else:
                traverse_state = BlockTraverseState(
                    block_id=block.block_id,
                    reversed=False,
                    entrance=(block.entrance.x, block.entrance.y),
                    exit=(block.exit.x, block.exit.y),
                )
                current_pos = (block.exit.x, block.exit.y)

            tour.append(traverse_state)
            unvisited.remove(best_block_idx)

        return tour

    def _two_opt_refinement(
        self,
        tour: List[BlockTraverseState],
        blocks: List[MacroBlock],
    ) -> List[BlockTraverseState]:
        """Improve tour using 2-opt local search.

        The 2-opt algorithm considers every pair of edges and checks if swapping
        them (which effectively reverses the segment between them) reduces total
        distance. This continues until no improvement can be made.

        Args:
            tour: Current traverse order.
            blocks: All macro blocks for coordinate lookups.

        Returns:
            Improved traverse order.
        """
        improved = True
        iterations = 0
        max_iterations = len(tour) ** 2  # Safety limit

        while improved and iterations < max_iterations:
            improved = False
            iterations += 1

            for i in range(len(tour) - 2):
                for j in range(i + 2, len(tour)):
                    if self._two_opt_swap_improves(tour, blocks, i, j):
                        # Perform the swap by reversing segment [i+1, j]
                        tour[i + 1:j + 1] = reversed(tour[i + 1:j + 1])
                        improved = True

        self._logger.debug(f"2-opt completed in {iterations} iterations")
        return tour

    def _two_opt_swap_improves(
        self,
        tour: List[BlockTraverseState],
        blocks: List[MacroBlock],
        i: int,
        j: int,
    ) -> bool:
        """Check if a 2-opt swap between edges (i, i+1) and (j, j+1) improves cost.

        Args:
            tour: Current traverse order.
            blocks: All macro blocks.
            i: First edge index.
            j: Second edge index (where j > i + 1).

        Returns:
            True if swapping would improve total distance.
        """
        # Get coordinates for the four points involved
        a = tour[i].exit
        b = tour[i + 1].entrance
        c = tour[j].exit
        d = tour[j + 1].entrance if j + 1 < len(tour) else None

        # Current distance: dist(a,b) + dist(c,d)
        current_dist = math.sqrt((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2)

        new_tour_end_cost = 0.0
        if d is not None:
            # Only count second edge if it exists in the tour segment we're keeping
            # For segments that will be reversed, we need different logic

            # After swap: dist(a,c) + dist(b,d)
            new_dist = math.sqrt((c[0] - a[0]) ** 2 + (c[1] - a[1]) ** 2)
            new_tour_end_cost = math.sqrt((d[0] - b[0]) ** 2 + (d[1] - b[1]) ** 2)
        else:
            # Edge case: j is last element, only one edge to consider after swap
            new_dist = math.sqrt((c[0] - a[0]) ** 2 + (c[1] - a[1]) ** 2)

        total_new_dist = new_dist + new_tour_end_cost

        return total_new_dist < current_dist


class OptimizerEngine:
    """Main optimization engine that coordinates strategy execution.

    This engine acts as the Context in the Strategy Pattern, managing which
    optimization strategy is active and coordinating the overall optimization
    workflow. It provides a clean API for running optimizations on MacroBlocks.

    Example:
        >>> from plt_optimizer.core.chunker import Chunker
        >>> chunker = Chunker()
        >>> blocks = chunker.chunk(stroke_paths, baseline_extent)
        >>>
        >>> engine = OptimizerEngine(strategy=NearestNeighbor2OptStrategy())
        >>> result = engine.optimize(blocks)
    """

    def __init__(
        self,
        strategy: Optional[OptimizationStrategy] = None,
    ) -> None:
        """Initialize the optimizer engine.

        Args:
            strategy: The optimization strategy to use. Defaults to NoOpStrategy.
        """
        self._strategy = strategy or NoOpStrategy()
        self._logger = get_text_logger()

    @property
    def strategy(self) -> OptimizationStrategy:
        """Return the currently active optimization strategy."""
        return self._strategy

    def set_strategy(self, strategy: OptimizationStrategy) -> None:
        """Change the active optimization strategy.

        Args:
            strategy: New strategy to use for subsequent optimizations.
        """
        old_name = self._strategy.name
        new_name = strategy.name
        self._strategy = strategy
        self._logger.info(f"Switching optimization strategy: {old_name} -> {new_name}")

    def optimize(
        self,
        blocks: List[MacroBlock],
        initial_position: Optional[Tuple[float, float]] = None,
    ) -> OptimizationResult:
        """Run the active optimization strategy on a list of MacroBlocks.

        Args:
            blocks: List of MacroBlocks to optimize.
            initial_position: Optional starting position as (x, y) tuple.

        Returns:
            An OptimizationResult with optimized traversal order.

        Raises:
            OptimizationError: If optimization fails.
        """
        self._logger.info(
            f"Starting optimization with {self._strategy.name} on {len(blocks)} blocks"
        )

        try:
            result = self._strategy.optimize(blocks, initial_position)
            self._logger.info(
                f"Optimization complete: total_travel_distance={result.total_travel_distance:.3f}"
            )
            return result
        except Exception as e:
            raise OptimizationError(f"Optimization failed: {e}") from e