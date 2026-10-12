# AGENTS.md - System Instructions for AI Coding Assistants

## Role & Core Philosophy
You are an expert Principal Software Engineer acting as an autonomous agent in this repository. Your primary goal is to build `PLT-Optimizer`, a deterministic, cross-platform Python tool for optimizing geometric toolpaths. 

Prioritize reliability, mathematical precision, and strictly typed code over speed of delivery. Do not guess or hallucinate logic—if an implementation detail regarding HPGL/PLT parsing or Traveling Salesperson algorithms is ambiguous, stop and ask the user for clarification.

## 1. Coding Style & Standards
We adhere strictly to the **Ruff / Black** formatting standards and modern Python paradigms.
* **Strict Typing:** Every function, class, and method must have complete PEP 484 type hints. Run type checks via `mypy` before finalizing code.
* **Pre-commit hook:** Ensure that all code passes the configured pre-commit hooks before committing. This includes linting, formatting, and type checks.
* **Docstrings:** Use Google-style docstrings for all modules, classes, and public functions.
* **Immutability & Data Structures:** Prefer `dataclasses` (with `frozen=True` where appropriate) or `pydantic` models for internal state representation. 
* **Mathematical Precision:** Never use `==` for floating-point coordinate comparisons. Always use `math.isclose()` or `numpy.isclose()` with explicit tolerances.

## 2. Testing & Coverage
Testing is not an afterthought; it is a primary deliverable. 
* **Test-Driven Operations:** Every time a new function or logical block is written and confirmed working, you must write the corresponding unit test immediately.
* **Full Coverage Requirement:** Maintain 100% test coverage for all core parsing, writing, and optimization logic. Use `pytest` and `pytest-cov`.
* **Identity Testing:** Any changes to the `parser.py` or `writer.py` must pass the identity validation suite (ensuring `input.plt -> parse -> write -> output.plt` results in semantic equivalence).
* **Fixture Location (`tests_deps/`):** Any file read by a unit test must live in `tests_deps/` (adjacent to `tests/`), never in `examples/` or its subdirectories. `tests_deps/` is frozen: never move, rename, or edit its contents unless you are deliberately updating the fixtures together with the tests that pin them. `examples/` remains user-facing sample material that is safe to tweak.
* **Execution:** Run the test suite autonomously after modifying the codebase. Do not commit failing code.

## 3. Git Workflow & Commits
* **Conventional Commits:** All commit messages must strictly follow the Conventional Commits specification (e.g., `feat:`, `fix:`, `refactor:`, `test:`, `chore:`).
* **Commit Frequency:** Commit frequently to establish a granular history.
* **Working State Only:** You must only commit code that has passed all static type checks and unit tests. Never commit code with syntax errors or broken tests. 
* **NEVER Rewrite History:** Do not run `git filter-repo`, `git filter-branch`, `git rebase` on pushed branches, `git commit --amend` on pushed commits, or any other command that rewrites commit hashes. Rewriting makes local and remote histories unrelated (zero merge-base), breaks every downstream clone/PR, and invalidates GPG signatures. History rewrites require explicit human approval.
* **NEVER Touch Remotes:** Do not run `git remote remove/set-url` or delete remote-tracking refs. Note that `git filter-repo` removes all remotes by design — a second reason it is banned. Restoring a lost remote or force-pushing over a branch is a human decision, not an agent default.
* **No Destructive Operations Without Approval:** `git push --force`/`--force-with-lease`, `git reset --hard` on shared branches, branch/tag deletion, and garbage collection (`git gc --prune=now`) must never be executed autonomously.
* **Secrets Incident Protocol:** If a secret was accidentally committed, do NOT attempt to scrub history. Report the situation to the user and recommend rotating the credential; removal of committed secrets is a coordinated human decision (it requires a force-push that rewrites all history).

## 4. Project-Specific Invariants
* **Package Management:** Use **`uv`** exclusively. Do not use standard `pip`, `poetry`, or `conda`. Update `pyproject.toml` directly for dependency management.
* **Cross-Platform Compatibility:** The tool is developed on Linux but deployed on Windows. You must use `pathlib.Path` for all file system operations. Never use hardcoded strings with forward or backward slashes. Account for Windows `\r\n` line endings in file I/O where it impacts parsing.
* **Dual Logging Topology:** Any new operational logic must hook into the established logging structure:
  1. Standard text logging (`logging` module) utilizing `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`.
  2. CSV Metrics logging for tracking optimization deltas (distance before/after).
* **Silent Execution:** Unless logging an error or running in verbose mode, the standard path optimization loop should execute cleanly without cluttering standard output, as it will run as a headless hot-watch service.
* **HPGL Device Scale (1016 units/inch, NOT 1000):** The plotter unit is metric by definition — HPGL's default step size is 0.025 mm, so `25.4 / 0.025 = 1016` units per inch (40 units/mm). The generate pipeline emits `IN;PA;` with **no** scale command (`IP`/`SC`/`SI`), so the plotter runs at that default. Every inch↔coordinate conversion on the emission/read-back path must go through `plt_optimizer/core/units.py` (`HPGL_UNITS_PER_INCH`, `inches_to_units()`, `units_to_inches()`); never hardcode 1000 there. The pipeline previously emitted 1000 units/inch and engraved every label 1.6% undersized (a 16in label came out 15.75in) — pinned by `tests/test_phase3_export.py::TestDeviceScaleRegression` and `tests/test_units.py` (which proves the reference sheet `examples/test_rect_grid13sheet0.txt` is engraved in exact multiples of 1016). Three distinct scales exist and only the first is the device scale: (1) device plotter units = 1016; (2) the font-library design frame = 1000 per *design* inch (`LIBRARY_UNITS_PER_INCH` in `Fonts/extract_plt_fonts.py`, `_DESIGN_UNITS_PER_INCH` in `plt_font_renderer.py` — a self-normalizing frame, do not "fix" it); (3) Z clearance = 1000 per inch (`Z_UNITS_PER_INCH`, proven by `examples/plt headers/z clearance *.plt`: 0.2in → `ZU200`).

## 5. Python 3.8 (Windows 7) compatibility requirement for watch directory function
* **Watch Directory Function:** Ensure that the directory watching mechanism works correctly on Python 3.8 running on Windows 7. Avoid using features introduced in later Python versions. Test the function thoroughly on the target environment to confirm compatibility.
* **Plotting and development:** Plotting and benchmarking is not necessary to run on Windows 7, so this constraint only applies to the watch directory function and core operational logic. Development tools that require newer Python versions can be used for plotting and benchmarking on other environments.

## 6. YAML Job Specification & Label Generation Schema

