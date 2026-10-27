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
    SuperellipseArc,
    ellipse_deviation,
    ellipse_from_arc,
    fit_arc,
    fit_ellipse_arc,
    fit_superellipse_arc,
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
        arcs (the widest in the library) split into six. Re-pinned for the
        0.0002in tolerance (EngraveLab reference parity): 30 deg now splits
        into 2, 45 into 3, 90 into 5 -> 6.
        """
        for sweep, expect in ((5.0, 1), (10.0, 1), (20.0, 1), (30.0, 2), (45.0, 3), (90.0, 6)):
            arc = ellipse_from_arc(0j, 0.35, 0.0, sweep, scale_x=0.5)
            chain = fit_ellipse_arc(arc)
            assert chain is not None
            assert len(chain) == expect, f"sweep={sweep}"


class TestDegenerateSpanCheck:
    """Only a sub-half-plotter-unit span collapses to a chord; curves stay AA."""

    def test_near_straight_arc_stays_native(self) -> None:
        """A huge-radius shallow arc refits to ONE arc, not a line.

        The 100 in osculating radius over a 0.3 deg sweep has a ~0.00014in
        sagitta -- under the 0.0002in tolerance -- but the span is far longer
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


class TestSuperellipseArcProtocol:
    """``SuperellipseArc`` satisfies the fitter's curve protocol exactly."""

    def _patch(self, **kw) -> SuperellipseArc:
        args = {
            "c": 0j,
            "rx": 1.0,
            "ry": 1.0,
            "exponent": 4.0,
            "sx": 1.0,
            "sy": 1.0,
            "t0_deg": 0.0,
            "sweep_deg": 90.0,
        }
        args.update(kw)
        return SuperellipseArc(**args)

    def test_points_match_the_renderer_sampling(self) -> None:
        """``point(t)`` equals the ``cos^p``/``sin^p`` form the polygon sampler uses."""
        arc = self._patch(rx=2.0, ry=1.0, exponent=4.0, sx=-1.0, sy=1.0)
        p = 2.0 / 4.0
        for t_deg in (0.0, 17.0, 45.0, 73.0, 90.0):
            t = math.radians(t_deg)
            z = arc.point(t)
            expect = complex(-2.0 * abs(math.cos(t)) ** p, 1.0 * abs(math.sin(t)) ** p)
            assert z == pytest.approx(expect, abs=1e-15)

    def test_axis_vertices_are_exact(self) -> None:
        """The quadrant endpoints hit the semi-axis vertices (to float noise).

        ``math.cos(pi/2)`` is 6.1e-17, not 0, so the far vertex carries the
        same ~1e-6 sub-plotter-unit artifact the existing ``cos^p`` polygon
        sampler has; the near vertex is exact.
        """
        arc = self._patch(rx=3.0, ry=2.0, exponent=5.0)
        assert arc.start == pytest.approx(3.0 + 0.0j, abs=1e-15)
        assert arc.end.real == pytest.approx(0.0, abs=1e-5)
        assert arc.end.imag == pytest.approx(2.0, abs=1e-15)

    def test_tangent_limits_at_the_vertices(self) -> None:
        """The vertex tangents are the exact axis-aligned limits (the G2 junction).

        At ``t = 0`` the point is the X-axis vertex ``(sx*rx, 0)`` and the curve
        rises along ``y``; at ``t = pi/2`` it is the Y-axis vertex and the curve
        runs along ``x``.
        """
        arc = self._patch(exponent=4.0)
        assert arc.tangent(0.0) == pytest.approx(0.0 + 1.0j, abs=1e-15)
        assert arc.tangent(math.pi / 2.0) == pytest.approx(-1.0 + 0.0j, abs=1e-15)
        # Openings toward -x / -y flip the limit directions.
        flipped = self._patch(exponent=4.0, sx=-1.0, sy=-1.0)
        assert flipped.tangent(0.0) == pytest.approx(0.0 - 1.0j, abs=1e-15)
        assert flipped.tangent(math.pi / 2.0) == pytest.approx(1.0 + 0.0j, abs=1e-15)

    def test_curvature_radius_is_infinite_at_vertices_for_n_gt_2(self) -> None:
        """The zero-curvature (G2) vertex property: R -> inf for n > 2."""
        arc = self._patch(exponent=4.0)
        assert arc.curvature_radius(0.0) == math.inf
        assert arc.curvature_radius(math.pi / 2.0) == math.inf
        assert math.isfinite(arc.curvature_radius(math.pi / 4.0))

    def test_curvature_radius_at_n_eq_2_matches_the_ellipse(self) -> None:
        """n == 2 is an ellipse quadrant: the numeric curvature agrees with EllipseArc."""
        sup = self._patch(rx=2.0, ry=1.0, exponent=2.0)
        ell = EllipseArc(c=0j, u=2.0 + 0j, v=0.0 + 1.0j, t0_deg=0.0, sweep_deg=90.0)
        for t_deg in (10.0, 30.0, 45.0, 60.0, 80.0):
            t = math.radians(t_deg)
            assert sup.curvature_radius(t) == pytest.approx(ell.curvature_radius(t), rel=1e-6)
            assert sup.point(t) == pytest.approx(ell.point(t), abs=1e-12)

    def test_max_curvature_radius_ignores_the_vertex_infinities(self) -> None:
        """The interior-only sampling keeps the estimate finite for n > 2."""
        arc = self._patch(rx=1.0, ry=1.0, exponent=20.0)
        assert math.isfinite(arc.max_curvature_radius)
        assert arc.max_curvature_radius > 0.0

    def test_zero_sweep_max_curvature_is_infinite(self) -> None:
        arc = self._patch(sweep_deg=0.0)
        assert arc.max_curvature_radius == math.inf

    def test_u_v_are_the_signed_semi_axis_vectors(self) -> None:
        arc = self._patch(rx=2.0, ry=0.5, sx=-1.0, sy=1.0)
        assert arc.u == pytest.approx(-2.0 + 0.0j)
        assert arc.v == pytest.approx(0.0 + 0.5j)


