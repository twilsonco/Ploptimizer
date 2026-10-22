# AGENTS.md - System Instructions for AI Coding Assistants

## Role & Core Philosophy
You are an expert Principal Software Engineer acting as an autonomous agent in this repository. Your primary goal is to build `PLT-Optimizer`, a deterministic, cross-platform Python tool for optimizing geometric toolpaths. 

Prioritize reliability, mathematical precision, and strictly typed code over speed of delivery. Do not guess or hallucinate logic—if an implementation detail regarding HPGL/PLT parsing or Traveling Salesperson algorithms is ambiguous, stop and ask the user for clarification.

## Project Background & Motivation
Ploptimizer (package `plt_optimizer`, console script `plt-optimizer`) minimizes
tool-up rapid travel time and removes redundant overlapping strokes from HPGL/PLT
plotter files produced by Cadlink EngraveLab Expert v10 (and generated in-house).
It serves a real sign/label engraving shop: a headless `watch` daemon hot-processes
PLT files on a Windows 7 plotter PC, while the `generate` pipeline turns YAML job
specifications into production-ready per-cutter PLT files with cascading
typography, cutter compensation, drill holes, and bounds-aware bin packing across
material plates. Determinism is a product requirement: identical input must
engrave identical output, so every optimization is measured, logged, and pinned
by tests.

## 1. Coding Style & Standards
We adhere strictly to the **Ruff / Black** formatting standards and modern Python paradigms.
* **Strict Typing:** Every function, class, and method must have complete PEP 484 type hints. Run type checks via `mypy` before finalizing code.
* **Pre-commit hook:** Ensure that all code passes the configured pre-commit hooks before committing. This includes linting, formatting, and type checks.
* **Docstrings:** Use Google-style docstrings for all modules, classes, and public functions.
* **Immutability & Data Structures:** Prefer `dataclasses` (with `frozen=True` where appropriate) or `pydantic` models for internal state representation. 
* **Mathematical Precision:** Never use `==` for floating-point coordinate comparisons. Always use `math.isclose()` or `numpy.isclose()` with explicit tolerances.

## 2. Testing & Coverage
Testing is not an afterthought; it is a primary deliverable. 
* **Test-Driven Operations:** Every time a new function or logical block is written and confirmed working, you must write the corresponding unit test immediately.
* **Full Coverage Requirement:** Maintain 100% test coverage for all core parsing, writing, and optimization logic. Use `pytest` and `pytest-cov`.
* **Identity Testing:** Any changes to the `parser.py` or `writer.py` must pass the identity validation suite (ensuring `input.plt -> parse -> write -> output.plt` results in semantic equivalence).
* **Fixture Location (`tests_deps/`):** Any file read by a unit test must live in `tests_deps/` (adjacent to `tests/`), never in `examples/` or its subdirectories. `tests_deps/` is frozen: never move, rename, or edit its contents unless you are deliberately updating the fixtures together with the tests that pin them. `examples/` remains user-facing sample material that is safe to tweak.
* **Execution:** Run the test suite autonomously after modifying the codebase. Do not commit failing code.

## 3. Git Workflow & Commits
* **Conventional Commits:** All commit messages must strictly follow the Conventional Commits specification (e.g., `feat:`, `fix:`, `refactor:`, `test:`, `chore:`).
* **Commit Frequency:** Commit frequently to establish a granular history.
* **Working State Only:** You must only commit code that has passed all static type checks and unit tests. Never commit code with syntax errors or broken tests. 
* **NEVER Rewrite History:** Do not run `git filter-repo`, `git filter-branch`, `git rebase` on pushed branches, `git commit --amend` on pushed commits, or any other command that rewrites commit hashes. Rewriting makes local and remote histories unrelated (zero merge-base), breaks every downstream clone/PR, and invalidates GPG signatures. History rewrites require explicit human approval.
* **NEVER Touch Remotes:** Do not run `git remote remove/set-url` or delete remote-tracking refs. Note that `git filter-repo` removes all remotes by design — a second reason it is banned. Restoring a lost remote or force-pushing over a branch is a human decision, not an agent default.
* **No Destructive Operations Without Approval:** `git push --force`/`--force-with-lease`, `git reset --hard` on shared branches, branch/tag deletion, and garbage collection (`git gc --prune=now`) must never be executed autonomously.
* **Secrets Incident Protocol:** If a secret was accidentally committed, do NOT attempt to scrub history. Report the situation to the user and recommend rotating the credential; removal of committed secrets is a coordinated human decision (it requires a force-push that rewrites all history).

