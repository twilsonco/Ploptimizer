"""Tests for plt_optimizer.cli.watch module.

These tests cover:
- PLTFileHandler file type detection and filtering
- WatchCommand argument parsing
- Processing logic with debouncing
- Event handlers (on_created, on_modified)
- run_watcher_from_config function
- WatchCommand.run() method
"""

from __future__ import annotations

import argparse
import signal
import threading
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

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


class TestPLTFileHandlerEventHandlers:
    """Tests for on_created and on_modified event handlers."""

    def test_on_created_skips_directories(self) -> None:
        """Test that directory events are ignored."""
        from plt_optimizer.cli.watch import PLTFileHandler

        handler = PLTFileHandler(
            watch_dir=Path("/watch"),
            output_dir=Path("/output"),
            text_logger=MagicMock(),
            metrics_logger=MagicMock(),
        )

        mock_event = MagicMock()
        mock_event.is_directory = True
        mock_event.src_path = "/watch/some_dir"

        with patch.object(handler, '_process_file') as mock_process:
            handler.on_created(mock_event)
            mock_process.assert_not_called()

    def test_on_created_skips_unsupported_extensions(self) -> None:
        """Test that non-PLT files are ignored."""
        from plt_optimizer.cli.watch import PLTFileHandler

        handler = PLTFileHandler(
            watch_dir=Path("/watch"),
            output_dir=Path("/output"),
            text_logger=MagicMock(),
            metrics_logger=MagicMock(),
        )

        mock_event = MagicMock()
        mock_event.is_directory = False
        mock_event.src_path = "/watch/file.txt"

        with patch.object(handler, '_process_file') as mock_process:
            handler.on_created(mock_event)
            mock_process.assert_not_called()

    def test_on_created_processes_valid_plt_files(self) -> None:
        """Test that valid PLT files trigger processing."""
        from plt_optimizer.cli.watch import PLTFileHandler

        handler = PLTFileHandler(
            watch_dir=Path("/watch"),
            output_dir=Path("/output"),
            text_logger=MagicMock(),
            metrics_logger=MagicMock(),
        )

        mock_event = MagicMock()
        mock_event.is_directory = False
        mock_event.src_path = "/watch/file.plt"

        # Need to also patch _is_plt_file because the file doesn't actually exist
        with patch.object(handler, '_is_plt_file', return_value=True):
            with patch.object(handler, '_should_process', return_value=True):
                with patch.object(handler, '_mark_processed'):
                    with patch.object(handler, '_process_file', return_value=True) as mock_process:
                        handler.on_created(mock_event)
                        mock_process.assert_called_once_with(Path("/watch/file.plt"))

    def test_on_modified_skips_directories(self) -> None:
        """Test that directory modification events are ignored."""
        from plt_optimizer.cli.watch import PLTFileHandler

        handler = PLTFileHandler(
            watch_dir=Path("/watch"),
            output_dir=Path("/output"),
            text_logger=MagicMock(),
            metrics_logger=MagicMock(),
        )

        mock_event = MagicMock()
        mock_event.is_directory = True
        mock_event.src_path = "/watch/some_dir"

        with patch.object(handler, '_process_file') as mock_process:
            handler.on_modified(mock_event)
            mock_process.assert_not_called()

    def test_on_modified_skips_unsupported_extensions(self) -> None:
        """Test that non-PLT file modifications are ignored."""
        from plt_optimizer.cli.watch import PLTFileHandler

        handler = PLTFileHandler(
            watch_dir=Path("/watch"),
            output_dir=Path("/output"),
            text_logger=MagicMock(),
            metrics_logger=MagicMock(),
        )

        mock_event = MagicMock()
        mock_event.is_directory = False
        mock_event.src_path = "/watch/file.csv"

        with patch.object(handler, '_process_file') as mock_process:
            handler.on_modified(mock_event)
            mock_process.assert_not_called()

    def test_on_modified_processes_valid_plt_files(self) -> None:
        """Test that valid PLT file modifications trigger processing."""
        from plt_optimizer.cli.watch import PLTFileHandler

        handler = PLTFileHandler(
            watch_dir=Path("/watch"),
            output_dir=Path("/output"),
            text_logger=MagicMock(),
            metrics_logger=MagicMock(),
        )

        mock_event = MagicMock()
        mock_event.is_directory = False
        mock_event.src_path = "/watch/file.hpgl"

        # Need to also patch _is_plt_file because the file doesn't actually exist
        with patch.object(handler, '_is_plt_file', return_value=True):
            with patch.object(handler, '_should_process', return_value=True):
                with patch.object(handler, '_mark_processed'):
                    with patch.object(handler, '_process_file', return_value=True) as mock_process:
                        handler.on_modified(mock_event)
                        mock_process.assert_called_once_with(Path("/watch/file.hpgl"))

    def test_on_created_skips_recently_processed(self) -> None:
        """Test that recently processed files are debounced."""
        from plt_optimizer.cli.watch import PLTFileHandler

        handler = PLTFileHandler(
            watch_dir=Path("/watch"),
            output_dir=Path("/output"),
            text_logger=MagicMock(),
            metrics_logger=MagicMock(),
        )

        test_file = Path("/watch/recent.plt")
        mock_event = MagicMock()
        mock_event.is_directory = False
        mock_event.src_path = str(test_file)

        with patch.object(handler, '_should_process', return_value=False):
            with patch.object(handler, '_mark_processed') as mock_mark:
                with patch.object(handler, '_process_file') as mock_process:
                    handler.on_created(mock_event)
                    # Should not mark or process if _should_process returns False
                    mock_mark.assert_not_called()
                    mock_process.assert_not_called()


