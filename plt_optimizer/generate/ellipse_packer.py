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
when a job actually contains round labels).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from enum import Enum
from typing import List, Sequence, Tuple

from shapely.affinity import scale as shapely_scale
from shapely.affinity import translate as shapely_translate
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

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
        occupied: BaseGeometry | None = _occupancy_union(occupancy, clearance)

        for spec in sorted_specs:
            best = self._find_best_position(spec, occupied, clearance, mode)
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
            occupied = candidate if occupied is None else unary_union([occupied, candidate])

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
        occupied: BaseGeometry | None,
        clearance: float,
        mode: ScrapDirection,
    ) -> Tuple[PlacedEllipse, BaseGeometry] | None:
        """Scan every grid position/orientation for one item's best fit.

        The scan walks the primary axis in ascending order (outer loop) and
        the secondary axis in ascending order (inner loop); the first
        non-colliding, in-bounds candidate is the optimum for that
        orientation (both axes ascend, so the extent score is minimized by
        the first hit), and the two orientations' winners are compared.

        Args:
            spec: The item to place.
            occupied: Union of previously placed buffered polygons.
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

            primary = self._axis_range(self.plate_w, half_w)
            secondary = self._axis_range(self.plate_h, half_h)
            for primary_pos in primary:
                for secondary_pos in secondary:
                    if mode is ScrapDirection.PREFER_WIDTH:
                        x, y = primary_pos, secondary_pos
                    else:
                        x, y = secondary_pos, primary_pos
                    candidate = shapely_translate(base_poly, xoff=x, yoff=y)
                    if occupied is not None and occupied.intersects(candidate):
                        continue
                    extent = (
                        (x + half_w) * _PRIMARY_WEIGHT + (y + half_h)
                        if mode is ScrapDirection.PREFER_WIDTH
                        else (y + half_h) * _PRIMARY_WEIGHT + (x + half_w)
                    )
                    if best is None or extent < best[0]:
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
                # The primary axis ascends: every later primary position
                # scores worse for this orientation, so stop at the first
                # hit. The secondary axis scan is what found it.
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
