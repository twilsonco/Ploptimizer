---
name: plates-layout-and-packing
description: 'Pack labels onto material plates: layout, rotation, clearances. Use when: packing many labels onto stock; declaring plates and materials; pinning labels to one plate; choosing rows vs columns layout; getting a fully saturated first plate. Keywords: plates, layout, packing, rotation, rotation_preference, plate_fill, material, material_weight_per_square_inch, clearance, plate_id, order, text_chunk_mode.'
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
- getting a fully saturated first plate

## Fields

| Field | Type | Default | Constraints | Tiers | Description |
|---|---|---|---|---|---|
| `material` | str \| null | null (unset) | — | job / label | Stock material name (free-form, e.g. 'wk' or 'wk(uv)'); cascades job -> label. Labels sharing a material pack together and a plate never mixes materials. Comparison trims whitespace and ignores case; unset (`null`) labels pack with the job-level material. |
| `order` | int \| null | null (unset) | — | job | Placement rank on the plate, honoured by the simple grid-fill modes ('columns-simple' / 'rows-simple') only: the lowest value is placed first (the head of the first plate) and the rest follow ascending, so a job decides which label sits at the head of the sheet and the reading/emission order of every label after it. Labels that omit it keep declaration order and place after every declared rank; equal ranks keep declaration order; all instances of a `count` / `replacement_text_file` label keep their own expansion order. Rejected on a job that bin packs ('columns' / 'rows'): that engine ranks layouts by used material, so a declared placement sequence would be silently discarded. Round labels place through the ellipse packer, which packs by adjacency, and ignore it. At the job level it is the single-label shorthand: accepted only when the job declares no `labels` list, where it ranks the one label the job produces (together with the other jobs of a jobset on a shared sheet); with a `labels` list it is rejected, because a rank belongs to each label, not to the job. |
| `plates` | list[PlateSpec] \| null | null (unset) | — | job | Plate (material sheet) definitions. Omit for unbounded mode: auto-allocated `default_plate_{i}` sheets (24x16 unless job-config `plate_width`/`plate_height` override). |
| `allow_rotation` | bool | `true` | — | job | Allow the bin packer to rotate labels 90 degrees (clockwise at assembly) for tighter layouts. Rotation also follows the `layout` fill order: at equal used material a label stands upright in 'columns' and lies along the width in 'rows'. Set false to keep every label horizontal. |
| `text_chunk_mode` | `line` \| `word` | `"line"` | — | job | Plate-space text optimization granularity: 'line' routes each text line as one node (default); 'word' routes each whitespace-delimited word separately. |
| `layout` | LayoutMode | `"columns"` | — | job | Preferential plate fill order: 'columns' (default) fills the plate height before extending rightward; 'rows' fills width before extending downward. With bin packing the fill order also picks the orientation: at equal used material a label stands upright in 'columns' and lies along the width in 'rows' (see `allow_rotation`). 'columns-simple'/'rows-simple' bypass bin packing with a flush grid fill in content order (no rotation; incompatible with an explicit `allow_rotation: true`). |
| `rotation_preference` | RotationPreference | `"fill_axis"` | — | job | Ranking preference between packings that fit: 'fill_axis' (default) follows the `layout` fill order, so rotation is a tool of the fill order (a label stands upright in 'columns' and lies along the width in 'rows'); 'minimize' takes the fewest rotated labels, so the job engraves upright whenever that costs no extra plate material. Both rank used material first, so rotation never buys material. Cascades job -> plate -> layout plan, and is inert on the simple grid layouts (they never rotate). |
| `plate_fill` | PlateFill | `"plate_major"` | — | job | Ranking preference between layouts that cost the same plate material: 'plate_major' (default) saturates the first plate, then the next, so the leading run of same-size labels fills a sheet completely and the job engraves whole sheets; 'global' ranks the whole layout at once (lift the least-filled plate, then minimize the total ink bounding box) -- the historical behaviour. Both rank plate material first, so a saturated first plate never costs an extra sheet. Under 'plate_major' the matching flush grid ('columns' -> 'columns-simple', 'rows' -> 'rows-simple') also competes as a layout candidate, which is what turns an exact grid-fit run into a 100 % plate. Cascades job -> plate -> layout plan. |
| `left_clearance` | float \| null | null (unset) | >=0 | job | Job-level default plate left-edge clearance in inches (cascades job -> plate; an explicit plate value, including 0.0, wins). |
| `top_clearance` | float \| null | null (unset) | >=0 | job | Job-level default plate top-edge clearance in inches (cascades job -> plate; an explicit plate value, including 0.0, wins). |
| `material_weight_per_square_inch` | float \| null | null (unset) | >=0 | job | Job-level default material weight per square inch in pounds (lb/in2), cascades job -> plate (an explicit plate value, including 0.0, wins) and is never label-level. Reports the estimated weight of engraved labels; unset means weight is not estimated. 0.00260417 is the shop's 16x24 sheet (approx. 1 lb). |
| `left_clearance` | float | `0.0` | >=0 | plate | Unused material width along the plate's left edge in inches (shifts the usable area rightward; must be >= 0). |
| `top_clearance` | float | `0.0` | >=0 | plate | Unused material height along the plate's top edge in inches (shifts the usable area downward; must be >= 0). |
| `layout` | LayoutMode \| null | null (unset) | — | plate | Per-plate fill-order override (None = inherit the job layout). Simple modes ('columns-simple'/'rows-simple') grid-fill this plate without bin packing and never rotate labels. |
| `rotation_preference` | RotationPreference \| null | null (unset) | — | plate | Per-plate rotation-ranking override (None = inherit the job value): 'fill_axis' lets the plate's fill order rotate labels, 'minimize' takes the fewest rotated labels at equal material. See the job-level `rotation_preference`. |
| `plate_fill` | PlateFill \| null | null (unset) | — | plate | Per-plate-group fill-ranking override (None = inherit the job value): 'plate_major' saturates the first plate at equal plate material, 'global' ranks the whole layout (historical). See the job-level `plate_fill`. One packing pass ranks by exactly one policy, so a pool of plates must agree. |
| `material` | str \| null | null (unset) | — | plate | Stock material this plate is cut from (free-form, e.g. 'wk' or 'wk(uv)'); cascades job -> plate (an explicit plate value wins). A plate carries exactly one material: labels whose material matches pack onto it and no other material shares it. A plate that omits material (`null`) is claimed by one material group. Comparison trims whitespace and ignores case. |
| `material_weight_per_square_inch` | float \| null | null (unset) | >=0 | plate | Material weight per square inch in pounds (lb/in2), used only to estimate the weight of the labels engraved on this plate; cascades job -> plate (an explicit plate value, including 0.0, wins) and is never label-level. 0.00260417 is the shop's 16x24 sheet (approx. 1 lb per sheet). Unset (`null`) everywhere means weight is not estimated (reported as null, never as 0). |
| `order` | int \| null | null (unset) | — | label | Placement rank of this label on the plate (see the job-level `order` for the full contract): honoured by the simple grid-fill modes ('columns-simple' / 'rows-simple') only, lowest value first, unset labels keeping declaration order after every declared rank, equal ranks keeping declaration order, and a label's own `count` / `replacement_text_file` instances staying together. Rejected on a job that bin packs ('columns' / 'rows'), which ranks layouts by used material and would discard it, and ignored for round labels, which place through the ellipse packer. |
| `plate_id` | str \| null | null (unset) | — | label | Id of the plate this label is pinned to (packs exclusively onto that plate). Set by plate-level replacement expansion. |

