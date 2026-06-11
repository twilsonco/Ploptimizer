"""Diagnostic plotting functionality for PLT-Optimizer.

This module provides visualization tools to render parsed HPGL/PLT data,
including color-coded path visualization based on cumulative distance traveled.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import matplotlib.pyplot as plt
import matplotlib.collections as mcoll
import numpy as np

from plt_optimizer.core.models import (
    Coordinate,
    PLTDocument,
    StrokePath,
    StrokeSegment,
)
from plt_optimizer.utils.geometry import calculate_cumulative_distances


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
    output_path: Optional[Path] = None,
    title: str = "PLT Toolpath Visualization",
    show_plot: bool = False,
    figure_size: Tuple[float, float] = DEFAULT_FIGURE_SIZE,
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

    Returns:
        The matplotlib Figure object.

    Raises:
        PlotterError: If plotting fails.
    """
    try:
        fig, ax = plt.subplots(figsize=figure_size)

        # Collect all segments and their properties
        all_segments: List[StrokeSegment] = []
        segment_is_cutting: List[bool] = []

        for path in document.stroke_paths:
            for seg in path.segments:
                all_segments.append(seg)
                segment_is_cutting.append(seg.is_cutting)

        if not all_segments:
            ax.text(
                0.5, 0.5,
                "No segments to plot",
                ha="center", va="center",
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
        norm_distances = [
            d / max_distance if max_distance > 0 else 0.0
            for d in cum_distances
        ]

        # Separate cutting and rapid segments
        cutting_lines: List[Tuple[float, float]] = []
        cutting_colors: List[float] = []
        rapid_lines: List[Tuple[float, float]] = []

        for i, (seg, is_cutting) in enumerate(
            zip(all_segments, segment_is_cutting)
        ):
            line_coords = [(seg.start.x, seg.start.y), (seg.end.x, seg.end.y)]
            if is_cutting:
                cutting_lines.append(line_coords)
                cutting_colors.append(norm_distances[i])
            else:
                rapid_lines.append(line_coords)

        # Plot rapid moves first (so they appear behind cutting lines)
        if rapid_lines:
            rapid_collection = mcoll.LineCollection(
                rapid_lines,
                colors="lightgray",
                linewidths=0.5,
                linestyles="dashed",
                alpha=0.6,
                label="Rapid Travel (PU)",
            )
            ax.add_collection(rapid_collection)

        # Plot cutting moves with plasma colormap
        if cutting_lines:
            cmap = plt.cm.get_cmap("plasma")
            cutting_collection = mcoll.LineCollection(
                cutting_lines,
                cmap=cmap,
                linewidths=1.5,
                alpha=0.9,
            )
            cutting_collection.set_array(np.array(cutting_colors))
            ax.add_collection(cutting_collection)

            # Add colorbar
            cbar = plt.colorbar(cutting_collection, ax=ax, shrink=0.8)
            cbar.set_label("Cumulative Distance (%)", rotation=270, labelpad=15)

        # Mark start and end points
        first_seg = all_segments[0]
        last_seg = all_segments[-1]

        ax.plot(
            first_seg.start.x, first_seg.start.y,
            marker="o", markersize=12, color="green",
            zorder=10, label="Start"
        )
        ax.plot(
            last_seg.end.x, last_seg.end.y,
            marker="s", markersize=12, color="red",
            zorder=10, label="End"
        )

        # Configure axes
        ax.set_xlabel("X (plotter units)")
        ax.set_ylabel("Y (plotter units)")
        ax.set_title(title)
        ax.legend(loc="upper right")
        ax.grid(True, alpha=0.3)

        # Equal aspect ratio for accurate visualization
        ax.set_aspect("equal", adjustable="box")

        # Add summary text
        cutting_dist = document.cutting_distance()
        rapid_dist = document.rapid_distance()

        summary_text = (
            f"Total Segments: {len(all_segments)}\n"
            f"Cutting Distance: {cutting_dist:,.2f}\n"
            f"Rapid Travel: {rapid_dist:,.2f}"
        )
        ax.text(
            0.02, 0.98, summary_text,
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
    output_path: Optional[Path] = None,
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
    output_path: Optional[Path] = None,
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

    # Extract x, y for all points
    xs = [c.x for c in coordinates]
    ys = [c.y for c in coordinates]

    # Calculate cumulative distances
    cum_dist = [0.0]
    total = 0.0
    for i in range(1, len(coordinates)):
        dx = coordinates[i].x - coordinates[i-1].x
        dy = coordinates[i].y - coordinates[i-1].y
        total += (dx*dx + dy*dy) ** 0.5
        cum_dist.append(total)

    # Normalize for coloring
    max_d = cum_dist[-1] if cum_dist else 1.0
    norm_dist = [d / max_d if max_d > 0 else 0.0 for d in cum_dist]

    # Plot each segment
    cmap = plt.cm.get_cmap("plasma")

    for i in range(len(coordinates) - 1):
        color_val = (norm_dist[i] + norm_dist[i+1]) / 2
        is_cutting = cutting_mask[i]

        line_style = "-" if is_cutting else "--"
        color = cmap(color_val) if is_cutting else "lightgray"

        ax.plot(
            [coordinates[i].x, coordinates[i+1].x],
            [coordinates[i].y, coordinates[i+1].y],
            color=color,
            linewidth=2 if is_cutting else 1,
            linestyle=line_style,
            alpha=0.9 if is_cutting else 0.5,
        )

    # Mark start/end
    ax.plot(xs[0], ys[0], "go", markersize=12, zorder=10, label="Start")
    ax.plot(xs[-1], ys[-1], "rs", markersize=12, zorder=10, label="End")

    ax.set_xlabel("X (plotter units)")
    ax.set_ylabel("Y (plotter units)")
    ax.set_title(title)
    ax.legend()
    ax.grid(True, alpha=0.3)
    ax.set_aspect("equal", adjustable="box")

    plt.tight_layout()

    if output_path is not None:
        save_figure(fig, output_path)

    return fig