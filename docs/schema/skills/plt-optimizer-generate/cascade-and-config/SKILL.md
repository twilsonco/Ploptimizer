---
name: cascade-and-config
description: 'How null inherits, shop defaults inject, and config gates bite. Use when: a value is not taking effect; deciding which tier to set a field on; `JobConfigError` about a required field; reading the cascade precedence table. Keywords: cascade, inherit, default, job-config, tools.json, fallback, precedence.'
version: 1.0.0
metadata:
  hermes:
    category: plt-optimizer-generate
    tags: [plt, engraving, toolpath, generate]
---

# Cascade Rules, job-config.json and tools.json

## When to use

- a value is not taking effect
- deciding which tier to set a field on
- `JobConfigError` about a required field
- reading the cascade precedence table

## Fields

| Field | Type | Default | Constraints | Tiers | Description |
|---|---|---|---|---|---|

## Required-when-unconfigured (job-config gate)

With a `--job-config` in play, these must come from the config **or** the spec, else parsing aborts with `JobConfigError`:

- `hole_margin`
- `hole_text_collision_distance`
- `max_h_compress`
- `min_hole_margin`
- `plate_width` / `plate_height` (only when the job declares no plates)

## Example

Job sets defaults, label overrides, line overrides again.

```yaml
job:
  job_name: "Cascade demo"
  text_height: 0.5
  labels:
    - id: big
      text_height: 0.8
      content:
        - text: "inherits 0.8 from label"
        - text: "overrides to 0.4"
          text_height: 0.4
```

## Pitfalls

- `null`/omitted always means inherit; an explicit value (including 0.0) always wins at its level — set null to inherit, never delete-and-hope.
- `job-config.json` sits below the YAML but above the code fallbacks: shop defaults are injected at the job layer, so any label can still override them.
- With a job-config in play, the required-when-unconfigured fields must come from config OR spec; missing everywhere aborts with `JobConfigError`.
- A spec-declared `margin` (job level, or on any label) suppresses the config's `h_margin`/`v_margin` injection — the cascade checks the axis keys before `margin`.
- `tools.json` supplies the cutter inventory the ladder snaps to; without it the ideal cutters are used and the physical tooling may not match.

## Read next

- `references/semantics.md` — the full semantics for this topic: mechanisms, thresholds, tier interactions and worked examples the tables above cannot express. Read it before authoring anything non-trivial in this area.
- `references/snippets.yaml` — more YAML fragments for this topic.
- `references/fallback-defaults.md` — The complete resolved-fallback-constants table (every `DEFAULT_*`, scraped live from source).
- [`../../../JOB_SPEC.md`](../../../JOB_SPEC.md) — exhaustive field/enum/default reference for the whole spec.
- [`../../../job_spec.schema.json`](../../../job_spec.schema.json) — machine-readable schema for structural validation.
- Related skills: [job-forms](../job-forms/SKILL.md); [cutters](../cutters/SKILL.md); [label-geometry](../label-geometry/SKILL.md); [running-and-troubleshooting](../running-and-troubleshooting/SKILL.md).
