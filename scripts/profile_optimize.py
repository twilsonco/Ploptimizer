#!/usr/bin/env python3
"""Optimize-pipeline profiling harness (CPU, memory, wall-clock, stage timers).

Profiles the production ``optimize`` sequence (the exact stage order of
``plt_optimizer.cli.optimize.run``: parse -> profile -> preprocess -> chunk
-> optimize+reassemble -> write) over a benchmark set of real PLT files,
using ONLY the standard library:

- **CPU**: ``cProfile`` -> binary ``<file>.prof`` (inspect with
  ``python -m pstats <file>`` or ``uv run --with snakeviz snakeviz <file>``)
  plus a top-N cumulative-time printout per file.
- **Memory**: ``tracemalloc`` -> peak/current traced bytes + top file:line
  allocation sites -> ``<file>.mem.txt``.
- **Wall**: median of ``--repeats`` runs per file (timers only; profiling
  runs once).
- **Stages**: the pipeline's ``stage <name>: <s>s`` INFO lines (see
  ``plt_optimizer/utils/stage_timing.py``) are captured per run and summed
  per stage name into the summary CSV. The core-pipeline stages
  (``optimize_tsp``, ``direction_sweep``, ``intra_sweep``, ``reassemble``,
  ``merge``) fire via ``optimize_and_reassemble(stage_timers=True)``.
- **KPIs**: rapid travel before/after (inches) and improvement % -- the
  optimization's own quality metric, recorded so a perf change can also be
  checked for quality drift.

Benchmark set (module-level ``BENCHMARK_PLTS``): two files chosen to cover
the pipeline's distinct hot paths -- a large text-dominant production
sheet (chronological chunking, contiguous glyph paths, ensemble TSP) and a
structural sheet (fracture + dedupe preprocessing, one-block-per-segment
chunking, drill holes + borders).

``--ab`` mode answers the ``python -O`` question: for each file x each
``PYTHONOPTIMIZE`` level (0, 1) x N repeats, it spawns a child run of this
script, records the median wall-clock, and sha256-manifests the produced
PLTs to prove byte-identical output across levels (determinism invariant).

Usage:
    uv run python scripts/profile_optimize.py                  # full sweep
    uv run python scripts/profile_optimize.py --plts a.plt     # custom files
    uv run python scripts/profile_optimize.py --ab --repeats 3 # -O A/B

For sampling-based profiling of a single run (flamegraphs, C-level
visibility), this script composes with external samplers, e.g.:
    uv run --with py-spy py-spy record -o profile.svg -- \\
        python scripts/profile_optimize.py --plts "<file>" --no-profile
    uv run --with memray memray run scripts/profile_optimize.py --no-profile
"""

from __future__ import annotations

import argparse
import cProfile
import csv
import hashlib
import logging
import os
import pstats
import statistics
import subprocess
import sys
import time
import tracemalloc
import uuid
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# Repo root (one level above scripts/) so the pipeline imports and every
# repo-root-relative default (test_output/, profiling/) work regardless of
# the current working directory.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # allow running as a plain script
    sys.path.insert(0, str(REPO_ROOT))

from plt_optimizer.core.units import units_to_inches  # noqa: E402
from plt_optimizer.utils.stage_timing import STAGE_LINE_PATTERN  # noqa: E402

# ---------------------------------------------------------------------------
# Benchmark set: the two PLTs covering the pipeline's feature surface.
# ---------------------------------------------------------------------------
BENCHMARK_PLTS: tuple[str, ...] = (
    # Text-dominant production sheet: chronological chunking of contiguous
    # glyph paths, large ensemble TSP layers.
    "examples/334X712 RED DANGER conveyers start and stop2.plt",
    # Structural sheet (borders + drill holes): fracture + dedupe
    # preprocessing and one-block-per-segment chunking.
    "tests_deps/1x3 half inch letters holes1.plt",
)

