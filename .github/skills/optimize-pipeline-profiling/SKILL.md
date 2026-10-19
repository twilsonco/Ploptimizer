---
name: optimize-pipeline-profiling
description: 'Measure, optimize, and prove optimize-pipeline (plt-optimizer optimize/watch) performance. Use when: profiling the optimize pipeline, finding bottlenecks in parse/profile/preprocess/chunk/optimize/write, patching a performance hot spot, measuring wall/stage time, maintaining the optimization_pipeline_summary.csv run history, re-measuring a pre-optimization baseline with a git worktree, plotting performance improvements, deciding whether a perf fix is worth it. Keywords: optimize pipeline, profiling, bottleneck, performance, wall time, stage timer, optimization_pipeline_summary, profile_optimize, plot_profiling_history.'
---

# Optimize-Pipeline Profiling & Bottleneck Optimization

The repo's perf loop for the **optimize** pipeline (the `plt-optimizer
optimize` CLI / `watch` daemon core: parse → profile → preprocess → chunk →
optimize+reassemble → write): **measure → identify → patch → re-measure →
plot**. The end-to-end optimization cycle (one bottleneck per cycle, with
the gain report) is the sibling skill
[`../optimize-pipeline-optimization/SKILL.md`](../optimize-pipeline-optimization/SKILL.md);
this skill owns the mechanics it calls into. The generate pipeline has its
own pair (`generate-pipeline-profiling` / `generate-pipeline-optimization`)
with its own harness and history CSV — never mix the two.
The machine-readable record is `profiling/optimization_pipeline_summary.csv`
(TRACKED — commit it with each perf cycle; append-mode HISTORY — every sweep
adds rows, `run_id` groups one sweep, `run_ts` stamps each row). The prose
record is [`../../../docs/PROFILING.md`](../../../docs/PROFILING.md)
("Optimize-pipeline profiling"). Never delete the history CSV; `--fresh`
exists for intentional resets.

## Procedure

### 1. Measure the current state

```sh
uv run python scripts/profile_optimize.py            # full sweep: 2 PLTs x 3 repeats + cProfile + tracemalloc
uv run python scripts/profile_optimize.py --plts "examples/<file>.plt" --repeats 1 --no-profile --no-memory   # quick
```

Flags: `--fast-mode` (NN2Opt, no ensemble — isolates pool-spawn wall),
`--fresh` (reset history). Outputs: the tracked
`profiling/optimization_pipeline_summary.csv` (`--history-csv` relocates it)
+ per-file scratch `.prof`/`.top.txt`/`.mem.txt`/`run.log` under
`test_output/optimize_profiling/`. Stage timers (`stage <name>: <s>s`, INFO)
also land in `logs_optimize/optimizer.log` on any production `optimize` run
(the core-pipeline inner stages fire via
`optimize_and_reassemble(stage_timers=True)`).

### 2. Identify the bottleneck

- `optimization_pipeline_summary.csv`: which stage dominates which file
  (`stage_*` columns) and whether quality moved (`rapid_before_in`,
  `rapid_after_in`, `improvement_pct` — the emitted rapid-travel KPIs).
- `<file>/<file>.top.txt`: top-N cumulative functions. **Call counts are
  the reliable signal** — cProfile inflates pure-Python hot loops ~1.5–2.6×.
- `run.log`: per-file block counts, ensemble benchmark table, per-stage lines.
- Sample the whole run for C-level visibility:
  `uv run --with py-spy py-spy record -o /tmp/profile.svg -- python scripts/profile_optimize.py --plts "<file>" --no-profile`
- Beware misdiagnosis: confirm wall-vs-cpu before blaming a mechanism.
  `wall >> cpu` on the ensemble means pool-spawn/subprocess wall (a knob —
  `--fast-mode`, `ENSEMBLE_SEQUENTIAL_BLOCK_THRESHOLD`), not single-core
  compute (the structural fixture's 0.9 s wall vs 0.04 s cpu is exactly
  this; the generate side's sunwest "spawn" misdiagnosis is the cautionary
  tale — a big layer's pooled compute is real work).

### 3. Patch, pinned

- The fix must be **exact** (bit-identical output PLT) or a deliberate,
  documented behavior change. Prefer provable pre-filters/caching over
  heuristics.
- Add the pin first-class: randomized brute-force equivalence tests for
  geometry changes; fixture pins (see `tests/AGENTS.md`) for pipeline
  changes. `tests_deps/` is FROZEN — the structural benchmark fixture
  (`1x3 half inch letters holes1.plt`) lives there; never edit it.
- Changes under `plt_optimizer/core/` must stay **Python 3.8 / Win7
  import-safe** (no matplotlib, no 3.9+ syntax — root AGENTS.md §5; the
  watch hot path runs on the plotter PC).
- Microbenchmark before adopting numpy: per-op numpy overhead LOSES to
  scalar loops at small arrays; the win is wholesale pruning.
- Full suite green (`uv run pytest -q --no-cov`), ruff/format/mypy clean,
  pre-commit hooks pass. Commit per phase (Conventional Commits).

### 4. Re-measure (appends to history)

Re-run step 1 on the same benchmark set. The sweep appends its rows, so the
before/after pair is in one CSV. Verify output identity against the pre-fix
optimized PLTs when the fix claims bit-identity: compare `sha256` per
output, and for order-only diffs compare **undirected segment multisets**
(PD blocks shift when merge boundaries move; the segment multiset is the
true geometry invariant). Also check `improvement_pct` drift — a speed fix
that changes routing quality is a behavior change, not a perf fix.

### 5. Plot the improvement

```sh
uv run python scripts/plot_profiling_history.py \
    --csv profiling/optimization_pipeline_summary.csv \
    --out-dir test_output/optimize_profiling/plots \
    --metrics wall_s,cpu_s --log-y
