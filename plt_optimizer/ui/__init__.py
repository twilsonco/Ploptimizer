"""UI package for PLT-Optimizer system tray and settings interface."""

from plt_optimizer.ui.settings import SettingsWindow
from plt_optimizer.ui.tray import TrayIconManager

__all__ = [
    "TrayIconManager",
    "SettingsWindow",
]
