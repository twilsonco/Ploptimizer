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
Stage timers (`plt_optimizer/utils/stage_timing.py`, INFO) wrap each stage:
`parse`, `profile`, `preprocess`, `chunk`, `optimize` (the whole
`optimize_and_reassemble` call, with `stage_timers=True` for the inner
`optimize_tsp`/`direction_sweep`/`intra_sweep`/`reassemble`/`merge` lines
from `core/pipeline.py`), `write` — the contract
`scripts/profile_optimize.py` measures (see
[`../../docs/PROFILING.md`](../../docs/PROFILING.md)).

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

#### `generate` output contract (the agent-facing surface)

The `generate` CLI's **stdout is deliberately minimal**: exactly one summary
line, naming the report. Everything else the pipeline learned about the job
lives in the report files, so an agent driving the CLI is not flooded with
progress chatter it has to sift through.

| Stream | Content |
|---|---|
| stdout | ONE line: `Generated '<job>': N PLT file(s), M PDF(s) in <out> (report: <out>/generation_report/report.json)`. Under `-v`, the full layout report is appended (the only verbose affordance). |
| stderr | WARNING and ERROR records only (the console handler is retuned to stderr at WARNING; `-v` raises it to DEBUG). The fatal-error line (`Generation FAILED (<ErrorType>): <msg> (report: ...)`) also goes here. |
| `logs_generate/generate.log` | The text log at the **same** level as the run: WARNING by default (so the INFO chatter never reaches the file either), DEBUG under `-v`. |
| `<out>/generation_report/report.json` | The machine-readable report (see below). |
| `<out>/generation_report/summary.txt` | The human-readable digest of the same data. |

**The generate pipeline runs at WARNING, not INFO.** The operational
chatter is INFO (stage timers, per-plate optimization summaries, the
per-strategy benchmark tables, the font-catalog and substitution notes) and
the report already carries everything worth knowing, so the default run
keeps it out of the log entirely. `-v` escalates the whole pipeline (the
logger, the console handler and the file handler) to DEBUG for a deep-dive
run.

> **The spawned ensemble workers need an explicit handoff.** The
> optimizer's `ParallelEnsembleStrategy` runs its strategies in a spawned
> `ProcessPoolExecutor` pool. A worker starts with a clean interpreter and
> builds its OWN logger, so it knows nothing about the parent's handler
> configuration: left alone it logs at the default INFO level to stdout,
> and its `Running <strategy> on N blocks` chatter lands on stdout even
> though the parent runs at WARNING with the console on stderr. The CLI
> therefore calls `utils.logging.publish_log_policy(...)` right after
> `apply_log_levels(...)`; the policy travels to the child through the
> environment (`PLT_LOG_LEVEL` / `PLT_LOG_CONSOLE` / `PLT_LOG_FILE`), which
> the `spawn` start method copies. See
> [`../utils/AGENTS.md`](../utils/AGENTS.md).

> The per-stage wall clock therefore CANNOT be read back from the log at
> the default level -- the stage timing *records* are INFO and never
> exist. The report's `timings` section gets them from the explicit
> `utils.stage_timing.StageTimings` collector instead (see
> [`../generate/AGENTS.md`](../generate/AGENTS.md)).

The report directory is a **sibling of `plt/` and `pdf/`** — the same
`<out>` the PLT/PDF files land in (`<out>/generation_report/`). It is
created on demand and is written **on success AND on failure**: a failed
job leaves `job.status = "failed"` with `job.error = {type, message, stage}`,
plus every warning logged before the failure, so the reason is available
without re-reading stdout.

`report.json` sections: `schema_version`, `job` (name, id, status, error,
paths, timestamps, duration, tools, options), `summary` (counts), `timings`
(per-stage wall clock), `outputs` (written files + plate placements),
`labels[]` → `lines[]` → `findings[]` (the typed per-line render-time
effects), `warnings[]`, `errors[]`. The findings come from
`layout_report.collect_line_findings`, so the JSON and the text report
describe the same effects.

Implementation: [`../generate/generation_report.py`](../generate/generation_report.py)
(the report builder), `utils.logging.apply_log_levels` (the console/stream
retuning), and the CLI's own `run()`.

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
