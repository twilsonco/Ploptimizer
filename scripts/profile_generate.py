#!/usr/bin/env python3
"""Generate-pipeline profiling harness (CPU, memory, wall-clock, stage timers).

Profiles the production ``generate`` sequence (the exact stage order of
``plt_optimizer.cli.generate.run``: parse -> expand -> resolve -> export)
over a benchmark set of real job specs, using ONLY the standard library:

- **CPU**: ``cProfile`` -> binary ``<spec>.prof`` (inspect with
  ``python -m pstats <file>`` or ``uv run --with snakeviz snakeviz <file>``)
  plus a top-N cumulative-time printout per spec.
- **Memory**: ``tracemalloc`` -> peak/current traced bytes + top file:line
  allocation sites -> ``<spec>.mem.txt``.
- **Wall**: median of ``--repeats`` runs per spec (timers only; profiling
  runs once).
- **Stages**: the pipeline's ``stage <name>: <s>s`` INFO lines (see
  ``plt_optimizer/generate/stage_timing.py``) are captured per run and
  summed per stage name into the summary CSV.

Benchmark set (module-level ``BENCHMARK_SPECS``): five specs chosen to
cover the pipeline's distinct hot paths -- rounded-rect boundaries +
replacement text, line-content reflow + markup + shared compression,
round/ellipse shapely packing, the TTF/matplotlib TextPath renderer, and
multi-material packing.

``--ab`` mode answers the ``python -O`` question: for each spec x each
``PYTHONOPTIMIZE`` level (0, 1) x N repeats, it spawns a child run of this
script, records the median wall-clock, and sha256-manifests the produced
PLTs to prove byte-identical output across levels (determinism invariant).

Usage:
    uv run python scripts/profile_generate.py                  # full sweep
    uv run python scripts/profile_generate.py --specs a.yaml   # custom specs
    uv run python scripts/profile_generate.py --ab --repeats 3 # -O A/B

For sampling-based profiling of a single run (flamegraphs, C-level
visibility), this script composes with external samplers, e.g.:
    uv run --with py-spy py-spy record -o profile.svg -- \\
        python scripts/profile_generate.py --specs "<spec>" --no-profile
    uv run --with memray memray run scripts/profile_generate.py --no-profile
"""

from __future__ import annotations

import argparse
import cProfile
import csv
import hashlib
import json
import logging
import os
import pstats
import statistics
import subprocess
import sys
import time
import tracemalloc
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# Repo root (one level above scripts/) so the pipeline imports and every
# repo-root-relative default (tools.json, job-config.json, test_output/)
# work regardless of the current working directory.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # allow running as a plain script
    sys.path.insert(0, str(REPO_ROOT))

from plt_optimizer.generate.stage_timing import STAGE_LINE_PATTERN  # noqa: E402

# ---------------------------------------------------------------------------
# Benchmark set: the five specs covering the pipeline's feature surface.
# ---------------------------------------------------------------------------
BENCHMARK_SPECS: tuple[str, ...] = (
    "examples/job_specs/2026-07-22 sunwest.yaml",
    "examples/job_specs/2026-10-07 coolsys 1 big label.yaml",
    "examples/job_specs/2026-10-15 coolsys round tags target 910 and walmart 2206.yaml",
    "examples/job_specs/2026-10-06 seer intrachunk opt TTF.yaml",
    "examples/job_specs/2026-10-06 seer wb-uv.yaml",
)

DEFAULT_OUTPUT_DIR = "test_output/profiling"
TOOLS_JSON = "tools.json"
JOB_CONFIG_JSON = "job-config.json"


class _StageCapture(logging.Handler):
    """Collect ``stage <name>: <s>s`` records emitted by the pipeline.

    Installed on the ``plt_optimizer`` logger at INFO so the timing lines
    are captured without printing the pipeline's operational chatter to
    the profiling console.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.stages: Dict[str, float] = {}
        self.order: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Record a matching timing line (summing repeated stage names)."""
        if record.levelno != logging.INFO:
            return
        match = STAGE_LINE_PATTERN.match(record.getMessage())
        if match is None:
            return
        name = match.group("name")
        seconds = float(match.group("seconds"))
        if name not in self.stages:
            self.order.append(name)
        self.stages[name] = self.stages.get(name, 0.0) + seconds


