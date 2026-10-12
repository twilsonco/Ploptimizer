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

**Small caps** (``small_caps_scale``, the cascaded ``sc_scale``; the caller
resolves the ``"<base> SC"`` registry variant): matplotlib's
:class:`~matplotlib.textpath.TextPath` ignores OpenType ``smcp``, so the
variant is synthesized -- the text is mapped per character (ASCII lowercase
``a-z`` -> uppercase, every other character verbatim, strictly 1:1) and the
mapped string renders through the normal path, keeping the font's natural
uppercase advances (the standard synthetic small-caps spacing). Each
lowercase-origin glyph's contours are then uniformly shrunk to
``small_caps_scale`` of the cap height **top-down, about the glyph's own
bottom-left ink corner** (its bottom stays exactly in place, its top
descends, and its left edge -- the pen-advance start -- stays put too).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

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

# The characters a small-caps font transforms: ASCII lowercase only (the
# user-facing contract). Accented lowercase, eszett, digits, punctuation
# and symbols engrave verbatim at full size, and the map is strictly 1:1
# per character, so word boundaries and the per-glyph contour slicing stay
# exact on the mapped string.
_ASCII_LOWER: frozenset[str] = frozenset("abcdefghijklmnopqrstuvwxyz")


def _ascii_upper_map(text: str) -> str:
    """Return ``text`` with ASCII lowercase ``a-z`` mapped to uppercase.

    Every other character maps to itself (1:1, length-preserving), so word
    boundaries, glyph positions and the per-character contour slicing are
    identical between the original and the mapped string.

    Args:
        text: The original line text.

    Returns:
        The string whose glyphs are rendered.
    """
    return "".join(ch.upper() if ch in _ASCII_LOWER else ch for ch in text)


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


def _per_glyph_counts(
    text: str,
    font_path: Path,
    contour_count: int,
    *,
    small_caps: bool = False,
) -> Optional[List[Tuple[str, int]]]:
    """Return ``(char, contour_count)`` per non-space character of a line.

    matplotlib emits contours glyph-major (each glyph's contours contiguous)
    and each glyph's contour count is a per-font constant, so the counts
    slice a whole-line render per character exactly. Under small caps the
    render used the mapped string, so the counts probe the **uppercase**
    source glyphs while the pairs stay keyed by the original characters
    (the map is 1:1).

    Args:
        text: The line's text (the original, not the mapped string).
        font_path: Resolved ``.ttf`` file path.
        contour_count: Number of contours in the whole-line render.
        small_caps: Whether the render used the small-caps mapped string.

    Returns:
        The per-character pairs in text order, or ``None`` when the counts
        do not sum to ``contour_count`` (unexpected font/pathology case).
    """
    chars = [ch for ch in text if ch != " "]
    sources = [_ascii_upper_map(ch) for ch in chars] if small_caps else chars
    counts = [_glyph_contour_count(font_path, ch) for ch in sources]
    if sum(counts) != contour_count:
        return None
    return list(zip(chars, counts))


def _apply_small_caps(
    scaled: vp.LineCollection,
    text: str,
    font_path: Path,
    factor: float,
) -> vp.LineCollection:
    """Shrink the lowercase-origin glyph contours of a scaled line in place.

    The line's contours are sliced per character (see
    :func:`_per_glyph_counts`, probing the **mapped** uppercase glyphs' counts
    while keying the slices by the original characters); every slice whose
    original character is ASCII lowercase is scaled uniformly by ``factor``
    **about that glyph's own bottom-left ink corner**: the glyph's bottom
    stays exactly where it is (it sits on the baseline like a shrunken
    capital) and its top descends, while its left edge -- the pen-advance
    start -- stays put too.

    Args:
        scaled: The line's contours in inches (baseline at y=0), before
            closing-chord removal.
        text: The ORIGINAL line text (the gate reads its lowercase
            characters; the render used the mapped string).
        font_path: Resolved ``.ttf`` file path.
        factor: The small-caps downscale to apply.

    Returns:
        The line with the lowercase-origin glyphs shrunk. When the per-glyph
        contour counts do not line up (unexpected font/pathology case), the
        line is returned unchanged with a WARNING -- the natural uppercase
        rendering is the graceful fallback.
    """
    pairs = _per_glyph_counts(text, font_path, len(scaled), small_caps=True)
    if pairs is None:
        counts_total = sum(
            _glyph_contour_count(font_path, _ascii_upper_map(ch)) for ch in text if ch != " "
        )
        logger.warning(
            "small-caps TTF render: per-glyph contour counts (%d) do not sum to the "
            "whole-line contour count (%d); rendering at full size",
            counts_total,
            len(scaled),
        )
        return scaled

    lines = [np.asarray(line) for line in scaled]
    cursor = 0
    for char, count in pairs:
        glyph_indices = list(range(cursor, cursor + count))
        cursor += count
        if char not in _ASCII_LOWER:
            continue
        points = np.concatenate([lines[index] for index in glyph_indices if len(lines[index])])
        # Anchor = the glyph's own bottom-left ink corner (x = its pen-advance
        # start, y = its lowest ink), so the shrink is purely top-down/inward.
        anchor = complex(float(points.real.min()), float(points.imag.min()))
        for index in glyph_indices:
            line = lines[index]
            lines[index] = anchor + (line - anchor) * factor
    return vp.LineCollection(lines)


