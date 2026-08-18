#!/usr/bin/env python3
"""Debug: Check what vpype actually renders for text positioning."""

import vpype as vp
import tempfile
from pathlib import Path

print("=" * 80)
print("TEST 1: Simple text WITHOUT translation")
print("=" * 80)

doc1 = vp.Document()
text_lc1 = vp.text_block("Test", width=2.0, size=0.5)
doc1.add(text_lc1)

with tempfile.NamedTemporaryFile(mode='w', suffix='.plt', delete=False, encoding='utf-8') as f:
    temp_path1 = f.name
    vp.write_hpgl(f, doc1, page_size="A3", landscape=True, center=True, device="hp7475a", velocity=None)

content1 = Path(temp_path1).read_text()
print("First 30 commands (before translate):")
for i, cmd in enumerate(content1.split(';')[:30]):
    if cmd.strip():
        print(f"  {cmd}")
Path(temp_path1).unlink()

print("\n" + "=" * 80)
print("TEST 2: Text WITH translation to Y=0.5")
print("=" * 80)

doc2 = vp.Document()
text_lc2 = vp.text_block("Test", width=2.0, size=0.5)
text_lc2.translate(0, 0.5)  # Move up by 0.5 inches
doc2.add(text_lc2)

with tempfile.NamedTemporaryFile(mode='w', suffix='.plt', delete=False, encoding='utf-8') as f:
    temp_path2 = f.name
    vp.write_hpgl(f, doc2, page_size="A3", landscape=True, center=True, device="hp7475a", velocity=None)

content2 = Path(temp_path2).read_text()
print("First 30 commands (after translate):")
for i, cmd in enumerate(content2.split(';')[:30]):
    if cmd.strip():
        print(f"  {cmd}")
Path(temp_path2).unlink()

print("\n" + "=" * 80)
print("COMPARISON: Extract Y coordinates from both")
print("=" * 80)

import re
def extract_y_coords(content):
    coords = []
    pattern = r'(?:PA|PU|PD)([\d,\-]+)'
    for match in re.finditer(pattern, content):
        coords_str = match.group(1)
        parts = coords_str.split(',')
        for i in range(1, len(parts), 2):
            try:
                y = int(parts[i])
                coords.append(y)
            except ValueError:
                pass
    return coords

y1 = extract_y_coords(content1)
y2 = extract_y_coords(content2)

print(f"Without translate: Y values = {sorted(set(y1))}")
print(f"With translate:    Y values = {sorted(set(y2))}")

if y1 and y2:
    diff = max(y2) - max(y1) if y2 and y1 else 0
    print(f"Max Y difference: {diff} plotter units ({diff/1000:.4f} inches)")