class TestFitSuperellipseArc:
    """The trim-to-line corner fit: caps + tangent-continuous chain, tol-guaranteed."""

    def _patch(self, rx, ry, n, **kw) -> SuperellipseArc:
        args = {
            "c": 0j,
            "rx": rx,
            "ry": ry,
            "exponent": n,
            "sx": 1.0,
            "sy": 1.0,
            "t0_deg": 0.0,
            "sweep_deg": 90.0,
        }
        args.update(kw)
        return SuperellipseArc(**args)

    def test_n_eq_2_is_bit_identical_to_the_ellipse_fit(self) -> None:
        """The ellipse quadrant skips the trim and matches ``fit_ellipse_arc`` exactly."""
        from plt_optimizer.generate.arc_fit import BORDER_ARC_FIT_MAX_ARCS

        sup = self._patch(2.0, 1.0, 2.0)
        ell = EllipseArc(c=0j, u=2.0 + 0j, v=0.0 + 1.0j, t0_deg=0.0, sweep_deg=90.0)
        chain_sup = fit_superellipse_arc(sup, max_arcs=BORDER_ARC_FIT_MAX_ARCS)
        chain_ell = fit_ellipse_arc(ell, max_arcs=BORDER_ARC_FIT_MAX_ARCS)
        assert chain_sup is not None and chain_ell is not None
        assert len(chain_sup) == len(chain_ell)
        for a, b in zip(chain_sup, chain_ell):
            assert a.start == pytest.approx(b.start, abs=1e-15)
            assert a.end == pytest.approx(b.end, abs=1e-15)
            assert a.is_arc == b.is_arc
            if a.is_arc:
                assert a.center == pytest.approx(b.center, abs=1e-15)
                assert a.sweep_deg == pytest.approx(b.sweep_deg, abs=1e-15)

    def test_chain_is_tip_to_tail_with_exact_endpoints(self) -> None:
        from plt_optimizer.generate.arc_fit import BORDER_ARC_FIT_MAX_ARCS

        arc = self._patch(0.5, 0.5, 4.0)
        chain = fit_superellipse_arc(arc, max_arcs=BORDER_ARC_FIT_MAX_ARCS)
        assert chain is not None
        assert chain[0].start == pytest.approx(arc.start, abs=1e-12)
        assert chain[-1].end == pytest.approx(arc.end, abs=1e-12)
        for a, b in zip(chain[:-1], chain[1:]):
            assert a.end == pytest.approx(b.start, abs=1e-12)

    def test_dense_deviation_within_tolerance(self) -> None:
        """The whole contour (caps + chain) stays within tol of the true curve."""
        from plt_optimizer.generate.arc_fit import BORDER_ARC_FIT_MAX_ARCS

        for rx, ry, n in (
            (0.1, 0.1, 20.0),
            (0.2, 0.2, 10.0),
            (0.5, 0.5, 4.0),
            (0.7, 0.7, 2.857142857),
            (0.9, 0.9, 2.222222222),
            (1.0, 0.5, 4.0),
            (2.0, 2.0, 4.0),
            (0.075, 0.075, 4.0),
        ):
            arc = self._patch(rx, ry, n)
            chain = fit_superellipse_arc(arc, max_arcs=BORDER_ARC_FIT_MAX_ARCS)
            assert chain is not None, (rx, ry, n)
            dev = _true_deviation(arc, list(chain))
            assert dev <= ARC_FIT_DEV_TOL_INCHES + 1e-9, (rx, ry, n, dev)

    def test_caps_are_short_chords_from_the_exact_vertices(self) -> None:
        """The trim caps are line chords starting/ending at the exact vertices.

        Each cap spans the vertex parameter span whose curve stays within tol
        of its chord, so it is short (sub-degree) and its outer endpoint is
        the patch's axis vertex -- the contour therefore starts and ends
        exactly on the box's straight edge.
        """
        from plt_optimizer.generate.arc_fit import BORDER_ARC_FIT_MAX_ARCS

        arc = self._patch(0.5, 0.5, 4.0)
        chain = fit_superellipse_arc(arc, max_arcs=BORDER_ARC_FIT_MAX_ARCS)
        assert chain is not None
        lines = [s for s in chain if not s.is_arc]
        assert lines, "a squircle quadrant needs trim caps"
        # The first cap starts at the X-vertex; the last ends at the Y-vertex.
        assert lines[0].start == pytest.approx(arc.start, abs=1e-15)
        assert lines[-1].end == pytest.approx(arc.end, abs=1e-15)
        # Every cap is a short chord of the flat end (the trim removes only
        # the near-vertex span; here ~3.3 degrees of parameter).
        for cap in lines:
            assert abs(cap.end - cap.start) < 0.5 * min(arc.rx, arc.ry)

    def test_negative_sweep_supported(self) -> None:
        """A reversed traversal fits with the same geometry (mirrored chain)."""
        from plt_optimizer.generate.arc_fit import BORDER_ARC_FIT_MAX_ARCS

        fwd = self._patch(0.5, 0.5, 4.0, t0_deg=90.0, sweep_deg=-90.0)
        chain = fit_superellipse_arc(fwd, max_arcs=BORDER_ARC_FIT_MAX_ARCS)
        assert chain is not None
        assert chain[0].start == pytest.approx(fwd.start, abs=1e-12)
        assert chain[-1].end == pytest.approx(fwd.end, abs=1e-12)
        assert _true_deviation(fwd, list(chain)) <= ARC_FIT_DEV_TOL_INCHES + 1e-9
        arcs = [s for s in chain if s.is_arc]
        assert arcs and all(s.sweep_deg < 0.0 for s in arcs)

    def test_near_straight_patch_collapses_to_one_line(self) -> None:
        """A patch whose whole span lies within tol of its chord emits one line.

        A 0.0004in semi-axis corner is sub-plotter-unit at every parameter, so
        the trim test accepts the full span and the corner is a single chord.
        """
        from plt_optimizer.generate.arc_fit import BORDER_ARC_FIT_MAX_ARCS

        arc = self._patch(0.0004, 0.0004, 4.0)
        chain = fit_superellipse_arc(arc, max_arcs=BORDER_ARC_FIT_MAX_ARCS)
        assert chain is not None
        assert len(chain) == 1
        assert not chain[0].is_arc

    def test_zero_sweep_declines(self) -> None:
        from plt_optimizer.generate.arc_fit import BORDER_ARC_FIT_MAX_ARCS

        arc = self._patch(0.5, 0.5, 4.0, sweep_deg=0.0)
        assert fit_superellipse_arc(arc, max_arcs=BORDER_ARC_FIT_MAX_ARCS) is None

    def test_degenerate_geometry_declines(self) -> None:
        """A patch the chain cap cannot resolve declines (caller polygonizes)."""
        from plt_optimizer.generate.arc_fit import SuperellipseArc

        # Extreme aspect: the ellipse branch needs > 12 arcs at the default cap.
        arc = SuperellipseArc(
            c=0j, rx=10.0, ry=0.25, exponent=2.0, sx=1.0, sy=1.0, t0_deg=0.0, sweep_deg=90.0
        )
        assert fit_superellipse_arc(arc, max_arcs=12) is None
        assert arc.exponent == 2.0  # ellipse branch, cap-bound

    def test_dense_verification_rejects_the_under_read_chain(self) -> None:
        """The 400-sample contour pass gates fits the 32-sample verifier misses.

        The stadium corner (rx=ry=2, n=4) is the measured worst case: its
        24-arc inner chain passes the 32-sample verifier (0.000141in, under the
        0.00016in gate) yet deviates 0.000233in densely, so the first-attempt
        fit is rejected and only the half-tolerance retry is emitted.
        """
        from plt_optimizer.generate.arc_fit import (
            _VERIFY_MARGIN,
            BORDER_ARC_FIT_MAX_ARCS,
            _contour_deviation,
            _fit_chain,
            _trim_to_line_span,
        )

        arc = self._patch(2.0, 2.0, 4.0)
        chain = fit_superellipse_arc(arc, max_arcs=BORDER_ARC_FIT_MAX_ARCS)
        assert chain is not None
        assert _true_deviation(arc, list(chain)) <= ARC_FIT_DEV_TOL_INCHES + 1e-9

        # Show the dense guard is load-bearing on this patch: the 24-arc chain
        # of the trimmed inner span clears the 32-sample gate but misses tol
        # densely, so it is exactly what the contour check rejects.
        t_lo, t_hi = _trim_to_line_span(arc, ARC_FIT_DEV_TOL_INCHES)
        inner = SuperellipseArc(
            c=0j,
            rx=2.0,
            ry=2.0,
            exponent=4.0,
            sx=1.0,
            sy=1.0,
            t0_deg=math.degrees(t_lo),
            sweep_deg=math.degrees(t_hi - t_lo),
        )
        loose = _fit_chain(inner, 24)
        assert loose is not None
        assert ellipse_deviation(inner, loose) <= ARC_FIT_DEV_TOL_INCHES * _VERIFY_MARGIN
        assert _contour_deviation(inner, tuple(loose)) > ARC_FIT_DEV_TOL_INCHES


