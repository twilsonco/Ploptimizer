
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

2. Set the **text height small enough that the whole row fits your machine
   plate** — and note the value you chose. EngraveLab silently compresses
   the *engraved toolpath* to fit the plate, so a 1.0-inch row on a small
   machine would come out squashed. A good starting point is **0.05 inch**
   on a typical 12" x 12" machine; use something smaller for wider fonts or
   bigger character counts. The extractor scales every glyph back up to a
   uniform 1.0-inch design height using the height recorded in the file
   name (Step 3), so any height works as long as it fits without
   compression. Remove any horizontal or vertical compression / stretch
   settings.

   > **Crisper glyphs:** the extractor's upscale multiplies everything —
   > including EngraveLab's per-stroke jitter at small sizes. Engraving at
   > 0.05 inch means a 20x upscale; engraving at 0.2 inch means only 5x, so
   > the same jitter lands 4x smaller in the final font. For the crispest
   > results, use the **largest height that still fits your plate without
   > compression** (e.g. 0.1" – 0.2" where the plate allows), and put that
   > number in the file name.

3. Paste the contents of [`ascii.txt`](ascii.txt) as the text: every printable
   ASCII character (`!` through `~`) in one long row, separated by many spaces.
   The wide spacing is what lets the extractor tell characters apart — do not
   reduce it. Characters must appear left-to-right in exactly `ascii.txt`
   order. If your software wraps it onto multiple lines, split it into two
   documents (e.g. `!`–`[` and `]`–`~`) and extract each with `--font-name`
   + merge (see [Tips](#tips-and-troubleshooting)).

4. Select the desired font. Any single-line (stroke) font works; script or
   multi-stroke fonts are fine too — each character's strokes are kept together.

## Step 2 — Engrave to PLT

"Engrave" (plot) the document to a PLT file using your usual plotter post.
The row must fit the plate at the chosen text height — if EngraveLab still
compresses the output (glyphs come out squashed / the median glyph height
logged in Step 4 doesn't match your chosen height), lower the text height
and re-engrave.

## Step 3 — Copy the PLT into `Fonts/PLT/`

Save/copy the file as:

```
Fonts/PLT/<font name> <text height>.plt
```

where `<text height>` is the text height you set in Step 1, in inches:

```
Fonts/PLT/dino 0.05.plt     →  font "Dino",     engraved at 0.05 in
Fonts/PLT/heavy eng 0.25.plt →  font "Heavy Eng", engraved at 0.25 in
```

The font-name part becomes the JSON key, title-cased (`dino 0.05.plt` →
`"Dino"`). The extractor scales every glyph by `1 / text height` (e.g. 20x
for `0.05`), so all fonts land in `plt_fonts.json` at a uniform 1.0-inch
design height no matter how small they were engraved. One font per file,
one full ASCII row per file.

> **Note:** files in `Fonts/PLT/` must each contain the *full*
> `ascii.txt` row for one font. Word engravings or partial samples cannot be
> mapped to the character list and will be rejected with a clear error.

## Step 4 — Run the extractor

```bash
uv run python Fonts/extract_plt_fonts.py
```

The script:

1. Parses each `Fonts/PLT/*.plt` with the core PLT parser.
2. Clusters stroke paths along X (the wide inter-character spacing separates
   characters even though EngraveLab emits strokes in scrambled order).
3. Translates every glyph so it is centered on the origin (Y keeps the
   plotter's native down-positive convention), then scales it by
   `1 / <text height>` to a uniform 1.0-inch design height.
4. Creates/updates [`plt_fonts.json`](plt_fonts.json):

```json
{
  "Dino": {
    "!": "PU-0.300,4990.000;PD-0.300,-4990.000;PU0.000,9980.000;PD0.000,9600.000;",
    "\"": "PU-1200.000,6000.000;PD-1200.000,10000.000;PU1200.000,6000.000;PD1200.000,10000.000;",
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
| `--font-name NAME` | Explicit font key (requires exactly one input file; the height still comes from the file name). |
| `--cluster-threshold N` | Manual X-gap split distance in plotter units. Only if auto-calibration fails. |
| `--rebuild` | Regenerate the JSON from scratch, dropping fonts whose `.plt` is gone. |

## Tips and troubleshooting

- **"...file name must be '<font name> <text height>.plt'".** The trailing
  space-separated token of the file name (before `.plt`) must be the engraved
  text height in inches, e.g. `dino 0.05.plt`. Fonts whose names genuinely
  end in a number need care: the last token is always read as the height.
- **"Clustering produced N glyph groups but the character list has 94."**
  The sheet doesn't contain exactly one widely-spaced copy of every character
  in order. Check: full `ascii.txt` pasted? Single row (not wrapped)?
  Text Compose (no compression)? If the row legitimately wraps, engrave two
  documents and extract each with `--font-name` into the same font key using
  two runs plus a manual merge, or split `ascii.txt` accordingly.
- **"Found only N separated stroke groups."** The characters are touching or
  nearly touching — the spacing is too small (or the text height is so small
  the spacing rounded away). Re-engrave with many more spaces between
  characters, or a slightly larger text height that still fits the plate.
- **Median glyph height far from 1.0 in after scaling.** The height in the
  file name doesn't match what was engraved, or EngraveLab compressed the
  toolpath anyway (row wider than the plate). Lower the text height,
  re-engrave, and fix the file name.
- **Glyphs look rough or jittery in the extracted font.** The engraved text
  height was very small, so the upscale factor (e.g. 20x at 0.05 inch)
  amplified EngraveLab's engraving jitter along with the geometry. Nothing
  is broken — re-engrave at the largest height that still fits the plate
  (see Step 1) to shrink the amplification.
- **A character logs a degenerate/no-geometry WARNING.** Some fonts draw
  certain characters with zero width (e.g. a dot-less style) — harmless; the
  entry is kept as an empty string and renders as blank.
- **Two files map to the same font key** (e.g. `DINO.plt` and `dino.plt`):
  the last one processed wins. Use distinct file names or `--font-name`.
- The space character is intentionally **not** in `plt_fonts.json` (nothing to
  cut); the downstream placement step handles character advance.
