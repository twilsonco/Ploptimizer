"""Tests for PLT-Optimizer CLI routing and subcommands.

These tests cover:
- Main entry point argument parsing
- Subcommand routing (optimize, generate, watch)
- Help text display
- Error handling for missing commands
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import patch

import pytest


class TestMainEntryPoint:
    """Tests for the main.py entry point routing."""

    def test_missing_subcommand_exits_with_help(self, capsys: pytest.CaptureFixture) -> None:
        """Test that running without a subcommand shows help and exits."""
        with patch.object(sys, "argv", ["plt-optimizer"]):
            with pytest.raises(SystemExit) as exc_info:
                from main import main

                main()

        # Should exit with error code (argparse returns 2 for usage errors)
        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        # argparse writes usage errors to stderr; subcommands appear in the usage line
        combined = captured.out + captured.err
        # Help should contain the subcommands
        assert "optimize" in combined
        assert "generate" in combined
        assert "watch" in combined

    def test_optimize_subcommand_routes_correctly(self) -> None:
        """Test that 'optimize' command routes to optimize.run()."""
        from main import main

        with patch.object(sys, "argv", ["plt-optimizer", "optimize", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

        # Help should display optimize-specific options
        assert exc_info.value.code == 0

    def test_generate_subcommand_routes_correctly(self) -> None:
        """Test that 'generate' command routes to generate.run()."""
        from main import main

        with patch.object(sys, "argv", ["plt-optimizer", "generate", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

        # Help should display generate-specific options
        assert exc_info.value.code == 0

    def test_watch_subcommand_routes_correctly(self) -> None:
        """Test that 'watch' command routes to watch.run()."""
        from main import main

        with patch.object(sys, "argv", ["plt-optimizer", "watch", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

        # Help should display watch-specific options
        assert exc_info.value.code == 0


class TestOptimizeSubcommand:
    """Tests for the optimize subcommand argument parsing."""

    def test_optimize_requires_input_file(self, capsys: pytest.CaptureFixture) -> None:
        """Test that optimize requires an input file argument."""
        from main import main

        with patch.object(sys, "argv", ["plt-optimizer", "optimize"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code != 0
        captured = capsys.readouterr()
        # argparse writes usage errors to stderr
        combined = (captured.out + captured.err).lower()
        # Should mention missing argument or show usage
        assert "error" in combined or "argument" in combined

    def test_optimize_accepts_input_file(self) -> None:
        """Test that optimize accepts a valid input file path."""
        from plt_optimizer.cli.optimize import setup_parser

        parser = argparse.ArgumentParser()
        setup_parser(parser)

        # Should not raise
        args = parser.parse_args(["input.plt"])
        assert args.input == Path("input.plt")

    def test_optimize_output_argument(self) -> None:
        """Test that optimize accepts -o/--output argument."""
        from plt_optimizer.cli.optimize import setup_parser

        parser = argparse.ArgumentParser()
        setup_parser(parser)

        args = parser.parse_args(["input.plt", "-o", "output.plt"])
        assert args.output == Path("output.plt")

    def test_optimize_fast_mode_flag(self) -> None:
        """Test that optimize accepts --fast-mode flag."""
        from plt_optimizer.cli.optimize import setup_parser

        parser = argparse.ArgumentParser()
        setup_parser(parser)

        args = parser.parse_args(["input.plt", "--fast-mode"])
        assert args.fast_mode is True


class TestGenerateSubcommand:
    """Tests for the generate subcommand argument parsing."""

    def test_generate_requires_spec_file(self, capsys: pytest.CaptureFixture) -> None:
        """Test that generate requires a specification file."""
        from main import main

        with patch.object(sys, "argv", ["plt-optimizer", "generate"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code != 0

    def test_generate_accepts_spec_file(self) -> None:
        """Test that generate accepts a spec file path."""
        from plt_optimizer.cli.generate import setup_parser

        parser = argparse.ArgumentParser()
        setup_parser(parser)

        args = parser.parse_args(["spec.yaml"])
        assert args.spec == Path("spec.yaml")

    def test_generate_output_is_directory(self) -> None:
        """-o parses as an output directory (default None -> spec parent)."""
        from plt_optimizer.cli.generate import setup_parser

        parser = argparse.ArgumentParser()
        setup_parser(parser)

        args = parser.parse_args(["spec.yaml", "-o", "/tmp/out"])
        assert args.output == Path("/tmp/out")

        args = parser.parse_args(["spec.yaml"])
        assert args.output is None

    def test_generate_no_plots_flag(self) -> None:
        """--no-plots defaults to False (plots on) and flips with the flag."""
        from plt_optimizer.cli.generate import setup_parser

        parser = argparse.ArgumentParser()
        setup_parser(parser)

        assert parser.parse_args(["spec.yaml"]).no_plots is False
        assert parser.parse_args(["spec.yaml", "--no-plots"]).no_plots is True

    def test_generate_default_plots_flag(self) -> None:
        """--default-plots is an opt-in flag, off by default."""
        from plt_optimizer.cli.generate import setup_parser

        parser = argparse.ArgumentParser()
        setup_parser(parser)

        assert parser.parse_args(["spec.yaml"]).default_plots is False
        assert parser.parse_args(["spec.yaml", "--default-plots"]).default_plots is True

    def test_sanitize_job_id(self) -> None:
        """Job names collapse whitespace and strip unsafe characters."""
        from plt_optimizer.cli.generate import _sanitize_job_id

        assert _sanitize_job_id("Complex Plant Signage - Batch 42") == (
            "Complex_Plant_Signage_-_Batch_42"
        )
        assert _sanitize_job_id("  Hello   World!  ") == "Hello_World"
        assert _sanitize_job_id("a/b\\c:d*e") == "abcde"
        assert _sanitize_job_id("keep.dots-and_9") == "keep.dots-and_9"
        assert _sanitize_job_id("!!!") == "job"
        assert _sanitize_job_id("") == "job"

    def test_generate_writes_per_cutter_dir_output(self, tmp_path: Path) -> None:
        """run() writes per-cutter PLTs under <output>/plt with job-id names."""
        from plt_optimizer.cli.generate import run

        spec_file = tmp_path / "cli_spec.yaml"
        spec_file.write_text(
            "job:\n"
            "  job_name: Cli Smoke Job\n"
            "  plates:\n"
            "    - id: p1\n"
            "      width: 24.0\n"
            "      height: 12.0\n"
            "      left_clearance: 0.25\n"
            "      top_clearance: 0.25\n"
            "  labels:\n"
            "    - id: l1\n"
            "      count: 1\n"
            "      width: 2.0\n"
            "      height: 1.0\n"
            "      content:\n"
            "        - text: Hello\n"
            "          height: 0.5\n"
        )

        out_dir = tmp_path / "out"

        class MockArgs:
            spec = spec_file
            output = out_dir
            verbose = False
            no_plots = True
            default_plots = False
            tools = Path("tools.json")
            fast_mode = False

        assert run(MockArgs()) == 0

        plt_files = sorted(p.name for p in (out_dir / "plt").iterdir())
        assert plt_files, "no per-cutter PLT files written"
        assert all(name.endswith("_Cli_Smoke_Job.plt") for name in plt_files)
        assert any("_text_" in name for name in plt_files)
        assert any("_bh_" in name for name in plt_files)
        # --no-plots: no PDF previews.
        assert not (out_dir / "pdf").exists() or not list((out_dir / "pdf").iterdir())


class TestWatchSubcommand:
    """Tests for the watch subcommand argument parsing."""

    def test_watch_requires_watch_dir(self, capsys: pytest.CaptureFixture) -> None:
        """Test that watch requires --watch-dir."""
        from main import main

        with patch.object(sys, "argv", ["plt-optimizer", "watch"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code != 0
        captured = capsys.readouterr()
        # argparse writes usage errors to stderr
        combined = (captured.out + captured.err).lower()
        # Should mention missing required argument
        assert "error" in combined or "--watch-dir" in combined

    def test_watch_accepts_watch_dir(self) -> None:
        """Test that watch accepts --watch-dir argument."""
        from plt_optimizer.cli.watch import setup_parser

        parser = argparse.ArgumentParser()
        setup_parser(parser)

        args = parser.parse_args(["--watch-dir", "/some/path"])
        assert args.watch_dir == Path("/some/path")

    def test_watch_accepts_all_arguments(self) -> None:
        """Test parsing of all valid watch arguments."""
        from plt_optimizer.cli.watch import setup_parser

        parser = argparse.ArgumentParser()
        setup_parser(parser)

        args = parser.parse_args(
            [
                "--watch-dir",
                "/watch",
                "--output-dir",
                "/output",
                "--log-dir",
                "/logs",
                "--processed-dir",
                "/archive",
                "--fast-mode",
                "--debug-save-files",
            ]
        )

        assert args.watch_dir == Path("/watch")
        assert args.output_dir == Path("/output")
        assert args.log_dir == Path("/logs")
        assert args.processed_dir == Path("/archive")
        assert args.fast_mode is True
        assert args.debug_save_files is True


class TestCLIIntegration:
    """Integration tests for CLI routing."""

    def test_generate_stub_runs(self, tmp_path: Path) -> None:
        """Test that generate stub executes and returns success."""
        from plt_optimizer.cli.generate import run

        spec_file = tmp_path / "test_spec.yaml"
        spec_file.write_text(
            "job:\n"
            "  job_name: Stub Test\n"
            "  plates:\n"
            "    - id: p1\n"
            "      width: 24.0\n"
            "      height: 12.0\n"
            "      left_clearance: 0.25\n"
            "      top_clearance: 0.25\n"
            "  labels:\n"
            "    - id: l1\n"
            "      count: 1\n"
            "      width: 2.0\n"
            "      height: 1.0\n"
            "      content:\n"
            "        - text: Hello\n"
            "          height: 0.5\n"
        )

        class MockArgs:
            spec = spec_file
            output = None
            verbose = False
            no_plots = True
            default_plots = False
            tools = Path("tools.json")
            fast_mode = False

        result = run(MockArgs())
        assert result == 0

    def test_generate_expands_replacement_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A replacement_text_file template expands via the generate CLI.

        Per the schema contract, expand_job_spec() must run between
        parse_yaml() and resolve_job_spec(); a 2-line data file against a
        single template label must yield 2 static labels (not an
        unexpanded template reaching the resolver).
        """
        from plt_optimizer.cli.generate import run

        (tmp_path / "r.txt").write_text("ALPHA\nBETA\n", encoding="utf-8")
        spec_file = tmp_path / "repl_spec.yaml"
        spec_file.write_text(
            "job:\n"
            "  job_name: Repl Cli Job\n"
            "  text_height: 0.3\n"
            "  plates:\n"
            "    - id: p1\n"
            "      width: 24.0\n"
            "      height: 12.0\n"
            "      left_clearance: 0.25\n"
            "      top_clearance: 0.25\n"
            "  labels:\n"
            "    - id: tmpl\n"
            "      width: 2.0\n"
            "      height: 1.0\n"
            "      replacement_text_file: r.txt\n"
            "      content:\n"
            "        - text: PLACEHOLDER\n"
            "          height: 0.5\n",
            encoding="utf-8",
        )

        out_dir = tmp_path / "out"

        class MockArgs:
            spec = spec_file
            output = out_dir
            verbose = False
            no_plots = True
            default_plots = False
            tools = Path("tools.json")
            fast_mode = False

        assert run(MockArgs()) == 0

        captured = capsys.readouterr()
        # Post-expansion count: 2 instances (tmpl_0000, tmpl_0001), not 1 template.
        assert "2 unique labels" in captured.out

        plt_files = list((out_dir / "plt").glob("*.plt"))
        assert plt_files, "no per-cutter PLT files written"

    def test_generate_missing_replacement_file_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A template pointing at a missing data file aborts with exit code 1."""
        from plt_optimizer.cli.generate import run

        spec_file = tmp_path / "repl_spec.yaml"
        spec_file.write_text(
            "job:\n"
            "  job_name: Missing Repl Job\n"
            "  plates:\n"
            "    - id: p1\n"
            "      width: 24.0\n"
            "      height: 12.0\n"
            "      left_clearance: 0.25\n"
            "      top_clearance: 0.25\n"
            "  labels:\n"
            "    - id: tmpl\n"
            "      width: 2.0\n"
            "      height: 1.0\n"
            "      replacement_text_file: missing.txt\n"
            "      content:\n"
            "        - text: PLACEHOLDER\n",
            encoding="utf-8",
        )

        class MockArgs:
            spec = spec_file
            output = tmp_path / "out"
            verbose = False
            no_plots = True
            default_plots = False
            tools = Path("tools.json")
            fast_mode = False

        assert run(MockArgs()) == 1
        captured = capsys.readouterr()
        assert "replacement text file" in captured.err.lower()


def _zigzag_path(base_y: int) -> str:
    """Build a many-facet zigzag stroke (classified as TEXT by the Profiler).

    Args:
        base_y: Y coordinate of the path's baseline in plot units.

    Returns:
        HPGL fragment with one PU followed by 40 tiny PD segments.
    """
    pts = ";".join(f"PD{x * 20},{base_y + (15 if x % 2 else 0)}" for x in range(1, 41))
    return f"PU0,{base_y};{pts};"


def _write_text_plt(directory: Path, name: str = "text.plt", paths: int = 2) -> Path:
    """Write a small hand-made text-like PLT (zigzags) into ``directory``.

    Args:
        directory: Directory to write into (always a tmp_path subtree).
        name: File name for the PLT.
        paths: Number of zigzag stroke paths to emit.

    Returns:
        Path to the written PLT file.
    """
    body = "".join(_zigzag_path(i * 100) for i in range(paths))
    plt_path = directory / name
    plt_path.write_text(f"IN;SP1;{body}PU0,0;SP;IN;", encoding="utf-8")
    return plt_path


def _write_structural_plt(directory: Path, name: str = "grid.plt") -> Path:
    """Write a structural PLT (five long single-segment lines) into ``directory``.

    Args:
        directory: Directory to write into (always a tmp_path subtree).
        name: File name for the PLT.

    Returns:
        Path to the written PLT file.
    """
    lines = "".join(f"PU0,{y};PD1000,{y};" for y in range(0, 5000, 1000))
    plt_path = directory / name
    plt_path.write_text(f"IN;SP1;{lines}PU0,0;SP;IN;", encoding="utf-8")
    return plt_path


def _read_metrics_rows(csv_path: Path) -> List[Dict[str, str]]:
    """Read job_metrics.csv rows written by the optimize CLI.

    Args:
        csv_path: Path to the CSV metrics file.

    Returns:
        List of row dicts (header-keyed), excluding the header row.
    """
    with open(csv_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class TestOptimizeRun:
    """End-to-end tests for plt_optimizer.cli.optimize.run().

    Every test passes an explicit ``log_dir`` under tmp_path (or chdirs into
    tmp_path) so nothing is ever written into ./logs_optimize or the repo.
    The module-level logger singletons are isolated per test so the CSV
    metrics assertions read the tmp file created by that test's run().
    """

    @pytest.fixture(autouse=True)
    def isolated_logging(self) -> Iterator[None]:
        """Reset logging singletons/handlers so run() logs into tmp paths only.

        Yields:
            Nothing; restores logger handlers, level, and module singletons
            after each test.
        """
        import plt_optimizer.utils.logging as logging_module

        logger = logging.getLogger("plt_optimizer")
        saved_text = logging_module._text_logger
        saved_csv = logging_module._csv_logger
        saved_level = logger.level
        saved_handlers = list(logger.handlers)
        for handler in saved_handlers:
            logger.removeHandler(handler)
        logging_module._text_logger = None
        logging_module._csv_logger = None
        try:
            yield
        finally:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
            for handler in saved_handlers:
                logger.addHandler(handler)
            logger.setLevel(saved_level)
            logging_module._text_logger = saved_text
            logging_module._csv_logger = saved_csv

    @staticmethod
    def _args(
        input_path: Path,
        output: Optional[Path] = None,
        log_dir: Optional[Path] = None,
        fast_mode: bool = True,
        verbose: bool = False,
    ) -> argparse.Namespace:
        """Build an argparse.Namespace matching optimize's expected attributes.

        Args:
            input_path: Value for the positional input argument.
            output: Value for -o/--output (None triggers default derivation).
            log_dir: Value for --log-dir (None triggers the ./logs_optimize
                default; tests pairing None with it must chdir to tmp_path).
            fast_mode: Value for --fast-mode.
            verbose: Value for -v/--verbose.

        Returns:
            Namespace consumable by optimize.run().
        """
        return argparse.Namespace(
            input=input_path,
            output=output,
            log_dir=log_dir,
            fast_mode=fast_mode,
            verbose=verbose,
        )

    def test_missing_input_file_returns_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A nonexistent input path exits 1 with an error on stderr."""
        from plt_optimizer.cli.optimize import run

        missing = tmp_path / "nope.plt"
        args = self._args(missing, log_dir=tmp_path / "logs")

        assert run(args) == 1
        captured = capsys.readouterr()
        assert "does not exist" in captured.err
        assert not (tmp_path / "logs").exists()

    def test_directory_input_returns_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """An input path that is a directory exits 1 with an error on stderr."""
        from plt_optimizer.cli.optimize import run

        args = self._args(tmp_path, log_dir=tmp_path / "logs")

        assert run(args) == 1
        captured = capsys.readouterr()
        assert "not a file" in captured.err

    def test_log_dir_permission_error_returns_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A PermissionError while creating the log dir exits 1 with stderr."""
        from plt_optimizer.cli.optimize import run

        plt_file = _write_text_plt(tmp_path)
        args = self._args(plt_file, log_dir=tmp_path / "locked_logs")

        with patch.object(Path, "mkdir", autospec=True, side_effect=PermissionError("denied")):
            assert run(args) == 1
        captured = capsys.readouterr()
        assert "Cannot create log directory" in captured.err

    def test_default_log_dir_created_under_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With log_dir=None, logs land in ./logs_optimize relative to CWD."""
        from plt_optimizer.cli.optimize import run

        plt_file = _write_text_plt(tmp_path)
        monkeypatch.chdir(tmp_path)
        args = self._args(plt_file, log_dir=None)

        assert run(args) == 0
        log_dir = tmp_path / "logs_optimize"
        assert (log_dir / "optimizer.log").is_file()
        assert (log_dir / "job_metrics.csv").is_file()

    def test_fast_mode_success_default_output_path(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Fast-mode run writes <stem>_optimized.plt next to input and logs success."""
        from plt_optimizer.cli.optimize import run

        plt_file = _write_text_plt(tmp_path)
        log_dir = tmp_path / "logs"
        args = self._args(plt_file, output=None, log_dir=log_dir)

        assert run(args) == 0

        out_file = tmp_path / "text_optimized.plt"
        assert out_file.is_file()
        captured = capsys.readouterr()
        assert "Optimized: text.plt -> text_optimized.plt" in captured.out

        rows = _read_metrics_rows(log_dir / "job_metrics.csv")
        assert len(rows) == 1
        assert rows[0]["status"] == "success"
        assert rows[0]["method"] == "NearestNeighbor + 2-Opt (Fast Mode)"
        assert float(rows[0]["percent_improvement"].rstrip("%")) == pytest.approx(87.6, abs=0.5)

    def test_explicit_output_verbose_structural_pipeline(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Verbose fast-mode on a structural doc: DEBUG level, no stdout summary."""
        from plt_optimizer.cli.optimize import run

        plt_file = _write_structural_plt(tmp_path)
        out_file = tmp_path / "nested" / "out.plt"
        log_dir = tmp_path / "logs"
        args = self._args(plt_file, output=out_file, log_dir=log_dir, verbose=True)

        assert run(args) == 0
        assert out_file.is_file()
        # verbose=True raises the shared text logger to DEBUG.
        assert logging.getLogger("plt_optimizer").level == logging.DEBUG
        # The stdout summary is suppressed in verbose mode.
        captured = capsys.readouterr()
        assert "Optimized:" not in captured.out

        rows = _read_metrics_rows(log_dir / "job_metrics.csv")
        assert len(rows) == 1
        assert rows[0]["status"] == "success"
        assert rows[0]["optimized_file"] == "out"

    def test_ensemble_branch_with_none_improvement(
        self, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default (non-fast) mode builds ParallelEnsembleStrategy and logs all benchmarks.

        OptimizerEngine is swapped for a fake returning a hand-built
        ParallelEnsembleOptimizationResult whose benchmarks mix a None and a
        non-None improvement_percent, exercising both formatting sub-branches
        of the benchmark logging and notes building without spawning workers.
        """
        from plt_optimizer.cli.optimize import run
        from plt_optimizer.core.optimizer import (
            BlockTraverseState,
            OptimizationResult,
            ParallelEnsembleOptimizationResult,
            ParallelEnsembleStrategy,
            StrategyBenchmarkResult,
        )

        captured_strategies: List[Any] = []

        def _fake_engine_factory(strategy: Any) -> Any:
            class _FakeEngine:
                def __init__(self, strategy: Any) -> None:
                    captured_strategies.append(strategy)

                def optimize(self, blocks: List[Any]) -> Any:
                    traverse = tuple(
                        BlockTraverseState(
                            block_id=block.block_id,
                            reversed=False,
                            entrance=block.entrance.as_tuple(),
                            exit=block.exit.as_tuple(),
                        )
                        for block in blocks
                    )
                    inner = OptimizationResult(
                        traverse_order=traverse,
                        connections=(),
                        total_travel_distance=42.0,
                        initial_position=None,
                    )
                    bench_none = StrategyBenchmarkResult(
                        strategy_name="NoOp (Baseline)",
                        result=inner,
                        execution_time_seconds=0.01,
                        improvement_percent=None,
                    )
                    bench_val = StrategyBenchmarkResult(
                        strategy_name="Genetic Algorithm",
                        result=inner,
                        execution_time_seconds=0.02,
                        improvement_percent=25.0,
                    )
                    return ParallelEnsembleOptimizationResult(
                        result=inner,
                        winner_name="Genetic Algorithm",
                        all_benchmarks=(bench_none, bench_val),
                    )

            return _FakeEngine(strategy)

        monkeypatch.setattr("plt_optimizer.cli.optimize.OptimizerEngine", _fake_engine_factory)

        plt_file = _write_text_plt(tmp_path)
        log_dir = tmp_path / "logs"
        args = self._args(plt_file, log_dir=log_dir, fast_mode=False)

        assert run(args) == 0
        assert (tmp_path / "text_optimized.plt").is_file()
        assert len(captured_strategies) == 1
        assert isinstance(captured_strategies[0], ParallelEnsembleStrategy)

        captured = capsys.readouterr()
        assert "Optimized: text.plt -> text_optimized.plt (saved 94.8%)" in captured.out

        rows = _read_metrics_rows(log_dir / "job_metrics.csv")
        assert len(rows) == 1
        assert rows[0]["method"] == "Genetic Algorithm"
        # Notes join both benchmarks: None renders as N/A, 25.0 as a percent.
        assert "NoOp (Baseline): 42.000 (improvement=N/A)" in rows[0]["notes"]
        assert "Genetic Algorithm: 42.000 (improvement=25.00%)" in rows[0]["notes"]

    def test_benchmark_log_includes_no_baseline_line(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A benchmark with None improvement_percent logs 'no baseline comparison'."""
        from plt_optimizer.cli.optimize import run
        from plt_optimizer.core.optimizer import (
            BlockTraverseState,
            OptimizationResult,
            ParallelEnsembleOptimizationResult,
            StrategyBenchmarkResult,
        )

        def _fake_engine_factory(strategy: Any) -> Any:
            class _FakeEngine:
                def __init__(self, strategy: Any) -> None:
                    pass

                def optimize(self, blocks: List[Any]) -> Any:
                    traverse = tuple(
                        BlockTraverseState(
                            block_id=block.block_id,
                            reversed=False,
                            entrance=block.entrance.as_tuple(),
                            exit=block.exit.as_tuple(),
                        )
                        for block in blocks
                    )
                    inner = OptimizationResult(
                        traverse_order=traverse,
                        connections=(),
                        total_travel_distance=10.0,
                        initial_position=None,
                    )
                    bench = StrategyBenchmarkResult(
                        strategy_name="Insertion Heuristic",
                        result=inner,
                        execution_time_seconds=0.5,
                        improvement_percent=None,
                    )
                    return ParallelEnsembleOptimizationResult(
                        result=inner,
                        winner_name="Insertion Heuristic",
                        all_benchmarks=(bench,),
                    )

            return _FakeEngine(strategy)

        monkeypatch.setattr("plt_optimizer.cli.optimize.OptimizerEngine", _fake_engine_factory)
        caplog.set_level(logging.DEBUG, logger="plt_optimizer")

        plt_file = _write_text_plt(tmp_path)
        args = self._args(plt_file, log_dir=tmp_path / "logs", fast_mode=False)

        assert run(args) == 0
        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert "Strategy benchmark results:" in combined
        assert "no baseline comparison" in combined

    def test_empty_blocks_returns_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the Chunker yields no blocks, run() warns and exits 1."""
        from plt_optimizer.cli.optimize import run

        class _EmptyChunker:
            def __init__(self, config: Any = None) -> None:
                pass

            def chunk(self, *args: Any, **kwargs: Any) -> List[Any]:
                return []

        monkeypatch.setattr("plt_optimizer.cli.optimize.Chunker", _EmptyChunker)

        plt_file = _write_text_plt(tmp_path)
        args = self._args(plt_file, log_dir=tmp_path / "logs")

        assert run(args) == 1
        assert not (tmp_path / "text_optimized.plt").exists()

    def test_parse_failure_logs_failed_metrics(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A parse-time exception exits 1 and records a 'failed' CSV row."""
        from plt_optimizer.cli.optimize import run

        class _BoomParser:
            def parse_file(self, path: Path) -> Any:
                raise RuntimeError("boom")

        monkeypatch.setattr("plt_optimizer.cli.optimize.PLTParser", _BoomParser)

        plt_file = _write_text_plt(tmp_path)
        log_dir = tmp_path / "logs"
        args = self._args(plt_file, log_dir=log_dir)

        assert run(args) == 1
        rows = _read_metrics_rows(log_dir / "job_metrics.csv")
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert "boom" in rows[0]["notes"]
        assert rows[0]["optimized_file"] == ""

    def test_parse_failure_verbose_logs_traceback(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verbose mode dumps the full traceback when the pipeline raises."""
        from plt_optimizer.cli.optimize import run

        class _BoomParser:
            def parse_file(self, path: Path) -> Any:
                raise RuntimeError("kaboom")

        monkeypatch.setattr("plt_optimizer.cli.optimize.PLTParser", _BoomParser)
        caplog.set_level(logging.DEBUG, logger="plt_optimizer")

        plt_file = _write_text_plt(tmp_path)
        args = self._args(plt_file, log_dir=tmp_path / "logs", verbose=True)

        assert run(args) == 1
        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert "Optimization failed: kaboom" in combined
        assert "Traceback (most recent call last)" in combined

    def test_zero_original_distance_reports_zero_improvement(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A single-path doc has zero rapid travel; improvement prints as 0.0%."""
        from plt_optimizer.cli.optimize import run

        plt_file = _write_text_plt(tmp_path, paths=1)
        log_dir = tmp_path / "logs"
        args = self._args(plt_file, log_dir=log_dir)

        assert run(args) == 0
        captured = capsys.readouterr()
        assert "saved 0.0%" in captured.out

        rows = _read_metrics_rows(log_dir / "job_metrics.csv")
        assert len(rows) == 1
        assert rows[0]["status"] == "success"
        assert float(rows[0]["percent_improvement"].rstrip("%")) == pytest.approx(0.0)


class TestGenerateRun:
    """End-to-end tests for plt_optimizer.cli.generate.run().

    ``run()`` hardcodes ``./logs_generate`` as its log directory, so every
    test chdirs into ``tmp_path`` (via monkeypatch) and asserts the repo's
    ``logs_generate/`` stays untouched. The module-level logging singletons
    are reset per test (same isolation pattern as :class:`TestOptimizeRun`)
    so logger handlers never keep file handles open in the repo.
    """

    @pytest.fixture(autouse=True)
    def isolated_run_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        """Confine every run() side effect to tmp_path and reset loggers.

        Args:
            tmp_path: Pytest-provided temporary directory (becomes cwd).
            monkeypatch: Pytest monkeypatch fixture.

        Yields:
            Nothing; restores logger handlers, level, and module singletons
            after each test.
        """
        import plt_optimizer.utils.logging as logging_module

        monkeypatch.chdir(tmp_path)
        logger = logging.getLogger("plt_optimizer")
        saved_text = logging_module._text_logger
        saved_csv = logging_module._csv_logger
        saved_level = logger.level
        saved_handlers = list(logger.handlers)
        for handler in saved_handlers:
            logger.removeHandler(handler)
        logging_module._text_logger = None
        logging_module._csv_logger = None
        try:
            yield
        finally:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
            for handler in saved_handlers:
                logger.addHandler(handler)
            logger.setLevel(saved_level)
            logging_module._text_logger = saved_text
            logging_module._csv_logger = saved_csv

    @staticmethod
    def _args(
        spec: Path,
        output: Optional[Path] = None,
        verbose: bool = False,
        no_plots: bool = True,
        default_plots: bool = False,
        tools: Optional[Path] = None,
        fast_mode: bool = False,
    ) -> argparse.Namespace:
        """Build an argparse.Namespace matching generate's expected attributes.

        Args:
            spec: Value for the positional spec argument.
            output: Value for -o/--output (None triggers the spec-parent
                default).
            verbose: Value for -v/--verbose.
            no_plots: Value for --no-plots.
            default_plots: Value for --default-plots.
            tools: Value for --tools (None selects the tools.json default).
            fast_mode: Value for --fast-mode.

        Returns:
            Namespace consumable by generate.run().
        """
        return argparse.Namespace(
            spec=spec,
            output=output,
            verbose=verbose,
            no_plots=no_plots,
            default_plots=default_plots,
            tools=tools if tools is not None else Path("tools.json"),
            fast_mode=fast_mode,
        )

    @staticmethod
    def _write_spec(directory: Path, name: str = "spec.yaml") -> Path:
        """Write a minimal valid label-list job spec into ``directory``.

        Args:
            directory: Directory to write into.
            name: File name for the YAML spec.

        Returns:
            Path to the written spec file.
        """
        spec_file = directory / name
        spec_file.write_text(
            "job:\n"
            "  job_name: Generate Run Job\n"
            "  plates:\n"
            "    - id: p1\n"
            "      width: 24.0\n"
            "      height: 12.0\n"
            "      left_clearance: 0.25\n"
            "      top_clearance: 0.25\n"
            "  labels:\n"
            "    - id: l1\n"
            "      count: 1\n"
            "      width: 2.0\n"
            "      height: 1.0\n"
            "      content:\n"
            "        - text: Hello\n"
            "          height: 0.5\n",
            encoding="utf-8",
        )
        return spec_file

    # ------------------------------------------------------------------
    # _load_cutter_inventory (pure function)
    # ------------------------------------------------------------------

    def test_inventory_missing_file_returns_nones(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A missing tools.json short-circuits to ideal cutters silently."""
        from plt_optimizer.cli.generate import _load_cutter_inventory

        inventory, boundary = _load_cutter_inventory(tmp_path / "nope.json")
        assert inventory is None
        assert boundary is None
        assert capsys.readouterr().out == ""

    def test_inventory_invalid_json_returns_nones(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Malformed JSON is swallowed into (None, None) without printing."""
        from plt_optimizer.cli.generate import _load_cutter_inventory

        bad = tmp_path / "tools.json"
        bad.write_text("{not json", encoding="utf-8")

        inventory, boundary = _load_cutter_inventory(bad)
        assert inventory is None
        assert boundary is None
        assert capsys.readouterr().out == ""

    def test_inventory_open_oserror_returns_nones(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An OSError while reading the file also degrades to (None, None)."""
        import builtins

        from plt_optimizer.cli.generate import _load_cutter_inventory

        tools = tmp_path / "tools.json"
        tools.write_text('{"available_cutters": [0.03]}', encoding="utf-8")

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise OSError("permission denied")

        monkeypatch.setattr(builtins, "open", _boom)
        inventory, boundary = _load_cutter_inventory(tools)
        assert inventory is None
        assert boundary is None

    def test_inventory_both_keys_returns_values_and_prints(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A full inventory returns both values and prints both notices."""
        from plt_optimizer.cli.generate import _load_cutter_inventory

        tools = tmp_path / "tools.json"
        tools.write_text(
            '{"available_cutters": [0.03, 0.06], "boundary_hole_cutter_size": 0.015}',
            encoding="utf-8",
        )

        inventory, boundary = _load_cutter_inventory(tools)
        assert inventory is not None
        assert inventory == pytest.approx([0.03, 0.06])
        assert boundary == pytest.approx(0.015)
        captured = capsys.readouterr()
        assert "Loaded cutter inventory" in captured.out
        assert "Loaded boundary/hole cutter size: 0.015" in captured.out

    def test_inventory_keys_absent_returns_nones_without_prints(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A JSON file lacking both keys yields (None, None) and stays quiet."""
        from plt_optimizer.cli.generate import _load_cutter_inventory

        tools = tmp_path / "tools.json"
        tools.write_text('{"description": "empty shop"}', encoding="utf-8")

        inventory, boundary = _load_cutter_inventory(tools)
        assert inventory is None
        assert boundary is None
        assert capsys.readouterr().out == ""

    # ------------------------------------------------------------------
    # run(): validation and logging setup
    # ------------------------------------------------------------------

    def test_missing_spec_returns_one(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """A nonexistent spec path aborts with exit code 1 before logging."""
        from plt_optimizer.cli.generate import run

        args = self._args(tmp_path / "missing.yaml")
        assert run(args) == 1
        assert "does not exist" in capsys.readouterr().err

    def test_directory_spec_returns_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A directory passed as the spec aborts with exit code 1."""
        from plt_optimizer.cli.generate import run

        args = self._args(tmp_path)
        assert run(args) == 1
        assert "not a file" in capsys.readouterr().err

    def test_log_dir_permission_error_returns_one(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A PermissionError creating ./logs_generate aborts before parsing."""
        from plt_optimizer.cli.generate import run

        spec_file = self._write_spec(tmp_path)
        original_mkdir = Path.mkdir

        def _guarded_mkdir(self_path: Path, *args: Any, **kwargs: Any) -> None:
            if self_path.name == "logs_generate":
                raise PermissionError("denied")
            original_mkdir(self_path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", _guarded_mkdir)
        args = self._args(spec_file)

        assert run(args) == 1
        assert "Cannot create log directory" in capsys.readouterr().err

    def test_verbose_sets_debug_level(self, tmp_path: Path) -> None:
        """-v raises the shared plt_optimizer logger to DEBUG for the run."""
        from plt_optimizer.cli.generate import run

        spec_file = self._write_spec(tmp_path)
        args = self._args(spec_file, verbose=True)

        assert run(args) == 0
        assert logging.getLogger("plt_optimizer").level == logging.DEBUG

    # ------------------------------------------------------------------
    # run(): parse-stage error handling
    # ------------------------------------------------------------------

    def test_invalid_spec_returns_one(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """A spec failing Pydantic validation aborts with a parse error."""
        from plt_optimizer.cli.generate import run

        spec_file = tmp_path / "bad.yaml"
        spec_file.write_text(
            "job:\n  job_name: Bad\n  count: -1\n  content:\n    - text: Hi\n",
            encoding="utf-8",
        )
        args = self._args(spec_file)

        assert run(args) == 1
        assert "Error parsing specification" in capsys.readouterr().err

    def test_root_level_job_without_plates_prints_zero_counts(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Root-level content jobs exercise the labels/plates None branches.

        Pattern 2 (root-level ``content`` + ``count``, no ``labels`` key and
        no ``plates`` key) must print zero counts and still export onto the
        default plate.
        """
        from plt_optimizer.cli.generate import run

        spec_file = tmp_path / "root.yaml"
        spec_file.write_text(
            "job:\n"
            "  job_name: Root Job\n"
            "  width: 2.0\n"
            "  height: 1.0\n"
            "  count: 2\n"
            "  content:\n"
            "    - text: Root\n"
            "      text_height: 0.4\n",
            encoding="utf-8",
        )
        args = self._args(spec_file, output=tmp_path / "out")

        assert run(args) == 0
        captured = capsys.readouterr()
        assert "0 plates" in captured.out
        assert "0 unique labels" in captured.out
        assert list((tmp_path / "out" / "plt").glob("*.plt"))

    # ------------------------------------------------------------------
    # run(): export-stage error handling
    # ------------------------------------------------------------------

    def test_label_render_error_aborts_job(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A LabelRenderError from export is logged and aborts with exit 1."""
        from plt_optimizer.cli.generate import run
        from plt_optimizer.generate.label_renderer import LabelRenderError

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise LabelRenderError("boom")

        monkeypatch.setattr("plt_optimizer.generate.vectorize.export_per_cutter_plts", _boom)
        caplog.set_level(logging.DEBUG, logger="plt_optimizer")
        spec_file = self._write_spec(tmp_path)
        args = self._args(spec_file)

        assert run(args) == 1
        assert "Error: boom" in capsys.readouterr().err
        assert "Generation aborted: boom" in caplog.text

    def test_layout_fit_error_aborts_job(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A LayoutFitError from export is logged and aborts with exit 1."""
        from plt_optimizer.cli.generate import run
        from plt_optimizer.generate.layout import LayoutFitError

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise LayoutFitError("no room")

        monkeypatch.setattr("plt_optimizer.generate.vectorize.export_per_cutter_plts", _boom)
        caplog.set_level(logging.DEBUG, logger="plt_optimizer")
        spec_file = self._write_spec(tmp_path)
        args = self._args(spec_file)

        assert run(args) == 1
        assert "Error: no room" in capsys.readouterr().err
        assert "Layout failed: no room" in caplog.text

    def test_os_error_during_export_aborts_job(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An OSError during export (e.g. disk full) aborts with exit 1."""
        from plt_optimizer.cli.generate import run

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("plt_optimizer.generate.vectorize.export_per_cutter_plts", _boom)
        caplog.set_level(logging.DEBUG, logger="plt_optimizer")
        spec_file = self._write_spec(tmp_path)
        args = self._args(spec_file)

        assert run(args) == 1
        assert "Error: disk full" in capsys.readouterr().err
        assert "Generation failed: disk full" in caplog.text

    # ------------------------------------------------------------------
    # run(): success reporting
    # ------------------------------------------------------------------

    def test_default_output_dir_is_spec_parent(self, tmp_path: Path) -> None:
        """With -o omitted, PLTs land in plt/ next to the spec file.

        Also asserts the repo's ``logs_generate/`` is untouched: run() must
        log into ./logs_generate under the (tmp) cwd instead.
        """
        from plt_optimizer.cli.generate import run

        repo_log = Path(__file__).resolve().parents[1] / "logs_generate" / "generate.log"
        repo_before = repo_log.stat().st_mtime_ns if repo_log.exists() else None

        spec_file = self._write_spec(tmp_path)
        args = self._args(spec_file)

        assert run(args) == 0
        plt_files = list((tmp_path / "plt").glob("*.plt"))
        assert plt_files, "no per-cutter PLT files written next to the spec"
        assert all(p.name.endswith("_Generate_Run_Job.plt") for p in plt_files)
        # run() logs into ./logs_generate under cwd (tmp_path), never the repo.
        assert (tmp_path / "logs_generate" / "generate.log").is_file()

        repo_after = repo_log.stat().st_mtime_ns if repo_log.exists() else None
        assert repo_after == repo_before

    def test_pdf_and_default_plot_paths_are_printed(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-empty pdf/default-pdf path lists are echoed to stdout."""
        from plt_optimizer.cli.generate import run
        from plt_optimizer.generate.vectorize import PerCutterExport

        plt_path = tmp_path / "plt" / "01_text_0.060_job.plt"
        pdf_path = tmp_path / "pdf" / "01_text_0.060_job.pdf"
        default_pdf = tmp_path / "pdf" / "01_all_job_default.pdf"

        def _fake_export(*args: Any, **kwargs: Any) -> PerCutterExport:
            return PerCutterExport(
                plt_paths=[plt_path],
                pdf_paths=[pdf_path],
                default_pdf_paths=[default_pdf],
            )

        monkeypatch.setattr("plt_optimizer.generate.vectorize.export_per_cutter_plts", _fake_export)
        spec_file = self._write_spec(tmp_path)
        args = self._args(spec_file)

        assert run(args) == 0
        captured = capsys.readouterr()
        assert "Generated 1 per-cutter PLT file(s):" in captured.out
        assert f"  {plt_path}" in captured.out
        assert "Generated 1 PDF preview(s):" in captured.out
        assert f"  {pdf_path}" in captured.out
        assert "Generated 1 default plot(s):" in captured.out
        assert f"  {default_pdf}" in captured.out

    def test_layout_field_forwarded_to_export(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The job-level layout enum must reach export_per_cutter_plts."""
        from plt_optimizer.cli.generate import run
        from plt_optimizer.generate.schema import LayoutMode
        from plt_optimizer.generate.vectorize import PerCutterExport

        captured_kwargs: dict[str, Any] = {}

        def _capture_export(*args: Any, **kwargs: Any) -> PerCutterExport:
            captured_kwargs.update(kwargs)
            return PerCutterExport(plt_paths=[], pdf_paths=[], default_pdf_paths=[])

        monkeypatch.setattr(
            "plt_optimizer.generate.vectorize.export_per_cutter_plts", _capture_export
        )
        spec_file = self._write_spec(tmp_path, name="rows_spec.yaml")
        spec_file.write_text(
            spec_file.read_text(encoding="utf-8").replace(
                "  job_name: Generate Run Job\n",
                "  job_name: Generate Run Job\n  layout: rows\n",
            ),
            encoding="utf-8",
        )

        assert run(self._args(spec_file)) == 0
        assert captured_kwargs["layout"] is LayoutMode.ROWS


class TestHelpDisplay:
    """Tests for help text display."""

    def test_main_help_shows_subcommands(self, capsys: pytest.CaptureFixture) -> None:
        """Test that main --help shows all subcommands."""
        from main import main

        with patch.object(sys, "argv", ["plt-optimizer", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "optimize" in captured.out
        assert "generate" in captured.out
        assert "watch" in captured.out

    def test_optimize_help_shows_options(self, capsys: pytest.CaptureFixture) -> None:
        """Test that optimize --help shows all options."""
        from main import main

        with patch.object(sys, "argv", ["plt-optimizer", "optimize", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        # Should show optimize-specific options
        assert "--fast-mode" in captured.out or "-o" in captured.out

    def test_watch_help_shows_options(self, capsys: pytest.CaptureFixture) -> None:
        """Test that watch --help shows all options."""
        from main import main

        with patch.object(sys, "argv", ["plt-optimizer", "watch", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        # Should show watch-specific options
        assert "--watch-dir" in captured.out


class TestNoMatplotlibImportPath:
    """Python 3.8 / Windows 7 guard (AGENTS.md section 7).

    The watch/optimize CLI path must stay importable without matplotlib.
    These tests run in a subprocess with a ``sys.meta_path`` blocker that
    raises on any matplotlib import, simulating the Win7 environment where
    matplotlib cannot be installed.
    """

    _BLOCKER_CODE = """
import sys

class MatplotlibBlocker:
    def find_module(self, fullname, path=None):
        if "matplotlib" in fullname:
            raise ImportError("Simulated unavailability")
        return None

sys.meta_path.insert(0, MatplotlibBlocker())

import main  # noqa: F401
sys.argv = ["plt-optimizer", "watch", "--help"]
try:
    main.main()
except SystemExit as exc:
    sys.exit(exc.code if exc.code is not None else 0)
"""

    def test_import_main_and_watch_help_without_matplotlib(self) -> None:
        """``import main`` + ``watch --help`` succeed with matplotlib blocked."""
        import subprocess

        project_root = str(Path(__file__).resolve().parent.parent)
        result = subprocess.run(
            [sys.executable, "-c", self._BLOCKER_CODE],
            cwd=project_root,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"watch path pulled in matplotlib or failed: rc={result.returncode} "
            f"stderr={result.stderr}"
        )
        assert "--watch-dir" in result.stdout
