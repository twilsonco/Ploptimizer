"""Composite contact sheet: every plate's proof, laid out by PyPhotoCollage.

The generation pipeline renders one whole-sheet proof (``all_plate<N>.pdf``) per
plate — ink, holes, relief regions, and the plate rectangle. This module turns
those plates into a single contact sheet, ``<output_dir>/<job_id>_proof.jpeg``,
so an operator can approve a whole job — every plate, every label, each caption
with its plate number, material size and material — from one JPEG.

The layout engine is the vendored **PyPhotoCollage** (MIT, © Tim Wilson; see
:mod:`plt_optimizer.vendor`), called as a library: each plate proof is
rasterized with matplotlib, cropped to its drawn content, captioned, and handed
to ``pyphotocollage.makeCollage``. The shop tunes the collage with
:data:`PROOF_COLLAGE_DEFAULT_ARGS`-plus-a-string — the same argument string the
standalone tool takes (``-t rows -r 1.375 -g 12 …``), parsed by upstream's own
``make_arg_parser``, so every feature upstream exposes is reachable from
``job-config.json``'s ``proof_collage_args`` or ``--proof-collage-args``
without this module knowing about it.

Panels are cropped to their drawn content (the plotter module measures the
figure's tight bounding box; see
:func:`plt_optimizer.diagnostics.plotter.figure_to_image`), so a 48x24 plate is
not 4.6x the canvas of a 10x10 plate mostly filled with empty material.

Determinism is a product requirement (identical input must engrave identical
output, root ``AGENTS.md``), and upstream's ``nested`` collage type, ``random``
ordering, and ``--rotate-images-max-deg`` all draw from the global ``random``
module. :func:`compose_proof_sheet` therefore seeds the generator with
:data:`PROOF_DETERMINISM_SEED` before every call, and the default argument
string selects the deterministic ``rows`` layout in plate order.

Like every preview in this package the sheet is **non-critical**: a panel that
fails to rasterize is skipped with a warning, and the run is never failed.
matplotlib is import-guarded because it is skipped on Windows 7, where the
headless CLI produces no previews at all (see
:mod:`plt_optimizer.generate.vectorize`).
"""

from __future__ import annotations

import argparse
import logging
import random
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

# Upstream is untyped (mypy: ignore_errors for that module; see
# plt_optimizer/vendor/CHANGES.md), so these three entry points are reached
# through module-level aliases that carry the signatures this module relies on.
# That keeps the seam typed and keeps every upstream call checked, instead of
# scattering ``# type: ignore`` over the call site below.
from plt_optimizer.vendor import pyphotocollage as _pyphotocollage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from matplotlib.figure import Figure

    _make_arg_parser: Callable[[], argparse.ArgumentParser]
    _make_collage: Callable[..., Image.Image]
    _resize_to_max: Callable[..., Image.Image]
else:
    _make_arg_parser = _pyphotocollage.make_arg_parser
    _make_collage = _pyphotocollage.makeCollage
    _resize_to_max = _pyphotocollage.resize_img_to_max_size

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import-guarded: matplotlib is skipped on Windows 7
    from plt_optimizer.diagnostics.plotter import (
        PlotterError,
        figure_to_image,
        plot_data_aspect,
    )

    PLOTTER_AVAILABLE = True
except ImportError:  # pragma: no cover
    PlotterError = Exception  # type: ignore[assignment,misc,unused-ignore]
    figure_to_image = None  # type: ignore[assignment]
    plot_data_aspect = None  # type: ignore[assignment]
    PLOTTER_AVAILABLE = False

# ---------------------------------------------------------------------------
# Sheet constants (pinned by tests/test_proof_composite.py)
# ---------------------------------------------------------------------------

#: Suffix of the contact sheet, appended to the job id.
PROOF_IMAGE_SUFFIX = ".jpeg"

#: The collage's own command-line defaults, in the standalone tool's syntax.
#:
#: These come first in the argument list, so anything the shop writes in
#: ``proof_collage_args`` (or ``--proof-collage-args``) wins: ``argparse`` keeps
#: the last occurrence of a flag, which is exactly override semantics.
#:
#: * ``-t rows`` — rows of equal-height panels, the deterministic layout (the
#:   ``nested`` type is available and reproducible, just seeded).
#: * ``-O input`` — plate order, so the sheet reads plate 1, 2, 3 …
#: * ``-S 4096`` — cap the sheet's longest edge (upstream's own default is 5000).
#: * ``-r 1.375`` — a slightly landscape sheet, which suits landscape plates.
#: * ``-g 12`` — a visible gutter between panels.
#: * ``-b 255,255,255`` — white gutters, not upstream's black default: a proof
#:   is printed and photocopied. (Upstream's colour syntax is bare
#:   ``r,g,b`` — parentheses are rejected by its own parser.)
PROOF_COLLAGE_DEFAULT_ARGS = "-t rows -O input -S 4096 -r 1.375 -g 12 -b 255,255,255"

