"""Optimizer engine for routing MacroBlocks.

This module implements the Strategy Pattern to allow seamless switching between
different routing algorithms. The optimizer operates on MacroBlock objects,
determining both the optimal traversal sequence and whether each block should
be traversed forward or in reverse.
"""

from __future__ import annotations

import math
import time
from abc import ABC, abstractmethod
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

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

        # Build a lookup map from block_id to MacroBlock for correct indexing
        block_by_id: Dict[int, MacroBlock] = {b.block_id: b for b in blocks}

        current_pos = initial_pos

        for i, state in enumerate(traverse_order):
            target_block = block_by_id[state.block_id]

            # Determine entry and exit coordinates based on reversal
            if state.reversed:
                actual_entrance = (target_block.exit.x, target_block.exit.y)
                actual_exit = (target_block.entrance.x, target_block.entrance.y)
            else:
                actual_entrance = (target_block.entrance.x, target_block.entrance.y)
                actual_exit = (target_block.exit.x, target_block.exit.y)

            if i > 0 and traverse_order[i - 1].block_id != state.block_id:
                # Connect from previous block's exit to current block's entrance
                prev_state = traverse_order[i - 1]
                travel_dist = math.sqrt(
                    (actual_entrance[0] - prev_state.exit[0]) ** 2
                    + (actual_entrance[1] - prev_state.exit[1]) ** 2
                )

                connections.append(BlockConnection(
                    source_block_id=prev_state.block_id,
                    target_block_id=state.block_id,
                    travel_distance=travel_dist,
                    entry_at_source=prev_state.exit,
                    entry_at_target=actual_entrance,
                ))
            # Note: connections from initial_pos to first block are not included here;
            # they are tracked separately via total_travel_distance calculation.

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

    DEFAULT_N_CANDIDATES: int = 2

    def __init__(self, same_row_preference: float = 1.0) -> None:
        """Initialize the strategy.

        Args:
            same_row_preference: Penalty multiplier for y-differences during greedy
                selection. Default 1.0 applies no penalty (backward compatible).
                Values > 1.0 increase cost for blocks with different y-values,
                biasing traversal to prefer blocks on the same row.
        """
        super().__init__()
        self._same_row_preference = same_row_preference

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

    def _find_farthest_origin_endpoints(
        self,
        blocks: List[MacroBlock],
        origin: Tuple[float, float] = (0.0, 0.0),
        n_candidates: int = 5,
    ) -> List[Tuple[Tuple[float, float], int, bool, float]]:
        """Find the N block endpoints farthest from the origin.

        This is used to evaluate candidate ending points for optimization,
        ensuring the tour ends at a point far from machine origin when desired.

        Args:
            blocks: List of MacroBlocks to search.
            origin: Reference point for distance calculation (default origin).
            n_candidates: Number of farthest endpoints to return.

        Returns:
            List of tuples sorted by distance descending: [(position, block_index, is_exit, distance), ...].
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

        # Sort by distance descending to get farthest first
        candidates.sort(key=lambda x: x[0], reverse=True)
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

    def _calculate_block_cost(
        self,
        from_pos: Tuple[float, float],
        to_entrance: Tuple[float, float],
        to_exit: Tuple[float, float],
    ) -> Tuple[float, bool]:
        """Calculate cost with y-difference penalty for same-row preference.

        Args:
            from_pos: Current position as (x, y).
            to_entrance: Block's original entrance coordinate.
            to_exit: Block's original exit coordinate.

        Returns:
            Tuple of (minimum_cost, should_reverse). If should_reverse is True,
            the block should be entered at its exit (and traversed backward).
        """
        dx = to_entrance[0] - from_pos[0]
        dy = to_entrance[1] - from_pos[1]
        base_distance_to_entrance = math.sqrt(dx ** 2 + dy ** 2)
        y_penalty = (self._same_row_preference - 1.0) * abs(dy)
        cost_to_entrance = base_distance_to_entrance + y_penalty

        dx = to_exit[0] - from_pos[0]
        dy = to_exit[1] - from_pos[1]
        base_distance_to_exit = math.sqrt(dx ** 2 + dy ** 2)
        y_penalty = (self._same_row_preference - 1.0) * abs(dy)
        cost_to_exit = base_distance_to_exit + y_penalty

        if cost_to_entrance <= cost_to_exit:
            return (cost_to_entrance, False)
        else:
            return (cost_to_exit, True)

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


class InsertionHeuristicStrategy(OptimizationStrategy):
    """Cheapest Insertion Heuristic for TSP optimization.

    This strategy builds a tour by iteratively inserting unvisited blocks into
    the current tour at the position that results in minimal distance increase.
    The algorithm:

    1. Find the closest pair of endpoints from all blocks to form initial tour
    2. For each remaining unvisited block, find the best insertion position
    3. Consider both entrance and exit orientations for each insertion point
    4. Insert at the position that minimizes total tour distance
    5. Repeat until all blocks are visited

    This is a greedy constructive heuristic that provides good solutions
    relatively quickly, making it suitable for larger problem instances.
    """

    def __init__(self) -> None:
        """Initialize the insertion heuristic strategy."""
        super().__init__()

    @property
    def name(self) -> str:
        """Return the strategy name."""
        return "Cheapest Insertion Heuristic"

    def optimize(
        self,
        blocks: List[MacroBlock],
        initial_position: Optional[Tuple[float, float]] = None,
    ) -> OptimizationResult:
        """Optimize using cheapest insertion heuristic.

        Args:
            blocks: List of MacroBlocks to optimize.
            initial_position: Starting position for optimization. If None,
                uses the closest endpoint to origin as starting point.

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

        if len(blocks) == 1:
            block = blocks[0]
            start_pos: Tuple[float, float]
            reversed_flag: bool
            cost_to_entrance = math.sqrt(block.entrance.x ** 2 + block.entrance.y ** 2)
            cost_to_exit = math.sqrt(block.exit.x ** 2 + block.exit.y ** 2)

            if initial_position is not None:
                cost_to_entrance = math.sqrt(
                    (block.entrance.x - initial_position[0]) ** 2
                    + (block.entrance.y - initial_position[1]) ** 2
                )
                cost_to_exit = math.sqrt(
                    (block.exit.x - initial_position[0]) ** 2
                    + (block.exit.y - initial_position[1]) ** 2
                )

            if cost_to_entrance <= cost_to_exit:
                start_pos = initial_position or (0.0, 0.0)
                reversed_flag = False
                tour_state = BlockTraverseState(
                    block_id=block.block_id,
                    reversed=False,
                    entrance=(block.entrance.x, block.entrance.y),
                    exit=(block.exit.x, block.exit.y),
                )
            else:
                start_pos = initial_position or (0.0, 0.0)
                reversed_flag = True
                tour_state = BlockTraverseState(
                    block_id=block.block_id,
                    reversed=True,
                    entrance=(block.exit.x, block.exit.y),
                    exit=(block.entrance.x, block.entrance.y),
                )

            connections = self._build_connections(blocks, [tour_state], start_pos)
            total_distance = sum(c.travel_distance for c in connections if c.source_block_id >= 0)

            return OptimizationResult(
                traverse_order=(tour_state,),
                connections=connections,
                total_travel_distance=total_distance,
                initial_position=start_pos,
            )

        if initial_position is not None:
            start_pos = initial_position
            tour = self._build_initial_tour_from_start(blocks, start_pos)
        else:
            closest_pair_result = self._find_closest_pair(blocks)
            start_pos = closest_pair_result[0]
            tour = self._build_tour_with_seed(blocks, closest_pair_result)

        unvisited = set(range(len(blocks))) - {state.block_id for state in tour}

        while unvisited:
            best_insertion: Optional[Tuple[int, BlockTraverseState, float]] = None
            best_block_idx = -1

            for block_idx in unvisited:
                block = blocks[block_idx]
                insertion_result = self._find_best_insertion_position(
                    block, tour, blocks
                )

                if best_insertion is None or insertion_result[2] < best_insertion[2]:
                    best_insertion = (
                        insertion_result[0],
                        insertion_result[1],
                        insertion_result[2],
                    )
                    best_block_idx = block_idx

            if best_insertion is not None and best_block_idx != -1:
                insert_pos, new_state, _cost = best_insertion
                tour.insert(insert_pos + 1, new_state)
                unvisited.remove(best_block_idx)

        connections = self._build_connections(blocks, tour, start_pos)
        total_distance = sum(c.travel_distance for c in connections if c.source_block_id >= 0)

        return OptimizationResult(
            traverse_order=tuple(tour),
            connections=connections,
            total_travel_distance=total_distance,
            initial_position=start_pos,
        )

    def _find_closest_pair(
        self, blocks: List[MacroBlock]
    ) -> Tuple[Tuple[float, float], int, bool, Tuple[float, float], int, bool]:
        """Find the pair of endpoints with minimum distance between them.

        Args:
            blocks: All macro blocks.

        Returns:
            Tuple of (endpoint1, block1_idx, is1_exit, endpoint2, block2_idx, is2_exit).
        """
        min_dist = float('inf')
        result: Optional[Tuple[
            Tuple[float, float], int, bool, Tuple[float, float], int, bool
        ]] = None

        for i, block in enumerate(blocks):
            endpoints = [
                ((block.entrance.x, block.entrance.y), False),
                ((block.exit.x, block.exit.y), True),
            ]

            for j in range(i + 1, len(blocks)):
                other_block = blocks[j]
                other_endpoints = [
                    ((other_block.entrance.x, other_block.entrance.y), False),
                    ((other_block.exit.x, other_block.exit.y), True),
                ]

                for (x1, y1), is1_exit in endpoints:
                    for (x2, y2), is2_exit in other_endpoints:
                        dist = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)

                        if dist < min_dist:
                            min_dist = dist
                            result = (
                                (x1, y1), i, is1_exit,
                                (x2, y2), j, is2_exit,
                            )

        if result is None:
            raise OptimizationError("Failed to find closest pair of endpoints")

        return result

    def _build_tour_with_seed(
        self,
        blocks: List[MacroBlock],
        seed_pair: Tuple[Tuple[float, float], int, bool, Tuple[float, float], int, bool],
    ) -> List[BlockTraverseState]:
        """Build initial tour from a seed pair of connected endpoints.

        Args:
            blocks: All macro blocks.
            seed_pair: Result from _find_closest_pair containing two endpoints
                that form the initial connection.

        Returns:
            Initial tour with two BlockTraverseStates.
        """
        (pos1, block1_idx, is1_exit), (pos2, block2_idx, is2_exit) = (
            (seed_pair[0], seed_pair[1], seed_pair[2]),
            (seed_pair[3], seed_pair[4], seed_pair[5]),
        )

        tour: List[BlockTraverseState] = []

        if is1_exit:
            state1 = BlockTraverseState(
                block_id=blocks[block1_idx].block_id,
                reversed=True,
                entrance=(blocks[block1_idx].exit.x, blocks[block1_idx].exit.y),
                exit=(blocks[block1_idx].entrance.x, blocks[block1_idx].entrance.y),
            )
        else:
            state1 = BlockTraverseState(
                block_id=blocks[block1_idx].block_id,
                reversed=False,
                entrance=(blocks[block1_idx].entrance.x, blocks[block1_idx].entrance.y),
                exit=(blocks[block1_idx].exit.x, blocks[block1_idx].exit.y),
            )

        if is2_exit:
            state2 = BlockTraverseState(
                block_id=blocks[block2_idx].block_id,
                reversed=True,
                entrance=(blocks[block2_idx].exit.x, blocks[block2_idx].exit.y),
                exit=(blocks[block2_idx].entrance.x, blocks[block2_idx].entrance.y),
            )
        else:
            state2 = BlockTraverseState(
                block_id=blocks[block2_idx].block_id,
                reversed=False,
                entrance=(blocks[block2_idx].entrance.x, blocks[block2_idx].entrance.y),
                exit=(blocks[block2_idx].exit.x, blocks[block2_idx].exit.y),
            )

        tour.append(state1)
        tour.append(state2)

        return tour

    def _build_initial_tour_from_start(
        self,
        blocks: List[MacroBlock],
        start_pos: Tuple[float, float],
    ) -> List[BlockTraverseState]:
        """Build initial tour starting from a specific position.

        Finds the closest endpoint to start_pos and uses it as first block.
        Then finds the closest unvisited endpoint to that first block's exit
        for the second block.

        Args:
            blocks: All macro blocks.
            start_pos: Starting position coordinates.

        Returns:
            Initial tour with two BlockTraverseStates.
        """
        min_first_dist = float('inf')
        first_block_idx = -1
        first_is_exit = False

        for i, block in enumerate(blocks):
            dist_entrance = math.sqrt(
                (block.entrance.x - start_pos[0]) ** 2
                + (block.entrance.y - start_pos[1]) ** 2
            )
            dist_exit = math.sqrt(
                (block.exit.x - start_pos[0]) ** 2
                + (block.exit.y - start_pos[1]) ** 2
            )

            if dist_entrance < min_first_dist:
                min_first_dist = dist_entrance
                first_block_idx = i
                first_is_exit = False

            if dist_exit < min_first_dist:
                min_first_dist = dist_exit
                first_block_idx = i
                first_is_exit = True

        first_block = blocks[first_block_idx]

        if first_is_exit:
            state1 = BlockTraverseState(
                block_id=first_block.block_id,
                reversed=True,
                entrance=(first_block.exit.x, first_block.exit.y),
                exit=(first_block.entrance.x, first_block.entrance.y),
            )
        else:
            state1 = BlockTraverseState(
                block_id=first_block.block_id,
                reversed=False,
                entrance=(first_block.entrance.x, first_block.entrance.y),
                exit=(first_block.exit.x, first_block.exit.y),
            )

        min_second_dist = float('inf')
        second_block_idx = -1
        second_is_exit = False

        for i, block in enumerate(blocks):
            if i == first_block_idx:
                continue

            dist_entrance = math.sqrt(
                (block.entrance.x - state1.exit[0]) ** 2
                + (block.entrance.y - state1.exit[1]) ** 2
            )
            dist_exit = math.sqrt(
                (block.exit.x - state1.exit[0]) ** 2
                + (block.exit.y - state1.exit[1]) ** 2
            )

            if dist_entrance < min_second_dist:
                min_second_dist = dist_entrance
                second_block_idx = i
                second_is_exit = False

            if dist_exit < min_second_dist:
                min_second_dist = dist_exit
                second_block_idx = i
                second_is_exit = True

        if second_block_idx == -1:
            return [state1]

        second_block = blocks[second_block_idx]

        if second_is_exit:
            state2 = BlockTraverseState(
                block_id=second_block.block_id,
                reversed=True,
                entrance=(second_block.exit.x, second_block.exit.y),
                exit=(second_block.entrance.x, second_block.entrance.y),
            )
        else:
            state2 = BlockTraverseState(
                block_id=second_block.block_id,
                reversed=False,
                entrance=(second_block.entrance.x, second_block.entrance.y),
                exit=(second_block.exit.x, second_block.exit.y),
            )

        return [state1, state2]

    def _get_block_endpoints(
        self,
        block: MacroBlock,
        reversed: bool,
    ) -> Tuple[Tuple[float, float], Tuple[float, float]]:
        """Get entrance and exit coordinates for a block considering reversal.

        Args:
            block: The macro block.
            reversed: Whether the block should be traversed in reverse.

        Returns:
            Tuple of (entrance, exit) coordinates.
        """
        if reversed:
            return (
                (block.exit.x, block.exit.y),
                (block.entrance.x, block.entrance.y),
            )
        else:
            return (
                (block.entrance.x, block.entrance.y),
                (block.exit.x, block.exit.y),
            )

    def _calculate_insertion_cost(
        self,
        block: MacroBlock,
        tour: List[BlockTraverseState],
        insert_position: int,
        blocks: List[MacroBlock],
    ) -> Tuple[float, bool]:
        """Calculate cost of inserting a block at a specific position in the tour.

        For insertion between A and B (where B is at insert_position + 1):
        - Current distance: dist(A.exit, B.entrance)
        - New distance with X inserted: min over entry options for X

        When inserting after position i (i.e., before element at i+1 in tour),
        we consider the cost to connect from A=tour[i] to X and then from X
        to B=tour[i+1].

        Args:
            block: Block to insert.
            tour: Current tour state list.
            insert_position: Position index after which to insert (0 means before first).
            blocks: All macro blocks for looking up block info.

        Returns:
            Tuple of (minimum_insertion_cost, should_reverse) for inserting at
            this position with optimal orientation.
        """
        if not tour:
            _entrance, exit = self._get_block_endpoints(block, False)
            cost_entrance, rev_entrance = self._calculate_block_cost(
                (0.0, 0.0),
                (block.entrance.x, block.entrance.y),
                (block.exit.x, block.exit.y),
            )
            return (cost_entrance, rev_entrance)

        if insert_position == 0:
            a_exit = tour[0].entrance
        else:
            a_exit = tour[insert_position - 1].exit

        b_entrance: Tuple[float, float]
        b_exit: Tuple[float, float]

        if insert_position < len(tour):
            next_state = tour[insert_position]
            b_entrance = next_state.entrance
            b_exit = next_state.exit
        else:
            prev_state = tour[-1]
            b_entrance = prev_state.exit
            b_exit = prev_state.exit

        current_dist = math.sqrt(
            (b_entrance[0] - a_exit[0]) ** 2 + (b_entrance[1] - a_exit[1]) ** 2
        )

        best_cost = float('inf')
        best_reversed = False

        for reversed_flag in [False, True]:
            x_entrance, x_exit = self._get_block_endpoints(block, reversed_flag)

            cost_to_x_entrance = math.sqrt(
                (x_entrance[0] - a_exit[0]) ** 2 + (x_entrance[1] - a_exit[1]) ** 2
            )
            cost_from_x_exit = math.sqrt(
                (b_entrance[0] - x_exit[0]) ** 2 + (b_entrance[1] - x_exit[1]) ** 2
            )

            total_insertion_cost = cost_to_x_entrance + cost_from_x_exit

            if insert_position >= len(tour):
                cost_from_x_entrance = math.sqrt(
                    (x_entrance[0] - a_exit[0]) ** 2 + (x_entrance[1] - a_exit[1]) ** 2
                )
                cost_to_x_exit = math.sqrt(
                    (b_entrance[0] - x_exit[0]) ** 2 + (b_entrance[1] - x_exit[1]) ** 2
                )
                total_insertion_cost = cost_from_x_entrance + cost_to_x_exit

            if total_insertion_cost < best_cost:
                best_cost = total_insertion_cost
                best_reversed = reversed_flag

        return (best_cost, best_reversed)

    def _find_best_insertion_position(
        self,
        block: MacroBlock,
        tour: List[BlockTraverseState],
        blocks: List[MacroBlock],
    ) -> Tuple[int, BlockTraverseState, float]:
        """Find the best position to insert a block in the current tour.

        Evaluates all possible insertion positions and both orientations
        (entrance-first vs exit-first) for each position.

        Args:
            block: Block to potentially insert.
            tour: Current tour state list.
            blocks: All macro blocks.

        Returns:
            Tuple of (best_position, new_block_state, minimum_cost).
        """
        best_pos = -1
        best_state: Optional[BlockTraverseState] = None
        best_cost = float('inf')

        num_positions = len(tour) + 1

        for pos in range(num_positions):
            cost, should_reverse = self._calculate_insertion_cost(
                block, tour, pos, blocks
            )

            if cost < best_cost:
                best_cost = cost
                best_pos = pos

                if should_reverse:
                    best_state = BlockTraverseState(
                        block_id=block.block_id,
                        reversed=True,
                        entrance=(block.exit.x, block.exit.y),
                        exit=(block.entrance.x, block.entrance.y),
                    )
                else:
                    best_state = BlockTraverseState(
                        block_id=block.block_id,
                        reversed=False,
                        entrance=(block.entrance.x, block.entrance.y),
                        exit=(block.exit.x, block.exit.y),
                    )

        if best_state is None:
            raise OptimizationError(f"Failed to find insertion position for block {block.block_id}")

        return (best_pos, best_state, best_cost)


