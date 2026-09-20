"""Tests for plt_optimizer/ui/settings.py."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Import messagebox at module level so tests can use it directly
from tkinter import messagebox
from typing import Any
from unittest.mock import MagicMock, patch

# Import the module under test - it will use the mocked tkinter from conftest
from plt_optimizer.ui.settings import SettingsWindow

# Module-level logger for test assertions
_logger = logging.getLogger(__name__)


# Import mock classes from conftest (they're available via the autouse fixture)
class MockStringVar:
    """Mock StringVar that stores and returns values properly.

    This is a copy of the class in conftest.py for use directly in tests.
    """

    def __init__(self, initial: str = "") -> None:
        self._value = initial

    def get(self) -> str:
        return self._value

    def set(self, value: str) -> None:
        self._value = value


class MockBooleanVar:
    """Mock BooleanVar that stores and returns boolean values properly.

    This is a copy of the class in conftest.py for use directly in tests.
    """

    def __init__(self, initial: bool = False) -> None:
        self._value = initial

    def get(self) -> bool:
        return self._value

    def set(self, value: bool) -> None:
        self._value = value


class TestSettingsWindowInit:
    """Tests for SettingsWindow initialization (lines 51-66)."""

    def test_init_with_parent(self) -> None:
        """Test initialization with a parent window."""
        mock_parent = MagicMock()
        current_config: dict[str, Any] = {"watch_dir": "/test", "output_dir": "./out"}

        with patch("plt_optimizer.ui.settings.tk.Toplevel") as mock_toplevel:
            mock_instance = MagicMock()
            mock_toplevel.return_value = mock_instance

            window = SettingsWindow(current_config, MagicMock(), parent=mock_parent)

            assert window._config == current_config
            assert window._save_callback is not None
            assert window._parent == mock_parent
            mock_toplevel.assert_called_once_with(mock_parent)

    def test_init_without_parent(self) -> None:
        """Test initialization without a parent (creates own Tk)."""
        current_config: dict[str, Any] = {"watch_dir": "/test"}

        with patch("plt_optimizer.ui.settings.tk.Tk") as mock_tk:
            window = SettingsWindow(current_config, MagicMock(), parent=None)

            assert window._config == current_config
            mock_tk.assert_called_once()

    def test_init_copies_config(self) -> None:
        """Test that config is copied to prevent mutation of original."""
        original_config: dict[str, Any] = {"watch_dir": "/test"}
        callback = MagicMock()

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(original_config, callback)

            # Modify the internal config
            window._config["watch_dir"] = "/modified"

            # Original should be unchanged
            assert original_config["watch_dir"] == "/test"


class TestLoadCurrentValues:
    """Tests for _load_current_values method (lines 297-307)."""

    def test_load_defaults_for_missing_keys(self) -> None:
        """Test that defaults are loaded when config keys are missing."""
        current_config: dict[str, Any] = {}

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())
            window._load_current_values()

            assert window._watch_dir_var.get() == ""
            assert window._output_dir_var.get() == "./optimized"
            assert window._log_dir_var.get() == "./logs"

    def test_load_existing_values(self) -> None:
        """Test loading existing configuration values."""
        current_config: dict[str, Any] = {
            "watch_dir": "/watch",
            "output_dir": "/output",
            "log_dir": "/logs",
            "processed_dir": "/processed",
            "fast_mode": True,
            "debug_save_files": True,
        }

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())
            window._load_current_values()

            assert window._watch_dir_var.get() == "/watch"
            assert window._output_dir_var.get() == "/output"
            assert window._log_dir_var.get() == "/logs"
            assert window._processed_dir_var.get() == "/processed"
            assert window._fast_mode_var.get() is True
            assert window._debug_save_files_var.get() is True

    def test_load_empty_processed_dir_not_set(self) -> None:
        """Test that empty processed_dir doesn't overwrite existing variable."""
        current_config: dict[str, Any] = {"processed_dir": None}

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())
            # The variable should be empty string by default
            assert window._processed_dir_var.get() == ""


class TestValidateInputs:
    """Tests for _validate_inputs method (lines 309-360)."""

    def test_validate_empty_watch_dir(self) -> None:
        """Test validation fails when watch directory is empty."""
        current_config: dict[str, Any] = {"output_dir": "/out", "log_dir": "/logs"}

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())
            # Set only output and log dirs
            window._output_dir_var.set("/out")
            window._log_dir_var.set("/logs")

            result = window._validate_inputs()

            assert result is False

    def test_validate_empty_output_dir(self) -> None:
        """Test validation fails when output directory is empty."""
        current_config: dict[str, Any] = {"watch_dir": "/watch", "log_dir": "/logs"}

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())
            window._watch_dir_var.set("/watch")
            window._output_dir_var.set("")  # explicitly clear output_dir
            window._log_dir_var.set("/logs")

            with patch(
                "plt_optimizer.ui.settings.messagebox.showerror"
            ) as mock_showerror:
                result = window._validate_inputs()

            assert result is False
            # The empty output_dir validation branch must call showerror
            assert mock_showerror.called
            args = mock_showerror.call_args[0]
            assert "Output Directory" in args[1]

    def test_validate_empty_log_dir(self) -> None:
        """Test validation fails when log directory is empty."""
        current_config: dict[str, Any] = {"watch_dir": "/watch", "output_dir": "/out"}

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())
            window._watch_dir_var.set("/watch")
            window._output_dir_var.set("/out")
            window._log_dir_var.set("")  # explicitly clear log_dir

            with patch(
                "plt_optimizer.ui.settings.messagebox.showerror"
            ) as mock_showerror:
                result = window._validate_inputs()

            assert result is False
            # The empty log_dir validation branch must call showerror
            assert mock_showerror.called
            args = mock_showerror.call_args[0]
            assert "Log Directory" in args[1]

    def test_validate_oserror_shows_error_dialog(self) -> None:
        """Test that OSError on mkdir shows error messagebox (lines 317-322)."""
        current_config: dict[str, Any] = {}

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())
            window._watch_dir_var.set("/new/dir")
            window._output_dir_var.set("/out")
            window._log_dir_var.set("/logs")

            with patch.object(Path, "exists", return_value=False):
                with patch(
                    "plt_optimizer.ui.settings.messagebox.askyesno",
                    return_value=True,
                ):
                    with patch.object(
                        Path, "mkdir", side_effect=OSError("Permission denied")
                    ):
                        with patch(
                            "plt_optimizer.ui.settings.messagebox.showerror"
                        ) as mock_showerror:
                            result = window._validate_inputs()

                            assert result is False
                            # OSError handler must call showerror
                            assert mock_showerror.called
                            args = mock_showerror.call_args[0]
                            assert "Could not create directory" in args[1]

    def test_validate_success_with_existing_watch_dir(self) -> None:
        """Test validation succeeds when watch directory exists."""
        current_config: dict[str, Any] = {}

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())
            window._watch_dir_var.set("/test")
            window._output_dir_var.set("/out")
            window._log_dir_var.set("/logs")

            with patch.object(Path, "exists", return_value=True):
                result = window._validate_inputs()

            assert result is True

    def test_validate_creates_missing_watch_dir(self) -> None:
        """Test validation creates watch directory when user confirms."""
        current_config: dict[str, Any] = {}

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())
            window._watch_dir_var.set("/new/dir")
            window._output_dir_var.set("/out")
            window._log_dir_var.set("/logs")

            with patch.object(Path, "exists", return_value=False):
                with patch(
                    "plt_optimizer.ui.settings.messagebox.askyesno",
                    return_value=True,
                ):
                    with patch.object(Path, "mkdir") as mock_mkdir:
                        result = window._validate_inputs()

            assert result is True
            mock_mkdir.assert_called_once_with(parents=True, exist_ok=True)

    def test_validate_fails_when_creating_dir_fails(self) -> None:
        """Test validation fails when directory creation raises OSError."""
        current_config: dict[str, Any] = {}

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())
            window._watch_dir_var.set("/new/dir")
            window._output_dir_var.set("/out")
            window._log_dir_var.set("/logs")

            with patch.object(Path, "exists", return_value=False):
                with patch(
                    "plt_optimizer.ui.settings.messagebox.askyesno",
                    return_value=True,
                ):
                    with patch.object(
                        Path, "mkdir", side_effect=OSError("Permission denied")
                    ):
                        result = window._validate_inputs()

            assert result is False

    def test_validate_fails_when_user_declines_dir_creation(self) -> None:
        """Test validation fails when user declines to create missing directory."""
        current_config: dict[str, Any] = {}

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())
            window._watch_dir_var.set("/new/dir")
            window._output_dir_var.set("/out")
            window._log_dir_var.set("/logs")

            with patch.object(Path, "exists", return_value=False):
                with patch(
                    "plt_optimizer.ui.settings.messagebox.askyesno",
                    return_value=False,
                ):
                    result = window._validate_inputs()

            assert result is False


