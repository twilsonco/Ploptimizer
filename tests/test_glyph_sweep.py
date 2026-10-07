"""Tests for the intra-chunk glyph direction sweep (``core/glyph_sweep.py``).

Fixtures are hand-computable so every expected distance is exact: single
straight strokes on the x-axis, one glyph per group unless a test needs a
multi-stroke glyph.
"""

from __future__ import annotations

import math
from typing import List, Sequence, Tuple

import pytest

from plt_optimizer.core.glyph_sweep import DEFAULT_TOL, GlyphSweepResult, sweep_glyph_directions
from plt_optimizer.core.models import (
    ArcSegment,
    Coordinate,
    PLTDocument,
    Segment,
    StrokePath,
    StrokeSegment,
)
from plt_optimizer.core.reassembler import Reassembler

Point = Tuple[float, float]


def _stroke(start: Point, end: Point) -> StrokePath:
    """One straight path with pen-up at its own start (generate-path shape).

    Args:
        start: Segment start (also the pen-up position).
        end: Segment end.

    Returns:
        The path.
    """
    a = Coordinate(*start)
    b = Coordinate(*end)
    return StrokePath(pen_up_position=a, segments=(StrokeSegment(a, b, True),))


def _multi_point_path(points: Sequence[Point]) -> StrokePath:
    """One path chaining ``points`` tip-to-tail, pen-up at the first vertex.

    Args:
        points: Vertices in tracing order.

    Returns:
        The path.
    """
    coords = [Coordinate(*p) for p in points]
    segments = [StrokeSegment(coords[i], coords[i + 1], True) for i in range(len(coords) - 1)]
    return StrokePath(pen_up_position=coords[0], segments=tuple(segments))


def _gap(left: StrokePath, left_reversed: bool, right: StrokePath, right_reversed: bool) -> float:
    """Rapid travel between two consecutive traversal states.

    Args:
        left: The earlier path.
        left_reversed: Whether the earlier path is traced backwards.
        right: The later path.
        right_reversed: Whether the later path is traced backwards.

    Returns:
        Distance from the earlier path's exit to the later path's entry.
    """
    exit_ = left.segments[0].start if left_reversed else left.segments[-1].end
    entry = right.segments[-1].end if right_reversed else right.segments[0].start
    return exit_.distance_to(entry)


def _intra_travel(paths: Sequence[StrokePath]) -> float:
    """Sum the rapid travel between consecutive paths of an emission.

    Args:
        paths: Emitted paths in order.

    Returns:
        Total pen-up travel between consecutive paths.
    """
    return PLTDocument(stroke_paths=list(paths)).rapid_distance()


# A glyph whose exit is far from the next glyph's forward entry, so reversing
# it shortens both adjacent gaps: A ends at x=1, B spans x=2..1.2 (traced
# right-to-left), C starts at x=3.
#   forward:  1 -> 2 (1.0) then 1.2 -> 3 (1.8)  = 2.8
#   flip B:   1 -> 1.2 (0.2) then 2 -> 3 (1.0)  = 1.2
def _flip_fixture() -> Tuple[List[StrokePath], List[Tuple[int, ...]]]:
    """Three single-stroke glyphs where the middle glyph wants reversing."""
    paths = [
        _stroke((0.0, 0.0), (1.0, 0.0)),
        _stroke((2.0, 0.0), (1.2, 0.0)),
        _stroke((3.0, 0.0), (4.0, 0.0)),
    ]
    return paths, [(0,), (1,), (2,)]