class ChristofidesStrategy(OptimizationStrategy):
    """Christofides-Serdyukov algorithm for S-T Path TSP with 5/3 approximation.

    This strategy implements Hoogeveen's modification of the Christofides-Serdyukov
    algorithm for the S-T Path TSP problem, where we need to find a minimum-weight
    path from a fixed start point S to a fixed end point T visiting all blocks.

    The algorithm:

    1. Build Minimum Spanning Tree (MST) of all vertices (block endpoints + S + T)
       using Prim's algorithm
    2. Find "wrong parity" vertices: block endpoints with odd MST degree, plus
       S and T with even MST degree
    3. Compute minimum-weight perfect matching on wrong-parity vertices
    4. Combine MST + matching edges to form Eulerian multigraph (exactly two
       odd-degree vertices: S and T)
    5. Find Eulerian path from S to T, then shortcut to Hamiltonian path

    The algorithm provides a theoretical guarantee that the resulting path length
    is at most 5/3 of the optimal S-T Path TSP tour.

    Each block has two endpoints (entrance and exit). For MST purposes, we treat
    each endpoint as a vertex but track which block they belong to. When building
    the final tour, we need to decide both sequence AND direction for each block.
    """

    # Special vertex IDs for start and end terminals
    START_VERTEX_ID: int = -1  # Reserved ID for S terminal
    END_VERTEX_ID: int = -2   # Reserved ID for T terminal

    DEFAULT_N_CANDIDATES: int = 2  # For finding closest to origin (start)
    DEFAULT_M_CANDIDATES: int = 2  # For finding farthest from origin (end)

    def __init__(self) -> None:
        """Initialize the Christofides-Serdyukov strategy."""
        super().__init__()
        self._start_point: Optional[Tuple[float, float]] = None
        self._end_point: Optional[Tuple[float, float]] = None

    @property
    def name(self) -> str:
        """Return the strategy name."""
        return "Christofides-Serdyukov S-T Path (5/3 approx)"

    def optimize(
        self,
        blocks: List[MacroBlock],
        start_point: Tuple[float, float],
        end_point: Tuple[float, float],
    ) -> OptimizationResult:
        """Optimize using Christofides-Serdyukov algorithm for S-T Path.

        Args:
            blocks: List of MacroBlocks to optimize.
            start_point: Fixed starting point as (x, y) tuple.
            end_point: Fixed ending point as (x, y) tuple.

        Returns:
            OptimizationResult with optimized traversal order from start_point
            to end_point.
        """
        self._logger.info(f"Running {self.name} on {len(blocks)} blocks")

        # Store terminal points as instance variables for access by helper methods
        self._start_point = start_point
        self._end_point = end_point

        if not blocks:
            return OptimizationResult(
                traverse_order=(),
                connections=(),
                total_travel_distance=0.0,
                initial_position=start_point,
            )

        if len(blocks) == 1:
            return self._optimize_single_block(blocks, start_point, end_point)

        if len(blocks) == 2:
            return self._optimize_two_blocks(blocks, start_point, end_point)

        # Evaluate multiple candidates for best S-T path
        # Find closest endpoints to origin for start candidates,
        # and farthest endpoints from origin for end candidates
        start_candidates = self._find_nearest_endpoints(
            blocks, origin=(0.0, 0.0), n_candidates=self.DEFAULT_N_CANDIDATES
        )
        end_candidates = self._find_farthest_origin_endpoints(
            blocks, origin=(0.0, 0.0), n_candidates=self.DEFAULT_M_CANDIDATES
        )

        best_result: Optional[OptimizationResult] = None

        # Try combinations of start and end endpoint candidates
        for start_entry, start_block_idx, start_is_exit, _ in start_candidates:
            for end_entry, end_block_idx, end_is_exit, _ in end_candidates:
                if start_block_idx == end_block_idx and len(blocks) > 1:
                    # Can't use same block as both entry and exit unless it's the only one
                    continue

                result = self._optimize_with_terminals(
                    blocks,
                    actual_start_point=start_entry,
                    actual_end_point=end_entry,
                )

                if best_result is None or result.total_travel_distance < best_result.total_travel_distance:
                    best_result = result
                    self._logger.debug(
                        f"New best Christofides path: {result.total_travel_distance:.3f} "
                        f"from candidate ({start_block_idx}, {'exit' if start_is_exit else 'entrance'}) "
                        f"to ({end_block_idx}, {'exit' if end_is_exit else 'entrance'})"
                    )

        if best_result is None:
            raise OptimizationError("Failed to find valid Christofides S-T path")

        return best_result

    def _optimize_with_terminals(
        self,
        blocks: List[MacroBlock],
        actual_start_point: Tuple[float, float],
        actual_end_point: Tuple[float, float],
    ) -> OptimizationResult:
        """Optimize with specific start and end terminal points.

        This is the core optimization that builds the MST + matching and finds
        the S-T path through all blocks.

        Args:
            blocks: List of MacroBlocks to optimize.
            actual_start_point: The actual starting coordinate for this candidate.
            actual_end_point: The actual ending coordinate for this candidate.

        Returns:
            OptimizationResult for this terminal configuration.
        """
        self._start_point = actual_start_point
        self._end_point = actual_end_point

        vertices = self._create_vertices(blocks, actual_start_point, actual_end_point)
        start_vertex = self.START_VERTEX_ID
        end_vertex = self.END_VERTEX_ID

        mst_edges = self._build_mst_prim(vertices, start_vertex)

        wrong_parity_vertices = self._find_wrong_parity_vertices(
            mst_edges, vertices, start_vertex, end_vertex
        )

        matching_edges = self._greedy_perfect_matching(wrong_parity_vertices, vertices)

        eulerian_edges = list(mst_edges) + matching_edges

        eulerian_path = self._build_eulerian_path(
            eulerian_edges, start_vertex, vertices
        )

        hamiltonian_sequence = self._euler_to_hamiltonian_shortcut_st_path(
            eulerian_path, blocks, actual_end_point
        )

        tour = self._create_traverse_order_st_path(
            hamiltonian_sequence, blocks, actual_start_point, actual_end_point
        )

        connections = self._build_connections(blocks, tour, actual_start_point)
        total_distance = sum(c.travel_distance for c in connections if c.source_block_id >= 0)

        return OptimizationResult(
            traverse_order=tuple(tour),
            connections=connections,
            total_travel_distance=total_distance,
            initial_position=actual_start_point,
        )

    def _optimize_single_block(
        self,
        blocks: List[MacroBlock],
        start_point: Tuple[float, float],
        end_point: Tuple[float, float],
    ) -> OptimizationResult:
        """Optimize case with single block for S-T path.

        Args:
            blocks: Single-block list.
            start_point: Fixed starting point as (x, y).
            end_point: Fixed ending point as (x, y).

        Returns:
            OptimizationResult for single block from start_point to end_point.
        """
        block = blocks[0]

        # Evaluate all four combinations: entry direction and exit direction
        # We need to go from start_point to some entry of the block,
        # traverse it, then go to end_point

        candidates: List[Tuple[float, bool]] = []  # (cost, reversed)

        for entrance_is_exit in [False, True]:
            if entrance_is_exit:
                entry_coord = (block.exit.x, block.exit.y)
                exit_coord = (block.entrance.x, block.entrance.y)
            else:
                entry_coord = (block.entrance.x, block.entrance.y)
                exit_coord = (block.exit.x, block.exit.y)

            cost_to_entry = math.sqrt(
                (entry_coord[0] - start_point[0]) ** 2
                + (entry_coord[1] - start_point[1]) ** 2
            )
            cost_from_exit = math.sqrt(
                (end_point[0] - exit_coord[0]) ** 2
                + (end_point[1] - exit_coord[1]) ** 2
            )
            total_cost = cost_to_entry + cost_from_exit
            candidates.append((total_cost, entrance_is_exit))

        candidates.sort(key=lambda x: x[0])
        best_cost, best_reversed = candidates[0]

        if best_reversed:
            tour_state = BlockTraverseState(
                block_id=block.block_id,
                reversed=True,
                entrance=(block.exit.x, block.exit.y),
                exit=(block.entrance.x, block.entrance.y),
            )
        else:
            tour_state = BlockTraverseState(
                block_id=block.block_id,
                reversed=False,
                entrance=(block.entrance.x, block.entrance.y),
                exit=(block.exit.x, block.exit.y),
            )

        connections = self._build_connections(blocks, [tour_state], start_point)
        total_distance = sum(c.travel_distance for c in connections if c.source_block_id >= 0)

        return OptimizationResult(
            traverse_order=(tour_state,),
            connections=connections,
            total_travel_distance=total_distance,
            initial_position=start_point,
        )

    def _optimize_two_blocks(
        self,
        blocks: List[MacroBlock],
        start_point: Tuple[float, float],
        end_point: Tuple[float, float],
    ) -> OptimizationResult:
        """Optimize case with two blocks for S-T path.

        Args:
            blocks: Two-block list.
            start_point: Fixed starting point as (x, y).
            end_point: Fixed ending point as (x, y).

        Returns:
            OptimizationResult for two blocks from start_point to end_point.
        """
        best_tour: Optional[List[BlockTraverseState]] = None
        best_distance = float('inf')

        # Try all orderings and orientations of the two blocks
        for first_idx, second_idx in [(0, 1), (1, 0)]:
            for first_rev in [False, True]:
                for second_rev in [False, True]:
                    tour = self._try_two_block_configuration(
                        blocks, first_idx, first_rev, second_idx, second_rev
                    )
                    distance = self._calculate_st_path_distance(
                        tour, blocks, start_point, end_point
                    )
                    if distance < best_distance:
                        best_distance = distance
                        best_tour = tour

        if best_tour is None:
            raise OptimizationError("Failed to find valid two-block path")

        connections = self._build_connections(blocks, best_tour, start_point)
        total_distance = sum(c.travel_distance for c in connections if c.source_block_id >= 0)

        return OptimizationResult(
            traverse_order=tuple(best_tour),
            connections=connections,
            total_travel_distance=total_distance,
            initial_position=start_point,
        )

    def _try_two_block_configuration(
        self,
        blocks: List[MacroBlock],
        first_idx: int,
        first_rev: bool,
        second_idx: int,
        second_rev: bool,
    ) -> List[BlockTraverseState]:
        """Try a specific configuration for two blocks.

        Args:
            blocks: Two-block list.
            first_idx: Index of first block in sequence.
            first_rev: Whether to traverse first block in reverse.
            second_idx: Index of second block in sequence.
            second_rev: Whether to traverse second block in reverse.

        Returns:
            List of BlockTraverseState for this configuration.
        """
        tour: List[BlockTraverseState] = []

        first_block = blocks[first_idx]
        if first_rev:
            tour.append(BlockTraverseState(
                block_id=first_block.block_id,
                reversed=True,
                entrance=(first_block.exit.x, first_block.exit.y),
                exit=(first_block.entrance.x, first_block.entrance.y),
            ))
        else:
            tour.append(BlockTraverseState(
                block_id=first_block.block_id,
                reversed=False,
                entrance=(first_block.entrance.x, first_block.entrance.y),
                exit=(first_block.exit.x, first_block.exit.y),
            ))

        second_block = blocks[second_idx]
        if second_rev:
            tour.append(BlockTraverseState(
                block_id=second_block.block_id,
                reversed=True,
                entrance=(second_block.exit.x, second_block.exit.y),
                exit=(second_block.entrance.x, second_block.entrance.y),
            ))
        else:
            tour.append(BlockTraverseState(
                block_id=second_block.block_id,
                reversed=False,
                entrance=(second_block.entrance.x, second_block.entrance.y),
                exit=(second_block.exit.x, second_block.exit.y),
            ))

        return tour

    def _calculate_st_path_distance(
        self,
        tour: List[BlockTraverseState],
        blocks: List[MacroBlock],
        start_point: Tuple[float, float],
        end_point: Tuple[float, float],
    ) -> float:
        """Calculate total S-T path distance for a tour.

        Args:
            tour: Traverse order.
            blocks: All macro blocks.
            start_point: Fixed starting point.
            end_point: Fixed ending point.

        Returns:
            Total travel distance from start_point through all blocks to end_point.
        """
        if not tour:
            return math.sqrt(
                (end_point[0] - start_point[0]) ** 2
                + (end_point[1] - start_point[1]) ** 2
            )

        # Distance from start to first block entrance
        first_state = tour[0]
        distance = math.sqrt(
            (first_state.entrance[0] - start_point[0]) ** 2
            + (first_state.entrance[1] - start_point[1]) ** 2
        )

        # Distance between blocks
        for i in range(len(tour) - 1):
            curr_exit = tour[i].exit
            next_entrance = tour[i + 1].entrance
            distance += math.sqrt(
                (next_entrance[0] - curr_exit[0]) ** 2
                + (next_entrance[1] - curr_exit[1]) ** 2
            )

        # Distance from last block exit to end_point
        last_state = tour[-1]
        distance += math.sqrt(
            (end_point[0] - last_state.exit[0]) ** 2
            + (end_point[1] - last_state.exit[1]) ** 2
        )

        return distance

    def _create_vertices(
        self,
        blocks: List[MacroBlock],
        start_point: Tuple[float, float],
        end_point: Tuple[float, float],
    ) -> Dict[int, Tuple[float, float, int, bool]]:
        """Create vertex mapping from block endpoints plus S and T terminals.

        Args:
            blocks: All macro blocks.
            start_point: Fixed starting point as (x, y).
            end_point: Fixed ending point as (x, y).

        Returns:
            Dictionary mapping vertex_id to (x, y, block_index, is_exit).
            Special IDs: START_VERTEX_ID (-1) for S, END_VERTEX_ID (-2) for T.
        """
        vertices: Dict[int, Tuple[float, float, int, bool]] = {}

        # Add start and end terminals with special IDs
        vertices[self.START_VERTEX_ID] = (start_point[0], start_point[1], -1, False)
        vertices[self.END_VERTEX_ID] = (end_point[0], end_point[1], -2, True)

        vid = 0

        for i, block in enumerate(blocks):
            vertices[vid] = (block.entrance.x, block.entrance.y, i, False)
            vid += 1
            vertices[vid] = (block.exit.x, block.exit.y, i, True)
            vid += 1

        return vertices

    def _find_nearest_endpoints(
        self,
        blocks: List[MacroBlock],
        origin: Tuple[float, float] = (0.0, 0.0),
        n_candidates: int = 5,
    ) -> List[Tuple[Tuple[float, float], int, bool, float]]:
        """Find the N block endpoints nearest to a reference point.

        This ensures the optimization evaluates multiple starting/ending candidates
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

    def _find_farthest_origin_endpoints(
        self,
        blocks: List[MacroBlock],
        origin: Tuple[float, float] = (0.0, 0.0),
        n_candidates: int = 5,
    ) -> List[Tuple[Tuple[float, float], int, bool, float]]:
        """Find the N block endpoints farthest from a reference point.

        This is used for finding candidate ending points that are far from
        the origin (machine home position).

        Args:
            blocks: List of MacroBlocks to search.
            origin: Reference point for distance calculation (default origin).
            n_candidates: Number of farthest endpoints to return.

        Returns:
            List of tuples sorted by distance descending: [(position, block_index, is_exit, distance), ...].
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

        # Sort by distance descending to get farthest first
        candidates.sort(key=lambda x: x[0], reverse=True)
        return [(pos, idx, is_exit, dist) for dist, (pos, idx, is_exit) in candidates[:n_candidates]]

    def _build_mst_prim(
        self,
        vertices: Dict[int, Tuple[float, float, int, bool]],
        start_vertex: int,
    ) -> List[Tuple[int, int]]:
        """Build Minimum Spanning Tree using Prim's algorithm.

        Args:
            vertices: All vertices in the graph (including S and T terminals).
            start_vertex: Vertex ID to start from (should be START_VERTEX_ID).

        Returns:
            List of edges (u, v) forming the MST.
        """
        if not vertices:
            return []

        in_mst = set([start_vertex])
        mst_edges: List[Tuple[int, int]] = []
        heap: List[Tuple[float, int, int]] = []

        for vid in vertices:
            if vid != start_vertex:
                dist = self._vertex_distance(vertices[start_vertex], vertices[vid])
                heap.append((dist, start_vertex, vid))

        import heapq
        heapq.heapify(heap)

        while heap and len(in_mst) < len(vertices):
            dist, u, v = heapq.heappop(heap)

            if v in in_mst:
                continue

            in_mst.add(v)
            mst_edges.append((u, v))

            for vid in vertices:
                if vid not in in_mst:
                    new_dist = self._vertex_distance(vertices[v], vertices[vid])
                    heapq.heappush(heap, (new_dist, v, vid))

        return mst_edges

    def _vertex_distance(
        self,
        v1: Tuple[float, float, int, bool],
        v2: Tuple[float, float, int, bool],
    ) -> float:
        """Calculate Euclidean distance between two vertices.

        Args:
            v1: First vertex (x, y, block_idx, is_exit).
            v2: Second vertex (x, y, block_idx, is_exit).

        Returns:
            Euclidean distance between the two vertices.
        """
        return math.sqrt((v2[0] - v1[0]) ** 2 + (v2[1] - v1[1]) ** 2)

    def _find_wrong_parity_vertices(
        self,
        mst_edges: List[Tuple[int, int]],
        vertices: Dict[int, Tuple[float, float, int, bool]],
        start_vertex: int,
        end_vertex: int,
    ) -> List[int]:
        """Find vertices with "wrong" parity for S-T Path TSP.

        For standard block endpoints: add to set O if MST degree is odd.
        For start_point vertex: add to set O if MST degree is even.
        For end_point vertex: add to set O if MST degree is even.

        This ensures that after adding the matching edges, exactly S and T
        will have odd degree (all other vertices even), enabling an Eulerian
        path from S to T.

        Args:
            mst_edges: Edges of the MST.
            vertices: All vertices including S and T terminals.
            start_vertex: Vertex ID of start terminal (START_VERTEX_ID).
            end_vertex: Vertex ID of end terminal (END_VERTEX_ID).

        Returns:
            List of vertex IDs that need to be matched ("wrong parity" set O).
        """
        degree: Dict[int, int] = {vid: 0 for vid in vertices}

        for u, v in mst_edges:
            degree[u] += 1
            degree[v] += 1

        wrong_parity: List[int] = []

        for vid, deg in degree.items():
            if vid == start_vertex or vid == end_vertex:
                # S and T terminals: add if degree is even (should be odd)
                if deg % 2 == 0:
                    wrong_parity.append(vid)
            else:
                # Block endpoints: add if degree is odd (should be even)
                if deg % 2 == 1:
                    wrong_parity.append(vid)

        return wrong_parity

    def _greedy_perfect_matching(
        self,
        odd_vertices: List[int],
        vertices: Dict[int, Tuple[float, float, int, bool]],
    ) -> List[Tuple[int, int]]:
        """Compute minimum-weight perfect matching on "wrong parity" vertices.

        Uses a simplified greedy approach: iteratively pair the closest
        unmatched vertices. This is not optimal but provides a valid
        perfect matching with reasonable quality.

        Args:
            odd_vertices: List of vertex IDs with wrong parity (set O).
            vertices: All vertices for distance calculations.

        Returns:
            List of edges (u, v) forming the matching.
        """
        if not odd_vertices:
            return []

        remaining = set(odd_vertices)
        matching_edges: List[Tuple[int, int]] = []

        while remaining:
            u = next(iter(remaining))
            remaining.remove(u)

            best_v = -1
            best_dist = float('inf')

            for v in remaining:
                dist = self._vertex_distance(vertices[u], vertices[v])
                if dist < best_dist:
                    best_dist = dist
                    best_v = v

            if best_v != -1:
                remaining.remove(best_v)
                matching_edges.append((u, best_v))

        return matching_edges

    def _build_eulerian_path(
        self,
        edges: List[Tuple[int, int]],
        start_vertex: int,
        vertices: Dict[int, Tuple[float, float, int, bool]],
    ) -> List[int]:
        """Build Eulerian path from S to T using Hierholzer's algorithm.

        Because of the parity adjustment in Hoogeveen's method, the multigraph
        (MST + Matching) has exactly two odd-degree vertices: S and T.
        Starting from S, Hierholzer's algorithm will naturally terminate at T.

        Args:
            edges: Combined MST + matching edges forming Eulerian multigraph.
            start_vertex: Vertex ID to start the path from (should be START_VERTEX_ID).
            vertices: All vertices for adjacency lookup.

        Returns:
            List of vertex IDs in Eulerian path order from S to T.
        """
        if not edges:
            return [start_vertex]

        adjacency: Dict[int, List[int]] = {vid: [] for vid in vertices}
        edge_count: Dict[Tuple[int, int], int] = {}

        for u, v in edges:
            adjacency[u].append(v)
            adjacency[v].append(u)
            edge_count[(u, v)] = edge_count.get((u, v), 0) + 1
            edge_count[(v, u)] = edge_count.get((v, u), 0) + 1

        stack: List[int] = [start_vertex]
        path: List[int] = []

        while stack:
            current = stack[-1]

            if adjacency[current]:
                next_v = adjacency[current].pop()
                adjacency[next_v].remove(current)

                edge_key = (current, next_v)
                edge_count[edge_key] -= 1

                stack.append(next_v)
            else:
                path.append(stack.pop())

        # Path is built in reverse order; since we start from S and end at T,
        # the reversal gives us the correct Eulerian path
        return list(reversed(path))

    def _euler_to_hamiltonian_shortcut_st_path(
        self,
        eulerian_path: List[int],
        blocks: List[MacroBlock],
        end_point: Tuple[float, float],
    ) -> List[Tuple[int, bool]]:
        """Convert Eulerian path to Hamiltonian S-T path by skipping visited nodes.

        When traversing the Eulerian path from S to T, we skip vertices that belong
        to a block already visited. For each block, we determine whether to traverse
        it forward or in reverse based on entry direction. The last vertex in the
        sequence should be T (end_point), which is not a block endpoint.

        Args:
            eulerian_path: List of vertex IDs in Eulerian path order from S to T.
            blocks: All macro blocks for state lookups.
            end_point: Fixed ending point as (x, y).

        Returns:
            List of (block_idx, reversed) tuples representing the Hamiltonian S-T path.
        """
        visited_blocks = set()
        hamiltonian: List[Tuple[int, bool]] = []
        prev_vertex: Optional[int] = None

        # The Eulerian path starts with S and ends with T
        num_vertices = len(eulerian_path)

        for i in range(num_vertices):
            vid = eulerian_path[i]

            x, y, block_idx, is_exit = self._get_vertex_info_st(vid, blocks)

            # Skip the start vertex (S) - it's not a block
            if block_idx == -1:
                continue

            # Skip T (end terminal) and already visited blocks
            if block_idx == -2 or block_idx in visited_blocks:
                continue

            should_reverse = False

            if prev_vertex is None:
                # First real block after S: determine direction based on entry from S
                # If we enter at an exit point (is_exit=True), reverse the traversal
                should_reverse = self._determine_reversal_for_first_block(
                    vid, blocks
                )
            else:
                _, _, prev_block_idx, _ = self._get_vertex_info_st(prev_vertex, blocks)

                entry_at_prev: Tuple[float, float]
                if prev_block_idx == block_idx:
                    # Same block (shouldn't happen in shortcutting normally)
                    continue
                elif prev_block_idx == -1:
                    # Previous was S terminal
                    entry_at_prev = self._get_start_coords()
                else:
                    entry_at_prev = self._get_vertex_coords(prev_vertex, blocks)

                dist_to_entrance = math.sqrt(
                    (blocks[block_idx].entrance.x - entry_at_prev[0]) ** 2
                    + (blocks[block_idx].entrance.y - entry_at_prev[1]) ** 2
                )
                dist_to_exit = math.sqrt(
                    (blocks[block_idx].exit.x - entry_at_prev[0]) ** 2
                    + (blocks[block_idx].exit.y - entry_at_prev[1]) ** 2
                )

                if is_exit:
                    should_reverse = False
                else:
                    should_reverse = dist_to_exit < dist_to_entrance

            visited_blocks.add(block_idx)
            hamiltonian.append((block_idx, should_reverse))
            prev_vertex = vid

        return hamiltonian

    def _determine_reversal_for_first_block(
        self,
        first_vid: int,
        blocks: List[MacroBlock],
    ) -> bool:
        """Determine whether to reverse the first block based on entry from S.

        When we enter a block from S at a particular endpoint, we need to decide
        if that entry point is an exit (meaning we'd be entering "backwards" and
        should reverse) or entrance (traverse forward).

        Args:
            first_vid: Vertex ID of first block endpoint.
            blocks: All macro blocks.

        Returns:
            True if block should be traversed in reverse.
        """
        _, _, block_idx, is_exit = self._get_vertex_info_st(first_vid, blocks)

        # If the Eulerian path hits an exit point as entry, we need to traverse
        # the block in reverse (enter at exit, go backwards to entrance)
        if is_exit:
            return True

        return False

    def _get_vertex_info_st(
        self,
        vid: int,
        blocks: List[MacroBlock],
    ) -> Tuple[float, float, int, bool]:
        """Get vertex information from a vertex ID including S and T terminals.

        Args:
            vid: Vertex ID. Special values: START_VERTEX_ID (-1) for S,
                END_VERTEX_ID (-2) for T.
            blocks: All macro blocks to find the corresponding endpoint.

        Returns:
            Tuple of (x, y, block_index, is_exit).
            - For S terminal: returns start_point coords with block_idx=-1
            - For T terminal: returns end_point coords with block_idx=-2
            - For block endpoints: standard behavior
        """
        if vid == self.START_VERTEX_ID:
            sp = self._get_start_coords()
            return (sp[0], sp[1], -1, False)
        elif vid == self.END_VERTEX_ID:
            ep = self._get_end_coords()
            return (ep[0], ep[1], -2, True)

        num_endpoints = len(blocks) * 2

        if vid < 0 or vid >= num_endpoints:
            return (0.0, 0.0, -1, False)

        block_idx = vid // 2
        is_exit = (vid % 2) == 1

        block = blocks[block_idx]
        if is_exit:
            return (block.exit.x, block.exit.y, block_idx, True)
        else:
            return (block.entrance.x, block.entrance.y, block_idx, False)

    def _get_start_coords(self) -> Tuple[float, float]:
        """Get the coordinates of the start terminal.

        Returns:
            Tuple of (x, y) coordinates for the S terminal.
        """
        if self._start_point is not None:
            return self._start_point
        return (0.0, 0.0)

    def _get_end_coords(self) -> Tuple[float, float]:
        """Get the coordinates of the end terminal.

        Returns:
            Tuple of (x, y) coordinates for the T terminal.
        """
        if self._end_point is not None:
            return self._end_point
        return (0.0, 0.0)

    def _get_vertex_coords(
        self,
        vid: int,
        blocks: List[MacroBlock],
    ) -> Tuple[float, float]:
        """Get coordinates of a vertex.

        Args:
            vid: Vertex ID.
            blocks: All macro blocks.

        Returns:
            Tuple of (x, y) coordinates.
        """
        x, y, _, _ = self._get_vertex_info_st(vid, blocks)
        return (x, y)

    def _create_traverse_order_st_path(
        self,
        hamiltonian_sequence: List[Tuple[int, bool]],
        blocks: List[MacroBlock],
        start_point: Tuple[float, float],
        end_point: Tuple[float, float],
    ) -> List[BlockTraverseState]:
        """Create BlockTraverseState list from Hamiltonian S-T path sequence.

        Args:
            hamiltonian_sequence: List of (block_idx, reversed) tuples.
            blocks: All macro blocks for coordinate lookups.
            start_point: Fixed starting point as (x, y).
            end_point: Fixed ending point as (x, y).

        Returns:
            List of BlockTraverseState objects forming path from start to end.
        """
        if not hamiltonian_sequence:
            return []

        first_block_idx, _ = hamiltonian_sequence[0]
        first_block = blocks[first_block_idx]

        # Determine actual reversal for first block based on distance from start_point
        dist_to_entrance_first = math.sqrt(
            (first_block.entrance.x - start_point[0]) ** 2
            + (first_block.entrance.y - start_point[1]) ** 2
        )
        dist_to_exit_first = math.sqrt(
            (first_block.exit.x - start_point[0]) ** 2
            + (first_block.exit.y - start_point[1]) ** 2
        )

        # Choose the entry point that is closer to start_point
        if dist_to_entrance_first <= dist_to_exit_first:
            actual_first_reversed = False
            first_entry = (first_block.entrance.x, first_block.entrance.y)
            first_exit = (first_block.exit.x, first_block.exit.y)
        else:
            actual_first_reversed = True
            first_entry = (first_block.exit.x, first_block.exit.y)
            first_exit = (first_block.entrance.x, first_block.entrance.y)

        tour: List[BlockTraverseState] = []

        for block_idx, sequence_reversed in hamiltonian_sequence:
            block = blocks[block_idx]

            if block_idx == first_block_idx:
                # First block - use the actual reversal based on start_point distance
                if actual_first_reversed:
                    state = BlockTraverseState(
                        block_id=block.block_id,
                        reversed=True,
                        entrance=(block.exit.x, block.exit.y),
                        exit=(block.entrance.x, block.entrance.y),
                    )
                else:
                    state = BlockTraverseState(
                        block_id=block.block_id,
                        reversed=False,
                        entrance=(block.entrance.x, block.entrance.y),
                        exit=(block.exit.x, block.exit.y),
                    )
            else:
                prev_state = tour[-1]
                entry_point = prev_state.exit

                dist_to_entrance = math.sqrt(
                    (block.entrance.x - entry_point[0]) ** 2
                    + (block.entrance.y - entry_point[1]) ** 2
                )
                dist_to_exit = math.sqrt(
                    (block.exit.x - entry_point[0]) ** 2
                    + (block.exit.y - entry_point[1]) ** 2
                )

                if dist_to_entrance <= dist_to_exit:
                    actual_reversed = False
                    state = BlockTraverseState(
                        block_id=block.block_id,
                        reversed=False,
                        entrance=(block.entrance.x, block.entrance.y),
                        exit=(block.exit.x, block.exit.y),
                    )
                else:
                    actual_reversed = True
                    state = BlockTraverseState(
                        block_id=block.block_id,
                        reversed=True,
                        entrance=(block.exit.x, block.exit.y),
                        exit=(block.entrance.x, block.entrance.y),
                    )

            tour.append(state)

        return tour


