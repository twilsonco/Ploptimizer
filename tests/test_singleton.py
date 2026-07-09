"""Tests for singleton instance enforcement via Windows mutex.

This module tests the mutex-based singleton enforcement that prevents multiple
instances of PLT-Optimizer from running simultaneously, which would otherwise
cause infinite spawning, resource exhaustion, and system crashes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestSingletonMutex:
    """Test mutex-based singleton enforcement in run_tray.py."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    def test_first_instance_acquires_mutex(self) -> None:
        """Test that the first instance successfully acquires the mutex."""
        with patch("win32event.CreateMutex") as mock_create_mutex, \
             patch("win32api.GetLastError") as mock_get_last_error:

            # Mock successful mutex creation (no existing instance)
            mock_mutex = MagicMock()
            mock_create_mutex.return_value = mock_mutex
            mock_get_last_error.return_value = 0  # Not ERROR_ALREADY_EXISTS

            # Import and run main (will be mocked to avoid full execution)
            with patch("plt_optimizer.ui.tray.TrayIconManager"), \
                 patch("plt_optimizer.utils.config.load_config", return_value={"watch_dir": "/tmp"}), \
                 patch("plt_optimizer.cli.watch.run_watcher_from_config"):

                from run_tray import main

                # Patch sys.argv to avoid argparse conflicts
                with patch("sys.argv", ["run_tray.py"]):
                    # Mock the tray manager's run to prevent blocking
                    with patch("plt_optimizer.ui.tray.TrayIconManager.run", side_effect=KeyboardInterrupt):
                        exit_code = main()

                # First instance should succeed (or exit via KeyboardInterrupt)
                assert exit_code in (0, 1)  # Either clean exit or interrupted

                # Verify mutex was created with correct name
                mock_create_mutex.assert_called_once_with(
                    None, False, "Global\\PLT-Optimizer-SingleInstance-Mutex"
                )

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    def test_second_instance_detects_existing_mutex(self) -> None:
        """Test that a second instance detects the existing mutex and exits."""
        with patch("win32event.CreateMutex") as mock_create_mutex, \
             patch("win32api.GetLastError") as mock_get_last_error, \
             patch("win32api.CloseHandle") as mock_close_handle:

            from winerror import ERROR_ALREADY_EXISTS  # type: ignore[import-untyped]

            # Mock mutex creation that detects existing instance
            mock_mutex = MagicMock()
            mock_create_mutex.return_value = mock_mutex
            mock_get_last_error.return_value = ERROR_ALREADY_EXISTS

            # Mock tkinter to avoid GUI during test
            with patch("tkinter.Tk") as mock_tk, \
                 patch("tkinter.messagebox.showwarning") as mock_showwarning:

                mock_root = MagicMock()
                mock_tk.return_value = mock_root

                from run_tray import main

                # Patch sys.argv
                with patch("sys.argv", ["run_tray.py"]):
                    exit_code = main()

                # Second instance should exit with code 1
                assert exit_code == 1

                # Verify mutex was created (even though it already existed)
                mock_create_mutex.assert_called_once_with(
                    None, False, "Global\\PLT-Optimizer-SingleInstance-Mutex"
                )

                # Verify warning dialog was shown
                mock_showwarning.assert_called_once()
                assert "already running" in mock_showwarning.call_args[0][1].lower()

                # Verify mutex was cleaned up
                mock_close_handle.assert_called_once_with(mock_mutex)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    def test_mutex_released_on_exit(self) -> None:
        """Test that mutex is released when application exits normally."""
        with patch("win32event.CreateMutex") as mock_create_mutex, \
             patch("win32api.GetLastError") as mock_get_last_error, \
             patch("win32api.CloseHandle") as mock_close_handle:

            mock_mutex = MagicMock()
            mock_create_mutex.return_value = mock_mutex
            mock_get_last_error.return_value = 0

            with patch("plt_optimizer.ui.tray.TrayIconManager"), \
                 patch("plt_optimizer.utils.config.load_config", return_value={"watch_dir": "/tmp"}), \
                 patch("plt_optimizer.cli.watch.run_watcher_from_config"):

                from run_tray import main

                with patch("sys.argv", ["run_tray.py"]):
                    # Mock tray to exit cleanly after start
                    with patch("plt_optimizer.ui.tray.TrayIconManager.run", return_value=None):
                        exit_code = main()

                assert exit_code == 0

                # Verify mutex was released on exit
                mock_close_handle.assert_called()
                # Should be called with the mutex handle
                assert mock_mutex in [call[0][0] for call in mock_close_handle.call_args_list]

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    def test_mutex_uses_global_namespace(self) -> None:
        """Test that mutex uses Global\\ namespace for cross-session protection."""
        with patch("win32event.CreateMutex") as mock_create_mutex, \
             patch("win32api.GetLastError") as mock_get_last_error:

            mock_mutex = MagicMock()
            mock_create_mutex.return_value = mock_mutex
            mock_get_last_error.return_value = 0

            with patch("plt_optimizer.ui.tray.TrayIconManager"), \
                 patch("plt_optimizer.utils.config.load_config", return_value={"watch_dir": "/tmp"}), \
                 patch("plt_optimizer.cli.watch.run_watcher_from_config"):

                from run_tray import main

                with patch("sys.argv", ["run_tray.py"]):
                    with patch("plt_optimizer.ui.tray.TrayIconManager.run", side_effect=KeyboardInterrupt):
                        main()

                # Verify Global\\ prefix is used (not Local\\)
                mutex_name = mock_create_mutex.call_args[0][2]
                assert mutex_name.startswith("Global\\")
                assert "PLT-Optimizer" in mutex_name

    def test_non_windows_platforms_skip_mutex(self) -> None:
        """Test that non-Windows platforms don't attempt mutex creation."""
        # This test runs on all platforms
        mock_config = {"watch_dir": "/tmp", "first_run": False}

        with patch("sys.platform", "linux"), \
             patch("plt_optimizer.ui.tray.TrayIconManager"), \
             patch("plt_optimizer.utils.config.load_config", return_value=mock_config), \
             patch("plt_optimizer.cli.watch.run_watcher_from_config"):

            # win32event should not be imported on non-Windows
            with patch("sys.argv", ["run_tray.py"]):
                # Mock TrayIconManager's run method to avoid blocking
                with patch("plt_optimizer.ui.tray.TrayIconManager.run", side_effect=KeyboardInterrupt):
                    from run_tray import main
                    exit_code = main()

            # Should run without attempting mutex (exit via interrupt is normal)
            assert exit_code in (0, 1)


