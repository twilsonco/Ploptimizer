"""Tests for plt_optimizer.cli.watch module.

These tests cover:
- PLTFileHandler file type detection and filtering
- WatchCommand argument parsing
- Processing logic with debouncing
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestPLTFileHandlerSupportedExtensions:
    """Tests for _is_supported_file and _is_plt_file methods."""

    def test_is_supported_file_accepts_lowercase(self) -> None:
        """Test that lowercase .plt and .hpgl extensions are accepted."""
        from plt_optimizer.cli.watch import PLTFileHandler

        handler = PLTFileHandler(
            watch_dir=Path("/watch"),
            output_dir=Path("/output"),
            text_logger=MagicMock(),
            metrics_logger=MagicMock(),
        )

        assert handler._is_supported_file(Path("file.plt")) is True
        assert handler._is_supported_file(Path("file.hpgl")) is True

    def test_is_supported_file_accepts_uppercase(self) -> None:
        """Test that uppercase .PLT and .HPGL extensions are accepted."""
        from plt_optimizer.cli.watch import PLTFileHandler

        handler = PLTFileHandler(
            watch_dir=Path("/watch"),
            output_dir=Path("/output"),
            text_logger=MagicMock(),
            metrics_logger=MagicMock(),
        )

        assert handler._is_supported_file(Path("file.PLT")) is True
        assert handler._is_supported_file(Path("file.HPGL")) is True

    def test_is_supported_file_rejects_other_extensions(self) -> None:
        """Test that unsupported extensions are rejected."""
        from plt_optimizer.cli.watch import PLTFileHandler

        handler = PLTFileHandler(
            watch_dir=Path("/watch"),
            output_dir=Path("/output"),
            text_logger=MagicMock(),
            metrics_logger=MagicMock(),
        )

        assert handler._is_supported_file(Path("file.txt")) is False
        assert handler._is_supported_file(Path("file.csv")) is False
        assert handler._is_supported_file(Path("file.gcode")) is False
        assert handler._is_supported_file(Path("file")) is False


class TestWatchCommandArgumentParsing:
    """Tests for WatchCommand argument parsing."""

    def test_parse_args_requires_watch_dir(self) -> None:
        """Test that --watch-dir is a required argument."""
        from plt_optimizer.cli.watch import WatchCommand

        with pytest.raises(SystemExit):
            WatchCommand(args=[])

    def test_parse_args_accepts_valid_arguments(self) -> None:
        """Test parsing of all valid arguments."""
        from plt_optimizer.cli.watch import WatchCommand

        cmd = WatchCommand(args=[
            "--watch-dir", "/some/path",
            "--output-dir", "/output/path",
            "--log-dir", "/log/path",
            "--fast-mode",
        ])

        assert cmd._args.watch_dir == Path("/some/path")
        assert cmd._args.output_dir == Path("/output/path")
        assert cmd._args.log_dir == Path("/log/path")
        assert cmd._args.fast_mode is True

    def test_parse_args_defaults(self) -> None:
        """Test default values when optional arguments are not provided."""
        from plt_optimizer.cli.watch import WatchCommand

        cmd = WatchCommand(args=["--watch-dir", "/some/path"])

        assert cmd._args.watch_dir == Path("/some/path")
        assert cmd._args.output_dir == Path("./optimized")
        assert cmd._args.log_dir == Path("./logs")
        assert cmd._args.fast_mode is False


class TestPLTFileHandlerDebouncing:
    """Tests for file processing debouncing logic."""

    def test_should_process_returns_false_for_recently_processed(
        self, tmp_path: Path
    ) -> None:
        """Test that files in processed_files set return False."""
        from plt_optimizer.cli.watch import PLTFileHandler

        handler = PLTFileHandler(
            watch_dir=tmp_path,
            output_dir=Path("/output"),
            text_logger=MagicMock(),
            metrics_logger=MagicMock(),
        )

        test_file = tmp_path / "test.plt"
        # Create a real file so exists() and open() succeed
        test_file.touch()

        try:
            # First call should return True (file not in processed set)
            assert handler._should_process(test_file) is True

            # Mark as processed
            handler._mark_processed(test_file)

            # Second call should return False
            assert handler._should_process(test_file) is False
        finally:
            test_file.unlink()

    def test_mark_processed_limits_set_size(self) -> None:
        """Test that _processed_files set doesn't grow unbounded."""
        from plt_optimizer.cli.watch import PLTFileHandler

        handler = PLTFileHandler(
            watch_dir=Path("/watch"),
            output_dir=Path("/output"),
            text_logger=MagicMock(),
            metrics_logger=MagicMock(),
        )

        # Add more than 1000 entries
        for i in range(1100):
            handler._mark_processed(Path(f"/watch/file{i}.plt"))

        # Set should be pruned to ~500 entries
        assert len(handler._processed_files) <= 600


