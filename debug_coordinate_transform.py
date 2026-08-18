#!/usr/bin/env python3
"""Debug: trace coordinate transformation through the rendering pipeline."""

import tempfile
from pathlib import Path
import re

import vpype as vp

from plt_optimizer.generate.resolution import resolve_job_spec
from plt_optimizer.generate.schema import parse_yaml

# Parse job and get first label
job = parse_yaml("examples/test123_spec.yaml")
labels = resolve_job_spec(job)
label = labels[0]

print("\n" + "=" * 80)
print(f"Tracing coordinate transformation for: {label.id}")
print("=" * 80)

# Step 1: Create text
size = label.content[0].toolpath_text_height / 0.65625
text_lc = vp.text_block(
    label.content[0].text,
    width=label.width,
    size=size,
)

print(f"\n1. After text_block():")
if text_lc.bounds():
    min_x, min_y, max_x, max_y = text_lc.bounds()
    print(f"   Bounds: X=[{min_x:.4f}, {max_x:.4f}], Y=[{min_y:.4f}, {max_y:.4f}]")
else:
    print("   No bounds")

# Step 2: Apply centering offset (simulating what _render_text_local does)
margin = label.margin
available_height = label.height - (2 * margin)
center_y = margin + available_height / 2

# Get bounds for centering calculation
if text_lc.bounds():
    min_x, min_y, max_x, max_y = text_lc.bounds()
    text_height = max_y - min_y
    
    # This is what the code does
    current_y = center_y + text_height / 2  # Top of text
    y_offset = current_y - max_y  # Translate so max_y reaches current_y
    
    print(f"\n2. Centering calculation:")
    print(f"   Label height: {label.height}\"")
    print(f"   Margin: {margin}\"")
    print(f"   Available height: {available_height}\"")
    print(f"   Center Y: {center_y}\"")
    print(f"   Text height: {text_height:.4f}\"")
    print(f"   Current Y (top of text): {current_y:.4f}\"")
    print(f"   Y offset to apply: {y_offset:.4f}\"")
    
    # Apply the offset
    text_lc_copy = vp.LineCollection(text_lc)
    text_lc_copy.translate(0, y_offset)
    
    if text_lc_copy.bounds():
        min_x2, min_y2, max_x2, max_y2 = text_lc_copy.bounds()
        print(f"\n3. After translate({y_offset:.4f}):")
        print(f"   Bounds: X=[{min_x2:.4f}, {max_x2:.4f}], Y=[{min_y2:.4f}, {max_y2:.4f}]")
    
    # Now create a vpype document and export
    doc = vp.Document()
    doc.add(text_lc_copy, 1)  # Pen 1 (text)
    
    # Also add border for reference
    boundary_lc = vp.LineCollection()
    rect_array = vp.rect(0, 0, label.width, label.height)
    boundary_lc.append(rect_array)
    doc.add(boundary_lc, 2)  # Pen 2 (boundary)
    
    print(f"\n4. Document created with:")
    print(f"   Layer 1 (text): {len(text_lc_copy)} segments")
    print(f"   Layer 2 (border): 1 segment")
    
    # Export to temporary file
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
    
    print(f"\n5. After write_hpgl() with center=True:")
    
    # Extract coordinates
    pattern = r"(?:PA|PU|PD)([\d,\-]+)"
    all_y = []
    for match in re.finditer(pattern, plt_content):
        coords_str = match.group(1)
        parts = coords_str.split(",")
        for i in range(1, len(parts), 2):
            try:
                y = int(parts[i])
                all_y.append(y)
            except ValueError:
                pass
    
    if all_y:
        print(f"   Y coordinates range: [{min(all_y)}, {max(all_y)}] plotter units")
        print(f"   Y coordinates range: [{min(all_y)/1000:.4f}, {max(all_y)/1000:.4f}] inches")
    
    # Print the PLT content (first 200 chars)
    print(f"\n6. PLT content (first 300 chars):")
    print(f"   {plt_content[:300]}...")
    
    # Clean up
    temp_path.unlink()