class SimulatedAnnealingStrategy(OptimizationStrategy):
    """Simulated Annealing algorithm for TSP optimization.

    This strategy implements Simulated Annealing, a global search technique that
    can escape local minima by accepting worse solutions with probability that
    decreases over time. The algorithm:

    1. Start with an initial tour (nearest neighbor heuristic)
    2. Initialize temperature T
    3. For each temperature level:
       - Generate random neighbor of current solution (segment reversal or block swap)
       - Calculate delta = new_cost - current_cost
       - If delta < 0: accept new solution
       - If delta > 0: accept with probability exp(-delta/T)
    4. Reduce temperature according to cooling schedule
    5. Repeat until stopping criteria met

    Key parameters:
        Initial temperature: 10000
        Cooling rate: 0.9995
        Iterations per temperature: 50
        Minimum temperature (stopping): 1e-8

    Neighbor generation:
        - With 70% probability: random segment reversal (2-opt style)
        - With 30% probability: swap two random blocks in tour
    """

    DEFAULT_INITIAL_TEMPERATURE: float = 8000.0
    DEFAULT_COOLING_RATE: float = 0.6
    DEFAULT_ITERATIONS_PER_TEMP: int = 50
    DEFAULT_MIN_TEMPERATURE: float = 1e-4

    def __init__(
        self,
        initial_temperature: float = DEFAULT_INITIAL_TEMPERATURE,
        cooling_rate: float = DEFAULT_COOLING_RATE,
        iterations_per_temp: int = DEFAULT_ITERATIONS_PER_TEMP,
        min_temperature: float = DEFAULT_MIN_TEMPERATURE,
    ) -> None:
        """Initialize the Simulated Annealing strategy.

        Args:
            initial_temperature: Starting temperature for SA. Default 10000.
            cooling_rate: Temperature multiplier per level (0 < rate < 1). Default 0.9995.
            iterations_per_temp: Iterations at each temperature level. Default 50.
            min_temperature: Stopping criterion temperature. Default 1e-8.
        """
        super().__init__()
        self._initial_temperature = initial_temperature
        self._cooling_rate = cooling_rate
        self._iterations_per_temp = iterations_per_temp
        self._min_temperature = min_temperature

    DEFAULT_N_CANDIDATES: int = 2

    @property
    def name(self) -> str:
        """Return the strategy name."""
        return "Simulated Annealing"

    def optimize(
        self,
        blocks: List[MacroBlock],
        initial_position: Optional[Tuple[float, float]] = None,
    ) -> OptimizationResult:
        """Optimize using Simulated Annealing algorithm.

        When no initial_position is specified, this method evaluates multiple
        candidate starting points (the N closest endpoints to origin) and selects
        the one that yields minimum total travel distance.

        Args:
            blocks: List of MacroBlocks to optimize.
            initial_position: Starting position for optimization. If None,
                uses the closest endpoint to origin as starting point.

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

        start_pos: Tuple[float, float]
        candidates: List[Tuple[Tuple[float, float], int, bool, float]]

        if initial_position is not None:
            start_pos = initial_position
            candidates = [(start_pos, -1, False, 0.0)]
        else:
            # Find N closest endpoints to origin and evaluate each
            candidates = self._find_nearest_origin_endpoints(
                blocks, origin=(0.0, 0.0), n_candidates=self.DEFAULT_N_CANDIDATES
            )
            self._logger.debug(f"Evaluating {len(candidates)} starting candidates")

        best_result: Optional[OptimizationResult] = None

        for start_pos, first_block_idx, start_at_exit, _dist in candidates:
            candidate_result = self._optimize_from_start(blocks, start_pos)

            if best_result is None or candidate_result.total_travel_distance < best_result.total_travel_distance:
                best_result = candidate_result
                self._logger.debug(f"New best: distance={candidate_result.total_travel_distance:.3f} from start at {start_pos}")

        return best_result  # type: ignore[return-value]

    def _optimize_from_start(
        self,
        blocks: List[MacroBlock],
        start_pos: Tuple[float, float],
    ) -> OptimizationResult:
        """Optimize starting from a specific position.

        Args:
            blocks: List of MacroBlocks to optimize.
            start_pos: Starting position for optimization.

        Returns:
            OptimizationResult with optimized traversal order.
        """
        if len(blocks) == 1:
            return self._optimize_single_block(blocks, start_pos)

        tour = self._generate_initial_tour(blocks, start_pos)

        if len(tour) < 4:
            connections = self._build_connections(blocks, tour, start_pos)
            total_distance = sum(c.travel_distance for c in connections if c.source_block_id >= 0)
            return OptimizationResult(
                traverse_order=tuple(tour),
                connections=connections,
                total_travel_distance=total_distance,
                initial_position=start_pos,
            )

        current_tour = list(tour)
        current_cost = self._calculate_tour_distance(current_tour, blocks)

        best_tour = list(current_tour)
        best_cost = current_cost

        temperature = self._initial_temperature

        while temperature > self._min_temperature:
            for _ in range(self._iterations_per_temp):
                neighbor = self._generate_neighbor(list(current_tour))
                neighbor_cost = self._calculate_tour_distance(neighbor, blocks)

                delta = neighbor_cost - current_cost

                if delta < 0 or self._acceptance_probability(delta, temperature):
                    current_tour = neighbor
                    current_cost = neighbor_cost

                    if current_cost < best_cost:
                        best_tour = list(current_tour)
                        best_cost = current_cost

            temperature *= self._cooling_rate

        connections = self._build_connections(blocks, best_tour, start_pos)
        total_distance = sum(c.travel_distance for c in connections if c.source_block_id >= 0)

        return OptimizationResult(
            traverse_order=tuple(best_tour),
            connections=connections,
            total_travel_distance=total_distance,
            initial_position=start_pos,
        )

    def _generate_initial_tour(
        self,
        blocks: List[MacroBlock],
        start_pos: Tuple[float, float],
    ) -> List[BlockTraverseState]:
        """Build initial tour using nearest neighbor heuristic.

        This provides a reasonable starting point for the SA algorithm.

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

    def _calculate_tour_distance(
        self,
        tour: List[BlockTraverseState],
        blocks: List[MacroBlock],
    ) -> float:
        """Calculate total travel distance of a tour.

        Args:
            tour: Traverse order to evaluate.
            blocks: All macro blocks for coordinate lookups.

        Returns:
            Total Euclidean distance traveling through all blocks in the tour.
        """
        if not tour:
            return 0.0

        total_distance = 0.0

        for i in range(len(tour) - 1):
            current_exit = tour[i].exit
            next_entrance = tour[i + 1].entrance
            dist = math.sqrt(
                (next_entrance[0] - current_exit[0]) ** 2
                + (next_entrance[1] - current_exit[1]) ** 2
            )
            total_distance += dist

        return total_distance

    def _generate_neighbor(self, tour: List[BlockTraverseState]) -> List[BlockTraverseState]:
        """Generate a neighboring solution via segment reversal or block swap.

        With 70% probability: random segment reversal (2-opt style)
        With 30% probability: swap two random blocks in tour

        Args:
            tour: Current traverse order.

        Returns:
            New traverse order as a neighbor of the input.
        """
        import random
        new_tour = list(tour)

        if len(new_tour) < 2:
            return new_tour

        if random.random() < 0.7:
            i = random.randint(0, len(new_tour) - 1)
            j = random.randint(i + 1, len(new_tour))
            new_tour[i:j] = reversed(new_tour[i:j])
        else:
            idx1 = random.randint(0, len(new_tour) - 1)
            idx2 = random.randint(0, len(new_tour) - 1)
            if idx1 != idx2:
                new_tour[idx1], new_tour[idx2] = new_tour[idx2], new_tour[idx1]

        return new_tour

    def _acceptance_probability(self, delta: float, temperature: float) -> bool:
        """Calculate Metropolis acceptance criterion.

        Determines whether to accept a worse solution based on the
        Metropolis criterion: P(accept) = exp(-delta/T)

        Args:
            delta: Cost difference (new_cost - current_cost). Positive means worse.
            temperature: Current temperature value.

        Returns:
            True if the worse solution should be accepted, False otherwise.
        """
        import random
        if temperature <= 0:
            return False
        probability = math.exp(-delta / temperature)
        return random.random() < probability

    def _optimize_single_block(
        self,
        blocks: List[MacroBlock],
        start_pos: Tuple[float, float],
    ) -> OptimizationResult:
        """Optimize case with single block.

        Args:
            blocks: Single-block list.
            start_pos: Starting position for the optimization.

        Returns:
            OptimizationResult for single block.
        """
        block = blocks[0]

        cost_to_entrance = math.sqrt(
            (block.entrance.x - start_pos[0]) ** 2
            + (block.entrance.y - start_pos[1]) ** 2
        )
        cost_to_exit = math.sqrt(
            (block.exit.x - start_pos[0]) ** 2
            + (block.exit.y - start_pos[1]) ** 2
        )

        if cost_to_entrance <= cost_to_exit:
            reversed_flag = False
            tour_state = BlockTraverseState(
                block_id=block.block_id,
                reversed=False,
                entrance=(block.entrance.x, block.entrance.y),
                exit=(block.exit.x, block.exit.y),
            )
        else:
            reversed_flag = True
            tour_state = BlockTraverseState(
                block_id=block.block_id,
                reversed=True,
                entrance=(block.exit.x, block.exit.y),
                exit=(block.entrance.x, block.entrance.y),
            )

        connections = self._build_connections(blocks, [tour_state], start_pos)
        total_distance = sum(c.travel_distance for c in connections if c.source_block_id >= 0)

        return OptimizationResult(
            traverse_order=(tour_state,),
            connections=connections,
            total_travel_distance=total_distance,
            initial_position=start_pos,
        )

    def _find_nearest_origin_endpoint(
        self,
        blocks: List[MacroBlock],
        origin: Tuple[float, float] = (0.0, 0.0),
    ) -> Tuple[Tuple[float, float], int, bool]:
        """Find the block endpoint nearest to the origin.

        Args:
            blocks: List of MacroBlocks to search.
            origin: Reference point for distance calculation (default origin).

        Returns:
            Tuple of (nearest_position, block_index, is_exit).
            - nearest_position: (x, y) coordinates closest to origin
            - block_index: index of the block containing this endpoint
            - is_exit: True if nearest position is block's exit (needs reversal)
        """
        min_dist = float('inf')
        best_pos: Tuple[float, float] = (0.0, 0.0)
        best_idx = 0
        best_is_exit = False

        for i, block in enumerate(blocks):
            dist_entrance = math.sqrt(
                (block.entrance.x - origin[0]) ** 2
                + (block.entrance.y - origin[1]) ** 2
            )
            if dist_entrance < min_dist:
                min_dist = dist_entrance
                best_pos = (block.entrance.x, block.entrance.y)
                best_idx = i
                best_is_exit = False

            dist_exit = math.sqrt(
                (block.exit.x - origin[0]) ** 2
                + (block.exit.y - origin[1]) ** 2
            )
            if dist_exit < min_dist:
                min_dist = dist_exit
                best_pos = (block.exit.x, block.exit.y)
                best_idx = i
                best_is_exit = True

        return (best_pos, best_idx, best_is_exit)

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

    def _find_farthest_origin_endpoints(
        self,
        blocks: List[MacroBlock],
        origin: Tuple[float, float] = (0.0, 0.0),
        n_candidates: int = 5,
    ) -> List[Tuple[Tuple[float, float], int, bool, float]]:
        """Find the N block endpoints farthest from the origin.

        This is used to evaluate candidate ending points for optimization,
        ensuring the tour ends at a point far from machine origin when desired.

        Args:
            blocks: List of MacroBlocks to search.
            origin: Reference point for distance calculation (default origin).
            n_candidates: Number of farthest endpoints to return.

        Returns:
            List of tuples sorted by distance descending: [(position, block_index, is_exit, distance), ...].
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

        # Sort by distance descending to get farthest first
        candidates.sort(key=lambda x: x[0], reverse=True)
        return [(pos, idx, is_exit, dist) for dist, (pos, idx, is_exit) in candidates[:n_candidates]]


class GeneticAlgorithmStrategy(OptimizationStrategy):
    """Genetic Algorithm (GA) for TSP optimization.

    This strategy implements a genetic algorithm to find high-quality tours
    through MacroBlocks. The algorithm uses:

    - Chromosome encoding: permutation of block indices with direction bits
    - Fitness function: total travel distance (lower is better)
    - Selection: tournament selection (pick best of 3 random individuals)
    - Crossover: Order crossover (OX) preserving relative order from parents
    - Mutation: swap mutation and inversion mutation
    - Elitism: preserve top 2 solutions unchanged to next generation

    Default parameters:
        Population size: 20
        Generations: 30
        Mutation rate: 0.25
        Tournament size: 4
        Elitism count: 3
    """

    DEFAULT_POPULATION_SIZE: int = 20
    DEFAULT_GENERATIONS: int = 30
    DEFAULT_MUTATION_RATE: float = 0.25
    DEFAULT_TOURNAMENT_SIZE: int = 4
    DEFAULT_ELITISM_COUNT: int = 3

    def __init__(
        self,
        population_size: int = DEFAULT_POPULATION_SIZE,
        generations: int = DEFAULT_GENERATIONS,
        mutation_rate: float = DEFAULT_MUTATION_RATE,
        tournament_size: int = DEFAULT_TOURNAMENT_SIZE,
        elitism_count: int = DEFAULT_ELITISM_COUNT,
    ) -> None:
        """Initialize the Genetic Algorithm strategy.

        Args:
            population_size: Number of individuals in population. Default 50.
            generations: Number of generations to evolve. Default 100.
            mutation_rate: Probability of mutation per offspring. Default 0.15.
            tournament_size: Number of individuals in tournament selection. Default 3.
            elitism_count: Number of top individuals preserved each generation. Default 2.
        """
        super().__init__()
        self._population_size = population_size
        self._generations = generations
        self._mutation_rate = mutation_rate
        self._tournament_size = tournament_size
        self._elitism_count = elitism_count

    DEFAULT_N_CANDIDATES: int = 2

    @property
    def name(self) -> str:
        """Return the strategy name."""
        return "Genetic Algorithm"

    def optimize(
        self,
        blocks: List[MacroBlock],
        initial_position: Optional[Tuple[float, float]] = None,
    ) -> OptimizationResult:
        """Optimize using Genetic Algorithm.

        When no initial_position is specified, this method evaluates multiple
        candidate starting points (the N closest endpoints to origin) and selects
        the one that yields minimum total travel distance.

        Args:
            blocks: List of MacroBlocks to optimize.
            initial_position: Starting position for optimization. If None,
                uses the closest endpoint to origin as starting point.

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

        start_pos: Tuple[float, float]
        candidates: List[Tuple[Tuple[float, float], int, bool, float]]

        if initial_position is not None:
            start_pos = initial_position
            candidates = [(start_pos, -1, False, 0.0)]
        else:
            # Find N closest endpoints to origin and evaluate each
            candidates = self._find_nearest_origin_endpoints(
                blocks, origin=(0.0, 0.0), n_candidates=self.DEFAULT_N_CANDIDATES
            )
            self._logger.debug(f"Evaluating {len(candidates)} starting candidates")

        best_result: Optional[OptimizationResult] = None

        for start_pos, first_block_idx, start_at_exit, _dist in candidates:
            candidate_result = self._optimize_from_start(blocks, start_pos)

            if best_result is None or candidate_result.total_travel_distance < best_result.total_travel_distance:
                best_result = candidate_result
                self._logger.debug(f"New best: distance={candidate_result.total_travel_distance:.3f} from start at {start_pos}")

        return best_result  # type: ignore[return-value]

    def _optimize_from_start(
        self,
        blocks: List[MacroBlock],
        start_pos: Tuple[float, float],
    ) -> OptimizationResult:
        """Optimize starting from a specific position.

        Args:
            blocks: List of MacroBlocks to optimize.
            start_pos: Starting position for optimization.

        Returns:
            OptimizationResult with optimized traversal order.
        """
        if len(blocks) == 1:
            return self._optimize_single_block(blocks, start_pos)

        tour = self._greedy_initial_tour(blocks, start_pos)
        if len(tour) < 4:
            connections = self._build_connections(blocks, tour, start_pos)
            total_distance = sum(c.travel_distance for c in connections if c.source_block_id >= 0)
            return OptimizationResult(
                traverse_order=tuple(tour),
                connections=connections,
                total_travel_distance=total_distance,
                initial_position=start_pos,
            )

        population = self._initialize_population(blocks, start_pos)

        best_chromosome: Optional[List[int]] = None
        best_fitness = float('inf')

        for generation in range(self._generations):
            fitness_scores = [(chrom, self._calculate_fitness(chrom, blocks, start_pos)) for chrom in population]

            fitness_scores.sort(key=lambda x: x[1])

            if fitness_scores[0][1] < best_fitness:
                best_fitness = fitness_scores[0][1]
                best_chromosome = list(fitness_scores[0][0])

            new_population: List[List[int]] = []

            for _ in range(self._elitism_count):
                if fitness_scores:
                    new_population.append(list(fitness_scores.pop(0)[0]))

            while len(new_population) < self._population_size:
                parent1 = self._tournament_selection(population, blocks, start_pos)
                parent2 = self._tournament_selection(population, blocks, start_pos)

                offspring = self._order_crossover(parent1, parent2)

                if offspring not in new_population:
                    mutated_offspring = self._mutate(offspring)
                    new_population.append(mutated_offspring)

            while len(new_population) < self._population_size:
                idx = generation % len(population)
                new_population.append(list(population[idx]))

            population = new_population[:self._population_size]

        if best_chromosome is None and population:
            best_chromosome = min(population, key=lambda c: self._calculate_fitness(c, blocks, start_pos))

        final_tour = self._create_tour_from_chromosome(best_chromosome, blocks, start_pos)

        # Apply 2-opt refinement to improve block ordering
        if len(final_tour) > 3:
            final_tour = self._two_opt_refinement(final_tour, blocks)

        connections = self._build_connections(blocks, final_tour, start_pos)
        total_distance = sum(c.travel_distance for c in connections if c.source_block_id >= 0)

        return OptimizationResult(
            traverse_order=tuple(final_tour),
            connections=connections,
            total_travel_distance=total_distance,
            initial_position=start_pos,
        )

    def _initialize_population(
        self,
        blocks: List[MacroBlock],
        start_pos: Tuple[float, float],
    ) -> List[List[int]]:
        """Create random population of tours with direction assignments.

        Each chromosome is a list of integers where:
        - Positive values represent block indices traversed forward
        - Negative values represent block indices traversed in reverse

        Args:
            blocks: All macro blocks.
            start_pos: Starting position for the tour.

        Returns:
            List of chromosomes (tours with direction encoding).
        """
        import random

        population: List[List[int]] = []

        greedy_tour = self._greedy_initial_tour(blocks, start_pos)
        initial_chromosome = self._tour_to_chromosome(greedy_tour)
        population.append(initial_chromosome)

        for _ in range(self._population_size - 1):
            block_indices = list(range(len(blocks)))
            random.shuffle(block_indices)

            chromosome: List[int] = []
            current_pos = start_pos

            for block_idx in block_indices:
                block = blocks[block_idx]

                dist_to_entrance = math.sqrt(
                    (block.entrance.x - current_pos[0]) ** 2
                    + (block.entrance.y - current_pos[1]) ** 2
                )
                dist_to_exit = math.sqrt(
                    (block.exit.x - current_pos[0]) ** 2
                    + (block.exit.y - current_pos[1]) ** 2
                )

                if dist_to_entrance <= dist_to_exit:
                    chromosome.append(block_idx)
                    current_pos = (block.exit.x, block.exit.y)
                else:
                    chromosome.append(-block_idx - 1)
                    current_pos = (block.entrance.x, block.entrance.y)

            population.append(chromosome)

        return population[:self._population_size]

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

    def _find_farthest_origin_endpoints(
        self,
        blocks: List[MacroBlock],
        origin: Tuple[float, float] = (0.0, 0.0),
        n_candidates: int = 5,
    ) -> List[Tuple[Tuple[float, float], int, bool, float]]:
        """Find the N block endpoints farthest from the origin.

        This is used to evaluate candidate ending points for optimization,
        ensuring the tour ends at a point far from machine origin when desired.

        Args:
            blocks: List of MacroBlocks to search.
            origin: Reference point for distance calculation (default origin).
            n_candidates: Number of farthest endpoints to return.

        Returns:
            List of tuples sorted by distance descending: [(position, block_index, is_exit, distance), ...].
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

        # Sort by distance descending to get farthest first
        candidates.sort(key=lambda x: x[0], reverse=True)
        return [(pos, idx, is_exit, dist) for dist, (pos, idx, is_exit) in candidates[:n_candidates]]

    def _calculate_fitness(
        self,
        chromosome: List[int],
        blocks: List[MacroBlock],
        start_pos: Tuple[float, float],
    ) -> float:
        """Calculate total distance for a tour encoded in chromosome.

        Lower fitness (distance) is better for selection.

        Args:
            chromosome: Encoded tour with direction bits.
            blocks: All macro blocks for coordinate lookups.
            start_pos: Starting position for the tour.

        Returns:
            Total Euclidean distance through the tour including travel from start.
        """
        if not chromosome:
            return 0.0

        total_distance = 0.0
        current_pos: Tuple[float, float] = start_pos

        for i, gene in enumerate(chromosome):
            block_idx, reversed_flag = self._decode_gene(gene)
            block = blocks[block_idx]

            entrance = (block.entrance.x, block.entrance.y)
            exit_coord = (block.exit.x, block.exit.y)

            entry_point = exit_coord if reversed_flag else entrance

            dist = math.sqrt(
                (entry_point[0] - current_pos[0]) ** 2
                + (entry_point[1] - current_pos[1]) ** 2
            )
            total_distance += dist

            current_pos = entrance if reversed_flag else exit_coord

        return total_distance

    def _tournament_selection(
        self,
        population: List[List[int]],
        blocks: List[MacroBlock],
        start_pos: Tuple[float, float],
    ) -> List[int]:
        """Select parent using tournament selection.

        Randomly selects tournament_size individuals and returns the fittest.

        Args:
            population: Current population of chromosomes.
            blocks: All macro blocks for fitness evaluation.
            start_pos: Starting position for fitness calculation.

        Returns:
            Selected chromosome for crossover.
        """
        import random

        if not population:
            return []

        tournament_indices = random.sample(range(len(population)), min(self._tournament_size, len(population)))

        best_idx = tournament_indices[0]
        best_fitness = self._calculate_fitness(population[best_idx], blocks, start_pos)

        for idx in tournament_indices[1:]:
            fitness = self._calculate_fitness(population[idx], blocks, start_pos)
            if fitness < best_fitness:
                best_fitness = fitness
                best_idx = idx

        return list(population[best_idx])

    def _order_crossover(self, parent1: List[int], parent2: List[int]) -> List[int]:
        """Order crossover (OX) between two parents.

        OX preserves relative order of genes from one parent in a segment,
        and fills the rest respecting the relative order from the other parent.

        Args:
            parent1: First parent chromosome.
            parent2: Second parent chromosome.

        Returns:
            Offspring chromosome with unique block indices.
        """
        import random

        n = len(parent1)
        if n < 2:
            return list(parent1)

        start = random.randint(0, n - 1)
        end = random.randint(start + 1, n)

        # Collect all block indices in the segment from parent1
        segment_set: set[int] = set()
        for i in range(start, end):
            gene = parent1[i]
            block_idx, _ = self._decode_gene(gene)
            segment_set.add(block_idx)

        offspring: List[int] = []
        parent2_pos = 0

        for i in range(n):
            if start <= i < end:
                # Copy segment from parent1
                offspring.append(parent1[i])
            else:
                # Find next gene from parent2 not in segment_set
                attempts = 0
                while attempts < n:
                    gene = parent2[parent2_pos]
                    block_idx, _ = self._decode_gene(gene)

                    if block_idx not in segment_set:
                        # Found a valid unused gene - append it and add to segment_set
                        offspring.append(gene)
                        segment_set.add(block_idx)
                        break

                    # Gene's block is already used, advance to next position with wrap-around
                    parent2_pos = (parent2_pos + 1) % n
                    attempts += 1
                else:
                    # All genes checked but none unused - shouldn't happen with valid permutation,
                    # but fall back to current gene to avoid infinite loop
                    offspring.append(gene)

                parent2_pos = (parent2_pos + 1) % n

        return offspring[:n]

    def _mutate(self, chromosome: List[int]) -> List[int]:
        """Apply mutation operators to offspring.

        With probability mutation_rate:
        - Swap mutation: swap two random positions
        - Inversion mutation: reverse a segment

        Args:
            chromosome: Chromosome to potentially mutate.

        Returns:
            Mutated chromosome.
        """
        import random

        if not chromosome or len(chromosome) < 2:
            return list(chromosome)

        mutated = list(chromosome)

        if random.random() < self._mutation_rate:
            mutation_type = random.choice(['swap', 'inversion'])

            if mutation_type == 'swap':
                idx1 = random.randint(0, len(mutated) - 1)
                idx2 = random.randint(0, len(mutated) - 1)
                mutated[idx1], mutated[idx2] = mutated[idx2], mutated[idx1]
            else:
                start = random.randint(0, len(mutated) - 2)
                end = random.randint(start + 1, len(mutated))
                mutated[start:end] = list(reversed(mutated[start:end]))

        return mutated

    def _create_tour_from_chromosome(
        self,
        chromosome: List[int],
        blocks: List[MacroBlock],
        start_pos: Tuple[float, float],
    ) -> List[BlockTraverseState]:
        """Convert chromosome (block order + directions) to BlockTraverseState list.

        Args:
            chromosome: Encoded tour with direction bits.
            blocks: All macro blocks for coordinate lookups.
            start_pos: Starting position for the tour.

        Returns:
            List of BlockTraverseState objects representing the tour.
        """
        if not chromosome:
            return []

        tour: List[BlockTraverseState] = []
        seen_blocks = set()

        for gene in chromosome:
            block_idx, reversed_flag = self._decode_gene(gene)

            if block_idx in seen_blocks:
                continue

            block = blocks[block_idx]

            if reversed_flag:
                state = BlockTraverseState(
                    block_id=block.block_id,
                    reversed=True,
                    entrance=(block.exit.x, block.exit.y),
                    exit=(block.entrance.x, block.entrance.y),
                )
            else:
                state = BlockTraverseState(
                    block_id=block.block_id,
                    reversed=False,
                    entrance=(block.entrance.x, block.entrance.y),
                    exit=(block.exit.x, block.exit.y),
                )

            tour.append(state)
            seen_blocks.add(block_idx)

        return self._optimize_tour_directions(tour, blocks, start_pos)

    def _greedy_initial_tour(
        self,
        blocks: List[MacroBlock],
        start_pos: Tuple[float, float],
    ) -> List[BlockTraverseState]:
        """Build initial tour using nearest neighbor heuristic.

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

    def _optimize_tour_directions(
        self,
        tour: List[BlockTraverseState],
        blocks: List[MacroBlock],
        start_pos: Tuple[float, float],
    ) -> List[BlockTraverseState]:
        """Optimize entry/exit decisions for each block in the tour.

        After determining block order, optimize whether each block should be
        traversed forward or reverse based on actual travel distance.

        Args:
            tour: Tour with potentially sub-optimal directions.
            blocks: All macro blocks.
            start_pos: Starting position for calculating first block cost.

        Returns:
            Optimized traverse order.
        """
        if not tour:
            return []

        optimized: List[BlockTraverseState] = []

        for i, state in enumerate(tour):
            block_idx = state.block_id
            original_block_idx = -1
            for j, b in enumerate(blocks):
                if b.block_id == block_idx:
                    original_block_idx = j
                    break

            if original_block_idx == -1:
                optimized.append(state)
                continue

            block = blocks[original_block_idx]

            prev_exit: Tuple[float, float]
            if i == 0:
                prev_exit = start_pos
            else:
                prev_exit = optimized[-1].exit

            dist_to_entrance = math.sqrt(
                (block.entrance.x - prev_exit[0]) ** 2
                + (block.entrance.y - prev_exit[1]) ** 2
            )
            dist_to_exit = math.sqrt(
                (block.exit.x - prev_exit[0]) ** 2
                + (block.exit.y - prev_exit[1]) ** 2
            )

            if dist_to_entrance <= dist_to_exit:
                optimized.append(BlockTraverseState(
                    block_id=block.block_id,
                    reversed=False,
                    entrance=(block.entrance.x, block.entrance.y),
                    exit=(block.exit.x, block.exit.y),
                ))
            else:
                optimized.append(BlockTraverseState(
                    block_id=block.block_id,
                    reversed=True,
                    entrance=(block.exit.x, block.exit.y),
                    exit=(block.entrance.x, block.entrance.y),
                ))

        return optimized

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
                        tour[i + 1:j + 1] = list(reversed(tour[i + 1:j + 1]))
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
            # After swap: dist(a,c) + dist(b,d)
            new_dist = math.sqrt((c[0] - a[0]) ** 2 + (c[1] - a[1]) ** 2)
            new_tour_end_cost = math.sqrt((d[0] - b[0]) ** 2 + (d[1] - b[1]) ** 2)
        else:
            # Edge case: j is last element, only one edge to consider after swap
            new_dist = math.sqrt((c[0] - a[0]) ** 2 + (c[1] - a[1]) ** 2)

        total_new_dist = new_dist + new_tour_end_cost

        return total_new_dist < current_dist

    def _tour_to_chromosome(self, tour: List[BlockTraverseState]) -> List[int]:
        """Convert BlockTraverseState list to chromosome encoding.

        Args:
            tour: Traverse order to convert.

        Returns:
            Chromosome with direction bits encoded.
        """
        chromosome: List[int] = []

        for state in tour:
            reversed_flag = state.reversed

            if reversed_flag:
                gene_value = -state.block_id - 1
            else:
                gene_value = state.block_id

            chromosome.append(gene_value)

        return chromosome

    def _decode_gene(self, gene: int) -> Tuple[int, bool]:
        """Decode a single gene to block index and reversal flag.

        Args:
            gene: Encoded gene value (positive = forward, negative = reversed).

        Returns:
            Tuple of (block_index, is_reversed).
        """
        if gene < 0:
            return (-gene - 1, True)
        else:
            return (gene, False)

    def _is_reversed_gene(self, gene: int) -> bool:
        """Check if a gene represents reversed traversal.

        Args:
            gene: Encoded gene value.

        Returns:
            True if gene represents reversed traversal.
        """
        return gene < 0

    def _optimize_single_block(
        self,
        blocks: List[MacroBlock],
        start_pos: Tuple[float, float],
    ) -> OptimizationResult:
        """Optimize case with single block.

        Args:
            blocks: Single-block list.
            start_pos: Starting position for the optimization.

        Returns:
            OptimizationResult for single block.
        """
        block = blocks[0]

        cost_to_entrance = math.sqrt(
            (block.entrance.x - start_pos[0]) ** 2
            + (block.entrance.y - start_pos[1]) ** 2
        )
        cost_to_exit = math.sqrt(
            (block.exit.x - start_pos[0]) ** 2
            + (block.exit.y - start_pos[1]) ** 2
        )

        if cost_to_entrance <= cost_to_exit:
            tour_state = BlockTraverseState(
                block_id=block.block_id,
                reversed=False,
                entrance=(block.entrance.x, block.entrance.y),
                exit=(block.exit.x, block.exit.y),
            )
        else:
            tour_state = BlockTraverseState(
                block_id=block.block_id,
                reversed=True,
                entrance=(block.exit.x, block.exit.y),
                exit=(block.entrance.x, block.entrance.y),
            )

        connections = self._build_connections(blocks, [tour_state], start_pos)
        total_distance = sum(c.travel_distance for c in connections if c.source_block_id >= 0)

        return OptimizationResult(
            traverse_order=(tour_state,),
            connections=connections,
            total_travel_distance=total_distance,
            initial_position=start_pos,
        )

    def _find_nearest_origin_endpoint(
        self,
        blocks: List[MacroBlock],
        origin: Tuple[float, float] = (0.0, 0.0),
    ) -> Tuple[Tuple[float, float], int, bool]:
        """Find the block endpoint nearest to the origin.

        Args:
            blocks: List of MacroBlocks to search.
            origin: Reference point for distance calculation (default origin).

        Returns:
            Tuple of (nearest_position, block_index, is_exit).
            - nearest_position: (x, y) coordinates closest to origin
            - block_index: index of the block containing this endpoint
            - is_exit: True if nearest position is block's exit (needs reversal)
        """
        min_dist = float('inf')
        best_pos: Tuple[float, float] = (0.0, 0.0)
        best_idx = 0
        best_is_exit = False

        for i, block in enumerate(blocks):
            dist_entrance = math.sqrt(
                (block.entrance.x - origin[0]) ** 2
                + (block.entrance.y - origin[1]) ** 2
            )
            if dist_entrance < min_dist:
                min_dist = dist_entrance
                best_pos = (block.entrance.x, block.entrance.y)
                best_idx = i
                best_is_exit = False

            dist_exit = math.sqrt(
                (block.exit.x - origin[0]) ** 2
                + (block.exit.y - origin[1]) ** 2
            )
            if dist_exit < min_dist:
                min_dist = dist_exit
                best_pos = (block.exit.x, block.exit.y)
                best_idx = i
                best_is_exit = True

        return (best_pos, best_idx, best_is_exit)


