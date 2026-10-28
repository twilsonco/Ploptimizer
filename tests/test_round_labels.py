"""Renderer tests for circular/elliptical (``type: round``) labels.

Covers the shape-aware branches of the renderer and the collision package
(``plt_optimizer.generate.label_renderer`` /
``plt_optimizer.generate.collision``):

- the ellipse boundary emission (``_render_boundary_local``);
- the ellipse-aware drill-hole placement (``hole_circles_local``);
- the shape-aware collision distance dispatcher (``_label_boundary_gap``);
- user margins trusted while positioning text (no radial inset; regression pin).

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
from plt_optimizer.generate.collision.detection import detect_text_hole_collisions
from plt_optimizer.generate.collision.holes import hole_circles_local, round_corner_hole
from plt_optimizer.generate.collision.shapes import label_ellipse, label_is_round
from plt_optimizer.generate.label_renderer import (
    _border_box,
    _LineEntry,
    _lower_ellipse_arc,
    _render_boundary_local,
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
        assert label_is_round(_label("rect")) is False

    def test_round_is_round(self) -> None:
        assert label_is_round(_label("round")) is True


class TestLabelEllipseGeometry:
    """The ellipse boundary in the label-local y-up frame."""

    def test_center_and_semi_axes(self) -> None:
        assert label_ellipse(_label("round", width=4.0, height=2.0)) == (2.0, 1.0, 2.0, 1.0)

    def test_circle_has_equal_semi_axes(self) -> None:
        _cx, _cy, semi_a, semi_b = label_ellipse(_label("round", width=3.0, height=3.0))
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
        circles = hole_circles_local(label)
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
        (cx, cy, radius) = hole_circles_local(label)[0]
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
        (cx, cy, radius) = hole_circles_local(label)[0]
        # The centre rides the SPECIFIED radius; `radius` is the emitted
        # (cutter-compensated) one.
        offset = label.hole_margin + label.hole_cutter_diameter / 2.0 + 0.0625
        assert cx == pytest.approx(4.0 - offset)
        assert cy == pytest.approx(2.0 - offset)

    def test_round_corner_hole_helper_is_symmetric(self) -> None:
        """The four corner holes are symmetric about the centre."""
        label = _label("round", width=4.0, height=2.0)
        offset = 0.3
        tr = round_corner_hole(label, "top-right", offset)
        tl = round_corner_hole(label, "top-left", offset)
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
        collisions = detect_text_hole_collisions(label, [entry])
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


class TestUserMarginsTrustedOnRoundLabels:
    """Round labels position text with the USER margins, never a radial inset.

    Regression pin for commit 4757694: recomputing the local margins from
    ``ellipse_content_inset`` applied the margin twice (the box inset AND the
    inscribed-block rule), compressing text that already fits the specified
    margin. That recomputation must never return -- a round label measures
    text against ``width - 2 h_margin`` by ``height - 2 v_margin``, exactly
    like its rectangular twin. (The rule also covers the fully-rounded
    rounded-rectangle limit, which shares the ellipse boundary path.)
    """

    def test_round_content_is_no_narrower_than_rect(self) -> None:
        """The same text compresses identically on round and rect labels."""
        from dataclasses import replace

        from plt_optimizer.generate.label_renderer import _render_positioned_lines

        # A wide line: both labels compress, and the round label must
        # measure the SAME box as the rect twin -- no inscribed-rect inset.
        content = [_make_line("WIDE TEXT THAT IS LONG", height=0.6)]
        base = _label("rect", width=4.0, height=2.0, margin=0.1, content=content)
        base = replace(
            base,
            content=[replace(base.content[0], max_h_compress=0.9)],
        )
        round_label = replace(base, shape_type="round")

        def scale_of(label: ResolvedLabel) -> float:
            positioned = _render_positioned_lines(label)
            return positioned[0][2].compression_scale

        assert scale_of(base) < 1.0  # the line really does compress
        assert scale_of(round_label) == pytest.approx(scale_of(base))

    def test_full_ellipse_rounded_rect_uses_user_margins(self) -> None:
        """The f=1 rounded-rect limit shares the margin rule, not an inset."""
        from dataclasses import replace

        from plt_optimizer.generate.label_renderer import _render_positioned_lines

        content = [_make_line("WIDE TEXT THAT IS LONG", height=0.6)]
        base = _label("rect", width=4.0, height=2.0, margin=0.1, content=content)
        base = replace(
            base,
            content=[replace(base.content[0], max_h_compress=0.9)],
        )
        full_ellipse = replace(
            base,
            rounded_rectangle_fraction=1.0,
            rounded_rectangle_proportional=True,
        )

        def scale_of(label: ResolvedLabel) -> float:
            positioned = _render_positioned_lines(label)
            return positioned[0][2].compression_scale

        assert scale_of(base) < 1.0
        assert scale_of(full_ellipse) == pytest.approx(scale_of(base))


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
        (cx_on, cy_on, r_on) = hole_circles_local(on)[0]
        (cx_off, cy_off, r_off) = hole_circles_local(off)[0]

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
        on = hole_circles_local(self._label_with_holes(adjust=True, locations=locations))
        off = hole_circles_local(self._label_with_holes(adjust=False, locations=locations))
        assert len(on) == len(off) == len(locations)
        for (x_on, y_on, r_on), (x_off, y_off, r_off) in zip(on, off):
            assert (x_on, y_on) == pytest.approx((x_off, y_off))
            assert r_on < r_off

    def test_round_corner_hole_centre_is_untouched(self) -> None:
        """The ellipse-diagonal placement is centre-invariant too."""
        on = hole_circles_local(
            self._label_with_holes("round", adjust=True, locations=("top-right",))
        )[0]
        off = hole_circles_local(
            self._label_with_holes("round", adjust=False, locations=("top-right",))
        )[0]
        assert on[:2] == pytest.approx(off[:2])
        assert on[2] == pytest.approx(off[2] - self._CUTTER / 2.0)

    def test_rounded_rect_corner_hole_centre_is_untouched(self) -> None:
        """The superellipse-patch placement is centre-invariant too."""
        on = hole_circles_local(
            self._label_with_holes(adjust=True, locations=("bottom-right",), fraction=0.5)
        )[0]
        off = hole_circles_local(
            self._label_with_holes(adjust=False, locations=("bottom-right",), fraction=0.5)
        )[0]
        assert on[:2] == pytest.approx(off[:2])
        assert on[2] == pytest.approx(off[2] - self._CUTTER / 2.0)

    def test_degenerate_hole_clamps_and_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """A hole at/below the cutter clamps to one plotter unit with a WARNING."""
        from plt_optimizer.core.units import HPGL_UNITS_PER_INCH

        label = self._label_with_holes(adjust=True, diameter=0.01)
        with caplog.at_level(logging.WARNING):
            (_cx, _cy, radius) = hole_circles_local(label)[0]
        assert radius == pytest.approx(1.0 / HPGL_UNITS_PER_INCH)
        assert "cannot be cutter-compensated" in caplog.text

    def test_no_warning_when_the_compensation_is_healthy(self) -> None:
        """A normal hole emits no warning."""
        hole_circles_local(self._label_with_holes(adjust=True, diameter=0.125))

    def test_opt_out_emits_no_warning_for_a_degenerate_hole(self) -> None:
        """With the mechanism off there is nothing to clamp and nothing to log."""
        hole_circles_local(self._label_with_holes(adjust=False, diameter=0.01))

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
        assert detect_text_hole_collisions(tight, entries) == []
        assert len(detect_text_hole_collisions(loose, entries)) == 1


def _parse_arcs(content: str):
    """Parse emitted HPGL into core ArcSegments (device units)."""
    from plt_optimizer.core.models import ArcSegment
    from plt_optimizer.core.parser import PLTParser

    doc = PLTParser().parse_string(f"IN;PA;SP2;{content};SP;")
    return [
        segment
        for path in doc.stroke_paths
        for segment in path.segments
        if isinstance(segment, ArcSegment)
    ]


class TestEllipseBorderArcNative:
    """``_render_boundary_hpgl`` engraves the ellipse with ``AA`` arc chains."""

    def test_ellipse_emits_arc_chain(self) -> None:
        """A 4x2 ellipse border: 40 arcs (10 per quadrant), PU-led per quadrant."""
        from plt_optimizer.generate.label_renderer import _render_boundary_hpgl

        content = _render_boundary_hpgl(_label("round", width=4.0, height=2.0))
        assert content.count("AA") == 40
        # One PU-led stroke per quadrant, each starting on an axis vertex.
        assert content.count("PU") == 4

    def test_circle_emits_four_exact_quarter_arcs(self) -> None:
        """A circle needs no refit: four 90-degree arcs sharing one centre."""
        from plt_optimizer.generate.label_renderer import _render_boundary_hpgl

        content = _render_boundary_hpgl(_label("round", width=3.0, height=3.0))
        assert content.count("AA") == 4
        arcs = _parse_arcs(content)
        assert len(arcs) == 4
        centres = {(round(a.center.x), round(a.center.y)) for a in arcs}
        assert len(centres) == 1  # one circle centre
        for arc in arcs:
            assert abs(abs(arc.sweep_angle)) == pytest.approx(90.0, abs=1e-6)
            assert arc.radius == pytest.approx(1.5 * 1016.0, abs=2.0)

    def test_arc_chain_stays_on_the_ellipse(self) -> None:
        """Every fitted arc's swept points stay within the emission floor of the curve.

        The deviation guarantee is 0.0002in against the TRUE ellipse; the
        emitted (integer-unit) arcs quantize to ~2 plotter units, so the
        check runs at 3e-3in -- the same floor the rendered-box pins use.
        """
        from plt_optimizer.generate.label_renderer import _render_boundary_hpgl

        content = _render_boundary_hpgl(_label("round", width=4.0, height=2.0))
        arcs = _parse_arcs(content)
        assert arcs
        cx, cy, semi_a, semi_b = 2.0 * 1016.0, 1.0 * 1016.0, 2.0 * 1016.0, 1.0 * 1016.0
        tol = 3e-3 * 1016.0
        worst = 0.0
        for arc in arcs:
            r = arc.radius
            a0 = math.atan2(arc.start.y - arc.center.y, arc.start.x - arc.center.x)
            sweep = math.radians(arc.sweep_angle)
            for i in range(33):
                a = a0 + sweep * i / 32.0
                x = arc.center.x + r * math.cos(a)
                y = arc.center.y + r * math.sin(a)
                # Distance from the ellipse, approximated by the implicit
                # function scaled by the local gradient (enough at this scale).
                ex, ey = (x - cx) / semi_a, (y - cy) / semi_b
                value = ex * ex + ey * ey
                grad = math.hypot(2 * ex / semi_a, 2 * ey / semi_b)
                dist = abs(value - 1.0) / grad if grad > 0.0 else math.inf
                worst = max(worst, dist)
        assert worst <= tol

    def test_border_is_one_closed_contour_per_quadrant(self) -> None:
        """Each quadrant chains tip-to-tail and the quadrants meet on the axes."""
        from plt_optimizer.generate.label_renderer import _render_boundary_hpgl

        content = _render_boundary_hpgl(_label("round", width=4.0, height=2.0))
        arcs = _parse_arcs(content)
        # Consecutive arcs chain (end == next start) within the quantization.
        for a, b in zip(arcs[:-1], arcs[1:]):
            if abs(b.start.x - a.end.x) > 2.0 and abs(b.start.y - a.end.y) > 2.0:
                # Quadrant boundary: the next PU restarts on an axis vertex.
                assert round(b.start.x) in (0, 2032, 4064)
                assert round(b.start.y) in (0, 1016, 2032)

    def test_bounds_reach_the_nominal_box(self) -> None:
        """The PU-led axis vertices keep the contour's bounds on the box."""
        from plt_optimizer.generate.label_renderer import (
            _render_boundary_hpgl,
            extract_bounds_from_plt,
        )

        content = _render_boundary_hpgl(_label("round", width=4.0, height=2.0))
        x_min, y_min, x_max, y_max = extract_bounds_from_plt(content)
        # The arc radius quantization floor: ~2 units (3e-3in) around nominal.
        assert x_min == pytest.approx(0.0, abs=3e-3)
        assert y_min == pytest.approx(0.0, abs=3e-3)
        assert x_max == pytest.approx(4.0, abs=3e-3)
        assert y_max == pytest.approx(2.0, abs=3e-3)

    def test_emission_is_deterministic(self) -> None:
        """Identical labels emit byte-identical borders (the product invariant)."""
        from plt_optimizer.generate.label_renderer import _render_boundary_hpgl

        label = _label("round", width=4.0, height=2.0)
        assert _render_boundary_hpgl(label) == _render_boundary_hpgl(label)

    def test_extreme_aspect_falls_back_to_the_polygon(self, caplog) -> None:
        """A border the chain cap cannot resolve polygonizes and logs a warning."""
        import logging

        from plt_optimizer.generate.label_renderer import _render_boundary_hpgl

        with caplog.at_level(logging.WARNING, logger="plt_optimizer.generate.label_renderer"):
            content = _render_boundary_hpgl(_label("round", width=20.0, height=1.0))
        # The polygon fallback: a PU/PD contour with no AA commands.
        assert "AA" not in content
        assert content.startswith("PU")
        assert "declined the arc refit" in caplog.text

    def test_degenerate_label_emits_nothing(self) -> None:
        """The degenerate-size guard precedes the arc path (parity with polygon)."""
        from plt_optimizer.generate.label_renderer import _render_boundary_hpgl

        assert _render_boundary_hpgl(_label("round", width=0.0, height=2.0)) == ""
        assert _render_boundary_hpgl(_label("round", width=4.0, height=0.0)) == ""


