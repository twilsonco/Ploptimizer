# AGENTS.md - Documentation & Generated Schema Artifacts (`docs/`)

Parent contract: [`../AGENTS.md`](../AGENTS.md). Component contracts live in the
per-directory `AGENTS.md` files (root file has the component map).

## `docs/schema/` - generated artifacts

| Artifact | Source of truth | Regenerate |
|---|---|---|
| `JOB_SPEC.md` | `schema.py` + `job_config.py` models + fallback constants scraped from `resolution.py`/`layout.py` + tool_options from `job-config.json` | `uv run python docs/schema/generate_ai_docs.py` |
| `job_spec.schema.json` | `JobSpec` model | same command |
| `job_config.schema.json` | `JobDefaults` model | same command |
| `README.md` | hand-written semantics | manual |

- **Drift is pinned by `tests/test_job_spec_docs.py`:** if `schema.py` /
  `job_config.py` change without regenerating the artifacts, the suite fails.
  Regenerate after ANY schema/job-config field change.
- New `str`-Enums must be added to `_ENUM_ORDER` in `generate_ai_docs.py`.
- `generate_schema_docs.py` - tool-options / font-list documentation generator
  (`--show-fonts` lists every valid font name).
- All units are **inches**.

## `docs/` prose

- `README_DEV.md` - developer guide (install, Win7 setup, usage, testing,
  troubleshooting). Points at the component AGENTS.md files for the job-spec
  schema.
- `INTEGRATION_TESTING.md` - the generate-pipeline walkthrough
  (`scripts/run_integration_test.py`).
- `RECTPACK_KNOBS.md` - packing knob analysis (fill-order contract:
  [`../plt_optimizer/generate/packing/AGENTS.md`](../plt_optimizer/generate/packing/AGENTS.md)).
- Keep prose in sync with the component AGENTS.md files when behavior changes;
  DONE.md tracks per-feature doc updates (historical record - do not rewrite).
