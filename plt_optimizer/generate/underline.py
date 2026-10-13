"""Pure geometry for the ``<u>`` underline markup.

This module hosts the framework-free math behind underline rendering (the
render-side integration lives in ``plt_optimizer.generate.label_renderer``).
An underline is a **centerline stroke cut with the line's own text cutter**:
its centerline sits ``cutter_diameter + underline_offset`` inches below the
baseline (so the engraved underline's top edge floats exactly
``underline_offset`` below the engraved baseline stroke -- the two strokes
touch at ``0.0``), and it spans each maximal underlined run's ink width
(the first glyph's bounding-box left edge to the last glyph's right edge).

The TODO contract is *minimal breaking*: a glyph whose engraved ink would
collide with the engraved underline cuts a gap into it. The collision test
is the true 2D ink distance (the same metric the descending-character
relief uses): the gap trigger radius is the engraved-stroke touching floor
plus the requested break clearance

    r = 0.5 * cutter(underline) + 0.5 * cutter(text) + underline_break_clearance
      = cutter_diameter + underline_break_clearance

(both strokes are cut with the line's own cutter, so the floor is one cutter
diameter; the clearance is the engraved air left at each cut edge, 0.0 = the
strokes touch). The **offset** is *air* (edge-to-edge) and lives in the
centerline **position** only -- it must stay out of the trigger. Were the
trigger ``cutter + offset`` too, the offset cancelled out of the collision
test (a glyph collides iff its ink dips below the baseline, at *any* offset),
so raising ``underline_offset`` could never prevent a break -- the reported
bug where caps punched gaps no matter how deep the underline was set.
A glyph's ink within ``r`` of the centerline removes the underline over the
exact X-interval where the ink's radius-``r`` capsule crosses it; the
underline is cut nowhere else.

Design guarantees (mirroring ``markup.py`` / ``descender_adjust.py``):

* **Pure Python** -- matplotlib-free, numpy-free, Python 3.8 compatible
  (Windows 7 import guarantee). No pipeline imports at all.
* **Closed-form intervals** -- a capsule (segment + radius disk) is convex,
  so its intersection with the horizontal centerline is a single interval,
  computed from the capsule rectangle's line crossings plus the endpoint
  disks. No sampling, no bisection.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from plt_optimizer.generate.geometry import Ink, Point

__all__ = [
    "capsule_line_interval",
    "underline_runs",
    "underline_segments",
]

# Ink intervals closer than this merge into one gap (a sliver narrower than
# half a plotter unit cannot exist in the emitted geometry).
_INTERVAL_MERGE_TOLERANCE: float = 0.5 / 1016.0


def underline_runs(flags: Sequence[bool]) -> Tuple[Tuple[int, int], ...]:
    """Return the maximal runs of ``True`` in per-character underline flags.

    Args:
        flags: One flag per plain-text character (aligned with
            ``ResolvedTextLine.plain_text``; spaces included).

    Returns:
        ``(first_char_index, last_char_index)`` inclusive pairs in text
        order, one per maximal underlined run. Empty when no flag is set.
    """
    runs: List[Tuple[int, int]] = []
    start: Optional[int] = None
    for index, flag in enumerate(flags):
        if flag:
            if start is None:
                start = index
        elif start is not None:
            runs.append((start, index - 1))
            start = None
    if start is not None:
        runs.append((start, len(flags) - 1))
    return tuple(runs)


def capsule_line_interval(
    start: Point,
    end: Point,
    radius: float,
    line_y: float,
) -> Optional[Tuple[float, float]]:
    """Intersect a capsule (segment + radius disk) with a horizontal line.

    The capsule ``segment (+) disk(radius)`` is convex, so its intersection
    with ``y = line_y`` is a single interval (or empty). The capsule is the
    swept area of a point kept within ``radius`` of the segment -- exactly
    the region where an engraved stroke centered on ``y = line_y`` would
    touch the segment's stroke.

    Args:
        start: Segment start ``(x, y)``.
        end: Segment end ``(x, y)``.
        radius: Capsule radius (must be > 0; ``<= 0`` yields ``None``).
        line_y: The horizontal line's Y coordinate.

    Returns:
        The ``(x_min, x_max)`` interval of the intersection, or ``None``
        when the capsule never reaches the line. Exact tangency (the capsule
        grazing the line) yields ``None``: touching strokes are safe (the
        repo-wide convention), so only a strict crossing cuts a gap.
    """
    if radius <= 0.0:
        return None

    x1, y1 = start
    x2, y2 = end
    lows: List[float] = []
    highs: List[float] = []

    dx = x2 - x1
    dy = y2 - y1
    length = math.hypot(dx, dy)

    if length == 0.0:
        # Degenerate segment: the capsule is a single disk.
        dy0 = abs(y1 - line_y)
        if dy0 >= radius:
            return None
        span = math.sqrt(max(0.0, radius * radius - dy0 * dy0))
        return (x1 - span, x1 + span)

    if dy == 0.0:
        # Horizontal segment: the capsule is a horizontal stadium; its
        # intersection with a parallel line is one interval (empty --
        # tangency included -- when the line does not cut into the band).
        dy0 = abs(y1 - line_y)
        if dy0 >= radius:
            return None
        span = math.sqrt(max(0.0, radius * radius - dy0 * dy0))
        return (min(x1, x2) - span, max(x1, x2) + span)

    # Rectangle part: the segment thickened by the radius along its
    # perpendicular. Crossing scan of the convex quad with y = line_y.
    nx = -dy / length * radius
    ny = dx / length * radius
    quad = (
        (x1 + nx, y1 + ny),
        (x2 + nx, y2 + ny),
        (x2 - nx, y2 - ny),
        (x1 - nx, y1 - ny),
    )
    for index in range(4):
        ax, ay = quad[index]
        bx, by = quad[(index + 1) % 4]
        da = ay - line_y
        db = by - line_y
        # A half-open crossing test: a vertex exactly on the line is counted
        # once per edge pair, and a segment tangent to the line (no strict
        # crossing) contributes nothing -- touching strokes are safe.
        if (da <= 0.0 < db) or (db <= 0.0 < da):
            t = (line_y - ay) / (by - ay)
            crossing = ax + t * (bx - ax)
            lows.append(crossing)
            highs.append(crossing)

    # Endpoint disks (a disk tangent to the line contributes a zero-width
    # interval, which the caller's strict inequality drops).
    for px, py in (start, end):
        dy0 = abs(py - line_y)
        if dy0 >= radius:
            continue
        span = math.sqrt(max(0.0, radius * radius - dy0 * dy0))
        lows.append(px - span)
        highs.append(px + span)

    if not lows:
        return None
    return (min(lows), max(highs))


def _ink_line_intervals(ink: Ink, radius: float, line_y: float) -> List[Tuple[float, float]]:
    """Collect the capsule-line intervals of every ink segment.

    Args:
        ink: A glyph's ink polylines (render frame, inches).
        radius: Gap trigger radius (see :func:`capsule_line_interval`).
        line_y: The underline centerline's Y.

    Returns:
        One interval per segment whose capsule reaches the line.
    """
    intervals: List[Tuple[float, float]] = []
    for polyline in ink:
        for index in range(len(polyline) - 1):
            interval = capsule_line_interval(polyline[index], polyline[index + 1], radius, line_y)
            if interval is not None:
                intervals.append(interval)
    return intervals


def underline_segments(
    ink: Ink,
    span: Tuple[float, float],
    line_y: float,
    radius: float,
    min_length: float = 0.0,
) -> Tuple[Tuple[float, float], ...]:
    """Cut the minimal descender gaps out of one underline span.

    Args:
        ink: The line's complete glyph ink (render frame, inches) -- every
            glyph is measured; the result is clipped to ``span``.
        span: ``(x_min, x_max)`` of the underline (the run's first glyph's
            bbox left to its last glyph's bbox right).
        line_y: The underline centerline's Y.
        radius: Gap trigger radius (the stroke-touching floor
            ``cutter_diameter``; see :func:`capsule_line_interval`).
        min_length: Kept segments shorter than this are dropped (a speck
            narrower than the emission floor cannot be engraved).

    Returns:
        The kept underline segments ``(x_min, x_max)`` in increasing X,
        covering exactly the span's collision-free parts. Empty when the
        gaps consume the whole span.
    """
    x_lo, x_hi = span
    if x_hi <= x_lo:
        return ()

    intervals = [
        (max(lo, x_lo), min(hi, x_hi))
        for lo, hi in _ink_line_intervals(ink, radius, line_y)
        if min(hi, x_hi) > max(lo, x_lo)
    ]
    if not intervals:
        return ((x_lo, x_hi),)

    intervals.sort()
    merged: List[Tuple[float, float]] = [intervals[0]]
    for lo, hi in intervals[1:]:
        prev_lo, prev_hi = merged[-1]
        if lo <= prev_hi + _INTERVAL_MERGE_TOLERANCE:
            merged[-1] = (prev_lo, max(prev_hi, hi))
        else:
            merged.append((lo, hi))

    segments: List[Tuple[float, float]] = []
    cursor = x_lo
    for lo, hi in merged:
        if lo > cursor:
            segments.append((cursor, lo))
        cursor = max(cursor, hi)
    if cursor < x_hi:
        segments.append((cursor, x_hi))
    return tuple(segment for segment in segments if segment[1] - segment[0] >= min_length)
