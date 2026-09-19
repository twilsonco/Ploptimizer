"""Generate subcommand for PLT-Optimizer CLI.

This module provides the 'generate' command which creates PLT files from
a YAML job specification using the three-phase generation pipeline:
label resolution (with cutter compensation), bounds-aware bin packing,
and lossless per-label rendering/assembly.

Text-hole collisions are unacceptable output: when any label's rendered
text comes closer to a drill hole than the stroke-aware collision
threshold (stroke floor plus ``hole_text_collision_distance``), the
offending labels are reported at ERROR level and the job aborts with a
non-zero exit code so the jobspec can be revised.

Usage:
    plt-optimizer generate spec.yaml -o output.plt
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional, Tuple

from plt_optimizer.generate.label_renderer import LabelRenderError
from plt_optimizer.generate.layout import LayoutFitError
from plt_optimizer.generate.resolution import resolve_job_spec
from plt_optimizer.generate.schema import parse_yaml
from plt_optimizer.generate.vectorize import export_and_optimize_phase3
from plt_optimizer.utils.logging import setup_logging


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
    parser.add_argument(
        "--tools",
        type=Path,
        default=Path("tools.json"),
        help=(
            "Path to the cutter inventory JSON (available_cutters list). "
            "If the file does not exist, ideal cutters are used."
        ),
    )


def _load_cutter_inventory(
    tools_path: Path,
) -> Tuple[Optional[list[float]], Optional[float]]:
    """Load the cutter inventory from a tools JSON file.

    Args:
        tools_path: Path to tools.json (``{"available_cutters": [...],
        "boundary_hole_cutter_size": float}``).

    Returns:
        Tuple of (list of cutter diameters in inches or None when the
        file/list is missing (ideal cutters are then used), requested
        boundary/hole cutter size in inches or None when the key is
        absent).
    """
    if not tools_path.is_file():
        return None, None
    try:
        with open(tools_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None
    inventory = data.get("available_cutters") or None
    boundary_hole_cutter = data.get("boundary_hole_cutter_size")
    if inventory:
        print(f"Loaded cutter inventory from {tools_path}: {inventory}")
    if boundary_hole_cutter is not None:
        print(f"Loaded boundary/hole cutter size: {boundary_hole_cutter}")
    return inventory, boundary_hole_cutter


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

    # Dual logging topology (console + file) so collision ERRORs and
    # avoidance WARNINGs are visible on the console and archived.
    log_dir = Path("./logs_generate")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        print(f"Error: Cannot create log directory '{log_dir}': {e}", file=sys.stderr)
        return 1

    text_logger, _metrics_logger = setup_logging(
        text_log_file=log_dir / "generate.log",
        csv_metrics_file=log_dir / "job_metrics.csv",
    )
    if args.verbose:
        text_logger.logger.setLevel(logging.DEBUG)

    try:
        job = parse_yaml(spec_path)
        unique_labels = len(job.labels) if job.labels is not None else 0
        plate_count = len(job.plates) if job.plates is not None else 0
        print(
            f"Loaded {job.job_name}: "
            f"{plate_count} plates, "
            f"{unique_labels} unique labels. "
            f"Output will be written to: {output_path}"
        )
    except Exception as e:
        print(f"Error parsing specification: {e}", file=sys.stderr)
        return 1

    inventory, boundary_hole_cutter = _load_cutter_inventory(args.tools)

    try:
        resolved_labels = resolve_job_spec(
            job,
            available_cutters=inventory,
            boundary_hole_cutter_size=boundary_hole_cutter,
        )
        exported_paths = export_and_optimize_phase3(
            resolved_labels,
            job.plates,
            output_dir=output_path.parent if str(output_path.parent) else Path("."),
            optimize=True,
            separate_layers=False,
        )
    except LabelRenderError as e:
        # Unacceptable output: per-label ERROR diagnostics were already
        # logged; surface the job abort and fail with non-zero exit.
        text_logger.error(f"Generation aborted: {e}")
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except LayoutFitError as e:
        text_logger.error(f"Layout failed: {e}")
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as e:
        text_logger.error(f"Generation failed: {e}")
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Single-plate jobs: move the exported file onto the requested path.
    if len(exported_paths) == 1 and exported_paths[0] != output_path:
        try:
            exported_paths[0].replace(output_path)
            exported_paths = [output_path]
        except OSError as e:
            print(f"Error: Could not write output to {output_path}: {e}", file=sys.stderr)
            return 1

    print(f"Generated {len(exported_paths)} PLT file(s):")
    for path in exported_paths:
        print(f"  {path}")
    return 0
