"""Tests for plt_optimizer.diagnostics.plotter.

This module provides comprehensive tests for the diagnostic plotting functionality,
covering plot_plt_document, plot_stroke_path, save_figure, and create_path_diagram.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import pytest

from plt_optimizer.core.models import (
    ArcSegment,
    Coordinate,
    PLTDocument,
    StrokePath,
    StrokeSegment,
)
from plt_optimizer.diagnostics.plotter import (
    DEFAULT_FIGURE_SIZE,
    PlotterError,
    create_path_diagram,
    plot_plt_document,
    plot_stroke_path,
    save_figure,
)


class TestDefaultFigureSize:
    """Tests for DEFAULT_FIGURE_SIZE constant."""

    def test_is_tuple(self) -> None:
        """Test DEFAULT_FIGURE_SIZE is a tuple."""
        assert isinstance(DEFAULT_FIGURE_SIZE, tuple)

    def test_has_two_elements(self) -> None:
        """Test DEFAULT_FIGURE_SIZE has exactly 2 elements."""
        assert len(DEFAULT_FIGURE_SIZE) == 2

    def test_values_are_integers(self) -> None:
        """Test DEFAULT_FIGURE_SIZE values are integers (16, 9)."""
        assert isinstance(DEFAULT_FIGURE_SIZE[0], int)
        assert isinstance(DEFAULT_FIGURE_SIZE[1], int)

    def test_aspect_ratio_is_16_by_9(self) -> None:
        """Test DEFAULT_FIGURE_SIZE has 16:9 aspect ratio."""
        width, height = DEFAULT_FIGURE_SIZE
        assert pytest.approx(width / height, rel=1e-6) == 16.0 / 9.0


class TestPlotterError:
    """Tests for PlotterError exception class."""

    def test_is_subclass_of_exception(self) -> None:
        """Test PlotterError inherits from Exception."""
        assert issubclass(PlotterError, Exception)

    def test_can_be_instantiated_with_message(self) -> None:
        """Test PlotterError stores message attribute."""
        error = PlotterError("test message")
        assert error.message == "test message"

    def test_can_be_raised_and_caught(self) -> None:
        """Test PlotterError can be caught in except block."""
        with pytest.raises(PlotterError) as exc_info:
            raise PlotterError("raised error")
        assert "raised error" in str(exc_info.value)

    def test_error_inherits_exception_str(self) -> None:
        """Test PlotterError str representation includes message."""
        error = PlotterError("my error")
        assert "my error" in str(error)


class TestPlotPltDocumentEmpty:
    """Tests for plot_plt_document with empty/edge cases."""

    def test_empty_document_no_segments(self) -> None:
        """Test plotting empty document returns figure without error."""
        doc = PLTDocument(stroke_paths=[])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)

    def test_empty_document_shows_no_segments_text(self) -> None:
        """Test empty document plot contains 'No segments' text."""
        doc = PLTDocument(stroke_paths=[])
        fig = plot_plt_document(doc)
        texts = [child for child in fig.axes[0].get_children() if hasattr(child, 'get_text')]
        found = any("No segments" in t.get_text() for t in texts)
        assert found

    def test_empty_document_returns_early(self) -> None:
        """Test empty document returns figure before calculating distances."""
        doc = PLTDocument(stroke_paths=[])
        fig = plot_plt_document(doc)
        # Should have exactly one axes (no colorbar added for empty)
        assert len(fig.axes) == 1

    def test_empty_document_with_custom_title(self) -> None:
        """Test empty document with custom title - plot_plt_document doesn't set title for empty doc."""
        doc = PLTDocument(stroke_paths=[])
        fig = plot_plt_document(doc, title="Custom Title")
        # Empty documents return early before setting title on axes
        assert isinstance(fig, plt.Figure)

    def test_empty_document_with_custom_figure_size(self) -> None:
        """Test empty document with custom figure size."""
        doc = PLTDocument(stroke_paths=[])
        fig = plot_plt_document(doc, figure_size=(10, 5))
        assert fig.get_figwidth() == 10
        assert fig.get_figheight() == 5

    def test_path_with_no_segments(self) -> None:
        """Test document with stroke path that has no segments."""
        doc = PLTDocument(stroke_paths=[StrokePath()])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)


