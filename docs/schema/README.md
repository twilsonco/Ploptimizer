# Job Spec Reference for AI Assistants

Hand-written companion to the generated artifacts in this directory. Read
this file **plus** [`JOB_SPEC.md`](JOB_SPEC.md) (field tables) to author or
review job YAML; use [`job_spec.schema.json`](job_spec.schema.json) for
machine validation.

| Artifact | Source of truth | Regenerate? |
|---|---|---|
| `job_spec.schema.json` | `JobSpec` model (`plt_optimizer/generate/schema.py`) | `uv run python docs/schema/generate_ai_docs.py` |
| `job_config.schema.json` | `JobDefaults` model (`plt_optimizer/generate/job_config.py`) | same command |
| `JOB_SPEC.md` | both models + fallback constants scraped from `resolution.py` / `layout.py` + tool_options from `job-config.json` | same command |
| `README.md` (this file) | hand-written semantics | manual |

Drift is pinned by `tests/test_job_spec_docs.py`: if `schema.py` /
`job_config.py` change without regenerating the artifacts, the test suite
fails. The tool_options table in `JOB_SPEC.md` is auto-generated from the
live `job-config.json`, so it always reflects the current available engraver
parameters. All units are **inches**.

## Pipeline context

```
spec.yaml ──parse_yaml()──▶ JobSpec ──expand_job_spec()──▶ JobSpec ──resolve_job_spec()──▶ ResolvedLabel[] ──▶ layout / render / export
              (+ job-config.json injection & required-field gate)   (replacement-file expansion)   (cascade resolution, cutter sizing)
```

CLI entry: `plt-optimizer generate <spec.yaml>` (flags: `--job-config`,
`--tools`, `--fast-mode`, `--no-plots`, ...). Validation entry point for
tooling: `plt_optimizer.generate.schema.parse_yaml(path)` → validated
`JobSpec` or `ValueError` (`JobConfigError` subclass for config-gate
violations).

## Tool Options (HPGL Header Commands)

The `tool_options` field lets you specify engraver parameters (cutting
velocity, spindle speed, dwell time, etc.) as HPGL header commands prepended
to generated PLT files. **See the "Available Tool Options" section in
`JOB_SPEC.md`** for the full table of parameters, their HPGL commands, units,
ranges, and defaults.

Typical usage:

```yaml
job:
  job_name: "Custom Speeds"
  tool_options:
    cutting_velocity: 1.5      # override default 0.8 in/sec
    spindle_speed: 15000       # override default 12000 rpm
  # ... rest of job spec ...
```

Defaults come from `job-config.json` with **dual values** for text vs
borders/holes layers (each layer may need different settings). Out-of-bounds
values are automatically clamped to their configured range with a WARNING
logged. See `plt_optimizer/generate/tool_options.py` for the clamping logic.

## The five job forms (exactly one label source)

A valid job defines **exactly one** label source. Mixing forms is a
validation error.

### 1. Explicit `labels` list

```yaml
job:
  job_name: "Batch 01"
  text_height: 0.5
  labels:
    - id: label_1
      count: 10
      content:
        - text: "Line 1"
        - text: "Line 2"
```

### 2. Root-level single label (`content` + optional `count`)

```yaml
job:
  job_name: "Simple Labels"
  count: 20
  content:
    - text: "Single repeating label"
```

### 3. Label-level replacement file (per-label template)

Each line of the file produces one label copy (`badge_0000`, `badge_0001`,
...). `count` must not be set; `content` is an optional per-line attribute
template (see "Replacement file semantics" below).

```yaml
job:
  job_name: "Batch 02"
  labels:
    - id: badge
      replacement_text_file: data.txt      # relative to the YAML's directory
      replacement_text_delimiter: ";"      # optional, default ";"
      content:
        - text: "PLACEHOLDER L1"           # declares line-1 attributes only
          text_height: 0.45
```

### 4. Job-level replacement file (replaces `labels` entirely)

Job-level `width`, `height`, `text_height` are **required** (the synthesized
labels carry no label-level dimensions). `labels` and `count` are rejected;
root `content` is the shared attribute template. Ids: `label_0000`, ...

