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

- `PhysicalStrokeLayer(segments, width_inches, alpha=1.0, label="")` - one
  width-tagged layer (plotter-unit segments, cutter diameter in inches).
  Text ink: `alpha=1.0`; border/hole underlays: `PHYSICAL_STRUCTURAL_ALPHA`
  (= `SIMPLE_STRUCTURAL_ALPHA`, 0.3) keeps them visually distinct.
- `stroke_geometry.expand_cutting_polygons(segments, width_inches)` is the
  pure-geometry core (shapely `buffer`, round caps/joins, `unary_union`):
  contiguous pen-down runs join into ribbons, arcs flatten via the same
  32-segment sampling, lone points engrave round dots, rapids/zero-width
  yield nothing. It converts plotter units -> inches itself and imports NO
  matplotlib (Win7-safe module; usable from headless code).
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
