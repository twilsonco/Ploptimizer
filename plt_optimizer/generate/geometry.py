"""Geometric primitives for the label generation pipeline.

This module hosts small, pure geometric predicates shared by the rendering
and layout layers of the generate pipeline. Everything here is
coordinate-frame agnostic: callers are responsible for providing text
bounding boxes and ink polylines in the *same* frame (the generate pipeline
uses label-local, y-up coordinates before plate placement).

The collision-specific predicates (the signed circle-vs-AABB gap, the
circle-vs-ellipse primitives, ``CollisionResult``, ``HoleCenter``) live in
:mod:`plt_optimizer.generate.collision.geometry`.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

# An axis-aligned bounding box as ``(x_min, y_min, x_max, y_max)``.
TextBounds = Tuple[float, float, float, float]


def aabb_vertical_gap(upper: TextBounds, lower: TextBounds) -> float:
    """Compute the signed vertical clearance between an upper box and a lower box.

    The gap is measured only where the two boxes overlap horizontally -- the
    region where their engraved strokes could actually meet. Two boxes whose
    X spans are disjoint never collide, so they report
    :data:`math.inf` ("infinitely far apart").

    - ``gap > 0``: the boxes are separated by ``gap`` inches vertically.
    - ``gap == 0``: they touch exactly (the engraved strokes are tangent).
    - ``gap < 0``: they overlap; ``abs(gap)`` is the penetration depth.

    The primary consumer is the automatic descending-character relief, which
    measures how close a glyph hanging below its baseline comes to a glyph on
    the following line.

    Args:
        upper: ``(x_min, y_min, x_max, y_max)`` of the upper box.
        lower: ``(x_min, y_min, x_max, y_max)`` of the lower box.

    Returns:
        The signed clearance in the caller's length units, or ``inf`` when
        the boxes do not overlap horizontally.
    """
    upper_x_min, upper_y_min, upper_x_max, _upper_y_max = upper
    lower_x_min, _lower_y_min, lower_x_max, lower_y_max = lower

    overlap = min(upper_x_max, lower_x_max) - max(upper_x_min, lower_x_min)
    if overlap <= 0.0:
        return math.inf

    return upper_y_min - lower_y_max


def arc_swept_bounds(
    center_x: float,
    center_y: float,
    radius: float,
    start_angle: float,
    sweep_angle: float,
) -> TextBounds:
    """Return the axis-aligned bounding box of an arc's *swept* portion.

    Single-line fonts approximate many near-straight glyph strokes with
    huge-radius best-fit arcs, so an arc's full circle dwarfs the actual cut.
    Measuring only the swept extent (the two endpoints plus any cardinal
    angle the arc passes through) keeps a glyph's footprint tight. A
    full-revolution arc still yields its whole circle.

    Angles are in degrees and follow the HPGL ``AA`` convention used by
    :class:`~plt_optimizer.core.models.ArcSegment`: the arc starts at
    ``start_angle`` and ends at ``start_angle + sweep_angle`` (the sign of
    ``sweep_angle`` encodes direction). The computation is frame-agnostic --
    it operates in whatever Cartesian frame the caller supplies.

    Args:
        center_x: Arc center X coordinate.
        center_y: Arc center Y coordinate.
        radius: Arc radius (>= 0).
        start_angle: Angle of the arc's start point, in degrees.
        sweep_angle: Signed sweep from start to end, in degrees.

    Returns:
        ``(x_min, y_min, x_max, y_max)`` of the swept arc in the caller's
        coordinate units.
    """
    theta_start = math.radians(start_angle)
    theta_end = theta_start + math.radians(sweep_angle)
    lo, hi = min(theta_start, theta_end), max(theta_start, theta_end)

    # Collect the endpoints plus every cardinal angle (multiple of 90 deg)
    # the swept range passes through -- those are the only places an arc can
    # reach an axis-aligned extremum.
    quarter = math.pi / 2.0
    angles = [theta_start, theta_end]
    k = math.floor(lo / quarter) - 1
    while True:
        cardinal = k * quarter
        if cardinal > hi:
            break
        if cardinal >= lo:
            angles.append(cardinal)
        k += 1

    xs = [center_x + radius * math.cos(a) for a in angles]
    ys = [center_y + radius * math.sin(a) for a in angles]
    return (min(xs), min(ys), max(xs), max(ys))


# A point as ``(x, y)``.
Point = Tuple[float, float]

# A connected chain of pen-down vertices -- one engraved stroke, with arcs
# already flattened to chord-bounded vertices. A single point is a valid
# (degenerate) polyline: it describes a dot of ink, e.g. an ``i``'s tittle.
Polyline = Tuple[Point, ...]

# Everything one glyph engraves: the polylines of its strokes. Empty when the
# glyph has no ink (a space, or ink clipped away entirely).
Ink = Tuple[Polyline, ...]

# A segment in flat storage, for the spatial grid: ``(x1, y1, x2, y2)``.
_Segment = Tuple[float, float, float, float]

# Degenerate-segment threshold. Emitted geometry is integer plotter units
# (1 unit = 1 / ``HPGL_UNITS_PER_INCH`` inch) and the flattening chord tolerance
# is 0.0002in, so this
# only absorbs float noise, never real geometry.
_DEGENERATE_EPSILON: float = 1e-12

# Floor for a :class:`InkGrid` cell size (a zero required clearance would
# otherwise produce zero-area cells).
_MIN_GRID_CELL: float = 1e-6


def point_segment_gap(point: Point, start: Point, end: Point) -> float:
    """Return the distance from a point to a segment (``0.0`` when on it).

    Args:
        point: The query point ``(x, y)``.
        start: Segment start ``(x, y)``.
        end: Segment end ``(x, y)``.

    Returns:
        The Euclidean distance from ``point`` to the closest point of the
        segment, in the caller's length units.
    """
    px, py = point
    sx, sy = start
    ex, ey = end
    dx, dy = ex - sx, ey - sy
    length_squared = dx * dx + dy * dy
    if length_squared <= _DEGENERATE_EPSILON * _DEGENERATE_EPSILON:
        return math.hypot(px - sx, py - sy)
    # Projection parameter, clamped to the segment (beyond it the closest
    # point is the corresponding endpoint).
    t = ((px - sx) * dx + (py - sy) * dy) / length_squared
    t = min(1.0, max(0.0, t))
    return math.hypot(px - (sx + t * dx), py - (sy + t * dy))


def segment_gap(
    a_start: Point,
    a_end: Point,
    b_start: Point,
    b_end: Point,
) -> float:
    """Return the minimum distance between two segments (``0.0`` when crossing).

    The classic closest-points solution for two segments in a plane: the
    parameters ``(s, t)`` minimising ``|A(s) - B(t)|`` are solved on the
    clamped unit square, which handles the parallel and degenerate cases and
    returns exactly ``0.0`` for crossing segments (the min of the four
    endpoint-to-segment distances would report a false positive there).

    Args:
        a_start: First segment's start ``(x, y)``.
        a_end: First segment's end ``(x, y)``.
        b_start: Second segment's start ``(x, y)``.
        b_end: Second segment's end ``(x, y)``.

    Returns:
        The Euclidean distance between the two segments, in the caller's
        length units.
    """
    ax, ay = a_start
    bx, by = a_end
    cx, cy = b_start
    dx, dy = b_end

    d1x, d1y = bx - ax, by - ay  # first segment's direction
    d2x, d2y = dx - cx, dy - cy  # second segment's direction
    rx, ry = ax - cx, ay - cy
    a = d1x * d1x + d1y * d1y
    e = d2x * d2x + d2y * d2y
    f = d2x * rx + d2y * ry

    degenerate = _DEGENERATE_EPSILON * _DEGENERATE_EPSILON
    if a <= degenerate and e <= degenerate:
        # Both segments are points.
        return math.hypot(rx, ry)
    if a <= degenerate:
        s = 0.0
        t = min(1.0, max(0.0, f / e))
    else:
        c = d1x * rx + d1y * ry
        if e <= degenerate:
            s = min(1.0, max(0.0, -c / a))
            t = 0.0
        else:
            b = d1x * d2x + d1y * d2y
            denominator = a * e - b * b
            if denominator > degenerate:
                # Ordinary case: the segments are not parallel.
                s = min(1.0, max(0.0, (b * f - c * e) / denominator))
            else:
                # Parallel: any s on the first segment is a local optimum;
                # the clamp below recovers the best t for it.
                s = 0.0
            t = (b * s + f) / e
            if t < 0.0:
                t = 0.0
                s = min(1.0, max(0.0, -c / a))
            elif t > 1.0:
                t = 1.0
                s = min(1.0, max(0.0, (b - c) / a))

    return math.hypot((ax + d1x * s) - (cx + d2x * t), (ay + d1y * s) - (cy + d2y * t))


def _polyline_segments(polyline: Polyline) -> List[_Segment]:
    """Return a polyline's segments as flat ``(x1, y1, x2, y2)`` tuples.

    A single-vertex polyline (a dot of ink) contributes a degenerate segment,
    which :func:`segment_gap` measures as a point.
    """
    if not polyline:
        return []
    if len(polyline) == 1:
        x, y = polyline[0]
        return [(x, y, x, y)]
    return [(x1, y1, x2, y2) for (x1, y1), (x2, y2) in zip(polyline[:-1], polyline[1:])]


def polyline_gap(upper: Polyline, lower: Polyline) -> float:
    """Return the minimum distance between two polylines.

    Args:
        upper: One chain of pen-down vertices.
        lower: Another chain of pen-down vertices.

    Returns:
        The smallest segment-to-segment distance between the two chains, in
        the caller's length units. ``math.inf`` when either chain has no ink
        (nothing to measure -- the pair can never collide).
    """
    first = _polyline_segments(upper)
    second = _polyline_segments(lower)
    if not first or not second:
        return math.inf
    # Exact prefilter: a segment pair's true distance is >= the gap between
    # its bounding boxes, so a pair whose boxes sit at or beyond the current
    # best can never improve it and skips segment_gap entirely. Far-apart
    # chains (the overwhelming majority on a long text line) cost a handful
    # of comparisons per pair instead of the full closest-points solve; the
    # measured minimum is unchanged (skipped pairs provably cannot be the
    # minimum).
    second_boxes = [
        (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)) for (x1, y1, x2, y2) in second
    ]
    best = math.inf
    for ax1, ay1, ax2, ay2 in first:
        ax_min = ax1 if ax1 <= ax2 else ax2
        ax_max = ax2 if ax1 <= ax2 else ax1
        ay_min = ay1 if ay1 <= ay2 else ay2
        ay_max = ay2 if ay1 <= ay2 else ay1
        a_start = (ax1, ay1)
        a_end = (ax2, ay2)
        for (bx1, by1, bx2, by2), (bx_min, by_min, bx_max, by_max) in zip(second, second_boxes):
            dx = ax_min - bx_max
            other = bx_min - ax_max
            if other > dx:
                dx = other
            if dx < 0.0:
                dx = 0.0
            dy = ay_min - by_max
            other = by_min - ay_max
            if other > dy:
                dy = other
            if dy < 0.0:
                dy = 0.0
            if dx * dx + dy * dy >= best * best:
                continue
            gap = segment_gap(a_start, a_end, (bx1, by1), (bx2, by2))
            if gap < best:
                best = gap
                if best <= 0.0:
                    return 0.0
    return best


def ink_gap(upper: Ink, lower: Ink) -> float:
    """Return the minimum distance between two glyphs' engraved ink.

    This is the metric the descending-character relief uses: rolling a circle
    of diameter ``required`` along one glyph's ink collides with the other
    glyph exactly when this distance falls below ``required``. Unlike
    :func:`aabb_vertical_gap` it is a true 2D distance, so a glyph poking
    *sideways* into the gap below (a ``g`` hook reaching an ``i`` dot beside
    it) measures its real proximity instead of reporting ``inf``.

    Args:
        upper: The upper glyph's ink (the polylines of its strokes).
        lower: The lower glyph's ink.

    Returns:
        The smallest distance between any pair of the two glyphs' segments, in
        the caller's length units. ``math.inf`` when either glyph has no ink.
    """
    # Exact prefilter (same argument as :func:`polyline_gap`): a polyline
    # pair's true distance is >= the gap between its bounding boxes, so
    # far-apart stroke pairs skip the chain scan entirely.
    lower_boxes = [polyline_bounds(polyline) for polyline in lower]
    best = math.inf
    for first in upper:
        first_box = polyline_bounds(first)
        if first_box is None:
            continue
        for second, second_box in zip(lower, lower_boxes):
            if second_box is None:
                continue
            if bbox_gap(first_box, second_box) >= best:
                continue
            gap = polyline_gap(first, second)
            if gap < best:
                best = gap
                if best <= 0.0:
                    return 0.0
    return best


def polyline_bounds(polyline: Polyline) -> Optional[TextBounds]:
    """Return the axis-aligned bounding box of a polyline's vertices.

    Args:
        polyline: The chain of vertices to box.

    Returns:
        ``(x_min, y_min, x_max, y_max)`` of the vertices, or ``None`` for an
        empty polyline (no ink to box).
    """
    if not polyline:
        return None
    x_min = x_max = polyline[0][0]
    y_min = y_max = polyline[0][1]
    for x, y in polyline[1:]:
        if x < x_min:
            x_min = x
        elif x > x_max:
            x_max = x
        if y < y_min:
            y_min = y
        elif y > y_max:
            y_max = y
    return (x_min, y_min, x_max, y_max)


def bbox_gap(a: TextBounds, b: TextBounds) -> float:
    """Return the Euclidean gap between two axis-aligned boxes.

    The gap is ``0.0`` when the boxes overlap or touch, and the distance
    between the closest corners when they are separated on one or both
    axes. It is a lower bound on the distance between any two point sets
    inside the boxes, which makes ``bbox_gap >= limit`` an exact prefilter
    for the predicate ``ink gap < limit``.

    Args:
        a: ``(x_min, y_min, x_max, y_max)`` of the first box.
        b: ``(x_min, y_min, x_max, y_max)`` of the second box.

    Returns:
        The box-to-box distance in the caller's length units.
    """
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


def ink_bounds(ink: Ink) -> Optional[TextBounds]:
    """Return the axis-aligned bounding box of a glyph's complete ink.

    Args:
        ink: The glyph's polylines.

    Returns:
        ``(x_min, y_min, x_max, y_max)`` over every vertex, or ``None`` when
        the ink is empty (nothing to box).
    """
    x_min = y_min = x_max = y_max = 0.0
    seen = False
    for polyline in ink:
        box = polyline_bounds(polyline)
        if box is None:
            continue
        if not seen:
            x_min, y_min, x_max, y_max = box
            seen = True
            continue
        if box[0] < x_min:
            x_min = box[0]
        if box[1] < y_min:
            y_min = box[1]
        if box[2] > x_max:
            x_max = box[2]
        if box[3] > y_max:
            y_max = box[3]
    if not seen:
        return None
    return (x_min, y_min, x_max, y_max)


class InkGrid:
    """Uniform spatial-hash grid over a glyph set's ink, for near-neighbour gaps.

    The descending-character relief compares every descending glyph of a line
    against every glyph of the line below, and re-measures them on each ramp
    bisection step. A label's line carries hundreds of glyphs, so the
    brute-force :func:`ink_gap` is too slow to be the inner loop. This grid
    buckets the lower line's segments into cells of the *required clearance*,
    so a query tests only the segments that can possibly reach it.

    The cell size is the whole required clearance, which makes the decision
    ``gap < required`` **exact**: two segments closer than the cell size always
    have bounding boxes that intersect the query's box expanded by one cell,
    so the pair is always tested. The returned minimum is therefore exact
    whenever it falls below the cell size -- the regime the relief acts on --
    and an upper bound above it (the grid tests a subset of the pairs, so it
    can report more air than there really is, never less). Callers that need a
    distance beyond the cell size use :func:`ink_gap`.

    Attributes:
        cell_size: The grid's cell edge, in the caller's length units.

    Args:
        ink: Every polyline to index (typically all glyphs of one text line).
        cell_size: Cell edge length -- pass the required clearance. Values at
            or below zero fall back to :data:`_MIN_GRID_CELL`.
    """

    cell_size: float
    _cells: Dict[Tuple[int, int], List[_Segment]]

    def __init__(self, ink: Ink, cell_size: float) -> None:
        self.cell_size = cell_size if cell_size > 0.0 else _MIN_GRID_CELL
        self._cells = {}
        for polyline in ink:
            for segment in _polyline_segments(polyline):
                x1, y1, x2, y2 = segment
                (min_x, max_x), (min_y, max_y) = _segment_span(x1, x2), _segment_span(y1, y2)
                for cell in self._cells_covered(min_x, min_y, max_x, max_y):
                    self._cells.setdefault(cell, []).append(segment)

    def _cells_covered(
        self, x_min: float, y_min: float, x_max: float, y_max: float
    ) -> List[Tuple[int, int]]:
        """Return every cell index whose square intersects a bounding box."""
        cell = self.cell_size
        i_start = math.floor(x_min / cell)
        i_stop = math.floor(x_max / cell)
        j_start = math.floor(y_min / cell)
        j_stop = math.floor(y_max / cell)
        return [(i, j) for i in range(i_start, i_stop + 1) for j in range(j_start, j_stop + 1)]

    def min_gap(self, query: Ink) -> float:
        """Return the nearest indexed ink to a glyph's ink.

        Args:
            query: The querying glyph's ink.

        Returns:
            The smallest distance from any of ``query``'s segments to any
            indexed segment sharing a cell with it. Exact below
            :attr:`cell_size`, an upper bound above it. ``math.inf`` when
            either side has no ink.
        """
        best = math.inf
        cell = self.cell_size
        for polyline in query:
            for x1, y1, x2, y2 in _polyline_segments(polyline):
                a_start = (x1, y1)
                a_end = (x2, y2)
                (min_x, max_x), (min_y, max_y) = (
                    _segment_span(x1, x2),
                    _segment_span(y1, y2),
                )
                for cell_index in self._cells_covered(
                    min_x - cell, min_y - cell, max_x + cell, max_y + cell
                ):
                    for segment in self._cells.get(cell_index, ()):
                        bx1, by1, bx2, by2 = segment
                        gap = segment_gap(a_start, a_end, (bx1, by1), (bx2, by2))
                        if gap < best:
                            best = gap
                            if best <= 0.0:
                                return 0.0
        return best


def _segment_span(first: float, second: float) -> Tuple[float, float]:
    """Return the ``(min, max)`` span of two segment endpoint coordinates."""
    return (min(first, second), max(first, second))


#: Bound on :func:`ink_grid_for`'s memo. The relief builds one grid per
#: (line pair, pass, probe render); a big label's line ink indexes cost
#: milliseconds to build, so a small LRU covering the label's working set
#: absorbs the whole repeat pattern while bounding memory to a few grids.
_INK_GRID_CACHE_SIZE: int = 16


def ink_grid_for(ink: Ink, cell_size: float) -> InkGrid:
    """Return an :class:`InkGrid` over ``ink``, memoized by ``(ink, cell_size)``.

    The relief re-indexes the SAME line ink over and over: every fixpoint
    pass re-measures the unchanged lines, and every collision-sweep step
    re-renders a label whose non-compressed lines are byte-identical (the
    sweep scales X only, on colliding lines). Grid construction is a pure
    function of hashable inputs and :class:`InkGrid` is read-only after
    ``__init__`` (:meth:`InkGrid.min_gap` mutates nothing), so a shared
    instance is exact by construction.

    Non-hashable ink (hand-built lists in tests) falls back to a fresh
    grid -- identical behavior, just unmemoized.

    Args:
        ink: Every polyline to index (see :class:`InkGrid`).
        cell_size: Cell edge length -- pass the required clearance.

    Returns:
        A (possibly shared) grid indexing ``ink`` at ``cell_size``.
    """
    try:
        return _cached_ink_grid(ink, cell_size)
    except TypeError:  # unhashable ink (e.g. lists of lists) -- no memo
        return InkGrid(ink, cell_size)


@lru_cache(maxsize=_INK_GRID_CACHE_SIZE)
def _cached_ink_grid(ink: Ink, cell_size: float) -> InkGrid:
    """Build the memoized :class:`InkGrid` for one ``(ink, cell_size)`` key."""
    return InkGrid(ink, cell_size)
