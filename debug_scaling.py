#!/usr/bin/env python3
"""Debug: check what _scale_coordinates_per_layer is doing."""

import tempfile
from pathlib import Path
import re

import vpype as vp

from plt_optimizer.generate.resolution import resolve_job_spec
from plt_optimizer.generate.schema import parse_yaml
from plt_optimizer.generate.label_renderer import (
    _extract_coordinates_by_pen,
    _scale_coordinates_per_layer,
)

# Parse job and get first label
job = parse_yaml("examples/test123_spec.yaml")
labels = resolve_job_spec(job)
label = labels[0]

# Create simple test document with text and border
size = label.content[0].toolpath_text_height / 0.65625
text_lc = vp.text_block(label.content[0].text, width=label.width, size=size)

# Manually position text to 0.28-0.72" Y range (centered)
text_lc.translate(0, 0.28)

doc = vp.Document()
doc.add(text_lc, 1)  # Pen 1

# Add border
boundary_lc = vp.LineCollection()
rect_array = vp.rect(0, 0, label.width, label.height)
boundary_lc.append(rect_array)
doc.add(boundary_lc, 2)  # Pen 2

# Export to temporary file with center=True
with tempfile.NamedTemporaryFile(mode="w", suffix=".plt", delete=False) as f:
    temp_path = Path(f.name)

with open(temp_path, "w", encoding="utf-8") as f:
    vp.write_hpgl(
        f,
        doc,
        page_size="A3",
        landscape=True,
        center=True,
        device="hp7475a",
        velocity=None,
        absolute=True,
    )

plt_content = temp_path.read_text()

print("=" * 80)
print("BEFORE SCALING")
print("=" * 80)

# Extract coordinates by pen
coords_by_pen = _extract_coordinates_by_pen(plt_content)

for pen_num in [1, 2]:
    coords = coords_by_pen.get(pen_num, [])
    if not coords:
        continue
    
    y_vals = [y for x, y in coords]
    print(f"\nPen {pen_num}:")
    print(f"  Coordinates: {len(coords)}")
    if y_vals:
        print(f"  Y range: [{min(y_vals)}, {max(y_vals)}]")
        print(f"  Y range (inches): [{min(y_vals)/1000:.4f}, {max(y_vals)/1000:.4f}]")

# Now apply per-layer scaling
print("\n" + "=" * 80)
print("APPLYING SCALING")
print("=" * 80)

# Calculate scale factors (same logic as _scale_coordinates_per_layer)
layer_scales = {}
for pen_num in [1, 2]:
    coords = coords_by_pen.get(pen_num, [])
    if not coords:
        continue
    
    x_vals = [x for x, y in coords]
    y_vals = [y for x, y in coords]
    
    x_min, x_max = min(x_vals), max(x_vals)
    y_min, y_max = min(y_vals), max(y_vals)
    
    x_range = x_max - x_min if x_max > x_min else 1
    y_range = y_max - y_min if y_max > y_min else 1
    
    scale_x = (label.width * 1000.0) / x_range
    scale_y = (label.height * 1000.0) / y_range
    
    layer_scales[pen_num] = (scale_x, scale_y)
    print(f"\nPen {pen_num}:")
    print(f"  Bounds: X=[{x_min}, {x_max}] ({x_range}), Y=[{y_min}, {y_max}] ({y_range})")
    print(f"  Scale: x={scale_x:.4f}, y={scale_y:.4f}")

# Apply scaling
scaled_plt = _apply_per_layer_scaling(plt_content, layer_scales)

print("\n" + "=" * 80)
print("AFTER SCALING")
print("=" * 80)

coords_by_pen_after = _extract_coordinates_by_pen(scaled_plt)

for pen_num in [1, 2]:
    coords = coords_by_pen_after.get(pen_num, [])
    if not coords:
        continue
    
    y_vals = [y for x, y in coords]
    print(f"\nPen {pen_num}:")
    print(f"  Coordinates: {len(coords)}")
    if y_vals:
        print(f"  Y range: [{min(y_vals)}, {max(y_vals)}]")
        print(f"  Y range (inches): [{min(y_vals)/1000:.4f}, {max(y_vals)/1000:.4f}]")

# Clean up
temp_path.unlink()
