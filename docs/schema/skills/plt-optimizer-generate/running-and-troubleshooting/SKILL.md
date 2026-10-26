---
name: running-and-troubleshooting
description: 'Generate command, outputs, validation entry, error taxonomy. Use when: running plt-optimizer generate; a job exits non-zero; locating the PLT and PDF outputs; validating a spec before running. Keywords: generate, cli, parse_yaml, JobConfigError, LabelRenderError, CutterSizeError, output, pdf.'
version: 1.0.0
metadata:
  hermes:
    category: plt-optimizer-generate
    tags: [plt, engraving, toolpath, generate]
---

# Running the CLI and Fixing Failures

## When to use

- running plt-optimizer generate
- a job exits non-zero
- locating the PLT and PDF outputs
- validating a spec before running

## Fields

| Field | Type | Default | Constraints | Tiers | Description |
|---|---|---|---|---|---|

## Example

The smallest job that generates (validated by parse_yaml).

```yaml
job:
  job_name: "Any valid job"
  text_height: 0.5
  content:
    - text: "OK"
```

## Pitfalls

- Validate before running: `parse_yaml(path)` raises `ValueError` (`JobConfigError` for config-gate violations) naming the exact failing path (e.g. `labels.0.content.0.tex_height`) — fix the spec.
- Outputs land under the output dir (default: the spec's directory): `plt/*.plt` (one file per cutter, ready for the engraver) and `pdf/*.pdf` (proof previews).
- `--job-config` / `--tools` default to `job-config.json` / `tools.json` in the CWD; production runs must use the shop files (no config = silent fallbacks; no tools.json = ideal cutters that may not match the physical tooling).
- The pipeline is deterministic: identical input + config + tools engraves identical output, so only re-run after editing the spec.
- `--fast-mode` trades rapid-travel quality for speed; production jobs should use the default ensemble.

## Read next

- `references/semantics.md` — the full semantics for this topic: mechanisms, thresholds, tier interactions and worked examples the tables above cannot express. Read it before authoring anything non-trivial in this area.
- `references/snippets.yaml` — more YAML fragments for this topic.
- [`../../../JOB_SPEC.md`](../../../JOB_SPEC.md) — exhaustive field/enum/default reference for the whole spec.
- [`../../../job_spec.schema.json`](../../../job_spec.schema.json) — machine-readable schema for structural validation.
- Related skills: [cascade-and-config](../cascade-and-config/SKILL.md); [job-forms](../job-forms/SKILL.md); [plates-layout-and-packing](../plates-layout-and-packing/SKILL.md).
