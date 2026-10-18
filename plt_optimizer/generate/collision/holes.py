"""Drill-hole geometry: placement in the label frame + HPGL emission.

Computes the drill-hole circles every consumer shares - the emitted ``AA``
arcs and the text-hole collision checks measure the SAME circles - and
emits the native four-arc ``AA`` drill pattern. Corner holes on round and
rounded-rectangle labels ride the engraved curve (the rectangular corner
inset would land them in cut-away material).

Moved verbatim from ``label_renderer.py`` when the collision package was
created; the contract is ``AGENTS.md`` beside this file.
"""

from __future__ import annotations

import logging
import math
from typing import List, Tuple

from plt_optimizer.core.units import inches_to_units
from plt_optimizer.generate.collision.shapes import (
    label_ellipse,
    rounded_rect_corner,
    uses_ellipse_boundary,
)
from plt_optimizer.generate.resolution import (
    MIN_HOLE_EMITTED_RADIUS_INCHES,
    ResolvedLabel,
    RoundedRectCorner,
    hole_engraved_radius,
)

logger = logging.getLogger(__name__)


def hole_circles_local(label: ResolvedLabel) -> List[Tuple[float, float, float]]:
    """Compute drill-hole circles in the label's local coordinate space.

    Each circle center is inset from the relevant edge(s) by ``hole_margin +
    half the hole cutter diameter + radius``, so the closest point of the
    circle's stroke sits exactly ``label.hole_margin`` inches from the label
    boundary. This ensures consistent spacing regardless of hole cutter size.
    The centre is always placed from the **specified** (nominal) hole
    diameter, so hole positions are independent of
    ``label.hole_adjust_for_cutter``.

    The returned **radius** is the emitted (engraved) one: with
    ``label.hole_adjust_for_cutter`` enabled it shrinks by half the hole
    cutter diameter, so the cutter's own stroke -- half a cutter width on
    each side of the path -- brings the finished hole up to the specified
    diameter (see :func:`hole_engraved_radius`). Both the emitted ``AA``
    arcs and the text-hole collision checks consume these circles, so a
    collision check always measures the circle that is actually engraved.

    Args:
        label: The resolved label whose ``holes`` should be measured.

    Returns:
        One ``(center_x, center_y, radius)`` tuple per hole, in inches.
    """
    circles: List[Tuple[float, float, float]] = []
    is_round = uses_ellipse_boundary(label)
    rounded_corner = rounded_rect_corner(label)
    corner_locations = ("top-left", "top-right", "bottom-left", "bottom-right")

    for hole in label.holes:
        # The centre rides the SPECIFIED diameter (positions never move);
        # the emitted radius is the cutter-compensated one.
        center_radius = hole.diameter / 2.0
        radius = hole_engraved_radius(
            hole.diameter, label.hole_cutter_diameter, label.hole_adjust_for_cutter
        )
        if (
            label.hole_adjust_for_cutter
            and hole.diameter - label.hole_cutter_diameter < 2.0 * MIN_HOLE_EMITTED_RADIUS_INCHES
        ):
            logger.warning(
                "Label %s: %s drill hole of %.4fin cannot be cutter-compensated "
                "with a %.4fin cutter (the tool alone is at least the target "
                "hole); emitting the smallest possible circle (%.4fin diameter) "
                "at the specified centre. Increase the hole diameter above the "
                "cutter size, or set hole_adjust_for_cutter: false.",
                label.id,
                hole.location,
                hole.diameter,
                label.hole_cutter_diameter,
                2.0 * MIN_HOLE_EMITTED_RADIUS_INCHES,
            )
        # Distance from the edge to the hole center: the requested hole
        # margin plus half the hole cutter diameter (for visual consistency)
        # plus the specified radius, so the circle's stroke edge sits exactly
        # hole_margin inches away from the edge.
        offset = label.hole_margin + (label.hole_cutter_diameter / 2.0) + center_radius
        location = str(hole.location)

        if is_round and location in corner_locations:
            # A round label's corner sits in the material the ellipse cuts
            # away, so a rectangular corner inset would drill outside the
            # boundary. Place the hole on the ellipse's diagonal instead:
            # at the 45-degree parametric point, inset inward along the
            # outward normal by ``offset`` so the stroke keeps hole_margin
            # from the engraved curve.
            hole_x, hole_y = round_corner_hole(label, location, offset)
            circles.append((hole_x, hole_y, radius))
            continue

        if rounded_corner is not None and location in corner_locations:
            # Same rule on a rounded rectangle: the sharp corner is cut
            # away, so the hole rides the corner patch at its 45-degree
            # parametric point, inset along the patch's outward normal.
            hole_x, hole_y = rounded_rect_corner_hole(
                label.width, label.height, rounded_corner, location, offset
            )
            circles.append((hole_x, hole_y, radius))
            continue

        if location == "top-left":
            hole_x = offset
            hole_y = label.height - offset
        elif location == "top-right":
            hole_x = label.width - offset
            hole_y = label.height - offset
        elif location == "bottom-left":
            hole_x = offset
            hole_y = offset
        elif location == "bottom-right":
            hole_x = label.width - offset
            hole_y = offset
        elif location == "left":
            hole_x = offset
            hole_y = label.height / 2.0
        elif location == "right":
            hole_x = label.width - offset
            hole_y = label.height / 2.0
        elif location == "top":
            hole_x = label.width / 2.0
            hole_y = label.height - offset
        elif location == "bottom":
            hole_x = label.width / 2.0
            hole_y = offset
        else:  # pragma: no cover - schema validates the location enum
            continue

        circles.append((hole_x, hole_y, radius))

    return circles


