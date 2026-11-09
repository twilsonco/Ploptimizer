"""Engrave-region geometry for relief engraving.

The raised islands are the exact silhouette normal engraving would cut: every
engraved stroke of the label (text, decorations, markers -- anything on a
text pen) widened to its ENGRAVED width -- the PHYSICAL cutter diameter
(relief neglects ``cutter_buffer_text`` under all circumstances; centerline
strokes buffer by `width / 2`; closed contours fill as polygons, TTF-style).
The engrave region
is the label box (optionally inset for `relief_engraving_preserve_border`)
minus the island union, in label-local inches (+y up, the
``TextChunkRecord`` frame, so the plate transform applies to relief strokes
exactly like text).

Drill holes never enter the region model: relief fills over them (the holes
are dropped from the structural emission by the export glue).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Sequence, Tuple

from shapely.geometry import LinearRing, LineString, MultiPolygon, Polygon
from shapely.geometry import Point as ShapelyPoint
from shapely.ops import unary_union

from plt_optimizer.generate.label_renderer import RenderedLabel
from plt_optimizer.generate.relief.filler import region_polygons
from plt_optimizer.generate.text_geometry import ArcSeg, LineSeg, Stroke

Point = Tuple[float, float]

#: Endpoint distance (inches) below which a stroke counts as a closed
#: contour (a TTF fill ring) instead of an open centerline stroke. A full
#: plotter unit is 1/1016 in; glyph rings close exactly.
_CLOSED_TOL = 1e-6

#: Arc flattening tolerance for island silhouettes (inches). Islands feed
#: shapely booleans only (never emission), so this can be looser than the
#: emission's arc fits while staying well under a plotter unit.
_ARC_FLATTEN_TOL = 2.5e-4


@dataclass(frozen=True)
class EngraveRegions:
    """The computed relief region set for one label.

    Attributes:
        box: The engrave box polygon (label rect, border band removed when
            `relief_engraving_preserve_border` is on).
        islands: Union polygon of the raised silhouettes (may be empty).
        engrave: The engrave region: ``box - islands``.
        interior: The engrave components NOT touching the box boundary
            (text counters like the "A" hole): their core erodes by the
            cutter RADIUS on the sides and by the FULL DIAMETER on the
            floor edge (the reference's "A" counter triangle: base at the
            crossbar island edge minus d, sides at the leg island edges
            minus r; see ``pipeline._clip_counter_floor``), and stays
            sharp-cornered (the counter outline is a plain triangle).
        outer: The engrave components touching the box boundary (the
            background): these erode by the cutter RADIUS per pass (the
            tool edge reaches the label edge tangentially).
        max_ink_width: The widest engraved stroke width feeding the islands
            (the skeleton's own engraved width, inches).
    """

    box: Polygon
    islands: Polygon
    engrave: MultiPolygon
    interior: Tuple[Polygon, ...]
    outer: Tuple[Polygon, ...]
    max_ink_width: float = 0.0


def stroke_to_points(stroke: Stroke, tol: float = _ARC_FLATTEN_TOL) -> List[Point]:
    """Flatten one arc-native stroke to a dense polyline.

    Args:
        stroke: The stroke (label-local inches, +y up).
        tol: Maximum arc chord deviation in inches.

    Returns:
        The vertex list (pen-up position dropped). Empty for segment-less
        strokes.
    """
    points: List[Point] = []
    for segment in stroke.segments:
        if isinstance(segment, LineSeg):
            if not points:
                points.append((segment.start.real, segment.start.imag))
            points.append((segment.end.real, segment.end.imag))
            continue
        assert isinstance(segment, ArcSeg)  # noqa: S101 - two segment types
        center = segment.center
        radius = segment.radius
        start_angle = math.atan2(segment.start.imag - center.imag, segment.start.real - center.real)
        sweep = math.radians(segment.sweep_deg)
        if not points:
            points.append((segment.start.real, segment.start.imag))
        if radius <= 0.0 or sweep == 0.0:
            points.append((segment.end.real, segment.end.imag))
            continue
        # Chord deviation tol on a circle of radius R over angle step a:
        # dev = R * (1 - cos(a / 2))  =>  a = 2 * acos(1 - tol / R).
        ratio = min(1.0, tol / radius)
        step = 2.0 * math.acos(max(-1.0, 1.0 - ratio))
        count = max(2, int(math.ceil(abs(sweep) / step)))
        for index in range(1, count + 1):
            angle = start_angle + sweep * (index / count)
            points.append(
                (center.real + radius * math.cos(angle), center.imag + radius * math.sin(angle))
            )
    return points


def _stroke_is_closed(points: Sequence[Point]) -> bool:
    """True when a flattened polyline is a closed ring."""
    if len(points) < 4:
        return False
    return math.dist(points[0], points[-1]) <= _CLOSED_TOL


def _rings_to_polygons(rings: List[List[Point]]) -> List[Polygon]:
    """Convert nested closed rings to polygons by containment parity.

    Even nesting depth (0, 2, ...) = filled shell; odd depth = hole in its
    container (a TTF "O" outer ring fills, its inner ring punches the
    counter).

    Args:
        rings: Closed vertex rings (any orientation).

    Returns:
        The constructed polygons (shells with holes applied).
    """
    areas = [abs(LinearRing(ring).area) for ring in rings]
    order = sorted(range(len(rings)), key=lambda index: -areas[index])
    polygons: List[Polygon] = []
    parents: List[int] = []  # Parallel to ``polygons``: container slot, -1 top.
    for index in order:
        ring = rings[index]
        try:
            polygon = Polygon(ring)
        except Exception:  # noqa: BLE001 - shapely rejects degenerate rings
            continue
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
            if polygon.is_empty:
                continue
        probe = ShapelyPoint(ring[0])
        parent = -1
        for pos, built in enumerate(polygons):
            # Container = smallest enclosing polygon; rings are sorted by
            # area descending, so the LAST enclosing polygon built is the
            # smallest one.
            if Polygon(built.exterior.coords).covers(probe):
                parent = pos
        parents.append(parent)
        polygons.append(polygon)
    with_holes: List[Polygon] = []
    for pos, polygon in enumerate(polygons):
        depth = 0
        walk = pos
        while parents[walk] >= 0:
            depth += 1
            walk = parents[walk]
        if depth % 2 == 1:
            # Hole of its container (applied there); not standalone geometry.
            continue
        holes = [rings[order[child]] for child, parent in enumerate(parents) if parent == pos]
        if holes:
            polygon = Polygon(polygon.exterior.coords, holes)
        with_holes.append(polygon)
    return with_holes


def _polygon_slot(order: List[int], parent_of: List[int], child: int) -> int:
    """Container slot of a ring (``-1`` when top-level)."""
    return parent_of[child]


def island_polygons(ink: Sequence[Tuple[Sequence[Point], float]]) -> Polygon:
    """Build the raised-island union from engraved ink.

    Closed polylines (TTF fill contours, ring skeletons) fill as polygons
    with containment-parity holes; open polylines (skeleton fonts,
    underline, markers) buffer by ``width / 2`` (the swept disk of the
    cutting tool, round caps/joins).

    Args:
        ink: ``(polyline, engraved width)`` pairs in label-local inches
            (+y up).

    Returns:
        The island union (an empty polygon when ``ink`` is empty).
    """
    shells: List[Polygon] = []
    rings: List[List[Point]] = []
    for points, width in ink:
        if len(points) < 2:
            continue
        if _stroke_is_closed(points):
            rings.append(list(points))
        else:
            line = LineString(points)
            shells.append(line.buffer(max(width, _CLOSED_TOL) / 2.0))
    shells.extend(_rings_to_polygons(rings))
    if not shells:
        return Polygon()
    merged = unary_union(shells)
    return merged if isinstance(merged, Polygon) else MultiPolygon(region_polygons(merged))


def build_engrave_regions(
    label_width: float,
    label_height: float,
    ink: Sequence[Tuple[Sequence[Point], float]],
    preserve_border: bool,
    border_band: float = 0.0,
    box_y_center: float = 0.0,
) -> EngraveRegions:
    """Compute the box / islands / engrave region set for one label.

    Args:
        label_width: Label width in inches.
        label_height: Label height in inches.
        ink: The label's engraved ink strokes with their engraved widths
            (see :func:`island_polygons`).
        preserve_border: Keep the border band raised (exclude it from the
            engrave region).
        border_band: The band width to preserve: the boundary/hole cutter's
            engraved width plus the coarsest relief cutter's radius (the
            relief tool's edge must not break the border's engraved stroke).
        box_y_center: Y center of the label box in the render frame (the
            ink's Y center -- the plate transform centers the text pen on
            the label, so the fill box rides the ink, not the origin).

    Returns:
        The :class:`EngraveRegions` for the label.
    """
    # The render frame: x spans the label width (content centering lands
    # the ink block on w/2), y is centered on the ink (see
    # ``plate_optimizer._label_transform``: the text pen gains a
    # height/2 - band - hole shift, and the border box follows the ink).
    y0 = box_y_center - label_height / 2.0
    y1 = box_y_center + label_height / 2.0
    box = Polygon([(0.0, y0), (label_width, y0), (label_width, y1), (0.0, y1)])
    if preserve_border and border_band > 0.0:
        # Miter (sharp) inset: the preserved border band follows the label
        # rect corners, so the engrave box is the rect inset by the band.
        inset = box.buffer(-border_band, join_style=2)
        box = inset if isinstance(inset, Polygon) else Polygon()
    islands = island_polygons(ink)
    engrave = box.difference(islands)
    components = region_polygons(engrave)
    interior: List[Polygon] = []
    outer: List[Polygon] = []
    box_bounds = box.bounds if not box.is_empty else (0.0, 0.0, 0.0, 0.0)
    for component in components:
        minx, miny, maxx, maxy = component.bounds
        touches = (
            abs(minx - box_bounds[0]) <= 1e-6
            or abs(miny - box_bounds[1]) <= 1e-6
            or abs(maxx - box_bounds[2]) <= 1e-6
            or abs(maxy - box_bounds[3]) <= 1e-6
        )
        (outer if touches else interior).append(component)
    return EngraveRegions(
        box=box,
        islands=islands,
        engrave=MultiPolygon(components),
        interior=tuple(interior),
        outer=tuple(outer),
        max_ink_width=max((width for _points, width in ink), default=0.0),
    )


def label_ink(
    rendered: RenderedLabel, pen_widths: dict[int, float]
) -> List[Tuple[List[Point], float]]:
    """Extract the label's engraved ink polylines with their widths.

    Walks the rendered chunk records (all text pens) in label-local (+y up)
    coordinates. Widths are the pen's PHYSICAL cutter diameter only:
    relief neglects ``cutter_buffer_text`` under all circumstances, so the
    per-stroke :attr:`TextChunkRecord.stroke_widths` map (the ``<cb:D>``
    buffer mechanism) is deliberately not consulted -- a per-character
    buffer must not inflate the raised islands either.

    Args:
        rendered: The rendered label.
        pen_widths: Mapping of text pen number to its PHYSICAL cutter
            width in inches (relief neglects ``cutter_buffer_text``; see
            :func:`plt_optimizer.generate.relief.pipeline.pen_widths_for_label`).

    Returns:
        ``(polyline, engraved width)`` pairs for the island builder.
    """
    ink: List[Tuple[List[Point], float]] = []
    for record in rendered.text_chunks:
        width = pen_widths.get(record.pen, 0.0)
        for block in record.blocks:
            for stroke in block.strokes:
                if stroke.is_empty:
                    continue
                points = stroke_to_points(stroke)
                if len(points) >= 2:
                    ink.append((points, width))
    return ink


__all__ = [
    "EngraveRegions",
    "build_engrave_regions",
    "island_polygons",
    "label_ink",
    "stroke_to_points",
]