class OptimizerEngine:

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
        end_point: Optional[Tuple[float, float]] = None,
    ) -> OptimizationResult:
        """Run the active optimization strategy on a list of MacroBlocks.

        Args:
            blocks: List of MacroBlocks to optimize.
            initial_position: Starting position for standard strategies (used as
                start_point for S-T Path ChristofidesStrategy).
            end_point: Fixed ending point for S-T Path strategies. If not provided,
                uses origin (0, 0) for ChristofidesStrategy compatibility.

        Returns:
            An OptimizationResult with optimized traversal order.

        Raises:
            OptimizationError: If optimization fails.
        """
        self._logger.info(
            f"Starting optimization with {self._strategy.name} on {len(blocks)} blocks"
        )

        try:
            # Check if this is ChristofidesStrategy (S-T Path variant)
            if isinstance(self._strategy, ChristofidesStrategy):
                # For S-T Path: use initial_position as start_point
                start_point = initial_position if initial_position is not None else (0.0, 0.0)
                # Default end_point to origin for backward compatibility when not specified
                final_end_point = end_point if end_point is not None else (0.0, 0.0)
                result = self._strategy.optimize(blocks, start_point, final_end_point)
            else:
                # Standard strategies use initial_position
                result = self._strategy.optimize(blocks, initial_position)

            # Handle ParallelEnsembleOptimizationResult (unwrap for logging)
            if isinstance(result, ParallelEnsembleOptimizationResult):
                inner_result = result.result
                winner_info = f", winner={result.winner_name}"
            else:
                inner_result = result
                winner_info = ""

            self._logger.info(
                f"Optimization complete: total_travel_distance={inner_result.total_travel_distance:.3f}{winner_info}"
            )
            return result
        except Exception as e:
            raise OptimizationError(f"Optimization failed: {e}") from e


