# AGENTS.md - Core HPGL Pipeline (`plt_optimizer/core/`)

Parent contract: [`../../AGENTS.md`](../../AGENTS.md) (coding standards, testing,
git rules, the 1016 device-scale invariant, the Python 3.8 / Windows 7
constraint). This directory is the parse → profile → chunk → optimize →
reassemble → write pipeline shared by the `optimize` CLI, the `watch` daemon,
the `benchmark` tool, and the generate pipeline's plate-space optimization
(`../generate/plate_optimizer.py` calls into `pipeline.py`).

## Module map

- `models.py` - frozen dataclasses for parsed PLT structure (`PLTDocument`,
  `StrokePath`, `LineSeg`, `ArcSeg`, `Coordinate`); precision kept to 3 decimal
  places as required by the EngraveLab format.
- `parser.py` / `writer.py` - HPGL tokenization / emission. Any change must pass
  the identity validation suite (`input.plt -> parse -> write -> output.plt`
  semantic equivalence, root AGENTS.md §2). The parser treats bare `PA` as
  header and `SP` as footer; `extract_pens_from_plt_text` (generate side)
  rewrites section-leading bare `PD` to `PU…;PD…` so dropped pen-selects never
  join sections with a phantom cut.
- `units.py` - THE device scale: `HPGL_UNITS_PER_INCH = 1016`,
  `inches_to_units()` / `units_to_inches()`. Every inch↔coordinate conversion on
  the emission/read-back path goes through here; never hardcode 1000 (root
  AGENTS.md §4; pinned by `tests/test_units.py` +
  `tests/test_phase3_export.py::TestDeviceScaleRegression`).
- `filenames.py` - THE output-filename contract (cap **and** character rules):
  `MAX_PLOT_FILENAME_CHARS = 39` + `ensure_plot_filename_length(path, logger)`
  (stem shortened from the right, extension preserved; a stem truncated onto a
  Windows device name is de-reserved in place) and
  `sanitize_filename_component(text, fallback)` (NFKD transliteration so
  international characters become ASCII, every punctuation run collapsed to a
  single underscore, Windows reserved names guarded). Every filename component
  the pipelines generate goes through the sanitizer; the cap is applied by
  `writer.PLTWriter._ensure_filename_length` (optimize/watch) and the
  `generate/vectorize` PLT write sites - the controller errors on longer
  names; pinned by `tests/test_filenames.py`.
- `profiler.py` - baseline extent (95th percentile, not max) + structural
  classification.
- `chunker.py` - chronological clustering of strokes into `MacroBlock`s by
  tool-up jump distance; never spatially pre-sorts (preserves text entry order).
  `ChunkerConfig.same_row_preference`; only `NN2OptStrategy` accepts it.
- `optimizer.py` - Strategy Pattern over MacroBlock traversal
  (`NearestNeighbor2Opt`, insertion, Christofides, SA, genetic,
  `ParallelEnsembleStrategy`). `OptimizerEngine.optimize` passes the ensemble
  result through - callers must unwrap `.result` before
  `Reassembler.reassemble` (see `../cli/watch.py`).
- `intra_chunk_optimizer.py` - within-block order/direction optimization with
  fixed entrance/exit.
- `direction_sweep.py` - post-TSP per-block forward/reverse re-pick (always on,
  `direction_sweep=True` escape hatch); monotone, repairs 2-opt flag staleness.
- `glyph_sweep.py` - intra-chunk per-glyph direction DP (generate path only;
  parsed PLTs carry no glyph knowledge and stay byte-identical).
- `path_merger.py` - tip-to-tail coincident-stroke merge (final stage,
  `merge_coincident=True`); metric-neutral (win = PU/path count, not travel).
- `stroke_simplifier.py` - deterministic symmetric interval-splitting dedupe of
  overlapping linear strokes.
- `reassembler.py` - traversal application + stroke reversal; deliberately a
  pure applier (the merge is NOT inside it).
