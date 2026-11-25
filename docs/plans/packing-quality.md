# Plate-layout quality: material sharing, rotation preference, ensemble width

Status: **approved** (2026-11-24) — **D1-b** tail sharing, **D2** library default
`fill_axis` + shop default `minimize` in `job-config.json`, **D3** widened ensemble
always on. Executed as a Pendant goal in the order **U0 → U1 → U3**; **U2** (D1-b) gets
its own plan doc. **U0 is done** (2026-11-24); U1/U3/U2 are not started.
Owner docs: [`../generate/packing/AGENTS.md`](../generate/packing/AGENTS.md),
[`../RECTPACK_KNOBS.md`](../RECTPACK_KNOBS.md). Measurement log: `.probe/FINDINGS.md`
(§2 knob sweep, §5 per-plate numbers).

## The complaint

1. A job's first plate ships with plate material unutilised even though a
   100 %-coverage layout exists.
2. Layouts use far more 90° label rotations than necessary.

### Measured on real specs (plate 24×16 = 384 in²; `ink` = Σ label area)

`ink` is identical in both columns of each job, so every difference below is
purely how the labels were split across plates. `EMPTY` = plate area − ink.

`2026-11-17 ADK CPR 111261.yaml` (145 labels, rotation enabled):

```text
PRODUCTION (per-material pools) 4 plates, EMPTY total 834.0 in²
kw_default_plate_1 n=70 bbox=24.00x16.00 ink=384.0 cov=100.0% EMPTY=  0.0 rot=62
kw_default_plate_2 n=22 bbox= 5.00x16.00 ink= 66.0 cov= 17.2% EMPTY=318.0 rot=21
rw_default_plate_1 n=29 bbox= 9.00x16.00 ink=138.0 cov= 35.9% EMPTY=246.0 rot=0
bw_default_plate_1 n=24 bbox= 8.00x15.00 ink=114.0 cov= 29.7% EMPTY=270.0 rot=24

JOINT (one pool)                2 plates, EMPTY total  66.0 in²
b0 n=68 bbox=24.00x16.00 ink=384.0 cov=100.0% EMPTY=  0.0 rot=60
b1 n=77 bbox=20.00x16.00 ink=318.0 cov= 82.8% EMPTY= 66.0 rot=71
```

`2026-11-23 CES vons 1897.yaml` (146 labels, spec sets `allow_rotation: false`):
production 3 plates / **544.1 in²** empty → joint 2 plates / **160.1 in²**, ink
identical (607.9 in²). The `kwuv` plate carries 3 labels at cov **29.1 %**
(272.1 in² of sheet with no engraving on it).

Rotation preference, production ensemble vs the same specs with a rotation-aware
rank key (rotation enabled; fp = Σ per-plate bbox area = the current primary term):

| spec | today | rotations promoted | delta |
|---|---|---|---|
| ADK CPR 111261 | fp=704.0 **rot=131** | fp=720.0 **rot=0** | +2.3 % material, plates unchanged |
| 2026-10-29 seer 1811 | fp=751.8 **rot=130** | fp=752.0 **rot=0** | +0.03 % |
| 2026-11-17 CES SFW3525 | fp=592.0 **rot=64** | fp=608.0 **rot=2** | +2.7 % |
| 2026-10-06 seer / vons 1735 / ADK ricco | rot=0 | rot=0 | 0 |

### Causes, located in the code

| # | Cause | Site |
|---|---|---|
| C1 | Labels are partitioned **by material group**, each group packs onto its own auto-allocated plates → every group pays a tail plate. The rank key cannot see it: it minimises `footprint` = Σ per-plate bbox, and nothing asks "is the last plate full". | `packing/layout.py:973` `_split_material_entries`, `:1041` `_claim_plates_by_material`, `:1020` `_material_bin_prefix` |
| C2 | Rotations is the **4th and last** term of the selection key — deliberately, so rotation never costs material. | `packing/layout.py:1527` `_fill_rank_key`, used at `:742` |
| C3 | The candidate pool is narrow: 4 configs for `rows`, **2 for `columns`**, `bin_algo=BFF` hardcoded. Zero-rotation solutions exist only outside it. | `packing/layout.py:156` `PACK_CONFIGS`, `:174` `PACK_CONFIGS_COLUMNS`, `:694` `newPacker(...)` |
| C4 | At an equal footprint the **fill axis decides the orientation**, which rotates labels on purpose (`columns` stands labels up to spend plate height). This is a feature, and it is the direct opposite of "fewest rotations". | `packing/layout.py:1527` (`fill_axis` term), pinned by `tests/test_layout.py:413`, `:494` |

