# Bullet and Enumerated Lists — semantics

A `content` entry carries **exactly one** of `text`, `itemize` or
`enumerate`. A list block nests its own settings plus one or more text
`items`, and expands at resolution into one rendered line per item.

## Marker column and hanging indent

- Every item engraves its marker — the `bullet_string` (itemize), or the
  computed `counter_prefix + counter + counter_suffix` (enumerate) — in
  **ONE flush marker column shared by the whole list**.
- The item's text — and every continuation line `optimize_line_content`
  grows it to — starts at the **hanging-indent column** right of the
  markers: the widest marker in the list + `list_marker_gap_fraction` of the
  line's cap height.
- The list aligns as ONE block by `text_h_alignment`, so the markers stay
  flush under center alignment (the block centers; the columns inside it
  stay aligned).

## Counters (enumerate)

The counter is **always computed** from `counter_start_index` (default 1),
`counter_style` (`decimal`, `lower-alpha`, `upper-alpha`, `lower-roman`,
`upper-roman`), `counter_prefix` / `counter_suffix` (default `''`). Because
it is computed, an enumerated item's own `bullet_string` is **inert**.

## Bullets (itemize)

- Default `bullet_string` is the derived bullet glyph `•` (a catalog glyph,
  engraved by the same font ladder).
- An explicit `bullet_string: ""` engraves **no marker** — an indent-only
  list. Any short string works as a marker (`-`, `>`, `*`).

## Cascade and reflow

- List settings cascade **item → list block → label → job → config** (plate
  parity), so one item can override its own `text_height` and the block
  shares everything else.
- An item grows to as many lines as it needs when `optimize_line_content`
  is enabled and no `optimize_line_content_max_lines` cap is set; **words
  never cross an item boundary**.
- A list block **cannot be a replacement-text template** (its items are
  authored, never substituted) — a `replacement_text_file` label whose
  `content` contains a list block is rejected.

```yaml
job:
  job_name: "Service tag"
  width: 4.0
  text_height: 0.25
  content:
    - text: "STARTUP CHECKLIST"
      text_height: 0.35
    - enumerate:
        counter_style: upper-roman
        counter_suffix: "."
        items:
          - text: "Verify isolation"
          - text: "Open panels"
    - itemize:
        bullet_string: "-"
        items:
          - text: "Wear eye protection"
          - text: "Two-person rule"
```
