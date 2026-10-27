# AGENTS.md - Plate-Space Routing & Bin-Packing (`plt_optimizer/generate/packing/`)

The packing engines live in this package: `packing/layout.py` (the `rectpack`
bounds-aware multi-heuristic engine for rectangular labels - rotation, fill
order, plate clearances, material partitioning) and `packing/ellipse_packer.py` (the
pure-shapely grid-search engine for `type: round` labels). Plate-frame TSP
*routing* is not packing and stays in `../plate_optimizer.py`; assembly/export
in `../vectorize.py`; the shared routing pipeline in `../core/` (see
[`../../core/AGENTS.md`](../../core/AGENTS.md)). Parent contracts:
[`../../../AGENTS.md`](../../../AGENTS.md), [`../AGENTS.md`](../AGENTS.md).
The sections below moved verbatim from the old root AGENTS.md section 6.

### Plate-Space Toolpath Optimization
Generated toolpaths are optimized in the **plate frame** (post-rectpack device
coordinates) before any PLT is written, one routing problem per plate **and per
cutter** (`plt_optimizer/generate/plate_optimizer.py`). Because the generator
already knows each toolpath's kind, the `Profiler` is skipped entirely.

- **Text layers**: one TSP node per `TextChunkRecord` carried out of label
  rendering (label-local inches, +y up). The chunker is bypassed — the records
  *are* the nodes. `text_chunk_mode` (job-level, `"line"` default / `"word"`)
  sets granularity: `line` routes each whole line; `word` splits on whitespace
  (exact stroke membership via `ftext_renderer.render_text_line_ftext_with_words`,
  which partitions the whole-line render's contours by translation-invariant
  signature — bit-exact, no advance math).
- **Structural layers** (borders SP2 + holes SP3): the extracted HPGL is parsed
  and pushed through the shared core pipeline (`core.pipeline`
  `preprocess_document` → `chunk_document` → `optimize_and_reassemble`) with a
  hand-built `ProfileResult(is_structural=True)`; the structural chunker branch
  maps every path 1:1 to a block.
- **Re-emission**: `emit_layer_document` writes integer-unit HPGL
  (`PU`/`PD`/`AA`) framed by the shared `PLT_HEADER` (`IN;PA;`) and
  `PLT_FOOTER` (`SP;`) constants from `label_renderer`. Per-cutter files
  carry **no `SP` pen selects** (each file is a single logical layer; the
  pen map only groups content upstream); every `StrokePath` is PU-led, and
  `extract_pens_from_plt_text` rewrites any section-leading bare `PD` to
  `PU{first_pair};PD{rest}` so dropping the pen-select resets can never
  join adjacent sections with a phantom cut. Written files end with
  `SP;\n`. The generic `PLTWriter` is not used here — it hoists all headers
  ahead of the geometry.
- **Transform chain** (verified against emitted files, plotter units): center
  text block to `height/2` → Y-mirror by the rendered bounds sum → rotate 90° CW
  if the packer placed the label sideways → translate by the packed slot. Text
  geometry is emitted vertex-exact; only coincident structural strokes are
  deduplicated.
- **Strategy**: `ParallelEnsembleStrategy` by default, `NearestNeighbor2Opt`
  under `--fast-mode` (mirrors the `optimize` CLI). `export_per_cutter_plts`
  takes `fast_mode` and `logger`; each layer logs baseline→optimized rapid
  travel at INFO.
- **Direction sweep** (`core/direction_sweep.py`, always on): after the
  strategy fixes the block (chunk) ORDER, `optimize_and_reassemble` re-picks
  each block's forward/reverse traversal to minimise inter-chunk rapid travel,
  alternating forward+backward greedy sweeps to a fixpoint (cap 10 passes,
  kwarg `direction_sweep: bool = True` as the escape hatch). It runs post-unwrap
  in the parent process (ensemble workers rebuild fake single-segment blocks),
  is strictly monotone (the objective never increases; on the generate path the
  emitted `rapid_distance()` is non-increasing since intra-chunk gaps are
  reversal-invariant), and repairs the latent 2-opt staleness (2-opt reverses a
  tour segment without flipping the `reversed` flags). Fires on generated
  multi-line text layers (e.g. the seer wbuv plate: 140979→57734 units, 59%) and
  on parsed fixtures under NoOp; on tours whose construction already picks
  optimal directions it finds no gain and stays silent. When a pass is accepted
  it logs INFO with BOTH metrics (inter-chunk + emitted rapid travel), appends
  `direction_sweep=before->after (N passes, M flips)` to `method_notes`, and
  populates the `OptimizationOutcome.direction_sweep_*` fields (all-or-nothing:
  set only when `passes>0`); `vectorize._report` breaks the layer line into
  `routing` (emitted baseline→emitted optimized, both intra+inter),
  `inter-chunk` (strategy objective), and the `direction sweep` clause.
