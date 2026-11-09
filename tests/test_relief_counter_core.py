"""Unit tests for the relief counter core: radius sides, full-diameter floor.

Reference ground truth (coarse 0.06, device units, 1016 u/in): the "A"
counter fill triangle has its SIDES at the island edges eroded by the cutter
RADIUS and its BASE at the floor island edge minus the FULL DIAMETER
(606.53 - 60.96 = 545.566), with SHARP corners (no fillet arcs). The tests
work label-local (+y up): floor edge at y = 0, so the base sits at y = d.
"""

from __future__ import annotations

import math
from typing import List, Tuple

import pytest
from shapely.geometry import LineString, MultiPolygon, Point, Polygon

from plt_optimizer.generate.relief.cutters import ReliefCutter
from plt_optimizer.generate.relief.pipeline import _clip_counter_floor, _cores
from plt_optimizer.generate.relief.regions import EngraveRegions

#: Reference 0.06 cutter in inches.
D = 0.06
R = D / 2.0
#: One plotter unit (the plan's tolerance bar).
U = 1.0 / 1016.0

# The "A" counter, scaled triangle: base (floor) on y = 0, apex on top.
COUNTER = Polygon([(-0.2, 0.0), (0.2, 0.0), (0.0, 0.4)])


def _eroded(counter: Polygon = COUNTER, radius: float = R) -> Polygon:
    return counter.buffer(-radius, quad_segs=256, join_style=1)


def _ring_points(polygons: List[Polygon]) -> List[Tuple[float, float]]:
    return list(polygons[0].exterior.coords)


class TestClipCounterFloor:
    """The floor edge of a counter core erodes by the full diameter."""

    def test_base_sits_one_diameter_off_the_floor(self) -> None:
        clipped = _clip_counter_floor(_eroded(), D)
        assert clipped
        x0, y0, x1, y1 = clipped[0].bounds
        assert y0 == pytest.approx(D, abs=U)
        # The apex (top, narrow) is untouched by the clip.
        assert y1 == pytest.approx(_eroded().bounds[3], abs=U)

    def test_sides_erode_by_the_radius(self) -> None:
        clipped = _clip_counter_floor(_eroded(), D)
        assert clipped
        left_edge = LineString([(-0.2, 0.0), (0.0, 0.4)])
        coords = list(clipped[0].exterior.coords)
        mids = [
            ((coords[i][0] + coords[i + 1][0]) / 2.0, (coords[i][1] + coords[i + 1][1]) / 2.0)
            for i in range(len(coords) - 1)
        ]
        dists = [
            Point(mx, my).distance(left_edge) for mx, my in mids if mx < 0.0 and 0.10 < my < 0.30
        ]
        assert dists
        assert all(d == pytest.approx(R, abs=U) for d in dists)

    def test_base_width_follows_radius_sides(self) -> None:
        clipped = _clip_counter_floor(_eroded(), D)
        assert clipped
        base_xs = [x for x, y in _ring_points(clipped) if abs(y - D) < 1e-9]
        # Side lines x = 0.2 - 0.5y offset inward by r / sin(63.435 deg).
        expected = 0.2 - 0.5 * D - R / math.sin(math.atan2(0.4, 0.2))
        assert max(base_xs) == pytest.approx(expected, abs=U)

    def test_corners_stay_sharp(self) -> None:
        """No fillet discs on interior cores: the ring is a plain polygon."""
        clipped = _clip_counter_floor(_eroded(), D)
        assert clipped
        # Triangle (3 sharp eroded corners) + floor cut = <= 5 vertices;
        # a fillet pass would add hundreds of arc samples.
        assert len(_ring_points(clipped)) <= 6

    def test_floor_is_the_widest_bbox_edge(self) -> None:
        trapezoid = Polygon([(0.0, 0.0), (1.0, 0.0), (0.8, 0.3), (0.2, 0.3)])
        clipped = _clip_counter_floor(trapezoid.buffer(-R, quad_segs=64), D)
        assert clipped
        # Floor = the wide base (min-y): y0 moved to 0.0 + (d - r) extra
        # beyond the r-erosion.
        assert clipped[0].bounds[1] == pytest.approx(0.0 + R + R, abs=1e-6)
        # Top edge (narrow) untouched.
        assert clipped[0].bounds[3] == pytest.approx(0.3 - R, abs=1e-6)

    def test_round_counter_clipped_plausibly(self) -> None:
        """An "O" counter (no wide/narrow edge): clip one bbox edge by d - r.

        Geometrically plausible; there is no EngraveLab "O" reference to
        verify the tie-breaking against (documented in relief/AGENTS.md).
        """
        ring = Point(0.5, 0.5).buffer(0.1).buffer(-R, quad_segs=256, join_style=1)
        clipped = _clip_counter_floor(ring, D)
        assert clipped
        x0, y0, x1, y1 = clipped[0].bounds
        # All four bbox edges tie on the circle; the floor (widest, ties to
        # the min-y edge) erodes to a full diameter from the island floor.
        assert y0 == pytest.approx(0.4 + D, abs=U)
        # The other edges keep the radius erosion.
        assert x1 == pytest.approx(0.6 - R, abs=U)
        assert y1 == pytest.approx(0.6 - R, abs=U)

    def test_empty_core(self) -> None:
        assert _clip_counter_floor(Polygon(), D) == []

    def test_deep_clip_empties_the_counter(self) -> None:
        # A counter shallower than the cutter diameter leaves nothing to fill.
        sliver = Polygon([(0.0, 0.0), (1.0, 0.0), (1.0, 0.02), (0.0, 0.02)])
        assert _clip_counter_floor(sliver.buffer(-0.005, quad_segs=16), D) == []


class TestCoresInterior:
    """_cores applies the clip (and no fillet) to interior components."""

    def _regions(self, interior: Tuple[Polygon, ...]) -> EngraveRegions:
        box = Polygon([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])
        return EngraveRegions(
            box=box,
            islands=Polygon(),
            engrave=MultiPolygon(list(interior)),
            interior=interior,
            outer=(),
            max_ink_width=0.1,
        )

    def test_interior_core_base_at_floor_minus_d(self) -> None:
        moved = Polygon([(x, y + 0.3) for x, y in COUNTER.exterior.coords])
        regions = self._regions((moved,))
        _outer, interior = _cores(regions, ReliefCutter(D, D))
        assert interior
        # Floor island edge 0.3, eroded base = 0.3 + d (label-local +y up).
        assert min(p.bounds[1] for p in interior) == pytest.approx(0.3 + D, abs=U)
        # Sides keep the radius erosion (apex just below 0.3 + 0.4).
        assert max(p.bounds[3] for p in interior) < 0.7

    def test_interior_core_sharp(self) -> None:
        moved = Polygon([(x, y + 0.3) for x, y in COUNTER.exterior.coords])
        regions = self._regions((moved,))
        _outer, interior = _cores(regions, ReliefCutter(D, D))
        assert interior
        vertices = sum(len(p.exterior.coords) for p in interior)
        assert vertices <= 6