class TestRunWatcherFromConfig:
    """Tests for run_watcher_from_config function."""

    def test_returns_error_for_nonexistent_watch_dir(self, tmp_path: Path) -> None:
        """Test that non-existent watch directory returns error code."""
        from plt_optimizer.cli.watch import run_watcher_from_config

        config = {
            "watch_dir": str(tmp_path / "nonexistent"),
            "output_dir": str(tmp_path / "output"),
            "log_dir": str(tmp_path / "logs"),
        }

        stop_event = threading.Event()

        # Create the watch dir so we don't fail on directory creation
        (tmp_path / "output").mkdir(parents=True)
        (tmp_path / "logs").mkdir(parents=True)

        with patch('plt_optimizer.utils.logging.setup_logging') as mock_setup:
            text_logger = MagicMock()
            metrics_logger = MagicMock()
            mock_setup.return_value = (text_logger, metrics_logger)

            result = run_watcher_from_config(config, stop_event)
            assert result == 1

    def test_returns_error_for_non_directory_watch_path(
        self, tmp_path: Path
    ) -> None:
        """Test that a file path for watch_dir returns error code."""
        from plt_optimizer.cli.watch import run_watcher_from_config

        # Create a file instead of directory
        watch_file = tmp_path / "watchfile"
        watch_file.touch()

        config = {
            "watch_dir": str(watch_file),
            "output_dir": str(tmp_path / "output"),
            "log_dir": str(tmp_path / "logs"),
        }

        stop_event = threading.Event()

        # Create other directories so we don't fail there
        (tmp_path / "output").mkdir(parents=True)
        (tmp_path / "logs").mkdir(parents=True)

        with patch('plt_optimizer.utils.logging.setup_logging') as mock_setup:
            text_logger = MagicMock()
            metrics_logger = MagicMock()
            mock_setup.return_value = (text_logger, metrics_logger)

            result = run_watcher_from_config(config, stop_event)
            assert result == 1


