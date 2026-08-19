"""Single-line TrueType font text rendering via the ``vpype-ttf`` plugin.

Replaces vpype's internal Hershey stroke-font engine (``vp.text_block()``)
with a real single-line TTF engraving font (Relief Single Line CAD). Text is
rendered as glyph outlines by FreeType through the ``ftext`` vpype plugin and
returned in plotter coordinate convention (upright, baseline at y=0).

Coordinate notes on ``ftext``:

- The command takes an integer point size via ``-s``; it rasterizes each glyph
  against a fixed resolution of 1024 units per em.
- It emits geometry with a **negative Y scale factor**, so glyph outlines are
  drawn downward from the text baseline (which sits at y=0).
- The rendered outline height scales linearly with the requested size.

This module therefore normalizes each rendered line by flipping the imaginary
component back to upright and applying uniform scaling so that the total bounds
height equals the requested toolpath text height in inches. This makes the
resulting geometry drop-in compatible with the rest of the label/plate layout
pipeline (which works entirely in inches).
"""

from __future__ import annotations

import logging
import shlex
from pathlib import Path
from typing import Optional

import numpy as np
import vpype as vp
import vpype_cli  # noqa: F401  (registers plugin commands)
import vpype_ttf  # noqa: F401  (registers the ``ftext`` command)

logger = logging.getLogger(__name__)

# Resolution factor used internally by ftext when rasterizing glyph outlines.
_FTEXT_RESOLUTION: int = 1024

# TrueType forces every outline path closed, so a single-line stroke becomes a
# continuous geometric loop. The font renderer adds an erroneous straight chord
# that jumps from the true end of the stroke back to its true start (e.g. the
# diagonal connecting the bottom lip of a "C" up to its top lip). Genuinely
# closed loops (like "o") have no such long jump - only microscopic quantized
# steps.
#
# Because the parser may rotate which node starts the array, this chord can sit
# anywhere in the loop. We therefore scan every segment and break the loop at
# its single longest segment when that exceeds a threshold.
#
# Measured in normalized inches (after scaling to toolpath height), genuine
# curve steps are tiny while erroneous chords are >= ~70 thousandths of an inch
# for this font. A threshold of 2 CSS pixels (~20.8 thousandths-inch, vpype uses
# 96 px/inch) safely isolates long chords while protecting tight curves.
CHORD_THRESHOLD_INCHES: float = 0.02

# Default bundled single-line engraving font. Resolved relative to this module
# so it works regardless of the current working directory at runtime.
DEFAULT_FONT_PATH: Path = (
    Path(__file__).resolve().parent.parent.parent
    / "Fonts"
    / "ReliefSingleLine"
    / "ReliefSingleLineCAD-Regular.ttf"
)


def _break_loop_at_chord(line: np.ndarray) -> Optional[np.ndarray]:
    """Break a forced-closed loop at its erroneous chord, if present.

    TrueType forces every outline closed into a continuous geometric loop. The
    array may start at any node of that loop, so the redundant closing chord is
    not necessarily the final segment - it can appear anywhere (e.g. for a "t"
    whose vertical stem was rotated to the end of the array).

    The erroneous chord is specifically the long segment whose endpoint returns
    to ``line[0]`` - i.e. the path's starting point, which TrueType connects back
    to when it closes the outline. This holds even for simple geometric glyphs
    ("1", "4", "7") that contain several legitimately-long straight strokes: only
    one of them returns to the start.

    If no long segment returns to the start (e.g. a closed loop whose array was
    not rotated), fall back to breaking at any single long segment - which is
    correct for curved glyphs like "C" that have exactly one such jump.

    Args:
        line: A closed polyline (first point == last point) in inches.

    Returns:
        The reordered open stroke with the chord removed, or ``None`` if no
        erroneous chord is found (i.e. a genuine loop like "o").
    """
    n = len(line)
    if n <= 2:
        return None

    # Length of every segment in the closed loop.
    segment_lengths = np.abs(np.diff(line))
    longest_idx = int(np.argmax(segment_lengths))
    longest_length = float(segment_lengths[longest_idx])

    # A genuine loop's segments are all microscopic; only an erroneous chord is
    # a massive jump, so leave genuine loops untouched.
    if longest_length <= CHORD_THRESHOLD_INCHES:
        return None

    break_idx: Optional[int] = None

    # Prefer the long segment whose endpoint returns to line[0]: this is always
    # the TrueType closing chord. Simple geometric glyphs ("1", "4", "7") have
    # several legitimately-long straight strokes, but only one returns to start.
    for i in range(n - 1):
        if segment_lengths[i] > CHORD_THRESHOLD_INCHES and abs(line[i + 1] - line[0]) < 1e-5:
            break_idx = i
            break

    # Fall back to the single longest segment (curved glyphs like "C" whose
    # array was not rotated, so no segment returns to start).
    if break_idx is None:
        break_idx = longest_idx

    # The chord connects point `break_idx` to `break_idx + 1`. Break the loop
    # there: the true open stroke starts at `break_idx + 1`, wraps around through
    # the end of the array, and continues from index 0 up to (and including)
    # `break_idx`.
    new_line = np.concatenate((line[break_idx + 1 :], line[: break_idx + 1]))
    return new_line


