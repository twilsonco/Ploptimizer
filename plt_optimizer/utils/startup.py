"""Windows Startup folder management for PLT-Optimizer.

This module handles adding and removing the application shortcut from the user's
Windows Startup folder to enable "Run at Startup" functionality.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Optional


def _safe_find_spec(name: str) -> bool:
    """Safely check whether a module spec can be located.

    Wraps :func:`importlib.util.find_spec` so the call never propagates an
    exception to the importer. ``find_spec`` is documented to return ``None``
    when a module cannot be found, but in practice it raises
    ``ModuleNotFoundError`` when an intermediate package on a dotted path is
    absent (e.g. ``find_spec("win32com.client")`` when ``win32com`` itself is
    not installed). Treating such errors as "module unavailable" lets the
    rest of this module load on systems where the optional ``tray`` extras
    are not installed (e.g. headless CI runners on Windows).

    Args:
        name: Fully-qualified module name to look up (may contain dots).

    Returns:
        True if the module is importable, False otherwise.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ModuleNotFoundError, ValueError):
        # ValueError covers malformed names; ModuleNotFoundError covers the
        # case where a parent package on a dotted path is missing entirely.
        return False


# Check availability of Windows-specific modules without importing at module level.
# We use the safe helper above because find_spec("win32com.client") raises
# ModuleNotFoundError (instead of returning None) when the win32com package
# itself is not installed, which is the case on CI runners that only install
# the ``dev`` and ``plotting`` extras.
_WINSHELL_AVAILABLE: bool = sys.platform == "win32" and _safe_find_spec("winshell")
_PYWIN32_AVAILABLE: bool = sys.platform == "win32" and _safe_find_spec("win32com.client")

# Shortcut filename without extension
APP_NAME = "PLT-Optimizer"


def get_startup_folder() -> Optional[Path]:
    """Get the Windows Startup folder path.

    Returns:
        Path to the user's Startup folder, or None if not on Windows.
    """
    if sys.platform != "win32":
        return None

    try:
        import winshell  # noqa: F401

        startup = winshell.startup()
        return Path(startup)
    except Exception:
        # Fallback for environments where winshell isn't available
        fallback = (
            Path.home()
            / "AppData"
            / "Roaming"
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
        )
        return fallback


def get_executable_path() -> Optional[Path]:
    """Get the path to the current executable.

    Returns:
        Path to the running executable or script, or None if unavailable.
    """
    if getattr(sys, "frozen", False):
        # Running as compiled executable
        return Path(sys.executable)
    else:
        # Running as Python script - use pythonw.exe for background execution
        if sys.platform == "win32":
            # Find pythonw in the same virtual environment
            venv_python = Path(sys.executable).parent / "pythonw.exe"
            if venv_python.exists():
                return venv_python

            # Fallback to any pythonw in PATH
            import shutil

            pythonw = shutil.which("pythonw.exe")
            if pythonw:
                return Path(pythonw)

        # Return the Python interpreter for non-Windows or as fallback
        return Path(sys.executable)


def create_shortcut(
    target_path: Optional[Path] = None,
    shortcut_name: str = APP_NAME,
) -> Optional[Path]:
    """Create a Windows shortcut in the Startup folder.

    Args:
        target_path: Path to the executable. Defaults to current executable.
        shortcut_name: Name for the shortcut file (without .lnk extension).

    Returns:
        Path to the created shortcut, or None on failure/not on Windows.
    """
    if sys.platform != "win32":
        return None

    startup_folder = get_startup_folder()
    if startup_folder is None:
        return None

    if target_path is None:
        target_path = get_executable_path()
        if target_path is None:
            return None

    shortcut_path = startup_folder / f"{shortcut_name}.lnk"

    try:
        from win32com.client import Dispatch

        shell = Dispatch("WScript.Shell")
        shortcut = shell.CreateShortcut(str(shortcut_path))
        shortcut.TargetPath = str(target_path)
        shortcut.WorkingDirectory = str(
            target_path.parent if target_path.parent.exists() else Path.cwd()
        )
        shortcut.WindowStyle = 7  # Minimized (start in background)
        shortcut.Description = "PLT-Optimizer - HPGL/PLT File Optimization"
        shortcut.Save()
        return shortcut_path

    except ImportError:
        # winshell or pywin32 not available
        import logging

        logging.warning("winshell or pywin32 not available for shortcut creation")
        return None
    except Exception as e:
        import logging

        logging.error(f"Failed to create startup shortcut: {e}")
        return None


def remove_shortcut(shortcut_name: str = APP_NAME) -> bool:
    """Remove a Windows shortcut from the Startup folder.

    Args:
        shortcut_name: Name of the shortcut file (without .lnk extension).

    Returns:
        True if removed successfully or didn't exist, False on error.
    """
    if sys.platform != "win32":
        return True

    startup_folder = get_startup_folder()
    if startup_folder is None:
        return True

    shortcut_path = startup_folder / f"{shortcut_name}.lnk"

    try:
        if shortcut_path.exists():
            shortcut_path.unlink()
        return True
    except OSError as e:
        import logging

        logging.error(f"Failed to remove startup shortcut: {e}")
        return False


def is_startup_enabled(shortcut_name: str = APP_NAME) -> bool:
    """Check if the application shortcut exists in Startup folder.

    Args:
        shortcut_name: Name of the shortcut file (without .lnk extension).

    Returns:
        True if the shortcut exists, False otherwise.
    """
    if sys.platform != "win32":
        return False

    startup_folder = get_startup_folder()
    if startup_folder is None:
        return False

    shortcut_path = startup_folder / f"{shortcut_name}.lnk"
    return shortcut_path.exists()


__all__ = [
    "APP_NAME",
    "get_startup_folder",
    "get_executable_path",
    "create_shortcut",
    "remove_shortcut",
    "is_startup_enabled",
]
