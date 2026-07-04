"""Batch benchmark tool for PLT-Optimizer.

This script processes every ``.plt`` file in a user-specified directory, runs
each registered optimization strategy against every file, and writes a CSV
report summarizing the results. Optimized PLT files and diagnostic plots are
written to a directory *adjacent* to the input directory.

Typical use case: when a file in production fails to process, this tool can
be pointed at a batch of real-world files to quickly identify which ones
break the pipeline and compare the effectiveness of each strategy across the
remaining ones.

Usage:
    python examples/benchmark.py /path/to/cad_files/
    python examples/benchmark.py /path/to/cad_files/ --same-row-preference 1.5

Output structure:
    <input_dir_name>_benchmark/
        report.csv                   # Per-file + per-strategy summary
        optimized/<strategy>/        # Optimized PLT files, one folder per strategy
        plots/                       # Before + after plots per file/strategy

The CSV has one row per input file. Performance data for each strategy is
spread across dedicated columns. The final column contains any error
messages (file-level parse failures or per-strategy failures).
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# Add project root to path for imports when running as script
sys.path.insert(0, str(Path(__file__).parent.parent))

from plt_optimizer.core.chunker import Chunker, ChunkerConfig
from plt_optimizer.core.optimizer import (
    ChristofidesStrategy,
    GeneticAlgorithmStrategy,
    InsertionHeuristicStrategy,
    NearestNeighbor2OptStrategy,
    OptimizerEngine,
    SimulatedAnnealingStrategy,
)
from plt_optimizer.core.parser import ParseError, PLTParser
from plt_optimizer.core.profiler import Profiler
from plt_optimizer.core.reassembler import Reassembler
from plt_optimizer.core.writer import PLTWriter
from plt_optimizer.diagnostics.plotter import plot_plt_document
from plt_optimizer.utils.geometry import remove_redundant_strokes
from plt_optimizer.utils.logging import get_metrics_logger, get_text_logger


# Registry of strategies to benchmark, in execution order.
STRATEGY_REGISTRY: dict[str, type] = {
    "nn2opt": NearestNeighbor2OptStrategy,
    "insertion": InsertionHeuristicStrategy,
    "christofides": ChristofidesStrategy,
    "sa": SimulatedAnnealingStrategy,
    "genetic": GeneticAlgorithmStrategy,
}

# Strategies that accept a same_row_preference parameter.
_STRATEGIES_WITH_SAME_ROW_PREFERENCE = {"nn2opt"}


def _build_csv_columns() -> list[str]:
    """Return the canonical CSV column order.

    The error_message column is intentionally last so that the most
    variable content does not affect alignment of the fixed-width
    preceding columns.
    """
    columns: list[str] = [
        "file_name",
        "status",
        "before_rapid_in",
        "before_cutting_in",
        "before_paths",
        "before_segments",
        "blocks_created",
    ]
    for strategy_name in STRATEGY_REGISTRY:
        columns.extend(
            [
                f"{strategy_name}_rapid_after_in",
                f"{strategy_name}_total_after_in",
                f"{strategy_name}_improvement_pct",
                f"{strategy_name}_time_ms",
            ]
        )
    columns.append("error_message")
    return columns


CSV_COLUMNS: list[str] = _build_csv_columns()


def _append_error(current: str, new_error: str) -> str:
    """Append a new error message to an existing one using a '; ' separator.

    Args:
        current: Existing accumulated error string (may be empty).
        new_error: New error message to append.

    Returns:
        Combined error string.
    """
    if not current:
        return new_error
    return f"{current}; {new_error}"


def find_plt_files(input_dir: Path) -> list[Path]:
    """Discover all ``.plt`` files in a directory (non-recursive).

    Args:
        input_dir: Directory to scan for PLT files.

    Returns:
        Sorted list of PLT file paths.
    """
    return sorted(input_dir.glob("*.plt"))


def build_output_directory(input_dir: Path) -> Path:
    """Create and return the adjacent output directory for benchmark results.

    Args:
        input_dir: Source directory containing PLT files.

    Returns:
        Newly created output directory adjacent to ``input_dir``.
    """
    output_dir = input_dir.parent / f"{input_dir.name}_benchmark"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "optimized").mkdir(exist_ok=True)
    (output_dir / "plots").mkdir(exist_ok=True)
    return output_dir


def _save_plot(
    doc,
    plot_path: Path,
    title: str,
    rapid_travel_inches: float,
    text_logger,
) -> None:
    """Generate and save a diagnostic plot, swallowing plot failures.

    Args:
        doc: PLTDocument to render.
        plot_path: Destination path for the PNG.
        title: Plot title.
        rapid_travel_inches: Rapid travel in inches for the legend.
        text_logger: Text logger for non-fatal plot errors.
    """
    try:
        fig = plot_plt_document(
            doc,
            output_path=plot_path,
            title=title,
            rapid_travel_inches=rapid_travel_inches,
        )
        import matplotlib.pyplot as plt

        plt.close(fig)
    except Exception as plot_err:  # noqa: BLE001 - plotting must never fail a row
        text_logger.warning(
            f"Failed to generate plot {plot_path.name}: {plot_err}"
        )


def _run_strategy(
    strategy_name: str,
    strategy_class: type,
    blocks,
    doc,
    before_rapid: float,
    before_cutting: float,
    input_path: Path,
    output_dir: Path,
    same_row_preference: float,
    metrics_logger,
    text_logger,
) -> tuple[float, float, float, float, bool]:
    """Run a single strategy and emit its outputs.

    Args:
        strategy_name: Strategy key from ``STRATEGY_REGISTRY``.
        strategy_class: Strategy class implementing ``OptimizationStrategy``.
        blocks: MacroBlocks to optimize.
        doc: Simplified PLTDocument used for reassembly.
        before_rapid: Rapid travel distance before optimization.
        before_cutting: Cutting distance before optimization.
        input_path: Source PLT path (for naming outputs).
        output_dir: Destination directory for outputs.
        same_row_preference: Penalty multiplier for y-differences.
        metrics_logger: CSV metrics logger.
        text_logger: Text logger.

    Returns:
        Tuple of ``(optimized_rapid, optimized_cutting, total_pct, opt_ms, success)``.
    """
    if strategy_name in _STRATEGIES_WITH_SAME_ROW_PREFERENCE:
        optimizer = OptimizerEngine(
            strategy=strategy_class(same_row_preference=same_row_preference)
        )
    else:
        optimizer = OptimizerEngine(strategy=strategy_class())

    opt_start = time.perf_counter()
    optimization_result = optimizer.optimize(blocks)
    opt_elapsed_ms = (time.perf_counter() - opt_start) * 1000

    reassembler = Reassembler()
    optimized_doc = reassembler.reassemble(doc, blocks, optimization_result)

    optimized_rapid = optimized_doc.rapid_distance()
    optimized_cutting = optimized_doc.cutting_distance()
    total_before = before_rapid + before_cutting
    total_after = optimized_rapid + optimized_cutting
    total_pct = (
        ((total_after - total_before) / total_before) * 100
        if total_before > 0
        else 0.0
    )

    strategy_output_dir = output_dir / "optimized" / strategy_name
    strategy_output_dir.mkdir(parents=True, exist_ok=True)
    optimized_plt_path = strategy_output_dir / f"{input_path.stem}_optimized.plt"

    writer = PLTWriter()
    writer.write_file(optimized_doc, optimized_plt_path)

    after_plot_path = (
        output_dir / "plots" / f"{input_path.stem}_after_{strategy_name}.png"
    )
    _save_plot(
        optimized_doc,
        after_plot_path,
        title=(
            f"{input_path.name} [{strategy_name}]: "
            f"Total {total_pct:+.1f}% ({opt_elapsed_ms:.0f} ms)"
        ),
        rapid_travel_inches=optimized_rapid / 1000,
        text_logger=text_logger,
    )

    metrics_logger.log_job(
        job_id=(
            f"{input_path.stem}_{strategy_name}_"
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        ),
        original_file=input_path,
        optimized_file=optimized_plt_path,
        original_distance=before_rapid,
        optimized_distance=optimized_rapid,
        status="success",
        method=strategy_name,
    )

    return optimized_rapid, optimized_cutting, total_pct, opt_elapsed_ms, True


def process_file(
    input_path: Path,
    output_dir: Path,
    same_row_preference: float,
    metrics_logger,
    text_logger,
) -> dict:
    """Process a single PLT file and return one CSV row of results.

    The returned dict always contains every column declared in
    :data:`CSV_COLUMNS`. Per-strategy columns default to empty strings
    so that downstream consumers see a consistent schema.

    Args:
        input_path: Path to the input PLT file.
        output_dir: Destination directory for optimized files and plots.
        same_row_preference: Penalty multiplier for y-differences.
        metrics_logger: CSV metrics logger instance.
        text_logger: Text logger instance.

    Returns:
        Dict mapping each CSV column name to its value (or empty string
        when not applicable).
    """
    row: dict = {col: "" for col in CSV_COLUMNS}
    row["file_name"] = input_path.name
    row["status"] = "failed"

    parser = PLTParser()
    try:
        original_doc = parser.parse_file(input_path)
    except (ParseError, OSError, ValueError) as parse_err:
        row["error_message"] = _append_error(
            row["error_message"], f"[parse] {type(parse_err).__name__}: {parse_err}"
        )
        text_logger.error(f"Failed to parse {input_path.name}: {parse_err}")
        metrics_logger.log_job(
            job_id=f"{input_path.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            original_file=input_path,
            optimized_file=None,
            original_distance=0.0,
            optimized_distance=0.0,
            status="failed",
            notes=str(parse_err)[:200],
        )
        return row

    # Capture baseline metrics from the un-simplified document.
    row["before_rapid_in"] = round(original_doc.rapid_distance() / 1000, 3)
    row["before_cutting_in"] = round(original_doc.cutting_distance() / 1000, 3)
    row["before_paths"] = len(original_doc.stroke_paths)
    row["before_segments"] = original_doc.total_segments

    # Save the "before" diagnostic plot for visual comparison.
    _save_plot(
        original_doc,
        output_dir / "plots" / f"{input_path.stem}_before.png",
        title=(
            f"{input_path.name}: Rapid={row['before_rapid_in']:.2f} in, "
            f"Cutting={row['before_cutting_in']:.2f} in"
        ),
        rapid_travel_inches=row["before_rapid_in"],
        text_logger=text_logger,
    )

    try:
        simplified_doc = remove_redundant_strokes(original_doc)

        profiler = Profiler()
        profile_result = profiler.profile(simplified_doc)

        chunker = Chunker(config=ChunkerConfig(threshold_multiplier=2.0))
        blocks = chunker.chunk(
            simplified_doc.stroke_paths,
            profile_result.baseline_extent,
            is_structural=profile_result.is_structural,
        )
        row["blocks_created"] = len(blocks)
    except Exception as setup_err:  # noqa: BLE001 - any failure here is fatal for the file
        row["error_message"] = _append_error(
            row["error_message"],
            f"[setup] {type(setup_err).__name__}: {setup_err}",
        )
        text_logger.error(
            f"Setup failed for {input_path.name}: {setup_err}"
        )
        text_logger.error(traceback.format_exc())
        metrics_logger.log_job(
            job_id=f"{input_path.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            original_file=input_path,
            optimized_file=None,
            original_distance=0.0,
            optimized_distance=0.0,
            status="failed",
            notes=f"setup: {setup_err}"[:200],
        )
        return row

    before_rapid = original_doc.rapid_distance()
    before_cutting = original_doc.cutting_distance()

    any_strategy_succeeded = False
    for strategy_name, strategy_class in STRATEGY_REGISTRY.items():
        try:
            optimized_rapid, optimized_cutting, total_pct, opt_ms, _ = (
                _run_strategy(
                    strategy_name=strategy_name,
                    strategy_class=strategy_class,
                    blocks=blocks,
                    doc=simplified_doc,
                    before_rapid=before_rapid,
                    before_cutting=before_cutting,
                    input_path=input_path,
                    output_dir=output_dir,
                    same_row_preference=same_row_preference,
                    metrics_logger=metrics_logger,
                    text_logger=text_logger,
                )
            )
            row[f"{strategy_name}_rapid_after_in"] = round(optimized_rapid / 1000, 3)
            row[f"{strategy_name}_total_after_in"] = (
                round((optimized_rapid + optimized_cutting) / 1000, 3)
            )
            row[f"{strategy_name}_improvement_pct"] = round(total_pct, 1)
            row[f"{strategy_name}_time_ms"] = round(opt_ms, 2)
            any_strategy_succeeded = True
        except Exception as strat_err:  # noqa: BLE001 - one strategy failing must not block others
            row[f"{strategy_name}_rapid_after_in"] = ""
            row[f"{strategy_name}_total_after_in"] = ""
            row[f"{strategy_name}_improvement_pct"] = ""
            row[f"{strategy_name}_time_ms"] = ""
            row["error_message"] = _append_error(
                row["error_message"],
                f"[{strategy_name}] {type(strat_err).__name__}: {strat_err}",
            )
            text_logger.error(
                f"Strategy {strategy_name} failed on {input_path.name}: {strat_err}"
            )
            metrics_logger.log_job(
                job_id=(
                    f"{input_path.stem}_{strategy_name}_"
                    f"{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                ),
                original_file=input_path,
                optimized_file=None,
                original_distance=before_rapid,
                optimized_distance=before_rapid,
                status="failed",
                method=strategy_name,
                notes=str(strat_err)[:200],
            )

    if any_strategy_succeeded:
        row["status"] = "success"
    return row


def write_report(rows: list[dict], output_path: Path) -> None:
    """Write collected rows to the CSV report.

    Args:
        rows: List of row dicts, each containing every column in
            :data:`CSV_COLUMNS`.
        output_path: Destination path for the CSV file.
    """
    if not rows:
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> int:
    """Entry point for the batch benchmark utility.

    Returns:
        Exit code (0 for success, 1 for invalid arguments).
    """
    parser = argparse.ArgumentParser(
        description=(
            "PLT-Optimizer Batch Benchmark - process every PLT file in a "
            "directory and write a CSV report comparing all registered "
            "optimization strategies."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python examples/benchmark.py /path/to/cad_files/\n"
            "  python examples/benchmark.py /path/to/cad_files/ "
            "--same-row-preference 1.5\n"
        ),
    )
    parser.add_argument(
        "input_dir",
        type=Path,
        help="Directory containing PLT files to process",
    )
    parser.add_argument(
        "--same-row-preference",
        type=float,
        default=1.0,
        help=(
            "Penalty multiplier for y-differences during greedy selection "
            "(default: 1.0, values > 1.0 prefer same-row blocks)"
        ),
    )

    args = parser.parse_args()

    input_dir: Path = args.input_dir
    if not input_dir.exists() or not input_dir.is_dir():
        print(f"Error: Input directory not found: {input_dir}", file=sys.stderr)
        return 1

    output_dir = build_output_directory(input_dir)

    text_logger = get_text_logger()
    metrics_logger = get_metrics_logger()

    plt_files = find_plt_files(input_dir)
    print("PLT-Optimizer Batch Benchmark")
    print(f"  Input:  {input_dir}")
    print(f"  Output: {output_dir}")
    print(f"  Files:  {len(plt_files)}")
    print(f"  Strategies: {', '.join(STRATEGY_REGISTRY.keys())}")
    print("=" * 60)

    if not plt_files:
        print("No PLT files found in input directory, exiting.")
        return 0

    rows: list[dict] = []
    success_count = 0
    failure_count = 0

    for index, plt_file in enumerate(plt_files, start=1):
        print(f"[{index}/{len(plt_files)}] {plt_file.name} ...", end=" ", flush=True)
        text_logger.info(f"Processing {plt_file}")
        row = process_file(
            plt_file,
            output_dir,
            args.same_row_preference,
            metrics_logger,
            text_logger,
        )
        rows.append(row)
        if row["status"] == "success":
            success_count += 1
            print("OK")
        else:
            failure_count += 1
            err = row["error_message"] or "(no error message recorded)"
            err_summary = err if len(err) <= 80 else f"{err[:77]}..."
            print(f"FAILED: {err_summary}")

    report_path = output_dir / "report.csv"
    write_report(rows, report_path)

    print()
    print("=" * 60)
    print("BENCHMARK COMPLETE")
    print(f"  Total files: {len(plt_files)}")
    print(f"  Successful:  {success_count}")
    print(f"  Failed:      {failure_count}")
    print(f"  Report:      {report_path}")
    print(f"  Optimized:   {output_dir / 'optimized'}")
    print(f"  Plots:       {output_dir / 'plots'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())