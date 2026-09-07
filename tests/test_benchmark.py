"""Tests for plt_optimizer.cli.benchmark streaming / parallelization helpers.

These tests cover the new building blocks introduced when benchmark.py was
refactored to stream CSV writes incrementally and parallelize work via
``ProcessPoolExecutor``. The legacy ``process_file`` / ``build_ensemble_rows``
behavior is exercised indirectly through ``process_file`` with the logger
arguments set to ``None``.
"""

from __future__ import annotations

import csv
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

from plt_optimizer.cli.benchmark import (
    _FILE_LEVEL_SENTINEL,
    _NO_WINNER_SENTINEL,
    _TIMING_STAT_COLUMNS,
    _WINNER_JOB_TIMING_COLUMNS,
    CSV_COLUMNS,
    FileResult,
    ReportWriter,
    _build_csv_columns,
    _combined_scores,
    _compute_timing_stats,
    _empty_row,
    _group_eligible_rows_by_file,
    _job_timing_fields,
    _log_metrics_from_row,
    _optimize_strategy_worker,
    _populate_metrics,
    _process_file_worker,
    _ratio_stats,
    _read_report_rows,
    _report_float,
    _run_one_strategy_with_timeout,
    _run_strategies_with_timeout,
    _run_strategy,
    _save_plot,
    _select_combined_winner,
    _select_ensemble_winner,
    _select_rapid_winner,
    _select_time_winner,
    _StrategyOutcome,
    _strip_private_keys,
    _summarize_file_result,
    _terminate_pool_workers,
    analyze_report_winners,
    build_ensemble_rows,
    build_output_directory,
    find_plt_files,
    main,
    process_file,
    write_report,
)
from plt_optimizer.core.optimizer import OptimizationResult, OptimizationStrategy

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_input_dir(tmp_path: Path) -> Path:
    """Create a temporary directory with one small PLT file inside."""
    input_dir = tmp_path / "cad"
    input_dir.mkdir()
    # Copy the smallest sample PLT from the examples folder so tests are
    # hermetic and don't depend on a particular cwd at runtime.
    src = Path(__file__).resolve().parents[1] / "examples" / "1-inch-square.plt"
    (input_dir / "square.plt").write_bytes(src.read_bytes())
    return input_dir


@pytest.fixture
def sample_output_dir(tmp_path: Path) -> Path:
    """Return a pre-created output directory next to ``sample_input_dir``."""
    output_dir = tmp_path / "cad_benchmark"
    (output_dir / "optimized").mkdir(parents=True)
    (output_dir / "plots").mkdir(parents=True)
    return output_dir


# ---------------------------------------------------------------------------
# ReportWriter
# ---------------------------------------------------------------------------


