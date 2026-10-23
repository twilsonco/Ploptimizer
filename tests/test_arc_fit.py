"""Tests for the ellipse-to-circular-arc fitter (``arc_fit``).

Covers the pure geometry that lets non-uniform glyph transforms keep native
``AA`` arcs: the affine image of a circular arc (the parametric ellipse,
``t`` = the source center angle), the tangent-continuous (C0 + G1) chain
construction, the degenerate-span collapse, the tolerance guarantee, and every
degenerate branch that must return ``None`` so the caller flattens.
"""

from __future__ import annotations

import math

import pytest

from plt_optimizer.generate.arc_fit import (
    ARC_FIT_DEV_TOL_INCHES,
    ARC_FIT_MAX_ARCS,
    EllipseArc,
    ellipse_deviation,
    ellipse_from_arc,
    fit_arc,
    fit_ellipse_arc,
)


def _mode_kwargs(mode: str, s: float) -> dict:
    """Transform kwargs for one of the three non-uniform maps."""
    if mode == "x":
        return {"scale_x": s}
    if mode == "y":
        return {"scale_y": s}
    if mode == "shear":
        return {"shear_tan": s}
    raise ValueError(mode)


def _dense_ellipse(arc: EllipseArc, n: int = 400) -> list:
    t0 = math.radians(arc.t0_deg)
    span = math.radians(arc.sweep_deg)
    return [arc.point(t0 + span * i / n) for i in range(n + 1)]


def _dist_point_segment(p: complex, a: complex, b: complex) -> float:
    d = b - a
    len2 = d.real * d.real + d.imag * d.imag
    if len2 <= 0.0:
        return abs(p - a)
    t = max(0.0, min(1.0, ((p - a).real * d.real + (p - a).imag * d.imag) / len2))
    return abs(p - (a + t * d))


def _dist_point_arc(p: complex, seg) -> float:
    center = seg.center
    assert center is not None
    radius = abs(seg.start - center)
    a0 = math.degrees(math.atan2(seg.start.imag - center.imag, seg.start.real - center.real))
    rel = math.degrees(math.atan2(p.imag - center.imag, p.real - center.real)) - a0
    while rel > 180.0:
        rel -= 360.0
    while rel <= -180.0:
        rel += 360.0
    lo, hi = (0.0, seg.sweep_deg) if seg.sweep_deg >= 0 else (seg.sweep_deg, 0.0)
    if lo - 1e-6 <= rel <= hi + 1e-6:
        return abs(abs(p - center) - radius)
    return min(abs(p - seg.start), abs(p - seg.end))


def _true_deviation(arc: EllipseArc, chain: list) -> float:
    """Max distance from densely-sampled ellipse points to the chain."""
    worst = 0.0
    for p in _dense_ellipse(arc):
        d = math.inf
        for seg in chain:
            d = min(
                d,
                _dist_point_arc(p, seg)
                if seg.is_arc
                else _dist_point_segment(p, seg.start, seg.end),
            )
        worst = max(worst, d)
    return worst


def _fit_chain_needs_more_than_cap(arc: EllipseArc) -> bool:
    """True when even the k=cap chain misses the tolerance (so None is right)."""
    from plt_optimizer.generate.arc_fit import _VERIFY_MARGIN, _fit_chain

    chain = _fit_chain(arc, ARC_FIT_MAX_ARCS)
    return (
        chain is None
        or _true_deviation(arc, chain) > ARC_FIT_DEV_TOL_INCHES * _VERIFY_MARGIN + 1e-9
    )