def run_pipeline(
    spec: Path,
    output_dir: Path,
    *,
    optimize: bool = True,
    fast_mode: bool = False,
    plots: bool = False,
) -> List[Path]:
    """Execute the production generate sequence for one spec.

    Mirrors ``plt_optimizer.cli.generate.run`` (load config -> parse ->
    expand -> resolve -> export_per_cutter_plts) minus the CLI's logging
    topology and report printing.

    Args:
        spec: Path to the job spec YAML.
        output_dir: Per-spec output directory (``plt/`` + ``pdf/`` land
            inside it).
        optimize: Enable plate toolpath optimization (the production
            default; ``--no-optimize`` isolates render/pack cost).
        fast_mode: NN2Opt routing instead of the parallel ensemble.
        plots: Write PDF previews (matplotlib; off by default for
            profiling).

    Returns:
        The written PLT paths.
    """
    from plt_optimizer.cli.generate import _default_plate_clearance, _sanitize_job_id
    from plt_optimizer.generate.job_config import load_job_config
    from plt_optimizer.generate.label_renderer import measure_line_vertical_extents
    from plt_optimizer.generate.resolution import memoize_extents_probe, resolve_job_spec
    from plt_optimizer.generate.schema import parse_yaml
    from plt_optimizer.generate.substitution import expand_job_spec
    from plt_optimizer.generate.vectorize import export_per_cutter_plts

    job_config_path = REPO_ROOT / JOB_CONFIG_JSON
    job_config = load_job_config(job_config_path)
    job = expand_job_spec(
        parse_yaml(spec, job_config_path=job_config_path),
        spec,
    )
    job_id = _sanitize_job_id(job.job_name)

    with open(REPO_ROOT / TOOLS_JSON) as f:
        tools_data = json.load(f)
    inventory = tools_data.get("available_cutters", [])
    boundary_hole_cutter = tools_data.get("boundary_hole_cutter_size")

    resolved_labels = resolve_job_spec(
        job,
        available_cutters=inventory,
        boundary_hole_cutter_size=boundary_hole_cutter,
        extents_probe=memoize_extents_probe(measure_line_vertical_extents),
    )
    export = export_per_cutter_plts(
        resolved_labels,
        job.plates,
        output_dir=output_dir,
        job_id=job_id,
        job_name=job.job_name,
        optimize=optimize,
        plots=plots,
        fast_mode=fast_mode,
        layout=job.layout,
        default_plate_clearance=_default_plate_clearance(job),
        job_spec=job,
        job_config=job_config,
        available_cutters=inventory,
    )
    return list(export.plt_paths)


def _install_logging(spec_log: Path, capture: _StageCapture) -> None:
    """Quiet console, full detail to ``spec_log``, stage capture on top.

    The core optimizer's lazy ``get_text_logger()`` singleton sets the
    ``plt_optimizer`` logger's LEVEL from ``PLT_LOG_LEVEL`` on first use
    (mid-run), which would gate out the INFO stage lines. So the singleton
    is pre-created here at WARNING (keeping its spawned ensemble children
    quiet via the env var), its stdout chatter is demoted to ERROR, and the
    logger level is then pinned to INFO for this process.
    """
    from plt_optimizer.utils.logging import setup_logging

    spec_log.parent.mkdir(parents=True, exist_ok=True)
    # Force the singleton into existence BEFORE touching handlers (its
    # constructor attaches a stdout + logs/optimizer.log handler pair).
    setup_logging(level="warning")

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(logging.FileHandler(spec_log, mode="w", encoding="utf-8"))
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.ERROR)
    root.addHandler(console)

    pipeline_logger = logging.getLogger("plt_optimizer")
    for handler in list(pipeline_logger.handlers):
        pipeline_logger.removeHandler(handler)
    pipeline_logger.addHandler(capture)
    pipeline_logger.setLevel(logging.INFO)


