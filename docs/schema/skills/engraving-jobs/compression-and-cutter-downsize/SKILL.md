---
name: compression-and-cutter-downsize
description: 'Squeeze long text horizontally; swap to a finer cutter when squeezed. Use when: text overflows the label width; choosing the max_h_compress budget; one text size engraves at two cutters. Keywords: max_h_compress, compression, cutter_downsize, h_compress_global, squeeze, arc_preserve_compression.'
version: 1.0.0
metadata:
  hermes:
    category: engraving-jobs
    tags: [plt, engraving, toolpath, generate]
---

# Compression and Cutter Downsizing

## When to use

- text overflows the label width
- choosing the max_h_compress budget
- one text size engraves at two cutters

## Fields

| Field | Type | Default | Constraints | Tiers | Description |
|---|---|---|---|---|---|
| `max_h_compress` | float \| null | null (unset) | >=0 <=1 | job / label / content entry / line / list block | Maximum horizontal compression fraction in `[0.0, 1.0]`. |
| `cutter_downsize` | bool \| null | null (unset) | — | job / label / content entry / line / list block | Allow the automatically selected cutter to be reduced when the text line is horizontally compressed: a line squeezed past the midpoint toward the next smaller inventory tool swaps to it and re-renders at `text_height - smaller_cutter`. One-way (a downsized cutter is never enlarged again within the same line) and fully overridden by an explicit cutter_size. Requires a tools.json `available_cutters` inventory. Cascades line -> label -> job (fallback true); explicit false is honored. |
| `max_cutter_downsizes` | int \| null | null (unset) | >=0 | job / label / content entry / line / list block | Maximum number of successive cutter downsizings applied to one text line (0 disables the mechanism, 1 = at most one size down). Each step re-measures the line, since a smaller cutter renders wider. Cascades line -> label -> job (fallback 1); explicit 0 is honored. |
| `cutter_downsize_global` | bool \| null | null (unset) | — | job / label / content entry / line / list block | Share a compression-driven cutter downsize across the text lines of one label: when a line's compression triggers a swap to a smaller inventory tool, every other eligible line of the same `text_height` in the same label receives the same swap (then each continues its own one-way step loop). Lines with an explicit `cutter_size`, `cutter_downsize` false, `max_cutter_downsizes` 0, or a zero `max_h_compress` budget are never touched. Setting the option at the job level enables it for every label; sharing is always per-label. Cascades line -> label -> job (fallback true); explicit false is honored. |
| `cutter_downsize_global_job_wide` | bool \| null | null (unset) | — | job / label / content entry / line / list block | Widen the scope of the shared cutter downsize from the label to the whole job: when a line's compression triggers a swap, every eligible line of the same `text_height` in ANY label of the job receives the same swap (the job converges on one tool per text size). Requires `cutter_downsize_global` true; a line with this flag false keeps exactly the per-label behavior (it neither shares to nor receives from the job-wide group). The same eligibility opt-outs apply. Cascades line -> label -> job (fallback false); explicit true is honored. |
| `h_compress_global` | bool \| null | null (unset) | — | job / label / content entry / line / list block | Share horizontal compression across the text lines of one label: when a line's width forces horizontal compression, every other eligible line of the same `text_height` in the same label is compressed to the same scale (the most-compressed scale among the triggering lines, clamped to each line's own `max_h_compress` budget floor). Lines with a zero `max_h_compress` budget are never touched. Setting the option at the job level enables it for every label; sharing is always per-label. Cascades line -> label -> job (fallback false); explicit true is honored. |
| `h_compress_global_same_height_only` | bool \| null | null (unset) | — | job / label / content entry / line / list block | The grouping scope of the shared horizontal compression: true (the default) shares the compression only between the lines of the same `text_height` in the label (one text size, one glyph density); false shares it across EVERY eligible line of the label, whatever their `text_height`, so one label engraves at one glyph density throughout. Cascades line -> label -> job (fallback true); explicit false is honored. |
| `arc_preserve_compression` | bool \| null | null (unset) | — | job / label / content entry / line / list block | Keep PLT-font glyphs arc-native through the non-uniform text transforms (horizontal compression, the cap-line clamp, the descender relief and the italic shear): each arc's ellipse image is refit with a short chain of circular AA arcs (bounded deviation, see `plt_optimizer/generate/arc_fit.py`) instead of polygonizing into line segments, so a compressed line engraves far faster. True (the default) preserves arcs; false restores the historical polygonized output. Cascades line -> label -> job (fallback true); explicit false is honored. |
| `max_h_compress` | float \| null | null (unset) | >=0 <=1 | plate | Maximum horizontal compression fraction in [0.0, 1.0]. |
| `cutter_downsize` | bool \| null | null (unset) | — | plate | Compression-driven cutter reduction permission (schema parity; not applied at plate level). |
| `max_cutter_downsizes` | int \| null | null (unset) | >=0 | plate | Maximum number of cutter downsizing steps (schema parity; not applied at plate level). |
| `cutter_downsize_global` | bool \| null | null (unset) | — | plate | Per-label sharing of compression-driven cutter downsizes (schema parity; not applied at plate level). |
| `cutter_downsize_global_job_wide` | bool \| null | null (unset) | — | plate | Job-wide scope of the shared compression-driven cutter downsizes (schema parity; not applied at plate level). |
| `h_compress_global` | bool \| null | null (unset) | — | plate | Per-label sharing of horizontal compression (schema parity; not applied at plate level). |
| `h_compress_global_same_height_only` | bool \| null | null (unset) | — | plate | Grouping scope of the shared horizontal compression (schema parity; not applied at plate level). |
| `arc_preserve_compression` | bool \| null | null (unset) | — | plate | Arc-preserving non-uniform text transforms (schema parity; not applied at plate level). |

