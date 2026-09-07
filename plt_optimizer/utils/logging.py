"""Dual logging system for PLT-Optimizer.

This module provides two independent logging mechanisms:
1. TextLogger: Standard Python logging to console and file (logs/optimizer.log)
2. CSVMetricsLogger: Structured metrics tracking in CSV format (logs/job_metrics.csv)

Both logs are written concurrently during optimization operations.
"""

from __future__ import annotations

import csv
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

# Log directory constant
LOG_DIR = Path("logs")
TEXT_LOG_FILE = LOG_DIR / "optimizer.log"
METRICS_LOG_FILE = LOG_DIR / "job_metrics.csv"

# Environment variable consulted by :func:`setup_logging` when no explicit
# level is given. CLI entry points set it so spawned subprocesses (which
# inherit ``os.environ`` under the ``spawn`` start method) resolve the same
# verbosity without threading the value through every worker signature.
LOG_LEVEL_ENV_VAR: str = "PLT_LOG_LEVEL"

# Recognized level names for :func:`resolve_log_level` (upper-case canonical).
_VALID_LOG_LEVEL_NAMES: Dict[str, int] = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}


def resolve_log_level(
    value: Optional[Union[int, str]] = None,
    default: int = logging.INFO,
) -> int:
    """Resolve a logging level from an explicit value or the environment.

    Resolution order:

    1. ``value`` when provided — an ``int`` passes through unchanged; a
       string is interpreted as a level name (case-insensitive, e.g.
       ``"debug"``/``"WARNING"``) or a numeric string such as ``"20"``.
    2. The :data:`LOG_LEVEL_ENV_VAR` environment variable (used to propagate
       a CLI choice into spawned subprocesses).
    3. ``default``.

    Args:
        value: Explicit level (int, level-name string, or ``None``).
        default: Level to use when neither ``value`` nor the env var is set.

    Returns:
        The resolved numeric logging level.

    Raises:
        ValueError: If ``value`` (or the env var) is not a recognized level.
    """
    if value is None:
        value = os.environ.get(LOG_LEVEL_ENV_VAR) or None
    if value is None:
        return default
    if isinstance(value, int):
        return value
    name = str(value).strip().upper()
    if name.isdigit():
        return int(name)
    if name in _VALID_LOG_LEVEL_NAMES:
        return _VALID_LOG_LEVEL_NAMES[name]
    raise ValueError(
        f"Invalid log level {value!r}; expected one of "
        f"{', '.join(_VALID_LOG_LEVEL_NAMES)} or a numeric level"
    )


class TextLogger:
    """Text-based logger using Python's standard logging module.

    Provides hierarchical logging with DEBUG, INFO, WARNING, ERROR, CRITICAL levels.
    Outputs to both console and logs/optimizer.log.

    Attributes:
        logger: The underlying Python logger instance.
    """

    def __init__(
        self,
        name: str = "plt_optimizer",
        level: int = logging.INFO,
        log_file: Optional[Path] = None,
    ) -> None:
        """Initialize the text logger.

        Args:
            name: Logger name (typically module path).
            level: Minimum logging level.
            log_file: Path to log file. Defaults to logs/optimizer.log.
        """
        self.logger = logging.getLogger(name)
        self.logger.setLevel(level)

        # Avoid duplicate handlers
        if not self.logger.handlers:
            formatter = logging.Formatter(
                fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )

            # Console handler
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(level)
            console_handler.setFormatter(formatter)
            self.logger.addHandler(console_handler)

            # File handler
            file_path = log_file or TEXT_LOG_FILE
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(file_path, encoding="utf-8")
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)

    def debug(self, message: str) -> None:
        """Log a DEBUG level message."""
        self.logger.debug(message)

    def info(self, message: str) -> None:
        """Log an INFO level message."""
        self.logger.info(message)

    def warning(self, message: str) -> None:
        """Log a WARNING level message."""
        self.logger.warning(message)

    def error(self, message: str) -> None:
        """Log an ERROR level message."""
        self.logger.error(message)

    def critical(self, message: str) -> None:
        """Log a CRITICAL level message."""
        self.logger.critical(message)


