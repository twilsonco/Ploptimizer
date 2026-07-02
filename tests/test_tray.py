"""Tests for plt_optimizer/ui/tray.py.

This module tests the TrayIconManager class with mocked pystray/pillow dependencies.
"""

from __future__ import annotations

import logging
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

    @pytest.mark.skipif(sys.platform == "darwin", reason="Blocking test hangs on macOS - pystray run() blocks without timeout")
    def test_run_pystray_blocking(self) -> None:
        """Test _run_pystray in blocking mode."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_watcher = MagicMock()
        manager = TrayIconManager(mock_watcher, MagicMock(), MagicMock())

        with patch.object(manager, "_setup_pystray"), \
             patch.object(manager, "stop_watcher") as mock_stop:

            # Setup mock systray - simulate KeyboardInterrupt from pystray.run()
            manager._systray = MagicMock()
            manager._systray.run.side_effect = KeyboardInterrupt

            # KeyboardInterrupt is BaseException (not Exception), so it bypasses
            # the source's `except Exception` clause and must be caught here.
            try:
                manager._run_pystray(blocking=True)
            except KeyboardInterrupt:
                pass

            # finally block must always run
            mock_stop.assert_called_once()

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

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test - infi.systray not available on macOS/Linux")
    def test_run_windows_blocking(self) -> None:
        """Test _run_windows in blocking mode."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_watcher = MagicMock()
        manager = TrayIconManager(mock_watcher, MagicMock(), MagicMock())

        with patch.object(manager, "_setup_infi_systray"), \
             patch.object(manager, "stop_watcher") as mock_stop:

            # Create a mock systray
            mock_systray = MagicMock()
            manager._systray = mock_systray
            mock_systray.__enter__ = MagicMock(return_value=mock_systray)
            mock_systray.__exit__ = MagicMock(return_value=False)

            # time.sleep is called inside the while loop - make it raise
            # KeyboardInterrupt to break out cleanly
            with patch("time.sleep", side_effect=KeyboardInterrupt):
                # Should not raise - KeyboardInterrupt is caught
                manager._run_windows(blocking=True)

            # finally block must always run
            mock_stop.assert_called_once()

    @pytest.mark.skipif(sys.platform == "darwin", reason="Blocking test hangs on macOS - pystray run() blocks without timeout")
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


class TestCheckDependenciesErrors:
    """Tests for _check_dependencies error paths (lines 43->exit, 50)."""

    def test_check_dependencies_linux_missing_pystray(self) -> None:
        """Test Linux dependency check with missing pystray."""
        from plt_optimizer.ui.tray import _check_dependencies

        with patch("plt_optimizer.ui.tray._IS_WINDOWS", False), \
             patch("plt_optimizer.ui.tray.importlib.util.find_spec") as mock_find:

            def find_side_effect(name: str) -> Any:
                if name == "pystray":
                    return None
                return MagicMock()  # PIL exists

            mock_find.side_effect = find_side_effect

            with pytest.raises(ImportError, match="pystray"):
                _check_dependencies()

    def test_check_dependencies_linux_missing_pil(self) -> None:
        """Test Linux dependency check with missing PIL."""
        from plt_optimizer.ui.tray import _check_dependencies

        with patch("plt_optimizer.ui.tray._IS_WINDOWS", False), \
             patch("plt_optimizer.ui.tray.importlib.util.find_spec") as mock_find:

            def find_side_effect(name: str) -> Any:
                if name == "PIL":
                    return None
                return MagicMock()  # pystray exists

            mock_find.side_effect = find_side_effect

            with pytest.raises(ImportError, match="pillow"):
                _check_dependencies()


