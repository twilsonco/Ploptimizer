"""Independent label rendering engine.

Renders individual labels to HPGL/PLT format with measured bounds.
Each label is rendered in isolation at local coordinates (0, 0), exported
with full postprocessing, and then bounds are extracted for packing.

This module eliminates the need for complex coordinate transformations
and postprocessing that was causing edge cases (e.g., label 3 centering bug).
"""

from __future__ import annotations

import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

import numpy as np
import vpype as vp

from plt_optimizer.generate.arc_converter import polyline_to_arc
from plt_optimizer.generate.resolution import ResolvedLabel

logger = logging.getLogger(__name__)

# vpype text rendering constants
POINTS_PER_INCH: float = 72.0
TEXT_BLOCK_HEIGHT_PER_SIZE: float = 0.65625

# Layer assignments (must match vectorize.py constants)
LAYER_TEXT: int = 1
LAYER_BOUNDARY: int = 2
LAYER_HOLES: int = 3


@dataclass(frozen=True)
class RenderedLabel:
    """A label rendered to HPGL format with measured bounds.

    Attributes:
        source_label: The original ResolvedLabel that was rendered.
        plt_content: Raw HPGL text content (without postprocessing).
        x_min: Minimum x-coordinate in inches.
        y_min: Minimum y-coordinate in inches.
        x_max: Maximum x-coordinate in inches.
        y_max: Maximum y-coordinate in inches.
        width: Actual rendered width in inches (x_max - x_min).
        height: Actual rendered height in inches (y_max - y_min).
    """

    source_label: ResolvedLabel
    plt_content: str
    x_min: float
    y_min: float
    x_max: float
    y_max: float
    width: float
    height: float


def render_label_to_plt(label: ResolvedLabel) -> RenderedLabel:
    """Render a label independently to HPGL format and extract bounds.

    Renders the label at local coordinates (origin at bottom-left, no translation).
    Exports to temporary file with postprocessing to ensure coordinates are
    correct and compressed to 1:1000 scale (1 inch = 1000 units).

    Args:
        label: The ResolvedLabel to render (text, borders, holes).

    Returns:
        RenderedLabel with rendered PLT content and measured bounds.

    Raises:
        ValueError: If bounds cannot be extracted from rendered PLT.
    """
    # Create vpype Document
    doc = vp.Document()

    # Render text layer
    text_lc = _render_text_local(label)
    if not text_lc.is_empty():
        doc.add(text_lc, LAYER_TEXT)

    # Render boundary layer
    boundary_lc = _render_boundary_local(label)
    if not boundary_lc.is_empty():
        doc.add(boundary_lc, LAYER_BOUNDARY)

    # Render holes layer
    holes_lc = _render_holes_local(label)
    if not holes_lc.is_empty():
        doc.add(holes_lc, LAYER_HOLES)

    # Export to temporary file with postprocessing
    with tempfile.NamedTemporaryFile(mode="w", suffix=".plt", delete=False) as f:
        temp_path = Path(f.name)

    try:
        _export_to_plt_with_postprocessing(doc, temp_path, label)
        plt_content = temp_path.read_text().strip()

        # Ensure proper formatting (ends with %)
        if not plt_content.endswith("%"):
            if plt_content.endswith("IN;"):
                plt_content += "%"
            else:
                plt_content = plt_content.rstrip(";") + ";%"

        # Extract bounds from rendered PLT
        x_min, y_min, x_max, y_max = extract_bounds_from_plt(plt_content)

        return RenderedLabel(
            source_label=label,
            plt_content=plt_content,
            x_min=x_min,
            y_min=y_min,
            x_max=x_max,
            y_max=y_max,
            width=x_max - x_min,
            height=y_max - y_min,
        )
    finally:
        temp_path.unlink(missing_ok=True)


