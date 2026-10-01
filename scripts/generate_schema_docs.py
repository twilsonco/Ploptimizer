#!/usr/bin/env python3
"""Generate tool_options documentation from job-config.json.

This script extracts tool option metadata from job-config.json and generates
a formatted list suitable for documentation, schema, or automated docs
generation.

Usage:
    # Show the generated documentation (for copy-paste or review)
    python3 scripts/generate_schema_docs.py --show

    # Generate Python code snippet for embedding in schema
    python3 scripts/generate_schema_docs.py --show-python-code

    # Typical workflow:
    # 1. Run with --show to see the current docs
    # 2. Copy the output into your documentation system
    # 3. For pre-commit integration, see SCHEMA_DOCS_INTEGRATION.md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def load_job_config(config_path: Path) -> dict:
    """Load job-config.json.
    
    Args:
        config_path: Path to job-config.json.
    
    Returns:
        Parsed JSON dict.
    
    Raises:
        FileNotFoundError: If config_path doesn't exist.
        json.JSONDecodeError: If JSON is invalid.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"job-config.json not found at {config_path}")
    
    with open(config_path) as f:
        return json.load(f)


def format_tool_options_list(tool_options: dict) -> list[str]:
    """Format tool options as a list of lines.
    
    Each line is a bullet-point entry with full details.
    
    Args:
        tool_options: Dict of tool_options from job-config.json.
    
    Returns:
        List of formatted lines (each can be printed or joined).
    """
    if not tool_options:
        return ["(No tool options defined)"]
    
    lines = [
        "**Available Tool Options:**",
        "",
    ]
    
    for key in sorted(tool_options.keys()):
        option = tool_options[key]
        name = option.get("name", key)
        command = option.get("command", "?")
        param_type = option.get("type", "?")
        units = option.get("units", "")
        
        default = option.get("default", {})
        if isinstance(default, dict):
            text_default = default.get("text", "—")
            bh_default = default.get("borders_holes", "—")
        else:
            text_default = default if default is not None else "—"
            bh_default = text_default
        
        # Build range description
        range_desc = ""
        if param_type in ("int", "float"):
            min_val = option.get("min")
            max_val = option.get("max")
            if min_val is not None and max_val is not None:
                range_desc = f" [{min_val}–{max_val}]"
        
        # Format the line
        line = (
            f"- **{key}**: {name} ({command}, {param_type}, {units}{range_desc}). "
            f"Defaults: text={text_default}, borders_holes={bh_default}."
        )
        lines.append(line)
    
    return lines


def format_python_docstring_addition(tool_options: dict) -> list[str]:
    """Generate Python code showing tool options addition to docstring.
    
    This produces lines that can be appended to an existing docstring.
    
    Args:
        tool_options: Dict of tool_options from job-config.json.
    
    Returns:
        List of Python string literal lines ready to paste into a docstring.
    """
    lines = [
        '            "\\n"',
        '            "**Available Tool Options:**\\n"',
        '            ""',
    ]
    
    for key in sorted(tool_options.keys()):
        option = tool_options[key]
        name = option.get("name", key)
        command = option.get("command", "?")
        param_type = option.get("type", "?")
        units = option.get("units", "")
        
        default = option.get("default", {})
        if isinstance(default, dict):
            text_default = default.get("text", "—")
            bh_default = default.get("borders_holes", "—")
        else:
            text_default = default if default is not None else "—"
            bh_default = text_default
        
        # Build range description
        range_desc = ""
        if param_type in ("int", "float"):
            min_val = option.get("min")
            max_val = option.get("max")
            if min_val is not None and max_val is not None:
                range_desc = f" [{min_val}–{max_val}]"
        
        line = (
            f'            "- **{key}**: {name} ({command}, {param_type}, {units}{range_desc}). '
            f'Defaults: text={text_default}, borders_holes={bh_default}.\\n"'
        )
        lines.append(line)
    
    return lines


def main() -> int:
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Generate tool_options documentation from job-config.json"
    )
    parser.add_argument(
        '--show',
        action='store_true',
        help='Show formatted documentation'
    )
    parser.add_argument(
        '--show-python-code',
        action='store_true',
        help='Show Python docstring code ready for copy-paste into schema.py'
    )
    parser.add_argument(
        '--config',
        type=Path,
        default=Path('job-config.json'),
        help='Path to job-config.json (default: job-config.json)'
    )
    
    args = parser.parse_args()
    
    # Load config
    try:
        config = load_job_config(args.config)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"❌ Error loading {args.config}: {e}", file=sys.stderr)
        return 1
    
    tool_options = config.get('tool_options', {})
    
    if args.show_python_code:
        print("Append these lines to the description=(...) string in JobSpec.tool_options:")
        print("=" * 80)
        for line in format_python_docstring_addition(tool_options):
            print(line)
        print("=" * 80)
        return 0
    
    if args.show:
        print("Available Tool Options (for documentation):")
        print("=" * 80)
        for line in format_tool_options_list(tool_options):
            print(line)
        print("=" * 80)
        return 0
    
    # Default: show both
    print("📋 Available Tool Options:")
    print("=" * 80)
    for line in format_tool_options_list(tool_options):
        print(line)
    print()
    print("🐍 Python Docstring Code (for schema.py):")
    print("=" * 80)
    for line in format_python_docstring_addition(tool_options):
        print(line)
    print("=" * 80)
    return 0


if __name__ == '__main__':
    sys.exit(main())
