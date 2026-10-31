"""Simple grid-fill layout engine (no ``rectpack``) for ``*-simple`` modes.

``layout: columns-simple`` / ``layout: rows-simple`` reproduce the VisionPro
"Multiples" (EngraveLab "Badges") behavior: labels are placed one at a time
in strict content order on a flush grid, never rotated, with no bin-packing
optimization. ``columns-simple`` fills each column top-to-bottom before
extending rightward; ``rows-simple`` fills each row left-to-right before
extending downward.

The only "optimization" is the overlap guard: a column spans the widest
label placed in it (a row spans the tallest), fixed for the column's whole
length, so a column mixing 1x3 and 1x4 labels starts the next column at
x=4 for every label in it. The first label that no longer fits the current
column (resp. row) opens a new one; a label that no longer fits the plate
opens the next plate (leftover entries surface through the caller's
:class:`~plt_optimizer.generate.packing.layout.LayoutFitError` paths).

Round labels are exempt from this engine: :mod:`plt_optimizer.generate.packing.layout`
routes them to the ellipse pipeline regardless of the fill-order mode.

The engine consumes the same ``_RectEntry`` tuples the ``rectpack`` passes
build (real space, no transposition) and emits the same
:class:`~plt_optimizer.generate.packing.layout.PackedPlate` /
``PackedLabel`` objects, so plate assembly, pinning, material partitioning
and clearance shifting are unchanged downstream.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

from plt_optimizer.generate.packing.layout import (
    PackedLabel,
    PackedPlate,
    _RectEntry,
)
from plt_optimizer.generate.schema import LayoutMode

# Float-noise tolerance for fit comparisons, inches. Placements accumulate
# cursor offsets, so an exact stack of e.g. 16 x 1.0in labels can sum to
# 16.000000000000002; a strict ">" would then open a phantom column.
_FIT_EPS: float = 1e-9


def _place(
    plate: PackedPlate,
    entry: _RectEntry,
    x: float,
    y: float,
    left_clearance: float,
    top_clearance: float,
) -> None:
    """Append one unrotated placement to a plate.

    Args:
        plate: The plate receiving the label.
        entry: The ``(width, height, rid)`` tuple being placed.
        x: Usable-area x offset of the label's top-left corner, inches.
        y: Usable-area y offset (y-down device frame), inches.
        left_clearance: Plate left-edge clearance added to every placement.
        top_clearance: Plate top-edge clearance added to every placement.
    """
    width, height, (rect_id, source_label, _pack_width, _seq) = entry
    plate.labels.append(
        PackedLabel(
            label_id=rect_id,
            x=x + left_clearance,
            y=y + top_clearance,
            width=width,
            height=height,
            rotated=False,
            source_label=source_label,
        )
    )


def pack_simple_grid(
    entries: Sequence[_RectEntry],
    bin_specs: Sequence[tuple[float, float, str]],
    layout: LayoutMode,
    clearances: Optional[Mapping[str, tuple[float, float]]] = None,
    material: Optional[str] = None,
    materials: Optional[Mapping[str, Optional[str]]] = None,
) -> list[PackedPlate]:
    """Grid-fill entries onto plates in strict content order.

    Mirrors the contract of
    :func:`~plt_optimizer.generate.packing.layout._pack_group`: one
    declaration-ordered group of same-mode plates is filled greedily;
    labels that no plate can host are left unpacked (the caller reports
    the leftover). Coordinates are the packer-space usable-area frame
    (origin at the usable area's top-left, y-down), shifted by each
    plate's clearances exactly like ``_extract_packed_plates``.

    Args:
        entries: Real-space ``(pack_width, pack_height, rid)`` tuples in
            content order.
        bin_specs: ``(width, height, bid)`` plates sharing this simple
            fill mode, declaration order.
        layout: :attr:`LayoutMode.COLUMNS_SIMPLE` (fill each column
            top-to-bottom, then rightward) or :attr:`LayoutMode.ROWS_SIMPLE`
            (fill each row left-to-right, then downward).
        clearances: Optional bin-id to ``(left_clearance,
            top_clearance)`` mapping (see
            :func:`~plt_optimizer.generate.packing.layout._plate_clearances`).
        material: Stock material stamped onto every produced plate.
        materials: Optional bin-id to declared-material-spelling mapping
            (falls back to ``material``).

    Returns:
        The non-empty ``PackedPlate`` objects, in bin declaration order,
        each carrying its placements in content order (which is the
        reader's fill order in the device frame).

    Raises:
        ValueError: If ``layout`` is not a simple mode.
    """
    if layout not in (LayoutMode.COLUMNS_SIMPLE, LayoutMode.ROWS_SIMPLE):
        raise ValueError(f"not a simple layout mode: {layout!r}")
    column_major = layout is LayoutMode.COLUMNS_SIMPLE

    clearance_map: Mapping[str, tuple[float, float]] = clearances or {}
    material_map: Mapping[str, Optional[str]] = materials or {}
    remaining: list[_RectEntry] = list(entries)
    plates: list[PackedPlate] = []

    for plate_width, plate_height, bin_id in bin_specs:
        if not remaining:
            break
        left_clearance, top_clearance = clearance_map.get(bin_id, (0.0, 0.0))
        plate = PackedPlate(
            plate_id=bin_id,
            width=plate_width,
            height=plate_height,
            left_clearance=left_clearance,
            top_clearance=top_clearance,
            material=material_map.get(bin_id, material),
        )

        # `main` is the cross-axis start of the current band (column x for
        # columns mode, row y for rows mode); `secondary` is the cursor
        # along the band (y within the column, x within the row); `extent`
        # is the widest (columns) / tallest (rows) label placed in the
        # current band, fixing its size for the whole band.
        main = 0.0
        secondary = 0.0
        extent = 0.0

        index = 0
        while index < len(remaining):
            width, height, _rid = remaining[index]
            # A label larger than the usable area never fits this plate.
            # Stop the plate here (do NOT skip ahead): content order is
            # sacred in the simple modes, so this label and every later
            # one cascade to the next plate (and finally to the caller's
            # leftover / LayoutFitError path).
            if width > plate_width + _FIT_EPS or height > plate_height + _FIT_EPS:
                break

            # `band_size` is the label's extent along the fill (secondary)
            # axis -- its height in columns mode, its width in rows mode;
            # `cross_size` is its extent along the band-start (main) axis
            # -- the width that fixes a column's size, the height that
            # fixes a row's size.
            if column_major:
                band_size, band_limit = height, plate_height
                cross_size, cross_limit = width, plate_width
            else:
                band_size, band_limit = width, plate_width
                cross_size, cross_limit = height, plate_height

            if secondary + band_size > band_limit + _FIT_EPS:
                # The label no longer fits the current band: close it
                # (fixed at the widest/tallest label it received) and
                # start the next one.
                main += extent
                secondary = 0.0
                extent = 0.0
            if main + cross_size > cross_limit + _FIT_EPS:
                # The next band itself would overflow the plate: this
                # plate is full, remaining labels cascade to the next.
                break

            x, y = (main, secondary) if column_major else (secondary, main)
            _place(plate, remaining[index], x, y, left_clearance, top_clearance)
            secondary += band_size
            extent = max(extent, cross_size)
            del remaining[index]

        if plate.labels:
            plates.append(plate)

    return plates
