"""Vectorization engine for rendering PackedPlate objects to HPGL/PLT files.

This module bridges the gap between the virtual layout and the physical
machine by rendering the 2D bounding boxes into standard vector lines,
exporting them, and running them through the PLT optimization utility.

Layer mapping:
- Layer 1: Text (engraving)
- Layer 2: Boundaries (score/cut lines)
- Layer 3: Drill holes

The module handles coordinate transformations for labels that were rotated
90 degrees by the bin packer, and renders text using vpype's internal
Hershey font engine.

Example:
    >>> from plt_optimizer.generate.layout import generate_layout
    >>> from plt_optimizer.generate.resolution import resolve_job_spec
    >>> from plt_optimizer.generate.vectorize import vectorize_plate, export_to_plt
    >>> job = parse_yaml("examples/sample_spec.yaml")
    >>> labels = resolve_job_spec(job)
    >>> plates = generate_layout(labels, job.plates)
    >>> doc = vectorize_plate(plates[0])
    >>> export_to_plt(doc, "output.plt")
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import vpype as vp

from plt_optimizer.generate.layout import PackedLabel, PackedPlate
from plt_optimizer.generate.resolution import ResolvedHoleSpec, ResolvedLabel

# ---------------------------------------------------------------------------
# Layer assignments
# ---------------------------------------------------------------------------
LAYER_TEXT: int = 1
LAYER_BOUNDARY: int = 2
LAYER_HOLES: int = 3

# vpype text_block uses points (72pt = 1 inch)
POINTS_PER_INCH: float = 72.0


# ---------------------------------------------------------------------------
# Coordinate transformation
# ---------------------------------------------------------------------------
def _get_transform_matrix(packed_label: PackedLabel) -> Tuple[float, float, float]:
    """Return translation and rotation for a packed label.

    The rectpack library rotates around the bottom-left origin, so a
    90-degree rotation swaps width and height while keeping the
    bottom-left corner fixed.

    Args:
        packed_label: The packed label to transform.

    Returns:
        A tuple of ``(dx, dy, angle_radians)`` describing the
        transformation to apply to local coordinates.
    """
    dx, dy = packed_label.x, packed_label.y

    if packed_label.rotated:
        # rectpack rotates 90 degrees around the bottom-left origin
        return (dx, dy, math.pi / 2)

    return (dx, dy, 0.0)


def _apply_transform(
    lc: vp.LineCollection,
    dx: float,
    dy: float,
    angle: float,
) -> vp.LineCollection:
    """Apply translation and rotation to a LineCollection.

    Note: vpype's ``rotate()`` and ``translate()`` modify in-place and
    return ``None``, so this function works on a copy created via extend.

    Args:
        lc: The LineCollection to transform.
        dx: X translation in inches.
        dy: Y translation in inches.
        angle: Rotation angle in radians.

    Returns:
        A new LineCollection with the transformation applied.
    """
    result = vp.LineCollection()
    result.extend(lc)
    if angle != 0.0:
        result.rotate(angle)
    if dx != 0.0 or dy != 0.0:
        result.translate(dx, dy)
    return result


# ---------------------------------------------------------------------------
# Hole rendering
# ---------------------------------------------------------------------------
def _hole_center(
    hole: ResolvedHoleSpec,
    label_width: float,
    label_height: float,
    margin: float,
) -> Tuple[float, float]:
    """Calculate the center coordinates for a hole on a label.

    Args:
        hole: The resolved hole specification.
        label_width: Label width in inches (inner content area).
        label_height: Label height in inches (inner content area).
        margin: Label margin in inches.

    Returns:
        A tuple of ``(cx, cy)`` in the label's local coordinate space.
    """
    inner_w = label_width
    inner_h = label_height

    if hole.location == "left":
        return (margin, margin + inner_h / 2)
    if hole.location == "right":
        return (margin + inner_w, margin + inner_h / 2)
    if hole.location == "top":
        return (margin + inner_w / 2, margin + inner_h)
    if hole.location == "bottom":
        return (margin + inner_w / 2, margin)
    if hole.location == "top-left":
        return (margin, margin + inner_h)
    if hole.location == "top-right":
        return (margin + inner_w, margin + inner_h)
    if hole.location == "bottom-left":
        return (margin, margin)
    if hole.location == "bottom-right":
        return (margin + inner_w, margin)
    # Fallback to center
    return (margin + inner_w / 2, margin + inner_h / 2)


def _render_holes(
    source_label: ResolvedLabel,
    dx: float,
    dy: float,
    angle: float,
) -> vp.LineCollection:
    """Render all holes for a label as a LineCollection.

    Args:
        source_label: The resolved label containing hole specifications.
        dx: X translation in inches.
        dy: Y translation in inches.
        angle: Rotation angle in radians.

    Returns:
        A LineCollection containing circle geometries for each hole.
    """
    if not source_label.holes:
        return vp.LineCollection()

    margin = source_label.margin
    inner_w = source_label.width
    inner_h = source_label.height

    lines: List[np.ndarray] = []
    for hole in source_label.holes:
        cx, cy = _hole_center(hole, inner_w, inner_h, margin)
        radius = hole.diameter / 2
        # vpype.circle(cx, cy, radius) returns a closed circle as np.ndarray
        circle = vp.circle(cx, cy, radius)
        lines.append(circle)

    holes_lc = vp.LineCollection(lines)
    return _apply_transform(holes_lc, dx, dy, angle)


# ---------------------------------------------------------------------------
# Boundary rendering
# ---------------------------------------------------------------------------
def _render_boundary(
    source_label: ResolvedLabel,
    dx: float,
    dy: float,
    angle: float,
) -> vp.LineCollection:
    """Render the boundary rectangle for a label.

    The boundary is drawn at the inner content area (excluding margin)
    so adjacent labels share collinear segments.

    Args:
        source_label: The resolved label.
        dx: X translation in inches.
        dy: Y translation in inches.
        angle: Rotation angle in radians.

    Returns:
        A LineCollection containing the boundary rectangle.
    """
    margin = source_label.margin
    # Boundary at the inner content area, starting at (margin, margin)
    x = margin
    y = margin
    w = source_label.width
    h = source_label.height
    # vpype.rect(x, y, width, height) returns a closed rectangle
    boundary_line = vp.rect(x, y, w, h)
    boundary_lc = vp.LineCollection([boundary_line])
    return _apply_transform(boundary_lc, dx, dy, angle)


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------
def _render_text(
    source_label: ResolvedLabel,
    dx: float,
    dy: float,
    angle: float,
) -> vp.LineCollection:
    """Render text content for a label using vpype's Hershey font engine.

    Each text line is rendered at its resolved ``text_height`` and stacked
    vertically using ``line_spacing``. Text is left-aligned within the
    inner content area.

    Args:
        source_label: The resolved label containing text content.
        dx: X translation in inches.
        dy: Y translation in inches.
        angle: Rotation angle in radians.

    Returns:
        A LineCollection containing all text line geometries.
    """
    if not source_label.content:
        return vp.LineCollection()

    margin = source_label.margin
    text_lc = vp.LineCollection()

    # Stack lines vertically starting from the top of the inner content area
    current_y = margin + source_label.height  # Start at top of inner area

    for line in source_label.content:
        # Render at the resolved text_height (convert inches to points)
        size_pt = line.text_height * POINTS_PER_INCH
        # Use a generous width; vpype will render at the given size
        line_lc = vp.text_block(
            line.text,
            width=source_label.width * POINTS_PER_INCH,
            size=size_pt,
        )

        # Scale from points back to inches
        line_lc.scale(1.0 / POINTS_PER_INCH)

        # Get the rendered height to position the next line
        bounds = line_lc.bounds()
        if bounds is None:
            continue
        _, min_y, _, max_y = bounds
        rendered_height = max_y - min_y

        # Position this line at the current Y (top-down stacking)
        line_lc.translate(margin, current_y - max_y)

        text_lc.extend(line_lc)

        # Move down for the next line
        current_y -= rendered_height + line.line_spacing

    return _apply_transform(text_lc, dx, dy, angle)


# ---------------------------------------------------------------------------
# Main rendering
# ---------------------------------------------------------------------------
def _render_label_to_doc(packed_label: PackedLabel, doc: vp.Document) -> None:
    """Render a single packed label into the appropriate layers of a document.

    Args:
        packed_label: The packed label to render.
        doc: The vpype Document to append geometry to.
    """
    source_label = packed_label.source_label
    dx, dy, angle = _get_transform_matrix(packed_label)

    # Layer 1: Text
    text_lc = _render_text(source_label, dx, dy, angle)
    if not text_lc.is_empty():
        doc.add(text_lc, LAYER_TEXT)

    # Layer 2: Boundary
    boundary_lc = _render_boundary(source_label, dx, dy, angle)
    if not boundary_lc.is_empty():
        doc.add(boundary_lc, LAYER_BOUNDARY)

    # Layer 3: Holes
    holes_lc = _render_holes(source_label, dx, dy, angle)
    if not holes_lc.is_empty():
        doc.add(holes_lc, LAYER_HOLES)


def vectorize_plate(plate: PackedPlate) -> vp.Document:
    """Render a packed plate into a vpype Document with layered geometry.

    Creates three layers:
    - Layer 1: Text (engraving)
    - Layer 2: Boundaries (score/cut lines)
    - Layer 3: Drill holes

    Args:
        plate: The packed plate to vectorize.

    Returns:
        A vpype.Document containing all geometry for the plate.
    """
    doc = vp.Document()

    for packed_label in plate.labels:
        _render_label_to_doc(packed_label, doc)

    return doc


def vectorize_plates(plates: list[PackedPlate]) -> list[vp.Document]:
    """Render multiple packed plates into separate vpype Documents.

    Args:
        plates: List of packed plates to vectorize.

    Returns:
        A list of vpype.Document objects, one per plate.
    """
    return [vectorize_plate(plate) for plate in plates]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def export_to_plt(
    doc: vp.Document,
    output_path: str | Path,
    page_size: Optional[Tuple[float, float]] = None,
    landscape: bool = False,
    device: Optional[str] = None,
) -> Path:
    """Export a vpype Document to PLT/HPGL format.

    Args:
        doc: The vpype Document to export.
        output_path: Destination file path.
        page_size: Optional ``(width, height)`` in inches. If None,
            uses a default that fits most plates.
        landscape: If True, rotates the output to landscape orientation.
        device: Optional device name for HPGL output. If None, uses
            ``"hp7475a"`` which is a common HPGL-compatible device.

    Returns:
        The absolute path to the written file.

    Raises:
        OSError: If the file cannot be written.
    """
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # vpype requires a standard page size name and a valid device
    # Use a large standard size that fits most plates
    page_size_name = "A3"  # 11.69 x 16.54 inches, fits 24x16 plates
    if device is None:
        device = "hp7475a"  # Common HPGL-compatible device

    with open(path, "w", encoding="utf-8") as f:
        vp.write_hpgl(
            f,
            doc,
            page_size=page_size_name,
            landscape=landscape,
            center=True,
            device=device,
            velocity=None,
        )

    return path.resolve()


# ---------------------------------------------------------------------------
# PLT optimization integration
# ---------------------------------------------------------------------------
def export_and_optimize(
    plates: list[PackedPlate],
    output_dir: str | Path,
    optimize: bool = True,
) -> list[Path]:
    """Export plates to PLT files and optionally run them through the optimizer.

    This is the main entry point for Phase 3: it vectorizes each plate,
    exports to PLT format, and then runs the exported files through the
    PLT optimization utility to deduplicate overlapping score lines and
    minimize tool-up travel distance.

    Args:
        plates: List of packed plates to export.
        output_dir: Directory to write PLT files to.
        optimize: If True, run the PLT optimizer on each exported file.

    Returns:
        A list of paths to the exported (and optionally optimized) PLT files.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    exported_paths: list[Path] = []

    for plate in plates:
        doc = vectorize_plate(plate)
        page_size = (plate.width, plate.height)
        output_path = output_dir / f"{plate.plate_id}.plt"
        export_to_plt(doc, output_path, page_size=page_size)
        exported_paths.append(output_path)

    if optimize:
        # Run the PLT optimizer on each exported file
        exported_paths = _run_optimizer(exported_paths)

    return exported_paths


