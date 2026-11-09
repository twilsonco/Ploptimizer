"""Raster fill patterns for relief engraving (the pluggable fill algorithm).

A filler turns 2D fill regions into contiguous raster strokes plus the clean
outline pass that squares off each region's scalloped edge (fill first,
outline after -- the order the EngraveLab references engrave in). The
:class:`RasterFiller` protocol is the extension point for future patterns
(spiral, offset contour, ...); :class:`SSweepFiller` implements the S-Sweep
demonstrated by ``examples/plt_relief_engraving/A_relief_s-sweep_*.plt``:

- horizontal scanlines at ``pitch`` spacing: with an ``anchor`` (the
  border fill, the reference grid) rows sit at ``anchor.y0 + k*pitch``
  INCLUDING the bbox edges -- the edge rows span the full core width with
  zero inset (the reference's 30.48/985.52 tab rows), interior rows inset
  ``end_inset_fraction * pitch``; without an anchor each region's grid
  starts one pitch above its own floor (counters, the classic
  ``(miny + k*pitch)`` rule),
- every scanline interval inset by ``pitch / 2`` (the reference's span
  inset),
- rows are grouped into x-continuous LANES and serpentine-chained per
  lane: the link from a row's exit to the lane's next row entry is
  pen-down when the connecting segment stays inside the region (the
  reference's pitch-spaced vertical/diagonal connectors), a pen-up rapid
  otherwise,
- a region that splits (as the "A" legs diverge) yields one lane per side,
  each its own stroke chain with connectors, while the region's outline
  stays one contiguous ring per boundary loop (the TODO contract),
- the outline = the fill region's boundary rings (exterior + interior), each
  ring a closed stroke.

Coordinates: label-local inches, +y up (the ``TextChunkRecord`` frame). The
filler is pure geometry: no HPGL, no pen concept, no optimizer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Protocol, Sequence, Tuple, runtime_checkable

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

    def fill(
        self,
        regions: Sequence[Polygon],
        pitch: float,
        anchor: Optional[Tuple[float, float, float, float]] = None,
    ) -> List[List[Point]]:
        """Produce the contiguous raster strokes for ``regions``.

        Args:
            regions: Fill regions (label-local inches, +y up).
            pitch: Row spacing in inches.
            anchor: Optional ``(x0, y0, x1, y1)`` global row-grid frame:
                rows sit at ``y0 + k*pitch`` through ``y1`` (the reference
                grid spans the core bbox edges, the edge rows full span
                with zero inset, interior rows inset
                ``end_inset_fraction * pitch``); each region contributes
                the grid-line intervals inside it. ``None`` anchors per
                region (its own bbox).

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


def _row_grid(
    bounds: Tuple[float, float, float, float],
    pitch: float,
    end_inset_fraction: float,
    edge_rows: bool = True,
) -> List[Tuple[float, float]]:
    """The row grid over a bbox: ``(y, end_inset)`` lines.

    Anchored (``edge_rows=True``, the reference border grid): rows sit at
    ``y0 + k*pitch`` INCLUDING both bbox edges (the edge rows span the
    full core width with zero inset -- the reference's ``y=30.48`` /
    ``y=985.52`` tab rows), interior rows inset
    ``end_inset_fraction * pitch``. Per-region grids (counters, the
    default when no anchor is given) start one pitch above the floor with
    every row inset, as before.
    """
    x0, y0, x1, y1 = bounds
    lines: List[Tuple[float, float]] = []
    if edge_rows:
        count = int(math.floor((y1 - y0) / pitch + 1e-9))
        for k in range(count + 1):
            y = y0 + k * pitch
            edge = k == 0 or y >= y1 - 1e-9
            lines.append((y, 0.0 if edge else end_inset_fraction * pitch))
        if not math.isclose(lines[-1][0], y1, abs_tol=1e-9):
            lines.append((y1, 0.0))
        return lines
    y = y0 + pitch
    while y <= y1 - 1e-9:
        lines.append((y, end_inset_fraction * pitch))
        y += pitch
    return lines


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


