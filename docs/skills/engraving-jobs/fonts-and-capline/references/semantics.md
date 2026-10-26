# Fonts, Small Caps and Cap-Line Enforcement — semantics

## What `text_height` means

`text_height` is the **nominal** engraved cap height (inches), cascading
line → label → job → config (fallback `0.25`). The rendered **toolpath**
height is `text_height - cutter_size` (the cutter radius on each side
consumes path height), so the engraved ink lands at the requested cap
height. Fit math (margins, stacking, collision) uses the same nominal value,
which is why raising a label's `text_height` moves everything predictably.

## Font names: two families, one field

`font` accepts (case-insensitive, canonicalized at validation):

- **PLT catalog fonts** — the `font_name` values of the extracted glyph
  sheets (`fonts/VP-EL-fonts/catalog/*.json`). These render **arc-native**:
  circular arcs in the glyph geometry stay arcs in the PLT.
- **TrueType fonts** — the basename of a `*.ttf` under `fonts/`, extension
  stripped.

Unknown names are rejected at validation with the offending value. The
catalog grows whenever a glyph sheet is extracted or a TTF is dropped in, so
**list the valid names at run time** rather than trusting any embedded copy:

```bash
python docs/schema/generate_schema_docs.py --show-fonts
```

Fallback when unset everywhere (spec + config): `ReliefSingleLineCAD-Regular`
(`DEFAULT_FONT`).

### Small caps: the ` SC` suffix

Appending ` SC` to **any** font name selects a synthetic small-caps variant:
uppercase glyphs engrave verbatim, ASCII lowercase `a-z` is downscaled by
`sc_scale` (fraction of cap height, default `0.7`, bounds `0.2–1.0`,
top-down about the glyph's own bottom). The
suffix is part of the font name string (e.g. `font: "Dino SC"`, and inside
markup `<f:Dino+SC>` with `+` as the canonical space). The same field drives
the inline `<sc>` tag (per-character small caps on any font); it is inert on
a non-SC font carrying no `<sc>` run.

## Cap-line enforcement (`enforce_cap_line`, default true)

Every font has a per-font **capline** (the reference-character height). An
ASCII alphanumeric `[0-9a-zA-Z]` glyph whose ink extends **beyond** that
capline is downscaled **top-down** — a vertical scale about the glyph's own
bottom, X untouched (so the aspect ratio changes) — landing its top exactly
on the capline. Rules that matter when authoring:

- **Strictly pre-markup:** enforcement applies to the raw glyph, BEFORE the
  small-caps, bold, `<scale:N>`, italic and `<sup>`/`<sub>` transforms.
- **Alphanumerics only:** punctuation, spaces, and derived Unicode symbols
  never move; a glyph sitting exactly at the capline (the reference char)
  never moves.
- **Arc side-effect:** a vertical-only scale turns circular arcs into
  ellipses, so clamped glyphs flatten at the chord tolerance. If a font's
  signature arc (e.g. the `O` in `AA`) must stay native, set
  `enforce_cap_line: false` for that line.
- Cascade: line → label → job → config (plate parity).

## Choosing a cutter per font

Fonts do not carry cutters: the ladder maps `text_height` → cutter diameter
(see the cutters skill). Multi-stroke (bold) catalog fonts change the ladder
(see the bold-multistroke skill). A line's font and `cutter_size` interact:
an explicit `cutter_size` bypasses the ladder for that line.
