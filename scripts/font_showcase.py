#!/usr/bin/env python3
"""Generate one font-showcase PDF per available font.

For every font selectable in a job spec -- the PLT-extracted keys of the
``fonts/VP-EL-fonts/catalog/`` files plus every ``*.ttf`` basename under ``fonts/``
(recursively), i.e. exactly :func:`plt_optimizer.generate.font_registry.font_name_choices` --
this script renders a fixed seven-line sample block and writes one
"simple"-mode outline PDF (the same black-outline styling the ``generate``
pipeline uses for its preview PDFs) into the output directory.

The seven lines are the font name itself (rendered in the font being
showcased) followed by::

    The quick brown fox jumps over the lazy dog
    ABCDEFGHIJKLMNOPQRSTUVWXYZ
    abcdefghijklmnopqrstuvwxyz
    1234567890 {[(!@#$%^&*.,?:;)]}
    - . 8 + _ c | = / ~ ? ! ^ < >
    – — • ∞ ± ¢ ≠ ≈ ≡ ¿ ¡ † ‡ ↑ ↓ ← →

The last two lines are the **derived-glyph pair** from
``fonts/glyph_transforms.py``: the ASCII base set on one line, then the 17
derived Unicode characters built from those bases, so a showcase PDF shows
both sides of every recipe side by side. The two lines have different lengths
because ``-`` and ``|`` each feed several recipes. A TrueType font that lacks a
derived codepoint renders that font's own ``.notdef`` glyph, which is exactly
the coverage information a showcase is for.

Fonts listed in ``fonts/VP-EL-fonts/non_text_fonts.txt`` (symbol fonts such
as ``Wingdings``) are **non-text**: they engrave their ASCII slots with
symbols, so the letter sample block above is meaningless for them (the
derived-glyph pair is dropped, the small-caps variant is exempted at the
registry, and those fonts contribute no ``"<base> SC"`` PDF either). Their
showcase is a **key -> symbol mapping** instead: one cell per character of
``fonts/ascii.txt``, each cell engraving the ASCII key (``a:``, ``b:``, ...)
in :data:`plt_optimizer.generate.ftext_renderer.DEFAULT_FONT_PATH` followed by
the symbol the showcased font engraves for that key, laid out as a
:data:`MAPPING_COLUMNS`-column grid under a header naming the mapping. The key
font is deliberately the readable default face, never the symbol font, so the
character the user must type always reads clearly. An ASCII slot the font
lacks (Wingdings has no ``"``) keeps its key with a blank symbol slot -- the
coverage information a showcase reports -- and is logged at WARNING.

Each label is rendered directly through the label renderer (no bin-packing,
no plate assembly, no PLT files) and plotted with
:func:`plt_optimizer.diagnostics.plotter.plot_plt_document` in ``simple_mode``,
so the PDF shows the engraved text plus the label boundary rectangle. The
mapping grid mixes two fonts in one drawing, which a single label cannot do, so
its geometry is composed from the renderers directly and emitted as HPGL (the
same ``IN;PA;``/``SP;`` framing the pipeline uses, Y-mirrored into the device
convention the plotter expects).

Labels are sized generously and ``max_h_compress`` is pinned to ``0.0`` so
horizontal compression never fires -- compression would flatten the native
``AA`` arcs of PLT-extracted fonts. When a font still renders wider than the
nominal label, the label is re-rendered once at a widened width so the
boundary frames the text instead of clipping it.

A font that cannot render the sample (e.g. a future PLT extraction missing a
character) is logged at ERROR and skipped; the run continues and prints a
summary of failures at the end.

Usage::

    uv run python scripts/font_showcase.py
    uv run python scripts/font_showcase.py --output /tmp/showcase -v
"""

from __future__ import annotations

import argparse
import logging
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # allow running as a plain script
    sys.path.insert(0, str(REPO_ROOT))

import matplotlib  # noqa: E402

matplotlib.use("Agg")  # headless: never open interactive windows

import numpy as np  # noqa: E402
import vpype as vp  # noqa: E402
from matplotlib import pyplot as plt  # noqa: E402

