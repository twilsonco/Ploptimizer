# Gap-balanced centering for a lone top/bottom drill hole

Rule: a label with a lone `top` (or lone `bottom`) drill hole centers its text
block so the gap to that hole equals the gap to the OPPOSITE label border,
**ink to ink**, clamped to the alignment content band. Opt-in
(`balanced_centering`, default off); the legacy hole-aware shift stays.

> **Superseded metric notice.** Stages 1–5 were built on a *box* metric: gap A
> was measured from a flat segment spanning the whole text block at its
> hole-side face, and gap B from a flat segment spanning the block at its
> border-side face. On a curved border the ends of that segment reach out into
> empty space, so B collapsed to the distance from the block's **corners** —
> not from any engraved stroke. Stage 6 replaces the metric; see
> **Stage 6 (ink-to-ink gaps)** below, which is now the authoritative
> definition. Everything below this notice that describes box faces is
> historical.

Status:
- Stage 1 solver — `plt_optimizer/generate/collision/balance.py`, committed `2d9e24b`.
- Stage 2 renderer integration — `label_renderer.py` `_balance_shift_inches`, in tree.
- Stage 3 cascade param — `schema.py` (LabelAttributes + PlateSpec parity),
  `resolution.py` (`DEFAULT_BALANCED_CENTERING`, `ResolvedLabel.balanced_centering`),
  generated docs/schemas regenerated.
- Stage 4 packing-path parity — IMPLEMENTED (`58c90b5`, `6e7f040`). The solve now lives in ONE
  place, `collision/balance.py::balance_shift(label, geometry)`, over a structured
  label-local-inch `BalanceGeometry` (block box, border box, hole circles). The renderer
  adapter feeds it the pre-centering HPGL box (lifted by the `y_adjustment` the block is
  about to receive); `plate_optimizer._label_transform` feeds it the structured
  `text_chunks` box + the structural border + `collision_hole_circles`. Parity is pinned
  by `tests/test_packing_balance.py` (analytic `expected_center` parity on 7 shapes + the
  pre-centred fixture end-to-end), not by a reimplementation of the formula.

  The cap-to-legacy-travel rule (`min(center_delta, target_delta)`) was made EXPLICIT as
  `BalanceGeometry.cap_to_legacy_travel`. The renderer caps (so a block that needs to pass
  the hole stops at contact, letting the hole-aware phase resolve the ordering); the
  packing path does NOT (no hole-aware phase runs after the transform, and a packed block
  has no pre-existing overlap to justify crossing the hole). Pinned by
  `tests/test_collision_balance.py` (both directions, each in both modes) and
  `test_packing_balance.py`.
- Stage 5 shop default — DONE (`445620e`): `job-config.json` now carries
  `"balanced_centering": false` and `JobDefaults.balanced_centering` is the config layer
  of the cascade (label YAML > job YAML > job-config defaults >
  `DEFAULT_BALANCED_CENTERING = False`). The code default stays False until Stage 4
  lands; flipping the shop default is a human call (it changes the engraved output of
  every one-sided-hole job in the library) and must follow Stage 4, or packed and
  unpacked jobs would center differently on the same setting.

## Stage 4 (packing-path parity) — implemented, parity test outstanding

DONE in code: `_label_transform` computes the top/bottom line ink from
`rendered.text_chunks` (grouped by `line_index`, exactly as the renderer groups
its chunk records) and passes it to `_balance_shift_units`, whose body now hands
the solve a `BalanceGeometry` carrying `top_line_ink`/`bottom_line_ink`. Both
adapters share `balance.top_and_bottom_line_ink`, so the line selection cannot
drift. Verified end to end on the coolsys job: the renderer's two passes and the
packer's one pass report the same `delta` for every label (`-0.0758in`).

STILL OPEN: an automated packed == unpacked parity test (the verification above
is a probe run, not a test).

`_center_text_layer_vertically` is the placement for an exported label. The
packing path re-derives that same placement from structured geometry, so this
rule must be mirrored there or a packed label centers the legacy way while an
unpacked one centers balanced:

- `plt_optimizer/generate/plate_optimizer.py:217` `_label_transform`:
  `expected_center = (height/2 - band_center_offset - hole_aware - collision) *
  _UNITS_PER_INCH`, whose docstring says "Mirrors _center_text_layer_vertically
  exactly." The balance shift is a fifth term and is absent.
