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
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from plt_optimizer.core.models import ArcSegment, PLTDocument, StrokePath
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
        plate_frame: Boxes of the structural contours that belong to the sheet
            itself - the plate's border and registration marks, drawn outside
            every placement slot. Travel on them is plate-level: it is charged
            to :data:`PLATE_FRAME_LABEL`, which keeps it out of the per-label
            and per-job attribution without leaving the totals.
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
) -> List[_Rect]:
    """Box the structural contours that belong to the sheet, not to a label.

    A plate can carry its own border and registration marks: contours drawn in
    the boundary/hole layers outside every placement slot. They are the plate's
    furniture, so the travel that engraves them belongs to no label - and, in a
    jobset, to no member. Identifying them keeps a member measured from its own
    files on the same footing as the batch's per-member rows.

    Args:
        combined: The plate's combined (all pens) PLT content.
        stroke_class: The plate's structural signature map (see
            :func:`_structural_classes`).
        slots: The plate's placement slots.

    Returns:
        The padded box of every structural contour that no slot contains.
    """
    if not stroke_class:
        return []
    parser = PLTParser()
    rects: List[_Rect] = []
    for path in parser.parse_string(combined).stroke_paths:
        if not any(seg.is_cutting for seg in path.segments):
            continue
        if path.pen_up_position is None and not path.segments:
            continue
        xs: List[float] = []
        ys: List[float] = []
        for seg in path.segments:
            if not seg.is_cutting:
                continue
            xs.extend((seg.start.x, seg.end.x))
            ys.extend((seg.start.y, seg.end.y))
        if not xs:
            continue
        box = _Rect(min(xs), min(ys), max(xs), max(ys))
        if _path_signature(path) not in stroke_class:
            continue
        point = _Rect(*box.center, *box.center)
        pad = inches_to_units(SLOT_TOL_IN)
        slot_boxes = [slot.rect.padded(pad) for slot in slots]
        if any(slot_box.covers(point) for slot_box in slot_boxes):
            continue
        # The frame is the furniture strip, never a label's interior: a plate
        # border is one contour whose box spans the whole sheet, so boxing it
        # raw would swallow every text stroke on the plate. Cut the padded
        # slots out of it - only travel outside the placements counts as
        # plate-level.
        rects.extend(box.padded(inches_to_units(PLATE_FRAME_PAD_IN)).pieces_excluding(slot_boxes))
    return rects


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
    ``(placement.x, placement.y)`` -- no mirroring and no Y flip (see the
    ``[x, x+H] x [y, y+W]`` slot comment there for a rotated placement). So the
    slot of a placement is exactly ``[x, x+width] x [y, y+height]`` in the
    emitted content's frame, in inches.

    Args:
        x: Slot origin X in inches (from the plate frame's origin).
        y: Slot origin Y in inches (from the plate frame's origin).
        width: The label's nominal width in inches (unrotated).
        height: The label's nominal height in inches (unrotated).
        rotated: Whether the packer placed the label sideways.

    Returns:
        The slot rectangle in device plotter units.
    """
    origin_x = inches_to_units(x)
    origin_y = inches_to_units(y)
    span_w = inches_to_units(width)
    span_h = inches_to_units(height)
    # A sideways placement occupies the swapped slot [x, x + H] x [y, y + W].
    x_span, y_span = (span_h, span_w) if rotated else (span_w, span_h)
    return _Rect(origin_x, origin_y, origin_x + x_span, origin_y + y_span)


def _slots_for(placements: Sequence[Any]) -> List[_Slot]:
    """Build the slot list of one plate's placements.

    Args:
        placements: The plate's ``PackedLabel`` placements, in packing order.

    Returns:
        One slot per placement, in packing order.
    """
    slots: List[_Slot] = []
    for placement in placements:
        source = getattr(placement, "source_label", None)
        slots.append(
            _Slot(
                label_id=str(getattr(placement, "label_id", "") or ""),
                spec_id=str(getattr(source, "id", "") or ""),
                rect=_slot_rect(
                    float(placement.x),
                    float(placement.y),
                    float(placement.width),
                    float(placement.height),
                    bool(placement.rotated),
                ),
                is_relief=bool(getattr(source, "relief_engraving", False)),
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
    time_by_op: List[float] = field(default_factory=_zero_ops)
    by_label_time: Dict[str, List[float]] = field(default_factory=dict)
    by_class_time: Dict[str, List[float]] = field(default_factory=dict)
    by_cutter_time: Dict[Tuple[Any, Any], List[float]] = field(default_factory=dict)
    by_material_time: Dict[Any, List[float]] = field(default_factory=dict)
    by_job_time: Dict[str, List[float]] = field(default_factory=dict)
    by_plate_time: Dict[int, List[float]] = field(default_factory=dict)
    unattributed_time: List[float] = field(default_factory=_zero_ops)
    files_with_rates: int = 0

    def add_cutting(self, value: float, stroke_class: str, label_id: Optional[str]) -> None:
        """Record a cutting distance.

        Args:
            value: Distance in inches.
            stroke_class: One of :data:`DISTANCE_CLASSES`.
            label_id: The owning placement id, or ``None`` when unattributed.
        """
        self.cutting += value
        self.by_class[stroke_class] = self.by_class.get(stroke_class, 0.0) + value
        if self.rates is not None:
            self._add_time(0, value * self.rates.cutting, stroke_class, label_id)
        if label_id is None:
            self.unattributed_cutting += value
            self._add_job(None, cutting=value)
            return
        self.by_label.setdefault(label_id, [0.0, 0.0])[0] += value
        self._add_job(label_id, cutting=value)
        row = self.class_by_label.setdefault(label_id, _zero_classes())
        row[stroke_class] = row.get(stroke_class, 0.0) + value

    def add_rapid(self, value: float, label_id: Optional[str]) -> None:
        """Record a rapid (pen-up) distance, charged to its destination label.

        Args:
            value: Distance in inches.
            label_id: The owning placement id, or ``None`` when unattributed.
        """
        self.rapid += value
        if self.rates is not None:
            self._add_time(1, value * self.rates.rapid, UNATTRIBUTED_CLASS, label_id)
        if label_id is None:
            self.unattributed_rapid += value
            self._add_job(None, rapid=value)
            return
        self.by_label.setdefault(label_id, [0.0, 0.0])[1] += value
        self._add_job(label_id, rapid=value)

    def add_stroke_cycle(self, stroke_class: str, label_id: Optional[str]) -> None:
        """Record one stroke's pen-down -> pen-up Z cycle (plunge, z-up, dwell).

        Called once per stroke, at the stroke's first cutting segment, so the
        cycle follows the same attribution as the cutting distance (a
        multi-subpath path plunges once and is charged once).

        Args:
            stroke_class: The class of that first cutting segment.
            label_id: The owning placement id, or ``None`` when unattributed.
        """
        if self.rates is None:
            return
        rates = self.rates
        self.strokes += 1
        # ``_add_time`` owns the operation accumulator - adding the terms here
        # as well would charge every stroke's Z cycle twice.
        for index, value in ((2, rates.plunge), (3, rates.z_up), (4, rates.dwell)):
            self._add_time(index, value, stroke_class, label_id)

    def _add_job(
        self, label_id: Optional[str], *, cutting: float = 0.0, rapid: float = 0.0
    ) -> None:
        """Charge a distance to the jobset member that owns the placement.

        A single-job run (empty :attr:`job_of_label`) keeps the map empty, so
        the report's ``by_job`` block stays absent exactly as before.

        Args:
            label_id: The owning placement id, or ``None`` when unattributed.
            cutting: Pen-down inches to add.
            rapid: Pen-up inches to add.
        """
        if not self.job_of_label:
            return
        owner = self.job_of_label.get(label_id, UNATTRIBUTED_JOB) if label_id else UNATTRIBUTED_JOB
        bucket = self.by_job.setdefault(owner, [0.0, 0.0])
        bucket[0] += cutting
        bucket[1] += rapid

    def _add_time(
        self,
        index: int,
        value: float,
        stroke_class: str,
        label_id: Optional[str],
    ) -> None:
        """Add seconds to the operation, per-label and per-class accumulators.

        Args:
            index: The :data:`TIME_OPERATIONS` index of the operation.
            value: Seconds.
            stroke_class: The stroke class charged.
            label_id: The owning placement id, or ``None`` when unattributed.
        """
        self.time_by_op[index] += value
        class_row = self.by_class_time.setdefault(stroke_class, _zero_ops())
        class_row[index] += value
        if self.job_of_label:
            owner = (
                self.job_of_label.get(label_id, UNATTRIBUTED_JOB) if label_id else UNATTRIBUTED_JOB
            )
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
) -> int:
    """Measure one parsed per-cutter file into the accumulators.

    Attribution walks the path's segments (see the module docstring): the
    per-segment lengths sum to the path's own ``cutting_distance`` /
    ``rapid_distance``, so the per-file, per-cutter and per-plate rows stay
    exact while a path that spans labels is split across them.

    Args:
        document: The parsed file.
        frame: The owning plate's frame, or ``None`` when unresolved.
        kind: The file's layer kind (:data:`KIND_TEXT` / :data:`KIND_STRUCTURAL`).
        totals: The accumulator to add into.

    Returns:
        The number of strokes (paths) that carry cutting geometry.
    """
    strokes = 0
    position: Optional[Tuple[float, float]] = None
    for path in document.stroke_paths:
        layer_class = (
            frame.stroke_class.get(_path_signature(path), BORDER_CLASS)
            if kind == KIND_STRUCTURAL and frame is not None
            else None
        )
        cutting = [seg for seg in path.segments if seg.is_cutting]
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
            if arrival_label is None and _in_plate_frame(
                frame, path.pen_up_position.x, path.pen_up_position.y
            ):
                arrival_label = PLATE_FRAME_LABEL
            totals.add_rapid(units_to_inches(gap), arrival_label)
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
            if _in_plate_frame(frame, x, y) and layer_class is not None:
                # The sheet's own border / registration mark: plate-level
                # travel, charged through the plate-frame entry of the job map
                # rather than dumped on the nearest label or left unattributed.
                # Structural files only - a text layer never draws the sheet.
                totals.add_cutting(
                    units_to_inches(seg.length),
                    _segment_class(kind, layer_class, owner),
                    PLATE_FRAME_LABEL,
                )
                if not cycle_charged:
                    totals.add_stroke_cycle(
                        _segment_class(kind, layer_class, owner), PLATE_FRAME_LABEL
                    )
                    cycle_charged = True
                if cutting:
                    owned_cutting += 1
                continue
            label_id = owner.label_id if owner is not None else None
            length = units_to_inches(seg.length)
            if seg.is_cutting:
                if not cycle_charged:
                    totals.add_stroke_cycle(_segment_class(kind, layer_class, owner), label_id)
                    cycle_charged = True
                totals.add_cutting(length, _segment_class(kind, layer_class, owner), label_id)
                if label_id is not None:
                    owned_cutting += 1
            else:
                totals.add_rapid(length, label_id)
        if cutting and owned_cutting == 0:
            totals.unattributed_strokes += 1
        # Where the tool stops is where the next path starts from.
        if path.segments:
            last = path.segments[-1]
            position = (last.end.x, last.end.y)
        elif path.pen_up_position is not None:
            position = (path.pen_up_position.x, path.pen_up_position.y)
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
    only when a material needs disambiguating.

    Args:
        stem: The parsed file-name stem.
        plate_numbers: The plate numbers in report order.
        frames: The per-plate attribution frames.

    Returns:
        The 1-based plate number, or ``None`` when the file cannot be placed.
    """
    if stem.plate is not None:
        # The stem parser only records an all-digit leading token, so this is
        # always a plate number (there is no unparsable form to guard).
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

    Returns:
        ``(totals, file_count)``: the measurement accumulators, and how many
        files were measured vs written (``{"measured": n, "written": m, "failed": k}``).
    """
    totals = _DistanceTotals()
    totals.job_of_label = dict(job_of_label or {})
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
        row["strokes"] = _measure_document(document, frame, stem.kind, file_totals)
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
) -> Dict[str, Any]:
    """Aggregate the job's production metrics into the report's ``usage`` block.

    Args:
        resolved_labels: The job's resolved labels, in content order.
        packed_plates: The packed plates from the layout stage, in packing
            order (the same objects the export reports).
        combined_by_plate: The export's in-memory combined per-plate content,
            keyed by 1-based plate number. The per-label section split and the
            border/hole split are read from it.
        plt_files: The written per-cutter PLT paths, in write order. The tool
            travel is measured from these files.
        material_by_plate: Stock material name per 1-based plate number.
        job_spec: The resolved job spec, used with ``job_config`` to resolve
            the ``tool_options`` rates of the run-time estimate. ``None`` (or
            ``job_config=None``) omits the ``time`` numbers entirely.
        job_config: The loaded job config (the ``tool_options`` registry plus
            the ``rapid_velocity`` estimate field).
        job_of_label: Placement id to owning jobset member (the composite-id
            side table of :func:`plt_optimizer.generate.jobset.build_jobset_plan`
            applied to the packing). Set by a jobset run only: it adds the
            ``by_job`` breakdown to both the ``distance`` and ``time`` blocks.

    Returns:
        The ``usage`` mapping: ``labels``, ``characters``, ``holes``,
        ``material``, ``distance`` and ``time`` blocks, JSON-ready.
    """
    frames: Dict[int, _PlateFrame] = {}
    for number, plate in enumerate(packed_plates, start=1):
        combined = combined_by_plate.get(number)
        frame = _PlateFrame(
            plate_number=number,
            material=material_by_plate.get(number, getattr(plate, "material", None)),
            slots=_slots_for(list(getattr(plate, "labels", ()) or ())),
        )
        if combined:
            frame.stroke_class = _structural_classes(combined)
            frame.plate_frame = _plate_frame_rects(combined, frame.stroke_class, frame.slots)
        frames[number] = frame
    rates_by_layer: Dict[str, _LayerRates] = {}
    for plt_type in ("text", "borders_holes"):
        rates = _build_layer_rates(job_spec, job_config, plt_type)
        if rates is not None:
            rates_by_layer[plt_type] = rates
    label_meta = _label_meta(packed_plates, material_by_plate)
    totals, file_count = _distance_usage(
        plt_files=plt_files,
        frames=frames,
        material_by_plate=material_by_plate,
        label_meta=label_meta,
        rates_by_layer=rates_by_layer or None,
        job_of_label=job_of_label,
    )
    return {
        "labels": _labels_usage(resolved_labels),
        "characters": _characters_usage(resolved_labels),
        "holes": _holes_usage(resolved_labels),
        "material": _material_usage(packed_plates, material_by_plate, resolved_labels),
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
    lines.extend(_distance_lines(usage))
    lines.extend(_time_lines(usage))
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
                f"{row.get('plate')} {_fmt_time(row.get('total'))} ({row.get('share')}%)"
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