from plt_optimizer.core.parser import PLTParser  # noqa: E402
from plt_optimizer.diagnostics.plotter import plot_plt_document  # noqa: E402
from plt_optimizer.generate.font_registry import (  # noqa: E402
    FONTS_DIR,
    font_name_choices,
    non_text_font_names,
    plt_font_has_glyph,
    resolve_font,
)
from plt_optimizer.generate.ftext_renderer import (  # noqa: E402
    DEFAULT_FONT_PATH,
    render_text_line_ftext,
)
from plt_optimizer.generate.label_renderer import (  # noqa: E402
    PLT_FOOTER,
    PLT_HEADER,
    RenderedLabel,
    render_label_to_plt,
)
from plt_optimizer.generate.plt_font_renderer import (  # noqa: E402
    render_text_line_plt_font,
)
from plt_optimizer.generate.resolution import resolve_job_spec  # noqa: E402
from plt_optimizer.generate.schema import JobSpec, TextLine  # noqa: E402
from plt_optimizer.generate.text_geometry import (  # noqa: E402
    TextBlock,
    block_from_linecollection,
)

logger = logging.getLogger(__name__)

# Default artifact location (repo-root relative so the CWD never matters).
DEFAULT_OUTPUT_DIR: Path = REPO_ROOT / "test_output" / "font_showcase"

# The fixed sample block rendered under the font-name line. The last two
# lines are the derived-glyph pair (ASCII bases, then the derived Unicode
# characters built from them by fonts/glyph_transforms.py); the bases are in
# DERIVED_GLYPHS order and deduplicated (- and | each feed several recipes).
SHOWCASE_LINES: Tuple[str, ...] = (
    "The quick brown fox jumps over the lazy dog",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
    "1234567890 {[(|!@#$%^&*.,?:;)]}",
    "Unicode: \u2013 \u2014 \u2022 \u221e \u00b1 \u00a2 \u2260 \u2248 \u2261 "
    "\u00bf \u00a1 \u2020 \u2021 \u2191 \u2193 \u2190 \u2192",
)

# Nominal label geometry (inches). The width is generous so the widest
# sample line fits uncompressed in most fonts; over-wide fonts trigger the
# single widening re-render in :func:`render_showcase`. The height stacks the
# seven sample lines (the derived glyphs all fit the cap band, so the auto
# line-spacing fit keeps the requested spacing in most fonts).
LABEL_WIDTH: float = 12.0
LABEL_HEIGHT: float = 4.5
MARGIN: float = 0.25

# The font-name header is drawn a touch larger than the sample lines.
NAME_TEXT_HEIGHT: float = 0.45
BODY_TEXT_HEIGHT: float = 0.3

# --- Symbol-font (non-text) key -> symbol mapping layout, in inches ---------
# The ordered character set the mapping walks: the 94 printable ASCII
# characters, i.e. exactly the slots an engraved sample sheet is extracted
# from (see fonts/extract_plt_fonts.py).
ASCII_CHARS_FILE: Path = FONTS_DIR / "ascii.txt"

# The grid: MAPPING_COLUMNS cells per row, one cell per ASCII character. The
# column pitch is measured from the widest rendered cell (see
# :func:`render_mapping_layout`), so only the vertical pitch is a constant.
MAPPING_COLUMNS: int = 10
MAPPING_KEY_TEXT_HEIGHT: float = 0.18
MAPPING_SYMBOL_TEXT_HEIGHT: float = 0.3
MAPPING_KEY_SYMBOL_GAP: float = 0.12
MAPPING_COLUMN_GAP: float = 0.25
MAPPING_CELL_HEIGHT: float = 0.5
MAPPING_BASELINE_INSET: float = 0.14
MAPPING_HEADER_NAME_HEIGHT: float = 0.45
MAPPING_HEADER_SUB_HEIGHT: float = 0.22
MAPPING_HEADER_GAP: float = 0.35

