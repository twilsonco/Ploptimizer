---
name: holes-and-collision
description: 'Mounting-hole placement, sizes, and text/hole clearance rules. Use when: adding mounting holes; holes overlap text; choosing hole margins and collision distances. Keywords: holes, location, corners, sides, hole_margin, min_hole_margin, min_line_spacing, collision, drill, align_margin_adjust_global, balanced_centering, balanced_centering_hole_text_mode, balanced_centering_hole_border_factor.'
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
| `min_line_spacing` | float \| null | null (unset) | >=0 | job / label / content entry / line / list block | Minimum inter-line spacing in inches; text-hole collision avoidance will not shrink a gap below this value when it packs a label's text block vertically to clear a top/bottom drill hole. Cascades line -> label -> job -> `job-config.json`; unset (`null`, the default) allows a gap to close all the way to `0.0` (adjacent lines' ink touch). Explicit `0.0` is honored; only `null` means unset. The floor is a LOWER bound in both directions: the side-hole spread phase GROWS the gaps to move the middle lines off mid-height holes and this value never caps that — the label's vertical margins do. Inert on a single-line label (no gap to pack). |
| `min_hole_margin` | float \| null | null (unset) | >=0 | job / label / content entry / line / list block | Minimum hole margin in inches; hole margins will not shrink below this value during collision avoidance. |
| `hole_text_collision_distance` | float \| null | null (unset) | >=0 | job / label / content entry / line / list block | Minimum air gap in inches between engraved text and drill hole strokes, on top of the stroke floor `0.5 * (hole_cutter + text_cutter)`. |
| `hole_margin` | float \| null | null (unset) | >=0 | job / label | Distance from hole edge to label edge in inches (label -> job, fallback 0.1875). |
| `holes` | list[HoleSpec] \| null | null (unset) | — | job / label | Drill holes; a label value replaces the job-level list entirely (an empty list suppresses holes). `corners`/`sides` expand to members. |
| `hole_adjust_for_cutter` | bool \| null | null (unset) | — | job / label | Shrink the emitted drill-hole circle by the boundary/hole cutter diameter (the cutter radius on each side of the hole's path) so the engraved hole measures the specified diameter: a 0.1in hole cut by a 0.015in cutter emits a 0.085in circle. The hole centre is untouched (placed from the specified diameter); a hole at or below the cutter diameter clamps to the smallest emittable circle with a WARNING. Cascades job -> plate -> label (never text lines); unset (`null`) falls back to `job-config.json` then `true`. Accepted on plates for schema parity only. |
| `balanced_centering` | bool \| null | null (unset) | — | job / label | Gap-balanced vertical centering for a label with a lone `top` (or lone `bottom`) drill hole. The hole-aware shift centers the text between the lone hole and the OPPOSITE vertical margin, which leaves the block about a quarter of a hole diameter off true center; this rule equalizes the two gaps instead -- the top text line's gap to the lone hole equals the bottom text line's gap to the opposite label BORDER (the engraved outline). Both distances are measured ink to ink: from the strokes the plotter actually cuts to the hole's engraved circle and to the border's engraved outline, so a round border or a line narrower than the block reads true. (A swept bounding box reads the block's corners instead, which on a round label reach into empty space past the ink and leave the text visibly off center -- the reason this rule measures ink.) Only the axis holes (`top`/`bottom`) count, so corner holes never trigger it, and a label with holes on both sides keeps the plain centering. Inert when there is no border ink to measure against, when the block cannot be balanced inside its band, and when the equal-gaps point would put less than the collision threshold between the text and the hole. Cascades job -> plate -> label (never text lines); unset (`null`) falls back to `job-config.json` then `false`. Accepted on plates for schema parity only. |
| `balanced_centering_hole_text_mode` | BalancedCenteringHoleTextMode \| null | null (unset) | — | job / label | How gap-balanced centering measures the hole-text distance (the hole-side gap `A`). `closest_ink` (the legacy metric) is the true stroke-to-circle minimum: the distance from the closest ink of any character in the hole-side line to the lone hole's engraved edge, so the glyph that reaches furthest toward the hole decides the gap. `top_line` is the vertical face-to-face gap: from the hole's engraved edge to the facing edge of the text box -- `(hole_center_y - hole_radius) - (top line's y_max)` for a lone top hole, mirrored for a lone bottom hole -- which ignores which glyph happens to be tallest. The border-side gap is ink-to-ink under both values. Cascades job -> plate -> label (never text lines); unset (`null`) falls back to `job-config.json` then `top_line`. Accepted on plates for schema parity only. |
| `balanced_centering_hole_border_factor` | float \| null | null (unset) | >=0 <=1 | job / label | Where gap-balanced centering lands the text block in the span between the lone hole and the opposite border, as a factor `f` in [0.0, 1.0] charged to the border side: the solve equalizes `f * A = (1 - f) * B`, where `A` is the hole-text gap (see `balanced_centering_hole_text_mode`) and `B` the text-border gap. 0.5 (the default) puts the block at equal gaps, 0.0 pushes it against the border (the text's border-side ink touches the border ink) and 1.0 against the hole. Cascades job -> plate -> label (never text lines); unset (`null`) falls back to `job-config.json` then 0.5. Accepted on plates for schema parity only. |
| `align_margin_adjust_global` | bool \| null | null (unset) | — | job / label | Share the collision-avoidance edge-margin increase across the text lines of one label: when a `left`/`right`-aligned line has to slide off its anchored margin to clear a drill hole, every other line of the label sharing that exact `text_h_alignment` matches the increased margin, so the group's anchored edges stay flush. When the avoidance switches a colliding line to `center`, the unswitched same-alignment lines are additionally flushed onto the switched lines' final anchored edge. Flushness is a guarantee: a shared slide that pushes a line's far end past the content area is absorbed by horizontal compression, and a line whose `max_h_compress` budget cannot absorb it fails the job. False (the default) moves only the colliding line. Only the automatic collision-avoidance slide is shared - declared margins are never touched. Setting the option at the job level enables it for every label; sharing is always per-label. Cascades label -> job (fallback false); explicit true is honored. Accepted on plates for schema parity only. |
| `hole_margin` | float \| null | null (unset) | >=0 | plate | Hole margin in inches (must be >= 0). |
| `min_hole_margin` | float \| null | null (unset) | >=0 | plate | Minimum hole margin in inches (schema parity; not applied at plate level). |
| `min_line_spacing` | float \| null | null (unset) | >=0 | plate | Minimum inter-line spacing in inches (schema parity; collision avoidance resolves the floor label -> job; not applied at plate level). |
| `hole_text_collision_distance` | float \| null | null (unset) | >=0 | plate | Minimum engraved-stroke air gap in inches (schema parity; not applied at plate level). |
| `hole_adjust_for_cutter` | bool \| null | null (unset) | — | plate | Cutter-compensated drill-hole diameter (schema parity; not applied at plate level -- labels render before packing). |
| `balanced_centering` | bool \| null | null (unset) | — | plate | Gap-balanced centering for a lone top/bottom drill hole (equal gaps to the hole and the opposite border) (schema parity; not applied at plate level -- labels render before packing). |
| `balanced_centering_hole_text_mode` | BalancedCenteringHoleTextMode \| null | null (unset) | — | plate | Hole-text gap metric for gap-balanced centering: `closest_ink` (stroke-to-circle minimum) or `top_line` (vertical box-face-to-hole-face) (schema parity; not applied at plate level -- labels render before packing). |
| `balanced_centering_hole_border_factor` | float \| null | null (unset) | >=0 <=1 | plate | Border-side factor `f` in [0.0, 1.0] of the gap-balanced centering solve `f * A = (1 - f) * B` (0.5 = equal gaps, 0.0 = against the border, 1.0 = against the hole) (schema parity; not applied at plate level -- labels render before packing). |
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

### `BalancedCenteringHoleTextMode`

Enumeration of hole-text distance metrics for gap-balanced centering.

Gap-balanced centering (see
:attr:`plt_optimizer.generate.schema.LabelAttributes.balanced_centering`
and :mod:`plt_optimizer.generate.collision.balance`) slides a label's text
block so the block's gap to its lone top (or lone bottom) drill hole and
its gap to the opposite border hit a chosen split. This field picks how the
**hole-side** gap is measured; the border side stays ink-to-ink.

| Value | Description |
|---|---|
| `closest_ink` |  |
| `top_line` |  |

## Defaults

Applied when unset at every tier (including `job-config.json`):

| Constant | Value | Note |
|---|---|---|
| `DEFAULT_ALIGN_MARGIN_ADJUST_GLOBAL` | `False` | `False` = the collision-avoidance edge-margin slide moves only the colliding `left`/`right`-aligned line. `True` = every other line of the label sharing that exact `text_h_alignment` matches the increased margin, so the group's anchored edges stay flush (the shared shift is capped by the tightest slack in the group). Only the automatic slide is shared - declared margins are never touched, and the phase never fires when `max_h_compress` is 0.0. Cascades label/job (accepted on plates for schema parity only, never text lines); job-config supplies the shop default. |
| `DEFAULT_BALANCED_CENTERING` | `False` | `False` = a label with a lone `top` (or lone `bottom`) drill hole keeps the legacy centering: the hole-aware shift centers the text block between the lone hole and the OPPOSITE vertical margin, about a quarter of a hole diameter off true center. `True` equalizes the two gaps instead - the block's gap to the lone hole equals its gap to the opposite label BORDER, swept-edge to swept-edge, clamped to the alignment content band and never inside the collision threshold. Off by default so every shipped job centers the legacy way until it opts in; cascades label/job (accepted on plates for schema parity only, never text lines); `job-config.json` supplies the shop default. |
| `DEFAULT_BALANCED_CENTERING_HOLE_BORDER_FACTOR` | `0.5` | `0.5` = gap-balanced centering solves `f * A = (1 - f) * B` at equal weights, so the block lands where the hole-text gap `A` equals the text-border gap `B` - the shipped rule, unchanged. Raising `f` toward `1.0` moves the block toward the hole, lowering it toward `0.0` moves it toward the border; the travel stays inside the same caps (alignment content band, collision threshold, the legacy centering move), so an extreme factor clamps to the best placement the caps allow and logs PARTIAL rather than engraving a collision. Valid range [0.0, 1.0]; inert unless `balanced_centering` is true. Cascades label/job (accepted on plates for schema parity only, never text lines); `job-config.json` supplies the shop default. |
| `DEFAULT_BALANCED_CENTERING_HOLE_TEXT_MODE` | `'closest_ink'` | `closest_ink` = the hole-side gap `A` of gap-balanced centering is the minimum distance from any engraved stroke of the hole-side text line to the hole's emitted circle, so the glyph that reaches furthest toward the hole decides (a capital's top edge is not special). `top_line` measures the vertical face-to-face gap between the lone hole's engraved edge and the facing edge of the text box - the reading a designer reasons about, and what `job-config.json` supplies as the shop default. The border-side gap stays ink-to-ink either way, and the metric is inert unless `balanced_centering` is true. Cascades label/job (accepted on plates for schema parity only, never text lines). |
| `DEFAULT_BOUNDARY_HOLE_CUTTER` | `0.015` | Boundary/hole cutter diameter feeding the collision stroke floor; `tools.json` `boundary_hole_cutter_size` overrides it. |
| `DEFAULT_HOLE_ADJUST_FOR_CUTTER` | `True` | `True` = the emitted drill-hole circle shrinks by the boundary/hole cutter diameter (the cutter radius on each side of the hole's path) so the engraved hole measures the specified diameter: a 0.1in hole cut by a 0.015in cutter emits a 0.085in circle. The hole centre is placed from the specified diameter and never moves; a hole at or below the cutter diameter clamps to the smallest emittable circle (one plotter unit radius) with a WARNING. Cascades label/job (accepted on plates for schema parity only, never text lines); job-config supplies the shop default. |
| `DEFAULT_HOLE_DIAMETER` | `0.125` | Drill-hole diameter when a hole entry omits 'diameter' (a job-config `hole_diameter` fills hole entries too). |
| `DEFAULT_HOLE_MARGIN` | `0.1875` | Hole margin when unset everywhere. Required from config-or-spec when a job-config is in play. |
| `DEFAULT_HOLE_TEXT_COLLISION_DISTANCE` | `0.15` | Engraved-stroke air gap on top of the stroke floor `0.5 * (hole_cutter + text_cutter)`. Required from config-or-spec when a job-config is in play (the shop `job-config.json` currently sets 0.1). |
| `DEFAULT_MIN_HOLE_MARGIN` | `None` | None = collision avoidance may shrink `hole_margin` all the way to 0.0. Required from config-or-spec when a job-config is in play. |
| `DEFAULT_MIN_LINE_SPACING` | `None` | None = collision avoidance may pack a multi-line text block's inter-line gaps all the way to 0.0 when a top/bottom hole leaves no other vertical room. Optional (not in the job-config gate): the shop default comes from the `line_spacing` cascade, and a job that wants a floor declares it at job or label level. |

## Example

Four corner holes, 0.125in, 0.2in from the edges.

```yaml
job:
  name: "Holes demo"
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
- Collision handling is a ladder run on the AXES the label's holes leave open: a hole on the TOP/BOTTOM edge is escaped only on the Y axis, a hole on the LEFT/RIGHT edge only on the X axis, a corner hole on both. A top/bottom-only job NEVER squeezes glyph widths -- sliding text sideways cannot move it off a hole that spans the row -- and one that runs out of vertical room FAILS the job (no sideways fallback). Phases: always-on detection -> (LEFT/RIGHT holes only, and only for an EVEN number of text lines) inter-line spread: the block is grown about its centre so the middle two lines step above/below the mid-height holes, capped by the label's vertical margins and tried FIRST because a cleared collision leaves every line worrying about margins alone; a spread that clears nothing is reverted untouched -> opt-in `min_hole_margin` sweep -> (top/bottom/corner holes only) text-block slide -> (same gate, multi-line only) inter-line packing down to `min_line_spacing` -> opt-in `max_h_compress` edge-margin slide (left/right-aligned text slides off its anchored margin; `align_margin_adjust_global: true` makes same-alignment siblings take the full increased margin) -> alignment switch to `center` + `max_h_compress` squeeze (with the flag on, unswitched siblings then FLUSH onto the switched lines' final anchored edge). The content box is a compression constraint, never an alignment-stopping one: a shared slide a line's budget cannot compress into the box fails the job. Unresolved collisions abort the job (`LabelRenderError`).
- The stroke floor is `0.5 * (hole_cutter + text_cutter) + hole_text_collision_distance` — raise the distance for more air, not the hole diameter.
- `hole_adjust_for_cutter: true` shrinks the EMITTED circle by the boundary cutter diameter so the ENGRAVED hole measures `diameter`; the centre never moves.

## Read next

- `references/semantics.md` — the full semantics for this topic: mechanisms, thresholds, tier interactions and worked examples the tables above cannot express. Read it before authoring anything non-trivial in this area.
- `references/snippets.yaml` — more YAML fragments for this topic.
- [`../engraving-job-complete-reference/references/JOB_SPEC.md`](../engraving-job-complete-reference/references/JOB_SPEC.md) — exhaustive field/enum/default reference for the whole spec.
- [`../engraving-job-complete-reference/references/JOB_CONFIG.md`](../engraving-job-complete-reference/references/JOB_CONFIG.md) — exhaustive reference for the `job-config.json` shop-default fields.
- [`../engraving-job-complete-reference/references/job_spec.schema.json`](../engraving-job-complete-reference/references/job_spec.schema.json) — machine-readable schema for structural validation.
- Related skills: [centering-and-descender-relief](../centering-and-descender-relief/SKILL.md); [label-geometry](../label-geometry/SKILL.md); [compression-and-cutter-downsize](../compression-and-cutter-downsize/SKILL.md); [cutters](../cutters/SKILL.md).
