"""Diagnostic plotting functionality for PLT-Optimizer.

This module provides visualization tools to render parsed HPGL/PLT data,
including color-coded path visualization based on cumulative distance traveled.
"""

# Check if matplotlib is available before importing plotter
try:
    import matplotlib  # noqa: F401

    from plt_optimizer.diagnostics.plotter import (
        DEFAULT_FIGURE_SIZE,
        PlotterError,
        create_path_diagram,
        plot_plt_document,
        plot_stroke_path,
        save_figure,
    )

    __all__ = [
        "DEFAULT_FIGURE_SIZE",
        "PlotterError",
        "create_path_diagram",
        "plot_plt_document",
        "plot_stroke_path",
        "save_figure",
    ]
except ImportError:
    # Matplotlib not available - diagnostics plotting features unavailable
    __all__ = []
