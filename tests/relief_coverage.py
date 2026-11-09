"""Shared coverage helpers for the relief engraving equivalence tests.

The relief feature is validated by MATERIAL REMOVED, not stroke-by-stroke
comparison (our raster row phase, region traversal order and optimizer
routing cannot match EngraveLab byte-for-byte). These helpers parse PLT
files (reference and generated), expand every cutting stroke (lines and
``AA`` arcs) into its swept envelope (the polyline dilated by the tool
radius), and measure symmetric differences / island intrusions in square
inches.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import List, Sequence, Tuple

from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from plt_optimizer.core.models import ArcSegment
from plt_optimizer.core.parser import PLTParser
from plt_optimizer.core.units import units_to_inches

Point = Tuple[float, float]


def strokes_to_geometry(
    path: Path, mirror_y: bool = False, arc_sample: float = 5e-4
) -> List[LineString]:
    """Parse a PLT file into per-path shapely LineStrings (inches).

    Args:
        path: The PLT file to parse.
        mirror_y: Flip y about 1.0 (the reference files are y-up plotter
            coordinates, our exports are y-down device coordinates in the
            1x1 plate frame).
        arc_sample: Maximum arc chord deviation for flattening.

    Returns:
        One LineString per cutting path (pen-up gaps split paths).
    """
    doc = PLTParser().parse_file(path)
    lines: List[LineString] = []
    for stroke in doc.stroke_paths:
        pts: List[Point] = []
        if stroke.pen_up_position is not None:
            pts.append(
                (
                    units_to_inches(stroke.pen_up_position.x),
                    units_to_inches(stroke.pen_up_position.y),
                )
            )
        for segment in stroke.segments:
            x0, y0 = units_to_inches(segment.start.x), units_to_inches(segment.start.y)
            x1, y1 = units_to_inches(segment.end.x), units_to_inches(segment.end.y)
            if not segment.is_cutting:
                pts = [(x1, y1)]
                continue
            if isinstance(segment, ArcSegment):
                cx, cy = units_to_inches(segment.center.x), units_to_inches(segment.center.y)
                radius = math.hypot(x0 - cx, y0 - cy)
                a0 = math.atan2(y0 - cy, x0 - cx)
                sweep = math.radians(segment.sweep_angle)
                count = max(2, int(abs(sweep) * radius / arc_sample) + 1)
                arc = [
                    (
                        cx + radius * math.cos(a0 + sweep * i / count),
                        cy + radius * math.sin(a0 + sweep * i / count),
                    )
                    for i in range(count + 1)
                ]
                pts.extend(arc)
                continue
            if (
                pts
                and math.isclose(pts[-1][0], x0, abs_tol=1e-9)
                and math.isclose(pts[-1][1], y0, abs_tol=1e-9)
            ):
                pts.append((x1, y1))
            else:
                if len(pts) >= 2:
                    lines.append(LineString(pts))
                pts = [(x0, y0), (x1, y1)]
        if len(pts) >= 2:
            lines.append(LineString(pts))
    if mirror_y:
        lines = [LineString([(x, 1.0 - y) for x, y in line.coords]) for line in lines]
    return lines


def coverage(geometries: Sequence[BaseGeometry], radius: float) -> BaseGeometry:
    """Union of the swept envelopes of cutting strokes.

    Args:
        geometries: Stroke centerline geometries.
        radius: Tool radius in inches.

    Returns:
        The removed-material polygon.
    """
    return unary_union([g.buffer(radius) for g in geometries])


def plate_polygon() -> Polygon:
    """The 1x1 plate rectangle in inches."""
    return Polygon([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])


__all__ = ["coverage", "plate_polygon", "strokes_to_geometry"]
