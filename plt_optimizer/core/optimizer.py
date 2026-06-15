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
    """Christofides-Serdyukov algorithm for TSP with 3/2 approximation guarantee.

    This strategy implements the Christofides-Serdyukov algorithm, a deterministic
    approximation algorithm for the Traveling Salesman Problem. The algorithm:

    1. Build Minimum Spanning Tree (MST) of all block endpoints using Prim's algorithm
    2. Find vertices with odd degree in MST
    3. Compute minimum-weight perfect matching on odd-degree vertices
    4. Combine MST + matching edges to form Eulerian multigraph
    5. Find Eulerian tour, then shortcut to Hamiltonian tour

    The algorithm provides a theoretical guarantee that the resulting tour length
    is at most 3/2 of the optimal TSP tour.

    Each block has two endpoints (entrance and exit). For MST purposes, we treat
    each endpoint as a vertex but track which block they belong to. When building
    the final tour, we need to decide both sequence AND direction for each block.
    """

    def __init__(self) -> None:
        """Initialize the Christofides-Serdyukov strategy."""
        super().__init__()

    @property
    def name(self) -> str:
        """Return the strategy name."""
        return "Christofides-Serdyukov Algorithm"

    def optimize(
        self,
        blocks: List[MacroBlock],
        initial_position: Optional[Tuple[float, float]] = None,
    ) -> OptimizationResult:
        """Optimize using Christofides-Serdyukov algorithm.

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
            return self._optimize_single_block(blocks, initial_position)

        if len(blocks) == 2:
            return self._optimize_two_blocks(blocks, initial_position)

        vertices = self._create_vertices(blocks)
        start_vertex = self._find_nearest_origin_vertex(vertices, initial_position or (0.0, 0.0))

        mst_edges = self._build_mst_prim(vertices, start_vertex)

        odd_vertices = self._find_odd_degree_vertices(mst_edges, vertices)

        matching_edges = self._greedy_perfect_matching(odd_vertices, vertices)

        eulerian_edges = list(mst_edges) + matching_edges

        eulerian_tour = self._build_eulerian_tour(eulerian_edges, start_vertex, vertices)

        hamiltonian_sequence = self._euler_to_hamiltonian_shortcut(eulerian_tour, blocks)

        if initial_position is not None:
            start_pos = initial_position
        else:
            v = vertices[start_vertex]
            start_pos = (v[0], v[1])  # v is (x, y, block_index, is_exit)

        tour = self._create_traverse_order(hamiltonian_sequence, blocks, start_pos)

        connections = self._build_connections(blocks, tour, start_pos)
        total_distance = sum(c.travel_distance for c in connections if c.source_block_id >= 0)

        return OptimizationResult(
            traverse_order=tuple(tour),
            connections=connections,
            total_travel_distance=total_distance,
            initial_position=start_pos,
        )

    def _optimize_single_block(
        self,
        blocks: List[MacroBlock],
        initial_position: Optional[Tuple[float, float]],
    ) -> OptimizationResult:
        """Optimize case with single block.

        Args:
            blocks: Single-block list.
            initial_position: Starting position.

        Returns:
            OptimizationResult for single block.
        """
        block = blocks[0]
        origin = initial_position or (0.0, 0.0)

        cost_to_entrance = math.sqrt(
            (block.entrance.x - origin[0]) ** 2
            + (block.entrance.y - origin[1]) ** 2
        )
        cost_to_exit = math.sqrt(
            (block.exit.x - origin[0]) ** 2
            + (block.exit.y - origin[1]) ** 2
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

        start_pos = origin
        connections = self._build_connections(blocks, [tour_state], start_pos)
        total_distance = sum(c.travel_distance for c in connections if c.source_block_id >= 0)

        return OptimizationResult(
            traverse_order=(tour_state,),
            connections=connections,
            total_travel_distance=total_distance,
            initial_position=start_pos,
        )

    def _optimize_two_blocks(
        self,
        blocks: List[MacroBlock],
        initial_position: Optional[Tuple[float, float]],
    ) -> OptimizationResult:
        """Optimize case with two blocks.

        Args:
            blocks: Two-block list.
            initial_position: Starting position.

        Returns:
            OptimizationResult for two blocks.
        """
        origin = initial_position or (0.0, 0.0)

        candidates: List[Tuple[float, int, bool]] = []

        for i, block in enumerate(blocks):
            cost_entrance = math.sqrt(
                (block.entrance.x - origin[0]) ** 2
                + (block.entrance.y - origin[1]) ** 2
            )
            candidates.append((cost_entrance, i, False))

            cost_exit = math.sqrt(
                (block.exit.x - origin[0]) ** 2
                + (block.exit.y - origin[1]) ** 2
            )
            candidates.append((cost_exit, i, True))

        candidates.sort(key=lambda x: x[0])
        first_pos, first_idx, first_rev = candidates[0]

        tour: List[BlockTraverseState] = []
        remaining_block_idx = 1 - first_idx

        if first_rev:
            tour.append(BlockTraverseState(
                block_id=blocks[first_idx].block_id,
                reversed=True,
                entrance=(blocks[first_idx].exit.x, blocks[first_idx].exit.y),
                exit=(blocks[first_idx].entrance.x, blocks[first_idx].entrance.y),
            ))
        else:
            tour.append(BlockTraverseState(
                block_id=blocks[first_idx].block_id,
                reversed=False,
                entrance=(blocks[first_idx].entrance.x, blocks[first_idx].entrance.y),
                exit=(blocks[first_idx].exit.x, blocks[first_idx].exit.y),
            ))

        second_block = blocks[remaining_block_idx]
        cost_to_entrance = math.sqrt(
            (second_block.entrance.x - tour[0].exit[0]) ** 2
            + (second_block.entrance.y - tour[0].exit[1]) ** 2
        )
        cost_to_exit = math.sqrt(
            (second_block.exit.x - tour[0].exit[0]) ** 2
            + (second_block.exit.y - tour[0].exit[1]) ** 2
        )

        if cost_to_entrance <= cost_to_exit:
            tour.append(BlockTraverseState(
                block_id=second_block.block_id,
                reversed=False,
                entrance=(second_block.entrance.x, second_block.entrance.y),
                exit=(second_block.exit.x, second_block.exit.y),
            ))
        else:
            tour.append(BlockTraverseState(
                block_id=second_block.block_id,
                reversed=True,
                entrance=(second_block.exit.x, second_block.exit.y),
                exit=(second_block.entrance.x, second_block.entrance.y),
            ))

        connections = self._build_connections(blocks, tour, origin)
        total_distance = sum(c.travel_distance for c in connections if c.source_block_id >= 0)

        return OptimizationResult(
            traverse_order=tuple(tour),
            connections=connections,
            total_travel_distance=total_distance,
            initial_position=origin,
        )

    def _create_vertices(
        self,
        blocks: List[MacroBlock],
    ) -> Dict[int, Tuple[float, float, int, bool]]:
        """Create vertex mapping from block endpoints.

        Args:
            blocks: All macro blocks.

        Returns:
            Dictionary mapping vertex_id to (x, y, block_index, is_exit).
        """
        vertices: Dict[int, Tuple[float, float, int, bool]] = {}
        vid = 0

        for i, block in enumerate(blocks):
            vertices[vid] = (block.entrance.x, block.entrance.y, i, False)
            vid += 1
            vertices[vid] = (block.exit.x, block.exit.y, i, True)
            vid += 1

        return vertices

    def _find_nearest_origin_vertex(
        self,
        vertices: Dict[int, Tuple[float, float, int, bool]],
        origin: Tuple[float, float],
    ) -> int:
        """Find vertex nearest to the origin point.

        Args:
            vertices: All vertices.
            origin: Reference point for distance calculation.

        Returns:
            Vertex ID of closest vertex to origin.
        """
        min_dist = float('inf')
        best_vid = -1

        for vid, (x, y, _, _) in vertices.items():
            dist = math.sqrt((x - origin[0]) ** 2 + (y - origin[1]) ** 2)
            if dist < min_dist:
                min_dist = dist
                best_vid = vid

        return best_vid

    def _build_mst_prim(
        self,
        vertices: Dict[int, Tuple[float, float, int, bool]],
        start_vertex: int,
    ) -> List[Tuple[int, int]]:
        """Build Minimum Spanning Tree using Prim's algorithm.

        Args:
            vertices: All vertices in the graph.
            start_vertex: Vertex ID to start from (nearest origin).

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

    def _find_odd_degree_vertices(
        self,
        mst_edges: List[Tuple[int, int]],
        vertices: Dict[int, Tuple[float, float, int, bool]],
    ) -> List[int]:
        """Find vertices with odd degree in the MST.

        Args:
            mst_edges: Edges of the MST.
            vertices: All vertices.

        Returns:
            List of vertex IDs with odd degree.
        """
        degree: Dict[int, int] = {vid: 0 for vid in vertices}

        for u, v in mst_edges:
            degree[u] += 1
            degree[v] += 1

        return [vid for vid, deg in degree.items() if deg % 2 == 1]

    def _greedy_perfect_matching(
        self,
        odd_vertices: List[int],
        vertices: Dict[int, Tuple[float, float, int, bool]],
    ) -> List[Tuple[int, int]]:
        """Compute minimum-weight perfect matching on odd-degree vertices.

        Uses a simplified greedy approach: iteratively pair the closest
        unmatched odd vertices. This is not optimal but provides a valid
        perfect matching with reasonable quality.

        Args:
            odd_vertices: List of vertex IDs with odd degree.
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

    def _build_eulerian_tour(
        self,
        edges: List[Tuple[int, int]],
        start_vertex: int,
        vertices: Dict[int, Tuple[float, float, int, bool]],
    ) -> List[int]:
        """Build Eulerian tour from edges using Hierholzer's algorithm.

        Args:
            edges: Combined MST + matching edges.
            start_vertex: Vertex ID to start the tour from.
            vertices: All vertices for adjacency lookup.

        Returns:
            List of vertex IDs in Eulerian tour order.
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
        tour: List[int] = []

        while stack:
            current = stack[-1]

            if adjacency[current]:
                next_v = adjacency[current].pop()
                adjacency[next_v].remove(current)

                edge_key = (current, next_v)
                edge_count[edge_key] -= 1

                stack.append(next_v)
            else:
                tour.append(stack.pop())

        return tour

    def _euler_to_hamiltonian_shortcut(
        self,
        eulerian_tour: List[int],
        blocks: List[MacroBlock],
    ) -> List[Tuple[int, bool]]:
        """Convert Eulerian tour to Hamiltonian by skipping visited nodes.

        When traversing the Eulerian tour, we skip vertices that belong to
        a block already visited. For each block, we also determine whether
        to traverse it forward or in reverse based on entry direction.

        Args:
            eulerian_tour: List of vertex IDs in Eulerian order.
            blocks: All macro blocks for state lookups.

        Returns:
            List of (block_idx, reversed) tuples representing the Hamiltonian tour.
        """
        visited_blocks = set()
        hamiltonian: List[Tuple[int, bool]] = []
        prev_vertex: Optional[int] = None

        num_vertices = len(eulerian_tour)

        for i in range(num_vertices):
            vid = eulerian_tour[i]

            x, y, block_idx, is_exit = self._get_vertex_info(vid, blocks)

            if block_idx in visited_blocks:
                continue

            next_vid = None
            for j in range(1, num_vertices):
                check_idx = (i + j) % num_vertices
                check_vid = eulerian_tour[check_idx]
                _, check_block_idx, _, _ = self._get_vertex_info(check_vid, blocks)
                if check_block_idx not in visited_blocks:
                    next_vid = check_vid
                    break

            should_reverse = False

            if prev_vertex is None:
                should_reverse = is_exit
            else:
                _, _, prev_block_idx, prev_is_exit = self._get_vertex_info(prev_vertex, blocks)

                entry_at_prev: Tuple[float, float]
                if prev_block_idx == block_idx:
                    if prev_is_exit:
                        entry_at_prev = (
                            blocks[prev_block_idx].exit.x,
                            blocks[prev_block_idx].exit.y,
                        )
                    else:
                        entry_at_prev = (
                            blocks[prev_block_idx].entrance.x,
                            blocks[prev_block_idx].entrance.y,
                        )
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

    def _get_vertex_info(
        self,
        vid: int,
        blocks: List[MacroBlock],
    ) -> Tuple[float, float, int, bool]:
        """Get vertex information from a vertex ID.

        Args:
            vid: Vertex ID.
            blocks: All macro blocks to find the corresponding endpoint.

        Returns:
            Tuple of (x, y, block_index, is_exit).
        """
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
        x, y, _, _ = self._get_vertex_info(vid, blocks)
        return (x, y)

    def _create_traverse_order(
        self,
        hamiltonian_sequence: List[Tuple[int, bool]],
        blocks: List[MacroBlock],
        start_pos: Tuple[float, float],
    ) -> List[BlockTraverseState]:
        """Create BlockTraverseState list from Hamiltonian sequence.

        Args:
            hamiltonian_sequence: List of (block_idx, reversed) tuples.
            blocks: All macro blocks for coordinate lookups.
            start_pos: Starting position for the tour.

        Returns:
            List of BlockTraverseState objects.
        """
        if not hamiltonian_sequence:
            return []

        first_block_idx, first_reversed = hamiltonian_sequence[0]
        first_block = blocks[first_block_idx]

        current_pos = start_pos

        entry_at_first: Tuple[float, float]
        if first_reversed:
            entry_at_first = (first_block.exit.x, first_block.exit.y)
        else:
            entry_at_first = (first_block.entrance.x, first_block.entrance.y)

        dist_to_entrance = math.sqrt(
            (first_block.entrance.x - current_pos[0]) ** 2
            + (first_block.entrance.y - current_pos[1]) ** 2
        )
        dist_to_exit = math.sqrt(
            (first_block.exit.x - current_pos[0]) ** 2
            + (first_block.exit.y - current_pos[1]) ** 2
        )

        if first_reversed:
            actual_first_reversed = dist_to_exit < dist_to_entrance
        else:
            actual_first_reversed = dist_to_entrance <= dist_to_exit

        tour: List[BlockTraverseState] = []

        for block_idx, sequence_reversed in hamiltonian_sequence:
            block = blocks[block_idx]

            if actual_first_reversed and block_idx == first_block_idx:
                actual_reversed = True
                state = BlockTraverseState(
                    block_id=block.block_id,
                    reversed=True,
                    entrance=(block.exit.x, block.exit.y),
                    exit=(block.entrance.x, block.entrance.y),
                )
            elif not actual_first_reversed and block_idx == first_block_idx:
                actual_reversed = False
                state = BlockTraverseState(
                    block_id=block.block_id,
                    reversed=False,
                    entrance=(block.entrance.x, block.entrance.y),
                    exit=(block.exit.x, block.exit.y),
                )
            else:
                prev_state = tour[-1] if tour else None

                entry_point: Tuple[float, float]
                if prev_state is not None:
                    entry_point = prev_state.exit
                else:
                    entry_point = start_pos

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

    DEFAULT_INITIAL_TEMPERATURE: float = 10000.0
    DEFAULT_COOLING_RATE: float = 0.9995
    DEFAULT_ITERATIONS_PER_TEMP: int = 50
    DEFAULT_MIN_TEMPERATURE: float = 1e-8

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
        if initial_position is not None:
            start_pos = initial_position
        else:
            closest = self._find_nearest_origin_endpoint(blocks)
            start_pos = closest[0]

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
        Population size: 50
        Generations: 100
        Mutation rate: 0.15
        Tournament size: 3
        Elitism count: 2
    """

    DEFAULT_POPULATION_SIZE: int = 50
    DEFAULT_GENERATIONS: int = 100
    DEFAULT_MUTATION_RATE: float = 0.15
    DEFAULT_TOURNAMENT_SIZE: int = 3
    DEFAULT_ELITISM_COUNT: int = 2

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
        if initial_position is not None:
            start_pos = initial_position
        else:
            closest = self._find_nearest_origin_endpoint(blocks)
            start_pos = closest[0]

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
            fitness_scores = [(chrom, self._calculate_fitness(chrom, blocks)) for chrom in population]

            fitness_scores.sort(key=lambda x: x[1])

            if fitness_scores[0][1] < best_fitness:
                best_fitness = fitness_scores[0][1]
                best_chromosome = list(fitness_scores[0][0])

            new_population: List[List[int]] = []

            for _ in range(self._elitism_count):
                if fitness_scores:
                    new_population.append(list(fitness_scores.pop(0)[0]))

            while len(new_population) < self._population_size:
                parent1 = self._tournament_selection(population, blocks)
                parent2 = self._tournament_selection(population, blocks)

                offspring = self._order_crossover(parent1, parent2)

                if offspring not in new_population:
                    mutated_offspring = self._mutate(offspring)
                    new_population.append(mutated_offspring)

            while len(new_population) < self._population_size:
                idx = generation % len(population)
                new_population.append(list(population[idx]))

            population = new_population[:self._population_size]

        if best_chromosome is None and population:
            best_chromosome = min(population, key=lambda c: self._calculate_fitness(c, blocks))

        final_tour = self._create_tour_from_chromosome(best_chromosome, blocks)  # type: ignore[arg-type]

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

    def _calculate_fitness(self, chromosome: List[int], blocks: List[MacroBlock]) -> float:
        """Calculate total distance for a tour encoded in chromosome.

        Lower fitness (distance) is better for selection.

        Args:
            chromosome: Encoded tour with direction bits.
            blocks: All macro blocks for coordinate lookups.

        Returns:
            Total Euclidean distance through the tour.
        """
        if not chromosome:
            return 0.0

        total_distance = 0.0
        current_pos: Optional[Tuple[float, float]] = None

        for i, gene in enumerate(chromosome):
            block_idx, reversed_flag = self._decode_gene(gene)
            block = blocks[block_idx]

            entrance = (block.entrance.x, block.entrance.y)
            exit_coord = (block.exit.x, block.exit.y)

            if current_pos is not None:
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
    ) -> List[int]:
        """Select parent using tournament selection.

        Randomly selects tournament_size individuals and returns the fittest.

        Args:
            population: Current population of chromosomes.
            blocks: All macro blocks for fitness evaluation.

        Returns:
            Selected chromosome for crossover.
        """
        import random

        if not population:
            return []

        tournament_indices = random.sample(range(len(population)), min(self._tournament_size, len(population)))

        best_idx = tournament_indices[0]
        best_fitness = self._calculate_fitness(population[best_idx], blocks)

        for idx in tournament_indices[1:]:
            fitness = self._calculate_fitness(population[idx], blocks)
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
            Offspring chromosome.
        """
        import random

        n = len(parent1)
        if n < 2:
            return list(parent1)

        size = abs(parent2[0]) if self._is_reversed_gene(parent2[0]) else parent2[0]
        if size < 0:
            size = -size - 1

        start = random.randint(0, n - 1)
        end = random.randint(start + 1, n)

        segment_set = set()
        for i in range(start, end):
            gene = parent1[i]
            block_idx, _ = self._decode_gene(gene)
            segment_set.add(block_idx)

        offspring: List[int] = []
        parent2_pos = 0

        for i in range(n):
            gene = parent2[parent2_pos]
            block_idx, reversed_flag = self._decode_gene(gene)

            if i < start or i >= end:
                while block_idx in segment_set and parent2_pos < n - 1:
                    parent2_pos += 1
                    next_gene = parent2[parent2_pos]
                    block_idx, _ = self._decode_gene(next_gene)
                    reversed_flag = self._is_reversed_gene(parent2[parent2_pos])

                if i < start or i >= end:
                    offspring.append(gene if not reversed_flag else -block_idx - 1)
                else:
                    offspring.append(-block_idx - 1 if reversed_flag else block_idx)

                segment_set.add(block_idx)
            else:
                offspring.append(parent1[i])
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
    ) -> List[BlockTraverseState]:
        """Convert chromosome (block order + directions) to BlockTraverseState list.

        Args:
            chromosome: Encoded tour with direction bits.
            blocks: All macro blocks for coordinate lookups.

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

        return self._optimize_tour_directions(tour, blocks)

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
    ) -> List[BlockTraverseState]:
        """Optimize entry/exit decisions for each block in the tour.

        After determining block order, optimize whether each block should be
        traversed forward or reverse based on actual travel distance.

        Args:
            tour: Tour with potentially sub-optimal directions.
            blocks: All macro blocks.

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
                prev_exit = state.entrance
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

    def _tour_to_chromosome(self, tour: List[BlockTraverseState]) -> List[int]:
        """Convert BlockTraverseState list to chromosome encoding.

        Args:
            tour: Traverse order to convert.

        Returns:
            Chromosome with direction bits encoded.
        """
        import random

        original_order = [state.block_id for state in tour]

        block_positions: Dict[int, int] = {}
        for i, state in enumerate(tour):
            block_positions[state.block_id] = i

        sorted_blocks = sorted(block_positions.keys(), key=lambda bid: block_positions[bid])

        chromosome: List[int] = []
        current_pos: Optional[Tuple[float, float]] = None

        for state in tour:
            reversed_flag = state.reversed
            gene_value = 0

            if reversed_flag:
                gene_value = -state.block_id - 1
            else:
                gene_value = state.block_id

            chromosome.append(gene_value)

        random.shuffle(chromosome)
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