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
from typing import Any, Dict, Optional, Tuple, Union

from plt_optimizer.core.units import units_to_inches

# Log directory constant
LOG_DIR = Path("logs")
TEXT_LOG_FILE = LOG_DIR / "optimizer.log"
METRICS_LOG_FILE = LOG_DIR / "job_metrics.csv"

# Environment variable consulted by :func:`setup_logging` when no explicit
# level is given. CLI entry points set it so spawned subprocesses (which
# inherit ``os.environ`` under the ``spawn`` start method) resolve the same
# verbosity without threading the value through every worker signature.
LOG_LEVEL_ENV_VAR: str = "PLT_LOG_LEVEL"

# Environment variable consulted by :func:`resolve_console_stream` when no
# explicit stream is given: ``"stdout"`` or ``"stderr"``. It propagates a
# CLI's console policy to the processes it spawns, so a worker does not
# write to stdout when its parent writes to stderr.
LOG_CONSOLE_ENV_VAR: str = "PLT_LOG_CONSOLE"

# Environment variable consulted by :func:`resolve_log_file` when no explicit
# file is given: the path of the text log file. It propagates a CLI's log
# file to the processes it spawns, so a worker appends to the pipeline's
# own log file instead of creating a second, stray one.
LOG_FILE_ENV_VAR: str = "PLT_LOG_FILE"

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


def console_stream_name(stream: Optional[Any] = None) -> str:
    """Name the console stream the logging policy should use.

    The policy travels to spawned workers as a NAME, not as a stream
    object: a :class:`logging.Logger` is pickled into the optimizer's
    subprocesses (a strategy holds one), and a stream object (pytest's
    ``EncodedFile``, for instance) is not picklable.

    Args:
        stream: The stream to name. ``None`` means "unset".

    Returns:
        ``"stdout"``, ``"stderr"`` or ``""`` (unset).
    """
    if stream is None:
        return ""
    if stream is sys.stdout:
        return "stdout"
    if stream is sys.stderr:
        return "stderr"
    return "stdout"


def resolve_console_stream(explicit: Optional[Any] = None) -> Any:
    """Resolve the stream the console handler writes to.

    A spawned worker process builds its own logger from scratch, so it
    cannot inherit the parent's handler configuration. The parent's policy
    is propagated through the environment instead
    (:data:`LOG_CONSOLE_ENV_VAR`), which the ``spawn`` start method
    copies into the child.

    Args:
        explicit: An explicit stream. When given, it wins.

    Returns:
        The stream to write to: ``sys.stdout`` by default, ``sys.stderr``
        when the environment says so.
    """
    if explicit is not None:
        return explicit
    if os.environ.get(LOG_CONSOLE_ENV_VAR, "").strip().lower() == "stderr":
        return sys.stderr
    return sys.stdout


def resolve_log_file(explicit: Optional[Path] = None) -> Path:
    """Resolve the text log file path.

    Mirrors :func:`resolve_console_stream` for the file handler: a spawned
    worker appends to the pipeline's own log file rather than creating a
    second, stray one at the default location.

    Args:
        explicit: An explicit log file path. When given, it wins.

    Returns:
        The log file path: ``explicit`` when given, else the path in
        :data:`LOG_FILE_ENV_VAR`, else :data:`TEXT_LOG_FILE`.
    """
    if explicit is not None:
        return explicit
    env_value = os.environ.get(LOG_FILE_ENV_VAR, "").strip()
    if env_value:
        return Path(env_value)
    return TEXT_LOG_FILE