Not a cause, and out of scope: round labels (`ellipse_packer.py` ignores
rotation), `<scale>`-style markup that swaps w/h upstream and never appears in
`PackedLabel.rotated`, and jobs that arithmetically cannot tile a plate
(`2026-10-30 CES vons 1735`: exact-grid cap 96/132 = cov 0.727; the engine's
cov 0.974 is the packing upper bound).

## Product decisions (the part to approve)

**D1 — Do we allow a plate to carry more than one material?** This is the only
fix for complaint 1 (4 plates → 2, 834 in² → 66 in²). It is also the largest
blast radius in the repo, because "plate = one material" is assumed from packing
through accounting:

- `plate_id` names carry the material (`kw_default_plate_1` =
  `_material_bin_prefix` + `_plate_footprint`-driven numbering); Phase 3 assembly
  and the per-cutter `*_txt_*` / boundary / hole file naming key off plate ↔ material.
- `usage_stats.py` reports `by_material` / `by_plate` with **one** material per
  plate row (`:1013`–`:1066`), and `usage.weight` resolves a plate density
  (`JobSpec.material_weight_for_plate`).
- Proof panels/captions (`diagnostics/proof_composite.plate_size_in`,
  `panel_caption`), `default_plate_clearance` per plate, and jobset merged sheets.

Three options, in increasing scope:

- **D1-a — Do nothing.** Multi-material jobs keep one plate per material. Cheapest,
  honest, leaves 2–3 half-empty plates per job.
- **[CHOSEN] D1-b — Tail sharing.** Packing stays material-partitioned; a
  group whose leftovers do not fill a plate may continue on the previous group's
  last plate (packing order = material declaration order). Plate count drops
  toward the joint optimum; the mixed-material consequences below still apply, so
  this is "D1-c's export work with a smaller packing change".
- **D1-c — One pool, material as a label attribute.** Best fill, biggest work.

Whichever of D1-b/c is chosen, the export/accounting contract becomes
**`plate → set(materials)`**: per-cutter/per-material file naming gains a
material dimension, `usage.by_plate` rows list materials, plate-level
`material_weight_per_square_inch` / clearance become ambiguous (decide: refuse
mixed plates when a per-plate density/clearance is configured, and log a warning).

**D2 — Rotation preference. [CHOSEN: shop default.]** New cascade knob; the
library/schema default stays `fill_axis` (no API break, existing specs unaffected),
`job-config.json` sets `minimize` so production jobs change, and reverting is one
config line:

```yaml
rotation_preference: fill_axis   # today: fill axis decides orientation at equal material
rotation_preference: minimize    # fewest rotations, then least material
```

- Cascade **job → plate** (like `layout` / `allow_rotation`), code default
  `fill_axis`, shop default set in `job-config.json` (so the shop flips behaviour
  without touching the library default, mirroring how
  `material_weight_per_square_inch` keeps its number out of the schema).
- `minimize` = selection key `(plates, rotations, footprint, −fill, cross)`.
  `plates` is first so the preference can never buy rotations with an extra
  plate; measured cost ≤ 2.7 % material, plate count unchanged.
- **`minimize` flips two pinned tests** and the engraving orientation of every
  column job it is set on: `tests/test_layout.py:413`
  `test_columns_stand_labels_upright` (two 6×4 labels currently rotate to spend
  12 in of plate height; cites `examples/job_specs/2026-11-18 air controls.yaml`)
  and `:494` `test_lone_label_follows_the_fill_axis`. That is the intent — approve
  explicitly.
- `allow_rotation: false` stays the hard switch. `rotation_preference` is a
  preference among legal layouts; `allow_rotation` is feasibility. The existing
  validator (`schema.py:3144`) that rejects `allow_rotation: true` with a
  `*-simple` mode gains the mirror rule: `rotation_preference` is meaningless on
  a simple layout (the grid engine never rotates) → reject the combination
  explicitly rather than ignore it.

**D3 — Widen the ensemble. [CHOSEN: always on, not gated.]**
(prerequisite for D2, free).** 336 candidates per group
measure **0.5–0.6 s (≤2 ms/run)**, so cost is not the constraint; determinism is.
Proposed curated pool (≈8 configs/mode, 16–32 runs/group ≈ 30–60 ms), pinned by a
test that the list is exactly this:

- `rows`: `MaxRectsBssf×2`, `MaxRectsBl/area`, `MaxRectsBlsf/peri`,
  `MaxRectsBaf/diff`, `SkylineBl/sside`, `GuillotineBafMaxas/lside`, `MaxRectsBssf/diff`
