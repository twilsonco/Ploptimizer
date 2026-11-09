# Relief stroke-parity: buffer neglect, strict stroke comparison, and the three reference discrepancies

## Context

Relief engraving shipped (commits `04de17c`, `d6c0112`) with **coverage-equivalence** to the EngraveLab ground truth
(`examples/plt_relief_engraving/A_relief_s-sweep_*.plt`) — a loose area-symdiff contract (99.96% area match).
The user found three stroke-level deviations and one settings bug:

1. **`cutter_buffer_text` inflates relief islands** → strokes overshoot the marks. It must be neglected for relief
   engraving under all circumstances.
2. The area comparison is too loose to find the remaining deviations → add a **strict stroke-wise comparison**
   (stroke error + missing/extra strokes) and use it to drive fixes.
3. Three known discrepancies (first fix targets, in order):
   - **(a) 0.06 coarse:** after the raster splits into left/right regions around the "A" legs, the vertical/diagonal
     pitch connectors between horizontal rows disappear. Reference keeps chaining (verified: coarse lines 138-148 —
     pen-down connectors at row ends inside split regions).
   - **(b) 0.005 fine, counter bottom-right corner:** we emit 10 rows, reference 13.
   - **(c) Outline rings approximate curves with line-segment chains; reference emits real arcs**
     (`PD;AA{cx},{cy},{sweep}` e.g. fine line 145 `AA470.577,545.566,95.841` — center 470.577,545.566u, sweep 95.841°).
     This includes the curved parts of the outlines AROUND the raised text/markup islands — the round caps over
     stroke tips: the "A" apex and the ends of its legs (coarse lines 169-173).

## Verified analysis (do not re-derive)

- **Pitch math:** reference pitch 2.032u = 0.002in = 0.4·d for the 0.005 cutter ⇒ exactly 60% overlap
  (`pitch = d·(1 − overlap/100)`). Our hardcoded `0.4·d` (pipeline.py:213) **already matches** — the user's overlap
  hypothesis is the requested *knob*, not the root cause.
- **Root cause of (b):** `_rest_region` (pipeline.py:142-166, 215-216) subtracts prior coverage from ALL cores.
  The fine counter rows at y 547.142-571.526 (13 rows, full core span, e.g. 557.318→584.369 = fine-core edges) sit
  INSIDE the coarse capsule sweep — reference re-cuts them. Reference model: **interior (counter) components are
  filled from the FULL core at every stage; outer background keeps rest-minus-coverage** (fine file contains only
  counters + 4 corner pockets + outlines — no perimeter re-fill).
- **Root cause of (a):** `SSweepFiller._fill_region` (filler.py:158-199) chains serpentine by GLOBAL row-index parity.
  Where a region splits into left+right intervals per y, intervals interleave in y-order, links cross the island,
  the "link inside region" test fails → chains become independent strokes with no connector. Fix: lane-based chaining
  (interval continuity across rows), serpentine parity per lane, keep the inside-region link guard.
- **(c) arcs:** `text_geometry.Stroke.to_hpgl` ALREADY emits arcs as the exact reference form
  `PD;AA{cx},{cy},{sweep:.3f}` (text_geometry.py:662-668) and `ArcSeg` survives translate/mirrored_y/scaled (the
  plate transform) + core parser/writer + TSP + previews. Our rings are circular-arc geometry already — corners are
  true arcs of the erosion radius (outer/interior core = `comp.buffer(-d/2, join_style=1)` circle corners;
  islands = capsules of r=w/2; island-tangency centers sit exactly d/2 from the core corner = arc center + start +
  sweep). **This includes the curved parts of the outlines around the raised text/markup (island caps): the cap
  arcs are concentric with the island's rounded caps — center = skeleton-endpoint cap center, radius = w/2 + d/2
  (verified: coarse "A" apex rings `AA332.922,731.520,180.000` / `AA683.078,731.520,180.000` and leg tip
  `AA507.868,281.280,137.502`, all radius 60.96u = 0.03in text-cutter w/2 + 0.03in coarse d/2; the dino "A" caps
  are round).** Polygonized by `regions.stroke_to_points` (_ARC_FLATTEN_TOL=2.5e-4) + shapely arcs. Fix = **post-fit
  circular runs back into `ArcSeg`** on outline rings (fill rows stay straight — reference rows are straight).
  Reference arcs (all centered on the offset corner geometry the buffers already build): coarse island-apex rings
  sweep 180°/137.502° (lines 169-173); fine counter apex (line 145) and the fine file's 4 box-corner pocket arcs
  centers (30.48,985.52)u etc. sweep 74.607° (lines 239-258). Exact radii emerge from the fit — do not hardcode.
- **Buffer entry points for (1):** `pen_widths_for_label` (pipeline.py:251-273) returns physical+buffer → feeds
  `label_ink` (export.py:283) → island capsule widths; and `select_relief_cutters(cutter_buffer=...)`
  (cutters.py:131-159, export.py:311-312) sets preset effective = physical+buffer. Bypass list is already exact.
