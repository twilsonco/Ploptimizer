# Profiling

Two pipelines, two harnesses, two tracked run histories:

| Pipeline | Harness | History CSV | Skills |
|---|---|---|---|
| `generate` (YAML → per-cutter PLTs) | `scripts/profile_generate.py` | `profiling/summary.csv` | `generate-pipeline-profiling` / `generate-pipeline-optimization` |
| `optimize` (PLT → optimized PLT; the `optimize` CLI + `watch` core) | `scripts/profile_optimize.py` | `profiling/optimization_pipeline_summary.csv` | `optimize-pipeline-profiling` / `optimize-pipeline-optimization` |

# Generate-Pipeline Profiling

How to measure where the `generate` pipeline spends time and memory, what
the benchmark set covers, and the measured verdict on running Python in
optimized mode (`-O`).

Harness: [`../scripts/profile_generate.py`](../scripts/profile_generate.py)
(stdlib only: `cProfile` + `tracemalloc` + `time.perf_counter` — no new
dependencies). It runs the exact production sequence of
`plt_optimizer.cli.generate.run` (parse → expand → resolve → export) for
each spec.

## Stage timers

The pipeline emits one machine-parseable INFO line per stage
(`plt_optimizer/utils/stage_timing.py`, format `stage <name>: <s>s`,
contract pinned by `tests/test_stage_timers.py`):

| Stage | Emplaced in | Covers |
|---|---|---|
| `parse` | `cli/generate.py` | YAML parse + replacement-text expansion |
| `resolve` | `cli/generate.py` | `resolve_job_spec` (cascade, cutter ladder, extents probe renders) |
| `reflow` | `generate/vectorize.py` | `optimize_line_content` word reflow (probe renders) |
| `cutter_downsize` | `generate/vectorize.py` | compression-driven cutter reduction pre-pass |
| `h_compress` | `generate/vectorize.py` | shared compression pre-pass |
| `layout_render_pack` | `generate/vectorize.py` | per-label `render_label_to_plt` + rectpack / ellipse packing |
| `assemble_split` | `generate/vectorize.py` | per-plate assembly + pen extraction |
| `optimize_layer` | `generate/vectorize.py` | per-layer TSP routing (ensemble; one line per layer) |
| `plots` / `default_plots` | `generate/vectorize.py` | matplotlib PDF previews |

The lines land in the normal log stream (INFO), so they are visible in
`logs_generate/generate.log` for any production run, not only under the
profiler.

## Usage

```sh
# Full benchmark sweep (5 specs x 3 timed repeats + cProfile + tracemalloc)
uv run python scripts/profile_generate.py

# Custom specs (files or directories), fewer repeats, skip profiling runs
uv run python scripts/profile_generate.py --specs "examples/job_specs/my job.yaml" \
    --repeats 1 --no-profile --no-memory

# Isolate cost centers
uv run python scripts/profile_generate.py --no-optimize   # render/pack only (no TSP)
uv run python scripts/profile_generate.py --fast-mode     # NN2Opt, no parallel ensemble
uv run python scripts/profile_generate.py --plots         # include matplotlib PDF cost
```

Outputs:

- `profiling/summary.csv` — **tracked** (commit it with each perf cycle):
  per spec, median wall/cpu, peak traced MiB, summed seconds per stage. It
  is a **run history**: every sweep APPENDS its rows (leading `run_id`
  groups one sweep, `run_ts` stamps each row), so before/after comparisons
  need no git archaeology — and the history itself is versioned. `--fresh`
  resets it; `--history-csv` relocates it (the worktree baseline capture
  uses this).
- Scratch under `test_output/profiling/` (ignored via `*_output`):
  - `<spec>/<spec>.prof` — cProfile binary; inspect with
    `python -m pstats "<spec>.prof"` (`cum` sort) or
    `uv run --with snakeviz snakeviz "<spec>.prof"`.
  - `<spec>/<spec>.top.txt` — top-N cumulative functions (pre-rendered).
  - `<spec>/<spec>.mem.txt` — tracemalloc peak + top allocation sites.
  - `<spec>/run.log` — full pipeline log for the run.

