# Relief stroke-parity II: counter core, pit corners, border ring corners

Goal: close the three user-reported deviations vs the EngraveLab references
(`examples/plt_relief_engraving/A_relief_s-sweep_{coarse_stroke_0.06,fine_stroke_0.005}.plt`):

1. **Coarse (0.06) counter strokes** — reference has 0.06 fill rows + a sharp
   outline triangle inside the "A" hole; our output's counter core is eroded
   too deep (base at floor−r instead of floor−d, filleted sharp corners), so
   rows land at the wrong y and the outline base/corners are wrong.
2. **Fine (0.005) pit outlines** — the fine rest region's outline rings must
   run at `(r_coarse − r_fine)` from the coarse outline sweep and turn the
   coarse core's corner points on small arcs ("pits" of the legs clean).
3. **Coarse border outline corners** — reference outer ring is a SHARP square
   `[30.48, 985.52]` u with no corner arcs/tabs; our `_tabbed_cores` +
   corner-fillet discs produce rounded/tabbed corners that do not match.

## Measured ground truth (device units, 1016 u/in, from the references)

Coarse (`coarse_stroke_0.06`, d=60.96u, r=30.48u, p=24.384u):
- Border rows: y = 30.48 + k·p, k=1..39 (54.864..981.456), **no** edge/tab
  rows at 30.48/985.52; x span `[42.672, 973.328]` = core inset 0.5p.
- Border outline ring: `PU30.480,30.480 PD30.480,985.520 PD985.520,985.520
  PD985.520,30.480 PD30.480,30.480` — sharp square, four straight lines, no AA.
- Counter (the "A" hole) outline: triangle `(507.911,449.483) (470.577,545.566)
  (545.302,545.566)` sharp (no arcs): sides = island edges eroded by **r**
  (apex 449.48 = crotch 365.24 + r/sin(21.25°) ≈ 449.1); **base = floor island
  edge (606.53) − d = 545.566** (full-diameter floor erosion; our code erodes
  the floor by r only → 576.05: two extra rows 546.6/570.0 and a base 30.5u high).
- Counter rows: y = 449.48 + {2p, 3p} = 498.251/522.635, x inset 0.5p
  (row 473.87 dropped by min-pass). Standard per-region `miny + k·p` grid.
- Island caps: 180° arcs at `(332.922,731.520)`/`(683.078,731.520)`, 137.5° at
  `(507.868,281.280)`, radius 60.96 = d (centerline tips; dilated-cap geometry)
  — current buffer+arcify already reproduces these; KEEP.

Fine (`fine_stroke_0.005`, d=5.08u, r=2.54u, p=2.032u):
- Border ring: lines at `[2.540, 1013.460]` + corner arcs `AA30.480,30.480,74.607`
  (also at (985.52,30.48)/(30.48,985.52)/(985.52,985.52)): arc centered on the
  COARSE core corner, radius 28.194 ≈ (r_c − r_f) + 0.25u, endpoints on the
  core lines at (2.540, 26.704)/(26.704, 2.54).
- Pit rings at the coarse core corners `(423.203,667.486)`/`(592.747,667.486)`
  (crossbar underside island edge 667.486 × leg side-core line): line at
  y = 667.486 − (r_c − r_f) = **639.546** + arc same construction
  (`AA423.203,667.486,53.372`, r=28.194). = the fine rest boundary against the
  coarse OUTLINE sweep dilated by (r_c − r_f), eroded by ~r_f, corner circle
  r = (r_c − r_f) + 0.25u.
- Counter rows: grid y = 372.39 + k·p (core apex with r_f erosion), rows inset
  ≈1.5p, chain ends where the coarse rows' dilation starts (last row 429.286).
- Border fill row grid: y ≡ 2.869 (mod 2.032) = core miny + 0.329u, x inset
  0.5p (3.556). Treat 0.329u as a fitted constant (quantum-like, ≈1/4 u + p/8
  fudge); pin exactly, do not over-theorize.