### Overview
The `plt_optimizer/generate/schema.py` module defines the complete data contract for label generation jobs via YAML specifications. All models use **Pydantic** for validation and inherit from typed base classes supporting cascading attributes.

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
  `Fonts/plt_fonts.json` key (rendered **arc-native** through
  `plt_font_renderer.py`: PU/PD/AA glyphs keep their native `AA` arcs
  end-to-end — only X-compression may flatten them, with a WARNING) or a
  `Fonts/**.ttf` basename (extension stripped, rendered through the
  matplotlib `ftext_renderer` path). Case-insensitive, canonicalized at
  validation (`font_registry.resolve_font`); unknown names are rejected.
  Appending ` SC` to any name selects that font's synthetic **small-caps**
  variant (rendered on the fly from the base, never stored in the library —
  see Small Caps Fonts below).
  TTF lines scale by a fixed per-font reference — the ink height of the
  reference glyph `H` — so the cap height equals `text_height` on *every*
  text line (glyph size is a per-font constant, independent of a line's own
  ink box; descenders hang below the baseline, matching the PLT contract).
  Scaling each line by its own rendered ink box (the historical behaviour)
  shrank every glyph on a line containing descenders/ascenders/tall
  punctuation, so identical `text_height` values engraved at visibly
  different sizes across lines. A TTF lacking a glyph raises
  `FtextRenderError` → `LabelRenderError` (parity with the PLT
  `PltFontRenderError`); `scripts/font_showcase.py` opts out of that probe
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
  `cutter_diameter + character_spacing + kerning_min_gap` (no fixed height
  fudge). The scaled penetration is the windowed silhouette penetration ×
  `kerning_penetration_scale` when non-negative, × `kerning_recession_scale`
  when negative (a recessed pair); the advance floor is
  `min(left glyph width, min_glyph_width)`, so a tight pair can never be
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
  ASCII bases through `Fonts/glyph_transforms.py` (affine transforms with
  arc-sweep rules; bounding box and envelopes re-sampled from the
  transformed geometry), so they kern and render like engraved characters.
  List every valid name with
  `python docs/schema/generate_schema_docs.py --show-fonts`.
- `character_spacing`: Extra spacing between characters
- `line_spacing`: Extra spacing between text lines
- `space_width_fraction`: Space advance as a fraction of the rendered text
  height for PLT fonts (`ge=0.0`, default `None` → **0.3**; only `None`
  means unset, an explicit `0.0` collapses the space to bare
  `character_spacing`). Cascades line → label → job (accepted on plates for
  schema parity only); `job-config.json` supplies the shop default.
- `min_glyph_width`: Global minimum glyph advance width in inches for PLT
  fonts (`ge=0.0`, default `None` → **0.0** = pure envelope kerning). The
  envelope kerning floors each pair's advance at this value, **capped by
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
  `min(left_width, min_glyph_width)` keeps the result non-negative, so a
  recessed pair's origin never moves left of the left glyph's width.
  Cascades line → label → job (accepted on plates for schema parity only);
  `job-config.json` supplies the shop default.
- `kerning_min_gap`: Extra air in inches added to every kerned pair
  advance for PLT fonts, on top of the `cutter_diameter +
  character_spacing` clearance (`ge=0.0`, default `None` → **0.0** = no
  extra gap). Cascades line → label → job (accepted on plates for schema
  parity only); `job-config.json` supplies the shop default.
- `fallback_advance_fraction`: Multiplier on the bounding-box-width
  fallback advance used for PLT glyph pairs without overlapping height
  (or lacking envelopes) (`ge=0.0`, default `None` → **1.0** = the left
  glyph's own width). Cascades line → label → job (accepted on plates for
  schema parity only); `job-config.json` supplies the shop default.
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
  schema parity only); `job-config.json` supplies the shop default. See
  Small Caps Fonts below.
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
  `IDEAL_CUTTER_MAP` + inventory snapping — the current behaviour). When set,
  the requested diameter is snapped to `tools.json` `available_cutters`
  (`snap_boundary_hole_cutter`: next size down, else next size up; a WARNING
  names the label id + line index when it moves) and the rendered toolpath
  height is recomputed as `text_height - cutter_size`. The nominal
  `text_height` is kept as the user's intent, so vertical fit math
  (`_resolve_auto_line_spacing`, `_fit_content_to_margins`) is unaffected.
  A cutter at or above the line's `text_height` (toolpath height ≤ 0) raises
  `CutterSizeError` (a `ValueError` subclass) → CLI non-zero exit. Cascades
  line → label → job (accepted on plates for schema parity only); there is
  deliberately **no** `job-config.json` counterpart (adding it to
  `JobDefaults` would make it a legal shop default — do not). Each distinct
  cutter, explicit or auto, becomes its own per-cutter text layer/PLT file
  (`cutter_diameter` is already per-line end-to-end, so `build_cutter_pen_map`,
  `label_renderer._render_text_lines_by_pen` and `export_per_cutter_plts` need
  no change). See also the sibling `cutter_downsize` (render-time downsize
  driven by horizontal compression — implemented, see below).
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
  Sharing is always per-label: setting the option at the job level simply
  enables it for every label (plate = parity-only, like every render-affecting
  field — plate identity does not exist when the export pre-pass runs).
  Cascades line → label → job → default (plate parity only);
  `job-config.json` supplies the shop default.
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
- `optimize_line_content`: Boolean gate for **word-level line-content reflow**
  (`default None` → **false**). When true, the text of the enabled lines may be
  redistributed across the consecutive lines that also enable it so the group's
  lines reach equal rendered width (distance, not character count) — whole
  words move up/down, word order is never changed, only the line breaks (the
  LaTeX paragraph-fill behaviour). Consecutive enabled lines form one
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
  (lazy `render_label_to_plt` → `compression_by_line`). Cascades line → label →
  job → unset (plate parity only); `job-config.json` supplies the shop default
  (`null` = unset).

**LabelAttributes** (extends TextAttributes, cascades to LabelSpec only):
- `width`: Label width in inches; must be defined at label or job level (required; no longer auto-sized).
- `height`: Label height in inches; must be defined at label or job level (required; no longer auto-sized).
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
| `JobSpec` | Root job container | Requires exactly one label source: `labels` list, root-level `content`, a job-level `replacement_text_file`, or plate-level replacement files (mutually exclusive); `width`/`height` must be defined at job or label level (no longer auto-sized) |
| `LabelSpec` | Individual label definition | `count >= 1`; requires `content` (min 1 TextLine) OR `replacement_text_file` (mutually exclusive with `count`); `width`/`height` must be defined at label or job level; optional `plate_id` pins the label to a declared plate |
| `TextLine` | Text content unit | Requires non-empty `text` string |
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
  `vectorize.export_per_cutter_plts`, *before* `build_cutter_pen_map`: it
  renders each label once through an injectable `ScaleProbe` (default:
  lazy-imported `render_label_to_plt`, reading
  `RenderedLabel.compression_by_line`), and emits adjusted `ResolvedLabel`
  clones (`dataclasses.replace`, mirroring the collision-avoidance pattern)
  carrying the reduced cutter + grown toolpath height +
  `ResolvedLabel.cutter_downsize_by_line` (line index → (original, final)).
  The pen map, per-cutter layers and PLT file names therefore all see the
  tool actually used (the downsized line lands in its own file).
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
  *after* `apply_compression_cutter_downsize` and *before*
  `build_cutter_pen_map`: it renders each label once through the shared
  `ScaleProbe` (lazy-imported `render_label_to_plt`, reading
  `RenderedLabel.compression_by_line`) and emits `ResolvedLabel` clones
  (`dataclasses.replace`) carrying `global_compress_by_line` (line index →
  shared scale).