def _lanes(rows: List[_Interval]) -> List[List[_Interval]]:
    """Group scanline intervals into x-continuous lanes (per region side).

    Walks the rows in ascending y; each interval joins the still-open lane
    (a lane whose last row is the immediately lower row) whose x-range
    overlaps it the most (strict overlap), else opens a new lane. A lane
    closes when a row has no overlapping interval -- a split hands both
    sides their own lane, a merge converges into the best-overlapping one.

    Args:
        rows: The region's kept intervals, ascending y (ties sorted by x).

    Returns:
        Lanes, each a list of intervals in ascending y, ordered by lane
        creation.
    """
    lanes: List[List[_Interval]] = []
    for row in rows:
        best: Optional[List[_Interval]] = None
        best_overlap = 0.0
        for lane in lanes:
            last = lane[-1]
            if last.y >= row.y - 1e-9:
                continue  # lane already consumed at this row
            overlap = min(row.x_max, last.x_max) - max(row.x_min, last.x_min)
            if overlap > best_overlap:
                best, best_overlap = lane, overlap
        if best is not None:
            best.append(row)
        else:
            lanes.append([row])
    return lanes


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

    def fill(
        self,
        regions: Sequence[Polygon],
        pitch: float,
        anchor: Optional[Tuple[float, float, float, float]] = None,
        edge_rows: bool = True,
    ) -> List[List[Point]]:
        """Raster-fill each region with chained serpentine strokes.

        Args:
            regions: Fill regions (label-local inches, +y up).
            pitch: Row spacing in inches (> 0).
            anchor: Optional global row-grid frame ``(x0, y0, x1, y1)``:
                one shared grid at ``y0 + k*pitch`` through ``y1`` (the
                reference grid spans the core bbox: the edge rows sit on
                the boundary full-span with zero inset, interior rows are
                inset ``end_inset_fraction * pitch``). ``None`` anchors the
                grid to each region's own bbox.
            edge_rows: With an anchor, include the two full-span bbox-edge
                rows (the reference's tab rows). Counters pass ``False``.

        Returns:
            Strokes: one vertex list per contiguous pen-down polyline.
        """
        if pitch <= 0.0:
            raise ValueError(f"raster pitch must be > 0, got {pitch!r}")
        grid = _row_grid(anchor, pitch, self.end_inset_fraction, edge_rows) if anchor else None
        strokes: List[List[Point]] = []
        for region in regions:
            lines = (
                grid
                if grid is not None
                else _row_grid(region.bounds, pitch, self.end_inset_fraction, edge_rows=False)
            )
            strokes.extend(self._fill_region(region, pitch, lines))
        return strokes

    def _fill_region(
        self,
        region: Polygon,
        pitch: float,
        grid: Optional[Sequence[Tuple[float, float]]] = None,
    ) -> List[List[Point]]:
        """Chain one region's scanlines into strokes (see :meth:`fill`).

        Rows are grouped into LANES: a chain of scanline intervals whose
        x-ranges overlap consecutive rows. A region that splits (the "A"
        legs diverging) yields one lane per side, and each lane
        serpentine-chains independently -- the pitch-spaced vertical/
        diagonal link from a row's exit to the next row's entry is part of
        the stroke (pen-down, exactly like the reference). Chaining by
        GLOBAL y-order row index interleaves the lanes at a split, every
        link crosses the island, and each row degrades to an isolated
        stroke -- the connector loss the reference comparison exposed.

        Args:
            region: The fill polygon.
            pitch: Row spacing (inches) -- used for the per-region grid
                when ``grid`` is None and for the pass floor.
            grid: Optional shared ``(y, end_inset)`` row lines.
        """
        rows: List[_Interval] = []
        lines = (
            list(grid)
            if grid is not None
            else _row_grid(region.bounds, pitch, self.end_inset_fraction)
        )
        for y, inset in lines:
            for x_lo, x_hi in _row_intervals(region, y):
                lo, hi = x_lo + inset, x_hi - inset
                if hi - lo >= self.min_pass_fraction * pitch:
                    rows.append(_Interval(y=y, x_min=lo, x_max=hi))
        strokes: List[List[Point]] = []
        for lane in _lanes(rows):
            strokes.extend(self._chain_lane(region, lane))
        return [stroke for stroke in strokes if len(stroke) >= 2]

    def _chain_lane(self, region: Polygon, lane: List[_Interval]) -> List[List[Point]]:
        """Serpentine one lane's rows into contiguous strokes.

        Args:
            region: The fill polygon (link containment guard).
            lane: The lane's rows in ascending y.

        Returns:
            One vertex list per pen-down chain.
        """
        strokes: List[List[Point]] = []
        current: List[Point] = []
        # Serpentine direction: lane-local row index parity flips the
        # traversal order, so consecutive rows meet at the SAME x end and
        # the pitch-spaced link is a short vertical/diagonal segment.
        for index, row in enumerate(lane):
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
        return strokes

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
