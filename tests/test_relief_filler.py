"""Unit tests for the S-Sweep raster filler (relief/filler.py)."""

from __future__ import annotations

import pytest
from shapely.geometry import Point, Polygon

from plt_optimizer.generate.relief.filler import (
    SSweepFiller,
    polyline_length,
    region_polygons,
)

SQUARE = Polygon([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])


class TestSSweepFill:
    """Scanline grid, insets, serpentine chaining, splits."""

    def test_row_grid_and_inset(self) -> None:
        filler = SSweepFiller()
        strokes = filler.fill([SQUARE], 0.25)
        assert strokes, "square must raster"
        ys = sorted({round(y, 6) for stroke in strokes for _x, y in stroke})
        assert ys == [0.25, 0.5, 0.75]
        # Interior rows inset by the default pitch/2 = 0.125.
        xs = [x for stroke in strokes for x, _y in stroke]
        assert min(xs) == pytest.approx(0.125)
        assert max(xs) == pytest.approx(0.875)

    def test_anchor_grid_has_edge_rows(self) -> None:
        """The reference border grid: edge rows at the bbox boundary, full
        span, zero inset (the 30.48/985.52 tab rows); interior rows inset."""
        filler = SSweepFiller()
        strokes = filler.fill([SQUARE], 0.25, anchor=(0.0, 0.0, 1.0, 1.0))
        ys = sorted({round(y, 6) for stroke in strokes for _x, y in stroke})
        assert ys == [0.0, 0.25, 0.5, 0.75, 1.0]
        edge = [x for stroke in strokes for x, y in stroke if y < 1e-9 or y > 1.0 - 1e-9]
        assert min(edge) == pytest.approx(0.0)
        assert max(edge) == pytest.approx(1.0)
        interior = [x for stroke in strokes for x, y in stroke if 1e-9 < y < 1.0 - 1e-9]
        assert min(interior) == pytest.approx(0.125)

    def test_contiguous_serpentine_single_stroke(self) -> None:
        filler = SSweepFiller()
        strokes = filler.fill([SQUARE], 0.25)
        assert len(strokes) == 1
        stroke = strokes[0]
        assert stroke[0][1] == pytest.approx(0.25)
        # 3 rows x (start, end) + explicit vertical links (per-region grid
        # has no bbox-edge rows; the anchored border grid does, tested in
        # test_anchor_grid_has_edge_rows).
        assert len(stroke) == 6

    def test_split_regions_chain_independently(self) -> None:
        left = Polygon([(0.0, 0.0), (0.5, 0.0), (0.5, 1.0), (0.0, 1.0)])
        right = Polygon([(0.7, 0.0), (1.0, 0.0), (1.0, 1.0), (0.7, 1.0)])
        filler = SSweepFiller()
        strokes = filler.fill([left, right], 0.1)
        assert len(strokes) >= 2
        # Each region rastered independently; no stroke spans the gap.
        for stroke in strokes:
            xs = [x for x, _y in stroke]
            assert max(xs) - min(xs) < 0.6

    def test_wedge_split_side_lanes_keep_connectors(self) -> None:
        """ONE region splitting (the "A" legs diverging) chains each side.

        The wedge (apex below, widening upward) cuts the square into a
        full-width lane below y=0.4 and left/right lanes above it. Per-side
        rows must stay a SINGLE pen-down chain with the pitch-spaced
        connector between consecutive rows (the reference's vertical/
        diagonal links) -- not isolated strokes.
        """
        wedge = Polygon([(0.5, 0.4), (1.0, 1.0), (0.0, 1.0)])
        region = SQUARE.difference(wedge)
        filler = SSweepFiller()
        pitch = 0.1
        strokes = filler.fill([region], pitch)

        # Rows 0.5..0.7 exist on both sides; 0.8/0.9 drop at the inset
        # floor near the apex side. Left lane (full-width rows 0.1..0.4
        # continuing on the left side) and right lane (0.5..0.7).
        def chain_with(y: float, side_x: float):
            hits = [
                s
                for s in strokes
                if any(abs(py - y) < 1e-9 and (px < side_x) == (side_x < 0.5) for px, py in s)
            ]
            assert len(hits) == 1, f"row y={y} side x<{side_x} not in exactly one stroke"
            return hits[0]

        left_chain = chain_with(0.5, 0.45)
        ys_left = {round(y, 6) for _x, y in left_chain}
        assert {0.5, 0.6, 0.7} <= ys_left, "left lane rows must chain into one stroke"
        right_chain = chain_with(0.5, 0.55)
        ys_right = {round(y, 6) for _x, y in right_chain}
        assert {0.5, 0.6, 0.7} <= ys_right, "right lane rows must chain into one stroke"
        # Every consecutive pair inside a lane chain is a row (horizontal,
        # dy=0) or a pitch-spaced link (dy=pitch, x follows the interval
        # edges -- the reference's diagonal connectors): never a multi-pitch
        # skip, never a vertical jump off the grid.
        for stroke in (left_chain, right_chain):
            for (_x0, y0), (_x1, y1) in zip(stroke, stroke[1:]):
                dy = abs(y1 - y0)
                assert min(dy, abs(dy - pitch)) < 1e-9, (
                    "chain segments must be rows (dy=0) or pitch links (dy=pitch)"
                )

    def test_unsplit_output_unchanged_by_lanes(self) -> None:
        # A square region: the lane grouping is a no-op (single lane,
        # global parity == lane parity), the chain is one stroke.
        filler = SSweepFiller()
        strokes = filler.fill([SQUARE], 0.1)
        assert len(strokes) == 1
        assert len(strokes[0]) == 2 * len({round(y, 6) for _x, y in strokes[0]})

    def test_min_pass_floor(self) -> None:
        # A triangle so thin at the apex that rows drop below the floor.
        triangle = Polygon([(0.0, 0.0), (1.0, 0.0), (0.5, 0.05)])
        filler = SSweepFiller(min_pass_fraction=0.25)
        strokes = filler.fill([triangle], 0.02)
        assert strokes
        for stroke in strokes:
            for (x0, y0), (x1, y1) in zip(stroke, stroke[1:]):
                if abs(y0 - y1) < 1e-9:
                    assert abs(x1 - x0) >= 0.25 * 0.02 - 1e-9

    def test_invalid_pitch_raises(self) -> None:
        with pytest.raises(ValueError):
            SSweepFiller().fill([SQUARE], 0.0)

    def test_end_inset_fraction(self) -> None:
        filler = SSweepFiller(end_inset_fraction=1.25)
        strokes = filler.fill([SQUARE], 0.25)
        xs = [x for stroke in strokes for x, _y in stroke]
        assert min(xs) == pytest.approx(0.3125)  # 0 + pitch * 1.25

    def test_no_island_intrusion(self) -> None:
        square = Polygon([(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)])
        filler = SSweepFiller()
        region = SQUARE.difference(square)
        strokes = filler.fill(region_polygons(region.buffer(-0.05)), 0.1)
        assert strokes
        for stroke in strokes:
            for x, y in stroke:
                assert not square.contains(Point(x, y))


