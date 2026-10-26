# Multi-Stroke (Bold) Fonts — semantics

Some PLT catalog fonts are **multi-stroke**: each glyph's geometry carries
N > 1 parallel cutter passes (the `_S<n>` sample-sheet suffix, recorded as
`stroke_count` in the font's `fonts/VP-EL-fonts/catalog/` file). Engraving
all passes with the right tool produces a genuinely bold stroke.

## The boldness ladder

For a multi-stroke font, the automatic cutter is derived from the target
stroke width, not from the plain height ladder:

1. the engraved passes must total `text_height × target_boldness_factor` of
   width (default `0.14`, bounds `0.05–0.30`),
2. `multistroke_overlap_ratio` (default `0.25`, bounds `0.1–0.5`) is the
   gap-free floor: the cutter is never smaller than
   `pass_spacing / (1 - ratio)`, so adjacent passes ALWAYS overlap and no
   unengraved stripe appears between them,
3. the result snaps to the `tools.json` inventory (stepping UP when a
   snap-down would break the overlap floor),
4. the implicit pass overlap is derived and logged.

The stored glyph geometry is emitted verbatim (the engraved sheet is the
source of truth for the bold shape), so the stroke count drives **cutter
sizing only** — kerning, collision, descender relief, the pen map and the
per-cutter files all read the per-line `cutter_diameter` and pick the result
up.

## Inertness

All three fields are inert on:

- single-stroke PLT fonts,
- TrueType fonts,
- any line with an explicit `cutter_size` (which bypasses the ladder).

Setting `target_boldness_factor` on a TTF job does nothing — that is by
design, not a bug.

## Inline `<b>`

`<b>…</b>` on a multi-stroke font re-runs the ladder at
`target_boldness_factor × bold_boldness_multiplier` (default `1.5`, clamped
to `0.30` with a WARNING) for the run. On single-stroke PLT and TTF fonts,
`<b>` instead steps to the **next larger inventory cutter** and the
multiplier is inert. The bold run engraves in its own per-cutter PLT file.

## Cascade

`target_boldness_factor`, `multistroke_overlap_ratio` and
`bold_boldness_multiplier` cascade line → label → job → config (plates are
parity-only); `job-config.json` supplies the shop defaults.

```yaml
job:
  job_name: "Heavy plate"
  font: "Claret4"   # a multi-stroke catalog font (stroke_count 4)
  target_boldness_factor: 0.18
  content:
    - text: "HEAVY PLATE"
    - text: "serial <b>0042</b>"
```
