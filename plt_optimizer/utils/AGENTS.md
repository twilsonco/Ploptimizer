# AGENTS.md - Shared Utilities (`plt_optimizer/utils/`)

Parent contract: [`../../AGENTS.md`](../../AGENTS.md) (coding standards, the
dual-logging topology in §4, the Python 3.8 / Windows 7 constraint in §5).
This package is the shared operational substrate: every `core/` module imports
`utils.logging`, and the `optimize`/`watch` hot path also imports
`utils.geometry`. **Everything here must stay Python 3.8 / Win7 import-safe**
(no matplotlib, no numpy, no 3.9+ syntax) and use `pathlib.Path` throughout.

## Module map

- `config.py` - the **user-level** `config.json` for the tray app and watch
  daemon. `get_config_path()`: `%LOCALAPPDATA%`-style
  `~/AppData/Local/PLT-Optimizer/config.json` on Windows,
  `~/.config/plt-optimizer/config.json` elsewhere. `DEFAULT_CONFIG` is a plain
  dict (watch/output/log/processed dirs, `fast_mode`, `debug_save_files`,
  `ensemble_timeout_seconds` 10.0, `same_row_preference` 1.0,
  `run_at_startup`, `first_run`, `structural_ratio` 0.85); `load_config()`
  merges the file over the defaults, `update_config()` patches it.
  NOT the shop-level `job-config.json` - that is the generate pipeline's
  [`../generate/AGENTS.md`](../generate/AGENTS.md) (`job_config.py`).
- `geometry.py` - pure Euclidean helpers (distance, path length, bbox,
  interpolation, point-on-segment, closed-path detection).
  `COORD_TOLERANCE = 1e-3` is the canonical coordinate tolerance -
  `core/path_merger.py` pins `DEFAULT_MERGE_TOLERANCE = COORD_TOLERANCE`.
  `fracture_linear_paths(doc)` breaks purely linear multi-segment paths into
  independent `StrokePath`s for routing while passing arc-containing paths
  (4-arc drill holes) and closed paths (ellipse boundaries) through intact -
  used by `optimize`, `watch`, and `core/pipeline.py`.
- `logging.py` - the §4 dual-logging implementation:
  1. `TextLogger` - stdlib `logging` to stdout + `logs/optimizer.log`
     (format `%(asctime)s | %(levelname)-8s | %(name)s | %(message)s`).
  2. `CSVMetricsLogger` - appends `logs/job_metrics.csv`; the 10-column
     `METRICS_HEADER` (timestamp, job_id, original_file, optimized_file,
     original/optimized_total_distance, percent_improvement, status, method,
     notes) is pinned by `tests/test_logging.py` - extend, never reorder.
  `resolve_log_level()` accepts int / level-name / numeric string, falling back
  to the `PLT_LOG_LEVEL` env var (`LOG_LEVEL_ENV_VAR`) then the default - the
  env var is how the CLI level propagates into spawned subprocesses
  (`cli/benchmark.py` sets it before spawning workers). `setup_logging()` /
  `get_text_logger()` / `get_metrics_logger()` are process singletons.
- `startup.py` - Windows Startup-folder autostart: creates/removes a
  `PLT-Optimizer.lnk` via `win32com` `WScript.Shell` (WindowStyle 7 =
  minimized). Every entry point no-ops off Windows (`create_shortcut` ->
  `None`, `is_startup_enabled` -> `False`); `_WINSHELL_AVAILABLE` guards the
  pywin32 import. Consumed by `ui/settings.py` and `run_tray.py`.

## Invariants

- Import-safe on the Win7 watch path: `cli/watch.py` imports
  `utils.logging`/`utils.geometry` at module scope - keep them dependency-light.
- Float comparisons use tolerances (`coordinates_equal`, `COORD_TOLERANCE`),
  never `==` (root §1).
- Tests: `tests/test_config.py`, `tests/test_geometry.py`,
  `tests/test_logging.py`, `tests/test_startup.py`.
