"""Diagnostic plotting functionality for PLT-Optimizer.

This module provides visualization tools to render parsed HPGL/PLT data,
including color-coded path visualization based on cumulative distance traveled.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import Optional

import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator

from plt_optimizer.core.models import (
    ArcSegment,
    Coordinate,
    PLTDocument,
    Segment,
    StrokePath,
)
from plt_optimizer.utils.geometry import calculate_cumulative_distances

# Plotter units are 1/1000ths of an inch; divide by 1000 for inches
PLT_UNITS_TO_INCHES = 1 / 1000


def _flip_y(y: float) -> float:
    """Negate y-coordinate to flip vertical direction for display."""
    return -y


def _arc_to_points(arc: ArcSegment, num_segments: int = 32) -> list[Coordinate]:
    """Sample points along an arc for linear approximation.

    Args:
        arc: The arc segment to sample.
        num_segments: Number of line segments to use for approximation.

    Returns:
        List of Coordinates sampled along the arc from start to end.
    """
    theta_start = math.atan2(arc.start.y - arc.center.y, arc.start.x - arc.center.x)
    delta_theta = arc.sweep_angle * math.pi / 180 / num_segments
    radius = arc.radius

    points: list[Coordinate] = []
    for i in range(num_segments + 1):
        theta = theta_start + i * delta_theta
        x = arc.center.x + radius * math.cos(theta)
        y = arc.center.y + radius * math.sin(theta)
        points.append(Coordinate(x, y))

    return points


# Default figure size in inches (16:9 aspect ratio suitable for wide tables)
DEFAULT_FIGURE_SIZE = (16, 9)


class PlotterError(Exception):
    """Exception raised when plotting operations fail.

    Attributes:
        message: Human-readable error description.
    """

    def __init__(self, message: str) -> None:
        """Initialize a PlotterError."""
        self.message = message
        super().__init__(message)


def plot_plt_document(
    document: PLTDocument,
    output_path: Path | None = None,
    title: str = "PLT Toolpath Visualization",
    show_plot: bool = False,
    figure_size: tuple[float, float] = DEFAULT_FIGURE_SIZE,
    rapid_travel_inches: float | None = None,
) -> plt.Figure:
    """Plot a complete PLT document with color-coded path segments.

    This function renders the toolpath with colors mapped to cumulative
    distance traveled, making it easy to identify:
    - Where cutting starts (typically blue/cooler colors)
    - The sequential progression of the toolpath
    - Where cutting ends (typically red/hotter colors)
    - Rapid air travel moves (shown in gray/dashed)

    Args:
        document: The parsed PLTDocument to visualize.
        output_path: Optional path to save the figure. Format inferred from extension.
        title: Title for the plot window/figure.
        show_plot: If True, call plt.show() to display interactively.
        figure_size: Figure dimensions in inches as (width, height).
        rapid_travel_inches: Optional pre-calculated rapid travel distance in inches.
            If provided, uses this value in the summary text box instead of
            computing it from document.rapid_distance(). This ensures consistency
            with any external calculation passed via title string.

    Returns:
        The matplotlib Figure object.

    Raises:
        PlotterError: If plotting fails.
    """
    try:
        fig, ax = plt.subplots(figsize=figure_size)

        all_segments: list[Segment] = []
        segment_is_cutting: list[bool] = []

        for path in document.stroke_paths:
            for seg in path.segments:
                all_segments.append(seg)
                segment_is_cutting.append(seg.is_cutting)

        if not all_segments:
            ax.text(
                0.5,
                0.5,
                "No segments to plot",
                ha="center",
                va="center",
                transform=ax.transAxes,
                fontsize=14,
            )
            return fig

        # Calculate cumulative distances for color mapping
        coord_pairs = [(seg.start, seg.end) for seg in all_segments]
        cum_distances = calculate_cumulative_distances(coord_pairs)

        if cum_distances:
            max_distance = cum_distances[-1] if cum_distances else 1.0
        else:
            max_distance = 1.0

        # Normalize distances to [0, 1] for colormap
        norm_distances = [d / max_distance if max_distance > 0 else 0.0 for d in cum_distances]

        # Separate cutting and rapid segments
        cutting_lines: list[tuple[float, float]] = []
        cutting_colors: list[float] = []
        rapid_lines: list[tuple[float, float]] = []

        for i, (seg, is_cutting) in enumerate(zip(all_segments, segment_is_cutting)):
            line_coords = [(seg.start.x, _flip_y(seg.start.y)), (seg.end.x, _flip_y(seg.end.y))]
            if is_cutting:
                cutting_lines.append(line_coords)
                cutting_colors.append(norm_distances[i])
            else:
                rapid_lines.append(line_coords)

        # Calculate axis limits from all segments with 10% padding (in inches)
        if all_segments:
            all_x = [seg.start.x * PLT_UNITS_TO_INCHES for seg in all_segments] + [
                seg.end.x * PLT_UNITS_TO_INCHES for seg in all_segments
            ]
            all_y = [_flip_y(seg.start.y) * PLT_UNITS_TO_INCHES for seg in all_segments] + [
                _flip_y(seg.end.y) * PLT_UNITS_TO_INCHES for seg in all_segments
            ]
            x_min, x_max = min(all_x), max(all_x)
            y_min, y_max = min(all_y), max(all_y)

            # Add 10% padding (5% on each side)
            x_range = x_max - x_min if x_max != x_min else abs(x_max) * 0.1 if x_max != 0 else 1.0
            y_range = y_max - y_min if y_max != y_min else abs(y_max) * 0.1 if y_max != 0 else 1.0
            padding_x = x_range * 0.1
            padding_y = y_range * 0.1

            ax.set_xlim(x_min - padding_x, x_max + padding_x)
            ax.set_ylim(y_min - padding_y, y_max + padding_y)

        # Plot each segment individually for better visibility and axis handling
        # First, plot all rapid moves (dotted gray lines)
        for i, seg in enumerate(all_segments):
            if not seg.is_cutting:  # Rapid travel
                if isinstance(seg, ArcSegment):
                    arc_points = _arc_to_points(seg)
                    xs = [p.x * PLT_UNITS_TO_INCHES for p in arc_points]
                    ys = [_flip_y(p.y) * PLT_UNITS_TO_INCHES for p in arc_points]
                    ax.plot(
                        xs, ys,
                        color="gray",
                        linewidth=0.5,
                        linestyle="dotted",
                        alpha=0.7,
                        label="Rapid Travel (PU)" if i == 0 else "",
                    )
                else:
                    ax.plot(
                        [seg.start.x * PLT_UNITS_TO_INCHES, seg.end.x * PLT_UNITS_TO_INCHES],
                        [_flip_y(seg.start.y) * PLT_UNITS_TO_INCHES,
                         _flip_y(seg.end.y) * PLT_UNITS_TO_INCHES],
                        color="gray",
                        linewidth=0.4,
                        linestyle="dotted",
                        alpha=0.7,
                        label="Rapid Travel (PU)" if i == 0 else "",
                    )

        # Plot tool-up (rapid) connections between strokes as dashed gray lines
        paths = document.stroke_paths
        for i in range(len(paths) - 1):
            curr_path = paths[i]
            next_path = paths[i + 1]

            if not curr_path.segments or next_path.pen_up_position is None:
                continue

            last_seg = curr_path.segments[-1]
            start_x = last_seg.end.x * PLT_UNITS_TO_INCHES
            start_y = _flip_y(last_seg.end.y) * PLT_UNITS_TO_INCHES
            end_x = next_path.pen_up_position.x * PLT_UNITS_TO_INCHES
            end_y = _flip_y(next_path.pen_up_position.y) * PLT_UNITS_TO_INCHES

            ax.plot(
                [start_x, end_x],
                [start_y, end_y],
                color="gray",
                linewidth=0.4,
                linestyle="dashed",
                alpha=0.7,
                label="Rapid Travel (PU)" if i == 0 else "",
            )

        # Then, plot all cutting moves with plasma colormap
        for i, seg in enumerate(all_segments):
            if seg.is_cutting:  # Cutting
                color_val = norm_distances[i]
                cmap = plt.colormaps["plasma"]
                color = cmap(color_val)

                if isinstance(seg, ArcSegment):
                    arc_points = _arc_to_points(seg)
                    xs = [p.x * PLT_UNITS_TO_INCHES for p in arc_points]
                    ys = [_flip_y(p.y) * PLT_UNITS_TO_INCHES for p in arc_points]
                    ax.plot(
                        xs, ys,
                        color=color,
                        linewidth=0.5,
                        alpha=0.9,
                        label="Cutting Path" if i == 2 else "",
                    )
                else:
                    ax.plot(
                        [seg.start.x * PLT_UNITS_TO_INCHES, seg.end.x * PLT_UNITS_TO_INCHES],
                        [_flip_y(seg.start.y) * PLT_UNITS_TO_INCHES,
                         _flip_y(seg.end.y) * PLT_UNITS_TO_INCHES],
                        color=color,
                        linewidth=0.5,
                        alpha=0.9,
                        label="Cutting Path" if i == 2 else "",
                    )

        # Add colorbar for cutting paths
        if any(seg.is_cutting for seg in all_segments):
            # Create a dummy plot to create colorbar
            cbar = plt.colorbar(plt.cm.ScalarMappable(cmap=plt.cm.plasma), ax=ax, shrink=0.8)
            cbar.set_label("Cumulative Distance (%)", rotation=270, labelpad=15)

        # Mark start and end points
        first_seg = all_segments[0]
        last_seg = all_segments[-1]

        ax.plot(
            first_seg.start.x * PLT_UNITS_TO_INCHES,
            _flip_y(first_seg.start.y) * PLT_UNITS_TO_INCHES,
            marker="o",
            markersize=6,
            color="green",
            zorder=10,
            label="Start",
        )
        ax.plot(
            last_seg.end.x * PLT_UNITS_TO_INCHES,
            _flip_y(last_seg.end.y) * PLT_UNITS_TO_INCHES,
            marker="s",
            markersize=6,
            color="red",
            zorder=10,
            label="End",
        )

        # Configure axes
        ax.set_xlabel("X (inches)")
        ax.set_ylabel("Y (inches)")
        ax.set_title(title)
        ax.xaxis.set_major_locator(MultipleLocator(1))
        ax.yaxis.set_major_locator(MultipleLocator(1))
        ax.legend(loc="upper right")
        ax.grid(True, alpha=0.3)

        # Equal aspect ratio for accurate visualization
        ax.set_aspect("equal", adjustable="box")

       # Add summary text (distances in PLT units, convert to inches for display)
        cutting_dist = document.cutting_distance() * PLT_UNITS_TO_INCHES
        # Use provided rapid_travel_inches if available, otherwise calculate from document
        rapid_dist = rapid_travel_inches if rapid_travel_inches is not None else (
            document.rapid_distance() * PLT_UNITS_TO_INCHES
        )

        summary_text = (
            f"Total Segments: {len(all_segments)}\n"
            f"Cutting Distance: {cutting_dist:,.2f} in\n"
            f"Rapid Travel: {rapid_dist:,.2f} in"
        )
        ax.text(
            0.02,
            0.98,
            summary_text,
            transform=ax.transAxes,
            verticalalignment="top",
            fontsize=9,
            bbox=dict(boxstyle="round", facecolor="wheat", alpha=0.5),
        )

        plt.tight_layout()

        # Save if output path provided
        if output_path is not None:
            save_figure(fig, output_path)

        if show_plot:
            plt.show()

        return fig

    except Exception as e:
        raise PlotterError(f"Plotting failed: {e}") from e


def plot_stroke_path(
    path: StrokePath,
    output_path: Path | None = None,
    title: str = "Stroke Path",
    show_plot: bool = False,
) -> plt.Figure:
    """Plot a single stroke path.

    Args:
        path: The stroke path to visualize.
        output_path: Optional path to save the figure.
        title: Title for the plot.
        show_plot: If True, display interactively.

    Returns:
        The matplotlib Figure object.
    """
    # Create minimal document and delegate
    doc = PLTDocument(stroke_paths=[path])
    return plot_plt_document(
        doc,
        output_path=output_path,
        title=title,
        show_plot=show_plot,
    )


def save_figure(fig: plt.Figure, path: Path) -> None:
    """Save a matplotlib figure to file.

    Args:
        fig: The figure to save.
        path: Destination path. Format inferred from extension (.png, .pdf, .svg).

    Raises:
        PlotterError: If saving fails.
    """
    try:
        # Ensure parent directory exists
        path.parent.mkdir(parents=True, exist_ok=True)

        # Infer format from extension
        ext = path.suffix.lower()
        if ext == ".pdf":
            fig.savefig(path, format="pdf", bbox_inches="tight")
        elif ext in (".svg", ".svgz"):
            fig.savefig(path, format="svg", bbox_inches="tight")
        else:
            # Default to PNG with high DPI
            fig.savefig(path, format="png", dpi=150, bbox_inches="tight")

    except Exception as e:
        raise PlotterError(f"Failed to save figure: {e}") from e


def create_path_diagram(
    coordinates: Sequence[Coordinate],
    cutting_mask: Sequence[bool],
    output_path: Path | None = None,
    title: str = "Toolpath Diagram",
) -> plt.Figure:
    """Create a simple path diagram without full document parsing.

    This is a convenience function for quickly visualizing coordinate sequences
    without building a complete PLTDocument.

    Args:
        coordinates: Sequence of (x, y) coordinates.
        cutting_mask: Boolean sequence indicating if each segment is cutting.
        output_path: Optional path to save the figure.
        title: Title for the plot.

    Returns:
        The matplotlib Figure object.
    """
    if len(coordinates) < 2:
        raise PlotterError("Need at least 2 coordinates to plot")

    if len(cutting_mask) != len(coordinates) - 1:
        raise PlotterError(
            f"cutting_mask length ({len(cutting_mask)}) must match "
            f"coordinates length ({len(coordinates)}) minus 1"
        )

    fig, ax = plt.subplots(figsize=DEFAULT_FIGURE_SIZE)

    # Extract x, y for all points (flip y for display)
    xs = [c.x for c in coordinates]
    ys = [_flip_y(c.y) for c in coordinates]

    # Calculate cumulative distances
    cum_dist = [0.0]
    total = 0.0
    for i in range(1, len(coordinates)):
        dx = coordinates[i].x - coordinates[i - 1].x
        dy = coordinates[i].y - coordinates[i - 1].y
        total += (dx * dx + dy * dy) ** 0.5
        cum_dist.append(total)

    # Normalize for coloring
    max_d = cum_dist[-1] if cum_dist else 1.0
    norm_dist = [d / max_d if max_d > 0 else 0.0 for d in cum_dist]

    # Plot each segment
    cmap = plt.colormaps["plasma"]

    for i in range(len(coordinates) - 1):
        color_val = (norm_dist[i] + norm_dist[i + 1]) / 2
        is_cutting = cutting_mask[i]

        line_style = "-" if is_cutting else "--"
        color = cmap(color_val) if is_cutting else "lightgray"

        ax.plot(
            [coordinates[i].x, coordinates[i + 1].x],
            [_flip_y(coordinates[i].y), _flip_y(coordinates[i + 1].y)],
            color=color,
            linewidth=2 if is_cutting else 1,
            linestyle=line_style,
            alpha=0.9 if is_cutting else 0.5,
        )

    # Mark start/end
    ax.plot(xs[0], ys[0], "go", markersize=12, zorder=10, label="Start")
    ax.plot(xs[-1], ys[-1], "rs", markersize=12, zorder=10, label="End")

    ax.set_xlabel("X (inches)")
    ax.set_ylabel("Y (inches)")
    ax.set_title(title)
    ax.xaxis.set_major_locator(MultipleLocator(1))
    ax.yaxis.set_major_locator(MultipleLocator(1))
    ax.legend()
    ax.grid(True, alpha=0.3)
    ax.set_aspect("equal", adjustable="box")

    plt.tight_layout()

    if output_path is not None:
        save_figure(fig, output_path)

    return fig
