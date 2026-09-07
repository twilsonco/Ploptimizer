"""Batch benchmark tool for PLT-Optimizer.

This script processes every ``.plt`` file in a user-specified directory, runs
each registered optimization strategy against every file, and writes two CSV
reports summarizing the results. Optimized PLT files and diagnostic plots are
written to a directory *adjacent* to the input directory.

Each file is processed in a separate worker process via
:class:`concurrent.futures.ProcessPoolExecutor`, and per-(file, strategy)
rows are streamed into ``report.csv`` as soon as a file finishes — so a
crash, ``Ctrl-C`` or timeout never loses results that were already
computed. Per-file timings are logged to ``logs/optimizer.log`` and
printed to stdout as work completes, including a rolling average and an
ETA based on the last ten files.

Typical use case: when a file in production fails to process, this tool can
be pointed at a batch of real-world files to quickly identify which ones
break the pipeline and compare the effectiveness of each strategy across the
remaining ones.

Usage:
    python examples/benchmark.py /path/to/cad_files/
    python examples/benchmark.py /path/to/cad_files/ --same-row-preference 1.5
    python examples/benchmark.py /path/to/cad_files/ --workers 8
    python examples/benchmark.py /path/to/cad_files/ --ensemble-timeout 30

The winners post-processing can also be re-run standalone against an
existing report (no PLT processing):
    python plt_optimizer/cli/benchmark.py --analyze-only <dir>/report.csv

Output structure:
    <input_dir_name>_benchmark/
        report.csv                   # Per-(file, strategy) summary, streamed
        ensemble_report.csv          # Synthetic ParallelEnsemble rows
        report_rapid_improvement_winners.csv  # Per-file best rapid improvement
        report_time_winners.csv      # Per-file fastest strategy
        report_combined_winners.csv  # Per-file best quality/speed balance
        report_winner_summary.csv    # Per-strategy win counts + timing stats
        optimized/<strategy>/        # Optimized PLT files, one folder per strategy
        plots/                       # Before + after plots per file/strategy

The first CSV (``report.csv``) contains one row per registered strategy for
every input file, allowing per-strategy error reporting and per-strategy
distance-saved metrics, followed by one ``ensemble`` row per file produced by
running the real ``ParallelEnsembleStrategy`` (the ensemble row is excluded
from the winners reports since it aggregates the other strategies). Every
optimization job is bounded by the per-job timeout configured via
``--ensemble-timeout``: each individual strategy runs in its own killable
subprocess and is aborted (marked ``failed``) when it exceeds the budget, and
the same budget applies to each member job inside the ensemble. The second CSV
(``ensemble_report.csv``) contains a single row per file, simulating what the
ParallelEnsemble strategy would have produced: the ``strategy_name`` column
holds the winning strategy's name (selected by greatest total improvement %,
ties broken by shortest total distance then fastest runtime). Both CSVs share
the same schema defined in :data:`CSV_COLUMNS`.

After both reports are written, :func:`analyze_report_winners` re-reads
``report.csv`` and produces the three winners CSVs plus a stdout summary
table (mirrored to ``<stem>_winner_summary.csv``) counting per-strategy
wins across the batch. Winners are selected per file among successful
strategies excluding the ``no-opt`` baseline, under three criteria: best
``rapid_improvement_pct``, lowest ``time_ms``, and a combined
quality/speed score (per-file min-max normalisation of both criteria,
averaged with equal weight). Files with no successful non-baseline
strategy are omitted from the winners reports.

The summary table additionally carries per-strategy runtime statistics
aggregated across the whole batch: the slowest single-file runtime
(``max_time_ms``) plus min/max/average runtime per path and per segment.
The report rows themselves (and therefore the winners CSVs) carry each
job's own runtime per path and per segment (``ms_per_path`` /
``ms_per_segment``: the row's ``time_ms`` divided by its ``before_paths`` /
``before_segments``).
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
import threading
import time
import traceback
from concurrent.futures import (
    Future,
    ProcessPoolExecutor,
    as_completed,
)
from concurrent.futures import (
    TimeoutError as FuturesTimeoutError,
)
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Set, Tuple

# Add project root to path for imports when running as script
sys.path.insert(0, str(Path(__file__).parent.parent))

from plt_optimizer.core.chunker import Chunker, ChunkerConfig
from plt_optimizer.core.optimizer import (
    ChristofidesStrategy,
    GeneticAlgorithmStrategy,
    InsertionHeuristicStrategy,
    NearestNeighbor2OptStrategy,
    OptimizerEngine,
    ParallelEnsembleStrategy,
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
STRATEGY_REGISTRY: Dict[str, type] = {
    "no-opt": None,  # type: ignore  # Baseline (no optimization)
    "nn2opt": NearestNeighbor2OptStrategy,
    "insertion": InsertionHeuristicStrategy,
    "christofides": ChristofidesStrategy,
    "sa": SimulatedAnnealingStrategy,
    "genetic": GeneticAlgorithmStrategy,
}

# Strategies that accept a same_row_preference parameter.
_STRATEGIES_WITH_SAME_ROW_PREFERENCE = {"nn2opt"}

# Strategy name for the real ParallelEnsemble run appended to every file's
# rows. It is excluded from the winners reports (it would compete against
# the very strategies it aggregates).
_ENSEMBLE_STRATEGY_NAME: str = "ensemble"

# Sentinel strategy name used in the per-strategy CSV when a file fails
# before any strategy is actually run.
_FILE_LEVEL_SENTINEL: str = "(file)"

# Sentinel used in the ensemble CSV when no strategy succeeded for a file.
_NO_WINNER_SENTINEL: str = "(none)"


def _build_csv_columns() -> List[str]:
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
        "ms_per_path",
        "ms_per_segment",
        "error_message",
    ]


CSV_COLUMNS: List[str] = _build_csv_columns()


def _empty_row(file_name: str) -> Dict[str, Any]:
    """Return a fresh dict with every CSV column initialized to ``""``.

    Args:
        file_name: Value for the ``file_name`` column.

    Returns:
        New dict ready to be filled in by a strategy or sentinel handler.
    """
    row: Dict[str, Any] = dict.fromkeys(CSV_COLUMNS, "")
    row["file_name"] = file_name
    return row


def _strip_private_keys(row: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``row`` with private (``_``-prefixed) keys removed.

    Workers attach bookkeeping fields like ``_metrics_event`` and
    ``_optimized_plt_path`` to row dicts for the main process. Those keys
    must never leak into the on-disk CSV, so this helper produces a clean
    copy whenever a row is handed to :class:`csv.DictWriter`.

    Args:
        row: Row dict possibly containing private keys.

    Returns:
        New dict containing only the public CSV columns.
    """
    return {k: v for k, v in row.items() if not k.startswith("_")}


