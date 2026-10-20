"""True-width stroke expansion for physical toolpath rendering.

This module converts parsed HPGL toolpath segments (plotter units) into
closed polygon rings (inches) that represent the *physical* ink a cutter
of a given diameter engraves. Each contiguous run of pen-down segments is
expanded into a constant-width corridor around its centerline -- a
rectangle for a straight run, an annular band for a full-circle arc, and a
miter-free round-joined ribbon for polylines -- so a diagnostic plot can
show every stroke at its true engraved width on inch-based axes.

The expansion is pure geometry (shapely ``buffer``); matplotlib is never
imported here, so this module stays importable on matplotlib-free
machines. Coordinates are converted from plotter units to inches via
``core/units.py`` (1016 units/inch, root AGENTS.md §4) before buffering,
so widths are exact physical diameters.

Segment model (see :func:`expand_cutting_polygons`):

- Consecutive segments whose endpoints touch (within tolerance) are joined
  into one polyline run; a gap starts a new run.
- :class:`~plt_optimizer.core.models.ArcSegment` is flattened to sampled
  points (same 32-segment approximation the plotter uses for rendering).
- Zero-length segments are dropped; a lone point engraves a round dot
  (the cutter footprint at the plunge point).
- ``width_inches <= 0`` yields no polygons (degenerate cutter).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Sequence, Tuple

from shapely.geometry import LineString, Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from plt_optimizer.core.models import ArcSegment, Coordinate, Segment
from plt_optimizer.core.units import units_to_inches

# Endpoint-matching tolerance in inches: parsed coordinates are rounded to
# 3 decimals in plotter units (<= 1/1016 in ~ 0.001 in), so touching
# endpoints agree to well under 1e-5 in.
_ENDPOINT_TOL_INCHES = 1e-6

# Arc flattening resolution (segments per full circle sweep of 360 deg),
# matching the plotter's rendering approximation.
_ARC_SAMPLES = 32


@dataclass(frozen=True)
class StrokePolygon:
    """A closed polygon in inch coordinates, ready for matplotlib paths.

    Attributes:
        exterior: Exterior ring vertices as ``(x_inches, y_inches)`` pairs.
        interiors: Hole rings (annular bands, self-joining ribbons) as
            ``(x_inches, y_inches)`` pairs per ring.
    """

    exterior: Tuple[Tuple[float, float], ...]
    interiors: Tuple[Tuple[Tuple[float, float], ...], ...] = ()


def _arc_to_points_in_inches(arc: ArcSegment, num_segments: int = _ARC_SAMPLES) -> List[Coordinate]:
    """Sample points along an arc, converted to inches.

    Mirrors ``plotter._arc_to_points`` (32-segment linear approximation)
    with the unit conversion folded in.

    Args:
        arc: The arc segment to sample.
        num_segments: Number of line segments for the approximation.

    Returns:
        Coordinates sampled along the arc from start to end, in inches.
    """
    theta_start = math.atan2(arc.start.y - arc.center.y, arc.start.x - arc.center.x)
    delta_theta = arc.sweep_angle * math.pi / 180 / num_segments
    radius = arc.radius

    points: List[Coordinate] = []
    for i in range(num_segments + 1):
        theta = theta_start + i * delta_theta
        x = arc.center.x + radius * math.cos(theta)
        y = arc.center.y + radius * math.sin(theta)
        points.append(Coordinate(x, y))

    return points


def _points_in_inches(seg: Segment) -> List[Coordinate]:
    """Return the sampled centerline points of a segment in inches.

    Straight segments yield their two endpoints; arcs yield the flattened
    polyline (start..end inclusive).

    Args:
        seg: The segment to sample.

    Returns:
        At least one Coordinate (in inches) tracing the segment.
    """
    if isinstance(seg, ArcSegment):
        raw = _arc_to_points_in_inches(seg)
    else:
        raw = [seg.start, seg.end]
    return [Coordinate(units_to_inches(p.x), units_to_inches(p.y)) for p in raw]


def _touches(a: Coordinate, b: Coordinate) -> bool:
    """Return whether two inch-space points coincide within tolerance.

    Uses ``math.isclose`` per root AGENTS.md §1 (never ``==`` on floats).
    """
    return math.isclose(a.x, b.x, rel_tol=0.0, abs_tol=_ENDPOINT_TOL_INCHES) and math.isclose(
        a.y, b.y, rel_tol=0.0, abs_tol=_ENDPOINT_TOL_INCHES
    )


def _runs_from_segments(segments: Sequence[Segment]) -> List[List[Tuple[float, float]]]:
    """Split segments into contiguous inch-space polyline runs.

    Only cutting segments participate. A segment joins the current run
    when its start touches the run's last point; otherwise a new run
    starts. Zero-length segments (and zero-sweep arcs) are dropped.

    Args:
        segments: Ordered segments (plotter units) of one stroke run/layer.

    Returns:
        One list of ``(x_inches, y_inches)`` vertices per contiguous run.
    """
    runs: List[List[Tuple[float, float]]] = []
    current: List[Tuple[float, float]] = []

    for seg in segments:
        if not seg.is_cutting:
            continue
        pts = _points_in_inches(seg)
        # Drop zero-length trailing point(s) of straight segments; arcs
        # with a zero sweep collapse to one point naturally.
        if len(pts) == 2 and _touches(pts[0], pts[1]):
            pts = pts[:1]
        if not pts:
            continue

        if current and _touches(Coordinate(*current[-1]), pts[0]):
            extended = pts[1:]
        else:
            if current:
                runs.append(current)
            current = []
            extended = pts
        current.extend((p.x, p.y) for p in extended)

    if current:
        runs.append(current)
    return [run for run in runs if run]


def _rings_of(geom: BaseGeometry) -> List[StrokePolygon]:
    """Convert a (possibly multi-part) shapely geometry into StrokePolygons.

    Geometry collections are flattened; non-polygon parts (points/lines
    from degenerate buffers) are ignored.

    Args:
        geom: Buffered geometry in inch coordinates.

    Returns:
        One StrokePolygon per polygon part.
    """
    polygons: List[StrokePolygon] = []
    parts = getattr(geom, "geoms", [geom])
    for part in parts:
        if part.is_empty or part.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        # MultiPolygon parts arrive here via the geoms walk above.
        if part.geom_type == "MultiPolygon":
            polygons.extend(_rings_of(part))
            continue
        exterior = tuple((float(x), float(y)) for x, y in part.exterior.coords)
        interiors = tuple(
            tuple((float(x), float(y)) for x, y in ring.coords) for ring in part.interiors
        )
        polygons.append(StrokePolygon(exterior=exterior, interiors=interiors))
    return polygons


def expand_cutting_polygons(
    segments: Sequence[Segment],
    width_inches: float,
) -> List[StrokePolygon]:
    """Expand cutting segments into true-width polygons (in inches).

    Each contiguous pen-down run becomes a constant-width corridor of
    ``width_inches`` centered on its centerline: round end caps, round
    joins, and a round dot for a lone point (plunge footprint). Rapid
    (pen-up) segments are ignored. Runs are unioned so overlapping strokes
    of the same cutter merge into clean silhouettes.

    Args:
        segments: Ordered segments in plotter units (any mix of lines and
            arcs, cutting and rapid).
        width_inches: Physical stroke (cutter) diameter in inches.

    Returns:
        Polygons in inch coordinates; empty when there is no cutting
        geometry or ``width_inches <= 0``.
    """
    if width_inches <= 0.0:
        return []

    half = width_inches / 2.0
    shapes: List[BaseGeometry] = []
    for run in _runs_from_segments(segments):
        if len(run) == 1:
            shapes.append(Point(run[0]).buffer(half))
        else:
            shapes.append(LineString(run).buffer(half))

    if not shapes:
        return []
    merged = unary_union(shapes)
    return _rings_of(merged)
