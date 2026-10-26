# Line Spacing, Baseline Stacking and Reflow — semantics

## Two stacking models

A label's content lines stack vertically by one of two models
(`use_baseline_spacing`, cascade label → job → config, never text lines —
spacing is a property of the stacked block; fallback `true`):

- **Baseline spacing (default).** Each line's baseline sits one pitch below
  the previous one, where `pitch = cap height + line_spacing`. A descender
  (`g j p q y`) hangs INTO the gap, so every gap on the label reads as one
  uniform visual spacing. Lines without descenders are bit-identical to the
  old model.
- **Ink-box stacking (`use_baseline_spacing: false`).** Each line's rendered
  ink box stacks directly, so one descender-heavy line pushes the next line
  down by its full ink height and inflates that one gap. This is the
  historical behavior; `false` restores it bit-identically.

Auto `line_spacing` and the vertical margin clamp measure each line's real
ascender/descender extents (a render probe), so a descender-heavy block
claims its space up front and fills the label exactly.

## `line_spacing`

Extra spacing between lines, in **inches** (cascade line → label → job →
config; fallback `0.0`-ish auto behavior — see the generated Defaults
table). Under baseline spacing it is added to the cap-height pitch.

## `auto_line_spacing_interline_to_top_bottom_ratio`

When `line_spacing` is unset, the auto spacing distributes the label's free
vertical space. This ratio scales the INTERLINE gaps relative to the
top/bottom padding: `1.0` spreads everything evenly; a smaller value keeps
the lines tight as a block and gives the surplus to the top/bottom margins.
Cascade label → job → config (plate parity).

## Reflow: `optimize_line_content`

Word-level reflow grows one text line into as many lines as the label's
usable width requires:

- Enabled per line/label/job (cascades; content entries and list items
  inherit). Words never split; the group's words fill the lines from the
  flagged line downward.
- `optimize_line_content_max_lines` caps the growth (group-scoped). Two
  different caps inside ONE reflow group raise
  `LineContentConfigError`; caps in two groups separated by a `new_line` are
  legal. No cap = the item grows as long as it needs.
- `new_line` (line-level ONLY, never cascades) starts a NEW reflow group:
  the flagged line and the lines after it reflow together, separate from the
  lines above. The flag is **inert** unless the line also enables
  `optimize_line_content` — it never raises and never enables reflow by
  itself.

```yaml
job:
  job_name: "Reflow"
  width: 4.0
  content:
    - text: "HEAD"
    - text: "This long sentence reflows into three lines max."
      optimize_line_content: true
      optimize_line_content_max_lines: 3
      new_line: true          # starts its own group after HEAD
```

## Interactions

- Reflow + baseline spacing: the grown lines stack by the same pitch model;
  the block re-centers as one unit.
- Reflow inside a list item: the item's continuation lines keep the
  hanging-indent column (see the lists skill).
- Reflow + `max_h_compress`: reflow adds lines first; compression squeezes
  the width. A line that reflows does not also need to compress, and vice
  versa — the budget fields interact per the compression skill.
- Reflow + replacement files: a template line that reflows reflows per copy
  (the item text varies, the width budget does not).
