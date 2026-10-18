# AGENTS.md - System Tray UI (`plt_optimizer/ui/`)

Parent contract: [`../../AGENTS.md`](../../AGENTS.md). The Windows 10+ system
tray app + Tkinter settings dialog, entered through `run_tray.py` (repo root,
console script `plt-optimizer-tray`, PyInstaller `Ploptimizer.spec`).
**Windows 7 machines run the headless CLI `watch` daemon instead** (Task
Scheduler startup, see `docs/README_DEV.md`) - this package is not part of the
Win7 runtime, and `main.py`'s CLI path never imports it.

## `tray.py` - `TrayIconManager`

- Platform split (the whole design): **Windows uses `infi.systray`**
  (pywin32-based, runs entirely in its own thread) because pystray and tkinter
  both want the Windows main-thread message loop; **Linux/macOS use
  `pystray` + Pillow**. Tray deps are optional extras
  (`uv add 'plt-optimizer[tray]'`); `_check_dependencies()` /
  `_safe_find_spec()` produce install hints, and `_safe_find_spec` exists
  because `find_spec("infi.systray")` raises on the dotted name.
- The watcher runs in a background thread via
  `cli/watch.py::run_watcher_from_config` (stop-event aware) - the GUI owns
  the main thread. Icon path resolution: `get_icon_path_frozen()` (PyInstaller
  `sys._MEIPASS`) vs `get_icon_path_dev()`.

## `settings.py` - `SettingsWindow`

- Tkinter dialog over the **user-level `utils/config.py`** config (NOT
  `job-config.json`): watch/output/log/processed dirs, `fast_mode`,
  `debug_save_files`, ensemble timeout, and the Windows-only
  **run-at-startup** checkbox.
- Saving applies `utils/startup.py::create_shortcut()/remove_shortcut()`
  immediately (both idempotent - `run_tray.py` also reconciles the delta after
  `show()` returns), then invokes the save callback.

## Invariants & tests

- Tkinter/`infi`/`pystray` imports stay inside this package (plus lazy
  `run_tray.py`) so the headless CLI stays import-light.
- `tests/test_settings.py` needs the `conftest.py` Tkinter mocks
  (MockStringVar/MockBooleanVar, MockDoubleVar for float fields) - see
  [`../../tests/AGENTS.md`](../../tests/AGENTS.md).
- `tests/test_tray.py` has many Windows-only skips and macOS blocking skips -
  green runs on macOS show those SKIPPED lines by design.
