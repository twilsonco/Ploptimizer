"""Font discovery and name resolution for the generate pipeline.

Two font families are selectable from a job spec via the cascading ``font``
attribute:

1. **PLT-extracted fonts** -- one JSON file per font under
   ``fonts/VP-EL-fonts/catalog/``, produced by
   ``fonts/extract_plt_fonts.py`` from engraved EngraveLab / Vision Pro
   sample sheets. Each file's ``font_name`` field is the font key. Each glyph
   is a baseline-normalized HPGL
   (``PU``/``PD``/``AA``) string in the library **design frame** (1000 units
   per design inch -- see ``_DESIGN_UNITS_PER_INCH`` in
   :mod:`plt_optimizer.generate.plt_font_renderer`; this is NOT the emitted
   device scale, which is 1016 units/inch per
   :mod:`plt_optimizer.core.units`), ``+y`` up, baseline at ``y = 0``,
   left edge at ``x = 0``) with the
   reference character exactly 1000 units tall, accompanied by its bounding
   box and left/right profile envelopes used for kerning (see
   :class:`PltGlyphEntry`). These render natively with arcs preserved (see
   :mod:`plt_optimizer.generate.plt_font_renderer`).
2. **TrueType fonts** -- every ``*.ttf`` under ``fonts/`` (recursively),
   selected by file-name basename without extension (e.g.
   ``"ReliefSingleLineCAD-Regular"``). These render through
   :mod:`plt_optimizer.generate.ftext_renderer`.

This module is deliberately **pure Python** (no matplotlib, no numpy, no
vpype): :mod:`plt_optimizer.generate.schema` imports it to validate and
document the valid font names, and ``schema`` must stay importable on the
Python 3.8 / Windows 7 CLI path where matplotlib is not installed.

Every base font also gets a synthetic **small-caps variant** named
``"<base name> SC"`` (case-insensitive, rendered on the fly from the base --
never stored in the JSON library): the uppercase glyphs engrave verbatim at
full size while ASCII lowercase ``a-z`` is replaced by its uppercase glyph
uniformly downscaled by the cascading ``sc_scale`` (see
:mod:`plt_optimizer.generate.plt_font_renderer` / :mod:`plt_optimizer.generate.ftext_renderer`
for the per-family transforms). The variant shares the base's kind (PLT
variants stay arc-native, TTF variants render through ftext).

Fonts that engrave **symbols** rather than letters (e.g. Wingdings) are
listed in ``fonts/VP-EL-fonts/non_text_fonts.txt`` (see
:data:`NON_TEXT_FONTS_FILE` / :func:`non_text_font_names`). A listed base
font is exempt from the synthetic small-caps variant (no ``"<base> SC"``
key), from derived-Unicode glyph creation in ``fonts/extract_plt_fonts.py``,
and from the Unicode line in ``scripts/font_showcase.py``; the base font
itself stays fully selectable.

Matching is case-insensitive and resolves to the canonical stored name, so
``"dino"`` and ``"DINO"`` both yield ``"Dino"``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)

# Repository fonts/ directory, resolved relative to this module so the
# lookup works regardless of the current working directory at runtime.
FONTS_DIR: Path = Path(__file__).resolve().parent.parent.parent / "fonts"

# The PLT-extracted font catalog produced by fonts/extract_plt_fonts.py:
# one JSON file per font, keyed by the file's ``font_name`` field.
PLT_FONT_CATALOG_DIR: Path = FONTS_DIR / "VP-EL-fonts" / "catalog"

# Font selected when ``font`` is unset in the YAML spec, the job config, and
# every label/line. Matches the historical hard-coded ftext default.
DEFAULT_FONT_NAME: str = "ReliefSingleLineCAD-Regular"

FontKind = Literal["ttf", "plt"]

# Suffix appended (space-separated) to a base font name to form its
# synthetic small-caps variant (``"Dino SC"``). Matched case-insensitively;
# stripped case-insensitively by :func:`base_font_name`.
SMALL_CAPS_SUFFIX: str = "SC"

# Canonical separator between a base font name and :data:`SMALL_CAPS_SUFFIX`.
_SMALL_CAPS_SEPARATOR: str = " "

# "Non-text" (symbol) fonts -- one font name per line, matched
# case-insensitively; blank lines and ``#`` comments are ignored. A listed
# base font is exempt from the synthetic ``"<base> SC"`` variant (see
# :func:`_canonical_map`), from derived-Unicode glyph creation in
# ``fonts/extract_plt_fonts.py``, and from the Unicode line in
# ``scripts/font_showcase.py``. A missing file means no exemptions.
NON_TEXT_FONTS_FILE: Path = FONTS_DIR / "VP-EL-fonts" / "non_text_fonts.txt"


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
            f"come from the {PLT_FONT_CATALOG_DIR} catalog; TrueType fonts are the "
            f"*.ttf basenames under {FONTS_DIR.name}/."
        )


@dataclass(frozen=True)
class FontRef:
    """A resolved font reference.

    Attributes:
        kind: ``"ttf"`` for a TrueType file, ``"plt"`` for a PLT-extracted
            glyph library.
        name: The canonical font name (the catalog's ``font_name`` key, the
            TTF file's basename without extension, or the synthetic
            small-caps variant ``"<base name> SC"``).
        path: Absolute path to the ``.ttf`` file; ``None`` for PLT fonts
            (whose geometry lives in the JSON library). A small-caps
            variant carries its **base** font's path -- the variant's
            geometry is the base's, transformed at render time.
        small_caps: True when the name is a synthetic small-caps variant:
            uppercase glyphs render verbatim and ASCII lowercase ``a-z``
            renders as its uppercase glyph downscaled by ``sc_scale``.
            Geometry and library lookups always use :func:`base_font_name`.
        italic_path: Absolute path to this font's **native italic face**
            discovered by the sibling-file probe (:func:`find_italic_face_path`),
            or ``None`` when the family ships no italic. The ``<i>`` markup
            renders italic runs from this file when present; without it the
            renderer falls back to the synthetic shear transform. ``None``
            for PLT fonts (whose italics are always the shear).
    """

    kind: FontKind
    name: str
    path: Optional[Path] = None
    small_caps: bool = False
    italic_path: Optional[Path] = None


@dataclass(frozen=True)
class PltGlyphEntry:
    """One character's stored geometry from the font catalog (v2 schema).

    All numeric values are library design-frame units (1000 units per design
    inch -- not the emitted device scale) in the
    baseline-normalized storage frame: ``+y`` up, baseline at ``y = 0``,
    glyph left edge at ``x = 0``, and the font's reference character
    exactly ``normalized_ref_height * 1000`` units tall.

    Attributes:
        glyph: Self-contained ``PU``/``PD``/``AA`` HPGL command string.
        bounding_box: ``(min_x, min_y, max_x, max_y)`` in design units;
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
            the reference char to exactly 1000 design units). The renderer
            derives its scale from this value.
        stroke_count: Number of parallel cutter passes each glyph carries
            (from the sample sheet's ``_S<n>`` file-name suffix). 1 is a
            single-stroke font; N > 1 is a multi-stroke (bold) font whose
            stored glyphs already contain all N engraved passes, so the
            renderer emits them verbatim and the count drives automatic
            cutter sizing only.
    """

    characters: Dict[str, PltGlyphEntry]
    reference_char: str = ""
    declared_height_in: Optional[float] = None
    reference_char_height_in: Optional[float] = None
    normalized_ref_height: float = 1.0
    stroke_count: int = 1


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
        stroke_count=_parse_stroke_count(name, path, raw.get("stroke_count")),
    )


def _parse_stroke_count(name: str, path: Path, raw: Any) -> int:
    """Parse a font's ``stroke_count`` metadata leniently.

    Args:
        name: The font key (for logging).
        path: Library path (for logging).
        raw: The raw JSON value for the ``stroke_count`` key.

    Returns:
        The stroke count; 1 (single-stroke) when absent, malformed,
        non-integral, or below one - libraries written before the
        multi-stroke convention simply carry no key.
    """
    if raw is None:
        return 1
    if isinstance(raw, bool):
        logger.warning("PLT font %r in %s has boolean stroke_count %r; using 1", name, path, raw)
        return 1
    try:
        count: int = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "PLT font %r in %s has non-integer stroke_count %r; using 1", name, path, raw
        )
        return 1
    if count < 1:
        logger.warning("PLT font %r in %s has stroke_count %r below 1; using 1", name, path, raw)
        return 1
    return count


@lru_cache(maxsize=8)
def load_plt_fonts(library_dir: Optional[Path] = None) -> Dict[str, PltFontData]:
    """Load the PLT-extracted font catalog (v2 nested schema, one file per font).

    Every ``*.json`` file in the catalog directory is one font, keyed by its
    ``font_name`` field; files are read in sorted order, so a duplicate key
    resolves to the alphabetically-first file deterministically.

    Args:
        library_dir: Optional override for the catalog directory (used by
            tests).

    Returns:
        Mapping of font name to :class:`PltFontData`. Empty (with a
        WARNING) when the catalog directory is missing -- a missing
        catalog must not break TTF-only jobs. An unreadable, non-object,
        nameless, malformed, or legacy-flat font file is skipped with a
        WARNING; the remaining fonts still load.
    """
    path = library_dir if library_dir is not None else PLT_FONT_CATALOG_DIR
    if not path.is_dir():
        logger.warning("PLT font catalog not found at %s; only TTF fonts available", path)
        return {}
    fonts: Dict[str, PltFontData] = {}
    for file_path in sorted(path.glob("*.json")):
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("PLT font file %s could not be read (%s); skipping it", file_path, exc)
            continue
        if not isinstance(raw, dict):
            logger.warning("PLT font file %s is not a JSON object; skipping it", file_path)
            continue
        name = raw.get("font_name")
        if not isinstance(name, str) or not name.strip():
            logger.warning("PLT font file %s has no usable font_name; skipping it", file_path)
            continue
        name = name.strip()
        if name in fonts:
            logger.warning(
                "PLT font %r is declared by %s as well as an earlier catalog "
                "file; keeping the earlier one",
                name,
                file_path,
            )
            continue
        data = _parse_font_data(name, raw, file_path)
        if data is None:
            continue
        fonts[name] = data
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


def _small_caps_canonical(name: str) -> str:
    """Return the canonical small-caps variant name of a base font name."""
    return f"{name}{_SMALL_CAPS_SEPARATOR}{SMALL_CAPS_SUFFIX}"


@lru_cache(maxsize=8)
def non_text_font_names(non_text_file: Optional[Path] = None) -> frozenset[str]:
    """Read the "non-text" (symbol) font exemption list.

    Symbol fonts engrave their ASCII slots with symbols, so the derived-
    Unicode recipes, the showcase's Unicode line, and the small-caps
    transform make no sense for them. The list lives in
    :data:`NON_TEXT_FONTS_FILE`: one font name per line, matched
    case-insensitively; blank lines and ``#`` comments (full-line or
    trailing) are ignored.

    Args:
        non_text_file: Optional override for the list file (used by tests).

    Returns:
        Lowercased font names. Empty when the file is missing (no
        exemptions); an unreadable file logs a WARNING and yields no
        exemptions.
    """
    path = non_text_file if non_text_file is not None else NON_TEXT_FONTS_FILE
    if not path.is_file():
        return frozenset()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Non-text font list %s could not be read (%s); ignoring it", path, exc)
        return frozenset()
    names = {line.split("#", 1)[0].strip().lower() for line in raw.splitlines()}
    names.discard("")
    return frozenset(names)


# Filename stems probed (case-insensitively) next to a TTF for its native
# italic face. ``<stem>-Italic``/``<stem>Italic`` are the family naming
# convention (``Foo-Italic.ttf`` next to ``Foo.ttf``); ``Oblique`` is the
# name italic families give the same slanted cut.
_ITALIC_FACE_SUFFIXES: tuple[str, ...] = ("-Italic", "Italic", "-Oblique", "Oblique")


@lru_cache(maxsize=None)
def find_italic_face_path(font_path: Optional[Path]) -> Optional[Path]:
    """Find the native italic face shipped next to a TrueType font file.

    The probe is the family naming convention: a sibling file of ``font_path``
    whose stem is the base stem plus an italic suffix (``Foo-Italic.ttf``,
    ``FooItalic.ttf``, ``Foo-Oblique.ttf``, ...), matched case-insensitively
    in the font's own directory. No schema surface: dropping an italic file
    next to the regular face is all a shop does to enable native italics;
    without one, ``<i>`` falls back to the synthetic shear transform.

    Args:
        font_path: The resolved ``.ttf`` path, or ``None`` (PLT fonts).

    Returns:
        The italic face's path, or ``None`` when the family ships none.
    """
    if font_path is None:
        return None
    directory = font_path.parent
    if not directory.is_dir():  # pragma: no cover - resolved paths exist
        return None
    wanted = {f"{font_path.stem}{suffix}".lower() for suffix in _ITALIC_FACE_SUFFIXES}
    for sibling in sorted(directory.iterdir()):
        if (
            sibling.suffix.lower() == ".ttf"
            and sibling.stem.lower() in wanted
            and sibling != font_path
        ):
            return sibling
    return None


def base_font_name(name: str) -> str:
    """Return the base font name of ``name``, stripping a trailing `` SC``.

    Glyph-library lookups (catalog ``font_name`` keys, TTF discovery) are
    keyed by the base name only -- the small-caps variant is a render-time
    transform, never a stored font -- so every consumer that indexes the
    library with a resolved :attr:`FontRef.name` must pass it through this
    helper first. Non-variant names are returned verbatim.

    Args:
        name: A canonical font name (``"Dino"``, ``"Dino SC"``,
            ``"Dino SC SC"``, ...).

    Returns:
        The base name: a single trailing `` SC`` separator + suffix
        (case-insensitive) is removed (``"Dino SC"`` -> ``"Dino"``);
        non-variant names are returned verbatim. Synthesis is one-tier
        (``_canonical_map`` appends the suffix to base names only), so a
        resolved variant name always strips to a stored library key.
    """
    lowered = name.rstrip().lower()
    suffix = f"{_SMALL_CAPS_SEPARATOR}{SMALL_CAPS_SUFFIX.lower()}"
    if lowered.endswith(suffix):
        return name.rstrip()[: -len(suffix)]
    return name


def _canonical_map(
    plt_fonts: Dict[str, PltFontData],
    ttfs: Dict[str, Path],
    non_text: frozenset[str] = frozenset(),
) -> Dict[str, tuple[FontKind, str, str, bool]]:
    """Build the case-insensitive index of base and small-caps font names.

    Maps each lowercase name to ``(kind, canonical, base, small_caps)``.
    TrueType entries are inserted last so a TTF basename always wins a
    cross-family collision (the historical default family is TTF). Every
    base font contributes a synthetic ``"<base> SC"`` variant of the same
    kind; variants are inserted with ``setdefault`` after all bases, so a
    real font literally named ``"Foo SC"`` always wins its name. Bases
    listed in ``non_text" (lowercased, see
    :func:`non_text_font_names`) contribute **no** small-caps variant --
    symbol fonts have no lowercase to shrink.

    Args:
        plt_fonts: Loaded PLT catalog (name -> data).
        ttfs: Discovered TrueType fonts (basename -> path).
        non_text: Lowercased names of exempt symbol fonts; their names are
            logged at DEBUG when they match no base (a typo costs nothing
            but deserves a trace).
    """
    index: Dict[str, tuple[FontKind, str, str, bool]] = {}
    for name in plt_fonts:
        index[name.lower()] = ("plt", name, name, False)
    for name in ttfs:
        index[name.lower()] = ("ttf", name, name, False)
    families: tuple[tuple[FontKind, Iterable[str]], ...] = (("plt", plt_fonts), ("ttf", ttfs))
    for kind, names in families:
        for name in names:
            if name.lower() in non_text:
                continue
            index.setdefault(
                f"{name.lower()}{_SMALL_CAPS_SEPARATOR}{SMALL_CAPS_SUFFIX.lower()}",
                (kind, _small_caps_canonical(name), name, True),
            )
    known = {name.lower() for name in plt_fonts} | {name.lower() for name in ttfs}
    for listed in sorted(non_text - known):
        logger.debug(
            "Non-text font list entry %r matches no catalog font; it has no effect",
            listed,
        )
    return index


def font_name_choices(
    library_dir: Optional[Path] = None,
    fonts_dir: Optional[Path] = None,
    non_text_file: Optional[Path] = None,
) -> List[str]:
    """Return every valid ``font`` value, sorted case-insensitively.

    Args:
        library_dir: Optional PLT font catalog directory override.
        fonts_dir: Optional Fonts root override.
        non_text_file: Optional non-text font list override (fonts listed
            there get no ``"<base> SC"`` variant).

    Returns:
        Canonical font names (catalog ``font_name`` keys + TTF basenames,
        plus the synthetic ``"<base> SC"`` small-caps variants of every
        non-exempt base), deduplicated and sorted.
    """
    index = _canonical_map(
        load_plt_fonts(library_dir),
        available_ttf_fonts(fonts_dir),
        non_text_font_names(non_text_file),
    )
    return sorted({canonical for _kind, canonical, _base, _sc in index.values()}, key=str.lower)


def resolve_font(
    name: str,
    library_dir: Optional[Path] = None,
    fonts_dir: Optional[Path] = None,
    non_text_file: Optional[Path] = None,
) -> FontRef:
    """Resolve a user-supplied font name to a :class:`FontRef`.

    Matching is case-insensitive; the returned :attr:`FontRef.name` is always
    the canonical stored name. A ``"<base> SC"`` request resolves to the
    base font's kind/path with :attr:`FontRef.small_caps` set -- the
    variant is a render-time transform of the base, never a stored font.
    A ``"<base> SC"`` request for a base on the non-text font list is
    unknown (no variant is synthesized for symbol fonts).

    Args:
        name: Requested font name (e.g. ``"dino"``, ``"Dino SC"`` or
            ``"ReliefSingleLineCAD-Regular"``).
        library_dir: Optional PLT font catalog directory override.
        fonts_dir: Optional Fonts root override.
        non_text_file: Optional non-text font list override (fonts listed
            there get no ``"<base> SC"`` variant).

    Returns:
        The resolved reference.

    Raises:
        FontNotFoundError: If no font matches ``name``.
    """
    if not name or not name.strip():
        raise FontNotFoundError(name, font_name_choices(library_dir, fonts_dir, non_text_file))
    index = _canonical_map(
        load_plt_fonts(library_dir),
        available_ttf_fonts(fonts_dir),
        non_text_font_names(non_text_file),
    )
    entry = index.get(name.strip().lower())
    if entry is None:
        raise FontNotFoundError(name, font_name_choices(library_dir, fonts_dir, non_text_file))
    kind, canonical, base, small_caps = entry
    path = available_ttf_fonts(fonts_dir)[base] if kind == "ttf" else None
    return FontRef(
        kind=kind,
        name=canonical,
        path=path,
        small_caps=small_caps,
        italic_path=find_italic_face_path(path),
    )


def normalize_font_name(
    name: str,
    library_dir: Optional[Path] = None,
    fonts_dir: Optional[Path] = None,
    non_text_file: Optional[Path] = None,
) -> str:
    """Return the canonical form of ``name`` (case-insensitive lookup).

    Args:
        name: Requested font name.
        library_dir: Optional PLT font catalog directory override.
        fonts_dir: Optional Fonts root override.
        non_text_file: Optional non-text font list override (fonts listed
            there get no ``"<base> SC"`` variant).

    Returns:
        The canonical font name.

    Raises:
        FontNotFoundError: If no font matches ``name``.
    """
    return resolve_font(name, library_dir, fonts_dir, non_text_file).name


def plt_font_stroke_count(name: str, library_dir: Optional[Path] = None) -> int:
    """Return the multi-stroke pass count of a PLT-extracted font.

    Resolution-time cutter sizing uses this to recognise multi-stroke
    (bold) fonts: their stored glyphs already carry all N engraved passes,
    so the count drives the cutter-width formula only. Lookup is
    case-insensitive and strips a trailing `` SC`` small-caps suffix (the
    variant renders from the base library). Anything that is not a PLT
    library font - a TTF name, an unknown name, a missing library - is
    single-stroke (1): multi-stroke sizing is a PLT-font feature.

    Args:
        name: Font name (canonical, lowercase, or a ``"<base> SC"``
            variant).
        library_dir: Optional PLT font catalog directory override.

    Returns:
        The font's stroke count (>= 1); 1 for TTF/unknown fonts.
    """
    if not name or not name.strip():
        return 1
    base = base_font_name(name.strip()).lower()
    for font_name, data in load_plt_fonts(library_dir).items():
        if font_name.lower() == base:
            return data.stroke_count
    return 1


def plt_font_has_glyph(
    name: str,
    character: str,
    library_dir: Optional[Path] = None,
) -> bool:
    """Return whether a PLT-extracted font carries ``character``.

    Symbol fonts (e.g. Wingdings) engrave their ASCII slots with symbols, so
    the derived-glyph recipes cannot build every derived Unicode character;
    coverage tools (the font showcase) use this to render only the glyphs a
    font actually has - the PLT analogue of a TrueType font drawing its
    ``.notdef`` box. A TTF name, an unknown name, or a missing library
    reports ``True`` (only PLT fonts have a discrete glyph set to probe).

    Args:
        name: Font name (canonical, lowercase, or a ``"<base> SC"`` variant).
        character: The single character to probe.
        library_dir: Optional PLT font catalog directory override.

    Returns:
        ``True`` when the font is a PLT font carrying ``character`` (or is
        not a PLT font at all); ``False`` for a PLT font missing it.
    """
    if not name or not name.strip():
        return True
    base = base_font_name(name.strip()).lower()
    for font_name, data in load_plt_fonts(library_dir).items():
        if font_name.lower() == base:
            return character in data.characters
    return True
