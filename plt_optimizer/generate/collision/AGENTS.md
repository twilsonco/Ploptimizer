# AGENTS.md - Text/Hole Collision & Drill Holes (`plt_optimizer/generate/collision/`)

The collision system lives in this package: `geometry.py` (the signed
circle-vs-AABB gap, the circle-vs-ellipse primitives, `CollisionResult`),
`shapes.py` (the label-shape predicates shared by renderer + holes + checks),
`holes.py` (drill-hole circle placement in the label frame + the native
four-arc `AA` emission), `detection.py` (the stroke-aware threshold,
detection, the opt-in avoidance phases (hole-margin reduction, edge-margin
slide, alignment switch + compression), diagnostics, and the
`assert_no_collisions` job gate). The renderer
(`../label_renderer.py`) orchestrates the phases inside `render_label_to_plt`
and injects its text renderer into the compression sweep (the `render`
callback), so this package has zero runtime imports from `label_renderer`.
`../resolution.py` keeps `hole_engraved_radius` (the cutter-compensation
chokepoint). Parent contracts:
[`../../../AGENTS.md`](../../../AGENTS.md), [`../AGENTS.md`](../AGENTS.md).
The sections below moved verbatim from the old root AGENTS.md section 6.

### Text-Hole Collision Avoidance

Rendered text lines are checked against drill holes via the circle-vs-AABB
closest-point gap in `geometry.circle_aabb_gap()`. The check is
**stroke-aware**: a (line, hole) pair collides when the geometric gap falls
below the per-line threshold

`threshold = 0.5 * (hole_cutter + text_cutter) + hole_text_collision_distance`

where `text_cutter` is the line's resolved cutter diameter and
`hole_cutter` is the boundary/hole cutter from `tools.json`
(`boundary_hole_cutter_size`, default 0.015, snapped to
`available_cutters`: next size down, else next size up). The first term is
the stroke floor at which the two cut strokes just touch, so near misses
that would make engraved strokes bleed together are collisions too; a gap
exactly equal to the threshold is safe. Detection runs in the label-local
y-up frame, shifted by `height / 2` to match export centering; Y-flip is
intersection-invariant.

- **Phase 1 (always on, observational):** `detect_text_hole_collisions()`
  flags per-(line, hole) threshold violations. `RenderedLabel.collision_detected`
  marks any render where a collision was found (even if a later phase fixed
  it) and `RenderedLabel.has_collisions` marks renders that still collide
  after resolution. The per-(line, hole) detection messages (label id, line
  index, hole location, measured gap, and the required-clearance breakdown
  of clearance + stroke floor) are logged once the outcome is known: at
  WARNING when an avoidance phase resolves them, at ERROR when they remain
  unresolved. Detection runs inside `render_label_to_plt` (and the
  collision-avoidance sweeps re-check via `detect_text_hole_collisions()`).
- **Phase 2 (opt-in via `min_hole_margin`):** sweep `hole_margin` toward the
  floor (analytical — hole positions are pure functions of `hole_margin`);
  on success re-render from the adjusted label clone and log WARNING with
  before/after margins and the offending text line. (Any geometry-altering
  avoidance action — margin reduction, edge-margin slide, or collision
  compression — always logs at WARNING and names the label id plus the
  affected text line.)
- **Phase 3 (opt-in via `max_h_compress`): the edge-margin slide.**
  Horizontal compression cannot free a `left`/`right`-aligned line from a
  hole on its *anchored* side: the anchored edge stays pinned at the margin,
  so compressing only shortens the far end. `resolve_via_edge_margin()`
  therefore increases the colliding line's edge margin (slides it off the
  near-side hole) by the closed-form shift that clears the collision — a
  line translate is a rigid AABB move, so `circle_aabb_gap` is monotonic in
  the shift and the shift solving the threshold is algebraic (no sweep, no
  re-render; the caller verifies with one render). The shift is capped, per
  far-side hole, by the shift at which that hole *starts* colliding (slide
  until a hole collides on the far side, never past it). **The content-area
  box is NOT a cap**: a slide that pushes the line's far end past the far
  margin is absorbed by the renderer's margin-fit compression (Pass A
  reserves each slid line's edge margin in its compression budget), so the
  box is a compression constraint, never an alignment-stopping one. The
  state rides `collision_edge_margin_by_line` on `ResolvedLabel` and lands
  in `resolution.compute_horizontal_offset(edge_margin=...)` (`left` →
  `margin + shift`, `right` → right margin − `shift`; inert on `center`,
  which has no anchored edge). When `align_margin_adjust_global` (cascade
  label → job → `job-config.json`, default **false**) is enabled, every
  other line of the label sharing the colliding line's exact
  `text_h_alignment` matches the increased margin (the group's anchored
  edges stay flush); the shared shift is the tightest (required × far-hole
  cap) product among the lines that must move, and **every receiver takes
  that full shift** (each capped only by its own far-hole limit). A receiver
  whose `max_h_compress` budget cannot absorb the resulting box overflow
  fails the job (`_check_shift_fits` raises `LabelRenderError`) — the flush
  the sharing guarantee demands is impossible, the same contract as an
  unresolvable hole collision. The renderer **re-runs Phase 3 to a fixed
  point** (bounded by the line count): the shared shift is the tightest need
  among the colliding lines, so a line needing marginally more stays a hair
  under the threshold after the first slide, and each round fully clears (or
  caps out) the tightest colliding line. Re-running lets it catch up — every
  line ends at the largest need — instead of escalating to the Phase-4
  switch, which would move that line's anchored edge and drag the whole flush
  group far inward. The phase is gated on the same
  `max_h_compress > 0` budget as Phase 4: a job with compression disabled
  behaves exactly as before this phase existed.