#: Seed for the global generator before every layout call.
#:
#: A fixed constant, not a per-run value: the sheet has to be byte-identical
#: across runs of the same job, including for the collage types that randomize.
PROOF_DETERMINISM_SEED = 20261123

#: Raster resolution of the panels, dots per inch of *material*.
#:
#: A panel of a 23x16 in plate at 100 dpi is 2300 px wide, so glyph strokes
#: engraved at 0.125 in (127 plotter units) land on ~12 px — comfortably
#: legible. Lower values make the proof unreadable; higher values cost file size
#: and memory, not information (the sheet's long edge is capped by the ``-S``
#: argument), and the composite is delivered as JPEG, where the encoder's chroma
#: smoothing, not the sample grid, is what softens thin strokes.
PROOF_PANEL_DPI = 100

#: White frame around the finished sheet, in sheet pixels.
PROOF_SHEET_MARGIN_PX = 12

#: Height of the caption strip drawn under each panel, in sheet pixels.
PROOF_PANEL_LABEL_PX = 26

#: Font height of the per-plate caption, in sheet pixels.
PROOF_LABEL_FONT_SIZE_PX = 18

#: RGB colour of the caption text.
PROOF_LABEL_RGB: Tuple[int, int, int] = (70, 70, 70)

#: JPEG quality for the contact sheet.
PROOF_JPEG_QUALITY = 92

#: Ordering modes that need per-image metadata in-memory panels do not have.
#:
#: ``oldest_first`` / ``newest_first`` sort by file modification time and
#: ``filename`` by path; panels come off the renderer with neither, so
#: :func:`_order_panels` logs once and keeps plate order.
UNSUPPORTED_ORDER_MODES = ("oldest_first", "newest_first")


def proof_image_name(job_id: str) -> str:
    """File name of the composite contact sheet for a job.

    Args:
        job_id: Identifier of the job being generated.

    Returns:
        ``"<job_id>_proof.jpeg"``.
    """
    return f"{job_id}_proof{PROOF_IMAGE_SUFFIX}"


def panel_caption(
    plate_number: int,
    size_in: Optional[Tuple[float, float]] = None,
    material: Optional[str] = None,
) -> str:
    """Caption drawn under one plate's panel.

    Reads ``"Plate 1 — 23.0 x 16.0 in — ACM"``: plate number, usable material
    size, and the stock name. The plate number leads so an operator scanning the
    sheet matches panels to plates (and to the ``p01``-tagged PLT folders)
    without reading the rest. Parts that are unknown are dropped, so a stubbed
    layout still captions as ``"Plate 1"``.

    Args:
        plate_number: 1-based plate index (packing order).
        size_in: ``(width, height)`` usable plate size in inches, or ``None``
            when the layout is unavailable.
        material: The plate's stock material name, or ``None``.

    Returns:
        The caption text.
    """
    parts = [f"Plate {plate_number}"]
    if size_in is not None:
        parts.append(f"{size_in[0]:.1f} x {size_in[1]:.1f} in")
    if material:
        parts.append(material)
    return " \u2014 ".join(parts)


def plate_size_in(
    packed_plates: Sequence[Any],
    plate_number: int,
) -> Optional[Tuple[float, float]]:
    """Usable ``(width, height)`` in inches of one packed plate.

    Args:
        packed_plates: The layout's plates, in packing order (``plate_number``
            is 1-based over this list).
        plate_number: 1-based plate index.

    Returns:
        The plate's usable size, or ``None`` when the plate is not in the list
        (a stubbed layout, e.g. in tests).
    """
    index = plate_number - 1
    if 0 <= index < len(packed_plates):
        plate = packed_plates[index]
        return (float(plate.width), float(plate.height))
    return None


def parse_collage_args(args: str = "") -> argparse.Namespace:
    """Parse the shop's collage argument string with upstream's own parser.

    :data:`PROOF_COLLAGE_DEFAULT_ARGS` is prepended, so the shop's string only
    has to name what it changes (``argparse`` keeps the last occurrence of a
    flag).

    Args:
        args: Extra command-line arguments for the collage, in the standalone
            tool's syntax (``"-t nested -r 1.2 -m 8"``). Empty string, or
            ``None`` passed by a caller that resolved an absent config field,
            means "defaults only".

    Returns:
        The parsed options, ready to hand to ``pyphotocollage.makeCollage``.

    Raises:
        SystemExit: The string is not valid for upstream's parser (argparse's
            contract). The caller presents it as a configuration error, exactly
            as an invalid command line is presented.
    """
    tokens = shlex.split(PROOF_COLLAGE_DEFAULT_ARGS)
    if args:
        tokens.extend(shlex.split(args))
    return _make_arg_parser().parse_args(tokens)


