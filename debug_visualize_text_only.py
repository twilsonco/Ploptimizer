#!/usr/bin/env python3
"""Debug script: visualize text-only labels without borders."""

import logging
import re
from pathlib import Path

from plt_optimizer.core.parser import PLTParser
from plt_optimizer.generate.schema import parse_yaml
from plt_optimizer.generate.resolution import resolve_job_spec
from plt_optimizer.generate.label_renderer import render_label_to_plt
from plt_optimizer.diagnostics.plotter import plot_plt_document, save_figure

logging.basicConfig(level=logging.INFO)


def extract_text_layer(plt_content: str) -> str:
    """Extract only SP1 (text) layer from PLT content, remove borders."""
    # SP1 is the text layer (pen 1)
    # We want everything from SP1 to the next SP command
    
    match = re.search(r"SP1(.*?)(?:SP\d|SP0|IN;|$)", plt_content, re.DOTALL)
    if match:
        text_section = match.group(1)
        # Reconstruct minimal HPGL with just text
        hpgl = "IN;DF;PS0;SP1;" + text_section + "SP0;IN;%"
        return hpgl
    return ""


def visualize_text_only() -> None:
    """Render each label and create text-only plots."""
    # Parse job
    job = parse_yaml("examples/test123_spec.yaml")
    labels = resolve_job_spec(job)

    print("\n" + "=" * 80)
    print("TEXT-ONLY LABEL VISUALIZATION")
    print("=" * 80)

    output_dir = Path("test_output/debug_plots")
    output_dir.mkdir(parents=True, exist_ok=True)

    for label in labels:
        print(f"\n{'=' * 80}")
        print(f"Label: {label.id} - TEXT ONLY")
        print(f"{'=' * 80}")

        # Phase 1: Render label individually
        rendered = render_label_to_plt(label)

        # Extract text-only layer
        text_only = extract_text_layer(rendered.plt_content)

        if not text_only:
            print("  No text layer found!")
            continue

        # Export text-only PLT
        text_plt_file = output_dir / f"{label.id}_text_only.plt"
        text_plt_file.write_text(text_only)
        print(f"  Text-only PLT: {text_plt_file}")

        # Parse and visualize
        try:
            parser = PLTParser()
            doc = parser.parse_file(text_plt_file)
            fig = plot_plt_document(doc)

            plot_file = output_dir / f"{label.id}_text_only_plot.png"
            save_figure(fig, plot_file)
            print(f"  Text-only Plot: {plot_file}")

            # Now plot and save a `simple_mode=True` plot
            fig_simple = plot_plt_document(doc, simple_mode=True)
            plot_file_simple = output_dir / f"{label.id}_text_only_plot_simple.png"
            save_figure(fig_simple, plot_file_simple)
            print(f"  Text-only Simple Plot: {plot_file_simple}")
        except Exception as e:
            print(f"  Plot error: {e}")

        # Analyze arc commands
        arc_count = text_only.count(";AA")
        line_count = text_only.count("PD")
        print(f"\nCommand Analysis:")
        print(f"  Arc commands (AA): {arc_count}")
        print(f"  Line commands (PD): {line_count}")

    print("\n" + "=" * 80)
    print(f"Text-only plots saved to: {output_dir}")
    print("=" * 80)


if __name__ == "__main__":
    visualize_text_only()