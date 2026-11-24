"""Composite contact sheet: every plate's proof, laid out by PyPhotoCollage.

The generation pipeline renders one whole-sheet proof (``all_plate<N>.pdf``) per
plate — ink, holes, relief regions, and the plate rectangle. This module tiles
those previews into a single JPEG, ``<output_dir>/<job_id>_proof.jpeg``, so an
operator can approve a whole job — every plate, every label — from one file.
There are no captions and no title band: the panels are the tight-cropped
previews themselves, which keeps the sheet dense and the panels' aspect ratios
exactly the plates' (see :func:`collage_layout`).

The layout engine is the vendored **PyPhotoCollage** (MIT, © Tim Wilson; see
:mod:`plt_optimizer.vendor`), called as a library: each plate proof is
rasterized with matplotlib, cropped to its drawn content, and handed to
``pyphotocollage.makeCollage``. The shop tunes the collage with
:data:`PROOF_COLLAGE_DEFAULT_ARGS`-plus-a-string — the same argument string the
standalone tool takes (``-t rows -g 12 -b 255,255,255 …``), parsed by upstream's
own ``make_arg_parser``, so every feature upstream exposes is reachable from
``job-config.json``'s ``proof_collage_args`` or ``--proof-collage-args``
without this module knowing about it.

Panels are cropped to their drawn content (the plotter module measures the
figure's tight bounding box; see
:func:`plt_optimizer.diagnostics.plotter.figure_to_image`), so a 48x24 plate is
not 4.6x the canvas of a 10x10 plate mostly filled with empty material.

Two of upstream's arguments are **computed from the plate set** rather than
fixed, because the engine resamples its panels on the way to the sheet: with a
10-plate job the first version of this module asked for a 12 px gutter and got
3 px, panels touching, edges bled into each other. :func:`collage_layout`
replays that arithmetic and hands over the target aspect ratio (``-r``) that
puts the panels in a single row and the sheet size (``-S``) that layout needs,
so panels reach the sheet at scale 1.0; a shop that names ``-r``/``-S`` wins,
and the gutter (``-g``) is then inflated by the squeeze those values cause.

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
import math
import random
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, List, Optional, Sequence, Tuple

from PIL import Image

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
    from matplotlib import transforms

    from plt_optimizer.diagnostics.plotter import (
        PlotterError,
        figure_to_image,
        tight_content_box,
    )

    PLOTTER_AVAILABLE = True
except ImportError:  # pragma: no cover
    PlotterError = Exception  # type: ignore[assignment,misc,unused-ignore]
    figure_to_image = None  # type: ignore[assignment]
    tight_content_box = None  # type: ignore[assignment]
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
#: * ``-O input_order`` — plate order, so the sheet reads plate 1, 2, 3 …
#:   (upstream's parser spells the value ``input_order``; the short form its own
#:   help text advertises, ``input``, raises, so :func:`parse_collage_args`
#:   normalizes it).
#: * ``-S 5000`` — cap the sheet's longest edge at upstream's own default: the
#:   cap is the sheet's *final* size, so a small ``-S`` shrinks every panel.
#: * ``-i 2400`` — resize each panel to at most 2400 px on input (upstream's
#:   default is 1000, which caps the whole sheet near 1000 px and renders a
#:   24 in plate's glyph strokes unreadable).
#: * ``-g 12`` — a visible gutter between panels.
#: * ``-b 255,255,255`` — white gutters, not upstream's black default: a proof
#:   is printed and photocopied. (Upstream's colour syntax is bare
#:   ``r,g,b`` — parentheses are rejected by its own parser.)
#:
#: Deliberately absent: ``-r``. Upstream sizes the rows from ``-r`` and then
#: resamples every row to the narrowest one, so a fixed value rescales (and
#: collapses) every panel set it was not written for -- see :func:`collage_layout`.
PROOF_COLLAGE_DEFAULT_ARGS = "-t rows -O input_order -S 5000 -i 2400 -g 12 -b 255,255,255"


@dataclass(frozen=True)
class CollageLayout:
    """The sheet geometry to hand the collage engine.

    Attributes:
        aspect: ``target_aspect_ratio`` (``-r``): the sheet aspect to aim at, which
            upstream uses to choose the row count (see :func:`collage_layout`).
        row_scale: The resample factor upstream applies on the way to that sheet
            (1.0 = panels pass through unscaled).
        gap_px: ``imagegap`` (``-g``) to request so the gutter **lands** at the
            width that was asked for after that resample.
        max_size_px: ``max_collage_size`` (``-S``): the sheet's final longest
            edge, sized so the row is not squeezed (see :func:`collage_layout`).
    """

    aspect: float
    row_scale: float
    gap_px: int
    max_size_px: int


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

#: JPEG quality for the contact sheet.
PROOF_JPEG_QUALITY = 92

#: Ordering modes that need per-image metadata in-memory panels do not have.
#:
#: ``oldest_first`` / ``newest_first`` sort by file modification time and
#: ``filename`` by path; panels come off the renderer with neither, so
#: :func:`_order_panels` logs once and keeps plate order.
UNSUPPORTED_ORDER_MODES = ("oldest_first", "newest_first")

#: Upstream's spelling of "keep the order the panels were passed in", and the
#: short alias its own ``--order`` help text advertises (which its validator
#: rejects). Normalized by :func:`parse_collage_args`.
ORDER_INPUT_ALIASES = {"input": "input_order"}

#: Flags that set the collage's target aspect ratio.
ASPECT_RATIO_FLAGS = ("-r", "--target-aspect-ratio")

#: Flags that cap the sheet's final longest edge.
MAX_SIZE_FLAGS = ("-S", "--max-collage-size")

#: Ceiling on the sheet's longest edge, in pixels.
#: PyPhotoCollage composites the sheet as RGBA, so a very wide sheet costs
#: ~4 bytes per pixel in memory and CPU; past this the row is squeezed instead
#: and the gutter compensation takes over (see :func:`collage_layout`).
PROOF_SHEET_MAX_SIZE_PX = 12000

#: Safety margin on the computed sheet size, so upstream's rounding of the row
#: width never trips its own ``-S`` rescale.
PROOF_SHEET_SIZE_MARGIN = 1.10

#: Floor on the computed sheet size: upstream's own ``-S`` default.
PROOF_SHEET_MIN_SIZE_PX = 5000

#: Panels per group in the narrowest group a ``nested`` collage builds, and the
#: scale its batches are composited at: upstream's ``min_batch_size_in`` (3) and
#: the 0.75 downscale x 1/2 rows of a recursive batch. Read by
#: :func:`collage_layout` when it prices the gutter for a ``nested`` job.
_MIN_NESTED_GROUP = 3


def row_forcing_aspect(mean_aspect: float, count: int) -> float:
    """``-r`` that puts every panel in a single layout row.

    Upstream picks its row count from a target width of ``(avg_h + avg_w) / 2 *
    sqrt(n * -r)`` (``avg_w = avg_h * mean_aspect``), so with ``n`` equal-height
    panels one row needs ``-r = (2*mean_aspect/(mean_aspect+1))**2 * n**2``.

    Args:
        mean_aspect: Mean ``width / height`` of the panels.
        count: Number of panels in the group being laid out.

    Returns:
        The target aspect ratio that keeps the group in one row (>= 1).
    """
    return ((2.0 * mean_aspect) / (mean_aspect + 1.0)) ** 2 * count * count


def _flags_present(args: str, flags: Sequence[str]) -> bool:
    """Whether an argument string names any of ``flags`` (``-r2.4`` included).

    Args:
        args: A PyPhotoCollage argument string (``"-t rows -r 2.4"``).
        flags: The flags to look for, long form first (``("-S",
            "--max-collage-size")``).

    Returns:
        ``True`` when the shop pinned that option (a glued ``-r2.4`` counts).
    """
    return any(
        token in flags or token.startswith(flags[0]) or token.startswith(flags[1])
        for token in shlex.split(args)
        if token.startswith("-")
    )


def aspect_provided(args: str) -> bool:
    """Whether an argument string names the collage's target aspect ratio.

    Args:
        args: A PyPhotoCollage argument string (``"-t rows -r 2.4"``).

    Returns:
        ``True`` when it carries ``-r``/``--target-aspect-ratio`` (in the
        ``-r2.4`` glued form too), i.e. when the shop pinned the sheet shape.
    """
    return _flags_present(args, ASPECT_RATIO_FLAGS)


def max_size_provided(args: str) -> bool:
    """Whether an argument string caps the sheet's final longest edge.

    Args:
        args: A PyPhotoCollage argument string (``"-t rows -S 8000"``).

    Returns:
        ``True`` when it carries ``-S``/``--max-collage-size``, i.e. when the
        shop pinned the sheet size and the adapter must not compute one.
    """
    return _flags_present(args, MAX_SIZE_FLAGS)


def panel_aspect_inches(box: Optional[transforms.BboxBase]) -> Optional[float]:
    """Aspect ratio of a figure's drawn content box.

    The box is measured in display (pixel) coordinates at the raster dpi; the
    dpi cancels in the ratio, so a degenerate (zero-height) box yields ``None``
    rather than an infinity.

    Args:
        box: The box from :func:`~plt_optimizer.diagnostics.plotter.tight_content_box`.

    Returns:
        ``width / height`` of the drawn content, or ``None`` for a missing or
        degenerate box.
    """
    if box is None:
        return None
    width = box.x1 - box.x0
    height = box.y1 - box.y0
    if width <= 0 or height <= 0:
        return None
    return width / height


def collage_layout(
    opts: argparse.Namespace,
    aspects: Sequence[float],
    *,
    mean_height_px: float = 1.0,
    pinned_aspect: Optional[float] = None,
    pinned_max_size: bool = False,
) -> CollageLayout:
    """The sheet geometry that keeps every panel inside its own frame.

    Upstream's ``rows`` / ``columns`` layouts choose a row count from
    ``-r``, size every row to a common height (gap = ``-g``) and then
    **resample every row to the width of the narrowest one** -- and rows are not
    equal: a row of one panel is ``H * aspect`` wide, a row of two is
    ``2 * H * aspect + gap``, so a 2-row layout squeezes the wide rows to ~1/2
    scale. Measured on the 3-plate CES job, the sheet's first ``-r`` values put
    the row resample at 0.78x and the reciprocal-aspect one at 0.24x: a 12 px
    gutter landed at 3 px, panels touched, and the resample bled their edges
    together -- the collapse that made the first collage unreadable.

    So ``-r`` is not a sheet shape here, it is the **row-count lever**, and the
    only collapse-free layout is one row of panels, all at scale 1.0. Hence:

    * ``aspect`` is :func:`row_forcing_aspect`, the ``-r`` that lands the
      partition on a single row (the same math drives ``columns``, and
      ``nested`` takes its top-level layout there too),
    * ``max_size_px`` is sized to the sheet that row makes (its width, and its
      height for the 1-row layout, which upstream caps by height), so its final
      ``-S`` resize is a no-op and the gutter lands at ``-g``, and
    * the gap is requested ``1 / row_scale`` larger, so a squeezed layout (a
      shop-pinned ``-r`` or ``-S``, a ``nested`` job's 3-panel batches, which
      upstream composites at half scale and downscales by 0.75) still gets
      readable gutters. The gutter widens the row it lands in, so that scale is
      solved as a fixpoint.

    Args:
        opts: Parsed collage options.
        aspects: ``width / height`` of every panel that reached the layout.
        mean_height_px: Mean panel height in pixels **after** the ``-i`` resize
            (upstream sizes the sheet from it).
        pinned_aspect: The shop's own ``-r``, which wins over the computed one
            (and whose resample the gutter is then compensated for).
        pinned_max_size: ``True`` when the shop passed ``-S``, which then wins
            over the computed sheet size.

    Returns:
        The layout: ``aspect`` for ``-r``, the expected row resample factor,
        ``gap_px`` to request from ``-g`` so the gutter lands at
        ``opts.imagegap``, and ``max_size_px`` for ``-S``.
    """
    requested_gap = int(opts.imagegap)
    # A shop `-S` is honoured exactly, floor included (it is how you ask for a
    # deliberately small sheet); without it, the sheet is sized to its own row.
    size = int(opts.max_size) if pinned_max_size else PROOF_SHEET_MIN_SIZE_PX
    if not aspects:
        return CollageLayout(1.0, 1.0, requested_gap, size)
    mean = sum(aspects) / len(aspects)
    height = float(mean_height_px) if mean_height_px > 0 else 1.0
    if mean <= 0.0 or not math.isfinite(mean):
        return CollageLayout(1.0, 1.0, requested_gap, size)
    count = len(aspects)
    target = row_forcing_aspect(mean, count)
    if pinned_aspect is not None and math.isfinite(pinned_aspect) and pinned_aspect > 0.0:
        target = float(pinned_aspect)
    # Upstream's own decisions, replayed. The row count comes from a target row
    # width of `(avg_h + avg_w) / 2 * sqrt(n * -r)` (computed from gap-free panel
    # widths); the panels are then resized to a common height
    # `min(H, -S / rows, -S / cols / aspect)`, every row is resampled to the width
    # of the NARROWEST row, rows are stacked, and the CLI finally resizes the
    # sheet to `-S`. So with `k` panels per (narrowest) row of aspect `ar`:
    # width `k * H' * ar + (k - 1) * g`, height `rows * H' + (rows - 1) * g`, and
    # a panel (and gutter) scale of `H' / H` at the layout stage.
    target_width = (height + height * mean) / 2.0 * math.sqrt(count * target)
    rows = max(1, min(count, int(round(count * mean * height / target_width))))
    cols = -(-count // rows)
    # Upstream splits `count` panels over `rows` rows; the sheet's width is the
    # narrowest row's, so price the layout with `count // rows` panels.
    row_panels = max(1, min(count, count // rows))
    # What the gutters are actually multiplied by on the way to the sheet. A
    # `nested` job that recurses (>= 10 panels, several rows) composites
    # upstream's 3-panel batches, and it does so at half scale (0.75 downscale x
    # 1/2 rows), so its gutters pay for that too.
    batch = 0.5 if (opts.collage_type == "nested" and rows > 1 and count >= 10) else 1.0

    def long_edge(gap: int, panel_scale: float) -> float:
        """The sheet's longest edge for a gutter and a panel scale."""
        # A 1-row layout leaves the panels at their own height; a multi-row
        # layout resizes them to `min(H, -S / rows, -S / cols / aspect)`.
        width = row_panels * height * mean * panel_scale + (row_panels - 1) * gap
        sheet_height = rows * height * panel_scale + (rows - 1) * gap
        return max(width, sheet_height)

    def panel_scale_at(scale_limit: float) -> float:
        """The scale upstream's common-height resize gives panels at `-S`."""
        if rows == 1:
            return min(1.0, scale_limit / (count * mean * height))
        return min(1.0, scale_limit / (height * max(rows, cols / mean)))

    if pinned_max_size:
        # A shop `-S` is honoured exactly, floor included (it is how you ask for
        # a deliberately small sheet); its squeeze is paid for below.
        limit = float(size)
    else:
        # Size the sheet to the layout it produces at full panel size, plus a
        # margin, so the resize to -S is as close to a no-op as the ceiling
        # allows. The sheet's longest edge is what -S caps, and for a 1-row
        # layout that edge is its HEIGHT, not its width.
        limit = max(
            float(PROOF_SHEET_MIN_SIZE_PX),
            min(
                float(PROOF_SHEET_MAX_SIZE_PX),
                PROOF_SHEET_SIZE_MARGIN * long_edge(requested_gap, 1.0),
            ),
        )
        size = int(limit)
    # The requested gutter widens the row (and heightens the sheet) it lands in,
    # so the landing scale - and the gutter that lands at `requested_gap` - is a
    # fixpoint. The inflation is capped: a hard-squeezed sheet is a small sheet,
    # and a 12 px gutter asked as 200 px would dominate it.
    gap_px = requested_gap
    landing = 1.0
    for _ in range(3):
        landing = min(1.0, limit / long_edge(gap_px, panel_scale_at(limit)))
        effective = landing * batch
        gap_px = max(1, min(160, int(round(requested_gap / min(1.0, effective)))))
    return CollageLayout(target, landing * batch, gap_px, size)


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
    """Label of one plate's panel: plate number, material size and material.

    Reads ``"Plate 1 — 23.0 x 16.0 in — ACM"``. Nothing is drawn from it on the
    sheet (the panels carry no captions); it names the panel in the log, orders
    panels under ``-O filename``, and is what a shop reads out of the layout when
    tuning ``proof_collage_args``. The plate number leads so an operator scanning
    the log matches panels to plates (and to the ``p01``-tagged PLT folders)
    without reading the rest. Parts that are unknown are dropped, so a stubbed
    layout still labels as ``"Plate 1"``.

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
    opts = _make_arg_parser().parse_args(tokens)
    # The help text advertises `-O input`, the validator wants `input_order`; a
    # shop copying the documented spelling gets a traceback at the end of a run.
    opts.order = ORDER_INPUT_ALIASES.get(opts.order, opts.order)
    return opts


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
    panel_jpeg_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Collage the whole-sheet plate proofs into one contact-sheet JPEG.

    Each panel is rasterized from ``fig`` cropped to the figure's drawn content,
    laid out by the vendored PyPhotoCollage engine under the parsed ``args``,
    framed, and written as an RGB JPEG at ``output_path``. Nothing is drawn on
    the panels and no title band is added: the sheet is the previews themselves,
    so a panel's aspect ratio is exactly its plate's drawn content (see
    :func:`collage_layout`). Panels that fail to rasterize are skipped with a
    warning; a panel is never stretched, and plate order is the default.

    Args:
        panels: ``(label, figure)`` per plate, in plate order. The label names
            the panel in the log (and sorts it under ``-O filename``); nothing is
            drawn from it.
        output_path: Where to write the JPEG (parent directories are created).
        args: Extra PyPhotoCollage command-line arguments (``"-t nested -r
            1.2"``), applied on top of :data:`PROOF_COLLAGE_DEFAULT_ARGS`.
        dpi: Raster resolution of the panels, dots per inch of material.
        margin_px: White frame around the finished sheet.
        panel_jpeg_dir: Directory to write each panel's JPEG (the exact images
            handed to the engine, so a panel's aspect ratio is its plate's
            drawn content) into before the layout. Its `plate*.jpeg` files are
            removed on the way out (the directory itself only when this call
            created it); ``None`` (the default) writes no panel files.

    Returns:
        The path written, or ``None`` when there was nothing to compose — no
        panels, every panel failed, or matplotlib is unavailable.

    Raises:
        OSError: The JPEG could not be written (the caller degrades).
        SystemExit: ``args`` is not valid for the collage's argument parser.
    """
    if not PLOTTER_AVAILABLE or figure_to_image is None or tight_content_box is None:
        logger.warning("matplotlib is unavailable; skipping the composite proof sheet")
        return None
    if not panels:
        logger.warning("No plate proofs to compose; skipping the composite proof sheet")
        return None

    opts = parse_collage_args(args)
    captioned: List[Tuple[str, Image.Image]] = []
    aspects: List[float] = []
    # The panels are written as JPEGs first: the collage engine reads its images
    # from disk, so the files handed to it are the exact tight-cropped previews
    # whose aspect ratios drive the layout. The directory is scratch - the JPEGs
    # are removed on the way out, and the directory itself only when this call
    # is the one that created it.
    scratch = Path(panel_jpeg_dir) if panel_jpeg_dir is not None else None
    made_scratch = False
    try:
        if scratch is not None:
            made_scratch = not scratch.is_dir()
            scratch.mkdir(parents=True, exist_ok=True)
        for index, (caption, fig) in enumerate(panels, start=1):
            try:
                # Cropped to the drawn content: the panel's aspect ratio is the
                # plate's toolpath aspect ratio, which is what the collage's
                # row layout needs to leave every panel inside its frame.
                raster = figure_to_image(fig, dpi=dpi, crop_tight=True)
                aspect = panel_aspect_inches(tight_content_box(fig, dpi))
            except (PlotterError, RuntimeError, OSError, ValueError) as exc:
                logger.warning("Skipping uncapturable proof panel %s: %s", caption, exc)
                continue
            if scratch is not None:
                _write_panel_jpeg(raster, scratch / f"plate{index:02d}.jpeg")
            captioned.append((caption, raster.convert("RGBA")))
            aspects.append(aspect if aspect is not None else raster.width / raster.height)
    finally:
        if scratch is not None:
            for leftover in sorted(scratch.glob("plate*.jpeg")):
                leftover.unlink(missing_ok=True)
            if made_scratch:
                shutil.rmtree(scratch, ignore_errors=True)
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
    # Seeded again: `nested`, and `--rotate-images-max_deg`, draw from the global
    # generator during layout, and the sheet must be reproducible.
    random.seed(PROOF_DETERMINISM_SEED)
    layout = collage_layout(
        opts,
        aspects,
        mean_height_px=sum(image.height for image in images) / len(images),
        pinned_aspect=float(opts.target_aspect_ratio) if aspect_provided(args) else None,
        pinned_max_size=max_size_provided(args),
    )
    sheet = _make_collage(
        images,
        collage_type=opts.collage_type,
        max_recursion_depth=opts.max_recursion_depth,
        spacing=layout.gap_px,
        no_antialias=not opts.noantialias,
        background=tuple(int(c) for c in opts.background),
        target_aspect_ratio=layout.aspect,
        max_collage_size=layout.max_size_px,
        round_image_corners_perc=float(opts.round_image_corners_perc),
        round_collage_corners_perc=float(opts.round_collage_corners_perc),
        rotate_images_max_deg=int(opts.rotate_images_max_deg),
        show_recursion_depth=bool(opts.show_recursion_depth),
    )

    sheet = _framed(sheet, margin_px)
    # Upstream's CLI closes with `resize_img_to_max_size(collage, -S)`; the
    # library entry point we call does not, so honour `-S` the same way. The
    # helper caps by width when the width exceeds -S and by height otherwise, so
    # the sheet's longest edge lands at -S - including for a 1-row sheet, whose
    # width a height cap leaves unbounded.
    if layout.max_size_px > 0:
        sheet = _resize_to_max(sheet, layout.max_size_px, no_antialias=not opts.noantialias)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.convert("RGB").save(output_path, "JPEG", quality=PROOF_JPEG_QUALITY, optimize=True)
    logger.info(
        "Wrote composite proof sheet %s (%dx%d px, %d plates, collage type %s, "
        "-r %.3f, -S %d, row scale %.2f, gutter %d px requested)",
        output_path,
        sheet.width,
        sheet.height,
        len(images),
        opts.collage_type,
        layout.aspect,
        layout.max_size_px,
        layout.row_scale,
        layout.gap_px,
    )
    return output_path


def _write_panel_jpeg(raster: Image.Image, path: Path) -> None:
    """Write one panel raster as an RGB JPEG (the collage engine reads files).

    Args:
        raster: The panel image, cropped to the plate's drawn content.
        path: Where to write it.

    Returns:
        Nothing.
    """
    raster.convert("RGB").save(path, "JPEG", quality=PROOF_JPEG_QUALITY, optimize=True)