def find_plt_files(input_dir: Path) -> List[Path]:
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
    text_logger: Optional[Any],
) -> None:
    """Generate and save a diagnostic plot, swallowing plot failures.

    Args:
        doc: PLTDocument to render.
        plot_path: Destination path for the PNG.
        title: Plot title.
        rapid_travel_inches: Rapid travel in inches for the legend.
        text_logger: Text logger for non-fatal plot errors. ``None`` silences
            non-fatal plot failures (useful for subprocess workers that have
            no logger handle of their own).
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
        if text_logger is not None:
            text_logger.warning(f"Failed to generate plot {plot_path.name}: {plot_err}")


def _populate_metrics(
    row: Dict[str, Any],
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
    cutting_pct = (cutting_saved / before_cutting * 100) if before_cutting > 0 else 0.0
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


class _StrategyOutcome(NamedTuple):
    """Result of running one strategy under a subprocess timeout.

    Exactly one of the fields is meaningful per run: on success ``result``
    and ``elapsed_ms`` are set and ``error`` is ``None``; on failure/timeout
    ``error`` carries a human-readable message and the others are ``None``.
    """

    result: Optional[Any]
    elapsed_ms: Optional[float]
    error: Optional[str]


def _optimize_strategy_worker(
    strategy: Any,
    blocks: Any,
) -> Tuple[Any, float]:
    """Run one strategy through the engine and return its result plus elapsed time.

    Module-level so :class:`concurrent.futures.ProcessPoolExecutor` can pickle
    it into the child process (``spawn`` re-imports this module and cannot use
    closures). The strategy instance itself is pickled by the caller, which
    keeps construction (and therefore ``same_row_preference`` wiring) in the
    parent process.

    Going through :class:`OptimizerEngine` keeps the child's behaviour identical
    to the previous in-process runs: the engine emits the established
    "Starting optimization" / "Optimization complete" INFO lines and wraps
    strategy failures in :class:`OptimizationError`.

    Args:
        strategy: A constructed ``OptimizationStrategy`` instance.
        blocks: MacroBlocks to optimize.

    Returns:
        Tuple ``(OptimizationResult, elapsed_seconds)`` measured inside the
        child so the reported ``time_ms`` reflects pure optimization work.
    """
    optimizer = OptimizerEngine(strategy=strategy)
    start = time.perf_counter()
    result = optimizer.optimize(blocks)
    return result, time.perf_counter() - start


def _terminate_pool_workers(executor: ProcessPoolExecutor) -> None:
    """Terminate the worker processes still owned by ``executor``.

    Uses the ``_processes`` private attribute (a ``pid -> _ProcessImage``
    mapping, stable through Python 3.13) because ``shutdown(wait=False)``
    alone does not interrupt running work, and ``cancel_futures=`` requires
    Python 3.9+ (this codebase targets 3.8). Guarded so exotic executor
    implementations degrade to a no-op.

    Args:
        executor: The executor whose workers should be killed.
    """
    processes = getattr(executor, "_processes", None)
    if not isinstance(processes, dict):
        return
    for process in list(processes.values()):
        try:
            process.terminate()
        except Exception:  # noqa: BLE001 - best-effort kill
            pass


def _run_one_strategy_with_timeout(
    strategy: Any,
    blocks: Any,
    strategy_timeout: float,
    text_logger: Optional[Any],
) -> _StrategyOutcome:
    """Run a single strategy in a killable subprocess bounded by a timeout.

    A dedicated one-worker :class:`ProcessPoolExecutor` runs
    :func:`_optimize_strategy_worker`; if it does not finish within
    ``strategy_timeout`` seconds the worker is terminated and a timeout outcome
    is returned. Running each strategy in its own process is what makes the
    bound enforceable — a runaway in-process optimization cannot be interrupted
    otherwise. If the pool cannot be created (restricted environments), the run
    degrades to an in-process call so the benchmark still produces a result.

    Args:
        strategy: A constructed ``OptimizationStrategy`` instance.
        blocks: MacroBlocks to optimize.
        strategy_timeout: Seconds the job may take before being aborted.
        text_logger: Text logger for abort diagnostics, or ``None``.

    Returns:
        A :class:`_StrategyOutcome` describing success, failure, or timeout.
    """
    try:
        executor = ProcessPoolExecutor(max_workers=1)
    except Exception as pool_err:  # noqa: BLE001 - degrade to in-process, never fail the row
        if text_logger is not None:
            text_logger.warning(
                f"Could not create strategy worker pool ({pool_err}); "
                "running strategy in-process without a timeout"
            )
        try:
            result, elapsed_s = _optimize_strategy_worker(strategy, blocks)
            return _StrategyOutcome(result, elapsed_s * 1000, None)
        except Exception as strat_err:  # noqa: BLE001 - surfaced as a failed outcome
            return _StrategyOutcome(None, None, f"{type(strat_err).__name__}: {strat_err}")

    with executor:
        future = executor.submit(_optimize_strategy_worker, strategy, blocks)
        try:
            result, elapsed_s = future.result(timeout=strategy_timeout)
            return _StrategyOutcome(result, elapsed_s * 1000, None)
        except FuturesTimeoutError:
            future.cancel()
            _terminate_pool_workers(executor)
            if text_logger is not None:
                text_logger.warning(f"Strategy timed out after {strategy_timeout}s and was aborted")
            return _StrategyOutcome(None, None, f"timed out after {strategy_timeout}s")
        except Exception as strat_err:  # noqa: BLE001 - surfaced as a failed outcome
            return _StrategyOutcome(None, None, f"{type(strat_err).__name__}: {strat_err}")


def _run_strategies_with_timeout(
    strategies: Dict[str, Any],
    blocks: Any,
    strategy_timeout: float,
    text_logger: Optional[Any],
) -> Dict[str, _StrategyOutcome]:
    """Run each strategy in a killable subprocess, one at a time, with a timeout.

    Strategies execute sequentially (each with the CPU to itself) so their
    measured ``time_ms`` stays comparable across strategies — the property the
    winners analysis relies on — while still being bounded by
    ``strategy_timeout`` via :func:`_run_one_strategy_with_timeout`.

    Args:
        strategies: Mapping of ``strategy_name -> constructed strategy`` to run,
            in execution order.
        blocks: MacroBlocks shared by every strategy.
        strategy_timeout: Seconds each individual job may take before abort.
        text_logger: Text logger for abort diagnostics, or ``None``.

    Returns:
        Mapping of ``strategy_name -> _StrategyOutcome`` for every input
        strategy, in the same order.
    """
    return {
        name: _run_one_strategy_with_timeout(strategy, blocks, strategy_timeout, text_logger)
        for name, strategy in strategies.items()
    }


def _run_strategy(
    strategy_name: str,
    outcome: _StrategyOutcome,
    doc: Any,
    blocks: Any,
    before_rapid: float,
    before_cutting: float,
    input_path: Path,
    output_dir: Path,
    text_logger: Optional[Any],
) -> Dict[str, Any]:
    """Build a CSV row from one strategy's pre-computed optimization outcome.

    The strategy itself has already been executed (in a killable subprocess by
    :func:`_run_strategies_with_timeout`); this function only performs the
    post-processing that must stay in the parent process: reassembly, metrics,
    PLT writing and plotting.

    On success, the returned dict has ``status == "success"`` and all metric
    columns filled. When ``outcome.error`` is set (strategy raised, or was
    aborted by the timeout), the dict has ``status == "failed"`` with the
    error captured in ``error_message`` and all other columns left empty.

    When ``text_logger`` is ``None``, the function still completes its work
    but skips logger side-effects. This lets :func:`process_file` be called
    from a subprocess worker that has no shared logger handle; the main
    process can re-emit metrics events from the returned row using
    :func:`_log_metrics_from_row`.

    Args:
        strategy_name: Strategy key from ``STRATEGY_REGISTRY`` (or the
            no-opt baseline pseudo-name, whose outcome carries the input
            document unchanged).
        outcome: The :class:`_StrategyOutcome` from the strategy's run.
        doc: Simplified PLTDocument used for reassembly.
        blocks: MacroBlocks to optimize (used for reassembly on success).
        before_rapid: Rapid travel distance before optimization (internal units).
        before_cutting: Cutting distance before optimization (internal units).
        input_path: Source PLT path (for naming outputs).
        output_dir: Destination directory for outputs.
        text_logger: Text logger, or ``None`` to suppress log output.

    Returns:
        Row dict containing every column declared in :data:`CSV_COLUMNS`
        and a private ``_optimized_plt_path`` key carrying the absolute
        path of the optimized file (or ``None`` on failure) so the main
        process can re-emit metrics events with the correct file handle.
    """
    row = _empty_row(input_path.name)
    row["strategy_name"] = strategy_name
    optimized_plt_path: Optional[Path] = None
    metrics_event: Dict[str, Any] = {
        "kind": "strategy",
        "strategy_name": strategy_name,
        "status": "failed",
        "job_id": (f"{input_path.stem}_{strategy_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"),
        "original_file": input_path,
        "optimized_file": None,
        "original_distance": before_rapid,
        "optimized_distance": before_rapid,
        "notes": "",
    }

    if outcome.error is not None:
        err_msg = outcome.error
        row["status"] = "failed"
        row["error_message"] = f"[{strategy_name}] {err_msg}"
        metrics_event["notes"] = err_msg[:200]
        if text_logger is not None:
            text_logger.error(f"Strategy {strategy_name} failed on {input_path.name}: {err_msg}")
        row["_metrics_event"] = metrics_event
        row["_optimized_plt_path"] = None
        return row

    try:
        opt_elapsed_ms = outcome.elapsed_ms if outcome.elapsed_ms is not None else 0.0
        if strategy_name == "no-opt":
            # Baseline: no reassembly, metrics are the input document's own.
            optimized_doc = doc
            optimized_rapid = before_rapid
            optimized_cutting = before_cutting
        else:
            if outcome.result is None:
                raise ValueError("strategy reported success without producing a result")
            reassembler = Reassembler()
            optimized_doc = reassembler.reassemble(doc, blocks, outcome.result)

            optimized_rapid = optimized_doc.rapid_distance()
            optimized_cutting = optimized_doc.cutting_distance()

        total_before = before_rapid + before_cutting
        total_after = optimized_rapid + optimized_cutting
        total_pct = ((total_after - total_before) / total_before) * 100 if total_before > 0 else 0.0

        strategy_output_dir = output_dir / "optimized" / strategy_name
        strategy_output_dir.mkdir(parents=True, exist_ok=True)
        optimized_plt_path = strategy_output_dir / f"{input_path.stem}_optimized.plt"

        writer = PLTWriter()
        writer.write_file(optimized_doc, optimized_plt_path)

        after_plot_path = output_dir / "plots" / f"{input_path.stem}_after_{strategy_name}.png"
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

        metrics_event["status"] = "success"
        metrics_event["optimized_file"] = optimized_plt_path
        metrics_event["optimized_distance"] = optimized_rapid

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
        metrics_event["notes"] = err_msg[:200]
        if text_logger is not None:
            text_logger.error(f"Strategy {strategy_name} failed on {input_path.name}: {strat_err}")

    # Stash event payload + optimized path on the row for the main process.
    row["_metrics_event"] = metrics_event
    row["_optimized_plt_path"] = str(optimized_plt_path) if optimized_plt_path else None
    return row


def _run_ensemble_row(
    blocks: Any,
    doc: Any,
    before_rapid: float,
    before_cutting: float,
    input_path: Path,
    output_dir: Path,
    ensemble_timeout: float,
    same_row_preference: float,
    text_logger: Optional[Any],
) -> Dict[str, Any]:
    """Run the real ParallelEnsemble strategy and return a populated CSV row.

    Mirrors :func:`_run_strategy` but drives
    :class:`~plt_optimizer.core.optimizer.ParallelEnsembleStrategy` with the
    configured per-job ``ensemble_timeout`` (seconds each member strategy job
    may take before it is aborted). The ensemble result is unwrapped before
    reassembly, exactly like the watch pipeline does.

    Args:
        blocks: MacroBlocks to optimize.
        doc: Simplified PLTDocument used for reassembly.
        before_rapid: Rapid travel distance before optimization (internal units).
        before_cutting: Cutting distance before optimization (internal units).
        input_path: Source PLT path (for naming outputs).
        output_dir: Destination directory for outputs.
        ensemble_timeout: Per-job timeout in seconds for the ensemble.
        same_row_preference: Penalty multiplier for y-differences.
        text_logger: Text logger, or ``None`` to suppress log output.

    Returns:
        Row dict with ``strategy_name == "ensemble"`` containing every column
        declared in :data:`CSV_COLUMNS` plus the private ``_metrics_event`` /
        ``_optimized_plt_path`` bookkeeping keys.
    """
    row = _empty_row(input_path.name)
    row["strategy_name"] = _ENSEMBLE_STRATEGY_NAME
    optimized_plt_path: Optional[Path] = None
    metrics_event: Dict[str, Any] = {
        "kind": "strategy",
        "strategy_name": _ENSEMBLE_STRATEGY_NAME,
        "status": "failed",
        "job_id": (
            f"{input_path.stem}_{_ENSEMBLE_STRATEGY_NAME}_"
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        ),
        "original_file": input_path,
        "optimized_file": None,
        "original_distance": before_rapid,
        "optimized_distance": before_rapid,
        "notes": "",
    }

    try:
        strategy = ParallelEnsembleStrategy(
            baseline_distance=before_rapid,
            job_timeout=ensemble_timeout,
            same_row_preference=same_row_preference,
        )
        optimizer = OptimizerEngine(strategy=strategy)

        opt_start = time.perf_counter()
        optimization_result = optimizer.optimize(blocks)
        opt_elapsed_ms = (time.perf_counter() - opt_start) * 1000

        # The ensemble returns a wrapper carrying the winning result; unwrap
        # it before reassembly (same contract as the watch pipeline).
        result_for_reassembly = getattr(optimization_result, "result", optimization_result)

        reassembler = Reassembler()
        optimized_doc = reassembler.reassemble(doc, blocks, result_for_reassembly)

        optimized_rapid = optimized_doc.rapid_distance()
        optimized_cutting = optimized_doc.cutting_distance()

        total_before = before_rapid + before_cutting
        total_after = optimized_rapid + optimized_cutting
        total_pct = ((total_after - total_before) / total_before) * 100 if total_before > 0 else 0.0

        strategy_output_dir = output_dir / "optimized" / _ENSEMBLE_STRATEGY_NAME
        strategy_output_dir.mkdir(parents=True, exist_ok=True)
        optimized_plt_path = strategy_output_dir / f"{input_path.stem}_optimized.plt"

        writer = PLTWriter()
        writer.write_file(optimized_doc, optimized_plt_path)

        after_plot_path = (
            output_dir / "plots" / f"{input_path.stem}_after_{_ENSEMBLE_STRATEGY_NAME}.png"
        )
        _save_plot(
            optimized_doc,
            after_plot_path,
            title=(
                f"{input_path.name} [{_ENSEMBLE_STRATEGY_NAME}]: "
                f"Total {total_pct:+.1f}% ({opt_elapsed_ms:.0f} ms)"
            ),
            rapid_travel_inches=optimized_rapid / 1000,
            text_logger=text_logger,
        )

        metrics_event["status"] = "success"
        metrics_event["optimized_file"] = optimized_plt_path
        metrics_event["optimized_distance"] = optimized_rapid

        row["status"] = "success"
        _populate_metrics(
            row,
            before_rapid=before_rapid,
            before_cutting=before_cutting,
            optimized_rapid=optimized_rapid,
            optimized_cutting=optimized_cutting,
            time_ms=opt_elapsed_ms,
        )
    except Exception as ens_err:  # noqa: BLE001 - ensemble failure must not kill the file
        err_msg = f"{type(ens_err).__name__}: {ens_err}"
        row["status"] = "failed"
        row["error_message"] = f"[{_ENSEMBLE_STRATEGY_NAME}] {err_msg}"
        metrics_event["notes"] = err_msg[:200]
        if text_logger is not None:
            text_logger.error(f"Ensemble strategy failed on {input_path.name}: {ens_err}")

    row["_metrics_event"] = metrics_event
    row["_optimized_plt_path"] = str(optimized_plt_path) if optimized_plt_path else None
    return row


def process_file(
    input_path: Path,
    output_dir: Path,
    same_row_preference: float,
    ensemble_timeout: float = 10.0,
    metrics_logger: Optional[Any] = None,
    text_logger: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Process a single PLT file and return one CSV row per strategy.

    On file-level parse or setup failure, a single sentinel row is returned
    (with ``strategy_name == "(file)"``) carrying the failure in
    ``error_message`` so consumers always see a consistent schema.

    When ``metrics_logger`` and/or ``text_logger`` are ``None``, the function
    still completes its work but skips logger side-effects. The file-level
    metrics event is captured on the returned row under the
    ``_metrics_event`` private key so the main process can re-emit it via
    :func:`_log_metrics_from_row`.

    Args:
        input_path: Path to the input PLT file.
        output_dir: Destination directory for optimized files and plots.
        same_row_preference: Penalty multiplier for y-differences.
        ensemble_timeout: Per-job timeout (seconds) applied to every strategy
            run for this file: each individual strategy executes in a killable
            subprocess bounded by this budget, as does each member job of the
            real ParallelEnsemble run appended after the per-strategy rows.
        metrics_logger: CSV metrics logger, or ``None`` to skip metrics.
        text_logger: Text logger, or ``None`` to suppress log output.

    Returns:
        List of row dicts. On file-level failure: one sentinel row. On
        success: one row per strategy in :data:`STRATEGY_REGISTRY` order,
        followed by the real ensemble row. Every dict contains every column
        in :data:`CSV_COLUMNS`.
    """
    parser = PLTParser()
    try:
        original_doc = parser.parse_file(input_path)
    except (ParseError, OSError, ValueError) as parse_err:
        row = _empty_row(input_path.name)
        row["strategy_name"] = _FILE_LEVEL_SENTINEL
        row["status"] = "parse_failed"
        row["error_message"] = f"[parse] {type(parse_err).__name__}: {parse_err}"
        row["_metrics_event"] = {
            "kind": "file",
            "strategy_name": _FILE_LEVEL_SENTINEL,
            "status": "failed",
            "job_id": f"{input_path.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            "original_file": input_path,
            "optimized_file": None,
            "original_distance": 0.0,
            "optimized_distance": 0.0,
            "notes": str(parse_err)[:200],
        }
        if text_logger is not None:
            text_logger.error(f"Failed to parse {input_path.name}: {parse_err}")
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
        row["_metrics_event"] = {
            "kind": "file",
            "strategy_name": _FILE_LEVEL_SENTINEL,
            "status": "failed",
            "job_id": f"{input_path.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            "original_file": input_path,
            "optimized_file": None,
            "original_distance": 0.0,
            "optimized_distance": 0.0,
            "notes": f"setup: {setup_err}"[:200],
        }
        if text_logger is not None:
            text_logger.error(f"Setup failed for {input_path.name}: {setup_err}")
            text_logger.error(traceback.format_exc())
        return [row]

    # Build the strategy instances to run. The no-opt baseline is a pseudo
    # strategy (no optimization), so it is given a synthetic zero-cost outcome
    # instead of a subprocess run. Every real strategy runs in its own killable
    # subprocess bounded by ``ensemble_timeout`` so a runaway optimization
    # cannot exceed the configured budget.
    strategies: Dict[str, Any] = {}
    construction_errors: Dict[str, str] = {}
    for strategy_name, strategy_class in STRATEGY_REGISTRY.items():
        if strategy_name == "no-opt" or strategy_class is None:
            continue
        try:
            if strategy_name in _STRATEGIES_WITH_SAME_ROW_PREFERENCE:
                strategies[strategy_name] = strategy_class(same_row_preference=same_row_preference)
            else:
                strategies[strategy_name] = strategy_class()
        except Exception as construct_err:  # noqa: BLE001 - one bad strategy is one failed row
            construction_errors[strategy_name] = f"{type(construct_err).__name__}: {construct_err}"

    outcomes = _run_strategies_with_timeout(
        strategies=strategies,
        blocks=blocks,
        strategy_timeout=ensemble_timeout,
        text_logger=text_logger,
    )
    # Fold construction failures in as failed outcomes.
    for name, err in construction_errors.items():
        outcomes[name] = _StrategyOutcome(None, None, err)
    # The no-opt baseline never runs: synthesize a zero-cost success outcome.
    outcomes["no-opt"] = _StrategyOutcome(result=None, elapsed_ms=0.0, error=None)

    rows: List[Dict[str, Any]] = []
    for strategy_name in STRATEGY_REGISTRY:
        outcome = outcomes[strategy_name]
        row = _run_strategy(
            strategy_name=strategy_name,
            outcome=outcome,
            doc=simplified_doc,
            blocks=blocks,
            before_rapid=before_rapid,
            before_cutting=before_cutting,
            input_path=input_path,
            output_dir=output_dir,
            text_logger=text_logger,
        )
        # Tag baseline file metrics on every per-strategy row.
        row["before_rapid_in"] = round(before_rapid / 1000, 3)
        row["before_cutting_in"] = round(before_cutting / 1000, 3)
        row["before_paths"] = before_paths
        row["before_segments"] = before_segments
        row["blocks_created"] = blocks_created
        # Per-job runtime ratios (blank for failed rows without time_ms).
        row.update(_job_timing_fields(row))
        rows.append(row)

    # Real ParallelEnsemble run (with the configured per-job timeout),
    # appended after the individual strategies so its row reflects the same
    # blocks/document the simulation in ensemble_report.csv is built from.
    ensemble_row = _run_ensemble_row(
        blocks=blocks,
        doc=simplified_doc,
        before_rapid=before_rapid,
        before_cutting=before_cutting,
        input_path=input_path,
        output_dir=output_dir,
        ensemble_timeout=ensemble_timeout,
        same_row_preference=same_row_preference,
        text_logger=text_logger,
    )
    ensemble_row["before_rapid_in"] = round(before_rapid / 1000, 3)
    ensemble_row["before_cutting_in"] = round(before_cutting / 1000, 3)
    ensemble_row["before_paths"] = before_paths
    ensemble_row["before_segments"] = before_segments
    ensemble_row["blocks_created"] = blocks_created
    ensemble_row.update(_job_timing_fields(ensemble_row))
    rows.append(ensemble_row)
    return rows