## Tasks

### 0. Baseline measurement (no source changes)
- `uv run plt-optimizer generate examples/job_specs/relief_simple.yaml --output /tmp/relief_p2 --no-plots`
- Run `uv run python scripts/compare_relief_strokes.py --ours /tmp/relief_p2/60_txt_relief_simple.plt --ref examples/plt_relief_engraving/A_relief_s-sweep_coarse_stroke_0.06.plt --json /tmp/cmp_coarse.json` (and fine).
- Record in the commit body: current counter strokes (confirm rows present at
  ~497.9/522.3/546.6/570.0 vs reference 498.251/522.635), border ring corner
  shape, pit-ring locations. This validates the root-cause list before edits.

### 1. Counter core: side erosion r + floor clip d; no fillet on sharp triangle corners
`plt_optimizer/generate/relief/pipeline.py` (`_cores`) + `regions.py` (classify which interior edges are "floor"):
- Interior core = `comp.buffer(-r, quad_segs=256, join_style=1)` (keep), then
  clip the floor: intersect with the half-plane
  `dist_from_component_bounds ≥ d` on the bbox edge the grid runs away from —
  concretely: floor edge = the counter component's bbox edge whose interior
  angle span is widest (the crossbar side); for the "A" it is the bbox max-y
  edge in device = the widest edge; erode that edge by `d - r` extra
  (`intersection` with the bbox-edge offset half-plane). Keep it general:
  clip = `comp.bounds` inset by (0,0,0,0) on sides, (d−r) on floor edge.
- Do NOT run `_fillet_convex_corners` on interior cores whose corner turns
  exceed a threshold: the reference counter triangle keeps SHARP apex/base
  corners. Fillet only where the corner is the eroded result of an island
  convex cap? Simpler: skip fillet discs for interior cores entirely (verify
  the counter outline ring stays sharp; tip caps come from buffer round joins
  already).
- Pins (unit tests, new `tests/test_relief_counter_core.py`): triangle core
  apex/base within 1u of (507.911,449.483)/(545.566) after label→device
  transform math (work label-local: apex y = 0.5·(1016−…)… express in inches:
  base 545.566/1016 in, centerline bar 637/1016: assert base = floor_edge − d,
  sides = floor geometry − r).

### 2. Border: remove tabbed cores & label-corner fillets; drop edge/tab rows
`pipeline.py` (`_tabbed_cores`, `_fillet_convex_corners` call sites), `filler.py` (`_row_grid` edge_rows):
- Outline rings use the RAW eroded outer cores (sharp square corners at
  (±r inset) = the reference's `PU30.48...` ring). Delete `_tabbed_cores`
  (and its docstring references) — the corner-tab/AA30.48 story came from a
  misread; the coarse reference has no box-corner arcs at all.
- `_fillet_convex_corners`: the eroded box core corners must stay SHARP
  (reference). The disc subtraction produced arcs at (30.48,30.48) r=30.48 —
  wrong for coarse. Evaluate: corner arcs of r = d/2 at convex core corners
  do NOT appear in the coarse references (tips/arcs all come from the island
  round caps via buffer). Restrict filleting to cores' corners that are
  concave-on-the-island (tool-wrap) cases; the box square corners get none.
  If shapely already yields what the references show with pure
  `buffer(-r, join_style=1)` (round joins: convex core corners round at r
  exactly where the ISLAND is convex; box corners miter sharp), then just
  drop the fillet pass wholesale — verify with the comparison tool first.
- Row grid: border rows = `miny + k·p, k≥1` strictly interior (reference:
  54.864..981.456, NO rows at 30.48/985.52). Change the anchor path to
  `edge_rows=False` everywhere (`_row_grid` keeps the param for counters
  only, then consider deleting it). Row inset 0.5p ✓ already.
- Update `filler.py`/`pipeline.py`/`relief/AGENTS.md` prose that cites the
  "30.48/985.52 tab rows" (measured absent) and "AA30.480,985.520 coarse
  corner arc" (that arc is FINE, r = r_c − r_f, centered on the coarse
  corner — see task 3).