```yaml
job:
  job_name: "Batch 03"
  width: 3.0
  height: 1.0
  text_height: 0.75
  holes:
    - location: sides
  replacement_text_file: data.txt
```

### 5. Plate-level replacement files (labels pinned to one plate)

Each plate's file lines synthesize labels **pinned to that plate**
(`<plate_id>_0000`, ...); pinned labels pack exclusively onto their plate,
which then accepts no other labels. Job-level `width`/`height`/`text_height`
are required whenever any plate declares a file. Mutually exclusive with a
job-level file. Static `labels` may coexist and pack normally onto
unpinned plates.

```yaml
job:
  job_name: "Batch 04"
  width: 3.0
  height: 1.0
  text_height: 0.75
  plates:
    - id: scrap_a
      width: 24.0
      height: 16.0
      replacement_text_file: data_a.txt
    - id: full_b
      width: 24.0
      height: 16.0
  labels:
    - id: header
      content:
        - text: "HEADER"
```

A minimal real-world example (job-level file, form 4):
[`examples/job_specs/2026-7-22 sunwest.yaml`](../../examples/job_specs/2026-7-22%20sunwest.yaml).

## Replacement file semantics (forms 3–5)

- File format is **pure data**: every line is one label copy; items within a
  line split on `replacement_text_delimiter` (single special/whitespace char,
  default `;`; must not appear in the data). CRLF/CR normalized; one trailing
  newline does not create a phantom copy; UTF-8 BOM stripped; items used
  verbatim (no whitespace stripping). Blank lines render blank (WARNING).
- **Fewer items than template lines** → the copy renders only as many lines
  as items (block stays vertically centered). **More items** → extra lines
  inherit label-level attributes.
- Without `content`, every rendered line inherits label-level attributes.
- Expansion (`expand_job_spec`) runs between `parse_yaml` and
  `resolve_job_spec`, flattening each file line into a static
  `LabelSpec(count=1)` with a `_NNNN` id suffix.

## Cascade rules (how `null` resolves)

`null`/omitted always means "inherit"; an explicit value (including `0.0`)
always wins at its level. Precedence per attribute:

| Attribute | Cascade order | Fallback |
|---|---|---|
| `text_height`, `character_spacing`, `line_spacing`, `max_h_compress`, `text_h_alignment` | line → label → job → config | see fallback table in `JOB_SPEC.md` |
| `cutter_size` | line → label → job (no config tier) | `null` = auto-select from `text_height` |
| `target_boldness_factor`, `multistroke_overlap_ratio` | line → label → job → config (plate parity) | `0.14` / `0.25` (multi-stroke PLT fonts only) |
| `bold_boldness_multiplier` | line → label → job → config (plate parity) | `1.5` (`<b>` markup on multi-stroke PLT fonts only) |
| `cutter_downsize`, `max_cutter_downsizes`, `cutter_downsize_global` | line → label → job → config | `true` / `1` / `true` |
| `h_compress_global` | line → label → job → config | `false` |
| `enforce_cap_line` | line → label → job → config (plate parity) | `true` |
| `use_baseline_spacing` | label → job → config (never lines; plate parity) | `true` |
| `v_center_by_content` | label → job → config (never lines; plate parity) | `false` |
| `auto_descending_character_adjustment`, `descending_character_v_scale`, `descending_character_raise`, `descending_character_clearance`, `descending_character_reciprocal_scale` | label → job → config (never lines; plate parity) | `true` / `0.5` / `0.7` / `0.0` / `0.5` |
| `text_color` | line → label (never job/plate/config) | `none` (implicit) |
| `new_line` | line only (never label/job/plate/config) | `false` |
| `width`, `height` | label → job → auto-size from rendered content | — |
| `margin` | label → job → config | 0.125 |
| `h_margin`, `v_margin` | label → job → config | `margin` (then 0.125) |
| `hole_margin` | label → job → config | 0.1875 |
| `min_hole_margin`, `hole_text_collision_distance` | label → job → config | see fallback table |
| `hole_adjust_for_cutter` | label → job → config (never lines; plate parity) | `true` |
| `holes` | label (replaces job list entirely; `[]` suppresses) → job → config | none |
| `layout`, `allow_rotation`, `text_chunk_mode` | job → config | `columns` / `true` / `line` |
| `layout` (per plate) | plate → job → config | — |
| `material` | label → job; plate → job | `null` (unset) |
| `left_clearance`, `top_clearance` | plate → job → config | 0.0 |
| plate `width`/`height` | plate → config (`plate_width`/`plate_height`) | 24×16 (unbounded auto-allocation only) |

