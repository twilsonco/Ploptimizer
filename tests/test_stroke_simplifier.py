"""Tests for the symmetric interval-splitting stroke simplifier.

Covers :mod:`plt_optimizer.core.stroke_simplifier`:

* :func:`split_and_deduplicate_intervals` -- pure 1D interval algebra.
* :func:`simplify_overlapping_strokes` -- document-level deduplication used
  as the structural-pipeline ``dedupe_factory``.
"""

from __future__ import annotations

from plt_optimizer.core.models import (
    ArcSegment,
    Coordinate,
    FooterCommand,
    HeaderCommand,
    PLTDocument,
    StrokePath,
    StrokeSegment,
)
from plt_optimizer.core.stroke_simplifier import (
    _axis_aligned_interval,
    simplify_overlapping_strokes,
    split_and_deduplicate_intervals,
)


def _h(y: float, x1: float, x2: float, cutting: bool = True) -> StrokeSegment:
    """Horizontal cutting segment from (x1, y) to (x2, y)."""
    return StrokeSegment(
        start=Coordinate(x=x1, y=y),
        end=Coordinate(x=x2, y=y),
        is_cutting=cutting,
    )


def _v(x: float, y1: float, y2: float, cutting: bool = True) -> StrokeSegment:
    """Vertical cutting segment from (x, y1) to (x, y2)."""
    return StrokeSegment(
        start=Coordinate(x=x, y=y1),
        end=Coordinate(x=x, y=y2),
        is_cutting=cutting,
    )


def _single_path_doc(*segments: StrokeSegment) -> PLTDocument:
    """Document with one single-segment path per segment (post-fracture shape)."""
    return PLTDocument(
        header_commands=["IN;"],
        stroke_paths=[StrokePath(pen_up_position=seg.start, segments=(seg,)) for seg in segments],
        footer_commands=["SP;"],
    )


def _spans(doc: PLTDocument) -> set:
    """Set of undirected (x1, y1, x2, y2) spans across all cutting segments."""
    result = set()
    for path in doc.stroke_paths:
        for seg in path.segments:
            if isinstance(seg, ArcSegment) or not seg.is_cutting:
                continue
            a = (round(seg.start.x, 5), round(seg.start.y, 5))
            b = (round(seg.end.x, 5), round(seg.end.y, 5))
            result.add((a, b) if a <= b else (b, a))
    return result


def _all_segments(doc: PLTDocument) -> list:
    """Flat list of every segment in document order."""
    return [seg for path in doc.stroke_paths for seg in path.segments]


