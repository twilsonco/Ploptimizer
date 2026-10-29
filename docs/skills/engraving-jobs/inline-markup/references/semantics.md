# Inline Markup Tags — semantics

Markup styles PART of a text line. Tags live inside a `text:` value, never
cross a content-line boundary, and nest.

## Parsing model (read this before debugging markup)

- **Delimiters are configurable:** `html_brackets` (cascade line → label →
  job → config; fallback `"<>"`) is an EVEN-LENGTH string whose first half
  is the left delimiter and second half the right (`"<>"` → `<`/`>`,
  `"<<>>"`, `"«»"`). The halves must differ and contain no alphanumerics or
  newlines (`"%%"`, `"||"`, `"  "` are rejected — identical halves cannot
  tell open from close). Choose a pair your data never contains so chevrons
  engrave literally.
- **Literal-first scanning:** the leftmost left delimiter opens a tag ONLY
  when its body matches a registered tag name (`name` or `name:args`).
  Anything else — a lone `<` in `3 < 5`, an unregistered `<xyz>`, a stray
  closer — is literal text.
- **Crossing spans are legal and HTML-style:** each character's style is the
  per-character INTERSECTION of the spans covering it.
  `<s>Always <sc>on</s> duty</sc>` engraves 'Always on' struck, 'on duty'
  small-caps, 'on' with both.
- **Broken markup is a hard error:** a close whose name was never opened,
  or an opener never closed, raises `MarkupError` (non-zero exit) —
  consistent with the fail-loud contract.
- Parsing runs at resolution time, so markup errors surface with the other
  spec-level validation errors.

## The fourteen tags

Registry order: `i, u, b, scale, sup, sub, s, sc, f, c, oc, vc, tc, cb`.

| Tag | Effect | Tuning fields |
|---|---|---|
| `<i>…</i>` | Italic: shear about each glyph's own baseline (X shifts by height × tan, Y untouched; a descender's bottom-left moves LEFT) | `italic_angle` (degrees, default 12) |
| `<u>…</u>` | Underline: one continuous stroke per maximal span, at `underline_offset_fraction` of cap height below the baseline; spans BREAK over spaces (and over gaps ≥ `underline_break_clearance_fraction` of cap height); `underline_break_spaces` (default true) drops the engraving of spaces inside the span | `underline_offset_fraction`, `underline_break_clearance_fraction`, `underline_break_spaces` |
| `<b>…</b>` | Bold. Multi-stroke PLT fonts: re-runs the cutter ladder at `target_boldness_factor × bold_boldness_multiplier` (clamped 0.30, WARNING). Single-stroke PLT/TTF: steps to the next larger inventory cutter | `target_boldness_factor`, `bold_boldness_multiplier` |
| `<scale:N>…</scale>` | Scales the run's text height by factor N (the run re-enters the cutter ladder at `N × text_height`) | — |
| `<sup>…</sup>` / `<sub>…</sub>` | Superscript / subscript: glyph downscaled by `supersubscript_downscale` and raised/dropped | `supersubscript_downscale` (default 0.6) |
| `<s>…</s>` | Strikethrough: one stroke at `strikethrough_offset_fraction` of cap height above the baseline; breaks over spaces like `<u>` (`strikethrough_break_spaces`) | `strikethrough_offset_fraction`, `strikethrough_break_spaces` |
| `<sc>…</sc>` | Small caps on ANY font: ASCII lowercase renders as the uppercase glyph downscaled to `sc_scale` of cap height, top-down; the nominal `text_height` is untouched (no cutter change) | `sc_scale` |
| `<f:name>…</f>` | Font for the run. Argument uses `+` as the canonical space (`<f:Dino+SC>`); unknown names raise. The run engraves with the cutter its own font's ladder gives that text height | — |
| `<c:D>…</c>` | Force cutter diameter D (inches) for the run; snapped to the `tools.json` inventory like `cutter_size` | — |
| `<oc>…</oc>` | Original cutter: return to the line's ladder cutter after a `<c:>` | — |
| `<vc>…</vc>` | Vertical centering of the run's characters against the line's `v_center_chars` set (e.g. center a serial against digits) | `v_center_chars` |
| `<tc:color>…</tc>` | Stroke color for the run: the run splits into a SEPARATE per-color toolpath (and its own PLT file). `none` = the default stroke. Line-level whole-line color is the `text_color` field (same enum) | `text_color` |
| `<cb:D>…</cb>` | Kerf buffer for the run: `D` inches REPLACES the line's cascaded `cutter_buffer_text` for the wrapped characters (the width the tool actually engraves, `cutter + buffer`). `D` is a mandatory float in `[0.0, 1.0]` — a bare `<cb>` engraves literally (the `<c:>` rule) and `0.0` explicitly zeroes the buffer for the span. Pure width modifier: no inventory snap, no new pen, no extra PLT file | `cutter_buffer_text` |

## Interactions that bite

- **Transform order:** cap-line enforcement (`enforce_cap_line`) is strictly
  PRE-markup; small-caps, bold, scale, italic and super/subscript apply to
  the clamped glyph, in registry order.
- **Cutter-bearing tags (`<b>`, `<scale:>`, `<f:>`, `<c:>`) change the pen
  map:** the run lands in the PLT file of its own cutter, so a line can
  split across per-cutter files.
- **`<tc:>` splits strokes by color** — one tag, one toolpath; combined with
  cutter splits, each (cutter, color) pair is its own path.
- **`<cb:D>` widens ink, never the tool:** unlike `<c:>`, `<scale:>`, `<b>`
  and `<f:>` it changes no pen map and writes no extra file. It composes with
  the cutter-bearing tags — the run's physical tool is whatever `<c:>`,
  `<scale:>`, `<b>` or the ladder chose, and `<cb:D>` adds `D` to that tool
  for the span. Glyph advance and
  per-pair kerning clearance use each character's own buffer
  (`effective_per_char_cutters`); the collision stroke floor, the descender
  relief and the `all_*.pdf` proof widths use the **widest** stroke, so per
  physical tool the **maximum** buffer among its characters wins
  (`effective_used_cutters`).
- **`<cb:D>` does NOT re-scale the text:** `toolpath_text_height` is resolved
  once from the line's cascaded `cutter_buffer_text` scalar
  (`text_height - effective`), so an inline override widens the stroke inside
  the line's existing cap height; only the line-level field preserves the
  engraved silhouette height. Nesting is set-to-value and idempotent —
  `<cb:0.02>` inside `<cb:0.01>` restores `0.01` for the trailing text.
- **Decorations follow the buffer:** underline/strikethrough runs split at
  buffer boundaries, so each run's centerline offset and break cuts use that
  run's own engraved width (the ink gap under the text stays exactly
  `underline_offset_fraction` of cap height at any buffer).
- **Reflow respects styles:** `optimize_line_content` keeps each word's tag
  spans intact (it re-serializes runs), and underline/strike spans follow
  the reflowed lines.
- **Kerning markers are not tags:** `kern_pos_string` bodies must not look
  like markup (see the kerning skill).

```yaml
job:
  job_name: "Markup"
  content:
    - text: "Nom <i>240</i> V / <b>50</b> A"
    - text: "Area 12<sup>2</sup> ft"
    - text: "<u>SHUT POWER</u> <s>THEN</s> PROCEED"
    - text: "Model <f:Dino>X900</f> <tc:red>HAZARD</tc>"
    - text: "Pocket <cb:0.020>deep cut</cb> <cb:0>surface score</cb>"
```
