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
| `.../references/job_config.schema.json` | `JobDefaults` model (the editor-only `$schema` property is stripped) | same command |
| `.../references/JOB_CONFIG.md` | `JobDefaults` model + fallback constants + field-audit key presence and tool_options from `job-config.json` (live sections excluded from the byte-pin) | same command |
| `../docs/skills/engraving-jobs/**` (generated files: sub-skill `SKILL.md`s, `references/snippets.yaml`, `references/fallback-defaults.md`, `recipes/`, `engraving-job-complete-reference/SKILL.md`, main-skill AUTO-TOC block) | the theme registry in `generate_skills.py` + the models above | `uv run python docs/schema/generate_skills.py` (`--check` = CI drift; `--seed-semantics` = stubs only) |
| `../docs/skills/engraving-jobs/*/references/semantics.md` | hand-written semantics | **manual - curated in place, the generator never overwrites** |
| `README.md` (docs/schema) | hand-written pointer (artifact map, skill-tree layout, regen commands) | manual |

- **Drift is pinned by `tests/test_job_spec_docs.py`** (models -> artifacts)
  and **`tests/test_job_spec_skills.py`** (registry -> skill tree: `--check`
  byte-match, TOC <-> skill dirs, exact field-partition coverage, prose
  token audit, link integrity / install-referencability, YAML validity, and
  the inline-markup docs <-> `markup.TAG_REGISTRY` pin: every shipped tag
  documented, the documented registry order + spelled tag count live,
  mandatory-argument tags shown as `<name:arg>` - a new tag with NO schema
  field is invisible to the field-partition check, so registering it without
  documenting it fails here).
  Regenerate after ANY schema/job-config field change - `generate_ai_docs.py`
  FIRST (the reference skill's files), then `generate_skills.py` (it reads
  the committed `job_spec.schema.json` for its coverage check).
- **Prose markdown-safety (pinned by `tests/test_prose_markdown.py`):** all
  rendered prose - `Field(description=...)` in `schema.py`/`job_config.py`,
  `_FALLBACK_NOTES`, the Theme `core`/`use_when`/`pitfalls` registry strings,
  and the skill-tree markdown bodies - must backtick markup tags
  (`<sup>`, `<vc>...</vc>`, `<theme>`), backtick every multi-word
  schema/config field name it mentions, backtick code-shaped globs
  (`*_bh_*`, `*.ttf` - intentional `*emphasis*` is exempt), and delimit
  formulas with `$...$` (e.g. `$1 - d/2$`) or backticks, so nothing
  renders as stray HTML/italics.
  Exempt by design: frontmatter `Keywords:` lists (bare search terms), fenced
  code blocks (YAML fragments engrave tags literally), single-word field
  names (`text`, `height` - indistinguishable from English), parenthetical
  prose like `(capline - baseline)`, and error-message strings.
- The skill tree is a Hermes **category** directory
  (`docs/skills/engraving-jobs/`) of skill folders: hand-written main skill
  `generate-engraving-job/` (workflow + AUTO-TOC markers), 17 generated
  thematic sub-skills (an EXACT partition of every `job_spec.schema.json`
  field - `generate_skills.py` hard-fails on a gap/duplicate), the
  hand-written `validating-jobs/` helper skill (documents
  `scripts/validate_job_config.py` / `scripts/validate_job_spec.py`; tooling,
  not schema fields, so it is NOT a theme and is exempt from the partition.
  Its `scripts/` dir is GENERATOR-MIRRORED: `generate_skills.py` copies both
  repo scripts byte-for-byte + executable, `--check` fails on drift, and the
  repo `scripts/` stays the source of truth - the helpers locate
  `plt_optimizer` themselves (importable -> checkout walk -> exit 2 with
  install hints) and their `--job-config`/`--tools`/`--config` defaults are
  CWD-relative like `generate`, so the skill's copies run from an install dir),
  generated
  `recipes/` (complete runnable specs from `examples/job_specs/`,
  `parse_yaml`-validated, with each spec's replacement data files copied
  beside it), and `engraving-job-complete-reference/` - the
  exhaustive-reference skill whose `references/` directory holds
  `JOB_SPEC.md`, `JOB_CONFIG.md` + the two JSON Schemas (written by
  `generate_ai_docs.py`);
  every other skill links the reference through
  `../engraving-job-complete-reference/references/...`. New schema field ->
  assign it to a theme in the registry (coverage check enforces this); new
  `JobDefaults` field -> add it to its theme's tuple in
  `generate_ai_docs._CONFIG_GROUP_FIELDS` (config-only fields go in
  `_CONFIG_EXTRA_GROUPS`), else `config_group_problems()` fails (the
  registry-mirror tests pin `_CONFIG_GROUP_FIELDS` against the live themes);
  new topic -> add a `Theme` entry (+ write its `references/semantics.md`;
  the generator seeds a `TODO`-marked stub and the registry gate fails while
  any stub remains). A hand-written non-theme skill (like `validating-jobs/`)
  needs: its folder + curated `references/semantics.md`, one bullet in
  `render_toc_lines()`, and re-running the generator (the TOC must list every
  skill dir on disk). A skill that documents repo executables must ALSO list
  their basenames in `generate_skills.SKILL_SCRIPT_SOURCES` so the generator
  mirrors them into the skill's own `scripts/` (Hermes installs only files
  inside the skill dir, so a skill pointing outside itself ships commands the
  agent cannot run); mirrored scripts are named in backticks in their
  `SKILL.md` (the install-referencability test requires it).