def publish_log_policy(
    *,
    level: int,
    console_stream: Optional[Any] = None,
    log_file: Optional[Path] = None,
) -> None:
    """Publish the logging policy to the processes this one spawns.

    The optimizer's ensemble runs its strategies in a
    :class:`~concurrent.futures.ProcessPoolExecutor` pool. A spawned
    worker starts with a clean interpreter: it builds its own logger via
    :func:`setup_logging` and therefore knows nothing about the parent's
    handler configuration. Without this, a worker logs at the default
    INFO level to stdout, even when its parent runs at WARNING with the
    console on stderr -- the worker's chatter lands on stdout anyway and
    floods the caller's output.

    Recording the policy in the environment fixes that: the ``spawn``
    start method copies the parent's environment into the child, and
    :func:`setup_logging` reads it back. Children therefore inherit the
    parent's verbosity, console stream and log file.

    Args:
        level: The numeric level the children should run at.
        console_stream: The stream the children's console handler should
            write to. ``None`` leaves the default (stdout).
        log_file: The file the children's file handler should append to.
            ``None`` leaves the default.
    """
    os.environ[LOG_LEVEL_ENV_VAR] = str(level)
    stream_name = console_stream_name(console_stream) or "stdout"
    os.environ[LOG_CONSOLE_ENV_VAR] = stream_name
    if log_file is not None:
        # Absolute: the child inherits the cwd, but an absolute path keeps
        # the file unambiguous even if it does not.
        os.environ[LOG_FILE_ENV_VAR] = str(Path(log_file).absolute())


class TextLogger:
    """Text-based logger using Python's standard logging module.

    Provides hierarchical logging with DEBUG, INFO, WARNING, ERROR, CRITICAL levels.
    Outputs to both console and logs/optimizer.log.

    Attributes:
        logger: The underlying Python logger instance.
        log_file: The resolved path of the text log file.
        console_stream: The resolved stream of the console handler.
    """

    def __init__(
        self,
        name: str = "plt_optimizer",
        level: int = logging.INFO,
        log_file: Optional[Path] = None,
        console_stream: Optional[Any] = None,
    ) -> None:
        """Initialize the text logger.

        Args:
            name: Logger name (typically module path).
            level: Minimum logging level.
            log_file: Path to log file. Defaults to
                :func:`resolve_log_file`'s answer.
            console_stream: Stream the console handler writes to. Defaults to
                :func:`resolve_console_stream`'s answer.
        """
        self.logger = logging.getLogger(name)
        self.logger.setLevel(level)
        self.log_file = resolve_log_file(log_file)
        self.console_stream = resolve_console_stream(console_stream)

        # Avoid duplicate handlers
        if not self.logger.handlers:
            formatter = logging.Formatter(
                fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )

            # Console handler
            console_handler = logging.StreamHandler(self.console_stream)
            console_handler.setLevel(level)
            console_handler.setFormatter(formatter)
            self.logger.addHandler(console_handler)

            # File handler
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(self.log_file, encoding="utf-8")
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)

    def __getstate__(self) -> Dict[str, Any]:
        """Return the picklable state of the logger.

        A strategy holds a :class:`TextLogger`, and the optimizer's
        benchmark pickles it into a subprocess. The stream object is not
        picklable (pytest's ``EncodedFile``, for instance), so only its
        NAME travels; the unpickled logger is rebuilt against the
        process's own stdout/stderr.

        Returns:
            The picklable state dict.
        """
        return {
            "logger_name": self.logger.name,
            "level": self.logger.level,
            "log_file": self.log_file,
            "console_stream_name": console_stream_name(self.console_stream),
        }

    def __setstate__(self, state: Dict[str, Any]) -> None:
        """Restore the logger from its picklable state.

        The restored logger is a plain handle onto the child's own
        logging setup: the handlers are NOT re-added here (re-adding a
        stdout handler would duplicate the child's output), and the level
        is only applied when nothing else has configured it.

        Args:
            state: The state dict produced by :meth:`__getstate__`.
        """
        stream_name = state.get("console_stream_name", "")
        stream: Any = sys.stderr if stream_name == "stderr" else sys.stdout
        self.logger = logging.getLogger(state.get("logger_name", "plt_optimizer"))
        if self.logger.level == logging.NOTSET:
            self.logger.setLevel(state.get("level", logging.INFO))
        self.log_file = Path(state.get("log_file", TEXT_LOG_FILE))
        self.console_stream = stream

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


