"""Tests for plt_optimizer.utils.logging module.

These tests target specific lines not covered by existing tests:
- TextLogger duplicate handler avoidance (line 53)
- CSVMetricsLogger._ensure_header() lines 87, 91
- CSVMetricsLogger.log_job() CSV writing lines 137-140
- setup_logging() module-level convenience functions lines 162-182
- get_text_logger() / get_metrics_logger() initialization branches lines 210->212, 212->215
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

from plt_optimizer.utils.logging import (
    LOG_CONSOLE_ENV_VAR,
    LOG_FILE_ENV_VAR,
    LOG_LEVEL_ENV_VAR,
    TEXT_LOG_FILE,
    CSVMetricsLogger,
    TextLogger,
    get_metrics_logger,
    get_text_logger,
    publish_log_policy,
    resolve_console_stream,
    resolve_log_file,
    resolve_log_level,
    setup_child_logging,
    setup_logging,
)


def _spawned_worker_probe(stdout_capture: str) -> None:
    """Build a logger the way a spawned worker does and log at two levels.

    This mirrors what the optimizer's ensemble worker does: a fresh
    interpreter that lazily creates its logger through
    :func:`setup_logging`, which reads the policy out of the environment.

    Args:
        stdout_capture: Path the child's stdout is redirected to (the
            console handler writes to stderr under the published policy,
            so this file stays empty -- which is the point).
    """
    from plt_optimizer.utils.logging import setup_logging as _setup_logging

    with open(stdout_capture, "w", encoding="utf-8") as redirected:
        original_stdout = sys.stdout
        sys.stdout = redirected
        try:
            text_logger, _ = _setup_logging()
            text_logger.info("WORKER INFO LINE")
            text_logger.warning("WORKER WARNING LINE")
        finally:
            sys.stdout = original_stdout


class TestTextLoggerDuplicateHandlers:
    """Tests for TextLogger duplicate handler avoidance (line 53)."""

    def test_no_duplicate_handlers(self) -> None:
        """Test that creating TextLogger twice doesn't add duplicate handlers."""
        logger1 = TextLogger(name="test_no_dups", level=logging.DEBUG)
        initial_handler_count = len(logger1.logger.handlers)

        # Create another TextLogger with same name
        logger2 = TextLogger(name="test_no_dups", level=logging.DEBUG)

        # Should not have added more handlers
        assert len(logger2.logger.handlers) == initial_handler_count

    def test_text_logger_default_name(self) -> None:
        """Test TextLogger with default name."""
        logger = TextLogger()
        assert logger.logger.name == "plt_optimizer"

    def test_text_logger_all_levels(self) -> None:
        """Test all logging levels work."""
        logger = TextLogger(level=logging.DEBUG)

        logger.debug("debug message")
        logger.info("info message")
        logger.warning("warning message")
        logger.error("error message")
        logger.critical("critical message")