For sampling-based profiling (flamegraphs, C-level visibility inside
matplotlib/numpy), compose with external samplers without adding deps:

```sh
uv run --with py-spy py-spy record -o /tmp/profile.svg -- \
    python scripts/profile_generate.py --specs "<spec>" --no-profile
uv run --with memray memray run --output /tmp/run.bin \
    scripts/profile_generate.py --specs "<spec>" --no-profile
```

## Comparing across optimization work

`summary.csv` accumulates every sweep, so the trend is one command away:

```sh
# One PNG per numeric metric (wall/cpu/memory/every stage): x = sweep
# iteration (oldest first), one line per job spec.
uv run python scripts/plot_profiling_history.py
uv run python scripts/plot_profiling_history.py --metrics wall_s,cpu_s --log-y
uv run python scripts/plot_profiling_history.py --show
```

PNGs land in `test_output/profiling/plots/`. Matplotlib is dev-side (root
§5), so a matplotlib-free machine gets a `uv run --extra plotting` hint
instead of a traceback. A pre-fix baseline row set can be re-measured with
a worktree at the pre-optimization commit (`git worktree add` + that tree's
`scripts/profile_generate.py`), then merged into the history so the plots
start from the true before-state; iteration order follows the code state
(the baseline rows carry the pre-fix commit's date, not the later
measurement date).

## Benchmark set

Five specs covering the pipeline's distinct hot paths
(`BENCHMARK_SPECS` in the script):

| Spec | Hot paths exercised |
|---|---|
| `2026-07-22 sunwest.yaml` | rounded-rect boundary, replacement text file, edge holes, PLT font |
| `2026-10-07 coolsys 1 big label.yaml` | line-content reflow (max_lines 8), global h-compress, full markup (`<f> <sc> <scale> <b> <u> <i> <sup> <sub>`), corner holes |
| `2026-10-15 coolsys round tags target 910 and walmart 2206.yaml` | round labels → shapely ellipse packing, small cutter, `dino sc` small-caps |
| `2026-10-06 seer intrachunk opt TTF.yaml` | TTF renderer (matplotlib TextPath) + intra-chunk optimization |
| `2026-10-06 seer wb-uv.yaml` | multi-material packing (wb + uv), per-material plate pools |

## Measured results (2026-10-17, `--repeats 3`, plots off, ensemble optimize on)

Historical pre-fix baseline; see "Bottleneck fixes" below for the
post-fix re-run and "Current bottlenecks" for the latest full-sweep
re-profile.

Machine: macOS dev box (Apple Silicon, Python 3.11). Wall = median of 3;
stage seconds = sums of the last timed run. `cpu_s` is process CPU
(wall >> cpu ⇒ subprocess-parallel work, not single-core compute).

| Spec | wall s | cpu s | peak mem | dominant stage(s) |
|---|---|---|---|---|
| 2026-07-22 sunwest | 11.6 | 1.4 | 21 MiB | `optimize_layer` 10.5 (ensemble pool) |
| 2026-10-07 coolsys 1 big label | 68.7 | 68.2 | 26 MiB | **`reflow` 66.7** |
| 2026-10-15 coolsys round tags 910+walmart | 51.0 | 49.9 | 31 MiB | **`layout_render_pack` 49.6** |
| 2026-10-06 seer intrachunk opt TTF | 0.3 | 0.1 | 1 MiB | `optimize_layer` 0.2 |
| 2026-10-06 seer wb-uv | 5.8 | 5.7 | 9 MiB | `cutter_downsize` 3.6 |

### Bottleneck #1: descender-relief ink gaps (the `reflow`/render path) — FIXED 2026-10-18

