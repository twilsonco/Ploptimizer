"""Unit tests for the plate-space toolpath optimizer.

Covers the coordinate transform chain, chunk-record -> MacroBlock
construction, integer-unit HPGL emission (pen grouping, arcs, reversals),
and the text/structural optimization entry points.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

import pytest

from plt_optimizer.core.models import ArcSegment, Coordinate, StrokePath, StrokeSegment
from plt_optimizer.core.optimizer import NearestNeighbor2OptStrategy
from plt_optimizer.generate.label_renderer import (
    RenderedLabel,
    TextChunkRecord,
    _collect_hpgl_geometry,
    _transform_hpgl_coordinates,
)
from plt_optimizer.generate.layout import PackedLabel
from plt_optimizer.generate.plate_optimizer import (
    _block_text_y_extents,
    _label_transform,
    _LabelTransform,
    _rapid_distance,
    _record_paths,
    _record_paths_with_glyphs,
    _stroke_to_path,
    _transform_point,
    build_text_blocks,
    build_text_blocks_with_glyphs,
    emit_layer_document,
    optimize_structural_layer,
    optimize_text_layer,
)
from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine
from plt_optimizer.generate.text_geometry import ArcSeg, LineSeg, Stroke, TextBlock


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
        h_margin=0.1,
        v_margin=0.1,
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
        plt_content="IN;PA;%",
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
        A TextChunkRecord with one polyline-only block.
    """
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return TextChunkRecord(
        line_index=line_index,
        word_index=None,
        word_text="",
        pen=pen,
        blocks=(block_from_points(points),),
        bounds=(min(xs), min(ys), max(xs), max(ys)),
    )


def _multi_stroke_record(
    spans: List[Tuple[Tuple[float, float], Tuple[float, float]]],
    pen: int = 1,
    line_index: int = 0,
) -> TextChunkRecord:
    """Build a chunk record of several *gapped* strokes (one per span).

    Models a real text chunk: each stroke is its own path with pen-up at its
    own first vertex, so the gaps between them are intra-chunk rapid travel.

    Args:
        spans: ``(start, end)`` inch pairs, one per stroke.
        pen: Pen number for the record.
        line_index: Source line index.

    Returns:
        A TextChunkRecord with one stroke per span.
    """
    strokes = tuple(
        Stroke(
            pen_up=complex(*start),
            segments=(LineSeg(complex(*start), complex(*end)),),
        )
        for start, end in spans
    )
    points = [complex(*span[i]) for span in spans for i in (0, 1)]
    return TextChunkRecord(
        line_index=line_index,
        word_index=None,
        word_text="",
        pen=pen,
        blocks=(TextBlock(strokes=strokes),),
        bounds=(
            min(p.real for p in points),
            min(p.imag for p in points),
            max(p.real for p in points),
            max(p.imag for p in points),
        ),
    )


def _glyph_record(
    spans: List[Tuple[Tuple[float, float], Tuple[float, float]]],
    glyph_groups: Tuple[Tuple[int, ...], ...],
    pen: int = 1,
    line_index: int = 0,
) -> TextChunkRecord:
    """Build a chunk record with one stroke per span plus a glyph partition.

    Args:
        spans: ``(start, end)`` inch pairs, one stroke per span.
        glyph_groups: Stroke-index groups (indices into ``spans``).
        pen: Pen number for the record.
        line_index: Source line index.

    Returns:
        A TextChunkRecord carrying ``glyph_groups`` verbatim.
    """
    record = _multi_stroke_record(spans, pen=pen, line_index=line_index)
    return TextChunkRecord(
        line_index=record.line_index,
        word_index=record.word_index,
        word_text=record.word_text,
        pen=record.pen,
        blocks=record.blocks,
        bounds=record.bounds,
        glyph_groups=glyph_groups,
    )


