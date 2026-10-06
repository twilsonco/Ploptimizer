"""Unit tests for the post-TSP chunk direction sweep (core.direction_sweep).

Covers the greedy forward/backward sweeps, the fixpoint iteration, the
non-increasing guarantee on the inter-chunk objective, and the two properties
that make the sweep safe to run unconditionally:

* the tour order and block set are preserved (only directions change), and
* whole-block reversal is intra-chunk travel invariant, so the *emitted*
  rapid travel is non-increasing too (the generate-path guarantee).
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

from plt_optimizer.core.chunker import MacroBlock
from plt_optimizer.core.direction_sweep import (
    DirectionSweepResult,
    _backward_pass,
    _canonical_state,
    _forward_pass,
    sweep_tour_directions,
)
from plt_optimizer.core.models import (
    ArcSegment,
    Coordinate,
    PLTDocument,
    StrokePath,
    StrokeSegment,
)
from plt_optimizer.core.optimizer import (
    BlockTraverseState,
    NoOpStrategy,
    OptimizationResult,
    build_block_connections,
    sum_travel_distance,
)
from plt_optimizer.core.reassembler import Reassembler

Position = Tuple[float, float]


def _make_path(start: Position, end: Position) -> StrokePath:
    """Single-segment cutting path with pen_up anchored at its start."""
    s = Coordinate(x=start[0], y=start[1])
    e = Coordinate(x=end[0], y=end[1])
    return StrokePath(pen_up_position=s, segments=(StrokeSegment(start=s, end=e, is_cutting=True),))


def _make_block(block_id: int, start: Position, end: Position) -> MacroBlock:
    """Single-path block whose entrance/exit are the path endpoints."""
    path = _make_path(start, end)
    return MacroBlock(
        block_id=block_id,
        paths=(path,),
        entrance=path.segments[0].start,
        exit=path.segments[-1].end,
    )


def _make_multi_path_block(block_id: int, points: Sequence[Position]) -> MacroBlock:
    """Block of chained single-segment paths (one per consecutive pair).

    Models a real text chunk: several strokes whose rapid-travel gaps are all
    intra-chunk.
    """
    paths = tuple(_make_path(a, b) for a, b in zip(points, points[1:]))
    return MacroBlock(
        block_id=block_id,
        paths=paths,
        entrance=paths[0].segments[0].start,
        exit=paths[-1].segments[-1].end,
    )


def _make_gapped_block(block_id: int, spans: Sequence[Tuple[Position, Position]]) -> MacroBlock:
    """Block of strokes that do NOT chain tip-to-tail (pen_up = each start).

    Mirrors the generate path, where ``plate_optimizer._stroke_to_path`` sets
    every path's pen-up position to its own first vertex.
    """
    paths = tuple(_make_path(start, end) for start, end in spans)
    return MacroBlock(
        block_id=block_id,
        paths=paths,
        entrance=paths[0].segments[0].start,
        exit=paths[-1].segments[-1].end,
    )


def _make_result(
    blocks: Sequence[MacroBlock],
    reversals: Sequence[bool],
    initial_position: Position,
) -> OptimizationResult:
    """Chronological-order result with the given per-block directions."""
    traverse = tuple(
        _canonical_state(block, is_reversed) for block, is_reversed in zip(blocks, reversals)
    )
    connections = build_block_connections(list(blocks), list(traverse), initial_position)
    return OptimizationResult(
        traverse_order=traverse,
        connections=connections,
        total_travel_distance=sum_travel_distance(connections),
        initial_position=initial_position,
    )


class TestCanonicalState:
    """The BlockTraverseState field contract the reassembler consumes."""

    def test_forward_keeps_entrance_and_exit(self) -> None:
        block = _make_block(0, (1.0, 2.0), (3.0, 4.0))
        state = _canonical_state(block, is_reversed=False)
        assert state.reversed is False
        assert state.entrance == (1.0, 2.0)
        assert state.exit == (3.0, 4.0)

    def test_reversed_swaps_entrance_and_exit(self) -> None:
        block = _make_block(0, (1.0, 2.0), (3.0, 4.0))
        state = _canonical_state(block, is_reversed=True)
        assert state.reversed is True
        assert state.entrance == (3.0, 4.0)
        assert state.exit == (1.0, 2.0)


class TestSweepDirectionChoice:
    """Per-block direction selection."""

    def test_flips_when_exit_is_closer(self) -> None:
        # Block 1 sits so its exit is nearer block 0's exit than its entrance is.
        blocks = [
            _make_block(0, (0.0, 0.0), (10.0, 0.0)),
            _make_block(1, (100.0, 0.0), (11.0, 0.0)),
        ]
        result = _make_result(blocks, [False, False], initial_position=(0.0, 0.0))

        swept = sweep_tour_directions(blocks, result)

        assert swept.result.traverse_order[1].reversed is True
        assert swept.flips == 1
        assert swept.passes >= 1

    def test_tie_prefers_forward(self) -> None:
        # Entrance and exit equidistant from the predecessor exit.
        blocks = [
            _make_block(0, (0.0, 0.0), (10.0, 0.0)),
            _make_block(1, (15.0, 5.0), (15.0, -5.0)),
        ]
        result = _make_result(blocks, [False, False], initial_position=(0.0, 0.0))

        swept = sweep_tour_directions(blocks, result)

        assert swept.result.traverse_order[1].reversed is False

    def test_already_optimal_tour_is_untouched(self) -> None:
        blocks = [
            _make_block(0, (0.0, 0.0), (10.0, 0.0)),
            _make_block(1, (12.0, 0.0), (22.0, 0.0)),
            _make_block(2, (24.0, 0.0), (34.0, 0.0)),
        ]
        result = _make_result(blocks, [False, False, False], initial_position=(0.0, 0.0))

        swept = sweep_tour_directions(blocks, result)

        assert swept.passes == 0
        assert swept.flips == 0
        assert swept.result is result
        assert swept.travel_before == swept.travel_after

    def test_stale_flags_are_repaired(self) -> None:
        """The post-2-opt failure mode: order fixed, directions left stale."""
        blocks = [
            _make_block(0, (0.0, 0.0), (10.0, 0.0)),
            _make_block(1, (11.0, 0.0), (21.0, 0.0)),
            _make_block(2, (22.0, 0.0), (32.0, 0.0)),
        ]
        # Block 1 marked reversed: its recorded exit is block 1's *entrance*,
        # so the gap into block 2 becomes 21 units instead of 1.
        stale = _make_result(blocks, [False, True, False], initial_position=(0.0, 0.0))
        stale_total = sum_travel_distance(
            build_block_connections(blocks, list(stale.traverse_order), (0.0, 0.0))
        )

        swept = sweep_tour_directions(blocks, stale)

        assert swept.travel_after < stale_total
        assert swept.result.traverse_order[1].reversed is False


class TestSweepInvariants:
    """Properties that must hold for every sweep."""

    def _layouts(self) -> List[List[MacroBlock]]:
        row = [_make_block(i, (i * 10.0, 0.0), (i * 10.0 + 5.0, 0.0)) for i in range(6)]
        zigzag = [
            _make_block(0, (0.0, 0.0), (10.0, 0.0)),
            _make_block(1, (40.0, 30.0), (30.0, 30.0)),
            _make_block(2, (20.0, 60.0), (30.0, 60.0)),
            _make_block(3, (0.0, 90.0), (10.0, 90.0)),
        ]
        collinear = [_make_block(i, (i * 2.0, 0.0), (i * 2.0 + 1.0, 0.0)) for i in range(8)]
        return [row, zigzag, collinear]

    def test_never_increases_objective(self) -> None:
        for blocks in self._layouts():
            for reversals in ([False] * len(blocks), [True] * len(blocks)):
                result = _make_result(blocks, reversals, initial_position=(0.0, 0.0))
                swept = sweep_tour_directions(blocks, result)
                assert swept.travel_after <= swept.travel_before + 1e-9

    def test_order_and_block_set_preserved(self) -> None:
        for blocks in self._layouts():
            result = _make_result(blocks, [False] * len(blocks), initial_position=(0.0, 0.0))
            swept = sweep_tour_directions(blocks, result)
            assert [s.block_id for s in swept.result.traverse_order] == [
                s.block_id for s in result.traverse_order
            ]
            assert swept.result.initial_position == result.initial_position

    def test_total_matches_connections(self) -> None:
        for blocks in self._layouts():
            result = _make_result(blocks, [True] * len(blocks), initial_position=(0.0, 0.0))
            swept = sweep_tour_directions(blocks, result)
            assert swept.result.total_travel_distance == sum_travel_distance(
                swept.result.connections
            )

    def test_states_are_canonical(self) -> None:
        """Every emitted state carries the effective entry/exit for its flag."""
        for blocks in self._layouts():
            result = _make_result(blocks, [False] * len(blocks), initial_position=(0.0, 0.0))
            swept = sweep_tour_directions(blocks, result)
            by_id = {b.block_id: b for b in blocks}
            for state in swept.result.traverse_order:
                block = by_id[state.block_id]
                expected = _canonical_state(block, state.reversed)
                assert (state.entrance, state.exit) == (expected.entrance, expected.exit)

    def test_max_passes_cap_is_honored(self) -> None:
        blocks = [
            _make_block(0, (0.0, 0.0), (10.0, 0.0)),
            _make_block(1, (100.0, 0.0), (11.0, 0.0)),
        ]
        result = _make_result(blocks, [False, False], initial_position=(0.0, 0.0))
        assert sweep_tour_directions(blocks, result, max_passes=0).passes == 0
        assert sweep_tour_directions(blocks, result, max_passes=1).passes == 1

    def test_backward_sweeps_beat_forward_only(self) -> None:
        """The fixpoint iteration earns its keep on a staggered layout.

        Coordinates come from a random search over layouts where a single
        forward pass leaves the tour strictly worse than the iterated result.
        """
        blocks = [
            _make_block(0, (3.297241, 98.129974), (4.85898, 100.123202)),
            _make_block(1, (26.005621, 6.908525), (32.811147, 8.743509)),
            _make_block(2, (67.872396, 13.022445), (74.737453, 13.268184)),
            _make_block(3, (14.955033, 3.864157), (19.607564, 1.219174)),
        ]
        result = _make_result(blocks, [False, True, True, True], initial_position=(0.0, 0.0))
        by_id = {b.block_id: b for b in blocks}
        forward_only = sum_travel_distance(
            build_block_connections(
                blocks,
                _forward_pass(list(result.traverse_order), by_id),
                None,
            )
        )

        swept = sweep_tour_directions(blocks, result)

        assert swept.passes > 1
        assert swept.travel_after < forward_only

    def test_flips_counts_net_direction_changes(self) -> None:
        blocks = [
            _make_block(0, (0.0, 0.0), (10.0, 0.0)),
            _make_block(1, (100.0, 0.0), (11.0, 0.0)),
            _make_block(2, (200.0, 0.0), (12.0, 0.0)),
        ]
        result = _make_result(blocks, [False, False, False], initial_position=(0.0, 0.0))
        swept = sweep_tour_directions(blocks, result)
        assert swept.flips == sum(
            1
            for before, after in zip(result.traverse_order, swept.result.traverse_order)
            if before.reversed != after.reversed
        )


class TestSweepDegenerate:
    """Inputs the sweep must pass through untouched."""

    def test_empty_tour(self) -> None:
        result = OptimizationResult(
            traverse_order=(),
            connections=(),
            total_travel_distance=0.0,
            initial_position=None,
        )
        swept = sweep_tour_directions([], result)
        assert swept.result is result
        assert swept.passes == 0
        assert swept.flips == 0

    def test_single_block(self) -> None:
        blocks = [_make_block(0, (0.0, 0.0), (10.0, 0.0))]
        result = _make_result(blocks, [False], initial_position=(0.0, 0.0))
        swept = sweep_tour_directions(blocks, result)
        assert swept.result is result
        assert swept.passes == 0
        assert swept.travel_before == swept.travel_after == 0.0

    def test_unknown_block_id_is_left_alone(self) -> None:
        blocks = [_make_block(0, (0.0, 0.0), (10.0, 0.0))]
        result = OptimizationResult(
            traverse_order=(
                BlockTraverseState(
                    block_id=0, reversed=False, entrance=(0.0, 0.0), exit=(10.0, 0.0)
                ),
                BlockTraverseState(
                    block_id=99, reversed=False, entrance=(0.0, 0.0), exit=(1.0, 0.0)
                ),
            ),
            connections=(),
            total_travel_distance=42.0,
            initial_position=(0.0, 0.0),
        )
        swept = sweep_tour_directions(blocks, result)
        assert swept.result is result
        assert swept.passes == 0
        assert swept.travel_before == 42.0

    def test_initial_position_none_is_supported(self) -> None:
        blocks = [
            _make_block(0, (0.0, 0.0), (10.0, 0.0)),
            _make_block(1, (100.0, 0.0), (11.0, 0.0)),
        ]
        result = _make_result(blocks, [False, False], initial_position=(0.0, 0.0))
        result = OptimizationResult(
            traverse_order=result.traverse_order,
            connections=result.connections,
            total_travel_distance=result.total_travel_distance,
            initial_position=None,
        )
        swept = sweep_tour_directions(blocks, result)
        assert swept.travel_after <= swept.travel_before
        assert swept.result.initial_position is None


class TestSweepPasses:
    """The two sweep directions in isolation."""

    def test_forward_pass_ignores_head_incoming_gap(self) -> None:
        # The head's exit is far from block 1 while its entrance is right next
        # to it: the forward pass decides the head by its *outgoing* gap, so
        # the head is reversed even though entering at its entrance is free.
        blocks = [
            _make_block(0, (12.0, 0.0), (500.0, 500.0)),
            _make_block(1, (12.0, 0.0), (22.0, 0.0)),
        ]
        tour = [_canonical_state(b, False) for b in blocks]
        swept = _forward_pass(tour, {b.block_id: b for b in blocks})
        assert swept[0].reversed is True

    def test_backward_pass_decides_tail_last(self) -> None:
        blocks = [
            _make_block(0, (0.0, 0.0), (10.0, 0.0)),
            _make_block(1, (100.0, 0.0), (11.0, 0.0)),
        ]
        tour = [_canonical_state(b, False) for b in blocks]
        swept = _backward_pass(tour, {b.block_id: b for b in blocks})
        assert swept[0].reversed is False
        assert swept[1].reversed is True

    def test_single_state_passes_are_noops(self) -> None:
        blocks = [_make_block(0, (0.0, 0.0), (10.0, 0.0))]
        tour = [_canonical_state(blocks[0], False)]
        by_id = {b.block_id: b for b in blocks}
        assert [s.reversed for s in _forward_pass(tour, by_id)] == [False]
        assert [s.reversed for s in _backward_pass(tour, by_id)] == [False]


class TestReversalInvarianceLemma:
    """Why the *emitted* rapid travel is non-increasing in the generate path.

    A block's intra-chunk gaps are identical forward and reversed, because
    ``Reassembler._reverse_paths_simple`` re-anchors each reversed path's
    pen-up position to its new first segment. That makes the sweep's
    inter-chunk objective the only moving part of ``rapid_distance()``.
    """

    def test_intra_chunk_travel_is_reversal_invariant(self) -> None:
        # Gapped strokes (not chained tip-to-tail) so the intra-chunk travel is
        # non-zero and the equality is meaningful.
        block = _make_gapped_block(
            0,
            [((0.0, 0.0), (10.0, 0.0)), ((50.0, 0.0), (60.0, 0.0)), ((100.0, 10.0), (110.0, 10.0))],
        )
        reassembler = Reassembler()

        forward = reassembler._apply_intra_chunk_order(block.paths, None)
        reversed_paths = reassembler._reverse_block_paths(block.paths, None)

        forward_gap = sum(
            a.segments[-1].end.distance_to(b.pen_up_position) for a, b in zip(forward, forward[1:])
        )
        reversed_gap = sum(
            a.segments[-1].end.distance_to(b.pen_up_position)
            for a, b in zip(reversed_paths, reversed_paths[1:])
        )
        assert forward_gap > 0.0
        assert forward_gap == reversed_gap

    def test_emitted_rapid_travel_never_increases(self) -> None:
        """Sweeping a multi-stroke text layer cannot increase emitted travel."""
        blocks = [
            _make_multi_path_block(0, [(0.0, 0.0), (10.0, 0.0), (20.0, 0.0)]),
            _make_multi_path_block(1, [(90.0, 20.0), (80.0, 20.0), (70.0, 20.0)]),
            _make_multi_path_block(2, [(5.0, 40.0), (15.0, 40.0), (25.0, 40.0)]),
        ]
        result = _make_result(blocks, [False, False, False], initial_position=(0.0, 0.0))
        reassembler = Reassembler()
        document = PLTDocument(stroke_paths=[path for block in blocks for path in block.paths])

        def emitted(res: OptimizationResult) -> float:
            return reassembler.reassemble(document, blocks, res).rapid_distance()

        swept = sweep_tour_directions(blocks, result)
        assert emitted(swept.result) <= emitted(result) + 1e-9


class TestArcSafety:
    """Reversal through the reassembler keeps arcs geometrically valid."""

    def test_arc_sweep_is_negated(self) -> None:
        arc = ArcSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=50.0),
            center=Coordinate(x=50.0, y=0.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        path = StrokePath(pen_up_position=Coordinate(x=0.0, y=0.0), segments=(arc,))
        block = MacroBlock(
            block_id=0,
            paths=(path,),
            entrance=arc.start,
            exit=arc.end,
        )
        # Block 1 sits to the LEFT of the arc's entrance, so the sweep reverses
        # the arc block (entering at its geometric exit).
        blocks = [
            block,
            _make_block(1, (-1000.0, 0.0), (-1010.0, 0.0)),
        ]
        result = _make_result(blocks, [False, False], initial_position=(0.0, 0.0))

        swept = sweep_tour_directions(blocks, result)
        assert swept.result.traverse_order[0].reversed is True
        doc = Reassembler().reassemble(PLTDocument(stroke_paths=[path]), blocks, swept.result)

        arcs = [
            segment
            for stroke in doc.stroke_paths
            for segment in stroke.segments
            if isinstance(segment, ArcSegment)
        ]
        assert arcs, "arc must survive reassembly"
        assert all(arc.sweep_angle == -90.0 for arc in arcs)


class TestSweepResultShape:
    """The result contract the pipeline consumes."""

    def test_result_is_frozen_dataclass_with_expected_fields(self) -> None:
        blocks = [
            _make_block(0, (0.0, 0.0), (10.0, 0.0)),
            _make_block(1, (100.0, 0.0), (11.0, 0.0)),
        ]
        result = _make_result(blocks, [False, False], initial_position=(0.0, 0.0))
        swept = sweep_tour_directions(blocks, result)

        assert isinstance(swept, DirectionSweepResult)
        assert swept.travel_before > swept.travel_after
        assert swept.result.block_count == len(blocks)

    def test_composes_with_noop_strategy_output(self) -> None:
        """End-to-end against a real strategy's own result object."""
        blocks = [
            _make_block(0, (0.0, 0.0), (10.0, 0.0)),
            _make_block(1, (100.0, 0.0), (11.0, 0.0)),
            _make_block(2, (200.0, 0.0), (12.0, 0.0)),
        ]
        strategy_result = NoOpStrategy().optimize(blocks, initial_position=(0.0, 0.0))

        swept = sweep_tour_directions(blocks, strategy_result)

        assert swept.travel_after <= strategy_result.total_travel_distance
        assert [s.block_id for s in swept.result.traverse_order] == [0, 1, 2]
