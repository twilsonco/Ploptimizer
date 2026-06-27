"""Tests for plt_optimizer.utils.startup module.

These tests target specific lines not covered by existing tests:
- get_startup_folder() non-Windows returns None (line 37)
- get_startup_folder() Windows with winshell (lines 43-44)
- get_startup_folder() Windows fallback path (lines 46-50)
- get_executable_path() frozen executable path (lines 67-68)
- get_executable_path() Python script on Windows without pythonw (lines 71-74)
- get_executable_path() non-Windows/fallback (line 77-78)
- create_shortcut() non-Windows returns None (lines 99-100)
- create_shortcut() startup_folder is None (lines 102-104)
- create_shortcut() target_path is None and get_executable_path() returns None (lines 106-111)
- create_shortcut() success with win32com.client (lines 117-131)
- create_shortcut() ImportError for winshell/pywin32 not available
- remove_shortcut() non-Windows returns True (lines 149-150)
- remove_shortcut() get_startup_folder is None (lines 152-154)
- remove_shortcut() file removed successfully (lines 158-159)
- remove_shortcut() OSError exception handling (lines 161-162)
- is_startup_enabled() non-Windows returns False (line 178-179)
- is_startup_enabled() startup_folder is None (lines 181-182)
- is_startup_enabled() shortcut exists / doesn't exist (lines 186-187)
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestGetStartupFolder:
    """Tests for get_startup_folder() function."""

    def test_non_windows_returns_none(self) -> None:
        """Test that non-Windows platforms return None (line 37)."""
        from plt_optimizer.utils.startup import get_startup_folder

        with patch.object(sys, "platform", "linux"):
            result = get_startup_folder()
            assert result is None

    def test_non_windows_darwin_returns_none(self) -> None:
        """Test that macOS returns None."""
        from plt_optimizer.utils.startup import get_startup_folder

        with patch.object(sys, "platform", "darwin"):
            result = get_startup_folder()
            assert result is None


class TestGetExecutablePath:
    """Tests for get_executable_path() function."""

    def test_frozen_executable_returns_sys_executable(self) -> None:
        """Test that frozen executable returns sys.executable (lines 67-68)."""
        from plt_optimizer.utils.startup import get_executable_path

        mock_executable = Path("/path/to/plt-optimizer.exe")

        with patch.dict(sys.__dict__, {"frozen": True, "executable": str(mock_executable)}):
            result = get_executable_path()
            assert result == mock_executable

    def test_non_frozen_non_windows_returns_sys_executable(self) -> None:
        """Test non-frozen on non-Windows returns sys.executable (line 77-78)."""
        from plt_optimizer.utils.startup import get_executable_path

        mock_executable = Path("/usr/bin/python")

        with patch.dict(sys.__dict__, {"frozen": False, "platform": "linux", "executable": str(mock_executable)}):
            result = get_executable_path()
            assert result == mock_executable

    def test_non_frozen_windows_no_pythonw_returns_sys_executable(self) -> None:
        """Test Windows without pythonw falls back to sys.executable (lines 71-74)."""
        from plt_optimizer.utils.startup import get_executable_path

        mock_executable = Path("/venv/Scripts/python.exe")

        with patch.dict(sys.__dict__, {"frozen": False, "platform": "win32", "executable": str(mock_executable)}):
            # Mock shutil.which to return None (pythonw not in PATH)
            with patch("shutil.which", return_value=None):
                result = get_executable_path()
                assert result == mock_executable


class TestCreateShortcut:
    """Tests for create_shortcut() function."""

    def test_non_windows_returns_none(self) -> None:
        """Test that non-Windows returns None (lines 99-100)."""
        from plt_optimizer.utils.startup import create_shortcut

        with patch.object(sys, "platform", "linux"):
            result = create_shortcut()
            assert result is None

    def test_startup_folder_none_returns_none(self) -> None:
        """Test that when startup_folder is None, returns None (lines 102-104)."""
        from plt_optimizer.utils.startup import create_shortcut

        with patch.object(sys, "platform", "win32"):
            with patch(
                "plt_optimizer.utils.startup.get_startup_folder",
                return_value=None,
            ):
                result = create_shortcut()
                assert result is None


class TestRemoveShortcut:
    """Tests for remove_shortcut() function."""

    def test_non_windows_returns_true(self) -> None:
        """Test that non-Windows returns True (lines 149-150)."""
        from plt_optimizer.utils.startup import remove_shortcut

        with patch.object(sys, "platform", "linux"):
            result = remove_shortcut()
            assert result is True

    def test_startup_folder_none_returns_true(self) -> None:
        """Test that when startup_folder is None, returns True (lines 152-154)."""
        from plt_optimizer.utils.startup import remove_shortcut

        with patch.object(sys, "platform", "win32"):
            with patch(
                "plt_optimizer.utils.startup.get_startup_folder",
                return_value=None,
            ):
                result = remove_shortcut()
                assert result is True


class TestIsStartupEnabled:
    """Tests for is_startup_enabled() function."""

    def test_non_windows_returns_false(self) -> None:
        """Test that non-Windows returns False (line 178-179)."""
        from plt_optimizer.utils.startup import is_startup_enabled

        with patch.object(sys, "platform", "linux"):
            result = is_startup_enabled()
            assert result is False

    def test_non_windows_darwin_returns_false(self) -> None:
        """Test that macOS returns False."""
        from plt_optimizer.utils.startup import is_startup_enabled

        with patch.object(sys, "platform", "darwin"):
            result = is_startup_enabled()
            assert result is False

    def test_startup_folder_none_returns_false(self) -> None:
        """Test that when startup_folder is None, returns False (lines 181-182)."""
        from plt_optimizer.utils.startup import is_startup_enabled

        with patch.object(sys, "platform", "win32"):
            with patch(
                "plt_optimizer.utils.startup.get_startup_folder",
                return_value=None,
            ):
                result = is_startup_enabled()
                assert result is False


class TestAppNameConstant:
    """Tests for APP_NAME constant."""

    def test_app_name_is_correct(self) -> None:
        """Test that APP_NAME has the expected value."""
        from plt_optimizer.utils.startup import APP_NAME

        assert APP_NAME == "PLT-Optimizer"


class TestExports:
    """Tests for module exports."""

    def test_all_exports_present(self) -> None:
        """Test that __all__ contains all expected items."""
        from plt_optimizer.utils import startup

        expected = [
            "APP_NAME",
            "get_startup_folder",
            "get_executable_path",
            "create_shortcut",
            "remove_shortcut",
            "is_startup_enabled",
        ]
        assert hasattr(startup, "__all__")
        for item in expected:
            assert item in startup.__all__


class TestGetStartupFolderWindowsPaths:
    """Tests for get_startup_folder() Windows fallback path (lines 46-50)."""

    def test_windows_fallback_path(self) -> None:
        """Test that Windows fallback returns Path.home()/AppData/.../Startup."""
        from plt_optimizer.utils.startup import get_startup_folder

        with patch.object(sys, "platform", "win32"):
            # Patch to raise exception when importing winshell
            with patch.dict("sys.modules", {"winshell": None}):
                result = get_startup_folder()
                
                assert result is not None
                assert "Roaming" in str(result)
                assert "Microsoft" in str(result)
                assert "Start Menu" in str(result)
                assert "Programs" in str(result)
                assert "Startup" in str(result)


class TestRemoveShortcutPaths:
    """Tests for remove_shortcut() function paths."""

    def test_remove_shortcut_file_not_exists_returns_true(self, tmp_path: Path) -> None:
        """Test that removing non-existent shortcut returns True."""
        from plt_optimizer.utils.startup import remove_shortcut

        with patch.object(sys, "platform", "win32"):
            with patch(
                "plt_optimizer.utils.startup.get_startup_folder",
                return_value=tmp_path,
            ):
                # Shortcut doesn't exist - should still return True
                result = remove_shortcut()
                assert result is True

    def test_remove_shortcut_success(self, tmp_path: Path) -> None:
        """Test successful shortcut removal."""
        from plt_optimizer.utils.startup import remove_shortcut

        with patch.object(sys, "platform", "win32"):
            with patch(
                "plt_optimizer.utils.startup.get_startup_folder",
                return_value=tmp_path,
            ):
                # Create a fake shortcut
                shortcut = tmp_path / "PLT-Optimizer.lnk"
                shortcut.touch()
                
                result = remove_shortcut()
                assert result is True
                assert not shortcut.exists()

    def test_remove_shortcut_oserror_returns_false(self) -> None:
        """Test OSError during removal returns False."""
        from plt_optimizer.utils import startup
        
        with patch.object(sys, "platform", "win32"):
            with patch("plt_optimizer.utils.startup.get_startup_folder") as mock_get_folder:
                tmp_shortcut = MagicMock(spec=Path)
                tmp_shortcut.exists.return_value = True
                
                # Make unlink raise OSError when called on the shortcut path
                original_unlink = Path.unlink
                def raising_unlink(self: object) -> None:
                    raise OSError("Permission denied")
                
                # Bind to a mock so it becomes a method-like callable
                bound_unlink = types.MethodType(raising_unlink, tmp_shortcut)
                tmp_shortcut.unlink = bound_unlink  # type: ignore[method-assign]
                tmp_shortcut.__str__ = lambda: "/tmp/PLT-Optimizer.lnk"
                
                mock_get_folder.return_value = MagicMock(spec=Path)
                mock_get_folder.return_value.__truediv__.return_value = tmp_shortcut
                
                result = startup.remove_shortcut()
                assert result is False


class TestIsStartupEnabledPaths:
    """Tests for is_startup_enabled() function paths."""

    def test_startup_folder_exists_shortcut_does_not_exist(self, tmp_path: Path) -> None:
        """Test when startup folder exists but shortcut doesn't."""
        from plt_optimizer.utils.startup import is_startup_enabled

        with patch.object(sys, "platform", "win32"):
            with patch(
                "plt_optimizer.utils.startup.get_startup_folder",
                return_value=tmp_path,
            ):
                result = is_startup_enabled()
                assert result is False

    def test_startup_folder_and_shortcut_exist(self, tmp_path: Path) -> None:
        """Test when both startup folder and shortcut exist."""
        from plt_optimizer.utils.startup import is_startup_enabled

        with patch.object(sys, "platform", "win32"):
            with patch(
                "plt_optimizer.utils.startup.get_startup_folder",
                return_value=tmp_path,
            ):
                # Create the fake shortcut
                shortcut = tmp_path / "PLT-Optimizer.lnk"
                shortcut.touch()
                
                result = is_startup_enabled()
                assert result is True


