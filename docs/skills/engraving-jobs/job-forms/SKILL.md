---
name: job-forms
description: 'Choose the job structure: labels list, single label, or files. Use when: starting any new job spec; deciding how labels are defined; a spec fails the label-source validation. Keywords: job form, labels, content, count, single label, job_name.'
version: 1.0.0
metadata:
  hermes:
    category: engraving-jobs
    tags: [plt, engraving, toolpath, generate]
---

# Job Forms

## When to use

- starting any new job spec
- deciding how labels are defined
- a spec fails the label-source validation

## Fields

| Field | Type | Default | Constraints | Tiers | Description |
|---|---|---|---|---|---|
| `job_name` | str | **required** | — | job | Human-readable name for this job. |
| `labels` | list[LabelSpec] \| null | null (unset) | — | job | Explicit label list. Mutually exclusive with root-level content and a job-level `replacement_text_file`. |
| `count` | int \| null | null (unset) | >=1 | job | Instance count for root-level single-label jobs; not allowed with replacement files. |
| `content` | list[ContentEntry] \| null | null (unset) | — | job | Root-level content entries (single-label job) or the per-line attribute template when a replacement file is in play. |
| `id` | str | **required** | — | plate | Unique identifier for this plate specification. |
| `id` | str | **required** | — | label | Unique identifier for this label specification. |
| `count` | int | `1` | >=1 | label | Number of instances to produce (must be >= 1). |
| `content` | list[ContentEntry] \| null | null (unset) | — | label | Content entries to render: plain text lines and nested `itemize`/`enumerate` list blocks. Required unless `replacement_text_file` is provided. |
| `text` | str \| null | null (unset) | — | content entry | The text string to render. Required on a plain content entry; must be omitted on a list entry (which carries itemize or enumerate instead). |
| `text` | str | **required** | — | line | The text string to render (non-empty). |

## Example

Form 1: explicit labels list, each with its own content lines.

```yaml
job:
  job_name: "Batch 01"
  text_height: 0.5
  labels:
    - id: label_1
      count: 10
      content:
        - text: "Line 1"
        - text: "Line 2"
```

## Pitfalls

- A job defines exactly ONE label source (`labels`, root `content`, or a job-level `replacement_text_file`); mixing sources fails validation at parse time — the pipeline cannot know which one you meant.
- Root `content` + `count` is the single-repeating-label form; adding `labels` alongside it is a mixing error, not an override.
- `count` is rejected on a label (or job) that uses a replacement file — the file's data lines ARE the copies.
- `id` is required on every entry of a `labels` list and is the filename stem of the per-label output; keep ids short, unique, filesystem-safe.

## Read next

- `references/semantics.md` — the full semantics for this topic: mechanisms, thresholds, tier interactions and worked examples the tables above cannot express. Read it before authoring anything non-trivial in this area.
- `references/snippets.yaml` — more YAML fragments for this topic.
- [`../engraving-job-complete-reference/references/JOB_SPEC.md`](../engraving-job-complete-reference/references/JOB_SPEC.md) — exhaustive field/enum/default reference for the whole spec.
- [`../engraving-job-complete-reference/references/job_spec.schema.json`](../engraving-job-complete-reference/references/job_spec.schema.json) — machine-readable schema for structural validation.
- Related skills: [replacement-text](../replacement-text/SKILL.md); [cascade-and-config](../cascade-and-config/SKILL.md); [running-and-troubleshooting](../running-and-troubleshooting/SKILL.md).
