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
  `is_structural` styles borders/holes as faint underlays.
  `plot_stroke_path` / `create_path_diagram` are the smaller helpers;
  `save_figure` infers format from the extension; `PlotterError` wraps bad
  input.
- **Inch display**: `PLT_UNITS_TO_INCHES = 1 / HPGL_UNITS_PER_INCH` - the
  device scale comes from `core/units.py` (root §4, 1016 units/inch), axes and
  ticks are laid out in inches (`TICK_INTERVAL_INCHES`).
- `_arc_to_points` flattens `ArcSeg`s (32 segments) for rendering only - it
  never touches emitted toolpaths.
- `_safe_range` keeps degenerate spans from collapsing axes, using
  `math.isclose` per root §1.

## Tests

`tests/test_diagnostics.py`. Output examples land in `examples_diag_output/`
via `examples/run_diagnostics.py` (user-facing demo).
