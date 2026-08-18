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

    Uses ``MaxRectsBssf`` (Best Short Side Fit) heuristic which yields
    vertical stacking suitable for label-style layouts where labels should
    be arranged top-to-bottom rather than left-to-right.

    Returns:
        A configured ``rectpack.Packer`` ready to accept rectangles and bins.
    """
    return rectpack.newPacker(
        mode=rectpack.PackingMode.Offline,
        bin_algo=rectpack.PackingBin.BFF,
        pack_algo=rectpack.MaxRectsBssf,
        rotation=False,  # Disable rotation to preserve label orientation
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
    packer = initialize_packer()
    rectangles = unroll_labels(resolved_labels)

    # 1. Add rectangles to the packer
    for w, h, r_id, label_ref in rectangles:
        packer.add_rect(w, h, rid=(r_id, label_ref))

    # 2. Add bins (plates)
    is_constrained = provided_plates is not None and len(provided_plates) > 0

    if is_constrained:
        # Constrained mode: use exactly what the user provided
        assert provided_plates is not None  # narrowed by is_constrained
        for plate in provided_plates:
            assert plate.id is not None  # PlateSpec.id is required
            packer.add_bin(plate.width, plate.height, bid=plate.id)
    else:
        # Unbounded mode: provide enough default plates to guarantee a fit.
        # Theoretical maximum is 1 plate per label instance.
        max_possible_plates = len(rectangles)
        for i in range(max_possible_plates):
            packer.add_bin(
                DEFAULT_PLATE_WIDTH,
                DEFAULT_PLATE_HEIGHT,
                bid=f"default_plate_{i + 1}",
            )

    # 3. Execute packing algorithm
    packer.pack()

    # 4. Verify all labels were packed
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

    # 5. Extract results into typed data structures
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

    # Phase 2b: Unroll labels using rendered dimensions
    packer = initialize_packer()
    rectangles = unroll_labels_with_rendered_bounds(resolved_labels, rendered_labels)

    # 1. Add rectangles to the packer
    for w, h, r_id, label_ref, _rendered_ref in rectangles:
        packer.add_rect(w, h, rid=(r_id, label_ref))

    # 2. Add bins (plates)
    is_constrained = provided_plates is not None and len(provided_plates) > 0

    if is_constrained:
        # Constrained mode: use exactly what the user provided
        assert provided_plates is not None  # narrowed by is_constrained
        for plate in provided_plates:
            assert plate.id is not None  # PlateSpec.id is required
            packer.add_bin(plate.width, plate.height, bid=plate.id)
    else:
        # Unbounded mode: provide enough default plates to guarantee a fit.
        # Theoretical maximum is 1 plate per label instance.
        max_possible_plates = len(rectangles)
        for i in range(max_possible_plates):
            packer.add_bin(
                DEFAULT_PLATE_WIDTH,
                DEFAULT_PLATE_HEIGHT,
                bid=f"default_plate_{i + 1}",
            )

    # 3. Execute packing algorithm
    packer.pack()

    # 4. Verify all labels were packed
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

    # 5. Extract results into typed data structures
    plates = _extract_packed_plates(packer)

    # Return both the plates and the rendered labels cache for Phase 3
    return plates, rendered_labels
