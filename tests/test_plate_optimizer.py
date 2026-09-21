"""Unit tests for the plate-space toolpath optimizer.

Covers the coordinate transform chain, chunk-record -> MacroBlock
construction, integer-unit HPGL emission (pen grouping, arcs, reversals),
and the text/structural optimization entry points.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np
import pytest

from plt_optimizer.core.models import ArcSegment, Coordinate, StrokePath, StrokeSegment
from plt_optimizer.core.optimizer import NearestNeighbor2OptStrategy
from plt_optimizer.generate.label_renderer import (
    LAYER_BOUNDARY,
    LAYER_HOLES,
    RenderedLabel,
    TextChunkRecord,
)
from plt_optimizer.generate.layout import PackedLabel
from plt_optimizer.generate.plate_optimizer import (
    _label_transform,
    _rapid_distance,
    _structural_pen_of,
    _transform_point,
    build_text_blocks,
    emit_layer_document,
    optimize_structural_layer,
    optimize_text_layer,
)
from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine


def _make_label(height: float = 1.0) -> ResolvedLabel:
    """Build a minimal resolved label for transform tests.

    Args:
        height: Nominal label height in inches.

    Returns:
        A text-only ResolvedLabel.
    """
    return ResolvedLabel(
        id="t",
        count=1,
        width=3.0,
        height=height,
        margin=0.1,
        hole_margin=0.1,
        content=[
            ResolvedTextLine(
                text="AB",
                nominal_text_height=0.4,
                toolpath_text_height=0.38,
                cutter_diameter=0.02,
                character_spacing=0.03,
                line_spacing=0.1,
            )
        ],
    )


def _make_rendered(
    chunks: Tuple[TextChunkRecord, ...],
    x_min: float = 0.0,
    y_min: float = 0.0,
    x_max: float = 3.0,
    y_max: float = 1.0,
    label: Optional[ResolvedLabel] = None,
) -> RenderedLabel:
    """Build a RenderedLabel carrying the given chunk records.

    Args:
        chunks: Chunk records to attach.
        x_min: Rendered minimum X in inches.
        y_min: Rendered minimum Y in inches.
        x_max: Rendered maximum X in inches.
        y_max: Rendered maximum Y in inches.
        label: Source label (defaults to :func:`_make_label`).

    Returns:
        A RenderedLabel for plate-space tests.
    """
    return RenderedLabel(
        source_label=label or _make_label(),
        plt_content="IN;DF;PS0;%",
        x_min=x_min,
        y_min=y_min,
        x_max=x_max,
        y_max=y_max,
        width=x_max - x_min,
        height=y_max - y_min,
        text_chunks=chunks,
    )


def _line_record(
    points: List[Tuple[float, float]],
    pen: int = 1,
    line_index: int = 0,
) -> TextChunkRecord:
    """Build a whole-line chunk record from inch coordinates.

    Args:
        points: Polyline vertices in label-local inches.
        pen: Pen number for the record.
        line_index: Source line index.

    Returns:
        A TextChunkRecord with one contour.
    """
    contour = np.array([complex(x, y) for x, y in points])
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return TextChunkRecord(
        line_index=line_index,
        word_index=None,
        word_text="",
        pen=pen,
        contours=(contour,),
        bounds=(min(xs), min(ys), max(xs), max(ys)),
    )


def _packed(rendered_id: str, x: float, y: float, rotated: bool = False) -> PackedLabel:
    """Build a PackedLabel referencing a resolved label id.

    Args:
        rendered_id: Source label id.
        x: Slot X in inches.
        y: Slot Y in inches.
        rotated: Whether the packer placed the label sideways.

    Returns:
        A PackedLabel for tests.
    """
    return PackedLabel(
        label_id=rendered_id,
        x=x,
        y=y,
        width=3.0,
        height=1.0,
        rotated=rotated,
        source_label=_make_label(),
    )


def _fast_strategy(baseline_distance: float) -> NearestNeighbor2OptStrategy:
    """Fast-mode strategy factory used across tests."""
    return NearestNeighbor2OptStrategy()


class TestTransformChain:
    """Label-local inches -> device plotter units transform."""

    def test_matches_export_chain(self) -> None:
        """Centering + mirror reproduce the emitted-file convention."""
        label = _make_label(height=1.0)
        # Text block spanning y in [-0.19, 0.19] (center 0), like a real render.
        record = _line_record([(0.5, -0.19), (2.5, 0.19)])
        rendered = _make_rendered((record,), label=label)
        transform = _label_transform(rendered, label)

        # shift = h*500 - center = 500.0 - 0.0
        assert transform.shift == pytest.approx(500.0)
        # flip span from the rendered bounds (device units).
        assert transform.flip_span == 1000

        x, y = _transform_point(transform, 0.5, -0.19, rotated=False, dx_units=0, dy_units=0)
        # y1 = -190; y2 = round(-190 + 500) = 310; y3 = 1000 - 310 = 690.
        assert (x, y) == (500, 690)

    def test_rotation_maps_points(self) -> None:
        """Rotated labels map (x, y) -> (y_max - y, x - x_min) + slot."""
        label = _make_label(height=1.0)
        record = _line_record([(0.5, -0.19), (2.5, 0.19)])
        rendered = _make_rendered(
            (record,), x_min=0.0, y_min=0.0, x_max=3.0, y_max=1.0, label=label
        )
        transform = _label_transform(rendered, label)

        x, y = _transform_point(transform, 0.5, -0.19, rotated=True, dx_units=10, dy_units=20)
        # Unrotated device point is (500, 690); rotation about (x_min=0, y_max=1000):
        # (1000 - 690, 500 - 0) = (310, 500); plus slot (10, 20).
        assert (x, y) == (320, 520)

    def test_shift_uses_union_of_all_chunks(self) -> None:
        """The centering shift spans every text-pen vertex of the label."""
        label = _make_label(height=2.0)
        records = (
            _line_record([(0.0, 0.0), (1.0, 0.0)], pen=1),
            _line_record([(0.0, 1.0), (1.0, 1.0)], pen=4, line_index=1),
        )
        rendered = _make_rendered(records, y_max=2.0, label=label)
        transform = _label_transform(rendered, label)
        # Union y in [0, 1000] -> center 500; expected = h*500 = 1000.
        assert transform.shift == pytest.approx(500.0)

    def test_no_chunks_yields_zero_shift(self) -> None:
        """Labels without text skip centering."""
        label = _make_label()
        rendered = _make_rendered((), label=label)
        transform = _label_transform(rendered, label)
        assert transform.shift == pytest.approx(label.height * 500.0)


class TestBuildTextBlocks:
    """Chunk-record -> MacroBlock node construction."""

    def test_one_block_per_matching_chunk(self) -> None:
        """Only chunks on the requested pen become blocks."""
        label = _make_label()
        records = (
            _line_record([(0.0, 0.0), (1.0, 0.0)], pen=1),
            _line_record([(0.0, 0.5), (1.0, 0.5)], pen=4, line_index=1),
        )
        rendered = _make_rendered(records, label=label)
        cache = {"t": rendered}

        blocks = build_text_blocks([_packed("t", 0.0, 0.0)], cache, pen=1)
        assert len(blocks) == 1
        block = blocks[0]
        assert block.block_id == 0
        # Chunk ys span [0, 500] -> center 250; shift = 500 - 250 = 250.
        # Entrance (0, 0)in -> y1=0, y2=250, flip span 1000 -> y3=750.
        assert block.entrance.x == 0.0 and block.entrance.y == 750.0
        assert block.exit.x == 1000.0 and block.exit.y == 750.0

    def test_skips_unknown_and_empty_labels(self) -> None:
        """Missing cache entries and chunk-less labels contribute nothing."""
        rendered = _make_rendered(())
        cache = {"t": rendered}
        packed = [_packed("missing", 0.0, 0.0), _packed("t", 5.0, 0.0)]
        assert build_text_blocks(packed, cache, pen=1) == []

    def test_degenerate_chunks_skipped(self) -> None:
        """Chunks whose contours carry no segments produce no block."""
        label = _make_label()
        single = TextChunkRecord(
            line_index=0,
            word_index=None,
            word_text="",
            pen=1,
            contours=(np.array([complex(0.5, 0.0)]),),
            bounds=(0.5, 0.0, 0.5, 0.0),
        )
        rendered = _make_rendered((single,), label=label)
        blocks = build_text_blocks([_packed("t", 0.0, 0.0)], {"t": rendered}, pen=1)
        assert blocks == []

    def test_block_ids_sequential_across_labels(self) -> None:
        """Plate order is preserved and ids stay contiguous."""
        label = _make_label()
        rendered = _make_rendered(
            (
                _line_record([(0.0, 0.0), (1.0, 0.0)]),
                _line_record([(0.0, 0.2), (1.0, 0.2)], line_index=1),
            ),
            label=label,
        )
        cache = {"t": rendered}
        packed = [_packed("t", 0.0, 0.0), _packed("t", 4.0, 0.0)]
        blocks = build_text_blocks(packed, cache, pen=1)
        assert [b.block_id for b in blocks] == [0, 1, 2, 3]


class TestEmitLayerDocument:
    """Integer-unit HPGL emission with pen grouping."""

    def _path(self, points: List[Tuple[int, int]]) -> StrokePath:
        start = Coordinate(float(points[0][0]), float(points[0][1]))
        segments = [
            StrokeSegment(
                start=Coordinate(float(a[0]), float(a[1])),
                end=Coordinate(float(b[0]), float(b[1])),
                is_cutting=True,
            )
            for a, b in zip(points, points[1:])
        ]
        return StrokePath(pen_up_position=start, segments=tuple(segments))

    def test_constant_pen_emits_single_sp(self) -> None:
        """A constant pen emits exactly one SP for all paths."""
        paths = [self._path([(0, 0), (10, 0)]), self._path([(5, 5), (15, 5)])]
        content = emit_layer_document(paths, lambda path: 1)
        assert content == "IN;DF;PS0;SP1;PU0,0;PD10,0;PU5,5;PD15,5;SP0;IN;%"

    def test_pen_switch_between_paths(self) -> None:
        """A pen change between paths re-emits SP."""
        paths = [self._path([(0, 0), (10, 0)]), self._path([(5, 5), (15, 5)])]
        content = emit_layer_document(paths, lambda path: 2 if path is paths[0] else 3)
        assert content == "IN;DF;PS0;SP2;PU0,0;PD10,0;SP3;PU5,5;PD15,5;SP0;IN;%"

    def test_arc_segments_emit_aa(self) -> None:
        """Arc segments become PD;AA commands with integer fields."""
        start = Coordinate(100.0, 100.0)
        end = Coordinate(0.0, 100.0)
        arc = ArcSegment(
            start=start,
            end=end,
            center=Coordinate(50.0, 100.0),
            sweep_angle=-90.0,
            is_cutting=True,
        )
        path = StrokePath(pen_up_position=start, segments=(arc,))
        content = emit_layer_document([path], lambda _p: 3)
        assert "SP3;PU100,100;PD;AA50,100,-90;" in content

    def test_degenerate_paths_skipped(self) -> None:
        """Segment-less paths emit nothing (but do not crash)."""
        content = emit_layer_document([StrokePath()], lambda _p: 1)
        assert content == "IN;DF;PS0;SP0;IN;%"


class TestStructuralPenOf:
    """Pen attribution for borders vs. drill holes."""

    def test_arc_path_is_hole(self) -> None:
        start = Coordinate(0.0, 0.0)
        arc = ArcSegment(
            start=start,
            end=Coordinate(10.0, 0.0),
            center=Coordinate(5.0, 0.0),
            sweep_angle=-90.0,
            is_cutting=True,
        )
        path = StrokePath(pen_up_position=start, segments=(arc,))
        assert _structural_pen_of(path) == LAYER_HOLES

    def test_line_path_is_boundary(self) -> None:
        start = Coordinate(0.0, 0.0)
        seg = StrokeSegment(start=start, end=Coordinate(10.0, 0.0), is_cutting=True)
        path = StrokePath(pen_up_position=start, segments=(seg,))
        assert _structural_pen_of(path) == LAYER_BOUNDARY


class TestRapidDistance:
    """Baseline rapid-travel metric."""

    def test_sums_penup_gaps(self) -> None:
        path_a = StrokePath(
            pen_up_position=Coordinate(0.0, 0.0),
            segments=(
                StrokeSegment(
                    start=Coordinate(0.0, 0.0),
                    end=Coordinate(3.0, 4.0),
                    is_cutting=True,
                ),
            ),
        )
        path_b = StrokePath(
            pen_up_position=Coordinate(3.0, 4.0),
            segments=(
                StrokeSegment(
                    start=Coordinate(3.0, 4.0),
                    end=Coordinate(3.0, 4.0),
                    is_cutting=True,
                ),
            ),
        )
        assert _rapid_distance([path_a, path_b]) == pytest.approx(0.0)
        path_b2 = StrokePath(
            pen_up_position=Coordinate(0.0, 0.0),
            segments=path_b.segments,
        )
        assert _rapid_distance([path_a, path_b2]) == pytest.approx(5.0)


class TestOptimizeTextLayer:
    """Text-layer routing entry point."""

    def test_empty_layer_returns_none(self) -> None:
        assert optimize_text_layer([], {}, pen=1, strategy_factory=_fast_strategy) is None

    def test_routes_and_preserves_geometry(self) -> None:
        label = _make_label()
        # Two far-apart lines; the optimizer may reorder them.
        records = (
            _line_record([(0.0, 0.0), (1.0, 0.0)]),
            _line_record([(2.0, 0.4), (2.8, 0.4)], line_index=1),
        )
        rendered = _make_rendered(records, label=label)
        result = optimize_text_layer(
            [_packed("t", 0.0, 0.0)], {"t": rendered}, pen=1, strategy_factory=_fast_strategy
        )
        assert result is not None
        assert result.node_count == 2
        assert result.content.startswith("IN;DF;PS0;SP1;")
        assert result.content.endswith("SP0;IN;%")
        # Every optimized stroke must exist in the unoptimized layer.
        from plt_optimizer.core.parser import PLTParser

        raw = emit_layer_document(
            [
                path
                for block in build_text_blocks([_packed("t", 0.0, 0.0)], {"t": rendered}, 1)
                for path in block.paths
            ],
            lambda _p: 1,
        )
        raw_doc = PLTParser().parse_string(raw)
        opt_doc = PLTParser().parse_string(result.content)
        raw_segs = sorted(
            (round(s.start.x), round(s.start.y), round(s.end.x), round(s.end.y))
            for p in raw_doc.stroke_paths
            for s in p.segments
        )
        opt_segs = sorted(
            (round(s.start.x), round(s.start.y), round(s.end.x), round(s.end.y))
            for p in opt_doc.stroke_paths
            for s in p.segments
        )

        # Reversals flip direction; compare undirected.
        def undirected(segs: list) -> list:
            return sorted(
                (min(s[0], s[2]), min(s[1], s[3]), max(s[0], s[2]), max(s[1], s[3])) for s in segs
            )

        assert undirected(opt_segs) == undirected(raw_segs)
        assert result.outcome.optimized_distance <= result.baseline_distance + 1e-6


class TestOptimizeStructuralLayer:
    """Borders+holes routing entry point."""

    def test_empty_content_returns_none(self) -> None:
        assert optimize_structural_layer("IN;DF;PS0;SP2;SP0;IN;%", _fast_strategy) is None

    def test_deduplicates_coincident_borders(self) -> None:
        # Two labels sharing the edge x=1000 (duplicated stroke).
        content = (
            "IN;DF;PS0;SP2;PU0,0;PD1000,0,1000,1000,0,1000,0,0;"
            "PU1000,0;PD2000,0,2000,1000,1000,1000,1000,0;"
            "SP0;IN;%"
        )
        result = optimize_structural_layer(content, _fast_strategy)
        assert result is not None
        # The shared edge collapses to one stroke: 7 unique segments.
        assert result.node_count <= 8
        assert "SP2;" in result.content
        assert result.content.endswith("SP0;IN;%")

    def test_holes_keep_arc_geometry(self) -> None:
        content = (
            "IN;DF;PS0;SP2;PU0,0;PD1000,0;SP3;PU100,100;PD100,100;"
            "AA50,100,-90;AA50,100,-90;AA50,100,-90;AA50,100,-90;SP0;IN;%"
        )
        result = optimize_structural_layer(content, _fast_strategy)
        assert result is not None
        assert "SP3;" in result.content
        assert "AA50,100,-90" in result.content
