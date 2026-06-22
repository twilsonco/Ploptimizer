"""Optimize subcommand for PLT-Optimizer CLI.

This module provides the 'optimize' command which reads an existing PLT file,
applies the full optimization pipeline, and writes the result to an output file.

Usage:
    plt-optimizer optimize input.plt -o output.plt
    plt-optimizer optimize input.plt --fast-mode
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path

# Local imports
from plt_optimizer.core.chunker import Chunker, ChunkerConfig
from plt_optimizer.core.optimizer import (
    NearestNeighbor2OptStrategy,
    OptimizationStrategy,
    OptimizerEngine,
    ParallelEnsembleOptimizationResult,
    ParallelEnsembleStrategy,
)
from plt_optimizer.core.parser import PLTParser
from plt_optimizer.core.profiler import Profiler
from plt_optimizer.core.reassembler import MetricsCalculator, Reassembler
from plt_optimizer.core.writer import PLTWriter
from plt_optimizer.utils.geometry import fracture_linear_paths, remove_redundant_strokes
from plt_optimizer.utils.logging import setup_logging


def setup_parser(parser: argparse.ArgumentParser) -> None:
    """Configure argument parser for the optimize subcommand.

    Args:
        parser: ArgumentParser instance to configure.
    """
    parser.add_argument(
        "input",
        type=Path,
        help="Input PLT/HPGL file to optimize.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help=("Output file path. If not specified, appends '_optimized' to the input filename."),
    )
    parser.add_argument(
        "--fast-mode",
        action="store_true",
        help=(
            "Use NearestNeighbor2OptStrategy exclusively for faster processing. "
            "If not specified, uses ParallelEnsembleStrategy which runs multiple "
            "strategies and selects the best result."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG) output.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help=(
            "Directory for log files. If not specified, logs are written "
            "to ./logs_optimize/. Creates optimizer.log and job_metrics.csv."
        ),
    )


def run(args: argparse.Namespace) -> int:
    """Execute the optimize command.

    Args:
        args: Parsed command-line arguments namespace.

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    input_path = args.input

    # Validate input file exists
    if not input_path.exists():
        print(f"Error: Input file does not exist: {input_path}", file=sys.stderr)
        return 1

    if not input_path.is_file():
        print(f"Error: Input path is not a file: {input_path}", file=sys.stderr)
        return 1

    # Determine output path
    if args.output is not None:
        output_path = args.output
    else:
        output_path = input_path.parent / f"{input_path.stem}_optimized.plt"

    # Set up logging
    log_dir = args.log_dir if args.log_dir is not None else Path("./logs_optimize")
    text_log_file = log_dir / "optimizer.log"
    csv_metrics_file = log_dir / "job_metrics.csv"

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        print(f"Error: Cannot create log directory '{log_dir}': {e}", file=sys.stderr)
        return 1

    text_logger_obj, metrics_logger = setup_logging(
        text_log_file=text_log_file,
        csv_metrics_file=csv_metrics_file,
    )

    # Set logging level based on verbosity
    if args.verbose:
        text_logger_obj.logger.setLevel(logging.DEBUG)

    job_id = f"opt_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    text_logger_obj.info(f"[{job_id}] Starting optimization: {input_path}")

    try:
        # Parse the file
        parser = PLTParser()
        doc = parser.parse_file(input_path)
        text_logger_obj.debug(f"[{job_id}] Parsed document with {doc.total_segments} segments")

        # Profile to determine document type BEFORE any preprocessing
        profiler = Profiler()
        profile_result = profiler.profile(doc)
        text_logger_obj.debug(
            f"[{job_id}] Document classified as "
            f"{'structural' if profile_result.is_structural else 'text'}"
        )

        metrics_calc = MetricsCalculator()

        # Calculate original distance (before any simplification)
        original_distance = metrics_calc.calculate_original_travel_distance(doc)

        # Bifurcate preprocessing pipeline based on document type
        if profile_result.is_structural:
            # STRUCTURAL PIPELINE: Fracture linear paths then remove redundancies
            doc = fracture_linear_paths(doc)
            text_logger_obj.debug(
                f"[{job_id}] Fractured structural document (linear paths -> independent segments)"
            )
            doc = remove_redundant_strokes(doc, tol=1e-3)
            text_logger_obj.debug(f"[{job_id}] Removed redundant strokes from fractured document")
        else:
            # TEXT PIPELINE: Skip stroke simplification to preserve contiguous paths
            text_logger_obj.debug(f"[{job_id}] Skipped stroke simplification for text document")

        chunker = Chunker(config=ChunkerConfig(threshold_multiplier=2.0))
        blocks = chunker.chunk(
            doc.stroke_paths,
            profile_result.baseline_extent,
            is_structural=profile_result.is_structural,
        )

        if not blocks:
            text_logger_obj.warning(f"[{job_id}] No blocks generated from file")
            return 1

        # Select strategy based on fast_mode
        if args.fast_mode:
            strategy: OptimizationStrategy = NearestNeighbor2OptStrategy()
        else:
            strategy = ParallelEnsembleStrategy(baseline_distance=original_distance)

        # Optimize
        optimizer = OptimizerEngine(strategy=strategy)
        optimization_result = optimizer.optimize(blocks)

        # Handle Parallel Ensemble results (contains winner info + all benchmarks)
        if isinstance(optimization_result, ParallelEnsembleOptimizationResult):
            ensemble_result = optimization_result
            method_name = ensemble_result.winner_name
            optimized_distance = ensemble_result.result.total_travel_distance

            # Log all strategy results at INFO level
            text_logger_obj.info(f"[{job_id}] Strategy benchmark results:")
            for bench in ensemble_result.all_benchmarks:
                imp_str = (
                    f"{bench.improvement_percent:.2f}% improvement"
                    if bench.improvement_percent is not None
                    else "no baseline comparison"
                )
                text_logger_obj.info(
                    f"  {bench.strategy_name}: "
                    f"distance={bench.result.total_travel_distance:.3f}, "
                    f"{imp_str} ({bench.execution_time_seconds:.3f}s)"
                )

            # Build notes from all benchmarks
            notes_parts = []
            for bench in ensemble_result.all_benchmarks:
                imp_str = (
                    f"{bench.improvement_percent:.2f}%"
                    if bench.improvement_percent is not None
                    else "N/A"
                )
                notes_parts.append(
                    f"{bench.strategy_name}: {bench.result.total_travel_distance:.3f} "
                    f"(improvement={imp_str})"
                )
            method_notes = "; ".join(notes_parts)
        else:
            method_name = "NearestNeighbor + 2-Opt (Fast Mode)"
            optimized_distance = optimization_result.total_travel_distance
            method_notes = f"optimized_distance={optimized_distance:.3f}"

        # Reassemble using the actual result (unwrapped if ensemble)
        reassembler = Reassembler()
        if isinstance(optimization_result, ParallelEnsembleOptimizationResult):
            result_for_reassembly = ensemble_result.result
        else:
            result_for_reassembly = optimization_result
        optimized_doc = reassembler.reassemble(doc, blocks, result_for_reassembly)

        # Write optimized file
        writer = PLTWriter()
        writer.write_file(optimized_doc, output_path)

        # Calculate improvement percentage
        improvement_pct = (
            ((original_distance - optimized_distance) / original_distance * 100)
            if original_distance > 0
            else 0.0
        )

        # Log success metrics
        text_logger_obj.info(
            f"[{job_id}] Success: {input_path.name} -> {output_path.name} "
            f"(saved {improvement_pct:.1f}%)"
        )

        metrics_logger.log_job(
            job_id=job_id,
            original_file=input_path,
            optimized_file=output_path,
            original_distance=original_distance,
            optimized_distance=optimized_distance,
            status="success",
            method=method_name,
            notes=method_notes,
        )

        # Print summary to stdout (unless verbose, in which case it's already logged)
        if not args.verbose:
            print(
                f"Optimized: {input_path.name} -> {output_path.name} (saved {improvement_pct:.1f}%)"
            )

        return 0

    except Exception as e:
        text_logger_obj.error(f"[{job_id}] Optimization failed: {e}")
        if args.verbose:
            import traceback

            text_logger_obj.debug(traceback.format_exc())

        metrics_logger.log_job(
            job_id=job_id,
            original_file=input_path,
            optimized_file=None,
            original_distance=0.0,
            optimized_distance=0.0,
            status="failed",
            method="",
            notes=str(e),
        )
        return 1
