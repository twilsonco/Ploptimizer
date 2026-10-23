"""Parity tests against the EngraveLab horizontal-compression reference exports.

The fixtures in ``tests_deps/arc_compression/`` are Cadlink EngraveLab Expert
v10 exports of the curved capitals O/C/S/8 at three horizontal-compression
settings. They are the ground truth for the arc-preserving non-uniform
transforms (``arc_fit`` + ``TextBlock.compress_x``): they pin *what* the target
geometry is and *how* a commercial plotter package keeps arcs through the same
operation. See ``tests_deps/arc_compression/README.md`` for the decoded
parameters.

Two contracts, both measured on the reference data alone:

1. **The target is the exact affine image.** The two-sided Hausdorff distance
   between ``scale_x(uncompressed, s)`` and the reference's compressed outline
   is <= 0.02in, so compressing a glyph means scaling its geometry -- there is
   no hidden re-typesetting.
2. **Arcs stay arcs.** EngraveLab's ``AA`` count grows by at most 1.25x under
   compression; it never polygonizes.

A third class runs OUR fitter over the reference's uncompressed arcs and
compares the emitted chain to the reference's compressed outline, so the
acceptance bar is "as close to EngraveLab's engraved shape as EngraveLab is".

Per-arc index pairing is deliberately NOT asserted -- EngraveLab re-tessellates
the whole outline, so these files are compared **as curves**, never arc-by-arc.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import List, Sequence, Tuple

import pytest

from plt_optimizer.core.models import ArcSegment, StrokeSegment
from plt_optimizer.core.parser import PLTParser
from plt_optimizer.core.units import units_to_inches
from plt_optimizer.generate.arc_fit import ellipse_from_arc, fit_arc

FIXTURES = Path(__file__).resolve().parents[1] / "tests_deps" / "arc_compression"

# The X-scale anchor: the text-box left edge, identical in all twelve files.
ANCHOR_UNITS = 1373.213

# Envelope for "the compressed outline is the affine image of the plain one".
# Measured worst case is 8 s=0.5 at 0.0124in; 0.02in leaves room for the
# reference's own loose fit without admitting a different geometry model.
AFFINE_IMAGE_TOLERANCE_INCHES = 0.02

# EngraveLab's arc count grows by at most this factor under compression.
MAX_ARC_GROWTH = 1.25

GLYPHS = ("O", "C", "S", "8")
SETTINGS = (("0.1", 0.9), ("0.5", 0.5))

Point = Tuple[float, float]


def _parse(name: str) -> Tuple[List[object], List[Point]]:
    """Return the reference file's segments (device units) and dense outline.

    Uses the core parser -- the same one the optimize pipeline runs on
    customer PLTs -- so the fixtures are exercised through real parsing, not a
    test-local regex.
    """
    doc = PLTParser().parse_file(FIXTURES / name)
    segments = [seg for path in doc.stroke_paths for seg in path.segments]
    return segments, _dense(segments)


def _arc_end(start: Point, center: Point, sweep_deg: float) -> Point:
    radius = math.hypot(start[0] - center[0], start[1] - center[1])
    theta = math.atan2(start[1] - center[1], start[0] - center[0]) + math.radians(sweep_deg)
    return (center[0] + radius * math.cos(theta), center[1] + radius * math.sin(theta))


def _dense(segments: Sequence[object], per_degree: float = 2.0) -> List[Point]:
    """Sample the engraved outline densely (arcs included), in device units."""
    pts: List[Point] = []
    for seg in segments:
        if isinstance(seg, StrokeSegment):
            pts.append((seg.start.x, seg.start.y))
            pts.append((seg.end.x, seg.end.y))
        elif isinstance(seg, ArcSegment):
            start = (seg.start.x, seg.start.y)
            center = (seg.center.x, seg.center.y)
            radius = seg.radius
            t0 = math.atan2(start[1] - center[1], start[0] - center[0])
            n = max(2, int(abs(seg.sweep_angle) * per_degree))
            for i in range(n + 1):
                t = t0 + math.radians(seg.sweep_angle) * i / n
                pts.append((center[0] + radius * math.cos(t), center[1] + radius * math.sin(t)))
    return pts


def _one_way(src: Sequence[Point], dst: Sequence[Point]) -> float:
    """Max distance (inches) from each point of ``src`` to the nearest in ``dst``."""
    worst = 0.0
    for px, py in src:
        best = math.inf
        for qx, qy in dst:
            d = math.hypot(px - qx, py - qy)
            if d < best:
                best = d
        worst = max(worst, best)
    return units_to_inches(worst)


def _hausdorff(a: Sequence[Point], b: Sequence[Point]) -> float:
    """Two-sided max nearest distance, in inches."""
    return max(_one_way(a, b), _one_way(b, a))


def _scale_x(points: Sequence[Point], scale: float) -> List[Point]:
    return [(ANCHOR_UNITS + (x - ANCHOR_UNITS) * scale, y) for x, y in points]


def _width(points: Sequence[Point]) -> float:
    xs = [p[0] for p in points]
    return max(xs) - min(xs)


def _arc_count(segments: Sequence[object]) -> int:
    return sum(1 for seg in segments if isinstance(seg, ArcSegment))


class TestReferenceDecoding:
    """The decoded fixture parameters the rest of the module relies on."""

    @pytest.mark.parametrize("glyph", GLYPHS)
    def test_y_extent_is_untouched(self, glyph: str) -> None:
        """Compression is X-only: every setting keeps the glyph's height."""
        _s, base = _parse(f"{glyph}_no-compress.plt")
        for setting, _scale in SETTINGS:
            _s2, comp = _parse(f"{glyph}_{setting}-x-compress.plt")
            base_h = max(p[1] for p in base) - min(p[1] for p in base)
            comp_h = max(p[1] for p in comp) - min(p[1] for p in comp)
            # S loses ~5 units of height to EngraveLab's own coarse fit.
            assert comp_h == pytest.approx(base_h, abs=12.0), f"{glyph} {setting}"

    @pytest.mark.parametrize("glyph", GLYPHS)
    def test_filename_setting_decodes_to_the_measured_scale(self, glyph: str) -> None:
        """`0.1` -> s=0.9 and `0.5` -> s=0.5, measured from the x-extents."""
        _s, base = _parse(f"{glyph}_no-compress.plt")
        for setting, expected in SETTINGS:
            _s2, comp = _parse(f"{glyph}_{setting}-x-compress.plt")
            measured = _width(comp) / _width(base)
            assert measured == pytest.approx(expected, abs=0.02), f"{glyph} {setting}"


