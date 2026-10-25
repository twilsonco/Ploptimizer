"""Rounded-rectangle label border tests (``rounded_rectangle_fraction``).

Covers the G2 superellipse-corner border added to
``plt_optimizer.generate``:

- the corner-parameter math (``rounded_rect_corner_params``): the behavior
  matrix over ``fraction`` x ``proportional`` x aspect;
- the boundary contour (``_rounded_rect_boundary_polygon``): closed, exact
  nominal-box bounds, and **continuous curvature** across every
  straight/patch junction (the G2 contract a circular fillet cannot meet);
- the exact circle/ellipse limits (``fraction == 1``) and the smoothed
  stadium;
- the corner-aware content inset (``rounded_rect_content_inset``);
- corner drill-hole placement on the patch normal
  (``rounded_rect_corner_hole``);
- the descender relief's rounded bottom border
  (``_border_box`` / ``_lower_rounded_rect_border``);
- the renderer predicates routing each label onto the sharp / rounded /
  ellipse path.

Sharp labels (``fraction == 0.0``, the default) are asserted to keep the
historical rectangle, so the rounded path is provably adjacent.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pytest

from plt_optimizer.generate.collision.holes import (
    hole_circles_local,
    rounded_rect_corner_hole,
)
from plt_optimizer.generate.collision.shapes import (
    rounded_rect_corner,
    rounded_rect_full_ellipse,
    uses_ellipse_boundary,
)
from plt_optimizer.generate.label_renderer import (
    _border_box,
    _lower_rounded_rect_border,
    _render_boundary_local,
    _rounded_rect_boundary_polygon,
)
from plt_optimizer.generate.resolution import (
    STADIUM_G2_EXPONENT,
    ResolvedHoleSpec,
    ResolvedLabel,
    ResolvedTextLine,
    RoundedRectCorner,
    rounded_rect_content_inset,
    rounded_rect_corner_params,
)


def _make_line(text: str, height: float = 0.3, cutter: float = 0.03) -> ResolvedTextLine:
    """Build a cutter-compensated ``ResolvedTextLine`` for local rendering."""
    return ResolvedTextLine(
        text=text,
        nominal_text_height=height,
        toolpath_text_height=height - cutter,
        cutter_diameter=cutter,
        character_spacing=0.0,
        line_spacing=0.0,
    )


def _label(
    width: float = 4.0,
    height: float = 2.0,
    margin: float = 0.1,
    hole_margin: float = 0.15,
    holes: Optional[list] = None,
    content: Optional[list[ResolvedTextLine]] = None,
    fraction: float = 0.0,
    proportional: bool = False,
    shape_type: str = "rect",
) -> ResolvedLabel:
    """Build a minimal ``ResolvedLabel`` with chosen rounding parameters."""
    return ResolvedLabel(
        id="rr_label",
        count=1,
        width=width,
        height=height,
        margin=margin,
        h_margin=margin,
        v_margin=margin,
        hole_margin=hole_margin,
        holes=holes or [],
        content=content if content is not None else [_make_line("HELLO")],
        rounded_rectangle_fraction=fraction,
        rounded_rectangle_proportional=proportional,
        shape_type=shape_type,
    )


def _hole(location: str, diameter: float = 0.125) -> ResolvedHoleSpec:
    """Build a resolved drill-hole spec at the given location."""
    return ResolvedHoleSpec(diameter=diameter, location=location)


def _menger_curvature(a: complex, b: complex, c: complex) -> float:
    """Curvature of the circle through three points (0 when collinear)."""
    ab, bc, ca = abs(a - b), abs(b - c), abs(c - a)
    if ab * bc * ca == 0.0:
        return 0.0
    area2 = abs((b.real - a.real) * (c.imag - a.imag) - (c.real - a.real) * (b.imag - a.imag))
    return 2.0 * area2 / (ab * bc * ca)


class TestRoundedRectCornerParams:
    """The behavior matrix of ``rounded_rect_corner_params``."""

    def test_zero_fraction_is_sharp(self) -> None:
        """fraction 0 (and negatives / NaN) resolve to the sharp rectangle."""
        assert rounded_rect_corner_params(4.0, 2.0, 0.0, False) is None
        assert rounded_rect_corner_params(4.0, 2.0, -1.0, False) is None
        assert rounded_rect_corner_params(4.0, 2.0, math.nan, False) is None

    def test_degenerate_size_is_sharp(self) -> None:
        """A zero/negative dimension has no border to round."""
        assert rounded_rect_corner_params(0.0, 2.0, 0.5, False) is None
        assert rounded_rect_corner_params(4.0, -1.0, 0.5, False) is None

    def test_subnormal_fraction_underflows_to_sharp(self) -> None:
        """A subnormal fraction whose rounding distance underflows is sharp."""
        # 5e-324 is the smallest subnormal; halving it rounds to 0.0, so the
        # patch semi-axis vanishes and the border stays the sharp rectangle.
        assert rounded_rect_corner_params(1.0, 1.0, 5e-324, True) is None

    def test_equal_distance_uses_shorter_edge(self) -> None:
        """The TODO example: 4x8 at 0.5 rounds 2in of the 4in sides."""
        corner = rounded_rect_corner_params(4.0, 8.0, 0.5, False)
        assert corner is not None
        # rx == ry: one identical rounding distance on every side.
        assert corner.rx == pytest.approx(1.0)
        assert corner.ry == pytest.approx(1.0)
        assert corner.exponent == pytest.approx(4.0)

    def test_proportional_scales_each_dimension(self) -> None:
        """Proportional true: rx = f*W/2, ry = f*H/2 (50% of the 8in side)."""
        corner = rounded_rect_corner_params(4.0, 8.0, 0.5, True)
        assert corner is not None
        assert corner.rx == pytest.approx(1.0)
        assert corner.ry == pytest.approx(2.0)
        assert corner.exponent == pytest.approx(4.0)

    def test_fraction_one_proportional_is_ellipse(self) -> None:
        """fraction 1 proportional: semi-axes W/2 H/2, exponent 2 (ellipse)."""
        corner = rounded_rect_corner_params(4.0, 8.0, 1.0, True)
        assert corner is not None
        assert (corner.rx, corner.ry, corner.exponent) == (2.0, 4.0, 2.0)

    def test_fraction_one_square_is_circle(self) -> None:
        """fraction 1 on a square: the circle, exponent 2 (both modes)."""
        corner = rounded_rect_corner_params(4.0, 4.0, 1.0, False)
        assert corner is not None
        assert (corner.rx, corner.ry, corner.exponent) == (2.0, 2.0, 2.0)

    def test_fraction_one_non_square_is_smoothed_stadium(self) -> None:
        """The stadium limit clamps the exponent above 2 to keep G2."""
        corner = rounded_rect_corner_params(4.0, 8.0, 1.0, False)
        assert corner is not None
        # Short edges fully rounded (r = 2in), long edges keep straights.
        assert (corner.rx, corner.ry) == (2.0, 2.0)
        assert corner.exponent == STADIUM_G2_EXPONENT

    def test_exponent_is_two_over_fraction(self) -> None:
        """The exponent family pins the endpoints: n = 2 / fraction."""
        for fraction in (0.1, 0.25, 0.5, 0.8):
            corner = rounded_rect_corner_params(4.0, 2.0, fraction, False)
            assert corner is not None
            assert corner.exponent == pytest.approx(2.0 / fraction)


class TestRoundedRectBoundaryPolygon:
    """The closed boundary contour."""

    @pytest.mark.parametrize(
        ("width", "height", "fraction", "proportional"),
        [
            (4.0, 2.0, 0.25, False),
            (4.0, 8.0, 0.5, False),
            (4.0, 8.0, 0.5, True),
            (4.0, 8.0, 1.0, False),
            (6.0, 6.0, 0.9, True),
        ],
    )
    def test_closed_and_box_exact(
        self, width: float, height: float, fraction: float, proportional: bool
    ) -> None:
        """The contour closes and reaches the nominal box's extrema exactly."""
        corner = rounded_rect_corner_params(width, height, fraction, proportional)
        assert corner is not None
        pts = _rounded_rect_boundary_polygon(width, height, corner)
        assert abs(pts[0] - pts[-1]) < 1e-12
        assert pts.real.min() == pytest.approx(0.0, abs=1e-12)
        assert pts.real.max() == pytest.approx(width, abs=1e-12)
        assert pts.imag.min() == pytest.approx(0.0, abs=1e-12)
        assert pts.imag.max() == pytest.approx(height, abs=1e-12)

    def test_every_vertex_is_inside_the_boundary(self) -> None:
        """No vertex escapes the rounded contour (the superellipse equation)."""
        corner = rounded_rect_corner_params(4.0, 8.0, 0.5, False)
        assert corner is not None
        pts = _rounded_rect_boundary_polygon(4.0, 8.0, corner)
        rx, ry, n = corner.rx, corner.ry, corner.exponent
        for z in pts:
            dx = max(abs(z.real - 2.0) - (2.0 - rx), 0.0) / rx
            dy = max(abs(z.imag - 4.0) - (4.0 - ry), 0.0) / ry
            assert dx**n + dy**n <= 1.0 + 1e-9

    def test_fraction_one_proportional_is_the_exact_ellipse(self) -> None:
        """The full-round proportional limit is the type: round ellipse."""
        corner = rounded_rect_corner_params(4.0, 8.0, 1.0, True)
        assert corner is not None
        pts = _rounded_rect_boundary_polygon(4.0, 8.0, corner)
        dev = np.abs(((pts.real - 2.0) / 2.0) ** 2 + ((pts.imag - 4.0) / 4.0) ** 2 - 1.0)
        assert dev.max() < 1e-12

    def test_fraction_one_square_is_the_exact_circle(self) -> None:
        """The full-round square limit is a circle of the declared diameter."""
        corner = rounded_rect_corner_params(3.0, 3.0, 1.0, False)
        assert corner is not None
        pts = _rounded_rect_boundary_polygon(3.0, 3.0, corner)
        radii = np.abs(pts - (1.5 + 1.5j))
        assert np.max(np.abs(radii - 1.5)) < 1e-12

    def test_curvature_is_continuous_across_junctions(self) -> None:
        """The G2 contract: curvature ramps from 0, never jumps.

        The straight edges carry zero curvature; a superellipse quadrant of
        exponent n > 2 has zero curvature at its axis vertices. So the
        measured curvature must START at ~0 at each patch's ends and rise
        smoothly to its peak -- no circular-fillet 0 -> 1/r step.
        """
        width, height, fraction = 4.0, 8.0, 0.5
        corner = rounded_rect_corner_params(width, height, fraction, False)
        assert corner is not None
        pts = _rounded_rect_boundary_polygon(width, height, corner)
        ks = [
            _menger_curvature(pts[i - 1], pts[i], pts[(i + 1) % (len(pts) - 1)])
            for i in range(len(pts) - 1)
        ]
        peak = max(ks)
        assert peak > 0.0
        # The first vertex of the contour is the BL patch's Y-vertex on the
        # bottom edge (a junction): its curvature is ~0 and the next samples
        # ramp up smoothly (each step is a small fraction of the peak).
        assert ks[0] < 0.02 * peak
        steps = np.abs(np.diff(ks))
        assert steps.max() < 0.25 * peak

    def test_stadium_junctions_keep_zero_curvature(self) -> None:
        """The stadium's straight/patch junctions are curvature-free."""
        corner = rounded_rect_corner_params(8.0, 4.0, 1.0, False)
        assert corner is not None
        pts = _rounded_rect_boundary_polygon(8.0, 4.0, corner)
        ks = [
            _menger_curvature(pts[i - 1], pts[i], pts[(i + 1) % (len(pts) - 1)])
            for i in range(len(pts) - 1)
        ]
        peak = max(ks)
        assert peak > 0.0
        # Every contour vertex adjacent to a straight run (the bottom edge
        # vertices, y == 0) reads ~0 curvature.
        bottom = [k for k, z in zip(ks, pts) if abs(z.imag) < 1e-9]
        assert bottom
        assert max(bottom) < 0.02 * peak


