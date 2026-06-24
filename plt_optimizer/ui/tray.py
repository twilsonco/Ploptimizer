"""System tray management for PLT-Optimizer.

This module provides the system tray icon, context menu, and notification handling.
It runs the file watcher in a background thread while maintaining the GUI event loop.
"""

from __future__ import annotations

# Third-party imports
import importlib.util
import logging
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Check availability of required libraries without importing them at module level
if importlib.util.find_spec("PIL") is None or importlib.util.find_spec("pystray") is None:
    raise ImportError("Required libraries missing. Install with: uv add pillow pystray")

from PIL import Image
from pystray import Icon, Menu, MenuItem  # type: ignore[import-untyped]

# Module-level logger
_logger = logging.getLogger(__name__)


class TrayIconManager:
    """Manages the system tray icon, menu, and notification dispatch.

    This class encapsulates all tray-related functionality including:
    - Creating and displaying the system tray icon
    - Handling menu actions (Open Settings, Exit)
    - Running the file watcher in a background thread
    - Dispatching notifications on file processing events

    Attributes:
        on_settings_requested: Callback when user requests settings window.
        on_exit_requested: Callback when user requests application exit.
    """

    def __init__(
        self,
        watcher_fn: Callable[[dict[str, Any]], None],
        config_loader: Callable[[], dict[str, Any]],
        get_icon_path: Callable[[], Path],
    ) -> None:
        """Initialize the tray icon manager.

        Args:
            watcher_fn: Function to call with config dict to start the watcher.
                This function should block until stop_event is set.
            config_loader: Function that returns current configuration dict.
            get_icon_path: Function that returns path to icon.ico file,
                accounting for PyInstaller's _MEIPASS in frozen builds.
        """
        self._watcher_fn = watcher_fn
        self._config_loader = config_loader
        self._get_icon_path = get_icon_path

        self._icon: Icon | None = None
        self._watcher_thread: threading.Thread | None = None
        self._stop_event: threading.Event = threading.Event()

        # Callbacks for UI events
        self.on_settings_requested: Callable[[], None] | None = None
        self.on_exit_requested: Callable[[], None] | None = None

    def _create_menu(self) -> Menu:
        """Create the tray icon context menu.

        Returns:
            pystray.Menu object with standard items.
        """
        return Menu(
            MenuItem("Open Settings", self._on_settings_click),
            Menu.SEPARATOR,
            MenuItem("Exit", self._on_exit_click),
        )

    def _load_icon_image(self) -> Image.Image:
        """Load the icon image from file.

        Returns:
            PIL Image object for the tray icon.
        """
        icon_path = self._get_icon_path()
        try:
            return Image.open(icon_path)
        except FileNotFoundError:
            _logger.warning(f"Icon not found at {icon_path}, creating default")
            # Create a simple 64x64 blue square as fallback
            img = Image.new("RGB", (64, 64), color=(0, 120, 200))
            return img

    def _on_settings_click(self, icon: Icon, item: MenuItem) -> None:
        """Handle 'Open Settings' menu click.

        Args:
            icon: The pystray Icon instance.
            item: The clicked menu item.
        """
        _logger.debug("Settings requested from tray menu")
        if self.on_settings_requested is not None:
            try:
                self.on_settings_requested()
            except Exception as e:
                _logger.error(f"Error opening settings: {e}")

    def _on_exit_click(self, icon: Icon, item: MenuItem) -> None:
        """Handle 'Exit' menu click.

        Args:
            icon: The pystray Icon instance.
            item: The clicked menu item.
        """
        _logger.debug("Exit requested from tray menu")
        if self.on_exit_requested is not None:
            try:
                self.on_exit_requested()
            except Exception as e:
                _logger.error(f"Error during exit: {e}")

    def _watcher_loop(
        self,
        stop_event: threading.Event,
        config: dict[str, Any],
        on_success: Callable[[str, float], None] | None = None,
        on_error: Callable[[str, str], None] | None = None,
    ) -> None:
        """Background thread loop for the file watcher.

        Args:
            stop_event: Event to signal watcher should stop.
            config: Configuration dictionary to use.
            on_success: Callback (filename, improvement_pct) on successful optimization.
            on_error: Callback (filename, error_msg) on failed optimization.
        """
        _logger.info("Watcher thread started")
        try:
            self._watcher_fn(config)
        except Exception as e:
            _logger.error(f"Watcher loop error: {e}")
        finally:
            _logger.info("Watcher thread stopped")

    def start_watcher(self) -> None:
        """Start the file watcher in a background thread."""
        if self._watcher_thread is not None and self._watcher_thread.is_alive():
            _logger.warning("Watcher already running, restart required")
            return

        # Reset stop event
        self._stop_event.clear()

        config = self._config_loader()
        _logger.info(f"Starting watcher with config: watch_dir={config.get('watch_dir')}")

        self._watcher_thread = threading.Thread(
            target=self._watcher_loop,
            args=(self._stop_event, config),
            daemon=True,
            name="PLT-Watcher",
        )
        self._watcher_thread.start()

    def stop_watcher(self) -> None:
        """Stop the file watcher thread."""
        if self._watcher_thread is None or not self._watcher_thread.is_alive():
            return

        _logger.info("Stopping watcher thread")
        self._stop_event.set()
        self._watcher_thread.join(timeout=10.0)
        self._watcher_thread = None

    def restart_watcher(self) -> None:
        """Restart the file watcher with current configuration."""
        self.stop_watcher()
        time.sleep(0.5)  # Brief pause for clean restart
        self.start_watcher()

    def notify_success(self, filename: str, improvement_pct: float) -> None:
        """Show a success notification.

        Args:
            filename: Name of the processed file.
            improvement_pct: Percentage improvement achieved.
        """
        if self._icon is not None:
            msg = f"Optimized {filename}\nSaved {improvement_pct:.1f}%"
            try:
                self._icon.notify(msg, "PLT-Optimizer")
            except Exception as e:
                _logger.warning(f"Failed to show notification: {e}")

    def notify_error(self, filename: str, error_msg: str) -> None:
        """Show an error notification.

        Args:
            filename: Name of the file that failed.
            error_msg: Error message describing what went wrong.
        """
        if self._icon is not None:
            msg = f"Failed: {filename}\n{error_msg[:50]}"
            try:
                self._icon.notify(msg, "PLT-Optimizer - Error")
            except Exception as e:
                _logger.warning(f"Failed to show notification: {e}")

    def run(self) -> None:
        """Run the system tray icon (blocking call)."""
        icon_image = self._load_icon_image()

        self._icon = Icon(
            name="PLT-Optimizer",
            icon=icon_image,
            title="PLT-Optimizer",
            menu=self._create_menu(),
        )

        _logger.info("Starting system tray icon")
        try:
            self._icon.run()
        except Exception as e:
            _logger.error(f"Tray icon error: {e}")
        finally:
            self.stop_watcher()

    def stop(self) -> None:
        """Stop the system tray icon."""
        if self._icon is not None:
            _logger.info("Stopping system tray icon")
            self._icon.stop()
            self._icon = None


def get_icon_path_frozen() -> Path:
    """Get path to icon.ico accounting for PyInstaller frozen builds.

    Returns:
        Path to the icon file in the bundled assets directory.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        # Running as compiled executable - look in temp extraction folder
        return Path(sys._MEIPASS) / "assets" / "icon.ico"
    else:
        # Running as Python script or in dev environment
        return Path(__file__).parent.parent.parent / "assets" / "icon.ico"


def get_icon_path_dev() -> Path:
    """Get path to icon.ico for development environments.

    Returns:
        Path to the icon file relative to project root.
    """
    return Path(__file__).parent.parent.parent / "assets" / "icon.ico"


__all__ = [
    "TrayIconManager",
    "get_icon_path_frozen",
    "get_icon_path_dev",
]
