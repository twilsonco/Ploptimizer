"""Symmetric interval-splitting deduplication for overlapping linear strokes.

This module replaces the historical O(N²) ``remove_redundant_strokes`` with a
deterministic, near-linear simplification pass built on 1D interval algebra.
Axis-aligned cutting segments that share a supporting line (``y = const`` for
horizontal, ``x = const`` for vertical) are treated as intervals along that
line; every endpoint of every interval becomes a *break point* that slices all
intervals on the same line into **atomic sub-segments**. Duplicate atomic
sub-segments (geometrically identical, regardless of which label/path
produced them) are then dropped first-come-first-served.

The result, unlike pure subtraction:

1. The shared overlapping region of two partially overlapping edges is kept
   exactly once.
2. The non-overlapping tails survive as independent, shorter segments
   (staggered / brick-work layouts split cleanly at every junction).
3. No output segment extends past a corner or intersection, which gives the
   TSP routing stage maximal reordering freedom without duplicate cuts.

Segments that cannot participate safely pass through untouched:

* Paths containing any :class:`~plt_optimizer.core.models.ArcSegment` (drill
  hole macros) are preserved intact, mirroring
  :func:`~plt_optimizer.utils.geometry.fracture_linear_paths`.
* Non-cutting (rapid) segments, zero-length segments, and diagonal cutting
  segments are kept in place verbatim.

Python 3.8 / Windows 7 compatible: no syntax or stdlib newer than 3.8 is
used at runtime (``typing`` generics only, no ``math.lcm`` etc.).
"""

from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

from plt_optimizer.core.models import (
    ArcSegment,
    Coordinate,
    PLTDocument,
    Segment,
    StrokePath,
    StrokeSegment,
)

# Decimal places used to canonicalize interval break points and deduplication
# keys. ``Coordinate.__post_init__`` already rounds every coordinate to 3
# decimals, so rounding to 5 is lossless for any parsed coordinate and merely
# guards against float noise in synthetically produced documents.
_BREAK_POINT_PRECISION: int = 5

# Orientation tags for the supporting-line group key.
_HORIZONTAL: str = "H"
_VERTICAL: str = "V"


class _Candidate(NamedTuple):
    """An axis-aligned cutting segment queued for interval splitting.

    Attributes:
        orient: ``"H"`` or ``"V"`` supporting-line orientation.
        fixed: Fixed-axis coordinate of the supporting line (y for H, x for V).
        var_start: Lower variable-axis endpoint of the normalized interval.
        var_end: Higher variable-axis endpoint of the normalized interval.
        forward: True when the original segment ran low -> high along the
            variable axis (used to re-orient won sub-segments).
    """

    orient: str
    fixed: float
    var_start: float
    var_end: float
    forward: bool


def split_and_deduplicate_intervals(
    intervals: Sequence[Tuple[float, float, int]],
    tol: float = 1e-5,
) -> List[Tuple[float, float, int]]:
    """Split 1D intervals on one line into atomic, disjoint, non-duplicated pieces.

    Every endpoint of every interval becomes a break point. Each interval is
    sliced at all break points falling strictly inside it, so any overlap
    between two intervals becomes an *exact* key match that can be dropped.
    The first interval (in input order) to claim a geometric sub-interval
    keeps it; later identical claims are discarded.

    Intervals must be normalized (``start <= end``); callers are responsible
    for ordering endpoints. The third tuple element is an opaque payload
    (typically the index of the originating candidate) carried through to the
    atomic pieces untouched.

    Args:
        intervals: Sequence of ``(start, end, payload)`` tuples on the same
            axis with ``start <= end``.
        tol: Atomic sub-intervals shorter than ``tol`` are dropped as
            floating-point slivers (default 1e-5).

    Returns:
        List of ``(start, end, payload)`` atomic sub-intervals in deterministic
        input order: per input interval, left to right, duplicates removed
        first-come-first-served.
    """
    if not intervals:
        return []

    # Step 1: collect every unique endpoint coordinate (break points).
    break_points: Set[float] = set()
    for start, end, _payload in intervals:
        break_points.add(round(start, _BREAK_POINT_PRECISION))
        break_points.add(round(end, _BREAK_POINT_PRECISION))
    sorted_breaks: List[float] = sorted(break_points)

    # Step 2: slice every interval at the break points strictly inside it.
    # The outer endpoints keep their raw values so reconstructed segments
    # reproduce the original extreme coordinates exactly.
    atomic: List[Tuple[float, float, int]] = []
    for start, end, payload in intervals:
        start_key = round(start, _BREAK_POINT_PRECISION)
        end_key = round(end, _BREAK_POINT_PRECISION)
        current = start
        for point in sorted_breaks:
            if start_key < point < end_key:
                atomic.append((current, point, payload))
                current = point
        atomic.append((current, end, payload))

    # Step 3: deduplicate atomic sub-intervals by geometric bounds only, so
    # identical spans claimed by different payloads collapse to one winner.
    seen: Set[Tuple[float, float]] = set()
    unique: List[Tuple[float, float, int]] = []
    for sub_start, sub_end, payload in atomic:
        if sub_end - sub_start < tol:
            continue
        geom_key = (
            round(sub_start, _BREAK_POINT_PRECISION),
            round(sub_end, _BREAK_POINT_PRECISION),
        )
        if geom_key not in seen:
            seen.add(geom_key)
            unique.append((sub_start, sub_end, payload))

    return unique


