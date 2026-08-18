#!/usr/bin/env python3
"""Debug script: analyze coordinates by pen layer to identify label issues."""

import logging
import re
from pathlib import Path

from plt_optimizer.generate.schema import parse_yaml
from plt_optimizer.generate.resolution import resolve_job_spec
from plt_optimizer.generate.label_renderer import render_label_to_plt

logging.basicConfig(level=logging.INFO)


def extract_coordinates_by_pen(plt_content: str) -> dict[int, list[tuple[float, float]]]:
    """Extract coordinates grouped by pen number."""
    coordinates_by_pen: dict[int, list[tuple[float, float]]] = {1: [], 2: [], 3: []}
    
    current_pen = 0
    
    # Split by SP (Select Pen) commands
    pen_sections = re.split(r"SP(\d+);", plt_content)
    
    # pen_sections will be: [before_first_SP, first_pen_num, first_pen_content, next_pen_num, ...]
    for i in range(1, len(pen_sections), 2):
        pen_num_str = pen_sections[i]
        pen_content = pen_sections[i + 1] if i + 1 < len(pen_sections) else ""
        
        try:
            pen_num = int(pen_num_str)
            current_pen = pen_num
        except (ValueError, IndexError):
            continue
        
        # Extract coordinates from this pen section
        coordinate_pattern = r"(PA|PU|PD)([\d,\-]+)"
        matches = re.findall(coordinate_pattern, pen_content)
        
        for _cmd, coords_str in matches:
            parts = coords_str.split(",")
            for j in range(0, len(parts) - 1, 2):
                try:
                    x_val = int(parts[j])
                    y_val = int(parts[j + 1])
                    x_inches = x_val / 1000.0
                    y_inches = y_val / 1000.0
                    if current_pen in coordinates_by_pen:
                        coordinates_by_pen[current_pen].append((x_inches, y_inches))
                except (ValueError, IndexError):
                    pass
    
    return coordinates_by_pen


def analyze_labels() -> None:
    """Analyze each label's coordinates by pen layer."""
    # Parse job
    job = parse_yaml("examples/test123_spec.yaml")
    labels = resolve_job_spec(job)

    print("\n" + "=" * 100)
    print("LABEL COORDINATE ANALYSIS BY PEN LAYER")
    print("=" * 100)

    output_dir = Path("test_output/debug_plots")
    output_dir.mkdir(parents=True, exist_ok=True)

    for label in labels:
        print(f"\n{'=' * 100}")
        print(f"Label: {label.id}")
        print(f"{'=' * 100}")
        print(f"Spec: {label.width}\" × {label.height}\" (margin: {label.margin}\")")

        # Phase 1: Render label individually
        rendered = render_label_to_plt(label)

        # Export PLT
        plt_file = output_dir / f"{label.id}_rendered.plt"
        plt_file.write_text(rendered.plt_content)

        # Extract coordinates by pen
        coords_by_pen = extract_coordinates_by_pen(rendered.plt_content)

        # Analyze each pen
        for pen_num in [1, 2, 3]:
            coords = coords_by_pen.get(pen_num, [])
            if not coords:
                print(f"\nPen {pen_num} ({['', 'TEXT', 'BORDER', 'HOLES'][pen_num]}): No coordinates")
                continue
            
            x_vals = [x for x, y in coords]
            y_vals = [y for x, y in coords]
            
            pen_names = {1: "TEXT", 2: "BORDER", 3: "HOLES"}
            print(f"\nPen {pen_num} ({pen_names.get(pen_num, 'UNKNOWN')}): {len(coords)} coordinates")
            print(f"  X range: [{min(x_vals):.4f}, {max(x_vals):.4f}\"] (width: {max(x_vals) - min(x_vals):.4f}\")")
            print(f"  Y range: [{min(y_vals):.4f}, {max(y_vals):.4f}\"] (height: {max(y_vals) - min(y_vals):.4f}\")")
            
            y_avg = sum(y_vals) / len(y_vals)
            print(f"  Y center: {y_avg:.4f}\"")

        # Expected positioning
        available_height = label.height - 2 * label.margin
        expected_y_center = label.margin + available_height / 2

        print(f"\nExpected Positioning:")
        print(f"  Label height: {label.height}\"")
        print(f"  Margin (2×): {2 * label.margin}\"")
        print(f"  Available height: {available_height}\"")
        print(f"  Expected Y center: {expected_y_center:.4f}\"")

        # Check text centering specifically
        text_coords = coords_by_pen.get(1, [])
        if text_coords:
            y_vals = [y for x, y in text_coords]
            text_y_avg = sum(y_vals) / len(y_vals)
            error = abs(text_y_avg - expected_y_center)
            status = "✓ GOOD" if error < 0.01 else "✗ BAD"
            print(f"\nText Centering Check:")
            print(f"  Expected Y center: {expected_y_center:.4f}\"")
            print(f"  Actual Y center: {text_y_avg:.4f}\"")
            print(f"  Error: {error:.4f}\" {status}")

    print("\n" + "=" * 100)
    print(f"Results saved to: {output_dir}")
    print("=" * 100)


if __name__ == "__main__":
    analyze_labels()
