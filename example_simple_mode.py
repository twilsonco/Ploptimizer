#!/usr/bin/env python3
"""Example demonstrating the new simple_mode parameter in plotter.py."""

from pathlib import Path

from plt_optimizer.core.models import Coordinate, PLTDocument, StrokePath, StrokeSegment
from plt_optimizer.diagnostics.plotter import plot_plt_document


def create_sample_document() -> PLTDocument:
    """Create a sample document with cutting and rapid segments."""
    # Create some cutting segments (tool-down)
    segments = [
        StrokeSegment(
            start=Coordinate(1000, 1000),
            end=Coordinate(5000, 1000),
            is_cutting=True,
        ),
        StrokeSegment(
            start=Coordinate(5000, 1000),
            end=Coordinate(5000, 5000),
            is_cutting=True,
        ),
        # Rapid move to next cutting area (tool-up)
        StrokeSegment(
            start=Coordinate(5000, 5000),
            end=Coordinate(10000, 1000),
            is_cutting=False,
        ),
        # More cutting
        StrokeSegment(
            start=Coordinate(10000, 1000),
            end=Coordinate(15000, 1000),
            is_cutting=True,
        ),
        StrokeSegment(
            start=Coordinate(15000, 1000),
            end=Coordinate(15000, 5000),
            is_cutting=True,
        ),
    ]
    path = StrokePath(segments=tuple(segments))
    return PLTDocument(stroke_paths=[path])


if __name__ == "__main__":
    doc = create_sample_document()
    output_dir = Path("test_output")
    output_dir.mkdir(exist_ok=True)

    # Default mode: color-coded toolpath with rapid travel shown
    print("Generating default plot (color-coded with rapids)...")
    fig1 = plot_plt_document(
        doc,
        output_path=output_dir / "example_default.png",
        title="Default Mode: Color-Coded Toolpath",
        simple_mode=False,
    )

    # Simple mode: clean black outline, no rapids
    print("Generating simple plot (black outline only)...")
    fig2 = plot_plt_document(
        doc,
        output_path=output_dir / "example_simple.png",
        title="Simple Mode: Clean Black Outline",
        simple_mode=True,
    )

    print(f"Plots saved to {output_dir}/")
    print("  - example_default.png: Default mode with color and rapid travel")
    print("  - example_simple.png: Simple mode with thick black lines only")
