"""Pure geometry for the ``<u>`` underline markup.

This module hosts the framework-free math behind underline rendering (the
render-side integration lives in ``plt_optimizer.generate.label_renderer``).
It also hosts :func:`runs_from_flags`, the per-character run scan shared by
every line decoration -- the underline and the ``<s>`` strikethrough (which
needs the runs alone: it engraves them unbroken, so none of the gap-cutting
geometry below applies to it).
An underline is a **centerline stroke cut with the line's own text cutter**:
its centerline sits ``cutter_diameter + underline_offset_inches`` below the
baseline (``underline_offset_inches`` is the cascaded
``underline_offset_fraction`` times the line's cap height, resolved by the
caller; so the engraved underline's top edge floats exactly
``underline_offset_inches`` below the engraved baseline stroke -- the two
strokes
touch at ``0.0``), and it spans each maximal underlined run's ink width
(the first glyph's bounding-box left edge to the last glyph's right edge).

The TODO contract is *minimal breaking*: a **descending** glyph cuts a gap
into the underline where it crosses *through* it. The test is **strictly
horizontal** and keyed to the underline's **centerline**: a glyph breaks the
underline only where its ink crosses the centerline ``line_y``, inside a thin
band of ``+/- BAND_TOLERANCE_FRACTION * underline_break_clearance_inches``
around it
(so a glyph whose ink stops a hair short of the centerline still cuts, while
ink above it never does). Ink beside the underline (a cap's stem, a neighbour)
and ink that dips *below* the underline without crossing its centerline (a
shallow graze of the lower half) never break it. A 2D radial (capsule) test is
wrong here: its distance grows with the *vertical* separation too, so a cap
sitting happily ABOVE the underline collides through the vertical component and
shatters the underline into per-glyph segments. A half-plane test (everything
below the underline's top edge) is also wrong: a ``g``'s open J-hook dips its
whole bowl below the underline, so the break swallows the glyph's full width
instead of just the stem that crosses through.

The break interval is the X-extent of the ink inside the centerline band,
padded by ``0.5 * cutter + underline_break_clearance_inches`` on each side:
the half
cutter is the glyph stroke's own half-width (the ink is a CENTERLINE, so a
vertical descender engraves a stroke one cutter wide and the underline must
break by that much), and the clearance is the engraved air left at each cut
edge. A hook that crosses the centerline twice (a ``g``) cuts one gap per
crossing, so the underline shows the stem, not the whole glyph.

The resulting rule is intuitive: a glyph breaks the underline where its ink
crosses the centerline (``line_y = -(cutter + underline_offset_inches)``),
so the
**offset** alone decides *which* glyphs break (the deeper the underline, the
fewer), and the **clearance** alone decides the air at each break. Neither knob
can cause a break the other one prevents.

The **offset** is *air* (edge-to-edge) and lives in the centerline
**position** only -- it must stay out of the trigger. Were the trigger
``cutter + offset`` too, the offset cancelled out of the collision test (a
glyph collides iff its ink dips below the baseline, at *any* offset), so
raising ``underline_offset_fraction`` could never prevent a break -- the
reported bug
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
    "runs_from_flags",
    "segment_band_interval",
    "underline_runs",
    "underline_segments",
]

# Ink intervals closer than this merge into one gap (a sliver narrower than
# half a plotter unit cannot exist in the emitted geometry).
_INTERVAL_MERGE_TOLERANCE: float = 0.5 / 1016.0

# How far ABOVE and BELOW the underline's centerline the trigger band reaches,
# as a fraction of ``underline_break_clearance_inches``. The breaks exist
# for glyphs
# that cross the underline, so the band is a thin strip on the centerline; the
# small reach lets a glyph whose ink stops a hair short of the centerline cut
# too, while a shallow graze of the underline's lower half (a ``g`` bowl
# bottom) and ink above it (a cap, an ascender) never do.
BAND_TOLERANCE_FRACTION: float = 0.2


def runs_from_flags(flags: Sequence[bool]) -> Tuple[Tuple[int, int], ...]:
    """Return the maximal runs of ``True`` in a per-character flag sequence.

    The shared run scan behind every line decoration (the ``<u>`` underline
    and the ``<s>`` strikethrough): both engrave one stroke per maximal run of
    marked characters, so both need the same run decomposition.

    Args:
        flags: One flag per plain-text character (aligned with
            ``ResolvedTextLine.plain_text``; spaces included).

    Returns:
        ``(first_char_index, last_char_index)`` inclusive pairs in text
        order, one per maximal marked run. Empty when no flag is set.
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