`job-config.json` sits **below the YAML but above the fallbacks**: its values
are injected at the job layer before validation, so labels still override
them. A spec-declared `margin` (job level, **or on any label**) suppresses
the configured `h_margin`/`v_margin` injection: the cascade checks the
axis-specific job-level keys *before* `margin`, so an injected axis value
would silently outrank the spec's margin on that axis. See
[`job_config.schema.json`](job_config.schema.json) for its
contract (unknown keys fail loudly; `hole_diameter` fills `diameter` on hole
entries that omit it).

## Behavioural semantics a schema cannot express

- **Hole groups**: `location: corners` expands in place to the four corner
  holes; `sides` to left+right — at validation time, so downstream only ever
  sees the 8 atomic locations.
- **Line hard break** (`new_line`, line-level only): the flagged line *begins*
  a new `optimize_line_content` reflow group, so the lines that follow it
  reflow with it rather than with the lines above it (the group's words fill
  the lines from the flagged one downward). The flag is inert unless the line
  also enables `optimize_line_content` — it never raises and never enables
  reflow by itself. The `optimize_line_content_max_lines` cap stays
  group-scoped, so two different caps in two groups separated by a flagged
  line are legal (only two different caps *within one group* raise
  `LineContentConfigError`).
- **Cutter-compensated holes** (`hole_adjust_for_cutter`, default true): the
  drill path is the circle the cutter *centre* follows, so the cutter engraves
  half a cutter width on each side of it. With the mechanism on, the emitted
  circle shrinks by the boundary/hole cutter diameter (the cutter radius on
  each side of the path) so the engraved hole measures the specified diameter:
  a `0.1` hole cut by a `0.015` cutter emits a `0.085` circle. The hole
  **centre** is always placed from the specified diameter, so hole positions
  never move; a hole at or below the cutter diameter clamps to the smallest
  emittable circle (one plotter unit radius) with a WARNING. The text-hole
  collision check measures the engraved circle, so near misses relax by the
  same half cutter.
- **Auto-sizing**: omitting `width`/`height` sizes the label from rendered
  text + margin.
- **Plates**: `width`/`height` are the *usable* pack area;
  `left_clearance`/`top_clearance` shift it right/down within the material
  (origin = material top-left, +y down). No plates → unbounded mode:
  auto-allocated `default_plate_{i}` sheets (config `plate_width/height`,
  else 24×16), overflowing onto as many sheets as needed.
- **`layout: columns` (default)** fills each plate's height before extending
  rightward (scrap-friendly single rectangular block); `rows` is the
  historical width-first fill. Per-plate override splits packing into
  sequential same-mode groups.
- **`allow_rotation` (default true)**: labels rotate 90° CW only when it
  strictly improves used area; ties keep all-horizontal.
- **`material` (default null)**: free-form stock name grouping labels into
  independent packing passes — labels sharing a material pack together and
  a plate carries exactly one material (matching is trim + casefold; the
  first-declared spelling wins for display). Material-less plates spread
  across the material groups; material-less jobs pack in one pass exactly
  as before. Unbounded mode allocates one auto-bin pool per material
  (`<material>_default_plate_{i}`). Not a `job-config.json` key.
- **Per-plate typographic fields** (`hole_margin`, `max_h_compress`,
  `text_h_alignment`, `min_hole_margin`, `hole_text_collision_distance`,
  `cutter_size`, `target_boldness_factor`, `multistroke_overlap_ratio`,
  `cutter_downsize`, `max_cutter_downsizes`,
  `cutter_downsize_global`, `h_compress_global`, `enforce_cap_line`,
  `use_baseline_spacing`,
  `v_center_by_content`,
  `auto_descending_character_adjustment`, `descending_character_v_scale`,
  `descending_character_raise`, `descending_character_clearance`,
  `descending_character_reciprocal_scale`, `hole_adjust_for_cutter`) are
  accepted on plates for schema parity but **not applied** at plate level —
  labels render once before packing.
