#!/usr/bin/env python3
"""Debug: Inspect text coordinates before and after scaling."""

import re
from pathlib import Path

# Read the rendered PLT file
plt_path = Path("test_output/debug_plots/test_1_rendered.plt")
if not plt_path.exists():
    print(f"File not found: {plt_path}")
    exit(1)

content = plt_path.read_text()

# Extract all coordinates from pen 1 (text)
pattern = r"SP1;(.*?)(?:SP\d|$)"
match = re.search(pattern, content, re.DOTALL)
if not match:
    print("No pen 1 section found")
    exit(1)

text_section = match.group(1)

# Find all Y values
y_values = []
coord_pattern = r"(?:PA|PU|PD)([\d,\-]+)"
for coord_match in re.finditer(coord_pattern, text_section):
    coords_str = coord_match.group(1)
    parts = coords_str.split(",")
    for i in range(1, len(parts), 2):
        try:
            y = int(parts[i])
            y_values.append(y)
        except (ValueError, IndexError):
            pass

if not y_values:
    print("No Y coordinates found in text layer")
    exit(1)

print(f"Text Y coordinates (plotter units):")
print(f"  Count: {len(y_values)}")
print(f"  Min: {min(y_values)}")
print(f"  Max: {max(y_values)}")
print(f"  Range: {max(y_values) - min(y_values)}")
print(f"  In inches: {min(y_values)/1000:.4f} to {max(y_values)/1000:.4f}")
print(f"  Height: {(max(y_values) - min(y_values))/1000:.4f} inches")

# Check unique Y values
unique_y = sorted(set(y_values))
print(f"\nUnique Y values (plotter units): {unique_y[:20]}")
if len(unique_y) > 20:
    print(f"  ... and {len(unique_y) - 20} more")

# Show first few coordinates
print(f"\nFirst 10 coordinate commands from text section:")
for i, coord_match in enumerate(list(re.finditer(coord_pattern, text_section))[:10]):
    coords_str = coord_match.group(1)
    # Take first pair only
    parts = coords_str.split(",")[:2]
    if len(parts) == 2:
        print(f"  {coords_str[:40]}... -> ({parts[0]}, {parts[1]})")
    else:
        print(f"  {coords_str[:40]}...")
