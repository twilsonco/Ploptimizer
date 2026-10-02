"""Tests for the arc-native text geometry model (``text_geometry``).

Covers the inch-space geometry primitives that let PLT-extracted fonts keep
their native ``AA`` arcs end-to-end: swept bounds, the affine transform
chain (translate / uniform scale / Y-mirror / 90 CW rotation), HPGL emission
framing (parser round-trip + historical polyline bit-parity), and the one
lossy operation (horizontal compression flattening arcs).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest
import vpype as vp

from plt_optimizer.core.parser import PLTParser
from plt_optimizer.generate import text_geometry
from plt_optimizer.generate.text_geometry import (
    ARC_FLATTEN_CHORD_TOL_INCHES,
    ArcSeg,
    LineSeg,
    Stroke,
    TextBlock,
    _units,
    arc_end_point,
    arc_flatten_segments,
    arc_start_angle,
    block_from_linecollection,
    block_from_parser_paths,
    flatten_arc_to_polyline,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _quarter_arc() -> ArcSeg:
    """Unit quarter arc: start (1,0) about origin, +90 deg -> (0,1)."""
    return ArcSeg(start=1 + 0j, center=0j, sweep_deg=90.0)


class TestArcHelpers:
    """Tests for the arc math helpers."""

    def test_arc_end_point_quarter(self) -> None:
        """A +90 deg sweep from (1,0) about the origin lands on (0,1)."""
        end = arc_end_point(1 + 0j, 0j, 90.0)
        assert math.isclose(end.real, 0.0, abs_tol=1e-12)
        assert math.isclose(end.imag, 1.0, abs_tol=1e-12)

    def test_arc_end_point_negative_sweep(self) -> None:
        """A -90 deg sweep lands on (0,-1)."""
        end = arc_end_point(1 + 0j, 0j, -90.0)
        assert math.isclose(end.real, 0.0, abs_tol=1e-12)
        assert math.isclose(end.imag, -1.0, abs_tol=1e-12)

    def test_arc_end_point_zero_sweep_is_start(self) -> None:
        """A zero sweep leaves the point untouched."""
        assert arc_end_point(3 + 4j, 1 + 1j, 0.0) == pytest.approx(3 + 4j)

    def test_arc_start_angle_roundtrip(self) -> None:
        """start angle + end point agree with the arc's own geometry."""
        start, center = 0 + 2j, 0j
        angle = arc_start_angle(start, center)
        assert math.isclose(angle, 90.0, abs_tol=1e-9)
        assert arc_end_point(start, center, 45) == pytest.approx(
            center + 2 * complex(math.cos(math.radians(135)), math.sin(math.radians(135))),
            abs=1e-12,
        )

    def test_flatten_segment_count_degenerate(self) -> None:
        """Zero sweep or radius needs a single chord."""
        assert arc_flatten_segments(0.0, 1.0) == 1
        assert arc_flatten_segments(90.0, 0.0) == 1

    def test_flatten_segment_count_refines_with_radius(self) -> None:
        """A huge radius needs many more chords than a tiny one."""
        small = arc_flatten_segments(90.0, 0.01)
        huge = arc_flatten_segments(90.0, 5.0)
        assert huge > small

    def test_flatten_segment_count_capped(self) -> None:
        """Absurd sweeps stay inside the tessellation cap."""
        assert arc_flatten_segments(3600000.0, 100.0) <= 4096

    def test_flatten_arc_chord_error_bounded(self) -> None:
        """Every flattened vertex sits within the chord tolerance of the arc."""
        center, start, sweep = 0j, 4 + 0j, 60.0
        points = flatten_arc_to_polyline(start, center, sweep)
        assert points[0] == start
        assert points[-1] == pytest.approx(arc_end_point(start, center, sweep), abs=1e-12)
        radius = abs(start - center)
        for point in points:
            # Chord points fall slightly INSIDE the circle; the radial error
            # is the sagitta and must respect the tolerance.
            assert radius - abs(point - center) <= ARC_FLATTEN_CHORD_TOL_INCHES + 1e-12

    def test_units_conversion(self) -> None:
        """Inches convert to integer plotter units with rounding."""
        assert _units(1.0) == 1000
        assert _units(0.0004) == 0
        assert _units(-0.0006) == -1


