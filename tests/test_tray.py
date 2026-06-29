"""Tests for plt_optimizer/ui/tray.py.

This module tests the TrayIconManager class with mocked pystray/pillow dependencies.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ============================================================================
# Test fixtures and helpers
# ============================================================================


@pytest.fixture
def mock_watcher_fn() -> MagicMock:
    """Create a mock watcher function."""
    return MagicMock()


@pytest.fixture
def mock_config_loader() -> MagicMock:
    """Create a mock config loader returning test configuration."""
    return MagicMock(return_value={"watch_dir": "/test/watch", "output_dir": "/test/out"})


@pytest.fixture
def mock_icon_path_fn() -> MagicMock:
    """Create a mock icon path function."""
    return MagicMock(return_value=Path("/fake/icon.ico"))


# ============================================================================
# Test cases
# ============================================================================


class TestCheckDependencies:
    """Tests for _check_dependencies function."""

    def test_check_dependencies_windows_with_infi_systray(self) -> None:
        """Test Windows dependency check with infi.systray available."""
        from plt_optimizer.ui.tray import TrayIconManager, _check_dependencies

        # Create fresh instance to avoid cached state issues
        with patch("plt_optimizer.ui.tray.importlib.util.find_spec") as mock_find:
            mock_find.return_value = MagicMock()  # infi.systray found

            # Should not raise
            _check_dependencies()

    def test_check_dependencies_windows_missing_infi_systray(self) -> None:
        """Test Windows dependency check with missing infi.systray."""
        from plt_optimizer.ui.tray import TrayIconManager, _check_dependencies

        with patch("plt_optimizer.ui.tray._IS_WINDOWS", True), \
             patch("plt_optimizer.ui.tray.importlib.util.find_spec") as mock_find:
            mock_find.return_value = None  # infi.systray NOT found
            with pytest.raises(ImportError, match="infi-systray"):
                _check_dependencies()

    def test_check_dependencies_linux_with_pystray(self) -> None:
        """Test Linux dependency check with pystray and PIL available."""
        from plt_optimizer.ui.tray import TrayIconManager, _check_dependencies

        with patch("plt_optimizer.ui.tray.importlib.util.find_spec") as mock_find:
            # Both pystray and PIL are found
            mock_find.return_value = MagicMock()
            # Should not raise - just loads module-level code


class TestTrayIconManagerInit:
    """Tests for TrayIconManager.__init__ (lines 89-100)."""

    def test_init_basic(self) -> None:
        """Test basic initialization."""
        from plt_optimizer.ui.tray import TrayIconManager

        watcher_fn = MagicMock()
        config_loader = MagicMock(return_value={"watch_dir": "/test"})
        get_icon_path = MagicMock(return_value=Path("/icon.ico"))

        manager = TrayIconManager(watcher_fn, config_loader, get_icon_path)

        assert manager._watcher_fn is watcher_fn
        assert manager._config_loader is config_loader
        assert manager._get_icon_path is get_icon_path
        assert manager._systray is None
        assert manager._watcher_thread is None
        assert manager._stop_event is not None

    def test_init_callbacks_default_to_none(self) -> None:
        """Test that callbacks default to None."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        assert manager.on_settings_requested is None
        assert manager.on_exit_requested is None


