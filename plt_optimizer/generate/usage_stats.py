r"""Job usage metrics for the ``generate`` pipeline's production report.

The generation report (:mod:`plt_optimizer.generate.generation_report`) answers
*what the pipeline decided* (compression, cutter downsizes, warnings). This
module answers the shop-floor question the same run leaves behind: *what did the
job consume* -- how many labels, characters, drill holes, how much material, and
how far the tool actually travelled.

One entry point, :func:`build_usage_stats`, turns the objects the export already
leaves behind into the ``usage`` block of ``report.json`` (and the ``Job usage``
section of ``summary.txt``). It measures, it does not model: nothing here is
emitted, so generated PLT/PDF bytes stay byte-identical.

Metric sources
--------------

* **Counts** (labels, characters, holes, cutters, sizes, materials) come from
  the resolved specs -- design intent, so every multi-up copy is counted --
  together with the packed placements and the per-line scale factors.
* **Material** is the plate area consumed: the plate's usable size plus its
  clearances, i.e. the sheet the shop cuts, grouped by the plate's stock
  material. The labels' nominal (spec ``width x height``) area is reported
  alongside; the gap between the two is the packer's waste, and for round
  labels the corners the packer cannot use.
* **Weight is estimated, not measured**: each label instance's nominal area
  times the ``material_weight_per_square_inch`` (lb/in\u00b2) of the plate it sits
  on, so it answers "what do the labels I just engraved weigh". The density is
  opt-in (``job-config.json`` default -> job -> plate); unconfigured it reports
  ``available: false`` with ``null`` numbers, never a zero weight.
* **Tool distance is measured from the emitted per-cutter PLT files** with the
  production HPGL parser, so it is exact machine truth: post-optimization rapid
  travel, relief passes, borders, and drill holes all included. Plan-derived
  distances would report pre-optimization air travel (overstated) and miss the
  relief passes, which are injected into the plate content *after* the render
  and pack stages.

Per-label attribution
---------------------

The packer records every placement as a ``PackedLabel`` rect in inches, and
:func:`~plt_optimizer.generate.vectorize.assemble_plt_from_rendered_labels`
lands that label on the plate with a plain **additive** translate of the
placement offset - no mirror, no Y flip (``[x, x + H] x [y, y + W]`` for a
sideways placement) - so a placement's slot is exactly
``[x, x + width] x [y, y + height]`` in the same device plotter-unit frame the
written files use. Rotation, the plate origin shift and the plate's Y direction
are applied by production code; nothing here re-derives a coordinate transform.

Distance is attributed **per segment**, not per path: the sweep and merge
optimizers routinely join contours that were laid out apart, so one parsed path
can carry geometry belonging to several labels, and a label whose content
overruns its reserved box straddles its neighbours. A cutting segment follows
the slot containing its midpoint, a rapid lead follows its destination (that is
the label the tool is travelling TO), and the inter-path gap the point the tool
arrived at. A segment no slot explains is reported under ``unattributed`` --
never silently dropped - so the per-label rows always sum to the file totals.

The border/hole split of a ``bh`` file comes from the combined content's pen
layers (:data:`LAYER_BOUNDARY` / :data:`LAYER_HOLES`), keyed by stroke
signature, because the per-cutter files carry no ``SP`` pen selects.

Determinism
-----------

Every bucket list is ordered by count (descending) then key (ascending), and
every float is rounded to :data:`USAGE_FLOAT_DECIMALS`, so two identical runs
produce byte-comparable JSON.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from plt_optimizer.core.models import (
    ArcSegment,
    PLTDocument,
    Segment,
    StrokePath,
)
from plt_optimizer.core.parser import PLTParser
from plt_optimizer.core.units import inches_to_units, units_to_inches
from plt_optimizer.generate.resolution import ResolvedLabel, hole_engraved_radius
from plt_optimizer.generate.tool_options import resolve_tool_option_values
from plt_optimizer.generate.vectorize import (
    LAYER_BOUNDARY,
    LAYER_HOLES,
    _cutter_token_to_inches,
    _format_material_tag,
    _parse_plt_stem,
    extract_pens_from_plt_text,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Public constants (part of the report's contract)
# --------------------------------------------------------------------------

#: Number of decimals every float in the ``usage`` block is rounded to. Keeps
#: two identical runs byte-comparable (mirrors the report's ``FLOAT_DECIMALS``).
USAGE_FLOAT_DECIMALS: int = 6

#: Square inches per square foot.
SQ_IN_PER_SQ_FT: float = 144.0

#: Ounces per pound (the weight block reports both units).
OZ_PER_LB: float = 16.0

#: Inches per foot (distances are reported in inches with a foot conversion).
IN_PER_FT: float = 12.0

#: Stroke classes. Text-layer strokes of a relief label are
#: :data:`RELIEF_CLASS` because the relief passes share the text cutter's pen
#: layer with plain text layers -- the owner label is what tells them apart.
TEXT_CLASS: str = "text"
RELIEF_CLASS: str = "relief"
BORDER_CLASS: str = "borders"
HOLE_CLASS: str = "holes"
UNATTRIBUTED_CLASS: str = "unattributed"

#: ``usage.by_job`` row key for a file no jobset member owns. A jobset names
#: every file's owner, so this only appears when ownership is incomplete - and
#: it keeps ``sum(by_job) == distance.total`` an identity instead of a leak.
UNATTRIBUTED_JOB: str = "unattributed"

#: Attribution id of the toolpath that engraves the **sheet** - a plate border
#: or a registration mark drawn outside every placement slot. Like a real
#: placement it is a key of the ``job_of_label`` side table, whose plate-frame
#: entry names the member that physically produced it (the owner of plate 1).
#: Charging it there instead of to :data:`UNATTRIBUTED_JOB` is what makes a
#: member measured from its own files equal the batch's row for that member:
#: the plate-level travel is present in whichever member's file the emitter
#: wrote it, and the per-job split must not depend on that accident.
PLATE_FRAME_LABEL: str = "__plate_frame__"

#: Padding of a plate-frame contour's box, in inches. The attribution point is
#: the cutting segment's midpoint, which lies on the contour; the padding only
#: has to absorb the cutter radius and the plate's own inset.

#: Every stroke class, in report order. ``by_class`` always carries all of
#: them: a legitimate zero is information, a missing bucket is a bug.
DISTANCE_CLASSES: Tuple[str, ...] = (
    TEXT_CLASS,
    RELIEF_CLASS,
    BORDER_CLASS,
    HOLE_CLASS,
    UNATTRIBUTED_CLASS,
)

PLATE_FRAME_PAD_IN: float = 0.05

#: Per-cutter file layer kinds (the ``_<kind>_`` token of the file name).
KIND_TEXT: str = "txt"
KIND_STRUCTURAL: str = "bh"

#: Label placement slot padding, in inches. Relief islands, descender relief,
#: and the cutter radius can push a stroke a hair outside the nominal
#: ``width x height`` box; the padding absorbs that without ever reaching a
#: neighbouring slot (the packer keeps a larger gap than this).
SLOT_TOL_IN: float = 0.02

#: How far a segment's measured **point** may sit outside every slot and still
#: be attributed to the nearest one, in inches. A stroke can legitimately
#: overhang (a relief pass covering the whole island, a descender relief, a
#: cutter-radius bleed). Anything further out - a full-plate border, a drill
#: guide, geometry the packer never placed - is charged to
#: :data:`UNATTRIBUTED_CLASS` instead of being silently dumped on the closest
#: label.
SLOT_CENTRE_TOL_IN: float = 0.25

#: Placeholder for "no cutter size recorded" in the cutter buckets (a stem the
#: file-name cap truncated, or a file that does not follow the naming scheme).
UNKNOWN_CUTTER: str = "unknown"

#: Bucket key of a per-cutter file whose plate could not be resolved.
UNRESOLVED_PLATE: int = -1

#: Maximum number of per-label distance rows listed in ``summary.txt``
#: before the list is truncated with a pointer to ``report.json`` (mirrors
#: ``generation_report.SUMMARY_FINDING_LIMIT``).
SUMMARY_USAGE_LABEL_LIMIT: int = 50

#: Rendered for a metric that is legitimately absent (``None`` in JSON).
MISSING_TEXT: str = "n/a"

#: The five operation types the run-time estimate is decomposed into. Every
#: ``time`` bucket row carries one entry per name, in this order.
TIME_OPERATIONS: Tuple[str, ...] = ("cutting", "rapid", "plunge", "z_up", "dwell")

#: Registry key of the per-layer XY cutting feed (``VS``), in inches/sec. The
#: time model needs the same names the shop's ``tool_options`` registry uses
#: (see docs/TODO.md), so they are constants: a shop that renamed a key edits
#: them here and the estimate degrades to "not estimable" with a log line.
CUTTING_VELOCITY_KEY: str = "cutting_velocity"
PLUNGE_VELOCITY_KEY: str = "plunge_velocity"
Z_CLEARANCE_KEY: str = "z_clearance"
Z_UP_VELOCITY_KEY: str = "z_up_velocity"
DWELL_TIME_KEY: str = "dwell_time"

#: ``tool_options`` layer of a per-cutter file, keyed by the file name's
#: ``kind`` token - identical to the ``plt_type`` the export passes to
#: :func:`~plt_optimizer.generate.tool_options.generate_tool_option_headers`
#: (``_txt_`` files are text, ``_bh_`` files are borders/holes), so the
#: estimate reads the same dual defaults the file was emitted with.
PLT_TYPE_BY_KIND: Dict[str, str] = {KIND_TEXT: "text", KIND_STRUCTURAL: "borders_holes"}

#: Milliseconds per second, for the ``ms``-unit options (``ZO105``/``ZO124``).
MS_PER_SECOND: float = 1000.0


# --------------------------------------------------------------------------
# Geometry helpers (device plotter units)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Rect:
    """An axis-aligned rectangle in device plotter units.

    Attributes:
        x_min: Left edge.
        y_min: Bottom edge (the PLT Y axis runs up).
        x_max: Right edge.
        y_max: Top edge.
    """

    x_min: float
    y_min: float
    x_max: float
    y_max: float

    @property
    def area(self) -> float:
        """Return the rectangle's area in square plotter units."""
        return max(0.0, self.x_max - self.x_min) * max(0.0, self.y_max - self.y_min)

    @property
    def center(self) -> Tuple[float, float]:
        """Return the rectangle's centre point."""
        return ((self.x_min + self.x_max) / 2.0, (self.y_min + self.y_max) / 2.0)

    def padded(self, amount: float) -> _Rect:
        """Return the rectangle grown by ``amount`` on every edge.

        Args:
            amount: Padding in plotter units.

        Returns:
            The grown rectangle.
        """
        return _Rect(
            x_min=self.x_min - amount,
            y_min=self.y_min - amount,
            x_max=self.x_max + amount,
            y_max=self.y_max + amount,
        )

    def covers(self, other: _Rect) -> bool:
        """Return whether this rectangle fully contains ``other``.

        Args:
            other: The rectangle to test.

        Returns:
            True when ``other`` lies entirely inside this rectangle.
        """
        return (
            self.x_min <= other.x_min
            and self.x_max >= other.x_max
            and self.y_min <= other.y_min
            and self.y_max >= other.y_max
        )

    def pieces_excluding(self, holes: Sequence[_Rect]) -> List[_Rect]:
        """Return this rectangle minus ``holes`` (see :func:`_exclude`)."""
        return _exclude(self, holes)


@dataclass(frozen=True)
class _Slot:
    """One label placement together with its device-unit slot rectangle.

    Attributes:
        label_id: Placement id (what the packer records -- the spec id for a
            single-copy label, the instance id for a multi-up label).
        spec_id: The resolved label's spec id.
        rect: The slot rectangle in device plotter units.
        is_relief: Whether the placed label is relief-engraved.
    """

    label_id: str
    spec_id: str
    rect: _Rect
    is_relief: bool


@dataclass
class _PlateFrame:
    """The attribution index for one plate.

    Attributes:
        plate_number: The 1-based plate number.
        material: The plate's stock material name (``None`` = the plate
            carries no material).
        slots: The placement slots, in packing order.
        stroke_class: Stroke signature to :data:`BORDER_CLASS` /
            :data:`HOLE_CLASS`, used to split a ``bh`` file's travel (the
            per-cutter files carry no ``SP`` pen selects).
        plate_frame: The sheet's furniture band - the padded bounding box of
            every structural contour, with the padded placement slots cut out
            of it. What is left is the strip the plate's own border and
            registration marks are drawn in, outside every label. Travel on it
            is plate-level: it is charged to :data:`PLATE_FRAME_LABEL`, which
            keeps it out of the per-label attribution without leaving the
            totals. It is one band (not one box per contour) because a mark is
            not always inside the contour it touches: the sheet's border is
            engraved 0.01in in from the stock edge while a registration mark's
            tail runs to the edge itself, so a per-contour box would put part
            of the same mark inside the sheet and part of it outside - and a
            point test that answers "is this stroke the sheet's furniture?"
            must not depend on which point of the stroke is asked about.
    """

    plate_number: int
    material: Optional[str] = None
    slots: List[_Slot] = field(default_factory=list)
    stroke_class: Dict[Tuple[Any, ...], str] = field(default_factory=dict)
    plate_frame: List[_Rect] = field(default_factory=list)


def _path_signature(path: StrokePath) -> Tuple[Any, ...]:
    """Return a hashable geometry signature of a stroke path.

    Two paths that draw the same contour carry the same signature, which is how
    a stroke read back from a written per-cutter file is matched to the layer it
    came from in the plate's combined content. The values are produced by the
    same parser on both sides, so they compare exactly.

    Args:
        path: The parsed stroke path.

    Returns:
        The path's geometry signature.
    """
    parts: List[Any] = []
    if path.pen_up_position is not None:
        parts.append(("PU", path.pen_up_position.x, path.pen_up_position.y))
    for seg in path.segments:
        if isinstance(seg, ArcSegment):
            parts.append(
                (
                    "AA",
                    seg.start.x,
                    seg.start.y,
                    seg.center.x,
                    seg.center.y,
                    seg.end.x,
                    seg.end.y,
                    seg.sweep_angle,
                    seg.is_cutting,
                )
            )
        else:
            parts.append(
                (
                    "PD",
                    seg.start.x,
                    seg.start.y,
                    seg.end.x,
                    seg.end.y,
                    seg.is_cutting,
                )
            )
    return tuple(parts)


def _structural_classes(combined: str) -> Dict[Tuple[Any, ...], str]:
    """Split a plate's combined content into border and hole stroke classes.

    Args:
        combined: The plate's combined (all pens) PLT content.

    Returns:
        A mapping of stroke signature to :data:`BORDER_CLASS` /
        :data:`HOLE_CLASS`. The border layer is read last, so a contour drawn in
        both layers keeps its border class (borders are engraved before the
        punches, and a pen-2 rectangle is never a drill hole).
    """
    parser = PLTParser()
    classes: Dict[Tuple[Any, ...], str] = {}
    for path in parser.parse_string(
        extract_pens_from_plt_text(combined, [LAYER_HOLES])
    ).stroke_paths:
        classes[_path_signature(path)] = HOLE_CLASS
    for path in parser.parse_string(
        extract_pens_from_plt_text(combined, [LAYER_BOUNDARY])
    ).stroke_paths:
        classes[_path_signature(path)] = BORDER_CLASS
    return classes


def _exclude(box: _Rect, holes: Sequence[_Rect]) -> List[_Rect]:
    """Split ``box`` into the rectangles that no entry of ``holes`` covers.

    A partition by the holes' edge coordinates: every cell of the grid is
    either fully inside one hole (dropped) or fully outside all of them
    (kept), so the union of the result is exactly ``box`` minus the holes and
    no cell straddles a hole boundary.

    Args:
        box: The rectangle to cut.
        holes: The rectangles to remove from it.

    Returns:
        The axis-aligned pieces of ``box`` that no hole covers.
    """
    xs = {box.x_min, box.x_max}
    ys = {box.y_min, box.y_max}
    for hole in holes:
        for value in (hole.x_min, hole.x_max):
            if box.x_min < value < box.x_max:
                xs.add(value)
        for value in (hole.y_min, hole.y_max):
            if box.y_min < value < box.y_max:
                ys.add(value)
    xs_sorted = sorted(xs)
    ys_sorted = sorted(ys)
    pieces: List[_Rect] = []
    for x_index in range(len(xs_sorted) - 1):
        for y_index in range(len(ys_sorted) - 1):
            cell = _Rect(
                xs_sorted[x_index],
                ys_sorted[y_index],
                xs_sorted[x_index + 1],
                ys_sorted[y_index + 1],
            )
            if cell.area <= 0.0:
                continue
            if any(hole.covers(cell) for hole in holes):
                continue
            pieces.append(cell)
    return pieces


