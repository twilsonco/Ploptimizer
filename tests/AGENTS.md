# AGENTS.md - Test Suite (`tests/`)

Parent contract: [`../AGENTS.md`](../AGENTS.md) - §2 (Testing & Coverage) is the
governing standard: 100% coverage for core parsing/writing/optimization,
identity round-trips for `parser.py`/`writer.py` changes, run the suite
autonomously after modifying the codebase.

## Conventions

- 65 `test_<module_or_feature>.py` files; every module in `plt_optimizer/` plus
  CLI, UI, and integration coverage. `pytest` + `pytest-cov`.
- **Fixtures live in `tests_deps/` (frozen)** - see its README. Never move,
  rename, or edit fixture contents unless deliberately updating the fixtures
  together with the tests that pin them. `examples/` is user-facing sample
  material, never read by unit tests.
- Feature fixtures follow the `tests_deps/<feature>_job.yaml` pattern, pinned
  by a `Test<Feature>DemoExample` export test in
  `test_phase3_export.py` (the component AGENTS.md files list the exact pins).
- `conftest.py` mocks Tkinter (`MockStringVar`/`MockBooleanVar` wired into
  `TkinterModule`; add `MockDoubleVar` for float fields) - required by
  `ui/settings.py` tests.
- Win7 import constraint: CLI import structure is verified with a matplotlib
  import-blocker (`import main` and `plt-optimizer watch --help` must succeed
  without matplotlib) - see [`../plt_optimizer/cli/AGENTS.md`](../plt_optimizer/cli/AGENTS.md).
- Tests monkeypatch `plt_optimizer.generate.vectorize.export_per_cutter_plts`
  (the source module), not the CLI module attribute.

## Known failure patterns

- **Shop-tuned `job-config.json` stale pins:** `test_job_config.py::test_repo_config_fills_job_layer`
  and `test_tool_options.py::test_generate_headers_with_defaults` pin EXACT
  values from the shipped `job-config.json`, which is hand-tuned for the real
  shop. When they fail, `git log -p -- job-config.json` FIRST - a deliberate
  value change means RE-PIN THE TEST, never revert the config. (The
  `descending_character_*` asserts are isinstance-only on purpose.)
- **Demo AA-arc counts** in `test_phase3_export.py` move whenever kerning or
  arc-flattening changes - re-pin only when counts actually move.
- Generated-docs drift is pinned by `test_job_spec_docs.py` - see
  [`../docs/AGENTS.md`](../docs/AGENTS.md).
- CLI arg test template: `test_watch.py::TestParseArgsDebounceSeconds`.
- `tests_deps/test123_spec.yaml` is the default spec of
  `scripts/run_integration_test.py` and holds exactly 3 reference labels -
  consumers assume 3 labels.
