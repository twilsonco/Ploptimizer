"""Relief cutter ladder selection.

Two sources for the ladder (the TODO contract):

- ``relief_engraving_cutters`` (bypass): the specified diameters are used
  DIRECTLY as exact engraved widths -- no inventory snap, no kerf buffer,
  no quality preset. This is what pins tests to the EngraveLab references
  (0.06 + 0.005) regardless of the shop inventory.
- ``relief_engraving_quality`` preset against the label's engrave-region
  geometry (all diameters below are EFFECTIVE engraved widths):

  - coarse (1 cutter): the largest inventory cutter that leaves no region
    entirely untouched -- every outer component admits at least one full
    raster pass after a RADIUS erosion, and every enclosed component (text
    counters) admits one after the counter's core erosion
    (``d - skeleton_width / 2``, matching the pipeline stage math).
  - medium (2 cutters): the largest inventory cutter (the open background
    only constrains it) + the smallest cutter. The counters and corners the
    background cutter cannot finish fall to the fine pass; this is the
    time-saver preset.
  - fine (3 cutters): the largest, an intermediate that fits at least one
    full raster pass inside EVERY enclosed text hole (core erosion), and
    the finest cutter in inventory.

Presets collapse gracefully: when the artwork admits fewer cutters than the
preset allows, the ladder is shorter (coarse falling back to medium
semantics with a WARNING when NO cutter is fully admissible).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry

from plt_optimizer.generate.relief.filler import region_polygons
from plt_optimizer.generate.relief.regions import EngraveRegions

#: Fraction of the raster pitch (0.4d) sampled when probing a polygon for
#: its longest horizontal pass.
_PASS_SCAN_PITCH = 2.0


@dataclass(frozen=True)
class ReliefCutter:
    """One relief pass's cutter.

    Attributes:
        physical: Tool identity (file-name token, pen key) in inches.
        effective: The width it engraves. Relief engraving neglects
            ``cutter_buffer_text`` under all circumstances, so effective
            == physical for every relief cutter (bypass list and presets
            alike). Drives ALL relief geometry.
    """

    physical: float
    effective: float


def _longest_span(geometry: BaseGeometry) -> float:
    """Widest horizontal extent of any linear part of a scanline cut."""
    if geometry.is_empty:
        return 0.0
    kind = geometry.geom_type
    if kind in ("LineString", "LinearRing"):
        xs = [float(p[0]) for p in geometry.coords]
        return float(max(xs) - min(xs))
    if kind in (
        "MultiLineString",
        "GeometryCollection",
        "MultiPolygon",
        "Polygon",
        "MultiPoint",
    ):
        best = 0.0
        for child in geometry.geoms:
            best = max(best, _longest_span(child))
        return best
    return 0.0


def _scanline_passes(polygon: BaseGeometry, pitch: float) -> float:
    """Longest horizontal pass length inside ``polygon`` at scan pitch.

    Args:
        polygon: The region to probe.
        pitch: Scanline spacing.

    Returns:
        The longest interval found (0.0 when the polygon has no rows).
    """
    minx, miny, maxx, maxy = polygon.bounds
    best = 0.0
    y = miny + pitch / 2.0
    line = LineString([(minx - 1.0, 0.0), (maxx + 1.0, 0.0)])
    while y <= maxy:
        cut = polygon.intersection(line)
        best = max(best, _longest_span(cut))
        y += pitch
    return best


def _admissible_erosion(
    comp: Polygon,
    effective: float,
    interior: bool,
    skel_half: float,
    pitch_fraction: float = 0.4,
) -> bool:
    """True when ``comp`` admits one full raster pass at this cutter size.

    Matches :func:`plt_optimizer.generate.relief.pipeline._cores`: every
    component's core erodes by the RADIUS; the pass floor (one full pitch)
    is what blocks coarse cutters from a label with narrow counters.

    Args:
        comp: The engrave component.
        effective: The cutter's effective (engraved) diameter.
        interior: Enclosed counter component.
        skel_half: Half the widest engraved stroke width (reserved for
            future centerline-aware counter math).
        pitch_fraction: Row pitch as a fraction of the diameter
            (``1 - overlap/100``).
    """
    del skel_half, interior
    if effective <= 0.0:
        return False
    erosion = comp.buffer(-effective / 2.0)
    if erosion.is_empty:
        return False
    pitch = pitch_fraction * effective
    longest = 0.0
    for polygon in region_polygons(erosion):
        longest = max(longest, _scanline_passes(polygon, pitch / _PASS_SCAN_PITCH))
    return longest >= pitch - 1e-9


def select_relief_cutters(
    quality: str,
    regions: EngraveRegions,
    available_cutters: Sequence[float],
    logger: object = None,
    overlap_percent: float = 60.0,
) -> List[ReliefCutter]:
    """Select the relief cutter ladder for one label's regions.

    Relief engraving neglects ``cutter_buffer_text`` under all
    circumstances, so preset tools run at their PHYSICAL diameter
    (effective == physical, exactly like bypass-list values).

    Args:
        quality: ``"coarse"`` / ``"medium"`` / ``"fine"``.
        regions: The label's engrave regions.
        available_cutters: The shop inventory (physical diameters, inches).
        logger: Optional logger for the WARNING fallbacks.
        overlap_percent: Raster row overlap percent (pitch = d *
            (1 - overlap/100)); the pass-floor admissibility test scans
            the same grid the fill will use.

    Returns:
        The ladder, largest diameter first (deduplicated).

    Raises:
        ValueError: When the inventory is empty.
    """
    inventory = sorted({float(c) for c in available_cutters if c > 0.0}, reverse=True)
    if not inventory:
        raise ValueError("relief cutter selection requires a non-empty cutter inventory")
    pitch_fraction = 1.0 - overlap_percent / 100.0

    def _cutter(physical: float) -> ReliefCutter:
        return ReliefCutter(physical=physical, effective=physical)

    def _warn(message: str) -> None:
        log = getattr(logger, "warning", None)
        if log is not None:
            log(message)

    def _admissible(effective: float) -> bool:
        outer_ok = all(
            _admissible_erosion(
                comp, effective, interior=False, skel_half=skel_half, pitch_fraction=pitch_fraction
            )
            for comp in regions.outer
        )
        interior_ok = all(
            _admissible_erosion(
                comp, effective, interior=True, skel_half=skel_half, pitch_fraction=pitch_fraction
            )
            for comp in regions.interior
        )
        return outer_ok and interior_ok

    def _fits_holes(effective: float) -> bool:
        if not regions.interior:
            return False
        return all(
            _admissible_erosion(
                comp, effective, interior=True, skel_half=skel_half, pitch_fraction=pitch_fraction
            )
            for comp in regions.interior
        )

    skel_half = regions.max_ink_width / 2.0

    def _finish(ladder: List[ReliefCutter]) -> List[ReliefCutter]:
        seen: List[float] = []
        out: List[ReliefCutter] = []
        for cutter in ladder:
            if not any(
                math.isclose(cutter.effective, prior, rel_tol=1e-9, abs_tol=1e-9) for prior in seen
            ):
                seen.append(cutter.effective)
                out.append(cutter)
        return out

    largest, smallest = _cutter(inventory[0]), _cutter(inventory[-1])
    if quality == "coarse":
        for physical in inventory:
            if _admissible(physical):
                return _finish([_cutter(physical)])
        _warn(
            "relief quality 'coarse': no inventory cutter engraves every region of the "
            "label without skipping material; falling back to the medium ladder "
            f"({inventory[0]}in + {inventory[-1]}in)."
        )
        return _finish([largest, smallest])
    if quality == "fine":
        middle: Optional[ReliefCutter] = None
        for physical in inventory:
            cutter = _cutter(physical)
            if math.isclose(cutter.effective, largest.effective, rel_tol=1e-9, abs_tol=1e-9):
                continue
            if _fits_holes(cutter.effective):
                middle = cutter
                break
        ladder = [largest] + ([middle] if middle is not None else []) + [smallest]
        if middle is None:
            _warn(
                "relief quality 'fine': no intermediate cutter fits a full raster stroke "
                "inside every interior text hole; using the medium ladder "
                f"({inventory[0]}in + {inventory[-1]}in)."
            )
        return _finish(ladder)
    if quality == "medium":
        return _finish([largest, smallest])
    raise ValueError(f"unknown relief quality preset: {quality!r}")


def bypass_relief_cutters(values: Sequence[float]) -> Tuple[ReliefCutter, ...]:
    """Turn the ``relief_engraving_cutters`` bypass list into a ladder.

    Values are used as EXACT engraved widths (no buffer, no inventory snap,
    per the schema contract), sorted largest first.

    Args:
        values: The declared diameters in inches (each > 0).

    Returns:
        The ladder (empty when no positive value is given).
    """
    clean = sorted({float(v) for v in values if v > 0.0}, reverse=True)
    return tuple(ReliefCutter(physical=v, effective=v) for v in clean)
