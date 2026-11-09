"""Relief engraving for the generate pipeline.

Relief engraving inverts the normal engraving model: instead of cutting the
text strokes as grooves, the material AROUND the engraved-stroke silhouette
is rastered away, so the text survives as a RAISED island whose stroke width
is exactly the width normal engraving would have cut. The passes are
generated per label in label-local inches (+y up, the ``TextChunkRecord``
frame) as one or more cutter ladders:

1. the engrave region ``R`` = label box - raised islands (:mod:`.regions`),
2. the cutter ladder -- the explicit ``relief_engraving_cutters`` bypass
   (values are exact engraved widths, no inventory snap) or the
   ``relief_engraving_quality`` preset selection against the region
   geometry (:mod:`.cutters`); relief neglects ``cutter_buffer_text``
   under all circumstances, so preset tools engrave at their PHYSICAL
   diameter (effective == physical, exactly like bypass values),
3. per cutter, descending: the tool-center region
   ``(C - disk(e)) - prior coverage`` for border-touching components
   (``e = d/2``), the FULL core for enclosed counters (the reference's
   fine pass re-cuts the whole counter), raster-filled by the pluggable
   :class:`.filler.RasterFiller` (S-Sweep serpentine, row pitch
   ``d * (1 - overlap/100)`` -- 60% overlap = the reference ``0.4 * d``
   grid -- contiguous per-lane links), and outlined by a clean pass ONCE
   the fill of each region is done (fill
   first, outline after -- the outline squares off the raster's scalloped
   edge),
 4. finer cutters repeat on the rest material the previous coverage left
    (morphological identity: ``(R - disk(r1)) + disk(r2) = R - disk(r1-r2)``
    for disks, so the coarse pass over an enclosed counter leaves a
    ``d/2``-wide skin for the next cutter, and concave corners + sharp
    pockets for the finest one). KNOWN DIVERGENCE (b): the reference's
    13-row fine chains along the "A" legs run INSIDE the leg islands
    (EngraveLab's raster core is the glyph CENTERLINE minus d/2); our
    island-edge eroded core mirrors that band outside the island.

Geometry runs on shapely (a core dependency, already used on the generate
path by ``packing/ellipse_packer``); the module is matplotlib-free.

Ground truth: ``examples/plt_relief_engraving/A_relief_s-sweep_*.plt``
(EngraveLab). Derived constants verified against the references (1016
units/inch):

- raster row pitch = ``d * (1 - overlap/100)`` with
  ``relief_engraving_stroke_overlap_percent`` default 60.0, i.e.
  ``0.4 * cutter diameter`` (24.384u for 0.06in,
  2.032u for 0.005in -- exact in both reference files);
- border outline inset = cutter radius (plate box engraved 30.48..985.52
  by the 0.06 cutter);
- every component's core erodes by the cutter RADIUS (outer: the tool edge
  reaches the label edge tangentially; counter: the fill/outline chain
  runs inside the counter's island edge, the ``d/2`` skin to the island
  engraves in the NEXT, finer cutter's clean pass -- the reference's fine
  counter outline sits one radius off the island boundary);
"""
