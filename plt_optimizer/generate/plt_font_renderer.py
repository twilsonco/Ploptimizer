"""Arc-native rendering of PLT-extracted fonts (``Fonts/plt_fonts.json``).

PLT-extracted fonts store every glyph as baseline-normalized HPGL
(``PU``/``PD``/``AA``) in plotter units (1000 units = 1 inch), ``+y`` up,
baseline at ``y = 0`` and left edge at ``x = 0``, with the font's
reference character exactly 1000 units tall. Each glyph also carries its
bounding box and left/right profile envelopes (silhouette samples). This
module lays a whole text line out by walking a cursor glyph-by-glyph and
returns a :class:`~plt_optimizer.generate.text_geometry.TextBlock`: arcs
stay arcs end-to-end (no flattening), so the finished cut file keeps the
native ``AA`` commands of the source font.

Layout contract:

- Glyphs are upright in the label-local render frame (``+y`` up), every
  glyph anchored on its baseline at ``y = 0`` (descenders -- ``g``, ``p``,
  ``_`` -- extend below it automatically), the first glyph's left edge at
  ``x = 0``, and the reference character's height equals
  ``target_height_inches`` exactly (uniform scale
  ``target_height_inches / normalized_ref_height``, so relative glyph
  sizes -- cap height vs. x-height -- are preserved).
- Adjacent glyphs inside a word are kerned with their profile envelopes
  through a *windowed* comparison controlled by
  ``kerning_window_fraction`` (``[0, 1]``, a fraction of the text height;
  default ``0.05``). With ``w = 0.5 * kerning_window_fraction * height``
  and ``p(y)`` the horizontal penetration of the left glyph's right
  silhouette into the right glyph's left silhouette at height ``y``:

  1. each sample ``p(y)`` on the left glyph compares against the deepest
     right-glyph silhouette sample within ``|y' - y| <= w``, so staggered
     pokes (the glyphs approach each other at slightly different heights)
     are still detected; and
  2. the effective penetration is the maximum of those windowed
     penetrations -- the window can only widen the advance relative to
     same-height kerning, never narrow it.

  The origin-to-origin advance is the effective penetration plus the
  clearance::

      clearance = cutter_diameter + character_spacing

  so a fully-close pair keeps exactly ``clearance`` of air at the
tightest height. ``kerning_window_fraction = 0.0`` reproduces the
  historical same-height maximum-penetration kerning exactly. A global
  minimum glyph width (``min_glyph_width``, an absolute inch floor,
  default ``0.0``) clamps the left silhouette outward so zero-width
  glyphs (``!``, ``|``) still reserve real air. Glyph pairs without
  overlapping height (or lacking envelopes) fall back to the left glyph's
  bounding-box width, floored by ``min_glyph_width``.
- A space character carries no glyph::

      space_advance = space_width_fraction * height + character_spacing

  measured from the previous glyph's right edge; no kerning crosses a
  space.
- A character the font does not define raises :class:`PltFontRenderError`
  at render time (never a silent skip or tofu box).

The module is matplotlib-free (core parser + text_geometry only).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from plt_optimizer.core.parser import PLTParser
from plt_optimizer.generate.font_registry import (
    FontNotFoundError,
    PltGlyphEntry,
    load_plt_fonts,
    resolve_font,
)
from plt_optimizer.generate.text_geometry import Stroke, TextBlock, block_from_parser_paths

logger = logging.getLogger(__name__)

# Fallback space advance as a fraction of the rendered text height (added
# to ``character_spacing``) when the caller does not cascade an explicit
# ``space_width_fraction``. A space carries no glyph and no kerning.
SPACE_HEIGHT_FRACTION: float = 0.3

# Fallback global minimum glyph width in inches, used when the caller does
# not cascade an explicit ``min_glyph_width``. ``0.0`` = pure profile
# envelope kerning.
MIN_GLYPH_WIDTH: float = 0.0

# Fallback kerning window as a fraction of the rendered text height,
# used when the caller does not cascade an explicit
# ``kerning_window_fraction``. Each envelope sample compares against the
# opposite silhouette within +/- (half of this fraction) of the text
# height and the effective penetration is the worst (maximum) windowed
# penetration, so staggered pokes can only widen the advance.
# ``0.0`` reproduces the historical same-height kerning exactly.
KERNING_WINDOW_FRACTION: float = 0.05

# Glyphs are stored in plotter units (1000 units per design inch).
_UNITS_PER_INCH: float = 1000.0


class PltFontRenderError(ValueError):
    """Raised when a PLT-extracted font cannot render a requested string.

    Subclasses :class:`ValueError` so callers guarding the pipeline with
    ``except ValueError`` keep working. The label renderer maps this onto
    a :class:`~plt_optimizer.generate.label_renderer.LabelRenderError`.
    """


@dataclass(frozen=True)
class _GlyphGeometry:
    """One glyph parsed into design-inch geometry (cached per font/char).

    Attributes:
        block: Parsed glyph strokes in design inches (1000 units = 1.0),
            +y up, baseline at y=0, left edge at x=0 (the storage frame,
            unit-converted only; arc sweeps verbatim).
        bounding_box: ``(min_x, min_y, max_x, max_y)`` in design inches;
            the stored bbox when present, else the parsed block's bounds.
        left_envelope: Left-silhouette samples ``((x, y), ...)`` in design
            inches, sorted by ascending y.
        right_envelope: Right-silhouette samples in the same frame/order.
    """

    block: TextBlock
    bounding_box: Tuple[float, float, float, float]
    left_envelope: Tuple[Tuple[float, float], ...]
    right_envelope: Tuple[Tuple[float, float], ...]


def _entry_bounds(entry: PltGlyphEntry, block: TextBlock) -> Tuple[float, float, float, float]:
    """Return the glyph bbox in design inches (stored bbox preferred).

    Args:
        entry: The stored character entry.
        block: The parsed glyph block (design inches).

    Returns:
        ``(min_x, min_y, max_x, max_y)``; zeros for geometry-less glyphs.
    """
    if entry.bounding_box is not None:
        min_x, min_y, max_x, max_y = entry.bounding_box
        return (
            min_x / _UNITS_PER_INCH,
            min_y / _UNITS_PER_INCH,
            max_x / _UNITS_PER_INCH,
            max_y / _UNITS_PER_INCH,
        )
    bounds = block.bounds()
    if bounds is None:
        return (0.0, 0.0, 0.0, 0.0)
    return bounds


@lru_cache(maxsize=None)
def _glyph_entry(
    json_path_key: str,
    font_name: str,
    character: str,
) -> _GlyphGeometry:
    """Parse one stored glyph into cached design-inch geometry.

    Args:
        json_path_key: Cache key for the font library path (``""`` selects
            the default ``Fonts/plt_fonts.json``).
        font_name: Canonical PLT font name.
        character: The single character to render.

    Returns:
        The glyph geometry in the design frame (+y up, baseline y=0, left
        edge x=0; arcs preserved with sweeps verbatim -- the v2 storage is
        already upright).

    Raises:
        PltFontRenderError: If the font or character is not in the library.
    """
    fonts = load_plt_fonts(Path(json_path_key) if json_path_key else None)
    font_data = fonts.get(font_name)
    if font_data is None or character not in font_data.characters:
        raise PltFontRenderError(
            f"Font {font_name!r} has no glyph for character {character!r} "
            f"(U+{ord(character):04X}); remove it or choose another font."
        )
    entry = font_data.characters[character]
    if not entry.glyph.strip():
        block = TextBlock.empty()
    else:
        document = PLTParser().parse_string(entry.glyph)
        block = block_from_parser_paths(document.stroke_paths, scale=1.0 / _UNITS_PER_INCH)
    return _GlyphGeometry(
        block=block,
        bounding_box=_entry_bounds(entry, block),
        left_envelope=tuple(
            (x / _UNITS_PER_INCH, y / _UNITS_PER_INCH) for x, y in entry.left_envelope
        ),
        right_envelope=tuple(
            (x / _UNITS_PER_INCH, y / _UNITS_PER_INCH) for x, y in entry.right_envelope
        ),
    )


def clear_glyph_cache() -> None:
    """Drop the cached glyph parses (used when the font library changes)."""
    _glyph_entry.cache_clear()


def interpolate_envelope(envelope: Sequence[Tuple[float, float]], y: float) -> Optional[float]:
    """Interpolate an envelope's x position at height ``y`` (linear, clamped).

    Args:
        envelope: Sample points ``((x, y), ...)`` sorted by ascending y.
        y: Height at which to evaluate the silhouette.

    Returns:
        The interpolated x, clamped to the first/last sample outside the
        envelope's sampled range; ``None`` when the envelope is empty.
    """
    if not envelope:
        return None
    if y <= envelope[0][1]:
        return envelope[0][0]
    if y >= envelope[-1][1]:
        return envelope[-1][0]
    for (x0, y0), (x1, y1) in zip(envelope, envelope[1:]):
        if y0 <= y <= y1:
            if y1 == y0:
                return x0
            t = (y - y0) / (y1 - y0)
            return x0 + t * (x1 - x0)
    return envelope[-1][0]  # pragma: no cover - unreachable for sorted input


def _kerning_sample_heights(
    left: Sequence[Tuple[float, float]],
    right: Sequence[Tuple[float, float]],
    y_low: float,
    y_high: float,
) -> List[float]:
    """Return the sorted sample heights used to evaluate a kerning pair.

    The union of both envelopes' sample heights inside the overlap range,
    plus the overlap endpoints themselves (so a silhouette extremum that
    falls between samples is still measured).

    Args:
        left: Left glyph's right-envelope samples (design inches).
        right: Right glyph's left-envelope samples (design inches).
        y_low: Lower bound of the overlapping height range.
        y_high: Upper bound of the overlapping height range.

    Returns:
        Sorted list of heights within ``[y_low, y_high]``.
    """
    heights = {y_low, y_high}
    heights.update(y for _x, y in left if y_low <= y <= y_high)
    heights.update(y for _x, y in right if y_low <= y <= y_high)
    return sorted(heights)


def kerning_offset(
    left: _GlyphGeometry,
    right: _GlyphGeometry,
    min_glyph_width_design: float = 0.0,
    window_design: float = 0.0,
) -> float:
    """Design-unit origin-to-origin offset between two adjacent glyphs.

    Windowed profile-envelope kerning. Over the pair's overlapping height
    range, each sample of the left glyph's right silhouette (floored by
    the global minimum glyph width) is compared against the *deepest*
    sample of the right glyph's left silhouette within ``window_design``
    of its height, so staggered pokes -- the glyphs approaching each
    other at slightly different heights -- are detected too. The
    effective offset is the maximum of those windowed penetrations (a
    max of maxes): the window can only widen the advance relative to
    same-height kerning, never narrow it. The clearance is NOT
    included -- the caller adds it in the output frame.

    Args:
        left: The earlier (left) glyph geometry.
        right: The following (right) glyph geometry.
        min_glyph_width_design: Global minimum advance width in design
            units (``0.0`` = pure envelope kerning).
        window_design: Half-width of the comparison window in design
            units (``0.0`` = historical same-height maximum
            penetration).

    Returns:
        Offset in design units (multiply by the render scale, then add
        the inch clearance).
    """
    left_box = left.bounding_box
    right_box = right.bounding_box
    y_low = max(left_box[1], right_box[1])
    y_high = min(left_box[3], right_box[3])
    if y_high >= y_low and left.right_envelope and right.left_envelope:
        ys: List[float] = []
        right_xs: List[float] = []
        left_xs: List[float] = []
        for y in _kerning_sample_heights(left.right_envelope, right.left_envelope, y_low, y_high):
            right_x = interpolate_envelope(left.right_envelope, y)
            left_x = interpolate_envelope(right.left_envelope, y)
            if right_x is None or left_x is None:  # pragma: no cover - guards
                continue
            ys.append(y)
            right_xs.append(max(right_x, min_glyph_width_design))
            left_xs.append(left_x)
        if right_xs:
            # Stage 1: deepest opposing silhouette within +/- window of
            # each sample height (a two-pointer min filter over the
            # ascending ys). With window == 0 this is the same-y sample.
            penetrations: List[float] = []
            lo = 0
            for k, (y_k, right_x) in enumerate(zip(ys, right_xs)):
                while ys[lo] < y_k - window_design:
                    lo += 1
                hi = k
                while hi + 1 < len(ys) and ys[hi + 1] <= y_k + window_design:
                    hi += 1
                deepest = min(left_xs[lo : hi + 1])
                penetrations.append(right_x - deepest)
            # Stage 2: the worst windowed penetration. Each sample's
            # window contains itself, so the max of the per-sample maxes
            # is simply the global maximum of the stage-1 profile.
            return max(penetrations)
    # No overlapping height (or missing envelopes): fall back to the left
    # glyph's bounding-box width, floored by the minimum width.
    return max(left_box[2] - left_box[0], min_glyph_width_design)


def _resolve_plt_font(
    font_name: str,
    json_path: Optional[Path],
    fonts_dir: Optional[Path],
) -> str:
    """Resolve ``font_name`` and require the PLT-extracted family.

    Args:
        font_name: Requested font name (canonical or not).
        json_path: Optional ``plt_fonts.json`` override.
        fonts_dir: Optional Fonts root override (TTF discovery).

    Returns:
        The canonical PLT font name.

    Raises:
        PltFontRenderError: If the name is unknown (registry error
            re-raised as a render error) or resolves to a TrueType font.
    """
    try:
        ref = resolve_font(font_name, json_path=json_path, fonts_dir=fonts_dir)
    except FontNotFoundError as exc:
        raise PltFontRenderError(str(exc)) from exc
    if ref.kind != "plt":
        raise PltFontRenderError(
            f"Font {ref.name!r} is a TrueType font; the PLT renderer only "
            "renders PLT-extracted fonts (the TTF path renders it instead)."
        )
    return ref.name


def render_text_line_plt_font_with_words(
    text: str,
    target_height_inches: float,
    font_name: str,
    cutter_diameter: float = 0.0,
    character_spacing: float = 0.0,
    space_width_fraction: Optional[float] = None,
    min_glyph_width: Optional[float] = None,
    kerning_window_fraction: Optional[float] = None,
    json_path: Optional[Path] = None,
    fonts_dir: Optional[Path] = None,
) -> Tuple[TextBlock, List[Tuple[str, List[int]]]]:
    """Render a text line in a PLT-extracted font, partitioned by word.

    The cursor walk builds each word's strokes contiguously, so the word
    groups are exact by construction (no signature matching, no drift):
    word ``i`` owns a contiguous slice of the returned block's strokes.

    Args:
        text: The string to render.
        target_height_inches: Desired reference-character (cap) height in
            inches; the whole line scales uniformly from it.
        font_name: PLT-extracted font name (case-insensitive).
        cutter_diameter: Resolved text cutter diameter in inches; part of
            the kerning clearance so engraved strokes never bleed together.
        character_spacing: Extra user spacing between characters in inches.
        space_width_fraction: Space advance as a fraction of
            ``target_height_inches`` (``None`` falls back to
            :data:`SPACE_HEIGHT_FRACTION`).
        min_glyph_width: Global minimum glyph advance width in inches
            clamping the profile envelopes (``None`` falls back to
            :data:`MIN_GLYPH_WIDTH`; ``0.0`` = pure envelope kerning).
        kerning_window_fraction: Kerning window as a fraction of the
            rendered text height in ``[0, 1]`` (``None`` falls back to
            :data:`KERNING_WINDOW_FRACTION`; ``0.0`` = historical
            same-height maximum-penetration kerning).
        json_path: Optional ``plt_fonts.json`` override (tests).
        fonts_dir: Optional Fonts root override (tests).

    Returns:
        ``(block, word_groups)`` where ``block`` is the positioned
        :class:`TextBlock` (first glyph's left edge at x=0, every glyph
        anchored on its baseline at y=0 -- descenders reach below it --
        and the reference character exactly ``target_height_inches`` tall
        for non-blank text) and ``word_groups`` lists one ``(word_text,
        stroke_indices)`` pair per whitespace-delimited segment in text
        order (blank segments yield empty index lists). Blank input yields
        an empty block and ``[]``.

    Raises:
        PltFontRenderError: If the font is unknown/not a PLT font, or a
            character has no glyph in the font.
    """
    canonical = _resolve_plt_font(font_name, json_path, fonts_dir)
    if not text:
        return TextBlock.empty(), []

    json_path_key = str(json_path) if json_path is not None else ""
    fonts = load_plt_fonts(Path(json_path) if json_path is not None else None)
    font_data = fonts[canonical]  # resolved above; presence guaranteed
    scale = target_height_inches / font_data.normalized_ref_height
    clearance = cutter_diameter + character_spacing
    space_fraction = SPACE_HEIGHT_FRACTION if space_width_fraction is None else space_width_fraction
    space_advance = space_fraction * target_height_inches + character_spacing
    min_width_inches = MIN_GLYPH_WIDTH if min_glyph_width is None else min_glyph_width
    # The kerning math runs in design units (output = design * scale), so
    # the absolute inch floor converts by dividing through the scale.
    min_width_design = min_width_inches / scale if scale > 0 else 0.0
    window_fraction = (
        KERNING_WINDOW_FRACTION if kerning_window_fraction is None else kerning_window_fraction
    )
    # The window is a fraction of the rendered text height; in the design
    # frame the reference character is exactly ``normalized_ref_height``
    # tall (1.0 for v2 libraries), so the height fraction converts directly.
    window_design = 0.5 * window_fraction * font_data.normalized_ref_height

    words = text.split(" ")

    # Cursor walk in the OUTPUT frame: every glyph anchors its baseline at
    # y = 0 (descenders hang below automatically) and its left edge at the
    # cursor. Adjacent glyphs inside a word advance origin-to-origin by the
    # envelope kerning offset (design units x scale) plus the clearance; a
    # space measures its own advance from the previous glyph's right edge
    # and breaks kerning. The first glyph's left edge lands at x = 0 by
    # construction (the stored left edge is x = 0).
    strokes: List[Stroke] = []
    word_groups: List[Tuple[str, List[int]]] = []
    cursor = 0.0
    previous: Optional[_GlyphGeometry] = None
    previous_origin = 0.0
    for word_index, word in enumerate(words):
        if word_index > 0:
            # Space: measures from the previous glyph's right edge; kerning
            # never crosses a word boundary.
            if previous is not None:
                previous_width = (previous.bounding_box[2] - previous.bounding_box[0]) * scale
                cursor = max(cursor, previous_origin + previous_width)
            cursor += space_advance
            previous = None
        start_index = len(strokes)
        for char in word:
            geometry = _glyph_entry(json_path_key, canonical, char)
            if previous is not None:
                offset = kerning_offset(previous, geometry, min_width_design, window_design)
                cursor = previous_origin + offset * scale + clearance
            placed = geometry.block.scaled(scale).translate(cursor, 0.0)
            strokes.extend(placed.strokes)
            previous_origin = cursor
            previous = geometry
        word_groups.append((word, list(range(start_index, len(strokes)))))

    return TextBlock(strokes=tuple(strokes)), word_groups


def render_text_line_plt_font(
    text: str,
    target_height_inches: float,
    font_name: str,
    cutter_diameter: float = 0.0,
    character_spacing: float = 0.0,
    space_width_fraction: Optional[float] = None,
    min_glyph_width: Optional[float] = None,
    kerning_window_fraction: Optional[float] = None,
    json_path: Optional[Path] = None,
    fonts_dir: Optional[Path] = None,
) -> TextBlock:
    """Render a text line in a PLT-extracted font (arcs preserved).

    Convenience wrapper around
    :func:`render_text_line_plt_font_with_words` discarding word groups.

    Args:
        text: The string to render.
        target_height_inches: Desired reference-character (cap) height in
            inches.
        font_name: PLT-extracted font name (case-insensitive).
        cutter_diameter: Resolved text cutter diameter in inches.
        character_spacing: Extra user spacing between characters in inches.
        space_width_fraction: Space advance as a fraction of
            ``target_height_inches`` (``None`` falls back to
            :data:`SPACE_HEIGHT_FRACTION`).
        min_glyph_width: Global minimum glyph advance width in inches
            clamping the profile envelopes (``None`` falls back to
            :data:`MIN_GLYPH_WIDTH`).
        kerning_window_fraction: Kerning window as a fraction of the
            rendered text height in ``[0, 1]`` (``None`` falls back to
            :data:`KERNING_WINDOW_FRACTION`).
        json_path: Optional ``plt_fonts.json`` override (tests).
        fonts_dir: Optional Fonts root override (tests).

    Returns:
        The positioned :class:`TextBlock` (empty for blank input).

    Raises:
        PltFontRenderError: If the font or a character cannot render.
    """
    block, _groups = render_text_line_plt_font_with_words(
        text,
        target_height_inches,
        font_name,
        cutter_diameter=cutter_diameter,
        character_spacing=character_spacing,
        space_width_fraction=space_width_fraction,
        min_glyph_width=min_glyph_width,
        kerning_window_fraction=kerning_window_fraction,
        json_path=json_path,
        fonts_dir=fonts_dir,
    )
    return block
