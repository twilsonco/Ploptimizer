# End-to-End Integration Testing

## Overview

`scripts/run_integration_test.py` validates the full PLT-Optimizer **generate** pipeline,
from YAML job-specification ingestion through per-cutter PLT export, with
intermediate state dumps for manual verification. The framework covers:

- **Hierarchical resolution** and the inheritance cascade (TextLine → LabelSpec → JobSpec)
- **Job-config defaults** (`job-config.json`): top-layer default injection and the
  required-when-unconfigured gate (`--job-config` on the `generate` CLI)
- **Cutter compensation**, inventory snapping, and the 3x tolerance logic
- **Replacement text file** ("badge"/multiples) expansion via `expand_job_spec()`
- **Bounds-aware bin packing**, multi-plate allocation, 90° rotation (`allow_rotation`),
  and plate fill order (`layout`: `columns` default / `rows`)
- **Text–hole collision detection** (stroke-aware; collisions abort the job)
- **Vectorization** and lossless per-cutter PLT export (matplotlib TTF renderer + custom HPGL writer)
- **Coordinate validation** (all exported coordinates non-negative)
- **Visualization** via simple-outline PDF previews (always) and opt-in color-coded plots

> **Note:** This runner exercises the *generation* pipeline only. The toolpath
> *optimization* pipeline (parse → chunk → optimize → reassemble → write) is
> covered by the pytest suite (`uv run pytest`) and the batch benchmark tool.

## Quick Start

```bash
cd /path/to/PLT-Optimizer
source .venv/bin/activate        # or: uv run python scripts/run_integration_test.py
python scripts/run_integration_test.py
```

An optional positional argument selects an alternative job spec (relative to the
repo root or absolute):

```bash
python scripts/run_integration_test.py tests_deps/complex_test_job.yaml
python scripts/run_integration_test.py examples/job_specs/replacement_job.yaml
python scripts/run_integration_test.py tests_deps/rotation_demo_job.yaml
```

When a spec override is used, Phase 3 outputs land under
`test_output/integration_test/<spec-stem>/` instead of the default
`test_output/integration_test/`.

The positional argument may also be a **directory**: every `*.yaml` / `*.yml`
job spec it contains (non-recursive, sorted order) is run in sequence, each
with its own per-spec output directory, followed by a PASS/FAIL summary. The
process exits non-zero if any spec fails:

```bash
python scripts/run_integration_test.py tests_deps
```

> Note: not every fixture/example spec is expected to pass end-to-end. Some
> (e.g. `tests_deps/sample_spec.yaml`, the `replacement_*` examples)
> intentionally trip the text–hole collision gate and report `[FAIL]`; the
> default single-spec flow (`test123_spec.yaml`) and the regression specs
> (`complex_test_job.yaml`, `rotation_demo_job.yaml`) must pass.

### Jobset Mode (`--jobset`)

A `jobset:` YAML (a batch of job files packed onto shared plates) runs the same
harness in batch mode:

```bash
python scripts/run_integration_test.py --jobset "examples/job_specs/job_sets/jobset example.yaml"
python scripts/run_integration_test.py --jobset examples/job_specs/job_sets  # every set in the directory
python scripts/run_integration_test.py --jobset                              # defaults to that directory
```