DEFAULT_OUTPUT_DIR = "test_output/optimize_profiling"
#: The run-history CSV lives in the TRACKED ``profiling/`` directory (git
#: commits the perf history; ``test_output/`` is ignored via ``*_output``,
#: so the per-file artifacts below stay local scratch). Adjacent to the
#: generate pipeline's ``summary.csv`` -- one history per pipeline.
DEFAULT_HISTORY_CSV = REPO_ROOT / "profiling" / "optimization_pipeline_summary.csv"


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
    plt_path: Path,
    output_dir: Path,
    *,
    fast_mode: bool = False,
) -> Dict[str, Any]:
    """Execute the production optimize sequence for one PLT file.

    Mirrors ``plt_optimizer.cli.optimize.run`` (parse -> profile ->
    preprocess -> chunk -> optimize+reassemble -> write) plus the KPI
    measurements the CLI logs, with ``stage_timers=True`` on the core
    pipeline helper.

    Args:
        plt_path: Input PLT path.
        output_dir: Directory the optimized PLT is written into.
        fast_mode: NN2Opt routing instead of the parallel ensemble.

    Returns:
        KPI dict: ``rapid_before_in``, ``rapid_after_in`` and
        ``improvement_pct`` (the emitted rapid travel, intra + inter, in
        inches -- what the plotter actually travels).
    """
    from plt_optimizer.core.chunker import Chunker
    from plt_optimizer.core.optimizer import (
        NearestNeighbor2OptStrategy,
        OptimizerEngine,
        ParallelEnsembleStrategy,
    )
    from plt_optimizer.core.parser import PLTParser
    from plt_optimizer.core.pipeline import (
        chunk_document,
        optimize_and_reassemble,
        preprocess_document,
    )
    from plt_optimizer.core.profiler import Profiler
    from plt_optimizer.core.reassembler import MetricsCalculator, Reassembler
    from plt_optimizer.core.stroke_simplifier import simplify_overlapping_strokes
    from plt_optimizer.core.writer import PLTWriter
    from plt_optimizer.utils.geometry import fracture_linear_paths
    from plt_optimizer.utils.logging import get_text_logger
    from plt_optimizer.utils.stage_timing import stage_timer

    text_logger = get_text_logger()

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{plt_path.stem}_optimized.plt"

    parser = PLTParser()
    with stage_timer("parse", text_logger.logger):
        doc = parser.parse_file(plt_path)

    profiler = Profiler()
    with stage_timer("profile", text_logger.logger):
        profile_result = profiler.profile(doc)

    metrics_calc = MetricsCalculator()
    # Emitted rapid travel (intra + inter) is the plotter's actual travel
    # and is defined on the parsed document before any preprocessing.
    rapid_before = doc.rapid_distance()

    with stage_timer("preprocess", text_logger.logger):
        doc = preprocess_document(
            doc,
            is_structural=profile_result.is_structural,
            fracture_factory=fracture_linear_paths,
            dedupe_factory=simplify_overlapping_strokes,
            logger=text_logger,
        )

    with stage_timer("chunk", text_logger.logger):
        blocks = chunk_document(doc, profile_result, chunker_factory=Chunker)
    if not blocks:
        raise RuntimeError(f"no blocks generated from {plt_path}")

    original_distance = metrics_calc.calculate_original_travel_distance(doc)
    if fast_mode:
        strategy: Any = NearestNeighbor2OptStrategy()
    else:
        strategy = ParallelEnsembleStrategy(baseline_distance=original_distance)

    with stage_timer("optimize", text_logger.logger):
        outcome = optimize_and_reassemble(
            doc,
            blocks,
            strategy,
            engine_factory=OptimizerEngine,
            reassembler_factory=Reassembler,
            logger=text_logger,
            stage_timers=True,
        )

    writer = PLTWriter()
    with stage_timer("write", text_logger.logger):
        writer.write_file(outcome.optimized_doc, output_path)

    rapid_after = outcome.optimized_doc.rapid_distance()
    improvement_pct = (
        (rapid_before - rapid_after) / rapid_before * 100.0 if rapid_before > 0 else 0.0
    )
    return {
        "rapid_before_in": round(units_to_inches(rapid_before), 3),
        "rapid_after_in": round(units_to_inches(rapid_after), 3),
        "improvement_pct": round(improvement_pct, 2),
    }