class TestRoundedRectContentInset:
    """The corner-aware content inset."""

    def test_sharp_limit_recovers_the_rectangular_inset(self) -> None:
        """A vanishing rounding leaves the plain box inset."""
        corner = rounded_rect_corner_params(4.0, 4.0, 0.001, False)
        assert corner is not None
        width, height = rounded_rect_content_inset(4.0, 4.0, 0.25, 0.25, corner)
        assert width == pytest.approx(3.5, abs=1e-6)
        assert height == pytest.approx(3.5, abs=1e-6)

    def test_rounding_shrinks_the_content_box(self) -> None:
        """A real rounding costs content width near the corners."""
        corner = rounded_rect_corner_params(4.0, 4.0, 0.5, False)
        assert corner is not None
        width, height = rounded_rect_content_inset(4.0, 4.0, 0.25, 0.25, corner)
        assert width < 3.5
        assert height < 3.5
        # The label's aspect is kept (the ellipse-inset rule).
        assert width == pytest.approx(height)

    def test_fully_spent_radius_is_the_rect_inset(self) -> None:
        """A margin past the corner radius erodes to the plain rectangle."""
        corner = rounded_rect_corner_params(4.0, 4.0, 0.25, False)
        assert corner is not None
        width, height = rounded_rect_content_inset(4.0, 4.0, 0.6, 0.6, corner)
        assert width == pytest.approx(2.8)
        assert height == pytest.approx(2.8)

    def test_margin_past_centre_vanishes(self) -> None:
        """A margin reaching the centre leaves no content area."""
        corner = rounded_rect_corner_params(2.0, 2.0, 0.5, False)
        assert corner is not None
        assert rounded_rect_content_inset(2.0, 2.0, 1.0, 0.1, corner) == (0.0, 0.0)
        assert rounded_rect_content_inset(2.0, 2.0, 0.1, 1.5, corner) == (0.0, 0.0)

    def test_inset_is_monotone_in_fraction(self) -> None:
        """More rounding never gives MORE content room."""
        widths = []
        for fraction in (0.1, 0.3, 0.5, 0.7):
            corner = rounded_rect_corner_params(4.0, 4.0, fraction, False)
            assert corner is not None
            width, _height = rounded_rect_content_inset(4.0, 4.0, 0.25, 0.25, corner)
            widths.append(width)
        assert widths == sorted(widths, reverse=True)

    def test_wide_label_fits_the_inset_directly(self) -> None:
        """A label whose aspect-limited block clears the patches skips the bisect.

        A 10x4 label at fraction 0.25 rounds only 0.5in of the 4in sides, so
        the largest centered block of the label's aspect inside the inset
        boundary is limited by the vertical inset (v = 1.75), and its corner
        (4.375, 1.75) sits inside the inset corner patch -- the closed-form
        answer is returned directly, no bisection.
        """
        corner = rounded_rect_corner_params(10.0, 4.0, 0.25, False)
        assert corner is not None
        width, height = rounded_rect_content_inset(10.0, 4.0, 0.25, 0.25, corner)
        assert height == pytest.approx(3.5)
        assert width == pytest.approx(3.5 * 2.5)  # the label's aspect, 8.75

    def test_bisection_converges_on_the_inset_boundary(self) -> None:
        """A label whose block corners hit the patches bisects to the curve.

        The 4x4 square at fraction 0.5 puts the block's corner exactly on the
        corner patch, so the inset shrinks below the rectangular rule and the
        bisected block's corner lands on the inset superellipse.
        """
        corner = rounded_rect_corner_params(4.0, 4.0, 0.5, False)
        assert corner is not None
        width, height = rounded_rect_content_inset(4.0, 4.0, 0.25, 0.25, corner)
        # Inset boundary: half-extents 1.75, radii 0.75, exponent 4. The
        # block's corner (width/2, height/2) about the centre satisfies it.
        rx = ry = corner.rx - 0.25
        dx = max(width / 2.0 - (1.75 - rx), 0.0) / rx
        dy = max(height / 2.0 - (1.75 - ry), 0.0) / ry
        assert dx**corner.exponent + dy**corner.exponent == pytest.approx(1.0, abs=1e-6)


