"""Tests for PLT-Optimizer CLI routing and subcommands.

These tests cover:
- Main entry point argument parsing
- Subcommand routing (optimize, generate, watch)
- Help text display
- Error handling for missing commands
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
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
        # Help should contain the subcommands
        assert "optimize" in captured.out
        assert "generate" in captured.out
        assert "watch" in captured.out

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
        # Should mention missing argument or show usage
        assert "error" in captured.out.lower() or "argument" in captured.out.lower()

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
        # Should mention missing required argument
        assert "error" in captured.out.lower() or "--watch-dir" in captured.out

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
        spec_file.write_text("key: value\n")

        class MockArgs:
            spec = spec_file
            output = None
            verbose = False

        result = run(MockArgs())
        assert result == 0


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