# The subtitle naming the mode (ASCII '->' -- the key font has no arrow
# codepoint, and this caption is the showcase's own text, not engraving).
MAPPING_SUBTITLE: str = "ASCII key -> symbol mapping"


def safe_font_filename(font_name: str) -> str:
    """Derive a filesystem-safe PDF stem from a font name.

    Mirrors ``plt_optimizer/cli/generate.py::_sanitize_job_id``: whitespace
    runs collapse to single underscores and every character outside
    ``[A-Za-z0-9._-]`` is stripped.

    Args:
        font_name: The canonical font name.

    Returns:
        A safe file stem (never empty).
    """
    sanitized = re.sub(r"\s+", "_", font_name.strip())
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "", sanitized)
    return sanitized or "font"


def load_ascii_characters(ascii_file: Path = ASCII_CHARS_FILE) -> List[str]:
    """Return the ordered printable-ASCII characters the mapping walks.

    Mirrors ``fonts/extract_plt_fonts.py::load_characters``: every whitespace
    run in the file is a separator and the remaining characters, in file
    order, are the character set (94 for the shipped ``fonts/ascii.txt``).
    The rule is duplicated rather than imported because ``fonts/`` is a data
    directory, not a package, so the showcase stays a plain consumer of the
    same fixture the extractor owns.

    Args:
        ascii_file: The character-list file to read.

    Returns:
        Ordered single-character strings (space is not part of the set).

    Raises:
        ValueError: If the file cannot be read or carries no characters.
    """
    try:
        content = ascii_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Failed to read character file {ascii_file}: {exc}") from exc
    characters = "".join(content.split())
    if not characters:
        raise ValueError(f"Character file {ascii_file} contains no characters")
    return list(characters)


def is_non_text_font(font_name: str) -> bool:
    """Return whether ``font_name`` is a listed symbol (non-text) font.

    The single source of truth for the ``fonts/VP-EL-fonts/non_text_fonts.txt``
    membership test, shared by the sample-line pruning and the mapping-mode
    dispatch. Lookup is case-insensitive (the registry lowercases the list).

    Args:
        font_name: Canonical font name.

    Returns:
        ``True`` when the font engraves symbols in its ASCII slots.
    """
    return font_name.lower() in non_text_font_names()


def showcase_pdf_title(font_name: str) -> str:
    """Return the plot title for a letter font's showcase PDF.

    Args:
        font_name: Canonical font name.

    Returns:
        The matplotlib figure title.
    """
    return f"Font showcase - {font_name}"


def mapping_pdf_title(font_name: str) -> str:
    """Return the plot title for a symbol font's mapping showcase PDF.

    Names the mode explicitly: the page maps text characters to symbols, it
    does not sample the font's letterforms.

    Args:
        font_name: Canonical font name.

    Returns:
        The matplotlib figure title.
    """
    return f"Font showcase - {font_name} ({MAPPING_SUBTITLE})"


def build_showcase_job(font_name: str, width: float = LABEL_WIDTH) -> JobSpec:
    """Build the single-label showcase job for one font.

    The first content line is the font name itself (rendered in the font
    being showcased); the remaining lines are :data:`SHOWCASE_LINES`. The
    font cascades from the job level to every line, and ``max_h_compress``
    is pinned to ``0.0`` so compression never flattens PLT arc glyphs.

    Args:
        font_name: Canonical font name (validated by the schema cascade).
        width: Label width in inches (widened on the fit re-render).

    Returns:
        A root-level single-label :class:`JobSpec`.
    """
    return JobSpec(
        job_name=f"Font Showcase {font_name}",
        font=font_name,
        width=width,
        height=LABEL_HEIGHT,
        margin=MARGIN,
        count=1,
        max_h_compress=0.0,
        content=[
            TextLine(text=font_name, text_height=NAME_TEXT_HEIGHT),
            *[
                TextLine(text=line, text_height=BODY_TEXT_HEIGHT)
                for line in _covered_showcase_lines(font_name)
            ],
        ],
    )


