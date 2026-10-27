# AGENTS.md - Documentation & Generated Schema Artifacts (`docs/`)

Parent contract: [`../AGENTS.md`](../AGENTS.md). Component contracts live in the
per-directory `AGENTS.md` files (root file has the component map).

## `docs/schema/` (generators) + `docs/skills/` (the production-agent skill tree)

`docs/schema/` holds the generators + dev utilities; the generated artifacts
and the Hermes skill tree live under `docs/skills/engraving-jobs/`:

| Artifact | Source of truth | Regenerate |
|---|---|---|
| `../docs/skills/engraving-jobs/engraving-job-complete-reference/references/JOB_SPEC.md` | `schema.py` + `job_config.py` models + fallback constants scraped from `resolution.py`/`layout.py` + tool_options from `job-config.json` | `uv run python docs/schema/generate_ai_docs.py` |
| `.../references/job_spec.schema.json` | `JobSpec` model | same command |
| `.../references/job_config.schema.json` | `JobDefaults` model | same command |
| `../docs/skills/engraving-jobs/**` (generated files: sub-skill `SKILL.md`s, `references/snippets.yaml`, `references/fallback-defaults.md`, `recipes/`, `engraving-job-complete-reference/SKILL.md`, main-skill AUTO-TOC block) | the theme registry in `generate_skills.py` + the models above | `uv run python docs/schema/generate_skills.py` (`--check` = CI drift; `--seed-semantics` = stubs only) |
| `../docs/skills/engraving-jobs/*/references/semantics.md` | hand-written semantics | **manual - curated in place, the generator never overwrites** |
| `README.md` (docs/schema) | hand-written pointer (artifact map, skill-tree layout, regen commands) | manual |

- **Drift is pinned by `tests/test_job_spec_docs.py`** (models -> artifacts)
  and **`tests/test_job_spec_skills.py`** (registry -> skill tree: `--check`
  byte-match, TOC <-> skill dirs, exact field-partition coverage, prose
  token audit, link integrity / install-referencability, YAML validity).
  Regenerate after ANY schema/job-config field change - `generate_ai_docs.py`
  FIRST (the reference skill's files), then `generate_skills.py` (it reads
  the committed `job_spec.schema.json` for its coverage check).
- The skill tree is a Hermes **category** directory
  (`docs/skills/engraving-jobs/`) of skill folders: hand-written main skill
  `generate-engraving-job/` (workflow + AUTO-TOC markers), 17 generated
  thematic sub-skills (an EXACT partition of every `job_spec.schema.json`
  field - `generate_skills.py` hard-fails on a gap/duplicate), the
  hand-written `validating-jobs/` helper skill (documents
  `scripts/validate_job_config.py` / `scripts/validate_job_spec.py`; tooling,
  not schema fields, so it is NOT a theme and is exempt from the partition),
  generated
  `recipes/` (complete runnable specs from `examples/job_specs/`,
  `parse_yaml`-validated), and `engraving-job-complete-reference/` - the
  exhaustive-reference skill whose `references/` directory holds
  `JOB_SPEC.md` + the two JSON Schemas (written by `generate_ai_docs.py`);
  every other skill links the reference through
  `../engraving-job-complete-reference/references/...`. New schema field ->
  assign it to a theme in the registry (coverage check enforces this); new
  topic -> add a `Theme` entry (+ a curated `semantics.md`; the generator
  seeds a stub). A hand-written non-theme skill (like `validating-jobs/`)
  needs: its folder + curated `references/semantics.md`, one bullet in
  `render_toc_lines()`, and re-running the generator (the TOC must list every
  skill dir on disk).
- Hermes progressive disclosure is explicit: `references/` files load only
  when NAMED in their `SKILL.md` ("Read next"), and installs copy only
  referenced files - never unlink a generated file from its skill.
- **Config-value immunity:** the live `job-config.json` tool-options tables
  (`JOB_SPEC.md` section; the tool-options skill's marked region) are
  excluded from the byte-pins - retuning the shop config never breaks tests.
- New `str`-Enums must be added to `_ENUM_ORDER` in `generate_ai_docs.py`.
- `generate_schema_docs.py` - tool-options / font-list documentation generator
  (`--show-fonts` lists every valid font name; the skills cite it for the
  live font list).
- All units are **inches**.

## `docs/` prose

- `README_DEV.md` - developer guide (install, Win7 setup, usage, testing,
  troubleshooting). Points at the component AGENTS.md files for the job-spec
  schema.
- `INTEGRATION_TESTING.md` - the generate-pipeline walkthrough
  (`scripts/run_integration_test.py`).
- `RECTPACK_KNOBS.md` - packing knob analysis (fill-order contract:
  [`../plt_optimizer/generate/packing/AGENTS.md`](../plt_optimizer/generate/packing/AGENTS.md)).
- `PROFILING.md` - the perf loop (measure → identify → patch → re-measure →
  plot) for BOTH pipelines. Two skills carry the generate procedure:
  [`../.github/skills/generate-pipeline-profiling/SKILL.md`](../.github/skills/generate-pipeline-profiling/SKILL.md)
  owns the mechanics (`scripts/profile_generate.py` appends to the
  tracked `profiling/summary.csv` run history with `run_id`/`run_ts`
  stamps, `--fresh` resets; `scripts/plot_profiling_history.py` plots every
  metric per job across sweeps; pre-optimization baselines come from a git
  worktree - never a checkout of the main tree - stamped with the baseline
  commit's date so iteration order follows code state), and
  [`../.github/skills/generate-pipeline-optimization/SKILL.md`](../.github/skills/generate-pipeline-optimization/SKILL.md)
  runs the end-to-end cycle (one bottleneck per cycle, exact + pinned
  patches, then
  `.github/skills/generate-pipeline-optimization/scripts/iteration_report.py`
  prints the per-job before → after → % gain between two iterations).
  The optimize pipeline (`optimize` CLI / `watch` core) has its own pair:
  [`../.github/skills/optimize-pipeline-profiling/SKILL.md`](../.github/skills/optimize-pipeline-profiling/SKILL.md)
  (`scripts/profile_optimize.py` appends to the tracked
  `profiling/optimization_pipeline_summary.csv` - adjacent to the generate
  history, never mixed; the harness also records the rapid-travel quality
  KPIs) and
  [`../.github/skills/optimize-pipeline-optimization/SKILL.md`](../.github/skills/optimize-pipeline-optimization/SKILL.md)
  (same cycle; its `scripts/iteration_report.py` defaults to the optimize
  CSV).
- Keep prose in sync with the component AGENTS.md files when behavior changes;
  DONE.md tracks per-feature doc updates (historical record - do not rewrite).
