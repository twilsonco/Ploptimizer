"""Vectorization engine for rendering PackedPlate objects to HPGL/PLT files.

This module bridges the gap between the virtual layout and the physical
machine by rendering the 2D bounding boxes into standard vector lines,
exporting them, and running them through the PLT optimization utility.

Coordinate System:
- Input (vpype Document): Plotter convention with origin at bottom-left,
  x+ rightward, y+ upward.
- Output (HPGL): Device convention with origin at top-left, x+ rightward,
  y+ downward. A Y-axis flip transformation is applied before export to
  convert from plotter convention to device convention.

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
# Retained for backward compatibility; text rendering now uses
# TEXT_BLOCK_HEIGHT_PER_SIZE to convert directly to document coordinates.
POINTS_PER_INCH: float = 72.0

# vpype's ``text_block()`` renders glyphs at approximately 0.65625 document
# units per unit of the ``size`` parameter (empirically measured for the
# default ``futural`` font). This is because the font's ``max_height`` is
# 32.0 and the glyphs are scaled by ``size / max_height`` internally, then
# the rendered glyph height happens to be ``max_height * 0.65625 / max_height
# * size = 0.65625 * size``. To produce text that is ``target_height``
# inches tall in document coordinates (matching ``rect``/``circle``), we
# must divide the target height by this factor.
TEXT_BLOCK_HEIGHT_PER_SIZE: float = 0.65625


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

    Uses plotter coordinate convention (origin bottom-left, y+ up).
    A Y-axis flip is applied later in vectorize_plate() to convert to
    device convention (origin top-left, y+ down).

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

    The boundary is drawn at the label's outer edges [0, width] × [0, height].
    Margin is NOT included in the boundary; it defines internal spacing for
    content only (text, holes).

    Args:
        source_label: The resolved label.
        dx: X translation in inches.
        dy: Y translation in inches.
        angle: Rotation angle in radians.

    Returns:
        A LineCollection containing the boundary rectangle.
    """
    # Boundary at the label's outer edges, starting at origin (0, 0)
    x = 0.0
    y = 0.0
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

    Each text line is rendered at its ``toolpath_text_height`` (the nominal
    height minus the cutter diameter) so the final cut geometry matches
    the requested nominal size. Lines are centered horizontally and
    vertically within the inner content area.

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
    inner_width = source_label.width
    inner_height = source_label.height
    text_lc = vp.LineCollection()

    # First pass: render all lines to calculate total height
    rendered_lines: list[tuple[vp.LineCollection, float]] = []
    total_rendered_height = 0.0

    for i, line in enumerate(source_label.content):
        # Render at the toolpath_text_height (cutter-compensated).
        # vpype's text_block() uses a different coordinate system than
        # rect()/circle(): the rendered glyph height is approximately
        # ``size * 0.65625`` document units (for the default ``futural``
        # font). To produce text that is ``toolpath_text_height`` inches
        # tall in document coordinates, divide by that factor.
        size = line.toolpath_text_height / TEXT_BLOCK_HEIGHT_PER_SIZE
        line_lc = vp.text_block(
            line.text,
            width=inner_width,
            size=size,
        )

        # Filter out spurious baseline/spacing segments generated by vpype
        # These are short horizontal 2-point lines (< 0.2") that appear between
        # character positions. Legitimate character strokes (like "T" crossbar)
        # are wider.
        filtered_lc = vp.LineCollection()
        for segment in line_lc:
            if segment is None or len(segment) == 0:
                continue
            # Skip very short horizontal 2-point lines (baseline/spacing artifacts)
            if len(segment) == 2:
                x_range = segment.real.max() - segment.real.min()
                y_range = segment.imag.max() - segment.imag.min()
                # Skip if it's a horizontal line (y_range ≈ 0) AND very short (< 0.2")
                # This removes baseline connectors but keeps character strokes like "T"
                if y_range < 0.01 and x_range < 0.2:
                    continue
            filtered_lc.append(segment)

        if filtered_lc.is_empty():
            continue

        bounds = filtered_lc.bounds()
        if bounds is None:
            continue
        _, min_y, _, max_y = bounds
        rendered_height = max_y - min_y

        rendered_lines.append((filtered_lc, rendered_height))
        total_rendered_height += rendered_height
        # Add line spacing between lines (not after the last line)
        if i < len(source_label.content) - 1:
            total_rendered_height += line.line_spacing

    if not rendered_lines:
        return text_lc

    # Calculate vertical centering within the available space (after margins)
    available_height = inner_height - (2 * margin)
    center_y = margin + available_height / 2
    # Start position: center_y offset by half the total text height
    # (text goes down from this position)
    current_y = center_y + total_rendered_height / 2

    # Second pass: position each line with horizontal and vertical centering
    for i, (line_lc, rendered_height) in enumerate(rendered_lines):
        bounds = line_lc.bounds()
        if bounds is None:
            continue
        min_x, min_y, max_x, max_y = bounds
        rendered_width = max_x - min_x

        # Horizontal centering within the available width
        available_width = inner_width - (2 * margin)
        center_x = margin + available_width / 2
        # Position text so it's centered: center_x - rendered_width / 2
        x_offset = center_x - rendered_width / 2 - min_x

        # Vertical centering: position top of text at current_y
        y_offset = current_y - max_y

        line_lc.translate(x_offset, y_offset)
        text_lc.extend(line_lc)

        # Move down for the next line
        current_y -= rendered_height
        # Add line spacing between lines
        if i < len(rendered_lines) - 1:
            line_spacing = source_label.content[i].line_spacing
            current_y -= line_spacing

    return _apply_transform(text_lc, dx, dy, angle)


