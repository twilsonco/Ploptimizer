---
name: label-geometry
description: 'Label dimensions, auto-sizing, margins, rounded corners. Use when: sizing a label or its margins; making rounded-rectangle labels; text spills outside the border. Keywords: width, height, margin, rounded, shape, border, auto-size, geometry.'
version: 1.0.0
metadata:
  hermes:
    category: engraving-jobs
    tags: [plt, engraving, toolpath, generate]
---

# Label Size, Margins and Shape

## When to use

- sizing a label or its margins
- making rounded-rectangle labels
- text spills outside the border

## Fields

| Field | Type | Default | Constraints | Tiers | Description |
|---|---|---|---|---|---|
| `width` | float \| null | null (unset) | >0 | job / label | Label width in inches (must be > 0). Cascades label -> job; must be defined at one level. |
| `height` | float \| null | null (unset) | >0 | job / label | Label height in inches (must be > 0). Cascades label -> job; must be defined at one level. |
| `type` | LabelShape \| null | null (unset) | — | job / label | Label boundary shape: `rect` (default, historical) or `round` (ellipse with semi-axes `width/2` and `height/2`; a circle when width == height). Cascades label -> job; unset (null) falls back to `rect`. Accepted on plates for schema parity only. |
| `rounded_rectangle_fraction` | float \| null | null (unset) | >=0 <=1 | job / label | Corner rounding fraction in [0.0, 1.0] of the rectangular border (0.0 = sharp rectangle, the default). Corners are superellipse-quadrant patches with exponent 2/fraction, so curvature varies continuously along the boundary (no circular-fillet curvature jump). The rounding spans 'fraction' of the shorter edge: a 4x8 label at 0.5 rounds 2in (50%) of the 4in sides, starting 1in from each corner. At 1.0 with `rounded_rectangle_proportional` true (or on a square) the boundary is the exact ellipse/circle of type: round; on a non-square at 1.0 with proportional false the short edges fully round and the long edges keep straights. Cascades job -> plate -> label; unset (`null`) falls back to `job-config.json` then 0.0. Accepted on plates for schema parity only. Inert on type: round labels. |
| `rounded_rectangle_proportional` | bool \| null | null (unset) | — | job / label | Whether `rounded_rectangle_fraction` applies proportionately to width and height (true: corner semi-axes `fraction*width/2` and `fraction*height/2`) or as one identical rounding distance on every side (false, the default: the shorter edge determines the rounding for all sides). Cascades job -> plate -> label; unset (`null`) falls back to `job-config.json` then `false`. Accepted on plates for schema parity only. |
| `margin` | float \| null | null (unset) | >=0 | job / label | Universal margin in inches (fallback for h_margin and v_margin); cascades label -> job (fallback 0.125). |
| `h_margin` | float \| null | null (unset) | >=0 | job / label | Horizontal margin in inches (left and right edges); cascades label -> job; falls back to margin if unset. |
| `v_margin` | float \| null | null (unset) | >=0 | job / label | Vertical margin in inches (top and bottom edges); cascades label -> job; falls back to margin if unset. |
| `width` | float | **required** | >=0 | plate | Usable plate width in inches (must be >= 0). |
| `height` | float | **required** | >=0 | plate | Usable plate height in inches (must be >= 0). |
| `h_margin` | float \| null | null (unset) | >=0 | plate | Horizontal margin in inches (schema parity; not applied at plate level). |
| `v_margin` | float \| null | null (unset) | >=0 | plate | Vertical margin in inches (schema parity; not applied at plate level). |
| `type` | LabelShape \| null | null (unset) | — | plate | Label boundary shape 'rect'/'round' (schema parity; label shape is resolved per label before packing; not applied at plate level). |
| `rounded_rectangle_fraction` | float \| null | null (unset) | >=0 <=1 | plate | Corner rounding fraction in [0.0, 1.0] (schema parity; the border is resolved per label before packing; not applied at plate level). |
| `rounded_rectangle_proportional` | bool \| null | null (unset) | — | plate | Proportional corner rounding (schema parity; the border is resolved per label before packing; not applied at plate level). |