def _set_handler_stream(handler: logging.StreamHandler[Any], stream: Any) -> None:
    """Point a stream handler at a new stream, tolerating a stale one.

    :meth:`logging.StreamHandler.setStream` flushes the handler's current
    stream before switching. When that stream has already been closed (a
    test harness that closed its capture between runs), the flush raises
    ``ValueError``. The switch must still happen -- the stale stream is
    exactly what we are moving away from -- so the flush error is
    swallowed and the stream is assigned directly.

    Args:
        handler: The stream handler to retarget.
        stream: The stream the handler should write to.
    """
    if handler.stream is stream:
        return
    try:
        handler.setStream(stream)
    except ValueError:
        handler.stream = stream


def apply_log_levels(
    text_logger: TextLogger,
    *,
    logger_level: Optional[int] = None,
    console_level: Optional[int] = None,
    file_level: Optional[int] = None,
    console_stream: Optional[Any] = None,
) -> None:
    """Retune an already-built text logger in place.

    :func:`setup_logging` intentionally never re-initializes its singleton,
    so a logger built by an earlier call keeps the verbosity and the output
    streams it was created with. This helper adjusts the logger and its
    handlers directly, so a CLI can impose its own logging policy (a quiet
    console, a verbose file) regardless of what ran before it in the same
    process.

    The console handler is identified by being a
    :class:`logging.StreamHandler` that is not a
    :class:`logging.FileHandler`; the file handler is the
    :class:`logging.FileHandler`. Handlers of any other type are left
    untouched.

    Args:
        text_logger: The logger to retune.
        logger_level: Level for the logger itself. ``None`` keeps it.
        console_level: Level for the console handler. ``None`` keeps it.
        file_level: Level for the file handler. ``None`` keeps it.
        console_stream: Stream the console handler writes to. ``None``
            keeps the current stream. Use ``sys.stderr`` to keep the
            console output off stdout.
    """
    if logger_level is not None:
        text_logger.logger.setLevel(logger_level)
    for handler in text_logger.logger.handlers:
        if isinstance(handler, logging.FileHandler):
            if file_level is not None:
                handler.setLevel(file_level)
            continue
        if isinstance(handler, logging.StreamHandler):
            if console_level is not None:
                handler.setLevel(console_level)
            if console_stream is not None:
                _set_handler_stream(handler, console_stream)


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
            f"{units_to_inches(original_distance):.1f}",
            f"{units_to_inches(optimized_distance):.1f}",
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


def setup_child_logging(
    *,
    level: Optional[Union[int, str]] = None,
    console_stream: Optional[Any] = None,
    log_file: Optional[Path] = None,
) -> TextLogger:
    """Set up the logging system and publish the policy to child processes.

    This is the entry point for a CLI that spawns worker processes (the
    optimizer's ensemble pool). It does what :func:`setup_logging` does
    and additionally records the resolved policy in the environment, so
    the spawned children inherit the parent's verbosity, console stream
    and log file instead of falling back to the defaults (INFO, stdout,
    ``logs/optimizer.log``).

    Args:
        level: Minimum logging level. Accepts a numeric level or a level
            name (case-insensitive). When ``None``, the
            :data:`LOG_LEVEL_ENV_VAR` environment variable is consulted,
            and finally :data:`logging.INFO` is used.
        console_stream: Stream the console handler writes to. ``None``
            keeps the default (stdout).
        log_file: Custom path for the text log file. ``None`` keeps the
            default.

    Returns:
        The initialized :class:`TextLogger`.
    """
    resolved = resolve_log_level(level)
    text_logger, _metrics_logger = setup_logging(
        level=resolved,
        text_log_file=log_file,
    )
    publish_log_policy(
        level=resolved,
        console_stream=console_stream,
        log_file=log_file or text_logger.log_file,
    )
    return text_logger


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
