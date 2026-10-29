---
name: cutters
description: 'Auto cutter ladder, explicit cutter_size, kerf buffers, per-cutter PLT files. Use when: choosing or overriding the engraving cutter; output splits into multiple PLT files; a cutter >= text height aborts the job; engraved strokes are wider than the PDF preview; tuning the kerf buffer for cut depth. Keywords: cutter_size, cutter, tools.json, available_cutters, pen, per-cutter, ladder, cutter_buffer_text, cutter_buffer_routing, cb, kerf, buffer, effective, stroke width.'
version: 1.0.0
metadata:
  hermes:
    category: engraving-jobs
    tags: [plt, engraving, toolpath, generate]
---

# Cutter Selection

## When to use

- choosing or overriding the engraving cutter
- output splits into multiple PLT files
- a cutter >= text height aborts the job
- engraved strokes are wider than the PDF preview
- tuning the kerf buffer for cut depth

## Fields

| Field | Type | Default | Constraints | Tiers | Description |
|---|---|---|---|---|---|
| `cutter_size` | float \| null | null (unset) | >0 | job / label / content entry / line / list block | Cutter diameter in inches to use for this text instead of the tool auto-selected from `text_height`. Snapped to the shop inventory (`tools.json` `available_cutters`: next size down, else next size up); the rendered toolpath height becomes `text_height - (cutter_size + cutter_buffer_text)`. Omit (`null`) to keep automatic cutter selection from `text_height`. Must be > 0 and below the line's `text_height` (the buffer included). No `job-config.json` counterpart. Cascades line -> label -> job. |
| `cutter_buffer_text` | float \| null | null (unset) | >=0 | job / label / content entry / line / list block | Kerf buffer in inches ADDED to a text line's cutter diameter to get the width it actually engraves. A V-bit with roughly 45-degree flanks cuts wider than its nominal diameter (the deeper the cut, the wider the stroke), so the nominal tool under-reports the engraved stroke. The effective width drives every geometry that is a width of material removed - glyph kerning clearance, the character-spacing fallback, the text/hole collision stroke floor, the compression margin, the descender relief and the `all_*.pdf` proof stroke widths - and the toolpath height compensates (`text_height - effective`) so the engraved text stays exactly `text_height` tall. The TOOL itself stays physical: pen map, per-cutter PLT file names, inventory snapping, the `<b>` bold rung and the compression-driven downsize ladder all use the nominal diameter. The inline `<cb:D>...</cb>` markup tag replaces this value for the wrapped characters (per character; `0.0` explicitly zeroes the buffer for the span) - a pure width modifier that widens whatever tool the run already engraves with, so unlike `<c:D>` it snaps to no inventory, adds no pen and writes no extra PLT file. There is no inline form of `cutter_buffer_routing` (hole geometry is a per-label property). Cascades line -> label -> job (fallback 0.0 = legacy behaviour; `job-config.json` ships the shop value). |
| `cutter_buffer_routing` | float \| null | null (unset) | >=0 | job / label / content entry / line / list block | Kerf buffer in inches ADDED to the boundary/drill-hole cutter diameter (`tools.json` `boundary_hole_cutter_size`) to get the width it actually engraves - the structural sibling of `cutter_buffer_text`, same 45-degree-flank rationale. The effective width drives the cutter-compensated drill-hole radius, the hole-margin edge offset, the text/hole collision stroke floor, the round-label packing kerf, the descender relief against the border and the `all_*.pdf` proof stroke widths for the border/hole pens. The TOOL stays physical (the `*_bh_*.plt` file name keeps the nominal tool size). Cascades line -> label -> job (the label tier is authoritative - hole geometry is per label; fallback 0.0 = legacy behaviour; `job-config.json` ships the shop value). |
| `cutter_buffer_text` | float \| null | null (unset) | >=0 | plate | Text kerf buffer in inches (schema parity; not applied at plate level -- text lines resolve their cutter before packing). |
| `cutter_buffer_routing` | float \| null | null (unset) | >=0 | plate | Boundary/hole kerf buffer in inches (schema parity; not applied at plate level -- hole geometry is a per-label property). |
| `cutter_size` | float \| null | null (unset) | >0 | plate | Cutter diameter in inches overriding automatic selection (schema parity; not applied at plate level). |

## Defaults

Applied when unset at every tier (including `job-config.json`):

