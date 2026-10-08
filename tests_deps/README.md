# tests_deps — frozen unit-test fixtures

Every file in this directory is read directly by the unit tests in `tests/`
(and by `run_integration_test.py`). **Do not move, rename, or edit these files**
unless you are deliberately updating the fixtures together with the tests that
pin them — the tests assert on their exact contents (identity round-trips,
classification ratios, dedupe counts, packed layouts, ...).

This directory exists so test dependencies are easy to recognize and safe to
keep:

- `tests_deps/` — frozen, machine-checked by the test suite.
- `examples/` — user-facing sample material (PLT files, job specs, scripts);
  safe to tweak, never read by unit tests.

## Contents

| File | Pinned by |
|------|-----------|
| `test123_spec.yaml` | `test_phase3_export.py`, `test_label_renderer.py`, `test_vectorize_phase3.py`, `test_label3_centering.py`; default spec of `run_integration_test.py` |
| `complex_test_job.yaml` (+ `replacement_text_complex.txt` + `replacement_text_placeholder.txt` + `replacement_text_placeholder_multi.txt`) | `test_schema.py`, `test_phase3_export.py`, `test_substitution.py` |
| `sample_spec.yaml` | `test_schema.py`; docstring examples in `plt_optimizer/generate/*` |
| `rotation_demo_job.yaml` | `test_layout.py` (rotation-required/refused regression) |
| `material_demo_job.yaml` | `test_layout.py`, `test_phase3_export.py` (material partitioning + tagged filenames regression) |
| `columns_demo_job.yaml` | `test_layout.py` (column-major fill regression) |
| `use_baseline_spacing_job.yaml` | `test_phase3_export.py` (baseline-vs-ink-box stacking regression) |
| `plt_font_demo_job.yaml` | `test_plt_font_renderer.py` (font cascade + arc-native AA regression) |
| `1-inch-square.plt` | `test_identity.py`, `test_benchmark.py` |
| `test_rect_grid13sheet0.plt` | `test_identity.py` |
| `1x3 half inch letters holes1.plt` | `test_identity.py` |
| `SFA3X611sheet1.plt` | `test_profiler.py` (best-fit-arc circle classification) |
| `2026-07-10 SW0914 1230sheet0.plt` | `test_stroke_simplifier.py` (jittered-line dedupe) |