class TestCSVMetricsLoggerEnsureHeader:
    """Tests for CSVMetricsLogger._ensure_header() (lines 87, 91)."""

    def test_ensure_header_creates_file(self, tmp_path: Path) -> None:
        """Test that _ensure_header creates the CSV file if it doesn't exist."""
        log_file = tmp_path / "test_metrics.csv"
        logger = CSVMetricsLogger(log_file=log_file)

        # The file should have been created during __init__
        assert log_file.exists()

    def test_ensure_header_writes_correct_columns(self, tmp_path: Path) -> None:
        """Test that _ensure_header writes the correct column headers."""
        log_file = tmp_path / "test_columns.csv"
        logger = CSVMetricsLogger(log_file=log_file)

        with open(log_file, encoding="utf-8") as f:
            first_line = f.readline().strip()

        expected_columns = [
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
        assert first_line == ",".join(expected_columns)

    def test_ensure_header_skips_existing_file(self, tmp_path: Path) -> None:
        """Test that _ensure_header doesn't overwrite existing files."""
        log_file = tmp_path / "test_existing.csv"

        # Pre-create file with custom content
        log_file.write_text("custom_header", encoding="utf-8")

        logger = CSVMetricsLogger(log_file=log_file)

        # Content should be unchanged
        assert log_file.read_text(encoding="utf-8") == "custom_header"


class TestCSVMetricsLoggerLogJob:
    """Tests for CSVMetricsLogger.log_job() (lines 137-140)."""

    def test_log_job_success(self, tmp_path: Path) -> None:
        """Test logging a successful job writes correct CSV row."""
        log_file = tmp_path / "test_success.csv"
        logger = CSVMetricsLogger(log_file=log_file)

        original = Path("input.plt")
        optimized = Path("output.plt")
        logger.log_job(
            job_id="job-001",
            original_file=original,
            optimized_file=optimized,
            original_distance=100.0,
            optimized_distance=80.0,
            status="success",
        )

        with open(log_file, encoding="utf-8") as f:
            lines = f.readlines()

        # Should have header + 1 data row
        assert len(lines) == 2

    def test_log_job_with_zero_original_distance(self, tmp_path: Path) -> None:
        """Test logging a job where original distance is 0 (percent improvement = 0)."""
        log_file = tmp_path / "test_zero.csv"
        logger = CSVMetricsLogger(log_file=log_file)

        logger.log_job(
            job_id="job-zero",
            original_file=Path("input.plt"),
            optimized_file=None,
            original_distance=0.0,
            optimized_distance=0.0,
            status="skipped",
        )

        with open(log_file, encoding="utf-8") as f:
            lines = f.readlines()

        assert len(lines) == 2

    def test_log_job_failed_no_optimized_file(self, tmp_path: Path) -> None:
        """Test logging a failed job with no optimized file."""
        log_file = tmp_path / "test_failed.csv"
        logger = CSVMetricsLogger(log_file=log_file)

        logger.log_job(
            job_id="job-failed",
            original_file=Path("input.plt"),
            optimized_file=None,
            original_distance=50.0,
            optimized_distance=0.0,
            status="failed",
        )

        with open(log_file, encoding="utf-8") as f:
            lines = f.readlines()

        # optimized_file field should be empty string when None
        assert len(lines) == 2

    def test_log_job_appends_to_existing(self, tmp_path: Path) -> None:
        """Test that log_job appends rows to existing CSV file."""
        log_file = tmp_path / "test_append.csv"
        logger1 = CSVMetricsLogger(log_file=log_file)

        logger1.log_job(
            job_id="job-001",
            original_file=Path("input.plt"),
            optimized_file=Path("output1.plt"),
            original_distance=100.0,
            optimized_distance=50.0,
            status="success",
        )

        logger2 = CSVMetricsLogger(log_file=log_file)

        logger2.log_job(
            job_id="job-002",
            original_file=Path("input2.plt"),
            optimized_file=Path("output2.plt"),
            original_distance=200.0,
            optimized_distance=150.0,
            status="success",
        )

        with open(log_file, encoding="utf-8") as f:
            lines = f.readlines()

        # Should have header + 2 data rows
        assert len(lines) == 3


class TestSetupLogging:
    """Tests for setup_logging() module-level convenience function (lines 162-182)."""

    def test_setup_logging_returns_tuple(self) -> None:
        """Test setup_logging returns a tuple of (TextLogger, CSVMetricsLogger)."""
        import plt_optimizer.utils.logging as logging_module

        original_text = logging_module._text_logger
        original_csv = logging_module._csv_logger

        try:
            text_logger, csv_logger = setup_logging()

            assert isinstance(text_logger, TextLogger)
            assert isinstance(csv_logger, CSVMetricsLogger)
        finally:
            logging_module._text_logger = original_text
            logging_module._csv_logger = original_csv

    def test_setup_logging_custom_paths(self, tmp_path: Path) -> None:
        """Test setup_logging with custom file paths."""
        import plt_optimizer.utils.logging as logging_module

        original_text = logging_module._text_logger
        original_csv = logging_module._csv_logger

        try:
            text_log = tmp_path / "custom_text.log"
            csv_log = tmp_path / "custom_csv.csv"

            # Reset singletons to force reinitialization
            logging_module._text_logger = None
            logging_module._csv_logger = None

            text_logger, csv_logger = setup_logging(
                level=logging.DEBUG,
                text_log_file=text_log,
                csv_metrics_file=csv_log,
            )

            assert text_logger.logger.handlers
            assert csv_log.exists()
        finally:
            logging_module._text_logger = original_text
            logging_module._csv_logger = original_csv

    def test_setup_logging_respects_level(self) -> None:
        """Test setup_logging respects the specified log level."""
        import plt_optimizer.utils.logging as logging_module

        original_text = logging_module._text_logger
        original_csv = logging_module._csv_logger

        try:
            # Reset singletons to force reinitialization
            logging_module._text_logger = None
            logging_module._csv_logger = None

            text_logger, _ = setup_logging(level=logging.ERROR)

            # Should only log ERROR and above
            assert text_logger.logger.level == logging.ERROR
        finally:
            logging_module._text_logger = original_text
            logging_module._csv_logger = original_csv


class TestGetTextLogger:
    """Tests for get_text_logger() initialization branches (lines 210->212, 212->215)."""

    def test_get_text_logger_initializes_if_none(self) -> None:
        """Test get_text_logger creates TextLogger if _text_logger is None."""
        # Reset the module-level variable first
        import plt_optimizer.utils.logging as logging_module

        original_text = logging_module._text_logger
        logging_module._text_logger = None

        text_logger = get_text_logger()

        assert isinstance(text_logger, TextLogger)

        # Restore original state
        logging_module._text_logger = original_text


class TestGetMetricsLogger:
    """Tests for get_metrics_logger() initialization branches (lines 210->212, 212->215)."""

    def test_get_metrics_logger_initializes_if_none(self) -> None:
        """Test get_metrics_logger creates CSVMetricsLogger if _csv_logger is None."""
        import plt_optimizer.utils.logging as logging_module

        original_csv = logging_module._csv_logger
        logging_module._csv_logger = None

        csv_logger = get_metrics_logger()

        assert isinstance(csv_logger, CSVMetricsLogger)

        # Restore original state
        logging_module._csv_logger = original_csv


class TestModuleLevelLoggers:
    """Tests for module-level logger state management."""

    def test_setup_logging_does_not_override_initialized(self) -> None:
        """Test setup_logging doesn't reinitialize if already initialized."""
        import plt_optimizer.utils.logging as logging_module

        original_text = logging_module._text_logger
        original_csv = logging_module._csv_logger

        # Initialize loggers
        setup_logging()

        first_text = logging_module._text_logger
        first_csv = logging_module._csv_logger

        # Call setup_logging again - should return existing instances
        text2, csv2 = setup_logging(level=logging.DEBUG)

        assert text2 is first_text
        assert csv2 is first_csv

        # Restore original state
        logging_module._text_logger = original_text
        logging_module._csv_logger = original_csv


class TestResolveLogLevel:
    """Tests for resolve_log_level() level parsing and env fallback."""

    def test_none_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no value and no env var, the default level is returned."""
        monkeypatch.delenv(LOG_LEVEL_ENV_VAR, raising=False)
        assert resolve_log_level(None) == logging.INFO
        assert resolve_log_level(default=logging.WARNING) == logging.WARNING

    def test_int_passes_through(self) -> None:
        """A numeric level is returned unchanged."""
        assert resolve_log_level(logging.DEBUG) == logging.DEBUG
        assert resolve_log_level(5) == 5

    def test_level_name_case_insensitive(self) -> None:
        """Level names resolve regardless of casing or surrounding space."""
        assert resolve_log_level("WARNING") == logging.WARNING
        assert resolve_log_level("debug") == logging.DEBUG
        assert resolve_log_level("  Error ") == logging.ERROR

    def test_numeric_string(self) -> None:
        """A numeric string resolves to its int value."""
        assert resolve_log_level("20") == logging.INFO

    def test_env_var_used_when_value_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The env var supplies the level when no explicit value is given."""
        monkeypatch.setenv(LOG_LEVEL_ENV_VAR, "error")
        assert resolve_log_level(None) == logging.ERROR

    def test_explicit_value_beats_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An explicit value takes precedence over the env var."""
        monkeypatch.setenv(LOG_LEVEL_ENV_VAR, "error")
        assert resolve_log_level("debug") == logging.DEBUG

    def test_invalid_value_raises(self) -> None:
        """An unrecognized level name raises ValueError."""
        with pytest.raises(ValueError, match="Invalid log level"):
            resolve_log_level("nope")

    def test_invalid_env_var_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unrecognized env var value raises ValueError."""
        monkeypatch.setenv(LOG_LEVEL_ENV_VAR, "verbose")
        with pytest.raises(ValueError, match="Invalid log level"):
            resolve_log_level(None)


class TestSetupLoggingEnvLevel:
    """setup_logging() must honor LOG_LEVEL_ENV_VAR when no level is given."""

    def test_setup_logging_reads_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without an explicit level, the env var configures the logger."""
        import plt_optimizer.utils.logging as logging_module

        monkeypatch.setenv(LOG_LEVEL_ENV_VAR, "WARNING")
        monkeypatch.setattr(logging_module, "_text_logger", None)
        monkeypatch.setattr(logging_module, "_csv_logger", None)

        text_logger, _ = setup_logging()
        assert text_logger.logger.level == logging.WARNING

    def test_setup_logging_explicit_level_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An explicit level overrides the env var."""
        import plt_optimizer.utils.logging as logging_module

        monkeypatch.setenv(LOG_LEVEL_ENV_VAR, "WARNING")
        monkeypatch.setattr(logging_module, "_text_logger", None)
        monkeypatch.setattr(logging_module, "_csv_logger", None)

        text_logger, _ = setup_logging(level=logging.DEBUG)
        assert text_logger.logger.level == logging.DEBUG


class TestResolveConsoleStream:
    """Tests for resolve_console_stream() -- the console policy."""

    def test_explicit_stream_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An explicit stream beats the environment."""
        monkeypatch.setenv(LOG_CONSOLE_ENV_VAR, "stderr")
        sentinel = object()
        assert resolve_console_stream(sentinel) is sentinel

    def test_env_var_selects_stderr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The environment can redirect the console to stderr."""
        monkeypatch.setenv(LOG_CONSOLE_ENV_VAR, "stderr")
        assert resolve_console_stream() is sys.stderr

    def test_defaults_to_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without an explicit value or env var, stdout is used."""
        monkeypatch.delenv(LOG_CONSOLE_ENV_VAR, raising=False)
        assert resolve_console_stream() is sys.stdout


class TestResolveLogFile:
    """Tests for resolve_log_file() -- the log-file policy."""

    def test_explicit_file_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """An explicit path beats the environment."""
        monkeypatch.setenv(LOG_FILE_ENV_VAR, str(tmp_path / "from_env.log"))
        explicit = tmp_path / "explicit.log"
        assert resolve_log_file(explicit) == explicit

    def test_env_var_selects_the_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """The environment can redirect the log file."""
        target = tmp_path / "from_env.log"
        monkeypatch.setenv(LOG_FILE_ENV_VAR, str(target))
        assert resolve_log_file() == target

    def test_defaults_to_the_standard_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without an explicit value or env var, the default file is used."""
        monkeypatch.delenv(LOG_FILE_ENV_VAR, raising=False)
        assert resolve_log_file() == TEXT_LOG_FILE


class TestPublishLogPolicy:
    """Tests for publish_log_policy() -- the parent-to-child handoff."""

    def test_publishes_all_three_settings(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The level, console choice and file are all recorded."""
        monkeypatch.delenv(LOG_LEVEL_ENV_VAR, raising=False)
        monkeypatch.delenv(LOG_CONSOLE_ENV_VAR, raising=False)
        monkeypatch.delenv(LOG_FILE_ENV_VAR, raising=False)
        target = tmp_path / "generate.log"

        publish_log_policy(level=logging.WARNING, console_stream=sys.stderr, log_file=target)

        assert os.environ[LOG_LEVEL_ENV_VAR] == str(logging.WARNING)
        assert os.environ[LOG_CONSOLE_ENV_VAR] == "stderr"
        assert os.environ[LOG_FILE_ENV_VAR] == str(target)

    def test_stdout_console_is_published_as_stdout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stdout console is published as such."""
        monkeypatch.delenv(LOG_CONSOLE_ENV_VAR, raising=False)
        publish_log_policy(level=logging.INFO, console_stream=sys.stdout)
        assert os.environ[LOG_CONSOLE_ENV_VAR] == "stdout"

    def test_absent_console_leaves_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No console stream means the child keeps the stdout default."""
        monkeypatch.delenv(LOG_CONSOLE_ENV_VAR, raising=False)
        publish_log_policy(level=logging.INFO)
        assert os.environ[LOG_CONSOLE_ENV_VAR] == "stdout"


class TestSpawnedWorkerInheritsPolicy:
    """A spawned worker must inherit the parent's logging policy.

    The optimizer's ensemble runs its strategies in a spawned
    ProcessPoolExecutor pool. A worker starts with a clean interpreter and
    builds its own logger, so without the environment handoff it logs at
    the default INFO level to stdout -- flooding the caller's stdout even
    when the parent runs at WARNING with the console on stderr.
    """

    def test_worker_logger_honours_the_published_level(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A logger built from the published policy runs at that level."""
        import plt_optimizer.utils.logging as logging_module

        monkeypatch.delenv(LOG_LEVEL_ENV_VAR, raising=False)
        monkeypatch.delenv(LOG_CONSOLE_ENV_VAR, raising=False)
        monkeypatch.delenv(LOG_FILE_ENV_VAR, raising=False)
        monkeypatch.setattr(logging_module, "_text_logger", None)
        monkeypatch.setattr(logging_module, "_csv_logger", None)

        publish_log_policy(
            level=logging.WARNING,
            console_stream=sys.stderr,
            log_file=tmp_path / "child.log",
        )

        # A fresh process would build its logger here; the singleton reset
        # above makes this the equivalent of the child's first call.
        child_logger, _ = setup_logging()
        assert child_logger.logger.level == logging.WARNING
        assert child_logger.console_stream is sys.stderr
        assert child_logger.log_file == tmp_path / "child.log"

    def test_setup_child_logging_publishes_the_policy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """setup_child_logging() sets up AND publishes in one step."""
        import plt_optimizer.utils.logging as logging_module

        monkeypatch.delenv(LOG_LEVEL_ENV_VAR, raising=False)
        monkeypatch.delenv(LOG_CONSOLE_ENV_VAR, raising=False)
        monkeypatch.delenv(LOG_FILE_ENV_VAR, raising=False)
        monkeypatch.setattr(logging_module, "_text_logger", None)
        monkeypatch.setattr(logging_module, "_csv_logger", None)

        target = tmp_path / "pipeline.log"
        text_logger = setup_child_logging(
            level=logging.WARNING,
            console_stream=sys.stderr,
            log_file=target,
        )

        assert text_logger.logger.level == logging.WARNING
        assert os.environ[LOG_LEVEL_ENV_VAR] == str(logging.WARNING)
        assert os.environ[LOG_CONSOLE_ENV_VAR] == "stderr"
        assert os.environ[LOG_FILE_ENV_VAR] == str(target)


class TestTextLoggerPickling:
    """A TextLogger must survive being pickled into a subprocess.

    A strategy holds a TextLogger, and the optimizer's benchmark pickles
    the strategy into a worker process. The console stream object is not
    picklable (pytest's ``EncodedFile``, for instance), so only its name
    travels.
    """

    def test_pickles_with_a_non_picklable_stream(self, tmp_path: Path) -> None:
        """A logger holding an unpicklable stream still pickles."""
        import pickle

        logger = TextLogger(name="test_pickle", level=logging.INFO, log_file=tmp_path / "a.log")
        # A file-backed stream is not picklable.
        with open(tmp_path / "stream.txt", "w", encoding="utf-8") as handle:
            logger.console_stream = handle  # type: ignore[assignment]
            restored = pickle.loads(pickle.dumps(logger))

        assert restored.logger.name == "test_pickle"
        assert restored.log_file == tmp_path / "a.log"

    def test_restored_logger_keeps_the_level(self, tmp_path: Path) -> None:
        """The level survives the round trip."""
        import pickle

        logger = TextLogger(name="test_pickle_level", level=logging.WARNING, log_file=tmp_path / "b.log")
        restored = pickle.loads(pickle.dumps(logger))
        assert restored.logger.level == logging.WARNING

    def test_restored_logger_does_not_duplicate_handlers(self, tmp_path: Path) -> None:
        """Restoring a logger does not re-add the console handler."""
        import pickle

        logger = TextLogger(name="test_pickle_handlers", level=logging.INFO, log_file=tmp_path / "c.log")
        handler_count = len(logger.logger.handlers)
        restored = pickle.loads(pickle.dumps(logger))
        assert len(restored.logger.handlers) == handler_count


class TestSpawnedProcessEndToEnd:
    """A real spawned process must honour the published policy.

    The unit tests above pin the mechanism; this pins the outcome. The
    optimizer's ensemble pool spawns worker processes, and a worker that
    ignores the policy floods the caller's stdout with INFO chatter.
    """

    def test_spawned_process_does_not_emit_info_to_stdout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A spawned process at WARNING writes no INFO to stdout."""
        import subprocess

        monkeypatch.delenv(LOG_LEVEL_ENV_VAR, raising=False)
        monkeypatch.delenv(LOG_CONSOLE_ENV_VAR, raising=False)
        monkeypatch.delenv(LOG_FILE_ENV_VAR, raising=False)

        child_log = tmp_path / "child.log"
        publish_log_policy(
            level=logging.WARNING,
            console_stream=sys.stderr,
            log_file=child_log,
        )

        child_script = (
            "import sys; sys.path.insert(0, {repo!r}); "
            "import tests.test_logging as t; "
            "t._spawned_worker_probe({capture!r})"
        ).format(repo=str(Path.cwd()), capture=str(tmp_path / "child_stdout.txt"))

        result = subprocess.run(
            [sys.executable, "-c", child_script],
            capture_output=True,
            text=True,
            check=False,
            env=dict(os.environ),
        )

        assert result.returncode == 0, result.stderr
        captured_stdout = (tmp_path / "child_stdout.txt").read_text(encoding="utf-8")
        # The console is on stderr, so stdout stays completely empty --
        # neither the INFO line nor the WARNING line reaches it.
        assert "WORKER INFO LINE" not in captured_stdout
        assert "WORKER WARNING LINE" not in captured_stdout
        # The WARNING still surfaces, on stderr.
        assert "WORKER WARNING LINE" in result.stderr

    def test_spawned_process_writes_to_the_published_log_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A spawned process appends to the published log file."""
        import subprocess

        monkeypatch.delenv(LOG_LEVEL_ENV_VAR, raising=False)
        monkeypatch.delenv(LOG_CONSOLE_ENV_VAR, raising=False)
        monkeypatch.delenv(LOG_FILE_ENV_VAR, raising=False)

        child_log = tmp_path / "child.log"
        publish_log_policy(
            level=logging.WARNING,
            console_stream=sys.stderr,
            log_file=child_log,
        )

        child_script = (
            "import sys; sys.path.insert(0, {repo!r}); "
            "import tests.test_logging as t; "
            "t._spawned_worker_probe({capture!r})"
        ).format(repo=str(Path.cwd()), capture=str(tmp_path / "child_stdout.txt"))

        result = subprocess.run(
            [sys.executable, "-c", child_script],
            capture_output=True,
            text=True,
            check=False,
            env=dict(os.environ),
        )

        assert result.returncode == 0, result.stderr
        log_text = child_log.read_text(encoding="utf-8")
        assert "WORKER WARNING LINE" in log_text
        assert "WORKER INFO LINE" not in log_text