# ---------------------------------------------------------------------------
# Coordinate system transformation
# ---------------------------------------------------------------------------
def _flip_y_coordinates(doc: vp.Document, plate_height: float) -> vp.Document:
    """Flip Y-axis coordinates to convert from plotter to device convention.

    Converts from plotter convention (origin bottom-left, y+ up) to device
    convention (origin top-left, y+ down) by mirroring all Y coordinates
    across the horizontal centerline of the plate.

    Transformation: y_flipped = plate_height - y_original

    vpype internally stores coordinates as complex numbers: x + yj

    Args:
        doc: The vpype Document to transform.
        plate_height: Height of the plate in inches.

    Returns:
        A new vpype.Document with Y-axis coordinates flipped.
    """
    result = vp.Document()

    for layer_id in doc.layers:
        flipped_lc = vp.LineCollection()
        layer_lc = doc.layers[layer_id]

        for line in layer_lc:
            # vpype stores coordinates as complex numbers: x + yj
            # Extract real (x) and imaginary (y) parts
            x = line.real
            y = line.imag
            # Flip Y-axis: y_flipped = plate_height - y_original
            y_flipped = plate_height - y
            # Reconstruct complex number array
            flipped_line = x + 1j * y_flipped
            flipped_lc.append(flipped_line)

        if not flipped_lc.is_empty():
            result.add(flipped_lc, layer_id)

    return result


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

    Coordinates are in plotter convention (origin at bottom-left, y+ up).
    The plotter visualization (_flip_y) will convert to display convention
    (origin at top-left, y+ down) when rendering PNG/matplotlib.

    Args:
        plate: The packed plate to vectorize.

    Returns:
        A vpype.Document containing all geometry for the plate in plotter
        coordinate convention.
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
# Layer extraction and export
# ---------------------------------------------------------------------------
def extract_layer(doc: vp.Document, layer_id: int) -> vp.Document:
    """Extract a single layer from a multi-layer document.

    Args:
        doc: The source vpype Document.
        layer_id: The layer ID to extract.

    Returns:
        A new vpype.Document containing only the specified layer.
    """
    new_doc = vp.Document()
    if layer_id in doc.layers:
        new_doc.add(doc.layers[layer_id], layer_id)
    return new_doc


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def export_to_plt(
    doc: vp.Document,
    output_path: str | Path,
    page_size: Optional[Tuple[float, float]] = None,
    landscape: bool = True,
    device: Optional[str] = None,
) -> Path:
    """Export a vpype Document to PLT/HPGL format using absolute positioning.

    Exports the document using PA (Pen Absolute) mode throughout with
    coordinates placed at the origin (no centering). This ensures the
    actual document coordinates are preserved and match the packed label
    positions on the plate.

    For the hp7475a device, only A3 and A4 page sizes are supported by
    vpype. The page size parameter is passed to vpype for compatibility but
    does not affect the coordinate data. Plates larger than the supported
    page sizes will have coordinates that exceed the device's plottable area,
    but the data integrity is preserved.

    Args:
        doc: The vpype Document to export (coordinates in document units).
        output_path: Destination file path.
        page_size: Optional ``(width, height)`` in inches. Currently unused
            due to vpype/HPGL device constraints. Future versions may use this
            for coordinate scaling or validation.
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

    if device is None:
        device = "hp7475a"  # Common HPGL-compatible device

    # Original document coordinates are in plotter convention (y+ up, origin at bottom).
    # vpype's write_hpgl() will output HPGL which also uses plotter convention.
    # The visualization (plotter.py) expects display convention (y+ down, origin at top).
    # We handle the coordinate flip in the visualization, not the export.
    #
    # NOTE: vpype only supports standard page sizes (A3, A4, etc.) for hp7475a device.
    # The page_size tuple is ignored here; we use "A3" which is large enough.

    with open(path, "w", encoding="utf-8") as f:
        vp.write_hpgl(
            f,
            doc,
            page_size="A3",  # Always use A3 - vpype only supports standard sizes
            landscape=landscape,
            center=True,  # Center on A3 page
            device=device,
            velocity=None,
            absolute=True,
        )

    return path.resolve()


# ---------------------------------------------------------------------------
# PLT optimization integration
# ---------------------------------------------------------------------------
def export_and_optimize(
    plates: list[PackedPlate],
    output_dir: str | Path,
    optimize: bool = True,
    separate_layers: bool = True,
) -> list[Path]:
    """Export plates to PLT files and optionally run them through the optimizer.

    This is the main entry point for Phase 3: it vectorizes each plate,
    exports to PLT format (optionally as separate layer files), and then
    runs the exported files through the PLT optimization utility to
    deduplicate overlapping score lines and minimize tool-up travel distance.

    Args:
        plates: List of packed plates to export.
        output_dir: Directory to write PLT files to.
        optimize: If True, run the PLT optimizer on each exported file.
        separate_layers: If True, export each layer (text, boundaries, holes)
            as separate PLT files with suffixes (_text, _borders, _holes).
            If False, export all layers in a single file.

    Returns:
        A list of paths to the exported (and optionally optimized) PLT files.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    exported_paths: list[Path] = []

    for plate in plates:
        doc = vectorize_plate(plate)
        page_size = (plate.width, plate.height)

        if separate_layers:
            # Export each layer separately
            layer_names = {
                LAYER_TEXT: "text",
                LAYER_BOUNDARY: "borders",
                LAYER_HOLES: "holes",
            }
            for layer_id, layer_name in layer_names.items():
                if layer_id in doc.layers and not doc.layers[layer_id].is_empty():
                    layer_doc = extract_layer(doc, layer_id)
                    output_path = output_dir / f"{plate.plate_id}_{layer_name}.plt"
                    export_to_plt(layer_doc, output_path, page_size=page_size)
                    exported_paths.append(output_path)
        else:
            # Export all layers in a single file
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
