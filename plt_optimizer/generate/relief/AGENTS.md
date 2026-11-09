# AGENTS.md - Relief Engraving Engine

Parent contract: [`../AGENTS.md`](../AGENTS.md) (root standards + generate pipeline
overview). Feature entry points: `LabelAttributes.relief_engraving*` fields
([`../AGENTS.md`](../AGENTS.md) "Relief engraving") and
`vectorize.export_per_cutter_plts`'s `relief` stage.

## Role

Relief engraving inverts normal engraving: the material AROUND the engraved
stroke silhouette is rastered away so the text survives as a RAISED island of
the exact width normal engraving would have cut. This package computes the
regions, the cutter ladder and the raster strokes; `vectorize`/`plate_optimizer`
route the resulting chunks through the normal plate-space TSP and per-cutter
file split. Ground truth: `examples/plt_relief_engraving/A_relief_s-sweep_*.plt`
(EngraveLab).

## Module map

- `regions.py` - `island_polygons` (ink -> raised islands: closed rings fill
  with containment-parity holes, open polylines buffer by `width/2`),
  `build_engrave_regions` (label box minus islands in the render frame -
  x `[0, w]`, y centered on the ink's `box_y_center`, plate transform verified),
  `label_ink` (per-stroke polylines + widths from `RenderedLabel.text_chunks`),
  `stroke_to_points` (arc flattening, `tol` in inches). Components classify
  as `outer` (touch the box boundary - erode by the radius) vs `interior`
  (enclosed counters - erode by the radius; the pass-floor test is what stops
  a coarse cutter from skipping a counter).
- `cutters.py` - `ReliefCutter(physical, effective)` (effective drives ALL
  geometry; physical names the file/pen), `bypass_relief_cutters` (the
  `relief_engraving_cutters` list: exact engraved widths, no buffer, no
  inventory, descending), `select_relief_cutters` (preset ladder: coarse =
  largest admissible; medium = largest + smallest; fine = largest + middle
  fitting one pass in every counter + smallest; graceful collapse + WARNINGs
  - the rules and fallbacks are pinned by `tests/test_relief_cutters.py`).
- `filler.py` - the pluggable pattern. `RasterFiller` Protocol:
  `fill(regions, pitch)` -> contiguous strokes, `outline(regions)` -> closed
  boundary rings. `SSweepFiller`: scanlines at `pitch = 0.4 * d` anchored at
  each region's bbox bottom + pitch (reference grid), row ENDS inset
  `end_inset_fraction * pitch` (1.25 default = d/2: capsule caps land tangent
  to the core, never into islands; 0.5 = the reference's counter-strip inset),
  serpentine chain continuation only while BOTH the link and the row traversal
  stay inside the region (splits -> independent strokes, pen-up), outlines =
  exterior + interior rings. Future patterns (spiral:
  `examples/plt_relief_engraving/A_relief_spiral_*.plt`) implement the
  Protocol without touching `pipeline.py`.
- `pipeline.py` - stage loop: per cutter (largest first) core = comp erosion,
  rest = core minus ALL prior stroke coverage (`_coverage_of`: rows/outlines
  dilated by `d/2`, clipped to the engrave region - swept area over an island
  removes no material), fill then outline per rest region, coverage feeds the
  next stage; every contiguous fill stroke / outline ring is one chunk.
  Residuals > `_RESIDUAL_TOLERANCE` sq in log a WARNING (features narrower
  than the finest cutter); chunk-count guard `_MAX_CHUNK_WARNING`.
- `export.py` - `relief_pen_layers` (candidate pens reserved pre-render -
  unused pens produce no files) and `apply_relief_to_plates` (runs after
  layout/pack: builds regions from the rendered ink, plans the ladder, replaces
  the relief labels' `RenderedLabel` - `text_chunks` = one synthetic
  `TextChunkRecord` per relief stroke on the relief pens, `plt_content` =
  border SP2 + relief sections (text pens and SP3 holes dropped),
  `band_center_offset_inches` forced so the plate transform centers the
  symmetric fill box on the label). Relief strokes are emitted as LineSeg
  chains in label-local +y-up inches (the `TextChunkRecord` frame) -
  rotation/slot/flip ride the normal `_label_transform`.

## Ground-truth constants (verified against the references, 1016 u/in)

- Raster pitch = `0.4 * effective_diameter` (24.384u @ 0.06, 2.032u @ 0.005).
- Border: outer components erode by the RADIUS; the plate/label border line
  the coarsest cutter engraves sits at the radius inset (tool edge tangent to
  the label edge).
- Counter outlines sit at the eroded core boundary; the `d/2` skin from there
  to the island edge is the NEXT (finer) cutter's rest strip (the reference's
  fine pass re-cuts it, fill-then-outline).
- Coverage equivalence (not stroke identity) is the test contract - see
  `tests/test_relief_export.py` thresholds and `tests/relief_coverage.py`.

## Invariants

- Effective diameters drive geometry; physical diameters name files/pens
  (`60_txt_*.plt` for a 0.06 tool; bypass values are both).
- Relief content must never intrude past the raised islands beyond the
  row-cap tolerance band, and never cut the plate border's bh path - the
  coverage tests pin both.
- No matplotlib here; shapely is a core dependency (VPype precedent on the
  generate export path). Match `generate/` typing/style (modern hints, strict
  mypy on `plt_optimizer/`).
- `tests_deps/relief/` fixtures (reference PLTs + both YAMLs + the zero-buffer
  test config) are frozen like all of `tests_deps/`.
