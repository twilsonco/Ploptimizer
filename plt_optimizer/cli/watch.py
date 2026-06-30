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
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

# Third-party imports
try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError as e:
    raise ImportError(
        "watchdog library is required for watch functionality. Install it with: uv add watchdog"
    ) from e

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
        processed_dir: Path | None = None,
        debug_save_files: bool = False,
        log_dir: Path | None = None,
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
        """Check if a file should be processed (debounce duplicate events).

        Args:
            path: File path to check.

        Returns:
            True if the file should be processed.
        """
        # Simple debouncing - track recently processed files
        if path in self._processed_files:
            return False

        # Check if file exists and is readable
        try:
            if not path.exists():
                return False
            # Wait briefly to ensure file is fully written
            time.sleep(0.1)
            # Try to open the file to verify it's accessible
            with open(path, "rb") as f:
                f.read(1)
            return True
        except OSError:
            return False

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
            self._writer.write_file(original_doc, orig_plt_path)

            # Save optimized PLT file
            opt_plt_path = debug_dir / f"{safe_job_id}_optimized.plt"
            self._writer.write_file(optimized_doc, opt_plt_path)

            # Calculate improvement percentage for titles
            improvement_pct = (
                ((original_distance - optimized_distance) / original_distance * 100)
                if original_distance > 0
                else 0.0
            )

            # Save plot for original document using plotter.py function
            orig_plot_path = debug_dir / f"{safe_job_id}_original.png"
            plot_plt_document(
                original_doc,
                output_path=orig_plot_path,
                title=f"Original - {original_distance:.3f} (Job: {job_id})",
            )

            # Save plot for optimized document using plotter.py function
            opt_plot_path = debug_dir / f"{safe_job_id}_optimized.png"
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

            # Generate output path
            output_path = self._output_dir / f"{input_path.stem}_optimized.plt"

            # Write optimized file
            self._writer.write_file(optimized_doc, output_path)

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

            # Fallback: copy unprocessed file to output directory when optimization fails
            try:
                fallback_output_path = self._output_dir / f"{input_path.stem}_unprocessed.plt"
                shutil.copy2(str(input_path), str(fallback_output_path))
                self._text_logger.warning(
                    f"[{job_id}] Optimization failed - copied unprocessed file to "
                    f"{fallback_output_path} for manual review"
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

        if self._should_process(path):
            self._mark_processed(path)
            self._process_file(path)

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

        if self._should_process(path):
            self._mark_processed(path)
            self._process_file(path)


def run_watcher_from_config(
    config: dict[str, Any],
    stop_event: threading.Event,
    on_success: Callable[[str, float], None] | None = None,
    on_error: Callable[[str, str], None] | None = None,
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
    )

    existing_count = 0
    for path in watch_dir.iterdir():
        if handler._is_plt_file(path):
            try:
                if handler._should_process(path):
                    handler._mark_processed(path)

                    # Wrap _process_file to capture success/error callbacks
                    def wrapped_process(input_path: Path) -> bool:  # pragma: no cover
                        result = handler._process_file(input_path)  # pragma: no cover
                        return result  # pragma: no cover

                    if handler._process_file(path):
                        existing_count += 1
            except Exception as e:
                text_logger.error(f"Error processing {path}: {e}")

    if existing_count > 0:
        text_logger.info(f"Processed {existing_count} existing file(s)")

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
    )

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

    text_logger.info("Watch daemon stopped.")
    return 0


class WatchCommand:
    """Command-line interface for the watch-directory daemon.

    This class handles argument parsing and orchestrates the file system
    watcher for automated PLT optimization via CLI arguments.

    For programmatic use (e.g., from tray application), use run_watcher_from_config()
    directly instead of this class.
    """

    def __init__(self, args: list[str] | None = None) -> None:
        """Initialize the watch command.

        Args:
            args: Command-line arguments (defaults to sys.argv).
        """
        # Check if --log-dir was explicitly provided before parsing
        self._log_dir_explicitly_set = (args is not None and "--log-dir" in args) or (
            args is None and "--log-dir" in sys.argv[1:]
        )
        self._args = self._parse_args(args)

    def _parse_args(self, args: list[str] | None) -> argparse.Namespace:
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
                # Parent exists, check if we can write to it
                try:
                    test_file = parent / f".plt_opt_write_test_{os.getpid()}"
                    test_file.touch()
                    test_file.unlink()
                except PermissionError as e:
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
        """Process any existing PLT files in the watch directory.

        Returns:
            Number of files successfully processed.
        """
        # Assert loggers are initialized (called after run() sets up logging)
        assert self._text_logger is not None, "Text logger should be initialized"
        assert self._metrics_logger is not None, "Metrics logger should be initialized"

        self._text_logger.info(f"Scanning for existing PLT files in {self._args.watch_dir}")

        count = 0
        handler = PLTFileHandler(
            watch_dir=self._args.watch_dir,
            output_dir=self._args.output_dir,
            text_logger=self._text_logger,
            metrics_logger=self._metrics_logger,
            fast_mode=self._args.fast_mode,
            processed_dir=self._args.processed_dir,
            debug_save_files=(self._args.debug_save_files and self._log_dir_explicitly_set),
            log_dir=self._args.log_dir if self._args.debug_save_files else None,
        )

        for path in self._args.watch_dir.iterdir():
            if handler._is_plt_file(path):
                try:
                    if handler._should_process(path):
                        handler._mark_processed(path)
                        if handler._process_file(path):
                            count += 1
                except Exception as e:
                    self._text_logger.error(f"Error processing {path}: {e}")

        return count

    def _signal_handler(self, signum: int, frame: object) -> None:
        """Handle shutdown signals gracefully.

        Args:
            signum: Signal number received.
            frame: Current stack frame (unused, required by signal handler signature).
        """
        sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
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
        processed_count = self._process_existing_files()
        if processed_count > 0:
            self._text_logger.info(f"Processed {processed_count} existing file(s)")

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
        )

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

        self._text_logger.info("Watch daemon stopped.")
        return 0


def main(args: list[str] | None = None) -> int:
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
