---
name: inline-markup
description: 'Fourteen in-text tags: style, scale, font, cutter, color, center, kerf buffer. Use when: styling part of a line; mixing fonts or cutters inside one line; splitting strokes by color; superscripts, units, serial prefixes; widening the kerf buffer on part of a line. Keywords: markup, italic, bold, underline, superscript, scale, small caps, text_color, cb, cutter_buffer_text, kerf, buffer.'
version: 1.0.0
metadata:
  hermes:
    category: engraving-jobs
    tags: [plt, engraving, toolpath, generate]
---

# Inline Markup Tags

## When to use

- styling part of a line
- mixing fonts or cutters inside one line
- splitting strokes by color
- superscripts, units, serial prefixes
- widening the kerf buffer on part of a line

## Fields

| Field | Type | Default | Constraints | Tiers | Description |
|---|---|---|---|---|---|
| `supersubscript_downscale` | float \| null | null (unset) | >=0.2 <=1 | job / label / content entry / line / list block | Downscale applied to a bare `<sup>`/`<sub>` markup run: the run's glyphs engrave uniformly shrunk to this fraction of the line's size, and the baseline shift derives from it, centering the shrunken glyph on the line it belongs to (superscript rises 1 - d/2 cap heights, its centre landing on the capline; subscript drops d/2 cap heights, its centre landing on the baseline). The argumented forms `<sup:0.3>`/`<sub:-0.2>` set the shift explicitly (cap-height units) and apply no scaling, so this field is inert on them. The run engraves with the cutter for its downscaled height (the `<scale:N>` ladder). Cascades line -> label -> job (fallback 0.5); explicit 0.2/1.0 are honored. |
| `html_brackets` | str \| null | null (unset) | — | job / label / content entry / line / list block | Delimiter pair for the inline markup syntax (`<i>` italic and future tags): an even-length string whose first half is the left delimiter and second half the right delimiter (`<>` -> `'<' + '>'`, `<<>>` -> `<<' + '>>`). The halves must differ and contain no alphanumeric characters or newlines. A bracketed body that is not a registered tag engraves verbatim, so choosing a pair absent from the data keeps chevrons from being read as markup. Cascades line -> label -> job (fallback `<>`); the slash is only special inside the delimiters. |
| `italic_angle` | float \| null | null (unset) | >=0 <=45 | job / label / content entry / line / list block | Slant angle in degrees for italic markup (`<i>`): PLT-extracted glyphs (and TTF glyphs with no native italic face) are sheared about the baseline, $x' = x + y * tan(angle)$, so the baseline stays fixed and the glyph leans right. Bound to `[0, 45]`; 0 means upright (the shear is skipped). Cascades line -> label -> job (fallback 12); explicit 0.0/45.0 are honored. |
| `underline_offset_fraction` | float \| null | null (unset) | >=0 | job / label / content entry / line / list block | Extra air for underline markup (`<u>`) as a fraction of the rendered cap height (capline - baseline): the underline centerline (cut with the line's own text cutter) sits cutter diameter + fraction * cap height below the baseline, so the engraved underline's top edge floats exactly that air below the engraved baseline stroke. The break test is strictly horizontal: a glyph breaks the underline where its ink crosses the underline's centerline, so raising it lifts the underline clear and stops it breaking. 0.0 = the underline's top edge touches the baseline stroke. Must be >= 0. Cascades line -> label -> job (fallback 0.0); explicit 0.0 is honored. |
| `underline_break_clearance_fraction` | float \| null | null (unset) | >=0 | job / label / content entry / line / list block | Extra air at each underline (`<u>`) break as a fraction of the rendered cap height (capline - baseline): each cut is padded horizontally by `0.5 * cutter + fraction * cap height` (the half cutter is the glyph stroke's own half-width), so the engraved air between a descending glyph's stroke and the underline's cut edge is exactly this value. 0.0 = the cut sits at the ink. Independent of underline_offset_fraction (which decides which glyphs break). Must be >= 0. Cascades line -> label -> job (fallback 0.0); explicit 0.0 is honored. |
| `underline_break_spaces` | bool \| null | null (unset) | — | job / label / content entry / line / list block | Break an underline (`<u>`) at the spaces between words: false (default) draws one continuous underline across the whole underlined run (spaces included, broken only by descending glyphs); true ends the underline at each space so every word gets its own underline and the inter-word space stays bare. Cascades line -> label -> job (fallback false); explicit false is honored. |
| `strikethrough_offset_fraction` | float \| null | null (unset) | >=0 | job / label / content entry / line / list block | Strikethrough (`<s>`) height ABOVE the baseline as a fraction of the rendered cap height (`capline - baseline`): the stroke (cut with the line's own text cutter) sits `fraction * cap height` above the baseline, so `0.0` puts it on the baseline and `1.0` puts it on the capline. Unlike the underline the stroke is never broken -- a descender punches through it. `null` (default) centres the stroke on the cap-band midline (half the rendered cap height). Must be >= 0. Cascades line -> label -> job (fallback `null` = mid-cap); explicit 0.0 is honored. |
| `strikethrough_break_spaces` | bool \| null | null (unset) | — | job / label / content entry / line / list block | Break a strikethrough (`<s>`) at the spaces between words: false (default) draws one continuous stroke across the whole struck run (spaces included); true ends the stroke at each space so every word gets its own strike and the inter-word space stays bare. Cascades line -> label -> job (fallback false); explicit false is honored. |
| `text_color` | TextColor \| null | null (unset) | — | job / label / content entry / line / list block | Stroke-color layer tag splitting otherwise-identical text into separate toolpaths (labels and text lines only; rejected at the job level; never cascades). Full name or case-insensitive single-letter abbreviation (c, m, y, k, r, g, b, v, o, p, t); `none` is the implicit default and cannot be specified. The inline `<tc:color>...</tc>` markup applies the same split to the wrapped segment, overriding this field per character. |
| `supersubscript_downscale` | float \| null | null (unset) | >=0.2 <=1 | plate | Bare `<sup>`/`<sub>` markup run downscale (schema parity; not applied at plate level). |
| `html_brackets` | str \| null | null (unset) | — | plate | Inline-markup delimiter pair (even length; first half left, second half right) (schema parity; not applied at plate level). |
| `italic_angle` | float \| null | null (unset) | >=0 <=45 | plate | Italic slant angle in degrees for `<i>` markup (schema parity; not applied at plate level). |
| `underline_offset_fraction` | float \| null | null (unset) | >=0 | plate | Underline (`<u>`) extra air below the baseline stroke as a fraction of the rendered cap height (schema parity; not applied at plate level). |
| `underline_break_clearance_fraction` | float \| null | null (unset) | >=0 | plate | Underline (`<u>`) extra air at each break as a fraction of the rendered cap height (schema parity; not applied at plate level). |
| `underline_break_spaces` | bool \| null | null (unset) | — | plate | Underline (`<u>`) break-at-spaces flag (schema parity; not applied at plate level). |
| `strikethrough_offset_fraction` | float \| null | null (unset) | >=0 | plate | Strikethrough (`<s>`) height above the baseline as a fraction of the rendered cap height (schema parity; not applied at plate level). |
| `strikethrough_break_spaces` | bool \| null | null (unset) | — | plate | Strikethrough (`<s>`) break-at-spaces flag (schema parity; not applied at plate level). |

## Enums

### `TextColor`

Enumeration of stroke-color layer tags for text lines and labels.

The color carries no visual meaning in the emitted PLT: it is a layer
tag that splits otherwise-identical text into separate toolpaths (one
HPGL `SP` layer and one PLT file per color), so the cutter depth can
be changed between runs to expose a different material layer color on
3-layer stock. This mirrors the EngraveLab/Vision Pro workflow where a
different stroke color forces a separate toolpath.

Values may be written as the full name (`cyan`) or as the
single-letter abbreviation (`c`); both forms are case-insensitive.
The inline `<tc:color>...</tc>` markup accepts exactly the same
values and applies the split per character (the tag wins over the
`text_color` field inside its span; see
`plt_optimizer/generate/markup/AGENTS.md`).
`none` is the implicit default for every line that does not declare
a color and must never be specified explicitly (see the `text_color`
field validator): users pick a real color name to force a toolpath
split.

| Value | Description |
|---|---|
| `cyan` | Cyan color layer. Abbreviation: `c`. |
| `magenta` | Magenta color layer. Abbreviation: `m`. |
| `yellow` | Yellow color layer. Abbreviation: `y`. |
| `black` | Black color layer. Abbreviation: `k`. |
| `red` | Red color layer. Abbreviation: `r`. |
| `green` | Green color layer. Abbreviation: `g`. |
| `blue` | Blue color layer. Abbreviation: `b`. |
| `violet` | Violet color layer. Abbreviation: `v`. |
| `orange` | Orange color layer. Abbreviation: `o`. |
| `pink` | Pink color layer. Abbreviation: `p`. |
| `teal` | Teal color layer. Abbreviation: `t`. |
| `none` | No color layer. |

## Defaults

Applied when unset at every tier (including `job-config.json`):

| Constant | Value | Note |
|---|---|---|
| `DEFAULT_HTML_BRACKETS` | `'<>'` | Delimiter pair for the inline markup syntax (`<i>` italic and future tags): an even-length string whose first half is the left delimiter and second half the right (`"<>"` -> `<` / `>`). Text that legitimately contains chevrons opts out by declaring a pair absent from its data; a bracketed body that is not a registered tag engraves verbatim. The forward slash is only special inside the delimiters (`</i>`), never elsewhere. See `plt_optimizer.generate.markup`. |
| `DEFAULT_ITALIC_ANGLE` | `12.0` | Slant angle (degrees) for italic markup: PLT-extracted glyphs (and TTF glyphs with no native italic face) are sheared about the baseline, $x' = x + y * tan(angle)$, so the baseline stays fixed and the glyph leans right. Bound to [0, 45] by the schema; 0 = upright (shear skipped). |
| `DEFAULT_STRIKETHROUGH_BREAK_SPACES` | `False` | False = an inline `<s>` strikethrough draws one continuous stroke across the whole struck run, inter-word spaces included. True ends the stroke at each space, so every word gets its own strike spanning its own ink and the space stays bare. Cascades line/label/job; job-config supplies the shop default. |
| `DEFAULT_STRIKETHROUGH_OFFSET_FRACTION` | `None` | `None` = an inline `<s>` strikethrough centres its centerline stroke on the cap-band midline (half the rendered cap height), the classic mid-height strike. An explicit value is the stroke's height as a fraction of the rendered cap height (`capline - baseline`) ABOVE the baseline (0.0 = the stroke sits on the baseline, 1.0 = on the capline). The stroke spans each struck run's ink width and is cut with the run's own text cutter; unlike the underline it is NEVER broken, so a descender punches through it (no clearance, no break test). Cascades line/label/job; job-config supplies the shop default. |
| `DEFAULT_SUPERSUBSCRIPT_DOWNSCALE` | `0.5` | 0.5 = a bare `<sup>`/`<sub>` markup run engraves its glyphs uniformly shrunk to 50% of the line's size, and the baseline shift derives from the same number, centering the shrunken glyph on the line it belongs to: a superscript rises $1 - d/2 = 0.75$ cap heights (its centre lands exactly on the capline) and a subscript drops $d/2 = 0.25$ cap heights (its centre lands on the baseline). The argumented forms `<sup:0.3>`/`<sub:-0.2>` set the shift explicitly in cap-height units (bound [-1.0, 1.0]) and apply no scaling, so this field is inert on them. A downscaled run engraves with the cutter for its downscaled height (the `<scale:N>` ladder), so it lands on its own per-cutter layer. Bound [0.2, 1.0]. Cascades line/label/job; job-config supplies the shop default. |
| `DEFAULT_TEXT_COLOR` | `'none'` | Implicit stroke-color layer of text that omits text_color (never cascades; a job-level text_color is rejected). 'none' cannot be specified explicitly. |
| `DEFAULT_UNDERLINE_BREAK_CLEARANCE_FRACTION` | `0.0` | Extra air at each underline break, as a fraction of the rendered cap height (capline - baseline). Each cut is padded horizontally by `0.5 * cutter + fraction * cap height` (the half cutter is the glyph stroke's own half-width -- the ink is a centerline), so the engraved air between a descending glyph's stroke and the underline's cut edge is exactly this clearance (0.0 = the cut sits at the ink). Independent of the offset (which decides which glyphs break); this adds air at the break itself. The code fallback is 0.0 (the historical cut); job-config.json supplies the shop default. See `plt_optimizer.generate.underline`. |
| `DEFAULT_UNDERLINE_BREAK_SPACES` | `False` | Whether an underline (`<u>`) breaks at the spaces between words. `False` (the default) draws one continuous underline across the whole underlined run (spaces included, broken only by descending glyphs); `True` ends the underline at each space, so every word gets its own underline spanning its own ink and the inter-word space stays bare. The code fallback is `False` (the historical continuous underline); job-config.json supplies the shop default. See `plt_optimizer.generate.label_renderer._underline_render_gate`. |
| `DEFAULT_UNDERLINE_OFFSET_FRACTION` | `0.0` | Extra air for underline markup (`<u>`) as a fraction of the rendered cap height (capline - baseline). The underline is a centerline stroke cut with the line's own text cutter, positioned `cutter diameter + fraction * cap height` below the baseline, so the engraved underline's top edge floats exactly `fraction * cap height` below the engraved baseline stroke. The break test is strictly horizontal: a glyph breaks the underline where its ink crosses the underline's centerline (dipping past `cutter diameter + fraction * cap height` below the baseline), so the offset alone decides which glyphs break. 0.0 = the underline's top edge touches the baseline stroke and gaps only where strokes truly cross. See `plt_optimizer.generate.underline`. |

## Example

Italic, bold and superscript in one line.

```yaml
job:
  job_name: "Markup demo"
  content:
    - text: "Nom <i>240</i> V / <b>50</b> A"
    - text: "Area 12<sup>2</sup> ft"
```

## Pitfalls

- Tags are inline spans inside a `text:` value and never cross content lines; nesting is supported. Their tuning fields (`italic_angle`, `underline_*`, `strikethrough_*`, `supersubscript_downscale`) cascade like everything else.
- `<b>` is real multi-pass bold ONLY on multi-stroke PLT fonts; on single-stroke PLT and TTF fonts it steps to the next larger inventory cutter.
- `<f:...>` uses `+` as the canonical space in the font name (e.g. `<f:Dino+SC>`); the run engraves with the cutter its own font's ladder gives that text height.
- `<c:D>` forces cutter diameter D for the run (snapped to the inventory); `<oc>` returns to the line's original cutter.
- `<tc:color>` splits the run into a separate per-color toolpath (and a separate PLT file); the `text_color` field colors a whole line.
- `<cb:D>` sets the kerf buffer (inches) for the run, replacing the line's cascaded `cutter_buffer_text` per character; `0.0` explicitly zeroes the buffer for the span. The argument is mandatory (a bare `<cb>` engraves literally, the `<c:>` rule) and must be a float in [0.0, 1.0].
- Unlike `<c:D>`, `<cb:D>` is a PURE width modifier: no inventory snap, no new pen, no extra PLT file — the physical tool stays whatever the ladder/`<c:>`/`<b>`/`<scale:>` chose and only the effective engraved width (`cutter + buffer`) widens. There is no inline form of `cutter_buffer_routing` (hole geometry is per label).
- `cb` is registered LAST in the tag registry so every existing tag's serialization order stays bit-identical.
- `html_brackets` is the delimiter pair itself (an even-length string whose first half opens and second half closes: `<>`, `<<>>`, `«»`); set it to a pair your data never contains. A bracketed body that is not a registered tag engraves verbatim, so `3 < 5` is safe, but a real tag name in chevrons is consumed as markup.

## Read next

- `references/semantics.md` — the full semantics for this topic: mechanisms, thresholds, tier interactions and worked examples the tables above cannot express. Read it before authoring anything non-trivial in this area.
- `references/snippets.yaml` — more YAML fragments for this topic.
- [`../engraving-job-complete-reference/references/JOB_SPEC.md`](../engraving-job-complete-reference/references/JOB_SPEC.md) — exhaustive field/enum/default reference for the whole spec.
- [`../engraving-job-complete-reference/references/job_spec.schema.json`](../engraving-job-complete-reference/references/job_spec.schema.json) — machine-readable schema for structural validation.
- Related skills: [fonts-and-capline](../fonts-and-capline/SKILL.md); [cutters](../cutters/SKILL.md); [bold-multistroke](../bold-multistroke/SKILL.md); [lists](../lists/SKILL.md).