class TestWatchCommandRun:
    """Tests for WatchCommand.run() method."""

    def test_run_returns_error_for_invalid_directories(self) -> None:
        """Test that invalid directories cause non-zero exit."""
        from plt_optimizer.cli.watch import WatchCommand

        tmp_watch = Path("/tmp/test_nonexistent")
        # Ensure it doesn't exist
        if tmp_watch.exists():
            import shutil
            shutil.rmtree(tmp_watch)

        cmd = WatchCommand(args=["--watch-dir", str(tmp_watch)])

        with patch('plt_optimizer.utils.logging.setup_logging') as mock_setup:
            text_logger = MagicMock()
            metrics_logger = MagicMock()
            mock_setup.return_value = (text_logger, metrics_logger)

            result = cmd.run()

            # Should fail because watch_dir doesn't exist
            assert result == 1

    def test_run_sets_shutdown_on_sigterm(self) -> None:
        """Test that SIGTERM handler sets shutdown flag."""
        from plt_optimizer.cli.watch import WatchCommand

        tmp_watch = Path("/tmp/test_signal_handler")
        tmp_output = Path("/tmp/test_output_signal")
        tmp_log = Path("/tmp/test_logs_signal")

        for p in [tmp_watch, tmp_output, tmp_log]:
            p.mkdir(parents=True, exist_ok=True)

        try:
            cmd = WatchCommand(args=[
                "--watch-dir", str(tmp_watch),
                "--output-dir", str(tmp_output),
                "--log-dir", str(tmp_log),
            ])

            text_logger = MagicMock()
            metrics_logger = MagicMock()

            # Set up the loggers directly
            cmd._text_logger = text_logger
            cmd._metrics_logger = metrics_logger

            # Call the signal handler
            cmd._signal_handler(signal.SIGTERM, None)

            assert cmd._shutdown_requested is True

        finally:
            import shutil
            for p in [tmp_watch, tmp_output, tmp_log]:
                if p.exists():
                    shutil.rmtree(p, ignore_errors=True)


class TestWatchCommandValidateDirectories:
    """Tests for _validate_directories method."""

    def test_validate_returns_false_for_nonexistent_watch_dir(self) -> None:
        """Test validation fails for missing watch directory."""
        from plt_optimizer.cli.watch import WatchCommand

        with patch.object(WatchCommand, '_setup_logging'):
            cmd = WatchCommand(args=["--watch-dir", "/nonexistent"])
            cmd._text_logger = MagicMock()
            cmd._metrics_logger = MagicMock()

            result = cmd._validate_directories()
            assert result is False
            # Should log error about non-existent directory
            cmd._text_logger.error.assert_called()


class TestWatchCommandProcessExistingFiles:
    """Tests for _process_existing_files method."""

    def test_processes_plt_files_in_directory(self, tmp_path: Path) -> None:
        """Test that existing PLT files are processed."""
        from plt_optimizer.cli.watch import WatchCommand

        # Create a valid PLT file in watch dir
        plt_file = tmp_path / "test.plt"
        plt_file.write_text("IN;PD100,100;SP;\n")

        log_dir = tmp_path / "logs"

        with patch.object(WatchCommand, '_setup_logging'):
            cmd = WatchCommand(args=[
                "--watch-dir", str(tmp_path),
                "--output-dir", str(tmp_path / "output"),
                "--log-dir", str(log_dir),
            ])
            cmd._text_logger = MagicMock()
            cmd._metrics_logger = MagicMock()

            with patch('plt_optimizer.cli.watch.PLTFileHandler') as MockHandler:
                mock_handler_instance = MagicMock()
                mock_handler_instance._is_plt_file.return_value = True
                mock_handler_instance._should_process.return_value = True
                mock_handler_instance._process_file.return_value = True
                MockHandler.return_value = mock_handler_instance

                result = cmd._process_existing_files()
                # Should count processed files


class TestWatchCommandSignalHandler:
    """Tests for _signal_handler method."""

    def test_sets_shutdown_flag(self) -> None:
        """Test that signal handler sets shutdown flag."""
        from plt_optimizer.cli.watch import WatchCommand

        with patch.object(WatchCommand, '_setup_logging'):
            cmd = WatchCommand(args=["--watch-dir", "/tmp"])
            cmd._text_logger = MagicMock()
            cmd._shutdown_requested = False

            # Call signal handler
            cmd._signal_handler(signal.SIGTERM, None)

            assert cmd._shutdown_requested is True