def extract_bounds_from_plt(plt_content: str) -> Tuple[float, float, float, float]:
    """Extract coordinate bounds from HPGL PLT content.

    Parses PA, PU (Pen Up), and PD (Pen Down) commands to find the
    minimum and maximum coordinates. Converts from plotter units
    (1 inch = 1000 units) to inches.

    Args:
        plt_content: Raw HPGL text content.

    Returns:
        Tuple of (x_min, y_min, x_max, y_max) in inches.

    Raises:
        ValueError: If no valid coordinates found in PLT content.
    """
    x_coords = []
    y_coords = []

    # Extract all coordinates from PA, PU, and PD commands
    pattern = r"(?:PA|PU|PD)([\d,\-]+)"

    for match in re.finditer(pattern, plt_content):
        coords_str = match.group(1)
        parts = coords_str.split(",")

        try:
            for i in range(0, len(parts) - 1, 2):
                x = int(parts[i])
                y = int(parts[i + 1])
                x_coords.append(x)
                y_coords.append(y)
        except (ValueError, IndexError):
            continue

    if not x_coords or not y_coords:
        raise ValueError("No valid coordinates found in PLT content")

    # Convert from plotter units (1000 units = 1 inch) to inches
    x_min = min(x_coords) / 1000.0
    x_max = max(x_coords) / 1000.0
    y_min = min(y_coords) / 1000.0
    y_max = max(y_coords) / 1000.0

    return x_min, y_min, x_max, y_max


