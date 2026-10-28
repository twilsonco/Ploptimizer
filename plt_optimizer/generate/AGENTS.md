# AGENTS.md - Label Generation Pipeline (`plt_optimizer/generate/`)

Parent contract: [`../../AGENTS.md`](../../AGENTS.md) (coding standards, testing,
git rules, cross-cutting invariants incl. the 1016 device scale and the
Python 3.8 / Windows 7 constraint). This file is the component contract for the
YAML-spec label generation pipeline; the sections below moved verbatim from the
old root AGENTS.md section 6.

Sub-contracts in this directory tree - read them when touching those modules:

- [`markup/AGENTS.md`](markup/AGENTS.md) - the inline markup framework + all 11
  tags (`<i> <u> <b> <scale:N> <sup> <sub> <s> <sc> <f:> <c:> <oc> <vc> <tc:>`). Sections in this
  file that reference an "Inline ..." contract point there.
- [`collision/AGENTS.md`](collision/AGENTS.md) - text-hole collision avoidance +
  cutter-compensated drill holes.
- [`packing/AGENTS.md`](packing/AGENTS.md) - plate-space toolpath optimization +
  bin-packing (rotation, fill order, plate clearances, material, ellipse,
  rounded-rect).
- [`../cli/AGENTS.md`](../cli/AGENTS.md) - the CLI surface (old section 7).

## 6. YAML Job Specification & Label Generation Schema

### Overview
The `plt_optimizer/generate/schema.py` module defines the complete data contract for label generation jobs via YAML specifications. All models use **Pydantic** for validation and inherit from typed base classes supporting cascading attributes.

**Strict schema (every model forbids unknown fields):** all 12 spec/config
models set `model_config = ConfigDict(extra="forbid")` (declared on the
`HoleSpec` / `TextAttributes` / `PlateSpec` roots — pydantic inheritance
covers the rest — and on `JobDefaults` in `job_config.py`). A misspelled key
at any tier (job, label, content line, list block/item, plate, hole) fails
loudly at `parse_yaml()` with its exact dotted path
(e.g. `labels.0.content.0.tex_height: Extra inputs are not permitted`) —
a field the user typed is a field the user expects to engrave, so silent
ignoring is never acceptable. The free-form `tool_options: dict[str, Any]`
*field value* is unaffected (its keys are engraver option names, not schema
fields), but its **metadata entries are semantically gated** on `JobDefaults`
(`validate_tool_options_metadata` in `job_config.py`): each entry must be an
object with a non-empty `command` (no `;`), a `type` of `bool`/`int`/`float`,
a `default` (scalar, dual `text`/`borders_holes`, or an explicit `null` layer
to omit that PLT type), numeric `min`/`max` with `min <= max` for int/float,
and a snake_case option key. Every problem in the file is reported in one
`JobConfigError` at `load_job_config()` — the shapes this rejects used to
crash mid-export, emit a header with no command prefix, or silently drop a
cutter option. `JobDefaults.font` is canonicalized with the same
`normalize_font_name` registry rule the spec tiers use, so a bad config font
blames the config, not the spec. Pinned by
`tests/test_job_config.py::TestToolOptionsMetadataValidation`. The
generated JSON schemas carry the structural contract as
`additionalProperties: false` (drift-pinned by `tests/test_job_spec_docs.py`);
unknown spec keys are pinned by `tests/test_schema.py::TestStrictUnknownFields`.
Pre-flight CLIs for both gates: `scripts/validate_job_config.py` and
`scripts/validate_job_spec.py` (see the `validating-jobs` skill).

**Attributes in schema model docstrings not typically necessary:** Because schema.py is Pydantic models (classes), and each attribute already has a `description` field, it is redundant to repeat the same information in the docstring. The docstring should focus on the overall purpose and usage of the model rather than individual attribute details.

### Core Data Model

**Inheritance Hierarchy (Top-Down Cascade):**
```
JobSpec (job-level defaults)
  ├── LabelSpec (per-label overrides)
  │   └── TextLine (individual text rendering)
  └── PlateSpec (material sheet definitions)
```

**TextAttributes** (cascades to TextLine):
- `text_height`: Font height in inches
- `font`: Font name selecting the glyph outlines. Either a PLT-extracted
  `fonts/VP-EL-fonts/catalog/*.json` `font_name` key (one JSON file per font,
  rendered **arc-native** through
  `plt_font_renderer.py`: PU/PD/AA glyphs keep their native `AA` arcs
  end-to-end — the non-uniform transforms (compression, the cap-line clamp,
  the descender relief, the italic shear) refit each arc's ellipse image with
  circular `AA` arcs (see Arc-Preserving Non-Uniform Transforms); only arcs the
  refit declines flatten, with a WARNING) or a
  `fonts/**.ttf` basename (extension stripped, rendered through the
  matplotlib `ftext_renderer` path). Case-insensitive, canonicalized at
  validation (`font_registry.resolve_font`); unknown names are rejected.
  Appending ` SC` to any name selects that font's synthetic **small-caps**
  variant (rendered on the fly from the base, never stored in the library —
  see Small Caps Fonts below).
  TTF lines scale by a fixed per-font reference — the ink height of the
  reference glyph `H` — so the cap height equals `text_height` on *every*
  text line (glyph size is a per-font constant, independent of a line's own
  ink box; descenders hang below the baseline, matching the PLT contract).
  Scaling each line by its own rendered ink box (the historical behavior)
  shrank every glyph on a line containing descenders/ascenders/tall
  punctuation, so identical `text_height` values engraved at visibly
  different sizes across lines. A TTF lacking a glyph raises
  `FtextRenderError` → `LabelRenderError` (parity with the PLT
  `PltFontRenderError`); `fonts/font_showcase.py` opts out of that probe
  (`check_glyph_coverage=False`) to render `.notdef` boxes as coverage info.
  Cascades line → label → job (default `ReliefSingleLineCAD-Regular`;
  accepted on plates for schema parity only). PLT fonts render from the v2
  library (baseline-normalized glyphs, +y up, ref char exactly 1000 units):
  glyphs anchor on the baseline (descenders hang below; a character listed in
  `extract_plt_fonts.VERTICALLY_CENTERED_CHARS` — currently `S` — is
  midline-centred at extraction, so its box straddles the baseline on purpose
  and the reference char is always exempt), the reference
  character scales to exactly `text_height`, and adjacent glyphs inside a
  word are **profile-envelope kerned** — origin-to-origin advance =
  `max(scaled penetration, advance floor)` + clearance
  `cutter_diameter + character_spacing + kerning_min_gap_fraction * cap
  height` (no fixed height
  fudge). The scaled penetration is the windowed silhouette penetration ×
  `kerning_penetration_scale` when non-negative, × `kerning_recession_scale`
  when negative (a recessed pair); the advance floor is
  `min(left glyph width, min_glyph_width_fraction * cap height)`, so a tight
  pair can never be
  kerned below the left glyph's own width (zero-width glyphs floor at 0.0
  and get their air from the clearance). The window
  `kerning_window_fraction` (fraction of
  text height, default 0.05) makes each envelope sample compare against
  the deepest opposing sample within ±half the window (staggered pokes
  count), then takes the maximum of those windowed penetrations — the
  window can only widen the advance relative to same-height kerning,
  never narrow it; `0.0` reproduces the historical same-height
  maximum-penetration math exactly. Sampling spans the pair's overlapping
  height extended by ±half the window (clamped to the union bbox), so a
  poke just outside the overlap compares against the opposing silhouette's
  nearest material;
  a space advances `space_width_fraction * text_height +
  character_spacing` and breaks kerning. A character the PLT font lacks
  raises `PltFontRenderError` at render → `LabelRenderError` (CLI
  non-zero). Every v2 library font also carries 17 **derived Unicode
  glyphs** (`–—•∞±¢≠≈≡¿¡†‡↑↓←→`) that `extract_plt_fonts.py` builds from
  ASCII bases through `fonts/glyph_transforms.py` (affine transforms with
  arc-sweep rules; bounding box and envelopes re-sampled from the
  transformed geometry), so they kern and render like engraved characters.
  Fonts extracted from a sample sheet whose name carries a trailing `_sym`/
  `_symbol` suffix (symbol fonts such as `Wingdings`; the flag is stored as
  the catalog's `symbol_font` bool, read by
  `font_registry.plt_font_is_symbol()`) engrave their ASCII slots with
  symbols, so they are exempt from derivation, from the showcase's Unicode
  line, and from the `SC` variant (see Small Caps Fonts below). Their font
  showcase (`fonts/font_showcase.py`) is a **key -> symbol mapping** grid
  instead of the letter sample block: one cell per `fonts/ascii.txt`
  character, each engraving the ASCII key (`a:`, `b:`, ...) in
  `ftext_renderer.DEFAULT_FONT_PATH` beside the font's symbol, under a
  header naming the mapping (see the showcase's `render_mapping_layout`).
  List every valid name with
  `python docs/schema/generate_schema_docs.py --show-fonts`.
- `character_spacing`: Extra spacing between characters
- `line_spacing`: Extra spacing between text lines
- `space_width_fraction`: Space advance as a fraction of the rendered text
  height for PLT fonts (`ge=0.0`, default `None` → **0.3**; only `None`
  means unset, an explicit `0.0` collapses the space to bare
  `character_spacing`). Cascades line → label → job (accepted on plates for
  schema parity only); `job-config.json` supplies the shop default.
- `min_glyph_width_fraction`: Global minimum glyph advance width as a
  **unitless factor of the rendered cap height** (capline − baseline =
  `toolpath_text_height`) for PLT
  fonts (`ge=0.0`, default `None` → **0.0** = pure envelope kerning). The
  envelope kerning floors each pair's advance at
  `fraction * cap height`, **capped by
  the left glyph's own bounding-box width** (`min(left_width, floor)`), so
  a thin glyph is never pushed past its own extent and a zero-width glyph
  (`!`, `|`) floors at 0.0 (its air comes from the clearance). Cascades
  line → label → job (accepted on plates for schema parity only);
  `job-config.json` supplies the shop default.
- `kerning_window_fraction`: Profile-envelope kerning window for PLT fonts
  as a fraction of the rendered text height (`ge=0.0`, `le=1.0`, default
  `None` → **0.05**; only `None` means unset, an explicit `0.0` restores
  the historical same-height maximum-penetration kerning). Each envelope
  sample compares against the opposite silhouette within ±(half this
  fraction) of the text height and the effective penetration is the worst
  windowed penetration, so staggered pokes widen the advance (the window
  never narrows it). Cascades line → label → job (accepted on
  plates for schema parity only); `job-config.json` supplies the shop
  default.
- `kerning_penetration_scale`: Multiplier on the detected windowed
  penetration for PLT fonts, applied only to **non-negative** penetration
  (`ge=0.0`, default `None` → **1.0** = geometric; `>1.0` over-kerns tight
  pairs proportionally, e.g. a detected 0.2" poke costs 0.3" of advance at
  1.5). Recessed (negative-penetration) pairs use `kerning_recession_scale`
  instead, so tightening tight pairs never pulls gapped pairs together.
  Cascades line → label → job (accepted on plates for schema parity only);
  `job-config.json` supplies the shop default.
- `kerning_recession_scale`: Multiplier on the detected windowed
  penetration for PLT fonts, applied only to **negative** penetration —
  pairs whose silhouettes are recessed (a gap, e.g. `,4`) (`ge=0.0`,
  default `None` → **1.0** = geometric; `0.0` neutralises recessed pairs to
  bare clearance, `>1.0` pushes them further apart). The advance floor
  `min(left_width, min_glyph_width_fraction * cap height)` keeps the result
  non-negative, so a
  recessed pair's origin never moves left of the left glyph's width.
  Cascades line → label → job (accepted on plates for schema parity only);
  `job-config.json` supplies the shop default.
- `kerning_min_gap_fraction`: Extra air added to every kerned pair
  advance for PLT fonts as a **unitless factor of the rendered cap height**
  (capline − baseline), on top of the `cutter_diameter +
  character_spacing` clearance (`ge=0.0`, default `None` → **0.0** = no
  extra gap). Cascades line → label → job (accepted on plates for schema
  parity only); `job-config.json` supplies the shop default.
- `fallback_advance_fraction`: Multiplier on the bounding-box-width
  fallback advance used for PLT glyph pairs without overlapping height
  (or lacking envelopes) (`ge=0.0`, default `None` → **1.0** = the left
  glyph's own width). Cascades line → label → job (accepted on plates for
  schema parity only); `job-config.json` supplies the shop default.
- `kern_pos_string` / `kern_neg_string` / `kern_width_fraction`: **manual
  kerning** via inline marker strings (see
  [`markup/AGENTS.md`](markup/AGENTS.md) "Manual Kerning"). Every occurrence
  of the positive marker string in a text line's raw text is stripped (it
  never engraves) and the following character's cursor advances
  `+kern_width_fraction * cap height` (capline − baseline); every occurrence
  of the negative marker retreats it by the same amount (**no clamp** — a
  large negative value lets glyphs overlap). Multi-character strings are
  legal (`<->`); the schema rejects an empty value, a newline, a value equal
  to the other marker, and any markup-shaped value (`<s>`, `</i>`,
  `<scale:0.5>` — a letters-only body the tag scan consumes first), and
  resolution re-validates the marker against the line's **effective**
  `html_brackets` + the tag registry (a marker that parses as markup raises
  `MarkupError` → CLI non-zero). `kern_width_fraction` is `ge=0.0`
  (default `None` → **0.1**; explicit `0.0` consumes marker characters while
  shifting nothing). Adjacent markers accumulate; a leading marker shifts
  the line's first glyph (the ink-box normalization cancels it) and a
  trailing marker is a no-op. Works on **PLT and TTF** fonts (the PLT walk
  adds the delta to the cursor after the pair's envelope kerning; the TTF
  path translates each glyph by the cumulative suffix total). Cascades
  line → label → job (accepted on plates for schema parity only);
  `job-config.json` supplies the shop default (the strings ship as
  `<+>` / `<->`). The schema validator enforces the bracket-agnostic
  invariants (non-empty, no newline, the two differ at one level); the
  markup-collision check runs at resolution against the line's EFFECTIVE
  `html_brackets` + the tag registry (a custom-bracket job may legally use
  `<s>` as a marker).
