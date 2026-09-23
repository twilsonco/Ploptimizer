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
from dataclasses import dataclass, field, replace
from typing import Optional, Sequence

import rectpack

from plt_optimizer.generate.label_renderer import (
    RenderedLabel,
    assert_no_collisions,
    render_label_to_plt,
)
from plt_optimizer.generate.resolution import ResolvedLabel
from plt_optimizer.generate.schema import DEFAULT_LAYOUT_MODE, LayoutMode, PlateSpec

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

    Attributes:
        plate_id: Unique identifier for this plate.
        width: Plate width in inches.
        height: Plate height in inches.
        labels: List of labels placed on this plate.
    """

    plate_id: str
    width: float
    height: float
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
            # Pack at nominal dimensions (no margin padding)
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
) -> list[PackedPlate]:
    """Translate ``rectpack`` results into typed ``PackedPlate`` objects.

    Args:
        packer: A ``rectpack.Packer`` that has already executed ``pack()``.
        transpose: When True, the packer ran in the transposed column-major
            frame (see :func:`_is_transposed`) and every placement is mapped
            back into real plate space via ``(x, y, w, h) -> (y, x, h, w)``.

    Returns:
        A list of ``PackedPlate`` objects with all labels positioned.
        Empty bins (from auto-allocation) are discarded.
    """
    final_plates: list[PackedPlate] = []

    for bin_obj in packer:
        if len(bin_obj) == 0:
            # Ignore empty auto-allocated bins
            continue

        if transpose:
            # The packer's bin was offered as (height, width); report the
            # real plate dimensions.
            plate = PackedPlate(
                plate_id=bin_obj.bid,
                width=bin_obj.height,
                height=bin_obj.width,
            )
        else:
            plate = PackedPlate(
                plate_id=bin_obj.bid,
                width=bin_obj.width,
                height=bin_obj.height,
            )

        for rect in bin_obj:
            # Unpack the custom ID tuple we passed in. The third element is
            # the *packing* width the rectangle was added with (rendered
            # dimensions in the bounds path, nominal otherwise; in the
            # transposed frame this is the packer-space width) -- the only
            # reliable baseline for rotation detection, since the nominal
            # ResolvedLabel width may differ from what was actually packed.
            rect_id, source_label, pack_width = rect.rid

            # Detect rotation: rect.width/height reflect post-rotation dims
            # in the *packer* frame. In the transposed frame a packer-space
            # rotation is exactly a real-space rotation (a 90-degree swap
            # composed with the transpose is again a 90-degree swap), so
            # comparing against the packer-space width stays correct.
            was_rotated = not math.isclose(rect.width, pack_width, rel_tol=1e-9)

            if transpose:
                # Map the placement back out of the transposed frame.
                packed_label = PackedLabel(
                    label_id=rect_id,
                    x=rect.y,
                    y=rect.x,
                    width=rect.height,
                    height=rect.width,
                    rotated=was_rotated,
                    source_label=source_label,
                )
            else:
                packed_label = PackedLabel(
                    label_id=rect_id,
                    x=rect.x,
                    y=rect.y,
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
# The ``rid`` payload is preserved verbatim by rectpack and recovered by
# :func:`_extract_packed_plates`.
_RectEntry = tuple[float, float, _RidPayload]


def _is_transposed(layout: LayoutMode) -> bool:
    """Whether a fill-order mode requires transposed (column-major) packing.

    ``rectpack`` offers no sort option able to enforce a fill order (for
    identical rectangles every sort algorithm produces identical order, and
    the emergent fill order is purely a property of the placement
    heuristics' tie-breaking), so column-major packing is realized by
    transposing the problem: rectangles ``(w, h)`` and bins ``(W, H)`` are
    swapped, the packer then fills the transposed width -- the real
    height -- first, and every placement is mapped back verbatim.

    Args:
        layout: The resolved plate fill-order mode.

    Returns:
        True when packing must run in the transposed frame.
    """
    # Equality (not identity): LayoutMode is a str-Enum, so raw YAML/CLI
    # strings compare correctly against the members.
    return layout != LayoutMode.ROWS


def _transpose_entries(entries: list[_RectEntry], transpose: bool) -> list[_RectEntry]:
    """Swap rectangle dimensions into the transposed packing frame.

    The ``rid`` payload is rebuilt carrying the *packer-space* width, so
    rotation detection in :func:`_extract_packed_plates` keeps comparing
    against the dimensions actually offered to the packer.

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


@dataclass(frozen=True)
class _PlateGroup:
    """One packing pass: bins sharing a fill-order mode, in declaration order.

    Attributes:
        bins: ``(bid, width, height)`` plate definitions in real space.
        layout: The fill-order mode shared by every plate in the group.
    """

    bins: tuple[tuple[str, float, float], ...]
    layout: LayoutMode


def _resolve_plate_layouts(
    provided_plates: Optional[list[PlateSpec]],
    job_layout: LayoutMode,
    max_default_plates: int,
) -> list[_PlateGroup]:
    """Group plates into sequential same-mode packing passes.

    Consecutive plates resolving to the same mode share one packing pass,
    so single-mode jobs -- including every unbounded job -- pack in exactly
    one pass, identically to the historical behaviour. When plates mix
    modes, each maximal run of same-mode plates becomes its own group and
    the groups pack sequentially in declaration order: a group receives
    only the label instances left over from the previous groups.

    Args:
        provided_plates: User-specified plates (constrained mode), or
            ``None`` / empty for unbounded auto-allocation of default
            24x16 plates.
        job_layout: The job-level fill-order mode, used as the fallback for
            plates that do not set ``layout`` and for unbounded mode.
        max_default_plates: Number of default plates to offer in unbounded
            mode (theoretical maximum: one per label instance).

    Returns:
        Declaration-ordered groups of same-mode bins.
    """
    if not provided_plates:
        return [
            _PlateGroup(
                bins=tuple(
                    (f"default_plate_{i + 1}", DEFAULT_PLATE_WIDTH, DEFAULT_PLATE_HEIGHT)
                    for i in range(max_default_plates)
                ),
                layout=job_layout,
            )
        ]

    groups: list[_PlateGroup] = []
    for plate in provided_plates:
        mode = plate.layout if plate.layout is not None else job_layout
        entry = (plate.id, plate.width, plate.height)
        if groups and groups[-1].layout == mode:
            groups[-1] = _PlateGroup(bins=groups[-1].bins + (entry,), layout=mode)
        else:
            groups.append(_PlateGroup(bins=(entry,), layout=mode))
    return groups


def _scan_key(label: PackedLabel) -> tuple[float, float]:
    """Sort key placing labels in column-major (reading) order.

    Labels read top-to-bottom then advance rightward: primary key ``x``,
    secondary ``y``.

    Args:
        label: The packed label to key.

    Returns:
        An ``(x, y)`` coordinate key.
    """
    return (label.x, label.y)


def _reorder_labels_by_scan(
    plates: list[PackedPlate],
    declaration_order: dict[str, int],
) -> None:
    """Reassign label instances so their sequence reads column-major.

    ``rectpack``'s placement order is emergent and shape-dependent (no
    algorithm, sort or coordinate frame reliably yields a chosen fill
    order -- in particular a uniform grid of identical labels tiles the
    same way in the transposed frame), so the packer's *slot set* is kept
    exactly as packed and only the label-to-slot assignment is permuted.
    Slots are grouped by footprint signature ``(width, height, rotated)``
    and within each group the instances -- in declaration order (see
    :paramref:`declaration_order`) -- are handed to the slots in scan
    order. Every label therefore lands on a slot of its exact own
    dimensions, leaving geometry, footprint, plate count and collision
    results untouched while making the emitted sequence read top-to-bottom
    before advancing rightward.

    ``plate.labels`` is additionally re-sorted into scan order, because its
    order drives assembly emission order and the plate-space optimizer's
    baseline tour.

    Only called for ``COLUMNS``; ``ROWS`` keeps ``rectpack``'s placement
    order verbatim so the historical layout stays bit-identical.

    Args:
        plates: Packed plates to reorder in place.
        declaration_order: Maps each instance id to its position in the
            input rectangle list (label declaration order, then instance
            index), used to hand instances to slots in that order.
    """
    for plate in plates:
        groups: dict[tuple[float, float, bool], list[PackedLabel]] = {}
        for label in plate.labels:
            signature = (
                round(label.width, 9),
                round(label.height, 9),
                label.rotated,
            )
            groups.setdefault(signature, []).append(label)

        reordered: list[PackedLabel] = []
        for members in groups.values():
            slots = sorted(members, key=_scan_key)
            instances = sorted(members, key=lambda label: declaration_order[label.label_id])
            # Slot geometry stays; identity moves to the scan-order slot.
            reordered.extend(
                replace(slot, label_id=inst.label_id, source_label=inst.source_label)
                for slot, inst in zip(slots, instances)
            )

        reordered.sort(key=_scan_key)
        plate.labels = reordered


def _pack_labels(
    rectangles_with_rid: list[_RectEntry],
    groups: Sequence[_PlateGroup],
    *,
    allow_rotation: bool,
    is_constrained: bool,
) -> list[PackedPlate]:
    """Pack labels through sequential same-mode plate groups.

    Each group runs :func:`_pack_best` against its own plates in its own
    fill-order frame, consuming only the instances left over from previous
    groups; groups whose plates are already full receive nothing. Because
    groups are maximal runs of *consecutive* same-mode plates, the returned
    plates stay in the caller's plate declaration order, which keeps the
    ``<2-digit plate>`` numbering in exported file names intuitive.

    Args:
        rectangles_with_rid: Real-space ``(w, h, rid)`` tuples (the ``rid``
            payload carries the real packing width; each group transposes
            it into packer space as needed).
        groups: Packing passes in declaration order (see
            :func:`_resolve_plate_layouts`).
        allow_rotation: Forwarded to :func:`_pack_best`.
        is_constrained: Whether user-provided plates are in use (selects
            the :class:`LayoutFitError` wording).

    Returns:
        Packed plates in declaration order; empty auto-allocated bins are
        discarded.

    Raises:
        LayoutFitError: If any label instance could not be placed.
    """
    remaining = list(rectangles_with_rid)
    plates: list[PackedPlate] = []
    # Instance id -> position in the input list (label declaration order,
    # then instance index). Drives column-major scan reassignment so the
    # emitted sequence follows declaration order regardless of id format.
    declaration_order = {entry[2][0]: index for index, entry in enumerate(rectangles_with_rid)}

    for group in groups:
        if not remaining:
            break
        packer = _pack_best(
            remaining,
            [(width, height, bid) for bid, width, height in group.bins],
            allow_rotation=allow_rotation,
            layout=group.layout,
        )
        packed = _extract_packed_plates(packer, transpose=_is_transposed(group.layout))
        if _is_transposed(group.layout):
            _reorder_labels_by_scan(packed, declaration_order)
        placed = {label.label_id for plate in packed for label in plate.labels}
        remaining = [entry for entry in remaining if entry[2][0] not in placed]
        plates.extend(plate for plate in packed if plate.labels)

    if remaining:
        unfitted = len(rectangles_with_rid) - len(remaining)
        if is_constrained:
            raise LayoutFitError(
                f"Could only fit {unfitted} of {len(rectangles_with_rid)} labels "
                "on the provided plates. Please specify larger or "
                "additional plates."
            )
        else:
            # This should only trigger if a single label is larger than
            # 24x16 in both orientations (rotation-aware packing).
            raise LayoutFitError(
                "A label's dimensions exceed the maximum plate size of "
                f"{DEFAULT_PLATE_WIDTH}x{DEFAULT_PLATE_HEIGHT} in either "
                "orientation."
            )

    return plates


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
    layout: LayoutMode = DEFAULT_LAYOUT_MODE,
) -> rectpack.packer.Packer:
    """Run several packing heuristics and return the packer with the best fit.

    Each candidate algorithm is run over the same set of rectangles and bins.
    The result that packs every rectangle onto the smallest total plate
    footprint (sum over non-empty plates of ``width * height``) is selected,
    which guarantees tighter layouts without ever regressing on packing quality
    for any given input.

    When ``allow_rotation`` is set, every candidate is evaluated twice — once
    with rotation disabled and once with it enabled — and candidates are
    ranked by ``(footprint, rotated_count)``. Labels therefore only ever
    rotate when rotation *strictly* improves the footprint; at equal
    footprints the all-horizontal layout always wins, keeping output stable
    for jobs that pack just as well without rotation.

    Args:
        rectangles_with_rid: List of ``(pack_width, pack_height, rid)`` tuples.
            The ``rid`` payload (e.g. a label reference) is preserved verbatim
            by rectpack and recovered in :func:`_extract_packed_plates`.
        bin_specs: List of ``(width, height, bid)`` plate definitions.
        allow_rotation: When True (the default), rotated packing candidates
            are considered. Rotated labels have their whole content rotated
            clockwise during plate assembly (see
            ``vectorize.assemble_plt_from_rendered_labels``).
        layout: Plate fill-order mode. ``COLUMNS`` (the default) runs the
            whole candidate sweep in the transposed frame -- rectangles and
            bins swapped -- so the packer's native width-first fill becomes
            a height-first fill in real space. The used-area footprint
            metric is transpose-invariant (``max_x * max_y``), so candidate
            ranking is unaffected. ``ROWS`` packs directly.

    Returns:
        The best-performing configured ``rectpack.Packer`` that successfully
        packs all rectangles. If no candidate fits every rectangle, returns
        the first packer (its partial result is surfaced to callers for error
        reporting).
    """
    transpose = _is_transposed(layout)
    packer_rects = _transpose_entries(rectangles_with_rid, transpose)
    if transpose:
        bin_specs = [(height, width, bid) for width, height, bid in bin_specs]

    # Rotation variants to evaluate per packing configuration. With rotation
    # disabled the behaviour is exactly the historical single-variant sweep.
    orientations: tuple[bool, ...] = (False, True) if allow_rotation else (False,)

    best_packer: Optional[rectpack.packer.Packer] = None
    best_footprint: Optional[float] = None
    best_rotations: int = 0

    for pack_algo, sort_algo in PACK_CONFIGS:
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

            for width, height, bid in bin_specs:
                packer.add_bin(width, height, bid=bid)

            packer.pack()

            total_packed = sum(len(b) for b in packer)
            if total_packed < len(packer_rects):
                # This candidate could not fit everything; keep it as a
                # fallback only if we have nothing better yet.
                if best_packer is None:
                    best_packer = packer
                continue

            footprint = _plate_footprint(packer)
            rotations = _count_rotated(packer)
            if best_footprint is None:
                take = True
            elif math.isclose(footprint, best_footprint, rel_tol=1e-9, abs_tol=1e-9):
                # Footprint tie (within float noise): prefer fewer rotations.
                take = rotations < best_rotations
            else:
                take = footprint < best_footprint

            if take:
                best_footprint = footprint
                best_rotations = rotations
                best_packer = packer

    assert best_packer is not None  # PACK_CONFIGS is never empty
    return best_packer


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


