"""Tests for plt_optimizer/ui/settings.py."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch


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


# Import the module under test - it will use the mocked tkinter from conftest
from plt_optimizer.ui.settings import SettingsWindow


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
            window._log_dir_var.set("/logs")

            result = window._validate_inputs()

            assert result is False

    def test_validate_empty_log_dir(self) -> None:
        """Test validation fails when log directory is empty."""
        current_config: dict[str, Any] = {"watch_dir": "/watch", "output_dir": "/out"}

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())
            window._watch_dir_var.set("/watch")
            window._output_dir_var.set("/out")

            result = window._validate_inputs()

            assert result is False

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
            home = str(Path.home())

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
        """Test that destroy handles None root gracefully."""
        current_config: dict[str, Any] = {}

        with patch("plt_optimizer.ui.settings.tk.Toplevel"):
            window = SettingsWindow(current_config, MagicMock())
            # Should not raise
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

        with patch("plt_optimizer.ui.settings.tk.Toplevel", return_value=mock_tk_instance):
            window = SettingsWindow(current_config, MagicMock())

            with patch.object(Path, "exists", return_value=True):
                with patch("sys.platform", "darwin"):
                    window.show()

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

        with patch("plt_optimizer.ui.settings.tk.Toplevel", return_value=mock_tk_instance):
            window = SettingsWindow(current_config, MagicMock())

            with patch("sys.platform", "win32"):
                window.show()


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
