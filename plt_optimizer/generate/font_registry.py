"""Font discovery and name resolution for the generate pipeline.

Two font families are selectable from a job spec via the cascading ``font``
attribute:

1. **PLT-extracted fonts** -- keys of ``Fonts/plt_fonts.json``, produced by
   ``Fonts/extract_plt_fonts.py`` from engraved EngraveLab / Vision Pro
   sample sheets. Each glyph is an origin-centered HPGL (``PU``/``PD``/``AA``)
   string normalized to a 1.0-inch design height. These render natively with
   arcs preserved (see :mod:`plt_optimizer.generate.plt_font_renderer`).
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
from typing import Dict, List, Literal, Optional

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


@lru_cache(maxsize=8)
def load_plt_fonts(json_path: Optional[Path] = None) -> Dict[str, Dict[str, str]]:
    """Load the PLT-extracted font library.

    Args:
        json_path: Optional override for ``plt_fonts.json`` (used by tests).

    Returns:
        Mapping of font name to ``{character: HPGL command string}``. Empty
        (with a WARNING) when the library file is missing or unreadable --
        a missing library must not break TTF-only jobs.
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
    fonts: Dict[str, Dict[str, str]] = {}
    for name, glyphs in raw.items():
        if not isinstance(glyphs, dict):
            logger.warning("PLT font %r in %s is not a character map; skipping it", name, path)
            continue
        fonts[str(name)] = {str(char): str(cmd) for char, cmd in glyphs.items()}
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
    plt_fonts: Dict[str, Dict[str, str]], ttfs: Dict[str, Path]
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