class TestPLTFileHandlerProcessFile:
    """Tests for the _process_file method."""

    def test_process_file_returns_false_for_empty_blocks(
        self, tmp_path: Path
    ) -> None:
        """Test that processing returns False when no blocks are generated."""
        from plt_optimizer.cli.watch import PLTFileHandler

        # Create a real temporary file to process
        test_file = tmp_path / "empty.plt"
        test_file.write_text("IN;SP;\n", encoding="utf-8")

        handler = PLTFileHandler(
            watch_dir=tmp_path,
            output_dir=Path("/output"),
            text_logger=MagicMock(),
            metrics_logger=MagicMock(),
        )

        # Mock the parser, profiler, chunker to simulate empty blocks
        mock_doc = MagicMock()
        mock_doc.stroke_paths = []
        mock_doc.total_segments = 0

        with patch.object(handler, '_parser') as mock_parser:
            mock_parser.parse_file.return_value = mock_doc

            with patch('plt_optimizer.cli.watch.Profiler') as MockProfiler:
                mock_profile_result = MagicMock()
                mock_profile_result.baseline_extent = 10.0
                MockProfiler.return_value.profile.return_value = mock_profile_result

                with patch('plt_optimizer.cli.watch.Chunker') as MockChunker:
                    # Return empty blocks to trigger the warning path
                    MockChunker.return_value.chunk.return_value = []

                    result = handler._process_file(test_file)

        assert result is False

    def test_process_file_uses_fast_mode_strategy(self, tmp_path: Path) -> None:
        """Test that fast mode uses NearestNeighbor2OptStrategy."""
        from plt_optimizer.cli.watch import PLTFileHandler
        from unittest.mock import MagicMock, patch

        # Create a real temporary file to process
        test_file = tmp_path / "test.plt"
        test_file.write_text("IN;PD100,100;SP;\n", encoding="utf-8")

        # Create handler in fast mode
        handler = PLTFileHandler(
            watch_dir=tmp_path,
            output_dir=Path("/output"),
            text_logger=MagicMock(),
            metrics_logger=MagicMock(),
            fast_mode=True,
        )

        mock_doc = MagicMock()
        mock_doc.stroke_paths = [MagicMock()]

        with patch.object(handler, '_parser') as mock_parser:
            mock_parser.parse_file.return_value = mock_doc

            with patch('plt_optimizer.cli.watch.Profiler') as MockProfiler:
                mock_profile_result = MagicMock()
                mock_profile_result.baseline_extent = 10.0
                MockProfiler.return_value.profile.return_value = mock_profile_result

                with patch('plt_optimizer.cli.watch.MetricsCalculator') as MockMetricsCalc:
                    mock_metrics_calc = MagicMock()
                    mock_metrics_calc.calculate_original_travel_distance.return_value = (
                        1000.0
                    )
                    MockMetricsCalc.return_value = mock_metrics_calc

                    with patch('plt_optimizer.cli.watch.Chunker') as MockChunker:
                        mock_blocks = [MagicMock()]
                        MockChunker.return_value.chunk.return_value = mock_blocks

                        with patch(
                            'plt_optimizer.cli.watch.OptimizerEngine'
                        ) as MockOptimizer:
                            handler._process_file(test_file)

                            # Verify the strategy was set correctly (fast_mode=True)
                            call_args = MockOptimizer.call_args
                            strategy_name = call_args[1]["strategy"].__class__.__name__
                            assert strategy_name == "NearestNeighbor2OptStrategy"

    def test_process_file_uses_parallel_ensemble_by_default(
        self, tmp_path: Path
    ) -> None:
        """Test that non-fast mode uses ParallelEnsembleStrategy."""
        from plt_optimizer.cli.watch import PLTFileHandler
        from unittest.mock import MagicMock, patch

        # Create a real temporary file to process
        test_file = tmp_path / "test.plt"
        test_file.write_text("IN;PD100,100;SP;\n", encoding="utf-8")

        # Create handler in normal mode (not fast)
        handler = PLTFileHandler(
            watch_dir=tmp_path,
            output_dir=Path("/output"),
            text_logger=MagicMock(),
            metrics_logger=MagicMock(),
            fast_mode=False,
        )

        mock_doc = MagicMock()
        mock_doc.stroke_paths = [MagicMock()]

        with patch.object(handler, '_parser') as mock_parser:
            mock_parser.parse_file.return_value = mock_doc

            with patch('plt_optimizer.cli.watch.Profiler') as MockProfiler:
                mock_profile_result = MagicMock()
                mock_profile_result.baseline_extent = 10.0
                MockProfiler.return_value.profile.return_value = mock_profile_result

                with patch('plt_optimizer.cli.watch.MetricsCalculator') as MockMetricsCalc:
                    mock_metrics_calc = MagicMock()
                    mock_metrics_calc.calculate_original_travel_distance.return_value = (
                        1000.0
                    )
                    MockMetricsCalc.return_value = mock_metrics_calc

                    with patch('plt_optimizer.cli.watch.Chunker') as MockChunker:
                        mock_blocks = [MagicMock()]
                        MockChunker.return_value.chunk.return_value = mock_blocks

                        with patch(
                            'plt_optimizer.cli.watch.OptimizerEngine'
                        ) as MockOptimizer:
                            handler._process_file(test_file)

                            # Verify the strategy was set correctly (fast_mode=False)
                            call_args = MockOptimizer.call_args
                            strategy_name = call_args[1]["strategy"].__class__.__name__
                            assert strategy_name == "ParallelEnsembleStrategy"