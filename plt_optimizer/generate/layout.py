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
    >>> from plt_optimizer.generate.layout import generate_layout
    >>> job = parse_yaml("tests_deps/sample_spec.yaml")
    >>> labels = resolve_job_spec(job)
    >>> plates = generate_layout(labels, job.plates)
    >>> print(plates[0].labels[0].x, plates[0].labels[0].y)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Optional

import rectpack

from plt_optimizer.generate.label_renderer import (
    RenderedLabel,
    assert_no_collisions,
    render_label_to_plt,
)
from plt_optimizer.generate.resolution import ResolvedLabel
from plt_optimizer.generate.schema import (
    DEFAULT_LAYOUT_MODE,
    LayoutMode,
    PlateSpec,
)

# ---------------------------------------------------------------------------
# Default plate dimensions for unbounded mode
# ---------------------------------------------------------------------------
DEFAULT_PLATE_WIDTH: float = 24.0
DEFAULT_PLATE_HEIGHT: float = 16.0


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
        labels: List of labels placed on this plate.
    """

    plate_id: str
    width: float
    height: float
    left_clearance: float = 0.0
    top_clearance: float = 0.0
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
PACK_CONFIGS: tuple[tuple[object, object], ...] = (
    (rectpack.MaxRectsBssf, rectpack.SORT_AREA),
    (rectpack.GuillotineBlsfLas, rectpack.SORT_NONE),
    (rectpack.GuillotineBlsfLas, rectpack.SORT_AREA),
    (rectpack.GuillotineBafLas, rectpack.SORT_NONE),
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
PACK_CONFIGS_COLUMNS: tuple[tuple[object, object], ...] = (
    (rectpack.SkylineBl, rectpack.SORT_NONE),
    (rectpack.SkylineBl, rectpack.SORT_AREA),
)


def _render_labels_cache(
    resolved_labels: list[ResolvedLabel],
    pen_map: Optional[dict[float, int]] = None,
) -> dict[str, RenderedLabel]:
    """Render all unique labels and cache by ID.

    For labels with count > 1, renders only once and caches the result
    to avoid redundant rendering.

    Args:
        resolved_labels: Flat list of fully resolved labels.
        pen_map: Optional mapping of text cutter diameter to HPGL pen
            number (see
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
        # returned an adjusted clone (reduced hole_margin and/or
        # collision_compress). Propagating it through the packer keeps
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
    packer: rectpack.packer.Packer,
    *,
    transpose: bool = False,
    clearances: Optional[Mapping[str, tuple[float, float]]] = None,
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

    Returns:
        A list of ``PackedPlate`` objects with all labels positioned.
        Empty bins (from auto-allocation) are discarded.
    """
    clearance_map: Mapping[str, tuple[float, float]] = clearances or {}
    final_plates: list[PackedPlate] = []

    for bin_obj in packer:
        if len(bin_obj) == 0:
            # Ignore empty auto-allocated bins
            continue

        left_clearance, top_clearance = clearance_map.get(bin_obj.bid, (0.0, 0.0))
        if transpose:
            # The packer's bin was offered as (height, width); report the
            # real plate dimensions.
            plate = PackedPlate(
                plate_id=bin_obj.bid,
                width=bin_obj.height,
                height=bin_obj.width,
                left_clearance=left_clearance,
                top_clearance=top_clearance,
            )
        else:
            plate = PackedPlate(
                plate_id=bin_obj.bid,
                width=bin_obj.width,
                height=bin_obj.height,
                left_clearance=left_clearance,
                top_clearance=top_clearance,
            )

        for rect in bin_obj:
            # Unpack the custom ID tuple we passed in. The third element is
            # the *packing* width the rectangle was added with (rendered
            # dimensions in the bounds path, nominal otherwise) — the only
            # reliable baseline for rotation detection, since the nominal
            # ResolvedLabel width may differ from what was actually packed.
            rect_id, source_label, pack_width = rect.rid

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
            # stays bit-identical to the historical behaviour.
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

        final_plates.append(plate)

    return final_plates


# The ``rid`` payload carried with every rectangle: the instance id, the
# source label, and the *packing* width the rectangle was offered to the
# packer with (the baseline rotation detection compares against).
_RidPayload = tuple[str, ResolvedLabel, float]

# A rectangle entry as passed to the packer: (width, height, rid-payload).
# The ``rid`` payload is an opaque tuple that rectpack preserves verbatim and
# which :func:`_extract_packed_plates` later unpacks.
_RectEntry = tuple[float, float, _RidPayload]


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
        (height, width, (rect_id, source_label, height))
        for width, height, (rect_id, source_label, _pack_width) in entries
    ]


def _count_rotated(packer: rectpack.packer.Packer) -> int:
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
) -> rectpack.packer.Packer:
    """Run several packing heuristics and return the packer with the best fit.

    Each candidate algorithm is run over the same set of rectangles and bins.
    The result that packs every rectangle onto the smallest total plate
    footprint (sum over non-empty plates of ``width * height``) is selected,
    which guarantees tighter layouts without ever regressing on packing quality
    for any given input. Candidates that cannot fit every rectangle are ranked
    by ``(packed_count desc, footprint asc)`` and only used when no candidate
    fits everything.

    When ``allow_rotation`` is set, every candidate is evaluated twice — once
    with rotation disabled and once with it enabled — and candidates are
    ranked by ``(footprint, rotated_count)``. Labels therefore only ever
    rotate when rotation *strictly* improves the footprint; at equal
    footprints the all-horizontal layout always wins, keeping output stable
    for jobs that pack just as well without rotation.

    ``layout`` selects the fill-order frame:

    - :attr:`LayoutMode.ROWS` (historical behaviour): pack in the real frame
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

    Returns:
        The best-performing configured ``rectpack.Packer`` that successfully
        packs all rectangles. If no candidate fits every rectangle, returns
        the best partial packer (its result is surfaced to callers for error
        reporting).
    """
    transpose = layout == LayoutMode.COLUMNS
    configs = PACK_CONFIGS_COLUMNS if transpose else PACK_CONFIGS

    # Rotation variants to evaluate per packing configuration. With rotation
    # disabled the behaviour is exactly the historical single-variant sweep.
    orientations: tuple[bool, ...] = (False, True) if allow_rotation else (False,)

    packer_rects = _transpose_entries(rectangles_with_rid, transpose)
    packer_bins = [(h, w, bid) for w, h, bid in bin_specs] if transpose else bin_specs

    best_packer: Optional[rectpack.packer.Packer] = None
    best_packed: int = -1
    best_footprint: float = 0.0
    best_rotations: int = 0

    for pack_algo, sort_algo in configs:
        for rotation in orientations:
            packer = rectpack.newPacker(
                mode=rectpack.PackingMode.Offline,
                bin_algo=rectpack.PackingBin.BFF,
                pack_algo=pack_algo,
                sort_algo=sort_algo,
                rotation=rotation,
            )

            for w, h, rid in packer_rects:
                packer.add_rect(w, h, rid=rid)

            for width, height, bid in packer_bins:
                packer.add_bin(width, height, bid=bid)

            packer.pack()

            total_packed = sum(len(b) for b in packer)
            footprint = _plate_footprint(packer)
            rotations = _count_rotated(packer)

            # Rank: full fits first; among equals, smallest footprint, then
            # fewest rotations. Partials rank by packed count only (ties keep
            # the incumbent); they only matter for error reporting.
            full_fit = total_packed == len(rectangles_with_rid)
            best_full = best_packer is not None and best_packed == len(rectangles_with_rid)
            if best_packer is None:
                take = True
            elif full_fit != best_full:
                take = full_fit
            elif full_fit:
                if math.isclose(footprint, best_footprint, rel_tol=1e-9, abs_tol=1e-9):
                    # Footprint tie (within float noise): prefer fewer rotations.
                    take = rotations < best_rotations
                else:
                    take = footprint < best_footprint
            else:
                take = total_packed > best_packed

            if take:
                best_packed = total_packed
                best_footprint = footprint
                best_rotations = rotations
                best_packer = packer

    assert best_packer is not None  # Config lists are never empty
    return best_packer


def _pack_group(
    rectangles_with_rid: list[_RectEntry],
    bin_specs: list[tuple[float, float, str]],
    allow_rotation: bool,
    layout: LayoutMode,
    clearances: Optional[Mapping[str, tuple[float, float]]] = None,
) -> list[PackedPlate]:
    """Pack rectangles onto one group of same-mode plates and extract plates.

    Runs :func:`_pack_best` with the group's fill-order mode and maps the
    packer result back into real plate space (transposing back for column
    major groups).

    Args:
        rectangles_with_rid: Remaining ``(pack_width, pack_height, rid)``
            tuples to pack.
        bin_specs: ``(width, height, bid)`` plates sharing one fill mode.
        allow_rotation: Whether rotated candidates are considered.
        layout: The group's fill-order mode.
        clearances: Optional bin-id to ``(left_clearance, top_clearance)``
            mapping applied to placements (see
            :func:`_extract_packed_plates`).

    Returns:
        The non-empty ``PackedPlate`` objects produced by this packing pass.
    """
    packer = _pack_best(
        rectangles_with_rid,
        bin_specs,
        allow_rotation=allow_rotation,
        layout=layout,
    )
    return _extract_packed_plates(
        packer,
        transpose=layout == LayoutMode.COLUMNS,
        clearances=clearances,
    )


def _resolve_plate_groups(
    provided_plates: Optional[list[PlateSpec]],
    job_layout: LayoutMode,
    n_rectangles: int,
    default_plate_size: Optional[tuple[float, float]] = None,
) -> list[tuple[list[tuple[float, float, str]], LayoutMode]]:
    """Group plates into sequential same-mode packing passes.

    Consecutive plates resolving to the same fill mode share one packing
    pass, so single-mode jobs — including every unbounded job — pack in
    exactly one pass, identically to the historical behaviour. When plates
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

    Returns:
        Declaration-ordered ``(bin_specs, layout)`` groups.
    """
    if not provided_plates:
        default_width, default_height = default_plate_size or (
            DEFAULT_PLATE_WIDTH,
            DEFAULT_PLATE_HEIGHT,
        )
        default_bins = [
            (default_width, default_height, f"default_plate_{i + 1}") for i in range(n_rectangles)
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


def _plate_footprint(packer: rectpack.packer.Packer) -> float:
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


def _pack_groups(
    rectangles_with_rid: list[_RectEntry],
    groups: list[tuple[list[tuple[float, float, str]], LayoutMode]],
    allow_rotation: bool,
    clearances: Optional[Mapping[str, tuple[float, float]]] = None,
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
        plates = _pack_group(remaining, bin_specs, allow_rotation, group_layout, clearances)
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
            width before extending downward (the historical behaviour).
            A plate may override the job value via ``PlateSpec.layout``;
            mixed modes pack in sequential declaration-ordered passes.
        default_plate_size: ``(width, height)`` override (inches) for the
            auto-allocated unbounded bins (from ``job-config.json``); the
            module defaults apply when ``None``.
        default_plate_clearance: ``(left, top)`` edge clearance (inches)
            applied to every auto-allocated unbounded bin (from
            ``job-config.json``); ignored when plates are provided.

    Returns:
        A list of ``PackedPlate`` objects containing all successfully
        packed labels.

    Raises:
        LayoutFitError: If constrained plates cannot fit all labels, or
            if a single label exceeds the default 24x16 plate size (in
            either orientation) in unbounded mode.

    Example:
        >>> plates = generate_layout(resolved_labels)
        >>> len(plates) >= 1
        True
    """
    rectangles = unroll_labels(resolved_labels)

    # Build rectangle entries with their (rid) payloads. The packing width
    # travels with the payload so _extract_packed_plates can detect rotation
    # against the dimensions actually offered to the packer.
    rect_with_rid: list[_RectEntry] = [
        (w, h, (r_id, label_ref, w)) for w, h, r_id, label_ref in rectangles
    ]

    is_constrained = provided_plates is not None and len(provided_plates) > 0

    groups = _resolve_plate_groups(
        provided_plates, layout, len(rectangles), default_plate_size=default_plate_size
    )
    clearances = _plate_clearances(provided_plates)
    if not is_constrained:
        clearances = _default_clearance_map(groups, default_plate_clearance)
    packed_plates, leftover = _pack_groups(
        rect_with_rid, groups, allow_rotation=allow_rotation, clearances=clearances
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
    pen_map: Optional[dict[float, int]] = None,
    allow_rotation: bool = True,
    layout: LayoutMode = DEFAULT_LAYOUT_MODE,
    default_plate_size: Optional[tuple[float, float]] = None,
    default_plate_clearance: Optional[tuple[float, float]] = None,
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
        pen_map: Optional mapping of text cutter diameter to HPGL pen
            number (see
            :func:`plt_optimizer.generate.resolution.build_cutter_pen_map`)
            used when rendering labels for per-cutter PLT splitting.
            ``None`` keeps the historical single text pen.
        allow_rotation: When True (the default), the packer may rotate
            label instances 90 degrees when it improves the fit. Rotated
            labels are flagged ``PackedLabel.rotated`` so plate assembly
            can rotate their content clockwise accordingly.
        layout: Preferential fill order. ``columns`` (the default) fills
            each plate's height before extending rightward; ``rows`` fills
            width before extending downward (the historical behaviour). A
            plate may override the job value via ``PlateSpec.layout``;
            mixed modes pack in sequential declaration-ordered passes.
        default_plate_size: ``(width, height)`` override (inches) for the
            auto-allocated unbounded bins (from ``job-config.json``); the
            module defaults apply when ``None``.
        default_plate_clearance: ``(left, top)`` edge clearance (inches)
            applied to every auto-allocated unbounded bin (from
            ``job-config.json``); ignored when plates are provided.

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
    # rotation detection compares against the dimensions actually packed.
    rect_with_rid: list[_RectEntry] = [
        (w, h, (r_id, label_ref, w)) for w, h, r_id, label_ref, _ in rectangles
    ]

    is_constrained = provided_plates is not None and len(provided_plates) > 0

    groups = _resolve_plate_groups(
        provided_plates, layout, len(rectangles), default_plate_size=default_plate_size
    )
    clearances = _plate_clearances(provided_plates)
    if not is_constrained:
        clearances = _default_clearance_map(groups, default_plate_clearance)
    packed_plates, leftover = _pack_groups(
        rect_with_rid, groups, allow_rotation=allow_rotation, clearances=clearances
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
            raise LayoutFitError(
                "A rendered label's dimensions exceed the maximum plate size of "
                f"{max_width}x{max_height} in either "
                "orientation."
            )

    # Return both the plates and the rendered labels cache for Phase 3
    return packed_plates, rendered_labels
