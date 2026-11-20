# Estimated label weight in the usage report (`usage.weight`)

Status: **shipped** (2026-11-20). Implements `docs/TODO.md` "Add to summary
txt/json report approximated weight of engraved labels".

## Product decision

A new `material_weight_per_square_inch` field (pounds per square inch) cascades
**job → plate**, and the generation report gains a `usage.weight` block that
estimates the weight of the **engraved labels**: each label's nominal
`width × height × count` area times the density of the plate it was packed on.

Four decisions were made explicitly, and are the reason the feature looks the
way it does:

- **Labels, not sheets.** The TODO's arithmetic (a 16×24 sheet ≈ 1 lb) reads
  like sheet weight, and the product ships whole sheets. What is useful is the
  label number — what a run's engraving weighs in material. Sheet weight is one
  multiplication from the `usage.material` rows (which carry the sheet area),
  so the plate number is not reported.
- **Four grains** — job total, per label spec, per plate, per material —
  because that is exactly what `usage.distance` and `usage.time` already
  provide, and a new block that breaks at a lower grain than its neighbours is
  a wart.
- **Opt-in.** Unset everywhere ⇒ `available: false`, `total: null`, and
  `summary.txt` says `not estimated`. The code default is `None` at every tier;
  the number `0.00260417` lives in `job-config.json`, not in the schema. An
  unconfigured job must never read as a 0 lb job. This mirrors `usage.time`.
- **Cascade job → plate only.** Never label-level (a label's density is decided
  by the sheet it lands on), and no material-name registry in code —
  `material` is a free-text field, so per-material densities would be a
  validation and aliasing problem (ACM / "acm 3/16" / blank). Deferred. Per-plate
  overrides give the same power, explicitly.

## Geometry

```text
label weight  =  width_in × height_in × count × density(plate)
density(plate)=  plate.material_weight_per_square_inch
                 ?? spec.material_weight_per_square_inch       # cascade
                 ?? None                                        # not estimated
```

`_material_usage` already computes `width × height × count` per placement with
the same spec-identity keying, so the weight block counts labels the same way
the material block does. The result is:

- **`label_sq_in` is exact** — it is the nominal rectangle, not a measurement.
- **`lb` is an estimate** — nominal area × a user-declared density. Rounding to
  3 decimals is display precision, not a claim of accuracy.

Area, never volume: this is a per-square-inch density, and engraving removes a
negligible, unmodelled amount of material (relief passes included).

## Plate-id resolution

`JobSpec.material_weight_for_plate(plate_id)` mirrors the packing layer's
material lookup: a declared `PlateSpec` matched on its **exact** id supplies its
own (already-cascaded) density; any other id falls back to the job-level value.

Ids that never match a declared plate, and are therefore resolved by the job-level
fallback, are the real cases in production:

- unbounded mode's generated `default_plate_N` and material-prefixed ids;
- a jobset's merged-sheet ids, numbered from the batch, so a member's packed
  plate carries the batch's id, never its own declared one.

The last point is the documented jobset limitation: a member that set the
density **per plate** and lands on a merged batch sheet reports the member's
**job-level** number, not the per-plate one. The member's own
`usage.material` rows show which batch sheet its labels went to, so the gap is
visible.

Explicit `0.0` is a real value: it beats the job tier and yields a genuine
0 lb row (not `null`). Negative values are rejected by `ge=0.0`. An explicit
YAML `null` counts as unset — the value is dropped in
`PlateSpec._drop_null_clearances` so the cascade applies, exactly as for
`material`.

## Shape

`report.json` → `usage.weight`:

```json
{
  "available": true,
  "note": "",
  "total": {"label_instances": 12, "label_sq_in": 480.0, "lb": 1.25, "oz": 20.0},
  "by_material": [{"material": "uv", "material_label": "uv", "label_instances": 12,
                   "label_sq_in": 480.0, "lb": 1.25, "oz": 20.0}],
  "by_plate":    [{"plate": 1, "plate_id": "plate1", "material": "uv",
                   "material_weight_per_square_inch": 0.00260417, "label_instances": 12,
                   "label_sq_in": 480.0, "lb": 1.25, "oz": 20.0}],
  "by_label":    [{"spec_id": "SPEC-1", "plate": 1, "material": "uv",
                   "material_weight_per_square_inch": 0.00260417, "instances": 3,
                   "label_sq_in": 24.0, "lb": 0.063, "oz": 1.0}],
  "unconfigured": {"label_instances": 0, "label_sq_in": 0.0}
}
```