def _load_font(size_px: int) -> Any:
    """Load a TrueType font sized for the sheet, falling back to PIL's bitmap.

    ``ImageFont.truetype`` returns a ``FreeTypeFont`` and
    ``ImageFont.load_default`` an ``ImageFont``; both are accepted by
    ``ImageDraw.text``, which is the only consumer.

    Args:
        size_px: Requested font height in sheet pixels.

    Returns:
        A Pillow font usable by :class:`PIL.ImageDraw.ImageDraw`.
    """
    from matplotlib import font_manager  # local import: lazy, like vectorize

    try:
        return ImageFont.truetype(font_manager.findfont("DejaVu Sans"), size_px)
    except OSError:  # pragma: no cover - corrupt or absent system font
        return ImageFont.load_default()


def _add_caption(panel: Image.Image, caption: str, *, strip_px: int, font: Any) -> Image.Image:
    """Return ``panel`` with a caption strip pasted under it.

    Args:
        panel: The rasterized plate proof.
        caption: Text to draw (plate number, material size and material).
        strip_px: Height of the strip under the panel.
        font: Pillow font for the caption.

    Returns:
        A new RGBA image ``(panel.width, panel.height + strip_px)``.
    """
    out = Image.new("RGBA", (panel.width, panel.height + strip_px), (255, 255, 255, 255))
    out.paste(panel.convert("RGBA"), (0, 0))
    ImageDraw.Draw(out).text(
        (4, panel.height + 2), caption, font=font, fill=PROOF_LABEL_RGB + (255,)
    )
    return out


def _add_title(
    sheet: Image.Image, title: str, *, strip_px: int, font: Any, margin_px: int
) -> Image.Image:
    """Return ``sheet`` with a job title band pasted above it.

    Args:
        sheet: The assembled collage.
        title: Text for the band (job id and plate count).
        strip_px: Height of the band.
        font: Pillow font for the title.
        margin_px: Left padding of the text.

    Returns:
        A new RGBA image ``(sheet.width, sheet.height + strip_px)``.
    """
    band = Image.new("RGBA", (sheet.width, strip_px), (255, 255, 255, 255))
    ImageDraw.Draw(band).text((margin_px, 2), title, font=font, fill=(20, 20, 20, 255))
    out = Image.new("RGBA", (sheet.width, sheet.height + strip_px), (255, 255, 255, 255))
    out.paste(band, (0, 0))
    out.paste(sheet, (0, strip_px))
    return out


def _framed(sheet: Image.Image, margin_px: int) -> Image.Image:
    """Return ``sheet`` surrounded by a white margin.

    Args:
        sheet: The assembled collage.
        margin_px: Width of the frame on every side.

    Returns:
        A new RGBA image ``(w + 2*margin_px, h + 2*margin_px)``, or ``sheet``
        itself when there is no frame.
    """
    if margin_px <= 0:
        return sheet
    out = Image.new(
        "RGBA", (sheet.width + 2 * margin_px, sheet.height + 2 * margin_px), (255, 255, 255, 255)
    )
    out.paste(sheet, (margin_px, margin_px))
    return out


def _order_panels(
    panels: Sequence[Tuple[str, Image.Image]], order: str
) -> List[Tuple[str, Image.Image]]:
    """Apply upstream's ``--order`` to captioned panels.

    ``input`` (the default, and what a proof sheet wants) keeps plate order;
    ``filename`` sorts by caption, which is the closest analogue of sorting by
    file name when the panels were never files; ``random`` shuffles under the
    determinism seed; ``oldest_first`` / ``newest_first`` need file
    modification times that in-memory panels do not have, so they log once and
    keep plate order.

    Args:
        panels: ``(caption, image)`` per plate, in plate order.
        order: The parsed ``--order`` value.

    Returns:
        The panels in the order they are handed to the layout engine.
    """
    ordered = list(panels)
    if order == "filename":
        return sorted(ordered, key=lambda entry: entry[0])
    if order == "random":
        random.shuffle(ordered)
        return ordered
    if order in UNSUPPORTED_ORDER_MODES:
        logger.warning(
            "Collage order %r needs file timestamps; keeping plate order for the proof sheet", order
        )
    return ordered


