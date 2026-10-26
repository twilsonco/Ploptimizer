---
name: cutters
description: 'Auto cutter ladder, explicit cutter_size, per-cutter PLT files. Use when: choosing or overriding the engraving cutter; output splits into multiple PLT files; a cutter >= text height aborts the job. Keywords: cutter_size, cutter, tools.json, available_cutters, pen, per-cutter, ladder.'
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

## Fields

| Field | Type | Default | Constraints | Tiers | Description |
|---|---|---|---|---|---|
| `cutter_size` | float \| null | null (unset) | >0 | job / label / content entry / line / list block | Cutter diameter in inches to use for this text instead of the tool auto-selected from `text_height`. Snapped to the shop inventory (`tools.json` `available_cutters`: next size down, else next size up); the rendered toolpath height becomes `text_height - cutter_size`. Omit (`null`) to keep automatic cutter selection from `text_height`. Must be > 0 and below the line's `text_height`. No `job-config.json` counterpart. Cascades line -> label -> job. |
| `cutter_size` | float \| null | null (unset) | >0 | plate | Cutter diameter in inches overriding automatic selection (schema parity; not applied at plate level). |

## Defaults

Applied when unset at every tier (including `job-config.json`):

| Constant | Value | Note |
|---|---|---|
| `DEFAULT_CUTTER_LADDER_TOLERANCE` | `3.0` | 3.0 = the narrower/wider snap tolerance the ladder applies when a `<scale:N>` run's effective height `(N * text_height)` has no exact `tools.json` rung: it prefers the narrower tool unless that is more than tolerance x the distance to the wider tool away. The same tolerance the automatic line cutter uses; stored on the resolved line (with the inventory) as a ladder *input* so the per-run cutters re-derive identically in every pre-pass. Not a cascading schema field - the `<scale:N>` value rides the tag argument. |
| `DEFAULT_CUTTER_SIZE` | `None` | None = the text cutter is auto-selected from text_height (IDEAL_CUTTER_MAP + inventory snapping). An explicit cutter_size (line/label/job, no job-config tier) overrides the lookup: it snaps to the inventory (next size down, else up) and the toolpath height becomes `text_height - cutter_size`. |

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
- An explicit `cutter_size` snaps to the inventory (next size down, else up, WARNING when it moves) and the toolpath height becomes `text_height - cutter_size`, preserving the engraved cap height.
- A cutter at or above the line's `text_height` aborts the job (`CutterSizeError`) — reduce `cutter_size` or raise `text_height`.
- `cutter_size` is NOT a `job-config.json` key; set it in the spec (line, label, or job level).

## Read next

- `references/semantics.md` — the full semantics for this topic: mechanisms, thresholds, tier interactions and worked examples the tables above cannot express. Read it before authoring anything non-trivial in this area.
- `references/snippets.yaml` — more YAML fragments for this topic.
- [`../../../JOB_SPEC.md`](../../../JOB_SPEC.md) — exhaustive field/enum/default reference for the whole spec.
- [`../../../job_spec.schema.json`](../../../job_spec.schema.json) — machine-readable schema for structural validation.
- Related skills: [bold-multistroke](../bold-multistroke/SKILL.md); [compression-and-cutter-downsize](../compression-and-cutter-downsize/SKILL.md); [fonts-and-capline](../fonts-and-capline/SKILL.md); [tool-options](../tool-options/SKILL.md).