class TestLineSeg:
    """Tests for the straight-segment primitive."""

    def test_bounds(self) -> None:
        """Bounds span the two endpoints."""
        assert LineSeg(0j, 2 + 4j).bounds() == (0.0, 0.0, 2.0, 4.0)

    def test_translate_scale_mirror(self) -> None:
        """Affine helpers return new segments without mutating."""
        seg = LineSeg(0j, 2 + 4j)
        assert seg.translate(1.0, -1.0) == LineSeg(1 - 1j, 3 + 3j)
        assert seg.scaled(2.0) == LineSeg(0j, 4 + 8j)
        assert seg.mirrored_y(4.0) == LineSeg(0 + 4j, 2 + 0j)

    def test_flattened_returns_endpoints(self) -> None:
        """A line flattens to its own two endpoints."""
        assert LineSeg(1 + 1j, 2 + 2j).flattened() == [1 + 1j, 2 + 2j]


class TestArcSeg:
    """Tests for the arc primitive."""

    def test_end_and_radius(self) -> None:
        """Derived end and radius agree with the helpers."""
        arc = _quarter_arc()
        assert arc.end == pytest.approx(0 + 1j, abs=1e-12)
        assert arc.radius == pytest.approx(1.0)

    def test_bounds_are_swept_not_full_circle(self) -> None:
        """A quarter arc in the first quadrant never reaches negative coords."""
        x_min, y_min, x_max, y_max = _quarter_arc().bounds()
        assert x_min == pytest.approx(0.0, abs=1e-12)
        assert y_min == pytest.approx(0.0, abs=1e-12)
        assert x_max == pytest.approx(1.0, abs=1e-12)
        assert y_max == pytest.approx(1.0, abs=1e-12)

    def test_bounds_huge_radius_shallow_arc(self) -> None:
        """A shallow huge-radius arc stays tight (the glyph-stroke case).

        EngraveLab approximates near-straight strokes with arcs whose full
        circle dwarfs the cut; swept bounds must report the chord's box, not
        the circle's.
        """
        # Center far below, tiny sweep near the top of a huge circle.
        radius = 4000.0
        arc = ArcSeg(start=0 + radius * 1j, center=0j, sweep_deg=1.0)
        x_min, y_min, x_max, y_max = arc.bounds()
        assert y_max - y_min < 1.0  # ~1 deg of a 4000 in circle, not 8000 tall
        assert y_max <= radius + 1e-9

    def test_translate_preserves_sweep(self) -> None:
        """Translation moves start/center and keeps the sweep."""
        moved = _quarter_arc().translate(2.0, 3.0)
        assert moved.center == pytest.approx(2 + 3j)
        assert moved.sweep_deg == 90.0

    def test_scale_preserves_sweep(self) -> None:
        """Uniform scaling resizes and keeps the sweep."""
        scaled = _quarter_arc().scaled(2.0)
        assert scaled.radius == pytest.approx(2.0)
        assert scaled.sweep_deg == 90.0

    def test_mirror_negates_sweep(self) -> None:
        """Y-mirroring negates the sweep and mirrors start/center."""
        mirrored = _quarter_arc().mirrored_y(0.0)
        assert mirrored.sweep_deg == -90.0
        assert mirrored.start == pytest.approx(1 + 0j)
        assert mirrored.end == pytest.approx(0 - 1j, abs=1e-12)

    def test_mirror_then_mirror_is_identity(self) -> None:
        """Two mirrors across the same span restore the arc exactly."""
        arc = ArcSeg(start=1 + 2j, center=3 - 1j, sweep_deg=37.5)
        twice = arc.mirrored_y(5.0).mirrored_y(5.0)
        assert twice.start == pytest.approx(arc.start, abs=1e-12)
        assert twice.center == pytest.approx(arc.center, abs=1e-12)
        assert twice.sweep_deg == pytest.approx(arc.sweep_deg)

    def test_flattened_end_matches_derived_end(self) -> None:
        """Flattening terminates exactly at the derived end point."""
        arc = ArcSeg(start=2 + 0j, center=0j, sweep_deg=-45.0)
        assert flatten_arc_to_polyline(arc.start, arc.center, arc.sweep_deg)[-1] == (
            pytest.approx(arc.end, abs=1e-12)
        )


