"""Arc command generation for smooth curve rendering.

This module converts polyline segments to arc commands (AA) for improved
rendering quality of curved character strokes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np


@dataclass
class ArcCommand:
    """Represents an HPGL arc command (AA)."""

    center_x: float
    center_y: float
    sweep_angle: float

    def to_hpgl(self) -> str:
        """Convert to HPGL format: AA cx,cy,angle."""
        return f"AA{self.center_x:.3f},{self.center_y:.3f},{self.sweep_angle:.3f}"


def fit_circle_to_points(
    points: List[Tuple[float, float]],
) -> Tuple[Optional[Tuple[float, float]], Optional[float]]:
    """Fit a circle to a list of points using least-squares algebraic method.

    Uses the algebraic least-squares method by Taubin (1991) for robustness.

    Args:
        points: List of (x, y) coordinate tuples.

    Returns:
        Tuple of ((center_x, center_y), radius) or (None, None) if fitting fails.
    """
    if len(points) < 3:
        return None, None

    try:
        points_array = np.array(points, dtype=np.float64)
        x = points_array[:, 0]
        y = points_array[:, 1]

        # Center the points
        x_mean = np.mean(x)
        y_mean = np.mean(y)
        u = x - x_mean
        v = y - y_mean

        # Compute sums for least-squares fit
        n = len(points)
        Suu = np.sum(u * u) / n
        Suv = np.sum(u * v) / n
        Svv = np.sum(v * v) / n
        Suuu = np.sum(u * u * u) / n
        Svvv = np.sum(v * v * v) / n
        Suvv = np.sum(u * v * v) / n
        Svuu = np.sum(v * u * u) / n

        # Build system matrix
        A = np.array([[Suu, Suv], [Suv, Svv]])
        b = np.array([Suuu + Suvv, Svvv + Svuu]) / 2.0

        try:
            # Solve for center offset
            center_offset = np.linalg.solve(A, b)
            uc = float(center_offset[0])
            vc = float(center_offset[1])

            # Actual center
            cx = x_mean + uc
            cy = y_mean + vc

            # Compute radius
            alpha = uc * uc + vc * vc
            radius = math.sqrt(alpha + (Suu + Svv))

            return (float(cx), float(cy)), float(radius)

        except np.linalg.LinAlgError:
            # Matrix singular, fallback to centroid method
            cx = float(x_mean)
            cy = float(y_mean)
            radius = float(np.mean(np.sqrt((x - cx) ** 2 + (y - cy) ** 2)))
            return (cx, cy), radius

    except (ValueError, IndexError):
        return None, None


def polyline_to_arc(
    points: List[Tuple[float, float]],
    start_point: Tuple[float, float],
    max_error: float = 0.5,
) -> Optional[ArcCommand]:
    """Convert a polyline to an arc command if it forms a circular segment.

    Args:
        points: List of (x, y) points forming the polyline.
        start_point: The starting point (current pen position).
        max_error: Maximum allowed distance error for arc fit (plotter units).

    Returns:
        ArcCommand if polyline fits a circular arc, None otherwise.
    """
    if len(points) < 3:
        return None

    # Fit circle to all points
    center, radius = fit_circle_to_points(points)
    if center is None or radius is None:
        return None

    # Check if fit quality is acceptable
    cx, cy = center
    errors = []
    for x, y in points:
        dist_to_center = math.sqrt((x - cx) ** 2 + (y - cy) ** 2)
        error = abs(dist_to_center - radius)
        errors.append(error)

    max_error_actual = max(errors)
    if max_error_actual > max_error:
        # Poor fit, return None
        return None

    # Calculate sweep angle
    # From start_point to end point (last point in polyline)
    start_x, start_y = start_point
    end_x, end_y = points[-1]

    # Calculate angles from center
    theta_start = math.atan2(start_y - cy, start_x - cx)
    theta_end = math.atan2(end_y - cy, end_x - cx)

    # Sweep angle (in degrees, counterclockwise)
    sweep_angle_rad = theta_end - theta_start

    # Normalize to -180 to 180 degrees
    sweep_angle_deg = math.degrees(sweep_angle_rad)
    while sweep_angle_deg > 180:
        sweep_angle_deg -= 360
    while sweep_angle_deg < -180:
        sweep_angle_deg += 360

    # For now, prefer positive sweep angles
    if sweep_angle_deg < 0:
        sweep_angle_deg += 360

    return ArcCommand(
        center_x=cx,
        center_y=cy,
        sweep_angle=sweep_angle_deg,
    )


def should_convert_to_arc(
    points: List[Tuple[float, float]],
    start_point: Tuple[float, float],
    angle_threshold: float = 5.0,
) -> bool:
    """Determine if a polyline should be converted to an arc.

    Args:
        points: List of (x, y) points forming the polyline.
        start_point: The starting point.
        angle_threshold: Minimum sweep angle to consider as arc (degrees).

    Returns:
        True if polyline should be converted to arc.
    """
    if len(points) < 3:
        return False

    # Fit circle
    center, radius = fit_circle_to_points(points)
    if center is None or radius is None:
        return False

    # Check sweep angle
    cx, cy = center
    start_x, start_y = start_point
    end_x, end_y = points[-1]

    theta_start = math.atan2(start_y - cy, start_x - cx)
    theta_end = math.atan2(end_y - cy, end_x - cx)

    sweep_angle_rad = abs(theta_end - theta_start)
    sweep_angle_deg = math.degrees(sweep_angle_rad)

    # Check if sweep angle is significant enough
    return sweep_angle_deg >= angle_threshold
