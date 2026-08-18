#!/usr/bin/env python3
"""Debug script to verify label centering in individual renders.

This script uses the new Phase 1 individual rendering to debug why label 3
still has centering issues. By rendering each label independently, we can
isolate the problem to a specific phase.
"""

import re
from pathlib import Path
from plt_optimizer.generate.schema import parse_yaml
from plt_optimizer.generate.resolution import resolve_job_spec
from plt_optimizer.generate.label_renderer import render_label_to_plt


def extract_text_bounds_from_plt(plt_content: str) -> dict:
    """Extract text bounding box from PLT content.
    
    Analyzes all coordinates and finds the bounding box of rendered text.
    """
    # Extract all PA/PU/PD commands
    coordinate_pattern = r"(PA|PU|PD)([\d,\-]+)"
    matches = re.findall(coordinate_pattern, plt_content)
    
    x_coords = []
    y_coords = []
    
    for cmd, coords_str in matches:
        parts = coords_str.split(",")
        for i in range(0, len(parts) - 1, 2):
            try:
                x = int(parts[i]) / 1000.0  # Convert to inches
                y = int(parts[i + 1]) / 1000.0
                x_coords.append(x)
                y_coords.append(y)
            except (ValueError, IndexError):
                pass
    
    if not x_coords or not y_coords:
        return {
            "x_min": None, "x_max": None, "x_center": None, "x_width": None,
            "y_min": None, "y_max": None, "y_center": None, "y_height": None,
            "coord_count": 0,
        }
    
    return {
        "x_min": min(x_coords),
        "x_max": max(x_coords),
        "x_center": (min(x_coords) + max(x_coords)) / 2,
        "x_width": max(x_coords) - min(x_coords),
        "y_min": min(y_coords),
        "y_max": max(y_coords),
        "y_center": (min(y_coords) + max(y_coords)) / 2,
        "y_height": max(y_coords) - min(y_coords),
        "coord_count": len(x_coords),
    }


def main():
    """Debug each label individually."""
    print("\n" + "="*80)
    print("PHASE 1 INDIVIDUAL LABEL RENDERING DEBUG")
    print("="*80)
    
    # Parse and resolve labels
    job = parse_yaml("examples/test123_spec.yaml")
    resolved_labels = resolve_job_spec(job)
    
    print(f"\nJob: {job.job_name}")
    print(f"Plates: {len(job.plates)}")
    if job.plates:
        plate = job.plates[0]
        print(f"Plate dimensions: {plate.width}\" × {plate.height}\"")
    
    print(f"\nResolved {len(resolved_labels)} labels:\n")
    
    # Render and analyze each label
    for i, label in enumerate(resolved_labels):
        print(f"\n--- LABEL {i+1}: {label.id} ---")
        print(f"Nominal dimensions: {label.width}\" × {label.height}\"")
        print(f"Margin: {label.margin}\"")
        print(f"Content lines: {len(label.content)}")
        for j, line in enumerate(label.content):
            print(f"  Line {j+1}: '{line.text}'")
            print(f"    - Nominal height: {line.nominal_text_height}\"")
            print(f"    - Cutter diameter: {line.cutter_diameter}\"")
            print(f"    - Toolpath height: {line.toolpath_text_height}\"")
        
        # Render the label individually
        print(f"\nRendering label individually...")
        rendered = render_label_to_plt(label)
        
        print(f"Rendered dimensions: {rendered.width:.4f}\" × {rendered.height:.4f}\"")
        print(f"Bounds: X=[{rendered.x_min:.4f}, {rendered.x_max:.4f}], "
              f"Y=[{rendered.y_min:.4f}, {rendered.y_max:.4f}]")
        
        # Analyze text positioning within the rendered label
        bounds = extract_text_bounds_from_plt(rendered.plt_content)
        
        if bounds["coord_count"] > 0:
            print(f"\nText bounds in rendered PLT:")
            print(f"  X: {bounds['x_min']:.4f}\" to {bounds['x_max']:.4f}\" (width {bounds['x_width']:.4f}\")")
            print(f"  Y: {bounds['y_min']:.4f}\" to {bounds['y_max']:.4f}\" (height {bounds['y_height']:.4f}\")")
            print(f"  Center: ({bounds['x_center']:.4f}\", {bounds['y_center']:.4f}\")")
            
            # Calculate expected center
            inner_width = label.width - 2 * label.margin
            inner_height = label.height - 2 * label.margin
            expected_x = label.margin + inner_width / 2
            expected_y = label.margin + inner_height / 2
            
            print(f"\nExpected center (with margin): ({expected_x:.4f}\", {expected_y:.4f}\")")
            
            # Calculate deviations
            x_dev = bounds["x_center"] - expected_x
            y_dev = bounds["y_center"] - expected_y
            
            print(f"Deviation from expected:")
            print(f"  X: {x_dev:+.4f}\" ({'✓ OK' if abs(x_dev) < 0.1 else '✗ FAIL'})")
            print(f"  Y: {y_dev:+.4f}\" ({'✓ OK' if abs(y_dev) < 0.1 else '✗ FAIL'})")
            
            # Special case for label 3
            if i == 2:  # Label 3
                print(f"\n*** LABEL 3 SPECIAL ANALYSIS ***")
                print(f"Expected Y (without margin): {label.height / 2:.4f}\"")
                print(f"Expected Y (with margin): {expected_y:.4f}\"")
                print(f"Actual Y center: {bounds['y_center']:.4f}\"")
                print(f"This is label 3, which had centering issues in the old pipeline")
                if abs(y_dev) > 0.15:
                    print(f"⚠ WARNING: Large Y deviation detected ({y_dev:.4f}\")")
        else:
            print(f"No text coordinates found in rendered PLT")
        
        # Save rendered PLT for inspection
        output_path = Path("test_output") / f"debug_label_{i+1}_rendered.plt"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered.plt_content)
        print(f"\nRendered PLT saved to: {output_path}")
    
    print("\n" + "="*80)
    print("DEBUG COMPLETE")
    print("="*80 + "\n")


if __name__ == "__main__":
    main()