def _plate_frame_rects(
    combined: str,
    stroke_class: Mapping[Tuple[Any, ...], str],
    slots: Sequence[_Slot],
    plate_size: Optional[Tuple[float, float]] = None,
) -> List[_Rect]:
    """Box the structural contours that belong to the sheet, not to a label.

    A plate can carry its own border and registration marks: contours drawn in
    the boundary/hole layers outside every placement slot. They are the plate's
    furniture, so the travel that engraves them belongs to no label - and, in a
    jobset, to no member. Identifying them keeps a member measured from its own
    files on the same footing as the batch's per-member rows.

    The band's outer box is the SHEET, never the content that is being measured
    through it. A jobset member is measured from a slice of the sheet's
    structural content, so a box fitted to that slice stops at the slice: a
    registration mark in a corner no member's slice covers would be furniture
    for the batch view and a mystery stroke for the member, and the member
    reports would stop adding up to the batch (see
    :func:`plt_optimizer.generate.jobset_report.build_job_usage`, which hands
    every member the batch's plate objects for exactly this reason).

    Args:
        combined: The plate's combined (all pens) PLT content.
        stroke_class: The plate's structural signature map (see
            :func:`_structural_classes`).
        slots: The plate's placement slots.
        plate_size: The plate's size in PLOTTER UNITS (see
            :func:`_plate_size_inches`), when the plate object can say. ``None``
            falls back to boxing the content, which is the sheet for a
            full-sheet run and the measured slice for a member slice.

    Returns:
        The pieces of the padded sheet box that no padded slot covers - the
        sheet's furniture band.
    """
    if not stroke_class:
        return []
    parser = PLTParser()
    xs: List[float] = []
    ys: List[float] = []
    for path in parser.parse_string(combined).stroke_paths:
        if _path_signature(path) not in stroke_class:
            continue
        for seg in path.segments:
            if not seg.is_cutting:
                continue
            xs.extend((seg.start.x, seg.end.x))
            ys.extend((seg.start.y, seg.end.y))
    if not xs:
        return []
    pad = inches_to_units(SLOT_TOL_IN)
    slot_boxes = [slot.rect.padded(pad) for slot in slots]
    # The band spans the whole sheet (every structural contour touches it), so
    # a mark that runs from the stock edge into the border it is tied to is
    # furniture in its entirety - see :attr:`_PlateFrame.plate_frame`.
    content = _Rect(min(xs), min(ys), max(xs), max(ys))
    # The plate's own box, origin at (0, 0) like the content's. A plate that
    # cannot say its size contributes nothing, leaving the content box.
    box = content if plate_size is None else _union(content, _Rect(0.0, 0.0, *plate_size))
    # The frame is the furniture strip, never a label's interior: a plate
    # border is one contour whose box spans the whole sheet, so boxing it
    # raw would swallow every text stroke on the plate. Cut the padded
    # slots out of it - only travel outside the placements counts as
    # plate-level.
    return box.padded(inches_to_units(PLATE_FRAME_PAD_IN)).pieces_excluding(slot_boxes)


def _union(left: _Rect, right: _Rect) -> _Rect:
    """Return the smallest axis-aligned box holding both rectangles.

    Args:
        left: First rectangle.
        right: Second rectangle.

    Returns:
        Their union as one box.
    """
    return _Rect(
        min(left.x_min, right.x_min),
        min(left.y_min, right.y_min),
        max(left.x_max, right.x_max),
        max(left.y_max, right.y_max),
    )


def _plate_size_inches(plate: Any) -> Optional[Tuple[float, float]]:
    """Return a packed plate's size in plotter units, or ``None``.

    Args:
        plate: The packed plate (duck-typed: a plate that cannot say its size
            is measured through a content-fitted band).

    Returns:
        ``(width, height)`` in plotter units, or ``None`` when the plate
        carries no usable size.
    """
    width = getattr(plate, "width", None)
    height = getattr(plate, "height", None)
    if not isinstance(width, (int, float)) or not isinstance(height, (int, float)):
        return None
    if width <= 0 or height <= 0:
        return None
    return (inches_to_units(float(width)), inches_to_units(float(height)))


def _slot_rect(
    x: float,
    y: float,
    width: float,
    height: float,
    rotated: bool,
) -> _Rect:
    """Return a placement slot as a device-unit rectangle.

    The plate frame is fixed by the assembly, not probed: the renderer draws a
    label into its local ``[0, width] x [0, height]`` box (Y up, origin at the
    slot's lower-left) and
    :func:`~plt_optimizer.generate.vectorize.assemble_plt_from_rendered_labels`
    lands it with a plain additive
    :func:`~plt_optimizer.generate.vectorize.translate_plt_coordinates` of
    ``(placement.x, placement.y)`` -- no mirroring and no Y flip. So the slot of
    a placement is exactly ``[x, x + width] x [y, y + height]`` in the emitted
    content's frame, in inches.

    ``width`` / ``height`` are PLACED inches, i.e. already turned into the
    orientation the label is engraved in, so the geometry swaps nothing.
    :func:`_slots_for` passes the ink rect from :func:`placement_ink_rect`
    (the label's nominal size, swapped and centred when the placement is
    sideways). Handing this helper ``PackedLabel.width`` / ``.height`` directly
    is the slot, ``clearance / 2`` wider than the ink on every side; swapping
    those post-rotation fields on ``rotated`` -- the shape this helper had
    before -- turns the rect 90 degrees away from the ink, which mis-bills
    every stroke near a sideways label.

    Args:
        x: Slot origin X in inches (from the plate frame's origin).
        y: Slot origin Y in inches (from the plate frame's origin).
        width: The rect's width in inches, as placed (post-rotation).
        height: The rect's height in inches, as placed (post-rotation).
        rotated: Whether the packer placed the label sideways. Carried for the
            slot's identity (a sideways slot is a distinct slot) and for the
            relief-orientation rule in :func:`_slots_for`; the geometry needs it
            by no means, the size is already turned.

    Returns:
        The slot rectangle in device plotter units.
    """
    return _Rect(
        inches_to_units(x),
        inches_to_units(y),
        inches_to_units(x) + inches_to_units(width),
        inches_to_units(y) + inches_to_units(height),
    )


def placement_ink_rect(placement: Any) -> Tuple[float, float, float, float]:
    """Return the rectangle a placement's engraved content covers, in inches.

    Three frames are in play and only the middle one is the ink:

    * the label's **nominal** ``width`` / ``height`` (its own spec box);
    * the **slot** the packer reserves, ``nominal + clearance`` on each axis,
      which is what ``PackedLabel.width`` / ``.height`` carry (post-rotation);
    * the **ink** the label engraves, which is the nominal size, turned 90
      degrees when the placement is sideways and kept CENTRED in its slot.

    The last two facts come from
    :func:`~plt_optimizer.generate.vectorize.assemble_plt_from_rendered_labels`:
    a rotated content block is rewritten by
    :func:`~plt_optimizer.generate.vectorize.rotate_plt_content_90cw` to span
    ``[0, height] x [0, width]`` about the label's own centre, then translated
    by ``(placement.x, placement.y)``. Verified against engraved ink: this rect
    coincides with a rotated label's border stroke to < 0.001in, where the slot
    rect is wide by the clearance and the slot rect swapped is wide by the
    label's whole short axis.

    Use this wherever geometry is attributed to, or drawn over, a label: the
    plate proof's per-label job key (:func:`~plt_optimizer.generate.jobset.job_overlay_boxes`)
    and the usage report's slot attribution. Swapping ``PackedLabel``'s
    post-rotation size is the classic error -- it turns the box 90 degrees away
    from the ink it is meant to mark.

    Args:
        placement: A ``PackedLabel`` (duck-typed). A placement whose source
            label carries no usable size falls back to its slot, the best
            available estimate.

    Returns:
        ``(x, y, width, height)`` in inches, in the plate frame -- the same
        space the packer places labels in.
    """
    source = getattr(placement, "source_label", None)
    nominal_w = getattr(source, "width", None)
    nominal_h = getattr(source, "height", None)
    if not isinstance(nominal_w, (int, float)) or not isinstance(nominal_h, (int, float)):
        return (
            float(placement.x),
            float(placement.y),
            float(placement.width),
            float(placement.height),
        )
    span_w, span_h = (nominal_h, nominal_w) if placement.rotated else (nominal_w, nominal_h)
    if span_w <= 0.0 or span_h <= 0.0:
        return (
            float(placement.x),
            float(placement.y),
            float(placement.width),
            float(placement.height),
        )
    # Centred in the slot: the label's own box is centred inside the footprint
    # the renderer measured (that is how the content is laid out), and the
    # assembly lands that footprint on the slot origin, so any clearance the
    # packer added splits evenly. With no clearance the ink is flush with the
    # slot's origin -- which is exactly where the plotter engraves it.
    x = float(placement.x) + max(float(placement.width) - span_w, 0.0) / 2.0
    y = float(placement.y) + max(float(placement.height) - span_h, 0.0) / 2.0
    return (x, y, float(span_w), float(span_h))


def _slots_for(placements: Sequence[Any], relief_ids: FrozenSet[str] = frozenset()) -> List[_Slot]:
    """Build the slot list of one plate's placements.

    Args:
        placements: The plate's ``PackedLabel`` placements, in packing order.
        relief_ids: The spec ids the label pool marks as relief runs. A slot is
            a relief slot when its own source label says so or the pool does -
            the pool is the attribution view, which is authoritative when a
            member report resolves strokes in the batch's label set.

    Returns:
        One slot per placement, in packing order.
    """
    slots: List[_Slot] = []
    for placement in placements:
        source = getattr(placement, "source_label", None)
        ink_x, ink_y, ink_w, ink_h = placement_ink_rect(placement)
        slots.append(
            _Slot(
                label_id=str(getattr(placement, "label_id", "") or ""),
                spec_id=str(getattr(source, "id", "") or ""),
                rect=_slot_rect(
                    ink_x,
                    ink_y,
                    ink_w,
                    ink_h,
                    bool(placement.rotated),
                ),
                is_relief=bool(getattr(source, "relief_engraving", False))
                or str(getattr(source, "id", "") or "") in relief_ids,
            )
        )
    return slots


def _point_owner(
    frame: Optional[_PlateFrame],
    x: float,
    y: float,
) -> Optional[_Slot]:
    """Return the slot that owns a point of toolpath geometry.

    Args:
        frame: The owning plate's frame, or ``None`` when unresolved.
        x: Device-unit X of the point to attribute.
        y: Device-unit Y of the point to attribute.

    Returns:
        The slot containing the point (padded by :data:`SLOT_TOL_IN`, nearest
        centre winning when overlapping slots both contain it); the nearest slot
        when the point only overhangs, i.e. it stays within
        :data:`SLOT_CENTRE_TOL_IN` of a slot; ``None`` when the plate carries no
        placements or the point belongs to no label (charged to
        :data:`UNATTRIBUTED_CLASS`).
    """
    if frame is None or not frame.slots:
        return None
    point = _Rect(x, y, x, y)

    def distance(slot: _Slot) -> Tuple[float, str]:
        cx, cy = slot.rect.center
        return ((cx - x) ** 2 + (cy - y) ** 2, slot.label_id)

    inside = [
        slot for slot in frame.slots if slot.rect.padded(inches_to_units(SLOT_TOL_IN)).covers(point)
    ]
    if inside:
        return min(inside, key=distance)
    # The point sits outside every slot (a relief pass covering the whole
    # island, a descender relief, cutter-radius bleed, a label that overruns
    # its reserved box): attribute on proximity, but only while the point is
    # still close to a slot.
    nearest = min(frame.slots, key=distance)
    if nearest.rect.padded(inches_to_units(SLOT_CENTRE_TOL_IN)).covers(point):
        return nearest
    return None


# --------------------------------------------------------------------------
# JSON shaping helpers
# --------------------------------------------------------------------------


def _round(value: Optional[float]) -> Optional[float]:
    """Round a float for the report.

    Args:
        value: The value, or ``None``.

    Returns:
        The value rounded to :data:`USAGE_FLOAT_DECIMALS`, or ``None``.
    """
    if value is None:
        return None
    return round(float(value), USAGE_FLOAT_DECIMALS)


def _band(value: float) -> float:
    """Round a bucket key for comparison.

    Args:
        value: The key value.

    Returns:
        The value rounded to :data:`USAGE_FLOAT_DECIMALS`.
    """
    return round(float(value), USAGE_FLOAT_DECIMALS)


def _sort_token(key: Any) -> Tuple[int, float, str]:
    """Return a total order key for a heterogeneous bucket key.

    ``None`` sorts first, then numbers, then strings, so bucket ordering is
    stable over mixed keys (a material name and a cutter diameter never compare
    directly, and Python 3 refuses to order them).

    Args:
        key: The bucket key.

    Returns:
        A comparable tuple.
    """
    if key is None:
        return (0, 0.0, "")
    if isinstance(key, bool):
        return (1, float(key), "")
    if isinstance(key, (int, float)):
        return (1, float(key), "")
    if isinstance(key, tuple):
        # Composite keys (a size bucket) order by their rounded width.
        return (1, float(key[0]), "|".join(str(part) for part in key[1:]))
    return (2, 0.0, str(key))


