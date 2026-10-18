"""Collision geometry predicates for the label generation pipeline.

This module hosts the pure geometric predicates behind the text-hole
collision avoidance system (the package contract is ``AGENTS.md`` beside
this file): the signed circle-vs-AABB gap that drives text-hole detection,
and the circle-vs-ellipse primitives behind ``type: round`` label work
(boundary-aware collision checks, the radial content inset). Everything
here is coordinate-frame agnostic: callers provide text bounding boxes and
hole circles in the *same* frame (the generate pipeline uses label-local,
y-up coordinates before plate placement).

Shared frame-agnostic primitives that are NOT collision-specific
(``TextBounds``, ink/gap helpers, arc bounds) live in
:mod:`plt_optimizer.generate.geometry`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Tuple

from plt_optimizer.generate.geometry import Point, TextBounds

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


# An axis-aligned ellipse as ``(center_x, center_y, semi_a, semi_b)``: the
# boundary of a ``type: round`` label (semi-axes = width/2, height/2).
EllipseSpec = Tuple[float, float, float, float]

# Bisection budget for the point-to-ellipse distance (see
# :func:`point_ellipse_signed_distance`). The bracket shrinks by half every
# iteration, so 100 iterations land far below a plotter unit.
_ELLIPSE_MAX_ITERATIONS: int = 100
_ELLIPSE_TOLERANCE: float = 1e-12


def point_ellipse_signed_distance(
    point: Point,
    ellipse_center: Point,
    semi_a: float,
    semi_b: float,
) -> float:
    """Signed distance from a point to an axis-aligned ellipse's boundary.

    The distance is measured to the ellipse *curve* (not to its centre),
    solved exactly through the Lagrange-multiplier formulation: the closest
    point ``(x, y)`` on the ellipse to ``p`` satisfies

    ``x = a^2 px / (a^2 + L)``, ``y = b^2 py / (b^2 + L)``

    for the unique ``L > -b^2`` making ``(x/a)^2 + (y/b)^2 == 1``. The left
    side is strictly decreasing in ``L`` on that interval, so bisection
    converges monotonically to the single root.

    Sign convention (matching :func:`circle_aabb_gap`):

    - ``> 0``: the point is outside the ellipse by that distance.
    - ``== 0``: the point sits exactly on the boundary (tangency).
    - ``< 0``: the point is inside; ``abs(value)`` is the distance to the
      boundary.

    Args:
        point: ``(x, y)`` query point.
        ellipse_center: ``(cx, cy)`` ellipse centre.
        semi_a: Semi-axis along X (> 0).
        semi_b: Semi-axis along Y (> 0).

    Returns:
        The signed distance in the caller's length units.

    Raises:
        ValueError: If either semi-axis is not positive.
    """
    if semi_a <= 0.0 or semi_b <= 0.0:
        raise ValueError("ellipse semi-axes must be positive")

    cx, cy = ellipse_center
    px = point[0] - cx
    py = point[1] - cy

    a2 = semi_a * semi_a
    b2 = semi_b * semi_b
    # Work with the larger semi-axis as ``a`` so the one degenerate axis case
    # (a point ON the major axis) has a single shape.
    swapped = b2 > a2
    if swapped:
        px, py = py, px
        a2, b2 = b2, a2
    minor = b2

    # A point on the MAJOR axis (py == 0) is the one case where the
    # Lagrange root can leave the bracket: inside the ellipse's evolute
    # intercept ``(a^2 - b^2) / a`` the closest point is a MINOR-axis vertex,
    # not a point solving the stationarity equation. The ellipse centre is
    # exactly this case (its distance to the boundary is the semi-minor).
    if py == 0.0:
        semi_major = math.sqrt(a2)
        evolute = (a2 - b2) / semi_major
        if abs(px) < evolute:
            distance = math.hypot(px, math.sqrt(b2))
            return -distance  # inside the evolute intercept: always interior

    def constraint(lam: float) -> float:
        """``(x/a)^2 + (y/b)^2 - 1`` at a candidate multiplier."""
        xa = a2 * px / (a2 + lam)
        yb = b2 * py / (b2 + lam)
        return (xa * xa) / a2 + (yb * yb) / b2 - 1.0

    # The constraint is strictly decreasing on (-minor, inf): +inf at the
    # left end, -1 at the right. Exterior points root at L > 0, interior
    # points at -minor < L < 0.
    low = -minor * (1.0 - 1e-12)
    high = max(a2, b2)
    while constraint(high) > 0.0:
        high *= 2.0

    for _ in range(_ELLIPSE_MAX_ITERATIONS):
        if high - low <= _ELLIPSE_TOLERANCE * max(1.0, high):
            break
        middle = (low + high) / 2.0
        if constraint(middle) > 0.0:
            low = middle
        else:
            high = middle

    lam = (low + high) / 2.0
    closest_x = a2 * px / (a2 + lam)
    closest_y = b2 * py / (b2 + lam)
    if swapped:
        closest_x, closest_y = closest_y, closest_x
    distance = math.hypot(closest_x - px, closest_y - py)

    # The implicit function's sign classifies the point (inside vs outside).
    inside = (px * px) / a2 + (py * py) / b2 < 1.0
    return -distance if inside else distance


def circle_ellipse_gap(
    circle_center: HoleCenter,
    circle_radius: float,
    ellipse: EllipseSpec,
) -> float:
    """Signed clearance between a circle and an axis-aligned ellipse boundary.

    The gap is measured from the circle's perimeter to the ellipse curve
    along the line joining the circle's centre to the ellipse's centre --
    the same construction :func:`circle_aabb_gap` uses for a box (closest
    point of the box to the circle's centre, minus the radius), with the
    point-to-ellipse distance solved exactly.

    - ``gap > 0``: the circle clears the ellipse boundary by ``gap`` inches.
    - ``gap == 0``: the two strokes are tangent.
    - ``gap < 0``: the circle crosses the boundary; ``abs(gap)`` is the
      penetration depth.

    Args:
        circle_center: ``(cx, cy)`` circle centre.
        circle_radius: Circle radius (>= 0).
        ellipse: ``(center_x, center_y, semi_a, semi_b)`` ellipse.

    Returns:
        The signed clearance in the caller's length units.
    """
    ellipse_center = (ellipse[0], ellipse[1])
    centre_distance = math.hypot(
        circle_center[0] - ellipse_center[0], circle_center[1] - ellipse_center[1]
    )
    if centre_distance <= _ELLIPSE_TOLERANCE:
        # Concentric: the boundary is the semi-minor axis away in every
        # direction, and the centre is inside.
        return -min(ellipse[2], ellipse[3]) - circle_radius
    signed = point_ellipse_signed_distance(circle_center, ellipse_center, ellipse[2], ellipse[3])
    return signed - circle_radius


def circle_ellipse_aabb_gap(
    text_bounds: TextBounds,
    ellipse: EllipseSpec,
    hole_radius: float,
) -> float:
    """Signed clearance between a drill hole and a ``round`` label boundary.

    The mirror of :func:`circle_aabb_gap` for an elliptical label: the
    label's boundary is the ellipse with semi-axes ``semi_a``/``semi_b``, and
    the measured point is the point of the text bounding box **nearest the
    ellipse centre** -- the exact analogue of the box construction (the box
    point closest to the hole centre), with the point-to-ellipse distance
    solved exactly. Along the ellipse's axes (where drill holes and the
    ``corners``/``sides`` locations sit) the measurement is the true minimum
    gap.

    Sign convention matches :func:`circle_aabb_gap`: positive = separated,
    zero = tangent, negative = the hole's circle crosses the engraved
    boundary stroke.

    Args:
        text_bounds: ``(x_min, y_min, x_max, y_max)`` text box coordinates.
        ellipse: ``(center_x, center_y, semi_a, semi_b)`` label boundary.
        hole_radius: Drill-hole circle radius (>= 0).

    Returns:
        The signed clearance in the caller's length units.
    """
    x_min, y_min, x_max, y_max = text_bounds
    cx, cy = ellipse[0], ellipse[1]
    # The box point nearest the ellipse centre.
    nearest_x = min(max(cx, x_min), x_max)
    nearest_y = min(max(cy, y_min), y_max)
    signed = point_ellipse_signed_distance((nearest_x, nearest_y), (cx, cy), ellipse[2], ellipse[3])
    return signed - hole_radius
