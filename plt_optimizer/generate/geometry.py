"""Geometric primitives for the label generation pipeline.

This module hosts small, pure geometric predicates shared by the rendering
and collision-avoidance layers of the generate pipeline. Everything here is
coordinate-frame agnostic: callers are responsible for providing text
bounding boxes and hole circles in the *same* frame (the generate pipeline
uses label-local, y-up coordinates before plate placement).

The primary consumer is the text-hole collision avoidance system in
``plt_optimizer.generate.label_renderer``, which detects rendered text lines
whose axis-aligned bounding boxes intersect drill-hole circles.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Tuple

# An axis-aligned bounding box as ``(x_min, y_min, x_max, y_max)``.
TextBounds = Tuple[float, float, float, float]

# A circle as ``(center_x, center_y)`` plus a separate radius.
HoleCenter = Tuple[float, float]


@dataclass(frozen=True)
class CollisionResult:
    """A single detected collision between a rendered text line and a hole.

    Attributes:
        line_index: Index of the colliding text line within the label's
            ``content`` list.
        line_text: The rendered text of the colliding line (diagnostics).
        hole_index: Index of the colliding hole within the label's
            ``holes`` list.
        hole_location: The hole's location enum value (e.g. ``"top-left"``).
        gap: Signed clearance between the text bounding box and the hole
            circle, in inches. Negative values indicate penetration depth
            (the box overlaps the circle by ``abs(gap)`` inches); zero is
            exact tangency. Note that collision detection may use a
            stroke-aware threshold, so a reported collision can carry a
            *positive* gap (a near miss below the required clearance).
    """

    line_index: int
    line_text: str
    hole_index: int
    hole_location: str
    gap: float


def circle_aabb_gap(
    text_bounds: TextBounds,
    hole_center: HoleCenter,
    hole_radius: float,
) -> float:
    """Compute the signed clearance between a circle and an axis-aligned box.

    The gap is measured from the circle's perimeter to the box:

    - ``gap > 0``: the circle and box are separated by ``gap`` inches.
    - ``gap == 0``: they touch tangentially (no overlap).
    - ``gap < 0``: they overlap; ``abs(gap)`` is a lower bound on the
      penetration depth (the exact distance the circle's center would need
      to move along the separating axis is not computed here).

    When the circle's center lies inside the box the distance component is
    zero and the gap equals ``-hole_radius``.

    Args:
        text_bounds: ``(x_min, y_min, x_max, y_max)`` box coordinates.
        hole_center: ``(cx, cy)`` circle center coordinates.
        hole_radius: Circle radius (must be >= 0).

    Returns:
        The signed clearance in the caller's length units.
    """
    x_min, y_min, x_max, y_max = text_bounds
    cx, cy = hole_center

    # Distance from the circle center to the closest point of the box
    # (zero when the center is inside the box).
    dx = max(x_min - cx, 0.0, cx - x_max)
    dy = max(y_min - cy, 0.0, cy - y_max)

    return math.hypot(dx, dy) - hole_radius


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
