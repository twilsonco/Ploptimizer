# How to extract fonts from EngraveLab or VisionPro

EngraveLab and Vision Pro cannot export their vector fonts, but they can
*engrave* them. This guide turns any installed font into an entry in
`Fonts/plt_fonts.json`: baseline-normalized HPGL toolpaths plus left/right
profile envelopes, one per printable ASCII character, ready for the label
generator to place, scale, and kern.

## What you need

- EngraveLab or Vision Pro with the font installed/available.
- A plotter post that produces a PLT (HPGL) file (the same one you already
  use for `plt-optimizer`).
- This repository checked out, with [`uv`](https://docs.astral.sh/uv/) available.

## Step 1 — Create the ASCII sample sheet

1. Open a new document and select the **Text Compose** tool.

   > **Important:** use *Text Compose*, **not** *Frame Text Compose*. Frame
   > Text Compose compresses the text to fit the plate width, which would
   > distort every glyph. Text Compose engraves the font at its natural width.

2. **Pick a reference character.** Every row of the sheet must start and end
   with the same chosen character — `E` is the recommended default. It acts as
   structural *framing*: the extractor uses it to verify the row split and to
   derive the normalization scale. Requirements:

   - It must **sit on the baseline** and have real height (letters like `E`,
     `H`, `F` are ideal; `.` or `_` are not — they measure ~0 tall and the
     extraction aborts).
   - It must be a character **in [`ascii.txt`](ascii.txt)** — it is also
     engraved as normal payload, and its internal copy is the geometry the
     framing copies are verified against.
   - It must be **filesystem-safe**: `|`, `:`, `*`, `?`, `"` and `\` are
     invalid in Windows file names, so avoid them as the reference.

3. Set the **text height** and note the value you chose. EngraveLab silently
   compresses the *engraved toolpath* to fit the plate, so the whole sheet
   must fit your machine plate at that height without compression — remove
   any horizontal/vertical compression or stretch settings.

   > **Crisper glyphs:** the extractor scales everything up, including
   > EngraveLab's per-stroke jitter at small sizes. Engraving at 0.05 inch
   > means a ~20x upscale; 0.5 inch means only ~2x, so the same jitter lands
   > ~10x smaller in the final font. Use the **largest height that still fits
   > your plate without compression** (e.g. 0.2"–0.5" where the plate allows)
   > and put that number in the file name.

4. Type the characters of [`ascii.txt`](ascii.txt) — every printable ASCII
   character (`!` through `~`, 94 total) **in exact order** — split across as
   many rows as your plate needs, then put the reference character at the
   **start and end of every row**. For example, with reference `E` and a
   three-row split:

   ```
   E ! " # $ % & ' ( ) * + , - . / 0 1 2 3 4 5 6 7 8 9 : ; < = > ? @ A B C D E F E
   E G H I J K L M N O P Q R S T U V W X Y Z [ \ ] ^ _ ` a b c d e f g h i j k l m E
   E n o p q r s t u v w x y z { | } ~ E
   ```

   The rows do not need to be equal length — the extractor discovers the row
   split from the geometry and verifies it against `ascii.txt`. What must hold:

   - Rows are in reading order (top row = the first characters of
     `ascii.txt`), and characters within each row are left-to-right in order.
   - **Every row starts and ends with the reference character.** The framing
     copies are structure only; the count check ignores them
     (`sum(row_glyphs − 2) = 94`).
   - Characters are spaced far enough apart that no two glyphs touch. Use
     spaces or tabs between characters if needed — extra whitespace is fine
     and makes clustering more reliable.
   - Recommended method is to simply copy the text of `ascii.txt` into your document, then add the reference character at the start and end of each row.

   > The number of spaces between characters does **not** need to be uniform,
   > and rows may even overlap horizontally — glyph grouping is spatial, not
   > positional.

5. Select the desired font. Any single-line (stroke) font works; script or
   multi-stroke fonts are fine too — each character's strokes are kept
   together.

## Step 2 — Engrave to PLT

"Engrave" (plot) the document to a PLT file using your usual plotter post.
The sheet must fit the plate at the chosen text height — if EngraveLab still
compresses the output (glyphs come out squashed, or the measured reference
height logged in Step 4 is far below your chosen height), lower the text
height and re-engrave.

## Step 3 — Copy the PLT into `Fonts/PLT/`

Save/copy the file as:

```
Fonts/PLT/<font name>_<declared text height>_<reference char>.plt
```

where `<declared text height>` is the text height set in Step 1 (inches) and
`<reference char>` is the framing character chosen in Step 1:

```
Fonts/PLT/dino_0.5_E.plt             →  font "Dino",            0.5 in, ref E
Fonts/PLT/heavy_engraving_0.25_H.plt →  font "Heavy Engraving", 0.25 in, ref H
```

The font-name part becomes the JSON key with underscores turned into spaces
and title-cased (`heavy_engraving_0.25_H.plt` → `"Heavy Engraving"`). One
font per file; the file must contain the **full** `ascii.txt` character set
framed on every row. Word engravings or partial samples cannot be mapped to
the character list and are rejected with a clear error.

> The declared height in the file name is **metadata only**. The extractor
> derives the true scale from the *measured* reference character, so a small
> mismatch between the two is fine (see [Height drift](#height-drift)).

## Step 4 — Run the extractor

```bash
uv run python Fonts/extract_plt_fonts.py
```

The script processes every `Fonts/PLT/*.plt` and, per sheet:

1. **Parses** it with the core PLT parser and **groups the strokes into rows**
   (single-linkage merging of Y extents). The row split and the per-row glyph
   split are auto-searched — nothing about the row pitch or character pitch
   needs to be configured.
2. **Verifies the layout** against `Fonts/ascii.txt` before trusting any
   glyph:
   - *Count check:* the payload glyphs inside the framing must total exactly
     the character count (`Σ(row_glyphs − 2) = 94`).
   - *Framing check:* the internal copy of the reference character must
     coincide with every row-framing copy to within 0.001 plotter units once
     translated to a common origin. This proves the row split and character
     alignment are correct, not merely plausible — a wrong split fails loudly
     instead of silently mislabeling glyphs.
3. **Normalizes every glyph** with one global similarity transform derived
   from the *measured* reference height `H_ref`:
   `X_norm = S·(X_raw − X_left_glyph)`, `Y_norm = S·(Y_baseline_row − Y_raw)`
   with `S = 1000 / H_ref`. Everything stays in **plotter units**
   (1000 units = 1 inch): the reference character stands exactly **1000 units
   tall**, every glyph sits on the baseline at `y = 0` with its left edge at
   `x = 0`, and **Y grows upward** — descenders (`g`, `j`, `p`, `_`, ...) get
   negative `y`. The raw sheets are engraved +Y-down, so this mirrors Y and
   negates every `AA` arc sweep.
4. **Aggregates the profile envelopes**: each glyph's left and right silhouette
   is recorded over 30 uniform vertical *bands* — every band covers half a
   sampling step above and below its height and takes the extreme X of *any*
   stroke meeting it (analytic line/arc intersections, no chord flattening).
   Banding is essential: EngraveLab engraves horizontal bars as single
   zero-width strokes, and a zero-thickness sample line systematically misses
   them whenever the bar's height is not an exact multiple of the sample step
   (e.g. a bar at y = 500.29 on a 34.48-unit grid). Bands tile the axis, so no
   hairline can slip through, giving the typesetter real air-gap profiles for
   tight kerning instead of fixed advance widths.
5. **Merges** the result into [`plt_fonts.json`](plt_fonts.json) — existing
   fonts are preserved; only the fonts just extracted are replaced.

### Output schema

```json
{
  "Dino": {
    "file_path": "Fonts/PLT/dino_0.5_E.plt",
    "reference_char": "E",
    "declared_height_in": 0.5,
    "reference_char_height_in": 0.508017,
    "normalized_ref_height": 1.0,
    "characters": {
      "!": {
        "bounding_box": {"min_x": 0.0, "max_x": 122.4, "min_y": 0.0, "max_y": 1000.0},
        "left_envelope": [[0.0, 0.0], [0.0, 34.48], "..."],
        "right_envelope": [[122.4, 0.0], [122.4, 34.48], "..."],
        "glyph": "PU0.0000,0.0000;PD0.0000,1000.0000;..."
      },
      "...": {}
    }
  }
}
```

All numbers are plotter units (1000 = 1 inch, 6-decimal rounding); `glyph` is
a self-contained `PU`/`PD`/`AA` string at 4-decimal precision. The space
character is intentionally **not** in the file (nothing to cut); the
downstream placement step handles character advance.

### Height drift

The measured reference height is **always stored as measured** — the
extractor never applies a corrective adjustment, because silently rescaling
would hide sheet problems (wrong reference character, mis-sized framing,
plate compression). The drift `|measured − declared| / declared` is checked
against 5%:

- **≤ 5%:** silent. Engraver jitter and font metrics routinely produce a few
  percent of drift (e.g. Dino: declared 0.5", measured 0.508" → 1.6%).
- **> 5%:** a prominent WARNING naming the font, both heights, and the drift
  percentage is logged — but extraction **completes normally** and the
  measured value is kept. The warning asks you to verify the reference
  character in the file name and that the framing characters were engraved
  at the same size as the payload.

### Useful flags

| Flag | Purpose |
| --- | --- |
| `-v` / `--verbose` | Debug logging (per-cluster detail). |
| `--fonts-dir DIR` | Read sample sheets from somewhere else. |
| `--output FILE` | Write the JSON somewhere else. |
| `--ascii-file FILE` | Use a different ordered character list. |
| `--font-name NAME` | Explicit font key (requires exactly one input file; the height and reference char still come from the file name). |
| `--row-threshold N` | Manual Y-gap row-split distance in plotter units. Only if the auto-search fails. |
| `--cluster-threshold N` | Manual X-gap glyph-split distance in plotter units. Only if the auto-search fails. |
| `--envelope-samples N` | Vertical bands per profile envelope (default 30); each band aggregates the geometry within half a sampling step of its height. |
| `--rebuild` | Regenerate the JSON from scratch, dropping fonts whose `.plt` is gone. |

## Tips and troubleshooting

- **"...file name must be '<font name>_<declared height in>_<ref char>.plt'".**
  The last two `_`-separated tokens of the file name (before `.plt`) are
  always read as the height and the reference character. Font names may
  contain underscores (`heavy_engraving_0.5_E.plt` works), but a font name
  that genuinely *ends* in a number needs care.
- **"Could not split the sheet into 94 payload glyphs framed by ...".** The
  sheet doesn't match the framed multi-row contract. Check: every row starts
  *and* ends with the reference character? Full `ascii.txt` engraved in order?
  Text Compose (no compression)? Characters far enough apart to not touch?
  The error message names the last failure (wrong count → how many glyphs per
  row were found; framing mismatch → which row's copy deviated).
- **"...differs from the internal copy by N units".** The row boundaries were
  split at the wrong place or a framing glyph was engraved at a different
  size than the payload. Re-engrave with uniform text height — do not resize
  the framing characters.
- **"Reference character 'X' measures 0.000 units tall".** The reference
  character has no height above the baseline (e.g. `_`, `.`, `'`). Pick one
  with real height (`E` recommended) and rename the file.
- **A character logs a degenerate/no-geometry WARNING.** Some fonts draw
  certain characters with zero width — harmless; the entry is kept and renders
  as blank.
- **Two files map to the same font key** (e.g. `dino_0.5_E.plt` and
  `dino_0.75_E.plt` both → `"Dino"`): the last one processed wins. Use
  distinct font-name parts or `--font-name` to disambiguate.
- **Legacy `plt_fonts.json`.** The schema changed (nested per-character
  entries with envelopes); an old flat file cannot be merged and the script
  says so. Re-run with `--rebuild` to regenerate it from the sample sheets.
