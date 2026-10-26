# Cascade Rules, job-config.json and tools.json — semantics

## How `null` resolves

`null`/omitted **always means "inherit"**; an explicit value (including `0.0`)
**always wins at its level**. The general tier order is
**line → label → job → config → code fallback**, with per-attribute
exceptions listed below. "Plate parity" means the field is *accepted* on
`PlateSpec` for schema symmetry but **not applied** at plate level (labels
render once, before packing).

| Attribute | Cascade order | Fallback |
|---|---|---|
| `text_height`, `character_spacing`, `line_spacing`, `max_h_compress`, `text_h_alignment` | line → label → job → config | see the fallback table |
| `cutter_size` | line → label → job (no config tier) | `null` = auto-select from `text_height` |
| `target_boldness_factor`, `multistroke_overlap_ratio` | line → label → job → config (plate parity) | `0.14` / `0.25` (multi-stroke PLT fonts only) |
| `bold_boldness_multiplier` | line → label → job → config (plate parity) | `1.5` (`<b>` on multi-stroke PLT fonts only) |
| `cutter_downsize`, `max_cutter_downsizes`, `cutter_downsize_global` | line → label → job → config | `true` / `1` / `true` |
| `h_compress_global` | line → label → job → config | `false` |
| `enforce_cap_line` | line → label → job → config (plate parity) | `true` |
| `use_baseline_spacing` | label → job → config (never lines; plate parity) | `true` |
| `v_center_by_content` | label → job → config (never lines; plate parity) | `false` |
| `auto_descending_character_adjustment`, `descending_character_v_scale`, `descending_character_raise`, `descending_character_clearance`, `descending_character_reciprocal_scale` | label → job → config (never lines; plate parity) | `true` / `0.5` / `0.7` / `0.0` / `0.5` |
| `text_color` | line → label (never job/plate/config) | `none` (implicit) |
| `new_line` | line only (never label/job/plate/config) | `false` |
| `bullet_string` | item → list block → label → job → config (plate parity) | `•` (the derived bullet glyph) |
| `counter_start_index`, `counter_style`, `counter_prefix`, `counter_suffix` | item → list block → label → job → config (plate parity) | `1` / `decimal` / `''` / `''` |
| `list_marker_gap_fraction` | item → list block → label → job → config (plate parity) | `0.5` (of the line's cap height) |
| `width`, `height` | label → job → auto-size from rendered content | — |
| `margin` | label → job → config | `0.125` |
| `h_margin`, `v_margin` | label → job → config | `margin` (then `0.125`) |
| `hole_margin` | label → job → config | `0.1875` |
| `min_hole_margin`, `hole_text_collision_distance` | label → job → config | see the fallback table |
| `hole_adjust_for_cutter` | label → job → config (never lines; plate parity) | `true` |
| `holes` | label (replaces the job list entirely; `[]` suppresses) → job → config | none |
| `layout`, `allow_rotation`, `text_chunk_mode` | job → config | `columns` / `true` / `line` |
| `layout` (per plate) | plate → job → config | — |
| `material` | label → job; plate → job | `null` (unset) |
| `replacement_text_delimiter` | label → plate (pinned) → job → config | `;` |
| `replacement_text_file_comment_string` | label → plate (pinned) → job → config | `"# "` (`""` = comments off) |
| `left_clearance`, `top_clearance` | plate → job → config | `0.0` |
| plate `width`/`height` | plate → config (`plate_width`/`plate_height`) | 24×16 (unbounded auto-allocation only) |

The complete, always-current constant values live in
`fallback-defaults.md` beside this file (generated from source).

## job-config.json: the shop tier

`job-config.json` sits **below the YAML but above the code fallbacks**. Its
values are injected at the **job layer** before validation, so any label (or
line) still overrides them. This is why an author should *omit* a field to
inherit the shop default rather than copy the shop's number into the spec —
retuning `job-config.json` then reaches every job that inherits.

Two injection subtleties:

- **`margin` suppression.** A spec-declared `margin` (job level, **or on any
  label**) suppresses the configured `h_margin`/`v_margin` injection. The
  cascade checks the axis-specific job keys *before* `margin`, so an injected
  axis value would otherwise silently outrank the spec's `margin` on that
  axis.
- **Replacement-file-only injection.** The configured
  `replacement_text_delimiter` and `replacement_text_file_comment_string` are
  injected **only** when the spec declares a `replacement_text_file` at some
  tier. The schema rejects a job-level delimiter/comment marker in a job with
  no replacement file anywhere.
- **`hole_diameter`** in the config fills the `diameter` of hole entries that
  omit it.

Unknown keys in `job-config.json` fail loudly (the config model is strict,
like the spec).

## Required-when-unconfigured gate

When a `--job-config` path is in play, these fields must be supplied by the
config **or** the spec (job level, or on every label); missing everywhere
aborts parsing with `JobConfigError`:

- `max_h_compress`
- `hole_margin`
- `min_hole_margin`
- `hole_text_collision_distance`
- `plate_width` / `plate_height` (only when the job declares no plates)

Without a config path, silent code fallbacks apply (the historical
all-optional contract) — which is exactly why production runs must pass the
shop config.

## tools.json: the cutter inventory

`tools.json` supplies `available_cutters` (the physical tool rack) and the
`boundary_hole_cutter_size`. The cutter ladder snaps requested diameters to
this inventory (see the cutters skill); compression-driven downsize steps
along it. **No `tools.json` → ideal cutters are used and compression-driven
downsize is disabled**, so the engraving may not match the real tooling.
`tools.json` is a run-time input (`--tools`), not part of the spec.
