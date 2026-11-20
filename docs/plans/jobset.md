# `jobset` — batch several job specs onto shared plates

Rule: a `jobset:` YAML names **existing per-job files** and nothing else. One
`generate` run resolves every member through the single-job path, packs all
members' labels as **one pool of plates**, and writes every plate as **one
toolpath segment per tool** - the sheet's borders and holes in one run, each
text cutter in one run. Material utilisation is the point: packing each job
alone leaves a gap at the end of each job's last sheet. Cutter utilisation is
the second: engraving a shared sheet job by job walks the same tool over the
same sheet once per job.

Status: SHIPPED. `plt_optimizer/generate/jobset.py` (plan + gate + provenance),
`plt_optimizer/generate/job_prep.py` (the shared per-job preparation path),
`plt_optimizer/generate/jobset_report.py` (batch report + manifest), the
jobset segmentation in `plt_optimizer/generate/vectorize.py`, the overlay in
`plt_optimizer/diagnostics/plotter.py`, the CLI (`generate --jobset`) and
`scripts/validate_job_spec.py`. Pinned by
`tests/test_jobset_schema.py`, `tests/test_jobset_merge.py`,
`tests/test_jobset_export.py`, `tests/test_jobset_reports.py`,
`tests/test_jobset_plots.py`.

> **Record, not plan.** This file documents the shipped contract. The design
> plan that produced it is not kept here, and the emission model changed shape
> during development (whole-plate layers, then one file per job, then the
> per-tool merge below). Any wording that says a batch writes one file per job
> is historical, not the contract: *Emission* is the authority. Files are per
> tool; only the plate-level preview spans jobs, and it keys them.

## The spec

```yaml
# batch.yaml - paths are relative to this file; order is the layout order
jobset:
  name: "Evening Batch"             # optional; default: the file's stem
  jobs:
    - job: "job-a.yaml"             # a complete single-job spec
    - job: "job-b.yaml"
```

The list shorthand (`jobset:` directly listing `job:` entries) is accepted by
the model; the mapping form above is what the shipped example uses because it
names the batch. Models: `JobSetSpec` / `JobSetEntry`
(`plt_optimizer/generate/schema.py`), generated into
`docs/skills/engraving-jobs/engraving-job-complete-reference/references/JOBSET.md`
+ `jobset.schema.json`. Both spec kinds reject unknown keys, and the two roots
refuse each other: `parse_yaml` on a `jobset:` file and `parse_jobset_yaml` on a
`job:` file both raise, naming the command to run instead. A jobset cannot nest
a jobset.

## What is per job, what is shared