- `sc_scale`: Downscale applied to ASCII lowercase `a-z` by a **small-caps**
  font — a `font` name ending in ` SC` (e.g. `"Dino SC"`), a synthetic
  registry variant of the base font (`ge=0.2`, `le=1.0`, default `None` →
  **0.7**). The lowercase character renders as the base font's **uppercase**
  glyph uniformly shrunk to `sc_scale` of the cap height, **top-down** (the
  glyph's own bottom stays exactly in place, its top descends). Uppercase and
  caseless characters (digits, punctuation, derived symbols) engrave at full
  size; the nominal/toolpath `text_height` is untouched (no cutter change);
  `1.0` renders the variant identically to its base font; the field is inert
  on a non-SC font. Cascades line → label → job (accepted on plates for
  schema parity only); `job-config.json` supplies the shop default. The same
  field drives the inline `<sc>` small-caps tag (the per-character selection,
  see Inline Small Caps below). See Small Caps Fonts below.
- `max_h_compress`: Maximum horizontal compression fraction in [0, 1] (default
  0.0 = disabled). When a rendered line is wider than the label's inner
  content area, it is uniformly compressed horizontally down to at most
  `(1 - max_h_compress)` of its natural width.
- `text_h_alignment`: Horizontal alignment of a rendered text line within the
  label's inner content area. Enum `TextHAlignment` with values `left`,
  `center`, `right` (default `center`). `left` places the line's left-most
  point precisely at the left margin; `right` places the right-most point
  precisely at the right margin; `center` centers the line. Accepted on
  plates for schema parity only (not applied at plate level).
- `min_hole_margin`: Minimum hole margin in inches (`ge=0.0`, default `None`
  = collision avoidance disabled). When a rendered text line collides with a
  drill hole, `hole_margin` may be reduced toward this floor to clear it.
  Cascades label → job (accepted on plates for schema parity only).
- `hole_text_collision_distance`: Minimum air gap in inches (`ge=0.0`,
  default `None` → resolved to **0.15**, or the `job-config.json` value when a
  config is in play — currently **0.1**) kept between the *engraved* text
  stroke and the *engraved* drill-hole stroke. Cascades label → job
  (accepted on text lines and plates for schema parity only); an explicit
  `0.0` is honored (strokes may touch but never overlap).
- `cutter_size`: Explicit text cutter diameter in inches (`gt=0.0`, default
  `None` = **automatic cutter selection** from `text_height` via
  `IDEAL_CUTTER_MAP` + inventory snapping — the current behavior). When set,
  the requested diameter is snapped to `tools.json` `available_cutters`
  (`snap_boundary_hole_cutter`: next size down, else next size up; a WARNING
  names the label id + line index when it moves) and the rendered toolpath
  height is recomputed as `text_height - (cutter_size + cutter_buffer_text)`.
  The nominal `text_height` is kept as the user's intent, so vertical fit math
  (`_resolve_auto_line_spacing`, `_fit_content_to_margins`) is unaffected.
  The buffer applies to an explicit tool exactly like an automatic one
  (`cutter_size` names the TOOL, never the engraved width). A cutter whose
  engraved width is at or above the line's `text_height` (toolpath height ≤ 0)
  raises
  `CutterSizeError` (a `ValueError` subclass) → CLI non-zero exit. Cascades
  line → label → job (accepted on plates for schema parity only); there is
  deliberately **no** `job-config.json` counterpart (adding it to
  `JobDefaults` would make it a legal shop default — do not). Each distinct
  cutter, explicit or auto, becomes its own per-cutter text layer/PLT file
  (`cutter_diameter` is already per-line end-to-end, so `build_cutter_pen_map`,
  `label_renderer._render_text_lines_by_pen` and `export_per_cutter_plts` need
  no change). See also the sibling `cutter_downsize` (render-time downsize
  driven by horizontal compression — implemented, see below).
- `cutter_buffer_text`: Kerf buffer in inches **added to a text line's cutter
  diameter** to get the width it actually engraves (`ge=0.0`, default `None` →
  **0.0** = the legacy engraved = nominal, or the `job-config.json` value when a
  config is in play — currently **0.005**). The physical model: a V-bit with
  roughly 45° flanks cuts a groove wider than its nominal diameter (the deeper
  the cut, the wider the stroke), so the nominal tool under-reports the engraved
  stroke — which is why the `all_*.pdf` proofs drew narrower strokes than the
  machine leaves. `resolution.effective_cutter_diameter(physical, buffer)` is
  the single implementation (`physical + buffer`); the resolved per-line value
  lives on `ResolvedTextLine.cutter_buffer` with derived `effective_*`
  properties (see Physical vs. effective cutter diameter below). Cascades
  line → label → job → `job-config.json` (accepted on plates for schema parity
  only); an explicit `0.0` is honored.
- `cutter_buffer_routing`: Kerf buffer in inches **added to the
  boundary/drill-hole cutter** (`tools.json` `boundary_hole_cutter_size`) to get
  the width it engraves (`ge=0.0`, default `None` → **0.0**, or the
  `job-config.json` value — currently **0.045**). The structural sibling of
  `cutter_buffer_text`, same 45°-flank rationale, stored on
  `ResolvedLabel.hole_cutter_buffer` with the derived
  `effective_hole_cutter_diameter` property. It drives the cutter-compensated
  emitted hole radius, the hole-margin edge offset, the collision stroke floor,
  the round-label packing kerf, the descender relief against the border and the
  `*_bh_*` / `all_*.pdf` proof stroke widths. Cascades label → job →
  `job-config.json` (accepted on text lines and plates for schema parity only —
  hole geometry is a per-label property); an explicit `0.0` is honored.
- `target_boldness_factor`: Target engraved stroke width for a **multi-stroke
  (bold) PLT font** as a fraction of `text_height` (`ge=0.05`, `le=0.30`,
  default `None` → **0.14**). A PLT font whose glyphs carry `stroke_count > 1`
  parallel cutter passes (the `_S<n>` sample-sheet suffix recorded as the
  font catalog file's `stroke_count`) gets its automatic cutter from the boldness ladder
  (see Multi-Stroke (Bold) PLT Fonts below) instead of the height-based
  lookup. Inert on single-stroke fonts, TTF fonts, and lines with an explicit
  `cutter_size` (which bypasses the ladder). Cascades line → label → job →
  `job-config.json` (accepted on plates for schema parity only).
- `multistroke_overlap_ratio`: Minimum overlap fraction between adjacent cutter
  passes of a multi-stroke PLT font (`ge=0.1`, `le=0.5`, default `None` →
  **0.25**). The ladder's cutter is never smaller than
  `pass_spacing / (1 - ratio)`, so adjacent engraved passes always overlap by
  at least this fraction and no unengraved stripe appears between them; the
  actual overlap is derived from the chosen cutter and logged (an overlap of
  0.25 means the stepover stays within 75% of the cutter width). Same cascade
  tiers and inertness as `target_boldness_factor`.
- `cutter_downsize`: Boolean gate for compression-driven cutter reduction
  (`default None` → **true**). When true, a text line whose *effective*
  horizontal compression scale (margin scale × collision scale, measured at
  render time) falls below the midpoint between the current **automatic**
  cutter and the next smaller `tools.json` `available_cutters` rung drops the
  line's cutter one rung (e.g. 0.100in current, 0.080in next: midpoint 0.90,
  so scale < 0.90 triggers, ≥ 0.90 keeps). The toolpath height grows to
  `text_height - smaller_cutter` (nominal `text_height` kept as user intent,
  matching the `cutter_size` contract). An explicit `cutter_size` disables
  the reduction for that line. Cascades line → label → job → default
  (plate parity only); `job-config.json` supplies the shop default.
- `max_cutter_downsizes`: Maximum number of cutter downsizings per text line
  (`ge=0`, default `None` → **1**). Each step re-measures the re-rendered
  line (the new cutter changes the compression), and the ladder is strictly
  one-way — a line never regains a larger cutter within the same render
  (monotonicity: no oscillation). `0` disables the mechanism entirely.
  Cascades line → label → job → default (plate parity only);
  `job-config.json` supplies the shop default.
- `cutter_downsize_global`: Boolean gate for **per-label sharing** of
  compression-driven cutter downsizes (`default None` → **true**). When a
  text line's compression triggers a cutter swap, every other *eligible* line
  of the same `text_height` **within the same label** receives the trigger's
  final cutter directly (the midpoint trigger check is bypassed for
  receivers — a sibling's trigger is their trigger), then each receiver
  continues its own one-way step loop; the group converges to the smallest
  final cutter among its members, so one label engraves one text size with
  one tool. Lines with an explicit `cutter_size`, `cutter_downsize: false`,
  `max_cutter_downsizes: 0`, `max_h_compress: 0.0`, or
  `cutter_downsize_global: false` are never touched — in either direction
  (such a line neither shares its own downsize nor receives a sibling's).
  Sharing is per-label by default; `cutter_downsize_global_job_wide` widens
  the group to the whole job (see below). Setting the option at the job level
  simply enables it for every label (plate = parity-only, like every
  render-affecting field — plate identity does not exist when the export
  pre-pass runs).
  Cascades line → label → job → default (plate parity only);
  `job-config.json` supplies the shop default.
- `cutter_downsize_global_job_wide`: Boolean scope knob for **job-wide
  sharing** of compression-driven cutter downsizes (`default None` →
  **false**). When a trigger line has it enabled (together with
  `cutter_downsize_global`), its swap reaches every *eligible* line of the
  same `text_height` in ANY label of the job, so one text size engraves with
  one tool job-wide (one per-cutter text layer per size — the shop's
  many-single-line-labels shape, where per-label sharing is inert). A line
  with this flag `false` keeps exactly the per-label behavior (it neither
  shares to nor receives from the job-wide group); the same eligibility
  opt-outs apply. The pass runs after every label's per-label pass and
  converges the same way (`_share_downsizes_job_wide`).
  Cascades line → label → job → default (plate parity only);
  `job-config.json` supplies the shop default (currently **true**).
- `h_compress_global`: Boolean gate for **per-label sharing** of horizontal
  compression (`default None` → **false**). When a text line is horizontally
  compressed, every other *eligible* line of the same `text_height` **within
  the same label** is compressed to the group's most-compressed (minimum)
  scale, clamped to each receiver's own `1 - max_h_compress` budget floor (a
  line whose natural compression is already tighter keeps it, never stretched
  back), so one label engraves one text size at one glyph density. Lines with
  `max_h_compress: 0.0` are never touched (no compression mechanism can fire
  on them) and a group needs ≥2 eligible lines to share.
  `h_compress_global: false` opts a line out in both directions (it neither
  shares its own compression nor receives a sibling's). Sharing is always
  per-label: setting the option at the job level simply enables it for every
  label (plate = parity-only, like every render-affecting field — plate
  identity does not exist when the export pre-pass runs). Cascades line →
  label → job → default (plate parity only); `job-config.json` supplies the
  shop default.
- `h_compress_global_same_height_only`: Boolean gate for the **grouping scope**
  of the shared compression (`default None` → **true**). `true` shares the
  compression only between the lines of the same `text_height` (one text size,
  one glyph density — the historical behavior); `false` shares it across
  **every** eligible line of the label, whatever their `text_height`, so one
  label engraves at one glyph density throughout. The group scale is still the
  label's most-compressed (minimum) scale, clamped to each receiver's own
  `1 - max_h_compress` budget floor. Cascades line → label → job → default
  (plate parity only); `job-config.json` supplies the shop default (`true`).
- `optimize_line_content`: Boolean gate for **word-level line-content reflow**
  (`default None` → **false**). When true, the text of the enabled lines may be
  redistributed across the consecutive lines that also enable it so the group's
  lines reach equal rendered width (distance, not character count) — whole
  words move up/down, word order is never changed, only the line breaks (the
  LaTeX paragraph-fill behavior). Consecutive enabled lines form one
  independent group; a disabled line breaks the group, and a word may travel
  across the whole group (not just one line). Optimizing the **pre-compression**
  widths is the same objective as minimizing the compression the label needs.
  Implemented as `plt_optimizer/generate/line_content.py::apply_line_content_reflow`,
  an **export pre-pass running FIRST** (before `apply_compression_cutter_downsize`
  and `apply_global_h_compress`, so those see the reflowed text) via the shared
  lazy `LineWidthProbe` (default `label_renderer.measure_line_natural_width`);
  it emits `ResolvedLabel` clones carrying the reflowed `content`. The
  repartition is the linear-partition problem solved exactly by DP: cut the
  concatenated word sequence into one contiguous segment per group line (every
  line keeps ≥1 word), minimizing the widest segment, tie-broken by the sum of
  squared widths. Segment widths use each line's own typography (per-word
  renders + a per-typography space advance, measured from the line's own text
  when it has two words). Scope guards (free no-ops): no enabled line; every
  enabled run is a single line; a run contains a blank line or has fewer words
  than lines. Each moved line logs an INFO with the label id, line index and
  before/after text. Cascades line → label → job → default (plate parity only);
  `job-config.json` supplies the shop default.
- `bullet_string`: String marker for every item of an `itemize` list
  (`default None` → **`"•"`** — the derived bullet glyph, engraved by every
  PLT catalog font except the symbol fonts; a TTF font lacking it fails
  loudly). Cascades **item → list block → label → job** → `job-config.json`
  (plate parity). Settable on a single item to override one bullet. An
  explicit empty string `""` engraves **no** marker (an indent-only list).
  A newline in the value is a validation error (a marker engraves on one line).
- `counter_start_index` / `counter_style` / `counter_prefix` / `counter_suffix`:
  The `enumerate` list's counter knobs (`default None` → **1 / `decimal` /
  `""` / `""`**). Each item's marker is
  `prefix + format_counter(start_index + k, style) + suffix` for its zero-based
  position `k` — the counter is always **computed**, so an enumerated item's
  own `bullet_string` is inert. `counter_style` is the `CounterStyle` enum
  (`decimal`, `lower-alpha`, `upper-alpha`, `lower-roman`, `upper-roman`; the
  alpha styles are bijective base-26, so `z` is followed by `aa`). All four
  cascade item → list block → label → job → `job-config.json` (plate parity);
  `counter_start_index` is `ge=1`.
- `list_marker_gap_fraction`: Float air gap between a list marker and its
  text, as a **unitless fraction of the line's rendered cap height** (so it
  scales with the text size), `ge=0.0`, `default None` → **0.5**. The marker
  column is the widest marker of the list plus this gap, and every item's text
  (and every continuation line) starts at that column — the hanging indent.
  Explicit `0.0` is honored (text flush against the marker). Cascades item →
  list block → label → job → `job-config.json` (plate parity).
- `optimize_line_content_max_lines`: Int ceiling `N` on the consecutive lines
  an `optimize_line_content` group may use (`ge=1`, `default None` = **unset =
  no growth**). **Growth-only: this option can only ever ADD lines to a label —
  it never removes, blanks, or collapses one.** Unset reflows across the
  group's existing lines and adds none (the render probe is never called, so
  uncapped jobs stay bit-identical). When set, the reflow starts at the group's
  existing line count `G` (which is the plain reflow) and grows the group —
  inserting clone lines (the group's last line's typography) after its last
  line — while its lines still need horizontal compression, up to
  `min(N, word_count)`. `N <= G` leaves the cap inert (plain reflow + DEBUG);
  a group with no `max_h_compress` budget skips the probe renders (nothing to
  measure). A cap spent while compressed keeps the widest layout + WARNING.
  Declaring it on any one line applies it to the whole group (resolution fans
  the value out); two *different* values in one group, or a cap on a label
  whose lines never enable reflow, raise `LineContentConfigError`. Implemented
  as the M = G..N loop in `line_content.py`, driven by the shared `ScaleProbe`
  (lazy `render_label_to_plt` → `compression_by_line`). The default probe
  renders a clone with `auto_descending_character_adjustment=False`: it
  consumes only the horizontal scales, which the X phase computes before the
  Y-only relief runs (pinned by `TestReliefFreeScaleProbeEquivalence`), so the
  relief's swept-ink scan never runs on a probe. Cascades line → label →
  job → unset (plate parity only); `job-config.json` supplies the shop default
  (`null` = unset).

**TextLine-only** (never inherited by the label, the job or a plate; no
`job-config.json` counterpart — the `TextLine.placeholder` precedent):
- `new_line`: Boolean hard break (`default False`). When true, the line
  **begins on its own line**: it *starts* a new `optimize_line_content` reflow
  group, so the lines that follow it (which do not carry the flag) reflow
  **with it** rather than with the lines above it — the group's words fill the
  lines from the flagged one downward. A later flagged line starts a fresh
  group. Word order is never changed; only the line breaks move. The flag is
  **inert** when the line does not enable `optimize_line_content` (every line
  already begins on its own line then) — it never raises and never enables
  reflow on its own. The line cap stays **group-scoped**: the flagged line's
  own `optimize_line_content_max_lines` governs its group, and a cap declared
  on a line of one group never leaks into the next group (so the
  four-bullet shape — four flagged lines with caps 2, 2, 2, 3 — is valid,
  not a conflicting-caps error). Implemented in
  `line_content.py` by the **grouping** only: the DP partition, the growth
  loop and the probes are untouched. Group membership lives in **one** place,
  `resolution.reflow_groups()` (the shared source of truth — the resolution
  cap fan-out and the export pre-pass must agree, or a cap would be fanned
  out onto lines the pre-pass never reflows together). Grown (clone) lines
  never carry the flag: a clone continues its block, so it must not split it.

**LabelAttributes** (extends TextAttributes, cascades to LabelSpec only):
- `width`: Label width in inches; must be defined at label or job level (required; no longer auto-sized).
- `height`: Label height in inches; must be defined at label or job level (required; no longer auto-sized).
- `type`: Label boundary shape. Enum `LabelShape` with values `rect` (the
  default, the historical rectangle) and `round` (an ellipse with semi-axes
  `width / 2` and `height / 2` — a circle when the two are equal). Cascades
  label → job (plate parity, `job-config.json` key, code fallback `rect`).
  A round label routes to the adjacent ellipse-packing pipeline; declaring
  only one of `width`/`height` produces a circle of that diameter. See
  Circular/Elliptical Labels below.
- `margin`: Label safety margins (optional)
- `hole_margin`: Distance from hole edge to label edge (cascades: label → plate → job)
- `holes`: List of `HoleSpec` objects (diameter + location enum). Group
  locations `corners` (all four corners) and `sides` (left + right) are
  expanded in place into their atomic member holes at validation time
  (`HOLE_LOCATION_GROUPS`); downstream consumers only see the 8 atomic
  locations.

### Key Classes