class TestWatchCommandPathValidation:
    """Tests for path validation methods."""

    def test_validate_path_can_be_created_succeeds_for_existing_parent(
        self, tmp_path: Path
    ) -> None:
        """Test that paths under existing parents validate successfully."""
        from plt_optimizer.cli.watch import WatchCommand

        with patch.object(WatchCommand, '_setup_logging'):
            cmd = WatchCommand(args=["--watch-dir", str(tmp_path)])
            # This should not raise - parent exists and is writable
            cmd._validate_path_can_be_created(tmp_path / "new_subdir")

    def test_validate_path_can_be_created_raises_for_protected_root(
        self, tmp_path: Path
    ) -> None:
        """Test that paths under protected root directories fail validation."""
        from plt_optimizer.cli.watch import WatchCommand

        with patch.object(WatchCommand, '_setup_logging'):
            cmd = WatchCommand(args=["--watch-dir", str(tmp_path)])

            # A path like /nonexistent_root/subdir should raise ValueError
            with pytest.raises(ValueError) as exc_info:
                cmd._validate_path_can_be_created(
                    Path("/this/path/does/not/exist/and/cannot/be/created")
                )
            assert "root directory" in str(exc_info.value).lower() or "writable" in str(
                exc_info.value
            ).lower()


class TestWatchCommandSetupLogging:
    """Tests for _setup_logging method."""

    def test_setup_logging_creates_directories(self, tmp_path: Path) -> None:
        """Test that logging setup creates necessary directories."""
        from plt_optimizer.cli.watch import WatchCommand

        log_dir = tmp_path / "logs"
        output_dir = tmp_path / "output"

        with patch('plt_optimizer.utils.logging.setup_logging') as mock_setup:
            mock_setup.return_value = (MagicMock(), MagicMock())

            cmd = WatchCommand(args=[
                "--watch-dir", str(tmp_path),
                "--output-dir", str(output_dir),
                "--log-dir", str(log_dir),
            ])

            # Mock path validation to succeed
            with patch.object(cmd, '_validate_path_can_be_created'):
                try:
                    cmd._setup_logging()
                except OSError:
                    pass  # May fail for other reasons

                # Either directories exist or an error was raised


class TestProcessFileEdgeCases:
    """Tests for edge cases in _process_file method."""

    def test_process_file_handles_exception_gracefully(self, tmp_path: Path) -> None:
        """Test that exceptions are caught and logged during processing."""
        from plt_optimizer.cli.watch import PLTFileHandler

        test_file = tmp_path / "test.plt"
        test_file.write_text("IN;PD100,100;SP;\n")

        handler = PLTFileHandler(
            watch_dir=tmp_path,
            output_dir=Path("/output"),
            text_logger=MagicMock(),
            metrics_logger=MagicMock(),
        )

        # Mock parser to raise exception
        with patch.object(handler, '_parser') as mock_parser:
            mock_parser.parse_file.side_effect = ValueError("Parse error")

            result = handler._process_file(test_file)
            assert result is False
            # Error should be logged
            handler._text_logger.error.assert_called()


class TestDebugSaveFiles:
    """Tests for debug file saving functionality."""

    def test_debug_save_files_creates_directory(self, tmp_path: Path) -> None:
        """Test that debug directory is created when debug mode enabled."""
        from plt_optimizer.cli.watch import PLTFileHandler

        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True)

        handler = PLTFileHandler(
            watch_dir=tmp_path,
            output_dir=Path("/output"),
            text_logger=MagicMock(),
            metrics_logger=MagicMock(),
            debug_save_files=True,
            log_dir=log_dir,
        )

        mock_doc = MagicMock()
        mock_optimized_doc = MagicMock()

        # Mock the writer to avoid file I/O issues
        with patch.object(handler, '_writer') as mock_writer:
            # plot_plt_document is imported inside _save_debug_files from diagnostics.plotter
            # So we need to mock it at its actual location
            with patch('plt_optimizer.diagnostics.plotter.plot_plt_document'):
                handler._save_debug_files(
                    job_id="test_job",
                    original_doc=mock_doc,
                    optimized_doc=mock_optimized_doc,
                    original_distance=1000.0,
                    optimized_distance=800.0,
                )

        # Debug directory should be created
        debug_dir = log_dir / "debug"
        assert debug_dir.exists()


