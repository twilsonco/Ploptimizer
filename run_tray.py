"""PLT-Optimizer Tray Application Entry Point.

This module is the entry point for running PLT-Optimizer as a system tray application
with GUI settings. It provides:
- System tray icon with context menu
- Background file watcher thread
- Settings configuration window
- Windows startup shortcut management

Usage:
    python run_tray.py           # Run as Python script
    plt-optimizer-tray           # After PyInstaller compilation

For CLI mode (no GUI), use:
    plt-optimizer watch --watch-dir /path/to/watch
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast

# Configure basic logging for the tray app before other imports
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def get_icon_path() -> Path:
    """Get path to icon.ico accounting for PyInstaller frozen builds.

    Returns:
        Path to the icon file in the bundled assets directory.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        # Running as compiled executable - look in temp extraction folder
        icon_path = Path(sys._MEIPASS) / "assets" / "icon.ico"
    else:
        # Running as Python script or in dev environment
        icon_path = Path(__file__).parent / "assets" / "icon.ico"

    logger.info(f"Icon path resolved to: {icon_path} (exists={icon_path.exists()})")
    return icon_path


def main() -> int:
    """Main entry point for the tray application.

    Returns:
        Exit code.
    """
    logger.info("Starting PLT-Optimizer Tray Application")

    # Import here to allow early logging setup
    from plt_optimizer.utils.config import load_config, save_config
    from plt_optimizer.utils.startup import (
        create_shortcut,
        remove_shortcut,
    )
    from plt_optimizer.cli.watch import run_watcher_from_config

    try:
        from plt_optimizer.ui.tray import TrayIconManager
    except ImportError as e:
        logger.error(f"Missing required dependency: {e}")
        print(
            "ERROR: Missing required dependencies for tray mode.",
            file=sys.stderr,
        )
        print(
            "Install with: uv add pystray pillow",
            file=sys.stderr,
        )
        return 1

    # Load configuration
    config = load_config()

    # Check if watch_dir is configured (if not, show settings first)
    if not config.get("watch_dir"):
        logger.info("No watch directory configured, skipping initial settings dialog")
        logger.info("Using default config - watch directory must be set via future settings")

        # Set a reasonable default so we can start the tray
        if "watch_dir" not in config:
            config["watch_dir"] = str(Path.home() / "Desktop")

    # Global state for the application
    app_state: dict[str, Any] = {"running": True}

    def watcher_fn(cfg: dict[str, Any]) -> None:
        """Wrapper around run_watcher_from_config that handles stop events.

        Args:
            cfg: Configuration dictionary.
        """
        # Use the stop_event from app_state (created in main thread)
        stop_evt = app_state.get("stop_event")
        if stop_evt is None:
            logger.error("No stop event in app_state")
            return

        try:
            run_watcher_from_config(cfg, cast(threading.Event, stop_evt))
        except Exception as e:
            logger.error(f"Watcher error: {e}")

    def on_settings_requested() -> None:
        """Handle request to open settings window."""
        # Note: When pystray is running in blocking mode, we can't easily show
        # a tkinter modal dialog from the callback. For now, just log that it was requested.
        logger.info("Settings requested (not yet implemented in blocking mode)")
        # TODO: Implement settings by stopping watcher, showing dialog, then restarting

    def on_exit_requested() -> None:
        """Handle request to exit application."""
        logger.info("Exit requested")
        app_state["running"] = False

        # Stop the watcher
        if "stop_event" in app_state:
            app_state["stop_event"].set()

        # This will cause tray icon to stop and app to exit
        if tray_manager is not None:
            tray_manager.stop()

    # Create tray manager
    tray_manager = TrayIconManager(
        watcher_fn=watcher_fn,
        config_loader=lambda: app_state.get("config", load_config()),
        get_icon_path=get_icon_path,
    )
    tray_manager.on_settings_requested = on_settings_requested
    tray_manager.on_exit_requested = on_exit_requested

    # Start the file watcher in background
    app_state["config"] = config
    stop_event = threading.Event()
    stop_event.clear()
    app_state["stop_event"] = stop_event

    watcher_thread = threading.Thread(
        target=watcher_fn,
        args=(config,),
        daemon=True,
        name="PLT-Watcher-Main",
    )
    watcher_thread.start()

    # Run the tray icon in BLOCKING mode (pystray manages its own event loop)
    # This is the standard pattern that works on Windows
    logger.info("About to start tray manager")

    try:
        tray_manager.run(blocking=True)  # Blocking - pystray manages events
        logger.info("Tray manager returned normally")
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as e:
        logger.error(f"Tray error: {e}", exc_info=True)
    finally:
        stop_event.set()
        tray_manager.stop_watcher()
        tray_manager.stop()
        logger.info("PLT-Optimizer Tray Application stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