- **Intra-chunk glyph sweep** (`core/glyph_sweep.py`, always on, generate path
  only): the direction sweep reverses whole chunks, which leaves the rapid
  travel *inside* a chunk exactly invariant — and on generated text that
  intra-chunk share is the majority of the emitted travel. The renderers know
  which strokes belong to which character, so `TextChunkRecord.glyph_groups`
  carries the per-glyph stroke partition (PLT: contiguous by construction from
  the `_walk_line` cursor walk; TTF: glyph-major contour slicing by cached
  per-char contour counts, `glyph_groups_for_line`),
  `plate_optimizer.build_text_blocks_with_glyphs` remaps it onto device paths,
  and `optimize_and_reassemble` (kwargs `intra_sweep: bool = True` +
  `glyph_groups_by_block`) runs `sweep_glyph_directions` per chunk **after**
  the inter-chunk routing is final: with the chronological glyph order fixed
  and the chunk entrance/exit pinned (the group owning the first traced path
  must start at the chronological entry, the one owning the last must end at
  the chronological exit — `_PIN_TOLERANCE`), the optimal per-glyph traversal
  is a shortest path through a chain whose nodes are that glyph's feasible
  traversals, solved exactly by DP; ties resolve to chronological and the
  all-forward traversal is always feasible, so the sweep is monotone and the
  inter-chunk tour (strategy + direction sweep) stays exactly valid.
  - **Per-stroke direction (`_group_modes`)**: a glyph's mode set is the union
    of the historical all-or-nothing pair (stored stroke order traced forwards
    / reversed order traced backwards) and **every per-stroke direction
    assignment on both stroke orders**, so a glyph can reverse just the strokes
    that want reversing (the TODO's "reversing some strokes"). The historical
    pair is a subset, so the optimum can only improve. Traversals are
    deduplicated by endpoint pair (entry/exit depend only on the first/last
    stroke and its direction, so 16 traversals of a 3-stroke glyph collapse to
    8) keeping the cheapest per pair; enumeration is capped at
    `MAX_EXACT_STROKE_MODES` = 10 strokes (the shipped font library peaks at 8,
    99% of glyphs at ≤3), above which the sweep falls back to the two-mode
    pair — a strict subset, so a pathological glyph is never worse. Reversal
    itself stays the Reassembler's job (`reassemble(..., intra_chunk_results=)`;
    `None` entries keep the chronological order, and block-level reversal
    composes by flipping both the sequence and every direction). Parsed PLTs
    (optimize/watch) carry no glyph knowledge, so the parsed path stays
    byte-identical. Fires on generated text: across the seer/wb-uv/intrachunk
    PLT+TTF/sunwest specs the sweep cuts emitted rapid travel 41.4% vs the
    pre-per-stroke sweep's 34.8% (**−10.2% emitted**, 289 → 527 flips); cutting
    geometry is exactly preserved (segment counts identical, arcs stay native).
    On improvement it logs INFO (intra + emitted rapid travel), appends
    `intra_sweep=before->after (N flips)` to `method_notes`, populates the
    `OptimizationOutcome.intra_sweep_*` fields (all-or-nothing: set only when
    `flips>0`), and `vectorize._report` adds the `intra sweep` clause.
  - **Order freedom is measured and rejected** (do not re-attempt the
    "optimize each character, then each word, then order the word chunks"
    hierarchy): real fonts give 2–3 strokes per glyph, and with the chunk
    entrance/exit pinned, chronological (reading) order **is** the optimal
    tour — a glyph/word-order DP gains exactly 0.00%. Freeing the endpoints to
    chase it is catastrophic: the plate TSP and direction sweep exploit a
    chunk's entrance/exit sitting at its left/right edges, so landing the
    entrance mid-word cost +29.7% emitted travel on those specs (+52.8% on
    sunwest). Direction is the entire intra-chunk win.
