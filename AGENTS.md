# AGENTS.md - System Instructions for AI Coding Assistants

## Role & Core Philosophy
You are an expert Principal Software Engineer acting as an autonomous agent in this repository. Your primary goal is to build `PLT-Optimizer`, a deterministic, cross-platform Python tool for optimizing geometric toolpaths. 

Prioritize reliability, mathematical precision, and strictly typed code over speed of delivery. Do not guess or hallucinate logic—if an implementation detail regarding HPGL/PLT parsing or Traveling Salesperson algorithms is ambiguous, stop and ask the user for clarification.

## 1. Coding Style & Standards
We adhere strictly to the **Ruff / Black** formatting standards and modern Python paradigms.
* **Strict Typing:** Every function, class, and method must have complete PEP 484 type hints. Run type checks (e.g., via `mypy` or `pyright` rules) before finalizing code.
* **Docstrings:** Use Google-style docstrings for all modules, classes, and public functions.
* **Immutability & Data Structures:** Prefer `dataclasses` (with `frozen=True` where appropriate) or `pydantic` models for internal state representation. 
* **Mathematical Precision:** Never use `==` for floating-point coordinate comparisons. Always use `math.isclose()` or `numpy.isclose()` with explicit tolerances.

## 2. Testing & Coverage
Testing is not an afterthought; it is a primary deliverable. 
* **Test-Driven Operations:** Every time a new function or logical block is written and confirmed working, you must write the corresponding unit test immediately.
* **Full Coverage Requirement:** Maintain 100% test coverage for all core parsing, writing, and optimization logic. Use `pytest` and `pytest-cov`.
* **Identity Testing:** Any changes to the `parser.py` or `writer.py` must pass the identity validation suite (ensuring `input.plt -> parse -> write -> output.plt` results in semantic equivalence).
* **Execution:** Run the test suite autonomously after modifying the codebase. Do not commit failing code.

## 3. Git Workflow & Commits
* **Conventional Commits:** All commit messages must strictly follow the Conventional Commits specification (e.g., `feat:`, `fix:`, `refactor:`, `test:`, `chore:`).
* **Commit Frequency:** Commit frequently to establish a granular history.
* **Working State Only:** You must only commit code that has passed all static type checks and unit tests. Never commit code with syntax errors or broken tests. 

## 4. Project-Specific Invariants
* **Package Management:** Use **`uv`** exclusively. Do not use standard `pip`, `poetry`, or `conda`. Update `pyproject.toml` directly for dependency management.
* **Cross-Platform Compatibility:** The tool is developed on Linux but deployed on Windows. You must use `pathlib.Path` for all file system operations. Never use hardcoded strings with forward or backward slashes. Account for Windows `\r\n` line endings in file I/O where it impacts parsing.
* **Dual Logging Topology:** Any new operational logic must hook into the established logging structure:
  1. Standard text logging (`logging` module) utilizing `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`.
  2. CSV Metrics logging for tracking optimization deltas (distance before/after).
* **Silent Execution:** Unless logging an error or running in verbose mode, the standard path optimization loop should execute cleanly without cluttering standard output, as it will run as a headless hot-watch service.

## 5. Windows 7 Compatibility Invariants — DO NOT REGRESS

Windows 7 is a documented, supported deployment target (see `README_DEV.md` → "Windows 7 Notes"). It has hard, non-negotiable constraints that must be preserved with every change. **Breaking Windows 7 support is a regression and must not happen.** This section exists because Win7 support was inadvertently lost in commits after `145b9d` and the regression was not caught by tests or CI.

### 5.1 Python Version Floor
* `pyproject.toml` MUST keep `requires-python = ">=3.8,<3.14"` (or a range whose floor is `>=3.8` and `<=3.8`-compatible).
* The `classifiers` list MUST keep `"Programming Language :: Python :: 3.8"` and `"Operating System :: Microsoft :: Windows :: Windows 7"`.
* `tool.mypy.python_version` and `tool.ruff.target-version` MUST NOT be raised above `py38` without first verifying all targeted dependency versions still resolve on Python 3.8.
* **Forbidden** without explicit user approval:
  * Raising `requires-python` to `>=3.9` or higher (Win7 cannot run Python 3.9+).
  * Removing or weakening the `3.8` classifier.
  * Introducing PEP 604 (`X | Y`) union syntax, PEP 634 (`match`/`case`), PEP 617 (new PEG parser), `zoneinfo`, `graphlib`, `removeprefix`/`removesuffix`, or any other Python 3.9+ stdlib feature without guarding it with `from __future__ import annotations` at the top of the file.