def _covered_showcase_lines(font_name: str) -> Tuple[str, ...]:
    """Return :data:`SHOWCASE_LINES` pruned to the glyphs ``font_name`` has.

    A **non-text** font (listed in ``fonts/VP-EL-fonts/non_text_fonts.txt``,
    e.g. Wingdings) engraves its ASCII slots with symbols, so the derived-
    glyph pair is meaningless for it: the derived-glyph line is dropped
    entirely. Other PLT fonts engrave letters but may lack individual derived
    characters, so the line is pruned to the characters the font carries -
    the PLT analogue of a TrueType font drawing its ``.notdef`` box (missing
    glyphs are simply absent, which is the coverage information a showcase
    reports). TrueType fonts report full coverage here and rely on
    ``check_glyph_coverage=False`` to draw ``.notdef``.

    Args:
        font_name: Canonical font name.

    Returns:
        The sample lines, with the derived-glyph line dropped for non-text
        fonts and pruned to covered chars for the other PLT fonts.
    """
    non_text = is_non_text_font(font_name)
    pruned: List[str] = []
    for line in SHOWCASE_LINES:
        if line.startswith("Unicode: "):
            if non_text:
                continue
            kept = [
                char
                for char in line.removeprefix("Unicode: ").split(" ")
                if plt_font_has_glyph(font_name, char)
            ]
            if not kept:
                continue
            pruned.append("Unicode: " + " ".join(kept))
        else:
            pruned.append(line)
    return tuple(pruned)


def _widest_text_width(rendered: RenderedLabel) -> float:
    """Return the widest rendered text-line width in inches.

    Measures the per-line chunk records (the boundary rectangle always spans
    the full label width, so the label bounds cannot reveal text overflow).

    Args:
        rendered: The rendered label whose text chunks to measure.

    Returns:
        Maximum ``x_max - x_min`` over all text chunks (``0.0`` when the
        label carries no text chunks).
    """
    if not rendered.text_chunks:
        return 0.0
    return max(chunk.bounds[2] - chunk.bounds[0] for chunk in rendered.text_chunks)


def render_showcase(font_name: str) -> RenderedLabel:
    """Render the showcase label for one font, widening once if over-wide.

    Two-pass fit: render at the nominal :data:`LABEL_WIDTH`; if the widest
    text line exceeds the inner content area, re-render once with the label
    widened to ``text_width + 2 * margin`` so the boundary frames the text.
    Compression is disabled, so this is the only accommodation over-wide
    fonts get (and PLT arcs stay native).

    Args:
        font_name: Canonical font name.

    Returns:
        The final rendered label (text + boundary rectangle).

    Raises:
        PltFontRenderError: If a PLT font lacks a sample character.
        ValueError: If the font name is unknown or rendering fails.
    """
    label = resolve_job_spec(build_showcase_job(font_name))[0]
    # check_glyph_coverage=False: a TrueType font missing a sample codepoint
    # must render its .notdef box (the coverage info a showcase reports),
    # not abort the render. PLT fonts keep raising PltFontRenderError.
    rendered = render_label_to_plt(label, check_glyph_coverage=False)

    text_width = _widest_text_width(rendered)
    needed_width = text_width + 2.0 * MARGIN
    if needed_width > label.width:
        logger.info(
            "Font %s renders %.2f in wide (label %.2f in); widening to %.2f in",
            font_name,
            text_width,
            label.width,
            needed_width,
        )
        label = resolve_job_spec(build_showcase_job(font_name, width=needed_width))[0]
        rendered = render_label_to_plt(label, check_glyph_coverage=False)
    return rendered


def write_showcase_pdf(font_name: str, output_dir: Path) -> Path:
    """Render one font and write its simple-outline showcase PDF.

    A **non-text** (symbol) font is routed to :func:`write_mapping_pdf`: its
    ASCII slots hold symbols, so the letter sample block would engrave glyphs
    that read as gibberish. Letter fonts render the sample block unchanged.

    Args:
        font_name: Canonical font name.
        output_dir: Directory to write ``<font>.pdf`` into (created if
            missing).

    Returns:
        Path of the written PDF.

    Raises:
        Exception: Propagates rendering/plotting failures to the caller
            (handled per font in :func:`main`).
    """
    if is_non_text_font(font_name):
        return write_mapping_pdf(font_name, output_dir)

    rendered = render_showcase(font_name)
    document = PLTParser().parse_string(rendered.plt_content)

    output_path = output_dir / f"{safe_font_filename(font_name)}.pdf"
    fig = plot_plt_document(
        document,
        output_path=output_path,
        title=showcase_pdf_title(font_name),
        simple_mode=True,
        gridlines=False,
    )
    plt.close(fig)
    return output_path


