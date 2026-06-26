"""System tray management for PLT-Optimizer.

This module provides the system tray icon, context menu, and notification handling.
It runs the file watcher in a background thread while maintaining the GUI event loop.

Platform-specific implementation:
- Windows: Uses infi.systray (pywin32-based) to avoid message loop conflicts with tkinter
- Linux/macOS: Uses pystray for cross-platform compatibility

The conflict on Windows occurs because both pystray and tkinter try to handle Windows
messages in the main thread. infi.systray runs entirely in a separate thread.
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

# Platform detection for systray library choice
_IS_WINDOWS = sys.platform == "win32"

if TYPE_CHECKING:
    from PIL import Image


def _check_dependencies() -> None:
    """Check that required systray dependencies are available.

    Raises:
        ImportError: If required libraries for the current platform are missing.
    """
    if _IS_WINDOWS:
        # On Windows, check for infi.systray (and implicitly pywin32)
        if importlib.util.find_spec("infi.systray") is None:
            raise ImportError(
                "Windows systray requires infi-systray. Install with: uv add 'plt-optimizer[tray]'"
            )
    else:
        # On other platforms, check for pystray
        if importlib.util.find_spec("pystray") is None or importlib.util.find_spec("PIL") is None:
            raise ImportError(
                "System tray requires pystray and pillow. "
                "Install with: uv add 'plt-optimizer[tray]'"
            )


class TrayIconManager:
    """Manages the system tray icon, menu, and notification dispatch.

    This class encapsulates all tray-related functionality including:
    - Creating and displaying the system tray icon
    - Handling menu actions (Open Settings, Exit)
    - Running the file watcher in a background thread
    - Dispatching notifications on file processing events

    The implementation uses different systray libraries based on platform:
    - Windows: infi.systray (avoids tkinter message loop conflicts)
    - Other: pystray

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

        # Platform-specific tray handle (pystray.Icon or infi.systray.SysTrayIcon)
        self._systray: Any = None
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
            _logger.debug(f"Icon loaded successfully: {img.size}, mode={img.mode}")
            return img
        except FileNotFoundError:
            _logger.warning(f"Icon not found at {icon_path}, creating default fallback")
            # Create a simple 64x64 blue square as fallback
            return Image.new("RGB", (64, 64), color=(0, 120, 200))
        except Exception as e:
            _logger.error(f"Failed to load icon from {icon_path}: {e}", exc_info=True)
            # Fall back to default
            return Image.new("RGB", (64, 64), color=(0, 120, 200))

    # === pystray methods (Linux/macOS) ===

    def _create_pystray_menu(self) -> Any:
        """Create the tray icon context menu using pystray.

        Returns:
            pystray.Menu object with standard items.
        """
        from pystray import Menu, MenuItem

        return Menu(
            MenuItem("Open Settings", self._on_settings_click),
            Menu.SEPARATOR,
            MenuItem("Exit", self._on_exit_click),
        )

    def _setup_pystray(self) -> None:
        """Set up the tray icon using pystray (Linux/macOS)."""
        from pystray import Icon

        icon_image = self._load_icon_image()

        self._systray = Icon(
            name="PLT-Optimizer",
            icon=icon_image,
            title="PLT-Optimizer",
            menu=self._create_pystray_menu(),
        )
        _logger.info("Pystray icon created successfully")

    def _on_settings_click(self, icon: Any, item: Any) -> None:
        """Handle 'Open Settings' menu click (pystray callback).

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
        """Handle 'Exit' menu click (pystray callback).

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

    # === infi.systray methods (Windows) ===

    def _setup_infi_systray(self) -> None:
        """Set up the tray icon using infi.systray (Windows only).

        The key advantage is that infi.systray runs in a separate thread and doesn't
        conflict with tkinter's message loop.
        """
        from infi.systray import SysTrayIcon

        icon_path = str(self._get_icon_path())

        # Create menu as tuple of (label, icon_file_or_None, callback) tuples
        # Note: infi.systray automatically adds a Quit option when on_quit is provided
        # so we don't need to include Exit in the menu ourselves
        menu_options = (("Open Settings", None, self._on_infi_settings_click),)

        def on_quit_callback(systray: Any) -> None:
            """Called when user clicks Quit (auto-added by infi.systray)."""
            _logger.debug("infi.systray quit callback")
            if self.on_exit_requested is not None:
                try:
                    self.on_exit_requested()
                except Exception as e:
                    _logger.error(f"Error during exit: {e}", exc_info=True)

        self._systray = SysTrayIcon(
            icon_path,
            "PLT-Optimizer",
            menu_options,
            on_quit=on_quit_callback,
        )
        _logger.info("infi.systray icon created successfully")

    def _on_infi_settings_click(self, systray: Any) -> None:
        """Handle 'Open Settings' menu click (infi.systray callback).

        Args:
            systray: The infi.systray SysTrayIcon instance.
        """
        _logger.debug("Settings requested from tray menu (infi)")
        if self.on_settings_requested is not None:
            try:
                self.on_settings_requested()
            except Exception as e:
                _logger.error(f"Error opening settings: {e}")

    def _on_infi_exit_click(self, systray: Any) -> None:
        """Handle 'Exit' menu click (infi.systray callback).

        Args:
            systray: The infi.systray SysTrayIcon instance.
        """
        _logger.debug("Exit requested from tray menu (infi)")
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
        msg = f"Optimized {filename}\nSaved {improvement_pct:.1f}%"
        _logger.info(f"Systray notification (success): {msg}")
        # Note: infi.systray doesn't have built-in notifications like pystray
        # If needed, could use win32api to show toast via Windows API

    def notify_error(self, filename: str, error_msg: str) -> None:
        """Show an error notification.

        Args:
            filename: Name of the file that failed.
            error_msg: Error message describing what went wrong.
        """
        msg = f"Failed: {filename}\n{error_msg[:50]}"
        _logger.info(f"Systray notification (error): {msg}")

    # === Main run/stop methods ===

    def run(self, blocking: bool = True) -> None:
        """Run the system tray icon.

        Args:
            blocking: If True (default), runs with its own message loop (blocking).
                     If False, runs detached and caller must manage events.
        """
        if _IS_WINDOWS:
            self._run_windows(blocking)
        else:
            self._run_pystray(blocking)

    def _run_windows(self, blocking: bool) -> None:
        """Run the system tray using infi.systray on Windows.

        Args:
            blocking: If True, blocks until shutdown. If False, returns immediately.
        """
        self._setup_infi_systray()

        if blocking:
            _logger.info("Starting infi.systray icon (blocking mode)")
            try:
                # infi.systray uses context manager pattern
                with self._systray:
                    import time

                    while True:
                        time.sleep(1.0)
            except KeyboardInterrupt:
                _logger.info("Keyboard interrupt received")
            except Exception as e:
                _logger.error(f"Tray icon error during run(): {e}", exc_info=True)
                raise
            finally:
                self.stop_watcher()
        else:
            # For non-blocking, we need to run in a thread because infi.systray
            # doesn't have run_detached like pystray
            _logger.info("Starting infi.systray icon (detached mode)")

            def tray_thread() -> None:
                try:
                    with self._systray:
                        import time

                        while True:
                            time.sleep(1.0)
                except Exception as e:
                    _logger.error(f"Tray thread error: {e}")

            thread = threading.Thread(target=tray_thread, daemon=True, name="Systray-Watcher")
            thread.start()

    def _run_pystray(self, blocking: bool) -> None:
        """Run the system tray using pystray on Linux/macOS.

        Args:
            blocking: If True, blocks. If False, runs detached.
        """
        self._setup_pystray()

        if blocking:
            # Traditional blocking mode - pystray manages its own message loop
            _logger.info("Starting pystray icon (blocking mode)")
            try:
                self._systray.run()
            except Exception as e:
                _logger.error(f"Tray icon error during run(): {e}", exc_info=True)
                raise
            finally:
                self.stop_watcher()
        else:
            # Detached mode - pystray runs in background, caller manages events
            _logger.info("Starting pystray icon (detached mode)")
            self._systray.run_detached()

    def stop(self) -> None:
        """Stop the system tray icon.

        Note: For infi.systray, we do NOT call shutdown() here because it would
        cause a deadlock - shutdown() tries to join the thread it's called from.
        Since we're exiting anyway, just clearing the reference is sufficient;
        the daemon thread will terminate when the process exits.
        """
        if self._systray is not None:
            _logger.info("Stopping system tray icon")
            if hasattr(self._systray, "stop") and not hasattr(self._systray, "shutdown"):
                # pystray - safe to call stop()
                try:
                    self._systray.stop()
                except Exception as e:
                    _logger.warning(f"Error during systray stop: {e}")
            elif hasattr(self._systray, "shutdown"):
                # infi.systray - skip shutdown() to avoid deadlock (see docstring)
                _logger.debug("Skipping infi.systray.shutdown() to prevent deadlock")
            self._systray = None


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


# Validate dependencies when module loads
_check_dependencies()


__all__ = [
    "TrayIconManager",
    "get_icon_path_frozen",
    "get_icon_path_dev",
]
