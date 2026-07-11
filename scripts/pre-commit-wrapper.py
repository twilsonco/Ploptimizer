#!/usr/bin/env python
"""Wrapper script for running pre-commit hooks."""

import sys
import subprocess
from pathlib import Path

def run_command(cmd_list, description):
    """Run a command and return exit code."""
    print(f"\n{description}...")
    result = subprocess.run(cmd_list, cwd=Path(__file__).parent.parent)
    return result.returncode

def main() -> int:
    """Run linting and type checking."""
    exit_code = 0
    
    # Run ruff check
    result = run_command(
        [sys.executable, "-m", "ruff", "check", "plt_optimizer/"],
        "Running ruff check"
    )
    if result != 0:
        exit_code = result
    
    # Run ruff format check
    result = run_command(
        [sys.executable, "-m", "ruff", "format", "--check", "plt_optimizer/"],
        "Running ruff format check"
    )
    if result != 0:
        exit_code = result
    
    # Run mypy
    result = run_command(
        [sys.executable, "-m", "mypy", "plt_optimizer/"],
        "Running mypy type check"
    )
    if result != 0:
        exit_code = result
    
    return exit_code

if __name__ == "__main__":
    sys.exit(main())
