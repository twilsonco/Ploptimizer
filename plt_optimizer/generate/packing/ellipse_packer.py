"""Grid-search packing of circular/elliptical labels onto rectangular plates.

This module is the **adjacent** packing pipeline for ``type: round`` labels
(see :attr:`plt_optimizer.generate.schema.LabelAttributes.type`). Rectangular
labels keep packing through ``rectpack`` untouched; round labels pack here,
where the *ellipse boundary* -- not its bounding box -- is the collision
object, so labels nest closer together than a rectangular packer allows.

The algorithm follows the reference prototype in ``docs/ellipse-packer.py``:

1. **Polygonize**: each ellipse (semi-axes ``a``, ``b``) becomes an
   axis-aligned N-point polygon buffered outward by ``clearance / 2`` (half
   the tool kerf/spacing), so a plain intersection test is collision-exact.
2. **Sort**: items pack largest-area-first (tighter packing).
3. **Scan**: candidate centers walk the primary scrap direction first --
   ``PREFER_WIDTH`` sweeps increasing X columns filling bottom-to-top in Y
   (minimizing used width, leaving a clean vertical scrap strip on the
   right); ``PREFER_HEIGHT`` sweeps increasing Y rows filling left-to-right
   (minimizing used height, leaving a clean horizontal strip at the top).
4. **Orient**: every candidate is evaluated in both the 0 and 90 degree
   orientations (an ellipse may sit tighter sideways; a circle is
   orientation-agnostic), and the position minimizing the primary extent
   wins.

Placements are returned as **plate-frame, y-down** centers (origin at the
usable area's top-left -- the emitted device convention, the same frame
rectpack fills in). The layout engine maps them to ``PackedLabel``
placements by adding the plate's clearances.

The module is pure Python + shapely: no rectpack, no matplotlib, no
generate-pipeline imports (it is imported lazily from ``layout.py`` only
when a job actually contains round labels). Numpy is used only inside the
placement index's vectorized center-distance prefilter (shapely itself
depends on numpy, so the generate path always has it).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Sequence, Tuple

import numpy as np
from shapely.affinity import scale as shapely_scale
from shapely.affinity import translate as shapely_translate
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.prepared import prep

logger = logging.getLogger(__name__)

#: Default polygonization resolution (points per quarter arc).
DEFAULT_NUM_POINTS: int = 32

#: Default grid step in inches for the candidate-position scan.
DEFAULT_STEP_SIZE_IN: float = 0.05

#: Default tool clearance/kerf allowance in inches (1/16").
DEFAULT_CLEARANCE_IN: float = 0.0625

#: Multiplier turning the primary extent into a lexicographic score: the
#: primary axis dominates, the secondary axis breaks ties.
_PRIMARY_WEIGHT: float = 1000.0

#: Circle-bound radii margins for the placement index. A buffered ellipse
#: polygon CONTAINS its inscribed circle (radius ``cos(pi/N) * min(semi_x,
#: semi_y) + clearance/2``, the apothem of the polygonized ring scaled to
#: the semi-axes) and is INSCRIBED in its circumcircle (radius
#: ``max(semi_x, semi_y) + clearance/2``). Center distance ``<`` the
#: inscribed-radii sum therefore PROVES the polygons intersect (the
#: inscribed circles are subsets of both), ``>`` the circum-radii sum
#: PROVES them disjoint; only the band between falls through to the exact
#: GEOS test. The margins absorb float noise in the squared-distance
#: compares so near-tangent pairs always reach GEOS (tangency is a
#: collision for ``intersects`` and must stay one). For CIRCLES the two
#: radii nearly coincide and the band is a sub-grid-step sliver, so a
#: packed circle sheet resolves almost entirely in numpy.
_PREFILTER_EPSILON_IN: float = 1e-9


def _polygon_inscribed_factor(num_points: int) -> float:
    """Return the apothem factor of the polygonized ellipse ring.

    ``ellipse_polygon`` polygonizes the unit circle as a regular
    ``4 * max(1, num_points // 4)``-gon (circumradius 1, inradius
    ``cos(pi / vertices)``) and scales it by ``(a, b)``. Every edge line of
    the ring sits at distance ``cos(pi/V)`` from the origin with unit normal
    ``n``, and the scaling maps it to a line at distance
    ``cos(pi/V) / sqrt((n_x/a)**2 + (n_y/b)**2) >= cos(pi/V) * min(a, b)``,
    so the polygon contains a circle of ``factor * min(a, b)``.

    Args:
        num_points: The polygonization resolution passed to
            :func:`ellipse_polygon`.

    Returns:
        The inscribed/circumscribed radius ratio of the ring polygon.
    """
    vertices = 4 * max(1, num_points // 4)
    return math.cos(math.pi / vertices)


class _PlacementIndex:
    """Placed-polygon index with a vectorized per-column center-distance test.

    Replaces the collision union of the original scan. Two facts make it
    exactly equivalent to ``union.intersects(candidate)``:

    - ``union ∩ c ≠ ∅`` iff some placed polygon intersects ``c`` (closed
      polygons, including at tangency), so per-polygon tests decide every
      candidate identically.
    - Inscribed/circumcircle bounds decide most candidates without GEOS:
      a candidate is provably **colliding** when its center comes within the
      inscribed-radii sum of a placed center, and provably **disjoint**
      beyond the circum-radii sum of every placed center. Only the band in
      between needs the exact per-polygon ``intersects`` test.

    The classification is batched per scan **column**: one numpy broadcast
    over the placed polygons whose primary-axis coordinate is within reach
    (``|fixed - center| <= R + R_i``) fills a ``len(scan) x active``
    squared-distance matrix; the per-candidate decision is a boolean row
    lookup, so a fully occupied column costs a handful of vectorized ops,
    zero per-candidate Python loops, and zero GEOS calls.

    The rectangular occupancy seed (rect labels sharing the plate) is a
    static geometry, prepared once for fast repeated tests.
    """

    def __init__(self, seed: Optional[BaseGeometry]) -> None:
        """Seed the index with the pre-occupied rectangle union (or None).

        Args:
            seed: Buffered union of the pre-occupied rectangles, or ``None``
                when the plate has no rectangular occupants.
        """
        self._seed_prepared = prep(seed) if seed is not None else None
        self._polygons: List[BaseGeometry] = []
        self._centers_x: List[float] = []
        self._centers_y: List[float] = []
        self._r_in: List[float] = []
        self._r_out: List[float] = []
        self._cx: Optional[np.ndarray] = None
        self._cy: Optional[np.ndarray] = None
        self._rin: Optional[np.ndarray] = None
        self._rout: Optional[np.ndarray] = None

    def add(
        self,
        polygon: BaseGeometry,
        center_x: float,
        center_y: float,
        radius_in: float,
        radius_out: float,
    ) -> None:
        """Index one placed polygon with its center and circle radii.

        Args:
            polygon: The placed (buffered) polygon.
            center_x: Placement center X, inches.
            center_y: Placement center Y, inches.
            radius_in: Inscribed-circle radius, inches.
            radius_out: Circumcircle radius, inches.
        """
        self._polygons.append(polygon)
        self._centers_x.append(center_x)
        self._centers_y.append(center_y)
        self._r_in.append(radius_in)
        self._r_out.append(radius_out)
        # Rebuild the scan arrays (n is one plate's item count, ~10^2).
        self._cx = np.asarray(self._centers_x)
        self._cy = np.asarray(self._centers_y)
        self._rin = np.asarray(self._r_in)
        self._rout = np.asarray(self._r_out)

    def column_scan(
        self,
        fixed: float,
        scan: np.ndarray,
        radius_in: float,
        radius_out: float,
        fixed_is_x: bool,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Classify every candidate of one scan column against placed ink.

        Args:
            fixed: The column's coordinate on the primary axis, inches.
            scan: Ascending candidate coordinates on the secondary axis.
            radius_in: Candidate inscribed-circle radius, inches.
            radius_out: Candidate circumcircle radius, inches.
            fixed_is_x: True when ``fixed`` is X (PREFER_WIDTH); the batch
                then varies Y.

        Returns:
            ``(collide, dist_sq, active)``: ``collide[i]`` is True when
            candidate ``i`` provably intersects a placed polygon (the
            inscribed-circle test); ``active`` lists the placed polygons
            whose primary-axis coordinate is within circumcircle reach of
            the column, and ``dist_sq[i, j]`` is candidate ``i``'s squared
            center distance to ``active[j]`` (the band test slices this
            matrix).
        """
        cx, cy, rin, rout = self._cx, self._cy, self._rin, self._rout
        if cx is None or cy is None or rin is None or rout is None or not self._polygons:
            return (
                np.zeros(scan.size, dtype=bool),
                np.zeros((scan.size, 0)),
                np.zeros(0, dtype=np.int64),
            )
        fixed_centers, scan_centers = (cx, cy) if fixed_is_x else (cy, cx)
        reach = rout + radius_out + _PREFILTER_EPSILON_IN
        active = np.flatnonzero(np.abs(fixed_centers - fixed) <= reach)
        if active.size == 0:
            return (
                np.zeros(scan.size, dtype=bool),
                np.zeros((scan.size, 0)),
                active,
            )
        fixed_delta_sq = (fixed_centers[active] - fixed) * (fixed_centers[active] - fixed)
        delta = scan[:, None] - scan_centers[active][None, :]
        dist_sq = fixed_delta_sq[None, :] + delta * delta
        collide_limit = (rin[active] + radius_in - _PREFILTER_EPSILON_IN) ** 2
        collide = np.any(dist_sq < collide_limit[None, :], axis=1)
        return collide, dist_sq, active

    def band_indices(
        self,
        dist_row: np.ndarray,
        active: np.ndarray,
        radius_in: float,
        radius_out: float,
    ) -> List[int]:
        """Return the placed polygons needing an exact GEOS test for one row.

        The band is the distance range where the circle bounds decide
        neither collision nor disjointness: at or beyond the
        inscribed-radii sum (collision unproven) and within the
        circum-radii sum (disjointness unproven).

        Args:
            dist_row: Squared center distances to the ``active`` polygons
                (candidate's row of :meth:`column_scan`'s matrix).
            active: Placed-polygon indices the row covers.
            radius_in: Candidate inscribed-circle radius, inches.
            radius_out: Candidate circumcircle radius, inches.

        Returns:
            Placed-polygon indices inside the band, in insertion order.
        """
        rin = self._rin
        rout = self._rout
        if rin is None or rout is None or active.size == 0:
            return []
        collide_limit = (rin[active] + radius_in - _PREFILTER_EPSILON_IN) ** 2
        reach_limit = (rout[active] + radius_out + _PREFILTER_EPSILON_IN) ** 2
        band = (dist_row >= collide_limit) & (dist_row <= reach_limit)
        return [int(active[j]) for j in np.flatnonzero(band)]

    def polygon(self, index: int) -> BaseGeometry:
        """Return the placed polygon at *index* (a band entry).

        Args:
            index: Position in insertion order (a ``band_indices`` entry).

        Returns:
            The placed (buffered) polygon.
        """
        return self._polygons[index]

    @property
    def has_seed(self) -> bool:
        """True when the plate carries rectangular occupancy to test."""
        return self._seed_prepared is not None

    def seed_intersects(self, candidate: BaseGeometry) -> bool:
        """Return True when the candidate touches the rectangular occupancy.

        Args:
            candidate: The translated candidate polygon.

        Returns:
            True when a seed geometry exists and intersects the candidate.
        """
        return self._seed_prepared is not None and bool(self._seed_prepared.intersects(candidate))