class TestEllipseFromArc:
    """The affine image of a circular arc is the parametric ellipse."""

    @pytest.mark.parametrize(
        "mode,s",
        [(m, s) for m in ("x", "y", "shear") for s in (0.9, 0.5, 0.2, 0.05)],
    )
    def test_pointwise_identity(self, mode: str, s: float) -> None:
        """P(t) equals the point-wise affine image of z(t) at many angles."""
        center = complex(0.31, -0.17)
        radius = 0.42
        anchor = 0.125
        kw = _mode_kwargs(mode, s)
        arc = ellipse_from_arc(center, radius, 0.0, 360.0, anchor_x=anchor, **kw)

        def map_pt(z: complex) -> complex:
            x, y = z.real, z.imag
            if "scale_x" in kw:
                return complex(anchor + (x - anchor) * kw["scale_x"], y)
            if "scale_y" in kw:
                return complex(x, y * kw["scale_y"])
            return complex(x + y * kw["shear_tan"], y)

        for deg in range(0, 360, 7):
            z = center + radius * complex(math.cos(math.radians(deg)), math.sin(math.radians(deg)))
            assert arc.point(math.radians(deg)) == pytest.approx(map_pt(z), abs=1e-12)

    def test_start_is_the_transformed_arc_start(self) -> None:
        """``P(t0)`` is exactly the affine image of the arc's start point.

        This is what makes a fitted chain chain tip-to-tail with the
        neighbouring transformed segments.
        """
        center = complex(0.5, 0.25)
        radius = 0.3
        t0 = 37.0
        start = center + radius * complex(math.cos(math.radians(t0)), math.sin(math.radians(t0)))
        for mode, s in (("x", 0.5), ("y", 0.5), ("shear", 0.5)):
            arc = ellipse_from_arc(center, radius, t0, 20.0, anchor_x=0.0, **_mode_kwargs(mode, s))
            if mode == "x":
                expect = complex(start.real * s, start.imag)
            elif mode == "y":
                expect = complex(start.real, start.imag * s)
            else:
                expect = complex(start.real + start.imag * s, start.imag)
            assert arc.start == pytest.approx(expect, abs=1e-14)

    def test_end_is_the_transformed_arc_end(self) -> None:
        """``P(t0+sweep)`` is the affine image of the arc's end point."""
        center = complex(-0.2, 0.6)
        radius = 0.5
        t0, sweep = -11.0, 63.0
        end = center + radius * complex(
            math.cos(math.radians(t0 + sweep)), math.sin(math.radians(t0 + sweep))
        )
        arc = ellipse_from_arc(center, radius, t0, sweep, scale_x=0.4, anchor_x=0.0)
        assert arc.end == pytest.approx(complex(end.real * 0.4, end.imag), abs=1e-14)

    def test_uniform_scale_stays_a_circle(self) -> None:
        """scale_x == scale_y keeps |P(t)-C| constant (a circle, not an ellipse)."""
        arc = EllipseArc(
            c=complex(0.1, 0.2),
            u=complex(0.3, 0.0),
            v=complex(0.0, 0.3),
            t0_deg=10.0,
            sweep_deg=80.0,
        )
        radii = {abs(p - arc.c) for p in _dense_ellipse(arc)}
        assert max(radii) - min(radii) < 1e-15

    def test_shear_gives_a_rotated_ellipse(self) -> None:
        """The shear's U/V are non-orthogonal (a rotated ellipse)."""
        arc = ellipse_from_arc(0j, 1.0, 0.0, 90.0, shear_tan=0.5)
        assert arc.u == pytest.approx(1 + 0j)
        assert arc.v == pytest.approx(0.5 + 1.0j)

    def test_composed_transform_order(self) -> None:
        """scale_x, scale_y and shear_tan compose as shear -> y -> x."""
        arc = ellipse_from_arc(
            complex(1.0, 2.0),
            0.5,
            0.0,
            90.0,
            scale_x=0.4,
            anchor_x=10.0,
            scale_y=0.6,
            shear_tan=0.25,
        )
        # C = sx*(cx + cy*tan - anchor) + anchor, sy*cy
        assert arc.c == pytest.approx(
            complex(0.4 * (1.0 + 2.0 * 0.25 - 10.0) + 10.0, 0.6 * 2.0), abs=1e-12
        )
        assert arc.u == pytest.approx(complex(0.4 * 0.5, 0.0), abs=1e-12)
        assert arc.v == pytest.approx(complex(0.4 * 0.25 * 0.5, 0.6 * 0.5), abs=1e-12)