| Class | Purpose | Validation Rules |
|-------|---------|------------------|
| `JobSpec` | Root job container | Requires exactly one label source: `labels` list, root-level `content`, a job-level `replacement_text_file`, or plate-level replacement files (mutually exclusive); `width`/`height` must be defined at job or label level (no longer auto-sized); a `round` label needs only ONE dimension (the other fills to a circle) |
| `LabelSpec` | Individual label definition | `count >= 1`; requires `content` (min 1 TextLine) OR `replacement_text_file` (mutually exclusive with `count`); `width`/`height` must be defined at label or job level (a `round` label needs only one); optional `plate_id` pins the label to a declared plate; a label-level `replacement_text_delimiter`/`replacement_text_file_comment_string` requires the label's own file |
| `TextLine` | Text content unit | Requires non-empty `text` string |
| `ContentEntry` | One `content` entry: a text line **or** a list block | Subclasses `TextLine` (`text` becomes optional); requires **exactly one** of `text` / `itemize` / `enumerate`; a `placeholder: true` item inside a list block is rejected (a list's items are authored, never substituted) |
| `ItemizeBlock` / `EnumerateBlock` | Nested bullet / enumerated list | `items` (min 1 `TextLine`) + the cascading list settings; both extend `TextAttributes`, so a block carries every typographic field and its items inherit them (item → block → label → job) |
| `PlateSpec` | Physical sheet definition | All dimensions `>= 0`; `width`/`height` are the usable pack area, offset from the material's top-left by `left_clearance`/`top_clearance` (both default `0.0`); optional `replacement_text_file` synthesizes labels pinned to this plate (requires job-level `width`/`height`/`text_height`) |
| `HoleSpec` | Drilled hole definition | Location (required; 8 atomic enum values: corners + edges, plus `corners`/`sides` group shorthands expanded at validation) + optional `diameter` (default 0.125", must be > 0) |
| `parse_yaml()` | Entry point | Returns validated `JobSpec` or raises `ValueError` |
| `expand_job_spec()` | Replacement expansion (substitution.py) | Called after `parse_yaml()`; flattens replacement-driven labels (job-, plate- or label-level) into static LabelSpecs |

### Horizontal Text Compression

Over-wide text lines are uniformly compressed horizontally (glyphs + spacing
scale together; Y is untouched) so they respect the label margins. This is
**opt-in** via `max_h_compress` in [0, 1] (default 0.0 = disabled), cascading
line → label → job (accepted on plates for schema parity only).

- `compute_horizontal_scale()` (resolution.py): pure scale-factor math,
  clamped to `[1 - max_h_compress, 1.0]`.
- `compress_line_to_width()` (label_renderer.py): applies the scale to a
  rendered LineCollection; called per-line by the render path
  (`_render_positioned_lines`, reached via `_render_text_local_with_bounds`
  and `_render_text_lines_by_pen`) before centering.
- Lines that already fit are never modified. If compression cannot fully
  resolve the overflow (limit too small), a WARNING is logged.
- The *applied* per-line scale is reported (not stored on the schema) via
  `RenderedLabel.compression_by_line` — see Applied Layout Reporting.

### Arc-Preserving Non-Uniform Transforms (`arc_fit`, `arc_preserve_compression`)

A PLT-extracted glyph is natively `PU`/`PD`/`AA`: its curves are **circular**
arcs. A uniform scale maps a circle to a circle, so `TextBlock.scaled` keeps
`ArcSeg` sweeps verbatim. A **non-uniform** map — horizontal compression
(`compress_x`), the cap-line clamp and descender relief (`scaled_y`), the italic
shear (`sheared`) — maps a circle to an **ellipse**, and an ellipse is not a
circular arc. Historically every one of those operations **polygonized** its
arcs into chord-bounded line segments, inflating a 34-arc `O` into ~84–128 line
segments (measured ~2.8 segments per arc across the shipped library) and
lengthening the engraved toolpath. `plt_optimizer/generate/arc_fit.py` refits
each arc's ellipse image with a short chain of **circular** arcs instead, so the
compressed/italic/clamped line keeps its native `AA` commands and engraves
faster. This is exactly what EngraveLab/Vision Pro do (see
`tests_deps/arc_compression/README.md`, the reference exports that pin the
contract).

- **The math** (the whole feature rests on it): applying an affine map to the
  circle `z(t) = c + r*(cos t, sin t)` gives the *parametric* ellipse
  `P(t) = C + cos(t)*U + sin(t)*V`, where **`t` is the source center angle, NOT
  the eccentric angle**. That identity is what makes the fitted chain chain
  tip-to-tail: `P(t0)` is algebraically the point-wise affine image of the
  source arc's start, so the chain's endpoints match the neighbouring
  transformed segments exactly (verified to 1e-15). The three maps, in
  `ellipse_from_arc`:

  | transform | `C` | `U` | `V` |
  |---|---|---|---|
  | `compress_x(s, anchor)` | `((cx−anchor)·s+anchor, cy)` | `(s·r, 0)` | `(0, r)` |
  | `scaled_y(f)` | `(cx, f·cy)` | `(r, 0)` | `(0, f·r)` |
  | `sheared(tan)` | `(cx + cy·tan, cy)` | `(r, 0)` | `(tan·r, r)` |

  The shear is the general case (a **rotated** ellipse), which is why `U`/`V`
  are complex vectors, not axis semi-diameters. All three have positive
  determinant, so every fitted arc's sweep sign equals the source's
  (`_sweep_sign_ok` asserts it and declines the fit on disagreement).

- **The fitter** (`fit_ellipse_arc`): splits the parametric span into `k`
  sub-arcs, each a circular arc whose centre is the intersection of the
  **ellipse normal at its start** with the **chord's perpendicular bisector**;
  every later centre chains along the shared normal (`prev_center → junction`),
  so consecutive arcs meet with equal tangents — the chain is **C0 + G1**
  (tangent-continuous) with exact tip-to-tail endpoints. A plain 3-point
  circumcircle fit is only C0 and kinks up to ~11° at interior junctions. `k`
  is the smallest value meeting `ARC_FIT_DEV_TOL_INCHES` (0.0002in), estimated
  closed-form from the max curvature radius then verified numerically. A
  **degenerate-span check** runs first: a span whose endpoints sit within half a
  plotter unit (the emission floor) carries no resolvable ink and emits as a
  single `PD` vertex. Near-straight arcs are **not** collapsed — an earlier
  version collapsed every arc whose sagitta fell under the tolerance (34 of a
  compressed `S`'s 44 arcs became line segments), which polygonized the very
  toolpaths this module exists to keep native. EngraveLab keeps arcs as arcs
  through the same compression (44 → 49 `AA` on that `S`), and an `AA` whose
  deviation from its chord is below coordinate quantization draws through the
  same integer plotter units as the chord — preserving it costs nothing on the
  plate and keeps the preview arc-native.

- **Measured on the shipped library** (59,670 arcs, median sweep ~10°, max 90°,
  none past 180°): **~1.07 emitted segments per source arc** at 0.0002in
  (all arcs x scales 0.5/0.3) — 95% need exactly one (arc or chord); the 90°
  arcs split into six, the measured maximum is 10 (`ARC_FIT_MAX_ARCS`=12).
  **2.1–2.8× fewer
  emitted commands** than flattening at equal tolerance. Because a chord
  collapse and a 1-arc refit are each a single command, the command-count win
  is unchanged by the arc-native policy — only the AA/PD mix shifts (a
  preserved line's `AA` count now tracks the source's, ~1:1, matching
  EngraveLab). The **emitted** deviation (integer plotter
  units, `AA` radius implicit from `|pen − center|`) sits at ~0.002in ≈ 2 units
  at *every* tolerance: coordinate quantization, not the fit, is the engraving
  floor. 0.0002in is the **EngraveLab-parity** constant — measured on the
  reference exports, its compression splits exactly the arcs whose 1-arc fit
  deviates from the true ellipse by more than ~0.0002in (the compressed `S`'
  three 19–26° arcs: 44 → 47–49 `AA`), so the tighter tolerance reproduces
  EngraveLab's arc count and sub-quantization curve shape (preview fidelity),
  at a small command-count cost on the widest arcs.
  Swept-box over-measure of the fitted arcs is ≤0.00022in, so
  `bounds()`, `compression_by_line`, kerning and collision are unaffected.
  `ARC_FLATTEN_CHORD_TOL_INCHES` (0.0002in) stays for the flatten fallback and
  the `Ink`/preview polyline views.

- **Enablement**: `arc_preserve_compression` (bool, default **true**, cascades
  line → label → job → `job-config.json`, plate parity, NOT
  `REQUIRED_WHEN_UNCONFIGURED`) lands on `ResolvedTextLine`. `False` restores
  the historical polygonized output **bit-identically** (the escape hatch is
  exact, not approximate). Threading: `label_renderer._render_line_core_once`
  adds the `preserve_arcs` kwarg to the PLT render call **only when the line
  opts out** (the three-tier empty-kwargs gate, so a default-True line keeps
  the historical call shape); `compress_line_to_width` /
  `_apply_collision_compress` / `_apply_glyph_ramps` take it from the line;
  `plt_font_renderer` carries keyword-only `preserve_arcs: bool = True` on
  `_walk_line` + the three public wrappers, forwarded to `_capline_geometry`
  and `_shear_geometry`.

- **The primitives** (`text_geometry.py`): `ArcSeg._xform` / `Stroke._xform`
  return `(segments, declined_arcs)`; `declined_arcs` is 1 **only** when the
  refit returned `None` (a degenerate geometry, a span past
  `ARC_FIT_MAX_ARCS`=12) and the arc flattened. Near-straight arcs stay native
  `AA` (only a sub-half-plotter-unit span collapses to a chord), so a
  preserved line's arc count tracks the source's. This is the metric that gates the log:
  `compress_x` warns on the **declined** count (WARNING — compression is an
  authoring compromise), `scaled_y`/`sheared` log the declined count at DEBUG
  (routine render-time transforms). The public `scaled_x`/`scaled_y`/`sheared`
  return `Tuple[TextSegment, ...]` (no signature churn for callers).

- **Stroke count is preserved 1:1** by every refit (a `Stroke` maps to one
  `Stroke`), so `glyph_groups`, `TextChunkRecord.glyph_groups` and the
  intra-chunk glyph sweep stay valid — the same guarantee the exact
  `scaled`/`translate` transforms give. Only the **segment count within a
  stroke** can grow (a split arc contributes k segments to its one stroke). The
  descender relief splices via `replaced_strokes` at the original stroke
  indices, and the capline clamp rebuilds the per-glyph block whole.

- **Reference parity** (`tests/test_arc_compression_parity.py`): the fixtures in
  `tests_deps/arc_compression/` pin (1) the compressed outline is the exact
  affine image (two-sided Hausdorff ≤0.02in) and (2) EngraveLab's arc count
  grows ≤1.25× under compression (it never polygonizes). Per-arc index pairing
  is deliberately NOT asserted — EngraveLab re-tessellates the whole outline, so
  the files compare **as curves**.

- **Arc-native label borders** (`label_renderer._render_boundary_hpgl`, always
  on): the ellipse (`type: round` / the fully-rounded limit) and the
  rounded-rectangle contour emit as `AA` chains built from the same fitter —
  the ellipse as four PU-led quadrant strokes (`_ellipse_boundary_strokes`),
  the rounded rect as one closed stroke of four straight edges + four
  `fit_superellipse_arc` patches. The superellipse needs the **trim-to-line**
  treatment (`fit_superellipse_arc`): for `n > 2` the patch's axis vertices
  have zero curvature (the G2 property), so a full-quadrant fit's `k` estimate
  diverges and the normal construction degenerates; the near-flat parameter
  spans at each vertex (bisected so each cap's chord deviation stays within
  tol) emit as short line segments, and only the inner span refits with the
  circular chain. The whole contour (caps + chain) is verified at 400 samples
  (`_contour_deviation`) — the 32-sample verifier under-reads the
  vertex-weighted superellipse by up to ~15% — with one half-tolerance retry
  before declining. Border chains use `BORDER_ARC_FIT_MAX_ARCS` = 32 per
  quadrant/patch (the glyph cap 12 is too small: a border quadrant sweeps 90
  degrees of a label-size ellipse). Measured at 0.0002in: ellipse 4x2 = 40
  arcs (was ~316 polygon vertices), circle = 4 exact quarter arcs, squircle
  4x2 f=0.5 = 52 arcs (was ~520 vertices), stadium 8x4 = 108. A declined fit
  (extreme aspect) falls back to the polygonized contour with a logged
  WARNING (never silent). The sharp rectangle stays the four-corner polyline
  bit-identically; `_render_boundary_local` and every margin/relief/inset
  path (`_border_box`, `_lower_ellipse_arc`, `_lower_rounded_rect_border`,
  `rounded_rect_content_inset`) stay polygonized by contract — polygonized
  boundaries are fine for margins, the ENGRAVING toolpath is arc-native.
  Bounds: each quadrant/patch starts on an axis vertex, so the PU coordinates
  keep the contour on the nominal box; an emitted `AA` carries no radius (the
  plotter derives it from the integer pen/centre pair), so the measured swept
  box lands within ~2 plotter units (0.002in) of nominal — the emission floor
  documented in `arc_fit.py`, pinned at `abs=3e-3` by the demo-fixture bounds
  tests. Emission requires the `_collect_hpgl_geometry` **pen-advance** fix
  (an `AA` advances the tracked pen to its derived end, matching the core
  parser): without it, a chained `PD;AA` run measures every later arc's radius
  from the chain start and over-measures a 40-arc ellipse border by ~0.4in,
  breaking the nominal-box contract.

- **Out of scope**: TTF arc fitting (TTF blocks are all `LineSeg`, unaffected);
  the font library itself (glyphs stay as extracted);
  `Affine._require_similarity` in `fonts/glyph_transforms.py` (an
  extraction-time dev tool, correct to reject arcs — the derived-glyph recipes
  are all similarities); the `optimize`/`watch` pipelines (they pass arcs
  through and already handle `ArcSegment` end-to-end).

### Alphanumeric Cap-Line Enforcement (`enforce_cap_line`)

`enforce_cap_line` (bool, default **true**, cascades line → label → job →
`job-config.json`, plate parity) clamps every **ASCII alphanumeric**
`[0-9a-zA-Z]` glyph whose ink extends *beyond* the per-font capline: the
glyph is downscaled **top-down** — a vertical-only scale about its own lowest
ink point, `y' = min_y + (y - min_y) * factor` with
`factor = (cap - min_y) / (max_y - min_y)`, X untouched, so the aspect ratio
changes — and its top lands exactly on the capline. Non-alphanumerics
(punctuation, spaces, derived Unicode symbols) are never clamped; a glyph
sitting exactly at the capline (the reference char) never moves. The gate is
ASCII-only: `é` keeps its overshooting accent. A symbol font's ASCII slots ARE
alphanumeric characters, so Wingdings symbols clamp too (the contract keys
off the character, not the font family).

- **Strictly pre-markup**: the clamp applies to the raw glyph, FIRST in the
  per-character transform chain — before the small-caps, bold/`<scale:N>`,
  italic and `<sup>`/`<sub>` transforms, so every markup transform composes on
  the clamped silhouette (`<sc>` shrinks the clamped capital; a bare `<sup>`
  centers the clamped glyph on the capline).
- **PLT** (`plt_font_renderer._capline_geometry`): applied inside `_walk_line`
  right after `_glyph_entry`, keyed by the character's own font frame's
  `normalized_ref_height` (the `<f:>` frames tuple carries it). The block,
  bounding box **and both profile envelopes** transform together, so kerning
  measures the clamped silhouette. The Y-only scale maps each `AA` arc to an
  ellipse arc, which the refit keeps native as circular `AA` arcs (see
  Arc-Preserving Non-Uniform Transforms, the `TextBlock.scaled_y` policy, DEBUG
  log only on the arcs the refit declines) — shipped sheets overshoot on
  digits/round capitals, so enabling the clamp by default raises the emitted
  arc counts of every affected fixture (the clamp no longer polygonizes).
- **TTF** (`ftext_renderer._apply_cap_line_enforcement`): applied right after
  the normalization scale (the reference `H` is exactly `target_height_inches`
  tall there) and before `_apply_small_caps`/`_apply_italic`/
  `_apply_glyph_scales`/`_apply_baseline_shifts`, through the per-glyph
  contour-slicing idiom; the `with_words` gate routes clamped lines to the
  exact slicing path (the signature matcher cannot match clamped glyphs). The
  bundled CAD font's ASCII capitals/digits sit at/below the cap (no-op) while
  its ascender lowercase overshoots (`b/d/f/h/k/l` ~1.096, `i/j` ~1.154 of the
  cap) and clamps.
- The clamp is a geometry transform like italic: it never moves
  `nominal_text_height` / `toolpath_text_height`, the cutter, the pen map, or
  the emitted file names. `measure_line_natural_width` /
  `measure_line_vertical_extents` route through `_render_line_block`, so the
  reflow/compression/extents pre-passes see the clamped geometry for free.
- Fixture: `tests_deps/enforce_cap_line_job.yaml` (pinned by
  `tests/test_phase3_export.py::TestEnforceCapLineDemoExample`) — Dino digits
  (clamped vs opt-out twin), exact-cap flat capitals (bit-identical), round
  capitals, the pre-markup `<sup>` ordering (vs its enforcement-off twin),
  small caps, the Wingdings character-based contract, the `(` punctuation
  exemption, and the TTF ascender pair. Unit tests:
  `TestCapLineGeometryHelper` / `TestWalkLineCapLineEnforcement`
  (`tests/test_plt_font_renderer.py`), `TestApplyCapLineEnforcement`
  (`tests/test_ftext_renderer.py`), `TestEnforceCapLineDispatch`
  (`tests/test_label_renderer_gaps.py`), `TestEnforceCapLineCascade`
  (`tests/test_resolution.py`) and `TestEnforceCapLineField`
  (`tests/test_schema.py`).

### Physical vs. effective cutter diameter (`cutter_buffer_text`, `cutter_buffer_routing`)

A cutter is two different numbers, and mixing them up is the one mistake this
contract exists to prevent.

- **Physical** = the tool in the spindle (`ResolvedTextLine.cutter_diameter`,
  `ResolvedLabel.hole_cutter_diameter`). It is an *inventory identity*: a
  0.030in cutter must always produce `30_txt_*.plt`.
- **Effective** = `physical + buffer`
  (`resolution.effective_cutter_diameter()`), the **width of material
  removed**. Exposed only as derived properties —
  `ResolvedTextLine.effective_cutter_diameter` /
  `effective_bold_cutter_diameter` / `effective_per_char_cutters` /
  `effective_used_cutters` / `effective_max_cutter_diameter` and
  `ResolvedLabel.effective_hole_cutter_diameter` — never stored, so a
  pre-pass that clones a line and the reflow that rewrites its text can
  never desynchronise the two.

| Mechanism | Uses | Why |
|---|---|---|
| `get_cutter_diameter` / `snap_boundary_hole_cutter` / `next_smaller_cutter` / `next_larger_cutter` | physical | They select a TOOL the shop owns. |
| `build_cutter_pen_map`, `used_cutters`, `used_layers`, `per_char_cutters`, the pen map, the per-cutter PLT file names | physical | Pen/file identity is the tool. |
| `cutter_downsize` ladder walk, `should_downsize_cutter` midpoint ratios, `cutter_downsize_by_line` | physical | The ladder is the shop's real tool list. |
| `toolpath_text_height`, `bold_toolpath_text_height`, the `CutterSizeError` material guards, the `character_spacing` fallback (`effective * 1.5`), `glyph_render_plan`, the renderer `cutter_diameter(s)` kwargs, cross-font kerning clearance, the underline/strikethrough geometry, `cutter_margin_h`, `collision_threshold`, `hole_circles_local`, `hole_engraved_radius`, `_round_clearance`, `_line_cutter`, `_border_required`, the PDF `pen_widths` | **effective** | Every one measures INK. |

**Text-height compensation.** `toolpath_text_height = nominal_text_height -
effective_cutter_diameter`, so the engraved silhouette stays exactly
`nominal_text_height` (the design contract: a 0.25in label engraves 0.25in
tall) while the centerline geometry shrinks by the buffer. The buffer is
universal: it applies to an explicit `cutter_size` and inline `<c:D>` exactly
like an automatic cutter (both name a TOOL).

**The decoration trap.** `_run_cutter` feeds *two* consumers with *different*
requirements: the underline geometry (ink) and the `(cutter, color)` pen layer
returned by `_underline_run_layers` / `_strikethrough_run_layers`, which
`_stroke_layer_map` also *recovers* from the stroke's Y
(`-y - underline_offset_inches`). `_run_cutter` therefore stays **physical**
(pen identity + a consistent Y-inversion) and
`label_renderer._run_effective_cutter` supplies the **effective** width for the
drawn geometry; the recovery site subtracts the run's own buffer (the
`<cb:D>`-aware span lookup in `_underline_run_layers`, the line scalar when
the line carries no override) before snapping
to `known`. Skipping the subtraction lets a large buffer snap an underline to a
neighbouring rung the run never engraves with, landing its strokes on the wrong
pen/PLT file.

**PDF previews.** The per-cutter file names carry the physical token, so the
mirrored previews cannot derive the engraved width from the stem:
`export_per_cutter_plts` builds `cutter_widths` (physical → engraved, the
widest buffer among the lines sharing a tool) for the pen-keyed `pen_widths`
and a path-keyed `file_widths` for the per-file plots (a text pen and the `bh`
pen can share a nominal size while carrying different buffers, which is why the
two maps are separate). `_write_simple_plots(file_widths=...)` prefers it over
`_stem_cutter_inches`.

**Per-stroke preview widths (`<cb:D>` visibility).** The emitted HPGL carries
no width channel, so the pen/file-keyed widths above alone cannot show a
`<cb:D>` span: the tag never changes the physical tool, so the span shares
its pen with the surrounding text, and a per-pen width (the pen's widest
engraved stroke) inflates the whole pen. The previews therefore consult a
per-stroke width map: `label_renderer._stroke_width_map` records each stroke's
engraved width (glyph strokes from `effective_per_char_cutters`, an underline
stroke inverts its centerline Y, a strike stroke takes its run's
`_run_effective_cutter`), the array rides `TextChunkRecord.stroke_widths`
through the pen split, and `plate_optimizer.build_stroke_width_map` re-keys it
into the PARSED device stream (`segment_width_key`): line segments by
normalized integer endpoint pairs (direction-agnostic, so the sweeps'
reversals and the merger's tip-to-tail stitching still match — chained
through `_path_line_keys` because the parser derives a `PD` start from the
previous `AA`'s float end, not the emitted integer), arcs by
`(int center, |sweep|)` — the traversal invariants the emission carries
exactly (a reversed traversal negates the sweep and re-chains the endpoints a
unit off; center + magnitude survive both).
`_write_simple_plots` splits each parsed pen/file into consecutive
constant-width `PhysicalStrokeLayer`s by that lookup, falling back to the
per-pen/per-file width for every segment the map does not cover (structural
content, synthetic exports). The optimized per-file stream keys exactly (the
plate transform and the emission share their rounding); the combined proof
parses the un-optimized assembly, whose vertical centering applies its float
adjustment to the integer device Y at a different rounding point than the
plate transform, so a line's endpoints land up to one plotter unit off (the Y
drift propagates through the parser's AA chain into X, and every `AA` center Y
takes its own rounding of the shift). The lookup therefore probes +/-1 unit in
increasing total drift, accepting only a UNIQUE hit at the first drift level
that hits (an ambiguous level falls back to the pen width -- glyph geometry is
dense, so a union over all levels is ambiguous almost everywhere). Lines probe
their normalized endpoint pairs; arcs probe their `(center, |sweep|)` key on
the CENTER (`arc_width_key`) -- |sweep| is carried verbatim through the
assembly, and an arc's endpoints re-chain from the drifted centers, so the
center is the stable identity (an arc endpoint probe would miss: a reversed
traversal re-chains endpoints a unit off on top of the center drift). The map
is NOT gated on the tag: every rendered line contributes its
own per-stroke widths, so a line that merely shares a tool with a buffered
span keeps its own (narrower) width in the proof. A job whose buffers are all
zero maps every stroke to its physical cutter, which equals the pen width --
the previews stay visually identical. Pinned by
`tests/test_phase3_export.py::TestPerStrokePreviewWidths` (both the optimized
and un-optimized paths, the line and arc drift probes), `tests/test_plate_optimizer.py::TestBuildStrokeWidthMap`
(map keys survive emission + parsing, arcs, rotation, reversal) and
`tests/test_label_renderer_gaps.py::TestStrokeWidthMap`.

**Known approximation: the multi-stroke boldness ladder.**
`multi_stroke_cutter_diameter` sizes the per-pass **tool** so the `N` passes
total `text_height * target_boldness_factor` (`W = target - (N-1) * d`), and
the result is snapped to the inventory and becomes the pen/file identity. With
a non-zero buffer each pass engraves `W + buffer`, so the engraved boldness
lands `buffer` heavier than the requested factor. Deliberate, and verified:
compensating (`W = target - (N-1) * d - buffer`) is a **no-op at the shop
default** `target_boldness_factor: 0.14`, where the design point sits exactly
ON the gap-free floor `d / (1 - multistroke_overlap_ratio)` — the subtraction
drops `W` below the floor and the floor clamps it straight back (measured
headroom at 0.14 is `0.00000` for N=3/5/7; the floor only leaves room for
factors above 0.14). So the compensation buys nothing where the shop engraves
and only thins strokes at heavier settings, while the buffer's own purpose
(modelling the extra ink) is served by the effective widths the passes are
measured with. `multistroke_overlap_ratio` keeps guarding the floor on the
physical diameters.

**Zero buffer = byte-identical legacy output.** `0.0` makes
`effective_cutter_diameter` the identity, and every site above degrades to its
historical expression. The code defaults are `0.0` (so the no-config path is
unchanged); the shipped `job-config.json` carries the shop's measured values.
Pinned by `tests/test_phase3_export.py::TestZeroBufferRegression`.

**Inline cutter buffer `<cb:D>` is implemented:** the fourteenth shipped
markup tag lets individual runs override the line's `cutter_buffer_text` scalar
per character. See `markup/AGENTS.md` "Inline Cutter Buffer" for the full
contract. The effective-width properties (`effective_per_char_cutters`,
`effective_used_cutters`) use per-character buffers when any `<cb:D>` span is
active; lines without the tag are bit-identical to the pre-feature baseline.

### Multi-Stroke (Bold) PLT Fonts (`target_boldness_factor`, `multistroke_overlap_ratio`)

A PLT sample sheet may be engraved with **N parallel cutter passes per glyph**
(a bold font). The naming convention carries the count:
`<font name>_<text height in>_<ref char>_S<n>.plt` (e.g.
`avenge5_0.55_E_S5.plt`); omitting `_S<n>` means a single-stroke font. The
extractor stores the engraved passes **verbatim** — the sheet is the source of
truth for the bold shape — and records the count as the per-font
`stroke_count` key in its `fonts/VP-EL-fonts/catalog/` file. The renderer therefore emits the
stored geometry unchanged (arcs stay native, envelopes/glyph groups untouched);
the stroke count drives **cutter sizing only**.

- `fonts/extract_plt_fonts.py::parse_font_file_name` strips the trailing
  `_sym`/`_symbol` (case-insensitive) and `_S<n>` suffixes (in a loop, so
  either order parses; canonical is `_S<n>_sym`) **before** the existing
  `rsplit("_", 2)` (so font names with underscores and
  symbol reference chars like `^` keep working) and returns
  `(name, height, ref_char, stroke_count, symbol_font)`. `_S0` is a
  `FontExtractionError`; a non-numeric suffix (`_Sx`) is not stripped and
  fails the normal name parse. `extract_font_file` writes
  `"stroke_count": n` and `"symbol_font": bool` into the font entry.
- `font_registry.PltFontData.stroke_count: int = 1` (lenient parse: missing /
  garbage / `< 1` → 1 with a WARNING, so pre-convention libraries keep
  loading). `font_registry.plt_font_stroke_count(name)` is the
  resolution-time lookup: case-insensitive, strips a trailing ` SC`
  small-caps variant, and returns **1** for TTF/unknown fonts — multi-stroke
  sizing is a PLT-font feature.
- The framing verification is **jitter-tolerant** (`framing_points_match`):
  multi-stroke glyphs stack passes at tied coordinates, so a sub-unit
  engraving jitter reorders the lexicographic point sort and a positional
  comparison reports a phantom deviation of hundreds of units. Each point is
  matched to its nearest partner over a spatial hash (cell
  `_FRAMING_MATCH_GRID`) in both directions; `REFERENCE_MATCH_TOLERANCE`
  (5e-3 units) covers the parser's 3-decimal rounding in the
  left-bottom-relative frame, while a genuine geometry difference (a wider
  framing glyph, a mis-split row) is tens to hundreds of units.
- **The ladder** (`resolution.multi_stroke_cutter_diameter`, matplotlib-free):
  the passes sit at `design_pass_spacing` = `(1 - DESIGN_MULTISTROKE_OVERLAP)
  * text_height * 0.14 / (1 + (N-1) * (1 - DESIGN_MULTISTROKE_OVERLAP))`
  (the spacing baked into the sheet at the design point), so the engraved
  width is `W + (N-1) * d` and the boldness target inverts to
  `W = text_height * target_boldness_factor - (N-1) * d`. The
  `multistroke_overlap_ratio` floor raises `W` to `d / (1 - ratio)` when the
  target asks for a thinner cutter (adjacent passes must overlap, so no
  unengraved stripe appears). At the defaults (`0.14` / `0.25`) the ladder
  equals the classic `text_height * 0.14 / (1 + (N-1) * 0.75)` exactly — the
  design point sits on the floor, and the TODO's rules of thumb (≈5.6/3.5/2.5%
  of text height for N=3/5/7) are its output. A heavier factor grows the
  cutter and the **implicit** overlap `1 - d/W` with it (the escape hatch
  past the floor); a lighter one clamps to the floor (WARNING).
- `_resolve_content` resolves the font name **before** the cutter block (the
  two cascades are independent) and, when no explicit `cutter_size` is set and
  `stroke_count > 1`, runs the ladder, snaps with
  `snap_boundary_hole_cutter`, and **steps up** one rung
  (`next_larger_cutter`) when the snap-down lands below the gap-free floor
  (WARNING). An INFO names the font, stroke count, factor, floor, cutter and
  implicit overlap. `toolpath_text_height = nominal - cutter` and the
  `CutterSizeError` guard are unchanged; the ladder cutter is always below
  the text height at the schema bounds.
- **Everything downstream is free**: `cutter_diameter` is already per-line
  end-to-end, so the kerning clearance, the text-hole/underline/descender
  stroke floors, `build_cutter_pen_map` and the per-cutter PLT files all see
  the ladder's tool. The floors are *exact*: the stored geometry is the bold
  pass centerlines, so the outermost pass's engraved stroke extends exactly
  `0.5 * cutter_diameter` past the ink. A multi-stroke line also participates
  in the `cutter_downsize` ladder normally (it reads `cutter_diameter`).
- Fixture: `tests_deps/multistroke_job.yaml` (pinned by
  `tests/test_phase3_export.py::TestMultistrokeDemoExample`) — the S1 control
  (Dino, plain height lookup), Avenge5 S5 (snap-down breaks the floor →
  stepped up), Helter2 S2, a heavier factor (bigger cutter), a lighter factor
  (clamped to the floor, WARNING), and an explicit `cutter_size` on the S5
  font (ladder bypassed).

### Compression-Driven Cutter Downsizing (`cutter_downsize`)

A line that must be squeezed horizontally is a line the automatic cutter is
too fat for. `cutter_downsize` (default **true**) + `max_cutter_downsizes`
(default **1**) let the export pipeline reduce the line's **automatic**
cutter when its *effective* horizontal scale (margin scale × collision
scale) falls below the midpoint between the current cutter and the next
smaller `tools.json` `available_cutters` rung (0.100in current, 0.080in
next: midpoint 0.90 — scale 0.89 triggers, 0.90 keeps; the tie keeps the
current tool). The toolpath height grows to `text_height - smaller_cutter`
(nominal `text_height` kept as user intent, matching the `cutter_size`
contract). An explicit `cutter_size` disables the reduction for that line.
Both fields cascade line → label → job → `job-config.json` default (plate
parity only); `max_cutter_downsizes: 0` disables the mechanism.

- `plt_optimizer/generate/cutter_downsize.py` runs as an **export pre-pass**
  (`apply_compression_cutter_downsize`) inside
  `vectorize.export_per_cutter_plts`, *after* `apply_global_h_compress` and
  *before* `build_cutter_pen_map`: it
  renders each label once through an injectable `ScaleProbe` (default:
  lazy-imported `render_label_to_plt`, reading
  `RenderedLabel.compression_by_line`), and emits adjusted `ResolvedLabel`
  clones (`dataclasses.replace`, mirroring the collision-avoidance pattern)
  carrying the reduced cutter + grown toolpath height +
  `ResolvedLabel.cutter_downsize_by_line` (line index → (original, final)).
  The pen map, per-cutter layers and PLT file names therefore all see the
  tool actually used (the downsized line lands in its own file).
- **The pre-pass order matters:** the shared compression runs FIRST, so the
  midpoint trigger measures the **shared** scale (the scale the label is
  actually engraved at), not the trigger line's natural scale. The same
  nominal height therefore gets the same compression AND the same tool.
- **The run-cutter clamp (a `<f:...>` run follows its line).** A font-only
  run keeps the line's nominal height, so its cutter is capped at the line's
  own resolved tool — the tool the downsize moves. The clamp lives in
  `resolution.scale_cutter_ladder` / `_scale_cutter_ladder_cached` (a
  `line_cutter` ceiling, applied only at `scale == 1.0`), and
  `ResolvedTextLine.per_char_cutters` + `label_renderer._font_segment_line`
  both pass the line's cutter, so the pen map, the kerning floors, the
  underline split and the cross-family composer all see the same tool. A
  multi-stroke run font keeps its **gap-free floor** through the clamp (the
  floor may pull the cutter back up to the smallest rung at or above it, so a
  symbol run never reopens an unengraved stripe). A `<scale:N>`/`<sup>`/
  `<sub>` run has a height of its own and keeps its own ladder (unchanged).
- **Monotone step loop:** each step swaps one rung and re-measures the
  re-rendered line (a smaller cutter renders *wider* — bigger toolpath
  height and wider kerning clearance — which can deepen the compression and
  justify a further step, up to the budget). The cutter strictly decreases
  every step and a line never regains a larger cutter within the same render
  (no oscillation); a step whose candidate cutter leaves no material
  (`nominal - candidate <= 0`) is refused.
- **Scope guards (free no-ops):** no `available_cutters` inventory (the
  ladder is the shop's real tool list); no eligible line; every line's
  `max_h_compress` budget `0.0` (both compression mechanisms need budget
  > 0). `export_per_cutter_plts` gains an `available_cutters` kwarg
  (default `None` ⇒ pre-pass inert ⇒ historical output bit-identical); the
  CLI and integration runner thread the `tools.json` inventory in.
- Pure math lives in resolution.py (matplotlib-free): `next_smaller_cutter`
  (largest inventory rung strictly below current) and
  `should_downsize_cutter` (midpoint rule, `1e-12` tie guard).
- Each swap logs a WARNING naming the label id, line index, text, scale and
  both cutters; the layout report (`layout_report.py`) prints a per-line
  `cutter 0.060in -> 0.045in (downsized for compression)` finding.
- **Per-label sharing (`cutter_downsize_global`, default true):**
  `apply_compression_cutter_downsize` runs a fixpoint pass per label after the
  per-line reduce loop (`_share_downsizes`): each `nominal_text_height` group
  containing a sharing trigger converges to the group's smallest final
  cutter; receivers get the shared cutter applied directly (seed step,
  midpoint check bypassed, toolpath floor still enforced), consume one unit
  of their own budget, and continue their own monotone loop — a receiver
  whose compression deepens below the shared tool lowers the group minimum
  and the pass repeats. Termination: every step strictly decreases some
  line's cutter on a finite ladder within each line's budget. Propagated
  swaps land in the same `cutter_downsize_by_line` map (original = the
  receiver's pre-swap tool), so the pen map, per-cutter layers, PLT file
  names and the layout report see them for free; each propagation logs a
  WARNING naming the label id, line index, text and both cutters. The flag
  rides resolved lines, so no export/CLI threading changes are needed.
- **Job-wide sharing (`cutter_downsize_global_job_wide`, default false):**
  the same mechanism widened from the label to the job — for the shop's
  many-single-line-labels jobs (e.g. the Sunwest hot-plate run) per-label
  sharing is inert because every height group has a single member, so the
  same-height labels split across two per-cutter text layers whenever their
  compressions straddle the midpoint. After the per-label loop,
  `apply_compression_cutter_downsize` runs `_share_downsizes_job_wide` (only
  when at least one downsized line enables the flag — the default pays
  nothing and stays bit-identical): each job-wide `nominal_text_height`
  group (built by `_job_height_groups` over all labels) containing a
  job-wide sharing trigger converges to the group's smallest final cutter,
  with the same seed-step contract (midpoint bypassed, toolpath floor
  enforced, own budget consumed, own monotone loop after) and the same
  termination argument. Receivers land in their own label's
  `cutter_downsize_by_line`, so the pen map, layers, file names and the
  layout report see them for free; each propagation logs a WARNING naming
  the label id, line index, text and both cutters ("sharing the job's cutter
  downsize"). Job-wide sharing requires `cutter_downsize_global` true (the
  per-label permission gates both scopes); a line with the job-wide flag
  false keeps exactly the per-label behavior in both directions. Pinned by
  `tests/test_cutter_downsize.py::TestJobWideSharing` and
  `tests/test_phase3_export.py::TestCutterDownsizeGlobalJobWideDemoExample`
  (fixture `tests_deps/cutter_downsize_global_job_wide_job.yaml`).
- Accepted staleness: `_resolve_auto_line_spacing` / `_fit_content_to_margins`
  size vertical spacing against the *original* toolpath height; the
  render-time `fit_line_spacing_to_margins` re-clamp preserves the margins.
- Example fixture: `tests_deps/cutter_downsize_job.yaml` (pinned by
  `tests/test_phase3_export.py::TestCutterDownsizeDemoExample`) — one
  downsized line (0.06→0.045), one fitting line, one explicit-cutter line
  (never reduced) and one `cutter_downsize: false` opt-out.

### Per-Label Shared Horizontal Compression (`h_compress_global`)

Two same-height lines in one label can end up engraved at visibly different
glyph densities — the over-wide line compressed, its short sibling left at
natural width. `h_compress_global` (default **false**) makes compression
label-wide: a compressed line's scale is applied to every other *eligible*
line of the same `nominal_text_height` **within the same label**, so one label
engraves one text size at one density. Cascades line → label → job →
`job-config.json` default (plate parity only); `h_compress_global: false` opts
a line out in both directions (it neither shares its own compression nor
receives a sibling's).

- `plt_optimizer/generate/h_compress.py` runs as an **export pre-pass**
  (`apply_global_h_compress`) inside `vectorize.export_per_cutter_plts`,
  *before* `apply_compression_cutter_downsize` and *before*
  `build_cutter_pen_map`: it renders each label once through the shared
  `ScaleProbe` (lazy-imported `render_label_to_plt`, reading
  `RenderedLabel.compression_by_line`) and emits `ResolvedLabel` clones
  (`dataclasses.replace`) carrying `global_compress_by_line` (line index →
  shared scale).
- **The pre-pass order matters:** the shared compression runs FIRST, so the
  cutter reduction's midpoint trigger measures the **shared** scale — the
  scale the label is actually engraved at. The same nominal height therefore
  receives the same horizontal compression **and** the same tool (one text
  size, one density, one tool). (The reverse order left the reduction blind
  to the shared scale, so a label could be engraved at one density with two
  tools.)
- **Group scale:** the group's *most-compressed* (minimum) effective scale
  among its triggering eligible lines, clamped to each receiver's own
  `1 - max_h_compress` budget floor (`_shared_scale`). A line whose natural
  compression is already tighter keeps it (never stretched back). A single
  pass converges — the group minimum is fixed by the triggering lines' natural
  scales, so applying it to a fitting sibling never deepens any line's natural
  compression (no fixpoint loop, unlike `_share_downsizes`).
- **Grouping scope:** `h_compress_global_same_height_only` (default **true**)
  picks the groups — `_label_groups` returns one group per
  `nominal_text_height` (the default) or ONE label-wide group when the flag
  is **false** (every eligible line of the label shares the label's
  most-compressed scale, whatever its height). The flag is uniform across a
  label (it cascades from one source to every line), so the first eligible
  member's value decides the scope.
- **Eligibility:** `h_compress_global AND max_h_compress > 0.0` (no compression
  mechanism can fire on a zero-budget line, so it can neither trigger nor
  receive). A group needs ≥2 eligible lines to share; a lone eligible line's
  natural compression is its own business. The renderer applies the map as the
  second-pass margin target (`margin = min(natural, shared / collision)`), so
  the effective scale is `min(natural, shared)` and a trigger line's geometry
  stays bit-identical to the per-line behavior.
- **Scope guards (free no-ops):** no label has an eligible line; no triggering
  line in a same-height group with two eligible lines. The flag rides resolved
  lines, so no export/CLI threading is needed (unlike `available_cutters`).
- Each propagated line logs a WARNING naming the label id, line index, text and
  scale; the layout report prints it as an ordinary compressed line
  (`scale 0.858 (14.2% compressed)`) — the shared scale lands in
  `RenderedLabel.compression_by_line` for free.
- Example fixture: `tests_deps/h_compress_global_job.yaml` (pinned by
  `tests/test_phase3_export.py::TestHCompressGlobalDemoExample`) — a shared
  label, a per-label boundary, and an opted-out label.
- Each propagated line logs a WARNING naming the label id, line index, text and
  scale; the layout report prints it as an ordinary compressed line
  (`scale 0.858 (14.2% compressed)`) — the shared scale lands in
  `RenderedLabel.compression_by_line` for free.
- Example fixture: `tests_deps/h_compress_global_job.yaml` (pinned by
  `tests/test_phase3_export.py::TestHCompressGlobalDemoExample`) — a shared
  label, a per-label boundary, and an opted-out label.

### Word-Level Line-Content Reflow (`optimize_line_content`)

An over-wide line is a line the label's other lines can help: `optimize_line_content`
(default **false**) lets the export pipeline move **whole words** between the
consecutive lines that enable it, so the group's lines reach equal rendered
width (equal *distance*, not equal character count) and the label needs less
horizontal compression. Word order is never changed — only the line breaks move,
exactly like LaTeX paragraph filling. Cascades line → label → job →
`job-config.json` default (plate parity only); `false` opts a line out (a
disabled line breaks the group into independent neighbours).

- `plt_optimizer/generate/line_content.py` runs as an **export pre-pass**
  (`apply_line_content_reflow`) inside `vectorize.export_per_cutter_plts`,
  *first* — before `apply_compression_cutter_downsize` and
  `apply_global_h_compress`, so both compression mechanisms see the reflowed
  text and measure the reflowed widths. Equalizing the **pre-compression**
  widths is the same objective as minimizing the compression the label needs.
  It emits `ResolvedLabel` clones (`dataclasses.replace`) carrying the reflowed
  `content`; untouched labels keep their object identity.
- **Groups:** maximal runs of consecutive enabled lines
  (`resolution.reflow_groups()`, the single source of truth shared with the
  resolution cap fan-out); a disabled line breaks a run, a run of one line is
  a no-op, and a word may travel across the whole group (not just one line).
  A line carrying the line-level `new_line` flag additionally breaks the run
  **before** it, so the flagged line starts a fresh group.
- **DP repartition** (`_partition_words`): the linear-partition problem solved
  exactly — cut the concatenated word sequence into one contiguous segment per
  group line (every line keeps ≥1 word), minimizing the widest segment,
  tie-broken by the sum of squared widths. O(words² × lines). Segment width
  (`_partition_cost`) = Σ per-word widths + (count − 1) × space advance, using
  each line's own typography; kerning never crosses a word boundary, so the
  concatenation is exact for PLT fonts.
- **Width probe:** `LineWidthProbe = Callable[[ResolvedTextLine], float]`,
  injectable for tests; the default lazily imports
  `label_renderer.measure_line_natural_width` (whole-line render bbox width),
  keeping matplotlib out of module scope. Space advances are measured per
  typography by rendering a line's own first two words and subtracting the
  standalone widths (exact for PLT, matches matplotlib shaping for TTF),
  falling back to `space_width_fraction * toolpath_text_height +
  character_spacing`. Word widths are cached per typography.
- **Scope guards (free no-ops):** no enabled line; every enabled run is a
  single line; a run contains a blank line or has fewer words than lines
  (infeasible — every line must keep one word).
- Each moved line logs an INFO naming the label id, line index and the
  before/after text. The reflow is invisible to the layout report (the text
  itself changed, no scale/cutter finding), so no reporting field is needed.
- Example fixture: `tests_deps/optimize_line_content_job.yaml` (pinned by
  `tests/test_phase3_export.py::TestOptimizeLineContentDemoExample`) — a
  reflowed label (zero compression), its opted-out twin (authored split,
  compresses), a disabled middle line splitting a label into independent
  groups, and a four-line group pulling words down to the last line.

#### Growth cap (`optimize_line_content_max_lines`)

A group that cannot reach equal width with its authored lines can be given
room to grow: `optimize_line_content_max_lines` (unset by default) caps the
consecutive lines a group may use at `N`. **The option is growth-only — it can
only ever ADD lines, never remove, blank, or collapse them.**

- **M = G..N loop** (`_reflow_label`): the loop's floor is `G`, the group's
  existing line count — `M = G` *is* the plain full-group reflow above, so a
  capped group always starts from the historical behavior and only grows
  upward. For each `M` the DP repartitions the group's words onto `M` lines
  (`_partition_words` is unchanged; `M - G` clone lines are inserted **after**
  the group's last line, cloning the group's **last** enabled line's
  typography, so the group's start stays anchored), then a `ScaleProbe` render
  reports the group's effective scales. The first `M` with no compressed group
  line is accepted (INFO: "grew lines A-B to M of N lines"); a group still
  compressed at the ceiling keeps its widest layout and logs a WARNING.
- **Ceilings:** `min(N, word_count)` (every line keeps ≥1 word). `N <= G` is
  inert — plain reflow + DEBUG, no WARNING (nothing was going to grow). With
  `max_h_compress == 0.0` on every group line the renderer can never compress,
  so the probe renders are skipped (DEBUG) and the group keeps its full-group
  pass.
- **Probe plumbing:** `apply_line_content_reflow` gained a keyword-only
  `scale_probe: Optional[ScaleProbe]` (the alias is imported from
  `cutter_downsize.py`, the lazy default renders through
  `label_renderer.render_label_to_plt` — matplotlib stays out of module scope,
  preserving the Python 3.8 / Win7 import guarantee). An uncapped group never
  calls it, so jobs without the cap are bit-identical and pay no renders.
- **Validation** (`resolution._apply_reflow_line_caps`, a post-pass over the
  resolved lines so config injection + the full cascade are visible): a cap
  resolved onto a label with **no** enabled line raises
  `LineContentConfigError` (a cap cascading onto individually disabled lines
  is fine as long as the label has an enabled line — the pre-pass reads the cap
  from the group's own lines); one group declaring **two different** caps
  raises; the group's single cap is fanned out onto every group line via
  `dataclasses.replace`, so the pre-pass reads it from the group's first line.
  `JobSpec` additionally rejects the self-contradictory plate-parity pairing
  (plate cap + plate `optimize_line_content: false`). A job-level cap with a
  job-level opt-out is **valid** (a label may enable reflow) — the resolution
  post-pass is the authority.
- **Accepted staleness:** `_resolve_auto_line_spacing` /
  `_fit_content_to_margins` size vertical spacing against the *original* line
  count; the render-time `fit_line_spacing_to_margins` re-clamp preserves the
  margins when lines are inserted.
- Example fixture: `tests_deps/optimize_line_content_max_lines_job.yaml`
  (pinned by `tests/test_phase3_export.py::TestOptimizeLineContentMaxLinesDemoExample`)
  — a grown label (1 → 4 lines, zero compression), a cap-exhausted label
  (stays compressed at N), an inert cap (`N < G`, all lines kept), a cap
  declared on a group's last line, a zero-budget label, and a disabled line
  keeping two capped groups independent.
  `tests_deps/optimize_line_content_max_lines_conflict_job.yaml` pins the
  conflicting-caps abort.

### Bullet & Enumerated Lists (`itemize` / `enumerate`)

A list nests inside a label's `content` list as an `itemize:` (bullet) or
`enumerate:` (numbered) block carrying its own fields and one or more text
items. The block **expands at resolution time into ordinary
`ResolvedTextLine`s** — `ResolvedLabel.content` stays a **flat line list**, so
every downstream consumer (pre-passes, renderer, reports, packing) keeps its
current shape. Each item line carries five new resolved fields:
`marker_text` (the bullet string, or the computed counter), `align_group_id`
(one fresh id per block, `-1` on a plain line), `list_item` (True),
`indent_left` (the hanging-indent column, set by the `list_layout` pre-pass)
and `list_marker_gap_fraction` (the cascaded gap, kept on the line so the
renderer can derive its own floor).

- **Marker rendering** (`label_renderer.py`): the marker is a **separate
  render element**, composed at render time — the item's `text` / `plain_text`
  / `styled_runs` stay pure, so the reflow pre-pass can never move a bullet
  and reports show the item text and its marker distinctly. A marked line
  renders the text and a marker-only clone (`_marker_line_clone`, the same
  typography) and composes them on the `_render_line_block_composed` cursor
  idiom: marker at x = 0, text at x = `indent_left`, both on the item's
  **first-line baseline and cap height** (a bullet centers on the cap band,
  as EngraveLab does). The composed glyph/word partitions concatenate, and the
  descender relief reads the composed `"marker text"` (threaded as
  `_RenderedLine.display_text` → `_apply_descender_relief(texts=...)`), so the
  per-character mapping stays exact. `_composed_stroke_cutters` concatenates
  both halves' per-stroke cutter maps, so a marker engraving with a different
  tool lands on its own pen (the `<b>`/`<c:D>` split machinery is reused).
- **Hanging indent** (`list_layout.py::apply_list_marker_layout`, the **first**
  export pre-pass, before `reflow`): measures every distinct marker (lazy
  `measure_line_natural_width`, cached per typography) and sets
  `indent_left = max(marker widths in the group) + list_marker_gap_fraction *
  toolpath_text_height`. A free no-op (same label objects) when no line
  carries a marker. The renderer also derives a per-line floor (its own
  marker's width + gap) when `indent_left` is unset, so a direct
  `render_label_to_plt` call (a probe, a showcase) never overlaps marker and
  text.
- **List alignment**: a list block aligns as **one unit** — the group's
  alignment width is the widest total extent among its lines (marker column +
  text), so the markers form one flush column under `left`/`center`/`right`
  (the cascaded `text_h_alignment` of the group's widest line decides), and
  every line's left edge is the group's left edge plus its own indent.
  Phase 2a of `_render_positioned_lines` therefore runs compression (pass A),
  the group widths (pass B) and alignment (pass C) as separate loops.
  A line's compression + alignment budgets shrink by `indent_left` when the
  line carries no marker (a continuation), so an indented line still fits.
- **Multi-line items**: an item grows into a hanging-indent block through the
  existing `optimize_line_content` (+ optional `optimize_line_content_max_lines`)
  machinery — both cascade item → block → label → job, so enabling them on the
  block governs every item. A list group grows on a **width-fit** criterion
  (the widest segment must fit `label.width - 2*h_margin - indent_left`) rather
  than the ScaleProbe's "still compressed" test, so it works with
  `max_h_compress: 0` and costs **no probe renders**. The ceiling is the cap,
  and an **omitted cap means as many lines as necessary** (bounded by the
  item's word count — every line keeps ≥1 word). A grown continuation line
  clones the donor's typography and keeps its `align_group_id`/`indent_left`
  (that is what makes it a continuation) but carries **no marker** — the
  bullet/counter rides the item's first line only.
- **Reflow grouping** (`resolution.reflow_groups`, the single source of truth):
  a `list_item` line is **its own group** — the run breaks both before and
  after it, so words never cross an item boundary (each item is its own
  paragraph, and the cap is naturally per item). A disabled item forms no
  group. This extends the `new_line` hard-break rule; the DP partition and
  `_group_layout` are shared.
- **Item typography** (`resolution._merge_item_with_block`): every
  `TextAttributes` field the item leaves unset is filled from the block before
  the normal cascade runs, so `_resolve_content` transparently sees
  item → block → label → job → config for **every** typographic field (height,
  font, alignment, reflow permission, cap, ...). The marker settings resolve
  through `_cascade_list_setting` (explicit-`None` precedence) in
  `_list_marker_text`; the counter text comes from
  `list_counter.format_counter` (a pure, matplotlib-free module: decimal,
  bijective base-26 alpha, subtractive roman).
- **Schema** (`schema.py`): `ContentEntry` **subclasses** `TextLine` (making
  `text` optional) rather than forming a discriminated union, so `TextLine`
  itself is untouched and `substitution.py`'s `model_copy(update={"text": ...})`
  keeps working; a `field_validator(mode="before")` coerces bare `TextLine`
  objects, so every pre-list spec and every direct-API caller validates
  unchanged. `LabelSpec`/`JobSpec.content` are `list[ContentEntry]`. A list
  block is rejected as a **replacement-text template** (counters are computed,
  never substituted), and `placeholder: true` is rejected on a list item.
- **Reporting**: `generation_report.py` writes `marker_text`, `indent_left`
  and `align_group_id` per line, and `layout_report.py` adds a per-line
  finding `Line N: '<text>' list marker '-' (text column 0.178in)` (the 16th
  slot of the findings tuple; a continuation line with no marker reports
  nothing extra).
- Example fixture: `tests_deps/list_job.yaml` (pinned by
  `tests/test_phase3_export.py::TestListDemoExample`) — a plain bullet list, a
  per-item `bullet_string` override, decimal and styled counters
  (`Step iv)`), an uncapped growing item (1 → 3 lines, zero compression), a
  capped item, two items that never exchange words, a list between plain
  lines, a zero-gap list, and an indent-only (empty bullet) list. Unit
  coverage: `tests/test_list_counter.py`, `tests/test_list_layout.py`,
  `tests/test_list_render.py`, plus `TestListResolution`
  (`test_resolution.py`), `TestListItemGrowth` (`test_line_content.py`),
  `TestListMarkerFindings` (`test_layout_report.py`), and the schema /
  job-config classes.

### Baseline-to-Baseline Line Spacing (`use_baseline_spacing`)

`use_baseline_spacing` (default **true**) stacks a label's text lines by
baseline-to-baseline **pitch** (`toolpath_text_height + line_spacing`) instead
of by rendered ink box. Both renderers return geometry with the baseline at
y = 0 (+y up, descenders negative), so a line's ink is
`[baseline - D, baseline + A]` where `A`/`D` are the measured ascender and
descender depths. A descender therefore **hangs into the gap below its line**
instead of inflating it: every gap reads as one uniform visual spacing
regardless of descenders, and the whole block is shorter by the descender
depth. Cap-only lines (ascender == cap height, descender 0) stack
bit-identically to ink-box stacking. Cascades job → plate → label (never text
lines — spacing is a property of the stacked block); plate values are accepted
for schema parity only (labels render before packing); `job-config.json`
supplies the shop default (repo default `true`).

- **Pitch basis = `toolpath_text_height`** (the per-font cap height), NOT
  `nominal_text_height`: pitch stays content-independent (a per-font constant),
  which is also what makes descender-free lines bit-identical to ink-box
  stacking.
- Pure math in `resolution.py` (matplotlib-free): `baseline_pitch`
  (`cap + spacing`, spacing floored at 0), `baseline_offsets` (baselines
  descending from 0 by one pitch), `baseline_block_height`
  (`max(b_i + A_i) - min(b_i - D_i)` — convex, continuous and non-decreasing
  in the spacings), `fit_baseline_spacing_to_margins` (render-time clamp:
  proportional shrink of all spacings via bisection on the scale factor —
  the closed-form of `fit_line_spacing_to_margins` is only valid for the
  linear ink-box model; tolerance 1e-9, ≤100 iterations),
  `solve_baseline_spacing_to_fill` (auto `line_spacing` with explicit
  `v_margin`: uniform spacing filling the inner height exactly) and
  `solve_baseline_spacing_to_ratio` (auto both: inter-line gap = ratio ×
  top/bottom gap, filling the label). All return `0.0`/input on degenerate
  input (single line, no spacing to remove, overflow with zero spacing).
- **Real descender extents (Option B, no accepted staleness):** the auto
  spacing solvers in `_resolve_auto_line_spacing` / `_fit_content_to_margins`
  size against **measured** `(A, D)` per line when the feature is on and an
  `extents_probe` is supplied. `LineExtentsProbe =
  Callable[[ResolvedTextLine], tuple[float, float]]` is injectable
  (`resolve_job_spec(..., extents_probe=...)`); the production probe is
  `memoize_extents_probe(measure_line_vertical_extents)` (label_renderer
  renders each line once, caches per line). The CLI (`cli/generate.py`) and
  `scripts/run_integration_test.py` thread it in. Without a probe (direct API
  / test callers) the historical nominal-height arithmetic runs unchanged;
  the probe is never called when the feature is off (`_measure_extents`
  returns `None`), so ink-box jobs pay no renders.
- **Renderer** (`_render_positioned_lines`): pass 1 collects
  `baseline_geometry` `(pitch_basis, ascender, descender)` per rendered line;
  the margin clamp chooses `fit_baseline_spacing_to_margins` vs
  `fit_line_spacing_to_margins`; baselines come from a precomputed table
  (`baseline_offsets` shifted so the ink block centers on y = 0) and each
  line's `y_offset` is exactly its baseline Y. The ink-box running anchor is
  skipped in baseline mode.
- **Reporting keeps implied-gap semantics with zero `layout_report.py`
  changes**: `reported_spacing` is the spacing term (`pitch - cap height`),
  equal to the requested `line_spacing` unless a margin clamp reduced it —
  exactly the deviation the report surfaces. A descender pair whose *visual*
  gap shrinks is therefore not a finding. No new `RenderedLabel` /
  `_LineEntry` fields; no CLI note (the CLI keeps its single summary line).
- `DEFAULT_USE_BASELINE_SPACING: bool = True` in `resolution.py` is the code
  fallback (also the `ResolvedLabel` dataclass default, so direct test
  callers get baseline stacking).
- Example fixture: `tests_deps/use_baseline_spacing_job.yaml` (pinned by
  `tests/test_phase3_export.py::TestUseBaselineSpacingDemoExample`) — a
  baseline label (descender hangs into the gap: visual gap 0.027 vs the
  requested 0.15, block 0.123in shorter), its ink-box twin
  (`use_baseline_spacing: false`, full 0.15in air), and a cap-only control
  label (mode-independent).

### Automatic Descending-Character Relief (`auto_descending_character_adjustment`)

Baseline stacking lets a descender hang into the gap below its line. When the
line under it carries a tall glyph close enough that the two **engraved
strokes** would bleed together, `auto_descending_character_adjustment` (default
**true**) relieves that one glyph: a vertical downscale about its own baseline
plus an upward raise, both bounded by cascading allowances. Cascades job →
plate → label (never text lines — the mechanism compares a line's glyphs
against the following line's, so it is a property of the stacked block); the
plate tier is schema parity only, like every render-affecting field.
`job-config.json` supplies the shop default.

- Five cascading fields, all on `LabelAttributes` + `PlateSpec` parity, landing
  on `ResolvedLabel` (label → job → default, explicit-`None` precedence so an
  explicit `false`/`0.0` is honored):
  `auto_descending_character_adjustment` (bool, default `true`),
  `descending_character_v_scale` (`[0.2, 1.0]`, default `0.5` = deepest squeeze
  to half height, about the baseline), `descending_character_raise`
  (`[0.0, 1.0]`, default `0.7` = fraction of the glyph's **own descender depth**,
  so `1.0` lifts its lowest point exactly to the baseline),
  `descending_character_clearance` (`ge=0.0`, default `0.0` = **unitless factor
  of the stroke floor**: the required gap is `floor * (1 + clearance)`, so the
  demanded air scales with the cutter width and reads the same at every text
  size; explicit `0.0` means only true touching triggers), and
  `descending_character_reciprocal_scale` (`[0.2, 1.0]`, default `0.5` =
  deepest **top-down** downscale allowed for the colliding glyphs on the
  **following** line; `1.0` forbids the reciprocal half and reproduces the
  historical one-sided relief bit-identically).
  All five are `JobDefaults` keys in `_JOB_LAYER_FIELDS` and are **not**
  required-when-unconfigured (they have code fallbacks).
- **The relief is reciprocal:** the deficit is distributed between the two
  lines through the **same ramp** `t` driving the descending glyph. The
  colliding partners on the following line (every glyph whose ink is within
  `required` of the descending glyph's sub-baseline ink —
  `descender_adjust.colliding_partners`) are scaled **top-down** about their
  own lowest ink point: `y' = y_min + (y - y_min) * s_l`,
  `s_l = 1 - t * (1 - descending_character_reciprocal_scale)`
  (`descender_adjust.reciprocal_scale_y` / `recip_ink`), so their lowest point
  never moves and their top descends toward it. The composition reuses the
  existing primitives — `translate(0, -y_min) → scaled_y(s_l) → translate(0,
  y_min)` — no new `TextBlock` method. Because the anchor is the lower glyph's
  lowest ink, a reciprocal fix never moves ink *down* past it: the gap above
  the line widens monotonically and the **bottom border is untouched**
  (`descender_border_job.yaml` stays bit-identical). A middle-line glyph can
  carry both ramps at once (its own descending ramp into the gap below + a
  top-down ramp from the gap above): the upper ramp is applied first, then the
  reciprocal about the resulting lowest point; the fixpoint re-measures each
  pass, ramps are one-way and capped at `1.0`, spacings one-way and capped by
  the inner height, so the loop converges.
- **Bisection plumbing:** the frozen `InkGrid` (cell = `required`) indexes the
  lower line's **static** ink; the moving reciprocal partners are excluded from
  the grid and measured directly per candidate ramp with `geometry.ink_gap`
  against their scaled ink (a handful of glyphs, so the exact per-partner
  measurement stays cheap). `propose_ramp`'s `gap_at(t)` is the minimum of the
  grid query (ramped upper ink) and the partner `ink_gap`s.
- **Trigger is per descending glyph, measured against the following line:**
  the **true 2D swept-ink distance** — imagine a circle whose diameter is the
  required clearance rolled along the descending glyph's **sub-baseline ink**
  (its strokes clipped at the baseline); it collides exactly when it touches
  any part of any glyph of the line below. Equivalently the gap is
  `geometry.ink_gap` between the two glyphs' stroke polylines, against
  `0.5 * (cutter_upper + cutter_lower) * (1 + descending_character_clearance)`.
  The first factor is the engraved-stroke floor at which the two cut strokes
  touch (the same stroke-aware floor the text-hole system uses); the clearance
  is a unitless multiplier of that floor, never an inch value. **Not a
  bounding-box comparison:** a box model reports X-disjoint boxes as
  "infinitely far apart" and hides exactly the pairs this mechanism exists for
  (a `g` hook reaching sideways into the crook of the `i` on the line below is
  X-disjoint at box level while its true proximity sits well inside the
  required clearance — the reported regression, pinned by
  `tests_deps/descender_crook_job.yaml`), and fires on box overlaps whose real
  ink is nowhere near (false positives). `descender_adjust.GlyphBox` carries
  the glyph's `ink` and `sub_baseline_ink`; glyphs without ink knowledge
  (hand-built boxes in tests) fall back to the vertical box model
  (`geometry.aabb_vertical_gap`, `inf` when X-disjoint). A descender with
  no close partner below is left exactly as rendered — never a blanket
  adjustment, never keyed off the following line's cap line. The lower line's
  complete ink is indexed once per gap per pass through
  `geometry.InkGrid` (cell = required clearance: the `gap < required` decision
  is exact below the cell, an upper bound above it), which keeps the per-glyph
  measurement cheap on lines of hundreds of glyphs.
- **The bottom border is a virtual "following line"** (`_border_box`): the
  border rectangle is drawn at the label edge (`vp.rect(0, 0, width, height)`,
  no inset) and cut by the boundary/hole tool, so its centreline sits at
  `-height / 2` in the relief's stacked frame (the frame centres the ink block
  on `y = 0` at label `height / 2` — the same constant in both stacking modes).
  A descending glyph collides with it on the **same** rolling-circle test, with
  `required_gap(text_cutter, label.hole_cutter_diameter, clearance)` — the
  engraved descender stroke against the engraved border stroke. **No new
  fields:** the border reuses `auto_descending_character_adjustment`, the three
  allowances and `hole_cutter_diameter`. `v_margin` is **not** a constraint —
  ink spilling into the margin is accepted behavior, only the border stroke
  matters (pinned by the `margin_cross` label of
  `tests_deps/descender_border_job.yaml`). Every line is measured, so a
  single-line label (no inter-line gap at all) still triggers.
- **Single proportional ramp:** one `t ∈ [0, 1]` drives both effects —
  `scale_y = 1 - t*(1 - v_scale)`, `raise = t * raise_fraction * descender`.
  For ink-measured pairs the gap is **not linear in `t`**, so the smallest
  clearing `t` is bisected (`descender_adjust.ramp_for_measured` over
  `ramp_ink`, the pure stacked-frame transform mirroring the renderer's
  stroke ramp); the box-model fallback keeps the closed form
  (`descender_adjust.ramp_for`). The bisection measures the glyph's **current**
  (already-relieved) ink, so it returns the ramp the glyph still needs *on top
  of* the one it carries; `propose_ramp` converts it to a **total**
  (`min(1, current + increment)`) because `_apply_glyph_ramps` rebuilds from the
  ORIGINAL render at the glyph's total ramp — storing the bare increment
  under-applies the relief (line-to-line hides that behind spacing growth; the
  border has no growth lever, so it engraves short of the clearance). `t`
  clamps to `1.0` (both allowances fully spent; zero allowances never propose a
  ramp); the relief is one-way (a glyph only ever gets smaller and higher), so
  the fixpoint converges. The gap tolerances are `1e-6`in (a thousandth of a
  plotter unit): the 2D distance grows sub-linearly with the vertical spacing,
  so the fixpoint's finite passes leave sub-plotter-unit noise that must never
  abort the job.
- **Exhausted ramp ⇒ grow the gap below the line** (label_renderer's
  `_grow_spacings` + `_solve_spacing_growth_factor`, bisection on the stacked
  block height, mode-agnostic via a `height_at` callback). The growth per gap
  is **measured, not deficit-derived** (`_solve_gap_growth`): the ink distance
  widens **sub-linearly** with the spacing (a laterally-offset pair gains
  `sqrt(dx² + (dy+g)²) - sqrt(dx² + dy²) < g`), so growing by the deficit
  under-shoots and the fixpoint stalls with a geometric tail of residuals.
  `_solve_gap_growth` bisects on the measured gap, reusing one frozen
  `InkGrid` per gap by translating the *query* ink (identical geometry, ~µs per
  step). The growth is capped by the vertical margins; a residual shortfall
  after every allowance and all admissible spacing raises `LabelRenderError`
  (non-zero CLI exit) naming both lines, the shortfall and the spent allowances.
- **Exhausted ramp at the border ⇒ WARNING, never an abort**
  (`_log_border_shortfalls`). Spacing growth cannot help a border collision —
  growing the gap above the last line moves that line **down** in the centred
  frame (`_baseline_table`'s `shift = (top + bottom) / 2`), deepening it — so
  the ramp is the only lever, and a spent ramp leaves the best the allowances
  allow. Every glyph that ever triggered against the border is re-measured on
  the final geometry (`border_triggered`, so a glyph whose allowances could not
  move it at all — ramp 0 — is not silently colliding); a shortfall is reported
  when it exceeds `_BORDER_SHORTFALL_TOLERANCE` (one plotter unit: the border
  ramp converges asymptotically because relieving a glyph shrinks the block's
  bottom extent, re-centring the block downward and undoing part of the relief,
  which leaves a sub-unit residual that cannot exist in the emitted geometry).
  Border deficits never enter `relief.residual`.
- **Runs inside `_render_positioned_lines`, between phase 2a (X: compression +
  alignment) and phase 2b (Y: stacking + `_LineEntry`).** The relief is Y-only
  and the X phase is X-only, so they commute; X must run first because the pair
  gaps need final X positions (alignment shifts lines relative to each other).
  Works in both stacking modes (baseline and ink-box). The relief works in the
  renderers' local frame (baseline `y = 0`); `_shifted_boxes` lifts glyph boxes
  (bounds **and ink**) to the stacked baselines for measurement, and
  `_apply_glyph_ramps` rebuilds each line from the **original** render at the
  glyph's full ramp (idempotent — ramps never compound). The FINAL/residual
  check re-measures the trigger metric on the final geometry, so a relief that
  shrinks and raises a descender can never land the raised part on the line
  below (an `i` dot pushed into the crook of a `g`) without growing the spacing
  or aborting. Untouched glyphs are measured by the same metric (tightness
  between untouched neighbours is the authored spacing, not this mechanism's
  doing); relief-moved glyphs additionally report `gap_after` (clamped to
  `_GAP_REPORT_RADIUS`, the grid's reporting radius, so a fully relieved glyph
  never prints `inf`).
- **Ink is clipped at the emission floor** (`_INK_EMISSION_FLOOR` = half a
  plotter unit = `0.5 / HPGL_UNITS_PER_INCH` ≈ 4.92e-4in): emitted coordinates
  are integer plotter units (1016 per inch), so ink
  shallower than that cannot collide in the engraved output. `_clip_below_baseline`
  clips the sub-baseline ink at `-floor` (not `0`) and `_glyph_boxes` zeroes a
  `descender` at or below the floor, so a 0.0004in font-data sliver never
  relieves a glyph that does not descend and never aborts a job on sub-unit
  deficits. Real descenders are two orders of magnitude deeper.
- **`TextBlock.scale_y(factor, context)`** mirrors `compress_x`: X untouched,
  Y scaled about the baseline; each arc's ellipse image refits with circular
  `AA` arcs (see Arc-Preserving Non-Uniform Transforms), flattening only the
  arcs the refit declines (DEBUG log). Stroke count is
  preserved 1:1, so `glyph_groups`, `TextChunkRecord.glyph_groups` and the
  intra-chunk glyph sweep stay valid. `TextBlock.replaced_strokes` splices one
  glyph back at its original stroke indices.
- **Reporting** rides `RenderedLabel.descender_adjustments_by_line`
  (`{line_index: (DescenderFix, ...)}`, only lines with a relief).
  `layout_report.py` prints each as a finding:
  `Line N: 'Depth' glyph 'p' descender v-scale 0.600, raised 0.071in (gap
  -0.077in -> 0.045in)`; a reciprocal record (`DescenderFix.reciprocal`, a
  following-line glyph sharing the load) prints
  `Line N: 'charging' glyph 'i' shares relief, top-down scale 0.875 (gap
  0.094in -> 0.281in)` instead (its `raise_amount` is always `0.0` — the
  lowest ink point is the anchor). `_LineEntry` is **unchanged** (the relief
  never touches the collision-detection tuple-unpack). INFO per glyph, WARNING
  per spacing growth, WARNING per unresolvable border shortfall (`gap_after` is
  the tightest of the constraints that bind the glyph — the line below and, for
  glyphs that triggered against it, the bottom border).
- **`plt_optimizer/generate/descender_adjust.py`** is the pure solver
  (matplotlib-free, `TYPE_CHECKING`-only pipeline imports — Win7/3.8 import
  guarantee): `GlyphBox` (with `ink`/`sub_baseline_ink`), `PairGap`,
  `GlyphGap`, `DescenderFix` (with `reciprocal`), `required_gap`,
  `measure_pairs`, `measure_glyph_gaps`, `worst_glyph_deficit`,
  `colliding_partners` (the reciprocal half's partner selection),
  `reciprocal_scale_y` + `recip_ink` (the top-down transform),
  `ramp_for` (box-model closed form, with the optional
  `lower_height`/`reciprocal_scale` reciprocal slope),
  `ramp_for_measured` (ink bisection), `ramp_ink`, `apply_ramp`,
  `merge_ramps`, `ramps_changed`, `is_finite_gap`. The distance primitives
  live in `geometry.py` (`point_segment_gap`, `segment_gap`, `polyline_gap`,
  `ink_gap`, the `InkGrid` spatial hash, the `Ink`/`Polyline`/`Point`
  aliases, and `bbox_gap`/`polyline_bounds`/`ink_bounds`); `aabb_vertical_gap`
  stays for the box-model fallback. The box gap is a lower bound on the ink
  distance inside, so `polyline_gap`/`ink_gap` prune segment/polyline pairs
  whose boxes sit beyond the current best, and `colliding_partners` skips
  partner glyphs whose ink box is out of reach — exact (bit-identical
  distances, pinned by randomized brute-force equivalence tests), and the
  reason the relief's gap scan is affordable on long lines. `geometry.py`
  stays dependency-free (no numpy: per-op overhead loses at glyph-sized
  arrays; the pre-filters are the wholesale win).
- **The relief's memoized leaves (exact, LRU-bounded).** Grid construction
  goes through `geometry.ink_grid_for(ink, cell)` — `lru_cache(16)` over
  `InkGrid`; a grid is a pure function of `(ink, cell_size)` and read-only
  after `__init__` (`min_gap` mutates nothing), so sharing is exact. The
  relief rebuilds the same line ink's grid every fixpoint pass and every
  collision-sweep step (the sweep scales X only, on colliding lines).
  `label_renderer._glyph_boxes` is `lru_cache(64)` over
  `(block, glyph_groups, text)` — frozen hashable inputs, frozen
  `GlyphBox` values; the wrapper returns a fresh LIST per call (callers
  own their result) and deep-coerces list-built partitions.
  `label_renderer._shifted_boxes` is `lru_cache(64)` over
  `(boxes, baseline_y)` — the stacked-frame shift is a pure function of
  frozen hashable boxes plus the baseline, and the baseline table only
  moves when a pass grows a spacing, so consecutive passes and the final
  measurement re-shift byte-identical boxes onto byte-identical
  baselines; same fresh-LIST wrapper, the `baseline_y == 0.0` identity
  fast path stays uncached. Unhashable
  ink falls back to a fresh grid (identical behavior, unmemoized).
  Pinned by `tests/test_relief_memoization.py` (identity hits, key
  isolation, randomized equivalence vs cache-cleared recompute, LRU
  bounds, render bit-identity). The border
  needs no solver changes: `_border_box` / `_border_required` (label_renderer)
  feed the border's centreline into the same `measure_glyph_gaps` /
  `ramp_for_measured` path, and `_BorderShortfall` carries the report.
- Accepted staleness: `_resolve_auto_line_spacing` / `_fit_content_to_margins`
  size vertical spacing against the *original* descender extents; the
  render-time growth is margin-safe and authoritative. Resolution-time
  awareness (a per-glyph extents probe) is a deliberate follow-up.
- Example fixtures: `tests_deps/descender_adjust_job.yaml` (pinned by
  `tests/test_phase3_export.py::TestDescenderAdjustDemoExample`, whose
  `border` label covers the bottom-border trigger and whose `reciprocal` /
  `reciprocal_off` labels pin the reciprocal half) and
  `tests_deps/descender_crook_job.yaml` (the reported `g`-hook-into-`i`-dot
  crook, pinned by `TestDescenderCrookExample` — the pair the box model
  reports as X-disjoint/`inf`). `tests_deps/descender_border_job.yaml`
  (pinned by `TestDescenderBorderExample`) isolates the border: trigger,
  no_trigger, `margin_cross` (margin crossed, border clear ⇒ no relief),
  `spacing_untouched` (growth excluded) and the opt-out twin.
  `tests_deps/descender_reciprocal_job.yaml` (pinned by
  `TestDescenderReciprocalExample`) isolates the reciprocal half on the crook
  pair: `shared` (both lines move), `reciprocal_only` (the descender's own
  allowances forbidden, the lower glyph carries the whole deficit) and
  `reciprocal_off` (the lever off ⇒ no relief at all).

### Baseline-to-Capline Band Centering (`v_center_by_content`)

A label centers its text block vertically. Centering it by its **rendered ink**
(the historical rule) charges the deepest descender to the block, so a line
carrying descenders rides high and its plain letters look visibly off-centre —
the reported Sunwest symptom (`B29ABg` on a 1x1.0in label: the visible text sat
0.089in from the top edge while the `g` wasted the air it needed below).

- **`ResolvedLabel.center_text_by_band`** is the single gate:
  `auto_descending_character_adjustment and not v_center_by_content`. With the
  relief disabled nothing would adjust a descender, so ink centering is kept and
  `v_center_by_content` is inert. `v_center_by_content: true` restores ink
  centering bit-identically.
- **One delta, applied in one place.** The band spans the lowest baseline to the
  highest capline; `_band_center_delta` returns its center measured in the
  ink-centered frame (which the frame already centers on `y = 0`), so the
  band-centered frame is that frame shifted down by the delta. Band top =
  `max(b_i + A_i)` when `v_center_by_content` is true (measured ascender) and
  `max(b_i + toolpath_text_height_i)` when false (content-independent per-font
  cap height); band bottom is always `min(b_i)`. Relative stacking is untouched,
  so line-to-line relief gaps are shift-invariant — only the absolute frame
  moves, which is what the border (fixed at `-height/2`) needs.
- **`_center_text_layer_vertically` is authoritative.** It runs after export and
  re-centers the exported layer's ink bounds onto `v_margin + available_height/2`,
  so it silently undoes any centering applied only to the render frame. The
  delta is therefore threaded to it (`band_center_offset_inches`, subtracted from
  the target) via the established out-param idiom
  (`_render_positioned_lines` → `_render_text_lines_by_pen` →
  `_render_label_once`) and stored on **`RenderedLabel.band_center_offset_inches`**.
- **Every consumer of the centering rule must see the same delta** — three
  sites, all reading `RenderedLabel.band_center_offset_inches`:
  `detect_text_hole_collisions` (its `shift_y = height/2` frame shift),
  `resolve_via_margin_adjustment` / `resolve_via_compression`
  (threaded through), and `plate_optimizer._label_transform` (the plate-space
  text transform, which mirrors the centering math exactly — a missed site here
  desynchronizes the optimized text layer from the emitted border).
- **The margin clamp follows the same model.** `baseline_block_height` gained
  `charge_descenders: bool = True`; band-centered labels pass `False` so the
  block is measured as the cap band and descenders are not charged to the inner
  area (they hang into the bottom margin). Threaded through
  `fit_baseline_spacing_to_margins`, `solve_baseline_spacing_to_fill`,
  `solve_baseline_spacing_to_ratio`, `_resolve_auto_line_spacing`,
  `_fit_content_to_margins`, the renderer's own clamp, and the relief's
  `height_at` growth cap — all must agree, or one clamp fights another. The
  default `True` keeps every existing caller and pin unchanged.
- **Packing is unaffected**: the border rect dominates `RenderedLabel` bounds and
  a pure Y shift changes no ink extent (pinned by
  `TestBandCentering::test_band_centering_does_not_move_the_label_bounds`).
- Fixture: `tests_deps/v_center_by_content_job.yaml` (pinned by
  `TestVCenterByContentExample`) — band-centered (relief fires), `ink_centered`
  (border clear), `relief_off` (field inert), `spacing_kept` vs `spacing_clamped`
  (the margin clamp model). Renderer-level: `TestBandCentering`.

### Small Caps Fonts (`<base> SC`)

Appending ` SC` to any font name selects a **synthetic small-caps variant**
of that base font, rendered on the fly — the variant is never a
font-catalog `font_name` key or a `.ttf` file. The same transform is available
**per character** through the inline `<sc>` tag (see Inline Small Caps below);
the two are mutually inert — on a ` SC` font the tag has no additional effect,
because the whole line is already small caps. Uppercase glyphs engrave verbatim at
full size; **ASCII lowercase `a-z`** (and only those characters — accented
lowercase, `ß`, digits, punctuation and the derived Unicode symbols pass
through verbatim at full size) renders as the base font's **uppercase glyph
uniformly downscaled** by the cascading `sc_scale` (default 0.7). The
transform is **top-down**: the glyph's own bottom stays exactly in place (it
sits on the baseline like a shrunken capital) and its top descends. The
nominal/toolpath `text_height` is untouched, so the cutter selection never
moves. The character map is strictly 1:1, so the word/glyph stroke
partitions (the intra-chunk glyph sweep) stay exact.

- **Registry** (`font_registry.py`): `_canonical_map` synthesizes
  `"<base> sc" -> (kind, "<base> SC", base, small_caps=True)` for every PLT
  key and TTF basename (case-insensitive; `setdefault` after all bases, so a
  real font literally named `"Foo SC"` wins its name). PLT bases whose catalog
  entry sets `symbol_font` (symbol fonts such as `Wingdings`, extracted from a
  `_sym`-suffixed sheet; read by `plt_font_is_symbol()` /
  `PltFontData.symbol_font`) contribute **no** `SC` variant: a symbol font has
  no lowercase to shrink, so `"Wingdings SC"` is unknown to the schema
  validator, the
  `--show-fonts` docs and the showcase (the base `Wingdings` stays fully
  selectable). `FontRef` carries
  `small_caps: bool` and the **base** font's `path`. `font_name_choices()`
  lists the variants (4 bases → 8 names today), so the schema validator,
  `--show-fonts` docs and the font showcase all see them. Library lookups
  must strip the suffix — `base_font_name("Dino SC") -> "Dino"` — which
  `plt_font_renderer._resolve_plt_font` and `scripts/kerning_report.py` do
  (a `"Dino SC"` key does not exist in the library).
- **The scale must be uniform (x = y)**: `TextBlock.scaled(f)` keeps `ArcSeg`
  sweeps native **exactly** (a uniform scale maps a circle to a circle), so PLT
  small caps stay **arc-native end-to-end** with zero refit cost; the
  non-uniform `scaled_y`/`compress_x` refit arcs approximately (see
  Arc-Preserving Non-Uniform Transforms) and are never used here. The transform
  is a uniform scale composed with a pure
  translation (`y' = y_min + (y - y_min) * f`, `x' = x * f`), which is what
  makes it top-down.
- **PLT** (`plt_font_renderer.py`, keyword-only `lowercase_scale:
  Optional[float] = None` on `_walk_line` + the three public wrappers): the
  char loop maps the glyph *lookup* through `_small_caps_source` (ASCII
  lowercase → uppercase) and scales the cached `_GlyphGeometry` — block,
  bounding box **and both profile envelopes** — via `_scale_geometry`
  **before** `kerning_offset`, so the small silhouette kerns against its
  neighbours exactly. `None` disables the transform entirely, so every
  legacy call site renders bit-identically; `1.0` keeps the uppercase map and
  only skips the shrink (the line engraves as full-height capitals). Glyph
  groups keep the ORIGINAL character.
- **TTF** (`ftext_renderer.py`, keyword-only `small_caps_scale:
  Optional[float] = None` on `render_text_line_ftext[_with_words|_with_glyphs]`
  and `small_caps: bool` on `glyph_groups_for_line`): matplotlib's `TextPath`
  ignores OpenType `smcp`, so the renderer maps the text through
  `_ascii_upper_map` (1:1, length-preserving) and renders the mapped string —
  keeping matplotlib's shaping and the font's **natural uppercase advances**
  (the standard synthetic small-caps spacing; advancing by the shrunken glyph
  would need a per-glyph TTF layout path and is deliberately out of scope).
  `_apply_small_caps` then slices the line's contours per character
  (`_per_glyph_counts` probes the **mapped** uppercase glyphs' contour counts
  while keying the slices by the original characters — a-z counts differ from
  A-Z counts) and scales each lowercase-origin slice about that glyph's own
  **bottom-left ink corner**. Under SC the word groups come from that exact
  slicing (the signature matcher cannot match shrunk glyphs). A contour-count
  drift logs a WARNING and leaves the natural uppercase render.
- **Dispatch** (`label_renderer._render_line_block`): the parameter is added
  to the renderer call **only** when `resolve_font(...).small_caps`, so every
  non-SC line keeps the historical call shape and output.
  `measure_line_natural_width` / `measure_line_vertical_extents` route
  through the same dispatch, so the reflow/compression/extents pre-passes are
  SC-aware for free.
- `sc_scale` is a `TextAttributes` field (`ge=0.2, le=1.0`, explicit-`None`
  cascade line → label → job, plate parity) landing on `ResolvedTextLine`;
  `DEFAULT_SC_SCALE = 0.7`; a `JobDefaults` key in `_JOB_LAYER_FIELDS`
  (repo `job-config.json` ships 0.7) and **not**
  `REQUIRED_WHEN_UNCONFIGURED`. It never touches `nominal_text_height` /
  `toolpath_text_height`, so it cannot interact with cutter selection,
  compression budgets, spacing solvers or collision math.
- Fixture: `tests_deps/small_caps_job.yaml` (pinned by
  `tests/test_phase3_export.py::TestSmallCapsDemoExample`) — PLT SC at 0.7 vs
  its capitals, `sc_scale: 1.0` == base-font capitals, `sc_scale` inert on a
  base font, caseless characters at full size, a mixed-case 0.5 line, and the
  TTF pair. Renderer-level: `TestSmallCaps` in `tests/test_plt_font_renderer.py`
  and `tests/test_ftext_renderer.py`; registry-level: `TestSmallCapsVariants`.

### Horizontal Text Alignment

Each text line is positioned horizontally within the label's inner content
area (`[margin, width - margin]`) according to `text_h_alignment`. Values
cascade line → label → job (default `center`; accepted on plates for schema
parity only). Enum `TextHAlignment` (`left`, `center`, `right`).

- `left`: the line's left-most point sits **precisely at the left margin**.
- `right`: the line's right-most point sits **precisely at the right margin**.
- `center`: the line is centered within the inner content area (existing
  behavior; backward compatible default).
- `compute_horizontal_offset()` (resolution.py): pure offset math returning
  the target left-edge X for a rendered line; unknown values fall back to
  `center`.
- Applied per-line by the render path (`_render_positioned_lines`) after
  compression, before vertical stacking.
- When a line is wider than the inner area (e.g. compression disabled),
  `center` overflows symmetrically while `left`/`right` keep their aligned
  margin edge anchored and spill out the opposite side.


### Applied Layout Reporting

Two render-time typography effects change what is engraved without
appearing in the YAML spec, and both are measured during rendering and
reported reporting-only (nothing downstream consumes them for geometry).
A third finding — the compression-driven cutter downsize (see
Compression-Driven Cutter Downsizing) — is reported from
`ResolvedLabel.cutter_downsize_by_line` set by the export pre-pass:

**Horizontal compression.** Both compression mechanisms (margin-overflow
and Phase 3 collision) are render-time effects: `resolve_job_spec()` only
resolves the *budget* (`max_h_compress`), and `collision_compress_by_line`
is empty until the renderer sets it. The *effective* per-line scale —
collision scale × margin scale, `1.0` = natural width — is measured during
rendering.

**Vertical line spacing.** The requested `line_spacing` (resolved to a
float by `resolution`, `"auto"` already expanded) is clamped at render
time by `fit_line_spacing_to_margins` to preserve the vertical margins
(the *effective* gap can be smaller than requested). The effective gap is
the value actually used for stacking, so it is what the report shows.

- `_LineEntry` (label_renderer.py) is a `NamedTuple`
  `(line_index, line_text, bounds, compression_scale, line_spacing)`; the
  margin scale is measured in `_render_positioned_lines` by diffing the
  line's width across the `compress_line_to_width()` call (`compress_x`
  scales X only, so the width ratio is exact) and multiplied by the
  collision scale applied in the first pass; `line_spacing` is the
  effective gap *below* the line (`adjusted_spacings[i]`), `None` on the
  last renderable line (n-1 gaps for n lines). ⚠️ Adding a field to
  `_LineEntry` requires updating the tuple-unpack in
  `detect_text_hole_collisions`.
- `RenderedLabel.compression_by_line: dict[int, float]` carries the
  per-line scales; only lines below `1.0` are included, so the common
  case is an empty dict. `RenderedLabel.line_spacing_by_line:
  dict[int, float]` carries **every** effective gap, keyed by the upper
  line's content index (the last line has no entry); the requested value
  is read from `source_label.content[i].line_spacing` (the adjusted clone,
  so collision-phase clones report correctly). Both populated by
  `_render_label_once`; single-line labels yield empty dicts.
- `PerCutterExport.rendered_labels: dict[str, RenderedLabel]` exposes the
  layout render cache (keyed by label id) so CLI/script callers read
  compression + spacing + collision state without re-rendering.
- `plt_optimizer/generate/layout_report.py` is the **single source of
  truth** for the report text: `format_layout_report(resolved_labels,
  rendered_by_id, full=...)` returns the lines, `has_layout_findings(...)`
  gates compact output. The module is pure Python (pipeline types are
  `TYPE_CHECKING`-only imports), so it is unit-testable without
  matplotlib and safe on the CLI's lazy-import path. A *finding* is a
  compressed line (scale < 1.0), a spacing gap that deviates from the
  requested value (compared with `math.isclose`), a downsized cutter
  (`source_label.cutter_downsize_by_line` non-empty), or a label with
  cutter-compensated drill holes (`source_label.hole_adjust_for_cutter`
  with holes — `layout_report._hole_adjust_lines`, a label-level line).
- `layout_report.collect_line_findings(rendered)` is the **structured**
  counterpart: it returns the same findings as a list of
  `layout_report.LineFinding` (a frozen dataclass with one optional field
  per effect kind — `compression`, `requested_spacing`/`effective_spacing`,
  `downsize`, `relief`, `bold`, `markup_scale`, `sup_sub`, `font_runs`,
  `cutter_runs`, `original_cutter`). The text report and the JSON report
  are both rendered from it, so the two surfaces can never diverge. The
  text output is **byte-identical** to the pre-`LineFinding` formatter
  (pinned by `tests/test_layout_report.py`).
- `plt_optimizer/generate/generation_report.py` builds the **structured
  job report** the `generate` CLI writes into `<out>/generation_report/`:
  - `ReportCapture(logging.Handler)` — accumulates the pipeline's
    WARNING/ERROR records (attached to the `plt_optimizer` logger, so the
    child loggers propagate into it) and diverts the `stage <name>: <s>s`
    timing lines into `stage_records`. It is a plain class, NOT a
    dataclass: a dataclass with `eq=True` is unhashable and
    `logging.Handler` registers itself in a `weakref.WeakSet`.
  - `build_report(...)` — assembles the JSON document: `job` (name, id,
    status, error, paths, timestamps, duration, tools, options), `summary`
    (counts), `timings` (per-stage wall clock, summed per stage name),
    `outputs` (written PLT/PDF paths + the packed plate placements),
    `labels[]` → `lines[]` → `findings[]`, `warnings[]`, `errors[]`. Every
    float is rounded to 6 decimals so two identical runs are comparable
    apart from the timestamp fields.
  - The `timings` section reads the **explicit
    `utils.stage_timing.StageTimings` collector** when the run provided
    one, and falls back to the captured stage log records otherwise. The
    collector is the primary path because the generate pipeline runs at
    WARNING: the stage timing *records* are INFO and never exist at that
    level, so the log alone would lose the measurements. The collector is
    threaded through the export as
    `export_per_cutter_plts(..., stage_timings=collector)`.
  - Each finding is a **typed entry** — one per applied effect, so a
    consumer can filter by `type`: `compression`, `spacing`,
    `cutter_downsize`, `descender_relief`, `bold_cutter`, `scale_markup`,
    `sup_sub_shift`, `font_run`, `cutter_markup`, `original_cutter`.
  - `format_summary(report)` renders the human-readable digest;
    `write_report(report_dir, report)` writes `report.json` + `summary.txt`
    (the directory is created on demand).
  - The report is written on success **and** on failure (the failure
    report carries `job.status = "failed"`, `job.error = {type, message,
    stage}` and the warnings collected before the failure). A report-write
    error is downgraded to an ERROR log line and never changes the exit
    code.
- `scripts/run_integration_test.py` prints the full report (`full=True`)
  as the **Phase 3.6: LAYOUT REPORT** section (per label, one line per
  compressed text line: `Line N: '<text>' scale 0.870 (13.0%
  compressed)`, one per gap: `Line N: '<text>' spacing below
  0.120in (requested 0.162in)`, and one per reduced tool: `Line N:
  '<text>' cutter 0.060in -> 0.045in (downsized for compression)`).
- The `generate` CLI prints the same report **only under `-v`** (full
  table). By default the per-label detail lives in the report files and
  the CLI's stdout carries a single summary line — see the output contract
  in [`../cli/AGENTS.md`](../cli/AGENTS.md).
- **The generate pipeline logs at WARNING, not INFO.** The operational
  chatter (stage timers, per-plate optimization summaries, the
  per-strategy benchmark tables, the font-catalog and substitution notes)
  is INFO, and the report carries everything worth knowing, so the default
  run keeps it out of the console *and* out of `logs_generate/generate.log`.
  `-v` escalates the whole pipeline (logger, console handler and file
  handler) to DEBUG. The per-stage wall clock therefore comes from the
  `StageTimings` collector rather than the log (see
  [`../utils/AGENTS.md`](../utils/AGENTS.md)); the profiling harness is
  unaffected because it runs the pipeline at INFO and reads the records.

### Job Specification Patterns

**Pattern 1: Explicit Labels List** (each label must have `width` and `height`)
```yaml
job:
  job_name: "Batch 01"
  text_height: 0.5
  labels:
    - id: "label_1"
      width: 3.0
      height: 1.0
      count: 10
      content:
        - text: "Line 1"
        - text: "Line 2"
```

**Pattern 2: Root-Level Single Label** (auto-repeated via `count`; dimensions required)
```yaml
job:
  job_name: "Simple Labels"
  width: 3.0
  height: 1.0
  text_height: 0.5
  count: 20
  content:
    - text: "Single repeating label"
```

**Pattern 3: Replacement Text File Template** (EngraveLab/Vision Pro style)
```yaml
job:
  job_name: "Batch 02"
  labels:
    - id: "badge"                    # expands to badge_0000, badge_0001, ...
      replacement_text_file: data.txt  # relative to the job YAML directory
      replacement_text_delimiter: ";"  # optional, default ";"
      content:                       # optional placeholders (see below)
        - text: "PLACEHOLDER L1"
          text_height: 0.45
```

**Pattern 4: Job-Level Replacement File** (no `labels` section needed)
```yaml
job:
  job_name: "Batch 03"
  width: 3.0                         # required: defines the label
  height: 1.0                        # required: defines the label
  text_height: 0.75                  # required: defines the label text
  holes:                             # optional, like any job-level attribute
    - location: sides
  replacement_text_file: data.txt    # each line becomes label_0000, label_0001, ...
  # content:                         # optional per-line attribute template
  #   - text: "PLACEHOLDER"
```

**Pattern 5: Plate-Level Replacement File** (labels pinned to one plate)
```yaml
job:
  job_name: "Batch 04"
  width: 3.0                         # required: defines the generated labels
  height: 1.0
  text_height: 0.75
  plates:
    - id: scrap_a                    # its file's lines pack ONLY onto scrap_a
      width: 24.0
      height: 16.0
      replacement_text_file: data_a.txt
    - id: full_b                     # normal plate (accepts unpinned labels)
      width: 24.0
      height: 16.0
  labels:                            # optional: static labels pack normally
    - id: header
      content:
        - text: "HEADER"
```

### Replacement Text Files (Badges / Multiples)

`plt_optimizer/generate/substitution.py` implements EngraveLab/Vision Pro
"badge" (a.k.a. "multiples") data-driven generation. A `LabelSpec` with
`replacement_text_file` is a template: **each data line of the file produces
one label copy** (the file's data-line count determines the count; `count`
must not be set alongside it). Items within a file line are split on
`replacement_text_delimiter` (default `;`; must be a single special or
whitespace character, never newline/alphanumeric).

- **File format:** data plus comments — every *data* line is a label copy.
  A line whose whitespace-stripped text starts with
  `replacement_text_file_comment_string` (default `"# "`; `""` disables
  comment handling, so every non-blank line is raw data) is a comment;
  whitespace-only lines are blank. Both
  kinds produce **no** copy and are reported together in one WARNING per
  file (1-based line numbers + text) so a shop can see exactly what was
  ignored. Blank lines are dropped **always** (even with comments disabled).
  A file left with no data lines raises `SubstitutionError`.
  CRLF/CR endings are normalized; a single trailing newline does
  not create a phantom copy; a UTF-8 BOM is stripped; items are used
  verbatim (no whitespace stripping). Loading failures raise
  `SubstitutionError` (a `ValueError` subclass).
- **Delimiter/comment cascade:** `replacement_text_delimiter` and
  `replacement_text_file_comment_string` cascade **label → plate (pinned) →
  job → `job-config.json` → code default** (`;` / `"# "`), resolved at
  `JobSpec` validation time (`_apply_replacement_cascades`, mirroring the
  `material`/clearance job → plate cascades) — expansion runs before
  `resolve_job_spec`, so the resolver never sees these fields. Each tier's
  value needs a file to apply to: a label's own file; a plate's own file
  *or* a file on a label pinned to it (`plate_id`); the job needs a file at
  any tier — a job-level delimiter/comment therefore drives **several**
  label-level files with no job-level `replacement_text_file` at all.
  `job-config.json` ships both as shop defaults
  (`replacement_text_delimiter` + `replacement_text_file_comment_string`,
  both `JobDefaults` keys in `_JOB_LAYER_FIELDS`) and injects them **only**
  when the raw spec declares a file somewhere
  (`_spec_declares_replacement_file`), so the shipped `"# "` default cannot
  break a file-less job.
- **`content` optional:** when omitted, every rendered line inherits
  label-level attributes. When present, each `text` value is a
  placeholder declaring that line's attributes (text_height, alignment,
  spacing, ...); the replacement item overrides only `text`.
- **Fewer items than template lines:** the copy renders only as many
  lines as items present (extra template lines dropped); the rendered
  block stays vertically centered (existing renderers center regardless
  of line count).
- **More items than template lines:** extra lines inherit label-level
  attributes (same as the no-content case).
- **Delimiter collisions:** items can never contain the delimiter (data
  is split on it), so users must choose a delimiter absent from their
  data — the EngraveLab constraint, enforced by construction.
- **Expansion:** `expand_job_spec(job, yaml_path)` runs between
  `parse_yaml()` and `resolve_job_spec()` (wired into `cli/generate.py`
  and `scripts/run_integration_test.py`). Each file line becomes a static
  `LabelSpec` with `count=1`, id suffix `_{index:04d}`, and replacement
  fields cleared. Static labels and root-level jobs pass through
  untouched; jobs without replacement labels return the same object.
- **Examples:** `examples/job_specs/replacement_job.yaml` with
  `examples/job_specs/replacement_text_sample.txt` / `replacement_text_assets.txt`.

#### Job-Level Replacement Files

`replacement_text_file` (plus `replacement_text_delimiter`) is also
accepted at the **job level**. This form *replaces* the `labels` section:
the schema rejects `labels` alongside it, and job-level `count` is
rejected (the file's line count determines the count). Because the
synthesized labels carry no label-level dimensions, job-level `width`,
`height` and `text_height` are **required** (a validation error names the
missing fields); every other job-level label attribute (`margin`,
`holes`, `max_h_compress`, ...) applies as usual. Job-level `content`,
when present, acts as the per-line attribute template exactly like
`LabelSpec.content` (it is *not* a second label source). Expansion
(`substitution._expand_job_level_replacements`) synthesizes one static
`LabelSpec` per file line with base id `label` (`label_0000`, ...) and
clears the job-level replacement fields and the consumed `content`.

#### Plate-Level Replacement Files

A `PlateSpec` may declare `replacement_text_file` (+ delimiter). Its file
lines synthesize labels **pinned to that plate** via
`LabelSpec.plate_id` (base id = plate id: `p1_0000`, ...): the layout
engine packs pinned entries in exclusive single-plate passes (pinned
plates first, declaration order), and the declaring plate accepts no
other labels. Label dimensions/typography are NOT plate fields — they
come from the job-level cascade, so job-level `width`/`height`/
`text_height` are required whenever any plate declares a file (same error
as the job-level form). Plate files are mutually exclusive with a
job-level file; root-level `content` acts as the shared attribute
template (and root-level `count` is rejected in that mode). A job whose
only label source is plate files is valid (no `labels`/`content`
needed). Expansion clears the plate replacement fields on the returned
copy. Layout failures raise `LayoutFitError`: pinned labels that do not
fit their plate never spill onto other sheets, undeclared `plate_id`
references abort, and unpinned labels with no unpinned plate left abort
with a dedicated message. `LabelSpec.plate_id` may also be declared
directly to pin a static label; `JobSpec` validates it against the
declared plates.


### Cascading Resolution
When a value is `None` at the TextLine/LabelSpec level, it inherits from the parent JobSpec. Cascade order for `hole_margin`: explicit label value → job value → default. Same precedence applies to `max_h_compress` (explicit 0.0 is honored, not treated as unset), `text_h_alignment` (explicit `center` is honored, not treated as unset), `min_hole_margin` (explicit 0.0 is honored; only `None` means unset), `hole_text_collision_distance` (explicit 0.0 is honored; only `None` means unset, falling back to the config or 0.15), `space_width_fraction` (explicit 0.0 is honored; only `None` means unset, falling back to the config or 0.3), `min_glyph_width_fraction` (explicit 0.0 is honored; only `None` means unset, falling back to the config or 0.0), and `kerning_window_fraction` (explicit 0.0 is honored; only `None` means unset, falling back to the config or 0.05), `kerning_penetration_scale` (explicit 0.0 is honored; only `None` means unset, falling back to the config or 1.0), `kerning_recession_scale` (explicit 0.0 is honored; only `None` means unset, falling back to the config or 1.0), `kerning_min_gap_fraction` (explicit 0.0 is honored; only `None` means unset, falling back to the config or 0.0), and `fallback_advance_fraction` (explicit 0.0 is honored; only `None` means unset, falling back to the config or 1.0), `underline_offset_fraction` (explicit 0.0 is honored; only `None` means unset, falling back to the config or 0.0), `underline_break_clearance_fraction` (explicit 0.0 is honored; only `None` means unset, falling back to the config or 0.0; the air at each underline break, never a position) and `underline_break_spaces` (explicit `false` is honored; only `None` means unset, falling back to the config or false; whether the underline breaks at inter-word spaces) and `html_brackets` (explicit pair is honored; only `None` means unset, falling back to the config or `"<>";` the value is an even-length string, first half left delimiter / second half right, halves must differ and contain no alphanumeric/newline characters) and `italic_angle` (explicit 0.0 is honored = upright; only `None` means unset, falling back to the config or 12; schema bounds `[0, 45]`), `cutter_downsize` (explicit `false` is honored; only `None` means unset, falling back to the config or true), `max_cutter_downsizes` (explicit 0 is honored; only `None` means unset, falling back to the config or 1) and `cutter_downsize_global` (explicit `false` is honored; only `None` means unset, falling back to the config or true) and `h_compress_global` (explicit `true` is honored; only `None` means unset, falling back to the config or false) and `optimize_line_content` (explicit `true` is honored; only `None` means unset, falling back to the config or false) and `optimize_line_content_max_lines` (explicit `None` means unset = no growth, no fallback; only an explicit cap > 0 applies) and `enforce_cap_line` (explicit `false` is honored; only `None` means unset, falling back to the config or true; the alphanumeric cap-line clamp, strictly pre-markup) and `use_baseline_spacing` (explicit `false` is honored; only `None` means unset, falling back to the config or true; cascades job → plate → label, never text lines, plate parity only) and `v_center_by_content` (explicit `true` is honored; only `None` means unset, falling back to the config or false; cascades job → plate → label, never text lines, plate parity only; inert while `auto_descending_character_adjustment` is false) and `auto_descending_character_adjustment` (explicit `false` is honored; only `None` means unset, falling back to the config or true; cascades job → plate → label, never text lines, plate parity only) and `descending_character_v_scale` (explicit `0.2` is honored; only `None` means unset, falling back to the config or 0.5) and `descending_character_raise` (explicit `0.0` is honored; only `None` means unset, falling back to the config or 0.7) and `descending_character_clearance` (explicit `0.0` is honored; only `None` means unset, falling back to the config or 0.0; a unitless factor of the engraved-stroke floor, not inches) and `descending_character_reciprocal_scale` (explicit `0.2` is honored; only `None` means unset, falling back to the config or 0.5; `[0.2, 1.0]`, `1.0` = reciprocal downscaling forbidden; all four floats cascade job → plate → label, never text lines, plate parity only). `cutter_size` (explicit-`None` precedence line → label → job → auto-select; no config tier, `gt=0.0` so an explicit `0.0` is a validation error, not a cascade value) and `target_boldness_factor` / `multistroke_overlap_ratio` (explicit-`None` precedence line → label → job → config, plate parity; falling back to 0.14 / 0.25; multi-stroke PLT fonts only, inert on an explicit `cutter_size`) and `supersubscript_downscale` (explicit `0.2` is honored; only `None` means unset, falling back to the config or 0.5; `[0.2, 1.0]`; the bare `<sup>`/`<sub>` run downscale + baseline-shift derivation, inert on the argumented `<sup:0.3>`/`<sub:-0.2>` form) and `strikethrough_offset_fraction` (explicit `0.0` is honored = the stroke sits on the baseline; only `None` means unset, which centres the `<s>` stroke on the cap-band midline — half `toolpath_text_height` — so the *default* is a derived value, not a constant) and `strikethrough_break_spaces` (explicit `false` is honored; only `None` means unset, falling back to the config or false; whether the strikethrough breaks at inter-word spaces), `v_center_chars` (explicit `""` is honored; only `None` means unset, falling back to the config or `""`; the auto-vertical-centred character set, `<vc>`) and `hole_adjust_for_cutter` (explicit `false` is honored; only `None` means unset, falling back to the config or true; the cutter-compensated drill-hole diameter, cascades label -> job, plate parity only). `material` cascades label → job (and plate → job via the clearance-style plate cascade): an explicit label/plate value wins, an explicit `null` counts as unset, and empty-after-trim strings are validation errors.

### Stroke-Color Toolpath Splitting (`text_color`)

`text_color` (enum `TextColor`) is a layer tag that splits otherwise-identical
text into separate toolpaths so the cutter depth can change between runs on
3-layer material (EngraveLab/Vision Pro stroke-color workflow). Accepted on
**labels and text lines only**: it deliberately never cascades (resolution
chain line → label → `"none"`, no job tier) and a job-level value is rejected
by a `JobSpec` validator. It is also absent from `PlateSpec` and
`job-config.json` (unknown key there).

- Values: full names or 1-letter abbreviations, both case-insensitive
  (`_missing_` normalization): cyan/c, magenta/m, yellow/y, black/k, red/r,
  green/g, blue/b, violet/v, orange/o, pink/p, teal/t, none/n.
- `none` is the implicit default of every line that omits the field and is
  rejected when specified explicitly (field validator on `TextAttributes`).
- The inline `<tc:color>...</tc>` markup applies the same split **per
  character** (the `<f:>` resolver precedent, canonicalized at parse time;
  `<tc:none>` and unknown colors are a `MarkupError`). Inside its span the tag
  wins over the field; the field is the fallback for unmarked characters. The
  pen map keys on the line's derived `used_layers` (the distinct
  `(cutter, color)` pairs), so a colored run's strokes -- and each
  `<u>`/`<s>` decoration stroke, whose runs split at color boundaries --
  land on their own color's pen and per-color PLT file. See
  [`markup/AGENTS.md`](markup/AGENTS.md) "Inline Text Color".
- `build_cutter_pen_map` keys on `(cutter_diameter, text_color)` sorted by
  that tuple: smallest layer keeps SP1, rest SP4+ (SP2/SP3 reserved). Jobs
  without colors produce the historical cutter-only assignment bit-identically.
- `export_per_cutter_plts` writes one text file per `(cutter, color)` pen;
  colored layers gain their 1-letter suffix (`40_m_txt_<job>.plt`),
  colorless jobs keep the plain cutter-first names. The `bh` structural file
  is never tagged. The cutter token is the diameter in integer thousandths
  of an inch, unpadded (`0.040` -> `40`), and every PLT name is capped at 39
  characters including the extension (the plotter controller rejects longer
  names -- `core/filenames.ensure_plot_filename_length`, shared with the
  optimize/watch pipeline). Every name component (job id, material tag,
  unknown-color fallback) is reduced by
  `core/filenames.sanitize_filename_component`: NFKD transliteration drops
  international characters (`Café` -> `Cafe`) and each punctuation run becomes
  a single underscore (`wb(uv)` -> `wb_uv`), so output names carry only ASCII
  letters, digits and underscores.


### Colored Combined PDF Previews (`preview_colors`)

The combined per-plate proof `pdf/[<plate>_][<material>_]all_<job_id>.pdf`
renders an accurate preview of the engraved label: text ink in its real
color over the material's background color on the (light grey,
configurable) proof page. The color logic lives in `preview_colors.py`
(pure string handling, matplotlib-free, Python 3.8-safe -- it sits on the
headless export path); only the combined plots are colored, the per-cutter
`_txt_`/`_bh_` PDFs and the `*_default.pdf` diagnostics keep the historical
black ink on white.

- **Page background**: `job-config.json` `preview_background_color` (hex
  `#RGB`/`#RRGGBB`, validated by a `JobDefaults` field validator via
  `normalize_preview_background_color`, default `#D3D3D3` =
  `DEFAULT_PREVIEW_BACKGROUND_COLOR`) colors the combined proof's figure +
  axes (`plot_physical_strokes(background_color=...)`). Display-only: it is
  deliberately NOT in `_JOB_LAYER_FIELDS`, so it never injects into the
  (extra-forbidding) `JobSpec` and never touches PLTs.
- **Material -> colors** (`parse_material_colors`): the free-form cascading
  `material` name yields `(background, text)`. Three case-insensitive forms,
  first match wins: delimiter (`red-on-white`, `red:white`, `red|white`,
  `red/white`; first token = top layer/background, second = text), full-name
  prefix (`white`, `black(uv)` -> background only), and the character run
  (leading recognized color letters, stop at the first unknown one so
  property suffixes like `bwuv` / `wb(uv)` are ignored): 1 letter =
  background only, exactly 2 = background + core/text (`bw` = black over
  white, `wb` = white over black), 3 = multi-layer stock (`wrb` =
  white/red/black) where only the background is certain. Material letters
  follow the shop convention **b = black** (not TextColor's b = blue);
  `k` is black too and `w` is white (blue is only reachable via the
  full-name/delimiter forms). Unset/unrecognized -> `(None, None)`, which
  the export maps to `DEFAULT_MATERIAL_COLORS` (the wb proof: white label
  background, black text, black outlines -- the shop default).
- **Text color precedence** (`resolve_text_color`): the pen's stroke-color
  tag (the `text_color` field or an inline `<tc:color>` run -- the pen map
  already splits per `(cutter, color)`, so `export_per_cutter_plts` builds a
  `pen_colors: dict[int, str]` from `layer_by_pen` and the preview needs no
  per-character arrays) > the material core color > black, except an
  unresolved color over a DARK background (black/blue/red/green/magenta/
  violet) renders white so the proof stays legible.
- **Contrast outlines** (`outline_color_for` / `is_white_color`): the label
  border (SP2) and drill-hole (SP3) inks render OPAQUE in ONE shared
  contrasting color -- black over a white material background, white over
  every other background (the simple automated rule; `#D3D3D3` grey is not
  white). Holes always match their label's border color (the disc they
  punch is the page color, so the outline reads against the material).
- **Background fills**: the label boundary rectangles (SP2) enclose the
  material; `_write_simple_plots` turns the parsed SP2 closed runs into
  enclosed-area polygons (`diagnostics.stroke_geometry.closed_path_polygons`
  -- union semantics, no cutter width) and passes them as
  `PhysicalFillLayer`s, which `plot_physical_strokes` draws beneath the ink.
  Drill holes (SP3) punch the preview background color: their closed circles
  become a second fill set (appended after the background fill, so the disc
  sits on top of the material color) -- the "see-through" look.
- **Stroke widths are the ENGRAVED widths**: `export_per_cutter_plts` passes
  `pen_widths` (pen -> tool + kerf buffer) and `file_widths` (written PLT path
  -> engraved width, because the mirrored per-file previews read their width
  from the file-name token, which stays the *physical* tool). See "Physical
  vs. effective cutter diameter".
- **Fallbacks unchanged**: no `pen_widths` (synthetic export) keeps the thin
  centerline preview; a stem whose cutter token cannot be parsed keeps the
  per-file thin render; no `file_widths` (direct callers) plots the nominal
  token width.
- Pinned by `tests/test_preview_colors.py` (every convention + precedence +
  the hex/contrast helpers), `tests_deps/preview_color_job.yaml` +
  `TestPreviewColors` (`tests/test_phase3_export.py`: bw combined
  fill/ink/outline colors, configurable page background, wb black-border
  contrast, per-file black-only, material-less wb-default proof),
  `TestClosedPathPolygons` (`tests/test_stroke_geometry.py`),
  `TestPlotPhysicalStrokesColors` + `TestPlotPhysicalStrokesBackground`
  (`tests/test_plotter.py`), and the config-field tests
  (`tests/test_job_config.py`).


### Integration Points
- `parse_yaml(file_path)` returns a `JobSpec` ready for downstream bin-packing and rendering pipelines
- `expand_job_spec(job, yaml_path)` (substitution.py) must run immediately after `parse_yaml()` before `resolve_job_spec()` to flatten replacement-driven labels
- All numeric fields support Pydantic's `ge` (greater-than-or-equal) validators for safety
- Use `job.labels` or synthesize from root-level `content` + `count` when processing

### Job Defaults Config (`job-config.json`)

`plt_optimizer/generate/job_config.py` defines a shop-level JSON companion to
`tools.json` (default path `job-config.json`, selected via the `generate` CLI's
`--job-config` flag) so a shop can maintain one defaults file per engraver /
toolset. `parse_yaml(spec, job_config_path=...)` loads it and injects its
values into the raw job mapping **before** `JobSpec` validation, always at the
top-most layer:

- Cascading attributes (`text_height`, `font`, `character_spacing`, `line_spacing`,
  `margin`, `h_margin`, `v_margin`, `hole_margin`, `min_hole_margin`, `hole_text_collision_distance`,
  `cutter_buffer_text`, `cutter_buffer_routing`,
  `max_h_compress`, `text_h_alignment`, `space_width_fraction`,
  `min_glyph_width_fraction`, `kerning_window_fraction`, `kerning_penetration_scale`,
  `kerning_recession_scale`,
  `kerning_min_gap_fraction`, `fallback_advance_fraction`, `sc_scale`, `underline_offset_fraction`,
  `underline_break_clearance_fraction`,
  `underline_break_spaces`,
  `strikethrough_offset_fraction`,
  `strikethrough_break_spaces`,
  `v_center_chars`,
  `target_boldness_factor`, `multistroke_overlap_ratio`,
  `cutter_downsize`,
  `max_cutter_downsizes`, `cutter_downsize_global`, `h_compress_global`,
  `optimize_line_content`, `enforce_cap_line`, `holes`, `allow_rotation`,
  `text_chunk_mode`, `layout`, `rounded_rectangle_fraction`,
  `rounded_rectangle_proportional`, `hole_adjust_for_cutter`) fill missing **job-level** keys; the existing
  label -> job cascade then works unchanged and YAML values always win (an
  explicit YAML `null` counts as unset; `holes: []` suppression is a value).
  `hole_diameter` additionally fills the `diameter` of any hole entry (config-
  or spec-declared) that omits it.
- **Margin suppression:** a spec-declared `margin` (job level, **or on any
  label**) suppresses the configured `h_margin` / `v_margin` injection
  (`job_config._spec_declares_margin`). The margin cascade checks
  `job.h_margin` / `job.v_margin` *before* `margin`, so an injected
  axis-specific default would silently outrank the spec's universal margin
  on that axis (the reported bug: YAML `margin: 2.0` applied vertically
  while the config's `h_margin: 0.3` won horizontally). A YAML-declared
  `h_margin`/`v_margin` always keeps its own value; a spec with no margin
  at all keeps the configured axis defaults.
- `plate_width` / `plate_height` fill missing keys of each `plates` entry
  and the unbounded auto-allocated bins:
  the CLI threads `default_plate_size=(plate_width, plate_height)` into
  `vectorize.export_per_cutter_plts` / `layout.generate_layout*`. They never
  fill the job-level label `width`/`height` (that would defeat label
  auto-sizing).
- `left_clearance` / `top_clearance` are injected at the **job layer**
  (like the cascading attributes): the `JobSpec` job -> plate cascade then
  applies them to clearance-less plates, and the CLI derives
  `default_plate_clearance=(job.left_clearance, job.top_clearance)` from
  the parsed spec for the unbounded auto-allocated bins. YAML job/plate
  values always win.
- **Optional plate specification:** a job spec without `plates:` (or with
  an empty list) never synthesizes a single fallback plate. `provided_plates`
  stays `None` through `export_per_cutter_plts` into the layout engine's
  unbounded mode, which auto-allocates `default_plate_{i}` bins sized by
  `default_plate_size` (module default 24x16; the config values win) and
  overflows onto as many sheets as the labels need. A non-zero
  `default_plate_clearance` shifts placements on every auto-allocated bin
  (see `_default_clearance_map`), mirroring a plate list of identical
  clearance sheets.
- **Required-when-unconfigured:** `max_h_compress`, `hole_margin`,
  `min_hole_margin`, and `hole_text_collision_distance` must come from the
  config or the spec (job level, or declared on every label); without plates,
  `plate_width`/`plate_height` must come from the config. A field missing
  from the JSON, set to `null`, or whose file is absent (while a config path
  is in play) makes the field required: `assert_required_fields()` raises
  `JobConfigError` (a `ValueError` subclass) listing the offenders.
  Enforcement only runs when a config path is supplied, so direct API /
  test callers of `JobSpec(...)` / bare `parse_yaml()` keep the historical
  all-optional contract (resolution's hardcoded fallbacks remain as the
  no-config safety net).
- Malformed JSON or unknown config keys fail loudly (`JobConfigError`);
  `description` is an accepted free-form key (mirrors `tools.json`).
- `preview_background_color` (hex `#RGB`/`#RRGGBB`, validated by a
  `JobDefaults` field validator) is the one **display-only** key: it is
  deliberately NOT in `_JOB_LAYER_FIELDS` (never injected into the
  extra-forbidding `JobSpec`) and is read directly from
  `job_config.defaults` by `export_per_cutter_plts` to color the combined
  `all_*.pdf` proof page (default `#D3D3D3`; see "Colored Combined PDF
  Previews").

## Stage timing contract (`utils/stage_timing.py`)

- `plt_optimizer/utils/stage_timing.py` is the single source of the
  pipeline timing-line format: `stage_timer(name, logger)` emits one INFO
  line `stage <name>: <s>s` per completed stage (via `time.perf_counter`,
  in a `finally`, so a raising stage is still timed). Import-light (stdlib
  only) and Win7-safe, so it lives in `utils/` (the shared substrate) and
  is imported at module scope by `vectorize`/`cli/generate`, the
  `cli/optimize` CLI, and `core/pipeline.py`.
- Stage names are a stable contract consumed by
  `scripts/profile_generate.py` (`STAGE_LINE_PATTERN`): `parse`, `resolve`
  (cli/generate.py), and `reflow`, `cutter_downsize`, `h_compress`,
  `layout_render_pack`, `assemble_split`, `optimize_layer`, `plots`,
  `default_plots` (vectorize.export_per_cutter_plts). `optimize_layer` /
  `assemble_split` fire per plate/layer (the harness sums repeats). Format
  pinned by `tests/test_stage_timers.py`; changing the format must update
  the regex + harness together.
- Timers are log-only (zero pipeline behavior change); the module logger is
  named `_LOG` so the `logger: TextLogger` parameter never shadows it.
  The optimize pipeline's own stage names live in
  [`../core/AGENTS.md`](../core/AGENTS.md) +
  [`../cli/AGENTS.md`](../cli/AGENTS.md); full profiling workflow for both
  pipelines: [`../../docs/PROFILING.md`](../../docs/PROFILING.md).