def _axis_aligned_interval(
    seg: StrokeSegment,
    tol: float,
) -> Optional[Tuple[str, float, float, float]]:
    """Classify a cutting segment as an interval on an axis-aligned line.

    Args:
        seg: The cutting stroke segment to classify.
        tol: Axis-alignment tolerance.

    Returns:
        ``(orient, fixed, var_start_raw, var_end_raw)`` where ``orient`` is
        ``"H"``/``"V"``, ``fixed`` is the supporting-line coordinate, and the
        variable-axis endpoints are in original (un-normalized) order.
        ``None`` for degenerate (point) or diagonal segments.
    """
    dx = seg.end.x - seg.start.x
    dy = seg.end.y - seg.start.y

    if abs(dx) < tol and abs(dy) < tol:
        return None  # Degenerate point: keep in place, nothing to split.
    if abs(dy) < tol:
        return (_HORIZONTAL, seg.start.y, seg.start.x, seg.end.x)
    if abs(dx) < tol:
        return (_VERTICAL, seg.start.x, seg.start.y, seg.end.y)
    return None  # Diagonal: pass through untouched.


def _rebuild_segment(
    cand: _Candidate,
    sub_start: float,
    sub_end: float,
) -> StrokeSegment:
    """Rebuild a cutting segment for a won atomic sub-interval.

    The sub-interval is re-oriented to the original segment's travel
    direction so multi-segment paths (rectangle sides, polygon chains) stay
    chain-contiguous and the writer emits no spurious pen lifts.

    Args:
        cand: The originating candidate carrying orientation and direction.
        sub_start: Lower variable-axis bound of the atomic sub-interval.
        sub_end: Higher variable-axis bound of the atomic sub-interval.

    Returns:
        A new cutting :class:`StrokeSegment` running in the original
        direction.
    """
    if cand.orient == _HORIZONTAL:
        low = Coordinate(x=sub_start, y=cand.fixed)
        high = Coordinate(x=sub_end, y=cand.fixed)
    else:
        low = Coordinate(x=cand.fixed, y=sub_start)
        high = Coordinate(x=cand.fixed, y=sub_end)

    if cand.forward:
        return StrokeSegment(start=low, end=high, is_cutting=True)
    return StrokeSegment(start=high, end=low, is_cutting=True)


