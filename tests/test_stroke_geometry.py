"""Tests for plt_optimizer.diagnostics.stroke_geometry.

Covers the true-width stroke expansion used by the physical-width plots:
contiguous-run joining, arc flattening, degenerate inputs, and the
inch-space polygon geometry.
"""

from __future__ import annotations

import math

import pytest

from plt_optimizer.core.models import ArcSegment, Coordinate, StrokeSegment
from plt_optimizer.core.units import HPGL_UNITS_PER_INCH
from plt_optimizer.diagnostics.stroke_geometry import (
    StrokePolygon,
    expand_cutting_polygons,
)

# Whole-plotter-scale in integer device units (1 inch), so hand-built
# coordinates keep meaning whole inches at the HPGL default step size.
U = int(HPGL_UNITS_PER_INCH)


def _bounds(polygons: list[StrokePolygon]) -> tuple[float, float, float, float]:
    """Inch-space bounds over every exterior vertex of the polygons."""
    xs = [x for polygon in polygons for x, _y in polygon.exterior]
    ys = [y for polygon in polygons for _x, y in polygon.exterior]
    return min(xs), min(ys), max(xs), max(ys)


class TestStraightRuns:
    """Expansion of straight cutting runs."""

    def test_horizontal_line_expands_to_full_width_band(self) -> None:
        """A 2in line at a 0.1in cutter measures 2.1 x 0.1 in (round caps)."""
        segs = [StrokeSegment(Coordinate(0, 0), Coordinate(2 * U, 0), True)]
        polygons = expand_cutting_polygons(segs, 0.1)
        assert len(polygons) == 1
        x_min, y_min, x_max, y_max = _bounds(polygons)
        assert x_min == pytest.approx(-0.05, abs=1e-9)
        assert x_max == pytest.approx(2.05, abs=1e-9)
        assert y_min == pytest.approx(-0.05, abs=1e-9)
        assert y_max == pytest.approx(0.05, abs=1e-9)

    def test_area_matches_rectangle_plus_round_caps(self) -> None:
        """Buffered area = length*width + full circle of caps (pi*r^2)."""
        segs = [StrokeSegment(Coordinate(0, 0), Coordinate(2 * U, 0), True)]
        (polygon,) = expand_cutting_polygons(segs, 0.1)
        xs = [x for x, _y in polygon.exterior]
        ys = [y for _x, y in polygon.exterior]
        # Shoelace area of the exterior ring (no interiors on a line band).
        ring = list(zip(xs, ys))
        area = 0.0
        for i in range(len(ring)):
            x1, y1 = ring[i]
            x2, y2 = ring[(i + 1) % len(ring)]
            area += x1 * y2 - x2 * y1
        area = abs(area) / 2
        expected = 2.0 * 0.1 + math.pi * 0.05**2
        assert area == pytest.approx(expected, rel=1e-3)

    def test_connected_polyline_joins_into_one_polygon(self) -> None:
        """Endpoint-touching segments merge into a single ribbon."""
        segs = [
            StrokeSegment(Coordinate(0, 0), Coordinate(U, 0), True),
            StrokeSegment(Coordinate(U, 0), Coordinate(U, U), True),
        ]
        polygons = expand_cutting_polygons(segs, 0.1)
        assert len(polygons) == 1

    def test_disconnected_segments_split_into_separate_polygons(self) -> None:
        """A gap between runs yields one polygon per run."""
        segs = [
            StrokeSegment(Coordinate(0, 0), Coordinate(U, 0), True),
            StrokeSegment(Coordinate(4 * U, 0), Coordinate(5 * U, 0), True),
        ]
        polygons = expand_cutting_polygons(segs, 0.1)
        assert len(polygons) == 2

    def test_rapid_segments_are_ignored(self) -> None:
        """Pen-up segments engrave nothing."""
        segs = [StrokeSegment(Coordinate(0, 0), Coordinate(U, 0), False)]
        assert expand_cutting_polygons(segs, 0.1) == []

    def test_rapid_break_splits_runs(self) -> None:
        """A rapid between two cuts separates the ribbons."""
        segs = [
            StrokeSegment(Coordinate(0, 0), Coordinate(U, 0), True),
            StrokeSegment(Coordinate(U, 0), Coordinate(2 * U, 0), False),
            StrokeSegment(Coordinate(2 * U, 0), Coordinate(3 * U, 0), True),
        ]
        polygons = expand_cutting_polygons(segs, 0.1)
        assert len(polygons) == 2


