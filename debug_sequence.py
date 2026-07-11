from plt_optimizer.core.parser import PLTParser
from plt_optimizer.core.writer import PLTWriter
from pathlib import Path
import math

parser = PLTParser()
content = Path('examples/2026-07-10 SW0914 1111sheet1.plt').read_text()
doc = parser.parse_string(content)
writer = PLTWriter()

# Manually simulate the write process to see what's happening
current_pos = None
for i, path in enumerate(doc.stroke_paths):
    path_str, current_pos = writer._format_stroke_path(path, current_pos)
    
    # Only show paths around the problem area (paths 0-4)
    if i <= 4:
        print(f"\nPath {i}:")
        print(f"  pen_up={path.pen_up_position}, {len(path.segments)} segs")
        if path.segments:
            seg = path.segments[0]
            print(f"  First seg: {seg.start} -> {seg.end}, cutting={getattr(seg, 'is_cutting', '?')}")
        print(f"  current_pos before: {current_pos if i == 0 else 'updated from prev'}")
        print(f"  Output: {repr(path_str[:100])}")
        print(f"  current_pos after: {current_pos}")
