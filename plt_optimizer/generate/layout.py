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
    >>> job = parse_yaml("examples/sample_spec.yaml")
    >>> labels = resolve_job_spec(job)
    >>> plates = generate_layout(labels, job.plates)
    >>> print(plates[0].labels[0].x, plates[0].labels[0].y)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import rectpack

from plt_optimizer.generate.label_renderer import RenderedLabel, render_label_to_plt
from plt_optimizer.generate.resolution import ResolvedLabel
from plt_optimizer.generate.schema import PlateSpec

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
        rotated: True if the label was rotated 90 degrees by the packer.
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
def initialize_packer() -> rectpack.packer.Packer:
    """Create a configured ``rectpack`` instance.

    Uses the default candidate pack configuration (see :data:`PACK_CONFIGS`).
    This is retained as a convenience for callers that only need a single,
    pre-configured packer and do not require best-fit selection across
    multiple heuristics.

    Returns:
        A configured ``rectpack.Packer`` ready to accept rectangles and bins.
    """
    algo, sort_algo = PACK_CONFIGS[0]
    return rectpack.newPacker(
        mode=rectpack.PackingMode.Offline,
        bin_algo=rectpack.PackingBin.BFF,
        pack_algo=algo,
        sort_algo=sort_algo,
        rotation=False,  # Disable rotation to preserve label orientation
    )


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
) -> dict[str, RenderedLabel]:
    """Render all unique labels and cache by ID.

    For labels with count > 1, renders only once and caches the result
    to avoid redundant rendering.

    Args:
        resolved_labels: Flat list of fully resolved labels.

    Returns:
        Dictionary mapping label ID to RenderedLabel.
    """
    rendered_cache: dict[str, RenderedLabel] = {}
    for label in resolved_labels:
        if label.id not in rendered_cache:
            rendered_cache[label.id] = render_label_to_plt(label)
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
        for i in range(label.count):
            # Use rendered dimensions for packing (actual width/height)
            # instead of nominal label dimensions
            pack_width = rendered.width
            pack_height = rendered.height

            # Unique ID to track instances of the same logical label
            rect_id = f"{label.id}_{i}"
            rectangles.append((pack_width, pack_height, rect_id, label, rendered))

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
) -> list[PackedPlate]:
    """Translate ``rectpack`` results into typed ``PackedPlate`` objects.

    Args:
        packer: A ``rectpack.Packer`` that has already executed ``pack()``.

    Returns:
        A list of ``PackedPlate`` objects with all labels positioned.
        Empty bins (from auto-allocation) are discarded.
    """
    final_plates: list[PackedPlate] = []

    for bin_obj in packer:
        if len(bin_obj) == 0:
            # Ignore empty auto-allocated bins
            continue

        plate = PackedPlate(
            plate_id=bin_obj.bid,
            width=bin_obj.width,
            height=bin_obj.height,
        )

        for rect in bin_obj:
            # Unpack the custom ID tuple we passed in
            rect_id, source_label = rect.rid

            # Detect rotation: rect.width/height reflect post-rotation dims
            # Original packing dimensions (without margin)
            original_width = source_label.width
            was_rotated = rect.width != original_width

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


# A rectangle entry as passed to the packer: (width, height, rid-payload).
# The ``rid`` payload is an opaque tuple that rectpack preserves verbatim and
# which :func:`_extract_packed_plates` later unpacks.
_RectEntry = tuple[float, float, object]