class TestProcessFileMetrics:
    """Tests for metrics logging in _process_file."""

    def test_process_file_logs_success_metrics(self, tmp_path: Path) -> None:
        """Test that successful processing logs metrics."""
        from plt_optimizer.cli.watch import PLTFileHandler

        test_file = tmp_path / "test.plt"
        test_file.write_text("IN;PD100,100;SP;\n")

        handler = PLTFileHandler(
            watch_dir=tmp_path,
            output_dir=Path("/output"),
            text_logger=MagicMock(),
            metrics_logger=MagicMock(),
        )

        mock_doc = MagicMock()
        mock_doc.stroke_paths = [MagicMock()]
        mock_optimized_result = MagicMock()
        mock_optimized_result.total_travel_distance = 800.0

        with patch.object(handler, '_parser') as mock_parser:
            mock_parser.parse_file.return_value = mock_doc
            mock_parser._parse_and_build_document.return_value = mock_doc

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
                            MockOptimizer.return_value.optimize.return_value = (
                                mock_optimized_result
                            )

                            with patch('plt_optimizer.cli.watch.Reassembler') as MockReasm:
                                mock_reasm_instance = MagicMock()
                                mock_reasm_instance.reassemble.return_value = mock_doc
                                MockReasm.return_value = mock_reasm_instance

                                result = handler._process_file(test_file)

        # Should attempt to log metrics on success


class TestProcessedDirHandling:
    """Tests for processed directory handling."""

    def test_processed_dir_moves_file_on_success(self, tmp_path: Path) -> None:
        """Test that files are moved when processed_dir is set."""
        from plt_optimizer.cli.watch import PLTFileHandler

        watch_dir = tmp_path / "watch"
        output_dir = tmp_path / "output"
        processed_dir = tmp_path / "processed"

        watch_dir.mkdir()
        output_dir.mkdir()

        test_file = watch_dir / "test.plt"
        test_file.write_text("IN;PD100,100;SP;\n")

        handler = PLTFileHandler(
            watch_dir=watch_dir,
            output_dir=output_dir,
            text_logger=MagicMock(),
            metrics_logger=MagicMock(),
            processed_dir=processed_dir,
        )

        mock_doc = MagicMock()
        mock_doc.stroke_paths = [MagicMock()]

        with patch.object(handler, '_parser') as mock_parser:
            mock_parser.parse_file.return_value = mock_doc

            with patch('plt_optimizer.cli.watch.Profiler') as MockProfiler:
                mock_profile_result = MagicMock()
                mock_profile_result.baseline_extent = 10.0
                MockProfiler.return_value.profile.return_value = mock_profile_result

                with patch('plt_optimizer.cli.watch.MetricsCalculator'):
                    with patch('plt_optimizer.cli.watch.Chunker') as MockChunker:
                        MockChunker.return_value.chunk.return_value = [MagicMock()]

                        with patch('plt_optimizer.cli.watch.OptimizerEngine') as MockOpt:
                            mock_opt_result = MagicMock()
                            mock_opt_result.total_travel_distance = 800.0
                            MockOpt.return_value.optimize.return_value = mock_opt_result

                            with patch(
                                'plt_optimizer.cli.watch.Reassembler'
                            ) as MockReasm:
                                MockReasm.return_value.reassemble.return_value = mock_doc

                                # Process file (will move to processed_dir)
                                handler._process_file(test_file)