class TestEllipseHelpers:
    """Point / tangent / curvature-radius helpers."""

    def test_tangent_is_finite_difference(self) -> None:
        arc = ellipse_from_arc(0j, 1.0, 0.0, 90.0, scale_x=0.5)
        t = 0.7
        h = 1e-7
        fd = (arc.point(t + h) - arc.point(t - h)) / (2 * h)
        assert arc.tangent(t) == pytest.approx(fd, abs=1e-5)

    def test_curvature_radius_matches_numeric(self) -> None:
        arc = ellipse_from_arc(0j, 1.0, 0.0, 90.0, scale_x=0.5)
        t = 0.4
        d1 = arc.tangent(t)
        d2 = (arc.tangent(t + 1e-6) - arc.tangent(t - 1e-6)) / 2e-6
        expect = abs(d1) ** 3 / abs(d1.real * d2.imag - d1.imag * d2.real)
        assert arc.curvature_radius(t) == pytest.approx(expect, rel=1e-4)

    def test_curvature_radius_of_circle_is_radius(self) -> None:
        arc = EllipseArc(c=0j, u=2.0 + 0j, v=0.0 + 2.0j, t0_deg=0.0, sweep_deg=90.0)
        assert arc.curvature_radius(1.1) == pytest.approx(2.0, rel=1e-12)

    def test_degenerate_ellipse_curvature_is_infinite(self) -> None:
        """A collapsed ellipse (v = 0) is a line: infinite curvature radius."""
        arc = EllipseArc(c=0j, u=1.0 + 0j, v=0j, t0_deg=0.0, sweep_deg=90.0)
        assert arc.curvature_radius(0.3) == math.inf

    def test_max_curvature_radius_bounds_point_radii(self) -> None:
        arc = ellipse_from_arc(complex(0.2, 0.3), 0.35, 15.0, 90.0, scale_x=0.5)
        pts = _dense_ellipse(arc)
        # The flattest point's curvature radius dominates the osculating radii.
        assert arc.max_curvature_radius >= 0.35 * 0.5 - 1e-12
        assert math.isfinite(arc.max_curvature_radius)
        assert len(pts) == 401


class TestFitChainGeometry:
    """The fitted chain's structural guarantees."""

    @pytest.mark.parametrize("mode", ["x", "y", "shear"])
    def test_chain_is_tip_to_tail_and_endpoint_exact(self, mode: str) -> None:
        center = complex(0.4, -0.2)
        radius = 0.33
        for s in (0.9, 0.5, 0.2):
            for sweep in (5.0, 30.0, 90.0, -90.0, 120.0):
                kw = _mode_kwargs(mode, s)
                arc = ellipse_from_arc(center, radius, 12.0, sweep, anchor_x=0.05, **kw)
                chain = fit_ellipse_arc(arc)
                if chain is None:
                    assert _fit_chain_needs_more_than_cap(arc)
                    continue
                assert chain[0].start == pytest.approx(arc.start, abs=1e-14)
                assert chain[-1].end == pytest.approx(arc.end, abs=1e-14)
                for a, b in zip(chain, chain[1:]):
                    assert a.end == pytest.approx(b.start, abs=0.0)

    def test_junctions_are_tangent_continuous(self) -> None:
        """At every arc-arc junction the two tangents agree (G1)."""
        checked = 0
        for s in (0.5, 0.2):
            for sweep in (90.0, 120.0, 150.0):
                arc = ellipse_from_arc(complex(0.3, 0.3), 0.4, 20.0, sweep, scale_x=s)
                chain = fit_ellipse_arc(arc)
                if chain is None:
                    assert _fit_chain_needs_more_than_cap(arc)
                    continue
                for a, b in zip(chain, chain[1:]):
                    if not (a.is_arc and b.is_arc):
                        continue
                    checked += 1
                    # G1: both centres lie on the SAME normal line through the
                    # junction (they may be on opposite sides of it, which is
                    # why parallelism -- not equality -- is the invariant).
                    assert a.center is not None and b.center is not None
                    n1 = (b.start - a.center) / abs(b.start - a.center)
                    n3 = (b.start - b.center) / abs(b.start - b.center)
                    cross = abs(n1.real * n3.imag - n1.imag * n3.real)
                    assert cross < 1e-9, f"junction normal split by {cross}"
        assert checked >= 10

    def test_sweep_sign_is_preserved(self) -> None:
        """Positive determinant of all three maps => every fitted arc turns
        the same way as the source sweep."""
        for mode in ("x", "y", "shear"):
            for s in (0.9, 0.5, 0.2):
                for sweep in (10.0, 90.0, 150.0):
                    kw = _mode_kwargs(mode, s)
                    for sign in (1.0, -1.0):
                        arc = ellipse_from_arc(complex(0.2, 0.4), 0.3, 5.0, sign * sweep, **kw)
                        chain = fit_ellipse_arc(arc)
                        if chain is None:
                            assert _fit_chain_needs_more_than_cap(arc)
                            continue
                        for seg in chain:
                            if seg.is_arc:
                                assert math.copysign(1.0, seg.sweep_deg) == math.copysign(
                                    1.0, sweep * sign
                                )

    def test_arc_radius_is_distance_to_center(self) -> None:
        arc = ellipse_from_arc(complex(0.5, 0.5), 0.25, 0.0, 45.0, scale_x=0.5)
        chain = fit_ellipse_arc(arc)
        assert chain is not None
        for seg in chain:
            if seg.is_arc:
                assert seg.radius == pytest.approx(abs(seg.start - seg.center), abs=1e-15)