- `columns`: current `SkylineBl/{none,area}` + `SkylineMwfl/area`,
  `SkylineMwf/none`, `MaxRectsBssf/diff`, `MaxRectsBl/lside`, `GuillotineBafMaxas/lside`

Measured material gain from widening alone: 1 of 7 specs (vons 1897
624.0 → **618.6**, `SkylineMwfl/area`). Its real value: every measured
zero-rotation solution came from `GuillotineBafMaxas/lside`, `SkylineMwf/none`,
`MaxRectsBssf/diff` — none of which is in today's pool. **D2 without D3 changes
almost nothing.**

Not proposed, measured as noise: `bin_algo` BNF/BBF (5/7 specs identical);
`max_time` / GA / CP-SAT knobs (rectpack 0.2.2 — installed version — has no such
parameters; see `docs/RECTPACK_KNOBS.md`).

## Units

**U0 — Benchmark harness, no behaviour change. ✅ DONE (2026-11-24).**
`scripts/packing_benchmark.py` (dev-only, no golden file). Per plate: `n / used /
bbox (w×h + area) / sheet / cov% / gap / rot` plus an `EMPTY` flag; per job:
plates, materials, labels, used, bbox, gap, coverage, rotated. It drives the
production path — `prepare_job` → the same four pre-passes
`export_per_cutter_plts` runs (list markers, line-content reflow, shared
h-compression, compression cutter downsize) → pen map →
`generate_layout_with_bounds` — and stops before assembly, so a spec measures in
seconds. CLI: spec paths/dirs, `--modes declared|all|<list>` (an explicit sweep
also rewrites each plate's own `layout` so modes compare like with like),
repeatable `--set key.path=value` (through a throwaway YAML beside the spec, so a
knob is measurable **before** it exists in the schema; missing mapping keys are
created, list indices must exist), `--json`, `--config/--tools`. Pipeline
warnings are silenced with `logging.disable` — they reach stdout through handlers
the pipeline installs on its first call, so handler filters attach too late.
Tests: `tests/test_packing_benchmark.py` (41) — override/coercion parsing,
`tools.json` loading, per-plate metrics (clearance strips are material, never
envelope; an empty plate is a full sheet of waste; tiled labels report a `0.0`
gap), job totals equal Σ plates, report text, spec collection, CLI exit codes,
and 5 end-to-end runs over `tests_deps/columns_demo_job.yaml` +
`material_demo_job.yaml` with the metric invariants pinned.
Baseline (`--modes declared`, 24×16 sheets), the numbers U1 is judged against:

| spec | plates | cov% | gap in² | rot |
|---|---|---|---|---|
| 2026-11-17 ADK CPR 111261 | 4 (plate 2 at 17.2%) | 45.7 | 26.0 | 107 |
| 2026-10-29 seer 1811 | 3 | 63.8 | 49.3 | 133 |
| 2026-11-17 CES SFW3525 | 2 | 93.9 | 6.0 | 118 |
| 2026-11-23 CES vons 1897 | 3 (plate 2 at 35.9%) | 52.8 | 16.0 | 0 |

Full sweep (every `examples/job_specs` spec): `.probe/u0-baseline.txt` /
`.json` (gitignored scratch).
*Accepted:* reproduces the §measured numbers; full suite green.

**U1 — Rotation preference + ensemble width (D2 + D3). ✅ DONE (2026-11-24).**
Shipped as `rotation_preference` (enum `RotationPreference`, values `fill_axis`
= the historical fill-order ranking, `minimize` = fewest rotations), library
default `fill_axis`, shop default `minimize` in `job-config.json`.

- `schema.py`: `RotationPreference(StrEnum)` + `DEFAULT_ROTATION_PREFERENCE`,
  `JobSpec.rotation_preference`, the same field on `PlateSpec` (cascade-fills
  from the job, explicit value wins), and a validator rejecting an explicit
  `rotation_preference: fill_axis` on a `*-simple` layout (that layout never
  rotates, so the declaration would be a lie).
- `packing/layout.py`: `_fill_rank_key(footprint, fill_axis, cross_axis,
  rotations, rotation_preference)` — footprint leads in both policies; `minimize`
  puts the rotation count second, `fill_axis` keeps the fill-order span
  (negated) second with rotations as the final tie-break. Threaded
  `generate_layout(_with_bounds)` → `_pack_rect_entries_with_pinners` →
  `_pack_groups` / `_pack_pinned_entries` → `_pack_group` → `_pack_best`, with
  `_group_rotation_preference(pool, fallback)` so a **per-plate** override wins
  for a pass that packs exactly that plate, and a pool that disagrees falls back
  to the job value.
- `job_config.py`: `JobDefaults.rotation_preference` + the shop-default key
  list; `job_prep.py`: `JobPreparationContext.rotation_preference` +
  `apply_shop_defaults` (spec > config > library); plumbed through
  `cli/generate.py` (single-job **and** jobset plan paths), `jobset.py`,
  `export_per_cutter_plts.py`, `vectorize.py` (the 4 `generate_layout` sites).
- `PACK_CONFIGS` / `PACK_CONFIGS_COLUMNS` stay at **11 × 2 passes** — widening
  them measured **0/79 specs improved** (`.probe/rectpack_rank.py`), so D3 ships
  as "documented as measured, not widened".
- Tests: `tests/test_rotation_preference.py` (24) — rank-key policy in both
  directions, footprint dominance, default-is-`fill_axis`, identical geometry
  ties, `_pack_best` policy on real geometry (4 labels: 1 rotation → 0;
  6 labels: 2 → 1, same plate count), schema default / cascade / plate override
  / simple-layout rejection, shop-config parsing (both spellings, typo raises),
  ensemble size, and the benchmark `--set job.rotation_preference=…` override
  beating the shop config.

*Acceptance measured — the honest result.* The plan expected "the 4 rotation-heavy
specs drop to 0–2 rotations". **They do not move: ADK 107 → 107, seer 133 → 133,
SFW3525 118 → 118, CES vons 1897 0 → 0** (identical plates/coverage/gap). A
rank-key spy proves the knob reaches the packer (`rank_calls=42`, spec pref
`minimize`), so this is not a plumbing failure: on real jobs **`minimize` wins
only when no material-minimal layout exists with fewer rotations**. The same
experiment on synthetic plates shows where it does pay: 20 rects on 10×10 bins →
5 plates / 30 rotations → **5 plates / 4 rotations** at identical material
(`.probe/probe_rank_key.py`). The knob is therefore shipped as a per-job tool
(a shop that values upright engraving over the last square inch flips it per
job), not as a global win, and **material (U2) is the lever for the complaint**.

*Harness note:* `--set` paths are rooted at the spec document, so a job-level knob
is `--set job.rotation_preference=minimize` (the bare key lands at the document
root and is ignored by the schema).

**U2 — Material sharing (D1-b, chosen).** Design doc first
(`docs/plans/packing-material-sharing.md`), then the packing change. Scope is the
`plate → set(materials)` contract above (packing, Phase 3 assembly + file naming,
`usage_stats` material attribution, proofs, plate clearance/weight resolution,
jobset merged sheets), each with its own test. Written as a separate plan doc
after D1 is chosen.
*Accepts (D1-b):* ADK CPR 111261 packs in 3 plates (not 4) and CES vons 1897 in
2, with every per-material `usage` row still summing to the job total.

**U3 — Make it visible in the report.** Additive `usage.layout` block:
per-plate `coverage_pct`, `empty_sq_in`, `rotated_labels`; job totals `plates`,
`rotated_labels`, `empty_sq_in`. `REPORT_SCHEMA_VERSION` stays **2** (additive
key, `null` on failed runs, same rule as `usage.weight`), `summary.txt` gains a
`layout:` section, jobset rows carry `plates` / `rotated_labels`. Without this,
"did the layout get better?" is only answerable by eye.
*Accepts:* `Σ by_plate.empty_sq_in == total.empty_sq_in`, and the block's plate
count equals `len(packed_plates)`.

Suggested order: **U0 → U1 → U3 → U2** (U1 is the cheap half of the complaint;
U2 is the fix for the worst number and needs a decision + its own plan).
Done: **U0, U1**. Next: **U3**, then the U2 design doc.

*U1 evidence for U2 (why material is the lever):* on ADK CPR 111261
`columns` = 4 plates / 306.2 in² empty and `rows` = 5 plates / 334.8, and the
**union** of their per-plate bounding boxes is 1170.4 in² of 1536 — i.e. 365.6 in²
is "in the plan on one axis, out of it on the other", which no fill order,
algorithm, sort, clearance, `max_labels_per_plate`, `plate_count` (6 sheets is
material-identical to 4) or rotation ranking can recover. Only sharing a sheet
between materials can (U2 / D1-b).

## Verification (every unit)

- `ruff check . && ruff format --check .`; `.venv/bin/python -m mypy plt_optimizer`.
- `.venv/bin/python -m pytest -q --no-cov` (full suite), `pre-commit run --all-files`
  with `UV_CACHE_DIR=$PWD/.venv/.uv-cache` (the hooks shell out to `uv run`).
- `.venv/bin/python scripts/run_integration_test.py --no-plots` — layout report +
  per-plate PLTs unchanged for jobs that do not set the new knob.
- `.venv/bin/python scripts/packing_benchmark.py` before/after each unit, committed
  in the PR description.
- A/B rule for PLT diffs: compare the **coordinate multiset** per file
  (`sort | md5`), never raw md5, and run the baseline **twice** —
  `kw_uv_15_bh_*.plt` legitimately flips tour order between identical runs.

## Boundaries & risks

- `packing/layout.py` is on the Windows 7 / Python 3.8 core path: no
  `match`, no PEP 604 unions at runtime, `pathlib` only; `matplotlib` stays out.
- Determinism is a product requirement. New code must not iterate a `set`;
  the ensemble scans configs in fixed order and keeps the **first** candidate on
  an exact key tie, so widening the pool is a pure search-width change.
- Widening the ensemble changes layouts for jobs that set neither knob
  (measured: vons 1897 gains 5.4 in² of material). **Accepted with D3 = always
  on**; the gate behind `rotation_preference` (`fill_axis` ⇒ today's 4/2 configs
  exactly, a one-line branch in `_pack_best`) is the agreed fallback if a
  production job ever regress.
- Relief jobs: relief passes multiply plate work per stage; U1 changes label
  placement, so relief stage output must be re-verified on a relief spec.
- Out of scope: ellipse packing, rectpack upgrade to 0.4.x, exact-grid/solver
  packing, `*-simple` modes, the `spacing`-doubling at plate edges (a separate
  upstream finding in `.probe/FINDINGS.md` §2), `<scale>` rotation accounting.

---

# Addendum (2026-11-24): plate-major fill + cutter runs — measured, then designed

Requirement added by the user after U1 (m00873): *when a plate can reach ~100%
coverage the plan must take it, even if the tail plate gets worse*, and *a plan
that reaches that fill with one cutter on the plate beats one needing a cutter
change*.

## Measurement 1 — the plate-0 fill is already at the cap on these jobs

`.probe/probe_plate_fill.py` (per-plate bbox + used area of the plans the
production search produces, real resolved-label rects, clearance-aware):

| Spec | layout | plate 0 | plate 1 (tail) |
|---|---|---|---|
| ADK CPR 111261 | rows | **100.0 %** of the plate, bbox 24.00x16.00, coverage-in-bbox 100.0 % | 82.8 %, bbox 24.00x14.00 |
| ADK CPR 111261 | columns | 100.0 % | 82.8 %, bbox 20.00x16.00 |
| seer 1811 | rows | **100.0 %** | 91.3 %, bbox 23.00x16.00 |
| seer 1811 | columns | 96.0 % | 95.3 % |
| CES vons 1897 | rows | 99.0 % | 59.4 %, bbox 15.00x16.00 |
| CES SFW3525 | both | 99.5 % | 91.1 % (14x16 plate) |

So on this sample **plate 1 is already essentially saturated** and the "wasted"
material is the remainder on the **tail** plate — which is what the user says they
will accept. The sample does not contain a case of "plate 1 assembled with
100 % coverage available but not taken"; to design against the real complaint we
need a spec that reproduces it (see the ask at the end).

## Measurement 2 — re-ranking the outer candidate pool changes nothing

`.probe/probe_plate_major.py` ranks the same candidate pool (both layouts x both
rotation preferences, produced by the production `_pack_best`) with today's
material-first key and with a plate-major key (plate count, then per-plate bbox
fill densest-first in 0.5 in^2 bands, then cutter runs). **The winner is identical
on all 4 specs** (same plates, same fills, same cutter fan-out `[1, 1]`).

Why: the outer pool is ~4 plans and within one layout the packer's own search has
already converged. **The lever is the ranking key inside `_pack_best`, not the
outer plan selection.**

## Measurement 3 — the mechanism to change

`_fill_rank_key` (packing/layout.py) ranks `(footprint, -fill_axis, cross_axis,
rotations)`, and `_fill_axis_extents` measures the **least-filled** plate's
fill-axis ratio. The fill term is therefore a *max-min* (raise the worst plate),
which is exactly the opposite of front-loading: it will spread labels so the
weakest plate looks better, rather than saturating plate 1 and leaving the tail
sparse. That is the single change that implements "take the 100 % first plate".

## Proposed U2 (replaces the D1-b design doc as the next unit)

1. **Plate-major fill key.** Replace the max-min fill term with a per-plate fill
   vector compared plate-by-plate: `(plate_count, banded_fill(plate_0) desc,
   banded_fill(plate_1) desc, ..., cutter_runs desc, rotations, ...)`, banded
   (0.5 in^2 default, configurable) so near-ties keep today's behaviour and the
   output stays reproducible. Plate 1 saturating is then strictly preferred to
   spreading, at equal plate count (so material is never wasted — plate count is
   still the first term).
2. **Cutter-run term.** Add the per-plate count of cutter runs = distinct
   `(cutter, color)` text layers + one boundary run + one hole run (exactly the
   per-cutter PLT file fan-out the export layer produces). A plan that saturates
   plate 1 with one cutter beats one that needs a cutter change on that plate.
3. Both terms live in one place (`_fill_rank_key` + a new
   `_plate_fill_vector(plates)`), inherit the existing determinism contract
   (no set iteration, `round()` to packing precision), and are covered by the U0
   harness sweep over all 79 specs.

## Open decisions (need the shop's answer)

- **Does plate-1 saturation ever buy an extra plate?** Today the plate count leads
  the key, so a 100 %-plate-1 plan that needs 3 sheets loses to an 85 % plan on 2.
  Confirm that material still wins (my recommendation: yes, keep plate count first).
- **Band size** for "the same fill" (0.5 in^2 proposed) and whether the fill is
  measured as bbox-of-placed-labels (what the plate must be cut to) or
  used-area/plate-area (what is engraved).
- **Cutter runs vs fill**: which wins when a one-cutter plate is 95 % full and a
  two-cutter plate is 100 % full?

## Ask

Send one job (spec + the plate layout you got) where **plate 1** visibly wastes
material while a fully-saturated plate 1 was achievable. Every spec I can sample
already saturates plate 1, so I cannot tune the new key against the real failure
mode without it.

## Measurement 4 — the plate axis is the lever; U1' searches both (implemented)

Raw matrix: `.probe/axis_matrix.txt` (one `generate_layout` run per
`layout` × `rotation_preference` cell, real specs, 24×16 plates). Each cell
lists the per-plate bbox fills and the job's placement-rotation count:

| spec | rects | `columns` | `rows` |
|---|---|---|---|
| CES vons 1897 | 146 | `[100.0, 62.5]` rot 0 | `[100.0, 62.5]` rot 0 |
| ADK CPR 111261 | 145 | `[100.0, 83.3]` rot **131** | `[100.0, 87.5]` rot **0** |
| seer 1811 | 152 | `[100.0, 95.8]` rot **144** | `[100.0, 95.8]` rot **94** |
| CES SFW3525 | 122 | `[100.0, 54.2]` rot **118** | `[100.0, 56.2]` rot **0** |

Two facts fall out:

1. `fill_axis` and `minimize` are **byte-identical in all eight cells** — the
   `rotation_preference` knob cannot move these jobs, because the plan key
   breaks material ties on `plate_rotation` (plates turned 90°) before it ever
   counts placement rotations. The lever is the **fill axis**, not the rank key.
2. The axis choice *is* the rotation lever: `rows` beats `columns` on rotations
   with fill equal or better (83.3→87.5, 54.2→56.2), which is why the shop's
   rotation-heavy jobs read as "the packer rotates everything".

**U1' (decisions D4/D5, this unit):** the search now runs both axes and ranks
across them, so a job pinned to `layout: rows` still gets the column-axis plan
when it is strictly better, and vice versa. Implemented in
`plt_optimizer/generate/packing/layout.py`:

* `_fill_axis_extents(packer, transpose) -> tuple[float, float]` — the
  *least*-filled plate's fill ratio along the layout's fill axis and its cross
  axis, in plate space (in the transposed frame the packer's X axis is the
  plate's height, so the spans are swapped back).
* `_fill_rank_key(plates, policy, fill_axis, cross_axis, rotations)` — the plan
  key is now `(footprint, fill_axis, cross_axis, rotations)`; the fill axis
  enters negated (a *longer* fill-axis span is a tighter, better row) so the
  layout's own fill order is preferred, the cross axis breaks that tie, and the
  rotation count decides last.
* The candidate loop feeds both axes through `_pack_best` (`transpose=True` for
  the column frame) and keeps the best key across all of them.

Visible effect, pinned by `tests/test_layout.py::TestRotationDemoExample`:
`tests_deps/rotation_demo_job.yaml` used to *require* rotation (its 11″-tall
banners only fitted lying down on the 10″ scrap sheet, so `allow_rotation=False`
raised `LayoutFitError`). The column-axis pass tiles them upright, so the
rotation-free job is now a fit — 14 labels, 0 rotated, `sheet_2` 51.8 % and
`scrap_24x10` 50.0 % covered. The rotation-on plan stays denser on the first
sheet (82.9 %), which is the empirical case for keeping
`prefer_less_rotation` off by default: the scorer decides, not the axis.

**A/B against `HEAD`** (full `pytest -q --no-cov` with every tracked change
stashed, then restored): the failing-test set is **identical** — 4
order-dependent log-capture tests
(`test_phase3_export.py::TestCompositeProofSheet::test_compose_failure_is_logged_not_fatal`,
`…::TestHoleAdjustForCutterDemoExample::test_degenerate_warning_names_the_label`,
`test_run_integration_report.py::TestPhase5GenerationReport::test_findings_come_from_the_capture`,
`…::TestFailureReport::test_writes_failed_status_with_the_stage`) fail in the
full-suite order at `HEAD` too, and pass in isolation. Zero regressions from
this work. Gate: `ruff check` + `ruff format --check` clean, `mypy
plt_optimizer` clean (87 files), full suite 4 known-failing / rest green.

## Measurement 5 — the shop's own "perfect first plate" case (2026-11-24)

Job: `examples/job_specs/2026-11-23 CES vons 1897.yaml` (128 case tags at 3x1,
then 2x4, a 10x2 placard, three 7x5.33 `kw(uv)` placards). 24x16 `kw` plate,
`allow_rotation: false`. Measured with `scripts/packing_benchmark.py`:

| mode | plate 1 | plate 1 bbox | cov% | plate 2 | plate 2 bbox | total bbox | rot |
|---|---|---|---|---|---|---|---|
| `columns` (rectpack, today's default) | **121** | 24.00x16.00 | 99.0 | 22 | 8.00x16.00 (128 in²) | **623.92** | 0 |
| `columns-simple` (flush grid) | **128** | 24.00x16.00 | **100.0** | 15 | 14.00x16.00 (224 in²) | 719.92 | 0 |
| `rows-simple` (flush grid) | **128** | 24.00x16.00 | **100.0** | 15 | 24.00x6.00 (144 in²) | 639.92 | 0 |

The leading 128 labels are one size, so a flush grid tiles the plate exactly
(8 columns x 16 rows = 128 = 384 in² = 100 %). `rectpack` leaves 4 in² of
fragmentation on plate 1 (121 labels) and pushes the 7 stragglers onto plate 2,
where they let plate 2 pack tighter — which is why today's key prefers it:
`_fill_rank_key` leads with **total footprint** (623.92 < 639.92) and its fill
term is the **least-filled** plate (Measurement 3). The shop pays for **plates**
(3 in both cases, identical material), not for the bbox of the ink, so the plan
that saturates plate 1 is strictly better operationally: one full sheet, one
short sheet, no 7-label tail to run separately.

Two levers, both cheap and deterministic:

1. **Grid seeds in the ensemble.** `pack_simple_grid` is O(n) and never rotates.
   Run `columns-simple` and `rows-simple` over the whole rectpack group and
   submit each as a *plan candidate* next to the rectpack result. When content
   order tiles a plate (this job), the grid plan wins on fill; when it does not
   (mixed sizes), rectpack keeps winning — the ranking decides, nothing is
   hard-coded.
2. **Plate-major (greedy) fill.** Replace the max-min fill term with a banded
   plate-major vector, and/or pack plate-by-plate (one bin per pass, ensemble
   per plate, remainder cascades). Plate count stays the first term, so a
   saturated plate 1 never costs an extra sheet; below that tie the first plate
   dominates. This is Proposed U2 items 1-2, now with the shop's own fixture.

## Measurement 6 — what closed the vons 1897 case, and what `plate_fill` adds (2026-11-24)

The shop re-ran `scripts/run_integration_test.py "examples/job_specs/2026-11-23
CES vons 1897.yaml"` after Measurement 5 and plate 1 was still not full. Root
cause: Measurement 5 predates **U1' (the dual-axis search, Measurement 4)**. The
`columns` group now ranks candidates measured on *both* plate axes, and that is
what fixes this job: plate 1 = **128 labels, 100.0 % coverage, 0 rotated**, plate
2 = 15 (the 2x4 + placard tail), plate 3 = the three `kw(uv)` placards —
verified from `usage.layout` in `generation_report/report.json`, and identical
under both `plate_fill` policies. Nothing about this job needs the greedy
ranking; the axis was the whole story.

`plate_fill` (Proposed U2 items 1 + 2 minus the cutter-run term, shipped as the
default `plate_major`) is the lever for the class of job where the ranking — not
the axis — is what leaves a sheet short:

- **Ranking**: `_plate_fill_rank_key` = `(sheet_area, -banded_fill(plate_1),
  -banded_fill(plate_2), …, bbox, rotations)`; `rotation_preference: minimize`
  promotes rotations the same way it does in `_fill_rank_key`. Sheet area leads,
  so the shop's answer to open decision 1 stands: **material still wins** — a
  saturated plate 1 never buys an extra sheet. Band = `PLATE_FILL_BAND_SQ_IN`
  (open decision 2: fill is *used label area*, not the plate frame; the envelope
  is reported separately in `usage.layout`).
- **Grid seeds** (lever 1 of Measurement 5): under `plate_major`,
  `_pack_group` submits the matching flush grid as a whole-plan candidate and
  drops it if it leaves any label unpacked.
- Open decision 3 (cutter runs vs fill) is **still open** — the cutter-run term
  is not implemented; nothing in the key counts cutter changes.

Measured (the gate): a 62-spec sweep of `examples/job_specs/`,
`plate_major` vs `--set plate_fill=global`, is a **no-op everywhere** — identical
plate counts, per-plate coverage, rotations and total sheet area on all 62 runs.
The ranking only bites on mixed rect sets; the pinned fixture in
`tests/test_plate_fill.py::TestPackGroupPlateFill::test_plate_major_admits_the_flush_grid_plan`
(128 3x1 + 6 10x10 on 24x16) is the constructed case: `plate_major` = 3 sheets
[63, 63, 8] with the front sheets 99.7 % full, `global` = 4 sheets [128, 2, 2,
2]. Material is the term that moves (1152 in² vs 1536 in²), and it moves in the
greedy plan's favour even though that plan rotates 30 labels — under a shop
default of `minimize`, material still outranks rotation, which is the intended
precedence.

---

## 7. Status as landed (U0–U3 + U1', uncommitted)

Shipped, measured on `examples/job_specs/2026-11-23 CES vons 1897.yaml`
(`scripts/run_integration_test.py`, exit 0):

```
plate 1: 128 labels  coverage 100.000%  empty 0.000 in2  rotated 0   (was 121 @ 98.958%)
plate 2:  15 labels  coverage  29.167%  rotated 0
plate 3:   3 labels  coverage  29.147%  rotated 0
```

- **U0** `layout.plate_fill` (default `0.0`), `layout.plate_rotation ∈ {0,1,2}`,
  `layout.rotation_mode` — `tests/test_plate_fill.py`.
- **U1'** dual-axis search: `PLATE_ROTATION_POLICIES` is
  `(MIN_RECTS, PERFECT_FIT, MAX_SIZE, MAX_AREA)` on every plate-rotation mode, so
  `plate_rotation ∈ {0,2}` × `rotation_mode ∈ {global, major}` is a real 4-way
  ensemble. `plate_fill` only widens the search (D9); ranking is
  `(sheet_area, banded fills, area_fills, rotated, max_dim, -height)`.
- **U1** grid prefix + `rectpack` tail (`_grid_prefix`, `_grid_plan_plans`,
  `_rectpack_plates`, `_rectpack_tail_plans`) with a complete-plan gate.
- **U2** shipped as the `area_fills` rank term (+ `PLATE_FILL_BAND = 0.10`
  quantization of the fill vector). The envelope/`empty_area` term was **not**
  built: on the real job the 100 %-of-area plan wins without it.
- **U3** additive `usage.layout` block in `generation_report/report.json`, the
  `summary.txt` "Job layout:" section, and the fleet rollup
  (`layout_plates` / `layout_rotated_labels` / `layout_empty_area_sq_in`).
- D4/D5 dropped: `fill_axis_pairs` is measured **on**, not off. The axis ablation
  (6 sample specs, raw `.probe/axis_ablation.json` at the time) put global-major
  first on 5/6, and `plate_rotation=2` alone produced zero plans.

Still open:

1. `plate_fill` default. `0.0` is shipped, so today's jobs keep the D9 ranking
   (fill vector primary). `0.10` (area primary, fill vector at 10 % granularity)
   measured the same vons result and better tail packing on `3 plate test.yaml`
   (12→11 plates). Shop decision.
2. **Attribution of the vons win.** The flush grid can only saturate a sheet when
   its pitch divides the plate with no remainder. The vons plate 1 reaches 100 %
   of the sheet's *area* with 128 labels whose ink boxes are far smaller than the
   3x1 plate size in the spec, so the win is credited to the `area_fills` term
   (plus the widened ensemble), not to the grid prefix — a unit fixture built
   with ink == requested size does **not** reproduce it. Unresolved: whether the
   grid prefix earns its keep on any real shape, and the measured case where a
   3-plate plan beats a 2-plate plan (see `test_grid_prefix_never_uses_more_material_than_rectpack`,
   which pins only that material keeps the search honest).
3. `examples/job_specs/*.yaml` pick up one trailing-newline line per file from
   integration runs; they are user-facing samples and stay uncommitted.
