"""Layout engine for packing ResolvedLabel objects onto physical plates.

This module takes the flat list of ``ResolvedLabel`` objects produced by
the resolution engine and maps them to physical ``(x, y)`` coordinates on
specific plates using the ``rectpack`` library.

The engine supports two modes:
- **Constrained mode**: User-specified plates (scrap material).
- **Unbounded mode**: Auto-allocates default 24x16 sheets until all
  labels fit.

Each ``ResolvedLabel`` with ``count > 1`` is unrolled into individual
rectangles so the bin packer can place every physical instance.

Example:
    >>> from plt_optimizer.generate.resolution import resolve_job_spec
    >>> from plt_optimizer.generate.packing.layout import generate_layout
    >>> job = parse_yaml("tests_deps/sample_spec.yaml")
    >>> labels = resolve_job_spec(job)
    >>> plates = generate_layout(labels, job.plates)
    >>> print(plates[0].labels[0].x, plates[0].labels[0].y)
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field, replace
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Iterator,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    cast,
)

import rectpack

from plt_optimizer.generate.collision.detection import assert_no_collisions
from plt_optimizer.generate.label_renderer import (
    RenderedLabel,
    render_label_to_plt,
)
from plt_optimizer.generate.packing.ellipse_packer import ScrapDirection as ScrapDirectionValue
from plt_optimizer.generate.resolution import ResolvedLabel
from plt_optimizer.generate.schema import (
    DEFAULT_LAYOUT_MODE,
    LabelShape,
    LayoutMode,
    PlateFill,
    PlateSpec,
    RotationPreference,
    is_simple_layout,
    material_key,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default plate dimensions for unbounded mode
# ---------------------------------------------------------------------------
DEFAULT_PLATE_WIDTH: float = 24.0
DEFAULT_PLATE_HEIGHT: float = 16.0

# Inter-label kerf used by the round (ellipse) packing pipeline when the
# labels carry no boundary/hole cutter information (see
# :func:`_round_clearance`). Matches the ellipse packer's default.
DEFAULT_ROUND_CLEARANCE_IN: float = 0.0625


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PackedLabel:
    """A label placed at a specific physical location on a plate.

    Attributes:
        label_id: Unique identifier for this physical instance
            (e.g., ``"pump_warn_01_0"``).
        x: X-coordinate of the label's bottom-left corner in inches.
        y: Y-coordinate of the label's bottom-left corner in inches.
        width: Final width in inches (after any rotation).
        height: Final height in inches (after any rotation).
        rotated: True if the label was rotated 90 degrees (clockwise at
            plate assembly) by the packer.
        source_label: Reference to the original ResolvedLabel for
            vector generation.
    """

    label_id: str
    x: float
    y: float
    width: float
    height: float
    rotated: bool
    source_label: ResolvedLabel


@dataclass
class PackedPlate:
    """A physical plate containing zero or more packed labels.

    ``width``/``height`` describe the *usable* area (what the packer
    received); ``left_clearance``/``top_clearance`` are the unused material
    strips along the plate's left and top edges that shift the packed
    placement within the material. In the emitted plate frame (origin at
    the material's top-left, +y downward -- the HPGL device convention),
    the usable area spans ``[left_clearance, left_clearance + width]``
    horizontally and ``[top_clearance, top_clearance + height]``
    vertically, so the material's right edge always sits at
    ``left_clearance + width`` and its bottom edge at
    ``top_clearance + height``.

    Attributes:
        plate_id: Unique identifier for this plate.
        width: Usable plate width in inches.
        height: Usable plate height in inches.
        left_clearance: Unused material width along the left edge in
            inches (see :attr:`PlateSpec.left_clearance`). Defaults to 0.0.
        top_clearance: Unused material height along the top edge in
            inches (see :attr:`PlateSpec.top_clearance`). Defaults to 0.0.
        material: Stock material name this plate is cut from (see
            :attr:`PlateSpec.material`). Labels are partitioned by
            material at packing time, so a plate carries at most one
            material (``None`` = unset/material-agnostic). Defaults to
            ``None``.
        labels: List of labels placed on this plate.
    """

    plate_id: str
    width: float
    height: float
    left_clearance: float = 0.0
    top_clearance: float = 0.0
    material: Optional[str] = None
    labels: list[PackedLabel] = field(default_factory=list)


class LayoutFitError(Exception):
    """Raised when constrained plates cannot fit all requested labels."""


# ---------------------------------------------------------------------------
# Packer configuration
# ---------------------------------------------------------------------------
# Candidate (algorithm, sort) packing configurations tried by the layout engine.
# Each heuristic has different strengths; no single one dominates on all inputs
# (e.g. ``MaxRectsBssf`` leaves narrow labels stranded in some orderings while
# Guillotine variants pack them tightly). We run several and select the result
# with the smallest total plate footprint so packing quality never regresses.
#
# The list is deliberately wide. Every candidate is ranked by the same key
# (:func:`_fill_rank_key`), so adding a candidate can only ever produce a layout
# that ranks equal or better -- the cost is time, never quality. The layouts a
# narrow list cannot reach are exactly the ones that need no label rotation: on
# the shop specs the widened pool takes 107-133 rotated labels down to 0-2 at
# equal plate count and equal-or-smaller material (docs/plans/packing-quality.md).
# Measured cost is ~2 s per 200-label plate group, against minutes of rendering.
PACK_CONFIGS: Tuple[Tuple[type[Any], Callable[..., Any]], ...] = (
    (rectpack.MaxRectsBssf, rectpack.SORT_AREA),
    (rectpack.MaxRectsBssf, rectpack.SORT_NONE),
    (rectpack.MaxRectsBssf, rectpack.SORT_DIFF),
    (rectpack.MaxRectsBaf, rectpack.SORT_AREA),
    (rectpack.MaxRectsBlsf, rectpack.SORT_AREA),
    (rectpack.GuillotineBlsfLas, rectpack.SORT_NONE),
    (rectpack.GuillotineBlsfLas, rectpack.SORT_AREA),
    (rectpack.GuillotineBafLas, rectpack.SORT_NONE),
    (rectpack.GuillotineBafLas, rectpack.SORT_AREA),
    (rectpack.SkylineBl, rectpack.SORT_NONE),
    (rectpack.SkylineBl, rectpack.SORT_AREA),
)

# Candidate configurations for ``layout: columns`` (column-major fill).
#
# Column-major packing runs in a transposed frame (rectangles and bins
# swapped, placements mapped back). The algorithms in :data:`PACK_CONFIGS`
# fill the *plate's long axis* first regardless of the frame (verified
# empirically: transposing a Guillotine/MaxRects run leaves the real-space
# fill order unchanged), so the transposed frame is instead paired with
# ``SkylineBl``, whose bottom-row fitness always fills the packer's X axis
# first -- the real plate height -- before extending rightward. (Other
# skyline fitnesses were rejected: e.g. ``SkylineMwf`` stacks upward to
# minimize waste, which un-transposes back to row-major fill.)
#
# The widening applies here too, stayed inside ``SkylineBl`` for the reason just
# given (a transposed Guillotine/MaxRects run re-produces the row-major fill, so
# it costs time and adds no layout): the sort orders are what change the packing,
# and a column job gains its upright layouts from them.
PACK_CONFIGS_COLUMNS: Tuple[Tuple[type[Any], Callable[..., Any]], ...] = (
    (rectpack.SkylineBl, rectpack.SORT_NONE),
    (rectpack.SkylineBl, rectpack.SORT_AREA),
    (rectpack.SkylineBl, rectpack.SORT_DIFF),
    (rectpack.SkylineBl, rectpack.SORT_SSIDE),
)


def _render_labels_cache(
    resolved_labels: list[ResolvedLabel],
    pen_map: Optional[dict[tuple[float, str], int]] = None,
) -> dict[str, RenderedLabel]:
    """Render all unique labels and cache by ID.

    For labels with count > 1, renders only once and caches the result
    to avoid redundant rendering.

    Args:
        resolved_labels: Flat list of fully resolved labels.
        pen_map: Optional mapping of ``(text cutter diameter, text color)``
            to HPGL pen number (see
            :func:`plt_optimizer.generate.resolution.build_cutter_pen_map`).
            Passed through to :func:`render_label_to_plt`; ``None`` keeps
            all text on the historical single text pen.

    Returns:
        Dictionary mapping label ID to RenderedLabel.
    """
    rendered_cache: dict[str, RenderedLabel] = {}
    for label in resolved_labels:
        if label.id not in rendered_cache:
            rendered_cache[label.id] = render_label_to_plt(label, pen_map=pen_map)
    return rendered_cache


def unroll_labels_with_rendered_bounds(
    resolved_labels: list[ResolvedLabel],
    rendered_labels: dict[str, RenderedLabel],
) -> list[tuple[float, float, str, ResolvedLabel, RenderedLabel]]:
    """Flatten label counts using rendered dimensions for packing.

    Each ``ResolvedLabel`` with ``count > 1`` produces that many rectangle
    entry. Uses actual rendered label dimensions instead of nominal
    dimensions to account for text/border sizing differences.

    Args:
        resolved_labels: Flat list of fully resolved labels.
        rendered_labels: Cache of rendered labels by ID.

    Returns:
        A list of ``(pack_width, pack_height, rect_id, source_label,
        rendered_label)`` tuples ready to be added to the packer.
    """
    rectangles: list[tuple[float, float, str, ResolvedLabel, RenderedLabel]] = []
    for label in resolved_labels:
        rendered = rendered_labels[label.id]
        # Prefer the *rendered* source label: collision avoidance may have
        # returned an adjusted clone (reduced hole_margin and/or per-line
        # collision_compress_by_line). Propagating it through the packer keeps
        # plate vectorization and Phase 3 assembly consistent with the
        # emitted PLT geometry.
        effective_label = rendered.source_label
        for i in range(label.count):
            # Use rendered dimensions for packing (actual width/height)
            # instead of nominal label dimensions
            pack_width = rendered.width
            pack_height = rendered.height

            # Unique ID to track instances of the same logical label
            rect_id = f"{label.id}_{i}"
            rectangles.append((pack_width, pack_height, rect_id, effective_label, rendered))

    return rectangles


def unroll_labels(
    resolved_labels: list[ResolvedLabel],
) -> list[tuple[float, float, str, ResolvedLabel]]:
    """Flatten label counts into individual rectangle definitions.

    Each ``ResolvedLabel`` with ``count > 1`` produces that many rectangle
    entry. Margin is NOT included in packing dimensions to ensure labels
    pack coincident (touching) with no gaps. Margin is applied only during
    rendering to position content inward from edges.

    Args:
        resolved_labels: Flat list of fully resolved labels.

    Returns:
        A list of ``(pack_width, pack_height, rect_id, source_label)``
        tuples ready to be added to the packer.
    """
    rectangles: list[tuple[float, float, str, ResolvedLabel]] = []
    for label in resolved_labels:
        for i in range(label.count):
            # Pack at nominal dimensions (labels pack coincident;
            # inter-label spacing comes from each label's own margin)
            # Margin is applied during rendering only
            pack_width = label.width
            pack_height = label.height

            # Unique ID to track instances of the same logical label
            rect_id = f"{label.id}_{i}"
            rectangles.append((pack_width, pack_height, rect_id, label))

    return rectangles


# ---------------------------------------------------------------------------
# Layout engine
# ---------------------------------------------------------------------------
def _extract_packed_plates(
    packer: _Packer,
    *,
    transpose: bool = False,
    clearances: Optional[Mapping[str, tuple[float, float]]] = None,
    material: Optional[str] = None,
    materials: Optional[Mapping[str, Optional[str]]] = None,
) -> list[PackedPlate]:
    """Translate ``rectpack`` results into typed ``PackedPlate`` objects.

    Args:
        packer: A ``rectpack.Packer`` that has already executed ``pack()``.
        transpose: When True, the packer ran in the transposed column-major
            frame (see :func:`_transpose_entries`) and every placement is
            mapped back into real plate space via
            ``(x, y, w, h) -> (y, x, h, w)``.
        clearances: Optional mapping of bin id to ``(left_clearance,
            top_clearance)`` in inches (see
            :attr:`PlateSpec.left_clearance`). Packer coordinates are
            offsets from the usable area's top-left corner, which sits at
            ``(left_clearance, top_clearance)`` inside the material, so
            every placement is shifted by that pair (in real plate space,
            after any transpose mapping). Bins missing from the mapping
            get zero clearance.
        material: Stock material name stamped onto every produced plate
            (see :attr:`PackedPlate.material`). The packing passes run
            per material group, so all plates produced by one pass share
            the group's material. ``None`` (the default) leaves plates
            material-agnostic.
        materials: Optional mapping of bin id to the plate's own declared
            material spelling. Constrained-mode passes use it so each
            plate keeps its own spelling (a `wk(uv)` plate carrying
            `WK(UV)` labels reports `wk(uv)`); bins missing from the
            mapping fall back to ``material``.

    When ``transpose`` is set, each plate's instance ids are re-assigned
    onto the packed slots in text-oriented reading order before returning
    (see :func:`_reorder_ids_for_reader_order`): the slot geometry is
    untouched, only interchangeable ``(label_id, source_label)`` pairs are
    permuted, so every plate keeps its exact id set.

    Returns:
        A list of ``PackedPlate`` objects with all labels positioned.
        Empty bins (from auto-allocation) are discarded.
    """
    clearance_map: Mapping[str, tuple[float, float]] = clearances or {}
    material_map: Mapping[str, Optional[str]] = materials or {}
    final_plates: list[PackedPlate] = []
    # Global pre-pack content-order index per instance id (4th rid element),
    # consumed by the reader-order reassignment in the transposed frame.
    seq_by_id: dict[str, int] = {}

    for bin_obj in packer:
        if len(bin_obj) == 0:
            # Ignore empty auto-allocated bins
            continue

        left_clearance, top_clearance = clearance_map.get(bin_obj.bid, (0.0, 0.0))
        bin_material = material_map.get(bin_obj.bid, material)
        if transpose:
            # The packer's bin was offered as (height, width); report the
            # real plate dimensions.
            plate = PackedPlate(
                plate_id=bin_obj.bid,
                width=bin_obj.height,
                height=bin_obj.width,
                left_clearance=left_clearance,
                top_clearance=top_clearance,
                material=bin_material,
            )
        else:
            plate = PackedPlate(
                plate_id=bin_obj.bid,
                width=bin_obj.width,
                height=bin_obj.height,
                left_clearance=left_clearance,
                top_clearance=top_clearance,
                material=bin_material,
            )

        for rect in bin_obj:
            # Unpack the custom ID tuple we passed in. The third element is
            # the *packing* width the rectangle was added with (rendered
            # dimensions in the bounds path, nominal otherwise) — the only
            # reliable baseline for rotation detection, since the nominal
            # ResolvedLabel width may differ from what was actually packed.
            rect_id, source_label, pack_width, seq = rect.rid
            seq_by_id[rect_id] = seq

            # Detect rotation: rect.width/height reflect post-rotation dims
            # in the *packer* frame. In the transposed frame a packer-space
            # rotation is exactly a real-space rotation (a 90-degree swap
            # composed with the transpose is again a 90-degree swap), so
            # comparing against the packer-space width stays correct.
            was_rotated = not math.isclose(rect.width, pack_width, rel_tol=1e-9)

            # Packer coordinates are offsets from the usable area's
            # top-left corner; the emitted plate frame is y-down (the
            # HPGL device convention, see rotate_plt_content_90cw), so
            # the usable area's top-left sits at (left_clearance,
            # top_clearance) inside the material and every placement
            # simply gains those offsets. Zero-clearance output therefore
            # stays bit-identical to the historical behavior.
            if transpose:
                # Map the placement back out of the transposed frame,
                # then apply the clearance shift in real plate space.
                packed_label = PackedLabel(
                    label_id=rect_id,
                    x=rect.y + left_clearance,
                    y=rect.x + top_clearance,
                    width=rect.height,
                    height=rect.width,
                    rotated=was_rotated,
                    source_label=source_label,
                )
            else:
                packed_label = PackedLabel(
                    label_id=rect_id,
                    x=rect.x + left_clearance,
                    y=rect.y + top_clearance,
                    width=rect.width,
                    height=rect.height,
                    rotated=was_rotated,
                    source_label=source_label,
                )
            plate.labels.append(packed_label)

        if transpose:
            # ``columns`` mode is text-oriented: re-pair the plate's ids
            # onto its slots so the id sequence follows reading order as
            # the reader sees the engraved labels (rotated labels included).
            plate.labels = _reorder_ids_for_reader_order(plate.labels, seq_by_id)

        final_plates.append(plate)

    return final_plates


# The ``rid`` payload carried with every rectangle: the instance id, the
# source label, the *packing* width the rectangle was offered to the
# packer with (the baseline rotation detection compares against), and the
# global pre-pack content-order index (used to re-pair ids onto slots in
# text-oriented reading order after transposed-frame packing).
_RidPayload = tuple[str, ResolvedLabel, float, int]

# A rectangle entry as passed to the packer: (width, height, rid-payload).
# The ``rid`` payload is an opaque tuple that rectpack preserves verbatim and
# which :func:`_extract_packed_plates` later unpacks.
_RectEntry = tuple[float, float, _RidPayload]

if TYPE_CHECKING:
    # ``rectpack`` ships no type stubs (and its own docstrings are the only
    # typing it carries), so pyright infers ``newPacker``'s return as the union
    # of the seven concrete packer classes -- a union ``rectpack.packer.Packer``
    # is NOT a member of (the online variants don't subclass it), which is why
    # every use of a packer below was an ``attribute unknown`` / ``not
    # assignable`` error. The surface this module actually touches is small and
    # identical across all seven, so it is declared once, structurally, here and
    # applied at the single construction site (:func:`_pack_best`). Verified
    # against the installed library: ``Packer.__iter__`` chains closed+open bins
    # (``pack_algo.PackingAlgorithm``, which carries ``bid``/``__len__`` and
    # yields ``geometry.Rectangle``, the 5-``__slots__`` placement record).
    class _PlacedRect(Protocol):
        """One placement rectpack reports inside a bin."""

        x: float
        y: float
        width: float
        height: float
        rid: _RidPayload

    class _PackedBin(Protocol):
        """One plate's packing surface, after ``pack()``."""

        bid: str
        width: float
        height: float

        def __len__(self) -> int: ...
        def __iter__(self) -> Iterator[_PlacedRect]: ...

    class _Packer(Protocol):
        """The packing surface :mod:`plt_optimizer.generate.packing.layout` uses."""

        def add_rect(self, width: float, height: float, **kwargs: Any) -> None: ...
        def add_bin(self, width: float, height: float, **kwargs: Any) -> None: ...
        def pack(self) -> None: ...
        def __iter__(self) -> Iterator[_PackedBin]: ...


def _transpose_entries(entries: list[_RectEntry], transpose: bool) -> list[_RectEntry]:
    """Swap rectangle dimensions into the transposed packing frame.

    The ``rid`` payload is rebuilt carrying the *packer-space* width (the
    swapped height), so rotation detection in :func:`_extract_packed_plates`
    keeps comparing against the dimensions actually offered to the packer.

    Args:
        entries: Real-space ``(pack_width, pack_height, rid)`` tuples.
        transpose: When False, entries are returned unchanged.

    Returns:
        Packer-space ``(pack_width, pack_height, rid)`` tuples.
    """
    if not transpose:
        return entries
    return [
        (height, width, (rect_id, source_label, height, seq))
        for width, height, (rect_id, source_label, _pack_width, seq) in entries
    ]


def _reader_order_key(packed: PackedLabel) -> tuple[float, float]:
    """Sort key placing a packed label in text-oriented reading order.

    The emitted plate frame is the device frame (origin at the material's
    top-left, +x rightward, +y downward). Unrotated labels read along +x,
    so a reader's column is a fixed-X band filled top-to-bottom: key
    ``(x, y)``. Rotated labels are turned 90 degrees clockwise at assembly
    (see ``vectorize.rotate_plt_content_90cw``): their text reads along +y
    with "up" pointing at +x, so the reader turns the sheet 90 degrees
    counter-clockwise and sees reader-right as plate +y and reader-up as
    plate +x. A reader's column is then a fixed-Y band filled right-to-left
    in plate X (true top-to-bottom reading order): key ``(y, -x)``.

    Args:
        packed: The packed label to key.

    Returns:
        ``(column, row)`` reader-frame sort coordinates (ascending).
    """
    if packed.rotated:
        return (packed.y, -packed.x)
    return (packed.x, packed.y)


def _reorder_ids_for_reader_order(
    labels: Sequence[PackedLabel],
    seq_by_id: Mapping[str, int],
) -> list[PackedLabel]:
    """Re-assign instance ids onto slots in text-oriented reading order.

    ``layout: columns`` means column-major *as the reader sees the engraved
    labels*. Because the transposed packing frame often rotates most
    labels, naive plate-frame column fill becomes reader row-major, so
    after extraction the ids are permuted back onto the (unchanged) slots.
    Only labels with identical packing dimensions are interchangeable, so:

    - Slots are grouped by the *natural* (unrotated) packing dimensions
      they demand: ``(w, h)`` for an unrotated slot, ``(h, w)`` for a
      rotated one. Every label of a group fits every slot of the group in
      exactly the orientation the slot encodes, so rotated and unrotated
      slots of the same label size interleave in one global reader-order
      sequence.
    - Within each group, slots are sorted by :func:`_reader_order_key`
      and the group's labels (sorted by their pre-pack content sequence,
      the 4th ``rid`` element) are re-paired onto them via
      :func:`dataclasses.replace`.

    The returned list is sorted by reader key across the whole plate, so
    downstream assembly order (and the plate optimizer's TSP baseline)
    follows reading order too. A plate whose winning packing is entirely
    unrotated keeps the historical ``(x, y)`` id-to-slot mapping, making
    this a bit-identical no-op for it.

    Args:
        labels: The plate's packed labels (instance ids unique).
        seq_by_id: Global pre-pack content-order index per ``label_id``.

    Returns:
        New ``PackedLabel`` objects (same slots, permuted ids) in plate-wide
        reader order.
    """
    if not labels:
        return []

    groups: dict[tuple[float, float], list[PackedLabel]] = {}
    for packed in labels:
        # Group by the *natural* (unrotated) dims the slot demands: a
        # rotated slot of plate dims (w, h) holds a natural (h, w) label,
        # an unrotated slot a natural (w, h) one. Only labels inside a
        # group fit its slots in the encoded orientation.
        if packed.rotated:
            group_key = (round(packed.height, 6), round(packed.width, 6))
        else:
            group_key = (round(packed.width, 6), round(packed.height, 6))
        groups.setdefault(group_key, []).append(packed)

    reassigned: list[PackedLabel] = []
    moved = 0
    for group in groups.values():
        slots = sorted(group, key=_reader_order_key)
        contents = sorted(group, key=lambda packed: seq_by_id[packed.label_id])
        for slot, content in zip(slots, contents):
            if slot.label_id != content.label_id:
                moved += 1
            reassigned.append(
                replace(
                    slot,
                    label_id=content.label_id,
                    source_label=content.source_label,
                )
            )

    reassigned.sort(key=_reader_order_key)
    if moved:
        logger.debug(
            "Re-assigned %d label id(s) onto slots in text-oriented reading order.",
            moved,
        )
    return reassigned


def _count_rotated(packer: _Packer) -> int:
    """Count rectangles the packer placed in a rotated orientation.

    Compares each placed rectangle's width against the *packing* width
    carried in its ``rid`` payload (the same baseline used by
    :func:`_extract_packed_plates`).

    Args:
        packer: A ``rectpack.Packer`` that has already executed ``pack()``.

    Returns:
        Number of rectangles placed rotated 90 degrees.
    """
    rotated = 0
    for bin_obj in packer:
        for rect in bin_obj:
            pack_width = rect.rid[2]
            if not math.isclose(rect.width, pack_width, rel_tol=1e-9):
                rotated += 1
    return rotated


def _pack_best(
    rectangles_with_rid: list[_RectEntry],
    bin_specs: list[tuple[float, float, str]],
    allow_rotation: bool = True,
    layout: LayoutMode = LayoutMode.ROWS,
    rotation_preference: RotationPreference = RotationPreference.FILL_AXIS,
    plate_fill: PlateFill = PlateFill.PLATE_MAJOR,
) -> _Packer:
    """Run several packing heuristics and return the packer with the best fit.

    Each candidate algorithm is run over the same set of rectangles and bins.
    The result that packs every rectangle onto the smallest total plate
    footprint (sum over non-empty plates of ``width * height``) is selected,
    which guarantees tighter layouts without ever regressing on packing quality
    for any given input. Candidates that cannot fit every rectangle are ranked
    by ``(packed_count desc, footprint asc)`` and only used when no candidate
    fits everything.

    When ``allow_rotation`` is set, every candidate is evaluated twice — once
    with rotation disabled and once with it enabled — and candidates are ranked
    by ``(footprint, fill-axis span, cross-axis span, rotated_count)``. The
    footprint stays first, so a label only rotates when rotation costs no extra
    material; among footprints that tie, the candidate that fills the axis
    ``layout`` fills first wins (``columns`` the plate height, ``rows`` the plate
    width), which is why a wider-than-tall label stands upright in a column job
    and lies along the plate in a row job. The rotation count decides only when
    both spans tie as well, keeping output stable for jobs whose layouts are
    indistinguishable on geometry.

    ``layout`` selects the fill-order frame (the simple grid modes never
    reach this function -- :func:`_pack_group` routes them to
    :mod:`plt_optimizer.generate.packing.simple_grid` before any packer is
    created):

    - :attr:`LayoutMode.ROWS` (historical behavior): pack in the real frame
      with :data:`PACK_CONFIGS`; the Guillotine/MaxRects heuristics fill the
      plate width first, then extend downward.
    - :attr:`LayoutMode.COLUMNS`: pack in the **transposed frame** (bins and
      rectangles swapped) with :data:`PACK_CONFIGS_COLUMNS`; the skyline
      bottom-row fitness fills the packer's X axis — the real plate height —
      before extending rightward. The returned packer's coordinates are
      packer-space; callers must extract them with
      ``_extract_packed_plates(packer, transpose=True)``.

    Args:
        rectangles_with_rid: List of ``(pack_width, pack_height, rid)`` tuples.
            The ``rid`` payload (e.g. a label reference) is preserved verbatim
            by rectpack and recovered in :func:`_extract_packed_plates`.
        bin_specs: List of ``(width, height, bid)`` plate definitions.
        allow_rotation: When True (the default), rotated packing candidates
            are considered. Rotated labels have their whole content rotated
            clockwise during plate assembly (see
            ``vectorize.assemble_plt_from_rendered_labels``).
        layout: Preferential fill order (see above). Defaults to the
            historical row-major frame.
        rotation_preference: Ranking preference among layouts that fit --
            the fill order of ``layout``, or the fewest rotated labels (see
            :func:`_fill_rank_key` and
            :attr:`plt_optimizer.generate.schema.JobSpec.rotation_preference`).
            The default reproduces the historical ranking exactly.
        plate_fill: Which layout wins among plans of equal material -- the
            plate-major (greedy) fill vector, or the historical whole-layout
            ranking (see :func:`_plate_fill_rank_key` and
            :attr:`plt_optimizer.generate.schema.JobSpec.plate_fill`).
            ``PlateFill.GLOBAL`` reproduces the historical ranking exactly, and
            is the only mode that skips the per-candidate plan measurement.

    Returns:
        The best-performing configured ``rectpack.Packer`` that successfully
        packs all rectangles. If no candidate fits every rectangle, returns
        the best partial packer (its result is surfaced to callers for error
        reporting).
    """
    transpose = layout == LayoutMode.COLUMNS
    configs = PACK_CONFIGS_COLUMNS if transpose else PACK_CONFIGS

    # Rotation variants to evaluate per packing configuration. With rotation
    # disabled the behavior is exactly the historical single-variant sweep.
    orientations: tuple[bool, ...] = (False, True) if allow_rotation else (False,)

    packer_rects = _transpose_entries(rectangles_with_rid, transpose)
    packer_bins = [(h, w, bid) for w, h, bid in bin_specs] if transpose else bin_specs

    best_packer: Optional[_Packer] = None
    best_packed: int = -1
    best_key: Optional[Tuple[Any, ...]] = None

    for pack_algo, sort_algo in configs:
        for rotation in orientations:
            packer = cast(
                # Quoted: ``_Packer`` is a Protocol inside ``if TYPE_CHECKING:``,
                # so the bare name does not exist at runtime and an unquoted cast
                # raises NameError on every packing call.
                "_Packer",
                rectpack.newPacker(
                    mode=rectpack.PackingMode.Offline,
                    bin_algo=rectpack.PackingBin.BFF,
                    pack_algo=pack_algo,
                    sort_algo=sort_algo,
                    rotation=rotation,
                ),
            )

            for w, h, rid in packer_rects:
                packer.add_rect(w, h, rid=rid)

            for width, height, bid in packer_bins:
                packer.add_bin(width, height, bid=bid)

            packer.pack()

            total_packed = sum(len(b) for b in packer)
            footprint = _plate_footprint(packer)
            rotations = _count_rotated(packer)
            height_fill, width_fill = _fill_axis_extents(packer, transpose)
            # ``columns`` fills each plate top-to-bottom, so its fill axis is the
            # plate height; ``rows`` fills left-to-right, so it is the plate
            # width. (The simple-grid modes never reach this function.)
            if transpose:
                fill_axis, cross_axis = height_fill, width_fill
            else:
                fill_axis, cross_axis = width_fill, height_fill

            # Rank: full fits first; among equals, ``plate_fill`` decides which
            # plan wins (plate-major fill, or the historical whole-layout
            # footprint/fill ranking). Partials rank by packed count only (ties
            # keep the incumbent); they only matter for error reporting.
            full_fit = total_packed == len(rectangles_with_rid)
            best_full = best_packer is not None and best_packed == len(rectangles_with_rid)
            candidate_key: Tuple[Any, ...] = (
                _plate_fill_rank_key(
                    _plan_metrics_from_packer(packer, transpose), rotation_preference
                )
                if plate_fill is PlateFill.PLATE_MAJOR
                else _fill_rank_key(
                    footprint, fill_axis, cross_axis, rotations, rotation_preference
                )
            )
            if best_packer is None:
                take = True
            elif full_fit != best_full:
                take = full_fit
            elif full_fit:
                assert best_key is not None  # set with every full-fit incumbent
                take = candidate_key < best_key
            else:
                take = total_packed > best_packed

            if take:
                best_packed = total_packed
                best_key = candidate_key if full_fit else None
                best_packer = packer

    assert best_packer is not None  # Config lists are never empty
    return best_packer


def _rectpack_plates(
    rectangles_with_rid: list[_RectEntry],
    bin_specs: list[tuple[float, float, str]],
    *,
    allow_rotation: bool,
    layout: LayoutMode,
    clearances: Optional[Mapping[str, tuple[float, float]]],
    material: Optional[str],
    materials: Optional[Mapping[str, Optional[str]]],
    rotation_preference: RotationPreference,
    plate_fill: PlateFill,
) -> list[PackedPlate]:
    """Pack rectangles with the ``rectpack`` ensemble and extract plates.

    Shared by :func:`_pack_group`'s whole-job plan and the tail of a
    grid-prefix plan (see there), so both map packer space back into real plate
    space identically.

    Args:
        rectangles_with_rid: ``(pack_width, pack_height, rid)`` tuples to pack.
        bin_specs: ``(width, height, bid)`` plates to pack onto.
        allow_rotation: Whether rotated candidates are considered.
        layout: Fill-order mode of the group.
        clearances: Optional bin-id to ``(left, top)`` clearance mapping.
        material: Stock material stamped onto every produced plate.
        materials: Optional bin-id to declared-material-spelling mapping.
        rotation_preference: Ranking preference for rotation counts.
        plate_fill: Ranking policy for plate saturation.

    Returns:
        The non-empty ``PackedPlate`` objects, in plate order.

    Raises:
        LayoutFitError: If the rectangles do not fit the given plates.
    """
    packer = _pack_best(
        rectangles_with_rid,
        bin_specs,
        allow_rotation=allow_rotation,
        layout=layout,
        rotation_preference=rotation_preference,
        plate_fill=plate_fill,
    )
    return _extract_packed_plates(
        packer,
        transpose=layout == LayoutMode.COLUMNS,
        clearances=clearances,
        material=material,
        materials=materials,
    )


def _pack_group(
    rectangles_with_rid: list[_RectEntry],
    bin_specs: list[tuple[float, float, str]],
    allow_rotation: bool,
    layout: LayoutMode,
    clearances: Optional[Mapping[str, tuple[float, float]]] = None,
    material: Optional[str] = None,
    materials: Optional[Mapping[str, Optional[str]]] = None,
    rotation_preference: RotationPreference = RotationPreference.FILL_AXIS,
    plate_fill: PlateFill = PlateFill.PLATE_MAJOR,
) -> list[PackedPlate]:
    """Pack rectangles onto one group of same-mode plates and extract plates.

    Runs :func:`_pack_best` with the group's fill-order mode and maps the
    packer result back into real plate space (transposing back for column
    major groups). Simple modes (``columns-simple`` / ``rows-simple``)
    bypass ``rectpack`` entirely and grid-fill through
    :func:`plt_optimizer.generate.packing.simple_grid.pack_simple_grid`
    (no rotation -- ``allow_rotation`` is ignored on that path, which the
    schema validator guarantees is never explicitly true for a simple
    layout).

    Args:
        rectangles_with_rid: Remaining ``(pack_width, pack_height, rid)``
            tuples to pack.
        bin_specs: ``(width, height, bid)`` plates sharing one fill mode.
        allow_rotation: Whether rotated candidates are considered (ignored
            by the simple grid-fill modes).
        layout: The group's fill-order mode.
        clearances: Optional bin-id to ``(left_clearance, top_clearance)``
            mapping applied to placements (see
            :func:`_extract_packed_plates`).
        material: Stock material stamped onto every produced plate (see
            :attr:`PackedPlate.material`). ``None`` (the default) keeps
            plates material-agnostic.
        materials: Optional bin-id to declared-material-spelling mapping
            (falls back to ``material``); lets constrained-mode passes
            keep each plate's own spelling.
        rotation_preference: Ranking preference forwarded to
            :func:`_pack_best` (ignored by the simple grid modes, which never
            rotate).
        plate_fill: Layout ranking forwarded to :func:`_pack_best`. Under
            :attr:`~plt_optimizer.generate.schema.PlateFill.PLATE_MAJOR` the
            matching flush grid also competes as a whole-plan candidate,
            completed by a ``rectpack`` tail: content order that tiles a plate
            exactly (a run of same-size labels) wins those plates outright and
            the awkward remainder is optimized per plate, while mixed-size jobs
            whose order tiles nothing keep the ``rectpack`` plan because the
            ranking decides.

    Returns:
        The non-empty ``PackedPlate`` objects produced by this packing pass.
    """
    if is_simple_layout(layout):
        # Lazy import keeps rectpack-free startup (Win7 watch path) and
        # avoids the simple_grid -> layout type-import cycle.
        from plt_optimizer.generate.packing.simple_grid import pack_simple_grid

        return pack_simple_grid(
            rectangles_with_rid,
            bin_specs,
            layout,
            clearances=clearances,
            material=material,
            materials=materials,
        )

    packer = _pack_best(
        rectangles_with_rid,
        bin_specs,
        allow_rotation=allow_rotation,
        layout=layout,
        rotation_preference=rotation_preference,
        plate_fill=plate_fill,
    )
    rect_plates = _extract_packed_plates(
        packer,
        transpose=layout == LayoutMode.COLUMNS,
        clearances=clearances,
        material=material,
        materials=materials,
    )

    grid_plates: list[PackedPlate] = []
    if plate_fill is PlateFill.PLATE_MAJOR:
        # Plate-major ranking: the matching flush grid competes as a whole-plan
        # candidate. Content order that tiles a plate exactly -- a leading run of
        # same-size labels -- is optimal by construction, and ``rectpack``'s
        # fragmentation can miss it (docs/plans/packing-quality.md Measurement 5:
        # 121 of 128 3x1 labels on a 24x16 sheet, where the flush grid fits all
        # 128 at 100 %). Ties go to ``rectpack``, so a job whose content order
        # does not tile a plate keeps its historical layout.
        from plt_optimizer.generate.packing.simple_grid import pack_simple_grid

        seed_layout = (
            LayoutMode.COLUMNS_SIMPLE if layout is LayoutMode.COLUMNS else LayoutMode.ROWS_SIMPLE
        )
        grid_plates = pack_simple_grid(
            rectangles_with_rid,
            bin_specs,
            seed_layout,
            clearances=clearances,
            material=material,
            materials=materials,
        )

        # The grid is strict about content order: the first label that no longer
        # fits closes the plate and everything behind it cascades, so on a mixed
        # job it stops early (128 3x1 labels fill a 24x16 sheet exactly, then a
        # 12x12 label cannot join the row band). Completing that prefix with a
        # ``rectpack`` tail is what makes the perfect sheet usable: the grid
        # takes the plates its content order fills, and the remainder is
        # optimized per plate. A tail that cannot fit the remaining plates
        # leaves the candidate incomplete, and the ranking below keeps
        # ``rectpack``.
        placed = sum(len(plate.labels) for plate in grid_plates)
        tail_bins = bin_specs[len(grid_plates) :]
        if placed < len(rectangles_with_rid) and tail_bins:
            tail_plates = _rectpack_plates(
                rectangles_with_rid[placed:],
                tail_bins,
                allow_rotation=allow_rotation,
                layout=layout,
                clearances=clearances,
                material=material,
                materials=materials,
                rotation_preference=rotation_preference,
                plate_fill=plate_fill,
            )
            grid_plates = [*grid_plates, *tail_plates]

    if sum(len(plate.labels) for plate in grid_plates) < len(rectangles_with_rid):
        return rect_plates
    fill_is_height = layout is LayoutMode.COLUMNS
    grid_key = _plate_fill_rank_key(
        _plan_metrics_from_plates(grid_plates, fill_is_height=fill_is_height), rotation_preference
    )
    rect_key = _plate_fill_rank_key(
        _plan_metrics_from_plates(rect_plates, fill_is_height=fill_is_height), rotation_preference
    )
    return grid_plates if grid_key < rect_key else rect_plates


def _resolve_plate_groups(
    provided_plates: Optional[list[PlateSpec]],
    job_layout: LayoutMode,
    n_rectangles: int,
    default_plate_size: Optional[tuple[float, float]] = None,
    bin_id_prefix: str = "",
) -> list[tuple[list[tuple[float, float, str]], LayoutMode]]:
    """Group plates into sequential same-mode packing passes.

    Consecutive plates resolving to the same fill mode share one packing
    pass, so single-mode jobs — including every unbounded job — pack in
    exactly one pass, identically to the historical behavior. When plates
    mix modes, each maximal run of same-mode plates becomes its own group
    and the groups pack sequentially in declaration order: a group receives
    only the label instances left over from the previous groups.

    Args:
        provided_plates: User-specified plates (constrained mode), or
            ``None`` / empty for unbounded auto-allocation of default 24x16
            plates.
        job_layout: The job-level fill-order mode, used as the fallback for
            plates that do not set ``layout`` and for unbounded mode.
        n_rectangles: Number of label instances; caps the number of default
            plates offered in unbounded mode (theoretical maximum: one per
            instance).
        default_plate_size: ``(width, height)`` override for the
            auto-allocated unbounded bins (from ``job-config.json``
            ``plate_width`` / ``plate_height``). ``None`` uses
            :data:`DEFAULT_PLATE_WIDTH` x :data:`DEFAULT_PLATE_HEIGHT`.
        bin_id_prefix: Prefix for auto-allocated bin ids (unbounded mode).
            Material partitioning runs one unbounded pass per material
            group, and each pass prefixes its bins (see
            :func:`_material_bin_prefix`) so the material is identifiable
            from the bin id and ids stay unique across groups. The empty
            default reproduces the historical ``default_plate_{i}`` naming.

    Returns:
        Declaration-ordered ``(bin_specs, layout)`` groups.
    """
    if not provided_plates:
        default_width, default_height = default_plate_size or (
            DEFAULT_PLATE_WIDTH,
            DEFAULT_PLATE_HEIGHT,
        )
        default_bins = [
            (
                default_width,
                default_height,
                f"{bin_id_prefix}default_plate_{i + 1}",
            )
            for i in range(n_rectangles)
        ]
        return [(default_bins, job_layout)]

    groups: list[tuple[list[tuple[float, float, str]], LayoutMode]] = []
    for plate in provided_plates:
        # PlateSpec.id is a required str, so every provided plate is usable.
        plate_layout = plate.layout or job_layout
        if not groups or groups[-1][1] != plate_layout:
            groups.append(([], plate_layout))
        groups[-1][0].append((plate.width, plate.height, plate.id))
    return groups


def _plate_clearances(
    provided_plates: Optional[list[PlateSpec]],
) -> dict[str, tuple[float, float]]:
    """Collect per-plate edge clearances keyed by bin id.

    Args:
        provided_plates: User-specified plates, or ``None`` / empty for
            unbounded mode (auto-allocated bins take their clearance from
            :func:`_default_clearance_map` instead).

    Returns:
        Mapping of plate id to ``(left_clearance, top_clearance)`` in
        inches. Plates with both clearances zero are omitted, keeping the
        mapping (and downstream shifting) a no-op for the common case.
    """
    clearances: dict[str, tuple[float, float]] = {}
    for plate in provided_plates or []:
        if plate.left_clearance or plate.top_clearance:
            clearances[plate.id] = (plate.left_clearance, plate.top_clearance)
    return clearances


def _default_clearance_map(
    groups: list[tuple[list[tuple[float, float, str]], LayoutMode]],
    default_plate_clearance: Optional[tuple[float, float]],
) -> dict[str, tuple[float, float]]:
    """Map every auto-allocated bin to the configured default clearance.

    Unbounded mode has no ``PlateSpec`` objects to carry edge clearances, so
    the ``job-config.json`` ``left_clearance`` / ``top_clearance`` values are
    applied uniformly to all auto-allocated bins (mirroring how a plate list
    of identical clearance sheets would behave).

    Args:
        groups: Declaration-ordered ``(bin_specs, layout)`` groups from
            :func:`_resolve_plate_groups`.
        default_plate_clearance: ``(left, top)`` clearance in inches, or
            ``None`` / all-zero for no shift.

    Returns:
        Mapping of auto-allocated bin id to its clearance pair. Empty when
        no clearance is configured (keeping downstream shifting a no-op).
    """
    if not default_plate_clearance:
        return {}
    left, top = default_plate_clearance
    if not left and not top:
        return {}
    return {
        bin_id: (left, top)
        for bin_specs, _layout in groups
        for (_width, _height, bin_id) in bin_specs
    }


def _split_pinned_entries(
    entries: list[_RectEntry],
) -> dict[str, list[_RectEntry]]:
    """Group rectangle entries by their label's pinned plate id.

    A label pins itself to a plate via ``ResolvedLabel.plate_id`` (set by
    plate-level ``replacement_text_file`` expansion or declared directly
    on a ``LabelSpec``). Pinned labels must pack exclusively onto the
    declaring plate, which then accepts no other labels.

    Args:
        entries: Real-space ``(pack_width, pack_height, rid)`` tuples.

    Returns:
        Mapping of plate id to the entries pinned to it (empty when
        nothing is pinned).
    """
    pinned: dict[str, list[_RectEntry]] = {}
    for entry in entries:
        plate_id = entry[2][1].plate_id
        if plate_id is not None:
            pinned.setdefault(plate_id, []).append(entry)
    return pinned


def _split_material_entries(
    entries: list[_RectEntry],
) -> dict[Optional[str], list[_RectEntry]]:
    """Group rectangle entries by their label's resolved material.

    Labels declare a stock material via ``ResolvedLabel.material`` (the
    ``material`` cascade, label -> job). Labels sharing a material pack
    together and a plate never mixes materials, so the packing passes run
    one pass per material group. Keys are the case-insensitive
    :func:`~plt_optimizer.generate.schema.material_key` of the material
    (``None`` = unset); insertion order preserves content order, which
    also fixes the claim order for material-less plates.

    Args:
        entries: Real-space ``(pack_width, pack_height, rid)`` tuples.

    Returns:
        Mapping of material key to that group's entries, in first-
        declaration order (empty input yields an empty mapping).
    """
    groups: dict[Optional[str], list[_RectEntry]] = {}
    for entry in entries:
        key = material_key(entry[2][1].material)
        groups.setdefault(key, []).append(entry)
    return groups


def _group_display_material(entries: Sequence[_RectEntry]) -> Optional[str]:
    """Return the first-declared spelling of a material group.

    Grouping is case-insensitive, so the group's *display* name (stamped
    onto packed plates and quoted in errors) is the first non-null
    spelling seen in content order.

    Args:
        entries: The group's ``(pack_width, pack_height, rid)`` tuples.

    Returns:
        The first declared material spelling, or ``None`` when the group
        is material-less.
    """
    for entry in entries:
        if entry[2][1].material is not None:
            return entry[2][1].material
    return None


def _material_bin_prefix(material: Optional[str]) -> str:
    """Build the unbounded auto-bin id prefix for one material group.

    Each material group runs its own unbounded pass over a fresh pool of
    auto-allocated bins; prefixing the ids with the sanitized material
    keeps them unique across groups and identifiable in reports.
    Material-less groups keep the historical ``default_plate_{i}`` naming
    (empty prefix).

    Args:
        material: The group's display material, or ``None`` (unset).

    Returns:
        Prefix ending in an underscore (e.g. ``"wkuv_"``), or ``""``.
    """
    if material is None:
        return ""
    sanitized = re.sub(r"[^0-9a-zA-Z]", "", material) or "x"
    return f"{sanitized}_"


def _group_rotation_preference(
    plates: Sequence[PlateSpec], fallback: RotationPreference
) -> RotationPreference:
    """Return the rotation preference a pool of plates agrees on.

    The schema cascade fills every plate's ``rotation_preference`` from the
    job, so a pool normally has one answer, and that is what its packing pass
    ranks by. Plates that disagree -- an explicit per-plate override mixed
    into a pool with plates that inherited the job value -- fall back to
    ``fallback``, because one packing pass can rank by exactly one policy and
    the job-level value is the shop's knob.

    Args:
        plates: The plates of one packing pool.
        fallback: Preference to use when the pool has no unanimous answer.

    Returns:
        The pool's rotation preference.
    """
    declared = {plate.rotation_preference for plate in plates}
    if len(declared) == 1:
        (only,) = declared
        if only is not None:
            return only
    return fallback


def _claim_plates_by_material(
    plates: Sequence[PlateSpec],
    material_groups: Mapping[Optional[str], list[_RectEntry]],
) -> dict[Optional[str], list[PlateSpec]]:
    """Partition a pool of unpinned plates into per-material pools.

    A plate declaring ``material`` joins exactly that group's pool; a
    plate whose declared material matches no label group stays unused
    (WARNING). Material-less plates are distributed across the groups
    that have entries, each plate joining the group with the fewest
    plates claimed so far (content order breaks ties): a plate carries
    exactly one material, so a claimed plate accepts no other group, and
    spreading the pool keeps every material group from starving. A single
    group therefore claims the entire pool, exactly like the historical
    no-material behavior.

    Args:
        plates: Unpinned plates in declaration order.
        material_groups: Content-ordered material-key to entries mapping
            from :func:`_split_material_entries`.

    Returns:
        Mapping of material key to the plates claimed by that group
        (declaration order preserved within each pool).
    """
    claimed: dict[Optional[str], list[PlateSpec]] = {key: [] for key in material_groups}
    unclaimed: list[PlateSpec] = []
    for plate in plates:
        key = material_key(plate.material)
        if key is None:
            unclaimed.append(plate)
        elif key in claimed:
            claimed[key].append(plate)
        else:
            logger.warning(
                "Plate '%s' declares material '%s' that no label uses; the plate stays unused.",
                plate.id,
                plate.material,
            )
    for plate in unclaimed:
        target = min(
            (key for key, entries in material_groups.items() if entries),
            key=lambda key: len(claimed[key]),
        )
        claimed[target].append(plate)
    return claimed


def _pack_pinned_entries(
    pinned: Mapping[str, list[_RectEntry]],
    provided_plates: Optional[list[PlateSpec]],
    allow_rotation: bool,
    layout: LayoutMode,
    clearances: Mapping[str, tuple[float, float]],
    rotation_preference: RotationPreference = RotationPreference.FILL_AXIS,
    plate_fill: PlateFill = PlateFill.PLATE_MAJOR,
) -> list[PackedPlate]:
    """Pack plate-pinned entries, one exclusive single-plate pass each.

    Each pinned plate receives exactly its pinned entries and nothing
    else; the pass uses the plate's own fill-order override and edge
    clearances. Packed plates are returned in plate declaration order.

    Args:
        pinned: Mapping of plate id to its pinned entries (from
            :func:`_split_pinned_entries`).
        provided_plates: Constrained-mode plates (pinned ids must be
            declared there).
        allow_rotation: Whether rotated candidates are considered.
        layout: Job-level fill-order fallback for plates without an
            explicit ``layout``.
        clearances: Bin-id to ``(left, top)`` clearance mapping.
        rotation_preference: Job-level ranking preference; each pinned plate
            ranks by its own ``rotation_preference`` (the cascade fills it in
            from the job when the plate declares none).

    Returns:
        The packed pinned plates (declaration order).

    Raises:
        LayoutFitError: If a pinned plate id is not declared, or a pinned
            label does not fit on its plate.
    """
    if not pinned:
        return []
    if not provided_plates:
        raise LayoutFitError(
            "Labels are pinned to specific plates but the job declares no "
            "plates; pinned labels require an explicit plate list."
        )

    plate_by_id = {plate.id: plate for plate in provided_plates}
    unknown = sorted(set(pinned) - set(plate_by_id))
    if unknown:
        raise LayoutFitError(
            "Labels are pinned to undeclared plate(s): "
            f"{', '.join(unknown)}. Declare these plates or remove the pinning."
        )

    packed_plates: list[PackedPlate] = []
    for plate in provided_plates:
        entries = pinned.get(plate.id)
        if not entries:
            continue
        plate_key = material_key(plate.material)
        if plate_key is not None:
            conflicts = sorted(
                {
                    label_material
                    for entry in entries
                    if (label_material := entry[2][1].material) is not None
                    and material_key(label_material) != plate_key
                }
            )
            if conflicts:
                raise LayoutFitError(
                    f"Labels pinned to plate '{plate.id}' (material "
                    f"'{plate.material}') carry conflicting material(s): "
                    f"{', '.join(conflicts)}. A plate carries exactly one "
                    "material: match the label materials to the plate or "
                    "remove the pinning."
                )
        plates = _pack_group(
            entries,
            [(plate.width, plate.height, plate.id)],
            allow_rotation,
            plate.layout or layout,
            clearances,
            material=plate.material,
            rotation_preference=plate.rotation_preference or rotation_preference,
            plate_fill=plate.plate_fill or plate_fill,
        )
        placed_ids = {packed.label_id for packed_plate in plates for packed in packed_plate.labels}
        leftover = [entry for entry in entries if entry[2][0] not in placed_ids]
        if leftover:
            raise LayoutFitError(
                f"Could only fit {len(entries) - len(leftover)} of "
                f"{len(entries)} label(s) pinned to plate '{plate.id}'. "
                "Pinned labels never overflow onto other plates: specify a "
                "larger plate or remove the pinning."
            )
        packed_plates.extend(plates)
    return packed_plates


def _pack_entries_with_pinners(
    rect_with_rid: list[_RectEntry],
    provided_plates: Optional[list[PlateSpec]],
    allow_rotation: bool,
    layout: LayoutMode,
    default_plate_size: Optional[tuple[float, float]],
    default_plate_clearance: Optional[tuple[float, float]],
    rotation_preference: RotationPreference = RotationPreference.FILL_AXIS,
    plate_fill: PlateFill = PlateFill.PLATE_MAJOR,
) -> tuple[list[PackedPlate], list[_RectEntry]]:
    """Pack entries, dispatching round labels to the adjacent ellipse pipeline.

    Rectangular labels (``shape_type == "rect"``) pack through the existing
    ``rectpack`` machinery -- :func:`_pack_rect_entries_with_pinners` runs
    them exactly as before (bit-identical for every job without round
    labels). ``round`` labels then pack through the ellipse pipeline
    (:func:`_pack_round_entries_with_pinners`) onto the same plates,
    seeded with the rectangular placements as occupancy so an ellipse never
    overlaps a rectangle. A job with no round labels returns the
    rectangular result untouched.

    Args:
        rect_with_rid: All ``(pack_width, pack_height, rid)`` tuples.
        provided_plates: User-specified plates, or ``None`` / empty for
            unbounded mode.
        allow_rotation: Whether rotated candidates are considered.
        layout: Job-level fill-order mode (maps to the ellipse packer's
            scrap direction: ``columns`` -> PREFER_WIDTH, ``rows`` ->
            PREFER_HEIGHT).
        default_plate_size: Auto-allocated bin size override (unbounded).
        default_plate_clearance: Auto-allocated bin clearance (unbounded).

    Returns:
        ``(packed_plates, leftover)`` with the same contract as
        :func:`_pack_rect_entries_with_pinners`; round placements merge into
        the plates sharing their id, and round-only plates (pinned
        round-only plates, unbounded ``round_default_plate_{i}`` bins) are
        appended after the rectangular plates.

    Raises:
        LayoutFitError: Propagated from both pipelines (pinning/material
            failures on either cohort).
    """
    rect_entries, round_entries = _split_shape_entries(rect_with_rid)

    rect_plates, rect_leftover = _pack_rect_entries_with_pinners(
        rect_entries,
        provided_plates,
        allow_rotation,
        layout,
        default_plate_size,
        default_plate_clearance,
        rotation_preference,
        plate_fill,
    )
    if not round_entries:
        # Rectangular-only job: the historical one-pipeline behavior,
        # bit-identical (no ellipse module import, no extra work).
        return rect_plates, rect_leftover

    occupancy = _rect_occupancy_by_plate(rect_plates)
    round_plates, round_leftover = _pack_round_entries_with_pinners(
        round_entries,
        provided_plates,
        layout,
        default_plate_size,
        default_plate_clearance,
        _plate_clearances(provided_plates),
        occupancy,
    )

    # Merge round placements into the plates sharing their id; round-only
    # plates append after the rectangular ones (stable order).
    plate_by_id = {plate.plate_id: plate for plate in rect_plates}
    merged: list[PackedPlate] = list(rect_plates)
    for plate in round_plates:
        target = plate_by_id.get(plate.plate_id)
        if target is not None:
            target.labels.extend(plate.labels)
        else:
            plate_by_id[plate.plate_id] = plate
            merged.append(plate)
    return merged, rect_leftover + round_leftover


def _rect_occupancy_by_plate(
    plates: Sequence[PackedPlate],
) -> dict[str, list[tuple[float, float, float, float]]]:
    """Convert packed rectangular placements into ellipse-packer occupancy.

    Each packed label's device-frame rectangle maps into the ellipse
    packer's frame -- the plate's **usable area**, y measured from its top
    edge (the emitted device convention) -- by subtracting the plate's
    clearances, so the round pipeline seeds its collision union with the
    space the rectangles already occupy.

    Args:
        plates: The rectangular pipeline's packed plates.

    Returns:
        Mapping of plate id to ``(x_min, y_min, x_max, y_max)`` usable-area
        rectangles (empty for plates with no labels).
    """
    occupancy: dict[str, list[tuple[float, float, float, float]]] = {}
    for plate in plates:
        rects: list[tuple[float, float, float, float]] = []
        for label in plate.labels:
            # Device frame: x from material left, y from material top
            # (y-down). Usable-area frame: subtract the clearances.
            x_min = label.x - plate.left_clearance
            y_min = label.y - plate.top_clearance
            rects.append((x_min, y_min, x_min + label.width, y_min + label.height))
        if rects:
            occupancy[plate.plate_id] = rects
    return occupancy


def _pack_rect_entries_with_pinners(
    rect_with_rid: list[_RectEntry],
    provided_plates: Optional[list[PlateSpec]],
    allow_rotation: bool,
    layout: LayoutMode,
    default_plate_size: Optional[tuple[float, float]],
    default_plate_clearance: Optional[tuple[float, float]],
    rotation_preference: RotationPreference = RotationPreference.FILL_AXIS,
    plate_fill: PlateFill = PlateFill.PLATE_MAJOR,
) -> tuple[list[PackedPlate], list[_RectEntry]]:
    """Pack entries honoring plate pinning, then material-partitioned packing.

    Pinned entries (see :func:`_split_pinned_entries`) pack first,
    exclusively onto their declaring plates; those plates are then
    removed from the pool so unpinned labels never share them. The
    unpinned remainder is partitioned by ``ResolvedLabel.material`` (see
    :func:`_split_material_entries`) and each material group packs in its
    own pass: in constrained mode the plates are partitioned per material
    (see :func:`_claim_plates_by_material`) so a plate carries exactly one
    material; in unbounded mode each group auto-allocates its own bin pool
    (material-prefixed ``default_plate_{i}`` ids). A job without materials
    has exactly one ``None`` group, whose pass is bit-identical to the
    historical one-pass behavior.

    Args:
        rect_with_rid: All ``(pack_width, pack_height, rid)`` tuples.
        provided_plates: User-specified plates, or ``None`` / empty for
            unbounded mode.
        allow_rotation: Whether rotated candidates are considered.
        layout: Job-level fill-order mode.
        default_plate_size: Auto-allocated bin size override (unbounded).
        default_plate_clearance: Auto-allocated bin clearance (unbounded).
        rotation_preference: Job-level ranking preference. Each pool ranks by
            the preference its plates agree on (see
            :func:`_group_rotation_preference`) and each pinned plate by its
            own, so a per-plate override is honoured wherever a pass packs
            exactly that plate's labels.

    Returns:
        ``(packed_plates, leftover)`` where packed plates are pinned
        plates first (declaration order) followed by the material-group
        packing results (group order, then plate order within a group),
        and leftover holds only unpinned entries no plate could place.
        Pinned-fit failures raise directly.

    Raises:
        LayoutFitError: If pinning is impossible (undeclared/absent
            plates), pinned labels do not fit their plates, a material
            group has no plate to pack onto, or a material group overflows
            its claimed plates.
    """
    pinned = _split_pinned_entries(rect_with_rid)
    is_constrained = provided_plates is not None and len(provided_plates) > 0

    all_clearances = _plate_clearances(provided_plates)

    pinned_plates = _pack_pinned_entries(
        pinned,
        provided_plates,
        allow_rotation,
        layout,
        all_clearances,
        rotation_preference,
        plate_fill,
    )

    unpinned_entries = (
        rect_with_rid
        if not pinned
        else [entry for entry in rect_with_rid if entry[2][1].plate_id is None]
    )
    unpinned_plates: list[PackedPlate] = []
    leftover: list[_RectEntry] = []
    if not unpinned_entries:
        return pinned_plates + unpinned_plates, leftover

    if is_constrained:
        assert provided_plates is not None
        unpinned_provided: Optional[list[PlateSpec]] = [
            plate for plate in provided_plates if plate.id not in pinned
        ]
        assert unpinned_provided is not None
        if not unpinned_provided:
            raise LayoutFitError(
                "Every declared plate is pinned to replacement-generated "
                f"label(s) but {len(unpinned_entries)} unpinned label(s) "
                "remain: declare an additional (unpinned) plate."
            )
        material_groups = _split_material_entries(unpinned_entries)
        pools = _claim_plates_by_material(unpinned_provided, material_groups)
        for key, entries in material_groups.items():
            pool = pools[key]
            if not pool:
                display = _group_display_material(entries)
                raise LayoutFitError(
                    f"{len(entries)} label(s) with material '{display}' have "
                    "no plate to pack onto: every declared plate belongs to "
                    "another material (materials never share a plate). "
                    "Declare an additional plate for this material."
                )
            groups = _resolve_plate_groups(pool, layout, len(entries))
            # Each plate keeps its own declared material spelling; plates
            # that omitted it (claimed material-less) report the group's
            # first-declared spelling.
            group_display = _group_display_material(entries)
            pool_materials: dict[str, Optional[str]] = {
                plate.id: (plate.material if plate.material is not None else group_display)
                for plate in pool
            }
            plates, group_leftover = _pack_groups(
                entries,
                groups,
                allow_rotation=allow_rotation,
                clearances=all_clearances,
                material=group_display,
                materials=pool_materials,
                rotation_preference=_group_rotation_preference(pool, rotation_preference),
                plate_fill=_group_plate_fill(pool, plate_fill),
            )
            if group_leftover:
                display = _group_display_material(entries)
                if display is None:
                    # Material-less group: propagate the leftover so the
                    # callers raise the historical generic fit error
                    # (bit-identical behavior for jobs without materials).
                    leftover.extend(group_leftover)
                else:
                    raise LayoutFitError(
                        f"Could only fit {len(entries) - len(group_leftover)} of "
                        f"{len(entries)} label(s) with material '{display}' on "
                        f"its plate(s) ({', '.join(plate.id for plate in pool)}). "
                        "Materials never share a plate: specify larger or "
                        "additional plates for this material."
                    )
            unpinned_plates.extend(plates)
    else:
        material_groups = _split_material_entries(unpinned_entries)
        for _key, entries in material_groups.items():
            display = _group_display_material(entries)
            groups = _resolve_plate_groups(
                None,
                layout,
                len(entries),
                default_plate_size=default_plate_size,
                bin_id_prefix=_material_bin_prefix(display),
            )
            clearances = _default_clearance_map(groups, default_plate_clearance)
            plates, group_leftover = _pack_groups(
                entries,
                groups,
                allow_rotation=allow_rotation,
                clearances=clearances,
                material=display,
                rotation_preference=rotation_preference,
                plate_fill=plate_fill,
            )
            unpinned_plates.extend(plates)
            leftover.extend(group_leftover)

    return pinned_plates + unpinned_plates, leftover


def _plate_footprint(packer: _Packer) -> float:
    """Compute the total used material area across a packed result.

    For each non-empty plate, computes the bounding box of its placed labels
    (``max_x * max_y`` in inches) rather than the full nominal plate size.
    Empty auto-allocated plates contribute zero. This metric rewards layouts
    that pack labels tightly into a small region and use fewer plates,
    regardless of whether multiple heuristics happen to share identical bin
    dimensions.

    Args:
        packer: A ``rectpack.Packer`` that has already executed ``pack()``.

    Returns:
        The sum over non-empty bins of the used bounding-box area.
    """
    footprint = 0.0
    for bin_obj in packer:
        if len(bin_obj) == 0:
            continue
        max_x = 0.0
        max_y = 0.0
        for rect in bin_obj:
            max_x = max(max_x, rect.x + rect.width)
            max_y = max(max_y, rect.y + rect.height)
        footprint += max_x * max_y
    return footprint


def _fill_axis_extents(packer: _Packer, transpose: bool) -> tuple[float, float]:
    """Measure how far the plates are filled along the plate's own axes.

    One plate's span is ``max(position + size)`` along an axis, the same
    bounding-box pass :func:`_plate_footprint` uses (clearance excluded), divided
    by that plate's own extent on the same axis. The ratio is what matters: it
    reads as "how much of the column (``columns``) or row (``rows``) did this
    layout fill", and it stays comparable across plate sizes. It also makes an
    auto-allocated plate (unbounded mode, sized to its content) neutral at 1.0
    for every orientation, so the fill-order preference never reorients a pack
    that has no plate to fill.

    The result reports the *least*-filled plate per axis, because the fill-order
    goal is to max out the plate that is filled least before adding another one;
    a sum would let several short plates outrank one full plate.

    Args:
        packer: A ``rectpack.Packer`` that has already executed ``pack()``.
        transpose: Whether the pack ran in the transposed frame (the ``columns``
            mode). There the packer's X axis is the plate's *height* and its Y
            axis the plate's width, so the spans are swapped back to plate space.

    Returns:
        ``(height_fill, width_fill)``: over the non-empty plates, the smallest
        used-height/plate-height and used-width/plate-width ratio. ``(0.0, 0.0)``
        when no plate carries a label.
    """
    height_fill: Optional[float] = None
    width_fill: Optional[float] = None
    for bin_obj in packer:
        if len(bin_obj) == 0:
            continue
        pack_right = 0.0
        pack_bottom = 0.0
        for rect in bin_obj:
            pack_right = max(pack_right, rect.x + rect.width)
            pack_bottom = max(pack_bottom, rect.y + rect.height)
        # In the transposed frame the packer's X axis is the plate's height.
        if transpose:
            used_height, used_width, tall_bin, wide_bin = (
                pack_right,
                pack_bottom,
                bin_obj.width,
                bin_obj.height,
            )
        else:
            used_height, used_width, tall_bin, wide_bin = (
                pack_bottom,
                pack_right,
                bin_obj.height,
                bin_obj.width,
            )
        bin_height = used_height / tall_bin if tall_bin else 0.0
        bin_width = used_width / wide_bin if wide_bin else 0.0
        height_fill = bin_height if height_fill is None else min(height_fill, bin_height)
        width_fill = bin_width if width_fill is None else min(width_fill, bin_width)
    if height_fill is None or width_fill is None:
        return (0.0, 0.0)
    return (height_fill, width_fill)


def _fill_rank_key(
    footprint: float,
    fill_axis: float,
    cross_axis: float,
    rotations: int,
    rotation_preference: RotationPreference = RotationPreference.FILL_AXIS,
) -> tuple[float, ...]:
    """Build the ranking key of a packing candidate that fits everything.

    ``footprint`` (plate material used, see :func:`_plate_footprint`) leads in
    both preferences: rotation never costs extra material, and material is the
    shop's currency. The remaining terms follow ``rotation_preference``:

    - :attr:`RotationPreference.FILL_AXIS` (the default, historical): the
      fill-axis ratio and then the cross-axis ratio prefer the fill order of the
      layout (see :func:`_fill_axis_extents`), and the rotation count decides
      only when both ratios tie -- two layouts that are geometrically identical
      keep producing the same output. The fill axis enters NEGATED (it is the
      span we want long, while every other term is one we want short).
    - :attr:`RotationPreference.MINIMIZE`: the rotation count moves to second,
      so a layout that engraves labels upright wins whenever it costs no extra
      material. The fill-order ratios stay on the tail of the key, so the
      anti-fragmentation preference still separates equally-rotated layouts
      (a plate filled to the bottom beats two half-filled plates).

    Floats are rounded to the packing-unit precision (0.01 in), which makes
    layouts that differ only by float noise compare exactly, while a genuine
    1/16 in fill difference still separates them.

    Args:
        footprint: Total used material area from :func:`_plate_footprint`.
        fill_axis: Least-filled plate's fill ratio along the axis the layout
            fills first (see :func:`_fill_axis_extents`); larger is better.
        cross_axis: The same plate's fill ratio along the other plate axis.
        rotations: Rotated rectangle count from :func:`_count_rotated`.
        rotation_preference: Which of the two is preferred, the fill order or
            the fewest rotations (see
            :attr:`plt_optimizer.generate.schema.JobSpec.rotation_preference`).

    Returns:
        The lexicographically comparable ranking tuple (smaller is better).
    """
    if rotation_preference is RotationPreference.MINIMIZE:
        return (round(footprint, 4), float(rotations), -round(fill_axis, 4), round(cross_axis, 4))
    return (round(footprint, 4), -round(fill_axis, 4), round(cross_axis, 4), rotations)


# Fill band of the plate-major comparison, as a fraction of the plate's fill
# axis. Two layouts whose plate is filled less than one band apart along that
# axis are "the same plate" for the ranking, so float noise and sub-band nudges
# cannot flip a shop's plate map (docs/plans/packing-quality.md, Proposed U2
# item 1), while one real row or column of material always separates them.
#
# A tenth of the axis, not a fiftieth: the layout report quotes fill at whole
# percentages, and a band this side of one label row makes the ranking decide on
# differences the shop cannot see. Measured on six labels (3,3,3,10,10,6 x 1")
# on a 24x16 sheet at ``layout=rows``: a rotated plan filling the width to
# 24.00" beat a flush two-row plan at 23.20" -- 3.3 % of one axis -- and paid 4"
# of plate height and six rotations for it. At 10 % both plans are "full"
# (bands 10 and 10), so the cross-axis ratio and the ink box decide, which is
# the ordering the complaint asked for. A plan that is one label row short is
# still separated: 121 of 128 3x1 labels on a 24x16 sheet fills to 94.5 %,
# band 9, and the flush grid that fits all 128 stays band 10.
PLATE_FILL_BAND: float = 0.10


@dataclass(frozen=True)
class _PlanMetrics:
    """The ranking metrics of one complete layout plan (every plate it uses).

    Attributes:
        sheet_area: Total sheet area of the non-empty plates, square inches --
            the material the shop buys, and the first term of the plan ranking.
        fill_bands: Per-plate fill ratio **along the axis the layout fills
            first** (``columns`` the plate height, ``rows`` the plate width), in
            :data:`PLATE_FILL_BAND` bands, in plate order (plate 1 first). This
            is what a plate-major ranking walks: plate 1 saturating beats plate
            2 packing tighter. It is fill *along the axis*, not used area --
            used area is axis-blind and would let a plate filled the wrong way
            outrank one that honours the layout's fill order.
        fill_axis: The least-filled plate's ratio along that same axis (see
            :func:`_fill_axis_extents`), the historical whole-layout fill-order
            term, kept behind the plate vector to break plate-vector ties.
        cross_axis: The least-filled plate's ratio along the other plate axis.
        area_fills: Per-plate label area over the plate's usable area, in
            declaration order. The axis ratios above say how far a plate is
            filled *along the fill axis*; this says how much of the sheet is
            covered by labels at all, which is the "is this sheet full?" the
            shop reads off the layout report.
        bbox_area: Total ink bounding-box area across the plates, square inches
            -- the scrap term, ranked below fill so a saturated plate 1 may cost
            a looser tail sheet.
        rotations: Rotated label count in the plan.
    """

    sheet_area: float
    fill_bands: Tuple[int, ...]
    fill_axis: float
    cross_axis: float
    area_fills: Tuple[float, ...]
    bbox_area: float
    rotations: int


def _plan_metrics_from_packer(packer: _Packer, transpose: bool) -> _PlanMetrics:
    """Measure a ``rectpack`` result as a layout plan.

    Plates are walked in bin order (the order they were declared), so the fill
    vector is plate-major and reproducible. Empty auto-allocated plates are
    skipped exactly like :func:`_plate_footprint` does. Fill is measured along
    the **packer's** X axis, which is the fill axis of both layouts: ``rows``
    packs in the real frame (X = plate width) and ``columns`` packs transposed
    (X = plate height), so the same pass matches what
    :func:`_fill_axis_extents` reports for the plan.

    Args:
        packer: A ``rectpack.Packer`` that has already executed ``pack()``.
        transpose: Whether the pack ran in the transposed (``columns``) frame.
            Only documents the frame the ratios are taken in -- the fill axis is
            the packer X axis either way.

    Returns:
        The plan's ranking metrics.
    """
    del transpose  # Documented above: the fill axis is the packer's X axis.
    sheet_area = 0.0
    bbox_area = 0.0
    bands: list[int] = []
    fills: list[float] = []
    crosses: list[float] = []
    areas: list[float] = []
    for bin_obj in packer:
        if len(bin_obj) == 0:
            continue
        right = 0.0
        bottom = 0.0
        for rect in bin_obj:
            right = max(right, rect.x + rect.width)
            bottom = max(bottom, rect.y + rect.height)
        fill = right / bin_obj.width
        sheet_area += bin_obj.width * bin_obj.height
        bbox_area += right * bottom
        bands.append(int(round(fill / PLATE_FILL_BAND)))
        fills.append(fill)
        crosses.append(bottom / bin_obj.height)
        areas.append(
            sum(rect.width * rect.height for rect in bin_obj) / (bin_obj.width * bin_obj.height)
            if bin_obj.width * bin_obj.height > 0
            else 0.0
        )
    return _PlanMetrics(
        sheet_area=sheet_area,
        fill_bands=tuple(bands),
        fill_axis=min(fills) if fills else 0.0,
        cross_axis=min(crosses) if crosses else 0.0,
        area_fills=tuple(areas),
        bbox_area=bbox_area,
        rotations=_count_rotated(packer),
    )


def _plan_metrics_from_plates(
    plates: Sequence[PackedPlate], *, fill_is_height: bool = False
) -> _PlanMetrics:
    """Measure a produced plate list as a layout plan.

    Mirrors :func:`_plan_metrics_from_packer` for plans that never went through
    ``rectpack`` (the flush-grid candidates), so both kinds of plan compare on
    identical terms: plate order is the produced order, fill is the placement
    extent along the layout's fill axis, and the bbox is the placement envelope
    including clearances (which every plan of this group carries identically).

    Args:
        plates: The non-empty ``PackedPlate`` objects of one plan, in plate
            order, in **real** plate space (the grid plan is never transposed).
        fill_is_height: ``True`` for a ``columns`` group, whose fill axis is the
            plate height; ``False`` (``rows``) measures along the plate width.

    Returns:
        The plan's ranking metrics.
    """
    sheet_area = 0.0
    bbox_area = 0.0
    bands: list[int] = []
    fills: list[float] = []
    crosses: list[float] = []
    areas: list[float] = []
    rotations = 0
    for plate in plates:
        if not plate.labels:
            continue
        right = max(label.x + label.width for label in plate.labels)
        bottom = max(label.y + label.height for label in plate.labels)
        fill = bottom / plate.height if fill_is_height else right / plate.width
        cross = right / plate.width if fill_is_height else bottom / plate.height
        sheet_area += plate.width * plate.height
        bbox_area += right * bottom
        bands.append(int(round(fill / PLATE_FILL_BAND)))
        fills.append(fill)
        crosses.append(cross)
        usable = plate.width * plate.height
        areas.append(
            sum(label.width * label.height for label in plate.labels) / usable
            if usable > 0
            else 0.0
        )
        rotations += sum(1 for label in plate.labels if label.rotated)
    return _PlanMetrics(
        sheet_area=sheet_area,
        fill_bands=tuple(bands),
        fill_axis=min(fills) if fills else 0.0,
        cross_axis=min(crosses) if crosses else 0.0,
        area_fills=tuple(areas),
        bbox_area=bbox_area,
        rotations=rotations,
    )


def _plate_fill_rank_key(
    metrics: _PlanMetrics,
    rotation_preference: RotationPreference = RotationPreference.FILL_AXIS,
) -> Tuple[Any, ...]:
    """Build the plate-major ranking key of a complete layout plan.

    Sheet area leads, so a layout that saturates plate 1 never costs an extra
    sheet -- the shop's material is untouched by the fill preference. The fill
    vector is then compared plate by plate (negated bands, larger fill first),
    which is the greedy preference: at equal material, the plan whose first
    plate is fullest wins, and only a tie on plate 1 looks at plate 2. Behind
    the vector sit the historical whole-layout terms -- the least-filled plate's
    fill-axis ratio, then its cross-axis ratio, then the ink bbox -- so a plan
    that fills along the axis ``layout`` fills still beats one that fills it the
    wrong way, and the "tighter total" preference now decides only what the
    plate fills and fill order leave open. The rotation count closes the key
    (``minimize`` promotes it, mirroring :func:`_fill_rank_key`).

    The axis ratios are quantized to :data:`PLATE_FILL_BAND` (a tenth of the
    axis), not compared exactly. An exact ratio lets a plan that squeezes a few
    extra tenths out of the fill axis outrank one that is inches shorter across
    the plate: six 3x1 labels on a 24x16 sheet, a rotated plan that fills the
    width to 24.00" beat a flush two-row plan at 23.20" and paid 4" of plate
    height plus six rotations for the 3.3 %. Measuring fill at the granularity
    the shop reads makes near-full fills tie, so the cross-axis ratio, the ink
    box and the rotation count decide -- which is the ordering the complaint
    asked for.

    Args:
        metrics: The plan's measured metrics.
        rotation_preference: Rotation ranking forwarded from the group.

    Returns:
        The lexicographically comparable ranking tuple (smaller is better).
    """
    sheet = round(metrics.sheet_area, 4)
    fills = tuple(-band for band in metrics.fill_bands)
    areas = tuple(-round(area, 6) for area in metrics.area_fills)
    fill_axis = -int(round(metrics.fill_axis / PLATE_FILL_BAND))
    cross_axis = int(round(metrics.cross_axis / PLATE_FILL_BAND))
    bbox = round(metrics.bbox_area, 4)
    if rotation_preference is RotationPreference.MINIMIZE:
        return (sheet, metrics.rotations, fills, areas, fill_axis, cross_axis, bbox)
    return (sheet, fills, areas, fill_axis, cross_axis, bbox, metrics.rotations)


def _group_plate_fill(plates: Sequence[PlateSpec], fallback: PlateFill) -> PlateFill:
    """Return the plate-fill preference a pool of plates agrees on.

    Mirrors :func:`_group_rotation_preference`: the schema cascade fills every
    plate's ``plate_fill`` from the job, so a pool normally has one answer;
    plates that disagree fall back to ``fallback`` because one packing pass
    ranks by exactly one policy.

    Args:
        plates: The plates of one packing pool.
        fallback: Preference to use when the pool has no unanimous answer.

    Returns:
        The pool's plate-fill preference.
    """
    declared = {plate.plate_fill for plate in plates}
    if len(declared) == 1:
        (only,) = declared
        if only is not None:
            return only
    return fallback


def _split_shape_entries(
    entries: list[_RectEntry],
) -> tuple[list[_RectEntry], list[_RectEntry]]:
    """Split rectangle entries into rectangular and round (ellipse) groups.

    A label is round when its resolved ``shape_type`` is ``round`` (see
    :attr:`plt_optimizer.generate.resolution.ResolvedLabel.shape_type`).
    Rectangular entries keep packing through ``rectpack``; round entries
    route to the adjacent ellipse-packing pipeline. A job with no round
    labels yields an empty round group, so the rectangular path runs
    exactly as before.

    Args:
        entries: Real-space ``(pack_width, pack_height, rid)`` tuples.

    Returns:
        ``(rect_entries, round_entries)`` in the original relative order.
    """
    rect: list[_RectEntry] = []
    round_: list[_RectEntry] = []
    for entry in entries:
        if entry[2][1].shape_type == LabelShape.ROUND.value:
            round_.append(entry)
        else:
            rect.append(entry)
    return rect, round_


def _pack_round_entries_on_plate(
    round_entries: list[_RectEntry],
    plate_width: float,
    plate_height: float,
    left_clearance: float,
    top_clearance: float,
    plate_id: str,
    material: Optional[str],
    clearance: float,
    mode: ScrapDirectionValue,
    occupancy: Sequence[tuple[float, float, float, float]],
) -> tuple[PackedPlate, list[_RectEntry]]:
    """Pack round entries onto one plate through the ellipse packer.

    Round labels pack as ellipses with semi-axes ``pack_width / 2`` and
    ``pack_height / 2`` (the rendered bounding-box dimensions), buffered by
    the inter-label ``clearance``. The ellipse packer returns plate-frame
    y-down centers (the emitted device convention, the frame rectpack fills
    in), so each placement maps to the ``PackedLabel`` rectangle plate
    assembly consumes by adding the plate clearances -- no Y flip. The
    label's slot is the ellipse's enclosing rectangle.

    Args:
        round_entries: The round ``(pack_width, pack_height, rid)`` tuples.
        plate_width: Usable plate width, inches.
        plate_height: Usable plate height, inches.
        left_clearance: Plate left-edge clearance, inches.
        top_clearance: Plate top-edge clearance, inches.
        plate_id: The plate id to stamp on the result.
        material: Material stamped on the plate (``None`` = material-agnostic).
        clearance: Inter-label kerf, inches.
        mode: Scrap direction (from the plate's fill-order mode).
        occupancy: ``(x_min, y_min, x_max, y_max)`` rectangles (usable-area
            frame, y-up) already occupied by rectangular labels.

    Returns:
        ``(plate, leftover)`` where ``plate`` carries the placements (empty
        when nothing fit) and ``leftover`` holds the entries that did not fit.
    """
    from plt_optimizer.generate.packing.ellipse_packer import (
        EllipseSpec,
        ScrapDirection,
        pack_ellipses,
    )

    specs = [EllipseSpec(entry[2][0], entry[0] / 2.0, entry[1] / 2.0) for entry in round_entries]
    result = pack_ellipses(
        plate_width,
        plate_height,
        specs,
        mode=ScrapDirection(mode),
        clearance=clearance,
        occupancy=occupancy,
    )
    entry_by_id = {entry[2][0]: entry for entry in round_entries}
    plate = PackedPlate(
        plate_id=plate_id,
        width=plate_width,
        height=plate_height,
        left_clearance=left_clearance,
        top_clearance=top_clearance,
        material=material,
    )
    for placed in result.placed:
        entry = entry_by_id[placed.item_id]
        _pack_w, _pack_h, (rect_id, source_label, _pack_width, _seq) = entry
        # The packer works in the usable-area frame with y measured from its
        # top edge -- the emitted device convention, the same frame rectpack
        # fills -- so the placement maps to the device frame by adding the
        # clearances. The label's bounding box is the ellipse's enclosing
        # rectangle.
        bbox_w = placed.semi_x * 2.0
        bbox_h = placed.semi_y * 2.0
        device_x = placed.center_x - placed.semi_x + left_clearance
        device_y = placed.center_y - placed.semi_y + top_clearance
        plate.labels.append(
            PackedLabel(
                label_id=rect_id,
                x=device_x,
                y=device_y,
                width=bbox_w,
                height=bbox_h,
                rotated=placed.rotation_deg == 90,
                source_label=source_label,
            )
        )
    placed_ids = {label.label_id for label in plate.labels}
    leftover = [entry for entry in round_entries if entry[2][0] not in placed_ids]
    return plate, leftover


def _round_clearance(entries: Sequence[_RectEntry]) -> float:
    """Return the inter-label kerf for one round packing pass, inches.

    Ellipse borders are cut by the boundary/hole tool, so two adjacent
    round labels keep at least the widest engraved border stroke of the
    group edge-to-edge (the border ink never touches; the cutter width
    includes the `cutter_buffer_routing` kerf buffer). Falls back to the
    ellipse packer's 1/16in default when the labels carry no cutter
    information.

    Args:
        entries: The round entries of one pass.

    Returns:
        The kerf/spacing clearance, inches.
    """
    cutters = [entry[2][1].effective_hole_cutter_diameter for entry in entries]
    return max(cutters, default=DEFAULT_ROUND_CLEARANCE_IN)


def _scrap_direction(layout: LayoutMode) -> ScrapDirectionValue:
    """Map the fill-order mode onto the ellipse packer's scrap direction.

    ``columns`` (column-major, the default) grows up the plate first and
    minimizes used width -- PREFER_WIDTH, leaving a clean vertical scrap
    strip. ``rows`` minimizes used height -- PREFER_HEIGHT, leaving a clean
    horizontal strip. The simple grid modes map to their bin-packed
    counterparts (round labels always keep the ellipse optimization
    regardless of the fill-order mode).

    Args:
        layout: The plate/job fill-order mode.

    Returns:
        The matching :class:`~plt_optimizer.generate.packing.ellipse_packer.ScrapDirection`
        value.
    """
    if layout in (LayoutMode.ROWS, LayoutMode.ROWS_SIMPLE):
        return ScrapDirectionValue.PREFER_HEIGHT
    return ScrapDirectionValue.PREFER_WIDTH


def _pack_round_entries_with_pinners(
    round_entries: list[_RectEntry],
    provided_plates: Optional[list[PlateSpec]],
    layout: LayoutMode,
    default_plate_size: Optional[tuple[float, float]],
    default_plate_clearance: Optional[tuple[float, float]],
    clearances: Mapping[str, tuple[float, float]],
    occupancy: Mapping[str, Sequence[tuple[float, float, float, float]]],
) -> tuple[list[PackedPlate], list[_RectEntry]]:
    """Pack all round entries through the adjacent ellipse pipeline.

    Mirrors :func:`_pack_rect_entries_with_pinners` for the round cohort
    only: pinned round labels pack first onto their declaring plate, then
    the unpinned remainder packs per material group (materials never share
    a plate). Each plate runs the ellipse packer once; leftover entries
    cascade to the next plate in declaration order. Rectangular placements
    (from the occupancy mapping) seed every plate's collision union, so an
    ellipse never overlaps a rectangle.

    Args:
        round_entries: The round ``(pack_width, pack_height, rid)`` tuples.
        provided_plates: Constrained-mode plates, or ``None`` for unbounded.
        layout: Job-level fill order (maps to the scrap direction).
        default_plate_size: Unbounded auto-bin size override.
        default_plate_clearance: Unbounded auto-bin clearance.
        clearances: Bin-id to ``(left, top)`` clearance mapping.
        occupancy: Plate-id to occupied rectangles (usable-area frame) from
            the rectangular pipeline.

    Returns:
        ``(packed_plates, leftover)`` in the same contract as
        :func:`_pack_rect_entries_with_pinners`.

    Raises:
        LayoutFitError: If a pinned round label does not fit its plate, a
            material group has no plate, or a constrained material group
            overflows its pool.
    """
    if not round_entries:
        return [], []

    is_constrained = provided_plates is not None and len(provided_plates) > 0
    plate_by_id = {plate.id: plate for plate in provided_plates or []}
    clearance = _round_clearance(round_entries)
    mode = _scrap_direction(layout)

    def _clearance_of(plate_id: str, plate: Optional[PlateSpec]) -> tuple[float, float]:
        if plate is not None:
            return clearances.get(plate_id, (plate.left_clearance, plate.top_clearance))
        return clearances.get(plate_id, default_plate_clearance or (0.0, 0.0))

    packed_plates: list[PackedPlate] = []
    leftover: list[_RectEntry] = []

    # Pinned round labels: one exclusive pass per declaring plate.
    pinned_ids = {e[2][1].plate_id for e in round_entries if e[2][1].plate_id is not None}
    for plate_id in sorted(pinned_ids):
        plate = plate_by_id.get(plate_id)
        if plate is None:
            raise LayoutFitError(f"Round labels are pinned to undeclared plate '{plate_id}'.")
        pinned = [e for e in round_entries if e[2][1].plate_id == plate_id]
        left, top = _clearance_of(plate_id, plate)
        packed, pinned_leftover = _pack_round_entries_on_plate(
            pinned,
            plate.width,
            plate.height,
            left,
            top,
            plate_id,
            plate.material,
            clearance,
            mode,
            occupancy.get(plate_id, ()),
        )
        if pinned_leftover:
            raise LayoutFitError(
                f"Could only fit {len(pinned) - len(pinned_leftover)} of "
                f"{len(pinned)} round label(s) pinned to plate '{plate_id}'. "
                "Pinned labels never overflow onto other plates."
            )
        if packed.labels:
            packed_plates.append(packed)

    unpinned = [e for e in round_entries if e[2][1].plate_id is None]
    if not unpinned:
        return packed_plates, leftover

    material_groups = _split_material_entries(unpinned)

    if is_constrained:
        assert provided_plates is not None
        unpinned_plates = [p for p in provided_plates if p.id not in pinned_ids]
        if not unpinned_plates:
            raise LayoutFitError(
                f"{len(unpinned)} unpinned round label(s) remain but every "
                "declared plate is pinned to other labels."
            )
        pools = _claim_plates_by_material(unpinned_plates, material_groups)
        for key, entries in material_groups.items():
            pool = pools[key]
            display = _group_display_material(entries)
            if not pool:
                raise LayoutFitError(
                    f"{len(entries)} round label(s) with material "
                    f"'{display}' have no plate to pack onto."
                )
            remaining = list(entries)
            for plate in pool:
                if not remaining:
                    break
                left, top = _clearance_of(plate.id, plate)
                packed, remaining = _pack_round_entries_on_plate(
                    remaining,
                    plate.width,
                    plate.height,
                    left,
                    top,
                    plate.id,
                    plate.material if plate.material is not None else display,
                    clearance,
                    mode,
                    occupancy.get(plate.id, ()),
                )
                if packed.labels:
                    packed_plates.append(packed)
            if remaining and display is not None:
                raise LayoutFitError(
                    f"Could only fit {len(entries) - len(remaining)} of "
                    f"{len(entries)} round label(s) with material '{display}' "
                    "on its plate(s). Materials never share a plate."
                )
            leftover.extend(remaining)
    else:
        for _key, entries in material_groups.items():
            display = _group_display_material(entries)
            width, height = default_plate_size or (
                DEFAULT_PLATE_WIDTH,
                DEFAULT_PLATE_HEIGHT,
            )
            left, top = default_plate_clearance or (0.0, 0.0)
            prefix = _material_bin_prefix(display)
            remaining = list(entries)
            sheet = 0
            while remaining:
                plate_id = f"{prefix}round_default_plate_{sheet}"
                packed, remaining = _pack_round_entries_on_plate(
                    remaining,
                    width,
                    height,
                    left,
                    top,
                    plate_id,
                    display,
                    clearance,
                    mode,
                    (),
                )
                if not packed.labels:
                    # Nothing fit an empty plate: the label exceeds the plate.
                    leftover.extend(remaining)
                    break
                packed_plates.append(packed)
                sheet += 1
            leftover.extend(remaining)

    return packed_plates, leftover


def _pack_groups(
    rectangles_with_rid: list[_RectEntry],
    groups: list[tuple[list[tuple[float, float, str]], LayoutMode]],
    allow_rotation: bool,
    clearances: Optional[Mapping[str, tuple[float, float]]] = None,
    material: Optional[str] = None,
    materials: Optional[Mapping[str, Optional[str]]] = None,
    rotation_preference: RotationPreference = RotationPreference.FILL_AXIS,
    plate_fill: PlateFill = PlateFill.PLATE_MAJOR,
) -> tuple[list[PackedPlate], list[_RectEntry]]:
    """Pack rectangles through sequential same-mode plate groups.

    Groups are processed in declaration order; each group only ever sees the
    label instances left over from the previous groups, so a plate's fill
    order governs exactly the labels that reach it. A single group (every
    unbounded job and every single-mode job) therefore behaves exactly like
    the historical one-pass packing.

    Args:
        rectangles_with_rid: All ``(pack_width, pack_height, rid)`` tuples.
        groups: Declaration-ordered ``(bin_specs, layout)`` groups from
            :func:`_resolve_plate_groups`.
        allow_rotation: Whether rotated candidates are considered.
        clearances: Optional bin-id to ``(left_clearance, top_clearance)``
            mapping (see :func:`_plate_clearances`).
        material: Stock material stamped onto every produced plate (see
            :attr:`PackedPlate.material`). ``None`` (the default) keeps
            plates material-agnostic.
        materials: Optional bin-id to declared-material-spelling mapping
            forwarded to :func:`_pack_group`.
        rotation_preference: Ranking preference forwarded to every group's
            :func:`_pack_group` call.

    Returns:
        A tuple of ``(packed_plates, leftover_rectangles)`` where
        ``leftover_rectangles`` are the entries no group could place (empty
        on success).
    """
    remaining = list(rectangles_with_rid)
    all_plates: list[PackedPlate] = []

    for bin_specs, group_layout in groups:
        if not remaining or not bin_specs:
            continue
        plates = _pack_group(
            remaining,
            bin_specs,
            allow_rotation,
            group_layout,
            clearances,
            material,
            materials,
            rotation_preference=rotation_preference,
            plate_fill=plate_fill,
        )
        if not plates:
            continue
        placed_ids = {packed.label_id for plate in plates for packed in plate.labels}
        remaining = [entry for entry in remaining if entry[2][0] not in placed_ids]
        all_plates.extend(plates)

    return all_plates, remaining


def generate_layout(
    resolved_labels: list[ResolvedLabel],
    provided_plates: Optional[list[PlateSpec]] = None,
    allow_rotation: bool = True,
    layout: LayoutMode = DEFAULT_LAYOUT_MODE,
    default_plate_size: Optional[tuple[float, float]] = None,
    default_plate_clearance: Optional[tuple[float, float]] = None,
    rotation_preference: RotationPreference = RotationPreference.FILL_AXIS,
    plate_fill: PlateFill = PlateFill.PLATE_MAJOR,
) -> list[PackedPlate]:
    """Pack resolved labels onto physical plates.

    Handles both constrained mode (user-specified plates) and unbounded
    mode (auto-allocating 24x16 default sheets).

    Args:
        resolved_labels: Flat list of fully resolved labels from the
            resolution engine.
        provided_plates: Optional list of user-specified plates. If None
            or empty, the engine auto-allocates default 24x16 sheets.
        allow_rotation: When True (the default), the packer may rotate
            label instances 90 degrees when it improves the fit. Rotated
            labels are flagged ``PackedLabel.rotated`` so plate assembly
            can rotate their content clockwise accordingly.
        layout: Preferential fill order. ``columns`` (the default) fills
            each plate's height before extending rightward; ``rows`` fills
            width before extending downward (the historical behavior).
            ``columns-simple`` / ``rows-simple`` bypass ``rectpack`` with a
            flush content-order grid fill (never rotated; see
            :mod:`plt_optimizer.generate.packing.simple_grid`).
            A plate may override the job value via ``PlateSpec.layout``;
            mixed modes pack in sequential declaration-ordered passes.
        default_plate_size: ``(width, height)`` override (inches) for the
            auto-allocated unbounded bins (from ``job-config.json``); the
            module defaults apply when ``None``.
        default_plate_clearance: ``(left, top)`` edge clearance (inches)
            applied to every auto-allocated unbounded bin (from
            ``job-config.json``); ignored when plates are provided.
        rotation_preference: Ranking preference between packings that fit --
            the ``layout`` fill order (default, historical) or the fewest
            rotated labels (see :func:`_fill_rank_key`).

    Returns:
        A list of ``PackedPlate`` objects containing all successfully
        packed labels. Labels pinned via ``ResolvedLabel.plate_id`` pack
        exclusively onto their declaring plate (pinned plates first, in
        declaration order).

    Raises:
        LayoutFitError: If constrained plates cannot fit all labels, or
            if a single label exceeds the default 24x16 plate size (in
            either orientation) in unbounded mode, or plate pinning
            cannot be honored (undeclared plate, pinned labels that do
            not fit their plate, or unpinned labels with no unpinned
            plate left).

    Example:
        >>> plates = generate_layout(resolved_labels)
        >>> len(plates) >= 1
        True
    """
    rectangles = unroll_labels(resolved_labels)

    # Build rectangle entries with their (rid) payloads. The packing width
    # travels with the payload so _extract_packed_plates can detect rotation
    # against the dimensions actually offered to the packer, and the
    # enumerate index carries the pre-pack content order used by the
    # text-oriented id reassignment in columns mode.
    rect_with_rid: list[_RectEntry] = [
        (w, h, (r_id, label_ref, w, seq)) for seq, (w, h, r_id, label_ref) in enumerate(rectangles)
    ]

    is_constrained = provided_plates is not None and len(provided_plates) > 0

    packed_plates, leftover = _pack_entries_with_pinners(
        rect_with_rid,
        provided_plates,
        allow_rotation=allow_rotation,
        layout=layout,
        default_plate_size=default_plate_size,
        default_plate_clearance=default_plate_clearance,
        rotation_preference=rotation_preference,
    )

    # Verify all labels were packed.
    if leftover:
        if is_constrained:
            raise LayoutFitError(
                f"Could only fit {len(rectangles) - len(leftover)} of "
                f"{len(rectangles)} labels on the provided plates. Please "
                "specify larger or additional plates."
            )
        else:
            # This should only trigger if a single label is larger than the
            # default plate in both orientations (rotation-aware packing).
            max_width, max_height = default_plate_size or (
                DEFAULT_PLATE_WIDTH,
                DEFAULT_PLATE_HEIGHT,
            )
            raise LayoutFitError(
                "A label's dimensions exceed the maximum plate size of "
                f"{max_width}x{max_height} in either "
                "orientation."
            )

    return packed_plates


def generate_layout_with_bounds(
    resolved_labels: list[ResolvedLabel],
    provided_plates: Optional[list[PlateSpec]] = None,
    pen_map: Optional[dict[tuple[float, str], int]] = None,
    allow_rotation: bool = True,
    layout: LayoutMode = DEFAULT_LAYOUT_MODE,
    default_plate_size: Optional[tuple[float, float]] = None,
    default_plate_clearance: Optional[tuple[float, float]] = None,
    rotation_preference: RotationPreference = RotationPreference.FILL_AXIS,
    plate_fill: PlateFill = PlateFill.PLATE_MAJOR,
) -> tuple[list[PackedPlate], dict[str, RenderedLabel]]:
    """Pack resolved labels onto plates using rendered dimensions.

    This is an enhanced version of generate_layout() that renders each
    label independently to determine its actual dimensions, then uses
    those rendered dimensions for bin-packing instead of nominal dimensions.

    This approach allows the packer to account for text rendering variations
    and ensures accurate label placement based on actual rendered content.

    Args:
        resolved_labels: Flat list of fully resolved labels from the
            resolution engine.
        provided_plates: Optional list of user-specified plates. If None
            or empty, the engine auto-allocates default 24x16 sheets.
        pen_map: Optional mapping of ``(text cutter diameter, text color)``
            to HPGL pen number (see
            :func:`plt_optimizer.generate.resolution.build_cutter_pen_map`)
            used when rendering labels for per-cutter PLT splitting.
            ``None`` keeps the historical single text pen.
        allow_rotation: When True (the default), the packer may rotate
            label instances 90 degrees when it improves the fit. Rotated
            labels are flagged ``PackedLabel.rotated`` so plate assembly
            can rotate their content clockwise accordingly.
        layout: Preferential fill order. ``columns`` (the default) fills
            each plate's height before extending rightward; ``rows`` fills
            width before extending downward (the historical behavior).
            ``columns-simple`` / ``rows-simple`` bypass ``rectpack`` with a
            flush content-order grid fill (never rotated; see
            :mod:`plt_optimizer.generate.packing.simple_grid`), the only
            modes that honour a label's ``order`` placement rank. A
            plate may override the job value via ``PlateSpec.layout``;
            mixed modes pack in sequential declaration-ordered passes.
        default_plate_size: ``(width, height)`` override (inches) for the
            auto-allocated unbounded bins (from ``job-config.json``); the
            module defaults apply when ``None``.
        default_plate_clearance: ``(left, top)`` edge clearance (inches)
            applied to every auto-allocated unbounded bin (from
            ``job-config.json``); ignored when plates are provided.
        rotation_preference: Ranking preference between packings that fit --
            the ``layout`` fill order (default, historical) or the fewest
            rotated labels (see :func:`_fill_rank_key`). Round (ellipse)
            labels are unaffected: they place through the ellipse packer,
            which never rotates.

    Returns:
        A tuple of:
        - List of ``PackedPlate`` objects containing all successfully
          packed labels.
        - Dictionary mapping label IDs to their RenderedLabel objects
          (cached for use in Phase 3 assembly).

    Raises:
        LayoutFitError: If constrained plates cannot fit all labels, or
            if a single rendered label exceeds the default 24x16 plate size
            in unbounded mode.
        LabelRenderError: If any rendered label still overlaps a drill
            hole after collision avoidance (all labels are rendered and
            their ERROR diagnostics logged before aborting). Labels whose
            collisions avoidance repaired proceed with a WARNING.
    """
    # Phase 2a: Render all unique labels and cache by ID
    rendered_labels = _render_labels_cache(resolved_labels, pen_map=pen_map)

    # Job-level gate: unavoidable text-hole collisions are unacceptable.
    # Repaired collisions only log WARNING at render time and proceed.
    # Every label has now been rendered (all per-label ERROR diagnostics
    # printed), so abort before wasting time on packing.
    assert_no_collisions(rendered_labels.values())

    # Phase 2b: Unroll labels using rendered dimensions.
    rectangles = unroll_labels_with_rendered_bounds(resolved_labels, rendered_labels)
    # The packing width (rendered, not nominal) travels in the rid payload so
    # rotation detection compares against the dimensions actually packed, and
    # the enumerate index carries the pre-pack content order used by the
    # text-oriented id reassignment in columns mode.
    rect_with_rid: list[_RectEntry] = [
        (w, h, (r_id, label_ref, w, seq))
        for seq, (w, h, r_id, label_ref, _) in enumerate(rectangles)
    ]

    is_constrained = provided_plates is not None and len(provided_plates) > 0

    packed_plates, leftover = _pack_entries_with_pinners(
        rect_with_rid,
        provided_plates,
        allow_rotation=allow_rotation,
        layout=layout,
        default_plate_size=default_plate_size,
        default_plate_clearance=default_plate_clearance,
        rotation_preference=rotation_preference,
    )

    # Verify all labels were packed.
    if leftover:
        if is_constrained:
            raise LayoutFitError(
                f"Could only fit {len(rectangles) - len(leftover)} of "
                f"{len(rectangles)} labels on the provided plates. Please "
                "specify larger or additional plates."
            )
        else:
            # This should only trigger if a single rendered label is larger
            # than the default plate in both orientations (rotation-aware).
            max_width, max_height = default_plate_size or (
                DEFAULT_PLATE_WIDTH,
                DEFAULT_PLATE_HEIGHT,
            )

            # Build a detailed error message showing which dimensions failed for each label
            oversized_details = []
            for w, h, (r_id, _, _, _) in leftover:
                if w > max_width and h > max_height:
                    detail = (
                        f"Label '{r_id}': {w:.1f}\"W × {h:.1f}\"H exceeds both width and height "
                        f'(max {max_width:.1f}"W × {max_height:.1f}"H)'
                    )
                elif w > max_width:
                    detail = (
                        f"Label '{r_id}': {w:.1f}\"W × {h:.1f}\"H exceeds max width "
                        f'({max_width:.1f}"W × {max_height:.1f}"H)'
                    )
                else:  # h > max_height
                    detail = (
                        f"Label '{r_id}': {w:.1f}\"W × {h:.1f}\"H exceeds max height "
                        f'({max_width:.1f}"W × {max_height:.1f}"H)'
                    )
                oversized_details.append(detail)

            error_msg = (
                "Rendered label(s) exceed the maximum plate size in both orientations:\n"
                + "\n".join(oversized_details)
            )
            raise LayoutFitError(error_msg)

    # Return both the plates and the rendered labels cache for Phase 3
    return packed_plates, rendered_labels
