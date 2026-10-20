# Font Showcases

Generated per-font preview images (one JPG per font in the library: the PLT
catalog keys of `VP-EL-fonts/catalog/` plus every `fonts/**/*.ttf` basename,
exactly `plt_optimizer.generate.font_registry.font_name_choices`). Letter
fonts show the sample block (font name, pangram, upper/lower case, digits/
symbols, derived-glyph pair); symbol fonts (catalog `symbol_font: true`,
e.g. Wingdings) show the ASCII key -> symbol mapping grid with the header
rendered in the readable default face.

**Do not hand-edit.** Regenerate after any font-library change with:

```
uv run python fonts/font_showcase.py
```

The images are chrome-free (no axes, grid, legend, or plot title) and are
committed so the library's coverage is reviewable in the repo.
