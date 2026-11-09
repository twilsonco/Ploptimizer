"""Arc-native outline rings: fit polygonized circular runs back to arcs.

Relief outline rings ride shapely polygons, so every boundary -- including
the true circular arcs of the geometry (erosion round corners, capsule caps
over the raised text/markup islands, glyph arcs offset concentrically) --
reaches export as a dense chord chain. The EngraveLab references cut those
curves as single ``PD;AA{cx},{cy},{sweep}`` commands (the exact format
``text_geometry.Stroke.to_hpgl`` emits), and the user's stroke-level
comparison flags chord chains where the reference has arcs.

:func:`fit_circular_runs` walks a ring polyline, groups maximal runs of
same-sign turns, and replaces each run that is provably circular (Kasa
circle fit, every vertex within ``max_dev`` of the fitted circle, radius
bounded, sweep past a floor, and enough vertices that a single sharp
miter-corner vertex is never arcified) with one :class:`ArcSeg`. Straight
runs (and refused fits) stay :class:`LineSeg` chains. Arc starts are real
vertices; each arc's end is the DERIVED ``arc_end_point`` so the chain
closes tip-to-tail exactly, matching the core parser's AA semantics.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from plt_optimizer.generate.text_geometry import ArcSeg, LineSeg, TextSegment

Point = Tuple[float, float]

#: Max chord deviation (inches) from the fitted circle, per vertex. Half a
#: plotter unit: sub-plotter-unit flattening, far under the 2.5e-4
#: island-flatten tolerance that produced these chains.
DEFAULT_MAX_DEV = 5e-4

#: Refuse fits whose radius exceeds this (inches): straight edges with
#: near-collinear noise fit to absurd circles; real ring arcs are tool-
#: radius-scale (<= a cutter diameter, well under an inch on a label).
DEFAULT_MAX_RADIUS = 1.0

#: Minimum sweep (degrees) worth an AA command -- and the stability floor
#: for the fit: Kasa on a sub-15-degree spread is ill-conditioned (a
#: straight-looking chain fits to absurd radii), so shorter prefixes are
#: skipped, and the run keeps growing.
DEFAULT_MIN_SWEEP_DEG = 15.0

#: Minimum points in a curved run (3 chord segments). A single sharp
#: miter-corner vertex (2 points of turn) passes through any circle fit
#: exactly -- it must stay two straight segments, never an arc.
_MIN_RUN_POINTS = 4

#: Per-vertex turn below this (degrees) counts as straight.
_STRAIGHT_TURN_DEG = 0.05


def _turn_deg(a: Point, b: Point, c: Point) -> float:
    """Signed turn angle (degrees, CCW positive) at ``b`` along a-b-c."""
    v1 = complex(b[0] - a[0], b[1] - a[1])
    v2 = complex(c[0] - b[0], c[1] - b[1])
    if abs(v1) == 0.0 or abs(v2) == 0.0:
        return 0.0
    cross = v1.real * v2.imag - v1.imag * v2.real
    dot = v1.real * v2.real + v1.imag * v2.imag
    return math.degrees(math.atan2(cross, dot))


def _fit_circle(points: Sequence[Point]) -> Optional[Tuple[Point, float, float]]:
    """Kasa algebraic circle fit (centroid-centered, well-conditioned).

    Returns:
        ``(center, radius, max_deviation)`` or ``None`` when the points are
        (near-)collinear (singular normal system).
    """
    n = len(points)
    if n < 3:
        return None
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = syy = sxy = sxz = syz = 0.0
    for x, y in points:
        dx, dy = x - mx, y - my
        z = dx * dx + dy * dy
        sxx += dx * dx
        syy += dy * dy
        sxy += dx * dy
        sxz += dx * z
        syz += dy * z
    # [2*Sxx 2*Sxy; 2*Sxy 2*Syy] [a; b] = [Sxz; Syz]  (center offset by
    # the centroid; the 3x3 form of this system is singular for the fine
    # chord chains produced by dense ring flattening).
    det = 4.0 * (sxx * syy - sxy * sxy)
    scale = sxx * syy + sxy * sxy
    if abs(det) < 1e-9 * max(scale, 1e-300):
        return None
    a = (2.0 * sxz * syy - 2.0 * syz * sxy) / det
    b = (2.0 * sxx * syz - 2.0 * sxy * sxz) / det
    cx, cy = mx + a, my + b
    radii = [math.dist((x, y), (cx, cy)) for x, y in points]
    radius = sum(radii) / n
    if radius <= 0.0:
        return None
    deviation = max(abs(r - radius) for r in radii)
    return ((cx, cy), radius, deviation)


def _run_sweep_deg(points: Sequence[Point], center: Point) -> float:
    """Signed total sweep (degrees) of the polyline around ``center``."""
    total = 0.0
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        a0 = math.atan2(y0 - center[1], x0 - center[0])
        a1 = math.atan2(y1 - center[1], x1 - center[0])
        delta = math.degrees(a1 - a0)
        while delta > 180.0:
            delta -= 360.0
        while delta < -180.0:
            delta += 360.0
        total += delta
    return total


def fit_circular_runs(
    points: Sequence[Point],
    max_dev: float = DEFAULT_MAX_DEV,
    max_radius: float = DEFAULT_MAX_RADIUS,
    min_sweep_deg: float = DEFAULT_MIN_SWEEP_DEG,
) -> List[TextSegment]:
    """Convert a ring polyline into a LineSeg/ArcSeg chain.

    Args:
        points: Vertices in cutting order (open list; a repeated closing
            vertex is fine).
        max_dev: Max per-vertex deviation (inches) from the fitted circle.
        max_radius: Refuse arcs above this fitted radius (inches).
        min_sweep_deg: Refuse arcs sweeping less than this.

    Returns:
        Tip-to-tail segments covering the polyline (straight vertices kept
        exactly; each arc starts at a real vertex and ends at its derived
        end point).
    """
    pts = [(float(x), float(y)) for x, y in points]
    # Drop a duplicate closing vertex; the ring re-closes via the chain.
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3:
        return [LineSeg(complex(*pts[i]), complex(*pts[i + 1])) for i in range(len(pts) - 1)]
    # Ring closure: append the first two vertices so wrap-around runs are
    # detected, then emit segments over indices 0..len-1 (closing included).
    wrapped = pts + pts[:2]
    turns = [
        _turn_deg(wrapped[i - 1], wrapped[i], wrapped[i + 1]) for i in range(1, len(wrapped) - 1)
    ]  # turns[i-1] is the turn at wrapped[i].

    def _sign(value: float) -> int:
        if abs(value) < _STRAIGHT_TURN_DEG:
            return 0
        return 1 if value > 0.0 else -1

    signs = [_sign(t) for t in turns]
    segments: List[TextSegment] = []
    # The chain cursor: every emitted segment starts exactly where the
    # previous one ended (arc ends are the DERIVED points, so the ring
    # chains tip-to-tail and to_hpgl emits no phantom pen-up).
    cursor = complex(*wrapped[0])
    index = 0
    n = len(pts)  # Emit a segment ending at each wrapped index 1..n.
    while index < n:
        start_turn = index  # signs[i] is the turn at wrapped[i + 1].
        if start_turn >= len(signs) or signs[start_turn] == 0:
            nxt = complex(*wrapped[index + 1])
            segments.append(LineSeg(start=cursor, end=nxt))
            cursor = nxt
            index += 1
            continue
        sign = signs[start_turn]
        # Same-sign run bound: vertices index+1 .. run_end all turn `sign`.
        run_end = index + 1
        k = start_turn
        while k + 1 < len(signs) and signs[k + 1] == sign and k + 1 < n:
            k += 1
            run_end = k + 1
        arc = _longest_arc(wrapped, index, run_end, sign, max_dev, max_radius, min_sweep_deg)
        if arc is None:
            nxt = complex(*wrapped[index + 1])
            segments.append(LineSeg(start=cursor, end=nxt))
            cursor = nxt
            index += 1
            continue
        found, end_index = arc
        found = ArcSeg(start=cursor, center=found.center, sweep_deg=found.sweep_deg)
        segments.append(found)
        cursor = found.end
        index = end_index
    return segments


def _longest_arc(
    wrapped: Sequence[Point],
    start: int,
    run_end: int,
    sign: int,
    max_dev: float,
    max_radius: float,
    min_sweep_deg: float,
) -> Optional[Tuple[ArcSeg, int]]:
    """Grow the circular prefix of a same-sign run.

    Convex rings (capsules, rounded rectangles) have no straight-separator
    vertices, so a same-sign run spans cap arcs, straight edges and all:
    the ARC is the longest prefix whose vertices AND edge midpoints stay
    within ``max_dev`` of one fitted circle.

    Args:
        wrapped: The ring vertices with the 2-point closure wrap.
        start: Wrapped index the arc starts at (chain entry vertex).
        run_end: Last wrapped index of the same-sign run.
        sign: The run's turn sign.
        max_dev: Max per-sample deviation from the fitted circle (inches).
        max_radius: Refuse fits above this radius (inches).
        min_sweep_deg: Refuse fits below this sweep.

    Returns:
        ``(arc, end_index)`` for the longest valid prefix, or ``None``.
    """
    best: Optional[Tuple[ArcSeg, int]] = None
    for end in range(start + 3, run_end + 1):
        run = wrapped[start : end + 1]
        outcome = _fit_run(run, sign, max_dev, max_radius, min_sweep_deg)
        if outcome[0] is not None:
            best = (outcome[0], end)
        elif outcome[1] == "deviation" and best is not None:
            break  # the prefix left the circle -- the arc ended
    return best


def _fit_run(
    run: Sequence[Point],
    sign: int,
    max_dev: float,
    max_radius: float,
    min_sweep_deg: float,
) -> Tuple[Optional[ArcSeg], str]:
    """Try the circular fit of a run prefix.

    Returns:
        ``(arc, "ok")`` on success, else ``(None, reason)`` where reason
        is ``"singular"``, ``"undersweep"``, ``"radius"`` or
        ``"deviation"``.
    """
    if len(run) < _MIN_RUN_POINTS:
        return None, "singularity"
    sample: List[Point] = []
    for (x0, y0), (x1, y1) in zip(run, run[1:]):
        sample.append((x0, y0))
        sample.append(((x0 + x1) / 2.0, (y0 + y1) / 2.0))
    sample.append(run[-1])
    fit = _fit_circle(sample)
    if fit is None:
        return None, "singular"
    center, radius, deviation = fit
    sweep = _run_sweep_deg(run, center)
    if math.copysign(1.0, sweep) != float(sign):
        return None, "deviation"
    if abs(sweep) < min_sweep_deg:
        return None, "undersweep"
    if radius <= 0.0 or radius > max_radius:
        return None, "radius"
    if deviation > max_dev:
        return None, "deviation"
    arc = ArcSeg(start=complex(*run[0]), center=complex(*center), sweep_deg=sweep)
    if math.dist((arc.end.real, arc.end.imag), run[-1]) > max_dev:
        return None, "deviation"
    return arc, "ok"


__all__ = ["fit_circular_runs"]