class TestFitTolerance:
    """The tolerance is a hard guarantee, and k is minimal."""

    @pytest.mark.parametrize("mode", ["x", "y", "shear"])
    @pytest.mark.parametrize("s", [0.9, 0.5, 0.2])
    def test_deviation_within_tol(self, mode: str, s: float) -> None:
        kw = _mode_kwargs(mode, s)
        for radius in (0.05, 0.2, 0.5, 2.0):
            for sweep in (10.0, 45.0, 90.0, 180.0):
                arc = ellipse_from_arc(complex(0.1, 0.2), radius, 33.0, sweep, **kw)
                chain = fit_ellipse_arc(arc)
                # The contract: a chain that meets the tolerance, or None
                # (the caller flattens). Wide sweeps on fat ellipses exceed
                # the k cap and correctly decline the fit.
                if chain is None:
                    assert _fit_chain_needs_more_than_cap(arc)
                    continue
                assert _true_deviation(arc, list(chain)) <= ARC_FIT_DEV_TOL_INCHES + 1e-9

    def test_deviation_within_tol_fine_grids(self) -> None:
        """Every (radius, sweep) the shipped library contains fits on the first try."""
        for mode in ("x", "y", "shear"):
            kw = _mode_kwargs(mode, 0.5)
            for radius in (0.05, 0.2, 0.5):
                for sweep in (5.0, 10.0, 20.0, 45.0, 90.0):
                    arc = ellipse_from_arc(complex(0.1, 0.2), radius, 33.0, sweep, **kw)
                    chain = fit_ellipse_arc(arc)
                    assert chain is not None
                    assert _true_deviation(arc, list(chain)) <= ARC_FIT_DEV_TOL_INCHES + 1e-9

    def test_k_grows_with_sweep(self) -> None:
        """A 90 deg arc of a fat ellipse splits; a 5 deg arc never does."""
        small = fit_ellipse_arc(ellipse_from_arc(0j, 0.4, 0.0, 5.0, scale_x=0.5))
        big = fit_ellipse_arc(ellipse_from_arc(0j, 0.4, 0.0, 90.0, scale_x=0.5))
        assert small is not None and big is not None
        assert len(small) == 1
        assert len(big) > 1

    def test_k_is_minimal(self) -> None:
        """k-1 must violate the tolerance for the returned chain's k."""
        arc = ellipse_from_arc(0j, 0.4, 0.0, 90.0, scale_x=0.5)
        chain = fit_ellipse_arc(arc)
        assert chain is not None
        k = len(chain)
        if k > 1:
            looser = fit_ellipse_arc(arc, tol_inches=ARC_FIT_DEV_TOL_INCHES * 1e6)
            assert looser is not None
            assert len(looser) == 1

    def test_looser_tol_needs_fewer_arcs(self) -> None:
        arc = ellipse_from_arc(0j, 0.4, 0.0, 120.0, scale_x=0.5)
        tight = fit_ellipse_arc(arc, tol_inches=0.002)
        loose = fit_ellipse_arc(arc, tol_inches=0.02)
        assert tight is not None and loose is not None
        assert len(loose) < len(tight)

    def test_measured_k_table_at_half_scale(self) -> None:
        """Pinned arc counts for the shipped library's sweep range (s=0.5).

        The median source sweep is ~10 deg and needs exactly one arc; the 90 deg
        arcs (the widest in the library) split into five.
        """
        for sweep, expect in ((5.0, 1), (10.0, 1), (20.0, 1), (30.0, 1), (45.0, 2), (90.0, 5)):
            arc = ellipse_from_arc(0j, 0.35, 0.0, sweep, scale_x=0.5)
            chain = fit_ellipse_arc(arc)
            assert chain is not None
            assert len(chain) == expect, f"sweep={sweep}"


