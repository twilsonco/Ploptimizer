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

Outputs under `test_output/profiling/`:

- `summary.csv` — per spec: median wall/cpu, peak traced MiB, summed
  seconds per stage.
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

### Bottleneck #1: descender-relief ink gaps (the `reflow`/render path)

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

### Bottleneck #2: ellipse-packing grid search (shapely)

The round-tags job spends ~80 s (profiled; real wall 51 s) in
`packing/ellipse_packer.py::_find_best_position`: **4.34 M
`shapely.affinity.translate` calls** — the candidate scan re-translates
every placed polygon per candidate position instead of testing against
pre-translated (or bbox-pruned) geometry.

### Bottleneck #3: ensemble wall time on small jobs

sunwest's 10.5 s `optimize_layer` is process-pool wall time (cpu 1.4 s):
the ParallelEnsemble spawns strategies per layer, so small jobs pay
fixed spawn cost. `--fast-mode` isolates it; this is a knob, not a code
hot spot.

### Python optimized mode (`-O`) — verdict: **no benefit, safe, not adopted**

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
results are appended in `as_completed` (wall-clock) order and
`optimizer._select_best_result` breaks exact-distance ties toward the
first-arriving job, so a layer whose strategies tie on travel distance
routes through a timing-dependent winner. `-O` does not change output
(the three byte-identical specs prove it); it only shifts process timing
enough to flip which tied worker lands first. This tie race is a
**determinism bug** (identical input must engrave identical output —
root AGENTS.md) tracked for a follow-up fix (a stable tie-break: sort
benchmarks deterministically, tie-break by strategy name) and is
independent of this profiling work.

`-OO` (docstring stripping) was rejected outright: it strips
matplotlib/pydantic docstrings consumed by `inspect.signature`-style
library paths, for zero measured gain. The PyInstaller spec keeps
`optimize=0`. The A/B harness stays in the repo
(`scripts/profile_generate.py --ab`) to re-verify on future Python
upgrades.
