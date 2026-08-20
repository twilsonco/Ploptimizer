"""Single-line TrueType font text rendering via matplotlib's raw path codes.

Replaces vpype's internal Hershey stroke-font engine (``vp.text_block()``)
and the earlier ``vpype-ttf``/FreeType approach with low-level access to the
raw TrueType drawing instructions through :class:`matplotlib.textpath.TextPath`.

Why this is more robust than geometry heuristics:

The previous implementation rendered glyphs as closed polygons and then tried
to *guess* which straight segment was an erroneous "closing chord" by scanning
for the longest line in each loop. That heuristic fails on characters whose
intended strokes are physically longer than the closing chord (e.g. digits 1,
4, and 7), causing the wrong stroke to be deleted.

matplotlib exposes the literal path codes a font designer encoded:

- ``MOVETO`` starts an open stroke / contour.
- ``LINETO``/curve commands continue it.
- A final ``LINETO`` returns to the start point (the TrueType closing chord).
- ``CLOSEPOLY`` carries no geometry.

For single-line engraving fonts every intended *open* stroke is therefore
emitted as: MOVETO -> ...points... -> LINETO(back to start) -> CLOSEPOLY.
The erroneous chord is always that final ``LINETO`` returning exactly to the
origin, so we can drop it deterministically instead of guessing. Genuine loops
(e.g. "0", "8", "o") have a microscopic closing step and are preserved intact.

Coordinate convention (matches matplotlib/plotter):

- Baseline sits at y=0; glyphs extend upward into positive Y.
- Output scales linearly with the requested point size, so we normalize to the
  target toolpath height in inches. The result is drop-in compatible with the
  rest of the label/plate layout pipeline (which works entirely in inches).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import vpype as vp
from matplotlib.font_manager import FontProperties
from matplotlib.path import Path as MplPath
from matplotlib.textpath import TextPath

logger = logging.getLogger(__name__)

# TrueType forces every contour closed, so a single-line stroke becomes:
#   MOVETO -> ...points... -> LINETO(back to start) -> CLOSEPOLY.
# The final ``LINETO`` back to the origin is the erroneous closing chord for an
# open stroke; genuine loops (like "0" or "o") end with a microscopic step.
#
# Measured in normalized inches (after scaling to toolpath height), genuine
# loop-closing steps are tiny while erroneous chords are >= ~70 thousandths of
# an inch for this font. A threshold of 2 CSS pixels (~20.8 thousandths-inch,
# vpype uses 96 px/inch) safely isolates long chords while protecting tight
# curves.
CHORD_THRESHOLD_INCHES: float = 0.02

# Default bundled single-line engraving font. Resolved relative to this module
# so it works regardless of the current working directory at runtime.
DEFAULT_FONT_PATH: Path = (
    Path(__file__).resolve().parent.parent.parent
    / "Fonts"
    / "ReliefSingleLine"
    / "ReliefSingleLineCAD-Regular.ttf"
)

# Resolution factor used internally by TextPath when rasterizing glyph outlines.
_FTEXT_RESOLUTION: int = 1024


def _split_contours(text_path: TextPath) -> list[np.ndarray]:
    """Split a matplotlib path into its raw contours, preserving all geometry.

    Each contour is emitted as ``MOVETO`` followed by points and a final
    ``LINETO`` back to its origin plus a geometry-less ``CLOSEPOLY``. This
    function only groups the vertices; closing-chord removal happens later in
    inch space (see :func:`_remove_closing_chords`).

    Args:
        text_path: A matplotlib :class:`TextPath` containing glyph outlines in
            points (baseline at y=0, +y upward).

    Returns:
        A list of numpy complex arrays. Each array is one raw contour including
        its final closing chord back to the origin.
    """
    contours: list[np.ndarray] = []
    current: list[complex] = []

    def _flush() -> None:
        """Finalize and append the in-progress contour."""
        nonlocal current
        if not current:
            return
        contours.append(np.asarray(current, dtype=complex))
        current = []

    for vertex, code in text_path.iter_segments(simplify=False, curves=False):
        c = int(code)
        if c == MplPath.MOVETO:
            _flush()
            current = [complex(float(vertex[0]), float(vertex[1]))]
        elif c in (MplPath.LINETO, MplPath.CURVE3, MplPath.CURVE4):
            # curves=False flattens beziers into LINETOs; curve codes are
            # handled defensively.
            current.append(complex(float(vertex[0]), float(vertex[1])))
        elif c == MplPath.CLOSEPOLY:
            # CLOSEPOLY carries no geometry and the closing chord was already
            # emitted as a LINETO; nothing to do here.
            continue

    _flush()
    return contours


def _remove_closing_chords(lc: vp.LineCollection) -> vp.LineCollection:
    """Remove erroneous closing chords from glyph outlines (in inches).

    Every contour ends with a ``LINETO`` back to its origin. For an open stroke
    that final segment is the long, erroneous TrueType closing chord and must be
    dropped; for a genuine closed loop it is microscopic and preserved.

    Args:
        lc: A LineCollection of glyph outlines in inches (baseline at y=0).

    Returns:
        A new LineCollection with erroneous closing chords removed.
    """
    cleaned = vp.LineCollection()
    for line in lc:
        if len(line) > 2 and abs(line[0] - line[-1]) < 1e-5:
            last_seg = abs(line[-1] - line[-2])
            if last_seg <= CHORD_THRESHOLD_INCHES:
                # Genuine closed loop: keep the full contour.
                cleaned.append(np.asarray(line))
            else:
                # Open stroke: drop the erroneous closing chord (the final
                # LINETO back to origin).
                cleaned.append(np.asarray(line[:-1]))
        elif len(line) > 2 and abs(line[0] - line[-1]) >= 1e-5:
            # Not closed; keep as-is.
            cleaned.append(np.asarray(line))
        else:
            cleaned.append(np.asarray(line))
    return cleaned


def render_text_line_ftext(
    text: str,
    target_height_inches: float,
    font_path: Optional[Path] = None,
) -> vp.LineCollection:
    """Render a single line of text with the Relief Single Line TTF font.

    Uses matplotlib's :class:`TextPath` to access the raw TrueType drawing
    instructions, then flattens curves and drops erroneous closing chords so
    open strokes (e.g. "1", "4", "7") render correctly while genuine loops are
    preserved. Normalizes orientation and scale so that:

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
        # 1024 points per em keeps the raw geometry high-resolution; it scales
        # linearly with size so normalization below yields exact target height.
        font_props = FontProperties(fname=str(resolved_font))
        text_path = TextPath((0, 0), text, prop=font_props, size=_FTEXT_RESOLUTION)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("matplotlib rendering failed for %r: %s", text, exc)
        return vp.LineCollection()

    contours = _split_contours(text_path)
    if not contours:
        return vp.LineCollection()

    lc = vp.LineCollection()
    for contour in contours:
        lc.append(contour)

    bounds = lc.bounds()
    if bounds is None:
        return vp.LineCollection()

    current_height = bounds[3] - bounds[1]
    scale = target_height_inches / current_height if current_height > 0 else 1.0

    # matplotlib emits upright glyphs (baseline at y=0, +y up) in plotter
    # convention. Apply uniform scaling to reach exactly the requested height.
    scaled = vp.LineCollection()
    for line in lc:
        scaled.append(line * scale)

    # Drop erroneous closing chords forced by TrueType's closed-path outlines,
    # so open strokes like "1", "4", "7" don't get a straight connector back to
    # their start, while genuine loops are preserved.
    return _remove_closing_chords(scaled)