class CSVMetricsLogger:
    """CSV-based metrics logger for tracking optimization job statistics.

    Maintains a structured ledger at logs/job_metrics.csv with columns:
    - timestamp: ISO format timestamp of the operation
    - job_id: Unique identifier for the optimization job
    - original_file: Path to input PLT file
    - optimized_file: Path to output PLT file (empty if failed)
    - original_total_distance: Total path distance before optimization
    - optimized_total_distance: Total path distance after optimization
    - percent_improvement: Percentage reduction in total distance
    - status: Job completion status (success, failed, skipped)
    - method: Optimization strategy used (e.g., "NearestNeighbor + 2-Opt", "Parallel Ensemble")
    - notes: Performance information for the method(s) used

    Attributes:
        log_file: Path to the CSV metrics file.
    """

    METRICS_HEADER = [
        "timestamp",
        "job_id",
        "original_file",
        "optimized_file",
        "original_total_distance",
        "optimized_total_distance",
        "percent_improvement",
        "status",
        "method",
        "notes",
    ]

    def __init__(
        self,
        log_file: Optional[Path] = None,
    ) -> None:
        """Initialize the CSV metrics logger.

        Args:
            log_file: Path to CSV file. Defaults to logs/job_metrics.csv.
        """
        self.log_file = log_file or METRICS_LOG_FILE
        self._ensure_header()

    def _ensure_header(self) -> None:
        """Create the CSV file with headers if it doesn't exist."""
        if not self.log_file.exists():
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(self.METRICS_HEADER)

    def log_job(
        self,
        job_id: str,
        original_file: Path,
        optimized_file: Optional[Path],
        original_distance: float,
        optimized_distance: float,
        status: str,
        method: str = "",
        notes: str = "",
    ) -> None:
        """Log a completed optimization job to the CSV metrics file.

        Args:
            job_id: Unique identifier for this job.
            original_file: Path to input PLT file.
            optimized_file: Path to output PLT file (None if failed/skipped).
            original_distance: Total path distance before optimization.
            optimized_distance: Total path distance after optimization.
            status: Job completion status ('success', 'failed', 'skipped').
            method: Optimization strategy used (e.g., "NearestNeighbor + 2-Opt").
            notes: Performance information for the method(s) used. If fast-mode,
                includes performance info for the single method used. Otherwise
                may include summary of all methods evaluated.
        """
        # Calculate percent improvement
        if original_distance > 0:
            pct_improvement = (original_distance - optimized_distance) / original_distance * 100
        else:
            pct_improvement = 0.0

        row = [
            datetime.now().isoformat(),
            job_id,
            str(original_file.stem),
            str(optimized_file.stem) if optimized_file else "",
            f"{original_distance / 1000:.1f}",
            f"{optimized_distance / 1000:.1f}",
            f"{pct_improvement:.2f}%",
            status,
            method,
            notes,
        ]

        with open(self.log_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(row)


# Module-level convenience functions using default loggers
_text_logger: Optional[TextLogger] = None
_csv_logger: Optional[CSVMetricsLogger] = None


def setup_logging(
    level: Optional[Union[int, str]] = None,
    text_log_file: Optional[Path] = None,
    csv_metrics_file: Optional[Path] = None,
) -> Tuple[TextLogger, CSVMetricsLogger]:
    """Set up the dual logging system.

    This function initializes both the text logger and CSV metrics logger
    with optional custom file paths.

    Args:
        level: Minimum logging level for text logger. Accepts a numeric level
            or a level name (case-insensitive). When ``None``, the
            :data:`LOG_LEVEL_ENV_VAR` environment variable is consulted, and
            finally :data:`logging.INFO` is used.
        text_log_file: Custom path for optimizer.log.
        csv_metrics_file: Custom path for job_metrics.csv.

    Returns:
        A tuple of (TextLogger, CSVMetricsLogger) instances.
    """
    global _text_logger, _csv_logger

    if _text_logger is None:
        _text_logger = TextLogger(level=resolve_log_level(level), log_file=text_log_file)
    if _csv_logger is None:
        _csv_logger = CSVMetricsLogger(log_file=csv_metrics_file)

    return (_text_logger, _csv_logger)


def get_text_logger() -> TextLogger:
    """Get the default text logger instance.

    Returns:
        The initialized TextLogger.
    """
    global _text_logger
    if _text_logger is None:
        setup_logging()
    assert _text_logger is not None
    return _text_logger


def get_metrics_logger() -> CSVMetricsLogger:
    """Get the default metrics logger instance.

    Returns:
        The initialized CSVMetricsLogger.
    """
    global _csv_logger
    if _csv_logger is None:
        setup_logging()
    assert _csv_logger is not None
    return _csv_logger
