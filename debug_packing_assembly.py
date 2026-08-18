#!/usr/bin/env python3
"""Debug script to verify label packing and assembly in Phase 2 & 3.

This traces the complete pipeline to see where label positioning goes wrong.
"""

import re
from pathlib import Path
from plt_optimizer.generate.schema import parse_yaml
from plt_optimizer.generate.resolution import resolve_job_spec
from plt_optimizer.generate.layout import generate_layout, generate_layout_with_bounds
from plt_optimizer.generate.vectorize import (
    export_and_optimize_phase3,
    assemble_plt_from_rendered_labels,
)


def extract_coordinate_bounds(plt_content: str) -> dict:
    """Extract bounding box from PLT content."""
    coordinate_pattern = r"(PA|PU|PD)([\d,\-]+)"
    matches = re.findall(coordinate_pattern, plt_content)
    
    coords = []
    for cmd, coords_str in matches:
        parts = coords_str.split(",")
        for i in range(0, len(parts) - 1, 2):
            try:
                x = int(parts[i]) / 1000.0
                y = int(parts[i + 1]) / 1000.0
                coords.append((x, y))
            except (ValueError, IndexError):
                pass
    
    if not coords:
        return {"x_min": None, "x_max": None, "y_min": None, "y_max": None}
    
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    
    return {
        "x_min": min(xs),
        "x_max": max(xs),
        "y_min": min(ys),
        "y_max": max(ys),
        "count": len(coords),
    }


def main():
    """Debug Phase 2 and 3."""
    print("\n" + "="*80)
    print("PHASE 2 & 3 DEBUG: PACKING AND ASSEMBLY")
    print("="*80)
    
    # Parse and resolve
    job = parse_yaml("examples/test123_spec.yaml")
    resolved_labels = resolve_job_spec(job)
    
    # Phase 2: Packing with NEW pipeline
    print("\n--- PHASE 2: PACKING WITH RENDERED BOUNDS ---\n")
    packed_plates, rendered_map = generate_layout_with_bounds(resolved_labels, job.plates)
    
    print(f"Generated {len(packed_plates)} plates\n")
    
    for plate in packed_plates:
        print(f"Plate {plate.plate_id}: {plate.width}\" × {plate.height}\"")
        print(f"Labels: {len(plate.labels)}\n")
        
        for packed_label in plate.labels:
            print(f"  {packed_label.label_id}:")
            print(f"    Position: ({packed_label.x:.4f}\", {packed_label.y:.4f}\")")
            print(f"    Size: {packed_label.width:.4f}\" × {packed_label.height:.4f}\"")
            print(f"    Rotated: {packed_label.rotated}")
            
            # Expected Y for label 3 should be Y >= 2.0"
            label_num = int(packed_label.label_id.split("_")[-1])
            if label_num == 3:
                print(f"    *** LABEL 3: Y position = {packed_label.y:.4f}\" (should be ≥2.0\")")
            
            print()
    
    # Phase 3: Assembly
    print("\n--- PHASE 3: ASSEMBLY ---\n")
    
    for plate in packed_plates:
        plt_content = assemble_plt_from_rendered_labels(plate, rendered_map)
        
        bounds = extract_coordinate_bounds(plt_content)
        print(f"Plate {plate.plate_id} assembled PLT:")
        if bounds["count"] > 0:
            print(f"  Coordinate bounds:")
            print(f"    X: {bounds['x_min']:.4f}\" to {bounds['x_max']:.4f}\"")
            print(f"    Y: {bounds['y_min']:.4f}\" to {bounds['y_max']:.4f}\"")
            print(f"    Total coordinates: {bounds['count']}")
            
            # Analyze by Y ranges
            print(f"\n  Coordinate distribution by Y:")
            for i, (label_num, expected_y_min) in enumerate([(1, 0), (2, 1), (3, 2)]):
                expected_y_max = expected_y_min + 1
                label_coords = extract_y_range(plt_content, expected_y_min, expected_y_max)
                print(f"    Label {label_num} (Y {expected_y_min:.1f}-{expected_y_max:.1f}\"): {label_coords} coords")
        else:
            print(f"  WARNING: No coordinates found!")
        
        print()
    
    # Export with Phase 3
    print("\n--- PHASE 3 EXPORT ---\n")
    output_dir = Path("test_output/debug_phase3")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    exported = export_and_optimize_phase3(
        resolved_labels,
        provided_plates=job.plates,
        output_dir=output_dir,
        optimize=False,
        separate_layers=True,
    )
    
    print(f"Exported {len(exported)} files:\n")
    for path in exported:
        print(f"  {path.name}")
        
        # If it's a text layer, analyze coordinates
        if "_text" in path.name:
            content = path.read_text()
            bounds = extract_coordinate_bounds(content)
            if bounds["count"] > 0:
                print(f"    Bounds: X=[{bounds['x_min']:.4f}, {bounds['x_max']:.4f}], "
                      f"Y=[{bounds['y_min']:.4f}, {bounds['y_max']:.4f}]")
                
                # Count by label
                for label_num in [1, 2, 3]:
                    y_min = label_num - 1
                    y_max = label_num
                    count = extract_y_range(content, y_min, y_max)
                    if count > 0:
                        print(f"    Label {label_num}: {count} coordinates")
    
    print("\n" + "="*80)
    print("DEBUG COMPLETE")
    print("="*80 + "\n")


def extract_y_range(plt_content: str, y_min: float, y_max: float) -> int:
    """Count coordinates in a specific Y range."""
    coordinate_pattern = r"(PA|PU|PD)([\d,\-]+)"
    matches = re.findall(coordinate_pattern, plt_content)
    
    count = 0
    for cmd, coords_str in matches:
        parts = coords_str.split(",")
        for i in range(1, len(parts), 2):
            try:
                y = int(parts[i]) / 1000.0
                if y_min <= y < y_max:
                    count += 1
            except (ValueError, IndexError):
                pass
    
    return count


if __name__ == "__main__":
    main()