class TestRoundLabelToolpathArcNative:
    """End-to-end: a round label's ENGRAVING toolpath uses arcs everywhere.

    The TODO contract: polygonized boundaries are fine for margins/relief
    (the analytic sampled paths), but the emitted toolpath -- border, text,
    drill holes -- must be arc-native. A PLT-font (Dino) round label with
    drill holes verifies all three layers of one ``render_label_to_plt``.
    """

    def _round_label(self, text: str, holes: Optional[list] = None):
        """A round Dino label (PLT-extracted font: its glyphs are AA-native)."""
        from plt_optimizer.generate.resolution import ResolvedTextLine

        line = ResolvedTextLine(
            text=text,
            nominal_text_height=0.5,
            toolpath_text_height=0.5 - 0.06,
            cutter_diameter=0.06,
            character_spacing=0.0,
            line_spacing=0.0,
            font="Dino",
        )
        return _label("round", width=4.0, height=2.0, holes=holes, content=[line])

    def test_every_layer_carries_arcs(self) -> None:
        """Border (SP2) + holes (SP3) + PLT-font text (SP1) all emit AA."""
        import re

        from plt_optimizer.generate.label_renderer import render_label_to_plt

        label = self._round_label("OOCC88", [_hole("left"), _hole("right")])
        content = render_label_to_plt(label).plt_content
        parts = re.split(r"(SP\d+;)", content)
        sections = {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}
        # SP1: the Dino glyphs are natively PU/PD/AA (no transform needed).
        assert sections.get("SP1;", "").count("AA") > 0
        # SP2: the ellipse border is the 40-arc quadrant chain.
        assert sections.get("SP2;", "").count("AA") == 40
        # SP3: two drill holes, four quarter arcs each.
        assert sections.get("SP3;", "").count("AA") == 8

    def test_round_label_bounds_stay_on_the_nominal_box(self) -> None:
        """The arc-native border keeps the rendered box on the label box."""
        from plt_optimizer.generate.label_renderer import render_label_to_plt

        rendered = render_label_to_plt(self._round_label("ELLIPSE"))
        assert rendered.width == pytest.approx(4.0, abs=3e-3)
        assert rendered.height == pytest.approx(2.0, abs=3e-3)


