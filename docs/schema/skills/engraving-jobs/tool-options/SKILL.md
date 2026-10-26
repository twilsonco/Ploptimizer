---
name: tool-options
description: 'Engraver velocity, spindle, dwell and Z via tool_options. Use when: setting cut speed, spindle rpm, dwell, Z clearance; text and borders need different engraver settings; a tool option value gets clamped. Keywords: tool_options, cutting_velocity, spindle_speed, dwell_time, z_clearance, hpgl, header.'
version: 1.0.0
metadata:
  hermes:
    category: engraving-jobs
    tags: [plt, engraving, toolpath, generate]
---

# Tool Options (HPGL Header Commands)

## When to use

- setting cut speed, spindle rpm, dwell, Z clearance
- text and borders need different engraver settings
- a tool option value gets clamped

## Fields

| Field | Type | Default | Constraints | Tiers | Description |
|---|---|---|---|---|---|
| `tool_options` | ? \| null | null (unset) | — | job | Optional engraver tool options (header commands). Keys are underscored header names (e.g., `cutting_velocity`, `dwell_time`, `spindle_speed`). Values override job-config.json defaults for those parameters. Supported options and their ranges are defined in job-config.json `tool_options` metadata, which includes `command`, `type`, `units`, `default` (with dual text/borders_holes values), and for numeric types `min`/`max` bounds. Omitted options use job-config defaults or are omitted from the PLT if the default is `null`. Out-of-bounds values are clamped to the configured range with a WARNING logged. |

## Available options

<!-- BEGIN TOOL OPTIONS (live from job-config.json) -->


These options control HPGL header commands prepended to PLT files.
Each can be set at the job level via `tool_options` dict in the job YAML;
defaults come from `job-config.json` with separate values for text vs
borders/holes layers. All numeric options are clamped to their configured
bounds with a WARNING logged if clamped.

| Key | Command | Type | Units | Text Default | Borders/Holes Default |
|---|---|---|---|---|---|
| `cutting_velocity` | `VS` | float | in/sec [0.05–3.0] | 1.5 | 0.8 |
| `dwell_time` | `ZO124,` | int | milliseconds [10–1000] | 50 | 50 |
| `plunge_velocity` | `VZ` | float | in/sec [0.1–3.0] | 2.0 | 2.0 |
| `proximity` | `ZO104,` | bool | on/off | True | True |
| `spindle` | `ZO123,` | bool | on/off | True | True |
| `spindle_speed` | `ZO100,` | int | rpm [3000–18000] | 12000 | 12000 |
| `vacuum` | `ZO102,` | bool | on/off | True | True |
| `z_clearance` | `ZU` | float | in [0.05–1.0] | 0.2 | 0.2 |
| `z_up_velocity` | `ZO105,` | int | milliseconds [100–3000] | 1000 | 1000 |

<!-- END TOOL OPTIONS -->

## Example

Faster cut and higher spindle, at the job level.

```yaml
job:
  job_name: "Custom Speeds"
  tool_options:
    cutting_velocity: 1.5
    spindle_speed: 15000
  content:
    - text: "FAST CUT"
```

## Pitfalls

- `tool_options` is the one intentional exception to strict mode: its keys are engraver option names, not schema fields. A key the shop `job-config.json` does not define is SILENTLY IGNORED (no header, no error), so spell options exactly as the config defines them.
- Each option carries DUAL defaults (text vs borders_holes) from `job-config.json`; setting one key in the spec applies to both layers.
- Out-of-range values are clamped to the configured range with a WARNING — the job still engraves, at the clamped setting.
- The values become HPGL header commands prepended to every generated PLT file, so they affect the whole file, not one label.

## Read next

- `references/semantics.md` — the full semantics for this topic: mechanisms, thresholds, tier interactions and worked examples the tables above cannot express. Read it before authoring anything non-trivial in this area.
- `references/snippets.yaml` — more YAML fragments for this topic.
- [`../../../JOB_SPEC.md`](../../../JOB_SPEC.md) — exhaustive field/enum/default reference for the whole spec.
- [`../../../job_spec.schema.json`](../../../job_spec.schema.json) — machine-readable schema for structural validation.
- Related skills: [cutters](../cutters/SKILL.md); [cascade-and-config](../cascade-and-config/SKILL.md); [running-and-troubleshooting](../running-and-troubleshooting/SKILL.md).
