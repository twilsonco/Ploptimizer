"""Font discovery and name resolution for the generate pipeline.

Two font families are selectable from a job spec via the cascading ``font``
attribute:

1. **PLT-extracted fonts** -- keys of ``Fonts/plt_fonts.json``, produced by
   ``Fonts/extract_plt_fonts.py`` from engraved EngraveLab / Vision Pro
   sample sheets. Each glyph is a baseline-normalized HPGL
   (``PU``/``PD``/``AA``) string in plotter units (1000 units = 1 inch,
   ``+y`` up, baseline at ``y = 0``, left edge at ``x = 0``) with the
   reference character exactly 1000 units tall, accompanied by its bounding
   box and left/right profile envelopes used for kerning (see
   :class:`PltGlyphEntry`). These render natively with arcs preserved (see
   :mod:`plt_optimizer.generate.plt_font_renderer`).
2. **TrueType fonts** -- every ``*.ttf`` under ``Fonts/`` (recursively),
   selected by file-name basename without extension (e.g.
   ``"ReliefSingleLineCAD-Regular"``). These render through
   :mod:`plt_optimizer.generate.ftext_renderer`.

This module is deliberately **pure Python** (no matplotlib, no numpy, no
vpype): :mod:`plt_optimizer.generate.schema` imports it to validate and
document the valid font names, and ``schema`` must stay importable on the
Python 3.8 / Windows 7 CLI path where matplotlib is not installed.

Matching is case-insensitive and resolves to the canonical stored name, so
``"dino"`` and ``"DINO"`` both yield ``"Dino"``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)

# Repository Fonts/ directory, resolved relative to this module so the
# lookup works regardless of the current working directory at runtime.
FONTS_DIR: Path = Path(__file__).resolve().parent.parent.parent / "Fonts"

# The PLT-extracted font library produced by Fonts/extract_plt_fonts.py.
PLT_FONTS_JSON_PATH: Path = FONTS_DIR / "plt_fonts.json"

# Font selected when ``font`` is unset in the YAML spec, the job config, and
# every label/line. Matches the historical hard-coded ftext default.
DEFAULT_FONT_NAME: str = "ReliefSingleLineCAD-Regular"

FontKind = Literal["ttf", "plt"]


class FontNotFoundError(ValueError):
    """Raised when a requested font name matches no known font.

    The message lists every valid name so a YAML validation error is
    self-explanatory.
    """

    def __init__(self, requested: str, choices: List[str]) -> None:
        """Build the error for ``requested`` against the available ``choices``.

        Args:
            requested: The unrecognized font name.
            choices: Every valid font name (canonical form).
        """
        self.requested = requested
        self.choices = list(choices)
        listing = ", ".join(sorted(choices)) if choices else "(none found)"
        super().__init__(
            f"Unknown font {requested!r}. Valid fonts are: {listing}. PLT fonts "
            f"come from {PLT_FONTS_JSON_PATH.name}; TrueType fonts are the "
            f"*.ttf basenames under {FONTS_DIR.name}/."
        )


@dataclass(frozen=True)
class FontRef:
    """A resolved font reference.

    Attributes:
        kind: ``"ttf"`` for a TrueType file, ``"plt"`` for a PLT-extracted
            glyph library.
        name: The canonical font name (``plt_fonts.json`` key, or the TTF
            file's basename without extension).
        path: Absolute path to the ``.ttf`` file; ``None`` for PLT fonts
            (whose geometry lives in the JSON library).
    """

    kind: FontKind
    name: str
    path: Optional[Path] = None


@dataclass(frozen=True)
class PltGlyphEntry:
    """One character's stored geometry from ``plt_fonts.json`` (v2 schema).

    All numeric values are plotter units (1000 units = 1 inch) in the
    baseline-normalized storage frame: ``+y`` up, baseline at ``y = 0``,
    glyph left edge at ``x = 0``, and the font's reference character
    exactly ``normalized_ref_height * 1000`` units tall.

    Attributes:
        glyph: Self-contained ``PU``/``PD``/``AA`` HPGL command string.
        bounding_box: ``(min_x, min_y, max_x, max_y)`` in plotter units;
            ``None`` when the library entry carries no bounding box (the
            renderer then measures the parsed glyph instead).
        left_envelope: Left-silhouette samples ``((x, y), ...)`` sorted by
            ascending ``y``, sampled across the bounding box height.
        right_envelope: Right-silhouette samples in the same frame/order.
    """

    glyph: str
    bounding_box: Optional[Tuple[float, float, float, float]] = None
    left_envelope: Tuple[Tuple[float, float], ...] = ()
    right_envelope: Tuple[Tuple[float, float], ...] = ()


@dataclass(frozen=True)
class PltFontData:
    """One PLT-extracted font: metadata plus its per-character geometry.

    Attributes:
        characters: Mapping of character to :class:`PltGlyphEntry`. The
            space character is intentionally absent (nothing to cut); the
            typesetter handles its advance.
        reference_char: Framing character the sheet was verified with.
        declared_height_in: Text height from the sample-sheet file name
            (metadata only; never applied as a correction).
        reference_char_height_in: Measured reference-character height in
            inches at the original sheet scale (metadata only).
        normalized_ref_height: Height of the reference character in stored
            design units (1.0 for every v2 library: the extractor scales
            the reference char to exactly 1000 plotter units). The renderer
            derives its scale from this value.
    """

    characters: Dict[str, PltGlyphEntry]
    reference_char: str = ""
    declared_height_in: Optional[float] = None
    reference_char_height_in: Optional[float] = None
    normalized_ref_height: float = 1.0


def _parse_bounding_box(raw: object) -> Optional[Tuple[float, float, float, float]]:
    """Parse a ``bounding_box`` mapping into ``(min_x, min_y, max_x, max_y)``.

    Args:
        raw: The raw JSON value for the entry's ``bounding_box`` key.

    Returns:
        The bbox tuple, or ``None`` when absent or malformed.
    """
    if not isinstance(raw, dict):
        return None
    try:
        return (
            float(raw["min_x"]),
            float(raw["min_y"]),
            float(raw["max_x"]),
            float(raw["max_y"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _parse_envelope(raw: object) -> Tuple[Tuple[float, float], ...]:
    """Parse an envelope list of ``[x, y]`` pairs into a tuple of tuples.

    Args:
        raw: The raw JSON value for an envelope key.

    Returns:
        The sample points; empty when absent or malformed.
    """
    if not isinstance(raw, list):
        return ()
    points: List[Tuple[float, float]] = []
    for point in raw:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            continue
        try:
            points.append((float(point[0]), float(point[1])))
        except (TypeError, ValueError):
            continue
    return tuple(points)


def _parse_glyph_entry(raw: object) -> Optional[PltGlyphEntry]:
    """Parse one v2 character entry; ``None`` when malformed.

    Args:
        raw: The raw JSON value for one character of a font's
            ``characters`` mapping.

    Returns:
        The parsed :class:`PltGlyphEntry`, or ``None`` when the entry is
        not a v2 object (missing/invalid ``glyph`` string).
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("glyph"), str):
        return None
    return PltGlyphEntry(
        glyph=raw["glyph"],
        bounding_box=_parse_bounding_box(raw.get("bounding_box")),
        left_envelope=_parse_envelope(raw.get("left_envelope")),
        right_envelope=_parse_envelope(raw.get("right_envelope")),
    )