class TestKerfBufferedHoles:
    """``cutter_buffer_routing`` widens the hole tool into its engraved width.

    The compensation and the edge offset are width-of-material-removed
    questions, so both consume
    :attr:`ResolvedLabel.effective_hole_cutter_diameter` (the physical tool
    plus the buffer). A 0.0 buffer is the identity, which is why every
    existing hole test above is unchanged.
    """

    _CUTTER = 0.015
    _BUFFER = 0.045

    @classmethod
    def _label(cls, *, buffer: float, adjust: bool = True, diameter: float = 0.25):
        """Build a rect label with one left hole and the given routing buffer."""
        from dataclasses import replace

        label = _label(
            "rect",
            width=4.0,
            height=2.0,
            holes=[_hole("left", diameter)],
        )
        return replace(
            label,
            hole_adjust_for_cutter=adjust,
            hole_cutter_diameter=cls._CUTTER,
            hole_cutter_buffer=buffer,
        )

    def test_effective_property_adds_the_buffer(self) -> None:
        label = self._label(buffer=self._BUFFER)
        assert label.hole_cutter_diameter == pytest.approx(self._CUTTER)
        assert label.effective_hole_cutter_diameter == pytest.approx(self._CUTTER + self._BUFFER)

    def test_zero_buffer_is_the_identity(self) -> None:
        label = self._label(buffer=0.0)
        assert label.effective_hole_cutter_diameter == pytest.approx(label.hole_cutter_diameter)

    def test_emitted_radius_shrinks_by_the_engraved_width(self) -> None:
        """The engraved hole keeps the specified diameter under a wider kerf."""
        (_cx, _cy, radius) = hole_circles_local(self._label(buffer=self._BUFFER))[0]
        # Specified 0.25 cut by a 0.015 tool engraving 0.06: the path circle
        # loses the full engraved width (half on each side).
        assert radius == pytest.approx((0.25 - (self._CUTTER + self._BUFFER)) / 2.0)

    def test_centre_offset_grows_by_half_the_buffer(self) -> None:
        """hole_margin + engraved/2 + specified/2 keeps the stroke edge at margin."""
        (_x0, y0, _r0) = hole_circles_local(self._label(buffer=0.0))[0]
        (_x1, y1, _r1) = hole_circles_local(self._label(buffer=self._BUFFER))[0]
        # A left hole centres vertically: the offset carries the engraved/2 term.
        assert y0 == pytest.approx(1.0)
        assert y1 == pytest.approx(1.0)
        # The X offset is where the cutter term lives.
        assert _x1 - _x0 == pytest.approx(self._BUFFER / 2.0)

    def test_centres_stay_specified_radius_driven(self) -> None:
        """The centre never uses the engraved width for the hole's own radius."""
        label = self._label(buffer=self._BUFFER)
        (cx, _cy, _r) = hole_circles_local(label)[0]
        expected = label.hole_margin + (label.effective_hole_cutter_diameter / 2.0) + 0.125
        assert cx == pytest.approx(expected)

    def test_compensation_off_emits_the_specified_radius(self) -> None:
        """hole_adjust_for_cutter: false ignores the buffer for the radius."""
        (_cx, _cy, radius) = hole_circles_local(self._label(buffer=self._BUFFER, adjust=False))[0]
        assert radius == pytest.approx(0.125)

    def test_tiny_hole_clamps_and_warns_naming_the_buffer(self, caplog) -> None:
        """A hole below the ENGRAVED width clamps, and the warning says why."""
        import logging

        with caplog.at_level(logging.WARNING, logger="plt_optimizer.generate.collision.holes"):
            (_cx, _cy, radius) = hole_circles_local(
                self._label(buffer=self._BUFFER, diameter=0.05)
            )[0]
        assert radius == pytest.approx(1.0 / 1016.0)
        assert "kerf buffer" in caplog.text
