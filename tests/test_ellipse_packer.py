"""Unit tests for the circular/elliptical label packer.

Covers :mod:`plt_optimizer.generate.ellipse_packer`: polygonization,
bounds, clearance, collision-freedom, the scrap-direction objectives,
rotation selection, and the fit-failure path.
"""

from __future__ import annotations

import math

import pytest

from shapely.affinity import translate as shapely_translate

from plt_optimizer.generate.ellipse_packer import (
    DEFAULT_CLEARANCE_IN,
    EllipsePacker,
    EllipsePackingResult,
    EllipseSpec,
    ScrapDirection,
    ellipse_polygon,
    pack_ellipses,
)


class TestEllipsePolygon:
    """Polygonization and kerf buffering."""

    def test_circle_radius_matches_semi_axis(self) -> None:
        polygon = ellipse_polygon(1.0, 1.0, clearance=0.0)
        min_x, min_y, max_x, max_y = polygon.bounds
        assert max_x == pytest.approx(1.0, abs=1e-3)
        assert max_y == pytest.approx(1.0, abs=1e-3)
        assert min_x == pytest.approx(-1.0, abs=1e-3)
        assert min_y == pytest.approx(-1.0, abs=1e-3)

    def test_clearance_grows_polygon_by_half(self) -> None:
        plain = ellipse_polygon(1.0, 0.5, clearance=0.0)
        buffered = ellipse_polygon(1.0, 0.5, clearance=0.0625)
        assert (buffered.bounds[2] - plain.bounds[2]) == pytest.approx(0.03125, abs=1e-6)
        assert (plain.bounds[1] - buffered.bounds[1]) == pytest.approx(0.03125, abs=1e-6)

    def test_ellipse_axes_are_independent(self) -> None:
        polygon = ellipse_polygon(2.0, 0.5, clearance=0.0)
        min_x, min_y, max_x, max_y = polygon.bounds
        assert (max_x - min_x) == pytest.approx(4.0, abs=1e-3)
        assert (max_y - min_y) == pytest.approx(1.0, abs=1e-3)


class TestPackerConstruction:
    """Constructor validation and grid-step snapping."""

    def test_rejects_non_positive_plate(self) -> None:
        with pytest.raises(ValueError, match="plate dimensions"):
            EllipsePacker(0.0, 8.0)
        with pytest.raises(ValueError, match="plate dimensions"):
            EllipsePacker(12.0, -1.0)

    def test_rejects_non_positive_step(self) -> None:
        with pytest.raises(ValueError, match="step_size_in"):
            EllipsePacker(12.0, 8.0, step_size_in=0.0)

    def test_axis_range_spans_the_axis_densely(self) -> None:
        packer = EllipsePacker(10.0, 8.0, step_size_in=0.3)
        # The scan covers the whole axis: first position flush with the low
        # edge, positions at most one step apart, last position within one
        # step of the far edge, and nothing ever exceeds the bounds.
        for extent in (10.0, 8.0):
            positions = packer._axis_range(extent, 0.0)
            assert positions[0] == pytest.approx(0.0, abs=1e-12)
            assert positions[-1] >= extent - packer.step_size - 1e-12
            assert all(p <= extent + 1e-12 for p in positions)
            gaps = [b - a for a, b in zip(positions, positions[1:])]
            assert all(gap <= packer.step_size + 1e-12 for gap in gaps)

    def test_axis_range_is_empty_when_item_cannot_fit(self) -> None:
        packer = EllipsePacker(4.0, 4.0, step_size_in=0.1)
        assert packer._axis_range(4.0, 2.5) == []