def _run_optimizer(plt_paths: list[Path]) -> list[Path]:
    """Run the PLT optimizer on a list of PLT files.

    Uses the existing PLT parser, profiler, chunker, optimizer, reassembler,
    and writer to deduplicate overlapping score lines and minimize tool-up
    travel distance.

    Args:
        plt_paths: List of paths to PLT files to optimize.

    Returns:
        A list of paths to the optimized PLT files (overwrites originals).
    """
    # Import here to avoid circular imports
    from plt_optimizer.core.chunker import Chunker, ChunkerConfig
    from plt_optimizer.core.optimizer import (
        NearestNeighbor2OptStrategy,
        OptimizerEngine,
    )
    from plt_optimizer.core.parser import PLTParser
    from plt_optimizer.core.profiler import Profiler
    from plt_optimizer.core.reassembler import Reassembler
    from plt_optimizer.core.writer import PLTWriter

    optimized_paths: list[Path] = []

    for plt_path in plt_paths:
        try:
            # Parse the exported PLT file
            parser = PLTParser()
            doc = parser.parse_file(plt_path)

            # Profile to determine document type
            profiler = Profiler()
            profile_result = profiler.profile(doc)

            # Chunk into MacroBlocks
            chunker = Chunker(config=ChunkerConfig(threshold_multiplier=2.0))
            blocks = chunker.chunk(
                doc.stroke_paths,
                profile_result.baseline_extent,
                is_structural=profile_result.is_structural,
            )

            if not blocks:
                # No blocks to optimize; keep the file as-is
                optimized_paths.append(plt_path)
                continue

            # Run the optimizer (use fast mode for generated files)
            strategy = NearestNeighbor2OptStrategy()
            optimizer = OptimizerEngine(strategy=strategy)
            optimization_result = optimizer.optimize(blocks)

            # Reassemble the optimized document
            reassembler = Reassembler()
            optimized_doc = reassembler.reassemble(doc, blocks, optimization_result)

            # Write the optimized result back
            writer = PLTWriter()
            writer.write_file(optimized_doc, plt_path)
            optimized_paths.append(plt_path)
        except Exception:
            # If optimization fails for any reason, keep the original file
            optimized_paths.append(plt_path)

    return optimized_paths
