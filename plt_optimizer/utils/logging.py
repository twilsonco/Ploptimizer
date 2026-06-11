"""Dual logging system for PLT-Optimizer.

This module provides two independent logging mechanisms:
1. TextLogger: Standard Python logging to console and file (logs/optimizer.log)
2. CSVMetricsLogger: Structured metrics tracking in CSV format (logs/job_metrics.csv)

Both logs are written concurrently during optimization operations.
"""

from __future__ import annotations

import csv
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Protocol


# Log directory constant
LOG_DIR = Path("logs")
TEXT_LOG_FILE = LOG_DIR / "optimizer.log"
METRICS_LOG_FILE = LOG_DIR / "job_metrics.csv"


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
    ) -> None:
        """Log a completed optimization job to the CSV metrics file.

        Args:
            job_id: Unique identifier for this job.
            original_file: Path to input PLT file.
            optimized_file: Path to output PLT file (None if failed/skipped).
            original_distance: Total path distance before optimization.
            optimized_distance: Total path distance after optimization.
            status: Job completion status ('success', 'failed', 'skipped').
        """
        # Calculate percent improvement
        if original_distance > 0:
            pct_improvement = (
                (original_distance - optimized_distance) / original_distance * 100
            )
        else:
            pct_improvement = 0.0

        row = [
            datetime.now().isoformat(),
            job_id,
            str(original_file),
            str(optimized_file) if optimized_file else "",
            f"{original_distance:.3f}",
            f"{optimized_distance:.3f}",
            f"{pct_improvement:.2f}%",
            status,
        ]

        with open(self.log_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(row)


# Module-level convenience functions using default loggers
_text_logger: Optional[TextLogger] = None
_csv_logger: Optional[CSVMetricsLogger] = None


def setup_logging(
    level: int = logging.INFO,
    text_log_file: Optional[Path] = None,
    csv_metrics_file: Optional[Path] = None,
) -> tuple[TextLogger, CSVMetricsLogger]:
    """Set up the dual logging system.

    This function initializes both the text logger and CSV metrics logger
    with optional custom file paths.

    Args:
        level: Minimum logging level for text logger.
        text_log_file: Custom path for optimizer.log.
        csv_metrics_file: Custom path for job_metrics.csv.

    Returns:
        A tuple of (TextLogger, CSVMetricsLogger) instances.
    """
    global _text_logger, _csv_logger

    if _text_logger is None:
        _text_logger = TextLogger(level=level, log_file=text_log_file)
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