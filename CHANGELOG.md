# Changelog

## [Unreleased]

### Features

* **fonts:** `font_showcase.py` moved from `scripts/` to `fonts/`; showcases are now chrome-free **JPGs** in the git-tracked `fonts/showcases/` directory
  - `plot_plt_document` gained `show_axes` / `show_title` flags (both default `True`, every existing caller unchanged); the showcase passes `show_axes=False, show_title=False, gridlines=False`, so each image carries only the engraved geometry - the font name is engraved in the body, so the matplotlib plot title (and the `showcase_pdf_title`/`mapping_pdf_title` helpers) is gone
  - `save_figure` learned `.jpg`/`.jpeg` (200 dpi, flattened onto white, quality 92); the writers are `write_showcase_image` / `write_mapping_image`
  - Symbol-font mapping showcases render their font-name header in the readable default (alpha) face via the new `mapping_header_blocks` - previously the name was engraved in the symbol font and read as gibberish; the grid's symbol slots still sample the font itself
  - The generated JPGs are committed (regenerate after any font-library change with `uv run python fonts/font_showcase.py`); the old `test_output/font_showcase/` PDFs are removed

* **fonts:** symbol (non-alphanumeric) PLT fonts declared by a trailing `_sym`/`_symbol` sample-sheet suffix, stored as the catalog's `symbol_font` bool — this **replaces** `fonts/VP-EL-fonts/non_text_fonts.txt` (deleted) as the sole symbol-font source of truth
  - `wingdings_0.35_^_S4_sym.plt` → `"symbol_font": true`; **every** catalog file carries the key (`false` for a letter font), so the flag is self-describing with the font
  - `parse_font_file_name` strips the suffix in a loop with `_S<n>` (either order parses, canonical `_S<n>_sym`) before the name split, so a font *named* "sym ..." is never mangled; matching is case-insensitive
  - The flag drives every exemption the txt list used to: derived-Unicode glyph creation, the synthetic `<base> SC` small-caps variant, the showcase's Unicode line, and the inline `<sc>` tag's inertness on symbol fonts (`font_registry.plt_font_is_symbol` is the single lookup)
  - Lenient parse mirrors `stroke_count`: a missing key loads as `false`, a non-boolean logs a WARNING; the `non_text_file` test-override kwargs are gone from `font_name_choices`/`resolve_font`/`normalize_font_name`
  - `wingdings_0.35_^_S4.plt` renamed to `..._S4_sym.plt` and the catalog regenerated — all 14 fonts' `characters` blocks verified byte-identical, only Wingdings' `file_path` moves

* **generate:** rounded-rectangle label borders (`rounded_rectangle_fraction` + `rounded_rectangle_proportional`)
  - Two cascading fields (label → job, plate parity, `job-config.json` keys; defaults `0.0` / `false`) round the rectangular border's corners; `0.0` keeps the sharp rectangle bit-identically
  - Corners are **superellipse-quadrant patches** with exponent `n = 2 / fraction`, so the curvature varies **continuously** along the whole boundary (G2) — no circular-fillet curvature jump where the rounding begins/ends
  - `proportional: false` (default) = one identical rounding distance on every side from the shorter edge (a 4x8 label at `0.5` rounds 2in of the 4in sides); `true` = the fraction applies to each dimension
  - Exact endpoints: `1.0` + proportional (or a square) engraves the exact ellipse/circle of `type: round` (bit-identical border); `1.0` + non-proportional + non-square is a smoothed stadium (short edges fully rounded, long edges straight, corner exponent clamped to 4 for G2)
  - Corner drill holes ride the patch normal (generalized `_round_corner_hole`); the text margin applies to the engraved rounded boundary (corner-aware content inset); the descender relief's bottom border follows the lower arc
  - A border change, never a packing change: rounded labels keep `shape_type: rect` and pack through rectpack in their exact nominal box

### Performance