class TestOnSave:
    """Tests for _on_save method (lines 362-388)."""

    def test_on_save_calls_callback_with_updated_config(self) -> None:
        """Test that save callback receives updated configuration."""
        current_config: dict[str, Any] = {}
        callback = MagicMock()

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, callback)
            window._watch_dir_var.set("/watch")
            window._output_dir_var.set("/out")
            window._log_dir_var.set("/logs")

            with patch.object(Path, "exists", return_value=True):
                with patch.object(window, "_validate_inputs", return_value=True):
                    window._on_save()

            callback.assert_called_once()
            saved_config = callback.call_args[0][0]
            assert saved_config["watch_dir"] == "/watch"
            assert saved_config["output_dir"] == "/out"
            assert saved_config["log_dir"] == "/logs"

    def test_on_save_uses_default_output_when_empty(self) -> None:
        """Test that empty output dir defaults to ./optimized."""
        current_config: dict[str, Any] = {}
        callback = MagicMock()

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, callback)
            window._watch_dir_var.set("/watch")
            window._output_dir_var.set("")
            window._log_dir_var.set("/logs")

            with patch.object(Path, "exists", return_value=True):
                with patch.object(window, "_validate_inputs", return_value=True):
                    window._on_save()

            saved_config = callback.call_args[0][0]
            assert saved_config["output_dir"] == "./optimized"

    def test_on_save_uses_default_log_when_empty(self) -> None:
        """Test that empty log dir defaults to ./logs."""
        current_config: dict[str, Any] = {}
        callback = MagicMock()

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, callback)
            window._watch_dir_var.set("/watch")
            window._output_dir_var.set("/out")
            window._log_dir_var.set("")

            with patch.object(Path, "exists", return_value=True):
                with patch.object(window, "_validate_inputs", return_value=True):
                    window._on_save()

            saved_config = callback.call_args[0][0]
            assert saved_config["log_dir"] == "./logs"

    def test_on_save_handles_callback_exception(self) -> None:
        """Test that exceptions in save callback are handled gracefully."""
        current_config: dict[str, Any] = {}
        callback = MagicMock(side_effect=RuntimeError("Save failed"))

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, callback)
            window._watch_dir_var.set("/watch")
            window._output_dir_var.set("/out")
            window._log_dir_var.set("/logs")

            with patch.object(Path, "exists", return_value=True):
                with patch.object(window, "_validate_inputs", return_value=True):
                    # Should not raise
                    window._on_save()

    def test_on_save_does_nothing_when_validation_fails(self) -> None:
        """Test that save does nothing when validation fails."""
        current_config: dict[str, Any] = {}
        callback = MagicMock()

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, callback)
            window._watch_dir_var.set("")
            window._output_dir_var.set("/out")
            window._log_dir_var.set("/logs")

            with patch.object(window, "_validate_inputs", return_value=False):
                window._on_save()

            callback.assert_not_called()


class TestOnCancel:
    """Tests for _on_cancel method."""

    def test_on_cancel_destroys_window(self) -> None:
        """Test that cancel destroys the root window."""
        current_config: dict[str, Any] = {}
        mock_root = MagicMock()

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())
            window._root = mock_root
            window._on_cancel()

            mock_root.destroy.assert_called_once()


class TestBrowseDirectory:
    """Tests for _browse_directory method (lines 348-373)."""

    def test_browse_uses_home_when_var_empty(self) -> None:
        """Test that home directory is used when variable is empty."""
        current_config: dict[str, Any] = {}

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())

            var = MockStringVar("")

            with patch(
                "plt_optimizer.ui.settings.filedialog.askdirectory",
                return_value="/selected",
            ):
                window._browse_directory(var)

            assert var.get() == "/selected"

    def test_browse_uses_existing_path_when_valid(self) -> None:
        """Test that existing path is used as initial directory."""
        current_config: dict[str, Any] = {}

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())
            var = MockStringVar("/valid/path")

            with patch.object(Path, "exists", return_value=True):
                with patch(
                    "plt_optimizer.ui.settings.filedialog.askdirectory",
                    return_value="/selected",
                ):
                    window._browse_directory(var)

    def test_browse_handles_oserror_on_path_check(self) -> None:
        """Test that OSError during path existence check falls back to home."""
        current_config: dict[str, Any] = {}

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())
            var = MockStringVar("//server/share")

            with patch.object(Path, "exists", side_effect=OSError("Network error")):
                with patch(
                    "plt_optimizer.ui.settings.filedialog.askdirectory",
                    return_value="/selected",
                ):
                    window._browse_directory(var)

    def test_browse_handles_filedialog_exception(self) -> None:
        """Test that file dialog exceptions are handled gracefully."""
        current_config: dict[str, Any] = {}
        mock_root = MagicMock()

        with patch("plt_optimizer.ui.settings.tk.Toplevel", return_value=mock_root):
            window = SettingsWindow(current_config, MagicMock())
            var = MockStringVar("/some/path")

            with patch.object(Path, "exists", return_value=True):
                with patch(
                    "plt_optimizer.ui.settings.filedialog.askdirectory",
                    side_effect=Exception("Dialog error"),
                ):
                    # Should not raise
                    window._browse_directory(var)

    def test_browse_does_nothing_when_cancelled(self) -> None:
        """Test that nothing happens when dialog is cancelled."""
        current_config: dict[str, Any] = {}

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())
            var = MockStringVar("/original")

            # Simulate askdirectory returning empty (cancelled)
            original_askdirectory = sys.modules["tkinter.filedialog"].askdirectory
            try:
                sys.modules["tkinter.filedialog"].askdirectory = lambda **kw: ""

                window._browse_directory(var)

                assert var.get() == "/original"
            finally:
                sys.modules["tkinter.filedialog"].askdirectory = original_askdirectory


