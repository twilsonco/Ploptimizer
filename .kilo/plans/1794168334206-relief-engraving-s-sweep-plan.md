# Relief Engraving (S-Sweep) — Implementation Plan

## Goal

`plt-optimizer generate` on `examples/job_specs/relief_simple.yaml` (and `relief_simple_alt.yaml`) must emit per-cutter PLT toolpaths that are **geometrically equivalent** (material-removal coverage) to the EngraveLab ground truth in `examples/plt_relief_engraving/A_relief_s-sweep_{coarse_stroke_0.06,fine_stroke_0.005}.plt`. Do not stop until the equivalence tests pass.

## Ground truth facts (verified by analysis of the reference files, 1016 u/in)

- Raster row pitch = **0.4 × cutter diameter** (24.384u for 0.06", 2.032u for 0.005").
- Raised islands = glyph centerline strokes ⊕ disk(effective_d/2) (the "A" band is 0.06" wide). Engraved region = label rect − islands.
- The plate/label border is engraved by the **coarsest** relief cutter as a rect inset by its **radius**; fill runs edge-to-edge (preserve_border=false is the reference behavior).
- Counters (the "A" triangle): the coarse cutter fills its d/2-eroded interior and outlines at the **d-eroded** boundary (base y = bar − 0.06" exactly, verified from `PU470.577,545.566` = 606.526 − 60.96). The **strip between that outline and the raised band** is rest material, filled + outlined by the fine cutter (fine file: rows at 2.032u pitch + `AA` arcs centered exactly on the coarse outline's corners `(470.577,545.566)`, `(545.302,545.566)`, `(507.911,449.483)`).
- Every raster fill region is followed by a **clean outline pass** at its boundary (fill first, outline after; arcs `AA` at rounded corners). Plate-corner rest regions outside the coarse border line get fine fill + fine rounded outline (fine file lines 147–259).
- Region rows anchor to their own region bbox (counter trapezoid rows are NOT on the global row grid) — anchoring is an implementation choice covered by the equivalence tolerance; **spec**: rows anchored bottom-up at region-bbox-min-y + pitch, x-start at bbox-min-x + pitch/2, direction alternating.
- The A's centerline strokes never appear in the relief output: relief **replaces** normal text strokes; drill holes are filled over.

## Resolved design decisions (user-approved)

1. **Scope**: S-Sweep only; abstract `RasterFiller` protocol; spiral deferred (references stay in `examples/` as forward pins).
2. **Cutter selection** (diameters = effective engraved widths; components = connected components of the full engrave region):
   - coarse = largest inventory cutter d such that EVERY engrave component is d-admissible: (a) interior components (text counters) erode by disk(d/2) to positive area; (b) every component admits ≥1 full raster pass (erosion by d/2 contains a scanline interval ≥ max(d, min-stroke floor) at 0.4d pitch).
   - medium = [largest inventory cutter (background only), smallest cutter].
   - fine = [largest, middle, smallest]; middle = largest inventory d with ≥1 full raster pass inside EVERY interior text hole.
   - Degenerate: presets collapse downward (one qualifying cutter = one pass; fine's middle falls back to smallest; if no cutter qualifies for coarse, use medium semantics + WARNING; if the smallest cutter can't enter a counter, engrave what's reachable + WARNING).
3. **Widths**: `relief_engraving_cutters` = exact engraved widths (no buffer, no inventory snap; they completely bypass `tools.json`). Inventory-selected cutters: physical = rung, geometry width = effective (physical + `cutter_buffer_text`), file token = physical.
4. **Border**: `relief_engraving_preserve_border` default **false** = reference behavior above; true = border band (bh effective width) + offset excluded from the engrave region (raised ridge), border centerline still emits to bh. bh plate-cut border unchanged in both modes; **drill holes suppressed entirely** (not engraved, not in bh) for relief labels.
5. **Validation**: coverage-equivalence (see Testing); fixtures copied into new additive dir `tests_deps/relief/`.
6. **Islands & space**: raised islands from ALL label ink (PLT/TTF glyph strokes, bold pass unions, underlines/strikethroughs, list markers, markup transforms — anything on a text pen); relief computed in LABEL space, rotated/translated with the label through the normal plate transform; non-relief labels pack normally; a relief cutter equal to a text cutter's physical diameter shares that pen/`<cutter>_txt_<job>.plt` file.

## Architecture

New package `plt_optimizer/generate/relief/` (matplotlib-free; shapely is a core dependency, already used on the generate path by `packing/ellipse_packer.py`; match the modern type hints used in `generate/`):

- `regions.py` — build shapely polygons from `RenderedLabel` + `ResolvedLabel`: label box, raised islands (open polyline → `buffer(w/2)`, closed contour → polygon fill; unary_union), engrave region = box − islands (− preserve_border band); holes excluded. `min_area` filter (≈ (pitch_final/2)²) kills slivers; `make_valid` everywhere.
- `cutters.py` — preset → ordered cutter list [(physical, effective)], per decision 2; bypass list per decision 3. Pure functions; inventory = `available_cutters` threaded to export.
- `filler.py` — `RasterFiller` Protocol: `fill(regions: list[Polygon], pitch: float, anchor: Point) -> list[Polyline]` (contiguous zigzag strokes, splits handled as independent sub-strokes with diagonal links) + `outline(regions) -> list[Polyline]` (boundary offset of the filled set, one contiguous ring per component-group so split regions keep the unified outline). `SSweepFiller` implements it via scanline clipping (`shapely.box` per row ∩ region).
- `pipeline.py` — per-label stage loop over selected cutters descending: reachable_i = R ⊖ disk(d_i/2) ⊕ disk(d_i/2); rest_i = reachable_i − ⋃_{j<i} reachable_j (∪ prior coverage); emit fill(rest_i regions) chunks then outline chunks; final uncovered = R − ⋃ all coverage; area > tol → WARNING. Also emits the coarsest cutter's border line (inset r) when preserve_border=false. Every contiguous fill stroke = one chunk, every outline ring = one chunk (TODO contract: chunks are TSP nodes for the existing rapid-travel optimizer).

Export glue (`vectorize.export_per_cutter_plts`, after `generate_layout_with_bounds`, new `stage_timer("relief", ...)` stage):

1. Collect relief labels' cutter lists → extend pen assignment: new pen(s) per distinct relief physical cutter (`build_cutter_pen_map` at `resolution.py:5813` grows an optional extra-layers input; keep SP1 smallest / SP4+ ordering).
2. For each packed label with relief: build regions in label space, generate chunks, map to device units through the SAME per-label transform the text chunks use (`plate_optimizer._LabelTransform` / `_transform_point`, plate_optimizer.py:115/:252 — rotation rides with the label).
3. Suppress relief labels' normal text strokes from the text layers (rendered ink remains the island source); suppress their SP3 holes from the structural extraction; keep SP2 border in bh.
4. Route each relief layer's chunks through the structural-style 1:1-chunk optimization (`optimize_structural_layer`, plate_optimizer.py:939, per plate+pen) and write as `<cutter>_txt_<job>.plt` with `prepend_tool_option_headers(..., "text")`.

## Tasks

1. **Schema** (`schema.py`): add to `LabelAttributes` + `_parity()` plate tail: `relief_engraving: bool`, `relief_engraving_quality: Literal["coarse","medium","fine"]` (code fallback "medium"), `relief_engraving_cutters: Optional[list[float]]` (each > 0; NO config tier, like `cutter_size`), `relief_engraving_preserve_border: bool` (fallback false). Cascade job → plate → label at `JobSpec` validation mirroring `_apply_replacement_cascades` (schema.py:3131; plate tier applies to labels pinned by `plate_id`).
2. **Config** (`job_config.py` + `job-config.json`): `JobDefaults.relief_engraving` (false), `relief_engraving_quality` ("medium"), `relief_engraving_preserve_border` (false) + `_JOB_LAYER_FIELDS` entries + `_CONFIG_GROUP_FIELDS`/skills `fields` tuples; set `"relief_engraving_quality": "medium"` in `job-config.json`.
3. **Resolution** (`resolution.py`): land the four fields on `ResolvedLabel`; no cutter selection here (needs rendered geometry).
4. **relief package**: `regions.py`, `cutters.py`, `filler.py`, `pipeline.py` per architecture, module docstrings carrying the derivations + reference constants (pitch 0.4d, insets, counter d-erosion).
5. **Export glue** in `vectorize.py` + `plate_optimizer.py` (steps 1–4 above); text-stroke/holes suppression for relief labels; pen-map extension.
6. **Docs regen**: `uv run python docs/schema/generate_ai_docs.py` then `generate_skills.py`; `git add docs/skills/...` (drift-pinned tests).
7. **Fixtures**: copy `A_relief_s-sweep_*.plt`, `A_no_relief.plt`, `relief_simple.yaml`, `relief_simple_alt.yaml` into `tests_deps/relief/`.
8. **Tests** (all in `tests/`):
   - `test_relief_cutters.py` — preset selection incl. every degenerate rule, bypass semantics.
   - `test_relief_filler.py` — square/annulus/split-region fills: pitch, one-stroke-per-region contiguity, splits independent, outline present and after fill, no island intrusion.
   - `test_relief_regions.py` — islands from buffered skeletons; TTF contour fill; holes excluded.
   - `test_schema.py` / `test_job_config.py` / `test_resolution.py` — fields, cascade (incl. plate parity), config injection, strict-unknown-field.
   - `test_phase3_export.py` — pre-check: non-relief generate of the relief spec matches `A_no_relief.plt` coverage; end-to-end: generate `relief_simple.yaml` (bypass [0.06, 0.005], zero-buffer config) → per-cutter envelopes vs reference: symmetric-difference area ≤ 1% of reference coverage per cutter, containment vs ideal regions (A band ⊕ 0.6·pitch) both directions, outline-presence check; `relief_simple_alt.yaml` split-leg scenario coverage check.
   - Coverage comparison helper (stroke → sampled polyline → shapely buffer, `AA` arc sampling, unit→inch via `core/units.py`) in a shared test util.
9. **Gates**: `uv run pytest` full suite; `uv run mypy plt_optimizer` (strict); `uv run ruff check .` + `uv run ruff format .`; pre-commit clean. Conventional commits: `feat(generate): ...` per task cluster.

## Risks / notes

- Fine-pitch row counts: relief fill covers **rest regions only** (corners/strips), not full plates; add a chunk-count guard: >200k segments → WARNING naming the label.
- Shapely degeneracies (tangential buffers, zero-width slivers): fixed tolerances (`1e-9`), `make_valid`, min-area prune; never trust `==` on floats (`math.isclose`).
- Outline `AA` arcs: emit offset boundaries as `PD` polylines (shapely quad_segs); arc-ifying corner rounds is a cosmetic follow-up, not needed for equivalence.
- The optimizer reorders chunks (file order will differ from references) — equivalence is per-layer coverage; test on written files after optimization.
- `tests_deps/` additions only; never edit `examples/plt_relief_engraving/`.
- Out of scope: spiral filler, depth/Z per-cutter control, relief in `optimize`/`watch` pipelines, label-rect fills beyond plate bounds, text_color interaction with relief pens (relief layers are colorless).