class FileResult(NamedTuple):
    """Picklable per-file bundle returned by the parallel worker.

    Wrapping the result of :func:`process_file` in a NamedTuple keeps the
    boundary between the main process and the executor worker minimal and
    trivially serializable. ``rows`` is the list of CSV row dicts; the main
    process uses it to build both the per-strategy and ensemble reports.
    """

    input_path: str
    elapsed_s: float
    rows: List[Dict[str, Any]]


def _process_file_worker(
    input_path_str: str,
    output_dir_str: str,
    same_row_preference: float,
    ensemble_timeout: float = 10.0,
) -> FileResult:
    """Top-level worker invoked by the process pool.

    Lives at module scope (rather than being nested in ``main``) because
    ``ProcessPoolExecutor`` requires its callables to be importable by the
    child processes — on Windows the ``spawn`` start method re-imports the
    worker module from scratch and cannot pickle closures.

    The worker suppresses logger side-effects inside the child process and
    returns all CSV row dicts plus the wall-clock elapsed time to the main
    process, which owns the text/CSV loggers and writes to the report files
    incrementally as futures complete.

    Args:
        input_path_str: Absolute path to the PLT file.
        output_dir_str: Absolute path to the output directory.
        same_row_preference: Penalty multiplier for y-differences.
        ensemble_timeout: Per-job timeout (seconds) applied to every strategy
            run for this file (individual strategies and ensemble member jobs
            alike); see :func:`process_file`.

    Returns:
        :class:`FileResult` bundling the source path, elapsed seconds, and
        the list of CSV row dicts produced by :func:`process_file`.
    """
    input_path = Path(input_path_str)
    output_dir = Path(output_dir_str)
    start = time.perf_counter()
    rows = process_file(
        input_path=input_path,
        output_dir=output_dir,
        same_row_preference=same_row_preference,
        ensemble_timeout=ensemble_timeout,
        metrics_logger=None,
        text_logger=None,
    )
    elapsed_s = time.perf_counter() - start
    return FileResult(input_path=input_path_str, elapsed_s=elapsed_s, rows=rows)