class TestReportWriter:
    """Tests for the thread-safe, streaming CSV writer."""

    def test_writes_header_on_open(self, tmp_path: Path) -> None:
        """Opening a ReportWriter should immediately write the header."""
        target = tmp_path / "report.csv"
        with ReportWriter(target, CSV_COLUMNS):
            pass

        with open(target, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            assert reader.fieldnames == CSV_COLUMNS

    def test_streams_rows_incrementally(self, tmp_path: Path) -> None:
        """Each ``write_row`` call should append and flush to disk."""
        target = tmp_path / "report.csv"
        with ReportWriter(target, CSV_COLUMNS) as writer:
            writer.write_row(_row("a.plt", "nn2opt", "success"))
            writer.write_row(_row("a.plt", "sa", "failed", error="boom"))

        with open(target, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        assert [r["strategy_name"] for r in rows] == ["nn2opt", "sa"]
        assert rows[1]["error_message"] == "boom"

    def test_strips_private_keys(self, tmp_path: Path) -> None:
        """Private keys prefixed with ``_`` must not leak into the CSV."""
        target = tmp_path / "report.csv"
        with ReportWriter(target, CSV_COLUMNS) as writer:
            row = _row("a.plt", "nn2opt", "success")
            row["_internal"] = "secret"
            writer.write_row(row)

        with open(target, newline="", encoding="utf-8") as f:
            text = f.read()
        assert "_internal" not in text
        assert "secret" not in text

    def test_context_manager_closes_file(self, tmp_path: Path) -> None:
        """Exiting the context should close the underlying file handle."""
        target = tmp_path / "report.csv"
        writer = ReportWriter(target, CSV_COLUMNS)
        with writer as w:
            assert not w._file.closed
        assert writer._file.closed

    def test_thread_safety(self, tmp_path: Path) -> None:
        """Concurrent ``write_row`` calls must not interleave or corrupt output."""
        import threading

        target = tmp_path / "report.csv"
        total = 50
        with ReportWriter(target, CSV_COLUMNS) as writer:

            def writer_task(i: int) -> None:
                writer.write_row(_row(f"f{i}.plt", "nn2opt", "success"))

            threads = [threading.Thread(target=writer_task, args=(i,)) for i in range(total)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        with open(target, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == total


# ---------------------------------------------------------------------------
# _strip_private_keys
# ---------------------------------------------------------------------------


class TestStripPrivateKeys:
    """Tests for the helper that removes private bookkeeping keys from rows."""

    def test_removes_underscore_prefixed_keys(self) -> None:
        """Private keys (``_`` prefix) must be removed from the returned dict."""
        row = _row("a.plt", "nn2opt", "success")
        row["_metrics_event"] = {"status": "success"}
        row["_optimized_plt_path"] = "/tmp/a.plt"
        clean = _strip_private_keys(row)
        assert "_metrics_event" not in clean
        assert "_optimized_plt_path" not in clean

    def test_preserves_public_columns(self) -> None:
        """All public CSV columns must be retained in the returned dict."""
        row = _row("a.plt", "nn2opt", "success")
        clean = _strip_private_keys(row)
        for col in CSV_COLUMNS:
            assert col in clean
        assert clean["file_name"] == "a.plt"
        assert clean["strategy_name"] == "nn2opt"

    def test_does_not_mutate_input(self) -> None:
        """The original dict must not be modified."""
        row = _row("a.plt", "nn2opt", "success")
        row["_metrics_event"] = {"status": "success"}
        _strip_private_keys(row)
        assert "_metrics_event" in row


# ---------------------------------------------------------------------------
# _summarize_file_result
# ---------------------------------------------------------------------------


class TestSummarizeFileResult:
    """Tests for the file-level status reducer."""

    def test_ok_when_any_strategy_succeeded(self) -> None:
        """A single success is enough to mark the file as OK."""
        rows = [
            _row("a.plt", "nn2opt", "failed", error="boom"),
            _row("a.plt", "sa", "success"),
        ]
        ok, summary = _summarize_file_result(rows)
        assert ok is True
        assert summary == "OK"

    def test_failed_when_no_strategy_succeeded(self) -> None:
        """All failures should produce a FAILED summary."""
        rows = [
            _row("a.plt", "nn2opt", "failed", error="boom"),
            _row("a.plt", "sa", "failed", error="other"),
        ]
        ok, summary = _summarize_file_result(rows)
        assert ok is False
        assert summary.startswith("FAILED:")

    def test_truncates_long_error_messages(self) -> None:
        """Long error messages should be truncated to 80 chars."""
        long_err = "x" * 200
        rows = [_row("a.plt", "nn2opt", "failed", error=long_err)]
        _, summary = _summarize_file_result(rows)
        # Format is "FAILED: <msg>" with msg truncated to 77 chars + "..."
        assert summary.endswith("...")
        assert len(summary) <= 80 + len("FAILED: ")


# ---------------------------------------------------------------------------
# _log_metrics_from_row
# ---------------------------------------------------------------------------


class TestLogMetricsFromRow:
    """Tests for re-emitting metrics events from worker rows."""

    def test_no_event_skips_logging(self) -> None:
        """Rows without a private metrics event should be ignored."""
        logger = MagicMock()
        row = _row("a.plt", "nn2opt", "success")
        _log_metrics_from_row(row, logger)
        logger.log_job.assert_not_called()

    def test_strategy_success_event(self) -> None:
        """A row carrying a success event should call log_job."""
        logger = MagicMock()
        row = _row("a.plt", "nn2opt", "success")
        row["_metrics_event"] = {
            "kind": "strategy",
            "strategy_name": "nn2opt",
            "status": "success",
            "job_id": "abc",
            "original_file": Path("/tmp/a.plt"),
            "optimized_file": Path("/tmp/a_optimized.plt"),
            "original_distance": 1000.0,
            "optimized_distance": 800.0,
            "notes": "",
        }
        _log_metrics_from_row(row, logger)
        logger.log_job.assert_called_once()
        kwargs = logger.log_job.call_args.kwargs
        assert kwargs["status"] == "success"
        assert kwargs["method"] == "nn2opt"
        assert kwargs["job_id"] == "abc"

    def test_failure_event_includes_notes(self) -> None:
        """A failure event should propagate the error notes."""
        logger = MagicMock()
        row = _row("a.plt", "nn2opt", "failed", error="boom")
        row["_metrics_event"] = {
            "kind": "strategy",
            "strategy_name": "nn2opt",
            "status": "failed",
            "job_id": "abc",
            "original_file": Path("/tmp/a.plt"),
            "optimized_file": None,
            "original_distance": 1000.0,
            "optimized_distance": 1000.0,
            "notes": "boom",
        }
        _log_metrics_from_row(row, logger)
        assert logger.log_job.call_args.kwargs["notes"] == "boom"


# ---------------------------------------------------------------------------
# _save_plot with logger=None
# ---------------------------------------------------------------------------


class TestSavePlotLoggerOptional:
    """``_save_plot`` must accept ``text_logger=None`` for subprocess use."""

    def test_none_logger_does_not_raise(self, sample_output_dir: Path) -> None:
        """A broken plot path with no logger should still return cleanly."""
        # Force plot_plt_document to fail by passing a None doc; if the
        # logger were called the test would still pass because it's None.
        _save_plot(
            doc=None,  # type: ignore[arg-type]
            plot_path=sample_output_dir / "plots" / "x.png",
            title="t",
            rapid_travel_inches=0.0,
            text_logger=None,
        )


# ---------------------------------------------------------------------------
# process_file with logger arguments
# ---------------------------------------------------------------------------


class TestProcessFileOptionalLoggers:
    """``process_file`` must work without logger side-effects."""

    def test_returns_rows_when_loggers_are_none(
        self, sample_input_dir: Path, sample_output_dir: Path
    ) -> None:
        """Calling ``process_file`` with ``None`` loggers still returns rows."""
        rows = process_file(
            input_path=sample_input_dir / "square.plt",
            output_dir=sample_output_dir,
            same_row_preference=1.0,
            metrics_logger=None,
            text_logger=None,
        )
        assert rows, "expected at least one row"
        # Every row must carry a private metrics event for the main process
        # to re-emit.
        assert all("_metrics_event" in row for row in rows)

    def test_metric_event_has_correct_status(
        self, sample_input_dir: Path, sample_output_dir: Path
    ) -> None:
        """Successful strategy rows should carry a ``status=success`` event."""
        rows = process_file(
            input_path=sample_input_dir / "square.plt",
            output_dir=sample_output_dir,
            same_row_preference=1.0,
            metrics_logger=None,
            text_logger=None,
        )
        success_rows = [r for r in rows if r["status"] == "success"]
        assert success_rows, "expected at least one strategy to succeed"
        assert all(r["_metrics_event"]["status"] == "success" for r in success_rows)

    def test_success_rows_carry_job_timing_ratios(
        self, sample_input_dir: Path, sample_output_dir: Path
    ) -> None:
        """Successful rows must carry per-job ms/path and ms/segment values."""
        rows = process_file(
            input_path=sample_input_dir / "square.plt",
            output_dir=sample_output_dir,
            same_row_preference=1.0,
            metrics_logger=None,
            text_logger=None,
        )
        success_rows = [r for r in rows if r["status"] == "success"]
        assert success_rows, "expected at least one strategy to succeed"
        for row in success_rows:
            assert float(row["ms_per_path"]) == pytest.approx(
                float(row["time_ms"]) / float(row["before_paths"]), rel=1e-4
            )
            assert float(row["ms_per_segment"]) == pytest.approx(
                float(row["time_ms"]) / float(row["before_segments"]), rel=1e-4
            )

    def test_file_level_failure_still_returns_sentinel(self, sample_output_dir: Path) -> None:
        """A missing input file should produce a sentinel row, not raise."""
        rows = process_file(
            input_path=Path("Z:/does/not/exist.plt"),
            output_dir=sample_output_dir,
            same_row_preference=1.0,
            metrics_logger=None,
            text_logger=None,
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["strategy_name"] == _FILE_LEVEL_SENTINEL
        assert row["status"] == "parse_failed"
        assert row["_metrics_event"]["kind"] == "file"

    def test_setup_failure_without_logger(
        self, sample_input_dir: Path, sample_output_dir: Path
    ) -> None:
        """A setup failure with ``text_logger=None`` must not crash.

        Covers the ``if text_logger is not None`` ``False`` branch in
        ``process_file``'s setup except block (lines 517-520).
        """
        from plt_optimizer.core.profiler import Profiler

        with patch.object(Profiler, "profile", side_effect=ValueError("boom")):
            rows = process_file(
                input_path=sample_input_dir / "square.plt",
                output_dir=sample_output_dir,
                same_row_preference=1.0,
                metrics_logger=None,
                text_logger=None,
            )
        assert len(rows) == 1
        assert rows[0]["status"] == "setup_failed"


# ---------------------------------------------------------------------------
# process_file / _run_strategy with loggers provided
# ---------------------------------------------------------------------------


class TestProcessFileWithLoggers:
    """``process_file`` must call provided loggers at the right moments."""

    def test_parse_failure_logs_error_and_metrics(
        self, sample_input_dir: Path, sample_output_dir: Path
    ) -> None:
        """A parse failure should log an error and emit a metrics event."""
        text_logger = MagicMock()
        metrics_logger = MagicMock()
        # Inject a synthetic parse failure via the parser. Patch where the
        # method actually lives, not the re-imported alias.
        from plt_optimizer.core.parser import ParseError, PLTParser

        with patch.object(PLTParser, "parse_file", side_effect=ParseError("synthetic boom")):
            rows = process_file(
                input_path=sample_input_dir / "square.plt",
                output_dir=sample_output_dir,
                same_row_preference=1.0,
                metrics_logger=metrics_logger,
                text_logger=text_logger,
            )
        assert len(rows) == 1
        assert rows[0]["status"] == "parse_failed"
        # Text logger should have received exactly one error.
        text_logger.error.assert_called_once()
        # process_file does NOT call metrics_logger directly — the main
        # process re-emits events from row["_metrics_event"] via
        # _log_metrics_from_row. Verify the row carries the payload.
        assert metrics_logger.log_job.call_count == 0
        event = rows[0]["_metrics_event"]
        assert event["kind"] == "file"
        assert event["status"] == "failed"
        assert "synthetic boom" in event["notes"]

    def test_setup_failure_logs_error_and_metrics(
        self, sample_input_dir: Path, sample_output_dir: Path
    ) -> None:
        """A setup failure should log an error and emit a metrics event."""
        text_logger = MagicMock()
        metrics_logger = MagicMock()
        # Make the profiler raise to trigger the setup_failed branch.
        from plt_optimizer.core.profiler import Profiler

        with patch.object(Profiler, "profile", side_effect=ValueError("synthetic setup boom")):
            rows = process_file(
                input_path=sample_input_dir / "square.plt",
                output_dir=sample_output_dir,
                same_row_preference=1.0,
                metrics_logger=metrics_logger,
                text_logger=text_logger,
            )
        assert len(rows) == 1
        assert rows[0]["status"] == "setup_failed"
        assert "setup boom" in rows[0]["error_message"]
        # Two log calls: error + full traceback
        assert text_logger.error.call_count == 2
        # Metrics events flow through _log_metrics_from_row, not log_job directly.
        assert metrics_logger.log_job.call_count == 0
        assert rows[0]["_metrics_event"]["kind"] == "file"
        assert "setup boom" in rows[0]["_metrics_event"]["notes"]

    def test_strategy_failure_logs_error_and_metrics(
        self, sample_input_dir: Path, sample_output_dir: Path
    ) -> None:
        """A failing strategy should log an error and emit a metrics event."""
        text_logger = MagicMock()
        metrics_logger = MagicMock()

        # Patch the genetic strategy class so constructing it raises.
        from plt_optimizer.core.optimizer import GeneticAlgorithmStrategy

        def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("synthetic strat boom")

        with patch.object(GeneticAlgorithmStrategy, "__init__", patched_init):
            rows = process_file(
                input_path=sample_input_dir / "square.plt",
                output_dir=sample_output_dir,
                same_row_preference=1.0,
                metrics_logger=metrics_logger,
                text_logger=text_logger,
            )

        # At least one success and at least one failure for the genetic strategy.
        statuses = [r["status"] for r in rows]
        assert "success" in statuses
        failed = [r for r in rows if r["status"] == "failed"]
        assert failed
        assert any("genetic" in r["strategy_name"] for r in failed), (
            "expected genetic strategy failure"
        )
        # Strategy failure should have been logged with strategy context.
        error_calls = [call.args[0] for call in text_logger.error.call_args_list]
        assert any("genetic" in msg for msg in error_calls)
        # The metrics logger is NOT called from process_file directly —
        # events are re-emitted by the main process from row["_metrics_event"].
        assert metrics_logger.log_job.call_count == 0

    def test_strategy_failure_without_logger(
        self, sample_input_dir: Path, sample_output_dir: Path
    ) -> None:
        """A failing strategy with ``text_logger=None`` must not crash.

        Covers the ``if text_logger is not None`` ``False`` branch in
        ``_run_strategy``'s except block.
        """
        from plt_optimizer.core.optimizer import GeneticAlgorithmStrategy

        def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("synthetic strat boom")

        with patch.object(GeneticAlgorithmStrategy, "__init__", patched_init):
            rows = process_file(
                input_path=sample_input_dir / "square.plt",
                output_dir=sample_output_dir,
                same_row_preference=1.0,
                metrics_logger=None,
                text_logger=None,
            )
        failed = [r for r in rows if r["status"] == "failed"]
        assert failed
        assert all(r["_metrics_event"]["status"] == "failed" for r in failed)


# ---------------------------------------------------------------------------
# _save_plot with logger
# ---------------------------------------------------------------------------


class TestSavePlotWithLogger:
    """``_save_plot`` must invoke the provided logger on failures."""

    def test_logger_warning_on_plot_failure(self, tmp_path: Path) -> None:
        """A failed plot should be reported via the logger's ``warning`` method."""
        text_logger = MagicMock()
        # Force plot_plt_document to raise to exercise the except branch.
        with patch(
            "plt_optimizer.cli.benchmark.plot_plt_document",
            side_effect=RuntimeError("synthetic plot boom"),
        ):
            _save_plot(
                doc=MagicMock(),
                plot_path=tmp_path / "x.png",
                title="t",
                rapid_travel_inches=0.0,
                text_logger=text_logger,
            )
        text_logger.warning.assert_called_once()
        # The warning should mention the failed plot's filename.
        assert "x.png" in text_logger.warning.call_args.args[0]


# ---------------------------------------------------------------------------
# _select_ensemble_winner
# ---------------------------------------------------------------------------


class TestSelectEnsembleWinner:
    """Tests for the strategy winner-selection helper."""

    def test_winner_is_highest_improvement(self) -> None:
        """The strategy with the highest improvement % must win."""
        rows = [
            _row("a.plt", "nn2opt", "success"),
            _row("a.plt", "sa", "success"),
        ]
        rows[0]["total_improvement_pct"] = 10.0
        rows[0]["total_after_in"] = 9.0
        rows[0]["time_ms"] = 5.0
        rows[1]["total_improvement_pct"] = 25.0
        rows[1]["total_after_in"] = 7.5
        rows[1]["time_ms"] = 50.0
        winner = _select_ensemble_winner(rows)
        assert winner["strategy_name"] == "sa"

    def test_tie_breaks_on_lowest_total_after(self) -> None:
        """On tie, the winner is the row with the lowest ``total_after_in``."""
        rows = [
            _row("a.plt", "nn2opt", "success"),
            _row("a.plt", "sa", "success"),
        ]
        rows[0]["total_improvement_pct"] = 20.0
        rows[0]["total_after_in"] = 8.0
        rows[0]["time_ms"] = 5.0
        rows[1]["total_improvement_pct"] = 20.0
        rows[1]["total_after_in"] = 6.0
        rows[1]["time_ms"] = 50.0
        winner = _select_ensemble_winner(rows)
        assert winner["strategy_name"] == "sa"

    def test_tie_breaks_on_fastest_runtime(self) -> None:
        """On tie of improvement % and total_after, the fastest wins."""
        rows = [
            _row("a.plt", "nn2opt", "success"),
            _row("a.plt", "sa", "success"),
        ]
        rows[0]["total_improvement_pct"] = 20.0
        rows[0]["total_after_in"] = 6.0
        rows[0]["time_ms"] = 50.0
        rows[1]["total_improvement_pct"] = 20.0
        rows[1]["total_after_in"] = 6.0
        rows[1]["time_ms"] = 5.0
        winner = _select_ensemble_winner(rows)
        assert winner["strategy_name"] == "sa"

    def test_empty_time_ms_treated_as_zero(self) -> None:
        """Empty ``time_ms`` strings must be coerced to 0.0."""
        rows = [
            _row("a.plt", "nn2opt", "success"),
            _row("a.plt", "sa", "success"),
        ]
        rows[0]["total_improvement_pct"] = 20.0
        rows[0]["total_after_in"] = 6.0
        rows[0]["time_ms"] = ""  # empty string
        rows[1]["total_improvement_pct"] = 20.0
        rows[1]["total_after_in"] = 6.0
        rows[1]["time_ms"] = 5.0
        winner = _select_ensemble_winner(rows)
        # Tied on improvement and total_after; nn2opt has time_ms=0 -> wins
        assert winner["strategy_name"] == "nn2opt"


# ---------------------------------------------------------------------------
# build_ensemble_rows
# ---------------------------------------------------------------------------


class TestBuildEnsembleRows:
    """Tests for the synthetic ensemble-row builder."""

    def test_winning_strategy_chosen(self) -> None:
        """Successful strategies must be replaced by a single ensemble row."""
        rows = [
            _success_row("a.plt", "nn2opt", improvement=10.0, total_after=9.0),
            _success_row("a.plt", "sa", improvement=25.0, total_after=7.5),
        ]
        ensemble = build_ensemble_rows(rows)
        assert len(ensemble) == 1
        assert ensemble[0]["strategy_name"] == "sa"
        assert ensemble[0]["status"] == "success"

    def test_file_level_failure_becomes_none_sentinel(self) -> None:
        """A file-level sentinel must be promoted to ``(none)``."""
        sentinel = _row("a.plt", _FILE_LEVEL_SENTINEL, "parse_failed", error="boom")
        ensemble = build_ensemble_rows([sentinel])
        assert len(ensemble) == 1
        assert ensemble[0]["strategy_name"] == _NO_WINNER_SENTINEL
        assert ensemble[0]["status"] == "parse_failed"
        assert ensemble[0]["error_message"] == "boom"

    def test_all_strategies_failed_branch(self) -> None:
        """Per-strategy failures without a file-level sentinel must synthesize a row."""
        rows = [
            _row("a.plt", "nn2opt", "failed", error=""),
            _row("a.plt", "sa", "failed", error="other"),
        ]
        ensemble = build_ensemble_rows(rows)
        assert len(ensemble) == 1
        assert ensemble[0]["strategy_name"] == _NO_WINNER_SENTINEL
        assert ensemble[0]["status"] == "all_strategies_failed"
        # Both error messages should be joined with "; "
        assert "other" in ensemble[0]["error_message"]

    def test_all_strategies_failed_keeps_existing_error(
        self,
    ) -> None:
        """If the base row already has an error, leave it alone (no join).

        Covers the ``if not ensemble_row["error_message"]`` ``False``
        branch (lines 801-807).
        """
        rows = [
            _row("a.plt", "nn2opt", "failed", error="first boom"),
            _row("a.plt", "sa", "failed", error="second boom"),
        ]
        ensemble = build_ensemble_rows(rows)
        assert len(ensemble) == 1
        assert ensemble[0]["status"] == "all_strategies_failed"
        # Base row's error preserved verbatim, no "; " joining.
        assert ensemble[0]["error_message"] == "first boom"

    def test_preserves_input_file_order(self) -> None:
        """Multiple files must be emitted in the order they first appear."""
        rows = [
            _success_row("b.plt", "nn2opt", improvement=10.0, total_after=9.0),
            _success_row("a.plt", "nn2opt", improvement=10.0, total_after=9.0),
            _success_row("b.plt", "sa", improvement=10.0, total_after=9.0),
        ]
        ensemble = build_ensemble_rows(rows)
        assert [r["file_name"] for r in ensemble] == ["b.plt", "a.plt"]

    def test_strips_private_keys(self) -> None:
        """Ensemble output rows must not carry private bookkeeping keys."""
        rows = [_success_row("a.plt", "nn2opt", improvement=10.0, total_after=9.0)]
        rows[0]["_metrics_event"] = {"status": "success"}
        ensemble = build_ensemble_rows(rows)
        assert all(not k.startswith("_") for k in ensemble[0])


# ---------------------------------------------------------------------------
# write_report
# ---------------------------------------------------------------------------


class TestWriteReport:
    """Tests for the bulk CSV writer."""

    def test_empty_rows_is_noop(self, tmp_path: Path) -> None:
        """An empty row list should produce no output file at all."""
        target = tmp_path / "report.csv"
        write_report([], target, CSV_COLUMNS)
        assert not target.exists()

    def test_writes_header_and_rows(self, tmp_path: Path) -> None:
        """A non-empty row list must produce a header + one row per input."""
        target = tmp_path / "report.csv"
        rows = [_row("a.plt", "nn2opt", "success")]
        write_report(rows, target, CSV_COLUMNS)
        with open(target, newline="", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
        assert len(reader) == 1
        assert reader[0]["strategy_name"] == "nn2opt"

    def test_strips_private_keys(self, tmp_path: Path) -> None:
        """Private keys must not be written to disk."""
        target = tmp_path / "report.csv"
        rows = [_row("a.plt", "nn2opt", "success")]
        rows[0]["_metrics_event"] = {"status": "success"}
        write_report(rows, target, CSV_COLUMNS)
        with open(target, newline="", encoding="utf-8") as f:
            text = f.read()
        assert "_metrics_event" not in text


# ---------------------------------------------------------------------------
# Winner analysis post-processing (analyze_report_winners and helpers)
# ---------------------------------------------------------------------------


class TestReadReportRows:
    """Tests for the report.csv reader."""

    def test_reads_rows_in_order(self, tmp_path: Path) -> None:
        """Rows must come back in on-disk order keyed by header names."""
        target = tmp_path / "report.csv"
        write_report(
            [_row("a.plt", "nn2opt", "success"), _row("b.plt", "sa", "failed")],
            target,
            CSV_COLUMNS,
        )
        rows = _read_report_rows(target)
        assert [r["file_name"] for r in rows] == ["a.plt", "b.plt"]

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """A missing report must raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            _read_report_rows(tmp_path / "nope.csv")


class TestReportFloat:
    """Tests for the lenient numeric-cell coercion helper."""

    def test_valid_number(self) -> None:
        """A normal numeric cell parses to float."""
        assert _report_float({"time_ms": "12.5"}, "time_ms") == 12.5

    def test_empty_string_is_zero(self) -> None:
        """An empty cell (failed strategy) coerces to 0.0."""
        assert _report_float({"time_ms": ""}, "time_ms") == 0.0

    def test_missing_column_is_zero(self) -> None:
        """A missing column coerces to 0.0."""
        assert _report_float({}, "rapid_improvement_pct") == 0.0

    def test_junk_value_is_zero(self) -> None:
        """A non-numeric cell must not raise; it coerces to 0.0."""
        assert _report_float({"time_ms": "n/a"}, "time_ms") == 0.0


class TestGroupEligibleRowsByFile:
    """Tests for the per-file eligibility grouping used by winners reports."""

    def test_excludes_no_opt_and_failures(self) -> None:
        """no-opt rows, failed rows and sentinels are never eligible."""
        rows = [
            _row("a.plt", "no-opt", "success"),
            _row("a.plt", "nn2opt", "success"),
            _row("a.plt", "sa", "failed"),
            _row("a.plt", _FILE_LEVEL_SENTINEL, "parse_failed"),
        ]
        grouped = _group_eligible_rows_by_file(rows)
        assert len(grouped) == 1
        assert grouped[0][0] == "a.plt"
        assert [r["strategy_name"] for r in grouped[0][1]] == ["nn2opt"]

    def test_preserves_file_order_and_omits_empty(self) -> None:
        """Files keep first-appearance order; all-failed files are omitted."""
        rows = [
            _row("b.plt", "nn2opt", "success"),
            _row("a.plt", "sa", "failed"),
            _row("b.plt", "sa", "success"),
            _row("c.plt", "nn2opt", "success"),
        ]
        grouped = _group_eligible_rows_by_file(rows)
        assert [name for name, _ in grouped] == ["b.plt", "c.plt"]
        assert [r["strategy_name"] for r in grouped[0][1]] == ["nn2opt", "sa"]


class TestCombinedScores:
    """Tests for the min-max normalised quality/speed blend."""

    def test_best_on_both_criteria_scores_one(self) -> None:
        """The row that is both fastest and most improved must score 1.0."""
        rows = [
            _rapid_row("a.plt", "fast", rapid_pct=50.0, time_ms=10.0),
            _rapid_row("a.plt", "slow", rapid_pct=10.0, time_ms=1000.0),
        ]
        scores = _combined_scores(rows)
        assert scores[0] == pytest.approx(1.0)
        assert scores[1] == pytest.approx(0.0)

    def test_perfect_tradeoff_ties(self) -> None:
        """A perfect quality/speed tradeoff must produce equal scores."""
        rows = [
            _rapid_row("a.plt", "quality", rapid_pct=60.0, time_ms=900.0),
            _rapid_row("a.plt", "speed", rapid_pct=10.0, time_ms=10.0),
        ]
        scores = _combined_scores(rows)
        assert scores[0] == pytest.approx(scores[1])

    def test_zero_spread_scores_one(self) -> None:
        """Exact ties on both criteria give every row a score of 1.0."""
        rows = [
            _rapid_row("a.plt", "x", rapid_pct=20.0, time_ms=5.0),
            _rapid_row("a.plt", "y", rapid_pct=20.0, time_ms=5.0),
        ]
        assert _combined_scores(rows) == [1.0, 1.0]

    def test_single_row_scores_one(self) -> None:
        """A lone successful strategy is its own winner with score 1.0."""
        rows = [_rapid_row("a.plt", "solo", rapid_pct=-3.0, time_ms=7.0)]
        assert _combined_scores(rows) == [1.0]

    def test_negative_improvements_are_normalised(self) -> None:
        """Negative improvements must not break the [0, 1] scaling."""
        rows = [
            _rapid_row("a.plt", "less_bad", rapid_pct=-1.0, time_ms=10.0),
            _rapid_row("a.plt", "worse", rapid_pct=-50.0, time_ms=20.0),
        ]
        scores = _combined_scores(rows)
        assert scores[0] == pytest.approx(1.0)
        assert scores[1] == pytest.approx(0.0)


class TestWinnerSelectors:
    """Tests for the three per-file winner selectors."""

    def test_rapid_winner_prefers_improvement(self) -> None:
        """Highest rapid_improvement_pct wins the rapid criterion."""
        rows = [
            _rapid_row("a.plt", "slow_but_good", rapid_pct=40.0, time_ms=900.0),
            _rapid_row("a.plt", "fast_but_weak", rapid_pct=5.0, time_ms=1.0),
        ]
        assert _select_rapid_winner(rows)["strategy_name"] == "slow_but_good"

    def test_rapid_winner_ties_break_on_time(self) -> None:
        """Equal improvements resolve to the faster strategy."""
        rows = [
            _rapid_row("a.plt", "slow", rapid_pct=30.0, time_ms=900.0),
            _rapid_row("a.plt", "fast", rapid_pct=30.0, time_ms=2.0),
        ]
        assert _select_rapid_winner(rows)["strategy_name"] == "fast"

    def test_rapid_winner_ties_break_on_name(self) -> None:
        """Full ties resolve alphabetically for determinism."""
        rows = [
            _rapid_row("a.plt", "zzz", rapid_pct=30.0, time_ms=5.0),
            _rapid_row("a.plt", "aaa", rapid_pct=30.0, time_ms=5.0),
        ]
        assert _select_rapid_winner(rows)["strategy_name"] == "aaa"

    def test_time_winner_prefers_speed(self) -> None:
        """Lowest time_ms wins the runtime criterion."""
        rows = [
            _rapid_row("a.plt", "slow", rapid_pct=90.0, time_ms=5000.0),
            _rapid_row("a.plt", "fast", rapid_pct=1.0, time_ms=3.0),
        ]
        assert _select_time_winner(rows)["strategy_name"] == "fast"

    def test_time_winner_ties_break_on_improvement(self) -> None:
        """Equal runtimes resolve to the more-improved strategy."""
        rows = [
            _rapid_row("a.plt", "weak", rapid_pct=2.0, time_ms=4.0),
            _rapid_row("a.plt", "strong", rapid_pct=50.0, time_ms=4.0),
        ]
        assert _select_time_winner(rows)["strategy_name"] == "strong"

    def test_empty_time_ms_treated_as_zero(self) -> None:
        """A blank time cell must not raise and sorts as fastest."""
        rows = [
            _rapid_row("a.plt", "timed", rapid_pct=10.0, time_ms=5.0),
            _rapid_row("a.plt", "blank", rapid_pct=10.0, time_ms=0.0),
        ]
        rows[1]["time_ms"] = ""
        assert _select_time_winner(rows)["strategy_name"] == "blank"

    def test_combined_winner_returns_row_and_score(self) -> None:
        """The combined selector returns the winner plus its score."""
        rows = [
            _rapid_row("a.plt", "balanced", rapid_pct=55.0, time_ms=100.0),
            _rapid_row("a.plt", "extreme", rapid_pct=60.0, time_ms=9000.0),
        ]
        winner, score = _select_combined_winner(rows)
        assert winner["strategy_name"] == "balanced"
        assert 0.0 <= score <= 1.0


class TestRatioStats:
    """Tests for the time-per-unit ratio aggregation helper."""

    def test_min_max_average(self) -> None:
        """Ratios are computed per row then reduced to min/max/avg."""
        rows = [
            _rapid_row("a.plt", "x", rapid_pct=1.0, time_ms=10.0, paths=5.0),
            _rapid_row("b.plt", "x", rapid_pct=1.0, time_ms=30.0, paths=15.0),
        ]
        lo, hi, avg = _ratio_stats(rows, "before_paths")
        assert lo == pytest.approx(2.0)
        assert hi == pytest.approx(2.0)
        assert avg == pytest.approx(2.0)

    def test_skips_non_positive_denominators(self) -> None:
        """Rows with zero/missing workload sizes must not divide."""
        rows = [
            _rapid_row("a.plt", "x", rapid_pct=1.0, time_ms=10.0, paths=0.0),
            _rapid_row("b.plt", "x", rapid_pct=1.0, time_ms=40.0, paths=8.0),
            _rapid_row("c.plt", "x", rapid_pct=1.0, time_ms=99.0),  # blank paths
        ]
        lo, hi, avg = _ratio_stats(rows, "before_paths")
        assert (lo, hi, avg) == (5.0, 5.0, 5.0)

    def test_all_usable_denominators_absent(self) -> None:
        """When no row qualifies, all three stats are None."""
        rows = [_rapid_row("a.plt", "x", rapid_pct=1.0, time_ms=10.0)]
        assert _ratio_stats(rows, "before_segments") == (None, None, None)


class TestComputeTimingStats:
    """Tests for the batch-wide per-strategy timing aggregation."""

    def test_aggregates_across_files(self) -> None:
        """Stats pool every eligible row for a strategy across all files."""
        grouped = _group_eligible_rows_by_file(
            [
                _rapid_row(
                    "a.plt", "nn2opt", rapid_pct=1.0, time_ms=10.0, paths=5.0, segments=50.0
                ),
                _rapid_row(
                    "b.plt", "nn2opt", rapid_pct=1.0, time_ms=60.0, paths=10.0, segments=100.0
                ),
            ]
        )
        stats = _compute_timing_stats(grouped)["nn2opt"]
        assert stats["max_time_ms"] == 60.0
        assert stats["min_ms_per_path"] == pytest.approx(2.0)
        assert stats["max_ms_per_path"] == pytest.approx(6.0)
        assert stats["avg_ms_per_path"] == pytest.approx(4.0)
        assert stats["min_ms_per_segment"] == pytest.approx(0.2)
        assert stats["max_ms_per_segment"] == pytest.approx(0.6)
        assert stats["avg_ms_per_segment"] == pytest.approx(0.4)

    def test_missing_workload_sizes_yield_none(self) -> None:
        """max_time_ms still computes when ratio stats are unavailable."""
        grouped = _group_eligible_rows_by_file(
            [_rapid_row("a.plt", "sa", rapid_pct=1.0, time_ms=250.0)]
        )
        stats = _compute_timing_stats(grouped)["sa"]
        assert stats["max_time_ms"] == 250.0
        for column in _TIMING_STAT_COLUMNS:
            if column != "max_time_ms":
                assert stats[column] is None

    def test_empty_grouped_returns_empty(self) -> None:
        """No eligible rows means no strategies to aggregate."""
        assert _compute_timing_stats([]) == {}


class TestJobTimingFields:
    """Tests for the per-job runtime-per-path/segment helper."""

    def test_computes_ratios_from_own_row(self) -> None:
        """The job's own time_ms divides by its own workload sizes."""
        row = _rapid_row("a.plt", "x", rapid_pct=1.0, time_ms=30.0, paths=10.0, segments=150.0)
        assert _job_timing_fields(row) == {"ms_per_path": 3.0, "ms_per_segment": 0.2}

    def test_missing_denominators_yield_blank_cells(self) -> None:
        """Zero/absent workload sizes must produce empty cells, not errors."""
        row = _rapid_row("a.plt", "x", rapid_pct=1.0, time_ms=30.0, paths=0.0)
        fields = _job_timing_fields(row)
        assert fields["ms_per_path"] == ""
        assert fields["ms_per_segment"] == ""

    def test_missing_time_ms_yields_blank_cells(self) -> None:
        """Failed rows without a recorded runtime must produce empty cells."""
        row = _row("a.plt", "x", "failed")
        row["before_paths"] = 10.0
        row["before_segments"] = 150.0
        assert _job_timing_fields(row) == {"ms_per_path": "", "ms_per_segment": ""}

    def test_columns_constant(self) -> None:
        """The helper's keys must match the exported column constant."""
        row = _rapid_row("a.plt", "x", rapid_pct=1.0, time_ms=1.0, paths=1.0, segments=1.0)
        assert sorted(_job_timing_fields(row)) == sorted(_WINNER_JOB_TIMING_COLUMNS)


class TestAnalyzeReportWinners:
    """End-to-end tests for the winners post-processing entry point."""

    def test_writes_three_csvs_with_winners(self, tmp_path: Path) -> None:
        """Each criterion CSV must contain the per-file winning rows."""
        report = _write_report_csv(
            tmp_path / "report.csv",
            [
                _rapid_row("a.plt", "no-opt", rapid_pct=0.0, time_ms=0.1),
                _rapid_row("a.plt", "nn2opt", rapid_pct=30.0, time_ms=5.0),
                _rapid_row("a.plt", "christofides", rapid_pct=45.0, time_ms=800.0),
                _rapid_row("a.plt", "insertion", rapid_pct=50.0, time_ms=20.0),
                _rapid_row("b.plt", "nn2opt", rapid_pct=10.0, time_ms=900.0),
                _rapid_row("b.plt", "sa", rapid_pct=12.0, time_ms=4000.0),
            ],
        )

        win_counts = analyze_report_winners(report)

        rapid_rows = _read_csv(tmp_path / "report_rapid_improvement_winners.csv")
        time_rows = _read_csv(tmp_path / "report_time_winners.csv")
        combined_rows = _read_csv(tmp_path / "report_combined_winners.csv")

        assert [(r["file_name"], r["strategy_name"]) for r in rapid_rows] == [
            ("a.plt", "insertion"),
            ("b.plt", "sa"),
        ]
        assert [(r["file_name"], r["strategy_name"]) for r in time_rows] == [
            ("a.plt", "nn2opt"),
            ("b.plt", "nn2opt"),
        ]
        # a.plt: insertion nearly tops both normalised criteria (best
        # improvement, second-fastest) -> combined winner. b.plt: sa beats
        # nn2opt on improvement but loses on time, so the 0.5/0.5 tie
        # resolves to the faster nn2opt.
        assert [(r["file_name"], r["strategy_name"]) for r in combined_rows] == [
            ("a.plt", "insertion"),
            ("b.plt", "nn2opt"),
        ]
        # Combined CSV carries the extra score column; others do not.
        # insertion: 0.5 * 1.0 + 0.5 * (800 - 20) / (800 - 5) = 0.9906.
        assert float(combined_rows[0]["combined_score"]) == pytest.approx(0.9906, abs=1e-4)
        assert float(combined_rows[1]["combined_score"]) == pytest.approx(0.5)
        assert "combined_score" not in rapid_rows[0]
        # Full winning rows are preserved (e.g. time_ms survives).
        assert rapid_rows[0]["time_ms"] == "20.0"

        assert win_counts["nn2opt"] == {"rapid_improvement": 0, "time": 2, "combined": 1}
        assert win_counts["insertion"] == {"rapid_improvement": 1, "time": 0, "combined": 1}
        # Observed but never winning strategies still appear in the table.
        assert win_counts["christofides"] == {"rapid_improvement": 0, "time": 0, "combined": 0}

    def test_no_opt_never_wins(self, tmp_path: Path) -> None:
        """Even a faster/cleaner no-opt row must be excluded from wins."""
        report = _write_report_csv(
            tmp_path / "report.csv",
            [
                _rapid_row("a.plt", "no-opt", rapid_pct=100.0, time_ms=0.01),
                _rapid_row("a.plt", "nn2opt", rapid_pct=1.0, time_ms=50.0),
            ],
        )
        win_counts = analyze_report_winners(report)
        assert "no-opt" not in win_counts
        for name in (
            "report_rapid_improvement_winners.csv",
            "report_time_winners.csv",
            "report_combined_winners.csv",
        ):
            rows = _read_csv(tmp_path / name)
            assert [r["strategy_name"] for r in rows] == ["nn2opt"]

    def test_files_without_winners_are_omitted(self, tmp_path: Path) -> None:
        """Parse-failed / all-failed files must not appear in winners CSVs."""
        report = _write_report_csv(
            tmp_path / "report.csv",
            [
                _row("bad.plt", _FILE_LEVEL_SENTINEL, "parse_failed", error="[parse] boom"),
                _row("worse.plt", "nn2opt", "failed", error="[nn2opt] boom"),
                _rapid_row("good.plt", "nn2opt", rapid_pct=5.0, time_ms=2.0),
            ],
        )
        win_counts = analyze_report_winners(report)
        for name in (
            "report_rapid_improvement_winners.csv",
            "report_time_winners.csv",
            "report_combined_winners.csv",
        ):
            rows = _read_csv(tmp_path / name)
            assert [r["file_name"] for r in rows] == ["good.plt"]
        assert win_counts == {"nn2opt": {"rapid_improvement": 1, "time": 1, "combined": 1}}

    def test_header_only_report_writes_nothing(self, tmp_path: Path, capsys: Any) -> None:
        """A report with no data rows must skip CSV writes and say so."""
        report = tmp_path / "report.csv"
        report.write_text(",".join(CSV_COLUMNS) + "\n", encoding="utf-8")

        win_counts = analyze_report_winners(report)

        assert win_counts == {}
        assert not (tmp_path / "report_rapid_improvement_winners.csv").exists()
        assert not (tmp_path / "report_winner_summary.csv").exists()
        out = capsys.readouterr().out
        assert "No successful non-baseline strategies found" in out

    def test_prints_summary_table(self, tmp_path: Path, capsys: Any) -> None:
        """The stdout table must list per-strategy wins for all criteria."""
        report = _write_report_csv(
            tmp_path / "report.csv",
            [
                _rapid_row("a.plt", "nn2opt", rapid_pct=30.0, time_ms=5.0),
                _rapid_row("a.plt", "sa", rapid_pct=45.0, time_ms=800.0),
            ],
        )
        analyze_report_winners(report)
        out = capsys.readouterr().out
        assert "STRATEGY WINNER SUMMARY" in out
        assert "rapid_improvement_wins" in out
        # The runtime-statistic columns are part of the header too.
        for column in _TIMING_STAT_COLUMNS:
            assert column in out
        # sa wins rapid, nn2opt wins time and combined. The rows carry no
        # before_paths/before_segments, so every ratio stat renders as n/a.
        table = out.split("STRATEGY WINNER SUMMARY", 1)[1]
        sa_line = next(line for line in table.splitlines() if line.startswith("sa"))
        nn_line = next(line for line in table.splitlines() if line.startswith("nn2opt"))
        assert sa_line.split() == ["sa", "1", "0", "0", "800", *(["n/a"] * 6)]
        assert nn_line.split() == ["nn2opt", "0", "1", "1", "5", *(["n/a"] * 6)]

    def test_writes_summary_csv_matching_stdout_table(self, tmp_path: Path, capsys: Any) -> None:
        """The summary CSV must mirror the printed win-count table exactly."""
        report = _write_report_csv(
            tmp_path / "report.csv",
            [
                _rapid_row("a.plt", "nn2opt", rapid_pct=30.0, time_ms=5.0, paths=5.0),
                _rapid_row("a.plt", "sa", rapid_pct=45.0, time_ms=800.0, paths=10.0),
            ],
        )
        analyze_report_winners(report)

        rows = _read_csv(tmp_path / "report_winner_summary.csv")
        assert [r["strategy"] for r in rows] == ["nn2opt", "sa"]
        nn, sa = rows
        # sa wins rapid; nn2opt wins time, and the combined tie (both 0.5)
        # breaks to the faster strategy, nn2opt.
        assert (nn["rapid_improvement_wins"], nn["runtime_wins"], nn["combined_wins"]) == (
            "0",
            "1",
            "1",
        )
        assert (sa["rapid_improvement_wins"], sa["runtime_wins"], sa["combined_wins"]) == (
            "1",
            "0",
            "0",
        )
        # Timing stats use the same compact rendering as the stdout table.
        assert nn["max_time_ms"] == "5"
        assert nn["min_ms_per_path"] == nn["max_ms_per_path"] == nn["avg_ms_per_path"] == "1"
        assert sa["max_time_ms"] == "800"
        assert sa["avg_ms_per_path"] == "80"
        # No before_segments in the fixture -> n/a, identical to the table.
        assert nn["min_ms_per_segment"] == "n/a"
        out = capsys.readouterr().out
        table = out.split("STRATEGY WINNER SUMMARY", 1)[1]
        nn_line = next(line for line in table.splitlines() if line.startswith("nn2opt"))
        assert nn_line.split() == [
            "nn2opt",
            "0",
            "1",
            "1",
            nn["max_time_ms"],
            nn["min_ms_per_path"],
            nn["max_ms_per_path"],
            nn["avg_ms_per_path"],
            nn["min_ms_per_segment"],
            nn["max_ms_per_segment"],
            nn["avg_ms_per_segment"],
        ]

    def test_win_counts_sum_to_file_count(self, tmp_path: Path) -> None:
        """Each criterion's wins must total the number of files with winners."""
        report = _write_report_csv(
            tmp_path / "report.csv",
            [
                _rapid_row(f"f{i}.plt", "nn2opt", rapid_pct=float(i), time_ms=float(10 - i))
                for i in range(3)
            ]
            + [
                _rapid_row(f"f{i}.plt", "sa", rapid_pct=float(3 - i), time_ms=float(20 + i))
                for i in range(3)
            ],
        )
        win_counts = analyze_report_winners(report)
        for criterion in ("rapid_improvement", "time", "combined"):
            total = sum(counts[criterion] for counts in win_counts.values())
            assert total == 3

    def test_custom_stem_is_respected(self, tmp_path: Path) -> None:
        """Output names derive from the report stem, not a hardcoded name."""
        report = _write_report_csv(
            tmp_path / "myrun.csv", [_rapid_row("a.plt", "nn2opt", rapid_pct=1.0, time_ms=1.0)]
        )
        analyze_report_winners(report)
        assert (tmp_path / "myrun_rapid_improvement_winners.csv").exists()
        assert (tmp_path / "myrun_time_winners.csv").exists()
        assert (tmp_path / "myrun_combined_winners.csv").exists()
        assert (tmp_path / "myrun_winner_summary.csv").exists()

    def test_winners_csvs_carry_job_timing_columns(self, tmp_path: Path) -> None:
        """Every winners CSV must carry the winning job's own runtime ratios.

        The values come from the winning row itself (its ``time_ms`` over its
        ``before_paths`` / ``before_segments``) via the canonical
        ``ms_per_path`` / ``ms_per_segment`` columns, not from batch-wide
        strategy aggregates, which live only in the winner-summary CSV.
        """
        report = _write_report_csv(
            tmp_path / "report.csv",
            [
                _rapid_row(
                    "a.plt", "nn2opt", rapid_pct=10.0, time_ms=10.0, paths=5.0, segments=50.0
                ),
                _rapid_row("a.plt", "sa", rapid_pct=20.0, time_ms=500.0, paths=5.0, segments=50.0),
                _rapid_row(
                    "b.plt", "nn2opt", rapid_pct=10.0, time_ms=60.0, paths=10.0, segments=100.0
                ),
                _rapid_row(
                    "b.plt", "sa", rapid_pct=20.0, time_ms=900.0, paths=10.0, segments=100.0
                ),
            ],
        )
        analyze_report_winners(report)

        for name in (
            "report_rapid_improvement_winners.csv",
            "report_time_winners.csv",
            "report_combined_winners.csv",
        ):
            rows = _read_csv(tmp_path / name)
            assert rows, name
            for row in rows:
                for column in _WINNER_JOB_TIMING_COLUMNS:
                    assert column in row, f"{name} missing {column}"
                # Batch-wide aggregates belong to the summary CSV only.
                for column in _TIMING_STAT_COLUMNS:
                    assert column not in row, f"{name} unexpectedly has {column}"
            # sa wins every rapid criterion here; each winning row must carry
            # its own ratios: a.plt 500/5=100 ms/path, 500/50=10 ms/segment;
            # b.plt 900/10=90 ms/path, 900/100=9 ms/segment.
            sa_rows = {r["file_name"]: r for r in rows if r["strategy_name"] == "sa"}
            if sa_rows:
                assert float(sa_rows["a.plt"]["ms_per_path"]) == pytest.approx(100.0)
                assert float(sa_rows["a.plt"]["ms_per_segment"]) == pytest.approx(10.0)
                assert float(sa_rows["b.plt"]["ms_per_path"]) == pytest.approx(90.0)
                assert float(sa_rows["b.plt"]["ms_per_segment"]) == pytest.approx(9.0)

    def test_unavailable_ratios_render_as_blank_cells(self, tmp_path: Path) -> None:
        """Winning jobs without workload sizes must not emit 'None' into CSVs."""
        report = _write_report_csv(
            tmp_path / "report.csv",
            [_rapid_row("a.plt", "nn2opt", rapid_pct=1.0, time_ms=5.0)],
        )
        analyze_report_winners(report)
        rows = _read_csv(tmp_path / "report_time_winners.csv")
        for column in _WINNER_JOB_TIMING_COLUMNS:
            assert rows[0][column] == ""

    def test_legacy_report_gets_job_timing_backfilled(self, tmp_path: Path) -> None:
        """Reports written before the ratio columns existed must still win.

        The winners CSVs recompute ``ms_per_path`` / ``ms_per_segment`` from
        the row's own ``time_ms`` / ``before_*`` values, so historical
        reports lacking the columns produce fully populated winners rows.
        """
        legacy_columns = [c for c in CSV_COLUMNS if c not in _WINNER_JOB_TIMING_COLUMNS]
        report = tmp_path / "report.csv"
        with open(report, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=legacy_columns)
            writer.writeheader()
            writer.writerow(
                {
                    "file_name": "a.plt",
                    "strategy_name": "nn2opt",
                    "status": "success",
                    "rapid_improvement_pct": "10.0",
                    "time_ms": "30.0",
                    "before_paths": "10",
                    "before_segments": "150",
                }
            )
        analyze_report_winners(report)
        rows = _read_csv(tmp_path / "report_time_winners.csv")
        assert float(rows[0]["ms_per_path"]) == pytest.approx(3.0)
        assert float(rows[0]["ms_per_segment"]) == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# find_plt_files / build_output_directory
# ---------------------------------------------------------------------------


class TestFindPltFiles:
    """Tests for the PLT file discovery helper."""

    def test_finds_sorted_plt_files(self, sample_input_dir: Path) -> None:
        """PLT files should be discovered and sorted alphabetically."""
        (sample_input_dir / "z.plt").write_bytes(b"IN;SP1;PU0,0;PD;PU;")
        (sample_input_dir / "a.plt").write_bytes(b"IN;SP1;PU0,0;PD;PU;")
        (sample_input_dir / "ignore.txt").write_text("not a plt")
        files = find_plt_files(sample_input_dir)
        names = [f.name for f in files]
        assert names == ["a.plt", "square.plt", "z.plt"]

    def test_empty_directory_returns_empty_list(self, tmp_path: Path) -> None:
        """A directory without PLT files should return an empty list."""
        assert find_plt_files(tmp_path) == []


class TestBuildOutputDirectory:
    """Tests for the output-directory construction helper."""

    def test_creates_adjacent_benchmark_dir(self, tmp_path: Path) -> None:
        """Output directory must be created next to the input dir."""
        input_dir = tmp_path / "cad"
        input_dir.mkdir()
        out = build_output_directory(input_dir)
        assert out == tmp_path / "cad_benchmark"
        assert out.exists()
        assert (out / "optimized").exists()
        assert (out / "plots").exists()

    def test_idempotent_when_called_twice(self, tmp_path: Path) -> None:
        """Re-running must not raise even if the dir already exists."""
        input_dir = tmp_path / "cad"
        input_dir.mkdir()
        out1 = build_output_directory(input_dir)
        out2 = build_output_directory(input_dir)
        assert out1 == out2


# ---------------------------------------------------------------------------
# CSV schema helpers
# ---------------------------------------------------------------------------


class TestCsvSchemaHelpers:
    """Tests for the module-level CSV schema helpers."""

    def test_csv_columns_match_canonical_order(self) -> None:
        """The exported ``CSV_COLUMNS`` must match the canonical order."""
        assert CSV_COLUMNS == _build_csv_columns()

    def test_csv_columns_first_is_file_name(self) -> None:
        """The first column must be ``file_name`` for downstream tools."""
        assert CSV_COLUMNS[0] == "file_name"

    def test_job_timing_columns_follow_time_ms(self) -> None:
        """``ms_per_path``/``ms_per_segment`` must sit right after ``time_ms``."""
        time_index = CSV_COLUMNS.index("time_ms")
        assert CSV_COLUMNS[time_index + 1 : time_index + 3] == _WINNER_JOB_TIMING_COLUMNS

    def test_empty_row_initializes_all_columns(self) -> None:
        """``_empty_row`` must populate every column with ``""``."""
        row = _empty_row("foo.plt")
        for col in CSV_COLUMNS:
            if col == "file_name":
                continue
            assert row[col] == "", f"column {col!r} should default to ''"
        assert row["file_name"] == "foo.plt"


# ---------------------------------------------------------------------------
# main() — argument parsing, error paths, parallel dispatch
# ---------------------------------------------------------------------------


def _fake_future(
    index: int,
    plt_file: Path,
    rows: list[dict[str, Any]],
    elapsed_s: float,
    *,
    raises: Optional[BaseException] = None,
) -> Any:
    """Build a minimal Future stub for ``main()``'s ``as_completed`` loop."""
    fut: Any = MagicMock()
    if raises is not None:
        fut.result.side_effect = raises
    else:
        fut.result.return_value = FileResult(
            input_path=str(plt_file), elapsed_s=elapsed_s, rows=rows
        )
    return fut


class TestMain:
    """Tests for ``main()`` covering args, errors, and the parallel loop."""

    def test_missing_input_dir_returns_1(self, tmp_path: Path, capsys: Any) -> None:
        """A non-existent input dir must exit with code 1 and print to stderr."""
        missing = tmp_path / "nope"
        rc = main(["--workers", "1", str(missing)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "Input directory not found" in err

    def test_empty_input_dir_returns_0(self, tmp_path: Path, capsys: Any) -> None:
        """An empty input dir must exit 0 cleanly with an informational message."""
        empty = tmp_path / "cad"
        empty.mkdir()
        rc = main(["--workers", "1", str(empty)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "No PLT files found" in out

    def test_no_arguments_returns_1(self, capsys: Any) -> None:
        """Omitting both input_dir and --analyze-only must exit 1 with an error."""
        rc = main([])
        assert rc == 1
        err = capsys.readouterr().err
        assert "input_dir is required" in err

    def test_analyze_only_runs_post_processing(self, tmp_path: Path, capsys: Any) -> None:
        """--analyze-only must analyze the report and write winners CSVs."""
        report = _write_report_csv(
            tmp_path / "report.csv",
            [
                _rapid_row("a.plt", "nn2opt", rapid_pct=10.0, time_ms=5.0),
                _rapid_row("a.plt", "sa", rapid_pct=20.0, time_ms=500.0),
            ],
        )
        rc = main(["--analyze-only", str(report)])
        assert rc == 0
        assert (tmp_path / "report_rapid_improvement_winners.csv").exists()
        assert (tmp_path / "report_time_winners.csv").exists()
        assert (tmp_path / "report_combined_winners.csv").exists()
        assert (tmp_path / "report_winner_summary.csv").exists()
        out = capsys.readouterr().out
        assert "STRATEGY WINNER SUMMARY" in out

    def test_analyze_only_missing_report_returns_1(self, tmp_path: Path, capsys: Any) -> None:
        """--analyze-only against a missing report must exit 1 with an error."""
        rc = main(["--analyze-only", str(tmp_path / "nope.csv")])
        assert rc == 1
        err = capsys.readouterr().err
        assert "Report CSV not found" in err

    def test_analyze_only_with_input_dir_returns_1(
        self, sample_input_dir: Path, capsys: Any
    ) -> None:
        """Passing both input_dir and --analyze-only must exit 1 as ambiguous."""
        rc = main(["--analyze-only", "report.csv", str(sample_input_dir)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "mutually exclusive" in err

    def test_happy_path_streams_results(
        self, sample_input_dir: Path, tmp_path: Path, capsys: Any
    ) -> None:
        """The parallel loop must stream per-strategy rows and the ensemble CSV."""
        plt_file = sample_input_dir / "square.plt"

        # Build two synthetic per-strategy rows that the worker would return.
        rows = [
            _row(plt_file.name, "nn2opt", "success"),
            _row(plt_file.name, "sa", "success"),
        ]
        rows[0]["total_improvement_pct"] = 20.0
        rows[0]["total_after_in"] = 8.0
        rows[0]["time_ms"] = 5.0
        rows[0]["_metrics_event"] = {
            "kind": "strategy",
            "strategy_name": "nn2opt",
            "status": "success",
            "job_id": "j1",
            "original_file": plt_file,
            "optimized_file": plt_file,
            "original_distance": 1000.0,
            "optimized_distance": 800.0,
            "notes": "",
        }
        rows[1]["total_improvement_pct"] = 10.0
        rows[1]["total_after_in"] = 9.0
        rows[1]["time_ms"] = 7.0
        rows[1]["_metrics_event"] = {
            "kind": "strategy",
            "strategy_name": "sa",
            "status": "success",
            "job_id": "j2",
            "original_file": plt_file,
            "optimized_file": plt_file,
            "original_distance": 1000.0,
            "optimized_distance": 900.0,
            "notes": "",
        }

        text_logger = MagicMock()
        metrics_logger = MagicMock()
        fake_future = _fake_future(1, plt_file, rows, elapsed_s=0.1)
        fake_executor = MagicMock()
        fake_executor.__enter__.return_value = fake_executor
        fake_executor.submit.return_value = fake_future

        with patch(
            "plt_optimizer.cli.benchmark.get_text_logger",
            return_value=text_logger,
        ), patch(
            "plt_optimizer.cli.benchmark.get_metrics_logger",
            return_value=metrics_logger,
        ), patch(
            "plt_optimizer.cli.benchmark.ProcessPoolExecutor",
            return_value=fake_executor,
        ), patch(
            "plt_optimizer.cli.benchmark.as_completed",
            return_value=iter([fake_future]),
        ):
            rc = main(["--workers", "1", str(sample_input_dir)])

        assert rc == 0
        # CSVs must exist next to the input dir.
        bench_dir = sample_input_dir.parent / f"{sample_input_dir.name}_benchmark"
        report = bench_dir / "report.csv"
        ensemble_report = bench_dir / "ensemble_report.csv"
        assert report.exists()
        assert ensemble_report.exists()
        # Winner analysis must have run automatically after the reports.
        assert (bench_dir / "report_rapid_improvement_winners.csv").exists()
        assert (bench_dir / "report_time_winners.csv").exists()
        assert (bench_dir / "report_combined_winners.csv").exists()
        # Per-strategy CSV must contain both rows.
        with open(report, newline="", encoding="utf-8") as f:
            written = list(csv.DictReader(f))
        assert len(written) == 2
        # Metrics re-emitted for both strategies.
        assert metrics_logger.log_job.call_count == 2
        # Worker spawning happened with the expected kwargs.
        fake_executor.submit.assert_called_once()
        # Output captures timing + summary lines.
        out = capsys.readouterr().out
        assert "OK" in out
        assert "BENCHMARK COMPLETE" in out
        # The text logger received the per-file completion event.
        text_logger.info.assert_any_call("[1/1] square.plt done in 0.10s (avg 0.10s, ETA 0.0s)")

    def test_future_exception_is_recorded(
        self, sample_input_dir: Path, tmp_path: Path, capsys: Any
    ) -> None:
        """A future raising in the worker must be counted as a failure."""
        plt_file = sample_input_dir / "square.plt"
        text_logger = MagicMock()
        metrics_logger = MagicMock()
        fake_future = _fake_future(
            1,
            plt_file,
            rows=[],
            elapsed_s=0.0,
            raises=RuntimeError("worker crashed"),
        )
        fake_executor = MagicMock()
        fake_executor.__enter__.return_value = fake_executor
        fake_executor.submit.return_value = fake_future

        with patch(
            "plt_optimizer.cli.benchmark.get_text_logger",
            return_value=text_logger,
        ), patch(
            "plt_optimizer.cli.benchmark.get_metrics_logger",
            return_value=metrics_logger,
        ), patch(
            "plt_optimizer.cli.benchmark.ProcessPoolExecutor",
            return_value=fake_executor,
        ), patch(
            "plt_optimizer.cli.benchmark.as_completed",
            return_value=iter([fake_future]),
        ):
            rc = main(["--workers", "1", str(sample_input_dir)])

        assert rc == 0  # CLI exits 0 even when files fail
        text_logger.error.assert_any_call("[1/1] square.plt crashed: RuntimeError: worker crashed")
        # The full traceback is also logged.
        traceback_calls = [call.args[0] for call in text_logger.error.call_args_list]
        assert any("Traceback" in arg for arg in traceback_calls)
        # metrics re-emission path is skipped for crashes (no rows)
        assert metrics_logger.log_job.call_count == 0
        out = capsys.readouterr().out
        assert "CRASHED" in out

    def test_default_workers_capped_by_cpu_count(
        self, sample_input_dir: Path, tmp_path: Path, capsys: Any
    ) -> None:
        """When --workers is omitted, the worker count must default to min(files, cpus)."""
        text_logger = MagicMock()
        metrics_logger = MagicMock()
        plt_file = sample_input_dir / "square.plt"
        rows = [_success_row(plt_file.name, "nn2opt", improvement=10.0, total_after=9.0)]
        rows[0]["_metrics_event"] = {
            "kind": "strategy",
            "strategy_name": "nn2opt",
            "status": "success",
            "job_id": "j1",
            "original_file": plt_file,
            "optimized_file": plt_file,
            "original_distance": 1000.0,
            "optimized_distance": 800.0,
            "notes": "",
        }
        fake_future = _fake_future(1, plt_file, rows, elapsed_s=0.0)
        fake_executor = MagicMock()
        fake_executor.__enter__.return_value = fake_executor
        fake_executor.submit.return_value = fake_future

        with patch(
            "plt_optimizer.cli.benchmark.get_text_logger",
            return_value=text_logger,
        ), patch(
            "plt_optimizer.cli.benchmark.get_metrics_logger",
            return_value=metrics_logger,
        ), patch(
            "plt_optimizer.cli.benchmark.ProcessPoolExecutor",
            return_value=fake_executor,
        ) as proc_exec, patch(
            "plt_optimizer.cli.benchmark.as_completed",
            return_value=iter([fake_future]),
        ), patch("plt_optimizer.cli.benchmark.os.cpu_count", return_value=64):
            main([str(sample_input_dir)])

        # ProcessPoolExecutor was constructed with a positive max_workers.
        kwargs = proc_exec.call_args.kwargs
        assert kwargs["max_workers"] >= 1

    def test_linux_affinity_branch(
        self, sample_input_dir: Path, tmp_path: Path, capsys: Any
    ) -> None:
        """The ``sched_getaffinity`` branch on Linux must still cap workers."""
        text_logger = MagicMock()
        metrics_logger = MagicMock()
        plt_file = sample_input_dir / "square.plt"
        rows = [_success_row(plt_file.name, "nn2opt", improvement=10.0, total_after=9.0)]
        rows[0]["_metrics_event"] = {
            "kind": "strategy",
            "strategy_name": "nn2opt",
            "status": "success",
            "job_id": "j1",
            "original_file": plt_file,
            "optimized_file": plt_file,
            "original_distance": 1000.0,
            "optimized_distance": 800.0,
            "notes": "",
        }
        fake_future = _fake_future(1, plt_file, rows, elapsed_s=0.0)
        fake_executor = MagicMock()
        fake_executor.__enter__.return_value = fake_executor
        fake_executor.submit.return_value = fake_future

        # Pretend we're on Linux by making sched_getaffinity succeed.
        with patch(
            "plt_optimizer.cli.benchmark.get_text_logger",
            return_value=text_logger,
        ), patch(
            "plt_optimizer.cli.benchmark.get_metrics_logger",
            return_value=metrics_logger,
        ), patch(
            "plt_optimizer.cli.benchmark.ProcessPoolExecutor",
            return_value=fake_executor,
        ) as proc_exec, patch(
            "plt_optimizer.cli.benchmark.as_completed",
            return_value=iter([fake_future]),
        ), patch(
            "plt_optimizer.cli.benchmark.os.sched_getaffinity",
            return_value={0, 1, 2, 3},
            create=True,
        ), patch(
            "plt_optimizer.cli.benchmark.hasattr",
            side_effect=lambda obj, name: name == "sched_getaffinity",
        ):
            main(["--workers", "10", str(sample_input_dir)])

        # sched_getaffinity returned 4 cores, so worker count must be capped.
        kwargs = proc_exec.call_args.kwargs
        assert kwargs["max_workers"] == 4

    def test_sched_getaffinity_oserror_branch(
        self, sample_input_dir: Path, tmp_path: Path, capsys: Any
    ) -> None:
        """An ``OSError`` from ``sched_getaffinity`` must fall back gracefully."""
        text_logger = MagicMock()
        metrics_logger = MagicMock()
        plt_file = sample_input_dir / "square.plt"
        rows = [_success_row(plt_file.name, "nn2opt", improvement=10.0, total_after=9.0)]
        rows[0]["_metrics_event"] = {
            "kind": "strategy",
            "strategy_name": "nn2opt",
            "status": "success",
            "job_id": "j1",
            "original_file": plt_file,
            "optimized_file": plt_file,
            "original_distance": 1000.0,
            "optimized_distance": 800.0,
            "notes": "",
        }
        fake_future = _fake_future(1, plt_file, rows, elapsed_s=0.0)
        fake_executor = MagicMock()
        fake_executor.__enter__.return_value = fake_executor
        fake_executor.submit.return_value = fake_future

        with patch(
            "plt_optimizer.cli.benchmark.get_text_logger",
            return_value=text_logger,
        ), patch(
            "plt_optimizer.cli.benchmark.get_metrics_logger",
            return_value=metrics_logger,
        ), patch(
            "plt_optimizer.cli.benchmark.ProcessPoolExecutor",
            return_value=fake_executor,
        ) as proc_exec, patch(
            "plt_optimizer.cli.benchmark.as_completed",
            return_value=iter([fake_future]),
        ), patch(
            "plt_optimizer.cli.benchmark.os.sched_getaffinity",
            side_effect=OSError("nope"),
            create=True,
        ), patch(
            "plt_optimizer.cli.benchmark.hasattr",
            side_effect=lambda obj, name: name == "sched_getaffinity",
        ):
            main(["--workers", "3", str(sample_input_dir)])

        # The except branch should set max_workers from the original count.
        kwargs = proc_exec.call_args.kwargs
        assert kwargs["max_workers"] == 3

    def test_rolling_window_evicts_after_10_files(
        self, sample_input_dir: Path, capsys: Any
    ) -> None:
        """Submitting >10 files must trigger the rolling-window eviction branch."""
        # Make 12 fake PLT files in the input dir.
        text_logger = MagicMock()
        metrics_logger = MagicMock()
        for i in range(12):
            (sample_input_dir / f"file{i}.plt").write_bytes(b"IN;SP1;PU0,0;PD;PU;")

        plt_files = sorted(sample_input_dir.glob("*.plt"))
        futures = []
        for idx, plt_file in enumerate(plt_files, start=1):
            rows = [_success_row(plt_file.name, "nn2opt", improvement=10.0, total_after=9.0)]
            rows[0]["_metrics_event"] = {
                "kind": "strategy",
                "strategy_name": "nn2opt",
                "status": "success",
                "job_id": f"j{idx}",
                "original_file": plt_file,
                "optimized_file": plt_file,
                "original_distance": 1000.0,
                "optimized_distance": 800.0,
                "notes": "",
            }
            futures.append(_fake_future(idx, plt_file, rows, elapsed_s=0.1 * idx))

        fake_executor = MagicMock()
        fake_executor.__enter__.return_value = fake_executor
        fake_executor.submit.side_effect = futures

        with patch(
            "plt_optimizer.cli.benchmark.get_text_logger",
            return_value=text_logger,
        ), patch(
            "plt_optimizer.cli.benchmark.get_metrics_logger",
            return_value=metrics_logger,
        ), patch(
            "plt_optimizer.cli.benchmark.ProcessPoolExecutor",
            return_value=fake_executor,
        ), patch(
            "plt_optimizer.cli.benchmark.as_completed",
            return_value=iter(futures),
        ):
            main(["--workers", "1", str(sample_input_dir)])

        out = capsys.readouterr().out
        assert "BENCHMARK COMPLETE" in out
        # The summary must report the right number of files.
        assert "Total files:" in out
        assert "12" in out


# ---------------------------------------------------------------------------
# _populate_metrics (regression)
# ---------------------------------------------------------------------------


class TestPopulateMetrics:
    """Sanity check that the metric-population helper still behaves correctly."""

    def test_populates_all_columns(self) -> None:
        """All metric-related columns should be filled in by the helper."""
        row = _row("a.plt", "nn2opt", "success")
        _populate_metrics(
            row,
            before_rapid=1000.0,
            before_cutting=2000.0,
            optimized_rapid=500.0,
            optimized_cutting=1500.0,
            time_ms=123.4,
        )
        assert row["rapid_after_in"] == 0.5
        assert row["cutting_after_in"] == 1.5
        assert row["total_before_in"] == 3.0
        assert row["total_after_in"] == 2.0
        assert row["rapid_saved_in"] == 0.5
        assert row["cutting_saved_in"] == 0.5
        assert row["total_saved_in"] == 1.0
        assert row["rapid_improvement_pct"] == 50.0
        assert row["cutting_improvement_pct"] == 25.0
        assert row["total_improvement_pct"] == pytest.approx(33.33, abs=0.01)
        assert row["time_ms"] == 123.4


# ---------------------------------------------------------------------------
# _process_file_worker (parallel boundary)
# ---------------------------------------------------------------------------


class TestProcessFileWorker:
    """The parallel worker must return a FileResult across processes."""

    def test_worker_returns_fileresult(
        self, sample_input_dir: Path, sample_output_dir: Path
    ) -> None:
        """A single-file submission should yield a populated FileResult."""
        plt_file = sample_input_dir / "square.plt"
        with ProcessPoolExecutor(max_workers=1) as ex:
            future = ex.submit(
                _process_file_worker,
                str(plt_file),
                str(sample_output_dir),
                1.0,
            )
            result = future.result(timeout=60)

        assert isinstance(result, FileResult)
        assert result.input_path == str(plt_file)
        assert result.elapsed_s >= 0.0
        assert result.rows, "expected at least one row from the worker"

    def test_worker_handles_missing_file(self, sample_output_dir: Path) -> None:
        """A missing input should return a parse_failed row, not raise."""
        with ProcessPoolExecutor(max_workers=1) as ex:
            future = ex.submit(
                _process_file_worker,
                str(Path("Z:/does/not/exist.plt")),
                str(sample_output_dir),
                1.0,
            )
            result = future.result(timeout=60)

        assert isinstance(result, FileResult)
        assert len(result.rows) == 1
        assert result.rows[0]["status"] == "parse_failed"

    def test_worker_direct_call(self, sample_input_dir: Path, sample_output_dir: Path) -> None:
        """Calling the worker in-process must also return a FileResult.

        This exercises lines 587-598 which subprocess execution doesn't
        cover for in-process coverage tracking.
        """
        plt_file = sample_input_dir / "square.plt"
        result = _process_file_worker(str(plt_file), str(sample_output_dir), 1.0)
        assert isinstance(result, FileResult)
        assert result.input_path == str(plt_file)
        assert result.rows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row(
    file_name: str,
    strategy: str,
    status: str,
    *,
    error: str = "",
) -> dict[str, Any]:
    """Build a minimal CSV row dict with the canonical schema."""
    row = dict.fromkeys(CSV_COLUMNS, "")
    row["file_name"] = file_name
    row["strategy_name"] = strategy
    row["status"] = status
    row["error_message"] = error
    return row


def _success_row(
    file_name: str,
    strategy: str,
    *,
    improvement: float,
    total_after: float,
    time_ms: float = 1.0,
) -> dict[str, Any]:
    """Build a success row with the numeric columns populated.

    Most ensemble-related tests need a row that survives the
    ``_select_ensemble_winner`` sort key, which calls ``float()`` on the
    numeric columns. This helper sets the minimum set of fields so those
    tests can focus on the selector logic.
    """
    row = _row(file_name, strategy, "success")
    row["total_improvement_pct"] = improvement
    row["total_after_in"] = total_after
    row["time_ms"] = time_ms
    return row


def _rapid_row(
    file_name: str,
    strategy: str,
    *,
    rapid_pct: float,
    time_ms: float,
    paths: float | None = None,
    segments: float | None = None,
) -> dict[str, Any]:
    """Build a success row with the rapid-analysis columns populated.

    Winner-analysis tests key off ``rapid_improvement_pct`` and ``time_ms``
    only; this helper sets exactly those (plus ``status == "success"``).
    ``paths``/``segments`` optionally populate the workload-size columns used
    by the per-path / per-segment runtime statistics.
    """
    row = _row(file_name, strategy, "success")
    row["rapid_improvement_pct"] = rapid_pct
    row["time_ms"] = time_ms
    if paths is not None:
        row["before_paths"] = paths
    if segments is not None:
        row["before_segments"] = segments
    return row


def _write_report_csv(path: Path, rows: list[dict[str, Any]]) -> Path:
    """Write winner-analysis input rows to ``path`` as a report.csv stand-in.

    Numeric values are stringified the way ``ReportWriter``/``csv`` would on
    disk, so tests exercise the same string-typed cells that
    :func:`analyze_report_winners` sees in production.
    """
    with open(path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if v == "" else str(v)) for k, v in row.items()})
    return path


def _read_csv(path: Path) -> list[dict[str, str]]:
    """Read a CSV file into a list of string dicts."""
    with open(path, newline="", encoding="utf-8") as csvfile:
        return list(csv.DictReader(csvfile))


# ---------------------------------------------------------------------------
# _run_ensemble_row / real ensemble integration
# ---------------------------------------------------------------------------


class TestRunEnsembleRow:
    """Tests for the real ParallelEnsemble row appended by process_file."""

    @staticmethod
    def _fake_ensemble_result() -> Any:
        """Build a ParallelEnsembleOptimizationResult wrapper for mocking."""
        from plt_optimizer.core.optimizer import (
            BlockTraverseState,
            OptimizationResult,
            ParallelEnsembleOptimizationResult,
        )

        inner = OptimizationResult(
            traverse_order=(
                BlockTraverseState(block_id=0, reversed=False, entrance=(0, 0), exit=(10, 0)),
            ),
            connections=(),
            total_travel_distance=42.0,
            initial_position=(0.0, 0.0),
        )
        return ParallelEnsembleOptimizationResult(
            result=inner,
            winner_name="NoOp (Baseline)",
            all_benchmarks=(),
        )

    def test_success_row(self, sample_input_dir: Path, sample_output_dir: Path) -> None:
        """A successful ensemble run produces a metrics-complete success row."""
        from plt_optimizer.cli.benchmark import _ENSEMBLE_STRATEGY_NAME, _run_ensemble_row

        fake_strategy = MagicMock()
        fake_strategy.optimize.return_value = self._fake_ensemble_result()

        fake_doc = MagicMock()
        fake_doc.rapid_distance.return_value = 800.0
        fake_doc.cutting_distance.return_value = 500.0

        with patch(
            "plt_optimizer.cli.benchmark.ParallelEnsembleStrategy",
            return_value=fake_strategy,
        ) as MockStrategy, patch(
            "plt_optimizer.cli.benchmark.Reassembler"
        ) as MockReassembler, patch("plt_optimizer.cli.benchmark.PLTWriter"):
            MockReassembler.return_value.reassemble.return_value = fake_doc
            row = _run_ensemble_row(
                blocks=[MagicMock()],
                doc=MagicMock(),
                before_rapid=1000.0,
                before_cutting=500.0,
                input_path=sample_input_dir / "square.plt",
                output_dir=sample_output_dir,
                ensemble_timeout=7.5,
                same_row_preference=2.0,
                text_logger=None,
            )

        assert row["strategy_name"] == _ENSEMBLE_STRATEGY_NAME
        assert row["status"] == "success"
        assert row["rapid_saved_in"] == pytest.approx(0.2, rel=1e-3)
        assert row["rapid_improvement_pct"] == pytest.approx(20.0, rel=1e-3)
        assert row["time_ms"] != ""
        # Strategy constructed with the configured timeout + preference.
        MockStrategy.assert_called_once_with(
            baseline_distance=1000.0, job_timeout=7.5, same_row_preference=2.0
        )
        # The wrapper must be unwrapped before reassembly.
        reassemble_arg = MockReassembler.return_value.reassemble.call_args[0][2]
        assert reassemble_arg.total_travel_distance == 42.0
        assert row["_metrics_event"]["status"] == "success"

    def test_failure_row(self, sample_input_dir: Path, sample_output_dir: Path) -> None:
        """An ensemble exception yields a failed row with the [ensemble] prefix."""
        from plt_optimizer.cli.benchmark import _ENSEMBLE_STRATEGY_NAME, _run_ensemble_row

        text_logger = MagicMock()
        with patch(
            "plt_optimizer.cli.benchmark.ParallelEnsembleStrategy",
            side_effect=RuntimeError("ensemble boom"),
        ):
            row = _run_ensemble_row(
                blocks=[MagicMock()],
                doc=MagicMock(),
                before_rapid=1000.0,
                before_cutting=500.0,
                input_path=sample_input_dir / "square.plt",
                output_dir=sample_output_dir,
                ensemble_timeout=10.0,
                same_row_preference=1.0,
                text_logger=text_logger,
            )

        assert row["strategy_name"] == _ENSEMBLE_STRATEGY_NAME
        assert row["status"] == "failed"
        assert row["error_message"].startswith("[ensemble]")
        assert "ensemble boom" in row["error_message"]
        assert row["_metrics_event"]["status"] == "failed"
        error_calls = [call.args[0] for call in text_logger.error.call_args_list]
        assert any("ensemble" in msg for msg in error_calls)

    def test_failure_without_logger_does_not_crash(
        self, sample_input_dir: Path, sample_output_dir: Path
    ) -> None:
        """text_logger=None on failure must not raise (subprocess path)."""
        from plt_optimizer.cli.benchmark import _run_ensemble_row

        with patch(
            "plt_optimizer.cli.benchmark.ParallelEnsembleStrategy",
            side_effect=RuntimeError("boom"),
        ):
            row = _run_ensemble_row(
                blocks=[MagicMock()],
                doc=MagicMock(),
                before_rapid=1000.0,
                before_cutting=500.0,
                input_path=sample_input_dir / "square.plt",
                output_dir=sample_output_dir,
                ensemble_timeout=10.0,
                same_row_preference=1.0,
                text_logger=None,
            )
        assert row["status"] == "failed"


class TestProcessFileEnsembleRow:
    """``process_file`` must append a real ensemble row after the strategies."""

    def test_last_row_is_ensemble(self, sample_input_dir: Path, sample_output_dir: Path) -> None:
        """The final row carries strategy_name='ensemble' with file metrics tagged."""
        from plt_optimizer.cli.benchmark import _ENSEMBLE_STRATEGY_NAME

        fake_strategy = MagicMock()
        fake_result = TestRunEnsembleRow._fake_ensemble_result()
        fake_strategy.optimize.return_value = fake_result
        fake_doc = MagicMock()
        fake_doc.rapid_distance.return_value = 900.0
        fake_doc.cutting_distance.return_value = 500.0

        with patch(
            "plt_optimizer.cli.benchmark.ParallelEnsembleStrategy",
            return_value=fake_strategy,
        ) as MockStrategy, patch(
            "plt_optimizer.cli.benchmark.Reassembler"
        ) as MockReassembler, patch("plt_optimizer.cli.benchmark.PLTWriter"):
            MockReassembler.return_value.reassemble.return_value = fake_doc
            rows = process_file(
                input_path=sample_input_dir / "square.plt",
                output_dir=sample_output_dir,
                same_row_preference=1.0,
                ensemble_timeout=5.0,
                metrics_logger=None,
                text_logger=None,
            )

        assert rows[-1]["strategy_name"] == _ENSEMBLE_STRATEGY_NAME
        assert rows[-1]["status"] == "success"
        assert rows[-1]["before_paths"] != ""
        assert rows[-1]["blocks_created"] != ""
        # The configured timeout reached the strategy constructor.
        assert MockStrategy.call_args.kwargs["job_timeout"] == 5.0
        # Every strategy row precedes the ensemble row.
        names = [r["strategy_name"] for r in rows]
        assert names.index(_ENSEMBLE_STRATEGY_NAME) == len(names) - 1


class TestEnsembleWinnerExclusion:
    """The real ensemble row must never compete in winners reports."""

    def test_group_eligible_rows_excludes_ensemble(self) -> None:
        """Rows with strategy_name='ensemble' are filtered as ineligible."""
        rows = [
            {"file_name": "a.plt", "status": "success", "strategy_name": "ensemble"},
            {"file_name": "a.plt", "status": "success", "strategy_name": "nn2opt"},
        ]
        grouped = _group_eligible_rows_by_file(rows)
        assert len(grouped) == 1
        names = [r["strategy_name"] for r in grouped[0][1]]
        assert names == ["nn2opt"]

    def test_build_ensemble_rows_ignores_real_ensemble(self) -> None:
        """The synthetic ensemble winner excludes the real ensemble row."""
        nn = _row("a.plt", "nn2opt", "success")
        nn["total_improvement_pct"] = 10.0
        nn["total_after_in"] = 9.0
        nn["time_ms"] = 5.0
        ens = _row("a.plt", "ensemble", "success")
        ens["total_improvement_pct"] = 99.0
        ens["total_after_in"] = 1.0
        ens["time_ms"] = 6.0

        out = build_ensemble_rows([nn, ens])
        assert len(out) == 1
        # The synthetic winner must be the nn2opt simulation, not the real run.
        assert out[0]["strategy_name"] == "nn2opt"

    def test_all_ensemble_only_file_gets_no_winner(self) -> None:
        """A file whose only success is the ensemble row gets no winner."""
        ens = _row("a.plt", "ensemble", "success")
        grouped = _group_eligible_rows_by_file([ens])
        assert grouped == []


class TestMainEnsembleTimeoutArg:
    """main() must parse --ensemble-timeout and pass it to the workers."""

    def test_default_passed_to_submit(self, sample_input_dir: Path) -> None:
        """Without the flag, workers receive the 10.0s default."""
        plt_file = sample_input_dir / "square.plt"
        rows = [_row(plt_file.name, "nn2opt", "success")]
        rows[0]["total_improvement_pct"] = 10.0
        rows[0]["total_after_in"] = 9.0
        rows[0]["time_ms"] = 5.0
        fake_future = _fake_future(1, plt_file, rows, elapsed_s=0.1)
        fake_executor = MagicMock()
        fake_executor.__enter__.return_value = fake_executor
        fake_executor.submit.return_value = fake_future

        with patch("plt_optimizer.cli.benchmark.get_text_logger", return_value=MagicMock()), patch(
            "plt_optimizer.cli.benchmark.get_metrics_logger", return_value=MagicMock()
        ), patch(
            "plt_optimizer.cli.benchmark.ProcessPoolExecutor", return_value=fake_executor
        ), patch("plt_optimizer.cli.benchmark.as_completed", return_value=iter([fake_future])):
            rc = main(["--workers", "1", str(sample_input_dir)])

        assert rc == 0
        submit_args = fake_executor.submit.call_args[0]
        # (worker, input_path, output_dir, same_row_preference, ensemble_timeout)
        assert submit_args[4] == 10.0

    def test_custom_value_passed_to_submit(self, sample_input_dir: Path) -> None:
        """--ensemble-timeout 2.5 reaches the worker submit args."""
        plt_file = sample_input_dir / "square.plt"
        rows = [_row(plt_file.name, "nn2opt", "success")]
        rows[0]["total_improvement_pct"] = 10.0
        rows[0]["total_after_in"] = 9.0
        rows[0]["time_ms"] = 5.0
        fake_future = _fake_future(1, plt_file, rows, elapsed_s=0.1)
        fake_executor = MagicMock()
        fake_executor.__enter__.return_value = fake_executor
        fake_executor.submit.return_value = fake_future

        with patch("plt_optimizer.cli.benchmark.get_text_logger", return_value=MagicMock()), patch(
            "plt_optimizer.cli.benchmark.get_metrics_logger", return_value=MagicMock()
        ), patch(
            "plt_optimizer.cli.benchmark.ProcessPoolExecutor", return_value=fake_executor
        ), patch("plt_optimizer.cli.benchmark.as_completed", return_value=iter([fake_future])):
            rc = main(["--workers", "1", "--ensemble-timeout", "2.5", str(sample_input_dir)])

        assert rc == 0
        assert fake_executor.submit.call_args[0][4] == 2.5


# ---------------------------------------------------------------------------
# Per-strategy subprocess timeout helpers
# ---------------------------------------------------------------------------


class _InstantStrategy(OptimizationStrategy):
    """Picklable test strategy returning a trivial result immediately."""

    @property
    def name(self) -> str:
        """Return the strategy name."""
        return "instant"

    def optimize(
        self,
        blocks: List[Any],
        initial_position: Optional[Tuple[float, float]] = None,
        end_point: Optional[Tuple[float, float]] = None,
    ) -> OptimizationResult:
        """Return an empty result without doing any work."""
        return OptimizationResult(
            traverse_order=(),
            connections=(),
            total_travel_distance=0.0,
            initial_position=None,
        )


class _SleepingStrategy(OptimizationStrategy):
    """Picklable test strategy that sleeps far longer than any test timeout."""

    @property
    def name(self) -> str:
        """Return the strategy name."""
        return "sleeping"

    def optimize(
        self,
        blocks: List[Any],
        initial_position: Optional[Tuple[float, float]] = None,
        end_point: Optional[Tuple[float, float]] = None,
    ) -> OptimizationResult:
        """Sleep for an hour so the timeout is guaranteed to fire."""
        time.sleep(3600)  # pragma: no cover - killed by the timeout
        raise AssertionError("unreachable")  # pragma: no cover


class _ExplodingStrategy(OptimizationStrategy):
    """Picklable test strategy whose ``optimize`` always raises."""

    @property
    def name(self) -> str:
        """Return the strategy name."""
        return "exploding"

    def optimize(
        self,
        blocks: List[Any],
        initial_position: Optional[Tuple[float, float]] = None,
        end_point: Optional[Tuple[float, float]] = None,
    ) -> OptimizationResult:
        """Raise unconditionally."""
        raise RuntimeError("synthetic boom")


class TestRunOneStrategyWithTimeout:
    """``_run_one_strategy_with_timeout`` bounds each run in a subprocess."""

    def test_success_returns_result_and_elapsed(self) -> None:
        """A fast strategy yields its result plus a positive elapsed time."""
        outcome = _run_one_strategy_with_timeout(
            _InstantStrategy(), [], strategy_timeout=60.0, text_logger=None
        )
        assert outcome.error is None
        assert isinstance(outcome.result, OptimizationResult)
        assert outcome.elapsed_ms is not None
        assert outcome.elapsed_ms >= 0.0

    def test_timeout_aborts_and_reports(self) -> None:
        """A strategy exceeding the budget is killed and reported as timed out."""
        text_logger = MagicMock()
        outcome = _run_one_strategy_with_timeout(
            _SleepingStrategy(), [], strategy_timeout=1.0, text_logger=text_logger
        )
        assert outcome.result is None
        assert outcome.elapsed_ms is None
        assert outcome.error == "timed out after 1.0s"
        assert any("timed out" in str(call) for call in text_logger.warning.call_args_list)

    def test_strategy_exception_becomes_failed_outcome(self) -> None:
        """An exception inside the child surfaces as a failed outcome."""
        outcome = _run_one_strategy_with_timeout(
            _ExplodingStrategy(), [], strategy_timeout=60.0, text_logger=None
        )
        assert outcome.result is None
        assert outcome.error is not None
        # The engine normalizes strategy failures into OptimizationError.
        assert "OptimizationError" in outcome.error
        assert "synthetic boom" in outcome.error

    def test_pool_creation_failure_falls_back_in_process(self) -> None:
        """When no subprocess pool can be created, run in-process instead."""
        with patch(
            "plt_optimizer.cli.benchmark.ProcessPoolExecutor",
            side_effect=OSError("no processes allowed"),
        ):
            text_logger = MagicMock()
            outcome = _run_one_strategy_with_timeout(
                _InstantStrategy(), [], strategy_timeout=60.0, text_logger=text_logger
            )
        assert outcome.error is None
        assert isinstance(outcome.result, OptimizationResult)
        assert any("in-process" in str(call) for call in text_logger.warning.call_args_list)

    def test_pool_creation_failure_fallback_reports_errors(self) -> None:
        """In-process fallback still converts exceptions into failed outcomes."""
        with patch(
            "plt_optimizer.cli.benchmark.ProcessPoolExecutor",
            side_effect=OSError("no processes allowed"),
        ):
            outcome = _run_one_strategy_with_timeout(
                _ExplodingStrategy(), [], strategy_timeout=60.0, text_logger=None
            )
        assert outcome.error is not None
        assert "synthetic boom" in outcome.error


class TestRunStrategiesWithTimeout:
    """``_run_strategies_with_timeout`` runs strategies sequentially, one budget each."""

    def test_returns_outcome_per_strategy_in_order(self) -> None:
        """Every strategy gets an outcome, preserving insertion order."""
        strategies = {"instant": _InstantStrategy(), "exploding": _ExplodingStrategy()}
        outcomes = _run_strategies_with_timeout(
            strategies, [], strategy_timeout=60.0, text_logger=None
        )
        assert list(outcomes) == ["instant", "exploding"]
        assert outcomes["instant"].error is None
        assert outcomes["exploding"].error is not None

    def test_timeout_only_affects_the_offending_strategy(self) -> None:
        """A slow strategy fails while its fast sibling still succeeds."""
        strategies = {"instant": _InstantStrategy(), "sleeping": _SleepingStrategy()}
        outcomes = _run_strategies_with_timeout(
            strategies, [], strategy_timeout=1.0, text_logger=None
        )
        assert outcomes["instant"].error is None
        assert outcomes["sleeping"].error == "timed out after 1.0s"


class TestTerminatePoolWorkers:
    """``_terminate_pool_workers`` must degrade gracefully on odd executors."""

    def test_terminates_tracked_processes(self) -> None:
        """Each tracked worker process receives ``terminate()``."""
        executor = MagicMock()
        proc_a, proc_b = MagicMock(), MagicMock()
        executor._processes = {1: proc_a, 2: proc_b}  # noqa: SLF001
        _terminate_pool_workers(executor)
        proc_a.terminate.assert_called_once()
        proc_b.terminate.assert_called_once()

    def test_missing_processes_attribute_is_noop(self) -> None:
        """An executor without ``_processes`` is left alone."""
        executor = MagicMock(spec=[])
        _terminate_pool_workers(executor)  # must not raise

    def test_terminate_errors_are_swallowed(self) -> None:
        """A worker refusing to terminate must not propagate."""
        executor = MagicMock()
        grumpy = MagicMock()
        grumpy.terminate.side_effect = OSError("already gone")
        executor._processes = {1: grumpy}  # noqa: SLF001
        _terminate_pool_workers(executor)  # must not raise


class TestRunStrategyOutcomeConsumption:
    """``_run_strategy`` turns a pre-computed outcome into a CSV row."""

    def test_failed_outcome_short_circuits(self, sample_input_dir: Path, sample_output_dir: Path) -> None:
        """An errored outcome produces a failed row without reassembly."""
        text_logger = MagicMock()
        doc = MagicMock()
        row = _run_strategy(
            strategy_name="genetic",
            outcome=_StrategyOutcome(result=None, elapsed_ms=None, error="timed out after 10.0s"),
            doc=doc,
            blocks=[],
            before_rapid=100.0,
            before_cutting=50.0,
            input_path=sample_input_dir / "square.plt",
            output_dir=sample_output_dir,
            text_logger=text_logger,
        )
        assert row["status"] == "failed"
        assert "timed out after 10.0s" in row["error_message"]
        assert row["_metrics_event"]["status"] == "failed"
        # Reassembly must never be attempted for a failed outcome.
        doc.rapid_distance.assert_not_called()
        assert any("genetic" in str(call) for call in text_logger.error.call_args_list)

    def test_failed_outcome_without_logger(self, sample_input_dir: Path, sample_output_dir: Path) -> None:
        """A failed outcome with ``text_logger=None`` must not crash."""
        row = _run_strategy(
            strategy_name="sa",
            outcome=_StrategyOutcome(result=None, elapsed_ms=None, error="boom"),
            doc=MagicMock(),
            blocks=[],
            before_rapid=100.0,
            before_cutting=50.0,
            input_path=sample_input_dir / "square.plt",
            output_dir=sample_output_dir,
            text_logger=None,
        )
        assert row["status"] == "failed"

    def test_success_without_result_is_reported_as_failure(
        self, sample_input_dir: Path, sample_output_dir: Path
    ) -> None:
        """A success outcome missing its result degrades to a failed row."""
        row = _run_strategy(
            strategy_name="nn2opt",
            outcome=_StrategyOutcome(result=None, elapsed_ms=1.0, error=None),
            doc=MagicMock(),
            blocks=[],
            before_rapid=100.0,
            before_cutting=50.0,
            input_path=sample_input_dir / "square.plt",
            output_dir=sample_output_dir,
            text_logger=None,
        )
        assert row["status"] == "failed"
        assert "without producing a result" in row["error_message"]

    def test_no_opt_outcome_reuses_baseline_metrics(
        self, sample_input_dir: Path, sample_output_dir: Path
    ) -> None:
        """The no-opt outcome writes the input document with unchanged metrics."""
        from plt_optimizer.core.parser import PLTParser

        doc = PLTParser().parse_file(sample_input_dir / "square.plt")
        before_rapid = doc.rapid_distance()
        before_cutting = doc.cutting_distance()

        row = _run_strategy(
            strategy_name="no-opt",
            outcome=_StrategyOutcome(result=None, elapsed_ms=0.0, error=None),
            doc=doc,
            blocks=[],
            before_rapid=before_rapid,
            before_cutting=before_cutting,
            input_path=sample_input_dir / "square.plt",
            output_dir=sample_output_dir,
            text_logger=None,
        )
        assert row["status"] == "success"
        assert row["rapid_after_in"] == round(before_rapid / 1000, 3)
        assert row["cutting_after_in"] == round(before_cutting / 1000, 3)
        assert float(row["total_improvement_pct"]) == 0.0
        assert row["_optimized_plt_path"] is not None


class TestProcessFilePerStrategyTimeout:
    """``process_file`` must bound every individual strategy run."""

    def test_timeout_marks_strategy_failed_without_killing_file(
        self, sample_input_dir: Path, sample_output_dir: Path
    ) -> None:
        """A timed-out strategy yields a failed row; the rest still succeed."""
        real_runner = _run_one_strategy_with_timeout

        def selective_runner(
            strategy: Any, blocks: Any, strategy_timeout: float, text_logger: Any
        ) -> _StrategyOutcome:
            if type(strategy).__name__ == "GeneticAlgorithmStrategy":
                return _StrategyOutcome(None, None, f"timed out after {strategy_timeout}s")
            return real_runner(strategy, blocks, strategy_timeout, text_logger)

        text_logger = MagicMock()
        with patch(
            "plt_optimizer.cli.benchmark._run_one_strategy_with_timeout",
            side_effect=selective_runner,
        ):
            rows = process_file(
                input_path=sample_input_dir / "square.plt",
                output_dir=sample_output_dir,
                same_row_preference=1.0,
                ensemble_timeout=7.5,
                metrics_logger=None,
                text_logger=text_logger,
            )

        by_name = {r["strategy_name"]: r for r in rows}
        assert by_name["genetic"]["status"] == "failed"
        assert "timed out after 7.5s" in by_name["genetic"]["error_message"]
        assert by_name["nn2opt"]["status"] == "success"
        assert by_name["no-opt"]["status"] == "success"

    def test_timeout_is_forwarded_to_every_strategy(
        self, sample_input_dir: Path, sample_output_dir: Path
    ) -> None:
        """The configured budget reaches every per-strategy subprocess run."""
        seen: list[float] = []
        real_runner = _run_one_strategy_with_timeout

        def spy_runner(strategy: Any, blocks: Any, strategy_timeout: float, text_logger: Any) -> _StrategyOutcome:
            seen.append(strategy_timeout)
            return real_runner(strategy, blocks, strategy_timeout, text_logger)

        with patch(
            "plt_optimizer.cli.benchmark._run_one_strategy_with_timeout",
            side_effect=spy_runner,
        ):
            process_file(
                input_path=sample_input_dir / "square.plt",
                output_dir=sample_output_dir,
                same_row_preference=1.0,
                ensemble_timeout=3.25,
                metrics_logger=None,
                text_logger=None,
            )

        # One call per real strategy (no-opt is synthesized, never run).
        assert seen == [3.25] * 5

    def test_construction_failure_becomes_failed_row(
        self, sample_input_dir: Path, sample_output_dir: Path
    ) -> None:
        """A strategy that cannot even be constructed reports a failed row."""
        from plt_optimizer.core.optimizer import GeneticAlgorithmStrategy

        def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("synthetic construct boom")

        with patch.object(GeneticAlgorithmStrategy, "__init__", patched_init):
            rows = process_file(
                input_path=sample_input_dir / "square.plt",
                output_dir=sample_output_dir,
                same_row_preference=1.0,
                ensemble_timeout=30.0,
                metrics_logger=None,
                text_logger=None,
            )

        by_name = {r["strategy_name"]: r for r in rows}
        assert by_name["genetic"]["status"] == "failed"
        assert "synthetic construct boom" in by_name["genetic"]["error_message"]
        assert by_name["nn2opt"]["status"] == "success"


class TestOptimizeStrategyWorker:
    """``_optimize_strategy_worker`` times the strategy call inside the child."""

    def test_returns_result_and_elapsed(self) -> None:
        """The worker returns the strategy result and a non-negative duration."""
        result, elapsed_s = _optimize_strategy_worker(_InstantStrategy(), [])
        assert isinstance(result, OptimizationResult)
        assert elapsed_s >= 0.0
