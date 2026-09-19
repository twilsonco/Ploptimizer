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


def check_text_hole_collision(
    text_bounds: TextBounds,
    hole_center: HoleCenter,
    hole_radius: float,
    min_clearance: float = 0.0,
) -> bool:
    """Return True when a text box is closer than ``min_clearance`` to a hole.

    Uses the standard circle-vs-axis-aligned-bounding-box test: the closest
    point on the box to the circle center is computed and the resulting gap
    compared against ``min_clearance``. With the default ``0.0`` this is
    the strict overlap predicate (exact tangency, gap == 0, is NOT a
    collision). A positive ``min_clearance`` additionally flags near misses
    whose engraved strokes would bleed together; a gap exactly equal to
    ``min_clearance`` is safe.

    Args:
        text_bounds: ``(x_min, y_min, x_max, y_max)`` box coordinates in
            label-local coordinates.
        hole_center: ``(cx, cy)`` hole center in the same coordinates.
        hole_radius: Hole radius in the same length units.
        min_clearance: Required minimum gap in the same length units
            (>= 0). The stroke-aware collision threshold in
            ``label_renderer`` combines the stroke floor
            ``0.5 * (hole_cutter + text_cutter)`` with the configured
            collision distance into this value.

    Returns:
        ``True`` if the gap is below ``min_clearance``, ``False`` otherwise.
    """
    return circle_aabb_gap(text_bounds, hole_center, hole_radius) < min_clearance