class ReportWriter:
    """Thread-safe CSV writer that streams rows incrementally to disk.

    Opens the destination file once for the lifetime of the benchmark and
    flushes after every row so that a crash, ``Ctrl-C`` or timeout never
    loses results that were already computed. A lock guards ``write_row``
    so multiple threads (e.g. completion callbacks on the main loop) can
    share a single writer without interleaving output.

    Attributes:
        output_path: Destination CSV path.
    """

    def __init__(self, output_path: Path, fieldnames: List[str]) -> None:
        """Open the CSV file with headers ready for streaming writes.

        Args:
            output_path: Destination CSV path.
            fieldnames: Column order to write.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path = output_path
        self.fieldnames = fieldnames
        self._lock = threading.Lock()
        self._file = open(output_path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=fieldnames)
        self._writer.writeheader()
        self._file.flush()

    def write_row(self, row: Dict[str, Any]) -> None:
        """Append a single row to the CSV, flushing immediately.

        Args:
            row: Dict containing every column in ``fieldnames``. Private
                keys prefixed with ``_`` are stripped before writing.
        """
        clean_row = {k: row.get(k, "") for k in self.fieldnames}
        with self._lock:
            self._writer.writerow(clean_row)
            self._file.flush()

    def close(self) -> None:
        """Flush and close the underlying file handle."""
        with self._lock:
            self._file.flush()
            self._file.close()

    def __enter__(self) -> ReportWriter:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def _log_metrics_from_row(
    row: dict[str, Any],
    metrics_logger: Any,
) -> None:
    """Re-emit a single CSV metrics event from a row's stashed payload.

    Workers built with ``metrics_logger=None`` embed their metric event
    on the row under the ``_metrics_event`` key. This helper unpacks that
    payload and calls :func:`CSVMetricsLogger.log_job` in the main process
    so the global ``logs/job_metrics.csv`` file still records every job.

    Args:
        row: Row dict returned by :func:`process_file` or :func:`_run_strategy`.
        metrics_logger: CSV metrics logger owned by the main process.
    """
    event = row.get("_metrics_event")
    if event is None:
        return
    metrics_logger.log_job(
        job_id=event["job_id"],
        original_file=event["original_file"],
        optimized_file=event["optimized_file"],
        original_distance=event["original_distance"],
        optimized_distance=event["optimized_distance"],
        status=event["status"],
        method=event["strategy_name"],
        notes=event.get("notes", ""),
    )


def _summarize_file_result(
    rows: list[dict[str, Any]],
) -> tuple[bool, str]:
    """Reduce a file's per-strategy rows to a single status + summary string.

    Args:
        rows: Per-strategy rows for one file.

    Returns:
        Tuple ``(ok, summary)``. ``ok`` is ``True`` when at least one
        strategy succeeded. ``summary`` is a short status string suitable
        for stdout (``"OK"``, ``"FAILED: <error>"``, or
        ``"FAILED (no strategies)"``).
    """
    statuses = {row["status"] for row in rows}
    if "success" in statuses:
        return True, "OK"
    err_message = next(
        (row["error_message"] for row in rows if row["error_message"]),
        "(no error message recorded)",
    )
    if len(err_message) > 80:
        err_message = f"{err_message[:77]}..."
    return False, f"FAILED: {err_message}"


def _select_ensemble_winner(successful_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
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

    def _sort_key(r: Dict[str, Any]) -> Tuple[float, float, float]:
        time_value = float(r["time_ms"]) if r["time_ms"] != "" else 0.0
        return (
            -float(r["total_improvement_pct"]),
            float(r["total_after_in"]),
            time_value,
        )

    return min(successful_rows, key=_sort_key)


def build_ensemble_rows(per_strategy_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
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
    files_in_order: List[str] = []
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in per_strategy_rows:
        file_name = row["file_name"]
        if file_name not in grouped:
            grouped[file_name] = []
            files_in_order.append(file_name)
        grouped[file_name].append(row)

    ensemble_rows: List[Dict[str, Any]] = []
    for file_name in files_in_order:
        file_rows = grouped[file_name]
        # The synthetic ensemble simulates picking a single member strategy,
        # so the real ensemble row (when present) is not a candidate.
        successful = [
            r
            for r in file_rows
            if r["status"] == "success" and r["strategy_name"] != _ENSEMBLE_STRATEGY_NAME
        ]

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
                    r["error_message"] for r in file_rows if r["error_message"]
                )
            ensemble_rows.append(ensemble_row)
    return [_strip_private_keys(row) for row in ensemble_rows]


def write_report(
    rows: List[Dict[str, Any]],
    output_path: Path,
    fieldnames: List[str],
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
            writer.writerow(_strip_private_keys(row))


# ---------------------------------------------------------------------------
# Post-run winner analysis (comparative post-processing of report.csv)
# ---------------------------------------------------------------------------

# Strategy names that are never eligible to win a winners report: the
# no-optimization baseline, the real ensemble run (which aggregates the very
# strategies it would compete against), plus the file-level / no-winner
# sentinels.
_INELIGIBLE_WINNER_STRATEGIES: Set[str] = {
    "no-opt",
    _ENSEMBLE_STRATEGY_NAME,
    _FILE_LEVEL_SENTINEL,
    _NO_WINNER_SENTINEL,
}

# Weight applied to the normalised rapid improvement in the combined score.
# The remaining weight (1 - this value) goes to normalised speed.
_COMBINED_IMPROVEMENT_WEIGHT: float = 0.5

# Canonical criterion keys used in the returned win-count mapping and as the
# suffixes of the generated winners CSVs.
_CRITERION_RAPID: str = "rapid_improvement"
_CRITERION_TIME: str = "time"
_CRITERION_COMBINED: str = "combined"

# Per-strategy runtime statistics shown in the stdout summary table and its
# winner-summary CSV mirror. Values are aggregated over all eligible
# (successful, non-baseline) rows for the strategy across the whole batch.
_TIMING_STAT_COLUMNS: List[str] = [
    "max_time_ms",
    "min_ms_per_path",
    "max_ms_per_path",
    "avg_ms_per_path",
    "min_ms_per_segment",
    "max_ms_per_segment",
    "avg_ms_per_segment",
]

# Per-job runtime ratio columns (each row's ``time_ms`` divided by its
# ``before_paths`` / ``before_segments``). They are part of
# :data:`CSV_COLUMNS`; this constant keeps them addressable as a pair for
# computation and backfilling.
_WINNER_JOB_TIMING_COLUMNS: List[str] = ["ms_per_path", "ms_per_segment"]

# Column order of the stdout win-count table, also written verbatim to a
# summary CSV so the printed table can be loaded/spreadsheeted later.
_WINNER_SUMMARY_COLUMNS: List[str] = [
    "strategy",
    "rapid_improvement_wins",
    "runtime_wins",
    "combined_wins",
    *_TIMING_STAT_COLUMNS,
]


def _read_report_rows(report_path: Path) -> List[Dict[str, str]]:
    """Read every row of a benchmark ``report.csv`` as string dicts.

    Args:
        report_path: Path to a previously written ``report.csv``.

    Returns:
        List of row dicts keyed by the CSV header names. Rows are returned
        in on-disk order.

    Raises:
        FileNotFoundError: If ``report_path`` does not exist.
    """
    if not report_path.exists():
        raise FileNotFoundError(f"Report CSV not found: {report_path}")
    with open(report_path, newline="", encoding="utf-8") as csvfile:
        return list(csv.DictReader(csvfile))


def _report_float(row: Dict[str, str], column: str) -> float:
    """Coerce a numeric CSV cell to ``float`` with lenient fallbacks.

    Mirrors the ``"" -> 0.0`` coercion used by :func:`_select_ensemble_winner`
    and additionally treats non-numeric junk (possible in hand-edited or
    truncated historical reports) as ``0.0`` rather than raising.

    Args:
        row: Row dict read from ``report.csv`` (or an in-memory row dict).
        column: Column name to read.

    Returns:
        The cell value as a float, or ``0.0`` when empty/missing/invalid.
    """
    raw = str(row.get(column) or "").strip()
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _group_eligible_rows_by_file(
    rows: List[Dict[str, str]],
) -> List[Tuple[str, List[Dict[str, str]]]]:
    """Group rows by file, keeping only strategies eligible to win.

    An eligible row has ``status == "success"`` and a ``strategy_name`` that
    is not the ``no-opt`` baseline or one of the sentinel names. Files with
    no eligible rows are omitted entirely (per the winners-report contract).

    Args:
        rows: Raw rows read from ``report.csv``.

    Returns:
        List of ``(file_name, eligible_rows)`` tuples in first-appearance
        order of the files, mirroring :func:`build_ensemble_rows`.
    """
    files_in_order: List[str] = []
    grouped: Dict[str, List[Dict[str, str]]] = {}
    for row in rows:
        file_name = row.get("file_name") or ""
        if row.get("status") != "success":
            continue
        if (row.get("strategy_name") or "") in _INELIGIBLE_WINNER_STRATEGIES:
            continue
        if file_name not in grouped:
            grouped[file_name] = []
            files_in_order.append(file_name)
        grouped[file_name].append(row)
    return [(name, grouped[name]) for name in files_in_order]


def _combined_scores(rows: List[Dict[str, str]]) -> List[float]:
    """Compute the combined quality/speed score for one file's eligible rows.

    Both criteria are min-max normalised *within the file* into [0, 1]
    "goodness" scores — which cancels scale differences between tiny and
    huge files and tolerates negative improvements or multi-second runtimes
    without outlier blow-ups — then averaged with equal weight:

    $$score = w \\cdot \\frac{imp - imp_{min}}{imp_{max} - imp_{min}}
            + (1 - w) \\cdot \\frac{t_{max} - t}{t_{max} - t_{min}}$$

    where $imp$ is ``rapid_improvement_pct`` (higher is better), $t$ is
    ``time_ms`` (lower is better), and $w$ is
    :data:`_COMBINED_IMPROVEMENT_WEIGHT`. A criterion with zero spread
    (e.g. a single successful strategy, or exact ties) contributes ``1.0``
    for every row, so it cannot create a false winner.

    Args:
        rows: Eligible (successful, non-baseline) rows for a single file.

    Returns:
        Score per input row, in the same order. Higher is better.
    """
    improvements = [_report_float(row, "rapid_improvement_pct") for row in rows]
    times = [_report_float(row, "time_ms") for row in rows]

    def _normalise(values: List[float], higher_is_better: bool) -> List[float]:
        lo = min(values)
        hi = max(values)
        spread = hi - lo
        if math.isclose(spread, 0.0, abs_tol=1e-12):
            return [1.0] * len(values)
        if higher_is_better:
            return [(v - lo) / spread for v in values]
        return [(hi - v) / spread for v in values]

    imp_norm = _normalise(improvements, higher_is_better=True)
    time_norm = _normalise(times, higher_is_better=False)
    w = _COMBINED_IMPROVEMENT_WEIGHT
    return [w * i + (1.0 - w) * t for i, t in zip(imp_norm, time_norm)]


def _select_rapid_winner(rows: List[Dict[str, str]]) -> Dict[str, str]:
    """Pick the row with the best ``rapid_improvement_pct`` for one file.

    Tie-breaks: fastest ``time_ms``, then alphabetical ``strategy_name``
    (so results are deterministic even across reruns of the same batch).

    Args:
        rows: Eligible rows for a single file (non-empty).

    Returns:
        The winning row.
    """
    return min(
        rows,
        key=lambda r: (
            -_report_float(r, "rapid_improvement_pct"),
            _report_float(r, "time_ms"),
            r.get("strategy_name") or "",
        ),
    )


def _select_time_winner(rows: List[Dict[str, str]]) -> Dict[str, str]:
    """Pick the fastest row (lowest ``time_ms``) for one file.

    Tie-breaks: best ``rapid_improvement_pct``, then alphabetical
    ``strategy_name``.

    Args:
        rows: Eligible rows for a single file (non-empty).

    Returns:
        The winning row.
    """
    return min(
        rows,
        key=lambda r: (
            _report_float(r, "time_ms"),
            -_report_float(r, "rapid_improvement_pct"),
            r.get("strategy_name") or "",
        ),
    )


def _select_combined_winner(rows: List[Dict[str, str]]) -> Tuple[Dict[str, str], float]:
    """Pick the best combined quality/speed row for one file.

    Scores rows with :func:`_combined_scores` and returns the highest.
    Ties (common with exactly two strategies whose rankings oppose, where
    both score 0.5) resolve to the faster strategy, then alphabetical
    ``strategy_name``.

    Args:
        rows: Eligible rows for a single file (non-empty).

    Returns:
        Tuple ``(winning_row, winning_score)`` where ``winning_score`` is
        the combined score of the winner (for the ``combined_score`` column
        in the combined winners CSV).
    """
    scores = _combined_scores(rows)
    ranked = min(
        range(len(rows)),
        key=lambda i: (
            -scores[i],
            _report_float(rows[i], "time_ms"),
            rows[i].get("strategy_name") or "",
        ),
    )
    return rows[ranked], scores[ranked]


def _ratio_stats(
    rows: List[Dict[str, str]],
    denominator_column: str,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Compute min/max/average of ``time_ms`` divided by ``denominator_column``.

    Rows whose denominator is missing, zero or negative are skipped — they
    would divide by zero or yield meaningless ratios (e.g. legacy rows
    written before the ``before_*`` columns were populated).

    Args:
        rows: Successful rows for a single strategy across the batch.
        denominator_column: Column holding the per-file workload size
            (``before_paths`` or ``before_segments``).

    Returns:
        ``(min, max, avg)`` in milliseconds per unit, or
        ``(None, None, None)`` when no row has a usable denominator.
    """
    ratios: List[float] = []
    for row in rows:
        denominator = _report_float(row, denominator_column)
        if denominator <= 0.0:
            continue
        ratios.append(_report_float(row, "time_ms") / denominator)
    if not ratios:
        return None, None, None
    return min(ratios), max(ratios), sum(ratios) / len(ratios)


