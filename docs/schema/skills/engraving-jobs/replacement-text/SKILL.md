---
name: replacement-text
description: 'Batch-generate one label per line of a text file. Use when: generating many labels from a data list; writing replacement_text_file specs; choosing job-, plate-, or label-level files. Keywords: replacement text, batch, data file, delimiter, comment, template, plate pinning.'
version: 1.0.0
metadata:
  hermes:
    category: engraving-jobs
    tags: [plt, engraving, toolpath, generate]
---

# Replacement Text Files

## When to use

- generating many labels from a data list
- writing replacement_text_file specs
- choosing job-, plate-, or label-level files

## Fields

| Field | Type | Default | Constraints | Tiers | Description |
|---|---|---|---|---|---|
| `replacement_text_file` | str \| null | null (unset) | — | job | Job-level EngraveLab/Vision Pro-style replacement text file. Each line produces one label built from the job-level label attributes. Mutually exclusive with `labels` and `count`. |
| `replacement_text_delimiter` | str \| null | null (unset) | — | job | Single-character delimiter separating text items within a replacement file line (default ';'). Requires a replacement file at some tier (job, plate, or label); cascades job -> plate -> label. |
| `replacement_text_file_comment_string` | str \| null | null (unset) | — | job | Comment marker for replacement file lines: a line whose whitespace-stripped text starts with this string is dropped (it produces no label copy) and reported in a per-file WARNING. Unset falls back to '# ' (`job-config.json` supplies the shop default); an explicit '' disables comment handling (every non-blank line is raw data; blank lines are still dropped). Requires a replacement file at some tier (job, plate, or label); cascades job -> plate -> label. |
| `replacement_text_file` | str \| null | null (unset) | — | plate | Path to an EngraveLab/Vision Pro-style replacement text file. Each line produces one label packed onto this plate only. |
| `replacement_text_delimiter` | str \| null | null (unset) | — | plate | Single-character delimiter separating text items within a replacement file line (default ';'). Requires a replacement file for this plate (its own, or a label-level file on a label pinned to it); cascades plate -> pinned label. |
| `replacement_text_file_comment_string` | str \| null | null (unset) | — | plate | Comment marker for replacement file lines: a line whose whitespace-stripped text starts with this string is dropped (it produces no label copy) and reported in a per-file WARNING. Unset inherits the job cascade (fallback '# '; `job-config.json` supplies the shop default); an explicit '' disables comment handling (every non-blank line is raw data; blank lines are still dropped). Same file requirement as `replacement_text_delimiter`; cascades plate -> pinned label. |
| `replacement_text_file` | str \| null | null (unset) | — | label | Path to an EngraveLab/Vision Pro-style `replacement_text_file`. Each line produces one label instance. Mutually exclusive with `count`. |
| `replacement_text_delimiter` | str \| null | null (unset) | — | label | Single-character delimiter separating text items within a replacement file line (default ';'). |
| `replacement_text_file_comment_string` | str \| null | null (unset) | — | label | Comment marker for replacement file lines: a line whose whitespace-stripped text starts with this string is dropped (it produces no label copy) and reported in a per-file WARNING. Unset inherits the plate/job cascade (fallback '# '; job-config.json supplies the shop default); an explicit '' disables comment handling (every non-blank line is raw data; blank lines are still dropped). Requires `replacement_text_file`. |
| `placeholder` | bool | `false` | — | content entry / line | Mark this line for selective replacement. When True, the line's text is replaced by the corresponding replacement-file item; its other attributes are preserved. Non-placeholder lines copy verbatim to every instance. Requires the parent label to declare `replacement_text_file`. |

## Example

Form 3: per-label file; each data line is one label copy.

```yaml
job:
  job_name: "Batch 02"
  labels:
    - id: badge
      replacement_text_file: data.txt
      replacement_text_delimiter: ";"
      content:
        - text: "PLACEHOLDER L1"
          text_height: 0.45
```

## Pitfalls

- The file path resolves relative to the YAML's directory, not the CWD — keep the .txt beside the .yaml.
- `content` on a replacement-file label is an attribute TEMPLATE (per-line heights/fonts), not text to engrave; the file's items replace it.
- More data items than template lines: the extra lines inherit label-level attributes. Fewer: the copy renders only as many lines as it has items.
- `replacement_text_file_comment_string: ""` is raw-data mode (every non-blank line engraves); the default `"# "` drops comment lines, reported as one WARNING per file.
- The delimiter must not appear inside the data itself; every data line is exactly one label copy.

## Read next

- `references/semantics.md` — the full semantics for this topic: mechanisms, thresholds, tier interactions and worked examples the tables above cannot express. Read it before authoring anything non-trivial in this area.
- `references/snippets.yaml` — more YAML fragments for this topic.
- [`../../../JOB_SPEC.md`](../../../JOB_SPEC.md) — exhaustive field/enum/default reference for the whole spec.
- [`../../../job_spec.schema.json`](../../../job_spec.schema.json) — machine-readable schema for structural validation.
- Related skills: [job-forms](../job-forms/SKILL.md); [cascade-and-config](../cascade-and-config/SKILL.md); [plates-layout-and-packing](../plates-layout-and-packing/SKILL.md).
