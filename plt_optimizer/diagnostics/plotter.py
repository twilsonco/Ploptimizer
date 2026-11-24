"""Diagnostic plotting functionality for PLT-Optimizer.

This module provides visualization tools to render parsed HPGL/PLT data,
including color-coded path visualization based on cumulative distance traveled.
"""

from __future__ import annotations

import io
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Protocol, Tuple, cast

import matplotlib
import matplotlib.path as matplotlib_path
from PIL import Image

# Use 'Agg' (non-interactive) backend by default to avoid Tcl/Tk initialization issues
# in headless environments (e.g., PyInstaller frozen executables on Windows).
# This must be set before importing pyplot.
if getattr(sys, "frozen", False) or sys.platform == "win32":
    matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import PathPatch, Rectangle
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

# Default raster resolution of :func:`figure_to_image` (dots per inch), the
# screen figure's dpi ignored. 150 matches ``save_figure``'s PNG export.
RASTER_DPI = 150

#: Long side (inches) of a composite-proof panel figure, i.e. the size
#: :func:`proof_figure_size` keeps while it adjusts the short side so the drawn
#: toolpath lands at its own aspect ratio. Matches :data:`DEFAULT_FIGURE_SIZE`'s
#: long side, so a panel is no coarser than the proof PDFs beside it.
PROOF_PANEL_FIGURE_LONG_SIDE_IN = 16.0


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


def _show_figure(fig: Figure) -> None:
    """Display ``fig`` interactively, but only when the backend can.

    ``plt.show()`` on a non-interactive canvas (Agg, pdf, svg, ps -- every
    backend whose ``required_interactive_framework`` is ``None``) cannot open a
    window: matplotlib raises ``NonGuiException`` internally and converts it to
    a ``UserWarning`` on every platform except headless Linux. Headless renders
    (the watch/optimize plots, the generate pipeline previews, the whole test
    suite) therefore ask for a display that cannot exist, so the show is
    skipped. GUI backends (``macosx``, ``TkAgg``, ``QtAgg``, ...) report the
    framework they need and show normally.

    Args:
        fig: The figure the caller asked to display.
    """
    # getattr default keeps this working on matplotlib versions predating the
    # attribute; a missing attribute means "no interactive framework".
    if getattr(fig.canvas, "required_interactive_framework", None) is not None:
        plt.show()


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
        show_plot: If True, display the figure interactively via ``plt.show()``.
            On a non-interactive backend (Agg, pdf, svg, ps) nothing can be
            displayed, so the show is skipped rather than warning.
        figure_size: Figure dimensions in inches as (width, height).
        rapid_travel_inches: Optional pre-calculated rapid travel distance in inches.
            If provided, uses this value in the summary text box instead of
            computing it from document.rapid_distance(). This ensures consistency
            with any external calculation passed via title string.
        simple_mode: If True, render only tool-down segments in thick black lines,
            omitting all rapid travel (tool-up) visualization. Useful for clean
            outlines of the actual cutting toolpath. No artist carries a legend
            label in this mode, so no legend is drawn.
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
            labels, the fixed inch-based tick layout, the legend (when any
            artist carries a label), and the frame spines. If False, both axes
            are hidden (no ticks, tick labels, axis labels, or legend) and all
            four spines are removed, leaving only the drawing (plus
            title/gridlines per their own flags) -- the clean-render mode the
            font showcases use.
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
            ax.set_xlabel("Width (inches)")
            ax.set_ylabel("Height (inches)")
            # Fixed drafting grid: major ticks/gridlines every inch, minor every 0.5in
            _apply_inch_tick_layout(ax)
            # Only draw the legend when the axes carry labeled artists: simple
            # mode suppresses every label, and legend() with no labeled artists
            # warns and draws an empty frame.
            if ax.get_legend_handles_labels()[1]:
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
            _show_figure(fig)

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