@dataclass(frozen=True)
class MappingCell:
    """One ``key: symbol`` cell of a symbol font's mapping showcase.

    Attributes:
        character: The ASCII character the user types.
        key_block: The ``"<char>:"`` label, rendered in the readable default
            face (:data:`DEFAULT_FONT_PATH`), baseline at ``y = 0``.
        symbol_block: The showcased font's glyph for :attr:`character`
            (baseline at ``y = 0``), or ``None`` when the font has no such
            ASCII slot.
        key_width: Ink width of :attr:`key_block` in inches.
        symbol_width: Ink width of :attr:`symbol_block` (``0.0`` when absent).
    """

    character: str
    key_block: TextBlock
    symbol_block: Optional[TextBlock]
    key_width: float
    symbol_width: float

    @property
    def width(self) -> float:
        """Total ink width of the cell (key + gap + symbol), in inches."""
        return self.key_width + MAPPING_KEY_SYMBOL_GAP + self.symbol_width


def _ink_width(block: Optional[TextBlock]) -> float:
    """Return a block's ink width in inches (``0.0`` when it has no ink).

    Args:
        block: The block to measure, or ``None`` (a missing glyph).

    Returns:
        ``x_max - x_min`` of the block's swept bounds, else ``0.0``.
    """
    if block is None:
        return 0.0
    bounds = block.bounds()
    if bounds is None:
        return 0.0
    return bounds[2] - bounds[0]


def _render_default_font_block(text: str, height_inches: float) -> TextBlock:
    """Render ``text`` in the readable default face (:data:`DEFAULT_FONT_PATH`).

    Used for the ASCII keys and the mapping subtitle: the character the user
    must type has to read clearly, so it is never drawn in the symbol font.
    ``check_glyph_coverage=False`` keeps the showcase's coverage contract (a
    missing codepoint draws ``.notdef`` instead of aborting).

    Args:
        text: The string to render.
        height_inches: Reference cap height in inches.

    Returns:
        The rendered block (baseline ``y = 0``, +y up).
    """
    return block_from_linecollection(
        render_text_line_ftext(
            text,
            height_inches,
            DEFAULT_FONT_PATH,
            check_glyph_coverage=False,
        )
    )


def _render_in_showcase_font(font_name: str, text: str, height_inches: float) -> TextBlock:
    """Render ``text`` in the showcased font, dropping characters it lacks.

    The header line (the font's own name) is engraved in the showcased font so
    the page carries a sample of its glyph shapes. A PLT font has a discrete
    glyph set, so uncovered characters are dropped up front (the PLT analogue
    of a TrueType ``.notdef``); TrueType fonts render everything and draw their
    own ``.notdef`` box.

    Args:
        font_name: Canonical font name.
        text: The string to render.
        height_inches: Reference cap height in inches.

    Returns:
        The rendered block (baseline ``y = 0``, +y up); empty when the font
        covers none of ``text``.
    """
    ref = resolve_font(font_name)
    if ref.kind != "plt":
        return block_from_linecollection(
            render_text_line_ftext(
                text,
                height_inches,
                ref.path,
                check_glyph_coverage=False,
            )
        )
    kept = "".join(char for char in text if plt_font_has_glyph(font_name, char))
    if len(kept) != len(text):
        logger.warning(
            "Font %s lacks %d header character(s); rendering %r for %r",
            font_name,
            len(text) - len(kept),
            kept,
            text,
        )
    if not kept:
        return TextBlock.empty()
    return render_text_line_plt_font(kept, height_inches, font_name)


