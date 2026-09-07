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
from pathlib import Path

import pytest

from plt_optimizer.utils.logging import (
    LOG_LEVEL_ENV_VAR,
    CSVMetricsLogger,
    TextLogger,
    get_metrics_logger,
    get_text_logger,
    resolve_log_level,
    setup_logging,
)


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