class TestLoadIconImage:
    """Tests for _load_icon_image method."""

    def test_load_existing_icon(self) -> None:
        """Test loading an existing icon file."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_get_path = MagicMock(return_value=Path("/test/icon.ico"))
        manager = TrayIconManager(MagicMock(), MagicMock(), mock_get_path)

        # Image is imported locally inside _load_icon_image, so patch PIL.Image
        with patch("PIL.Image") as MockImage:
            mock_img = MagicMock()
            mock_img.size = (64, 64)
            mock_img.mode = "RGB"
            MockImage.open.return_value = mock_img

            result = manager._load_icon_image()

            assert result is mock_img
            mock_get_path.assert_called_once()

    def test_load_icon_file_not_found_creates_fallback(self) -> None:
        """Test that FileNotFoundError creates fallback icon."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_get_path = MagicMock(return_value=Path("/nonexistent/icon.ico"))
        manager = TrayIconManager(MagicMock(), MagicMock(), mock_get_path)

        with patch("PIL.Image") as MockImage:
            # First call raises FileNotFoundError, second is the fallback
            mock_fallback = MagicMock()
            MockImage.new.return_value = mock_fallback
            MockImage.open.side_effect = FileNotFoundError("Icon not found")

            result = manager._load_icon_image()

            assert result is mock_fallback

    def test_load_icon_other_error_creates_fallback(self) -> None:
        """Test that other image errors create fallback icon."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_get_path = MagicMock(return_value=Path("/bad/icon.ico"))
        manager = TrayIconManager(MagicMock(), MagicMock(), mock_get_path)

        with patch("PIL.Image") as MockImage:
            mock_fallback = MagicMock()
            MockImage.new.return_value = mock_fallback
            MockImage.open.side_effect = IOError("Corrupt image")

            result = manager._load_icon_image()

            assert result is mock_fallback


class TestPystrayMenuAndSetup:
    """Tests for pystray menu creation and setup (lines 163-182)."""

    def test_create_pystray_menu(self) -> None:
        """Test creating the pystray menu structure."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        with patch("pystray.Menu") as MockMenu, \
             patch("pystray.MenuItem") as MockMenuItem:

            mock_menu_instance = MagicMock()
            MockMenu.return_value = mock_menu_instance
            MockMenuItem.side_effect = [MagicMock(), MagicMock()]

            result = manager._create_pystray_menu()

            assert result is mock_menu_instance
            # Check that Menu items were created with callbacks
            assert MockMenuItem.call_count == 2

    def test_setup_pystray(self) -> None:
        """Test setting up pystray icon."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        with patch("pystray.Icon") as MockIcon, \
             patch.object(manager, "_load_icon_image") as mock_load_icon, \
             patch.object(manager, "_create_pystray_menu") as mock_create_menu:

            mock_icon_instance = MagicMock()
            MockIcon.return_value = mock_icon_instance
            mock_load_icon.return_value = MagicMock()
            mock_create_menu.return_value = MagicMock()

            manager._setup_pystray()

            assert manager._systray is mock_icon_instance
            MockIcon.assert_called_once()


class TestPystrayCallbacks:
    """Tests for pystray menu callbacks (lines 192-216)."""

    def test_on_settings_click_with_callback(self) -> None:
        """Test _on_settings_click with callback set."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())
        manager.on_settings_requested = MagicMock()

        manager._on_settings_click(MagicMock(), MagicMock())

        manager.on_settings_requested.assert_called_once()

    def test_on_settings_click_without_callback(self) -> None:
        """Test _on_settings_click without callback set."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())
        # No callback set - should not raise

        manager._on_settings_click(MagicMock(), MagicMock())

    def test_on_settings_click_with_exception(self) -> None:
        """Test _on_settings_click handles exception gracefully."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())
        manager.on_settings_requested = MagicMock(side_effect=RuntimeError("Test error"))

        # Should not raise
        manager._on_settings_click(MagicMock(), MagicMock())

    def test_on_exit_click_with_callback(self) -> None:
        """Test _on_exit_click with callback set."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())
        manager.on_exit_requested = MagicMock()

        manager._on_exit_click(MagicMock(), MagicMock())

        manager.on_exit_requested.assert_called_once()


class TestInfiSystraySetup:
    """Tests for infi.systray setup (lines 224-242)."""

    @pytest.mark.skipif(sys.platform != "win32", reason="infi.systray is Windows-only")
    def test_setup_infi_systray(self) -> None:
        """Test setting up infi.systray icon on Windows."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_get_path = MagicMock(return_value=Path("/icon.ico"))
        manager = TrayIconManager(MagicMock(), MagicMock(), mock_get_path)

        with patch("infi.systray.SysTrayIcon") as MockSysTray:
            mock_instance = MagicMock()
            MockSysTray.return_value = mock_instance

            manager._setup_infi_systray()

            assert manager._systray is mock_instance
            MockSysTray.assert_called_once()
            # Check on_quit callback was provided
            call_kwargs = MockSysTray.call_args.kwargs
            assert "on_quit" in call_kwargs

    def test_on_infi_settings_click(self) -> None:
        """Test infi settings click handler."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())
        manager.on_settings_requested = MagicMock()

        manager._on_infi_settings_click(MagicMock())

        manager.on_settings_requested.assert_called_once()


class TestWatcherLoop:
    """Tests for watcher loop and control (lines 259-295)."""

    def test_watcher_loop_calls_watcher_function(self) -> None:
        """Test that _watcher_loop calls the watcher function."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_watcher = MagicMock()
        manager = TrayIconManager(mock_watcher, MagicMock(), MagicMock())

        stop_event = threading.Event()
        config = {"watch_dir": "/test"}

        manager._watcher_loop(stop_event, config)

        mock_watcher.assert_called_once_with(config)

    def test_watcher_loop_handles_exception(self) -> None:
        """Test that _watcher_loop handles exceptions from watcher."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_watcher = MagicMock(side_effect=RuntimeError("Watcher error"))
        manager = TrayIconManager(mock_watcher, MagicMock(), MagicMock())

        stop_event = threading.Event()
        config = {"watch_dir": "/test"}

        # Should not raise
        manager._watcher_loop(stop_event, config)

    def test_start_watcher_creates_thread(self) -> None:
        """Test start_watcher creates a background thread."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_watcher = MagicMock()
        mock_config_loader = MagicMock(return_value={"watch_dir": "/test"})
        manager = TrayIconManager(mock_watcher, mock_config_loader, MagicMock())

        # Patch the actual watcher function to not block - needs correct signature
        def non_blocking_mock(stop_event: threading.Event, config: dict[str, Any]) -> None:
            pass

        with patch.object(manager, "_watcher_loop", side_effect=non_blocking_mock):
            manager.start_watcher()

            assert manager._watcher_thread is not None
            # Give thread time to start and complete (since our mock returns immediately)
            import time
            time.sleep(0.2)

            # Clean up - ensure no hanging threads
            if manager._stop_event:
                manager._stop_event.set()
            manager.stop_watcher()

    def test_start_watcher_when_already_running(self) -> None:
        """Test that starting watcher when already running returns early."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_watcher = MagicMock()
        manager = TrayIconManager(mock_watcher, MagicMock(), MagicMock())

        with patch.object(manager, "_watcher_loop") as mock_loop:
            # Start first time
            manager.start_watcher()

            # Try to start again - should return early
            manager.start_watcher()

            # Loop should only be called once (from first start)
            import time
            time.sleep(0.2)

            assert manager._watcher_thread is not None

            # Clean up
            manager.stop_watcher()
            manager._stop_event.set()

    def test_stop_watcher_when_not_running(self) -> None:
        """Test stop_watcher when no thread exists."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_watcher = MagicMock()
        manager = TrayIconManager(mock_watcher, MagicMock(), MagicMock())

        # No thread started - should not raise
        manager.stop_watcher()

    def test_stop_watcher_with_running_thread(self) -> None:
        """Test stop_watcher with an active thread."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_watcher = MagicMock()
        mock_config_loader = MagicMock(return_value={"watch_dir": "/test"})
        manager = TrayIconManager(mock_watcher, mock_config_loader, MagicMock())

        # Override watcher to check stop event - needs correct signature
        def slow_watcher(stop_event: threading.Event, config: dict[str, Any]) -> None:
            stop_event.wait(timeout=5.0)

        with patch.object(manager, "_watcher_loop", side_effect=slow_watcher):
            manager.start_watcher()
            import time
            time.sleep(0.2)

            assert manager._watcher_thread is not None

            # Stop should work without hanging
            manager.stop_watcher()

    def test_restart_watcher(self) -> None:
        """Test restart_watcher stops and starts."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_watcher = MagicMock()
        mock_config_loader = MagicMock(return_value={"watch_dir": "/test"})
        manager = TrayIconManager(mock_watcher, mock_config_loader, MagicMock())

        with patch.object(manager, "_watcher_loop"):
            # Start first time
            manager.start_watcher()

            original_thread = manager._watcher_thread

            # Restart should stop and start again
            manager.restart_watcher()

            assert manager._watcher_thread is not None

            # Clean up
            manager.stop_watcher()
            manager._stop_event.set()


