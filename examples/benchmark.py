"""Batch benchmark tool for PLT-Optimizer.

This script processes every ``.plt`` file in a user-specified directory, runs
each registered optimization strategy against every file, and writes two CSV
reports summarizing the results. Optimized PLT files and diagnostic plots are
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
        report.csv                   # Per-(file, strategy) summary
        ensemble_report.csv          # Synthetic ParallelEnsemble rows
        optimized/<strategy>/        # Optimized PLT files, one folder per strategy
        plots/                       # Before + after plots per file/strategy

The first CSV (``report.csv``) contains one row per registered strategy for
every input file, allowing per-strategy error reporting and per-strategy
distance-saved metrics. The second CSV (``ensemble_report.csv``) contains a
single row per file, simulating what the ParallelEnsemble strategy would
have produced: the ``strategy_name`` column holds the winning strategy's
name (selected by greatest total improvement %, ties broken by shortest
total distance then fastest runtime). Both CSVs share the same schema
defined in :data:`CSV_COLUMNS`.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

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

# Sentinel strategy name used in the per-strategy CSV when a file fails
# before any strategy is actually run.
_FILE_LEVEL_SENTINEL: str = "(file)"

# Sentinel used in the ensemble CSV when no strategy succeeded for a file.
_NO_WINNER_SENTINEL: str = "(none)"


def _build_csv_columns() -> list[str]:
    """Return the canonical CSV column order.

    The schema is shared between the per-strategy report and the synthetic
    ensemble report so the two CSVs can be diffed or concatenated.
    """
    return [
        "file_name",
        "strategy_name",
        "status",
        "before_rapid_in",
        "before_cutting_in",
        "before_paths",
        "before_segments",
        "blocks_created",
        "rapid_after_in",
        "cutting_after_in",
        "total_before_in",
        "total_after_in",
        "rapid_saved_in",
        "cutting_saved_in",
        "total_saved_in",
        "rapid_improvement_pct",
        "cutting_improvement_pct",
        "total_improvement_pct",
        "time_ms",
        "error_message",
    ]


CSV_COLUMNS: list[str] = _build_csv_columns()


def _empty_row(file_name: str) -> dict[str, Any]:
    """Return a fresh dict with every CSV column initialized to ``""``.

    Args:
        file_name: Value for the ``file_name`` column.

    Returns:
        New dict ready to be filled in by a strategy or sentinel handler.
    """
    row: dict[str, Any] = {col: "" for col in CSV_COLUMNS}
    row["file_name"] = file_name
    return row


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
    doc: Any,
    plot_path: Path,
    title: str,
    rapid_travel_inches: float,
    text_logger: Any,
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


def _populate_metrics(
    row: dict[str, Any],
    *,
    before_rapid: float,
    before_cutting: float,
    optimized_rapid: float,
    optimized_cutting: float,
    time_ms: float,
) -> None:
    """Populate the metric-related columns of a row in-place.

    All distances are stored in inches (rounded to 3 decimal places). The
    ``*_saved_in`` columns are ``before - after`` (positive when the
    optimization reduced the distance). Improvement percentages follow the
    same sign convention.

    Args:
        row: Row dict to populate (modified in place).
        before_rapid: Rapid travel distance before optimization (internal units).
        before_cutting: Cutting distance before optimization (internal units).
        optimized_rapid: Rapid travel distance after optimization (internal units).
        optimized_cutting: Cutting distance after optimization (internal units).
        time_ms: Wall-clock optimization time in milliseconds.
    """
    rapid_saved = before_rapid - optimized_rapid
    cutting_saved = before_cutting - optimized_cutting
    total_before = before_rapid + before_cutting
    total_after = optimized_rapid + optimized_cutting
    total_saved = total_before - total_after

    rapid_pct = (rapid_saved / before_rapid * 100) if before_rapid > 0 else 0.0
    cutting_pct = (
        (cutting_saved / before_cutting * 100) if before_cutting > 0 else 0.0
    )
    total_pct = (total_saved / total_before * 100) if total_before > 0 else 0.0

    row["rapid_after_in"] = round(optimized_rapid / 1000, 3)
    row["cutting_after_in"] = round(optimized_cutting / 1000, 3)
    row["total_before_in"] = round(total_before / 1000, 3)
    row["total_after_in"] = round(total_after / 1000, 3)
    row["rapid_saved_in"] = round(rapid_saved / 1000, 3)
    row["cutting_saved_in"] = round(cutting_saved / 1000, 3)
    row["total_saved_in"] = round(total_saved / 1000, 3)
    row["rapid_improvement_pct"] = round(rapid_pct, 2)
    row["cutting_improvement_pct"] = round(cutting_pct, 2)
    row["total_improvement_pct"] = round(total_pct, 2)
    row["time_ms"] = round(time_ms, 2)


def _run_strategy(
    strategy_name: str,
    strategy_class: type,
    blocks: Any,
    doc: Any,
    before_rapid: float,
    before_cutting: float,
    input_path: Path,
    output_dir: Path,
    same_row_preference: float,
    metrics_logger: Any,
    text_logger: Any,
) -> dict[str, Any]:
    """Run a single strategy and return a populated CSV row.

    On success, the returned dict has ``status == "success"`` and all metric
    columns filled. On any exception raised by the strategy, the dict has
    ``status == "failed"`` with the error captured in ``error_message`` and
    all other columns left empty.

    Args:
        strategy_name: Strategy key from ``STRATEGY_REGISTRY``.
        strategy_class: Strategy class implementing ``OptimizationStrategy``.
        blocks: MacroBlocks to optimize.
        doc: Simplified PLTDocument used for reassembly.
        before_rapid: Rapid travel distance before optimization (internal units).
        before_cutting: Cutting distance before optimization (internal units).
        input_path: Source PLT path (for naming outputs).
        output_dir: Destination directory for outputs.
        same_row_preference: Penalty multiplier for y-differences.
        metrics_logger: CSV metrics logger.
        text_logger: Text logger.

    Returns:
        Row dict containing every column declared in :data:`CSV_COLUMNS`.
    """
    row = _empty_row(input_path.name)
    row["strategy_name"] = strategy_name

    try:
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

        row["status"] = "success"
        _populate_metrics(
            row,
            before_rapid=before_rapid,
            before_cutting=before_cutting,
            optimized_rapid=optimized_rapid,
            optimized_cutting=optimized_cutting,
            time_ms=opt_elapsed_ms,
        )
    except Exception as strat_err:  # noqa: BLE001 - one strategy failing must not block others
        err_msg = f"{type(strat_err).__name__}: {strat_err}"
        row["status"] = "failed"
        row["error_message"] = f"[{strategy_name}] {err_msg}"
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
            notes=err_msg[:200],
        )
    return row


def process_file(
    input_path: Path,
    output_dir: Path,
    same_row_preference: float,
    metrics_logger: Any,
    text_logger: Any,
) -> list[dict[str, Any]]:
    """Process a single PLT file and return one CSV row per strategy.

    On file-level parse or setup failure, a single sentinel row is returned
    (with ``strategy_name == "(file)"``) carrying the failure in
    ``error_message`` so consumers always see a consistent schema.

    Args:
        input_path: Path to the input PLT file.
        output_dir: Destination directory for optimized files and plots.
        same_row_preference: Penalty multiplier for y-differences.
        metrics_logger: CSV metrics logger instance.
        text_logger: Text logger instance.

    Returns:
        List of row dicts. On file-level failure: one sentinel row. On
        success: one row per strategy in :data:`STRATEGY_REGISTRY` order.
        Every dict contains every column in :data:`CSV_COLUMNS`.
    """
    parser = PLTParser()
    try:
        original_doc = parser.parse_file(input_path)
    except (ParseError, OSError, ValueError) as parse_err:
        row = _empty_row(input_path.name)
        row["strategy_name"] = _FILE_LEVEL_SENTINEL
        row["status"] = "parse_failed"
        row["error_message"] = f"[parse] {type(parse_err).__name__}: {parse_err}"
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
        return [row]

    # Capture baseline metrics from the un-simplified document.
    before_rapid = original_doc.rapid_distance()
    before_cutting = original_doc.cutting_distance()
    before_paths = len(original_doc.stroke_paths)
    before_segments = original_doc.total_segments

    # Save the "before" diagnostic plot for visual comparison.
    _save_plot(
        original_doc,
        output_dir / "plots" / f"{input_path.stem}_before.png",
        title=(
            f"{input_path.name}: Rapid={before_rapid / 1000:.2f} in, "
            f"Cutting={before_cutting / 1000:.2f} in"
        ),
        rapid_travel_inches=before_rapid / 1000,
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
        blocks_created = len(blocks)
    except Exception as setup_err:  # noqa: BLE001 - any failure here is fatal for the file
        row = _empty_row(input_path.name)
        row["strategy_name"] = _FILE_LEVEL_SENTINEL
        row["status"] = "setup_failed"
        row["error_message"] = f"[setup] {type(setup_err).__name__}: {setup_err}"
        row["before_rapid_in"] = round(before_rapid / 1000, 3)
        row["before_cutting_in"] = round(before_cutting / 1000, 3)
        row["before_paths"] = before_paths
        row["before_segments"] = before_segments
        text_logger.error(f"Setup failed for {input_path.name}: {setup_err}")
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
        return [row]

    rows: list[dict[str, Any]] = []
    for strategy_name, strategy_class in STRATEGY_REGISTRY.items():
        row = _run_strategy(
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
        # Tag baseline file metrics on every per-strategy row.
        row["before_rapid_in"] = round(before_rapid / 1000, 3)
        row["before_cutting_in"] = round(before_cutting / 1000, 3)
        row["before_paths"] = before_paths
        row["before_segments"] = before_segments
        row["blocks_created"] = blocks_created
        rows.append(row)
    return rows


def _select_ensemble_winner(successful_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the winning strategy row for the ensemble report.

    Selection criteria (mirrors ``ParallelEnsembleStrategy``):
    1. Highest ``total_improvement_pct`` (most reduction in total distance).
    2. Tie → lowest ``total_after_in``.
    3. Tie → fastest ``time_ms``.

    Args:
        successful_rows: Per-strategy rows with ``status == "success"``.

    Returns:
        The winning row.
    """

    def _sort_key(r: dict[str, Any]) -> tuple[float, float, float]:
        time_value = float(r["time_ms"]) if r["time_ms"] != "" else 0.0
        return (
            -float(r["total_improvement_pct"]),
            float(r["total_after_in"]),
            time_value,
        )

    return min(successful_rows, key=_sort_key)