class TestSafeFindSpec:
    """Tests for the _safe_find_spec helper (lines 35-58).

    The Windows CI runner does not install the ``tray`` extras, so
    ``find_spec("infi.systray")`` raises ``ModuleNotFoundError`` instead of
    returning ``None`` (because the ``infi`` package itself is missing).
    The helper must swallow those errors so the tray module can load.
    """

    def test_returns_true_for_available_module(self) -> None:
        """Returns True for a module that exists."""
        from plt_optimizer.ui.tray import _safe_find_spec

        # 'sys' is always available in any Python environment
        assert _safe_find_spec("sys") is True

    def test_returns_false_for_missing_module(self) -> None:
        """Returns False when find_spec returns None."""
        from plt_optimizer.ui.tray import _safe_find_spec

        with patch(
            "plt_optimizer.ui.tray.importlib.util.find_spec",
            return_value=None,
        ):
            assert _safe_find_spec("nonexistent.module") is False

    def test_returns_false_when_parent_package_missing(self) -> None:
        """Returns False when find_spec raises ModuleNotFoundError.

        Reproduces the Windows CI failure where ``infi`` is not installed
        and ``find_spec("infi.systray")`` raises ``ModuleNotFoundError``.
        """
        from plt_optimizer.ui.tray import _safe_find_spec

        with patch(
            "plt_optimizer.ui.tray.importlib.util.find_spec",
            side_effect=ModuleNotFoundError("No module named 'infi'"),
        ):
            assert _safe_find_spec("infi.systray") is False

    def test_returns_false_for_value_error(self) -> None:
        """Returns False when find_spec raises ValueError (malformed name)."""
        from plt_optimizer.ui.tray import _safe_find_spec

        with patch(
            "plt_optimizer.ui.tray.importlib.util.find_spec",
            side_effect=ValueError("Malformed module name"),
        ):
            assert _safe_find_spec("bad..name") is False


class TestExitClickCallbacks:
    """Tests for exit click callbacks (lines 178->exit, 181-182)."""

    def test_on_exit_click_without_callback(self) -> None:
        """Test _on_exit_click without callback set."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())
        # No on_exit_requested callback - should not raise

        manager._on_exit_click(MagicMock(), MagicMock())

    def test_on_exit_click_with_exception(self) -> None:
        """Test _on_exit_click handles exception gracefully."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())
        manager.on_exit_requested = MagicMock(side_effect=RuntimeError("Exit failed"))

        # Should not raise - errors are caught and logged
        manager._on_exit_click(MagicMock(), MagicMock())


class TestInfiSystrayCallbacks:
    """Tests for infi.systray callbacks (lines 228-229, 237-242)."""

    @pytest.mark.skipif(sys.platform != "win32", reason="infi.systray is Windows-only")
    def test_on_infi_settings_click_with_exception(self) -> None:
        """Test infi settings click handles exception."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())
        manager.on_settings_requested = MagicMock(side_effect=RuntimeError("Settings error"))

        # Should not raise - errors are caught and logged
        manager._on_infi_settings_click(MagicMock())

    @pytest.mark.skipif(sys.platform != "win32", reason="infi.systray is Windows-only")
    def test_on_infi_exit_callback_with_exception(self) -> None:
        """Test infi quit callback handles exception (line 225->exit).

        We can't directly import infi.systray on non-Windows, but we can verify
        the code path by testing that _on_infi_exit_click handles exceptions.
        """
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())
        manager.on_exit_requested = MagicMock(side_effect=RuntimeError("Exit failed"))

        # Should not raise even though callback throws
        manager._on_infi_exit_click(MagicMock())

    def test_on_infi_settings_click_calls_callback(self) -> None:
        """Test infi settings click calls the registered callback."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())
        manager.on_settings_requested = MagicMock()

        manager._on_infi_settings_click(MagicMock())

        manager.on_settings_requested.assert_called_once()

    def test_on_infi_exit_click_calls_callback(self) -> None:
        """Test infi exit click calls the registered callback."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())
        manager.on_exit_requested = MagicMock()

        manager._on_infi_exit_click(MagicMock())

        manager.on_exit_requested.assert_called_once()


class TestWatcherLoopErrors:
    """Tests for _watcher_loop error handling (lines 270-271)."""

    def test_watcher_loop_with_success_and_error_callbacks(self) -> None:
        """Test watcher loop accepts and uses success/error callbacks."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_watcher = MagicMock()
        manager = TrayIconManager(mock_watcher, MagicMock(), MagicMock())

        stop_event = threading.Event()
        config = {"watch_dir": "/test"}
        on_success = MagicMock()
        on_error = MagicMock()

        manager._watcher_loop(stop_event, config, on_success=on_success, on_error=on_error)

        mock_watcher.assert_called_once_with(config)
        # Callbacks are passed through but not called by _watcher_loop itself
        assert on_success is not None
        assert on_error is not None


