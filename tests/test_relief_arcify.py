"""Unit tests for relief/arcify.py (ring polyline -> native AA arc chains)."""

from __future__ import annotations

import math
from pathlib import Path

from shapely.geometry import LineString, Point, Polygon

from plt_optimizer.core.models import ArcSegment
from plt_optimizer.core.parser import PLTParser
from plt_optimizer.generate.relief.arcify import fit_circular_runs
from plt_optimizer.generate.text_geometry import ArcSeg, LineSeg, Stroke

U = 1016.0  # plotter units per inch


def _circle_chain(cx: float, cy: float, r: float, a0: float, a1: float, n: int):
    return [
        (cx + r * math.cos(t), cy + r * math.sin(t))
        for t in [a0 + (a1 - a0) * i / n for i in range(n + 1)]
    ]


class TestFitCircularRuns:
    """Run grouping, circle fit, chain closure."""

    def test_straight_line_all_lines(self) -> None:
        pts = [(x / U, 0.0) for x in range(0, 1017, 100)]
        segs = fit_circular_runs(pts)
        assert segs
        assert all(isinstance(s, LineSeg) for s in segs)

    def test_capsule_ring_is_two_arcs_plus_two_lines(self) -> None:
        # Engraved-stroke island silhouette: the "A" leg tip cap scenario.
        capsule = LineString([(0.1, 0.5), (0.9, 0.5)]).buffer(0.05, quad_segs=32)
        ring = list(capsule.exterior.coords)
        segs = fit_circular_runs(ring)
        arcs = [s for s in segs if isinstance(s, ArcSeg)]
        lines = [s for s in segs if isinstance(s, LineSeg)]
        assert len(arcs) == 2, [round(getattr(s, "sweep_deg", 0), 1) for s in segs]
        assert len(lines) == 2
        for arc in arcs:
            assert abs(arc.radius - 0.05) < 5e-4
            assert abs(abs(arc.sweep_deg) - 180.0) < 1.0

    def test_minor_arc_ring(self) -> None:
        # 45-degree arc closed by two radii = a pie slice ring.
        pts = _circle_chain(0.5, 0.5, 0.25, 0.0, math.pi / 4.0, 12)
        pts = pts + [(0.5, 0.5), pts[0]]
        segs = fit_circular_runs(pts)
        arcs = [s for s in segs if isinstance(s, ArcSeg)]
        assert len(arcs) == 1
        arc = arcs[0]
        assert math.dist((arc.center.real, arc.center.imag), (0.5, 0.5)) < 5e-4
        assert abs(arc.radius - 0.25) < 1e-3
        assert abs(abs(arc.sweep_deg) - 45.0) < 1.5

    def test_sharp_miter_corner_stays_lines(self) -> None:
        # A lone 90-degree vertex (3 points, 1 turn) must NOT arcify.
        pts = [(0.0, 0.0), (0.5, 0.0), (0.5, 0.5)]
        segs = fit_circular_runs(pts)
        assert all(isinstance(s, LineSeg) for s in segs)

    def test_rounded_square_ring(self) -> None:
        # The production fillet construction (pipeline._cores): erode
        # (shapely mitres the inset corners SHARP), then subtract a disc
        # of the cutter radius at each inset corner -- exactly the
        # reference's ``AA30.480,985.520`` ring corners.
        square = Polygon([(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)])
        core = square.buffer(-0.05, quad_segs=256, join_style=1)
        for cx, cy in ((0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75)):
            core = core.difference(Point(cx, cy).buffer(0.05, quad_segs=256, join_style=1))
        ring = list(core.exterior.coords)
        segs = fit_circular_runs(ring)
        arcs = [s for s in segs if isinstance(s, ArcSeg)]
        lines = [s for s in segs if isinstance(s, LineSeg)]
        assert len(arcs) == 4, f"expected 4 corner arcs, got {len(arcs)}"
        assert len(lines) >= 4  # tangency slivers split the straight runs
        for arc in arcs:
            assert abs(arc.radius - 0.05) < 1e-3
            assert abs(abs(arc.sweep_deg) - 90.0) < 2.0

    def test_closed_chain_returns_to_start(self) -> None:
        square = Polygon([(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)])
        rounded = square.buffer(-0.05, quad_segs=64).buffer(0.05, quad_segs=64)
        ring = list(rounded.exterior.coords)
        segs = fit_circular_runs(ring)
        x, y = ring[0]
        pos = complex(x, y)
        for seg in segs:
            assert math.isclose(abs(seg.start - pos), 0.0, abs_tol=1e-12)
            pos = seg.end
        start = complex(ring[0][0], ring[0][1])
        assert abs(pos - start) < 5e-4

    def test_full_circle_ring(self) -> None:
        disc = Point(0.5, 0.5).buffer(0.1, quad_segs=128)
        ring = list(disc.exterior.coords)
        segs = fit_circular_runs(ring)
        arcs = [s for s in segs if isinstance(s, ArcSeg)]
        assert len(arcs) == 1
        assert abs(abs(arcs[0].sweep_deg) - 360.0) < 1.0


class TestStrokeEmission:
    """Fit -> Stroke -> to_hpgl -> core parse round trip (the emitted form)."""

    def test_arc_emits_pd_aa_and_parses_back(self) -> None:
        pts = _circle_chain(0.5, 0.5, 0.1, 0.0, math.pi / 2.0, 24)
        stroke = Stroke(pen_up=complex(*pts[0]), segments=tuple(fit_circular_runs(pts)))
        hpgl = stroke.to_hpgl()
        assert "PD;AA" in hpgl
        text = f"IN;PA;SP1;{hpgl};SP;\n"
        path = Path(__import__("tempfile").mkdtemp()) / "a.plt"
        path.write_text(text, encoding="utf-8", newline="\n")
        doc = PLTParser().parse_file(path)
        arcs = [
            seg for sp in doc.stroke_paths for seg in sp.segments if isinstance(seg, ArcSegment)
        ]
        assert len(arcs) == 1
        assert math.isclose(arcs[0].center.x, 0.5 * U, abs_tol=1.0)
        assert math.isclose(arcs[0].center.y, 0.5 * U, abs_tol=1.0)
        assert abs(arcs[0].sweep_angle) > 80.0
