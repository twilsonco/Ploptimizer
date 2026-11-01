# `docs/schema/` — Doc Generators (and the `docs/skills/` Production-Agent Skill Tree)

Generators and dev utilities for the YAML job specification. The generated
reference artifacts and the **hierarchical Hermes skill tree** a production
agent loads live under **`../skills/engraving-jobs/`**.

All units are **inches**. Every model is strict: unknown keys are forbidden
(`additionalProperties: false`) at **every** tier — job, label, content line,
list block/item, plate, hole — so a typo fails loudly at `parse_yaml()` with
its exact path (e.g. `labels.0.content.0.tex_height`). The free-form
`tool_options` mapping is the one intentional exception (its keys are
engraver option names, not schema fields).

## Artifacts

| Artifact | Source of truth | Regenerate? |
|---|---|---|
| `../skills/engraving-jobs/engraving-job-complete-reference/references/job_spec.schema.json` | `JobSpec` model (`plt_optimizer/generate/schema.py`) | `uv run python docs/schema/generate_ai_docs.py` |
| `.../references/job_config.schema.json` | `JobDefaults` model (`plt_optimizer/generate/job_config.py`) | same command |
| `.../references/JOB_SPEC.md` | `JobSpec` model + fallback constants scraped from `resolution.py` / `layout.py` + tool_options from `job-config.json` | same command |
| `.../references/JOB_CONFIG.md` | `JobDefaults` model + fallback constants + field-audit key presence & tool_options from `job-config.json` | same command |
| `../skills/engraving-jobs/**` (generated files) | the theme registry in `generate_skills.py` + the models above | `uv run python docs/schema/generate_skills.py` |
| `../skills/engraving-jobs/*/references/semantics.md` | hand-written semantics | **manual — curated, never regenerated** |
| `../pdf/plt-generation-pipeline.pdf` | the skill-tree markdown above + live `generate` runs of the curated showcase | `uv run python scripts/build_pipeline_pdf.py` (gitignored artifact; needs pandoc + xelatex + ghostscript) |
| `README.md` (this file) | hand-written | manual |

Drift is pinned by `tests/test_job_spec_docs.py` (models ↔ generated
artifacts) and `tests/test_job_spec_skills.py` (registry ↔ skill tree, TOC ↔
skill dirs, field-partition coverage, prose token audit, link integrity,
YAML validity). If the models change, regenerate **both** artifact sets and
commit the output; the suites fail otherwise. The sections rendered live from
`job-config.json` — the tool-options tables (`JOB_SPEC.md`, `JOB_CONFIG.md`,
and the tool-options skill) plus `JOB_CONFIG.md`'s field-audit key-presence
column — are **excluded from the byte-pins** (config-value immunity: retuning,
adding, or removing shop-config keys never breaks the tests).

## Editor hover docs for `job-config.json`

`job-config.json` starts with
`"$schema": "./docs/skills/engraving-jobs/engraving-job-complete-reference/references/job_config.schema.json"`,
which makes a JSON-aware editor (VS Code included) show every field's
description as a hover tooltip and autocomplete detail, right next to the
value. The key is accepted by the strict `JobDefaults` model as the
editor-only `schema_ref` field (`alias="$schema"`, `exclude=True`): it is
stripped from the generated schema by `build_job_config_schema()` and can
never reach cascade injection. Because the pointer is **relative to the
config file**, hover works for the in-repo config; a shop copy kept outside
the repo needs `job_config.schema.json` beside it (or an absolute `file:`
URI) to get the same tooltips.

## Rendering a PDF (optional)

The generated references are the master artifacts; a printable manual is a
one-liner with [pandoc](https://pandoc.org) (a system binary — intentionally
not a project dependency, and no PDF is committed):

```bash
uv run python docs/schema/generate_ai_docs.py
pandoc \
  docs/skills/engraving-jobs/engraving-job-complete-reference/references/JOB_SPEC.md \
  docs/skills/engraving-jobs/engraving-job-complete-reference/references/JOB_CONFIG.md \
  -o /tmp/plt-optimizer-job-reference.pdf \
  --toc --toc-depth=2 --number-sections \
  -V documentclass=report -V geometry:margin=2cm
```

## The skill tree (Hermes category, `../skills/engraving-jobs/`)

`../skills/engraving-jobs/` is a single Hermes **category** directory
of skill folders (two-level layout, agentskills.io format). Point a Hermes
profile at it with:

```yaml
# ~/.hermes/config.yaml
skills:
  external_dirs:
    - <repo>/docs/skills/engraving-jobs
```

- **`generate-engraving-job/`** — the main skill (hand-written): the
  production workflow, invariants, and the auto-generated table of contents
  (between `AUTO-TOC` markers) listing every sub-skill.
- **`validating-jobs/`** — hand-written `SKILL.md` + curated
  `references/semantics.md`: the pre-flight validation helpers, their advisory
  warnings and the exit-code contract. Not a theme: it documents tooling, not
  schema fields, so it is exempt from the field-partition requirement. Its
  `scripts/` directory is **generator-mirrored**: `generate_skills.py` copies
  the repo's `scripts/validate_job_config.py` / `scripts/validate_job_spec.py`
  in byte-for-byte (executable), so the installed skill carries the very
  executables its `SKILL.md` documents; `--check` fails on drift. The repo
  `scripts/` files stay the source of truth (the helpers locate
  `plt_optimizer` themselves, so one file runs from any directory).