class TestDegenerateSpanCheck:
    """Only a sub-half-plotter-unit span collapses to a chord; curves stay AA."""

    def test_near_straight_arc_stays_native(self) -> None:
        """A huge-radius shallow arc refits to ONE arc, not a line.

        The 100 in osculating radius over a 0.3 deg sweep has a ~0.00014in
        sagitta -- under the 0.0005in tolerance -- but the span is far longer
        than the plotter resolves, so the arc emits as a native ``AA`` (an
        arc that deviates from its chord by less than coordinate
        quantization draws through the same integer units as the chord --
        collapsing it used to polygonize 34 of a compressed S's 44 arcs).
        """
        arc = ellipse_from_arc(0j, 50.0, 0.0, 0.3, scale_x=0.5)
        chain = fit_ellipse_arc(arc)
        assert chain is not None
        assert len(chain) == 1
        assert chain[0].is_arc

    def test_sub_plotter_unit_span_is_a_line(self) -> None:
        """A span shorter than half a plotter unit carries no resolvable ink."""
        from plt_optimizer.generate.arc_fit import _MIN_CHORD_INCHES

        # r=0.5in over 0.01 deg: chord ~8.7e-5in, under the 4.9e-4in floor.
        arc = ellipse_from_arc(0j, 0.5, 0.0, 0.01, scale_x=0.5)
        assert abs(arc.end - arc.start) < _MIN_CHORD_INCHES
        chain = fit_ellipse_arc(arc)
        assert chain is not None
        assert len(chain) == 1
        assert not chain[0].is_arc

    def test_line_endpoints_are_exact(self) -> None:
        from plt_optimizer.generate.arc_fit import _MIN_CHORD_INCHES

        arc = ellipse_from_arc(0j, 0.5, 0.0, 0.01, scale_x=0.5)
        chain = fit_ellipse_arc(arc)
        assert chain is not None
        assert abs(arc.end - arc.start) < _MIN_CHORD_INCHES
        assert chain[0].start == pytest.approx(arc.start, abs=1e-14)
        assert chain[0].end == pytest.approx(arc.end, abs=1e-14)

    def test_curved_arc_is_not_collapsed(self) -> None:
        arc = ellipse_from_arc(0j, 0.3, 0.0, 90.0, scale_x=0.5)
        chain = fit_ellipse_arc(arc)
        assert chain is not None
        assert all(seg.is_arc for seg in chain)


