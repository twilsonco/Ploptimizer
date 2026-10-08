# Job Spec Reference for AI Assistants

Hand-written companion to the generated artifacts in this directory. Read
this file **plus** [`JOB_SPEC.md`](JOB_SPEC.md) (field tables) to author or
review job YAML; use [`job_spec.schema.json`](job_spec.schema.json) for
machine validation.

| Artifact | Source of truth | Regenerate? |
|---|---|---|
| `job_spec.schema.json` | `JobSpec` model (`plt_optimizer/generate/schema.py`) | `uv run python docs/schema/generate_ai_docs.py` |
| `job_config.schema.json` | `JobDefaults` model (`plt_optimizer/generate/job_config.py`) | same command |
| `JOB_SPEC.md` | both models + fallback constants scraped from `resolution.py` / `layout.py` + tool_options from `job-config.json` | same command |
| `README.md` (this file) | hand-written semantics | manual |

Drift is pinned by `tests/test_job_spec_docs.py`: if `schema.py` /
`job_config.py` change without regenerating the artifacts, the test suite
fails. The tool_options table in `JOB_SPEC.md` is auto-generated from the
live `job-config.json`, so it always reflects the current available engraver
parameters. All units are **inches**.

## Pipeline context

```
spec.yaml ──parse_yaml()──▶ JobSpec ──expand_job_spec()──▶ JobSpec ──resolve_job_spec()──▶ ResolvedLabel[] ──▶ layout / render / export
              (+ job-config.json injection & required-field gate)   (replacement-file expansion)   (cascade resolution, cutter sizing)
```

CLI entry: `plt-optimizer generate <spec.yaml>` (flags: `--job-config`,
`--tools`, `--fast-mode`, `--no-plots`, ...). Validation entry point for
tooling: `plt_optimizer.generate.schema.parse_yaml(path)` → validated
`JobSpec` or `ValueError` (`JobConfigError` subclass for config-gate
violations).

## Tool Options (HPGL Header Commands)

The `tool_options` field lets you specify engraver parameters (cutting
velocity, spindle speed, dwell time, etc.) as HPGL header commands prepended
to generated PLT files. **See the "Available Tool Options" section in
`JOB_SPEC.md`** for the full table of parameters, their HPGL commands, units,
ranges, and defaults.

Typical usage:

```yaml
job:
  job_name: "Custom Speeds"
  tool_options:
    cutting_velocity: 1.5      # override default 0.8 in/sec
    spindle_speed: 15000       # override default 12000 rpm
  # ... rest of job spec ...
```

Defaults come from `job-config.json` with **dual values** for text vs
borders/holes layers (each layer may need different settings). Out-of-bounds
values are automatically clamped to their configured range with a WARNING
logged. See `plt_optimizer/generate/tool_options.py` for the clamping logic.

## The five job forms (exactly one label source)

A valid job defines **exactly one** label source. Mixing forms is a
validation error.

### 1. Explicit `labels` list

```yaml
job:
  job_name: "Batch 01"
  text_height: 0.5
  labels:
    - id: label_1
      count: 10
      content:
        - text: "Line 1"
        - text: "Line 2"
```

### 2. Root-level single label (`content` + optional `count`)

```yaml
job:
  job_name: "Simple Labels"
  count: 20
  content:
    - text: "Single repeating label"
```

### 3. Label-level replacement file (per-label template)

Each line of the file produces one label copy (`badge_0000`, `badge_0001`,
...). `count` must not be set; `content` is an optional per-line attribute
template (see "Replacement file semantics" below).

```yaml
job:
  job_name: "Batch 02"
  labels:
    - id: badge
      replacement_text_file: data.txt      # relative to the YAML's directory
      replacement_text_delimiter: ";"      # optional, default ";"
      content:
        - text: "PLACEHOLDER L1"           # declares line-1 attributes only
          text_height: 0.45
```

### 4. Job-level replacement file (replaces `labels` entirely)

