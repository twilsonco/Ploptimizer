"""Post-TSP chunk direction sweep.

Strategies decide each block's traversal direction *while building* the tour,
and refinement passes (2-opt) rewrite the block order afterwards. The result:
a fixed order whose per-block ``reversed`` flags no longer match the geometry
of their neighbours -- 2-opt reverses a tour slice without flipping the flags
of the blocks inside it, so the recorded ``(exit, entrance)`` pairs go stale.

This module closes that gap. Given a finished :class:`OptimizationResult`, it
keeps the block **order** untouched and re-solves only the per-block
**direction**, greedily minimising the inter-chunk rapid travel. It iterates
alternating forward and backward sweeps until the objective stops improving.

The pass is cheap (O(n) per sweep, no geometry beyond the block endpoints) and
runs strictly after the Parallel Ensemble unwrap, in the parent process, where
the real blocks are available.

Reversal itself is not implemented here: the sweep only rewrites
:class:`BlockTraverseState` flags, which :class:`~plt_optimizer.core.
reassembler.Reassembler` already knows how to apply (path order, per-segment
start/end swap, and arc sweep negation).

Python 3.8 / Windows 7 compatible: no matplotlib, no generate-pipeline imports,
and no syntax newer than 3.8 at runtime.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from plt_optimizer.core.chunker import MacroBlock
from plt_optimizer.core.optimizer import (
    BlockTraverseState,
    OptimizationResult,
    build_block_connections,
    sum_travel_distance,
)

# Safety cap on the number of sweeps. The pass converges in a handful of
# iterations on real toolpaths; the bound keeps a pathological degenerate tour
# from looping forever.
DEFAULT_MAX_PASSES: int = 10

# A sweep is accepted only on a *strict* improvement larger than this, so
# floating-point noise on collinear layouts cannot cause an infinite loop.
DEFAULT_TOL: float = 1e-9

_Position = Tuple[float, float]


@dataclass(frozen=True)
class DirectionSweepResult:
    """Outcome of :func:`sweep_tour_directions`.

    Attributes:
        result: The re-optimised result. Identical to the input (same object)
            when the sweep found no improvement.
        travel_before: Inter-chunk travel recomputed from the input tour's
            geometry. Deliberately not ``result.total_travel_distance``, which
            a refinement pass may have left stale.
        travel_after: Inter-chunk travel of ``result``. Always
            ``<= travel_before``.
        passes: Number of sweeps accepted (0 when nothing improved).
        flips: Number of blocks whose direction differs from the input tour.
    """

    result: OptimizationResult
    travel_before: float
    travel_after: float
    passes: int
    flips: int


def _dist(a: _Position, b: _Position) -> float:
    """Euclidean distance between two plain coordinate tuples.

    Args:
        a: First position as ``(x, y)``.
        b: Second position as ``(x, y)``.

    Returns:
        Euclidean distance.
    """
    return math.sqrt((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2)


def _canonical_state(block: MacroBlock, is_reversed: bool) -> BlockTraverseState:
    """Build the traverse state for a block travelling in a given direction.

    Mirrors the contract every strategy honours and the reassembler consumes:
    the state carries the *effective* entry/exit, so a reversed block reports
    its geometric exit as its entrance.

    Args:
        block: The block being traversed.
        is_reversed: ``True`` to traverse the block backwards.

    Returns:
        The canonical :class:`BlockTraverseState`.
    """
    entrance: _Position = (block.entrance.x, block.entrance.y)
    exit_: _Position = (block.exit.x, block.exit.y)

    if is_reversed:
        return BlockTraverseState(
            block_id=block.block_id,
            reversed=True,
            entrance=exit_,
            exit=entrance,
        )
    return BlockTraverseState(
        block_id=block.block_id,
        reversed=False,
        entrance=entrance,
        exit=exit_,
    )


def _objective(
    block_by_id: Dict[int, MacroBlock],
    tour: Sequence[BlockTraverseState],
) -> float:
    """Inter-chunk travel of a tour, using the strategies' own connection math.

    Args:
        block_by_id: Block lookup keyed by ``block_id``.
        tour: Candidate traverse order.

    Returns:
        Sum of inter-block travel distances (the initial jump excluded,
        exactly like every strategy's ``total_travel_distance``).
    """
    blocks = [block_by_id[state.block_id] for state in tour]
    return sum_travel_distance(build_block_connections(blocks, list(tour), None))


def _forward_pass(
    tour: Sequence[BlockTraverseState],
    block_by_id: Dict[int, MacroBlock],
) -> List[BlockTraverseState]:
    """One left-to-right greedy direction sweep.

    Each block's direction is chosen to minimise the gap into it, given the
    already-decided predecessor exit. The head block is decided last: its
    direction has no incoming gap, only the outgoing one.

    Args:
        tour: Tour to re-sweep.
        block_by_id: Block lookup keyed by ``block_id``.

    Returns:
        A new tour with re-chosen directions.
    """
    out = list(tour)
    n = len(out)

    for i in range(1, n):
        block = block_by_id[out[i].block_id]
        prev_exit = out[i - 1].exit
        to_entrance = _dist(prev_exit, (block.entrance.x, block.entrance.y))
        to_exit = _dist(prev_exit, (block.exit.x, block.exit.y))
        out[i] = _canonical_state(block, is_reversed=to_exit < to_entrance)

    if n >= 2:
        head = block_by_id[out[0].block_id]
        next_entrance = out[1].entrance
        # Forward exits at head.exit, reversed exits at head.entrance.
        forward_out = _dist((head.exit.x, head.exit.y), next_entrance)
        reversed_out = _dist((head.entrance.x, head.entrance.y), next_entrance)
        out[0] = _canonical_state(head, is_reversed=reversed_out < forward_out)

    return out


def _backward_pass(
    tour: Sequence[BlockTraverseState],
    block_by_id: Dict[int, MacroBlock],
) -> List[BlockTraverseState]:
    """One right-to-left greedy direction sweep.

    The mirror of :func:`_forward_pass`: each block's direction is chosen to
    minimise the gap out of it, given the already-decided successor entrance.
    The tail block is decided last (it has no outgoing gap).

    Args:
        tour: Tour to re-sweep.
        block_by_id: Block lookup keyed by ``block_id``.

    Returns:
        A new tour with re-chosen directions.
    """
    out = list(tour)
    n = len(out)

    for i in range(n - 2, -1, -1):
        block = block_by_id[out[i].block_id]
        next_entrance = out[i + 1].entrance
        # Forward exits at block.exit, reversed exits at block.entrance.
        forward_out = _dist((block.exit.x, block.exit.y), next_entrance)
        reversed_out = _dist((block.entrance.x, block.entrance.y), next_entrance)
        out[i] = _canonical_state(block, is_reversed=reversed_out < forward_out)

    if n >= 2:
        tail = block_by_id[out[n - 1].block_id]
        prev_exit = out[n - 2].exit
        to_entrance = _dist(prev_exit, (tail.entrance.x, tail.entrance.y))
        to_exit = _dist(prev_exit, (tail.exit.x, tail.exit.y))
        out[n - 1] = _canonical_state(tail, is_reversed=to_exit < to_entrance)

    return out


def sweep_tour_directions(
    blocks: List[MacroBlock],
    result: OptimizationResult,
    *,
    max_passes: int = DEFAULT_MAX_PASSES,
    tol: float = DEFAULT_TOL,
) -> DirectionSweepResult:
    """Re-optimise each block's traversal direction, keeping the order fixed.

    Alternates :func:`_forward_pass` and :func:`_backward_pass`, accepting a
    sweep only when it strictly improves the inter-chunk objective, until a
    full round gains nothing (fixpoint) or ``max_passes`` sweeps have run.
    Because every accepted sweep improves the objective, the returned
    ``travel_after`` is never greater than ``travel_before``.

    The tour order, ``initial_position`` and block set are preserved; only the
    per-block direction flags change, so the reassembled document contains the
    exact same paths.

    Args:
        blocks: The blocks the tour traverses.
        result: Finished optimisation result whose order is kept.
        max_passes: Upper bound on sweeps executed (safety cap).
        tol: Minimum gain required to accept a sweep.

    Returns:
        A :class:`DirectionSweepResult`. When the tour is shorter than two
        blocks, or any ``block_id`` is missing from ``blocks``, the input
        result is returned untouched with ``passes=0``.
    """
    tour = list(result.traverse_order)

    if len(tour) < 2:
        total = result.total_travel_distance
        return DirectionSweepResult(
            result=result,
            travel_before=total,
            travel_after=total,
            passes=0,
            flips=0,
        )

    block_by_id: Dict[int, MacroBlock] = {b.block_id: b for b in blocks}
    if any(state.block_id not in block_by_id for state in tour):
        # Defensive: a tour referencing unknown blocks is not ours to repair.
        return DirectionSweepResult(
            result=result,
            travel_before=result.total_travel_distance,
            travel_after=result.total_travel_distance,
            passes=0,
            flips=0,
        )

    best = tour
    travel_before = _objective(block_by_id, best)
    best_total = travel_before
    passes = 0

    for sweep_index in range(max_passes):
        sweep = _backward_pass if sweep_index % 2 else _forward_pass
        candidate = sweep(best, block_by_id)
        candidate_total = _objective(block_by_id, candidate)

        if candidate_total < best_total - tol:
            best = candidate
            best_total = candidate_total
            passes += 1
        else:
            break

    flips = sum(1 for original, final in zip(tour, best) if original.reversed != final.reversed)

    if passes == 0:
        return DirectionSweepResult(
            result=result,
            travel_before=travel_before,
            travel_after=travel_before,
            passes=0,
            flips=0,
        )

    swept = OptimizationResult(
        traverse_order=tuple(best),
        connections=build_block_connections(blocks, best, result.initial_position),
        total_travel_distance=best_total,
        initial_position=result.initial_position,
    )

    return DirectionSweepResult(
        result=swept,
        travel_before=travel_before,
        travel_after=best_total,
        passes=passes,
        flips=flips,
    )