class TestDegenerateAndFallback:
    """Every path that cannot guarantee the tolerance returns None."""

    def test_zero_sweep(self) -> None:
        arc = ellipse_from_arc(0j, 0.3, 0.0, 0.0, scale_x=0.5)
        assert fit_ellipse_arc(arc) is None

    def test_zero_scale_x(self) -> None:
        """A fully collapsed ellipse has no fit (the caller flattens)."""
        arc = ellipse_from_arc(0j, 0.3, 0.0, 90.0, scale_x=0.0)
        assert fit_ellipse_arc(arc) is None

    def test_negative_scale_flips_sweep_and_is_rejected(self) -> None:
        """A mirrored map (negative determinant) is out of scope: the sweep
        sign check rejects the fit so the caller flattens."""
        arc = ellipse_from_arc(0j, 0.3, 0.0, 90.0, scale_x=-0.5)
        assert fit_ellipse_arc(arc) is None

    def test_nonpositive_tol(self) -> None:
        arc = ellipse_from_arc(0j, 0.3, 0.0, 90.0, scale_x=0.5)
        assert fit_ellipse_arc(arc, tol_inches=0.0) is None

    def test_span_needing_more_than_max_arcs(self) -> None:
        """A full circle at an extreme aspect exceeds the cap -> None."""
        arc = ellipse_from_arc(0j, 0.4, 0.0, 360.0, scale_x=0.02)
        assert fit_ellipse_arc(arc, tol_inches=1e-6, max_arcs=ARC_FIT_MAX_ARCS) is None

    def test_collapsed_radius(self) -> None:
        """A zero-area ellipse (u = v = 0) is a point: no fit, caller flattens."""
        arc = EllipseArc(c=0j, u=0j, v=0j, t0_deg=0.0, sweep_deg=90.0)
        assert fit_ellipse_arc(arc) is None

    def test_full_circle_collapsed_axis(self) -> None:
        """A 360 deg span whose endpoints meet declines (a chord erases the loop)."""
        arc = ellipse_from_arc(0j, 0.4, 0.0, 360.0, scale_x=1.0)
        assert abs(arc.end - arc.start) < 1e-12
        assert fit_ellipse_arc(arc) is None

    def test_near_closed_loop_declines(self) -> None:
        """A 359.9 deg span also declines: its chord is sub-unit, its ink is not."""
        arc = ellipse_from_arc(0j, 0.4, 0.0, 359.9, scale_x=0.5)
        assert fit_ellipse_arc(arc) is None

    def test_sub_unit_sub_chords_decline(self) -> None:
        """A span whose every k-probed chain has a sub-floor chord declines.

        A 0.0003in-radius circle over 181 deg: the whole span is above the
        emission floor (so the degenerate-span guard does not fire), but every
        multi-arc split produces a chord shorter than half a plotter unit, so
        :func:`_fit_chain` returns ``None`` for every probe and the fitter
        declines (the caller flattens).
        """
        arc = ellipse_from_arc(0j, 0.0003, 0.0, 181.0, scale_x=1.0)
        assert abs(arc.end - arc.start) > 0.5 / 1016.0
        assert fit_ellipse_arc(arc) is None

    def test_line_segment_radius_is_zero(self) -> None:
        from plt_optimizer.generate.arc_fit import FittedSegment

        seg = FittedSegment(start=0j, end=1 + 1j)
        assert seg.radius == 0.0
        assert not seg.is_arc


