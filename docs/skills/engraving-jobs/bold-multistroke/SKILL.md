---
name: bold-multistroke
description: 'Stroke-count PLT fonts, target boldness, and <b> behavior. Use when: engraving bold text on PLT fonts; tuning target_boldness_factor; passes leave unengraved stripes. Keywords: target_boldness_factor, multistroke_overlap_ratio, bold, stroke_count, bold_boldness_multiplier.'
version: 1.0.0
metadata:
  hermes:
    category: engraving-jobs
    tags: [plt, engraving, toolpath, generate]
---

# Multi-Stroke (Bold) Fonts

## When to use

- engraving bold text on PLT fonts
- tuning target_boldness_factor
- passes leave unengraved stripes

## Fields

| Field | Type | Default | Constraints | Tiers | Description |
|---|---|---|---|---|---|
| `target_boldness_factor` | float \| null | null (unset) | >=0.05 <=0.3 | job / label / content entry / line / list block | Target engraved stroke width as a fraction of `text_height` for multi-stroke (bold) PLT fonts - fonts whose glyphs carry `N > 1` parallel cutter passes (the `stroke_count` stored in the font's catalog file). The automatic cutter is sized so the `N` passes plus their overlaps engrave this total width, with multistroke_overlap_ratio guarding against unengraved stripes; the resulting cutter is snapped to the shop inventory. The implicit pass overlap is derived and logged. Inert on single-stroke fonts, TTF fonts, and lines with an explicit `cutter_size`. Bound to [0.05, 0.30]. Cascades line -> label -> job (fallback 0.14); accepted on plates for schema parity only. |
| `multistroke_overlap_ratio` | float \| null | null (unset) | >=0.1 <=0.5 | job / label / content entry / line / list block | Minimum overlap fraction between adjacent cutter passes of a multi-stroke (bold) PLT font: the automatic cutter is never smaller than `pass_spacing / (1 - this ratio)`, so adjacent engraved strokes always overlap by at least this fraction and no unengraved stripe can appear between passes (an overlap of 0.25 means the stepover stays within 75% of the cutter width). The actual overlap is derived from the chosen cutter and logged. Inert on single-stroke fonts, TTF fonts, and lines with an explicit cutter_size. Bound to [0.1, 0.5]. Cascades line -> label -> job (fallback 0.25); accepted on plates for schema parity only. |
| `bold_boldness_multiplier` | float \| null | null (unset) | >=1 <=4 | job / label / content entry / line / list block | Boldness boost applied by inline `<b>` markup to a multi-stroke (bold) PLT font: the bold runs re-run the cutter ladder at `target_boldness_factor` * this multiplier (clamped to 0.30 with a WARNING), so their parallel passes engrave heavier than the line's plain text. On single-stroke PLT and TTF fonts `<b>` instead steps to the next larger inventory cutter and this multiplier is inert. Bound to [1.0, 4.0]; 1.0 disables the boost. Cascades line -> label -> job (fallback 1.5); accepted on plates for schema parity only. |
| `target_boldness_factor` | float \| null | null (unset) | >=0.05 <=0.3 | plate | Multi-stroke target engraved stroke width as a fraction of text height (schema parity; not applied at plate level). |
| `multistroke_overlap_ratio` | float \| null | null (unset) | >=0.1 <=0.5 | plate | Multi-stroke minimum pass overlap ratio (schema parity; not applied at plate level). |
| `bold_boldness_multiplier` | float \| null | null (unset) | >=1 <=4 | plate | Bold markup (`<b>`) boldness multiplier for multi-stroke fonts (schema parity; not applied at plate level). |

## Defaults

Applied when unset at every tier (including `job-config.json`):

| Constant | Value | Note |
|---|---|---|
| `DEFAULT_BOLD_BOLDNESS_MULTIPLIER` | `1.5` | 1.5 = the multiplier inline `<b>` markup applies to target_boldness_factor on a multi-stroke (bold) PLT font (the bold runs re-run the cutter ladder at factor * multiplier, clamped to 0.30 with a WARNING). On single-stroke PLT and TTF fonts `<b>` instead steps to the next larger inventory cutter and this multiplier is inert. Cascades line/label/job; job-config supplies the shop default. |
| `DEFAULT_MULTISTROKE_OVERLAP_RATIO` | `0.25` | 0.25 = the minimum overlap fraction between adjacent cutter passes of a multi-stroke (bold) PLT font: the derived cutter is never smaller than `pass_spacing / (1 - ratio)`, so engraved passes always overlap and no unengraved stripe appears. The actual overlap is derived from the chosen cutter and logged. Inert on single-stroke fonts, TTF fonts, and lines with an explicit `cutter_size`. Cascades line/label/job; job-config supplies the shop default. |
| `DEFAULT_TARGET_BOLDNESS_FACTOR` | `0.14` | 0.14 = the target engraved stroke width (as a fraction of text height) for multi-stroke (bold) PLT fonts (`stroke_count` > 1 in the font's catalog file). The automatic cutter is sized so the N engraved passes total this width, then snapped to the inventory (stepping up when the snap-down breaks the gap-free floor). Inert on single-stroke fonts, TTF fonts, and lines with an explicit cutter_size. Cascades line/label/job; job-config supplies the shop default. |

## Example

Heavier engraved strokes for a bold PLT font.

```yaml
job:
  job_name: "Bold demo"
  font: "Claret4"
  target_boldness_factor: 0.18
  content:
    - text: "HEAVY PLATE"
```

## Pitfalls

- Boldness is cutter-driven on multi-stroke PLT fonts (catalog `stroke_count` > 1): the ladder sizes the cutter so the N passes total `target_boldness_factor` of the text height. Inert on single-stroke PLT and TTF fonts, and on lines with an explicit `cutter_size`.
- `multistroke_overlap_ratio` is the anti-stripe floor: adjacent passes always overlap by at least this fraction, so the derived cutter is never smaller than `pass_spacing / (1 - ratio)`.
- Inline `<b>` multiplies `target_boldness_factor` by `bold_boldness_multiplier` for the run (clamped, WARNING-logged); on single-stroke/TTF fonts it steps to the next larger inventory cutter and the multiplier is inert.

## Read next

- `references/semantics.md` — the full semantics for this topic: mechanisms, thresholds, tier interactions and worked examples the tables above cannot express. Read it before authoring anything non-trivial in this area.
- `references/snippets.yaml` — more YAML fragments for this topic.
- [`../engraving-job-complete-reference/references/JOB_SPEC.md`](../engraving-job-complete-reference/references/JOB_SPEC.md) — exhaustive field/enum/default reference for the whole spec.
- [`../engraving-job-complete-reference/references/job_spec.schema.json`](../engraving-job-complete-reference/references/job_spec.schema.json) — machine-readable schema for structural validation.
- Related skills: [cutters](../cutters/SKILL.md); [inline-markup](../inline-markup/SKILL.md); [fonts-and-capline](../fonts-and-capline/SKILL.md).