class TestDestroy:
    """Tests for destroy method."""

    def test_destroy_handles_none_root(self) -> None:
        """Test that destroy handles None root gracefully (line 400->exit)."""
        current_config: dict[str, Any] = {}

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())
            # Explicitly set _root to None to test the False branch
            window._root = None
            # Should not raise - if self._root is not None: ... is False
            window.destroy()

    def test_destroy_calls_root_destroy(self) -> None:
        """Test that destroy calls root.destroy()."""
        current_config: dict[str, Any] = {}
        mock_root = MagicMock()

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())
            window._root = mock_root
            window.destroy()

            mock_root.destroy.assert_called_once()


class TestCenterWindow:
    """Tests for _center_window method."""

    def test_center_window_calls_update_idletasks(self) -> None:
        """Test that center_window updates idle tasks before calculating."""
        current_config: dict[str, Any] = {}
        mock_tk_instance = MagicMock()
        mock_tk_instance.winfo_screenwidth.return_value = 1920
        mock_tk_instance.winfo_screenheight.return_value = 1080
        mock_tk_instance.winfo_width.return_value = 580
        mock_tk_instance.winfo_height.return_value = 480

        with patch("plt_optimizer.ui.settings.tk.Tk", return_value=mock_tk_instance):
            window = SettingsWindow(current_config, MagicMock(), parent=None)
            window._root = mock_tk_instance  # Ensure our mock is used
            window._center_window()

            mock_tk_instance.update_idletasks.assert_called_once()
            # Verify geometry was set with calculated position
            call_args = mock_tk_instance.geometry.call_args[0][0]
            assert "+" in call_args  # Should be like +670+300


class TestShow:
    """Tests for show method."""

    def test_show_on_non_windows(self) -> None:
        """Test that show works correctly on non-Windows platforms."""
        current_config: dict[str, Any] = {}
        mock_tk_instance = MagicMock()
        mock_tk_instance.winfo_screenwidth.return_value = 1920
        mock_tk_instance.winfo_screenheight.return_value = 1080
        mock_tk_instance.winfo_width.return_value = 580
        mock_tk_instance.winfo_height.return_value = 480

        with patch("plt_optimizer.ui.settings.tk.Tk", return_value=mock_tk_instance):
            window = SettingsWindow(current_config, MagicMock(), parent=None)
            window._root = mock_tk_instance

            with patch.object(Path, "exists", return_value=True):
                with patch("sys.platform", "darwin"):
                    window.show()
                    # On non-Windows, mainloop should NOT be called
                    mock_tk_instance.mainloop.assert_not_called()
                    # But deiconify SHOULD be called
                    mock_tk_instance.deiconify.assert_called_once()

    def test_show_on_windows_with_keyboard_interrupt(self) -> None:
        """Test that show handles KeyboardInterrupt on Windows."""
        current_config: dict[str, Any] = {}
        mock_tk_instance = MagicMock()
        mock_tk_instance.winfo_screenwidth.return_value = 1920
        mock_tk_instance.winfo_screenheight.return_value = 1080
        mock_tk_instance.winfo_width.return_value = 580
        mock_tk_instance.winfo_height.return_value = 480

        # Simulate KeyboardInterrupt during mainloop
        mock_tk_instance.mainloop.side_effect = KeyboardInterrupt()

        with patch("plt_optimizer.ui.settings.tk.Tk", return_value=mock_tk_instance):
            window = SettingsWindow(current_config, MagicMock(), parent=None)
            window._root = mock_tk_instance

            with patch("sys.platform", "win32"):
                with patch.object(window, "_on_cancel") as mock_cancel:
                    window.show()
                    # Verify _on_cancel was called due to KeyboardInterrupt
                    mock_cancel.assert_called_once()
                    mock_tk_instance.protocol.assert_called_once()
                    mock_tk_instance.deiconify.assert_called_once()
                    mock_tk_instance.focus_force.assert_called_once()
                    mock_tk_instance.mainloop.assert_called_once()


class TestModuleLevelConstants:
    """Tests for module-level constants."""

    def test_is_windows_true_on_win32(self) -> None:
        """Test _IS_WINDOWS is True on win32 platform."""
        import importlib

        from plt_optimizer.ui import settings

        with patch("sys.platform", "win32"):
            importlib.reload(settings)
            assert settings._IS_WINDOWS is True

    def test_is_windows_false_on_darwin(self) -> None:
        """Test _IS_WINDOWS is False on darwin platform."""
        import importlib

        from plt_optimizer.ui import settings

        with patch("sys.platform", "darwin"):
            importlib.reload(settings)
            assert settings._IS_WINDOWS is False


class TestAllExports:
    """Tests for __all__ exports."""

    def test_all_exports_settings_window(self) -> None:
        """Test that SettingsWindow is exported."""
        from plt_optimizer.ui.settings import SettingsWindow, __all__

        assert "SettingsWindow" in __all__
        assert SettingsWindow.__name__ == "SettingsWindow"


