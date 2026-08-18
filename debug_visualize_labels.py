#!/usr/bin/env python3
"""Debug script: visualize individual labels to identify centering issues."""

import logging
import re
from pathlib import Path

from plt_optimizer.core.parser import PLTParser
from plt_optimizer.generate.schema import parse_yaml
from plt_optimizer.generate.resolution import resolve_job_spec
from plt_optimizer.generate.label_renderer import render_label_to_plt
from plt_optimizer.diagnostics.plotter import plot_plt_document, save_figure

logging.basicConfig(level=logging.INFO)


def extract_y_coordinates(plt_content: str) -> tuple[list[float], float, float]:
    """Extract Y coordinates from PLT content."""
    coordinate_pattern = r"(PA|PU|PD)([\d,\-]+)"
    matches = re.findall(coordinate_pattern, plt_content)

    y_coords = []
    for _cmd, coords_str in matches:
        parts = coords_str.split(",")
        if len(parts) >= 2:
            try:
                y_val = int(parts[1])
                y_inches = y_val / 1000.0
                y_coords.append(y_inches)
            except ValueError:
                pass

    if y_coords:
        return y_coords, min(y_coords), max(y_coords)
    return [], 0.0, 0.0


def visualize_labels() -> None:
    """Render each label individually and create plots."""
    # Parse job
    job = parse_yaml("examples/test123_spec.yaml")
    labels = resolve_job_spec(job)

    print("\n" + "=" * 80)
    print("INDIVIDUAL LABEL VISUALIZATION & ANALYSIS")
    print("=" * 80)

    output_dir = Path("test_output/debug_plots")
    output_dir.mkdir(parents=True, exist_ok=True)

    for label in labels:
        print(f"\n{'=' * 80}")
        print(f"Label: {label.id}")
        print(f"{'=' * 80}")
        print(f"Dimensions: {label.width}\" × {label.height}\"")
        print(f"Margin: {label.margin}\"")
        print(f"Content: {[line.text for line in label.content]}")

        # Phase 1: Render label individually
        rendered = render_label_to_plt(label)

        print(f"\nRendered PLT Bounds:")
        print(f"  X: [{rendered.x_min:.4f}, {rendered.x_max:.4f}\"] "
              f"(size: {rendered.width:.4f}\")")
        print(f"  Y: [{rendered.y_min:.4f}, {rendered.y_max:.4f}\"] "
              f"(size: {rendered.height:.4f}\")")
        print(f"  Center: ({(rendered.x_min + rendered.x_max) / 2:.4f}, "
              f"{(rendered.y_min + rendered.y_max) / 2:.4f})")

        # Export PLT
        plt_file = output_dir / f"{label.id}_rendered.plt"
        plt_file.write_text(rendered.plt_content)
        print(f"  Exported: {plt_file}")

        # Parse PLT and create visualization plot
        try:
            parser = PLTParser()
            doc = parser.parse_file(plt_file)
            fig = plot_plt_document(doc)

            plot_file = output_dir / f"{label.id}_plot.png"
            save_figure(fig, plot_file)
            print(f"  Plot: {plot_file}")
        except Exception as e:
            print(f"  Plot error: {e}")

        # Analyze Y coordinates
        y_coords, y_min, y_max = extract_y_coordinates(rendered.plt_content)

        print(f"\nY-Coordinate Analysis:")
        print(f"  Extracted {len(y_coords)} Y coordinates")
        print(f"  Y range: [{y_min:.4f}, {y_max:.4f}\"]")
        if y_coords:
            y_avg = sum(y_coords) / len(y_coords)
            print(f"  Y average: {y_avg:.4f}\"")

        # Expected positioning
        available_height = label.height - 2 * label.margin
        expected_text_height = 0.5  # From test spec
        expected_y_center = label.margin + available_height / 2

        print(f"\nExpected Positioning:")
        print(f"  Label height: {label.height}\"")
        print(f"  Margin (2×): {2 * label.margin}\"")
        print(f"  Available height: {available_height}\"")
        print(f"  Expected text height: {expected_text_height}\"")
        print(f"  Expected Y center: {expected_y_center:.4f}\"")

        # Check if text is too small
        if y_coords:
            actual_text_height = y_max - y_min
            print(f"\nText Height Check:")
            print(f"  Expected: {expected_text_height:.4f}\"")
            print(f"  Actual: {actual_text_height:.4f}\"")
            print(f"  Ratio: {actual_text_height / expected_text_height:.4f}")

            # Check centering
            actual_y_center = (y_min + y_max) / 2
            centering_error = abs(actual_y_center - expected_y_center)
            print(f"\nCentering Check:")
            print(f"  Expected Y center: {expected_y_center:.4f}\"")
            print(f"  Actual Y center: {actual_y_center:.4f}\"")
            print(f"  Error: {centering_error:.4f}\" {'✓ GOOD' if centering_error < 0.01 else '✗ BAD'}")

    print("\n" + "=" * 80)
    print(f"Results saved to: {output_dir}")
    print("=" * 80)


if __name__ == "__main__":
    visualize_labels()