## 4. Project-Specific Invariants
* **Package Management:** Use **`uv`** exclusively. Do not use standard `pip`, `poetry`, or `conda`. Update `pyproject.toml` directly for dependency management.
* **Cross-Platform Compatibility:** The tool is developed on Linux but deployed on Windows. You must use `pathlib.Path` for all file system operations. Never use hardcoded strings with forward or backward slashes. Account for Windows `\r\n` line endings in file I/O where it impacts parsing.
* **Dual Logging Topology:** Any new operational logic must hook into the established logging structure:
  1. Standard text logging (`logging` module) utilizing `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`.
  2. CSV Metrics logging for tracking optimization deltas (distance before/after).
* **Silent Execution:** Unless logging an error or running in verbose mode, the standard path optimization loop should execute cleanly without cluttering standard output, as it will run as a headless hot-watch service.
* **HPGL Device Scale (1016 units/inch, NOT 1000):** The plotter unit is metric by definition — HPGL's default step size is 0.025 mm, so `25.4 / 0.025 = 1016` units per inch (40 units/mm). The generate pipeline emits `IN;PA;` with **no** scale command (`IP`/`SC`/`SI`), so the plotter runs at that default. Every inch↔coordinate conversion on the emission/read-back path must go through `plt_optimizer/core/units.py` (`HPGL_UNITS_PER_INCH`, `inches_to_units()`, `units_to_inches()`); never hardcode 1000 there. The pipeline previously emitted 1000 units/inch and engraved every label 1.6% undersized (a 16in label came out 15.75in) — pinned by `tests/test_phase3_export.py::TestDeviceScaleRegression` and `tests/test_units.py` (which proves the reference sheet `examples/test_rect_grid13sheet0.txt` is engraved in exact multiples of 1016). Three distinct scales exist and only the first is the device scale: (1) device plotter units = 1016; (2) the font-library design frame = 1000 per *design* inch (`LIBRARY_UNITS_PER_INCH` in `fonts/extract_plt_fonts.py`, `_DESIGN_UNITS_PER_INCH` in `plt_font_renderer.py` — a self-normalizing frame, do not "fix" it); (3) Z clearance = 1000 per inch (`Z_UNITS_PER_INCH`, proven by `examples/plt headers/z clearance *.plt`: 0.2in → `ZU200`).

## 5. Python 3.8 (Windows 7) compatibility requirement for watch directory function
* **Watch Directory Function:** Ensure that the directory watching mechanism works correctly on Python 3.8 running on Windows 7. Avoid using features introduced in later Python versions. Test the function thoroughly on the target environment to confirm compatibility.
* **Plotting and development:** Plotting and benchmarking is not necessary to run on Windows 7, so this constraint only applies to the watch directory function and core operational logic. Development tools that require newer Python versions can be used for plotting and benchmarking on other environments.

## 6. Component Map - Where the Detailed Contracts Live

This root file carries the global standards only. Each component has its own
`AGENTS.md` beside its code (auto-loaded when working in that directory; every
one of them inherits the standards above):