## Enums

### `LayoutMode`

Enumeration of plate fill-order preferences for bin packing.

`rectpack` has no sort option that controls fill order directly (the
emergent order is a side effect of placement tie-breaking), so
`columns` is realized by packing in a transposed frame with a
bottom-row fill algorithm and mapping every placement back into plate
space (see `plt_optimizer.generate.packing.layout`).

The `*-simple` modes bypass `rectpack` entirely: labels are placed
one at a time in strict content order on a flush grid, never rotated
(this mirrors the VisionPro "Multiples" / EngraveLab "Badges"
behavior). `columns-simple` fills each column top-to-bottom before
extending rightward; `rows-simple` fills each row left-to-right
before extending downward. A column (resp. row) spans the widest
(resp. tallest) label placed in it, fixed for its whole length, so a
1x3/1x4 mix starts the second column at x=4 for the entire column.
Round labels are exempt: they always keep the ellipse-packer layout
optimization regardless of mode.

| Value | Description |
|---|---|
| `rows` | Fill the plate row by row (rectpack). |
| `columns` | Fill the plate column by column (rectpack). |
| `rows-simple` | Grid-fill each row left-to-right, then down. |
| `columns-simple` | Grid-fill each column top-to-bottom, then right. |

### `RotationPreference`

Enumeration of what breaks the tie between packings that fit.