def _render_symbol_block(
    font_name: str,
    character: str,
    height_inches: float,
) -> Optional[TextBlock]:
    """Render one symbol-font glyph, or ``None`` when the slot is missing.

    A PLT font without ``character`` reports ``None`` (the cell keeps its key
    with a blank symbol slot, so the grid stays a complete map of the ASCII
    range). TrueType fonts report their ``.notdef`` box instead, matching the
    coverage reporting of the letter showcase.

    Args:
        font_name: Canonical font name.
        character: The single ASCII character to engrave.
        height_inches: Reference cap height in inches.

    Returns:
        The glyph block (baseline ``y = 0``, +y up), or ``None``.
    """
    if plt_font_has_glyph(font_name, character):
        return _render_in_showcase_font(font_name, character, height_inches)
    logger.warning(
        "Font %s has no glyph for %r; leaving its symbol slot blank",
        font_name,
        character,
    )
    return None


def build_mapping_cells(
    font_name: str,
    characters: Sequence[str],
) -> List[MappingCell]:
    """Build one ``key: symbol`` cell per character of ``characters``.

    Args:
        font_name: The showcased (symbol) font providing the symbols.
        characters: The ASCII characters to map, in grid order.

    Returns:
        One :class:`MappingCell` per character, in the same order.
    """
    cells: List[MappingCell] = []
    for character in characters:
        key_block = _render_default_font_block(f"{character}:", MAPPING_KEY_TEXT_HEIGHT)
        symbol_block = _render_symbol_block(font_name, character, MAPPING_SYMBOL_TEXT_HEIGHT)
        cells.append(
            MappingCell(
                character=character,
                key_block=key_block,
                symbol_block=symbol_block,
                key_width=_ink_width(key_block),
                symbol_width=_ink_width(symbol_block),
            )
        )
    return cells


def render_mapping_layout(
    font_name: str,
    characters: Optional[Sequence[str]] = None,
) -> str:
    """Compose the key -> symbol mapping grid for one symbol font as HPGL.

    A cell mixes two fonts (the readable key, the symbol font's glyph), which
    a single label cannot express -- a label carries one font per line -- so
    the geometry is composed directly from the renderers and emitted in the
    pipeline's framing. Every block is rendered in the renderers' +y-up frame,
    placed, then Y-mirrored across the page height into the device convention
    the plotter (and
    :func:`plt_optimizer.diagnostics.plotter.plot_plt_document`) expects --
    the same correction ``label_renderer._flip_y_coordinates_in_plt`` applies
    to a rendered label.

    Args:
        font_name: The showcased (symbol) font.
        characters: The characters to map (default:
            :func:`load_ascii_characters`).

    Returns:
        HPGL content (``IN;PA;`` header, geometry, ``SP;`` footer) sized to
        the grid plus a boundary rectangle.

    Raises:
        ValueError: If ``characters`` is empty (nothing to lay out).
    """
    chars = list(characters) if characters is not None else load_ascii_characters()
    if not chars:
        raise ValueError("Mapping layout needs at least one character")

    cells = build_mapping_cells(font_name, chars)
    cell_width = max(cell.width for cell in cells) + MAPPING_COLUMN_GAP
    rows = math.ceil(len(cells) / MAPPING_COLUMNS)

    header_name = _render_in_showcase_font(font_name, font_name, MAPPING_HEADER_NAME_HEIGHT)
    header_sub = _render_default_font_block(MAPPING_SUBTITLE, MAPPING_HEADER_SUB_HEIGHT)

    grid_height = rows * MAPPING_CELL_HEIGHT
    grid_bottom = MARGIN
    sub_baseline = grid_bottom + grid_height + MAPPING_HEADER_GAP + MAPPING_HEADER_SUB_HEIGHT
    name_baseline = sub_baseline + MAPPING_HEADER_GAP + MAPPING_HEADER_NAME_HEIGHT
    page_height = name_baseline + MAPPING_HEADER_NAME_HEIGHT + MARGIN
    page_width = (
        max(
            MAPPING_COLUMNS * cell_width - MAPPING_COLUMN_GAP,
            _ink_width(header_name),
            _ink_width(header_sub),
        )
        + 2.0 * MARGIN
    )

    blocks: List[TextBlock] = [
        header_name.translate(MARGIN, name_baseline),
        header_sub.translate(MARGIN, sub_baseline),
    ]
    for index, cell in enumerate(cells):
        column = index % MAPPING_COLUMNS
        row = index // MAPPING_COLUMNS
        left = MARGIN + column * cell_width
        baseline = grid_bottom + (rows - 1 - row) * MAPPING_CELL_HEIGHT + MAPPING_BASELINE_INSET
        blocks.append(cell.key_block.translate(left, baseline))
        if cell.symbol_block is not None:
            blocks.append(
                cell.symbol_block.translate(
                    left + cell.key_width + MAPPING_KEY_SYMBOL_GAP,
                    baseline,
                )
            )

    boundary = vp.LineCollection()
    rect = vp.rect(0, 0, page_width, page_height)
    if isinstance(rect, np.ndarray) and rect.size > 0:
        boundary.append(rect)
    blocks.append(block_from_linecollection(boundary))

    # Device convention (+y downward): mirror every block across the page
    # height, exactly as the label pipeline mirrors a rendered label.
    geometry = ";".join(
        block.mirrored_y(page_height).to_hpgl() for block in blocks if not block.is_empty()
    )
    return f"{PLT_HEADER}{geometry};{PLT_FOOTER}"