class TestRoundedRectCornerHole:
    """Corner drill holes ride the patch normal."""

    def test_reduces_to_the_ellipse_rule_at_n_two(self) -> None:
        """The exponent-2 normal is the shipped ellipse diagonal rule."""
        corner = RoundedRectCorner(rx=2.0, ry=1.0, exponent=2.0)
        x, y = rounded_rect_corner_hole(4.0, 2.0, corner, "top-right", 0.3)
        # Compare against the analytic ellipse point/normal at 45 degrees.
        cos45 = math.sqrt(0.5)
        bx, by = 2.0 + 2.0 * cos45, 1.0 + 1.0 * cos45
        nx, ny = cos45 / 2.0, cos45 / 1.0
        norm = math.hypot(nx, ny)
        assert x == pytest.approx(bx - nx / norm * 0.3)
        assert y == pytest.approx(by - ny / norm * 0.3)

    def test_hole_sits_inside_the_boundary(self) -> None:
        """Every corner hole centre stays inside the rounded contour."""
        corner = rounded_rect_corner_params(4.0, 2.0, 0.5, False)
        assert corner is not None
        for location in ("top-left", "top-right", "bottom-left", "bottom-right"):
            x, y = rounded_rect_corner_hole(4.0, 2.0, corner, location, 0.2)
            dx = max(abs(x - 2.0) - (2.0 - corner.rx), 0.0) / corner.rx
            dy = max(abs(y - 1.0) - (1.0 - corner.ry), 0.0) / corner.ry
            assert dx**corner.exponent + dy**corner.exponent < 1.0

    def test_hole_circles_local_uses_the_patch(self) -> None:
        """``hole_circles_local`` places corner holes off the sharp corner."""
        label = _label(
            width=4.0, height=2.0, fraction=0.5, holes=[_hole("bottom-right")]
        )
        (cx, cy, _r) = hole_circles_local(label)[0]
        # The sharp-rect inset would sit at (4 - offset, offset); the patch
        # pulls the centre toward the label's centre.
        offset = label.hole_margin + label.hole_cutter_diameter / 2.0 + 0.0625
        assert cx < 4.0 - offset
        assert cy > offset

    def test_side_holes_keep_the_rect_inset(self) -> None:
        """Side holes stay on the straight edge midpoints (exact rule)."""
        label = _label(width=4.0, height=2.0, fraction=0.5, holes=[_hole("left")])
        (cx, cy, _r) = hole_circles_local(label)[0]
        offset = label.hole_margin + label.hole_cutter_diameter / 2.0 + 0.0625
        assert cx == pytest.approx(offset)
        assert cy == pytest.approx(1.0)


