"""Composite proof sheet: every plate's combined preview on one JPEG.

The generate pipeline renders one combined ``all_*`` proof per plate (see
:func:`plt_optimizer.generate.vectorize.export_per_cutter_plts`) and writes
each as its own PDF in ``pdf/``. A multi-plate job therefore means opening
several files to sign a job off, which is exactly what the shop's proof
approval is not. This module stitches those renders into a **single contact
sheet**: the :class:`PIL.Image.Image` of every whole-sheet proof, tiled into a
grid of at most :data:`PROOF_PANEL_COLUMNS` panels per row, flattened onto
white and written as ``<output_dir>/<job_id>_proof.jpeg``.

The module is deliberately matplotlib-free (PIL only, which is a hard
dependency) so the geometry is unit-testable headless and the code stays
importable on the Python 3.8 / Windows 7 target (root §5). The rasterization
of a matplotlib figure into the panels this module consumes lives in
:func:`plt_optimizer.diagnostics.plotter.figure_to_image` (matplotlib side,
import-guarded) and the two meet in
:func:`plt_optimizer.generate.vectorize._write_simple_plots`.

Panels keep their own figure titles (``"<job> (plate 2)"``), so the sheet is
self-describing and needs no captions of its own.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import List, Sequence, Tuple

from PIL import Image

#: Extension of the composite proof sheet (JPEG, per the proof contract).
PROOF_IMAGE_SUFFIX = ".jpeg"

#: Panels per row. Two-up keeps a plate proof readable while keeping a
#: multi-sheet jobset a few rows tall instead of one unreadably wide strip.
PROOF_PANEL_COLUMNS = 2

#: Raster resolution each proof figure is rendered at (dots per inch) so the
#: sheet stays a quick-look proof; the per-plate PDFs stay the zoomable,
#: vector-quality artifact.
PROOF_PANEL_DPI = 100

#: White space (pixels) between neighbouring panels.
PROOF_PANEL_GUTTER_PX = 12

#: White space (pixels) framing the whole sheet.
PROOF_SHEET_MARGIN_PX = 12

#: JPEG quality, matching :func:`plt_optimizer.diagnostics.plotter.save_figure`.
PROOF_JPEG_QUALITY = 92

#: Sheet background. JPEG carries no alpha, and the panels' own page colour is
#: the configured proof background, so the frame is plain white.
PROOF_SHEET_BACKGROUND = (255, 255, 255)

#: Long-side cap of the finished sheet, in pixels. Panels are rasterized at
#: :data:`PROOF_PANEL_DPI`, so a jobset of 48x24in plates would otherwise tile
#: into a ~9600x4900 pixel (47 MP) JPEG: appropriate for a plate, absurd for a
#: quick-look proof. The panel grid is scaled down (aspect preserved) until it
#: fits inside ``max_long_side_px`` minus the frame, so the sheet's long side
#: never exceeds the cap. ``0`` disables the cap.
PROOF_SHEET_MAX_LONG_SIDE_PX = 4096


def proof_image_name(job_id: str) -> str:
    """Name of a job's composite proof sheet.

    Args:
        job_id: The sanitized job id (``job.name`` for a single-job run, the
            batch id for a jobset) used in every generated file name.

    Returns:
        ``"<job_id>_proof.jpeg"``.
    """
    return f"{job_id}_proof{PROOF_IMAGE_SUFFIX}"


def proof_sheet_layout(
    panel_count: int, columns: int = PROOF_PANEL_COLUMNS
) -> List[Tuple[int, int]]:
    """Grid cell (row, column) of every panel, in plate order.

    Panels flow left to right, top to bottom, so a 3-panel run fills
    ``(0,0) (0,1) (1,0)``: a partly filled last row stays left-aligned in its
    columns instead of being centred, which keeps every column on one vertical
    line.

    Args:
        panel_count: Number of panels on the sheet (must be positive).
        columns: Panels per row (must be positive).

    Returns:
        One ``(row, column)`` pair per panel, in input order.

    Raises:
        ValueError: If ``panel_count`` or ``columns`` is not positive.
    """
    if panel_count <= 0:
        raise ValueError(f"panel_count must be positive, got {panel_count}")
    if columns <= 0:
        raise ValueError(f"columns must be positive, got {columns}")
    return [(index // columns, index % columns) for index in range(panel_count)]


def _fit_size(panel: Image.Image, column_width: int) -> Tuple[int, int]:
    """Size at which ``panel`` is drawn inside a ``column_width`` column.

    Args:
        panel: The rendered proof image.
        column_width: The column width every panel is fitted to.

    Returns:
        The ``(width, height)`` to draw the panel at. Panels are only ever
        scaled *down* to the column width: upscaling a raster turns a
        quick-look proof to mush.
    """
    if panel.width <= column_width:
        return panel.width, panel.height
    return column_width, max(1, round(panel.height * column_width / panel.width))


def _row_heights(sizes: Sequence[Tuple[int, int]], cells: Sequence[Tuple[int, int]]) -> List[int]:
    """Tallest drawn panel per grid row (panels are centred in their cell).

    Args:
        sizes: The drawn size of every panel, in panel order.
        cells: The grid cell of every panel, in panel order.

    Returns:
        One row height per grid row.
    """
    rows = max(row for row, _ in cells) + 1
    heights = [0] * rows
    for (row, _), (_, height) in zip(cells, sizes):
        heights[row] = max(heights[row], height)
    return heights


def compose_proof_sheet(
    panels: Sequence[Image.Image],
    output_path: Path,
    columns: int = PROOF_PANEL_COLUMNS,
    gutter_px: int = PROOF_PANEL_GUTTER_PX,
    margin_px: int = PROOF_SHEET_MARGIN_PX,
    quality: int = PROOF_JPEG_QUALITY,
    background: Tuple[int, int, int] = PROOF_SHEET_BACKGROUND,
    max_long_side_px: int = PROOF_SHEET_MAX_LONG_SIDE_PX,
) -> Path:
    """Tile rendered plate proofs into one JPEG proof sheet.

    Every panel is scaled down (aspect preserved) to the width of the narrowest
    panel, so mixed plate formats line up on one column grid, then centred in
    its cell. Nothing is upscaled: a sheet of small renders stays small rather
    than turning into mush. A run of fewer panels than ``columns`` uses only
    the columns it needs, so the usual single-plate sheet is exactly one panel
    wide.

    Args:
        panels: The rendered proof images, in plate order (one per combined
            ``all_*`` preview). Must not be empty.
        output_path: Where to write the sheet (``.jpeg`` by convention; the
            encoder is forced to JPEG, so the extension names the file and does
            not select the format). Parent directories are created.
        columns: Panels per row.
        gutter_px: White pixels between neighbouring panels.
        margin_px: White pixels framing the sheet.
        quality: JPEG encoder quality.
        background: Sheet background colour (RGB).
        max_long_side_px: Long-side cap of the finished sheet in pixels (the
            panel grid is scaled to fit inside it); ``0`` writes the sheet at
            native panel size.

    Returns:
        ``output_path`` (the written sheet).

    Raises:
        ValueError: If ``panels`` is empty.
        OSError: If the sheet cannot be written (unwritable path). The caller
            logs and continues: a proof sheet is a reporting artifact and must
            never fail a good job.
    """
    if not panels:
        raise ValueError("Cannot compose a proof sheet from zero panels")

    # A 1-panel run is the normal case (a single-plate job): it must not pay
    # for an empty second column.
    used_columns = min(columns, len(panels))
    cells = proof_sheet_layout(len(panels), used_columns)
    tiles = [panel.convert("RGBA") for panel in panels]
    guttered = (used_columns - 1) * gutter_px

    column_width = min(panel.width for panel in tiles)
    sizes = [_fit_size(panel, column_width) for panel in tiles]
    row_heights = _row_heights(sizes, cells)
    grid_width = used_columns * column_width + guttered
    grid_height = sum(row_heights) + (len(row_heights) - 1) * gutter_px

    if max_long_side_px > 0:
        # The frame is added on top of the grid, so the grid gets what is left
        # of the budget after the margins.
        available = max_long_side_px - 2 * margin_px
        grid_long_side = max(grid_width, grid_height)
        if available > 0 and grid_long_side > available:
            column_width = max(
                1,
                math.floor((available - guttered) * column_width / grid_long_side),
            )
            sizes = [_fit_size(panel, column_width) for panel in tiles]
            row_heights = _row_heights(sizes, cells)
            grid_width = used_columns * column_width + guttered
            grid_height = sum(row_heights) + (len(row_heights) - 1) * gutter_px

    sheet = Image.new("RGB", (grid_width + 2 * margin_px, grid_height + 2 * margin_px), background)

    for (row, column), panel, (width, height) in zip(cells, tiles, sizes):
        if (panel.width, panel.height) != (width, height):
            panel = panel.resize((width, height), Image.Resampling.LANCZOS)
        x = margin_px + column * (column_width + gutter_px) + (column_width - width) // 2
        y = margin_px + sum(row_heights[:row]) + row * gutter_px + (row_heights[row] - height) // 2
        # The panel's own alpha is the mask, so transparent page margins read
        # as the sheet background instead of black.
        sheet.paste(panel, (x, y), panel)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, format="JPEG", quality=quality)
    return output_path
