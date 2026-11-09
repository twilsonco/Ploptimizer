# AGENTS.md - Label Generation Pipeline (`plt_optimizer/generate/`)

Parent contract: [`../../AGENTS.md`](../../AGENTS.md) (coding standards, testing, git rules, the 1016
device scale, the Python 3.8 / Windows 7 constraint). This file is the component contract for the YAML-spec label generation pipeline.

**This file is a routing index, not a manual.** Canonical per-field semantics are the
`Field(description=...)` strings in `schema.py`/`job_config.py` (generated into
`docs/skills/engraving-jobs/engraving-job-complete-reference/references/JOB_SPEC.md` and the JSON
schemas, drift-pinned by `tests/test_job_spec_docs.py`); derivations and measurements live in the
owning module's docstring (cited at each section head); operator guidance lives in the skill tree's
`semantics.md` family. What lives here is what exists nowhere else: **invariants, pipeline ordering,
cross-file impact, test pins.** Never restate a field description; never copy a `job-config.json` value (say "the shop value").

Sub-contracts in this directory tree - read them when touching those modules:

- [`markup/AGENTS.md`](markup/AGENTS.md) - the inline markup framework and every tag registered in
  `markup.TAG_REGISTRY` (the registry is the source of truth for membership; never re-list tags or counts here).
- [`collision/AGENTS.md`](collision/AGENTS.md) - text-hole collision avoidance + cutter-compensated
  drill holes (detection, phases, relief threading).
- [`packing/AGENTS.md`](packing/AGENTS.md) - plate-space toolpath optimization + bin-packing
  (rotation, fill order, clearances, material, ellipse, rounded-rect).
- [`../cli/AGENTS.md`](../cli/AGENTS.md) - the CLI surface.

Cross-references use `§Section` for a heading in this file and name the file for a heading in another contract.

## YAML Job Specification & Label Generation Schema

### Overview

`schema.py` defines the data contract for YAML-spec label generation; all models are
Pydantic and inherit typed bases supporting cascading attributes.

**Strict schema (every model forbids unknown fields).** All 12 spec/config models set
`model_config = ConfigDict(extra="forbid")` (on the `HoleSpec`/`TextAttributes`/
`PlateSpec` roots - inheritance covers the rest - and on `JobDefaults`). A misspelled key
at any tier (job, label, content line, list block/item, plate, hole) fails loudly at
`parse_yaml()` with its exact dotted path: a field the user typed is a field the user
expects to engrave, so silent ignoring is never acceptable. The free-form `tool_options`
*value* is unaffected (its keys are engraver option names), but its `JobDefaults`
**metadata entries** are gated by `validate_tool_options_metadata`: non-empty `command` (no
`;`), a `bool`/`int`/`float` `type`, a `default` (scalar, dual `text`/`borders_holes`,
or an explicit `null` layer to omit that PLT type), numeric `min`/`max` with `min <= max`, and a snake_case key.
Every problem is reported in one `JobConfigError` at `load_job_config()` - these shapes used
to crash mid-export, emit a header with no command prefix, or silently drop a cutter option
- and `JobDefaults.font` is canonicalized with the same `normalize_font_name` rule the spec
  tiers use. Pinned by `tests/test_job_config.py::TestToolOptionsMetadataValidation` and
  `tests/test_schema.py::TestStrictUnknownFields`; the generated JSON schemas carry the
  structural contract as `additionalProperties: false` (`tests/test_job_spec_docs.py`).
  Pre-flight CLIs: `scripts/validate_job_config.py`/`scripts/validate_job_spec.py` (the
  `validating-jobs` skill mirrors both byte-for-byte).

### Adding New Fields to the Schema

1. **Add the field to the model(s)** in `schema.py`: type hints + a `description` (rendered
   as markdown in schema hovers and `JOB_SPEC.md` - backtick markup tags and multi-word field
   names, formulas in `$...$`; pinned by `tests/test_prose_markdown.py`, see
   [`../../docs/AGENTS.md`](../../docs/AGENTS.md)). Cascade it in the validators
   (explicit-`None` precedence) on every tier needed. **Plate parity:** the `PlateSpec`
   re-declaration comes from `_parity()` (upper-tier tail from `PLATE_PARITY_TAIL`) - never
   retype the "(schema parity; not applied at plate level)" clause. Multi-tier replacement
   fields reuse `_DELIMITER_CORE`, `_COMMENT_HEAD`/`_COMMENT_TAIL`, `_BLANK_LINES_CORE`, `_REPLACEMENT_FILE_LEAD`.

2. **Add it to `JobDefaults`** in `job_config.py` with a description, and add the config
   name to `_JOB_LAYER_FIELDS` - the single source of truth for the config → job-layer
   injection set. Gate with replacement files via `_REPLACEMENT_SUPPRESSED_FIELDS`, with
   margins via `_MARGIN_SUPPRESSED_FIELDS`; decide whether it joins `REQUIRED_WHEN_UNCONFIGURED`.

3. **Update both generators** - semantics are written ONCE, in the `description`, and
   generated outward. `generate_ai_docs.py`: job-spec fields auto-generate, but a new
   `JobDefaults` field must also join its group in `_CONFIG_GROUP_FIELDS` (or
   `_CONFIG_EXTRA_GROUPS`), else `config_group_problems()` fails the docs tests; run it, then
   `git add docs/skills/engraving-jobs/engraving-job-complete-reference/references/` (verifies
   `tests/test_job_spec_docs.py::test_job_spec_schema_json_is_up_to_date` and
   `test_job_config_schema_json_is_up_to_date`). `generate_skills.py`: add the field to its
   theme's `fields` tuple (+ a pitfall if non-obvious), run it, `git add docs/skills/`
   (verifies `tests/test_job_spec_skills.py::test_registry_problems_none` and
   `test_every_schema_field_covered_exactly_once`). Skipping either causes schema drift, registry failures, and broken skill routing.

4. **Add one line to the §Core Data Model index below** - tiers, code fallback, owning section.

5. **Write tests**: `tests/test_schema.py`, `tests/test_job_config.py`,
   `tests/test_resolution.py` (cascade), and a `tests_deps/*_job.yaml` fixture pinned in `tests/test_phase3_export.py`.

6. **Run**
   `uv run pytest tests/test_schema.py tests/test_job_config.py tests/test_job_spec_docs.py tests/test_job_spec_skills.py -v`
, then the full suite; fix generated-file drift before committing.

### Core Data Model

**Inheritance hierarchy (top-down cascade):**

```
JobSpec (job-level defaults)
├── LabelSpec (per-label overrides)
│   └── ContentEntry / TextLine (text lines + itemize/enumerate blocks)
└── PlateSpec (material sheet definitions)
```

The field set is `TextAttributes` (51) + the `LabelAttributes` additions (21) in `schema.py`; this is
a **routing index** (tiers, code fallback, owning section), never a restatement of the `description`
strings. `parity` = accepted on `PlateSpec` for schema parity only, not applied at plate level (labels
render before packing). "shop value" = the `job-config.json` entry - hand-tuned, never cited
numerically. **Every `TextAttributes` field cascades line → label → job (parity)** and joins the shop
layer where `_JOB_LAYER_FIELDS` includes it; exceptions are named inline.

- `text_height` (inches; 0.25) · `character_spacing` (inches; 1.5× the *effective* cutter) ·
  `line_spacing` (inches, or `auto`; the requested value, clamped at render, `auto` resolved at job
  level) → §Baseline-to-Baseline Line Spacing.
- `font` - PLT catalog key or `fonts/**.ttf` basename (`<name> SC` = synthetic small caps); canonicalized
  by `font_registry.resolve_font`, unknown names rejected; PLT renders arc-native with baseline-anchored
  v2 glyphs + profile-envelope kerning, TTF scales by the `H` reference glyph so cap height equals
  `text_height` on every line; a missing glyph raises `PltFontRenderError`/`FtextRenderError` →
  `LabelRenderError` (CLI non-zero). Symbol sheets (`plt_font_is_symbol()`) are exempt from the 17
  derived Unicode glyphs and from `SC`. Contract: [`../../fonts/AGENTS.md`](../../fonts/AGENTS.md).
- PLT kerning (`ge=0.0`, explicit 0.0 honored, fallback inline): `space_width_fraction` (0.3) ·
  `min_glyph_width_fraction` (0.0 = pure envelope kerning; the floor is capped by the left glyph's own
  width) · `kerning_window_fraction` (`[0, 1]`, 0.05 - widens an advance, never narrows) ·
  `kerning_penetration_scale` · `kerning_recession_scale` (non-negative / gapped windowed penetration;
  1.0) · `kerning_min_gap_fraction` (0.0) · `fallback_advance_fraction` (1.0).
- `kern_pos_string`/`kern_neg_string`/`kern_width_fraction` - manual-kern advance / retreat markers
  (stripped, never engrave; no clamp, so glyphs may overlap) + the step / cap height (0.1); markers take
  the shop value and resolution re-validates them against the line's effective `html_brackets` + tag
  registry (`MarkupError` on collision) → [`markup/AGENTS.md`](markup/AGENTS.md) "Manual Kerning".
- Inline-markup typography (contracts in [`markup/AGENTS.md`](markup/AGENTS.md)): `html_brackets`
  (even-length delimiter string: left half + right half; `<>`) · `italic_angle` (`[0, 45]`, 12) ·
  `supersubscript_downscale` (`[0.2, 1.0]`, 0.5; drives the bare `<sup>`/`<sub>` shift) ·
  `underline_offset_fraction` · `underline_break_clearance_fraction` (both / cap height, 0.0) ·
  `underline_break_spaces` · `strikethrough_break_spaces` (false) · `strikethrough_offset_fraction`
  (`None` = cap-band midline, a **derived** default) · `v_center_chars` (`''` = off; shop value).
- `sc_scale` (`[0.2, 1.0]`, fallback 0.7) → §Small Caps Fonts.
- `max_h_compress` (`[0, 1]`; 0.0 = disabled; **required** when a config is in play) ·
  `h_compress_global` (false) · `h_compress_global_same_height_only` (true) → §Horizontal Text Compression, §Per-Label Shared Horizontal Compression.
- `optimize_line_content` (false) · `optimize_line_content_max_lines` (`ge=1`; **unset = no growth**,
  growth-only, one value per group) → §Word-Level Line-Content Reflow, §Growth cap.
- `cutter_size` (`gt=0.0`, snapped to the inventory; `None` = automatic) - **no config tier**, so it can
  never become a legal shop default; a tool whose engraved width reaches `text_height` raises
  `CutterSizeError` → §Physical vs. effective cutter diameter.
- `cutter_buffer_text` (fallback 0.0, shop value) - kerf buffer added to a text cutter's physical diameter to get
  the engraved width · `cutter_buffer_routing` - the same buffer on the boundary/drill-hole cutter:
  **label → job → shop value** (parity on text lines + plates). Both honor explicit 0.0 → §Physical vs. effective cutter diameter.
- `cutter_downsize` (true) · `max_cutter_downsizes` (`ge=0`, 1, 0 disables) · `cutter_downsize_global`
  (true) · `cutter_downsize_global_job_wide` (false) → §Compression-Driven Cutter Downsizing.
- `target_boldness_factor` (`[0.05, 0.30]`, fallback 0.14) · `multistroke_overlap_ratio` (`[0.1, 0.5]`,
  fallback 0.25 - the gap-free floor) → §Multi-Stroke (Bold) PLT Fonts · `bold_boldness_multiplier` (`[1.0, 4.0]`, fallback 1.5, the
  `<b>` boost) → [`markup/AGENTS.md`](markup/AGENTS.md) "Bold Markup".
