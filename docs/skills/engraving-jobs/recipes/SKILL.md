---
name: recipes
description: 'Complete runnable job specs to copy and adapt. Use when: starting a job from a known-good shape; reading a full spec end to end. Keywords: example, recipe, template, yaml, copy.'
version: 1.0.0
metadata:
  hermes:
    category: engraving-jobs
    tags: [plt, engraving, toolpath, generate]
---

# Recipes — Complete Example Job Specs

## When to use

- Starting a new job: pick the closest shape below, copy its YAML, adapt it.
- Learning how the fields combine in a real, validated spec.

Every example below is copied verbatim from `examples/job_specs/` in the tool repository and re-validated through `parse_yaml` on every regeneration. The files live in `examples/` beside this skill. Replacement data files (where a recipe uses one) live beside the ORIGINAL spec and must be copied with it — paths resolve relative to the YAML file's directory.

## Multi-line label, auto-sized

A plain multi-line label — the baseline shape every spec grows from.

File: [`examples/S.yaml`](examples/S.yaml)

```yaml
# UV White text on black background

job:
  job_name: Test S
  width: 2
  height: 1
  text_height: 0.5
  font: dino

  labels:
    - id: a
      content:
        - text: "S"
```

## Compressed long text

A `max_h_compress` budget squeezing a long line into the label width.

File: [`examples/S-compressed.yaml`](examples/S-compressed.yaml)

```yaml
# UV White text on black background

job:
  job_name: Test S compressed
  width: 0.5
  height: 1
  text_height: 0.5
  font: dino

  labels:
    - id: a
      content:
        - text: "S"
```

## Bullet and enumerated lists

`itemize` / `enumerate` blocks with counters and hanging indent.

File: [`examples/lists_demo.yaml`](examples/lists_demo.yaml)

```yaml
# Bullet & enumerated lists -- feature demo
#
# A `content` entry carries exactly one of `text`, `itemize` or `enumerate`.
# A list block nests its own settings plus one or more text `items`; it expands
# at resolution into one rendered line per item. Every item engraves its marker
# (the bullet string, or the computed counter) in ONE flush marker column
# shared by the whole list, and the item's text -- plus every continuation line
# `optimize_line_content` grows it to -- starts at the hanging-indent column
# right of the markers.
#
# The list settings cascade item -> list block -> label -> job ->
# job-config.json, so a shop default lives in job-config.json and a label (or
# a single item) overrides it.
job:
  job_name: List Showcase
  width: 7
  height: 5
  margin: 0.25
  text_height: 0.28
  font: dino
  h_compress_global: true
  max_h_compress: 0.4
  labels:
    - id: instructions
      content:
        - text: "<b>STARTUP PROCEDURE</b>"
          text_height: 0.45
          text_h_alignment: center
        # An enumerated list: "Step 1)", "Step 2)", ... The block enables the
        # reflow for every item, so an over-wide item grows into a
        # hanging-indent block (the counter rides the FIRST line only).
        - enumerate:
            counter_prefix: "Step "
            counter_suffix: ")"
            optimize_line_content: true
            items:
              - text: "Isolate power at the disconnect and lock it out"
              - text: "Verify zero energy at the terminals with a rated meter before touching any conductor"
              - text: "Restore power only after the guard is refitted"
        # A bullet list with a per-item override: the block uses the default
        # bullet, the last item swaps in a dagger. The list is one aligned
        # block, so both markers share the column of the widest one.
        - itemize:
            text_h_alignment: left
            items:
              - text: "Wear rated eye protection"
              - text: "Keep the work area dry"
              - text: "Two-person job above 600V"
                bullet_string: "†"
        - text: "<s>Provisional revision</s> Rev C"
          text_height: 0.22

    - id: lower_alpha
      width: 5
      height: 3
      content:
        - text: "CAUTION"
          text_height: 0.4
        # The counter style, the start index, and the marker gap are all
        # knobs: "b)", "c)", "d)" with a tight 0.15 cap-height gap.
        - enumerate:
            counter_style: lower-alpha
            counter_start_index: 2
            counter_suffix: ")"
            list_marker_gap_fraction: 1.5
            items:
              - text: "Pinch point"
              - text: "Hot surface"
              - text: "Stored energy"
```

## Batch from a replacement file

A job-level `replacement_text_file` batch. Its data file lives beside the spec in `examples/job_specs/` — copy both together; paths resolve relative to the YAML.

File: [`examples/replacement_job.yaml`](examples/replacement_job.yaml)