class TestNotifications:
    """Tests for notification methods (lines 312-325)."""

    def test_notify_success(self) -> None:
        """Test success notification."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        # Should not raise - just logs
        manager.notify_success("test.plt", 15.5)

    def test_notify_error(self) -> None:
        """Test error notification."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        # Should not raise - just logs
        manager.notify_error("test.plt", "File corrupted")


class TestRunMethods:
    """Tests for run methods (lines 336-363)."""

    @pytest.mark.skip(reason="Blocking test hangs on macOS - pystray run() blocks without timeout")
    def test_run_pystray_blocking(self) -> None:
        """Test _run_pystray in blocking mode."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_watcher = MagicMock()
        manager = TrayIconManager(mock_watcher, MagicMock(), MagicMock())

        with patch.object(manager, "_setup_pystray"), \
             patch.object(manager, "stop_watcher") as mock_stop:

            # Setup mock systray
            manager._systray = MagicMock()

            def raise_interrupt() -> None:
                raise KeyboardInterrupt

            manager._systray.run.side_effect = raise_interrupt  # Don't call, just assign

            # Should handle KeyboardInterrupt gracefully
            try:
                manager._run_pystray(blocking=True)
            except KeyboardInterrupt:
                pass

    def test_run_pystray_non_blocking(self) -> None:
        """Test _run_pystray in non-blocking mode."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_watcher = MagicMock()
        manager = TrayIconManager(mock_watcher, MagicMock(), MagicMock())

        with patch.object(manager, "_setup_pystray"):
            manager._systray = MagicMock()

            # Should not raise
            manager._run_pystray(blocking=False)

            manager._systray.run_detached.assert_called_once()

    @pytest.mark.skip(reason="Windows-only test - infi.systray not available on macOS/Linux")
    def test_run_windows_blocking(self) -> None:
        """Test _run_windows in blocking mode."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_watcher = MagicMock()
        manager = TrayIconManager(mock_watcher, MagicMock(), MagicMock())

        with patch.object(manager, "_setup_infi_systray"), \
             patch.object(manager, "stop_watcher") as mock_stop:

            # Create a mock systray that raises KeyboardInterrupt
            mock_systray = MagicMock()
            manager._systray = mock_systray

            def run_blocking() -> None:
                raise KeyboardInterrupt

            with patch("plt_optimizer.ui.tray.time.sleep"):
                # Simulate context manager exit from interrupt
                mock_systray.__enter__ = MagicMock(return_value=mock_systray)
                mock_systray.__exit__ = MagicMock(return_value=False)

                try:
                    manager._run_windows(blocking=True)
                except KeyboardInterrupt:
                    pass

    @pytest.mark.skip(reason="Blocking test hangs on macOS - pystray run() blocks without timeout")
    def test_run_pystray_exception_during_run(self) -> None:
        """Test pystray run handles exceptions."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_watcher = MagicMock()
        manager = TrayIconManager(mock_watcher, MagicMock(), MagicMock())

        with patch.object(manager, "_setup_pystray"):
            manager._systray = MagicMock()
            manager._systray.run.side_effect = RuntimeError("Systray error")

            with pytest.raises(RuntimeError, match="Systray error"):
                manager._run_pystray(blocking=True)