class TestSinglePlacement:
    """First-item placement sits in the preferred corner."""

    def test_prefer_width_places_in_bottom_left(self) -> None:
        result = pack_ellipses(12.0, 8.0, [EllipseSpec("a", 1.0, 1.0)], ScrapDirection.PREFER_WIDTH)
        assert len(result.placed) == 1
        placed = result.placed[0]
        # Buffered half-extent = 1.0 + clearance/2; flush with both edges.
        assert placed.center_x == pytest.approx(1.0 + DEFAULT_CLEARANCE_IN / 2.0, abs=1e-6)
        assert placed.center_y == pytest.approx(1.0 + DEFAULT_CLEARANCE_IN / 2.0, abs=1e-6)
        assert placed.rotation_deg == 0
        assert result.unplaced_ids == ()

    def test_used_extents_track_the_placement(self) -> None:
        result = pack_ellipses(12.0, 8.0, [EllipseSpec("a", 2.0, 1.0)], ScrapDirection.PREFER_WIDTH)
        placed = result.placed[0]
        # The wide ellipse stands up (90 deg) to minimize the used width, so
        # the extents follow the *placed* semi-axes, not the input ones.
        assert placed.rotation_deg == 90
        assert result.used_width_in == pytest.approx(placed.center_x + placed.semi_x, abs=1e-9)
        assert result.used_height_in == pytest.approx(placed.center_y + placed.semi_y, abs=1e-9)
        # Extents are the engraved ink edges (no kerf buffer included):
        # center_x (flush + kerf/2) + the placed semi_x (1.0, rotated).
        assert result.used_width_in == pytest.approx(
            1.0 + DEFAULT_CLEARANCE_IN / 2.0 + 1.0, abs=1e-6
        )

    def test_empty_input_is_a_no_op(self) -> None:
        result = pack_ellipses(12.0, 8.0, [])
        assert result.placed == ()
        assert result.unplaced_ids == ()
        assert result.used_width_in == 0.0
        assert result.used_height_in == 0.0


class TestClearanceAndCollisions:
    """Placed ellipses never overlap and keep the requested kerf."""

    def test_circles_keep_clearance_apart(self) -> None:
        specs = [EllipseSpec(f"c{i}", 1.0, 1.0) for i in range(4)]
        result = pack_ellipses(12.0, 12.0, specs, ScrapDirection.PREFER_WIDTH, clearance=0.25)
        assert len(result.placed) == 4
        for i, first in enumerate(result.placed):
            for second in result.placed[i + 1 :]:
                centre_distance = math.hypot(
                    first.center_x - second.center_x, first.center_y - second.center_y
                )
                # Two unit circles need 2 * radius + clearance centre distance.
                assert centre_distance >= 2.0 + 0.25 - 1e-9

    def test_polygons_do_not_intersect(self) -> None:
        specs = [EllipseSpec(f"e{i}", 1.5, 0.75) for i in range(3)]
        result = pack_ellipses(10.0, 10.0, specs, ScrapDirection.PREFER_WIDTH, clearance=0.125)
        polygons = [
            shapely_translate(
                ellipse_polygon(p.semi_x, p.semi_y, 0.125), xoff=p.center_x, yoff=p.center_y
            )
            for p in result.placed
        ]
        for i, first in enumerate(polygons):
            for second in polygons[i + 1 :]:
                assert not first.intersects(second)

    def test_zero_clearance_allows_tangency(self) -> None:
        specs = [EllipseSpec(f"c{i}", 1.0, 1.0) for i in range(2)]
        result = pack_ellipses(12.0, 4.0, specs, ScrapDirection.PREFER_WIDTH, clearance=0.0)
        assert len(result.placed) == 2
        first, second = result.placed
        centre_distance = math.hypot(first.center_x - second.center_x, first.center_y - second.center_y)
        assert centre_distance >= 2.0 - 1e-9


class TestScrapDirection:
    """The primary objective minimizes the requested axis."""

    def test_prefer_width_minimizes_used_width(self) -> None:
        specs = [EllipseSpec(f"c{i}", 1.0, 1.0) for i in range(6)]
        width_result = pack_ellipses(24.0, 16.0, specs, ScrapDirection.PREFER_WIDTH, 0.0625, 0.1)
        height_result = pack_ellipses(24.0, 16.0, specs, ScrapDirection.PREFER_HEIGHT, 0.0625, 0.1)
        assert width_result.used_width_in < height_result.used_width_in
        assert height_result.used_height_in < width_result.used_height_in

    def test_prefer_width_leaves_one_vertical_column(self) -> None:
        specs = [EllipseSpec(f"c{i}", 0.5, 0.5) for i in range(8)]
        result = pack_ellipses(24.0, 16.0, specs, ScrapDirection.PREFER_WIDTH, 0.0625, 0.1)
        # Every centre shares the first column's X (used width is one column
        # plus the kerf the packer must keep from the plate's low edge).
        xs = {round(p.center_x, 6) for p in result.placed}
        assert len(xs) == 1
        assert result.used_width_in == pytest.approx(1.0 + DEFAULT_CLEARANCE_IN / 2.0, abs=1e-6)


