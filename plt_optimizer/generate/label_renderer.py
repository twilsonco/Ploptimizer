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
    """Export vpype Document to PLT with postprocessing for single labels.

    For individual labels, we use center=True to ensure vpype properly renders
    content, then apply scaling based on the label's expected dimensions
    to convert vpype's output coordinates to the expected 1:1000 plotter
    coordinate system.

    Args:
        doc: The vpype Document to export.
        output_path: Destination PLT file path.
        label: The label being rendered (used to get expected dimensions).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Export using vpype's HPGL writer
    with open(output_path, "w", encoding="utf-8") as f:
        vp.write_hpgl(
            f,
            doc,
            page_size="A3",
            landscape=True,
            center=True,  # Center content - vpype scales coordinates accordingly
            device="hp7475a",
            velocity=None,
            absolute=True,
        )

    # For single labels, use simple coordinate scaling based on actual bounds
    _scale_coordinates_simple(output_path, label)


def _scale_coordinates_simple(file_path: Path, label: ResolvedLabel) -> None:
    """Scale coordinates for a single label to expected dimensions.

    Vpype's center=True applies scaling to fit content on the A3 page,
    and the output aspect ratio may differ from the label's expected aspect ratio.
    We scale x and y independently to achieve the expected label dimensions.
    """
    content = file_path.read_text(encoding="utf-8")

    # Extract all coordinates to find actual bounds
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

    scale_x = expected_x_range_units / x_range if x_range > 0 else 1.0
    scale_y = expected_y_range_units / y_range if y_range > 0 else 1.0

    logger.debug(
        f"_scale_coordinates_simple: {file_path.name} - "
        f"vpype ranges: x={x_range}, y={y_range}, "
        f'label={label.width:.1f}"×{label.height:.1f}", '
        f"scale: x={scale_x:.4f}, y={scale_y:.4f}"
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

    # After scaling, translate to origin
    modified_content = _translate_after_scaling(modified_content)

    file_path.write_text(modified_content, encoding="utf-8")


def _translate_after_scaling(content: str) -> str:
    """Translate coordinates to origin after scaling.

    Finds minimum coordinates and shifts so (min_x, min_y) becomes (0, 0).
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

    Renders all text lines centered within the label's content area,
    matching the original vectorize._render_text() implementation.
    """
    if not label.content:
        return vp.LineCollection()

    margin = label.margin
    inner_width = label.width
    inner_height = label.height
    text_lc = vp.LineCollection()

    # First pass: render all lines to calculate total height
    rendered_lines: list[tuple[vp.LineCollection, float]] = []
    total_rendered_height = 0.0

    for i, line in enumerate(label.content):
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
        _, min_y, _, max_y = bounds
        rendered_height = max_y - min_y

        rendered_lines.append((filtered_lc, rendered_height))
        total_rendered_height += rendered_height
        # Add line spacing between lines (not after the last line)
        if i < len(label.content) - 1:
            total_rendered_height += line.line_spacing

    if not rendered_lines:
        return text_lc

    # Calculate vertical centering within the available space (after margins)
    available_height = inner_height - (2 * margin)
    center_y = margin + available_height / 2
    # Start position: center_y offset by half the total text height
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
        # Position text so it's centered horizontally
        x_offset = center_x - rendered_width / 2 - min_x

        # Vertical centering: position top of text at current_y
        y_offset = current_y - max_y

        line_lc.translate(x_offset, y_offset)
        text_lc.extend(line_lc)

        # Move down for the next line
        current_y -= rendered_height
        # Add line spacing between lines
        if i < len(rendered_lines) - 1:
            line_spacing = label.content[i].line_spacing
            current_y -= line_spacing

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
