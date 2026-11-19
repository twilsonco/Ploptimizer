"""Generate subcommand for PLT-Optimizer CLI.

This module provides the 'generate' command which creates per-cutter PLT
files (and simple-outline PDF previews) from a YAML job specification
using the three-phase generation pipeline: label resolution (with cutter
compensation), bounds-aware bin packing, and lossless per-label
rendering/assembly.

Output layout (under ``-o``, defaulting to the spec's parent directory)::

    <out>/plt/30_txt_<job_id>.plt        # one file per text cutter (single sheet)
    <out>/plt/wk_uv_30_m_txt_<job_id>.plt  # ... per material/color tag
    <out>/plt/wk_uv_01_30_txt_<job_id>.plt  # ... plate number only when a
    #                                        material spans several sheets
    <out>/plt/15_bh_<job_id>.plt         # borders + holes together
    <out>/pdf/30_txt_<job_id>.pdf        # simple-outline previews
    <out>/pdf/all_<job_id>.pdf           # combined colored proof per plate
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
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

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
from plt_optimizer.generate.job_config import load_job_config
from plt_optimizer.generate.job_prep import (
    JobPreparationError,
    default_plate_clearance,
    default_plate_size,
    prepare_job,
    sanitize_job_id,
)
from plt_optimizer.generate.layout_report import format_layout_report
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
        nargs="?",
        default=None,
        help="Path to the job specification YAML file (omit with --jobset).",
    )
    parser.add_argument(
        "--jobset",
        type=Path,
        default=None,
        metavar="JOBSET.yaml",
        help=(
            "Path to a jobset YAML file listing several job specs to pack, "
            "export and report as one run (per-job reports are written under "
            "<output>/<job_id>/generation_report/ beside the batch report). "
            "Mutually exclusive with the positional spec argument."
        ),
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
    # NOTE: the positional ``spec`` is optional so ``--jobset`` can replace it,
    # which argparse cannot express for a positional + optional pair. The
    # "exactly one of them" contract is therefore checked by
    # :func:`_run_generate` at dispatch time (a parser pre-pass here would run
    # every subparser's ``-h`` action as a side effect and hijack ``--help``).


def _sanitize_job_id(job_name: str) -> str:
    """Derive a filesystem-safe job identifier from a job name.

    Delegates to :func:`plt_optimizer.core.filenames.sanitize_filename_component`:
    international characters are transliterated to ASCII (``"Caf\\u00e9"`` ->
    ``"Cafe"``) and every run of punctuation or whitespace collapses to a
    single underscore (``"2026-10-06 wk-uv"`` -> ``"2026_10_06_wk_uv"``), so an
    id carries only ASCII letters, digits and underscores. The empty result
    falls back to ``"job"``.

    Args:
        job_name: The raw ``job_name`` from the YAML specification.

    Returns:
        A safe identifier for use in output file names.
    """
    return sanitize_job_id(job_name)


# The per-job front half of the pipeline (config load, parse + expand,
# resolution) moved to :mod:`plt_optimizer.generate.job_prep` so a jobset
# resolves every member job through exactly this code path. The private
# aliases below stay for the callers outside this module (the profiler
# scripts, the test suite) that address them here.
_default_plate_size = default_plate_size
_default_plate_clearance = default_plate_clearance


class _RunBootstrap(NamedTuple):
    """Per-run services shared by the single-job and jobset pipelines.

    Attributes:
        text_logger: The dual (console + file) text logger, retuned to the
            pipeline's level.
        capture: The handler collecting WARNING/ERROR records for the report.
        timings: The explicit per-stage timing collector.
        started_at: Run start timestamp (ISO 8601).
        clock: ``time.perf_counter()`` at run start.
        job_config: The shop-level config (``None`` = code fallbacks).
    """

    text_logger: Any
    capture: ReportCapture
    timings: StageTimings
    started_at: str
    clock: float
    job_config: Any


def _bootstrap_run(
    args: argparse.Namespace,
) -> _RunBootstrap:
    """Set up the logging, capture, timing and config services of one run.

    Shared by :func:`_run_generate` (single job) and
    :func:`_run_generate_jobset` (job set): both need the identical dual
    logging topology, the report's log capture, the stage-timing collector
    and the job config, and the two paths must never drift apart.

    Args:
        args: Parsed command-line arguments namespace.

    Returns:
        The run's services.

    Raises:
        PermissionError: If the log directory cannot be created.
        Exception: If ``job-config.json`` cannot be parsed (the caller's
            "Error parsing specification" contract).
    """
    log_dir = Path("./logs_generate")
    log_dir.mkdir(parents=True, exist_ok=True)

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
    timings = StageTimings()

    # ``load_job_config`` reads ``job-config.json`` relative to the current
    # working directory when the path is relative (and uses the code
    # fallback when it is missing) - always resolve it to an absolute path
    # first so the file is found no matter where the command is invoked
    # from (Windows Task Scheduler starts watch-daemon jobs at System32).
    job_config = load_job_config(args.job_config)

    return _RunBootstrap(
        text_logger=text_logger,
        capture=capture,
        timings=timings,
        started_at=utc_now_iso(),
        clock=time.perf_counter(),
        job_config=job_config,
    )


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

    Dispatches on the input: a positional spec runs one job, ``--jobset``
    runs a set of jobs as one packing/export/report unit.

    Args:
        args: Parsed command-line arguments namespace.

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    if getattr(args, "jobset", None) is not None:
        return _run_generate_jobset(args)

    spec_path = args.spec

    if spec_path is None:
        print(
            "Error: Missing the job specification path (or use --jobset).\n"
            "Usage: plt-optimizer generate <job.yaml>\n"
            "   or: plt-optimizer generate --jobset <jobset.yaml>",
            file=sys.stderr,
        )
        return 1

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
    except Exception as e:
        print(f"Error parsing specification: {e}", file=sys.stderr)
        return 1

    inventory, boundary_hole_cutter = _load_cutter_inventory(args.tools)
    tools = {
        "available_cutters": list(inventory) if inventory else [],
        "boundary_hole_cutter_size": boundary_hole_cutter,
        # Kerf buffers (inches) the job-config added to each cutter to get the
        # width it engraves; the per-line/per-label resolved values (which the
        # spec can override) are reported on the labels themselves. `None`
        # when no config is in play (the code fallback is then 0.0).
        "cutter_buffer_text": (
            job_config.defaults.cutter_buffer_text if job_config is not None else None
        ),
        "cutter_buffer_routing": (
            job_config.defaults.cutter_buffer_routing if job_config is not None else None
        ),
    }

    def _handle_prep_failure(e: JobPreparationError) -> int:
        """Reproduce the CLI's per-stage failure contract for a prep error.

        Args:
            e: The preparation error (carries the stage, the underlying
                exception and the console line for that shape).

        Returns:
            The process exit code (1).
        """
        if not e.console_message:
            # CutterSizeError and friends from the resolve stage: the
            # generic "Generation FAILED" path, as before.
            return _fail(e.cause, e.stage)
        capture.close()
        print(e.console_message, file=sys.stderr)
        _write_failure_report(
            report_dir,
            job_name=job_name,
            job_id=job_id,
            spec_path=spec_path,
            output_dir=output_dir,
            error=e.cause,
            stage=e.stage,
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

    try:
        prepared = prepare_job(
            spec_path,
            job_config=job_config,
            job_config_path=args.job_config,
            inventory=inventory,
            boundary_hole_cutter=boundary_hole_cutter,
            stage_timings=stage_timings,
        )
    except JobPreparationError as e:
        return _handle_prep_failure(e)

    job = prepared.job
    job_name = prepared.job_name
    job_id = prepared.job_id
    plate_count = prepared.plate_count

    from plt_optimizer.generate.collision.detection import LabelRenderError
    from plt_optimizer.generate.packing.layout import LayoutFitError
    from plt_optimizer.generate.resolution import CutterSizeError
    from plt_optimizer.generate.usage_stats import build_usage_stats
    from plt_optimizer.generate.vectorize import export_per_cutter_plts

    default_plate_size = _default_plate_size(job_config)
    try:
        with stage_timer("export", _LOG, timings=stage_timings):
            export_result = export_per_cutter_plts(
                prepared.resolved_labels,
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

    resolved_labels = prepared.resolved_labels

    # Production usage accounting (labels / characters / holes / material /
    # tool distance / run time). The distance and time blocks are measured
    # from the PLT files the export stage just wrote, so it runs after the
    # export and outside it: a measurement failure must never fail a job that
    # engraved fine, it just leaves the ``usage`` block out of the report.
    # ``job`` + ``job_config`` are passed so the time estimate resolves the
    # same per-layer tool_options the headers of those files were emitted
    # from.
    try:
        with stage_timer("usage", _LOG, timings=stage_timings):
            usage = build_usage_stats(
                resolved_labels=resolved_labels,
                packed_plates=export_result.packed_plates,
                combined_by_plate=export_result.combined_by_plate,
                plt_files=export_result.plt_paths,
                material_by_plate=export_result.material_by_plate,
                job_spec=job,
                job_config=job_config,
            )
    except Exception as exc:  # noqa: BLE001 - reporting must not fail a good job
        _LOG.warning("Could not measure the job's production usage: %s", exc)
        usage = None

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
        usage=usage,
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


def _run_generate_jobset(args: argparse.Namespace) -> int:
    """Execute the generate command in jobset mode (``--jobset``).

    A set of job specs is prepared with the single-job preparation path,
    merged into one namespaced pool, packed and exported as one run (shared
    per-cutter files per plate), and reported three ways: one report per job
    under ``<output>/<job_id>/generation_report/``, the batch manifest at
    ``<output>/generation_report/jobset.json`` and the run's own
    ``report.json`` carrying the ``jobset`` block.

    Args:
        args: Parsed command-line arguments namespace (``args.jobset`` is
            the jobset YAML path).

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    from plt_optimizer.generate.collision.detection import LabelRenderError
    from plt_optimizer.generate.jobset import (
        JobSetError,
        build_jobset_plan,
        jobset_id_of,
        parse_jobset_yaml,
    )
    from plt_optimizer.generate.jobset_report import (
        build_jobset_block,
        write_job_reports,
        write_manifest,
    )
    from plt_optimizer.generate.packing.layout import LayoutFitError
    from plt_optimizer.generate.resolution import CutterSizeError
    from plt_optimizer.generate.usage_stats import build_usage_stats
    from plt_optimizer.generate.vectorize import export_per_cutter_plts

    jobset_path: Path = args.jobset
    if args.spec is not None:
        print(
            "Error: --jobset and the positional job spec are mutually exclusive.\n"
            "Usage: plt-optimizer generate <job.yaml>\n"
            "   or: plt-optimizer generate --jobset <jobset.yaml>",
            file=sys.stderr,
        )
        return 1
    if not jobset_path.exists() or not jobset_path.is_file():
        print(f"Error: Jobset file does not exist: {jobset_path}", file=sys.stderr)
        return 1

    output_dir = args.output if args.output is not None else jobset_path.parent
    report_dir = report_dir_for(output_dir)
    job_name = jobset_path.stem
    job_id = _sanitize_job_id(job_name)
    tools: Optional[Dict[str, Any]] = None
    options: Dict[str, Any] = {
        "no_plots": bool(args.no_plots),
        "default_plots": bool(args.default_plots),
        "fast_mode": bool(args.fast_mode),
        "verbose": bool(args.verbose),
        "jobset": str(jobset_path),
    }

    try:
        bootstrap = _bootstrap_run(args)
    except PermissionError as e:
        print(f"Error: Cannot create the log directory: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error parsing specification: {e}", file=sys.stderr)
        return 1

    text_logger = bootstrap.text_logger
    capture = bootstrap.capture
    stage_timings = bootstrap.timings
    job_config = bootstrap.job_config
    started_at = bootstrap.started_at

    def _fail(error: BaseException, stage: str) -> int:
        """Log the failure, write the batch failure report, return the code."""
        text_logger.error(f"Jobset generation failed: {error}")
        capture.close()
        _write_failure_report(
            report_dir,
            job_name=job_name,
            job_id=job_id,
            spec_path=jobset_path,
            output_dir=output_dir,
            error=error,
            stage=stage,
            started_at=started_at,
            finished_at=utc_now_iso(),
            duration_seconds=time.perf_counter() - bootstrap.clock,
            tools=tools,
            options=options,
            plate_count=0,
            records=capture.records,
            stage_records=capture.stage_records,
            timings=stage_timings,
        )
        print(
            f"Jobset generation FAILED ({type(error).__name__}): {error} "
            f"(report: {report_dir / REPORT_JSON_NAME})",
            file=sys.stderr,
        )
        return 1

    with stage_timer("parse", _LOG, timings=stage_timings):
        try:
            set_spec = parse_jobset_yaml(jobset_path)
            job_name = set_spec.name or jobset_path.stem
        except (JobSetError, ValueError, OSError) as e:
            print(f"Error parsing jobset specification: {e}", file=sys.stderr)
            capture.close()
            return 1
        # The batch id is known from the spec, so the failure reports written
        # by the stages below carry it (and a gate/packing failure still
        # leaves a report.json to explain it).
        job_id = jobset_id_of(set_spec, jobset_path)

    inventory, boundary_hole_cutter = _load_cutter_inventory(args.tools)
    tools = {
        "available_cutters": list(inventory) if inventory else [],
        "boundary_hole_cutter_size": boundary_hole_cutter,
        "cutter_buffer_text": (
            job_config.defaults.cutter_buffer_text if job_config is not None else None
        ),
        "cutter_buffer_routing": (
            job_config.defaults.cutter_buffer_routing if job_config is not None else None
        ),
    }

    with stage_timer("resolve", _LOG, timings=stage_timings):
        try:
            plan = build_jobset_plan(
                jobset_path,
                job_config=job_config,
                job_config_path=args.job_config,
                inventory=inventory,
                boundary_hole_cutter=boundary_hole_cutter,
                stage_timings=stage_timings,
            )
        except (JobSetError, JobPreparationError, ValueError, OSError) as e:
            return _fail(e, "resolve")

    job_name = plan.jobset_name
    job_id = plan.jobset_id
    for warning in plan.warnings:
        _LOG.warning("Jobset gate: %s", warning)

    # The batch-level JobSpec the export reads its pool-wide settings from:
    # the first member's spec (fonts, clearances, tool rates) carrying the
    # SET's identity, plate list and packing pool. Members' own specs reach
    # the per-file ``tool_options`` headers through ``layer_specs``, so the
    # per-segment cascade stays per job.
    batch_spec = plan.contexts[0].spec.model_copy(
        update={
            "job_name": plan.jobset_name,
            "plates": plan.merged_plates,
            "labels": plan.merged_labels,
        }
    )

    try:
        with stage_timer("export", _LOG, timings=stage_timings):
            export_result = export_per_cutter_plts(
                plan.merged_labels,
                plan.merged_plates,
                output_dir=output_dir,
                job_id=job_id,
                job_name=job_id,
                optimize=True,
                plots=not args.no_plots,
                default_plots=args.default_plots,
                allow_rotation=plan.allow_rotation,
                fast_mode=args.fast_mode,
                logger=text_logger,
                layout=plan.layout,
                default_plate_size=_default_plate_size(job_config),
                default_plate_clearance=plan.default_plate_clearance,
                job_spec=batch_spec,
                job_config=job_config,
                available_cutters=inventory,
                stage_timings=stage_timings,
                layer_specs={c.job_key: c.spec for c in plan.contexts},
                provenance=plan.provenance,
            )
    except LabelRenderError as e:
        return _fail(e, "render")
    except LayoutFitError as e:
        return _fail(e, "layout")
    except CutterSizeError as e:
        return _fail(e, "resolve")
    except (OSError, ValueError) as e:
        return _fail(e, "export")

    # Production usage accounting of the whole batch, attributed per job:
    # every stroke a shared file carries is charged to the member whose
    # placement contains it (see usage_stats.build_usage_stats' job_of_label).
    try:
        with stage_timer("usage", _LOG, timings=stage_timings):
            from plt_optimizer.generate.jobset_report import (
                job_of_placement,
                sheet_first_owners,
            )

            usage = build_usage_stats(
                resolved_labels=plan.merged_labels,
                packed_plates=export_result.packed_plates,
                combined_by_plate=export_result.combined_by_plate,
                plt_files=export_result.plt_paths,
                material_by_plate=export_result.material_by_plate,
                job_spec=batch_spec,
                job_config=job_config,
                job_of_label=job_of_placement(export_result, plan),
                plate_owner_of=sheet_first_owners(export_result),
                file_jobs=dict(export_result.file_jobs),
            )
    except Exception as exc:  # noqa: BLE001 - reporting must not fail a good job
        _LOG.warning("Could not measure the jobset's production usage: %s", exc)
        usage = None

    finished_at = utc_now_iso()
    duration = time.perf_counter() - bootstrap.clock

    reports = write_job_reports(
        plan,
        export_result,
        output_dir=output_dir,
        job_config=job_config,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration,
        tools=tools,
        options=options,
        timings=stage_timings,
    )
    jobset_block = build_jobset_block(
        plan, export_result, reports, output_dir=output_dir, root_usage=usage
    )
    write_manifest(plan, export_result, reports, output_dir=output_dir, root_usage=usage)

    report = build_report(
        job_name=job_name,
        job_id=job_id,
        spec_path=jobset_path,
        output_dir=output_dir,
        report_dir=report_dir,
        status="success",
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration,
        tools=tools,
        options=options,
        plate_count=len(export_result.packed_plates),
        resolved_labels=plan.merged_labels,
        rendered_by_id=export_result.rendered_labels,
        packed_plates=export_result.packed_plates,
        plt_files=export_result.plt_paths,
        pdf_files=export_result.pdf_paths,
        default_pdf_files=export_result.default_pdf_paths,
        records=capture.records,
        stage_records=capture.stage_records,
        timings=stage_timings,
        usage=usage,
        jobset=jobset_block,
    )
    capture.close()
    try:
        write_report(report_dir, report)
    except OSError as exc:  # pragma: no cover - defensive
        text_logger.error(f"Could not write the jobset report to {report_dir}: {exc}")

    label_count = sum(len(c.resolved_labels) for c in plan.contexts)
    print(
        f"Generated jobset '{job_name}': {len(plan.contexts)} job(s), "
        f"{label_count} label(s), {len(export_result.plt_paths)} PLT file(s), "
        f"{len(export_result.pdf_paths)} PDF(s) in {output_dir} "
        f"(reports: {reports.manifest_path.parent})"
    )
    return 0