def block_from_points(points: List[Tuple[float, float]]) -> TextBlock:
    """Build a single-stroke polyline :class:`TextBlock` from inch vertices.

    Args:
        points: Polyline vertices in label-local inches (at least one).

    Returns:
        A TextBlock whose one stroke chains the points tip-to-tail (a lone
        point yields a stroke with no segments).
    """
    vertices = [complex(x, y) for x, y in points]
    segments = tuple(LineSeg(a, b) for a, b in zip(vertices[:-1], vertices[1:]))
    return TextBlock(strokes=(Stroke(pen_up=vertices[0], segments=segments),))


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


def _arc_record(
    segments: Tuple[ArcSeg, ...],
    pen_up: complex,
    pen: int = 1,
) -> TextChunkRecord:
    """Build a whole-line chunk record from one arc-aware stroke.

    Args:
        segments: The stroke's text segments (LineSeg/ArcSeg).
        pen_up: Stroke rapid target in label-local inches.
        pen: Pen number for the record.

    Returns:
        A TextChunkRecord with one arc-aware block.
    """
    block = TextBlock(strokes=(Stroke(pen_up=pen_up, segments=segments),))
    return TextChunkRecord(
        line_index=0,
        word_index=None,
        word_text="",
        pen=pen,
        blocks=(block,),
        bounds=(0.0, 0.0, 1.0, 1.0),
    )


class TestArcAwareYExtents:
    """``_block_text_y_extents`` mirrors the HPGL-side Y measurement."""

    def test_matches_hpgl_side_measurement(self) -> None:
        """A mixed line/arc block measures identically via both paths."""
        stroke = Stroke(
            pen_up=0.5 + 0.2j,
            segments=(
                LineSeg(0.6 + 0.3j, 0.8 + 0.7j),
                ArcSeg(start=0.9 + 0.4j, center=1.2 + 0.4j, sweep_deg=-90.5),
                ArcSeg(start=1.2 + 0.1j, center=1.2 + 0.4j, sweep_deg=45.25),
                LineSeg(1.5 + 0.2j, 1.6 + 0.9j),
            ),
        )
        block = TextBlock(strokes=(stroke,))
        local_ys = _block_text_y_extents(block)

        # The HPGL side: emit the stroke, then measure with the exact
        # collector the label renderer's centering uses.
        hpgl = stroke.to_hpgl()
        points, arcs = _collect_hpgl_geometry(hpgl)
        hpgl_ys = [y for _x, y in points]
        for arc in arcs:
            _x_min, y_min, _x_max, y_max = arc.swept_bounds()
            hpgl_ys.append(int(math.floor(y_min)))
            hpgl_ys.append(int(math.ceil(y_max)))

        assert sorted(local_ys) == sorted(hpgl_ys)

    def test_arc_measures_from_last_recorded_pen(self) -> None:
        """Chained arcs share the measured pen (it never advances across AA)."""
        # Arc starts deliberately away from the pen: to_hpgl re-anchors with
        # PU, and both measurements must see the re-anchored point.
        stroke = Stroke(
            pen_up=0.0 + 0.0j,
            segments=(ArcSeg(start=0.5 + 0.5j, center=0.5 + 0.0j, sweep_deg=180.0),),
        )
        block = TextBlock(strokes=(stroke,))
        local_ys = _block_text_y_extents(block)

        points, arcs = _collect_hpgl_geometry(stroke.to_hpgl())
        hpgl_ys = [y for _x, y in points]
        for arc in arcs:
            _x_min, y_min, _x_max, y_max = arc.swept_bounds()
            hpgl_ys.append(int(math.floor(y_min)))
            hpgl_ys.append(int(math.ceil(y_max)))

        # PU0,0 + PU500,500 recorded; the half-circle (center 500,0, r=500)
        # sweeps from 90 deg clockwise through 0 deg to -90 deg: y in [-500, 500].
        assert min(local_ys) == -500
        assert max(local_ys) == 500
        assert sorted(local_ys) == sorted(hpgl_ys)

    def test_line_run_start_away_from_pen_is_recorded(self) -> None:
        """A line run not starting at the pen records its run-start point."""
        stroke = Stroke(pen_up=0.0 + 0.0j, segments=(LineSeg(1 + 1j, 2 + 2j),))
        block = TextBlock(strokes=(stroke,))
        # PU(pen_up)=0, re-anchoring PU(1000,1000), PD end (2000,2000).
        assert _block_text_y_extents(block) == [0, 1000, 2000]

    def test_shift_with_arcs_matches_centering(self) -> None:
        """The arc-aware center keeps the transform shift aligned with export."""
        label = _make_label(height=1.0)
        # Arc sweeping y in [100, 700] device units (label-local y 100..700).
        record = _arc_record(
            (ArcSeg(start=0.5 + 0.1j, center=0.5 + 0.4j, sweep_deg=180.0),),
            pen_up=0.5 + 0.1j,
        )
        rendered = _make_rendered((record,), y_min=0.0, y_max=1.0, label=label)
        transform = _label_transform(rendered, label)
        # Union y in [100, 700] -> center 400; expected = 500 -> shift +100.
        assert transform.shift == pytest.approx(100.0)


