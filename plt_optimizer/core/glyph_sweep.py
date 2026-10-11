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
* pick, per glyph, how its strokes are traced -- the historical all-or-nothing
  pair (all strokes forwards in stored order, or all backwards in reversed
  order) *plus* every per-stroke direction assignment, so a glyph can reverse
  just the strokes that want reversing.

With the order fixed and the endpoints pinned, the problem is a shortest path
through a chain whose nodes are a glyph's feasible traversals (a handful after
endpoint deduplication), solved exactly by dynamic programming. The
all-forward traversal is always feasible, so the sweep is monotone by
construction: it is accepted only on a strict improvement.

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
from typing import Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

from plt_optimizer.core.intra_chunk_optimizer import (
    IntraChunkResult,
    PathTraverseState,
)
from plt_optimizer.core.models import Coordinate, StrokePath

# A direction assignment is accepted only on a *strict* improvement larger
# than this, so floating-point noise on collinear layouts cannot flip glyphs
# for nothing (and cannot loop -- the DP runs once).
DEFAULT_TOL: float = 1e-9

# Largest glyph (in strokes) whose per-stroke direction assignments are
# enumerated exhaustively. The shipped font library peaks at 8 strokes
# (``Jhanuni`` ``#``) with 99% of glyphs at 3 or fewer, so this covers every
# real glyph; above it the sweep falls back to the historical all-or-nothing
# pair, which is a strict subset of the enumerated set (never worse).
MAX_EXACT_STROKE_MODES: int = 10

# Tolerance for "this mode starts/ends exactly where the chronological
# emission does", i.e. the endpoint-pinning test. Plotter coordinates are
# integer device units (~1e-3 in), so a tenth of a plotter unit is far below
# any real movement and far above float noise on the distances involved.
_PIN_TOLERANCE: float = 1e-4


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
        flips: Glyph groups re-traced away from the chronological
            (all-forward) traversal -- a whole-glyph reversal, one or more
            reversed strokes, or both.
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


class _GroupMode(NamedTuple):
    """One feasible traversal of a glyph group.

    Attributes:
        entry: Coordinate the traversal starts at.
        exit: Coordinate the traversal ends at.
        intra: Rapid travel *between* the group's own strokes.
        sequence: ``(path_index, is_reversed)`` pairs in emission order.
        all_forward: ``True`` for the chronological traversal (stored stroke
            order, every stroke traced forwards) -- the mode the sweep does
            not count as a change.
    """

    entry: Coordinate
    exit: Coordinate
    intra: float
    sequence: Tuple[Tuple[int, bool], ...]
    all_forward: bool


def _mode_key(mode: _GroupMode) -> Tuple[float, float, float, float]:
    """Endpoint-pair identity of a mode, for deduplication.

    Coordinates are quantised to nanometres so traversals that end at the
    same pair (within float noise) collapse to one DP transition.

    Args:
        mode: The traversal to key.

    Returns:
        ``(entry_x, entry_y, exit_x, exit_y)`` quantised to 1e-9.
    """
    return (
        round(mode.entry.x, 9),
        round(mode.entry.y, 9),
        round(mode.exit.x, 9),
        round(mode.exit.y, 9),
    )


def _traversal(
    chronological: Sequence[int],
    order: Sequence[int],
    directions: Sequence[bool],
    paths: Sequence[StrokePath],
) -> _GroupMode:
    """Measure one traversal of a group's strokes in a given order/direction.

    Args:
        chronological: The group's path indices in chronological order.
        order: Path indices in emission order.
        directions: Per-stroke ``is_reversed`` flags, aligned with ``order``.
        paths: The block's paths.

    Returns:
        The measured :class:`_GroupMode`.
    """
    sequence = tuple(zip(order, directions))
    intra = 0.0
    for (prev_index, prev_rev), (next_index, next_rev) in zip(sequence, sequence[1:]):
        intra += _dist(_exit(paths[prev_index], prev_rev), _entry(paths[next_index], next_rev))
    first_index, first_rev = sequence[0]
    last_index, last_rev = sequence[-1]
    return _GroupMode(
        entry=_entry(paths[first_index], first_rev),
        exit=_exit(paths[last_index], last_rev),
        intra=intra,
        sequence=sequence,
        all_forward=list(order) == list(chronological) and not any(directions),
    )


