from pathlib import Path

from plt_optimizer.core.parser import PLTParser

parser = PLTParser()
content = Path("examples/2026-07-10 SW0914 1111sheet1.plt").read_text()
doc = parser.parse_string(content)

# Check what the last segment of Path 0 is
path0 = doc.stroke_paths[0]
path1 = doc.stroke_paths[1]

print("Path 0 analysis:")
print(f"  pen_up_position: {path0.pen_up_position}")
print(f"  Number of segments: {len(path0.segments)}")
if path0.segments:
    last_seg = path0.segments[-1]
    print(f"  Last segment: {last_seg.start} -> {last_seg.end}")
    print(f"  Last segment is_cutting: {last_seg.is_cutting}")

print("\nPath 1 analysis:")
print(f"  pen_up_position: {path1.pen_up_position}")
print(f"  Segment: {path1.segments[0].start} -> {path1.segments[0].end}")
print(f"  Segment is_cutting: {path1.segments[0].is_cutting}")

print("\nComparison:")
if path0.segments:
    last_seg_end = path0.segments[-1].end
    path1_pen_up = path1.pen_up_position
    print(f"  Path 0 ends at: {last_seg_end}")
    print(f"  Path 1 pen_up_position: {path1_pen_up}")
    if last_seg_end.x == path1_pen_up.x and last_seg_end.y == path1_pen_up.y:
        print("  ✓ They match! This is why Path 1's initial PU is skipped by the writer!")
    else:
        print("  ✗ They don't match")