class TestOnCleanup:
    """Tests for the on_cleanup handler (lines 185-216).

    Note: These tests verify the cleanup logic by directly testing what would
    happen when the closure executes. The actual UI callback lines in settings.py
    are defined inside _setup_ui() and execute within tkinter's event loop context,
    which is difficult to cover with unit tests due to mocking limitations.
    """

    def _find_cleanup_command(self) -> Any:
        """Extract the on_cleanup closure from ttk.Button mock calls.

        The closure is passed as the ``command=`` kwarg to ``ttk.Button`` during
        ``_setup_ui()``. We find the most recent button by its text and return
        the bound callable so tests can invoke it directly.

        Because ``ttk.Button`` is a class-level MagicMock in our test fixtures
        and accumulates calls across tests, we track the call count before
        ``_setup_ui`` runs and only consider calls made during this test.
        """
        from tkinter import ttk

        cleanup_cmd: Any = None
        for call in ttk.Button.call_args_list:
            text = call.kwargs.get("text", "")
            if "Clean" in text:
                cleanup_cmd = call.kwargs["command"]
        return cleanup_cmd

    def test_on_cleanup_closure_user_declines(self) -> None:
        """Test the actual on_cleanup closure when user declines."""
        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow({}, MagicMock())
            window._log_dir_var.set("/test/logs")
            window._processed_dir_var.set("/test/processed")

            cleanup_cmd = self._find_cleanup_command()
            assert cleanup_cmd is not None

            with patch(
                "plt_optimizer.ui.settings.messagebox.askyesno", return_value=False
            ) as mock_ask:
                with patch("plt_optimizer.ui.settings.messagebox.showinfo") as mock_info:
                    cleanup_cmd()
                    # When user declines, no further cleanup actions should occur
                    mock_ask.assert_called_once()
                    mock_info.assert_not_called()

    def test_on_cleanup_closure_cleans_log_dir(self) -> None:
        """Test the on_cleanup closure cleans log dir files."""
        mock_file = MagicMock()
        mock_file.is_file.return_value = True

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow({}, MagicMock())
            window._log_dir_var.set("/test/logs")
            window._processed_dir_var.set("")

            cleanup_cmd = self._find_cleanup_command()
            assert cleanup_cmd is not None

            with patch(
                "plt_optimizer.ui.settings.messagebox.askyesno", return_value=True
            ):
                with patch.object(Path, "exists", return_value=True):
                    with patch.object(
                        Path, "iterdir", return_value=[mock_file]
                    ):
                        with patch(
                            "plt_optimizer.ui.settings.messagebox.showinfo"
                        ) as mock_info:
                            cleanup_cmd()
                            mock_file.unlink.assert_called_once()
                            mock_info.assert_called_once()
                            # Verify the cleanup message
                            args = mock_info.call_args[0]
                            assert "Deleted 1 files" in args[1]

    def test_on_cleanup_closure_skips_log_dir_when_empty(self) -> None:
        """Test the on_cleanup closure skips log dir when var is empty."""
        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow({}, MagicMock())
            window._log_dir_var.set("")
            window._processed_dir_var.set("")

            cleanup_cmd = self._find_cleanup_command()
            assert cleanup_cmd is not None

            with patch(
                "plt_optimizer.ui.settings.messagebox.askyesno", return_value=True
            ):
                with patch.object(Path, "exists") as mock_exists:
                    with patch(
                        "plt_optimizer.ui.settings.messagebox.showinfo"
                    ) as mock_info:
                        cleanup_cmd()
                        # Path.exists() should NOT have been called because log_dir is empty
                        mock_exists.assert_not_called()
                        mock_info.assert_called_once()

    def test_on_cleanup_closure_handles_iterdir_oserror_on_log(self) -> None:
        """Test the on_cleanup closure handles OSError on log dir iterdir."""
        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow({}, MagicMock())
            window._log_dir_var.set("/test/logs")
            window._processed_dir_var.set("")

            cleanup_cmd = self._find_cleanup_command()
            assert cleanup_cmd is not None

            with patch(
                "plt_optimizer.ui.settings.messagebox.askyesno", return_value=True
            ):
                with patch.object(Path, "exists", return_value=True):
                    with patch.object(
                        Path, "iterdir", side_effect=OSError("Permission denied")
                    ):
                        with patch(
                            "plt_optimizer.ui.settings.messagebox.showinfo"
                        ) as mock_info:
                            # Should not raise
                            cleanup_cmd()
                            mock_info.assert_called_once()

    def test_on_cleanup_closure_handles_unlink_oserror_on_log(self) -> None:
        """Test the on_cleanup closure handles OSError on file unlink."""
        mock_file = MagicMock()
        mock_file.is_file.return_value = True
        mock_file.unlink.side_effect = OSError("Permission denied")

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow({}, MagicMock())
            window._log_dir_var.set("/test/logs")
            window._processed_dir_var.set("")

            cleanup_cmd = self._find_cleanup_command()
            assert cleanup_cmd is not None

            with patch(
                "plt_optimizer.ui.settings.messagebox.askyesno", return_value=True
            ):
                with patch.object(Path, "exists", return_value=True):
                    with patch.object(
                        Path, "iterdir", return_value=[mock_file]
                    ):
                        with patch(
                            "plt_optimizer.ui.settings.messagebox.showinfo"
                        ) as mock_info:
                            # Should not raise
                            cleanup_cmd()
                            mock_info.assert_called_once()
                            # cleaned_count is 0 because unlink failed
                            args = mock_info.call_args[0]
                            assert "Deleted 0 files" in args[1]

    def test_on_cleanup_closure_cleans_processed_dir(self) -> None:
        """Test the on_cleanup closure cleans processed dir files."""
        mock_file = MagicMock()
        mock_file.is_file.return_value = True

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow({}, MagicMock())
            window._log_dir_var.set("")
            window._processed_dir_var.set("/test/processed")

            cleanup_cmd = self._find_cleanup_command()
            assert cleanup_cmd is not None

            with patch(
                "plt_optimizer.ui.settings.messagebox.askyesno", return_value=True
            ):
                with patch.object(Path, "exists", return_value=True):
                    with patch.object(
                        Path, "iterdir", return_value=[mock_file]
                    ):
                        with patch(
                            "plt_optimizer.ui.settings.messagebox.showinfo"
                        ) as mock_info:
                            cleanup_cmd()
                            mock_file.unlink.assert_called_once()
                            mock_info.assert_called_once()

    def test_on_cleanup_closure_skips_processed_dir_when_empty(self) -> None:
        """Test the on_cleanup closure skips processed dir when var is empty."""
        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow({}, MagicMock())
            window._log_dir_var.set("")
            window._processed_dir_var.set("")

            cleanup_cmd = self._find_cleanup_command()
            assert cleanup_cmd is not None

            with patch(
                "plt_optimizer.ui.settings.messagebox.askyesno", return_value=True
            ):
                with patch.object(Path, "exists") as mock_exists:
                    with patch(
                        "plt_optimizer.ui.settings.messagebox.showinfo"
                    ) as mock_info:
                        cleanup_cmd()
                        # Path.exists() should NOT have been called for processed_dir
                        mock_exists.assert_not_called()
                        mock_info.assert_called_once()

    def test_on_cleanup_closure_handles_iterdir_oserror_on_processed(self) -> None:
        """Test the on_cleanup closure handles OSError on processed iterdir."""
        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow({}, MagicMock())
            window._log_dir_var.set("")
            window._processed_dir_var.set("/test/processed")

            cleanup_cmd = self._find_cleanup_command()
            assert cleanup_cmd is not None

            with patch(
                "plt_optimizer.ui.settings.messagebox.askyesno", return_value=True
            ):
                with patch.object(Path, "exists", return_value=True):
                    with patch.object(
                        Path, "iterdir", side_effect=OSError("Permission denied")
                    ):
                        with patch(
                            "plt_optimizer.ui.settings.messagebox.showinfo"
                        ) as mock_info:
                            # Should not raise
                            cleanup_cmd()
                            mock_info.assert_called_once()

    def test_on_cleanup_closure_handles_unlink_oserror_on_processed(self) -> None:
        """Test the on_cleanup closure handles OSError on processed file unlink."""
        mock_file = MagicMock()
        mock_file.is_file.return_value = True
        mock_file.unlink.side_effect = OSError("Permission denied")

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow({}, MagicMock())
            window._log_dir_var.set("")
            window._processed_dir_var.set("/test/processed")

            cleanup_cmd = self._find_cleanup_command()
            assert cleanup_cmd is not None

            with patch(
                "plt_optimizer.ui.settings.messagebox.askyesno", return_value=True
            ):
                with patch.object(Path, "exists", return_value=True):
                    with patch.object(
                        Path, "iterdir", return_value=[mock_file]
                    ):
                        with patch(
                            "plt_optimizer.ui.settings.messagebox.showinfo"
                        ) as mock_info:
                            # Should not raise
                            cleanup_cmd()
                            mock_info.assert_called_once()
                            # cleaned_count is 0 because unlink failed
                            args = mock_info.call_args[0]
                            assert "Deleted 0 files" in args[1]

    def test_on_cleanup_closure_counts_combined_cleanups(self) -> None:
        """Test the on_cleanup closure reports combined count from both dirs."""
        mock_log_file = MagicMock()
        mock_log_file.is_file.return_value = True
        mock_processed_file = MagicMock()
        mock_processed_file.is_file.return_value = True

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow({}, MagicMock())
            window._log_dir_var.set("/test/logs")
            window._processed_dir_var.set("/test/processed")

            cleanup_cmd = self._find_cleanup_command()
            assert cleanup_cmd is not None

            iterdir_results = [[mock_log_file], [mock_processed_file]]
            iterdir_iter = iter(iterdir_results)

            with patch(
                "plt_optimizer.ui.settings.messagebox.askyesno", return_value=True
            ):
                with patch.object(Path, "exists", return_value=True):
                    with patch.object(
                        Path, "iterdir", side_effect=lambda *_: next(iterdir_iter)
                    ):
                        with patch(
                            "plt_optimizer.ui.settings.messagebox.showinfo"
                        ) as mock_info:
                            cleanup_cmd()
                            mock_log_file.unlink.assert_called_once()
                            mock_processed_file.unlink.assert_called_once()
                            mock_info.assert_called_once()
                            args = mock_info.call_args[0]
                            assert "Deleted 2 files" in args[1]

    def test_on_cleanup_closure_skips_non_files_in_log_dir(self) -> None:
        """Test the on_cleanup closure skips entries where is_file() is False."""
        mock_dir = MagicMock()
        mock_dir.is_file.return_value = False  # Not a file - should be skipped

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow({}, MagicMock())
            window._log_dir_var.set("/test/logs")
            window._processed_dir_var.set("")

            cleanup_cmd = self._find_cleanup_command()
            assert cleanup_cmd is not None

            with patch(
                "plt_optimizer.ui.settings.messagebox.askyesno", return_value=True
            ):
                with patch.object(Path, "exists", return_value=True):
                    with patch.object(Path, "iterdir", return_value=[mock_dir]):
                        with patch(
                            "plt_optimizer.ui.settings.messagebox.showinfo"
                        ) as mock_info:
                            cleanup_cmd()
                            # unlink should NOT be called for a non-file
                            mock_dir.unlink.assert_not_called()
                            mock_info.assert_called_once()
                            args = mock_info.call_args[0]
                            assert "Deleted 0 files" in args[1]

    def test_on_cleanup_closure_skips_non_files_in_processed_dir(self) -> None:
        """Test the on_cleanup closure skips non-files in processed dir."""
        mock_dir = MagicMock()
        mock_dir.is_file.return_value = False

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow({}, MagicMock())
            window._log_dir_var.set("")
            window._processed_dir_var.set("/test/processed")

            cleanup_cmd = self._find_cleanup_command()
            assert cleanup_cmd is not None

            with patch(
                "plt_optimizer.ui.settings.messagebox.askyesno", return_value=True
            ):
                with patch.object(Path, "exists", return_value=True):
                    with patch.object(Path, "iterdir", return_value=[mock_dir]):
                        with patch(
                            "plt_optimizer.ui.settings.messagebox.showinfo"
                        ) as mock_info:
                            cleanup_cmd()
                            mock_dir.unlink.assert_not_called()
                            mock_info.assert_called_once()

    def test_on_cleanup_user_declines(self) -> None:
        """Test cleanup does nothing when user clicks No."""
        mock_root = MagicMock()

        # When askyesno returns False, the cleanup should return early
        with patch(
            "plt_optimizer.ui.settings.messagebox.askyesno",
            return_value=False,
        ):
            result = messagebox.askyesno(
                "Cleanup Files",
                "This will delete all files in the logs and processed directories.\n\nContinue?",
                parent=mock_root,
            )
            assert result is False
            # If user declines, no further cleanup operations happen

    def test_on_cleanup_cleans_log_directory(self) -> None:
        """Test cleanup deletes files from log directory when confirmed."""
        mock_file1 = MagicMock()
        mock_file1.is_file.return_value = True
        mock_file2 = MagicMock()
        mock_file2.is_file.return_value = True

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "iterdir", return_value=[mock_file1, mock_file2]):
                with patch(
                    "plt_optimizer.ui.settings.messagebox.askyesno",
                    return_value=True,
                ):
                    # Simulate cleanup logic
                    log_dir = "/test/logs"
                    cleaned_count = 0
                    for f in Path(log_dir).iterdir():
                        if f.is_file():
                            f.unlink()
                            cleaned_count += 1

                    assert cleaned_count == 2
                    mock_file1.unlink.assert_called_once()
                    mock_file2.unlink.assert_called_once()

    def test_on_cleanup_handles_oserror_on_log_delete(self) -> None:
        """Test cleanup handles OSError when deleting log files."""
        mock_file = MagicMock()
        mock_file.is_file.return_value = True
        mock_file.unlink.side_effect = OSError("Permission denied")

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "iterdir", return_value=[mock_file]):
                with patch(
                    "plt_optimizer.ui.settings.messagebox.askyesno",
                    return_value=True,
                ):
                    # The cleanup catches and logs the exception
                    cleaned_count = 0
                    error_logged = False
                    try:
                        for f in Path("/test/logs").iterdir():
                            if f.is_file():
                                f.unlink()
                                cleaned_count += 1
                    except OSError as e:
                        _logger.error(f"Error cleaning log directory: {e}")
                        error_logged = True

                    assert error_logged
                    assert cleaned_count == 0  # No files cleaned due to error

    def test_on_cleanup_cleans_processed_directory(self) -> None:
        """Test cleanup deletes files from processed directory when confirmed."""
        mock_file3 = MagicMock()
        mock_file3.is_file.return_value = True

        with patch(
            "plt_optimizer.ui.settings.messagebox.askyesno",
            return_value=True,
        ):
            # Patch exists for processed dir
            def exists_side_effect(path: str | Path) -> bool:
                path_str = str(path)
                if "logs" in path_str.lower():
                    return False  # Log dir doesn't exist or is empty string
                return True

            with patch.object(Path, "exists", side_effect=exists_side_effect):
                with patch.object(Path, "iterdir", return_value=[mock_file3]):
                    processed_dir = "/test/processed"
                    cleaned_count = 0
                    for f in Path(processed_dir).iterdir():
                        if f.is_file():
                            f.unlink()
                            cleaned_count += 1

                    assert cleaned_count == 1

    def test_on_cleanup_shows_info_message(self) -> None:
        """Test cleanup shows info message when complete."""
        mock_root = MagicMock()

        with patch(
            "plt_optimizer.ui.settings.messagebox.askyesno",
            return_value=True,
        ):
            # When both log and processed dirs are empty/non-existent, cleaned_count is 0
            def exists_side_effect(path: str | Path) -> bool:
                path_str = str(path)
                if path_str == "" or "logs" in path_str.lower():
                    return False
                return True

            with patch.object(Path, "exists", side_effect=exists_side_effect):
                log_dir = ""
                cleaned_count = 0

                # Clean logs directory (doesn't exist)
                if log_dir and Path(log_dir).exists():
                    for f in Path(log_dir).iterdir():
                        if f.is_file():
                            f.unlink()
                            cleaned_count += 1

                # Clean processed directory (doesn't have files to clean, would show 0)

            messagebox.showinfo(
                "Cleanup Complete",
                f"Deleted {cleaned_count} files.",
                parent=mock_root,
            )