### 3. Fine rest = core − fill⊕r_k − outline⊕(r_k − r_{k+1}); corner arcs fall out
`pipeline.py` (`generate_relief_passes` coverage staging, `_coverage_of`):
- Stage coverage per cutter as TWO dilations:
  `coverage_fill = dilate(fill_strokes, r)` (unchanged) and
  `coverage_outline = dilate(outline_strokes, r - r_next)` where `r_next` is
  the NEXT finer cutter's radius (last stage: full r, nothing follows).
  `rest_{next} = core_{next} − (coverage_fill ∪ coverage_outline)`.
- With shapely round joins, the pit rings get exactly the reference shape:
  lines at (r_c − r_f) from the coarse outline, corner arcs of radius
  (r_c − r_f) centered on the coarse core corners; the +0.25u endpoint chord
  quantum is below a plotter unit — accept (comparison tol 1/1016 covers it).
  Verify pit rings at (423.203,667.486)/(592.747,667.486) and box corners
  (30.48,30.48) etc. with the stroke tool (match mean < 1u).
- Border ring lines at r_f inset `[2.54, 1013.46]` — already the fine core
  bbox; the corner arcs replace the sharp corners via the circle/line
  intersections above (grid anchor +0.329u: fit and pin as a constant in
  `filler.py` if the row diff at 2.869-vs-4.572 shows; keep 0.5p row inset).

### 4. Re-pin coverage thresholds + docs
`tests/test_relief_export.py`, `tests/relief_coverage.py` helpers,
`plt_optimizer/generate/relief/AGENTS.md`, `relief/__init__.py` docstring:
- Re-run stroke comparison for coarse & fine; expect: counter rows match
  (498.251/522.635, 0 extra at 546.6/570.0), counter outline sharp at
  545.566, border ring straight sharp square, fine pit rings + box corner
  arcs matched to <1u mean, fine row grids within the tol.
- Tighten bands that the fixes make safe (e.g. drop any test asserting the
  tab/edge rows exist; update `TestReliefArcOutlines` coarse box-corner
  expectations to the straight square (no corner arcs on coarse); fine
  corner arcs at (±30.48,±30.48) centers r≈28.19 sweep 74.6°).
- Fix stale prose (tab rows, `AA30.480,985.520` coarse, "counters erode by
  radius" vs "diameter" contradictions — canonical model: sides r, floor d,
  no fillet).

### 5. Gates
- `uv run pytest -q --no-cov -p no:cacheprovider` (full), `uv run mypy plt_optimizer/`,
  `uv run ruff check . && uv run ruff format --check .`, `pre-commit run --all-files` (scoped),
  docs drift: `uv run pytest tests/test_job_spec_docs.py tests/test_job_spec_skills.py -q`,
  `uv run python scripts/validate_job_config.py --config job-config.json`.
- CLI acceptance: relief_simple generate → 3 PLTs; stroke-compare JSON saved;
  conventional commits per task (never commit `.kilo/plans/`).

## Risks / decisions
- Removing edge/tab rows + tabs changes the border fill span at corners:
  corner pockets rely on the coverage-dilation geometry (task 3) — verify
  the fine corner-strip rows (2.869..22.239, x 3.556..23.762 arcs) emerge.
- The `floor − d` clip rule is fit to the "A" (one counter, one floor edge).
  General shape (e.g. "O" counters, multi-floor) has no reference: keep the
  rule "clip the widest bbox edge by d−r" and pin an "O"-glyph unit test as
  geometric-plausible, documented as unverified vs EngraveLab.
- Residual 0.25–0.5u quantums (EngraveLab internal rounding) stay documented
  in AGENTS.md, not chased.