@dataclass(frozen=True)
class StrategyBenchmarkResult:
    """Results from benchmarking a single strategy.

    Attributes:
        strategy_name: Name of the strategy that produced this result.
        result: The optimization result from this strategy.
        execution_time_seconds: Time taken to execute this strategy.
        improvement_percent: Percent improvement over baseline (if baseline provided).
    """
    strategy_name: str
    result: OptimizationResult
    execution_time_seconds: float
    improvement_percent: Optional[float] = None


@dataclass(frozen=True)
class ParallelEnsembleOptimizationResult:
    """Results from running the parallel ensemble optimization.

    This wraps the winning OptimizationResult along with metadata about
    which strategy won and benchmark results for all strategies evaluated.

    Attributes:
        result: The winning OptimizationResult.
        winner_name: Name of the strategy that produced the best result.
        all_benchmarks: Tuple of StrategyBenchmarkResult for all strategies run,
            sorted by improvement percent (best first).
    """
    result: OptimizationResult
    winner_name: str
    all_benchmarks: Tuple[StrategyBenchmarkResult, ...]

    @property
    def block_count(self) -> int:
        """Return number of blocks in the winning route."""
        return self.result.block_count


def _run_strategy_worker(
    strategy_name: str,
    blocks_serialized: Tuple[Tuple[int, Tuple[float, float], Tuple[float, float]], ...],
    initial_position: Optional[Tuple[float, float]],
) -> StrategyBenchmarkResult:
    """Worker function to run a single strategy in a subprocess.

    This is a module-level function to allow pickling for ProcessPoolExecutor.

    Args:
        strategy_name: Name of the strategy class to instantiate and run.
        blocks_serialized: Serializable representation of MacroBlocks.
        initial_position: Starting position for optimization.

    Returns:
        StrategyBenchmarkResult with timing and result data.
    """
    # Import here to avoid issues with multiprocessing
    from plt_optimizer.core.chunker import MacroBlock
    from plt_optimizer.core.models import Coordinate, StrokePath, StrokeSegment

    # Reconstruct blocks from serialized form
    blocks: List[MacroBlock] = []
    for block_data in blocks_serialized:
        block_id, entrance, exit = block_data
        # We need to reconstruct with actual paths - use a minimal representation
        # The strategy only needs entrance/exit coordinates
        seg = StrokeSegment(
            start=Coordinate(x=entrance[0], y=entrance[1]),
            end=Coordinate(x=exit[0], y=exit[1]),
            is_cutting=True,
        )
        path = StrokePath(pen_up_position=None, segments=(seg,))
        block = MacroBlock(
            block_id=block_id,
            paths=(path,),
            entrance=seg.start,
            exit=seg.end,
        )
        blocks.append(block)

    # Import strategies
    from plt_optimizer.core.optimizer import (
        GeneticAlgorithmStrategy,
        InsertionHeuristicStrategy,
        NearestNeighbor2OptStrategy,
        NoOpStrategy,
        SimulatedAnnealingStrategy,
        ChristofidesStrategy,
    )

    strategy_map: Dict[str, OptimizationStrategy] = {
        "NoOp (Baseline)": NoOpStrategy(),
        "NearestNeighbor + 2-Opt": NearestNeighbor2OptStrategy(),
        "Insertion Heuristic": InsertionHeuristicStrategy(),
        "Simulated Annealing": SimulatedAnnealingStrategy(),
        "Genetic Algorithm": GeneticAlgorithmStrategy(),
        # ChristofidesStrategy requires start/end points and is S-T Path specific
    }

    if strategy_name not in strategy_map:
        raise ValueError(f"Unknown strategy: {strategy_name}")

    strategy = strategy_map[strategy_name]
    start_time = time.perf_counter()
    result = strategy.optimize(blocks, initial_position)
    execution_time = time.perf_counter() - start_time

    return StrategyBenchmarkResult(
        strategy_name=strategy_name,
        result=result,
        execution_time_seconds=execution_time,
    )