- Hermes progressive disclosure is explicit: `references/` files load only
  when NAMED in their `SKILL.md` ("Read next"), and installs copy only
  referenced files - never unlink a generated file from its skill.
- **Config-value immunity:** the live `job-config.json` sections - the
  tool-options tables (`JOB_SPEC.md`, `JOB_CONFIG.md`, the tool-options
  skill's marked region) and `JOB_CONFIG.md`'s field-audit key-presence
  column (everything from `CONFIG_DYNAMIC_HEADING` on, excluded via
  `split_config_tool_options`) - are excluded from the byte-pins: retuning,
  adding, or removing shop-config keys never breaks tests.
  Generator prose (field descriptions, `_FALLBACK_NOTES`, theme pitfalls)
  must therefore NEVER hardcode a `job-config.json` VALUE - say "job-config
  ships the shop value" (a retune like `a30be3e`'s cutter buffers silently
  turns such a number into a lie; the drift pins cannot catch it).
- **`$schema` is editor-only:** `JobDefaults.schema_ref` (alias `$schema`,
  `exclude=True`) lets the repo `job-config.json` carry a JSON-Schema pointer
  for hover docs; `build_job_config_schema()` strips the property from the
  generated schema, and it is never injected into a job spec.
- New `str`-Enums must be added to `_ENUM_ORDER` in `generate_ai_docs.py`.
- `generate_schema_docs.py` - tool-options / font-list documentation generator
  (`--show-fonts` lists every valid font name; the skills cite it for the
  live font list).
- All units are **inches**.

## `docs/` prose

- **Component `AGENTS.md` files are routing indexes, not manuals.** Per-field
  semantics live in the `Field(description=...)` strings of `schema.py` /
  `job_config.py` (generated into `JOB_SPEC.md` + the JSON schemas), design
  derivations and measurements live in the owning module's docstring, operator
  guidance lives in the skill tree's `semantics.md` family. A component contract
  states what exists nowhere else - invariants, pipeline ordering, cross-file
  impact, test pins - and LINKS to the canonical home for everything else. When
  a section cites its module docstring, do not re-paste the derivation into the
  contract; restating a third copy is how the contracts grew to outnumber the
  code they describe.
- **No component contract hardcodes a `job-config.json` VALUE** (extending the
  generator-prose rule below to the AGENTS.md files), and no contract re-lists
  `_JOB_LAYER_FIELDS` membership or `markup.TAG_REGISTRY` membership. Both move
  with the code, and a hand-copied list is stale the moment it is written: the
  pre-cleanup `generate/AGENTS.md` carried a field list missing ~15 live config
  keys, a tag count off by three, and four shop values that had been retuned
  (it claimed `cutter_buffer_text` "ships 0.005" while the config said 0.015).
  State the code fallback; say "the shop value" for the config.
- **Component `AGENTS.md` files are routing indexes, not manuals.** Per-field
  semantics live in the `Field(description=...)` strings of `schema.py` /
  `job_config.py` (generated into `JOB_SPEC.md` + the JSON schemas), design
  derivations and measurements live in the owning module's docstring, and
  operator guidance lives in the skill tree's `semantics.md` family. A component
  contract states what exists nowhere else - invariants, pipeline ordering,
  cross-file impact, test pins - and LINKS out for everything else. When a
  section opens with "Module docstring: `x.py`", do NOT re-paste that
  docstring's derivation into the contract; a third copy of the same design
  story is how `generate/AGENTS.md` grew to 2,487 lines (171 KB) against a
  100-200 line sibling, and rot is proportional to copies, not to content.
- **No component contract hardcodes a `job-config.json` VALUE** (the rule above
  covers generator prose; this covers the AGENTS.md files), and no contract
  re-lists `_JOB_LAYER_FIELDS` membership or `markup.TAG_REGISTRY` membership.
  All three move with the code and no test pins them: the pre-cleanup
  `generate/AGENTS.md` carried a hand-copied config key list missing ~15 live
  keys, a tag count off by three ("all 11 tags" over 14 registered), and four
  retuned shop values stated as current (`cutter_buffer_text` 0.005 vs the
  config's 0.015, `cutter_buffer_routing` 0.045 vs 0.060,
  `hole_text_collision_distance` 0.1 vs 0.15, `sc_scale` 0.7 vs 0.8). State the
  CODE fallback; say "the shop value" for the config, and name the collection
  (`_JOB_LAYER_FIELDS`, `TAG_REGISTRY`) instead of enumerating it.
- `README_DEV.md` - developer guide (install, Win7 setup, usage, testing,
  troubleshooting). Points at the component AGENTS.md files for the job-spec
  schema.
- `INTEGRATION_TESTING.md` - the generate-pipeline walkthrough
  (`scripts/run_integration_test.py`).
- `pdf/` - the compiled **PDF handbook** (`plt-generation-pipeline.pdf`), a
  DERIVED artifact built by `scripts/build_pipeline_pdf.py` from the committed
  skill-tree markdown + live example runs (pandoc + xelatex + ghostscript,
  external tools). The PDF is gitignored (`docs/pdf/*.pdf`); `pdf/README.md`
  documents the build. Never hand-edit the PDF, never byte-pin it, and never
  add content the generators do not already emit - the builder renders
  `docs/skills/engraving-jobs/**` as-is, so fixing the handbook means fixing
  the source docs and rebuilding.
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