def simplify_overlapping_strokes(
    doc: PLTDocument,
    tol: float = 1e-5,
) -> PLTDocument:
    """Split partially overlapping axis-aligned strokes and drop duplicates.

    Symmetric replacement for the historical endpoint-on-segment removal:
    instead of only deleting a stroke whose endpoints lie on a longer one,
    overlapping intervals on the same supporting line are sliced at *each
    other's* endpoints into atomic sub-segments, and exactly one copy of each
    atomic piece is kept. Non-overlapping tails therefore survive as
    independent shorter segments, and shared borders between labels (or
    staggered brick-work layouts) are cut exactly once.

    Behavioural contract (mirrors the removed ``remove_redundant_strokes``):

    * Only *cutting* segments participate; rapid moves pass through untouched.
    * Paths containing arcs (drill-hole macros) are preserved intact.
    * Diagonal cutting segments pass through untouched (axis-aligned only).
    * When removals split a path, it is broken into multiple ``StrokePath``
      objects with ``pen_up_position`` re-anchored to the first surviving
      segment; empty paths are filtered out.
    * Header/footer command lists are copied verbatim.

    Complexity is O(N log N) per supporting line (sort + linear slicing),
    a strict improvement over the previous O(N²) pairwise scan.

    Args:
        doc: The input PLTDocument.
        tol: Tolerance for axis alignment and sliver removal (default 1e-5).

    Returns:
        New PLTDocument with atomic, non-duplicated cutting segments.
    """
    # Collect candidates (axis-aligned cutting segments) grouped by their
    # supporting line, and remember which (path, segment) each belongs to.
    candidates: List[_Candidate] = []
    candidate_of: Dict[Tuple[int, int], int] = {}
    groups: Dict[Tuple[str, float], List[int]] = {}

    for path_idx, path in enumerate(doc.stroke_paths):
        if any(isinstance(seg, ArcSegment) for seg in path.segments):
            continue  # Arc-bearing paths pass through whole (drill macros).
        for seg_idx, seg in enumerate(path.segments):
            if isinstance(seg, ArcSegment) or not seg.is_cutting:
                continue
            interval = _axis_aligned_interval(seg, tol)
            if interval is None:
                continue  # Degenerate or diagonal: keep in place.
            orient, fixed, raw_start, raw_end = interval
            forward = raw_start <= raw_end
            cand_idx = len(candidates)
            candidates.append(
                _Candidate(
                    orient=orient,
                    fixed=fixed,
                    var_start=raw_start if forward else raw_end,
                    var_end=raw_end if forward else raw_start,
                    forward=forward,
                )
            )
            candidate_of[(path_idx, seg_idx)] = cand_idx
            groups.setdefault((orient, round(fixed, _BREAK_POINT_PRECISION)), []).append(cand_idx)

    # Split + deduplicate per supporting line; collect won segments per
    # candidate (empty list == the candidate was fully superseded).
    won: Dict[int, List[Segment]] = {idx: [] for idx in range(len(candidates))}
    for cand_indices in groups.values():
        intervals = [
            (
                candidates[idx].var_start,
                candidates[idx].var_end,
                idx,
            )
            for idx in cand_indices
        ]
        for sub_start, sub_end, cand_idx in split_and_deduplicate_intervals(intervals, tol=tol):
            won[cand_idx].append(_rebuild_segment(candidates[cand_idx], sub_start, sub_end))

    # Re-emit paths: arc paths pass through whole; every other path is
    # rebuilt walking its segments in order, each candidate replaced by its
    # won sub-segments (possibly none -> path split with pen-up semantics).
    new_stroke_paths: List[StrokePath] = []
    for path_idx, path in enumerate(doc.stroke_paths):
        if any(isinstance(seg, ArcSegment) for seg in path.segments):
            new_stroke_paths.append(path)
            continue

        current_segments: List[Segment] = []
        current_pen_up = path.pen_up_position

        for seg_idx, seg in enumerate(path.segments):
            won_index = candidate_of.get((path_idx, seg_idx))
            outputs: List[Segment] = [seg]
            if won_index is not None:
                outputs = won[won_index]

            if not outputs:
                if current_segments:
                    new_stroke_paths.append(
                        StrokePath(
                            pen_up_position=current_pen_up,
                            segments=tuple(current_segments),
                        )
                    )
                    current_segments = []
                current_pen_up = None
                continue

            for out_seg in outputs:
                if not current_segments and current_pen_up is None:
                    current_pen_up = out_seg.start
                current_segments.append(out_seg)

        if current_segments:
            new_stroke_paths.append(
                StrokePath(
                    pen_up_position=current_pen_up,
                    segments=tuple(current_segments),
                )
            )

    return PLTDocument(
        header_commands=list(doc.header_commands),
        stroke_paths=new_stroke_paths,
        footer_commands=list(doc.footer_commands),
    )
