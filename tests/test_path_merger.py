"""Tests for the coincident-stroke merger (core.path_merger).

Covers :func:`plt_optimizer.core.path_merger.is_coincident_junction` and
:func:`plt_optimizer.core.path_merger.merge_coincident_paths`:

* tip-to-tail stitching (single pair, transitive runs, barriers);
* the two-sided predicate (rapid target *and* first segment start);
* tolerance behaviour (inside/outside, configurable);
* invariants: segment multiset, cutting distance, traversal order, and
  ``rapid_distance()`` preservation.
"""

from __future__ import annotations

import math
from typing import List, Sequence, Tuple

from plt_optimizer.core.models import (
    ArcSegment,
    Coordinate,
    PLTDocument,
    Segment,
    StrokePath,
    StrokeSegment,
)
from plt_optimizer.core.path_merger import (
    DEFAULT_MERGE_TOLERANCE,
    MergeResult,
    is_coincident_junction,
    merge_coincident_paths,
)


def _seg(x1: float, y1: float, x2: float, y2: float, cutting: bool = True) -> StrokeSegment:
    """Line segment from ``(x1, y1)`` to ``(x2, y2)``."""
    return StrokeSegment(
        start=Coordinate(x=x1, y=y1),
        end=Coordinate(x=x2, y=y2),
        is_cutting=cutting,
    )


def _path(
    pen_up: Tuple[float, float] | None,
    *segments: Segment,
) -> StrokePath:
    """StrokePath with an explicit pen-up target (``None`` = unset)."""
    target = Coordinate(x=pen_up[0], y=pen_up[1]) if pen_up is not None else None
    return StrokePath(pen_up_position=target, segments=tuple(segments))


def _spans(paths: Sequence[StrokePath]) -> List[Tuple[float, float, float, float]]:
    """Undirected ``(x1, y1, x2, y2)`` span of every segment, sorted."""
    spans: List[Tuple[float, float, float, float]] = []
    for path in paths:
        for seg in path.segments:
            a = (round(seg.start.x, 6), round(seg.start.y, 6))
            b = (round(seg.end.x, 6), round(seg.end.y, 6))
            span = (a[0], a[1], b[0], b[1])
            reverse = (b[0], b[1], a[0], a[1])
            spans.append(span if span <= reverse else reverse)
    return sorted(spans)


def _doc(paths: Sequence[StrokePath]) -> PLTDocument:
    """Wrap paths in a bare document for metric checks."""
    return PLTDocument(header_commands=[], stroke_paths=list(paths), footer_commands=[])


class TestIsCoincidentJunction:
    """The two-sided junction predicate."""

    def test_tip_to_tail_is_coincident(self) -> None:
        tail = _path((0.0, 0.0), _seg(0.0, 0.0, 10.0, 0.0))
        head = _path((10.0, 0.0), _seg(10.0, 0.0, 10.0, 5.0))
        assert is_coincident_junction(tail, head)

    def test_rapid_gap_is_not_coincident(self) -> None:
        tail = _path((0.0, 0.0), _seg(0.0, 0.0, 10.0, 0.0))
        head = _path((40.0, 0.0), _seg(40.0, 0.0, 40.0, 5.0))
        assert not is_coincident_junction(tail, head)

    def test_pen_up_ahead_of_start_is_not_coincident(self) -> None:
        """Arc-native plunge: pen-up lands away from the previous stroke's end."""
        tail = _path((0.0, 0.0), _seg(0.0, 0.0, 10.0, 0.0))
        head = _path((0.0, 0.0), _seg(10.0, 0.0, 20.0, 0.0))
        assert not is_coincident_junction(tail, head)

    def test_start_ahead_of_pen_up_is_not_coincident(self) -> None:
        """Pen-up lands on the previous end but the plunge is elsewhere."""
        tail = _path((0.0, 0.0), _seg(0.0, 0.0, 10.0, 0.0))
        head = _path((10.0, 0.0), _seg(12.0, 0.0, 20.0, 0.0))
        assert not is_coincident_junction(tail, head)

    def test_unset_pen_up_falls_back_to_first_start(self) -> None:
        tail = _path((0.0, 0.0), _seg(0.0, 0.0, 10.0, 0.0))
        head = _path(None, _seg(10.0, 0.0, 10.0, 5.0))
        assert is_coincident_junction(tail, head)

    def test_within_tolerance_is_coincident(self) -> None:
        tail = _path((0.0, 0.0), _seg(0.0, 0.0, 10.0, 0.0))
        head = _path((10.0005, 0.0), _seg(10.0005, 0.0, 10.0, 5.0))
        assert is_coincident_junction(tail, head)

    def test_outside_tolerance_is_not_coincident(self) -> None:
        tail = _path((0.0, 0.0), _seg(0.0, 0.0, 10.0, 0.0))
        head = _path((10.01, 0.0), _seg(10.01, 0.0, 10.0, 5.0))
        assert not is_coincident_junction(tail, head)

    def test_custom_tolerance_widens_the_test(self) -> None:
        tail = _path((0.0, 0.0), _seg(0.0, 0.0, 10.0, 0.0))
        head = _path((10.01, 0.0), _seg(10.01, 0.0, 10.0, 5.0))
        assert is_coincident_junction(tail, head, tolerance=0.05)

    def test_segment_less_paths_are_never_coincident(self) -> None:
        empty = _path((0.0, 0.0))
        filled = _path((0.0, 0.0), _seg(0.0, 0.0, 10.0, 0.0))
        assert not is_coincident_junction(filled, empty)
        assert not is_coincident_junction(empty, filled)

    def test_default_tolerance_matches_coord_tolerance(self) -> None:
        assert DEFAULT_MERGE_TOLERANCE == 1e-3