```yaml
# Replacement text file demo job for the PLT-Optimizer generate pipeline.
#
# Demonstrates EngraveLab / Vision Pro "badge" (a.k.a. "multiples") style
# data-driven label generation: a label template combined with a
# replacement text file, where each file line produces one label copy.
#
# Rules:
#   - `replacement_text_file` is resolved relative to this YAML file
#     (absolute paths also accepted).
#   - The file's data-line count determines the label count; `count` must
#     NOT be combined with `replacement_text_file`.
#   - `replacement_text_delimiter` is a single special/whitespace
#     character (default ";").
#   - Replacement data files may carry comments: a line whose
#     whitespace-stripped text starts with
#     `replacement_text_file_comment_string` (default "# ", cascades
#     job -> plate -> label) produces no copy, and blank lines are
#     dropped too. Set "" to disable comments: every non-blank line is
#     then raw data. Dropped lines are reported in one WARNING per file.
#   - The delimiter and the comment marker cascade job -> plate -> label,
#     so declaring them once at the job level (as below) applies them to
#     every label file in the job -- no job-level `replacement_text_file`
#     is needed. A label (or plate) may declare its own value to override.
#   - `content` is optional: when present, its `text` values are
#     placeholders declaring per-line attributes; when omitted, every
#     rendered line inherits the label-level attributes. Mark a line
#     `placeholder: true` to make ONLY that line receive a file item
#     (constant lines copy verbatim to every copy).
#   - Fewer items on a file line -> fewer rendered lines (block stays
#     vertically centered). More items -> extra lines inherit label attrs.
#
# Run with:
#   uv run plt-optimizer generate examples/job_specs/replacement_job.yaml -o out.plt
# or:
#   uv run python scripts/run_integration_test.py examples/job_specs/replacement_job.yaml
job:
  job_name: "Replacement Text Demo - Field Tags"

  text_height: 0.3
  character_spacing: 0.04
  line_spacing: 0.25
  margin: 0.15

  # Job-level replacement settings: declared once, cascaded to every label
  # file below (the job itself has NO replacement_text_file). Both values
  # equal the code defaults, so existing copies are unchanged.
  replacement_text_delimiter: ";"
  replacement_text_file_comment_string: "# "

  # -------------------------------------------------------------------
  # 1. Template WITH placeholder content: the two `content` lines
  #    declare per-line attributes (sizes, alignment). Each line of
  #    examples/job_specs/replacement_text_sample.txt replaces them in order.
  #    The file has 5 lines -> 5 label copies (badge_0000 .. badge_0004).
  #    Every file line carries 3 items, so each copy renders 3 lines:
  #    the first two use the placeholder attributes, the third is an
  #    "extra" line inheriting the label-level attributes.
  # -------------------------------------------------------------------
  labels:
    - id: badge
      width: 3.5
      height: 2.0
      text_height: 0.35
      replacement_text_file: replacement_text_sample.txt
      content:
        - text: "PLACEHOLDER LINE 1"
          text_height: 0.45
        - text: "PLACEHOLDER LINE 2"
          text_h_alignment: center

    # -----------------------------------------------------------------
    # 2. Template WITHOUT content: every rendered line inherits the
    #    label-level attributes. examples/job_specs/replacement_text_assets.txt
    #    has 4 lines; the first three carry two items (two rendered
    #    lines), the last carries one item (one rendered line, still
    #    vertically centered).
    # -----------------------------------------------------------------
    - id: asset
      width: 3.0
      height: 1.5
      text_height: 0.3
      replacement_text_file: replacement_text_assets.txt
      holes:
        - location: top-left
        - location: top-right

    # -----------------------------------------------------------------
    # 3. A fully static label, mixed with the templates in the same
    #    job: replacement-driven and traditional labels coexist freely.
    # -----------------------------------------------------------------
    - id: static_header
      count: 2
      width: 6.0
      height: 1.0
      text_height: 0.5
      content:
        - text: "FIELD TAGS"

    # -----------------------------------------------------------------
    # 4. SELECTIVE placeholders driven by the JOB-level cascade: this
    #    label declares only replacement_text_file -- no delimiter, no
    #    comment marker. It inherits both from the job block above, so
    #    examples/job_specs/replacement_text_crew.txt is split on ";" and
    #    its "# " comment lines are dropped. The `placeholder: true` line
    #    is the ONLY line that receives a file item (the constant line
    #    copies verbatim to every copy); the second item lands on the
    #    appended extra line, which inherits the label-level attributes.
    #    The file has 3 data lines -> crew_0000 .. crew_0002.
    # -----------------------------------------------------------------
    - id: crew
      width: 3.0
      height: 1.5
      text_height: 0.3
      replacement_text_file: replacement_text_crew.txt
      content:
        - text: "CREW"
        - text: "NAME"
          placeholder: true
          text_h_alignment: center
```

## Round labels

Circular label shapes with content fitted inside.

File: [`examples/round_labels_demo.yaml`](examples/round_labels_demo.yaml)