class TestStroke:
    """Tests for the pen-up-led stroke container."""

    def test_empty_stroke(self) -> None:
        """A stroke without segments is empty with no bounds or output."""
        stroke = Stroke(pen_up=1 + 1j, segments=())
        assert stroke.is_empty
        assert stroke.bounds() is None
        assert stroke.to_hpgl() == ""

    def test_bounds_union(self) -> None:
        """Stroke bounds union every segment."""
        stroke = Stroke(pen_up=0j, segments=(LineSeg(0j, 1 + 1j), LineSeg(1 + 1j, 3 + 0j)))
        assert stroke.bounds() == (0.0, 0.0, 3.0, 1.0)

    def test_polyline_chains_split_on_arcs(self) -> None:
        """A line-arc-line stroke yields three faithful chains."""
        stroke = Stroke(
            pen_up=0j,
            segments=(
                LineSeg(0j, 1 + 0j),
                ArcSeg(start=1 + 0j, center=1 + 1j, sweep_deg=90.0),
                LineSeg(2 + 1j, 3 + 1j),
            ),
        )
        chains = stroke.polyline_chains()
        assert len(chains) == 3
        assert chains[0] == [0j, 1 + 0j]
        assert chains[2] == [2 + 1j, 3 + 1j]
        assert chains[1][-1] == pytest.approx(stroke.segments[1].end, abs=1e-12)

    def test_to_linecollection_drops_degenerate(self) -> None:
        """Single-point chains never reach the LineCollection view."""
        stroke = Stroke(pen_up=0j, segments=(LineSeg(0j, 1 + 1j),))
        lc = stroke.to_linecollection()
        lines = list(lc)
        assert len(lines) == 1
        assert len(lines[0]) == 2

    def test_translate_scale_mirror(self) -> None:
        """Affine helpers map pen_up and every segment."""
        stroke = Stroke(pen_up=0j, segments=(LineSeg(0j, 1 + 1j),))
        assert stroke.translate(1.0, 2.0).pen_up == pytest.approx(1 + 2j)
        assert stroke.scaled(3.0).segments[0].end == pytest.approx(3 + 3j)
        assert stroke.mirrored_y(2.0).segments[0].end == pytest.approx(1 + 1j)