class TestSplitAndDeduplicateIntervals:
    """Tests for the pure 1D splitter."""

    def test_empty_input(self) -> None:
        assert split_and_deduplicate_intervals([]) == []

    def test_single_interval_unchanged(self) -> None:
        result = split_and_deduplicate_intervals([(0.0, 10.0, 0)])
        assert result == [(0.0, 10.0, 0)]

    def test_disjoint_intervals_both_kept(self) -> None:
        result = split_and_deduplicate_intervals([(0.0, 4.0, 0), (6.0, 10.0, 1)])
        assert result == [(0.0, 4.0, 0), (6.0, 10.0, 1)]

    def test_touching_endpoints_both_kept(self) -> None:
        result = split_and_deduplicate_intervals([(0.0, 5.0, 0), (5.0, 10.0, 1)])
        assert result == [(0.0, 5.0, 0), (5.0, 10.0, 1)]

    def test_exact_duplicate_first_wins(self) -> None:
        result = split_and_deduplicate_intervals([(0.0, 10.0, 0), (0.0, 10.0, 1)])
        assert result == [(0.0, 10.0, 0)]

    def test_brick_work_partial_overlap(self) -> None:
        # The canonical staggered case: [0,10] + [4,14] -> [0,4], [4,10], [10,14].
        result = split_and_deduplicate_intervals([(0.0, 10.0, 0), (4.0, 14.0, 1)])
        assert result == [(0.0, 4.0, 0), (4.0, 10.0, 0), (10.0, 14.0, 1)]

    def test_containment_splits_into_three(self) -> None:
        # [0,10] + [2,5] -> [0,2], [2,5], [5,10]. The long interval is
        # collected first, so it claims the shared middle piece.
        result = split_and_deduplicate_intervals([(0.0, 10.0, 0), (2.0, 5.0, 1)])
        assert result == [(0.0, 2.0, 0), (2.0, 5.0, 0), (5.0, 10.0, 0)]

    def test_both_endpoints_inside_longer(self) -> None:
        # [0,10] + [3,7]: the short interval loses both slices to the long one.
        result = split_and_deduplicate_intervals([(0.0, 10.0, 0), (3.0, 7.0, 1)])
        assert result == [(0.0, 3.0, 0), (3.0, 7.0, 0), (7.0, 10.0, 0)]

    def test_zero_length_interval_dropped(self) -> None:
        # The degenerate interval still contributes its break point (5.0),
        # slicing the long interval; the zero-width piece itself is dropped.
        result = split_and_deduplicate_intervals([(5.0, 5.0, 0), (0.0, 10.0, 1)])
        assert result == [(0.0, 5.0, 1), (5.0, 10.0, 1)]

    def test_sub_tolerance_sliver_dropped(self) -> None:
        # With tol=0.01, the 0.005-wide sliver [9.995, 10] created by slicing
        # falls below tolerance and is dropped from both claimants.
        result = split_and_deduplicate_intervals(
            [(0.0, 10.0, 0), (9.995, 20.0, 1)],
            tol=0.01,
        )
        assert result == [(0.0, 9.995, 0), (10.0, 20.0, 1)]

    def test_multi_interval_chain_splits_at_every_break(self) -> None:
        # Three intervals with staggered ends split at all four interior breaks.
        result = split_and_deduplicate_intervals([(0.0, 10.0, 0), (5.0, 15.0, 1), (12.0, 20.0, 2)])
        assert result == [
            (0.0, 5.0, 0),
            (5.0, 10.0, 0),
            (10.0, 12.0, 1),
            (12.0, 15.0, 1),
            (15.0, 20.0, 2),
        ]

    def test_deterministic_first_come_first_served(self) -> None:
        # Swapping input order flips which payload wins the shared region.
        forward = split_and_deduplicate_intervals([(0.0, 10.0, 0), (4.0, 14.0, 1)])
        reverse = split_and_deduplicate_intervals([(4.0, 14.0, 1), (0.0, 10.0, 0)])
        assert [p for _, _, p in forward] == [0, 0, 1]
        assert [p for _, _, p in reverse] == [1, 1, 0]

    def test_payload_carried_through(self) -> None:
        result = split_and_deduplicate_intervals([(0.0, 10.0, 42)])
        assert result[0][2] == 42

    def test_extra_breaks_slice_intervals(self) -> None:
        # A caller-injected break point (e.g. a perpendicular junction) slices
        # intervals even though no interval endpoint lands there.
        result = split_and_deduplicate_intervals([(0.0, 10.0, 0)], extra_breaks={5.0})
        assert result == [(0.0, 5.0, 0), (5.0, 10.0, 0)]

    def test_extra_breaks_outside_intervals_ignored(self) -> None:
        result = split_and_deduplicate_intervals([(0.0, 10.0, 0)], extra_breaks={20.0})
        assert result == [(0.0, 10.0, 0)]

    def test_extra_breaks_align_offsets_for_dedup(self) -> None:
        # Staggered pair plus an injected junction at 9: every piece boundary
        # aligns, so the shared region still collapses to one winner.
        result = split_and_deduplicate_intervals(
            [(0.0, 10.0, 0), (4.0, 14.0, 1)], extra_breaks={9.0}
        )
        assert result == [
            (0.0, 4.0, 0),
            (4.0, 9.0, 0),
            (9.0, 10.0, 0),
            (10.0, 14.0, 1),
        ]

    def test_line_tol_snaps_jittered_endpoints(self) -> None:
        # CAD export jitter: [0,10] vs [0.002,10.002] would leave two sub-tol
        # slivers plus a near-duplicate middle; snapping onto the lower
        # cluster member collapses the shared region to one winner.
        result = split_and_deduplicate_intervals(
            [(0.0, 10.0, 0), (0.002, 10.002, 1)],
            tol=1e-3,
            line_tol=0.01,
        )
        assert result == [(0.0, 10.0, 0)]

    def test_line_tol_closes_jittered_gap(self) -> None:
        # Two collinear neighbours whose facing endpoints differ by less than
        # line_tol join at the snapped break: no sliver, no gap.
        result = split_and_deduplicate_intervals(
            [(0.0, 10.0, 0), (10.002, 20.0, 1)],
            tol=1e-3,
            line_tol=0.01,
        )
        assert result == [(0.0, 10.0, 0), (10.0, 20.0, 1)]

    def test_line_tol_beyond_tolerance_does_not_snap(self) -> None:
        # Breaks farther apart than line_tol keep their own boundaries.
        result = split_and_deduplicate_intervals(
            [(0.0, 10.0, 0), (30.0, 40.0, 1)],
            tol=1e-3,
            line_tol=0.01,
        )
        assert result == [(0.0, 10.0, 0), (30.0, 40.0, 1)]

    def test_line_tol_none_keeps_exact_breaks(self) -> None:
        # Default (line_tol=None): jittered endpoints stay distinct, so the
        # 0.002 sliver survives and the near-duplicate middle is NOT deduped.
        result = split_and_deduplicate_intervals(
            [(0.0, 10.0, 0), (0.002, 10.002, 1)],
            tol=1e-3,
        )
        assert result == [(0.0, 0.002, 0), (0.002, 10.0, 0), (10.0, 10.002, 1)]

    def test_line_tol_preserves_short_strokes(self) -> None:
        # A stroke shorter than line_tol must not be collapsed away by
        # endpoint snapping: its unsnapped span survives intact.
        result = split_and_deduplicate_intervals(
            [(0.0, 5.0, 0), (5.0, 20.0, 1)],
            tol=1e-3,
            line_tol=10.0,
        )
        assert result == [(0.0, 5.0, 0), (5.0, 20.0, 1)]