class TestRoundedRectBorderBox:
    """The descender relief's bottom border."""

    def test_flat_middle_and_rising_corners(self) -> None:
        """The border ink is flat mid-edge and rises at the corners."""
        label = _label(width=4.0, height=2.0, fraction=0.5)
        box = _border_box(label)
        assert box.ink is not None
        arc = box.ink[0]
        ys = [y for _x, y in arc]
        # Bottom edge at -height/2; the ends rise by the corner radius
        # (ry = fraction * min(W, H) / 2 = 0.5 on this 4x2 label).
        assert min(ys) == pytest.approx(-1.0)
        assert max(ys) == pytest.approx(-1.0 + 0.5, abs=1e-9)
        assert arc[0][0] == pytest.approx(0.0)
        assert arc[-1][0] == pytest.approx(4.0)

    def test_sharp_label_keeps_the_flat_edge(self) -> None:
        """fraction 0 keeps the historical single-segment bottom edge."""
        box = _border_box(_label(width=4.0, height=2.0))
        assert box.ink == (((0.0, -1.0), (4.0, -1.0)),)

    def test_lower_border_spans_the_full_width(self) -> None:
        """The sampled lower boundary spans the label width, left to right."""
        corner = rounded_rect_corner_params(4.0, 2.0, 0.5, False)
        assert corner is not None
        arc = _lower_rounded_rect_border(4.0, 2.0, corner)
        assert arc[0][0] == pytest.approx(0.0)
        assert arc[-1][0] == pytest.approx(4.0)
        xs = [x for x, _y in arc]
        assert xs == sorted(xs)