- **Phase 4 (opt-in via `max_h_compress`): alignment switch + compression.**
  A colliding line that is still edge-aligned when this phase runs has
  exhausted its slide (a hole collides on BOTH sides), so
  `switch_edge_aligned_to_center()` flips it to `center`
  (`collision_center_by_line` on `ResolvedLabel`) — centering makes the
  compression act symmetrically on *both* ends. The switch alone is checked
  first; if collisions remain, sweep a uniform horizontal compression
  (`collision_compress_by_line`, applied before margin-driven compression)
  up to the line budget floor `1 - max_h_compress`, per colliding line.
  Stacks on top of the Phase 2 floor and the Phase 3 slide.
- **Phase 4 tail (opt-in via `align_margin_adjust_global`): the sibling
  flush.** A switched line's centering + compression repositioned its
  anchored edge, so the unswitched same-alignment siblings (which matched
  the Phase-3 *margin*, not the switched line's *final edge*) are no longer
  flush. `flush_siblings_to_switched_edge()` sets each unswitched
  edge-aligned line's edge margin to the switched lines' final anchored
  edge — the LEFTMOST final left edge for `left` groups (mirrored: the
  rightmost final right edge for `right`), the tightest so no sibling moves
  further than it must. The switched lines are left to their
  auto-adjustment. Sliding inward shrinks the room left for a sibling's far
  end: the box overflow is absorbed by Pass A's margin-fit compression (the
  flush reserves each receiver's slide room in its compression budget), and a
  receiver whose `max_h_compress` budget cannot absorb it fails the job
  (`_check_shift_fits` raises, the same contract as Phase 3 sharing). A flush
  that lands a sibling on a far-side HOLE sweeps compression
  (`collision_compress_by_line`) to clear it. The renderer
  (`_apply_edge_flush` in `render_label_to_plt`) runs the flush on EVERY
  collision-free return path — including the initial clean render — because
  a later pipeline pass (e.g. the cutter-downsize re-measure) moves the
  switched lines' final edges, so the flush must be re-applied to the
  geometry that is actually emitted. The flush is verified with a full
  re-render + re-detection; only a collision-free flush is kept.
- **Measurement probes run with avoidance OFF.** The `h_compress_global` and
  cutter-downsize pre-passes measure a label's per-line scales by rendering
  it, and the final pass re-derives its own avoidance — so a probe that
  baked a *collision* scale into a shared *typography* decision (or a
  downsize ladder) double-charged it: the final render paid compression its
  own geometry never asked for. Both probes therefore call
  `render_label_to_plt(label, collision_avoidance=False)`, a mode that runs
  detection only (so `collision_detected` still marks the render) and fires
  no phase, leaving `compression_by_line` as pure margin-fit typography.
- **Failure semantics (only unavoidable collisions fail the job):** a
  collision that an enabled phase resolves logs its detections at WARNING
  (plus the avoidance action's own WARNING) and the render proceeds —
  `collision_detected=True`, `has_collisions=False`. When no enabled phase
  clears a collision, `render_label_to_plt` logs the detections plus full
  diagnostics (gap shortfalls, margin/compression state, recommendations)
  at ERROR and flags `has_collisions=True`. The job-level gate
  `assert_no_collisions()` — wired into `layout.generate_layout_with_bounds`
  (covering `vectorize.export_per_cutter_plts`) — raises `LabelRenderError`
  once every label has been rendered (so all per-label ERRORs print
  first), naming every unresolved label id. The `generate` CLI surfaces the
  abort as a non-zero exit code.
- Adjusted label clones propagate to downstream rendering via
  `RenderedLabel.source_label` (consumed by `layout.unroll_labels_with_rendered_bounds`).

### Cutter-Compensated Drill Holes (`hole_adjust_for_cutter`)

The drill path is the circle the cutter **centre** follows, so the cutter
engraves a stroke half a cutter width on each side of it: a path of diameter
`D` engraves a hole of `D + cutter`. A spec diameter of 0.1in cut by the
0.015in boundary/hole tool therefore engraves a 0.115in hole. 
`hole_adjust_for_cutter` (default **true**) shrinks the **emitted** circle by
the cutter diameter — the cutter radius on each side of the path — so the
engraved hole measures the specified diameter: a 0.1in hole with a 0.015in
cutter emits a 0.085in circle. Cascades label → job → `job-config.json`
(plate parity, like every render-affecting field — labels render before
packing); the code fallback is `DEFAULT_HOLE_ADJUST_FOR_CUTTER = True`.

- **Every "cutter" here is the ENGRAVED width**, not the nominal tool: the
  callers pass `ResolvedLabel.effective_hole_cutter_diameter` (the physical
  boundary/hole tool plus the cascaded `cutter_buffer_routing` kerf buffer) and
  `ResolvedTextLine.effective_max_cutter_diameter` (the widest tool the line
  engraves with, plus `cutter_buffer_text`). A roughly 45-degree-flank V-bit
  removes more material than its name suggests, and both the hole compensation
  and the collision stroke floor are width-of-material-removed questions. A 0.0
  buffer (the code default) makes both the identity, reproducing the historical
  nominal-diameter geometry byte-for-byte. See
  [`../AGENTS.md`](../AGENTS.md) "Physical vs. effective cutter diameter" for
  the full invariant table.
- **Single geometry chokepoint**: `resolution.hole_engraved_radius(diameter,
  cutter_diameter, adjust)` is the pure math; the only call site is
  `collision.holes.hole_circles_local`, which is the single source of hole
  circles for BOTH the emitted `AA` arcs (`_render_holes_hpgl`) and the
  text-hole collision checks (`detect_text_hole_collisions`, the Phase 2
  margin sweep) — collision detection measures the circle that is actually
  engraved, so a near miss relaxes by exactly the half cutter, with zero
  changes at the collision sites.
- **Centres never move**: the centre offset keeps using the **specified**
  radius (`hole_margin + cutter/2 + diameter/2`), so every placement rule
  (rect edges, round-label ellipse diagonal, rounded-rect patch) is
  bit-identical with the mechanism off — only the drawn circle shrinks.
  `ResolvedHoleSpec.diameter` stays the nominal spec value (never mutated),
  which also keeps `_compute_hole_aware_shifts` bit-identical — provably
  correct: the shift `(D_top − D_bot)/2` cancels the common cutter.
- **Clamp, never a point**: a specified diameter at/below the cutter
  diameter leaves no path to draw; the radius clamps to
  `MIN_HOLE_EMITTED_RADIUS_INCHES` = **one plotter unit**
  (`1 / HPGL_UNITS_PER_INCH`), with a WARNING naming label id, location,
  diameter and cutter. NOT `_INK_EMISSION_FLOOR` (half a unit):
  `inches_to_units` is `int(round(v * 1016))` and Python's `round(0.5) == 0`
  (banker's rounding), so a half-unit radius emits a degenerate `AA` arc.
- **The flag rides `ResolvedLabel.hole_adjust_for_cutter`** (explicit-`None`
  cascade label → job → config → true; explicit `false` emits the specified
  diameter, the historical bit-identical behavior). No `_LineEntry` /
  `RenderedLabel` field; the layout report prints a per-label finding
  `Holes 0.125in -> 0.110in (cutter 0.015in)` (one line per distinct
  specified diameter, compact-mode finding) from
  `layout_report._hole_adjust_lines`.
- Fixture: `tests_deps/hole_adjust_for_cutter_job.yaml` (pinned by
  `tests/test_phase3_export.py::TestHoleAdjustForCutterDemoExample`) — the
  default 0.1 → 0.085in, the `opt_out` twin (0.1in emitted, centres identical
  to the on-label), mixed diameters (0.125 → 0.110, 0.25 → 0.235), the
  degenerate 0.01in clamp + WARNING, and a round label's corner holes. Unit
  tests: `TestHoleAdjustForCutterField` (`tests/test_schema.py`),
  `TestHoleEngravedRadius` / `TestHoleAdjustForCutterCascade`
  (`tests/test_resolution.py`), `TestCutterCompensatedHoles`
  (`tests/test_round_labels.py`), `TestHoleAdjustFindings`
  (`tests/test_layout_report.py`), and the `hole_adjust_for_cutter`
  job-config key tests (`tests/test_job_config.py`).