- Rows sort heaviest-first; areas/weights use `_round` (3 dp) while
  **`material_weight_per_square_inch` is emitted verbatim** — it is a configured
  number, not a measurement, and the shop tunes it.
- Every row echoes its density, so each weight is traceable to the number that
  produced it. A plate with no density is a row with
  `material_weight_per_square_inch: null, lb: null, oz: null` — never a silent 0.
- `by_label` is keyed `(spec_id, plate)`, not per placement id: a 500-copy job
  stays readable, and `report.json` remains the complete record. Copies split
  across plates produce one row per (spec, plate).
- `oz` is derived (`lb × 16`, `OZ_PER_LB`), never configured.
- `unconfigured` is always a dict (never `null`) listing what was **not**
  weighed, so readers do not branch on `available` to inspect it.

Invariants, test-pinned: `Σ by_plate.lb == Σ by_material.lb == Σ by_label.lb ==
total.lb`, and `total.label_sq_in == usage.material.label_nominal_area.sq_in`.
`usage` gains a key; `REPORT_SCHEMA_VERSION` stays 1 (additive, `null` on
failed runs).

## Text report

Between the material and distance sections, mirroring the `time:` block's
header + sub-line layout:

```text
  weight (estimated, from the labels' nominal area x the plate density):
    total: 1 lb 4 oz (1.250 lb)  12 label(s)  480.000 sq in of material
    by material: uv 1 lb 4 oz (1.250 lb)  acm 0.67 oz (0.042 lb)
    by plate: plate 1 (uv, 0.00260417 lb/in2) 1 lb 4 oz (1.250 lb)  12 label(s)
    by label: SPEC-1 x3 on plate 1 1 oz (0.063 lb)
    … and 3 more (see report.json)
```

`_fmt_lb` renders `≥ 1 lb` as the compound `1 lb 4 oz (1.250 lb)` form, sub-pound
as `0.67 oz (0.042 lb)`, exact zero as `0.000 lb`, and missing as `n/a`.
Per-label rows use the existing `SUMMARY_USAGE_LABEL_LIMIT` (50) cap.

## Jobset tracing

`_job_usage_totals` gains `weight_total` (the member's `usage.weight.total.lb`,
`None` when unavailable) so every `jobset.json` job row carries it, and
`cross_job_summary` gains `total_weight_lb` from the batch's own `usage.weight`.
Member totals are measured from the member's spec; the batch total is measured
from the batch spec, so a batch whose YAML declares no density reports
`total_weight_lb: null` while its members stay weighed — `null` is never folded
into a sum as 0, and the batch total is never recomputed from member rows
(same rule as `total_files`, which counts PLT files, not per-job `files`).

## Caller contract

`_weight_usage` resolves densities from the **spec** only. A shop default set in
`job-config.json` weighs the whole job because `apply_job_config_defaults` injects
the job-level density onto every plate **before layout** — so the spec that reaches
the reporter already carries it. The resolver deliberately does not re-apply the
config layer to an arbitrary spec (the plate cascade has already run; re-applying it
after the fact could not reach per-plate values). Consequence: **every caller that
measures usage must pass the same `job_spec` / `job_config` pair the job was laid
out with.** A bare spec with no config means nothing is configured, and the block
reports "not estimated" — 0 lb would claim the labels are weightless. This is what
phase 5 of `scripts/run_integration_test.py` got wrong when the block shipped; the
CLI (`cli/generate.py`), the jobset batch root, and the jobset member reports
(`jobset_report.build_job_usage`) all pass the pair.

## Boundaries

`usage_stats.py` measures; it never emits toolpaths. PLT and PDF bytes are
untouched by this feature. `_weight_usage` runs inside `build_usage_stats`,
which every caller wraps in `try/except`, so reporting cannot fail a job.

## Verification

- `ruff check . && ruff format --check .`, `.venv/bin/python -m mypy plt_optimizer`.
- `.venv/bin/python -m pytest -q --no-cov` (full suite).
- `.venv/bin/python scripts/run_integration_test.py --no-plots` — every
  `summary.txt` shows a `weight:` section whose total equals Σ per-plate (the
  single-job phase 5 path is the one that regressed; `tests/test_run_integration_report.py::TestPhase5UsageWeight`
  pins it).
- `main.py generate --jobset …` — `jobset.json` job rows carry `weight_total`
  and `jobset.total_weight_lb` equals the sum of the members' rows.
- Manual: a single 16×24 label at the shop default density reads
  `1 lb 0 oz (1.000 lb)`; setting the value on one plate of a two-plate job
  moves only that plate's row.