def _remove_closing_chords(lc: vp.LineCollection) -> vp.LineCollection:
    """Remove erroneous closing chords forced by the TrueType renderer.

    FreeType forces every glyph outline closed into a continuous loop. For open
    single-line strokes (e.g. "C", "D") this adds an erroneous straight chord
    from the stroke's true end back to its true start; genuinely closed loops
    (like "o") only contain microscopic quantized steps.

    Because the parser may rotate which node starts each array, the chord is not
    always at the end. This scans every segment of each closed loop and breaks
    it at its single longest segment when that exceeds a threshold, reordering
    so the toolpath flows correctly from true start to true end.

    Args:
        lc: The LineCollection of rendered glyph outlines in inches.

    Returns:
        A new LineCollection with erroneous closing chords removed.
    """
    cleaned = vp.LineCollection()
    for line in lc:
        if len(line) > 2 and abs(line[0] - line[-1]) < 1e-5:
            broken = _break_loop_at_chord(np.asarray(line))
            if broken is not None:
                cleaned.append(broken)
                continue
        cleaned.append(line)
    return cleaned


def _shell_quote(value: str) -> str:
    """Shell-quote a string for safe embedding in a vpype pipeline command.

    Args:
        value: The raw string to quote.

    Returns:
        A shell-safe quoted form of ``value``.
    """
    return shlex.quote(value)


def render_text_line_ftext(
    text: str,
    target_height_inches: float,
    font_path: Optional[Path] = None,
) -> vp.LineCollection:
    """Render a single line of text with the Relief Single Line TTF font.

    Uses the ``ftext`` plugin to rasterize glyph outlines, then normalizes
    orientation and scale so that:

    - Glyphs are upright in plotter convention (baseline at y=0, +y upward).
    - The total rendered bounds height equals ``target_height_inches``.

    Args:
        text: The string to render.
        target_height_inches: Desired glyph height in inches.
        font_path: Optional path to the TTF font. Defaults to the bundled
            Relief Single Line CAD font.

    Returns:
        A vpype.LineCollection containing the rendered glyph outlines, or an
        empty collection if text is blank or rendering fails.
    """
    if not text:
        return vp.LineCollection()

    resolved_font = font_path if font_path is not None else DEFAULT_FONT_PATH

    try:
        pipeline = (
            f"ftext -s {_FTEXT_RESOLUTION} {_shell_quote(str(resolved_font))} {_shell_quote(text)}"
        )
        doc = vpype_cli.execute(pipeline)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("ftext rendering failed for %r: %s", text, exc)
        return vp.LineCollection()

    lc = doc.layers.get(1)
    if lc is None or lc.is_empty():
        return vp.LineCollection()

    bounds = lc.bounds()
    if bounds is None:
        return vp.LineCollection()

    current_height = bounds[3] - bounds[1]
    scale = target_height_inches / current_height if current_height > 0 else 1.0

    # ftext emits glyphs with a negative Y factor (downward from baseline).
    # Flip the imaginary component and apply uniform scaling to produce
    # upright geometry at exactly the requested height.
    flipped = vp.LineCollection()
    for line in lc:
        flipped.append((line.real * scale) - 1j * (line.imag * scale))

    # Drop erroneous closing chords forced by TrueType's closed-path outlines,
    # so open strokes like "C" don't get a straight connector back to their start.
    return _remove_closing_chords(flipped)
