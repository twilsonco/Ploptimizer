#!/usr/bin/env python3
"""Debug: Check vpype internal coordinates before export."""

import vpype as vp

width = 3.0
size = 0.5 / 0.65625
text = "Test 1"

print("Testing vpype internal coordinate representation")
print("=" * 80)

# Generate text
text_lc = vp.text_block(text, width=width, size=size)

# Bounds
bounds = text_lc.bounds()
print(f"Text bounds from text_block: {bounds}")
if bounds:
    min_x, min_y, max_x, max_y = bounds
    print(f"  Width: {max_x - min_x:.6f}\"")
    print(f"  Height: {max_y - min_y:.6f}\"")

# Translate to origin
if bounds:
    text_lc.translate(-bounds[0], -bounds[1])

# Add to document
doc = vp.Document()
doc.add(text_lc)

# Check document bounds
doc_bounds = doc.bounds()
print(f"\nDocument bounds after adding text: {doc_bounds}")

# Try to extract raw coordinates from LineCollection
print(f"\nLineCollection details:")
print(f"  Number of segments: {len(text_lc)}")
print(f"  First 5 segments:")
for i, segment in enumerate(text_lc):
    if i >= 5:
        break
    if segment is not None:
        # Segments are complex numbers
        real_parts = segment.real
        imag_parts = segment.imag
        min_y = imag_parts.min()
        max_y = imag_parts.max()
        print(f"    Segment {i}: {len(segment)} points, Y range [{min_y:.6f}, {max_y:.6f}]")

# Extract all Y coordinates from all segments
all_y = []
for segment in text_lc:
    if segment is not None:
        all_y.extend(segment.imag)

if all_y:
    print(f"\nAll Y coordinates in vpype space:")
    print(f"  Count: {len(all_y)}")
    print(f"  Min: {min(all_y):.6f}\"")
    print(f"  Max: {max(all_y):.6f}\"")
    print(f"  Height: {max(all_y) - min(all_y):.6f}\"")
else:
    print("\nNo segments found!")
