# AGENTS.md - Inline Markup (`plt_optimizer/generate/markup/`)

Parent contracts: [`../../../AGENTS.md`](../../../AGENTS.md) (global standards)
and [`../AGENTS.md`](../AGENTS.md) (schema, cascade, typography - the old root
AGENTS.md section 6 sections that stayed with the pipeline). The sections below
moved verbatim from the old root AGENTS.md section 6 ("Inline Markup & Italic"
through "Inline Font Selection").

The framework code is `__init__.py` (this package). Tag geometry lands in
`../label_renderer.py`, `../plt_font_renderer.py` and `../ftext_renderer.py`;
resolution/cascade in `../resolution.py`; markup-aware reflow in
`../line_content.py`; decoration geometry in `../underline.py`.

### Inline Markup & Italic (`html_brackets`, `italic_angle`, `<i>`)

Text lines may carry inline markup (`<i>...</i>`) that styles part of a line.
`plt_optimizer/generate/markup/` (this package, `__init__.py`) is the **framework** (pure Python,
matplotlib-free, Python 3.8 / Win7 import-safe, no pipeline imports at all): it
parses a line's raw `text` into frozen `Run(text, style)` values sharing one
`Style` (currently `italic: bool`), and exposes `split_styled_words` /
`style_boundaries` for the reflow and `serialize_runs` / `serialize_word` for
rebuilding raw markup.

- **`html_brackets`** (cascades line → label → job, `PlateSpec` parity,
  `job-config.json` key, default `"<>";` only `None` means unset) is an
  **even-length** string whose first half is the left delimiter and second half
  the right: `"<>"` → `<` / `>`, `"<<>>"` → `<<` / `>>`, `"«»"` → `«` / `»`.
  The validator (`_validate_html_brackets_value`, shared with the plate parity
  field) requires even length, **distinct halves** (identical halves cannot tell
  open from close, so `"%%"`, `"||"`, `"//"` and `"  "` are all rejected), no
  newline, and no alphanumeric character in either half. Choosing a pair absent
  from the data keeps chevrons from being read as markup — the motivating
  feature. The forward slash is special **only** inside the delimiters
  (`</i>`), never elsewhere, so no slash-escape field exists.
- **Literal-first scanning**: the leftmost left delimiter is scanned and the
  body up to the first right delimiter is a tag only when it matches
  `[A-Za-z]+` (open; the `name:args` form is legal for a tag registered with
  `accepts_args=True` — `<scale:N>` requires the argument, `<sup>`/`<sub>`
  make it optional via `args_optional`) or
  `/[A-Za-z]+` (close) **and names a registered tag**. Anything else — a lone
  `<` in `3 < 5`, an unregistered `<xyz>` / `</xyz>`, an opener with no closer —
  is literal text. **Crossing spans are legal**: a close tag closes the most
  recent open tag with that name, which need not be the innermost one, and
  each tag covers its own open..close range, so a character's style is the
  per-character *intersection* of the spans covering it (HTML-style) —
  `<s>Always <sc>on</s> duty</sc>` engraves 'Always on' struck, 'on duty' in
  small caps, 'on' with both. The close removes its opener from the stack and
  rebuilds the active style by **replaying the remaining opens** (each tag's
  `apply` is idempotent, so the replay reproduces the pre-close style minus
  the closed tag exactly — the style an open stores is only valid until a
  crossing close removes an opener below it, so the replay is the single
  correct rule for every close). The per-character flag arrays every renderer
  consumes are that intersection, so the decoration runs, the pen split, the
  kerning floors and the reflow all follow with no crossing-specific logic;
  `serialize_runs` emits the canonical nested form, which re-parses to
  identical runs. Broken markup stays a **hard error**: a close tag whose name
  was never opened, a close with no open tag at all, or an opener never closed
  raises `MarkupError` (a `ValueError` subclass) → CLI non-zero, consistent
  with the fail-loud contract for an unknown font / missing glyph. Parsing
  runs at **resolution time** (`_resolve_content`), so the abort lands with
  the other spec-level errors.