- **Multi-stroke (bold) fonts** (`target_boldness_factor` default 0.14, bounds
  [0.05, 0.30]; `multistroke_overlap_ratio` default 0.25, bounds [0.1, 0.5]):
  a PLT font whose glyphs carry N > 1 parallel cutter passes (the `_S<n>`
  sample-sheet suffix recorded as `stroke_count` in the font's
  `fonts/VP-EL-fonts/catalog/` file)
  gets its automatic cutter from the boldness ladder: the engraved passes must
  total `text_height * target_boldness_factor` of width, with
  `multistroke_overlap_ratio` as the gap-free floor (the cutter is never
  smaller than `pass_spacing / (1 - ratio)`, so adjacent passes always
  overlap), then snapped to the `tools.json` inventory (stepping up when the
  snap-down breaks the floor). The implicit pass overlap is derived and logged.
  The stored glyph geometry is emitted verbatim (the sheet is the source of
  truth for the bold shape), so the stroke count drives cutter sizing only —
  kerning, collision, descender relief, the pen map and per-cutter files all
  read the per-line `cutter_diameter` and pick the result up. Inert on
  single-stroke fonts, TTF fonts, and lines with an explicit `cutter_size`
  (which bypasses the ladder). Both cascade line → label → job → config
  (plate parity only).
- **`cutter_size` (default null)**: an explicit cutter diameter (inches)
  overriding the automatic height→cutter lookup. The requested diameter is
  snapped to `tools.json` `available_cutters` (next size down, else next up,
  WARNING-logged when it moves); the nominal `text_height` is kept as the
  user's intent and the rendered toolpath height becomes
  `text_height - cutter_size` (so vertical fit math is unaffected). A cutter
  at or above the line's `text_height` aborts the job (`CutterSizeError`,
  non-zero exit). Each distinct cutter — explicit or auto — becomes its own
  per-cutter text layer/PLT file. Omitting it keeps automatic selection
  (current behaviour). Not a `job-config.json` key.
- **`cutter_downsize` (default true) + `max_cutter_downsizes` (default 1)**:
  compression-driven cutter reduction. When a text line's *effective*
  horizontal scale (margin × collision compression) falls below the midpoint
  between the current automatic cutter and the next smaller
  `tools.json` `available_cutters` rung — e.g. 0.100in current, 0.080in next:
  scale < 0.90 downsizes to 0.080in — the line's cutter drops one rung and
  the toolpath height grows to `text_height - smaller_cutter` (nominal
  `text_height` kept). At most `max_cutter_downsizes` rungs per line
  (`0` = disabled); each step re-measures the re-rendered line, and the
  ladder is strictly one-way (a line never regains a larger cutter). Requires
  a cutter inventory (no `tools.json` → no reduction) and a compression
  budget (`max_h_compress > 0`); an explicit `cutter_size` is never reduced.
  The reduced cutter flows into the pen map, so the downsized line lands in
  its own per-cutter PLT file; the change is WARNING-logged at render time
  and reported in the layout report (`cutter 0.060in -> 0.045in`).
  `job-config.json` supplies the shop defaults.