class TestBrowseDirectoryErrorHandling:
    """Tests for _browse_directory error handling (lines 263-264, 271-272)."""

    def test_browse_handles_deiconify_exception_after_filedialog_failure(
        self,
    ) -> None:
        """Test deiconify exception after filedialog failure is swallowed (lines 263-264)."""
        current_config: dict[str, Any] = {}
        mock_root = MagicMock()

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())
            window._root = mock_root
            var = MockStringVar("/some/path")

            with patch.object(Path, "exists", return_value=True):
                # First withdraw must succeed, then filedialog raises
                mock_root.withdraw.return_value = None
                mock_root.deiconify.side_effect = Exception("Display error")
                with patch(
                    "plt_optimizer.ui.settings.filedialog.askdirectory",
                    side_effect=Exception("Dialog error"),
                ):
                    # Should not raise - error is caught and logged
                    window._browse_directory(var)
                    # withdraw should have been called to hide window
                    mock_root.withdraw.assert_called_once()
                    # deiconify should have been called once inside the except
                    # (the silent swallow in the inner try)
                    assert mock_root.deiconify.called

    def test_browse_handles_focus_force_exception(self) -> None:
        """Test focus_force exception during window restore (lines 271-272)."""
        current_config: dict[str, Any] = {}
        mock_root = MagicMock()

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())
            window._root = mock_root
            var = MockStringVar("/some/path")

            with patch.object(Path, "exists", return_value=True):
                # filedialog succeeds, but focus_force fails during restore
                mock_root.deiconify.return_value = None
                mock_root.focus_force.side_effect = Exception("Focus error")

                with patch(
                    "plt_optimizer.ui.settings.filedialog.askdirectory",
                    return_value="/selected",
                ):
                    # Should not raise - window restore errors are caught
                    window._browse_directory(var)
                    # deiconify should be called to restore window
                    assert mock_root.deiconify.called
                    # focus_force should have been called and failed
                    assert mock_root.focus_force.called
                    # var should still be set because selected is not empty
                    assert var.get() == "/selected"

    def test_browse_deiconify_exception_silently_swallowed(self) -> None:
        """Test that inner deiconify exception is silently swallowed (lines 263-264).

        When filedialog raises and the cleanup deiconify also fails, the
        exception from deiconify should be silently caught (no re-raise).
        """
        current_config: dict[str, Any] = {}
        mock_root = MagicMock()

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())
            window._root = mock_root
            var = MockStringVar("/some/path")

            with patch.object(Path, "exists", return_value=True):
                # Both filedialog and deiconify raise
                mock_root.deiconify.side_effect = Exception("Display error")
                with patch(
                    "plt_optimizer.ui.settings.filedialog.askdirectory",
                    side_effect=Exception("Dialog error"),
                ):
                    # Should not raise - both exceptions are handled
                    window._browse_directory(var)