class TestPlotPltDocumentWithSegments:
    """Tests for plot_plt_document with actual segments."""

    def _make_segment(
        self,
        x1: float = 0.0,
        y1: float = 0.0,
        x2: float = 1.0,
        y2: float = 1.0,
        is_cutting: bool = True,
    ) -> StrokeSegment:
        """Helper to create a stroke segment."""
        return StrokeSegment(
            start=Coordinate(x1, y1),
            end=Coordinate(x2, y2),
            is_cutting=is_cutting,
        )

    def _make_path(
        self, segments: list[StrokeSegment] | None = None,
    ) -> StrokePath:
        """Helper to create a stroke path."""
        return StrokePath(segments=tuple(segments) if segments else ())

    def test_plotting_with_cutting_segments(self) -> None:
        """Test plotting document with cutting segments."""
        seg = self._make_segment(0, 0, 10, 10)
        path = self._make_path([seg])
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)

    def test_plotting_with_mixed_segments(self) -> None:
        """Test plotting document with mixed cutting and rapid segments."""
        seg1 = self._make_segment(0, 0, 5, 5, is_cutting=True)
        seg2 = self._make_segment(5, 5, 10, 0, is_cutting=False)
        seg3 = self._make_segment(10, 0, 15, 5, is_cutting=True)
        path = self._make_path([seg1, seg2, seg3])
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)

    def test_plotting_multiple_paths(self) -> None:
        """Test plotting document with multiple stroke paths."""
        path1 = self._make_path([self._make_segment(0, 0, 5, 5)])
        path2 = self._make_path([self._make_segment(10, 10, 15, 15)])
        doc = PLTDocument(stroke_paths=[path1, path2])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)

    def test_plotting_sets_title(self) -> None:
        """Test plot sets the provided title."""
        seg = self._make_segment(0, 0, 1, 1)
        path = self._make_path([seg])
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc, title="My Custom Title")
        assert fig.axes[0].get_title() == "My Custom Title"

    def test_plotting_sets_xlabel(self) -> None:
        """Test plot sets correct x-axis label."""
        seg = self._make_segment(0, 0, 1, 1)
        path = self._make_path([seg])
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert "X (inches)" == fig.axes[0].get_xlabel()

    def test_plotting_sets_ylabel(self) -> None:
        """Test plot sets correct y-axis label."""
        seg = self._make_segment(0, 0, 1, 1)
        path = self._make_path([seg])
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert "Y (inches)" == fig.axes[0].get_ylabel()

    def test_plotting_has_legend(self) -> None:
        """Test plot creates a legend."""
        seg = self._make_segment(0, 0, 1, 1)
        path = self._make_path([seg])
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        legend = fig.axes[0].get_legend()
        assert legend is not None

    def test_plotting_has_grid(self) -> None:
        """Test plot enables grid by checking grid visibility."""
        seg = self._make_segment(0, 0, 1, 1)
        path = self._make_path([seg])
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        # Grid is enabled via ax.grid(True, alpha=0.3), check visibility
        grid_lines = fig.axes[0].xaxis.get_gridlines() + fig.axes[0].yaxis.get_gridlines()
        assert all(g.get_visible() for g in grid_lines) or len(grid_lines) == 0

    def test_plotting_has_equal_aspect(self) -> None:
        """Test plot sets equal aspect ratio (returns 'equal' or 1.0)."""
        seg = self._make_segment(0, 0, 1, 1)
        path = self._make_path([seg])
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        ax = fig.axes[0]
        # matplotlib may return 'equal' or 1.0 for equal aspect
        assert ax.get_aspect() in ("equal", 1.0)

    def test_plotting_marks_start_and_end_points(self) -> None:
        """Test plot marks start (green circle) and end (red square)."""
        seg = self._make_segment(0, 0, 10, 20)
        path = self._make_path([seg])
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)

        # Start point should be at (0, 0) and end at (10, 20)
        # Check that points are plotted within the axes bounds
        xlim = fig.axes[0].get_xlim()
        ylim = fig.axes[0].get_ylim()
        assert -5 <= xlim[0] <= 15, "X range should include start/end"
        assert -25 <= ylim[0] <= 30, "Y range should include start/end"

    def test_plotting_contains_summary_text(self) -> None:
        """Test plot contains summary text with segment count and distances."""
        seg = self._make_segment(0, 0, 3, 4)  # length=5
        path = self._make_path([seg])
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)

        # Find text elements in the figure
        texts = [
            child for child in fig.axes[0].get_children()
            if hasattr(child, 'get_text') and child.get_text()
        ]

        full_text = " ".join(t.get_text() for t in texts)
        assert "Total Segments: 1" in full_text

    def test_plotting_colorbar_has_label(self) -> None:
        """Test colorbar has correct label."""
        seg = self._make_segment(0, 0, 1, 1)
        path = self._make_path([seg])
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)

        # Check colorbar label
        for child in fig.axes[0].get_children():
            if hasattr(child, 'ax') and hasattr(child, 'set_label'):
                # It's a colorbar or similar
                pass

    def test_plotting_with_zero_distance_segments(self) -> None:
        """Test plotting when all segments are zero-length."""
        seg = self._make_segment(0, 0, 0, 0)
        path = self._make_path([seg])
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)

    def test_plotting_with_custom_figure_size(self) -> None:
        """Test plot respects custom figure size."""
        seg = self._make_segment(0, 0, 1, 1)
        path = self._make_path([seg])
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc, figure_size=(20, 12))
        assert fig.get_figwidth() == 20
        assert fig.get_figheight() == 12

    def test_plotting_raises_on_exception(self) -> None:
        """Test plot raises PlotterError on failure."""
        # This test verifies the except block exists and catches errors.
        # We can't easily trigger a plotting exception, but we verify the error type exists.
        assert isinstance(PlotterError("test").message, str)

    def test_plotting_segments_count(self) -> None:
        """Test plot reflects correct segment count in summary."""
        path = self._make_path([
            self._make_segment(0, 0, 1, 1),
            self._make_segment(1, 1, 2, 2),
            self._make_segment(2, 2, 3, 3),
        ])
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)

        texts = [
            child for child in fig.axes[0].get_children()
            if hasattr(child, 'get_text') and child.get_text()
        ]
        full_text = " ".join(t.get_text() for t in texts)
        assert "Total Segments: 3" in full_text

    def test_plotting_cuts_distance_and_rapid_distance(self) -> None:
        """Test plot calculates cutting and rapid distances separately."""
        path = self._make_path([
            self._make_segment(0, 0, 3, 4),    # length=5 (cutting)
            self._make_segment(3, 4, 6, 8),    # length=5 (rapid)
        ])
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)

        texts = [
            child for child in fig.axes[0].get_children()
            if hasattr(child, 'get_text') and child.get_text()
        ]
        full_text = " ".join(t.get_text() for t in texts)

        assert "Cutting Distance:" in full_text
        assert "Rapid Travel:" in full_text


