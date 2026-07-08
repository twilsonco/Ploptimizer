"""System tray management for PLT-Optimizer.

This module provides the system tray icon, context menu, and notification handling.
It runs the file watcher in a background thread while maintaining the GUI event loop.

Implementation uses ``pystray`` on all platforms for cross-platform compatibility.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Module-level logger
_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from PIL import Image


def _safe_find_spec(name: str) -> bool:
    """Safely check whether a module spec can be located.

    ``importlib.util.find_spec`` is documented to return ``None`` when a
    module is unavailable, but in practice it raises ``ModuleNotFoundError``
    when an intermediate package on a dotted path is missing entirely. This
    helper swallows those errors so the module-level dependency probe below
    never crashes the importer, which is critical for headless CI runners
    that do not install the optional ``tray`` extras.

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


def _check_dependencies() -> None:
    """Check that required systray dependencies are available.

    This is invoked when a tray is actually about to be created (i.e. from
    :meth:`TrayIconManager.run`), not at module import time. This lets the
    module load cleanly on systems where the optional ``tray`` extras are
    not installed (e.g. headless CI runners), while still raising a helpful
    error when the user actually tries to run the system tray.

    Raises:
        ImportError: If required libraries are missing.
    """
    if not _safe_find_spec("pystray") or not _safe_find_spec("PIL"):
        raise ImportError(
            "System tray requires pystray and pillow. Install with: uv add 'plt-optimizer[tray]'"
        )


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

        self._icon: Any = None
        self._watcher_thread: threading.Thread | None = None
        self._stop_event: threading.Event = threading.Event()

        # Callbacks for UI events - set by consumer
        self.on_settings_requested: Callable[[], None] | None = None
        self.on_exit_requested: Callable[[], None] | None = None

    def _load_icon_image(self) -> Image.Image:
        """Load the icon image from file.

        Returns:
            PIL Image object for the tray icon.
        """
        # Import here to avoid hard dependency on non-Windows
        from PIL import Image

        icon_path = self._get_icon_path()
        _logger.info(f"Attempting to load icon from: {icon_path}")
        try:
            img = Image.open(icon_path)
            _logger.info(f"Icon loaded successfully: {img.size}, mode={img.mode}")
            return img
        except FileNotFoundError:
            _logger.warning(f"Icon not found at {icon_path}, creating default fallback")
            # Create a simple 64x64 blue square as fallback
            return Image.new("RGB", (64, 64), color=(0, 120, 200))
        except Exception as e:
            _logger.error(f"Failed to load icon from {icon_path}: {e}", exc_info=True)
            # Fall back to default
            return Image.new("RGB", (64, 64), color=(0, 120, 200))

    def _create_menu(self) -> Any:
        """Create the tray icon context menu.

        Returns:
            pystray.Menu object with standard items.
        """
        from pystray import Menu, MenuItem

        return Menu(
            MenuItem("Open Settings", self._on_settings_click),
            Menu.SEPARATOR,
            MenuItem("Exit", self._on_exit_click),
        )

    def _on_settings_click(self, icon: Any, item: Any) -> None:
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

    def _on_exit_click(self, icon: Any, item: Any) -> None:
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

    # === Notification methods ===

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

    # === Main run/stop methods ===

    def run(self, blocking: bool = True) -> None:
        """Run the system tray icon.

        Args:
            blocking: If True (default), runs with its own message loop (blocking).
                     If False, runs detached and caller must manage events.

        Raises:
            ImportError: If ``pystray`` + ``pillow`` are not installed.
        """
        # Validate dependencies at the public entry point rather than at
        # module import time so the module can load cleanly on systems that
        # do not install the optional ``tray`` extras (e.g. CI runners).
        _check_dependencies()

        from pystray import Icon

        _logger.info("Loading tray icon image")
        icon_image = self._load_icon_image()
        _logger.debug(f"Icon image loaded: {icon_image}, size={icon_image.size}")

        try:
            self._icon = Icon(
                name="PLT-Optimizer",
                icon=icon_image,
                title="PLT-Optimizer",
                menu=self._create_menu(),
            )
            _logger.info("Tray icon object created successfully")
        except Exception as e:
            _logger.error(f"Failed to create tray icon: {e}", exc_info=True)
            raise

        if blocking:
            # Traditional blocking mode - pystray manages its own message loop
            _logger.info("Starting system tray icon (blocking mode)")
            try:
                self._icon.run()
            except Exception as e:
                _logger.error(f"Tray icon error during run(): {e}", exc_info=True)
                raise
            finally:
                self.stop_watcher()
        else:
            # Detached mode - pystray runs in background, caller manages events
            _logger.info("Starting system tray icon (detached mode)")
            self._icon.run_detached()

    def stop(self) -> None:
        """Stop the system tray icon."""
        if self._icon is not None:
            _logger.info("Stopping system tray icon")
            try:
                self._icon.stop()
            except Exception as e:
                _logger.warning(f"Error during systray stop: {e}")
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
