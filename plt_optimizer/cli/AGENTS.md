# AGENTS.md - CLI Surface (`plt_optimizer/cli/`)

Parent contract: [`../../AGENTS.md`](../../AGENTS.md). This file moved verbatim
from the old root AGENTS.md section 7. `main.py` (repo root) is the single entry
point (`[project.scripts]` in `pyproject.toml` points at `main:main`); the
subcommands live in `optimize.py`, `generate.py`, `watch.py` (+ `benchmark.py`).

## 7. CLI Surface (`optimize` / `generate` / `watch`)

The console script `plt-optimizer` is routed by `main.py` into three
subcommands (`plt_optimizer/cli/optimize.py`, `generate.py`, `watch.py`).
`main.py` must stay the single entry point (`[project.scripts]` in
`pyproject.toml` points at `main:main`).

### `optimize <input.plt>`
Single-file parse → profile (text vs. structural) → chunk → optimize →
reassemble → write. Flags: `-o/--output` (default `<stem>_optimized.plt`
beside the input), `--fast-mode` (NearestNeighbor2Opt only; default is
ParallelEnsemble with per-strategy benchmark logging), `-v/--verbose`,
`--log-dir` (default `./logs_optimize/`; writes `optimizer.log` +
`job_metrics.csv`). Prints one summary line to stdout unless `-v`.

### `generate <spec.yaml>`
Three-phase pipeline: `parse_yaml` → `expand_job_spec` → `resolve_job_spec` →
`export_per_cutter_plts` (bounds-aware packing + per-label rendering + plate-space
per-cutter optimization + split by cutter). Flags: `-o/--output` (default: spec's
parent dir; receives `plt/` and `pdf/`), `-v/--verbose`, `--no-plots` (skip
simple-outline PDF previews), `--default-plots` (opt-in color-coded `*_default.pdf`
rapid-travel plots), `--tools` (default `tools.json`; missing file → ideal
cutters), `--job-config` (default `job-config.json`; top-layer job defaults and
the required-when-unconfigured gate, see
[`../generate/AGENTS.md`](../generate/AGENTS.md)), `--fast-mode` (plate-space
routing via `NearestNeighbor2Opt` instead of the default `ParallelEnsemble`). File names:
`[<2-digit plate>_][<material>_]<cutter>[_<color>]_{txt|bh}_<job_id>.<plt|pdf>`
plus combined `[<2-digit plate>_][<material>_]all_<job_id>.pdf`. The plate
number appears only when a material spans more than one plate (its presence
signals that *this material* needs multiple sheets; material-less plates form
one group, so multi-sheet material-less jobs always keep their numbers); the
material tag appears only for material-declared output.
Simple-outline PDFs style strokes by toolpath kind:
purely structural (`bh`) plots use `linewidth=2.0`/`alpha=0.3`; text and mixed
combined (`all`) plots use `linewidth=1.0`/`alpha=1.0` (via
`plot_plt_document(..., is_structural=...)`). Logs go to `./logs_generate/generate.log`.
Collision aborts (`LabelRenderError`) and fit failures (`LayoutFitError`) exit
non-zero after full ERROR diagnostics.

### `watch --watch-dir <dir>`
Hot-folder daemon. Flags: `--watch-dir` (required), `--output-dir` (default
`./optimized`), `--log-dir` (default `./logs`), `--processed-dir` (archive
originals; without it originals are **deleted**), `--fast-mode`,
`--debug-save-files` (before/after PLTs + PNG plots under `<log-dir>/debug/`;
only with an explicit `--log-dir`), `--debounce-seconds` (default 2.0).
Behavioural invariants:
- Files are processed only after `debounce_seconds` of quiescence **and** after
  the OS file lock is released (`_is_file_locked` probe).
- Outputs are staged in `<output-dir>/.incomplete/` and moved into place with
  `os.replace` (fallback `shutil.move`) — consumers never see partial files.
- On failure, the input is copied to the output dir as `<stem>_unprocessed.plt`
  and removed from the watch dir; the job is logged with `status=failed`.
- `setup_parser()` is the single source of truth for watch flags (shared by the
  `main.py` router and `python -m plt_optimizer.cli.watch`); the tray app calls
  `run_watcher_from_config()` instead of the CLI layer.

### Python 3.8 / Windows 7 import constraint
Per section 5, Windows 7 support is **CLI-only**: the pre-built
`Ploptimizer.exe` installer and the system tray GUI are Windows 10+ only
(dropped for Win7). On Windows 7 the supported workflow is the headless
`watch`/`optimize` CLI on Python 3.8 without matplotlib (not installable on
3.8). Therefore `watch` (and the tray's watcher path) must remain importable
and runnable on Python 3.8 without matplotlib. The `generate` pipeline and its
modules (`plt_optimizer/generate/*`, which import numpy/matplotlib/vpype text
rendering) require Python 3.9+. Keep heavy generation imports out of any code
path the watch/optimize commands execute at startup:
- `main.py` builds all three subparsers at startup, so
  `plt_optimizer/cli/generate.py` stays import-light at module scope: pure
  Python imports (`schema`, `resolution`, `substitution`) are top-level, while
  the matplotlib-transitive ones (`layout`, `label_renderer`, `vectorize`) are
  imported lazily inside `run()`. Tests monkeypatch
  `plt_optimizer.generate.vectorize.export_per_cutter_plts` (the source module),
  not the CLI module attribute.
- Verify with a matplotlib import-blocker that `import main` and
  `plt-optimizer watch --help` succeed without matplotlib before changing CLI
  import structure.