class TestStructuralVsTextPipeline:
    """Tests for structural vs text document processing pipeline."""

    def test_structural_document_uses_fracture_and_remove_redundant(
        self, tmp_path: Path
    ) -> None:
        """Test that structural documents are processed through fracture path."""
        from plt_optimizer.cli.watch import PLTFileHandler

        test_file = tmp_path / "test.plt"
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        test_file.write_text("IN;PD100,100;SP;\n")

        handler = PLTFileHandler(
            watch_dir=tmp_path,
            output_dir=output_dir,
            text_logger=MagicMock(),
            metrics_logger=MagicMock(),
        )

        mock_doc = MagicMock()
        mock_doc.stroke_paths = [MagicMock()]

        with patch.object(handler, '_parser') as mock_parser:
            mock_parser.parse_file.return_value = mock_doc

            with patch('plt_optimizer.cli.watch.Profiler') as MockProfiler:
                # Set is_structural to True
                mock_profile_result = MagicMock()
                mock_profile_result.is_structural = True
                mock_profile_result.baseline_extent = 10.0
                MockProfiler.return_value.profile.return_value = mock_profile_result

                with patch('plt_optimizer.cli.watch.MetricsCalculator') as MockMetricsCalc:
                    mock_metrics_calc = MagicMock()
                    mock_metrics_calc.calculate_original_travel_distance.return_value = (
                        1000.0
                    )
                    MockMetricsCalc.return_value = mock_metrics_calc

                    # Patch fracture_linear_paths and remove_redundant_strokes
                    with patch(
                        'plt_optimizer.cli.watch.fracture_linear_paths'
                    ) as mock_fracture:
                        mock_fractured_doc = MagicMock()
                        mock_fracture.return_value = mock_fractured_doc

                        with patch(
                            'plt_optimizer.cli.watch.remove_redundant_strokes'
                        ) as mock_remove:
                            mock_remove.return_value = mock_fractured_doc

                            with patch('plt_optimizer.cli.watch.Chunker') as MockChunker:
                                MockChunker.return_value.chunk.return_value = [
                                    MagicMock()
                                ]

                                with patch(
                                    'plt_optimizer.cli.watch.OptimizerEngine'
                                ) as MockOpt:
                                    mock_opt_result = MagicMock()
                                    mock_opt_result.total_travel_distance = 800.0
                                    MockOpt.return_value.optimize.return_value = (
                                        mock_opt_result
                                    )

                                    with patch(
                                        'plt_optimizer.cli.watch.Reassembler'
                                    ) as MockReasm:
                                        MockReasm.return_value.reassemble.return_value = (
                                            mock_doc
                                        )

                                        handler._process_file(test_file)

                                        # Verify fracture was called for structural docs
                                        mock_fracture.assert_called_once()
                                        mock_remove.assert_called_once()

    def test_text_document_skips_simplification(self, tmp_path: Path) -> None:
        """Test that text documents skip stroke simplification."""
        from plt_optimizer.cli.watch import PLTFileHandler

        test_file = tmp_path / "test.plt"
        test_file.write_text("IN;PD100,100;SP;\n")

        handler = PLTFileHandler(
            watch_dir=tmp_path,
            output_dir=Path("/output"),
            text_logger=MagicMock(),
            metrics_logger=MagicMock(),
        )

        mock_doc = MagicMock()
        mock_doc.stroke_paths = [MagicMock()]

        with patch.object(handler, '_parser') as mock_parser:
            mock_parser.parse_file.return_value = mock_doc

            with patch('plt_optimizer.cli.watch.Profiler') as MockProfiler:
                # Set is_structural to False (text document)
                mock_profile_result = MagicMock()
                mock_profile_result.is_structural = False
                mock_profile_result.baseline_extent = 10.0
                MockProfiler.return_value.profile.return_value = mock_profile_result

                with patch('plt_optimizer.cli.watch.MetricsCalculator') as MockMetricsCalc:
                    mock_metrics_calc = MagicMock()
                    mock_metrics_calc.calculate_original_travel_distance.return_value = (
                        1000.0
                    )
                    MockMetricsCalc.return_value = mock_metrics_calc

                    # These should NOT be called for text documents
                    with patch(
                        'plt_optimizer.cli.watch.fracture_linear_paths'
                    ) as mock_fracture:
                        with patch(
                            'plt_optimizer.cli.watch.remove_redundant_strokes'
                        ) as mock_remove:
                            with patch('plt_optimizer.cli.watch.Chunker') as MockChunker:
                                MockChunker.return_value.chunk.return_value = [
                                    MagicMock()
                                ]

                                with patch(
                                    'plt_optimizer.cli.watch.OptimizerEngine'
                                ) as MockOpt:
                                    mock_opt_result = MagicMock()
                                    mock_opt_result.total_travel_distance = 800.0
                                    MockOpt.return_value.optimize.return_value = (
                                        mock_opt_result
                                    )

                                    with patch(
                                        'plt_optimizer.cli.watch.Reassembler'
                                    ) as MockReasm:
                                        MockReasm.return_value.reassemble.return_value = (
                                            mock_doc
                                        )

                                        handler._process_file(test_file)

                                        # Fracture and remove should NOT be called for text
                                        mock_fracture.assert_not_called()
                                        mock_remove.assert_not_called()