| Constant | Value | Note |
|---|---|---|
| `DEFAULT_CUTTER_BUFFER_ROUTING` | `0.0` | 0.0 = the boundary/hole cutter engraves exactly its nominal diameter (the legacy behaviour and the code fallback). The structural sibling of DEFAULT_CUTTER_BUFFER_TEXT: added to the physical boundary/hole tool to get the engraved width used by the cutter-compensated hole radius, the hole-margin offset, the collision stroke floor, the round-label packing kerf and the PDF proof strokes. job-config.json supplies the shop default; there is no inline markup override for it (hole geometry is per label). |
| `DEFAULT_CUTTER_BUFFER_TEXT` | `0.0` | 0.0 = a text cutter engraves exactly its nominal diameter (the legacy behaviour, and the code fallback so an unconfigured job is bit-identical). Added to the physical diameter to get the width it engraves: a roughly 45-degree-flank V-bit cuts wider than nominal. The effective width drives the toolpath height, kerning clearance, collision floors, compression margins and the PDF proof strokes; the tool, the pen map and the PLT file names stay physical. job-config.json supplies the shop default; the inline `<cb:D>` markup tag overrides it per character. |
| `DEFAULT_CUTTER_LADDER_TOLERANCE` | `3.0` | 3.0 = the narrower/wider snap tolerance the ladder applies when a `<scale:N>` run's effective height `(N * text_height)` has no exact `tools.json` rung: it prefers the narrower tool unless that is more than tolerance x the distance to the wider tool away. The same tolerance the automatic line cutter uses; stored on the resolved line (with the inventory) as a ladder *input* so the per-run cutters re-derive identically in every pre-pass. Not a cascading schema field - the `<scale:N>` value rides the tag argument. |
| `DEFAULT_CUTTER_SIZE` | `None` | None = the text cutter is auto-selected from text_height (IDEAL_CUTTER_MAP + inventory snapping). An explicit cutter_size (line/label/job, no job-config tier) overrides the lookup: it snaps to the inventory (next size down, else up) and the toolpath height becomes `text_height - (cutter_size + cutter_buffer_text)` (the buffer applies to an explicit tool exactly like an automatic one). |

## Example

Force a 0.040in cutter on one line only.

```yaml
job:
  job_name: "Cutter demo"
  content:
    - text: "Fine detail"
      cutter_size: 0.040
    - text: "Auto cutter"
```

## Pitfalls

- Default `cutter_size: null` auto-selects from `text_height` via the ladder + `tools.json` inventory; each distinct cutter becomes its own per-cutter PLT file (one tool load per file on the engraver).
- An explicit `cutter_size` snaps to the inventory (next size down, else up, WARNING when it moves) and the toolpath height becomes `text_height - (cutter_size + cutter_buffer_text)`, preserving the engraved cap height.
- A cutter whose engraved width is at or above the line's `text_height` aborts the job (`CutterSizeError`) — reduce `cutter_size` or raise `text_height`.
- `cutter_size` is NOT a `job-config.json` key; set it in the spec (line, label, or job level). The two `cutter_buffer_*` fields ARE job-config keys.
- `cutter_buffer_text` / `cutter_buffer_routing` are kerf buffers in inches ADDED to a cutter diameter to get the width it engraves (a roughly 45-degree-flank V-bit cuts wider than nominal, the deeper the cut the wider the stroke). They cascade line -> label -> job; code fallback 0.0 = engraved = nominal, and `job-config.json` ships the shop value.
- The buffer produces the EFFECTIVE width, used for every ink measurement (kerning clearance, text/hole collision floor, hole cutter compensation, compression margin, packing kerf) and for the `all_*.pdf` proof stroke widths. The TOOL stays physical: pen map, per-cutter PLT file names, inventory snapping, the `<b>` bold rung and the compression-driven downsize ladder all use the nominal diameter, so a 0.030in cutter still writes `30_txt_*.plt`.
- Inline `<cb:D>` overrides `cutter_buffer_text` for the wrapped characters only (per-character kerf buffer). It names no tool: unlike `<c:D>` it snaps to no inventory, adds no pen and writes no extra PLT file — it widens whatever tool the run already engraves with. `cutter_buffer_routing` has no inline form.
- Set a buffer to `0.0` to reproduce the pre-buffer output byte-for-byte (useful when bisecting an unexpected geometry change).

## Read next

- `references/semantics.md` — the full semantics for this topic: mechanisms, thresholds, tier interactions and worked examples the tables above cannot express. Read it before authoring anything non-trivial in this area.
- `references/snippets.yaml` — more YAML fragments for this topic.
- [`../engraving-job-complete-reference/references/JOB_SPEC.md`](../engraving-job-complete-reference/references/JOB_SPEC.md) — exhaustive field/enum/default reference for the whole spec.
- [`../engraving-job-complete-reference/references/job_spec.schema.json`](../engraving-job-complete-reference/references/job_spec.schema.json) — machine-readable schema for structural validation.
- Related skills: [bold-multistroke](../bold-multistroke/SKILL.md); [compression-and-cutter-downsize](../compression-and-cutter-downsize/SKILL.md); [fonts-and-capline](../fonts-and-capline/SKILL.md); [holes-and-collision](../holes-and-collision/SKILL.md); [tool-options](../tool-options/SKILL.md).
