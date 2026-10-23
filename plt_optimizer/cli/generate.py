"""Generate subcommand for PLT-Optimizer CLI.

This module provides the 'generate' command which creates per-cutter PLT
files (and simple-outline PDF previews) from a YAML job specification
using the three-phase generation pipeline: label resolution (with cutter
compensation), bounds-aware bin packing, and lossless per-label
rendering/assembly.

Output layout (under ``-o``, defaulting to the spec's parent directory)::

    <out>/plt/30_txt_<job_id>.plt        # one file per text cutter (single sheet)
    <out>/plt/wbuv_30_m_txt_<job_id>.plt  # ... per material/color tag
    <out>/plt/01_wbuv_30_txt_<job_id>.plt  # ... plate number only when a
    #                                        material spans several sheets
    <out>/plt/15_bh_<job_id>.plt         # borders + holes together
    <out>/pdf/30_txt_<job_id>.pdf        # simple-outline previews
    <out>/pdf/all_<job_id>.pdf           # combined preview per plate
    <out>/generation_report/report.json  # the machine-readable job report
    <out>/generation_report/summary.txt  # the human-readable job summary

**Output contract.** The stdout of this command is deliberately minimal: a
single summary line naming the report, and nothing else. All progress
chatter, the written file listings and the per-label layout report live in
the ``generation_report/`` directory instead, so an agent driving the
pipeline is not flooded with output it has to sift through. WARNING and
ERROR records go to stderr; ``-v`` additionally prints the full layout
report to stdout (the only verbose affordance) and raises the log level in
``logs_generate/generate.log``.

Text-hole collisions are reported per label: collisions that collision
avoidance resolves (hole-margin reduction and/or horizontal compression)
log WARNINGs and the job proceeds. Only collisions that survive every
enabled avoidance phase are unacceptable output: the offending labels are
reported at ERROR level and the job aborts with a non-zero exit code so
the jobspec can be revised. A report is written either way, so a failed
job leaves a machine-readable reason behind.

Usage:
    plt-optimizer generate spec.yaml -o output_dir --no-plots
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Light generation imports (pure Python) are safe at module scope. The heavy
# ones (layout / label_renderer / vectorize transitively pull in
# numpy + matplotlib + vpype) are imported lazily inside run(): the main CLI
# router builds every subparser at startup, and `plt-optimizer watch` must
# remain importable on Python 3.8 / Windows 7 where matplotlib cannot be
# installed (see plt_optimizer/cli/AGENTS.md).
from plt_optimizer.generate.generation_report import (
    REPORT_JSON_NAME,
    ReportCapture,
    build_report,
    report_dir_for,
    utc_now_iso,
    write_report,
)
from plt_optimizer.generate.job_config import JobConfig, load_job_config
from plt_optimizer.generate.layout_report import format_layout_report
from plt_optimizer.generate.resolution import resolve_job_spec
from plt_optimizer.generate.schema import JobSpec, parse_yaml
from plt_optimizer.generate.substitution import SubstitutionError, expand_job_spec
from plt_optimizer.utils.logging import apply_log_levels, publish_log_policy, setup_logging
from plt_optimizer.utils.stage_timing import StageTimings, stage_timer

# Module logger for coarse stage timing (parse/expand/resolve/export);
# named _LOG so it is never shadowed inside run() (see stage_timing.py).
_LOG = logging.getLogger(__name__)


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
        "--default-plots",
        action="store_true",
        help=(
            "Also write color-coded *_default.pdf diagnostic plots "
            "(rapid-travel view). Off by default."
        ),
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
    parser.add_argument(
        "--job-config",
        type=Path,
        default=Path("job-config.json"),
        help=(
            "Path to the job defaults JSON (job-config.json). Its values "
            "supply the top-most (job/plate) layer defaults for fields the "
            "spec omits; fields configured nowhere become required. "
            "Pair different tools/job-config files per engraver or toolset."
        ),
    )
    parser.add_argument(
        "--fast-mode",
        action="store_true",
        help=(
            "Use NearestNeighbor2OptStrategy exclusively for plate-space "
            "toolpath optimization. If not specified, uses "
            "ParallelEnsembleStrategy which runs multiple strategies and "
            "keeps the best (mirrors the optimize CLI)."
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

    The inventory is logged at INFO level (it lands in the log file and in
    the report's ``job.tools`` section), never printed: the CLI's stdout
    carries only the final summary line.

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
        _LOG.info("Loaded cutter inventory from %s: %s", tools_path, inventory)
    if boundary_hole_cutter is not None:
        _LOG.info("Loaded boundary/hole cutter size: %s", boundary_hole_cutter)
    return inventory, boundary_hole_cutter


def _default_plate_size(job_config: Optional[JobConfig]) -> Optional[Tuple[float, float]]:
    """Extract the ``(width, height)`` default plate size from a job config.

    Args:
        job_config: The loaded job config, or ``None``.

    Returns:
        The configured ``(plate_width, plate_height)`` when both are set,
        otherwise ``None`` (callers keep their historical defaults).
    """
    if job_config is None:
        return None
    defaults = job_config.defaults
    if defaults.plate_width is None or defaults.plate_height is None:
        return None
    return (defaults.plate_width, defaults.plate_height)


def _default_plate_clearance(job: JobSpec) -> Optional[Tuple[float, float]]:
    """Extract the ``(left, top)`` default plate clearance from a job spec.

    Reads the job-level ``left_clearance`` / ``top_clearance`` (which
    already include ``job-config.json`` injection via ``parse_yaml``).

    Args:
        job: The parsed job specification.

    Returns:
        The job-level ``(left_clearance, top_clearance)`` when at least
        one is non-zero, otherwise ``None`` (no shift on auto-allocated
        plates; the fields default to ``0.0``).
    """
    left = job.left_clearance or 0.0
    top = job.top_clearance or 0.0
    if not left and not top:
        return None
    return (left, top)


def _write_failure_report(
    report_dir: Path,
    *,
    job_name: str,
    job_id: str,
    spec_path: Path,
    output_dir: Path,
    error: BaseException,
    stage: str,
    started_at: str,
    finished_at: str,
    duration_seconds: float,
    tools: Optional[Dict[str, Any]],
    options: Dict[str, Any],
    plate_count: int,
    records: List[logging.LogRecord],
    stage_records: List[logging.LogRecord],
    timings: Optional[StageTimings] = None,
) -> None:
    """Write the report for a job that failed.

    The failure report carries the exception type, its message and the
    pipeline stage it happened in, plus every warning logged before the
    failure -- so the reason is available without reading the log file.

    Args:
        report_dir: The report directory to write into.
        job_name: The job's name.
        job_id: The sanitized job id.
        spec_path: The job specification path.
        output_dir: The output directory.
        error: The exception that aborted the job.
        stage: The pipeline stage the failure happened in.
        started_at: The job start timestamp (ISO 8601).
        finished_at: The job end timestamp (ISO 8601).
        duration_seconds: The job's wall-clock duration.
        tools: The cutter inventory and boundary/hole cutter size.
        options: The CLI options the run was made with.
        plate_count: The number of plates the job declared.
        records: The captured log records.
        stage_records: The captured stage timing records.
        timings: The explicit per-stage timing collector.
    """
    report = build_report(
        job_name=job_name,
        job_id=job_id,
        spec_path=spec_path,
        output_dir=output_dir,
        report_dir=report_dir,
        status="failed",
        error={
            "type": type(error).__name__,
            "message": str(error),
            "stage": stage,
        },
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration_seconds,
        tools=tools,
        options=options,
        plate_count=plate_count,
        records=records,
        stage_records=stage_records,
        timings=timings,
    )
    try:
        write_report(report_dir, report)
    except OSError as exc:  # pragma: no cover - defensive
        _LOG.error("Could not write the generation report to %s: %s", report_dir, exc)


def run(args: argparse.Namespace) -> int:
    """Execute the generate command.

    The pipeline retunes the process-wide ``plt_optimizer`` logger
    (WARNING by default, DEBUG under ``-v``). That logger is a singleton,
    so the level it had on entry is restored when the command returns:
    a CLI must not leave the shared logger permanently quieter (or louder)
    for whatever runs next in the same process.

    Args:
        args: Parsed command-line arguments namespace.

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    logger = logging.getLogger("plt_optimizer")
    previous_level = logger.level
    try:
        return _run_generate(args)
    finally:
        logger.setLevel(previous_level)


