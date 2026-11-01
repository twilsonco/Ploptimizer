---
name: engraving-job-complete-reference
description: 'Exhaustive job-spec reference: every field, enum, default, schema, plus the job-config.json shop defaults. Use when: looking up an exact field, type, range, or fallback; validating a spec structurally. Keywords: reference, JOB_SPEC, JOB_CONFIG, job-config, schema, fields, enums, defaults, validation.'
version: 1.0.0
metadata:
  hermes:
    category: engraving-jobs
    tags: [plt, engraving, toolpath, generate]
---

# Engraving Job — Complete Reference

## When to use

- You need the exact type, default, constraint range, or full description of any job-spec field.
- You need the exact meaning of any `job-config.json` shop-default field (see `references/JOB_CONFIG.md`).
- You need the enum value lists (hole locations, alignments, layout modes, colors, counter styles, label shapes).
- You need the resolved fallback constants, the required-when-unconfigured gate, or the live tool-options table.
- You want a machine-readable JSON Schema to validate a spec structure against.

The full schema lives in this skill's `references/` directory. All four files are AUTO-GENERATED from the tool's Pydantic models (`schema.py` / `job_config.py`) plus the fallback constants scraped from the pipeline source, so they are always current with the installed tool version:

- `references/JOB_SPEC.md` — the dense human/AI reference: per-model field tables (type, default, constraints, description), enum value tables, resolved fallback defaults, the required-when-unconfigured set, and the live tool-options table (from `job-config.json`).
- `references/JOB_CONFIG.md` — the same treatment for the `job-config.json` shop defaults: a field audit (type, code fallback, whether the shop config sets it), per-topic field tables, the config enums, the required gate, and the live tool-options table.
- `references/job_spec.schema.json` — JSON Schema for a job YAML's `job:` mapping (usable with `jsonschema.validate`).
- `references/job_config.schema.json` — JSON Schema for `job-config.json` shop defaults.

## How to read the reference

1. Load `references/JOB_SPEC.md` and search for the field name (backticked, e.g. `` `max_h_compress` ``). Every model that declares the field has its own table row; the descriptions state the cascade tiers and behavior.
2. For a `job-config.json` key, load `references/JOB_CONFIG.md` and search the same way; its field audit table also shows each field's code fallback (what applies when the config leaves it unset).
3. For a structural validation loop, load `references/job_spec.schema.json` and validate the parsed `job:` mapping with any JSON-Schema validator. The schema is self-describing: every field carries its full description.
4. Topic semantics (mechanisms, thresholds, interactions the schema cannot express) live in the thematic sub-skills of this category — start from the main skill's TOC.

## Read next

- `references/JOB_SPEC.md` — exhaustive field/enum/default/tool-options reference.
- `references/JOB_CONFIG.md` — exhaustive shop-config field reference.
- `references/job_spec.schema.json` — machine-readable job-spec schema.
- `references/job_config.schema.json` — machine-readable shop-config schema.
- [`../generate-engraving-job/SKILL.md`](../generate-engraving-job/SKILL.md) — the main workflow skill and category TOC.