def write_mapping_pdf(font_name: str, output_dir: Path) -> Path:
    """Render one symbol font's key -> symbol mapping to its outline PDF.

    Args:
        font_name: Canonical font name of a listed non-text (symbol) font.
        output_dir: Directory to write ``<font>.pdf`` into (created if
            missing).

    Returns:
        Path of the written PDF.

    Raises:
        Exception: Propagates rendering/plotting failures to the caller
            (handled per font in :func:`main`).
    """
    document = PLTParser().parse_string(render_mapping_layout(font_name))

    output_path = output_dir / f"{safe_font_filename(font_name)}.pdf"
    fig = plot_plt_document(
        document,
        output_path=output_path,
        title=mapping_pdf_title(font_name),
        simple_mode=True,
        gridlines=False,
    )
    plt.close(fig)
    return output_path


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the showcase utility.

    Returns:
        Configured parser with ``--output`` and ``--verbose`` flags.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Render a font-showcase PDF (font name + pangram + upper/lower "
            "case + digits/symbols) for every PLT and TrueType font. Fonts "
            "listed in fonts/VP-EL-fonts/non_text_fonts.txt get an ASCII key "
            "-> symbol mapping grid instead."
        )
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory for the PDFs (default: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point: render every available font to its own showcase PDF.

    Args:
        argv: Optional argument override (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` when at least one PDF was written (per-font failures are
        logged and skipped), ``1`` when nothing could be produced.
    """
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    fonts = font_name_choices()
    if not fonts:
        logger.error(
            "No fonts found (VP-EL-Fonts catalog keys + fonts/**.ttf basenames); nothing to do."
        )
        return 1

    logger.info("Showcasing %d font(s) into %s", len(fonts), output_dir)
    succeeded: List[str] = []
    failed: List[Tuple[str, str]] = []
    for font_name in fonts:
        try:
            pdf_path = write_showcase_pdf(font_name, output_dir)
        except Exception as exc:  # one bad font must not kill the run
            logger.error("Font %s failed: %s", font_name, exc, exc_info=True)
            failed.append((font_name, str(exc)))
            continue
        logger.info("Wrote %s", pdf_path)
        succeeded.append(font_name)

    logger.info(
        "Font showcase complete: %d/%d PDFs written%s",
        len(succeeded),
        len(fonts),
        f" ({', '.join(name for name, _ in failed)} failed)" if failed else "",
    )
    for font_name, message in failed:
        logger.error("FAILED font %s: %s", font_name, message)
    return 0 if succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
