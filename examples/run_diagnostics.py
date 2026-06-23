"""Example: Run diagnostics and optimization on a PLT file.

This script demonstrates how to use the PLT-Optimizer tools to:
1. Load a PLT file from disk or string content
2. Log actions using the dual logging topology (text + CSV metrics)
3. Perform identity validation by writing and re-parsing
4. Generate diagnostic plots with color-coded path visualization
5. Run full optimization pipeline with before/after comparison

Usage:
    # Run on a specific PLT file:
    python examples/run_diagnostics.py /path/to/your/file.plt

    # Run demonstration mode with sample data:
    python examples/run_diagnostics.py

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

from plt_optimizer.core.models import Coordinate, PLTDocument, StrokePath, StrokeSegment
from plt_optimizer.core.parser import PLTParser
from plt_optimizer.core.profiler import Profiler
from plt_optimizer.core.chunker import Chunker, ChunkerConfig
from plt_optimizer.core.optimizer import (
    GeneticAlgorithmStrategy,
    ChristofidesStrategy,
    InsertionHeuristicStrategy,
    NearestNeighbor2OptStrategy,
    OptimizerEngine,
    SimulatedAnnealingStrategy,
)
from plt_optimizer.core.reassembler import Reassembler, MetricsCalculator
from plt_optimizer.core.writer import PLTWriter, WriteError
from plt_optimizer.diagnostics.plotter import plot_plt_document


def validate_output_file(original_path: Path, optimized_path: Path) -> bool:
    """Validate the optimized output file against the original.

    Args:
        original_path: Path to the original input PLT file.
        optimized_path: Path to the generated optimized PLT file.

    Returns:
        True if validation passes or only has warnings, False if critical errors.
    """
    writer = PLTWriter()
    try:
        output_content = optimized_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"  Warning: Could not read optimized file for validation: {e}")
        return True

    is_valid, messages = writer.validate_against_original(original_path, output_content)

    if messages:
        print("\n  VALIDATION WARNINGS:")
        for msg in messages:
            # Indent and format the message
            for line in msg.split('\n'):
                print(f"    ! {line}")
        print()

    return is_valid or len(messages) > 0  # Return True if only warnings
from plt_optimizer.utils.logging import (
    CSVMetricsLogger,
    TextLogger,
    get_metrics_logger,
    get_text_logger,
)
from plt_optimizer.utils.geometry import remove_redundant_strokes


STRATEGY_REGISTRY = {
    "nn2opt": NearestNeighbor2OptStrategy,
    "insertion": InsertionHeuristicStrategy,
    "christofides": ChristofidesStrategy,
    "sa": SimulatedAnnealingStrategy,
    "genetic": GeneticAlgorithmStrategy,
}

# Strategies that support same_row_preference parameter
_STRATEGIES_WITH_SAME_ROW_PREFERENCE = {"nn2opt"}


# Sample HPGL content from Cadlink EngraveLab Expert v10 for Vision 1624 table
SAMPLE_HPGL = """IN;VS0.50;ZO123,1;VZ2.00;PA;PU0.000,0.000;PD18288.000,0.000;SP;"""

# Longer sample with multiple paths - simulates text entry left-to-right
COMPLEX_SAMPLE_HPGL = """
IN;
VS0.50;
ZO123,1;
VZ2.00;
PA;
PU0.000,0.000;
PD1000.000,0.000;
PU1200.000,0.000;
PD2200.000,0.000;
PU2400.000,0.000;
PD3400.000,0.000;
PU3600.000,0.000;
PD4600.000,0.000;
PU5000.000,1000.000;
PD6000.000,1000.000;
PU6200.000,1000.000;
PD7200.000,1000.000;
SP;
"""


def process_user_file(
    input_path: Path,
    optimize: bool = True,
    same_row_preference: float = 1.0,
) -> int:
    """Process a user-specified PLT file and generate diagnostics.

    Args:
        input_path: Path to the user's PLT/HPGL file.
        optimize: Whether to run optimization pipeline (default True).
        same_row_preference: Penalty multiplier for y-differences (default 1.0).

    Returns:
        Exit code (0 for success).
    """
    print("PLT-Optimizer - Processing User File")
    print("=" * 60)
    print(f"Input file: {input_path}")

    if not input_path.exists():
        print(f"\nError: File not found: {input_path}", file=sys.stderr)
        return 1

    text_logger = get_text_logger()
    metrics_logger = get_metrics_logger()

    try:
        # Parse the file
        parser = PLTParser()
        writer = PLTWriter()
        text_logger.info(f"Parsing user file: {input_path}")
        doc = parser.parse_file(input_path)

        print(f"\nDocument statistics:")
        print(f"  Header commands: {len(doc.header_commands)}")
        print(f"  Stroke paths: {len(doc.stroke_paths)}")
        print(f"  Total segments: {doc.total_segments}")
        print(f"  Cutting distance: {doc.cutting_distance():,.2f}")

        # Calculate before rapid travel
        metrics_calc = MetricsCalculator()
        original_rapid = metrics_calc.calculate_original_travel_distance(doc)
        print(f"  Rapid travel (before): {original_rapid:,.2f}")

        if not optimize:
            # Generate only the diagnostic plot without optimization
            output_stem = input_path.stem
            plot_path = input_path.parent / f"{output_stem}_diagnostic.png"

            text_logger.info("Generating diagnostic plot")
            fig = plot_plt_document(
                doc,
                output_path=plot_path,
                title=f"Toolpath Diagnostic: {input_path.name}",
            )

            print(f"\nDiagnostic plot saved to: {plot_path}")

            import matplotlib.pyplot as plt
            plt.close(fig)

            # Write the unoptimized PLT file (parse → write round-trip for validation)
            text_logger.info("Writing unoptimized PLT file for comparison")
            unoptimized_plt_path = input_path.parent / f"{output_stem}_reassembled.plt"
            writer.write_file(doc, unoptimized_plt_path)

            # Validate the parse→write pipeline against original
            validation_ok = validate_output_file(input_path, unoptimized_plt_path)
            if not validation_ok:
                text_logger.warning("Round-trip validation found critical errors")
            else:
                print(f"\n  Round-trip validation passed (parse → write produces semantically equivalent output)")

            job_id = f"user_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            metrics_logger.log_job(
                job_id=job_id,
                original_file=input_path,
                optimized_file=unoptimized_plt_path,
                original_distance=original_rapid,
                optimized_distance=original_rapid,
                status="diagnostic",
            )
            print(f"\n✓ Processing complete (no optimization)")
            return 0

        # Run full optimization pipeline
        text_logger.info("Starting optimization pipeline")
        before_plot_path, after_plot_path, stats = demonstrate_optimization_pipeline(
            doc,
            output_prefix=input_path.stem,
            same_row_preference=same_row_preference if optimize else 1.0,
        )

        # Write optimized PLT file
        output_stem = input_path.stem
        optimized_plt_path = input_path.parent / f"{output_stem}_optimized.plt"
        writer.write_file(doc, optimized_plt_path)

        # Validate the output file against original
        validation_ok = validate_output_file(input_path, optimized_plt_path)
        if not validation_ok:
            text_logger.warning("Output validation found critical errors")

        print(f"\n✓ Optimization complete")
        print(f"  Before plot: {before_plot_path}")
        print(f"  After plot: {after_plot_path}")
        print(f"  Optimized PLT: {optimized_plt_path}")

        # Log metrics
        job_id = f"user_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        metrics_logger.log_job(
            job_id=job_id,
            original_file=input_path,
            optimized_file=optimized_plt_path,
            original_distance=stats["before_rapid_distance"],
            optimized_distance=stats["after_rapid_distance"],
            status="success",
        )

        return 0

    except Exception as e:
        text_logger.error(f"Failed to process {input_path}: {e}")
        print(f"\nError: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


def run_single_strategy_on_file(
    input_path: Path,
    strategy_name: str,
    same_row_preference: float = 1.0,
) -> int:
    """Run a single specific optimization strategy on a PLT file.

    Args:
        input_path: Path to the user's PLT/HPGL file.
        strategy_name: Name of the strategy from STRATEGY_REGISTRY.
        same_row_preference: Penalty multiplier for y-differences.

    Returns:
        Exit code (0 for success).
    """
    print(f"PLT-Optimizer - Running {strategy_name} on {input_path.name}")
    print("=" * 60)

    if not input_path.exists():
        print(f"\nError: File not found: {input_path}", file=sys.stderr)
        return 1

    text_logger = get_text_logger()
    metrics_logger = get_metrics_logger()

    try:
        parser = PLTParser()
        writer = PLTWriter()
        text_logger.info(f"Parsing user file: {input_path}")
        doc = parser.parse_file(input_path)

        print(f"\nDocument statistics (before simplification):")
        print(f"  Stroke paths: {len(doc.stroke_paths)}")
        print(f"  Total segments: {doc.total_segments}")

        # Preserve original document before simplification for baseline calculations
        original_doc = doc
        original_cutting = original_doc.cutting_distance()
        original_rapid = original_doc.rapid_distance()

        doc = remove_redundant_strokes(doc)
        text_logger.info("Simplified document by removing redundant strokes")

        metrics_calc = MetricsCalculator()

        profiler = Profiler()
        profile_result = profiler.profile(doc)

        chunker = Chunker(config=ChunkerConfig(threshold_multiplier=2.0))
        blocks = chunker.chunk(
            doc.stroke_paths,
            profile_result.baseline_extent,
            is_structural=profile_result.is_structural,
        )

        strategy_class = STRATEGY_REGISTRY[strategy_name]
        if strategy_name in _STRATEGIES_WITH_SAME_ROW_PREFERENCE:
            optimizer = OptimizerEngine(strategy=strategy_class(same_row_preference=same_row_preference))
        else:
            optimizer = OptimizerEngine(strategy=strategy_class())
        opt_start_time = time.perf_counter()
        optimization_result = optimizer.optimize(blocks)
        opt_elapsed_ms = (time.perf_counter() - opt_start_time) * 1000

        reassembler = Reassembler()
        optimized_doc = reassembler.reassemble(doc, blocks, optimization_result)

        # Calculate percent changes from original to optimized
        optimized_rapid = optimized_doc.rapid_distance()
        optimized_cutting = optimized_doc.cutting_distance()

        rapid_change_pct = ((optimized_rapid - original_rapid) / original_rapid * 100) if original_rapid > 0 else 0.0
        cutting_change_pct = ((optimized_cutting - original_cutting) / original_cutting * 100) if original_cutting > 0 else 0.0

        total_original = original_rapid + original_cutting
        total_optimized = optimized_rapid + optimized_cutting
        total_change_pct = ((total_optimized - total_original) / total_original * 100) if total_original > 0 else 0.0

        before_plot_path = input_path.parent / f"{input_path.stem}_before.png"
        after_plot_path = input_path.parent / f"{input_path.stem}_after_{strategy_name}.png"

        fig_before = plot_plt_document(
            original_doc,
            output_path=before_plot_path,
            title=f"Original: Rapid={original_rapid / 1000:.2f} in, Cutting={original_cutting / 1000:.2f} in",
            rapid_travel_inches=original_rapid / 1000,
        )
        import matplotlib.pyplot as plt
        plt.close(fig_before)

        fig_after = plot_plt_document(
            optimized_doc,
            output_path=after_plot_path,
            title=f"{optimizer.strategy.name}: Rapid {rapid_change_pct:+.1f}%, Cutting {cutting_change_pct:+.1f}%, Total {total_change_pct:+.1f}% ({opt_elapsed_ms:.0f} ms)",
            rapid_travel_inches=optimized_rapid / 1000,
        )
        plt.close(fig_after)

        writer.write_file(optimized_doc, input_path.parent / f"{input_path.stem}_optimized.plt")

        # Validate the output file against original
        optimized_plt_path = input_path.parent / f"{input_path.stem}_optimized.plt"
        validation_ok = validate_output_file(input_path, optimized_plt_path)
        if not validation_ok:
            text_logger.warning("Output validation found critical errors")

        print(f"\n✓ Optimization complete")
        print(f"  Strategy: {optimizer.strategy.name}")
        print(f"  Before plot: {before_plot_path}")
        print(f"  After plot: {after_plot_path}")
        print(f"  Optimization time: {opt_elapsed_ms:.2f} ms")

        job_id = f"user_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        metrics_logger.log_job(
            job_id=job_id,
            original_file=input_path,
            optimized_file=input_path.parent / f"{input_path.stem}_optimized.plt",
            original_distance=original_rapid,
            optimized_distance=optimized_rapid,
            status="success",
        )

        return 0

    except Exception as e:
        text_logger.error(f"Failed to process {input_path}: {e}")
        print(f"\nError: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


def write_strategy_comparison_csv(
    results: dict[str, dict],
    output_path: Path,
) -> None:
    """Write strategy comparison results to a CSV file.

    Args:
        results: Dict mapping strategy names to result dictionaries.
        output_path: Destination path for the CSV file.
    """
    import csv

    fieldnames = [
        "strategy",
        "before_rapid_distance",
        "after_rapid_distance",
        "distance_saved",
        "percent_improvement",
        "optimization_time_ms",
        "blocks_created",
    ]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for strategy_name, stats in results.items():
            row = {
                "strategy": strategy_name,
                "before_rapid_distance": f"{stats['before_rapid_distance']:.2f}",
                "after_rapid_distance": f"{stats['after_rapid_distance']:.2f}",
                "distance_saved": f"{stats['distance_saved']:.2f}",
                "percent_improvement": f"{stats['percent_improvement']:.1f}",
                "optimization_time_ms": f"{stats['optimization_time_ms']:.2f}",
                "blocks_created": stats["blocks_created"],
            }
            writer.writerow(row)


def run_all_strategies_on_file(
    input_path: Path,
    same_row_preference: float = 1.0,
) -> int:
    """Run all optimization strategies on a PLT file.

    Args:
        input_path: Path to the user's PLT/HPGL file.
        same_row_preference: Penalty multiplier for y-differences.

    Returns:
        Exit code (0 for success).
    """
    print(f"PLT-Optimizer - Running all strategies on {input_path.name}")
    print("=" * 60)

    if not input_path.exists():
        print(f"\nError: File not found: {input_path}", file=sys.stderr)
        return 1

    text_logger = get_text_logger()
    metrics_logger = get_metrics_logger()

    try:
        parser = PLTParser()
        writer = PLTWriter()
        text_logger.info(f"Parsing user file: {input_path}")
        doc = parser.parse_file(input_path)

        print(f"\nDocument statistics (before simplification):")
        print(f"  Stroke paths: {len(doc.stroke_paths)}")
        print(f"  Total segments: {doc.total_segments}")

        # Preserve original document before simplification for baseline calculations
        original_doc = doc
        original_cutting = original_doc.cutting_distance()
        original_rapid = original_doc.rapid_distance()

        doc = remove_redundant_strokes(doc)
        text_logger.info("Simplified document by removing redundant strokes")

        metrics_calc = MetricsCalculator()
        print(f"  Rapid travel (before): {original_rapid:,.2f}")

        profiler = Profiler()
        profile_result = profiler.profile(doc)

        chunker = Chunker(config=ChunkerConfig(threshold_multiplier=2.0))
        blocks = chunker.chunk(
            doc.stroke_paths,
            profile_result.baseline_extent,
            is_structural=profile_result.is_structural,
        )
        block_count = len(blocks)

        before_plot_path = input_path.parent / f"{input_path.stem}_before.png"
        fig_before = plot_plt_document(
            original_doc,
            output_path=before_plot_path,
            title=f"Original: Rapid={original_rapid / 1000:.2f} in, Cutting={original_cutting / 1000:.2f} in",
            rapid_travel_inches=original_rapid / 1000,
        )
        import matplotlib.pyplot as plt
        plt.close(fig_before)

        # Collect results for CSV
        strategy_results: dict[str, dict] = {}

        print(f"\nRunning all strategies...")
        for strategy_name, strategy_class in STRATEGY_REGISTRY.items():
            if strategy_name in _STRATEGIES_WITH_SAME_ROW_PREFERENCE:
                optimizer = OptimizerEngine(strategy=strategy_class(same_row_preference=same_row_preference))
            else:
                optimizer = OptimizerEngine(strategy=strategy_class())
            opt_start_time = time.perf_counter()
            optimization_result = optimizer.optimize(blocks)
            opt_elapsed_ms = (time.perf_counter() - opt_start_time) * 1000

            reassembler = Reassembler()
            optimized_doc = reassembler.reassemble(doc, blocks, optimization_result)

            # Calculate percent changes from original to optimized
            optimized_rapid = optimized_doc.rapid_distance()
            optimized_cutting = optimized_doc.cutting_distance()

            rapid_change_pct = ((optimized_rapid - original_rapid) / original_rapid * 100) if original_rapid > 0 else 0.0
            cutting_change_pct = ((optimized_cutting - original_cutting) / original_cutting * 100) if original_cutting > 0 else 0.0

            total_original = original_rapid + original_cutting
            total_optimized = optimized_rapid + optimized_cutting
            total_change_pct = ((total_optimized - total_original) / total_original * 100) if total_original > 0 else 0.0

            after_plot_path = input_path.parent / f"{input_path.stem}_after_{strategy_name}.png"
            fig_after = plot_plt_document(
                optimized_doc,
                output_path=after_plot_path,
                title=f"{optimizer.strategy.name}: Rapid {rapid_change_pct:+.1f}%, Cutting {cutting_change_pct:+.1f}%, Total {total_change_pct:+.1f}% ({opt_elapsed_ms:.0f} ms)",
                rapid_travel_inches=optimized_rapid / 1000,
            )
            plt.close(fig_after)

            # Store results for CSV
            strategy_results[strategy_name] = {
                "before_rapid_distance": original_rapid,
                "after_rapid_distance": optimized_rapid,
                "distance_saved": original_rapid - optimized_rapid,
                "percent_improvement": rapid_change_pct,
                "optimization_time_ms": opt_elapsed_ms,
                "blocks_created": block_count,
            }

            print(f"  {strategy_name}: Rapid {rapid_change_pct:+.1f}%, Cutting {cutting_change_pct:+.1f}%, Total {total_change_pct:+.1f}% ({opt_elapsed_ms:.0f} ms)")

        # Write CSV comparison file
        csv_path = input_path.parent / f"{input_path.stem}_strategy_comparison.csv"
        write_strategy_comparison_csv(strategy_results, csv_path)
        print(f"  Strategy comparison CSV: {csv_path}")

        writer.write_file(doc, input_path.parent / f"{input_path.stem}_optimized.plt")

        print(f"\n✓ All strategies complete")
        print(f"  Before plot: {before_plot_path}")
        for strategy_name in STRATEGY_REGISTRY:
            after_path = input_path.parent / f"{input_path.stem}_after_{strategy_name}.png"
            print(f"  After plot ({strategy_name}): {after_path}")

        job_id = f"user_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        metrics_logger.log_job(
            job_id=job_id,
            original_file=input_path,
            optimized_file=None,
            original_distance=original_rapid,
            optimized_distance=original_rapid,
            status="all_strategies",
        )

        return 0

    except Exception as e:
        text_logger.error(f"Failed to process {input_path}: {e}")
        print(f"\nError: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


def create_sample_plt_file(output_path: Path) -> None:
    """Create a sample PLT file for testing.

    Args:
        output_path: Destination path for the sample .plt file.
    """
    content = SAMPLE_HPGL.strip()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    print(f"Created sample PLT file: {output_path}")


def demonstrate_logging() -> None:
    """Demonstrate the dual logging topology."""
    print("\n" + "=" * 60)
    print("DEMONSTRATING DUAL LOGGING TOPOLOGY")
    print("=" * 60)

    # Get loggers
    text_logger = get_text_logger()
    metrics_logger = get_metrics_logger()

    # Text logging examples
    text_logger.info("Starting PLT optimization workflow")
    text_logger.debug(f"Processing sample HPGL content ({len(SAMPLE_HPGL)} chars)")

    # Metrics logging example
    job_id = f"demo_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    metrics_logger.log_job(
        job_id=job_id,
        original_file=Path("examples/sample.plt"),
        optimized_file=None,  # No optimization in this demo
        original_distance=18288.0,
        optimized_distance=18288.0,
        status="demo",
    )

    print(f"\nText log output: See logs/optimizer.log")
    print(f"Metrics log output: See logs/job_metrics.csv")


def demonstrate_parsing_and_writing() -> tuple[Path, Path]:
    """Demonstrate parsing, identity validation, and file writing.

    Returns:
        Tuple of (sample_path, verified_output_path).
    """
    print("\n" + "=" * 60)
    print("DEMONSTRATING PARSING AND WRITING")
    print("=" * 60)

    # Create sample file
    sample_path = Path("examples/sample.plt")
    create_sample_plt_file(sample_path)

    # Parse
    parser = PLTParser()
    text_logger = get_text_logger()

    text_logger.info(f"Parsing {sample_path}")
    doc = parser.parse_file(sample_path)

    print(f"\nParsed document structure:")
    print(f"  Header commands: {len(doc.header_commands)}")
    print(f"  Stroke paths: {len(doc.stroke_paths)}")
    print(f"  Footer commands: {len(doc.footer_commands)}")

    # Write back
    writer = PLTWriter()
    output_path = Path("examples/output_verified.plt")

    text_logger.info(f"Writing verified output to {output_path}")
    writer.write_file(doc, output_path)

    # Validate round-trip
    is_valid, errors = writer.validate_output(
        doc,
        output_path.read_text(encoding="utf-8")
    )

    print(f"\nIdentity validation: {'PASSED' if is_valid else 'FAILED'}")
    if errors:
        for error in errors:
            print(f"  Error: {error}")

    return sample_path, output_path


def demonstrate_diagnostics_plot(output_path: Path) -> Path:
    """Generate and save diagnostic plot.

    Args:
        output_path: Path to the PLT file to visualize.

    Returns:
        Path to the generated plot file.
    """
    print("\n" + "=" * 60)
    print("DEMONSTRATING DIAGNOSTIC PLOTTING")
    print("=" * 60)

    text_logger = get_text_logger()

    # Parse for plotting
    parser = PLTParser()
    doc = parser.parse_file(output_path)

    print(f"\nDocument statistics:")
    print(f"  Total segments: {doc.total_segments}")
    print(f"  Cutting distance: {doc.cutting_distance():,.2f}")
    print(f"  Rapid travel: {doc.rapid_distance():,.2f}")

    # Generate plot
    text_logger.info("Generating diagnostic plot")
    plot_path = Path("examples/toolpath_diagnostic.png")

    fig = plot_plt_document(
        doc,
        output_path=plot_path,
        title="PLT-Optimizer Diagnostic View",
    )

    print(f"\nDiagnostic plot saved to: {plot_path}")

    # Close figure to free memory
    import matplotlib.pyplot as plt
    plt.close(fig)

    return plot_path


def demonstrate_complex_sample() -> tuple[Path, Path]:
    """Demonstrate with the more complex sample.

    Returns:
        Tuple of (plt_output_path, plot_output_path).
    """
    print("\n" + "=" * 60)
    print("DEMONSTRATING COMPLEX SAMPLE")
    print("=" * 60)

    parser = PLTParser()
    writer = PLTWriter()

    text_logger = get_text_logger()
    text_logger.info("Parsing complex HPGL sample")

    doc = parser.parse_string(COMPLEX_SAMPLE_HPGL.strip())

    print(f"\nComplex document statistics:")
    print(f"  Header commands: {len(doc.header_commands)}")
    print(f"  Stroke paths: {len(doc.stroke_paths)}")
    print(f"  Total segments: {doc.total_segments}")
    print(f"  Cutting distance: {doc.cutting_distance():,.2f}")

    # Write and plot
    complex_output = Path("examples/complex_sample.plt")
    complex_plot = Path("examples/complex_toolpath.png")
    writer.write_file(doc, complex_output)

    fig = plot_plt_document(
        doc,
        output_path=complex_plot,
        title="Complex Toolpath Diagnostic",
    )

    print(f"\nComplex sample written to: {complex_output}")
    print(f"Plot saved to: {complex_plot}")

    import matplotlib.pyplot as plt
    plt.close(fig)

    return complex_output, complex_plot


def demonstrate_optimization_pipeline(
    doc: PLTDocument,
    output_prefix: str = "optimized",
    same_row_preference: float = 1.0,
) -> tuple[Path, Path, dict]:
    """Run the full optimization pipeline and generate before/after comparison.

    Args:
        doc: The parsed PLTDocument to optimize.
        output_prefix: Prefix for output file names.
        same_row_preference: Penalty multiplier for y-differences (default 1.0).

    Returns:
        Tuple of (before_plot_path, after_plot_path, stats_dict).
    """
    print("\n" + "=" * 60)
    print("RUNNING OPTIMIZATION PIPELINE")
    print("=" * 60)

    text_logger = get_text_logger()
    metrics_calc = MetricsCalculator()

    # Calculate before statistics (preserve original_doc for before plot)
    original_doc = doc
    original_cutting = original_doc.cutting_distance()
    original_rapid = metrics_calc.calculate_original_travel_distance(original_doc)
    stroke_count = original_doc.total_segments

    print(f"\n[BEFORE OPTIMIZATION]")
    print(f"  Total strokes: {stroke_count}")
    print(f"  Stroke paths: {len(original_doc.stroke_paths)}")
    print(f"  Rapid travel distance: {original_rapid:,.2f}")

    # Step 1.5: Simplify - Remove redundant overlapping strokes
    doc = remove_redundant_strokes(doc)
    text_logger.info("Step 1.5/4: Simplified document by removing redundant strokes")
    stroke_count = doc.total_segments
    print(f"\n[AFTER SIMPLIFICATION]")
    print(f"  Total strokes: {stroke_count}")
    print(f"  Stroke paths: {len(doc.stroke_paths)}")

    # Step 2: Profile - Calculate baseline extent
    text_logger.info("Step 2/4: Profiling document for baseline extent")
    profiler = Profiler()
    profile_result = profiler.profile(doc)

    print(f"\n  Profiler results:")
    print(f"    Baseline extent (95th percentile): {profile_result.baseline_extent:.2f}")
    print(f"    Median DX: {profile_result.median_dx:.2f}")
    print(f"    Median DY: {profile_result.median_dy:.2f}")
    if profile_result.is_structural:
        print(f"    Structural file detected - using 1:1 block routing")

    # Step 3: Chunk - Group strokes into MacroBlocks
    text_logger.info("Step 3/5: Chunking stroke paths into MacroBlocks")
    chunker = Chunker(config=ChunkerConfig(threshold_multiplier=2.0))
    blocks = chunker.chunk(
        doc.stroke_paths,
        profile_result.baseline_extent,
        is_structural=profile_result.is_structural,
    )

    print(f"\n  Chunker results:")
    print(f"    MacroBlocks created: {len(blocks)}")

    # Step 4: Optimize - Find optimal traversal order
    text_logger.info("Step 4/5: Optimizing block traversal order")
    optimizer = OptimizerEngine(
        strategy=NearestNeighbor2OptStrategy(same_row_preference=same_row_preference)
    )
    opt_start_time = time.perf_counter()
    optimization_result = optimizer.optimize(blocks)
    opt_elapsed_ms = (time.perf_counter() - opt_start_time) * 1000

    print(f"\n  Optimizer results:")
    print(f"    Strategy: {optimizer.strategy.name}")
    print(f"    Blocks in optimized sequence: {optimization_result.block_count}")
    print(f"    Optimization time: {opt_elapsed_ms:.2f} ms")

    # Step 5: Reassemble - Rebuild PLTDocument with optimized order
    text_logger.info("Step 5/5: Reassembling document with optimized block order")
    reassembler = Reassembler()
    optimized_doc = reassembler.reassemble(doc, blocks, optimization_result)

    # Calculate percent changes from original to optimized
    # Use optimized_doc.rapid_distance() for accurate total rapid travel including intra-block moves
    optimized_rapid = optimized_doc.rapid_distance()
    optimized_cutting = optimized_doc.cutting_distance()

    rapid_change_pct = ((optimized_rapid - original_rapid) / original_rapid * 100) if original_rapid > 0 else 0.0
    cutting_change_pct = ((optimized_cutting - original_cutting) / original_cutting * 100) if original_cutting > 0 else 0.0

    total_original = original_rapid + original_cutting
    total_optimized = optimized_rapid + optimized_cutting
    total_change_pct = ((total_optimized - total_original) / total_original * 100) if total_original > 0 else 0.0

    print(f"\n[AFTER OPTIMIZATION]")
    print(f"  Rapid travel distance: {optimized_rapid:,.2f}")

    print(f"\n[OPTIMIZATION SUMMARY]")
    print(f"  Distance saved (rapid): {original_rapid - optimized_rapid:,.2f}")
    print(f"  Percent improvement (total): {total_change_pct:.1f}%")

    # Generate before plot
    text_logger.info("Generating before-optimization plot")
    before_plot_path = Path(f"examples/{output_prefix}_before.png")
    fig_before = plot_plt_document(
        original_doc,
        output_path=before_plot_path,
        title=f"Original: Rapid={original_rapid / 1000:.2f} in, Cutting={original_cutting / 1000:.2f} in",
        rapid_travel_inches=original_rapid / 1000,
    )
    import matplotlib.pyplot as plt
    plt.close(fig_before)

    # Generate after plot
    text_logger.info("Generating after-optimization plot")
    after_plot_path = Path(f"examples/{output_prefix}_after.png")
    fig_after = plot_plt_document(
        optimized_doc,
        output_path=after_plot_path,
        title=f"{optimizer.strategy.name}: Rapid {rapid_change_pct:+.1f}%, Cutting {cutting_change_pct:+.1f}%, Total {total_change_pct:+.1f}% ({opt_elapsed_ms:.0f} ms)",
        rapid_travel_inches=optimized_rapid / 1000,
    )
    plt.close(fig_after)

    stats = {
        "before_strokes": stroke_count,
        "before_paths": len(original_doc.stroke_paths),
        "before_rapid_distance": original_rapid,
        "after_rapid_distance": optimized_rapid,
        "blocks_created": len(blocks),
        "distance_saved": total_original - total_optimized,
        "percent_improvement": total_change_pct,
        "optimization_time_ms": opt_elapsed_ms,
    }

    return before_plot_path, after_plot_path, stats


def demonstrate_all_strategies(
    doc: PLTDocument,
    output_prefix: str = "optimized",
    same_row_preference: float = 1.0,
) -> dict[str, tuple[Path, Path, dict]]:
    """Run full optimization pipeline with each registered strategy.

    Args:
        doc: The parsed PLTDocument to optimize.
        output_prefix: Prefix for output file names.
        same_row_preference: Penalty multiplier for y-differences (default 1.0).

    Returns:
        Dict mapping strategy names to (before_plot, after_plot, stats) tuples.
    """
    print("\n" + "=" * 60)
    print("RUNNING ALL OPTIMIZATION STRATEGIES")
    print("=" * 60)

    text_logger = get_text_logger()
    metrics_calc = MetricsCalculator()

    original_distance = metrics_calc.calculate_original_travel_distance(doc)
    stroke_count = doc.total_segments

    print(f"\n[BEFORE OPTIMIZATION]")
    print(f"  Total strokes: {stroke_count}")
    print(f"  Rapid travel distance: {original_distance:,.2f}")

    profiler = Profiler()
    profile_result = profiler.profile(doc)

    chunker = Chunker(config=ChunkerConfig(threshold_multiplier=2.0))
    blocks = chunker.chunk(
        doc.stroke_paths,
        profile_result.baseline_extent,
        is_structural=profile_result.is_structural,
    )
    block_count = len(blocks)

    before_plot_path = Path(f"examples/{output_prefix}_before.png")
    fig_before = plot_plt_document(
        doc,
        output_path=before_plot_path,
        title=f"Rapid Travel (before): {original_distance / 1000:,.2f} in",
        rapid_travel_inches=original_distance / 1000,
    )
    import matplotlib.pyplot as plt
    plt.close(fig_before)

    results: dict[str, tuple[Path, Path, dict]] = {}
    strategy_results: dict[str, dict] = {}

    for strategy_name, strategy_class in STRATEGY_REGISTRY.items():
        if strategy_name in _STRATEGIES_WITH_SAME_ROW_PREFERENCE:
            optimizer = OptimizerEngine(strategy=strategy_class(same_row_preference=same_row_preference))
        else:
            optimizer = OptimizerEngine(strategy=strategy_class())

        print(f"\n  Strategy: {optimizer.strategy.name}")

        opt_start_time = time.perf_counter()
        optimization_result = optimizer.optimize(blocks)
        opt_elapsed_ms = (time.perf_counter() - opt_start_time) * 1000

        reassembler = Reassembler()
        optimized_doc = reassembler.reassemble(doc, blocks, optimization_result)

        # Use optimized_doc.rapid_distance() for accurate total rapid travel including intra-block moves
        optimized_distance = optimized_doc.rapid_distance()
        savings, pct_improvement = metrics_calc.calculate_improvement(original_distance, optimized_distance)

        after_plot_path = Path(f"examples/{output_prefix}_after_{strategy_name}.png")
        fig_after = plot_plt_document(
            optimized_doc,
            output_path=after_plot_path,
            title=f"{optimizer.strategy.name}: Rapid Travel (after): {optimized_distance / 1000:,.2f} in ({pct_improvement:.1f}% improvement, {opt_elapsed_ms:.1f} ms)",
            rapid_travel_inches=optimized_distance / 1000,
        )
        plt.close(fig_after)

        stats = {
            "before_strokes": stroke_count,
            "before_paths": len(doc.stroke_paths),
            "before_rapid_distance": original_distance,
            "after_rapid_distance": optimized_distance,
            "blocks_created": block_count,
            "distance_saved": savings,
            "percent_improvement": pct_improvement,
            "optimization_time_ms": opt_elapsed_ms,
        }

        results[strategy_name] = (before_plot_path, after_plot_path, stats)

        # Collect for CSV
        strategy_results[strategy_name] = {
            "before_rapid_distance": original_distance,
            "after_rapid_distance": optimized_distance,
            "distance_saved": savings,
            "percent_improvement": pct_improvement,
            "optimization_time_ms": opt_elapsed_ms,
            "blocks_created": block_count,
        }

        print(f"    Rapid travel: {optimized_distance:,.2f} ({pct_improvement:.1f}% improvement) in {opt_elapsed_ms:.1f} ms")

    # Write CSV comparison file
    csv_path = Path(f"examples/{output_prefix}_strategy_comparison.csv")
    write_strategy_comparison_csv(strategy_results, csv_path)
    print(f"\n  Strategy comparison CSV: {csv_path}")

    return results


def demonstrate_full_optimization(same_row_preference: float = 1.0) -> list[Path]:
    """Demonstrate the full optimization pipeline with sample data.

    Args:
        same_row_preference: Penalty multiplier for y-differences (default 1.0).

    Returns:
        List of generated file paths.
    """
    print("\n" + "=" * 60)
    print("DEMONSTRATING FULL OPTIMIZATION PIPELINE")
    print("=" * 60)

    parser = PLTParser()
    text_logger = get_text_logger()

    # Use the complex sample which has multiple stroke paths
    text_logger.info("Parsing HPGL sample for optimization demo")
    doc = parser.parse_string(COMPLEX_SAMPLE_HPGL.strip())

    print(f"\nInput document:")
    print(f"  Stroke paths: {len(doc.stroke_paths)}")
    print(f"  Total segments: {doc.total_segments}")
    print(f"  Cutting distance: {doc.cutting_distance():,.2f}")

    # Run optimization
    before_path, after_path, stats = demonstrate_optimization_pipeline(
        doc,
        output_prefix="full_demo",
        same_row_preference=same_row_preference,
    )

    return [before_path, after_path]


def main() -> int:
    """Main entry point for the diagnostics demonstration.

    Returns:
        Exit code (0 for success).
    """
    parser = argparse.ArgumentParser(
        description="PLT-Optimizer Diagnostics and Optimization Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Process a specific PLT file with optimization:
  python examples/run_diagnostics.py /path/to/your/file.plt

  # Run demonstration mode (optimization + diagnostics):
  python examples/run_diagnostics.py

  # Diagnostics only (skip optimization):
  python examples/run_diagnostics.py --no-optimize

  # Run a specific strategy on a PLT file:
  python examples/run_diagnostics.py /path/to/your/file.plt --strategy nn2opt
  python examples/run_diagnostics.py /path/to/your/file.plt --strategy insertion
  python examples/run_diagnostics.py /path/to/your/file.plt --strategy christofides
  python examples/run_diagnostics.py /path/to/your/file.plt --strategy sa
  python examples/run_diagnostics.py /path/to/your/file.plt --strategy genetic

  # Run all strategies and generate comparison plots:
  python examples/run_diagnostics.py /path/to/your/file.plt --all-strategies

Available strategies: nn2opt, insertion, christofides, sa, genetic
""",
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        type=Path,
        default=None,
        help="Path to a PLT/HPGL file to process and visualize",
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
    parser.add_argument(
        "--strategy",
        choices=list(STRATEGY_REGISTRY.keys()),
        default=None,
        help="Run a specific optimization strategy only",
    )
    parser.add_argument(
        "--all-strategies",
        action="store_true",
        default=False,
        help="Run all optimization strategies and generate individual plots for each",
    )

    args = parser.parse_args()

    if args.all_strategies and args.strategy is not None:
        print("Error: Cannot use both --all-strategies and --strategy together", file=sys.stderr)
        return 1

    # If user provided an input file, process it directly
    if args.input_file is not None:
        optimize = not args.no_optimize
        if args.all_strategies:
            return run_all_strategies_on_file(
                args.input_file,
                same_row_preference=args.same_row_preference,
            )
        elif args.strategy is not None:
            return run_single_strategy_on_file(
                args.input_file,
                args.strategy,
                same_row_preference=args.same_row_preference,
            )
        else:
            return process_user_file(
                args.input_file,
                optimize=optimize,
                same_row_preference=args.same_row_preference,
            )

    # Otherwise run demonstration mode with sample data
    print("PLT-Optimizer Diagnostics Demonstration")
    print("=" * 60)

    generated_files: list[Path] = []

    try:
        # Step 1: Demonstrate logging
        demonstrate_logging()

        # Step 2: Parse, write, and validate
        sample_path, verified_path = demonstrate_parsing_and_writing()
        generated_files.extend([sample_path, verified_path])

        # Step 3: Generate diagnostic plot for simple sample
        simple_plot_path = demonstrate_diagnostics_plot(verified_path)
        generated_files.append(simple_plot_path)

        # Step 4: Demo with complex multi-path sample
        if args.no_optimize:
            print("\n" + "=" * 60)
            print("SKIPPING OPTIMIZATION (--no-optimize flag set)")
            print("=" * 60)
        else:
            # Step 5: Full optimization pipeline demonstration
            opt_paths = demonstrate_full_optimization(same_row_preference=args.same_row_preference)
            generated_files.extend(opt_paths)

            complex_plt_path, complex_plot_path = demonstrate_complex_sample()
            generated_files.extend([complex_plt_path, complex_plot_path])

        print("\n" + "=" * 60)
        print("DEMONSTRATION COMPLETE")
        print("=" * 60)
        print(f"\nGenerated files:")
        for path in generated_files:
            exists = "\u2713" if path.exists() else "\u2717"
            print(f"  {exists} {path}")
        print(f"\nLog files:")
        log_dir = Path("logs")
        for log_file in sorted(log_dir.glob("*")):
            size = log_file.stat().st_size
            size_str = f"({size:,} bytes)" if size < 1024 else f"({size / 1024:.1f} KB)"
            print(f"  - {log_file} {size_str}")

        return 0

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())