def round_corner_hole(
    label: ResolvedLabel,
    location: str,
    offset: float,
) -> Tuple[float, float]:
    """Place a corner drill hole on a round label's elliptical boundary.

    The corner is measured along the 45-degree parametric direction (the
    diagonal toward the named corner): the boundary point
    ``(cx + a cos45, cy + b sin45)`` inset inward along the outward normal
    ``(cos45 / a, sin45 / b)`` (normalized) by ``offset``. The result keeps
    the hole's stroke ``offset`` from the engraved curve near the diagonal,
    where the rectangular corner inset would land the hole in cut-away
    material.

    Args:
        label: The resolved round label.
        location: One of the four corner location strings.
        offset: Inset distance from the boundary, inches.

    Returns:
        ``(center_x, center_y)`` in label-local y-up inches.
    """
    cx, cy, semi_a, semi_b = label_ellipse(label)
    sign_x = -1.0 if "left" in location else 1.0
    sign_y = 1.0 if "top" in location else -1.0
    cos45 = math.sqrt(0.5)
    sin45 = math.sqrt(0.5)
    # Boundary point toward the corner.
    bx = cx + sign_x * semi_a * cos45
    by = cy + sign_y * semi_b * sin45
    # Unit outward normal at that parametric angle.
    nx = (sign_x * cos45) / semi_a
    ny = (sign_y * sin45) / semi_b
    norm = math.hypot(nx, ny)
    nx, ny = nx / norm, ny / norm
    return (bx - nx * offset, by - ny * offset)


def rounded_rect_corner_hole(
    width: float,
    height: float,
    corner: RoundedRectCorner,
    location: str,
    offset: float,
) -> Tuple[float, float]:
    """Place a corner drill hole on a rounded rectangle's corner patch.

    The generalization of :func:`round_corner_hole` to a superellipse
    quadrant: the boundary point toward the named corner is the patch's
    45-degree parametric point ``C + (sign_x rx cos^p 45, sign_y ry sin^p 45)``
    (``p = 2 / n``), and the outward normal there is the gradient of
    ``|X / rx| ^ n + |Y / ry| ^ n``, i.e. ``(sign_x cos^2 / rx, sign_y sin^2 /
    ry)`` scaled by the common factor ``cos^{n-2} sin^{n-2}`` -- which cancels
    in the normalized direction. At ``n == 2`` this is exactly the ellipse
    rule. Insetting by ``offset`` along it keeps the hole's stroke
    ``offset`` from the engraved curve near the diagonal, where the
    rectangular corner inset would land the hole in cut-away material.

    Args:
        width: Label width in inches (> 0).
        height: Label height in inches (> 0).
        corner: The corner parameters.
        location: One of the four corner location strings.
        offset: Inset distance from the boundary, inches.

    Returns:
        ``(center_x, center_y)`` in label-local y-up inches.
    """
    rx, ry, exponent = corner.rx, corner.ry, corner.exponent
    power = 2.0 / exponent
    sign_x = -1.0 if "left" in location else 1.0
    sign_y = 1.0 if "top" in location else -1.0
    cos45 = math.sqrt(0.5)
    sin45 = math.sqrt(0.5)
    # Patch centre: inset from the label's corner by (rx, ry).
    cx = rx if sign_x < 0.0 else width - rx
    cy = ry if sign_y < 0.0 else height - ry
    # Boundary point toward the corner (the patch's 45-degree parametric point).
    bx = cx + sign_x * rx * cos45**power
    by = cy + sign_y * ry * sin45**power
    # Unit outward normal (the gradient's direction; the common parametric
    # factor cancels under normalization).
    nx = (sign_x * cos45 * cos45) / rx
    ny = (sign_y * sin45 * sin45) / ry
    norm = math.hypot(nx, ny)
    if norm == 0.0:  # pragma: no cover - rx, ry > 0 by construction
        return (bx, by)
    return (bx - nx / norm * offset, by - ny / norm * offset)


def render_holes_hpgl(label: ResolvedLabel) -> str:
    """Render drill holes as native HPGL arc (``AA``) commands.

    Each hole becomes one closed circle drawn as four 90-degree absolute
    arcs, matching the EngraveLab drill-hole convention already understood
    by the parser, writer, profiler and plotter::

        PU{sx},{sy};PD{sx},{sy};AA{cx},{cy},90;AA{cx},{cy},90;AA{cx},{cy},90;AA{cx},{cy},90

    The zero-length ``PD`` plunge is deliberate: the parser only opens a new
    stroke path when a pen-down segment is emitted, so without it every hole
    on the layer would merge into one path and the profiler's "3+ arcs
    totaling ~360 degrees plus an optional plunge" drill-hole rule would not
    fire.

    Emitting arcs instead of a sampled polygon keeps the drill circles
    perfectly smooth on the plotter regardless of hole diameter, and lets
    the profiler recognise them as structural features.

    Args:
        label: The resolved label whose ``holes`` should be emitted.

    Returns:
        HPGL command string (without the ``SP`` wrapper), or an empty string
        when the label has no holes. Coordinates are in plotter units
        (``HPGL_UNITS_PER_INCH`` units = 1 inch).
    """
    commands: List[str] = []

    for center_x, center_y, radius in hole_circles_local(label):
        # Convert to plotter units so the emitted PU start and AA center are
        # mutually consistent integers (the parser derives the radius from
        # their distance).
        cx = inches_to_units(center_x)
        cy = inches_to_units(center_y)
        r = inches_to_units(radius)
        if r <= 0:  # pragma: no cover - degenerate hole
            continue

        # Begin at the rightmost point of the circle, then sweep four
        # quarter-arcs back to it.
        start = f"{cx + r},{cy}"
        commands.append(f"PU{start}")
        commands.append(f"PD{start}")  # zero-length plunge, opens a new path
        commands.extend(f"AA{cx},{cy},90" for _ in range(4))

    return ";".join(commands)
