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
(`plt_optimizer/generate/stage_timing.py`, format `stage <name>: <s>s`,
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

## Current bottlenecks (2026-10-18, post-sweep-cache re-profile, full sweep)

Full sweep after the fixes below (`--repeats 3` + cProfile + tracemalloc,
plots off). Wall = median of 3; profile seconds are cProfile cumulative
(inflated ~1.5-2.6x on pure-Python hot loops — call counts are the
reliable signal). Memory is a non-issue everywhere (peak 1.3-30.7 MiB).

| Spec | wall s (pre-memo) | wall s (pre-cache) | wall s (post-cache) | dominant stage(s) after |
|---|---|---|---|---|
| 2026-07-22 sunwest | 11.5 | 11.2 | 11.2 | `optimize_layer` 10.5 (481-block bh layer) |
| 2026-10-06 seer wb-uv | 5.7 | 4.6 | **3.1** | `cutter_downsize` 1.9 + `layout_render_pack` 0.9 |
| 2026-10-07 coolsys 1 big label | 3.4 | 2.7 | **2.6** | `reflow` 1.4 |
| 2026-10-15 coolsys round tags 910+walmart | 2.6 | 2.5 | 2.4 | `optimize_layer` 1.3 / `layout_render_pack` 1.0 |
| 2026-10-06 seer intrachunk opt TTF | 0.04 | 0.04 | 0.03 | — |

### Bottleneck: `TextBlock.bounds` recompute (the universal render leaf) — FIXED 2026-10-18

The sweep's top cost center on every render-heavy spec was
`text_geometry.TextBlock.bounds` recomputing the union of its strokes'
swept bounds on EVERY call — the frozen dataclass cached nothing, and the
render pipeline calls it per line, per pass, per probe. wb-uv: **25,894
calls / 4.7 s cumulative** (via 90,477 `Stroke.bounds` + 116 k segment
bounds); coolsys: **7,660 calls / 3.2 s = 36 % of the whole profiled
run**.

**Fix:** `Stroke` and `TextBlock` each carry a lazy union-bounds memo —
`_bounds_cache: Optional[TextBounds] = field(default=None, init=False,
compare=False, repr=False)`, computed once in `bounds()` and written with
`object.__setattr__`. Exact by construction: a frozen block's strokes
never mutate, every transform constructor builds a fresh instance whose
memo starts empty, and `init=False` means `dataclasses.replace` (which
replays only init fields) can never copy a stale memo. Empty geometry
returns `None` without caching (the empty scan is trivially cheap). The
field is excluded from equality/hashing/repr — pure derived state. The
microbenchmark at glyph sizes (300 strokes x 30 segments, 3000 query
passes): recompute 7.79 s vs memoized 0.02 s (386x); construction cost
+0.04 us/instance.

Measured (same box, full sweep): coolsys `bounds` 3.2 s -> **0.87 s**
profiled (7,660 calls, only 392 first computes — 95 % memo hits); wb-uv's
leaf vanishes from the top-25 entirely (was 4.8 s), total profiled calls
91.5 M -> 60.9 M. Wall: coolsys **3.4 -> 2.7 s**, wb-uv **5.7 -> 4.6 s**.
Identity: all 13 benchmark PLTs byte-identical under `--fast-mode`; in
full mode 10/13 byte-identical and the 3 diffs (sunwest `txt`,
round-tags `bh`/`text` — exactly the unseeded-SA/GA layers of the open
determinism gap) have **identical undirected segment multisets**
(15,918 / 13,529 / 35,777 segments, verified against a HEAD worktree).
Pinned by `TestStrokeBoundsMemoization` + the block-level memo tests in
`tests/test_text_geometry.py` (memo object identity, randomized
brute-force equivalence, empty-never-cached, every transform constructor
uncached, `replace()` never carries a memo, eq/hash/repr invariance).

### Bottleneck: the Phase-3 sweep re-walks the glyphs per scale step (wb-uv) — FIXED 2026-10-18

wb-uv's 2.9 s `cutter_downsize` stage was 14 full `render_label_to_plt`
probes (`cutter_downsize._default_probe`, 7.6 s profiled), and inside
those renders the collision Phase-3 sweep
(`detection.resolve_via_compression`, 18 calls / 8.4 s profiled) re-ran a
full `_render_text_local_with_bounds` per scale step — **144 renders**
whose first-pass glyph walk is byte-identical every step: the sweep
changes ONLY the label-level `collision_compress_by_line` map, and
`_render_line_block` depends on nothing else. The profile proved it: 577
`_render_line_block` calls (~500 of them sweep steps, 3.9 s profiled).

**Fix:** `render_label_to_plt` owns one `_LineBlockCache` per
collision-resolution sequence — the initial render, the Phase-2
re-render, every sweep step, the verification render, and the
diagnostics render all share it — threaded through `_render_label_once` →
`_render_text_lines_by_pen` → `_render_positioned_lines`. Keyed by
`(line, chunk_mode, check_glyph_coverage)`; the line is a frozen
hashable dataclass, so a content clone can never hit a stale entry.
Values are the RAW pre-compression blocks (frozen `TextBlock`s, shared
safely — the per-step X scale is applied downstream on every render
exactly as before); word-group lists are copied in and out because
downstream code mutates them. Exact by construction: the cache serves
precisely the renders the sweep already proves identical.

Measured (same box, full sweep): wb-uv profiled calls 60.9 M -> **47.1 M**,
11.6 s -> 7.8 s; `_render_line_block` vanishes from the top-30 entirely.
Wall: wb-uv **4.6 -> 3.1 s** (`cutter_downsize` 2.9 -> 1.9 s), coolsys
**2.7 -> 2.6 s**, round-tags 2.5 -> 2.4 s, TOTAL 21.0 -> 19.3 s. The
sweep's remaining per-step cost is the positioning/relief/collision-check
that legitimately changes with the X scale. Identity: 11/13 benchmark
PLTs byte-identical; the 2 diffs (round-tags `bh`/`text` — the unseeded
SA/GA layers of the open determinism gap) compare **equal as undirected
segment + arc multisets** (13,114/332 and 34,048/1,442) — order-only.
Pinned by `tests/test_line_block_cache.py` (full-render bit-identity vs
a null-cache sequence across resolving, Phase-2+3, unresolvable, clean,
WORD-mode, markup, and multi-line labels; zero re-walks across a
resolving sweep; pre-compression values; key isolation; the defensive
copy).

### Current #1: the descender relief inside every collision-sequence render (wb-uv)

wb-uv's remaining 1.9 s `cutter_downsize` is now dominated by
`_apply_descender_relief` (**183 calls / 5.2 s profiled**, via 2,241
`measure_glyph_gaps` / 2.4 s): the relief re-runs on every render of
every probe, and the sweep's 144 steps each re-measure glyph gaps whose
Y-geometry is unchanged (compression is X-only). The X scale DOES change
the overlapping-X spans the relief compares, so caching the relief result
is not exact; the win is per-glyph ink caching between renders (the ink
boxes depend only on (line, mode, coverage) — `measure_glyph_gaps`
rebuilds them per render) plus pruning gap pairs beyond the required
clearance. Same shape as the reflow-probe fix: exact pre-filters, not
heuristics.

### Current #2: sunwest's 481-block bh layer (real compute, knob not bug)

Confirmed by the profile: 10.1 s of the 10.5 s stage is the parent
blocking in `as_completed` on the pooled 481-block layer (the strategies'
real parallel compute; sequential is ~3.5x worse). `--fast-mode` isolates
it. Improving THIS means cheaper big-instance strategies (e.g. capping
2-opt passes by block count), not plumbing.

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