def _parse_font_data(name: str, raw: object, path: Path) -> Optional[PltFontData]:
    """Parse one v2 font object; ``None`` when malformed or legacy-flat.

    Args:
        name: The font key (for logging).
        raw: The raw JSON value for the font.
        path: Library path (for logging).

    Returns:
        The parsed :class:`PltFontData`, or ``None`` when the entry is not
        a v2 font object (including the legacy flat ``char -> HPGL``
        schema, which is rejected with a WARNING).
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("characters"), dict):
        logger.warning(
            "PLT font %r in %s is not a v2 font object (expected a 'characters' "
            "mapping; legacy flat libraries require re-running the extractor "
            "with --rebuild); skipping it",
            name,
            path,
        )
        return None
    characters: Dict[str, PltGlyphEntry] = {}
    for char, entry_raw in raw["characters"].items():
        entry = _parse_glyph_entry(entry_raw)
        if entry is None:
            logger.warning(
                "PLT font %r character %r in %s is not a v2 glyph entry; skipping it",
                name,
                char,
                path,
            )
            continue
        characters[str(char)] = entry
    normalized = raw.get("normalized_ref_height")
    try:
        normalized_ref_height = float(normalized) if normalized is not None else 1.0
    except (TypeError, ValueError):
        normalized_ref_height = 1.0
    if normalized_ref_height <= 0.0:
        logger.warning(
            "PLT font %r in %s has non-positive normalized_ref_height %r; using 1.0",
            name,
            path,
            normalized,
        )
        normalized_ref_height = 1.0

    def _opt_float(key: str) -> Optional[float]:
        value = raw.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    reference_char = raw.get("reference_char")
    return PltFontData(
        characters=characters,
        reference_char=reference_char if isinstance(reference_char, str) else "",
        declared_height_in=_opt_float("declared_height_in"),
        reference_char_height_in=_opt_float("reference_char_height_in"),
        normalized_ref_height=normalized_ref_height,
    )


@lru_cache(maxsize=8)
def load_plt_fonts(json_path: Optional[Path] = None) -> Dict[str, PltFontData]:
    """Load the PLT-extracted font library (v2 nested schema).

    Args:
        json_path: Optional override for ``plt_fonts.json`` (used by tests).

    Returns:
        Mapping of font name to :class:`PltFontData`. Empty (with a
        WARNING) when the library file is missing or unreadable -- a
        missing library must not break TTF-only jobs. Fonts stored in the
        rejected legacy flat schema are skipped with a WARNING.
    """
    path = json_path if json_path is not None else PLT_FONTS_JSON_PATH
    if not path.exists():
        logger.warning("PLT font library not found at %s; only TTF fonts available", path)
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("PLT font library %s could not be read (%s); ignoring it", path, exc)
        return {}
    if not isinstance(raw, dict):
        logger.warning("PLT font library %s is not a JSON object; ignoring it", path)
        return {}
    fonts: Dict[str, PltFontData] = {}
    for name, font_raw in raw.items():
        data = _parse_font_data(str(name), font_raw, path)
        if data is None:
            continue
        fonts[str(name)] = data
    return fonts


@lru_cache(maxsize=8)
def available_ttf_fonts(fonts_dir: Optional[Path] = None) -> Dict[str, Path]:
    """Discover every TrueType font under the Fonts directory.

    Args:
        fonts_dir: Optional override for the Fonts root (used by tests).

    Returns:
        Mapping of file basename (extension stripped) to the font file path,
        sorted by name. On a basename collision the alphabetically-first path
        wins, keeping discovery deterministic across platforms.
    """
    root = fonts_dir if fonts_dir is not None else FONTS_DIR
    found: Dict[str, Path] = {}
    if not root.is_dir():
        logger.warning("Fonts directory %s does not exist; no TTF fonts found", root)
        return {}
    for path in sorted(root.rglob("*.ttf"), key=lambda p: str(p)):
        found.setdefault(path.stem, path)
    return found


def _canonical_map(
    plt_fonts: Dict[str, PltFontData], ttfs: Dict[str, Path]
) -> Dict[str, tuple[FontKind, str]]:
    """Build the case-insensitive lowercase-name to (kind, canonical) index.

    TrueType entries are inserted last so a TTF basename always wins a
    cross-family collision (the historical default family is TTF).
    """
    index: Dict[str, tuple[FontKind, str]] = {}
    for name in plt_fonts:
        index[name.lower()] = ("plt", name)
    for name in ttfs:
        index[name.lower()] = ("ttf", name)
    return index


def font_name_choices(
    json_path: Optional[Path] = None, fonts_dir: Optional[Path] = None
) -> List[str]:
    """Return every valid ``font`` value, sorted case-insensitively.

    Args:
        json_path: Optional ``plt_fonts.json`` override.
        fonts_dir: Optional Fonts root override.

    Returns:
        Canonical font names (PLT keys + TTF basenames), deduplicated and
        sorted.
    """
    index = _canonical_map(load_plt_fonts(json_path), available_ttf_fonts(fonts_dir))
    return sorted({canonical for _kind, canonical in index.values()}, key=str.lower)


def resolve_font(
    name: str,
    json_path: Optional[Path] = None,
    fonts_dir: Optional[Path] = None,
) -> FontRef:
    """Resolve a user-supplied font name to a :class:`FontRef`.

    Matching is case-insensitive; the returned :attr:`FontRef.name` is always
    the canonical stored name.

    Args:
        name: Requested font name (e.g. ``"dino"`` or ``"ReliefSingleLineCAD-Regular"``).
        json_path: Optional ``plt_fonts.json`` override.
        fonts_dir: Optional Fonts root override.

    Returns:
        The resolved reference.

    Raises:
        FontNotFoundError: If no font matches ``name``.
    """
    if not name or not name.strip():
        raise FontNotFoundError(name, font_name_choices(json_path, fonts_dir))
    index = _canonical_map(load_plt_fonts(json_path), available_ttf_fonts(fonts_dir))
    entry = index.get(name.strip().lower())
    if entry is None:
        raise FontNotFoundError(name, font_name_choices(json_path, fonts_dir))
    kind, canonical = entry
    if kind == "ttf":
        return FontRef(kind="ttf", name=canonical, path=available_ttf_fonts(fonts_dir)[canonical])
    return FontRef(kind="plt", name=canonical, path=None)


def normalize_font_name(
    name: str,
    json_path: Optional[Path] = None,
    fonts_dir: Optional[Path] = None,
) -> str:
    """Return the canonical form of ``name`` (case-insensitive lookup).

    Args:
        name: Requested font name.
        json_path: Optional ``plt_fonts.json`` override.
        fonts_dir: Optional Fonts root override.

    Returns:
        The canonical font name.

    Raises:
        FontNotFoundError: If no font matches ``name``.
    """
    return resolve_font(name, json_path, fonts_dir).name
