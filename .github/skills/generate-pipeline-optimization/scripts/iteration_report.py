#!/usr/bin/env python3
"""Report the optimization gain between two profiling iterations.

Reads the run-history CSV (``scripts/profile_generate.py`` appends every
sweep to the tracked ``profiling/summary.csv``; ``run_id`` groups one sweep)
and prints a per-job scoreboard for a metric (default ``wall_s``): baseline
-> target, delta, percent, plus the top-N stage columns that moved. This is
the "did the patch pay off?" step of the optimization loop — run it after
re-profiling.

Usage:
    uv run python .github/skills/generate-generate-pipeline-optimization/scripts/iteration_report.py
    uv run python .../iteration_report.py --baseline <run_id> --target <run_id>
    uv run .../iteration_report.py --metric stage_reflow_s --top-stages 5
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:  # allow running as a plain script
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_CSV = REPO_ROOT / "profiling" / "summary.csv"
KEY_COLUMNS = ("run_id", "run_ts", "spec")


def load_rows(csv_path: Path) -> List[Dict[str, str]]:
    """Read the history CSV; SystemExit with a hint when missing."""
    if not csv_path.is_file():
        raise SystemExit(
            f"no history CSV at {csv_path}\n"
            "run the sweep first: uv run python scripts/profile_generate.py"
        )
    with open(csv_path, newline="") as f:
        return list(csv.DictReader(f))


def iteration_key(row: Dict[str, str]) -> str:
    """Sweep id for a row: ``run_id`` when present, else ``run_ts``."""
    return row.get("run_id") or row.get("run_ts") or ""


def iteration_ids(rows: Sequence[Dict[str, str]]) -> List[str]:
    """Sweep ids ordered oldest-first (by earliest run_ts, then id)."""
    first_ts: Dict[str, str] = {}
    for row in rows:
        key = iteration_key(row)
        ts = row.get("run_ts") or ""
        if key not in first_ts or ts < first_ts[key]:
            first_ts[key] = ts
    return sorted(first_ts, key=lambda key: (first_ts[key], key))


def rows_by_spec(rows: Sequence[Dict[str, str]], run_id: str) -> Dict[str, Dict[str, str]]:
    """Rows of one sweep keyed by spec (last row wins within the sweep)."""
    out: Dict[str, Dict[str, str]] = {}
    for row in rows:
        if iteration_key(row) == run_id:
            out[row.get("spec", "?")] = row
    return out


def metric_columns(rows: Sequence[Dict[str, str]]) -> List[str]:
    """Numeric columns (CSV order), excluding the key columns."""
    columns: List[str] = []
    for row in rows:
        for key in row:
            if key not in KEY_COLUMNS and key not in columns:
                columns.append(key)
    return [name for name in columns if any(_to_float(row.get(name)) is not None for row in rows)]


def gains(
    baseline: Dict[str, Dict[str, str]],
    target: Dict[str, Dict[str, str]],
    metric: str,
) -> List[Tuple[str, float, float]]:
    """Per-spec (spec, before, after) for a metric, sorted by biggest drop.

    A spec present in both iterations with a numeric value in each is a
    row; specs missing from either side are skipped.
    """
    out: List[Tuple[str, float, float]] = []
    for spec in sorted(set(baseline) & set(target)):
        before = _to_float(baseline[spec].get(metric))
        after = _to_float(target[spec].get(metric))
        if before is None or after is None:
            continue
        out.append((spec, before, after))
    # Biggest improvement (drop) first; ties by name.
    out.sort(key=lambda row: (-(row[1] - row[2]), row[0]))
    return out


def stage_deltas(
    baseline: Dict[str, Dict[str, str]],
    target: Dict[str, Dict[str, str]],
    *,
    top: int,
) -> List[Tuple[str, str, float, float]]:
    """Top-N (spec, stage, before, after) by baseline stage seconds.

    Only stages numeric in both iterations are considered; the ranking is
    by baseline magnitude so the biggest cost centers surface first.
    """
    stages = _stage_columns(baseline, target)
    scored: List[Tuple[str, str, float, float]] = []
    for spec in set(baseline) & set(target):
        for stage in stages:
            before = _to_float(baseline[spec].get(stage))
            after = _to_float(target[spec].get(stage))
            if before is None or after is None:
                continue
            scored.append((spec, stage, before, after))
    scored.sort(key=lambda row: -row[2])
    return scored[:top]


def _stage_columns(*dicts: Dict[str, Dict[str, str]]) -> List[str]:
    """Stage columns (``stage_*``) present in the given row maps."""
    seen: List[str] = []
    for mapping in dicts:
        for row in mapping.values():
            for key in row:
                if key.startswith("stage_") and key not in seen:
                    seen.append(key)
    return seen


def _to_float(value: Optional[str]) -> Optional[float]:
    """Parse a CSV cell as float; None for empty/unparsable cells."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def format_report(
    baseline_id: str,
    target_id: str,
    metric: str,
    gain_rows: Sequence[Tuple[str, float, float]],
    stage_rows: Sequence[Tuple[str, str, float, float]],
) -> str:
    """Render the scoreboard text for one metric + stage deltas."""
    lines = [f"gain: {baseline_id} -> {target_id}  metric={metric}"]
    lines.append(f"{'spec':44s} {'before':>9s} {'after':>9s} {'delta':>9s} {'pct':>8s}")
    total_before = total_after = 0.0
    for spec, before, after in gain_rows:
        delta = after - before
        pct = (delta / before * 100.0) if before > 0 else 0.0
        total_before += before
        total_after += after
        lines.append(f"{spec[:43]:44s} {before:9.2f} {after:9.2f} {delta:+9.2f} {pct:+7.1f}%")
    if gain_rows:
        total_delta = total_after - total_before
        total_pct = (total_delta / total_before * 100.0) if total_before > 0 else 0.0
        lines.append(
            f"{'TOTAL':44s} {total_before:9.2f} {total_after:9.2f} "
            f"{total_delta:+9.2f} {total_pct:+7.1f}%"
        )
    if stage_rows:
        lines.append(f"\ntop stages by baseline cost ({baseline_id}):")
        lines.append(f"{'spec':40s} {'stage':26s} {'before':>9s} {'after':>9s}")
        for spec, stage, before, after in stage_rows:
            lines.append(f"{spec[:39]:40s} {stage[6:-2][:25]:26s} {before:9.2f} {after:9.2f}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="History CSV.")
    parser.add_argument(
        "--baseline", default="", help="Baseline run_id (default: second-to-last sweep)."
    )
    parser.add_argument("--target", default="", help="Target run_id (default: last sweep).")
    parser.add_argument("--metric", default="wall_s", help="Metric column (default wall_s).")
    parser.add_argument(
        "--top-stages", type=int, default=5, help="Stage rows to show (0 disables)."
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point: load, compare, print."""
    args = build_parser().parse_args(argv)
    rows = load_rows(args.csv)
    ids = iteration_ids(rows)
    if len(ids) < 2 and not (args.baseline and args.target):
        raise SystemExit(
            f"need at least two sweeps to compare (have {len(ids)}: {', '.join(ids)})\n"
            "run the sweep again after your patch, or pass --baseline/--target"
        )
    baseline_id = args.baseline or ids[-2]
    target_id = args.target or ids[-1]
    baseline = rows_by_spec(rows, baseline_id)
    target = rows_by_spec(rows, target_id)
    if not baseline or not target:
        raise SystemExit(f"no rows for baseline={baseline_id!r} or target={target_id!r}")
    if args.metric not in metric_columns(rows):
        raise SystemExit(
            f"unknown metric {args.metric!r}; available: {', '.join(metric_columns(rows))}"
        )
    gain_rows = gains(baseline, target, args.metric)
    if not gain_rows:
        raise SystemExit(f"no specs with numeric {args.metric!r} in both sweeps")
    stage_rows = stage_deltas(baseline, target, top=args.top_stages) if args.top_stages > 0 else []
    print(format_report(baseline_id, target_id, args.metric, gain_rows, stage_rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
