---
name: fonts-and-capline
description: 'Pick fonts (PLT catalog, TTF, SC), text_height, cap-line clamp. Use when: choosing a font; letters sit off the cap line; using small caps (SC); setting text height. Keywords: font, text_height, small caps, sc_scale, cap line, enforce_cap_line, ttf.'
version: 1.0.0
metadata:
  hermes:
    category: plt-optimizer-generate
    tags: [plt, engraving, toolpath, generate]
---

# Fonts, Small Caps and Cap-Line Enforcement

## When to use

- choosing a font
- letters sit off the cap line
- using small caps (SC)
- setting text height

## Fields

| Field | Type | Default | Constraints | Tiers | Description |
|---|---|---|---|---|---|
| `text_height` | float \| null | null (unset) | — | job / label / content entry / line / list block | Font height in inches. Cascades line -> label -> job (fallback 0.25). |
| `font` | str \| null | null (unset) | — | job / label / content entry / line / list block | Font name: a PLT-extracted font key (the font_name fields of `fonts/VP-EL-fonts/catalog/*.json`, rendered arc-native with arcs preserved) or a TrueType basename (`*.ttf` under fonts/, extension stripped); case-insensitive, canonicalized at validation. Appending ' SC' to any name selects its synthetic small-caps variant (uppercase glyphs verbatim, ASCII lowercase a-z downscaled by sc_scale). Cascades line -> label -> job (fallback ReliefSingleLineCAD-Regular). Unknown names are rejected; run docs/schema/generate_schema_docs.py --show-fonts for the full valid list. The inline `<f:font name>...</f>` markup tag selects any of these names for the wrapped characters of a text line (the run engraves with the cutter its own font's ladder gives that text height); '+' is the canonical space in the tag argument (e.g. `<f:Dino+SC>`). |
| `sc_scale` | float \| null | null (unset) | >=0.2 <=1 | job / label / content entry / line / list block | Downscale applied to ASCII lowercase a-z by a small-caps font (a font name ending in ' SC', e.g. 'Dino SC'): the lowercase character renders as the base font's uppercase glyph uniformly shrunk to `sc_scale` of the cap height, top-down (the glyph's own bottom stays in place and its top descends). Uppercase and caseless characters engrave at full size; the nominal `text_height` is untouched (no cutter change). The same field drives the inline `<sc>` small-caps markup tag (the per-character selection); inert on a non-SC font carrying no `<sc>` run. Cascades line -> label -> job (fallback 0.7); explicit 0.2/1.0 are honored. |
| `enforce_cap_line` | bool \| null | null (unset) | — | job / label / content entry / line / list block | Clamp alphanumeric characters to the capline: when true (the default), every ASCII alphanumeric `[0-9a-zA-Z]` glyph whose ink extends above the per-font cap height is downscaled top-down (a vertical downscale about the glyph's own bottom, X untouched -- the aspect ratio changes) so its top lands exactly on the capline. The enforcement is strictly pre-markup: it applies before the small-caps, bold, scale, italic and superscript/subscript transforms. Non-alphanumeric characters (punctuation, spaces, derived Unicode symbols) are never clamped. Cascades line -> label -> job (fallback true); explicit false is honored. |
| `font` | str \| null | null (unset) | — | plate | Font name (schema parity; not applied at plate level). Accepted and canonicalized like the cascading font field. |
| `sc_scale` | float \| null | null (unset) | >=0.2 <=1 | plate | Small-caps downscale applied to ASCII lowercase a-z by a ' SC' font variant (schema parity; not applied at plate level). |
| `enforce_cap_line` | bool \| null | null (unset) | — | plate | Alphanumeric cap-line clamping (schema parity; not applied at plate level -- labels render before packing). |

## Defaults

Applied when unset at every tier (including `job-config.json`):

| Constant | Value | Note |
|---|---|---|
| `DEFAULT_ENFORCE_CAP_LINE` | `True` | `True` = every ASCII alphanumeric [0-9a-zA-Z] glyph whose ink extends above the per-font capline is downscaled top-down (a vertical scale about the glyph's own bottom, X untouched - the aspect ratio changes) so its top lands exactly on the capline. Strictly pre-markup: applied before the small-caps, bold, scale, italic and superscript/subscript transforms. Non-alphanumerics (punctuation, derived Unicode symbols) are never clamped. Cascades line/label/job; job-config supplies the shop default. |
| `DEFAULT_FONT` | `'ReliefSingleLineCAD-Regular'` | Font fallback when unset at line/label/job level (and when job-config font is unset). Must be a valid font_registry name: a PLT-extracted catalog `font_name` key (arc-native) or a fonts/ TTF basename; run `generate_schema_docs.py --show-fonts`. |
| `DEFAULT_FONT_NAME` | `'ReliefSingleLineCAD-Regular'` | Registry-level default font name (font_registry.DEFAULT_FONT_NAME); resolution.DEFAULT_FONT mirrors it. |
| `DEFAULT_SC_SCALE` | `0.7` | 0.7 = a small-caps font (a font name ending in ' SC', a synthetic variant of the base font) renders ASCII lowercase a-z as the base font's uppercase glyph uniformly shrunk to 70% of the cap height, top-down (the glyph's own bottom stays in place, its top descends). Uppercase and caseless characters engrave at full size and the nominal text_height is untouched (no cutter change). 1.0 renders the variant identically to its base font; the field is inert on a non-SC font. Cascades line/label/job; job-config supplies the shop default. |
| `DEFAULT_TEXT_HEIGHT` | `0.25` | Font height when unset at line/label/job level. |

## Example

Font at job level, per-line override, small-caps variant.

```yaml
job:
  job_name: "Font demo"
  font: "ReliefSingleLineCAD-Regular"
  content:
    - text: "Main line"
    - text: "Small caps line"
      font: "ReliefSingleLineCAD-Regular SC"
```

## Pitfalls

- `text_height` is nominal: the toolpath height is `text_height - cutter_size`, so fit math and the engraved cap height both land at the requested size.
- Appending ` SC` to any font name selects its synthetic small-caps variant (uppercase verbatim, lowercase downscaled by `sc_scale`).
- `enforce_cap_line` clamps over-tall alphanumerics top-down (strictly pre-markup: before small-caps, bold, scale, italic and super/subscript); punctuation and derived glyphs never move.
- Valid font names change as the catalog grows — list them with `python docs/schema/generate_schema_docs.py --show-fonts`.

## Read next

- `references/semantics.md` — the full semantics for this topic: mechanisms, thresholds, tier interactions and worked examples the tables above cannot express. Read it before authoring anything non-trivial in this area.
- `references/snippets.yaml` — more YAML fragments for this topic.
- [`../../../JOB_SPEC.md`](../../../JOB_SPEC.md) — exhaustive field/enum/default reference for the whole spec.
- [`../../../job_spec.schema.json`](../../../job_spec.schema.json) — machine-readable schema for structural validation.
- Related skills: [inline-markup](../inline-markup/SKILL.md); [bold-multistroke](../bold-multistroke/SKILL.md); [cutters](../cutters/SKILL.md); [glyph-spacing-and-kerning](../glyph-spacing-and-kerning/SKILL.md).
