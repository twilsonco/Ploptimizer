from pathlib import Path

from plt_optimizer.core.parser import PLTParser
from plt_optimizer.core.writer import PLTWriter

parser = PLTParser()
content = Path("examples/2026-07-10 SW0914 1111sheet1.plt").read_text()
doc = parser.parse_string(content)

# Check paths 0-3
print("Paths before Path 1 (PU1284...):")
for i in range(0, 4):
    if i < len(doc.stroke_paths):
        path = doc.stroke_paths[i]
        print(f"Path {i}: pen_up={path.pen_up_position}, {len(path.segments)} segs")
        if path.segments:
            print(
                f"  First seg: {path.segments[0].start} -> {path.segments[0].end}, cutting={path.segments[0].is_cutting}"
            )

print("\n" + "=" * 60)
print("Testing writer output for Path 1:")
writer = PLTWriter()

# Test writing just path 1
if len(doc.stroke_paths) > 1:
    path1 = doc.stroke_paths[1]
    output, current_pos = writer._format_stroke_path(path1, current_pos=None)
    print(f"Path 1 output: {repr(output)}")
    print(f"Current pos after: {current_pos}")

print("\n" + "=" * 60)
print("Testing full document write (first few hundred chars around the PU):")
output = writer.write_string(doc)
# Find the section with 1284
idx = output.find("1284")
if idx >= 0:
    print(output[max(0, idx - 50) : idx + 150])
else:
    print("1284 not found in output!")
    # Try 1617
    idx2 = output.find("1617")
    if idx2 >= 0:
        print(f"Found 1617 at index {idx2}:")
        print(output[max(0, idx2 - 150) : idx2 + 100])
