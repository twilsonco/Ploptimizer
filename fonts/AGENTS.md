# AGENTS.md - Font Library & Extraction (`fonts/`)

Parent contracts: [`../AGENTS.md`](../AGENTS.md) (global standards; §4's
device-scale note explains the font-library design frame) and
[`../plt_optimizer/generate/AGENTS.md`](../plt_optimizer/generate/AGENTS.md)
(the renderer side: Small Caps Fonts, Multi-Stroke (Bold) PLT Fonts,
font-registry bullets).

## Layout

- `extract_plt_fonts.py` - the v2 extractor. Workflow guide:
  "How to extract fonts from EngraveLab or VisionPro.md" (Text Compose, rows
  framed by the reference char, engrave → PLT). Run:
  `uv run python fonts/extract_plt_fonts.py`. A successful catalog write
  finishes by regenerating the showcase JPGs in-process (`--no-showcase`
  opts out, `--showcase-output` redirects the JPGs); the step is skipped
  when `--output-dir` is not the default catalog (the showcase renders the
  default library) and a showcase failure never changes the extraction rc.
- `font_showcase.py` - per-font showcase JPGs (letter sample block; key→symbol
  grid with an alpha-face header for `symbol_font` fonts), written to the
  **git-tracked** `showcases/` directory. Chrome-free renders (no axes/grid/
  title - the font name is engraved in the body). Run after any library
  change: `uv run python fonts/font_showcase.py` (the extractor runs it
  automatically after extraction).
- `glyph_transforms.py` - arc-native HPGL glyph utilities + the 17 derived
  Unicode glyphs (`–—•∞±¢≠≈≡¿¡†‡↑↓←→`) built from ASCII bases. Operates on the
  catalog's self-contained HPGL command strings at full float precision (the
  core `Coordinate` rounds to 3 decimals and would truncate the 4-decimal
  storage format - use `GlyphGeometry`).
- `ascii.txt` / `ascii_E.txt` / `ascii_2space*.txt` - character-order reference
  sheets the extractor verifies rows against (count + framing geometry ≤ 1e-3
  units).
- `VP-EL-fonts/input-plt/` - the engraved sample sheets, named
  `<font>_<height in>_<ref char>[_S<n>][_sym].plt` (`_S<n>` = multi-stroke/bold
  pass count → `stroke_count` metadata; `_S0` is an error, `_Sx` is not
  stripped; a trailing `_sym`/`_symbol`, case-insensitive, marks a symbol
  font → `symbol_font` metadata; canonical order is `_S<n>_sym`).
- `VP-EL-fonts/catalog/*.json` - one file per font (the v2 library the renderer
  loads via `../plt_optimizer/generate/font_registry.py`). Every file carries
  the `symbol_font` bool (from the sheet's `_sym` suffix): symbol-font
  exemptions (Wingdings etc.) skip derived glyphs, the `SC` variant, and the
  showcase's Unicode line; the showcase renders those as a key→symbol grid.
- `Line fonts/`, `ttf/` - raw font source files (`.VEF`, TTF/OTF).

## v2 library contract (the renderer depends on all of it)

- Glyphs stored **+y up, baseline y = 0, plotter units, reference char exactly
  1000 units tall** (`normalized_ref_height` 1.0). This design frame is 1000 per
  *design* inch (`LIBRARY_UNITS_PER_INCH`) - a self-normalizing frame, NOT the
  1016 device scale; do not "fix" it (root AGENTS.md §4).
- Nested JSON schema: `characters[<char>]` → `bbox` / `left_envelope` /
  `right_envelope` / `glyph` (envelopes = 30 samples; kerning measures the
  silhouettes in the shared baseline frame, so a geometry transform MUST move
  block + bbox + both envelopes together).
- Drift: measured height stored unadjusted; >5% WARNING.
- `VERTICALLY_CENTERED_CHARS` (currently `S`): extraction-time pure-Y shift
  centering the ink box on the midline; the reference char is ALWAYS exempt
  (it defines y=0/y=1000 and the framing verification).
- Regeneration must stay reproducible: `--no-vertical-centering` is verified
  byte-identical to the pre-centering library.

## Invariants

- Fixture `tests_deps/dino_0.5_E.plt` MUST keep the
  `<font>_<height>_<ref>` name (the extractor parses it).
- After a library regen, demo AA-arc counts in
  `tests/test_phase3_export.py` may need re-pinning (kerning/compression
  changes flatten arcs) - re-pin only when counts actually move.
- `fonts/` scripts run on the dev interpreter (uv, modern Python); only the
  emitted library data crosses to the Win7 runtime.