* **generate:** LRU memo on the relief's stacked-frame box shift (`label_renderer._shifted_boxes`) — the fixpoint passes re-shifted byte-identical glyph boxes onto byte-identical baselines every pass; wb-uv `_shifted_boxes` **0.41s → 0.055s** profiled (96% hits), coolsys **0.093s → 0.009s** (median-of-3, same box; `docs/PROFILING.md` "Current bottlenecks")
  - **Root cause**: the descender relief's top remaining render leaf. Per fixpoint pass `stacked_boxes_of`/`_shifted_boxes` rebuilt every line's stacked ink (wb-uv 1,674 calls / 0.41s profiled, `dataclasses.replace` + full polyline translation), and the baseline table only moves when a pass actually grows a spacing — so consecutive passes and the final measurement re-shift identical boxes onto identical baselines
  - **Solution**: `lru_cache(64)` on `(tuple(boxes), baseline_y)` — `GlyphBox` is frozen and hashable, the shifted results are read-only (no call site mutates them), a fresh LIST is returned per call so callers own their result, and the `baseline_y == 0.0` identity fast path stays uncached. Exact by construction; the same memo shape as `_glyph_boxes`/`ink_grid_for`
  - **Measured**: wb-uv profiled calls 25.7M → 24.3M, relief 2.24s → 1.93s. Wall TOTAL 8.77 → 8.73s (inside the noise band — the leaf is provably gone, the relief's grid-build + gap-measurement leaves now dominate; recorded as the next cycle's #1)
  - **Verification**: 8/13 benchmark PLTs byte-identical; the 5 diffs (sunwest `bh`/`txt`, wb-uv `bh`, round-tags `bh`/`text`) are exactly the unseeded SA/GA layers of the documented open determinism gap, with **identical undirected segment + arc multisets** (10,837/800, 15,918/0, 35/56, 13,197/332, 34,335/1,442) vs a pre-fix worktree at `e6d6f3a` — order-only
  - **Regression testing**: `TestShiftedBoxesMemoization` in `tests/test_relief_memoization.py` — identity hits, fresh-list-per-call, baseline/box-content key isolation, zero-baseline bypass, list mutation cannot poison the memo, randomized brute-force equivalence vs a plain per-box shift, LRU eviction bounds, list-key coercion

* **generate:** LRU memos on the descender relief's two hot leaves (`geometry.ink_grid_for` + `label_renderer._glyph_boxes`) — the collision-sweep re-measured identical line ink on every pass/step; benchmark-set TOTAL **10.25s → 8.77s** (-14.5%), wb-uv **3.37s → 2.27s** (-32.7%, `cutter_downsize` 2.09 → 1.32) (median-of-3, same box; `docs/PROFILING.md` "Current bottlenecks")
  - **Root cause**: the relief's leaves were pure functions of hashable inputs recomputed thousands of times. `InkGrid.__init__`: 2,616 constructions / 2.9s profiled (1.4M `_cells_covered`) — every fixpoint pass re-indexed the unchanged lines, every sweep step re-indexed the non-compressed lines (the sweep scales X only). `_glyph_boxes`: 2,232 calls / 1.4s — 25.8k `_stroke_polyline` (100k arc flattenings) rebuilding per-glyph ink identical between renders of the same line
  - **Solution**: `geometry.ink_grid_for(ink, cell)` shares one grid per `(ink, cell_size)` (`lru_cache(16)`, all seven construction sites converted; `InkGrid` is read-only after `__init__`, unhashable hand-built ink falls back to a fresh grid). `label_renderer._glyph_boxes` shares the frozen `GlyphBox` tuple per `(block, glyph_groups, text)` (`lru_cache(64)`, partition deep-coerced to nested tuples, fresh LIST returned per call so callers own their result). Exact by construction — both results depend on nothing else and are read-only; `TextBlock`/`Stroke` are frozen with value equality, so a re-rendered line hits its old entry
  - **Measured**: wb-uv profiled calls 47.1M → 25.7M, 7.8s → 4.9s; grid constructions 2,616 → 365, relief 5.5s → 2.2s profiled. Round tags 2.01 → 1.83, coolsys 2.79 → 2.57; sunwest +1.7% (noise — its 11.2 → 2.08 across the last two cycles is the optimize-side IH cache)
  - **Verification**: 10/13 benchmark PLTs byte-identical; the 3 diffs (sunwest `bh`/`txt`, round-tags `bh`) are exactly the unseeded SA/GA layers of the documented open determinism gap, with **identical undirected segment + arc multisets** (10,837/800, 15,918/0, 13,197/332) vs a pre-fix worktree — order-only
  - **Regression testing**: `tests/test_relief_memoization.py` — identity hits, key isolation (cell size / block content / partition / text), unhashable fallback, randomized brute-force equivalence vs cache-cleared recompute, LRU eviction bounds, full-render bit-identity on relief-firing and collision-sweep labels, zero new grid builds on a warm re-render

* **core:** O(n^2) incremental insertion cache in the cheapest-insertion heuristic (`optimizer.py`) — the optimize pipeline's first profiled win; structural benchmark **0.91s → 0.35s** wall (-62%), benchmark-set TOTAL **1.11s → 0.51s** (-53.5%) (median-of-3, same box; `docs/PROFILING.md` "Optimize-pipeline profiling")
  - **Root cause**: the profiled 0.90 s ensemble wall on the 194-block structural fixture was NOT pool spawn (a 14-worker noop-pool bench is 0.12 s) — the two Insertion Heuristic jobs (0.80 s each) set it. The cheapest-insertion loop re-scanned every (unvisited block, tour position) on every insertion: 1.2M `_calculate_insertion_cost` evaluations, O(n^3)
  - **Solution**: inserting the winner after tour position `p` replaces exactly ONE edge (old pos `p`) with two new edges (`p`, `p+1`), so every other (block, position) insertion cost is unchanged. One cached cost row per unvisited block (built from the same `_calculate_insertion_cost` calls, keeping floats bit-identical) plus each block's first-minimum under the naive scan's strict-< tie rule (`_first_min_insertion`); after each insertion only positions `p`/`p+1` are recomputed per remaining block. O(n^3) evaluations → O(n^2); IH job 0.80 s → 0.04 s
  - **Verification**: benchmark PLTs byte-identical (sha256), KPIs unchanged (39.0 / 75.81 imp%); the post-fix wall is the GA job (0.21 s) + ~0.12 s spawn — real parallel compute, the documented stop condition (knob: `--fast-mode`)
  - **Regression testing**: `tests/test_optimizer.py::TestInsertionHeuristicIncrementalEquivalence` — seeded randomized equivalence vs the verbatim naive loop (integer-grid tie storms + floats, exact tour/direction/total equality) + the frozen-fixture distance pin 137966.52784046388; `TestFirstMinInsertion` (tie → lowest index)

* **generate:** first-pass line-block cache across the collision-resolution sequence (`label_renderer.py`) — the Phase-3 compression sweep re-rendered the full text 17x per label; wb-uv **4.6s → 3.1s** wall (-33%), `cutter_downsize` **2.9s → 1.9s** (median-of-3, same box; `docs/PROFILING.md` "Current bottlenecks")
  - **Root cause**: `collision.detection.resolve_via_compression` sweeps 16 compression scales, each a full `_render_text_local_with_bounds` render, changing ONLY the label-level `collision_compress_by_line` map — so every step's first-pass glyph walk (`_render_line_block`) is byte-identical to the last. wb-uv's profile: 577 `_render_line_block` calls (~500 of them sweep steps, 3.9s profiled) inside 14 cutter-downsize probe renders; the sweep's 144 renders were 8.4s of the 11.6s profiled run
  - **Solution**: `render_label_to_plt` owns one `_LineBlockCache` per collision-resolution sequence (initial render, Phase-2 re-render, every sweep step, the verification render, the diagnostics render), threaded through `_render_label_once` → `_render_text_lines_by_pen` → `_render_positioned_lines`. Keyed by `(line, chunk_mode, check_glyph_coverage)` — the line is a frozen hashable dataclass, so a content clone can never hit a stale entry. Values are the RAW pre-compression blocks (frozen `TextBlock`s, shared safely; the per-step X scale is applied downstream on every render exactly as before); word-group lists are defensively copied in and out (downstream mutates them)
  - **Measured**: wb-uv profiled calls 60.9M → 47.1M, 11.6s → 7.8s; `_render_line_block` vanishes from the top-30 entirely; sweep steps now cost only the positioning/relief/collision-check that legitimately changes per scale
  - **Verification**: 11/13 benchmark PLTs byte-identical; the 2 diffs (round-tags `bh`/`text`) are exactly the unseeded SA/GA layers of the documented open determinism gap, with **identical undirected segment + arc multisets** (13,114/332 and 34,048/1,442) — order-only
  - **Regression testing**: `tests/test_line_block_cache.py` — full-render bit-identity vs a null-cache sequence (resolving, Phase-2+3, unresolvable, clean, WORD-mode, markup, multi-line), zero re-walks across a resolving sweep, pre-compression values (a compressed warm-up never leaks), key isolation per mode/coverage, defensive copy

* **generate:** lazy union-bounds memo on `Stroke`/`TextBlock` (`text_geometry.py`) — the sweep's top render leaf; coolsys big label **3.4s → 2.7s**, wb-uv **5.7s → 4.6s** wall (median-of-3, same box; `docs/PROFILING.md` "Current bottlenecks")
  - **Root cause**: `TextBlock.bounds`/`Stroke.bounds` recomputed the union of swept segment bounds on EVERY call — the frozen dataclasses cached nothing, and the render pipeline queries line bounds per line, per pass, per probe. coolsys: 7,660 block-bounds calls = 3.2s = 36% of the whole profiled run (50,314 stroke + 400,222 segment bounds); wb-uv: 4.8s profiled, its leaf dominated the top-25
  - **Solution**: `_bounds_cache: Optional[TextBounds] = field(default=None, init=False, compare=False, repr=False)` computed once in `bounds()` and written with `object.__setattr__`. Exact by construction: a frozen instance's strokes never mutate, every transform constructor builds a fresh instance whose memo starts empty, and `init=False` makes `dataclasses.replace` (which replays only init fields) structurally unable to copy a stale memo. Equality/hashing/repr ignore the field (pure derived state); empty geometry returns `None` without caching
  - **Measured**: coolsys `bounds` 3.2s → 0.87s profiled (95% memo hits); wb-uv's leaf vanishes from the top-25 entirely, profiled calls 91.5M → 60.9M; microbenchmark at glyph sizes 386x on repeat queries, +0.04us/instance construction
  - **Verification**: all 13 benchmark PLTs byte-identical under `--fast-mode`; full mode 10/13 byte-identical, the 3 diffs are exactly the unseeded SA/GA layers (sunwest `txt`, round-tags `bh`/`text`) with **identical undirected segment multisets** vs a HEAD worktree — order-only, the documented open determinism gap
  - **Regression testing**: `TestStrokeBoundsMemoization` + block-level memo pins in `tests/test_text_geometry.py` (memo object identity, randomized brute-force union equivalence, empty-never-cached, every transform constructor uncached, `replace()` never carries a memo, eq/hash/repr invariance)

* **generate:** fixed the three profiled pipeline bottlenecks (`docs/PROFILING.md`); coolsys big label **68.7s → 3.6s**, round tags **51.0s → 2.7s** wall (median-of-3, same box)
  - **`reflow` 66.7s → 2.1s**: exact AABB pre-filters in `generate/geometry.py` (`bbox_gap` is a proven lower bound on the ink distance inside, so `polyline_gap`/`ink_gap` skip segment/polyline pairs whose boxes sit beyond the current best) and in `descender_adjust.colliding_partners` (per-partner ink-bbox skip); every measured distance is bit-identical (pinned by randomized brute-force equivalence tests). The reflow growth-loop scale probe renders a relief-disabled clone — it consumes only `compression_by_line`, which the X phase computes before the Y-only relief runs
  - **`layout_render_pack` 49.6s → 1.3s**: `ellipse_packer._find_best_position` breaks the secondary scan at the first feasible candidate of a column (the first hit is the column's minimum extent), and the collision union is replaced by a placement index whose inscribed/circumcircle center-distance bounds (numpy, batched per scan column) prove collision/disjointness for nearly every candidate, with the thin band falling through to exact per-polygon `intersects` (tangency semantics unchanged, no per-placement `unary_union` rebuild)
  - **ensemble spawn**: layers of at most `ENSEMBLE_SEQUENTIAL_BLOCK_THRESHOLD` (48) blocks run their strategy jobs in-process — the per-layer `ProcessPoolExecutor` spawn costs seconds on the Windows spawn context for milliseconds of work; large layers stay pooled (a 481-block layer is ~35s sequential vs ~10s pooled). Follow-up measurement corrected the original diagnosis: sunwest's 10.5s is its 481-block layer's real parallel compute, not spawn cost
  - **Verification**: all 13 benchmark PLTs match the pre-fix baseline byte-for-byte (8) or with identical segment multisets and order-only diffs (5, the tie-break's intended effect); `geometry.py` stays dependency-free (per-op numpy overhead loses at glyph-sized arrays)

### Bug Fixes

* **core:** deterministic `ParallelEnsembleStrategy` winner tie-break (restores the identical-input-identical-output invariant)
  - **Root cause**: worker benchmarks were appended in `as_completed` (wall-clock) order and `_select_best_result` broke exact-distance ties toward the first-arriving job, so a layer whose strategies tie on travel distance routed through a timing-dependent winner — identical inputs emitted different toolpath **order** between runs (the byte-MISMATCH rows in `docs/PROFILING.md`)
  - **Solution**: `StrategyBenchmarkResult` carries its job's start/end terminals and a canonical total-order key (improvement desc, distance asc, strategy name, terminals — never execution time) drives both winner selection and the displayed benchmark table
  - **Remaining gap (open)**: SA/GA use the unseeded global `random`, so on specs where a stochastic strategy wins the winning tour varies run to run (`--fast-mode` runs are byte-identical, proving the RNG is the source); seeding each job deterministically is the tracked follow-up
  - **Regression testing**: scrambled-completion-order fake-pool test (submission vs reversed order select the same winner and table), permutation test on `_select_best_result`, sequential/pool path-agreement + pool-skip/boundary tests

* **generate:** re-remove the buggy radial margin recomputation for round labels (regression in `7dde646`)
  - **Root cause**: `7dde646` "restored" the `ellipse_content_inset` margin recomputation in `_render_positioned_lines` as merge-lost wiring, but `4757694` had deliberately removed it — the recomputation double-applies the margin (box inset AND inscribed-block rule), compressing text that already fits
  - **Manifestation**: unnecessary horizontal text compression on `type: round` labels (and the `rounded_rectangle_fraction: 1.0` full-ellipse limit); e.g. a line fitting the user margins compressed to scale 0.395 → 0.271
  - **Solution**: round labels trust the user-specified margins directly (the rounded-rect corner-aware inset for `0 < f < 1` is a deliberate design and stays); `ellipse_content_inset` remains a tested geometry primitive, unapplied
  - **Regression testing**: `TestUserMarginsTrustedOnRoundLabels` pins round/full-ellipse compression == the rect twin (the old `TestRadialContentInsetApplied` used `>=` + the compression floor, so it passed under both behaviors)
* **parser:** fix multi-coordinate PD command fragmentation causing 99.2% path bloat in round labels
  - **Root cause**: Stale `pen_state` variable check during coordinate iteration in `_build_document()` lines ~195-230
  - **Manifestation**: Each coordinate pair in multi-coordinate PD commands incorrectly created new StrokePath instead of appending to existing path
  - **Impact**: Round labels with 3 ellipse boundaries + 3 drill holes produced 477 paths instead of 6
  - **Performance consequence**: Optimization block count ~6519 instead of ~24 (99.63% bloat), making round label generation unusable
  - **Solution**: Added `path_started_in_command` flag to distinguish first coordinate (pen-state transition) from subsequent coordinates in same command
  - **Validation**: 119 parser unit tests pass; integration test with ellipse_job.yaml confirms 24 blocks (vs 6519+ before fix) with correct segment distribution {5: 6, 158: 3, ...}
  - **Performance gain**: 40.8% rapid travel reduction in generated toolpath optimization (58,145.7 → 34,442.6 plotter units)
  - **Regression testing**: Full test suite validation completed with 0 failures, 98% coverage maintained
### Tooling

* **profiling:** optimize-pipeline perf loop: stage timers on the `optimize` CLI + core pipeline, the `scripts/profile_optimize.py` harness, the tracked `profiling/optimization_pipeline_summary.csv` history, and the `optimize-pipeline-profiling` / `optimize-pipeline-optimization` skills
  - `plt_optimizer/generate/stage_timing.py` moved to `plt_optimizer/utils/stage_timing.py` (the shared Win7-safe substrate) so `core/pipeline.py` can import it; the timing-line format is unchanged and stays pinned by `tests/test_stage_timers.py`
  - `cli/optimize.py` emits `parse`/`profile`/`preprocess`/`chunk`/`optimize`/`write`; `optimize_and_reassemble(stage_timers=True)` (opt-in, default off — watch/generate log streams unchanged) emits the inner `optimize_tsp`/`direction_sweep`/`intra_sweep`/`reassemble`/`merge` lines; pinned by `tests/test_pipeline.py::TestStageTimers` (emission, default-off, log-only identity)
  - `scripts/profile_optimize.py`: the `profile_generate.py` shape (median-of-N wall, cProfile, tracemalloc, stage sums, `--ab` -O mode) for the benchmark pair `examples/334X712 RED DANGER conveyers start and stop2.plt` (text branch) + `tests_deps/1x3 half inch letters holes1.plt` (structural branch, frozen fixture); adds the quality KPI columns `rapid_before_in`/`rapid_after_in`/`improvement_pct` so a perf change is checked for routing-quality drift; history CSV is `profiling/optimization_pipeline_summary.csv` (adjacent to the generate `summary.csv`, never mixed); pinned by `tests/test_profile_optimize.py`
  - Skills: `.github/skills/optimize-pipeline-profiling/` (mechanics + `scripts/baseline_capture.py` worktree baselines) and `.github/skills/optimize-pipeline-optimization/` (the one-bottleneck-per-cycle loop + `scripts/iteration_report.py` gain scoreboard)
  - Baseline measured (median-of-3, macOS): RED DANGER 0.19 s wall / 39.0% rapid saved; holes1 0.91 s wall (0.04 s cpu — pooled-ensemble spawn wall on 194 blocks) / 75.8% saved; `docs/PROFILING.md` "Optimize-Pipeline Profiling" documents the stage table, benchmark set, and current #1 bottleneck

* **profiling:** run-history CSV moved to the **tracked** `profiling/summary.csv` (was `test_output/profiling/summary.csv`, ignored via `*_output`)
  - The perf history is now versioned: every sweep's rows commit with the cycle they measure, so gains are diffable in git, not just on disk
  - `scripts/profile_generate.py`: new `--history-csv` (default the tracked path); per-spec artifacts (`.prof`/`.top.txt`/`.mem.txt`/`run.log`) stay scratch under `test_output/profiling/`; `plot_profiling_history.py` + `iteration_report.py` default to the tracked CSV (plot PNGs stay scratch); `baseline_capture.py` sweeps `--history-csv` inside the worktree and merges into the tracked history
  - Docs/skills updated (`docs/PROFILING.md`, both pipeline skills, `docs/AGENTS.md`); defaults pinned by `tests/test_profile_generate.py` / `test_plot_profiling_history.py` / `test_iteration_report.py`
## [1.8.0](https://github.com/twilsonco/Ploptimizer/compare/v1.7.0...v1.8.0) (2026-09-15)


### Features

* add --analyze-only mode to re-run benchmark winner analysis ([1bd8ec9](https://github.com/twilsonco/Ploptimizer/commit/1bd8ec98907d776010856ca9ab133a7b8583f089))
* add --log-level flag to benchmark tool (default WARNING) ([e9cadfe](https://github.com/twilsonco/Ploptimizer/commit/e9cadfe8df4dcf17cecfe3bf4869866c0eed3865))
* add batch benchmark tool for strategies ([bae59ec](https://github.com/twilsonco/Ploptimizer/commit/bae59ec3523b52439ac9f6d8c80d8d27287b75d5))
* add Christofides-Serdyukov heuristic ([a66e4c6](https://github.com/twilsonco/Ploptimizer/commit/a66e4c6f209a9d619c33af14e3ee5842833b7adc))
* add CI/CD workflows for continuous integration and automated builds ([be0810b](https://github.com/twilsonco/Ploptimizer/commit/be0810b8acd572be46351fbb171f62dbbbdd9b04))
* add configurable per-job timeout to parallel ensemble optimization ([191d7c2](https://github.com/twilsonco/Ploptimizer/commit/191d7c2a972a3004e56baa46050b9ba5bbc677ca))
* add CSV output for strategy comparison results in diagnostics ([fb8f253](https://github.com/twilsonco/Ploptimizer/commit/fb8f253cf4997fd189115bca458b0cf3b41bd8c2))
* add debug file saving functionality with comparison plots in watch command ([417e802](https://github.com/twilsonco/Ploptimizer/commit/417e8026c2f1680d31b01b08995c3b4daaa403a6))
* Add diagnostic entries for hello-world.plt in logs and metrics ([716c3a9](https://github.com/twilsonco/Ploptimizer/commit/716c3a996d4ee8307b0085aa1996747df99caf70))
* add ms_per_path and ms_per_segment columns to benchmark report.csv ([302dbda](https://github.com/twilsonco/Ploptimizer/commit/302dbdabaa510240d957871d5abc507696e54802))
* add optimization time measurement to diagnostics output ([6ca5873](https://github.com/twilsonco/Ploptimizer/commit/6ca5873291ea17029c198a5045ce6d0d9c0aa7f0))
* add output validation for optimized PLT files and enhance PLTWriter with validation logic ([48bde5e](https://github.com/twilsonco/Ploptimizer/commit/48bde5e21675dd9f74d3f6fde29f8a7472fb6c87))
* add per-file strategy winner analysis to benchmark reports ([fb772ae](https://github.com/twilsonco/Ploptimizer/commit/fb772aede9b8212549377733d468fe37f2fdf157))
* add per-strategy runtime stats to benchmark winner analysis ([cdeb478](https://github.com/twilsonco/Ploptimizer/commit/cdeb47825db157f4ad10e9ff4a5836cac3010b82))
* add pre-commit hooks for linting and type checks; update build dependencies in uv.lock ([e2fdd6e](https://github.com/twilsonco/Ploptimizer/commit/e2fdd6ea0f46e10275d5407f6d39340164966157))
* add same row preference configuration to Chunker for improved stroke grouping ([6cb9264](https://github.com/twilsonco/Ploptimizer/commit/6cb92648d4d21afb636a76370bc90db0ab264916))
* add same-row preference to block optimization ([fec0e5a](https://github.com/twilsonco/Ploptimizer/commit/fec0e5a32322da0a293e268202c92ff4c06e84ea))
* add stroke simplification functionality and corresponding tests ([6632778](https://github.com/twilsonco/Ploptimizer/commit/6632778c5d455272c1f62196c6c21d6a0487735f))
* Add system tray application and GUI components; update README and pyproject.toml ([17195ee](https://github.com/twilsonco/Ploptimizer/commit/17195ee03c5e9315076d87a6b6443cd66b177471))
* add true arc length and chord length properties ([f96ae72](https://github.com/twilsonco/Ploptimizer/commit/f96ae723b43d6571e4a8b4d0dd5f92bfaa412a5b))
* apply benchmark timeout to per-strategy runs via killable subprocesses ([0b835c0](https://github.com/twilsonco/Ploptimizer/commit/0b835c09f3b5d600b91a6a9fbebd786beca8c9ca))
* **benchmark:** add parallel execution with ProcessPoolExecutor ([b1991b9](https://github.com/twilsonco/Ploptimizer/commit/b1991b915b3a529cd6ea40e0cce2f13bd081aa92))
* enable immediate shortcut update on startup toggle ([16131d5](https://github.com/twilsonco/Ploptimizer/commit/16131d5f43b66c3db151a80e52da4b39b5bfda83))
* enhance Christofides strategy for S-T Path TSP with improved approximation and traversal logic ([72b5d32](https://github.com/twilsonco/Ploptimizer/commit/72b5d3222a7a969a860f5fefc7b6b9cd0191f25a))
* enhance Christofides strategy with candidate endpoint evaluation for optimized S-T path ([a76468b](https://github.com/twilsonco/Ploptimizer/commit/a76468b8c62c7de437bd875e2dcc2040f6ab49d9))
* enhance chunking logic to support structural files with 1:1 mapping ([19150da](https://github.com/twilsonco/Ploptimizer/commit/19150da52f87731b31c91b734e95f8c6d432bc40))
* enhance CI/CD workflows and diagnostics module; update asset creation and dependency installation ([1f29d32](https://github.com/twilsonco/Ploptimizer/commit/1f29d3273f23ad4ac0bd64cf08cdd412029858db))
* enhance diagnostics by preserving original document for baseline calculations and improving optimization metrics reporting ([eb04ae6](https://github.com/twilsonco/Ploptimizer/commit/eb04ae61b67d246a46c432a92ae43b049583c370))
* enhance Genetic Algorithm strategy with improved fitness calculation and 2-opt refinement ([245b088](https://github.com/twilsonco/Ploptimizer/commit/245b088618c855f8f7e22ad536f2c40d20bd1a3a))
* enhance logging and error handling in tray UI ([cab06ee](https://github.com/twilsonco/Ploptimizer/commit/cab06eec2d4fe2275733375f7c2119ec27b746e9))
* enhance PLTWriter to validate distance preservation during tip-to-tail optimization ([34b0845](https://github.com/twilsonco/Ploptimizer/commit/34b08453813ba4f2994a2946f0b0b7e9cef9e3fb))
* enhance Profiler to classify structural paths with geometric analysis ([dad93f2](https://github.com/twilsonco/Ploptimizer/commit/dad93f26ac3f5cefadf7719141d6f4a5c008ce73))
* enhance stroke path formatting to track spindle position across paths ([8d173aa](https://github.com/twilsonco/Ploptimizer/commit/8d173aa10ad2e343f5c7772278f72a2c8f81e379))
* enhance stroke path formatting with segment-level state tracking ([c7e079f](https://github.com/twilsonco/Ploptimizer/commit/c7e079f121a40ab4f5145ddb739a5cd15cb8a410))
* enhance watch functionality with processed directory option and logging improvements ([e49a2f1](https://github.com/twilsonco/Ploptimizer/commit/e49a2f1e8c94f7ca16e2ecfb68baa616f2f93f7d))
* **handler:** ensure filename length constraint in output paths ([57f8549](https://github.com/twilsonco/Ploptimizer/commit/57f85493b029dbafb6a9dcc73a0ecd2350e3d9b2))
* Implement chunking, optimization, profiling, and reassembly modules ([47b9e23](https://github.com/twilsonco/Ploptimizer/commit/47b9e23943c0fbe32a56d31c9c9e55bac14c4182))
* implement fracture_linear_paths function to decompose linear paths into segments ([dad93f2](https://github.com/twilsonco/Ploptimizer/commit/dad93f26ac3f5cefadf7719141d6f4a5c008ce73))
* Implement HPGL arc command parsing and rendering ([3801784](https://github.com/twilsonco/Ploptimizer/commit/3801784b3f43c9f98bdec9a3796bee602d12842b))
* implement settings modal UI and management ([fbec4a9](https://github.com/twilsonco/Ploptimizer/commit/fbec4a9011a683da20ac26d2669717a050510d01))
* implement strict identity test for no-op strategy and fix writer output formatting ([c087392](https://github.com/twilsonco/Ploptimizer/commit/c08739275ae6fce08da2ec4dc0b0d2ef434f1c3c))
* Implement system tray application with settings interface and background file watcher ([208e0aa](https://github.com/twilsonco/Ploptimizer/commit/208e0aa075e59df850b8bb25dd5d06d91ff11430))
* initial PLT-Optimizer project setup ([b5f592f](https://github.com/twilsonco/Ploptimizer/commit/b5f592ffc6ad60c3c7d76fcd92c803fa0eb40a14))
* initial release ([b17c3d5](https://github.com/twilsonco/Ploptimizer/commit/b17c3d5ae30fbfb1865ac225a86a1eca9fe368a5))
* mirror benchmark winner summary table to report_winner_summary.csv ([95c6cec](https://github.com/twilsonco/Ploptimizer/commit/95c6cec28a8e4005da12e1788e7a3046940f062e))
* other optimization strategies get farthest and nearest endpoint evaluations ([27c212e](https://github.com/twilsonco/Ploptimizer/commit/27c212e074eafc76ca49def537c862016b012241))
* **tray:** add detached mode and tkinter event loop ([e2677e3](https://github.com/twilsonco/Ploptimizer/commit/e2677e3642a801a385856be7dca3a6c8887579f7))
* **ui:** add cleanup section and improve directory dialog handling ([9eabe1a](https://github.com/twilsonco/Ploptimizer/commit/9eabe1ac3fde4ae562641a01afeaa0e908e92518))
* **ui:** add Open buttons to directory fields and implement open logic ([18a3502](https://github.com/twilsonco/Ploptimizer/commit/18a3502af5df4fa3a5e64e156621ef7d98531933))
* **ui:** add platform-specific systray impl for Windows ([76e8c94](https://github.com/twilsonco/Ploptimizer/commit/76e8c94393417a62e5931a05d95d664f6ed936ef))
* **ui:** wrap settings spinboxes in frames ([856fd5f](https://github.com/twilsonco/Ploptimizer/commit/856fd5f39c11199eee9778215a620cca9f8ace85))
* update Simulated Annealing and Genetic Algorithm strategies with new default parameters for improved optimization ([f92e587](https://github.com/twilsonco/Ploptimizer/commit/f92e5877a68058f68aa9d5110bcbe18e93fa8651))
* **writer:** add filename length constraint method and tests ([8af7096](https://github.com/twilsonco/Ploptimizer/commit/8af709680db3a3612c33759c2acedbd951f82cfe))


### Bug Fixes

* add line to trigger tests ([6bae78e](https://github.com/twilsonco/Ploptimizer/commit/6bae78e8c4ac44de118f586ec7336d8e48255b1e))
* add log statement at startup ([8b916fa](https://github.com/twilsonco/Ploptimizer/commit/8b916faa328be3a0be8ee531a65565ef1c22c96e))
* add plotting extra to dependency installation in CI workflow ([2a778ec](https://github.com/twilsonco/Ploptimizer/commit/2a778ec54e28defb712384a3b3e91ea173369fcc))
* build Windows exe on every release ([3ab3e8f](https://github.com/twilsonco/Ploptimizer/commit/3ab3e8fe9523e48c1e68643e598ead1a72659d25))
* ci - ensure Windows 7 executable compatibility by using Python 3.8 and uv&lt;0.1.40 ([91ee6bc](https://github.com/twilsonco/Ploptimizer/commit/91ee6bcf6ed4479406df369b1d9aa9257137c937))
* ci build directory ([c5d11d7](https://github.com/twilsonco/Ploptimizer/commit/c5d11d73f0cc87ad83e7df19efcb123b6ba42a22))
* ci exe build ([435a531](https://github.com/twilsonco/Ploptimizer/commit/435a53107ba4d9afbeb0f57887f5ecb8de41cc53))
* ci exe build ([b22b1a6](https://github.com/twilsonco/Ploptimizer/commit/b22b1a650cacdadcb52ebb6bb7ddee4a9fbeb9c2))
* ci exe build ([137a602](https://github.com/twilsonco/Ploptimizer/commit/137a602dd798992ca20021ad91eb8126f865e3ba))
* ci optimization ([b2a04f8](https://github.com/twilsonco/Ploptimizer/commit/b2a04f8e5444e4cb7b83435088bb464c40c50fc3))
* ci optimization ([ed8f504](https://github.com/twilsonco/Ploptimizer/commit/ed8f50421bb2494c57b01db4b58a3c4f53e81282))
* ci optimization ([6a1053b](https://github.com/twilsonco/Ploptimizer/commit/6a1053bad2707daee1101dcc7adefab8b32d6097))
* ci optimization ([b7a77cf](https://github.com/twilsonco/Ploptimizer/commit/b7a77cfc2a9567c620cc8f04b305354593b0e70c))
* ci optimization ([92e02dd](https://github.com/twilsonco/Ploptimizer/commit/92e02dd3f8246f769cd2dd850fb8190bbf68175c))
* ci path filter ([fbc99ca](https://github.com/twilsonco/Ploptimizer/commit/fbc99caf628d8279ada46f1a298803136dca78e8))
* ci path filtering for lint/type-check/test ([b3f0382](https://github.com/twilsonco/Ploptimizer/commit/b3f0382efc0f88ccb6614afb79e6225dd6b9a3d1))
* **ci:** update Python setup in CI to version 3.11 and streamline build steps for Windows 7 compatibility ([9f942ce](https://github.com/twilsonco/Ploptimizer/commit/9f942cef4f8aabf7e89b4d5305486d550ad7db89))
* **ci:** update uv installation to remove version constraint for compatibility ([4c3e04b](https://github.com/twilsonco/Ploptimizer/commit/4c3e04b0512d35a5e474590bdd2a9c588857f0b2))
* **ci:** use windows latest for exe build ([671f618](https://github.com/twilsonco/Ploptimizer/commit/671f618b32954e78a8aaebe5d16f78141b980251))
* cleanup unused code ([0176655](https://github.com/twilsonco/Ploptimizer/commit/0176655ffa71eb33537c20a3f8d9497c36cc0fce))
* convert distances to inches in diagnostic plots and summaries ([9d77a1a](https://github.com/twilsonco/Ploptimizer/commit/9d77a1a96131c9d335b5d8bb76a4b51d6497e305))
* Correct arc sweep angle calculations in parser and writer ([716c3a9](https://github.com/twilsonco/Ploptimizer/commit/716c3a996d4ee8307b0085aa1996747df99caf70))
* correct artifact paths for Windows executable in build workflow ([f706d62](https://github.com/twilsonco/Ploptimizer/commit/f706d62384237bbd542b1b99f215f818deedc4f6))
* correct structural detection to enable optimization ([a26f250](https://github.com/twilsonco/Ploptimizer/commit/a26f250cfbe5c00a812daaf0be1e04acd4de1902))
* correct YAML syntax for push tags in CI/CD workflows ([ab1ae91](https://github.com/twilsonco/Ploptimizer/commit/ab1ae916ab8f0a1f6f2c1ac2ec737163d549850c))
* default paths ([c72f8b0](https://github.com/twilsonco/Ploptimizer/commit/c72f8b08a8c5843f20846188543faa946c999f35))
* **diagnostics:** flip y-axis in plotter for correct orientation ([9ef192c](https://github.com/twilsonco/Ploptimizer/commit/9ef192c2a8d6f52debe2aeb5b5bd124ddd6dc838))
* enable optimization for multi-arc drill holes in structural files ([760d403](https://github.com/twilsonco/Ploptimizer/commit/760d403a79209efe726f29f6ec51f8dd9a6dd2ef))
* handle PyInstaller multiprocessing issues on Windows ([d596aea](https://github.com/twilsonco/Ploptimizer/commit/d596aea1ecbc6bb92691d3c7fcff12351ba6d273))
* improve file handling in PLTFileHandler to check for existence before moving or deleting ([34e68fc](https://github.com/twilsonco/Ploptimizer/commit/34e68fcc6938c94bf6f3dba00d228ca5f7f5d07c))
* improve genetic algorithm offspring generation logic for unique block indices ([6179c1c](https://github.com/twilsonco/Ploptimizer/commit/6179c1c10564ff10d010f4bf745b048ebb7cb107))
* initialize _shutdown_requested attribute in WatchCommand ([9c96a28](https://github.com/twilsonco/Ploptimizer/commit/9c96a285d3de287415ea0e75775fe59edc16b90a))
* only show settings automatically on first run (i.e. no config file exists) ([bb4b7a9](https://github.com/twilsonco/Ploptimizer/commit/bb4b7a9fb24aa21198894187d24bbefbcbc7194f))
* paths as strings ([f5ddf0b](https://github.com/twilsonco/Ploptimizer/commit/f5ddf0b37516bbc0fb9448ea6733ca1c16c4ceb1))
* **plt_optimizer:** fix insertion heuristic logic and costs ([1d54c89](https://github.com/twilsonco/Ploptimizer/commit/1d54c89bcfbf1cf7d2f7dfb625a6d91b22988ce7))
* preserve original document for accurate before-optimization statistics and plots ([f4ba99c](https://github.com/twilsonco/Ploptimizer/commit/f4ba99ce4ffbc5f25591e193a1cb24416771ae57))
* prevent infinite spawning of built exe ([722e91c](https://github.com/twilsonco/Ploptimizer/commit/722e91c684e557960ac0224d67ae4e6a02666a9e))
* re-enable Christofides-Serdyukov stratrgy ([5ccb9c5](https://github.com/twilsonco/Ploptimizer/commit/5ccb9c5ddffccfd68010328eec8a15e17aead0f9))
* replace __file__ with SPECPATH in PyInstaller spec ([67bed30](https://github.com/twilsonco/Ploptimizer/commit/67bed308e51d8aebacd724e315a705a7fda12412))
* replace *other* test file ([2c36429](https://github.com/twilsonco/Ploptimizer/commit/2c3642984ae7896ce604d97648a1a1dcd72caf82))
* replace test plt file ([fb4b87b](https://github.com/twilsonco/Ploptimizer/commit/fb4b87b84f043f0c68990346fbe2d6bd8b5baa09))
* restore Python 3.8 compatibility ([d4d06f3](https://github.com/twilsonco/Ploptimizer/commit/d4d06f361e69706d2693e7e0bde985e03bf134ca))
* retry parallel ensemble jobs lost to a broken worker pool ([9654de0](https://github.com/twilsonco/Ploptimizer/commit/9654de06dfa1d17f9094b49ef54a6886a08935f0))
* skip-duplicate-actions path filter causing false skips ([2e27964](https://github.com/twilsonco/Ploptimizer/commit/2e2796422f329a8373fa9de9cab7e95d9280f5b6))
* startup logging to trigger test ([f7685ec](https://github.com/twilsonco/Ploptimizer/commit/f7685ec26f29a3740679154bc4a418c2f35ca98c))
* **test:** correct inverted skip markers on Windows-only tests ([cef9134](https://github.com/twilsonco/Ploptimizer/commit/cef91349ce644031a72e1e892a51808a07e8fc7c))
* **test:** patch _ensure_filename_length in mocks ([7bbcead](https://github.com/twilsonco/Ploptimizer/commit/7bbcead45fe9fc57d27c07f149529dbecb2aee89))
* tray icon and config directory string field widths ([bdf7d4b](https://github.com/twilsonco/Ploptimizer/commit/bdf7d4b9847135834bb61fb5c5e87aa2e7a5f8c9))
* **tray:** avoid deadlock on infi.systray shutdown ([7410665](https://github.com/twilsonco/Ploptimizer/commit/74106657ca67bb6b84c2e92c75872986cd31df81))
* **tray:** fix signal handling and settings window loop ([0d07aa9](https://github.com/twilsonco/Ploptimizer/commit/0d07aa9de2851ab08756baba780752b6ccdf9a28))
* **tray:** skip initial settings dialog and use default watch dir ([cd4d9aa](https://github.com/twilsonco/Ploptimizer/commit/cd4d9aa3f863bb1f02890d98be7c1e6dbe06ee33))
* **tray:** switch to blocking mode for pystray ([f5e42b9](https://github.com/twilsonco/Ploptimizer/commit/f5e42b90f36e745a407e17a9318a6df6b3695f3d))
* typehint errors ([d9686a2](https://github.com/twilsonco/Ploptimizer/commit/d9686a26db70ded1047e2c16a7cb2b0c5dca270f))
* **ui, startup:** handle missing optional dependencies gracefully ([0122b71](https://github.com/twilsonco/Ploptimizer/commit/0122b7184bdaefe16a1bdf949ea22eb3ed3f1be9))
* **ui:** fix dialog UX and tray termination ([1cc3b6e](https://github.com/twilsonco/Ploptimizer/commit/1cc3b6ee24eb315bf88127dffbf0cd4bc4b79734))
* **ui:** fix filedialog focus issues with withdrawn window ([50ef18f](https://github.com/twilsonco/Ploptimizer/commit/50ef18f2459a94e8a8c0834fc644c79c30eac903))
* **ui:** fix settings window modal and crash issues ([0ea98d2](https://github.com/twilsonco/Ploptimizer/commit/0ea98d2034e18a8d0a8dfec02244988a18e0b792))
* **ui:** improve window centering and focus handling ([3baef93](https://github.com/twilsonco/Ploptimizer/commit/3baef930957a96cea0b8236ca3758d7e0e4f4e9f))
* **ui:** safely handle network path errors ([6bca37a](https://github.com/twilsonco/Ploptimizer/commit/6bca37a8431b18e644a430fbea8bfd749b1e52b6))
* **ui:** skip grab_set to fix pystray conflict ([a58c6be](https://github.com/twilsonco/Ploptimizer/commit/a58c6be05f2f3fb4eb3863fb53d6c975bde314cc))
* **ui:** type-check os.startfile via sys.platform and align pre-commit with CI ([967ddc2](https://github.com/twilsonco/Ploptimizer/commit/967ddc2d76a6b0a3a99a7cdbe76e5e168422119c))
* update build workflow to correctly handle executable paths and options ([28551af](https://github.com/twilsonco/Ploptimizer/commit/28551afc9ec1030bbb75c730d1be1612d8a032a9))
* update file handling logic to delete original files by default after optimization ([457c41d](https://github.com/twilsonco/Ploptimizer/commit/457c41d8f1bc3e38004e604b903de24b16b030dc))
* update rapid travel distance display in diagnostic plots for consistency ([a96404a](https://github.com/twilsonco/Ploptimizer/commit/a96404a851e71b98fdf3cbcb3ebc07e7927c8ef7))
* update strategy name references in diagnostics output ([74039d8](https://github.com/twilsonco/Ploptimizer/commit/74039d8fce010bd822062690373525c4e9627b61))
* use correct default paths for Windows ([86b9c7e](https://github.com/twilsonco/Ploptimizer/commit/86b9c7e049ec3e15f809ba0b856d7963477a681e))
* use optimized_doc.rapid_distance() for accurate rapid travel calculations ([3968607](https://github.com/twilsonco/Ploptimizer/commit/3968607a209c7ed47dd7ed81fae06f9db20381f6))
* **watch:** prevent processing files locked by other processes ([75fcc8c](https://github.com/twilsonco/Ploptimizer/commit/75fcc8c50a4f7aba907dee6bbaff660c8b64c8da))
* **watch:** wait for input file to stop being modified, and use atomic writes to place full output file at once ([2ae3a0b](https://github.com/twilsonco/Ploptimizer/commit/2ae3a0b33ac59a609073caf00ac3ce06a8a4b789))
* Win7 support path ([f060732](https://github.com/twilsonco/Ploptimizer/commit/f060732ff7a0dc701f42385413f2d48c7a00f9d6))
* windows 7 support and prevent infinite spawn of exe ([e507abd](https://github.com/twilsonco/Ploptimizer/commit/e507abdd14354b9432f44ec8a5490889d1bafaae))
* **writer:** adhere to use of newlines when writing plt files ([d91f9b5](https://github.com/twilsonco/Ploptimizer/commit/d91f9b587ded1aebd7725ea9c9847e5fc631dbc5))


### Documentation

* add instructions for installing Visual C++ Redistributable for Windows users ([4f05a71](https://github.com/twilsonco/Ploptimizer/commit/4f05a718e69b66abee5796b782f50821e914af94))
* add some references to dev docs ([f570236](https://github.com/twilsonco/Ploptimizer/commit/f57023621d9ff9b1d52c2ba3437abc523cd7e0f1))
* correct capitalization and improve description of Ploptimizer in README ([121106c](https://github.com/twilsonco/Ploptimizer/commit/121106c26fb017564a9cd28995ee6e32d3f43634))
* reflect installer-only publishing in READMEs ([42651f2](https://github.com/twilsonco/Ploptimizer/commit/42651f20c31f9d1e19275f7a2562498101993ad3))
* rename executable to Ploptimizer and document --onefile requirement ([05746cf](https://github.com/twilsonco/Ploptimizer/commit/05746cf2c2251c91696e7e72ef6b48f875811290))
* update AGENTS.md with Win7 compatability constraint ([a48da04](https://github.com/twilsonco/Ploptimizer/commit/a48da0483a549602aaa9f8538512f29410bdd97f))
* update readme and make `README_DEV.md` for more details ([1b99581](https://github.com/twilsonco/Ploptimizer/commit/1b99581df5d01758041dd29447b23003b73af81d))
* update README for Windows installation instructions and add standalone executable build steps; update pyproject.toml for build dependencies ([145b9d9](https://github.com/twilsonco/Ploptimizer/commit/145b9d920a8dea4c2b3ca6ee4179bdec6061b204))
* update README to clarify support for Windows 7 in build instructions ([4c829de](https://github.com/twilsonco/Ploptimizer/commit/4c829de404c53536d5017dac39c0df72a882388f))
* update readme with images. ([3433533](https://github.com/twilsonco/Ploptimizer/commit/34335335d03ee85f5c8b4182dd73fceec766b04d))
* update readme with windows instructions ([a85f68d](https://github.com/twilsonco/Ploptimizer/commit/a85f68dd8d1a722a7dc2a90c8d260a1ebf1e0ebf))
* update README with Windows-specific setup instructions and troubleshooting tips ([d14ea57](https://github.com/twilsonco/Ploptimizer/commit/d14ea5795e711981667f3c8b5aeb4ecb6741be47))

## [1.7.0](https://github.com/twilsonco/Ploptimizer/compare/v1.6.4...v1.7.0) (2026-09-07)


### Features

* add --analyze-only mode to re-run benchmark winner analysis ([1bd8ec9](https://github.com/twilsonco/Ploptimizer/commit/1bd8ec98907d776010856ca9ab133a7b8583f089))
* add --log-level flag to benchmark tool (default WARNING) ([e9cadfe](https://github.com/twilsonco/Ploptimizer/commit/e9cadfe8df4dcf17cecfe3bf4869866c0eed3865))
* add configurable per-job timeout to parallel ensemble optimization ([191d7c2](https://github.com/twilsonco/Ploptimizer/commit/191d7c2a972a3004e56baa46050b9ba5bbc677ca))
* add ms_per_path and ms_per_segment columns to benchmark report.csv ([302dbda](https://github.com/twilsonco/Ploptimizer/commit/302dbdabaa510240d957871d5abc507696e54802))
* add per-file strategy winner analysis to benchmark reports ([fb772ae](https://github.com/twilsonco/Ploptimizer/commit/fb772aede9b8212549377733d468fe37f2fdf157))
* add per-strategy runtime stats to benchmark winner analysis ([cdeb478](https://github.com/twilsonco/Ploptimizer/commit/cdeb47825db157f4ad10e9ff4a5836cac3010b82))
* apply benchmark timeout to per-strategy runs via killable subprocesses ([0b835c0](https://github.com/twilsonco/Ploptimizer/commit/0b835c09f3b5d600b91a6a9fbebd786beca8c9ca))
* mirror benchmark winner summary table to report_winner_summary.csv ([95c6cec](https://github.com/twilsonco/Ploptimizer/commit/95c6cec28a8e4005da12e1788e7a3046940f062e))

## [1.6.4](https://github.com/twilsonco/Ploptimizer/compare/v1.6.3...v1.6.4) (2026-08-15)


### Bug Fixes

* handle PyInstaller multiprocessing issues on Windows ([d596aea](https://github.com/twilsonco/Ploptimizer/commit/d596aea1ecbc6bb92691d3c7fcff12351ba6d273))

## [1.6.3](https://github.com/twilsonco/Ploptimizer/compare/v1.6.2...v1.6.3) (2026-07-13)


### Bug Fixes

* correct structural detection to enable optimization ([a26f250](https://github.com/twilsonco/Ploptimizer/commit/a26f250cfbe5c00a812daaf0be1e04acd4de1902))
* enable optimization for multi-arc drill holes in structural files ([760d403](https://github.com/twilsonco/Ploptimizer/commit/760d403a79209efe726f29f6ec51f8dd9a6dd2ef))
* **watch:** prevent processing files locked by other processes ([75fcc8c](https://github.com/twilsonco/Ploptimizer/commit/75fcc8c50a4f7aba907dee6bbaff660c8b64c8da))

## [1.6.2](https://github.com/twilsonco/Ploptimizer/compare/v1.6.1...v1.6.2) (2026-07-13)


### Bug Fixes

* **ci:** update Python setup in CI to version 3.11 and streamline build steps for Windows 7 compatibility ([9f942ce](https://github.com/twilsonco/Ploptimizer/commit/9f942cef4f8aabf7e89b4d5305486d550ad7db89))
* improve file handling in PLTFileHandler to check for existence before moving or deleting ([34e68fc](https://github.com/twilsonco/Ploptimizer/commit/34e68fcc6938c94bf6f3dba00d228ca5f7f5d07c))


### Documentation

* add some references to dev docs ([f570236](https://github.com/twilsonco/Ploptimizer/commit/f57023621d9ff9b1d52c2ba3437abc523cd7e0f1))
* update README to clarify support for Windows 7 in build instructions ([4c829de](https://github.com/twilsonco/Ploptimizer/commit/4c829de404c53536d5017dac39c0df72a882388f))

## [1.6.1](https://github.com/twilsonco/Ploptimizer/compare/v1.6.0...v1.6.1) (2026-07-13)


### Bug Fixes

* **ci:** update uv installation to remove version constraint for compatibility ([4c3e04b](https://github.com/twilsonco/Ploptimizer/commit/4c3e04b0512d35a5e474590bdd2a9c588857f0b2))
* initialize _shutdown_requested attribute in WatchCommand ([9c96a28](https://github.com/twilsonco/Ploptimizer/commit/9c96a285d3de287415ea0e75775fe59edc16b90a))
* restore Python 3.8 compatibility ([d4d06f3](https://github.com/twilsonco/Ploptimizer/commit/d4d06f361e69706d2693e7e0bde985e03bf134ca))

## [1.6.0](https://github.com/twilsonco/Ploptimizer/compare/v1.5.0...v1.6.0) (2026-07-12)


### Features

* add batch benchmark tool for strategies ([bae59ec](https://github.com/twilsonco/Ploptimizer/commit/bae59ec3523b52439ac9f6d8c80d8d27287b75d5))
* add Christofides-Serdyukov heuristic ([a66e4c6](https://github.com/twilsonco/Ploptimizer/commit/a66e4c6f209a9d619c33af14e3ee5842833b7adc))
* add CI/CD workflows for continuous integration and automated builds ([be0810b](https://github.com/twilsonco/Ploptimizer/commit/be0810b8acd572be46351fbb171f62dbbbdd9b04))
* add CSV output for strategy comparison results in diagnostics ([fb8f253](https://github.com/twilsonco/Ploptimizer/commit/fb8f253cf4997fd189115bca458b0cf3b41bd8c2))
* add debug file saving functionality with comparison plots in watch command ([417e802](https://github.com/twilsonco/Ploptimizer/commit/417e8026c2f1680d31b01b08995c3b4daaa403a6))
* Add diagnostic entries for hello-world.plt in logs and metrics ([716c3a9](https://github.com/twilsonco/Ploptimizer/commit/716c3a996d4ee8307b0085aa1996747df99caf70))
* add optimization time measurement to diagnostics output ([6ca5873](https://github.com/twilsonco/Ploptimizer/commit/6ca5873291ea17029c198a5045ce6d0d9c0aa7f0))
* add output validation for optimized PLT files and enhance PLTWriter with validation logic ([48bde5e](https://github.com/twilsonco/Ploptimizer/commit/48bde5e21675dd9f74d3f6fde29f8a7472fb6c87))
* add pre-commit hooks for linting and type checks; update build dependencies in uv.lock ([e2fdd6e](https://github.com/twilsonco/Ploptimizer/commit/e2fdd6ea0f46e10275d5407f6d39340164966157))
* add same row preference configuration to Chunker for improved stroke grouping ([6cb9264](https://github.com/twilsonco/Ploptimizer/commit/6cb92648d4d21afb636a76370bc90db0ab264916))
* add same-row preference to block optimization ([fec0e5a](https://github.com/twilsonco/Ploptimizer/commit/fec0e5a32322da0a293e268202c92ff4c06e84ea))
* add stroke simplification functionality and corresponding tests ([6632778](https://github.com/twilsonco/Ploptimizer/commit/6632778c5d455272c1f62196c6c21d6a0487735f))
* Add system tray application and GUI components; update README and pyproject.toml ([17195ee](https://github.com/twilsonco/Ploptimizer/commit/17195ee03c5e9315076d87a6b6443cd66b177471))
* add true arc length and chord length properties ([f96ae72](https://github.com/twilsonco/Ploptimizer/commit/f96ae723b43d6571e4a8b4d0dd5f92bfaa412a5b))
* **benchmark:** add parallel execution with ProcessPoolExecutor ([b1991b9](https://github.com/twilsonco/Ploptimizer/commit/b1991b915b3a529cd6ea40e0cce2f13bd081aa92))
* enable immediate shortcut update on startup toggle ([16131d5](https://github.com/twilsonco/Ploptimizer/commit/16131d5f43b66c3db151a80e52da4b39b5bfda83))
* enhance Christofides strategy for S-T Path TSP with improved approximation and traversal logic ([72b5d32](https://github.com/twilsonco/Ploptimizer/commit/72b5d3222a7a969a860f5fefc7b6b9cd0191f25a))
* enhance Christofides strategy with candidate endpoint evaluation for optimized S-T path ([a76468b](https://github.com/twilsonco/Ploptimizer/commit/a76468b8c62c7de437bd875e2dcc2040f6ab49d9))
* enhance chunking logic to support structural files with 1:1 mapping ([19150da](https://github.com/twilsonco/Ploptimizer/commit/19150da52f87731b31c91b734e95f8c6d432bc40))
* enhance CI/CD workflows and diagnostics module; update asset creation and dependency installation ([1f29d32](https://github.com/twilsonco/Ploptimizer/commit/1f29d3273f23ad4ac0bd64cf08cdd412029858db))
* enhance diagnostics by preserving original document for baseline calculations and improving optimization metrics reporting ([eb04ae6](https://github.com/twilsonco/Ploptimizer/commit/eb04ae61b67d246a46c432a92ae43b049583c370))
* enhance Genetic Algorithm strategy with improved fitness calculation and 2-opt refinement ([245b088](https://github.com/twilsonco/Ploptimizer/commit/245b088618c855f8f7e22ad536f2c40d20bd1a3a))
* enhance logging and error handling in tray UI ([cab06ee](https://github.com/twilsonco/Ploptimizer/commit/cab06eec2d4fe2275733375f7c2119ec27b746e9))
* enhance PLTWriter to validate distance preservation during tip-to-tail optimization ([34b0845](https://github.com/twilsonco/Ploptimizer/commit/34b08453813ba4f2994a2946f0b0b7e9cef9e3fb))
* enhance Profiler to classify structural paths with geometric analysis ([dad93f2](https://github.com/twilsonco/Ploptimizer/commit/dad93f26ac3f5cefadf7719141d6f4a5c008ce73))
* enhance stroke path formatting to track spindle position across paths ([8d173aa](https://github.com/twilsonco/Ploptimizer/commit/8d173aa10ad2e343f5c7772278f72a2c8f81e379))
* enhance stroke path formatting with segment-level state tracking ([c7e079f](https://github.com/twilsonco/Ploptimizer/commit/c7e079f121a40ab4f5145ddb739a5cd15cb8a410))
* enhance watch functionality with processed directory option and logging improvements ([e49a2f1](https://github.com/twilsonco/Ploptimizer/commit/e49a2f1e8c94f7ca16e2ecfb68baa616f2f93f7d))
* **handler:** ensure filename length constraint in output paths ([57f8549](https://github.com/twilsonco/Ploptimizer/commit/57f85493b029dbafb6a9dcc73a0ecd2350e3d9b2))
* Implement chunking, optimization, profiling, and reassembly modules ([47b9e23](https://github.com/twilsonco/Ploptimizer/commit/47b9e23943c0fbe32a56d31c9c9e55bac14c4182))
* implement fracture_linear_paths function to decompose linear paths into segments ([dad93f2](https://github.com/twilsonco/Ploptimizer/commit/dad93f26ac3f5cefadf7719141d6f4a5c008ce73))
* Implement HPGL arc command parsing and rendering ([3801784](https://github.com/twilsonco/Ploptimizer/commit/3801784b3f43c9f98bdec9a3796bee602d12842b))
* implement settings modal UI and management ([fbec4a9](https://github.com/twilsonco/Ploptimizer/commit/fbec4a9011a683da20ac26d2669717a050510d01))
* implement strict identity test for no-op strategy and fix writer output formatting ([c087392](https://github.com/twilsonco/Ploptimizer/commit/c08739275ae6fce08da2ec4dc0b0d2ef434f1c3c))
* Implement system tray application with settings interface and background file watcher ([208e0aa](https://github.com/twilsonco/Ploptimizer/commit/208e0aa075e59df850b8bb25dd5d06d91ff11430))
* initial PLT-Optimizer project setup ([b5f592f](https://github.com/twilsonco/Ploptimizer/commit/b5f592ffc6ad60c3c7d76fcd92c803fa0eb40a14))
* initial release ([b17c3d5](https://github.com/twilsonco/Ploptimizer/commit/b17c3d5ae30fbfb1865ac225a86a1eca9fe368a5))
* other optimization strategies get farthest and nearest endpoint evaluations ([27c212e](https://github.com/twilsonco/Ploptimizer/commit/27c212e074eafc76ca49def537c862016b012241))
* **tray:** add detached mode and tkinter event loop ([e2677e3](https://github.com/twilsonco/Ploptimizer/commit/e2677e3642a801a385856be7dca3a6c8887579f7))
* **ui:** add cleanup section and improve directory dialog handling ([9eabe1a](https://github.com/twilsonco/Ploptimizer/commit/9eabe1ac3fde4ae562641a01afeaa0e908e92518))
* **ui:** add platform-specific systray impl for Windows ([76e8c94](https://github.com/twilsonco/Ploptimizer/commit/76e8c94393417a62e5931a05d95d664f6ed936ef))
* update Simulated Annealing and Genetic Algorithm strategies with new default parameters for improved optimization ([f92e587](https://github.com/twilsonco/Ploptimizer/commit/f92e5877a68058f68aa9d5110bcbe18e93fa8651))
* **writer:** add filename length constraint method and tests ([8af7096](https://github.com/twilsonco/Ploptimizer/commit/8af709680db3a3612c33759c2acedbd951f82cfe))


### Bug Fixes

* add line to trigger tests ([6bae78e](https://github.com/twilsonco/Ploptimizer/commit/6bae78e8c4ac44de118f586ec7336d8e48255b1e))
* add log statement at startup ([8b916fa](https://github.com/twilsonco/Ploptimizer/commit/8b916faa328be3a0be8ee531a65565ef1c22c96e))
* add plotting extra to dependency installation in CI workflow ([2a778ec](https://github.com/twilsonco/Ploptimizer/commit/2a778ec54e28defb712384a3b3e91ea173369fcc))
* build Windows exe on every release ([3ab3e8f](https://github.com/twilsonco/Ploptimizer/commit/3ab3e8fe9523e48c1e68643e598ead1a72659d25))
* ci - ensure Windows 7 executable compatibility by using Python 3.8 and uv&lt;0.1.40 ([91ee6bc](https://github.com/twilsonco/Ploptimizer/commit/91ee6bcf6ed4479406df369b1d9aa9257137c937))
* ci build directory ([c5d11d7](https://github.com/twilsonco/Ploptimizer/commit/c5d11d73f0cc87ad83e7df19efcb123b6ba42a22))
* ci exe build ([435a531](https://github.com/twilsonco/Ploptimizer/commit/435a53107ba4d9afbeb0f57887f5ecb8de41cc53))
* ci exe build ([b22b1a6](https://github.com/twilsonco/Ploptimizer/commit/b22b1a650cacdadcb52ebb6bb7ddee4a9fbeb9c2))
* ci exe build ([137a602](https://github.com/twilsonco/Ploptimizer/commit/137a602dd798992ca20021ad91eb8126f865e3ba))
* ci optimization ([b2a04f8](https://github.com/twilsonco/Ploptimizer/commit/b2a04f8e5444e4cb7b83435088bb464c40c50fc3))
* ci optimization ([ed8f504](https://github.com/twilsonco/Ploptimizer/commit/ed8f50421bb2494c57b01db4b58a3c4f53e81282))
* ci optimization ([6a1053b](https://github.com/twilsonco/Ploptimizer/commit/6a1053bad2707daee1101dcc7adefab8b32d6097))
* ci optimization ([b7a77cf](https://github.com/twilsonco/Ploptimizer/commit/b7a77cfc2a9567c620cc8f04b305354593b0e70c))
* ci optimization ([92e02dd](https://github.com/twilsonco/Ploptimizer/commit/92e02dd3f8246f769cd2dd850fb8190bbf68175c))
* ci path filter ([fbc99ca](https://github.com/twilsonco/Ploptimizer/commit/fbc99caf628d8279ada46f1a298803136dca78e8))
* ci path filtering for lint/type-check/test ([b3f0382](https://github.com/twilsonco/Ploptimizer/commit/b3f0382efc0f88ccb6614afb79e6225dd6b9a3d1))
* **ci:** use windows latest for exe build ([671f618](https://github.com/twilsonco/Ploptimizer/commit/671f618b32954e78a8aaebe5d16f78141b980251))
* cleanup unused code ([0176655](https://github.com/twilsonco/Ploptimizer/commit/0176655ffa71eb33537c20a3f8d9497c36cc0fce))
* convert distances to inches in diagnostic plots and summaries ([9d77a1a](https://github.com/twilsonco/Ploptimizer/commit/9d77a1a96131c9d335b5d8bb76a4b51d6497e305))
* Correct arc sweep angle calculations in parser and writer ([716c3a9](https://github.com/twilsonco/Ploptimizer/commit/716c3a996d4ee8307b0085aa1996747df99caf70))
* correct artifact paths for Windows executable in build workflow ([f706d62](https://github.com/twilsonco/Ploptimizer/commit/f706d62384237bbd542b1b99f215f818deedc4f6))
* correct YAML syntax for push tags in CI/CD workflows ([ab1ae91](https://github.com/twilsonco/Ploptimizer/commit/ab1ae916ab8f0a1f6f2c1ac2ec737163d549850c))
* default paths ([c72f8b0](https://github.com/twilsonco/Ploptimizer/commit/c72f8b08a8c5843f20846188543faa946c999f35))
* **diagnostics:** flip y-axis in plotter for correct orientation ([9ef192c](https://github.com/twilsonco/Ploptimizer/commit/9ef192c2a8d6f52debe2aeb5b5bd124ddd6dc838))
* improve genetic algorithm offspring generation logic for unique block indices ([6179c1c](https://github.com/twilsonco/Ploptimizer/commit/6179c1c10564ff10d010f4bf745b048ebb7cb107))
* only show settings automatically on first run (i.e. no config file exists) ([bb4b7a9](https://github.com/twilsonco/Ploptimizer/commit/bb4b7a9fb24aa21198894187d24bbefbcbc7194f))
* paths as strings ([f5ddf0b](https://github.com/twilsonco/Ploptimizer/commit/f5ddf0b37516bbc0fb9448ea6733ca1c16c4ceb1))
* **plt_optimizer:** fix insertion heuristic logic and costs ([1d54c89](https://github.com/twilsonco/Ploptimizer/commit/1d54c89bcfbf1cf7d2f7dfb625a6d91b22988ce7))
* preserve original document for accurate before-optimization statistics and plots ([f4ba99c](https://github.com/twilsonco/Ploptimizer/commit/f4ba99ce4ffbc5f25591e193a1cb24416771ae57))
* prevent infinite spawning of built exe ([722e91c](https://github.com/twilsonco/Ploptimizer/commit/722e91c684e557960ac0224d67ae4e6a02666a9e))
* re-enable Christofides-Serdyukov stratrgy ([5ccb9c5](https://github.com/twilsonco/Ploptimizer/commit/5ccb9c5ddffccfd68010328eec8a15e17aead0f9))
* replace __file__ with SPECPATH in PyInstaller spec ([67bed30](https://github.com/twilsonco/Ploptimizer/commit/67bed308e51d8aebacd724e315a705a7fda12412))
* replace *other* test file ([2c36429](https://github.com/twilsonco/Ploptimizer/commit/2c3642984ae7896ce604d97648a1a1dcd72caf82))
* replace test plt file ([fb4b87b](https://github.com/twilsonco/Ploptimizer/commit/fb4b87b84f043f0c68990346fbe2d6bd8b5baa09))
* skip-duplicate-actions path filter causing false skips ([2e27964](https://github.com/twilsonco/Ploptimizer/commit/2e2796422f329a8373fa9de9cab7e95d9280f5b6))
* startup logging to trigger test ([f7685ec](https://github.com/twilsonco/Ploptimizer/commit/f7685ec26f29a3740679154bc4a418c2f35ca98c))
* **test:** correct inverted skip markers on Windows-only tests ([cef9134](https://github.com/twilsonco/Ploptimizer/commit/cef91349ce644031a72e1e892a51808a07e8fc7c))
* **test:** patch _ensure_filename_length in mocks ([7bbcead](https://github.com/twilsonco/Ploptimizer/commit/7bbcead45fe9fc57d27c07f149529dbecb2aee89))
* tray icon and config directory string field widths ([bdf7d4b](https://github.com/twilsonco/Ploptimizer/commit/bdf7d4b9847135834bb61fb5c5e87aa2e7a5f8c9))
* **tray:** avoid deadlock on infi.systray shutdown ([7410665](https://github.com/twilsonco/Ploptimizer/commit/74106657ca67bb6b84c2e92c75872986cd31df81))
* **tray:** fix signal handling and settings window loop ([0d07aa9](https://github.com/twilsonco/Ploptimizer/commit/0d07aa9de2851ab08756baba780752b6ccdf9a28))
* **tray:** skip initial settings dialog and use default watch dir ([cd4d9aa](https://github.com/twilsonco/Ploptimizer/commit/cd4d9aa3f863bb1f02890d98be7c1e6dbe06ee33))
* **tray:** switch to blocking mode for pystray ([f5e42b9](https://github.com/twilsonco/Ploptimizer/commit/f5e42b90f36e745a407e17a9318a6df6b3695f3d))
* typehint errors ([d9686a2](https://github.com/twilsonco/Ploptimizer/commit/d9686a26db70ded1047e2c16a7cb2b0c5dca270f))
* **ui, startup:** handle missing optional dependencies gracefully ([0122b71](https://github.com/twilsonco/Ploptimizer/commit/0122b7184bdaefe16a1bdf949ea22eb3ed3f1be9))
* **ui:** fix dialog UX and tray termination ([1cc3b6e](https://github.com/twilsonco/Ploptimizer/commit/1cc3b6ee24eb315bf88127dffbf0cd4bc4b79734))
* **ui:** fix filedialog focus issues with withdrawn window ([50ef18f](https://github.com/twilsonco/Ploptimizer/commit/50ef18f2459a94e8a8c0834fc644c79c30eac903))
* **ui:** fix settings window modal and crash issues ([0ea98d2](https://github.com/twilsonco/Ploptimizer/commit/0ea98d2034e18a8d0a8dfec02244988a18e0b792))
* **ui:** improve window centering and focus handling ([3baef93](https://github.com/twilsonco/Ploptimizer/commit/3baef930957a96cea0b8236ca3758d7e0e4f4e9f))
* **ui:** safely handle network path errors ([6bca37a](https://github.com/twilsonco/Ploptimizer/commit/6bca37a8431b18e644a430fbea8bfd749b1e52b6))
* **ui:** skip grab_set to fix pystray conflict ([a58c6be](https://github.com/twilsonco/Ploptimizer/commit/a58c6be05f2f3fb4eb3863fb53d6c975bde314cc))
* update build workflow to correctly handle executable paths and options ([28551af](https://github.com/twilsonco/Ploptimizer/commit/28551afc9ec1030bbb75c730d1be1612d8a032a9))
* update file handling logic to delete original files by default after optimization ([457c41d](https://github.com/twilsonco/Ploptimizer/commit/457c41d8f1bc3e38004e604b903de24b16b030dc))
* update rapid travel distance display in diagnostic plots for consistency ([a96404a](https://github.com/twilsonco/Ploptimizer/commit/a96404a851e71b98fdf3cbcb3ebc07e7927c8ef7))
* update strategy name references in diagnostics output ([74039d8](https://github.com/twilsonco/Ploptimizer/commit/74039d8fce010bd822062690373525c4e9627b61))
* use correct default paths for Windows ([86b9c7e](https://github.com/twilsonco/Ploptimizer/commit/86b9c7e049ec3e15f809ba0b856d7963477a681e))
* use optimized_doc.rapid_distance() for accurate rapid travel calculations ([3968607](https://github.com/twilsonco/Ploptimizer/commit/3968607a209c7ed47dd7ed81fae06f9db20381f6))
* **watch:** wait for input file to stop being modified, and use atomic writes to place full output file at once ([2ae3a0b](https://github.com/twilsonco/Ploptimizer/commit/2ae3a0b33ac59a609073caf00ac3ce06a8a4b789))
* Win7 support path ([f060732](https://github.com/twilsonco/Ploptimizer/commit/f060732ff7a0dc701f42385413f2d48c7a00f9d6))
* windows 7 support and prevent infinite spawn of exe ([e507abd](https://github.com/twilsonco/Ploptimizer/commit/e507abdd14354b9432f44ec8a5490889d1bafaae))
* **writer:** adhere to use of newlines when writing plt files ([d91f9b5](https://github.com/twilsonco/Ploptimizer/commit/d91f9b587ded1aebd7725ea9c9847e5fc631dbc5))


### Documentation

* add instructions for installing Visual C++ Redistributable for Windows users ([4f05a71](https://github.com/twilsonco/Ploptimizer/commit/4f05a718e69b66abee5796b782f50821e914af94))
* correct capitalization and improve description of Ploptimizer in README ([121106c](https://github.com/twilsonco/Ploptimizer/commit/121106c26fb017564a9cd28995ee6e32d3f43634))
* update AGENTS.md with Win7 compatability constraint ([a48da04](https://github.com/twilsonco/Ploptimizer/commit/a48da0483a549602aaa9f8538512f29410bdd97f))
* update readme and make `README_DEV.md` for more details ([1b99581](https://github.com/twilsonco/Ploptimizer/commit/1b99581df5d01758041dd29447b23003b73af81d))
* update README for Windows installation instructions and add standalone executable build steps; update pyproject.toml for build dependencies ([145b9d9](https://github.com/twilsonco/Ploptimizer/commit/145b9d920a8dea4c2b3ca6ee4179bdec6061b204))
* update readme with images. ([3433533](https://github.com/twilsonco/Ploptimizer/commit/34335335d03ee85f5c8b4182dd73fceec766b04d))
* update readme with windows instructions ([a85f68d](https://github.com/twilsonco/Ploptimizer/commit/a85f68dd8d1a722a7dc2a90c8d260a1ebf1e0ebf))
* update README with Windows-specific setup instructions and troubleshooting tips ([d14ea57](https://github.com/twilsonco/Ploptimizer/commit/d14ea5795e711981667f3c8b5aeb4ecb6741be47))

## [1.5.0](https://github.com/twilsonco/Ploptimizer/compare/v1.4.1...v1.5.0) (2026-07-12)


### Features

* add batch benchmark tool for strategies ([bae59ec](https://github.com/twilsonco/Ploptimizer/commit/bae59ec3523b52439ac9f6d8c80d8d27287b75d5))
* add Christofides-Serdyukov heuristic ([a66e4c6](https://github.com/twilsonco/Ploptimizer/commit/a66e4c6f209a9d619c33af14e3ee5842833b7adc))
* add CI/CD workflows for continuous integration and automated builds ([be0810b](https://github.com/twilsonco/Ploptimizer/commit/be0810b8acd572be46351fbb171f62dbbbdd9b04))
* add CSV output for strategy comparison results in diagnostics ([fb8f253](https://github.com/twilsonco/Ploptimizer/commit/fb8f253cf4997fd189115bca458b0cf3b41bd8c2))
* add debug file saving functionality with comparison plots in watch command ([417e802](https://github.com/twilsonco/Ploptimizer/commit/417e8026c2f1680d31b01b08995c3b4daaa403a6))
* Add diagnostic entries for hello-world.plt in logs and metrics ([716c3a9](https://github.com/twilsonco/Ploptimizer/commit/716c3a996d4ee8307b0085aa1996747df99caf70))
* add optimization time measurement to diagnostics output ([6ca5873](https://github.com/twilsonco/Ploptimizer/commit/6ca5873291ea17029c198a5045ce6d0d9c0aa7f0))
* add output validation for optimized PLT files and enhance PLTWriter with validation logic ([48bde5e](https://github.com/twilsonco/Ploptimizer/commit/48bde5e21675dd9f74d3f6fde29f8a7472fb6c87))
* add pre-commit hooks for linting and type checks; update build dependencies in uv.lock ([e2fdd6e](https://github.com/twilsonco/Ploptimizer/commit/e2fdd6ea0f46e10275d5407f6d39340164966157))
* add same row preference configuration to Chunker for improved stroke grouping ([6cb9264](https://github.com/twilsonco/Ploptimizer/commit/6cb92648d4d21afb636a76370bc90db0ab264916))
* add same-row preference to block optimization ([fec0e5a](https://github.com/twilsonco/Ploptimizer/commit/fec0e5a32322da0a293e268202c92ff4c06e84ea))
* add stroke simplification functionality and corresponding tests ([6632778](https://github.com/twilsonco/Ploptimizer/commit/6632778c5d455272c1f62196c6c21d6a0487735f))
* Add system tray application and GUI components; update README and pyproject.toml ([17195ee](https://github.com/twilsonco/Ploptimizer/commit/17195ee03c5e9315076d87a6b6443cd66b177471))
* add true arc length and chord length properties ([f96ae72](https://github.com/twilsonco/Ploptimizer/commit/f96ae723b43d6571e4a8b4d0dd5f92bfaa412a5b))
* **benchmark:** add parallel execution with ProcessPoolExecutor ([b1991b9](https://github.com/twilsonco/Ploptimizer/commit/b1991b915b3a529cd6ea40e0cce2f13bd081aa92))
* enable immediate shortcut update on startup toggle ([16131d5](https://github.com/twilsonco/Ploptimizer/commit/16131d5f43b66c3db151a80e52da4b39b5bfda83))
* enhance Christofides strategy for S-T Path TSP with improved approximation and traversal logic ([72b5d32](https://github.com/twilsonco/Ploptimizer/commit/72b5d3222a7a969a860f5fefc7b6b9cd0191f25a))
* enhance Christofides strategy with candidate endpoint evaluation for optimized S-T path ([a76468b](https://github.com/twilsonco/Ploptimizer/commit/a76468b8c62c7de437bd875e2dcc2040f6ab49d9))
* enhance chunking logic to support structural files with 1:1 mapping ([19150da](https://github.com/twilsonco/Ploptimizer/commit/19150da52f87731b31c91b734e95f8c6d432bc40))
* enhance CI/CD workflows and diagnostics module; update asset creation and dependency installation ([1f29d32](https://github.com/twilsonco/Ploptimizer/commit/1f29d3273f23ad4ac0bd64cf08cdd412029858db))
* enhance diagnostics by preserving original document for baseline calculations and improving optimization metrics reporting ([eb04ae6](https://github.com/twilsonco/Ploptimizer/commit/eb04ae61b67d246a46c432a92ae43b049583c370))
* enhance Genetic Algorithm strategy with improved fitness calculation and 2-opt refinement ([245b088](https://github.com/twilsonco/Ploptimizer/commit/245b088618c855f8f7e22ad536f2c40d20bd1a3a))
* enhance logging and error handling in tray UI ([cab06ee](https://github.com/twilsonco/Ploptimizer/commit/cab06eec2d4fe2275733375f7c2119ec27b746e9))
* enhance PLTWriter to validate distance preservation during tip-to-tail optimization ([34b0845](https://github.com/twilsonco/Ploptimizer/commit/34b08453813ba4f2994a2946f0b0b7e9cef9e3fb))
* enhance Profiler to classify structural paths with geometric analysis ([dad93f2](https://github.com/twilsonco/Ploptimizer/commit/dad93f26ac3f5cefadf7719141d6f4a5c008ce73))
* enhance stroke path formatting to track spindle position across paths ([8d173aa](https://github.com/twilsonco/Ploptimizer/commit/8d173aa10ad2e343f5c7772278f72a2c8f81e379))
* enhance stroke path formatting with segment-level state tracking ([c7e079f](https://github.com/twilsonco/Ploptimizer/commit/c7e079f121a40ab4f5145ddb739a5cd15cb8a410))
* enhance watch functionality with processed directory option and logging improvements ([e49a2f1](https://github.com/twilsonco/Ploptimizer/commit/e49a2f1e8c94f7ca16e2ecfb68baa616f2f93f7d))
* Implement chunking, optimization, profiling, and reassembly modules ([47b9e23](https://github.com/twilsonco/Ploptimizer/commit/47b9e23943c0fbe32a56d31c9c9e55bac14c4182))
* implement fracture_linear_paths function to decompose linear paths into segments ([dad93f2](https://github.com/twilsonco/Ploptimizer/commit/dad93f26ac3f5cefadf7719141d6f4a5c008ce73))
* Implement HPGL arc command parsing and rendering ([3801784](https://github.com/twilsonco/Ploptimizer/commit/3801784b3f43c9f98bdec9a3796bee602d12842b))
* implement settings modal UI and management ([fbec4a9](https://github.com/twilsonco/Ploptimizer/commit/fbec4a9011a683da20ac26d2669717a050510d01))
* implement strict identity test for no-op strategy and fix writer output formatting ([c087392](https://github.com/twilsonco/Ploptimizer/commit/c08739275ae6fce08da2ec4dc0b0d2ef434f1c3c))
* Implement system tray application with settings interface and background file watcher ([208e0aa](https://github.com/twilsonco/Ploptimizer/commit/208e0aa075e59df850b8bb25dd5d06d91ff11430))
* initial PLT-Optimizer project setup ([b5f592f](https://github.com/twilsonco/Ploptimizer/commit/b5f592ffc6ad60c3c7d76fcd92c803fa0eb40a14))
* initial release ([b17c3d5](https://github.com/twilsonco/Ploptimizer/commit/b17c3d5ae30fbfb1865ac225a86a1eca9fe368a5))
* other optimization strategies get farthest and nearest endpoint evaluations ([27c212e](https://github.com/twilsonco/Ploptimizer/commit/27c212e074eafc76ca49def537c862016b012241))
* **tray:** add detached mode and tkinter event loop ([e2677e3](https://github.com/twilsonco/Ploptimizer/commit/e2677e3642a801a385856be7dca3a6c8887579f7))
* **ui:** add cleanup section and improve directory dialog handling ([9eabe1a](https://github.com/twilsonco/Ploptimizer/commit/9eabe1ac3fde4ae562641a01afeaa0e908e92518))
* **ui:** add platform-specific systray impl for Windows ([76e8c94](https://github.com/twilsonco/Ploptimizer/commit/76e8c94393417a62e5931a05d95d664f6ed936ef))
* update Simulated Annealing and Genetic Algorithm strategies with new default parameters for improved optimization ([f92e587](https://github.com/twilsonco/Ploptimizer/commit/f92e5877a68058f68aa9d5110bcbe18e93fa8651))


### Bug Fixes

* add line to trigger tests ([6bae78e](https://github.com/twilsonco/Ploptimizer/commit/6bae78e8c4ac44de118f586ec7336d8e48255b1e))
* add log statement at startup ([8b916fa](https://github.com/twilsonco/Ploptimizer/commit/8b916faa328be3a0be8ee531a65565ef1c22c96e))
* add plotting extra to dependency installation in CI workflow ([2a778ec](https://github.com/twilsonco/Ploptimizer/commit/2a778ec54e28defb712384a3b3e91ea173369fcc))
* build Windows exe on every release ([3ab3e8f](https://github.com/twilsonco/Ploptimizer/commit/3ab3e8fe9523e48c1e68643e598ead1a72659d25))
* ci - ensure Windows 7 executable compatibility by using Python 3.8 and uv&lt;0.1.40 ([91ee6bc](https://github.com/twilsonco/Ploptimizer/commit/91ee6bcf6ed4479406df369b1d9aa9257137c937))
* ci build directory ([c5d11d7](https://github.com/twilsonco/Ploptimizer/commit/c5d11d73f0cc87ad83e7df19efcb123b6ba42a22))
* ci exe build ([435a531](https://github.com/twilsonco/Ploptimizer/commit/435a53107ba4d9afbeb0f57887f5ecb8de41cc53))
* ci exe build ([b22b1a6](https://github.com/twilsonco/Ploptimizer/commit/b22b1a650cacdadcb52ebb6bb7ddee4a9fbeb9c2))
* ci exe build ([137a602](https://github.com/twilsonco/Ploptimizer/commit/137a602dd798992ca20021ad91eb8126f865e3ba))
* ci optimization ([b2a04f8](https://github.com/twilsonco/Ploptimizer/commit/b2a04f8e5444e4cb7b83435088bb464c40c50fc3))
* ci optimization ([ed8f504](https://github.com/twilsonco/Ploptimizer/commit/ed8f50421bb2494c57b01db4b58a3c4f53e81282))
* ci optimization ([6a1053b](https://github.com/twilsonco/Ploptimizer/commit/6a1053bad2707daee1101dcc7adefab8b32d6097))
* ci optimization ([b7a77cf](https://github.com/twilsonco/Ploptimizer/commit/b7a77cfc2a9567c620cc8f04b305354593b0e70c))
* ci optimization ([92e02dd](https://github.com/twilsonco/Ploptimizer/commit/92e02dd3f8246f769cd2dd850fb8190bbf68175c))
* ci path filter ([fbc99ca](https://github.com/twilsonco/Ploptimizer/commit/fbc99caf628d8279ada46f1a298803136dca78e8))
* ci path filtering for lint/type-check/test ([b3f0382](https://github.com/twilsonco/Ploptimizer/commit/b3f0382efc0f88ccb6614afb79e6225dd6b9a3d1))
* cleanup unused code ([0176655](https://github.com/twilsonco/Ploptimizer/commit/0176655ffa71eb33537c20a3f8d9497c36cc0fce))
* convert distances to inches in diagnostic plots and summaries ([9d77a1a](https://github.com/twilsonco/Ploptimizer/commit/9d77a1a96131c9d335b5d8bb76a4b51d6497e305))
* Correct arc sweep angle calculations in parser and writer ([716c3a9](https://github.com/twilsonco/Ploptimizer/commit/716c3a996d4ee8307b0085aa1996747df99caf70))
* correct artifact paths for Windows executable in build workflow ([f706d62](https://github.com/twilsonco/Ploptimizer/commit/f706d62384237bbd542b1b99f215f818deedc4f6))
* correct YAML syntax for push tags in CI/CD workflows ([ab1ae91](https://github.com/twilsonco/Ploptimizer/commit/ab1ae916ab8f0a1f6f2c1ac2ec737163d549850c))
* default paths ([c72f8b0](https://github.com/twilsonco/Ploptimizer/commit/c72f8b08a8c5843f20846188543faa946c999f35))
* **diagnostics:** flip y-axis in plotter for correct orientation ([9ef192c](https://github.com/twilsonco/Ploptimizer/commit/9ef192c2a8d6f52debe2aeb5b5bd124ddd6dc838))
* improve genetic algorithm offspring generation logic for unique block indices ([6179c1c](https://github.com/twilsonco/Ploptimizer/commit/6179c1c10564ff10d010f4bf745b048ebb7cb107))
* only show settings automatically on first run (i.e. no config file exists) ([bb4b7a9](https://github.com/twilsonco/Ploptimizer/commit/bb4b7a9fb24aa21198894187d24bbefbcbc7194f))
* paths as strings ([f5ddf0b](https://github.com/twilsonco/Ploptimizer/commit/f5ddf0b37516bbc0fb9448ea6733ca1c16c4ceb1))
* **plt_optimizer:** fix insertion heuristic logic and costs ([1d54c89](https://github.com/twilsonco/Ploptimizer/commit/1d54c89bcfbf1cf7d2f7dfb625a6d91b22988ce7))
* preserve original document for accurate before-optimization statistics and plots ([f4ba99c](https://github.com/twilsonco/Ploptimizer/commit/f4ba99ce4ffbc5f25591e193a1cb24416771ae57))
* prevent infinite spawning of built exe ([722e91c](https://github.com/twilsonco/Ploptimizer/commit/722e91c684e557960ac0224d67ae4e6a02666a9e))
* replace __file__ with SPECPATH in PyInstaller spec ([67bed30](https://github.com/twilsonco/Ploptimizer/commit/67bed308e51d8aebacd724e315a705a7fda12412))
* replace *other* test file ([2c36429](https://github.com/twilsonco/Ploptimizer/commit/2c3642984ae7896ce604d97648a1a1dcd72caf82))
* replace test plt file ([fb4b87b](https://github.com/twilsonco/Ploptimizer/commit/fb4b87b84f043f0c68990346fbe2d6bd8b5baa09))
* skip-duplicate-actions path filter causing false skips ([2e27964](https://github.com/twilsonco/Ploptimizer/commit/2e2796422f329a8373fa9de9cab7e95d9280f5b6))
* startup logging to trigger test ([f7685ec](https://github.com/twilsonco/Ploptimizer/commit/f7685ec26f29a3740679154bc4a418c2f35ca98c))
* **test:** correct inverted skip markers on Windows-only tests ([cef9134](https://github.com/twilsonco/Ploptimizer/commit/cef91349ce644031a72e1e892a51808a07e8fc7c))
* tray icon and config directory string field widths ([bdf7d4b](https://github.com/twilsonco/Ploptimizer/commit/bdf7d4b9847135834bb61fb5c5e87aa2e7a5f8c9))
* **tray:** avoid deadlock on infi.systray shutdown ([7410665](https://github.com/twilsonco/Ploptimizer/commit/74106657ca67bb6b84c2e92c75872986cd31df81))
* **tray:** fix signal handling and settings window loop ([0d07aa9](https://github.com/twilsonco/Ploptimizer/commit/0d07aa9de2851ab08756baba780752b6ccdf9a28))
* **tray:** skip initial settings dialog and use default watch dir ([cd4d9aa](https://github.com/twilsonco/Ploptimizer/commit/cd4d9aa3f863bb1f02890d98be7c1e6dbe06ee33))
* **tray:** switch to blocking mode for pystray ([f5e42b9](https://github.com/twilsonco/Ploptimizer/commit/f5e42b90f36e745a407e17a9318a6df6b3695f3d))
* typehint errors ([d9686a2](https://github.com/twilsonco/Ploptimizer/commit/d9686a26db70ded1047e2c16a7cb2b0c5dca270f))
* **ui, startup:** handle missing optional dependencies gracefully ([0122b71](https://github.com/twilsonco/Ploptimizer/commit/0122b7184bdaefe16a1bdf949ea22eb3ed3f1be9))
* **ui:** fix dialog UX and tray termination ([1cc3b6e](https://github.com/twilsonco/Ploptimizer/commit/1cc3b6ee24eb315bf88127dffbf0cd4bc4b79734))
* **ui:** fix filedialog focus issues with withdrawn window ([50ef18f](https://github.com/twilsonco/Ploptimizer/commit/50ef18f2459a94e8a8c0834fc644c79c30eac903))
* **ui:** fix settings window modal and crash issues ([0ea98d2](https://github.com/twilsonco/Ploptimizer/commit/0ea98d2034e18a8d0a8dfec02244988a18e0b792))
* **ui:** improve window centering and focus handling ([3baef93](https://github.com/twilsonco/Ploptimizer/commit/3baef930957a96cea0b8236ca3758d7e0e4f4e9f))
* **ui:** safely handle network path errors ([6bca37a](https://github.com/twilsonco/Ploptimizer/commit/6bca37a8431b18e644a430fbea8bfd749b1e52b6))
* **ui:** skip grab_set to fix pystray conflict ([a58c6be](https://github.com/twilsonco/Ploptimizer/commit/a58c6be05f2f3fb4eb3863fb53d6c975bde314cc))
* update build workflow to correctly handle executable paths and options ([28551af](https://github.com/twilsonco/Ploptimizer/commit/28551afc9ec1030bbb75c730d1be1612d8a032a9))
* update file handling logic to delete original files by default after optimization ([457c41d](https://github.com/twilsonco/Ploptimizer/commit/457c41d8f1bc3e38004e604b903de24b16b030dc))
* update rapid travel distance display in diagnostic plots for consistency ([a96404a](https://github.com/twilsonco/Ploptimizer/commit/a96404a851e71b98fdf3cbcb3ebc07e7927c8ef7))
* update strategy name references in diagnostics output ([74039d8](https://github.com/twilsonco/Ploptimizer/commit/74039d8fce010bd822062690373525c4e9627b61))
* use correct default paths for Windows ([86b9c7e](https://github.com/twilsonco/Ploptimizer/commit/86b9c7e049ec3e15f809ba0b856d7963477a681e))
* use optimized_doc.rapid_distance() for accurate rapid travel calculations ([3968607](https://github.com/twilsonco/Ploptimizer/commit/3968607a209c7ed47dd7ed81fae06f9db20381f6))
* **watch:** wait for input file to stop being modified, and use atomic writes to place full output file at once ([2ae3a0b](https://github.com/twilsonco/Ploptimizer/commit/2ae3a0b33ac59a609073caf00ac3ce06a8a4b789))
* Win7 support path ([f060732](https://github.com/twilsonco/Ploptimizer/commit/f060732ff7a0dc701f42385413f2d48c7a00f9d6))
* windows 7 support and prevent infinite spawn of exe ([e507abd](https://github.com/twilsonco/Ploptimizer/commit/e507abdd14354b9432f44ec8a5490889d1bafaae))
* **writer:** adhere to use of newlines when writing plt files ([d91f9b5](https://github.com/twilsonco/Ploptimizer/commit/d91f9b587ded1aebd7725ea9c9847e5fc631dbc5))


### Documentation

* add instructions for installing Visual C++ Redistributable for Windows users ([4f05a71](https://github.com/twilsonco/Ploptimizer/commit/4f05a718e69b66abee5796b782f50821e914af94))
* correct capitalization and improve description of Ploptimizer in README ([121106c](https://github.com/twilsonco/Ploptimizer/commit/121106c26fb017564a9cd28995ee6e32d3f43634))
* update readme and make `README_DEV.md` for more details ([1b99581](https://github.com/twilsonco/Ploptimizer/commit/1b99581df5d01758041dd29447b23003b73af81d))
* update README for Windows installation instructions and add standalone executable build steps; update pyproject.toml for build dependencies ([145b9d9](https://github.com/twilsonco/Ploptimizer/commit/145b9d920a8dea4c2b3ca6ee4179bdec6061b204))
* update readme with images. ([3433533](https://github.com/twilsonco/Ploptimizer/commit/34335335d03ee85f5c8b4182dd73fceec766b04d))
* update readme with windows instructions ([a85f68d](https://github.com/twilsonco/Ploptimizer/commit/a85f68dd8d1a722a7dc2a90c8d260a1ebf1e0ebf))
* update README with Windows-specific setup instructions and troubleshooting tips ([d14ea57](https://github.com/twilsonco/Ploptimizer/commit/d14ea5795e711981667f3c8b5aeb4ecb6741be47))

## [1.4.1](https://github.com/twilsonco/Ploptimizer/compare/v1.4.0...v1.4.1) (2026-07-11)


### Bug Fixes

* prevent infinite spawning of built exe ([722e91c](https://github.com/twilsonco/Ploptimizer/commit/722e91c684e557960ac0224d67ae4e6a02666a9e))
* typehint errors ([d9686a2](https://github.com/twilsonco/Ploptimizer/commit/d9686a26db70ded1047e2c16a7cb2b0c5dca270f))
* **watch:** wait for input file to stop being modified, and use atomic writes to place full output file at once ([2ae3a0b](https://github.com/twilsonco/Ploptimizer/commit/2ae3a0b33ac59a609073caf00ac3ce06a8a4b789))
* Windows 7 support and prevent infinite spawn of exe ([e507abd](https://github.com/twilsonco/Ploptimizer/commit/e507abdd14354b9432f44ec8a5490889d1bafaae))

## [1.4.0](https://github.com/twilsonco/Ploptimizer/compare/v1.3.1...v1.4.0) (2026-07-06)


### Features

* enable immediate shortcut update on startup toggle ([16131d5](https://github.com/twilsonco/Ploptimizer/commit/16131d5f43b66c3db151a80e52da4b39b5bfda83))

## [1.3.1](https://github.com/twilsonco/Ploptimizer/compare/v1.3.0...v1.3.1) (2026-07-06)


### Bug Fixes

* replace *other* test file ([2c36429](https://github.com/twilsonco/Ploptimizer/commit/2c3642984ae7896ce604d97648a1a1dcd72caf82))
* replace test plt file ([fb4b87b](https://github.com/twilsonco/Ploptimizer/commit/fb4b87b84f043f0c68990346fbe2d6bd8b5baa09))


### Documentation

* update readme with images. ([3433533](https://github.com/twilsonco/Ploptimizer/commit/34335335d03ee85f5c8b4182dd73fceec766b04d))

## [1.3.0](https://github.com/twilsonco/Ploptimizer/compare/v1.2.15...v1.3.0) (2026-07-06)


### Features

* add batch benchmark tool for strategies ([bae59ec](https://github.com/twilsonco/Ploptimizer/commit/bae59ec3523b52439ac9f6d8c80d8d27287b75d5))
* add Christofides-Serdyukov heuristic ([a66e4c6](https://github.com/twilsonco/Ploptimizer/commit/a66e4c6f209a9d619c33af14e3ee5842833b7adc))
* add true arc length and chord length properties ([f96ae72](https://github.com/twilsonco/Ploptimizer/commit/f96ae723b43d6571e4a8b4d0dd5f92bfaa412a5b))
* **benchmark:** add parallel execution with ProcessPoolExecutor ([b1991b9](https://github.com/twilsonco/Ploptimizer/commit/b1991b915b3a529cd6ea40e0cce2f13bd081aa92))


### Bug Fixes

* **plt_optimizer:** fix insertion heuristic logic and costs ([1d54c89](https://github.com/twilsonco/Ploptimizer/commit/1d54c89bcfbf1cf7d2f7dfb625a6d91b22988ce7))

## [1.2.15](https://github.com/twilsonco/Ploptimizer/compare/v1.2.14...v1.2.15) (2026-07-04)


### Bug Fixes

* ci build directory ([c5d11d7](https://github.com/twilsonco/Ploptimizer/commit/c5d11d73f0cc87ad83e7df19efcb123b6ba42a22))

## [1.2.14](https://github.com/twilsonco/Ploptimizer/compare/v1.2.13...v1.2.14) (2026-07-04)


### Bug Fixes

* ci exe build ([435a531](https://github.com/twilsonco/Ploptimizer/commit/435a53107ba4d9afbeb0f57887f5ecb8de41cc53))
* only show settings automatically on first run (i.e. no config file exists) ([bb4b7a9](https://github.com/twilsonco/Ploptimizer/commit/bb4b7a9fb24aa21198894187d24bbefbcbc7194f))

## [1.2.13](https://github.com/twilsonco/Ploptimizer/compare/v1.2.12...v1.2.13) (2026-07-03)


### Bug Fixes

* tray icon and config directory string field widths ([bdf7d4b](https://github.com/twilsonco/Ploptimizer/commit/bdf7d4b9847135834bb61fb5c5e87aa2e7a5f8c9))

## [1.2.12](https://github.com/twilsonco/Ploptimizer/compare/v1.2.11...v1.2.12) (2026-07-03)


### Bug Fixes

* default paths ([c72f8b0](https://github.com/twilsonco/Ploptimizer/commit/c72f8b08a8c5843f20846188543faa946c999f35))
* paths as strings ([f5ddf0b](https://github.com/twilsonco/Ploptimizer/commit/f5ddf0b37516bbc0fb9448ea6733ca1c16c4ceb1))
* use correct default paths for Windows ([86b9c7e](https://github.com/twilsonco/Ploptimizer/commit/86b9c7e049ec3e15f809ba0b856d7963477a681e))

## [1.2.11](https://github.com/twilsonco/Ploptimizer/compare/v1.2.10...v1.2.11) (2026-07-03)


### Bug Fixes

* build Windows exe on every release ([3ab3e8f](https://github.com/twilsonco/Ploptimizer/commit/3ab3e8fe9523e48c1e68643e598ead1a72659d25))

## [1.2.10](https://github.com/twilsonco/Ploptimizer/compare/v1.2.9...v1.2.10) (2026-07-03)


### Bug Fixes

* cleanup unused code ([0176655](https://github.com/twilsonco/Ploptimizer/commit/0176655ffa71eb33537c20a3f8d9497c36cc0fce))

## [1.2.9](https://github.com/twilsonco/Ploptimizer/compare/v1.2.8...v1.2.9) (2026-07-03)


### Documentation

* update readme and make `README_DEV.md` for more details ([1b99581](https://github.com/twilsonco/Ploptimizer/commit/1b99581df5d01758041dd29447b23003b73af81d))

## [1.2.8](https://github.com/twilsonco/Ploptimizer/compare/v1.2.7...v1.2.8) (2026-07-03)


### Bug Fixes

* ci path filtering for lint/type-check/test ([b3f0382](https://github.com/twilsonco/Ploptimizer/commit/b3f0382efc0f88ccb6614afb79e6225dd6b9a3d1))

## [1.2.7](https://github.com/twilsonco/Ploptimizer/compare/v1.2.6...v1.2.7) (2026-07-03)


### Bug Fixes

* ci optimization ([b2a04f8](https://github.com/twilsonco/Ploptimizer/commit/b2a04f8e5444e4cb7b83435088bb464c40c50fc3))

## [1.2.6](https://github.com/twilsonco/Ploptimizer/compare/v1.2.5...v1.2.6) (2026-07-03)


### Bug Fixes

* ci optimization ([ed8f504](https://github.com/twilsonco/Ploptimizer/commit/ed8f50421bb2494c57b01db4b58a3c4f53e81282))

## [1.2.5](https://github.com/twilsonco/Ploptimizer/compare/v1.2.4...v1.2.5) (2026-07-03)


### Bug Fixes

* ci exe build ([b22b1a6](https://github.com/twilsonco/Ploptimizer/commit/b22b1a650cacdadcb52ebb6bb7ddee4a9fbeb9c2))

## [1.2.4](https://github.com/twilsonco/Ploptimizer/compare/v1.2.3...v1.2.4) (2026-07-03)


### Bug Fixes

* ci exe build ([137a602](https://github.com/twilsonco/Ploptimizer/commit/137a602dd798992ca20021ad91eb8126f865e3ba))

## [1.2.3](https://github.com/twilsonco/Ploptimizer/compare/v1.2.2...v1.2.3) (2026-07-03)


### Bug Fixes

* ci optimization ([6a1053b](https://github.com/twilsonco/Ploptimizer/commit/6a1053bad2707daee1101dcc7adefab8b32d6097))

## [1.2.2](https://github.com/twilsonco/Ploptimizer/compare/v1.2.1...v1.2.2) (2026-07-03)


### Bug Fixes

* add line to trigger tests ([6bae78e](https://github.com/twilsonco/Ploptimizer/commit/6bae78e8c4ac44de118f586ec7336d8e48255b1e))
* ci path filter ([fbc99ca](https://github.com/twilsonco/Ploptimizer/commit/fbc99caf628d8279ada46f1a298803136dca78e8))
* skip-duplicate-actions path filter causing false skips ([2e27964](https://github.com/twilsonco/Ploptimizer/commit/2e2796422f329a8373fa9de9cab7e95d9280f5b6))
* startup logging to trigger test ([f7685ec](https://github.com/twilsonco/Ploptimizer/commit/f7685ec26f29a3740679154bc4a418c2f35ca98c))

## [1.2.1](https://github.com/twilsonco/Ploptimizer/compare/v1.2.0...v1.2.1) (2026-07-03)


### Bug Fixes

* add log statement at startup ([8b916fa](https://github.com/twilsonco/Ploptimizer/commit/8b916faa328be3a0be8ee531a65565ef1c22c96e))
* ci optimization ([b7a77cf](https://github.com/twilsonco/Ploptimizer/commit/b7a77cfc2a9567c620cc8f04b305354593b0e70c))
* ci optimization ([92e02dd](https://github.com/twilsonco/Ploptimizer/commit/92e02dd3f8246f769cd2dd850fb8190bbf68175c))

## [1.2.0](https://github.com/twilsonco/Ploptimizer/compare/v1.1.0...v1.2.0) (2026-07-02)


### Features

* initial release ([b17c3d5](https://github.com/twilsonco/Ploptimizer/commit/b17c3d5ae30fbfb1865ac225a86a1eca9fe368a5))

## [1.1.0](https://github.com/twilsonco/Ploptimizer/compare/v1.0.0...v1.1.0) (2026-07-02)


### Features

* add CI/CD workflows for continuous integration and automated builds ([be0810b](https://github.com/twilsonco/Ploptimizer/commit/be0810b8acd572be46351fbb171f62dbbbdd9b04))
* add CSV output for strategy comparison results in diagnostics ([fb8f253](https://github.com/twilsonco/Ploptimizer/commit/fb8f253cf4997fd189115bca458b0cf3b41bd8c2))
* add debug file saving functionality with comparison plots in watch command ([417e802](https://github.com/twilsonco/Ploptimizer/commit/417e8026c2f1680d31b01b08995c3b4daaa403a6))
* Add diagnostic entries for hello-world.plt in logs and metrics ([716c3a9](https://github.com/twilsonco/Ploptimizer/commit/716c3a996d4ee8307b0085aa1996747df99caf70))
* add optimization time measurement to diagnostics output ([6ca5873](https://github.com/twilsonco/Ploptimizer/commit/6ca5873291ea17029c198a5045ce6d0d9c0aa7f0))
* add output validation for optimized PLT files and enhance PLTWriter with validation logic ([48bde5e](https://github.com/twilsonco/Ploptimizer/commit/48bde5e21675dd9f74d3f6fde29f8a7472fb6c87))
* add pre-commit hooks for linting and type checks; update build dependencies in uv.lock ([e2fdd6e](https://github.com/twilsonco/Ploptimizer/commit/e2fdd6ea0f46e10275d5407f6d39340164966157))
* add same row preference configuration to Chunker for improved stroke grouping ([6cb9264](https://github.com/twilsonco/Ploptimizer/commit/6cb92648d4d21afb636a76370bc90db0ab264916))
* add same-row preference to block optimization ([fec0e5a](https://github.com/twilsonco/Ploptimizer/commit/fec0e5a32322da0a293e268202c92ff4c06e84ea))
* add stroke simplification functionality and corresponding tests ([6632778](https://github.com/twilsonco/Ploptimizer/commit/6632778c5d455272c1f62196c6c21d6a0487735f))
* Add system tray application and GUI components; update README and pyproject.toml ([17195ee](https://github.com/twilsonco/Ploptimizer/commit/17195ee03c5e9315076d87a6b6443cd66b177471))
* enhance Christofides strategy for S-T Path TSP with improved approximation and traversal logic ([72b5d32](https://github.com/twilsonco/Ploptimizer/commit/72b5d3222a7a969a860f5fefc7b6b9cd0191f25a))
* enhance Christofides strategy with candidate endpoint evaluation for optimized S-T path ([a76468b](https://github.com/twilsonco/Ploptimizer/commit/a76468b8c62c7de437bd875e2dcc2040f6ab49d9))
* enhance chunking logic to support structural files with 1:1 mapping ([19150da](https://github.com/twilsonco/Ploptimizer/commit/19150da52f87731b31c91b734e95f8c6d432bc40))
* enhance CI/CD workflows and diagnostics module; update asset creation and dependency installation ([1f29d32](https://github.com/twilsonco/Ploptimizer/commit/1f29d3273f23ad4ac0bd64cf08cdd412029858db))
* enhance diagnostics by preserving original document for baseline calculations and improving optimization metrics reporting ([eb04ae6](https://github.com/twilsonco/Ploptimizer/commit/eb04ae61b67d246a46c432a92ae43b049583c370))
* enhance Genetic Algorithm strategy with improved fitness calculation and 2-opt refinement ([245b088](https://github.com/twilsonco/Ploptimizer/commit/245b088618c855f8f7e22ad536f2c40d20bd1a3a))
* enhance logging and error handling in tray UI ([cab06ee](https://github.com/twilsonco/Ploptimizer/commit/cab06eec2d4fe2275733375f7c2119ec27b746e9))
* enhance PLTWriter to validate distance preservation during tip-to-tail optimization ([34b0845](https://github.com/twilsonco/Ploptimizer/commit/34b08453813ba4f2994a2946f0b0b7e9cef9e3fb))
* enhance Profiler to classify structural paths with geometric analysis ([dad93f2](https://github.com/twilsonco/Ploptimizer/commit/dad93f26ac3f5cefadf7719141d6f4a5c008ce73))
* enhance stroke path formatting to track spindle position across paths ([8d173aa](https://github.com/twilsonco/Ploptimizer/commit/8d173aa10ad2e343f5c7772278f72a2c8f81e379))
* enhance stroke path formatting with segment-level state tracking ([c7e079f](https://github.com/twilsonco/Ploptimizer/commit/c7e079f121a40ab4f5145ddb739a5cd15cb8a410))
* enhance watch functionality with processed directory option and logging improvements ([e49a2f1](https://github.com/twilsonco/Ploptimizer/commit/e49a2f1e8c94f7ca16e2ecfb68baa616f2f93f7d))
* Implement chunking, optimization, profiling, and reassembly modules ([47b9e23](https://github.com/twilsonco/Ploptimizer/commit/47b9e23943c0fbe32a56d31c9c9e55bac14c4182))
* implement fracture_linear_paths function to decompose linear paths into segments ([dad93f2](https://github.com/twilsonco/Ploptimizer/commit/dad93f26ac3f5cefadf7719141d6f4a5c008ce73))
* Implement HPGL arc command parsing and rendering ([3801784](https://github.com/twilsonco/Ploptimizer/commit/3801784b3f43c9f98bdec9a3796bee602d12842b))
* implement settings modal UI and management ([fbec4a9](https://github.com/twilsonco/Ploptimizer/commit/fbec4a9011a683da20ac26d2669717a050510d01))
* Implement system tray application with settings interface and background file watcher ([208e0aa](https://github.com/twilsonco/Ploptimizer/commit/208e0aa075e59df850b8bb25dd5d06d91ff11430))
* initial PLT-Optimizer project setup ([b5f592f](https://github.com/twilsonco/Ploptimizer/commit/b5f592ffc6ad60c3c7d76fcd92c803fa0eb40a14))
* other optimization strategies get farthest and nearest endpoint evaluations ([27c212e](https://github.com/twilsonco/Ploptimizer/commit/27c212e074eafc76ca49def537c862016b012241))
* **tray:** add detached mode and tkinter event loop ([e2677e3](https://github.com/twilsonco/Ploptimizer/commit/e2677e3642a801a385856be7dca3a6c8887579f7))
* **ui:** add cleanup section and improve directory dialog handling ([9eabe1a](https://github.com/twilsonco/Ploptimizer/commit/9eabe1ac3fde4ae562641a01afeaa0e908e92518))
* **ui:** add platform-specific systray impl for Windows ([76e8c94](https://github.com/twilsonco/Ploptimizer/commit/76e8c94393417a62e5931a05d95d664f6ed936ef))
* update Simulated Annealing and Genetic Algorithm strategies with new default parameters for improved optimization ([f92e587](https://github.com/twilsonco/Ploptimizer/commit/f92e5877a68058f68aa9d5110bcbe18e93fa8651))


### Bug Fixes

* add plotting extra to dependency installation in CI workflow ([2a778ec](https://github.com/twilsonco/Ploptimizer/commit/2a778ec54e28defb712384a3b3e91ea173369fcc))
* convert distances to inches in diagnostic plots and summaries ([9d77a1a](https://github.com/twilsonco/Ploptimizer/commit/9d77a1a96131c9d335b5d8bb76a4b51d6497e305))
* Correct arc sweep angle calculations in parser and writer ([716c3a9](https://github.com/twilsonco/Ploptimizer/commit/716c3a996d4ee8307b0085aa1996747df99caf70))
* correct artifact paths for Windows executable in build workflow ([f706d62](https://github.com/twilsonco/Ploptimizer/commit/f706d62384237bbd542b1b99f215f818deedc4f6))
* correct YAML syntax for push tags in CI/CD workflows ([ab1ae91](https://github.com/twilsonco/Ploptimizer/commit/ab1ae916ab8f0a1f6f2c1ac2ec737163d549850c))
* **diagnostics:** flip y-axis in plotter for correct orientation ([9ef192c](https://github.com/twilsonco/Ploptimizer/commit/9ef192c2a8d6f52debe2aeb5b5bd124ddd6dc838))
* improve genetic algorithm offspring generation logic for unique block indices ([6179c1c](https://github.com/twilsonco/Ploptimizer/commit/6179c1c10564ff10d010f4bf745b048ebb7cb107))
* preserve original document for accurate before-optimization statistics and plots ([f4ba99c](https://github.com/twilsonco/Ploptimizer/commit/f4ba99ce4ffbc5f25591e193a1cb24416771ae57))
* replace __file__ with SPECPATH in PyInstaller spec ([67bed30](https://github.com/twilsonco/Ploptimizer/commit/67bed308e51d8aebacd724e315a705a7fda12412))
* **test:** correct inverted skip markers on Windows-only tests ([cef9134](https://github.com/twilsonco/Ploptimizer/commit/cef91349ce644031a72e1e892a51808a07e8fc7c))
* **tray:** avoid deadlock on infi.systray shutdown ([7410665](https://github.com/twilsonco/Ploptimizer/commit/74106657ca67bb6b84c2e92c75872986cd31df81))
* **tray:** fix signal handling and settings window loop ([0d07aa9](https://github.com/twilsonco/Ploptimizer/commit/0d07aa9de2851ab08756baba780752b6ccdf9a28))
* **tray:** skip initial settings dialog and use default watch dir ([cd4d9aa](https://github.com/twilsonco/Ploptimizer/commit/cd4d9aa3f863bb1f02890d98be7c1e6dbe06ee33))
* **tray:** switch to blocking mode for pystray ([f5e42b9](https://github.com/twilsonco/Ploptimizer/commit/f5e42b90f36e745a407e17a9318a6df6b3695f3d))
* **ui, startup:** handle missing optional dependencies gracefully ([0122b71](https://github.com/twilsonco/Ploptimizer/commit/0122b7184bdaefe16a1bdf949ea22eb3ed3f1be9))
* **ui:** fix dialog UX and tray termination ([1cc3b6e](https://github.com/twilsonco/Ploptimizer/commit/1cc3b6ee24eb315bf88127dffbf0cd4bc4b79734))
* **ui:** fix filedialog focus issues with withdrawn window ([50ef18f](https://github.com/twilsonco/Ploptimizer/commit/50ef18f2459a94e8a8c0834fc644c79c30eac903))
* **ui:** fix settings window modal and crash issues ([0ea98d2](https://github.com/twilsonco/Ploptimizer/commit/0ea98d2034e18a8d0a8dfec02244988a18e0b792))
* **ui:** improve window centering and focus handling ([3baef93](https://github.com/twilsonco/Ploptimizer/commit/3baef930957a96cea0b8236ca3758d7e0e4f4e9f))
* **ui:** safely handle network path errors ([6bca37a](https://github.com/twilsonco/Ploptimizer/commit/6bca37a8431b18e644a430fbea8bfd749b1e52b6))
* **ui:** skip grab_set to fix pystray conflict ([a58c6be](https://github.com/twilsonco/Ploptimizer/commit/a58c6be05f2f3fb4eb3863fb53d6c975bde314cc))
* update build workflow to correctly handle executable paths and options ([28551af](https://github.com/twilsonco/Ploptimizer/commit/28551afc9ec1030bbb75c730d1be1612d8a032a9))
* update file handling logic to delete original files by default after optimization ([457c41d](https://github.com/twilsonco/Ploptimizer/commit/457c41d8f1bc3e38004e604b903de24b16b030dc))
* update rapid travel distance display in diagnostic plots for consistency ([a96404a](https://github.com/twilsonco/Ploptimizer/commit/a96404a851e71b98fdf3cbcb3ebc07e7927c8ef7))
* update strategy name references in diagnostics output ([74039d8](https://github.com/twilsonco/Ploptimizer/commit/74039d8fce010bd822062690373525c4e9627b61))
* use optimized_doc.rapid_distance() for accurate rapid travel calculations ([3968607](https://github.com/twilsonco/Ploptimizer/commit/3968607a209c7ed47dd7ed81fae06f9db20381f6))
* Win7 support path ([f060732](https://github.com/twilsonco/Ploptimizer/commit/f060732ff7a0dc701f42385413f2d48c7a00f9d6))


### Documentation

* add instructions for installing Visual C++ Redistributable for Windows users ([4f05a71](https://github.com/twilsonco/Ploptimizer/commit/4f05a718e69b66abee5796b782f50821e914af94))
* correct capitalization and improve description of Ploptimizer in README ([121106c](https://github.com/twilsonco/Ploptimizer/commit/121106c26fb017564a9cd28995ee6e32d3f43634))
* update README for Windows installation instructions and add standalone executable build steps; update pyproject.toml for build dependencies ([145b9d9](https://github.com/twilsonco/Ploptimizer/commit/145b9d920a8dea4c2b3ca6ee4179bdec6061b204))
* update readme with windows instructions ([a85f68d](https://github.com/twilsonco/Ploptimizer/commit/a85f68dd8d1a722a7dc2a90c8d260a1ebf1e0ebf))
* update README with Windows-specific setup instructions and troubleshooting tips ([d14ea57](https://github.com/twilsonco/Ploptimizer/commit/d14ea5795e711981667f3c8b5aeb4ecb6741be47))
