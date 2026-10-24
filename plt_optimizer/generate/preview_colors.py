"""Color resolution for the combined ``all_*.pdf`` proof previews.

The per-plate combined PDF preview (see
:func:`plt_optimizer.generate.vectorize.export_per_cutter_plts`) renders an
accurate proof of the engraved label: text ink in its real color over the
material's background color on a configurable grey page
(``job-config.json`` ``preview_background_color``, default
:data:`DEFAULT_PREVIEW_BACKGROUND_COLOR`). This module turns the two
free-form job-spec fields into that palette:

- ``material`` (cascades job -> plate -> label; a plate carries exactly one
  material) yields the **background** color and, when the name encodes it,
  the default **text** color.
- The per-pen stroke-color tag (the ``text_color`` field and inline
  ``<tc:color>`` markup, both of which the pen map already splits into
  separate layers) overrides the material-derived text color.
- The structural layers (label borders, drill holes) take a simple
  automated contrast outline via :func:`outline_color_for`: black over a
  white background, white over everything else.

Material naming conventions (all case-insensitive; anything after the color
part is a property suffix -- adhesive, UV, etc. -- and is ignored, per the
"first part of the material string specifies the color" rule):

1. **Delimiter form**: two color tokens (full names or letters) joined by
   ``-on-``, ``:``, ``|`` or ``/`` (``red-on-white``, ``red:white``,
   ``red|white``, ``red/white``); the first token is the background, the
   second the text color.
2. **Full-name form**: the name starts with a color name (``white``,
   ``black(uv)``, ``red adhesive``) -> that is the background; the text
   color is left for ``text_color`` to supply.
3. **Character form**: the leading run of recognized color letters, where
   the first letter is the top layer (= background the text engraves
   through) and the second (when the run is exactly two letters) is the core
   (= text color): ``bw`` -> black background / white text, ``wb`` -> white
   background / black text. A run of three or more letters is multi-layer
   stock (``wrb`` = white/red/black) where the core is not necessarily the
   engraved text color, so only the background is certain.

Preview color letters are the
:data:`~plt_optimizer.generate.schema.TEXT_COLOR_ABBREVIATIONS` plus ``w``
for white, with one deliberate material-side override: ``b`` means **black**
(the shop's ``bw`` / ``wrb`` convention), not blue, and ``k`` is black too
(CMYK spelling, matching :class:`~plt_optimizer.generate.schema.TextColor`).
Blue is therefore only reachable in a material name via the full-name or
delimiter forms. Recognized colors are the
:class:`~plt_optimizer.generate.schema.TextColor` names plus ``white``, all
of which are valid matplotlib color names.

Known limitation: the character form is a prefix match, so an unrelated
material word made of color letters reads as layers (``gold`` -> green over
orange). The delimiter/full-name forms and an explicit ``text_color`` are
the escape hatch; an unset material always keeps the historical black ink on
white.

This module is pure string handling (import-light, Python 3.8-safe, no
matplotlib) so it can sit on the headless export path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from plt_optimizer.generate.schema import TEXT_COLOR_ABBREVIATIONS

# Canonical preview palette: every TextColor layer name except the implicit
# "none" default, plus "white" (materials are commonly white-top stock, and
# "w" is not a TextColor abbreviation). All values are matplotlib color names.
PREVIEW_COLOR_NAMES: Tuple[str, ...] = tuple(
    name for name in TEXT_COLOR_ABBREVIATIONS if name != "none"
) + ("white",)

# Canonical name -> TextColor-style single-letter tag (the layer abbreviations
# plus w). Used for the *pen* (stroke-color) side, which follows the schema's
# CMYK-style convention (k = black, b = blue).
PREVIEW_COLOR_ABBREVIATIONS: Dict[str, str] = {
    name: TEXT_COLOR_ABBREVIATIONS[name] for name in TEXT_COLOR_ABBREVIATIONS if name != "none"
}
PREVIEW_COLOR_ABBREVIATIONS["white"] = "w"

# Letter -> canonical name, for the stroke-color side (the schema's CMYK-style
# convention: k = black, b = blue).
_COLOR_BY_STROKE_LETTER: Dict[str, str] = {
    letter: name for name, letter in PREVIEW_COLOR_ABBREVIATIONS.items()
}

# Lookup from any accepted *material* spelling (full name or letter) to the
# canonical color name. The shop spells black "b" in material names (bw =
# black over white, wrb = white/red/black), so b maps to black here and k
# stays black as well; blue is intentionally absent from the letter map (see
# the module docstring).
_MATERIAL_COLOR_BY_TOKEN: Dict[str, str] = {name: name for name in PREVIEW_COLOR_NAMES}
_MATERIAL_COLOR_BY_TOKEN.update(_COLOR_BY_STROKE_LETTER)
_MATERIAL_COLOR_BY_TOKEN["b"] = "black"
_MATERIAL_COLOR_BY_TOKEN["k"] = "black"

# Lookup from any accepted *stroke-color* spelling (the schema's TextColor
# names + abbreviations + white) to the canonical color name.
_STROKE_COLOR_BY_TOKEN: Dict[str, str] = {name: name for name in PREVIEW_COLOR_NAMES}
_STROKE_COLOR_BY_TOKEN.update(_COLOR_BY_STROKE_LETTER)

# Longest-first full color names for the material full-name form.
_FULL_NAMES_BY_LENGTH: Tuple[str, ...] = tuple(
    sorted(PREVIEW_COLOR_NAMES, key=lambda name: (-len(name), name))
)

# Backgrounds dark enough that black ink would vanish into them: text whose
# color is not explicitly resolved falls back to white over these.
_DARK_BACKGROUND_COLORS: frozenset[str] = frozenset(
    {"black", "blue", "red", "green", "magenta", "violet"}
)

# Default page/plot background of the combined proof previews (light grey,
# configurable via job-config.json ``preview_background_color``). Drill
# holes punch this color (the "see-through" disc matches the page).
DEFAULT_PREVIEW_BACKGROUND_COLOR = "#D3D3D3"

# Hex color literal: #RGB or #RRGGBB (case-insensitive).
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# "red-on-white" / "red:white" / "red|white" / "red/white": two alphabetic
# color tokens with an explicit delimiter. Anchored at the start; a trailing
# property suffix ((uv), -adhesive, ...) is ignored.
_DELIMITER_RE = re.compile(r"^([a-z]+)(?:-on-|:|\||/)([a-z]+)")

# Longest color-letter run that can describe a material's layers (1-3).
_MAX_MATERIAL_LETTERS = 3


@dataclass(frozen=True)
class MaterialColors:
    """Background (and optional default text) color parsed from a material.

    Attributes:
        background: Canonical color name of the material's top layer (the
            surface the text engraves through), or ``None`` when the
            material name carries no recognized color.
        text: Canonical color name of the material's engraved text color
            (the core layer), or ``None`` when the name does not encode it
            (single-letter, multi-layer, or full-name backgrounds).
    """

    background: Optional[str] = None
    text: Optional[str] = None


def parse_material_colors(material: Optional[str]) -> MaterialColors:
    """Derive the preview background/text colors from a material name.

    Applies the conventions documented in the module docstring (delimiter
    form, then full-name prefix, then the leading color-letter run).
    Comparison is case-insensitive; trailing property suffixes are ignored.

    Args:
        material: The resolved material name (e.g. ``"bw"``, ``"wb(uv)"``,
            ``"red-on-white"``, ``"wrb"``), or ``None`` (material-agnostic).

    Returns:
        The parsed colors; ``(None, None)`` when nothing is recognized, so
        callers keep the historical black-on-white proof.
    """
    if not material:
        return MaterialColors()
    value = material.strip().lower()

    delimiter_match = _DELIMITER_RE.match(value)
    if delimiter_match is not None:
        background = _MATERIAL_COLOR_BY_TOKEN.get(delimiter_match.group(1))
        if background is not None:
            text = _MATERIAL_COLOR_BY_TOKEN.get(delimiter_match.group(2))
            return MaterialColors(background=background, text=text)

    for name in _FULL_NAMES_BY_LENGTH:
        if value.startswith(name):
            return MaterialColors(background=name)

    # Character form: consume the leading run of recognized color letters
    # (stop at the first unrecognized one, cap at the layer count).
    letters: List[str] = []
    for char in value:
        if char not in _MATERIAL_COLOR_BY_TOKEN or len(letters) == _MAX_MATERIAL_LETTERS:
            break
        letters.append(char)
    if not letters:
        return MaterialColors()
    background = _MATERIAL_COLOR_BY_TOKEN[letters[0]]
    # Exactly two letters = top layer + core (background + text); three =
    # multi-layer stock whose core is not necessarily the engraved text color.
    text = _MATERIAL_COLOR_BY_TOKEN[letters[1]] if len(letters) == 2 else None
    return MaterialColors(background=background, text=text)


def resolve_text_color(pen_color: Optional[str], material_colors: MaterialColors) -> str:
    """Resolve the ink color of one text pen for the proof preview.

    Precedence: the pen's explicit stroke-color tag (the ``text_color``
    field or an inline ``<tc:color>`` run, both of which split into their
    own pen) wins; then the material's core color; then black -- except over
    a dark background with no explicit color, where white keeps the proof
    legible.

    Args:
        pen_color: The pen layer's stroke-color value (a
            :class:`~plt_optimizer.generate.schema.TextColor` name, the
            implicit ``"none"``, or ``None`` for structural/unknown pens).
        material_colors: The plate material's parsed colors (see
            :func:`parse_material_colors`).

    Returns:
        A canonical preview color name (matplotlib-compatible).
    """
    if pen_color and pen_color != "none":
        resolved = _STROKE_COLOR_BY_TOKEN.get(pen_color.strip().lower())
        if resolved is not None:
            return resolved
    if material_colors.text is not None:
        return material_colors.text
    if material_colors.background in _DARK_BACKGROUND_COLORS:
        return "white"
    return "black"


def is_white_color(color: str) -> bool:
    """Return whether a preview color is (near-)white.

    Accepts the canonical palette names and ``#RGB``/``#RRGGBB`` hex
    literals (the ``preview_background_color`` form). Only pure white
    counts: the name ``white`` and the hex spellings ``#FFF``/``#FFFFFF``
    (case-insensitive). Light greys -- including the default
    ``#D3D3D3`` page background -- are deliberately NOT white, so
    outlines over them take the non-white branch.

    Args:
        color: A canonical preview color name or a hex color string.

    Returns:
        True when the color is pure white; False for any other value
        (including unrecognized names).
    """
    normalized = color.strip().lower()
    if normalized == "white":
        return True
    return normalized in ("#fff", "#ffffff")


def outline_color_for(background: Optional[str]) -> str:
    """Pick a contrasting outline color for a filled background.

    The simple automated contrast rule of the proof previews: white
    backgrounds get a black outline, everything else (dark materials, the
    grey page background, unknown names) gets a white outline.

    Args:
        background: The background color name/hex to contrast against, or
            ``None`` (unknown background -> black, the historical look).

    Returns:
        ``"black"`` or ``"white"``.
    """
    if background is None:
        return "black"
    return "black" if is_white_color(background) else "white"


def normalize_preview_background_color(value: Optional[str]) -> Optional[str]:
    """Validate/normalize a configured preview background color.

    Args:
        value: A ``#RGB``/``#RRGGBB`` hex color string, or ``None``
            (unset -> the caller applies :data:`DEFAULT_PREVIEW_BACKGROUND_COLOR`).

    Returns:
        The trimmed hex string, or ``None`` when unset.

    Raises:
        ValueError: If the value is not a hex color string.
    """
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("'preview_background_color' cannot be empty or whitespace-only")
    if not _HEX_COLOR_RE.match(trimmed):
        raise ValueError(
            f"'preview_background_color' must be a hex color like '#D3D3D3', got {value!r}"
        )
    return trimmed