- **The tag registry** (`TAG_REGISTRY` + `register_tag(name, apply,
  accepts_args)`) is the extension point: one registry entry plus
  render-side handling of the new `Style` field. The parser, cascade,
  serialization and reflow need no change. `apply` must be **idempotent** so
  `serialize_runs` can recover the tag stack from styles; with several tags
  active, serialization emits them in registry order (the nesting order that
  round-trips). `<u>` (see Inline Underline below) is the second shipped tag:
  `Style.underline` + `register_tag("u", ...)`, and nested `<i>...<u>...</u>
  ...</i>` composes (every transformation applies to its runs, in stack order).
  The registry order is `i, u, b, scale, sup, sub, s, sc, f, c` (each tag registers
  last so every earlier tag's serialization stays bit-identical). The font tag
  (`<f:font name>`, see Inline Font Selection below) is the one tag whose
  argument needs pipeline knowledge (the font catalog): `parse_markup` accepts
  an injected `font_resolver` (raw name -> canonical name, `ValueError` for
  unknown) that canonicalizes `Style.font` at open time and on every
  crossing-close replay, so the markup package stays free of pipeline imports (the
  Windows 7 / Python 3.8 guarantee) and an unknown font raises `MarkupError`
  (CLI non-zero).
- **Resolved fields**: `ResolvedTextLine` gains `html_brackets`,
  `italic_angle`, `plain_text` (the markup-free text) and `styled_runs`
  (`()` for a markup-free line — the renderers' fast path). Every line resolves
  to at least one run, so "no styling" is `not any(run.style.italic ...)`, not
  `not styled_runs`. `__post_init__` defaults `plain_text` to `text` for
  hand-built lines, and **`with_markup(text)` is the only supported way a
  pre-pass changes a resolved line's text** — a bare `dataclasses.replace`
  would leave `plain_text` / `styled_runs` stale (the renderers would engrave
  the old plain text).
- **Reporting and measurement always use `plain_text`**: `_LineEntry.line_text`,
  `TextChunkRecord.word_text`, the layout-report findings, the descender
  relief's `_glyph_boxes` / `_apply_glyph_ramps` character names, and the
  render-error messages. The tags never engrave and never appear in a report;
  feeding the raw text to `_glyph_boxes` would break its
  `len(chars) == len(glyph_groups)` check and silently disable the relief.
- **Italic = a shear about the baseline**, `x' = x + y * tan(italic_angle)`,
  `y' = y`: the baseline (and every point on it) stays fixed, the glyph leans
  right, and the ink height is untouched. `italic_angle` cascades line → label →
  job (plate parity, `job-config.json`, default **12**, schema bounds `[0, 45]`);
  **`0` is upright by contract** — the shear is skipped, so an italic run
  engraves exactly like plain text (the `_italic_render_kwargs` gate returns
  `{}` for a plain line, a line with no italic run, and an angle of 0, which
  keeps those lines on the historical renderer call shape, bit-identical).
- **PLT path** (`_shear_geometry`, mirroring `_scale_geometry`): the block,
  bounding box **and both profile envelopes** are sheared together and the
  result feeds `kerning_offset` **before** kerning, so the slanted silhouette
  kerns exactly and an italic overhang widens the pair's advance with no fudge
  factor. Shearing both glyphs of a pair shifts both edges by `y * tan`, so the
  pair's advance is the plain pair's (the line widens by one overhang, not two).
  Arcs **flatten** at `ARC_FLATTEN_CHORD_TOL_INCHES` (a sheared circular arc is
  a *rotated* ellipse, which `ArcSeg` cannot represent) and log at DEBUG — the
  `scaled_y` policy (italic is a routine render-time transform), not the
  `compress_x` WARNING. `TextBlock.sheared(tan, context)` /
  `Stroke.sheared` / `LineSeg.sheared` / `ArcSeg.sheared` are the primitives;
  the pen-up rapid target shears with the geometry, so a plunge stays on its
  slanted stroke.
- **TTF path** (`_apply_italic`): the whole-line render is kept and the flagged
  glyph contours are italicized in the pre-chord-removal slicing frame (the
  `_apply_small_caps` idiom, `_per_glyph_counts` probing the mapped counts under
  SC). A **native italic face** splices that glyph's contours in, translated
  onto the regular glyph's ink-left/baseline (the regular face keeps the
  advance — the standard oblique spacing); the face is discovered by
  `font_registry.find_italic_face_path`, a **sibling-file probe** of the family
  naming convention (`Foo-Italic.ttf`, `FooItalic.ttf`, `Foo-Oblique.ttf`,
  case-insensitive, in the font's own directory) surfaced as
  `FontRef.italic_path` (an SC variant carries its base's face). No schema
  surface: dropping an italic file next to the regular face is all a shop does.
  Without a face — the shipped state, since `fonts/` has no italic files — the
  glyph is sheared, matching the PLT math. A contour-count drift, an unreadable
  face, or a count-sum mismatch degrades to the shear / upright render with a
  WARNING, never a crash. SC + italic always shears the small glyph (splicing a
  full-height italic glyph would undo the shrink).
- **Markup-aware reflow** (`line_content._word_tokens`): a markup-free line
  keeps the historical `.split()` (PLAIN separators, bit-identical); a line
  with markup yields one `serialize_word` token per styled word **plus the
  style of the whitespace that followed it**
  (`markup.split_styled_words_with_separators`), so a span crossing a
  whitespace boundary decomposes into per-word tags (`<i>foo bar</i>` →
  `<i>foo</i> <i>bar</i>`) and the junction's style travels with the word it
  precedes. `_group_layout` rebuilds each line with `_join_tokens` (a
  `serialize_runs` pass over the segment's runs interleaved with
  `Run(" ", separator)`), NOT a plain `" ".join`: a span that **covers a
  space** (`<u>foo bar</u>`) styles that space, and a plain join would drop it
  — which splits a continuous underline per word, visible engraving (italic
  spaces carry no ink, so the loss was invisible there; underline spaces carry
  ink, so it is not). A PLAIN-junction segment rebuilds to the historical
  `" ".join` exactly. Probes measure lines through `line.with_markup(...)`, so
  word widths include the styling.
- **Interaction notes**: `measure_line_natural_width` /
  `measure_line_vertical_extents` route through `_render_line_block`, so the
  reflow / compression / extents pre-passes are italic-aware for free; the
  descender relief, hole collisions and band centering measure the **rendered**
  geometry, so they see slanted ink with no changes of their own. Italic is a
  pure geometry transform: it never moves `nominal_text_height` /
  `toolpath_text_height`, the cutter, the pen map, or the emitted file names.
- Fixture: `tests_deps/italic_job.yaml` (pinned by
  `tests/test_phase3_export.py::TestItalicDemoExample`) — PLT italic vs its
  plain twin (baseline/height invariant, ink wider), `italic_angle: 0` ==
  plain, per-character selection strictly between the two, a `«»` pair
  rendering like `<>`, chevrons engraved verbatim under `«»`, `italic_angle:
  45` overhanging further, and the TTF shear fallback. Unit tests:
  `tests/test_markup.py` (parser, brackets, registry, serialization),
  `TestSheared` (`tests/test_text_geometry.py`), `TestItalicShear`
  (`tests/test_plt_font_renderer.py`, `tests/test_ftext_renderer.py`),
  `TestItalicFaceProbe` (`tests/test_font_registry.py`),
  `TestItalicRenderDispatch` (`tests/test_label_renderer.py`),
  `TestMarkupAwareReflow` (`tests/test_line_content.py`),
  `TestHtmlBracketsCascade` / `TestItalicAngleCascade` / `TestMarkupResolution`
  (`tests/test_resolution.py`) and `TestHtmlBracketsField` /
  `TestItalicAngleField` (`tests/test_schema.py`).

### Inline Underline (`underline_offset_fraction`, `underline_break_clearance_fraction`, `underline_break_spaces`, `<u>`)

`<u>...</u>` is the second shipped markup tag: it draws an underline with the
line's **own text cutter** as a centerline stroke. The centerline sits
`cutter_diameter + underline_offset_fraction * cap height` below the baseline
(cap height = `toolpath_text_height` = capline − baseline; the fraction
resolves to inches through the derived
`ResolvedTextLine.underline_offset_inches`), so the engraved
underline's **top edge floats exactly `fraction * cap height`** below the engraved
baseline stroke (`0.0` = the two strokes touch, the classic underline). The
font's own underline metrics are deliberately never consulted — the PLT-style
geometry is the single contract for both families (TTF parity by construction).

- **The break test is strictly horizontal** (the model's core, and the fix
  for the shattering bug): a glyph cuts the underline only where its ink
  **crosses the underline's centerline**, never merely for standing beside it
  or dipping below it. A 2D radial (capsule) test is *wrong* here — its
  distance grows with the vertical separation too, so a cap sitting happily
  above the underline collides through the vertical component and shatters the
  underline into per-glyph segments. A half-plane test (everything below the
  underline's top edge) is *also wrong*: a `g`'s open J-hook dips its whole
  bowl below the underline, so the break swallows the glyph's full width
  instead of just the stem that crosses through. The trigger is a thin band on
  the centerline `line_y`, reaching `0.2 * underline_break_clearance_inches`
  above
  and below it (`BAND_TOLERANCE_FRACTION`) so a glyph stopping a hair short of
  the centerline still cuts. **A glyph breaks the underline where its ink
  crosses the centerline — i.e. dips more than `cutter +
  underline_offset_fraction * cap height`
  below the baseline** — so the **offset** alone decides *which* glyphs break,
  and the **clearance** alone decides the air at each break. Folding the offset
  into the trigger cancels it out of the test (a glyph breaks iff its ink dips
  below the baseline at *any* offset), so raising `underline_offset_fraction`
  could
  never stop a break — the earlier bug where caps punched gaps no matter how
  deep the underline was set.
- **`underline_offset_fraction`** (`ge=0.0`, default `None` → **0.0**; explicit
  `0.0`
  honored, only `None` means unset) cascades line → label → job (plate parity,
  `job-config.json` shop default, `JobDefaults` key, not
  `REQUIRED_WHEN_UNCONFIGURED`). The value is a **unitless factor of the
  rendered cap height** (capline − baseline): it sets the centerline depth
  `cutter_diameter + fraction * cap height` (the offset is edge-to-edge air,
  uniform for every run of the line — never word to word) and so
  the break threshold: glyphs crossing the centerline (dipping past
  `cutter + fraction * cap height` below the baseline) break the underline,
  everything above stays whole. `0.0` = the underline's top edge
  touches the baseline stroke.
- **`underline_break_clearance_fraction`** (`ge=0.0`, default `None` →
  **0.0**; explicit
  `0.0` honored, only `None` means unset) cascades line → label → job (plate
  parity, `job-config.json` shop default, `JobDefaults` key, not
  `REQUIRED_WHEN_UNCONFIGURED`). The value is a **unitless factor of the
  rendered cap height** (capline − baseline, resolved through the derived
  `ResolvedTextLine.underline_break_clearance_inches`): it is the **air at
  each break**: every cut is
  padded horizontally by `0.5 * cutter + fraction * cap height` (the half
  cutter is the
  glyph stroke's own half-width — the ink is a centerline — so a vertical
  descender engraves a one-cutter-wide stroke and the gap is `cutter +
  2 * clearance`), so the engraved air between a descending glyph's stroke and
  the underline's cut edge equals the clearance (`0.0` = the cut sits at the
  ink). The **code** fallback is `0.0`
  (`DEFAULT_UNDERLINE_BREAK_CLEARANCE_FRACTION`,
  keeping bare-API/test callers bit-identical); `job-config.json` ships the
  shop default. It is a *trigger*, never a position: it never moves
  the centerline, the block's floor, the cutter, the pen map, or the file
  names, and it cannot cause a break the offset prevents.
- **`underline_break_spaces`** (`bool`, default `None` → **false**; explicit
  `false` honored, only `None` means unset) cascades line → label → job (plate
  parity, `job-config.json` shop default **false**, `JobDefaults` key, not
  `REQUIRED_WHEN_UNCONFIGURED`). It controls whether the underline is
  **continuous across the spaces between words**. `false` (the default) draws
  one continuous underline spanning the whole underlined run, spaces included
  (only descending glyphs that cross the centerline break it) — the shipped
  behaviour. `true` ends the underline at each space, so every word of a
  multi-word run draws its own underline spanning its own ink and the
  inter-word space stays bare. Implemented in the gate
  (`label_renderer._underline_render_gate` → `_split_runs_at_spaces`): the flag
  splits each maximal run at the spaces in `plain_text`, so the per-run span
  logic is untouched — a run of spaces yields no sub-run (nothing to engrave).
  The flag rides resolved lines, so no export/CLI threading is needed.
- **Per-run spans:** each maximal underlined run draws its underline from its
  first glyph's bounding-box **left edge** to its last glyph's **right edge**
  (the run's own ink, not the whole line). Plain glyphs between two runs keep
  their air — nothing is engraved under them. Runs are `(first, last)`
  inclusive `plain_text` indices (`markup.StyledText.underline_flags` →
  `underline_runs`).
- **Minimal gap cutting** (`plt_optimizer/generate/underline.py`, pure Python,
  matplotlib-free, Win7/3.8-safe): each ink segment is clipped to the thin
  **strip around the centerline** (`segment_band_interval`); the part of a
  segment inside a horizontal strip is convex, so its X-extent is one
  interval, read off the two band-edge crossings (closed-form, no
  sampling, touching = safe). Each interval, padded by `0.5 * cutter +
  clearance`, cuts ONE gap (`underline_segments` = the gap complement over the
  span, gaps closer than `_INK_EMISSION_FLOOR` merge). A hook that crosses the
  centerline twice (a `g`) cuts one gap per crossing, so the underline shows
  the stem, not the whole glyph. Descenders punch through
  the underline and nowhere else does the line break: a glyph beside the
  underline (its X outside the run), a glyph above it (its ink short of the
  band), and a bowl that dips below the underline without crossing the
  centerline are all invisible to the cut. Caps tangent to a 0.0-offset
  underline cut nothing (tangency is safe, the repo-wide convention).
- **Render wiring** (`label_renderer`): `_underline_render_gate` (per-char
  flags → runs; `()` for plain / italic-only lines, keeping those lines
  bit-identical; `underline_break_spaces` splits each run at the spaces via
  `_split_runs_at_spaces`) → `_underline_strokes` (centerline geometry on the FINAL
  sheared/kerned/compressed block, so the underline stays horizontal under an
  italic run and follows the slanted bounding boxes) → `_apply_underline`
  (appends the strokes to the block on all four `_render_line_block` return
  paths). The strokes are appended **after** the glyph partition is validated,
  so `glyph_groups` stays the exact 1:1 character partition — the descender
  relief's character→group mapping and the intra-chunk glyph sweep never see
  the underline as glyph ink. WORD chunk mode: the underline indices extend
  the **last non-empty word** group, so chunking never drops the underline.
- **The underline is text ink**: it extends the line's rendered bounds, counts
  toward the vertical margins and the label's vertical centering (an
  underlined bottom line raises the block), and rides the line's cutter pen /
  per-cutter PLT file unchanged (a geometry transform, like italic — it never
  moves `text_height`, the cutter, the pen map, or the file names).
- **Serializer fix** (shipped with `<u>`): `serialize_runs` tracked "already
  active" against the style a sibling open had just produced, hiding nested
  tags (`<i>a<u>b</u></i>` serialized as `<i>` only). The stack state is now
  read after the closes, so nested tags round-trip.
- Fixture: `tests_deps/underline_job.yaml` (pinned by
  `tests/test_phase3_export.py::TestUnderlineDemoExample`) — PLT underline
  (`p`/`g` punch gaps, block unchanged) vs its plain twin, caps (tangent, one
  segment, block grows), `underline_offset_fraction: 0.25` (deeper centerline),
  `underline_break_clearance_fraction: 0.1` (`pay`) vs its clearance-0.0
  control,
  nested `<i>nested <u>markup</u> tail</i>` (shears all, underlines the run) vs
  its italic-only control, two runs, a partial run, the TTF pair, small caps,
  and a multi-word run (`<u>hello world</u>`) continuous vs
  `underline_break_spaces: true` (the space stays bare).
  Unit tests: `tests/test_underline.py` (runs scan, centerline-band interval,
  gap complement), `TestParseUnderline` / `TestParseNested` /
  `TestUnderlineRegistry` (`tests/test_markup.py`),
  `TestUnderlineRenderDispatch` (`tests/test_label_renderer.py`),
  `TestUnderlineOffsetCascade` / `TestUnderlineBreakClearanceCascade` /
  `TestUnderlineBreakSpacesCascade` (`tests/test_resolution.py`) and
  `TestUnderlineOffsetField` / `TestUnderlineBreakClearanceField` /
  `TestUnderlineBreakSpacesField` (`tests/test_schema.py`).

### Inline Bold (`<b>`, `bold_boldness_multiplier`)

`<b>...</b>` is the third shipped markup tag: the wrapped characters engrave
with a **thicker cutter** than the rest of the line, so one text line splits
across two per-cutter layers (the plain tool's pen and the bold tool's). The
TODO's contract in full: single-stroke PLT fonts and TTF fonts step to the
**next larger rung** of the `tools.json` `available_cutters` inventory above
the line's own cutter; a **multi-stroke (bold)** PLT font (`stroke_count > 1`)
re-runs the existing boldness ladder at
`target_boldness_factor * bold_boldness_multiplier` (the new cascading field,
default 1.5, `[1.0, 4.0]`, product clamped to `MAX_TARGET_BOLDNESS_FACTOR`
0.30 with a WARNING). No native TTF bold face is ever consulted -- the
centerline geometry is the single contract for both families (the underline
precedent). No tag args ship: the parser's reserved `name:args` form keeps
`<b:2>` literal text (`accepts_args=False`).

- **The centerline contract.** A glyph's stored geometry is the path the
  *design* cutter follows, so the line's plain run scales it to
  `nominal - cutter` and the bold run scales the same geometry to
  `nominal - bold_cutter`. The bold glyphs therefore scale **uniformly about
  their own baseline** by `s = (nominal - bold_cutter) / (nominal - cutter)
  <= 1` (`ResolvedTextLine._bold_glyph_scale()`) -- a thicker tool engraves a
  fatter stroke, so the centerlines shrink to keep the engraved silhouette
  inside the box the plain glyph occupies. The scale is uniform so `AA` arcs
  stay **native** (PLT `plt_font_renderer._scale_baseline_geometry`, the SC
  `_scale_geometry` sibling anchored on the baseline instead of the glyph
  bottom; TTF `ftext_renderer._apply_bold` slices the flagged glyph contours
  about its ink-left on the baseline). The nominal `text_height` is untouched
  (no cutter change for the vertical fit math), and the TTF pen advance is
  deliberately unchanged (the italic shaping precedent).
- **The engraved-baseline alignment raise.** Scaling about the baseline alone
  leaves the bold run's *engraved* baseline `(bold_cutter - cutter) / 2`
  **below** the plain run's (the thicker stroke hangs half a cutter lower),
  so the bold text's engraved baseline and capline sit visibly low against
  its normal neighbours. `ResolvedTextLine.baseline_raises` therefore raises
  every character by `(run_cutter - cutter) / 2` inches, riding the renderers'
  existing per-character `baseline_shifts` array (the `<sup>`/`<sub>`
  plumbing, composed in `label_renderer._baseline_shift_inches`; the PLT
  walker bakes the array into each glyph's geometry before kerning, so the
  raise is measured at its true engraved height -- a pure translate at
  placement, so the renderers need no change). For a bold run
  this single value aligns the **engraved baseline AND the engraved
  capline**: the plain silhouette spans `[-c/2, nominal - c/2]` and the bold
  run's spans `[r - d/2, r + s*(nominal - c) + d/2]`, and bottom- and
  top-alignment both reduce to `r = (d - c) / 2`. Plain and shift-only
  characters raise `0.0`, so a markup-free line (and a line whose markup
  engraves with one tool) keeps the historical call shape, bit-identical.
  The raise is **derived** from `per_char_cutters` (never stored
  positionally), so it follows the text through `with_markup`/the reflow and
  recomputes from the live cutters after a compression-driven lockstep step.
  Underline/strikethrough deliberately do **not** follow the raise (they keep
  their baseline-relative depth / cap-band height, the shipped contract).
- **Per-pair kerning floor.** `_walk_line` gained keyword-only
  `cutter_diameters` (per character, spaces included) + `glyph_scales`; each
  kerned pair's clearance becomes `0.5 * (diameter_left + diameter_right) +
  character_spacing + kerning_min_gap_fraction * cap height` -- the exact
  engraved-stroke floor of
  the two strokes, parity with the `0.5 * (a + b)` collision floors elsewhere
  (a bold neighbour widens the pair). The scaled silhouette is what kerns
  (applied before `kerning_offset`, the SC/italic contract). `None` kwargs
  keep the line-level clearance for every pair, so every legacy call site
  renders bit-identically.
- **Resolution** (`_bold_cutter_diameter`, after the plain cutter block):
  `next_larger_cutter(cutter_diameter)` for single-stroke/TTF, the boosted
  ladder + snap + step-up for multi-stroke. `bold_cutter_diameter` /
  `bold_toolpath_text_height` land on `ResolvedTextLine` (`None` = no bold
  run). The bold runs keep the **plain** cutter (field stays `None`, WARNING
  logged) when: the line sits at the top of the inventory (no larger rung);
  a multi-stroke boosted ladder resolves no rung strictly above the plain
  cutter (e.g. `bold_boldness_multiplier: 1.0`); or the line carries an
  explicit `cutter_size` (an explicit tool is a human decision, never
  overridden -- the same contract the compression-driven downsize honours).
- **The pen split happens LAST.** One unified positioned block flows through
  every render phase (X compression, alignment, the descender relief, the
  underline, Y stacking); only in `_render_text_lines_by_pen` is it partitioned
  by stroke cutter (`_split_block_by_cutters`) onto one pen per cutter
  (`pen_map[(cutter, color)]`), driven by the per-stroke cutter array
  (`_stroke_cutter_map`, stored on `_LineEntry.stroke_cutters`) walked over the
  exact `glyph_groups` partition (a glyph is all-bold or all-plain, so every
  glyph group maps wholly into one group; `plate_optimizer` filters chunk
  records by `pen`, so the groups route as separate layers for free). The
  array is empty when the whole line engraves with one tool, so a plain line
  keeps the single-pen fast path. `build_cutter_pen_map` keys every distinct
  cutter in `ResolvedTextLine.used_cutters` as its own `(cutter, color)` layer,
  so SP numbering, the `<cutter>_txt_` file names and the `(cutter, color)`
  suffix all come free. The split is vertically safe:
  `_center_text_layer_vertically` applies ONE shared Y delta computed from the
  UNION of all text pens.
- **Stroke floors read the line's widest tool.** A `<b>` line engraves part of
  its ink with the thicker tool (and a large `<scale:N>` run with a larger
  tool), so `collision_threshold` (the whole-line AABB floor) and
  `_line_cutter` (the descender relief's pair/border floors) use
  `max(used_cutters)`. Deliberately conservative: a plain descender on a
  wide-tool-carrying line asks for a touch more air than its own stroke
  strictly needs, which can never engrave a collision.
- **Underline runs split at cutter boundaries** (`_split_runs_at_cutter`): the
  underline is cut with the RUN's own tool, so a bold run's centerline sits
  `bold_cutter + underline_offset_fraction * cap height` below the baseline
  (deeper, because the
  thicker tool's stroke starts lower) and its strokes join that tool's pen. One
  underline cannot span two tools, hence the split (the general form of the
  old bold-only split; a scaled run's centerline sits one effective-height
  cutter below the baseline).
- **Lockstep with the compression-driven downsize (user decision).** Every
  monotone step `X -> next_smaller(X)` on the plain cutter also steps the bold
  cutter one rung down (`_with_cutter` takes the inventory), so the bold run
  stays exactly one rung above the line's final plain cutter (a multi-stroke
  line preserves its factor-driven rung distance). The pair can never cross:
  the plain rung is strictly below the bold rung, so `next_smaller(bold)` is at
  or above the plain rung the plain step just left; the plain toolpath-floor
  guard refuses the step for both at once, and a downward bold step only
  *grows* the bold toolpath height. `_share_downsizes` receivers step their
  bold rung in lockstep with the seed step and their own loop. Bold lines are
  fully eligible for `cutter_downsize`. Accepted staleness: the midpoint
  trigger measures the plain cutter only.
- **Reporting.** `layout_report.py` prints a per-line finding
  `Line N: '<text>' bold cutter 0.090in (line cutter 0.060in)` (a compact-mode
  finding; the report reads `plain_text`).
- **Markup-aware reflow / probes are bold-aware for free**:
  `measure_line_natural_width` / `measure_line_vertical_extents` route through
  `_render_line_block`, so the reflow/compression/extents pre-passes see the
  bold-scaled widths. Italic + bold compose: the shear applies to the glyph at
  its natural size, then the bold scale shrinks the sheared result about the
  same baseline (PLT: shear after scale on the geometry; TTF: shear then scale
  the contours).
- Fixture: `tests_deps/bold_job.yaml` (pinned by
  `tests/test_phase3_export.py::TestBoldDemoExample`) -- the single-stroke PLT
  bold vs its plain twin, a fully bold line, the multi-stroke boosted ladder
  (+ clamped-to-ceiling and multiplier-1.0 inert), the top-of-inventory and
  explicit-`cutter_size` inert cases, `<b>`+`<i>` nested vs its italic control,
  `<b>`+`<u>` vs its underline control, a partial run, the TTF pair, and the
  compression lockstep label (plain 0.060->0.045, bold 0.090->0.060) vs its
  plain-only control. Unit tests: `TestParseBold` / `TestBoldRegistry`
  (`tests/test_markup.py`), `TestBoldBoldnessMultiplierCascade` /
  `TestBoldCutterResolution` (`tests/test_resolution.py`),
  `TestBoldBoldnessMultiplierField` (`tests/test_schema.py`),
  `TestBoldLockstep` (`tests/test_cutter_downsize.py`), `TestBoldMarkup`
  (`tests/test_plt_font_renderer.py`), `TestApplyBold`
  (`tests/test_ftext_renderer.py`), `TestBoldRenderDispatch`
  (`tests/test_label_renderer.py`), `TestBoldCutterFindings`
  (`tests/test_layout_report.py`), and the `bold_boldness_multiplier`
  job-config key tests (`tests/test_job_config.py`).

### Inline Text Scaling (`<scale:N>`)

`<scale:N>...</scale>` is the fourth shipped markup tag and the **first tag
with an argument**: the wrapped characters engrave a silhouette exactly `N`
times the plain run's **and engrave with the cutter that `N x text_height` of
text would get on its own**, so a scaled run matches text authored at that
size (the reported symptom: a 0.5x run on 1in text engraved with the 1in
text's 0.125in tool read as an ink blob). The argument is required and bound
to `[0.2, 4.0]` (`MIN_SCALE_ARG` / `MAX_SCALE_ARG` in the markup package); a bad
argument (`<scale:abc>`, `<scale:>`, out-of-bounds) is broken markup --
`MarkupError` -> CLI non-zero, the fail-loud contract. A bare `<scale>` (no
argument) is **literal text** (the value is mandatory), and `<scale: 0.5>` (a
space) never matches the tag-body regex (`\S*`) so it engraves verbatim. The
name is deliberately `scale`, not `sc`: the `sc` namespace belongs to the
small-caps field / ` SC` font suffix / the inline `<sc>` small-caps tag.

- **The framework upgrade (args) is the durable part.** `TagSpec` gained
  `apply(style, arg)`, `is_active(style)` (defaults to the flag-tag rule
  `apply(style, "") == style`), and `arg(style)` (the canonical serializer,
  mandatory with `accepts_args`); `parse_markup` threads the
  `open_match.group(2)` argument into the open branch (previously captured
  and discarded), validating inside `apply` (a `MarkupError` there is
  re-raised with the tag position + text). `serialize_runs` re-emits
  `name:arg` via `spec.arg` -- **required** so the `optimize_line_content`
  reflow round-trip (`serialize_word` -> raw text -> `with_markup` re-parse)
  never drops the value (an arg-unaware serializer would emit a bare
  `<scale>`, which is literal text, silently engraving the tags). A
  set-to-value `apply` is idempotent, so the serializer's stack recovery
  survives; a nested `<scale:0.5>a<scale:1.5>b</scale>c</scale>` restores
  `0.5` for `c` for free (the close pops the pre-open `Style`), and
  serialization flattens the nesting into per-span tag pairs that re-parse to
  the identical per-character factors. `Style.scale: float = 1.0`
  (`1.0` = `UNSET_SCALE` = no tag); `StyledText.scale_values` is the
  per-character sibling of `bold_flags`.
- **The centerline contract.** A glyph's stored geometry is the design
  cutter's path, so engraving a silhouette `s` times the plain run's through
  cutter `d` scales the geometry uniformly about the baseline by
  `g = (s*nominal - d) / (nominal - cutter)` -- at `s = 1`, `d = bold_cutter`
  this is exactly `_bold_glyph_scale()`, so **bold is the plan's special
  case**. `resolution.glyph_render_plan(line)` is the single source: it
  returns the per-character `(cutter_diameters, glyph_scales)` arrays (the
  `<b>` + `<scale:N>` union) or `None` for the fast path. A uniform x=y scale
  keeps `AA` arcs native (PLT `glyph_scales` via `_scale_baseline_geometry`;
  TTF `_apply_glyph_scales`, the `_apply_bold` contour-slicing idiom
  generalized to per-index factors -- anchor = the glyph's ink-left on the
  baseline, pen advance stays with matplotlib shaping, the italic/bold
  contract). `label_renderer._bold_render_kwargs` became `_glyph_render_kwargs`
  (same three-tier empty-dict gate: no markup / no bold-and-scaled run /
  all-unity TTF factors); the TTF `_with_words` grouping gate includes a
  non-unity `glyph_scales`.
- **The engraved-baseline alignment raise (the `<b>` rule, uniform).** Every
  character rides `(run_cutter - cutter) / 2` inches
  (`ResolvedTextLine.baseline_raises`, one per-character `baseline_shifts`
  array shared with the `<sup>`/`<sub>` shift), so a downscaled run's
  *engraved* stroke floor lands on the plain run's engraved floor (a negative
  drop -- its thinner centerline box sits lower) and an upscaled run's rises
  with it. The export pin measures the shared glyph's box delta as
  `(g - 1) * plain_bottom + (d - c) / 2` (the glyph's sub-unit baseline
  overshoot scales with the geometry).
- **The per-run cutter (`per_char_cutters`, derived).** A scaled run is a
  smaller (or larger) glyph, so it takes the **height-based ladder** cutter
  for its effective height `N * nominal` -- the tool that size of text gets
  authored on its own (`scale_cutter_ladder`, an `lru_cache`d pure function of
  the line's ladder inputs; a bold+scaled run takes the bold rung at the
  effective height, so `<b>text <scale:0.5>small</scale></b>` splits onto
  three pens: plain, bold, and the small-bold rung). **The line stores the
  ladder's inputs (`scale_cutter_inventory` + `scale_cutter_tolerance`), never
  a positional per-character result array**: `per_char_cutters` derives each
  character's cutter from its own scale factor on demand, so a pre-pass that
  rewrites the line's text (`with_markup`, the line-content reflow) keeps every
  run on its own tool. A stored positional array goes stale when words move --
  the reflow handed a downscaled run the neighbouring plain text's (larger)
  cutter, engraving a *downscaled* word with a *larger* tool. An explicit line
  `cutter_size` governs the plain runs (a human decision, never overridden)
  while the scaled runs still use the ladder for their own height. The line's
  own `cutter_diameter` / `toolpath_text_height` never move (the vertical fit
  math is untouched). `ResolvedTextLine.used_cutters` (the line, bold, and
  derived per-char cutters) is the layer set `build_cutter_pen_map` keys, so
  each run's strokes land on their own tool's pen and `<cutter>_txt_` file;
  `_LineEntry.stroke_cutters` (the per-stroke array from `_stroke_cutter_map`,
  empty when one tool engraves the whole line) drives the N-way
  `_split_block_by_cutters`. A degenerate factor (`s*nominal <= d`, only
  reachable when the inventory has no rung small enough for the run) clamps to
  `MIN_SCALED_GLYPH_FACTOR` = 0.02 with a WARNING.
- **No schema field.** The value rides the tag argument (no
  `TextAttributes`/`PlateSpec`/`JobDefaults`/`job-config.json` surface --
  unlike `sc_scale`/`italic_angle`, it is never a shop default). The scale is
  a geometry transform like italic: it never moves `text_height`, the
  underline (same cutter => no run split; a *different* cutter splits the run
  at the boundary, one underline cannot span two tools), or the file names.
- **Reporting**: `layout_report.py` prints a per-line finding
  `Line N: '<text>' scaled 0.500-1.200 (run cutter 0.030-0.125in, line cutter
  0.090in)` (the factor range over the line's runs + the run cutters the
  markup caused; a uniform range/factor set prints one value; reads
  `plain_text`, compact-mode finding). The finding tuple is 10-wide (9th = the
  factor range, 10th = the sup/sub shift range).
- **Accepted staleness**: an upscaled run grows ink above the capline while
  the baseline pitch stays per-line -- the author adds `line_spacing` (the
  descender relief fixes descenders-vs-following-line only); spaces inside a
  scaled run keep the line's space advance (no ink, keeps the reflow exact,
  and they carry the plain cutter so an underline run keeps its trailing
  space); the band-centering margin clamp charges the per-font cap height, so
  an upscaled run can cross the top margin (the relief's border/below-line
  checks still see the scaled ink -- they measure the rendered geometry).
- Fixture: `tests_deps/scale_job.yaml` (pinned by
  `tests/test_phase3_export.py::TestScaleDemoExample`) -- the PLT downscale
  vs its plain twin, the upscale, `<scale:1>` == plain, a per-glyph partial
  scale, `<scale>`+`<i>`, `<scale>`+`<b>` (the three-pen split),
  `<scale>`+`<u>` (the run split at the scale boundary), and the TTF pair;
  the export test pins the four per-cutter files (0.030 / 0.045 / 0.060 /
  0.090). Unit tests: `TestScaleRegistry` / `TestScaleParse` /
  `TestScaleSerialize` / `TestScaleWords` (`tests/test_markup.py`),
  `TestScaleResolution` / `TestGlyphRenderPlan` (`tests/test_resolution.py`),
  `TestApplyGlyphScales` (`tests/test_ftext_renderer.py`),
  `TestScaleRenderDispatch` (`tests/test_label_renderer.py`),
  `TestScaleMarkupFindings` (`tests/test_layout_report.py`), and the
  scale-argument reflow round-trip (`tests/test_line_content.py`).

### Inline Superscript/Subscript (`supersubscript_downscale`, `<sup>`, `<sub>`)

`<sup>...</sup>` / `<sub>...</sub>` are the fifth and sixth shipped markup
tags: the wrapped characters engrave **downscaled** (uniform, about the
baseline -- arcs stay native) **and vertically shifted** (a pure translate, in
cap-height units). A **bare** tag derives both from the cascading
`supersubscript_downscale` `d`; the **argumented** form (`<sup:0.3>`,
`<sub:-0.2>`) sets the shift explicitly (in cap-height units: `0` = the
current baseline, `1` = the capline, `-1` = one cap height below) and applies
**no** scaling, so the field is inert on it.

- **The shift derivation (the whole design).** A bare tag **centers the
  shrunken glyph on the line it belongs to**: a bare superscript rises
  `1 - d/2` cap heights, so the downscaled glyph's **centre lands exactly on
  the capline**; a bare subscript drops `d/2` cap heights, so its **centre
  lands on the baseline** (parking a top on the capline left the glyph
  visually below the cap-height text; resting a bottom on the baseline left
  a subscript barely lowered). Raising `supersubscript_downscale` to 0.8
  therefore gives sup `t = 0.6` with the centre still on the capline -- the
  field drives the geometry and the placement with one number, and a bare
  tag GROWS the line's ink box by `d/2` cap heights (the glyph straddles the
  line it is centered on; the `plt_sup` / `plt_plain` export pins the
  growth, symmetrically spread by the centered render frame).
- **The centerline contract, shared with `<scale:N>`.** The downscale is a
  *scale factor*, so it composes multiplicatively with `<scale:N>` into one
  effective silhouette factor `s_eff = scale_values * sup_sub_scales` and
  rides the existing `glyph_scales` array: `resolution.glyph_render_plan`
  divides nothing new, and `g = (s_eff * nominal - cutter) /
  (nominal - cutter)` stays the single geometry formula (uniform, about the
  baseline, `AA` arcs native). A bare `<sup>` on plain text **is** a
  `<scale:d>` run plus a translate. The shift is the only new renderer input.
  Every character also rides the engraved-baseline alignment raise
  (`(run_cutter - cutter) / 2`, the `<b>` rule, composed into the same
  `baseline_shifts` array): a bare tag's smaller ladder cutter means a small
  *drop*, so the centering growth shrinks by half the cutter delta, and a
  shift-only run (the line's own cutter) raises `0.0` and keeps its exact
  historical placement.
- **The run cutter = the ladder for the downscaled height** (the `<scale:N>`
  contract, `per_char_cutters` reading `s_eff`): the run lands on its own
  pen, `<cutter>_txt_` file, per-pair kerning floor and underline run-split
  for free. A **shift-only** run (the argumented form, `s_eff == 1.0`) keeps
  the line's own cutter, so it never splits the pen.
- **The framework upgrade (optional args) is the durable part.**
  `TagSpec.args_optional` makes the `name:arg` argument optional: a bare
  `<sup>` IS markup (`apply` receives `""` and derives), and the canonical
  `arg(style)` returns `""` for the bare form so `serialize_runs` emits the
  bare tag -- the line-content reflow round-trip depends on it (emitting
  `<sup:>` would engrave literally). `scale`'s arg stays mandatory (a bare
  `<scale>` is literal text).
- **`Style.sup_sub` is a frozen `SupSub(direction, shift)`** (`+1` sup /
  `-1` sub). Frozen ⇒ hashable + value-equal, which `serialize_runs`' tag-
  stack recovery needs. The per-direction `is_active` makes the two tags
  mutually exclusive, so `<sup>a<sub>b</sub>c</sup>` restores `sup` for `c`
  for free (the close pops the pre-open `Style`), and `<sup>x</sub>` is a
  mismatched-close `MarkupError`.
- **The shift is a pure translate applied at placement, after the scale.**
  PLT: keyword-only `baseline_shifts` (inches, per character) on `_walk_line`
  + the three wrappers, used in `translate(cursor, shift)`. TTF:
  `_apply_baseline_shifts` (the `_apply_glyph_scales` contour-slicing idiom,
  `line + 1j * shift`), called after `_apply_glyph_scales` and before chord
  removal. `label_renderer._baseline_shift_inches` converts cap units →
  inches with `toolpath_text_height` (the per-font cap height, the same basis
  as the baseline pitch) and `_glyph_render_kwargs` gates it (empty dict for
  every markup-free line ⇒ bit-identical output).
- **Kerning measures the shifted silhouette (PLT).** The PLT walker bakes the
  baseline shift into each glyph's geometry (block + bbox + both envelopes)
  as the LAST transform (`_shift_geometry`, a pure translate) **before**
  kerning, so a pair's penetration is measured at the glyph's true engraved
  height: a raised superscript kerns against its neighbour at the capline
  (narrowing the pair — the reported over-spacing symptom), and a shift that
  separates the boxes falls to the fallback advance instead of sampling a
  phantom overlap. A vertical translate changes no envelope X, so the shift
  can narrow a pair (a raise clears the neighbour's widest bulge) or widen it
  (a glyph riding past the neighbour's top overlaps only a sliver). The TTF
  path has no envelope kerning — matplotlib shaping owns the advances, so a
  shift never changes a TTF line's width (`measure_line_natural_width` is
  shift-blind there ⇒ the TTF reflow stays exact; a PLT markup line's width
  now reflects the shifted kerning).
- **Everything downstream is free**: the pen split (`_stroke_cutter_map` reads
  `per_char_cutters`), the underline run-split, `_line_cutter` /
  `collision_threshold` (`max(used_cutters)`), the descender relief, the
  band centering, `measure_line_vertical_extents` and the plate-space chunk
  records all read the resolved/rendered geometry. A bare tag lifts the
  ascender to `(1 + d/2) * cap` (the glyph's centre rides the capline), so
  spacing/centering see it through the measured extents with no new model.
- **`supersubscript_downscale`** (`ge=0.2, le=1.0`, explicit-`None` cascade
  line → label → job, plate parity, `JobDefaults` key, default **0.5**, not
  `REQUIRED_WHEN_UNCONFIGURED`). The shift argument is bound to
  `[-1.0, 1.0]` cap units; a non-numeric or out-of-bounds argument is
  `MarkupError` → CLI non-zero (the fail-loud contract).
- **Reporting**: `layout_report.py` prints a per-line finding
  `Line N: '<text>' sup/sub shift 0.000-0.750 cap (run cutter 0.015in, line
  cutter 0.030in)` (the shift range over the line's runs + the run cutters;
  a uniform range prints one value; reads `plain_text`, compact-mode
  finding; the 10th slot of the findings tuple).
- **Accepted staleness**: a **shift-only** run has no downscale to
  compensate, so its top rises above the capline and the line's ink box
  grows by exactly the shift -- the author adds `line_spacing` (the
  `<scale:N>` upscale precedent; the relief fixes descenders-vs-following-
  line only). The band-centering margin clamp charges the per-font cap
  height, so a shifted run can cross the top margin (the relief's
  border/below-line checks still see the shifted ink -- they measure the
  rendered geometry).
- Fixture: `tests_deps/sup_sub_job.yaml` (pinned by
  `tests/test_phase3_export.py::TestSupSubDemoExample`) -- the bare sup vs
  its plain twin (the centering growth, spread symmetrically by the centered
  frame), the bare sub (the floor drops),
  `<sup:0.3>` (shift, no scale, one pen), a per-glyph selection, the
  `supersubscript_downscale: 0.8` label (shift 0.6, the 0.045 rung),
  `<scale:0.5>` inside `<sup>` (the 0.015 rung), `<sup>`+`<u>` (the run
  split), `<sup>`+`<i>`, sup+sub on one line, and the TTF pair; the export
  test pins the four per-cutter files (0.015 bh / 0.030 / 0.045 / 0.060).
  Unit tests: `TestSupSubRegistry` / `TestSupSubParse` /
  `TestSupSubSerialize` / `TestSupSubWords` (`tests/test_markup.py`),
  `TestSupersubscriptDownscaleCascade` / `TestSupSubResolution` /
  `TestGlyphRenderPlanSupSub` (`tests/test_resolution.py`),
  `TestSupersubscriptDownscaleField` (`tests/test_schema.py`),
  `TestBaselineShiftMarkup` (`tests/test_plt_font_renderer.py`),
  `TestApplyBaselineShifts` (`tests/test_ftext_renderer.py`),
  `TestSupSubRenderDispatch` (`tests/test_label_renderer.py`),
  `TestSupSubMarkupFindings` (`tests/test_layout_report.py`), the bare +
  argumented reflow round-trips (`tests/test_line_content.py`), and the
  `supersubscript_downscale` job-config key tests (`tests/test_job_config.py`).

### Inline Strikethrough (`strikethrough_offset_fraction`, `strikethrough_break_spaces`, `<s>`)

`<s>...</s>` is the seventh shipped markup tag: the wrapped characters get a
**horizontal centerline stroke through the middle of the text**, cut with the
run's own text cutter. It is the underline's geometry with the break model
deliberately **removed** — one continuous stroke per struck run, so a descender
punches *through* it instead of breaking it (the TODO's "like underline, except
no breaks in the line"). No `underline.py` geometry is reused: no gap cutting,
no clearance, no trigger band.

- **The height is a typography property, uniform per line.** The cascading
  `strikethrough_offset_fraction` (`ge=0.0`) is the stroke's height as a
  **unitless factor of the rendered cap height** (capline − baseline)
  **above**
  the baseline (`0.0` = the stroke sits on the baseline, `1.0` = on the
  capline, honored); **`None`
  (the default) centres the stroke on the cap-band midline** — half the line's
  `toolpath_text_height` (the per-font cap height, the same cap basis as the
  `<sup>`/`<sub>` shift) — the classic mid-height strike.
  `ResolvedTextLine.strikethrough_cap` is the single Y rule the renderer draws
  with (it multiplies the fraction by `toolpath_text_height` at consumption,
  so the height tracks the CURRENT cap height across a cutter downsize).
  Cascades line → label → job (plate parity, `job-config.json` shop
  default, `JobDefaults` key, not `REQUIRED_WHEN_UNCONFIGURED`). This is the
  opposite shape from the underline, whose depth is *per run* cutter-driven
  (`-(cutter + underline_offset_inches)`) — the strike's height encodes no
  cutter.
- **At mid-cap the ink box is unchanged.** The stroke spans the run's own glyph
  bounding boxes at a height the caps already reach, so a mid-cap strike leaves
  the line's rendered bounds (and `measure_line_natural_width`) **identical**
  to its plain twin — the reflow/compression/extents pre-passes see nothing.
  An explicit offset **above** the capline grows the block upward: the stroke is
  text ink, counting toward the vertical margins and the label's centering (the
  underline extends the block downward the same way). Accepted staleness, the
  `<scale:N>` upscale precedent: a high stroke can cross the top margin (the
  relief and border checks measure the rendered geometry, so they see it).
- **Idempotent flag tag ⇒ "already struck through" is free.**
  `Style.strikethrough` + `register_tag("s", replace(style, strikethrough=True))`
  registered last at the time (order `i, u, b, scale, sup, sub, s`; `sc`
  joined after it), so every earlier
  tag's serialization stays bit-identical. Applying it to an already-struck run
  is the identity, so `<s>a<s>b</s>c</s>` is **one** struck run engraving
  **one** stroke (the close pops the pre-open style, restoring the outer run).
  No tag args ship. `StyledText.strikethrough_flags` /
  `ResolvedTextLine.strikethrough_flags` + `has_strikethrough` are the
  per-character views.
- **`strikethrough_break_spaces`** (`bool`, default `None` → **false**) controls
  whether the stroke is **continuous across the spaces** between words: `false`
  strikes the whole run including the spaces, `true` ends the stroke at each
  space so every word gets its own strike. Implemented in the gate, so the
  per-run span logic is untouched. Independent of `underline_break_spaces`
  (each decoration breaks its own spaces). Cascades line → label → job (plate
  parity, `job-config.json` shop default **false**).
- **The decoration plumbing is shared with the underline** (extracted, not
  renamed — `_underline_render_gate` / `_underline_strokes` / `_apply_underline`
  stay as thin delegates, so every existing call site and test is untouched):
  `underline.py::runs_from_flags` (the maximal-run scan, `underline_runs` kept
  as an alias), `label_renderer._decoration_runs` (the run scan +
  `_split_runs_at_spaces` + `_split_runs_at_cutter` pipeline), `_run_glyph_positions`
  + `_run_span` (the `plain_text` → glyph-index walk and the bbox span), and
  `_append_decoration` (the splice). `_strikethrough_render_gate` /
  `_strikethrough_strokes` / `_apply_strikethrough` are the strike's thin
  kind-specific halves.
- **Runs split at `<b>`/`<scale:N>` boundaries** (the underline rule, reused
  verbatim): one stroke cannot be cut by two tools, so each struck run rides its
  own tool's pen, `<cutter>_txt_` file and per-pair kerning floor for free. The
  strike never introduces a tool of its own.
- **The pen split identifies a strike stroke by its SPAN, not its depth.**
  `_stroke_cutter_map` recovers an underline stroke's cutter from its centerline
  Y (`-y - underline_offset_inches`), which works precisely because the
  underline's
  depth encodes its tool. A strike stroke's Y is cutter-independent, so the
  depth heuristic would recover garbage and key a spurious pen layer; instead
  `_strikethrough_run_cutters` recomputes each run's `(x_min, x_max, cutter)`
  from the same `glyph_groups` partition of the same block (bit-for-bit the
  strokes' own spans) and the map matches the stroke by span within
  `_INK_EMISSION_FLOOR`. Strike strokes are tested for **first** (a Y match on
  the strike height), and the underline's depth rule stays the fallback.
- **Wiring** (`label_renderer`): `_render_line_block` wraps all six return
  paths as `_apply_strikethrough(line, *_apply_underline(...))`, so the strike
  strokes splice **last** (after the underline) and `glyph_groups` stays the
  exact 1:1 character partition — the descender relief's character→group map and
  the intra-chunk glyph sweep never see a decoration stroke as glyph ink. WORD
  chunk mode extends the last non-empty word group (the underline precedent).
  No `_LineEntry` field, no layout-report finding (underline parity), no
  `RenderedLabel`, pen-map, file-name, kerning, cutter-selection or reflow
  change.
- Fixture: `tests_deps/strikethrough_job.yaml` (pinned by
  `TestStrikethroughDemoExample`) — the mid-cap strike vs its plain twin (the
  ink box identical) and vs its **underline twin** on the same `Depth` glyphs
  (the underline breaks into ≥2 segments, the strike stays one), caps,
  `strikethrough_offset_fraction: 0.0` (on the baseline) vs `2.0` (above the
  capline,
  the block grows), a continuous multi-word run vs `strikethrough_break_spaces:
  true` (the space stays bare), nested `<i>nested <s>markup</s> tail</i>` vs its
  italic-only control, two runs, the idempotent `<s>a<s>b</s>c</s>`, the `<b>`
  and `<scale:0.5>` pen splits, `<u><s>AV</s></u>` (both decorations), small
  caps, and the TTF pair; the export test pins that the strike adds no cutter
  file of its own. Unit tests: `TestParseStrikethrough` /
  `TestStrikethroughRegistry` (`tests/test_markup.py`), the
  `runs_from_flags` alias pin (`tests/test_underline.py`),
  `TestStrikethroughRenderDispatch` (`tests/test_label_renderer.py`),
  `TestStrikethroughOffsetCascade` / `TestStrikethroughCapRule` /
  `TestStrikethroughBreakSpacesCascade` (`tests/test_resolution.py`),
  `TestStrikethroughOffsetField` / `TestStrikethroughBreakSpacesField`
  (`tests/test_schema.py`), the `<s>` reflow round-trip
  (`tests/test_line_content.py`), and the `strikethrough_*` job-config key
  tests (`tests/test_job_config.py`).

### Inline Small Caps (`<sc>`)

`<sc>...</sc>` is the eighth shipped markup tag: the wrapped characters get the
**small-caps transform** — ASCII lowercase `a-z` engrave as the base font's
**uppercase** glyph uniformly shrunk to `sc_scale` of the cap height, top-down
(the glyph's own bottom stays exactly in place, its top descends). It is the
exact transform the `<base> SC` **font variant** applies to the whole line
(see Small Caps Fonts above), narrowed to a **per-character selection** — the
`italic_flags` idiom applied to the existing `lowercase_scale` /
`small_caps_scale` renderer parameters. Bare flag tag, no args (`<sc:0.5>` is
literal text, the `<b:2>` rule); applying it to a run that is already in small
caps has no additional effect.

- **No new schema field.** The factor reuses the cascading `sc_scale` (line →
  label → job → `job-config.json`, default 0.7) that already drives the font
  variant, so there is no new `TextAttributes`/`PlateSpec`/`JobDefaults`/
  `job-config.json` surface, no new `DEFAULT_*` constant, and no docs-drift
  regen. `sc_scale: 1.0` keeps the uppercase map and skips only the shrink (the
  run engraves as full-height capitals), the font-level contract.
- **The map stays 1:1** (ASCII `a-z` only, the shared `_ASCII_LOWER` gate):
  accented lowercase, `ß`, digits, punctuation and the derived Unicode symbols
  engrave verbatim at full size, so the word/glyph stroke partitions stay exact
  and the glyph groups keep the ORIGINAL character.
- **It is a pure geometry transform**: `nominal_text_height` /
  `toolpath_text_height` / the cutter never move, so there is no pen split, no
  `used_cutters` entry, no kerning-floor change, no underline/strikethrough
  run-split and no layout-report finding (the underline/strikethrough parity).
  `measure_line_natural_width` / `measure_line_vertical_extents` route through
  `_render_line_block`, so the reflow/compression/extents pre-passes are
  inline-SC-aware for free; the descender relief, hole collisions and band
  centering measure the rendered geometry, so they see the shrunken ink with no
  changes of their own.
- **PLT** (`plt_font_renderer.py`): `_walk_line` gained keyword-only
  `lowercase_flags` (the companion to `lowercase_scale`, mirroring
  `italic_tan` + `italic_flags`) threaded through the three public wrappers.
  The per-character gate maps the glyph *lookup* through
  `_small_caps_source` and shrinks the cached `_GlyphGeometry` (block + bbox +
  both envelopes) via `_scale_geometry` **before** kerning, so the small
  silhouette kerns exactly; the transform order stays SC → bold/scale → shear →
  kerning → placement, and the uniform scale keeps `AA` arcs native.
  `lowercase_flags=None` is the font-level whole-line behaviour, so every
  legacy call site renders bit-identically.
- **TTF** (`ftext_renderer.py`): a companion `small_caps_flags` rides every
  existing `small_caps: bool` parameter so the contour-count probe stays
  consistent — `_ascii_upper_map(text, flags)` maps only the flagged characters
  (the render string), `_glyph_sources` yields the per-character probe sources,
  and `_per_glyph_counts` / `_apply_small_caps` / `_apply_italic` /
  `_apply_glyph_scales` / `_apply_bold` / `_apply_baseline_shifts` /
  `glyph_groups_for_line` all take the flags. The shrink stays top-down about
  each glyph's own bottom-left ink corner. Under SC a flagged glyph always
  takes the italic **shear** (splicing a full-height native italic glyph would
  undo the shrink); an unflagged full-size capital keeps the native-face splice.
  The word-grouping gate includes the per-char flags ⇒ exact contour slicing,
  never the signature matcher (which cannot match shrunk glyphs).
- **Dispatch** (`label_renderer._small_caps_render_gate`) is the **single
  decision** shared by the PLT renderer, the TTF renderer and the per-glyph
  contour-count probe — all three must agree, or the probe asks the font about
  glyphs the render never drew and the glyph groups are silently lost. It
  returns `(sc_scale, flags)` when the tag engraves, `None` (the historical
  call shape) on: no parsed markup, no small-caps run, a font that is **already**
  a small-caps variant (the line is already small caps — the TODO's "no
  additional effect", and the font-level render stays bit-identical), or a
  **symbol font** (`plt_font_is_symbol(ref.name)`, the catalog's
  `symbol_font` flag),
  which is exempt from the ` SC` variant because it has no lowercase to shrink.
  The flags are DERIVED from `styled_runs` (`ResolvedTextLine.small_caps_flags`
  / `has_small_caps`), never stored positionally — the reflow's `with_markup`
  text swap keeps every run on its own style (the `<scale:N>` staleness rule).
- Fixture: `tests_deps/inline_sc_job.yaml` (23 labels) pinned by
  `TestInlineScDemoExample` — the tagged run between its plain and capitals
  twins, the exact 0.7 ratio on a lone glyph (a mixed line's ink box is
  dominated by its full-size capitals, so the ratio pins on `o` vs `O`), the
  shrunk capital ≠ the font's own lowercase, per-character selection,
  `sc_scale: 1.0` == authored capitals, the ` SC`-font and symbol-font inertness,
  caseless characters, the idempotent `<sc>a<sc>b</sc>c</sc>`, `<sc>`+`<b>` /
  `<scale:0.5>` / `<i>`, and the TTF triple; the export test pins that the tag
  adds no cutter file of its own. Unit tests: `TestParseSmallCaps` /
  `TestSmallCapsRegistry` (`tests/test_markup.py`),
  `TestSmallCapsMarkupFlags` (`tests/test_resolution.py`, the `with_markup`
  survival pin), `TestInlineSmallCaps` (`tests/test_plt_font_renderer.py`,
  `tests/test_ftext_renderer.py`), `TestInlineSmallCapsGate` /
  `TestInlineSmallCapsDispatch` (`tests/test_label_renderer_gaps.py`), and the
  `<sc>` reflow round-trips (`tests/test_line_content.py`).

### Inline Font Selection (`<f:font name>`)

`<f:font name>...</f>` is the ninth shipped markup tag: the wrapped characters
engrave with a **different font** than the line's cascaded `font` — the same
names the `font` field accepts (a PLT catalog key, a TTF basename, or a
`<base> SC` variant; case-insensitive), so one line mixes fonts (the motivating
case: Wingdings symbols inside a Dino line). The run engraves at the line's
`text_height` and with the cutter that size of text **in that font** gets
authored on its own (the `<scale:N>` contract), so it lands on its own pen /
`<cutter>_txt_` file. **PLT and TTF families mix freely** in one line.

- **The argument regex stays `\S*`** (zero risk to the eight shipped tags), so
  `+` is the canonical space of a multi-word name (`<f:Foo+Bar>`); `_` stays
  literal (catalog names keep underscores). A bare `<f>` is literal text (the
  name is mandatory, the `<scale:N>` rule); `<f:>` is `MarkupError` → CLI
  non-zero. The `arg` serializer re-encodes spaces as `+`, so the reflow
  round-trip (`serialize_word` → raw text → `with_markup` re-parse) is exact.
- **Dependency injection is the framework upgrade**: `parse_markup` gained the
  optional `font_resolver` (`(decoded name -> canonical name)`, raising
  `ValueError` for an unknown name, wrapped into `MarkupError` with the tag
  position). The markup package imports no pipeline module, so the Win7/3.8 import
  guarantee holds; the pipeline injects `resolution._canonical_font_name` (a
  `resolve_font(name).name` wrapper) from `_resolve_content` **and**
  `ResolvedTextLine.with_markup`, so a re-parse re-resolves canonical names to
  identity. The resolver runs at **open** time and the stack stores the
  canonical name, so the crossing-close replay reproduces it without
  re-resolving.
- **`Style.font: Optional[str] = None`** (canonical name; `None` = the line's
  own font) is a set-to-value field (idempotent), so a nested `<f:B>` inside
  `<f:A>` restores `A` for the trailing text for free (the close pops the
  pre-open style), and crossing spans intersect like every other tag.
  `StyledText.font_values` / `ResolvedTextLine.font_runs` / `has_font_runs`
  are the per-character views, DERIVED from `styled_runs` (never stored
  positionally — the `<scale:N>` staleness rule, pinned by
  `test_font_run_survives_the_with_markup_text_swap`).
- **The run cutter = the ladder for the run's font + effective height.**
  `scale_cutter_ladder` gained `font` + `run_ladder`: a single-stroke run font
  takes the plain height ladder, a **multi-stroke (bold) run font re-runs the
  boldness ladder** at its own `stroke_count` (snap + gap-free step-up, exactly
  like the line-level cutter block), and the bold rung resolves against the run
  font too. `per_char_cutters` keys its ladder cache on `(scale, run_font)`, so
  a `<f:>` run at scale 1.0 needs no scale tag to move to its own tool.
  `glyph_render_plan`'s gate includes `has_font_runs`; its centerline formula
  `g = (s*nominal - d)/(nominal - cutter)` is font-agnostic, so the run's cap
  engraves at `nominal - run_cutter` (the run's engraved cap matches the plain
  run's) and the engraved-baseline raise `(d - c)/2` rides the existing
  `baseline_shifts` array. An explicit line `cutter_size` governs the **plain**
  runs only (the scaled-run precedent); the run font keeps its own ladder.
  `used_cutters` → `build_cutter_pen_map` → the N-way `_split_block_by_cutters`
  → per-cutter `<cutter>_txt_` files: all free.
- **The run cutter is CLAMPED to the line's tool (an unscaled run).** A
  `<f:>` run changes only the FONT, so it keeps the line's nominal height and
  must never engrave with a *wider* tool than the line it sits in: the
  ladder's plain rung is capped at the line's own resolved cutter
  (`scale_cutter_ladder(..., line_cutter=...)`, applied only at
  `scale == 1.0`). The clamp makes a font run **follow the line through the
  compression-driven downsize** (the downsize moves the line's tool; the run
  follows it), so one nominal height engraves one label with one tool — a
  `<f:>` run is never left on the pre-downsize tool while the text around it
  moved down. A multi-stroke run font keeps its **gap-free floor** through
  the clamp (the floor pulls the cutter back up to the smallest inventory
  rung at or above it, so a symbol run never reopens an unengraved stripe).
  A `<scale:N>`/`<sup>`/`<sub>` run has a height of its own and keeps its own
  ladder (the clamp does not apply to it). Both `per_char_cutters` and
  `label_renderer._font_segment_line` pass the line's cutter, so the
  cross-family composer and the pen map agree on the run's tool. Pinned by
  `TestRunCutterLineClamp` (`tests/test_resolution.py`).
- **All-PLT lines kern EXACTLY inside the walk.** `plt_font_renderer._walk_line`
  (and the three public wrappers) gained keyword-only `font_names`: each named
  character fetches its geometry from that font's catalog entry (`_glyph_entry`
  is already keyed on the font), and every distinct run font resolves once into
  a render frame (library name + scale + design-unit kerning window +
  inch→design floor). Every v2 library shares the design frame
  (`normalized_ref_height` 1.0, baseline y=0, ref char exactly 1000 units), so
  the envelope kerning across a font switch is the same exact math as a
  same-font pair, the per-pair stroke floor `0.5*(d_L + d_R)` widens a pair
  against a thicker run tool, and `glyph_groups` stay exact by construction.
  `label_renderer._font_names_kwargs` is the gate: `{}` (the historical call
  shape) when the line has no font run, the base font is TTF, any run font is
  TTF, or a name fails to resolve.
- **A `<f:Base+SC>` run font gets small caps per character.** The walk's
  font-level `lowercase_scale` is whole-line, so
  `label_renderer._walk_small_caps_kwargs` merges the per-character selection:
  the union of the characters whose run font resolves to a PLT small-caps
  variant and the inline `<sc>` gate's flags (the gate's flags win where both
  apply), with a per-character **symbol-font exclusion** (a symbol font has no
  lowercase to shrink — the line-level gate's exemption, applied per character
  here). Inert on a ` SC` base font (the variant owns the whole line, the
  "already small caps" contract) and on a TTF base (the composer handles it).
- **Cross-family lines compose per-font segments.** The PLT walk renders PLT
  only and the TTF path shapes a whole line with ONE face, so
  `_render_line_block` splits into `_render_line_core` (the historical dispatch,
  now with `skip_decorations`) and the composer: each maximal single-font
  segment renders through a core call on a clone built by
  `_font_segment_line` (the segment's font as its `font`, the `<f:>` tag
  stripped, the run's ladder cutter / toolpath height / bold rung applied) and
  is placed on a running cursor. The boundary advance is the **bounding-box
  rule** (previous ink's right edge + `0.5*(d_L + d_R) + character_spacing +
  kerning_min_gap_fraction`) — a TTF glyph carries no envelopes, so an exact
  silhouette
  kern across a TTF boundary is not computable; the rule is conservative
  (accepted, documented). A run of spaces is its own segment: it advances by
  the walk's space width and breaks kerning. Word groups concatenate with
  stroke offsets (a word split across fonts yields two groups — chunking
  granularity only); glyph groups concatenate **exactly**, so the descender
  relief's character map and the intra-chunk glyph sweep stay valid.
  Decorations (`<u>`/`<s>`) apply once on the composed block (the whole-line
  gates unchanged), so a run spanning two fonts stays one continuous stroke and
  `_split_runs_at_cutter` splits it at the cutter boundary for free.
- **Single-font lines are bit-identical.** No font run ⇒ no `font_names` kwarg,
  no composer call, the historical renderer call shape.
- **No schema field, no `job-config.json` key, no cascade** — the value rides
  the tag (the `<scale:N>` precedent). The schema `font` field description
  documents the tag; `docs/schema/generate_ai_docs.py` regen is required (no new
  `DEFAULT_*` constant ⇒ no `_FALLBACK_NOTES` row).
- **Reporting**: `layout_report.py` prints a per-line finding
  `Line N: '<text>' font Wingdings (run cutter 0.030in, line cutter 0.060in)`
  (the run fonts are invisible in the YAML and each run steps to its own tool;
  multiple fonts/cutters print ascending ranges; the 11th slot of the findings
  tuple; reads `plain_text`, compact-mode finding).
- Fixture: `tests_deps/font_job.yaml` (15 labels) pinned by
  `TestFontDemoExample` — the Wingdings-in-Dino headline vs its plain twin (the
  S4 ladder 0.030 run cutter, the run's cap at `nominal - 0.030`),
  case-insensitivity, `<f:Dino+SC>` (the 0.7 ratio vs the plain-caps control,
  one pen), per-character selection, nesting (the close restores the outer
  font), `<f>`+`<b>` (the boosted run ladder 0.045, three pens),
  `<f>`+`<scale:N>`, `<f>`+`<i>`, `<f>`+`<u>` (the run split at the cutter
  boundary), a TTF run in a PLT line and a PLT run in a TTF line (exact glyph
  groups), the all-TTF control, an explicit-`cutter_size` line, and the
  per-cutter file set; the unknown-font `MarkupError` abort is pinned too. Unit
  tests: `TestFontRegistry` / `TestFontParse` / `TestFontResolver` /
  `TestFontSerialize` (`tests/test_markup.py`), `TestFontRunResolution` /
  `TestRunLadderCutter` (`tests/test_resolution.py`), `TestFontNames`
  (`tests/test_plt_font_renderer.py`), `TestFontNamesGate` /
  `TestWalkSmallCapsGate` / `TestFontNamesDispatch` /
  `TestFontSegmentComposition` (`tests/test_label_renderer_gaps.py`),
  `TestFontMarkupFindings` (`tests/test_layout_report.py`), and the `<f>`
  reflow round-trips (`tests/test_line_content.py`).


### Inline Cutter Selection (`<c:D>`)

`<c:D>...</c>` is the tenth shipped markup tag: the wrapped characters engrave
with the **explicit cutter diameter `D`** (inches) the tag declares, overriding
every other cutter setting for that segment — the line's own tool, the `<b>`
bold rung, the `<scale:N>` height ladder. The motivating case: one text line
that needs a heavier tool on a word (a deep-engraved serial number) and a
lighter one on the rest, without splitting the line into two YAML entries.

- **The argument is mandatory and in INCHES** (like every other cutter value
  in the repo: `cutter_size`, the `tools.json` `available_cutters`). A bare
  `<c>` is literal text (the `<scale:N>` rule) and `<c:abc>` / `<c:0>` /
  `<c:1.5>` are `MarkupError` → CLI non-zero (the fail-loud contract). The
  syntactic range is `0 < D <= 1.0` inch (`MIN_CUTTER_ARG` / `MAX_CUTTER_ARG`
  in the markup package); the real limit is the line's own `text_height`.
- **The value snaps to the shop inventory** with the same rule the line-level
  `cutter_size` uses (`snap_boundary_hole_cutter`: an exact match is kept,
  otherwise the next size **down** is preferred, and only when no smaller tool
  exists the next size **up**), with a WARNING naming the line — a tool the
  shop does not own cannot be engraved. The snap happens ONCE at resolution
  time (`resolution._validate_cutter_markup`), and the per-character
  derivation re-derives the same value from the same cached helper
  (`resolution.snapped_cutter`), so the validation and the renderers can never
  disagree.
- **No material ⇒ the job aborts.** A tool at or above the line's nominal
  `text_height` leaves nothing to engrave, so resolution raises
  `CutterSizeError` (a `ValueError` → CLI non-zero) — exactly the contract an
  out-of-range line-level `cutter_size` obeys. The check runs on the
  **snapped** diameter (the tool that would actually engrave).
- **The centerline contract (the `<b>` rule, generalized).** A glyph's stored
  geometry is the *design* cutter's path, so the run scales it uniformly about
  its own baseline by `g = (nominal - D) / (nominal - line_cutter)` — the
  `<b>` formula at `s = 1`, `d = D`. The run's **engraved silhouette is
  therefore exactly `nominal - D` tall**, the same invariant the plain and
  bold runs uphold: a lighter tool engraves a finer stroke (the run is
  slightly TALLER), a heavier tool a fatter one (shorter). The nominal
  `text_height` and the line's own `cutter_diameter` / `toolpath_text_height`
  **never move** — a segment override, not a line-level change.
- **The engraved-baseline alignment raise** rides along unchanged: a run
  engraving with tool `D` rises by `(D - line_cutter) / 2` inches (the
  `<b>` rule, derived from `per_char_cutters`), so its **engraved baseline and
  capline** match the plain run's.
- **Precedence: the explicit tool wins.** `per_char_cutters` takes the
  override **before** the `<b>` rung and the `<scale:N>` ladder branches, so
  `<c:D>` beats both. The bold rung is not even looked up for an overridden
  character (`scaled_bold` excludes them), so a bold `<c:D>` run never logs a
  meaningless "no larger rung" warning.
- **Everything downstream is free.** `used_cutters` (the line, bold, and
  derived per-char cutters) → `build_cutter_pen_map` → the N-way
  `_split_block_by_cutters` → the per-cutter `<cutter>_txt_` files: the run's
  strokes land on their own tool's pen and file. `_stroke_cutter_map` (the
  per-stroke array) reads `per_char_cutters`, so the split is exact. The
  underline / strikethrough runs split at the override boundary (one
  underline cannot span two tools — `_split_runs_at_cutter`). The descender
  relief's and the collision detection's stroke floors use
  `max(used_cutters)` (conservative, never a collision).
- **The compression-driven downsize moves the PLAIN characters only.** An
  explicit tool is a human decision, never overridden automatically (the
  line-level `cutter_size` contract), so the downsize steps the line's
  `cutter_diameter` while the `<c:D>` run keeps its pinned tool — the line
  then engraves across **two** tools (its reduced plain tool + the pinned
  one). The pin survives the clone because the override is **derived from the
  style** (`cutter_overrides`, never a stored positional array — the
  `<scale:N>` staleness rule), so a text-rewriting pre-pass (`with_markup`,
  the line-content reflow) keeps every run on its own tool.
- **The TTF caveat.** The TTF renderer draws outlines; a glyph's *stroke
  width* is the pen's, not the glyph's. A `<c:D>` TTF run's visible weight
  comes from the **pen assignment** (the per-cutter file split), while its
  geometry factor still applies (the run's silhouette is `nominal - D` tall).
  The PLT path carries the weight in the geometry itself, so the two families
  differ here by construction (the same caveat as the `<b>` tag).
- **No schema field, no `job-config.json` key, no cascade** — the value rides
  the tag (the `<scale:N>` precedent). No new `DEFAULT_*` constant ⇒ no
  `docs/schema/generate_ai_docs.py` `_FALLBACK_NOTES` row.
- **Reporting**: `layout_report.py` prints a per-line finding
  `Line N: '<text>' cutter 0.030in (engraved cutter 0.030in, line cutter
  0.060in)` — the requested diameter and the tool that actually runs (they
  differ when the value snapped), so a shop can see the swap the markup
  caused, which the YAML spec never states (the 12th slot of the findings
  tuple; reads `plain_text`, compact-mode finding).
- Fixture: `tests_deps/cutter_job.yaml` (9 labels) pinned by
  `TestCutterDemoExample` — the `<c:0.03>` run vs its plain twin (the run's
  engraved box at `nominal - D`), a heavier explicit tool, the unowned 0.05
  diameter (snaps down to 0.045), `<c:D>` vs `<b>` and vs `<scale:N>` (the
  explicit tool wins), a per-glyph selection, a nested override (the close
  restores the outer value), the TTF pair, and the per-cutter file set
  (0.030 / 0.045 / 0.060 / 0.090). Unit tests: `TestCutterRegistry` /
  `TestCutterParse` / `TestCutterSerialize` / `TestCutterWords`
  (`tests/test_markup.py`), `TestCutterMarkupResolution`
  (`tests/test_resolution.py`), `TestInlineCutterDispatch`
  (`tests/test_label_renderer_gaps.py`), `TestCutterMarkupFindings`
  (`tests/test_layout_report.py`), and `TestCutterMarkupPin`
  (`tests/test_cutter_downsize.py`).
