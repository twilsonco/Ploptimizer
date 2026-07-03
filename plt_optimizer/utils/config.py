"""Configuration management for PLT-Optimizer.

This module handles reading and writing parameters to a JSON configuration file,
decoupling the application from CLI-only argument parsing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Default configuration values
DEFAULT_CONFIG: dict[str, Any] = {
    "watch_dir": "",
    "output_dir": "./optimized",
    "log_dir": "./logs",
    "processed_dir": None,
    "fast_mode": False,
    "debug_save_files": False,
    "run_at_startup": False,
    "first_run": True,
}


def get_config_path() -> Path:
    """Get the path to the configuration file.

    Returns:
        Path to config.json in the user's application data directory.
    """
    if sys.platform == "win32":
        base_dir = Path.home() / "AppData" / "Local" / "PLT-Optimizer"
    else:
        base_dir = Path.home() / ".config" / "plt-optimizer"

    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / "config.json"


def load_config() -> dict[str, Any]:
    """Load configuration from the JSON file.

    Returns:
        Configuration dictionary with all settings. Missing values
        are filled with defaults.
    """
    config_path = get_config_path()

    if not config_path.exists():
        # Return defaults if no config exists yet
        return DEFAULT_CONFIG.copy()

    try:
        with open(config_path, encoding="utf-8") as f:
            loaded = json.load(f)
    except (OSError, json.JSONDecodeError):
        # If file is corrupted or unreadable, return defaults
        return DEFAULT_CONFIG.copy()

    # Merge with defaults to ensure all keys exist
    config = DEFAULT_CONFIG.copy()
    config.update(loaded)

    return config


def save_config(config: dict[str, Any]) -> None:
    """Save configuration to the JSON file.

    Args:
        config: Configuration dictionary to save.
    """
    config_path = get_config_path()

    # Ensure parent directory exists
    config_path.parent.mkdir(parents=True, exist_ok=True)

    config["first_run"] = False

    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
    except OSError as e:
        raise OSError(f"Failed to save configuration: {e}") from e


def update_config(updates: dict[str, Any]) -> dict[str, Any]:
    """Update specific configuration values.

    Args:
        updates: Dictionary of values to update.

    Returns:
        Updated configuration dictionary.
    """
    config = load_config()
    config.update(updates)
    save_config(config)
    return config


__all__ = [
    "DEFAULT_CONFIG",
    "get_config_path",
    "load_config",
    "save_config",
    "update_config",
]
