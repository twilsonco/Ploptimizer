# AGENTS.md - Text/Hole Collision & Drill Holes (`plt_optimizer/generate/collision/`)

Doc-only directory: the code lives in the parent - `../geometry.py` (distance
primitives), `../label_renderer.py` (detection + avoidance phases),
`../resolution.py` (`hole_engraved_radius`). Parent contracts:
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

- **Phase 1 (always on, observational):** `_detect_text_hole_collisions()`
  flags per-(line, hole) threshold violations. `RenderedLabel.collision_detected`
  marks any render where a collision was found (even if a later phase fixed
  it) and `RenderedLabel.has_collisions` marks renders that still collide
  after resolution. The per-(line, hole) detection messages (label id, line
  index, hole location, measured gap, and the required-clearance breakdown
  of clearance + stroke floor) are logged once the outcome is known: at
  WARNING when an avoidance phase resolves them, at ERROR when they remain
  unresolved. Detection runs inside `render_label_to_plt` (and the
  collision-avoidance sweeps re-check via `_detect_text_hole_collisions()`).
- **Phase 2 (opt-in via `min_hole_margin`):** sweep `hole_margin` toward the
  floor (analytical — hole positions are pure functions of `hole_margin`);
  on success re-render from the adjusted label clone and log WARNING with
  before/after margins and the offending text line. (Any geometry-altering
  avoidance action — margin reduction or collision compression — always logs
  at WARNING and names the label id plus the affected text line.)
- **Phase 3 (opt-in via `max_h_compress`):** if margins cannot clear the
  overlap, sweep a uniform horizontal compression
  (`collision_compress_by_line` on `ResolvedLabel`, applied before
  margin-driven compression) up to the line budget floor
  `1 - max_h_compress`. Stacks on top of the Phase 2 floor.
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

- **Single geometry chokepoint**: `resolution.hole_engraved_radius(diameter,
  cutter_diameter, adjust)` is the pure math; the only call site is
  `label_renderer._hole_circles_local`, which is the single source of hole
  circles for BOTH the emitted `AA` arcs (`_render_holes_hpgl`) and the
  text-hole collision checks (`_detect_text_hole_collisions`, the Phase 2
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
  diameter, the historical bit-identical behaviour). No `_LineEntry` /
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

