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
        # Row ends inset by the default pitch/2 = 0.125.
        xs = [x for stroke in strokes for x, _y in stroke]
        assert min(xs) == pytest.approx(0.125)
        assert max(xs) == pytest.approx(0.875)

    def test_contiguous_serpentine_single_stroke(self) -> None:
        filler = SSweepFiller()
        strokes = filler.fill([SQUARE], 0.25)
        assert len(strokes) == 1
        stroke = strokes[0]
        assert stroke[0][1] == pytest.approx(0.25)
        # 3 rows x (start, end) with explicit vertical link points.
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
