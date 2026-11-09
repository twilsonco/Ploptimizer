"""Relief engraving pipeline: stages, fills, outlines, chunk records.

The stage loop (per cutter, largest effective diameter first):

- each engrave component's tool-center CORE: every component erodes by the
  cutter RADIUS (border-touching: the tool edge reaches the label edge
  tangentially; enclosed counters: the fill runs inside the counter edge),
- the fill region for this cutter: every component's core minus ALL
  earlier cutters' coverage (coverage = the stroke envelopes: every
  fill/outline polyline dilated by ``d/2`` -- the row disks, not the full
  core dilation, so the ``d/2`` margin band an outline engraved stays
  rest for the next, finer cutter to re-clean, exactly as the EngraveLab
  references do),
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
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

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


def _cores(regions: EngraveRegions, cutter: ReliefCutter) -> Tuple[List[Polygon], List[Polygon]]:
    """This cutter's tool-center areas, (outer, interior).

    Matches the reference row geometry (the 0.06 box rows span
    ``[42.672, 973.328]`` = the plate-box radius erosion; the "A" counter
    rows span the full-diameter-eroded triangle inset 12.192 = pitch/2):

    - border-touching (outer) components erode by the cutter RADIUS (the
      tool edge reaches the label edge tangentially);
    - enclosed (interior: text counters) components erode by the FULL
      DIAMETER (the outline then sits one diameter off the centerlines,
      and the ``d/2`` skin between it and the island is the next cutter's
      rest strip).

    Args:
        regions: The label's engrave regions.
        cutter: The current cutter.

    Returns:
        ``(outer cores, interior cores)``.
    """
    d = cutter.effective
    outer_cores: List[Polygon] = []
    interior_cores: List[Polygon] = []
    for comp in regions.outer:
        outer_cores.extend(region_polygons(comp.buffer(-d / 2.0, quad_segs=256, join_style=1)))
    for comp in regions.interior:
        interior_cores.extend(region_polygons(comp.buffer(-d / 2.0, quad_segs=256, join_style=1)))
    return outer_cores, interior_cores


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
    strokes: Sequence[Sequence[Point]], d: float, engrave: BaseGeometry
) -> BaseGeometry:
    """The stroke envelope (rows/outlines dilated by the tool radius).

    Clipped to the engrave region: raised island material survives relief,
    so swept area that overlaps an island is not "removed" material.
    """
    r = d / 2.0
    geoms = [LineString(points).buffer(r) for points in strokes if len(points) >= 2]
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
    border_fraction = active.min_pass_fraction if isinstance(active, SSweepFiller) else 0.25
    border_filler = SSweepFiller(min_pass_fraction=border_fraction, end_inset_fraction=1.25)
    passes: List[ReliefPass] = []
    coverage: List[BaseGeometry] = []
    total_chunks = 0
    pitch_fraction = 1.0 - overlap_percent / 100.0
    for cutter in ladder:
        d = cutter.effective
        pitch = d * pitch_fraction
        outer_cores, interior_cores = _cores(regions, cutter)
        # Border-touching components engrave only the material NO earlier
        # cutter's FILL capsule reached. Counting an earlier cutter's
        # OUTLINE sweep as coverage is what dropped the 13-row strip
        # chains along the "A" legs in the reference comparison: the
        # outline leaves a d/2 skin band that the NEXT, finer cutter
        # re-cuts (fill-then-outline at every stage, the reference's
        # fine chains run straight through the coarse outline sweep).
        rest_outer = _rest_region(outer_cores, coverage)
        rest_interior = _rest_region(interior_cores, coverage)
        rest = rest_outer + rest_interior
        if not rest:
            continue
        fill_points = border_filler.fill(rest_outer, pitch) + active.fill(rest_interior, pitch)
        outline_points = border_filler.outline(rest_outer) + active.outline(rest_interior)
        strokes: List[ReliefStroke] = []
        for points in fill_points:
            if len(points) >= 2:
                strokes.append(ReliefStroke(stroke=_stroke_from_points(points), is_outline=False))
        for ring in outline_points:
            if len(ring) >= 2:
                strokes.append(ReliefStroke(stroke=_stroke_from_points(ring), is_outline=True))
        if not strokes:
            continue
        # Staged coverage = the full swept envelope (fill rows AND outline
        # rings dilated by d/2): the proven coverage-equivalence model
        # (the fine pass engraves only the material nothing reached).
        # KNOWN (b) DIVERGENCE: the reference's 13-row fine chains along
        # the "A" legs run INSIDE the leg islands (EngraveLab's raster
        # core is the glyph CENTERLINE minus d/2, so its fine cut reaches
        # from centerline-2.54u to centerline-30.48u); our island-edge
        # eroded core mirrors that band outside the island. Reproducing
        # the reference strips exactly requires a centerline-distance
        # core model -- tracked, not silently attempted.
        coverage.append(_coverage_of(fill_points + outline_points, d, regions.engrave))
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
