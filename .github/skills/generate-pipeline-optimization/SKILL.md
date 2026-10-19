---
name: generate-generate-pipeline-optimization
description: 'End-to-end performance optimization loop for the generate pipeline: profile, identify the top bottleneck, patch it (exact + pinned), re-profile, and report the measured gain. Use when: optimize the pipeline, find and fix the next bottleneck, make generation faster, squeeze performance, "what should I optimize next", quantify speedup, iterate on bottlenecks. Keywords: optimize, bottleneck, speedup, profiling loop, iteration report, wall time gain.'
---

# Generate-Pipeline Optimization Loop

Run one **measure → identify → patch → re-measure → report-gain** cycle,
and keep the cycle's evidence in the run-history CSV so every iteration is
comparable. This skill orchestrates the loop; the profiling mechanics
(sweep flags, history CSV, plotting, worktree baselines, gotchas) live in
the sibling skill
[`../generate-pipeline-profiling/SKILL.md`](../generate-pipeline-profiling/SKILL.md)
— read it for detail.

## The cycle

### 0. Baseline (once, if the history has no pre-change sweep)

The history CSV must contain a sweep of the CURRENT code before you patch,
so the gain has a "before". If the last sweep predates uncommitted work,
re-run it first (step 1). To import a historical pre-optimization baseline,
use the profiling skill's `baseline_capture.py` worktree recipe.

### 1. Measure

```sh
uv run python scripts/profile_generate.py            # appends one iteration to summary.csv
```

Note the printed `run_id` (the new iteration's sweep id) — it is the
baseline for step 5.

### 2. Identify the ONE top bottleneck

Rank by **size of prize** (wall seconds recoverable), not call count. Read
`summary.csv` (dominant `stage_*` per spec) then the profile:
`test_output/profiling/<spec>/<spec>.top.txt` (cumulative functions;
**call counts are the reliable signal** — cProfile inflates pure-Python
seconds ~1.5–2.6×). Confirm the diagnosis before patching: check wall-vs-cpu
and the real work size (see the sunwest spawn misdiagnosis in the profiling
skill's Gotchas). Pick the single highest-leverage fix; do not batch.

### 3. Patch, exactly and pinned

- The fix must be **bit-identical output** (a pre-filter, cache, memo,
  algorithmic win) or a **deliberate, documented** behavior change. Prefer
  provable pruning/caching over heuristics.
- Pin it: randomized brute-force equivalence tests for geometry, fixture
  pins for pipeline behavior (see `tests/AGENTS.md`). Add the test with the
  fix.
- Full suite green, ruff/format/mypy clean, pre-commit passes. Commit the
  fix on its own (Conventional Commits: `perf: ...`).

### 4. Re-measure (same benchmark set)

```sh
uv run python scripts/profile_generate.py            # appends the "after" iteration
```

Verify output identity when the fix claims bit-identity: `sha256` per PLT
vs the baseline sweep; for order-only diffs compare undirected **segment
multisets** (the geometry invariant — see the profiling skill).

### 5. Report the gain

```sh
uv run python .github/skills/generate-generate-pipeline-optimization/scripts/iteration_report.py
# explicit iterations / metric:
uv run python .../iteration_report.py --baseline <run_id> --target <run_id> --metric stage_reflow_s
```

Prints per-job before → after → delta → % for `wall_s` (default) with a
TOTAL row, plus the top stages by baseline cost. A negative delta is a win.
If the gain is in the run-to-run noise (±1–3s on these specs — see
`docs/PROFILING.md`), the fix is not worth shipping for speed alone.

### 6. Plot + record

```sh
uv run python scripts/plot_profiling_history.py --metrics wall_s,cpu_s --log-y
```

Update `docs/PROFILING.md` (bottleneck status + measured table),
`CHANGELOG.md`, `docs/DONE.md`. Mark fixed bottlenecks `— FIXED <date>`;
keep the pre-fix numbers as the historical baseline.

### 7. Decide the next cycle

Re-read the post-fix profile: the old bottleneck should be gone and a new
top cost center revealed. Repeat from step 2 with the new top bottleneck.
Stop when the remaining cost is real parallel compute (a knob, e.g.
`--fast-mode`) or below the noise floor.

## Invariants

- **One bottleneck per cycle**, committed separately, so each gain is
  attributable in the history and in git.
- **Never ship a speed fix that changes output** without an explicit,
  documented decision + a pin of the new expected output.
- **The history CSV is the record** (`profiling/summary.csv`, tracked,
  append-mode — commit it with each perf cycle). The durable prose is
  `docs/PROFILING.md`. Never delete history rows; `--fresh` is for
  intentional resets only.
- Determinism byte-checks use `--fast-mode` (SA/GA use the unseeded global
  `random` — open gap, see `docs/PROFILING.md`).