```

PNGs land in the chosen plot dir. Matplotlib is dev-side (root AGENTS.md
§5); a matplotlib-free machine gets a `uv run --extra plotting` hint.

### 6. Record

Update `docs/PROFILING.md` ("Optimize-pipeline profiling" section:
bottleneck status, measured tables), `CHANGELOG.md`, `docs/DONE.md`. Mark
fixed bottlenecks `— FIXED <date>`; keep the pre-fix numbers as the
historical baseline.

## Pre-optimization baseline (no history rows for the before-state)

Use a **git worktree** — never check the main tree out of the user's working
state. [baseline_capture.py](./scripts/baseline_capture.py) wraps it:

```sh
uv run python .github/skills/optimize-pipeline-profiling/scripts/baseline_capture.py capture --commit <pre-fix-sha>
uv run python .github/skills/optimize-pipeline-profiling/scripts/baseline_capture.py merge   --commit <pre-fix-sha> --replace-ts <thin-row-ts> --remove-worktree
```

- The script verifies `pyproject.toml`/`uv.lock` are identical between the
  commits (the repo venv is then valid for the baseline tree; the worktree's
  own code wins the import path), runs the FULL sweep inside the worktree
  with `--output-dir` AND `--history-csv` INSIDE the worktree (the defaults
  `REPO_ROOT/test_output` / `REPO_ROOT/profiling` are shared — a sweep there
  would overwrite the main tree's artifacts/history), then merges.
- **Iteration order = code state**: merged rows are stamped `run_ts=<commit
  date>`, not the later measurement date, so the baseline plots as the oldest
  iteration.
- Cleanup: `git worktree remove --force <path>` (the script does it with
  `--remove-worktree`).

## Benchmark set

Two files (`BENCHMARK_PLTS` in the harness) covering the pipeline's two
preprocessing branches:

| File | Hot paths exercised |
|---|---|
| `examples/334X712 RED DANGER conveyers start and stop2.plt` | text branch: chronological chunking of contiguous glyph paths, ensemble TSP (sequential path on this box), direction sweep |
| `tests_deps/1x3 half inch letters holes1.plt` | structural branch: fracture + dedupe preprocessing, one-block-per-segment chunking (drill holes + borders), pooled ensemble |

## Stage table

| Stage | Emplaced in | Covers |
|---|---|---|
| `parse` | `cli/optimize.py` | HPGL tokenization → `PLTDocument` |
| `profile` | `cli/optimize.py` | baseline extent + structural classification |
| `preprocess` | `cli/optimize.py` | fracture + dedupe (structural) / passthrough (text) |
| `chunk` | `cli/optimize.py` | chronological clustering into MacroBlocks |
| `optimize` | `cli/optimize.py` | the whole `optimize_and_reassemble` call |
| `optimize_tsp` | `core/pipeline.py` | strategy/engine solve (ensemble incl. pool) |
| `direction_sweep` | `core/pipeline.py` | post-TSP per-block direction re-pick |
| `intra_sweep` | `core/pipeline.py` | glyph direction DP (generate path only; ~0 here) |
| `reassemble` | `core/pipeline.py` | traversal application + stroke reversal |
| `merge` | `core/pipeline.py` | coincident-stroke merge |
| `write` | `cli/optimize.py` | HPGL emission |

`optimize` = `optimize_tsp` + sweeps + `reassemble` + `merge` (nested
timers; the harness sums each name independently).

## Measured baseline (2026-10-18, `--repeats 3`, ensemble on)

macOS dev box (Apple Silicon, Python 3.11). Wall = median of 3.

| File | wall s | cpu s | imp% | dominant stage(s) |
|---|---|---|---|---|
| 334X712 RED DANGER (text) | 0.19 | 0.19 | 39.0 | `optimize_tsp` 0.16 (sequential ensemble) |
| 1x3 half inch letters holes1 (structural) | 0.91 | 0.04 | 75.8 | `optimize_tsp` 0.90 (pooled ensemble; wall≫cpu = spawn) |

Current #1 bottleneck: **ensemble pool-spawn wall on the structural
fixture** (0.90 s wall / 0.04 s cpu; its 194 single-segment blocks sit
above `ENSEMBLE_SEQUENTIAL_BLOCK_THRESHOLD` = 48, so the 14 strategy jobs
run pooled). It is partly a knob
(`ENSEMBLE_SEQUENTIAL_BLOCK_THRESHOLD`, `--fast-mode`); the exact next win
is shrinking per-job work inside the pool, not removing the pool.

## Gotchas

- Pool microbenchmarks from stdin (`python - << 'PY'`) are INVALID: spawn
  can't re-import `__main__` from `<stdin>`, every job fails, the pool "wins"
  via the serial fallback. Write the harness to a real `.py` file.
- Pool-mechanics unit tests must pin `sequential_block_threshold=0` (small
  inputs take the in-process ensemble path and bypass mocked pools).
- `test_output/` is gitignored (`*_output`): the per-file artifacts are local
  scratch; the history CSV is TRACKED in `profiling/` — commit it with each
  perf cycle (the durable prose is docs/PROFILING.md + CHANGELOG).
- Determinism: SA/GA use the unseeded global `random` (open gap, see
  docs/PROFILING.md); byte-compare determinism checks use `--fast-mode`.
- The structural fixture lives in `tests_deps/` (frozen, `tests/AGENTS.md`):
  read-only benchmark input, never regenerate or reformat it.
- Core-pipeline changes are on the Win7 watch hot path: verify with the
  matplotlib import-blocker (`import main`, `watch --help`) when touching
  CLI import structure, and keep 3.8-compatible syntax in `core/` + `utils/`.
