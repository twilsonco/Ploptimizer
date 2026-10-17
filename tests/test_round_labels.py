"""Renderer tests for circular/elliptical (``type: round``) labels.

Covers the shape-aware branches of ``plt_optimizer.generate.label_renderer``:

- the ellipse boundary emission (``_render_boundary_local``);
- the ellipse-aware drill-hole placement (``_hole_circles_local``);
- the shape-aware collision distance dispatcher (``_label_boundary_gap``);
- the radial content inset applied while positioning text.

Rectangular labels are asserted to keep the historical geometry, so the
round path is provably adjacent, never a replacement.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import numpy as np
import pytest

from plt_optimizer.core.units import units_to_inches
from plt_optimizer.generate.label_renderer import (
    _border_box,
    _detect_text_hole_collisions,
    _hole_circles_local,
    _label_ellipse,
    _label_is_round,
    _LineEntry,
    _lower_ellipse_arc,
    _render_boundary_local,
    _round_corner_hole,
)
from plt_optimizer.generate.resolution import (
    ResolvedHoleSpec,
    ResolvedLabel,
    ResolvedTextLine,
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
    shape_type: str = "rect",
    width: float = 4.0,
    height: float = 2.0,
    margin: float = 0.1,
    hole_margin: float = 0.15,
    holes: Optional[list] = None,
    content: Optional[list[ResolvedTextLine]] = None,
) -> ResolvedLabel:
    """Build a minimal ``ResolvedLabel`` with a chosen boundary shape."""
    return ResolvedLabel(
        id="round_label",
        count=1,
        width=width,
        height=height,
        margin=margin,
        h_margin=margin,
        v_margin=margin,
        hole_margin=hole_margin,
        holes=holes or [],
        content=content if content is not None else [_make_line("HELLO")],
        shape_type=shape_type,
    )


def _hole(location: str, diameter: float = 0.125) -> ResolvedHoleSpec:
    """Build a resolved drill-hole spec at the given location."""
    return ResolvedHoleSpec(diameter=diameter, location=location)


class TestLabelIsRound:
    """The single shape predicate."""

    def test_rect_is_not_round(self) -> None:
        assert _label_is_round(_label("rect")) is False

    def test_round_is_round(self) -> None:
        assert _label_is_round(_label("round")) is True


class TestLabelEllipseGeometry:
    """The ellipse boundary in the label-local y-up frame."""

    def test_center_and_semi_axes(self) -> None:
        assert _label_ellipse(_label("round", width=4.0, height=2.0)) == (2.0, 1.0, 2.0, 1.0)

    def test_circle_has_equal_semi_axes(self) -> None:
        _cx, _cy, semi_a, semi_b = _label_ellipse(_label("round", width=3.0, height=3.0))
        assert semi_a == semi_b == 1.5


class TestEllipseBoundaryEmission:
    """``_render_boundary_local`` emits an ellipse for round labels."""

    def test_rect_emits_four_corners(self) -> None:
        """The rectangle path is unchanged: a 4-vertex closed contour."""
        lc = _render_boundary_local(_label("rect", width=4.0, height=2.0))
        assert len(lc) == 1
        contour = lc[0]
        # vp.rect emits 5 points (closed rectangle): 4 unique corners.
        xs = {round(p.real, 6) for p in contour}
        ys = {round(p.imag, 6) for p in contour}
        assert xs == {0.0, 4.0}
        assert ys == {0.0, 2.0}

    def test_round_emits_a_closed_ellipse(self) -> None:
        """The ellipse contour is closed, dense, and spans the label box."""
        lc = _render_boundary_local(_label("round", width=4.0, height=2.0))
        assert len(lc) == 1
        contour = np.asarray(lc[0])
        # A sampled ellipse has many vertices (not the rectangle's 5).
        assert len(contour) > 32
        assert np.allclose(contour[0], contour[-1])  # closed
        # Spans the enclosing box (extrema on the axes).
        assert contour.real.min() == pytest.approx(0.0, abs=1e-6)
        assert contour.real.max() == pytest.approx(4.0, abs=1e-6)
        assert contour.imag.min() == pytest.approx(0.0, abs=1e-6)
        assert contour.imag.max() == pytest.approx(2.0, abs=1e-6)

    def test_round_vertices_lie_on_the_ellipse(self) -> None:
        """Every emitted vertex satisfies the ellipse equation."""
        lc = _render_boundary_local(_label("round", width=4.0, height=2.0))
        contour = np.asarray(lc[0])
        cx, cy, semi_a, semi_b = 2.0, 1.0, 2.0, 1.0
        value = ((contour.real - cx) / semi_a) ** 2 + ((contour.imag - cy) / semi_b) ** 2
        assert np.allclose(value, 1.0, atol=1e-3)

    def test_degenerate_round_label_is_empty(self) -> None:
        """A non-positive ellipse size emits nothing (rect-shape guard parity)."""
        assert _render_boundary_local(_label("round", width=0.0, height=2.0)).is_empty()
        assert _render_boundary_local(_label("round", width=4.0, height=0.0)).is_empty()


class TestRoundHolePlacement:
    """Ellipse-aware drill-hole placement."""

    def test_sides_holes_land_on_the_axis_vertices(self) -> None:
        """On a round label the `sides` holes sit on the horizontal axis."""
        label = _label(
            "round",
            width=4.0,
            height=2.0,
            holes=[_hole("left"), _hole("right")],
        )
        circles = _hole_circles_local(label)
        # The horizontal axis keeps the rectangular edge-inset (exact there).
        # The centre rides the SPECIFIED radius (0.0625); the emitted radius
        # is the cutter-compensated one (hole_adjust_for_cutter defaults on).
        offset = label.hole_margin + label.hole_cutter_diameter / 2.0 + 0.0625
        assert circles[0][0] == pytest.approx(offset)
        assert circles[1][0] == pytest.approx(4.0 - offset)
        for _x, y, _r in circles:
            assert y == pytest.approx(1.0)  # on the horizontal midline

    def test_corner_holes_sit_on_the_diagonal_inside_the_ellipse(self) -> None:
        """A round corner hole is inset along the diagonal, inside the curve."""
        label = _label(
            "round",
            width=4.0,
            height=2.0,
            holes=[_hole("top-right")],
        )
        (cx, cy, radius) = _hole_circles_local(label)[0]
        # Toward the top-right: x past centre, y past centre.
        assert cx > 2.0
        assert cy > 1.0
        # Inside the ellipse (the centre is strictly interior to the curve).
        assert ((cx - 2.0) / 2.0) ** 2 + ((cy - 1.0) / 1.0) ** 2 < 1.0

    def test_rect_corner_holes_keep_the_box_inset(self) -> None:
        """The rectangular corner placement is untouched."""
        label = _label(
            "rect",
            width=4.0,
            height=2.0,
            holes=[_hole("top-right")],
        )
        (cx, cy, radius) = _hole_circles_local(label)[0]
        # The centre rides the SPECIFIED radius; `radius` is the emitted
        # (cutter-compensated) one.
        offset = label.hole_margin + label.hole_cutter_diameter / 2.0 + 0.0625
        assert cx == pytest.approx(4.0 - offset)
        assert cy == pytest.approx(2.0 - offset)

    def test_round_corner_hole_helper_is_symmetric(self) -> None:
        """The four corner holes are symmetric about the centre."""
        label = _label("round", width=4.0, height=2.0)
        offset = 0.3
        tr = _round_corner_hole(label, "top-right", offset)
        tl = _round_corner_hole(label, "top-left", offset)
        assert tl[0] == pytest.approx(4.0 - tr[0])
        assert tl[1] == pytest.approx(tr[1])


class TestRoundCollisionDetection:
    """Collision detection runs the ellipse distance through the phases."""

    def test_hole_in_the_cut_away_corner_is_not_a_text_collision(self) -> None:
        """A hole in the material the ellipse cuts away is outside the boundary.

        Detection measures the text box against the ellipse boundary; a
        drill hole placed in the (rectangular) corner the ellipse removes
        measures a positive gap to the ellipse, so it is not a collision
        with text sitting inside the ellipse.
        """
        label = _label(
            "round",
            width=6.0,
            height=4.0,
            holes=[_hole("top-right")],
        )
        # A text line box well inside the ellipse.
        entry = _LineEntry(line_index=0, line_text="HELLO", bounds=(2.5, 1.5, 3.5, 2.5))
        collisions = _detect_text_hole_collisions(label, [entry])
        assert collisions == []


class TestShapeAwareBorderBox:
    """The descender relief measures against the ellipse's lower arc."""

    def test_rect_border_is_a_flat_edge(self) -> None:
        """The rectangular border box is unchanged: one flat polyline."""
        box = _border_box(_label("rect", width=4.0, height=2.0))
        assert len(box.ink) == 1
        assert len(box.ink[0]) == 2
        assert all(y == pytest.approx(-1.0) for _x, y in box.ink[0])

    def test_round_border_is_the_lower_arc(self) -> None:
        """The round border box carries the ellipse's lower arc."""
        box = _border_box(_label("round", width=4.0, height=2.0))
        arc = box.ink[0]
        assert len(arc) > 2
        # Bottom vertex at -height/2, side vertices rise to y = 0.
        assert min(y for _x, y in arc) == pytest.approx(-1.0)
        assert arc[0][1] == pytest.approx(0.0, abs=1e-9)
        assert arc[-1][1] == pytest.approx(0.0, abs=1e-9)
        # Every vertex satisfies the ellipse equation about (2, 0).
        for x, y in arc:
            assert ((x - 2.0) / 2.0) ** 2 + (y / 1.0) ** 2 == pytest.approx(1.0, abs=1e-3)

    def test_lower_arc_rises_toward_the_edges(self) -> None:
        """The boundary is deepest at the centre, sharpest at the shoulders."""
        arc = _lower_ellipse_arc(4.0, 2.0)
        mid = len(arc) // 2
        assert arc[mid][1] < arc[0][1]
        assert arc[mid][1] < arc[-1][1]