def _round_or_none(value: Optional[float], digits: int) -> Optional[float]:
    """Round ``value`` to ``digits`` decimal places, passing ``None`` through.

    Args:
        value: Value to round, or ``None``.
        digits: Number of decimal places.

    Returns:
        The rounded value, or ``None``.
    """
    return None if value is None else round(value, digits)


def _compute_timing_stats(
    grouped: List[Tuple[str, List[Dict[str, str]]]],
) -> Dict[str, Dict[str, Any]]:
    """Aggregate per-strategy runtime statistics across the whole batch.

    For every strategy observed in the eligible rows, computes the slowest
    single-file runtime plus min/max/average runtime per path and per
    segment. These describe each strategy's cost profile independently of
    which files it won and are printed in the stdout summary table (and its
    winner-summary CSV mirror).

    Args:
        grouped: Per-file eligible rows from
            :func:`_group_eligible_rows_by_file`.

    Returns:
        Mapping of ``strategy_name -> {column: value}`` for every column in
        :data:`_TIMING_STAT_COLUMNS`. Values are rounded floats, or ``None``
        when no row had a usable path/segment denominator.
    """
    rows_by_strategy: Dict[str, List[Dict[str, str]]] = {}
    for _file_name, file_rows in grouped:
        for row in file_rows:
            strategy = str(row.get("strategy_name") or "")
            rows_by_strategy.setdefault(strategy, []).append(row)

    stats: Dict[str, Dict[str, Any]] = {}
    for strategy, strategy_rows in rows_by_strategy.items():
        times = [_report_float(row, "time_ms") for row in strategy_rows]
        path_min, path_max, path_avg = _ratio_stats(strategy_rows, "before_paths")
        seg_min, seg_max, seg_avg = _ratio_stats(strategy_rows, "before_segments")
        stats[strategy] = {
            "max_time_ms": round(max(times), 2) if times else None,
            "min_ms_per_path": _round_or_none(path_min, 6),
            "max_ms_per_path": _round_or_none(path_max, 6),
            "avg_ms_per_path": _round_or_none(path_avg, 6),
            "min_ms_per_segment": _round_or_none(seg_min, 6),
            "max_ms_per_segment": _round_or_none(seg_max, 6),
            "avg_ms_per_segment": _round_or_none(seg_avg, 6),
        }
    return stats