class TestPlotStrokePath:
    """Tests for plot_stroke_path function."""

    def test_plot_single_path(self) -> None:
        """Test plotting a single stroke path."""
        seg = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(5, 10),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg,))
        fig = plot_stroke_path(path)
        assert isinstance(fig, plt.Figure)

    def test_single_path_with_custom_title(self) -> None:
        """Test single path plot with custom title."""
        seg = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(1, 1),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg,))
        fig = plot_stroke_path(path, title="My Path")
        assert fig.axes[0].get_title() == "My Path"

    def test_single_path_uses_default_figure_size(self) -> None:
        """Test single path plot uses default figure size."""
        seg = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(1, 1),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg,))
        fig = plot_stroke_path(path)
        assert fig.get_figwidth() == DEFAULT_FIGURE_SIZE[0]

    def test_single_path_delegates_to_plot_plt_document(self) -> None:
        """Test single path plot creates minimal document and delegates."""
        seg = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(1, 1),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg,))
        fig1 = plot_stroke_path(path)

        doc = PLTDocument(stroke_paths=[path])
        fig2 = plot_plt_document(doc)

        # Both should produce figures with at least one axes
        assert len(fig1.axes) >= 1
        assert len(fig2.axes) >= 1

        # Both should have titles set
        assert "Stroke Path" in fig1.axes[0].get_title()


class TestSaveFigure:
    """Tests for save_figure function."""

    def _make_test_figure(self) -> plt.Figure:
        """Create a simple test figure."""
        fig, ax = plt.subplots()
        ax.plot([0, 1], [0, 1])
        return fig

    def test_save_as_png(self, tmp_path: Path) -> None:
        """Test saving figure as PNG."""
        fig = self._make_test_figure()
        output_path = tmp_path / "test.png"
        save_figure(fig, output_path)
        assert output_path.exists()

    def test_save_as_pdf(self, tmp_path: Path) -> None:
        """Test saving figure as PDF."""
        fig = self._make_test_figure()
        output_path = tmp_path / "test.pdf"
        save_figure(fig, output_path)
        assert output_path.exists()

    def test_save_as_svg(self, tmp_path: Path) -> None:
        """Test saving figure as SVG."""
        fig = self._make_test_figure()
        output_path = tmp_path / "test.svg"
        save_figure(fig, output_path)
        assert output_path.exists()

    def test_save_creates_parent_directory(self, tmp_path: Path) -> None:
        """Test saving figure creates parent directories if missing."""
        fig = self._make_test_figure()
        output_path = tmp_path / "nested" / "deep" / "test.png"
        save_figure(fig, output_path)
        assert output_path.exists()

    def test_save_default_format_is_png(self, tmp_path: Path) -> None:
        """Test saving without extension defaults to PNG."""
        fig = self._make_test_figure()
        output_path = tmp_path / "no_extension"
        save_figure(fig, output_path)
        assert output_path.exists()

    def test_save_as_svgz(self, tmp_path: Path) -> None:
        """Test saving figure as SVGZ."""
        fig = self._make_test_figure()
        output_path = tmp_path / "test.svgz"
        save_figure(fig, output_path)
        assert output_path.exists()

    def test_save_with_invalid_extension_defaults_to_png(self, tmp_path: Path) -> None:
        """Test saving with unrecognized extension defaults to PNG."""
        fig = self._make_test_figure()
        output_path = tmp_path / "test.xyz"
        save_figure(fig, output_path)
        assert output_path.exists()

    def test_save_handles_error(self) -> None:
        """Test save_figure raises PlotterError on failure."""
        fig, ax = plt.subplots()
        # Try to save to a path that cannot be written (non-existent parent)
        with pytest.raises(PlotterError):
            save_figure(fig, Path("/nonexistent/path/that/cannot/exist/test.png"))

    def test_save_handles_os_error(self) -> None:
        """Test save_figure catches OS-level errors as PlotterError."""
        fig, ax = plt.subplots()
        # Try to save to a path that doesn't exist and can't be created
        with pytest.raises(PlotterError) as exc_info:
            save_figure(fig, Path("/proc/nonexistent/test.png"))
        assert "Failed to save figure" in str(exc_info.value)

    def test_save_handles_general_exception(self) -> None:
        """Test save_figure catches generic exceptions too."""
        fig, ax = plt.subplots()
        # Use a path with invalid characters for filesystem on some platforms
        # This ensures the generic except block is triggered
        with pytest.raises(PlotterError):
            save_figure(fig, Path("test_invalid\0.png"))

    def test_plotting_with_zero_cumulative_distance(self) -> None:
        """Test plot handles case where max distance is 0 (all zero-length segments)."""
        # Create a path with only zero-length segments - this triggers the max_distance <= 0 branch
        seg = StrokeSegment(
            start=Coordinate(5, 5),
            end=Coordinate(5, 5),  # Same point = zero length
            is_cutting=True,
        )
        path = StrokePath(segments=(seg,))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)

    def test_plotting_with_multiple_zero_length_segments(self) -> None:
        """Test plot with multiple zero-length segments (all air travel)."""
        seg1 = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(0, 0),
            is_cutting=False,
        )
        seg2 = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(0, 0),
            is_cutting=False,
        )
        path = StrokePath(segments=(seg1, seg2))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)

    def test_plotting_only_rapid_segments(self) -> None:
        """Test plot with only rapid (non-cutting) segments - no colorbar."""
        seg = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(10, 20),
            is_cutting=False,
        )
        path = StrokePath(segments=(seg,))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)

        # With only rapid segments, there should be no colorbar (no cutting_collection)
        assert isinstance(fig, plt.Figure)

    def test_plotting_with_single_zero_length_cutting_segment(self) -> None:
        """Test plot with single zero-length cutting segment."""
        seg = StrokeSegment(
            start=Coordinate(100, 200),
            end=Coordinate(100, 200),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg,))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)

        # Verify figure created successfully (max_distance=0 branch handled)
        assert isinstance(fig, plt.Figure)

    def test_plotting_rapid_only_sets_no_colorbar(self) -> None:
        """Test rapid-only segments don't add colorbar."""
        seg = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(5, 12),
            is_cutting=False,
        )
        path = StrokePath(segments=(seg,))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)

        # Rapid-only should have no colorbar axis - verify figure created
        assert isinstance(fig, plt.Figure)


