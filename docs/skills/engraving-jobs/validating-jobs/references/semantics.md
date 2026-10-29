# Validation Semantics

Curated prose: what each gate actually covers, how the layers compose, and the
failure modes each tier exists to catch. The tables in `SKILL.md` are the
command surface; this file is the reasoning.

## The three input files, and who gates them

| File | Gate | Loaded by |
|---|---|---|
| `job-config.json` | `JobDefaults` (Pydantic, `additionalProperties: false`) | `plt_optimizer/generate/job_config.py` |
| `<job>.yaml` | `JobSpec` (Pydantic, forbids unknown keys at every tier) | `plt_optimizer/generate/schema.py` |
| `tools.json` | none in the library — read as a raw mapping | `plt_optimizer/cli/generate.py` |

The two helpers wrap exactly these gates, in this order: config → spec →
inventory. That ordering is not cosmetic.

## Why the config is validated first

A config defect is applied *before* the spec is validated, so the pipeline
reports it as a spec error. Two concrete shapes:

- **A typo'd config key** is rejected by the config's own
  `additionalProperties: false` contract, with the offending key named — the
  earliest possible point.
- **A config font that does not exist** used to pass the config gate and then
  fail inside `JobSpec` validation, so the error pointed at the *spec*'s `font`
  field even though the spec never mentioned a font. `JobDefaults` now
  canonicalizes `font` with the same registry rule the spec tiers use
  (`normalize_font_name`), so the config file is blamed, correctly.

The general rule: **the config layer is shop-wide.** One bad value in
`job-config.json` breaks every job that does not override it, and each
breakage is reported per-spec. Validate the config once, standalone, before
touching any spec.

## The `tool_options` metadata contract

`tool_options` is the one config field deliberately typed as a free-form
mapping: its keys are engraver option names (`cutting_velocity`,
`dwell_time`, …), not schema fields, so the schema cannot enumerate them. That
made the *values* unchecked, and each unchecked shape had a distinct, bad
consequence in the header generator:

| Malformed entry | Consequence without the gate |
|---|---|
| entry is not an object | `AttributeError` raised mid-export, after rendering |
| `command` missing or empty | a header with no command prefix (`1.00;`) is written into the PLT — corrupt output on the material |
| `type` not `bool`/`int`/`float` | the option is silently dropped; only a WARNING in the log |
| `min` > `max` | every value clamps to `min`, producing a nonsense header |
| `min`/`max` absent on a numeric | the clamp range is undefined |
| dual `default` missing a layer | that PLT type gets no header for the option |

The gate validates the metadata on `JobDefaults`, so `load_job_config` rejects
all of it at once: the message lists **every** problem in the file, keyed by
option name, rather than the first one. `plt_optimizer/generate/tool_options.py`
itself is unchanged — once the metadata is known-good, its clamping and
formatting are total.

Keys must be snake_case because they are the lookup names a job spec uses in
its own `tool_options` mapping.

## What the spec gate covers (and what a JSON Schema validator does not)

`job_spec.schema.json` is generated from the same models, so a JSON-Schema
validator over it reproduces the **structural** layer: types, enums, ranges,
required keys, unknown-key rejection. It cannot reproduce the model's
cross-field validators, which is where most real authoring errors land:

- exactly one label source per job (`labels`, root `content`, or a
  `replacement_text_file`);
- a root-level `content` job must declare `width` and `height`;
- a job-level `replacement_text_file` synthesizes labels from job-level
  attributes, so those attributes must be complete;
- a content entry carries exactly one form (text, itemize, enumerate);
- the required-when-unconfigured gate, which needs the config in scope.

So: structural checks are a subset of `parse_yaml`. Use `parse_yaml` (via the
helper) as the single gate; the JSON Schema is for tooling that cannot import
the package.

## The required-when-unconfigured rule, from the config side

With a config in play, `max_h_compress`, `hole_margin`, `min_hole_margin` and
`hole_text_collision_distance` lose their silent in-code fallbacks: each must
come from the config, the job level of the spec, **or** every label. A config
that omits them is legal — it just moves the burden onto every spec written
against it.

`plate_width` / `plate_height` are enforced separately and are **config-only**
(they are not job-spec fields at all): a spec with no `plates:` packs in
unbounded mode onto config-sized default sheets, and without both dimensions
there is no sheet to pack onto.

This is why the config helper warns about them. The pipeline can only report
the omission when a specific spec trips over it; the helper tells the shop
operator up front, from the config alone.

## Pre-flight checks the parser cannot make

`parse_yaml` validates content, not the filesystem. Two checks therefore live
in the helpers:

- **Data-file existence.** `replacement_text_file` is resolved relative to the
  **spec file's directory** (the pipeline's own rule, shared with
  `plt_optimizer/generate/substitution.py`), at every tier: job, plate, label.
  A renamed batch data file is a hard failure at expansion time, and it is the
  single most common breakage in the shipped examples — the helper catches it
  before a run.
- **Cutter inventory shape.** `tools.json` has no schema; the pipeline reads
  `available_cutters` and `boundary_hole_cutter_size` from a raw mapping. The
  helper checks presence, JSON syntax, and that both are positive numbers.
  Absent inventory is a warning, not an error: the run then uses ideal cutter
  sizes that may not match the physical tooling.

## Exit-code contract

`0` clean (warnings allowed) · `1` validation errors · `2` the file is unusable
(absent, unreadable, malformed JSON/YAML).

The `1`/`2` split is deliberate: `2` means "you pointed at the wrong file",
`1` means "the file needs editing". `--strict` promotes warnings to errors,
which is what a CI gate on the shop config should use.

## Windows 7 constraint

The helpers import `plt_optimizer` and the standard library only. `jsonschema`
is a `dev` extra and is **not** installed on the plotter PC, so neither helper
may depend on it; the Pydantic models are the validator.

## The mirror contract: repo `scripts/` -> `validating-jobs/scripts/`

The two helpers are authored once, in the tool repository's top-level
`scripts/` directory, where the CI suite (`tests/test_validate_job_config.py`,
`tests/test_validate_job_spec.py`) exercises them. `generate_skills.py` copies
both files byte-for-byte into this skill's `scripts/` directory and marks the
copies executable, exactly as it copies `examples/job_specs/` into
`recipes/examples/`. `--check` (and the skills drift test) fails when a mirror
and its source differ, so the skill can never document a script that diverged
from the tested one.

Two consequences of the byte-identical rule:

- **The scripts are location-independent.** They cannot assume they sit one
  level below a repo root, so each one bootstraps its own import path: use an
  already-importable `plt_optimizer` (installed package, `uv run` inside a
  checkout, `PYTHONPATH`), else walk up from the script's own file looking for
  a checkout root (`pyproject.toml` + `plt_optimizer/__init__.py`), else exit
  `2` with a message naming both fixes. The same file therefore runs from
  `repo/scripts/`, from this skill's `scripts/` inside a checkout, and from an
  install directory that has no checkout at all.
- **Defaults follow the cwd, not the script.** `--job-config` / `--tools` /
  `--config` default to `job-config.json` / `tools.json` in the current
  directory — the same rule `plt-optimizer generate` uses — because a skill
  install has no repo root to point at. "Run it beside your files" is the
  contract; an explicit path always wins.

The source of truth stays in the repo: the mirror is a distribution artifact
for the agent, not a second implementation. Editing a helper means editing
`scripts/validate_*.py` and regenerating the skill tree.