class ParallelEnsembleStrategy(OptimizationStrategy):
    """Parallel ensemble that runs all optimization strategies concurrently.

    This strategy uses a dynamic queue pattern via ProcessPoolExecutor to run
    all available optimization strategies in parallel. Results are collected
    as they complete (fast strategies return first), and the best result is
    selected based on improvement score.

    The selection metric is:
    - If baseline_distance provided: maximize improvement percent
    - Otherwise: minimize absolute travel distance

    Note: ChristofidesStrategy is excluded because it requires fixed start/end
    points and operates as an S-T Path variant rather than a standard tour.
    """

    def __init__(
        self,
        baseline_distance: Optional[float] = None,
        max_workers: Optional[int] = None,
    ) -> None:
        """Initialize the parallel ensemble strategy.

        Args:
            baseline_distance: Original travel distance for computing improvement %.
                If None, selection is based on absolute travel distance.
            max_workers: Maximum number of parallel workers. Defaults to number
                of available strategies (typically 5-6).
        """
        super().__init__()
        self._baseline_distance = baseline_distance
        self._max_workers = max_workers

    @property
    def name(self) -> str:
        """Return the strategy name."""
        return "Parallel Ensemble"

    def optimize(
        self,
        blocks: List[MacroBlock],
        initial_position: Optional[Tuple[float, float]] = None,
    ) -> ParallelEnsembleOptimizationResult:
        """Run all strategies in parallel and select the best result.

        Args:
            blocks: List of MacroBlocks to optimize.
            initial_position: Starting position for optimization.

        Returns:
            A ParallelEnsembleOptimizationResult containing the winning
            OptimizationResult, the name of the winning strategy, and
            benchmark results for all strategies evaluated.
        """
        if len(blocks) == 0:
            return ParallelEnsembleOptimizationResult(
                result=OptimizationResult(
                    traverse_order=(),
                    connections=(),
                    total_travel_distance=0.0,
                    initial_position=initial_position,
                ),
                winner_name="NoOp (Baseline)",
                all_benchmarks=(),
            )

        self._logger.info(
            f"Running {self.name} on {len(blocks)} blocks with "
            f"{self._max_workers or 'auto'} workers"
        )

        # Serialize blocks for multiprocessing (MacroBlock isn't directly picklable)
        blocks_serialized: Tuple[Tuple[int, Tuple[float, float], Tuple[float, float]], ...] = tuple(
            (
                block.block_id,
                (block.entrance.x, block.entrance.y),
                (block.exit.x, block.exit.y),
            )
            for block in blocks
        )

        strategy_names = [
            "NoOp (Baseline)",
            "NearestNeighbor + 2-Opt",
            "Insertion Heuristic",
            "Simulated Annealing",
            "Genetic Algorithm",
        ]

        all_benchmarks: List[StrategyBenchmarkResult] = []
        best_result: Optional[StrategyBenchmarkResult] = None
        completed_count = 0

        with ProcessPoolExecutor(max_workers=self._max_workers) as executor:
            futures = {
                executor.submit(
                    _run_strategy_worker,
                    name,
                    blocks_serialized,
                    initial_position,
                ): name
                for name in strategy_names
            }

            # Collect results dynamically as they complete (fast strategies first)
            failed_strategies: List[Tuple[str, str]] = []  # Track (name, error_msg) for logging
            for future in as_completed(futures):
                strategy_name = futures[future]
                try:
                    benchmark_result = future.result()
                    completed_count += 1

                    self._logger.debug(
                        f"Strategy {strategy_name} completed in "
                        f"{benchmark_result.execution_time_seconds:.3f}s with "
                        f"distance={benchmark_result.result.total_travel_distance:.3f}"
                    )

                    # Calculate improvement percent if baseline provided
                    if self._baseline_distance is not None and self._baseline_distance > 0:
                        pct_improvement = (
                            (self._baseline_distance - benchmark_result.result.total_travel_distance)
                            / self._baseline_distance
                            * 100
                        )
                        # Create new result with improvement percent attached
                        benchmark_result = StrategyBenchmarkResult(
                            strategy_name=benchmark_result.strategy_name,
                            result=benchmark_result.result,
                            execution_time_seconds=benchmark_result.execution_time_seconds,
                            improvement_percent=pct_improvement,
                        )

                    all_benchmarks.append(benchmark_result)

                    # Select best result based on metric
                    if best_result is None:
                        best_result = benchmark_result
                    elif self._baseline_distance is not None and best_result.improvement_percent is not None:
                        # Prefer higher improvement percent
                        if (benchmark_result.improvement_percent or 0) > best_result.improvement_percent:
                            best_result = benchmark_result
                    else:
                        # Fall back to absolute distance minimization
                        if benchmark_result.result.total_travel_distance < best_result.result.total_travel_distance:
                            best_result = benchmark_result

                except Exception as e:
                    failed_strategies.append((strategy_name, str(e)))
                    self._logger.warning(
                        f"Strategy {strategy_name} failed: {e}"
                    )

        if best_result is None:
            # All strategies failed - fall back to NoOp
            self._logger.warning("All parallel strategies failed, using NoOp fallback")
            noop = NoOpStrategy()
            noop_result = noop.optimize(blocks, initial_position)
            return ParallelEnsembleOptimizationResult(
                result=noop_result,
                winner_name="NoOp (Baseline)",
                all_benchmarks=(),
            )

        # Sort benchmarks by improvement percent (descending), then by distance (ascending)
        sorted_benchmarks = sorted(
            all_benchmarks,
            key=lambda b: (
                -(b.improvement_percent if b.improvement_percent is not None else 0.0),
                b.result.total_travel_distance,
            ),
        )

        failed_count = len(failed_strategies)
        if failed_count > 0:
            failed_names = ", ".join(name for name, _ in failed_strategies)
            self._logger.warning(
                f"Parallel ensemble: {failed_count}/{len(strategy_names)} strategies "
                f"failed: {failed_names}"
            )

        imp_str = (
            f", improvement={best_result.improvement_percent:.2f}%"
            if best_result.improvement_percent is not None
            else ""
        )
        self._logger.info(
            f"Parallel ensemble complete: {completed_count}/{len(strategy_names)} "
            f"strategies succeeded. Best: {best_result.strategy_name} "
            f"(distance={best_result.result.total_travel_distance:.3f}{imp_str})"
        )

        return ParallelEnsembleOptimizationResult(
            result=best_result.result,
            winner_name=best_result.strategy_name,
            all_benchmarks=tuple(sorted_benchmarks),
        )