The coolsys big-label profile is one function deep:
`line_content._reflow_label` → probe `render_label_to_plt` →
`label_renderer._apply_descender_relief` →
`descender_adjust.colliding_partners` → `geometry.ink_gap` →
`polyline_gap` → **`geometry.segment_gap` — 125,695,509 calls,
~140 s cumulative** (pure-Python per-segment pair loop; profiled seconds
are inflated by cProfile's per-call overhead — ~2.6× on this job, real
wall 69 s — the call COUNT is the reliable signal). Every reflow
probe render re-runs the relief's O(lines × partners × segments²) gap
scan over the huge label's long lines. Memory is a non-issue everywhere
(≤ 31 MiB traced peak).

### Bottleneck #2: ellipse-packing grid search (shapely) — FIXED 2026-10-18

The round-tags job spends ~80 s (profiled; real wall 51 s) in
`packing/ellipse_packer.py::_find_best_position`: **4.34 M
`shapely.affinity.translate` calls** — the candidate scan re-translates
every placed polygon per candidate position instead of testing against
pre-translated (or bbox-pruned) geometry.

### Bottleneck #3: ensemble wall time on small jobs — PARTLY FIXED 2026-10-18

sunwest's 10.5 s `optimize_layer` is process-pool wall time (cpu 1.4 s):
the ParallelEnsemble spawns strategies per layer, so small jobs pay
fixed spawn cost. `--fast-mode` isolates it; this is a knob, not a code
hot spot.

**Follow-up measurement corrected the diagnosis:** sunwest's layers are
40/60/481 blocks, and the 10.5 s is the **481-block layer's real parallel
compute** (pooled n=481 ≈ 10 s; sequential would be ~35 s), not spawn
overhead. The spawn cost is real for genuinely small layers (1–48 blocks)
and on the Windows spawn context (seconds per worker boot), where the
sequential path (`ENSEMBLE_SEQUENTIAL_BLOCK_THRESHOLD`,
`core/optimizer.py`) now skips the pool entirely. The big-layer compute
stays parallel — that part is a knob (`--fast-mode`), not a hot spot.

## Bottleneck fixes (2026-10-18, measured re-run, `--repeats 3`, plots off)

Same box, same harness. Post-fix medians:

| Spec | before wall s | after wall s | dominant stage after |
|---|---|---|---|
| 2026-07-22 sunwest | 11.6 | 11.6 | `optimize_layer` 10.6 (481-block layer, real compute) |
| 2026-10-07 coolsys 1 big label | 68.7 | **3.6** | `reflow` 2.1 |
| 2026-10-15 coolsys round tags 910+walmart | 51.0 | **2.7** | `optimize_layer` 1.3 / `layout_render_pack` 1.3 |
| 2026-10-06 seer intrachunk opt TTF | 0.3 | 0.04 | — |
| 2026-10-06 seer wb-uv | 5.8 | 6.1 | `cutter_downsize` 3.9 (untouched by these fixes; see Current #1) |

What fixed what:

1. **`reflow` 66.7 s → 2.1 s.** (a) Exact AABB pre-filters in
   `generate/geometry.py` (`bbox_gap` lower bound prunes `polyline_gap`
   segment pairs and `ink_gap` polyline pairs) and
   `descender_adjust.colliding_partners` (per-partner ink-bbox skip) —
   bit-identical distances, pinned by randomized brute-force equivalence
   tests. (b) The reflow scale probe (`line_content._default_scale_probe`)
   renders a relief-disabled clone: the probe consumes only
   `compression_by_line`, which the X phase computes before the Y-only
   relief runs (pinned by `TestReliefFreeScaleProbeEquivalence`).
   `geometry.py` deliberately stays numpy-free: per-op numpy overhead
   loses to scalar at glyph-sized arrays; the pre-filters are the exact
   wholesale win.
2. **`layout_render_pack` 49.6 s → 1.3 s.** `ellipse_packer._find_best_position`
   now breaks the inner (secondary) scan at the first feasible candidate of
   a column (the first hit is the column's minimum), and the collision union
   is replaced by a `_PlacementIndex`: inscribed/circumcircle center-distance
   bounds (numpy, batched per column) PROVE collision/disjointness for
   nearly every candidate, with the thin band falling through to exact
   per-polygon `intersects` (tangency semantics unchanged). No per-placement
   `unary_union` rebuild. All `test_ellipse_packer` /
   `TestRoundLabelPacking` pins pass unchanged; emitted PLTs byte-identical.
3. **Small ensemble layers** run in-process (`ENSEMBLE_SEQUENTIAL_BLOCK_THRESHOLD = 48`,
   measured macOS crossover; Windows crossover is far higher). Large layers
   stay pooled.
4. **Determinism:** the ParallelEnsemble winner tie race is fixed (see the
   `-O` section below) — winner selection is now a canonical total order
   (improvement, distance, strategy name, job terminals), independent of
   worker completion order. Post-fix verification: all 13 benchmark PLTs
   match the pre-fix baseline byte-for-byte (8 files) or with identical
   segment multisets and order-only diffs (5 files, the tie-break's intended
   effect); coolsys + sunwest + round-tags `--fast-mode` runs are
   byte-identical across repeated runs.

**Remaining determinism gap (open, follow-up):** SA/GA use the unseeded
global `random`, so on specs where a stochastic strategy wins (round-tags
`bh`/`text`), the winning tour — and therefore the emitted stroke order —
varies run to run (`--fast-mode` runs are byte-identical, proving the RNG
is the source). Seeding each strategy job deterministically (e.g. from a
hash of the serialized blocks + job payload) is the remaining step toward
the identical-input-identical-output invariant.

## Current bottlenecks (2026-10-19, post-shift-memo re-profile, full sweep)

Full sweep after the fixes below (`--repeats 3` + cProfile + tracemalloc,
plots off). Wall = median of 3; profile seconds are cProfile cumulative
(inflated ~1.5-2.6x on pure-Python hot loops — call counts are the
reliable signal). Memory is a non-issue everywhere (peak 1.0-31.7 MiB).

| Spec | pre-cache | post-cache | post-relief-memo | post-shift-memo | dominant stage(s) after |
|---|---|---|---|---|---|
| 2026-07-22 sunwest | 11.2 | 2.08 | 2.08 | **1.99** | `optimize_layer` 1.2 (481-block layer, post-IH-cache) |
| 2026-10-06 seer wb-uv | 3.1 | 2.27 | 2.27 | **2.20** | `cutter_downsize` 1.3 + `layout_render_pack` 0.62 |
| 2026-10-07 coolsys 1 big label | 2.6 | 2.57 | 2.57 | **2.61** | `reflow` 1.7 |
| 2026-10-15 coolsys round tags 910+walmart | 2.4 | 1.83 | 1.83 | **1.90** | `layout_render_pack` 0.98 |
| 2026-10-06 seer intrachunk opt TTF | 0.03 | 0.03 | 0.03 | **0.03** | — |

TOTAL (median-of-3, shift-memo sweep `72e2e650` → `6faa75cd`):
**8.77 → 8.73 s (-0.5 %, inside the noise band)** — the patched leaf is
provably gone (wb-uv `_shifted_boxes` 0.41 → 0.055 s profiled, 96 %
memo hits; coolsys 0.093 → 0.009 s), but the relief's remaining leaves
(grid builds + gap measurement, Current #1) dominate its cost, so the
wall gain is small. The previous cycle (relief memo, `a5d6d429` →
`72e2e650`) measured **10.25 → 8.77 s (-14.5 %)**; wb-uv **-32.7 %**
(`cutter_downsize`
2.09 → 1.32), round tags -9.1 %, coolsys -8.0 %. sunwest's 11.2 → 2.08
across the last two cycles is the optimize-side IH incremental-insertion
cache (its 481-block layer 10.5 → 1.3 s), not this patch — the relief
memo is noise-level there (+1.7 %).

### Current #1: the relief's grid builds + per-glyph gap measurement (wb-uv, coolsys)

Post-shift-memo the relief is still the top render cost (wb-uv **1.93 s
profiled of the 4.7 s run**, coolsys 0.73 s across 11 renders), and its
two remaining leaves are:

- **`InkGrid.__init__`: 365 cold constructions / 0.60 s profiled** (272 k
  `_cells_covered` + 667 k `_segment_span`). The `lru_cache(16)` never
  hits on wb-uv — every sweep step's X-compressed line ink is a fresh
  key, so the working set exceeds the bound. The exact next win: key the
  grid on the LOCAL ink + the X scale (a scaled-ink grid is the local
  grid scaled — the cell math maps exactly), or index the line's segments
  once per (block, cell) and transform queries, not data.
- **`measure_glyph_gaps`: 2,241 calls / 0.66 s** (8,313 `min_gap` /
  0.30 s; the bisection's per-candidate `ramp_ink` re-translates the
  glyph's ink every step — 682 k `LineSeg.translate` + 600 k
  `LineSeg.bounds` profiled run-wide). The per-glyph segment tuples are
  frozen between candidates (only the Y transform changes) — the same
  memo shape again.

Same rule: exact memoization/pruning, not heuristics.

### Current #2: sunwest's 481-block bh layer (real compute, knob not bug)

The IH incremental-insertion cache (optimize pipeline, 2026-10-18) cut
this layer's stage from 10.5 s to ~1.3 s wall; what remains is the
strategies' real parallel compute on the pooled 481-block layer
(sequential is ~3.5x worse). `--fast-mode` isolates it. Improving THIS
means cheaper big-instance strategies (e.g. capping 2-opt passes by block
count), not plumbing.

### Current #3: coolsys `reflow` residual (no longer pathological)

1.6 s = 8 probe renders (0.45 s each) of the 24x16in label; inside each,
`_walk_line` (1.8 s profiled across 410 calls) + the relief (1.6 s
profiled across 11 renders, down from ~140 s) + kerning (0.9 s). The
relief pre-filters made it linear-ish and the bounds memo removed the
leaf recompute; further wins come from caching per-glyph ink between
probe renders (the word partition changes line breaks, not glyph geometry).

### Ruled out

- **Ellipse packing is no longer a hot spot**: round-tags packing is now
  ~0.5 s profiled (~0.2 s real, was ~50 s); the spec's
  `layout_render_pack` 1.0 s is the 83 `render_label_to_plt` renders.
- **Ensemble pool spawn** on the benchmark set: the small layers
  (1-48 blocks) run sequentially; the 146/166-block round-tags layers
  pool in 1.1 s profiled total — spawn is amortized there.
- **Memory**: peak <= 31 MiB on every spec, unchanged.

## Python optimized mode (`-O`) — verdict: **no benefit, safe, not adopted**

Static: the package contains exactly ONE `assert` (the module-level
1016-units invariant in `core/units.py`, import-time), zero `__debug__`
guards, and zero runtime docstring inspection (pydantic descriptions come
from `Field(description=)`), so `-O` strips no meaningful validation —
and nothing measurable to gain. Measured (`--ab --repeats 3`, median
wall per cell, PLT outputs sha256-verified byte-identical at both
levels):

| Spec | -O0 median s | -O1 median s | delta | PLT byte-identical |
|---|---|---|---|---|
| 2026-07-22 sunwest | 12.05 | 12.11 | +0.5% | see note |
| 2026-10-07 coolsys 1 big label | 69.03 | 69.01 | -0.0% | yes |
| 2026-10-15 coolsys round tags 910+walmart | 49.34 | 48.92 | -0.8% | see note |
| 2026-10-06 seer intrachunk opt TTF | 0.71 | 0.72 | +2.2% | yes |
| 2026-10-06 seer wb-uv | 6.29 | 6.29 | +0.1% | yes |

Every delta is within run-to-run noise (the harness's own median-of-3
spread on these specs is ±1–3 s). **`-O` yields no meaningful speedup** —
exactly as the static analysis predicts: the hot paths are library calls
(shapely, matplotlib) and the pure-Python loops that dominate
(`segment_gap`, `_find_best_position`) contain no asserts for `-O` to
strip.

**The two "MISMATCH" rows are NOT an `-O` effect.** Re-running sunwest
twice at the *same* `-O0` reproduces the byte difference. The emitted
engraving is identical; only the toolpath **order** changes:

- sunwest text layer: stroke **multiset identical** (16,228 commands, the
  PD set compares equal) — pure stroke-order difference.
- round-tags `bh` layer: same (PD multiset identical, order differs).
- round-tags `text` layer: identical stroke count (34,335) and identical
  total point count (68,670); the first 31,252 strokes match, then 16
  strokes re-merge at the tail — order-dependent **coincident-stroke
  merging** (adjacent strokes sharing an endpoint fuse into one polyline,
  so a routing-order change moves the merge boundaries). Same engraving.

Root cause is a pre-existing **ParallelEnsemble winner tie race**: worker
results were appended in `as_completed` (wall-clock) order and
`optimizer._select_best_result` broke exact-distance ties toward the
first-arriving job, so a layer whose strategies tie on travel distance
routed through a timing-dependent winner. `-O` does not change output
(the three byte-identical specs prove it); it only shifts process timing
enough to flip which tied worker lands first. This tie race is a
**determinism bug** (identical input must engrave identical output —
root AGENTS.md) and is independent of this profiling work.
**Fixed 2026-10-18:** `StrategyBenchmarkResult` carries its job's
start/end terminals and selection uses a canonical total-order key
(improvement desc, distance asc, strategy name, terminals — never
execution time), so the winner and the benchmark table are pure functions
of the results. The remaining run-to-run byte differences on
SA/GA-winning specs are the unseeded-RNG gap documented above, not ties.

`-OO` (docstring stripping) was rejected outright: it strips
matplotlib/pydantic docstrings consumed by `inspect.signature`-style
library paths, for zero measured gain. The PyInstaller spec keeps
`optimize=0`. The A/B harness stays in the repo
(`scripts/profile_generate.py --ab`) to re-verify on future Python
upgrades.

# Optimize-Pipeline Profiling

How to measure where the **optimize** pipeline (the `plt-optimizer optimize`
CLI / `watch` daemon core: parse → profile → preprocess → chunk →
optimize+reassemble → write) spends time and memory, what its benchmark set
covers, and its measured baseline. The two skills carry the procedure:
`.github/skills/optimize-pipeline-profiling/SKILL.md` (mechanics) and
`.github/skills/optimize-pipeline-optimization/SKILL.md` (the
measure → identify → patch → re-measure → report-gain cycle).

Harness: [`../scripts/profile_optimize.py`](../scripts/profile_optimize.py)
(stdlib only: `cProfile` + `tracemalloc` + `time.perf_counter` — no new
dependencies). It runs the exact production sequence of
`plt_optimizer.cli.optimize.run` for each PLT, and records the pipeline's
own quality KPIs per sweep (`rapid_before_in`, `rapid_after_in`,
`improvement_pct` — emitted rapid travel in inches) so a perf change can
also be checked for routing-quality drift.

## Stage timers

The same `plt_optimizer/utils/stage_timing.py` contract powers both
pipelines (it moved from `generate/` to `utils/` — the shared, Win7-safe
substrate — when the core pipeline adopted it). The `optimize` CLI emits
`parse`, `profile`, `preprocess`, `chunk`, `optimize`, `write`; the core
pipeline helper emits its inner stages when called with
`stage_timers=True` (the CLI and the harness do; the `watch` daemon and
generate's plate-space path keep the default off so their log streams stay
byte-identical): `optimize_tsp`, `direction_sweep`, `intra_sweep`,
`reassemble`, `merge`. `optimize` nests the inner five (the harness sums
each name independently). Pinned by
`tests/test_pipeline.py::TestStageTimers` (emission, default-off,
log-only-identity). The lines land in the normal log stream (INFO), so
they are visible in `logs_optimize/optimizer.log` for any production
`optimize` run, not only under the profiler.

## Usage

```sh
# Full benchmark sweep (2 PLTs x 3 timed repeats + cProfile + tracemalloc)
uv run python scripts/profile_optimize.py

# Custom inputs (files or directories of *.plt), quick mode
uv run python scripts/profile_optimize.py --plts "examples/my file.plt" \
    --repeats 1 --no-profile --no-memory

# Isolate the ensemble pool-spawn cost
uv run python scripts/profile_optimize.py --fast-mode
```

Outputs:

- `profiling/optimization_pipeline_summary.csv` — **tracked** (commit it
  with each perf cycle), adjacent to the generate pipeline's
  `summary.csv` — one append-mode history per pipeline, never mixed. Same
  `run_id`/`run_ts` semantics; `--fresh` resets; `--history-csv` relocates
  (the worktree baseline capture uses this).
- Scratch under `test_output/optimize_profiling/` (ignored via
  `*_output`): `<file>/<file>.prof`, `.top.txt`, `.mem.txt`, `run.log`.

Plotting reuses `scripts/plot_profiling_history.py` with the CSV/plot-dir
pointed at this pipeline:

```sh
uv run python scripts/plot_profiling_history.py \
    --csv profiling/optimization_pipeline_summary.csv \
    --out-dir test_output/optimize_profiling/plots --metrics wall_s,cpu_s --log-y
```

Pre-optimization baselines:
`.github/skills/optimize-pipeline-profiling/scripts/baseline_capture.py`
(worktree capture + commit-date-stamped merge, same recipe as the
generate side). Gain scoreboard between two sweeps:
`.github/skills/optimize-pipeline-optimization/scripts/iteration_report.py`.

## Benchmark set

Two files (`BENCHMARK_PLTS` in the script) covering the pipeline's two
preprocessing branches:

| File | Hot paths exercised |
|---|---|
| `examples/334X712 RED DANGER conveyers start and stop2.plt` | text branch: chronological chunking of contiguous glyph paths, ensemble TSP (sequential path on this box), direction sweep |
| `tests_deps/1x3 half inch letters holes1.plt` | structural branch: fracture + dedupe preprocessing, one-block-per-segment chunking (drill holes + borders), pooled ensemble |

The structural fixture lives in `tests_deps/` (frozen, `tests/AGENTS.md`):
benchmark input, never a file to edit.

## Measured baseline (2026-10-18, `--repeats 3`, ensemble on)

Machine: macOS dev box (Apple Silicon, Python 3.11). Wall = median of 3;
stage seconds = sums of the last timed run. `cpu_s` is process CPU
(wall >> cpu ⇒ subprocess-parallel work, not single-core compute).

| File | wall s | cpu s | peak mem | rapid in→out (in) | imp% | dominant stage(s) |
|---|---|---|---|---|---|---|
| 334X712 RED DANGER (text) | 0.19 | 0.19 | 2.8 MiB | 341.4 → 208.4 | 39.0 | `optimize_tsp` 0.16 (sequential ensemble) |
| 1x3 half inch letters holes1 (structural) | 0.91 | 0.04 | 2.4 MiB | 554.8 → 134.2 | 75.8 | `optimize_tsp` 0.90 (pooled ensemble) |

### Current #1: Genetic Algorithm job on the structural fixture (a knob)

Post-fix the structural fixture's 0.35 s wall is ~0.12 s pool spawn + the
0.21 s **GA job** (the pool's critical path; SA 0.06 s, IH 0.04 s). That is
real parallel compute on a 194-block layer — the documented stop condition.
It is a knob: `--fast-mode` (NN2Opt, no ensemble) isolates it,
`ENSEMBLE_SEQUENTIAL_BLOCK_THRESHOLD` moves the sequential/pool crossover.
Further exact wins here need to shrink GA's own search budget, which is a
routing-quality trade-off, not a perf fix.