class TestStrokeHpgl:
    """HPGL emission framing (parser round-trip + historical parity)."""

    def test_polyline_matches_historical_framing(self) -> None:
        """A pure polyline stroke emits the historical ``PU{first};PD{rest}``."""
        stroke = Stroke(pen_up=0j, segments=(LineSeg(0j, 1 + 1j), LineSeg(1 + 1j, 2 + 0j)))
        assert stroke.to_hpgl() == "PU0,0;PD1000,1000,2000,0"

    def test_arc_emits_plunge_form(self) -> None:
        """An arc emits ``PU{start};PD;AA{cx},{cy},{sweep}`` with decimal sweep."""
        stroke = Stroke(pen_up=1 + 0j, segments=(_quarter_arc(),))
        assert stroke.to_hpgl() == "PU1000,0;PD;AA0,0,90.000"

    def test_arc_after_line_chains_without_extra_pu(self) -> None:
        """An arc starting where the pen rests needs no extra PU."""
        arc = ArcSeg(start=2 + 0j, center=2 + 1j, sweep_deg=45.0)
        stroke = Stroke(pen_up=0j, segments=(LineSeg(0j, 2 + 0j), arc))
        hpgl = stroke.to_hpgl()
        assert hpgl == "PU0,0;PD2000,0;PD;AA2000,1000,45.000"

    def test_repositioned_arc_gets_pu(self) -> None:
        """An arc whose start is elsewhere is preceded by a rapid PU."""
        stroke = Stroke(pen_up=0j, segments=(ArcSeg(start=5 + 5j, center=5 + 6j, sweep_deg=10),))
        assert stroke.to_hpgl() == "PU0,0;PU5000,5000;PD;AA5000,6000,10.000"

    def test_line_after_arc_replays_start(self) -> None:
        """A line run following an arc replays its start (pen moved by the arc)."""
        arc = _quarter_arc()  # ends at 0+1j
        stroke = Stroke(
            pen_up=1 + 0j,
            segments=(arc, LineSeg(arc.end, 5 + 5j)),
        )
        hpgl = stroke.to_hpgl()
        assert hpgl.endswith(";PD5000,5000")

    def test_roundtrip_through_parser(self) -> None:
        """Emitted geometry re-parses to the same swept bounds."""
        doc = PLTParser().parse_string(
            "IN;PA;PU0,0;PD1000,0;PD;AA1000,1000,-90.000;PU3000,0;PD4000,1000;SP;"
        )
        block = block_from_parser_paths(doc.stroke_paths, scale=1 / 1000)
        reparsed = PLTParser().parse_string("IN;PA;" + block.to_hpgl() + ";SP;")
        block2 = block_from_parser_paths(reparsed.stroke_paths, scale=1 / 1000)
        assert block.bounds() == pytest.approx(block2.bounds(), abs=1e-9)
        assert len(block2.strokes) == 2