- **17 thematic sub-skills** — generated: `job-forms`,
  `replacement-text`, `cascade-and-config`, `label-geometry`,
  `fonts-and-capline`, `glyph-spacing-and-kerning`,
  `line-stacking-and-reflow`, `centering-and-descender-relief`,
  `inline-markup`, `lists`, `cutters`, `bold-multistroke`,
  `compression-and-cutter-downsize`, `holes-and-collision`,
  `plates-layout-and-packing`, `tool-options`, `running-and-troubleshooting`.
- **`recipes/`** — generated: complete runnable example specs (validated
  through `parse_yaml` on every regeneration) under `examples/`.
- **`engraving-job-complete-reference/`** — generated `SKILL.md` whose
  `references/` directory holds the exhaustive artifacts (`JOB_SPEC.md`,
  `JOB_CONFIG.md` + the two JSON Schemas, written by
  `generate_ai_docs.py`). Every other
  skill links the full schema through
  `../engraving-job-complete-reference/references/...`, so installing this
  one category gives the agent the complete contract.

Each sub-skill folder contains:

| File | Owner |
|---|---|
| `SKILL.md` | generated (frontmatter, when-to-use, live field/enum/default tables, example, pitfalls, explicit Read-next) |
| `references/semantics.md` | **curated prose** — the rules/mechanisms a schema cannot express; edited in place (generator seeds a stub only when missing, never overwrites) |
| `references/snippets.yaml` | generated theme YAML fragments |
| `scripts/validate_job_*.py` | **generator-mirrored** (`validating-jobs/` only): byte-identical executable copies of the repo `scripts/` helpers, drift-pinned by `--check` |

The 17 themes are an **exact partition of every field in
`job_spec.schema.json`** (the copy in the complete-reference skill's
`references/`) — `generate_skills.py` hard-fails if a schema field
is uncovered or double-covered, so a new field can never ship undocumented.
The same partition discipline extends to the shop config:
`generate_ai_docs.py`'s `_CONFIG_GROUP_FIELDS` groups every
`job_config.schema.json` property into exactly one `JOB_CONFIG.md` section
(the theme groups, plus `_CONFIG_EXTRA_GROUPS` for the four config-only
fields and a Meta section for `description`), and
`config_group_problems()` — pinned by
`tests/test_job_spec_docs.py` / `tests/test_job_spec_skills.py` — fails on a
gap, a double-claim, or a group order that drifted from the registry.
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

**Recommended (no dependencies, all cross-field rules):** the CLI helpers,
documented by the `validating-jobs` skill (which ships executable copies in
its own `scripts/`):

```bash
uv run python scripts/validate_job_config.py           # shop defaults (job-config.json)
uv run python scripts/validate_job_spec.py my_job.yaml # spec + data files + tools.json
```

From an installed skill (outside a checkout), run the mirrored copies with any
interpreter that can import `plt_optimizer` — `python scripts/validate_job_spec.py
my_job.yaml` — beside the files being validated: the `--job-config` / `--tools`
defaults are cwd-relative, exactly like `plt-optimizer generate`.

Or in-process:

```python
from pathlib import Path

from plt_optimizer.generate.job_config import load_job_config
from plt_optimizer.generate.schema import parse_yaml

load_job_config(Path("job-config.json"))  # config gate (raises JobConfigError)
parse_yaml("my_job.yaml")                 # spec gate (+ all cross-field validators)
```

A raw JSON-Schema validator works after `yaml.safe_load` for structure only
(the schema covers types/enums/constraints, **not** the cross-field rules —
use `parse_yaml` for those; `jsonschema` is a dev extra, absent on Win7):

```python
import json, yaml, jsonschema
from pathlib import Path

schema = json.loads(
    Path(
        "docs/skills/engraving-jobs/engraving-job-complete-reference"
        "/references/job_spec.schema.json"
    ).read_text()
)
job = yaml.safe_load(Path("my_job.yaml").read_text())["job"]
jsonschema.validate(job, schema)          # structural check
```

## Regenerating

```bash
uv run python docs/schema/generate_ai_docs.py     # JOB_SPEC.md + JOB_CONFIG.md
                                                  # + JSON schemas
                                                  # -> engraving-job-complete-reference/references/
uv run python docs/schema/generate_skills.py      # skill tree + TOC (+ --check for CI)
```

Run **in this order** after any change to `schema.py`, `job_config.py`, the
fallback constants in `resolution.py`/`layout.py`, or the skill theme
registry, and commit the output — the drift tests fail otherwise
(`generate_skills.py` reads the committed `job_spec.schema.json` for its
field-partition coverage check, so it must run after `generate_ai_docs.py`
when the models changed).

## Dev utilities

- `generate_schema_docs.py` — tool-options / font-list documentation
  generator (`--show-fonts` lists every valid font name; the skill tree
  points at it for the live font list).
- `extract_headers.py` — one-shot builder of the `tool_options` metadata in
  `job-config.json` from paired PLT reference files.
