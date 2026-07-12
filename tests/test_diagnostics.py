"""Tests for plt_optimizer.diagnostics.__init__ re-exports.

This module verifies that the public API exposed by diagnostics/__init__.py
is correctly re-exported from plt_optimizer.diagnostics.plotter,
and that matplotlib ImportError fallback works correctly.
"""

from __future__ import annotations

import sys

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

    def test_fallback_with_mock_import(self) -> None:
        """Test that the except block sets __all__ = [] when matplotlib unavailable.

        This tests lines 28-30 by blocking matplotlib imports via builtin override,
        clearing cached diagnostics modules, and re-importing. This executes
        within the pytest process so coverage measurement includes these lines.
        """
        import builtins

        # Save original __import__ to restore later
        _real_import = builtins.__import__

        try:
            # 1. Block matplotlib imports at the builtin level
            def _blocked_import(name: str, *args: object, **kwargs: object) -> object:
                if "matplotlib" in name or name == "matplotlib":
                    raise ImportError("Simulated matplotlib unavailability")
                return _real_import(name, *args, **kwargs)

            builtins.__import__ = _blocked_import

            # 2. Clear any cached diagnostics modules so we get fresh import
            mods_cleared: list[str] = []
            for key in list(sys.modules.keys()):
                if key.startswith("plt_optimizer.diagnostics"):
                    del sys.modules[key]
                    mods_cleared.append(key)

            try:
                # 3. Re-import diagnostics - should hit the except block now
                from plt_optimizer.diagnostics import __all__

                # Verify fallback behavior: __all__ should be empty list
                assert isinstance(__all__, list)
                assert len(__all__) == 0, f"Expected [], got {__all__}"

            finally:
                # Clean up: remove diagnostics modules again so original state is restored
                for key in mods_cleared:
                    if key in sys.modules:
                        del sys.modules[key]

        finally:
            # Always restore the real __import__
            builtins.__import__ = _real_import

    def test_fallback_path_coverage_via_subprocess(self) -> None:
        """Test fallback path coverage via subprocess for line-level tracking.

        This ensures lines 28-30 are executed in a fresh Python process where
        we can definitively control matplotlib availability. The subprocess runs
        with coverage enabled to track execution of the except block.
        """
        import os
        import subprocess
        import tempfile

        # Use unique coverage data file and . Coverage database
        # Use a platform-appropriate temp directory
        coverage_file = os.path.join(
            tempfile.gettempdir(), "test_diag_fallback.coverage"
        )
        if os.path.exists(coverage_file):
            os.remove(coverage_file)

        # Use forward slashes to avoid backslash escaping issues when embedding
        # the path into the subprocess code string
        coverage_file_escaped = coverage_file.replace("\\", "/")
        project_root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )
        project_root_escaped = project_root.replace("\\", "/")

        code = f'''
import coverage
import sys

# Start coverage collection before anything else
cov = coverage.Coverage(data_file="{coverage_file_escaped}")
cov.start()

# Block ALL matplotlib imports at the import system level
class MatplotlibBlocker:
    def find_module(self, fullname, path=None):
        if "matplotlib" in fullname:
            raise ImportError("Simulated unavailability")
        return None

sys.meta_path.insert(0, MatplotlibBlocker())

# Force reimport of diagnostics package to hit the fallback
for key in list(sys.modules.keys()):
    if key.startswith("plt_optimizer.diagnostics"):
        del sys.modules[key]

from plt_optimizer.diagnostics import __all__
print(repr(__all__))

cov.stop()
cov.save()
'''
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=project_root,
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

