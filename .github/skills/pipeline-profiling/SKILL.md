---
name: pipeline-profiling
description: 'Measure, optimize, and prove generate-pipeline performance. Use when: profiling the generate pipeline, finding bottlenecks, patching a performance hot spot, measuring wall/stage time, maintaining the summary.csv run history, re-measuring a pre-optimization baseline with a git worktree, plotting performance improvements, deciding whether a perf fix is worth it. Keywords: profiling, bottleneck, performance, wall time, stage timer, summary.csv, profile_generate, plot_profiling_history.'
---

# Generate-Pipeline Profiling & Bottleneck Optimization

The repo's perf loop: **measure → identify → patch → re-measure → plot**.
The end-to-end optimization cycle (one bottleneck per cycle, with the
gain report) is the sibling skill
[`../generate-pipeline-optimization/SKILL.md`](../generate-pipeline-optimization/SKILL.md);
this skill owns the mechanics it calls into.
The machine-readable record is `test_output/profiling/summary.csv` (gitignored
scratch, append-mode HISTORY — every sweep adds rows, `run_id` groups one
sweep, `run_ts` stamps each row). The prose record is
[`../../../docs/PROFILING.md`](../../../docs/PROFILING.md). Never delete the
history CSV; `--fresh` exists for intentional resets.

## Procedure

### 1. Measure the current state

```sh
uv run python scripts/profile_generate.py            # full sweep: 5 specs x 3 repeats + cProfile + tracemalloc
uv run python scripts/profile_generate.py --specs "examples/job_specs/<spec>.yaml" --repeats 1 --no-profile --no-memory   # quick
```

Flags: `--no-optimize` (render/pack only), `--fast-mode` (NN2Opt, no ensemble),
`--plots` (matplotlib cost), `--fresh` (reset history). Outputs:
`test_output/profiling/summary.csv` + per-spec `.prof`/`.top.txt`/`.mem.txt`/`run.log`.
Stage timers (`stage <name>: <s>s`, INFO) also land in `logs_generate/generate.log`
on any production run.

### 2. Identify the bottleneck

- `summary.csv`: which stage dominates which spec (`stage_*` columns).
- `<spec>/<spec>.top.txt`: top-N cumulative functions. **Call counts are the
  reliable signal** — cProfile inflates pure-Python hot loops ~1.5–2.6×.
- `run.log`: per-layer sizes (`Running Parallel Ensemble on N blocks`),
  per-stage lines.
- Sample the whole run for C-level visibility:
  `uv run --with py-spy py-spy record -o /tmp/profile.svg -- python scripts/profile_generate.py --specs "<spec>" --no-profile`
- Beware misdiagnosis: confirm wall-vs-cpu and the real work size before
  blaming a mechanism (the "ensemble spawn" diagnosis for sunwest was wrong —
  it was a 481-block layer's real parallel compute; sequential was 3.5× worse).

### 3. Patch, pinned

- The fix must be **exact** (bit-identical output) or a deliberate, documented
  behavior change. Prefer provable pre-filters/caching over heuristics.
- Add the pin FIRST-class: randomized brute-force equivalence tests for
  geometry changes; fixture pins (see `tests/AGENTS.md`) for pipeline changes.
- Microbenchmark before adopting numpy: per-op numpy overhead LOSES to scalar
  loops at glyph-sized arrays (tens of elements); the win is wholesale
  pruning, not vectorizing small work.
- Full suite green (`uv run pytest -q --no-cov`), ruff/format/mypy clean,
  pre-commit hooks pass. Commit per phase (Conventional Commits).

### 4. Re-measure (appends to history)

Re-run step 1 on the same benchmark set. The sweep appends its rows, so the
before/after pair is in one CSV. Verify output identity against the pre-fix
PLTs when the fix claims bit-identity: compare `sha256` per PLT, and for
order-only diffs compare **undirected segment multisets** (PD blocks shift
when merge boundaries move; the segment multiset is the true geometry
invariant).

### 5. Plot the improvement

```sh
uv run python scripts/plot_profiling_history.py                 # one PNG per metric, one line per job
uv run python scripts/plot_profiling_history.py --metrics wall_s,cpu_s --log-y
```

PNGs land in `test_output/profiling/plots/`. Matplotlib is dev-side (root
AGENTS.md §5); a matplotlib-free machine gets a `uv run --extra plotting` hint.

### 6. Record

Update `docs/PROFILING.md` (bottleneck status, measured tables),
`CHANGELOG.md`, `docs/DONE.md`. Mark fixed bottlenecks `— FIXED <date>`; keep
the pre-fix numbers as the historical baseline.

## Pre-optimization baseline (no history rows for the before-state)

Use a **git worktree** — never check the main tree out of the user's working
state. [baseline_capture.py](./scripts/baseline_capture.py) wraps it:

```sh
uv run python .github/skills/pipeline-profiling/scripts/baseline_capture.py capture --commit <pre-fix-sha>
uv run python .github/skills/pipeline-profiling/scripts/baseline_capture.py merge   --commit <pre-fix-sha> --replace-ts <thin-row-ts> --remove-worktree
```

- The script verifies `pyproject.toml`/`uv.lock` are identical between the
  commits (the repo venv is then valid for the baseline tree; the worktree's
  own code wins the import path), runs the FULL sweep inside the worktree with
  `--output-dir` INSIDE the worktree (the default `REPO_ROOT/test_output` is
  shared — a sweep there overwrites the main tree's artifacts), then merges.
- **Iteration order = code state**: merged rows are stamped `run_ts=<commit
  date>`, not the later measurement date, so the baseline plots as the oldest
  iteration.
- Cleanup: `git worktree remove --force <path>` (the script does it with
  `--remove-worktree`).

## Gotchas

- Pool microbenchmarks from stdin (`python - << 'PY'`) are INVALID: spawn
  can't re-import `__main__` from `<stdin>`, every job fails, the pool "wins"
  via the serial fallback. Write the harness to a real `.py` file.
- Pool-mechanics unit tests must pin `sequential_block_threshold=0` (small
  inputs take the in-process ensemble path and bypass mocked pools).
- `test_output/` is gitignored (`*_output`): the CSV is local scratch; the
  durable record is docs/PROFILING.md + CHANGELOG.
- Determinism: SA/GA use the unseeded global `random` (open gap, see
  docs/PROFILING.md); byte-compare determinism checks use `--fast-mode`.