def _shear_contour(line: np.ndarray, tan: float) -> np.ndarray:
    """Shear one contour about the baseline ``y = 0`` (``x' = x + y * tan``).

    The synthetic italic for a TTF with no native italic face: the baseline
    stays fixed and the glyph leans right, matching the PLT shear exactly.
    """
    sheared = line + 1j * (line.imag * tan)
    return np.asarray(sheared, dtype=complex)


def _render_glyph_contours(
    char: str,
    font_path: Path,
    scale: float,
) -> Optional[List[np.ndarray]]:
    """Render one character's raw contours (font units scaled to inches).

    Used to splice a native italic face's glyph into a whole-line render.
    Closing chords are NOT removed (the caller runs chord removal on the
    spliced whole line, matching the pre-chord-removal slice frame).

    Args:
        char: The single character to render.
        font_path: The ``.ttf`` path (the italic face).
        scale: Uniform scale from raw font units to inches.

    Returns:
        The scaled contours, or ``None`` when the render yields none.
    """
    try:
        font_props = FontProperties(fname=str(font_path))
        text_path = TextPath((0, 0), char, prop=font_props, size=_FTEXT_RESOLUTION)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("native-italic glyph render failed for %r: %s", char, exc)
        return None
    contours = _split_contours(text_path)
    if not contours:  # pragma: no cover - defensive (a glyph slice is never blank)
        return None
    return [contour * scale for contour in contours]


def _apply_italic(
    scaled: vp.LineCollection,
    text: str,
    font_path: Path,
    scale: float,
    italic_tan: float,
    italic_flags: Optional[Sequence[bool]],
    italic_face: Optional[Path],
    small_caps: bool = False,
) -> vp.LineCollection:
    """Italicize the flagged glyph contours of a scaled line.

    The line's contours are sliced per character (see
    :func:`_per_glyph_counts`); every flagged glyph is italicized in place.
    With a native ``italic_face`` its glyph contours are spliced in
    (translated onto the regular glyph's ink-left/baseline, keeping the
    regular face's advance); without one -- or when the italic face's glyph
    contour count does not match the regular glyph's -- the glyph is sheared
    about the baseline (``x' = x + y * italic_tan``), the synthetic italic.

    Args:
        scaled: The line's contours in inches (baseline at y=0), before
            closing-chord removal.
        text: The line's text (spaces own no contours).
        font_path: The regular ``.ttf`` path (per-glyph contour slicing).
        scale: The whole line's uniform scale (for the native-face splice).
        italic_tan: Shear factor for the synthetic fallback.
        italic_flags: Per-character italic selection aligned with ``text``;
            ``None`` italicizes every character.
        italic_face: The native italic face path, or ``None`` (shear only).
        small_caps: True when the render used the small-caps mapped string
            (the per-glyph slicing then probes the uppercase sources, as in
            :func:`_apply_small_caps`).

    Returns:
        The line with the flagged glyphs italicized. When the per-glyph
        contour counts do not line up, the line is returned unchanged with a
        WARNING (the natural upright render is the graceful fallback).
    """
    pairs = _per_glyph_counts(text, font_path, len(scaled), small_caps=small_caps)
    if pairs is None:
        counts_total = sum(_glyph_contour_count(font_path, ch) for ch in text if ch != " ")
        logger.warning(
            "italic TTF render: per-glyph contour counts (%d) do not sum to the "
            "whole-line contour count (%d); rendering upright",
            counts_total,
            len(scaled),
        )
        return scaled

    occurrences = [index for index, ch in enumerate(text) if ch != " "]
    # Walk the text's non-space characters in order: the contour slices are
    # glyph-major, so each occurrence owns its own slice and styles
    # independently (the same character can be plain and italic on one line).
    flags: Sequence[bool] = (True,) * len(text) if italic_flags is None else italic_flags
    lines = [np.asarray(line) for line in scaled]
    cursor = 0
    for char, count in pairs:
        glyph_indices = list(range(cursor, cursor + count))
        cursor += count
        text_index = _pop_occurrence(occurrences)
        if not flags[text_index]:
            continue
        spliced: Optional[List[np.ndarray]] = None
        # Under small caps the regular slice is already the shrunk uppercase
        # glyph; splicing a full-height italic glyph would undo the shrink,
        # so small-caps italics always take the shear (of the small glyph).
        if italic_face is not None and not small_caps:
            spliced = _splice_native_italic(lines, glyph_indices, char, italic_face, scale)
        if spliced is not None:
            for position, index in enumerate(glyph_indices):
                lines[index] = spliced[position]
        else:
            for index in glyph_indices:
                lines[index] = _shear_contour(lines[index], italic_tan)
    return vp.LineCollection(lines)


