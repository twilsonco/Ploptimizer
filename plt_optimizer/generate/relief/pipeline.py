"""Relief engraving pipeline: stages, fills, outlines, chunk records.

The stage loop (per cutter, largest effective diameter first):

- each engrave component's tool-center CORE: the border-touching (outer)
  components erode by the cutter RADIUS (the tool edge reaches the label
  edge tangentially), the enclosed (interior: text counters) components
  erode by ``d - skeleton_width / 2`` -- negative when the cutter is
  smaller than the surrounding island (the counter core then expands past
  its island boundary, into the strip the previous cutter outlined),
- the rest region for this cutter = its core minus ALL earlier cutters'
  coverage (coverage = the stroke envelopes: every fill/outline polyline
  dilated by ``d/2`` -- the row disks, not the full core dilation, so the
  ``d/2`` margin band an outline engraved stays rest for the next, finer
  cutter to re-clean, exactly as the EngraveLab references do),
- the rest is raster-filled by the pluggable filler, then each region's
  boundary is emitted as clean outline strokes (fill first, outline after),
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

#: Core dilation fraction: the EngraveLab reference raster centers run
#: ~0.55*d past the engrave boundary into the raised islands (the bar/leg
#: strips), leaving the island a raised core of about one third of its
#: engraved width. 0.06 box rows: [42.672, 973.328] = inset 12.192 from
#: the 30.48 radius-erosion; 0.06 counter rows end at leg centerline -77.4u
#: = island edge + 33.6u = +0.55d into the band.
_CORE_DILATE_FRACTION = 0.55

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
) -> List[ReliefPass]:
    """Run the cutter ladder over the regions and emit the chunk strokes.

    Args:
        regions: The label's engrave regions.
        ladder: Cutters, largest effective diameter first.
        filler: The raster pattern engine (default :class:`SSweepFiller`).
        logger: Optional logger for the selection/residual report.
        log_prefix: Prefix for log lines (e.g. ``[label 3]``).

    Returns:
        One :class:`ReliefPass` per cutter that had geometry to engrave.
    """
    active = filler if filler is not None else SSweepFiller()
    if regions.engrave.is_empty:
        return []
    border_fraction = active.min_pass_fraction if isinstance(active, SSweepFiller) else 0.25
    border_filler = SSweepFiller(min_pass_fraction=border_fraction, end_inset_fraction=1.25)
    passes: List[ReliefPass] = []
    coverage: List[BaseGeometry] = []
    total_chunks = 0
    for cutter in ladder:
        d = cutter.effective
        pitch = 0.4 * d
        outer_cores, interior_cores = _cores(regions, cutter)
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
