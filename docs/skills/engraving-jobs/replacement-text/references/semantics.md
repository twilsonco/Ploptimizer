# Replacement Text Files — semantics

A replacement text file turns a data list into label copies: **one data line
= one label copy**. The schema types the three fields; the rules below are
what the pipeline actually does with them.

## File format

- **Data + comments.** Every non-blank, non-comment line is one label copy.
- **Items within a line** split on `replacement_text_delimiter` — a single
  special/whitespace character (default `;`). The delimiter must not appear
  inside the data itself (it has no escape).
- **Comment lines** are lines whose whitespace-stripped text starts with
  `replacement_text_file_comment_string` (default `"# "`). `""` disables
  comment handling entirely: every non-blank line is then raw data.
- **Blank/whitespace-only lines** are dropped.
- Every dropped line (comment or blank) is reported in **one WARNING per
  file** listing line numbers + text, so a silently-empty batch is visible.
- A file left with **no data lines raises** — the job fails rather than
  engraving nothing.
- **Normalization:** CRLF/CR are normalized to LF; one trailing newline does
  not create a phantom copy; a UTF-8 BOM is stripped; items are used
  **verbatim** (no whitespace stripping — pad deliberately, not by accident).

## The attribute template

`content` on a replacement-file label is **not text to engrave** — it is a
per-line attribute template. Line *i* of the template supplies the
`text_height`, `font`, `cutter_size`, … for item *i*; the file's item replaces
the placeholder text.

- **Fewer items than template lines** → the copy renders only as many lines
  as it has items (the block stays vertically centered).
- **More items than template lines** → the extra lines inherit label-level
  attributes.
- **No `content` at all** → every rendered line inherits label-level
  attributes.

```yaml
labels:
  - id: badge
    replacement_text_file: data.txt
    content:
      - text: "PLACEHOLDER L1"   # line 1 attributes
        text_height: 0.45
      - text: "PLACEHOLDER L2"   # line 2 attributes
        text_height: 0.25
```

## Selective replacement (`placeholder`)

`placeholder` is a per-line boolean. When `true`, that line's text is
replaced by the corresponding replacement-file item (its other attributes are
preserved); lines left `false` **copy verbatim to every instance** — the
fixed-header + varying-serial pattern. A `placeholder: true` line requires
the parent label to declare `replacement_text_file`.

```yaml
labels:
  - id: tag
    replacement_text_file: serials.txt
    content:
      - text: "COOLSYS"             # verbatim on every copy
      - text: "SERIAL"
        placeholder: true           # replaced by the file's item
        text_height: 0.3
```

## Delimiter / comment cascade

Both fields cascade **label → plate (pinned) → job → config → default**
(`;` / `"# "`). Each tier has a guard:

- a job-level value requires a replacement file somewhere in the job (job,
  plate, or label);
- a plate-level value requires a file on that plate or on a label pinned to
  it;
- a label-level value requires the label's own file.

A value declared with no file to govern is rejected — the schema will not
accept a delimiter that can never apply.

## Where the file lives

`replacement_text_file` resolves **relative to the YAML's directory**, not
the process CWD. Keep the `.txt` beside the `.yaml`.

## Expansion

`expand_job_spec` (between `parse_yaml` and `resolve_job_spec`) flattens each
data line into a static `LabelSpec(count=1)` with a `_NNNN` id suffix —
`badge_0000`, `badge_0001`, … (job-level file → `label_0000`, …; plate-level →
`<plate_id>_0000`, …, pinned to that plate). After expansion the rest of the
pipeline never knows a file was involved.
