---
name: glyph-spacing-and-kerning
description: 'Per-character advance, space width, auto-kerning tuning. Use when: letters ride too close or too far; tuning space width; using manual kerning markers in text. Keywords: character_spacing, kerning, space_width_fraction, glyph, advance, kern_pos_string.'
version: 1.0.0
metadata:
  hermes:
    category: plt-optimizer-generate
    tags: [plt, engraving, toolpath, generate]
---

# Glyph Spacing and Kerning

## When to use

- letters ride too close or too far
- tuning space width
- using manual kerning markers in text

## Fields

| Field | Type | Default | Constraints | Tiers | Description |
|---|---|---|---|---|---|
| `character_spacing` | float \| null | null (unset) | — | job / label / content entry / line / list block | Extra spacing between characters in inches (fallback: 1.5x the resolved cutter diameter). |
| `space_width_fraction` | float \| null | null (unset) | >=0 | job / label / content entry / line / list block | Space advance as a fraction of the rendered text height (PLT-extracted fonts): a space advances `space_width_fraction * text_height + character_spacing`. Cascades line -> label -> job (fallback 0.3); explicit 0.0 is honored. |
| `min_glyph_width_fraction` | float \| null | null (unset) | >=0 | job / label / content entry / line / list block | Global minimum glyph advance width as a fraction of the rendered cap height `(capline - baseline)` for PLT-extracted fonts: clamps the profile-envelope kerning so zero-width glyphs still reserve real air. Cascades line -> label -> job (fallback 0.0 = pure envelope kerning); explicit 0.0 is honored. |
| `kerning_window_fraction` | float \| null | null (unset) | >=0 <=1 | job / label / content entry / line / list block | Kerning window for PLT-extracted fonts as a fraction of the rendered text height in `[0.0, 1.0]`: each envelope sample compares against the opposite silhouette within +/- (half of this fraction) of the text height and the effective penetration is the worst windowed penetration, so staggered pokes widen the advance. Cascades line -> label -> job (fallback 0.05); explicit 0.0 is honored (= historical same-height kerning). |
| `kerning_penetration_scale` | float \| null | null (unset) | >=0 | job / label / content entry / line / list block | Multiplier on the profile-envelope penetration detected for PLT-extracted fonts: 1.0 keeps the geometric penetration, >1.0 over-kerns tight pairs proportionally. Cascades line -> label -> job (fallback 1.0); explicit 0.0 is honored (ignores detected closeness entirely). |
| `kerning_recession_scale` | float \| null | null (unset) | >=0 | job / label / content entry / line / list block | Multiplier on a *recessed* (negative) profile-envelope penetration for PLT-extracted fonts: 1.0 keeps the geometric recession (the historical linear behavior, where lowering `kerning_penetration_scale` also pulled gapped pairs closer), `0.0` makes a recessed pair advance by nothing beyond the `min_glyph_width_fraction` floor. Cascades line -> label -> job (fallback 1.0); explicit 0.0 is honored. |
| `kerning_min_gap_fraction` | float \| null | null (unset) | >=0 | job / label / content entry / line / list block | Extra air added to every kerned character pair advance (PLT-extracted fonts) as a fraction of the rendered cap height (capline - baseline), on top of the cutter diameter and `character_spacing` clearance. Cascades line -> label -> job (fallback 0.0 = no extra gap); explicit 0.0 is honored. |
| `fallback_advance_fraction` | float \| null | null (unset) | >=0 | job / label / content entry / line / list block | Multiplier on the bounding-box-width fallback advance used for PLT-extracted glyph pairs without overlapping height (or lacking envelopes). Cascades line -> label -> job (fallback 1.0 = the left glyph's own width); explicit 0.0 is honored. |
| `kern_pos_string` | str \| null | null (unset) | — | job / label / content entry / line / list block | Manual-kern positive marker string: every occurrence in a text line is stripped (it never engraves) and replaced by a forward cursor shift of `kern_width_fraction * cap height` (capline - baseline) applied to the following character. Multi-character strings are legal (e.g. `<->`); the value must be non-empty, contain no newline, and differ from kern_neg_string. A marker that parses as inline markup under the line's effective html_brackets (e.g. `<s>` with the default brackets - the strikethrough tag consumes it before the kern strip) is rejected at resolution; `<->` is a legal marker. Unset (`None`) disables the positive marker. Cascades line -> label -> job (`job-config.json` supplies the shop default); works on PLT and TTF fonts. |
| `kern_neg_string` | str \| null | null (unset) | — | job / label / content entry / line / list block | Manual-kern negative marker string: every occurrence in a text line is stripped (it never engraves) and replaced by a backward cursor shift of `kern_width_fraction * cap height` (capline - baseline) applied to the following character (no clamp, so a large value lets glyphs overlap). Same validation as `kern_pos_string`; the two strings must differ. Unset (`None`) disables the negative marker. Cascades line -> label -> job (`job-config.json` supplies the shop default); works on PLT and TTF fonts. |
| `kern_width_fraction` | float \| null | null (unset) | >=0 | job / label / content entry / line / list block | Manual-kern width as a fraction of the rendered cap height (capline - baseline): one `kern_pos_string` marker advances the cursor by `+fraction * cap height`, one `kern_neg_string` marker retreats it by `-fraction * cap height` (adjacent markers accumulate). Cascades line -> label -> job (fallback `0.1`); explicit `0.0` is honored (markers consume their characters but shift nothing). |
| `space_width_fraction` | float \| null | null (unset) | >=0 | plate | Space advance fraction (schema parity; not applied at plate level). |
| `min_glyph_width_fraction` | float \| null | null (unset) | >=0 | plate | Global minimum glyph advance width as a fraction of the rendered cap height (schema parity; not applied at plate level). |
| `kerning_window_fraction` | float \| null | null (unset) | >=0 <=1 | plate | Kerning window fraction as a fraction of the rendered text height (schema parity; not applied at plate level). |
| `kerning_penetration_scale` | float \| null | null (unset) | >=0 | plate | Kerning penetration multiplier applied to the detected penetration (schema parity; not applied at plate level). |
| `kerning_recession_scale` | float \| null | null (unset) | >=0 | plate | Kerning recession multiplier applied to a negative (recessed) detected penetration (schema parity; not applied at plate level). |
| `kerning_min_gap_fraction` | float \| null | null (unset) | >=0 | plate | Extra kerning air added to every kerned pair advance as a fraction of the rendered cap height (schema parity; not applied at plate level). |
| `fallback_advance_fraction` | float \| null | null (unset) | >=0 | plate | Fallback advance multiplier on the bounding-box width for pairs without overlapping height (schema parity; not applied at plate level). |
| `kern_pos_string` | str \| null | null (unset) | — | plate | Manual-kern positive marker string (schema parity; not applied at plate level). |
| `kern_neg_string` | str \| null | null (unset) | — | plate | Manual-kern negative marker string (schema parity; not applied at plate level). |
| `kern_width_fraction` | float \| null | null (unset) | >=0 | plate | Manual-kern width as a fraction of the rendered cap height (schema parity; not applied at plate level). |

## Defaults

Applied when unset at every tier (including `job-config.json`):

| Constant | Value | Note |
|---|---|---|
| `DEFAULT_FALLBACK_ADVANCE_FRACTION` | `1.0` | Multiplier on the bounding-box-width fallback advance (no y-overlap or missing envelopes). 1.0 = the left glyph's own width. |
| `DEFAULT_KERNING_MIN_GAP_FRACTION` | `0.0` | Extra air added to every kerned pair advance on top of the cutter + character-spacing clearance, as a fraction of the rendered cap height (capline - baseline). 0.0 = no extra gap. |
| `DEFAULT_KERNING_PENETRATION_SCALE` | `1.0` | Multiplier on the detected windowed penetration (PLT-extracted fonts). 1.0 keeps the geometric penetration; >1.0 over-kerns tight pairs. |
| `DEFAULT_KERNING_RECESSION_SCALE` | `1.0` | Multiplier on a *recessed* (negative) detected penetration (PLT-extracted fonts). 1.0 keeps the geometric recession (the historical linear behavior); 0.0 makes a recessed pair advance by nothing beyond the min_glyph_width_fraction floor, so the penetration dial only tightens pairs whose silhouettes overlap. |
| `DEFAULT_KERNING_WINDOW_FRACTION` | `0.05` | Kerning window (fraction of the rendered text height, in [0.0, 1.0]). For PLT-extracted fonts. Each envelope sample compares against the opposite silhouette within +/- (half of this fraction) of the text height and the effective penetration is the worst windowed penetration, so staggered pokes widen the advance; 0.0 reproduces the historical same-height maximum-penetration kerning. |
| `DEFAULT_KERN_NEG_STRING` | `None` | `None` = the manual-kern negative marker is disabled. When set (`kern_neg_string`), every occurrence is stripped and the following character's cursor retreats by `kern_width_fraction` of the cap height (no clamp, so a large value lets glyphs overlap). Same validation as `kern_pos_string`; the two strings must differ. |
| `DEFAULT_KERN_POS_STRING` | `None` | `None` = the manual-kern positive marker is disabled (no occurrence is stripped). When set (`kern_pos_string`, cascades line/label/job; job-config supplies the shop default), every occurrence of the string in a text line is stripped - it never engraves - and the following character's cursor advances by `kern_width_fraction` of the cap height. Multi-character markers are legal ('<->'); the value must be non-empty, newline-free, and not markup-shaped (a letters-only body like `<s>` is consumed by the tag scan, so the schema rejects it; the resolution-time check validates against the effective `html_brackets`). Works on PLT and TTF fonts. |
| `DEFAULT_KERN_WIDTH_FRACTION` | `0.1` | 0.1 = the manual-kern width as a fraction of the rendered cap height (capline - baseline): one marker shifts the cursor by this fraction of the cap height (positive string forward, negative backward; adjacent markers accumulate). Cascades line/label/job; job-config supplies the shop default. Explicit 0.0 is honored (markers consume their characters, shift nothing). |
| `DEFAULT_MIN_GLYPH_WIDTH_FRACTION` | `0.0` | Global minimum glyph advance width (as a fraction of the rendered cap height, capline - baseline) for PLT-extracted fonts. The profile-envelope kerning floors each pair's advance at this fraction of the cap height, capped by the left glyph's own width; 0.0 means pure envelope kerning. |
| `DEFAULT_SPACE_WIDTH_FRACTION` | `0.3` | Space advance (PLT-extracted fonts) as a fraction of the rendered text height: a space advances `DEFAULT_SPACE_WIDTH_FRACTION * text_height + character_spacing`. |

## Example

Add breathing room between characters.

```yaml
job:
  job_name: "Tracking demo"
  character_spacing: 0.12
  content:
    - text: "SPACED OUT"
```

## Pitfalls

- These are shop-level tuning knobs; most jobs never set them — start with `character_spacing` and leave the `kerning_*` family at defaults.
- `kern_pos_string`/`kern_neg_string` are manual-kern markers: each occurrence is stripped (it never engraves) and the NEXT character's cursor advances by `kern_width_fraction` of the cap height (the negative marker moves it back).
- Markers must be non-empty, newline-free, and not markup-shaped (a letters-only body like `<s>` would be eaten by the tag scan, so the schema rejects it).
- `min_glyph_width_fraction` floors how narrow a glyph may get, protecting legibility when spacing/compression squeeze a line.

## Read next

- `references/semantics.md` — the full semantics for this topic: mechanisms, thresholds, tier interactions and worked examples the tables above cannot express. Read it before authoring anything non-trivial in this area.
- `references/snippets.yaml` — more YAML fragments for this topic.
- [`../../../JOB_SPEC.md`](../../../JOB_SPEC.md) — exhaustive field/enum/default reference for the whole spec.
- [`../../../job_spec.schema.json`](../../../job_spec.schema.json) — machine-readable schema for structural validation.
- Related skills: [fonts-and-capline](../fonts-and-capline/SKILL.md); [inline-markup](../inline-markup/SKILL.md); [compression-and-cutter-downsize](../compression-and-cutter-downsize/SKILL.md).