Directory mode recognises a set by its `jobset:` root, so `--jobset <dir>`
sweeps the sets in a folder and skips the plain job specs beside it. A set
passed **without** the flag is refused with the exact command to run - the
single-job path cannot resolve a `jobset:` root. Batch mode adds three phases
around the ones documented below
(see [Jobset Phase Sequence](#jobset-phase-sequence--2j--2j-b--5j)): the
cross-job gate with per-member preparation, the pool packing that names which
jobs share each sheet, and the batch report surface.

### `--no-plots`

`--no-plots` works in both modes and skips **every** PDF: the export's
simple-outline previews and the color-coded Phase 4 diagnostics. Toolpaths,
coordinate validation, the layout report and all reports are unaffected, and
the report's `plots` / `no_plots` options follow the switch. Use it for a
headless sweep, or on a machine where the matplotlib PDF backend aborts the
process (see the `SIGABRT` entry in
[Troubleshooting](#issue-the-process-dies-with-abort-trap-6-in-the-plot-phases)).

### Expected Output

The run ends with:

```
================================================================================
  INTEGRATION TEST COMPLETE
================================================================================

✓ Pipeline executed successfully
```

Exit code `0` on success, `1` on any failure (the traceback is logged and a
`✗ Error: ...` line is printed).

### Total Runtime

Roughly 2–10 seconds for the default spec (TTF text rendering dominates);
longer for `tests_deps/complex_test_job.yaml`, which renders many labels across
two plates.

## Test Fixtures

> **Fixture location convention.** Every file a unit test reads lives under
> `tests_deps/` (adjacent to `tests/`), never under `examples/`. Treat
> `tests_deps/` as frozen: do not move, rename, or edit its contents, or the
> unit tests break. `examples/` holds user-facing sample specs and PLT files
> that are safe to tweak.

| File | Purpose |
|------|---------|
| `tools.json` | Mock shop tool inventory + boundary/hole cutter size |
| `job-config.json` | Shop-level job defaults (top-layer cascade + default plate size); drives the required-when-unconfigured gate |
| `tests_deps/test123_spec.yaml` | Default job spec (reproduces `examples/test123.plt`) |
| `tests_deps/complex_test_job.yaml` | Stress-test spec exercising every pipeline feature |
| `tests_deps/sample_spec.yaml` | Minimal canonical spec used in docstring examples |
| `tests_deps/rotation_demo_job.yaml` | Tight 24×10 scrap sheet forcing 90° bin-packing rotation (pins `layout: rows`) |
| `tests_deps/columns_demo_job.yaml` | 16 3×1 labels on a 24×16 sheet packing one full-height column (`layout: columns`) |
| `tests_deps/*.plt` | Identity/profiler/simplifier example PLTs (e.g. `SFA3X611sheet1.plt`) |
| `examples/job_specs/replacement_job.yaml` + `examples/job_specs/replacement_text_*.txt` | Replacement text file ("badge") template flow (example only; not a unit-test dep) |
| `test_output/test_ref_plot.png`, `test_output/test_ref_borders.png` | Manually kept reference plots for visual comparison (not generated by the runner) |

### `tools.json` — Mock Tool Inventory

```json
{
  "available_cutters": [0.01, 0.015, 0.02, 0.03, 0.045, 0.06, 0.09, 0.125, 0.25],
  "boundary_hole_cutter_size": 0.015
}
```

- `available_cutters` drives cutter selection: the ideal cutter from the
  internal map is snapped to inventory (narrower preferred when within the 3x
  tolerance).
- `boundary_hole_cutter_size` (0.015") is the cutter used for label boundaries
  and drill holes; it feeds the text–hole collision **stroke floor**
  (`0.5 × (hole_cutter + text_cutter)`), snapped to `available_cutters`
  (next size down, else next size up).

### `tests_deps/test123_spec.yaml` — Default Job Spec

Reproduces the reference file `examples/test123.plt`: three 3" × 1" labels
with 0.5" text ("Test 1", "Test 2", "Test 3"), packed on a single 24" × 16"
plate (constrained mode, edge-to-edge: `left_clearance: 0.0`,
`top_clearance: 0.0`).

### `tests_deps/complex_test_job.yaml` — Stress-Test Spec

Designed to exercise every layer of the pipeline in a single run:

- Constrained multi-plate packing (24×16 primary + 24×12 scrap sheet)
- Full inheritance cascade: TextLine → LabelSpec → JobSpec → defaults
- Cutter selection across the whole ideal-cutter map range
- Auto-sizing mixed with fixed dimensions
- Job-level hole inheritance, label-level overrides, `holes: []` suppression,
  location-only holes, and the `corners` / `sides` group shorthands
- `hole_margin` cascade and `min_hole_margin` collision-avoidance floor
- Horizontal text compression (`max_h_compress`) and alignment
  (`text_h_alignment`)
- Stroke-color toolpath splitting (`text_color`): same-cutter lines tagged
  black / magenta / implicit none export as separate per-color PLTs
- Replacement text file templates mixed with static labels
- High-count volume labels forcing overflow onto the second plate

## Pipeline Phases

### Phase 1 — Test Data Preparation

Loads `tools.json` (cutter inventory + boundary/hole cutter size), locates
`job-config.json` (job defaults; the runner always passes it to `parse_yaml`, so
the required-when-unconfigured gate is active), and resolves the job spec path
(default `tests_deps/test123_spec.yaml`, or the CLI override).

```
Loaded cutter inventory: [0.01, 0.015, 0.02, 0.03, 0.045, 0.06, 0.09, 0.125, 0.25]
Loaded boundary/hole cutter size: 0.015
```

### Phase 2 — Resolution & Layout

Executes the first half of the generate pipeline:

1. **Parse**: `parse_yaml()` → validated `JobSpec` (with `job-config.json`
   defaults injected at the job/plate layer), then
   `expand_job_spec()` flattens replacement-driven labels into static
   `LabelSpec`s (must run between parsing and resolution).
2. **Resolve**: `resolve_job_spec()` flattens the cascade into `ResolvedLabel`
   objects with cutter compensation
   (`toolpath_text_height = nominal_text_height − cutter_diameter`) and
   inventory snapping. Per-label results are printed: id, count, dimensions,
   margin, and per-line nominal height / cutter diameter / toolpath height.
3. **Bin pack**: `generate_layout()` packs every label instance onto physical
   plates (constrained when the spec defines `plates:`, otherwise unbounded
   auto-allocation of 24" × 16" sheets), honouring `allow_rotation` and the
   `layout` fill order (`columns` = the plate's height fills before extending
   rightward, so instance ids advance bottom-to-top up the engraving area;
   `rows` = the historical width-first order; plates may override per plate).
   Positions and `rotated` flags are printed per plate.

The job name is sanitized into a filesystem-safe `job_id` (same
`_sanitize_job_id()` used by the `generate` CLI: whitespace → `_`, everything
outside `[A-Za-z0-9._-]` stripped).

### Phase 3 — Vectorization & Per-Cutter Export

Uses the production export path, `export_per_cutter_plts()`:

- Renders each label independently with the matplotlib TTF text renderer and a
  custom lossless HPGL writer (text, border, and drill holes share one
  `plt_content`, so rotated labels turn 90° clockwise as a unit).
- Bin-packs labels onto plates using their **rendered** bounds.
- Assembles each plate in memory and splits the assembly **by cutter**:
  - borders + drill holes → one `[<material>_][p<plate>_]<cutter>_bh_<job_id>.plt`
    per plate
  - text → one `[<material>_][p<plate>_]<cutter>[_<color>]_txt_<job_id>.plt` per
    plate and cutter diameter
- The combined per-plate PLT stays in memory only (used for the combined PDF
  preview and optional color plots).
- Text–hole collisions that avoidance resolves (hole-margin reduction
  and/or horizontal compression) log WARNINGs and the job proceeds. Only
  collisions that survive every enabled avoidance phase are unacceptable
  output: the job-level gate (`assert_no_collisions()`, wired into
  `generate_layout_with_bounds`) logs an ERROR per offending label and
  aborts with `LabelRenderError` (exit 1).

Outputs land in `test_output/integration_test/`:

```
test_output/integration_test/
├── plt/
│   ├── 60_txt_Test_123_-_Single_Column.plt      # one per text cutter
│   └── 15_bh_Test_123_-_Single_Column.plt       # borders + holes together
└── pdf/
    ├── 60_txt_Test_123_-_Single_Column.pdf      # simple-outline previews
    ├── 15_bh_Test_123_-_Single_Column.pdf
    └── all_Test_123_-_Single_Column.pdf          # combined per plate
```

The run also writes the generation report (`report.json` + `summary.txt`) into
`test_output/integration_test/generation_report/` — see Phase 5.

File-name layout: `[<material>_][p<2-digit plate>_]<cutter>[_<color>]_<kind>_<job_id>.<ext>`
where `kind` is `txt` (text) or `bh` (borders+holes) and `cutter` is the
diameter in integer thousandths of an inch, unpadded (`0.060` -> `60`). The
material tag (sanitized, e.g. `wk_uv`) leads, so every sheet of one stock
sorts together, and appears only for material-declared output; the plate
number follows it and appears only when a material
spans more than one plate. Every name carries only ASCII letters,
digits and underscores (international characters transliterate, punctuation
collapses to a single underscore). PLT names are capped at 39 characters
including the extension (the plotter controller rejects longer names; the
stem is shortened from the right). With a spec override, the same tree appears
under `test_output/integration_test/<spec-stem>/`.

Note: Phase 3 runs the export with `optimize=False` — travel optimization of
the assembled plates is intentionally left to the separate `optimize` pipeline.

### Phase 3.5 — Coordinate Validation

Parses every exported PLT and asserts all `PA`/`PU`/`PD` coordinates are
non-negative, printing the X/Y ranges (plotter units and inches) per file:

```
✓ 15_bh_Test_123_-_Single_Column.plt:
    X range: [       0,    19304] (0.000, 19.000 inches)
    Y range: [       0,     2032] (0.000,  2.000 inches)
    Coordinates valid: all X≥0, all Y≥0 ✓
```

Coordinates are integer plotter units at the HPGL default step size of
0.025 mm = 1/1016 inch (see `plt_optimizer/core/units.py`), so a 19in-wide
plate spans 19304 units and a 2in-tall one spans 2032.

### Phase 4 — Visualization (Optional, opt-in)

The simple-outline PDF previews (one per per-cutter PLT plus one combined
`[<material>_][p<plate>_]all_<job_id>.pdf` per plate) are already written by Phase 3;
Phase 3 also tiles those whole-sheet proofs into the composite
`<output_dir>/<job_id>_proof.jpeg` proof sheet (one panel per plate — no flag gates
it, and `--no-plots` writes none because there is nothing to tile).
The combined preview is a colored proof: the page background is the configured
`preview_background_color` (default `#D3D3D3` grey), text ink in its resolved color
(`text_color` / `<tc:color>` pen tag, then the material core, then black) over the
material's background fill, with contrasting label/hole outlines and drill holes
punched in the page color (see `plt_optimizer/generate/preview_colors.py`); the
per-cutter PDFs stay black. Phase 4
adds the **color-coded** `*_default.pdf` diagnostic plots (cumulative-distance
toolpath with rapid-travel visualization) via `write_default_plots()`, one per
per-cutter PLT. The combined per-plate default plot (text + borders/holes
together) is deliberately **not** generated by this runner
(`include_combined=False`) -- the per-cutter default plots plus the simple
combined preview already cover both views.

These are strictly opt-in because they are slow to render; by default Phase 4
is a no-op:

```
ℹ Default (color-coded) plots disabled; set GENERATE_DEFAULT_PLOTS = True to enable.
```

Enable by flipping the module-level `GENERATE_DEFAULT_PLOTS = True` flag in
`scripts/run_integration_test.py` (equivalents elsewhere: `export_per_cutter_plts(...,
default_plots=True)` or the CLI's `--default-plots`).

### Phase 5 — Generation Report (`report.json` + `summary.txt`)

Writes the same report pair the `generate` CLI leaves next to every job, in the
same place — a sibling of `plt/` and `pdf/`:

```
test_output/integration_test/generation_report/
├── report.json    # machine-readable document (schema below)
└── summary.txt    # human-readable digest of the same data, echoed to stdout
```

`report.json` top-level keys, in document order:

| Key | Contents |
|-----|----------|
| `schema_version` | Integer document version (currently `1`) |
| `job` | `name`, `job_id` (sanitized, = the id in the file names), `status`, `error`, `spec_path`, `output_dir`, `report_dir`, `started_at`, `finished_at`, `duration_seconds`, `tools`, `options` |
| `summary` | `plates`, `labels`, `lines`, `labels_with_findings`, `compressed_lines`, `downsized_cutters`, `descender_reliefs`, `distinct_cutters`, `warnings`, `errors` |
| `timings` | Per-stage wall clock in seconds (`parse`, `resolve`, `layout`, the export's per-plate/per-layer stages) |
| `outputs` | `plt_files`, `pdf_files`, `default_pdf_files` (paths) and `plates` — per-plate geometry with every placed label (`label_id`, `x`, `y`, `width`, `height`, `rotated`) |
| `labels` | Per label: identity, role, material, plate placement, size, and its lines with the applied effects (`compression`, `line_spacing`, holes, relief, cutter downsize, …) |
| `warnings` / `errors` | The captured log records: `level`, `logger`, `message`, plus the best-effort `label_id` / `line_index` attribution |
| `usage` | Production-usage accounting (labels / characters / holes / material / tool distance / estimated run time) — see below; `null` on a failed run |

`usage` is built by `plt_optimizer/generate/usage_stats.py` (contract: its
module docstring) and is the only block whose numbers are **measured from the
emitted PLT files** rather than read off the plan. Its sections, each a
subtotal plus its own breakdown rows:

| Sub-block | Contents |
|---|---|
| `labels` | `instances` (physical labels, `count`-aware) and `specs`, broken down by `by_shape`, `by_relief` (plain vs relief), `by_material`, `by_size` (width x height x shape x material), `by_cutter` (one count per distinct diameter a label engraves with) |
| `characters` | Engraved glyph totals with **spaces excluded** and `<enumerate>`/`<itemize>` markers counted separately (`markers`), per `by_text_height` (one band per `(nominal_text_height, text_height)` pair, so compression, `<scale:N>` and bold partners each land in their own band; `summary.txt` prints the ordered size first and the machine's size in parentheses - `0.500 (0.425) x15` - dropping the parenthetical when the two agree) and `by_label` |
| `holes` | `total` per drill hole, by `by_diameter` (requested plus the cutter-compensated `engraved_diameter`) and `by_label` |
| `material` | `total` plate area (`plates`, `sq_in`, `sq_ft`, clearance frame included) plus `by_material`; and two label-area measures: `label_nominal_area` (spec size x count) and `label_bbox_area` (the plate-frame bounding box the packer reserved, rotation-aware) |
| `distance` | Tool travel in inches, `available`/`note` first: `total` (`cutting` + `rapid` = `total`, each with `*_ft`), `by_movement`, `by_class` (`text` / `relief` / `borders` / `holes` / `unattributed`), `by_cutter` (diameter + layer kind), `by_material`, `by_plate`, `by_label` (placement id + spec + plate + material), `files` (every measured file) and `unattributed` |
| `time` | **Estimated** run time in seconds, `available`/`note` first: the same measured travel divided by the machine parameters those files' headers were emitted with. `model` (the exact parameters, per layer), `total` (plus `total_hms`, `strokes`, `files`), `by_operation` (`cutting` / `rapid` / `plunge` / `z_up` / `dwell`), then `by_cutter`, `by_material`, `by_plate`, `by_class`, `by_label` (per placement) and `by_spec` (per job: one label summed over its copies, plates and cutters), each row carrying the operation split, the `total` and its `share` |

Every `distance` counter reconciles with `distance.total`: the breakdowns sum to
the measured total, with strokes outside every label slot reported under
`unattributed` so nothing is dropped. Because distance is measured from what the
machine will actually run, the numbers include sweep/merge rewrites — they
intentionally differ from any pre-write estimate. `time` reconciles the same way
and adds `time.total == sum(time.by_operation)`; it is an **estimate**, not a
measurement of the plotter, and it excludes tool changes, setup and loading.
When no `cutting_velocity` is configured (or a layer's rate is unresolvable) the
whole block reports `available: false` with a `note` and no numbers, so an
unsupported estimate is never mistaken for a zero. The rapid and Z terms read
`rapid_velocity` from `job-config.json` (falling back to the layer's
`cutting_velocity`). `summary.txt` renders the same blocks as its "Job usage:"
section (2-decimal figures); see
`plt_optimizer/generate/AGENTS.md` for the rule on extending it when new job or
label types appear.

`job.tools` is the cutter inventory the run used — `available_cutters`,
`boundary_hole_cutter_size`, and the two shop kerf buffers
(`cutter_buffer_text` / `cutter_buffer_routing`). `job.options` describes the
run's switches and carries `"source": "scripts/run_integration_test.py"`, which
is the only field that distinguishes an integration run's report from a CLI's.

Both come from the shared builder in `plt_optimizer/generate/generation_report.py`,
so the format can never diverge from a real `generate` run. Because the runner
calls the pipeline in-process, it feeds the builder the same two things the CLI
attaches:

- a `ReportCapture` logging handler on the pipeline logger — the `warnings` /
  `errors` arrays are built from the WARNING/ERROR records the pipeline logs
  (the INFO stage-timing records are diverted out, so they never count as
  warnings)
- a `StageTimings` collector — the parse / resolve / layout seconds, which the
  INFO records alone would not survive (the pipeline reconfigures the logger)

and the per-line effects come from the render cache Phase 3 exposes on
`PerCutterExport.rendered_labels`.

**A failed run leaves a report too.** The `except` branch writes
`job.status: "failed"` with `job.error: {type, message, stage}` naming the phase
that aborted (e.g. `export`), the warnings logged before the failure, and
whatever the export had already written. When parsing itself failed there is no
job to read the name from, so the spec stem stands in for both `name` and
`job_id`, and `summary.plates` is `0`.

## Jobset Phase Sequence — 2J / 2J-B / 5J

Batch mode (`--jobset`) keeps Phases 1, 3, 3.5, 3.6 and 4 exactly as documented
above - it feeds them the pooled labels and plates of every member - and adds
three phases of its own. Each one calls the same production helper the
`generate --jobset` CLI calls, so the harness grows no packing, naming, or
accounting logic of its own and the two paths cannot drift.

### Phase 2J — Jobset Plan (cross-job gate + per-job preparation)

`plt_optimizer.generate.jobset.build_jobset_plan` resolves the entries, runs the
cross-job gate, and prepares every member through the single-job preparation
path (`job_prep.prepare_job`): per-job cutter-driven compression, per-job
geometry, per-job plate pinning - all preserved. The console prints the gate's
verdict (every rule that fired), each member's plate/label geometry with its
pinned sheets, and the merged plate pool.

### Phase 2J-B — Pool Bin Packing (all jobs together)

`generate_layout` over the pooled labels, which is the number the feature
exists for: how many sheets carry more than one job. Every placement is printed
with the job that owns it, so a shared sheet is visible before a single byte is
written.

### Phase 3 (batch) — one export over the pool

`export_per_cutter_plts` receives two extra arguments it uses only in batch
mode: `layer_specs` (the `job_key -> member JobSpec` map, so each per-cutter
header carries the options of the job that owns those strokes) and `provenance`
(the label/plate ownership index, which drives the `jobset.json` stamp and the
job-coloured plate-proof overlays). A shared sheet is emitted as **one toolpath
segment per tool** - one `bh` file per (boundary/hole cutter, kerf width,
`tool_options` signature) and one `txt` file per (pen, signature) - so members
that run the same tool with the same engraver parameters are engraved in one
pass, named for the set (`kw_15_bh_Evening_Batch.plt`). A layer a job owns alone
keeps that job's name (`kw_60_txt_Job_B.plt`), and a layer splits per job only
where `tool_options` disagree (the run warns). Cutting geometry is unchanged by
sharing, and every file records all of its owning jobs, so a member still bills
exactly as it would have run alone.

### Phase 5J — Jobset Reports (per-job + batch)

Three surfaces, from the CLI's own builders:

| Artifact | Content |
| --- | --- |
| `<output>/<job_id>/generation_report/report.json` + `summary.txt` | one per member, measured from the files that carry its strokes (its own segments on a shared sheet, never the whole sheet) |
| `<output>/generation_report/jobset.json` | the batch manifest: the plate map (sheet → id, material, jobs, files), the gate's warnings, per-job usage, and every file written |
| `<output>/generation_report/report.json` + `summary.txt` | the batch run's own report, carrying the `jobset` block |

The console adds the plate map and the per-job distance / time totals, plus the
cross-job line (the strokes in shared text layers on mixed plates, and
`distance_saved` - the per-label sum minus the measured total, i.e. the travel
sharing reclaims, `0.000` when the packer never crossed a job boundary). The
per-job totals sum to `usage.distance.by_job`, which equals the batch's measured
`distance.total`: a stroke is charged to exactly one owner, so sharing never shows
up as a billing gap.

### Generated Files (batch mode)

```text
test_output/integration_test/<set-stem>/
├── plt/
│   ├── kw_15_bh_Evening_Batch.plt  every job's borders + holes: one tool, one run
│   ├── kw_45_txt_Evening_Batch.plt shared layers are named for the set
│   ├── kw_60_txt_Job_B.plt         a layer one job owns alone keeps its own name
│   └── ...                         (splits per job where tool_options disagree)
├── pdf/                           all_<set>.pdf per plate, job-coloured, with a
│                                  name-per-job key box on mixed sheets; a mixed
│                                  sheet also gets a per-job proof, a solo sheet
│                                  gets only its whole-sheet picture
├── Job_A/generation_report/       report.json + summary.txt for member A
├── Job_B/generation_report/       report.json + summary.txt for member B
└── generation_report/
    ├── report.json                the batch run, with the jobset block
    ├── summary.txt
    └── jobset.json                the batch manifest
```

The cutter token is the diameter in unpadded integer thousandths of an inch
(`45` = 0.045"), the material tag (`kw`) leads and appears only when the plate
declares a material, and the plate number follows it — marked with a `p` (`kw_p01`,
so it cannot be read as a second number beside the cutter) — only when the
material spans several sheets.

## Verification Checklist

After a run:

### Console Output
- [ ] No Python errors or exceptions
- [ ] Phases 1 → 3.5 complete; Phase 4 reports disabled (or generates `*_default.pdf`)
- [ ] Phase 5 prints the `Report:` / `Summary:` paths and the
      `Generation report: <job name>` digest
- [ ] Every exported PLT prints "Coordinates valid: all X≥0, all Y≥0 ✓"
- [ ] Final line: `✓ Pipeline executed successfully` (exit code 0)

### Generated Files (`test_output/integration_test/`)
- [ ] `plt/` contains one `[<material>_][p<plate>_]<cutter>_bh_<job>.plt` per plate
      and one `[<material>_][p<plate>_]<cutter>[_<color>]_txt_<job>.plt` per
      plate/cutter diameter (colored text layers gain a 1-letter suffix, e.g.
      `40_k_txt_`)
- [ ] `pdf/` contains a matching simple-outline PDF per PLT plus one
      `[<material>_][p<plate>_]all_<job>.pdf` combined preview per plate
- [ ] the run root contains `<job_id>_proof.jpeg`, the composite proof sheet
      tiling those whole-sheet proofs (one panel per plate; `--no-plots` writes
      none — there is nothing to tile)
- [ ] `generation_report/` contains `report.json` and `summary.txt`, and the
      summary's cutter list / plate count / output counts match the files
      actually written (a failed run writes the pair too, with
      `job.status: "failed"` and the failing phase in `job.error.stage`)
- [ ] `tests/test_run_integration_report.py` stays green — it pins this
      contract (schema parity with the CLI, the failure report, the capture)
- [ ] No text bleeds over label boundaries; drill holes sit at their specified
      locations with the resolved `hole_margin`

### Visual Comparison
- [ ] Compare the simple-outline PDFs against the reference plots
      (`test_output/test_ref_plot.png`, `test_output/test_ref_borders.png`) —
      use only the simple-mode plots for accurate comparison
- [ ] For `complex_test_job.yaml`: labels overflow onto plate 2, compressed
      lines respect margins, and replacement-expanded labels appear with their
      `_0000`-style ids' content

### Cutter Compensation
- [ ] `toolpath_text_height = nominal − effective_cutter` for every printed
      line, where `effective_cutter` is the matched tool **plus** the line's
      `cutter_buffer_text` kerf buffer (a roughly 45°-flank V-bit engraves
      wider than nominal; the buffer is 0.005" in the shipped
      `job-config.json`, and the code fallback when configured nowhere is
      0.0 = the nominal diameter)
- [ ] Unavailable ideal cutters snap per the 3x tolerance rule (narrower
      preferred when `narrower_gap ≤ 3 × wider_gap`), on the **nominal**
      diameter — the buffer never changes which tool is selected
- [ ] The per-cutter PLT file names quote the **physical** tool (a 0.030"
      cutter writes `30_txt_*.plt`) even with a non-zero buffer, while the
      `all_*.pdf` / per-cutter PDF previews draw the wider engraved stroke

### Job-Config Defaults
- [ ] Labels that omit `hole_margin` / `max_h_compress` / `min_hole_margin` /
      `hole_text_collision_distance` resolve to the `job-config.json` values
      (0.0625 / 0.5 / 0.05 / 0.1 in the shipped config), not the in-code
      fallbacks
- [ ] Labels/lines that omit `cutter_buffer_text` / `cutter_buffer_routing`
      inherit them down the job → plate (parity) → label → line cascade, and
      an explicit `0.0` at any tier opts that line/label out (engraved =
      nominal)
- [ ] Spec-declared values still win over the config (e.g. `test123_spec.yaml`
      keeps its `margin: 0.1`)
- [ ] Removing a required default from the config (and not declaring it in the
      spec) aborts with `JobConfigError`

## Troubleshooting

### Issue: the process dies with `Abort trap: 6` in the plot phases

Symptom: the run stops mid-way (exit code 134, no Python traceback) right after
`Parsing PLT file: ...`, inside Phase 3.5/4 where the matplotlib PDF backend
renders previews. It is a native abort in that backend, not a pipeline fault -
it reproduces for the default single-job run and a jobset run at the identical
line, and the process dies before the reports are written.

Fix: run with `--no-plots`, which skips every PDF and leaves the toolpaths,
coordinate validation, layout report and all report surfaces intact:

```bash
python scripts/run_integration_test.py --jobset --no-plots
```

A non-writable matplotlib config cache (`~/.matplotlib`) causes the same class
of trouble in the font manager; point `MPLCONFIGDIR` at a writable directory to
silence it.

### Issue: `LayoutFitError`
**Cause:** Labels do not fit the spec's plates. In unbounded mode the message is
orientation-aware ("in either orientation") — a label that fails 24×16 upright
may still fit sideways when `allow_rotation` is on (the default).
**Solution:** Reduce counts / dimensions, add plates, or keep rotation enabled.
`tests_deps/rotation_demo_job.yaml` intentionally aborts this way when rotation
is disabled.

### Issue: `LabelRenderError` (collision abort)
**Cause:** Rendered text comes closer to a drill hole than the stroke-aware
threshold `0.5 × (hole_cutter + text_cutter) + hole_text_collision_distance`
(0.1" with the shipped `job-config.json`; the in-code fallback is 0.15"). Both
cutters are the **engraved** widths — the nominal tool plus its
`cutter_buffer_routing` / `cutter_buffer_text` kerf buffer — so a job that
previously cleared can start aborting when the buffers are raised.
Per-label ERRORs name the label id, offending text line, hole, and measured gap.
**Solution:** Revise the jobspec — e.g. increase `hole_margin` /
`min_hole_margin`, reduce `hole_text_collision_distance`, move holes, or enable
`max_h_compress`. Avoidance (margin sweeps / collision compression) repairing
a collision does **not** excuse it; the job still aborts.

### Issue: `SubstitutionError`
**Cause:** Replacement text file missing/unreadable, or the delimiter is
invalid (must be a single non-alphanumeric, non-newline character).
**Solution:** Fix the file path (relative to the job YAML's directory) or pick
a delimiter absent from the data.

### Issue: `JobConfigError` (job defaults)
**Cause:** The job spec omits a required field that `job-config.json` also fails
to define (`max_h_compress`, `hole_margin`, `min_hole_margin`,
`hole_text_collision_distance`, or — for plate-less jobs — `plate_width` /
`plate_height`), or the config JSON is malformed / contains unknown keys.
**Solution:** Declare the field in the job spec, add the default to
`job-config.json`, or fix the config syntax (typos in key names are rejected on
purpose).

### Issue: Cutter selection differs from expectations
**Cause:** `tools.json` inventory mismatch.
**Solution:** The 3x-tolerance checks assume the shipped
`available_cutters` list; restore it before trusting cutter-related output.

### Issue: `ImportError` for matplotlib / font rendering failures
**Cause:** The generate pipeline needs matplotlib (Python 3.9+).
**Solution:** `uv sync --extra plotting`. (Windows 7 / Python 3.8 cannot run
the generate pipeline — the 3.8 constraint applies to `watch`/`optimize` only.)

## Running Related Checks

```bash
# Unit + integration-style pytest suite (100% coverage target on core logic)
uv run pytest tests/ -v

# The generate CLI itself (same pipeline as this runner, Phase 2-3 only)
uv run plt-optimizer generate tests_deps/test123_spec.yaml -o out/ --no-plots

# Optimization pipeline on an exported PLT
uv run plt-optimizer optimize out/plt/60_txt_Test_123_-_Single_Column.plt
```

## Integration Points Verified

| Component | What the runner proves |
|-----------|------------------------|
| `schema.parse_yaml()` | Pydantic validation, mutual-exclusion rules, group-hole expansion |
| `job_config.load_job_config()` / `apply_job_config_defaults()` | Top-layer default injection, plate-size defaults, required-when-unconfigured gate |
| `substitution.expand_job_spec()` | Badge/multiples flattening before resolution |
| `resolution.resolve_job_spec()` | Cascade, cutter compensation/snapping, compression & alignment resolution |
| `layout.generate_layout[_with_bounds]()` | Multi-heuristic packing, constrained/unbounded modes, rotation, collision gate |
| `label_renderer` / `ftext_renderer` | TTF glyph vectorization, native HPGL arcs for holes, margin/compression/alignment application |
| `vectorize.export_per_cutter_plts()` | Per-cutter splitting, file naming, PDF previews, in-memory combined content |
| `diagnostics.plotter` | Simple-outline + color-coded PDF rendering |