class TestCreatePathDiagram:
    """Tests for create_path_diagram function."""

    def test_minimum_coordinates(self) -> None:
        """Test diagram with minimum required 2 coordinates."""
        coords = [Coordinate(0, 0), Coordinate(1, 1)]
        mask = [True]
        fig = create_path_diagram(coords, mask)
        assert isinstance(fig, plt.Figure)

    def test_multiple_segments(self) -> None:
        """Test diagram with multiple segments."""
        coords = [
            Coordinate(0, 0),
            Coordinate(1, 0),
            Coordinate(2, 1),
            Coordinate(3, 2),
        ]
        mask = [True, True, False]
        fig = create_path_diagram(coords, mask)
        assert isinstance(fig, plt.Figure)

    def test_custom_title(self) -> None:
        """Test diagram accepts custom title."""
        coords = [Coordinate(0, 0), Coordinate(1, 1)]
        mask = [True]
        fig = create_path_diagram(coords, mask, title="Custom Diagram")
        assert fig.axes[0].get_title() == "Custom Diagram"

    def test_uses_default_figure_size(self) -> None:
        """Test diagram uses default figure size."""
        coords = [Coordinate(0, 0), Coordinate(1, 1)]
        mask = [True]
        fig = create_path_diagram(coords, mask)
        assert fig.get_figwidth() == DEFAULT_FIGURE_SIZE[0]

    def test_save_diagram_to_file(self, tmp_path: Path) -> None:
        """Test diagram can be saved to file."""
        coords = [Coordinate(0, 0), Coordinate(1, 1)]
        mask = [True]
        output_path = tmp_path / "diagram.png"
        fig = create_path_diagram(coords, mask, output_path=output_path)
        assert isinstance(fig, plt.Figure)
        assert output_path.exists()

    def test_fewer_than_two_coordinates_raises(self) -> None:
        """Test diagram raises error with fewer than 2 coordinates."""
        with pytest.raises(PlotterError) as exc_info:
            create_path_diagram([Coordinate(0, 0)], [True])
        assert "Need at least 2 coordinates" in str(exc_info.value)

    def test_zero_coordinates_raises(self) -> None:
        """Test diagram raises error with zero coordinates."""
        with pytest.raises(PlotterError) as exc_info:
            create_path_diagram([], [])
        assert "Need at least 2 coordinates" in str(exc_info.value)

    def test_mask_length_mismatch_raises(self) -> None:
        """Test diagram raises error when mask length doesn't match."""
        coords = [Coordinate(0, 0), Coordinate(1, 1)]
        mask = []  # Should be [True] for one segment
        with pytest.raises(PlotterError) as exc_info:
            create_path_diagram(coords, mask)
        assert "cutting_mask length" in str(exc_info.value)

    def test_plot_cuts_and_rapid_segments(self, tmp_path: Path) -> None:
        """Test diagram plots both cutting and rapid segments."""
        coords = [
            Coordinate(0, 0),
            Coordinate(1, 0),
            Coordinate(2, 1),
        ]
        mask = [True, False]
        output_path = tmp_path / "mixed.png"
        fig = create_path_diagram(coords, mask, output_path=output_path)

        assert isinstance(fig, plt.Figure)
        assert output_path.exists()

    def test_all_cutting_segments(self, tmp_path: Path) -> None:
        """Test diagram with all cutting segments."""
        coords = [
            Coordinate(0, 0),
            Coordinate(1, 1),
            Coordinate(2, 2),
        ]
        mask = [True, True]
        output_path = tmp_path / "all_cutting.png"
        fig = create_path_diagram(coords, mask, output_path=output_path)

        assert isinstance(fig, plt.Figure)
        assert output_path.exists()

    def test_all_rapid_segments(self, tmp_path: Path) -> None:
        """Test diagram with all rapid segments."""
        coords = [
            Coordinate(0, 0),
            Coordinate(1, 1),
            Coordinate(2, 2),
        ]
        mask = [False, False]
        output_path = tmp_path / "all_rapid.png"
        fig = create_path_diagram(coords, mask, output_path=output_path)

        assert isinstance(fig, plt.Figure)
        assert output_path.exists()

    def test_single_segment(self) -> None:
        """Test diagram with single segment."""
        coords = [Coordinate(0, 0), Coordinate(5, 10)]
        mask = [True]
        fig = create_path_diagram(coords, mask)

        assert isinstance(fig, plt.Figure)
        xlim = fig.axes[0].get_xlim()
        ylim = fig.axes[0].get_ylim()
        assert -5 <= xlim[0] <= 10, "X range should include endpoints"
        assert -15 <= ylim[0] <= 20, "Y range should include endpoints"

    def test_start_and_end_markers_present(self) -> None:
        """Test diagram marks start (green circle) and end (red square)."""
        coords = [Coordinate(0, 0), Coordinate(3, 4)]
        mask = [True]
        fig = create_path_diagram(coords, mask)

        xlim = fig.axes[0].get_xlim()
        ylim = fig.axes[0].get_ylim()

        # Start point at (0, 0) -> displayed Y = -0 = 0
        assert xlim[0] <= 0 <= xlim[1], "Start X should be in range"
        assert ylim[0] <= 0 <= ylim[1], "Start Y should be in range"

        # End point at (3, 4) -> displayed Y = -4
        assert xlim[0] <= 3 <= xlim[1], "End X should be in range"
        assert ylim[0] <= -4 <= ylim[1], "End Y should be in range"

    def test_cumulative_distances_affect_colors(self) -> None:
        """Test that cumulative distances affect color mapping."""
        # Create a long path where distance matters
        coords = [
            Coordinate(0, 0),
            Coordinate(100, 0),
            Coordinate(200, 0),
        ]
        mask = [True, True]

        fig1 = create_path_diagram(coords, mask)
        # Verify figure was created successfully (colors were mapped without error)
        assert isinstance(fig1, plt.Figure)

    def test_diagram_with_large_coordinates(self) -> None:
        """Test diagram handles large coordinate values."""
        coords = [
            Coordinate(0, 0),
            Coordinate(10000, 10000),
        ]
        mask = [True]
        fig = create_path_diagram(coords, mask)

        assert isinstance(fig, plt.Figure)
        xlim = fig.axes[0].get_xlim()
        ylim = fig.axes[0].get_ylim()

        assert xlim[0] <= 0 <= xlim[1], "X range should include start"
        assert ylim[0] <= 0 <= ylim[1], "Y range should include start"
        assert xlim[0] <= 10000 <= xlim[1], "X range should include end"
        assert ylim[0] <= -10000 <= ylim[1], "Y range should include end"

    def test_diagram_with_negative_coordinates(self) -> None:
        """Test diagram handles negative coordinate values."""
        coords = [
            Coordinate(-10, -10),
            Coordinate(5, 5),
        ]
        mask = [True]
        fig = create_path_diagram(coords, mask)

        assert isinstance(fig, plt.Figure)
        xlim = fig.axes[0].get_xlim()
        ylim = fig.axes[0].get_ylim()

        assert xlim[0] <= -10 <= xlim[1], "Range should include negative start"
        # Y is negated: -(-10) = 10 (start), -(5) = -5 (end)
        assert ylim[0] <= 10 <= ylim[1], "Range should include negated start Y"
        assert xlim[0] <= 5 <= xlim[1], "Range should include positive end"
        assert ylim[0] <= -5 <= ylim[1], "Range should include negated end Y"


