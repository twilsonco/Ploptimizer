"""Tests for plt_optimizer.ui package exports.

These tests verify that the ui package correctly exposes its public API.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch


class TestUIExports:
    """Tests for plt_optimizer.ui module exports."""

    def test_imports_tray_icon_manager(self) -> None:
        """Test that TrayIconManager can be imported from plt_optimizer.ui."""
        with patch.dict(sys.modules, {"tkinter": MagicMock(), "tkinter.ttk": MagicMock()}):
            # We need to reload the module since it was already loaded
            if "plt_optimizer.ui" in sys.modules:
                del sys.modules["plt_optimizer.ui"]
            if "plt_optimizer.ui.settings" in sys.modules:
                del sys.modules["plt_optimizer.ui.settings"]
            if "plt_optimizer.ui.tray" in sys.modules:
                del sys.modules["plt_optimizer.ui.tray"]

            from plt_optimizer.ui import TrayIconManager

            assert TrayIconManager is not None
            assert issubclass(TrayIconManager, object)

    def test_imports_settings_window(self) -> None:
        """Test that SettingsWindow can be imported from plt_optimizer.ui."""
        with patch.dict(sys.modules, {"tkinter": MagicMock(), "tkinter.ttk": MagicMock()}):
            if "plt_optimizer.ui" in sys.modules:
                del sys.modules["plt_optimizer.ui"]
            if "plt_optimizer.ui.settings" in sys.modules:
                del sys.modules["plt_optimizer.ui.settings"]
            if "plt_optimizer.ui.tray" in sys.modules:
                del sys.modules["plt_optimizer.ui.tray"]

            from plt_optimizer.ui import SettingsWindow

            assert SettingsWindow is not None
            assert issubclass(SettingsWindow, object)


class TestUIAllList:
    """Tests for __all__ definition in ui package."""

    def test_all_exports_are_classes(self) -> None:
        """Test that all items in __all__ are importable classes."""
        with patch.dict(sys.modules, {"tkinter": MagicMock(), "tkinter.ttk": MagicMock()}):
            if "plt_optimizer.ui" in sys.modules:
                del sys.modules["plt_optimizer.ui"]
            if "plt_optimizer.ui.settings" in sys.modules:
                del sys.modules["plt_optimizer.ui.settings"]
            if "plt_optimizer.ui.tray" in sys.modules:
                del sys.modules["plt_optimizer.ui.tray"]

            from plt_optimizer.ui import SettingsWindow, TrayIconManager

            assert issubclass(TrayIconManager, object)
            assert issubclass(SettingsWindow, object)

    def test_all_list_contains_expected_symbols(self) -> None:
        """Test that __all__ contains exactly the expected public API."""
        with patch.dict(sys.modules, {"tkinter": MagicMock(), "tkinter.ttk": MagicMock()}):
            if "plt_optimizer.ui" in sys.modules:
                del sys.modules["plt_optimizer.ui"]
            if "plt_optimizer.ui.settings" in sys.modules:
                del sys.modules["plt_optimizer.ui.settings"]
            if "plt_optimizer.ui.tray" in sys.modules:
                del sys.modules["plt_optimizer.ui.tray"]

            import plt_optimizer.ui as ui_module

            expected = {"TrayIconManager", "SettingsWindow"}
            assert set(ui_module.__all__) == expected