def _pop_occurrence(occurrences: List[int]) -> int:
    """Return and remove the first text index in ``occurrences``.

    Args:
        occurrences: Remaining non-space character indices (ascending).

    Returns:
        The first index (the caller consumed its glyph slice).

    Raises:
        ValueError: When the pairs and the text disagree (contour counts
            already validated against the text; this guards pathology).
    """
    if not occurrences:  # pragma: no cover - the counts validated the pairs
        raise ValueError("italic glyph slice exceeds the text's characters")
    return occurrences.pop(0)


def _splice_native_italic(
    lines: List[np.ndarray],
    glyph_indices: List[int],
    char: str,
    italic_face: Path,
    scale: float,
) -> Optional[List[np.ndarray]]:
    """Splice one glyph's contours from the native italic face.

    The italic glyph is rendered standalone and translated so its ink-left
    and baseline land on the regular glyph's ink-left and baseline (the
    regular face keeps the advance). Returns ``None`` when the italic glyph's
    contour count does not match the regular glyph's (the caller falls back
    to the shear).
    """
    present = [lines[index] for index in glyph_indices if len(lines[index])]
    if not present:  # pragma: no cover - a glyph slice carries contours
        return None
    italic_contours = _render_glyph_contours(char, italic_face, scale)
    if italic_contours is None or len(italic_contours) != len(glyph_indices):
        return None
    regular = np.concatenate(present)
    target_left = float(regular.real.min())
    target_base = float(regular.imag.min())
    italic_points = np.concatenate(italic_contours)
    shift = complex(
        target_left - float(italic_points.real.min()),
        target_base - float(italic_points.imag.min()),
    )
    return [contour + shift for contour in italic_contours]


