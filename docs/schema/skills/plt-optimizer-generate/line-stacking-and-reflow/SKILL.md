---
name: line-stacking-and-reflow
description: 'Stack lines evenly and reflow long text to fit the label width. Use when: gaps between lines look uneven; a line is too long for the label; wrapping one long sentence across lines. Keywords: line_spacing, baseline, optimize_line_content, reflow, new_line, wrapping.'
version: 1.0.0
metadata:
  hermes:
    category: plt-optimizer-generate
    tags: [plt, engraving, toolpath, generate]
---

# Line Spacing, Baseline Stacking and Reflow

## When to use

- gaps between lines look uneven
- a line is too long for the label
- wrapping one long sentence across lines

## Fields

| Field | Type | Default | Constraints | Tiers | Description |
|---|---|---|---|---|---|
| `line_spacing` | float \| str \| null | null (unset) | — | job / label / content entry / line / list block | Extra spacing between text lines in inches, or `auto` to calculate spacing automatically based on label height, margins, and text line count (default when unspecified). Fallback when not specified at any level is `auto`. |
| `optimize_line_content` | bool \| null | null (unset) | — | job / label / content entry / line / list block | Redistribute this line's words across the consecutive lines that also enable the option: whole words move between the enabled lines of the group (word order is never changed, only the line breaks) so the group's lines reach equal rendered width, minimizing the horizontal compression the label would otherwise need. Consecutive enabled lines form one independent group; a disabled line breaks the group. Runs as an export pre-pass before the cutter-reduction and shared-compression pre-passes. Cascades line -> label -> job (fallback false); explicit true is honored. |
| `optimize_line_content_max_lines` | int \| null | null (unset) | >=1 | job / label / content entry / line / list block | Ceiling N on how many consecutive lines an `optimize_line_content` group may use. This option can only ever ADD lines to a label; it never removes, blanks or collapses one. Unset reflows across the group's existing lines and adds none. When set, the reflow starts at the group's existing line count G and grows the group (inserting lines cloned from the group's typography) until its lines need no horizontal compression, M reaches N, or M reaches the group's word count. N <= G leaves the cap inert. Setting it on any one line of a group applies it to the whole group; two different values in one group are an error, and so is specifying it while `optimize_line_content` is not enabled. Cascades line -> label -> job (unset by default = no growth). |
| `use_baseline_spacing` | bool \| null | null (unset) | — | job / label | Stack a label's text lines by baseline-to-baseline pitch (`text_height` + `line_spacing`) so descenders hang into the gap and every gap reads as one uniform visual spacing; false stacks by rendered ink box (historical). Cascades job -> plate -> label (never text lines); unset (`null`) falls back to `job-config.json` then true. Accepted on plates for schema parity only. |
| `auto_line_spacing_interline_to_top_bottom_ratio` | float \| null | null (unset) | >0 | job | Ratio controlling inter-line spacing relative to top/bottom margins when auto line spacing is enabled. Default 1.0 makes all gaps equal. Values > 1.0 increase inter-line spacing at the expense of top/bottom margins. |
| `optimize_line_content` | bool \| null | null (unset) | — | plate | Word-level line-content reflow permission (schema parity; not applied at plate level). |
| `optimize_line_content_max_lines` | int \| null | null (unset) | >=1 | plate | Ceiling on the number of consecutive lines a reflow group may use (schema parity; not applied at plate level). |
| `use_baseline_spacing` | bool \| null | null (unset) | — | plate | Baseline-to-baseline line spacing (schema parity; not applied at plate level -- labels render before packing). |
| `new_line` | bool | `false` | — | content entry / line | Force this line to begin on its own line. When `True`, the line starts a new `optimize_line_content` reflow group: the lines that follow it reflow with it rather than with the lines above it, so the group's words fill the lines from this one downward. The flag has no effect unless this line also enables `optimize_line_content`. Line-level only: it is not inherited from the label or the job, and `job-config.json` has no counterpart. |

## Defaults

Applied when unset at every tier (including `job-config.json`):

| Constant | Value | Note |
|---|---|---|
| `DEFAULT_LINE_SPACING` | `'auto'` | Extra line spacing when unset at line/label/job level. |
| `DEFAULT_OPTIMIZE_LINE_CONTENT` | `False` | `False` = every text line keeps its authored words. `True` = consecutive enabled lines exchange whole words (word order kept, only the line breaks move) so the group's natural rendered widths equalize, minimizing the compression the label needs. A disabled line breaks the group; runs of one line, blank lines and groups with fewer words than lines are left alone. Runs as an export pre-pass before the cutter-reduction and shared-compression pre-passes. Cascades line/label/job; job-config supplies the shop default. |
| `DEFAULT_OPTIMIZE_LINE_CONTENT_MAX_LINES` | `None` | Ceiling on the consecutive lines one reflow group may use. `None` (the default) is *unset*: the group reflows across its existing lines and never gains one. The option is growth-only -- a numeric value may ADD lines to a label (the group starts at its existing line count and grows while its lines still need horizontal compression) and never removes any. Consumed by :mod:`plt_optimizer.generate.line_content`. |
| `DEFAULT_USE_BASELINE_SPACING` | `True` | `True` = a label's text lines stack by baseline pitch (cap height + `line_spacing`), so descenders hang into the gap below their line and every gap reads as one uniform visual spacing; auto `line_spacing` and the margin clamp measure each line's real descender extent. `False` = lines stack by rendered ink box (historical), where a descender inflates the gap under its own line. Cascades label/job (accepted on plates for schema parity only, never text lines); job-config supplies the shop default. |

## Example

Reflow a long sentence to the label width, max 3 lines.

```yaml
job:
  job_name: "Reflow demo"
  width: 4.0
  optimize_line_content: true
  optimize_line_content_max_lines: 3
  content:
    - text: "A long sentence that must wrap to fit the label width."
```

## Pitfalls

- `use_baseline_spacing` (default true) sets every gap to cap height + `line_spacing` so descenders hang INTO the gap; `false` restores ink-box stacking, where one descender-heavy line inflates its own gap.
- `optimize_line_content` reflows words into as many lines as needed — it GROWS the line count, so cap it with `optimize_line_content_max_lines` when the vertical budget is fixed.
- `new_line` starts a new reflow group; it is inert unless the line also enables `optimize_line_content`, and never enables reflow by itself.
- Spacing cascades label -> job -> config only (never text lines) — spacing is a property of the stacked block, not of one line.

## Read next

- `references/semantics.md` — the full semantics for this topic: mechanisms, thresholds, tier interactions and worked examples the tables above cannot express. Read it before authoring anything non-trivial in this area.
- `references/snippets.yaml` — more YAML fragments for this topic.
- [`../../../JOB_SPEC.md`](../../../JOB_SPEC.md) — exhaustive field/enum/default reference for the whole spec.
- [`../../../job_spec.schema.json`](../../../job_spec.schema.json) — machine-readable schema for structural validation.
- Related skills: [centering-and-descender-relief](../centering-and-descender-relief/SKILL.md); [label-geometry](../label-geometry/SKILL.md); [lists](../lists/SKILL.md).
