"""Watch-directory daemon for automated PLT file optimization.

This module provides a file system watcher that monitors a directory for new or
modified PLT/HPGL files and automatically optimizes them using the configured
strategy.

Usage:
    python -m plt_optimizer.cli.watch --watch-dir /path/to/watch \
                                       --output-dir /path/to/output \
                                       --log-dir /path/to/logs

    # Fast mode (uses NearestNeighbor2OptStrategy exclusively):
    python -m plt_optimizer.cli.watch --watch-dir /path/to/watch \
                                       --fast-mode

    # With processed-dir to archive original files after optimization:
    python -m plt_optimizer.cli.watch --watch-dir /input \
                                       --output-dir /output \
                                       --processed-dir /archive
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import signal
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

# Third-party imports
try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError as e:
    raise ImportError(
        "watchdog library is required for watch functionality. Install it with: uv add watchdog"
    ) from e

if TYPE_CHECKING:
    from plt_optimizer.utils.logging import CSVMetricsLogger, TextLogger

# Local imports
from plt_optimizer.core.chunker import Chunker, ChunkerConfig
from plt_optimizer.core.models import PLTDocument
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
from plt_optimizer.utils.logging import CSVMetricsLogger, TextLogger

# File extensions to watch for
SUPPORTED_EXTENSIONS = {".plt", ".hpgl", ".PLT", ".HPGL"}


class PLTFileHandler(FileSystemEventHandler):
    """Handles file system events for PLT files in the watch directory.

    This handler processes new and modified PLT files, optimizing them and
    writing the results to the output directory. Processed files can optionally
    be moved to a separate directory.

    Attributes:
        watch_dir: Directory being watched for changes.
        output_dir: Directory where optimized files are saved.
        processed_dir: Directory to move processed files to (or None).
        text_logger: Logger for text messages.
        metrics_logger: Logger for CSV metrics.
        parser: PLT file parser instance.
        writer: PLT file writer instance.
        fast_mode: If True, uses NearestNeighbor2OptStrategy exclusively.
        processed_files: Set of recently processed file paths to debounce events.
    """

    def __init__(
        self,
        watch_dir: Path,
        output_dir: Path,
        text_logger: TextLogger,
        metrics_logger: CSVMetricsLogger,
        fast_mode: bool = False,
        processed_dir: Optional[Path] = None,
        debug_save_files: bool = False,
        log_dir: Optional[Path] = None,
        temp_dir: Optional[Path] = None,
        debounce_seconds: float = 2.0,
        poll_interval: float = 0.5,
    ) -> None:
        """Initialize the PLT file handler.

        Args:
            watch_dir: Directory to watch for new/modified files.
            output_dir: Directory where optimized files are saved.
            text_logger: Initialized text logger instance.
            metrics_logger: Initialized CSV metrics logger instance.
            fast_mode: If True, use NearestNeighbor2OptStrategy exclusively.
            processed_dir: Directory to move processed files to after optimization.
            debug_save_files: If True, save debug copies of PLT files and plots.
            log_dir: Directory for debug output (required when debug_save_files=True).
            temp_dir: Directory for in-progress output files. Defaults to
                ``output_dir / ".incomplete"``.
            debounce_seconds: Quiet period (seconds) after the last modification
                before a file is considered stable and processed.
            poll_interval: How often the debounce thread polls for stable files.
        """
        super().__init__()
        self._watch_dir = watch_dir
        self._output_dir = output_dir
        self._processed_dir = processed_dir
        self._text_logger = text_logger
        self._metrics_logger = metrics_logger
        self._fast_mode = fast_mode
        self._debug_save_files = debug_save_files
        self._log_dir = log_dir if debug_save_files else None
        self._parser = PLTParser()
        self._writer = PLTWriter()
        self._processed_files: set[Path] = set()
        self._debounce_seconds = debounce_seconds
        self._poll_interval = poll_interval
        self._temp_dir = temp_dir if temp_dir is not None else output_dir / ".incomplete"
        self._pending_files: Dict[Path, float] = {}
        self._pending_lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._debounce_thread: Optional[threading.Thread] = None

    def _is_supported_file(self, path: Path) -> bool:
        """Check if a file has a supported PLT/HPGL extension.

        Args:
            path: File path to check.

        Returns:
            True if the file extension is supported.
        """
        return path.suffix in SUPPORTED_EXTENSIONS

    def _is_plt_file(self, path: Path) -> bool:
        """Check if a path points to an actual PLT file (not directory).

        Args:
            path: Path to check.

        Returns:
            True if it's a supported file (not directory).
        """
        return path.is_file() and self._is_supported_file(path)

    def _should_process(self, path: Path) -> bool:
        """Check if a file is eligible for processing (filtered by state).

        This is a lightweight gate that complements the debounce mechanism.
        Timing-based debouncing (waiting for the file to stop changing) is
        handled separately by the background ``_debounce_loop`` thread;
        this method only filters out files we already processed, files that
        have disappeared, and unsupported files.

        Args:
            path: File path to check.

        Returns:
            True if the file exists, is a supported file type, and has not
            already been processed.
        """
        if path in self._processed_files:
            return False
        try:
            return path.is_file() and self._is_supported_file(path)
        except OSError:
            return False

    def _enqueue_file(self, path: Path) -> None:
        """Record a file as needing processing.

        The background debounce thread will pick the file up once it has been
        quiescent for ``debounce_seconds``. Existing files (e.g. files
        discovered at startup) can also be enqueued; the thread will pick them
        up on the next poll.

        Args:
            path: File path that was created or modified.
        """
        try:
            if not path.exists():
                return
            mtime = path.stat().st_mtime
        except OSError:
            return
        with self._pending_lock:
            self._pending_files[path] = mtime
        self._text_logger.debug(f"File enqueued for processing: {path}")

    def _check_pending_files(self) -> None:
        """Scan pending files and process any that are stable.

        A file is stable when its current ``mtime`` matches the recorded
        ``mtime`` (no new writes) and at least ``debounce_seconds`` have
        elapsed since the last modification. Stable files are processed in
        the calling thread (the debounce thread); errors are logged but do
        not stop the loop.
        """
        to_process: List[Path] = []
        with self._pending_lock:
            for path, recorded_mtime in list(self._pending_files.items()):
                if not path.exists():
                    del self._pending_files[path]
                    continue
                try:
                    current_mtime = path.stat().st_mtime
                except OSError:
                    del self._pending_files[path]
                    continue
                if current_mtime > recorded_mtime:
                    self._pending_files[path] = current_mtime
                    continue
                if time.time() - current_mtime < self._debounce_seconds:
                    continue
                if not self._should_process(path):
                    del self._pending_files[path]
                    continue
                to_process.append(path)
                del self._pending_files[path]
        for path in to_process:
            try:
                self._mark_processed(path)
                self._process_file(path)
            except Exception as e:
                self._text_logger.error(f"Failed to process {path}: {e}")

    def _debounce_loop(self) -> None:
        """Background loop that polls for stable files.

        Runs until ``_shutdown_event`` is set. ``Event.wait`` is used so the
        thread can be awoken (or shut down) without sleeping for the full
        poll interval.
        """
        while not self._shutdown_event.is_set():
            try:
                self._check_pending_files()
            except Exception as e:
                self._text_logger.error(f"Debounce loop error: {e}")
            self._shutdown_event.wait(timeout=self._poll_interval)

    def start(self) -> None:
        """Start the background debounce thread.

        Idempotent: calling this method multiple times is safe and only one
        thread is ever running.
        """
        if self._debounce_thread is not None and self._debounce_thread.is_alive():
            return
        self._shutdown_event.clear()
        self._debounce_thread = threading.Thread(
            target=self._debounce_loop,
            name="plt-debouncer",
            daemon=True,
        )
        self._debounce_thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the debounce thread to stop and wait for it to join.

        Safe to call even if ``start()`` was never invoked.

        Args:
            timeout: Maximum seconds to wait for the thread to exit.
        """
        self._shutdown_event.set()
        thread = self._debounce_thread
        if thread is not None:
            thread.join(timeout=timeout)
            self._debounce_thread = None

    def _mark_processed(self, path: Path) -> None:
        """Mark a file as processed for debouncing.

        Args:
            path: File path that was processed.
        """
        self._processed_files.add(path)
        # Clean up old entries to prevent memory growth
        if len(self._processed_files) > 1000:
            # Remove oldest half
            self._processed_files = set(list(self._processed_files)[-500:])

    def _save_debug_files(
        self,
        job_id: str,
        original_doc: PLTDocument,
        optimized_doc: PLTDocument,
        original_distance: float,
        optimized_distance: float,
    ) -> None:
        """Save debug copies of PLT files and plots.

        Args:
            job_id: Unique identifier for this processing job.
            original_doc: The parsed original document before optimization.
            optimized_doc: The optimized document after reassembly.
            original_distance: Total travel distance before optimization.
            optimized_distance: Total travel distance after optimization.
        """
        if not self._debug_save_files or self._log_dir is None:
            return

        try:
            from plt_optimizer.diagnostics.plotter import plot_plt_document

            debug_dir = self._log_dir / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)

            # Sanitize job_id for use in filenames
            safe_job_id = job_id.replace(":", "-").replace("/", "_")

            # Save original PLT file
            orig_plt_path = debug_dir / f"{safe_job_id}_original.plt"
            orig_plt_path = self._writer._ensure_filename_length(orig_plt_path)
            self._writer.write_file(original_doc, orig_plt_path)

            # Save optimized PLT file
            opt_plt_path = debug_dir / f"{safe_job_id}_optimized.plt"
            opt_plt_path = self._writer._ensure_filename_length(opt_plt_path)
            self._writer.write_file(optimized_doc, opt_plt_path)

            # Calculate improvement percentage for titles
            improvement_pct = (
                ((original_distance - optimized_distance) / original_distance * 100)
                if original_distance > 0
                else 0.0
            )

            # Save plot for original document using plotter.py function
            orig_plot_path = debug_dir / f"{safe_job_id}_original.png"
            orig_plot_path = self._writer._ensure_filename_length(orig_plot_path)
            plot_plt_document(
                original_doc,
                output_path=orig_plot_path,
                title=f"Original - {original_distance:.3f} (Job: {job_id})",
            )

            # Save plot for optimized document using plotter.py function
            opt_plot_path = debug_dir / f"{safe_job_id}_optimized.png"
            opt_plot_path = self._writer._ensure_filename_length(opt_plot_path)
            plot_plt_document(
                optimized_doc,
                output_path=opt_plot_path,
                title=f"Optimized - {optimized_distance:.3f} ({improvement_pct:.1f}% saved) (Job: {job_id})",
            )

            self._text_logger.debug(f"[{job_id}] Saved debug files to {debug_dir}")

        except Exception as e:
            self._text_logger.warning(f"[{job_id}] Failed to save debug files: {e}")

    def _process_file(self, input_path: Path) -> bool:
        """Optimize a single PLT file.

        Args:
            input_path: Path to the input PLT file.

        Returns:
            True if optimization was successful.
        """
        job_id = f"watch_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        self._text_logger.info(f"[{job_id}] Processing: {input_path}")

        # Ensure temp directory exists (lazy; created on first use so that
        # constructing a handler against a not-yet-existent output dir is OK).
        # Failure here is non-fatal: the subsequent write will surface the
        # error in the standard exception handler.
        try:
            self._temp_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._text_logger.warning(
                f"[{job_id}] Could not pre-create temp directory {self._temp_dir}: {e}"
            )

        try:
            # Parse the file
            doc = self._parser.parse_file(input_path)

            # Profile to determine document type BEFORE any preprocessing
            profiler = Profiler()
            profile_result = profiler.profile(doc)
            self._text_logger.debug(
                f"[{job_id}] Document classified as {'structural' if profile_result.is_structural else 'text'}"
            )

            metrics_calc = MetricsCalculator()

            # Calculate original distance (before any simplification)
            original_distance = metrics_calc.calculate_original_travel_distance(doc)

            # Bifurcate preprocessing pipeline based on document type
            if profile_result.is_structural:
                # STRUCTURAL PIPELINE: Fracture linear paths then remove redundancies
                # 1. fracture_linear_paths breaks rectangles/grids into individual segments
                doc = fracture_linear_paths(doc)
                self._text_logger.debug(
                    f"[{job_id}] Fractured structural document (linear paths -> independent segments)"
                )
                # 2. remove_redundant_strokes culls overlapping coincident lines
                doc = remove_redundant_strokes(doc, tol=1e-3)
                self._text_logger.debug(
                    f"[{job_id}] Removed redundant strokes from fractured document"
                )
            else:
                # TEXT PIPELINE: Skip stroke simplification to preserve contiguous paths
                self._text_logger.debug(
                    f"[{job_id}] Skipped stroke simplification for text document"
                )

            chunker = Chunker(config=ChunkerConfig(threshold_multiplier=2.0))
            blocks = chunker.chunk(
                doc.stroke_paths,
                profile_result.baseline_extent,
                is_structural=profile_result.is_structural,
            )

            if not blocks:
                self._text_logger.warning(f"[{job_id}] No blocks generated from file")
                return False

            # Select strategy based on fast_mode
            strategy: OptimizationStrategy
            if self._fast_mode:
                strategy = NearestNeighbor2OptStrategy()
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
                self._text_logger.info(f"[{job_id}] Strategy benchmark results:")
                for bench in ensemble_result.all_benchmarks:
                    imp_str = (
                        f"{bench.improvement_percent:.2f}% improvement"
                        if bench.improvement_percent is not None
                        else "no baseline comparison"
                    )
                    self._text_logger.info(
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
                result_for_reassembly = optimization_result.result
            else:
                result_for_reassembly = optimization_result
            optimized_doc = reassembler.reassemble(doc, blocks, result_for_reassembly)

            # Save debug files if enabled (after reassembly but before writing output)
            self._save_debug_files(
                job_id=job_id,
                original_doc=doc,
                optimized_doc=optimized_doc,
                original_distance=original_distance,
                optimized_distance=optimized_distance,
            )

            # Generate output paths. Write to temp first so consumers never
            # observe a partially-written file; then atomically move to the
            # final location via os.replace (with shutil.move fallback).
            output_path = self._output_dir / f"{input_path.stem}_optimized.plt"
            temp_output_path = self._temp_dir / f"{input_path.stem}_optimized.plt"

            output_path = self._writer._ensure_filename_length(output_path)
            temp_output_path = self._writer._ensure_filename_length(temp_output_path)

            try:
                self._writer.write_file(optimized_doc, temp_output_path)
            except Exception:
                if temp_output_path.exists():
                    try:
                        temp_output_path.unlink()
                    except OSError:
                        pass
                raise

            try:
                os.replace(str(temp_output_path), str(output_path))
            except OSError as replace_error:
                self._text_logger.warning(
                    f"[{job_id}] os.replace failed ({replace_error}); "
                    f"falling back to shutil.move for {output_path.name}"
                )
                shutil.move(str(temp_output_path), str(output_path))

            # Log success metrics
            improvement_pct = (
                ((original_distance - optimized_distance) / original_distance * 100)
                if original_distance > 0
                else 0.0
            )
            self._text_logger.info(
                f"[{job_id}] Success: {input_path.name} -> {output_path.name} "
                f"(saved {improvement_pct:.1f}%)"
            )

            self._metrics_logger.log_job(
                job_id=job_id,
                original_file=input_path,
                optimized_file=output_path,
                original_distance=original_distance,
                optimized_distance=optimized_distance,
                status="success",
                method=method_name,
                notes=method_notes,
            )

            # Archive or delete original file after successful optimization
            if self._processed_dir is not None:
                try:
                    dest_path = self._processed_dir / input_path.name
                    shutil.move(str(input_path), str(dest_path))
                    self._text_logger.debug(
                        f"[{job_id}] Moved {input_path.name} to {self._processed_dir}"
                    )
                except OSError as e:
                    self._text_logger.warning(f"[{job_id}] Failed to move processed file: {e}")
            else:
                # Delete original file from watch directory by default
                try:
                    input_path.unlink()
                    self._text_logger.debug(f"[{job_id}] Deleted original file {input_path.name}")
                except OSError as e:
                    self._text_logger.warning(f"[{job_id}] Failed to delete processed file: {e}")

            return True

        except Exception as e:
            import traceback

            tb_str = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            self._text_logger.error(f"[{job_id}] Failed: {e}")
            self._text_logger.debug(f"[{job_id}] Traceback:\n{tb_str}")

            # Log detailed error info for troubleshooting
            failed_method = "unknown"
            if "method_name" in dir():
                try:
                    failed_method = method_name
                except NameError:  # pragma: no cover
                    pass  # pragma: no cover

            self._metrics_logger.log_job(
                job_id=job_id,
                original_file=input_path,
                optimized_file=None,
                original_distance=0.0,
                optimized_distance=0.0,
                status="failed",
                method=failed_method,
                notes=str(e)[:200],  # Truncate long error messages
            )

            # Fallback: copy unprocessed file to output directory when optimization fails.
            # Use the same atomic-write pattern (temp + os.replace) so consumers
            # never observe a partially-written file.
            try:
                fallback_output_path = self._output_dir / f"{input_path.stem}_unprocessed.plt"
                fallback_temp_path = self._temp_dir / f"{input_path.stem}_unprocessed.plt"
                shutil.copy2(str(input_path), str(fallback_temp_path))
                try:
                    os.replace(str(fallback_temp_path), str(fallback_output_path))
                except OSError as replace_error:
                    self._text_logger.warning(
                        f"[{job_id}] os.replace failed ({replace_error}); "
                        f"falling back to shutil.move for {fallback_output_path.name}"
                    )
                    shutil.move(str(fallback_temp_path), str(fallback_output_path))
                self._text_logger.warning(
                    f"[{job_id}] Optimization failed - copied unprocessed file to "
                    f"{fallback_output_path} for manual review"
                )
                # Remove input file to avoid cluttering the watch directory
                try:
                    input_path.unlink()
                except OSError as remove_error:
                    self._text_logger.warning(
                        f"[{job_id}] Failed to remove input file {input_path}: {remove_error}"
                    )
            except OSError as copy_error:
                self._text_logger.error(
                    f"[{job_id}] Failed to copy unprocessed file to output directory: {copy_error}"
                )

            return False

    def on_created(self, event: FileSystemEvent) -> None:
        """Handle file creation events.

        Args:
            event: The file system event.
        """
        if event.is_directory:
            return

        path = Path(event.src_path)
        if not self._is_plt_file(path):
            return

        self._enqueue_file(path)

    def on_modified(self, event: FileSystemEvent) -> None:
        """Handle file modification events.

        Args:
            event: The file system event.
        """
        if event.is_directory:
            return

        path = Path(event.src_path)
        if not self._is_plt_file(path):
            return

        self._enqueue_file(path)


def run_watcher_from_config(
    config: Dict[str, Any],
    stop_event: threading.Event,
    on_success: Optional[Callable[[str, float], None]] = None,
    on_error: Optional[Callable[[str, str], None]] = None,
) -> int:
    """Run the watcher using a configuration dictionary.

    This function is designed to be called from a background thread
    by the tray application. It blocks until stop_event is set or
    a shutdown signal is received.

    Args:
        config: Configuration dictionary with keys matching DEFAULT_CONFIG.
        stop_event: Threading Event to signal graceful shutdown.
        on_success: Optional callback(filename, improvement_pct) on success.
        on_error: Optional callback(filename, error_msg) on failure.

    Returns:
        Exit code (0 for success).
    """
    # Import here to avoid circular imports and allow standalone use
    from plt_optimizer.utils.logging import setup_logging

    watch_dir = Path(config.get("watch_dir", ""))
    output_dir = Path(config.get("output_dir", "./optimized"))
    log_dir = Path(config.get("log_dir", "./logs"))
    processed_dir = Path(config["processed_dir"]) if config.get("processed_dir") else None
    fast_mode = bool(config.get("fast_mode", False))
    debug_save_files = bool(config.get("debug_save_files", False))
    debounce_seconds = float(config.get("debounce_seconds", 2.0))

    text_log_file = log_dir / "optimizer.log"
    csv_metrics_file = log_dir / "job_metrics.csv"

    # Ensure directories exist
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        if processed_dir is not None:
            processed_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        print(f"Permission denied creating directory: {e}", file=sys.stderr)
        return 1

    # Set up logging
    text_logger, metrics_logger = setup_logging(
        text_log_file=text_log_file,
        csv_metrics_file=csv_metrics_file,
    )

    text_logger.info("=" * 60)
    text_logger.info("PLT-Optimizer Watch Daemon")
    text_logger.info(f"Watch directory: {watch_dir}")
    text_logger.info(f"Output directory: {output_dir}")
    text_logger.info(f"Log directory: {log_dir}")
    if processed_dir is not None:
        text_logger.info(f"Processed directory: {processed_dir}")
    text_logger.info(
        f"Strategy: {'NearestNeighbor2Opt (Fast Mode)' if fast_mode else 'ParallelEnsemble'}"
    )
    text_logger.info(f"Debounce window: {debounce_seconds}s")
    text_logger.info("=" * 60)

    # Validate watch directory
    if not watch_dir.exists():
        text_logger.error(f"Watch directory does not exist: {watch_dir}")
        return 1

    if not watch_dir.is_dir():
        text_logger.error(f"Watch path is not a directory: {watch_dir}")
        return 1

    def signal_handler(signum: int, frame: object) -> None:
        sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        text_logger.info(f"Received {sig_name}, initiating graceful shutdown...")
        stop_event.set()

    # Set up signal handlers only in main thread (check with threading.current_thread)
    is_main_thread = threading.current_thread() == threading.main_thread()
    if is_main_thread:
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    # Process existing files first
    handler = PLTFileHandler(
        watch_dir=watch_dir,
        output_dir=output_dir,
        text_logger=text_logger,
        metrics_logger=metrics_logger,
        fast_mode=fast_mode,
        processed_dir=processed_dir,
        debug_save_files=debug_save_files,
        log_dir=log_dir if debug_save_files else None,
        debounce_seconds=debounce_seconds,
    )
    handler.start()

    enqueued_count = 0
    try:
        for path in watch_dir.iterdir():
            if handler._is_plt_file(path):
                try:
                    if handler._should_process(path):
                        handler._enqueue_file(path)
                        enqueued_count += 1
                except Exception as e:
                    text_logger.error(f"Error enqueuing {path}: {e}")
    except Exception:
        handler.stop()
        raise

    if enqueued_count > 0:
        text_logger.info(f"Enqueued {enqueued_count} existing file(s) for processing")

    # Create event handler for watchdog
    event_handler = PLTFileHandler(
        watch_dir=watch_dir,
        output_dir=output_dir,
        text_logger=text_logger,
        metrics_logger=metrics_logger,
        fast_mode=fast_mode,
        processed_dir=processed_dir,
        debug_save_files=debug_save_files,
        log_dir=log_dir if debug_save_files else None,
        debounce_seconds=debounce_seconds,
    )
    event_handler.start()

    observer = Observer()
    observer.schedule(event_handler, str(watch_dir), recursive=False)  # type: ignore[no-untyped-call]
    observer.start()  # type: ignore[no-untyped-call]

    text_logger.info(f"Watching for PLT files in {watch_dir}")
    text_logger.info("Press Ctrl+C to stop...")

    try:
        while not stop_event.is_set():
            # Check every second (Observer is running in background thread)
            signal.pause() if hasattr(signal, "pause") else time.sleep(1)
    except KeyboardInterrupt:
        text_logger.info("Keyboard interrupt received")
    finally:
        observer.stop()  # type: ignore[no-untyped-call]
        observer.join(timeout=5.0)
        handler.stop()
        event_handler.stop()

    text_logger.info("Watch daemon stopped.")
    return 0


class WatchCommand:
    """Command-line interface for the watch-directory daemon.

    This class handles argument parsing and orchestrates the file system
    watcher for automated PLT optimization via CLI arguments.

    For programmatic use (e.g., from tray application), use run_watcher_from_config()
    directly instead of this class.
    """

    def __init__(self, args: Optional[List[str]] = None) -> None:
        """Initialize the watch command.

        Args:
            args: Command-line arguments (defaults to sys.argv).
        """
        # Check if --log-dir was explicitly provided before parsing
        self._log_dir_explicitly_set = (args is not None and "--log-dir" in args) or (
            args is None and "--log-dir" in sys.argv[1:]
        )
        self._args = self._parse_args(args)
        self._existing_handler: Optional[PLTFileHandler] = None
        self._shutdown_requested = False
        self._observer: Optional[Observer] = None  # type: ignore[valid-type]
        self._text_logger: Optional[TextLogger] = None
        self._metrics_logger: Optional[CSVMetricsLogger] = None

    def _parse_args(self, args: Optional[List[str]]) -> argparse.Namespace:
        """Parse command-line arguments.

        Args:
            args: Arguments to parse (defaults to sys.argv).

        Returns:
            Parsed argument namespace.
        """
        if args is None:
            args = sys.argv[1:]
        if args and args[0] == "watch":
            args = args[1:]
        parser = argparse.ArgumentParser(
            prog="plt-optimizer watch",
            description="Watch a directory for PLT files and optimize them automatically.",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
Examples:
  # Watch current directory, output to ./optimized, logs to ./logs
  python -m plt_optimizer.cli.watch --watch-dir .

  # With explicit directories and fast mode
  python -m plt_optimizer.cli.watch --watch-dir /input/plt \\
                                    --output-dir /output/plt \\
                                    --log-dir /var/log/plt-optimizer \\
                                    --fast-mode

  # With processed-dir to archive original files after optimization
  python -m plt_optimizer.cli.watch --watch-dir /input/plt \\
                                    --output-dir /output/plt \\
                                    --processed-dir /archive/plt

  # Run as module
  uv run plt-optimizer watch --watch-dir /input
            """,
        )

        parser.add_argument(
            "--watch-dir",
            type=Path,
            required=True,
            help="Directory to watch for new/modified PLT files.",
        )
        parser.add_argument(
            "--output-dir",
            type=Path,
            default=Path("./optimized"),
            help="Directory where optimized PLT files are saved (default: ./optimized).",
        )
        parser.add_argument(
            "--log-dir",
            type=Path,
            default=Path("./logs"),
            help="Directory for log files (default: ./logs).",
        )
        parser.add_argument(
            "--processed-dir",
            type=Path,
            default=None,
            help=(
                "Directory to move processed PLT files to after optimization. "
                "If not specified, original files remain in the watch directory."
            ),
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
            "--debug-save-files",
            action="store_true",
            help=(
                "Save before/after PLT files and comparison plots to the log directory. "
                "Only effective when --log-dir is specified. Creates a 'debug' subdirectory "
                "containing original.plt, optimized.plt, and comparison.png for each job."
            ),
        )
        parser.add_argument(
            "--debounce-seconds",
            type=float,
            default=2.0,
            help=(
                "Quiet period (seconds) the watcher waits after the last "
                "modification to a file before processing it. Prevents "
                "reading partially-written files (default: 2.0)."
            ),
        )

        return parser.parse_args(args)

    def _validate_path_can_be_created(self, path: pathlib.Path) -> None:
        """Validate that a path's parent directories exist and are writable.

        Args:
            path: The path to validate.

        Raises:
            ValueError: If the path cannot be created due to missing or unwritable
                        parent directories.
        """
        import platform

        if path.exists():
            return

        # Check each parent directory
        for parent in path.parents:
            if parent == pathlib.Path("/"):
                raise ValueError(f"Cannot create path '{path}': root directory '/' is not writable")
            if parent.exists():
                # Parent exists, check if we can write to it.
                # Catch OSError (not just PermissionError) to handle:
                #   - PermissionError (errno 13 EACCES) on Linux when the
                #     parent is owned by another user/group
                #   - OSError (errno 30 EROFS, "Read-only file system") on
                #     macOS when the parent resides on a read-only volume
                #     (e.g. /usr/share on the sealed system volume)
                #   - Any other OS-level write failure surfaced by touch()
                try:
                    test_file = parent / f".plt_opt_write_test_{os.getpid()}"
                    test_file.touch()
                    test_file.unlink()
                except OSError as e:
                    raise ValueError(
                        f"Cannot create path '{path}': "
                        f"parent directory '{parent}' is not writable: {e}"
                    ) from e
                # Parent is writable, so we can create children
                return
            # Parent doesn't exist, continue checking grandparents

        # If we get here, no parents exist up to root - check if root itself exists
        root = path.anchor
        if root and not pathlib.Path(root).exists():
            raise ValueError(
                f"Cannot create path '{path}': "
                f"root directory '{root}' does not exist on this system "
                f"({platform.system()})"
            )

    def _setup_logging(self) -> None:
        """Initialize logging system with configured log directory."""
        text_log_file = self._args.log_dir / "optimizer.log"
        csv_metrics_file = self._args.log_dir / "job_metrics.csv"

        # Validate paths before attempting creation
        try:
            self._validate_path_can_be_created(self._args.output_dir)
            self._validate_path_can_be_created(self._args.log_dir)
        except ValueError as e:
            raise OSError(str(e)) from e

        # Ensure directories exist
        try:
            self._args.output_dir.mkdir(parents=True, exist_ok=True)
            self._args.log_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError as e:
            raise OSError(
                f"Permission denied creating directory '{self._args.output_dir}' or "
                f"'{self._args.log_dir}': {e}. This may indicate a path issue - "
                f"ensure the parent directories exist and are writable."
            ) from e

        from plt_optimizer.utils.logging import setup_logging

        self._text_logger, self._metrics_logger = setup_logging(
            text_log_file=text_log_file,
            csv_metrics_file=csv_metrics_file,
        )

    def _validate_directories(self) -> bool:
        """Validate that required directories exist and are accessible.

        Returns:
            True if all directories are valid.
        """
        # Assert loggers are initialized (called after run() sets up logging)
        assert self._text_logger is not None, "Text logger should be initialized"
        assert self._metrics_logger is not None, "Metrics logger should be initialized"

        # Check watch directory
        if not self._args.watch_dir.exists():
            self._text_logger.error(f"Watch directory does not exist: {self._args.watch_dir}")
            return False

        if not self._args.watch_dir.is_dir():
            self._text_logger.error(f"Watch path is not a directory: {self._args.watch_dir}")
            return False

        # Try to list directory contents (check read permissions)
        try:
            list(self._args.watch_dir.iterdir())
        except PermissionError:
            self._text_logger.error(
                f"No permission to read watch directory: {self._args.watch_dir}"
            )
            return False

        # Check/create output directory
        if not self._args.output_dir.exists():
            try:
                self._args.output_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self._text_logger.error(
                    f"Cannot create output directory {self._args.output_dir}: {e}"
                )
                return False

        # Check/create log directory
        if not self._args.log_dir.exists():
            try:
                self._args.log_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self._text_logger.error(f"Cannot create log directory {self._args.log_dir}: {e}")
                return False

        # Check/create processed directory if specified
        if self._args.processed_dir is not None and not self._args.processed_dir.exists():
            try:
                self._args.processed_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self._text_logger.error(
                    f"Cannot create processed directory {self._args.processed_dir}: {e}"
                )
                return False

        return True

    def _process_existing_files(self) -> int:
        """Enqueue any existing PLT files in the watch directory for processing.

        Files are not processed inline; they are handed off to a background
        debounce thread which waits for them to stabilise. The handler is
        kept alive after this method returns so the background thread can
        finish processing the enqueued files. ``run()`` is responsible for
        stopping it during shutdown.

        Returns:
            Number of files enqueued for processing.
        """
        # Assert loggers are initialized (called after run() sets up logging)
        assert self._text_logger is not None, "Text logger should be initialized"
        assert self._metrics_logger is not None, "Metrics logger should be initialized"

        self._text_logger.info(f"Scanning for existing PLT files in {self._args.watch_dir}")

        self._existing_handler = PLTFileHandler(
            watch_dir=self._args.watch_dir,
            output_dir=self._args.output_dir,
            text_logger=self._text_logger,
            metrics_logger=self._metrics_logger,
            fast_mode=self._args.fast_mode,
            processed_dir=self._args.processed_dir,
            debug_save_files=(self._args.debug_save_files and self._log_dir_explicitly_set),
            log_dir=self._args.log_dir if self._args.debug_save_files else None,
            debounce_seconds=self._args.debounce_seconds,
        )
        self._existing_handler.start()

        count = 0
        try:
            for path in self._args.watch_dir.iterdir():
                if self._existing_handler._is_plt_file(path):
                    try:
                        if self._existing_handler._should_process(path):
                            self._existing_handler._enqueue_file(path)
                            count += 1
                    except Exception as e:
                        self._text_logger.error(f"Error enqueuing {path}: {e}")
        except Exception:
            # If iterdir itself fails, stop the handler we just started so
            # we don't leak a thread; the caller will see the exception.
            self._existing_handler.stop()
            self._existing_handler = None
            raise
        return count

    def _signal_handler(self, signum: int, frame: object) -> None:
        """Handle shutdown signals gracefully.

        Args:
            signum: Signal number received.
            frame: Current stack frame (unused, required by signal handler signature).
        """
        sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        if self._text_logger is not None:
            self._text_logger.info(f"Received {sig_name}, initiating graceful shutdown...")
        self._shutdown_requested = True

    def run(self) -> int:
        """Run the watch daemon.

        Returns:
            Exit code (0 for success).
        """
        # Set up logging
        self._setup_logging()
        # Assert loggers are initialized (mypy type narrowing)
        assert self._text_logger is not None, "Text logger should be initialized"
        assert self._metrics_logger is not None, "Metrics logger should be initialized"

        self._text_logger.info("=" * 60)
        self._text_logger.info("PLT-Optimizer Watch Daemon")
        self._text_logger.info(f"Watch directory: {self._args.watch_dir}")
        self._text_logger.info(f"Output directory: {self._args.output_dir}")
        self._text_logger.info(f"Log directory: {self._args.log_dir}")
        if self._args.processed_dir is not None:
            self._text_logger.info(f"Processed directory: {self._args.processed_dir}")
        self._text_logger.info(
            f"Strategy: {'NearestNeighbor2Opt (Fast Mode)' if self._args.fast_mode else 'ParallelEnsemble'}"
        )
        self._text_logger.info("=" * 60)

        # Validate directories
        if not self._validate_directories():
            return 1

        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # Process existing files first
        enqueued_count = self._process_existing_files()
        if enqueued_count > 0:
            self._text_logger.info(f"Enqueued {enqueued_count} existing file(s) for processing")

        # Start watching for new files
        event_handler = PLTFileHandler(
            watch_dir=self._args.watch_dir,
            output_dir=self._args.output_dir,
            text_logger=self._text_logger,
            metrics_logger=self._metrics_logger,
            fast_mode=self._args.fast_mode,
            processed_dir=self._args.processed_dir,
            debug_save_files=(self._args.debug_save_files and self._log_dir_explicitly_set),
            log_dir=self._args.log_dir if self._args.debug_save_files else None,
            debounce_seconds=self._args.debounce_seconds,
        )
        event_handler.start()

        self._observer = Observer()
        self._observer.schedule(event_handler, str(self._args.watch_dir), recursive=False)  # type: ignore[no-untyped-call]
        self._observer.start()  # type: ignore[no-untyped-call]

        self._text_logger.info(f"Watching for PLT files in {self._args.watch_dir}")
        self._text_logger.info("Press Ctrl+C to stop...")

        try:
            while not self._shutdown_requested:
                # Check every second (Observer is running in background thread)
                signal.pause() if hasattr(signal, "pause") else time.sleep(1)
        except KeyboardInterrupt:
            self._text_logger.info("Keyboard interrupt received")
        finally:
            if self._observer is not None:  # pragma: no branch
                self._observer.stop()  # type: ignore[no-untyped-call]
                self._observer.join(timeout=5.0)
            event_handler.stop()
            if self._existing_handler is not None:
                self._existing_handler.stop()
                self._existing_handler = None

        self._text_logger.info("Watch daemon stopped.")
        return 0


def main(args: Optional[List[str]] = None) -> int:
    """Entry point for the watch command.

    Args:
        args: Command-line arguments (defaults to sys.argv).

    Returns:
        Exit code.
    """
    command = WatchCommand(args)
    return command.run()


if __name__ == "__main__":
    sys.exit(main())
