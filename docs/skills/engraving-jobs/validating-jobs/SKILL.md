---
name: validating-jobs
description: 'Pre-flight validation of job specs, shop configs and cutter inventories via the scripts/validate_*.py helpers. Use when: checking a job YAML before generating; editing a job-config.json; "is this spec valid"; diagnosing a config-gate failure; agent or CI pre-commit checks. Keywords: validate, job-config, parse_yaml, pre-flight, yaml check, schema, tool_options, font.'
version: 1.0.0
metadata:
  hermes:
    category: engraving-jobs
    tags: [plt, engraving, toolpath, generate, validation]
---

# Validating Jobs, Configs and Cutter Inventories

Check the inputs **before** a generation run. Both helpers are thin CLIs over
the pipeline's own gates, so they can never disagree with
`plt-optimizer generate` — and neither one renders typography, writes PLT/PDF
output, or needs the plotter attached.

Run them from the repo root (they bootstrap their own import path, so the cwd
does not matter for imports; the *defaults* are repo-root-relative).

## The two helpers

### 1. `scripts/validate_job_config.py` — the shop defaults file

```bash
uv run python scripts/validate_job_config.py                       # repo job-config.json
uv run python scripts/validate_job_config.py --config cfg.json     # a per-engraver config
uv run python scripts/validate_job_config.py --json                # machine-readable
uv run python scripts/validate_job_config.py --strict              # warnings fail the run
```

Gate: `load_job_config` → the `JobDefaults` model. Catches unknown keys (the
config forbids them, so a typo fails), ranges, enums, the font name, and the
full `tool_options` metadata contract.

Advisory warnings (config-only — no spec is involved, so the pipeline cannot
surface these until a specific job fails):

- a required-when-unconfigured field the config omits → every spec must then
  declare it, or parsing aborts;
- no default plate size → any spec without `plates:` aborts;
- `tool_options` entries missing the `name` / `description` / `units` keys the
  documentation tables render;
- a dual default whose `text` or `borders_holes` layer is explicitly null → no
  header is emitted for that PLT type.

### 2. `scripts/validate_job_spec.py` — a job YAML (plus its data file)

```bash
uv run python scripts/validate_job_spec.py my_job.yaml
uv run python scripts/validate_job_spec.py my_job.yaml --job-config cfg.json
uv run python scripts/validate_job_spec.py my_job.yaml --job-config "" --tools ""
uv run python scripts/validate_job_spec.py my_job.yaml --json
```

Gate: `parse_yaml` with the same `--job-config` the CLI uses — YAML syntax, the
`job:` root, config-default injection, the required-when-unconfigured rule, and
every model validator (the exact unknown-key path, enums, ranges, all
cross-field rules).

Extra pre-flight the parser cannot do:

- `replacement_text_file` **existence** at every tier (job, plate, label),
  resolved relative to the spec file exactly as the pipeline resolves it — a
  batch spec with a renamed data file is a real, silent production failure;
- the `tools.json` cutter inventory shape (`available_cutters` positive
  numbers, `boundary_hole_cutter_size` positive).

`--job-config` and `--tools` default to the repo files (like the generate CLI).
Pass `""` to opt out of a layer: an empty value skips it, which is how you
validate a spec that must stand on its own.

## Recommended order

1. `validate_job_config.py` — the config is shared by every job; fix it first,
   because a config error is reported per-spec and blames the spec.
2. `validate_job_spec.py` — the spec + its data files + the inventory.
3. `plt-optimizer generate my_job.yaml`.

## Exit codes (both helpers)

| Code | Meaning |
|---|---|
| `0` | valid (warnings allowed without `--strict`) |
| `1` | validation errors — fix the file |
| `2` | the file itself is unusable: absent, unreadable, or not valid JSON/YAML |

`--json` emits `{path, ok, errors, warnings, findings: [{severity, message}]}`
(plus `job_name` / `label_count` / `plate_count` for a spec that parsed).

## What validation cannot check

- **Engraving outcome.** Fit, collisions, packing and legibility are computed
  during the run; a valid spec can still pack badly. Read the layout report.
- **Font glyph coverage.** A valid font name may lack a glyph; the renderer
  decides at render time.
- **Physical tooling.** `tools.json` is checked for shape, not for the cutters
  actually in the machine.
- **A spec-side `tool_options` key absent from the config metadata** is ignored
  by the header generator (it iterates the config's options). Declare the
  option in the config to make it real.

## Pitfalls

- Validate with the **same** `--job-config` you generate with. A spec that
  passes with `--job-config ""` can fail against a config that injects
  defaults, and vice versa.
- `parse_yaml` is the authoritative gate for a spec; a JSON-Schema validator
  over `job_spec.schema.json` checks structure only and skips every cross-field
  rule. Use the helper.
- These helpers are stdlib + `plt_optimizer` on purpose: `jsonschema` is a dev
  extra and is not installed on the Windows 7 plotter PC.
- A config that "loads fine" in the pipeline and then crashes mid-export is a
  `tool_options` metadata problem — the gate reports all of them at once.

## Read next

- `references/semantics.md` — what each gate covers, the layering of config vs
  spec checks, and the failure modes each validation tier exists to catch.
- [`../generate-engraving-job/SKILL.md`](../generate-engraving-job/SKILL.md) —
  the main workflow skill and category TOC.
- [`../running-and-troubleshooting/SKILL.md`](../running-and-troubleshooting/SKILL.md)
  — the CLI, its outputs, and the error taxonomy.
- [`../cascade-and-config/SKILL.md`](../cascade-and-config/SKILL.md) — how
  config defaults inject and the required-when-unconfigured gate works.
- [`../engraving-job-complete-reference/references/job_spec.schema.json`](../engraving-job-complete-reference/references/job_spec.schema.json)
  — machine-readable spec schema.
- [`../engraving-job-complete-reference/references/job_config.schema.json`](../engraving-job-complete-reference/references/job_config.schema.json)
  — machine-readable config schema.
