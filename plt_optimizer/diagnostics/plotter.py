"""Diagnostic plotting functionality for PLT-Optimizer.

This module provides visualization tools to render parsed HPGL/PLT data,
including color-coded path visualization based on cumulative distance traveled.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

import matplotlib

# Use 'Agg' (non-interactive) backend by default to avoid Tcl/Tk initialization issues
# in headless environments (e.g., PyInstaller frozen executables on Windows).
# This must be set before importing pyplot.
if getattr(sys, "frozen", False) or sys.platform == "win32":
    matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.patches import PathPatch
from matplotlib.ticker import FormatStrFormatter, MultipleLocator

from plt_optimizer.core.models import (
    ArcSegment,
    Coordinate,
    PLTDocument,
    Segment,
    StrokePath,
)
from plt_optimizer.core.units import HPGL_UNITS_PER_INCH
from plt_optimizer.diagnostics.stroke_geometry import StrokePolygon, expand_cutting_polygons
from plt_optimizer.utils.geometry import calculate_cumulative_distances

# One plotter unit is HPGL's default 0.025 mm step, i.e. 1/1016 inch (see
# plt_optimizer.core.units); multiply units by this for inches.
PLT_UNITS_TO_INCHES = 1 / HPGL_UNITS_PER_INCH


def _safe_range(lo: float, hi: float, default_floor: float = 1.0) -> float:
    """Compute axis range with sensible fallback for degenerate spans.

    Uses ``math.isclose`` to avoid direct floating-point equality per project
    standards (AGENTS.md §1). Falls back to 10% of the larger endpoint
    magnitude when both endpoints are within tolerance of each other but
    meaningfully non-zero. When both endpoints are within tolerance of zero,
    falls back to ``default_floor`` to keep the axis from collapsing.

    Args:
        lo: Lower bound (in inches).
        hi: Upper bound (in inches).
        default_floor: Range to return when both endpoints are within
            ``ZERO_TOL`` of zero. Defaults to 1.0 inch.

    Returns:
        A strictly positive range suitable for computing symmetric padding.
    """
    zero_tol = 1e-9
    if not math.isclose(lo, hi, rel_tol=0.0, abs_tol=zero_tol):
        return hi - lo
    magnitude = max(abs(lo), abs(hi))
    if math.isclose(magnitude, 0.0, rel_tol=0.0, abs_tol=zero_tol):
        return default_floor
    return magnitude * 0.1


def _arc_to_points(arc: ArcSegment, num_segments: int = 32) -> List[Coordinate]:
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

    points: List[Coordinate] = []
    for i in range(num_segments + 1):
        theta = theta_start + i * delta_theta
        x = arc.center.x + radius * math.cos(theta)
        y = arc.center.y + radius * math.sin(theta)
        points.append(Coordinate(x, y))

    return points


# Default figure size in inches (16:9 aspect ratio suitable for wide tables)
DEFAULT_FIGURE_SIZE = (16, 9)

# Tick spacing in inches: major ticks/gridlines every 1 inch, minor ticks
# every 0.5 inch (minor ticks are unlabeled by default).
TICK_INTERVAL_INCHES = 1.0
MINOR_TICK_INTERVAL_INCHES = 0.5

# Simple-mode stroke styling per toolpath kind. Purely structural toolpaths
# (borders + drill holes) keep the original thick, semi-transparent black
# strokes; text-only or mixed (text + borders + holes) toolpaths render
# thin, fully opaque strokes.
SIMPLE_STRUCTURAL_LINEWIDTH = 2.0
SIMPLE_STRUCTURAL_ALPHA = 0.3
SIMPLE_LINEWIDTH = 0.4
SIMPLE_ALPHA = 1.0

# Physical-stroke rendering: structural (border/hole) layers keep the
# semi-transparent underlay look of simple mode's structural strokes so they
# stay visually distinct from the text ink drawn at full opacity.
PHYSICAL_STRUCTURAL_ALPHA = SIMPLE_STRUCTURAL_ALPHA


def _polygon_exterior_bounds(
    polygons: Sequence[StrokePolygon],
) -> Tuple[float, float, float, float]:
    """Compute the (min_x, min_y, max_x, max_y) bounds of StrokePolygon rings.

    Args:
        polygons: StrokePolygon objects (inch-coordinate rings) from
            :func:`~plt_optimizer.diagnostics.stroke_geometry.expand_cutting_polygons`.

    Returns:
        The inclusive bounds in inches.

    Raises:
        PlotterError: If ``polygons`` is empty.
    """
    xs: List[float] = []
    ys: List[float] = []
    for polygon in polygons:
        for x, y in polygon.exterior:
            xs.append(x)
            ys.append(y)
    if not xs:
        raise PlotterError("Cannot compute bounds of an empty polygon set")
    return min(xs), min(ys), max(xs), max(ys)


@dataclass(frozen=True)
class PhysicalStrokeLayer:
    """One width-tagged stroke layer for :func:`plot_physical_strokes`.

    Attributes:
        segments: Ordered segments in plotter units (the layer's toolpath;
            rapid/pen-up segments are ignored by the expansion).
        width_inches: Physical stroke (cutter) diameter in inches.
        alpha: Fill opacity for the layer's polygons (1.0 = text ink, the
            semi-transparent :data:`PHYSICAL_STRUCTURAL_ALPHA` for border
            and hole underlays).
        label: Caller-facing layer description (e.g. ``"0.030 txt"``).
            Metadata only -- the clean physical render draws no legend.
        facecolor: Matplotlib color name of the engraved ink (default
            ``"black"``; the colored proof previews pass the resolved
            text/material color).
    """

    segments: Sequence[Segment]
    width_inches: float
    alpha: float = 1.0
    label: str = ""
    facecolor: str = "black"


@dataclass(frozen=True)
class PhysicalFillLayer:
    """One enclosed-area fill layer for :func:`plot_physical_strokes`.

    Fills render *beneath* every stroke layer, so a proof preview can show
    the material background (the region a label's closed border encloses)
    under the colored text ink.

    Attributes:
        polygons: Enclosed-area polygons in inch coordinates (see
            :func:`~plt_optimizer.diagnostics.stroke_geometry.closed_path_polygons`).
        color: Matplotlib color name of the fill.
        alpha: Fill opacity (1.0 = solid).
        label: Caller-facing layer description. Metadata only -- the clean
            render draws no legend.
    """

    polygons: Sequence[StrokePolygon]
    color: str
    alpha: float = 1.0
    label: str = ""


def _apply_inch_tick_layout(ax: Axes) -> None:
    """Configure fixed inch-based tick spacing on both axes.

    Major ticks (and the major gridlines drawn from them) are placed at
    whole-inch multiples via ``MultipleLocator`` and labeled as integers;
    minor ticks are placed at half-inch multiples and left unlabeled (the
    default ``NullFormatter``). This keeps every diagnostic plot on a
    predictable 1-inch drafting grid regardless of axis range, instead of
    the auto-scaled fractional intervals ``AutoLocator`` would pick.

    Args:
        ax: The matplotlib Axes to configure.
    """
    for axis in (ax.xaxis, ax.yaxis):
        axis.set_major_locator(MultipleLocator(TICK_INTERVAL_INCHES))
        axis.set_major_formatter(FormatStrFormatter("%.0f"))
        axis.set_minor_locator(MultipleLocator(MINOR_TICK_INTERVAL_INCHES))


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
    rapid_travel_inches: Optional[float] = None,
    simple_mode: bool = False,
    is_structural: bool = False,
    gridlines: bool = True,
    show_axes: bool = True,
    show_title: bool = True,
) -> Figure:
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
        simple_mode: If True, render only tool-down segments in thick black lines,
            omitting all rapid travel (tool-up) visualization. Useful for clean
            outlines of the actual cutting toolpath.
        is_structural: If True, the document carries a purely structural
            toolpath (borders + drill holes); simple-mode strokes render thick
            and semi-transparent (``linewidth=2.0``, ``alpha=0.3``). If False
            (the default), simple-mode strokes render thin and fully opaque
            (``linewidth=1.0``, ``alpha=1.0``). Text-only and mixed
            (text + borders + holes) documents use the default. Has no effect
            outside simple mode.
        gridlines: If True (the default), draw the major (1 in) and minor
            (0.5 in) drafting gridlines. If False, the fixed inch-based tick
            layout is kept (ticks and labels are unchanged) while both grid
            layers are suppressed, giving a clean background for exports.
        show_axes: If True (the default), draw the axis chrome: inch axis
            labels, the fixed inch-based tick layout, the legend, and the
            frame spines. If False, both axes are hidden (no ticks, tick
            labels, or axis labels) and all four spines are removed, leaving
            only the drawing (plus title/gridlines per their own flags) --
            the clean-render mode the font showcases use.
        show_title: If True (the default), set ``title`` as the figure title.
            If False, no title is drawn (the chrome-free showcase mode wants
            nothing but the engraved geometry).

    Returns:
        The matplotlib Figure object.

    Raises:
        PlotterError: If plotting fails.
    """
    try:
        fig, ax = plt.subplots(figsize=figure_size)

        all_segments: List[Segment] = []
        segment_is_cutting: List[bool] = []

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

        # Calculate axis limits from all segments with 10% padding (in inches)
        if all_segments:
            all_x = [seg.start.x * PLT_UNITS_TO_INCHES for seg in all_segments] + [
                seg.end.x * PLT_UNITS_TO_INCHES for seg in all_segments
            ]
            all_y = [seg.start.y * PLT_UNITS_TO_INCHES for seg in all_segments] + [
                seg.end.y * PLT_UNITS_TO_INCHES for seg in all_segments
            ]
            x_min, x_max = min(all_x), max(all_x)
            y_min, y_max = min(all_y), max(all_y)

            # Add 10% padding (5% on each side)
            x_range = _safe_range(x_min, x_max)
            y_range = _safe_range(y_min, y_max)
            padding_x = x_range * 0.1
            padding_y = y_range * 0.1

            ax.set_xlim(x_min - padding_x, x_max + padding_x)
            # Invert y-axis so y increases downward (screen/display convention)
            # With y_max at bottom and y_min at top, y+ points down
            ax.set_ylim(y_max + padding_y, y_min - padding_y)

        # Plot each segment individually for better visibility and axis handling
        # First, plot all rapid moves (dotted gray lines) unless in simple_mode
        if not simple_mode:
            for i, seg in enumerate(all_segments):
                if not seg.is_cutting:  # Rapid travel
                    if isinstance(seg, ArcSegment):
                        arc_points = _arc_to_points(seg)
                        xs = [p.x * PLT_UNITS_TO_INCHES for p in arc_points]
                        ys = [p.y * PLT_UNITS_TO_INCHES for p in arc_points]
                        ax.plot(
                            xs,
                            ys,
                            color="gray",
                            linewidth=0.2,
                            linestyle="dotted",
                            alpha=0.7,
                            label="Rapid Travel (PU)" if i == 0 else "",
                        )
                    else:
                        ax.plot(
                            [seg.start.x * PLT_UNITS_TO_INCHES, seg.end.x * PLT_UNITS_TO_INCHES],
                            [
                                seg.start.y * PLT_UNITS_TO_INCHES,
                                seg.end.y * PLT_UNITS_TO_INCHES,
                            ],
                            color="gray",
                            linewidth=0.15,
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
                start_y = last_seg.end.y * PLT_UNITS_TO_INCHES
                end_x = next_path.pen_up_position.x * PLT_UNITS_TO_INCHES
                end_y = next_path.pen_up_position.y * PLT_UNITS_TO_INCHES

                ax.plot(
                    [start_x, end_x],
                    [start_y, end_y],
                    color="gray",
                    linewidth=0.15,
                    linestyle="dashed",
                    alpha=0.7,
                    label="Rapid Travel (PU)" if i == 0 else "",
                )

        # Then, plot all cutting moves with plasma colormap or black in simple_mode
        for i, seg in enumerate(all_segments):
            if seg.is_cutting:  # Cutting
                if simple_mode:
                    color: Any = "black"
                    if is_structural:
                        linewidth = SIMPLE_STRUCTURAL_LINEWIDTH
                        alpha = SIMPLE_STRUCTURAL_ALPHA
                    else:
                        linewidth = SIMPLE_LINEWIDTH
                        alpha = SIMPLE_ALPHA
                else:
                    color_val = norm_distances[i]
                    cmap = plt.colormaps["plasma"]
                    color = cmap(color_val)
                    linewidth = 0.3
                    alpha = 0.9

                if isinstance(seg, ArcSegment):
                    arc_points = _arc_to_points(seg)
                    xs = [p.x * PLT_UNITS_TO_INCHES for p in arc_points]
                    ys = [p.y * PLT_UNITS_TO_INCHES for p in arc_points]
                    ax.plot(
                        xs,
                        ys,
                        color=color,
                        linewidth=linewidth,
                        alpha=alpha,
                        label="Cutting Path" if i == 2 and not simple_mode else "",
                    )
                else:
                    ax.plot(
                        [seg.start.x * PLT_UNITS_TO_INCHES, seg.end.x * PLT_UNITS_TO_INCHES],
                        [seg.start.y * PLT_UNITS_TO_INCHES, seg.end.y * PLT_UNITS_TO_INCHES],
                        color=color,
                        linewidth=linewidth,
                        alpha=alpha,
                        label="Cutting Path" if i == 2 and not simple_mode else "",
                    )

        # Add colorbar for cutting paths (skip in simple_mode)
        if not simple_mode and any(seg.is_cutting for seg in all_segments):
            # Create a dummy plot to create colorbar
            cbar = plt.colorbar(plt.cm.ScalarMappable(cmap=plt.cm.plasma), ax=ax, shrink=0.8)
            cbar.set_label("Cumulative Distance (%)", rotation=270, labelpad=15)

        # Mark start and end points
        first_seg = all_segments[0]
        last_seg = all_segments[-1]

        if not simple_mode:
            ax.plot(
                first_seg.start.x * PLT_UNITS_TO_INCHES,
                first_seg.start.y * PLT_UNITS_TO_INCHES,
                marker="o",
                markersize=6,
                color="green",
                zorder=10,
                label="Start",
            )
            ax.plot(
                last_seg.end.x * PLT_UNITS_TO_INCHES,
                last_seg.end.y * PLT_UNITS_TO_INCHES,
                marker="s",
                markersize=6,
                color="red",
                zorder=10,
                label="End",
            )

        # Configure axes
        if show_title:
            ax.set_title(title)
        if show_axes:
            ax.set_xlabel("X (inches)")
            ax.set_ylabel("Y (inches)")
            # Fixed drafting grid: major ticks/gridlines every inch, minor every 0.5in
            _apply_inch_tick_layout(ax)
            ax.legend(loc="upper right")
        else:
            # Clean-render mode: no ticks, tick labels, axis labels, legend,
            # or frame -- just the drawing inside its padded limits.
            ax.get_xaxis().set_visible(False)
            ax.get_yaxis().set_visible(False)
            for spine in ax.spines.values():
                spine.set_visible(False)
        if gridlines:
            ax.grid(True, alpha=0.3)
            ax.grid(which="minor", alpha=0.15)

        # Equal aspect ratio for accurate visualization
        ax.set_aspect("equal", adjustable="box")

        # Add summary text (distances in PLT units, convert to inches for display)
        cutting_dist = document.cutting_distance() * PLT_UNITS_TO_INCHES
        # Use provided rapid_travel_inches if available, otherwise calculate from document
        rapid_dist = (
            rapid_travel_inches
            if rapid_travel_inches is not None
            else (document.rapid_distance() * PLT_UNITS_TO_INCHES)
        )

        if not simple_mode:
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
                bbox={"boxstyle": "round", "facecolor": "wheat", "alpha": 0.5},
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
) -> Figure:
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


def plot_physical_strokes(
    layers: Sequence[PhysicalStrokeLayer],
    output_path: Optional[Path] = None,
    title: str = "Physical Stroke Widths",
    show_plot: bool = False,
    figure_size: Tuple[float, float] = DEFAULT_FIGURE_SIZE,
    gridlines: bool = True,
    show_axes: bool = True,
    show_title: bool = True,
    fills: Sequence[PhysicalFillLayer] = (),
) -> Figure:
    """Plot toolpath strokes at their true engraved (cutter) widths.

    Every layer's cutting segments are expanded into constant-width
    polygons -- the physical ink corridor a cutter of ``width_inches``
    engraves -- and filled in the layer's ``facecolor`` (black by default),
    so each stroke measures its real cutter diameter on the inch axes at any
    zoom or export scale (unlike ``simple_mode`` centerline strokes, whose
    linewidth in points is display-only). Rapid travel, colorbar, start/end
    markers, and the summary box are omitted; the axes keep the fixed inch
    drafting grid.

    Args:
        layers: Width-tagged stroke layers to render (see
            :class:`PhysicalStrokeLayer`). Layers with no cutting geometry
            or a non-positive width contribute nothing.
        output_path: Optional path to save the figure (format inferred
            from the extension, see :func:`save_figure`).
        title: Title for the plot window/figure.
        show_plot: If True, call plt.show() to display interactively.
        figure_size: Figure dimensions in inches as (width, height).
        gridlines: If True (the default), draw the major (1 in) and minor
            (0.5 in) drafting gridlines.
        show_axes: If True (the default), draw the axis chrome (inch axis
            labels, fixed inch-based ticks, frame). If False, hide all
            ticks, labels, and spines -- the clean-render mode.
        show_title: If True (the default), set ``title`` as the plot title.
        fills: Enclosed-area background fills (see
            :class:`PhysicalFillLayer`) drawn *beneath* every stroke layer --
            the colored proof previews pass the material background under the
            text ink. Fills with no polygons contribute nothing.

    Returns:
        The matplotlib Figure object.

    Raises:
        PlotterError: If plotting fails.
    """
    try:
        fig, ax = plt.subplots(figsize=figure_size)

        rendered: List[Tuple[PhysicalStrokeLayer, List[StrokePolygon]]] = []
        for layer in layers:
            polygons = expand_cutting_polygons(layer.segments, layer.width_inches)
            if polygons:
                rendered.append((layer, polygons))

        rendered_fills = [fill for fill in fills if fill.polygons]

        if not rendered and not rendered_fills:
            ax.text(
                0.5,
                0.5,
                "No strokes to plot",
                ha="center",
                va="center",
                transform=ax.transAxes,
                fontsize=14,
            )
            return fig

        # Background fills first (z-order under the ink): each ring becomes
        # a MOVETO/LINETO subpath; the compound path's even-odd fill renders
        # interior rings as holes.
        for fill in rendered_fills:
            for polygon in fill.polygons:
                rings = (polygon.exterior, *polygon.interiors)
                path = matplotlib.path.Path.make_compound_path(
                    *(
                        matplotlib.path.Path(
                            ring,
                            [matplotlib.path.Path.MOVETO]
                            + [matplotlib.path.Path.LINETO] * (len(ring) - 1),
                        )
                        for ring in rings
                    )
                )
                ax.add_patch(
                    PathPatch(
                        path,
                        facecolor=fill.color,
                        edgecolor="none",
                        alpha=fill.alpha,
                    )
                )

        # Fill each layer's expanded polygons as true-width ink silhouettes.
        # Each ring becomes a MOVETO/LINETO subpath; the compound path's
        # even-odd fill renders interior rings as holes.
        for layer, polygons in rendered:
            for polygon in polygons:
                rings = (polygon.exterior, *polygon.interiors)
                path = matplotlib.path.Path.make_compound_path(
                    *(
                        matplotlib.path.Path(
                            ring,
                            [matplotlib.path.Path.MOVETO]
                            + [matplotlib.path.Path.LINETO] * (len(ring) - 1),
                        )
                        for ring in rings
                    )
                )
                ax.add_patch(
                    PathPatch(
                        path,
                        facecolor=layer.facecolor,
                        edgecolor="none",
                        alpha=layer.alpha,
                    )
                )

        # Axis limits from the polygon bounds (the ink envelope, half the
        # cutter width wider than the centerlines) with 10% padding, y
        # inverted to the device convention (+y down) like plot_plt_document.
        all_x: List[float] = []
        all_y: List[float] = []
        for _, polygons in rendered:
            min_x, min_y, max_x, max_y = _polygon_exterior_bounds(polygons)
            all_x.extend([min_x, max_x])
            all_y.extend([min_y, max_y])
        for fill in rendered_fills:
            min_x, min_y, max_x, max_y = _polygon_exterior_bounds(list(fill.polygons))
            all_x.extend([min_x, max_x])
            all_y.extend([min_y, max_y])
        x_min, x_max = min(all_x), max(all_x)
        y_min, y_max = min(all_y), max(all_y)
        padding_x = _safe_range(x_min, x_max) * 0.1
        padding_y = _safe_range(y_min, y_max) * 0.1
        ax.set_xlim(x_min - padding_x, x_max + padding_x)
        ax.set_ylim(y_max + padding_y, y_min - padding_y)

        if show_title:
            ax.set_title(title)
        if show_axes:
            ax.set_xlabel("X (inches)")
            ax.set_ylabel("Y (inches)")
            _apply_inch_tick_layout(ax)
        else:
            ax.get_xaxis().set_visible(False)
            ax.get_yaxis().set_visible(False)
            for spine in ax.spines.values():
                spine.set_visible(False)
        if gridlines:
            ax.grid(True, alpha=0.3)
            ax.grid(which="minor", alpha=0.15)

        ax.set_aspect("equal", adjustable="box")

        plt.tight_layout()

        if output_path is not None:
            save_figure(fig, output_path)

        if show_plot:
            plt.show()

        return fig

    except Exception as e:
        raise PlotterError(f"Plotting failed: {e}") from e


def save_figure(fig: Figure, path: Path) -> None:
    """Save a matplotlib figure to file.

    Args:
        fig: The figure to save.
        path: Destination path. Format inferred from extension (.png, .pdf,
            .svg/.svgz, .jpg/.jpeg).

    Raises:
        PlotterError: If saving fails.
    """
    try:
        # On Windows, paths like "/nonexistent/..." are silently converted to
        # drive-relative paths (e.g. "\nonexistent\...") which can cause
        # mkdir to succeed when it should fail. Detect this case and raise
        # an error before attempting to save.
        path_str = str(path)
        looks_rooted = path_str.startswith("/") or path_str.startswith("\\")
        is_truly_absolute = path.is_absolute()
        if looks_rooted and not is_truly_absolute:
            # The path looks absolute but isn't truly absolute on this platform.
            if not path.parent.exists():
                raise OSError(
                    f"Cannot save figure to '{path}': "
                    f"parent directory '{path.parent}' does not exist "
                    f"and the path is not absolute on this platform."
                )

        # Ensure parent directory exists
        path.parent.mkdir(parents=True, exist_ok=True)

        # Infer format from extension
        ext = path.suffix.lower()
        if ext == ".pdf":
            fig.savefig(path, format="pdf", bbox_inches="tight")
        elif ext in (".svg", ".svgz"):
            fig.savefig(path, format="svg", bbox_inches="tight")
        elif ext in (".jpg", ".jpeg"):
            # JPEG has no alpha channel: flatten onto white so the raster is
            # never a black-filled surprise on dark-mode viewers.
            fig.savefig(
                path,
                format="jpg",
                dpi=200,
                facecolor="white",
                bbox_inches="tight",
                pil_kwargs={"quality": 92},
            )
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
) -> Figure:
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
            [coordinates[i].y, coordinates[i + 1].y],
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
    # Fixed drafting grid: major ticks/gridlines every inch, minor every 0.5in
    _apply_inch_tick_layout(ax)
    ax.legend()
    ax.grid(True, alpha=0.3)
    ax.grid(which="minor", alpha=0.15)
    ax.set_aspect("equal", adjustable="box")

    plt.tight_layout()

    if output_path is not None:
        save_figure(fig, output_path)

    return fig
