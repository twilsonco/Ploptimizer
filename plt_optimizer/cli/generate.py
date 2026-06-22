"""Generate subcommand for PLT-Optimizer CLI.

This module provides the 'generate' command which creates a PLT file from
a YAML job specification (for future CAM generation functionality).

Usage:
    plt-optimizer generate spec.yaml -o output.plt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from plt_optimizer.generate.schema import parse_yaml


def setup_parser(parser: argparse.ArgumentParser) -> None:
    """Configure argument parser for the generate subcommand.

    Args:
        parser: ArgumentParser instance to configure.
    """
    parser.add_argument(
        "spec",
        type=Path,
        help="Path to the job specification YAML file.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help=(
            "Output PLT file path. If not specified, uses the spec filename with .plt extension."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG) output.",
    )


def run(args: argparse.Namespace) -> int:
    """Execute the generate command.

    Args:
        args: Parsed command-line arguments namespace.

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    spec_path = args.spec

    # Validate spec file exists
    if not spec_path.exists():
        print(f"Error: Specification file does not exist: {spec_path}", file=sys.stderr)
        return 1

    if not spec_path.is_file():
        print(
            f"Error: Specification path is not a file: {spec_path}",
            file=sys.stderr,
        )
        return 1

    # Determine output path
    if args.output is not None:
        output_path = args.output
    else:
        output_path = spec_path.parent / f"{spec_path.stem}.plt"

    try:
        job = parse_yaml(spec_path)
        unique_labels = len(job.labels)
        print(
            f"Loaded {job.job_name}: "
            f"{len(job.plates)} plates, "
            f"{unique_labels} unique labels. "
            f"Output will be written to: {output_path}"
        )
        # TODO: Implement actual PLT generation in Phase 3
    except Exception as e:
        print(f"Error parsing specification: {e}", file=sys.stderr)
        return 1

    # TODO: Implement actual generation logic in Phase 2+
    return 0
