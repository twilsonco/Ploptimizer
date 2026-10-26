# Job Forms — semantics

The schema validates each form in isolation; what it cannot express is the
**exactly-one-label-source** rule and how each form synthesizes its labels.
A job is a single top-level `job:` mapping; its label source is exactly one
of the five forms below. Mixing sources is a validation error at parse time.

## The five forms

### 1. Explicit `labels` list

Every entry of `labels` is a full `LabelSpec` (`id` required, unique). Each
label's `count` replicates it (`label_1` → `label_1_0000`, `label_1_0001`,
… when `count > 1`; ids stay plain when `count` is 1/omitted).

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

### 2. Root-level single label

Root `content` (+ optional root `count`) defines one label repeated. Root
`width`, `height`, `text_height` etc. are the label's own attributes — there
is no label dict to put them in. `labels` must be absent.

```yaml
job:
  job_name: "Simple Labels"
  count: 20
  content:
    - text: "Single repeating label"
```

### 3. Label-level replacement file

One entry of `labels` carries `replacement_text_file` instead of (or
alongside, as an attribute template) `content`. Each data line of the file
becomes one label copy: `badge` → `badge_0000`, `badge_0001`, … `count` must
not be set. See the replacement-text skill for file semantics.

### 4. Job-level replacement file

Root `replacement_text_file` replaces `labels` entirely: every data line
becomes a copy (`label_0000`, …). Root `content` is the shared attribute
template. Job-level `width`, `height`, `text_height` are **required** — the
synthesized labels carry no label-level dimensions of their own. `labels`
and `count` are rejected.

```yaml
job:
  job_name: "Batch 03"
  width: 3.0
  height: 1.0
  text_height: 0.75
  holes:
    - location: sides
  replacement_text_file: data.txt
```

### 5. Plate-level replacement files

Each plate's `replacement_text_file` synthesizes labels **pinned to that
plate** (`<plate_id>_0000`, …). Pinned labels pack exclusively onto their
plate, and that plate accepts no other labels. Job-level
`width`/`height`/`text_height` are required whenever any plate declares a
file. Mutually exclusive with a job-level file. Static `labels` may coexist
and pack normally onto the unpinned plates.

```yaml
job:
  job_name: "Batch 04"
  width: 3.0
  height: 1.0
  text_height: 0.75
  plates:
    - id: scrap_a
      width: 24.0
      height: 16.0
      replacement_text_file: data_a.txt
    - id: full_b
      width: 24.0
      height: 16.0
  labels:
    - id: header
      content:
        - text: "HEADER"
```

## Rejection matrix (what fails, and why)

| Combination | Result | Why |
|---|---|---|
| `labels` + root `content` | error | two label sources |
| `labels` + job `replacement_text_file` | error | two label sources |
| root `content` + job `replacement_text_file` | allowed | file is the source; `content` is its attribute template |
| `count` + job `replacement_text_file` | error | the file's lines ARE the copies |
| label `count` + label `replacement_text_file` | error | same rule at label tier |
| job `replacement_text_file` + any plate `replacement_text_file` | error | job-level file replaces `labels` entirely; plate pinning has nothing to pin |
| plate `replacement_text_file` without job `width`/`height`/`text_height` | error | synthesized labels have no dimensions of their own |

## Identity and output naming

`id` (label) and `job_name` (job) drive output organization; ids must be
unique within the job and filesystem-safe. Replacement copies get the
`_NNNN` suffix automatically — never author it yourself.

## Where the forms expand

Expansion (`expand_job_spec`) runs between `parse_yaml` and
`resolve_job_spec`: every replacement-file line flattens into a static
`LabelSpec(count=1)` (pinned to its plate in form 5). Downstream stages only
ever see static labels, which is why form-specific behavior (template
inheritance, pinning) must be understood at authoring time, not at render.
