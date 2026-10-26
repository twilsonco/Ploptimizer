---
name: plates-layout-and-packing
description: 'Pack labels onto material plates: layout, rotation, clearances. Use when: packing many labels onto stock; declaring plates and materials; pinning labels to one plate; choosing rows vs columns layout. Keywords: plates, layout, packing, rotation, material, clearance, plate_id, text_chunk_mode.'
version: 1.0.0
metadata:
  hermes:
    category: engraving-jobs
    tags: [plt, engraving, toolpath, generate]
---

# Plates, Layout and Bin-Packing

## When to use

- packing many labels onto stock
- declaring plates and materials
- pinning labels to one plate
- choosing rows vs columns layout

## Fields

| Field | Type | Default | Constraints | Tiers | Description |
|---|---|---|---|---|---|
| `material` | str \| null | null (unset) | — | job / label | Stock material name (free-form, e.g. 'wb' or 'wb(uv)'); cascades job -> label. Labels sharing a material pack together and a plate never mixes materials. Comparison trims whitespace and ignores case; unset (`null`) labels pack with the job-level material. |
| `plates` | list[PlateSpec] \| null | null (unset) | — | job | Plate (material sheet) definitions. Omit for unbounded mode: auto-allocated `default_plate_{i}` sheets (24x16 unless job-config plate_width/plate_height override). |
| `allow_rotation` | bool | `true` | — | job | Allow the bin packer to rotate labels 90 degrees (clockwise at assembly) for tighter layouts. Set false to keep every label horizontal. |
| `text_chunk_mode` | `line` \| `word` | `"line"` | — | job | Plate-space text optimization granularity: 'line' routes each text line as one node (default); 'word' routes each whitespace-delimited word separately. |
| `layout` | LayoutMode | `"columns"` | — | job | Preferential plate fill order: 'columns' (default) fills the plate height before extending rightward; 'rows' fills width before extending downward. |
| `left_clearance` | float \| null | null (unset) | >=0 | job | Job-level default plate left-edge clearance in inches (cascades job -> plate; an explicit plate value, including 0.0, wins). |
| `top_clearance` | float \| null | null (unset) | >=0 | job | Job-level default plate top-edge clearance in inches (cascades job -> plate; an explicit plate value, including 0.0, wins). |
| `left_clearance` | float | `0.0` | >=0 | plate | Unused material width along the plate's left edge in inches (shifts the usable area rightward; must be >= 0). |
| `top_clearance` | float | `0.0` | >=0 | plate | Unused material height along the plate's top edge in inches (shifts the usable area downward; must be >= 0). |
| `layout` | LayoutMode \| null | null (unset) | — | plate | Per-plate fill-order override (None = inherit the job layout). |
| `material` | str \| null | null (unset) | — | plate | Stock material this plate is cut from (free-form, e.g. 'wb' or 'wb(uv)'); cascades job -> plate (an explicit plate value wins). A plate carries exactly one material: labels whose material matches pack onto it and no other material shares it. A plate that omits material (`null`) is claimed by one material group. Comparison trims whitespace and ignores case. |
| `plate_id` | str \| null | null (unset) | — | label | Id of the plate this label is pinned to (packs exclusively onto that plate). Set by plate-level replacement expansion. |

## Enums

### `LayoutMode`

Enumeration of plate fill-order preferences for bin packing.

`rectpack` has no sort option that controls fill order directly (the
emergent order is a side effect of placement tie-breaking), so
`columns` is realized by packing in a transposed frame with a
bottom-row fill algorithm and mapping every placement back into plate
space (see `plt_optimizer.generate.packing.layout`).

| Value | Description |
|---|---|
| `rows` | Fill the plate row by row. |
| `columns` | Fill the plate column by column. |

## Defaults

Applied when unset at every tier (including `job-config.json`):

| Constant | Value | Note |
|---|---|---|
| `DEFAULT_PLATE_HEIGHT` | `16.0` | Auto-allocated (unbounded mode) default plate height when job-config `plate_height` is unset. Required from config when a job-config is in play and plates are undeclared. |
| `DEFAULT_PLATE_WIDTH` | `24.0` | Auto-allocated (unbounded mode) default plate width when job-config `plate_width` is unset. Required from config when a job-config is in play and plates are undeclared. |

## Example

Pack onto two named plates, one with a clearance strip.

```yaml
job:
  job_name: "Plate demo"
  width: 3.0
  height: 1.0
  plates:
    - id: scrap_a
      width: 24.0
      height: 16.0
      left_clearance: 0.5
    - id: full_b
      width: 48.0
      height: 96.0
  labels:
    - id: pinned
      plate_id: scrap_a
      count: 5
      content:
        - text: "SCRAP RUN"
```

## Pitfalls

- With no `plates` declared, plates are auto-allocated at the configured size (default 24x16); declared plates are used exactly, so labels that do not fit fail the job.
- `plate_id` pins a label to one plate; a plate fed by a replacement file accepts ONLY its pinned labels.
- `layout: columns` (default) packs column-first, `rows` packs row-first; `allow_rotation: false` keeps labels upright and uses more plate space.
- `text_chunk_mode: word` routes each word as its own rapid-travel node (better travel, more chunk boundaries); `line` (default) is one node per line.
- `left_clearance` / `top_clearance` are per-plate keep-out strips (e.g. a clamped edge), not margins around each label.

## Read next

- `references/semantics.md` — the full semantics for this topic: mechanisms, thresholds, tier interactions and worked examples the tables above cannot express. Read it before authoring anything non-trivial in this area.
- `references/snippets.yaml` — more YAML fragments for this topic.
- [`../engraving-job-complete-reference/references/JOB_SPEC.md`](../engraving-job-complete-reference/references/JOB_SPEC.md) — exhaustive field/enum/default reference for the whole spec.
- [`../engraving-job-complete-reference/references/job_spec.schema.json`](../engraving-job-complete-reference/references/job_spec.schema.json) — machine-readable schema for structural validation.
- Related skills: [replacement-text](../replacement-text/SKILL.md); [label-geometry](../label-geometry/SKILL.md); [cascade-and-config](../cascade-and-config/SKILL.md); [running-and-troubleshooting](../running-and-troubleshooting/SKILL.md).
