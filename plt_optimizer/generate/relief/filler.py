"""Raster fill patterns for relief engraving (the pluggable fill algorithm).

A filler turns 2D fill regions into contiguous raster strokes plus the clean
outline pass that squares off each region's scalloped edge (fill first,
outline after -- the order the EngraveLab references engrave in). The
:class:`RasterFiller` protocol is the extension point for future patterns
(spiral, offset contour, ...); :class:`SSweepFiller` implements the S-Sweep
demonstrated by ``examples/plt_relief_engraving/A_relief_s-sweep_*.plt``:

- horizontal scanlines at ``pitch`` spacing, anchored at the region's bbox
  bottom (``y = miny + pitch`` upward, the reference grid),
- every scanline interval inset by ``pitch / 2`` (the reference's span
  inset),
- rows chained into contiguous strokes: the link from a row's exit to the
  next row's entry is pen-down when the connecting segment stays inside the
  region (the reference's diagonal "down-and-right" links), a pen-up rapid
  otherwise,
- a region that splits (as the "A" legs diverge) becomes independent stroke
  chains, while the region's outline stays one contiguous ring per boundary
  loop (the TODO contract),
- the outline = the fill region's boundary rings (exterior + interior), each
  ring a closed stroke.

Coordinates: label-local inches, +y up (the ``TextChunkRecord`` frame). The
filler is pure geometry: no HPGL, no pen concept, no optimizer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Protocol, Sequence, Tuple, runtime_checkable

from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

#: Tolerance (inches) for endpoint/link containment tests (well under a
#: plotter unit: 1/1016 in ~ 0.001).
_TOL = 1e-6

Point = Tuple[float, float]


@runtime_checkable
class RasterFiller(Protocol):
    """A raster fill pattern: regions in, strokes out.

    Implementations must be pure functions of the region geometry and pitch
    so the relief pipeline (and tests) can drive them with plain polygons.
    """

    def fill(self, regions: Sequence[Polygon], pitch: float) -> List[List[Point]]:
        """Produce the contiguous raster strokes for ``regions``.

        Args:
            regions: Fill regions (label-local inches, +y up).
            pitch: Row spacing in inches.

        Returns:
            One point list per contiguous stroke (polyline vertices in
            cutting order; a stroke shorter than two points is skipped).
        """
        ...

    def outline(self, regions: Sequence[Polygon]) -> List[List[Point]]:
        """Produce the clean outline strokes for ``regions``.

        Args:
            regions: The same regions :meth:`fill` consumed.

        Returns:
            Closed rings (first point repeated at the end), exterior and
            interior boundaries of every region.
        """
        ...


@dataclass(frozen=True)
class _Interval:
    """One scanline interval with its row y (pre-inset)."""

    y: float
    x_min: float
    x_max: float


def _row_intervals(region: Polygon, y: float) -> List[Tuple[float, float]]:
    """Intersect ``region`` with the horizontal line at ``y``.

    Args:
        region: The fill polygon.
        y: Scanline height in inches.

    Returns:
        The ``(x_min, x_max)`` intervals of the cut, sorted by x. Touch
        slivers (zero-length cuts) are dropped.
    """
    bounds = region.bounds
    line = LineString([(bounds[0] - 1.0, y), (bounds[2] + 1.0, y)])
    cut = region.intersection(line)
    intervals: List[Tuple[float, float]] = []
    geoms = list(cut.geoms) if cut.geom_type.startswith("Multi") else [cut]
    for geom in geoms:
        if geom.is_empty:
            continue
        xs = [p[0] for p in geom.coords]
        lo, hi = min(xs), max(xs)
        if hi - lo > _TOL:
            intervals.append((lo, hi))
    intervals.sort()
    return intervals


class SSweepFiller:
    """S-Sweep: serpentine scanline raster with contiguous chaining.

    Attributes:
        min_pass_fraction: Shortest kept pass as a fraction of the pitch
            (scanline intervals shorter than this drop -- degenerate slivers
            at erosion apexes).
        end_inset_fraction: Row-end inset as a fraction of the pitch.
            ``1.25`` insets each row end by ``d/2`` (the capsule cap then
            lands exactly on the core boundary -- used for border-touching
            regions so row caps never cut into raised islands; the edge
            strip is removed by the outline ring's dilated cap). ``1.25``
            (the default) insets by d/2 so the capsule cap lands tangent to the
            core boundary (no cap intrusion into raised islands); ``0.5`` is
            the reference's counter-strip inset.
    """

    def __init__(self, min_pass_fraction: float = 0.25, end_inset_fraction: float = 0.5) -> None:
        """Store the pass floor and row-end inset fractions.

        Args:
            min_pass_fraction: Shortest kept pass as a fraction of pitch.
            end_inset_fraction: Row-end inset as a fraction of pitch.
        """
        self.min_pass_fraction = min_pass_fraction
        self.end_inset_fraction = end_inset_fraction

    def fill(self, regions: Sequence[Polygon], pitch: float) -> List[List[Point]]:
        """Raster-fill each region with chained serpentine strokes.

        Args:
            regions: Fill regions (label-local inches, +y up).
            pitch: Row spacing in inches (> 0).

        Returns:
            Strokes: one vertex list per contiguous pen-down polyline.
        """
        if pitch <= 0.0:
            raise ValueError(f"raster pitch must be > 0, got {pitch!r}")
        strokes: List[List[Point]] = []
        for region in regions:
            strokes.extend(self._fill_region(region, pitch))
        return strokes

    def _fill_region(self, region: Polygon, pitch: float) -> List[List[Point]]:
        """Chain one region's scanlines into strokes (see :meth:`fill`)."""
        minx, miny, maxx, maxy = region.bounds
        rows: List[_Interval] = []
        # Reference row grid: the first pass sits one pitch above the region
        # floor (0.06 box: rows at inset+pitch ... top). Row ENDS inset by
        # ``end_inset_fraction`` of the pitch: 0.5 = the reference's span
        # inset (counter strips), 1.25 = half the tool diameter (border
        # regions: the capsule cap lands on the core boundary, tangent to
        # the raised islands; the box-edge skin goes to the outline ring).
        y = miny + pitch
        while y <= maxy - 1e-9:
            for x_lo, x_hi in _row_intervals(region, y):
                inset = self.end_inset_fraction * pitch
                lo, hi = x_lo + inset, x_hi - inset
                if hi - lo >= self.min_pass_fraction * pitch:
                    rows.append(_Interval(y=y, x_min=lo, x_max=hi))
            y += pitch
        strokes: List[List[Point]] = []
        current: List[Point] = []
        # Serpentine direction: row index parity flips the x traversal order
        # and the per-row interval order.
        for index, row in enumerate(rows):
            left_to_right = index % 2 == 0
            start = (row.x_min, row.y) if left_to_right else (row.x_max, row.y)
            end = (row.x_max, row.y) if left_to_right else (row.x_min, row.y)
            if not current:
                current = [start, end]
                continue
            link = LineString([current[-1], start])
            traverse = LineString([start, end])
            prepared = region.buffer(_TOL)
            if prepared.covers(link) and prepared.covers(traverse):
                if current[-1] != start:
                    current.append(start)
                current.append(end)
            else:
                strokes.append(current)
                current = [start, end]
        if current:
            strokes.append(current)
        return [stroke for stroke in strokes if len(stroke) >= 2]

    def outline(self, regions: Sequence[Polygon]) -> List[List[Point]]:
        """Return each region's boundary rings as closed strokes.

        Args:
            regions: The filled regions.

        Returns:
            One point list per ring (exterior and interior boundaries; the
            first vertex repeated at the end to close the loop).
        """
        rings: List[List[Point]] = []
        for region in regions:
            polygons: List[Polygon] = []
            if isinstance(region, Polygon):
                polygons = [region]
            elif isinstance(region, MultiPolygon):
                polygons = list(region.geoms)
            for polygon in polygons:
                for ring in (polygon.exterior, *polygon.interiors):
                    coords = list(ring.coords)
                    if len(coords) >= 2:
                        rings.append([(float(x), float(y)) for x, y in coords])
        return rings


def region_polygons(geometry: BaseGeometry) -> List[Polygon]:
    """Flatten any polygonal geometry into a list of Polygons.

    Args:
        geometry: A Polygon, MultiPolygon, or empty geometry.

    Returns:
        The polygon list (empty geometries yield ``[]``).
    """
    if geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return [polygon for polygon in geometry.geoms if not polygon.is_empty]
    # GeometryCollection (defensive): keep polygonal members.
    out: List[Polygon] = []
    for geom in getattr(geometry, "geoms", []):
        out.extend(region_polygons(geom))
    return out


def polyline_length(points: Sequence[Point]) -> float:
    """Total Euclidean length of a polyline (in inches).

    Args:
        points: Vertices in order.

    Returns:
        The summed segment lengths (0.0 for fewer than two points).
    """
    total = 0.0
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        total += math.hypot(x1 - x0, y1 - y0)
    return total