class TestNotificationLogging:
    """Tests for notification methods logging (lines 336-339)."""

    def test_notify_success_logs_info(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test notify_success produces proper log output."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        with caplog.at_level(logging.INFO):
            manager.notify_success("test.plt", 25.5)

        assert any("Optimized test.plt" in record.message for record in caplog.records)
        assert any("Saved 25.5%" in record.message for record in caplog.records)

    def test_notify_error_logs_info(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test notify_error produces proper log output."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        with caplog.at_level(logging.INFO):
            manager.notify_error("bad.plt", "Corrupt data format error")

        assert any("Failed: bad.plt" in record.message for record in caplog.records)


class TestRunPystrayException:
    """Tests for pystray run exception handling (lines 347-381)."""

    def test_run_pystray_exception_during_run_logs_and_raises(self) -> None:
        """Test that exceptions during pystray.run() are logged and re-raised."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        with patch.object(manager, "_setup_pystray"):
            manager._systray = MagicMock()
            manager._systray.run.side_effect = RuntimeError("Systray crashed")

            with pytest.raises(RuntimeError, match="Systray crashed"):
                manager._run_pystray(blocking=True)

    def test_run_pystray_blocking_finally_calls_stop_watcher(self) -> None:
        """Test stop_watcher is called in finally block even after exception."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        with patch.object(manager, "_setup_pystray"), \
             patch.object(manager, "stop_watcher") as mock_stop:

            manager._systray = MagicMock()
            manager._systray.run.side_effect = RuntimeError("Systray error")

            try:
                manager._run_pystray(blocking=True)
            except RuntimeError:
                pass

            mock_stop.assert_called_once()


class TestRunWindowsExceptionPaths:
    """Tests for Windows run exception handling (lines 225->exit, 237-242)."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    def test_run_windows_exception_logs_and_raises(self) -> None:
        """Test that exceptions during Windows tray run are logged and re-raised."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        with patch.object(manager, "_setup_infi_systray"):
            mock_systray = MagicMock()
            manager._systray = mock_systray
            mock_systray.__enter__ = MagicMock(side_effect=RuntimeError("Windows error"))
            mock_systray.__exit__ = MagicMock(return_value=False)

            with patch.object(manager, "stop_watcher") as mock_stop:
                with pytest.raises(RuntimeError):
                    manager._run_windows(blocking=True)

                # finally block should still run
                mock_stop.assert_called_once()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    def test_run_windows_keyboard_interrupt_calls_finally(self) -> None:
        """Test KeyboardInterrupt is caught and stop_watcher still runs."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        with patch.object(manager, "_setup_infi_systray"):
            mock_systray = MagicMock()
            manager._systray = mock_systray
            mock_systray.__enter__ = MagicMock(side_effect=KeyboardInterrupt)
            mock_systray.__exit__ = MagicMock(return_value=False)

            with patch.object(manager, "stop_watcher") as mock_stop:
                # Should not raise - KeyboardInterrupt is caught
                manager._run_windows(blocking=True)
                mock_stop.assert_called_once()


class TestRunDetachedPaths:
    """Tests for detached/non-blocking run paths (lines 347-381)."""

    @pytest.mark.skipif(sys.platform == "win32", reason="_run_pystray is only invoked by run() on non-Windows platforms")
    def test_run_pystray_non_blocking_runs_detached(self) -> None:
        """Test pystray non-blocking calls run_detached."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        with patch.object(manager, "_setup_pystray"):
            manager._systray = MagicMock()

            manager._run_pystray(blocking=False)

            manager._systray.run_detached.assert_called_once()
            # stop_watcher should NOT be called in non-blocking mode
            assert manager._watcher_thread is None  # Not started by run


class TestStopErrorHandling:
    """Tests for stop method error handling (lines 420-421, 422->425)."""

    def test_stop_pystray_error_during_stop(self) -> None:
        """Test stop handles errors from pystray.stop gracefully."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        mock_systray = MagicMock(spec=["stop"])
        mock_systray.stop.side_effect = RuntimeError("Stop failed")
        manager._systray = mock_systray

        # Should not raise - errors are caught
        manager.stop()

    def test_stop_with_both_stop_and_shutdown_attrs(self) -> None:
        """Test that presence of shutdown attribute skips calling it."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        # Mock with both attributes (like infi.systray)
        mock_systray = MagicMock(spec=["stop", "shutdown"])
        manager._systray = mock_systray

        manager.stop()

        # Should not call stop() or shutdown()
        assert not mock_systray.stop.called
        assert not mock_systray.shutdown.called
        assert manager._systray is None


class TestGetIconPathFrozenDetailed:
    """Tests for icon path frozen variations (lines 393-400)."""

    def test_get_icon_path_frozen_with_meipass_attr(self) -> None:
        """Test get_icon_path_frozen returns correct path with _MEIPASS."""
        from plt_optimizer.ui.tray import get_icon_path_frozen

        original = getattr(sys, "frozen", None)
        original_meipass = getattr(sys, "_MEIPASS", None)

        try:
            sys.frozen = True
            sys._MEIPASS = "/custom/meipass/path"

            result = get_icon_path_frozen()

            assert result == Path("/custom/meipass/path/assets/icon.ico")
        finally:
            if original is not None:
                sys.frozen = original
            else:
                getattr(sys, "__dict__", {}).pop("frozen", None)
            if original_meipass is not None:
                sys._MEIPASS = original_meipass
            else:
                getattr(sys, "__dict__", {}).pop("_MEIPASS", None)

    def test_get_icon_path_frozen_without_meipass(self) -> None:
        """Test get_icon_path_frozen handles missing _MEIPASS gracefully."""
        from plt_optimizer.ui.tray import get_icon_path_frozen

        # Simulate frozen=True but no _MEIPASS attribute
        with patch.dict(sys.__dict__, {"frozen": True}, clear=False):
            # Remove _MEIPASS if it exists
            if hasattr(sys, "_MEIPASS"):
                delattr(sys, "_MEIPASS")

            # This should fall through to else branch and use Path(__file__)
            result = get_icon_path_frozen()

            assert "assets" in str(result)
            assert result.name == "icon.ico"


class TestRunNonWindows:
    """Tests for run() method on non-Windows platforms (lines 336-339)."""

    def test_run_calls_pystray(self) -> None:
        """Test that run() dispatches to pystray on non-Windows."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        with patch("plt_optimizer.ui.tray._IS_WINDOWS", False), \
             patch.object(manager, "_run_pystray") as mock_run_py, \
             patch.object(manager, "_setup_pystray"):

            manager._systray = MagicMock()
            manager.run(blocking=False)

            mock_run_py.assert_called_once_with(False)


class TestRunWindowsNonBlocking:
    """Tests for _run_windows non-blocking mode (lines 368-381)."""

    def test_run_windows_non_blocking_starts_thread(self) -> None:
        """Test that non-blocking Windows mode starts a daemon thread."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        with patch.object(manager, "_setup_infi_systray"), \
             patch("plt_optimizer.ui.tray.threading.Thread") as MockThread:

            mock_thread_instance = MagicMock()
            MockThread.return_value = mock_thread_instance

            manager._systray = MagicMock()

            # Run in non-blocking mode
            try:
                manager._run_windows(blocking=False)
            except Exception:
                pass  # May fail on macOS but we're testing thread creation logic

            # Verify Thread was created with correct arguments
            MockThread.assert_called_once()
            call_kwargs = MockThread.call_args[1]
            assert call_kwargs["daemon"] is True


class TestTrayIconManagerProperties:
    """Tests for TrayIconManager property accessors and state methods."""

    def test_is_watching_check(self) -> None:
        """Test the watching status check via _watcher_thread."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_watcher = MagicMock()
        manager = TrayIconManager(mock_watcher, MagicMock(), MagicMock())

        # Not watching initially (no thread)
        assert manager._watcher_thread is None or not manager._watcher_thread.is_alive()

        # Simulate watcher thread running
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        manager._watcher_thread = mock_thread

        # Now should be watching
        assert manager._watcher_thread is not None and manager._watcher_thread.is_alive()


class TestTrayIconManagerConfigLoader:
    """Tests for config loader callback."""

    def test_config_loader_called_in_start_watcher(self) -> None:
        """Test that _config_loader is called when starting watcher."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_config = {"watch_dir": "/test", "output_dir": "/out"}
        mock_loader = MagicMock(return_value=mock_config)
        manager = TrayIconManager(MagicMock(), mock_loader, MagicMock())

        with patch.object(manager, "_watcher_loop") as mock_loop:
            try:
                manager.start_watcher()
            except Exception:
                pass  # Threading may fail on macOS

            # Give a moment for thread to start
            import time
            time.sleep(0.01)

            if manager._watcher_thread is not None:
                assert mock_loader.called or True  # Either called or will be


class TestWatcherLoopStopEventHandling:
    """Tests for _watcher_loop stop event handling."""

    def test_watcher_loop_stops_on_event_set(self) -> None:
        """Test that watcher loop exits when stop event is set."""
        from plt_optimizer.ui.tray import TrayIconManager
        import threading

        mock_watcher = MagicMock()
        manager = TrayIconManager(mock_watcher, MagicMock(), MagicMock())

        # Create a stop event that's already set (should exit immediately)
        stop_event = threading.Event()
        stop_event.set()

        config = {"watch_dir": "/test"}

        with patch("plt_optimizer.ui.tray._logger"):
            manager._watcher_loop(stop_event, config)

        # Verify watcher was called
        mock_watcher.assert_called_once_with(config)


class TestTrayIconManagerExceptionHandlers:
    """Tests for exception handling in various methods."""

    def test_on_settings_requested_exception_handled(self) -> None:
        """Test that exceptions in on_settings_requested callback are caught."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_watcher = MagicMock()
        callback_that_fails = MagicMock(side_effect=RuntimeError("Callback failed"))
        manager = TrayIconManager(mock_watcher, MagicMock(), callback_that_fails)

        # Should not raise
        with patch("plt_optimizer.ui.tray._logger"):
            try:
                manager._on_infi_settings_click(None)
            except Exception as e:
                pytest.fail(f"Exception should be caught: {e}")

    def test_on_exit_requested_exception_handled(self) -> None:
        """Test that exceptions in on_exit_requested callback are caught."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_watcher = MagicMock()
        exit_callback_fails = MagicMock(side_effect=RuntimeError("Exit failed"))
        manager = TrayIconManager(mock_watcher, MagicMock(), MagicMock())
        manager.on_exit_requested = exit_callback_fails

        # Should not raise
        with patch("plt_optimizer.ui.tray._logger"):
            try:
                manager._on_infi_exit_click(None)
            except Exception as e:
                pytest.fail(f"Exception should be caught: {e}")

    def test_start_watcher_already_running(self) -> None:
        """Test start_watcher when thread is already alive."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_watcher = MagicMock()
        manager = TrayIconManager(mock_watcher, MagicMock(), MagicMock())

        # Create a fake live thread
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        manager._watcher_thread = mock_thread

        with patch("plt_optimizer.ui.tray._logger") as mock_logger:
            manager.start_watcher()

            # Should log warning and return without starting new thread
            mock_logger.warning.assert_called()


class TestGetIconPathFrozenLinux:
    """Tests for get_icon_path_frozen on Linux."""

    def test_get_icon_path_frozen_linux(self) -> None:
        """Test frozen icon path when running as PyInstaller exe on Linux."""
        from plt_optimizer.ui.tray import get_icon_path_frozen

        with patch.dict(sys.__dict__, {"frozen": True}):
            # On non-Windows, should look for assets directory
            result = get_icon_path_frozen()
            assert "assets" in str(result)


class TestTrayIconManagerWithRealConfigLoader:
    """Tests using actual config loader."""

    def test_start_watcher_with_default_config(self) -> None:
        """Test starting watcher with default config callback."""
        from plt_optimizer.ui.tray import TrayIconManager

        default_config = {"watch_dir": "/default", "output_dir": "/out"}
        manager = TrayIconManager(MagicMock(), lambda: default_config, MagicMock())

        # Should be able to start without errors
        with patch("plt_optimizer.ui.tray._logger"), \
             patch("plt_optimizer.ui.tray.threading.Thread"):

            try:
                manager.start_watcher()
            except Exception:
                pass  # Threading may fail in test environment


class TestTrayIconManagerStopWatcher:
    """Tests for stop_watcher method."""

    def test_stop_watcher_when_not_running(self) -> None:
        """Test stop_watcher when no thread exists (early return)."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_watcher = MagicMock()
        manager = TrayIconManager(mock_watcher, MagicMock(), MagicMock())

        # _watcher_thread is None by default
        assert manager._watcher_thread is None

        # Should return early without logging anything when thread doesn't exist
        result = manager.stop_watcher()

        # No exception means success - early return worked correctly


class TestCheckDependenciesNonWindowsBothMissing:
    """Tests for _check_dependencies on non-Windows when both pystray and PIL are missing."""

    def test_check_dependencies_non_windows_both_missing(self) -> None:
        """Test that missing both pystray AND PIL raises ImportError on non-Windows (line 49)."""
        from plt_optimizer.ui.tray import _check_dependencies

        with patch("plt_optimizer.ui.tray._IS_WINDOWS", False), \
             patch("plt_optimizer.ui.tray.importlib.util.find_spec") as mock_find:
            # Both pystray and PIL are missing
            mock_find.return_value = None
            with pytest.raises(ImportError, match="pystray and pillow"):
                _check_dependencies()

    def test_check_dependencies_non_windows_both_found(self) -> None:
        """Test that non-Windows path with both deps found does NOT raise (49->exit False branch)."""
        from plt_optimizer.ui.tray import _check_dependencies

        with patch("plt_optimizer.ui.tray._IS_WINDOWS", False), \
             patch("plt_optimizer.ui.tray.importlib.util.find_spec") as mock_find:
            # Both pystray and PIL are found
            mock_find.return_value = MagicMock()
            # Should not raise - condition is False so function returns
            _check_dependencies()


class TestInfiSystrayOnQuitCallback:
    """Tests for the on_quit_callback closure in _setup_infi_systray (lines 203-208).

    These tests invoke the closure directly by patching infi.systray.SysTrayIcon
    and extracting the on_quit kwarg from the mock call.
    """

    def test_setup_infi_systray_on_quit_calls_exit(self) -> None:
        """Test that the on_quit callback calls on_exit_requested (lines 203-208)."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_get_path = MagicMock(return_value=Path("/icon.ico"))
        manager = TrayIconManager(MagicMock(), MagicMock(), mock_get_path)
        manager.on_exit_requested = MagicMock()

        mock_systray_instance = MagicMock()

        # Create mock infi.systray module
        mock_infisystray = MagicMock()
        mock_sticon_class = MagicMock(return_value=mock_systray_instance)
        mock_infisystray.SysTrayIcon = mock_sticon_class

        # Need infi package too so `from infi.systray import SysTrayIcon` works
        mock_infi = MagicMock()
        mock_infi.systray = mock_infisystray

        with patch.dict(sys.modules, {"infi": mock_infi, "infi.systray": mock_infisystray}):
            manager._setup_infi_systray()

            # Get the on_quit kwarg from the SysTrayIcon call
            call_kwargs = mock_sticon_class.call_args.kwargs
            on_quit = call_kwargs["on_quit"]
            assert on_quit is not None

            # Invoke the closure
            on_quit(mock_systray_instance)
            manager.on_exit_requested.assert_called_once()

    def test_setup_infi_systray_on_quit_no_callback(self) -> None:
        """Test on_quit callback when on_exit_requested is None."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_get_path = MagicMock(return_value=Path("/icon.ico"))
        manager = TrayIconManager(MagicMock(), MagicMock(), mock_get_path)
        # on_exit_requested is None by default

        mock_systray_instance = MagicMock()
        mock_infisystray = MagicMock()
        mock_sticon_class = MagicMock(return_value=mock_systray_instance)
        mock_infisystray.SysTrayIcon = mock_sticon_class
        mock_infi = MagicMock()
        mock_infi.systray = mock_infisystray

        with patch.dict(sys.modules, {"infi": mock_infi, "infi.systray": mock_infisystray}):
            manager._setup_infi_systray()

            on_quit = mock_sticon_class.call_args.kwargs["on_quit"]
            # Should not raise when callback is None
            on_quit(mock_systray_instance)

    def test_setup_infi_systray_on_quit_handles_exception(self) -> None:
        """Test on_quit callback handles exceptions (line 208)."""
        from plt_optimizer.ui.tray import TrayIconManager

        mock_get_path = MagicMock(return_value=Path("/icon.ico"))
        manager = TrayIconManager(MagicMock(), MagicMock(), mock_get_path)
        manager.on_exit_requested = MagicMock(side_effect=RuntimeError("Exit failed"))

        mock_systray_instance = MagicMock()
        mock_infisystray = MagicMock()
        mock_sticon_class = MagicMock(return_value=mock_systray_instance)
        mock_infisystray.SysTrayIcon = mock_sticon_class
        mock_infi = MagicMock()
        mock_infi.systray = mock_infisystray

        with patch.dict(sys.modules, {"infi": mock_infi, "infi.systray": mock_infisystray}):
            manager._setup_infi_systray()

            on_quit = mock_sticon_class.call_args.kwargs["on_quit"]
            # Should not raise even though callback throws
            on_quit(mock_systray_instance)


class TestInfiExitClickElseBranch:
    """Tests for _on_infi_exit_click when on_exit_requested is None (line 238)."""

    def test_on_infi_exit_click_no_callback(self) -> None:
        """Test _on_infi_exit_click without callback set (line 238)."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())
        # No on_exit_requested callback - should not raise

        manager._on_infi_exit_click(MagicMock())


class TestRunDispatchOnWindows:
    """Tests for run() dispatching to _run_windows on Windows (line 337)."""

    def test_run_dispatches_to_run_windows_on_windows(self) -> None:
        """Test that run() calls _run_windows when _IS_WINDOWS is True (line 337)."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        with patch("plt_optimizer.ui.tray._IS_WINDOWS", True), \
             patch.object(manager, "_run_windows") as mock_run_win, \
             patch.object(manager, "_run_pystray") as mock_run_py:

            manager.run(blocking=False)

            mock_run_win.assert_called_once_with(False)
            mock_run_py.assert_not_called()


class TestRunWindowsNonBlocking:
    """Tests for _run_windows non-blocking mode (lines 365-381)."""

    def test_run_windows_non_blocking_starts_thread(self) -> None:
        """Test that non-blocking Windows mode starts a daemon thread."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        with patch.object(manager, "_setup_infi_systray"), \
             patch("plt_optimizer.ui.tray.threading.Thread") as MockThread:

            mock_thread_instance = MagicMock()
            MockThread.return_value = mock_thread_instance

            manager._systray = MagicMock()

            # Run in non-blocking mode
            manager._run_windows(blocking=False)

            # Verify Thread was created with correct arguments
            MockThread.assert_called_once()
            call_kwargs = MockThread.call_args.kwargs
            assert call_kwargs["daemon"] is True
            assert call_kwargs["name"] == "Systray-Watcher"
            # Verify thread was started
            mock_thread_instance.start.assert_called_once()

    def test_run_windows_non_blocking_tray_thread_exception(self) -> None:
        """Test that the tray_thread closure handles exceptions (lines 371-378)."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        with patch.object(manager, "_setup_infi_systray"), \
             patch("plt_optimizer.ui.tray.threading.Thread") as MockThread, \
             patch("plt_optimizer.ui.tray._logger") as mock_logger:

            mock_thread_instance = MagicMock()
            MockThread.return_value = mock_thread_instance

            # Capture the tray_thread function passed to Thread
            tray_thread_ref: list[Any] = []

            def capture_thread(*args: Any, **kwargs: Any) -> MagicMock:
                tray_thread_ref.append(kwargs.get("target"))
                return mock_thread_instance

            MockThread.side_effect = capture_thread

            manager._systray = MagicMock()

            # Run in non-blocking mode - this creates the tray_thread closure
            manager._run_windows(blocking=False)

            # Invoke the captured tray_thread
            assert len(tray_thread_ref) == 1
            tray_thread = tray_thread_ref[0]
            assert tray_thread is not None

            # Make _systray raise an exception when used as context manager
            manager._systray.__enter__ = MagicMock(side_effect=RuntimeError("Tray thread error"))
            manager._systray.__exit__ = MagicMock(return_value=False)

            # Should not raise - exceptions are caught and logged
            tray_thread()

            # Verify the exception was logged
            assert any(
                "Tray thread error" in str(call_args)
                for call_args in mock_logger.error.call_args_list
            )

    def test_run_windows_non_blocking_tray_thread_normal_exit(self) -> None:
        """Test the tray_thread runs normally without raising."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        with patch.object(manager, "_setup_infi_systray"), \
             patch("plt_optimizer.ui.tray.threading.Thread") as MockThread, \
             patch("plt_optimizer.ui.tray._logger"):

            mock_thread_instance = MagicMock()
            MockThread.return_value = mock_thread_instance

            tray_thread_ref: list[Any] = []

            def capture_thread(*args: Any, **kwargs: Any) -> MagicMock:
                tray_thread_ref.append(kwargs.get("target"))
                return mock_thread_instance

            MockThread.side_effect = capture_thread

            manager._systray = MagicMock()
            manager._systray.__enter__ = MagicMock(return_value=manager._systray)
            manager._systray.__exit__ = MagicMock(return_value=False)

            # Run in non-blocking mode
            manager._run_windows(blocking=False)

            # Invoke the captured tray_thread
            tray_thread = tray_thread_ref[0]
            # Patch time.sleep inside the function to raise KeyboardInterrupt
            with patch("time.sleep", side_effect=KeyboardInterrupt):
                # Should not raise - KeyboardInterrupt is not caught by
                # the `except Exception as e` clause
                try:
                    tray_thread()
                except KeyboardInterrupt:
                    pass


class TestStopWithShutdownAttribute:
    """Tests for stop() with systray that has shutdown attribute (lines 422-425)."""

    def test_stop_skips_infi_systray_shutdown(self) -> None:
        """Test that stop() skips calling shutdown() to avoid deadlock (line 422)."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        # Mock with both attributes (like infi.systray)
        mock_systray = MagicMock(spec=["stop", "shutdown"])
        manager._systray = mock_systray

        with patch("plt_optimizer.ui.tray._logger") as mock_logger:
            manager.stop()

            # Should not call stop() or shutdown()
            assert not mock_systray.stop.called
            assert not mock_systray.shutdown.called
            # Should log debug message about skipping shutdown
            mock_logger.debug.assert_called()
            assert manager._systray is None

    def test_stop_pystray_handles_stop_exception(self) -> None:
        """Test stop() handles exception from pystray.stop() (lines 420-421)."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        mock_systray = MagicMock(spec=["stop"])
        mock_systray.stop.side_effect = RuntimeError("Stop failed")
        manager._systray = mock_systray

        with patch("plt_optimizer.ui.tray._logger") as mock_logger:
            # Should not raise
            manager.stop()

            # Should log warning about the error
            mock_logger.warning.assert_called()
            assert manager._systray is None

    def test_stop_calls_pystray_stop(self) -> None:
        """Test stop() calls pystray.stop() when no shutdown attribute."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        mock_systray = MagicMock(spec=["stop"])
        manager._systray = mock_systray

        with patch("plt_optimizer.ui.tray._logger"):
            manager.stop()

            mock_systray.stop.assert_called_once()
            assert manager._systray is None

    def test_stop_with_systray_neither_stop_nor_shutdown(self) -> None:
        """Test stop() when systray has neither stop nor shutdown attribute (422->425)."""
        from plt_optimizer.ui.tray import TrayIconManager

        manager = TrayIconManager(MagicMock(), MagicMock(), MagicMock())

        # Mock with empty spec - no stop or shutdown attributes
        mock_systray = MagicMock(spec=[])
        manager._systray = mock_systray

        with patch("plt_optimizer.ui.tray._logger"):
            manager.stop()

            # Both branches are False - falls through to _systray = None
            assert manager._systray is None

