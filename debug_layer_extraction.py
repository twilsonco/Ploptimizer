#!/usr/bin/env python3
"""Debug layer extraction to find the text layer issue."""

import re
from pathlib import Path
from plt_optimizer.generate.schema import parse_yaml
from plt_optimizer.generate.resolution import resolve_job_spec
from plt_optimizer.generate.layout import generate_layout_with_bounds
from plt_optimizer.generate.vectorize import (
    assemble_plt_from_rendered_labels,
    extract_layer_from_plt_text,
)


def analyze_plt_layers(plt_content: str) -> dict:
    """Analyze layer composition of PLT content."""
    lines = plt_content.split(";")
    
    layers = {}
    current_pen = 0
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        if line.startswith("SP"):
            try:
                current_pen = int(line[2:])
                if current_pen not in layers:
                    layers[current_pen] = {"commands": 0, "content": ""}
            except ValueError:
                pass
        elif current_pen > 0:
            if line and not line.startswith("IN") and not line.startswith("DF"):
                layers[current_pen]["commands"] += 1
                if len(layers[current_pen]["content"]) < 100:
                    layers[current_pen]["content"] += line[:30] + "..."
    
    return layers


def main():
    """Debug layer extraction."""
    print("\n" + "="*80)
    print("LAYER EXTRACTION DEBUG")
    print("="*80)
    
    # Parse and resolve
    job = parse_yaml("examples/test123_spec.yaml")
    resolved_labels = resolve_job_spec(job)
    
    # Phase 2: Packing
    packed_plates, rendered_map = generate_layout_with_bounds(resolved_labels, job.plates)
    plate = packed_plates[0]
    
    # Phase 3: Assembly
    print("\n--- ASSEMBLED PLT ANALYSIS ---\n")
    assembled_plt = assemble_plt_from_rendered_labels(plate, rendered_map)
    
    print(f"Assembled PLT length: {len(assembled_plt)} bytes")
    print(f"First 200 chars: {assembled_plt[:200]}")
    print(f"Last 200 chars: {assembled_plt[-200:]}")
    
    layers = analyze_plt_layers(assembled_plt)
    print(f"\nLayers found: {sorted(layers.keys())}")
    for pen_id, info in sorted(layers.items()):
        print(f"  Pen {pen_id}: {info['commands']} commands")
        if info["commands"] > 0:
            print(f"    Sample: {info['content']}")
    
    # Extract each layer
    print("\n--- LAYER EXTRACTION ---\n")
    
    for pen_id in [1, 2, 3]:
        print(f"Extracting pen {pen_id} (layer {['text', 'borders', 'holes'][pen_id-1]})...")
        
        extracted = extract_layer_from_plt_text(assembled_plt, pen_id)
        
        print(f"  Result length: {len(extracted)} bytes")
        if len(extracted) > 50:
            print(f"  First 100 chars: {extracted[:100]}")
        else:
            print(f"  Content: {extracted}")
        
        # Count commands
        cmd_count = extracted.count("PA") + extracted.count("PU") + extracted.count("PD")
        print(f"  Commands found: {cmd_count}")
        
        # Check if it's considered empty
        is_empty = len(extracted) <= 20
        print(f"  Considered empty: {is_empty}")
        print()
    
    print("="*80 + "\n")


if __name__ == "__main__":
    main()