```yaml
# Circular / elliptical labels (`type: round`).
#
# A round label's boundary is the ellipse with semi-axes width/2 and height/2
# -- a circle when the two are equal. Declaring only ONE of width/height gives
# a circle of that diameter. `rect` (the default) is the historical rectangle.
#
# Round labels pack through the adjacent ellipse-packing pipeline (shapely),
# which nests them closer than their bounding boxes allow; every rectangular
# label in the same job keeps packing through rectpack untouched. The text
# margin applies radially (content stays inside the inset ellipse), drill
# holes land on the ellipse, and all existing label features -- holes, borders,
# collision avoidance, HTML markup -- work on round labels.
job:
  job_name: Round Labels Demo

  text_height: 0.5
  margin: 0.15
  font: dino

  plates:
    - id: sheet
      width: 24.0
      height: 16.0

  labels:
    # A 3in circle. The radial margin leaves a ~1.9in inscribed content box
    # (a 3in circle inset 0.15in, inscribed square), so the text height is
    # sized to fit it at natural width.
    - id: asset_tag
      type: round
      width: 3.0
      height: 3.0
      text_height: 0.22
      holes:
        - location: top
      content:
        - text: "ASSET 4471"

    # An ellipse: width != height.
    - id: oval_plate
      type: round
      width: 5.0
      height: 2.0
      text_height: 0.25
      line_spacing: 0.1
      holes:
        - location: sides
      content:
        - text: "COMPRESSOR"
        - text: "UNIT 12"

    # A circle declared by diameter alone (height fills to match width).
    - id: serial_disc
      type: round
      width: 2.25
      text_height: 0.18
      content:
        - text: "SN 00912"

    # Repeated circles: the ellipse packer nests them in one column.
    - id: port_tag
      type: round
      width: 1.75
      height: 1.75
      count: 6
      text_height: 0.15
      content:
        - text: "PORT A"

    # A rectangular label in the same job: the rectpack path, unchanged.
    - id: rect_note
      width: 5.0
      height: 2.0
      text_height: 0.25
      content:
        - text: "RECTANGULAR CONTROL"
```

## Rounded-rectangle labels

`rounded_rectangle_fraction` corner rounding on rect labels.

File: [`examples/rounded_rect_demo.yaml`](examples/rounded_rect_demo.yaml)

```yaml
# Rounded-rectangle label borders (`rounded_rectangle_fraction`).
#
# The rectangular border's corners become superellipse-quadrant patches with
# exponent n = 2 / fraction, so the curvature varies CONTINUOUSLY along the
# whole boundary -- no circular-fillet curvature jump where the rounding
# begins and ends. fraction 0.0 (the default) is the sharp rectangle.
#
# The rounding spans `fraction` of the SHORTER edge: a 4x8 label at 0.5
# rounds 2in (50%) of the 4in sides, starting 1in from each corner. With
# `rounded_rectangle_proportional: true` the fraction applies to width and
# height separately (50% of the 8in side rounds too).
#
# The endpoints are exact: fraction 1.0 with proportional true (or on a
# square) engraves the exact ellipse/circle of `type: round`; fraction 1.0
# with proportional false on a non-square fully rounds the short edges and
# keeps straight sections on the long edges (a smoothed stadium).
#
# Corner drill holes ride the rounded border (inset along the curve normal),
# the text margin applies to the engraved rounded boundary, and the labels
# keep packing through rectpack in their exact nominal box.
job:
  job_name: Rounded Rect Demo

  text_height: 0.5
  margin: 0.15
  font: dino

  plates:
    - id: sheet
      width: 24.0
      height: 16.0

  labels:
    # Equal-distance rounding: 1in of rounding on every side (50% of the
    # short 2in half-edges), n = 4 squircle corners.
    - id: machine_tag
      width: 6.0
      height: 2.0
      rounded_rectangle_fraction: 0.5
      content:
        - text: "COMPRESSOR 12"

    # The same label fully rounded, non-proportional: the short edges round
    # off completely and the long edges keep straight sections.
    - id: long_stripe
      width: 8.0
      height: 2.0
      rounded_rectangle_fraction: 1.0
      content:
        - text: "WARNING"

    # Proportional rounding: the fraction applies to each dimension, so a
    # full 1.0 engraves the exact ellipse of the label box.
    - id: oval_tag
      width: 5.0
      height: 2.5
      rounded_rectangle_fraction: 1.0
      rounded_rectangle_proportional: true
      content:
        - text: "OVAL"

    # A full-round square: the exact circle, corner holes riding the curve.
    - id: round_tag
      width: 3.0
      height: 3.0
      rounded_rectangle_fraction: 1.0
      text_height: 0.22
      hole_margin: 0.15
      holes:
        - location: corners
      content:
        - text: "ASSET 88"

    # A gentle rounding with corner holes: the holes follow the corner
    # patches, never the cut-away sharp corners.
    - id: corner_drills
      width: 4.0
      height: 4.0
      rounded_rectangle_fraction: 0.35
      hole_margin: 0.15
      holes:
        - location: corners
      content:
        - text: "DRILL"

    # The sharp control: fraction 0.0 (the default) stays the historical
    # rectangle.
    - id: sharp_control
      width: 6.0
      height: 2.0
      content:
        - text: "SHARP EDGE"
```