class TestArcStrokeConversion:
    """Label-local arc strokes map to device ArcSegments exactly once mirrored."""

    _TRANSFORM = _LabelTransform(shift=0.0, flip_span=1000, rot_y_max=1000, rot_x_min=0)

    def _arc_stroke(self) -> Stroke:
        """One arc stroke: start (500,400), center (500,600), sweep +90."""
        return Stroke(
            pen_up=0.5 + 0.4j,
            segments=(ArcSeg(start=0.5 + 0.4j, center=0.5 + 0.6j, sweep_deg=90.0),),
        )

    def test_sweep_negated_and_end_derived(self) -> None:
        """Exactly one Y-mirror: start/center flip, sweep negates, end derives."""
        path = _stroke_to_path(self._TRANSFORM, self._arc_stroke(), False, 0, 0)
        assert len(path.segments) == 1
        arc = path.segments[0]
        assert isinstance(arc, ArcSegment)
        assert (arc.start.x, arc.start.y) == (500.0, 600.0)
        assert (arc.center.x, arc.center.y) == (500.0, 400.0)
        assert arc.sweep_angle == pytest.approx(-90.0)
        # Start sits due north of the center (radius 200); -90 deg lands due east.
        assert arc.end.x == pytest.approx(700.0, abs=1e-6)
        assert arc.end.y == pytest.approx(400.0, abs=1e-6)
        assert emit_layer_document([path]) == "IN;PA;PU500,600;PD;AA500,400,-90;SP;"

    def test_matches_hpgl_string_transform(self) -> None:
        """Object conversion equals _transform_hpgl_coordinates on the string."""
        # Chained stroke: the line run starts at the pen-up and the arc
        # starts at the line end, so the source HPGL carries no redundant
        # re-anchoring PUs and both paths emit the same minimal stream.
        stroke = Stroke(
            pen_up=0.5 + 0.2j,
            segments=(
                LineSeg(0.5 + 0.2j, 0.8 + 0.7j),
                ArcSeg(start=0.8 + 0.7j, center=1.2 + 0.4j, sweep_deg=-90.0),
            ),
        )
        transformed = _transform_hpgl_coordinates(
            stroke.to_hpgl(), flip_y_span=1000, flip_y_axis=True
        )
        path = _stroke_to_path(self._TRANSFORM, stroke, False, 0, 0)
        emitted = emit_layer_document([path])
        body = emitted[len("IN;PA;") : -len("SP;")]
        assert body == transformed + ";"

    def test_rotated_center_maps_through_rotation(self) -> None:
        """Rotated labels map arc centers with the 90 CW rotation too."""
        record = _arc_record(
            (ArcSeg(start=0.5 + 0.4j, center=0.5 + 0.6j, sweep_deg=90.0),),
            pen_up=0.5 + 0.4j,
        )
        paths = _record_paths(self._TRANSFORM, record, rotated=True, dx_units=10, dy_units=20)
        assert len(paths) == 1
        arc = paths[0].segments[0]
        assert isinstance(arc, ArcSegment)
        # Unrotated device start (500, 600) rotates to (1000-600, 500-0)=(400,500).
        assert (arc.start.x, arc.start.y) == (410.0, 520.0)
        # Unrotated center (500, 400) rotates to (1000-400, 500)=(600,500).
        assert (arc.center.x, arc.center.y) == pytest.approx((610.0, 520.0))
        assert arc.sweep_angle == pytest.approx(-90.0)