def _run_generate(args: argparse.Namespace) -> int:
    """Execute the generate command (the body of :func:`run`).

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
    report_dir = report_dir_for(output_dir)

    # Dual logging topology (console + file) so collision ERRORs and
    # avoidance WARNINGs are visible on the console and archived. The
    # console handler is retuned to stderr at WARNING: the CLI's stdout
    # carries only the final summary line, so an agent driving the
    # pipeline is never flooded with progress chatter.
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
    verbose = bool(args.verbose)
    # The generate pipeline runs at WARNING: the operational chatter is
    # INFO (stage timers, per-plate optimization summaries, the strategy
    # benchmark tables) and the report already carries everything worth
    # knowing. The console handler shows WARNING/ERROR on stderr; the
    # file handler keeps the full detail. `-v` escalates the whole
    # pipeline to DEBUG for a deep-dive run. The logger's previous level
    # is restored by run() when the command returns.
    apply_log_levels(
        text_logger,
        logger_level=logging.DEBUG if verbose else logging.WARNING,
        console_level=logging.DEBUG if verbose else logging.WARNING,
        file_level=logging.DEBUG if verbose else logging.WARNING,
        console_stream=sys.stderr,
    )

    # The optimizer's ensemble runs its strategies in a spawned worker
    # pool. A worker starts with a clean interpreter and builds its own
    # logger, so it knows nothing about the retuning above: without this
    # it would log at the default INFO level to stdout, and its "Running
    # <strategy> on N blocks" chatter would land on stdout anyway. The
    # policy is published to the environment, which the spawn start method
    # copies into the child.
    publish_log_policy(
        level=logging.DEBUG if verbose else logging.WARNING,
        console_stream=sys.stderr,
        log_file=text_logger.log_file,
    )

    # Capture the WARNING/ERROR records for the report, so the report can
    # explain a failed or degraded job without the log file.
    capture = ReportCapture()
    text_logger.logger.addHandler(capture)

    # The per-stage wall-clock timings are collected directly rather than
    # read back from the log: the stage timing records are INFO level and
    # never exist at the pipeline's WARNING level.
    stage_timings = StageTimings()

    started_at = utc_now_iso()
    start = time.perf_counter()
    job_name = spec_path.stem
    job_id = _sanitize_job_id(job_name)
    plate_count = 0
    tools: Optional[Dict[str, Any]] = None
    options: Dict[str, Any] = {
        "no_plots": bool(args.no_plots),
        "default_plots": bool(args.default_plots),
        "fast_mode": bool(args.fast_mode),
        "verbose": verbose,
    }

    def _fail(error: BaseException, stage: str) -> int:
        """Log the failure, write the failure report and return the exit code.

        Args:
            error: The exception that aborted the job.
            stage: The pipeline stage the failure happened in.

        Returns:
            The process exit code (1).
        """
        text_logger.error(f"Generation failed: {error}")
        capture.close()
        _write_failure_report(
            report_dir,
            job_name=job_name,
            job_id=job_id,
            spec_path=spec_path,
            output_dir=output_dir,
            error=error,
            stage=stage,
            started_at=started_at,
            finished_at=utc_now_iso(),
            duration_seconds=time.perf_counter() - start,
            tools=tools,
            options=options,
            plate_count=plate_count,
            records=capture.records,
            stage_records=capture.stage_records,
            timings=stage_timings,
        )
        print(
            f"Generation FAILED ({type(error).__name__}): {error} "
            f"(report: {report_dir / REPORT_JSON_NAME})",
            file=sys.stderr,
        )
        return 1

    try:
        job_config = load_job_config(args.job_config)
        with stage_timer("parse", _LOG, timings=stage_timings):
            job = parse_yaml(spec_path, job_config_path=args.job_config)
            # Flatten replacement-driven labels (EngraveLab "badge"/multiples)
            # into static LabelSpecs before resolution, per the schema contract.
            job = expand_job_spec(job, spec_path)
        unique_labels = len(job.labels) if job.labels is not None else 0
        plate_count = len(job.plates) if job.plates is not None else 0
        job_name = job.job_name
        job_id = _sanitize_job_id(job.job_name)
        _LOG.info(
            "Loaded %s: %d plates, %d unique labels. Output will be written to: %s",
            job.job_name,
            plate_count,
            unique_labels,
            output_dir,
        )
    except SubstitutionError as e:
        capture.close()
        print(f"Error expanding replacement text file: {e}", file=sys.stderr)
        _write_failure_report(
            report_dir,
            job_name=job_name,
            job_id=job_id,
            spec_path=spec_path,
            output_dir=output_dir,
            error=e,
            stage="expand",
            started_at=started_at,
            finished_at=utc_now_iso(),
            duration_seconds=time.perf_counter() - start,
            tools=tools,
            options=options,
            plate_count=plate_count,
            records=capture.records,
            stage_records=capture.stage_records,
            timings=stage_timings,
        )
        return 1
    except Exception as e:
        capture.close()
        print(f"Error parsing specification: {e}", file=sys.stderr)
        _write_failure_report(
            report_dir,
            job_name=job_name,
            job_id=job_id,
            spec_path=spec_path,
            output_dir=output_dir,
            error=e,
            stage="parse",
            started_at=started_at,
            finished_at=utc_now_iso(),
            duration_seconds=time.perf_counter() - start,
            tools=tools,
            options=options,
            plate_count=plate_count,
            records=capture.records,
            stage_records=capture.stage_records,
            timings=stage_timings,
        )
        return 1

    inventory, boundary_hole_cutter = _load_cutter_inventory(args.tools)
    tools = {
        "available_cutters": list(inventory) if inventory else [],
        "boundary_hole_cutter_size": boundary_hole_cutter,
    }

    # Lazy heavy imports (see module docstring note in the top-level imports).
    # The root logger's handlers are snapshotted first: some third-party
    # libraries (vpype) install a root handler of their own on import, and
    # the pipeline logger propagates to the root -- so the extra handler
    # would print every record a second time, in a foreign format. The
    # handlers that exist before the import (the test suite's capture
    # handler, a caller's own) are kept.
    root_handlers = list(logging.getLogger().handlers)
    from plt_optimizer.generate.collision.detection import LabelRenderError
    from plt_optimizer.generate.label_renderer import measure_line_vertical_extents
    from plt_optimizer.generate.packing.layout import LayoutFitError
    from plt_optimizer.generate.resolution import (
        CutterSizeError,
        memoize_extents_probe,
    )
    from plt_optimizer.generate.vectorize import export_per_cutter_plts

    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if handler not in root_handlers:
            root_logger.removeHandler(handler)

    try:
        with stage_timer("resolve", _LOG, timings=stage_timings):
            resolved_labels = resolve_job_spec(
                job,
                available_cutters=inventory,
                boundary_hole_cutter_size=boundary_hole_cutter,
                # Baseline spacing measures each line's real descender extent so
                # auto spacing and margin fitting account for it (the probe is
                # memoized and is never called for single-line labels, labels
                # stacking by ink box, or jobs without a multi-line label).
                extents_probe=memoize_extents_probe(measure_line_vertical_extents),
            )
        default_plate_size = _default_plate_size(job_config)
        with stage_timer("export", _LOG, timings=stage_timings):
            export_result = export_per_cutter_plts(
                resolved_labels,
                job.plates,
                output_dir=output_dir,
                job_id=job_id,
                job_name=job.job_name,
                optimize=True,
                plots=not args.no_plots,
                default_plots=args.default_plots,
                allow_rotation=job.allow_rotation,
                fast_mode=args.fast_mode,
                logger=text_logger,
                layout=job.layout,
                default_plate_size=default_plate_size,
                default_plate_clearance=_default_plate_clearance(job),
                job_spec=job,
                job_config=job_config,
                available_cutters=inventory,
                stage_timings=stage_timings,
            )
        exported_paths = export_result.plt_paths
    except LabelRenderError as e:
        # Unacceptable output: per-label ERROR diagnostics were already
        # logged; surface the job abort and fail with non-zero exit.
        return _fail(e, "export")
    except LayoutFitError as e:
        return _fail(e, "export")
    except CutterSizeError as e:
        return _fail(e, "resolve")
    except (OSError, ValueError) as e:
        return _fail(e, "export")

    # The report carries the whole per-label detail; the stdout carries
    # only the one-line summary. The full layout report is the one verbose
    # affordance, printed to stdout under -v.
    report = build_report(
        job_name=job.job_name,
        job_id=job_id,
        spec_path=spec_path,
        output_dir=output_dir,
        report_dir=report_dir,
        status="success",
        started_at=started_at,
        finished_at=utc_now_iso(),
        duration_seconds=time.perf_counter() - start,
        tools=tools,
        options=options,
        plate_count=plate_count,
        resolved_labels=resolved_labels,
        rendered_by_id=export_result.rendered_labels,
        packed_plates=export_result.packed_plates,
        plt_files=export_result.plt_paths,
        pdf_files=export_result.pdf_paths,
        default_pdf_files=export_result.default_pdf_paths,
        records=capture.records,
        stage_records=capture.stage_records,
        timings=stage_timings,
    )
    capture.close()
    try:
        write_report(report_dir, report)
    except OSError as exc:  # pragma: no cover - defensive
        text_logger.error(f"Could not write the generation report to {report_dir}: {exc}")

    if verbose:
        verbose_report = format_layout_report(
            resolved_labels,
            export_result.rendered_labels,
            full=True,
        )
        if verbose_report:
            print("Layout report:")
            for line in verbose_report:
                print(line)

    pdf_count = len(export_result.pdf_paths)
    print(
        f"Generated '{job.job_name}': {len(exported_paths)} PLT file(s), "
        f"{pdf_count} PDF(s) in {output_dir} "
        f"(report: {report_dir / REPORT_JSON_NAME})"
    )
    return 0