- **Frame:** device frame == reference plotter frame (`mirror_y=False`, established). Test thresholds today:
  SYMDIFF_BAND=0.035, INTRUSION_TOL=0.05, UNCOVERED_TOL=0.06 in `tests/test_relief_export.py`.
- `pitch` is used in: pipeline (fill), cutters.py:124 (`_admissible_erosion` pass floor via `_PASS_SCAN_PITCH`),
  filler end-inset/min-pass (fractions of pitch — keep pitch-relative).
- Scripts have no existing test-import pattern → put the reusable engine in `plt_optimizer/diagnostics/`
  (import-guarded package, matplotlib-free module OK, per package rules), script is a thin CLI wrapper.

## Tasks

### 1. Neglect `cutter_buffer_text` for relief (fix)
- `pipeline.py::pen_widths_for_label` → physical engraved width only (no `effective - physical` add). Rename/
  docstring: relief islands are the ink at the physical cutter width.
- `cutters.py::select_relief_cutters` → drop `cutter_buffer` param; `ReliefCutter(physical, effective=physical)`
  everywhere. `export.py` → stop computing/passing `cutter_buffer` (line 311, `_plan_ladder`).
- Module + package docstrings and `relief/AGENTS.md`: relief ignores `cutter_buffer_text` under all circumstances.
- Tests: unit (cutters/pipeline/export); e2e — `tests/test_relief_export.py` drops `_zero_buffer_config` in favor of
  the repo `job-config.json`, plus a NEW test asserting relief output is **identical** under the repo config (shop
  buffer) and `tests_deps/relief/relief_zero_buffer_job_config.json` (buffer-invariance pin). Fixture files are
  frozen; only references change.

### 2. Strict stroke-wise comparison tool
- New `plt_optimizer/diagnostics/stroke_compare.py` (pure stdlib+shapely, NO matplotlib):
  - `parse_strokes(path) -> List[SampledStroke]`: core `PLTParser`; split PU-runs; arcs flattened for sampling,
    `ArcSegment` params kept for reporting; device-frame inches.
  - Cell-grid `PointIndex` (cell = tolerance) for nearest-point queries.
  - `compare(ours, ref, tol=1/1016, sample=0.0005) -> ComparisonReport`: per ref stroke mean/max distance to ours
    (chamfer); UNMATCHED REF = **missing stroke** (mean > tol), with bbox + length + max deviation; per our stroke
    same against ref = **extra**; matched pairs get distance stats; totals summary.
  - Optional JSON dict export; ASCII report formatter.
- New `scripts/compare_relief_strokes.py` (argparse): `--ours --ref [--tol] [--sample] [--json out.json]
  [--plot out.png]` (plot via matplotlib: ref black / extra red / missing blue, dev-only). Exit 0 = no
  missing/extra within tol, 1 otherwise.
- Tests `tests/test_stroke_compare.py`: synthetic in-memory PLT pairs — identical (zero error, no flags), row
  shifted > tol (matched, high error), row deleted (missing), row added (extra), arc vs dense chords (within
  tol). Script wrapper smoke-tested through `main()` with tmp files.

### 3. (a) Serpentine lane connectors in split regions
- `filler.py::_fill_region`: group scanline intervals into **lanes** by x-overlap continuity with the previous row's
  intervals (greedy, ordered by x); serpentine parity per lane; chain row N→N+1 inside a lane pen-down (the
  pitch-spaced vertical/diagonal link, guard: link stays inside region — a real link always does); lane start/end
  (split/merge/appearance) = new chain, pen-up. Keep `end_inset_fraction`/`min_pass_fraction` semantics.
- Unit: rectangle with a wedge island splitting rows (assert split-region chains carry N-1 connectors, links pen-
  down, inside region; unsplit regions byte-identical to current output).

### 4. (b) `relief_engraving_stroke_overlap_percent` + full-counter clean pass
- **Schema (full doc cascade, follow the `relief_engraving_cutters` pattern):**
  `LabelAttributes` + `PlateSpec` parity + `_apply_relief_cascades` (job→plate→label, never lines) in `schema.py`:
  `float = Field(default=None, ge=0.0, lt=100.0)` — "Row overlap percent; pitch = d·(1 − overlap/100); null = shop
  default". `JobDefaults` field (code fallback `DEFAULT_RELIEF_ENGRAVING_STROKE_OVERLAP_PERCENT = 60.0` in
  `resolution.py`) + `_JOB_LAYER_FIELDS` + `job-config.json: "relief_engraving_stroke_overlap_percent": 60.0`.
  `ResolvedLabel` field + cascade in `_resolve_label`.
- `pipeline.py::generate_relief_passes(..., overlap_percent: float = 60.0)`: `pitch = d * (1 - overlap/100)`;
  `export.py` passes `label.relief_engraving_stroke_overlap_percent`.
- `cutters.py::select_relief_cutters`: add `overlap_percent` param feeding `_admissible_erosion` pitch floor.
- **Full-counter fill:** interior (counter) components fill from the FULL core at every stage (no coverage
  subtraction; `_rest_region` outer-only). Interior outlines likewise ring the full core. Coverage union is
  unchanged ⇒ existing area-coverage tests keep passing; stroke counts in counters rise.