class TestRecordPathFiltering:
    """``_record_paths`` drops empty strokes and segment-less paths."""

    def test_empty_strokes_never_reach_paths(self) -> None:
        """A block mixing empty and real strokes yields only the real path."""
        record = TextChunkRecord(
            line_index=0,
            word_index=None,
            word_text="",
            pen=1,
            blocks=(
                TextBlock(
                    strokes=(
                        Stroke(pen_up=0.5 + 0.5j, segments=()),
                        Stroke(pen_up=0.0 + 0.0j, segments=(LineSeg(0j, 1 + 1j),)),
                    )
                ),
            ),
            bounds=(0.0, 0.0, 1.0, 1.0),
        )
        transform = _LabelTransform(shift=0.0, flip_span=1000, rot_y_max=1000, rot_x_min=0)
        paths = _record_paths(transform, record, rotated=False, dx_units=0, dy_units=0)
        assert len(paths) == 1
        assert len(paths[0].segments) == 1


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
        """Chunks whose blocks carry no segments produce no block."""
        label = _make_label()
        single = TextChunkRecord(
            line_index=0,
            word_index=None,
            word_text="",
            pen=1,
            blocks=(TextBlock(strokes=(Stroke(pen_up=complex(0.5, 0.0), segments=()),)),),
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


class TestRecordPathsWithGlyphs:
    """``_record_paths_with_glyphs`` remaps glyph groups onto surviving paths."""

    def _transform(self) -> _LabelTransform:
        return _LabelTransform(shift=0.0, flip_span=1000, rot_y_max=1000, rot_x_min=0)

    def test_groups_remap_onto_path_indices(self) -> None:
        """Contiguous stroke groups map 1:1 when nothing is dropped."""
        record = _glyph_record(
            [((0.0, 0.0), (1.0, 0.0)), ((2.0, 0.0), (3.0, 0.0)), ((4.0, 0.0), (5.0, 0.0))],
            glyph_groups=((0,), (1, 2)),
        )

        paths, groups = _record_paths_with_glyphs(
            self._transform(), record, rotated=False, dx_units=0, dy_units=0
        )

        assert len(paths) == 3
        assert groups == ((0,), (1, 2))

    def test_dropped_strokes_shrink_their_group(self) -> None:
        """An empty stroke drops out of its glyph group; empty groups vanish."""
        record = TextChunkRecord(
            line_index=0,
            word_index=None,
            word_text="",
            pen=1,
            blocks=(
                TextBlock(
                    strokes=(
                        Stroke(pen_up=0.5 + 0.5j, segments=()),  # stroke 0: empty
                        Stroke(pen_up=0.0 + 0.0j, segments=(LineSeg(0j, 1 + 0j),)),
                        Stroke(pen_up=2.0 + 0.0j, segments=(LineSeg(2 + 0j, 3 + 0j),)),
                    )
                ),
            ),
            bounds=(0.0, 0.0, 3.0, 0.0),
            glyph_groups=((0,), (1,), (2,)),
        )

        paths, groups = _record_paths_with_glyphs(
            self._transform(), record, rotated=False, dx_units=0, dy_units=0
        )

        assert len(paths) == 2
        # The empty stroke's group is gone; the survivors renumber to 0, 1.
        assert groups == ((0,), (1,))

    def test_record_without_groups_returns_empty_partition(self) -> None:
        """Records with no glyph knowledge keep the historical behaviour."""
        record = _multi_stroke_record([((0.0, 0.0), (1.0, 0.0)), ((2.0, 0.0), (3.0, 0.0))])

        paths, groups = _record_paths_with_glyphs(
            self._transform(), record, rotated=False, dx_units=0, dy_units=0
        )

        assert len(paths) == 2
        assert groups == ()


class TestBuildTextBlocksWithGlyphs:
    """``build_text_blocks_with_glyphs`` keys glyph groups by block id."""

    def test_only_blocks_with_groups_join_the_map(self) -> None:
        """A group-less chunk is absent from the map; ids stay aligned."""
        label = _make_label()
        records = (
            _line_record([(0.0, 0.0), (1.0, 0.0)]),  # block 0: no groups
            _glyph_record(
                [((0.0, 0.5), (1.0, 0.5)), ((2.0, 0.5), (3.0, 0.5))],
                glyph_groups=((0,), (1,)),
                line_index=1,
            ),  # block 1: groups
        )
        rendered = _make_rendered(records, label=label)

        blocks, groups_by_block = build_text_blocks_with_glyphs(
            [_packed("t", 0.0, 0.0)], {"t": rendered}, pen=1
        )

        assert [b.block_id for b in blocks] == [0, 1]
        assert set(groups_by_block) == {1}
        assert groups_by_block[1] == ((0,), (1,))

    def test_wrapper_matches_glyph_free_builder(self) -> None:
        """The historical wrapper returns identical blocks."""
        label = _make_label()
        records = (
            _glyph_record(
                [((0.0, 0.0), (1.0, 0.0)), ((2.0, 0.0), (3.0, 0.0))],
                glyph_groups=((0,), (1,)),
            ),
        )
        rendered = _make_rendered(records, label=label)
        packed = [_packed("t", 0.0, 0.0)]

        plain = build_text_blocks(packed, {"t": rendered}, pen=1)
        blocks, _ = build_text_blocks_with_glyphs(packed, {"t": rendered}, pen=1)

        assert blocks == plain


class TestEmitLayerDocument:
    """Integer-unit HPGL emission (pen-select-free per-cutter framing)."""

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

    def test_paths_emit_penup_led_without_sp(self) -> None:
        """All paths emit as bare PU-led streams: no SP selects at all."""
        paths = [self._path([(0, 0), (10, 0)]), self._path([(5, 5), (15, 5)])]
        content = emit_layer_document(paths)
        assert content == "IN;PA;PU0,0;PD10,0;PU5,5;PD15,5;SP;"

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
        content = emit_layer_document([path])
        assert "PU100,100;PD;AA50,100,-90;" in content

    def test_degenerate_paths_skipped(self) -> None:
        """Segment-less paths emit nothing (but do not crash)."""
        content = emit_layer_document([StrokePath()])
        assert content == "IN;PA;SP;"


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
        assert result.content.startswith("IN;PA;")
        assert result.content.endswith("SP;")
        # Every optimized stroke must exist in the unoptimized layer.
        from plt_optimizer.core.parser import PLTParser

        raw = emit_layer_document(
            [
                path
                for block in build_text_blocks([_packed("t", 0.0, 0.0)], {"t": rendered}, 1)
                for path in block.paths
            ]
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

    def test_emitted_rapid_travel_never_increases(self) -> None:
        """The direction sweep cannot lengthen the emitted file's travel.

        Multi-stroke chunks exercise intra-chunk travel: whole-chunk reversal
        leaves those gaps invariant, so the emitted metric moves only with the
        inter-chunk gaps the sweep optimises.
        """
        from plt_optimizer.core.parser import PLTParser

        label = _make_label()
        records = (
            _multi_stroke_record(
                [((0.0, 0.0), (0.4, 0.0)), ((0.9, 0.0), (1.3, 0.0)), ((1.8, 0.0), (2.2, 0.0))]
            ),
            _multi_stroke_record(
                [((0.2, 0.5), (0.6, 0.5)), ((1.1, 0.5), (1.5, 0.5))],
                line_index=1,
            ),
        )
        rendered = _make_rendered(records, label=label)
        packed = [_packed("t", 0.0, 0.0)]

        raw = emit_layer_document(
            [
                path
                for block in build_text_blocks(packed, {"t": rendered}, 1)
                for path in block.paths
            ]
        )
        raw_travel = PLTParser().parse_string(raw).rapid_distance()

        result = optimize_text_layer(
            packed, {"t": rendered}, pen=1, strategy_factory=_fast_strategy
        )
        assert result is not None
        opt_travel = PLTParser().parse_string(result.content).rapid_distance()
        assert opt_travel <= raw_travel + 1e-6

    def test_sweep_reported_on_outcome(self) -> None:
        """A layer whose chunks gain from reversal reports the sweep.

        Five single-stroke chunks chosen (by random search over the real
        NearestNeighbor + 2-Opt path) so the strategy leaves a stale direction
        flag: the sweep reverses one chunk and cuts inter-chunk travel by
        ~14% (1135.248 -> 979.359 plotter units).
        """
        label = _make_label()
        spans = [
            ((0.226, 0.852), (0.758, 0.852)),
            ((1.204, 0.669), (0.208, 0.669)),
            ((0.413, 0.894), (0.000, 0.894)),
            ((1.536, 0.419), (1.910, 0.419)),
            ((1.590, 0.536), (1.529, 0.536)),
        ]
        records = tuple(_line_record(list(span), line_index=i) for i, span in enumerate(spans))
        rendered = _make_rendered(records, label=label)
        result = optimize_text_layer(
            [_packed("t", 0.0, 0.0)],
            {"t": rendered},
            pen=1,
            strategy_factory=_fast_strategy,
        )
        assert result is not None
        outcome = result.outcome
        assert outcome.direction_sweep_passes == 1
        assert outcome.direction_sweep_flips == 1
        assert outcome.direction_sweep_travel_before == pytest.approx(1135.2477, abs=1e-3)
        assert outcome.direction_sweep_travel_after == pytest.approx(979.3592, abs=1e-3)
        assert "direction_sweep=" in outcome.method_notes

    def test_intra_sweep_flips_glyphs_and_shrinks_emitted_travel(self) -> None:
        """A chunk whose middle glyph wants reversing reports the intra sweep.

        One chunk of three single-stroke glyphs (the flip fixture): the middle
        glyph is traced right-to-left, so reversing it shortens both adjacent
        gaps. The inter-chunk tour is a single node (no gain there), so the
        entire emitted-travel drop is the intra sweep's doing.
        """
        from plt_optimizer.core.parser import PLTParser

        label = _make_label()
        record = _glyph_record(
            [((0.0, 0.0), (0.1, 0.0)), ((0.2, 0.0), (0.12, 0.0)), ((0.3, 0.0), (0.4, 0.0))],
            glyph_groups=((0,), (1,), (2,)),
        )
        rendered = _make_rendered((record,), label=label)
        packed = [_packed("t", 0.0, 0.0)]

        result = optimize_text_layer(
            packed, {"t": rendered}, pen=1, strategy_factory=_fast_strategy
        )
        assert result is not None
        outcome = result.outcome
        assert outcome.intra_sweep_flips == 1
        assert outcome.intra_sweep_groups == 3
        assert outcome.intra_sweep_travel_after < outcome.intra_sweep_travel_before
        assert "intra_sweep=" in outcome.method_notes

        raw = emit_layer_document(
            [p for b in build_text_blocks(packed, {"t": rendered}, 1) for p in b.paths]
        )
        raw_travel = PLTParser().parse_string(raw).rapid_distance()
        opt_travel = PLTParser().parse_string(result.content).rapid_distance()
        # The intra gain flows one-for-one into the emitted file.
        assert opt_travel < raw_travel - 1e-6
        assert opt_travel == pytest.approx(outcome.intra_sweep_emitted_after, abs=1e-3)

    def test_intra_sweep_escape_hatch_reproduces_pre_sweep_output(self) -> None:
        """``intra_sweep=False`` emits the pre-sweep (chronological) layer."""
        label = _make_label()
        record = _glyph_record(
            [((0.0, 0.0), (0.1, 0.0)), ((0.2, 0.0), (0.12, 0.0)), ((0.3, 0.0), (0.4, 0.0))],
            glyph_groups=((0,), (1,), (2,)),
        )
        rendered = _make_rendered((record,), label=label)
        packed = [_packed("t", 0.0, 0.0)]

        swept = optimize_text_layer(packed, {"t": rendered}, pen=1, strategy_factory=_fast_strategy)
        plain = optimize_text_layer(
            packed,
            {"t": rendered},
            pen=1,
            strategy_factory=_fast_strategy,
            intra_sweep=False,
        )
        assert swept is not None and plain is not None
        assert swept.outcome.intra_sweep_flips == 1
        assert plain.outcome.intra_sweep_flips == 0
        assert plain.outcome.intra_sweep_travel_before is None
        assert "intra_sweep=" not in plain.outcome.method_notes
        assert plain.content == emit_layer_document(
            [p for b in build_text_blocks(packed, {"t": rendered}, 1) for p in b.paths]
        )

    def test_glyph_free_layer_reports_no_intra_sweep(self) -> None:
        """A layer with no glyph partition reports nothing intra."""
        label = _make_label()
        records = (
            _line_record([(0.0, 0.0), (1.0, 0.0)]),
            _line_record([(0.2, 0.5), (1.2, 0.5)], line_index=1),
        )
        rendered = _make_rendered(records, label=label)

        result = optimize_text_layer(
            [_packed("t", 0.0, 0.0)], {"t": rendered}, pen=1, strategy_factory=_fast_strategy
        )
        assert result is not None
        assert result.outcome.intra_sweep_flips == 0
        assert result.outcome.intra_sweep_travel_before is None
        assert "intra_sweep=" not in result.outcome.method_notes

    def test_coincident_strokes_merge_and_drop_their_tool_up(self) -> None:
        """Tip-to-tail strokes emit as one path, so the file loses a PU.

        Two touching strokes plus one far stroke: the touching pair merges
        into a single continuous cut, removing the pen-up/pen-down pair
        between them. Emitted rapid travel and the cutting geometry are
        unchanged -- the win is purely the tool-up (and the bytes it costs).
        """
        label = _make_label()
        record = _multi_stroke_record(
            [((0.0, 0.0), (0.5, 0.0)), ((0.5, 0.0), (1.0, 0.0)), ((2.0, 0.5), (2.5, 0.5))]
        )
        rendered = _make_rendered((record,), label=label)
        packed = [_packed("t", 0.0, 0.0)]

        raw = emit_layer_document(
            [p for b in build_text_blocks(packed, {"t": rendered}, 1) for p in b.paths]
        )
        result = optimize_text_layer(
            packed, {"t": rendered}, pen=1, strategy_factory=_fast_strategy
        )
        assert result is not None

        # Every path is PU-led, so the PU count is the path count.
        assert raw.count("PU") == 3
        assert result.content.count("PU") == 2
        assert result.outcome.merges_applied == 1
        assert "merge=" in result.outcome.method_notes

        # Geometry and rapid travel are preserved; only the tool-up is gone.
        from plt_optimizer.core.parser import PLTParser

        raw_doc = PLTParser().parse_string(raw)
        opt_doc = PLTParser().parse_string(result.content)

        def undirected(doc: object) -> list:
            return sorted(
                (
                    min(round(s.start.x), round(s.end.x)),
                    min(round(s.start.y), round(s.end.y)),
                    max(round(s.start.x), round(s.end.x)),
                    max(round(s.start.y), round(s.end.y)),
                )
                for p in doc.stroke_paths  # type: ignore[attr-defined]
                for s in p.segments
            )

        assert undirected(opt_doc) == undirected(raw_doc)
        assert opt_doc.rapid_distance() == pytest.approx(raw_doc.rapid_distance(), abs=1e-3)

    def test_merge_escape_hatch_keeps_every_tool_up(self) -> None:
        """``merge_coincident=False`` emits the unmerged (PU-per-stroke) layer."""
        label = _make_label()
        record = _multi_stroke_record(
            [((0.0, 0.0), (0.5, 0.0)), ((0.5, 0.0), (1.0, 0.0)), ((2.0, 0.5), (2.5, 0.5))]
        )
        rendered = _make_rendered((record,), label=label)
        packed = [_packed("t", 0.0, 0.0)]

        raw = emit_layer_document(
            [p for b in build_text_blocks(packed, {"t": rendered}, 1) for p in b.paths]
        )
        merged = optimize_text_layer(
            packed, {"t": rendered}, pen=1, strategy_factory=_fast_strategy
        )
        plain = optimize_text_layer(
            packed,
            {"t": rendered},
            pen=1,
            strategy_factory=_fast_strategy,
            merge_coincident=False,
        )
        assert merged is not None and plain is not None

        assert merged.outcome.merges_applied == 1
        assert plain.outcome.merges_applied == 0
        assert plain.outcome.merged_paths_before is None
        assert "merge=" not in plain.outcome.method_notes
        assert plain.content.count("PU") == raw.count("PU")


class TestOptimizeStructuralLayer:
    """Borders+holes routing entry point."""

    def test_empty_content_returns_none(self) -> None:
        assert optimize_structural_layer("IN;PA;SP;", _fast_strategy) is None

    def test_deduplicates_coincident_borders(self) -> None:
        # Two labels sharing the edge x=1000 (duplicated stroke).
        content = (
            "IN;PA;PU0,0;PD1000,0,1000,1000,0,1000,0,0;"
            "PU1000,0;PD2000,0,2000,1000,1000,1000,1000,0;"
            "SP;"
        )
        result = optimize_structural_layer(content, _fast_strategy)
        assert result is not None
        # The shared edge collapses to one stroke: 7 unique segments.
        assert result.node_count <= 8
        import re as _re

        assert not _re.search(r"SP\d", result.content)  # pen-select-free output
        assert result.content.endswith("SP;")

    def test_holes_keep_arc_geometry(self) -> None:
        content = (
            "IN;PA;PU0,0;PD1000,0;PU100,100;PD100,100;"
            "AA50,100,-90;AA50,100,-90;AA50,100,-90;AA50,100,-90;SP;"
        )
        result = optimize_structural_layer(content, _fast_strategy)
        assert result is not None
        assert "PU100,100;PD100,100" in result.content
        assert "AA50,100,-90" in result.content

    def test_sweep_preserves_arc_magnitudes(self) -> None:
        """Reversal negates an arc's sweep sign but never its magnitude."""
        import re as _re

        content = (
            "IN;PA;PU0,0;PD1000,0;PU100,100;PD100,100;"
            "AA50,100,-90;AA50,100,-90;AA50,100,-90;AA50,100,-90;SP;"
        )
        result = optimize_structural_layer(content, _fast_strategy)
        assert result is not None

        sweeps = [
            abs(float(match)) for match in _re.findall(r"AA50,100,(-?[\d.]+)", result.content)
        ]
        assert len(sweeps) == 4
        assert all(math.isclose(sweep, 90.0) for sweep in sweeps)