def build_ensemble_rows(per_strategy_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Synthesize a ParallelEnsemble CSV from per-strategy rows.

    For each file, picks the successful strategy with the greatest total
    improvement percentage (ties broken by shortest total distance, then
    fastest runtime). When no strategy succeeded, a sentinel row is emitted
    with ``strategy_name == "(none)"`` and a status of either
    ``all_strategies_failed`` (every strategy raised) or the sentinel row's
    status (``parse_failed`` / ``setup_failed``).

    The output rows use the same schema as :data:`CSV_COLUMNS` so the two
    CSVs can be compared or concatenated trivially.

    Args:
        per_strategy_rows: Flat list of per-(file, strategy) rows produced by
            :func:`process_file`.

    Returns:
        One ensemble row per file, mirroring the per-strategy schema but with
        the ``strategy_name`` column overwritten with the winning strategy's
        name (or ``"(none)"`` when nothing succeeded).
    """
    # Group rows by file while preserving input order.
    files_in_order: list[str] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in per_strategy_rows:
        file_name = row["file_name"]
        if file_name not in grouped:
            grouped[file_name] = []
            files_in_order.append(file_name)
        grouped[file_name].append(row)

    ensemble_rows: list[dict[str, Any]] = []
    for file_name in files_in_order:
        file_rows = grouped[file_name]
        successful = [r for r in file_rows if r["status"] == "success"]

        if successful:
            winner = _select_ensemble_winner(successful)
            ensemble_row = dict(winner)
            ensemble_row["strategy_name"] = winner["strategy_name"]
            ensemble_row["status"] = "success"
            ensemble_rows.append(ensemble_row)
            continue

        # No strategy succeeded. Prefer the file-level sentinel row when
        # present so the error message is preserved.
        sentinel = next(
            (r for r in file_rows if r["strategy_name"] == _FILE_LEVEL_SENTINEL),
            None,
        )
        if sentinel is not None:
            ensemble_row = dict(sentinel)
            ensemble_row["strategy_name"] = _NO_WINNER_SENTINEL
            ensemble_rows.append(ensemble_row)
        else:
            # Every strategy failed without any file-level failure recorded;
            # synthesize a row from the first per-strategy row's metadata.
            base = file_rows[0]
            ensemble_row = dict(base)
            ensemble_row["strategy_name"] = _NO_WINNER_SENTINEL
            ensemble_row["status"] = "all_strategies_failed"
            if not ensemble_row["error_message"]:
                ensemble_row["error_message"] = "; ".join(
                    r["error_message"]
                    for r in file_rows
                    if r["error_message"]
                )
            ensemble_rows.append(ensemble_row)
    return ensemble_rows


def write_report(
    rows: list[dict[str, Any]],
    output_path: Path,
    fieldnames: list[str],
) -> None:
    """Write collected rows to a CSV report.

    Args:
        rows: List of row dicts, each containing every column in ``fieldnames``.
        output_path: Destination path for the CSV file.
        fieldnames: Column order to write.
    """
    if not rows:
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
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
            "directory and write two CSV reports (per-strategy and synthetic "
            "ParallelEnsemble) comparing all registered optimization strategies."
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

    all_rows: list[dict[str, Any]] = []
    success_count = 0
    failure_count = 0

    for index, plt_file in enumerate(plt_files, start=1):
        print(f"[{index}/{len(plt_files)}] {plt_file.name} ...", end=" ", flush=True)
        text_logger.info(f"Processing {plt_file}")
        rows = process_file(
            plt_file,
            output_dir,
            args.same_row_preference,
            metrics_logger,
            text_logger,
        )
        all_rows.extend(rows)

        # Determine file-level status from the rows we produced.
        file_statuses = {row["status"] for row in rows}
        if "success" in file_statuses:
            success_count += 1
            print("OK")
        else:
            failure_count += 1
            # Pick the first error we can find to display.
            err_message = next(
                (row["error_message"] for row in rows if row["error_message"]),
                "(no error message recorded)",
            )
            err_summary = err_message if len(err_message) <= 80 else f"{err_message[:77]}..."
            print(f"FAILED: {err_summary}")

    report_path = output_dir / "report.csv"
    write_report(all_rows, report_path, CSV_COLUMNS)

    ensemble_rows = build_ensemble_rows(all_rows)
    ensemble_report_path = output_dir / "ensemble_report.csv"
    write_report(ensemble_rows, ensemble_report_path, CSV_COLUMNS)

    print()
    print("=" * 60)
    print("BENCHMARK COMPLETE")
    print(f"  Total files:        {len(plt_files)}")
    print(f"  Successful files:   {success_count}")
    print(f"  Failed files:       {failure_count}")
    print(f"  Total CSV rows:     {len(all_rows)}")
    print(f"  Per-strategy CSV:   {report_path}")
    print(f"  Ensemble CSV:       {ensemble_report_path}")
    print(f"  Optimized:          {output_dir / 'optimized'}")
    print(f"  Plots:              {output_dir / 'plots'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())