class TestRadialContentInsetApplied:
    """Round labels position text inside the inscribed (radial) content box."""

    def test_round_content_is_narrower_than_rect(self) -> None:
        """The same text on a round label compresses sooner (smaller box)."""
        from plt_optimizer.generate.label_renderer import _render_positioned_lines

        # A wide line + a compression budget: the round label's narrower
        # inscribed box forces a smaller effective scale than the rect box.
        content = [_make_line("WIDE TEXT THAT IS LONG", height=0.6)]
        from dataclasses import replace

        base = _label(
            "rect", width=4.0, height=2.0, margin=0.1, content=content
        )
        base = replace(
            base,
            content=[replace(base.content[0], max_h_compress=0.9)],
        )
        round_label = replace(base, shape_type="round")

        def scale_of(label: ResolvedLabel) -> float:
            positioned = _render_positioned_lines(label)
            return positioned[0][2].compression_scale

        # The rect label fits its wider box; the round label must compress.
        assert scale_of(base) >= scale_of(round_label)
        assert scale_of(round_label) < 1.0


class TestCutterCompensatedHoles:
    """``hole_adjust_for_cutter`` shrinks the emitted radius, never the centre.

    The centre is placed from the SPECIFIED diameter, so every placement rule
    (rect edges, round diagonal, rounded-rect patch) is bit-identical with the
    mechanism off; only the drawn circle shrinks by half the cutter diameter.
    """

    _CUTTER = 0.015

    @staticmethod
    def _label_with_holes(
        shape_type: str = "rect",
        *,
        adjust: bool = True,
        diameter: float = 0.125,
        locations: tuple[str, ...] = ("bottom-left",),
        fraction: float = 0.0,
    ) -> ResolvedLabel:
        """Build a resolved label carrying holes and the compensation flag."""
        from dataclasses import replace

        label = _label(
            shape_type,
            width=4.0,
            height=2.0,
            holes=[_hole(location, diameter) for location in locations],
        )
        return replace(
            label,
            hole_adjust_for_cutter=adjust,
            hole_cutter_diameter=TestCutterCompensatedHoles._CUTTER,
            rounded_rectangle_fraction=fraction,
        )

    def test_centre_is_untouched_radius_shrinks(self) -> None:
        """The centre matches the specified radius; the radius loses cutter/2."""
        on = self._label_with_holes(adjust=True)
        off = self._label_with_holes(adjust=False)
        (cx_on, cy_on, r_on) = _hole_circles_local(on)[0]
        (cx_off, cy_off, r_off) = _hole_circles_local(off)[0]

        assert (cx_on, cy_on) == pytest.approx((cx_off, cy_off))
        assert r_off == pytest.approx(0.0625)
        assert r_on == pytest.approx(0.0625 - self._CUTTER / 2.0)

    def test_rect_edge_placements_all_keep_their_centres(self) -> None:
        """Every atomic location keeps its centre under compensation."""
        locations = (
            "top-left",
            "top-right",
            "bottom-left",
            "bottom-right",
            "left",
            "right",
            "top",
            "bottom",
        )
        on = _hole_circles_local(self._label_with_holes(adjust=True, locations=locations))
        off = _hole_circles_local(self._label_with_holes(adjust=False, locations=locations))
        assert len(on) == len(off) == len(locations)
        for (x_on, y_on, r_on), (x_off, y_off, r_off) in zip(on, off):
            assert (x_on, y_on) == pytest.approx((x_off, y_off))
            assert r_on < r_off

    def test_round_corner_hole_centre_is_untouched(self) -> None:
        """The ellipse-diagonal placement is centre-invariant too."""
        on = _hole_circles_local(
            self._label_with_holes("round", adjust=True, locations=("top-right",))
        )[0]
        off = _hole_circles_local(
            self._label_with_holes("round", adjust=False, locations=("top-right",))
        )[0]
        assert on[:2] == pytest.approx(off[:2])
        assert on[2] == pytest.approx(off[2] - self._CUTTER / 2.0)

    def test_rounded_rect_corner_hole_centre_is_untouched(self) -> None:
        """The superellipse-patch placement is centre-invariant too."""
        on = _hole_circles_local(
            self._label_with_holes(
                adjust=True, locations=("bottom-right",), fraction=0.5
            )
        )[0]
        off = _hole_circles_local(
            self._label_with_holes(
                adjust=False, locations=("bottom-right",), fraction=0.5
            )
        )[0]
        assert on[:2] == pytest.approx(off[:2])
        assert on[2] == pytest.approx(off[2] - self._CUTTER / 2.0)

    def test_degenerate_hole_clamps_and_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """A hole at/below the cutter clamps to one plotter unit with a WARNING."""
        from plt_optimizer.core.units import HPGL_UNITS_PER_INCH

        label = self._label_with_holes(adjust=True, diameter=0.01)
        with caplog.at_level(logging.WARNING):
            (_cx, _cy, radius) = _hole_circles_local(label)[0]
        assert radius == pytest.approx(1.0 / HPGL_UNITS_PER_INCH)
        assert "cannot be cutter-compensated" in caplog.text

    def test_no_warning_when_the_compensation_is_healthy(self) -> None:
        """A normal hole emits no warning."""
        _hole_circles_local(self._label_with_holes(adjust=True, diameter=0.125))

    def test_opt_out_emits_no_warning_for_a_degenerate_hole(self) -> None:
        """With the mechanism off there is nothing to clamp and nothing to log."""
        _hole_circles_local(self._label_with_holes(adjust=False, diameter=0.01))

    def test_collision_check_measures_the_engraved_circle(self) -> None:
        """The collision sweep sees the smaller circle, so near misses relax.

        The left hole sits at x = 0.15 + 0.0075 + 0.0625 = 0.22; the text box
        starts at x = 0.45 (its y spans the hole centre after the +height/2
        frame shift), so the geometric gap is 0.23 - r. The threshold is
        0.5 * (0.015 + 0.03) + 0.15 = 0.1725: the specified circle (r =
        0.0625, gap 0.1675) collides, the engraved circle (r = 0.055, gap
        0.175) does not.
        """
        entries = [_LineEntry(0, "X", (0.45, -0.1, 4.0, 0.1), 1.0, None, ())]
        tight = self._label_with_holes(adjust=True, diameter=0.125, locations=("left",))
        loose = self._label_with_holes(adjust=False, diameter=0.125, locations=("left",))
        assert _detect_text_hole_collisions(tight, entries) == []
        assert len(_detect_text_hole_collisions(loose, entries)) == 1
