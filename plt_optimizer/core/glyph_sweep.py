"""Intra-chunk glyph direction sweep for generated text.

The inter-chunk optimizer treats a text chunk (a whole line, or one word
under ``text_chunk_mode: word``) as a single TSP node, and the post-TSP
direction sweep re-picks the direction of that *whole* node. Neither can
touch the rapid travel *inside* a chunk: whole-chunk reversal leaves the
intra-chunk gaps exactly invariant (reversing a path sequence permutes the
same consecutive endpoint pairs), and on real generated text that intra-chunk
share is the majority of the emitted rapid travel.

This module closes that gap from the inside. The generate pipeline knows
exactly which strokes belong to which character (the renderers build each
glyph's strokes contiguously and carry the partition out on
``TextChunkRecord.glyph_groups``), so for one chunk it can:

* keep the chronological glyph order (reading order is preserved),
* keep the chunk's entrance and exit fixed (so the inter-chunk tour and the
  direction sweep stay exactly valid, and the emitted rapid travel can never
  increase), and
* pick, per glyph, forward or reversed tracing -- all of a glyph's strokes
  flip together (stroke order reverses and every segment is traced backwards,
  arcs included).

With the order fixed and the endpoints pinned, the problem is a shortest path
through a 2-state chain (glyph *k* traversed forward / reversed), solved
exactly by dynamic programming in O(glyphs) time. The all-forward traversal is
always feasible, so the sweep is monotone by construction: it is accepted only
on a strict improvement.

Reversal itself is not implemented here: the sweep emits an
:class:`~plt_optimizer.core.intra_chunk_optimizer.IntraChunkResult` of
per-path :class:`~plt_optimizer.core.intra_chunk_optimizer.PathTraverseState`
flags, which :class:`~plt_optimizer.core.reassembler.Reassembler` already
knows how to apply (per-segment start/end swap, arc sweep negation and
``pen_up`` re-anchoring).

Python 3.8 / Windows 7 compatible: no matplotlib, no generate-pipeline
imports, and no syntax newer than 3.8 at runtime.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

from plt_optimizer.core.intra_chunk_optimizer import (
    IntraChunkResult,
    PathTraverseState,
)
from plt_optimizer.core.models import Coordinate, StrokePath

# A direction assignment is accepted only on a *strict* improvement larger
# than this, so floating-point noise on collinear layouts cannot flip glyphs
# for nothing (and cannot loop -- the DP runs once).
DEFAULT_TOL: float = 1e-9

# The two traversal directions of one glyph group.
_FORWARD: int = 0
_REVERSED: int = 1


@dataclass(frozen=True)
class GlyphSweepResult:
    """Outcome of :func:`sweep_glyph_directions`.

    Attributes:
        result: Per-path traversal states in emission order. Equal to the
            all-forward (chronological) traversal when the sweep found no
            improvement, so callers key off :attr:`flips` before feeding it
            to the reassembler.
        travel_before: Intra-chunk rapid travel of the all-forward traversal,
            recomputed from geometry.
        travel_after: Intra-chunk rapid travel of ``result``. Always
            ``<= travel_before``.
        groups: Number of glyph groups considered.
        flips: Glyph groups assigned a reversed traversal.
    """

    result: IntraChunkResult
    travel_before: float
    travel_after: float
    groups: int
    flips: int


def _dist(a: Coordinate, b: Coordinate) -> float:
    """Euclidean distance between two coordinates.

    Args:
        a: First coordinate.
        b: Second coordinate.

    Returns:
        Euclidean distance.
    """
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2)


def _entry(path: StrokePath, is_reversed: bool) -> Coordinate:
    """Return the coordinate a path is entered at in a given direction.

    Args:
        path: The path being traversed.
        is_reversed: ``True`` when the path is traced backwards.

    Returns:
        The first pen-down point of the traversal.
    """
    if is_reversed:
        return path.segments[-1].end
    return path.segments[0].start


def _exit(path: StrokePath, is_reversed: bool) -> Coordinate:
    """Return the coordinate a path is left from in a given direction.

    Args:
        path: The path being traversed.
        is_reversed: ``True`` when the path is traced backwards.

    Returns:
        The last pen-down point of the traversal.
    """
    if is_reversed:
        return path.segments[0].start
    return path.segments[-1].end


def _intra_travel(sequence: Sequence[Tuple[int, bool]], paths: Sequence[StrokePath]) -> float:
    """Sum the rapid travel between consecutive entries of a traversal.

    Args:
        sequence: ``(path_index, is_reversed)`` pairs in emission order.
        paths: The block's paths.

    Returns:
        Total pen-up travel between consecutive paths of the traversal
        (0.0 for a traversal of fewer than two paths).
    """
    total = 0.0
    for (prev_index, prev_rev), (next_index, next_rev) in zip(sequence, sequence[1:]):
        total += _dist(_exit(paths[prev_index], prev_rev), _entry(paths[next_index], next_rev))
    return total


def _group_endpoints(
    group: Sequence[int],
    paths: Sequence[StrokePath],
) -> Tuple[Coordinate, Coordinate, float, float]:
    """Measure one glyph group's endpoints and internal travel both ways.

    A glyph's strokes are traced in stored order forward, or in reversed
    order backwards. The internal (intra-glyph) rapid travel of the two
    traversals is measured explicitly rather than assumed equal, so the
    reversal invariance is verified on the data.

    Args:
        group: The group's path indices, in chronological order.
        paths: The block's paths.

    Returns:
        ``(forward_entry, forward_exit, intra_forward, intra_reversed)``.
    """
    forward_entry = _entry(paths[group[0]], False)
    forward_exit = _exit(paths[group[-1]], False)

    intra_forward = 0.0
    intra_reversed = 0.0
    for left, right in zip(group, group[1:]):
        # Forward: ...left's end -> right's start...
        intra_forward += _dist(_exit(paths[left], False), _entry(paths[right], False))
        # Reversed emission visits `right` before `left`, both backwards:
        # ...right's start -> left's end...
        intra_reversed += _dist(_entry(paths[right], False), _exit(paths[left], False))
    return forward_entry, forward_exit, intra_forward, intra_reversed


def _forward_chain(
    indices: Sequence[int],
    paths: Sequence[StrokePath],
) -> Tuple[float, Optional[Coordinate], Optional[Coordinate]]:
    """Measure a fixed forward chain of ungrouped paths.

    Args:
        indices: Path indices in chronological order (may be empty).
        paths: The block's paths.

    Returns:
        ``(internal_travel, entry, exit)`` with ``entry``/``exit`` ``None``
        for an empty chain.
    """
    if not indices:
        return 0.0, None, None
    sequence = [(index, False) for index in indices]
    return (
        _intra_travel(sequence, paths),
        _entry(paths[indices[0]], False),
        _exit(paths[indices[-1]], False),
    )


def _chronological_order(live: Sequence[int]) -> List[Tuple[int, bool]]:
    """Build the all-forward chronological traversal of a chunk.

    This is the emission order the generator produces today (glyphs in text
    order, each glyph's strokes in stored order), and the traversal the sweep
    is measured against.

    Args:
        live: Indices of the chunk's traced (segment-bearing) paths.

    Returns:
        ``(path_index, False)`` pairs in chronological order.
    """
    return [(index, False) for index in live]


def sweep_glyph_directions(
    paths: Sequence[StrokePath],
    glyph_groups: Sequence[Sequence[int]],
    *,
    tol: float = DEFAULT_TOL,
) -> GlyphSweepResult:
    """Pick each glyph's tracing direction to minimise intra-chunk travel.

    The glyph order (and therefore the reading order of the engraved text)
    is fixed, as are the chunk's entrance and exit: the group owning the
    block's first path can only be entered forward and the group owning the
    last path can only be exited forward, so the inter-chunk tour and the
    post-TSP direction sweep stay exactly valid. Within those constraints the
    optimal per-glyph direction set is found exactly by dynamic programming
    over the 2-state chain; ties resolve to forward.

    Args:
        paths: The chunk's paths, in chronological (emission) order.
        glyph_groups: Path indices per glyph, in text order. Groups are
            expected disjoint and contiguous; anything not covered by a group
            keeps its chronological slot and forward direction.
        tol: Minimum strict improvement required to accept a reversal.

    Returns:
        A :class:`GlyphSweepResult`. ``flips == 0`` (with an all-forward
        ``result``) whenever there is nothing to sweep: fewer than two
        groups, no groups, groups that do not include the chunk's first/last
        traced path, or no achievable improvement.
    """
    path_count = len(paths)
    # Segment-less paths are never emitted, so the sweep (and its endpoint
    # pinning) is defined over the traced paths only.
    live = [index for index, path in enumerate(paths) if path.segments]
    valid_groups: List[Tuple[int, ...]] = []
    for group in glyph_groups:
        cleaned = tuple(
            index for index in group if 0 <= index < path_count and paths[index].segments
        )
        if cleaned:
            valid_groups.append(cleaned)

    def _no_op(groups_seen: int) -> GlyphSweepResult:
        """Return the identity (all-forward, chronological) sweep result."""
        sequence = _chronological_order(live)
        travel = _intra_travel(sequence, paths)
        return GlyphSweepResult(
            result=_build_result(sequence, paths, travel),
            travel_before=travel,
            travel_after=travel,
            groups=groups_seen,
            flips=0,
        )

    if len(valid_groups) < 2:
        return _no_op(len(valid_groups))

    ungrouped = _ungrouped_indices(valid_groups, live)
    # The sweep must not move the chunk's entrance/exit (the inter-chunk tour
    # and the direction sweep are pinned to them), so the groups have to own
    # the first and last traced path of the chunk.
    if valid_groups[0][0] != live[0] or valid_groups[-1][-1] != live[-1]:
        return _no_op(len(valid_groups))

    endpoints = [_group_endpoints(group, paths) for group in valid_groups]
    # Fixed forward chains of ungrouped paths, between the groups.
    chains: List[Tuple[float, Optional[Coordinate], Optional[Coordinate]]] = []
    for left, right in zip(valid_groups, valid_groups[1:]):
        between = [index for index in ungrouped if left[-1] < index < right[0]]
        chains.append(_forward_chain(between, paths))

    baseline = _intra_travel(_chronological_order(live), paths)

    group_count = len(valid_groups)
    # dp[dir] = best intra travel up to and including the current group,
    # the group traversed in `dir`. parent[dir] records the previous dir.
    dp: List[float] = [math.inf, math.inf]
    parent: List[List[Optional[int]]] = [[None, None] for _ in range(group_count)]
    # The first group owns path 0, so it is entered forward (the block's
    # entrance is pinned); only its exit side is free.
    _first_entry, _first_exit, intra_fwd_first, _intra_rev_first = endpoints[0]
    dp[_FORWARD] = intra_fwd_first
    dp[_REVERSED] = math.inf

    for k in range(1, group_count):
        entry, exit_, intra_fwd, intra_rev = endpoints[k]
        chain_travel, chain_entry, chain_exit = chains[k - 1]
        pinned_last = valid_groups[k][-1] == live[-1]
        next_dp: List[float] = [math.inf, math.inf]
        for direction in (_FORWARD, _REVERSED):
            if direction == _REVERSED and pinned_last:
                continue
            use_entry = entry if direction == _FORWARD else exit_
            intra = intra_fwd if direction == _FORWARD else intra_rev
            best = math.inf
            best_prev: Optional[int] = None
            for prev_direction in (_FORWARD, _REVERSED):
                if dp[prev_direction] == math.inf:
                    continue
                prev_exit = (
                    endpoints[k - 1][1] if prev_direction == _FORWARD else endpoints[k - 1][0]
                )
                if chain_exit is not None and chain_entry is not None:
                    gap = (
                        _dist(prev_exit, chain_entry) + chain_travel + _dist(chain_exit, use_entry)
                    )
                else:
                    gap = _dist(prev_exit, use_entry)
                cost = dp[prev_direction] + gap + intra
                if cost < best:
                    best = cost
                    best_prev = prev_direction
            next_dp[direction] = best
            parent[k][direction] = best_prev
        dp = next_dp

    best_direction = _FORWARD if dp[_FORWARD] <= dp[_REVERSED] else _REVERSED
    best_travel = dp[best_direction]
    if not (best_travel < baseline - tol):
        return _no_op(group_count)

    # Backtrack the winning direction per group.
    chosen: List[int] = [best_direction]
    direction = best_direction
    for k in range(group_count - 1, 0, -1):
        direction = parent[k][direction]  # type: ignore[assignment]
        chosen.append(direction)
    chosen.reverse()

    sequence: List[Tuple[int, bool]] = []
    for k, group in enumerate(valid_groups):
        reversed_group = chosen[k] == _REVERSED
        for index in reversed(group) if reversed_group else group:
            sequence.append((index, reversed_group))
        if k + 1 < group_count:
            # Ungrouped paths sitting between this group and the next keep
            # their chronological slot, traced forward.
            for index in ungrouped:
                if group[-1] < index < valid_groups[k + 1][0]:
                    sequence.append((index, False))

    flips = sum(1 for direction in chosen if direction == _REVERSED)
    return GlyphSweepResult(
        result=_build_result(sequence, paths, best_travel),
        travel_before=baseline,
        travel_after=best_travel,
        groups=group_count,
        flips=flips,
    )


def _ungrouped_indices(
    groups: Sequence[Sequence[int]],
    live: Sequence[int],
) -> List[int]:
    """Return the traced indices covered by no group, ascending.

    Args:
        groups: Glyph groups (path indices).
        live: Indices of the chunk's traced (segment-bearing) paths.

    Returns:
        Uncovered traced path indices, ascending.
    """
    covered: Set[int] = set()
    for group in groups:
        covered.update(group)
    return [index for index in live if index not in covered]


def _build_result(
    sequence: Sequence[Tuple[int, bool]],
    paths: Sequence[StrokePath],
    total_internal_distance: float,
) -> IntraChunkResult:
    """Materialise the traversal sequence as an :class:`IntraChunkResult`.

    Args:
        sequence: ``(path_index, is_reversed)`` pairs in emission order.
        paths: The chunk's paths.
        total_internal_distance: Intra-chunk rapid travel of the traversal.

    Returns:
        The result consumed by :class:`~plt_optimizer.core.reassembler.
        Reassembler`.
    """
    states: List[PathTraverseState] = []
    for index, is_reversed in sequence:
        path = paths[index]
        if not path.segments:  # pragma: no cover - defensive; live-filtered
            continue
        states.append(
            PathTraverseState(
                path_index=index,
                reversed=is_reversed,
                entrance=_entry(path, is_reversed),
                exit=_exit(path, is_reversed),
            )
        )
    return IntraChunkResult(
        traverse_order=tuple(states),
        total_internal_distance=total_internal_distance,
    )


__all__ = ["DEFAULT_TOL", "GlyphSweepResult", "sweep_glyph_directions"]

# Re-exported for typing convenience of the pipeline layer.
GroupMap = Dict[int, Tuple[Tuple[int, ...], ...]]
