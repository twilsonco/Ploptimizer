#!/usr/bin/env python3
"""Plot the profiling history (summary.csv) as one chart per metric.

Reads the run-history CSV written by ``scripts/profile_generate.py``
(every sweep appends rows stamped with ``run_id``/``run_ts``) and renders
one PNG per numeric metric column: x = sweep iteration (oldest first),
one line per job spec. Comparing optimization work across iterations
needs no git — the CSV is the record.

Usage:
    uv run python scripts/plot_profiling_history.py
    uv run python scripts/plot_profiling_history.py --metrics wall_s,cpu_s
    uv run python scripts/plot_profiling_history.py --log-y --show

Matplotlib is a dev-side dependency (root AGENTS.md §5: plotting never
runs on the Windows 7 target); a missing matplotlib exits with guidance,
never a traceback.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # allow running as a plain script
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_CSV = REPO_ROOT / "test_output" / "profiling" / "summary.csv"

#: Columns that identify a row rather than measure it.
KEY_COLUMNS = ("run_id", "run_ts", "spec")


def load_rows(csv_path: Path) -> List[Dict[str, str]]:
    """Read the history CSV; raise SystemExit with a hint when missing."""
    if not csv_path.is_file():
        raise SystemExit(
            f"no history CSV at {csv_path}\n"
            "run the sweep first: uv run python scripts/profile_generate.py"
        )
    with open(csv_path, newline="") as f:
        return list(csv.DictReader(f))


def iteration_key(row: Dict[str, str]) -> str:
    """Return the row's sweep id: ``run_id`` when present, else ``run_ts``.

    Backfilled rows predate ``run_id``; their per-spec ``run_ts`` is shared
    across the sweep, so it groups them correctly.
    """
    return row.get("run_id") or row.get("run_ts") or ""


def build_iterations(
    rows: Sequence[Dict[str, str]],
) -> Tuple[List[str], Dict[str, str]]:
    """Order sweep ids oldest-first.

    Returns:
        ``(iterations, label_by_iteration)`` where ``iterations`` are sweep
        ids sorted by their earliest ``run_ts``, and the label maps an id to
        a short display string (``#N ts``).
    """
    first_ts: Dict[str, str] = {}
    for row in rows:
        key = iteration_key(row)
        ts = row.get("run_ts") or ""
        if key not in first_ts or ts < first_ts[key]:
            first_ts[key] = ts
    iterations = sorted(first_ts, key=lambda key: (first_ts[key], key))
    labels = {key: f"#{i} {first_ts[key][:16]}" for i, key in enumerate(iterations, start=1)}
    return iterations, labels


def metric_names(rows: Sequence[Dict[str, str]], requested: Optional[Sequence[str]]) -> List[str]:
    """Numeric metric columns (CSV order), filtered by ``requested``.

    A column qualifies when at least one row parses as a float. Requested
    names that do not exist raise SystemExit.
    """
    columns: List[str] = []
    for row in rows:
        for key in row:
            if key not in KEY_COLUMNS and key not in columns:
                columns.append(key)
    numeric = [
        name for name in columns if any(_to_float(row.get(name)) is not None for row in rows)
    ]
    if requested is None:
        return numeric
    unknown = [name for name in requested if name not in numeric]
    if unknown:
        raise SystemExit(
            f"unknown metric(s): {', '.join(unknown)}\navailable: {', '.join(numeric)}"
        )
    return list(requested)


def series_for_metric(
    rows: Sequence[Dict[str, str]],
    iterations: Sequence[str],
    metric: str,
) -> Dict[str, List[Optional[float]]]:
    """Map spec -> per-iteration metric values (None where missing).

    Repeated rows for one (spec, iteration) take the last, matching the
    CSV's append-only semantics (a re-run within a sweep supersedes).
    """
    index = {key: i for i, key in enumerate(iterations)}
    out: Dict[str, List[Optional[float]]] = {}
    for row in rows:
        spec = row.get("spec") or "?"
        values = out.setdefault(spec, [None] * len(iterations))
        value = _to_float(row.get(metric))
        values[index[iteration_key(row)]] = value
    return out


def _to_float(value: Optional[str]) -> Optional[float]:
    """Parse a CSV cell as float; None for empty/unparsable cells."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def plot_history(
    rows: Sequence[Dict[str, str]],
    metrics: Sequence[str],
    out_dir: Path,
    *,
    log_y: bool = False,
    show: bool = False,
) -> List[Path]:
    """Render one PNG per metric into *out_dir*; return the written paths.

    ``show`` additionally opens each figure in an interactive window
    (matplotlib's native GUI backend; the PNGs are always written first).
    """
    import matplotlib

    if not show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    iterations, labels = build_iterations(rows)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for metric in metrics:
        fig, ax = plt.subplots(figsize=(9.0, 5.0))
        for spec, values in sorted(series_for_metric(rows, iterations, metric).items()):
            points = [
                (i, value) for i, value in enumerate(values) if value is not None
            ]
            if not points:
                continue
            ax.plot([i for i, _v in points], [v for _i, v in points], marker="o", label=spec)
        ax.set_xticks(range(len(iterations)))
        ax.set_xticklabels([labels[key] for key in iterations], rotation=30, ha="right")
        ax.set_xlabel("profiling sweep (oldest first)")
        ax.set_ylabel(metric)
        ax.set_title(f"{metric} per job across profiling sweeps")
        ax.grid(True, alpha=0.3)
        if log_y:
            ax.set_yscale("log")
        ax.legend(fontsize=8)
        fig.tight_layout()
        path = out_dir / f"{metric}.png"
        fig.savefig(path, dpi=150)
        print(f"wrote {path}")
        written.append(path)
        if not show:
            plt.close(fig)
    if show:
        plt.show()
    return written


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="History CSV.")
    parser.add_argument(
        "--metrics",
        default="",
        help="Comma-separated metric columns (default: every numeric column).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_CSV.parent / "plots",
        help="Directory for the per-metric PNGs.",
    )
    parser.add_argument("--log-y", action="store_true", help="Logarithmic y axis (wide ranges).")
    parser.add_argument(
        "--show", action="store_true", help="Open the plots in a window after writing."
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point: load, plot, report."""
    args = build_parser().parse_args(argv)
    rows = load_rows(args.csv)
    requested = [m.strip() for m in args.metrics.split(",") if m.strip()] or None
    metrics = metric_names(rows, requested)
    if not metrics:
        raise SystemExit(f"no numeric metric columns in {args.csv}")
    iterations, labels = build_iterations(rows)
    specs = sorted({row.get("spec", "?") for row in rows})
    print(
        f"{len(rows)} rows, {len(iterations)} sweep(s), {len(specs)} job(s), "
        f"{len(metrics)} metric(s)"
    )
    for key in iterations:
        print(f"  iteration {labels[key]}")
    try:
        plot_history(rows, metrics, args.out_dir, log_y=args.log_y, show=args.show)
    except ImportError:
        raise SystemExit(
            "matplotlib is required for plotting (dev-side extra):\n"
            "  uv run --extra plotting python scripts/plot_profiling_history.py"
        ) from None
    return 0


if __name__ == "__main__":
    sys.exit(main())