def _pack_best(
    rectangles_with_rid: list[_RectEntry],
    bin_specs: list[tuple[float, float, str]],
) -> rectpack.packer.Packer:
    """Run several packing heuristics and return the packer with the best fit.

    Each candidate algorithm is run over the same set of rectangles and bins.
    The result that packs every rectangle onto the smallest total plate
    footprint (sum over non-empty plates of ``width * height``) is selected,
    which guarantees tighter layouts without ever regressing on packing quality
    for any given input.

    Args:
        rectangles_with_rid: List of ``(pack_width, pack_height, rid)`` tuples.
            The ``rid`` payload (e.g. a label reference) is preserved verbatim
            by rectpack and recovered in :func:`_extract_packed_plates`.
        bin_specs: List of ``(width, height, bid)`` plate definitions.

    Returns:
        The best-performing configured ``rectpack.Packer`` that successfully
        packs all rectangles. If no candidate fits every rectangle, returns the
        first packer (its partial result is surfaced to callers for error
        reporting).

    Raises:
        LayoutFitError: If a single label instance exceeds every plate's
            dimensions such that even one rectangle cannot be placed.
    """
    best_packer: Optional[rectpack.packer.Packer] = None
    best_footprint: float | None = None

    for pack_algo, sort_algo in PACK_CONFIGS:
        packer = rectpack.newPacker(
            mode=rectpack.PackingMode.Offline,
            bin_algo=rectpack.PackingBin.BFF,
            pack_algo=pack_algo,
            sort_algo=sort_algo,
            rotation=False,  # Disable rotation to preserve label orientation
        )

        for w, h, rid in rectangles_with_rid:
            packer.add_rect(w, h, rid=rid)

        for width, height, bid in bin_specs:
            packer.add_bin(width, height, bid=bid)

        packer.pack()

        total_packed = sum(len(b) for b in packer)
        if total_packed < len(rectangles_with_rid):
            # This candidate could not fit everything; keep it as a fallback
            # only if we have nothing better yet.
            if best_packer is None:
                best_packer = packer
            continue

        footprint = _plate_footprint(packer)
        if best_footprint is None or footprint < best_footprint:
            best_footprint = footprint
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
) -> list[PackedPlate]:
    """Pack resolved labels onto physical plates.

    Handles both constrained mode (user-specified plates) and unbounded
    mode (auto-allocating 24x16 default sheets).

    Args:
        resolved_labels: Flat list of fully resolved labels from the
            resolution engine.
        provided_plates: Optional list of user-specified plates. If None
            or empty, the engine auto-allocates default 24x16 sheets.

    Returns:
        A list of ``PackedPlate`` objects containing all successfully
        packed labels.

    Raises:
        LayoutFitError: If constrained plates cannot fit all labels, or
            if a single label exceeds the default 24x16 plate size in
            unbounded mode.

    Example:
        >>> plates = generate_layout(resolved_labels)
        >>> len(plates) >= 1
        True
    """
    rectangles = unroll_labels(resolved_labels)

    # Build rectangle entries with their (rid) payloads.
    rect_with_rid: list[_RectEntry] = [
        (w, h, (r_id, label_ref)) for w, h, r_id, label_ref in rectangles
    ]

    is_constrained = provided_plates is not None and len(provided_plates) > 0

    if is_constrained:
        # Constrained mode: use exactly what the user provided.
        assert provided_plates is not None  # narrowed by is_constrained
        bin_specs = [
            (plate.width, plate.height, plate.id)
            for plate in provided_plates
            if plate.id is not None
        ]
    else:
        # Unbounded mode: provide enough default plates to guarantee a fit.
        # Theoretical maximum is 1 plate per label instance.
        bin_specs = [
            (DEFAULT_PLATE_WIDTH, DEFAULT_PLATE_HEIGHT, f"default_plate_{i + 1}")
            for i in range(len(rectangles))
        ]

    packer = _pack_best(rect_with_rid, bin_specs)

    # Verify all labels were packed.
    total_packed = sum(len(b) for b in packer)
    if total_packed < len(rectangles):
        if is_constrained:
            raise LayoutFitError(
                f"Could only fit {total_packed} of {len(rectangles)} labels "
                "on the provided plates. Please specify larger or "
                "additional plates."
            )
        else:
            # This should only trigger if a single label is larger than 24x16
            raise LayoutFitError(
                "A label's dimensions exceed the maximum plate size of "
                f"{DEFAULT_PLATE_WIDTH}x{DEFAULT_PLATE_HEIGHT}."
            )

    return _extract_packed_plates(packer)


def generate_layout_with_bounds(
    resolved_labels: list[ResolvedLabel],
    provided_plates: Optional[list[PlateSpec]] = None,
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
    """
    # Phase 2a: Render all unique labels and cache by ID
    rendered_labels = _render_labels_cache(resolved_labels)

    # Phase 2b: Unroll labels using rendered dimensions.
    rectangles = unroll_labels_with_rendered_bounds(resolved_labels, rendered_labels)
    rect_with_rid: list[_RectEntry] = [
        (w, h, (r_id, label_ref)) for w, h, r_id, label_ref, _ in rectangles
    ]

    is_constrained = provided_plates is not None and len(provided_plates) > 0

    if is_constrained:
        # Constrained mode: use exactly what the user provided.
        assert provided_plates is not None  # narrowed by is_constrained
        bin_specs = [
            (plate.width, plate.height, plate.id)
            for plate in provided_plates
            if plate.id is not None
        ]
    else:
        # Unbounded mode: provide enough default plates to guarantee a fit.
        # Theoretical maximum is 1 plate per label instance.
        bin_specs = [
            (DEFAULT_PLATE_WIDTH, DEFAULT_PLATE_HEIGHT, f"default_plate_{i + 1}")
            for i in range(len(rectangles))
        ]

    packer = _pack_best(rect_with_rid, bin_specs)

    # Verify all labels were packed.
    total_packed = sum(len(b) for b in packer)
    if total_packed < len(rectangles):
        if is_constrained:
            raise LayoutFitError(
                f"Could only fit {total_packed} of {len(rectangles)} labels "
                "on the provided plates. Please specify larger or "
                "additional plates."
            )
        else:
            # This should only trigger if a single label is larger than 24x16
            raise LayoutFitError(
                "A rendered label's dimensions exceed the maximum plate size of "
                f"{DEFAULT_PLATE_WIDTH}x{DEFAULT_PLATE_HEIGHT}."
            )

    plates = _extract_packed_plates(packer)

    # Return both the plates and the rendered labels cache for Phase 3
    return plates, rendered_labels