class TestSSweepOutline:
    """Outlines are the closed boundary rings of the filled regions."""

    def test_square_outline_closed(self) -> None:
        filler = SSweepFiller()
        rings = filler.outline([SQUARE])
        assert len(rings) == 1
        ring = rings[0]
        assert ring[0] == ring[-1]
        assert len(ring) >= 5

    def test_annulus_outlines_both_rings(self) -> None:
        hole = Polygon([(0.4, 0.4), (0.6, 0.4), (0.6, 0.6), (0.4, 0.6)])
        annulus = SQUARE.difference(hole)
        filler = SSweepFiller()
        rings = filler.outline(region_polygons(annulus))
        assert len(rings) == 2
        for ring in rings:
            assert ring[0] == ring[-1]


class TestHelpers:
    """Small pure helpers."""

    def test_polyline_length(self) -> None:
        assert polyline_length([(0, 0), (3, 4)]) == pytest.approx(5.0)
        assert polyline_length([]) == 0.0

    def test_region_polygons_empty(self) -> None:
        assert region_polygons(Polygon()) == []

    def test_closed_sweep_is_contiguous(self) -> None:
        filler = SSweepFiller()
        strokes = filler.fill([SQUARE], 0.25)
        assert len(strokes) == 1
        total = sum(polyline_length(s) for s in strokes)
        # 3 row traversals (0.75) + 2 vertical links (0.25).
        assert total == pytest.approx(3 * 0.75 + 2 * 0.25, rel=1e-9)
