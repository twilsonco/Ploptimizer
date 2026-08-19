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

import vpype as vp
import vpype_cli  # noqa: F401  (registers plugin commands)
import vpype_ttf  # noqa: F401  (registers the ``ftext`` command)

logger = logging.getLogger(__name__)

# Resolution factor used internally by ftext when rasterizing glyph outlines.
_FTEXT_RESOLUTION: int = 1024

# TrueType forces every outline path closed, so the renderer appends a
# duplicate of the first coordinate as the last. For open strokes (like "C")
# this creates an erroneous straight chord from the stroke's end back to its
# start; for genuinely closed loops (like "o") it is only the microscopic final
# step that lands on the starting point.
#
# We distinguish the two by measuring the closing segment length. Measured in
# normalized inches (after scaling to toolpath height), genuine loop closures
# measure ~0 while erroneous chords are >= ~70 thousandths of an inch for this
# font. A threshold of 2 CSS pixels (~20.8 thousandths-inch, vpype uses
# 96 px/inch) safely isolates long chords while protecting tight curves.
CHORD_THRESHOLD_INCHES: float = 2.0 / 72.0

# Default bundled single-line engraving font. Resolved relative to this module
# so it works regardless of the current working directory at runtime.
DEFAULT_FONT_PATH: Path = (
    Path(__file__).resolve().parent.parent.parent
    / "Fonts"
    / "ReliefSingleLine"
    / "ReliefSingleLineCAD-Regular.ttf"
)


def _remove_closing_chords(lc: vp.LineCollection) -> vp.LineCollection:
    """Remove erroneous closing chords forced by the TrueType renderer.

    FreeType forces every glyph outline closed by duplicating its first
    coordinate as the last. For open single-line strokes (e.g. "C", "D") this
    produces a long straight chord from the stroke's end back to its start.
    Genuinely closed loops (e.g. "o") only gain a microscopic final step.

    This helper drops that forced closing point when it represents a large
    jump, leaving genuine loop closures untouched.

    Args:
        lc: The LineCollection of rendered glyph outlines in inches.

    Returns:
        A new LineCollection with erroneous closing chords removed.
    """
    cleaned = vp.LineCollection()
    for line in lc:
        if len(line) > 2 and abs(line[0] - line[-1]) < 1e-5:
            # The renderer forced the path closed; check how big that jump is.
            closing_segment_length = abs(line[-2] - line[-1])
            if closing_segment_length > CHORD_THRESHOLD_INCHES:
                cleaned.append(line[:-1])
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