def generate_layout(
    resolved_labels: list[ResolvedLabel],
    provided_plates: Optional[list[PlateSpec]] = None,
    allow_rotation: bool = True,
    layout: LayoutMode = DEFAULT_LAYOUT_MODE,
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
        layout: Default plate fill-order mode (``COLUMNS`` fills each
            plate's height before advancing to the next column; ``ROWS``
            fills the width first). Individual plates may override it via
            ``PlateSpec.layout``; mixed-mode plates pack in sequential
            declaration-ordered groups (see :func:`_resolve_plate_layouts`).

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
    groups = _resolve_plate_layouts(provided_plates, layout, len(rectangles))

    return _pack_labels(
        rect_with_rid,
        groups,
        allow_rotation=allow_rotation,
        is_constrained=is_constrained,
    )


def generate_layout_with_bounds(
    resolved_labels: list[ResolvedLabel],
    provided_plates: Optional[list[PlateSpec]] = None,
    pen_map: Optional[dict[float, int]] = None,
    allow_rotation: bool = True,
    layout: LayoutMode = DEFAULT_LAYOUT_MODE,
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
        layout: Default plate fill-order mode (``COLUMNS`` fills each
            plate's height before advancing to the next column; ``ROWS``
            fills the width first). Individual plates may override it via
            ``PlateSpec.layout``; mixed-mode plates pack in sequential
            declaration-ordered groups (see :func:`_resolve_plate_layouts`).

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
    groups = _resolve_plate_layouts(provided_plates, layout, len(rectangles))

    plates = _pack_labels(
        rect_with_rid,
        groups,
        allow_rotation=allow_rotation,
        is_constrained=is_constrained,
    )

    # Return both the plates and the rendered labels cache for Phase 3
    return plates, rendered_labels
