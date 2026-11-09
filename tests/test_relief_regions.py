"""Unit tests for the relief engraving region model (relief/regions.py)."""

from __future__ import annotations

import math

import pytest
from shapely.geometry import Point

from plt_optimizer.generate.relief.regions import (
    build_engrave_regions,
    island_polygons,
    stroke_to_points,
)
from plt_optimizer.generate.text_geometry import ArcSeg, LineSeg, Stroke

SQUARE = [(0.2, -0.3), (0.8, -0.3), (0.8, 0.3), (0.2, 0.3), (0.2, -0.3)]


class TestIslandPolygons:
    """Open strokes buffer, closed rings fill with hole parity."""

    def test_open_stroke_buffers_by_half_width(self) -> None:
        islands = island_polygons([([(0.0, 0.0), (1.0, 0.0)], 0.1)])
        # 1.0 x 0.1 strip plus two half-disk caps (pi * 0.05^2).
        assert islands.area == pytest.approx(0.10784, abs=2e-4)
        assert islands.bounds == pytest.approx((-0.05, -0.05, 1.05, 0.05))

    def test_closed_ring_fills_interior(self) -> None:
        islands = island_polygons([(SQUARE, 0.1)])
        assert islands.area == pytest.approx(0.36, rel=1e-9)

    def test_nested_ring_punches_hole(self) -> None:
        inner = [(0.4, -0.1), (0.6, -0.1), (0.6, 0.1), (0.4, 0.1), (0.4, -0.1)]
        islands = island_polygons([(SQUARE, 0.1), (inner, 0.1)])
        assert islands.area == pytest.approx(0.32, rel=1e-9)
        assert not islands.contains(Point(0.5, 0.0))
        assert islands.contains(Point(0.3, 0.0))

    def test_ring_inside_shelled_ring(self) -> None:
        ring = [(0.3, -0.2), (0.7, -0.2), (0.7, 0.2), (0.3, 0.2), (0.3, -0.2)]
        square = [(0.45, -0.05), (0.55, -0.05), (0.55, 0.05), (0.45, 0.05), (0.45, -0.05)]
        islands = island_polygons([(square, 0.1), (ring, 0.1)])
        # ring shell with the square hole; the square itself fills inside.
        assert islands.area == pytest.approx(0.16 - 0.01 + 0.01, rel=1e-9)

    def test_empty_ink(self) -> None:
        assert island_polygons([]).is_empty


class TestBuildEngraveRegions:
    """Box minus islands with outer/interior classification."""

    def test_counter_is_interior_component(self) -> None:
        # The "A" island: a closed ring band (square ring) whose counter
        # (the enclosed hole of the band) is its own engrave component.
        outer = [(0.3, -0.2), (0.7, -0.2), (0.7, 0.2), (0.3, 0.2), (0.3, -0.2)]
        counter = [(0.45, -0.05), (0.55, -0.05), (0.55, 0.05), (0.45, 0.05), (0.45, -0.05)]
        islands = island_polygons([(outer, 0.1), (counter, 0.1)])
        # islands = the ring band (outer minus counter).
        assert islands.area == pytest.approx(0.16 - 0.01, rel=1e-9)
        regions = build_engrave_regions(1.0, 1.0, [(outer, 0.3), (counter, 0.3)], False)
        assert len(regions.interior) == 1
        assert regions.interior[0].bounds == pytest.approx((0.45, -0.05, 0.55, 0.05))
        assert len(regions.outer) == 1
        assert regions.max_ink_width == 0.3

    def test_border_touching_is_outer(self) -> None:
        regions = build_engrave_regions(1.0, 1.0, [([(0.5, 0.0), (0.9, 0.0)], 0.1)], False)
        assert not regions.interior
        assert len(regions.outer) == 1

    def test_preserve_border_insets_box(self) -> None:
        regions = build_engrave_regions(1.0, 1.0, [], True, border_band=0.2)
        minx, miny, maxx, maxy = regions.box.bounds
        assert math.isclose(minx, 0.2, abs_tol=1e-9)
        assert math.isclose(maxx, 0.8, abs_tol=1e-9)
        assert regions.engrave.area == pytest.approx(0.36, rel=1e-9)

    def test_box_y_center_offsets_fill(self) -> None:
        regions = build_engrave_regions(1.0, 1.0, [], False, box_y_center=0.25)
        _minx, miny, _maxx, maxy = regions.box.bounds
        assert math.isclose(miny, -0.25, abs_tol=1e-9)
        assert math.isclose(maxy, 0.75, abs_tol=1e-9)


class TestStrokeToPoints:
    """Flattening keeps line vertices and densely samples arcs."""

    def test_lines(self) -> None:
        stroke = Stroke(
            pen_up=0j,
            segments=(LineSeg(0j, 1 + 1j), LineSeg(1 + 1j, 2j)),
        )
        points = stroke_to_points(stroke)
        assert points[0] == (0.0, 0.0)
        assert points[-1] == (0.0, 2.0)

    def test_arc_flattening_lands_on_endpoints(self) -> None:
        start = 1 + 0j
        arc = ArcSeg(start=start, center=0j, sweep_deg=90.0)
        stroke = Stroke(pen_up=start, segments=(arc,))
        points = stroke_to_points(stroke, tol=1e-4)
        assert math.isclose(points[0][0], 1.0, abs_tol=1e-9)
        assert math.isclose(points[-1][0], 0.0, abs_tol=1e-4)
        assert math.isclose(points[-1][1], 1.0, abs_tol=1e-4)
        assert len(points) > 10