def _bucket_rows(
    counts: Mapping[Any, int],
    fields_for: Callable[[Any], Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Render a bucket counter as report rows, biggest first.

    Args:
        counts: Bucket key to count total.
        fields_for: Callable turning a bucket key into the row's fields.

    Returns:
        One row per bucket, ordered by count descending then key ascending.
    """
    return [
        dict(fields_for(key), count=count)
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], _sort_token(item[0])))
    ]


def _material_label(material: Optional[str]) -> str:
    """Return the display tag for a material name.

    Args:
        material: The stock material name, or ``None``.

    Returns:
        The name, or ``"(unset)"`` for the material-agnostic plate case.
    """
    return material if material else "(unset)"


def _zero_classes() -> Dict[str, float]:
    """Return a fresh all-zero stroke-class accumulator.

    Returns:
        Every :data:`DISTANCE_CLASSES` entry mapped to ``0.0``.
    """
    return dict.fromkeys(DISTANCE_CLASSES, 0.0)


# --------------------------------------------------------------------------
# Section builders: labels / characters / holes / material
# --------------------------------------------------------------------------


def _label_cutters(label: ResolvedLabel) -> List[float]:
    """Return the distinct cutter diameters a label engraves with.

    Args:
        label: The resolved label.

    Returns:
        Sorted distinct effective diameters (text lines, relief ladder, and the
        boundary/hole tool).
    """
    cutters: List[float] = []
    for line in label.content:
        if line.effective_cutter_diameter is not None:
            cutters.append(_band(line.effective_cutter_diameter))
    for extra in (label.effective_hole_cutter_diameter, label.hole_cutter_diameter):
        if extra is not None:
            cutters.append(_band(extra))
    for extra in getattr(label, "relief_engraving_cutters", None) or ():
        cutters.append(_band(extra))
    return sorted(set(cutters))


def _labels_usage(resolved_labels: Sequence[ResolvedLabel]) -> Dict[str, Any]:
    """Aggregate the label inventory of the job.

    Counts are physical labels (a multi-up label contributes ``count``), so the
    ``instances`` total matches the number of placements on the plates.

    Args:
        resolved_labels: The job's resolved labels, in content order.

    Returns:
        The ``usage.labels`` block.
    """
    instances = sum(int(label.count) for label in resolved_labels)
    by_shape: Counter[Any] = Counter()
    by_relief: Counter[Any] = Counter()
    by_material: Counter[Any] = Counter()
    by_size: Counter[Any] = Counter()
    by_cutter: Counter[Any] = Counter()
    for label in resolved_labels:
        count = int(label.count)
        shape = str(getattr(label, "shape_type", "rect") or "rect")
        material = label.material
        by_shape[shape] += count
        by_relief["relief" if label.relief_engraving else "plain"] += count
        by_material[material] += count
        by_size[(_band(label.width), _band(label.height), shape, material)] += count
        # One label can engrave with several cutters (per-line overrides, the
        # bold partner, the boundary/hole tool): count it once per diameter.
        for diameter in _label_cutters(label):
            by_cutter[diameter] += count

    return {
        "instances": instances,
        "specs": len(resolved_labels),
        "by_shape": _bucket_rows(by_shape, lambda key: {"shape": key}),
        "by_relief": _bucket_rows(by_relief, lambda key: {"engraving": key}),
        "by_material": _bucket_rows(
            by_material,
            lambda key: {"material": key, "material_label": _material_label(key)},
        ),
        "by_size": _bucket_rows(
            by_size,
            lambda key: {
                "width": key[0],
                "height": key[1],
                "shape": key[2],
                "material": key[3],
            },
        ),
        "by_cutter": _bucket_rows(by_cutter, lambda key: {"cutter_diameter": key}),
    }


def _countable_text(line: Any) -> str:
    """Return the characters a text line engraves, spaces excluded.

    Args:
        line: The resolved text line.

    Returns:
        ``plain_text`` with every whitespace character removed.
    """
    text = str(getattr(line, "plain_text", "") or "")
    return "".join(char for char in text if not char.isspace())


def _countable_markers(line: Any) -> str:
    """Return the glyphs a line's list marker engraves, spaces excluded.

    Args:
        line: The resolved text line.

    Returns:
        ``marker_text`` with every whitespace character removed.
    """
    text = str(getattr(line, "marker_text", "") or "")
    return "".join(char for char in text if not char.isspace())


def _line_char_bands(line: Any) -> List[Tuple[Tuple[float, float], int]]:
    """Return a line's character counts per engraved text height.

    Each band is a ``(nominal, engraved)`` pair because the shop orders by the
    nominal height while the machine cuts the toolpath height: compression, the
    cutter-diameter downsize and a bold partner all move the engraved value off
    the nominal one, and the pair is what makes ``0.25 (0.205)`` readable.

    ``<scale:N>`` / ``<sup>`` / ``<sub>`` / ``<sc>`` markup scales part of a
    line, so a character's band is its *engraved* height: the line's toolpath
    height times the per-character scale factor
    (:attr:`ResolvedTextLine.scale_values`, empty when the line carries no scale
    markup), with the nominal scaled by the same factor. A bold line reports its
    bold partner's height, which is the width the machine cuts with.

    Args:
        line: The resolved text line.

    Returns:
        ``((nominal_height, engraved_height), character_count)`` pairs, sorted,
        whitespace excluded.
    """
    text = _countable_text(line)
    if not text:
        return []
    scales: Sequence[float] = list(getattr(line, "scale_values", ()) or ())
    # The nominal side of every band: the ordered font height. There is no
    # separate bold nominal - bold_toolpath_text_height IS
    # ``nominal_text_height - the bold cutter's compensation``.
    nominal_base = float(line.nominal_text_height)
    bold_base = getattr(line, "bold_toolpath_text_height", None)
    if bold_base is not None:
        base = float(bold_base)
    else:
        # A toolpath height of 0 means the line was never laid out (a spec
        # built outside the cascade); the nominal height is the band the shop
        # orders cutters by.
        base = float(line.toolpath_text_height) or nominal_base
    nominal_base = _band(nominal_base)
    if len(scales) != len(text):
        # No (or desynchronized) per-character factors: the line engraves at its
        # own height.
        return [((nominal_base, _band(base)), len(text))]
    bands: Counter[Any] = Counter()
    for index in range(len(text)):
        scale = float(scales[index])
        bands[(_band(nominal_base * scale), _band(base * scale))] += 1
    return [(band, int(count)) for band, count in sorted(bands.items())]


def _characters_usage(resolved_labels: Sequence[ResolvedLabel]) -> Dict[str, Any]:
    """Aggregate the engraved character counts of the job.

    Spaces are excluded everywhere (they engrave nothing) and the count is
    reported per **text height band**, keyed by the ``(nominal, engraved)`` pair
    so the ordered size and the size the machine cuts are both visible (a
    0.250 in name compressed to 0.205 in is one band, ``0.25 (0.205)``). List
    markers are counted separately from the text so the two never double up.

    Args:
        resolved_labels: The job's resolved labels.

    Returns:
        The ``usage.characters`` block.
    """
    total = 0
    markers = 0
    spaces = 0
    by_height: Counter[Any] = Counter()
    by_label: Counter[Any] = Counter()
    for label in resolved_labels:
        count = int(label.count)
        for line in label.content:
            text = _countable_text(line)
            raw = str(getattr(line, "plain_text", "") or "")
            total += len(text) * count
            markers += len(_countable_markers(line)) * count
            spaces += len(raw) - len(text)
            by_label[label.id] += len(text) * count
            for band, chars in _line_char_bands(line):
                by_height[band] += chars * count

    return {
        "total": total,
        "markers": markers,
        "spaces_excluded": spaces,
        "by_text_height": _bucket_rows(
            by_height,
            lambda key: {"nominal_text_height": key[0], "text_height": key[1]},
        ),
        "by_label": _bucket_rows(by_label, lambda key: {"label_id": key}),
    }


def _holes_usage(resolved_labels: Sequence[ResolvedLabel]) -> Dict[str, Any]:
    """Aggregate the drill-hole counts of the job.

    Holes are bucketed by the diameter the spec asks for, together with the
    diameter the machine emits (the cutter-compensated circle a routed hole
    becomes when ``hole_adjust_for_cutter`` is on).

    Args:
        resolved_labels: The job's resolved labels.

    Returns:
        The ``usage.holes`` block.
    """
    total = 0
    by_diameter: Counter[Any] = Counter()
    by_label: Counter[Any] = Counter()
    engraved_by_diameter: Dict[float, float] = {}
    for label in resolved_labels:
        count = int(label.count)
        for hole in label.holes:
            specified = _band(hole.diameter)
            engraved_by_diameter[specified] = _band(
                2.0
                * hole_engraved_radius(
                    hole.diameter,
                    label.effective_hole_cutter_diameter,
                    label.hole_adjust_for_cutter,
                )
            )
            total += count
            by_diameter[specified] += count
            by_label[label.id] += count

    return {
        "total": total,
        "by_diameter": _bucket_rows(
            by_diameter,
            lambda key: {"diameter": key, "engraved_diameter": engraved_by_diameter.get(key)},
        ),
        "by_label": _bucket_rows(by_label, lambda key: {"label_id": key}),
    }


def _material_usage(
    packed_plates: Sequence[Any],
    material_by_plate: Mapping[int, Optional[str]],
    resolved_labels: Sequence[ResolvedLabel],
) -> Dict[str, Any]:
    """Aggregate the material the job consumes.

    Args:
        packed_plates: The packed plates, in packing order.
        material_by_plate: Stock material name per 1-based plate number.
        resolved_labels: The job's resolved labels (for the nominal area).

    Returns:
        The ``usage.material`` block.
    """
    plates_by_material: Counter[Any] = Counter()
    labels_by_material: Counter[Any] = Counter()
    area_by_material: Dict[Any, float] = {}
    bbox_by_material: Dict[Any, float] = {}
    total_area = 0.0
    total_bbox = 0.0
    for number, plate in enumerate(packed_plates, start=1):
        material = material_by_plate.get(number, getattr(plate, "material", None))
        span_x = float(plate.left_clearance) + float(plate.width)
        span_y = float(plate.top_clearance) + float(plate.height)
        area = span_x * span_y
        # The boxes the packer reserved, summed: wider than the nominal
        # label area (clearances + rotation) and narrower than the plate
        # when a sheet is only partly filled.
        bbox = sum(float(item.width) * float(item.height) for item in plate.labels)
        total_area += area
        total_bbox += bbox
        plates_by_material[material] += 1
        area_by_material[material] = area_by_material.get(material, 0.0) + area
        bbox_by_material[material] = bbox_by_material.get(material, 0.0) + bbox
        labels_by_material[material] += sum(
            int(placement.source_label.count) for placement in plate.labels
        )

    nominal = sum(
        float(label.width) * float(label.height) * int(label.count) for label in resolved_labels
    )
    materials = sorted(plates_by_material, key=_sort_token)

    return {
        "total": {
            "plates": len(packed_plates),
            "sq_in": _round(total_area),
            "sq_ft": _round(total_area / SQ_IN_PER_SQ_FT),
        },
        "by_material": [
            {
                "material": key,
                "material_label": _material_label(key),
                "plates": int(plates_by_material[key]),
                "sq_in": _round(float(area_by_material[key])),
                "sq_ft": _round(float(area_by_material[key]) / SQ_IN_PER_SQ_FT),
                "label_instances": int(labels_by_material[key]),
                "label_bbox_sq_in": _round(float(bbox_by_material[key])),
                "label_bbox_sq_ft": _round(float(bbox_by_material[key]) / SQ_IN_PER_SQ_FT),
            }
            for key in materials
        ],
        "label_nominal_area": {
            "sq_in": _round(nominal),
            "sq_ft": _round(nominal / SQ_IN_PER_SQ_FT),
        },
        "label_bbox_area": {
            "sq_in": _round(total_bbox),
            "sq_ft": _round(total_bbox / SQ_IN_PER_SQ_FT),
        },
    }


def _layout_usage(
    packed_plates: Sequence[Any],
    material_by_plate: Mapping[int, Optional[str]],
) -> Dict[str, Any]:
    """Aggregate how much of each plate the layout actually uses.

    The plate's sheet is the material the shop buys, so its span is the usable
    area plus the left/top clearance strips (the same span
    :func:`_material_usage` charges for). The envelope is the bounding box of
    the placements the packer put on the plate, so ``empty_sq_in`` is the sheet
    outside that envelope: the material a plate carries and nothing is engraved
    on. ``coverage_pct`` is the engraved area over the sheet, the same ratio the
    packing benchmark reports, so the report and the harness agree.

    Args:
        packed_plates: The packed plates, in packing order.
        material_by_plate: Stock material name per 1-based plate number.

    Returns:
        The ``usage.layout`` block: one ``by_plate`` row per plate plus the job
        ``total`` (plate count, rotated placements, empty material).
    """
    by_plate: List[Dict[str, Any]] = []
    total_empty = 0.0
    total_rotated = 0
    for number, plate in enumerate(packed_plates, start=1):
        placements = list(plate.labels)
        sheet_w = float(plate.left_clearance) + float(plate.width)
        sheet_h = float(plate.top_clearance) + float(plate.height)
        sheet = sheet_w * sheet_h
        used = sum(float(item.width) * float(item.height) for item in placements)
        if placements:
            bbox_w = max(float(item.x) + float(item.width) for item in placements) - min(
                float(item.x) for item in placements
            )
            bbox_h = max(float(item.y) + float(item.height) for item in placements) - min(
                float(item.y) for item in placements
            )
            envelope = bbox_w * bbox_h
        else:
            envelope = 0.0
        # A plate can never be more than full: rounding at packing precision
        # leaves a hair over 100 % coverage / a hair under 0 empty on plates
        # the packer tiled exactly, which reads as a bug in the report.
        coverage = min(100.0, used / sheet * 100.0) if sheet > 0.0 else 0.0
        empty = max(0.0, sheet - envelope)
        rotated = sum(1 for item in placements if bool(getattr(item, "rotated", False)))
        total_empty += empty
        total_rotated += rotated
        by_plate.append(
            {
                "plate": number,
                "material": material_by_plate.get(number, getattr(plate, "material", None)),
                "label_instances": sum(
                    int(placement.source_label.count) for placement in placements
                ),
                "sheet_sq_in": _round(sheet),
                "used_sq_in": _round(used),
                "envelope_sq_in": _round(envelope),
                "coverage_pct": _round(coverage),
                "empty_sq_in": _round(empty),
                "rotated_labels": rotated,
            }
        )
    sheets = sum(float(row["sheet_sq_in"] or 0.0) for row in by_plate)
    used_total = sum(float(row["used_sq_in"] or 0.0) for row in by_plate)
    return {
        "total": {
            "plates": len(packed_plates),
            "rotated_labels": total_rotated,
            "empty_sq_in": _round(total_empty),
            "coverage_pct": _round(
                min(100.0, used_total / sheets * 100.0) if sheets > 0.0 else 0.0
            ),
        },
        "by_plate": by_plate,
    }


@dataclass
class _WeightBucket:
    """One weight-report bucket: a plate, material, or (spec, plate) pair.

    Attributes:
        material: Stock material name of the bucket (``None`` = unset).
        density: The plate's lb/in2 density, or ``None`` when the bucket's
            plates carry no configured density (its weight is then ``None``).
        instances: Label instances counted in the bucket.
        area: Sum of those instances' nominal areas in square inches.
        lb: Sum of those instances' estimated weights in pounds.
    """

    material: Optional[str]
    density: Optional[float]
    instances: int = 0
    area: float = 0.0
    lb: float = 0.0

    def add(self, area: float, lb: float) -> None:
        """Accumulate one label instance into the bucket.

        Args:
            area: The instance's nominal area in square inches.
            lb: Its estimated weight in pounds (``0.0`` when the plate has no
                configured density, which :attr:`weight_lb` reports as ``None``).
        """
        self.instances += 1
        self.area += area
        self.lb += lb

    @property
    def weight_lb(self) -> Optional[float]:
        """The bucket's pounds, or ``None`` when its density is unconfigured."""
        return None if self.density is None else _round(self.lb)

    @property
    def weight_oz(self) -> Optional[float]:
        """The bucket's ounces, or ``None`` when its density is unconfigured."""
        return None if self.density is None else _round(self.lb * OZ_PER_LB)


def _weight_usage(
    packed_plates: Sequence[Any],
    material_by_plate: Mapping[int, Optional[str]],
    job_spec: Any,
) -> Dict[str, Any]:
    """Estimate the weight of the labels the job engraved.

    Every placement (one per physical label instance - packing unrolls
    ``count``) contributes its spec's nominal ``width x height`` area times the
    ``material_weight_per_square_inch`` (lb/in\u00b2) density of the plate it sits on,
    resolved by :meth:`~plt_optimizer.generate.schema.JobSpec.material_weight_for_plate`
    (a plate that declares a density wins; any other plate - including an
    auto-allocated one whose id is generated, not declared - uses the job-level
    density; ``None`` when neither is configured).

    Only the label's nominal area is weighed, never the sheet: the sheet's own
    weight is one multiplication away from ``usage.material.by_material``'s
    ``sq_in`` rows, which carry the plate area.

    The density is opt-in. When no plate carrying labels has one, the block
    reports ``available: false`` with ``total: null`` (the ``usage.time``
    convention) - an unconfigured density must never read as a 0 lb job, and
    every label then lands in ``unconfigured``.
    Partially configured (a plate without a density among plates that have
    one) reports ``available: true``, keeps those plates' rows at ``null``
    weights, counts their instances in ``unconfigured``, and says so in
    ``note``: the totals then cover the configured plates only.

    A jobset member's report resolves densities from the member's own spec,
    whose ``plates`` do not carry the batch's plate ids (a merged sheet is
    numbered from the batch), so a member's rows fall back to its job-level
    density.

    Caller contract: a shop default set in ``job-config.json`` weighs the whole
    job, because ``JobDefaults.apply`` injects the job-level density onto every
    plate before layout - so a spec built through the config layers already
    carries it, and this resolver deliberately does not re-apply the config
    layer to an arbitrary spec (the plate cascade has already run; re-applying
    it after the fact could not reach per-plate values). The consequence: a
    caller that wants a job weighed must pass the SAME ``job_spec`` /
    ``job_config`` pair it laid the job out with. A bare spec with no config
    means nothing is configured, and the block reports "not estimated" - 0 lb
    would claim the labels are weightless.

    Args:
        packed_plates: The packed plates, in packing order.
        material_by_plate: Stock material name per 1-based plate number (the
            counting pool's material names, for the row labels).
        job_spec: The resolved job spec, used to resolve each plate's density.
            ``None`` (a spec-less API call) disables the estimate. Must be the
            spec the job was actually laid out with (config layers applied) for
            a configured job to read as weighed.

    Returns:
        The ``usage.weight`` block, in pounds (with ``oz`` alongside every
        pound figure). ``material_weight_per_square_inch`` is echoed per row
        verbatim - it is a configured number, not a measurement, so it is the
        one float in this module that is not rounded.
    """
    weight_of: Any = getattr(job_spec, "material_weight_for_plate", None)
    densities: Dict[int, Optional[float]] = {}
    for number, plate in enumerate(packed_plates, start=1):
        plate_id = getattr(plate, "plate_id", None)
        value: Any = (
            weight_of(str(plate_id))
            if callable(weight_of) and plate_id is not None
            else getattr(job_spec, "material_weight_per_square_inch", None)
        )
        densities[number] = None if value is None else float(value)

    plate_rows: List[Dict[str, Any]] = []
    label_acc: Dict[Tuple[str, int], _WeightBucket] = {}
    material_acc: Dict[Optional[str], _WeightBucket] = {}
    total_instances = 0
    total_area = 0.0
    total_lb = 0.0
    unconfigured_instances = 0
    unconfigured_area = 0.0

    for number, plate in enumerate(packed_plates, start=1):
        density = densities[number]
        material = material_by_plate.get(number, getattr(plate, "material", None))
        plate_instances = 0
        plate_area = 0.0
        plate_lb = 0.0
        for placement in plate.labels:
            spec = placement.source_label
            spec_id = str(spec.id)
            area = float(spec.width) * float(spec.height)
            lb = area * density if density is not None else 0.0
            plate_instances += 1
            plate_area += area
            plate_lb += lb
            total_instances += 1
            total_area += area
            if density is None:
                unconfigured_instances += 1
                unconfigured_area += area
            label = label_acc.setdefault((spec_id, number), _WeightBucket(material, density))
            label.add(area, lb)
            material_acc.setdefault(material, _WeightBucket(material, density)).add(area, lb)
        if density is not None:
            total_lb += plate_lb
        plate_rows.append(
            {
                "plate": number,
                "plate_id": str(getattr(plate, "plate_id", number)),
                "material": material,
                "material_label": _material_label(material),
                "material_weight_per_square_inch": density,
                "label_instances": plate_instances,
                "label_sq_in": _round(plate_area),
                "lb": _round(plate_lb) if density is not None else None,
                "oz": _round(plate_lb * OZ_PER_LB) if density is not None else None,
            }
        )

    available = any(density is not None for density in densities.values())
    if not available:
        return {
            "available": False,
            "note": (
                "No material_weight_per_square_inch is configured (job-config.json "
                "defaults, the job, or a plate), so label weight is not estimated."
            ),
            "units": "lb",
            "density_units": "lb/in2",
            "basis": "label nominal area (spec width x height, per instance)",
            "total": None,
            "by_material": [],
            "by_plate": [],
            "by_label": [],
            # Every instance is unweighed in this case, which is what the block
            # is saying: the bucket stays a dict so consumers never branch on
            # ``available`` to read it.
            "unconfigured": {
                "label_instances": total_instances,
                "label_sq_in": _round(total_area),
            },
        }

    notes: List[str] = []
    if unconfigured_instances:
        notes.append(
            f"{unconfigured_instances} label instance(s) sit on plates with no configured "
            "density and are excluded from the weights (listed under 'unconfigured')."
        )
    return {
        "available": True,
        "note": "  ".join(notes),
        "units": "lb",
        "density_units": "lb/in2",
        "basis": "label nominal area (spec width x height, per instance)",
        "total": {
            "label_instances": total_instances,
            "label_sq_in": _round(total_area),
            "lb": _round(total_lb),
            "oz": _round(total_lb * OZ_PER_LB),
        },
        "by_material": [
            {
                "material": key,
                "material_label": _material_label(key),
                "label_instances": material_acc[key].instances,
                "label_sq_in": _round(material_acc[key].area),
                "lb": material_acc[key].weight_lb,
                "oz": material_acc[key].weight_oz,
            }
            for key in sorted(
                material_acc,
                key=lambda key: (-material_acc[key].lb, _sort_token(key)),
            )
        ],
        "by_plate": plate_rows,
        "by_label": [
            {
                "spec_id": spec_id,
                "plate": number,
                "material": label_acc[(spec_id, number)].material,
                "material_label": _material_label(label_acc[(spec_id, number)].material),
                "material_weight_per_square_inch": label_acc[(spec_id, number)].density,
                "instances": label_acc[(spec_id, number)].instances,
                "label_sq_in": _round(label_acc[(spec_id, number)].area),
                "lb": label_acc[(spec_id, number)].weight_lb,
                "oz": label_acc[(spec_id, number)].weight_oz,
            }
            for (spec_id, number) in sorted(
                label_acc,
                key=lambda key: (-label_acc[key].lb, key[0], key[1]),
            )
        ],
        "unconfigured": {
            "label_instances": unconfigured_instances,
            "label_sq_in": _round(unconfigured_area),
        },
    }


# --------------------------------------------------------------------------
# Run-time model (tool_options driven)
# --------------------------------------------------------------------------


def _positive(value: Any) -> Optional[float]:
    """Return ``value`` as a float when it is a usable rate, else ``None``.

    Args:
        value: A resolved tool-option value (any declared type), or ``None``.

    Returns:
        The value as a float when it is a real number above zero.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number > 0.0 else None


@dataclass(frozen=True)
class _LayerRates:
    """Run-time rates of one ``tool_options`` layer.

    Distances are divided into seconds and strokes are multiplied into the
    per-stroke Z terms, so every rate here is a cost, never a speed.

    Attributes:
        cutting: Seconds per inch of pen-down travel (1 / cutting_velocity).
        rapid: Seconds per inch of pen-up travel (1 / rapid_velocity, which
            falls back to the layer's cutting_velocity when unconfigured).
        plunge: Seconds per stroke (z_clearance / plunge_velocity).
        z_up: Seconds per stroke (z_up_velocity, declared in ms).
        dwell: Seconds per stroke (dwell_time, declared in ms).
        params: The resolved parameter values that produced these rates,
            reported verbatim in ``usage.time.model`` so the operator can see
            what the estimate assumed.
    """

    cutting: float
    rapid: float
    plunge: float
    z_up: float
    dwell: float
    params: Dict[str, Any] = field(default_factory=dict)

    @property
    def per_stroke(self) -> float:
        """Seconds of one stroke's full Z cycle (plunge + z-up + dwell)."""
        return self.plunge + self.z_up + self.dwell


def _build_layer_rates(
    job_spec: Any,
    job_config: Any,
    plt_type: str,
) -> Optional[_LayerRates]:
    """Resolve the run-time rates of one layer from the tool options.

    Values come from :func:`~plt_optimizer.generate.tool_options.resolve_tool_option_values`,
    i.e. the job-spec override -> the config's dual default for ``plt_type``
    -> unset, with the configured ``min``/``max`` clamps - exactly what the
    export wrote into that layer's PLT headers. The rapid velocity is the one
    term the registry cannot supply (it has no HPGL command of its own), so it
    comes from the ``rapid_velocity`` job-config field and falls back to the
    layer's cutting velocity.

    Args:
        job_spec: The resolved job spec (its ``tool_options`` are the override
            tier), or ``None``.
        job_config: The loaded job config, or ``None``.
        plt_type: ``"text"`` or ``"borders_holes"``.

    Returns:
        The layer's rates, or ``None`` when no cutting velocity is configured
        (the whole time block is then omitted - a report must never imply a
        run time the machine parameters cannot support).
    """
    if job_spec is None or job_config is None:
        return None
    values = resolve_tool_option_values(job_spec, job_config, plt_type)
    cutting_velocity = _positive(values.get(CUTTING_VELOCITY_KEY))
    if cutting_velocity is None:
        logger.debug(
            "usage: no %s resolved for the %r layer, run time is not estimable",
            CUTTING_VELOCITY_KEY,
            plt_type,
        )
        return None
    rapid_velocity = _positive(getattr(job_config.defaults, "rapid_velocity", None))
    rapid_source = "rapid_velocity" if rapid_velocity is not None else CUTTING_VELOCITY_KEY
    if rapid_velocity is None:
        rapid_velocity = cutting_velocity
    plunge_velocity = _positive(values.get(PLUNGE_VELOCITY_KEY))
    z_clearance = _positive(values.get(Z_CLEARANCE_KEY))
    plunge = (
        z_clearance / plunge_velocity
        if plunge_velocity is not None and z_clearance is not None
        else 0.0
    )
    z_up_ms = _positive(values.get(Z_UP_VELOCITY_KEY))
    dwell_ms = _positive(values.get(DWELL_TIME_KEY))
    return _LayerRates(
        cutting=1.0 / cutting_velocity,
        rapid=1.0 / rapid_velocity,
        plunge=plunge,
        z_up=(z_up_ms or 0.0) / MS_PER_SECOND,
        dwell=(dwell_ms or 0.0) / MS_PER_SECOND,
        params={
            CUTTING_VELOCITY_KEY: cutting_velocity,
            "rapid_velocity": rapid_velocity,
            "rapid_velocity_source": rapid_source,
            PLUNGE_VELOCITY_KEY: plunge_velocity,
            Z_CLEARANCE_KEY: z_clearance,
            Z_UP_VELOCITY_KEY: z_up_ms,
            DWELL_TIME_KEY: dwell_ms,
            "stroke_z_cycle_seconds": _round(
                plunge + (z_up_ms or 0.0) / MS_PER_SECOND + (dwell_ms or 0.0) / MS_PER_SECOND
            ),
        },
    )


def _zero_ops() -> List[float]:
    """Return a fresh zeroed per-operation seconds accumulator.

    Returns:
        ``[cutting, rapid, plunge, z_up, dwell]`` in seconds.
    """
    return [0.0] * len(TIME_OPERATIONS)


def _ops_dict(values: Sequence[float]) -> Dict[str, Optional[float]]:
    """Render one operation accumulator as a JSON mapping.

    Args:
        values: The ``[cutting, rapid, plunge, z_up, dwell]`` seconds.

    Returns:
        The named operation totals, rounded.
    """
    return {name: _round(values[index]) for index, name in enumerate(TIME_OPERATIONS)}


# --------------------------------------------------------------------------
# Distance measurement
# --------------------------------------------------------------------------


@dataclass
class _DistanceTotals:
    """Accumulator for measured toolpath distances (inches) and run time.

    The time accumulators are fed by the same pass as the distances (see
    :meth:`add_cutting`, :meth:`add_rapid` and :meth:`add_stroke_cycle`) and
    are only non-zero when :attr:`rates` was set for the measured file, so the
    two blocks can never disagree about which geometry was counted.

    Attributes:
        cutting: Total pen-down distance.
        rapid: Total pen-up (air travel) distance, inter-path gaps included.
        by_class: Cutting distance per stroke class.
        by_cutter: ``[cutting, rapid, files]`` per (cutter diameter, layer kind).
        by_material: ``[cutting, rapid]`` per stock material.
        by_job: ``[cutting, rapid]`` per jobset member (``usage.by_job``) -
            empty for a single-job run.
        by_plate: ``[cutting, rapid]`` per plate id.
        by_label: ``[cutting, rapid]`` per label placement id.
        class_by_label: Stroke-class cutting distance per placement id.
        files: One report row per measured file.
        unattributed_strokes: Strokes no slot could explain.
        unattributed_cutting: Cutting distance of those strokes.
        unattributed_rapid: Rapid distance of those strokes.
        rates: The measured file's run-time rates, set before measuring; the
            time methods add nothing while it is ``None``.
        strokes: Strokes measured with rates active (each carries one Z cycle).
        time_by_op: Seconds per operation for the whole job.
        by_label_time: Operation seconds per label placement id.
        by_class_time: Operation seconds per stroke class.
        unattributed_time: Operation seconds of the strokes no slot explained.
        by_cutter_time: Operation seconds per (cutter diameter, layer kind).
        by_material_time: Operation seconds per stock material.
        by_job_time: Operation seconds per jobset member.
        by_plate_time: Operation seconds per plate id.
        files_with_rates: Files measured with rates active - time is only
            reportable when this equals the number of measured files.
    """

    cutting: float = 0.0
    rapid: float = 0.0
    by_class: Dict[str, float] = field(default_factory=_zero_classes)
    by_cutter: Dict[Tuple[Any, Any], List[float]] = field(default_factory=dict)
    by_material: Dict[Any, List[float]] = field(default_factory=dict)
    by_job: Dict[str, List[float]] = field(default_factory=dict)
    by_plate: Dict[int, List[float]] = field(default_factory=dict)
    by_label: Dict[str, List[float]] = field(default_factory=dict)
    class_by_label: Dict[str, Dict[str, float]] = field(default_factory=dict)
    files: List[Dict[str, Any]] = field(default_factory=list)
    unattributed_strokes: int = 0
    unattributed_cutting: float = 0.0
    unattributed_rapid: float = 0.0
    rates: Optional[_LayerRates] = None
    strokes: int = 0
    # Placement id -> owning jobset member, set once by the measurement pass.
    # Empty for a single-job run, which keeps ``by_job`` out of the report.
    job_of_label: Mapping[str, str] = field(default_factory=dict)
    # Plate number -> the first member whose labels are on that sheet. A sheet's
    # own furniture (border, registration marks) belongs to no label, so it is
    # charged through this map - the same rule in the batch view and in every
    # member view, which is what makes the members add up to the batch.
    plate_owner_of: Mapping[int, str] = field(default_factory=dict)
    # The sheet the file being measured sits on, set per file alongside
    # :attr:`job_of_label`; :meth:`_add_job` resolves sheet furniture through it.
    plate_number: Optional[int] = None
    # The one member a file was written for, set per file. A file with a single
    # contributing member IS that member's engraving, so a stroke the slot
    # geometry cannot place (a glyph widened by compression, a kern overhang)
    # is charged to it - the same claim :func:`stroke_job`'s gate makes, which
    # is what keeps a member's own file from being billed to nobody.
    file_owner: Optional[str] = None
    time_by_op: List[float] = field(default_factory=_zero_ops)
    by_label_time: Dict[str, List[float]] = field(default_factory=dict)
    by_class_time: Dict[str, List[float]] = field(default_factory=dict)
    by_cutter_time: Dict[Tuple[Any, Any], List[float]] = field(default_factory=dict)
    by_material_time: Dict[Any, List[float]] = field(default_factory=dict)
    by_job_time: Dict[str, List[float]] = field(default_factory=dict)
    by_plate_time: Dict[int, List[float]] = field(default_factory=dict)
    unattributed_time: List[float] = field(default_factory=_zero_ops)
    files_with_rates: int = 0

    def add_cutting(
        self,
        value: float,
        stroke_class: str,
        label_id: Optional[str],
        job_owner: Optional[str] = None,
    ) -> None:
        """Record a cutting distance.

        Args:
            value: Distance in inches.
            stroke_class: One of :data:`DISTANCE_CLASSES`.
            label_id: The owning placement id, or ``None`` when unattributed.
            job_owner: The member the STROKE belongs to, from the ownership
                gate, used for :attr:`by_job` only when ``label_id`` names no
                placement. ``None`` resolves the member from ``label_id`` (the
                unchanged behaviour). Per-label, per-class and
                :attr:`unattributed_cutting` never see it - the stroke's owner
                makes a job account complete, it does not move geometry into a
                slot it is not in.
        """
        self.cutting += value
        self.by_class[stroke_class] = self.by_class.get(stroke_class, 0.0) + value
        if self.rates is not None:
            self._add_time(0, value * self.rates.cutting, stroke_class, label_id, job_owner)
        if label_id is None:
            self.unattributed_cutting += value
            self._add_job(None, cutting=value, job_owner=job_owner)
            return
        self.by_label.setdefault(label_id, [0.0, 0.0])[0] += value
        self._add_job(label_id, cutting=value, job_owner=job_owner)
        row = self.class_by_label.setdefault(label_id, _zero_classes())
        row[stroke_class] = row.get(stroke_class, 0.0) + value

    def add_rapid(
        self, value: float, label_id: Optional[str], job_owner: Optional[str] = None
    ) -> None:
        """Record a rapid (pen-up) distance, charged to its destination label.

        Args:
            value: Distance in inches.
            label_id: The owning placement id, or ``None`` when unattributed.
            job_owner: The member the stroke of this travel belongs to; see
                :meth:`add_cutting`.
        """
        self.rapid += value
        if self.rates is not None:
            self._add_time(1, value * self.rates.rapid, UNATTRIBUTED_CLASS, label_id, job_owner)
        if label_id is None:
            self.unattributed_rapid += value
            self._add_job(None, rapid=value, job_owner=job_owner)
            return
        self.by_label.setdefault(label_id, [0.0, 0.0])[1] += value
        self._add_job(label_id, rapid=value, job_owner=job_owner)

    def add_stroke_cycle(
        self, stroke_class: str, label_id: Optional[str], job_owner: Optional[str] = None
    ) -> None:
        """Record one stroke's pen-down -> pen-up Z cycle (plunge, z-up, dwell).

        Called once per stroke, at the stroke's first cutting segment, so the
        cycle follows the same attribution as the cutting distance (a
        multi-subpath path plunges once and is charged once).

        Args:
            stroke_class: The class of that first cutting segment.
            label_id: The owning placement id, or ``None`` when unattributed.
            job_owner: The member the stroke belongs to; see
                :meth:`add_cutting`.
        """
        if self.rates is None:
            return
        rates = self.rates
        self.strokes += 1
        # ``_add_time`` owns the operation accumulator - adding the terms here
        # as well would charge every stroke's Z cycle twice.
        for index, value in ((2, rates.plunge), (3, rates.z_up), (4, rates.dwell)):
            self._add_time(index, value, stroke_class, label_id, job_owner)

    def sheet_owner(self) -> Optional[str]:
        """Return the member charged with the current sheet's furniture.

        The sheet's border and registration marks belong to no label, so they are
        charged to the first member whose labels are on the sheet being measured
        (:attr:`plate_owner_of`) - one rule shared by the batch view and every
        member view, which is what makes the members add up to the batch. The
        plate-frame entry of :attr:`job_of_label` is the single-sheet fallback.

        Returns:
            The member key, or ``None`` when neither map resolves.
        """
        owner = self.plate_owner_of.get(self.plate_number or 0)
        if owner is not None:
            return owner
        return self.job_of_label.get(PLATE_FRAME_LABEL)

    def resolve(self, label_id: Optional[str]) -> str:
        """Return the member one placement id belongs to.

        THE attribution rule, shared by every charge (distance, time, and the
        per-stroke gate in :func:`_measure_document`) so the three can never
        disagree about who owns a stroke:

        - a placement id in :attr:`job_of_label` belongs to that member;
        - :data:`PLATE_FRAME_LABEL` (a sheet's border / registration marks)
          belongs to the sheet's first member - see :meth:`sheet_owner`; a
          global frame entry is only the fallback, which is what keeps a sheet
          neighbour's furniture out of this member's account;
        - a stroke that resolves to no placement at all belongs to
          :attr:`file_owner` when the file was written for one member;
        - anything else, and a single-job run, is :data:`UNATTRIBUTED_JOB`.

        Args:
            label_id: The placement id a stroke resolved to, or ``None``.

        Returns:
            The member key.
        """
        if label_id is None:
            return self.file_owner or UNATTRIBUTED_JOB
        if label_id == PLATE_FRAME_LABEL:
            return self.sheet_owner() or UNATTRIBUTED_JOB
        return self.job_of_label.get(label_id, UNATTRIBUTED_JOB)

    def _add_job(
        self,
        label_id: Optional[str],
        *,
        cutting: float = 0.0,
        rapid: float = 0.0,
        job_owner: Optional[str] = None,
    ) -> None:
        """Charge a distance to the jobset member that owns the placement.

        A single-job run (empty :attr:`job_of_label`) keeps the map empty, so
        the report's ``by_job`` block stays absent exactly as before.

        Args:
            label_id: The owning placement id, or ``None`` when unattributed.
            cutting: Pen-down inches to add.
            rapid: Pen-up inches to add.
            job_owner: The member the stroke belongs to, which wins over the
                placement id's owner. See :meth:`add_cutting`.
        """
        if not self.job_of_label:
            return
        # THE single rule, :meth:`resolve` - the same function the per-stroke
        # ownership gate in :func:`_measure_document` asks. Every earlier
        # incident of a member's ``by_job`` row not matching its own per-file
        # row came from this line having its own copy of the lookup: a charge
        # made with no placement id (the gate's "this file has one owner" claim)
        # landed on ``unattributed`` here even though the gate had named the
        # member, so the file measured the stroke and the job account did not.
        owner = job_owner if job_owner is not None else self.resolve(label_id)
        bucket = self.by_job.setdefault(owner, [0.0, 0.0])
        bucket[0] += cutting
        bucket[1] += rapid

    def _add_time(
        self,
        index: int,
        value: float,
        stroke_class: str,
        label_id: Optional[str],
        job_owner: Optional[str] = None,
    ) -> None:
        """Add seconds to the operation, per-label and per-class accumulators.

        Args:
            index: The :data:`TIME_OPERATIONS` index of the operation.
            value: Seconds.
            stroke_class: The stroke class charged.
            label_id: The owning placement id, or ``None`` when unattributed.
            job_owner: The member the stroke belongs to; see
                :meth:`add_cutting`.
        """
        self.time_by_op[index] += value
        class_row = self.by_class_time.setdefault(stroke_class, _zero_ops())
        class_row[index] += value
        if self.job_of_label:
            owner = job_owner if job_owner is not None else self.resolve(label_id)
            self.by_job_time.setdefault(owner, _zero_ops())[index] += value
        if label_id is None:
            self.unattributed_time[index] += value
            return
        self.by_label_time.setdefault(label_id, _zero_ops())[index] += value

    def time_total(self, values: Optional[Sequence[float]] = None) -> float:
        """Sum an operation accumulator.

        Args:
            values: The seconds to sum; defaults to the job totals.

        Returns:
            The total seconds.
        """
        return sum(self.time_by_op if values is None else values)

    def merge(self, other: _DistanceTotals) -> None:
        """Fold another accumulator (one file's measurements) into this one.

        Args:
            other: The measurements to add.
        """
        self.cutting += other.cutting
        self.rapid += other.rapid
        for class_name, value in other.by_class.items():
            self.by_class[class_name] = self.by_class.get(class_name, 0.0) + value
        self.unattributed_strokes += other.unattributed_strokes
        self.unattributed_cutting += other.unattributed_cutting
        self.unattributed_rapid += other.unattributed_rapid
        self.strokes += other.strokes
        self.files_with_rates += other.files_with_rates
        for index, value in enumerate(other.time_by_op):
            self.time_by_op[index] += value
        for index, value in enumerate(other.unattributed_time):
            self.unattributed_time[index] += value
        for label_id, seconds in other.by_label_time.items():
            bucket = self.by_label_time.setdefault(label_id, _zero_ops())
            for index, value in enumerate(seconds):
                bucket[index] += value
        for class_name, seconds in other.by_class_time.items():
            bucket = self.by_class_time.setdefault(class_name, _zero_ops())
            for index, value in enumerate(seconds):
                bucket[index] += value
        for label_id, (cut, rapid) in other.by_label.items():
            bucket = self.by_label.setdefault(label_id, [0.0, 0.0])
            bucket[0] += cut
            bucket[1] += rapid
        for owner, (cut, rapid) in other.by_job.items():
            bucket = self.by_job.setdefault(owner, [0.0, 0.0])
            bucket[0] += cut
            bucket[1] += rapid
        for owner, seconds in other.by_job_time.items():
            bucket = self.by_job_time.setdefault(owner, _zero_ops())
            for op_index, value in enumerate(seconds):
                bucket[op_index] += value
        for label_id, classes in other.class_by_label.items():
            class_bucket = self.class_by_label.setdefault(label_id, _zero_classes())
            for class_name, value in classes.items():
                class_bucket[class_name] = class_bucket.get(class_name, 0.0) + value


def _in_plate_frame(frame: Optional[_PlateFrame], x: float, y: float) -> bool:
    """Test a point against the plate's own (label-free) structural contours.

    Args:
        frame: The owning plate's frame, or ``None`` when unresolved.
        x: Device-unit X of the point.
        y: Device-unit Y of the point.

    Returns:
        ``True`` when the point lies on the sheet's border / registration
        geometry, i.e. inside one of the frame's plate-level boxes.
    """
    if frame is None or not frame.plate_frame:
        return False
    point = _Rect(x, y, x, y)
    return any(box.covers(point) for box in frame.plate_frame)


def _measure_document(
    document: PLTDocument,
    frame: Optional[_PlateFrame],
    kind: Optional[str],
    totals: _DistanceTotals,
    only_job: Optional[str] = None,
    file_owners: Sequence[str] = (),
) -> int:
    """Measure one parsed per-cutter file into the accumulators.

    Attribution walks the path's segments (see the module docstring): the
    per-segment lengths sum to the path's own ``cutting_distance`` /
    ``rapid_distance``, so the per-file, per-cutter and per-plate rows stay
    exact while a path that spans labels is split across them.

    A jobset export merges every job engraving with a tool into ONE file per
    (cutter, color, header block), so a shared sheet's file carries several
    members' strokes. ``only_job`` measures a single one of those members: a
    stroke whose owning member differs adds nothing anywhere (no distance, no
    stroke count, no class / cutter / material / plate / label bucket, no time,
    and nothing in :data:`UNATTRIBUTED_CLASS`), while the tool position keeps
    walking it so every inter-path gap stays charged exactly once - a gap is
    charged at the arrival of the path it flies to, so the member owning that
    path pays it, and the members sum to the batch view by construction.

    Args:
        document: The parsed file.
        frame: The owning plate's frame, or ``None`` when unresolved.
        kind: The file's layer kind (:data:`KIND_TEXT` / :data:`KIND_STRUCTURAL`).
        totals: The accumulator to add into.
        only_job: Jobset member to restrict the measurement to, or ``None`` to
            measure every stroke (a single-job run and a jobset's batch view).
        file_owners: Every member with strokes in THIS file, from
            :attr:`~plt_optimizer.generate.vectorize.PerCutterExport.file_jobs`.
            Used by :func:`stroke_job`'s last fallback; empty for a single-job
            run and for a file shared by several members.

    Returns:
        The number of strokes (paths) that carry cutting geometry.
    """
    strokes = 0
    position: Optional[Tuple[float, float]] = None

    def advance(last_path: StrokePath) -> Tuple[float, float]:
        """Return where the tool stops after ``last_path``.

        Args:
            last_path: The path just walked.

        Returns:
            Its last segment's end, else its pen-up destination, else a
            coordinate the caller recognises as "the tool did not move" (a path
            with no coordinates at all).
        """
        if last_path.segments:
            end = last_path.segments[-1].end
            return (end.x, end.y)
        if last_path.pen_up_position is not None:
            return (last_path.pen_up_position.x, last_path.pen_up_position.y)
        return (float("nan"), float("nan"))

    def owner_job(label_id: Optional[str]) -> Optional[str]:
        """Return the jobset member that owns one plate-frame label id.

        Args:
            label_id: The placement id a stroke resolved to, ``None`` when it
                resolved to no slot, :data:`PLATE_FRAME_LABEL` for plate-level
                furniture, or - for a single-job run, whose owner map is empty -
                the placement id itself, which is its own member.

        Returns:
            The member key, or ``None`` when nothing resolves.
        """
        if label_id is None:
            return None
        job = totals.job_of_label.get(label_id)
        return job if job is not None else label_id

    def slot_job(slot: Optional[_Slot]) -> Optional[str]:
        """Return the jobset member that owns a slot.

        Args:
            slot: The slot found by :func:`_point_owner`, or ``None``.

        Returns:
            The member key, or ``None`` for an unowned slot.
        """
        return owner_job(slot.label_id if slot is not None else None)

    def stroke_job(
        cutting_segments: Sequence[Segment],
        last_path: StrokePath,
    ) -> Optional[str]:
        """Return the jobset member a whole stroke belongs to.

        THE ONE OWNERSHIP CHAIN, shared by the ``only_job`` gate and every
        per-segment charge so the two can never disagree about who owns a stroke
        (a stroke the gate claims and the charge bills to ``unattributed`` breaks
        the partition the reports are built on). On the stroke as one unit: a
        file written for exactly one member is that member's engraving by
        construction, so it wins the whole file; then the first cutting
        segment's slot (a relief pass is one contour covering every island on
        the plate, so a later segment's owner would be arbitrary); then the
        sheet's own furniture; then where the tool landed. The file rule comes
        FIRST because a member's slice of a shared sheet is measured against the
        shared slot geometry: a relief pass from Alpha's own text file sweeps
        over Beta's slot, and a slot rule would hand a stroke that Alpha wrote -
        and Alpha pays to engrave - to Beta, which measures no file it did not
        write and therefore never books it. The slot rule answers for a file
        several members share, where a slot hit really is the stroke's owner.

        Args:
            cutting_segments: The path's cutting segments.
            last_path: The path being walked.

        Returns:
            The owning member key, or ``None`` when the stroke resolves to
            neither a slot, the sheet, a landing point, nor a lone file owner.
        """
        if len(file_owners) == 1:
            # A file written for exactly one member IS that member's engraving:
            # claimed before the slot geometry, which is the shared sheet's and
            # can put one of this member's contours inside a neighbour's slot.
            return file_owners[0]
        for segment in cutting_segments:
            slot = _point_owner(
                frame,
                (segment.start.x + segment.end.x) / 2.0,
                (segment.start.y + segment.end.y) / 2.0,
            )
            if slot is not None:
                return totals.resolve(slot.label_id)
        if cutting_segments and frame is not None and kind == KIND_STRUCTURAL:
            # The sheet's own border / registration marks, tested the way the
            # per-segment charge tests them (a structural-layer contour with
            # travel in the furniture band) and on every segment, not on the
            # first one only: which subpath a mark's contour starts with is an
            # artefact of the writer, and a gate that reads the first point of
            # a stroke resolves a mark the charge answers differently for.
            # Text files are excluded here because the charge site only names
            # furniture for structural paths (``layer_class``); a text stroke
            # outside every slot belongs to the file's owner, not to the sheet.
            for segment in cutting_segments:
                if _in_plate_frame(frame, segment.start.x, segment.start.y) and _in_plate_frame(
                    frame, segment.end.x, segment.end.y
                ):
                    return totals.sheet_owner()
            if totals.job_of_label:
                # The charge bills EVERY unslotted structural segment to the
                # sheet in a jobset run, so the gate must claim the whole stroke
                # too - a mark engraved across the cut line, whose first segment
                # starts outside the band, is otherwise answered differently by
                # the two paths and the members stop adding up to the batch.
                # A single-job run keeps the narrow rule: it has no members to
                # partition, and widening it here would relabel a stroke that
                # falls outside its own slot as sheet furniture.
                return totals.sheet_owner()
        if last_path.pen_up_position is not None:
            slot = _point_owner(frame, last_path.pen_up_position.x, last_path.pen_up_position.y)
            if slot is not None:
                return totals.resolve(slot.label_id)
        return None

    for path in document.stroke_paths:
        layer_class = (
            frame.stroke_class.get(_path_signature(path), BORDER_CLASS)
            if kind == KIND_STRUCTURAL and frame is not None
            else None
        )
        cutting = [seg for seg in path.segments if seg.is_cutting]
        # One ownership answer per stroke, from the gate - and carried into
        # every charge the stroke produces (``job_owner``). A contour is the
        # unit the gate decides on because a contour is the unit the plotter
        # engraves: a sheet border is one path whose segments run past every
        # slot on the plate, so a per-segment job lookup would bill the same
        # stroke to the sheet, to a neighbour, and to nobody, in the same file.
        job_owner = stroke_job(cutting, path)
        if only_job is not None and job_owner != only_job:
            # Another member's stroke in a merged file: it contributes to
            # nothing at all - counted here, it would double the shared file's
            # toolpath across the member reports; counted unattributed, the
            # members would no longer add up to the batch view. The tool still
            # travelled it, so the position bookkeeping keeps walking.
            if path.segments or path.pen_up_position is not None:
                moved = advance(path)
                if not math.isnan(moved[0]):
                    position = moved
            continue
        if cutting:
            strokes += 1
        # A stroke (path) is unattributed when no cutting segment of it found an
        # owner - the unit that matches the per-file "strokes" count.
        owned_cutting = 0
        # One Z cycle (plunge / z-up / dwell) per stroke, charged at the first
        # cutting segment so it follows the same attribution as that stroke's
        # cutting distance - a path that spans labels plunges once.
        cycle_charged = False
        # The gap the tool flew between the previous path's end and this path's
        # pen-up destination lands on the label it arrived at.
        if position is not None and path.pen_up_position is not None:
            gap = (
                (path.pen_up_position.x - position[0]) ** 2
                + (path.pen_up_position.y - position[1]) ** 2
            ) ** 0.5
            arrival = _point_owner(frame, path.pen_up_position.x, path.pen_up_position.y)
            arrival_label = arrival.label_id if arrival is not None else None
            if (
                arrival_label is None
                and kind == KIND_STRUCTURAL
                and _in_plate_frame(frame, path.pen_up_position.x, path.pen_up_position.y)
            ):
                arrival_label = PLATE_FRAME_LABEL
            totals.add_rapid(units_to_inches(gap), arrival_label, job_owner)
        for seg in path.segments:
            # Cutting travel follows the segment's midpoint; a rapid lead
            # follows its destination, which is where the next cut starts.
            if seg.is_cutting:
                mid_x = (seg.start.x + seg.end.x) / 2.0
                mid_y = (seg.start.y + seg.end.y) / 2.0
                owner = _point_owner(frame, mid_x, mid_y)
                x, y = mid_x, mid_y
            else:
                owner = _point_owner(frame, seg.end.x, seg.end.y)
                x, y = seg.end.x, seg.end.y
            if layer_class is not None and (
                _in_plate_frame(frame, x, y) or bool(totals.job_of_label)
            ):
                # The sheet's own border / registration mark: plate-level
                # travel, charged through the plate-frame entry of the job map
                # rather than dumped on the nearest label or left unattributed.
                # Structural files only - a text layer never draws the sheet. In
                # a JOBSET run every unslotted structural segment is furniture
                # (the sheet's first member pays for it, which is the rule
                # :func:`stroke_job` gates on); a single-job run keeps the narrow
                # in-band test, so its per-label rows do not change.
                totals.add_cutting(
                    units_to_inches(seg.length),
                    _segment_class(kind, layer_class, owner),
                    PLATE_FRAME_LABEL,
                    job_owner,
                )
                if not cycle_charged:
                    totals.add_stroke_cycle(
                        _segment_class(kind, layer_class, owner), PLATE_FRAME_LABEL, job_owner
                    )
                    cycle_charged = True
                if cutting:
                    owned_cutting += 1
                continue
            label_id = owner.label_id if owner is not None else None
            length = units_to_inches(seg.length)
            if seg.is_cutting:
                if not cycle_charged:
                    totals.add_stroke_cycle(
                        _segment_class(kind, layer_class, owner), label_id, job_owner
                    )
                    cycle_charged = True
                totals.add_cutting(
                    length, _segment_class(kind, layer_class, owner), label_id, job_owner
                )
                if label_id is not None:
                    owned_cutting += 1
            else:
                totals.add_rapid(length, label_id, job_owner)
        if cutting and owned_cutting == 0:
            totals.unattributed_strokes += 1
        # Where the tool stops is where the next path starts from.
        if path.segments or path.pen_up_position is not None:
            moved = advance(path)
            if not math.isnan(moved[0]):
                position = moved
    return strokes


def _segment_class(
    kind: Optional[str],
    layer_class: Optional[str],
    owner: Optional[_Slot],
) -> str:
    """Return the stroke class of one segment.

    Args:
        kind: The file's layer kind.
        layer_class: The path's class from the plate's pen layers, or ``None``
            for a text file.
        owner: The owning placement slot, or ``None`` when unattributed.

    Returns:
        One of :data:`DISTANCE_CLASSES`.
    """
    if owner is None:
        return UNATTRIBUTED_CLASS
    if kind == KIND_STRUCTURAL:
        return layer_class if layer_class is not None else UNATTRIBUTED_CLASS
    return RELIEF_CLASS if owner.is_relief else TEXT_CLASS


def _file_plate_number(
    stem: Any,
    plate_numbers: Sequence[int],
    frames: Mapping[int, _PlateFrame],
) -> Optional[int]:
    """Resolve which plate a per-cutter file belongs to.

    The file-name plate token is authoritative when present; a single-plate job
    (and a material that spans exactly one plate) has no token, because
    :func:`~plt_optimizer.generate.vectorize.export_per_cutter_plts` appends it
    only when a material needs disambiguating (it sits between the leading
    material tag and the cutter).

    Args:
        stem: The parsed file-name stem.
        plate_numbers: The plate numbers in report order.
        frames: The per-plate attribution frames.

    Returns:
        The 1-based plate number, or ``None`` when the file cannot be placed.
    """
    if stem.plate is not None:
        # The stem parser only records the p-marked token there, and hands back
        # its digits, so this is always a plate number (there is no unparsable
        # form to guard).
        return int(stem.plate)
    if len(plate_numbers) == 1:
        return plate_numbers[0]
    if stem.material:
        matches = [
            number
            for number in plate_numbers
            if number in frames and _format_material_tag(frames[number].material) == stem.material
        ]
        if len(matches) == 1:
            return matches[0]
    return None


def _label_meta(
    packed_plates: Sequence[Any],
    material_by_plate: Mapping[int, Optional[str]],
) -> Dict[str, Tuple[str, int, Optional[str]]]:
    """Index every placement by its spec id, plate, and material.

    Args:
        packed_plates: The packed plates, in packing order.
        material_by_plate: Stock material name per 1-based plate number.

    Returns:
        Placement id to ``(spec id, plate number, material)``.
    """
    meta: Dict[str, Tuple[str, int, Optional[str]]] = {}
    for number, plate in enumerate(packed_plates, start=1):
        material = material_by_plate.get(number, getattr(plate, "material", None))
        for placement in plate.labels:
            meta[str(placement.label_id)] = (str(placement.source_label.id), number, material)
    return meta


def _distance_rows(
    totals: _DistanceTotals,
    frames: Mapping[int, _PlateFrame],
    label_meta: Mapping[str, Tuple[str, int, Optional[str]]],
) -> Dict[str, Any]:
    """Render the measured distances as the report's ``usage.distance`` block.

    Args:
        totals: The accumulators filled by the measurement pass.
        frames: The per-plate attribution frames (for plate materials).
        label_meta: Placement id to ``(spec id, plate number, material)``.

    Returns:
        The distance block, in inches, with a foot conversion on the totals.
    """
    cutting = totals.cutting
    rapid = totals.rapid
    available = bool(totals.files)

    def plate_material(key: int) -> Optional[str]:
        frame = frames.get(key)
        return frame.material if frame is not None else None

    by_cutter_keys = sorted(
        totals.by_cutter,
        key=lambda key: (-totals.by_cutter[key][0], _sort_token(key[0]), str(key[1])),
    )
    by_material_keys = sorted(
        totals.by_material,
        key=lambda key: (-totals.by_material[key][0], _sort_token(key)),
    )
    by_job_keys = sorted(
        totals.by_job,
        key=lambda key: (-totals.by_job[key][0], _sort_token(key)),
    )
    by_plate_keys = sorted(totals.by_plate, key=lambda key: (-totals.by_plate[key][0], key))
    by_label_keys = sorted(
        totals.by_label,
        key=lambda key: (-totals.by_label[key][0], key),
    )

    return {
        "available": available,
        "note": (
            ""
            if available
            else "No PLT files were found next to the report, so the tool "
            "travel of this job could not be measured."
        ),
        "units": "inches",
        "total": {
            "cutting": _round(cutting),
            "rapid": _round(rapid),
            "total": _round(cutting + rapid),
            "cutting_ft": _round(cutting / IN_PER_FT),
            "rapid_ft": _round(rapid / IN_PER_FT),
            "total_ft": _round((cutting + rapid) / IN_PER_FT),
        },
        "by_movement": {"cutting": _round(cutting), "rapid": _round(rapid)},
        "by_class": {
            class_name: _round(totals.by_class.get(class_name, 0.0))
            for class_name in DISTANCE_CLASSES
        },
        "by_cutter": [
            {
                "cutter": cutter,
                "kind": kind,
                "files": int(totals.by_cutter[key][2]),
                "cutting": _round(totals.by_cutter[key][0]),
                "rapid": _round(totals.by_cutter[key][1]),
            }
            for key in by_cutter_keys
            for cutter, kind in (key,)
        ],
        "by_material": [
            {
                "material": key,
                "material_label": _material_label(key),
                "cutting": _round(totals.by_material[key][0]),
                "rapid": _round(totals.by_material[key][1]),
            }
            for key in by_material_keys
        ],
        # Jobset runs only (absent for a single job): the same toolpath, summed
        # per member. Every stroke has exactly one owner, so this list adds up
        # to ``total`` - the per-job numbers reconcile with the batch total.
        **(
            {
                "by_job": [
                    {
                        "job": key,
                        "cutting": _round(totals.by_job[key][0]),
                        "rapid": _round(totals.by_job[key][1]),
                        "total": _round(sum(totals.by_job[key])),
                    }
                    for key in by_job_keys
                ]
            }
            if by_job_keys
            else {}
        ),
        "by_plate": [
            {
                "plate": None if key == UNRESOLVED_PLATE else key,
                "material": plate_material(key),
                "cutting": _round(totals.by_plate[key][0]),
                "rapid": _round(totals.by_plate[key][1]),
            }
            for key in by_plate_keys
        ],
        "by_label": [
            {
                "label_id": key,
                "spec_id": label_meta.get(key, (key, UNRESOLVED_PLATE, None))[0],
                "plate": plate_or_none(label_meta.get(key, (key, UNRESOLVED_PLATE, None))[1]),
                "material": label_meta.get(key, (key, UNRESOLVED_PLATE, None))[2],
                "cutting": _round(totals.by_label[key][0]),
                "rapid": _round(totals.by_label[key][1]),
                "by_class": {
                    class_name: _round(totals.class_by_label.get(key, _zero_classes())[class_name])
                    for class_name in DISTANCE_CLASSES
                },
            }
            for key in by_label_keys
        ],
        "files": totals.files,
        "unattributed": {
            "strokes": totals.unattributed_strokes,
            "cutting": _round(totals.unattributed_cutting),
            "rapid": _round(totals.unattributed_rapid),
        },
    }


def plate_or_none(number: int) -> Optional[int]:
    """Return a plate number, or ``None`` for the unresolved sentinel.

    Args:
        number: The 1-based plate number, or :data:`UNRESOLVED_PLATE`.

    Returns:
        The plate number, or ``None``.
    """
    return None if number == UNRESOLVED_PLATE else number


def _merge_ops(
    bucket: Dict[Any, List[float]],
    key: Any,
    values: Sequence[float],
) -> None:
    """Fold one file's operation seconds into a keyed bucket.

    The per-file values are folded **unrounded** so every time bucket sums
    back to the job total exactly (the same invariant the distance buckets
    keep); only the rendered rows are rounded.

    Args:
        bucket: The accumulator map (cutter / material / plate keyed).
        key: The bucket row's key.
        values: The file's ``[cutting, rapid, plunge, z_up, dwell]`` seconds.
    """
    row = bucket.setdefault(key, _zero_ops())
    for index, value in enumerate(values):
        row[index] += value


def _distance_usage(
    plt_files: Sequence[Path],
    frames: Mapping[int, _PlateFrame],
    material_by_plate: Mapping[int, Optional[str]],
    label_meta: Mapping[str, Tuple[str, int, Optional[str]]],
    rates_by_layer: Optional[Mapping[str, _LayerRates]],
    job_of_label: Optional[Mapping[str, str]] = None,
    plate_owner_of: Optional[Mapping[int, str]] = None,
    file_jobs: Optional[Mapping[Path, Sequence[str]]] = None,
    only_job: Optional[str] = None,
) -> Tuple[_DistanceTotals, Dict[str, int]]:
    """Measure the tool travel (and run time) of the emitted per-cutter PLT files.

    Args:
        plt_files: The written per-cutter PLT paths, in write order. These are
            what the travel is measured from.
        frames: The per-plate attribution frames.
        material_by_plate: Stock material name per 1-based plate number.
        label_meta: Placement id to ``(spec id, plate number, material)``.
        rates_by_layer: Run-time rates per ``tool_options`` layer, or ``None``
            when no cutting velocity is configured (time is then not measured).
        job_of_label: Placement id to owning jobset member. Set only by a
            jobset run, which is what adds ``by_job`` to the report; a
            placement missing here is charged to :data:`UNATTRIBUTED_JOB`.
        plate_owner_of: 1-based plate number to the member charged with that
            sheet's own furniture (its border / registration marks), the
            per-plate form of the rule :meth:`_DistanceTotals.sheet_owner`
            applies. Set by a jobset run alongside :attr:`job_of_label`.
        file_jobs: PLT path to every member with strokes in that file, from
            :attr:`~plt_optimizer.generate.vectorize.PerCutterExport.file_jobs`.
            A file written for exactly ONE member attributes its
            unresolvable strokes to that member (see :func:`stroke_job`); a
            merged file's unresolvable strokes stay unattributed, since naming
            one contributor would be arbitrary. Set by a jobset run.
        only_job: Jobset member to measure - the strokes of every other member
            sharing a merged file are skipped entirely (see
            :func:`_measure_document`). ``None`` measures the whole file set,
            which is the single-job run and the batch view.

    Returns:
        ``(totals, file_count)``: the measurement accumulators, and how many
        files were measured vs written (``{"measured": n, "written": m, "failed": k}``).
    """
    totals = _DistanceTotals()
    totals.job_of_label = dict(job_of_label or {})
    totals.plate_owner_of = dict(plate_owner_of or {})
    owners_by_path = {Path(key): tuple(value) for key, value in (file_jobs or {}).items()}
    plate_numbers = sorted(frames)
    parser = PLTParser()
    measured = 0
    for plt_path in plt_files:
        path = Path(plt_path)
        stem = _parse_plt_stem(path.stem)
        plate_number = _file_plate_number(stem, plate_numbers, frames)
        frame = frames.get(plate_number) if plate_number is not None else None
        material = frame.material if frame is not None else stem.material
        cutter: Any = (
            _round(_cutter_token_to_inches(stem.cutter))
            if stem.cutter is not None
            else UNKNOWN_CUTTER
        )
        row: Dict[str, Any] = {
            "path": str(path),
            "plate": plate_number,
            "material": material,
            "cutter": cutter,
            "kind": stem.kind,
            "strokes": 0,
            "cutting": 0.0,
            "rapid": 0.0,
        }
        totals.files.append(row)
        try:
            document = parser.parse_file(path)
        except Exception as exc:  # noqa: BLE001 - metrics must never fail a job
            # A file the parser rejects is reported, not swallowed: the
            # operator sees the row with its ``error`` and an empty distance.
            row["error"] = f"{type(exc).__name__}: {exc}"
            continue
        measured += 1
        file_totals = _DistanceTotals()
        layer = PLT_TYPE_BY_KIND.get(stem.kind or "")
        rates = rates_by_layer.get(layer) if rates_by_layer is not None and layer else None
        file_totals.rates = rates
        # The per-file pass is where placements get charged, so the jobset's
        # owner map has to be visible there too - `merge` then folds the
        # per-member buckets into the job totals.
        file_totals.job_of_label = totals.job_of_label
        file_totals.plate_owner_of = totals.plate_owner_of
        file_totals.plate_number = plate_number
        owners = owners_by_path.get(path, ())
        file_totals.file_owner = owners[0] if len(owners) == 1 else None
        row["strokes"] = _measure_document(
            document,
            frame,
            stem.kind,
            file_totals,
            only_job,
            owners,
        )
        row["cutting"] = _round(file_totals.cutting)
        row["rapid"] = _round(file_totals.rapid)
        if rates is not None:
            row["z_strokes"] = file_totals.strokes
            row["by_operation"] = _ops_dict(file_totals.time_by_op)
            row["seconds"] = _round(file_totals.time_total())
            _merge_ops(totals.by_cutter_time, (cutter, stem.kind), file_totals.time_by_op)
            _merge_ops(totals.by_material_time, material, file_totals.time_by_op)
            _merge_ops(
                totals.by_plate_time,
                plate_number if plate_number is not None else UNRESOLVED_PLATE,
                file_totals.time_by_op,
            )
            totals.files_with_rates += 1
        totals.merge(file_totals)
        cutter_bucket = totals.by_cutter.setdefault((cutter, stem.kind), [0.0, 0.0, 0.0])
        cutter_bucket[0] += file_totals.cutting
        cutter_bucket[1] += file_totals.rapid
        cutter_bucket[2] += 1
        plate_bucket = totals.by_plate.setdefault(
            plate_number if plate_number is not None else UNRESOLVED_PLATE, [0.0, 0.0]
        )
        plate_bucket[0] += file_totals.cutting
        plate_bucket[1] += file_totals.rapid
        material_bucket = totals.by_material.setdefault(material, [0.0, 0.0])
        material_bucket[0] += file_totals.cutting
        material_bucket[1] += file_totals.rapid
    return totals, {"measured": measured, "written": len(plt_files)}


def _time_rows(
    totals: _DistanceTotals,
    frames: Mapping[int, _PlateFrame],
    label_meta: Mapping[str, Tuple[str, int, Optional[str]]],
    rates_by_layer: Optional[Mapping[str, _LayerRates]],
    file_count: Mapping[str, int],
) -> Dict[str, Any]:
    """Render the run-time estimate as the report's ``usage.time`` block.

    Args:
        totals: The accumulators filled by the measurement pass.
        frames: The per-plate attribution frames (for plate materials).
        label_meta: Placement id to ``(spec id, plate number, material)``.
        rates_by_layer: The rates the measurement used, per layer, reported
            verbatim in ``model`` so the estimate is auditable.
        file_count: ``{"measured": n, "written": m}`` from the measurement pass.

    Returns:
        The time block in seconds. ``available`` is false - with an explanatory
        ``note`` and no numbers - when the machine parameters could not be
        resolved, because a run time the config cannot support must never be
        reported as a zero.
    """
    measured = int(file_count.get("measured", 0))
    available = bool(rates_by_layer) and totals.files_with_rates == measured and measured > 0
    if not rates_by_layer or not available:
        note = (
            "No PLT files were measured, so the run time could not be estimated."
            if measured == 0
            else f"No {CUTTING_VELOCITY_KEY} is configured in the job-config "
            "tool_options registry, so the run time of this job cannot be estimated."
        )
        return {
            "available": False,
            "note": note,
            "units": "seconds",
            "model": {},
            "total": None,
            "by_operation": None,
            "by_cutter": [],
            "by_material": [],
            **({"by_job": []} if totals.job_of_label else {}),
            "by_plate": [],
            "by_class": {},
            "by_label": [],
            "by_spec": [],
            "unattributed": None,
        }

    ops = totals.time_by_op
    total_s = sum(ops)

    def share(seconds: float) -> float:
        """Percentage of the job total, 0.0 when there is no total."""
        return round(100.0 * seconds / total_s, 1) if total_s > 0.0 else 0.0

    def row_ops(values: Sequence[float]) -> Dict[str, Any]:
        """One bucket row: the operation split, the total and the share."""
        return {
            "by_operation": _ops_dict(values),
            "total": _round(sum(values)),
            "share": share(sum(values)),
        }

    def plate_material(key: int) -> Optional[str]:
        frame = frames.get(key)
        return frame.material if frame is not None else None

    by_cutter_keys = sorted(
        totals.by_cutter_time,
        key=lambda key: (-sum(totals.by_cutter_time[key]), _sort_token(key[0]), str(key[1])),
    )
    by_material_keys = sorted(
        totals.by_material_time,
        key=lambda key: (-sum(totals.by_material_time[key]), _sort_token(key)),
    )
    by_job_keys = sorted(
        totals.by_job_time,
        key=lambda key: (-sum(totals.by_job_time[key]), _sort_token(key)),
    )
    by_plate_keys = sorted(
        totals.by_plate_time,
        key=lambda key: (-sum(totals.by_plate_time[key]), key),
    )
    by_label_keys = sorted(
        totals.by_label_time,
        key=lambda key: (-sum(totals.by_label_time[key]), key),
    )

    # Job roll-up: the placements of one label spec (its copies, its plates and
    # every cutter that cut it) summed into a single number, which is what a
    # shop schedules against. The per-placement rows above stay, because they
    # are what ties a number back to one slot on one sheet.
    spec_ops: Dict[str, List[float]] = {}
    spec_placements: Dict[str, List[str]] = {}
    spec_plates: Dict[str, List[Any]] = {}
    spec_materials: Dict[str, List[Optional[str]]] = {}
    for key, seconds in totals.by_label_time.items():
        spec_id, plate, material = label_meta.get(key, (key, UNRESOLVED_PLATE, None))
        bucket = spec_ops.setdefault(spec_id, _zero_ops())
        for index, value in enumerate(seconds):
            bucket[index] += value
        spec_placements.setdefault(spec_id, []).append(key)
        plates = spec_plates.setdefault(spec_id, [])
        if plate not in plates:
            plates.append(plate)
        materials = spec_materials.setdefault(spec_id, [])
        if material not in materials:
            materials.append(material)
    by_spec_keys = sorted(
        spec_ops,
        key=lambda key: (-sum(spec_ops[key]), _sort_token(key)),
    )

    return {
        "available": True,
        "note": "",
        "units": "seconds",
        "model": {layer: rates.params for layer, rates in sorted(rates_by_layer.items())},
        "total": {
            **_ops_dict(ops),
            "total": _round(total_s),
            "total_hms": _hms(total_s),
            "strokes": totals.strokes,
            "files": totals.files_with_rates,
        },
        "by_operation": _ops_dict(ops),
        "by_cutter": [
            {
                "cutter": cutter,
                "kind": kind,
                "files": int(totals.by_cutter.get(key, [0.0, 0.0, 0.0])[2]),
                **row_ops(totals.by_cutter_time[key]),
            }
            for key in by_cutter_keys
            for cutter, kind in (key,)
        ],
        "by_material": [
            {
                "material": key,
                "material_label": _material_label(key),
                **row_ops(totals.by_material_time[key]),
            }
            for key in by_material_keys
        ],
        **(
            {"by_job": [{"job": key, **row_ops(totals.by_job_time[key])} for key in by_job_keys]}
            if by_job_keys
            else {}
        ),
        "by_plate": [
            {
                "plate": None if key == UNRESOLVED_PLATE else key,
                "material": plate_material(key),
                **row_ops(totals.by_plate_time[key]),
            }
            for key in by_plate_keys
        ],
        "by_class": {
            # Unlike ``distance.by_class`` (cutting only - rapid travel has no
            # stroke class), this bucket covers EVERY operation, so pen-up
            # travel shows up under the ``unattributed`` class. Its sum is the
            # full ``time.total.total``.
            class_name: _round(sum(totals.by_class_time.get(class_name, [])))
            for class_name in DISTANCE_CLASSES
        },
        "by_label": [
            {
                "label_id": key,
                "spec_id": label_meta.get(key, (key, UNRESOLVED_PLATE, None))[0],
                "plate": plate_or_none(label_meta.get(key, (key, UNRESOLVED_PLATE, None))[1]),
                "material": label_meta.get(key, (key, UNRESOLVED_PLATE, None))[2],
                **row_ops(totals.by_label_time[key]),
            }
            for key in by_label_keys
        ],
        "by_spec": [
            {
                "label_id": key,
                "plates": sorted(
                    (plate for plate in spec_plates[key]),
                    key=lambda plate: (plate is None, plate),
                ),
                "materials": sorted(m for m in spec_materials[key] if m is not None) or [None],
                "placements": sorted(spec_placements[key]),
                "copies": len(spec_placements[key]),
                **row_ops(spec_ops[key]),
            }
            for key in by_spec_keys
        ],
        "unattributed": {
            "strokes": totals.unattributed_strokes,
            **row_ops(totals.unattributed_time),
        },
    }


def _hms(seconds: Optional[float]) -> Optional[str]:
    """Render seconds as ``h:mm:ss`` for long jobs.

    Args:
        seconds: The measured seconds, or ``None``.

    Returns:
        ``"1:02:03"``, or ``None`` when ``seconds`` is ``None``.
    """
    if seconds is None:
        return None
    whole = int(round(float(seconds)))
    return f"{whole // 3600}:{whole % 3600 // 60:02d}:{whole % 60:02d}"


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def build_usage_stats(
    *,
    resolved_labels: Sequence[ResolvedLabel],
    packed_plates: Sequence[Any],
    combined_by_plate: Mapping[int, str],
    plt_files: Sequence[Path],
    material_by_plate: Mapping[int, Optional[str]],
    job_spec: Any = None,
    job_config: Any = None,
    job_of_label: Optional[Mapping[str, str]] = None,
    plate_owner_of: Optional[Mapping[int, str]] = None,
    file_jobs: Optional[Mapping[Path, Sequence[str]]] = None,
    attribution_labels: Optional[Sequence[ResolvedLabel]] = None,
    attribution_plates: Optional[Sequence[Any]] = None,
    only_job: Optional[str] = None,
) -> Dict[str, Any]:
    """Aggregate the job's production metrics into the report's ``usage`` block.

    Counts and measurement are two separate views of the run: the COUNTS side
    (labels / characters / holes / material) is always ``resolved_labels`` and
    ``packed_plates``, while the ATTRIBUTION side (the per-plate slot frames every
    measured stroke is resolved against, and the label id -> plate map) is built
    from the ``attribution_labels`` / ``attribution_plates`` pool.
    A single job, and a jobset's batch view, need both pools to be the same set,
    so they pass nothing; a jobset MEMBER report passes the batch's pool,
    because the export merged every job engraving with a tool into one shared
    file whose strokes belong to several jobs (see ``only_job``).

    Args:
        resolved_labels: The job's resolved labels, in content order - the
            counting pool.
        packed_plates: The packed plates from the layout stage, in packing
            order (the same objects the export reports) - the counting pool.
        combined_by_plate: The export's in-memory combined per-plate content,
            keyed by 1-based plate number. The per-label section split and the
            border/hole split are read from it.
        plt_files: The written per-cutter PLT paths, in write order. The tool
            travel is measured from these files.
        material_by_plate: Stock material name per 1-based plate number. The
            plate numbers are the ATTRIBUTION pool's (a merged file's name
            carries the batch's plate number), and a plate missing from it
            falls back to the plate object's own material.
        job_spec: The resolved job spec, used with ``job_config`` to resolve
            the ``tool_options`` rates of the run-time estimate. ``None`` (or
            ``job_config=None``) omits the ``time`` numbers entirely. It also
            resolves the per-plate ``material_weight_per_square_inch`` density
            of the weight estimate; ``None`` there omits the ``weight`` numbers.
        job_config: The loaded job config (the ``tool_options`` registry plus
            the ``rapid_velocity`` estimate field).
        job_of_label: Placement id to owning jobset member (the composite-id
            side table of :func:`plt_optimizer.generate.jobset.build_jobset_plan`
            applied to the packing). Set by a jobset run only: it adds the
            ``by_job`` breakdown to both the ``distance`` and ``time`` blocks.
        attribution_labels: The stroke-attribution pool of resolved labels - the
            label set the measured strokes are classified in (a relief slot is
            one the pool runs as relief). ``None`` uses ``resolved_labels``.
        attribution_plates: The stroke-attribution pool of packed plates, whose
            slots every measured stroke must resolve against. ``None`` uses
            ``packed_plates``.
        only_job: Jobset member to measure strokes for: a stroke owned by any
            other member is skipped entirely (no distance, stroke, class,
            cutter, material, plate, label, time, and nothing counted as
            unattributed). The counts above stay the member's own, so summing
            the members reproduces the batch view exactly. ``None`` measures
            every stroke.

    Returns:
        The ``usage`` mapping: ``labels``, ``characters``, ``holes``,
        ``material``, ``weight``, ``distance`` and ``time`` blocks, JSON-ready.
    """
    attr_labels = list(resolved_labels if attribution_labels is None else attribution_labels)
    attr_plates = list(packed_plates if attribution_plates is None else attribution_plates)
    relief_ids = frozenset(str(label.id) for label in attr_labels if label.relief_engraving)
    # The counts side enumerates the counting pool, whose positions are NOT the
    # attribution pool's plate numbers when the pools differ (a member's plates
    # are a renumbered subset of the batch's). A map keyed by the batch's
    # numbering must never be read by position, so a split run takes each
    # counting plate's material from the plate object - the same value the
    # export stamped it with.
    counts_materials: Dict[int, Optional[str]] = (
        {
            number: getattr(plate, "material", None)
            for number, plate in enumerate(packed_plates, start=1)
        }
        if attribution_plates is not None
        else dict(material_by_plate)
    )
    frames: Dict[int, _PlateFrame] = {}
    for number, plate in enumerate(attr_plates, start=1):
        combined = combined_by_plate.get(number)
        frame = _PlateFrame(
            plate_number=number,
            material=material_by_plate.get(number, getattr(plate, "material", None)),
            slots=_slots_for(list(getattr(plate, "labels", ()) or ()), relief_ids),
        )
        if combined:
            frame.stroke_class = _structural_classes(combined)
            frame.plate_frame = _plate_frame_rects(
                combined,
                frame.stroke_class,
                frame.slots,
                _plate_size_inches(plate),
            )
        frames[number] = frame
    rates_by_layer: Dict[str, _LayerRates] = {}
    for plt_type in ("text", "borders_holes"):
        rates = _build_layer_rates(job_spec, job_config, plt_type)
        if rates is not None:
            rates_by_layer[plt_type] = rates
    label_meta = _label_meta(attr_plates, material_by_plate)
    totals, file_count = _distance_usage(
        plt_files=plt_files,
        frames=frames,
        material_by_plate=material_by_plate,
        label_meta=label_meta,
        rates_by_layer=rates_by_layer or None,
        job_of_label=job_of_label,
        plate_owner_of=plate_owner_of,
        file_jobs=file_jobs,
        only_job=only_job,
    )
    return {
        "labels": _labels_usage(resolved_labels),
        "characters": _characters_usage(resolved_labels),
        "holes": _holes_usage(resolved_labels),
        "material": _material_usage(packed_plates, counts_materials, resolved_labels),
        "layout": _layout_usage(packed_plates, counts_materials),
        "weight": _weight_usage(packed_plates, counts_materials, job_spec),
        "distance": _distance_rows(totals, frames, label_meta),
        "time": _time_rows(totals, frames, label_meta, rates_by_layer or None, file_count),
    }


# ==============================================================================
# summary.txt rendering
# ==============================================================================


def _fmt3(value: Any) -> str:
    """Render a measurement with three decimals.

    Args:
        value: The number, or ``None``.

    Returns:
        The value as ``0.000``, or :data:`MISSING_TEXT` when absent.
    """
    if value is None:
        return MISSING_TEXT
    return f"{float(value):.3f}"


def _fmt_cutter(value: Any) -> str:
    """Render a cutter bucket key for ``summary.txt``.

    Args:
        value: The cutter diameter, ``None`` (no cutter configured), or
            :data:`UNKNOWN_CUTTER` for a file whose cutter is not in the job.

    Returns:
        The display tag.
    """
    if value is None:
        return "none"
    if isinstance(value, str):
        return value
    return _fmt3(value)


def _rows(usage: Mapping[str, Any], section: str, key: str) -> List[Dict[str, Any]]:
    """Return one bucket list of a usage section.

    Args:
        usage: The ``usage`` block.
        section: The section name (``labels``, ``characters``, ...).
        key: The bucket list's key (``by_material``, ``by_class``, ...).

    Returns:
        The rows, empty when the section is absent.
    """
    rows: List[Dict[str, Any]] = list(usage.get(section, {}).get(key, ()) or ())
    return rows


def _join(parts: Sequence[str]) -> str:
    """Join rendered bucket entries for one line.

    Args:
        parts: The rendered entries.

    Returns:
        The parts joined by two spaces, or :data:`MISSING_TEXT` when empty.
    """
    return "  ".join(parts) if parts else MISSING_TEXT


def _plural(count: int, singular: str, plural: Optional[str] = None) -> str:
    """Render a count's noun with English pluralisation.

    Args:
        count: The number of things.
        singular: The singular noun.
        plural: The irregular plural, when the noun is not ``noun + "s"``.

    Returns:
        The noun in the number matching ``count``.
    """
    if count == 1:
        return singular
    return plural if plural is not None else f"{singular}s"


def format_usage_lines(usage: Mapping[str, Any]) -> List[str]:
    """Render the ``usage`` block as ``summary.txt`` lines.

    Every line is indented a multiple of two spaces so the section stays
    valid inside the ``=`` fenced blocks :func:`
    ~plt_optimizer.generate.generation_report.format_summary` emits. The
    per-label distance rows are capped at
    :data:`SUMMARY_USAGE_LABEL_LIMIT` with a pointer to ``report.json``,
    which carries the complete record.

    Args:
        usage: The ``usage`` block of a generation report (the JSON-shaped
            mapping built by :func:`build_usage_stats`).

    Returns:
        The lines of the "Job usage:" section, in report order.
    """
    lines: List[str] = ["Job usage:"]
    lines.extend(_labels_lines(usage))
    lines.extend(_characters_lines(usage))
    lines.extend(_holes_lines(usage))
    lines.extend(_material_lines(usage))
    lines.extend(_layout_lines(usage))
    lines.extend(_weight_lines(usage))
    lines.extend(_distance_lines(usage))
    lines.extend(_time_lines(usage))
    return lines


def _layout_lines(usage: Mapping[str, Any]) -> List[str]:
    """Render the ``usage.layout`` block.

    Args:
        usage: The ``usage`` block.

    Returns:
        The section's lines (absent when the run recorded no layout).
    """
    layout = usage.get("layout")
    if not layout:
        return []
    total = layout.get("total", {})
    lines = [
        f"  layout: {total.get('plates', 0)} {_plural(int(total.get('plates', 0)), 'plate')}  "
        f"coverage {_fmt3(total.get('coverage_pct'))}%  "
        f"rotated {total.get('rotated_labels', 0)}  "
        f"empty {_fmt3(total.get('empty_sq_in'))} in2"
    ]
    lines.extend(
        f"    plate {row.get('plate')}: {row.get('label_instances', 0)} labels  "
        f"coverage {_fmt3(row.get('coverage_pct'))}%  "
        f"empty {_fmt3(row.get('empty_sq_in'))} in2  "
        f"rotated {row.get('rotated_labels', 0)}  "
        f"[{row.get('material_label') or _material_label(row.get('material'))}]"
        for row in _rows(usage, "layout", "by_plate")
    )
    return lines


def _counts(rows: Sequence[Dict[str, Any]], key: str) -> str:
    """Render ``name=count`` pairs from a breakdown list.

    Args:
        rows: The bucket rows.
        key: The row's bucket-name field.

    Returns:
        The pairs joined by two spaces, or :data:`MISSING_TEXT` when empty.
    """
    return _join([f"{row.get(key)}={row.get('count')}" for row in rows])


def _labels_lines(usage: Mapping[str, Any]) -> List[str]:
    """Render the ``usage.labels`` block.

    Args:
        usage: The ``usage`` block.

    Returns:
        The section's lines.
    """
    labels = usage.get("labels", {})
    lines = [
        f"  labels: {labels.get('instances', 0)} "
        f"{_plural(int(labels.get('instances', 0)), 'label')} "
        f"({labels.get('specs', 0)} {_plural(int(labels.get('specs', 0)), 'spec')})  "
        + _counts(_rows(usage, "labels", "by_shape"), "shape")
        + "  "
        + _counts(_rows(usage, "labels", "by_relief"), "engraving")
    ]
    lines.append(
        "    by material: " + _counts(_rows(usage, "labels", "by_material"), "material_label")
    )
    lines.append(
        "    by cutter: "
        + _join(
            [
                f"{_fmt_cutter(row.get('cutter_diameter'))} x{row.get('count')}"
                for row in _rows(usage, "labels", "by_cutter")
            ]
        )
    )
    lines.append(
        "    by size: "
        + _join(
            [
                f"{_fmt3(row.get('width'))}x{_fmt3(row.get('height'))} {row.get('shape')} "
                f"{_material_label(row.get('material'))} x{row.get('count')}"
                for row in _rows(usage, "labels", "by_size")
            ]
        )
    )
    return lines


def _height_band_label(row: Mapping[str, Any]) -> str:
    """Render a text-height band as ``nominal (engraved)``.

    The parenthetical is the cutter-diameter-adjusted (compressed/downsized)
    height the machine actually cuts, and it is dropped when it matches the
    nominal size - an unadjusted line reads ``0.25``, an adjusted one
    ``0.25 (0.205)``.

    Args:
        row: A ``by_text_height`` row.

    Returns:
        The display text (without the count).
    """
    engraved = _fmt3(row.get("text_height"))
    nominal = row.get("nominal_text_height")
    if nominal is None:
        return engraved
    nominal_text = _fmt3(nominal)
    if nominal_text == engraved:
        return nominal_text
    return f"{nominal_text} ({engraved})"


def _characters_lines(usage: Mapping[str, Any]) -> List[str]:
    """Render the ``usage.characters`` block.

    Args:
        usage: The ``usage`` block.

    Returns:
        The section's lines.
    """
    characters = usage.get("characters", {})
    lines = [
        f"  characters: {characters.get('total', 0)} engraved "
        f"{_plural(int(characters.get('total', 0)), 'glyph', 'glyphs')} "
        f"({characters.get('spaces_excluded', 0)} space(s) excluded; "
        f"{characters.get('markers', 0)} list-marker "
        f"{_plural(int(characters.get('markers', 0)), 'glyph', 'glyphs')} counted separately)"
    ]
    lines.append(
        "    by text height: "
        + _join(
            [
                f"{_height_band_label(row)} x{row.get('count')}"
                for row in _rows(usage, "characters", "by_text_height")
            ]
        )
    )
    return lines


def _holes_lines(usage: Mapping[str, Any]) -> List[str]:
    """Render the ``usage.holes`` block.

    Args:
        usage: The ``usage`` block.

    Returns:
        The section's lines.
    """
    holes = usage.get("holes", {})
    lines = [f"  drill holes: {holes.get('total', 0)}"]
    lines.append(
        "    by diameter: "
        + _join(
            [
                f"{_fmt3(row.get('diameter'))} (engraved {_fmt3(row.get('engraved_diameter'))}) "
                f"x{row.get('count')}"
                for row in _rows(usage, "holes", "by_diameter")
            ]
        )
    )
    return lines


def _material_lines(usage: Mapping[str, Any]) -> List[str]:
    """Render the ``usage.material`` block.

    Args:
        usage: The ``usage`` block.

    Returns:
        The section's lines.
    """
    material = usage.get("material", {})
    total = material.get("total", {})
    plates = int(total.get("plates", 0))
    lines = [
        f"  material: {plates} {_plural(plates, 'plate')}  "
        f"{_fmt3(total.get('sq_in'))} sq in ({_fmt3(total.get('sq_ft'))} sq ft)"
    ]
    for row in _rows(usage, "material", "by_material"):
        row_plates = int(row.get("plates", 0))
        lines.append(
            f"    {row.get('material_label')}: {row_plates} {_plural(row_plates, 'plate')}  "
            f"{_fmt3(row.get('sq_in'))} sq in ({_fmt3(row.get('sq_ft'))} sq ft)  "
            f"{row.get('label_instances')} label(s)  "
            f"boxes {_fmt3(row.get('label_bbox_sq_in'))} sq in"
        )
    nominal = material.get("label_nominal_area", {})
    bbox = material.get("label_bbox_area", {})
    lines.append(
        f"    label nominal area: {_fmt3(nominal.get('sq_in'))} sq in "
        f"({_fmt3(nominal.get('sq_ft'))} sq ft)"
    )
    lines.append(
        f"    label bounding-box area: {_fmt3(bbox.get('sq_in'))} sq in "
        f"({_fmt3(bbox.get('sq_ft'))} sq ft)"
    )
    return lines


def _trim(value: float, decimals: int) -> str:
    """Format a fixed-decimal number with trailing zeros stripped.

    Args:
        value: The number.
        decimals: The maximum number of decimals to show.

    Returns:
        ``1.5`` for ``(1.500, 3)``, ``2`` for ``(2.0, 2)``, ``0.00260417`` for
        a value needing every digit.
    """
    text = f"{value:.{decimals}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _fmt_lb(value: Any) -> str:
    """Render a weight in pounds with an ounce part for readability.

    A shop weighing labels thinks in pounds and ounces, so a value of 1.25 lb
    reads ``1 lb 4 oz (1.250 lb)``: the compound form first, the exact decimal
    in parentheses. A sub-pound weight drops the (always zero) pound part
    (``0.67 oz (0.042 lb)``).

    Args:
        value: The pounds, or ``None`` (weight not estimated).

    Returns:
        The rendered weight, or :data:`MISSING_TEXT` when absent.
    """
    if value is None:
        return MISSING_TEXT
    pounds = float(value)
    if pounds <= 0.0:
        return "0.000 lb"
    if pounds < 1.0:
        return f"{_trim(pounds * OZ_PER_LB, 2)} oz ({pounds:.3f} lb)"
    whole = int(pounds)
    ounces = (pounds - whole) * OZ_PER_LB
    if ounces <= 0.0:
        return f"{whole} lb 0 oz ({pounds:.3f} lb)"
    return f"{whole} lb {_trim(ounces, 2)} oz ({pounds:.3f} lb)"


def _fmt_density(value: Any) -> str:
    """Render a lb/in2 density exactly as configured.

    Args:
        value: The density, or ``None`` (unconfigured plate).

    Returns:
        The shortest representation that round-trips (never the 3-decimal
        rounding :func:`_fmt3` applies to measurements, which would print the
        shop's ``0.00260417`` as ``0.003``), or :data:`MISSING_TEXT`.
    """
    if value is None:
        return MISSING_TEXT
    return str(float(value))


def _weight_lines(usage: Mapping[str, Any]) -> List[str]:
    """Render the ``usage.weight`` block.

    Args:
        usage: The ``usage`` block.

    Returns:
        The section's lines.
    """
    block = usage.get("weight", {})
    lines = ["  weight (estimated, from the labels' nominal area x the plate density):"]
    if not block.get("available"):
        note = block.get("note")
        lines.append(f"    Not estimated: {note}" if note else "    Not estimated.")
        return lines
    total = block.get("total") or {}
    lines.append(
        f"    total: {_fmt_lb(total.get('lb'))}  {total.get('label_instances', 0)} label(s)  "
        f"{_fmt3(total.get('label_sq_in'))} sq in of material"
    )
    lines.append(
        "    by material: "
        + _join(
            [
                f"{row.get('material_label')} {_fmt_lb(row.get('lb'))}"
                for row in _rows(usage, "weight", "by_material")
            ]
        )
    )
    lines.append(
        "    by plate: "
        + _join(
            [
                f"{row.get('plate')} ({row.get('material_label')}, "
                f"{_fmt_density(row.get('material_weight_per_square_inch'))} lb/in2) "
                f"{_fmt_lb(row.get('lb'))} x{row.get('label_instances')}"
                for row in _rows(usage, "weight", "by_plate")
            ]
        )
    )
    per_label = _rows(usage, "weight", "by_label")
    lines.append("    per label (per spec on one plate):")
    for row in per_label[:SUMMARY_USAGE_LABEL_LIMIT]:
        lines.append(
            f"      {row.get('spec_id')}  plate {row.get('plate')}  "
            f"{_material_label(row.get('material'))}  x{row.get('instances')}  "
            f"{_fmt_lb(row.get('lb'))}"
        )
    remaining = len(per_label) - SUMMARY_USAGE_LABEL_LIMIT
    if remaining > 0:
        lines.append(f"      ... and {remaining} more (see report.json)")
    unconfigured = block.get("unconfigured") or {}
    if int(unconfigured.get("label_instances", 0)) > 0:
        lines.append(
            f"    unconfigured: {unconfigured.get('label_instances')} label(s)  "
            f"{_fmt3(unconfigured.get('label_sq_in'))} sq in on plates with no density "
            f"(excluded from the weights above)"
        )
    lines.append(
        "    Estimated from material_weight_per_square_inch; not a measured weight,\n"
        "      and it covers the labels only, not the sheet they are cut from.\n"
        "      Sheet weight = usage.material area x the same density."
    )
    return lines


def _distance_lines(usage: Mapping[str, Any]) -> List[str]:
    """Render the ``usage.distance`` block.

    Args:
        usage: The ``usage`` block.

    Returns:
        The section's lines.
    """
    distance = usage.get("distance", {})
    lines = ["  tool distance (inches, measured from the emitted PLT files):"]
    files = _rows(usage, "distance", "files")
    if not files:
        lines.append("    Not measured: the job produced no PLT files to read back.")
        return lines
    total = distance.get("total", {})
    lines.append(
        f"    total: cutting {_fmt3(total.get('cutting'))} + rapid "
        f"{_fmt3(total.get('rapid'))} = {_fmt3(total.get('total'))} "
        f"({_fmt3(total.get('total_ft'))} ft)"
    )
    by_class = distance.get("by_class", {})
    lines.append(
        "    by class: "
        + _join([f"{name} {_fmt3(by_class.get(name))}" for name in DISTANCE_CLASSES])
    )
    lines.append(
        "    by cutter: "
        + _join(
            [
                f"{_fmt_cutter(row.get('cutter'))} {row.get('kind')} "
                f"[{row.get('files')} file(s)] cut {_fmt3(row.get('cutting'))} "
                f"rapid {_fmt3(row.get('rapid'))}"
                for row in _rows(usage, "distance", "by_cutter")
            ]
        )
    )
    lines.append(
        "    by material: "
        + _join(
            [
                f"{row.get('material_label')} cut {_fmt3(row.get('cutting'))} "
                f"rapid {_fmt3(row.get('rapid'))}"
                for row in _rows(usage, "distance", "by_material")
            ]
        )
    )
    lines.append(
        "    by plate: "
        + _join(
            [
                f"{row.get('plate')} cut {_fmt3(row.get('cutting'))} "
                f"rapid {_fmt3(row.get('rapid'))}"
                for row in _rows(usage, "distance", "by_plate")
            ]
        )
    )
    per_label = _rows(usage, "distance", "by_label")
    lines.append("    per label:")
    for row in per_label[:SUMMARY_USAGE_LABEL_LIMIT]:
        row_classes = row.get("by_class", {})
        parts = [
            f"      {row.get('label_id')}"
            + ("" if row.get("spec_id") == row.get("label_id") else f" ({row.get('spec_id')})")
            + f"  plate {row.get('plate')}  {_material_label(row.get('material'))}"
            + f"  cut {_fmt3(row.get('cutting'))}  rapid {_fmt3(row.get('rapid'))}"
        ]
        parts.extend(
            f"  {name} {_fmt3(row_classes.get(name))}"
            for name in DISTANCE_CLASSES
            if float(row_classes.get(name, 0.0) or 0.0) > 0.0
        )
        lines.append("".join(parts))
    remaining = len(per_label) - SUMMARY_USAGE_LABEL_LIMIT
    if remaining > 0:
        lines.append(f"      ... and {remaining} more (see report.json)")
    unattributed = distance.get("unattributed", {})
    if int(unattributed.get("strokes", 0)) > 0:
        lines.append(
            f"    unattributed: {unattributed.get('strokes')} stroke(s)  "
            f"cut {_fmt3(unattributed.get('cutting'))}  rapid {_fmt3(unattributed.get('rapid'))}"
        )
    return lines


def _fmt_time(value: Any) -> str:
    """Render a time with three decimals and the unit.

    Args:
        value: The seconds, or ``None``.

    Returns:
        The value as ``0.000 s``, or :data:`MISSING_TEXT` when absent.
    """
    return MISSING_TEXT if value is None else f"{_fmt3(value)} s"


def _fmt_hms(value: Any) -> str:
    """Render seconds as a compact, human-readable duration.

    Leading zero units are dropped so a shop can read the number at a glance
    (3857.321 -> ``1h4m17s``, 1347.340 -> ``22m27s``, 45.2 -> ``45s``); an
    interior zero stays (3600 -> ``1h0m0s``) and the seconds field is always
    present. Whole seconds are rounded, like :func:`_hms`.

    Args:
        value: The seconds, or ``None``.

    Returns:
        The duration as ``XXhYYmZZs`` with the zero units omitted, or
        :data:`MISSING_TEXT` when absent.
    """
    if value is None:
        return MISSING_TEXT
    hours, rest = divmod(int(round(float(value))), 3600)
    minutes, seconds = divmod(rest, 60)
    text = f"{hours}h" if hours else ""
    if minutes or hours:
        text += f"{minutes}m"
    return f"{text}{seconds}s"


def _time_lines(usage: Mapping[str, Any]) -> List[str]:
    """Render the ``usage.time`` block.

    Args:
        usage: The ``usage`` block.

    Returns:
        The section's lines.
    """
    time_block = usage.get("time", {})
    lines = ["  run time (seconds, estimated from the emitted tool_options):"]
    if not time_block.get("available"):
        note = time_block.get("note")
        lines.append(f"    Not estimated: {note}" if note else "    Not estimated.")
        return lines
    total = time_block.get("total", {})
    operations = time_block.get("by_operation", {})
    lines.append(
        "    total: "
        + " + ".join(f"{name} {_fmt3(operations.get(name))}" for name in TIME_OPERATIONS)
        + f" = {_fmt_time(total.get('total'))} ({total.get('total_hms')})"
    )
    lines.append(f"    strokes: {total.get('strokes')} (one Z cycle each)")
    by_class = time_block.get("by_class", {})
    lines.append(
        "    by class: "
        + _join([f"{name} {_fmt3(by_class.get(name))}" for name in DISTANCE_CLASSES])
    )
    lines.append(
        "    by cutter: "
        + _join(
            [
                f"{_fmt_cutter(row.get('cutter'))} {row.get('kind')} "
                f"[{row.get('files')} file(s)] {_fmt_time(row.get('total'))}"
                f" ({row.get('share')}%)"
                for row in _rows(usage, "time", "by_cutter")
            ]
        )
    )
    lines.append(
        "    by material: "
        + _join(
            [
                f"{row.get('material_label')} {_fmt_time(row.get('total'))} ({row.get('share')}%)"
                for row in _rows(usage, "time", "by_material")
            ]
        )
    )
    lines.append(
        "    by plate: "
        + _join(
            [
                f"{row.get('plate')} {_fmt_time(row.get('total'))} "
                f"({_fmt_hms(row.get('total'))}; {row.get('share')}%)"
                for row in _rows(usage, "time", "by_plate")
            ]
        )
    )
    per_label = _rows(usage, "time", "by_label")
    lines.append("    per label (per placement on one plate):")
    for row in per_label[:SUMMARY_USAGE_LABEL_LIMIT]:
        row_ops = row.get("by_operation", {})
        parts = [
            f"      {row.get('label_id')}"
            + ("" if row.get("spec_id") == row.get("label_id") else f" ({row.get('spec_id')})")
            + f"  plate {row.get('plate')}  {_material_label(row.get('material'))}"
            + f"  {_fmt_time(row.get('total'))}  ({row.get('share')}%)"
        ]
        parts.extend(
            f"  {name} {_fmt3(row_ops.get(name))}"
            for name in TIME_OPERATIONS
            if float(row_ops.get(name, 0.0) or 0.0) > 0.0
        )
        lines.append("".join(parts))
    remaining = len(per_label) - SUMMARY_USAGE_LABEL_LIMIT
    if remaining > 0:
        lines.append(f"      ... and {remaining} more (see report.json)")
    per_spec = _rows(usage, "time", "by_spec")
    if per_spec:
        lines.append("    per job (one label summed over its plates, copies and cutters):")
        for row in per_spec[:SUMMARY_USAGE_LABEL_LIMIT]:
            plates = ", ".join(str(plate) for plate in row.get("plates", []))
            materials = ", ".join(
                _material_label(material) for material in row.get("materials", [])
            )
            lines.append(
                f"      {row.get('label_id')}  plate(s) {plates}  {materials}  "
                f"{row.get('copies')} placement(s)  {_fmt_time(row.get('total'))}"
                f"  ({row.get('share')}%)"
            )
        spec_remaining = len(per_spec) - SUMMARY_USAGE_LABEL_LIMIT
        if spec_remaining > 0:
            lines.append(f"      ... and {spec_remaining} more (see report.json)")
    unattributed = time_block.get("unattributed") or {}
    if int(unattributed.get("strokes", 0)) > 0:
        lines.append(
            f"    unattributed: {unattributed.get('strokes')} stroke(s)  "
            f"{_fmt_time(unattributed.get('total'))}"
        )
    lines.append(
        "    Estimated from tool_options (cutting/rapid velocity, plunge, z-up, "
        "dwell);\n      excludes tool changes, setup, and loading."
    )
    return lines
