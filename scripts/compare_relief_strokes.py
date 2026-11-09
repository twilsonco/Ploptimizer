#!/usr/bin/env python3
"""Strict stroke-level comparison of a generated relief PLT vs a reference.

The relief engraving equivalence tests compare MATERIAL REMOVED (stroke
envelopes dilated by the tool radius) -- a loose contract that cannot see a
missing raster row, a lost chain connector, or an outline drawn as 40 chords
where the reference cuts one arc. This script compares the toolpaths as
STROKES: every pen-down run of each file is sampled at a fixed step, and
each stroke's mean distance to the nearest sample of the other file decides
its verdict (bidirectional chamfer, tolerance = 1 plotter unit by default):

- ``MISSING`` - reference strokes the generated file does not reproduce;
- ``EXTRA``   - generated strokes the reference does not have;
- ``matched`` - reproduced within tolerance (mean/max error reported).

Stroke order, chaining and pen routing are irrelevant (our plate-space TSP
routes toolpaths differently than EngraveLab's sweep order) -- only the
emitted cutting geometry is compared. Coordinates are compared as parsed
(device/plotter frame, inches; the plate transform maps label geometry into
the same frame the reference files use -- no mirroring).

Usage::

    uv run python scripts/compare_relief_strokes.py \
        --ours out/plt/60_txt_relief_simple.plt \
        --ref  examples/plt_relief_engraving/A_relief_s-sweep_coarse_stroke_0.06.plt

    # JSON dump + overlay PNG (matplotlib, dev-only):
    ... --json diff.json --plot diff.png

Exit code: 0 = PASS (no missing/extra within tolerance), 1 = FAIL, 2 = bad
usage.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # allow running as a plain script
    sys.path.insert(0, str(REPO_ROOT))

from plt_optimizer.diagnostics.stroke_compare import (  # noqa: E402
    PLOTTER_UNIT,
    ComparisonReport,
    SampledStroke,
    compare_files,
)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Build the CLI parser (kept separate so tests can drive ``main``)."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--ours", required=True, type=Path, help="Generated PLT file.")
    parser.add_argument("--ref", required=True, type=Path, help="Reference PLT file.")
    parser.add_argument(
        "--tol",
        type=float,
        default=PLOTTER_UNIT,
        help=f"Match tolerance in inches (default 1 plotter unit = {PLOTTER_UNIT:.6f}).",
    )
    parser.add_argument(
        "--sample",
        type=float,
        default=5e-4,
        help="Sample step along strokes in inches (default 0.0005).",
    )
    parser.add_argument("--json", type=Path, default=None, help="Write the full report as JSON.")
    parser.add_argument(
        "--plot",
        type=Path,
        default=None,
        help="Write an overlay PNG (matched=black, missing=red, extra=blue).",
    )
    parser.add_argument(
        "--max-rows", type=int, default=40, help="Max table rows per section (default 40)."
    )
    return parser.parse_args(argv)


def _write_plot(
    ours: Sequence[SampledStroke],
    ref: Sequence[SampledStroke],
    report: ComparisonReport,
    out: Path,
) -> None:
    """Render the overlay PNG (dev-only matplotlib, imported lazily).

    Reference strokes: black (matched) / red (missing). Our strokes: gray
    dashed (matched) / blue dashed (extra) - the overlay shows exactly which
    cutting geometry deviates.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    missing = {v.index for v in report.missing}
    extra = {v.index for v in report.extra}
    fig, ax = plt.subplots(figsize=(8, 8))
    for stroke in ref:
        pts = list(stroke.points)
        ax.plot(
            *zip(*pts),
            color="red" if stroke.index in missing else "black",
            linewidth=0.7,
            zorder=2,
        )
    for stroke in ours:
        pts = list(stroke.points)
        ax.plot(
            *zip(*pts),
            color="blue" if stroke.index in extra else "0.6",
            linewidth=0.7,
            linestyle="--",
            zorder=3,
        )
    ax.set_aspect("equal")
    ax.set_title(
        f"ours vs reference  (missing {len(missing)}, extra {len(extra)}, tol {report.tol:.4f}in)"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the comparison, print the report, write JSON/PNG when asked."""
    args = parse_args(argv)
    if not args.ours.is_file():
        print(f"error: --ours file not found: {args.ours}", file=sys.stderr)
        return 2
    if not args.ref.is_file():
        print(f"error: --ref file not found: {args.ref}", file=sys.stderr)
        return 2
    report = compare_files(args.ours, args.ref, tol=args.tol, sample=args.sample)
    print(report.format_report(max_rows=args.max_rows))
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    if args.plot is not None:
        from plt_optimizer.diagnostics.stroke_compare import parse_strokes

        _write_plot(
            parse_strokes(args.ours, sample=args.sample),
            parse_strokes(args.ref, sample=args.sample),
            report,
            args.plot,
        )
        print(f"plot: {args.plot}")
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