class TestAxisAlignedInterval:
    """Tests for the segment classification helper."""

    def test_horizontal(self) -> None:
        result = _axis_aligned_interval(_h(5.0, 0.0, 10.0), tol=1e-5)
        assert result == ("H", 5.0, 0.0, 10.0)

    def test_vertical(self) -> None:
        result = _axis_aligned_interval(_v(3.0, 1.0, 9.0), tol=1e-5)
        assert result == ("V", 3.0, 1.0, 9.0)

    def test_diagonal_returns_none(self) -> None:
        seg = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=10.0),
            is_cutting=True,
        )
        assert _axis_aligned_interval(seg, tol=1e-5) is None

    def test_degenerate_point_returns_none(self) -> None:
        assert _axis_aligned_interval(_h(2.0, 4.0, 4.0), tol=1e-5) is None


class TestSimplifyOverlappingStrokes:
    """Document-level behaviour of the dedupe factory."""

    def test_empty_document(self) -> None:
        doc = PLTDocument(header_commands=[], stroke_paths=[], footer_commands=[])
        result = simplify_overlapping_strokes(doc)
        assert result.stroke_paths == []

    def test_no_overlap_document_unchanged(self) -> None:
        doc = _single_path_doc(_h(0.0, 0.0, 100.0), _h(50.0, 50.0, 150.0))
        result = simplify_overlapping_strokes(doc)
        assert len(result.stroke_paths) == 2
        assert len(_all_segments(result)) == 2

    def test_identical_duplicates_collapse(self) -> None:
        doc = _single_path_doc(_h(0.0, 0.0, 100.0), _h(0.0, 0.0, 100.0))
        result = simplify_overlapping_strokes(doc)
        assert len(_all_segments(result)) == 1

    def test_reversed_duplicate_collapses(self) -> None:
        doc = _single_path_doc(_h(0.0, 0.0, 100.0), _h(0.0, 100.0, 0.0))
        result = simplify_overlapping_strokes(doc)
        assert len(_all_segments(result)) == 1

    def test_brick_work_shared_region_split(self) -> None:
        # Label A top edge X in [0,10] @ Y=5; label B bottom edge X in [4,14].
        doc = _single_path_doc(_h(5.0, 0.0, 10.0), _h(5.0, 4.0, 14.0))
        result = simplify_overlapping_strokes(doc)
        assert _spans(result) == {
            ((0.0, 5.0), (4.0, 5.0)),
            ((4.0, 5.0), (10.0, 5.0)),
            ((10.0, 5.0), (14.0, 5.0)),
        }

    def test_shared_rectangle_edges_grid(self) -> None:
        # Two unit squares sharing edge x=1: 7 unique segments (8 - 1 shared).
        left = [
            _v(0.0, 0.0, 1.0),
            _h(1.0, 0.0, 1.0),
            _v(1.0, 1.0, 0.0),
            _h(0.0, 1.0, 0.0),
        ]
        right = [
            _v(1.0, 0.0, 1.0),
            _h(1.0, 1.0, 2.0),
            _v(2.0, 1.0, 0.0),
            _h(0.0, 2.0, 1.0),
        ]
        doc = _single_path_doc(*(left + right))
        result = simplify_overlapping_strokes(doc)
        assert len(_all_segments(result)) == 7

    def test_vertical_brick_work(self) -> None:
        doc = _single_path_doc(_v(7.0, 0.0, 10.0), _v(7.0, 4.0, 14.0))
        result = simplify_overlapping_strokes(doc)
        assert _spans(result) == {
            ((7.0, 0.0), (7.0, 4.0)),
            ((7.0, 4.0), (7.0, 10.0)),
            ((7.0, 10.0), (7.0, 14.0)),
        }

    def test_parallel_lines_never_merge(self) -> None:
        # Same span, different supporting lines: both kept.
        doc = _single_path_doc(_h(0.0, 0.0, 10.0), _h(1.0, 0.0, 10.0))
        result = simplify_overlapping_strokes(doc)
        assert len(_all_segments(result)) == 2

    def test_rapid_segments_untouched(self) -> None:
        doc = _single_path_doc(_h(0.0, 0.0, 10.0, cutting=False), _h(0.0, 0.0, 10.0))
        result = simplify_overlapping_strokes(doc)
        segs = _all_segments(result)
        assert len(segs) == 2
        assert segs[0].is_cutting is False
        assert segs[1].is_cutting is True

    def test_diagonal_segments_pass_through(self) -> None:
        diag1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=10.0),
            is_cutting=True,
        )
        diag2 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=5.0, y=5.0),
            is_cutting=True,
        )
        doc = _single_path_doc(diag1, diag2)
        result = simplify_overlapping_strokes(doc)
        assert len(_all_segments(result)) == 2

    def test_arc_paths_pass_through_whole(self) -> None:
        arc = ArcSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=10.0),
            center=Coordinate(x=5.0, y=0.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        arc_path = StrokePath(pen_up_position=None, segments=(arc,))
        doc = PLTDocument(
            header_commands=[],
            stroke_paths=[arc_path],
            footer_commands=[],
        )
        result = simplify_overlapping_strokes(doc)
        assert result.stroke_paths == [arc_path]

    def test_arc_bearing_mixed_path_passes_through(self) -> None:
        # A path mixing an arc with a line that duplicates a standalone line:
        # the arc path is untouchable, so both survive.
        arc = ArcSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=10.0),
            center=Coordinate(x=5.0, y=0.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        line = _h(0.0, 0.0, 100.0)
        mixed = StrokePath(pen_up_position=None, segments=(arc, line))
        dup = _h(0.0, 0.0, 100.0)
        doc = PLTDocument(
            header_commands=[],
            stroke_paths=[mixed, StrokePath(pen_up_position=dup.start, segments=(dup,))],
            footer_commands=[],
        )
        result = simplify_overlapping_strokes(doc)
        assert len(_all_segments(result)) == 3

    def test_header_footer_preserved_as_objects(self) -> None:
        header = HeaderCommand(instruction="IN")
        footer = FooterCommand(instruction="SP")
        doc = PLTDocument(
            header_commands=[header],
            stroke_paths=[StrokePath(pen_up_position=None, segments=(_h(0.0, 0.0, 1.0),))],
            footer_commands=[footer],
        )
        result = simplify_overlapping_strokes(doc)
        assert list(result.header_commands) == [header]
        assert list(result.footer_commands) == [footer]

    def test_fully_superseded_segment_removed(self) -> None:
        # Short stroke fully inside a longer one: the short one vanishes and
        # the long one survives, sliced at the short one's break points.
        long_seg = _h(0.0, 0.0, 100.0)
        short_seg = _h(0.0, 20.0, 30.0)
        doc = _single_path_doc(long_seg, short_seg)
        result = simplify_overlapping_strokes(doc)
        assert len(result.stroke_paths) == 1
        assert _spans(result) == {
            ((0.0, 0.0), (20.0, 0.0)),
            ((20.0, 0.0), (30.0, 0.0)),
            ((30.0, 0.0), (100.0, 0.0)),
        }

    def test_multi_segment_path_splits_when_edge_superseded(self) -> None:
        # One path holds two edges of a rectangle; the shared bottom edge is
        # claimed by an earlier path -> this path splits into a lone top edge.
        top = _h(10.0, 0.0, 10.0)
        bottom = _h(0.0, 0.0, 10.0)
        path = StrokePath(pen_up_position=Coordinate(x=0.0, y=10.0), segments=(top, bottom))
        dup_bottom = _h(0.0, 0.0, 10.0)
        doc = PLTDocument(
            header_commands=[],
            stroke_paths=[
                # Winner collected first so `bottom` loses the shared edge.
                StrokePath(pen_up_position=dup_bottom.start, segments=(dup_bottom,)),
                path,
            ],
            footer_commands=[],
        )
        result = simplify_overlapping_strokes(doc)
        assert len(result.stroke_paths) == 2
        assert _spans(result) == {
            ((0.0, 10.0), (10.0, 10.0)),
            ((0.0, 0.0), (10.0, 0.0)),
        }

    def test_partial_replacement_keeps_chain_contiguous(self) -> None:
        # A 2-segment path where the first segment wins two atomic pieces:
        # both pieces stay in the same path, chain-connected.
        long_seg = _h(0.0, 0.0, 10.0)
        tail = _h(0.0, 10.0, 20.0)
        path = StrokePath(pen_up_position=Coordinate(x=0.0, y=0.0), segments=(long_seg, tail))
        overlap = _h(0.0, 4.0, 14.0)
        doc = PLTDocument(
            header_commands=[],
            stroke_paths=[
                path,
                StrokePath(pen_up_position=overlap.start, segments=(overlap,)),
            ],
            footer_commands=[],
        )
        result = simplify_overlapping_strokes(doc)
        # Expected spans: [0,4], [4,10] (from long_seg), [10,14], [14,20]
        # (from tail); the overlap loses both of its slices.
        assert _spans(result) == {
            ((0.0, 0.0), (4.0, 0.0)),
            ((4.0, 0.0), (10.0, 0.0)),
            ((10.0, 0.0), (14.0, 0.0)),
            ((14.0, 0.0), (20.0, 0.0)),
        }
        # All four pieces stay in the single source path, chain-connected and
        # running low -> high like the originals (no spurious pen lifts).
        assert len(result.stroke_paths) == 1
        segs = result.stroke_paths[0].segments
        assert len(segs) == 4
        for previous, following in zip(segs, segs[1:]):
            assert previous.end == following.start
        assert all(seg.start.x < seg.end.x for seg in segs)

    def test_reversed_winner_keeps_original_direction(self) -> None:
        # Winner ran high -> low; won pieces must also run high -> low.
        rev = _h(0.0, 10.0, 0.0)
        overlap = _h(0.0, 4.0, -4.0)
        doc = _single_path_doc(rev, overlap)
        result = simplify_overlapping_strokes(doc)
        winners = [
            seg
            for seg in _all_segments(result)
            if round(seg.start.x, 5) == 10.0 or round(seg.start.x, 5) == 4.0
        ]
        assert winners
        for seg in winners:
            assert seg.start.x > seg.end.x

    def test_pen_up_reanchored_when_first_segment_removed(self) -> None:
        # Path whose first segment is fully superseded: the split-off path's
        # pen_up_position must re-anchor to the first surviving segment.
        dup1 = _h(0.0, 0.0, 10.0)
        keeper = _h(0.0, 30.0, 40.0)
        path = StrokePath(pen_up_position=Coordinate(x=0.0, y=0.0), segments=(dup1, keeper))
        master = _h(0.0, 0.0, 10.0)
        doc = PLTDocument(
            header_commands=[],
            stroke_paths=[
                # Winner collected first so `dup1` loses the shared span.
                StrokePath(pen_up_position=master.start, segments=(master,)),
                path,
            ],
            footer_commands=[],
        )
        result = simplify_overlapping_strokes(doc)
        keeper_paths = [
            p for p in result.stroke_paths if p.segments and round(p.segments[0].start.x, 5) == 30.0
        ]
        assert len(keeper_paths) == 1
        assert keeper_paths[0].pen_up_position == Coordinate(x=30.0, y=0.0)

    def test_pen_up_reanchored_when_first_segment_loses_head(self) -> None:
        # Partial loss is just as dangerous as full removal: the surviving
        # piece starts at an interior point, and a stale pen_up_position
        # makes the emitter cut a phantom stroke from the old pen-up spot.
        master = _h(0.0, 10.0, 0.0)
        path = StrokePath(
            pen_up_position=Coordinate(x=6.0, y=0.0),
            segments=(_h(0.0, 6.0, 12.0),),
        )
        doc = PLTDocument(
            header_commands=[],
            stroke_paths=[
                # Winner collected first: claims [0,10], leaving [10,12].
                StrokePath(pen_up_position=master.start, segments=(master,)),
                path,
            ],
            footer_commands=[],
        )
        result = simplify_overlapping_strokes(doc)
        survivor = next(
            p for p in result.stroke_paths if p.segments and round(p.segments[0].end.x, 5) == 12.0
        )
        assert survivor.pen_up_position == Coordinate(x=10.0, y=0.0)

    def test_pen_up_matches_first_segment_after_simplify(self) -> None:
        # Emitter invariant: PU lands exactly on the first cut's start, so
        # the pen never drags a phantom stroke from a stale position.
        doc = _single_path_doc(
            _h(0.0, 10.0, 0.0),
            _h(0.0, 6.0, 12.0),
            _h(5.0, 0.0, 10.0),
            _v(5.0, 5.0, 0.0),
        )
        result = simplify_overlapping_strokes(doc)
        for path in result.stroke_paths:
            if path.pen_up_position is not None and path.segments:
                assert path.pen_up_position == path.segments[0].start

    def test_path_fully_superseded_dropped(self) -> None:
        # Both segments of a path lose everything -> path disappears.
        s1 = _h(0.0, 0.0, 10.0)
        s2 = _v(0.0, 0.0, 10.0)
        path = StrokePath(pen_up_position=Coordinate(x=0.0, y=0.0), segments=(s1, s2))
        doc = _single_path_doc(_h(0.0, 0.0, 10.0), _v(0.0, 0.0, 10.0))
        doc.stroke_paths.append(path)  # Collected last: loses both edges.
        result = simplify_overlapping_strokes(doc)
        assert len(result.stroke_paths) == 2
        assert len(_all_segments(result)) == 2

    def test_empty_path_passthrough(self) -> None:
        empty = StrokePath(pen_up_position=None, segments=())
        doc = PLTDocument(
            header_commands=[],
            stroke_paths=[empty],
            footer_commands=[],
        )
        result = simplify_overlapping_strokes(doc)
        assert result.stroke_paths == []

    def test_zero_length_segment_kept_in_place(self) -> None:
        point = _h(3.0, 5.0, 5.0)
        doc = _single_path_doc(point)
        result = simplify_overlapping_strokes(doc)
        assert len(_all_segments(result)) == 1

    def test_tolerance_scales_with_argument(self) -> None:
        # A 0.01 "slope" counts as horizontal with tol=0.05.
        seg = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=0.004),
            is_cutting=True,
        )
        doc = _single_path_doc(seg, _h(0.0, 4.0, 14.0))
        result = simplify_overlapping_strokes(doc, tol=0.05)
        assert len(_all_segments(result)) == 3

    def test_line_tol_merges_jittered_supporting_lines(self) -> None:
        # EngraveLab duplicate at a 6-unit perpendicular offset (the
        # 2026-07-10 sheet's x=8122 vs x=8128 case): with line_tol the two
        # supporting lines merge and the inner stroke is fully superseded;
        # without it both whole strokes survive on separate lines.
        doc = _single_path_doc(_v(8122.0, 142.788, 4696.788), _v(8128.0, 142.8, 4206.8))
        merged = simplify_overlapping_strokes(doc, tol=1e-3, line_tol=10.0)
        assert _spans(merged) == {
            ((8122.0, 142.788), (8122.0, 4206.8)),
            ((8122.0, 4206.8), (8122.0, 4696.788)),
        }
        unmerged = simplify_overlapping_strokes(doc, tol=1e-3)
        assert len(_all_segments(unmerged)) == 2

    def test_line_tol_brickwork_jitter_dedupes(self) -> None:
        # Brick-work stagger with 0.002 line jitter: merging the supporting
        # lines collapses the shared region at the snapped boundary.
        doc = _single_path_doc(_v(0.0, 0.0, 10.0), _v(0.002, 5.0, 15.0))
        result = simplify_overlapping_strokes(doc, tol=1e-3, line_tol=0.01)
        assert len(_all_segments(result)) == 3
        unmerged = simplify_overlapping_strokes(doc, tol=1e-3)
        assert len(_all_segments(unmerged)) == 2

    def test_line_tol_keeps_genuinely_distinct_lines(self) -> None:
        # Parallel lines 196 units apart (real design spacing) never merge,
        # even with a generous line_tol.
        doc = _single_path_doc(_v(0.0, 0.0, 100.0), _v(196.0, 0.0, 100.0))
        result = simplify_overlapping_strokes(doc, tol=1e-3, line_tol=10.0)
        assert len(_all_segments(result)) == 2


