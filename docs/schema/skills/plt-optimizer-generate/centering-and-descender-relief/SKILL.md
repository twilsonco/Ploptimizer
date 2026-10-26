---
name: centering-and-descender-relief
description: 'Horizontal/vertical centering and per-glyph descender clearance. Use when: text looks off-center; g/j/p/q/y collide with the line below; descenders hit the bottom border. Keywords: text_h_alignment, v_center_by_content, descender, auto_descending_character_adjustment, centering.'
version: 1.0.0
metadata:
  hermes:
    category: plt-optimizer-generate
    tags: [plt, engraving, toolpath, generate]
---

# Centering and Descending-Character Relief

## When to use

- text looks off-center
- g/j/p/q/y collide with the line below
- descenders hit the bottom border

## Fields

| Field | Type | Default | Constraints | Tiers | Description |
|---|---|---|---|---|---|
| `text_h_alignment` | TextHAlignment \| null | null (unset) | — | job / label / content entry / line / list block | Horizontal text alignment: `left`, `center`, or `right`. |
| `v_center_chars` | str \| null | null (unset) | — | job / label / content entry / line / list block | Character set auto-centred vertically between the baseline and the capline: every text character occurring in this string is raised or lowered (a pure vertical translate) so its own ink is centred on the cap-band midline (half the rendered cap height above the baseline) - the characters a font typically draws too high (hyphen, colon, en/em dash, '=', '<', '>'). The inline `<vc>...</vc>` markup tag applies the same centering to any other Empty string `''` disables the auto-apply (the `<vc>` tag keeps working on its own). Cascades line -> label -> job (code fallback `''` = auto-apply off; `job-config.json` ships the shop set '-:–—=<>'); an explicit `''` is honored. |
| `v_center_by_content` | bool \| null | null (unset) | — | job / label | Center a label's text block by its rendered ink (`true`, historical -- the deepest descender is charged to the block, so lines with descenders ride high) or by its baseline-to-capline band (false, the default -- the visible text is optically centered and descenders hang into the bottom margin for the descending-character relief to handle). Band centering is active only when `auto_descending_character_adjustment` is enabled; with the relief off, ink centering is kept and this field is inert. Cascades job -> plate -> label (never text lines); unset (`null`) falls back to `job-config.json` then `false`. Accepted on plates for schema parity only. |
| `auto_descending_character_adjustment` | bool \| null | null (unset) | — | job / label | Automatically downscale and raise a descending glyph that comes (close to) colliding with a glyph on the following line. Per-descending-character, triggered by a measured near-miss against the line below -- never a blanket adjustment. Cascades job -> plate -> label (never text lines); unset (`null`) falls back to `job-config.json` then `true`. Accepted on plates for schema parity only. |
| `descending_character_v_scale` | float \| null | null (unset) | >=0.2 <=1 | job / label | Maximum vertical downscale allowed for a triggering descending glyph in [0.2, 1.0] (1.0 = no downscaling, 0.5 = 50% downscaling), about the glyph's own baseline. Cascades job -> plate -> label (never text lines); unset (`null`) falls back to `job-config.json` then 0.5. Accepted on plates for schema parity only. |
| `descending_character_raise` | float \| null | null (unset) | >=0 <=1 | job / label | Maximum vertical raise allowed for a triggering descending glyph as a fraction of its own descender depth in [0.0, 1.0] (0.0 = no raising, 1.0 = lift until the glyph's lowest point sits exactly at the baseline). Cascades job -> plate -> label (never text lines); unset (`null`) falls back to `job-config.json` then 0.7. Accepted on plates for schema parity only. |
| `descending_character_clearance` | float \| null | null (unset) | >=0 | job / label | Extra air demanded between the engraved stroke of a descending glyph and the engraved stroke of a glyph on the following line, as a unitless factor of the stroke floor (required gap = floor * (1 + clearance)), so it scales with the cutter width and reads the same at every text size. Cascades job -> plate -> label (never text lines); explicit 0.0 is honored (only true touching triggers); unset (`null`) falls back to `job-config.json` then 0.0. Accepted on plates for schema parity only. |
| `descending_character_reciprocal_scale` | float \| null | null (unset) | >=0.2 <=1 | job / label | Maximum vertical downscale allowed for the would-be-colliding glyphs on the FOLLOWING line in [0.2, 1.0] (1.0 = no reciprocal downscaling, 0.5 = 50% downscaling). The lower glyphs are scaled top-down about their own lowest ink point, so their lowest point never moves; they share the relief with the descending glyph in proportion to the same ramp driving its downscale/raise. Cascades job -> plate -> label (never text lines); unset (`null`) falls back to `job-config.json` then 0.5. Accepted on plates for schema parity only. |
| `adjust_h_center_for_single_side_hole` | bool \| null | null (unset) | — | job / label | Permission to apply horizontal shift for asymmetric `left`/`right` drill holes. When enabled (true), text is horizontally shifted to center between `left` and `right` holes: shift = (left_diameter - right_diameter) / 2. Only applied to center-aligned text; `left`/`right` alignment ignores the shift. Vertical shifts (for `top`/`bottom` holes) are always applied and are not gated by this field. Cascades job -> plate -> label (never text lines); unset (`null`) falls back to `job-config.json` then `false`. Accepted on plates for schema parity only. |
| `text_h_alignment` | TextHAlignment \| null | null (unset) | — | plate | Horizontal text alignment (schema parity; not applied at plate level). |
| `v_center_chars` | str \| null | null (unset) | — | plate | Auto-vertical-centred character set (schema parity; not applied at plate level -- labels render before packing). |
| `v_center_by_content` | bool \| null | null (unset) | — | plate | Vertical centering mode: rendered ink (true) or baseline-to-capline band (false, the default). Schema parity; not applied at plate level -- labels render before packing. |
| `auto_descending_character_adjustment` | bool \| null | null (unset) | — | plate | Automatic descending-character relief permission (schema parity; not applied at plate level -- labels render before packing). |
| `descending_character_v_scale` | float \| null | null (unset) | >=0.2 <=1 | plate | Maximum vertical downscale for a triggering descending glyph in [0.2, 1.0] (schema parity; not applied at plate level). |
| `descending_character_raise` | float \| null | null (unset) | >=0 <=1 | plate | Maximum raise for a triggering descending glyph as a fraction of its descender depth in [0.0, 1.0] (schema parity; not applied at plate level). |
| `descending_character_clearance` | float \| null | null (unset) | >=0 | plate | Extra engraved-stroke air gap in inches for descending-character relief (schema parity; not applied at plate level). |
| `descending_character_reciprocal_scale` | float \| null | null (unset) | >=0.2 <=1 | plate | Maximum top-down downscale for the following line's colliding glyphs in [0.2, 1.0] (schema parity; not applied at plate level). |

## Enums

### `TextHAlignment`

Enumeration of valid horizontal text alignment modes for a text line.

The alignment anchors the rendered line within the label's inner content
area (label width minus both margins). `left` places the line's
left-most point precisely at the left margin; `right` places the
line's right-most point precisely at the right margin; `center`
(the default) centers the line horizontally.

| Value | Description |
|---|---|
| `left` | Align text to the left margin. |
| `center` | Center text horizontally. |
| `right` | Align text to the right margin. |

## Defaults

Applied when unset at every tier (including `job-config.json`):

| Constant | Value | Note |
|---|---|---|
| `DEFAULT_AUTO_DESCENDING_CHARACTER_ADJUSTMENT` | `True` | `True` = a descending glyph is vertically downscaled and raised when a glyph on the FOLLOWING line comes within the required clearance (the engraved-stroke floor `0.5*(cutter_upper+cutter_lower)` scaled by 1 + `descending_character_clearance`) of it. The relief is per triggering glyph (never a blanket adjustment) and runs at render time; when the allowances are exhausted the gap below the line grows, and a collision that neither the allowances nor the admissible spacing can clear aborts the job. False = descenders engrave exactly as rendered. Cascades label/job (accepted on plates for schema parity only, never text lines); job-config supplies the shop default. |
| `DEFAULT_DESCENDING_CHARACTER_CLEARANCE` | `0.0` | 0.0 = the relief triggers only when the two engraved strokes would touch (the stroke floor); a unitless factor of that floor, so 0.3 demands 30% extra air and the requirement scales with the cutter width (reads the same at every text size). Cascades label/job; job-config supplies the shop default. |
| `DEFAULT_DESCENDING_CHARACTER_RAISE` | `0.7` | 0.7 = a triggering descending glyph may be lifted by up to 70% of its own descender depth (1.0 lifts until its lowest point sits exactly at the baseline, 0.0 forbids raising). Cascades label/job; job-config supplies the shop default. |
| `DEFAULT_DESCENDING_CHARACTER_RECIPROCAL_SCALE` | `0.5` | Deepest top-down downscale permitted for the would-be-colliding glyphs on the FOLLOWING line (`1.0` = reciprocal downscaling forbidden, the historical behavior; `0.5` = squeezed to half height). The lower glyphs are scaled about their own lowest ink point, so their lowest point never moves; they share the relief with the descending glyph through the same ramp driving its downscale/raise. Bound to `[0.2, 1.0]` by the schema. |
| `DEFAULT_DESCENDING_CHARACTER_V_SCALE` | `0.5` | 0.5 = a triggering descending glyph may be vertically squeezed to 50% of its height about its own baseline (1.0 forbids downscaling, 0.2 is the deepest squeeze). Cascades label/job; job-config supplies the shop default. |
| `DEFAULT_TEXT_H_ALIGNMENT` | `'center'` | Horizontal alignment fallback when unset everywhere. |
| `DEFAULT_V_CENTER_BY_CONTENT` | `False` | Vertical centering mode for a label's text block. `False` (the default) centers the block by its **baseline-to-capline band** -- the band spans the lowest baseline to the highest capline, so the visible text is optically centered and descenders hang into the bottom margin, where the descending- character relief handles them against the bottom border. `True` centers by the block's **rendered ink** (the historical behavior): the deepest descender is charged to the block, so a line carrying descenders rides high in the label. Band centering is active only when the descending-character relief is enabled (see `center_text_by_band`); with the relief off nothing would adjust the descenders, so ink centering is kept. Label-level only -- centering is a property of the stacked block, never of one line, so the field is deliberately absent from `TextLine`/`TextAttributes`. |
| `DEFAULT_V_CENTER_CHARS` | `''` | '' = the auto-apply is OFF by default (every pre-feature line renders bit-identically); job-config.json ships the shop set '-:–—=<>' = the characters auto-centred vertically between the baseline and the capline: every text character occurring in this string is raised or lowered (a pure vertical translate) so its own ink is centred on the cap-band midline (half the rendered cap height above the baseline) -- the characters a font typically draws too high (hyphen, colon, en/em dash, '=', '<', '>'). The inline `<vc>...</vc>` markup tag applies the same centering to any other characters it wraps. An empty string disables the auto-apply (the `<vc>` tag keeps working on its own). Cascades line/label/job; job-config supplies the shop default. |

## Example

More descender room: raise the relief higher, allow deeper scale.

```yaml
job:
  job_name: "Descender demo"
  descending_character_raise: 0.85
  descending_character_v_scale: 0.45
  content:
    - text: "Lighting qty gph"
    - text: "MEG 200A"
```

## Pitfalls

- Default vertical centering centers the baseline-to-capline BAND (the optical center); descenders hang into the bottom margin, where the relief clears them against the border. `v_center_by_content: true` restores ink-box centering.
- Band centering is active ONLY with `auto_descending_character_adjustment` on; with the relief off nothing would adjust a descender, so ink centering is kept and the field is inert.
- The relief is per colliding glyph, measured as true 2D swept-ink distance (not bounding boxes), and reciprocal: the glyph below may be scaled top-down too, capped by `descending_character_reciprocal_scale`.
- A text-text collision that nothing can clear aborts the job (`LabelRenderError`); a BORDER collision never aborts — it engraves the best allowance permits and logs a WARNING.

## Read next

- `references/semantics.md` — the full semantics for this topic: mechanisms, thresholds, tier interactions and worked examples the tables above cannot express. Read it before authoring anything non-trivial in this area.
- `references/snippets.yaml` — more YAML fragments for this topic.
- [`../../../JOB_SPEC.md`](../../../JOB_SPEC.md) — exhaustive field/enum/default reference for the whole spec.
- [`../../../job_spec.schema.json`](../../../job_spec.schema.json) — machine-readable schema for structural validation.
- Related skills: [line-stacking-and-reflow](../line-stacking-and-reflow/SKILL.md); [holes-and-collision](../holes-and-collision/SKILL.md); [inline-markup](../inline-markup/SKILL.md).