def render_text_line_ftext(
    text: str,
    target_height_inches: float,
    font_path: Optional[Path] = None,
    *,
    check_glyph_coverage: bool = True,
    small_caps_scale: Optional[float] = None,
    italic_tan: Optional[float] = None,
    italic_flags: Optional[Sequence[bool]] = None,
    italic_face: Optional[Path] = None,
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
            coverage information a showcase reports. The probe runs on the
            string actually rendered, i.e. the small-caps mapped string when
            ``small_caps_scale`` is active.
        small_caps_scale: Small-caps mode for ASCII lowercase ``a-z`` (the
            cascaded ``sc_scale``). The text is mapped per character
            (lowercase -> uppercase, everything else verbatim) and the
            mapped string renders with the font's natural uppercase
            advances; each lowercase-origin glyph is then uniformly shrunk
            to this fraction **top-down** (the glyph's own bottom stays
            exactly in place and its top descends), about its own
            bottom-left ink corner. ``None`` (the default) disables the
            transform, keeping every legacy render bit-identical; ``1.0``
            keeps the uppercase map and only skips the shrink (the line
            engraves as full-height capitals).
        italic_tan: Italic shear factor ``tan(italic_angle)`` for ``<i>``
            markup: flagged glyphs are italicized in place (see
            :func:`_apply_italic`) -- spliced from the native ``italic_face``
            when one exists, otherwise sheared about the baseline. ``None``
            (the default) disables the transform entirely, keeping every
            legacy render bit-identical.
        italic_flags: Per-character italic selection aligned with ``text``
            (one flag per character, spaces included); only consulted when
            ``italic_tan`` is set. ``None`` italicizes every character.
        italic_face: The family's native italic ``.ttf`` (the registry's
            sibling-file probe), or ``None`` for the shear-only fallback.

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
    small_caps = small_caps_scale is not None
    render_text = _ascii_upper_map(text) if small_caps else text
    if check_glyph_coverage:
        _assert_glyph_coverage(resolved_font, render_text)

    try:
        # 1024 points per em keeps the raw geometry high-resolution; it scales
        # linearly with size so the reference normalization below yields the
        # exact target cap height.
        font_props = FontProperties(fname=str(resolved_font))
        text_path = TextPath((0, 0), render_text, prop=font_props, size=_FTEXT_RESOLUTION)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("matplotlib rendering failed for %r: %s", render_text, exc)
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

    # Shrink the lowercase-origin glyphs before chord removal, while the
    # contour arrays still line up 1:1 with the per-glyph contour slicing.
    if small_caps_scale is not None:
        scaled = _apply_small_caps(scaled, text, resolved_font, small_caps_scale)

    # Italicize the flagged glyphs in the same pre-chord-removal slicing
    # frame (under small caps the render used the mapped string, so the
    # per-glyph slicing probes the uppercase sources; the map is 1:1).
    if italic_tan is not None:
        scaled = _apply_italic(
            scaled,
            render_text,
            resolved_font,
            scale,
            italic_tan,
            italic_flags,
            italic_face,
            small_caps=small_caps,
        )

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
    plotter's 1/1016-inch unit and far below any real glyph-shape difference,
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
    small_caps_scale: Optional[float] = None,
    italic_tan: Optional[float] = None,
    italic_flags: Optional[Sequence[bool]] = None,
    italic_face: Optional[Path] = None,
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

    Under small caps the signature matcher cannot work -- the lowercase-origin
    glyphs were shrunk after the whole-line render, so they no longer match
    their standalone render -- and the word groups come from the exact
    per-glyph contour slicing instead (see :func:`_per_glyph_counts`; the
    character map is 1:1, so word boundaries are identical).

    Args:
        text: The string to render.
        target_height_inches: Desired glyph height in inches.
        font_path: Optional path to the TTF font. Defaults to the bundled
            Relief Single Line CAD font.
        check_glyph_coverage: When True (the default), characters the font
            lacks raise :class:`FtextRenderError`.
        small_caps_scale: Small-caps mode for ASCII lowercase ``a-z``
            (``None`` = off; see :func:`render_text_line_ftext`).

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
        small_caps_scale=small_caps_scale,
        italic_tan=italic_tan,
        italic_flags=italic_flags,
        italic_face=italic_face,
    )
    if whole.is_empty():
        return whole, []

    resolved_font = font_path if font_path is not None else DEFAULT_FONT_PATH

    if small_caps_scale is not None or italic_tan is not None:
        # The signature matcher cannot work: the shrunk/italicized glyphs no
        # longer match their standalone render, so the word groups come from
        # the exact per-glyph contour slicing instead.
        pairs = _per_glyph_counts(
            text, resolved_font, len(whole), small_caps=small_caps_scale is not None
        )
        if pairs is None:
            logger.warning(
                "ftext grouped render: per-glyph contour counts do not sum "
                "to the whole-line contour count (%d); falling back to "
                "whole-line grouping",
                len(whole),
            )
            return whole, []
        # Contiguous glyph slices -> contiguous word slices (spaces own no
        # contours, so a word's contours are exactly its glyphs' slices).
        word_groups: List[Tuple[str, List[int]]] = []
        cursor = 0
        glyph_iter = iter(pairs)
        for word in text.split(" "):
            word_indices: List[int] = []
            for _char in word:
                _source, count = next(glyph_iter)
                word_indices.extend(range(cursor, cursor + count))
                cursor += count
            word_groups.append((word, word_indices))
        return whole, word_groups

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


def glyph_groups_for_line(
    text: str,
    font_path: Optional[Path],
    contour_count: int,
    *,
    small_caps: bool = False,
) -> List[Tuple[str, List[int]]]:
    """Partition a line's contours per character by per-glyph contour counts.

    matplotlib's :class:`TextPath` emits contours glyph-major (left-to-right,
    each glyph's contours contiguous), so slicing the whole-line contour
    sequence by each character's standalone contour count (a per-font
    constant, see :func:`_glyph_contour_count`) partitions it per character
    exactly. Spaces contribute no contours and no group.

    Args:
        text: The rendered line's text.
        font_path: The TTF font used (``None`` = bundled default).
        contour_count: Number of contours in the whole-line render.
        small_caps: When True, the render used the small-caps mapped string
            (see :func:`_ascii_upper_map`), so the contour counts are probed
            on the **uppercase** source glyphs while the groups stay keyed by
            the original characters (the map is 1:1).

    Returns:
        One ``(char, contour_indices)`` pair per non-space character in text
        order, indices into the whole-line contour sequence. Empty list when
        the per-character counts do not sum to ``contour_count`` (unexpected
        font/pathology case) -- callers then treat the line as
        glyph-group-less.
    """
    resolved_font = font_path if font_path is not None else DEFAULT_FONT_PATH
    chars = [ch for ch in text if ch != " "]
    sources = [_ascii_upper_map(ch) for ch in chars] if small_caps else chars
    counts = [_glyph_contour_count(resolved_font, ch) for ch in sources]
    if sum(counts) != contour_count:
        logger.warning(
            "ftext glyph grouping: per-glyph contour counts (%d) do not sum to "
            "the whole-line contour count (%d); rendering without glyph groups",
            sum(counts),
            contour_count,
        )
        return []

    glyph_groups: List[Tuple[str, List[int]]] = []
    pos = 0
    for ch, count in zip(chars, counts):
        glyph_groups.append((ch, list(range(pos, pos + count))))
        pos += count
    return glyph_groups