class TestRotation:
    """Sideways orientation is chosen when it packs tighter."""

    def test_wide_ellipse_rotates_to_fit_narrow_plate(self) -> None:
        # A 3.0 x 1.0 ellipse (semi 1.5 x 0.5) on a 1.2in-wide plate must
        # stand up (90 deg) to fit at all.
        result = pack_ellipses(1.2, 4.0, [EllipseSpec("wide", 1.5, 0.5)], clearance=0.0)
        assert len(result.placed) == 1
        assert result.placed[0].rotation_deg == 90
        assert result.placed[0].semi_x == pytest.approx(0.5)
        assert result.placed[0].semi_y == pytest.approx(1.5)

    def test_circle_never_reports_rotation(self) -> None:
        result = pack_ellipses(6.0, 6.0, [EllipseSpec("c", 2.0, 2.0)])
        assert result.placed[0].rotation_deg == 0


class TestFitFailures:
    """Unfittable items are reported, never silently dropped."""

    def test_item_larger_than_plate_is_unplaced(self) -> None:
        result = pack_ellipses(2.0, 2.0, [EllipseSpec("big", 3.0, 3.0)])
        assert result.placed == ()
        assert result.unplaced_ids == ("big",)

    def test_partial_fit_reports_the_remainder(self) -> None:
        specs = [EllipseSpec(f"c{i}", 1.0, 1.0) for i in range(5)]
        # A 2.5 x 2.5 plate fits exactly one buffered unit circle.
        result = pack_ellipses(2.5, 2.5, specs, clearance=0.0)
        assert len(result.placed) == 1
        assert len(result.unplaced_ids) == 4


class TestOccupancy:
    """Pre-occupied rectangles (rectangular labels) are respected."""

    def test_ellipse_avoids_the_occupied_rectangle(self) -> None:
        # Occupy the plate's bottom-left 4x4 usable area; a unit circle
        # must land clear of it (kerf included).
        result = pack_ellipses(
            12.0,
            12.0,
            [EllipseSpec("c", 1.0, 1.0)],
            ScrapDirection.PREFER_WIDTH,
            clearance=0.25,
            step_size_in=0.25,
            occupancy=[(0.0, 0.0, 4.0, 4.0)],
        )
        assert len(result.placed) == 1
        placed = result.placed[0]
        # Buffered circle must not intersect the buffered rectangle.
        circle = shapely_translate(
            ellipse_polygon(placed.semi_x, placed.semi_y, 0.25),
            xoff=placed.center_x,
            yoff=placed.center_y,
        )
        from shapely.geometry import box as shapely_box

        occupied = shapely_box(-0.125, -0.125, 4.125, 4.125)
        assert not circle.intersects(occupied)

    def test_occupancy_can_make_an_item_unplaceable(self) -> None:
        result = pack_ellipses(
            4.0,
            4.0,
            [EllipseSpec("c", 1.0, 1.0)],
            ScrapDirection.PREFER_WIDTH,
            clearance=0.0,
            step_size_in=0.25,
            occupancy=[(0.0, 0.0, 4.0, 4.0)],
        )
        assert result.placed == ()
        assert result.unplaced_ids == ("c",)

    def test_empty_occupancy_is_a_no_op(self) -> None:
        with_none = pack_ellipses(6.0, 6.0, [EllipseSpec("c", 1.0, 1.0)])
        with_empty = pack_ellipses(6.0, 6.0, [EllipseSpec("c", 1.0, 1.0)], occupancy=[])
        assert with_none.placed == with_empty.placed


class TestResultShape:
    """Result ordering and dataclass contract."""

    def test_placements_follow_input_order_not_area_order(self) -> None:
        specs = [
            EllipseSpec("small", 0.5, 0.5),
            EllipseSpec("large", 2.0, 2.0),
        ]
        result = pack_ellipses(12.0, 12.0, specs, clearance=0.0)
        assert [p.item_id for p in result.placed] == ["small", "large"]

    def test_result_is_frozen(self) -> None:
        result = pack_ellipses(6.0, 6.0, [EllipseSpec("a", 1.0, 1.0)])
        assert isinstance(result, EllipsePackingResult)
        with pytest.raises(Exception):
            result.used_width_in = 99.0  # type: ignore[misc]