def profile_one_spec(
    spec: Path,
    out_root: Path,
    args: argparse.Namespace,
) -> Dict[str, Any]:
    """Profile a single spec: wall repeats + one profiled/memory-traced run.

    Args:
        spec: Job spec YAML path.
        out_root: Root output directory for this sweep.
        args: Parsed CLI arguments.

    Returns:
        Summary row: spec name, wall medians, cpu seconds, peak memory,
        and summed stage seconds.
    """
    stem = spec.stem
    spec_out = out_root / stem
    spec_out.mkdir(parents=True, exist_ok=True)
    capture = _StageCapture()
    _install_logging(spec_out / "run.log", capture)

    def _once() -> List[Path]:
        return run_pipeline(
            spec,
            spec_out,
            optimize=not args.no_optimize,
            fast_mode=args.fast_mode,
            plots=args.plots,
        )

    # --- Wall-clock repeats (median; stage capture from the LAST run).
    walls: List[float] = []
    cpu_seconds: List[float] = []
    for _ in range(max(1, args.repeats)):
        capture.stages.clear()
        capture.order.clear()
        t0 = time.perf_counter()
        c0 = time.process_time()
        _once()
        walls.append(time.perf_counter() - t0)
        cpu_seconds.append(time.process_time() - c0)
    stages = dict(capture.stages)
    stage_order = list(capture.order)

    row: Dict[str, Any] = {
        "run_ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "spec": stem,
        "wall_s": statistics.median(walls),
        "wall_min_s": min(walls),
        "wall_max_s": max(walls),
        "cpu_s": statistics.median(cpu_seconds),
    }

    # --- CPU profile (one extra run, cProfile).
    if args.profile:
        profiler = cProfile.Profile()
        profiler.runcall(_once)
        prof_path = spec_out / f"{stem}.prof"
        profiler.dump_stats(prof_path)
        stream = StringIO()
        stats = pstats.Stats(profiler, stream=stream)
        stats.sort_stats("cumulative")
        stats.print_stats(args.top)
        (spec_out / f"{stem}.top.txt").write_text(stream.getvalue(), encoding="utf-8")
        print(f"\n--- top {args.top} cumulative functions: {stem} (full: {prof_path}) ---")
        print(stream.getvalue())

    # --- Memory trace (one extra run, tracemalloc).
    if args.memory:
        tracemalloc.start()
        try:
            _once()
            current, peak = tracemalloc.get_traced_memory()
        finally:
            snapshot = tracemalloc.take_snapshot()
            tracemalloc.stop()
        lines = [
            f"traced current: {current / 1024 / 1024:.1f} MiB",
            f"traced peak:    {peak / 1024 / 1024:.1f} MiB",
            "",
            "top allocation sites:",
        ]
        for stat in snapshot.statistics("lineno")[: args.top]:
            lines.append(f"  {stat}")
        (spec_out / f"{stem}.mem.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n--- memory: {stem}: peak {peak / 1024 / 1024:.1f} MiB "
              f"(report: {spec_out / (stem + '.mem.txt')}) ---")
        row["peak_mem_mib"] = round(peak / 1024 / 1024, 1)

    for name in stage_order:
        row[f"stage_{name}_s"] = round(stages[name], 3)
    return row


# ---------------------------------------------------------------------------
# -O A/B harness
# ---------------------------------------------------------------------------
def _plt_manifest(output_dir: Path) -> Dict[str, str]:
    """sha256 of every produced PLT, keyed by path relative to *output_dir*."""
    manifest: Dict[str, str] = {}
    for path in sorted(output_dir.rglob("*.plt")):
        manifest[str(path.relative_to(output_dir))] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return manifest