- **Coincident-stroke merge** (`core/path_merger.py`, always on, final stage):
  after reassembly, consecutive paths whose junction is tip-to-tail stitch into
  one continuous cut, so the redundant tool-up between them disappears. The
  predicate is the writer's own PU-suppression test
  (`PLTWriter._format_stroke_path`, same `COORD_TOLERANCE` = 1e-3): merge
  `tail → head` only when `tail.segments[-1].end` is within tolerance of BOTH
  `head.pen_up_position` (when set) and `head.segments[0].start`, so every
  genuine rapid survives — including arc-native glyph plunges
  (`PU x,y;PD;AA…`). Collapsing is transitive (a run of N tip-to-tail paths
  becomes one path carrying the run's first `pen_up_position`), order-preserving,
  and segment-less paths pass through verbatim as barriers. The transform is
  **metric-neutral**: a merged junction contributes ~0 to
  `PLTDocument.rapid_distance()`, so the win is PU count, path count, and bytes
  — not rapid travel (the undirected segment multiset and cutting distance are
  invariant). It makes the writer's long-standing suppression a real document
  transform, which is what the **generate** path needed: `emit_layer_document`
  emits every path PU-led and kept every tool-up (the integration bh layer
  merges 10 → 3 paths); the parsed path's emitted files are unchanged because
  the writer already collapsed those PUs. Runs in the parent after the sweeps
  (kwarg `merge_coincident: bool = True` as the escape hatch, threaded through
  `plate_optimizer.optimize_text_layer` / `optimize_structural_layer`), so the
  sweeps' `emitted_before` measurements stay pre-merge while `emitted_after`
  describes what is written. On merges it logs INFO, appends
  `merge=before->after (N merge(s))` to `method_notes`, populates
  `OptimizationOutcome.merged_paths_before/after` + `merges_applied` (the counts
  are set whenever the merge *ran*, unlike the sweeps' all-or-nothing fields —
  `merges_applied=0` with non-None counts means "ran, nothing to do"), and
  `vectorize._report` adds the `path merge` clause. Deliberately NOT inside
  `Reassembler.reassemble` (which stays a pure traversal applier, keeping its
  path-count tests and the direct-caller NoOp byte identity intact).
- The post-write `_run_optimizer` (parse each file → profile → optimize) is
  **removed**; optimization now happens pre-write in plate space.


### Bin-Packing Rotation (`allow_rotation`)
`JobSpec.allow_rotation` (bool, default `True`) lets the `rectpack` bin
packer test both orientations (0°/90°) for every label instance.

- `_pack_best()` (layout.py) evaluates every `PACK_CONFIGS` heuristic twice
  (rotation off, then on) and ranks candidates by `(footprint, rotated_count)`
  with a float-tolerant footprint comparison. Labels therefore rotate only
  when rotation *strictly* improves the used bounding-box area; at equal
  footprints the all-horizontal layout always wins (backward compatible).
- Rotation is detected in `_extract_packed_plates()` by comparing
  `rect.width` against the *packing* width carried in the `rid` payload
  (rendered bounds in `generate_layout_with_bounds`, nominal otherwise) —
  never against the nominal `ResolvedLabel.width`, which can differ from
  what was actually packed.
- Rotated content is turned 90° **clockwise** at plate assembly:
  `vectorize.rotate_plt_content_90cw()` maps `(x, y) → (y_max − y, x − x_min)`
  over `PA`/`PU`/`PD` pairs and `AA` arc centers (sweep angles preserved —
  a pure rotation has positive determinant), normalizing the rotated bbox to
  the origin so the existing slot translation lands it in `[x, x+H] × [y, y+W]`
  with non-negative coordinates. Text, border and drill holes rotate together
  because they share one `plt_content` string.
- Unbounded-mode `LayoutFitError` wording is orientation-aware ("in either
  orientation"): a label that fails 24x16 upright may still fit sideways.
- Example fixture: `tests_deps/rotation_demo_job.yaml` (pinned by
  `tests/test_layout.py::TestRotationDemoExample`) — a tight 24x10 scrap
  sheet where rotation-required, rotation-refused and opportunistic-rotation
  labels coexist; it aborts with `LayoutFitError` when rotation is disabled.
  The fixture pins `layout: rows` because its banner-must-rotate and abort
  expectations are row-frame specific.

### Bin-Packing Fill Order (`layout`)
`JobSpec.layout` (str-Enum `LayoutMode`, default `columns`) sets the
preferential plate fill order. `columns` fills each plate's **height** before
extending rightward (so the used area grows up the engraving area first and
the unused material stays one clean rectangular block for scrap reuse);
`rows` fills width before extending downward (the historical behavior). It
cascades job → plate: `PlateSpec.layout` (`None` = inherit) overrides per
plate. Threading mirrors `allow_rotation` (read from `JobSpec` at the CLI /
integration-test call sites, passed as a keyword into
`vectorize.export_per_cutter_plts` → `layout.generate_layout*` → `_pack_best`);
it is a global packing concern and never enters `ResolvedLabel`.

- `rectpack` cannot be *sorted* into column-major: for identical rectangles
  every `SORT_*` is a stable no-op and the emergent fill order is purely the
  placement heuristics' tie-breaking. `PACK_CONFIGS` (Guillotine/MaxRects)
  fill the plate's **long axis** first regardless of frame, so transposing
  alone leaves the real-space order unchanged (verified empirically).
- `columns` therefore packs in a **transposed frame** (bins `(W,H)→(H,W)`,
  rects `(w,h)→(h,w)` via `_transpose_entries`) paired with
  `PACK_CONFIGS_COLUMNS` = `SkylineBl`, whose bottom-row fitness always fills
  the packer's X axis — the real plate height — first. `_pack_best` returns
  packer-space coordinates; callers extract with
  `_extract_packed_plates(packer, transpose=True)`, which maps
  `(x, y, w, h) → (y, x, h, w)` and the bin `(w, h) → (h, w)` back to plate
  space. `rows` runs `PACK_CONFIGS` in the real frame verbatim (bit-identical
  to the historical layouts).
- **`columns` is text-oriented, not plate-oriented.** Because the transposed
  frame often *rotates* most labels, a naive plate-frame column fill would be
  reader **row**-major once the sheet is turned to read the engraving. So
  after extraction, `_extract_packed_plates` (transposed path only) re-assigns
  instance ids onto the unchanged slots via `_reorder_ids_for_reader_order`:
  slots are grouped by the *natural* (unrotated) dims they demand and, within
  each interchangeable group, the content-ordered ids (the 4th `rid` element,
  `seq`) are re-paired onto slots sorted by `_reader_order_key` — plate
  `(x, y)` for unrotated labels, plate `(y, -x)` for rotated ones (reader
  column-major: a rotated label's column is a plate-Y band filled
  right-to-left in plate X, i.e. true top-to-bottom after turning the sheet
  90° CCW). `plate.labels` is returned in this reader order too (assembly and
  the plate optimizer's TSP baseline follow it). Only `(label_id,
  source_label)` pairs move, so each plate keeps its exact id set and slot
  geometry; an all-horizontal `columns` pack is a bit-identical no-op.
  `rows` mode is never reordered.
- Rotation detection is unchanged by the transpose: `_transpose_entries`
  rebuilds the `rid` payload with the *packer-space* width, and a packer-space
  90° swap composed with the transpose is again a 90° swap, so
  `_extract_packed_plates` still compares `rect.width` against `rid[2]` and
  `PackedLabel.rotated` keeps meaning "content turned 90° CW at assembly".
  Instance ids advance bottom-to-top within a column (rectpack y-up).
- `_plate_footprint` (used bounding-box area) is the selection metric for
  both modes and is transpose-symmetric, so the tight-rectangular-block
  objective is orientation-agnostic (no aspect penalty by design).
- Per-plate modes: `_resolve_plate_groups` splits the plate list into maximal
  runs of equal effective mode; `_pack_groups` packs those groups
  sequentially in declaration order, each receiving only the previous group's
  leftovers. A single group (every unbounded job and every uniform-mode job)
  therefore behaves exactly like a one-pass pack. Unbounded mode uses the job
  value.
- Example fixture: `tests_deps/columns_demo_job.yaml` (pinned by
  `tests/test_layout.py::TestColumnsDemoExample`) — 16 3x1 labels on a 24x16
  sheet pack into one full-height column (bounding box 3x16).

### Plate Edge Clearances (`left_clearance` / `top_clearance`)

`PlateSpec.width`/`height` describe the **usable** pack area. Two optional
fields shift that area within the physical material to account for scrap
that does not start at the sheet's left/top edge:

- `left_clearance` (default `0.0`): unused material along the plate's
  **left** edge; shifts the pack area rightward.
- `top_clearance` (default `0.0`): unused material along the plate's
  **top** edge; shifts the pack area downward.

Both fields also exist at the **job level** (`JobSpec`, default `None` =
unset) and cascade job -> plate like `layout`: `JobSpec._apply_job_level_clearances`
fills every plate that omits the field, while an explicit plate value
(including an explicit `0.0`) always wins; an explicit plate `null` counts
as unset (a `PlateSpec` before-validator drops the key). The job-level
value additionally drives unbounded mode: the CLI / integration runner
read it off the parsed `JobSpec` (`_default_plate_clearance(job)`, after
job-config injection) and thread it into the export/layout calls as
`default_plate_clearance`, so auto-allocated bins shift identically.

Bottom and right clearances need no fields: in the emitted plate frame
(origin at the material's top-left, +y downward — the HPGL device
convention) the usable area spans `[left_clearance, left_clearance + width]`
horizontally and `[top_clearance, top_clearance + height]` vertically, so
the material's right edge always sits at `left_clearance + width` and its
bottom edge at `top_clearance + height`.

Packing is unchanged — the packer
still receives the usable `width × height` bin — and
`_extract_packed_plates` adds `(left_clearance, top_clearance)` to every
placement (in real plate space, after any transpose mapping).
Zero-clearance output is therefore bit-identical to the historical
behavior. `_plate_clearances` builds the per-bin-id `(left, top)` map
(omitting all-zero plates) that `_pack_groups` → `_pack_group` →
`_extract_packed_plates` threads through.

### Material Partitioned Packing (`material`)

`material` (free-form string, default `null`) groups labels by the stock they
are cut from, so one job YAML can carry a mixed-material batch. Declared on
`LabelAttributes` (cascades label → job, inherited by `LabelSpec` and
`JobSpec`; NOT on `TextLine`/`TextAttributes`) and on `PlateSpec` (cascades
plate → job like the clearances; an explicit plate `null` counts as unset).
`normalize_material` trims and rejects empty/whitespace-only names; matching
uses `material_key` (trim + casefold) while the first-declared spelling is kept
for display. Not a `job-config.json` key — per-job decision only.

- `PackedPlate.material` carries the plate's material out of the layout engine;
  `_split_material_entries` groups entries by material key (insertion order =
  content order) and `_pack_entries_with_pinners` runs **one packing pass per
  material**: materials never share a plate.
- Constrained mode: plates declaring `material` join exactly their group's pool
  (`_claim_plates_by_material`); a declared material no label uses leaves the
  plate unused (WARNING); material-less plates spread across the groups
  (fewest-claimed first, declaration order as tie-break) so no group starves.
  A group with no pool aborts with `LayoutFitError` ("no plate to pack onto"),
  a group that overflows its pool aborts with "Materials never share a plate"
  (no partial leftover propagates). Pinned labels whose materials conflict
  with their plate's material abort ("carry conflicting material").
- Unbounded mode: one auto-allocated bin pool per material,
  `<sanitized>_default_plate_{i}` (sanitized = `[^0-9A-Za-z]` stripped; the
  display material is the group's first-declared spelling).
- Plate-level `replacement_text_file` labels inherit the declaring plate's
  material at expansion (`substitution`), the only point where the plate →
  label direction is applied (labels resolve before plate assignment).
- Output filenames gain the material tag (see
  [`../../cli/AGENTS.md`](../../cli/AGENTS.md)): `wb_uv_45_txt_<job>.plt`
  (tag sanitized by `core/filenames.sanitize_filename_component`; the bin **ids**
  above keep their own `[^0-9A-Za-z]`-stripped prefix — they are report
  identifiers, not filenames).
- A material-less job is one `None` group: bit-identical to the historical
  single-pass packing (constrained leftovers keep the generic error, unbounded
  bins keep `default_plate_{i}` ids).
- Example fixture: `tests_deps/material_demo_job.yaml` (pinned by
  `tests/test_layout.py::TestMaterialDemoExample` and
  `tests/test_phase3_export.py::TestMaterialDemoExample`) — two materials, one
  plate each, plus a magenta deep-engraved layer on the uv sheet.

### Circular/Elliptical Labels (`type: round`)

`type` (enum `LabelShape`: `rect` / `round`, cascades label → job, plate
parity, `job-config.json` key, default `rect`) makes a label's boundary an
**ellipse** with semi-axes `width / 2` and `height / 2` — a circle when the
two are equal. The TODO contract: `width ≠ height` → ellipse; `width = height`
→ circle; declaring **only one** of the two → circle of that diameter (the
schema validator relaxes the both-required rule for round labels and
`resolution._resolve_label` fills the missing dimension from the declared
one). `rect` is the default and yields the current behavior.

**This is an ADJACENT pipeline, never a replacement.** Every rectangular label
keeps packing through `rectpack` exactly as before (bit-identical); `round`
labels route to the shapely packer. The dispatch is per label at the packing
boundary, and the rectangular path imports nothing new.

- **`plt_optimizer/generate/packing/ellipse_packer.py`** (NEW, pure shapely, no
  pipeline imports; numpy only inside the placement index): `ellipse_polygon`
  (unit circle polygonized at
  `quad_segs`, scaled to the semi-axes buffered by `clearance / 2`, so a plain
  `intersects` test is collision-exact), `EllipsePacker.pack` (largest-area
  first; a grid scan whose outer loop walks the primary axis ascending and
  stops at the first fit — the extent score is minimized by the first hit, and
  the inner secondary scan breaks at that first hit too;
  both 0°/90° orientations evaluated, circles dedupe to one),
  `ScrapDirection.PREFER_WIDTH` (minimize used width → clean vertical scrap
  strip; the `columns` fill order) / `PREFER_HEIGHT` (the `rows` fill order),
  and the `occupancy` parameter (rectangular placements, buffered by
  `clearance / 2`, seed the collision test so an ellipse never overlaps a
  rectangle). Feasibility is decided by `_PlacementIndex` (not a collision
  union): a placed polygon's inscribed circle (`cos(pi/N) * min(buffered
  semi-axes)`) and circumcircle bound center-distance tests that PROVE
  collision/disjointness for nearly every candidate — batched per scan column
  as one numpy broadcast — with the thin band between falling through to exact
  per-polygon `intersects` (tangency = collision semantics preserved); the
  occupancy seed is `shapely.prep`-prepared once. Placements are plate-frame **y-down** centers — the emitted
  device convention, the same frame rectpack fills — so
  `layout._pack_round_entries_on_plate` maps them to `PackedLabel` by adding
  the plate clearances (no Y flip); the label's slot is the ellipse's
  enclosing rectangle.
- **`packing/layout.py`**: `_pack_entries_with_pinners` is the single dispatcher — it
  splits entries by `shape_type` (`_split_shape_entries`), runs the
  **unchanged** rectangular pipeline (`_pack_rect_entries_with_pinners`, the
  renamed original), and only then the round pipeline
  (`_pack_round_entries_with_pinners`) seeded with the rectangles as
  occupancy; a job with no round labels returns the rectangular result
  untouched (no shapely import, no extra work). The round pipeline mirrors the
  rectangular one (pinned plates first, then per-material passes, materials
  never share a plate, unbounded overflow onto `round_default_plate_{i}`
  sheets). The inter-label kerf is the group's widest boundary/hole cutter
  (`_round_clearance`) — two engraved borders never touch.
- **`label_renderer.py`**: `label_is_round` is the single shape predicate.
  The engraved border is **arc-native**: `_render_boundary_hpgl` emits the
  ellipse as four PU-led quadrant strokes, each refit with the tangent-
  continuous circular `AA` chain of
  [`arc_fit.fit_ellipse_arc`](../arc_fit.py) at `ARC_FIT_DEV_TOL_INCHES`
  (a 4x2in label: 40 arcs; a circle: 4 exact quarter arcs; the border cap is
  `BORDER_ARC_FIT_MAX_ARCS` = 32/quadrant, larger than the glyph cap because
  a border quadrant sweeps 90 degrees of a label-size ellipse). Every
  quadrant starts on an axis vertex, so the PU coordinates keep the contour's
  swept bounds on the nominal box (the packer and plate transforms depend on
  that; the emitted `AA` radius is integer-unit quantized, so the measured
  box lands within ~2 plotter units — the emission floor documented in
  `arc_fit.py`). An extreme aspect ratio the cap cannot resolve falls back to
  the polygonized contour with a logged WARNING (never silent).
  `_render_boundary_local` stays the flattened polygon view (closed polygon
  sampled at `ARC_FLATTEN_CHORD_TOL_INCHES` with an EVEN vertex count so the
  four axis vertices are hit exactly) for vpype-era consumers/tests.
  `hole_circles_local` places `sides`/`top`/`bottom` holes on the axis
  vertices (the inset is exact on an axis) and the four corner holes on the
  45° diagonal inset along the outward normal (`round_corner_hole`) — the
  rectangular corner inset would drill in the material the ellipse cuts away.
  The text margin is the plain **rectangular** inset: the user margins are
  trusted directly, with no radial recomputation. `resolution.ellipse_content_inset`
  (the largest centered block of the label's aspect inscribed in the inset
  ellipse, closed form `θ = atan2(a′, aspect·b′)`) ships as a tested geometry
  primitive documenting the inset rule, and is deliberately NOT applied while
  positioning text: recomputing the local margins from it double-applied the
  margin and caused spurious horizontal compression (fixed in 4757694; the
  restore in 7dde646 was a misdiagnosis — the wiring was removed on purpose,
  not lost — and was reverted again). `_border_box` (the descender relief's
  virtual "following line") carries the ellipse's **lower arc**
  (`_lower_ellipse_arc`) for round labels, so a descender near the shoulders
  measures the rising boundary.
- **Collision detection stays shape-agnostic.** The text↔hole gap is
  `circle_aabb_gap` for every label: the label shape enters through the hole
  POSITIONS, not through the gap (the
  engraved-boundary clearance is the margin system's job on both shapes —
  measuring the boundary here would make a rectangular label's corner hole
  collide with itself). The `geometry.circle_ellipse_aabb_gap` /
  `circle_ellipse_gap` / `point_ellipse_signed_distance` primitives (exact
  Lagrange-multiplier point-to-ellipse distance, bisection, evolute-aware on
  the major axis) ship with the feature for boundary-aware work.
- **Win7/3.8 guarantee**: `packing/layout.py` is the only shapely importer and stays
  lazily imported inside `generate.run()`, so `main` + `watch` + `optimize`
  import without shapely (pinned by the matplotlib-import-blocker pattern in
  [`../../cli/AGENTS.md`](../../cli/AGENTS.md); verified).
- Example fixture: `tests_deps/ellipse_job.yaml` (pinned by
  `tests/test_phase3_export.py::TestEllipseDemoExample`) — circle, ellipse,
  width-only circle, rectangular control twin, `sides`/`corners` holes on a
  round label, and a repeated circle. Unit tests:
  `tests/test_ellipse_packer.py`, `tests/test_round_labels.py`,
  `tests/test_layout.py::TestRoundLabelPacking`,
  `TestLabelShapeField`/`TestRoundLabelDimensions` (`tests/test_schema.py`),
  `TestLabelShapeResolution`/`TestRoundLabelCircleFilling`/
  `TestEllipseContentInset` (`tests/test_resolution.py`), and the ellipse
  distance classes (`tests/test_collision_detection.py`).

### Rounded-Rectangle Borders (`rounded_rectangle_fraction`)

`rounded_rectangle_fraction` (float `[0.0, 1.0]`, cascades label → job, plate
parity, `job-config.json` key, default `0.0` = the sharp rectangle, bit-
identical) rounds the **rectangular** border's corners, and
`rounded_rectangle_proportional` (bool, same cascade, default `false`) picks
the rounding mode. The corners are **superellipse-quadrant patches**
`|X/rx|^n + |Y/ry|^n = 1` with exponent `n = 2 / fraction`, chosen so the
curvature varies **continuously** along the whole boundary (G2): a superellipse
patch of exponent `n > 2` has curvature **exactly zero** at its axis vertices
(near a vertex `x = rx − c·y^n`, so `κ ∝ y^(n−2) → 0`) and axis-aligned
tangents there, so the straight edges (κ = 0) join the patches with no
curvature jump — the artifact a circular fillet (κ: 0 → 1/r step at each
tangent point) cannot avoid. The exponent pins the family's endpoints:
`fraction → 0` ⇒ `n → ∞` (sharp rectangle), `0.5` ⇒ `n = 4` (squircle),
`1.0` ⇒ `n = 2` (the exact ellipse). The rounding distance follows the TODO
contract: proportional `false` = one **identical** rounding distance on every
side from the shorter edge (`rx = ry = fraction · min(W, H) / 2` — a 4x8 label
at `0.5` rounds 2in of the 4in sides, starting 1in from each corner);
proportional `true` = the fraction applies to each dimension
(`rx = fraction · W / 2`, `ry = fraction · H / 2`).

This is a **border change, never a packing change**: rounded labels keep
`shape_type: rect` and pack through rectpack in their nominal box — each patch
is tangent to the box at its axis vertices, so the rendered bounds equal the
nominal box (within the ~2-plotter-unit `AA` radius-quantization floor; the
packer and plate transforms are untouched, and
`packing/ellipse_packer.py` / `packing/layout.py` see nothing new).

- **The behavior matrix** (`resolution.rounded_rect_corner_params`,
  matplotlib-free, returns `None` for the sharp border): `0 < f < 1` = G2
  fillets in the chosen mode; `f = 1` + proportional (or a square) = the
  **exact ellipse/circle** — the renderer routes those labels to the shipped
  `type: round` ellipse path (`rounded_rect_full_ellipse` /
  `uses_ellipse_boundary`), so their boundary emission, corner holes and
  relief border are bit-identical to a `type: round` label of the same box
  (only the packing stays rectangular); `f = 1` + non-proportional +
  `W ≠ H` = the **smoothed stadium** — the short edges fully round, the long
  edges keep straight runs, and the corner exponent is clamped to
  `STADIUM_G2_EXPONENT = 4` (a quarter-circle there would join the straights
  with a curvature jump; the clamp keeps every junction κ = 0). Inert on
  `type: round` labels (the ellipse owns the boundary).
- **`label_renderer.py`**: `rounded_rect_corner` is the single predicate
  (sharp / round-shaped / fully-rounded ⇒ `None`). `_superellipse_quad`
  samples one patch from its X-vertex to its Y-vertex at
  `ARC_FLATTEN_CHORD_TOL_INCHES` with a step from the patch's **numerically
  measured** max curvature (`_superellipse_max_curvature`; the curvature is
  non-uniform, zero at the vertices, so the ellipse's uniform-step formula
  does not apply), endpoints exact. `_rounded_rect_boundary_polygon`
  concatenates the four patches CCW (consecutive patches share exactly one
  junction point, so the straight edges are the implied chords) and closes
  the loop. The engraved border is **arc-native**: `_render_boundary_hpgl`
  emits the same CCW contour as one closed PU-led stroke — four straight
  edges (merged `PD` runs) joined by four `arc_fit.fit_superellipse_arc`
  chains (trim-to-line: the near-flat parameter spans at each axis vertex
  emit as short chords that merge into the straights; the inner span refits
  with tangent-continuous circular `AA` arcs at `ARC_FIT_DEV_TOL_INCHES`,
  cap `BORDER_ARC_FIT_MAX_ARCS`/patch; a 4x2 squircle: 52 arcs) — while
  `_render_boundary_local` keeps the flattened polygon view. Patch endpoints
  are the exact axis vertices, so the contour's bounds stay on the nominal
  box (within the ~2-unit `AA` radius-quantization floor); a patch the cap
  cannot resolve falls back to the polygon with a logged WARNING.
  `_lower_rounded_rect_border` is the descender relief's bottom border (the
  `_lower_ellipse_arc` counterpart: bottom edge + the two lower patches, so a
  descender near a corner measures the rising curve). Corner drill holes ride
  the patch's 45° parametric point inset along the **outward normal**
  `(sign_x·cos²/rx, sign_y·sin²/ry)` — the gradient of the superellipse, the
  generalization of `round_corner_hole` (`rounded_rect_corner_hole`; at
  `n = 2` it is exactly the ellipse rule). Side/top/bottom holes keep the
  rectangular inset (exact at the edge midpoints, where the border is
  straight).
- **Corner-aware text inset** (`resolution.rounded_rect_content_inset`, the
  `ellipse_content_inset` rule): the margin applies to the engraved rounded
  boundary, so the centered content block's **corners** are measured against
  the inset boundary (half-extents `W/2 − h_margin` × `H/2 − v_margin`,
  corner radii shrunk by `min(h_margin, v_margin)`, same exponent) — a
  monotone fit test bisected for the largest centered block of the label's
  aspect. Applied by recomputing the two local margins in
  `_render_positioned_lines` (the `max` of the rectangular and rounded
  insets, so the rule only ever tightens), reaching the available height,
  compression width, alignment width and anchor with no per-site branching;
  concentric, so centering is unaffected. A fully-spent inset radius falls
  back to the plain rectangular inset (exact: the inset boundary is a box).
- **Collision detection stays shape-agnostic** (the `type: round` precedent):
  the text↔hole gap is `circle_aabb_gap` for every label; the rounding enters
  through the hole POSITIONS and the content inset, never the gap.
- Example fixture: `tests_deps/rounded_rect_job.yaml` (pinned by
  `tests/test_phase3_export.py::TestRoundedRectDemoExample`) — sharp control,
  equal-distance vs proportional rounding, the full-round ellipse with its
  `type: round` twin (bit-identical border emission), the smoothed stadium
  (flat long-edge runs), a full-round square (circle), corner holes on the
  patches, and a squircle carrying text. Unit tests: `tests/test_rounded_rect.py`
  (corner params, contour bounds/closure, **numeric curvature continuity**
  across junctions, the exact ellipse/circle limits, inset monotonicity, hole
  placement, the relief border, the routing predicates, and the arc-native
  border emission `TestRoundedRectBorderArcNative`),
  `TestRoundedRectangleField` (`tests/test_schema.py`),
  `TestRoundedRectangleResolution` (`tests/test_resolution.py`), and the
  `rounded_rectangle_*` job-config key tests (`tests/test_job_config.py`).
  User-facing demo: `examples/job_specs/rounded_rect_demo.yaml`.

