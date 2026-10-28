# AGENTS.md - Diagnostic Visualization (`plt_optimizer/diagnostics/`)

Parent contract: [`../../AGENTS.md`](../../AGENTS.md). Matplotlib-based
toolpath plotting. This package is **not part of the Windows 7 runtime**: the
plotting/benchmarking side may use modern Python/matplotlib (root §5), while
the Win7 watch/optimize path must import cleanly WITHOUT matplotlib.

## The import guard (the critical contract)

`__init__.py` wraps the `plotter` import in `try: import matplotlib ...
except ImportError: __all__ = []` - importing `plt_optimizer.diagnostics`
never raises on matplotlib-free machines. Therefore:

- **Never import `plotter` (or this package's names) at module scope from the
  CLI/core import path.** Consumers import lazily inside functions:
  `cli/watch.py` (optional plots), `generate/vectorize.py` (the opt-in
  `*_default.pdf` rapid-travel previews), `cli/benchmark.py`.
- The matplotlib-import-blocker guard in `tests/test_cli.py`
  (`TestNoMatplotlibImportPath`) pins this - see
  [`../cli/AGENTS.md`](../cli/AGENTS.md).

## `plotter.py`

- `plot_plt_document(document, ...)` - the main entry: colors cutting segments
  by cumulative distance traveled (cool -> hot), rapid air travel as gray
  dashed lines, summary text box (accepts a pre-calculated
  `rapid_travel_inches` so the plot matches the caller's metric).
  `simple_mode` = tool-down strokes only (thick black, no rapids);
  `is_structural` styles borders/holes as faint underlays;
  `gridlines=False` suppresses both grid layers (ticks kept);
  `show_axes=False` hides all axis chrome (ticks, tick/axis labels, legend,
  spines) and `show_title=False` drops the title - the pair the font
  showcases (`fonts/font_showcase.py`) use for chrome-free JPGs.
  `plot_stroke_path` / `create_path_diagram` are the smaller helpers;
  `save_figure` infers format from the extension (.pdf, .svg/.svgz, .jpg/
  .jpeg flattened onto white, everything else PNG); `PlotterError` wraps bad
  input.
- **Legend guard**: the axis legend is drawn only when the axes carry labeled
  artists (`get_legend_handles_labels()`). `simple_mode` suppresses every
  label, so it draws NO legend - calling `legend()` with nothing to show emits
  a `UserWarning` and paints an empty frame; keep it guarded.
- **`show_plot` / `_show_figure(fig)`**: `plt.show()` runs only when the active
  canvas reports an interactive framework
  (`fig.canvas.required_interactive_framework is not None`). Headless backends
  (Agg/pdf/svg/ps - every CI run, the watch/optimize plots, the generate
  previews) skip the show silently; a bare `plt.show()` there warns on
  macOS/Windows. Both `plot_plt_document` and `plot_physical_strokes` route
  `show_plot` through this helper.
- **Inch display**: `PLT_UNITS_TO_INCHES = 1 / HPGL_UNITS_PER_INCH` - the
  device scale comes from `core/units.py` (root §4, 1016 units/inch), axes and
  ticks are laid out in inches (`TICK_INTERVAL_INCHES`).
- `_arc_to_points` flattens `ArcSeg`s (32 segments) for rendering only - it
  never touches emitted toolpaths.
- `_safe_range` keeps degenerate spans from collapsing axes, using
  `math.isclose` per root §1.

## `stroke_geometry.py` + `plot_physical_strokes` (true cutter widths)

`plot_physical_strokes(layers, ...)` renders the **physical ink** of a
toolpath: every cutting stroke is expanded into a constant-width polygon
(the cutter's corridor) and filled, so each stroke measures its real cutter
diameter on the inch axes at any zoom/export scale - unlike `simple_mode`
centerline strokes whose linewidth in points is display-only. This is the
`pdf/` preview renderer of the generate pipeline
(`generate/vectorize.py::_write_simple_plots`); `simple_mode` stays for the
font showcases (`fonts/font_showcase.py`) and thin-line fallbacks.

The module is width-agnostic: it draws exactly the `width_inches` it is handed.
The generate pipeline supplies the **engraved** width (the physical cutter plus
its `cutter_buffer_text` / `cutter_buffer_routing` kerf buffer - a roughly
45-degree-flank V-bit cuts wider than its nominal diameter), not the nominal
tool, so a proof matches the material the machine removes even though the
per-cutter PLT file name in the same view quotes the nominal tool. See
`generate/AGENTS.md` "Physical vs. effective cutter diameter".

- `PhysicalStrokeLayer(segments, width_inches, alpha=1.0, label="", facecolor="black")` -
  one width-tagged layer (plotter-unit segments, cutter diameter in inches).
  Text ink: `alpha=1.0`; per-file border/hole underlays:
  `PHYSICAL_STRUCTURAL_ALPHA` (= `SIMPLE_STRUCTURAL_ALPHA`, 0.3) keeps them
  visually distinct (the colored combined proof renders every pen opaque
  with contrast outlines). `facecolor` colors the engraved ink (the colored
  `all_*.pdf` proof previews pass the resolved text/material color - see
  [`../generate/AGENTS.md`](../generate/AGENTS.md) "Colored Combined PDF
  Previews"); the default keeps every other render black.
- `PhysicalFillLayer(polygons, color, alpha=1.0, label="")` + the
  `plot_physical_strokes(..., fills=...)` parameter - enclosed-area
  background fills drawn *beneath* every stroke layer (the material
  background and the drill-hole "see-through" disc under the proof ink).
  Fills also widen the axis envelope; fills without ink render (the
  empty-plot placeholder needs both empty).
- `plot_physical_strokes(..., background_color=None)` - optional figure +
  axes facecolor for the proof page (the combined previews pass the
  configured `preview_background_color`); `None` keeps matplotlib's white.
- `stroke_geometry.expand_cutting_polygons(segments, width_inches)` is the
  pure-geometry core (shapely `buffer`, round caps/joins, `unary_union`):
  contiguous pen-down runs join into ribbons, arcs flatten via the same
  32-segment sampling, lone points engrave round dots, rapids/zero-width
  yield nothing. It converts plotter units -> inches itself and imports NO
  matplotlib (Win7-safe module; usable from headless code).
- `stroke_geometry.closed_path_polygons(segments)` is the companion for the
  proof fills: contiguous *closed* pen-down runs (first point = last point)
  become enclosed-area polygons (no cutter-width buffering; `make_valid`
  repairs self-intersecting runs; nested rings union, they do not punch
  holes). Open runs, rapids, and zero-area collapses yield nothing.
- Stroke-width provenance lives OUTSIDE this package: per-cutter file names
  carry the cutter (`_parse_plt_stem(...).cutter`), the assembled plate
  splits by pen, and `export_per_cutter_plts` builds the pen -> cutter
  `pen_widths` map. `PLTDocument` itself carries no pen/tool metadata.
- No rapids, colorbar, markers, legend, or summary box - just the filled
  ink over the fixed inch drafting grid (`gridlines`/`show_axes`/
  `show_title` behave as in `plot_plt_document`).

## Tests

`tests/test_diagnostics.py` (re-export surface), `tests/test_plotter.py`
(`TestPlotPhysicalStrokes`), `tests/test_stroke_geometry.py` (expansion
geometry). Output examples land in `examples_diag_output/`
via `examples/run_diagnostics.py` (user-facing demo).