class TestSweepGlyphDirections:
    """Core DP behaviour."""

    def test_reverses_the_glyph_that_shortens_both_gaps(self) -> None:
        """The middle glyph flips and the intra total drops 2.8 -> 1.2."""
        paths, groups = _flip_fixture()

        result = sweep_glyph_directions(paths, groups)

        assert isinstance(result, GlyphSweepResult)
        assert result.travel_before == 2.8
        assert result.travel_after == 1.2
        assert result.groups == 3
        assert result.flips == 1
        assert [(s.path_index, s.reversed) for s in result.result.traverse_order] == [
            (0, False),
            (1, True),
            (2, False),
        ]
        assert result.result.total_internal_distance == 1.2

    def test_reversed_state_carries_effective_endpoints(self) -> None:
        """A reversed state enters at the path's end and exits at its start."""
        paths, groups = _flip_fixture()

        states = sweep_glyph_directions(paths, groups).result.traverse_order

        flipped = states[1]
        assert flipped.entrance == Coordinate(1.2, 0.0)
        assert flipped.exit == Coordinate(2.0, 0.0)

    def test_endpoints_stay_pinned(self) -> None:
        """The swept traversal starts and ends where the chunk did."""
        paths, groups = _flip_fixture()

        result = sweep_glyph_directions(paths, groups)
        states = result.result.traverse_order

        assert states[0].entrance == paths[0].segments[0].start
        assert states[-1].exit == paths[-1].segments[-1].end

    def test_no_improvement_is_a_silent_no_op(self) -> None:
        """An already-optimal chunk reports zero flips."""
        paths = [
            _stroke((0.0, 0.0), (1.0, 0.0)),
            _stroke((1.5, 0.0), (2.5, 0.0)),
            _stroke((3.0, 0.0), (4.0, 0.0)),
        ]

        result = sweep_glyph_directions(paths, [(0,), (1,), (2,)])

        assert result.flips == 0
        assert result.travel_before == result.travel_after == 1.0
        assert [(s.path_index, s.reversed) for s in result.result.traverse_order] == [
            (0, False),
            (1, False),
            (2, False),
        ]

    def test_tied_directions_stay_forward(self) -> None:
        """A glyph equidistant from its neighbour keeps the forward traversal.

        ``B`` spans (2,1)-(2,-1): both ends sit the same distance from ``A``'s
        exit, so forward and reversed cost exactly the same and the strict
        improvement gate rejects the flip.
        """
        paths = [
            _stroke((0.0, 0.0), (1.0, 0.0)),
            _stroke((2.0, 1.0), (2.0, -1.0)),
            _stroke((3.0, 0.0), (4.0, 0.0)),
        ]

        result = sweep_glyph_directions(paths, [(0,), (1,), (2,)])

        assert result.flips == 0
        assert result.travel_before == pytest.approx(2 * math.sqrt(2))
        assert result.travel_after == result.travel_before

    def test_dp_beats_the_greedy_left_to_right_choice(self) -> None:
        """The optimum needs a flip that the first gap alone does not reveal.

        Greedy from ``A``'s exit (0,0) keeps ``B`` forward (gap 1.0 < 10.05),
        which strands the traversal at (1,10) facing ``C``'s forward entry at
        (1.5,0). The DP reverses ``B`` for a strictly better total.
        """
        paths = [
            _stroke((-1.0, 0.0), (0.0, 0.0)),
            _stroke((1.0, 0.0), (1.0, 10.0)),
            _stroke((1.5, 0.0), (2.0, 0.0)),
        ]

        result = sweep_glyph_directions(paths, [(0,), (1,), (2,)])

        greedy = 1.0 + math.hypot(0.5, 10.0)  # forward B, then (1,10) -> (1.5,0)
        optimal = math.hypot(1.0, 10.0) + 0.5  # reversed B: (0,0) -> (1,10), (1,0) -> (1.5,0)
        assert result.travel_before == pytest.approx(greedy)
        assert result.travel_after == pytest.approx(optimal)
        assert result.travel_after < result.travel_before
        assert result.flips == 1

    def test_dp_matches_brute_force_on_a_five_glyph_chain(self) -> None:
        """The chain DP equals the exhaustive optimum over the middle glyphs."""
        paths = [
            _stroke((0.0, 0.0), (5.0, 5.0)),
            _stroke((0.0, 0.0), (5.0, 5.0)),
            _stroke((4.0, 5.0), (0.0, 0.0)),
            _stroke((1.0, 1.0), (4.0, 4.0)),
            _stroke((5.0, 5.0), (6.0, 6.0)),
        ]
        groups = [(0,), (1,), (2,), (3,), (4,)]

        def _travel(directions: Sequence[bool]) -> float:
            sequence = list(enumerate(directions))
            return sum(
                _gap(paths[left], rev_left, paths[right], rev_right)
                for (left, rev_left), (right, rev_right) in zip(sequence, sequence[1:])
            )

        brute = math.inf
        for bits in range(8):  # the middle three glyphs, endpoints pinned forward
            directions = [False, bool(bits & 1), bool(bits & 2), bool(bits & 4), False]
            brute = min(brute, _travel(directions))

        result = sweep_glyph_directions(paths, groups)

        assert result.travel_after == pytest.approx(brute)
        assert result.travel_after < result.travel_before
        assert result.flips >= 1
        order = [(s.path_index, s.reversed) for s in result.result.traverse_order]
        assert order[0] == (0, False)
        assert order[-1] == (4, False)

    def test_travel_after_never_exceeds_travel_before(self) -> None:
        """Monotonicity across a spread of hand-built chunks."""
        fixtures = [
            _flip_fixture(),
            (
                [
                    _stroke((0.0, 0.0), (1.0, 1.0)),
                    _stroke((0.5, 2.0), (3.0, 0.25)),
                    _stroke((1.0, 0.0), (2.0, 2.0)),
                ],
                [(0,), (1,), (2,)],
            ),
            (
                [
                    _multi_point_path([(0.0, 0.0), (1.0, 0.0), (0.5, 0.8)]),
                    _multi_point_path([(3.0, 0.5), (2.0, 0.1), (2.6, 0.9)]),
                    _stroke((4.0, 0.0), (5.0, 0.0)),
                ],
                [(0,), (1,), (2,)],
            ),
        ]
        for paths, groups in fixtures:
            result = sweep_glyph_directions(paths, groups)
            assert result.travel_after <= result.travel_before + DEFAULT_TOL