def _format_stat_number(value: Optional[float]) -> str:
    """Render an optional statistic compactly for the stdout table.

    Args:
        value: Statistic to render, or ``None`` when unavailable.

    Returns:
        ``"n/a"`` for ``None``, otherwise the value in up to six
        significant digits with trailing zeros stripped.
    """
    return "n/a" if value is None else f"{value:.6g}"


def _job_timing_fields(row: Dict[str, str]) -> Dict[str, Any]:
    """Compute a single job's own runtime per path and per segment.

    Args:
        row: One report row (a single file/strategy run).

    Returns:
        Mapping with :data:`_WINNER_JOB_TIMING_COLUMNS` keys holding the
        row's ``time_ms`` divided by its ``before_paths`` /
        ``before_segments`` (rounded to six decimals), or ``""`` when the
        row has no recorded runtime or the denominator is missing, zero or
        negative.
    """
    raw_time = row.get("time_ms")
    if raw_time is None or not str(raw_time).strip():
        return dict.fromkeys(_WINNER_JOB_TIMING_COLUMNS, "")
    time_ms = _report_float(row, "time_ms")
    denominators = ("before_paths", "before_segments")
    fields: Dict[str, Any] = {}
    for column, denominator_column in zip(_WINNER_JOB_TIMING_COLUMNS, denominators):
        denominator = _report_float(row, denominator_column)
        if denominator <= 0.0:
            fields[column] = ""
        else:
            fields[column] = round(time_ms / denominator, 6)
    return fields