- It reads `rendered.text_chunks` through `_block_text_y_extents` (:146, which
  mirrors `_collect_hpgl_geometry`'s pen/arc quirks), not a file, so
  `_balance_shift_inches` cannot be called as written.

Plan: factor the solve out of the file parsing into
`collision.balance.balance_shift(label, *, bottom_ink, top_ink, x_min, x_max,
border_ink, holes, band, threshold)` on structured inputs. The renderer feeds it
pens parsed from the file; the optimizer feeds it `text_chunks` extents + the
plate's structural layer (borders/holes are the layer it already parses, :949).
One implementation of the frame/cap logic, and a parity test that asserts
packed == unpacked placement.

## Stage 6 (ink-to-ink gaps) — the authoritative metric

Stages 1–5 measured both gaps against a flat segment as wide as the text block.
On a `round` label that segment's ends reach past the ink into empty space, so B
became the distance from the block's **corners** to the arc — a distance no
engraved stroke participates in. Two labels with identical real clearances
balanced differently depending on how wide their longest line was. Stage 6
replaces both gaps with true ink distances.

### The two distances

Let the **hole-side line** be the text line nearest the lone hole and the
**opposite line** the text line nearest the border opposite that hole. A
one-line label has the same ink for both.

For a **top-hole-only label**:

* `A` — the minimum distance between **the hole ink** and **any stroke of any
  character in the top line**.
* `B` — the minimum distance between **the label border** and **any stroke of
  any character in the bottom line**.

For a **bottom-hole-only label** the definitions are reversed: `B` is the
minimum distance from the border to any stroke of the **top** line, and `A` is
the minimum distance from the hole ink to any stroke of the **bottom** line.

Solver invariant: find the rigid vertical translate where `A == B`.

### Scope — exactly one hole, on the middle of an edge

Recorded verbatim from the user (2026-11-15), answering the structured question
*"Which holes count toward A on the hole side?"* (answer text below, unaltered):

> This whole feature is exclusively for the case when there is only one top hole
> or only one bottom hole. Clearly, if there is only one "top" (i.e. top-middle;
> `location: top`) hole and no other holes, then it is the hole that counts
> towards the hole-text distance. Conversely, if there is only one bottom hole
> and no other holes, then the lone bottom hole is the hole that counts towards
> the text-hole distance.

So the rule applies iff the label carries **exactly one drill hole** (counting
only `diameter > 0`) and that hole's `location` is `top` or `bottom`. Any corner,
edge-side (`left`/`right`) or additional hole makes the label out of scope and
the legacy centering stands. `lone_vertical_hole` is tightened to this predicate;
it currently tolerates extra corner/side holes.

Consequence: the X-overlap adjacency filter (`adjacent_holes`,
`outermost_hole_radii`, `block_x_padding`) has no remaining job — with one hole
on the lone edge it is trivially the hole measured, and its X-overlap test was
only ever a patch for the box metric's width blindness. Removed in Stage 7.

### Search caps stay scalar faces

The band clamp and the hole-side `edge - threshold` cap need scalars, not ink
sets. They come from the **measured line ink extremes**:

* the hole-side face = the hole-side line's ink extreme toward the hole
  (`max` Y for a top hole, `min` Y for a bottom hole);
* the border-side face = the opposite line's ink extreme toward the border.

### Multi-root rule

The box metric was monotone in the shift, so `solve_balance`'s "unique root"
argument held. Real ink against a **curved** border need not be monotone — an arc
can approach the ink, recede, then approach again. Replacement search, recorded
from the user's choice (2026-11-15) on the structured question *"When several
placements equalize A and B, which do we pick?"* — answer: **nearest root to the
shipped placement**.

1. Coarse-scan the travel bracket `[-travel, +travel]` (64 steps) evaluating
   `f(d) = A(d) - B(d)`.
2. Collect every interior sign change.
3. Bisect each to the device grid.
4. Apply the root with the smallest `|d|`; ties break toward the smaller delta.
5. No sign change anywhere → decline, or clamp to the nearest cap and warn (the
   existing partial-solve path).

### Frame rule

The emitted per-label HPGL stream from `render_label_to_plt` is **y-down**
(label y=0 at the top edge); the plate export mirrors it back. The structured
stroke ink (`rendered.text_chunks`) and every quantity the rule consumes — the
content band, `lone_vertical_hole`, the collision threshold, the hole circles —
are **y-up**. The renderer adapter must mirror the file's coordinates
(`height - y`) before measuring, and negate the resulting delta when applying it
to the y-down stream. Skipping this mirror is what made the whole feature inert:
a `top` hole read as sitting below the block, the adjacency test returned no
holes, and `balance_shift` declined on every label.

IMPLEMENTED. `BalanceInputs` carries `top_ink`/`bottom_ink` **as required
fields**, `measure_gaps` measures only ink, and the geometry record threads
`top_line_ink`/`bottom_line_ink` through `BalanceGeometry` so every caller that
owns strokes supplies them.

Measured on real jobs, through both adapters (throwaway probe, removed after the
measurement; re-derive with `scripts/run_integration_test.py <yaml>` plus a
solve wrapper around `_balance_shift_units` / `_balance_shift_inches`):

| label | hole gap | border gap | delta | residual |
|---|---|---|---|---|
| `balance-demo-round` | 0.3008 | 0.3010 | -0.0719 | 0.0002 |
| coolsys `round walmart 3620_0002` | 0.2834 | 0.2841 | -0.062 | 0.0007 |
| coolsys `round walmart 3620_0004` | 0.2832 | 0.2836 | -0.062 | 0.0005 |

Sweep of **all 24 balanced coolsys labels** (48 solves: renderer twice + packer
once per label, all agreeing):

| quantity | min | max | mean |
|---|---|---|---|
| residual `|A - B|` | 0.0001 | 0.0009 | 0.00035 |
| hole-side gap A | 0.2832 | 0.3009 | -- |
| border-side gap B | 0.2832 | 0.3010 | -- |
| applied delta | -0.0738 | -0.0620 | -- |

Every residual is at or below one device unit (0.000984in), so all 24 labels are
exact solves -- none fell back to a partial root.

The residual is a device-unit rounding artefact (1 unit = 0.00098in), i.e. the
gaps are equal to the plotter's own resolution. Under the box metric the same
coolsys label reported 0.75/0.25 -- visually half an inch off -- which is the
whole reason the metric changed.

NOTE the decline seen on the first renderer pass of every coolsys label: that
pass runs on the pre-collision label (different `legacy_travel_inches`, no
adjacent hole in X) and returns `None`; the second pass, on the final label,
solves exactly. Both are renderer passes -- no stale state.

### Stage 7 -- the box metric is gone

Removed once Stages 6 and 4-6 were green: `BalanceInputs.top_box` /
`bottom_box`, the `box_loop()` helper that turned a box into a polyline, the
`circle_aabb_gap` import, and every fallback branch in `measure_gaps`. The
fallback was the trap this whole project was about -- a caller that forgot its
strokes silently got the corner metric with no error -- so ink is now a
required field and `balance_shift` declines when a line engraves nothing.

Kept, because grep shows them live and not dead: `adjacent_holes()` (the
X-overlap filter, balance.py:618), `block_x_padding` (its reach parameter,
balance.py:582) and `circle_aabb_gap` (collision detection's own metric,
`detection.py:214`). Removed as dead: `outermost_hole_radii()`, whose only
references were its own `__all__` entry and two tests.

The kernel tests kept their numbers by modelling each fixture line as its
rectangular outline: outside the block an outline and a filled box measure the
same gap, so 0.40/0.75 still pin the fixture while every value the solver reads
comes from strokes.

### Ink sources

* Border ink: `label_renderer._section_border_ink_in_inches` (pen-state-aware:
  arcs sampled, `PU` rapid moves never counted as ink).
* Hole ink: the emitted, kerf-compensated drill circle (`BalanceCircle`).
* Text ink: `rendered.text_chunks[i].blocks[j].strokes[].segments[]` flattened
  to polylines. `ArcSeg` is **sampled** (`arc_polyline`, never the chord — the
  PLT fonts are arc-bearing, and a 90° chord on a 1016-unit arc is off by
  ~0.147in, larger than the whole clearance being measured). Sampling happens
  once per label, outside the bisection loop.