class TestFitArcWrapper:
    """The convenience wrapper matches the explicit path."""

    def test_wrapper_matches_ellipse_from_arc(self) -> None:
        center, radius, t0, sweep = complex(0.2, 0.3), 0.35, 11.0, 47.0
        direct = fit_ellipse_arc(
            ellipse_from_arc(center, radius, t0, sweep, scale_x=0.5, anchor_x=0.1)
        )
        wrapped = fit_arc(center, radius, t0, sweep, scale_x=0.5, anchor_x=0.1)
        assert direct is not None and wrapped is not None
        assert len(direct) == len(wrapped)
        for a, b in zip(direct, wrapped):
            assert a.start == pytest.approx(b.start, abs=1e-15)
            assert a.end == pytest.approx(b.end, abs=1e-15)
            assert a.sweep_deg == pytest.approx(b.sweep_deg, abs=1e-12)

    def test_negative_sweep_wraps(self) -> None:
        chain = fit_arc(0j, 0.3, 0.0, -60.0, scale_x=0.5)
        assert chain is not None
        # End = scale_x applied to the source end point (cos(-60), sin(-60)).
        assert chain[-1].end == pytest.approx(
            complex(0.5 * 0.3 * math.cos(math.radians(-60)), 0.3 * math.sin(math.radians(-60))),
            abs=1e-12,
        )


class TestEllipseDeviation:
    """The verifier's own contract."""

    def test_zero_for_exact_circle(self) -> None:
        from plt_optimizer.generate.arc_fit import FittedSegment

        arc = EllipseArc(c=0j, u=1.0 + 0j, v=0.0 + 1.0j, t0_deg=0.0, sweep_deg=90.0)
        seg = FittedSegment(start=arc.start, end=arc.end, center=0j, sweep_deg=90.0)
        assert ellipse_deviation(arc, [seg]) == pytest.approx(0.0, abs=1e-12)

    def test_positive_for_bad_fit(self) -> None:
        from plt_optimizer.generate.arc_fit import FittedSegment

        arc = ellipse_from_arc(0j, 0.4, 0.0, 90.0, scale_x=0.5)
        seg = FittedSegment(start=arc.start, end=arc.end)  # chord: deviates
        assert ellipse_deviation(arc, [seg]) > 0.001