class ScrapDirection(str, Enum):
    """Which plate axis the packer minimizes (the scrap-strip direction).

    Attributes:
        PREFER_WIDTH: Minimize total used width X: scan increasing X
            columns, filling bottom-to-top in Y. Leaves one clean
            rectangular scrap strip on the plate's right side.
        PREFER_HEIGHT: Minimize total used height Y: scan increasing Y
            rows, filling left-to-right in X. Leaves one clean rectangular
            scrap strip along the plate's top.
    """

    PREFER_WIDTH = "prefer_width"
    PREFER_HEIGHT = "prefer_height"


@dataclass(frozen=True)
class EllipseSpec:
    """One ellipse to pack.

    Attributes:
        item_id: Caller-chosen identifier (the packed result carries it).
        semi_a: Semi-axis along the item's local X, inches (> 0).
        semi_b: Semi-axis along the item's local Y, inches (> 0).
    """

    item_id: str
    semi_a: float
    semi_b: float


@dataclass(frozen=True)
class PlacedEllipse:
    """One packed ellipse placement.

    Attributes:
        item_id: The :attr:`EllipseSpec.item_id` this placement belongs to.
        center_x: Center X in the plate frame (y-down, origin at the usable
            area's top-left), inches.
        center_y: Center Y in the plate frame, inches.
        rotation_deg: 0 or 90. 90 means the item's ``semi_a`` axis runs
            along the plate's Y (the ellipse is turned clockwise at
            assembly, matching the rectangular pipeline's rotation
            convention).
        semi_x: Effective semi-axis along the plate X after rotation.
        semi_y: Effective semi-axis along the plate Y after rotation.
    """

    item_id: str
    center_x: float
    center_y: float
    rotation_deg: int
    semi_x: float
    semi_y: float