class TestRendererPredicates:
    """The routing predicates pick exactly one path per label."""

    def test_sharp_label_is_unrouted(self) -> None:
        """fraction 0: no corner, no ellipse boundary (bit-identical path)."""
        label = _label()
        assert rounded_rect_corner(label) is None
        assert rounded_rect_full_ellipse(label) is False
        assert uses_ellipse_boundary(label) is False

    def test_partial_rounding_routes_to_the_patch(self) -> None:
        """0 < fraction < 1: the rounded-rect patch path."""
        label = _label(fraction=0.5)
        assert rounded_rect_corner(label) is not None
        assert rounded_rect_full_ellipse(label) is False
        assert uses_ellipse_boundary(label) is False

    def test_full_rounding_routes_to_the_ellipse(self) -> None:
        """fraction 1 proportional (or square): the shipped ellipse path."""
        prop = _label(width=4.0, height=2.0, fraction=1.0, proportional=True)
        square = _label(width=3.0, height=3.0, fraction=1.0)
        for label in (prop, square):
            assert rounded_rect_full_ellipse(label) is True
            assert uses_ellipse_boundary(label) is True
            assert rounded_rect_corner(label) is None

    def test_stadium_stays_on_the_patch_path(self) -> None:
        """The smoothed stadium is a patch label, not an ellipse."""
        label = _label(width=8.0, height=4.0, fraction=1.0)
        assert rounded_rect_full_ellipse(label) is False
        assert uses_ellipse_boundary(label) is False
        assert rounded_rect_corner(label) is not None

    def test_round_shape_is_inert(self) -> None:
        """type: round ignores the fraction (the ellipse owns the border)."""
        label = _label(fraction=0.5, shape_type="round")
        assert rounded_rect_corner(label) is None
        assert rounded_rect_full_ellipse(label) is False
        assert uses_ellipse_boundary(label) is True


class TestRenderBoundaryLocal:
    """``_render_boundary_local`` branches per label."""

    def test_sharp_stays_the_four_corner_rect(self) -> None:
        """fraction 0 emits the historical rectangle."""
        lc = _render_boundary_local(_label(width=4.0, height=2.0))
        assert len(lc) == 1
        pts = lc.lines[0]
        assert len(pts) == 5  # closed 4-corner rect

    def test_rounded_emits_the_sampled_contour(self) -> None:
        """A rounded label emits the many-vertex closed contour."""
        lc = _render_boundary_local(_label(width=4.0, height=2.0, fraction=0.5))
        assert len(lc) == 1
        pts = lc.lines[0]
        assert len(pts) > 20
        assert abs(pts[0] - pts[-1]) < 1e-12

    def test_full_round_emits_the_ellipse(self) -> None:
        """The full-round limit emits the ellipse contour."""
        rounded = _render_boundary_local(
            _label(width=4.0, height=2.0, fraction=1.0, proportional=True)
        )
        round_label = _render_boundary_local(
            _label(width=4.0, height=2.0, shape_type="round")
        )
        np.testing.assert_allclose(rounded.lines[0], round_label.lines[0])