- **`cutter_downsize_global` (default true)**: per-label sharing of
  compression-driven cutter downsizes. When a line's compression triggers a
  swap, every other *eligible* line of the same `text_height` **within the
  same label** receives the same swap (the midpoint trigger check is bypassed
  for receivers — a sibling's trigger is their trigger), then each receiver
  continues its own one-way step loop; the group converges to the smallest
  final cutter among its members, so one label engraves one text size with
  one tool. Lines with an explicit `cutter_size`, `cutter_downsize: false`,
  `max_cutter_downsizes: 0`, `max_h_compress: 0.0`, or
  `cutter_downsize_global: false` are never touched (in either direction:
  such a line neither shares its own downsize nor receives a sibling's).
  Sharing is always per-label — setting the option at the job level simply
  enables it for every label. WARNING-logged per propagated line and
  reported in the layout report like a trigger downsize. `job-config.json`
  supplies the shop default.
- **`h_compress_global` (default false)**: per-label sharing of horizontal
  compression. When a text line is horizontally compressed (margin overflow
  and/or hole collision), every other *eligible* line of the same
  `text_height` **within the same label** is compressed to the group's
  most-compressed (minimum) scale, clamped to each receiver's own
  `1 - max_h_compress` budget floor — so one label engraves one text size at
  one glyph density. A line whose natural compression is already tighter keeps
  it (never stretched back). Lines with `max_h_compress: 0.0` are never
  touched (no compression mechanism can fire on them), and
  `h_compress_global: false` opts a line out in both directions (it neither
  shares its own compression nor receives a sibling's). Sharing is always
  per-label — setting the option at the job level simply enables it for every
  label. Runs as an export pre-pass **after** the cutter reduction, so a
  shared compression can deepen a line's squeeze without re-triggering a
  cutter swap (accepted staleness). WARNING-logged per propagated line and
  reported in the layout report as an ordinary compressed line. `job-config.json`
  supplies the shop default.
- **`enforce_cap_line` (default true)**: alphanumeric cap-line clamping.
  Every ASCII alphanumeric `[0-9a-zA-Z]` glyph whose ink extends *beyond* the
  per-font capline (the reference-character height) is downscaled **top-down**
  — a vertical scale about the glyph's own bottom, X untouched, so the aspect
  ratio changes — and its top lands exactly on the capline. The enforcement is
  strictly **pre-markup**: it applies to the raw glyph, before the small-caps,
  bold, scale, italic and superscript/subscript transforms. Non-alphanumeric
  characters (punctuation, spaces, derived Unicode symbols) are never clamped,
  and a glyph sitting exactly at the capline (the reference char) never moves.
  The vertical-only scale cannot keep `AA` arcs native (a non-uniform scale
  maps a circular arc to an ellipse), so clamped glyphs flatten at the chord
  tolerance (the `scale_y` policy). Cascades line → label → job (plate parity);
  `job-config.json` supplies the shop default.
- **`use_baseline_spacing` (default true)**: baseline-to-baseline line
  spacing. Stacking a label by rendered ink box means a line carrying
  descenders (`g j p q y`) pushes the next line down by its full ink height,
  inflating that one visual gap. With baseline spacing each line's baseline
  sits one pitch below the previous one (`pitch = cap height + line_spacing`),
  so the descender hangs *into* the gap and every gap on the label reads as
  one uniform visual spacing. Lines without descenders are bit-identical to
  ink-box stacking. Auto `line_spacing` and the margin clamp measure each
  line's real ascender/descender extents (render probe), so a descender-heavy
  block claims its space up front and fills the label exactly. `false`
  restores the historical ink-box stacking. Cascades label → job → config
  (never text lines — spacing is a property of the stacked block); plates are
  parity-only.
- **`v_center_by_content` (default false)**: how a label centers its text
  vertically. The historical rule centers the block by its **rendered ink**,
  charging the deepest descender to the label — so a line carrying descenders
  rides high and its plain letters look visibly off-centre. `false` (the
  default) centers the **baseline-to-capline band** instead: the visible glyphs
  sit in the label's optical middle and descenders hang into the bottom margin,
  where the descending-character relief clears them against the bottom border.
  The vertical margin clamp follows the same model, so a band-centered label
  sizes its line spacing against the cap band and descenders no longer shrink
  the spacing. Band centering is active **only** when
  `auto_descending_character_adjustment` is enabled — with the relief off
  nothing would adjust a descender, so ink centering is kept and this field is
  inert. `true` restores ink centering bit-identically. Cascades label → job →
  config (never text lines — centering is a property of the stacked block);
  plates are parity-only.
- **`auto_descending_character_adjustment` (default true)**: per-glyph
  descending-character relief. Baseline spacing lets a descender hang into the
  gap below its line; when a glyph on the **following** line is close enough
  that the two engraved strokes would bleed together, that one descending glyph
  is vertically downscaled (about its own baseline, down to
  `descending_character_v_scale`) and raised (by up to `descending_character_raise`
  × its own descender depth, so `1.0` lifts its lowest point exactly to the
  baseline). The trigger is a measured near-miss against the line below, using
  the **true 2D swept-ink distance**: a circle of the required diameter rolled
  along the descending glyph's sub-baseline ink touching any part of any glyph
  of the line below, i.e. a gap under
  `0.5 * (cutter_upper + cutter_lower) * (1 + descending_character_clearance)`
  (the clearance is a unitless factor of the stroke floor, so it reads the same
  at every text size). This is deliberately **not** a bounding-box comparison:
  a box model calls X-disjoint boxes "infinitely far apart" and hides a `g`
  hook reaching sideways into the crook of the `i` on the line below, while
  firing on box overlaps whose real ink is nowhere near. The relief is per
  descending glyph — never a blanket adjustment of every descender, and never
  based on the following line's cap line in the abstract.
  The label's **bottom border** is measured the same way, as a virtual
  "following line": the border is drawn at the label edge and cut by the
  boundary/hole tool, so a descender collides when its ink comes within
  `0.5 * (text_cutter + hole_cutter_diameter) * (1 + descending_character_clearance)`
  of the border centreline. Crossing `v_margin` is **not** a collision — ink
  may spill into the margin; only the border stroke counts. No extra fields.
  The relief is **reciprocal**: the colliding glyphs on the following line
  share the deficit through the *same* ramp, each scaled **top-down** about its
  own lowest ink point (so its lowest point never moves and its top descends
  toward it), capped by `descending_character_reciprocal_scale` (default 0.5;
  `1.0` forbids the reciprocal half and reproduces the historical one-sided
  relief bit-identically). A glyph on a middle line can carry both roles at
  once — its own descending ramp into the gap below and a top-down ramp from
  the gap above.
  When both allowances are exhausted and the pair is still too close, the gap
  below the line grows (capped by the vertical margins); a collision that
  neither the allowances nor the admissible spacing can clear aborts the job
  (`LabelRenderError`, non-zero exit). A **border** collision never aborts:
  growing the spacing above the last line moves that line *down* toward the
  border, so the per-glyph ramp is the only remedy and a spent ramp is reported
  as a WARNING while the job engraves the best the allowances allow. Runs at
  render time in both stacking modes; arcs on a relieved glyph are flattened
  (bounded chord error, DEBUG).
  Reported per glyph in the layout report. Cascades label → job → config (never
  text lines — the mechanism compares a line's glyphs against the following
  line's); plates are parity-only. `false` engraves descenders exactly as
  rendered.
- **Text–hole collision avoidance** (3 phases): always-on detection →
  opt-in `min_hole_margin` sweep → opt-in `max_h_compress` compression.
  Unresolved collisions fail the job (`LabelRenderError`, non-zero exit).
  Threshold: `0.5 * (hole_cutter + text_cutter) + hole_text_collision_distance`.
- **Required-when-unconfigured gate**: with `--job-config` in play,
  `max_h_compress`, `hole_margin`, `min_hole_margin`,
  `hole_text_collision_distance` (and `plate_width`/`plate_height` when
  plates are undeclared) must come from the config **or** the spec, else
  `JobConfigError`. Without a config path, silent fallbacks apply.

## Validating YAML against these artifacts

Any JSON-Schema validator works after `yaml.safe_load` (note: the schema
covers structure/types/enums/constraints, **not** the cross-field rules
above — use `parse_yaml` for those):

```python
import json, yaml, jsonschema
from pathlib import Path

schema = json.loads(Path("docs/schema/job_spec.schema.json").read_text())
job = yaml.safe_load(Path("my_job.yaml").read_text())["job"]
jsonschema.validate(job, schema)          # structural check
# authoritative check:
from plt_optimizer.generate.schema import parse_yaml
# parse_yaml("my_job.yaml")               # + all cross-field validators
```

## Regenerating the artifacts

```bash
uv run python docs/schema/generate_ai_docs.py
```

Run it after any change to `schema.py`, `job_config.py`, or the fallback
constants in `resolution.py`/`layout.py`, and commit the output — CI fails
otherwise (`tests/test_job_spec_docs.py`).