class TestAffineImageContract:
    """Contract 1: the compressed outline is the affine image of the plain one."""

    @pytest.mark.parametrize("glyph", GLYPHS)
    @pytest.mark.parametrize("setting,scale", SETTINGS)
    def test_compressed_is_the_affine_image(
        self, glyph: str, setting: str, scale: float
    ) -> None:
        _s, base = _parse(f"{glyph}_no-compress.plt")
        _s2, comp = _parse(f"{glyph}_{setting}-x-compress.plt")
        deviation = _hausdorff(_scale_x(base, scale), comp)
        assert deviation <= AFFINE_IMAGE_TOLERANCE_INCHES, (
            f"{glyph} at s={scale}: EngraveLab's compressed outline sits "
            f"{deviation:.5f}in from the affine image of the plain one"
        )


class TestArcsStayArcsContract:
    """Contract 2: a commercial plotter package keeps arcs through compression."""

    @pytest.mark.parametrize("glyph", GLYPHS)
    @pytest.mark.parametrize("setting,scale", SETTINGS)
    def test_arc_count_barely_grows(self, glyph: str, setting: str, scale: float) -> None:
        base, _pts = _parse(f"{glyph}_no-compress.plt")
        comp, _pts2 = _parse(f"{glyph}_{setting}-x-compress.plt")
        base_arcs, comp_arcs = _arc_count(base), _arc_count(comp)
        assert base_arcs > 0, f"{glyph} has no native arcs to preserve"
        # Polygonizing would multiply the command count by ~2.8x; EngraveLab
        # refits, so its count grows by at most a quarter.
        assert comp_arcs <= base_arcs * MAX_ARC_GROWTH, (
            f"{glyph} at s={scale}: {base_arcs} arcs became {comp_arcs} "
            f"(> {MAX_ARC_GROWTH}x), i.e. closer to polygonizing than refitting"
        )
        # And it never collapses the arc count into line segments.
        comp_lines = sum(1 for seg in comp if isinstance(seg, StrokeSegment))
        assert comp_lines < comp_arcs, f"{glyph} at s={scale}: polygonized"


class TestOurFitterMatchesReference:
    """Our refit lands on EngraveLab's engraved shape.

    Runs :func:`fit_arc` over the reference's own uncompressed arcs, applies
    the decoded X scale, and compares the emitted chain to the reference's
    compressed outline as curves. The bar is the same envelope contract 1 puts
    on EngraveLab: our output must be within
    :data:`AFFINE_IMAGE_TOLERANCE_INCHES` of the affine image, hence of the
    reference's shape to twice that.
    """

    @pytest.mark.parametrize("glyph", GLYPHS)
    @pytest.mark.parametrize("setting,scale", SETTINGS)
    def test_fitted_chain_matches_the_affine_image(
        self, glyph: str, setting: str, scale: float
    ) -> None:
        segments, _base_pts = _parse(f"{glyph}_no-compress.plt")

        fitted: List[Point] = []
        arcs = [seg for seg in segments if isinstance(seg, ArcSegment)]
        assert arcs, f"{glyph} has no arcs"
        for seg in arcs:
            center = complex(*map(units_to_inches, (seg.center.x, seg.center.y)))
            start = complex(*map(units_to_inches, (seg.start.x, seg.start.y)))
            radius = units_to_inches(seg.radius)
            t0 = math.degrees(math.atan2(start.imag - center.imag, start.real - center.real))
            anchor = units_to_inches(ANCHOR_UNITS)
            chain = fit_arc(
                center, radius, t0, seg.sweep_angle, scale_x=scale, anchor_x=anchor
            )
            assert chain is not None, f"{glyph} at s={scale}: refused to fit a real arc"
            for piece in chain:
                if piece.is_arc:
                    fitted.extend(
                        _dense(
                            [
                                ArcSegment(
                                    start=_coord(piece.start),
                                    end=_coord(piece.end),
                                    center=_coord(piece.center),
                                    sweep_angle=piece.sweep_deg,
                                    is_cutting=True,
                                )
                            ]
                        )
                    )
                else:
                    fitted.append(_inches(piece.start))
                    fitted.append(_inches(piece.end))

        # Compare in inches against the reference's compressed outline.
        _comp_segments, comp_pts = _parse(f"{glyph}_{setting}-x-compress.plt")
        comp_in = [(units_to_inches(x), units_to_inches(y)) for x, y in comp_pts]
        deviation = _hausdorff(fitted, comp_in)
        assert deviation <= 2.0 * AFFINE_IMAGE_TOLERANCE_INCHES, (
            f"{glyph} at s={scale}: our fitted chain sits {deviation:.5f}in from "
            f"EngraveLab's compressed outline"
        )


def _coord(z: complex) -> object:
    from plt_optimizer.core.models import Coordinate

    return Coordinate(z.real, z.imag)


def _inches(z: complex) -> Point:
    return (z.real, z.imag)
