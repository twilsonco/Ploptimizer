"""Example: Run diagnostics and optimization on all example PLT files.

This script demonstrates how to use the PLT-Optimizer tools to:
1. Load PLT files from disk
2. Generate diagnostic plots with before/after comparison
3. Generate simple mode plots (clean black outline, no rapids)
4. Run full optimization pipeline for each file
5. Log actions using the dual logging topology (text + CSV metrics)

Usage:
    # Run on all examples in ./examples directory:
    python examples/run_diagnostics.py

    # Run on a specific input directory:
    python examples/run_diagnostics.py --input-dir /path/to/files

    # Skip optimization (diagnostics only):
    python examples/run_diagnostics.py --no-optimize
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

# Add project root to path for imports when running as script
sys.path.insert(0, str(Path(__file__).parent.parent))

from plt_optimizer.core.chunker import Chunker, ChunkerConfig
from plt_optimizer.core.optimizer import (
    NearestNeighbor2OptStrategy,
    OptimizerEngine,
)
from plt_optimizer.core.parser import PLTParser
from plt_optimizer.core.profiler import Profiler
from plt_optimizer.core.reassembler import MetricsCalculator, Reassembler
from plt_optimizer.core.writer import PLTWriter
from plt_optimizer.diagnostics.plotter import plot_plt_document
from plt_optimizer.utils.geometry import remove_redundant_strokes
from plt_optimizer.utils.logging import (
    get_metrics_logger,
    get_text_logger,
)


def run_diagnostics_on_file(
    input_path: Path,
    output_dir: Path,
    optimize: bool = True,
    same_row_preference: float = 1.0,
) -> dict[str, str | float] | None:
    """Process a single PLT file and generate before/optimized/simple mode plots.

    Args:
        input_path: Path to the PLT/HPGL file.
        output_dir: Directory to save output plots.
        optimize: Whether to run optimization pipeline (default True).
        same_row_preference: Penalty multiplier for y-differences (default 1.0).

    Returns:
        Dictionary with statistics or None on failure.
    """
    text_logger = get_text_logger()
    metrics_logger = get_metrics_logger()

    try:
        # Parse the file
        parser = PLTParser()
        text_logger.info(f"Processing: {input_path.name}")
        doc = parser.parse_file(input_path)

        # Preserve original document for before plots
        original_doc = doc
        original_cutting = original_doc.cutting_distance()
        original_rapid = original_doc.rapid_distance()

        output_stem = input_path.stem

        # Generate BEFORE plot (color-coded with rapids)
        before_plot_path = output_dir / f"{output_stem}_01_before.png"
        fig_before = plot_plt_document(
            original_doc,
            output_path=before_plot_path,
            title=f"Before: {input_path.name}",
            simple_mode=False,
            rapid_travel_inches=original_rapid / 1000,
        )
        import matplotlib.pyplot as plt
        plt.close(fig_before)
        text_logger.info(f"Generated before plot: {before_plot_path.name}")

        # Generate SIMPLE MODE plot (black outline, no rapids - before state only)
        simple_plot_path = output_dir / f"{output_stem}_02_simple_outline.png"
        fig_simple = plot_plt_document(
            original_doc,
            output_path=simple_plot_path,
            title=f"Simple Outline (Before): {input_path.name}",
            simple_mode=True,
        )
        plt.close(fig_simple)
        text_logger.info(f"Generated simple mode plot: {simple_plot_path.name}")

        if not optimize:
            # Return stats without optimization
            return {
                "file": input_path.name,
                "original_rapid": original_rapid,
                "optimized_rapid": original_rapid,
                "improvement_pct": 0.0,
                "status": "diagnostic_only",
            }

        # Run optimization pipeline
        # Step 1: Simplify - Remove redundant overlapping strokes
        doc = remove_redundant_strokes(doc)
        text_logger.info("Simplified document by removing redundant strokes")

        # Step 2: Profile - Calculate baseline extent
        profiler = Profiler()
        profile_result = profiler.profile(doc)

        # Step 3: Chunk - Group strokes into MacroBlocks
        chunker = Chunker(config=ChunkerConfig(threshold_multiplier=2.0))
        blocks = chunker.chunk(doc.stroke_paths, profile_result.baseline_extent)

        # Step 4: Optimize - Find optimal traversal order
        optimizer = OptimizerEngine(
            strategy=NearestNeighbor2OptStrategy(same_row_preference=same_row_preference)
        )
        opt_start_time = time.perf_counter()
        optimization_result = optimizer.optimize(blocks)
        opt_elapsed_ms = (time.perf_counter() - opt_start_time) * 1000

        # Step 5: Reassemble - Rebuild PLTDocument with optimized order
        reassembler = Reassembler()
        optimized_doc = reassembler.reassemble(doc, blocks, optimization_result)

        # Calculate metrics
        metrics_calc = MetricsCalculator()
        optimized_rapid = optimized_doc.rapid_distance()
        optimized_cutting = optimized_doc.cutting_distance()

        rapid_improvement_pct = (
            ((original_rapid - optimized_rapid) / original_rapid * 100)
            if original_rapid > 0
            else 0.0
        )

        # Generate AFTER plot (color-coded, optimized)
        after_plot_path = output_dir / f"{output_stem}_03_after_optimized.png"
        fig_after = plot_plt_document(
            optimized_doc,
            output_path=after_plot_path,
            title=f"After Optimization ({rapid_improvement_pct:+.1f}%): {input_path.name}",
            simple_mode=False,
            rapid_travel_inches=optimized_rapid / 1000,
        )
        plt.close(fig_after)
        text_logger.info(f"Generated after plot: {after_plot_path.name}")

        # Write optimized PLT file
        writer = PLTWriter()
        optimized_plt_path = output_dir / f"{output_stem}_optimized.plt"
        writer.write_file(optimized_doc, optimized_plt_path)
        text_logger.info(f"Wrote optimized PLT: {optimized_plt_path.name}")

        # Log metrics
        job_id = f"diag_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{input_path.stem}"
        metrics_logger.log_job(
            job_id=job_id,
            original_file=input_path,
            optimized_file=optimized_plt_path,
            original_distance=original_rapid,
            optimized_distance=optimized_rapid,
            status="success",
        )

        return {
            "file": input_path.name,
            "original_rapid": original_rapid,
            "optimized_rapid": optimized_rapid,
            "improvement_pct": rapid_improvement_pct,
            "optimization_time_ms": opt_elapsed_ms,
            "status": "success",
        }

    except Exception as e:
        text_logger.error(f"Failed to process {input_path}: {e}")
        print(f"✗ Error processing {input_path.name}: {e}", file=sys.stderr)
        return None


def process_directory(
    input_dir: Path,
    output_dir: Path | None = None,
    optimize: bool = True,
    same_row_preference: float = 1.0,
) -> int:
    """Process all PLT files in a directory.

    Args:
        input_dir: Directory containing PLT files.
        output_dir: Output directory (default: input_dir + "_diag_output").
        optimize: Whether to run optimization pipeline (default True).
        same_row_preference: Penalty multiplier for y-differences (default 1.0).

    Returns:
        Exit code (0 for success).
    """
    if not input_dir.exists():
        print(f"Error: Input directory not found: {input_dir}", file=sys.stderr)
        return 1

    # Determine output directory
    if output_dir is None:
        output_dir = input_dir.parent / f"{input_dir.name}_diag_output"

    output_dir.mkdir(parents=True, exist_ok=True)

    text_logger = get_text_logger()
    text_logger.info(f"Processing directory: {input_dir}")

    print("\n" + "=" * 70)
    print("PLT-Optimizer Diagnostics")
    print("=" * 70)
    print(f"Input directory:  {input_dir}")
    print(f"Output directory: {output_dir}")
    print(f"Optimization:     {'Enabled' if optimize else 'Disabled (diagnostics only)'}")
    print("=" * 70 + "\n")

    # Find all PLT files
    plt_files = sorted(input_dir.glob("*.plt"))

    if not plt_files:
        print(f"No .plt files found in {input_dir}")
        return 1

    print(f"Found {len(plt_files)} PLT file(s) to process:\n")

    results = []
    for i, plt_file in enumerate(plt_files, 1):
        print(f"[{i}/{len(plt_files)}] {plt_file.name}...", end=" ", flush=True)
        stats = run_diagnostics_on_file(
            plt_file,
            output_dir,
            optimize=optimize,
            same_row_preference=same_row_preference,
        )
        if stats:
            print(
                f"✓ ({stats['improvement_pct']:+.1f}% improvement)"
                if stats["status"] == "success"
                else f"✓ ({stats['status']})"
            )
            results.append(stats)
        else:
            print("✗")

    # Print summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"\nProcessed: {len(results)}/{len(plt_files)} files")

    if results:
        total_original = sum(r["original_rapid"] for r in results)
        total_optimized = sum(r["optimized_rapid"] for r in results)
        overall_improvement = (
            ((total_original - total_optimized) / total_original * 100)
            if total_original > 0
            else 0.0
        )

        print("\nAggregated Results:")
        print(f"  Total original rapid distance: {total_original:,.0f} units")
        print(f"  Total optimized rapid distance: {total_optimized:,.0f} units")
        print(f"  Overall improvement: {overall_improvement:+.1f}%")

        print("\nPer-file Results:")
        for r in results:
            status_emoji = "✓" if r["status"] == "success" else "◆"
            print(
                f"  {status_emoji} {r['file']:40s} "
                f"Original: {r['original_rapid']:10,.0f}  "
                f"Optimized: {r['optimized_rapid']:10,.0f}  "
                f"Improvement: {r['improvement_pct']:+6.1f}%"
            )

    print(f"\nOutput saved to: {output_dir}")
    print("Log file: logs/optimizer.log")
    print("Metrics file: logs/job_metrics.csv")
    print("=" * 70 + "\n")

    return 0


def main() -> int:
    """Main entry point for the diagnostics tool.

    Returns:
        Exit code (0 for success).
    """
    parser = argparse.ArgumentParser(
        description="PLT-Optimizer Diagnostics Tool - Process all example PLT files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Process all PLT files in ./examples directory:
  python examples/run_diagnostics.py

  # Process a specific directory:
  python examples/run_diagnostics.py --input-dir /path/to/files

  # Diagnostics only (skip optimization):
  python examples/run_diagnostics.py --no-optimize

  # Specify custom output directory:
  python examples/run_diagnostics.py --output-dir /path/to/output
""",
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path(__file__).parent,
        help="Directory containing PLT files to process (default: ./examples)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory to save output plots and files "
        "(default: <input-dir>_diag_output)",
    )
    parser.add_argument(
        "--no-optimize",
        action="store_true",
        default=False,
        help="Skip optimization pipeline (diagnostics only)",
    )
    parser.add_argument(
        "--same-row-preference",
        type=float,
        default=1.0,
        help="Penalty multiplier for y-differences during greedy selection "
        "(default 1.0, values > 1.0 prefer same-row blocks)",
    )

    args = parser.parse_args()

    return process_directory(
        args.input_dir,
        output_dir=args.output_dir,
        optimize=not args.no_optimize,
        same_row_preference=args.same_row_preference,
    )


if __name__ == "__main__":
    sys.exit(main())
