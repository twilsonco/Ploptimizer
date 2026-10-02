# How to extract fonts from EngraveLab or VisionPro

EngraveLab and Vision Pro cannot export their vector fonts, but they can
*engrave* them. This guide turns any installed font into entries in
`Fonts/plt_fonts.json`: origin-centered HPGL toolpath strings, one per
printable ASCII character, ready for the label generator to place and scale.

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

2. Set the **text height to exactly 1.0 inch** and remove any horizontal or
   vertical compression / stretch settings.

3. Paste the contents of [`ascii.txt`](ascii.txt) as the text: every printable
   ASCII character (`!` through `~`) in one long row, separated by many spaces.
   The wide spacing is what lets the extractor tell characters apart — do not
   reduce it. The row will be long (roughly 20+ inches); that is expected. If
   your software wraps it onto multiple lines, split it into two documents
   (e.g. `!`–`[` and `]`–`~`) and extract each with `--font-name` + merge
   (see [Tips](#tips-and-troubleshooting)).

4. Select the desired font. Any single-line (stroke) font works; script or
   multi-stroke fonts are fine too — each character's strokes are kept together.

## Step 2 — Engrave to PLT

"Engrave" (plot) the document to a PLT file using your usual plotter post.
Do **not** let the software scale-to-fit the material; the 1.0-inch text
height must survive into the file (the extractor logs the median glyph height
as a sanity check).

## Step 3 — Copy the PLT into `Fonts/PLT-ascii/`

Save/copy the file as:

```
Fonts/PLT-ascii/<font_name>.plt
```

`<font_name>` becomes the JSON key, title-cased (`DINO.plt` → `"Dino"`).
One font per file, one full ASCII row per file.

> **Note:** files in `Fonts/PLT-ascii/` must each contain the *full*
> `ascii.txt` row for one font. Word engravings or partial samples cannot be
> mapped to the character list and will be rejected with a clear error.

## Step 4 — Run the extractor

```bash
uv run python Fonts/extract_plt_fonts.py
```

The script:

1. Parses each `Fonts/PLT-ascii/*.plt` with the core PLT parser.
2. Clusters stroke paths along X (the wide inter-character spacing separates
   characters even though EngraveLab emits strokes in scrambled order).
3. Translates every glyph so it is centered on the origin (Y keeps the
   plotter's native down-positive convention).
4. Creates/updates [`plt_fonts.json`](plt_fonts.json):

```json
{
  "Dino": {
    "!": "PU-0.015,249.500;PD-0.015,-249.500;PU0.000,499.000;PD0.000,480.000;",
    "\"": "PU-60.000,300.000;PD-60.000,500.000;PU60.000,300.000;PD60.000,500.000;",
    "...": "..."
  },
  "Jhanuni": { "...": "..." }
}
```

Each value is a self-contained `PU`/`PD`/`AA` command string in plotter units
(1000 units = 1 inch, 3-decimal precision) that draws the character centered
at `(0, 0)`. Existing fonts in the file are preserved; only the fonts just
extracted are replaced.

### Useful flags

| Flag | Purpose |
| --- | --- |
| `-v` / `--verbose` | Debug logging (per-cluster detail). |
| `--fonts-dir DIR` | Read sample sheets from somewhere else. |
| `--output FILE` | Write the JSON somewhere else. |
| `--font-name NAME` | Explicit font key (requires exactly one input file). |
| `--cluster-threshold N` | Manual X-gap split distance in plotter units. Only if auto-calibration fails. |
| `--rebuild` | Regenerate the JSON from scratch, dropping fonts whose `.plt` is gone. |

## Tips and troubleshooting

- **"Clustering produced N glyph groups but the character list has 94."**
  The sheet doesn't contain exactly one widely-spaced copy of every character
  in order. Check: full `ascii.txt` pasted? Single row (not wrapped)?
  Text Compose (no compression)? If the row legitimately wraps, engrave two
  documents and extract each with `--font-name` into the same font key using
  two runs plus a manual merge, or split `ascii.txt` accordingly.
- **"Found only N separated stroke groups."** The characters are touching or
  nearly touching — the spacing is too small. Re-engrave with many more spaces
  between characters.
- **Median glyph height far from 1.0 in.** Something scaled the text
  (Frame Text Compose, or a scale-to-fit in the plot post). The glyphs still
  extract, but downstream placement assumes 1-inch design height.
- **A character logs a degenerate/no-geometry WARNING.** Some fonts draw
  certain characters with zero width (e.g. a dot-less style) — harmless; the
  entry is kept as an empty string and renders as blank.
- **Two files map to the same font key** (e.g. `DINO.plt` and `dino.plt`):
  the last one processed wins. Use distinct file names or `--font-name`.
- The space character is intentionally **not** in `plt_fonts.json` (nothing to
  cut); the downstream placement step handles character advance.