- Docs: `generate_ai_docs.py` (`_CONFIG_GROUP_FIELDS`, `_FALLBACK_NOTES`), `generate_skills.py` (label-geometry
  theme), regenerate `JOB_SPEC.md`/`JOB_CONFIG.md`/schemas/skill files; `relief/AGENTS.md` + `__init__.py` pitch
  constant text.
- Tests: schema/job_config/resolution cascade classes (mirror the relief field suites); pipeline: 60% default =
  current behavior; 50% → pitch 0.5d row grid (row y multiples); e2e counter-corner row count = 13
  (fine `relief_simple` bottom-right corner).

### 5. (c) Arc-native outline rings (fill boundaries AND text/markup island caps)
- `filler.py` (or new `relief/arcify.py`): `fit_circular_runs(points, min_dev, max_r) -> List[TextSegment]` —
  Kåsa algebraic circle fit per constant-turn run (cross-product sign), reject straight runs (signed curvature
  ~0) and fits with radius > `max_r` (straight-edge noise; a straight run through the cap must not become a
  1000u arc), chord deviation < `min_dev` (use ~5e-4, sub-plotter-unit), sweep = angle between start/end about
  fitted center.
- `pipeline.py`: outline rings → `Stroke(pen_up=start, segments=fit_circular_runs(...))` (LineSeg+ArcSeg mix);
  fill strokes unchanged (pure lines). Export/TSP/parser handle arcs natively; verify the Y-mirror sign convention
  against the reference AA (reference is y-up plotter frame).
- Sweep/center continuity: an outline ring ending in an arc chains tip-to-tail via `ArcSeg.end` =
  `arc_end_point(start, center, sweep)` — the exact inverse of the core parser's AA semantics.
- Tests: unit fitter (chord chain of a 90° quarter circle → one ArcSeg center/radius/sweep within tol; straight
  line → LinesSegs; mixed L/A/L ring; a 180° cap run → one ArcSeg with start/end diametrically opposite the
  center); e2e: fine 0.005 outline of the bottom-right counter region contains ≥1 AA
  with center ≈ (470.577,545.566)u (reference line 145), and the fine file's 4 box-corner pocket arcs with centers
  on the (±30.48, ±30.48)-inset box corners, sweep ≈ 74.6° (fine lines 239-258), and the coarse island-apex rings
  with sweep 180° (coarse lines 169/173), and the coarse leg-tip cap arc with sweep ≈ 137.5°, center
  ≈ (507.868,281.280)u, radius ≈ 60.96u = w/2 + d/2 (coarse line 171). Stroke-compare (Task 2) reports
  outline-ring deviation ≤ tol.

### 6. Integration pass + gates
- `uv run plt-optimizer generate examples/job_specs/relief_simple.yaml --output /tmp/relief_cli --no-plots`, then
  `uv run python scripts/compare_relief_strokes.py --ours /tmp/relief_cli/60_txt_relief_simple.plt --ref
  examples/plt_relief_engraving/A_relief_s-sweep_coarse_stroke_0.06.plt` (and 5_txt vs fine). Iterate any residual
  structural mismatches the tool surfaces (missing/extra list). Re-pin
  `tests/test_relief_export.py` thresholds at measured post-fix values (tighten: coverage tests stay; add the
  stroke-compare e2e as the strict pin — keep it non-brittle: assert missing=0 and matched max-error, tolerate
  chain-ordering diffs by comparing stroke GEOMETRY sets, not order).
- Gates: full `uv run pytest -q --no-cov -p no:cacheprovider`, `uv run mypy plt_optimizer`, `ruff check .`,
  `ruff format --check`, `pre-commit run --all-files`.

## Conventions
- Conventional commits, trailer `Co-Authored-By: Kilo <kilo@kilocode.ai>`. Suggested split: 1 fix + 1 feat
  (tool) + 3 fixes (a/b/c, the overlap-parameter feat bundled with b) + 1 docs, or per-task commits.
- Never commit `.kilo/plans/`. `tests_deps/` additive-only (no changes needed; the zero-buffer fixture stays, now
  as the buffer-invariance control).

## Validation
- Task 1: buffer-invariance e2e green; plain-text (non-relief) output bit-identical (existing zero-buffer
  regression pins untouched).
- Task 2: unit tests + manual run on reference vs reference (self-compare: zero missing, zero extra, max error 0).
- Task 3: coarse 0.06 split regions contain pitch connectors (stroke-compare: 0 missing in the leg-split band).
- Task 4: fine counter corner = 13 chained rows; overlap param default reproduces current grid (bit-compare
  relief output pre/post for the YAMLs).
- Task 5: outline rings emit `PD;AA` matching reference arc centers/sweeps within tolerance; whole-file
  stroke-compare missing=0, extras=0, matched error ≤ ~1 plotter unit for `relief_simple` (both cutters).
- Full suite + mypy + ruff + pre-commit green.
