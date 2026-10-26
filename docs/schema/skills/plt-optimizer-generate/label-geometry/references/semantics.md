# Label Size, Margins and Shape — semantics

## Size: explicit vs auto

- `width` / `height` cascade **label → job**. Omit both and the label
  **auto-sizes** from its rendered content + margins — the safe default for
  one-off labels.
- Set them for fixed-size stock (a 2×4 placard, a 3×1 tag). When content
  overflows a fixed label the pipeline compresses (per `max_h_compress`) and
  reflows (per `optimize_line_content`); what still cannot fit is a
  collision/fit failure, not a silent crop.
- In the job-level and plate-level replacement-file forms, job `width` /
  `height` are **required** (the synthesized labels have no dimensions of
  their own).

## Margins: three fields, one suppression rule

- `margin` is the shorthand; `h_margin` / `v_margin` are axis-specific.
  Within one tier, the axis value wins over the shorthand.
- Cascade: label → job → config (fallback `0.125`).
- **Suppression:** a spec-declared `margin` (job level, **or on any label**)
  suppresses the config's `h_margin`/`v_margin` injection for that scope —
  the cascade checks the axis keys *before* `margin`, so an injected axis
  value would otherwise silently outrank the spec's shorthand.
- **Ink may spill into the margin.** The margin is the keep-out for the
  *border stroke* and hole placement, not for ink: descenders hang into the
  bottom margin by design (the descending-character relief clears them
  against the border). Crossing `v_margin` is not a collision.
- `hole_margin` (fallback `0.1875`) is separate: the distance from the label
  edge to a hole centre.

## Shape: `type` picks the boundary; the fraction rounds a rectangle

`type` (cascade label → job, fallback `rect`) selects the boundary shape,
and it changes what the packer and the collision checks measure:

| Value | Boundary |
|---|---|
| `rect` | the `width` × `height` rectangle (default, historical) |
| `round` | an **ellipse** with semi-axes `width/2` and `height/2` — a circle when the two are equal |

- `type: round` switches the label onto the ellipse-packing pipeline and
  measures collisions against the ellipse boundary. For a circular tag, set
  `width == height` and `type: round`; no fraction is involved.
- `rounded_rectangle_fraction` (`0.0–1.0`, fallback `0.0` = sharp) rounds the
  corners of a **rect** boundary: superellipse-quadrant patches whose
  rounding spans `fraction` of the shorter edge (a 4×8 label at `0.5` rounds
  2in of the 4in sides, starting 1in from each corner). At `1.0` with
  `rounded_rectangle_proportional: true` (or on a square) the boundary
  becomes the exact ellipse/circle of `type: round`. **Inert on
  `type: round` labels.** Cascades job → plate → label (accepted on plates
  for schema parity only).
- `rounded_rectangle_proportional` (fallback `false`): `true` applies the
  fraction proportionately to each axis (corner semi-axes
  `fraction*width/2` and `fraction*height/2`); `false` uses one identical
  rounding distance on every side, taken from the shorter edge.

The border is engraved by the boundary/hole tool; text collides with the
border stroke, not with the nominal edge.

## Worked examples

Fixed placard, rounded corners, generous margins:

```yaml
job:
  job_name: "Placard"
  width: 6.0
  height: 3.0
  h_margin: 0.35
  v_margin: 0.3
  rounded_rectangle_fraction: 0.5
  text_height: 0.6
  content:
    - text: "MAINTENANCE AISLE"
    - text: "KEEP CLEAR"
```

Circular tag (square + ellipse boundary):

```yaml
labels:
  - id: round_tag
    width: 2.0
    height: 2.0
    type: round
    margin: 0.2
    content:
      - text: "INSPECTED"
```