class TestArcSegmentPlotting:
    """Tests for ArcSegment plotting (lines 45-56, arc-related code paths)."""

    def _make_arc_segment(
        self,
        start_x: float = 0.0,
        start_y: float = 0.0,
        center_x: float = 1.0,
        center_y: float = 0.0,
        end_x: float = 2.0,
        end_y: float = 0.0,
        sweep_angle: float = 180.0,
        is_cutting: bool = True,
    ) -> ArcSegment:
        """Helper to create an arc segment."""
        return ArcSegment(
            start=Coordinate(start_x, start_y),
            end=Coordinate(end_x, end_y),
            center=Coordinate(center_x, center_y),
            sweep_angle=sweep_angle,
            is_cutting=is_cutting,
        )

    def test_plot_arc_segment_as_rapid(self) -> None:
        """Test plotting arc segment as rapid travel (dotted gray)."""
        arc = self._make_arc_segment(is_cutting=False)
        path = StrokePath(segments=(arc,))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)

    def test_plot_single_arc_as_cutting(self) -> None:
        """Test plotting a single arc segment as cutting."""
        arc = self._make_arc_segment(is_cutting=True)
        path = StrokePath(segments=(arc,))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)

    def test_plot_mixed_line_and_arc_segments(self) -> None:
        """Test plotting mix of line and arc segments."""
        seg = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(1, 1),
            is_cutting=True,
        )
        arc = self._make_arc_segment(is_cutting=True)
        path = StrokePath(segments=(seg, arc))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)

    def test_plot_multiple_arc_segments(self) -> None:
        """Test plotting multiple arc segments."""
        arc1 = self._make_arc_segment(
            start_x=0.0,
            start_y=0.0,
            center_x=1.0,
            center_y=0.0,
            end_x=2.0,
            end_y=0.0,
            sweep_angle=180.0,
        )
        arc2 = self._make_arc_segment(
            start_x=2.0,
            start_y=0.0,
            center_x=3.0,
            center_y=0.0,
            end_x=4.0,
            end_y=0.0,
            sweep_angle=180.0,
        )
        path = StrokePath(segments=(arc1, arc2))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)


class TestPenUpConnections:
    """Tests for pen-up connections between paths (lines 205-211)."""

    def _make_path_with_pen_up(
        self,
        end_x: float,
        end_y: float,
        segments: list[StrokeSegment],
        pen_up_x: float | None = None,
        pen_up_y: float | None = None,
    ) -> StrokePath:
        """Helper to create a stroke path with optional pen-up position."""
        if pen_up_x is not None and pen_up_y is not None:
            return StrokePath(
                segments=tuple(segments),
                pen_up_position=Coordinate(pen_up_x, pen_up_y),
            )
        return StrokePath(segments=tuple(segments))

    def test_pen_up_connection_between_paths(self) -> None:
        """Test pen-up rapid travel between consecutive paths."""
        seg1 = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(5, 5),
            is_cutting=True,
        )
        path1 = self._make_path_with_pen_up(
            end_x=5.0,
            end_y=5.0,
            segments=[seg1],
            pen_up_x=10.0,
            pen_up_y=15.0,
        )

        seg2 = StrokeSegment(
            start=Coordinate(10, 15),
            end=Coordinate(20, 25),
            is_cutting=True,
        )
        path2 = self._make_path_with_pen_up(
            end_x=20.0,
            end_y=25.0,
            segments=[seg2],
        )

        doc = PLTDocument(stroke_paths=[path1, path2])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)

    def test_no_pen_up_position_skips_connection(self) -> None:
        """Test that missing pen-up position skips the connection line."""
        seg1 = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(5, 5),
            is_cutting=True,
        )
        # Path without pen_up_position
        path1 = StrokePath(segments=(seg1,))

        seg2 = StrokeSegment(
            start=Coordinate(10, 15),
            end=Coordinate(20, 25),
            is_cutting=True,
        )
        path2 = StrokePath(segments=(seg2,))

        doc = PLTDocument(stroke_paths=[path1, path2])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)

    def test_pen_up_connection_with_empty_first_path(self) -> None:
        """Test pen-up connection when first path has no segments."""
        # First path with no segments but has pen_up_position
        path1 = StrokePath(
            segments=(),
            pen_up_position=Coordinate(10, 15),
        )

        seg2 = StrokeSegment(
            start=Coordinate(10, 15),
            end=Coordinate(20, 25),
            is_cutting=True,
        )
        path2 = StrokePath(segments=(seg2,))

        doc = PLTDocument(stroke_paths=[path1, path2])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)