def _winner_summary_rows(
    win_counts: Dict[str, Dict[str, int]],
    timing_stats: Dict[str, Dict[str, Any]],
) -> List[Dict[str, str]]:
    """Build the per-strategy summary table rows (stdout + CSV shared).

    Values are pre-formatted strings so the printed table and the written
    CSV carry identical content; unavailable statistics render as ``n/a``.

    Args:
        win_counts: Mapping of ``strategy_name -> {criterion_key: wins}``
            covering every strategy observed in eligible rows.
        timing_stats: Per-strategy runtime statistics from
            :func:`_compute_timing_stats`; missing strategies render as
            ``n/a``.

    Returns:
        One dict per strategy keyed by :data:`_WINNER_SUMMARY_COLUMNS`.
    """
    criterion_columns = {
        _CRITERION_RAPID: "rapid_improvement_wins",
        _CRITERION_TIME: "runtime_wins",
        _CRITERION_COMBINED: "combined_wins",
    }
    rows: List[Dict[str, str]] = []
    for strategy, counts in win_counts.items():
        stats = timing_stats.get(strategy, {})
        rows.append(
            {
                "strategy": strategy,
                **{
                    criterion_columns[criterion]: str(counts.get(criterion, 0))
                    for criterion in criterion_columns
                },
                **{
                    column: _format_stat_number(stats.get(column))
                    for column in _TIMING_STAT_COLUMNS
                },
            }
        )
    return rows


def _print_winner_summary(
    win_counts: Dict[str, Dict[str, int]],
    timing_stats: Dict[str, Dict[str, Any]],
) -> None:
    """Print the per-strategy win-count and runtime-statistics table.

    Args:
        win_counts: Mapping of ``strategy_name -> {criterion_key: wins}``
            covering every strategy observed in eligible rows.
        timing_stats: Per-strategy runtime statistics from
            :func:`_compute_timing_stats`; missing strategies render as
            ``n/a`` / blank cells.
    """
    rows = _winner_summary_rows(win_counts, timing_stats)
    headers = tuple(_WINNER_SUMMARY_COLUMNS)
    table_rows = [headers] + [tuple(row[column] for column in headers) for row in rows]
    widths = [max(len(row[col]) for row in table_rows) for col in range(len(headers))]
    separator = "  "

    def _format_row(row: Tuple[str, ...]) -> str:
        return separator.join(value.ljust(widths[i]) for i, value in enumerate(row))

    print(_format_row(headers))
    print(separator.join("-" * width for width in widths))
    for row in table_rows[1:]:
        print(_format_row(row))