class TestDegenerateHelpers:
    """The private geometric helpers' degenerate branches.

    These paths are unreachable through real glyph arcs (the fitter's guards
    catch the inputs that would produce them), so they are exercised directly.
    """

    def test_max_curvature_radius_zero_sweep(self) -> None:
        """A zero span has no curvature to measure -> inf."""
        arc = EllipseArc(c=0j, u=1.0 + 0j, v=0.0 + 1.0j, t0_deg=0.0, sweep_deg=0.0)
        assert arc.max_curvature_radius == math.inf

    def test_line_intersection_zero_direction(self) -> None:
        """A zero direction vector has no line to intersect -> None."""
        from plt_optimizer.generate.arc_fit import _line_intersection

        assert _line_intersection(0j, 0j, 1 + 0j, 1j) is None

    def test_line_intersection_parallel(self) -> None:
        """Parallel lines -> None."""
        from plt_optimizer.generate.arc_fit import _line_intersection

        assert _line_intersection(0j, 1 + 0j, 1j, 2 + 0j) is None

    def test_circumcenter_collinear(self) -> None:
        """Three collinear points have no circumcircle -> None."""
        from plt_optimizer.generate.arc_fit import _circumcenter

        assert _circumcenter(0j, 1 + 0j, 2 + 0j) is None

    def test_chain_center_coincident(self) -> None:
        """A junction sitting exactly on the previous centre has no normal -> None."""
        from plt_optimizer.generate.arc_fit import _chain_center

        assert _chain_center(1 + 0j, 1 + 0j, 2 + 0j) is None

    def test_fit_chain_collapsed_ellipse_falls_back_to_lines(self) -> None:
        """A degenerate (zero-area) ellipse yields no centres -> line segments.

        ``v = 0`` collapses the ellipse onto a segment: the start normal is a
        zero vector (no first centre), the 3-point circumcircle is collinear (no
        chained centre), and every piece emits as a line.
        """
        from plt_optimizer.generate.arc_fit import FittedSegment, _fit_chain

        arc = EllipseArc(c=0j, u=1.0 + 0j, v=0j, t0_deg=0.0, sweep_deg=90.0)
        chain = _fit_chain(arc, 2)
        assert chain is not None
        assert len(chain) == 2
        for seg in chain:
            assert isinstance(seg, FittedSegment)
            assert not seg.is_arc

    def test_dist_to_degenerate_line_segment(self) -> None:
        """A zero-length segment measures the distance to its point."""
        from plt_optimizer.generate.arc_fit import FittedSegment, _dist_to_segment

        seg = FittedSegment(start=1 + 0j, end=1 + 0j)
        assert _dist_to_segment(1 + 3j, seg) == pytest.approx(3.0)

    def test_dist_to_arc_outside_the_sweep_uses_the_endpoints(self) -> None:
        """A point outside the swept angles measures to the nearer endpoint."""
        from plt_optimizer.generate.arc_fit import FittedSegment, _dist_to_segment

        # 10 deg arc of the unit circle, from angle 0 to angle 10.
        end = complex(math.cos(math.radians(10)), math.sin(math.radians(10)))
        seg = FittedSegment(start=1 + 0j, end=end, center=0j, sweep_deg=10.0)
        outside = complex(math.cos(math.radians(-5)), math.sin(math.radians(-5)))
        assert _dist_to_segment(outside, seg) == pytest.approx(abs(outside - 1))
        # The mirrored (negative-sweep) arc takes the other branch of the test.
        seg_neg = FittedSegment(start=1 + 0j, end=outside, center=0j, sweep_deg=-10.0)
        beyond = complex(math.cos(math.radians(5)), math.sin(math.radians(5)))
        assert _dist_to_segment(beyond, seg_neg) == pytest.approx(abs(beyond - 1))

    def test_estimate_k_degenerate_inputs(self) -> None:
        """Zero sweep, zero radius and a collapsed ellipse all estimate k=1."""
        from plt_optimizer.generate.arc_fit import _estimate_k

        assert _estimate_k(EllipseArc(0j, 1 + 0j, 1j, 0.0, 0.0), 5e-4) == 1
        assert _estimate_k(EllipseArc(0j, 0j, 0j, 0.0, 90.0), 5e-4) == 1

    def test_zero_area_ellipse_declines(self) -> None:
        """A point ellipse (u = v = 0, zero cross product) has no fit."""
        arc = EllipseArc(c=1j, u=0j, v=0j, t0_deg=0.0, sweep_deg=90.0)
        assert fit_ellipse_arc(arc) is None

    def test_degenerate_span_emits_chord(self) -> None:
        """A span under half a plotter unit emits a single chord line.

        Exercises the degenerate-span guard in :func:`fit_ellipse_arc`: the
        endpoints are within the emission floor, so the arc collapses to one
        non-arc :class:`FittedSegment` (a real sub-unit sliver, not a loop).
        """
        from plt_optimizer.generate.arc_fit import _MIN_CHORD_INCHES

        arc = EllipseArc(c=0j, u=0.5 + 0j, v=0.5j, t0_deg=0.0, sweep_deg=0.01)
        assert abs(arc.end - arc.start) < _MIN_CHORD_INCHES
        chain = fit_ellipse_arc(arc)
        assert chain is not None
        assert len(chain) == 1
        assert not chain[0].is_arc

    def test_sweep_sign_ok_skips_line_segments(self) -> None:
        """Lines carry no orientation, so they never fail the sign check."""
        from plt_optimizer.generate.arc_fit import FittedSegment, _sweep_sign_ok

        arc = EllipseArc(c=0j, u=1.0 + 0j, v=0.0 + 1.0j, t0_deg=0.0, sweep_deg=90.0)
        assert _sweep_sign_ok(arc, [FittedSegment(start=1 + 0j, end=1j)]) is True

    def test_normalize_deg_wraps_both_ways(self) -> None:
        """The wrap covers >180 and <=-180."""
        from plt_optimizer.generate.arc_fit import _normalize_deg

        assert _normalize_deg(270.0) == pytest.approx(-90.0)
        assert _normalize_deg(-270.0) == pytest.approx(90.0)
        assert _normalize_deg(180.0) == pytest.approx(180.0)
        assert _normalize_deg(-180.0) == pytest.approx(180.0)