#: Back-compat alias -- the run scan is shared with the ``<s>`` strikethrough.
underline_runs = runs_from_flags


def segment_band_interval(
    start: Point,
    end: Point,
    band_lo: float,
    band_hi: float,
    pad: float,
) -> Optional[Tuple[float, float]]:
    """Intersect a segment with a horizontal strip (the centerline band).

    The part of a segment inside the strip ``[band_lo, band_hi]`` is convex,
    so its X-extent is a single interval (or empty). This is the strictly
    horizontal crossing test: a glyph cuts the underline only where its ink
    crosses the underline's centerline strip -- never for dipping *below* it
    (a ``g`` bowl) and never for standing beside it.

    Args:
        start: Segment start ``(x, y)``.
        end: Segment end ``(x, y)``.
        band_lo: The strip's lower Y edge (``band_lo == band_hi`` is the
            degenerate band: the centerline itself, at zero clearance).
        band_hi: The strip's upper Y edge.
        pad: Horizontal air added to each side of the interval (the engraved
            gap left at each cut edge).

    Returns:
        The padded ``(x_min, x_max)`` interval, or ``None`` when the segment
        never crosses the strip. A segment merely touching a band edge (a
        graze) cuts nothing -- touching strokes are safe (the repo-wide
        convention).
    """
    x1, y1 = start
    x2, y2 = end

    if y1 == y2:
        # Horizontal segment: cuts only when it lies strictly inside the strip
        # (or runs exactly along a degenerate centerline band).
        inside = band_lo < y1 < band_hi or (band_lo == band_hi == y1)
        if not inside:
            return None
        low, high = (x1, x2) if x1 <= x2 else (x2, x1)
        return (low - pad, high + pad)

    # Clip the segment's parameter range to the strip, then read off the
    # X-extent of the kept part (convex, so one interval).
    ta = (band_lo - y1) / (y2 - y1)
    tb = (band_hi - y1) / (y2 - y1)
    t_in = min(ta, tb)
    t_out = max(ta, tb)
    t0 = max(0.0, t_in)
    t1 = min(1.0, t_out)

    if t1 > t0:
        xa = x1 + t0 * (x2 - x1)
        xb = x1 + t1 * (x2 - x1)
    elif t1 == t0 and band_lo == band_hi and 0.0 < t0 < 1.0:
        # Degenerate band (zero clearance): the segment crosses the centerline
        # exactly at t0, strictly inside the segment (an endpoint graze is
        # safe).
        xa = xb = x1 + t0 * (x2 - x1)
    else:
        return None

    low, high = (xa, xb) if xa <= xb else (xb, xa)
    return (low - pad, high + pad)


def _ink_band_intervals(
    ink: Ink,
    band_lo: float,
    band_hi: float,
    pad: float,
) -> List[Tuple[float, float]]:
    """Collect the centerline-band intervals of every ink segment.

    Args:
        ink: A glyph's ink polylines (render frame, inches).
        band_lo: The strip's lower Y edge (see :func:`segment_band_interval`).
        band_hi: The strip's upper Y edge.
        pad: Horizontal air added to each side of each interval.

    Returns:
        One interval per segment whose ink crosses the strip.
    """
    intervals: List[Tuple[float, float]] = []
    for polyline in ink:
        for index in range(len(polyline) - 1):
            interval = segment_band_interval(
                polyline[index], polyline[index + 1], band_lo, band_hi, pad
            )
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
        line_y: The underline centerline's Y (the band is centered on it).
        cutter: The cutter diameter cutting both the underline and the glyphs.
            The cut is padded by ``0.5 * cutter + clearance``: the half cutter
            is the glyph stroke's own half-width (the ink is a centerline).
        clearance: Horizontal air left at each cut edge (and the source of the
            band's thin reach above and below the centerline, see
            :data:`BAND_TOLERANCE_FRACTION`).
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

    # The trigger band is a thin strip on the underline's centerline: ink
    # crosses it (and cuts) where the glyph passes through the underline. A
    # shallow graze of the lower half (a `g` bowl bottom) stays below the
    # band and cuts nothing.
    reach = BAND_TOLERANCE_FRACTION * clearance
    pad = 0.5 * cutter + clearance
    intervals = [
        (max(lo, x_lo), min(hi, x_hi))
        for lo, hi in _ink_band_intervals(ink, line_y - reach, line_y + reach, pad)
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
