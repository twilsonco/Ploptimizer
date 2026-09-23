"""Symmetric interval-splitting deduplication for overlapping linear strokes.

This module replaces redundant stroke removal with a deterministic 1D interval pass.
Axis-aligned cutting segments sharing a supporting line are split at collinear endpoints
and cross-axis T-junctions into atomic sub-segments. Duplicate sub-segments are dropped.

Supporting lines are clustered with a dedicated ``line_tol`` (coarser than the
segment ``tol``) because CAD exports such as EngraveLab emit the same physical
line twice at small perpendicular offsets (observed up to 6 plotter units =
0.006" at 1016 units/inch). Break points closer than ``line_tol`` snap to one
cluster representative so jittered endpoints deduplicate cleanly instead of
leaving sliver strokes behind.
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

_BREAK_POINT_PRECISION: int = 5

_HORIZONTAL: str = "H"
_VERTICAL: str = "V"


class _Candidate(NamedTuple):
    orient: str
    fixed: float
    var_start: float
    var_end: float
    forward: bool


def split_and_deduplicate_intervals(
    intervals: Sequence[Tuple[float, float, int]],
    tol: float = 1e-5,
    extra_breaks: Optional[Set[float]] = None,
    line_tol: Optional[float] = None,
) -> List[Tuple[float, float, int]]:
    """Split overlapping 1D intervals at every break and drop duplicate pieces.

    Args:
        intervals: ``(start, end, payload)`` triples; the payload (candidate
            index) is carried through to the surviving pieces.
        tol: Minimum piece length; shorter pieces are dropped.
        extra_breaks: Caller-injected break points (e.g. cross-axis junctions).
        line_tol: Optional break-snapping tolerance. When set, break points
            within ``line_tol`` of a cluster's lowest member collapse onto
            that representative, so jittered endpoints of merged supporting
            lines align into shared dedup keys instead of leaving sliver
            pieces. An interval whose snapped span falls below ``tol`` keeps
            its unsnapped endpoints (and re-adds them to the break set) so
            genuinely short strokes are never collapsed away. ``None``
            (default) disables snapping.

    Returns:
        Deduplicated atomic pieces in input order, first claimant wins.
    """
    if not intervals:
        return []

    def _round(val: float) -> float:
        return round(val, _BREAK_POINT_PRECISION)

    raw_breaks: Set[float] = set()
    if extra_breaks:
        for eb in extra_breaks:
            raw_breaks.add(_round(eb))
    for start, end, _payload in intervals:
        raw_breaks.add(_round(start))
        raw_breaks.add(_round(end))

    # Cluster breaks onto representatives: a break joins the current cluster
    # only while within line_tol of the cluster's lowest member (bounded
    # diameter, no transitive chaining), otherwise it opens a new cluster.
    snap: Dict[float, float] = {}
    representatives: List[float] = []
    cluster_rep: Optional[float] = None
    for point in sorted(raw_breaks):
        if line_tol is not None and cluster_rep is not None and point - cluster_rep <= line_tol:
            snap[point] = cluster_rep
        else:
            cluster_rep = point
            snap[point] = point
            representatives.append(point)

    # Canonicalise interval endpoints. Snapping must not collapse a real
    # stroke: when the snapped span drops below tol the interval keeps its
    # unsnapped endpoints and re-adds them to the break set, so neighbouring
    # intervals still slice at the true boundary and dedup keys stay aligned.
    break_set: Set[float] = set(representatives)
    canonical: List[Tuple[float, float, int]] = []
    for start, end, payload in intervals:
        start_k = snap[_round(start)]
        end_k = snap[_round(end)]
        if end_k - start_k < tol:
            start_k = _round(start)
            end_k = _round(end)
            break_set.add(start_k)
            break_set.add(end_k)
        canonical.append((start_k, end_k, payload))

    sorted_breaks: List[float] = sorted(break_set)

    atomic: List[Tuple[float, float, int]] = []
    for start_k, end_k, payload in canonical:
        current = start_k
        for point in sorted_breaks:
            # Slice at every break strictly inside the interval. Pieces below
            # tol are dropped below, which keeps piece boundaries (and
            # therefore dedup keys) aligned instead of letting a piece extend
            # past a neighbour's endpoint by up to tol.
            if start_k < point < end_k:
                atomic.append((current, point, payload))
                current = point
        atomic.append((current, end_k, payload))

    seen: Set[Tuple[float, float]] = set()
    unique: List[Tuple[float, float, int]] = []
    for sub_start, sub_end, payload in atomic:
        if sub_end - sub_start < tol:
            continue
        geom_key = (_round(sub_start), _round(sub_end))
        if geom_key not in seen:
            seen.add(geom_key)
            unique.append((sub_start, sub_end, payload))

    return unique


def _axis_aligned_interval(
    seg: StrokeSegment,
    tol: float,
) -> Optional[Tuple[str, float, float, float]]:
    dx = seg.end.x - seg.start.x
    dy = seg.end.y - seg.start.y

    if abs(dx) < tol and abs(dy) < tol:
        return None
    if abs(dy) < tol:
        mid_y = (seg.start.y + seg.end.y) / 2.0
        return (_HORIZONTAL, mid_y, seg.start.x, seg.end.x)
    if abs(dx) < tol:
        mid_x = (seg.start.x + seg.end.x) / 2.0
        return (_VERTICAL, mid_x, seg.start.y, seg.end.y)
    return None


def _rebuild_segment(
    cand: _Candidate,
    sub_start: float,
    sub_end: float,
) -> StrokeSegment:
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
    line_tol: Optional[float] = None,
) -> PLTDocument:
    """Drop duplicate atomic pieces of overlapping axis-aligned cut strokes.

    Args:
        doc: Document to simplify (structural pipeline, post-fracture).
        tol: Axis-alignment and minimum-piece-length tolerance.
        line_tol: Optional supporting-line merge tolerance, coarser than
            ``tol``. Cutting segments whose supporting lines lie within
            ``line_tol`` of each other are treated as the same line (CAD
            export jitter), and their break points snap accordingly.
            ``None`` (default) keeps the historical behaviour of merging
            supporting lines only within ``tol``.

    Returns:
        A new document with duplicate sub-segments culled.
    """
    merge_tol = tol if line_tol is None else line_tol
    candidates: List[_Candidate] = []
    candidate_of: Dict[Tuple[int, int], int] = {}

    for path_idx, path in enumerate(doc.stroke_paths):
        if any(isinstance(seg, ArcSegment) for seg in path.segments):
            continue
        for seg_idx, seg in enumerate(path.segments):
            if isinstance(seg, ArcSegment) or not seg.is_cutting:
                continue
            interval = _axis_aligned_interval(seg, tol)
            if interval is None:
                continue
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

    # Group supporting lines using fuzzy tolerance matching instead of dict
    # keys. line_tol (when set) absorbs CAD export offsets between copies of
    # the same physical line.
    groups: List[Tuple[str, float, List[int]]] = []
    for cand_idx, cand in enumerate(candidates):
        matched_idx = None
        for g_idx, (g_orient, g_fixed, _) in enumerate(groups):
            if g_orient == cand.orient and abs(g_fixed - cand.fixed) <= merge_tol:
                matched_idx = g_idx
                break
        if matched_idx is not None:
            groups[matched_idx][2].append(cand_idx)
        else:
            groups.append((cand.orient, cand.fixed, [cand_idx]))

    horizontal_cands = [c for c in candidates if c.orient == _HORIZONTAL]
    vertical_cands = [c for c in candidates if c.orient == _VERTICAL]

    won: Dict[int, List[Segment]] = {idx: [] for idx in range(len(candidates))}

    for orient, fixed, cand_indices in groups:
        extra_breaks: Set[float] = set()

        group_var_min = min(candidates[i].var_start for i in cand_indices)
        group_var_max = max(candidates[i].var_end for i in cand_indices)

        if orient == _HORIZONTAL:
            for vc in vertical_cands:
                # Require vertical candidate to cross horizontal line AND fall within horizontal span
                if (vc.var_start - merge_tol <= fixed <= vc.var_end + merge_tol) and (
                    group_var_min - merge_tol <= vc.fixed <= group_var_max + merge_tol
                ):
                    extra_breaks.add(vc.fixed)
        else:
            for hc in horizontal_cands:
                # Require horizontal candidate to cross vertical line AND fall within vertical span
                if (hc.var_start - merge_tol <= fixed <= hc.var_end + merge_tol) and (
                    group_var_min - merge_tol <= hc.fixed <= group_var_max + merge_tol
                ):
                    extra_breaks.add(hc.fixed)

        intervals = [
            (candidates[idx].var_start, candidates[idx].var_end, idx) for idx in cand_indices
        ]

        atomic_pieces = split_and_deduplicate_intervals(
            intervals, tol=tol, extra_breaks=extra_breaks, line_tol=line_tol
        )

        for sub_start, sub_end, cand_idx in atomic_pieces:
            seg = _rebuild_segment(candidates[cand_idx], sub_start, sub_end)
            won[cand_idx].append(seg)

        # Reverse sub-segment output order for backward candidates to preserve travel direction
        for cand_idx in cand_indices:
            if not candidates[cand_idx].forward and len(won[cand_idx]) > 1:
                won[cand_idx].reverse()

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
                if not current_segments:
                    # Re-anchor unconditionally: when the first segment of
                    # this path lost its head piece to another stroke, the
                    # surviving piece starts at an interior point and the
                    # stale pen_up_position would make the emitter cut a
                    # phantom stroke from the old pen-up location.
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