class TestSuperellipseHelpers:
    """Direct coverage of the trim/assemble helpers' degenerate branches."""

    def _patch(self, **kw) -> SuperellipseArc:
        args = {
            "c": 0j,
            "rx": 0.5,
            "ry": 0.5,
            "exponent": 4.0,
            "sx": 1.0,
            "sy": 1.0,
            "t0_deg": 0.0,
            "sweep_deg": 90.0,
        }
        args.update(kw)
        return SuperellipseArc(**args)

    def test_chord_deviation_of_a_point_span_is_zero(self) -> None:
        """A degenerate chord (t_a == t_b) carries no deviation."""
        from plt_optimizer.generate.arc_fit import _chord_deviation

        arc = self._patch()
        assert _chord_deviation(arc, 0.5, 0.5) == 0.0

    def test_assemble_caps_skips_zero_length_caps(self) -> None:
        """A chain starting/ending exactly on the vertices gets no caps."""
        from plt_optimizer.generate.arc_fit import FittedSegment, _assemble_caps

        arc = self._patch()
        chain = (
            FittedSegment(start=arc.start, end=0.35 + 0.35j),
            FittedSegment(start=0.35 + 0.35j, end=arc.end),
        )
        assert _assemble_caps(arc, chain) == chain

    def test_assemble_caps_keeps_sub_unit_caps(self) -> None:
        """A cap shorter than a plotter unit is kept (the exact-vertex contract)."""
        from plt_optimizer.generate.arc_fit import (
            _MIN_CHORD_INCHES,
            FittedSegment,
            _assemble_caps,
        )

        arc = self._patch()
        tiny = arc.start + complex(0.0, _MIN_CHORD_INCHES / 2.0)
        chain = (FittedSegment(start=tiny, end=arc.end),)
        out = _assemble_caps(arc, chain)
        assert len(out) == 2
        assert out[0].start == pytest.approx(arc.start)
        assert out[0].end == pytest.approx(tiny)

    def test_retry_decline_returns_none(self) -> None:
        """A patch whose half-tolerance retry still misses declines.

        The rx=6/ry=3 n=4 patch is the measured case: both the first fit and
        the half-tolerance retry saturate the 32-arc cap at a dense deviation
        of ~0.00068in, so the caller polygonizes.
        """
        from plt_optimizer.generate.arc_fit import BORDER_ARC_FIT_MAX_ARCS

        arc = self._patch(rx=6.0, ry=3.0)
        assert fit_superellipse_arc(arc, max_arcs=BORDER_ARC_FIT_MAX_ARCS) is None

    def test_retry_exhausting_the_cap_returns_none(self) -> None:
        """The retry fitting ``None`` (cap below its needs) declines too.

        The stadium patch's first fit lands on 24 arcs (passes the 32-sample
        gate, misses densely); the half-tolerance retry needs 27, so a cap of
        26 leaves the retry with no admissible chain -- the ``None`` return
        exits through the retry guard, not the first-fit one.
        """
        arc = self._patch(rx=2.0, ry=2.0)
        assert fit_superellipse_arc(arc, max_arcs=26) is None

    def test_retry_path_emits_the_tighter_chain(self) -> None:
        """The stadium corner takes the half-tolerance retry and passes densely."""
        from plt_optimizer.generate.arc_fit import (
            BORDER_ARC_FIT_MAX_ARCS,
            _assemble_caps,
            _contour_deviation,
            _fit_chain,
            _trim_to_line_span,
        )

        arc = self._patch(rx=2.0, ry=2.0)
        chain = fit_superellipse_arc(arc, max_arcs=BORDER_ARC_FIT_MAX_ARCS)
        assert chain is not None
        assert _contour_deviation(arc, chain) <= ARC_FIT_DEV_TOL_INCHES
        # The first-attempt 24-arc chain misses densely (the gate fires); the
        # emitted chain is the retry's (27 arcs of the inner span + 2 caps).
        t_lo, t_hi = _trim_to_line_span(arc, ARC_FIT_DEV_TOL_INCHES)
        inner = SuperellipseArc(
            c=0j,
            rx=2.0,
            ry=2.0,
            exponent=4.0,
            sx=1.0,
            sy=1.0,
            t0_deg=math.degrees(t_lo),
            sweep_deg=math.degrees(t_hi - t_lo),
        )
        first = _fit_chain(inner, 24)
        assert first is not None
        assert _contour_deviation(arc, _assemble_caps(arc, tuple(first))) > ARC_FIT_DEV_TOL_INCHES
        arcs = sum(1 for s in chain if s.is_arc)
        assert arcs > 24

    def test_zero_semi_axis_curvature_is_infinite(self) -> None:
        """A collapsed axis (rx = 0) has a vanishing cross product: R = inf."""
        arc = self._patch(rx=0.0, ry=1.0)
        assert arc.curvature_radius(math.pi / 4.0) == math.inf

    def test_first_fit_cap_exhaustion_declines(self) -> None:
        """A patch whose inner span needs more arcs than the cap declines.

        The stadium-scale patch (rx=ry=2, n=4) needs ~24 inner arcs; with a
        cap of 4 every probe misses the 32-sample gate, so the FIRST fit
        returns ``None`` and the retry never runs.
        """
        arc = self._patch(rx=2.0, ry=2.0)
        assert fit_superellipse_arc(arc, max_arcs=4) is None