def _export_to_plt_with_postprocessing(
    doc: vp.Document, output_path: Path, label: ResolvedLabel
) -> None:
    """Export vpype Document to PLT with unified scaling for single labels.

    Each label is rendered independently. Instead of using vpype's write_hpgl()
    which applies complex coordinate transformations, we manually generate HPGL
    commands from the LineCollection to preserve coordinate fidelity.

    Process:
    1. Extract coordinates directly from vpype LineCollection (units are inches)
    2. Build raw HPGL commands without any transformation
    3. Scale coordinates to match expected label dimensions
    4. Center text layer (pen 1) vertically
    5. Convert polylines to arc commands where appropriate

    Args:
        doc: The vpype Document to export.
        output_path: Destination PLT file path.
        label: The label being rendered (used to get expected dimensions).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Manually generate HPGL from LineCollection to preserve coordinates
    hpgl_content = _linecollection_to_hpgl(doc)

    # Write to file
    output_path.write_text(hpgl_content, encoding="utf-8")

    # Scale all coordinates uniformly to match label dimensions
    _scale_coordinates_unified(output_path, label)

    # Center text layer (pen 1) vertically within label bounds
    _center_text_layer_vertically(output_path, label)


def _linecollection_to_hpgl(doc: vp.Document) -> str:
    """Convert vpype Document to raw HPGL commands with arc detection.

    Manually generates HPGL from LineCollections instead of using vpype's
    write_hpgl() to preserve coordinate fidelity. vpype's export applies
    complex coordinate transformations that distort text height.

    Attempts to fit circular arcs to polyline segments where possible,
    outputting AA (Arc Absolute) commands for smooth curves.

    Coordinates are converted from inches (vpype units) to plotter units
    (1 inch = 1000 units).

    Args:
        doc: The vpype Document containing line collections for each pen.

    Returns:
        Raw HPGL/PLT content as a string.
    """
    lines = [
        "IN",  # Initialize
        "DF",  # Default values
        "PS0",  # Select primary pen slot
    ]

    # Track if we've added any content to know if we need footer
    has_content = False
    skipped_first_pu0_0 = False  # Track if we've skipped the initial PU0,0

    # Process each pen layer in the document
    # Pens are numbered 0-7, but we typically use 1 (text), 2 (border), 3 (holes)
    for pen_num in range(4):
        lc = doc.layers.get(pen_num)
        if lc is None or lc.is_empty():
            continue

        # Select this pen
        lines.append(f"SP{pen_num}")
        has_content = True

        # Extract segments from LineCollection
        for segment in lc:
            if segment is None or len(segment) == 0:
                continue

            # Convert from inches to plotter units (1 inch = 1000 units)
            points = []
            for point in segment:
                x_plotter = int(round(point.real * 1000))
                y_plotter = int(round(point.imag * 1000))
                points.append((x_plotter, y_plotter))

            if not points:
                continue

            # Start with PU (pen up) to first point
            x, y = points[0]

            # Skip initial PU0,0 in the very first segment of any layer
            # Assembly will add it, so we avoid duplication
            if not skipped_first_pu0_0 and x == 0 and y == 0:
                skipped_first_pu0_0 = True
                # Still process the drawing if there are more points
                if len(points) > 1:
                    # Try to convert to arc if polyline has enough points
                    arc_cmd = polyline_to_arc(
                        [(p[0], p[1]) for p in points[1:]], (x, y), max_error=5.0
                    )
                    if arc_cmd is not None:
                        # Output arc command
                        lines.append(f"PD;{arc_cmd.to_hpgl()}")
                    else:
                        # Output as polyline
                        pd_coords = ",".join(f"{px},{py}" for px, py in points[1:])
                        lines.append(f"PD{pd_coords}")
            else:
                lines.append(f"PU{x},{y}")

                # Draw to remaining points with PD (pen down)
                if len(points) > 1:
                    # Try to convert to arc if polyline has enough points
                    arc_cmd = polyline_to_arc(
                        [(p[0], p[1]) for p in points[1:]], (x, y), max_error=5.0
                    )
                    if arc_cmd is not None:
                        # Output arc command
                        lines.append(f"PD;{arc_cmd.to_hpgl()}")
                    else:
                        # Output as polyline
                        pd_coords = ",".join(f"{px},{py}" for px, py in points[1:])
                        lines.append(f"PD{pd_coords}")

    # End sequence - no PU command in footer, let assembly add it
    if has_content:
        lines.append("SP0")
        lines.append("IN")

    return ";".join(lines) + ";"


def _scale_coordinates_unified(file_path: Path, label: ResolvedLabel) -> None:
    """Scale all coordinates uniformly for a single label to expected dimensions.

    All pen layers (text, border, holes) are scaled with the SAME scale factors
    based on the combined bounds of all coordinates. This ensures layers stay
    aligned even when they have different coordinate ranges from vpype.

    Args:
        file_path: Path to the PLT file.
        label: The label being rendered (for expected dimensions).
    """
    content = file_path.read_text(encoding="utf-8")

    # Extract ALL coordinates from all layers to find actual bounds
    pattern = r"(?:PA|PU|PD)([\d,\-]+)"
    all_x = []
    all_y = []

    for match in re.finditer(pattern, content):
        coords_str = match.group(1)
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

    x_min, x_max = min(all_x), max(all_x)
    y_min, y_max = min(all_y), max(all_y)

    # Current coordinate ranges (in plotter units from vpype)
    x_range = x_max - x_min if x_max > x_min else 1
    y_range = y_max - y_min if y_max > y_min else 1

    # Expected ranges (in inches, at 1:1000 scale, so multiply by 1000)
    # Use the label's actual dimensions
    expected_x_range_units = label.width * 1000.0
    expected_y_range_units = label.height * 1000.0

    # UNIFIED scaling: use the same scale for all coordinates
    scale_x = expected_x_range_units / x_range if x_range > 0 else 1.0
    scale_y = expected_y_range_units / y_range if y_range > 0 else 1.0

    logger.debug(
        f"_scale_coordinates_unified: {file_path.name} - "
        f"vpype bounds: x=[{x_min}, {x_max}] ({x_range}), "
        f"y=[{y_min}, {y_max}] ({y_range}), "
        f'label={label.width:.1f}"×{label.height:.1f}", '
        f"unified scale: x={scale_x:.4f}, y={scale_y:.4f}"
    )

    def scale_coordinates(match: re.Match[str]) -> str:
        """Scale x and y coordinates independently."""
        cmd = match.group(1)
        coords_str = match.group(2)
        parts = coords_str.split(",")

        try:
            scaled_parts = []
            for i, part in enumerate(parts):
                val = int(part)
                if i % 2 == 0:  # x coordinate
                    scaled_val = int(round(val * scale_x))
                else:  # y coordinate
                    scaled_val = int(round(val * scale_y))
                scaled_parts.append(str(scaled_val))
            return f"{cmd}{','.join(scaled_parts)}"
        except (ValueError, IndexError):
            return match.group(0)

    coord_pattern = r"(PA|PU|PD)([\d,\-]+)"
    modified_content = re.sub(coord_pattern, scale_coordinates, content)

    # After scaling, translate all coordinates to origin
    modified_content = _translate_all_to_origin(modified_content)

    file_path.write_text(modified_content, encoding="utf-8")


def _center_text_layer_vertically(file_path: Path, label: ResolvedLabel) -> None:
    """Center text layer (pen 1) vertically within label bounds after scaling.

    After export and scaling, the text coordinates need to be shifted vertically
    to center them within the label. This function:
    1. Extracts Y coordinates from pen 1 (TEXT) only
    2. Calculates the expected center position
    3. Adjusts all text Y coordinates to center the text

    Args:
        file_path: Path to the PLT file (after scaling).
        label: The label being rendered (for dimensions and margins).
    """
    content = file_path.read_text(encoding="utf-8")

    # Extract Y coordinates from pen 1 (TEXT) only
    pattern = r"SP1;(.*?)(?:SP\d|$)"
    matches = re.search(pattern, content, re.DOTALL)
    if not matches:
        # No text layer found
        return

    text_section = matches.group(1)
    y_coords = []
    coord_pattern = r"(?:PA|PU|PD)([\d,\-]+)"
    for match in re.finditer(coord_pattern, text_section):
        coords_str = match.group(1)
        parts = coords_str.split(",")
        for i in range(1, len(parts), 2):
            try:
                y = int(parts[i])
                y_coords.append(y)
            except (ValueError, IndexError):
                pass

    if not y_coords:
        # No coordinates in text layer
        return

    # Calculate text bounds (in plotter units, post-scaling)
    text_y_min = min(y_coords)
    text_y_max = max(y_coords)

    # Expected center position (in plotter units: 1 inch = 1000 units)
    margin_units = label.margin * 1000.0
    label_height_units = label.height * 1000.0
    available_height_units = label_height_units - (2 * margin_units)
    expected_center_y = margin_units + available_height_units / 2.0

    # Current center of text
    current_center_y = (text_y_min + text_y_max) / 2.0

    # Calculate adjustment
    y_adjustment = expected_center_y - current_center_y

    if abs(y_adjustment) < 0.1:
        # Already centered
        return

    logger.debug(
        f"_center_text_layer_vertically: {file_path.name} - "
        f"text Y=[{text_y_min}, {text_y_max}] (center={current_center_y:.1f}), "
        f"expected center={expected_center_y:.1f}, "
        f"adjustment={y_adjustment:.1f} plotter units"
    )

    def adjust_text_y(match: re.Match[str]) -> str:
        """Adjust Y coordinates in pen 1 (TEXT) only."""
        coords_str = match.group(1)
        parts = coords_str.split(",")

        try:
            adjusted_parts = []
            for i, part in enumerate(parts):
                val = int(part)
                if i % 2 == 1:  # Y coordinate (odd index)
                    adjusted_val = int(round(val + y_adjustment))
                    adjusted_parts.append(str(adjusted_val))
                else:  # X coordinate
                    adjusted_parts.append(part)
            return f"PA{','.join(adjusted_parts)}" if ",".join(adjusted_parts) else ""
        except (ValueError, IndexError):
            return match.group(0)

    # Find and replace: Extract pen 1 section, adjust Y, replace it
    def replace_pen1_section(match: re.Match[str]) -> str:
        sp1_and_content = match.group(0)
        # Adjust Y coordinates within this section
        coord_pattern_in_pen = r"(PA|PU|PD)([\d,\-]+)"

        def adjust_coords_in_pen(coord_match: re.Match[str]) -> str:
            cmd = coord_match.group(1)
            coords_str = coord_match.group(2)
            parts = coords_str.split(",")

            try:
                adjusted_parts = []
                for i, part in enumerate(parts):
                    val = int(part)
                    if i % 2 == 1:  # Y coordinate (odd index)
                        adjusted_val = int(round(val + y_adjustment))
                        adjusted_parts.append(str(adjusted_val))
                    else:  # X coordinate
                        adjusted_parts.append(part)
                return f"{cmd}{','.join(adjusted_parts)}"
            except (ValueError, IndexError):
                return coord_match.group(0)

        return re.sub(coord_pattern_in_pen, adjust_coords_in_pen, sp1_and_content)

    # Replace the pen 1 section with adjusted coordinates
    modified_content = re.sub(pattern, replace_pen1_section, content, flags=re.DOTALL)

    file_path.write_text(modified_content, encoding="utf-8")


def _scale_coordinates_per_layer(file_path: Path, label: ResolvedLabel) -> None:
    """[DEPRECATED - use _scale_coordinates_unified instead]

    This function is kept for backward compatibility but should not be used.
    Use _scale_coordinates_unified which applies uniform scaling to all layers.
    """
    pass


def _translate_all_to_origin(content: str) -> str:
    """Translate all coordinates to origin after scaling.

    Finds minimum coordinates across all layers and shifts so (min_x, min_y)
    becomes (0, 0).
    """
    # Extract coordinates again after scaling
    pattern = r"(?:PA|PU|PD)([\d,\-]+)"
    all_x = []
    all_y = []

    for match in re.finditer(pattern, content):
        coords_str = match.group(1)
        parts = coords_str.split(",")
        try:
            for i in range(0, len(parts) - 1, 2):
                x = int(parts[i])
                y = int(parts[i + 1])
                if abs(x) < 100000 and abs(y) < 100000:
                    all_x.append(x)
                    all_y.append(y)
        except (ValueError, IndexError):
            continue

    if not all_x or not all_y:
        return content

    min_x = min(all_x)
    min_y = min(all_y)

    if min_x == 0 and min_y == 0:
        return content

    def translate_coordinates(match: re.Match[str]) -> str:
        """Translate coordinates to origin."""
        cmd = match.group(1)
        coords_str = match.group(2)
        parts = coords_str.split(",")

        try:
            translated_parts = []
            for i, part in enumerate(parts):
                val = int(part)
                if i % 2 == 0:  # x coordinate
                    translated_val = val - min_x
                else:  # y coordinate
                    translated_val = val - min_y
                translated_parts.append(str(translated_val))
            return f"{cmd}{','.join(translated_parts)}"
        except (ValueError, IndexError):
            return match.group(0)

    coord_pattern = r"(PA|PU|PD)([\d,\-]+)"
    return re.sub(coord_pattern, translate_coordinates, content)


def _extract_coordinates_by_pen(
    plt_content: str,
) -> dict[int, list[tuple[float, float]]]:
    """Extract coordinates grouped by pen number from PLT content.

    [DEPRECATED - no longer needed with unified scaling]

    Args:
        plt_content: Raw HPGL text content.

    Returns:
        Dictionary mapping pen number to list of (x, y) coordinate tuples
        in plotter units.
    """
    coordinates_by_pen: dict[int, list[tuple[float, float]]] = {1: [], 2: [], 3: []}

    current_pen = 0

    # Split by SP (Select Pen) commands
    pen_sections = re.split(r"SP(\d+);", plt_content)

    # pen_sections will be: [before_first_SP, first_pen_num, first_pen_content, ...]
    for i in range(1, len(pen_sections), 2):
        pen_num_str = pen_sections[i]
        pen_content = pen_sections[i + 1] if i + 1 < len(pen_sections) else ""

        try:
            pen_num = int(pen_num_str)
            current_pen = pen_num
        except (ValueError, IndexError):
            continue

        # Extract coordinates from this pen section
        coordinate_pattern = r"(PA|PU|PD)([\d,\-]+)"
        matches = re.findall(coordinate_pattern, pen_content)

        for _cmd, coords_str in matches:
            parts = coords_str.split(",")
            for j in range(0, len(parts) - 1, 2):
                try:
                    x_val = int(parts[j])
                    y_val = int(parts[j + 1])
                    if current_pen in coordinates_by_pen:
                        coordinates_by_pen[current_pen].append((x_val, y_val))
                except (ValueError, IndexError):
                    pass

    return coordinates_by_pen


def _translate_after_scaling(content: str) -> str:
    """Translate coordinates to origin after scaling.

    Finds minimum coordinates and shifts so (min_x, min_y) becomes (0, 0).
    [DEPRECATED - use _translate_all_to_origin instead]
    """
    return _translate_all_to_origin(content)


def _fix_rectangle_heights_in_plt(file_path: Path) -> None:
    """Ensure all rectangles have consistent height.

    vpype's coordinate rounding during compression can cause slightly
    different rectangle heights. This function detects the most common
    height and adjusts shorter rectangles to match.
    """
    content = file_path.read_text(encoding="utf-8")
    pd_pattern = r"PD([\d,\-]+)"

    def analyze_rectangle(coords_str: str) -> Tuple[int, int, int] | None:
        """Analyze if a PD command draws a rectangle and return (min_y, max_y, height)."""
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

    # Find all rectangles and their heights
    rectangles = []
    for match in re.finditer(pd_pattern, content):
        result = analyze_rectangle(match.group(1))
        if result:
            rectangles.append(result)

    if not rectangles:
        return

    # Find common height (largest height among rectangles > 5 units)
    large_heights = [r[2] for r in rectangles if r[2] > 5]
    if not large_heights:
        return

    common_height = max(large_heights)
    heights = [r[2] for r in rectangles if r[2] > 5]
    if heights:
        avg_height = sum(heights) / len(heights)
        common_height = int(round(avg_height))

    logger.debug(
        f"_fix_rectangle_heights_in_plt: {file_path.name} - found {len(rectangles)} "
        f"rectangles, {len(heights)} large (>500), avg_height={avg_height:.1f}, "
        f"common_height={common_height}"
    )

    # Fix shorter rectangles
    def fix_rectangle(match: re.Match[str]) -> str:
        """Extend shorter rectangles to match common height."""
        coords_str = match.group(1)
        result = analyze_rectangle(coords_str)
        if result is None or int(result[2]) >= common_height:
            return match.group(0)
        if int(result[2]) <= 5:
            return match.group(0)

        parts = coords_str.split(",")
        points = [(int(parts[i]), int(parts[i + 1])) for i in range(0, len(parts), 2)]
        adjusted_points = []
        for x, y in points:
            if y == result[0]:
                adjusted_points.append((x, y - 1))
            else:
                adjusted_points.append((x, y))

        new_coords = ",".join(str(v) for p in adjusted_points for v in p)
        return f"PD{new_coords}"

    modified_content = re.sub(pd_pattern, fix_rectangle, content)
    file_path.write_text(modified_content, encoding="utf-8")


def _scale_coordinates_in_plt(file_path: Path) -> None:
    """Scale coordinates to correct vpype compression.

    vpype applies compression when fitting to A3 page. This calculates
    the scale factor from detected rectangle heights and rescales all
    coordinates to restore 1:1000 ratio (1 inch = 1000 units).
    """
    content = file_path.read_text(encoding="utf-8")
    pd_pattern = r"PD([\d,\-]+)"

    def analyze_rectangle(coords_str: str) -> Tuple[int, int, int] | None:
        """Analyze if a PD command draws a rectangle and return (min_y, max_y, height)."""
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

    rectangles = []
    for match in re.finditer(pd_pattern, content):
        result = analyze_rectangle(match.group(1))
        if result:
            rectangles.append(result)

    if not rectangles:
        return

    heights = [r[2] for r in rectangles if r[2] > 500]
    if not heights:
        return

    avg_height = sum(heights) / len(heights)
    expected_height = 1000
    scale = expected_height / avg_height if avg_height > 0 else 1.0

    # Find center for scaling
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
            continue

    if not all_x or not all_y:
        return

    center_x = (min(all_x) + max(all_x)) / 2
    center_y = (min(all_y) + max(all_y)) / 2

    def scale_coordinates(match: re.Match[str]) -> str:
        """Scale coordinates."""
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

    coord_pattern_all = r"(PA|PU|PD)([\d,\-]+)"
    modified_content = re.sub(coord_pattern_all, scale_coordinates, content)
    file_path.write_text(modified_content, encoding="utf-8")

    logger.debug(
        f"_scale_coordinates_in_plt: {file_path.name} - found {len(rectangles)} rectangles, "
        f"{len(heights)} large (>500), avg_height={avg_height:.1f}, scale={scale:.4f}"
    )


def _translate_coordinates_to_origin_in_plt(file_path: Path) -> None:
    """Translate and flip coordinates for plotter convention.

    Applies two transformations:
    1. Translate x so min_x becomes 0 (left edge at origin)
    2. Flip y-axis: y_new = max_y - y (first label at y=0, last at y=max_y)
    """
    content = file_path.read_text(encoding="utf-8")
    content_stripped = content

    # Extract all PA/PD coordinates to find ranges
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
            continue

    if not all_x or not all_y:
        return

    min_x = min(all_x)
    min_y = min(all_y)
    max_y = max(all_y)

    if min_x == 0 and min_y == max_y:
        return

    # Remove spurious PU commands
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
                if abs(x - min_x) > threshold_x or abs(y - min_y) > threshold_y:
                    return True
        except ValueError:
            pass
        return False

    pu_pattern = r"(;?)PU([\d,\-]+)"
    matches_to_remove = []
    for match in re.finditer(pu_pattern, content_stripped):
        coords_str = match.group(2)
        if is_spurious_pu(coords_str):
            matches_to_remove.append((match.start(), match.end(), match.group(1)))

    for start, end, _leading_char in sorted(matches_to_remove, key=lambda x: x[0], reverse=True):
        content_stripped = content_stripped[:start] + content_stripped[end:]

    def translate_coordinates(match: re.Match[str]) -> str:
        """Translate x and flip y."""
        cmd = match.group(1)
        coords_str = match.group(2)
        parts = coords_str.split(",")

        try:
            translated_parts = []
            for i, part in enumerate(parts):
                val = int(part)
                if i % 2 == 0:  # x coordinate
                    translated_val = val - min_x
                else:  # y coordinate
                    translated_val = max_y - val
                translated_parts.append(str(translated_val))
            return f"{cmd}{','.join(translated_parts)}"
        except (ValueError, IndexError):
            return match.group(0)

    coord_pattern_all = r"(PA|PU|PD)([\d,\-]+)"
    modified_content = re.sub(coord_pattern_all, translate_coordinates, content_stripped)
    file_path.write_text(modified_content, encoding="utf-8")

    logger.debug(
        f"_translate_coordinates_to_origin_in_plt: {file_path.name} - "
        f"translated x by {-min_x}, flipped y (max_y={max_y})"
    )


# ============================================================================
# Local rendering functions (render at 0,0 without translation)
# ============================================================================


def _render_text_local(label: ResolvedLabel) -> vp.LineCollection:
    """Render text at local coordinates (0, 0).

    NOTE: We render at (0, 0) WITHOUT vertical centering because vpype's
    write_hpgl() with center=True overwrites any pre-export translation.
    Vertical centering is applied POST-EXPORT in _center_text_layer_vertically().

    Renders all text lines horizontally centered within the label's content area.
    """
    if not label.content:
        return vp.LineCollection()

    margin = label.margin
    inner_width = label.width
    text_lc = vp.LineCollection()

    # Render all lines at their natural positions (no vertical offset)
    for _i, line in enumerate(label.content):
        # Render at the toolpath_text_height (cutter-compensated)
        # vpype's text_block() rendered glyph height ≈ size * 0.65625 document units
        size = line.toolpath_text_height / TEXT_BLOCK_HEIGHT_PER_SIZE
        line_lc = vp.text_block(
            line.text,
            width=inner_width,
            size=size,
        )

        # Filter out spurious baseline/spacing segments generated by vpype
        filtered_lc = vp.LineCollection()
        for segment in line_lc:
            if segment is None or len(segment) == 0:
                continue
            # Skip very short horizontal 2-point lines (baseline/spacing artifacts)
            if len(segment) == 2:
                x_range = segment.real.max() - segment.real.min()
                y_range = segment.imag.max() - segment.imag.min()
                # Skip if horizontal (y_range < 0.01) AND very short (< 0.2")
                if y_range < 0.01 and x_range < 0.2:
                    continue
            filtered_lc.append(segment)

        if filtered_lc.is_empty():
            continue

        bounds = filtered_lc.bounds()
        if bounds is None:
            continue
        min_x, min_y, max_x, max_y = bounds
        rendered_width = max_x - min_x

        # Horizontal centering within the available width
        available_width = inner_width - (2 * margin)
        center_x = margin + available_width / 2
        # Position text so it's centered horizontally
        x_offset = center_x - rendered_width / 2 - min_x

        # NO vertical offset here - will be applied post-export
        y_offset = -min_y

        line_lc.translate(x_offset, y_offset)
        text_lc.extend(line_lc)

    return text_lc


def _render_boundary_local(label: ResolvedLabel) -> vp.LineCollection:
    """Render boundary rectangle at local coordinates."""
    lc = vp.LineCollection()
    rect_array = vp.rect(0, 0, label.width, label.height)
    # vp.rect() returns a numpy array of complex coordinates, need to convert to LineCollection
    if isinstance(rect_array, np.ndarray) and rect_array.size > 0:
        lc.append(rect_array)
    return lc


def _render_holes_local(label: ResolvedLabel) -> vp.LineCollection:
    """Render holes at local coordinates."""
    lc = vp.LineCollection()

    for hole in label.holes:
        # Calculate hole position based on location
        # Offset is the hole radius (diameter/2) from the edge
        offset = hole.diameter / 2.0

        if hole.location == "top-left":
            hole_x = offset
            hole_y = label.height - offset
        elif hole.location == "top-right":
            hole_x = label.width - offset
            hole_y = label.height - offset
        elif hole.location == "bottom-left":
            hole_x = offset
            hole_y = offset
        elif hole.location == "bottom-right":
            hole_x = label.width - offset
            hole_y = offset
        else:
            continue

        # Render hole as circle
        circle = vp.circle(hole_x, hole_y, offset)
        lc.extend(circle)

    return lc
