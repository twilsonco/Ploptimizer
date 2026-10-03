"""Arc-native rendering of PLT-extracted fonts (``Fonts/plt_fonts.json``).

PLT-extracted fonts store every glyph as origin-centered HPGL
(``PU``/``PD``/``AA``) normalized to a 1.0-inch design height, in the
**device frame** (``+y`` downward, exactly as engraved). This module lays a
whole text line out by walking a cursor glyph-by-glyph and returns a
:class:`~plt_optimizer.generate.text_geometry.TextBlock`: arcs stay arcs
end-to-end (no flattening), so the finished cut file keeps the native
``AA`` commands of the source font.

Layout contract (matches the ftext TTF path):

- Glyphs are upright in the label-local render frame (``+y`` up), the
  line's union bounds height equals ``target_height_inches`` exactly
  (whole-line uniform normalization, so relative glyph sizes -- cap
  height vs. x-height -- are preserved), the left edge sits at ``x = 0``
  and the bottom edge at ``y = 0``.
- Inter-character advance is cutter-aware::

      advance = glyph_width + cutter_diameter
                + GAP_HEIGHT_FRACTION * height + character_spacing

  i.e. the engraved strokes keep a full cutter width plus
  ``0.125 * height`` of clean air between neighbouring glyphs (half the
  cutter clearance per side), mirroring how EngraveLab spaces single-line
  fonts. The gap is only inserted between two adjacent *glyphs*: a space
  character contributes its own advance instead::

      space_advance = SPACE_HEIGHT_FRACTION * height + character_spacing

- A character the font does not define raises :class:`PltFontRenderError`
  at render time (never a silent skip or tofu box).

The module is matplotlib-free (core parser + text_geometry only).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple

from plt_optimizer.core.parser import PLTParser
from plt_optimizer.generate.font_registry import (
    FontNotFoundError,
    load_plt_fonts,
    resolve_font,
)
from plt_optimizer.generate.text_geometry import Stroke, TextBlock, block_from_parser_paths

logger = logging.getLogger(__name__)

# Inter-glyph gap as a fraction of the rendered text height, added on top
# of the cutter diameter (the user-visible "air" between engraved glyphs).
GAP_HEIGHT_FRACTION: float = 0.125

# Space advance as a fraction of the rendered text height (added to
# ``character_spacing``); a space carries no glyph and no inter-glyph gap.
SPACE_HEIGHT_FRACTION: float = 0.5

# Glyphs are stored in plotter units (1000 units per design inch).
_UNITS_PER_INCH: float = 1000.0


class PltFontRenderError(ValueError):
    """Raised when a PLT-extracted font cannot render a requested string.

    Subclasses :class:`ValueError` so callers guarding the pipeline with
    ``except ValueError`` keep working. The label renderer maps this onto
    a :class:`~plt_optimizer.generate.label_renderer.LabelRenderError`.
    """


@lru_cache(maxsize=None)
def _glyph_block(
    json_path_key: str,
    font_name: str,
    character: str,
) -> TextBlock:
    """Parse one glyph into the +y-up inch render frame (cached).

    Args:
        json_path_key: Cache key for the font library path (``""`` selects
            the default ``Fonts/plt_fonts.json``).
        font_name: Canonical PLT font name.
        character: The single character to render.

    Returns:
        The origin-centered glyph block in the render frame (Y-mirrored
        from the stored device frame; arcs preserved with negated sweeps).
        Empty when the stored glyph string carries no geometry.

    Raises:
        PltFontRenderError: If the font or character is not in the library.
    """
    fonts = load_plt_fonts(Path(json_path_key) if json_path_key else None)
    glyphs = fonts.get(font_name)
    if glyphs is None or character not in glyphs:
        raise PltFontRenderError(
            f"Font {font_name!r} has no glyph for character {character!r} "
            f"(U+{ord(character):04X}); remove it or choose another font."
        )
    hpgl = glyphs[character]
    if not hpgl.strip():
        return TextBlock.empty()
    document = PLTParser().parse_string(hpgl)
    block = block_from_parser_paths(document.stroke_paths, scale=1.0 / _UNITS_PER_INCH)
    # Stored glyphs are device-frame (+y down); the render frame is +y up.
    # Mirroring negates every arc sweep, keeping arc endpoints exact.
    return block.mirrored_y(0.0)


def clear_glyph_cache() -> None:
    """Drop the cached glyph parses (used when the font library changes)."""
    _glyph_block.cache_clear()


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
    json_path: Optional[Path] = None,
    fonts_dir: Optional[Path] = None,
) -> Tuple[TextBlock, List[Tuple[str, List[int]]]]:
    """Render a text line in a PLT-extracted font, partitioned by word.

    The cursor walk builds each word's strokes contiguously, so the word
    groups are exact by construction (no signature matching, no drift):
    word ``i`` owns a contiguous slice of the returned block's strokes.

    Args:
        text: The string to render.
        target_height_inches: Desired whole-line glyph height in inches.
        font_name: PLT-extracted font name (case-insensitive).
        cutter_diameter: Resolved text cutter diameter in inches; part of
            the inter-glyph gap so engraved strokes never bleed together.
        character_spacing: Extra user spacing between characters in inches.
        json_path: Optional ``plt_fonts.json`` override (tests).
        fonts_dir: Optional Fonts root override (tests).

    Returns:
        ``(block, word_groups)`` where ``block`` is the positioned
        :class:`TextBlock` (left edge at x=0, bottom at y=0, height
        exactly ``target_height_inches`` for non-blank text) and
        ``word_groups`` lists one ``(word_text, stroke_indices)`` pair per
        whitespace-delimited segment in text order (blank segments yield
        empty index lists). Blank input yields an empty block and ``[]``.

    Raises:
        PltFontRenderError: If the font is unknown/not a PLT font, or a
            character has no glyph in the font.
    """
    canonical = _resolve_plt_font(font_name, json_path, fonts_dir)
    if not text:
        return TextBlock.empty(), []

    json_path_key = str(json_path) if json_path is not None else ""
    gap = cutter_diameter + GAP_HEIGHT_FRACTION * target_height_inches + character_spacing
    space_advance = SPACE_HEIGHT_FRACTION * target_height_inches + character_spacing

    words = text.split(" ")

    # First pass: parse every glyph and measure the whole-line union so a
    # single uniform scale normalizes the line height exactly (relative
    # glyph sizes preserved, matching the ftext whole-line contract).
    blocks: List[List[Tuple[str, TextBlock]]] = []
    union: Optional[Tuple[float, float, float, float]] = None
    for word in words:
        word_blocks: List[Tuple[str, TextBlock]] = []
        for char in word:
            block = _glyph_block(json_path_key, canonical, char)
            word_blocks.append((char, block))
            bounds = block.bounds()
            if bounds is None:
                continue
            if union is None:
                union = bounds
            else:
                union = (
                    min(union[0], bounds[0]),
                    min(union[1], bounds[1]),
                    max(union[2], bounds[2]),
                    max(union[3], bounds[3]),
                )
        blocks.append(word_blocks)

    if union is None:
        # Nothing renderable (e.g. blank/spaces only): empty geometry, but
        # still report the word structure so callers keep alignment.
        return TextBlock.empty(), [(word, []) for word in words]

    union_height = union[3] - union[1]
    scale = target_height_inches / union_height if union_height > 0 else 1.0
    y_shift = -union[1] * scale  # land the line's bottom edge on y = 0

    # Second pass: cursor walk. A glyph occupies [cursor, cursor + width];
    # the inter-glyph gap is inserted only between two adjacent glyphs, so
    # a space's own advance measures from the previous glyph's right edge.
    strokes: List[Stroke] = []
    word_groups: List[Tuple[str, List[int]]] = []
    cursor = 0.0
    for word_index, word in enumerate(words):
        if word_index > 0:
            cursor += space_advance
        start_index = len(strokes)
        last_glyph_position = len(word) - 1
        for char_position, (_char, block) in enumerate(blocks[word_index]):
            scaled = block.scaled(scale) if scale != 1.0 else block
            bounds = scaled.bounds()
            if bounds is not None:
                placed = scaled.translate(cursor - bounds[0], y_shift)
                strokes.extend(placed.strokes)
                cursor += bounds[2] - bounds[0]
            if char_position < last_glyph_position:
                cursor += gap
        word_groups.append((word, list(range(start_index, len(strokes)))))

    return TextBlock(strokes=tuple(strokes)), word_groups


def render_text_line_plt_font(
    text: str,
    target_height_inches: float,
    font_name: str,
    cutter_diameter: float = 0.0,
    character_spacing: float = 0.0,
    json_path: Optional[Path] = None,
    fonts_dir: Optional[Path] = None,
) -> TextBlock:
    """Render a text line in a PLT-extracted font (arcs preserved).

    Convenience wrapper around
    :func:`render_text_line_plt_font_with_words` discarding word groups.

    Args:
        text: The string to render.
        target_height_inches: Desired whole-line glyph height in inches.
        font_name: PLT-extracted font name (case-insensitive).
        cutter_diameter: Resolved text cutter diameter in inches.
        character_spacing: Extra user spacing between characters in inches.
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
        json_path=json_path,
        fonts_dir=fonts_dir,
    )
    return block
