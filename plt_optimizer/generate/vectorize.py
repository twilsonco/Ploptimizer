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
import re
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import vpype as vp

from plt_optimizer.generate.label_renderer import RenderedLabel
from plt_optimizer.generate.layout import PackedLabel, PackedPlate
from plt_optimizer.generate.resolution import ResolvedHoleSpec, ResolvedLabel
from plt_optimizer.generate.schema import PlateSpec

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
    postprocess: bool = True,
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
        postprocess: If True, apply coordinate post-processing (height fixing,
            scaling, translation). Set to False for layer exports that have
            already been processed through a parent document export.

    Returns:
        The absolute path to the written file.

    Raises:
        OSError: If the file cannot be written.
    """
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if device is None:
        device = "hp7475a"  # Common HPGL-compatible device

    # NOTE: vpype only supports standard page sizes (A3, A4, etc.) for hp7475a device.
    # The page_size tuple is ignored here; we use "A3" which is large enough.

    with open(path, "w", encoding="utf-8") as f:
        vp.write_hpgl(
            f,
            doc,
            page_size="A3",  # Always use A3 - vpype only supports standard sizes
            landscape=landscape,
            center=True,  # Center on page to ensure all coordinates are positive
            device=device,
            velocity=None,
            absolute=True,
        )

    if postprocess:
        # Post-process: fix rectangle heights to ensure consistency.
        # Due to vpype's coordinate rounding during compression, rectangles may
        # end up with slightly different heights. This function ensures all
        # rectangles have the same height.
        _fix_rectangle_heights_in_plt(path)

        # Post-process: scale coordinates to correct vpype compression.
        # vpype applies compression to fit the document on the A3 page. We scale
        # back to preserve the actual toolpath dimensions (1 inch = 1000 units).
        _scale_coordinates_in_plt(path)

        # Post-process: translate x and flip y-axis for plotter convention.
        # - X coordinates shifted so minimum x becomes 0 (left edge)
        # - Y-axis flipped (y_new = max_y - y_original) so first label has smallest y
        # - This ensures correct label ordering for visualization
        # - All coordinates remain non-negative and in plotter convention
        _translate_coordinates_to_origin_in_plt(path)

    return path.resolve()


def _fix_rectangle_heights_in_plt(file_path: Path) -> None:
    """Fix rectangle heights in PLT file to ensure consistency.

    Due to vpype's coordinate rounding during compression, rectangles may
    end up with slightly different heights. This function detects rectangles
    (closed PD commands with vertical and horizontal segments) and adjusts
    coordinates to ensure all rectangles have the same height.

    The algorithm:
    1. Find all PD (Pen Down) commands that draw rectangles
    2. Detect the most common rectangle height
    3. Adjust rectangles that are 1 unit shorter to match the common height
    4. Preserve x-coordinates and the top position

    Args:
        file_path: Path to the PLT file to modify.
    """
    content = file_path.read_text(encoding="utf-8")
    # Remove newlines to handle HPGL files with line wrapping
    content = content.replace("\n", "")

    # Find all PD commands with coordinate sequences
    # Match PD followed by comma-separated coordinates (including negative numbers)
    pd_pattern = r"PD([\d,\-]+)"

    def analyze_rectangle(coords_str: str) -> tuple[bool, int, int, int, int] | None:
        """Analyze if this PD command draws a rectangle and return its bounds.

        Returns:
            (is_rect, min_x, max_x, min_y, max_y) if it's a rectangle, None otherwise.
        """
        parts = coords_str.split(",")
        if len(parts) < 8:  # Rectangle needs at least 5 points (4 corners + close)
            return None

        try:
            points = [(int(parts[i]), int(parts[i + 1])) for i in range(0, len(parts), 2)]
        except ValueError:
            return None

        if len(points) < 4:
            return None

        # Check if it's a rectangle: should have ~4 unique corners + close point
        xs = [p[0] for p in points[:-1]]  # Exclude last point (should equal first)
        ys = [p[1] for p in points[:-1]]

        # A rectangle should have only 2 unique x-values and 2 unique y-values
        unique_xs = len(set(xs))
        unique_ys = len(set(ys))

        if unique_xs == 2 and unique_ys == 2:
            return (True, min(xs), max(xs), min(ys), max(ys))

        return None

    # Collect all rectangles
    rectangles: list[dict[str, int | str]] = []
    for match in re.finditer(pd_pattern, content):
        result = analyze_rectangle(match.group(1))
        if result:
            is_rect, min_x, max_x, min_y, max_y = result
            rectangles.append(
                {
                    "min_x": min_x,
                    "max_x": max_x,
                    "min_y": min_y,
                    "max_y": max_y,
                    "height": max_y - min_y,
                    "coords_str": match.group(1),
                }
            )

    if not rectangles:
        return

    # Find the most common height among LARGE rectangles (borders > 5 units)
    # This filters out small text bounding boxes (1-2 units) which can appear in raw files
    heights = [int(r["height"]) for r in rectangles if int(r["height"]) > 5]

    if not heights:
        # Fallback: use all rectangles if no large ones found
        heights = [int(r["height"]) for r in rectangles]

    common_height = max(set(heights), key=heights.count)

    # Fix rectangles that are 1 unit shorter
    def fix_rectangle(match: re.Match[str]) -> str:
        """Fix rectangle height if needed."""
        coords_str = match.group(1)
        rect_data: dict[str, int | str] | None = None

        # Find which rectangle this is
        for r in rectangles:
            if r["coords_str"] == coords_str:
                rect_data = r
                break

        if rect_data is None or int(rect_data["height"]) >= common_height:
            return match.group(0)

        # Skip small rectangles (text bounding boxes) - don't try to fix them
        if int(rect_data["height"]) <= 5:
            return match.group(0)

        # This rectangle is shorter, extend it
        parts = coords_str.split(",")
        points = [(int(parts[i]), int(parts[i + 1])) for i in range(0, len(parts), 2)]

        # Increase the minimum y-value by 1 to make height match
        adjusted_points = []
        for x, y in points:
            if y == int(rect_data["min_y"]):
                # Decrease min_y by 1 (since y+ is downward in coordinates)
                adjusted_points.append((x, y - 1))
            else:
                adjusted_points.append((x, y))

        # Rebuild coordinate string
        new_coords = ",".join(str(coord) for point in adjusted_points for coord in point)
        return f"PD{new_coords}"

    # Apply fixes
    modified_content = re.sub(pd_pattern, fix_rectangle, content)
    file_path.write_text(modified_content, encoding="utf-8")


def _scale_coordinates_in_plt(file_path: Path) -> None:
    """Scale all coordinates in a PLT file to correct vpype compression.

    vpype's write_hpgl with center=True applies compression to fit the
    document on the specified page size (A3 in our case). Since we want
    to preserve the actual toolpath dimensions, we scale the coordinates
    back up to their original intended size.

    The scaling factor is determined from the detected rectangles' dimensions.

    Args:
        file_path: Path to the PLT file to modify.
    """
    content = file_path.read_text(encoding="utf-8")
    # Remove newlines to handle HPGL files with line wrapping
    content = content.replace("\n", "")

    # First, detect rectangles to get the current scale
    pd_pattern = r"PD([\d,\-]+)"

    def analyze_rectangle(coords_str: str) -> tuple[int, int, int] | None:
        """Analyze if this PD command draws a rectangle.

        Returns:
            (min_y, max_y, height) if it's a rectangle, None otherwise.
        """
        parts = coords_str.split(",")
        if len(parts) < 8:
            return None

        try:
            points = [(int(parts[i]), int(parts[i + 1])) for i in range(0, len(parts), 2)]
        except ValueError:
            return None

        if len(points) < 4:
            return None

        xs = [p[0] for p in points[:-1]]
        ys = [p[1] for p in points[:-1]]

        unique_xs = len(set(xs))
        unique_ys = len(set(ys))

        if unique_xs == 2 and unique_ys == 2:
            height = abs(max(ys) - min(ys))
            return (min(ys), max(ys), height)
        return None

    # Find all rectangles
    rectangles = []
    for match in re.finditer(pd_pattern, content):
        result = analyze_rectangle(match.group(1))
        if result:
            rectangles.append(result)

    if not rectangles:
        import logging

        logging.getLogger(__name__).debug(
            f"_scale_coordinates_in_plt: No rectangles found in {file_path.name}"
        )
        return

    # Calculate scale from rectangle heights
    # Filter to large rectangles (borders > 5 units) to exclude text bounding boxes
    # This must match the filter in _fix_rectangle_heights_in_plt
    heights = [r[2] for r in rectangles if r[2] > 5]

    if not heights:
        # Fallback: use all rectangles if no large ones found
        heights = [r[2] for r in rectangles]

    avg_height = sum(heights) / len(heights)

    # Expected height: 1.0 inch = 1000 plotter units
    expected_height = 1000
    scale = expected_height / avg_height if avg_height > 0 else 1.0

    import logging

    logger = logging.getLogger(__name__)
    logger.debug(
        f"_scale_coordinates_in_plt: {file_path.name} - found {len(rectangles)} rectangles, "
        f"{len(heights)} large (>500), avg_height={avg_height:.1f}, scale={scale:.4f}"
    )

    # Find coordinate ranges for centering calculation
    # Use only PA and PD commands (actual content), exclude PU (spurious init)
    coord_pattern_pard = r"(PA|PD)([\d,\-]+)"
    all_x = []
    all_y = []

    for match in re.finditer(coord_pattern_pard, content):
        coords_str = match.group(2)
        parts = coords_str.split(",")
        try:
            for i in range(0, len(parts) - 1, 2):
                x = int(parts[i])
                y = int(parts[i + 1])
                all_x.append(x)
                all_y.append(y)
        except (ValueError, IndexError):
            # Skip invalid coordinate pairs
            continue

    if not all_x or not all_y:
        return

    # Find center for scaling transformation (based on actual content only)
    center_x = (min(all_x) + max(all_x)) / 2
    center_y = (min(all_y) + max(all_y)) / 2

    def scale_coordinates(match: re.Match[str]) -> str:
        """Scale coordinates in a command."""
        cmd = match.group(1)
        coords_str = match.group(2)
        parts = coords_str.split(",")

        try:
            scaled_parts = []
            for i, part in enumerate(parts):
                val = int(part)
                if i % 2 == 0:  # x coordinate
                    scaled_val = int(round((val - center_x) * scale + center_x))
                else:  # y coordinate
                    scaled_val = int(round((val - center_y) * scale + center_y))
                scaled_parts.append(str(scaled_val))
            return f"{cmd}{','.join(scaled_parts)}"
        except (ValueError, IndexError):
            return match.group(0)

    # Apply scaling to ALL commands (PA, PU, PD)
    coord_pattern_all = r"(PA|PU|PD)([\d,\-]+)"
    modified_content = re.sub(coord_pattern_all, scale_coordinates, content)
    file_path.write_text(modified_content, encoding="utf-8")


def _translate_coordinates_to_origin_in_plt(file_path: Path) -> None:
    """Translate and flip coordinates to plotter convention with correct label order.

    Applies two transformations to all coordinates:
    1. Translate x so min_x becomes 0 (left edge at origin)
    2. Flip y-axis: y_new = max_y - y (first label at y=0, last at y=max_y-min_y)

    The y-axis flip ensures that labels are in the correct order for plotter convention:
    - First label (which vpype places at y=max_y) becomes y=0 (bottom)
    - Last label (which vpype places at y=min_y) becomes y=max_y-min_y (top)
    - Result: y=0 at bottom, y+ points upward, all coordinates non-negative

    Removes spurious PU commands (pen-up/initialization) that have coordinates
    far outside the actual content bounds.

    Args:
        file_path: Path to the PLT file to modify.
    """
    content = file_path.read_text(encoding="utf-8")
    content_stripped = content.replace("\n", "")

    # Calculate min/max from PA and PD commands only (actual content)
    coord_pattern_pard = r"(PA|PD)([\d,\-]+)"
    all_x = []
    all_y = []

    for match in re.finditer(coord_pattern_pard, content_stripped):
        coords_str = match.group(2)
        parts = coords_str.split(",")
        try:
            for i in range(0, len(parts) - 1, 2):
                x = int(parts[i])
                y = int(parts[i + 1])
                all_x.append(x)
                all_y.append(y)
        except (ValueError, IndexError):
            continue

    if not all_x or not all_y:
        return

    # Find minimum and maximum coordinates from actual content
    min_x = min(all_x)
    min_y = min(all_y)
    max_y = max(all_y)

    # Skip if already at origin (unlikely after vpype output, but safe to check)
    if min_x == 0 and min_y == max_y:
        return

    # Remove spurious PU commands that are far outside content bounds
    content_range_x = max(all_x) - min_x
    content_range_y = max_y - min_y
    threshold_x = 2 * content_range_x if content_range_x > 0 else 100000
    threshold_y = 2 * content_range_y if content_range_y > 0 else 100000

    def is_spurious_pu(coords_str: str) -> bool:
        """Check if a PU command has coordinates far from content."""
        parts = coords_str.split(",")
        try:
            if len(parts) >= 2:
                x = int(parts[0])
                y = int(parts[1])
                # If any coordinate is far outside content bounds, it's spurious
                if abs(x - min_x) > threshold_x or abs(y - min_y) > threshold_y:
                    return True
        except ValueError:
            pass
        return False

    # Remove spurious PU commands - be aggressive to ensure they don't interfere
    # Remove ALL PU commands that don't match actual content bounds
    pu_pattern = r"(;?)PU([\d,\-]+)"
    matches_to_remove = []
    for match in re.finditer(pu_pattern, content_stripped):
        coords_str = match.group(2)
        if is_spurious_pu(coords_str):
            matches_to_remove.append((match.start(), match.end(), match.group(1)))

    # Remove matches in reverse order so indices don't shift
    for start, end, _leading_char in sorted(matches_to_remove, key=lambda x: x[0], reverse=True):
        # Keep the leading character (semicolon or nothing) if it exists
        content_stripped = content_stripped[:start] + content_stripped[end:]

    def translate_coordinates(match: re.Match[str]) -> str:
        """Translate x and flip y-axis for plotter convention with correct label order.

        Transformation:
        - x_new = x - min_x (shift to start at 0)
        - y_new = max_y - y (flip y-axis so first label has smallest y)

        After transformation:
        - First label (originally at y_max) is now at y=0
        - Last label (originally at y_min) is now at y=max_y-min_y
        - All coordinates are non-negative
        - Plotter convention: y=0 at bottom, y+ upward
        """
        cmd = match.group(1)
        coords_str = match.group(2)
        parts = coords_str.split(",")

        try:
            translated_parts = []
            for i, part in enumerate(parts):
                val = int(part)
                if i % 2 == 0:  # x coordinate - translate to start at 0
                    translated_val = val - min_x
                else:  # y coordinate - flip axis
                    # Flip y-axis: y_new = max_y - y_original
                    # This reverses label order so first label has smallest y
                    # Result range: [0, max_y - min_y] which is always non-negative
                    translated_val = max_y - val
                translated_parts.append(str(translated_val))
            return f"{cmd}{','.join(translated_parts)}"
        except (ValueError, IndexError):
            return match.group(0)

    # Apply translation to ALL commands (PA, PU, PD)
    coord_pattern_all = r"(PA|PU|PD)([\d,\-]+)"
    modified_content = re.sub(coord_pattern_all, translate_coordinates, content_stripped)

    # Second pass: ensure all coordinates are positive (in case spurious commands remain)
    final_min_x = None
    final_min_y = None
    for match in re.finditer(coord_pattern_all, modified_content):
        coords_str = match.group(2)
        parts = coords_str.split(",")
        try:
            for i in range(0, len(parts) - 1, 2):
                x = int(parts[i])
                y = int(parts[i + 1])
                if final_min_x is None or x < final_min_x:
                    final_min_x = x
                if final_min_y is None or y < final_min_y:
                    final_min_y = y
        except (ValueError, IndexError):
            continue

    # If any negative coordinates remain, translate them
    if final_min_x is not None and final_min_y is not None and (final_min_x < 0 or final_min_y < 0):
        shift_x = -final_min_x if final_min_x < 0 else 0
        shift_y = -final_min_y if final_min_y < 0 else 0

        def ensure_positive(match: re.Match[str]) -> str:
            """Translate to ensure all coordinates are positive."""
            cmd = match.group(1)
            coords_str = match.group(2)
            parts = coords_str.split(",")
            try:
                translated_parts = []
                for i, part in enumerate(parts):
                    val = int(part)
                    translated_parts.append(str(val + (shift_x if i % 2 == 0 else shift_y)))
                return f"{cmd}{','.join(translated_parts)}"
            except (ValueError, IndexError):
                return match.group(0)

        modified_content = re.sub(coord_pattern_all, ensure_positive, modified_content)

    file_path.write_text(modified_content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Phase 3: PLT Assembly and Export (new three-phase architecture)
# ---------------------------------------------------------------------------
def translate_plt_coordinates(plt_content: str, dx: float, dy: float) -> str:
    """Translate all coordinates in a PLT file by an offset.

    Parses HPGL PA/PU/PD commands and applies x/y offsets, converting
    from inches to plotter units (1 inch = 1000 units).

    Args:
        plt_content: Raw HPGL PLT text content.
        dx: X offset in inches.
        dy: Y offset in inches.

    Returns:
        Modified PLT content with translated coordinates (without header/footer).
    """
    # Convert offsets from inches to plotter units
    dx_units = int(round(dx * 1000.0))
    dy_units = int(round(dy * 1000.0))

    if dx_units == 0 and dy_units == 0:
        # No translation needed
        return plt_content

    import re

    def translate_coordinates(match: re.Match[str]) -> str:
        """Translate coordinates within a PA/PU/PD command."""
        cmd = match.group(1)
        coords_str = match.group(2)
        parts = coords_str.split(",")

        try:
            translated_parts = []
            for i, part in enumerate(parts):
                val = int(part)
                if i % 2 == 0:  # x coordinate
                    translated_val = val + dx_units
                else:  # y coordinate
                    translated_val = val + dy_units
                translated_parts.append(str(translated_val))
            return f"{cmd}{','.join(translated_parts)}"
        except (ValueError, IndexError):
            return match.group(0)

    coord_pattern = r"(PA|PU|PD)([\d,\-]+)"
    return re.sub(coord_pattern, translate_coordinates, plt_content)


def assemble_plt_from_rendered_labels(
    plate: PackedPlate,
    rendered_labels_map: dict[str, RenderedLabel],
) -> str:
    """Assemble a complete PLT for a plate from rendered labels.

    Takes all labels on a packed plate and combines their rendered PLT
    content, applying coordinate offsets to position each label at its
    final location on the plate.

    Args:
        plate: A packed plate containing positioned labels.
        rendered_labels_map: Cache of rendered labels by label ID.

    Returns:
        Complete HPGL/PLT content for the plate (with header and footer).
    """
    import re

    # Start with HPGL header
    plt_lines = ["IN;DF;PS0;"]

    # Process each label on the plate
    for packed_label in plate.labels:
        label_id = packed_label.source_label.id
        rendered = rendered_labels_map[label_id]

        # Get the rendered PLT content (strip header/footer)
        plt_content = rendered.plt_content
        # Remove header and footer
        plt_content = re.sub(r"IN;DF;PS0;SP\d+;", "", plt_content)
        plt_content = re.sub(r"SP0;IN;%?$", "", plt_content)
        plt_content = plt_content.strip()
        if plt_content.endswith(";"):
            plt_content = plt_content[:-1]

        # Translate coordinates to position on plate
        translated = translate_plt_coordinates(plt_content, packed_label.x, packed_label.y)

        # Add to assembly with pen-up command between labels
        if translated:
            plt_lines.append("PU0,0;")  # Pen up to safe position
            plt_lines.append(translated)
            plt_lines.append(";")

    # Add footer
    plt_lines.append("SP0;IN;%")

    return "".join(plt_lines)


def extract_layer_from_plt_text(plt_content: str, layer_pen_id: int) -> str:
    """Extract a single layer (pen) from PLT text content.

    Parses HPGL PLT text and filters commands to only include those
    for a specific pen ID (layer).

    Args:
        plt_content: Raw HPGL PLT text content.
        layer_pen_id: The pen ID to extract (1=text, 2=borders, 3=holes).

    Returns:
        PLT text content containing only commands for the specified pen.
    """
    lines = []
    lines.append("IN;DF;PS0;")

    # Parse and filter commands
    in_target_layer = False
    commands = plt_content.split(";")

    for cmd in commands:
        if not cmd.strip():
            continue

        # Check for pen select command
        if cmd.startswith("SP"):
            try:
                pen_id = int(cmd[2:])
                in_target_layer = pen_id == layer_pen_id
                if in_target_layer and pen_id > 0:
                    lines.append(f"SP{pen_id};")
            except (ValueError, IndexError):
                pass
        # Include drawing commands only if we're in target layer
        elif in_target_layer and cmd.strip() and not cmd.startswith("IN"):
            lines.append(f"{cmd};")

    lines.append("SP0;IN;%")
    return "".join(lines)


def export_and_optimize_phase3(
    resolved_labels: list[ResolvedLabel],
    provided_plates: list[PlateSpec] | None = None,
    output_dir: str | Path = "output",
    optimize: bool = True,
    separate_layers: bool = True,
) -> list[Path]:
    """Export plates to PLT files using the Phase 3 pipeline (new architecture).

    Implements the complete three-phase pipeline:
    1. Phase 1: Render each label independently with bounds measurement
    2. Phase 2: Bin-pack labels onto plates using rendered dimensions
    3. Phase 3: Assemble rendered labels at their packed positions

    This function fixes label centering issues by rendering each label
    independently rather than using global postprocessing.

    Args:
        resolved_labels: List of resolved labels from Phase 2 resolution step.
        provided_plates: Optional list of PlateSpec objects. If None, uses
            standard A3 paper (11"×8.5").
        output_dir: Directory to write PLT files to.
        optimize: If True, run the PLT optimizer on each exported file.
        separate_layers: If True, export each layer (text, boundaries, holes)
            as separate PLT files with suffixes (_text, _borders, _holes).
            If False, export all layers in a single file.

    Returns:
        A list of paths to the exported (and optionally optimized) PLT files.
    """
    from plt_optimizer.generate.layout import generate_layout_with_bounds

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Use default plates if not provided
    if provided_plates is None:
        provided_plates = [
            PlateSpec(
                id="default",
                width=11.0,
                height=8.5,
                margin=0.5,
                clearance_padding=0.0,
            )
        ]

    # Phase 2: Generate layout with rendered bounds
    packed_plates, rendered_labels_map = generate_layout_with_bounds(
        resolved_labels, provided_plates
    )

    exported_paths: list[Path] = []

    # Phase 3: Assemble PLT from rendered labels for each plate
    for plate in packed_plates:
        # Assemble complete PLT
        plt_content = assemble_plt_from_rendered_labels(plate, rendered_labels_map)

        if separate_layers:
            # Export each layer separately
            layer_names = {
                LAYER_TEXT: "text",
                LAYER_BOUNDARY: "borders",
                LAYER_HOLES: "holes",
            }
            for layer_id, layer_name in layer_names.items():
                layer_content = extract_layer_from_plt_text(plt_content, layer_id)
                # Only write non-empty layers
                if len(layer_content) > 20:  # More than just header
                    output_path = output_dir / f"{plate.plate_id}_{layer_name}.plt"
                    output_path.write_text(layer_content)
                    exported_paths.append(output_path)
        else:
            # Export all layers in a single file
            output_path = output_dir / f"{plate.plate_id}.plt"
            output_path.write_text(plt_content)
            exported_paths.append(output_path)

    if optimize:
        # Run the PLT optimizer on each exported file
        exported_paths = _run_optimizer(exported_paths)

    return exported_paths


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
                    # Export layer with full post-processing to ensure coordinates are correct
                    export_to_plt(layer_doc, output_path, page_size=page_size, postprocess=True)
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