def _child_run(
    spec: Path,
    out_dir: Path,
    python_optimize: int,
    args: argparse.Namespace,
) -> float:
    """Spawn one child pipeline run at a given PYTHONOPTIMIZE level.

    Returns:
        Child wall-clock seconds (full subprocess lifetime, interpreter
        startup included -- identical overhead on both sides).
    """
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--specs",
        str(spec),
        "--output-dir",
        str(out_dir),
        "--no-profile",
        "--no-memory",
        "--repeats",
        "1",
    ]
    if args.no_optimize:
        command.append("--no-optimize")
    if args.fast_mode:
        command.append("--fast-mode")
    if args.plots:
        command.append("--plots")
    env = dict(os.environ, PYTHONOPTIMIZE=str(python_optimize))
    start = time.perf_counter()
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.perf_counter() - start
    if result.returncode != 0:
        raise RuntimeError(
            f"child run failed (PYTHONOPTIMIZE={python_optimize}, rc={result.returncode}):\n"
            f"{result.stderr[-4000:]}"
        )
    return elapsed


def ab_mode(args: argparse.Namespace, specs: Sequence[Path], out_root: Path) -> int:
    """Run the -O A/B sweep: median wall per (spec, level) + output hashes.

    Args:
        args: Parsed CLI arguments (``repeats`` = runs per cell).
        specs: Specs to measure.
        out_root: Root output directory.

    Returns:
        Process exit code (0 = all cells ran and outputs matched).
    """
    repeats = max(1, args.repeats)
    levels = (0, 1)
    rows: List[Dict[str, Any]] = []
    failures = 0
    for spec in specs:
        medians: Dict[int, float] = {}
        manifests: Dict[int, Dict[str, str]] = {}
        for level in levels:
            cell_dir = out_root / "ab" / spec.stem / f"O{level}"
            walls: List[float] = []
            for _ in range(repeats):
                walls.append(_child_run(spec, cell_dir, level, args))
            medians[level] = statistics.median(walls)
            manifests[level] = _plt_manifest(cell_dir)
        identical = manifests[0] == manifests[1]
        if not identical:
            failures += 1
        delta = medians[1] - medians[0]
        pct = (delta / medians[0] * 100.0) if medians[0] > 0 else 0.0
        rows.append(
            {
                "spec": spec.stem,
                "O0_median_s": round(medians[0], 3),
                "O1_median_s": round(medians[1], 3),
                "delta_s": round(delta, 3),
                "delta_pct": round(pct, 2),
                "plt_bytes_identical": identical,
                "plt_files": len(manifests[0]),
            }
        )
        print(
            f"[ab] {spec.stem}: -O0 {medians[0]:.2f}s | -O1 {medians[1]:.2f}s "
            f"({pct:+.1f}%) | PLT outputs {'IDENTICAL' if identical else 'MISMATCH'}"
        )
    csv_path = out_root / "ab_optimize.csv"
    _write_csv(csv_path, rows)
    print(f"\nA/B summary written to {csv_path}")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# Summary CSV + CLI
# ---------------------------------------------------------------------------
def _write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    """Write the summary rows (union of keys, stable order) to *path*."""
    fieldnames: List[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _append_csv(
    path: Path, rows: Sequence[Dict[str, Any]], *, fresh: bool = False
) -> List[Dict[str, str]]:
    """Append *rows* to the CSV at *path*, preserving any existing history.

    The summary CSV is a run HISTORY: every sweep appends its rows (each
    stamped with ``run_ts``), so wall-time trends across optimization work
    are diffable and comparable without digging through git. Column order
    is the existing file's, with any new keys appended; rows missing a key
    write an empty cell. ``fresh=True`` discards the existing file.

    Args:
        path: Target CSV.
        rows: Rows from this sweep.
        fresh: Overwrite instead of appending.

    Returns:
        The existing rows that were preserved (empty when ``fresh`` or no
        file existed) — for the console summary.
    """
    existing: List[Dict[str, str]] = []
    if path.exists() and not fresh:
        with open(path, newline="") as f:
            existing = list(csv.DictReader(f))
    fieldnames: List[str] = []
    for row in (*existing, *rows):
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, restval="")
        writer.writeheader()
        writer.writerows(existing)
        writer.writerows(rows)
    return existing