def _group_modes(
    group: Sequence[int],
    paths: Sequence[StrokePath],
) -> List[_GroupMode]:
    """Enumerate a glyph group's feasible traversals, cheapest per endpoint pair.

    The feasible set is the union of the historical all-or-nothing pair
    (stored stroke order traced forwards / reversed stroke order traced
    backwards) and every per-stroke direction assignment on both stroke
    orders. The shipped sweep's two traversals are members of that set, so
    the sweep's optimum stays feasible and the result can only be better.

    Entry and exit depend only on the first/last stroke and its direction, so
    the set collapses hard after deduplication; the enumeration itself is
    ``O(k * 2^k)`` and is capped at :data:`MAX_EXACT_STROKE_MODES` strokes,
    above which the module falls back to the historical two-mode set (a strict
    subset, so the sweep stays monotone).

    Args:
        group: The group's path indices, in chronological order.
        paths: The block's paths.

    Returns:
        The deduplicated modes, cheapest internal travel first.
    """
    indices = tuple(group)
    size = len(indices)
    if size > MAX_EXACT_STROKE_MODES:
        # Pathological glyph (no shipped font reaches this): keep the
        # historical all-or-nothing pair.
        candidates = [
            _traversal(indices, indices, (False,) * size, paths),
            _traversal(indices, tuple(reversed(indices)), (True,) * size, paths),
        ]
    else:
        candidates = []
        for order in (indices, tuple(reversed(indices))):
            for mask in range(1 << size):
                directions = tuple(bool(mask >> bit & 1) for bit in range(size))
                candidates.append(_traversal(indices, order, directions, paths))

    best: Dict[Tuple[float, float, float, float], _GroupMode] = {}
    for mode in candidates:
        key = _mode_key(mode)
        current = best.get(key)
        if current is None or mode.intra < current.intra:
            best[key] = mode
    return sorted(best.values(), key=lambda mode: mode.intra)


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
    """Pick each glyph's stroke tracing directions to minimise intra-chunk travel.

    The glyph order (and therefore the reading order of the engraved text)
    is fixed, as are the chunk's entrance and exit: the group owning the
    block's first path can only be entered at the chronological entry and the
    group owning the last path can only be left at the chronological exit, so
    the inter-chunk tour and the post-TSP direction sweep stay exactly valid.
    Within those constraints each glyph is traversed in one of the modes
    enumerated by :func:`_group_modes` -- the historical all-or-nothing pair
    plus every per-stroke direction assignment -- and the optimal mode per
    glyph is found exactly by dynamic programming over the group chain. Ties
    resolve to the chronological (all-forward) mode.

    Args:
        paths: The chunk's paths, in chronological (emission) order.
        glyph_groups: Path indices per glyph, in text order. Groups are
            expected disjoint and contiguous; anything not covered by a group
            keeps its chronological slot and forward direction.
        tol: Minimum strict improvement required to accept a re-tracing.

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

    # Every feasible traversal per group, filtered so the chunk's entrance and
    # exit stay exactly where the chronological emission puts them (the
    # inter-chunk tour and the direction sweep are pinned to them).
    pinned_entry = _entry(paths[live[0]], False)
    pinned_exit = _exit(paths[live[-1]], False)
    modes_by_group: List[List[_GroupMode]] = []
    for group in valid_groups:
        modes = _group_modes(group, paths)
        owns_first = group[0] == live[0]
        owns_last = group[-1] == live[-1]
        kept = [
            mode
            for mode in modes
            if (not owns_first or _dist(mode.entry, pinned_entry) <= _PIN_TOLERANCE)
            and (not owns_last or _dist(mode.exit, pinned_exit) <= _PIN_TOLERANCE)
        ]
        if not kept:  # pragma: no cover - the all-forward mode always qualifies
            return _no_op(len(valid_groups))
        modes_by_group.append(kept)

    # Fixed forward chains of ungrouped paths, between the groups.
    chains: List[Tuple[float, Optional[Coordinate], Optional[Coordinate]]] = []
    for left, right in zip(valid_groups, valid_groups[1:]):
        between = [index for index in ungrouped if left[-1] < index < right[0]]
        chains.append(_forward_chain(between, paths))

    baseline = _intra_travel(_chronological_order(live), paths)

    # Chain DP: dp[i] = best intra travel up to and including the current
    # group, traversed in mode i. parent[i] records the previous mode index.
    dp: List[float] = [mode.intra for mode in modes_by_group[0]]
    parents: List[List[Optional[int]]] = [[None] * len(modes) for modes in modes_by_group]

    for k in range(1, len(modes_by_group)):
        chain_travel, chain_entry, chain_exit = chains[k - 1]
        previous = modes_by_group[k - 1]
        next_dp: List[float] = []
        for mode_index, mode in enumerate(modes_by_group[k]):
            best = math.inf
            best_prev: Optional[int] = None
            for prev_index, prev_mode in enumerate(previous):
                prev_cost = dp[prev_index]
                if prev_cost == math.inf:  # pragma: no cover - modes are finite
                    continue
                if chain_entry is not None and chain_exit is not None:
                    gap = (
                        _dist(prev_mode.exit, chain_entry)
                        + chain_travel
                        + _dist(chain_exit, mode.entry)
                    )
                else:
                    gap = _dist(prev_mode.exit, mode.entry)
                cost = prev_cost + gap + mode.intra
                if cost < best:
                    best = cost
                    best_prev = prev_index
            next_dp.append(best)
            parents[k][mode_index] = best_prev
        dp = next_dp

    best_index = min(range(len(dp)), key=lambda index: dp[index])
    best_travel = dp[best_index]
    if not (best_travel < baseline - tol):
        return _no_op(len(valid_groups))

    # Backtrack the winning mode per group (walk the chain backwards, then
    # restore group order).
    reversed_chosen: List[_GroupMode] = []
    cursor: Optional[int] = best_index
    for k in range(len(modes_by_group) - 1, -1, -1):
        assert cursor is not None  # noqa: S101 - the chain is fully connected
        reversed_chosen.append(modes_by_group[k][cursor])
        cursor = parents[k][cursor]
    chosen = list(reversed(reversed_chosen))

    sequence: List[Tuple[int, bool]] = []
    for k, group in enumerate(valid_groups):
        sequence.extend(chosen[k].sequence)
        if k + 1 < len(valid_groups):
            # Ungrouped paths sitting between this group and the next keep
            # their chronological slot, traced forward.
            for index_value in ungrouped:
                if group[-1] < index_value < valid_groups[k + 1][0]:
                    sequence.append((index_value, False))

    flips = sum(1 for mode in chosen if not mode.all_forward)
    return GlyphSweepResult(
        result=_build_result(sequence, paths, best_travel),
        travel_before=baseline,
        travel_after=best_travel,
        groups=len(valid_groups),
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
