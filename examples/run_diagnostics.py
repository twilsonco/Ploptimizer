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
from plt_optimizer.core.writer import PLTWriter
from plt_optimizer.diagnostics.plotter import plot_plt_document
from plt_optimizer.utils.logging import (
    CSVMetricsLogger,
    TextLogger,
    get_metrics_logger,
    get_text_logger,
)


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

            job_id = f"user_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            metrics_logger.log_job(
                job_id=job_id,
                original_file=input_path,
                optimized_file=None,
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

        print(f"\nDocument statistics:")
        print(f"  Stroke paths: {len(doc.stroke_paths)}")
        print(f"  Total segments: {doc.total_segments}")

        metrics_calc = MetricsCalculator()
        original_rapid = metrics_calc.calculate_original_travel_distance(doc)
        print(f"  Rapid travel (before): {original_rapid:,.2f}")

        profiler = Profiler()
        profile_result = profiler.profile(doc)

        chunker = Chunker(config=ChunkerConfig(threshold_multiplier=2.0))
        blocks = chunker.chunk(doc.stroke_paths, profile_result.baseline_extent)

        strategy_class = STRATEGY_REGISTRY[strategy_name]
        if strategy_name in _STRATEGIES_WITH_SAME_ROW_PREFERENCE:
            optimizer = OptimizerEngine(strategy=strategy_class(same_row_preference=same_row_preference))
        else:
            optimizer = OptimizerEngine(strategy=strategy_class())
        optimization_result = optimizer.optimize(blocks)

        reassembler = Reassembler()
        optimized_doc = reassembler.reassemble(doc, blocks, optimization_result)

        optimized_distance = metrics_calc.calculate_optimized_travel_distance(optimization_result)
        savings, pct_improvement = metrics_calc.calculate_improvement(original_rapid, optimized_distance)

        before_plot_path = input_path.parent / f"{input_path.stem}_before.png"
        after_plot_path = input_path.parent / f"{input_path.stem}_after_{strategy_name}.png"

        fig_before = plot_plt_document(
            doc,
            output_path=before_plot_path,
            title=f"Before Optimization - Rapid Travel: {original_rapid / 1000:,.2f} in",
        )
        import matplotlib.pyplot as plt
        plt.close(fig_before)

        fig_after = plot_plt_document(
            optimized_doc,
            output_path=after_plot_path,
                title=f"{optimizer.strategy.name} After - Rapid Travel: {optimized_distance / 1000:,.2f} in ({pct_improvement:.1f}% improvement)",
        )
        plt.close(fig_after)

        writer.write_file(optimized_doc, input_path.parent / f"{input_path.stem}_optimized.plt")

        print(f"\n✓ Optimization complete")
        print(f"  Strategy: {optimizer.strategy.name}")
        print(f"  Before plot: {before_plot_path}")
        print(f"  After plot: {after_plot_path}")

        job_id = f"user_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        metrics_logger.log_job(
            job_id=job_id,
            original_file=input_path,
            optimized_file=input_path.parent / f"{input_path.stem}_optimized.plt",
            original_distance=original_rapid,
            optimized_distance=optimized_distance,
            status="success",
        )

        return 0

    except Exception as e:
        text_logger.error(f"Failed to process {input_path}: {e}")
        print(f"\nError: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


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

        print(f"\nDocument statistics:")
        print(f"  Stroke paths: {len(doc.stroke_paths)}")
        print(f"  Total segments: {doc.total_segments}")

        metrics_calc = MetricsCalculator()
        original_rapid = metrics_calc.calculate_original_travel_distance(doc)
        print(f"  Rapid travel (before): {original_rapid:,.2f}")

        profiler = Profiler()
        profile_result = profiler.profile(doc)

        chunker = Chunker(config=ChunkerConfig(threshold_multiplier=2.0))
        blocks = chunker.chunk(doc.stroke_paths, profile_result.baseline_extent)

        before_plot_path = input_path.parent / f"{input_path.stem}_before.png"
        fig_before = plot_plt_document(
            doc,
            output_path=before_plot_path,
            title=f"Before Optimization - Rapid Travel: {original_rapid / 1000:,.2f} in",
        )
        import matplotlib.pyplot as plt
        plt.close(fig_before)

        print(f"\nRunning all strategies...")
        for strategy_name, strategy_class in STRATEGY_REGISTRY.items():
            if strategy_name in _STRATEGIES_WITH_SAME_ROW_PREFERENCE:
                optimizer = OptimizerEngine(strategy=strategy_class(same_row_preference=same_row_preference))
            else:
                optimizer = OptimizerEngine(strategy=strategy_class())
            optimization_result = optimizer.optimize(blocks)

            reassembler = Reassembler()
            optimized_doc = reassembler.reassemble(doc, blocks, optimization_result)

            optimized_distance = metrics_calc.calculate_optimized_travel_distance(optimization_result)
            savings, pct_improvement = metrics_calc.calculate_improvement(original_rapid, optimized_distance)

            after_plot_path = input_path.parent / f"{input_path.stem}_after_{strategy_name}.png"
            fig_after = plot_plt_document(
                optimized_doc,
                output_path=after_plot_path,
           title=f"{optimizer.strategy.name} After - Rapid Travel: {optimized_distance / 1000:,.2f} in ({pct_improvement:.1f}% improvement)",
            )
            plt.close(fig_after)

            print(f"  {strategy_name}: {original_rapid:,.2f} -> {optimized_distance:,.2f} ({pct_improvement:.1f}% improvement)")

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

    # Calculate before statistics
    original_distance = metrics_calc.calculate_original_travel_distance(doc)
    stroke_count = doc.total_segments

    print(f"\n[BEFORE OPTIMIZATION]")
    print(f"  Total strokes: {stroke_count}")
    print(f"  Stroke paths: {len(doc.stroke_paths)}")
    print(f"  Rapid travel distance: {original_distance:,.2f}")

    # Step 1: Profile - Calculate baseline extent
    text_logger.info("Step 1/4: Profiling document for baseline extent")
    profiler = Profiler()
    profile_result = profiler.profile(doc)

    print(f"\n  Profiler results:")
    print(f"    Baseline extent (95th percentile): {profile_result.baseline_extent:.2f}")
    print(f"    Median DX: {profile_result.median_dx:.2f}")
    print(f"    Median DY: {profile_result.median_dy:.2f}")

    # Step 2: Chunk - Group strokes into MacroBlocks
    text_logger.info("Step 2/4: Chunking stroke paths into MacroBlocks")
    chunker = Chunker(config=ChunkerConfig(threshold_multiplier=2.0))
    blocks = chunker.chunk(doc.stroke_paths, profile_result.baseline_extent)

    print(f"\n  Chunker results:")
    print(f"    MacroBlocks created: {len(blocks)}")

    # Step 3: Optimize - Find optimal traversal order
    text_logger.info("Step 3/4: Optimizing block traversal order")
    optimizer = OptimizerEngine(
        strategy=NearestNeighbor2OptStrategy(same_row_preference=same_row_preference)
    )
    optimization_result = optimizer.optimize(blocks)

    print(f"\n  Optimizer results:")
    print(f"    Strategy: {optimizer.strategy.name}")
    print(f"    Blocks in optimized sequence: {optimization_result.block_count}")

    # Step 4: Reassemble - Rebuild PLTDocument with optimized order
    text_logger.info("Step 4/4: Reassembling document with optimized block order")
    reassembler = Reassembler()
    optimized_doc = reassembler.reassemble(doc, blocks, optimization_result)

    # Calculate after statistics
    optimized_distance = metrics_calc.calculate_optimized_travel_distance(optimization_result)
    savings, pct_improvement = metrics_calc.calculate_improvement(original_distance, optimized_distance)

    print(f"\n[AFTER OPTIMIZATION]")
    print(f"  Rapid travel distance: {optimized_distance:,.2f}")

    print(f"\n[OPTIMIZATION SUMMARY]")
    print(f"  Distance saved: {savings:,.2f}")
    print(f"  Percent improvement: {pct_improvement:.1f}%")

    # Generate before plot
    text_logger.info("Generating before-optimization plot")
    before_plot_path = Path(f"examples/{output_prefix}_before.png")
    fig_before = plot_plt_document(
        doc,
        output_path=before_plot_path,
        title=f"Before Optimization - Rapid Travel: {original_distance / 1000:,.2f} in",
    )
    import matplotlib.pyplot as plt
    plt.close(fig_before)

    # Generate after plot
    text_logger.info("Generating after-optimization plot")
    after_plot_path = Path(f"examples/{output_prefix}_after.png")
    fig_after = plot_plt_document(
        optimized_doc,
        output_path=after_plot_path,
        title=f"After Optimization - Rapid Travel: {optimized_distance / 1000:,.2f} in ({pct_improvement:.1f}% improvement)",
    )
    plt.close(fig_after)

    stats = {
        "before_strokes": stroke_count,
        "before_paths": len(doc.stroke_paths),
        "before_rapid_distance": original_distance,
        "after_rapid_distance": optimized_distance,
        "blocks_created": len(blocks),
        "distance_saved": savings,
        "percent_improvement": pct_improvement,
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
    blocks = chunker.chunk(doc.stroke_paths, profile_result.baseline_extent)

    before_plot_path = Path(f"examples/{output_prefix}_before.png")
    fig_before = plot_plt_document(
        doc,
        output_path=before_plot_path,
        title=f"Before Optimization - Rapid Travel: {original_distance / 1000:,.2f} in",
    )
    import matplotlib.pyplot as plt
    plt.close(fig_before)

    results: dict[str, tuple[Path, Path, dict]] = {}

    for strategy_name, strategy_class in STRATEGY_REGISTRY.items():
        if strategy_name in _STRATEGIES_WITH_SAME_ROW_PREFERENCE:
            optimizer = OptimizerEngine(strategy=strategy_class(same_row_preference=same_row_preference))
        else:
            optimizer = OptimizerEngine(strategy=strategy_class())

        print(f"\n  Strategy: {optimizer.strategy.name}")

        optimization_result = optimizer.optimize(blocks)

        reassembler = Reassembler()
        optimized_doc = reassembler.reassemble(doc, blocks, optimization_result)

        optimized_distance = metrics_calc.calculate_optimized_travel_distance(optimization_result)
        savings, pct_improvement = metrics_calc.calculate_improvement(original_distance, optimized_distance)

        after_plot_path = Path(f"examples/{output_prefix}_after_{strategy_name}.png")
        fig_after = plot_plt_document(
            optimized_doc,
            output_path=after_plot_path,
            title=f"{optimizer.strategy.name} - Rapid Travel: {optimized_distance / 1000:,.2f} in ({pct_improvement:.1f}% improvement)",
        )
        plt.close(fig_after)

        stats = {
            "before_strokes": stroke_count,
            "before_paths": len(doc.stroke_paths),
            "before_rapid_distance": original_distance,
            "after_rapid_distance": optimized_distance,
            "blocks_created": len(blocks),
            "distance_saved": savings,
            "percent_improvement": pct_improvement,
        }

        results[strategy_name] = (before_plot_path, after_plot_path, stats)

        print(f"    Rapid travel: {optimized_distance:,.2f} ({pct_improvement:.1f}% improvement)")

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