class TestTextBlock:
    """Tests for the block-level container and its transforms."""

    @staticmethod
    def _sample() -> TextBlock:
        """A block mixing a polyline stroke and an arc stroke."""
        return TextBlock(
            strokes=(
                Stroke(pen_up=0j, segments=(LineSeg(0j, 2 + 0j),)),
                Stroke(pen_up=2 + 0j, segments=(_quarter_arc().translate(2.0, 0.0),)),
            )
        )

    def test_empty_block(self) -> None:
        """An empty block reports empty and bounds-less."""
        block = TextBlock.empty()
        assert block.is_empty()
        assert block.bounds() is None
        assert block.to_hpgl() == ""

    def test_bounds_union_across_strokes(self) -> None:
        """Block bounds union polyline and swept-arc extents."""
        x_min, y_min, x_max, y_max = self._sample().bounds()  # type: ignore[union-attr]
        assert (x_min, y_min) == (0.0, 0.0)
        assert x_max == pytest.approx(3.0, abs=1e-12)  # arc center 2 + radius 1
        assert y_max == pytest.approx(1.0, abs=1e-12)

    def test_is_empty_ignores_strokes_without_segments(self) -> None:
        """Strokes without segments do not make a block non-empty."""
        assert TextBlock(strokes=(Stroke(pen_up=0j, segments=()),)).is_empty()

    def test_translate_and_scaled(self) -> None:
        """Affine helpers map the whole block."""
        block = self._sample()
        assert block.translate(1.0, 1.0).bounds()[0] == pytest.approx(1.0)  # type: ignore[index]
        assert block.scaled(2.0).bounds()[2] == pytest.approx(6.0, abs=1e-12)  # type: ignore[index]

    def test_rotated_90cw_swaps_dimensions(self) -> None:
        """The 90 CW rotation matches ``rotate_plt_content_90cw`` semantics."""
        block = self._sample()
        bounds = block.bounds()
        assert bounds is not None
        rotated = block.rotated_90cw(bounds[0], bounds[3])
        rb = rotated.bounds()
        assert rb is not None
        assert (rb[0], rb[1]) == pytest.approx((0.0, 0.0), abs=1e-12)
        assert (rb[2] - rb[0]) == pytest.approx(bounds[3] - bounds[1], abs=1e-12)
        assert (rb[3] - rb[1]) == pytest.approx(bounds[2] - bounds[0], abs=1e-12)

    def test_rotated_arc_sweep_preserved(self) -> None:
        """Rotation (positive determinant) leaves arc sweeps verbatim."""
        block = self._sample()
        arc = block.strokes[1].segments[0]
        rotated_arc = block.rotated_90cw(0.0, 1.0).strokes[1].segments[0]
        assert isinstance(arc, ArcSeg) and isinstance(rotated_arc, ArcSeg)
        assert rotated_arc.sweep_deg == arc.sweep_deg

    def test_mirrored_y_negates_arc_sweep(self) -> None:
        """The block-level mirror propagates the sweep negation."""
        block = self._sample()
        arc = block.strokes[1].segments[0]
        mirrored = block.mirrored_y(1.0).strokes[1].segments[0]
        assert isinstance(arc, ArcSeg) and isinstance(mirrored, ArcSeg)
        assert mirrored.sweep_deg == -arc.sweep_deg

    def test_to_hpgl_joins_strokes(self) -> None:
        """Block output is the semicolon-joined strokes."""
        block = self._sample()
        hpgl = block.to_hpgl()
        assert hpgl.startswith("PU0,0;")
        assert "PD;AA2000,0,90.000" in hpgl

    def test_to_linecollection_flattens_arcs(self) -> None:
        """The vpype view keeps one polyline per pen-down run."""
        lc = self._sample().to_linecollection()
        assert not lc.is_empty()
        for line in lc:
            assert len(line) >= 2

    def test_to_vpype_polylines_one_polyline_per_stroke(self) -> None:
        """The legacy view chains a stroke's vertices into a single polyline."""
        stroke = Stroke(
            pen_up=0j,
            segments=(LineSeg(0j, 1 + 0j), _quarter_arc().translate(1.0, 0.0), LineSeg(2 + 1j, 3 + 1j)),
        )
        block = TextBlock(strokes=(stroke,))
        lc = block.to_vpype_polylines()
        lines = list(lc)
        assert len(lines) == 1
        points = np.asarray(lines[0])
        assert points[0] == pytest.approx(0j)
        assert points[-1] == pytest.approx(3 + 1j)


