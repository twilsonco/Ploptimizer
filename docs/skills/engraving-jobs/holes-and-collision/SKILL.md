---
name: holes-and-collision
description: 'Mounting-hole placement, sizes, and text/hole clearance rules. Use when: adding mounting holes; holes overlap text; choosing hole margins and collision distances. Keywords: holes, location, corners, sides, hole_margin, min_hole_margin, collision, drill, align_margin_adjust_global.'
version: 1.0.0
metadata:
  hermes:
    category: engraving-jobs
    tags: [plt, engraving, toolpath, generate]
---

# Drill Holes and Collision Avoidance

## When to use

- adding mounting holes
- holes overlap text
- choosing hole margins and collision distances

## Fields

| Field | Type | Default | Constraints | Tiers | Description |
|---|---|---|---|---|---|
| `min_hole_margin` | float \| null | null (unset) | >=0 | job / label / content entry / line / list block | Minimum hole margin in inches; hole margins will not shrink below this value during collision avoidance. |
| `hole_text_collision_distance` | float \| null | null (unset) | >=0 | job / label / content entry / line / list block | Minimum air gap in inches between engraved text and drill hole strokes, on top of the stroke floor `0.5 * (hole_cutter + text_cutter)`. |
| `hole_margin` | float \| null | null (unset) | >=0 | job / label | Distance from hole edge to label edge in inches (label -> job, fallback 0.1875). |
| `holes` | list[HoleSpec] \| null | null (unset) | — | job / label | Drill holes; a label value replaces the job-level list entirely (an empty list suppresses holes). `corners`/`sides` expand to members. |
| `hole_adjust_for_cutter` | bool \| null | null (unset) | — | job / label | Shrink the emitted drill-hole circle by the boundary/hole cutter diameter (the cutter radius on each side of the hole's path) so the engraved hole measures the specified diameter: a 0.1in hole cut by a 0.015in cutter emits a 0.085in circle. The hole centre is untouched (placed from the specified diameter); a hole at or below the cutter diameter clamps to the smallest emittable circle with a WARNING. Cascades job -> plate -> label (never text lines); unset (`null`) falls back to `job-config.json` then `true`. Accepted on plates for schema parity only. |
| `align_margin_adjust_global` | bool \| null | null (unset) | — | job / label | Share the collision-avoidance edge-margin increase across the text lines of one label: when a `left`/`right`-aligned line has to slide off its anchored margin to clear a drill hole, every other line of the label sharing that exact `text_h_alignment` matches the increased margin, so the group's anchored edges stay flush. When the avoidance switches a colliding line to `center`, the unswitched same-alignment lines are additionally flushed onto the switched lines' final anchored edge (compressing a flushed line whose far end no longer fits). False (the default) moves only the colliding line. Only the automatic collision-avoidance slide is shared - declared margins are never touched. Setting the option at the job level enables it for every label; sharing is always per-label. Cascades label -> job (fallback false); explicit true is honored. Accepted on plates for schema parity only. |
| `hole_margin` | float \| null | null (unset) | >=0 | plate | Hole margin in inches (must be >= 0). |
| `min_hole_margin` | float \| null | null (unset) | >=0 | plate | Minimum hole margin in inches (schema parity; not applied at plate level). |
| `hole_text_collision_distance` | float \| null | null (unset) | >=0 | plate | Minimum engraved-stroke air gap in inches (schema parity; not applied at plate level). |
| `hole_adjust_for_cutter` | bool \| null | null (unset) | — | plate | Cutter-compensated drill-hole diameter (schema parity; not applied at plate level -- labels render before packing). |
| `align_margin_adjust_global` | bool \| null | null (unset) | — | plate | Per-label sharing of the collision-avoidance edge-margin increase (schema parity; not applied at plate level -- labels render before packing). |
| `location` | HoleLocation | **required** | — | hole | Hole position on the label edge. Group shorthands `corners` (all four corners) and `sides` (left + right) expand into their atomic members at validation time. |
| `diameter` | float | `0.125` | >0 | hole | Hole diameter in inches (default 0.125). A job-config.json `hole_diameter` fills entries that omit this field. |

## Enums

### `HoleLocation`

Enumeration of valid hole locations on a label.

Besides the eight atomic positions (four edges and four corners), two
group shorthands are accepted for the most common drilling patterns:
`corners` (all four corner holes) and `sides` (the left and right
edge holes). Groups are expanded into their atomic members at schema
validation time (see :data:`HOLE_LOCATION_GROUPS`), so downstream
consumers only ever see the eight atomic locations.

| Value | Description |
|---|---|
| `left` | Hole on the left edge. |
| `right` | Hole on the right edge. |
| `top` | Hole on the top edge. |
| `bottom` | Hole on the bottom edge. |
| `top-left` | Hole at the top-left corner. |
| `top-right` | Hole at the top-right corner. |
| `bottom-left` | Hole at the bottom-left corner. |
| `bottom-right` | Hole at the bottom-right corner. |
| `corners` | Group shorthand for all four corner holes. |
| `sides` | Group shorthand for the left and right edge holes. |

## Defaults

Applied when unset at every tier (including `job-config.json`):

| Constant | Value | Note |
|---|---|---|
| `DEFAULT_ALIGN_MARGIN_ADJUST_GLOBAL` | `False` | `False` = the collision-avoidance edge-margin slide moves only the colliding `left`/`right`-aligned line. `True` = every other line of the label sharing that exact `text_h_alignment` matches the increased margin, so the group's anchored edges stay flush (the shared shift is capped by the tightest slack in the group). Only the automatic slide is shared - declared margins are never touched, and the phase never fires when `max_h_compress` is 0.0. Cascades label/job (accepted on plates for schema parity only, never text lines); job-config supplies the shop default. |
| `DEFAULT_BOUNDARY_HOLE_CUTTER` | `0.015` | Boundary/hole cutter diameter feeding the collision stroke floor; `tools.json` `boundary_hole_cutter_size` overrides it. |
| `DEFAULT_HOLE_ADJUST_FOR_CUTTER` | `True` | `True` = the emitted drill-hole circle shrinks by the boundary/hole cutter diameter (the cutter radius on each side of the hole's path) so the engraved hole measures the specified diameter: a 0.1in hole cut by a 0.015in cutter emits a 0.085in circle. The hole centre is placed from the specified diameter and never moves; a hole at or below the cutter diameter clamps to the smallest emittable circle (one plotter unit radius) with a WARNING. Cascades label/job (accepted on plates for schema parity only, never text lines); job-config supplies the shop default. |
| `DEFAULT_HOLE_DIAMETER` | `0.125` | Drill-hole diameter when a hole entry omits 'diameter' (a job-config hole_diameter fills hole entries too). |
| `DEFAULT_HOLE_MARGIN` | `0.1875` | Hole margin when unset everywhere. Required from config-or-spec when a job-config is in play. |
| `DEFAULT_HOLE_TEXT_COLLISION_DISTANCE` | `0.15` | Engraved-stroke air gap on top of the stroke floor `0.5 * (hole_cutter + text_cutter)`. Required from config-or-spec when a job-config is in play (the shop `job-config.json` currently sets 0.1). |
| `DEFAULT_MIN_HOLE_MARGIN` | `None` | None = collision avoidance may shrink hole_margin all the way to 0.0. Required from config-or-spec when a job-config is in play. |

## Example

Four corner holes, 0.125in, 0.2in from the edges.

```yaml
job:
  job_name: "Holes demo"
  width: 4.0
  height: 2.0
  hole_margin: 0.2
  holes:
    - location: corners
      diameter: 0.125
  content:
    - text: "MOUNT HERE"
```

## Pitfalls

- `location: corners` / `sides` are group shorthands expanded AT VALIDATION time into the atomic locations; downstream only ever sees the 8 atomic values.
- Label `holes` REPLACES the job's list entirely; `holes: []` suppresses the job's holes on that label.
- Collision handling is 4 phases: always-on detection -> opt-in `min_hole_margin` sweep -> opt-in `max_h_compress` edge-margin slide (left/right-aligned text slides off its anchored margin; `align_margin_adjust_global: true` makes same-alignment siblings match the increased margin) -> alignment switch to `center` + `max_h_compress` squeeze (with the flag on, unswitched siblings then FLUSH onto the switched lines' final anchored edge, compressing any that no longer fit). Unresolved collisions abort the job (`LabelRenderError`).
- The stroke floor is `0.5 * (hole_cutter + text_cutter) + hole_text_collision_distance` — raise the distance for more air, not the hole diameter.
- `hole_adjust_for_cutter: true` shrinks the EMITTED circle by the boundary cutter diameter so the ENGRAVED hole measures `diameter`; the centre never moves.

## Read next

- `references/semantics.md` — the full semantics for this topic: mechanisms, thresholds, tier interactions and worked examples the tables above cannot express. Read it before authoring anything non-trivial in this area.
- `references/snippets.yaml` — more YAML fragments for this topic.
- [`../engraving-job-complete-reference/references/JOB_SPEC.md`](../engraving-job-complete-reference/references/JOB_SPEC.md) — exhaustive field/enum/default reference for the whole spec.
- [`../engraving-job-complete-reference/references/job_spec.schema.json`](../engraving-job-complete-reference/references/job_spec.schema.json) — machine-readable schema for structural validation.
- Related skills: [centering-and-descender-relief](../centering-and-descender-relief/SKILL.md); [label-geometry](../label-geometry/SKILL.md); [compression-and-cutter-downsize](../compression-and-cutter-downsize/SKILL.md); [cutters](../cutters/SKILL.md).
