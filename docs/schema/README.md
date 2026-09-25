# Job Spec Reference for AI Assistants

Hand-written companion to the generated artifacts in this directory. Read
this file **plus** [`JOB_SPEC.md`](JOB_SPEC.md) (field tables) to author or
review job YAML; use [`job_spec.schema.json`](job_spec.schema.json) for
machine validation.

| Artifact | Source of truth | Regenerate? |
|---|---|---|
| `job_spec.schema.json` | `JobSpec` model (`plt_optimizer/generate/schema.py`) | `uv run python docs/schema/generate_ai_docs.py` |
| `job_config.schema.json` | `JobDefaults` model (`plt_optimizer/generate/job_config.py`) | same command |
| `JOB_SPEC.md` | both models + fallback constants scraped from `resolution.py` / `layout.py` | same command |
| `README.md` (this file) | hand-written semantics | manual |

Drift is pinned by `tests/test_job_spec_docs.py`: if `schema.py` /
`job_config.py` change without regenerating the artifacts, the test suite
fails. All units are **inches**.

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
| `width`, `height` | label → job → auto-size from rendered content | — |
| `margin` | label → job → config | 0.125 |
| `hole_margin` | label → job → config | 0.1875 |
| `min_hole_margin`, `hole_text_collision_distance` | label → job → config | see fallback table |
| `holes` | label (replaces job list entirely; `[]` suppresses) → job → config | none |
| `layout`, `allow_rotation`, `text_chunk_mode` | job → config | `columns` / `true` / `line` |
| `layout` (per plate) | plate → job → config | — |
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
- **Per-plate typographic fields** (`hole_margin`, `max_h_compress`,
  `text_h_alignment`, `min_hole_margin`, `hole_text_collision_distance`) are
  accepted on plates for schema parity but **not applied** at plate level —
  labels render once before packing.
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