class TestCompressX:
    """Horizontal compression is the one lossy operation."""

    def test_noop_scale(self) -> None:
        """Scale >= 1 returns the identical block."""
        block = TextBlock(strokes=(Stroke(pen_up=0j, segments=(LineSeg(0j, 1 + 1j),)),))
        assert block.compress_x(1.0) is block

    def test_empty_block_noop(self) -> None:
        """A bounds-less block compresses to itself."""
        block = TextBlock.empty()
        assert block.compress_x(0.5) is block

    def test_lines_scale_exactly(self) -> None:
        """Polyline X spans scale by the factor about the left edge."""
        block = TextBlock(strokes=(Stroke(pen_up=0j, segments=(LineSeg(0j, 4 + 2j),)),))
        compressed = block.compress_x(0.5)
        assert compressed.bounds() == pytest.approx((0.0, 0.0, 2.0, 2.0))

    def test_arcs_flattened_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Arc-bearing lines flatten to chords and log a WARNING."""
        block = TextBlock(strokes=(Stroke(pen_up=1 + 0j, segments=(_quarter_arc(),)),))
        with caplog.at_level("WARNING"):
            compressed = block.compress_x(0.5, context="label_1 line 0")
        assert all(isinstance(seg, LineSeg) for seg in compressed.strokes[0].segments)
        assert "flattened" in caplog.text
        assert "label_1 line 0" in caplog.text

    def test_arcs_flattened_without_context(self, caplog: pytest.LogCaptureFixture) -> None:
        """The WARNING works without a context string."""
        block = TextBlock(strokes=(Stroke(pen_up=1 + 0j, segments=(_quarter_arc(),)),))
        with caplog.at_level("WARNING"):
            block.compress_x(0.5)
        assert "flattened" in caplog.text


class TestBlockFromLineCollection:
    """Adapter from the polyline-only vpype representation."""

    def test_empty_collection(self) -> None:
        """An empty collection yields an empty block."""
        assert block_from_linecollection(vp.LineCollection()).is_empty()

    def test_short_lines_skipped(self) -> None:
        """Single-point lines cannot form a segment and are dropped."""
        lc = vp.LineCollection()
        lc.append(np.array([1 + 1j], dtype=complex))
        lc.append(np.array([0j, 1 + 1j], dtype=complex))
        block = block_from_linecollection(lc)
        assert len(block.strokes) == 1

    def test_roundtrip_preserves_vertices(self) -> None:
        """Vertices survive block -> HPGL -> re-parse."""
        lc = vp.LineCollection()
        lc.append(np.array([0j, 1 + 2j, 3 + 1j], dtype=complex))
        block = block_from_linecollection(lc)
        reparsed = PLTParser().parse_string("IN;PA;" + block.to_hpgl() + ";SP;")
        block2 = block_from_parser_paths(reparsed.stroke_paths, scale=1 / 1000)
        assert block2.bounds() == pytest.approx(block.bounds(), abs=1e-9)


class TestBlockFromParserPaths:
    """Adapter from core parser output (arc-preserving)."""

    def test_skips_paths_without_segments(self) -> None:
        """Pen-up-only paths never reach the block."""
        doc = PLTParser().parse_string("IN;PA;PU100,100;PD200,200;SP;")
        block = block_from_parser_paths(doc.stroke_paths, scale=1 / 1000)
        assert len(block.strokes) == 1

    def test_pen_up_defaults_to_first_segment_start(self) -> None:
        """A path without an explicit pen-up uses its first vertex."""
        doc = PLTParser().parse_string("IN;PA;PU0,0;PD1000,0;SP;")
        block = block_from_parser_paths(doc.stroke_paths, scale=1 / 1000)
        assert block.strokes[0].pen_up == pytest.approx(0j)

    def test_scale_converts_units(self) -> None:
        """The scale maps plotter units to inches."""
        doc = PLTParser().parse_string("IN;PA;PU0,0;PD1000,2000;SP;")
        block = block_from_parser_paths(doc.stroke_paths, scale=1 / 1000)
        assert block.bounds() == pytest.approx((0.0, 0.0, 1.0, 2.0))


@pytest.mark.parametrize("char", ["A", "0", "S", "%", "&", "1"])
def test_real_glyphs_roundtrip(char: str) -> None:
    """Every Dino glyph survives parse -> block -> emit -> re-parse.

    Swept bounds must agree within the integer plotter-unit quantization
    (0.001 in per coordinate, so a fraction of that for bounds computed
    from many vertices).
    """
    fonts = json.loads((REPO_ROOT / "Fonts" / "plt_fonts.json").read_text(encoding="utf-8"))
    glyph = fonts["Dino"][char]
    doc = PLTParser().parse_string("IN;PA;" + glyph + "SP;")
    block = block_from_parser_paths(doc.stroke_paths, scale=1 / 1000)
    bounds = block.bounds()
    assert bounds is not None

    reparsed = PLTParser().parse_string("IN;PA;" + block.to_hpgl() + ";SP;")
    block2 = block_from_parser_paths(reparsed.stroke_paths, scale=1 / 1000)
    assert block2.bounds() == pytest.approx(bounds, abs=0.002)


def test_module_exports() -> None:
    """The public surface exists (guards accidental renames)."""
    assert text_geometry.ARC_FLATTEN_CHORD_TOL_INCHES > 0
    for name in (
        "ArcSeg",
        "LineSeg",
        "Stroke",
        "TextBlock",
        "arc_end_point",
        "block_from_linecollection",
        "block_from_parser_paths",
    ):
        assert hasattr(text_geometry, name)