## Enums

### `LabelShape`

Enumeration of valid label boundary shapes.

`rect` (the default) is the historical behavior: the label boundary is
the `width` x `height` rectangle, the packer packs that rectangle, and
every collision check measures the rectangle. `round` makes the boundary
an ellipse with semi-axes `width / 2` and `height / 2` -- a circle when
the two are equal -- which switches the label onto the adjacent
ellipse-packing pipeline (see :mod:`plt_optimizer.generate.packing.ellipse_packer`)
and makes the collision checks measure the ellipse boundary.

| Value | Description |
|---|---|
| `rect` | Rectangular label boundary. |
| `round` | Elliptical (round) label boundary. |

## Defaults

Applied when unset at every tier (including `job-config.json`):

| Constant | Value | Note |
|---|---|---|
| `DEFAULT_H_MARGIN` | `None` | Horizontal margin fall back to margin, then the default. Both are None to indicate "unset"; resolution logic fills them in. |
| `DEFAULT_LABEL_SHAPE` | `'rect'` | Fallback label boundary shape when the `type` cascade resolves to unset. See :attr:`plt_optimizer.generate.schema.LabelAttributes.type`. |
| `DEFAULT_MARGIN` | `0.125` | Label margin when unset at label/job level. |
| `DEFAULT_ROUNDED_RECTANGLE_FRACTION` | `0.0` | Fallback rectangular-border corner rounding fraction when the `rounded_rectangle_fraction` cascade resolves to unset (see :attr:`plt_optimizer.generate.schema.LabelAttributes.rounded_rectangle_fraction`). `0.0` is the sharp rectangle, bit-identical to the historical border. |
| `DEFAULT_ROUNDED_RECTANGLE_PROPORTIONAL` | `False` | Fallback proportional-rounding flag when the `rounded_rectangle_proportional` cascade resolves to unset (see :attr:`plt_optimizer.generate.schema.LabelAttributes.rounded_rectangle_proportional`). `False` uses one identical rounding distance on every side (the shorter edge determines it). |
| `DEFAULT_V_MARGIN` | `None` | Vertical margin fall back to margin, then the default. Both are None to indicate "unset"; resolution logic fills them in. |

## Example

Fixed 3x1.5in label, 0.15in margins, rounded corners.

```yaml
job:
  job_name: "Rounded placard"
  width: 3.0
  height: 1.5
  margin: 0.15
  rounded_rectangle_fraction: 0.5
  content:
    - text: "ROUNDED CORNERS"
```

## Pitfalls

- Omitting `width`/`height` auto-sizes the label from rendered content — the safe default for single labels; set them for fixed-size stock.
- `margin` is the shorthand; `h_margin`/`v_margin` are axis overrides and win over `margin` at the same tier.
- Ink may spill into the margin (only the border stroke counts for collision) — that is by design, not overflow.
- `type: round` is an ELLIPSE boundary (a circle when width == height) and switches the label to ellipse packing; `rounded_rectangle_fraction` rounds the corners of a `rect` boundary and is inert on `round` labels.
- `rounded_rectangle_fraction` spans that fraction of the SHORTER edge (superellipse corners, no fillet curvature jump); `rounded_rectangle_proportional: true` applies it per-axis instead.

## Read next

- `references/semantics.md` — the full semantics for this topic: mechanisms, thresholds, tier interactions and worked examples the tables above cannot express. Read it before authoring anything non-trivial in this area.
- `references/snippets.yaml` — more YAML fragments for this topic.
- [`../../../JOB_SPEC.md`](../../../JOB_SPEC.md) — exhaustive field/enum/default reference for the whole spec.
- [`../../../job_spec.schema.json`](../../../job_spec.schema.json) — machine-readable schema for structural validation.
- Related skills: [cascade-and-config](../cascade-and-config/SKILL.md); [holes-and-collision](../holes-and-collision/SKILL.md); [line-stacking-and-reflow](../line-stacking-and-reflow/SKILL.md).