class TestCrossAxisBreakPoints:
    """T-junction / crossing fracturing from perpendicular segments."""

    def test_horizontal_fractured_at_vertical_t_junction(self) -> None:
        # A vertical border touching a long horizontal border mid-span
        # (endpoint touch) must fracture the horizontal stroke.
        doc = _single_path_doc(_h(0.0, 0.0, 10.0), _v(5.0, 0.0, 5.0))
        result = simplify_overlapping_strokes(doc)
        assert _spans(result) == {
            ((0.0, 0.0), (5.0, 0.0)),
            ((5.0, 0.0), (10.0, 0.0)),
            ((5.0, 0.0), (5.0, 5.0)),
        }

    def test_crossing_fractures_both_axes(self) -> None:
        # A vertical crossing through the line splits the horizontal, and the
        # horizontal's supporting line symmetrically splits the vertical.
        doc = _single_path_doc(_h(0.0, 0.0, 10.0), _v(5.0, -5.0, 5.0))
        result = simplify_overlapping_strokes(doc)
        assert _spans(result) == {
            ((0.0, 0.0), (5.0, 0.0)),
            ((5.0, 0.0), (10.0, 0.0)),
            ((5.0, -5.0), (5.0, 0.0)),
            ((5.0, 0.0), (5.0, 5.0)),
        }

    def test_vertical_fractured_at_horizontal_junction(self) -> None:
        # Symmetric case: a horizontal stub touching a long vertical border.
        doc = _single_path_doc(_v(0.0, 0.0, 10.0), _h(5.0, 0.0, 5.0))
        result = simplify_overlapping_strokes(doc)
        assert _spans(result) == {
            ((0.0, 0.0), (0.0, 5.0)),
            ((0.0, 5.0), (0.0, 10.0)),
            ((0.0, 5.0), (5.0, 5.0)),
        }

    def test_brick_work_shared_region_fractured_at_junction(self) -> None:
        # Staggered labels sharing y=0: edges [0,10] and [4,14] plus a
        # vertical border at x=7 touching the line. The shared region is
        # deduplicated AND every piece boundary aligns at x=7.
        doc = _single_path_doc(_h(0.0, 0.0, 10.0), _h(0.0, 4.0, 14.0), _v(7.0, 0.0, 5.0))
        result = simplify_overlapping_strokes(doc)
        assert _spans(result) == {
            ((0.0, 0.0), (4.0, 0.0)),
            ((4.0, 0.0), (7.0, 0.0)),
            ((7.0, 0.0), (10.0, 0.0)),
            ((10.0, 0.0), (14.0, 0.0)),
            ((7.0, 0.0), (7.0, 5.0)),
        }

    def test_perpendicular_segment_far_away_injects_nothing(self) -> None:
        # A vertical that never touches the horizontal's supporting line must
        # not split it.
        doc = _single_path_doc(_h(0.0, 0.0, 10.0), _v(20.0, 10.0, 20.0))
        result = simplify_overlapping_strokes(doc)
        assert _spans(result) == {
            ((0.0, 0.0), (10.0, 0.0)),
            ((20.0, 10.0), (20.0, 20.0)),
        }

    def test_touch_at_far_endpoint_injects_boundary_only(self) -> None:
        # Vertical touching exactly at the horizontal's start endpoint: the
        # junction is a boundary, so no extra piece is created.
        doc = _single_path_doc(_h(0.0, 0.0, 10.0), _v(0.0, 0.0, 5.0))
        result = simplify_overlapping_strokes(doc)
        assert _spans(result) == {
            ((0.0, 0.0), (10.0, 0.0)),
            ((0.0, 0.0), (0.0, 5.0)),
        }

    def test_rapid_perpendicular_injects_nothing(self) -> None:
        # Rapid (non-cutting) verticals are not candidates and must not
        # fracture cutting horizontals.
        doc = _single_path_doc(_h(0.0, 0.0, 10.0), _v(5.0, 0.0, 5.0, cutting=False))
        result = simplify_overlapping_strokes(doc)
        # _spans() only counts cutting segments, so only the intact
        # horizontal appears; the rapid move survives untouched.
        assert _spans(result) == {((0.0, 0.0), (10.0, 0.0))}
        assert len(_all_segments(result)) == 2

    def test_closed_rectangle_unchanged(self) -> None:
        # A lone rectangle: every junction lands on an existing endpoint, so
        # cross-axis breaks create no new pieces.
        rect = [_h(0.0, 0.0, 10.0), _v(10.0, 0.0, 5.0), _h(5.0, 10.0, 0.0), _v(0.0, 5.0, 0.0)]
        doc = _single_path_doc(*rect)
        result = simplify_overlapping_strokes(doc)
        assert len(_all_segments(result)) == 4

    def test_sw0914_example_duplicates_resolved(self) -> None:
        """Pin the 2026-07-10 sheet: near-coincident strokes must fully dedupe.

        The sheet's two label rectangles are emitted with 0.002-unit line
        jitter and a 6-unit (0.006") offset duplicate (x=8122 vs x=8128),
        which the strict 1e-3 supporting-line tolerance used to miss. After
        production preprocessing no two output strokes within ``line_tol``
        may overlap along a supporting line, and no cut coverage may be lost.
        Merging jittered lines may *bridge* sub-line_tol gaps between
        rectangles (intended), so only coverage loss is bounded.
        """
        from pathlib import Path

        import pytest

        from plt_optimizer.core.parser import PLTParser
        from plt_optimizer.core.pipeline import (
            _REDUNDANCY_LINE_TOL,
            _REDUNDANCY_TOL,
            preprocess_document,
        )

        example_path = (
            Path(__file__).parent.parent / "examples" / "2026-07-10 SW0914 1230sheet0.plt"
        )
        if not example_path.exists():
            pytest.skip(f"Example file not found: {example_path}")

        doc = PLTParser().parse_file(example_path)
        result = preprocess_document(doc, is_structural=True)

        def _linear_cutting(document: PLTDocument) -> list:
            out = []
            for path in document.stroke_paths:
                for seg in path.segments:
                    if isinstance(seg, ArcSegment) or not seg.is_cutting:
                        continue
                    dx = abs(seg.end.x - seg.start.x)
                    dy = abs(seg.end.y - seg.start.y)
                    if dx < _REDUNDANCY_TOL:
                        lo, hi = sorted((seg.start.y, seg.end.y))
                        out.append(("V", seg.start.x, lo, hi))
                    elif dy < _REDUNDANCY_TOL:
                        lo, hi = sorted((seg.start.x, seg.end.x))
                        out.append(("H", seg.start.y, lo, hi))
            return out

        def _cluster(lines: list) -> list:
            groups: list = []
            for orient, fixed, lo, hi in lines:
                for group in groups:
                    if group[0] == orient and abs(group[1] - fixed) <= _REDUNDANCY_LINE_TOL:
                        group[2].append((lo, hi))
                        break
                else:
                    groups.append([orient, fixed, [(lo, hi)]])
            return groups

        def _union(spans: list) -> float:
            total = 0.0
            cur_lo, cur_hi = sorted(spans)[0]
            for lo, hi in sorted(spans)[1:]:
                if lo <= cur_hi + 1e-6:
                    cur_hi = max(cur_hi, hi)
                else:
                    total += cur_hi - cur_lo
                    cur_lo, cur_hi = lo, hi
            return total + cur_hi - cur_lo

        out_lines = _linear_cutting(result)
        assert out_lines

        # No two output strokes on a merged supporting line may overlap.
        for orient, fixed, spans in _cluster(out_lines):
            for i in range(len(spans)):
                for j in range(i + 1, len(spans)):
                    overlap = min(spans[i][1], spans[j][1]) - max(spans[i][0], spans[j][0])
                    assert overlap <= 1e-6, f"{orient}@{fixed}: {spans[i]} vs {spans[j]}"

        # No cut coverage lost (merging may bridge jittered gaps).
        in_total = sum(_union(g[2]) for g in _cluster(_linear_cutting(doc)))
        out_total = sum(_union(g[2]) for g in _cluster(out_lines))
        assert out_total >= in_total - 1.0