Every packing candidate is ranked by used plate material first, so a label
is never rotated to buy material. The preference decides the tie between
candidates that cost the same material:

| Value | Description |
|---|---|
| `fill_axis` |  |
| `minimize` | Take the fewest rotated labels, so a job engraves upright whenever that costs no extra material. The fill order still separates equally-rotated layouts, so plates stay unfragmented. |

### `PlateFill`

Enumeration of how a multi-plate layout is ranked once material ties.

Both policies rank total plate material first, so no layout is accepted for
spreading labels over more plates. What they disagree about is the tie
between layouts that cost the same material:

| Value | Description |
|---|---|
| `global` | Rank the layout as one whole -- least-filled plate lifted (max-min), then total ink bounding box. Fragments less material in total, and will accept a 99 % first plate with a 7-label tail when the tail lets the last plate pack tighter. |
| `plate_major` |  |

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
  name: "Plate demo"
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
- `rotation_preference: fill_axis` (default) lets a rotation that improves plate utilisation win; `minimize` treats rotation as a last resort, keeping labels upright wherever a plan without it matches the fill. Reads as engraved: every rotation is a hand setup change on the plotter.
- `plate_fill: plate_major` (default) saturates plate 1, then plate 2, among layouts that cost the same plate material, so a run of same-size labels fills whole sheets; `global` ranks the whole layout at once (the historical behaviour, and the way to reproduce an older plate map). Plate material is ranked first in both, so a full plate 1 never costs an extra sheet.
- `text_chunk_mode: word` routes each word as its own rapid-travel node (better travel, more chunk boundaries); `line` (default) is one node per line.
- `left_clearance` / `top_clearance` are per-plate keep-out strips (e.g. a clamped edge), not margins around each label.
- `order` decides which label is placed first, and it is honoured by the `*-simple` grid fills only: the lowest rank takes the head of plate 1, labels that omit it keep declaration order and place last, and a label's own `count` instances stay together. Bin packing (`columns` / `rows`) ranks layouts by used material and has no placement sequence, so declaring `order` there is rejected rather than ignored. It is also a rank, not an id: a jobset merges the ranks of every member's labels into one batch-wide sequence (ties fall back to merged declaration order), so a rank is a preference within the batch — unlike `plate_id`, which is namespaced per member and dropped by the jobset's pin normalization when its plate is absent.

## Read next

- `references/semantics.md` — the full semantics for this topic: mechanisms, thresholds, tier interactions and worked examples the tables above cannot express. Read it before authoring anything non-trivial in this area.
- `references/snippets.yaml` — more YAML fragments for this topic.
- [`../engraving-job-complete-reference/references/JOB_SPEC.md`](../engraving-job-complete-reference/references/JOB_SPEC.md) — exhaustive field/enum/default reference for the whole spec.
- [`../engraving-job-complete-reference/references/JOB_CONFIG.md`](../engraving-job-complete-reference/references/JOB_CONFIG.md) — exhaustive reference for the `job-config.json` shop-default fields.
- [`../engraving-job-complete-reference/references/job_spec.schema.json`](../engraving-job-complete-reference/references/job_spec.schema.json) — machine-readable schema for structural validation.
- Related skills: [replacement-text](../replacement-text/SKILL.md); [label-geometry](../label-geometry/SKILL.md); [cascade-and-config](../cascade-and-config/SKILL.md); [running-and-troubleshooting](../running-and-troubleshooting/SKILL.md).