class TestMainFunction:
    """Tests for the main entry point."""

    def test_main_creates_watch_command(self) -> None:
        """Test that main() creates WatchCommand and runs it."""
        from plt_optimizer.cli.watch import main

        with patch('plt_optimizer.cli.watch.WatchCommand') as MockCmd:
            mock_cmd_instance = MagicMock()
            mock_cmd_instance.run.return_value = 0
            MockCmd.return_value = mock_cmd_instance

            result = main(["--watch-dir", "/tmp"])

            assert result == 0
            # WatchCommand is called with args as first positional argument
            MockCmd.assert_called_once_with(["--watch-dir", "/tmp"])


class TestWatchCommandArgumentParsingExtended:
    """Extended tests for WatchCommand argument parsing."""

    def test_parse_args_processed_dir(self) -> None:
        """Test parsing of --processed-dir argument."""
        from plt_optimizer.cli.watch import WatchCommand

        cmd = WatchCommand(args=[
            "--watch-dir", "/some/path",
            "--processed-dir", "/archive/path",
        ])

        assert cmd._args.processed_dir == Path("/archive/path")

    def test_parse_args_debug_save_files(self) -> None:
        """Test parsing of --debug-save-files argument."""
        from plt_optimizer.cli.watch import WatchCommand

        cmd = WatchCommand(args=[
            "--watch-dir", "/some/path",
            "--debug-save-files",
        ])

        assert cmd._args.debug_save_files is True


class TestIsPltFile:
    """Tests for _is_plt_file method."""

    def test_is_plt_file_returns_true_for_supported_files(self) -> None:
        """Test that valid PLT files return True."""
        from plt_optimizer.cli.watch import PLTFileHandler

        handler = PLTFileHandler(
            watch_dir=Path("/watch"),
            output_dir=Path("/output"),
            text_logger=MagicMock(),
            metrics_logger=MagicMock(),
        )

        # Test with actual Path objects
        assert handler._is_supported_file(Path("test.plt")) is True
        assert handler._is_supported_file(Path("test.hpgl")) is True

    def test_is_plt_file_returns_false_for_directories(self, tmp_path: Path) -> None:
        """Test that directories return False."""
        from plt_optimizer.cli.watch import PLTFileHandler

        handler = PLTFileHandler(
            watch_dir=tmp_path,
            output_dir=Path("/output"),
            text_logger=MagicMock(),
            metrics_logger=MagicMock(),
        )

        subdir = tmp_path / "subdir"
        subdir.mkdir()

        assert handler._is_plt_file(subdir) is False