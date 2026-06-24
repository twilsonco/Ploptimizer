"""Utility modules for PLT-Optimizer."""

from plt_optimizer.utils.config import (
    DEFAULT_CONFIG,
    get_config_path,
    load_config,
    save_config,
    update_config,
)
from plt_optimizer.utils.geometry import calculate_distance, calculate_path_length
from plt_optimizer.utils.logging import (
    CSVMetricsLogger,
    TextLogger,
    setup_logging,
)
from plt_optimizer.utils.startup import (
    APP_NAME,
    create_shortcut,
    get_executable_path,
    get_startup_folder,
    is_startup_enabled,
    remove_shortcut,
)

__all__ = [
    "TextLogger",
    "CSVMetricsLogger",
    "setup_logging",
    "calculate_distance",
    "calculate_path_length",
    "DEFAULT_CONFIG",
    "get_config_path",
    "load_config",
    "save_config",
    "update_config",
    "APP_NAME",
    "get_startup_folder",
    "get_executable_path",
    "create_shortcut",
    "remove_shortcut",
    "is_startup_enabled",
]