def analyze_report_winners(report_path: Path) -> Dict[str, Dict[str, int]]:
    """Comparative post-processing of a benchmark ``report.csv``.

    Re-reads the per-(file, strategy) report written by a benchmark run and,
    for every file with at least one successful strategy excluding the
    ``no-opt`` baseline, selects a winning strategy under three criteria:

    1. **rapid improvement** — highest ``rapid_improvement_pct``.
    2. **runtime** — lowest ``time_ms``.
    3. **combined** — highest per-file min-max normalised blend of the two
       (see :func:`_combined_scores`).

    Three winners CSVs are written next to ``report.csv``, named after its
    stem (e.g. ``report_rapid_improvement_winners.csv`` for ``report.csv``).
    The rapid and time CSVs reuse :data:`CSV_COLUMNS` and contain the full
    winning row (including its canonical ``ms_per_path`` / ``ms_per_segment``
    columns); the combined CSV appends a ``combined_score`` column. Files
    without any eligible winner are omitted from all three CSVs.

    Finally, a win-count + runtime-statistics summary table is printed to
    stdout and mirrored to ``<stem>_winner_summary.csv`` (one row per
    strategy, columns in :data:`_WINNER_SUMMARY_COLUMNS`).

    Args:
        report_path: Path to a ``report.csv`` produced by :func:`main`.

    Returns:
        Mapping of ``strategy_name -> {criterion: wins}`` in first-seen
        strategy order, counting only strategies that appeared in eligible
        rows. Criterion keys are ``"rapid_improvement"``, ``"time"`` and
        ``"combined"``.

    Raises:
        FileNotFoundError: If ``report_path`` does not exist.
    """
    rows = _read_report_rows(report_path)
    grouped = _group_eligible_rows_by_file(rows)
    timing_stats = _compute_timing_stats(grouped)

    rapid_winners: List[Dict[str, Any]] = []
    time_winners: List[Dict[str, Any]] = []
    combined_winners: List[Dict[str, Any]] = []
    win_counts: Dict[str, Dict[str, int]] = {}

    def _register(strategy_name: str) -> Dict[str, int]:
        return win_counts.setdefault(
            strategy_name,
            dict.fromkeys((_CRITERION_RAPID, _CRITERION_TIME, _CRITERION_COMBINED), 0),
        )

    # Pre-register every strategy observed in eligible rows (first-seen
    # order) so the summary table also lists strategies that never won.
    for _name, file_rows in grouped:
        for eligible in file_rows:
            _register(str(eligible.get("strategy_name") or ""))

    def _tally(strategy_name: str, criterion: str) -> None:
        _register(strategy_name)[criterion] += 1

    def _stamp_job_timing(winner_row: Dict[str, Any], winner: Dict[str, str]) -> None:
        """Attach the winning job's own per-path / per-segment runtime.

        New reports already carry these canonical columns; recomputing keeps
        the values correct for historical reports written before the columns
        existed.
        """
        winner_row.update(_job_timing_fields(winner))

    for _file_name, file_rows in grouped:
        rapid_winner = _select_rapid_winner(file_rows)
        time_winner = _select_time_winner(file_rows)
        combined_winner, combined_score = _select_combined_winner(file_rows)

        rapid_row: Dict[str, Any] = dict(rapid_winner)
        _stamp_job_timing(rapid_row, rapid_winner)
        rapid_winners.append(rapid_row)

        time_row: Dict[str, Any] = dict(time_winner)
        _stamp_job_timing(time_row, time_winner)
        time_winners.append(time_row)

        combined_row: Dict[str, Any] = dict(combined_winner)
        combined_row["combined_score"] = round(combined_score, 4)
        _stamp_job_timing(combined_row, combined_winner)
        combined_winners.append(combined_row)

        _tally(str(rapid_winner.get("strategy_name") or ""), _CRITERION_RAPID)
        _tally(str(time_winner.get("strategy_name") or ""), _CRITERION_TIME)
        _tally(str(combined_winner.get("strategy_name") or ""), _CRITERION_COMBINED)

    stem = report_path.stem
    output_dir = report_path.parent
    write_report(rapid_winners, output_dir / f"{stem}_{_CRITERION_RAPID}_winners.csv", CSV_COLUMNS)
    write_report(time_winners, output_dir / f"{stem}_{_CRITERION_TIME}_winners.csv", CSV_COLUMNS)
    write_report(
        combined_winners,
        output_dir / f"{stem}_{_CRITERION_COMBINED}_winners.csv",
        [*CSV_COLUMNS, "combined_score"],
    )
    # Mirror the stdout summary table to disk so it can be loaded later.
    # write_report() is a no-op when no strategy won, matching the printed
    # "winners CSVs not written" notice.
    write_report(
        _winner_summary_rows(win_counts, timing_stats),
        output_dir / f"{stem}_winner_summary.csv",
        _WINNER_SUMMARY_COLUMNS,
    )

    print()
    print("=" * 60)
    print("STRATEGY WINNER SUMMARY (per-file wins, excluding no-opt)")
    print(f"  Files with winners: {len(grouped)}")
    if win_counts:
        _print_winner_summary(win_counts, timing_stats)
    else:
        print("  No successful non-baseline strategies found; winners CSVs not written.")
    return win_counts


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for the batch benchmark utility.

    Args:
        argv: Optional argument list. When ``None`` (the default), arguments
            are read from :data:`sys.argv`; when supplied, the list is used
            as-is. This indirection is purely a testability hook — the CLI
            contract is identical either way.

    Returns:
        Exit code (0 for success, 1 for invalid arguments or a missing
        ``--analyze-only`` report).
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
            "  python examples/benchmark.py /path/to/cad_files/ --workers 8\n"
            "  python plt_optimizer/cli/benchmark.py --analyze-only "
            "/path/to/cad_files_benchmark/report.csv\n"
        ),
    )
    parser.add_argument(
        "input_dir",
        type=Path,
        nargs="?",
        default=None,
        help=(
            "Directory containing PLT files to process. Required unless --analyze-only is given."
        ),
    )
    parser.add_argument(
        "--analyze-only",
        type=Path,
        default=None,
        metavar="REPORT_CSV",
        help=(
            "Skip benchmarking and only run the winners post-processing "
            "(analyze_report_winners) on an existing report.csv, rewriting "
            "the winners CSVs beside it and printing the summary table."
        ),
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
    parser.add_argument(
        "--ensemble-timeout",
        type=float,
        default=10.0,
        help=(
            "Seconds each optimization job may take before it is aborted and "
            "marked as failed (default: 10.0). Applies to every individual "
            "strategy run (each executes in a killable subprocess) and to every "
            "ParallelEnsemble member job in the real ensemble run appended to "
            "each file's rows"
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help=(
            "Number of parallel worker processes to use. Defaults to the "
            "CPU count reported by the OS, capped at the number of files."
        ),
    )

    args = parser.parse_args(argv)

    if args.analyze_only is not None:
        if args.input_dir is not None:
            print(
                "Error: input_dir and --analyze-only are mutually exclusive.",
                file=sys.stderr,
            )
            return 1
        try:
            analyze_report_winners(args.analyze_only)
        except FileNotFoundError as analyze_err:
            print(f"Error: {analyze_err}", file=sys.stderr)
            return 1
        return 0

    if args.input_dir is None:
        print(
            "Error: input_dir is required unless --analyze-only is given.",
            file=sys.stderr,
        )
        return 1

    input_dir: Path = args.input_dir
    if not input_dir.exists() or not input_dir.is_dir():
        print(f"Error: Input directory not found: {input_dir}", file=sys.stderr)
        return 1

    output_dir = build_output_directory(input_dir)

    text_logger = get_text_logger()
    metrics_logger = get_metrics_logger()

    plt_files = find_plt_files(input_dir)
    worker_count = args.workers or min(len(plt_files), os.cpu_count() or 1)
    worker_count = max(1, worker_count)
    print("PLT-Optimizer Batch Benchmark")
    print(f"  Input:    {input_dir}")
    print(f"  Output:   {output_dir}")
    print(f"  Files:    {len(plt_files)}")
    print(f"  Workers:  {worker_count}")
    print(f"  Strategies: {', '.join(STRATEGY_REGISTRY.keys())}, ensemble")
    print(f"  Strategy job timeout: {args.ensemble_timeout}s")
    print("=" * 60)

    if not plt_files:
        print("No PLT files found in input directory, exiting.")
        return 0

    text_logger.info(f"Benchmark starting: {len(plt_files)} file(s), {worker_count} worker(s)")

    report_path = output_dir / "report.csv"
    ensemble_report_path = output_dir / "ensemble_report.csv"

    all_rows: list[dict[str, Any]] = []
    success_count = 0
    failure_count = 0
    completed_count = 0
    total_started = time.perf_counter()
    rolling_window: list[float] = []  # last N per-file durations for ETA
    eta_window = 10

    def _record_completion(
        index: int,
        plt_file: Path,
        result: FileResult | None,
        error: BaseException | None,
    ) -> None:
        """Handle a single completed future: log, write, update progress.

        Runs on the main process (the ``as_completed`` loop's thread). It is
        the only place that touches the report writer, the text logger, the
        metrics logger, or stdout — keeping all I/O serialized avoids the
        need for locks across processes/threads.
        """
        nonlocal success_count, failure_count, completed_count
        elapsed = result.elapsed_s if result is not None else 0.0
        rolling_window.append(elapsed)
        if len(rolling_window) > eta_window:
            rolling_window.pop(0)
        completed_count += 1

        if error is not None:
            failure_count += 1
            err_msg = f"{type(error).__name__}: {error}"
            text_logger.error(f"[{index}/{len(plt_files)}] {plt_file.name} crashed: {err_msg}")
            text_logger.error(traceback.format_exc())
            print(f"[{index}/{len(plt_files)}] {plt_file.name} CRASHED: {err_msg} ({elapsed:.2f}s)")
            return

        assert result is not None  # for type-checkers
        rows = result.rows
        all_rows.extend(rows)

        # Re-emit metrics events from the worker rows to the global CSV.
        for row in rows:
            _log_metrics_from_row(row, metrics_logger)

        # Stream the per-strategy rows into report.csv immediately.
        for row in rows:
            report_writer.write_row(row)

        ok, summary = _summarize_file_result(rows)
        if ok:
            success_count += 1
        else:
            failure_count += 1

        avg = sum(rolling_window) / len(rolling_window) if rolling_window else 0.0
        remaining = len(plt_files) - completed_count
        eta_s = avg * remaining if avg > 0 else 0.0
        text_logger.info(
            f"[{index}/{len(plt_files)}] {plt_file.name} done in "
            f"{elapsed:.2f}s (avg {avg:.2f}s, ETA {eta_s:.1f}s)"
        )
        text_logger.info(f"  -> {summary}")
        print(
            f"[{index}/{len(plt_files)}] {plt_file.name} ... {summary} "
            f"({elapsed:.2f}s, avg {avg:.2f}s, ETA {eta_s:.1f}s)"
        )

    with ReportWriter(report_path, CSV_COLUMNS) as report_writer:
        # ``spawn`` is the default on Windows and macOS; it guarantees child
        # processes start with a clean interpreter, which is the safest
        # choice given the matplotlib/cProfile heavy imports in the
        # optimization pipeline.
        executor_kwargs: dict[str, Any] = {}
        if hasattr(os, "sched_getaffinity"):
            # On Linux we can respect the process affinity mask so we don't
            # over-subscribe cores that aren't actually available.
            try:
                cpu_quota = len(os.sched_getaffinity(0))
                worker_count = min(worker_count, cpu_quota)
                executor_kwargs["max_workers"] = max(1, worker_count)
            except OSError:
                executor_kwargs["max_workers"] = worker_count
        else:
            executor_kwargs["max_workers"] = worker_count

        with ProcessPoolExecutor(**executor_kwargs) as executor:
            # Submit every file up front, then process completions as they
            # arrive so we get the streaming-CSV and ETA benefits even when
            # file sizes vary wildly.
            future_to_file: dict[Future[FileResult], tuple[int, Path]] = {}
            for index, plt_file in enumerate(plt_files, start=1):
                future = executor.submit(
                    _process_file_worker,
                    str(plt_file),
                    str(output_dir),
                    args.same_row_preference,
                    args.ensemble_timeout,
                )
                future_to_file[future] = (index, plt_file)

            text_logger.info(f"Submitted {len(plt_files)} file(s) to {worker_count} worker(s)")

            for future in as_completed(future_to_file):
                index, plt_file = future_to_file[future]
                try:
                    result = future.result()
                    _record_completion(index, plt_file, result, None)
                except Exception as future_err:  # noqa: BLE001 - propagate worker failures
                    _record_completion(index, plt_file, None, future_err)

    total_elapsed = time.perf_counter() - total_started
    text_logger.info(f"Benchmark finished in {total_elapsed:.2f}s")

    # The ensemble report depends on every file's per-strategy rows, so it
    # is computed after the parallel loop finishes. The write itself is a
    # single bulk operation since the per-file data has already been
    # collected into ``all_rows``.
    ensemble_rows = build_ensemble_rows(all_rows)
    write_report(ensemble_rows, ensemble_report_path, CSV_COLUMNS)

    # Comparative post-processing: per-file winners per criterion + stdout
    # summary table. Re-reads report.csv from disk (the streaming writer is
    # closed by now) so the same code path works on historical reports.
    analyze_report_winners(report_path)

    avg_per_file = total_elapsed / len(plt_files) if plt_files else 0.0
    text_logger.info(
        f"Wrote {len(all_rows)} per-strategy row(s) and {len(ensemble_rows)} "
        f"ensemble row(s); avg {avg_per_file:.2f}s/file in parallel"
    )

    print()
    print("=" * 60)
    print("BENCHMARK COMPLETE")
    print(f"  Total files:        {len(plt_files)}")
    print(f"  Successful files:   {success_count}")
    print(f"  Failed files:       {failure_count}")
    print(f"  Total CSV rows:     {len(all_rows)}")
    print(f"  Wall time:          {total_elapsed:.2f}s (avg {avg_per_file:.2f}s/file)")
    print(f"  Per-strategy CSV:   {report_path}")
    print(f"  Ensemble CSV:       {ensemble_report_path}")
    print(f"  Optimized:          {output_dir / 'optimized'}")
    print(f"  Plots:              {output_dir / 'plots'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