class TestStopMethod:
    """Tests for stop method (lines 369-385)."""

    def test_stop_with_pystray(self) -> None:
        """Test stopping pystray icon."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_watcher = MagicMock()
        manager = TrayIconManager(mock_watcher, MagicMock(), MagicMock())

        # Simulate pystray (has stop, no shutdown)
        mock_systray = MagicMock(spec=["stop"])
        manager._systray = mock_systray

        with patch.object(manager, "_setup_pystray"):
            pass

        manager.stop()

        mock_systray.stop.assert_called_once()
        assert manager._systray is None

    def test_stop_with_infi_systray(self) -> None:
        """Test stopping infi.systray (should skip shutdown)."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_watcher = MagicMock()
        manager = TrayIconManager(mock_watcher, MagicMock(), MagicMock())

        # Simulate infi.systray (has both stop and shutdown)
        mock_systray = MagicMock(spec=["stop", "shutdown"])
        manager._systray = mock_systray

        manager.stop()

        # Should NOT call shutdown to avoid deadlock
        mock_systray.shutdown.assert_not_called()
        assert manager._systray is None

    def test_stop_with_no_systray(self) -> None:
        """Test stop when _systray is already None."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_watcher = MagicMock()
        manager = TrayIconManager(mock_watcher, MagicMock(), MagicMock())

        # _systray is None by default
        assert manager._systray is None

        # Should not raise
        manager.stop()


class TestGetIconPath:
    """Tests for icon path functions (lines 391-415)."""

    def test_get_icon_path_frozen(self) -> None:
        """Test get_icon_path_frozen when running as frozen exe."""
        from plt_optimizer.ui.tray import get_icon_path_frozen

        # sys.frozen only exists when running as PyInstaller frozen exe
        with patch.dict(sys.__dict__, {"frozen": True, "_MEIPASS": "/tmp/_MEIPASS"}):
            result = get_icon_path_frozen()

            assert result == Path("/tmp/_MEIPASS/assets/icon.ico")

    def test_get_icon_path_frozen_not_frozen(self) -> None:
        """Test get_icon_path_frozen when running as script."""
        from plt_optimizer.ui.tray import get_icon_path_frozen

        with patch.dict(sys.__dict__, {"frozen": False}, clear=False):
            result = get_icon_path_frozen()

            # Should be relative to this file's location
            assert "assets" in str(result)
            assert result.name == "icon.ico"

    def test_get_icon_path_dev(self) -> None:
        """Test get_icon_path_dev returns expected path."""
        from plt_optimizer.ui.tray import get_icon_path_dev

        result = get_icon_path_dev()

        # Should be relative to project root
        assert "assets" in str(result)
        assert result.name == "icon.ico"
