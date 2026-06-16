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
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Set

# Third-party imports
try:
    from watchdog.events import FileSystemEventHandler, FileSystemEvent
    from watchdog.observers import Observer
except ImportError as e:
    raise ImportError(
        "watchdog library is required for watch functionality. "
        "Install it with: uv add watchdog"
    ) from e

# Local imports
from plt_optimizer.core.chunker import Chunker, ChunkerConfig
from plt_optimizer.core.models import PLTDocument
from plt_optimizer.core.optimizer import (
    NearestNeighbor2OptStrategy,
    OptimizerEngine,
    ParallelEnsembleStrategy,
)
from plt_optimizer.core.parser import PLTParser
from plt_optimizer.core.profiler import Profiler
from plt_optimizer.core.reassembler import MetricsCalculator, Reassembler
from plt_optimizer.core.writer import PLTWriter
from plt_optimizer.utils.logging import CSVMetricsLogger, TextLogger


# File extensions to watch for
SUPPORTED_EXTENSIONS = {".plt", ".hpgl", ".PLT", ".HPGL"}


class PLTFileHandler(FileSystemEventHandler):
    """Handles file system events for PLT files in the watch directory.

    This handler processes new and modified PLT files, optimizing them and
    writing the results to the output directory.

    Attributes:
        watch_dir: Directory being watched for changes.
        output_dir: Directory where optimized files are saved.
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
    ) -> None:
        """Initialize the PLT file handler.

        Args:
            watch_dir: Directory to watch for new/modified files.
            output_dir: Directory where optimized files are saved.
            text_logger: Initialized text logger instance.
            metrics_logger: Initialized CSV metrics logger instance.
            fast_mode: If True, use NearestNeighbor2OptStrategy exclusively.
        """
        super().__init__()
        self._watch_dir = watch_dir
        self._output_dir = output_dir
        self._text_logger = text_logger
        self._metrics_logger = metrics_logger
        self._fast_mode = fast_mode
        self._parser = PLTParser()
        self._writer = PLTWriter()
        self._processed_files: Set[Path] = set()

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
        except (IOError, OSError):
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
            metrics_calc = MetricsCalculator()

            # Calculate original distance
            original_distance = metrics_calc.calculate_original_travel_distance(doc)

            # Profile and chunk
            profiler = Profiler()
            profile_result = profiler.profile(doc)

            chunker = Chunker(config=ChunkerConfig(threshold_multiplier=2.0))
            blocks = chunker.chunk(doc.stroke_paths, profile_result.baseline_extent)

            if not blocks:
                self._text_logger.warning(f"[{job_id}] No blocks generated from file")
                return False

            # Select strategy based on fast_mode
            if self._fast_mode:
                strategy = NearestNeighbor2OptStrategy()
                strategy_name = "NearestNeighbor + 2-Opt (Fast Mode)"
            else:
                strategy = ParallelEnsembleStrategy(baseline_distance=original_distance)
                strategy_name = "Parallel Ensemble"

            # Optimize
            optimizer = OptimizerEngine(strategy=strategy)
            optimization_result = optimizer.optimize(blocks)

            # Reassemble
            reassembler = Reassembler()
            optimized_doc = reassembler.reassemble(doc, blocks, optimization_result)

            # Calculate after distance
            optimized_distance = metrics_calc.calculate_optimized_travel_distance(
                optimization_result
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
            )

            return True

        except Exception as e:
            self._text_logger.error(f"[{job_id}] Failed: {e}")
            self._metrics_logger.log_job(
                job_id=job_id,
                original_file=input_path,
                optimized_file=None,
                original_distance=0.0,
                optimized_distance=0.0,
                status="failed",
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


class WatchCommand:
    """Command-line interface for the watch-directory daemon.

    This class handles argument parsing and orchestrates the file system
    watcher for automated PLT optimization.

    Attributes:
        args: Parsed command-line arguments.
        text_logger: Text logger instance.
        metrics_logger: CSV metrics logger instance.
        observer: Watchdog observer for file system events.
    """

    def __init__(self, args: Optional[list[str]] = None) -> None:
        """Initialize the watch command.

        Args:
            args: Command-line arguments (defaults to sys.argv).
        """
        self._args = self._parse_args(args)
        self._text_logger: Optional[TextLogger] = None
        self._metrics_logger: Optional[CSVMetricsLogger] = None
        self._observer: Optional[Observer] = None
        self._shutdown_requested = False

    def _parse_args(self, args: Optional[list[str]]) -> argparse.Namespace:
        """Parse command-line arguments.

        Args:
            args: Arguments to parse (defaults to sys.argv).

        Returns:
            Parsed argument namespace.
        """
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
            "--fast-mode",
            action="store_true",
            help=(
                "Use NearestNeighbor2OptStrategy exclusively for faster processing. "
                "If not specified, uses ParallelEnsembleStrategy which runs multiple "
                "strategies and selects the best result."
            ),
        )

        return parser.parse_args(args)

    def _setup_logging(self) -> None:
        """Initialize logging system with configured log directory."""
        text_log_file = self._args.log_dir / "optimizer.log"
        csv_metrics_file = self._args.log_dir / "job_metrics.csv"

        # Ensure directories exist
        self._args.output_dir.mkdir(parents=True, exist_ok=True)
        self._args.log_dir.mkdir(parents=True, exist_ok=True)

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
                self._text_logger.error(
                    f"Cannot create log directory {self._args.log_dir}: {e}"
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

        self._text_logger.info(
            f"Scanning for existing PLT files in {self._args.watch_dir}"
        )

        count = 0
        handler = PLTFileHandler(
            watch_dir=self._args.watch_dir,
            output_dir=self._args.output_dir,
            text_logger=self._text_logger,
            metrics_logger=self._metrics_logger,
            fast_mode=self._args.fast_mode,
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
        )

        self._observer = Observer()
        self._observer.schedule(event_handler, str(self._args.watch_dir), recursive=False)
        self._observer.start()

        self._text_logger.info(f"Watching for PLT files in {self._args.watch_dir}")
        self._text_logger.info("Press Ctrl+C to stop...")

        try:
            while not self._shutdown_requested:
                # Check every second (Observer is running in background thread)
                signal.pause() if hasattr(signal, "pause") else time.sleep(1)
        except KeyboardInterrupt:
            self._text_logger.info("Keyboard interrupt received")
        finally:
            if self._observer is not None:
                self._observer.stop()
                self._observer.join(timeout=5.0)

        self._text_logger.info("Watch daemon stopped.")
        return 0


def main(args: Optional[list[str]] = None) -> int:
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