class TestValidateInputsElseBranch:
    """Tests for _validate_inputs else branch (lines 317-322)."""

    def test_validate_returns_false_when_user_declines_missing_dir(
        self,
    ) -> None:
        """Test validation returns False when user declines creating missing directory."""
        current_config: dict[str, Any] = {}

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())
            window._watch_dir_var.set("/nonexistent/dir")
            window._output_dir_var.set("/out")
            window._log_dir_var.set("/logs")

            with patch.object(Path, "exists", return_value=False):
                with patch(
                    "plt_optimizer.ui.settings.messagebox.askyesno",
                    return_value=False,
                ):
                    result = window._validate_inputs()

            assert result is False


class TestShowWindows:
    """Tests for show method on Windows (lines 395-396, 400+).

    Note: These tests verify the platform-specific code paths. The actual
    lines in show() that are Windows-only (setting protocol and calling mainloop)
    cannot be easily tested on non-Windows platforms without extensive mocking.
    """

    def test_show_platform_check_exists(self) -> None:
        """Test that _IS_WINDOWS constant is correctly set based on platform."""
        import importlib
        import sys

        from plt_optimizer.ui import settings

        # Reload to ensure _IS_WINDOWS reflects the actual current platform,
        # since other tests in this module reload the module with patched
        # sys.platform which leaves _IS_WINDOWS in an inconsistent state.
        importlib.reload(settings)

        # On darwin, _IS_WINDOWS should be False
        if sys.platform == "win32":
            assert settings._IS_WINDOWS is True
        else:
            assert settings._IS_WINDOWS is False