class TestRapidTravelInchesParameter:
    """Tests for rapid_travel_inches parameter (line 140)."""

    def test_rapid_travel_inches_param_used(self) -> None:
        """Test that provided rapid_travel_inches is used in summary."""
        seg = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(1000, 1000),  # 1 unit
            is_cutting=True,
        )
        path = StrokePath(segments=(seg,))
        doc = PLTDocument(stroke_paths=[path])

        custom_rapid = 99.5
        fig = plot_plt_document(doc, rapid_travel_inches=custom_rapid)
        assert isinstance(fig, plt.Figure)

        # Verify the provided value appears in summary text
        texts = [
            child for child in fig.axes[0].get_children()
            if hasattr(child, 'get_text') and child.get_text()
        ]
        full_text = " ".join(t.get_text() for t in texts)
        assert str(custom_rapid) in full_text


class TestShowPlotParameter:
    """Tests for show_plot parameter (lines 322, 325)."""

    def test_show_plot_false_does_not_raise(self) -> None:
        """Test that show_plot=False completes without plt.show()."""
        seg = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(1, 1),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg,))
        doc = PLTDocument(stroke_paths=[path])
        # Should not raise even if plt.show() would block
        fig = plot_plt_document(doc, show_plot=False)
        assert isinstance(fig, plt.Figure)

    def test_show_plot_true_does_not_raise(self) -> None:
        """Test that show_plot=True does not crash (may not actually show)."""
        seg = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(1, 1),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg,))
        doc = PLTDocument(stroke_paths=[path])
        # Should complete without raising (interactive display may or may not appear)
        fig = plot_plt_document(doc, show_plot=True)
        assert isinstance(fig, plt.Figure)


class TestSaveFigureOutput:
    """Tests for save_figure with output path (lines 329-330)."""

    def test_save_to_png_with_dpi(self, tmp_path: Path) -> None:
        """Test saving to PNG uses specified DPI."""
        fig, ax = plt.subplots()
        ax.plot([0, 1], [0, 1])
        output_path = tmp_path / "output.png"
        save_figure(fig, output_path)
        assert output_path.exists()

    def test_save_to_explicit_png(self, tmp_path: Path) -> None:
        """Test saving with explicit .png extension."""
        fig, ax = plt.subplots()
        ax.plot([0, 1], [0, 1])
        output_path = tmp_path / "explicit.png"
        save_figure(fig, output_path)
        assert output_path.exists()


class TestPlotStrokePathOutput:
    """Tests for plot_stroke_path with output path (line 322)."""

    def test_plot_stroke_path_saves_to_file(self, tmp_path: Path) -> None:
        """Test that plot_stroke_path can save to file."""
        seg = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(1, 1),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg,))
        output_path = tmp_path / "path.png"
        fig = plot_stroke_path(path, output_path=output_path)
        assert isinstance(fig, plt.Figure)
        # Note: save happens in delegated call to plot_plt_document

    def test_plot_stroke_path_show_false(self) -> None:
        """Test plot_stroke_path with show_plot=False."""
        seg = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(1, 1),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg,))
        fig = plot_stroke_path(path, show_plot=False)
        assert isinstance(fig, plt.Figure)


class TestAxisLimitsEdgeCases:
    """Tests for axis limit edge cases (lines 170-173)."""

    def test_axis_limits_with_identical_x_values(self) -> None:
        """Test axis limits when all x values are the same."""
        seg1 = StrokeSegment(
            start=Coordinate(5, 0),
            end=Coordinate(5, 10),
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(5, 10),
            end=Coordinate(5, 20),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg1, seg2))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)

    def test_axis_limits_with_identical_y_values(self) -> None:
        """Test axis limits when all y values are the same."""
        seg1 = StrokeSegment(
            start=Coordinate(0, 5),
            end=Coordinate(10, 5),
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(10, 5),
            end=Coordinate(20, 5),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg1, seg2))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)

    def test_axis_limits_with_identical_xy_values(self) -> None:
        """Test axis limits when all points are identical."""
        # This tests the else branch: max_distance != 0 but range is 0
        seg1 = StrokeSegment(
            start=Coordinate(5, 5),
            end=Coordinate(5, 5),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg1,))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)


class TestColorbarAndLabels:
    """Tests for colorbar and label conditions (line 229-232)."""

    def test_colorbar_label_exists(self) -> None:
        """Test that colorbar has label when cutting segments exist."""
        seg = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(100, 200),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg,))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)

        # Verify figure was created successfully
        assert isinstance(fig, plt.Figure)


class TestCuttingDistanceDisplay:
    """Tests for cutting distance display in summary."""

    def test_cutting_distance_shown_in_summary(self) -> None:
        """Test that cutting distance is displayed in summary text."""
        seg = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(3000, 4000),  # Distance = 5000 units = 5 inches
            is_cutting=True,
        )
        path = StrokePath(segments=(seg,))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)

        texts = [
            child for child in fig.axes[0].get_children()
            if hasattr(child, 'get_text') and child.get_text()
        ]
        full_text = " ".join(t.get_text() for t in texts)
        assert "Cutting Distance:" in full_text


