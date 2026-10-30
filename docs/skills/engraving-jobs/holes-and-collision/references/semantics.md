# Drill Holes and Collision Avoidance — semantics

## Declaring holes

`holes` is a list of `{location, diameter}` entries, cascading
label → job → config (fallback: none). **A label's `holes` list REPLACES the
job's list entirely**; `holes: []` on a label suppresses the job's holes.
`diameter` is in inches; when omitted it fills from the config's
`hole_diameter` (or the fallback constant).

### Locations: 8 atomic + 2 group shorthands

`location` accepts `left`, `right`, `top`, `bottom`, `top-left`,
`top-right`, `bottom-left`, `bottom-right`, plus the group shorthands
`corners` (→ the four corner holes) and `sides` (→ left + right). Groups
expand **at validation time**, so every downstream stage (and every error
message) only ever sees the 8 atomic locations.

Placement is from `hole_margin` (cascade label → job → config, fallback
`0.1875`): the distance from the label edge to the hole centre.

## Cutter-compensated holes (`hole_adjust_for_cutter`, default true)

The drill path is the circle the cutter **centre** follows, so the cutter
engraves half a cutter width on each side of it. With the mechanism on, the
emitted circle shrinks by the boundary/hole cutter diameter so the ENGRAVED
hole measures the specified `diameter`: a `0.1` hole cut by a `0.015` cutter
emits a `0.085` circle.

- The hole **centre** is always placed from the specified diameter — hole
  positions never move with the compensation.
- A hole at or below the cutter diameter clamps to the smallest emittable
  circle (one plotter unit radius) with a WARNING.
- The text–hole collision check measures the ENGRAVED circle, so near misses
  relax by the same half cutter.

## Text–hole collision avoidance (4 phases)

1. **Detection (always on).** A text stroke collides with a hole when the
   gap between the ink and the engraved circle falls below the stroke floor:
   `0.5 * (hole_cutter + text_cutter) + hole_text_collision_distance`.
2. **`min_hole_margin` sweep (opt-in).** When set, holes may be swept toward
   the edge (shrinking their margin down to `min_hole_margin`) to clear the
   text. `min_hole_margin: null` (the default) allows the sweep all the way
   to `0.0`.
3. **Edge-margin slide (opt-in via `max_h_compress`).** Horizontal
   compression cannot free a `left`/`right`-aligned line from a hole on its
   *anchored* side — the anchored edge stays pinned at the margin, so
   compressing only shortens the far end. The slide increases that line's
   edge margin (moves it away from the near-side hole), capped by the point
   where a far-side hole starts colliding. The content-area box is **not** a
   cap: a slide that pushes a line's far end past the far margin is absorbed
   by horizontal compression, so the box is a compression constraint, never
   an alignment-stopping one. With `align_margin_adjust_global: true`, every
   other line of the label sharing that exact `text_h_alignment` matches the
   increased margin (taking the full shared shift), so the group's anchored
   edges stay flush; the default (`false`) moves only the colliding line. A
   shared slide a line's `max_h_compress` budget cannot absorb fails the job.
   The slide re-runs to a fixed point: the shared shift is the tightest need
   among the colliding lines, so a line needing marginally more is cleared by
   the next round (every line ends at the largest need) instead of escalating
   to the center switch below.
4. **`max_h_compress` squeeze (opt-in).** A colliding line whose slide budget
   is exhausted (holes collide on both sides) switches to `center` alignment,
   and text that still collides compresses horizontally (per its budget) — on
   a centered line compression acts symmetrically and pulls both ends away
   from the side holes. With `align_margin_adjust_global: true`, the
   unswitched same-alignment lines are then **flushed** onto the switched
   lines' final anchored edge (the leftmost final left edge for `left`
   groups, mirrored for `right`), compressing a flushed line whose far end no
   longer fits; the switched lines keep their auto-adjustment. With the flag
   off, a switch moves only the colliding line.

**Unresolved collisions fail the job** (`LabelRenderError`, non-zero exit) —
the tool never engraves ink through a hole. Fixes, in order of preference:
raise `hole_text_collision_distance` shop-wide only if the machine needs it;
per job, move the holes (bigger `hole_margin`, fewer locations), shorten the
text, allow `max_h_compress`, or grow the label. Every applied avoidance
(edge-margin slide, alignment switch, compression) is logged at WARNING and
listed in the layout report.

## Single-side hole centering

`adjust_h_center_for_single_side_hole` (cascade label → job → config) shifts
horizontal centering when holes exist on ONE side only, so text centers in
the free space (see the centering skill).

```yaml
job:
  job_name: "Panel tag"
  width: 5.0
  height: 1.5
  hole_margin: 0.25
  min_hole_margin: 0.15
  holes:
    - location: sides
      diameter: 0.125
  content:
    - text: "FIELD WIRING — TERMINAL B"
```