class JobOverlayBoxLike(Protocol):
    """One label's provenance rectangle, as a caller wants it drawn on a proof.

    Structural (duck-typed) on purpose: the boxes are built by
    :func:`plt_optimizer.generate.jobset.job_overlay_boxes`, whose dataclass
    :class:`~plt_optimizer.generate.jobset.JobOverlayBox` satisfies this
    protocol. ``generate`` may not import ``diagnostics`` (matplotlib is
    import-guarded and absent on the Windows 7 target), and ``diagnostics``
    has no reason to know the jobset model - the geometry contract is all
    the two sides need to share.

    Attributes:
        x: Left edge in inches (plot/assembly space).
        y: Bottom edge in inches.
        width: Box width in inches.
        height: Box height in inches.
        label: Legend text for this box.
        color: Matplotlib colour token (e.g. ``"tab:blue"``).
        style: Matplotlib dash pattern (e.g. ``"--"``, ``"-."``, ``":"``).
        line_width: Edge width in points.
    """

    @property
    def x(self) -> float: ...

    @property
    def y(self) -> float: ...

    @property
    def width(self) -> float: ...

    @property
    def height(self) -> float: ...

    @property
    def label(self) -> str: ...

    @property
    def color(self) -> str: ...

    @property
    def style(self) -> str: ...

    @property
    def line_width(self) -> float: ...


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
    background_color: Optional[str] = None,
    overlay_boxes: Sequence[JobOverlayBoxLike] = (),
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
        show_plot: If True, display the figure interactively via ``plt.show()``.
            On a non-interactive backend (Agg, pdf, svg, ps) nothing can be
            displayed, so the show is skipped rather than warning.
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
        background_color: Optional page/axes background color (e.g. the
            proof previews' configured grey). ``None`` (the default) keeps
            matplotlib's white figure.
        overlay_boxes: Provenance rectangles drawn over the ink, one per
            LABEL and drawn inboard of each label's own border (see
            :func:`~plt_optimizer.generate.jobset.job_overlay_boxes`), each
            with a dashed coloured edge (see
            :class:`JobOverlayBoxLike`; a jobset plate proof passes a box for
            every label on the sheet, so each label reads as belonging to a
            job, and the legend collapses to one row per job). ``()`` (the
            default) draws nothing and adds no legend - single-job proofs are
            unchanged.

    Returns:
        The matplotlib Figure object.

    Raises:
        PlotterError: If plotting fails.
    """
    try:
        fig, ax = plt.subplots(figsize=figure_size)
        if background_color is not None:
            fig.patch.set_facecolor(background_color)
            ax.set_facecolor(background_color)

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
        # interior rings as holes (``matplotlib_path`` is the explicit
        # ``matplotlib.path`` import - the root package binds it as a side
        # effect only, which pyright cannot see).
        for fill in rendered_fills:
            for polygon in fill.polygons:
                rings = (polygon.exterior, *polygon.interiors)
                path = matplotlib_path.Path.make_compound_path(
                    *(
                        matplotlib_path.Path(
                            ring,
                            [matplotlib_path.Path.MOVETO]
                            + [matplotlib_path.Path.LINETO] * (len(ring) - 1),
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

        # Fill each layer's expanded polygons as true-width ink silhouettes.        # Each ring becomes a MOVETO/LINETO subpath; the compound path's
        # even-odd fill renders interior rings as holes.
        for layer, polygons in rendered:
            for polygon in polygons:
                rings = (polygon.exterior, *polygon.interiors)
                path = matplotlib_path.Path.make_compound_path(
                    *(
                        matplotlib_path.Path(
                            ring,
                            [matplotlib_path.Path.MOVETO]
                            + [matplotlib_path.Path.LINETO] * (len(ring) - 1),
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
        for box in overlay_boxes:
            all_x.extend([box.x, box.x + box.width])
            all_y.extend([box.y, box.y + box.height])
        x_min, x_max = min(all_x), max(all_x)
        y_min, y_max = min(all_y), max(all_y)
        padding_x = _safe_range(x_min, x_max) * 0.1
        padding_y = _safe_range(y_min, y_max) * 0.1
        ax.set_xlim(x_min - padding_x, x_max + padding_x)
        ax.set_ylim(y_max + padding_y, y_min - padding_y)

        if show_title:
            ax.set_title(title)
        if show_axes:
            ax.set_xlabel("Width (inches)")
            ax.set_ylabel("Height (inches)")
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

        # Per-job provenance overlay: a dashed coloured rectangle per LABEL
        # (boxes repeat a job's colour across its own labels), above the ink,
        # with one legend row per job. Drawn before the layout pass so the
        # legend claims its own space.
        if overlay_boxes:
            proxies = []
            keyed: set[str] = set()
            for box in overlay_boxes:
                ax.add_patch(
                    Rectangle(
                        (box.x, box.y),
                        box.width,
                        box.height,
                        fill=False,
                        linestyle=box.style,
                        edgecolor=box.color,
                        linewidth=box.line_width,
                        zorder=6,
                    )
                )
                # One legend row per job: the boxes are per-label, so a job
                # with a dozen labels on the sheet would otherwise print its
                # colour a dozen times.
                if box.label in keyed:
                    continue
                keyed.add(box.label)
                proxies.append(
                    Line2D(
                        [],
                        [],
                        color=box.color,
                        # A named dash pattern (see JOB_LEGEND_STYLES); the
                        # cast is for the stub's Literal union, not a runtime
                        # claim - matplotlib accepts the string verbatim.
                        linestyle=cast(Any, box.style),
                        label=box.label,
                    )
                )
            ax.legend(handles=proxies, loc="upper right", fontsize="small", framealpha=0.9)

        plt.tight_layout()

        if output_path is not None:
            save_figure(fig, output_path)

        if show_plot:
            _show_figure(fig)

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


def plot_data_aspect(fig: Figure) -> Optional[float]:
    """Aspect ratio (width / height) of the data a plot frames.

    Both plotters set the axis limits to the ink envelope plus 10 % padding on
    *each* side (see :func:`plot_physical_strokes`, :func:`plot_plt_document`),
    so the data view is the ink box scaled by 1.2 on both axes and the ratio of
    the two data spans **is** the aspect ratio of the drawn toolpath -- no
    geometry re-measurement needed. Because the axes draw at ``equal`` aspect,
    that ratio is what a proof panel must have to show the plate as the machine
    runs it, instead of the fixed :data:`DEFAULT_FIGURE_SIZE` frame (a 16:9
    figure turns a 7 x 16 in plate into a mostly-empty 1.78 panel).

    Args:
        fig: A rendered figure whose first axes carries the plot.

    Returns:
        ``data_width / data_height``, or ``None`` when the figure has no axes
        with a usable, non-degenerate data box (an empty/mock figure, a
        diagram with no geometry), so the caller keeps its default framing.
    """
    axes = getattr(fig, "axes", None)
    if not axes:
        return None
    try:
        x_min, x_max = axes[0].get_xlim()
        y_min, y_max = axes[0].get_ylim()
    except Exception:  # noqa: BLE001 - a stubbed figure has no real limits
        return None
    span_x = abs(float(x_max) - float(x_min))
    span_y = abs(float(y_max) - float(y_min))
    if not (math.isfinite(span_x) and math.isfinite(span_y)):
        return None
    if span_x <= 0.0 or span_y <= 0.0:
        return None
    return span_x / span_y


def proof_figure_size(
    data_aspect: float,
    *,
    title: str = "",
    show_axes: bool = True,
    long_side_inches: float = PROOF_PANEL_FIGURE_LONG_SIDE_IN,
) -> Tuple[float, float]:
    """Figure size (inches) that draws a ``data_aspect`` toolpath at its own shape.

    The proof panels of the composite plate proof are tiled by a collage, which
    lays images out by their aspect ratio: a panel carrying the empty margins of
    a fixed 16:9 figure makes the collage lay out those margins, not the plate.
    This sizes a figure so the **axes box** (the box the ink fills, after
    :func:`matplotlib.pyplot.tight_layout`) has exactly ``data_aspect``, with
    the plate's long side kept at ``long_side_inches``.

    The figure's chrome (title, inch tick labels, axis labels) is measured by
    laying out a square probe figure at the same aspect with the same chrome and
    comparing the tight bounding box with the axes box; font metrics are
    absolute (points), so the insets in inches do not depend on the figure size.

    Args:
        data_aspect: Desired axes (data) aspect ratio, width / height. Must be
            positive; clamped to ``[1/6, 6]`` so an extreme sliver still leaves
            the chrome room to render.
        title: The title the real plot draws (its height is chrome).
        show_axes: Whether the real plot draws axis chrome (ticks, tick labels,
            axis labels).
        long_side_inches: Length of the figure's long side.

    Returns:
        ``(width, height)`` in inches for ``figure_size``.
    """
    aspect = float(data_aspect)
    if not math.isfinite(aspect) or aspect <= 0.0:
        aspect = 1.0
    aspect = min(max(aspect, 1.0 / 6.0), 6.0)
    long_side = max(1.0, float(long_side_inches))

    left, bottom, right, top = _proof_chrome_inches(
        aspect, title=title, show_axes=show_axes, long_side_inches=long_side
    )
    inner_width = max(1e-3, long_side - left - right)
    inner_height = max(1e-3, long_side - bottom - top)
    if aspect >= 1.0:
        # Landscape: the width is the long side, the height buys the aspect.
        return (long_side, inner_width / aspect + bottom + top)
    return (inner_height * aspect + left + right, long_side)


def _proof_chrome_inches(
    data_aspect: float,
    *,
    title: str,
    show_axes: bool,
    long_side_inches: float,
) -> Tuple[float, float, float, float]:
    """Chrome insets (inches, left/bottom/right/top) around a proof figure's axes.

    Args:
        data_aspect: Data aspect ratio to lay the probe out at (so the tick
            label widths match the real plate).
        title: Title text drawn above the axes.
        show_axes: Draw axis labels and the inch tick layout.
        long_side_inches: Side of the square probe figure.

    Returns:
        ``(left, bottom, right, top)`` insets from the figure edge to the axes
        box, each clamped at 0 (a tight layout can pin the axes to the edge).
    """
    side = max(1.0, float(long_side_inches))
    fig, ax = plt.subplots(figsize=(side, side))
    try:
        width = max(data_aspect, 1.0)
        height = max(1.0 / data_aspect, 1.0)
        ax.set_xlim(0.0, width)
        # y inverted exactly like the plotters (device convention, +y down).
        ax.set_ylim(height, 0.0)
        ax.set_aspect("equal", adjustable="box")
        if show_axes:
            ax.set_xlabel("Width (inches)")
            ax.set_ylabel("Height (inches)")
            _apply_inch_tick_layout(ax)
        else:
            ax.get_xaxis().set_visible(False)
            ax.get_yaxis().set_visible(False)
            for spine in ax.spines.values():
                spine.set_visible(False)
        if title:
            ax.set_title(title)
        plt.tight_layout()
        # matplotlib ships no signatures for the canvas/renderer raster API, so
        # the strict-mode `no-untyped-call` gate rejects a typed binding; going
        # through Any keeps the gate quiet (root §1).
        canvas: Any = fig.canvas
        canvas.draw()
        renderer = canvas.get_renderer()
        tight = fig.get_tightbbox(renderer)
        # Both boxes in display pixels at the figure's dpi: the tight bounding
        # box of everything drawn, and the axes box the ink fills.
        axes_box = ax.get_window_extent(renderer)
        dpi = float(fig.dpi)
        return (
            max(0.0, (axes_box.x0 - tight.x0) / dpi),
            max(0.0, (axes_box.y0 - tight.y0) / dpi),
            max(0.0, (tight.x1 - axes_box.x1) / dpi),
            max(0.0, (tight.y1 - axes_box.y1) / dpi),
        )
    finally:
        plt.close(fig)


def _tight_crop_box(
    image_size: Tuple[int, int],
    tight_box: Tuple[int, int, int, int],
    crop_aspect: float,
) -> Tuple[int, int, int, int]:
    """PIL crop box (left, upper, right, lower) holding ``tight_box`` at ``crop_aspect``.

    Args:
        image_size: The full raster ``(width, height)``.
        tight_box: The content box the crop must keep whole.
        crop_aspect: Required width / height of the crop.

    Returns:
        The crop box, at (integer) ``crop_aspect``, containing ``tight_box`` and
        inside the raster: the box grows the short side about the content centre
        (so the plate stays centred), then slides inside the raster, then shrinks
        to fit a raster smaller than the content plus its aspect.
    """
    width, height = image_size
    left, upper, right, lower = tight_box
    box_width = max(1, right - left)
    box_height = max(1, lower - upper)
    # The smallest box at the target aspect that still holds the content.
    target_width = box_width
    target_height = max(1, int(round(target_width / crop_aspect)))
    if target_height < box_height:
        target_height = box_height
        target_width = max(1, int(round(target_height * crop_aspect)))
    center_x = (left + right) / 2.0
    center_y = (upper + lower) / 2.0
    # Shrink to fit a raster that cannot host the box, preserving the aspect.
    scale = min(1.0, width / max(1, target_width), height / max(1, target_height))
    target_width = max(1, int(target_width * scale))
    target_height = max(1, int(target_height * scale))
    crop_left = int(round(center_x - target_width / 2.0))
    crop_upper = int(round(center_y - target_height / 2.0))
    crop_left = min(max(0, crop_left), max(0, width - target_width))
    crop_upper = min(max(0, crop_upper), max(0, height - target_height))
    return (crop_left, crop_upper, crop_left + target_width, crop_upper + target_height)


def _tight_bbox_pixels(fig: Figure, dpi: int) -> Tuple[float, float, float, float]:
    """Figure tight bounding box in pixels of a raster rendered at ``dpi``.

    Matplotlib returns the tight bounding box in **figure units** (0..1 of the
    canvas, wrapped in a ``TransformedBbox``) -- treating the raw numbers as
    display pixels, as the docstring invites you to, yields a box 100x too small
    on Matplotlib 3.8+, which collapsed every cropped panel to a ~30 px
    thumbnail. The figure's own unit->inch transform is therefore applied
    explicitly, then scaled by ``dpi / fig.dpi`` because the canvas is at the
    figure's screen dpi when measured and the box crops a raster drawn at the
    export dpi. The box is inflated 1 % so sub-pixel rounding at the export dpi
    can never clip drawn content.

    Args:
        fig: The rendered figure.
        dpi: Dots per inch of the raster the box will crop.

    Returns:
        ``(x0, y0, x1, y1)`` in the raster's pixel space (bottom-up origin).

    Raises:
        PlotterError: If the measured box is degenerate, so the caller reports
            "no panel" instead of writing a broken one.
    """
    canvas: Any = fig.canvas
    if not hasattr(canvas, "get_renderer"):
        # A canvas-less figure (built with ``Figure()`` directly) or a
        # vector-only backend (``pdf``, ``svg``) has no raster renderer to
        # measure with. Attaching an Agg canvas is safe *here*: the warning
        # against replacing a canvas is about live GUI figures (Cocoa aborts),
        # and every interactive backend matplotlib ships derives from Agg, so
        # those take the fast path above and never reach this line.
        from matplotlib.backends.backend_agg import FigureCanvasAgg

        canvas = FigureCanvasAgg(fig)
    canvas.draw()
    try:
        renderer = canvas.get_renderer()
        tight_inches = fig.get_tightbbox(renderer).transformed(fig.dpi_scale_trans)
    except Exception as e:  # noqa: BLE001 - defensive against exotic backends
        raise PlotterError(f"Cannot measure the figure's content box: {e}") from e
    scale = float(dpi) / max(1.0, float(fig.get_dpi()))
    x0 = float(tight_inches.x0) * scale
    y0 = float(tight_inches.y0) * scale
    x1 = float(tight_inches.x1) * scale
    y1 = float(tight_inches.y1) * scale
    if x1 <= x0 or y1 <= y0:
        raise PlotterError(f"Figure content box is degenerate: {tight_inches!r}")
    grow_x = (x1 - x0) * 0.01
    grow_y = (y1 - y0) * 0.01
    return (x0 - grow_x, y0 - grow_y, x1 + grow_x, y1 + grow_y)


def figure_to_image(
    fig: Figure,
    dpi: int = RASTER_DPI,
    crop_aspect: Optional[float] = None,
    crop_pad_px: int = 8,
) -> Image.Image:
    """Rasterize a figure into an in-memory RGBA PIL image.

    The figure is drawn on **its own** canvas whenever that canvas can produce
    pixels: every interactive backend matplotlib ships (``macosx``, ``TkAgg``,
    ``QtAgg``, ``WXAgg``, the inline backends) derives from ``Agg`` and
    therefore carries the ``buffer_rgba`` raster API, so the headless render
    path works on a developer's GUI machine exactly as it does on the Windows
    plotter PC (where the module forces ``Agg``). Attaching a *second* canvas
    is deliberately avoided: replacing the canvas of a live GUI figure
    aborts the process on the Cocoa backend. A canvas with no pixel buffer (a
    vector-only backend such as ``pdf``/``svg``) falls back to an in-memory
    PNG round-trip, which every backend can write.

    The raster is rendered at the export ``dpi``, independent of the figure's
    screen dpi; the figure's own dpi is restored afterwards, so the caller's
    on-screen size is untouched. This is the rasterizer behind the generate
    pipeline's composite proof sheet (see
    :mod:`plt_optimizer.generate.proof_composite`).

    Args:
        fig: The figure to rasterize.
        dpi: Raster resolution in dots per inch, so the image measures
            ``fig.get_size_inches() * dpi`` pixels.
        crop_aspect: When set, crop the raster to the figure's **tight
            bounding box** (title, axis labels, ticks, legend, ink -- never a
            pixel of drawn content is cut) expanded about its centre to this
            width / height ratio. This is how a proof panel is made to carry the
            shape of the plate it shows instead of the figure's margins (see
            :func:`plot_data_aspect`, :func:`proof_figure_size`). ``None`` (the
            default) returns the whole figure, pixel-identical to before.
        crop_pad_px: Pixels of margin kept around the tight box when cropping,
            so antialiased edges and the outermost tick labels are never flush
            against the crop.

    Returns:
        The rendered image in RGBA mode.

    Raises:
        PlotterError: If rasterizing fails, or a ``crop_aspect`` is requested of
            a figure whose tight bounding box cannot be measured (a stubbed
            figure). The generate pipeline treats that as "this plate has no
            panel", never as a failed job.
    """
    try:
        # matplotlib ships no signatures for the canvas raster API, so the
        # strict-mode `no-untyped-call` gate rejects a typed binding; calling
        # through Any keeps the gate quiet (root §1).
        canvas: Any = fig.canvas
        image: Optional[Image.Image] = None
        if hasattr(canvas, "buffer_rgba"):
            screen_dpi = fig.get_dpi()
            fig.set_dpi(dpi)
            try:
                canvas.draw()
                width, height = canvas.get_width_height()
                image = Image.frombytes("RGBA", (width, height), bytes(canvas.buffer_rgba()))
            finally:
                fig.set_dpi(screen_dpi)

        if image is None:
            # Vector-only canvas: rasterize through an in-memory PNG.
            buffer = io.BytesIO()
            fig.savefig(buffer, format="png", dpi=dpi)
            buffer.seek(0)
            image = Image.open(buffer).convert("RGBA")

        if crop_aspect is None:
            return image

        # The tight box is measured in display pixels of the raster just drawn
        # (the figure is at the export dpi on both rasterization paths above).
        x0, y0, x1, y1 = _tight_bbox_pixels(fig, dpi)
        left = int(math.floor(x0)) - int(crop_pad_px)
        bottom = int(math.floor(y0)) - int(crop_pad_px)
        right = int(math.ceil(x1)) + int(crop_pad_px)
        top = int(math.ceil(y1)) + int(crop_pad_px)
        # Display coordinates are bottom-up; PIL boxes are top-down.
        tight_box = (
            max(0, left),
            max(0, image.height - top),
            min(image.width, right),
            min(image.height, image.height - bottom),
        )
        if tight_box[2] <= tight_box[0] or tight_box[3] <= tight_box[1]:
            raise PlotterError(f"Figure has no drawable content to crop (got {tight_box})")
        return image.crop(_tight_crop_box((image.width, image.height), tight_box, crop_aspect))
    except PlotterError:
        raise
    except Exception as e:
        raise PlotterError(f"Rasterizing figure failed: {e}") from e


def close_figure(fig: Figure) -> None:
    """Release a figure the caller is finished with.

    The generate pipeline renders one proof per plate (and, for a jobset, one
    per plate per job) inside a single process, and the pyplot state machine
    keeps every figure it created alive, so a proof loop that never closes its
    figures holds a whole jobset's worth of renders in RAM.

    Args:
        fig: The figure to dispose of. A figure pyplot no longer knows about is
            ignored.
    """
    plt.close(fig)


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

    ax.set_xlabel("Width (inches)")
    ax.set_ylabel("Height (inches)")
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