- `pipeline.py` - the shared stage sequence
  (`preprocess_document` → `chunk_document` → `optimize_and_reassemble`).
  `optimize_and_reassemble(stage_timers=True)` (default `False`) emits the
  `stage <name>: <s>s` INFO timing lines for its inner stages
  (`optimize_tsp`, `direction_sweep`, `intra_sweep`, `reassemble`, `merge`)
  via `../utils/stage_timing.py` - log-only, off by default so the watch
  daemon's and generate's log streams stay byte-identical; the `optimize`
  CLI and `scripts/profile_optimize.py` turn them on (pinned by
  `tests/test_pipeline.py::TestStageTimers`).

## Invariants

- **Python 3.8 / Windows 7:** this package is on the watch/optimize hot path -
  no syntax or stdlib newer than 3.8, no matplotlib. `ProcessPoolExecutor`
  `cancel_futures=` is 3.9+ and banned; `as_completed(timeout=)` is OK.
  `ParallelEnsembleStrategy` uses a spawn context on win32/frozen; workers must
  stay module-level/picklable; all-fail fallback is serial NN2Opt → NoOp.
- **Ensemble timeout:** per-job timeout (default 10 s) per strategy; timeout =
  marked failed; every-fail = serial NN2Opt fallback. Unused on the sequential
  path (see next bullet) — safe because every strategy is polynomially bounded
  at sequential sizes.
- **Ensemble small layers run in-process:** layers with at most
  `ENSEMBLE_SEQUENTIAL_BLOCK_THRESHOLD` (48) blocks run every strategy job
  through `_run_strategy_worker` in the parent (no pool spawn — the per-layer
  pool costs seconds on the Windows spawn context for milliseconds of work);
  large layers stay pooled (a 481-block layer is ~35 s sequential vs ~10 s
  pooled). `sequential_block_threshold=0` forces the pool (pool-mechanics
  tests pin it). Both paths share `_finalize_result` (selection, fallback
  chain, benchmark table).
- **Ensemble selection is a canonical total order:** `_benchmark_sort_key`
  (improvement desc, distance asc, strategy name, job start/end terminals —
  never `execution_time_seconds`) drives `_select_best_result` and the
  displayed table, so exact-distance ties resolve identically regardless of
  `as_completed` arrival order (the identical-input-identical-output
  invariant). Known open gap: SA/GA use the unseeded global `random`, so a
  stochastic winner's tour varies run to run (deterministic seeding is a
  tracked follow-up).
- **Cheapest-insertion is incremental (O(n^2), bit-identical):**
  `InsertionHeuristicStrategy.optimize` caches one insertion-cost row per
  unvisited block and, after each insertion at position `p`, recomputes only
  positions `p`/`p+1` (the only edges an insertion changes) instead of
  re-scanning the full tour (the old O(n^3) loop: 1.2M
  `_calculate_insertion_cost` calls on 194 blocks, 0.80 s/job). Rows are
  built from the same `_calculate_insertion_cost` calls (bit-identical
  floats); the cached per-block best follows the naive scan's strict-<
  first-minimum tie rule (`_first_min_insertion` — ties resolve to the
  LOWEST position). Tie bookkeeping on update: cached best below `p`
  survives at a lower index (only a strict `<` replaces it); above `p` it
  shifts to `best_pos+1` (ties go to the new lower index, `<=` replaces);
  at `p` it is rescanned. Pinned by
  `tests/test_optimizer.py::TestInsertionHeuristicIncrementalEquivalence`
  (seeded randomized equivalence vs the verbatim naive loop + the
  frozen-fixture distance pin).
- Keep the `with`-block pattern around `ProcessPoolExecutor` (tests patch it
  with `__enter__`-style mocks).
- Float comparisons: `math.isclose` / tolerances, never `==` (root §1; the
  canonical `COORD_TOLERANCE = 1e-3` lives in
  [`../utils/AGENTS.md`](../utils/AGENTS.md) `geometry.py` and is the
  merge/suppression predicate via `path_merger.DEFAULT_MERGE_TOLERANCE`).