def _install_logging(file_log: Path, capture: _StageCapture) -> None:
    """Quiet console, full detail to ``file_log``, stage capture on top.

    The core optimizer's lazy ``get_text_logger()`` singleton sets the
    ``plt_optimizer`` logger's LEVEL from ``PLT_LOG_LEVEL`` on first use
    (mid-run), which would gate out the INFO stage lines. So the singleton
    is pre-created here at WARNING (keeping its spawned ensemble children
    quiet via the env var), its stdout chatter is demoted to ERROR, and the
    logger level is then pinned to INFO for this process.
    """
    from plt_optimizer.utils.logging import setup_logging

    file_log.parent.mkdir(parents=True, exist_ok=True)
    # Force the singleton into existence BEFORE touching handlers (its
    # constructor attaches a stdout + logs/optimizer.log handler pair).
    setup_logging(level="warning", text_log_file=file_log)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(logging.FileHandler(file_log, mode="w", encoding="utf-8"))
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.ERROR)
    root.addHandler(console)

    pipeline_logger = logging.getLogger("plt_optimizer")
    for handler in list(pipeline_logger.handlers):
        pipeline_logger.removeHandler(handler)
    pipeline_logger.addHandler(capture)
    pipeline_logger.setLevel(logging.INFO)


def profile_one_file(
    plt_path: Path,
    out_root: Path,
    args: argparse.Namespace,
    run_id: str = "",
) -> Dict[str, Any]:
    """Profile a single PLT: wall repeats + one profiled/memory-traced run.

    Args:
        plt_path: Input PLT path.
        out_root: Root output directory for this sweep.
        args: Parsed CLI arguments.

    Returns:
        Summary row: file name, wall medians, cpu seconds, peak memory,
        KPI columns, and summed stage seconds.
    """
    stem = plt_path.stem
    spec_out = out_root / stem
    spec_out.mkdir(parents=True, exist_ok=True)
    capture = _StageCapture()
    _install_logging(spec_out / "run.log", capture)

    def _once() -> Dict[str, Any]:
        return run_pipeline(plt_path, spec_out, fast_mode=args.fast_mode)

    # --- Wall-clock repeats (median; stage capture from the LAST run).
    walls: List[float] = []
    cpu_seconds: List[float] = []
    kpis: Dict[str, Any] = {}
    for _ in range(max(1, args.repeats)):
        capture.stages.clear()
        capture.order.clear()
        t0 = time.perf_counter()
        c0 = time.process_time()
        kpis = _once()
        walls.append(time.perf_counter() - t0)
        cpu_seconds.append(time.process_time() - c0)
    stages = dict(capture.stages)
    stage_order = list(capture.order)

    row: Dict[str, Any] = {
        "run_id": run_id,
        "run_ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "spec": stem,
        "wall_s": statistics.median(walls),
        "wall_min_s": min(walls),
        "wall_max_s": max(walls),
        "cpu_s": statistics.median(cpu_seconds),
        **kpis,
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
        print(
            f"\n--- memory: {stem}: peak {peak / 1024 / 1024:.1f} MiB "
            f"(report: {spec_out / (stem + '.mem.txt')}) ---"
        )
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
        if path.name.endswith("_unprocessed.plt"):
            continue
        manifest[str(path.relative_to(output_dir))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


def _child_run(
    plt_path: Path,
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
        "--plts",
        str(plt_path),
        "--output-dir",
        str(out_dir),
        "--no-profile",
        "--no-memory",
        "--repeats",
        "1",
    ]
    if args.fast_mode:
        command.append("--fast-mode")
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


def ab_mode(args: argparse.Namespace, plts: Sequence[Path], out_root: Path) -> int:
    """Run the -O A/B sweep: median wall per (file, level) + output hashes.

    Args:
        args: Parsed CLI arguments (``repeats`` = runs per cell).
        plts: PLT files to measure.
        out_root: Root output directory.

    Returns:
        Process exit code (0 = all cells ran and outputs matched).
    """
    repeats = max(1, args.repeats)
    levels = (0, 1)
    rows: List[Dict[str, Any]] = []
    failures = 0
    for plt_path in plts:
        medians: Dict[int, float] = {}
        manifests: Dict[int, Dict[str, str]] = {}
        for level in levels:
            cell_dir = out_root / "ab" / plt_path.stem / f"O{level}"
            walls: List[float] = []
            for _ in range(repeats):
                walls.append(_child_run(plt_path, cell_dir, level, args))
            medians[level] = statistics.median(walls)
            manifests[level] = _plt_manifest(cell_dir)
        identical = manifests[0] == manifests[1]
        if not identical:
            failures += 1
        delta = medians[1] - medians[0]
        pct = (delta / medians[0] * 100.0) if medians[0] > 0 else 0.0
        rows.append(
            {
                "spec": plt_path.stem,
                "O0_median_s": round(medians[0], 3),
                "O1_median_s": round(medians[1], 3),
                "delta_s": round(delta, 3),
                "delta_pct": round(pct, 2),
                "plt_bytes_identical": identical,
                "plt_files": len(manifests[0]),
            }
        )
        print(
            f"[ab] {plt_path.stem}: -O0 {medians[0]:.2f}s | -O1 {medians[1]:.2f}s "
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


def _collect_plts(values: Sequence[str]) -> List[Path]:
    """Resolve PLT paths (repo-relative allowed); directories expand to *.plt."""
    plts: List[Path] = []
    for value in values:
        path = Path(value)
        if not path.is_absolute():
            path = REPO_ROOT / path
        if path.is_dir():
            plts.extend(sorted(path.glob("*.plt")))
        else:
            plts.append(path)
    missing = [str(p) for p in plts if not p.is_file()]
    if missing:
        raise SystemExit("input PLT(s) not found:\n  " + "\n  ".join(missing))
    return plts


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the profiling harness."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--plts",
        nargs="+",
        default=list(BENCHMARK_PLTS),
        help="Input PLT files (or directories) to profile.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(REPO_ROOT / DEFAULT_OUTPUT_DIR),
        help="Root output directory for profiles, memory reports, and A/B CSVs.",
    )
    parser.add_argument(
        "--history-csv",
        type=Path,
        default=DEFAULT_HISTORY_CSV,
        help="Run-history summary CSV (tracked; sweeps append to its history).",
    )
    parser.add_argument(
        "--repeats", type=int, default=3, help="Timed runs per file (median reported)."
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
        "--fast-mode",
        action="store_true",
        help="NN2Opt routing instead of the parallel ensemble (isolates TSP cost).",
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
        help="Reset the history CSV instead of appending this sweep to it.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point: sweep mode or -O A/B mode."""
    args = build_parser().parse_args(argv)
    plts = _collect_plts(args.plts)
    out_root = Path(args.output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    # The ensemble's spawned children resolve their log level from this
    # env var; keep their INFO chatter off the profiling console (full
    # detail still lands in each file's run.log via the parent handlers).
    os.environ.setdefault("PLT_LOG_LEVEL", "warning")

    if args.ab:
        return ab_mode(args, plts, out_root)

    rows: List[Dict[str, Any]] = []
    # One id per sweep: rows of the same sweep share it (run_ts is stamped
    # per file, seconds apart), so history tools group by run_id.
    run_id = uuid.uuid4().hex[:8]
    for plt_path in plts:
        print(f"\n=== profiling {plt_path.relative_to(REPO_ROOT)} ===")
        rows.append(profile_one_file(plt_path, out_root, args, run_id))

    csv_path = Path(args.history_csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    preserved = _append_csv(csv_path, rows, fresh=args.fresh)

    stage_keys = [key for key in (rows[0] if rows else {}) if str(key).startswith("stage_")]
    print(f"\n=== wall-clock summary (median of {max(1, args.repeats)}) ===")
    header = f"{'spec':<48} {'wall_s':>8} {'cpu_s':>8} {'imp%':>7}"
    print(header)
    for row in rows:
        print(
            f"{row['spec']:<48} {row['wall_s']:>8.2f} {row['cpu_s']:>8.2f} "
            f"{row.get('improvement_pct', 0.0):>7.1f}"
        )
    if stage_keys:
        print("\n=== summed stage seconds (last timed run) ===")
        print(f"{'spec':<48}" + "".join(f"{k[6:-2]:>16}" for k in stage_keys))
        for row in rows:
            print(f"{row['spec']:<48}" + "".join(f"{row.get(k, 0.0):>16.2f}" for k in stage_keys))
    print(f"\nSummary CSV: {csv_path} (history: {len(preserved)} prior row(s) + {len(rows)} new)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