@dataclass(frozen=True)
class EllipsePackingResult:
    """The outcome of one packing pass over one plate.

    Attributes:
        placed: Every placement, in input order.
        unplaced_ids: ``item_id`` of every item the plate could not fit.
        used_width_in: Right-most engraved extent (max center_x + semi_x).
        used_height_in: Top-most engraved extent (max center_y + semi_y).
    """

    placed: Tuple[PlacedEllipse, ...]
    unplaced_ids: Tuple[str, ...]
    used_width_in: float
    used_height_in: float


def ellipse_polygon(
    semi_a: float,
    semi_b: float,
    clearance: float = 0.0,
    num_points: int = DEFAULT_NUM_POINTS,
) -> BaseGeometry:
    """Build an axis-aligned ellipse polygon centered at the origin.

    The unit circle is polygonized at ``num_points // 4`` vertices per
    quarter arc and scaled to ``semi_a + clearance / 2`` by
    ``semi_b + clearance / 2``, so the polygon already includes half the
    tool kerf on every side -- a plain :meth:`intersects` test between two
    such polygons is collision-exact for the requested clearance.

    Args:
        semi_a: Semi-axis along X, inches.
        semi_b: Semi-axis along Y, inches.
        clearance: Total kerf/spacing allowance, inches (each polygon
            grows by half of it on every side).
        num_points: Polygonization resolution (points per quarter arc).

    Returns:
        The buffered ellipse polygon (shapely geometry).
    """
    circle = Point(0.0, 0.0).buffer(1.0, quad_segs=max(1, num_points // 4))
    return shapely_scale(
        circle,
        xfact=semi_a + clearance / 2.0,
        yfact=semi_b + clearance / 2.0,
        origin=(0.0, 0.0),
    )


def _occupancy_union(
    occupancy: Sequence[Tuple[float, float, float, float]],
    clearance: float,
) -> BaseGeometry | None:
    """Buffer and union the pre-occupied rectangles into one collision shape.

    Args:
        occupancy: ``(x_min, y_min, x_max, y_max)`` rectangles.
        clearance: Kerf allowance, inches (each rectangle grows by half).

    Returns:
        The union polygon, or ``None`` when nothing is occupied.
    """
    from shapely.geometry import box as shapely_box

    if not occupancy:
        return None
    buffered = [
        shapely_box(
            x_min - clearance / 2.0,
            y_min - clearance / 2.0,
            x_max + clearance / 2.0,
            y_max + clearance / 2.0,
        )
        for x_min, y_min, x_max, y_max in occupancy
    ]
    return buffered[0] if len(buffered) == 1 else unary_union(buffered)


class EllipsePacker:
    """Greedy grid-search packer for ellipses on a rectangular plate.

    The packer scans candidate centers on a fixed grid over the usable
    plate area, testing each candidate against the union of every
    previously placed (buffered) ellipse polygon. The scan order and the
    extent score implement the :class:`ScrapDirection` objectives.
    """

    def __init__(
        self,
        plate_width_in: float,
        plate_height_in: float,
        step_size_in: float = DEFAULT_STEP_SIZE_IN,
        num_points: int = DEFAULT_NUM_POINTS,
    ) -> None:
        """Configure the plate and scan resolution.

        Args:
            plate_width_in: Usable plate width, inches.
            plate_height_in: Usable plate height, inches.
            step_size_in: Candidate-grid step, inches. Smaller steps find
                tighter nests at cubic-ish cost; the step is snapped so it
                divides both plate axes exactly.
            num_points: Polygonization resolution per quarter arc.
        """
        if plate_width_in <= 0.0 or plate_height_in <= 0.0:
            raise ValueError("plate dimensions must be positive")
        if step_size_in <= 0.0:
            raise ValueError("step_size_in must be positive")
        self.plate_w = float(plate_width_in)
        self.plate_h = float(plate_height_in)
        self.step_size = self._snap_step(step_size_in)
        self.num_points = int(num_points)

    def _snap_step(self, step_size_in: float) -> float:
        """Snap the grid step to divide both plate axes exactly.

        ``numpy.arange``-style scans accumulate float drift and can miss
        the final row/column; snapping the step to an integer count of
        cells per axis keeps the scan exact on both axes.

        Args:
            step_size_in: Requested step, inches.

        Returns:
            The snapped step (at most the requested value, so the scan
            never gets coarser than asked).
        """
        cells_x = max(1, math.ceil(self.plate_w / step_size_in))
        cells_y = max(1, math.ceil(self.plate_h / step_size_in))
        return min(self.plate_w / cells_x, self.plate_h / cells_y)

    def pack(
        self,
        specs: Sequence[EllipseSpec],
        mode: ScrapDirection = ScrapDirection.PREFER_WIDTH,
        clearance: float = DEFAULT_CLEARANCE_IN,
        occupancy: Sequence[Tuple[float, float, float, float]] = (),
    ) -> EllipsePackingResult:
        """Pack every ellipse onto the plate, largest-area first.

        Args:
            specs: The ellipses to pack.
            mode: Primary scrap direction (see :class:`ScrapDirection`).
            clearance: Tool kerf/spacing allowance, inches (each polygon
                is buffered by half of it; two labels therefore keep at
                least ``clearance`` of edge-to-edge air).
            occupancy: ``(x_min, y_min, x_max, y_max)`` rectangles in the
                same plate frame the placements are returned in (usable
                area, y-down) that are already taken -- typically the
                rectangular labels sharing the plate. Each rectangle is
                buffered by ``clearance / 2`` so a round label keeps the
                same kerf from a rectangular neighbour as from another
                ellipse.

        Returns:
            The placements, the ids that did not fit, and the used
            extents. Placements preserve the *input* order (the sort is
            internal).
        """
        sorted_specs = sorted(specs, key=lambda s: s.semi_a * s.semi_b, reverse=True)

        placements: dict[str, PlacedEllipse] = {}
        unplaced: List[str] = []
        index = _PlacementIndex(_occupancy_union(occupancy, clearance))

        for spec in sorted_specs:
            best = self._find_best_position(spec, index, clearance, mode)
            if best is None:
                unplaced.append(spec.item_id)
                logger.warning(
                    "EllipsePacker: item %r (semi-axes %.3f x %.3fin) does not "
                    "fit the %.1f x %.1fin plate.",
                    spec.item_id,
                    spec.semi_a,
                    spec.semi_b,
                    self.plate_w,
                    self.plate_h,
                )
                continue
            placed, candidate = best
            placements[spec.item_id] = placed
            # The candidate polygon's buffered semi-axes are
            # (placed.semi + clearance/2); its inradius is at least
            # factor * min(buffered semi-axes).
            radius_in = _polygon_inscribed_factor(self.num_points) * (
                min(placed.semi_x, placed.semi_y) + clearance / 2.0
            )
            radius_out = max(placed.semi_x, placed.semi_y) + clearance / 2.0
            index.add(candidate, placed.center_x, placed.center_y, radius_in, radius_out)

        placed_in_input_order = tuple(
            placements[spec.item_id] for spec in specs if spec.item_id in placements
        )
        used_width = max(
            (p.center_x + p.semi_x for p in placed_in_input_order),
            default=0.0,
        )
        used_height = max(
            (p.center_y + p.semi_y for p in placed_in_input_order),
            default=0.0,
        )
        return EllipsePackingResult(
            placed=placed_in_input_order,
            unplaced_ids=tuple(unplaced),
            used_width_in=used_width,
            used_height_in=used_height,
        )

    def _find_best_position(
        self,
        spec: EllipseSpec,
        index: _PlacementIndex,
        clearance: float,
        mode: ScrapDirection,
    ) -> Tuple[PlacedEllipse, BaseGeometry] | None:
        """Scan every grid position/orientation for one item's best fit.

        The scan walks the primary axis in ascending order (outer loop) and
        the secondary axis in ascending order (inner loop); the first
        non-colliding, in-bounds candidate is the optimum for that
        orientation (both axes ascend, so the extent score is minimized by
        the first hit), and the two orientations' winners are compared.
        Because the secondary axis ascends too, the scan stops at the first
        feasible candidate of a column: every later candidate in the same
        column scores worse, and every later column scores worse than this
        one -- so the first hit is the orientation's optimum.

        Feasibility is decided by :class:`_PlacementIndex`: a vectorized
        center-distance prefilter proves most candidates disjoint without
        touching GEOS, and only a candidate that survives it is translated
        for the exact per-polygon ``intersects`` tests.

        Args:
            spec: The item to place.
            index: The placed-polygon index (includes the occupancy seed).
            clearance: Kerf allowance, inches.
            mode: Primary scrap direction.

        Returns:
            ``(placement, candidate_polygon)`` for the best fit, or
            ``None`` when no grid position fits.
        """
        best: Tuple[float, PlacedEllipse, BaseGeometry] | None = None
        # Orientations: 0 deg keeps (semi_a along X), 90 deg swaps them.
        # A circle dedupes to one orientation via the (semi_x, semi_y) key.
        for semi_x, semi_y, rotation in (
            (spec.semi_a, spec.semi_b, 0),
            (spec.semi_b, spec.semi_a, 90),
        ):
            if rotation == 90 and math.isclose(spec.semi_a, spec.semi_b, rel_tol=1e-12):
                continue
            base_poly = ellipse_polygon(semi_x, semi_y, clearance, self.num_points)
            half_w, half_h = semi_x + clearance / 2.0, semi_y + clearance / 2.0
            radius_in = _polygon_inscribed_factor(self.num_points) * min(half_w, half_h)
            radius_out = max(half_w, half_h)
            primary = self._axis_range(self.plate_w, half_w)
            secondary = self._axis_range(self.plate_h, half_h)
            secondary_scan = np.asarray(secondary)
            prefer_width = mode is ScrapDirection.PREFER_WIDTH
            for primary_pos in primary:
                # One vectorized classification for the whole column:
                # `collide[i]` proves candidate i collides (inscribed
                # circles), `band` lists the polygons candidate i needs an
                # exact GEOS test against.
                collide, dist_sq, active = index.column_scan(
                    primary_pos,
                    secondary_scan,
                    radius_in,
                    radius_out,
                    fixed_is_x=prefer_width,
                )
                for row, secondary_pos in enumerate(secondary):
                    if prefer_width:
                        x, y = primary_pos, secondary_pos
                    else:
                        x, y = secondary_pos, primary_pos
                    candidate: Optional[BaseGeometry] = None
                    if collide[row]:
                        continue
                    if not index.has_seed:
                        band = index.band_indices(dist_sq[row], active, radius_in, radius_out)
                        if band:
                            candidate = shapely_translate(base_poly, xoff=x, yoff=y)
                            if any(index.polygon(placed).intersects(candidate) for placed in band):
                                continue
                    else:
                        candidate = shapely_translate(base_poly, xoff=x, yoff=y)
                        band = index.band_indices(dist_sq[row], active, radius_in, radius_out)
                        if any(
                            index.polygon(placed).intersects(candidate) for placed in band
                        ) or index.seed_intersects(candidate):
                            continue
                    extent = (
                        (x + half_w) * _PRIMARY_WEIGHT + (y + half_h)
                        if prefer_width
                        else (y + half_h) * _PRIMARY_WEIGHT + (x + half_w)
                    )
                    if best is None or extent < best[0]:
                        if candidate is None:
                            candidate = shapely_translate(base_poly, xoff=x, yoff=y)
                        best = (
                            extent,
                            PlacedEllipse(
                                item_id=spec.item_id,
                                center_x=x,
                                center_y=y,
                                rotation_deg=rotation,
                                semi_x=semi_x,
                                semi_y=semi_y,
                            ),
                            candidate,
                        )
                    # The secondary axis ascends: every later candidate in
                    # this column scores worse, and every later primary
                    # position scores worse than this column's first hit.
                    # The first feasible candidate is the optimum.
                    break
                if best is not None:
                    break
        if best is None:
            return None
        return best[1], best[2]

    def _axis_range(self, plate_extent: float, half_extent: float) -> List[float]:
        """Grid positions along one axis keeping the item inside the plate.

        Args:
            plate_extent: Plate width/height, inches.
            half_extent: The item's (buffered) half extent on this axis.

        Returns:
            Ascending centers in ``[half_extent, plate_extent -
            half_extent]`` (empty when the item cannot fit at all).
        """
        low = half_extent
        high = plate_extent - half_extent
        if high < low - 1e-12:
            return []
        count = int(math.floor((high - low) / self.step_size + 1e-9))
        positions = [low + self.step_size * i for i in range(count + 1)]
        # Snap the final position onto the far edge when it is within half
        # a step (the grid never exceeds the bounds).
        if high - positions[-1] > 0.5 * self.step_size:
            positions.append(high)
        return positions


def pack_ellipses(
    plate_width_in: float,
    plate_height_in: float,
    specs: Sequence[EllipseSpec],
    mode: ScrapDirection = ScrapDirection.PREFER_WIDTH,
    clearance: float = DEFAULT_CLEARANCE_IN,
    step_size_in: float = DEFAULT_STEP_SIZE_IN,
    num_points: int = DEFAULT_NUM_POINTS,
    occupancy: Sequence[Tuple[float, float, float, float]] = (),
) -> EllipsePackingResult:
    """One-shot convenience wrapper around :class:`EllipsePacker`.

    Args:
        plate_width_in: Usable plate width, inches.
        plate_height_in: Usable plate height, inches.
        specs: The ellipses to pack.
        mode: Primary scrap direction.
        clearance: Kerf allowance, inches.
        step_size_in: Candidate-grid step, inches.
        num_points: Polygonization resolution per quarter arc.
        occupancy: Pre-occupied ``(x_min, y_min, x_max, y_max)`` rectangles
            (plate frame, y-down) the ellipses must avoid.

    Returns:
        The packing result (see :meth:`EllipsePacker.pack`).
    """
    packer = EllipsePacker(plate_width_in, plate_height_in, step_size_in, num_points)
    return packer.pack(specs, mode=mode, clearance=clearance, occupancy=occupancy)