class TestDegenerateInputs:
    """Zero-width, zero-length, and empty inputs."""

    def test_zero_width_yields_no_polygons(self) -> None:
        """A non-positive cutter diameter engraves nothing."""
        segs = [StrokeSegment(Coordinate(0, 0), Coordinate(U, 0), True)]
        assert expand_cutting_polygons(segs, 0.0) == []
        assert expand_cutting_polygons(segs, -0.1) == []

    def test_empty_segments_yield_no_polygons(self) -> None:
        """No geometry in, no polygons out."""
        assert expand_cutting_polygons([], 0.1) == []

    def test_zero_length_segment_engraves_a_dot(self) -> None:
        """A plunge point becomes a round cutter footprint."""
        segs = [StrokeSegment(Coordinate(0, 0), Coordinate(0, 0), True)]
        polygons = expand_cutting_polygons(segs, 0.1)
        assert len(polygons) == 1
        x_min, y_min, x_max, y_max = _bounds(polygons)
        assert x_min == pytest.approx(-0.05, abs=1e-9)
        assert x_max == pytest.approx(0.05, abs=1e-9)
        assert y_min == pytest.approx(-0.05, abs=1e-9)
        assert y_max == pytest.approx(0.05, abs=1e-9)


class TestArcs:
    """Arc flattening and annular expansion."""

    @staticmethod
    def _full_circle_arc(radius_units: float, sweep: float = 360.0) -> ArcSegment:
        """Full-circle (or partial) arc centered at the origin."""
        start = Coordinate(radius_units, 0)
        end_angle = math.radians(sweep)
        end = Coordinate(
            radius_units * math.cos(end_angle),
            radius_units * math.sin(end_angle),
        )
        return ArcSegment(
            start=start,
            end=end,
            center=Coordinate(0, 0),
            sweep_angle=sweep,
            is_cutting=True,
        )

    def test_full_circle_yields_annulus_with_hole(self) -> None:
        """A closed circle engraves a ring: one polygon, one interior ring."""
        polygons = expand_cutting_polygons([self._full_circle_arc(U)], 0.2)
        assert len(polygons) == 1
        assert len(polygons[0].interiors) == 1
        # Outer radius = R + w/2, inner = R - w/2.
        xs = [x for x, _y in polygons[0].exterior]
        assert max(xs) == pytest.approx(1.1, abs=1e-6)
        inner_xs = [x for x, _y in polygons[0].interiors[0]]
        assert max(inner_xs) == pytest.approx(0.9, abs=1e-3)

    def test_partial_arc_has_no_hole(self) -> None:
        """A 90-degree arc is an open ribbon (no interior ring)."""
        polygons = expand_cutting_polygons([self._full_circle_arc(U, sweep=90.0)], 0.2)
        assert len(polygons) == 1
        assert polygons[0].interiors == ()

    def test_zero_sweep_arc_engraves_a_dot(self) -> None:
        """A zero-sweep arc collapses to its plunge point."""
        polygons = expand_cutting_polygons([self._full_circle_arc(U, sweep=0.0)], 0.1)
        assert len(polygons) == 1
        x_min, _y_min, x_max, _y_max = _bounds(polygons)
        # Dot centered on the arc's start point at x = 1in.
        assert x_min == pytest.approx(0.95, abs=1e-6)
        assert x_max == pytest.approx(1.05, abs=1e-6)

    def test_rapid_arc_is_ignored(self) -> None:
        """Pen-up arcs engrave nothing."""
        arc = self._full_circle_arc(U)
        rapid = ArcSegment(
            start=arc.start,
            end=arc.end,
            center=arc.center,
            sweep_angle=arc.sweep_angle,
            is_cutting=False,
        )
        assert expand_cutting_polygons([rapid], 0.1) == []


class TestUnitConversion:
    """Inch conversion of the expanded geometry."""

    def test_width_is_physical_inches_not_plotter_units(self) -> None:
        """A 1016-unit (1in) line at width 0.25 spans 0.25 in vertically."""
        segs = [StrokeSegment(Coordinate(0, 0), Coordinate(HPGL_UNITS_PER_INCH, 0), True)]
        polygons = expand_cutting_polygons(segs, 0.25)
        _x_min, y_min, _x_max, y_max = _bounds(polygons)
        assert y_max - y_min == pytest.approx(0.25, abs=1e-9)
