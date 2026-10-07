"""Coincident-stroke merging: drop intermediate tool-up moves.

After optimization the reassembled path list frequently contains tip-to-tail
junctions: one stroke's final point sits (within tolerance) exactly at the
next stroke's rapid target *and* its first cut start. The tool is already
physically there, so the pen-up/pen-down pair between the two strokes is pure
command overhead -- the plotter lifts, "travels" zero distance, and plunges
again.

This module collapses such junctions at the document level: consecutive
:class:`~plt_optimizer.core.models.StrokePath` objects whose endpoints are
coincident are stitched into a single continuous path (transitively, so a run
of N tip-to-tail paths becomes one path). The transform is metric-neutral by
construction -- a merged junction contributes ~0 to
:meth:`~plt_optimizer.core.models.PLTDocument.rapid_distance` -- and its win
is PU (tool-up) count, path count, and file size.

The merge predicate mirrors the writer's long-standing PU suppression in
:meth:`~plt_optimizer.core.writer.PLTWriter._format_stroke_path` (same
tolerance, same two-sided test), which makes that suppression a real,
inspectable document transform: it becomes visible to metrics, path counts,
and the generate path's per-cutter emitter
(:func:`~plt_optimizer.generate.plate_optimizer.emit_layer_document`), which
emits every path PU-led and therefore keeps every tool-up today.

Safety properties:

* **Geometry-preserving.** No segment is added, removed, or modified; the
  undirected segment multiset and the cutting distance are invariant.
* **Rapid-preserving.** A path is absorbed only when the rapid into it is
  (near-)zero, so every genuine pen-up move -- including arc-native glyph
  plunges whose ``pen_up_position`` differs from the previous stroke's end --
  survives verbatim.
* **Order-preserving.** Paths are only stitched in place; the traversal order
  produced by the optimizer is never touched.

Python 3.8 / Windows 7 compatible: no matplotlib, no generate-pipeline
imports, and no syntax newer than 3.8 at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence, Tuple

from plt_optimizer.core.models import StrokePath
from plt_optimizer.utils.geometry import COORD_TOLERANCE, coordinates_equal

# Junctions closer than this count as coincident. Matches COORD_TOLERANCE --
# the same absolute tolerance the writer uses to suppress redundant PU
# commands and the precision Coordinate rounds to (3 decimals).
DEFAULT_MERGE_TOLERANCE: float = COORD_TOLERANCE


@dataclass(frozen=True)
class MergeResult:
    """Outcome of :func:`merge_coincident_paths`.

    Attributes:
        paths: The merged, order-preserving path list. Equal to the input when
            nothing merged.
        paths_before: Number of segment-bearing input paths considered.
        paths_after: Number of segment-bearing paths in :attr:`paths`.
        merges: Number of junctions collapsed (``paths_before - paths_after``).
    """

    paths: Tuple[StrokePath, ...]
    paths_before: int
    paths_after: int
    merges: int


def is_coincident_junction(
    tail: StrokePath,
    head: StrokePath,
    tolerance: float = DEFAULT_MERGE_TOLERANCE,
) -> bool:
    """Check whether ``head`` starts exactly where ``tail`` ends.

    Both sides of the writer's PU-suppression test must hold: the previous
    stroke's end must coincide with ``head``'s rapid target (the point the PU
    would move to -- its ``pen_up_position`` when set, else its first segment
    start) *and* with ``head``'s first segment start (the point the pen
    plunges at). When both match, the PU between them is a zero-distance move
    and the two strokes form one continuous cut.

    Args:
        tail: The earlier path (must have at least one segment).
        head: The candidate path to absorb (must have at least one segment).
        tolerance: Absolute coordinate tolerance in plotter units.

    Returns:
        True when the junction is coincident and safe to merge.
    """
    if not tail.segments or not head.segments:
        return False

    tail_end = tail.segments[-1].end
    if not coordinates_equal(tail_end, head.segments[0].start, tolerance):
        return False
    if head.pen_up_position is not None and not coordinates_equal(
        tail_end, head.pen_up_position, tolerance
    ):
        return False
    return True


def merge_coincident_paths(
    paths: Sequence[StrokePath],
    *,
    tolerance: float = DEFAULT_MERGE_TOLERANCE,
) -> MergeResult:
    """Stitch tip-to-tail stroke paths into continuous cuts.

    Consecutive paths are merged when the earlier path's last point is within
    ``tolerance`` of both the later path's rapid target and its first segment
    start (see :func:`is_coincident_junction`). Merging is transitive: a run of
    N tip-to-tail paths collapses to a single path carrying the run's first
    ``pen_up_position`` and the concatenated segments. Segment-less paths are
    passed through verbatim and act as merge barriers (they carry no endpoint
    to test against, and keeping them in place is the conservative choice).

    Args:
        paths: Ordered stroke paths, typically the flat list produced by
            :meth:`~plt_optimizer.core.reassembler.Reassembler.reassemble`.
        tolerance: Absolute coordinate tolerance for the coincidence test
            (default :data:`DEFAULT_MERGE_TOLERANCE`, 1e-3 plotter units --
            the writer's PU-suppression tolerance).

    Returns:
        A :class:`MergeResult` whose :attr:`~MergeResult.merges` is 0 when no
        junction qualifies.
    """
    merged: List[StrokePath] = []
    paths_before = 0
    merges = 0

    for path in paths:
        if not path.segments:
            # Segment-less paths are kept verbatim and block merging across
            # them: there is no endpoint to test against.
            merged.append(path)
            continue
        paths_before += 1

        head = merged[-1] if merged else None
        if head is not None and is_coincident_junction(head, path, tolerance):
            merged[-1] = StrokePath(
                pen_up_position=head.pen_up_position,
                segments=head.segments + path.segments,
            )
            merges += 1
            continue

        merged.append(path)

    paths_after = sum(1 for path in merged if path.segments)
    return MergeResult(
        paths=tuple(merged),
        paths_before=paths_before,
        paths_after=paths_after,
        merges=merges,
    )