class TestDegenerateInputs:
    """Nothing to sweep -> identity result, never an exception."""

    def test_no_groups(self) -> None:
        """An empty partition yields the chronological traversal."""
        paths, _groups = _flip_fixture()

        result = sweep_glyph_directions(paths, [])

        assert result.flips == 0
        assert result.groups == 0
        assert result.travel_before == result.travel_after == 2.8

    def test_single_group(self) -> None:
        """One glyph cannot be reordered against itself."""
        paths, _groups = _flip_fixture()

        result = sweep_glyph_directions(paths, [(0, 1, 2)])

        assert result.flips == 0
        assert result.groups == 1

    def test_empty_paths(self) -> None:
        """A chunk with no paths yields an empty result."""
        result = sweep_glyph_directions([], [])

        assert result.result.path_count == 0
        assert result.result.total_internal_distance == 0.0
        assert result.travel_before == result.travel_after == 0.0
        assert result.flips == 0

    def test_groups_not_owning_the_chunk_ends(self) -> None:
        """Groups that miss the first/last path pin nothing, so nothing flips.

        Reversing a group that owns the chunk's first path would move the
        block entrance (and therefore the inter-chunk tour), which the sweep
        is not allowed to do.
        """
        paths = [
            _stroke((0.0, 0.0), (1.0, 0.0)),
            _stroke((9.0, 0.0), (2.0, 0.0)),
            _stroke((3.0, 0.0), (4.0, 0.0)),
        ]

        result = sweep_glyph_directions(paths, [(1,), (2,)])

        assert result.flips == 0
        assert result.groups == 2
        assert result.travel_before == result.travel_after

    def test_empty_and_out_of_range_indices_are_dropped(self) -> None:
        """Index hygiene: segment-less paths and bad indices never reach the DP."""
        paths = [
            StrokePath(pen_up_position=Coordinate(0.0, 0.0), segments=()),
            _stroke((0.0, 0.0), (1.0, 0.0)),
            _stroke((2.0, 0.0), (1.2, 0.0)),
            _stroke((3.0, 0.0), (4.0, 0.0)),
        ]

        result = sweep_glyph_directions(paths, [(-5, 99), (1,), (2,), (3,)])

        assert result.groups == 3
        assert result.flips == 1
        assert all(state.path_index != 0 for state in result.result.traverse_order)

    def test_out_of_order_group_pins_the_last_path_owner(self) -> None:
        """A non-contiguous partition pins the group that owns the last path.

        ``groups[1]`` owns the chunk's final path, so it is forced forward
        even though it is not the final group; the sweep stays valid.
        """
        paths = [
            _stroke((0.0, 0.0), (1.0, 0.0)),
            _stroke((3.0, 0.0), (4.0, 0.0)),
            _stroke((2.0, 0.0), (1.5, 0.0)),
        ]

        result = sweep_glyph_directions(paths, [(0,), (2,), (1,)])

        assert result.groups == 3
        last_owner = next(state for state in result.result.traverse_order if state.path_index == 2)
        assert last_owner.reversed is False


class TestUngroupedPaths:
    """Paths outside every glyph group keep their chronological slot."""

    def test_ungrouped_path_between_groups_is_traversed_forward(self) -> None:
        """The fixed chain between two groups is measured, not skipped."""
        paths = [
            _stroke((0.0, 0.0), (1.0, 0.0)),
            _stroke((3.0, 0.0), (2.0, 0.0)),
            _stroke((2.0, 2.0), (3.0, 2.0)),  # ungrouped, between the groups
            _stroke((3.0, 3.0), (4.0, 3.0)),
        ]
        groups = [(0,), (1,), (3,)]

        result = sweep_glyph_directions(paths, groups)

        order = [(s.path_index, s.reversed) for s in result.result.traverse_order]
        assert order == [(0, False), (1, True), (2, False), (3, False)]
        assert result.flips == 1
        # Flipped gaps: (1,0) -> (2,0) = 1.0, (3,0) -> chain (2,2) = sqrt(5),
        # chain exit (3,2) -> (3,3) = 1.0. The chain's own length is cutting
        # travel, not rapid travel, so it contributes nothing.
        assert result.travel_after == pytest.approx(1.0 + math.sqrt(5.0) + 1.0)

    def test_travel_before_includes_the_chain(self) -> None:
        """The baseline measures the chronological emission, chains included."""
        paths = [
            _stroke((0.0, 0.0), (1.0, 0.0)),
            _stroke((3.0, 0.0), (2.0, 0.0)),
            _stroke((2.0, 2.0), (3.0, 2.0)),
            _stroke((3.0, 3.0), (4.0, 3.0)),
        ]

        result = sweep_glyph_directions(paths, [(0,), (1,), (3,)])

        # Chronological: (1,0) -> (3,0) = 2.0, (2,0) -> (2,2) = 2.0,
        # (3,2) -> (3,3) = 1.0.
        assert result.travel_before == pytest.approx(5.0)