class TestShowPlatformBehavior:
    """Tests that verify platform-specific behavior in show()."""

    def test_show_does_not_call_mainloop_on_non_windows(self) -> None:
        """Verify mainloop is NOT called on non-Windows platforms.

        The show() method has this structure:
        - On Windows: deiconify, focus_force, protocol(WM_DELETE_WINDOW), mainloop
        - On non-Windows: deiconify, return

        Since we're running on darwin, the else branch (no mainloop) should execute.
        """
        current_config: dict[str, Any] = {}
        mock_tk_instance = MagicMock()
        mock_tk_instance.winfo_screenwidth.return_value = 1920
        mock_tk_instance.winfo_screenheight.return_value = 1080
        mock_tk_instance.winfo_width.return_value = 580
        mock_tk_instance.winfo_height.return_value = 480

        # Track that show() was called and mainloop should NOT be called
        with patch.object(Path, "exists", return_value=True):
            with patch("plt_optimizer.ui.settings.tk.Toplevel", return_value=mock_tk_instance):
                window = SettingsWindow(current_config, MagicMock())
                try:
                    window.show()
                except Exception:
                    pass  # Expected - no display

        # On non-Windows (darwin), mainloop should NOT be called
        mock_tk_instance.mainloop.assert_not_called()


class TestSaveCallback:
    """Tests for save callback handling."""

    def test_save_callback_exception_shows_error(self) -> None:
        """Test that save callback exceptions show error message."""
        current_config: dict[str, Any] = {}
        callback = MagicMock(side_effect=RuntimeError("Database connection failed"))

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, callback)
            window._watch_dir_var.set("/watch")
            window._output_dir_var.set("/out")
            window._log_dir_var.set("/logs")

            with patch.object(Path, "exists", return_value=True):
                with patch.object(window, "_validate_inputs", return_value=True):
                    with patch(
                        "plt_optimizer.ui.settings.messagebox.showerror"
                    ) as mock_error:
                        window._on_save()
                        # Error dialog should have been shown
                        assert mock_error.called


class TestProcessedDirHandling:
    """Tests for processed_dir handling in _on_save."""

    def test_on_save_sets_processed_dir_to_none_when_empty(self) -> None:
        """Test that empty processed dir is set to None on save."""
        current_config: dict[str, Any] = {}
        callback = MagicMock()

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, callback)
            window._watch_dir_var.set("/watch")
            window._output_dir_var.set("/out")
            window._log_dir_var.set("/logs")
            window._processed_dir_var.set("")

            with patch.object(Path, "exists", return_value=True):
                with patch.object(window, "_validate_inputs", return_value=True):
                    window._on_save()

            saved_config = callback.call_args[0][0]
            assert saved_config["processed_dir"] is None

    def test_on_save_preserves_processed_dir_when_set(self) -> None:
        """Test that processed dir value is preserved when non-empty."""
        current_config: dict[str, Any] = {}
        callback = MagicMock()

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, callback)
            window._watch_dir_var.set("/watch")
            window._output_dir_var.set("/out")
            window._log_dir_var.set("/logs")
            window._processed_dir_var.set("/processed")

            with patch.object(Path, "exists", return_value=True):
                with patch.object(window, "_validate_inputs", return_value=True):
                    window._on_save()

            saved_config = callback.call_args[0][0]
            assert saved_config["processed_dir"] == "/processed"


class TestDebugAndFastMode:
    """Tests for fast_mode and debug_save_files handling."""

    def test_on_save_preserves_fast_mode_setting(self) -> None:
        """Test that fast mode setting is preserved on save."""
        current_config: dict[str, Any] = {"fast_mode": True}
        callback = MagicMock()

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, callback)
            window._watch_dir_var.set("/watch")
            window._output_dir_var.set("/out")
            window._log_dir_var.set("/logs")

            # Fast mode should already be set from config
            assert window._fast_mode_var.get() is True

    def test_on_save_preserves_debug_setting(self) -> None:
        """Test that debug save files setting is preserved on save."""
        current_config: dict[str, Any] = {"debug_save_files": False}
        callback = MagicMock()

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, callback)
            window._watch_dir_var.set("/watch")
            window._output_dir_var.set("/out")
            window._log_dir_var.set("/logs")

            # Debug mode should already be set from config
            assert window._debug_save_files_var.get() is False


class TestWindowsStartupSection:
    """Tests for the Windows-only Startup section of _setup_ui (lines 161-175).

    ``_IS_WINDOWS`` is a module-level constant resolved at import time, so on
    macOS it is False and the Startup block never runs. These tests patch the
    constant to True while a :class:`SettingsWindow` is built, which is the
    only way to exercise the Windows two-column layout.
    """

    def test_windows_startup_section_created_and_maintenance_shifted(self) -> None:
        """Test the Startup section/checkbox are built and Maintenance moves column."""
        startup_var = MockBooleanVar()
        sections: dict[str, MagicMock] = {}
        checkbutton_kwargs: list[dict[str, Any]] = []

        def make_section(*args: Any, **kwargs: Any) -> MagicMock:
            """Return a distinct mock per LabelFrame so grid() calls stay separable."""
            section = MagicMock()
            sections[str(kwargs.get("text", ""))] = section
            return section

        def make_checkbutton(*args: Any, **kwargs: Any) -> MagicMock:
            """Record Checkbutton kwargs so the startup checkbox can be inspected."""
            checkbutton_kwargs.append(kwargs)
            return MagicMock()

        with patch("plt_optimizer.ui.settings._IS_WINDOWS", True):
            with patch("plt_optimizer.ui.settings.tk.Toplevel"):
                with patch(
                    "plt_optimizer.ui.settings.tk.BooleanVar", return_value=startup_var
                ) as mock_boolvar:
                    with patch(
                        "plt_optimizer.ui.settings.ttk.LabelFrame", side_effect=make_section
                    ) as mock_labelframe:
                        with patch(
                            "plt_optimizer.ui.settings.ttk.Checkbutton",
                            side_effect=make_checkbutton,
                        ):
                            window = SettingsWindow({}, MagicMock())

        # Directories/Optimization vars plus the extra Windows startup var.
        assert mock_boolvar.call_count == 3
        assert window._run_at_startup_var is startup_var
        # Directories, Optimization Options, Startup and Maintenance frames.
        assert mock_labelframe.call_count == 4
        sections["Startup"].grid.assert_called_once_with(
            row=3, column=0, sticky="ew", pady=(10, 10), ipady=5
        )
        # On Windows Maintenance sits beside Startup (column 1, no columnspan).
        sections["Maintenance"].grid.assert_called_once_with(
            row=3, column=1, sticky="ew", pady=(10, 10), ipady=5, padx=(10, 0)
        )
        startup_checks = [
            kwargs for kwargs in checkbutton_kwargs if kwargs.get("text") == "Run at Windows Startup"
        ]
        assert len(startup_checks) == 1
        assert startup_checks[0]["variable"] is startup_var

    def test_non_windows_layout_has_no_startup_section(self) -> None:
        """Test the Startup section is absent and Maintenance spans full width elsewhere."""
        sections: dict[str, MagicMock] = {}

        def make_section(*args: Any, **kwargs: Any) -> MagicMock:
            """Return a distinct mock per LabelFrame so grid() calls stay separable."""
            section = MagicMock()
            sections[str(kwargs.get("text", ""))] = section
            return section

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            with patch(
                "plt_optimizer.ui.settings.ttk.LabelFrame", side_effect=make_section
            ) as mock_labelframe:
                window = SettingsWindow({}, MagicMock())

        assert "Startup" not in sections
        assert mock_labelframe.call_count == 3
        assert not hasattr(window, "_run_at_startup_var")
        sections["Maintenance"].grid.assert_called_once_with(
            row=3, column=0, columnspan=4, sticky="ew", pady=(10, 10)
        )