Job-level `width`, `height`, `text_height` are **required** (the synthesized
labels carry no label-level dimensions). `labels` and `count` are rejected;
root `content` is the shared attribute template. Ids: `label_0000`, ...

```yaml
job:
  job_name: "Batch 03"
  width: 3.0
  height: 1.0
  text_height: 0.75
  holes:
    - location: sides
  replacement_text_file: data.txt
```

### 5. Plate-level replacement files (labels pinned to one plate)

Each plate's file lines synthesize labels **pinned to that plate**
(`<plate_id>_0000`, ...); pinned labels pack exclusively onto their plate,
which then accepts no other labels. Job-level `width`/`height`/`text_height`
are required whenever any plate declares a file. Mutually exclusive with a
job-level file. Static `labels` may coexist and pack normally onto
unpinned plates.

```yaml
job:
  job_name: "Batch 04"
  width: 3.0
  height: 1.0
  text_height: 0.75
  plates:
    - id: scrap_a
      width: 24.0
      height: 16.0
      replacement_text_file: data_a.txt
    - id: full_b
      width: 24.0
      height: 16.0
  labels:
    - id: header
      content:
        - text: "HEADER"
```

A minimal real-world example (job-level file, form 4):
[`examples/job_specs/2026-7-22 sunwest.yaml`](../../examples/job_specs/2026-7-22%20sunwest.yaml).

## Replacement file semantics (forms 3–5)

- File format is **pure data**: every line is one label copy; items within a
  line split on `replacement_text_delimiter` (single special/whitespace char,
  default `;`; must not appear in the data). CRLF/CR normalized; one trailing
  newline does not create a phantom copy; UTF-8 BOM stripped; items used
  verbatim (no whitespace stripping). Blank lines render blank (WARNING).
- **Fewer items than template lines** → the copy renders only as many lines
  as items (block stays vertically centered). **More items** → extra lines
  inherit label-level attributes.
- Without `content`, every rendered line inherits label-level attributes.
- Expansion (`expand_job_spec`) runs between `parse_yaml` and
  `resolve_job_spec`, flattening each file line into a static
  `LabelSpec(count=1)` with a `_NNNN` id suffix.

## Cascade rules (how `null` resolves)

`null`/omitted always means "inherit"; an explicit value (including `0.0`)
always wins at its level. Precedence per attribute:

| Attribute | Cascade order | Fallback |
|---|---|---|
| `text_height`, `character_spacing`, `line_spacing`, `max_h_compress`, `text_h_alignment` | line → label → job → config | see fallback table in `JOB_SPEC.md` |
| `cutter_size` | line → label → job (no config tier) | `null` = auto-select from `text_height` |
| `cutter_downsize`, `max_cutter_downsizes`, `cutter_downsize_global` | line → label → job → config | `true` / `1` / `true` |
| `h_compress_global` | line → label → job → config | `false` |
| `use_baseline_spacing` | label → job → config (never lines; plate parity) | `true` |
| `text_color` | line → label (never job/plate/config) | `none` (implicit) |
| `width`, `height` | label → job → auto-size from rendered content | — |
| `margin` | label → job → config | 0.125 |
| `hole_margin` | label → job → config | 0.1875 |
| `min_hole_margin`, `hole_text_collision_distance` | label → job → config | see fallback table |
| `holes` | label (replaces job list entirely; `[]` suppresses) → job → config | none |
| `layout`, `allow_rotation`, `text_chunk_mode` | job → config | `columns` / `true` / `line` |
| `layout` (per plate) | plate → job → config | — |
| `material` | label → job; plate → job | `null` (unset) |
| `left_clearance`, `top_clearance` | plate → job → config | 0.0 |
| plate `width`/`height` | plate → config (`plate_width`/`plate_height`) | 24×16 (unbounded auto-allocation only) |

`job-config.json` sits **below the YAML but above the fallbacks**: its values
are injected at the job layer before validation, so labels still override
them. See [`job_config.schema.json`](job_config.schema.json) for its
contract (unknown keys fail loudly; `hole_diameter` fills `diameter` on hole
entries that omit it).

