# Glyph Spacing and Kerning — semantics

These fields tune the horizontal advance between glyphs. They are shop-level
knobs: **most jobs set none of them.** If a line reads too tight/too loose,
`character_spacing` is the first (and usually last) field to touch; the
`kerning_*` family re-tunes the automatic kerning algorithm itself.

All of these are in inches or fractions of the **rendered cap height**
(`capline - baseline`) — not of `text_height` — so they scale with the text.

## The basics

- `character_spacing` — extra spacing between characters, in **inches**.
  Fallback when unset: `1.5 × the resolved cutter diameter` (so it scales
  with the tool automatically; an explicit inch value does not).
- `space_width_fraction` (fallback `0.3`) — a space advances
  `space_width_fraction * text_height + character_spacing` (PLT-extracted
  fonts).
- `min_glyph_width_fraction` — floors the per-glyph advance (fraction of cap
  height) so a zero-width glyph still reserves real space; clamps the
  profile-envelope kerning.

## Automatic kerning (PLT-extracted fonts)

Kerning is measured from each glyph's height **profile envelope**: for each
sample column the pair's silhouettes are compared inside a window, and the
worst detected penetration closes the advance.

- `kerning_window_fraction` (`0–1`, fraction of text height) — how far left/
  right the envelopes are compared: `±(fraction/2)`.
- `kerning_penetration_scale` (fallback `1.0`) — multiplier on the detected
  penetration: `>1` over-kerns tight pairs proportionally, `<1` under-kerns,
  `0.0` ignores detected closeness entirely.
- `kerning_recession_scale` — multiplier on a *recessed* (negative)
  penetration: `1.0` keeps the historical linear behavior (where lowering
  `kerning_penetration_scale` also pulled already-gapped pairs closer),
  `0.0` makes a recessed pair contribute nothing (gaps stay open when you
  loosen tight pairs).
- `kerning_min_gap_fraction` (fallback `0.0`) — extra air added to EVERY
  kerned pair's advance, on top of the cutter diameter and
  `character_spacing`.
- `fallback_advance_fraction` (fallback `1.0`) — multiplier on the
  bounding-box fallback advance used for pairs with no overlapping height
  (or no envelopes).

## Manual kerning markers

`kern_pos_string` / `kern_neg_string` are literal marker strings (default
unset = disabled). Every occurrence in a text line is **stripped** (it never
engraves) and the FOLLOWING character's cursor shifts by
`kern_width_fraction` of the cap height — forward for the positive marker,
back for the negative.

```yaml
job:
  job_name: "Hand-kerned"
  kern_pos_string: "<->"
  kern_neg_string: "<-<"
  kern_width_fraction: 0.08
  content:
    - text: "T<->O   W<-<A"
```

Constraints: each marker must be non-empty, contain no newline, and not be
markup-shaped — a letters-only body like `<s>` would be consumed by the tag
scan, so the schema rejects it. Multi-character markers are legal. Works on
PLT and TTF fonts.

## When to reach for what

| Symptom | Field |
|---|---|
| Everything reads tight/loose | `character_spacing` |
| Specific pairs touch | `kerning_penetration_scale` (down), `kerning_min_gap_fraction` (up) |
| Loosening tight pairs also opens gapped pairs | `kerning_recession_scale: 0.0` |
| One exact pair needs a nudge | manual kerning markers |
| Spaces too wide/narrow | `space_width_fraction` |
