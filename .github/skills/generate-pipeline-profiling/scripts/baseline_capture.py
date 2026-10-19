#!/usr/bin/env python3
"""Measure a pre-optimization baseline into the profiling history CSV.

Creates a git worktree at the pre-optimization commit (the main tree is
NEVER checked out away from your working state), runs the FULL sweep
there with the repo venv (pyproject/uv.lock must be identical between
the commits, checked up front), then merges the rows into the history
CSV:

- rows are stamped ``run_id=<commit8>`` and ``run_ts=<commit date>`` so
  iteration order follows CODE STATE, not the later measurement time;
- rows whose ``run_ts`` is in ``--replace-ts`` are dropped (the thin
  backfill rows an earlier partial capture left behind).

Usage:
    uv run python .github/skills/generate-pipeline-profiling/scripts/baseline_capture.py capture \\
        --commit <pre-fix-sha>
    uv run python .github/skills/generate-pipeline-profiling/scripts/baseline_capture.py merge \\
        --commit <pre-fix-sha> [--replace-ts TS ...]
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[4]
#: The tracked run-history CSV (``profiling/`` is committed; ``test_output/``
#: is ignored via ``*_output``, so the worktree sweep writes its own copy).
HISTORY = REPO_ROOT / "profiling" / "summary.csv"
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"


def _run(cmd: Sequence[str], cwd: Optional[Path] = None) -> None:
    """Run a command, echoing it; exit on failure."""
    print("$ " + " ".join(str(c) for c in cmd))
    result = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None)
    if result.returncode != 0:
        raise SystemExit(f"command failed (exit {result.returncode}): {' '.join(cmd)}")


def _git(*args: str) -> str:
    """Run git in the repo; return trimmed stdout."""
    result = subprocess.run(["git", *args], cwd=str(REPO_ROOT), capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _commit_date(commit: str) -> str:
    """The commit's ISO-8601 committer date (the code-state iteration stamp)."""
    return _git("log", "-1", "--format=%cI", commit)


def _worktree_path(commit: str) -> Path:
    return Path("/tmp") / f"plt-baseline-{commit[:8]}"


def _read(path: Path) -> List[Dict[str, str]]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def cmd_capture(args: argparse.Namespace) -> int:
    """Worktree + full sweep at the pre-optimization commit."""
    commit = _git("rev-parse", args.commit)
    date = _commit_date(commit)
    print(f"baseline commit {commit[:8]} ({date})")

    # The venv must be valid for the baseline tree: identical dependency
    # manifests, or the comparison is not apples-to-apples.
    for manifest in ("pyproject.toml", "uv.lock"):
        diff = _git("diff", "--stat", commit, "HEAD", "--", manifest)
        if diff:
            raise SystemExit(
                f"{manifest} differs between {commit[:8]} and HEAD:\n{diff}\n"
                "the venv is not valid for the baseline tree; sync a venv for "
                "the baseline commit (uv sync in the worktree) and run the "
                "sweep there manually, then merge."
            )
    if not VENV_PYTHON.is_file():
        raise SystemExit(f"no venv at {VENV_PYTHON} (run uv sync first)")

    wt = _worktree_path(commit)
    if wt.exists():
        print(f"worktree {wt} exists; removing")
        _run(["git", "worktree", "remove", "--force", str(wt)])
    _run(["git", "worktree", "add", str(wt), commit])

    out_dir = wt / "test_output" / "profiling"
    history_csv = wt / "profiling" / "summary.csv"
    _run(
        [
            str(VENV_PYTHON),
            "scripts/profile_generate.py",
            "--repeats",
            str(args.repeats),
            "--output-dir",
            str(out_dir),
            "--history-csv",
            str(history_csv),
        ],
        cwd=wt,
    )
    print(f"\nbaseline sweep done: {history_csv}")
    print(
        "merge into the history with:\n"
        f"  uv run python {Path(__file__).relative_to(REPO_ROOT)} merge --commit {commit[:8]}"
    )
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    """Stamp + merge the captured baseline rows into the history CSV."""
    commit = _git("rev-parse", args.commit)
    wt = _worktree_path(commit)
    prefix_csv = Path(args.csv) if args.csv else wt / "profiling" / "summary.csv"
    if not prefix_csv.is_file():
        raise SystemExit(f"no baseline CSV at {prefix_csv} (run capture first)")
    rows = _read(prefix_csv)
    if not rows:
        raise SystemExit(f"empty baseline CSV at {prefix_csv}")

    ts = _commit_date(commit)
    stamped = [{"run_id": commit[:8], "run_ts": ts, **row} for row in rows]

    history = _read(HISTORY) if HISTORY.is_file() else []
    replace = set(args.replace_ts)
    kept = [row for row in history if row.get("run_ts") not in replace]
    dropped = len(history) - len(kept)

    merged = stamped + kept
    fields: List[str] = []
    for row in merged:
        for key in row:
            if key not in fields:
                fields.append(key)
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, restval="")
        writer.writeheader()
        writer.writerows(merged)

    print(f"merged {len(stamped)} baseline rows (run_id={commit[:8]}, run_ts={ts})")
    print(f"dropped {dropped} replaced row(s); history now {len(merged)} rows")
    if args.remove_worktree:
        _run(["git", "worktree", "remove", "--force", str(wt)])
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser (capture | merge)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    cap = sub.add_parser("capture", help="Worktree + full sweep at a commit.")
    cap.add_argument("--commit", required=True, help="Pre-optimization commit (sha/ref).")
    cap.add_argument("--repeats", type=int, default=3)
    cap.set_defaults(func=cmd_capture)

    mrg = sub.add_parser("merge", help="Merge captured rows into the history CSV.")
    mrg.add_argument("--commit", required=True, help="The captured commit (sha/ref).")
    mrg.add_argument("--csv", default="", help="Baseline CSV (default: worktree path).")
    mrg.add_argument(
        "--replace-ts",
        action="append",
        default=[],
        help="run_ts values to drop from the history (repeatable).",
    )
    mrg.add_argument(
        "--remove-worktree", action="store_true", help="Remove the worktree after merge."
    )
    mrg.set_defaults(func=cmd_merge)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
