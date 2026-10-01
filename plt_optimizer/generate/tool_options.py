"""Tool option (HPGL header command) generation and bounds clamping.

This module handles:
- Extracting tool option values from a JobSpec
- Applying job-config defaults (with dual text/borders_holes support)
- Clamping int/float values to configured ranges
- Generating HPGL header command strings with proper formatting
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from plt_optimizer.generate.job_config import JobConfig
    from plt_optimizer.generate.schema import JobSpec

logger = logging.getLogger(__name__)


def generate_tool_option_headers(
    job_spec: JobSpec,
    job_config: Optional[JobConfig],
    plt_type: str,
) -> list[str]:
    """Generate HPGL header command strings from tool options.

    Applies job-config defaults (selecting the appropriate dual default for
    plt_type: "text" vs "borders_holes"), clamps int/float values to their
    configured ranges, and generates formatted HPGL commands.

    Args:
        job_spec: The resolved job specification (may have tool_options dict).
        job_config: The loaded job-config (with tool_options metadata and defaults).
        plt_type: Either "text" or "borders_holes" to select the dual default.

    Returns:
        List of HPGL header command strings (e.g., ["VS0.80;", "ZO124,50;"]).
        If job_spec has no tool_options and job_config is None, returns empty list.
    """
    if job_config is None or job_config.defaults.tool_options is None:
        # No config available; use job_spec tool_options if provided
        if not job_spec.tool_options:
            return []
        config_tool_options = {}
    else:
        config_tool_options = job_config.defaults.tool_options

    job_tool_options = job_spec.tool_options or {}

    headers = []

    # Iterate tool_options metadata from config (maintains consistent order)
    for option_key in sorted(config_tool_options.keys()):
        option_metadata = config_tool_options[option_key]

        # Determine final value (priority: job_spec > job-config default > None)
        if option_key in job_tool_options:
            value = job_tool_options[option_key]
        else:
            # Use job-config default for this plt_type
            default_spec = option_metadata.get("default")
            if default_spec is None:
                continue
            if isinstance(default_spec, dict):
                # Dual default: select based on plt_type
                value = default_spec.get(plt_type)
            else:
                # Legacy single default (for backwards compatibility)
                value = default_spec

        # Skip if value is None (header should be omitted)
        if value is None:
            continue

        # Validate and clamp value based on type
        param_type = option_metadata.get("type", "bool")
        command = option_metadata.get("command", "")

        if param_type == "float":
            value = _clamp_float(value, option_key, option_metadata)
            header = _format_float_header(command, value)
        elif param_type == "int":
            value = _clamp_int(value, option_key, option_metadata)
            header = _format_int_header(command, value)
        elif param_type == "bool":
            header = _format_bool_header(command, value)
        else:
            logger.warning(f"Unknown tool_option type '{param_type}' for '{option_key}'")
            continue

        headers.append(header)

    return headers


def _clamp_float(value: Any, option_key: str, metadata: dict[str, Any]) -> float:
    """Clamp a float value to its configured range.

    Logs a WARNING if the value was out of bounds and had to be clamped.

    Args:
        value: The value to clamp.
        option_key: The option key (for logging).
        metadata: The option metadata dict (with 'min' and 'max' keys).

    Returns:
        The clamped float value.
    """
    try:
        fvalue = float(value)
    except (ValueError, TypeError):
        logger.error(f"Tool option '{option_key}' expected float, got {type(value).__name__}")
        raise

    min_val = metadata.get("min")
    max_val = metadata.get("max")

    if min_val is not None and fvalue < min_val:
        logger.warning(
            f"Tool option '{option_key}' value {fvalue} is below minimum {min_val}; "
            f"clamping to {min_val}"
        )
        return float(min_val)

    if max_val is not None and fvalue > max_val:
        logger.warning(
            f"Tool option '{option_key}' value {fvalue} is above maximum {max_val}; "
            f"clamping to {max_val}"
        )
        return float(max_val)

    return fvalue


def _clamp_int(value: Any, option_key: str, metadata: dict[str, Any]) -> int:
    """Clamp an int value to its configured range.

    Logs a WARNING if the value was out of bounds and had to be clamped.

    Args:
        value: The value to clamp.
        option_key: The option key (for logging).
        metadata: The option metadata dict (with 'min' and 'max' keys).

    Returns:
        The clamped int value.
    """
    try:
        ivalue = int(value)
    except (ValueError, TypeError):
        logger.error(f"Tool option '{option_key}' expected int, got {type(value).__name__}")
        raise

    min_val = metadata.get("min")
    max_val = metadata.get("max")

    if min_val is not None and ivalue < min_val:
        logger.warning(
            f"Tool option '{option_key}' value {ivalue} is below minimum {min_val}; "
            f"clamping to {min_val}"
        )
        return int(min_val)

    if max_val is not None and ivalue > max_val:
        logger.warning(
            f"Tool option '{option_key}' value {ivalue} is above maximum {max_val}; "
            f"clamping to {max_val}"
        )
        return int(max_val)

    return ivalue


def _format_float_header(command: str, value: float) -> str:
    """Format a float header command.

    For most commands like "VS", the format is "VS{value:.2f};". For some
    commands like "ZU" (z clearance), the value is scaled (e.g., 0.2 -> 200).
    This function uses the command as-is and formats the value with 2 decimal
    places by default (matching existing HPGL output).

    Args:
        command: The HPGL command (e.g., "VS", "VZ", "ZU").
        value: The float value to format.

    Returns:
        The formatted header string (e.g., "VS0.80;").
    """
    # Special handling for commands that need scaling
    if command == "ZU":
        # z clearance: convert inches to plotter units (multiply by 1000)
        scaled = int(value * 1000)
        return f"{command}{scaled};"
    # Default: format with 2 decimal places (matching HPGL conventions)
    return f"{command}{value:.2f};"


def _format_int_header(command: str, value: int) -> str:
    """Format an int header command.

    Args:
        command: The HPGL command (e.g., "ZO124,", "ZO100,", "ZO105,").
        value: The int value to format.

    Returns:
        The formatted header string (e.g., "ZO124,50;").
    """
    return f"{command}{value};"


def _format_bool_header(command: str, value: bool) -> str:
    """Format a bool header command.

    Args:
        command: The HPGL command (e.g., "ZO123,", "ZO102,", "ZO104,").
        value: The bool value (True -> 1, False -> 0).

    Returns:
        The formatted header string (e.g., "ZO123,1;" or "ZO104,0;").
    """
    int_val = 1 if value else 0
    return f"{command}{int_val};"


def prepend_tool_option_headers(
    plt_content: str,
    job_spec: JobSpec,
    job_config: Optional[JobConfig],
    plt_type: str,
) -> str:
    """Prepend tool option headers to PLT content.

    Inserts tool option headers after the "IN;" initialization command.
    Works with both line-oriented and single-line HPGL content.

    Args:
        plt_content: The original HPGL PLT content string.
        job_spec: The resolved job specification.
        job_config: The loaded job-config (with tool_options metadata).
        plt_type: Either "text" or "borders_holes".

    Returns:
        The PLT content with tool option headers prepended (after IN;).
    """
    headers = generate_tool_option_headers(job_spec, job_config, plt_type)

    if not headers:
        # No headers to add; return content as-is
        return plt_content

    # Find the IN; command and insert headers after it
    # PLT content may be all on one line (IN;DF;PS0;...) or multi-line
    in_index = plt_content.find("IN;")
    if in_index == -1:
        logger.warning(
            f"Could not find 'IN;' command in {plt_type} PLT content; "
            "tool option headers will not be prepended."
        )
        return plt_content

    # Insert headers after IN;
    header_string = "".join(headers)
    return plt_content[: in_index + 3] + header_string + plt_content[in_index + 3 :]