@lru_cache(maxsize=4096)
def _glyph_contour_count(font_path: Path, character: str) -> int:
    """Return how many contours the font renders for one character.

    A single-line TTF encodes each glyph as a fixed number of contours (one
    per engraved stroke, plus counters), and matplotlib's :class:`TextPath`
    emits them contiguously in glyph order, so per-character counts slice a
    whole-line render into per-glyph groups. The count is a per-font
    constant, cached per ``(font, character)``.

    Args:
        font_path: Resolved ``.ttf`` file path.
        character: The single character to probe.

    Returns:
        Raw contour count for the glyph (``0`` for blank glyphs such as the
        space).
    """
    font_props = FontProperties(fname=str(font_path))
    text_path = TextPath((0, 0), character, prop=font_props, size=_FTEXT_RESOLUTION)
    return len(_split_contours(text_path))


def render_text_line_ftext_with_glyphs(
    text: str,
    target_height_inches: float,
    font_path: Optional[Path] = None,
    *,
    check_glyph_coverage: bool = True,
    small_caps_scale: Optional[float] = None,
    italic_tan: Optional[float] = None,
    italic_flags: Optional[Sequence[bool]] = None,
    italic_face: Optional[Path] = None,
) -> Tuple[
    vp.LineCollection,
    List[Tuple[str, List[int]]],
    List[Tuple[str, List[int]]],
]:
    """Render a line of text and partition its contours by word and by glyph.

    The whole line renders exactly as :func:`render_text_line_ftext` and the
    word groups come from :func:`render_text_line_ftext_with_words`. Glyph
    groups exploit the fact that matplotlib emits contours glyph-major
    (left-to-right, each glyph's contours contiguous): each character's
    standalone contour count (see :func:`_glyph_contour_count`) is a
    per-font constant, so slicing the whole-line contours by those counts
    partitions them per character exactly -- the slice membership is verified
    by the sum check below.

    Args:
        text: The string to render.
        target_height_inches: Desired glyph height in inches.
        font_path: Optional path to the TTF font. Defaults to the bundled
            Relief Single Line CAD font.
        check_glyph_coverage: When True (the default), a character the font
            lacks raises :class:`FtextRenderError`.
        small_caps_scale: Small-caps mode for ASCII lowercase ``a-z``
            (``None`` = off; see :func:`render_text_line_ftext`).
        italic_tan: Italic shear factor (``None`` = off; see
            :func:`render_text_line_ftext`).
        italic_flags: Per-character italic selection (``None`` = whole
            line; see :func:`render_text_line_ftext`).
        italic_face: Native italic face path (``None`` = shear only).

    Returns:
        ``(whole_lc, word_groups, glyph_groups)`` where ``whole_lc`` and
        ``word_groups`` are identical to
        :func:`render_text_line_ftext_with_words` output, and
        ``glyph_groups`` lists one ``(char, contour_indices)`` pair per
        non-space character in text order (indices into ``whole_lc``).
        ``glyph_groups`` is ``[]`` when the per-character counts do not sum
        to the whole-line contour count (unexpected font/pathology case);
        the caller should then treat the line as glyph-group-less.
    """
    whole, word_groups = render_text_line_ftext_with_words(
        text,
        target_height_inches,
        font_path,
        check_glyph_coverage=check_glyph_coverage,
        small_caps_scale=small_caps_scale,
        italic_tan=italic_tan,
        italic_flags=italic_flags,
        italic_face=italic_face,
    )
    if whole.is_empty():
        return whole, word_groups, []

    small_caps = small_caps_scale is not None
    return (
        whole,
        word_groups,
        glyph_groups_for_line(text, font_path, len(whole), small_caps=small_caps),
    )