- **Group scale:** the group's *most-compressed* (minimum) effective scale
  among its triggering eligible lines, clamped to each receiver's own
  `1 - max_h_compress` budget floor (`_shared_scale`). A line whose natural
  compression is already tighter keeps it (never stretched back). A single
  pass converges — the group minimum is fixed by the triggering lines' natural
  scales, so applying it to a fitting sibling never deepens any line's natural
  compression (no fixpoint loop, unlike `_share_downsizes`).
- **Eligibility:** `h_compress_global AND max_h_compress > 0.0` (no compression
  mechanism can fire on a zero-budget line, so it can neither trigger nor
  receive). A group needs ≥2 eligible lines to share; a lone eligible line's
  natural compression is its own business. The renderer applies the map as the
  second-pass margin target (`margin = min(natural, shared / collision)`), so
  the effective scale is `min(natural, shared)` and a trigger line's geometry
  stays bit-identical to the per-line behaviour.
- **Scope guards (free no-ops):** no label has an eligible line; no triggering
  line in a same-height group with two eligible lines. The flag rides resolved
  lines, so no export/CLI threading is needed (unlike `available_cutters`).
- Accepted staleness: this pre-pass runs *after* the cutter reduction, so a
  shared compression can deepen a line's squeeze without re-triggering a cutter
  swap. The two pre-passes are deliberately **not** looped to a joint fixpoint.
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
- **Groups:** maximal runs of consecutive enabled lines (`_reflow_groups`);
  a disabled line breaks a run, a run of one line is a no-op, and a word may
  travel across the whole group (not just one line).
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
  capped group always starts from the historical behaviour and only grows
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
  ink spilling into the margin is accepted behaviour, only the border stroke
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
  Y scaled about the baseline; arcs flatten at the chord tolerance (DEBUG log —
  vertical scaling and native arcs are mutually exclusive). Stroke count is
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
  `ink_gap`, the `InkGrid` spatial hash, and the `Ink`/`Polyline`/`Point`
  aliases); `aabb_vertical_gap` stays for the box-model fallback. The border
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
  `_detect_text_hole_collisions` (its `shift_y = height/2` frame shift),
  `_resolve_collision_via_margin_adjustment` / `_resolve_collision_via_compression`
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
`plt_fonts.json` key or a `.ttf` file. Uppercase glyphs engrave verbatim at
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
  real font literally named `"Foo SC"` wins its name). `FontRef` carries
  `small_caps: bool` and the **base** font's `path`. `font_name_choices()`
  lists the variants (4 bases → 8 names today), so the schema validator,
  `--show-fonts` docs and the font showcase all see them. Library lookups
  must strip the suffix — `base_font_name("Dino SC") -> "Dino"` — which
  `plt_font_renderer._resolve_plt_font` and `scripts/kerning_report.py` do
  (a `"Dino SC"` key does not exist in the library).
- **The scale must be uniform (x = y)**: `TextBlock.scaled(f)` keeps `ArcSeg`
  sweeps native (an ellipse is not a circular arc), so PLT small caps stay
  **arc-native end-to-end**; `scaled_y`/`compress_x` flatten arcs and are
  never used here. The transform is a uniform scale composed with a pure
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

### Inline Markup & Italic (`html_brackets`, `italic_angle`, `<i>`)