## Descender relief showcase

Lines with g/j/p/q/y beside caps — the per-glyph relief in action.

File: [`examples/descender_adjust_demo.yaml`](examples/descender_adjust_demo.yaml)

```yaml
# Demo: automatic descending-character relief
# (`auto_descending_character_adjustment`, default true).
#
# Each label stacks a line with DESCENDERS (p y g) directly over a line of tall
# CAPITALS with a tight line_spacing, so the descenders come close to colliding
# with the capitals below.
#
#   - *_relief  (default, relief ON): the individual descending glyphs that
#     come too close are vertically downscaled (about their own baseline) and
#     raised, so the engraved strokes clear. The relief is per glyph -- only the
#     glyphs with a close partner below move.
#   - *_raw     (relief OFF): the same content with the mechanism disabled, so
#     the descenders hang exactly as baseline stacking leaves them and bleed
#     into the capitals below.
#
# Compare the pairs in the simple-outline PDFs (pdf/*.pdf) and the color-coded
# rapid-travel plots (pdf/*_default.pdf, written with --default-plots).
#
#   uv run plt-optimizer generate \
#     examples/job_specs/descender_adjust_demo.yaml \
#     -o test_output/descender_adjust_demo --default-plots
job:
  job_name: Descender Relief Demo
  width: 6.0
  height: 3.0
  margin: 0.2
  text_height: 1.0
  line_spacing: 0.1
  font: dino

  plates:
    - id: plate_1
      width: 30.0
      height: 16.0

  labels:
    # 1. Relief ON (the shipped default): the 'p' and 'y' are relieved.
    - id: typical_relief
      content:
        - text: "Typical"
        - text: "HEIGHT"

    # 2. Relief OFF: same content, descenders bleed into the capitals below.
    - id: typical_raw
      auto_descending_character_adjustment: false
      content:
        - text: "Typical"
        - text: "HEIGHT"

    # 3. Relief ON: 'p' over "RANGE".
    - id: depth_relief
      content:
        - text: "Depth"
        - text: "RANGE"

    # 4. Relief OFF: 'p' over "RANGE" (un-relieved twin of label 3).
    - id: depth_raw
      auto_descending_character_adjustment: false
      content:
        - text: "Depth"
        - text: "RANGE"
```

## Production multi-label job

A real shop job: multiple labels, per-label content and sizing. Pair it with its sibling data files in `examples/job_specs/` when copying.

File: [`examples/2026-10-06-seer.yaml`](examples/2026-10-06-seer.yaml)

```yaml
# 2026-10-06
# SEER 2963 Equip. Labels
job:
  job_name: SEER 2963
  font: dino
  material: bw
  
  labels:
    - id: case tags
      width: 3
      height: 1
      text_height: 0.75
      replacement_text_file: 2026-10-06 seer 1x3.txt
      holes:
        - location: sides
    - id: alarm placard
      width: 10
      height: 2
      text_height: 0.5
      content:
        - text: "REFRIGERATION ALARM"
        - text: "CONTACT STORE MANAGER"
    - id: equipment labels
      count: 6
      width: 4
      height: 2
      text_height: 1.25
      holes:
        - location: sides
      content:
        - text: "R507"
    - id: 8x8 placard
      material: bw(uv)
      width: 8
      height: 8
      text_height: 1
      h_margin: 0.5
      holes:
        - location: sides
      content:
        - text: "AHU1"
        - text: "MFG. 01/2015"
        - text: "Ckt 1: 68lbs. R407C"
        - text: "Ckt 2: 64lbs. R407C"
        - text: "Ckt 3: 90lbs. R407C"
        - text: "Ckt 4: 95lbs. R407C"
    - id: 7x5.33 placards
      material: bw(uv)
      width: 7
      height: 4
      text_height: 1
      replacement_text_file: 2026-10-06 seer 7x5.33.txt
      holes:
        - location: sides
      content:
        - text: "AHU2"
          placeholder: true
        - text: "Mfg. 04/2013"
        - text: "Ckt 1: 70lbs. R407C"
          placeholder: true
```

## Read next

- `references/semantics.md` — choosing and adapting a recipe.
- [`../generate-engraving-job/SKILL.md`](../generate-engraving-job/SKILL.md) — the main workflow skill.
- [`../engraving-job-complete-reference/references/JOB_SPEC.md`](../engraving-job-complete-reference/references/JOB_SPEC.md) — exhaustive field reference.