class TestMergeCoincidentPaths:
    """Document-level stitching semantics."""

    def test_empty_input(self) -> None:
        result = merge_coincident_paths([])
        assert result == MergeResult(paths=(), paths_before=0, paths_after=0, merges=0)

    def test_single_path_unchanged(self) -> None:
        paths = [_path((0.0, 0.0), _seg(0.0, 0.0, 10.0, 0.0))]
        result = merge_coincident_paths(paths)
        assert list(result.paths) == paths
        assert (result.paths_before, result.paths_after, result.merges) == (1, 1, 0)

    def test_no_coincident_junction_is_identity(self) -> None:
        paths = [
            _path((0.0, 0.0), _seg(0.0, 0.0, 10.0, 0.0)),
            _path((40.0, 0.0), _seg(40.0, 0.0, 40.0, 5.0)),
        ]
        result = merge_coincident_paths(paths)
        assert list(result.paths) == paths
        assert (result.paths_before, result.paths_after, result.merges) == (2, 2, 0)

    def test_pair_merges_into_one_path(self) -> None:
        paths = [
            _path((0.0, 0.0), _seg(0.0, 0.0, 10.0, 0.0)),
            _path((10.0, 0.0), _seg(10.0, 0.0, 10.0, 5.0)),
        ]
        result = merge_coincident_paths(paths)
        assert (result.paths_before, result.paths_after, result.merges) == (2, 1, 1)
        assert len(result.paths) == 1
        merged = result.paths[0]
        # Keeps the FIRST path's pen-up target and concatenates the segments.
        assert merged.pen_up_position == Coordinate(x=0.0, y=0.0)
        assert len(merged.segments) == 2
        assert merged.segments[0].end == Coordinate(x=10.0, y=0.0)
        assert merged.segments[1].start == Coordinate(x=10.0, y=0.0)

    def test_run_collapses_transitively(self) -> None:
        """A fractured rectangle (4 tip-to-tail sides) becomes one closed path."""
        paths = [
            _path((0.0, 0.0), _seg(0.0, 0.0, 10.0, 0.0)),
            _path((10.0, 0.0), _seg(10.0, 0.0, 10.0, 10.0)),
            _path((10.0, 10.0), _seg(10.0, 10.0, 0.0, 10.0)),
            _path((0.0, 10.0), _seg(0.0, 10.0, 0.0, 0.0)),
        ]
        result = merge_coincident_paths(paths)
        assert (result.paths_before, result.paths_after, result.merges) == (4, 1, 3)
        assert len(result.paths[0].segments) == 4

    def test_partial_merges_keep_order(self) -> None:
        """Two runs separated by a genuine rapid: [A+B], [C+D]."""
        paths = [
            _path((0.0, 0.0), _seg(0.0, 0.0, 10.0, 0.0)),
            _path((10.0, 0.0), _seg(10.0, 0.0, 10.0, 10.0)),
            _path((50.0, 50.0), _seg(50.0, 50.0, 60.0, 50.0)),
            _path((60.0, 50.0), _seg(60.0, 50.0, 60.0, 60.0)),
        ]
        result = merge_coincident_paths(paths)
        assert (result.paths_before, result.paths_after, result.merges) == (4, 2, 2)
        assert result.paths[0].pen_up_position == Coordinate(x=0.0, y=0.0)
        assert result.paths[1].pen_up_position == Coordinate(x=50.0, y=50.0)

    def test_arc_native_plunge_rapid_survives(self) -> None:
        """A glyph whose PU lands away from the previous end is never absorbed."""
        arc = ArcSegment(
            start=Coordinate(x=10.0, y=0.0),
            end=Coordinate(x=10.0, y=0.0),
            center=Coordinate(x=15.0, y=0.0),
            sweep_angle=360.0,
            is_cutting=True,
        )
        paths = [
            _path((0.0, 0.0), _seg(0.0, 0.0, 10.0, 0.0)),
            _path((10.0, 0.0), arc),
        ]
        result = merge_coincident_paths(paths)
        assert result.merges == 1  # pen-up == previous end == arc start -> continuous

        far_arc = ArcSegment(
            start=Coordinate(x=30.0, y=0.0),
            end=Coordinate(x=30.0, y=0.0),
            center=Coordinate(x=35.0, y=0.0),
            sweep_angle=360.0,
            is_cutting=True,
        )
        paths = [
            _path((0.0, 0.0), _seg(0.0, 0.0, 10.0, 0.0)),
            _path((30.0, 0.0), far_arc),
        ]
        result = merge_coincident_paths(paths)
        assert result.merges == 0

    def test_segment_less_paths_pass_through_as_barriers(self) -> None:
        empty = _path((10.0, 0.0))
        paths = [
            _path((0.0, 0.0), _seg(0.0, 0.0, 10.0, 0.0)),
            empty,
            _path((10.0, 0.0), _seg(10.0, 0.0, 10.0, 5.0)),
        ]
        result = merge_coincident_paths(paths)
        assert (result.paths_before, result.paths_after, result.merges) == (2, 2, 0)
        assert list(result.paths) == paths

    def test_custom_tolerance_merges_jittered_junction(self) -> None:
        paths = [
            _path((0.0, 0.0), _seg(0.0, 0.0, 10.0, 0.0)),
            _path((10.01, 0.0), _seg(10.01, 0.0, 10.0, 5.0)),
        ]
        assert merge_coincident_paths(paths).merges == 0
        assert merge_coincident_paths(paths, tolerance=0.05).merges == 1

    def test_invariants_preserved(self) -> None:
        """Segment multiset, cutting distance, and rapid travel are unchanged."""
        paths = [
            _path((0.0, 0.0), _seg(0.0, 0.0, 10.0, 0.0)),
            _path((10.0, 0.0), _seg(10.0, 0.0, 10.0, 10.0)),
            _path((50.0, 50.0), _seg(50.0, 50.0, 60.0, 50.0)),
        ]
        result = merge_coincident_paths(paths)
        assert _spans(result.paths) == _spans(paths)
        assert math.isclose(
            sum(p.cutting_distance for p in result.paths),
            sum(p.cutting_distance for p in paths),
            abs_tol=1e-9,
        )
        assert _doc(result.paths).rapid_distance() == _doc(paths).rapid_distance()

    def test_rapid_distance_is_metric_neutral(self) -> None:
        """Merging removes tool-ups without changing emitted rapid travel."""
        paths = [
            _path((0.0, 0.0), _seg(0.0, 0.0, 10.0, 0.0)),
            _path((10.0, 0.0), _seg(10.0, 0.0, 10.0, 10.0)),
            _path((50.0, 50.0), _seg(50.0, 50.0, 60.0, 50.0)),
        ]
        result = merge_coincident_paths(paths)
        assert len(result.paths) == 2
        # Before: 0->10 gap 0, 10->50 gap = hypot(40, 40). After: one gap, same length.
        assert _doc(result.paths).rapid_distance() == _doc(paths).rapid_distance()

    def test_input_sequence_is_not_mutated(self) -> None:
        paths = [
            _path((0.0, 0.0), _seg(0.0, 0.0, 10.0, 0.0)),
            _path((10.0, 0.0), _seg(10.0, 0.0, 10.0, 5.0)),
        ]
        snapshot = list(paths)
        merge_coincident_paths(paths)
        assert paths == snapshot