| Directory / file | What it is | Contract |
|---|---|---|
| `main.py` | Single CLI entry point; routes `plt-optimizer optimize \| generate \| watch` into `plt_optimizer/cli/` | [`plt_optimizer/cli/AGENTS.md`](plt_optimizer/cli/AGENTS.md) |
| `plt_optimizer/core/` | Shared HPGL pipeline: parse → profile → chunk → optimize (TSP strategies) → sweeps → merge → reassemble → write; `units.py` is the 1016 device-scale source of truth | [`plt_optimizer/core/AGENTS.md`](plt_optimizer/core/AGENTS.md) |
| `plt_optimizer/cli/` | The three subcommands + `benchmark.py`; import-light startup (Win7 constraint) | [`plt_optimizer/cli/AGENTS.md`](plt_optimizer/cli/AGENTS.md) |
| `plt_optimizer/generate/` | The YAML-spec label generation pipeline: schema, cascade resolution, typography/rendering, cutter sizing, layout, export. Start here for anything touching job YAML, fonts rendering, compression, spacing, collisions, packing, per-cutter PLT export | [`plt_optimizer/generate/AGENTS.md`](plt_optimizer/generate/AGENTS.md) |
| `plt_optimizer/generate/markup/` | The inline markup framework (`<i> <u> <b> <scale:N> <sup> <sub> <s> <sc> <f:> <c:> <oc> <vc>`) - parser, tag registry, cascade fields, per-tag geometry contracts | [`plt_optimizer/generate/markup/AGENTS.md`](plt_optimizer/generate/markup/AGENTS.md) |
| `plt_optimizer/generate/collision/` | Text-hole collision avoidance + cutter-compensated drill holes (geometry, shapes, hole placement, detection/avoidance phases) | [`plt_optimizer/generate/collision/AGENTS.md`](plt_optimizer/generate/collision/AGENTS.md) |
| `plt_optimizer/generate/packing/` | Bin-packing engines: `rectpack` layout (rotation, fill order, clearances, material) + shapely ellipse packing | [`plt_optimizer/generate/packing/AGENTS.md`](plt_optimizer/generate/packing/AGENTS.md) |
| `plt_optimizer/diagnostics/` | matplotlib path-visualization plots (import-guarded; never on the Win7 import path) | [`plt_optimizer/diagnostics/AGENTS.md`](plt_optimizer/diagnostics/AGENTS.md) |
| `plt_optimizer/ui/` | System-tray app + Tkinter settings (Windows 10+ only; Win7 runs the headless CLI) | [`plt_optimizer/ui/AGENTS.md`](plt_optimizer/ui/AGENTS.md) |
| `plt_optimizer/utils/` | Config loading, distance geometry, dual text/CSV logging, Windows startup shortcuts | [`plt_optimizer/utils/AGENTS.md`](plt_optimizer/utils/AGENTS.md) |
| `fonts/` | PLT font extraction from engraved sample sheets, derived Unicode glyphs, the v2 catalog the renderer loads | [`fonts/AGENTS.md`](fonts/AGENTS.md) |
| `tests/` + `tests_deps/` | pytest suite + frozen fixtures (stale-pin patterns, conftest mocks) | [`tests/AGENTS.md`](tests/AGENTS.md) |
| `docs/` + `docs/schema/` | Developer docs + generated `JOB_SPEC.md`/JSON schemas (regenerate via `generate_ai_docs.py`, drift-pinned) | [`docs/AGENTS.md`](docs/AGENTS.md) |
| `scripts/` | Dev/ops scripts: `run_integration_test.py` (end-to-end generate run + layout report), `kerning_report.py` | (see module docstrings) |
| `examples/` | User-facing sample PLTs + `examples/job_specs/` sample YAML (safe to tweak; never read by unit tests) | - |
| `job-config.json`, `tools.json` | Shop-level defaults + cutter inventory (hand-tuned; test pins on exact values are the known stale-pin pattern in `tests/AGENTS.md`) | [`docs/AGENTS.md`](docs/AGENTS.md) |
| `installer/`, `Ploptimizer.spec`, `run_tray.py` | Inno Setup script, PyInstaller spec, tray entry (Windows 10+ distribution) | - |

**Reading order for the generate pipeline:** root (this file) →
`plt_optimizer/generate/AGENTS.md` (schema/cascade/typography core) → the
specific sub-contract (`markup/`, `collision/`, `packing/`) → the module
docstrings. When a feature section says "pinned by `tests/...`", the fixture
contract is in `tests_deps/` (frozen, see `tests/AGENTS.md`).