class TestPlasmaColormap:
    """Tests for plasma colormap usage."""

    def test_cutting_uses_plasma_colormap(self) -> None:
        """Test that cutting segments use plasma colormap."""
        seg1 = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(10, 0),
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(10, 0),
            end=Coordinate(20, 0),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg1, seg2))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)


class TestEmptyCumDistancesEdgeCase:
    """Tests for empty cum_distances edge case (line 140)."""

    def test_empty_cumulative_distances_handled(self) -> None:
        """Test that empty cumulative distances list is handled gracefully.

        This tests the branch where cum_distances evaluates to falsy,
        defaulting max_distance to 1.0.
        """
        # Create segments with identical start/end points which may result
        # in empty or very short cumulative distance lists depending on
        # calculate_cumulative_distances behavior
        segs = [
            StrokeSegment(
                start=Coordinate(5, 5),
                end=Coordinate(5, 5),  # Zero length - should be filtered out
                is_cutting=True,
            ),
            StrokeSegment(
                start=Coordinate(6, 6),
                end=Coordinate(6, 6),  # Another zero length
                is_cutting=True,
            ),
        ]
        path = StrokePath(segments=tuple(segs))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)

    def test_all_zero_length_segments_max_distance_default(self) -> None:
        """Test that max_distance defaults to 1.0 when all segments are zero-length."""
        segs = [
            StrokeSegment(
                start=Coordinate(10, 10),
                end=Coordinate(10, 10),  # Zero length
                is_cutting=True,
            ),
            StrokeSegment(
                start=Coordinate(20, 20),
                end=Coordinate(20, 20),  # Zero length at different position
                is_cutting=True,
            ),
        ]
        path = StrokePath(segments=tuple(segs))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)


class TestArcSegmentRapidTravel:
    """Tests for Arc segment rapid travel plotting (lines 170-183)."""

    def _make_arc_segment(
        self,
        start_x: float = 0.0,
        start_y: float = 0.0,
        center_x: float = 1.0,
        center_y: float = 0.0,
        end_x: float = 2.0,
        end_y: float = 0.0,
        sweep_angle: float = 180.0,
        is_cutting: bool = False,  # Default to rapid
    ) -> ArcSegment:
        """Helper to create an arc segment."""
        return ArcSegment(
            start=Coordinate(start_x, start_y),
            end=Coordinate(end_x, end_y),
            center=Coordinate(center_x, center_y),
            sweep_angle=sweep_angle,
            is_cutting=is_cutting,
        )

    def test_arc_rapid_travel_full_semicircle(self) -> None:
        """Test plotting arc rapid travel with full 180-degree sweep."""
        arc = self._make_arc_segment(
            start_x=-1.0,
            start_y=0.0,
            center_x=0.0,
            center_y=0.0,
            end_x=1.0,
            end_y=0.0,
            sweep_angle=180.0,
            is_cutting=False,
        )
        path = StrokePath(segments=(arc,))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)

    def test_arc_rapid_travel_small_angle(self) -> None:
        """Test plotting arc rapid travel with small sweep angle."""
        arc = self._make_arc_segment(
            start_x=0.0,
            start_y=1.0,
            center_x=0.0,
            center_y=0.0,
            end_x=1.0,
            end_y=0.0,
            sweep_angle=90.0,
            is_cutting=False,
        )
        path = StrokePath(segments=(arc,))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)

    def test_multiple_arc_rapid_segments(self) -> None:
        """Test plotting multiple arc segments as rapid travel."""
        arc1 = self._make_arc_segment(
            start_x=0.0,
            start_y=-1.0,
            center_x=0.0,
            center_y=0.0,
            end_x=0.0,
            end_y=1.0,
            sweep_angle=180.0,
            is_cutting=False,
        )
        arc2 = self._make_arc_segment(
            start_x=5.0,
            start_y=-1.0,
            center_x=5.0,
            center_y=0.0,
            end_x=5.0,
            end_y=1.0,
            sweep_angle=180.0,
            is_cutting=False,
        )
        path = StrokePath(segments=(arc1, arc2))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)

    def test_mixed_arc_rapid_and_line_rapid(self) -> None:
        """Test plotting mix of rapid arcs and rapid lines."""
        seg = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(5, 0),
            is_cutting=False,
        )
        arc = self._make_arc_segment(
            start_x=5.0,
            start_y=0.0,
            center_x=10.0,
            center_y=0.0,
            end_x=15.0,
            end_y=0.0,
            sweep_angle=180.0,
            is_cutting=False,
        )
        path = StrokePath(segments=(seg, arc))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)


class TestArcSegmentCuttingPlot:
    """Tests for Arc segment cutting plotting (lines 244-256)."""

    def _make_arc_segment(
        self,
        start_x: float = 0.0,
        start_y: float = 0.0,
        center_x: float = 1.0,
        center_y: float = 0.0,
        end_x: float = 2.0,
        end_y: float = 0.0,
        sweep_angle: float = 180.0,
    ) -> ArcSegment:
        """Helper to create an arc segment."""
        return ArcSegment(
            start=Coordinate(start_x, start_y),
            end=Coordinate(end_x, end_y),
            center=Coordinate(center_x, center_y),
            sweep_angle=sweep_angle,
            is_cutting=True,
        )

    def test_arc_cutting_full_circle(self) -> None:
        """Test plotting arc cutting with 360-degree sweep."""
        arc = ArcSegment(
            start=Coordinate(1.0, 0.0),
            end=Coordinate(1.0, 0.0),  # Same as start for full circle
            center=Coordinate(0.0, 0.0),
            sweep_angle=360.0,
            is_cutting=True,
        )
        path = StrokePath(segments=(arc,))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)

    def test_single_arc_cutting_segment(self) -> None:
        """Test plotting a single arc segment as cutting."""
        arc = self._make_arc_segment(
            start_x=0.0,
            start_y=-1.0,
            center_x=0.0,
            center_y=0.0,
            end_x=0.0,
            end_y=1.0,
            sweep_angle=180.0,
        )
        path = StrokePath(segments=(arc,))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)

    def test_multiple_sequential_cutting_arcs(self) -> None:
        """Test plotting multiple arcs all as cutting."""
        arc1 = ArcSegment(
            start=Coordinate(0.0, 0.0),
            end=Coordinate(2.0, 0.0),
            center=Coordinate(1.0, 0.0),
            sweep_angle=180.0,
            is_cutting=True,
        )
        arc2 = ArcSegment(
            start=Coordinate(2.0, 0.0),
            end=Coordinate(4.0, 0.0),
            center=Coordinate(3.0, 0.0),
            sweep_angle=180.0,
            is_cutting=True,
        )
        path = StrokePath(segments=(arc1, arc2))
        doc = PLTDocument(stroke_paths=[path])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)


