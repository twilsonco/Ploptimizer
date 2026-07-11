from pathlib import Path

from plt_optimizer.core.parser import PLTParser

parser = PLTParser()
content = Path("examples/2026-07-10 SW0914 1111sheet1.plt").read_text()
doc = parser.parse_string(content)

print(f"Total paths: {len(doc.stroke_paths)}\n")

# Find paths mentioning coordinates like 1284 or 1617 (the PU coordinates we're looking for)
found = False
for i, path in enumerate(doc.stroke_paths):
    if path.pen_up_position:
        x, y = path.pen_up_position.x, path.pen_up_position.y
        if 1200 < x < 1700 and 450 < y < 520:
            print(f"Path {i}: pen_up={path.pen_up_position}, {len(path.segments)} segs")
            for j, seg in enumerate(path.segments):
                print(f"  Seg {j}: {seg.start} -> {seg.end}, is_cutting={seg.is_cutting}")
            found = True

    for seg in path.segments:
        if hasattr(seg, "start"):
            x, y = seg.start.x, seg.start.y
            if 1200 < x < 1700 and 450 < y < 520:
                if not found:
                    print(f"Path {i}: (via segment) {len(path.segments)} segs")
                    print(f"  pen_up={path.pen_up_position}")
                    for j, seg2 in enumerate(path.segments):
                        print(
                            f"  Seg {j}: {seg2.start} -> {seg2.end}, is_cutting={seg2.is_cutting}"
                        )
                    found = True

if not found:
    print("No paths found in that coordinate range. Showing first 3 paths:")
    for i in range(min(3, len(doc.stroke_paths))):
        path = doc.stroke_paths[i]
        print(f"Path {i}: pen_up={path.pen_up_position}, {len(path.segments)} segs")