class TestCreateShortcutPaths:
    """Tests for create_shortcut() function paths."""

    def test_create_shortcut_get_executable_returns_none(self) -> None:
        """Test when get_executable_path returns None."""
        from plt_optimizer.utils.startup import create_shortcut

        with patch.object(sys, "platform", "win32"):
            with patch(
                "plt_optimizer.utils.startup.get_startup_folder",
                return_value=Path("/some/startup/folder"),
            ):
                with patch(
                    "plt_optimizer.utils.startup.get_executable_path",
                    return_value=None,
                ):
                    result = create_shortcut()
                    assert result is None

    def test_create_shortcut_import_error(self, tmp_path: Path) -> None:
        """Test ImportError for winshell/pywin32 not available."""
        from plt_optimizer.utils.startup import create_shortcut

        with patch.object(sys, "platform", "win32"):
            with patch(
                "plt_optimizer.utils.startup.get_startup_folder",
                return_value=tmp_path,
            ):
                with patch(
                    "plt_optimizer.utils.startup.get_executable_path",
                    return_value=Path("/some/exe"),
                ):
                    # Mock ImportError when importing win32com.client
                    import builtins
                    original_import = builtins.__import__
                    
                    def mock_import(name: str, *args: object, **kwargs: object) -> object:
                        if name == "win32com.client":
                            raise ImportError("No module named 'win32com'")
                        return original_import(name, *args, **kwargs)
                    
                    with patch.object(builtins, "__import__", side_effect=mock_import):
                        result = create_shortcut()
                        assert result is None