## Behavioural semantics a schema cannot express

- **Hole groups**: `location: corners` expands in place to the four corner
  holes; `sides` to left+right — at validation time, so downstream only ever
  sees the 8 atomic locations.
- **Auto-sizing**: omitting `width`/`height` sizes the label from rendered
  text + margin.
- **Plates**: `width`/`height` are the *usable* pack area;
  `left_clearance`/`top_clearance` shift it right/down within the material
  (origin = material top-left, +y down). No plates → unbounded mode:
  auto-allocated `default_plate_{i}` sheets (config `plate_width/height`,
  else 24×16), overflowing onto as many sheets as needed.
- **`layout: columns` (default)** fills each plate's height before extending
  rightward (scrap-friendly single rectangular block); `rows` is the
  historical width-first fill. Per-plate override splits packing into
  sequential same-mode groups.
- **`allow_rotation` (default true)**: labels rotate 90° CW only when it
  strictly improves used area; ties keep all-horizontal.
- **`material` (default null)**: free-form stock name grouping labels into
  independent packing passes — labels sharing a material pack together and
  a plate carries exactly one material (matching is trim + casefold; the
  first-declared spelling wins for display). Material-less plates spread
  across the material groups; material-less jobs pack in one pass exactly
  as before. Unbounded mode allocates one auto-bin pool per material
  (`<material>_default_plate_{i}`). Not a `job-config.json` key.
- **Per-plate typographic fields** (`hole_margin`, `max_h_compress`,
  `text_h_alignment`, `min_hole_margin`, `hole_text_collision_distance`,
  `cutter_size`, `cutter_downsize`, `max_cutter_downsizes`,
  `cutter_downsize_global`, `h_compress_global`, `use_baseline_spacing`) are
  accepted on plates for schema parity but **not applied** at plate level —
  labels render once before packing.
- **`cutter_size` (default null)**: an explicit cutter diameter (inches)
  overriding the automatic height→cutter lookup. The requested diameter is
  snapped to `tools.json` `available_cutters` (next size down, else next up,
  WARNING-logged when it moves); the nominal `text_height` is kept as the
  user's intent and the rendered toolpath height becomes
  `text_height - cutter_size` (so vertical fit math is unaffected). A cutter
  at or above the line's `text_height` aborts the job (`CutterSizeError`,
  non-zero exit). Each distinct cutter — explicit or auto — becomes its own
  per-cutter text layer/PLT file. Omitting it keeps automatic selection
  (current behaviour). Not a `job-config.json` key.
- **`cutter_downsize` (default true) + `max_cutter_downsizes` (default 1)**:
  compression-driven cutter reduction. When a text line's *effective*
  horizontal scale (margin × collision compression) falls below the midpoint
  between the current automatic cutter and the next smaller
  `tools.json` `available_cutters` rung — e.g. 0.100in current, 0.080in next:
  scale < 0.90 downsizes to 0.080in — the line's cutter drops one rung and
  the toolpath height grows to `text_height - smaller_cutter` (nominal
  `text_height` kept). At most `max_cutter_downsizes` rungs per line
  (`0` = disabled); each step re-measures the re-rendered line, and the
  ladder is strictly one-way (a line never regains a larger cutter). Requires
  a cutter inventory (no `tools.json` → no reduction) and a compression
  budget (`max_h_compress > 0`); an explicit `cutter_size` is never reduced.
  The reduced cutter flows into the pen map, so the downsized line lands in
  its own per-cutter PLT file; the change is WARNING-logged at render time
  and reported in the layout report (`cutter 0.060in -> 0.045in`).
  `job-config.json` supplies the shop defaults.
