---
name: lists
description: 'itemize/enumerate content blocks with markers and hanging indent. Use when: engraving a bullet list; numbering items (1. 2. 3. / a. b. c. / roman); tuning marker gap or counter style. Keywords: itemize, enumerate, bullet, counter, list, marker, hanging indent.'
version: 1.0.0
metadata:
  hermes:
    category: plt-optimizer-generate
    tags: [plt, engraving, toolpath, generate]
---

# Bullet and Enumerated Lists

## When to use

- engraving a bullet list
- numbering items (1. 2. 3. / a. b. c. / roman)
- tuning marker gap or counter style

## Fields

| Field | Type | Default | Constraints | Tiers | Description |
|---|---|---|---|---|---|
| `bullet_string` | str \| null | null (unset) | — | job / label / content entry / line / list block | Character(s) used for each bullet of an itemize list. Cascades item -> itemize block -> label -> job (fallback '•' -- the derived bullet glyph, engraved by every PLT catalog font except the symbol fonts); an explicit empty string '' engraves no marker (an indent-only list). Settable on a list item to override one bullet. |
| `counter_start_index` | int \| null | null (unset) | >=1 | job / label / content entry / line / list block | Starting number for an enumerate list (the first item's counter). Cascades item -> enumerate block -> label -> job (fallback 1). |
| `counter_style` | CounterStyle \| null | null (unset) | — | job / label / content entry / line / list block | Style of an enumerate list's counters: `decimal` (1, 2, 3), `lower-alpha` (a, b, ..., z, aa), `upper-alpha` (A, B, ...), `lower-roman` (i, ii, iii) or `upper-roman` (I, II, III). Cascades item -> enumerate block -> label -> job (fallback 'decimal'). |
| `counter_prefix` | str \| null | null (unset) | — | job / label / content entry / line / list block | String prepended to each enumerate counter (e.g. 'Step ' for 'Step 1', 'Step 2'). Cascades item -> enumerate block -> label -> job (fallback '' = no prefix). |
| `counter_suffix` | str \| null | null (unset) | — | job / label / content entry / line / list block | String appended to each enumerate counter (e.g. '.' for '1.', ')' for '1)'). Cascades item -> enumerate block -> label -> job (fallback '' = no suffix). |
| `list_marker_gap_fraction` | float \| null | null (unset) | >=0 | job / label / content entry / line / list block | Air gap between a list item's marker (bullet or counter) and its text, as a unitless fraction of the line's rendered cap height, so it scales with the text size. The marker column is the widest marker of the list plus this gap, and every item's text (and every continuation line) starts at that column -- the hanging indent. Cascades item -> list block -> label -> job (fallback 0.5); explicit 0.0 is honored (marker flush against the text). |
| `bullet_string` | str \| null | null (unset) | — | plate | Bullet-list marker string (schema parity; not applied at plate level). |
| `counter_start_index` | int \| null | null (unset) | >=1 | plate | Enumerated-list starting counter (schema parity; not applied at plate level). |
| `counter_style` | CounterStyle \| null | null (unset) | — | plate | Enumerated-list counter style (schema parity; not applied at plate level). |
| `counter_prefix` | str \| null | null (unset) | — | plate | Enumerated-list counter prefix (schema parity; not applied at plate level). |
| `counter_suffix` | str \| null | null (unset) | — | plate | Enumerated-list counter suffix (schema parity; not applied at plate level). |
| `list_marker_gap_fraction` | float \| null | null (unset) | >=0 | plate | List marker-to-text gap as a fraction of cap height (schema parity; not applied at plate level). |
| `itemize` | ItemizeBlock \| null | null (unset) | — | content entry | Bullet-list block: every item engraves with the resolved bullet_string marker in one shared marker column. Mutually exclusive with text and enumerate. |
| `enumerate` | EnumerateBlock \| null | null (unset) | — | content entry | Enumerated-list block: every item's marker is the computed counter (prefix + style + suffix). Mutually exclusive with text and itemize. |
| `items` | list[TextLine] | **required** | — | list block | The list's text items. Each item is a text line carrying the list block's attributes (cascading item -> block -> label -> job); at least one item is required. |

## Enums

### `CounterStyle`

Enumeration of enumerated-list counter styles.

The style decides how an :class:`EnumerateBlock` renders the number of
each of its items (see :func:`plt_optimizer.generate.list_counter.format_counter`).
`decimal` is the historical/plain numbering; the alpha styles count in
letters (bijective base 26, so `z` is followed by `aa`); the roman
styles count in Roman numerals.

| Value | Description |
|---|---|
| `decimal` | Arabic numerals: 1, 2, 3, ... |
| `lower-alpha` | Lowercase letters: a, b, c, ..., z, aa, ab, ... |
| `upper-alpha` | Uppercase letters: A, B, C, ..., Z, AA, AB, ... |
| `lower-roman` | Lowercase Roman numerals: i, ii, iii, iv, ... |
| `upper-roman` | Uppercase Roman numerals: I, II, III, IV, ... |

## Defaults

Applied when unset at every tier (including `job-config.json`):

| Constant | Value | Note |
|---|---|---|
| `DEFAULT_BULLET_STRING` | `'•'` | Bullet-list marker used when `bullet_string` is unset at every tier. The derived bullet glyph (`fonts/glyph_transforms.py`), engraved by every PLT catalog font except the symbol fonts. Cascades item -> list block -> label -> job -> `job-config.json`; an explicit empty string engraves no marker. |
| `DEFAULT_COUNTER_PREFIX` | `''` | String prepended to each enumerate counter when `counter_prefix` is unset at every tier (empty = no prefix). |
| `DEFAULT_COUNTER_START_INDEX` | `1` | First counter value of an enumerate list when `counter_start_index` is unset at every tier. |
| `DEFAULT_COUNTER_STYLE` | `'decimal'` | Counter style of an enumerate list when `counter_style` is unset at every tier (the `decimal` value of `schema.CounterStyle`). One of `decimal`, `lower_alpha`, `upper_alpha`, `lower_roman`, `upper_roman`. |
| `DEFAULT_COUNTER_SUFFIX` | `''` | String appended to each enumerate counter when `counter_suffix` is unset at every tier (empty = no suffix). |
| `DEFAULT_LIST_MARKER_GAP_FRACTION` | `0.5` | Gap between a list marker and its text, as a fraction of the line's rendered cap height, when `list_marker_gap_fraction` is unset at every tier. The marker column is the widest marker of the list plus this gap. |

## Example

A numbered block and a bullet block in one label.

```yaml
job:
  job_name: "Lists demo"
  content:
    - text: "MAINTENANCE SCHEDULE"
    - enumerate:
        counter_style: upper-roman
        counter_suffix: "."
        items:
          - text: "Inspect belts"
          - text: "Lubricate bearings"
    - itemize:
        items:
          - text: "Shut power first"
          - text: "Wear eye protection"
```

## Pitfalls

- A content entry carries exactly ONE of `text`, `itemize` or `enumerate` — a list block is not a text line.
- Markers engrave in ONE flush column shared by the whole list and item text starts at the hanging-indent column (widest marker + `list_marker_gap_fraction` of the cap height); the list aligns as one block by `text_h_alignment`, so markers stay flush under center alignment.
- The counter is always computed, so an enumerated item's own `bullet_string` is inert; `bullet_string: ""` gives an indent-only (markerless) list.
- A list block cannot be a replacement-text template (its items are authored, never substituted), and words never cross an item boundary when items reflow.

## Read next

- `references/semantics.md` — the full semantics for this topic: mechanisms, thresholds, tier interactions and worked examples the tables above cannot express. Read it before authoring anything non-trivial in this area.
- `references/snippets.yaml` — more YAML fragments for this topic.
- [`../../../JOB_SPEC.md`](../../../JOB_SPEC.md) — exhaustive field/enum/default reference for the whole spec.
- [`../../../job_spec.schema.json`](../../../job_spec.schema.json) — machine-readable schema for structural validation.
- Related skills: [line-stacking-and-reflow](../line-stacking-and-reflow/SKILL.md); [inline-markup](../inline-markup/SKILL.md); [fonts-and-capline](../fonts-and-capline/SKILL.md).
