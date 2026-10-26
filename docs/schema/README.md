# `docs/schema/` — Job-Spec Artifacts and the Production Agent Skill Tree

Machine-generated reference artifacts for the YAML job specification, plus
the **hierarchical Hermes skill tree** that a production agent loads to
author job specs and run `plt-optimizer generate` autonomously.

All units are **inches**. Every model is strict: unknown keys are forbidden
(`additionalProperties: false`) at **every** tier — job, label, content line,
list block/item, plate, hole — so a typo fails loudly at `parse_yaml()` with
its exact path (e.g. `labels.0.content.0.tex_height`). The free-form
`tool_options` mapping is the one intentional exception (its keys are
engraver option names, not schema fields).

## Artifacts

| Artifact | Source of truth | Regenerate? |
|---|---|---|
| `job_spec.schema.json` | `JobSpec` model (`plt_optimizer/generate/schema.py`) | `uv run python docs/schema/generate_ai_docs.py` |
| `job_config.schema.json` | `JobDefaults` model (`plt_optimizer/generate/job_config.py`) | same command |
| `JOB_SPEC.md` | both models + fallback constants scraped from `resolution.py` / `layout.py` + tool_options from `job-config.json` | same command |
| `skills/plt-optimizer-generate/**` (generated files) | the theme registry in `generate_skills.py` + the models above | `uv run python docs/schema/generate_skills.py` |
| `skills/plt-optimizer-generate/*/references/semantics.md` | hand-written semantics | **manual — curated, never regenerated** |
| `README.md` (this file) | hand-written | manual |

Drift is pinned by `tests/test_job_spec_docs.py` (models ↔ generated
artifacts) and `tests/test_job_spec_skills.py` (registry ↔ skill tree, TOC ↔
skill dirs, field-partition coverage, prose token audit, link integrity,
YAML validity). If the models change, regenerate **both** artifact sets and
commit the output; the suites fail otherwise. The tool-options tables
(`JOB_SPEC.md` and the tool-options skill) render live from
`job-config.json` and are **excluded from the byte-pins** (config-value
immunity — retuning the shop config never breaks the tests).

## The skill tree (Hermes category)

`skills/plt-optimizer-generate/` is a single Hermes **category** directory
of skill folders (two-level layout, agentskills.io format). Point a Hermes
profile at it with:

```yaml
# ~/.hermes/config.yaml
skills:
  external_dirs:
    - <repo>/docs/schema/skills/plt-optimizer-generate
```

- **`generate-engraving-job/`** — the main skill (hand-written): the
  production workflow, invariants, and the auto-generated table of contents
  (between `AUTO-TOC` markers) listing every sub-skill.
- **17 thematic sub-skills** — generated: `job-forms`,
  `replacement-text`, `cascade-and-config`, `label-geometry`,
  `fonts-and-capline`, `glyph-spacing-and-kerning`,
  `line-stacking-and-reflow`, `centering-and-descender-relief`,
  `inline-markup`, `lists`, `cutters`, `bold-multistroke`,
  `compression-and-cutter-downsize`, `holes-and-collision`,
  `plates-layout-and-packing`, `tool-options`, `running-and-troubleshooting`.
- **`recipes/`** — generated: complete runnable example specs (validated
  through `parse_yaml` on every regeneration) under `examples/`.

Each sub-skill folder contains:

| File | Owner |
|---|---|
| `SKILL.md` | generated (frontmatter, when-to-use, live field/enum/default tables, example, pitfalls, explicit Read-next) |
| `references/semantics.md` | **curated prose** — the rules/mechanisms a schema cannot express; edited in place (generator seeds a stub only when missing, never overwrites) |
| `references/snippets.yaml` | generated theme YAML fragments |

The 17 themes are an **exact partition of every field in
`job_spec.schema.json`** — `generate_skills.py` hard-fails if a schema field
is uncovered or double-covered, so a new field can never ship undocumented.
Hermes progressive disclosure is explicit: `skill_view(name)` loads only
`SKILL.md`; reference files load on demand and are installed only when named
in their `SKILL.md` — which is why every generated file is linked from its
parent, and every sub-skill is linked from the main TOC.

## Pipeline context

```
spec.yaml ──parse_yaml()──▶ JobSpec ──expand_job_spec()──▶ JobSpec ──resolve_job_spec()──▶ ResolvedLabel[] ──▶ layout / render / export
              (+ job-config.json injection & required-field gate)   (replacement-file expansion)   (cascade resolution, cutter sizing)
```

CLI entry: `plt-optimizer generate <spec.yaml>` (flags: `--job-config`,
`--tools`, `-o`, `--fast-mode`, `--no-plots`, `--default-plots`, `-v`).
Validation entry point for tooling:
`plt_optimizer.generate.schema.parse_yaml(path)` → validated `JobSpec` or
`ValueError` (`JobConfigError` subclass for config-gate violations).

## Validating YAML against these artifacts

Any JSON-Schema validator works after `yaml.safe_load` (note: the schema
covers structure/types/enums/constraints, **not** the cross-field rules —
use `parse_yaml` for those):

```python
import json, yaml, jsonschema
from pathlib import Path

schema = json.loads(Path("docs/schema/job_spec.schema.json").read_text())
job = yaml.safe_load(Path("my_job.yaml").read_text())["job"]
jsonschema.validate(job, schema)          # structural check
# authoritative check:
from plt_optimizer.generate.schema import parse_yaml
# parse_yaml("my_job.yaml")               # + all cross-field validators
```

## Regenerating

```bash
uv run python docs/schema/generate_ai_docs.py     # JOB_SPEC.md + JSON schemas
uv run python docs/schema/generate_skills.py      # skill tree + TOC (+ --check for CI)
```

Run after any change to `schema.py`, `job_config.py`, the fallback constants
in `resolution.py`/`layout.py`, or the skill theme registry, and commit the
output — the drift tests fail otherwise.

## Dev utilities

- `generate_schema_docs.py` — tool-options / font-list documentation
  generator (`--show-fonts` lists every valid font name; the skill tree
  points at it for the live font list).
- `extract_headers.py` — one-shot builder of the `tool_options` metadata in
  `job-config.json` from paired PLT reference files.