Text lines may carry inline markup (`<i>...</i>`) that styles part of a line.
`plt_optimizer/generate/markup.py` is the **framework** (pure Python,
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
  `[A-Za-z]+` (open; the reserved `name:args` form is recognized but only legal
  for a tag registered with `accepts_args=True` — none ship yet) or
  `/[A-Za-z]+` (close) **and names a registered tag**. Anything else — a lone
  `<` in `3 < 5`, an unregistered `<xyz>` / `</xyz>`, an opener with no closer —
  is literal text. Broken markup is a **hard error**: a close tag that is not
  the innermost opener, or an opener never closed, raises `MarkupError` (a
  `ValueError` subclass) → CLI non-zero, consistent with the fail-loud contract
  for an unknown font / missing glyph. Parsing runs at **resolution time**
  (`_resolve_content`), so the abort lands with the other spec-level errors.
- **The tag registry** (`TAG_REGISTRY` + `register_tag(name, apply,
  accepts_args)`) is the extension point for the remaining TODO tags
  (`<b>`, `<u>`, `<f:name>`, `<sup:0.3>`, `<sc>`): one registry entry plus
  render-side handling of the new `Style` field. The parser, cascade,
  serialization and reflow need no change. `apply` must be **idempotent** so
  `serialize_runs` can recover the tag stack from styles; with several tags
  active, serialization emits them in registry order (the nesting order that
  round-trips).
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
  Without a face — the shipped state, since `Fonts/` has no italic files — the
  glyph is sheared, matching the PLT math. A contour-count drift, an unreadable
  face, or a count-sum mismatch degrades to the shear / upright render with a
  WARNING, never a crash. SC + italic always shears the small glyph (splicing a
  full-height italic glyph would undo the shrink).
- **Markup-aware reflow** (`line_content._word_tokens`): a markup-free line
  keeps the historical `.split()`; a line with markup yields one
  `serialize_word` token per styled word, so a span crossing a whitespace
  boundary decomposes into per-word tags (`<i>foo bar</i>` → `<i>foo</i>
  <i>bar</i>`) — identical engraving for the shipped per-character styles, and
  every word stays a fully styled atomic token the DP can move. Probes measure
  lines through `line.with_markup(...)`, so word widths include the styling.
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

### Horizontal Text Alignment

Each text line is positioned horizontally within the label's inner content
area (`[margin, width - margin]`) according to `text_h_alignment`. Values
cascade line → label → job (default `center`; accepted on plates for schema
parity only). Enum `TextHAlignment` (`left`, `center`, `right`).

- `left`: the line's left-most point sits **precisely at the left margin**.
- `right`: the line's right-most point sits **precisely at the right margin**.
- `center`: the line is centered within the inner content area (existing
  behaviour; backward compatible default).
- `compute_horizontal_offset()` (resolution.py): pure offset math returning
  the target left-edge X for a rendered line; unknown values fall back to
  `center`.
- Applied per-line by the render path (`_render_positioned_lines`) after
  compression, before vertical stacking.
- When a line is wider than the inner area (e.g. compression disabled),
  `center` overflows symmetrically while `left`/`right` keep their aligned
  margin edge anchored and spill out the opposite side.

### Text-Hole Collision Avoidance

Rendered text lines are checked against drill holes via the circle-vs-AABB
closest-point gap in `geometry.circle_aabb_gap()`. The check is
**stroke-aware**: a (line, hole) pair collides when the geometric gap falls
below the per-line threshold

`threshold = 0.5 * (hole_cutter + text_cutter) + hole_text_collision_distance`

where `text_cutter` is the line's resolved cutter diameter and
`hole_cutter` is the boundary/hole cutter from `tools.json`
(`boundary_hole_cutter_size`, default 0.015, snapped to
`available_cutters`: next size down, else next size up). The first term is
the stroke floor at which the two cut strokes just touch, so near misses
that would make engraved strokes bleed together are collisions too; a gap
exactly equal to the threshold is safe. Detection runs in the label-local
y-up frame, shifted by `height / 2` to match export centering; Y-flip is
intersection-invariant.

- **Phase 1 (always on, observational):** `_detect_text_hole_collisions()`
  flags per-(line, hole) threshold violations. `RenderedLabel.collision_detected`
  marks any render where a collision was found (even if a later phase fixed
  it) and `RenderedLabel.has_collisions` marks renders that still collide
  after resolution. The per-(line, hole) detection messages (label id, line
  index, hole location, measured gap, and the required-clearance breakdown
  of clearance + stroke floor) are logged once the outcome is known: at
  WARNING when an avoidance phase resolves them, at ERROR when they remain
  unresolved. Detection runs inside `render_label_to_plt` (and the
  collision-avoidance sweeps re-check via `_detect_text_hole_collisions()`).
- **Phase 2 (opt-in via `min_hole_margin`):** sweep `hole_margin` toward the
  floor (analytical — hole positions are pure functions of `hole_margin`);
  on success re-render from the adjusted label clone and log WARNING with
  before/after margins and the offending text line. (Any geometry-altering
  avoidance action — margin reduction or collision compression — always logs
  at WARNING and names the label id plus the affected text line.)
- **Phase 3 (opt-in via `max_h_compress`):** if margins cannot clear the
  overlap, sweep a uniform horizontal compression
  (`collision_compress_by_line` on `ResolvedLabel`, applied before
  margin-driven compression) up to the line budget floor
  `1 - max_h_compress`. Stacks on top of the Phase 2 floor.
- **Failure semantics (only unavoidable collisions fail the job):** a
  collision that an enabled phase resolves logs its detections at WARNING
  (plus the avoidance action's own WARNING) and the render proceeds —
  `collision_detected=True`, `has_collisions=False`. When no enabled phase
  clears a collision, `render_label_to_plt` logs the detections plus full
  diagnostics (gap shortfalls, margin/compression state, recommendations)
  at ERROR and flags `has_collisions=True`. The job-level gate
  `assert_no_collisions()` — wired into `layout.generate_layout_with_bounds`
  (covering `vectorize.export_per_cutter_plts`) — raises `LabelRenderError`
  once every label has been rendered (so all per-label ERRORs print
  first), naming every unresolved label id. The `generate` CLI surfaces the
  abort as a non-zero exit code.
- Adjusted label clones propagate to downstream rendering via
  `RenderedLabel.source_label` (consumed by `layout.unroll_labels_with_rendered_bounds`).

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
  `_detect_text_hole_collisions`.
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
  requested value (compared with `math.isclose`), or a downsized cutter
  (`source_label.cutter_downsize_by_line` non-empty).
- `scripts/run_integration_test.py` prints the full report (`full=True`)
  as the **Phase 3.6: LAYOUT REPORT** section (per label, one line per
  compressed text line: `Line N: '<text>' scale 0.870 (13.0%
  compressed)`, one per gap: `Line N: '<text>' spacing below
  0.120in (requested 0.162in)`, and one per reduced tool: `Line N:
  '<text>' cutter 0.060in -> 0.045in (downsized for compression)`).
- The `generate` CLI prints the same report after the file summaries
  (`Layout report:` header), **compact by default** (only labels with
  findings) and **full under `-v`**; it is skipped silently when the
  render cache is empty. This is the CLI's only per-label stdout beyond
  its summary blocks (see §7).

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
`replacement_text_file` is a template: **each line of the file produces one
label copy** (the file's line count determines the count; `count` must not
be set alongside it). Items within a file line are split on
`replacement_text_delimiter` (default `;`; must be a single special or
whitespace character, never newline/alphanumeric).

- **File format:** pure data — every line is a label copy (no comment
  syntax). CRLF/CR endings are normalized; a single trailing newline does
  not create a phantom copy; a UTF-8 BOM is stripped; items are used
  verbatim (no whitespace stripping). Blank lines render blank lines
  (WARNING logged). Loading failures raise `SubstitutionError` (a
  `ValueError` subclass).
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

### Plate-Space Toolpath Optimization
Generated toolpaths are optimized in the **plate frame** (post-rectpack device
coordinates) before any PLT is written, one routing problem per plate **and per
cutter** (`plt_optimizer/generate/plate_optimizer.py`). Because the generator
already knows each toolpath's kind, the `Profiler` is skipped entirely.

- **Text layers**: one TSP node per `TextChunkRecord` carried out of label
  rendering (label-local inches, +y up). The chunker is bypassed — the records
  *are* the nodes. `text_chunk_mode` (job-level, `"line"` default / `"word"`)
  sets granularity: `line` routes each whole line; `word` splits on whitespace
  (exact stroke membership via `ftext_renderer.render_text_line_ftext_with_words`,
  which partitions the whole-line render's contours by translation-invariant
  signature — bit-exact, no advance math).
- **Structural layers** (borders SP2 + holes SP3): the extracted HPGL is parsed
  and pushed through the shared core pipeline (`core.pipeline`
  `preprocess_document` → `chunk_document` → `optimize_and_reassemble`) with a
  hand-built `ProfileResult(is_structural=True)`; the structural chunker branch
  maps every path 1:1 to a block.
- **Re-emission**: `emit_layer_document` writes integer-unit HPGL
  (`PU`/`PD`/`AA`) framed by the shared `PLT_HEADER` (`IN;PA;`) and
  `PLT_FOOTER` (`SP;`) constants from `label_renderer`. Per-cutter files
  carry **no `SP` pen selects** (each file is a single logical layer; the
  pen map only groups content upstream); every `StrokePath` is PU-led, and
  `extract_pens_from_plt_text` rewrites any section-leading bare `PD` to
  `PU{first_pair};PD{rest}` so dropping the pen-select resets can never
  join adjacent sections with a phantom cut. Written files end with
  `SP;\n`. The generic `PLTWriter` is not used here — it hoists all headers
  ahead of the geometry.
- **Transform chain** (verified against emitted files, plotter units): center
  text block to `height/2` → Y-mirror by the rendered bounds sum → rotate 90° CW
  if the packer placed the label sideways → translate by the packed slot. Text
  geometry is emitted vertex-exact; only coincident structural strokes are
  deduplicated.
- **Strategy**: `ParallelEnsembleStrategy` by default, `NearestNeighbor2Opt`
  under `--fast-mode` (mirrors the `optimize` CLI). `export_per_cutter_plts`
  takes `fast_mode` and `logger`; each layer logs baseline→optimized rapid
  travel at INFO.
- **Direction sweep** (`core/direction_sweep.py`, always on): after the
  strategy fixes the block (chunk) ORDER, `optimize_and_reassemble` re-picks
  each block's forward/reverse traversal to minimise inter-chunk rapid travel,
  alternating forward+backward greedy sweeps to a fixpoint (cap 10 passes,
  kwarg `direction_sweep: bool = True` as the escape hatch). It runs post-unwrap
  in the parent process (ensemble workers rebuild fake single-segment blocks),
  is strictly monotone (the objective never increases; on the generate path the
  emitted `rapid_distance()` is non-increasing since intra-chunk gaps are
  reversal-invariant), and repairs the latent 2-opt staleness (2-opt reverses a
  tour segment without flipping the `reversed` flags). Fires on generated
  multi-line text layers (e.g. the seer wbuv plate: 140979→57734 units, 59%) and
  on parsed fixtures under NoOp; on tours whose construction already picks
  optimal directions it finds no gain and stays silent. When a pass is accepted
  it logs INFO with BOTH metrics (inter-chunk + emitted rapid travel), appends
  `direction_sweep=before->after (N passes, M flips)` to `method_notes`, and
  populates the `OptimizationOutcome.direction_sweep_*` fields (all-or-nothing:
  set only when `passes>0`); `vectorize._report` breaks the layer line into
  `routing` (emitted baseline→emitted optimized, both intra+inter),
  `inter-chunk` (strategy objective), and the `direction sweep` clause.
- **Intra-chunk glyph sweep** (`core/glyph_sweep.py`, always on, generate path
  only): the direction sweep reverses whole chunks, which leaves the rapid
  travel *inside* a chunk exactly invariant — and on generated text that
  intra-chunk share is the majority of the emitted travel. The renderers know
  which strokes belong to which character, so `TextChunkRecord.glyph_groups`
  carries the per-glyph stroke partition (PLT: contiguous by construction from
  the `_walk_line` cursor walk; TTF: glyph-major contour slicing by cached
  per-char contour counts, `glyph_groups_for_line`),
  `plate_optimizer.build_text_blocks_with_glyphs` remaps it onto device paths,
  and `optimize_and_reassemble` (kwargs `intra_sweep: bool = True` +
  `glyph_groups_by_block`) runs `sweep_glyph_directions` per chunk **after**
  the inter-chunk routing is final: with the chronological glyph order fixed
  and the chunk entrance/exit pinned (the group owning the first traced path
  must start at the chronological entry, the one owning the last must end at
  the chronological exit — `_PIN_TOLERANCE`), the optimal per-glyph traversal
  is a shortest path through a chain whose nodes are that glyph's feasible
  traversals, solved exactly by DP; ties resolve to chronological and the
  all-forward traversal is always feasible, so the sweep is monotone and the
  inter-chunk tour (strategy + direction sweep) stays exactly valid.
  - **Per-stroke direction (`_group_modes`)**: a glyph's mode set is the union
    of the historical all-or-nothing pair (stored stroke order traced forwards
    / reversed order traced backwards) and **every per-stroke direction
    assignment on both stroke orders**, so a glyph can reverse just the strokes
    that want reversing (the TODO's "reversing some strokes"). The historical
    pair is a subset, so the optimum can only improve. Traversals are
    deduplicated by endpoint pair (entry/exit depend only on the first/last
    stroke and its direction, so 16 traversals of a 3-stroke glyph collapse to
    8) keeping the cheapest per pair; enumeration is capped at
    `MAX_EXACT_STROKE_MODES` = 10 strokes (the shipped font library peaks at 8,
    99% of glyphs at ≤3), above which the sweep falls back to the two-mode
    pair — a strict subset, so a pathological glyph is never worse. Reversal
    itself stays the Reassembler's job (`reassemble(..., intra_chunk_results=)`;
    `None` entries keep the chronological order, and block-level reversal
    composes by flipping both the sequence and every direction). Parsed PLTs
    (optimize/watch) carry no glyph knowledge, so the parsed path stays
    byte-identical. Fires on generated text: across the seer/wb-uv/intrachunk
    PLT+TTF/sunwest specs the sweep cuts emitted rapid travel 41.4% vs the
    pre-per-stroke sweep's 34.8% (**−10.2% emitted**, 289 → 527 flips); cutting
    geometry is exactly preserved (segment counts identical, arcs stay native).
    On improvement it logs INFO (intra + emitted rapid travel), appends
    `intra_sweep=before->after (N flips)` to `method_notes`, populates the
    `OptimizationOutcome.intra_sweep_*` fields (all-or-nothing: set only when
    `flips>0`), and `vectorize._report` adds the `intra sweep` clause.
  - **Order freedom is measured and rejected** (do not re-attempt the
    "optimize each character, then each word, then order the word chunks"
    hierarchy): real fonts give 2–3 strokes per glyph, and with the chunk
    entrance/exit pinned, chronological (reading) order **is** the optimal
    tour — a glyph/word-order DP gains exactly 0.00%. Freeing the endpoints to
    chase it is catastrophic: the plate TSP and direction sweep exploit a
    chunk's entrance/exit sitting at its left/right edges, so landing the
    entrance mid-word cost +29.7% emitted travel on those specs (+52.8% on
    sunwest). Direction is the entire intra-chunk win.
- **Coincident-stroke merge** (`core/path_merger.py`, always on, final stage):
  after reassembly, consecutive paths whose junction is tip-to-tail stitch into
  one continuous cut, so the redundant tool-up between them disappears. The
  predicate is the writer's own PU-suppression test
  (`PLTWriter._format_stroke_path`, same `COORD_TOLERANCE` = 1e-3): merge
  `tail → head` only when `tail.segments[-1].end` is within tolerance of BOTH
  `head.pen_up_position` (when set) and `head.segments[0].start`, so every
  genuine rapid survives — including arc-native glyph plunges
  (`PU x,y;PD;AA…`). Collapsing is transitive (a run of N tip-to-tail paths
  becomes one path carrying the run's first `pen_up_position`), order-preserving,
  and segment-less paths pass through verbatim as barriers. The transform is
  **metric-neutral**: a merged junction contributes ~0 to
  `PLTDocument.rapid_distance()`, so the win is PU count, path count, and bytes
  — not rapid travel (the undirected segment multiset and cutting distance are
  invariant). It makes the writer's long-standing suppression a real document
  transform, which is what the **generate** path needed: `emit_layer_document`
  emits every path PU-led and kept every tool-up (the integration bh layer
  merges 10 → 3 paths); the parsed path's emitted files are unchanged because
  the writer already collapsed those PUs. Runs in the parent after the sweeps
  (kwarg `merge_coincident: bool = True` as the escape hatch, threaded through
  `plate_optimizer.optimize_text_layer` / `optimize_structural_layer`), so the
  sweeps' `emitted_before` measurements stay pre-merge while `emitted_after`
  describes what is written. On merges it logs INFO, appends
  `merge=before->after (N merge(s))` to `method_notes`, populates
  `OptimizationOutcome.merged_paths_before/after` + `merges_applied` (the counts
  are set whenever the merge *ran*, unlike the sweeps' all-or-nothing fields —
  `merges_applied=0` with non-None counts means "ran, nothing to do"), and
  `vectorize._report` adds the `path merge` clause. Deliberately NOT inside
  `Reassembler.reassemble` (which stays a pure traversal applier, keeping its
  path-count tests and the direct-caller NoOp byte identity intact).
- The post-write `_run_optimizer` (parse each file → profile → optimize) is
  **removed**; optimization now happens pre-write in plate space.

### Cascading Resolution
When a value is `None` at the TextLine/LabelSpec level, it inherits from the parent JobSpec. Cascade order for `hole_margin`: explicit label value → job value → default. Same precedence applies to `max_h_compress` (explicit 0.0 is honored, not treated as unset), `text_h_alignment` (explicit `center` is honored, not treated as unset), `min_hole_margin` (explicit 0.0 is honored; only `None` means unset), `hole_text_collision_distance` (explicit 0.0 is honored; only `None` means unset, falling back to the config or 0.15), `space_width_fraction` (explicit 0.0 is honored; only `None` means unset, falling back to the config or 0.3), `min_glyph_width` (explicit 0.0 is honored; only `None` means unset, falling back to the config or 0.0), and `kerning_window_fraction` (explicit 0.0 is honored; only `None` means unset, falling back to the config or 0.05), `kerning_penetration_scale` (explicit 0.0 is honored; only `None` means unset, falling back to the config or 1.0), `kerning_recession_scale` (explicit 0.0 is honored; only `None` means unset, falling back to the config or 1.0), `kerning_min_gap` (explicit 0.0 is honored; only `None` means unset, falling back to the config or 0.0), and `fallback_advance_fraction` (explicit 0.0 is honored; only `None` means unset, falling back to the config or 1.0), `html_brackets` (explicit pair is honored; only `None` means unset, falling back to the config or `"<>";` the value is an even-length string, first half left delimiter / second half right, halves must differ and contain no alphanumeric/newline characters) and `italic_angle` (explicit 0.0 is honored = upright; only `None` means unset, falling back to the config or 12; schema bounds `[0, 45]`), `cutter_downsize` (explicit `false` is honored; only `None` means unset, falling back to the config or true), `max_cutter_downsizes` (explicit 0 is honored; only `None` means unset, falling back to the config or 1) and `cutter_downsize_global` (explicit `false` is honored; only `None` means unset, falling back to the config or true) and `h_compress_global` (explicit `true` is honored; only `None` means unset, falling back to the config or false) and `optimize_line_content` (explicit `true` is honored; only `None` means unset, falling back to the config or false) and `optimize_line_content_max_lines` (explicit `None` means unset = no growth, no fallback; only an explicit cap > 0 applies) and `use_baseline_spacing` (explicit `false` is honored; only `None` means unset, falling back to the config or true; cascades job → plate → label, never text lines, plate parity only) and `v_center_by_content` (explicit `true` is honored; only `None` means unset, falling back to the config or false; cascades job → plate → label, never text lines, plate parity only; inert while `auto_descending_character_adjustment` is false) and `auto_descending_character_adjustment` (explicit `false` is honored; only `None` means unset, falling back to the config or true; cascades job → plate → label, never text lines, plate parity only) and `descending_character_v_scale` (explicit `0.2` is honored; only `None` means unset, falling back to the config or 0.5) and `descending_character_raise` (explicit `0.0` is honored; only `None` means unset, falling back to the config or 0.7) and `descending_character_clearance` (explicit `0.0` is honored; only `None` means unset, falling back to the config or 0.0; a unitless factor of the engraved-stroke floor, not inches) and `descending_character_reciprocal_scale` (explicit `0.2` is honored; only `None` means unset, falling back to the config or 0.5; `[0.2, 1.0]`, `1.0` = reciprocal downscaling forbidden; all four floats cascade job → plate → label, never text lines, plate parity only). `cutter_size` (explicit-`None` precedence line → label → job → auto-select; no config tier, `gt=0.0` so an explicit `0.0` is a validation error, not a cascade value). `material` cascades label → job (and plate → job via the clearance-style plate cascade): an explicit label/plate value wins, an explicit `null` counts as unset, and empty-after-trim strings are validation errors.

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
- `build_cutter_pen_map` keys on `(cutter_diameter, text_color)` sorted by
  that tuple: smallest layer keeps SP1, rest SP4+ (SP2/SP3 reserved). Jobs
  without colors produce the historical cutter-only assignment bit-identically.
- `export_per_cutter_plts` writes one text file per `(cutter, color)` pen;
  colored layers gain their 1-letter suffix (`0.040_m_txt_<job>.plt`),
  colorless jobs keep the plain cutter-first names. The `bh` structural file
  is never tagged.

### Bin-Packing Rotation (`allow_rotation`)
`JobSpec.allow_rotation` (bool, default `True`) lets the `rectpack` bin
packer test both orientations (0°/90°) for every label instance.

- `_pack_best()` (layout.py) evaluates every `PACK_CONFIGS` heuristic twice
  (rotation off, then on) and ranks candidates by `(footprint, rotated_count)`
  with a float-tolerant footprint comparison. Labels therefore rotate only
  when rotation *strictly* improves the used bounding-box area; at equal
  footprints the all-horizontal layout always wins (backward compatible).
- Rotation is detected in `_extract_packed_plates()` by comparing
  `rect.width` against the *packing* width carried in the `rid` payload
  (rendered bounds in `generate_layout_with_bounds`, nominal otherwise) —
  never against the nominal `ResolvedLabel.width`, which can differ from
  what was actually packed.
- Rotated content is turned 90° **clockwise** at plate assembly:
  `vectorize.rotate_plt_content_90cw()` maps `(x, y) → (y_max − y, x − x_min)`
  over `PA`/`PU`/`PD` pairs and `AA` arc centers (sweep angles preserved —
  a pure rotation has positive determinant), normalizing the rotated bbox to
  the origin so the existing slot translation lands it in `[x, x+H] × [y, y+W]`
  with non-negative coordinates. Text, border and drill holes rotate together
  because they share one `plt_content` string.
- Unbounded-mode `LayoutFitError` wording is orientation-aware ("in either
  orientation"): a label that fails 24x16 upright may still fit sideways.
- Example fixture: `tests_deps/rotation_demo_job.yaml` (pinned by
  `tests/test_layout.py::TestRotationDemoExample`) — a tight 24x10 scrap
  sheet where rotation-required, rotation-refused and opportunistic-rotation
  labels coexist; it aborts with `LayoutFitError` when rotation is disabled.
  The fixture pins `layout: rows` because its banner-must-rotate and abort
  expectations are row-frame specific.

### Bin-Packing Fill Order (`layout`)
`JobSpec.layout` (str-Enum `LayoutMode`, default `columns`) sets the
preferential plate fill order. `columns` fills each plate's **height** before
extending rightward (so the used area grows up the engraving area first and
the unused material stays one clean rectangular block for scrap reuse);
`rows` fills width before extending downward (the historical behaviour). It
cascades job → plate: `PlateSpec.layout` (`None` = inherit) overrides per
plate. Threading mirrors `allow_rotation` (read from `JobSpec` at the CLI /
integration-test call sites, passed as a keyword into
`vectorize.export_per_cutter_plts` → `layout.generate_layout*` → `_pack_best`);
it is a global packing concern and never enters `ResolvedLabel`.

- `rectpack` cannot be *sorted* into column-major: for identical rectangles
  every `SORT_*` is a stable no-op and the emergent fill order is purely the
  placement heuristics' tie-breaking. `PACK_CONFIGS` (Guillotine/MaxRects)
  fill the plate's **long axis** first regardless of frame, so transposing
  alone leaves the real-space order unchanged (verified empirically).
- `columns` therefore packs in a **transposed frame** (bins `(W,H)→(H,W)`,
  rects `(w,h)→(h,w)` via `_transpose_entries`) paired with
  `PACK_CONFIGS_COLUMNS` = `SkylineBl`, whose bottom-row fitness always fills
  the packer's X axis — the real plate height — first. `_pack_best` returns
  packer-space coordinates; callers extract with
  `_extract_packed_plates(packer, transpose=True)`, which maps
  `(x, y, w, h) → (y, x, h, w)` and the bin `(w, h) → (h, w)` back to plate
  space. `rows` runs `PACK_CONFIGS` in the real frame verbatim (bit-identical
  to the historical layouts).
- **`columns` is text-oriented, not plate-oriented.** Because the transposed
  frame often *rotates* most labels, a naive plate-frame column fill would be
  reader **row**-major once the sheet is turned to read the engraving. So
  after extraction, `_extract_packed_plates` (transposed path only) re-assigns
  instance ids onto the unchanged slots via `_reorder_ids_for_reader_order`:
  slots are grouped by the *natural* (unrotated) dims they demand and, within
  each interchangeable group, the content-ordered ids (the 4th `rid` element,
  `seq`) are re-paired onto slots sorted by `_reader_order_key` — plate
  `(x, y)` for unrotated labels, plate `(y, -x)` for rotated ones (reader
  column-major: a rotated label's column is a plate-Y band filled
  right-to-left in plate X, i.e. true top-to-bottom after turning the sheet
  90° CCW). `plate.labels` is returned in this reader order too (assembly and
  the plate optimizer's TSP baseline follow it). Only `(label_id,
  source_label)` pairs move, so each plate keeps its exact id set and slot
  geometry; an all-horizontal `columns` pack is a bit-identical no-op.
  `rows` mode is never reordered.
- Rotation detection is unchanged by the transpose: `_transpose_entries`
  rebuilds the `rid` payload with the *packer-space* width, and a packer-space
  90° swap composed with the transpose is again a 90° swap, so
  `_extract_packed_plates` still compares `rect.width` against `rid[2]` and
  `PackedLabel.rotated` keeps meaning "content turned 90° CW at assembly".
  Instance ids advance bottom-to-top within a column (rectpack y-up).
- `_plate_footprint` (used bounding-box area) is the selection metric for
  both modes and is transpose-symmetric, so the tight-rectangular-block
  objective is orientation-agnostic (no aspect penalty by design).
- Per-plate modes: `_resolve_plate_groups` splits the plate list into maximal
  runs of equal effective mode; `_pack_groups` packs those groups
  sequentially in declaration order, each receiving only the previous group's
  leftovers. A single group (every unbounded job and every uniform-mode job)
  therefore behaves exactly like a one-pass pack. Unbounded mode uses the job
  value.
- Example fixture: `tests_deps/columns_demo_job.yaml` (pinned by
  `tests/test_layout.py::TestColumnsDemoExample`) — 16 3x1 labels on a 24x16
  sheet pack into one full-height column (bounding box 3x16).

### Plate Edge Clearances (`left_clearance` / `top_clearance`)

`PlateSpec.width`/`height` describe the **usable** pack area. Two optional
fields shift that area within the physical material to account for scrap
that does not start at the sheet's left/top edge:

- `left_clearance` (default `0.0`): unused material along the plate's
  **left** edge; shifts the pack area rightward.
- `top_clearance` (default `0.0`): unused material along the plate's
  **top** edge; shifts the pack area downward.

Both fields also exist at the **job level** (`JobSpec`, default `None` =
unset) and cascade job -> plate like `layout`: `JobSpec._apply_job_level_clearances`
fills every plate that omits the field, while an explicit plate value
(including an explicit `0.0`) always wins; an explicit plate `null` counts
as unset (a `PlateSpec` before-validator drops the key). The job-level
value additionally drives unbounded mode: the CLI / integration runner
read it off the parsed `JobSpec` (`_default_plate_clearance(job)`, after
job-config injection) and thread it into the export/layout calls as
`default_plate_clearance`, so auto-allocated bins shift identically.

Bottom and right clearances need no fields: in the emitted plate frame
(origin at the material's top-left, +y downward — the HPGL device
convention) the usable area spans `[left_clearance, left_clearance + width]`
horizontally and `[top_clearance, top_clearance + height]` vertically, so
the material's right edge always sits at `left_clearance + width` and its
bottom edge at `top_clearance + height`.

Packing is unchanged — the packer
still receives the usable `width × height` bin — and
`_extract_packed_plates` adds `(left_clearance, top_clearance)` to every
placement (in real plate space, after any transpose mapping).
Zero-clearance output is therefore bit-identical to the historical
behaviour. `_plate_clearances` builds the per-bin-id `(left, top)` map
(omitting all-zero plates) that `_pack_groups` → `_pack_group` →
`_extract_packed_plates` threads through.

### Material Partitioned Packing (`material`)

`material` (free-form string, default `null`) groups labels by the stock they
are cut from, so one job YAML can carry a mixed-material batch. Declared on
`LabelAttributes` (cascades label → job, inherited by `LabelSpec` and
`JobSpec`; NOT on `TextLine`/`TextAttributes`) and on `PlateSpec` (cascades
plate → job like the clearances; an explicit plate `null` counts as unset).
`normalize_material` trims and rejects empty/whitespace-only names; matching
uses `material_key` (trim + casefold) while the first-declared spelling is kept
for display. Not a `job-config.json` key — per-job decision only.

- `PackedPlate.material` carries the plate's material out of the layout engine;
  `_split_material_entries` groups entries by material key (insertion order =
  content order) and `_pack_entries_with_pinners` runs **one packing pass per
  material**: materials never share a plate.
- Constrained mode: plates declaring `material` join exactly their group's pool
  (`_claim_plates_by_material`); a declared material no label uses leaves the
  plate unused (WARNING); material-less plates spread across the groups
  (fewest-claimed first, declaration order as tie-break) so no group starves.
  A group with no pool aborts with `LayoutFitError` ("no plate to pack onto"),
  a group that overflows its pool aborts with "Materials never share a plate"
  (no partial leftover propagates). Pinned labels whose materials conflict
  with their plate's material abort ("carry conflicting material").
- Unbounded mode: one auto-allocated bin pool per material,
  `<sanitized>_default_plate_{i}` (sanitized = `[^0-9A-Za-z]` stripped; the
  display material is the group's first-declared spelling).
- Plate-level `replacement_text_file` labels inherit the declaring plate's
  material at expansion (`substitution`), the only point where the plate →
  label direction is applied (labels resolve before plate assignment).
- Output filenames gain the material tag (see §7): `wbuv_0.045_txt_<job>.plt`.
- A material-less job is one `None` group: bit-identical to the historical
  single-pass packing (constrained leftovers keep the generic error, unbounded
  bins keep `default_plate_{i}` ids).
- Example fixture: `tests_deps/material_demo_job.yaml` (pinned by
  `tests/test_layout.py::TestMaterialDemoExample` and
  `tests/test_phase3_export.py::TestMaterialDemoExample`) — two materials, one
  plate each, plus a magenta deep-engraved layer on the uv sheet.

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
  `max_h_compress`, `text_h_alignment`, `space_width_fraction`,
  `min_glyph_width`, `kerning_window_fraction`, `kerning_penetration_scale`,
  `kerning_recession_scale`,
  `kerning_min_gap`, `fallback_advance_fraction`, `sc_scale`, `cutter_downsize`,
  `max_cutter_downsizes`, `cutter_downsize_global`, `h_compress_global`,
  `optimize_line_content`, `holes`, `allow_rotation`,
  `text_chunk_mode`, `layout`) fill missing **job-level** keys; the existing
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

## 7. CLI Surface (`optimize` / `generate` / `watch`)

The console script `plt-optimizer` is routed by `main.py` into three
subcommands (`plt_optimizer/cli/optimize.py`, `generate.py`, `watch.py`).
`main.py` must stay the single entry point (`[project.scripts]` in
`pyproject.toml` points at `main:main`).

### `optimize <input.plt>`
Single-file parse → profile (text vs. structural) → chunk → optimize →
reassemble → write. Flags: `-o/--output` (default `<stem>_optimized.plt`
beside the input), `--fast-mode` (NearestNeighbor2Opt only; default is
ParallelEnsemble with per-strategy benchmark logging), `-v/--verbose`,
`--log-dir` (default `./logs_optimize/`; writes `optimizer.log` +
`job_metrics.csv`). Prints one summary line to stdout unless `-v`.

### `generate <spec.yaml>`
Three-phase pipeline: `parse_yaml` → `expand_job_spec` → `resolve_job_spec` →
`export_per_cutter_plts` (bounds-aware packing + per-label rendering + plate-space
per-cutter optimization + split by cutter). Flags: `-o/--output` (default: spec's
parent dir; receives `plt/` and `pdf/`), `-v/--verbose`, `--no-plots` (skip
simple-outline PDF previews), `--default-plots` (opt-in color-coded `*_default.pdf`
rapid-travel plots), `--tools` (default `tools.json`; missing file → ideal
cutters), `--job-config` (default `job-config.json`; top-layer job defaults and
the required-when-unconfigured gate, see section 6), `--fast-mode` (plate-space
routing via `NearestNeighbor2Opt` instead of the default `ParallelEnsemble`). File names:
`[<2-digit plate>_][<material>_]<cutter>[_<color>]_{txt|bh}_<job_id>.<plt|pdf>`
plus combined `[<2-digit plate>_][<material>_]all_<job_id>.pdf`. The plate
number appears only when a material spans more than one plate (its presence
signals that *this material* needs multiple sheets; material-less plates form
one group, so multi-sheet material-less jobs always keep their numbers); the
material tag appears only for material-declared output.
Simple-outline PDFs style strokes by toolpath kind:
purely structural (`bh`) plots use `linewidth=2.0`/`alpha=0.3`; text and mixed
combined (`all`) plots use `linewidth=1.0`/`alpha=1.0` (via
`plot_plt_document(..., is_structural=...)`). Logs go to `./logs_generate/generate.log`.
Collision aborts (`LabelRenderError`) and fit failures (`LayoutFitError`) exit
non-zero after full ERROR diagnostics.

### `watch --watch-dir <dir>`
Hot-folder daemon. Flags: `--watch-dir` (required), `--output-dir` (default
`./optimized`), `--log-dir` (default `./logs`), `--processed-dir` (archive
originals; without it originals are **deleted**), `--fast-mode`,
`--debug-save-files` (before/after PLTs + PNG plots under `<log-dir>/debug/`;
only with an explicit `--log-dir`), `--debounce-seconds` (default 2.0).
Behavioural invariants:
- Files are processed only after `debounce_seconds` of quiescence **and** after
  the OS file lock is released (`_is_file_locked` probe).
- Outputs are staged in `<output-dir>/.incomplete/` and moved into place with
  `os.replace` (fallback `shutil.move`) — consumers never see partial files.
- On failure, the input is copied to the output dir as `<stem>_unprocessed.plt`
  and removed from the watch dir; the job is logged with `status=failed`.
- `setup_parser()` is the single source of truth for watch flags (shared by the
  `main.py` router and `python -m plt_optimizer.cli.watch`); the tray app calls
  `run_watcher_from_config()` instead of the CLI layer.

### Python 3.8 / Windows 7 import constraint
Per section 5, Windows 7 support is **CLI-only**: the pre-built
`Ploptimizer.exe` installer and the system tray GUI are Windows 10+ only
(dropped for Win7). On Windows 7 the supported workflow is the headless
`watch`/`optimize` CLI on Python 3.8 without matplotlib (not installable on
3.8). Therefore `watch` (and the tray's watcher path) must remain importable
and runnable on Python 3.8 without matplotlib. The `generate` pipeline and its
modules (`plt_optimizer/generate/*`, which import numpy/matplotlib/vpype text
rendering) require Python 3.9+. Keep heavy generation imports out of any code
path the watch/optimize commands execute at startup:
- `main.py` builds all three subparsers at startup, so
  `plt_optimizer/cli/generate.py` stays import-light at module scope: pure
  Python imports (`schema`, `resolution`, `substitution`) are top-level, while
  the matplotlib-transitive ones (`layout`, `label_renderer`, `vectorize`) are
  imported lazily inside `run()`. Tests monkeypatch
  `plt_optimizer.generate.vectorize.export_per_cutter_plts` (the source module),
  not the CLI module attribute.
- Verify with a matplotlib import-blocker that `import main` and
  `plt-optimizer watch --help` succeed without matplotlib before changing CLI
  import structure.