def _collect_specs(values: Sequence[str]) -> List[Path]:
    """Resolve spec paths (repo-relative allowed); directories expand to yaml."""
    specs: List[Path] = []
    for value in values:
        path = Path(value)
        if not path.is_absolute():
            path = REPO_ROOT / path
        if path.is_dir():
            specs.extend(sorted(p for p in path.iterdir() if p.suffix.lower() in (".yaml", ".yml")))
        else:
            specs.append(path)
    missing = [str(p) for p in specs if not p.is_file()]
    if missing:
        raise SystemExit(f"job spec(s) not found:\n  " + "\n  ".join(missing))
    return specs


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the profiling harness."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--specs",
        nargs="+",
        default=list(BENCHMARK_SPECS),
        help="Job spec YAML files (or directories) to profile.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(REPO_ROOT / DEFAULT_OUTPUT_DIR),
        help="Root output directory for profiles, memory reports, and CSVs.",
    )
    parser.add_argument(
        "--repeats", type=int, default=3, help="Timed runs per spec (median reported)."
    )
    parser.add_argument(
        "--top", type=int, default=25, help="Top-N functions/lines in CPU/memory reports."
    )
    parser.add_argument(
        "--no-profile",
        dest="profile",
        action="store_false",
        default=True,
        help="Skip the cProfile run (wall/stages only).",
    )
    parser.add_argument(
        "--no-memory",
        dest="memory",
        action="store_false",
        default=True,
        help="Skip the tracemalloc run.",
    )
    parser.add_argument(
        "--no-optimize",
        action="store_true",
        help="Disable plate toolpath optimization (isolates render/pack cost).",
    )
    parser.add_argument(
        "--fast-mode",
        action="store_true",
        help="NN2Opt routing instead of the parallel ensemble (isolates TSP cost).",
    )
    parser.add_argument(
        "--plots", action="store_true", help="Write PDF previews (matplotlib cost included)."
    )
    parser.add_argument(
        "--ab",
        action="store_true",
        help="A/B mode: median wall + byte-identical PLT manifest for "
        "PYTHONOPTIMIZE=0 vs 1 (spawns child runs of this script).",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Reset summary.csv instead of appending this sweep to its history.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point: sweep mode or -O A/B mode."""
    args = build_parser().parse_args(argv)
    specs = _collect_specs(args.specs)
    out_root = Path(args.output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    # The ensemble's spawned children resolve their log level from this
    # env var; keep their INFO chatter off the profiling console (full
    # detail still lands in each spec's run.log via the parent handlers).
    os.environ.setdefault("PLT_LOG_LEVEL", "warning")

    if args.ab:
        return ab_mode(args, specs, out_root)

    rows: List[Dict[str, Any]] = []
    for spec in specs:
        print(f"\n=== profiling {spec.relative_to(REPO_ROOT)} ===")
        rows.append(profile_one_spec(spec, out_root, args))

    csv_path = out_root / "summary.csv"
    preserved = _append_csv(csv_path, rows, fresh=args.fresh)

    stage_keys = [
        key for key in (rows[0] if rows else {}) if str(key).startswith("stage_")
    ]
    print("\n=== wall-clock summary (median of %d) ===" % max(1, args.repeats))
    header = f"{'spec':<48} {'wall_s':>8} {'cpu_s':>8}"
    print(header)
    for row in rows:
        print(f"{row['spec']:<48} {row['wall_s']:>8.2f} {row['cpu_s']:>8.2f}")
    if stage_keys:
        print("\n=== summed stage seconds (last timed run) ===")
        print(f"{'spec':<48}" + "".join(f"{k[6:-2]:>16}" for k in stage_keys))
        for row in rows:
            print(
                f"{row['spec']:<48}"
                + "".join(f"{row.get(k, 0.0):>16.2f}" for k in stage_keys)
            )
    print(
        f"\nSummary CSV: {csv_path} "
        f"(history: {len(preserved)} prior row(s) + {len(rows)} new)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
