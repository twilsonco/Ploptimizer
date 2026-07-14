"""PLT-Optimizer CLI entry point.

This module serves as the main router for all PLT-Optimizer commands,
dispatching to the appropriate subcommand handler (optimize, generate, watch).

Usage:
    plt-optimizer optimize input.plt -o output.plt
    plt-optimizer generate spec.yaml
    plt-optimizer watch --watch-dir /path/to/watch

For help on a specific subcommand:
    plt-optimizer optimize --help
    plt-optimizer generate --help
    plt-optimizer watch --help
"""

from __future__ import annotations

import argparse
import sys

from plt_optimizer.cli import generate, optimize, watch


def main() -> int:
    """Main entry point for the PLT-Optimizer CLI.

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    parser = argparse.ArgumentParser(
        prog="plt-optimizer",
        description="PLT-Optimizer: HPGL processing and CAM generation suite.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Optimize a single PLT file
  plt-optimizer optimize input.plt -o output.plt

  # Generate from YAML specification (Phase 2+)
  plt-optimizer generate job_spec.yaml

  # Watch directory for automatic optimization
  plt-optimizer watch --watch-dir /path/to/watch --output-dir ./optimized

For more information on a specific command, run:
  plt-optimizer <command> --help
        """,
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        help="Available commands",
    )

    # Subcommand: optimize
    optimize_parser = subparsers.add_parser(
        "optimize",
        help="Optimize an existing PLT file.",
        description="Parse, optimize, and write a single PLT file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic optimization with default output filename
  plt-optimizer optimize input.plt

  # Specify output file and fast mode
  plt-optimizer optimize input.plt -o optimized.plt --fast-mode

  # With verbose logging
  plt-optimizer optimize input.plt -v --log-dir ./my_logs
        """,
    )
    optimize.setup_parser(optimize_parser)

    # Subcommand: generate
    generate_parser = subparsers.add_parser(
        "generate",
        help="Generate and optimize a PLT file from a YAML specification.",
        description="Create a PLT file from a YAML job specification (Phase 2+).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate from spec (stubbed in Phase 1)
  plt-optimizer generate spec.yaml

  # Specify output path
  plt-optimizer generate spec.yaml -o output.plt
        """,
    )
    generate.setup_parser(generate_parser)

    # Subcommand: watch
    watch_parser = subparsers.add_parser(
        "watch",
        help="Watch a directory for new PLT files and auto-optimize.",
        description="Run the hot-folder watch daemon for batch optimization.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic watch with default directories
  plt-optimizer watch --watch-dir /path/to/watch

  # With explicit output and fast mode
  plt-optimizer watch --watch-dir /input \\
                     --output-dir ./optimized \\
                     --fast-mode

  # Archive processed files to a separate directory
  plt-optimizer watch --watch-dir /input \\
                     --processed-dir /archive
        """,
    )
    watch.setup_parser(watch_parser)

    args = parser.parse_args()

    # Route to the appropriate module's run function
    if args.command == "optimize":
        return optimize.run(args)
    elif args.command == "generate":
        return generate.run(args)
    elif args.command == "watch":
        return watch.run(args)
    else:
        # This should never happen since subparsers is required=True
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