def compose_proof_sheet(
    panels: Sequence[Tuple[str, Figure]],
    output_path: Path,
    *,
    args: str = "",
    dpi: int = PROOF_PANEL_DPI,
    margin_px: int = PROOF_SHEET_MARGIN_PX,
    label_strip_px: int = PROOF_PANEL_LABEL_PX,
    label_font_size_px: int = PROOF_LABEL_FONT_SIZE_PX,
    title: str = "",
) -> Optional[Path]:
    """Collage the whole-sheet plate proofs into one contact-sheet JPEG.

    Each panel is rasterized from ``fig`` cropped to the figure's drawn content,
    captioned, laid out by the vendored PyPhotoCollage engine under the parsed
    ``args``, framed, and written as an RGB JPEG at ``output_path``. Panels that
    fail to rasterize are skipped with a warning; a panel is never cropped,
    stretched, or reordered away from plate order under the default arguments.

    Args:
        panels: ``(caption, figure)`` per plate, in plate order — the caption
            is drawn under the panel.
        output_path: Where to write the JPEG (parent directories are created).
        args: Extra PyPhotoCollage command-line arguments (``"-t nested -r
            1.2"``), applied on top of :data:`PROOF_COLLAGE_DEFAULT_ARGS`.
        dpi: Raster resolution of the panels, dots per inch of material.
        margin_px: White frame around the finished sheet.
        label_strip_px: Height of each panel's caption strip.
        label_font_size_px: Caption font height in sheet pixels.
        title: Optional job title band drawn across the top of the sheet.

    Returns:
        The path written, or ``None`` when there was nothing to compose — no
        panels, every panel failed, or matplotlib is unavailable.

    Raises:
        OSError: The JPEG could not be written (the caller degrades).
        SystemExit: ``args`` is not valid for the collage's argument parser.
    """
    if not PLOTTER_AVAILABLE or figure_to_image is None or plot_data_aspect is None:
        logger.warning("matplotlib is unavailable; skipping the composite proof sheet")
        return None
    if not panels:
        logger.warning("No plate proofs to compose; skipping the composite proof sheet")
        return None

    opts = parse_collage_args(args)
    font = _load_font(label_font_size_px)
    captioned: List[Tuple[str, Image.Image]] = []
    for caption, fig in panels:
        try:
            raster = figure_to_image(fig, dpi=dpi, crop_aspect=plot_data_aspect(fig))
        except (PlotterError, RuntimeError, OSError, ValueError) as exc:
            logger.warning("Skipping uncapturable proof panel %s: %s", caption, exc)
            continue
        captioned.append(
            (caption, _add_caption(raster, caption, strip_px=label_strip_px, font=font))
        )
    if not captioned:
        logger.warning("No proof panels could be rasterized; skipping the composite proof sheet")
        return None

    # Upstream's CLI honours `-c` by truncating the image list (and only when
    # more than two images are requested - its own semantics, kept verbatim),
    # then orders; seed first because `--order random` draws from the generator.
    if opts.count > 2:
        captioned = captioned[: opts.count]
    random.seed(PROOF_DETERMINISM_SEED)
    captioned = _order_panels(captioned, opts.order)
    if opts.init_size > 0:
        captioned = [
            (caption, _resize_to_max(image, opts.init_size, no_antialias=not opts.noantialias))
            for caption, image in captioned
        ]

    images = [image for _caption, image in captioned]
    # Seeded again: `nested`, and `--rotate-images-max-deg`, draw from the global
    # generator during layout, and the sheet must be reproducible.
    random.seed(PROOF_DETERMINISM_SEED)
    sheet = _make_collage(
        images,
        collage_type=opts.collage_type,
        max_recursion_depth=opts.max_recursion_depth,
        spacing=int(opts.imagegap),
        no_antialias=not opts.noantialias,
        background=tuple(int(c) for c in opts.background),
        target_aspect_ratio=float(opts.target_aspect_ratio),
        max_collage_size=int(opts.max_size),
        round_image_corners_perc=float(opts.round_image_corners_perc),
        round_collage_corners_perc=float(opts.round_collage_corners_perc),
        rotate_images_max_deg=int(opts.rotate_images_max_deg),
        show_recursion_depth=bool(opts.show_recursion_depth),
    )

    if title:
        sheet = _add_title(
            sheet,
            title,
            strip_px=label_strip_px + 6,
            font=_load_font(int(label_font_size_px * 1.25)),
            margin_px=margin_px,
        )
    sheet = _framed(sheet, margin_px)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.convert("RGB").save(output_path, "JPEG", quality=PROOF_JPEG_QUALITY, optimize=True)
    logger.info(
        "Wrote composite proof sheet %s (%dx%d px, %d plates, collage type %s)",
        output_path,
        sheet.width,
        sheet.height,
        len(images),
        opts.collage_type,
    )
    return output_path
