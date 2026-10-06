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

- Baseline sits at y=0; glyphs extend upward into positive Y. Descenders
  (``g``, ``p``, ``y``) extend below the baseline into negative Y.
- Scale is a fixed per-font reference: the ink height of the reference glyph
  ``"H"`` (the TrueType analogue of the PLT library's reference character)
  equals the requested toolpath height exactly, on *every* text line. Scaling
  each line by its own rendered ink box instead (the historical behaviour)
  made every glyph on a line shrink whenever that line contained descenders,
  ascenders, or tall punctuation, so identical ``text_height`` values produced
  visibly different glyph sizes across lines. The result is drop-in compatible
  with the rest of the label/plate layout pipeline (which works entirely in
  inches).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import vpype as vp
from matplotlib.font_manager import FontProperties
from matplotlib.ft2font import FT2Font
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

# The reference glyph whose ink height defines the per-font scale (the
# TrueType analogue of the PLT library's reference character, e.g. "E").
_REFERENCE_CHAR: str = "H"


class FtextRenderError(ValueError):
    """Raised when a TrueType font lacks glyphs required by a text line.

    Mirrors :class:`plt_optimizer.generate.plt_font_renderer.PltFontRenderError`
    for the TTF family: matplotlib silently substitutes the ``.notdef`` box
    (a full-height rectangle) for characters a font does not cover, so glyph
    coverage is probed up front and reported loudly instead of engraving
    empty rectangles.
    """


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


@lru_cache(maxsize=16)
def _reference_cap_height(font_path: Path) -> float:
    """Return the raw-unit ink height of the reference glyph ``"H"``.

    The cap height of ``"H"`` is a fixed per-font metric (the TrueType
    analogue of the PLT library's reference character), so every text line of
    a font renders at the same glyph scale regardless of the line's own ink
    box.

    Args:
        font_path: Resolved ``.ttf`` file path.

    Returns:
        The reference glyph's ink height in raw :data:`_FTEXT_RESOLUTION`
        units, or ``0.0`` when the font has no usable ``"H"`` glyph (callers
        then fall back to per-line ink-box normalization).
    """
    try:
        font_props = FontProperties(fname=str(font_path))
        text_path = TextPath((0, 0), _REFERENCE_CHAR, prop=font_props, size=_FTEXT_RESOLUTION)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("cap-height probe failed for %s: %s", font_path, exc)
        return 0.0
    contours = _split_contours(text_path)
    if not contours:
        logger.warning("reference glyph %r produced no contours for %s", _REFERENCE_CHAR, font_path)
        return 0.0
    points = np.concatenate(contours)
    height = float(points.imag.max() - points.imag.min())
    if height <= 0.0:
        logger.warning(
            "reference glyph %r has non-positive height for %s", _REFERENCE_CHAR, font_path
        )
        return 0.0
    return height


def _normalization_scale(
    font_path: Path,
    raw_contours: List[np.ndarray],
    target_height_inches: float,
) -> float:
    """Return the uniform scale from raw font units to inches for one line.

    Primary path: ``target_height / cap_height("H")`` -- a fixed per-font
    reference, so lines with descenders, ascenders, or tall punctuation no
    longer shrink every glyph on the line. Fallback (a font without a usable
    ``"H"``): the historical per-line ink-box normalization, logged at WARNING
    because it reproduces the cross-line height drift.

    Args:
        font_path: Resolved ``.ttf`` file path.
        raw_contours: The line's raw contours (font units, baseline at y=0);
            used only by the fallback.
        target_height_inches: Desired reference-glyph height in inches.

    Returns:
        The uniform scale factor.
    """
    cap_height = _reference_cap_height(font_path)
    if cap_height > 0.0:
        return target_height_inches / cap_height
    if not raw_contours:
        return 1.0
    points = np.concatenate(raw_contours)
    ink_height = float(points.imag.max() - points.imag.min())
    if ink_height <= 0.0:
        return 1.0
    logger.warning(
        "font %s has no usable reference glyph %r; falling back to per-line "
        "ink-box normalization (glyph heights drift between text lines)",
        font_path,
        _REFERENCE_CHAR,
    )
    return target_height_inches / ink_height


def _assert_glyph_coverage(font_path: Path, text: str) -> None:
    """Raise :class:`FtextRenderError` when ``font_path`` lacks glyphs for ``text``.

    matplotlib silently substitutes the ``.notdef`` box (a full-height
    rectangle) for uncovered characters, so coverage is probed through the
    font's character map before rendering (mirrors the PLT family's
    ``PltFontRenderError`` contract). Whitespace is skipped. A failed probe
    (unreadable font file) is logged and ignored -- rendering proceeds and
    fails later in matplotlib if the font is truly broken.

    Args:
        font_path: Resolved ``.ttf`` file path.
        text: The string about to be rendered.

    Raises:
        FtextRenderError: If one or more characters have no glyph in the font.
    """
    chars = [ch for ch in dict.fromkeys(text) if not ch.isspace()]
    if not chars:
        return
    try:
        font = FT2Font(str(font_path))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("glyph coverage probe failed for %s: %s", font_path, exc)
        return
    missing = [ch for ch in chars if font.get_char_index(ord(ch)) == 0]
    if missing:
        listing = ", ".join(repr(ch) for ch in missing)
        raise FtextRenderError(f"font {font_path.name!r} has no glyphs for: {listing}")


def render_text_line_ftext(
    text: str,
    target_height_inches: float,
    font_path: Optional[Path] = None,
    *,
    check_glyph_coverage: bool = True,
) -> vp.LineCollection:
    """Render a single line of text with the Relief Single Line TTF font.

    Uses matplotlib's :class:`TextPath` to access the raw TrueType drawing
    instructions, then flattens curves and drops erroneous closing chords so
    open strokes (e.g. "1", "4", "7") render correctly while genuine loops are
    preserved. Normalizes orientation and scale so that:

    - Glyphs are upright in plotter convention (baseline at y=0, +y upward);
      descenders (``g``, ``p``, ``y``) hang below the baseline.
    - The font's reference cap height (the ink height of ``"H"``) equals
      ``target_height_inches`` exactly on *every* line: glyph size is a
      per-font constant, independent of the line's own ink box.

    Args:
        text: The string to render.
        target_height_inches: Desired reference-glyph (cap) height in inches.
        font_path: Optional path to the TTF font. Defaults to the bundled
            Relief Single Line CAD font.
        check_glyph_coverage: When True (the default), characters the font
            lacks raise :class:`FtextRenderError` instead of silently
            engraving ``.notdef`` boxes. The font-showcase tool disables the
            probe deliberately: rendering the ``.notdef`` box *is* the
            coverage information a showcase reports.

    Returns:
        A vpype.LineCollection containing the rendered glyph outlines, or an
        empty collection if text is blank or rendering fails.

    Raises:
        FtextRenderError: If the font lacks a glyph for a character in
            ``text`` and ``check_glyph_coverage`` is True (matplotlib would
            silently engrave ``.notdef`` boxes).
    """
    if not text:
        return vp.LineCollection()

    resolved_font = font_path if font_path is not None else DEFAULT_FONT_PATH
    if check_glyph_coverage:
        _assert_glyph_coverage(resolved_font, text)

    try:
        # 1024 points per em keeps the raw geometry high-resolution; it scales
        # linearly with size so the reference normalization below yields the
        # exact target cap height.
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

    scale = _normalization_scale(resolved_font, contours, target_height_inches)

    # matplotlib emits upright glyphs (baseline at y=0, +y up) in plotter
    # convention. Apply the uniform per-font scale; descenders land below y=0.
    scaled = vp.LineCollection()
    for line in lc:
        scaled.append(line * scale)

    # Drop erroneous closing chords forced by TrueType's closed-path outlines,
    # so open strokes like "1", "4", "7" don't get a straight connector back to
    # their start, while genuine loops are preserved.
    return _remove_closing_chords(scaled)


def _render_scaled_line(
    text: str,
    font_props: FontProperties,
    scale: float,
) -> vp.LineCollection:
    """Render ``text`` at the bundled font and apply a precomputed uniform scale.

    Shared low-level path for :func:`render_text_line_ftext_with_words`: raw
    contours are flattened and closing chords removed exactly as in the
    whole-line render, but the caller supplies the scale factor so word
    geometry lands in the whole line's coordinate space.

    Args:
        text: The string to render (typically a single word).
        font_props: Resolved font properties for the bundled TTF font.
        scale: Uniform scale from raw font units to inches.

    Returns:
        Chord-free :class:`vpype.LineCollection` in the scaled frame, or an
        empty collection when rendering fails or the text has no contours.
    """
    try:
        text_path = TextPath((0, 0), text, prop=font_props, size=_FTEXT_RESOLUTION)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("matplotlib rendering failed for %r: %s", text, exc)
        return vp.LineCollection()

    contours = _split_contours(text_path)
    if not contours:
        return vp.LineCollection()

    scaled = vp.LineCollection()
    for contour in contours:
        scaled.append(contour * scale)
    return _remove_closing_chords(scaled)


def _contour_signature(contour: np.ndarray) -> str:
    """Return a translation-invariant signature for one contour.

    The signature is the rounded list of vertex offsets relative to the
    contour's first vertex. Two contours that are pure translations of each
    other (a word rendered standalone vs the same word inside a whole-line
    render) produce identical signatures.

    Rounding is at 1e-5 inch (10 micro-inch) precision: 100x finer than the
    plotter's 1/1000-inch unit and far below any real glyph-shape difference,
    yet coarse enough that 1-ulp float noise in the scaled vertex products
    (whole-line vertices carry a glyph-position translation the standalone
    word render lacks, so their scaled offsets differ in the last ulp) cannot
    straddle a rounding boundary and split a glyph's signature.

    Args:
        contour: Complex vertex array of one contour.

    Returns:
        Stable string key for signature matching.
    """
    origin = contour[0]
    offsets = np.round(contour - origin, 5)
    return "|".join(f"{z.real:.5f}:{z.imag:.5f}" for z in offsets)


def render_text_line_ftext_with_words(
    text: str,
    target_height_inches: float,
    font_path: Optional[Path] = None,
    *,
    check_glyph_coverage: bool = True,
) -> Tuple[vp.LineCollection, List[Tuple[str, List[int]]]]:
    """Render a line of text and partition its contours by whitespace-delimited word.

    The whole line is rendered exactly as :func:`render_text_line_ftext`
    (single matplotlib path, per-font reference scale, chord removal),
    then each word is rendered standalone and its contours are matched into
    the whole-line render by translation-invariant vertex signatures. The
    returned word groups therefore *are* the whole-line contours partitioned
    by word -- bit-exact modulo float rounding, with no drift from advance
    arithmetic -- giving the optimizer exact stroke membership per word.

    Words are split on single spaces (the schema's text content carries no
    tabs/newlines). Runs of spaces yield empty groups, preserving column
    alignment. If any word contour cannot be matched into the whole-line
    render (unexpected font/pathology case), the word groups are reported as
    ``[]`` and the caller should fall back to whole-line (ungrouped) mode.

    Args:
        text: The string to render.
        target_height_inches: Desired glyph height in inches.
        font_path: Optional path to the TTF font. Defaults to the bundled
            Relief Single Line CAD font.

    Returns:
        ``(whole_lc, word_groups)`` where ``whole_lc`` is identical to
        :func:`render_text_line_ftext` output, and ``word_groups`` lists one
        ``(word_text, contour_indices)`` pair per whitespace-delimited
        segment in text order (blank segments yield empty index lists).
        ``contour_indices`` index into ``whole_lc`` in original contour
        order. ``word_groups`` is ``[]`` when grouping is unavailable.
    """
    whole = render_text_line_ftext(
        text,
        target_height_inches,
        font_path=font_path,
        check_glyph_coverage=check_glyph_coverage,
    )
    if whole.is_empty():
        return whole, []

    resolved_font = font_path if font_path is not None else DEFAULT_FONT_PATH
    font_props = FontProperties(fname=str(resolved_font))

    # Recompute the whole-line normalization scale so word contours can be
    # scaled into the exact same frame before matching.
    try:
        raw_path = TextPath((0, 0), text, prop=font_props, size=_FTEXT_RESOLUTION)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("matplotlib rendering failed for %r: %s", text, exc)
        return whole, []
    raw_contours = _split_contours(raw_path)
    if not raw_contours:  # pragma: no cover - whole render already succeeded
        return whole, []
    scale = _normalization_scale(resolved_font, raw_contours, target_height_inches)

    # Multimap of translation-invariant signatures over the whole-line
    # contours, consumed as words claim them (handles repeated glyphs).
    whole_contours = [np.asarray(line) for line in whole]
    pool: dict[str, List[int]] = {}
    for idx, contour in enumerate(whole_contours):
        if len(contour) == 0:
            continue
        pool.setdefault(_contour_signature(contour), []).append(idx)

    groups: List[Tuple[str, List[int]]] = []
    for word in text.split(" "):
        indices: List[int] = []
        if word:
            word_lc = _render_scaled_line(word, font_props, scale)
            if word_lc.is_empty():  # pragma: no cover - defensive
                logger.warning(
                    "ftext grouped render: word %r produced no contours; "
                    "falling back to whole-line grouping",
                    word,
                )
                return whole, []
            for contour in word_lc:
                key = _contour_signature(np.asarray(contour))
                candidates = pool.get(key)
                if not candidates:
                    logger.warning(
                        "ftext grouped render: contour of word %r unmatched in "
                        "whole-line render; falling back to whole-line grouping",
                        word,
                    )
                    return whole, []
                indices.append(candidates.pop(0))
                if not candidates:
                    del pool[key]
            indices.sort()
        groups.append((word, indices))

    return whole, groups
