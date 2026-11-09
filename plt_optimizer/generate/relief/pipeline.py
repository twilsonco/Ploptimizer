"""Relief engraving pipeline: stages, fills, outlines, chunk records.

The stage loop (per cutter, largest effective diameter first):

- each engrave component's tool-center CORE: border-touching components
  erode by the cutter RADIUS (the tool edge reaches the label edge
  tangentially, corners SHARP - the island caps round via the round-join
  buffer); enclosed counters erode by the RADIUS on the sides and by the
  FULL DIAMETER on the floor edge (``_clip_counter_floor``), kept sharp,
  and are re-cleaned from their full core at every stage,
- the fill region for this cutter: every component's core minus ALL
  earlier cutters' coverage (coverage = the stroke envelopes: every fill
  row dilated by its radius ``d/2`` and every outline ring dilated by
  ``d/2 - r_next`` -- the row disks, not the full core dilation, so the
  skin band off an engraved outline stays rest for the next, finer cutter
  to re-clean, exactly as the EngraveLab references do),
- KNOWN (b) DIVERGENCE (measured, documented, tracked): the reference's
  13-row fine chains along the "A" legs run INSIDE the leg islands
  (EngraveLab's raster core is the glyph CENTERLINE minus d/2), while our
  island-edge eroded core mirrors that skin band outside the island.
  Reproducing the strips exactly requires a centerline-distance core
  model -- a core rework, not silently attempted here.
- the regions are raster-filled by the pluggable filler at
  ``pitch = d * (1 - overlap/100)`` (``relief_engraving_stroke_overlap_percent``,
  60.0 = the reference 0.4d grid), then each region's boundary is emitted
  as clean outline strokes (fill first, outline after),
- one contiguous fill stroke / one outline ring = one chunk (the TSP node),
- the final uncovered remainder is logged (a WARNING when it is more than
  sub-plotter-unit dust), which means the artwork has features narrower
  than the finest available cutter.

Strokes are returned as ``text_geometry.Stroke`` in label-local inches
(+y up), so the normal plate transform (centering, Y-mirror, rotation, slot
translation) carries relief content exactly like text chunks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from shapely.geometry import LineString, Polygon
from shapely.geometry import box as shapely_box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from plt_optimizer.generate.relief.arcify import fit_circular_runs
from plt_optimizer.generate.relief.cutters import ReliefCutter
from plt_optimizer.generate.relief.filler import RasterFiller, SSweepFiller, region_polygons
from plt_optimizer.generate.relief.regions import EngraveRegions
from plt_optimizer.generate.text_geometry import LineSeg, Stroke
from plt_optimizer.utils.logging import TextLogger

Point = Tuple[float, float]

#: Residual (uncovered) engrave area, in square inches, tolerated silently
#: (skin the finest cutter cannot remove at its own d/2 boundary is
#: ~perimeter * d_min/2; this threshold only guards against a region the
#: ladder missed entirely).
_RESIDUAL_TOLERANCE = 0.02

#: Chunk-count guard: rasters on huge areas with a fine cutter can explode
#: the optimizer's node count; warn (do not abort) past this.
_MAX_CHUNK_WARNING = 200_000


@dataclass(frozen=True)
class ReliefStroke:
    """One relief chunk (label-local inches, +y up).

    Attributes:
        stroke: The arc-free polyline stroke (LineSeg chain).
        is_outline: True for the clean outline pass (fill strokes first).
    """

    stroke: Stroke
    is_outline: bool


@dataclass(frozen=True)
class ReliefPass:
    """One cutter's relief output.

    Attributes:
        cutter: The cutter (physical identity + effective engraved width).
        strokes: Fill strokes first, then outline rings.
    """

    cutter: ReliefCutter
    strokes: Tuple[ReliefStroke, ...]


def _stroke_from_points(points: Sequence[Point]) -> Stroke:
    """Build a LineSeg-chain Stroke from label-local points.

    Args:
        points: Vertices in cutting order (at least two).

    Returns:
        A pen-up-led stroke starting at the first point.
    """
    start = complex(points[0][0], points[0][1])
    segments = []
    previous = start
    for x, y in points[1:]:
        current = complex(x, y)
        if current != previous:
            segments.append(LineSeg(start=previous, end=current))
            previous = current
    return Stroke(pen_up=start, segments=tuple(segments))


def _stroke_from_ring(ring: Sequence[Point]) -> Stroke:
    """Build a Stroke for an outline ring, arcifying its circular runs.

    The ring polyline is dense chords (shapely boundary of a buffer, and
    island arcs flattened at 2.5e-4 in); :func:`fit_circular_runs` groups
    same-sign turn runs, verifies each is provably circular (fit radius,
    per-vertex deviation, sweep floor -- and never a single sharp
    miter-corner vertex), and replaces it with ONE native arc. Fill rows
    stay pure lines (the reference rows are straight); the outline is
    exactly where the references cut ``PD;AA`` commands (box corners at
    the tool radius, semicircle caps over the raised text/markup tips --
    apex of the "A", leg ends -- the cap arcs concentric with the island
    capsule caps).

    Args:
        ring: Ring vertices in cutting order (closed: first == last).

    Returns:
        A pen-up-led mixed LineSeg/ArcSeg stroke.
    """
    start = complex(ring[0][0], ring[0][1])
    segments = fit_circular_runs(ring)
    return Stroke(pen_up=start, segments=tuple(segments))


def _cores(regions: EngraveRegions, cutter: ReliefCutter) -> Tuple[List[Polygon], List[Polygon]]:
    """This cutter's tool-center areas, (outer, interior).

    Matches the reference row geometry (the 0.06 box rows span
    ``[42.672, 973.328]`` = the plate-box radius erosion; the "A" counter
    rows span the full-diameter-eroded triangle inset 12.192 = pitch/2):

    - border-touching (outer) components erode by the cutter RADIUS (the
      tool edge reaches the label edge tangentially); the box corners stay
      SHARP (the reference coarse ring is the plain square
      ``PU30.48,30.48 ... 985.52`` - the corner ``AA30.48`` arcs in the
      references belong to the FINE stage, radius ``r_c - r_f`` around the
      coarse corner, task 3), and concave wrap corners round at r (the
      island cap arcs: the "A" apex, the leg ends - buffer round joins +
      :func:`arcify.fit_circular_runs` reproduce them);
    - enclosed (interior: text counters) components erode by the RADIUS on
      their sides and by the FULL DIAMETER on the floor (the reference's
      "A" counter triangle: sides at the island edges minus r, base at the
      crossbar island edge 606.53u minus d = 545.566u), and their corners
      stay SHARP - the reference counter outline is a plain triangle, no
      fillet arcs (see :func:`_clip_counter_floor`).

    Args:
        regions: The label's engrave regions.
        cutter: The current cutter.

    Returns:
        ``(outer cores, interior cores)``.
    """
    d = cutter.effective
    r = d / 2.0
    outer_cores: List[Polygon] = []
    interior_cores: List[Polygon] = []
    for comp in regions.outer:
        outer_cores.extend(region_polygons(comp.buffer(-r, quad_segs=256, join_style=1)))
    for comp in regions.interior:
        eroded = comp.buffer(-r, quad_segs=256, join_style=1)
        interior_cores.extend(_clip_counter_floor(eroded, d))
    return outer_cores, interior_cores


def _clip_counter_floor(geom: BaseGeometry, d: float) -> List[Polygon]:
    """Clip a counter core's floor edge to the full-diameter erosion.

    The reference's counter fill (coarse, the "A" hole) is a triangle whose
    SIDES sit at the island edges eroded by the cutter RADIUS but whose
    BASE sits at the floor island edge minus the FULL DIAMETER
    (606.53 − 60.96 = 545.566u): the tool must clear a full diameter of
    the raised bar below, only a radius from the slanted sides. The floor
    is the bbox edge with the widest intersection with the core (the
    crossbar side; the "A" apex side is a point, the leg sides are
    slanted). Erode that edge the extra ``d - r`` (the radius erosion
    already ran) and keep the rest of the core untouched. Sharp corners
    are preserved (no fillet discs on interior cores).

    Args:
        geom: The radius-eroded counter core.
        d: The cutter's effective diameter.

    Returns:
        The floor-clipped polygons (``[]`` when the clip empties the core).
    """
    if geom.is_empty:
        return []
    x0, y0, x1, y1 = geom.bounds
    span = max(x1 - x0, y1 - y0)
    probe = max(1e-9, span * 1e-3)
    edge_spans = [
        geom.intersection(LineString([(x0, y0 + probe), (x1, y0 + probe)])).length,
        geom.intersection(LineString([(x0, y1 - probe), (x1, y1 - probe)])).length,
        geom.intersection(LineString([(x0 + probe, y0), (x0 + probe, y1)])).length,
        geom.intersection(LineString([(x1 - probe, y0), (x1 - probe, y1)])).length,
    ]
    floor = max(range(4), key=lambda index: edge_spans[index])
    if edge_spans[floor] <= probe:
        return region_polygons(geom)  # degenerate: no measurable floor edge
    extra = d / 2.0  # d - r with r = d / 2: the full-diameter floor total
    pad = span + 1.0
    boxes = (
        shapely_box(x0 - pad, y0 + extra, x1 + pad, y1 + pad),  # floor at min-y
        shapely_box(x0 - pad, y0 - pad, x1 + pad, y1 - extra),  # floor at max-y
        shapely_box(x0 + extra, y0 - pad, x1 + pad, y1 + pad),  # floor at min-x
        shapely_box(x0 - pad, y0 - pad, x1 - extra, y1 + pad),  # floor at max-x
    )
    return region_polygons(geom.intersection(boxes[floor]))


def _rest_region(
    cores: Sequence[BaseGeometry],
    coverage: Sequence[BaseGeometry],
) -> List[Polygon]:
    """This cutter's fill regions: its cores minus prior stroke coverage.

    The remainder is exactly the material no earlier cutter's swept
    envelope reached -- concave corner pockets, the strips an outline's
    dilation leaves, and the regions the coarse tool physically cannot
    enter.

    Args:
        cores: This cutter's cores.
        coverage: Dilated stroke envelopes of every earlier stage.

    Returns:
        Connected rest polygons.
    """
    if not cores:
        return []
    core_u = unary_union(list(cores))
    rest = core_u
    if coverage:
        rest = core_u.difference(unary_union(list(coverage)).buffer(1e-9))
    return region_polygons(rest)


def _coverage_of(
    strokes: Sequence[Sequence[Point]], radius: float, engrave: BaseGeometry
) -> BaseGeometry:
    """The swept envelope of ``strokes`` dilated by ``radius``.

    Clipped to the engrave region: raised island material survives relief,
    so swept area that overlaps an island is not "removed" material.
    """
    geoms = [
        LineString(points).buffer(radius, quad_segs=64) for points in strokes if len(points) >= 2
    ]
    if not geoms:
        return Polygon()
    return unary_union(geoms).intersection(engrave)


def generate_relief_passes(
    regions: EngraveRegions,
    ladder: Sequence[ReliefCutter],
    filler: Optional[RasterFiller] = None,
    logger: Optional[TextLogger] = None,
    log_prefix: str = "",
    overlap_percent: float = 60.0,
) -> List[ReliefPass]:
    """Run the cutter ladder over the regions and emit the chunk strokes.

    Args:
        regions: The label's engrave regions.
        ladder: Cutters, largest effective diameter first.
        filler: The raster pattern engine (default :class:`SSweepFiller`).
        logger: Optional logger for the selection/residual report.
        log_prefix: Prefix for log lines (e.g. ``[label 3]``).
        overlap_percent: Adjacent-row overlap percent; the raster pitch is
            ``d * (1 - overlap/100)`` (60.0 = the reference 0.4d grid).

    Returns:
        One :class:`ReliefPass` per cutter that had geometry to engrave.

    Raises:
        ValueError: When ``overlap_percent`` is outside ``[0, 100)``.
    """
    if not 0.0 <= overlap_percent < 100.0:
        raise ValueError(
            f"relief stroke overlap percent must be in [0, 100), got {overlap_percent!r}"
        )
    active = filler if filler is not None else SSweepFiller()
    if regions.engrave.is_empty:
        return []
    passes: List[ReliefPass] = []
    # Two coverage layers per stage (measured from the references): the FILL
    # rows sweep a band their full width (dilated by r - the skin a row's
    # capsule cannot reach is the outline's job), while an OUTLINE ring is
    # dilated only by (r - r_next): the (r_next) skin off the outline is the
    # next, finer cutter's rest strip - the reference's fine pit rings and
    # box corner arcs (r_c - r_f from the coarse outline sweep, lines at the
    # finer core) fall out of this geometry. The last stage dilates its
    # outline by the full r (nothing follows).
    coverage: List[BaseGeometry] = []
    total_chunks = 0
    pitch_fraction = 1.0 - overlap_percent / 100.0
    for index, cutter in enumerate(ladder):
        d = cutter.effective
        r = d / 2.0
        pitch = d * pitch_fraction
        r_next = ladder[index + 1].effective / 2.0 if index + 1 < len(ladder) else 0.0
        outer_cores, interior_cores = _cores(regions, cutter)
        # Fill (measured against the references):
        # - border components engrave the RAW core rect minus earlier FILL
        #   capsules: the d/2 skins an earlier OUTLINE swept are rest for
        #   the finer cutter (fill-then-outline at every stage -- the
        #   reference's 13-row fine strips run straight through the coarse
        #   outline sweep),
        # - enclosed counters are re-cleaned from their FULL core at every
        #   stage (the reference's fine counter chains cross the coarse
        #   sweep: 13 rows in the "A" counter corner).
        rest_fill = _rest_region(outer_cores, coverage)
        rest_interior = region_polygons(unary_union(interior_cores)) if interior_cores else []
        rest = rest_fill + rest_interior
        if not rest:
            continue
        gx0, gy0, gx1, gy1 = regions.engrave.bounds
        grid_anchor = (gx0 + r, gy0 + r, gx1 - r, gy1 - r)
        fill_points = active.fill(rest_fill, pitch, anchor=grid_anchor) + active.fill(
            rest_interior, pitch
        )
        # Outline: the RAW eroded outer core ring (sharp box corners at the
        # radius inset - the reference coarse ring is the plain square
        # PU30.48...985.52; the island-cap arcs come from the round-join
        # buffer), minus prior coverage.
        rest_outer = _rest_region(outer_cores, coverage)
        outline_points = active.outline(rest_outer) + active.outline(rest_interior)
        strokes: List[ReliefStroke] = []
        for points in fill_points:
            if len(points) >= 2:
                strokes.append(ReliefStroke(stroke=_stroke_from_points(points), is_outline=False))
        for ring in outline_points:
            if len(ring) >= 2:
                strokes.append(ReliefStroke(stroke=_stroke_from_ring(ring), is_outline=True))
        if not strokes:
            continue
        # Staged coverage: fill rows dilated by r (their full sweep) plus
        # outline rings dilated by (r - r_next) -- the finer cutter's rest
        # strip off the outline. Fill-then-outline at every stage: the
        # reference's 13-row fine strips run straight through the coarse
        # outline sweep and re-cut the skin, and the fine pit rings /
        # box-corner arcs land at (r_c - r_f) from the coarse outline.
        # KNOWN (b) DIVERGENCE: the reference's 13-row fine chains along
        # the "A" legs run INSIDE the leg islands (EngraveLab's raster
        # core is the glyph CENTERLINE minus d/2, so its fine cut reaches
        # from centerline-2.54u to centerline-30.48u); our island-edge
        # eroded core mirrors that band outside the island. Reproducing
        # the reference strips exactly requires a centerline-distance
        # core model -- tracked, not silently attempted.
        coverage.append(_coverage_of(fill_points, r, regions.engrave))
        coverage.append(_coverage_of(outline_points, r - r_next, regions.engrave))
        total_chunks += len(strokes)
        passes.append(ReliefPass(cutter=cutter, strokes=tuple(strokes)))
    if total_chunks > _MAX_CHUNK_WARNING and logger is not None:
        logger.warning(
            f"{log_prefix}relief raster produced {total_chunks} chunks (> "
            f"{_MAX_CHUNK_WARNING}): consider a coarser relief quality preset."
        )
    if coverage:
        residual = regions.engrave.difference(unary_union(coverage))
    else:
        residual = regions.engrave
    if not residual.is_empty and residual.area > _RESIDUAL_TOLERANCE and logger is not None:
        logger.warning(
            f"{log_prefix}relief ladder left {residual.area:.4f} sq in unengraved -- "
            "the artwork has interior features narrower than the finest relief cutter."
        )
    return passes


def pen_widths_for_label(
    layers: Dict[int, Tuple[float, str]],
) -> Dict[int, float]:
    """Map each text pen of a label to its PHYSICAL cutter width.

    Relief engraving neglects ``cutter_buffer_text`` under all
    circumstances: the raised islands are the ink swept by the physical
    tool, so a shop kerf buffer never inflates the island silhouette (a
    buffered island pushes the raster past the engraved marks).

    Args:
        layers: Pen -> ``(physical cutter, color)`` (``layer_by_pen``).

    Returns:
        Pen number -> physical cutter diameter in inches.
    """
    return {pen: cutter for pen, (cutter, _color) in layers.items()}


__all__ = [
    "ReliefPass",
    "ReliefStroke",
    "generate_relief_passes",
    "pen_widths_for_label",
]
