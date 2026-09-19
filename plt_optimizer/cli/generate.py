"""Generate subcommand for PLT-Optimizer CLI.

This module provides the 'generate' command which creates per-cutter PLT
files (and simple-outline PDF previews) from a YAML job specification
using the three-phase generation pipeline: label resolution (with cutter
compensation), bounds-aware bin packing, and lossless per-label
rendering/assembly.

Output layout (under ``-o``, defaulting to the spec's parent directory)::

    <out>/plt/<job_id>_<plate>_text_0.030.plt          # one file per text cutter
    <out>/plt/<job_id>_<plate>_borders-holes_0.015.plt # borders + holes together
    <out>/pdf/<job_id>_<plate>_text_0.030.pdf          # simple-outline previews
    <out>/pdf/<job_id>_<plate>_all.pdf                 # combined preview per plate

Text-hole collisions are unacceptable output: when any label's rendered
text comes closer to a drill hole than the stroke-aware collision
threshold (stroke floor plus ``hole_text_collision_distance``), the
offending labels are reported at ERROR level and the job aborts with a
non-zero exit code so the jobspec can be revised.

Usage:
    plt-optimizer generate spec.yaml -o output_dir --no-plots
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Optional, Tuple

from plt_optimizer.generate.label_renderer import LabelRenderError
from plt_optimizer.generate.layout import LayoutFitError
from plt_optimizer.generate.resolution import resolve_job_spec
from plt_optimizer.generate.schema import parse_yaml
from plt_optimizer.generate.vectorize import export_per_cutter_plts
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
            "Output directory for the plt/ and pdf/ subdirectories. "
            "If not specified, uses the spec file's parent directory."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG) output.",
    )
    parser.add_argument(
        "--no-plots",
        action="store_true",
        help="Skip generating simple-outline PDF previews (PLT files only).",
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


def _sanitize_job_id(job_name: str) -> str:
    """Derive a filesystem-safe job identifier from a job name.

    Whitespace runs collapse to single underscores and every character
    outside ``[A-Za-z0-9._-]`` is stripped. The empty result falls back
    to ``"job"``.

    Args:
        job_name: The raw ``job_name`` from the YAML specification.

    Returns:
        A safe identifier for use in output file names.
    """
    sanitized = re.sub(r"\s+", "_", job_name.strip())
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "", sanitized)
    return sanitized or "job"


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

    # Determine output directory (default: the spec file's parent).
    output_dir = args.output if args.output is not None else spec_path.parent

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
        job_id = _sanitize_job_id(job.job_name)
        print(
            f"Loaded {job.job_name}: "
            f"{plate_count} plates, "
            f"{unique_labels} unique labels. "
            f"Output will be written to: {output_dir}"
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
        export_result = export_per_cutter_plts(
            resolved_labels,
            job.plates,
            output_dir=output_dir,
            job_id=job_id,
            optimize=True,
            plots=not args.no_plots,
        )
        exported_paths = export_result.plt_paths
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

    print(f"Generated {len(exported_paths)} per-cutter PLT file(s):")
    for path in exported_paths:
        print(f"  {path}")
    if export_result.pdf_paths:
        print(f"Generated {len(export_result.pdf_paths)} PDF preview(s):")
        for path in export_result.pdf_paths:
            print(f"  {path}")
    return 0