### 5.2 Dependency Constraints — Win7 + Python 3.8 Compatibility Audit
Before adding **any** new dependency (in `dependencies`, `optional-dependencies`, or `dependency-groups`), you MUST verify:

1. The dependency publishes wheels for **Python 3.8** on the **win32** platform.
2. The dependency's own `requires-python` is `<=3.8` (or the chosen upper bound is `>=3.8`).
3. The dependency does not transitively require a package that fails #1 or #2.

**Required safe constraints** (update these when bumping versions):
* `watchdog>=2.6,<5.0` — the upper bound `<5.0` is intentional; verify it before bumping.
* `pystray>=0.19.5` — verify the chosen version still supports Python 3.8 / Win7 before bumping.
* `pillow>=10.0.0` — verify the chosen version still supports Python 3.8 / Win7 before bumping.
* `winshell>=0.6` and `pywin32>=306` — Windows-only, verify Win7 wheels before bumping.

**Known Win7-incompatible packages — DO NOT ADD:**
* `infi-systray` — no Python 3.8 / Win7 wheels. (Was added in `76e8c94` and removed in the Win7 restoration commit.)
* Any package whose latest release requires Python `>=3.9` AND has no Python 3.8 wheels.
* `pystray` alternatives that depend on `tkinter`'s message loop conflict workarounds requiring Python 3.9+ stdlib.

### 5.3 Dependency Marker Hygiene
When adding optional/dev dependencies that are not universally needed, you MUST use PEP 508 environment markers so they do not break resolution on lower Python versions:
* `matplotlib>=3.9.0; python_version >= "3.9"` (already gated)
* `numpy>=1.24.0; python_version >= "3.9"` (already gated)
* Any package in `[dependency-groups] dev` that requires Python `>=3.9` MUST be gated: e.g. `pytest-cov>=7.1.0; python_version >= '3.9'`, `matplotlib>=3.10.9; python_version >= '3.10'`.

Unconditional (ungated) entries in `dependencies`, `optional-dependencies.tray`, or `dependency-groups.dev` are a **regression risk** — gate them.

### 5.4 `pyproject.toml` Required Pre-Commit Audit
Before committing any change to `pyproject.toml` or `uv.lock`, run:
```bash
uv lock --check       # ensure lock file resolves
uv sync --python 3.8  # ensure resolution succeeds on the Python 3.8 floor
```
If `uv sync --python 3.8` fails or pulls packages that cannot install on Win7, the change is a regression and must be reverted or reworked before commit.

### 5.5 Tray / GUI Stack Constraints
* `pystray` is the **only** GUI dependency allowed in `tray` extras. It must work on Python 3.8 / Win7.
* `tkinter` is part of the Python stdlib and is acceptable (it ships with the Python 3.8 Win7 installer).
* `winshell` and `pywin32` are acceptable for Windows-specific shortcut management, but their versions must be verified to install on Win7.
* **Forbidden** without explicit user approval:
  * Adding `infi-systray` or any fork/derivative of it.
  * Switching to a tray library that hard-requires Python 3.10+ features.
  * Using `tkinter.ttk` features only available in Python 3.9+.

### 5.6 CI / Test Requirements for Win7 Safety
The test suite must include (and these tests must pass on every PR):
* A unit test that imports `plt_optimizer.ui.tray` and asserts the `tray` extras resolve on Python 3.8 / Win32 markers. (Already covered by `tests/test_tray.py::TestCheckDependencies`.)
* A unit test that asserts `pyproject.toml` reports `requires-python` floor `<=3.8`. (If absent, add it.)
* CI matrix must include at least one Python 3.8 job so dependency resolution is exercised on the floor version. (Verify `.github/workflows/ci.yml` if it exists; if missing, file an issue.)

### 5.7 What To Do If You Suspect a Regression
1. Re-read this section. The rules above are derived from a real, observed regression.
2. Run `uv sync --python 3.8` in a clean venv to confirm resolution.
3. Check `uv.lock` for any newly added packages — verify each on PyPI for Win32 + Python 3.8 wheels.
4. Search for `requires-python` and `python_version >=` in `pyproject.toml` to spot any ungated bumps.
5. If still uncertain, **stop and ask the user** — per the role philosophy in §1, do not guess on platform-support questions.