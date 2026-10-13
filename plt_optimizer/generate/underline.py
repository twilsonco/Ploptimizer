"""Pure geometry for the ``<u>`` underline markup.

This module hosts the framework-free math behind underline rendering (the
render-side integration lives in ``plt_optimizer.generate.label_renderer``).
An underline is a **centerline stroke cut with the line's own text cutter**:
its centerline sits ``cutter_diameter + underline_offset`` inches below the
baseline (so the engraved underline's top edge floats exactly
``underline_offset`` below the engraved baseline stroke -- the two strokes
touch at ``0.0``), and it spans each maximal underlined run's ink width
(the first glyph's bounding-box left edge to the last glyph's right edge).

The TODO contract is *minimal breaking*: a **descending** glyph cuts a gap
into the underline where it crosses *through* it. The test is **strictly
horizontal**: the underline is a stroke of width ``cutter`` centered on
``line_y``, so its top edge sits at ``line_y + cutter`` in ink-centerline
terms (the ink of a same-width glyph stroke touches it exactly there -- the
level ``-underline_offset`` below the baseline). Only ink that reaches down
into that band breaks the underline; ink beside it (a cap's stem, a glyph
to the left or right) never does. A 2D radial (capsule) test is wrong here:
its distance grows with the *vertical* separation too, so a cap sitting
happily ABOVE the underline collides through the vertical component and
shatters the underline into per-glyph segments.

The break interval is the X-extent of the ink inside the trigger band, padded
by ``0.5 * cutter + underline_break_clearance`` on each side: the half cutter
is the glyph stroke's own half-width (the ink is a CENTERLINE, so a vertical
descender engraves a stroke one cutter wide and the underline must break by
that much), and the clearance is the engraved air left at each cut edge. The
band reaches ``band_tolerance`` **above** the touching level -- a fraction of
the clearance (``BAND_TOLERANCE_FRACTION * clearance``) -- so a glyph whose ink
stops just short of the underline still cuts, while ink far above it never
does.

The resulting rule is intuitive: a glyph breaks the underline iff its ink
dips more than ``underline_offset`` below the baseline (the touching level is
``line_y + cutter == -underline_offset``), so the **offset** alone decides
*which* glyphs break, and the **clearance** alone decides the air at each
break. Neither knob can cause a break the other one prevents.

The **offset** is *air* (edge-to-edge) and lives in the centerline
**position** only -- it must stay out of the trigger. Were the trigger
``cutter + offset`` too, the offset cancelled out of the collision test (a
glyph collides iff its ink dips below the baseline, at *any* offset), so
raising ``underline_offset`` could never prevent a break -- the reported bug
where caps punched gaps no matter how deep the underline was set.

Design guarantees (mirroring ``markup.py`` / ``descender_adjust.py``):

* **Pure Python** -- matplotlib-free, numpy-free, Python 3.8 compatible
  (Windows 7 import guarantee). No pipeline imports at all.
* **Closed-form intervals** -- the part of a segment at or below a horizontal
  level is convex, so its X-extent is a single interval, read off the
  endpoints plus the level crossing. No sampling, no bisection.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from plt_optimizer.generate.geometry import Ink, Point

__all__ = [
    "segment_band_interval",
    "underline_runs",
    "underline_segments",
]

# Ink intervals closer than this merge into one gap (a sliver narrower than
# half a plotter unit cannot exist in the emitted geometry).
_INTERVAL_MERGE_TOLERANCE: float = 0.5 / 1016.0

# How far ABOVE the stroke-touching level the trigger band reaches, as a
# fraction of ``underline_break_clearance``. The breaks exist for descending
# glyphs, so the band is essentially the underline's own stroke; the small
# reach above lets a glyph whose ink stops just short of the underline cut
# too, while ink well above it (a cap, an ascender) never does.
BAND_TOLERANCE_FRACTION: float = 0.2


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


def segment_band_interval(
    start: Point,
    end: Point,
    level: float,
    pad: float,
) -> Optional[Tuple[float, float]]:
    """Intersect a segment with the half-plane at or below a horizontal level.

    The part of a segment strictly below ``level`` is convex, so its X-extent
    is a single interval (or empty). This is the strictly horizontal break
    test: a glyph cuts the underline only where its ink reaches down INTO the
    underline's stroke band, never merely for standing beside it.

    Args:
        start: Segment start ``(x, y)``.
        end: Segment end ``(x, y)``.
        level: The band's trigger level (ink strictly below it cuts).
        pad: Horizontal air added to each side of the interval (the engraved
            gap left at each cut edge).

    Returns:
        The padded ``(x_min, x_max)`` interval, or ``None`` when no part of
        the segment reaches below the level. Ink exactly ON the level touches
        the band and cuts nothing -- touching strokes are safe (the repo-wide
        convention).
    """
    x1, y1 = start
    x2, y2 = end
    first_in = y1 < level
    second_in = y2 < level

    if not first_in and not second_in:
        return None

    if first_in and second_in:
        low, high = (x1, x2) if x1 <= x2 else (x2, x1)
    else:
        # Exactly one endpoint is in the band: clip at the level crossing.
        # The crossing exists because the endpoints straddle the level, so
        # y2 != y1 here.
        ratio = (level - y1) / (y2 - y1)
        crossing = x1 + ratio * (x2 - x1)
        if first_in:
            low, high = (x1, crossing) if x1 <= crossing else (crossing, x1)
        else:
            low, high = (crossing, x2) if crossing <= x2 else (x2, crossing)

    return (low - pad, high + pad)


def _ink_band_intervals(
    ink: Ink,
    level: float,
    pad: float,
) -> List[Tuple[float, float]]:
    """Collect the band intervals of every ink segment.

    Args:
        ink: A glyph's ink polylines (render frame, inches).
        level: The band's trigger level (see :func:`segment_band_interval`).
        pad: Horizontal air added to each side of each interval.

    Returns:
        One interval per segment whose ink reaches the level.
    """
    intervals: List[Tuple[float, float]] = []
    for polyline in ink:
        for index in range(len(polyline) - 1):
            interval = segment_band_interval(polyline[index], polyline[index + 1], level, pad)
            if interval is not None:
                intervals.append(interval)
    return intervals


def underline_segments(
    ink: Ink,
    span: Tuple[float, float],
    line_y: float,
    cutter: float,
    clearance: float,
    min_length: float = 0.0,
) -> Tuple[Tuple[float, float], ...]:
    """Cut the minimal descender gaps out of one underline span.

    Args:
        ink: The line's complete glyph ink (render frame, inches) -- every
            glyph is measured; the result is clipped to ``span``.
        span: ``(x_min, x_max)`` of the underline (the run's first glyph's
            bbox left to its last glyph's bbox right).
        line_y: The underline centerline's Y.
        cutter: The cutter diameter cutting both the underline and the glyphs
            (so the two strokes touch at ``line_y + cutter``, the band's
            trigger level).
        clearance: Horizontal air left at each cut edge (and the source of the
            band's small reach above the touching level, see
            :data:`BAND_TOLERANCE_FRACTION`). The cut is padded by
            ``0.5 * cutter + clearance``: the half cutter is the glyph
            stroke's own half-width (the ink is a centerline).
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

    # The touching level: ink at `line_y + cutter` is exactly one full stroke
    # width from the centerline, i.e. the two same-width strokes touch. In
    # baseline terms that level is `-underline_offset`.
    level = line_y + cutter + BAND_TOLERANCE_FRACTION * clearance
    pad = 0.5 * cutter + clearance
    intervals = [
        (max(lo, x_lo), min(hi, x_hi))
        for lo, hi in _ink_band_intervals(ink, level, pad)
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