class TestStartupArgument:
    """Test --started-from-startup argument parsing and logging."""

    def test_started_from_startup_flag_parsed(self) -> None:
        """Test that --started-from-startup flag is parsed correctly."""
        mock_config = {"watch_dir": "/tmp", "first_run": False}

        with patch("plt_optimizer.ui.tray.TrayIconManager"), \
             patch("plt_optimizer.utils.config.load_config", return_value=mock_config), \
             patch("plt_optimizer.cli.watch.run_watcher_from_config"), \
             patch("run_tray.logger") as mock_logger:

            # Mock Windows mutex to avoid platform-specific code
            if sys.platform == "win32":
                with patch("win32event.CreateMutex"), \
                     patch("win32api.GetLastError", return_value=0):
                    from run_tray import main

                    with patch("sys.argv", ["run_tray.py", "--started-from-startup"]):
                        with patch("plt_optimizer.ui.tray.TrayIconManager.run", side_effect=KeyboardInterrupt):
                            main()
            else:
                from run_tray import main

                with patch("sys.argv", ["run_tray.py", "--started-from-startup"]):
                    with patch("plt_optimizer.ui.tray.TrayIconManager.run", side_effect=KeyboardInterrupt):
                        main()

            # Verify logging includes "launched from Windows Startup"
            info_calls = [str(call) for call in mock_logger.info.call_args_list]
            assert any("launched from Windows Startup" in str(call) for call in info_calls)

    def test_manual_launch_logged(self) -> None:
        """Test that manual launches (no flag) are logged correctly."""
        mock_config = {"watch_dir": "/tmp", "first_run": False}

        with patch("plt_optimizer.ui.tray.TrayIconManager"), \
             patch("plt_optimizer.utils.config.load_config", return_value=mock_config), \
             patch("plt_optimizer.cli.watch.run_watcher_from_config"), \
             patch("run_tray.logger") as mock_logger:

            # Mock Windows mutex
            if sys.platform == "win32":
                with patch("win32event.CreateMutex"), \
                     patch("win32api.GetLastError", return_value=0):
                    from run_tray import main

                    with patch("sys.argv", ["run_tray.py"]):
                        with patch("plt_optimizer.ui.tray.TrayIconManager.run", side_effect=KeyboardInterrupt):
                            main()
            else:
                from run_tray import main

                with patch("sys.argv", ["run_tray.py"]):
                    with patch("plt_optimizer.ui.tray.TrayIconManager.run", side_effect=KeyboardInterrupt):
                        main()

            # Verify logging includes "launched manually"
            info_calls = [str(call) for call in mock_logger.info.call_args_list]
            assert any("launched manually" in str(call) for call in info_calls)


class TestShortcutArgument:
    """Test that startup shortcut includes --started-from-startup argument."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    def test_shortcut_includes_startup_argument(self) -> None:
        """Test that create_shortcut sets Arguments property."""
        from plt_optimizer.utils.startup import create_shortcut

        mock_shortcut = MagicMock()
        mock_shell = MagicMock()
        mock_shell.CreateShortcut.return_value = mock_shortcut

        with patch("win32com.client.Dispatch", return_value=mock_shell), \
             patch("plt_optimizer.utils.startup.get_startup_folder", return_value=Path("/tmp")), \
             patch("plt_optimizer.utils.startup.get_executable_path", return_value=Path("/tmp/app.exe")):

            create_shortcut()

            # Verify Arguments property was set
            assert mock_shortcut.Arguments == "--started-from-startup"

            # Verify shortcut was saved
            mock_shortcut.Save.assert_called_once()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    def test_shortcut_other_properties_preserved(self) -> None:
        """Test that other shortcut properties are still set correctly."""
        from plt_optimizer.utils.startup import create_shortcut

        mock_shortcut = MagicMock()
        mock_shell = MagicMock()
        mock_shell.CreateShortcut.return_value = mock_shortcut

        target_path = Path("/tmp/PLT-Optimizer.exe")

        with patch("win32com.client.Dispatch", return_value=mock_shell), \
             patch("plt_optimizer.utils.startup.get_startup_folder", return_value=Path("/tmp")), \
             patch("plt_optimizer.utils.startup.get_executable_path", return_value=target_path):

            create_shortcut()

            # Verify all properties are set
            assert mock_shortcut.TargetPath == str(target_path)
            assert mock_shortcut.WorkingDirectory == str(target_path.parent)
            assert mock_shortcut.WindowStyle == 7  # Minimized
            assert "PLT-Optimizer" in mock_shortcut.Description
            assert mock_shortcut.Arguments == "--started-from-startup"