## Defaults

Applied when unset at every tier (including `job-config.json`):

| Constant | Value | Note |
|---|---|---|
| `DEFAULT_ARC_PRESERVE_COMPRESSION` | `True` | True = the non-uniform PLT glyph transforms (horizontal compression, the cap-line clamp, the descender relief, the italic shear) keep native AA arcs: each arc's ellipse image is refit with a short chain of circular arcs (bounded deviation, plt_optimizer/generate/arc_fit.py), so a compressed line emits ~1.05 arcs per source arc instead of ~2.8 polygonized line segments and engraves faster. False restores the historical polygonized output. Cascades line/label/job; job-config supplies the shop default. |
| `DEFAULT_CUTTER_DOWNSIZE` | `True` | `True` = the automatic cutter may be reduced one rung per allowed step when a text line is horizontally compressed past the midpoint between the current cutter and the next smaller inventory cutter (requires `tools.json` inventory; an explicit `cutter_size` disables it). |
| `DEFAULT_CUTTER_DOWNSIZE_GLOBAL` | `True` | `True` = a compression-driven cutter downsize is shared across the label: every other eligible line of the same `text_height` in the same label receives the trigger's final cutter (then each continues its own one-way loop; the group converges to the smallest). Explicit `cutter_size` / `cutter_downsize` false / `max_cutter_downsizes` 0 / `max_h_compress` 0.0 lines are never touched. Cascades line/label/job; job-config supplies the shop default. |
| `DEFAULT_CUTTER_DOWNSIZE_GLOBAL_JOB_WIDE` | `False` | `False` = the shared cutter downsize stays per-label. True = the sharing group widens from the label to the whole job: every eligible line of the same `text_height` in ANY label receives the trigger's final cutter, so one text size engraves with one tool job-wide. Requires `cutter_downsize_global` `true`; a line with this flag false keeps exactly the per-label behavior. Cascades line/label/job; job-config supplies the shop default. |
| `DEFAULT_H_COMPRESS_GLOBAL` | `False` | `False` = horizontal compression stays per-line. `True` = a compressed line's scale is shared across the label: every other eligible line of the same `text_height` in the same label is compressed to the group's most-compressed (minimum) scale, clamped to each line's own 1 - `max_h_compress` budget floor. `max_h_compress` 0.0 lines are never touched. Cascades line/label/job; job-config supplies the shop default. |
| `DEFAULT_H_COMPRESS_GLOBAL_SAME_HEIGHT_ONLY` | `True` | `True` = the shared compression is scoped to the lines of the same `text_height` (one text size, one glyph density). `False` = the compression is shared across EVERY eligible line of the label, whatever its `text_height`, so one label engraves at one glyph density throughout. Cascades line/label/job; job-config supplies the shop default. |
| `DEFAULT_MAX_CUTTER_DOWNSIZES` | `1` | Maximum number of cutter downsizings per text line (0 = disabled). Cascades line/label/job; job-config supplies the shop default. |
| `DEFAULT_MAX_H_COMPRESS` | `0.0` | 0.0 = horizontal compression disabled. Required from config-or-spec when a job-config is in play. |

## Example

Allow up to 30% horizontal squeeze, shared per label.

```yaml
job:
  job_name: "Compression demo"
  max_h_compress: 0.3
  h_compress_global: true
  content:
    - text: "A VERY LONG MODEL NUMBER THAT NEEDS TO FIT"
```

## Pitfalls

- `max_h_compress: 0.0` disables compression entirely (and makes cutter downsize impossible); the squeeze floor is `1 - max_h_compress`.
- Compression fires on margin overflow and on hole collision; the result is a horizontal scale (glyphs get narrower, height unchanged).
- Cutter downsize is ONE-WAY: when a compressed line's effective scale crosses the midpoint to the next smaller inventory cutter, the line drops a rung (max `max_cutter_downsizes` rungs, 0 disables) and the toolpath grows to `text_height - smaller_cutter`. An explicit `cutter_size` is never reduced.
- `h_compress_global` / `cutter_downsize_global` share the squeeze/swap across same-height lines WITHIN a label (one size = one density = one tool); sharing is always per-label — setting it at job level just enables it for every label.
- Cutter reduction runs BEFORE the shared-compression pre-pass, so a shared compression can deepen a line's squeeze without re-triggering a cutter swap.

## Read next

- `references/semantics.md` — the full semantics for this topic: mechanisms, thresholds, tier interactions and worked examples the tables above cannot express. Read it before authoring anything non-trivial in this area.
- `references/snippets.yaml` — more YAML fragments for this topic.
- [`../../../JOB_SPEC.md`](../../../JOB_SPEC.md) — exhaustive field/enum/default reference for the whole spec.
- [`../../../job_spec.schema.json`](../../../job_spec.schema.json) — machine-readable schema for structural validation.
- Related skills: [cutters](../cutters/SKILL.md); [holes-and-collision](../holes-and-collision/SKILL.md); [line-stacking-and-reflow](../line-stacking-and-reflow/SKILL.md); [label-geometry](../label-geometry/SKILL.md).
