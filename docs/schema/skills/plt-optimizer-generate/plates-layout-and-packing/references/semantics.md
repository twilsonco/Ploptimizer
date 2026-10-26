# Plates, Layout and Bin-Packing — semantics

## Two plate modes

- **Declared plates.** `plates:` lists each sheet's `id`, `width`, `height`
  (the **usable pack area**), plus per-plate overrides. Labels pack onto
  them exactly; a label that fits no plate fails the job.
- **Unbounded mode (no `plates`).** The packer auto-allocates
  `default_plate_{i}` sheets at the config's `plate_width`/`plate_height`
  (fallback 24×16), overflowing onto as many sheets as needed. In this mode
  the config gate makes `plate_width`/`plate_height` required from
  config-or-spec.

## Clearances and origin

- Plate origin is the **material top-left, +y down**.
- `left_clearance` / `top_clearance` (plate → job → config, default `0.0`)
  shift the usable area right/down within the sheet — keep-out strips for
  clamps or a squashed edge, not per-label margins.

## Layout mode

- `layout: columns` (default) fills each plate's **height** before extending
  rightward — the scrap-friendly single rectangular block.
- `layout: rows` is the historical width-first fill.
- Cascade job → config, with a **per-plate override** (`plate → job →
  config`): mixed modes split packing into sequential same-mode groups.

## Rotation

`allow_rotation` (job → config, default `true`): a label rotates 90° CW only
when it **strictly improves** used area; ties keep all-horizontal. Set
`false` when grain, hinge edges, or reading direction matter (uses more
plate space).

## Material grouping

`material` (label → job; plate → job; default `null` = unset) is a free-form
stock name that groups labels into **independent packing passes**: labels
sharing a material pack together, and a plate carries exactly one material
(matching is trim + casefold; the first-declared spelling wins for display).
Material-less plates spread across the material groups; material-less jobs
pack in one pass. Unbounded mode allocates one auto-bin pool per material
(`<material>_default_plate_{i}`). Not a `job-config.json` key.

```yaml
job:
  job_name: "Two stocks"
  width: 3.0
  height: 1.0
  plates:
    - id: alu
      width: 48.0
      height: 96.0
      material: "0.05 aluminum"
    - id: acrylic
      width: 24.0
      height: 16.0
      material: "1/8 acrylic"
      left_clearance: 1.0
  labels:
    - id: a_tag
      material: "0.05 aluminum"
      count: 12
      content:
        - text: "ALU TAG"
    - id: b_tag
      material: "1/8 acrylic"
      content:
        - text: "ACRYLIC TAG"
```

## Plate pinning

`plate_id` on a label pins it to that plate. A plate fed by a
plate-level `replacement_text_file` accepts **only** its pinned (synthesized)
labels. Static labels pack normally onto the unpinned plates.

## Rapid travel inside the plate

`text_chunk_mode` (job → config, default `line`) sets the travel-optimization
granularity for plate-space text: `line` routes each text line as one node;
`word` routes each whitespace-delimited word separately (shorter rapid
travel, more chunk boundaries). The per-chunk path order is TSP-optimized by
the same engine the `optimize` CLI uses; `--fast-mode` trades travel quality
for speed (see the running-and-troubleshooting skill).

## Reading the result

The layout report (run log) shows every label's plate, position, rotation,
and the per-plate fill. PLT files are per (plate, material, cutter, layer):
the stem is `[<plate>_][<material>_]<cutter>[_<color>]_<kind>_<job_id>.plt`
where `kind` is `txt` (the text layer) or `bh` (borders + holes, engraved
with the boundary/hole tool). Each file takes one tool on the engraver;
every PLT also gets a matching proof PDF.