class TestMultiStrokeGlyphs:
    """A glyph's strokes flip as one unit, in reversed order."""

    def test_reversed_group_emits_its_paths_reversed(self) -> None:
        """Reversing a two-stroke glyph reverses the stroke order too.

        ``B``'s strokes run right-to-left (x=5 then x=0) while ``A`` exits at
        its left end and ``C`` enters at its right end, so the reversed
        traversal saves four units on each side.
        """
        paths = [
            _stroke((-1.0, 1.0), (0.0, 1.0)),
            _stroke((5.0, 0.0), (5.0, 1.0)),
            _stroke((0.0, 0.0), (0.0, 1.0)),
            _stroke((5.0, 0.0), (6.0, 0.0)),
        ]

        result = sweep_glyph_directions(paths, [(0,), (1, 2), (3,)])

        assert result.flips == 1
        order = [(s.path_index, s.reversed) for s in result.result.traverse_order]
        assert order == [(0, False), (2, True), (1, True), (3, False)]
        assert result.travel_before == pytest.approx(3 * math.hypot(5.0, 1.0))
        assert result.travel_after == pytest.approx(math.hypot(5.0, 1.0))

    def test_intra_group_travel_is_reversal_invariant(self) -> None:
        """A glyph's internal gaps are identical forward and reversed.

        This is the lemma that makes the sweep's gain flow one-for-one into
        the emitted rapid travel: reversing the stroke sequence permutes the
        same consecutive endpoint pairs.
        """
        paths = [
            _stroke((0.0, 0.0), (1.0, 0.0)),
            _multi_point_path([(2.0, 0.0), (3.0, 1.0), (2.2, 0.4), (4.0, 0.2)]),
            _stroke((5.0, 0.0), (6.0, 0.0)),
        ]
        glyph = paths[1]
        forward = sum(
            glyph.segments[i].end.distance_to(glyph.segments[i + 1].start)
            for i in range(len(glyph.segments) - 1)
        )
        reversed_gaps = sum(
            glyph.segments[i + 1].start.distance_to(glyph.segments[i].end)
            for i in range(len(glyph.segments) - 1)
        )

        assert forward == pytest.approx(reversed_gaps)

    def test_travel_after_matches_the_emitted_intra_travel(self) -> None:
        """The reported objective is what the reassembler actually emits."""
        paths = [
            _stroke((-1.0, 1.0), (0.0, 1.0)),
            _stroke((5.0, 0.0), (5.0, 1.0)),
            _stroke((0.0, 0.0), (0.0, 1.0)),
            _stroke((5.0, 0.0), (6.0, 0.0)),
        ]
        groups = [(0,), (1, 2), (3,)]

        result = sweep_glyph_directions(paths, groups)
        assert result.flips == 1
        emitted = Reassembler()._apply_intra_chunk_order(tuple(paths), result.result)

        assert _intra_travel(emitted) == pytest.approx(result.travel_after)


class TestArcSafety:
    """Reversing a glyph containing arcs negates the sweep angle."""

    def test_arc_glyph_reversal_negates_sweep(self) -> None:
        """The emitted arc keeps its radius and flips direction."""
        arc = StrokePath(
            pen_up_position=Coordinate(2.0, 0.0),
            segments=(
                ArcSegment(
                    start=Coordinate(2.0, 0.0),
                    end=Coordinate(1.2, 0.0),
                    center=Coordinate(1.6, 0.0),
                    sweep_angle=180.0,
                    is_cutting=True,
                ),
            ),
        )
        paths = [_stroke((0.0, 0.0), (1.0, 0.0)), arc, _stroke((3.0, 0.0), (4.0, 0.0))]

        result = sweep_glyph_directions(paths, [(0,), (1,), (2,)])
        assert result.flips == 1
        emitted = Reassembler()._apply_intra_chunk_order(tuple(paths), result.result)

        arcs: List[Segment] = [seg for path in emitted for seg in path.segments]
        sweeps = [seg.sweep_angle for seg in arcs if isinstance(seg, ArcSegment)]
        assert sweeps and all(math.isclose(abs(s), 180.0) for s in sweeps)
        assert any(math.isclose(s, -180.0) for s in sweeps)