| Per job (unchanged) | Shared by the batch |
|---|---|
| cascade, fonts, markup, cutter sizes, hole placement, relief | the pool of plates — ONE packing pass |
| `generation_report/` + `summary.txt`, each with its own `usage` | the plate proofs `pdf/*_all_*` (with a per-job overlay) |
| its own label/plate ids (namespaced in the run's internals) | the run-level tool options, the shop config, the inventory |

**Emission is per tool, not per job.** A plate that carries several jobs is
written as one segment per **tool-layer**: the plate's structural strokes are
bucketed by (boundary/hole cutter, kerf-buffered width, `tool_options` header
signature) and its text strokes by (pen, header signature), so members that
agree on the tool and its engraver parameters share one `bh` file and one `txt`
file and are routed together (`vectorize.py::_bh_buckets`,
`vectorize.py::_text_buckets`). The invariants are that a stroke is never routed
across two layers and a layer never mixes two tools - optimisation, sweeps and
merging all run inside one bucket. What is deliberately **not** guaranteed any
more is per-job routing: strokes of different jobs are co-routed inside a shared
layer, and that is exactly the saving. Per-job reportability survives it through
`JobProvenance` and `PerCutterExport.file_jobs` (which names every owning job of
every file), not through file separation.

When `tool_options` diverge, a layer cannot merge: two jobs that share a cutter
and pen but give it different `tool_options` get one file per distinct signature
and the run **warns** that the cutter engraves twice per sheet, naming the
jobs (`vectorize.py::_warn_split_layers`). Same tool, same parameters, one run.

Every PLT file is named for the job that owns its strokes, or for the **batch**
when the layer is shared, so a batch's names match what each job produces
standalone for its own layers:

```text
[<material>_][p<plate>_]<cutter>[_<color>]_txt_<job_key>.plt
[<material>_][p<plate>_]<cutter>_bh_<job_key>.plt
```

(the single-job naming contract, unchanged; `plt_optimizer/generate/AGENTS.md`
owns it). The `<job_key>` token is the owning member's id when a layer carries
exactly one job's strokes, and the batch id when the jobs share the layer - the
file belongs to every job engraved on that tool, and its membership is recorded
in `file_jobs` and the reports rather than in a name the 39-character plotter
cap would truncate. A plate's segment order is the jobset file order, which is
the run's determinism anchor.

## Provenance and composite ids

`build_jobset_plan` namespaces each member's ids so two jobs may both declare
`main` and `plate1`: labels become `<job_key>::<label_id>`, plates
`<job_key>::<plate_id>`, and auto-allocated sheets `plate:<n>::<job_key>`. The
`::` separator is chosen to be illegal in a spec id, so a split is unambiguous.

`JobProvenance` is the side table that maps every composite id back to its job
(`job_of_label`, `job_of_plate`, `base_id`, `records_by_job`,
`owners_of_labels`, `owners_of_plate`). It is the **only** channel from packed
geometry to job identity: nothing in the layout, packing, render or optimize
layers knows what a jobset is. Composite ids never reach a filename, a report's
label list, or a user-facing summary — every consumer calls `base_id` first
(`plt_optimizer/generate/AGENTS.md` carries the rule).

## The gate

`evaluate_jobset_gate` runs on the prepared members, before any rendering, and
`build_jobset_plan` raises `JobSetError` listing every problem at once.

**Hard errors** (the batch is rejected):

- members declaring different `layout` fill orders — one shared sheet has one
  fill order, so `columns` cannot mix with `rows`, and a `*-simple` grid fill
  cannot share a pool with the rectangle packer;
- a member declaring one plate id twice (namespacing makes that ambiguous);
- the set listing no job;
- a member file that is missing, listed twice, is itself a jobset, or fails to
  parse, expand, validate or resolve (the stage names the member, e.g.
  `resolve[seer_2963]`).

**Warnings** (the batch runs):

- `allow_rotation` and the job-level plate clearance disagree — the first member
  in file order wins for the whole pool;
- several members declare the same `(width, height, material)` sheet geometry —
  those sheets are pooled, and the line names the owners (qualified by composite
  key when two members share a display name);
- two members whose names sanitize to the same job key — the second gains a
  numeric suffix, which is visible in its file names;
- a `plate_id` pin the pool cannot honour (its plate is not in the merged list,
  or the batch runs unbounded) — named and dropped, so the label packs into the
  pool instead of silently reserving a sheet that nothing else may use.

## Reports

The batch writes three things over the run:

- `OUT/generation_report/report.json` — the normal generation report, plus a
  `jobset` block: the set identity, one entry per member (`job_key`, `job_id`,
  `job_name`, `report`, `distance_total`, `time_total`, `unattributed`) and the
  `cross_job` measure of what sharing saved. `usage.distance.by_job` and
  `usage.time.by_job` split the run per member (`by_operation` seconds, `total`,
  and the member's `share` of the run).
- `OUT/generation_report/jobset.json` — the manifest (`MANIFEST_NAME`):
  `jobset` (identity, layout, counts, status), `gate` (the findings), `jobs[]`
  (source file, declared plates, report path, usage), `plates[]` (the jobs on
  the sheet and each job's label ids), `files[]` (path, plate, layer, cutter,
  colour, owning jobs, bytes, sha256) and `cross_job`. Dropping one job's layer
  on a re-run is a lookup in this file, not a guess.
- `OUT/<job_id>/generation_report/{report.json,summary.txt}` — one report per
  member, the same shape a standalone run writes, carrying the ids its own file
  declares. A member report is measured from the files that carry **its**
  strokes, so its `by_cutter` / `by_label` totals are its own.

The accounting reconciles: each member's `usage.distance.total` sums to the
batch's `usage.distance.by_job` total, which **equals** the batch's measured
`distance.total` - a stroke is charged to exactly one job (the single attribution
rule in `usage_stats.py`), and `unattributed` is the reconciliation escape hatch
(pinned by `tests/test_jobset_reports.py`). The saving therefore does not show up
as a distance gap between members and batch. It shows up in `cross_job`:
`distance_saved` is the excess of the per-label sum over the measured total (the
travel a shared run does not make), `strokes_merged` counts the strokes in shared
`txt` layers on mixed plates, and the plate count itself is the material saving.

## Running it

```bash
plt-optimizer generate --jobset batch.yaml -o OUT/       # mutually exclusive with SPEC
plt-optimizer generate --jobset batch.yaml -o OUT/ --no-plots   # skip the PDF proofs
python scripts/validate_job_spec.py batch.yaml           # pre-flight; accepts both roots
```

The example shipped with the repo is
`examples/job_specs/job_sets/jobset example.yaml` (plus its two members).
Pre-flight output is one line: `OK batch.yaml: jobset is valid (2 members)`;
`--json` adds `kind: "jobset"` and `member_count`.

## Known limits

- A jobset cannot nest a jobset (hard error, by design: a nested run's reports
  would have to be merged twice).
- The set has one plate pool: a member's `plate_id` pin is honoured only while
  its sheet survives the merge, and an unresolvable pin is dropped with a
  warning rather than reserving a sheet the pool never packs.
- `unbounded` batches (no member declares plates) share auto-allocated sheets,
  so a member's `plate_id` (if any) has nothing to pin to and is dropped with
  the same warning.
- The plate proof's overlay is geometry by placement slot; it is a shop
  readability aid, not a cutting layer — the boxes are never emitted to the
  plotter.
- Time accounting is per segment file, so the rapid travel *between* a plate's
  job segments is not modeled (it is not in any segment's file).
- Two jobs that share a cutter and pen but give it different `tool_options`
  cannot share a run: the layer splits into one file per distinct header
  signature, that cutter engraves the sheet once per signature, and the run
  warns and names the jobs. Same tool with same parameters is one run; the
  split is the exception, not the shape of a batch (see *Emission*).