- **`cutter_downsize_global` (default true)**: per-label sharing of
  compression-driven cutter downsizes. When a line's compression triggers a
  swap, every other *eligible* line of the same `text_height` **within the
  same label** receives the same swap (the midpoint trigger check is bypassed
  for receivers — a sibling's trigger is their trigger), then each receiver
  continues its own one-way step loop; the group converges to the smallest
  final cutter among its members, so one label engraves one text size with
  one tool. Lines with an explicit `cutter_size`, `cutter_downsize: false`,
  `max_cutter_downsizes: 0`, `max_h_compress: 0.0`, or
  `cutter_downsize_global: false` are never touched (in either direction:
  such a line neither shares its own downsize nor receives a sibling's).
  Sharing is always per-label — setting the option at the job level simply
  enables it for every label. WARNING-logged per propagated line and
  reported in the layout report like a trigger downsize. `job-config.json`
  supplies the shop default.
- **`h_compress_global` (default false)**: per-label sharing of horizontal
  compression. When a text line is horizontally compressed (margin overflow
  and/or hole collision), every other *eligible* line of the same
  `text_height` **within the same label** is compressed to the group's
  most-compressed (minimum) scale, clamped to each receiver's own
  `1 - max_h_compress` budget floor — so one label engraves one text size at
  one glyph density. A line whose natural compression is already tighter keeps
  it (never stretched back). Lines with `max_h_compress: 0.0` are never
  touched (no compression mechanism can fire on them), and
  `h_compress_global: false` opts a line out in both directions (it neither
  shares its own compression nor receives a sibling's). Sharing is always
  per-label — setting the option at the job level simply enables it for every
  label. Runs as an export pre-pass **after** the cutter reduction, so a
  shared compression can deepen a line's squeeze without re-triggering a
  cutter swap (accepted staleness). WARNING-logged per propagated line and
  reported in the layout report as an ordinary compressed line. `job-config.json`
  supplies the shop default.
- **`use_baseline_spacing` (default true)**: baseline-to-baseline line
  spacing. Stacking a label by rendered ink box means a line carrying
  descenders (`g j p q y`) pushes the next line down by its full ink height,
  inflating that one visual gap. With baseline spacing each line's baseline
  sits one pitch below the previous one (`pitch = cap height + line_spacing`),
  so the descender hangs *into* the gap and every gap on the label reads as
  one uniform visual spacing. Lines without descenders are bit-identical to
  ink-box stacking. Auto `line_spacing` and the margin clamp measure each
  line's real ascender/descender extents (render probe), so a descender-heavy
  block claims its space up front and fills the label exactly. `false`
  restores the historical ink-box stacking. Cascades label → job → config
  (never text lines — spacing is a property of the stacked block); plates are
  parity-only.
- **Text–hole collision avoidance** (3 phases): always-on detection →
  opt-in `min_hole_margin` sweep → opt-in `max_h_compress` compression.
  Unresolved collisions fail the job (`LabelRenderError`, non-zero exit).
  Threshold: `0.5 * (hole_cutter + text_cutter) + hole_text_collision_distance`.
- **Required-when-unconfigured gate**: with `--job-config` in play,
  `max_h_compress`, `hole_margin`, `min_hole_margin`,
  `hole_text_collision_distance` (and `plate_width`/`plate_height` when
  plates are undeclared) must come from the config **or** the spec, else
  `JobConfigError`. Without a config path, silent fallbacks apply.

## Validating YAML against these artifacts

Any JSON-Schema validator works after `yaml.safe_load` (note: the schema
covers structure/types/enums/constraints, **not** the cross-field rules
above — use `parse_yaml` for those):

```python
import json, yaml, jsonschema
from pathlib import Path

schema = json.loads(Path("docs/schema/job_spec.schema.json").read_text())
job = yaml.safe_load(Path("my_job.yaml").read_text())["job"]
jsonschema.validate(job, schema)          # structural check
# authoritative check:
from plt_optimizer.generate.schema import parse_yaml
# parse_yaml("my_job.yaml")               # + all cross-field validators
```

## Regenerating the artifacts

```bash
uv run python docs/schema/generate_ai_docs.py
```

Run it after any change to `schema.py`, `job_config.py`, or the fallback
constants in `resolution.py`/`layout.py`, and commit the output — CI fails
otherwise (`tests/test_job_spec_docs.py`).