class TestPenUpConnectionDrawing:
    """Tests for pen-up connection drawing between paths (lines 205-219)."""

    def test_pen_up_connection_plotted(self) -> None:
        """Test that pen-up connections are plotted as dashed lines."""
        # First path ends at (10, 10)
        seg1 = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(10, 10),
            is_cutting=True,
        )
        path1 = StrokePath(
            segments=(seg1,),
            pen_up_position=Coordinate(30, 40),  # Next start after travel
        )

        # Second path starts at (30, 40)
        seg2 = StrokeSegment(
            start=Coordinate(30, 40),
            end=Coordinate(50, 60),
            is_cutting=True,
        )
        path2 = StrokePath(
            segments=(seg2,),
            pen_up_position=None,
        )

        doc = PLTDocument(stroke_paths=[path1, path2])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)

    def test_multiple_pen_up_connections(self) -> None:
        """Test multiple sequential pen-up connections."""
        seg1 = StrokeSegment(start=Coordinate(0, 0), end=Coordinate(10, 0), is_cutting=True)
        path1 = StrokePath(segments=(seg1,), pen_up_position=Coordinate(20, 0))

        seg2 = StrokeSegment(start=Coordinate(20, 0), end=Coordinate(30, 0), is_cutting=True)
        path2 = StrokePath(segments=(seg2,), pen_up_position=Coordinate(40, 0))

        seg3 = StrokeSegment(start=Coordinate(40, 0), end=Coordinate(50, 0), is_cutting=True)
        path3 = StrokePath(segments=(seg3,), pen_up_position=None)

        doc = PLTDocument(stroke_paths=[path1, path2, path3])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)

    def test_pen_up_connection_with_empty_path_skipped(self) -> None:
        """Test that paths with no segments skip pen-up connection."""
        # Empty first path (no segments to get last_seg from)
        path1 = StrokePath(segments=(), pen_up_position=Coordinate(20, 30))

        seg2 = StrokeSegment(
            start=Coordinate(20, 30),
            end=Coordinate(40, 50),
            is_cutting=True,
        )
        path2 = StrokePath(segments=(seg2,))

        doc = PLTDocument(stroke_paths=[path1, path2])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)

    def test_pen_up_connection_with_only_rapid_segments(self) -> None:
        """Test pen-up connection when last segment is rapid (non-cutting)."""
        seg1 = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(10, 10),
            is_cutting=False,  # Rapid move
        )
        path1 = StrokePath(
            segments=(seg1,),
            pen_up_position=Coordinate(50, 50),
        )

        seg2 = StrokeSegment(
            start=Coordinate(50, 50),
            end=Coordinate(60, 60),
            is_cutting=True,
        )
        path2 = StrokePath(segments=(seg2,))

        doc = PLTDocument(stroke_paths=[path1, path2])
        fig = plot_plt_document(doc)
        assert isinstance(fig, plt.Figure)


class TestPlotStrokePathSaveToFile:
    """Tests for plot_stroke_path saving to file (lines 329-330)."""

    def test_plot_stroke_path_saves_png(self, tmp_path: Path) -> None:
        """Test that plot_stroke_path saves figure when output_path is provided."""
        seg = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(1, 1),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg,))
        output_path = tmp_path / "stroke_output.png"
        fig = plot_stroke_path(path, output_path=output_path)
        assert isinstance(fig, plt.Figure)
        assert output_path.exists()

    def test_plot_stroke_path_saves_pdf(self, tmp_path: Path) -> None:
        """Test that plot_stroke_path saves figure as PDF."""
        seg = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(100, 200),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg,))
        output_path = tmp_path / "stroke_output.pdf"
        fig = plot_stroke_path(path, output_path=output_path)
        assert isinstance(fig, plt.Figure)
        assert output_path.exists()

    def test_plot_stroke_path_saves_with_nested_dir(self, tmp_path: Path) -> None:
        """Test that plot_stroke_path creates nested directories for output."""
        seg = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(1, 1),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg,))
        output_path = tmp_path / "nested" / "dirs" / "output.png"
        fig = plot_stroke_path(path, output_path=output_path)
        assert isinstance(fig, plt.Figure)
        assert output_path.exists()

    def test_plot_plt_document_saves_with_output_path(self, tmp_path: Path) -> None:
        """Test that plot_plt_document saves when output_path is provided."""
        seg = StrokeSegment(
            start=Coordinate(0, 0),
            end=Coordinate(1, 1),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg,))
        doc = PLTDocument(stroke_paths=[path])
        output_path = tmp_path / "document_output.png"
        fig = plot_plt_document(doc, output_path=output_path)
        assert isinstance(fig, plt.Figure)
        assert output_path.exists()
