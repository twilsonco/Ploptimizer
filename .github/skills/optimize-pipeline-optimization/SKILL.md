---
name: optimize-pipeline-optimization
description: 'End-to-end performance optimization loop for the optimize pipeline (plt-optimizer optimize/watch): profile, identify the top bottleneck, patch it (exact + pinned), re-profile, and report the measured gain. Use when: optimize the optimize pipeline, find and fix the next bottleneck in optimize/watch, make optimization faster, squeeze performance, "what should I optimize next", quantify speedup, iterate on bottlenecks. Keywords: optimize, bottleneck, speedup, profiling loop, iteration report, wall time gain, rapid travel.'
---

# Optimize-Pipeline Optimization Loop

Run one **measure → identify → patch → re-measure → report-gain** cycle on
the **optimize** pipeline (the `plt-optimizer optimize` CLI / `watch` daemon
core), and keep the cycle's evidence in the run-history CSV so every
iteration is comparable. This skill orchestrates the loop; the profiling
mechanics (sweep flags, history CSV, plotting, worktree baselines, stage
table, benchmark set, gotchas) live in the sibling skill
[`../optimize-pipeline-profiling/SKILL.md`](../optimize-pipeline-profiling/SKILL.md)
— read it for detail. The generate pipeline has its own pair of skills and
its own history CSV — never mix the two.

## The cycle

### 0. Baseline (once, if the history has no pre-change sweep)

The history CSV must contain a sweep of the CURRENT code before you patch,
so the gain has a "before". If the last sweep predates uncommitted work,
re-run it first (step 1). To import a historical pre-optimization baseline,
use the profiling skill's `baseline_capture.py` worktree recipe.

### 1. Measure

```sh
uv run python scripts/profile_optimize.py            # appends one iteration to optimization_pipeline_summary.csv
```

Note the printed `run_id` (the new iteration's sweep id) — it is the
baseline for step 5.

### 2. Identify the ONE top bottleneck

Rank by **size of prize** (wall seconds recoverable), not call count. Read
`profiling/optimization_pipeline_summary.csv` (dominant `stage_*` per file)
then the profile: `test_output/optimize_profiling/<file>/<file>.top.txt`
(cumulative functions; **call counts are the reliable signal** — cProfile
inflates pure-Python seconds ~1.5–2.6×). Confirm the diagnosis before
patching: for the ensemble, `wall >> cpu` means **pooled parallel work** —
read the per-job strategy benchmark table in
`test_output/optimize_profiling/<file>/run.log`: the slowest job's seconds
are the pool's critical path (real compute; e.g. the 0.80 s IH jobs set
the structural fixture's 0.90 s wall — NOT spawn, a noop pool is ~0.12 s).
Only after the per-job table is flat is the residue spawn (a knob:
`--fast-mode`, `ENSEMBLE_SEQUENTIAL_BLOCK_THRESHOLD`). Pick the single
highest-leverage fix; do not batch.

### 3. Patch, exactly and pinned

- The fix must be **bit-identical output PLT** (a pre-filter, cache, memo,
  algorithmic win) or a **deliberate, documented** behavior change. Prefer
  provable pruning/caching over heuristics.
- Pin it: randomized brute-force equivalence tests for geometry, fixture
  pins for pipeline behavior (see `tests/AGENTS.md`). Add the test with the
  fix.
- `plt_optimizer/core/` stays Python 3.8 / Win7 import-safe (root
  AGENTS.md §5 — the watch hot path on the plotter PC).
- Full suite green, ruff/format/mypy clean, pre-commit passes. Commit the
  fix on its own (Conventional Commits: `perf: ...`).

### 4. Re-measure (same benchmark set)

```sh
uv run python scripts/profile_optimize.py            # appends the "after" iteration
```

Verify output identity when the fix claims bit-identity: `sha256` per
optimized PLT vs the baseline sweep; for order-only diffs compare undirected
**segment multisets** (the geometry invariant — see the profiling skill).
Check the KPI columns too: `rapid_after_in` / `improvement_pct` moving means
routing quality changed — that is a behavior change, not a perf fix.

### 5. Report the gain

```sh
uv run python .github/skills/optimize-pipeline-optimization/scripts/iteration_report.py
# explicit iterations / metric:
uv run python .../iteration_report.py --baseline <run_id> --target <run_id> --metric stage_optimize_tsp_s
```

Prints per-file before → after → delta → % for `wall_s` (default) with a
TOTAL row, plus the top stages by baseline cost. A negative delta is a win.
These benchmark files are sub-second, so the noise floor is tens of
milliseconds — a gain under ~5% on the 0.9 s structural fixture is noise;
re-run with more `--repeats` before believing it. If the gain is in the run-
to-run noise, the fix is not worth shipping for speed alone.

### 6. Plot + record

```sh
uv run python scripts/plot_profiling_history.py \
    --csv profiling/optimization_pipeline_summary.csv \
    --out-dir test_output/optimize_profiling/plots \
    --metrics wall_s,cpu_s --log-y
```

Update `docs/PROFILING.md` ("Optimize-pipeline profiling": bottleneck status
+ measured table), `CHANGELOG.md`, `docs/DONE.md`. Mark fixed bottlenecks
`— FIXED <date>`; keep the pre-fix numbers as the historical baseline.

### 7. Decide the next cycle

Re-read the post-fix profile: the old bottleneck should be gone and a new
top cost center revealed. Repeat from step 2 with the new top bottleneck.
Stop when the remaining cost is real parallel compute (a knob, e.g.
`--fast-mode`) or below the noise floor.

## Invariants

- **One bottleneck per cycle**, committed separately, so each gain is
  attributable in the history and in git.
- **Never ship a speed fix that changes output** without an explicit,
  documented decision + a pin of the new expected output. The rapid-travel
  KPI columns (`rapid_before_in`, `rapid_after_in`, `improvement_pct`) are
  the quality guard rail.
- **The history CSV is the record**
  (`profiling/optimization_pipeline_summary.csv`, tracked, append-mode —
  commit it with each perf cycle). The durable prose is
  `docs/PROFILING.md`. Never delete history rows; `--fresh` is for
  intentional resets only.
- Determinism byte-checks use `--fast-mode` (SA/GA use the unseeded global
  `random` — open gap, see `docs/PROFILING.md`).
- The structural benchmark (`tests_deps/1x3 half inch letters holes1.plt`)
  is FROZEN (`tests/AGENTS.md`) — benchmark input, never a file to edit.
