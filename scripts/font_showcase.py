#!/usr/bin/env python3
"""Generate one font-showcase PDF per available font.

For every font selectable in a job spec -- the PLT-extracted keys of the
``Fonts/VP-EL-Fonts/catalog/`` files plus every ``*.ttf`` basename under ``Fonts/``
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
``Fonts/glyph_transforms.py``: the ASCII base set on one line, then the 17
derived Unicode characters built from those bases, so a showcase PDF shows
both sides of every recipe side by side. The two lines have different lengths
because ``-`` and ``|`` each feed several recipes. A TrueType font that lacks a
derived codepoint renders that font's own ``.notdef`` glyph, which is exactly
the coverage information a showcase is for.

Fonts listed in ``Fonts/VP-EL-Fonts/non_text_fonts.txt`` (symbol fonts such
as ``Wingdings``) are **non-text**: they engrave their ASCII slots with
symbols, so the letter sample block above is meaningless for them (the
derived-glyph pair is dropped, the small-caps variant is exempted at the
registry, and those fonts contribute no ``"<base> SC"`` PDF either). Their
showcase is a **key -> symbol mapping** instead: one cell per character of
``Fonts/ascii.txt``, each cell engraving the ASCII key (``a:``, ``b:``, ...)
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
# characters built from them by Fonts/glyph_transforms.py); the bases are in
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

    A **non-text** font (listed in ``Fonts/VP-EL-Fonts/non_text_fonts.txt``,
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
    non_text = font_name.lower() in non_text_font_names()
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
    rendered = render_showcase(font_name)
    document = PLTParser().parse_string(rendered.plt_content)

    output_path = output_dir / f"{safe_font_filename(font_name)}.pdf"
    fig = plot_plt_document(
        document,
        output_path=output_path,
        title=f"Font showcase - {font_name}",
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
            "case + digits/symbols) for every PLT and TrueType font."
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
            "No fonts found (VP-EL-Fonts catalog keys + Fonts/**.ttf basenames); nothing to do."
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