- `enforce_cap_line` (true; strictly pre-markup) → §Alphanumeric Cap-Line Enforcement ·
  `arc_preserve_compression` (true; `False` restores polygonized output bit-identically) → §Arc-Preserving Non-Uniform Transforms.
- `text_color` (`TextColor`) - **line and label only: it never cascades**; absent from `PlateSpec` and
  `job-config.json` → §Stroke-Color Toolpath Splitting.
- List knobs, all **item → block → label → job → shop value** (parity): `bullet_string` (`•`; `""` = no
  marker; a newline is a validation error) · `counter_start_index` (`ge=1`, 1) · `counter_style`
  (`decimal`, `lower-alpha`/`upper-alpha` (bijective base-26), `lower-roman`/`upper-roman`) ·
  `counter_prefix`/`counter_suffix` (`""`) · `list_marker_gap_fraction` (0.5) → §Bullet & Enumerated Lists.

#### `LabelAttributes` additions (label/job tier; block-scoped)

- `width`/`height` (`gt=0.0`) - **required** at label or job level (no auto-sizing); a `round` label
  needs only one (the other fills to a circle).
- `type` (`rect`; `round` = ellipse with semi-axes `width/2`, `height/2`) ·
  `rounded_rectangle_fraction` (`[0, 1]`, fallback 0.0 = sharp; superellipse quadrants) ·
  `rounded_rectangle_proportional` (false) - the latter two cascade **job → plate → label** →
  [`packing/AGENTS.md`](packing/AGENTS.md) "Circular / Elliptical Labels", "Rounded-Rectangle Labels".
- `margin` (the axis-specific fallback; **required** when configured; declaring it **suppresses the
  configured `h_margin`/`v_margin`** → §Job Defaults Config) · `h_margin`/`v_margin` (outrank `margin`
  on their axis; **required** when configured).
- Hole geometry, all **label → job** (parity), contract in
  [`collision/AGENTS.md`](collision/AGENTS.md): `hole_margin` (**required**) · `min_hole_margin` (the
  floor it may be reduced to; `None` = collision avoidance disabled) ·
  `hole_text_collision_distance` (air between the engraved text and hole strokes; fallback 0.15; explicit 0.0
  honored - strokes may touch, never overlap) · `holes` (a label value **replaces** the job list; `[]`
  suppresses; `corners`/`sides` expand at validation via `HOLE_LOCATION_GROUPS`) ·
  `adjust_h_center_for_single_side_hole` (true) · `hole_adjust_for_cutter` (true; engraved diameter =
  declared, the centerline shrinks by the tool) · `align_margin_adjust_global` (false; explicit true
  honored) → [`collision/AGENTS.md`](collision/AGENTS.md) "Sharing Inherits the Label Groups".
- `material` - free-form stock name (trimmed, case-insensitive); labels sharing a material pack together
  and a plate never mixes materials. job → label, job → plate (an explicit plate value wins) →
  [`packing/AGENTS.md`](packing/AGENTS.md) "Material-Aware Grouping".
