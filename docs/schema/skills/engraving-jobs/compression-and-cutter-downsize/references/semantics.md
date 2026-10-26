# Compression and Cutter Downsizing — semantics

Two coupled mechanisms keep long text inside the label: horizontal
compression (squeeze the glyphs) and compression-driven cutter downsize (swap
to a finer tool so the squeezed line keeps real ink height).

## Horizontal compression (`max_h_compress`)

- `max_h_compress` (cascade line → label → job → config; required from
  config-or-spec in production) is the compression **budget**: the line may
  be squeezed to scale `1 - max_h_compress`. `0.0` = compression off.
- Triggers: (1) the rendered line overflows the label's usable width, (2) a
  text–hole collision needs width (see the holes-and-collision skill).
- The scale is horizontal only: glyphs narrow, height unchanged.
- `arc_preserve_compression` (cascade line → label → job → config) keeps
  circular arcs native under the non-uniform scale by fitting true elliptical
  arcs (ellipse-preserving transforms); off, arcs flatten at the chord
  tolerance like any other geometry.

## Cutter downsize (compression-driven)

Fields: `cutter_downsize` (default `true`), `max_cutter_downsizes`
(default `1`, `0` = disabled).

When a text line's **effective horizontal scale** (margin × collision
compression) falls below the **midpoint** between the current automatic
cutter and the next smaller `tools.json` rung — e.g. current `0.100in`, next
`0.080in`: scale `< 0.90` downsizes to `0.080in` — the line's cutter drops
one rung and the toolpath height grows to `text_height - smaller_cutter`
(the nominal `text_height` is kept). Details:

- at most `max_cutter_downsizes` rungs per line; each step re-measures the
  re-rendered line; the ladder is **strictly one-way** (a line never regains
  a larger cutter),
- requires a cutter inventory (no `tools.json` → no reduction) and a
  compression budget (`max_h_compress > 0`),
- an explicit `cutter_size` is never reduced,
- the reduced cutter flows into the pen map, so the downsized line lands in
  its own per-cutter PLT file,
- WARNING-logged at render time and reported in the layout report
  (`cutter 0.060in -> 0.045in`).

## Per-label sharing

Two sharing fields make one label engrave one text size with **one tool at
one density**:

- `cutter_downsize_global` (default `true`): when a line's compression
  triggers a swap, every other **eligible** line of the same `text_height`
  **within the same label** receives the same swap (the midpoint trigger is
  bypassed for receivers — a sibling's trigger is their trigger), then each
  receiver continues its own one-way loop; the group converges to the
  **smallest final cutter** among its members.
- `h_compress_global` (default `false`): when a line is compressed, every
  other eligible same-height line in the label compresses to the group's
  **most-compressed (minimum) scale**, clamped to each receiver's own
  `1 - max_h_compress` floor. A line already tighter keeps it (never
  stretched back). `h_compress_global_same_height_only` (default `true`)
  scopes the sharing to lines of the **same** `text_height`; `false` shares
  it across every eligible line of the label, so the whole label engraves at
  one glyph density.

Eligibility (both mechanisms): a line with an explicit `cutter_size`,
`cutter_downsize: false`, `max_cutter_downsizes: 0`, `max_h_compress: 0.0`,
or the sharing field `false` is never touched — in **either direction** (it
neither shares its own result nor receives a sibling's).

Sharing is always **per-label** — setting the option at the job level simply
enables it for every label.

## Phase order (why a squeeze can look stale)

Cutter reduction runs **first**; shared compression runs as an export
pre-pass **after** it. A shared compression can therefore deepen a line's
squeeze without re-triggering a cutter swap (accepted staleness). The layout
report shows the final state.

```yaml
job:
  job_name: "Tight serials"
  max_h_compress: 0.25
  cutter_downsize: true
  max_cutter_downsizes: 2
  h_compress_global: true
  cutter_downsize_global: true
  labels:
    - id: tag
      content:
        - text: "MODEL 241113-0506/REV-B LONG"
        - text: "SHORT"
```
