"""Tests for plt_optimizer.diagnostics.__init__ re-exports.

This module verifies that the public API exposed by diagnostics/__init__.py
is correctly re-exported from plt_optimizer.diagnostics.plotter,
and that matplotlib ImportError fallback works correctly.
"""

from __future__ import annotations

import sys
from unittest import mock

import pytest

# Test that all re-exported names are accessible from the package
from plt_optimizer.diagnostics import (
    DEFAULT_FIGURE_SIZE,
    PlotterError,
    create_path_diagram,
    plot_plt_document,
    plot_stroke_path,
    save_figure,
)


class TestReexports:
    """Tests for diagnostics module re-exports."""

    def test_default_figure_size_exists(self) -> None:
        """Test DEFAULT_FIGURE_SIZE is accessible and has correct type."""
        assert isinstance(DEFAULT_FIGURE_SIZE, tuple)
        assert len(DEFAULT_FIGURE_SIZE) == 2

    def test_plotter_error_is_exception(self) -> None:
        """Test PlotterError is a proper exception class."""
        assert issubclass(PlotterError, Exception)

    def test_plotter_error_can_be_instantiated(self) -> None:
        """Test PlotterError can be instantiated with a message."""
        error = PlotterError("test error")
        assert error.message == "test error"

    def test_plotter_error_can_be_raised_and_caught(self) -> None:
        """Test PlotterError can be raised and caught."""
        with pytest.raises(PlotterError) as exc_info:
            raise PlotterError("test")
        assert "test" in str(exc_info.value)

    def test_plot_plt_document_is_callable(self) -> None:
        """Test plot_plt_document is a callable function."""
        assert callable(plot_plt_document)

    def test_plot_stroke_path_is_callable(self) -> None:
        """Test plot_stroke_path is a callable function."""
        assert callable(plot_stroke_path)

    def test_save_figure_is_callable(self) -> None:
        """Test save_figure is a callable function."""
        assert callable(save_figure)

    def test_create_path_diagram_is_callable(self) -> None:
        """Test create_path_diagram is a callable function."""
        assert callable(create_path_diagram)

    def test_no_missing_reexports(self) -> None:
        """Test that all names in __all__ are actually accessible."""
        import plt_optimizer.diagnostics as diag

        all_names = diag.__all__
        for name in all_names:
            assert hasattr(diag, name), f"Missing re-export: {name}"

    def test_no_extra_exports(self) -> None:
        """Test that __all__ contains exactly the expected names."""
        import plt_optimizer.diagnostics as diag

        expected = {
            "DEFAULT_FIGURE_SIZE",
            "PlotterError",
            "create_path_diagram",
            "plot_plt_document",
            "plot_stroke_path",
            "save_figure",
        }
        actual = set(diag.__all__)
        assert actual == expected, f"Expected {expected}, got {actual}"


class TestMatplotlibFallback:
    """Tests for matplotlib ImportError fallback behavior."""

    def test_all_empty_when_matplotlib_unavailable(self) -> None:
        """Test that __all__ is empty list when matplotlib cannot be imported.

        This tests the fallback path in diagnostics/__init__.py lines 28-30
        where ImportError is caught and __all__ is set to [].
        """
        import subprocess
        import sys

        code = '''
import sys
# Remove any cached matplotlib modules first
for key in list(sys.modules.keys()):
    if "matplotlib" in key.lower():
        del sys.modules[key]

# Block matplotlib imports using meta_path finder that raises ImportError
class MatplotlibBlocker:
    def find_module(self, fullname, path=None):
        if fullname == "matplotlib" or fullname.startswith("matplotlib."):
            raise ImportError("Matplotlib is not available")
        return None

sys.meta_path.insert(0, MatplotlibBlocker())

# Now import diagnostics - should hit the except block
from plt_optimizer.diagnostics import __all__
print(repr(__all__))
'''
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd="/Users/haiiro/NoSync/PLT-Optimizer",
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        output = result.stdout.strip()
        # When matplotlib is unavailable, __all__ should be empty list
        assert output == "[]", (
            f"Expected empty __all__ when matplotlib unavailable, got {output}. "
            f"stderr: {result.stderr}"
        )