- Stacking block knobs, all **job → plate → label, never text lines** (parity; labels render before
  packing): `use_baseline_spacing` (true) → §Baseline-to-Baseline Line Spacing · `v_center_by_content`
  (false; inert while the relief is off) → §Baseline-to-Capline Band Centering ·
  `auto_descending_character_adjustment` (true) · `descending_character_v_scale` (`[0.2, 1.0]`, 0.5) ·
  `descending_character_raise` (`[0, 1]`, fallback 0.7 - a fraction of the glyph's **own descender depth**) ·
  `descending_character_clearance` (`ge=0.0`, 0.0 - a **unitless factor of the engraved-stroke floor**,
  never inches) · `descending_character_reciprocal_scale` (`[0.2, 1.0]`, 0.5 - `1.0` forbids the
  reciprocal half = the historical one-sided relief, bit-identically) → §Automatic Descending-Character Relief.

#### Content entries (line-level; never inherited, no config tier) · `HoleSpec`

- `text` - required (non-empty) on a plain `TextLine`, **must be omitted** on a list entry.
  `placeholder` - the line's attributes survive, only `text` is substituted; requires the label's
  `replacement_text_file` → §Replacement Text Files (Badges / Multiples). `new_line` - hard break for
  reflow: the line **starts** a new `optimize_line_content` group, inert unless the line enables reflow,
  and the cap stays group-scoped → §Word-Level Line-Content Reflow.
- `itemize`/`enumerate` - a nested list block, mutually exclusive with `text`; `items` is its
  `TextLine` list (`min_length=1`) and typography cascades item → block → label → job for **every**
  `TextAttributes` field → §Bullet & Enumerated Lists.
- `location` - one of the 8 atomic edge locations, or the `corners`/`sides` shorthand
  (expanded at validation; downstream sees atomics only). `diameter` (`gt=0.0`, 0.125) - the
  config `hole_diameter` fills entries that omit it.

#### Job / plate fields owned by other contracts

- `job_name`, `labels`, `plates`, `content`, `count` - **exactly one** label source: `labels`, root
  `content`, a job-level `replacement_text_file`, or plate-level files. `plate_id` (label) pins a label
  to a declared plate (validated against `plates`); pinned entries pack in exclusive single-plate passes
  → §Job Specification Patterns, §Plate-Level Replacement Files, [`packing/AGENTS.md`](packing/AGENTS.md).
- `layout` (`columns`, `rows`, `columns-simple`, `rows-simple`) · `allow_rotation` ·
  `text_chunk_mode` (`line` | `word`) -> [`packing/AGENTS.md`](packing/AGENTS.md)
  "Plate-Space Fill Order", "Text Chunking". -
  `auto_line_spacing_interline_to_top_bottom_ratio` (inter-line gap = ratio x top/bottom gap)
  · `replacement_text_file` + `replacement_text_delimiter`/
  `replacement_text_file_comment_string`/`replacement_text_file_allow_blank_lines` (label /
  plate / job) · `tool_options` (job; free-form value, validated config **metadata** ->
  §Overview) · `id` (label, plate) · plate `width`/`height` (usable pack area) · plate `left_clearance`/`top_clearance`.
- `auto_line_spacing_interline_to_top_bottom_ratio` (inter-line gap = ratio x top/bottom
  gap) - `replacement_text_file` + `replacement_text_delimiter`
  /`replacement_text_file_comment_string`/`replacement_text_file_allow_blank_lines` (label /
  plate / job) - `tool_options` (job; free-form value, validated config **metadata** ->
  §Overview) - `id` (label, plate) - plate `width`/`height` (usable pack area) - plate `left_clearance`/`top_clearance`.

### Key Classes

| Class | Purpose | Validation Rules |
|-------|---------|------------------|
| `JobSpec` | Root job container | Requires exactly one label source: `labels` list, root-level `content`, a job-level `replacement_text_file`, or plate-level replacement files (mutually exclusive); `width`/`height` must be defined at job or label level (no longer auto-sized); a `round` label needs only ONE dimension (the other fills to a circle) |
| `LabelSpec` | Individual label definition | `count >= 1`; requires `content` (min 1 TextLine) OR `replacement_text_file` (mutually exclusive with `count`); `width`/`height` must be defined at label or job level (a `round` label needs only one); optional `plate_id` pins the label to a declared plate; a label-level `replacement_text_delimiter`/`replacement_text_file_comment_string` requires the label's own file |
| `TextLine` | Text content unit | Requires non-empty `text` string |
| `ContentEntry` | One `content` entry: a text line **or** a list block | Subclasses `TextLine` (`text` becomes optional); requires **exactly one** of `text`/`itemize`/`enumerate`; a `placeholder: true` item inside a list block is rejected (a list's items are authored, never substituted) |
| `ItemizeBlock`/`EnumerateBlock` | Nested bullet / enumerated list | `items` (min 1 `TextLine`) + the cascading list settings; both extend `TextAttributes`, so a block carries every typographic field and its items inherit them (item → block → label → job) |
| `PlateSpec` | Physical sheet definition | All dimensions `>= 0`; `width`/`height` are the usable pack area, offset from the material's top-left by `left_clearance`/`top_clearance` (both default `0.0`); optional `replacement_text_file` synthesizes labels pinned to this plate (requires job-level `width`/`height`/`text_height`) |
| `HoleSpec` | Drilled hole definition | Location (required; 8 atomic enum values: corners + edges, plus `corners`/`sides` group shorthands expanded at validation) + optional `diameter` (default 0.125", must be > 0) |
| `parse_yaml()` -> `expand_job_spec()` | Entry point + replacement expansion (`substitution.py`) | `parse_yaml()` returns a validated `JobSpec` or raises `ValueError`; `expand_job_spec()` runs immediately after it and flattens replacement-driven labels (job-, plate- or label-level) into static `LabelSpec`s |

### Pipeline order

The order is a contract: a pre-pass that moves changes what the next pass measures, and every demo fixture's numbers pin the current one.

1. `parse_yaml(path, job_config_path=...)` - inject the `job-config.json` job layer, validate → `JobSpec` → §Job Defaults Config.

2. `expand_job_spec(job, yaml_path)` (`substitution.py`) - **immediately after**
   `parse_yaml()`, before resolution: the cascading replacement tiers are applied at `JobSpec`
   validation time, so the resolver never sees them.

3. `resolve_job_spec(spec,..., extents_probe=...)` (`resolution.py`) - cascade resolution,
   the reflow cap fan-out (`_apply_reflow_line_caps`), cutter sizing, and collision **detection** (read-only) + its per-line geometry maps.

4. `vectorize.export_per_cutter_plts` pre-passes, in order: `apply_list_marker_layout` →
   `apply_line_content_reflow` (reflow FIRST: both compression mechanisms must see the reflowed
   widths) → `apply_global_h_compress` → `apply_compression_cutter_downsize` (the shared scale
   must exist before the midpoint trigger measures it) → `build_cutter_pen_map` → render / pack → assemble → optimize → plots.

5. `label_renderer._render_text_lines_by_pen` → `plate_optimizer` → the two per-cutter write
   sites → §PLT emission, filenames & write-boundary reflow.

All numeric fields support Pydantic `ge`/`gt`/`le` validators; use `job.labels` or synthesize from root-level `content` + `count`.

### Horizontal Text Compression

Over-wide lines are compressed **horizontally only** (glyphs + spacing scale together; Y
untouched) so they respect the inner content area. Opt-in via `max_h_compress` (0.0 =
disabled; **required** when a config is in play): `resolution.compute_horizontal_scale()` is
the pure factor math, clamped to `[1 - max_h_compress, 1.0]`, and
`label_renderer.compress_line_to_width()` applies it per line in `_render_positioned_lines`
(reached via `_render_text_local_with_bounds` and `_render_text_lines_by_pen`) **before**
centering. A line that already fits is never modified; unresolvable overflow logs a WARNING.
The *applied* scale is report-only - `RenderedLabel.compression_by_line` → §Applied Layout Reporting. Interlocks: §Per-Label Shared Horizontal Compression (label-wide scale),
§Compression-Driven Cutter Downsizing (scale → tool), §Arc-Preserving Non-Uniform Transforms (scale → arcs).

### Arc-Preserving Non-Uniform Transforms (`arc_fit`, `arc_preserve_compression`)

Derivation, the fitter, the measured numbers, the C/U/V maps: **`arc_fit.py` module docstring**; the
reference exports that pin the contract: `tests_deps/arc_compression/README.md`.

**Invariant: a PLT glyph is natively `PU`/`PD`/`AA` and its curves stay circular arcs.** A uniform
scale maps a circle to a circle; every non-uniform map - compression (`compress_x`), the cap-line
clamp and descender relief (`scaled_y`), the italic shear (`sheared`) - maps it to an ellipse. Each
arc's ellipse image is refit with a short chain of circular arcs (`arc_fit.fit_ellipse_arc`), never
polygonized. The four properties the pipeline depends on: the chain's `P(t0)` is the point-wise affine
image of the source arc's start, so arcs chain tip-to-tail against the neighbouring transformed
segments (verified to 1e-15) and meet with equal tangents (**C0 + G1**); **stroke count is preserved
1:1**, so `glyph_groups`, `TextChunkRecord.glyph_groups` and the intra-chunk glyph sweep stay valid
(only the segment count *within* a stroke grows; the relief splices via `replaced_strokes`, the
capline clamp rebuilds the block); the swept-box over-measure is ≤0.00022in, so `bounds()`,
`compression_by_line`, kerning and collision are unaffected; and all three maps have positive
determinant, so a fitted arc's sweep sign equals the source's (`_sweep_sign_ok` declines on disagreement).

- **The escape hatch is exact**: `arc_preserve_compression` (fallback true, **not**
  `REQUIRED_WHEN_UNCONFIGURED`) lands on `ResolvedTextLine`; `False` restores the historical
  polygonized output **bit-identically**. Threading: `_render_line_core_once` adds
  `preserve_arcs` only when the line opts out (the three-tier empty-kwargs gate keeps a
  default-True line on the historical call shape); `compress_line_to_width`/
  `_apply_collision_compress`/`_apply_glyph_ramps` take it from the line;
  `plt_font_renderer._walk_line` + the three public wrappers forward it to `_capline_geometry`/`_shear_geometry`.
- **The math, the fitter's C0 + G1 continuity, the 1:1 stroke-count preservation, the ≤0.00022in
  swept-box over-measure and the declined-arc logging gate (`declined_arcs` is 1 only when the refit
  returned `None`: a degenerate geometry, or a span past `ARC_FIT_MAX_ARCS` = 12) are contract
  surfaces of `arc_fit.py` - read the module docstring before touching them. `compress_x` logs a
  WARNING (compression is an authoring compromise); `scaled_y`/`sheared` log at DEBUG.
- **Arc-native label borders** (`_render_boundary_hpgl`, always on): the ellipse and the
  rounded rect emit as `AA` chains from the same fitter (`_ellipse_boundary_strokes`,
  `fit_superellipse_arc` - needs **trim-to-line**, since for `n > 2` the axis vertices have
  zero curvature), verified at 400 samples (`_contour_deviation`) at `BORDER_ARC_FIT_MAX_ARCS`
  = 32 per quadrant; a declined fit falls back to the polygonized contour with a WARNING,
  never silent. Emission needs the `_collect_hpgl_geometry` **pen-advance** fix: the emitted
  `AA` carries no radius, so without it a chained `PD;AA` run over-measures a 40-arc ellipse
  border by ~0.4in, breaking the nominal-box contract (pinned at `abs=3e-3`).
  `_render_boundary_local` and every margin/relief/inset path (`_border_box`,
  `_lower_ellipse_arc`, `_lower_rounded_rect_border`, `rounded_rect_content_inset`) **stay polygonized by contract**.
- **Out of scope**: TTF fitting (TTF blocks are all `LineSeg`), the font library,
  `Affine._require_similarity` in `fonts/glyph_transforms.py` (an extraction-time tool,
  correct to reject arcs), and the `optimize`/`watch` pipelines (they pass `ArcSegment` through end-to-end).
- **Pins**: `tests/test_arc_compression_parity.py` (compressed outline = the exact affine
  image, two-sided Hausdorff ≤0.02in; EngraveLab's arc count grows ≤1.25× under compression -
  per-arc index pairing is deliberately NOT asserted: EngraveLab re-tessellates the whole
  outline, so the files compare **as curves**); `tests_deps/arc_preserve_job.yaml` →
  `tests/test_phase3_export.py::TestArcPreserveCompressionDemoExample` and
  `TestWrittenPltLineFormatting`; `tests/test_text_geometry.py`.

### Alphanumeric Cap-Line Enforcement (`enforce_cap_line`)

Cap-line heights are not extractable from the sheets (the v2 library is baseline-normalized with the
reference char at exactly 1000 units, so every reference is 1.0). The clamp (docstrings
`plt_font_renderer._capline_geometry`, `ftext_renderer._apply_cap_line_enforcement`) downscales every
**ASCII alphanumeric** `[0-9a-zA-Z]` glyph whose ink passes the per-font capline, top-down about its
own lowest ink point (`y' = min_y + (y - min_y) * factor`,
`factor = (cap - min_y) / (max_y - min_y)`, X untouched), landing its top exactly on the capline.
Non-alphanumerics are never clamped; a glyph exactly at the cap never moves; the gate is ASCII-only
(`é` keeps its accent); and a symbol font's ASCII slots ARE alphanumeric, so Wingdings symbols clamp
too - the contract keys off the character, not the font family.

- **Strictly pre-markup**: first in the per-character transform chain, before small-caps,
  bold/`<scale:N>`, italic and `<sup>`/`<sub>`, so every markup transform composes on the clamped
  silhouette → [`markup/AGENTS.md`](markup/AGENTS.md) "Transform ordering".
- Applied inside the render walk so the block, bbox **and both profile envelopes** transform together
  (kerning measures the clamped silhouette): PLT in `_walk_line` after `_glyph_entry`, keyed by the
  font frame's `normalized_ref_height`, with the Y-only scale arc-refit → §Arc-Preserving Non-Uniform Transforms; TTF right after the normalization scale, before `_apply_small_caps`/`_apply_italic`/
  `_apply_glyph_scales`/`_apply_baseline_shifts`, via per-glyph contour slicing (the `with_words` gate
  routes clamped lines to exact slicing - the signature matcher cannot match clamped glyphs).
- A geometry transform only: it never moves `nominal_text_height`/`toolpath_text_height`, the cutter,
  the pen map, or the emitted file names, and the extents/width pre-passes see the clamped geometry for
  free (both route through `_render_line_block`).
- **Pins**: fixture `tests_deps/enforce_cap_line_job.yaml`
  (`tests/test_phase3_export.py::TestEnforceCapLineDemoExample`);
  `TestCapLineGeometryHelper`/`TestWalkLineCapLineEnforcement` (`tests/test_plt_font_renderer.py`),
  `TestApplyCapLineEnforcement` (`tests/test_ftext_renderer.py`), `TestEnforceCapLineDispatch`
  (`tests/test_label_renderer_gaps.py`), `TestEnforceCapLineCascade` (`tests/test_resolution.py`),
  `TestEnforceCapLineField` (`tests/test_schema.py`).

### Physical vs. effective cutter diameter (`cutter_buffer_text`, `cutter_buffer_routing`)

A cutter is two different numbers, and mixing them up is the one mistake this contract exists to
prevent. **Physical** = the tool in the spindle (`ResolvedTextLine.cutter_diameter`,
`ResolvedLabel.hole_cutter_diameter`) - an *inventory identity*: a 0.030in cutter must always produce
`30_txt_*.plt`. **Effective** = `physical + buffer` (`resolution.effective_cutter_diameter()`) - the
**width of material removed**: a V-bit with roughly 45° flanks cuts a groove wider than its nominal
diameter, so the nominal tool under-reports the stroke. Effective is exposed ONLY as derived
properties (`ResolvedTextLine.effective_cutter_diameter`, `effective_bold_cutter_diameter`,
`effective_per_char_cutters`, `effective_used_cutters`, `effective_max_cutter_diameter`,
`ResolvedLabel.effective_hole_cutter_diameter`), never stored, so a pre-pass that clones a line and
the reflow that rewrites its text can never desynchronise the two.

- **Text-height compensation**: `toolpath_text_height = nominal_text_height -
  effective_cutter_diameter`, so the engraved silhouette stays exactly `nominal_text_height` (a 0.25in
  label engraves 0.25in tall) while the centerline shrinks by the buffer. The buffer is universal: it
  applies to an explicit `cutter_size` and inline `<c:D>` exactly like an automatic cutter (both name a TOOL).
- **The decoration trap.** `_run_cutter` stays **physical** (pen identity + the Y-inversion
  `_stroke_layer_map` recovers `(cutter, color)` layers from), and
  `label_renderer._run_effective_cutter` supplies the **effective** width for the drawn underline /
  strikethrough geometry; the layer-recovery site subtracts the run's own buffer before snapping to a
  known cutter. Skipping that subtraction lets a large buffer snap an underline to a rung the run
  never engraves with, landing its strokes on the wrong pen/PLT file.
- **Per-stroke preview widths (`<cb:D>` visibility).** The emitted HPGL carries no width
  channel, so a pen/file-keyed width cannot show a `<cb:D>` span: the tag never changes the
  physical tool, so the span shares its pen, and a per-pen width (the pen's widest engraved
  stroke) inflates the whole pen. The previews therefore consult a per-stroke engraved-width
  map - construction, the `TextChunkRecord.stroke_widths` ride, and the `segment_width_key`/
  `arc_width_key` re-keying into the parsed device stream by
  `plate_optimizer.build_stroke_width_map` are the contract of the
  **`label_renderer._stroke_width_map` docstring**. Keys must survive emission + parsing
  (integers only) and the ±1-plotter-unit drift of the **un-optimized** assembly in the
  combined proof; `_write_simple_plots` splits each parsed pen/file into constant-width
  `PhysicalStrokeLayer`s, preferring `file_widths` over `_stem_cutter_inches` (the file-name
  token stays the *physical* tool) → §Colored Combined PDF Previews. Pinned by
  `tests/test_phase3_export.py::TestPerStrokePreviewWidths`,
  `tests/test_plate_optimizer.py::TestBuildStrokeWidthMap`, `tests/test_label_renderer_gaps.py::TestStrokeWidthMap`.
- **Known approximation: the multi-stroke boldness ladder** `multi_stroke_cutter_diameter` sizes the
  per-pass **tool** so the `N` passes total `text_height * target_boldness_factor`
  (`W = target - (N-1) * d`), snapped to the inventory as the pen/file identity; with a non-zero buffer
  each pass engraves `W + buffer`, so the engraved boldness lands `buffer` heavier than requested.
  Deliberate, and verified: compensating (`- buffer`) is a **no-op at the shop's
  `target_boldness_factor`**, where the design point sits exactly ON the gap-free floor
  `d / (1 - multistroke_overlap_ratio)` - the floor clamps `W` straight back - so it buys nothing where
  the shop engraves and only thins strokes at heavier settings.
  `multistroke_overlap_ratio` keeps guarding the floor on the physical diameters.
- **Zero buffer = byte-identical legacy output** (`effective_cutter_diameter` is the identity; code
  defaults are `0.0`, the shipped config carries the shop's measured values); inline `<cb:D>` →
  [`markup/AGENTS.md`](markup/AGENTS.md) "Inline Cutter Buffer" (per-character `cutter_buffer_text`
  overrides; lines without the tag are bit-identical to the pre-feature baseline). Pinned by
  `tests/test_phase3_export.py::TestZeroBufferRegression`, `tests/test_resolution.py`,
  `tests/test_job_config.py` (buffer fields + injection).

### Multi-Stroke (Bold) PLT Fonts (`target_boldness_factor`, `multistroke_overlap_ratio`)

Docstrings: `fonts/extract_plt_fonts.py::parse_font_file_name`,
`resolution.multi_stroke_cutter_diameter`. A PLT sheet may be engraved with **N parallel cutter passes
per glyph**; the count rides in the name: `<font>_<text height in>_<ref char>_S<n>.plt` (omitting
`_S<n>` = single-stroke). **The extractor stores the engraved passes verbatim** - the sheet is the
source of truth for the bold shape - and the renderer emits the stored geometry unchanged (arcs stay
native, envelopes/glyph groups untouched); the stroke count drives **cutter sizing only**.

- `parse_font_file_name` strips trailing `_sym`/`_symbol` and `_S<n>` suffixes in a loop
  (canonical `_S<n>_sym`) **before** the existing `rsplit("_", 2)`, so names with
  underscores and symbol reference chars keep working; `_S0` is a `FontExtractionError`.
  `PltFontData.stroke_count: int = 1` (lenient: missing / garbage / `< 1` → 1 + WARNING) and
  `plt_font_stroke_count(name)` strips a trailing ` SC` and returns **1** for TTF/unknown fonts.
- **The ladder** (matplotlib-free): the passes sit at `design_pass_spacing` (baked into the sheet at
  the design point), so the engraved width is `W + (N-1) * d` and the target inverts to
  `W = text_height * target_boldness_factor - (N-1) * d`; the `multistroke_overlap_ratio` floor raises
  `W` to `d / (1 - ratio)` so adjacent passes overlap and no unengraved stripe appears. At the shipped
  factor/ratio this equals the classic `text_height * factor / (1 + (N-1) * (1 - ratio))` exactly - the
  design point sits on the floor. A heavier factor grows the cutter and the **implicit** overlap
  `1 - d/W`; a lighter one clamps to the floor (WARNING).
- `_resolve_content` resolves the font name **before** the cutter block (the cascades are independent)
  and, with no explicit `cutter_size` and `stroke_count > 1`, runs the ladder, snaps with
  `snap_boundary_hole_cutter`, and **steps up** one rung (`next_larger_cutter`) when the snap-down lands
  below the gap-free floor (WARNING; an INFO names font, stroke count, factor, floor, cutter, implicit
  overlap). `toolpath_text_height = nominal - cutter` and the `CutterSizeError` guard are unchanged.
- **Everything downstream is free**: `cutter_diameter` is already per-line end-to-end, so the kerning
  clearance, the text-hole/underline/descender stroke floors, `build_cutter_pen_map` and the per-cutter
  PLT files all see the ladder's tool, and the floors are *exact* (the outermost pass's engraved stroke
  extends `0.5 * cutter_diameter` past the stored centerline ink). A multi-stroke line joins the
  `cutter_downsize` ladder normally; `<b>` re-runs the ladder at
  `target_boldness_factor * bold_boldness_multiplier` → [`markup/AGENTS.md`](markup/AGENTS.md) "Bold Markup".
- **Pin**: fixture `tests_deps/multistroke_job.yaml`
  (`tests/test_phase3_export.py::TestMultistrokeDemoExample`) - the S1 control, an S5 font whose
  snap-down breaks the floor (stepped up), an S2 font, a heavier factor, a lighter factor (clamped,
  WARNING), and an explicit `cutter_size` on the S5 font (ladder bypassed).

### Compression-Driven Cutter Downsizing (`cutter_downsize`)

Step loop, probes and the swap history: **`cutter_downsize.py` module docstring**. When a line's
*effective* horizontal scale (margin × collision) falls below the **midpoint** between its current
**automatic** cutter and the next smaller `tools.json` rung (0.100 current, 0.080 next: midpoint 0.90 -
0.89 triggers, 0.90 keeps), the line drops one rung and the toolpath height grows to
`text_height - smaller_cutter` (nominal `text_height` stays user intent). An explicit `cutter_size`
disables it for that line; `max_cutter_downsizes: 0` disables the mechanism.

- `apply_compression_cutter_downsize` is an **export pre-pass** in
  `vectorize.export_per_cutter_plts`, *after* `apply_global_h_compress` and *before*
  `build_cutter_pen_map`: one probe render per label through an injectable `ScaleProbe` (lazy
  `render_label_to_plt`, reading `RenderedLabel.compression_by_line`), emitting
  `ResolvedLabel` clones with the reduced cutter + grown toolpath height +
  `cutter_downsize_by_line` (line index → (original, final)), so the pen map, layers and PLT
  names see the tool actually used. **Order matters:** the shared compression runs first, so
  the midpoint trigger measures the scale the label is engraved at.
- **The run-cutter clamp:** a `<f:...>` font-only run follows its line - its cutter is
  capped at the line's own resolved tool, the tool the downsize moves
  (`resolution.scale_cutter_ladder`/`_scale_cutter_ladder_cached`, a `line_cutter` ceiling
  applied only at `scale == 1.0`, passed by `per_char_cutters` and `_font_segment_line`). A
  multi-stroke run font keeps its **gap-free floor** through the clamp, so a symbol run never reopens an unengraved stripe.
- **Sharing:** `_share_downsizes` (per label) and `_share_downsizes_job_wide` (over
  `_job_height_groups`) run a fixpoint pass after the per-line loop - each
  `nominal_text_height` group containing a trigger converges on the group's smallest final
  cutter. Receivers get the shared cutter applied directly (seed step, midpoint check
  bypassed, toolpath floor enforced), spend one unit of their own budget and continue their
  own monotone loop, so a deeper compression lowers the group minimum and the pass repeats;
  every step strictly decreases some cutter on a finite ladder, so it terminates. Propagated
  swaps land in `cutter_downsize_by_line` and log a WARNING. Lines with an explicit
  `cutter_size`, `cutter_downsize: false`, `max_cutter_downsizes: 0`, `max_h_compress: 0.0`
, or `cutter_downsize_global: false` are never touched - in either direction. Job-wide
sharing exists because on many-single-line-labels jobs per-label sharing is inert
(same-height labels split across two per-cutter layers whenever their compressions straddle
the midpoint); it runs only when a downsized line enables `cutter_downsize_global_job_wide`
(so the default stays bit-identical) and requires `cutter_downsize_global` true.
- **Scope guards (free no-ops):** no `available_cutters` inventory (the ladder is the shop's
  real tool list), no eligible line, every line's budget `0.0`. `export_per_cutter_plts`
  gained an `available_cutters` kwarg (default `None` ⇒ inert ⇒ bit-identical), threaded by
  the CLI and `scripts/run_integration_test.py`; `next_smaller_cutter`/
  `should_downsize_cutter` (with a `1e-12` tie guard) stay matplotlib-free in `resolution.py`
. Each swap logs a WARNING naming label id, line index, text, scale and both cutters.
**Accepted staleness:** the vertical-spacing solvers size against the *original* toolpath
height; the render-time `fit_line_spacing_to_margins` re-clamp preserves margins.
- **Pins:** `tests/test_cutter_downsize.py` (incl. `TestJobWideSharing`); fixtures
  `tests_deps/cutter_downsize_job.yaml` (`TestCutterDownsizeDemoExample`),
  `tests_deps/cutter_downsize_global_job.yaml` (`TestCutterDownsizeGlobalDemoExample`),
  `tests_deps/cutter_downsize_global_job_wide_job.yaml`
  (`TestCutterDownsizeGlobalJobWideDemoExample`) - in `tests/test_phase3_export.py`.

### Per-Label Shared Horizontal Compression (`h_compress_global`)

Derivation: **`h_compress.py` module docstring**. Two same-height lines in one label can engrave at
visibly different densities; `h_compress_global` (fallback false) applies a compressed line's scale to
every other *eligible* line of the same `nominal_text_height` **within the same label**.

- `apply_global_h_compress` is an **export pre-pass** *before* `apply_compression_cutter_downsize` and
  `build_cutter_pen_map`: one probe render per label through the shared `ScaleProbe`, emitting
  `ResolvedLabel` clones carrying `global_compress_by_line` (line index → shared scale).
- **The probe measures typography, never collision avoidance.** It renders with
  `collision_avoidance=False` (detection only), so `compression_by_line` carries the pure
  **margin-fit** scales: collision compression (Phase 4, the tail flush) is re-derived by
  every pass and is usually milder, so baking it into a shared typography decision
  double-charges it. The cutter-downsize probe shares the rule.
- **Group scale** (`_shared_scale`): the group's most-compressed (minimum) effective scale among its
  triggering eligible lines, clamped to each receiver's own `1 - max_h_compress` budget floor (never
  stretched back); one pass converges, unlike `_share_downsizes`. `h_compress_global_same_height_only`
  picks the scope (`_label_groups`: one group per `nominal_text_height`, true; ONE label-wide group,
  false - the flag is uniform across a label, so the first eligible member decides).
- **Eligibility:** `h_compress_global AND max_h_compress > 0.0` (a zero-budget line can
  neither trigger nor receive); a group needs ≥2 eligible lines. The renderer applies the map
  as the second-pass margin target (`margin = min(natural, shared / collision)`), so a trigger
  line stays bit-identical to the per-line behavior. No eligible line (or no trigger in a
  group of two) is a free no-op; the flag rides resolved lines, so no export/CLI threading is
  needed. Each propagated line logs a WARNING naming label id, line index, text and scale.
- **Pins:** `tests/test_h_compress.py`; fixture `tests_deps/h_compress_global_job.yaml`
  (`tests/test_phase3_export.py::TestHCompressGlobalDemoExample`: a shared label, a per-label boundary, an opted-out label).

### Word-Level Line-Content Reflow (`optimize_line_content`)

DP and derivation: **`line_content.py` module docstring**. Consecutive lines that enable reflow
(fallback false) may **redistribute whole words** so the group reaches equal rendered width (equal
*distance*, not character count) and the label needs less compression. Word order is never changed -
only the line breaks, like LaTeX paragraph filling. A disabled line breaks the group; a word may travel across the whole group.

- `apply_line_content_reflow` is an **export pre-pass** running *first* in `export_per_cutter_plts`,
  before both compression passes, so they see the reflowed text and measure the reflowed widths
  (optimizing the **pre-compression** widths is the same objective as minimizing the compression needed).
  It emits `ResolvedLabel` clones carrying the reflowed `content`; untouched labels keep object identity.
- **Groups:** maximal runs of consecutive enabled lines from `resolution.reflow_groups()` - the **single
  source of truth** shared with the resolution cap fan-out (they must agree, or a cap is fanned onto
  lines the pre-pass never reflows together). A run of one is a no-op; a line carrying `new_line`
- **DP repartition** (`_partition_words`/`_partition_cost`) solves the linear-partition
  problem exactly: one contiguous segment per group line (every line keeps ≥1 word),
  minimizing the widest segment, tie-broken by the sum of squared widths, using each line's
  own typography (kerning never crosses a word boundary, so the concatenation is exact for PLT
  fonts). The width probe lazily imports `label_renderer.measure_line_natural_width`, keeping matplotlib out of module scope.
- **Scope guards (free no-ops):** no enabled line; every enabled run is a single line; a run
  contains a blank line or has fewer words than lines (infeasible). Each moved line logs an
  INFO naming label id, line index and before/after text; the reflow is invisible to the
  layout report (the text changed, no scale/cutter finding). **Accepted staleness:** the
  resolution-time spacing solvers size against the *original* line count; `fit_line_spacing_to_margins` re-clamps at render.
- **Pins:** `tests/test_line_content.py`; fixtures `tests_deps/optimize_line_content_job.yaml`
  (`TestOptimizeLineContentDemoExample`) and `tests_deps/optimize_line_content_new_line_job.yaml`
  (`TestOptimizeLineContentNewLineDemoExample`) in `tests/test_phase3_export.py`.

#### Growth cap (`optimize_line_content_max_lines`)

The cap limits the consecutive lines a group may use at `N` (unset = no growth).
**Growth-only - it can only ever ADD lines, never remove, blank, or collapse them.**

- **M = G..N loop** (`_reflow_label`): the floor is `G`, the group's existing line count,
  so `M = G` *is* the plain reflow. For each `M` the DP repartitions onto `M` lines (`M - G`
  clones inserted **after** the group's last line, cloning its typography) and a `ScaleProbe`
  render reports the scales: the first `M` with no compressed group line is accepted (INFO
  "grew lines A-B to M of N lines"); a group still compressed at the ceiling keeps its widest
  layout and logs a WARNING. **Ceilings:** `min(N, word_count)`; `N <= G` is inert;
  `max_h_compress == 0.0` on every group line skips probes.
- **Probe plumbing:** the growth loop's keyword-only `scale_probe: Optional[ScaleProbe]`
  (lazy default `label_renderer.render_label_to_plt` - matplotlib stays out of module scope,
  preserving the Win7/3.8 guarantee) is never called by an uncapped group, and renders clones
  with `auto_descending_character_adjustment=False`, so the relief's swept-ink scan never
  runs on a probe (pinned by `tests/test_line_content.py::TestReliefFreeScaleProbeEquivalence`).
- **Validation** (`resolution._apply_reflow_line_caps`, a post-pass over the resolved lines so config
  injection + the full cascade are visible): a cap on a label with **no** enabled line raises
  `LineContentConfigError` (a cap cascading onto individually disabled lines is fine as long as the
  label has an enabled line); one group declaring **two different** caps raises; the group's single cap
  is fanned out onto every group line via `dataclasses.replace`. `JobSpec` rejects the plate-parity
  pairing (plate cap + plate `optimize_line_content: false`); a job-level cap with a job-level opt-out
- **Validation** (`resolution._apply_reflow_line_caps`, a post-pass over the resolved lines so config
  injection + the full cascade are visible): a cap on a label with **no** enabled line raises
  `LineContentConfigError` (a cap cascading onto individually disabled lines is fine while the label has
  an enabled line); one group declaring **two different** caps raises; the group's cap is fanned out onto
  every group line via `dataclasses.replace`. `JobSpec` rejects the plate-parity pairing (plate cap +
  plate `optimize_line_content: false`); a job-level cap with a job-level opt-out is valid.
- **Pins:** fixtures `tests_deps/optimize_line_content_max_lines_job.yaml`
  (`TestOptimizeLineContentMaxLinesDemoExample`: grown 1 → 4 lines, cap-exhausted, inert `N < G`, cap
  on a group's last line, zero-budget, a disabled line keeping two capped groups independent) and
  `tests_deps/optimize_line_content_max_lines_conflict_job.yaml` (conflicting caps).

### Bullet & Enumerated Lists (`itemize`/`enumerate`)

Docstrings: `list_layout.py`, `list_counter.py`, `label_renderer._apply_list_marker`. A list
nests in a label's `content` as an `itemize:`/`enumerate:` block.
**The block expands at resolution time into ordinary `ResolvedTextLine`s** - `ResolvedLabel.content`
stays a **flat line list**, so every downstream consumer (pre-passes, renderer, reports, packing)
keeps its shape. Each item line carries `marker_text`, `align_group_id` (one fresh id per block,
`-1` on a plain line), `list_item`, `indent_left` and `list_marker_gap_fraction`.

- **The marker is a separate render element** composed at render time (`_marker_line_clone`
  at x = 0 plus the text at x = `indent_left`, both on the item's
  **first-line baseline and cap height**): the item's `text`/`plain_text`/`styled_runs`
  stay pure, so reflow can never move a bullet; the relief reads the composed `"marker text"`
  (`_RenderedLine.display_text`); `_composed_stroke_cutters` keeps a marker using a different tool on its own pen.
- **Hanging indent** (`apply_list_marker_layout`, the **first** export pre-pass, before
  `reflow`): `indent_left` = the group's max marker width (lazy `measure_line_natural_width`
, cached per typography) + `list_marker_gap_fraction * toolpath_text_height`. A free no-op
when no line carries a marker; the renderer derives a per-line floor when `indent_left` is
unset, so a direct `render_label_to_plt` call never overlaps them.
- **A list block aligns as one unit** (alignment width = the widest total extent, marker
  column + text, so markers form one flush column); Phase 2a of `_render_positioned_lines`
  runs compression (A), group widths (B) and alignment (C) as **separate loops**, and a
  continuation line's budgets shrink by `indent_left`.
- **Multi-line items** grow through `optimize_line_content` (+ the cap), cascading
  item → block → label → job, on a **width-fit** criterion (the widest segment must fit
  `label.width - 2*h_margin - indent_left`) - not the ScaleProbe's "still compressed" test - so it works
  with `max_h_compress: 0` and costs **no probe renders**; an omitted cap means as many lines as
  necessary (bounded by the word count). A grown continuation line clones the donor's typography, keeps
  `align_group_id`/`indent_left` and carries **no marker**.
- **Item typography** (`resolution._merge_item_with_block`): every `TextAttributes` field the item
  leaves unset is filled from the block **before** the normal cascade, so `_resolve_content` sees
  item → block → label → job → config for **every** typographic field. Markers resolve through
  `_cascade_list_setting` (explicit-`None` precedence); counters from `list_counter.format_counter`.
- **Schema:** `ContentEntry` **subclasses** `TextLine` (making `text` optional) rather than
  a discriminated union, so `TextLine` is untouched and `substitution.py` 's
  `model_copy(update={"text":...})` keeps working; a `field_validator(mode="before")` coerces
  bare `TextLine` objects. A list block is rejected as a **replacement-text template** and
  `placeholder: true` is rejected on a list item.
- **Pins:** `tests/test_list_counter.py`, `tests/test_list_layout.py`, `tests/test_list_render.py`
, `TestListResolution` (`test_resolution.py`), `TestListItemGrowth` (`test_line_content.py`),
`TestListMarkerFindings` (`test_layout_report.py`); fixture `tests_deps/list_job.yaml`
(`tests/test_phase3_export.py::TestListDemoExample`).

### Baseline-to-Baseline Line Spacing (`use_baseline_spacing`)

Docstrings: `resolution.baseline_pitch`/`baseline_offsets`/`baseline_block_height`. Baseline
stacking (fallback true) positions every line's **baseline** at a fixed pitch =
`toolpath_text_height + line_spacing` - constant line height, not constant ink-box gap - so a
descender **hangs into the gap below its line** instead of inflating it. Cap-only lines stack
bit-identically to ink-box stacking, because **the pitch basis is `toolpath_text_height`** (the
per-font cap height), NOT `nominal_text_height`; `DEFAULT_USE_BASELINE_SPACING: bool = True` is the
code fallback (also the `ResolvedLabel` dataclass default).

- Pure math in `resolution.py` (matplotlib-free; all return `0.0`/input on degenerate
  input): `baseline_pitch` (`cap + spacing`, floored at 0), `baseline_offsets`,
  `baseline_block_height` (`max(b_i + A_i) - min(b_i - D_i)` - convex, continuous,
  non-decreasing in the spacings), `fit_baseline_spacing_to_margins` (bisection, since
  `fit_line_spacing_to_margins` 's closed form is valid only for the linear ink-box model),
  `solve_baseline_spacing_to_fill` and `solve_baseline_spacing_to_ratio` (gap = ratio ×
  top/bottom, via `auto_line_spacing_interline_to_top_bottom_ratio`).
- **Real descender extents:** `_resolve_auto_line_spacing`/`_fit_content_to_margins` size
  against **measured** `(A, D)` per line when the feature is on and an `extents_probe` is
  supplied (`LineExtentsProbe`, injectable via `resolve_job_spec(..., extents_probe=...)`;
  production probe `memoize_extents_probe(measure_line_vertical_extents)`, threaded by
  `cli/generate.py` and `scripts/run_integration_test.py`). Without a probe the historical
  nominal-height arithmetic runs unchanged, and the probe is never called when the feature is off.
- **Pins:** fixture `tests_deps/use_baseline_spacing_job.yaml`
  (`tests/test_phase3_export.py::TestUseBaselineSpacingDemoExample`: a baseline label, its ink-box twin,
  a cap-only control) + the spacing classes in `tests/test_resolution.py`/
  `tests/test_label_renderer_gaps.py`.
  `tests_deps/baseline_spacing_mixed_height_job.yaml` exists for the mixed-height pitch-basis case but
  **no test reads it**: its header describes a "baseline-to-next-capline" basis (pitch = the FOLLOWING
  line's cap height + spacing) that `baseline_pitch` does not implement (it uses the current line's).
  Frozen and unreferenced - a pending contract, not a pin.

### Automatic Descending-Character Relief (`auto_descending_character_adjustment`)

The swept-ink model, the `required` formula, the box-model critique, the `g` -into-`i` crook
example, the `GlyphBox`/`DescenderFix` types and the solver entry points (`required_gap`,
`measure_glyph_gaps`, `colliding_partners`, `propose_ramp`, `reciprocal_scale_y` +
`recip_ink`, `ramp_for_measured`, `ramp_ink`, `merge_ramps`): **`descender_adjust.py`**
; the fixpoint and phase slot: `label_renderer._apply_descender_relief`. The distance
primitives (`ink_gap`, `polyline_gap`, `InkGrid`, `bbox_gap`) live in dependency-free
`geometry.py`; the box pre-filters are exact because the box gap is a lower bound on the
ink distance inside (pinned by randomized brute-force equivalence tests).
- **Trigger: the true 2D swept-ink distance** - a circle whose diameter is the required
  clearance rolled along the descending glyph's **sub-baseline ink** - against
  `0.5 * (cutter_upper + cutter_lower) * (1 + descending_character_clearance)`, the
  engraved-stroke floor the text-hole system uses, scaled by a unitless clearance.
  **Not a bounding-box comparison** (boxes report X-disjoint pairs as infinitely far apart,
  hiding the crook pair this exists for). A descender with no close partner below is left
  exactly as rendered: never a blanket adjustment, never keyed off the following line's cap
  line. The lower line's ink is indexed once per gap per pass through `geometry.InkGrid` (cell
  = required clearance); moving partners use `geometry.ink_gap`.
- **Reciprocal, one ramp:** one `t ∈ [0, 1]` drives both lines - the descending glyph gets
  `scale_y = 1 - t*(1 - v_scale)` and `raise = t * raise_fraction * descender`, and the
  colliding partners on the **following** line are scaled **top-down** about their own lowest
  ink point (`s_l = 1 - t*(1 - descending_character_reciprocal_scale)`), so their lowest ink
  never moves and the bottom border is untouched. Ramps are one-way (capped at 1.0), spacings
  one-way (capped by the inner height), so the fixpoint converges; a middle-line glyph can carry both ramps (upper first).
- **Bisection, and the total-ramp trap:** the gap is **not linear in `t`**, so the clearing is
  bisected (`ramp_for_measured` over `ramp_ink`) against the glyph's **current** (already-relieved)
  ink, and `propose_ramp` converts it to a **total** (`min(1, current + increment)`) because
  `_apply_glyph_ramps` rebuilds from the ORIGINAL render at the glyph's total ramp - storing the bare
  increment under-applies the relief (line-to-line hides that behind spacing growth; the border has no
  growth lever, so it engraves short of the clearance). Gap tolerances are `1e-6` in (a thousandth of
  a plotter unit): the 2D distance grows sub-linearly with spacing, so the fixpoint's finite passes
  leave sub-unit noise that must never abort the job.
- **Exhausted ramp ⇒ grow the gap below the line** (`_grow_spacings`/
  `_solve_spacing_growth_factor`, bisection on the stacked block height via a `height_at`
  callback). Growth is **measured, not deficit-derived** (`_solve_gap_growth`): ink distance
  widens **sub-linearly** with the spacing, so growing by the deficit under-shoots and the
  fixpoint stalls. Growth is capped by the vertical margins; a residual shortfall after every
  allowance raises `LabelRenderError` (non-zero CLI exit) naming both lines, the shortfall and the spent allowances.
- **The bottom border is a virtual "following line"** (`_border_box`): the border rect is
  drawn at the label edge (no inset) and cut by the boundary/hole tool, so its centreline sits
  at `-height / 2` in the relief's stacked frame, and a descending glyph collides with it on
  the **same** rolling-circle test with
  `required_gap(text_cutter, label.hole_cutter_diameter, clearance)` - **no new fields**.
  `v_margin` is **not** a constraint and every line is measured, so a single-line label
  triggers. Exhaustion logs a **WARNING, never an abort** (`_log_border_shortfalls`, above
  `_BORDER_SHORTFALL_TOLERANCE` = one plotter unit, the border ramp converging asymptotically
  as relieving a glyph shrinks the block's bottom extent). Border deficits never enter `relief.residual`.
- **Phase contract:** the relief runs inside `_render_positioned_lines`,
  **between phase 2a (X: compression + alignment) and phase 2b (Y: stacking + `_LineEntry`)**
  - the slot hole-avoidance uses, per [`collision/AGENTS.md`](collision/AGENTS.md) "Threading
  Through the Pipeline". X runs first (the pair gaps need final X positions); the relief is
  Y-only, so they commute, in both stacking modes. `_shifted_boxes` lifts glyph boxes (bounds
  **and ink**) to the stacked baselines and `_apply_glyph_ramps` rebuilds from the
  **original** render at the glyph's full ramp (idempotent). The FINAL/residual check
  re-measures the trigger metric on the **final** geometry, so a relief can never land a
  raised descender on the line below without growing the spacing or aborting; relief-moved
  glyphs report `gap_after`, clamped to `_GAP_REPORT_RADIUS`.
- **Ink is clipped at the emission floor** (`_INK_EMISSION_FLOOR` = half a plotter unit =
  `0.5 / HPGL_UNITS_PER_INCH`): emitted coordinates are integer plotter units (1016 per
  inch), so ink shallower than that cannot collide in the engraved output
  (`_clip_below_baseline` clips at `-floor`, `_glyph_boxes` zeroes a `descender` at or below it).
- **Memoized leaves (exact, LRU-bounded):** `geometry.ink_grid_for(ink, cell)` (`lru_cache(16)`; a grid
  is a pure function of `(ink, cell_size)`, read-only after `__init__`) plus
- **Memoized leaves (exact, LRU-bounded):** `geometry.ink_grid_for(ink, cell)` (`lru_cache(16)`; a grid
  is a pure function of `(ink, cell_size)`, read-only after `__init__`) and
  `label_renderer._glyph_boxes`/`_shifted_boxes` (`lru_cache(64)`). Pinned by
  `tests/test_relief_memoization.py` (identity hits, key isolation, randomized equivalence vs
  cache-cleared recompute, LRU bounds, render bit-identity).
- **Reporting:** `RenderedLabel.descender_adjustments_by_line` (`{line_index: (DescenderFix,
...)}`, only lines with a relief) → `layout_report.py` findings; a reciprocal record
(`raise_amount` always `0.0`) prints `shares relief, top-down scale...`. `_LineEntry` is
**unchanged** (the relief never touches the collision-detection tuple-unpack). INFO per
glyph, WARNING per spacing growth, WARNING per unresolvable border shortfall.
**Accepted staleness:** the resolution-time spacing solvers size against the *original* descender extents.
- **Pins:** `tests/test_descender_adjust.py` + fixtures
  `tests_deps/descender_adjust_job.yaml` (`TestDescenderAdjustDemoExample`: `border` covers
  the bottom-border trigger, `reciprocal`/`reciprocal_off` pin the reciprocal half),
  `tests_deps/descender_crook_job.yaml` (`TestDescenderCrookExample`),
  `tests_deps/descender_border_job.yaml` (`TestDescenderBorderExample`: trigger, no_trigger,
  `margin_cross`, `spacing_untouched`, the opt-out twin) and
  `tests_deps/descender_reciprocal_job.yaml` (`TestDescenderReciprocalExample`) - all in `tests/test_phase3_export.py`.

### Baseline-to-Capline Band Centering (`v_center_by_content`)

Centering by **rendered ink** (the historical rule) charges the deepest descender to the block, so a
line carrying descenders rides high and its plain letters look off-centre. Docstrings:
`label_renderer._band_center_delta`, `_center_text_layer_vertically`.

- **`ResolvedLabel.center_text_by_band`** is the single gate:
  `auto_descending_character_adjustment and not v_center_by_content` - with the relief disabled nothing
  adjusts a descender, so ink centering is kept and the field is inert; `true` restores ink centering bit-identically.
- **One delta, applied in one place.** `_band_center_delta` measures the lowest-baseline-to-highest-capline
  band in the ink-centered frame (which the frame already centers on y = 0), so the band-centered frame
  is that frame shifted down by the delta; band top = `max(b_i + A_i)` when `v_center_by_content` is true
  (measured ascender) and `max(b_i + toolpath_text_height_i)` when false, band bottom always `min(b_i)`.
  Relative stacking is untouched, so relief gaps are shift-invariant - only the absolute frame moves,
  which is what the border (fixed at `-height/2`) needs.
- **`_center_text_layer_vertically` is authoritative:** it runs after export and re-centers the exported
  layer's ink bounds onto `v_margin + available_height/2`, silently undoing centering applied only to
  the render frame - so the delta is threaded to it (`band_center_offset_inches`, subtracted from the
  target) via the out-param idiom (`_render_positioned_lines` → `_render_text_lines_by_pen` →
  `_render_label_once`) and stored on **`RenderedLabel.band_center_offset_inches`**.
- **Every consumer of the centering rule must see the same delta** - three sites, all reading
  `RenderedLabel.band_center_offset_inches`: `detect_text_hole_collisions` (its `shift_y = height/2`
  frame shift), `resolve_via_margin_adjustment`/`resolve_via_compression`, and
  `plate_optimizer._label_transform` (the plate-space text transform mirrors the centering math exactly
  - a missed site desynchronizes the optimized text layer from the emitted border).
- **The margin clamp follows the same model:** `baseline_block_height` gained
  `charge_descenders: bool = True`; band-centered labels pass `False` (the block is measured as the cap
  band, descenders hanging into the bottom margin), threaded through every spacing solver, the
  renderer's clamp and the relief's `height_at` growth cap - all must agree. **Packing is unaffected:**
  the border rect dominates `RenderedLabel` bounds and a pure Y shift changes no ink extent
  (`TestBandCentering::test_band_centering_does_not_move_the_label_bounds`).
- **Pins:** fixture `tests_deps/v_center_by_content_job.yaml` (`TestVCenterByContentExample`:
  band-centered, `ink_centered`, `relief_off`, `spacing_kept` vs `spacing_clamped`) and the renderer-level `TestBandCentering`.

### Small Caps Fonts (`<base> SC`)

Appending ` SC` to any font name selects a **synthetic small-caps variant** of the base font, rendered
on the fly - never a catalog key or a `.ttf` file. Uppercase engraves verbatim; **ASCII lowercase
`a-z` only** (accented lowercase, `ß`, digits, punctuation, derived Unicode symbols pass through)
renders as the base font's **uppercase glyph uniformly downscaled** by `sc_scale`, **top-down** (the
glyph's own bottom stays exactly in place, its top descends). The nominal/toolpath `text_height` is
untouched, so the cutter never moves; the map is strictly 1:1, so the word/glyph stroke partitions
stay exact. The per-character form is the `<sc>` tag →
[`markup/AGENTS.md`](markup/AGENTS.md) "Inline Small Caps"; the two are mutually inert.

- **Registry** (`font_registry.py`): `_canonical_map` synthesizes `"<base> sc"` entries for
  every PLT key and TTF basename (`setdefault` after all bases, so a real font named
  `"Foo SC"` wins); bases whose catalog entry sets `symbol_font` (`plt_font_is_symbol()`)
  contribute **no** variant, so `"Wingdings SC"` is unknown to the validator, `--show-fonts`
  and the showcase; `font_name_choices()` lists variants and lookups strip the suffix (`base_font_name("Dino SC") -> "Dino"`).
- **PLT** (`plt_font_renderer.py`, keyword-only `lowercase_scale`): the char loop maps the
  glyph *lookup* through `_small_caps_source` and scales the cached `_GlyphGeometry` (block,
  bbox, **both envelopes**) via `_scale_geometry` **before** `kerning_offset`, so the small
  silhouette kerns exactly like it engraves. `None` disables the transform; `1.0` keeps the uppercase map and skips only the shrink.
- **TTF** (`ftext_renderer.py`, keyword-only `small_caps_scale`, `small_caps` on
  `glyph_groups_for_line`): matplotlib's `TextPath` ignores OpenType `smcp`, so the text
  maps through `_ascii_upper_map` (1:1) and the mapped string renders - keeping matplotlib's
  shaping and the font's **natural uppercase advances** - while `_apply_small_caps` slices
  contours per character (`_per_glyph_counts` probes the **mapped** glyphs' contour counts,
  keying by the original characters) and scales each slice about its own bottom-left ink
  corner; a contour-count drift logs a WARNING.
- **Dispatch** (`label_renderer._render_line_block`): the parameter is added to the renderer
  call **only** when `resolve_font(...).small_caps`, so non-SC lines keep the historical call
  shape, and `measure_line_natural_width`/`measure_line_vertical_extents` share that
  dispatch. `DEFAULT_SC_SCALE = 0.7` is the code fallback; the field never touches
  `nominal_text_height`/`toolpath_text_height`, so it cannot interact with cutter
  selection, compression budgets, spacing solvers or collision math.
- **Pins:** fixture `tests_deps/small_caps_job.yaml`
  (`tests/test_phase3_export.py::TestSmallCapsDemoExample`); `TestSmallCaps`
  (`tests/test_plt_font_renderer.py`, `tests/test_ftext_renderer.py`), `TestSmallCapsVariants`.

### Horizontal Text Alignment

Each line is positioned within the inner content area (`[margin, width - margin]`) by
`text_h_alignment` (`left`, `center`, `right`; fallback `center`; line → label → job, parity). `left`
puts the line's left-most point **precisely at the left margin**, `right` the right-most point
**precisely at the right margin**, `center` centers on the content-area center (the default). When a
line is wider than the inner area, `center` overflows symmetrically while `left`/`right` keep their
margin edge anchored and spill out the opposite side.

- `resolution.compute_horizontal_offset()` is the pure offset math (unknown values fall back
  to `center`); it takes the collision-avoidance `edge_margin` (inches on the line's anchored
  edge: `left` slides rightward, `right` leftward, inert on `center`, clamped so a fitting
  line never leaves the box) alongside the center-only `hole_aware_h_shift` →
  [`collision/AGENTS.md`](collision/AGENTS.md) "Hole-Aware Text Centering". A line in
  `collision_center_by_line` is placed as `center` (the Phase-4 switch) and
  `collision_edge_margin_by_line` carries its Phase-3 slide; both feed the Pass C offset (a
  list group takes the maximum slide, so the marker column stays flush).
- **Pass A reserves the slide's room in the compression budget:** for a non-center line,
  `compression_available_width` is reduced by the line's `collision_edge_margin_by_line`, so
  the margin-fit compression shortens a slid line to the space its slide left inside the box -
  the anchored edge stays flush, the far edge lands on the far margin. The box is a
  compression constraint, not an alignment-stopping one. Center lines (incl. switched ones)
  ignore the edge margin. List blocks align as **one unit** → §Bullet & Enumerated Lists.

### Applied Layout Reporting

Docstrings: `layout_report.py`, `generation_report.py`. Two render-time typography effects change what
is engraved without appearing in the YAML spec, and are reported **reporting-only** (nothing
downstream consumes them for geometry): **horizontal compression** (both mechanisms are render-time -
`resolve_job_spec()` resolves only the *budget*; the effective scale is collision × margin) and
**vertical line spacing** (the requested `line_spacing` is clamped by `fit_line_spacing_to_margins`).
A third finding - the cutter downsize - comes from `ResolvedLabel.cutter_downsize_by_line` → §Compression-Driven Cutter Downsizing.

- `_LineEntry` (`label_renderer.py`) is a `NamedTuple`
  `(line_index, line_text, bounds, compression_scale, line_spacing)`: the margin scale is measured by
  diffing the line's width across the `compress_line_to_width()` call (`compress_x` scales X only)
  times the collision scale; `line_spacing` is the effective gap *below* the line, `None` on the last
  renderable line (n-1 gaps for n lines). ⚠️ **Adding a field to `_LineEntry` requires updating the
  tuple-unpack in `detect_text_hole_collisions`.**
- `RenderedLabel.compression_by_line` (only lines below `1.0`) and
  `RenderedLabel.line_spacing_by_line` (**every** effective gap, keyed by the upper line's
  content index) are populated by `_render_label_once`; the requested spacing comes from
  `source_label.content[i].line_spacing` (the adjusted clone, so collision-phase clones report
  correctly). `PerCutterExport.rendered_labels` exposes the render cache (keyed by label id).
- `layout_report.py` is the **single source of truth** for the report text
  (`format_layout_report(resolved_labels, rendered_by_id, full=...)`, gated by
  `has_layout_findings(...)`); pure Python with `TYPE_CHECKING` -only pipeline imports, so it
  is unit-testable without matplotlib and safe on the CLI's lazy-import path. A *finding* is a
  compressed line, a spacing gap deviating from the request (`math.isclose`), a downsized
  cutter, a relief, a list marker / hanging indent, a line slid off its anchored margin
  (`collision_edge_margin_by_line`), a line whose alignment hole avoidance switched to
  `center` (`collision_center_by_line`), a label with cutter-compensated holes
  (`_hole_adjust_lines`), or a markup/bold effect. `collect_line_findings(rendered)` is the
  **structured** counterpart - the frozen `layout_report.LineFinding` dataclass, one optional
  field per effect kind (`compression`, `requested_spacing`/`effective_spacing`, `downsize`,
  `relief`, `bold`, `markup_scale`, `sup_sub`, `font_runs`, `cutter_runs`,
  `original_cutter`, `edge_margin` (`(anchored side, inches)`), `align_switched` +
  `alignment`) - and the text and JSON reports both render from it, so the surfaces cannot
  diverge; the text output is **byte-identical** to the pre-`LineFinding` formatter (`tests/test_layout_report.py`).
- `generation_report.py` builds the **structured job report** under
  `<out>/generation_report/` (contract: its module docstring + `build_report`):
  `ReportCapture(logging.Handler)` collects WARNING/ERROR records and the `stage <name>: <s>s`
  lines, and is a plain class, NOT a dataclass, because a dataclass with `eq=True` is
  unhashable and `logging.Handler` registers in a `weakref.WeakSet`. `timings` prefers the
  explicit `utils.stage_timing.StageTimings` collector over captured records (the pipeline
  runs at WARNING, where INFO stage lines never exist). Every finding is a **typed entry**
  (`compression`, `spacing`, `cutter_downsize`, `descender_relief`, `bold_cutter`,
  `scale_markup`, `sup_sub_shift`, `font_run`, `cutter_markup`, `original_cutter`,
  `edge_margin`, `alignment_switch`); floats round to 6 decimals; the report is written on
  success **and** failure (`job.status = "failed"`, `job.error = {type, message, stage}`) and
  a report-write error never changes the exit code.
- `scripts/run_integration_test.py` prints the full report as the
  **Phase 3.6: LAYOUT REPORT** section; the `generate` CLI prints it **only under `-v`**
  (output contract: [`../cli/AGENTS.md`](../cli/AGENTS.md)).
  **The generate pipeline logs at WARNING, not INFO:** the operational chatter (stage timers,
  per-plate optimization summaries, benchmark tables, font-catalog and substitution notes) is
  INFO and the report carries everything worth knowing, so the default run keeps it out of the
  console *and* `logs_generate/generate.log`; `-v` escalates logger, console handler and file
  handler to DEBUG. The profiling harness is unaffected (it runs at INFO and reads the
  records) → [`../utils/AGENTS.md`](../utils/AGENTS.md).

### Job Specification Patterns

**Pattern 1: Explicit Labels List** (each label must have `width` and `height`)
```yaml
job: {job_name: "Batch 01", text_height: 0.5,
      labels: [{id: "label_1", width: 3.0, height: 1.0, count: 10,
                content: [{text: "Line 1"}, {text: "Line 2"}]}]}
```

**Pattern 2: Root-Level Single Label** (auto-repeated via `count`; dimensions required)
```yaml
job: {job_name: "Simple Labels", width: 3.0, height: 1.0, text_height: 0.5, count: 20,
      content: [{text: "Single repeating label"}]}
```

**Pattern 3: Replacement Text File Template** (EngraveLab/Vision Pro style; the label expands to
`badge_0000`, `badge_0001`, ...; a relative path resolves against the job YAML directory)
```yaml
job: {job_name: "Batch 02",
      labels: [{id: "badge", replacement_text_file: data.txt, replacement_text_delimiter: ";",
                content: [{text: "PLACEHOLDER L1", text_height: 0.45}]}]}
```

**Pattern 4: Job-Level Replacement File** (no `labels` section; job-level `width`/`height`/
`text_height` are **required** - they define every synthesized label)
```yaml
job: {job_name: "Batch 03", width: 3.0, height: 1.0, text_height: 0.75,
      holes: [{location: sides}], replacement_text_file: data.txt}
```

**Pattern 5: Plate-Level Replacement File** (`full_b` accepts the unpinned labels; static `labels`
pack normally)
```yaml
job: {job_name: "Batch 04", width: 3.0, height: 1.0, text_height: 0.75,
      plates: [{id: scrap_a, width: 24.0, height: 16.0, replacement_text_file: data_a.txt},
               {id: full_b, width: 24.0, height: 16.0}],
      labels: [{id: header, content: [{text: "HEADER"}]}]}
```


### Replacement Text Files (Badges / Multiples)

Module docstring: `substitution.py`. A `LabelSpec` with `replacement_text_file` is a template: **each
data line produces one label copy** (the file's data-line count determines the count; `count` must not
be set alongside it), and `expand_job_spec(job, yaml_path)` runs between `parse_yaml()` and
`resolve_job_spec()` (wired into `cli/generate.py` and `scripts/run_integration_test.py`), turning each
line into a static `LabelSpec` with `count=1`, id suffix `_{index:04d}`, replacement fields cleared.
Static labels and root-level jobs pass through untouched; a job without replacement labels returns the
same object. Items within a line split on `replacement_text_delimiter` (code default `;`; a single
special or whitespace character, never newline/alphanumeric) and can never contain it - the EngraveLab
constraint, enforced by construction.

- **File format:** a line whose whitespace-stripped text starts with
  `replacement_text_file_comment_string` (code default `"# "`; `""` disables comment
  handling) is a comment, whitespace-only lines are blank; both produce **no** copy and are
  reported together in one WARNING per file (1-based line numbers + text). Blank lines are
  dropped **always**; a file with no data lines raises `SubstitutionError`; CRLF/CR endings
  are normalized, a trailing newline creates no phantom copy, a UTF-8 BOM is stripped, items are used verbatim.
- **Delimiter/comment cascade:** `replacement_text_delimiter` and
  `replacement_text_file_comment_string` cascade
  **label → plate (pinned) → job → shop value → code default**, applied at `JobSpec`
  validation time (`_apply_replacement_cascades`, mirroring the `material`/clearance
  cascades) - expansion runs before `resolve_job_spec`, so the resolver never sees these
  fields. Each tier's value needs a file to apply to (a label's own file; a plate's own file
  *or* a file on a label pinned to it; the job needs a file at any tier), and the config
  injects them **only** when the raw spec declares a file somewhere (`_spec_declares_replacement_file`).
- **`content` optional:** omitted, every rendered line inherits label-level attributes; present, each
  `text` value is a placeholder declaring that line's attributes and the replacement item overrides only
  `text`. Fewer items than template lines: the copy renders only as many lines as items present (extras
  dropped) and stays vertically centered. More items: extras inherit label-level attributes.
- **Pins:** `tests/test_substitution.py`, `tests/test_replacement_blank_lines.py`,
  `tests/test_schema.py`; examples `examples/job_specs/replacement_job.yaml` with
  `replacement_text_sample.txt`/`replacement_text_assets.txt`.

#### Job-Level Replacement Files

`replacement_text_file` (+ delimiter) at the **job level** *replaces* the `labels` section: `labels`
alongside it is rejected, as is job-level `count` (the file's line count determines it). The
synthesized labels carry no label-level dimensions, so job-level `width`, `height` and `text_height`
are **required** (a validation error names the missing fields); every other job-level attribute applies
as usual, and job-level `content`, when present, is the per-line attribute template exactly like
`LabelSpec.content` (it is *not* a second label source). `_expand_job_level_replacements` synthesizes
one static `LabelSpec` per file line with base id `label` (`label_0000`,...) and clears the job-level
replacement fields and the consumed `content`.

#### Plate-Level Replacement Files

A `PlateSpec` may declare `replacement_text_file` (+ delimiter): its lines synthesize labels **pinned to
that plate** via `LabelSpec.plate_id` (base id = plate id: `p1_0000`,...), packed in exclusive
single-plate passes (pinned plates first, declaration order); the declaring plate accepts no other
labels. Label dimensions/typography are NOT plate fields - they come from the job-level cascade, so
job-level `width`/`height`/`text_height` are required whenever any plate declares a file. Plate files
are mutually exclusive with a job-level file; root-level `content` is the shared attribute template
(root-level `count` is rejected); a job whose only label source is plate files is valid. Layout
failures raise `LayoutFitError`: pinned labels never spill onto other sheets, undeclared `plate_id`
references abort, and unpinned labels with no unpinned plate left abort with a dedicated message.
`LabelSpec.plate_id` may also pin a static label directly; `JobSpec` validates it against the declared
plates.

A field cascades only through the tiers its description names; the tiers are authoritative,
this list is not. **Only `None` means unset at a tier** - a zero-width explicit value (`0`,
`0.0`, `false`, `""`, `[]`) is a *value* and must survive to render, and a per-tier
replacement is a full replacement, never a merge (declaring any label-level `font` replaces
the entire job-level font string, which is how a label opts out of a job-level ` SC`
variant). So `hole_margin` 's label -> job -> default chain and every explicit-0.0 case in
the index above are one rule, not dozens. Code fallbacks live in `schema.py`/
`resolution.py`, shop values in `job-config.json`, and
**neither is copied into this file** - a config key's *presence* is a fact, its value is
not. `REQUIRED_WHEN_UNCONFIGURED` names the fields that must come from the config or the
spec -> §Job Defaults Config. The one field that never cascades is `text_color` ->
§Stroke-Color Toolpath Splitting.

### Stroke-Color Toolpath Splitting (`text_color`)

`text_color` (enum `TextColor`) is a layer tag splitting otherwise-identical text into separate
toolpaths so cutter depth can change between runs on 3-layer material (the EngraveLab/Vision Pro
stroke-color workflow). Accepted on **labels and text lines only**: it deliberately never cascades
(resolution chain line → label → `"none"`, no job tier), a job-level value is rejected by a
`JobSpec` validator, and it is absent from `PlateSpec` and `job-config.json`. Values: full names or
1-letter abbreviations, case-insensitive (`_missing_` normalization): cyan/c, magenta/m, yellow/y,
black/k, red/r, green/g, blue/b, violet/v, orange/o, pink/p, teal/t, none/n; `none` is the implicit
default of every line that omits the field and is rejected when specified explicitly (field
validator on `TextAttributes`).

- The inline `<tc:color>...</tc>` tag applies the same split **per character** (the `<f:>`
  resolver precedent, canonicalized at parse time; `<tc:none>` and unknown colors are a
  `MarkupError`; the tag wins over the field inside its span). The pen map keys on the line's
  `used_layers` (the distinct `(cutter, color)` pairs), so a colored run's strokes - and each
  `<u>`/`<s>` decoration stroke, whose runs split at color boundaries - land on their own pen
  and per-color PLT file -> [`markup/AGENTS.md`](markup/AGENTS.md) "Inline Text Color".
  `build_cutter_pen_map` sorts by `(cutter_diameter, text_color)`: smallest keeps SP1, the
  rest SP4+ (SP2/SP3 reserved); colorless jobs reproduce the historical cutter-only assignment bit-identically.
- **Pins:** `tests/test_schema.py` (field + enum), `tests/test_resolution.py` (cascade/rejection),
  `tests/test_vectorize.py` (pen map + file names).

### PLT emission, filenames & write-boundary reflow

- `export_per_cutter_plts` writes one text file per `(cutter, color)` pen; colored layers
  gain their 1-letter suffix (`40_m_txt_<job>.plt`), colorless jobs keep the plain
  cutter-first names, and the `bh` structural file is never tagged. The cutter token is the
  diameter in integer thousandths, unpadded (`0.040` -> `40`); every name is capped at 39
  characters including the extension (`core/filenames.ensure_plot_filename_length`, shared
  with optimize/watch) and reduced by `core/filenames.sanitize_filename_component` (NFKD drops
  accents, ` Cafe` -> `Cafe`; a punctuation run becomes one underscore, `wb(uv)` -> `wb_uv`).
- **Written PLT files are reflowed at the write boundary.** Every emitter
  (`plate_optimizer.emit_layer_document`, `vectorize.extract_pens_from_plt_text`/
  `assemble_plt_from_rendered_labels`, `text_geometry.Stroke.to_hpgl`,
  `label_renderer._linecollection_to_hpgl`) produces a **compact one-line** stream, and every
  regex transform on `RenderedLabel.plt_content`/`PerCutterExport.combined_by_plate` assumes
  that. The two per-cutter write sites go through `vectorize._write_plt`, which applies
  `core.hpgl_lines.format_hpgl_lines` (one command per line, a bare `PD;`/`PU;` glued to its
  arc) and writes **CRLF** - the EngraveLab/VisionPro layout (`examples/ex_text.plt`,
  `examples/ex_structural.plt`). The reflow is cosmetic and parse-identity guaranteed. Pinned
  by `tests/test_phase3_export.py::TestWrittenPltLineFormatting` (written files assert
  `splitlines()[:2] == ["IN;", "PA;"]`, never `startswith("IN;PA;")`).

### Colored Combined PDF Previews (`preview_colors`)

Module docstring: `preview_colors.py` (pure string handling, matplotlib-free, Python 3.8-safe - it sits
on the headless export path). The combined per-plate proof
`pdf/[<plate>_][<material>_]all_<job_id>.pdf` renders text ink in its real color over the material's
background color on the (light grey, configurable) proof page. Only the combined plots are colored;
the per-cutter `_txt_`/`_bh_` PDFs and the `*_default.pdf` diagnostics keep the historical black ink
on white.

- **Page background**: the config `preview_background_color` (hex `#RGB`/`#RRGGBB`, validated by a
  `JobDefaults` field validator via `normalize_preview_background_color`, code default
  `DEFAULT_PREVIEW_BACKGROUND_COLOR`) colors the combined proof's figure + axes
  (`plot_physical_strokes(background_color=...)`). Display-only: deliberately NOT in
  `_JOB_LAYER_FIELDS`, so it never injects into the (extra-forbidding) `JobSpec` and never touches PLTs.
- **Material → colors** (`parse_material_colors`): the cascading `material` name yields
  `(background, text)` from three case-insensitive forms, first match wins - delimiter
  (`red-on-white`, `red:white`, `red|white`, `red/white`; first token = background, second
  = text), full-name prefix (`white`, `black(uv)` → background only), and the leading
  color-letter run (stopping at the first unknown letter, so `bwuv`/`wb(uv)` suffixes are
  ignored: 1 letter = background, 2 = background + core/text (`wb` = white over black), 3 =
  multi-layer stock where only the background is certain). Shop convention: **b = black** (NOT
  TextColor's b = blue), `k` black, `w` white. Unset/unrecognized → `(None, None)` → `DEFAULT_MATERIAL_COLORS` (the wb proof).
- **Text color precedence** (`resolve_text_color`): the pen's stroke-color tag (the
  `text_color` field or an inline `<tc:color>` run - `export_per_cutter_plts` builds
  `pen_colors` from `layer_by_pen`) > the material core color > black, except an unresolved
  color over a DARK background renders white so the proof stays legible. **Contrast outlines**
  (`outline_color_for`): the border (SP2) and holes (SP3) render OPAQUE in ONE contrasting
  color - black over a white material background, white over every other; holes match their label's border.
- **Background fills**: the parsed SP2 closed runs become enclosed-area polygons
  (`diagnostics.stroke_geometry.closed_path_polygons` - union semantics, no cutter width) drawn as
  `PhysicalFillLayer`s beneath the ink, and the SP3 hole circles become a second fill set punched in
  the preview background color, appended after the material fill (the "see-through" look).
- **Stroke widths are the ENGRAVED widths**: `export_per_cutter_plts` passes `pen_widths`
  (pen → tool + kerf buffer) and `file_widths` (written PLT path → engraved width - the mirrored
  per-file previews read their width from the file-name token, which stays the *physical* tool)
  → §Physical vs. effective cutter diameter. Fallbacks unchanged: no `pen_widths` keeps the thin
  centerline preview; an unparseable stem token keeps the per-file thin render; no `file_widths` plots the nominal token width.
- **Pins:** `tests/test_preview_colors.py` (conventions, precedence, hex/contrast helpers),
  `tests_deps/preview_color_job.yaml` + `TestPreviewColors` (`tests/test_phase3_export.py`: bw combined
  fill/ink/outline colors, configurable page background, wb black-border contrast, per-file black-only,
  material-less wb-default proof), `TestClosedPathPolygons` (`tests/test_stroke_geometry.py`),
  `TestPlotPhysicalStrokesColors` + `TestPlotPhysicalStrokesBackground` (`tests/test_plotter.py`), `tests/test_job_config.py`.

### Job Defaults Config (`job-config.json`)

Docstrings: `job_config.py` (`JobDefaults`, `_JOB_LAYER_FIELDS`, `REQUIRED_WHEN_UNCONFIGURED`). A
shop-level JSON companion to `tools.json` (default path `job-config.json`, selected via the
`generate` CLI's `--job-config` flag), so a shop maintains one defaults file per engraver / toolset.
`parse_yaml(spec, job_config_path=...)` loads it and injects its values into the raw job mapping
**before** `JobSpec` validation, always at the top-most layer.

- **Injection set:** `_JOB_LAYER_FIELDS` (config field → job-spec key) is the single source
  of truth for which `JobDefaults` keys fill missing **job-level** spec keys; the existing
  label → job cascade then works unchanged and YAML values always win (an explicit YAML `null`
  counts as unset; `holes: []` suppression is a value). `plate_width`/`plate_height` and
  `hole_diameter` are deliberately NOT in it (plate-layer defaults, hole-entry surgery);
  `left_clearance`/`top_clearance` land at the job layer and cascade onto plates via
  `JobSpec`. This file never lists the membership and never copies a value.
- **Margin suppression:** a spec-declared `margin` (job level, **or on any label**) suppresses the
  configured `h_margin`/`v_margin` injection (`_spec_declares_margin`). The margin cascade checks
  `job.h_margin`/`job.v_margin` *before* `margin`, so an injected axis-specific default would
  silently outrank the spec's universal margin on that axis (the reported bug: a YAML universal margin
- **Margin suppression:** a spec-declared `margin` (job level, **or on any label**) suppresses the
  configured `h_margin`/`v_margin` injection (`_spec_declares_margin`), because the margin cascade
  checks `job.h_margin`/`job.v_margin` *before* `margin` - an injected axis-specific default would
  silently outrank the spec's universal margin on that axis (the reported bug). A YAML-declared
  `h_margin`/`v_margin` keeps its own value. Replacement-file fields are gated the same way (`_REPLACEMENT_SUPPRESSED_FIELDS`).
- **Optional plate specification:** a spec without `plates:` (or with an empty list) never
  synthesizes a fallback plate - `provided_plates` stays `None` into the layout engine's
  unbounded mode, which auto-allocates `default_plate_{i}` bins sized by `default_plate_size`
  and overflows onto as many sheets as needed; a non-zero `default_plate_clearance` shifts
  placements on every bin (`_default_clearance_map`). `plate_width`/`plate_height` reach the
  bins via the CLI's `default_plate_size=(plate_width, plate_height)` and never fill the
  job-level label `width`/`height`; `hole_diameter` fills the `diameter` of any hole entry that omits it.
- **Required-when-unconfigured:** `REQUIRED_WHEN_UNCONFIGURED` names the fields that must
  come from the config **or** the spec (job level, or declared on every label). A field
  missing from the JSON, set to `null`, or whose file is absent (while a config path is in
  play) makes it required: `assert_required_fields()` raises `JobConfigError` (a `ValueError`
  subclass) listing the offenders. Enforcement runs only when a config path is supplied, so
  direct API / test callers keep the historical all-optional contract.
- Malformed JSON or unknown config keys fail loudly (`JobConfigError`); `description` is an accepted
  free-form key (mirrors `tools.json`). `preview_background_color` is the one **display-only** key →
  §Colored Combined PDF Previews. Every config field is documented in the generated `JOB_CONFIG.md`,
  and `tests/test_job_config.py` derives from the live `JobDefaults` model -
  **never pin the shop's values in a test** (`tests/AGENTS.md`).

## Stage timing contract (`utils/stage_timing.py`)

- `plt_optimizer/utils/stage_timing.py` is the single source of the pipeline timing-line format:
  `stage_timer(name, logger)` emits one INFO line `stage <name>: <s>s` per completed stage (via
  `time.perf_counter`, in a `finally`, so a raising stage is still timed). Import-light (stdlib
  only) and Win7-safe, so it lives in `utils/` (the shared substrate) and is imported at module scope
  by `vectorize`/`cli/generate`, the `cli/optimize` CLI, and `core/pipeline.py`.
- Stage names are a stable contract consumed by `scripts/profile_generate.py`
  (`STAGE_LINE_PATTERN`): `parse`, `resolve` (`cli/generate.py`), and `reflow`,
  `cutter_downsize`, `h_compress`, `layout_render_pack`, `relief`, `assemble_split`,
  `optimize_layer`, `plots`, `default_plots` (`vectorize.export_per_cutter_plts`);
  `relief` fires once per job (only when a label enables relief engraving);
  `optimize_layer`/`assemble_split` fire per plate/layer (the harness sums repeats). Format
  pinned by `tests/test_stage_timers.py` - changing it must update the regex + harness together.
- Timers are log-only (zero behavior change); the module logger is `_LOG` so the
  `logger: TextLogger` parameter never shadows it. Optimize-pipeline stage names:
  [`../core/AGENTS.md`](../core/AGENTS.md) + [`../cli/AGENTS.md`](../cli/AGENTS.md);
  profiling: [`../../docs/PROFILING.md`](../../docs/PROFILING.md).
