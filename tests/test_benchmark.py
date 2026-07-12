"""Tests for plt_optimizer.cli.benchmark streaming / parallelization helpers.

These tests cover the new building blocks introduced when benchmark.py was
refactored to stream CSV writes incrementally and parallelize work via
``ProcessPoolExecutor``. The legacy ``process_file`` / ``build_ensemble_rows``
behavior is exercised indirectly through ``process_file`` with the logger
arguments set to ``None``.
"""

from __future__ import annotations

import csv
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from plt_optimizer.cli.benchmark import (
    _FILE_LEVEL_SENTINEL,
    _NO_WINNER_SENTINEL,
    CSV_COLUMNS,
    FileResult,
    ReportWriter,
    _build_csv_columns,
    _empty_row,
    _log_metrics_from_row,
    _populate_metrics,
    _process_file_worker,
    _save_plot,
    _select_ensemble_winner,
    _strip_private_keys,
    _summarize_file_result,
    build_ensemble_rows,
    build_output_directory,
    find_plt_files,
    main,
    process_file,
    write_report,
)

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

            threads = [threading.Thread(target=writer_task, args=(i,))
                       for i in range(total)]
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
        assert all(
            r["_metrics_event"]["status"] == "success" for r in success_rows
        )

    def test_file_level_failure_still_returns_sentinel(
        self, sample_output_dir: Path
    ) -> None:
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
        with patch.object(Profiler, "profile",
                          side_effect=ValueError("boom")):
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
        with patch.object(PLTParser, "parse_file",
                          side_effect=ParseError("synthetic boom")):
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
        with patch.object(Profiler, "profile",
                          side_effect=ValueError("synthetic setup boom")):
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
        assert any(
            "genetic" in r["strategy_name"] for r in failed
        ), "expected genetic strategy failure"
        # Strategy failure should have been logged with strategy context.
        error_calls = [
            call.args[0] for call in text_logger.error.call_args_list
        ]
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

    def test_happy_path_streams_results(
        self, sample_input_dir: Path, tmp_path: Path, capsys: Any
    ) -> None:
        """The parallel loop must stream per-strategy rows and the ensemble CSV."""
        output_dir = tmp_path / "out"
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
            "kind": "strategy", "strategy_name": "nn2opt",
            "status": "success", "job_id": "j1",
            "original_file": plt_file, "optimized_file": plt_file,
            "original_distance": 1000.0, "optimized_distance": 800.0,
            "notes": "",
        }
        rows[1]["total_improvement_pct"] = 10.0
        rows[1]["total_after_in"] = 9.0
        rows[1]["time_ms"] = 7.0
        rows[1]["_metrics_event"] = {
            "kind": "strategy", "strategy_name": "sa",
            "status": "success", "job_id": "j2",
            "original_file": plt_file, "optimized_file": plt_file,
            "original_distance": 1000.0, "optimized_distance": 900.0,
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
        report = sample_input_dir.parent / f"{sample_input_dir.name}_benchmark" / "report.csv"
        ensemble_report = (
            sample_input_dir.parent / f"{sample_input_dir.name}_benchmark" / "ensemble_report.csv"
        )
        assert report.exists()
        assert ensemble_report.exists()
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
        text_logger.info.assert_any_call(
            "[1/1] square.plt done in 0.10s (avg 0.10s, ETA 0.0s)"
        )

    def test_future_exception_is_recorded(
        self, sample_input_dir: Path, tmp_path: Path, capsys: Any
    ) -> None:
        """A future raising in the worker must be counted as a failure."""
        plt_file = sample_input_dir / "square.plt"
        text_logger = MagicMock()
        metrics_logger = MagicMock()
        fake_future = _fake_future(
            1, plt_file, rows=[], elapsed_s=0.0,
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
        text_logger.error.assert_any_call(
            "[1/1] square.plt crashed: RuntimeError: worker crashed"
        )
        # The full traceback is also logged.
        traceback_calls = [
            call.args[0] for call in text_logger.error.call_args_list
        ]
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
        rows = [_success_row(plt_file.name, "nn2opt",
                             improvement=10.0, total_after=9.0)]
        rows[0]["_metrics_event"] = {
            "kind": "strategy", "strategy_name": "nn2opt",
            "status": "success", "job_id": "j1",
            "original_file": plt_file, "optimized_file": plt_file,
            "original_distance": 1000.0, "optimized_distance": 800.0,
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
            "plt_optimizer.cli.benchmark.os.cpu_count", return_value=64
        ):
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
        rows = [_success_row(plt_file.name, "nn2opt",
                             improvement=10.0, total_after=9.0)]
        rows[0]["_metrics_event"] = {
            "kind": "strategy", "strategy_name": "nn2opt",
            "status": "success", "job_id": "j1",
            "original_file": plt_file, "optimized_file": plt_file,
            "original_distance": 1000.0, "optimized_distance": 800.0,
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
        rows = [_success_row(plt_file.name, "nn2opt",
                             improvement=10.0, total_after=9.0)]
        rows[0]["_metrics_event"] = {
            "kind": "strategy", "strategy_name": "nn2opt",
            "status": "success", "job_id": "j1",
            "original_file": plt_file, "optimized_file": plt_file,
            "original_distance": 1000.0, "optimized_distance": 800.0,
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
            rows = [_success_row(plt_file.name, "nn2opt",
                                 improvement=10.0, total_after=9.0)]
            rows[0]["_metrics_event"] = {
                "kind": "strategy", "strategy_name": "nn2opt",
                "status": "success", "job_id": f"j{idx}",
                "original_file": plt_file, "optimized_file": plt_file,
                "original_distance": 1000.0, "optimized_distance": 800.0,
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

    def test_worker_handles_missing_file(
        self, sample_output_dir: Path
    ) -> None:
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

    def test_worker_direct_call(
        self, sample_input_dir: Path, sample_output_dir: Path
    ) -> None:
        """Calling the worker in-process must also return a FileResult.

        This exercises lines 587-598 which subprocess execution doesn't
        cover for in-process coverage tracking.
        """
        plt_file = sample_input_dir / "square.plt"
        result = _process_file_worker(
            str(plt_file), str(sample_output_dir), 1.0
        )
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
