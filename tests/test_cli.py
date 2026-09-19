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
            "      margin: 0.25\n"
            "      clearance_padding: 0.125\n"
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
            "      margin: 0.25\n"
            "      clearance_padding: 0.125\n"
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
            "      margin: 0.25\n"
            "      clearance_padding: 0.125\n"
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
            "      margin: 0.25\n"
            "      clearance_padding: 0.125\n"
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
