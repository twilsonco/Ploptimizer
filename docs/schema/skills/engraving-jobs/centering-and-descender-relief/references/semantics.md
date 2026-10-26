# Centering and Descending-Character Relief — semantics

## Horizontal alignment

`text_h_alignment` (`left` / `center` / `right`; cascade line → label → job →
config, fallback `center`) aligns each text line within the label's usable
width. A list block aligns as ONE unit (its marker column stays flush under
`center`). `adjust_h_center_for_single_side_hole` (cascade label → job →
config) shifts the centering when a label has a hole on ONE side only, so
the text optically centers in the free space rather than the full width.

`v_center_chars` is the per-line set of characters that define the vertical
center band for the inline `<vc>` tag (see the inline-markup skill).

## Vertical centering: band vs ink

How a label centers its stacked block vertically
(`v_center_by_content`, cascade label → job → config, never lines; plate
parity; fallback `false`):

- **`false` (default) — band centering.** The baseline-to-capline BAND is
  centered, so the visible glyphs sit in the label's optical middle and
  descenders hang into the bottom margin (where the relief clears them
  against the border). The vertical margin clamp follows the same model: a
  band-centered label sizes its line spacing against the cap band, so
  descenders no longer shrink the spacing.
- **`true` — ink centering.** The block centers by its RENDERED INK,
  charging the deepest descender to the label: a line carrying descenders
  rides high and its plain letters look visibly off-centre. `true` restores
  this historical behavior bit-identically.
- **Dependency:** band centering is active ONLY when
  `auto_descending_character_adjustment` is enabled. With the relief off
  nothing would adjust a descender, so ink centering is kept and
  `v_center_by_content` is inert.

## Descending-character relief (`auto_descending_character_adjustment`, default true)

Baseline spacing lets a descender hang into the gap below its line. When a
glyph on the FOLLOWING line is close enough that the two engraved strokes
would bleed together, the relief adjusts that one descending glyph:

- **Trigger — true 2D swept-ink distance:** a circle of the required
  diameter rolled along the descending glyph's sub-baseline ink touching any
  part of any glyph of the line below, i.e. a gap under
  `0.5 * (text_cutter + hole_cutter_diameter) * (1 + descending_character_clearance)`.
  Deliberately NOT a bounding-box comparison: a box model calls X-disjoint
  boxes "infinitely far apart" (hiding a `g` hook reaching into the crook of
  the `i` below) and fires on box overlaps whose real ink is nowhere near.
- **The descending glyph's ramp:** it is vertically downscaled about its own
  baseline (down to `descending_character_v_scale`, default `0.5`) and
  raised (by up to `descending_character_raise` × its own descender depth —
  `1.0` lifts its lowest point exactly to the baseline).
- **The reciprocal half:** the colliding glyphs on the FOLLOWING line share
  the deficit through the same ramp, each scaled **top-down** about its own
  lowest ink point (its lowest point never moves; its top descends toward
  it), capped by `descending_character_reciprocal_scale` (default `0.5`;
  `1.0` forbids the reciprocal half and reproduces the historical one-sided
  relief bit-identically).
- A glyph on a middle line can carry BOTH roles at once: its own descending
  ramp into the gap below, and a top-down ramp from the gap above.
- **Per glyph, never blanket:** the relief adjusts the colliding descending
  glyph only, and never compares against the following line's cap line in
  the abstract.

### The bottom border counts as a virtual "following line"

The border is measured the same way: it is drawn at the label edge and cut
by the boundary/hole tool, so a descender collides when its ink comes within
`0.5 * (text_cutter + hole_cutter_diameter) * (1 + descending_character_clearance)`
of the border centreline. Crossing `v_margin` is NOT a collision — ink may
spill into the margin; only the border stroke counts. No extra fields.

### When the allowances run out

- **Text–text pair still too close:** the gap below the line grows (capped
  by the vertical margins); a collision that neither the allowances nor the
  admissible spacing can clear ABORTS the job (`LabelRenderError`, non-zero
  exit).
- **Border collision: never aborts.** Growing the spacing above the last
  line moves that line DOWN toward the border, so the per-glyph ramp is the
  only remedy; a spent ramp is reported as a WARNING and the job engraves
  the best the allowances allow.

The relief runs at render time in BOTH stacking modes. Arcs on a relieved
glyph are flattened (bounded chord error, DEBUG). Every adjustment is
reported per glyph in the layout report. Cascade: label → job → config
(never text lines — the mechanism compares a line's glyphs against the
following line's); plates are parity-only. `false` engraves descenders
exactly as rendered.