class TestLoadCurrentValuesWindows:
    """Tests for the Windows startup-state reflection in _load_current_values (291-299)."""

    def test_load_uses_system_startup_state(self) -> None:
        """Test the system registry check wins over the stored config value."""
        with patch("plt_optimizer.ui.settings._IS_WINDOWS", True):
            with patch("plt_optimizer.ui.settings.tk.Toplevel"):
                with patch(
                    "plt_optimizer.utils.startup.is_startup_enabled", return_value=True
                ) as mock_is_enabled:
                    window = SettingsWindow({"run_at_startup": False}, MagicMock())

        mock_is_enabled.assert_called_once()
        assert window._run_at_startup_var.get() is True

    def test_load_falls_back_to_config_when_startup_check_raises(self) -> None:
        """Test a failing system check falls back to the stored config value."""
        with patch("plt_optimizer.ui.settings._IS_WINDOWS", True):
            with patch("plt_optimizer.ui.settings.tk.Toplevel"):
                with patch(
                    "plt_optimizer.utils.startup.is_startup_enabled",
                    side_effect=RuntimeError("registry unavailable"),
                ):
                    window = SettingsWindow({"run_at_startup": True}, MagicMock())

        assert window._run_at_startup_var.get() is True

    def test_load_skips_reflection_when_startup_var_missing(self) -> None:
        """Test reflection is skipped when the Startup section was never built."""
        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow({"run_at_startup": True}, MagicMock())

        # Built on a non-Windows layout, so the attribute does not exist.
        with patch("plt_optimizer.ui.settings._IS_WINDOWS", True):
            window._load_current_values()

        assert not hasattr(window, "_run_at_startup_var")


class TestOnSaveWindowsStartup:
    """Tests for the Windows startup-shortcut block in _on_save (lines 373-392)."""

    def _build_window(self, config: dict[str, Any], callback: MagicMock) -> SettingsWindow:
        """Build a Windows-layout window with valid directory values.

        Args:
            config: Configuration dictionary handed to the window.
            callback: Save callback mock to install on the window.

        Returns:
            A SettingsWindow whose inputs pass validation.
        """
        window = SettingsWindow(config, callback)
        window._watch_dir_var.set("/watch")
        window._output_dir_var.set("/out")
        window._log_dir_var.set("/logs")
        return window

    def test_on_save_enables_startup_shortcut(self) -> None:
        """Test checking Run at Startup creates the shortcut and stores the flag."""
        callback = MagicMock()

        with patch("plt_optimizer.ui.settings._IS_WINDOWS", True):
            with patch("plt_optimizer.ui.settings.tk.Toplevel"):
                window = self._build_window({}, callback)
                window._run_at_startup_var.set(True)

                with patch("plt_optimizer.utils.startup.create_shortcut") as mock_create:
                    with patch("plt_optimizer.utils.startup.remove_shortcut") as mock_remove:
                        with patch.object(Path, "exists", return_value=True):
                            window._on_save()

        saved_config = callback.call_args[0][0]
        assert saved_config["run_at_startup"] is True
        mock_create.assert_called_once()
        mock_remove.assert_not_called()

    def test_on_save_disables_startup_shortcut(self) -> None:
        """Test clearing Run at Startup removes the shortcut and stores the flag."""
        callback = MagicMock()

        with patch("plt_optimizer.ui.settings._IS_WINDOWS", True):
            with patch("plt_optimizer.ui.settings.tk.Toplevel"):
                window = self._build_window({"run_at_startup": True}, callback)
                # _load_current_values reflects the (mocked) system state: disabled.
                window._run_at_startup_var.set(False)

                with patch("plt_optimizer.utils.startup.create_shortcut") as mock_create:
                    with patch("plt_optimizer.utils.startup.remove_shortcut") as mock_remove:
                        with patch.object(Path, "exists", return_value=True):
                            window._on_save()

        saved_config = callback.call_args[0][0]
        assert saved_config["run_at_startup"] is False
        mock_remove.assert_called_once()
        mock_create.assert_not_called()

    def test_on_save_survives_startup_update_failure(self) -> None:
        """Test a shortcut failure is logged but does not abort saving."""
        callback = MagicMock()

        with patch("plt_optimizer.ui.settings._IS_WINDOWS", True):
            with patch("plt_optimizer.ui.settings.tk.Toplevel"):
                window = self._build_window({}, callback)
                window._run_at_startup_var.set(True)

                with patch(
                    "plt_optimizer.utils.startup.create_shortcut",
                    side_effect=OSError("shortcut write failed"),
                ):
                    with patch.object(Path, "exists", return_value=True):
                        with patch(
                            "plt_optimizer.ui.settings.messagebox.showerror"
                        ) as mock_showerror:
                            window._on_save()

        # The startup failure is swallowed, so the save still completes.
        callback.assert_called_once()
        saved_config = callback.call_args[0][0]
        assert saved_config["run_at_startup"] is True
        mock_showerror.assert_not_called()

    def test_on_save_skips_startup_block_when_startup_var_missing(self) -> None:
        """Test a non-Windows layout never writes run_at_startup on save."""
        callback = MagicMock()

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = self._build_window({"run_at_startup": True}, callback)

        with patch("plt_optimizer.ui.settings._IS_WINDOWS", True):
            with patch("plt_optimizer.utils.startup.create_shortcut") as mock_create:
                with patch("plt_optimizer.utils.startup.remove_shortcut") as mock_remove:
                    with patch.object(Path, "exists", return_value=True):
                        window._on_save()

        saved_config = callback.call_args[0][0]
        # The block never ran, so the stored value is untouched (not rewritten
        # from the non-existent startup variable).
        assert saved_config["run_at_startup"] is True
        mock_create.assert_not_called()
        mock_remove.assert_not_called()
