"""Tests for ``Fonts/extract_plt_fonts.py`` (PLT font reverse-engineering).

The script is not part of the installed package, so it is loaded via
``importlib`` (mirroring ``tests/test_job_spec_docs.py``). Most geometry
fixtures are synthetic PLT strings built inline: a multi-row, reference-char
framed sheet whose glyph shapes have exactly known dimensions, so the
baseline normalization, envelope sampling and scaling can be asserted
numerically.

Two real-world fixtures pin behaviour end-to-end:

* ``tests_deps/dino_0.5_E.plt`` (a copy of ``Fonts/PLT/dino_0.5_E.plt``)
  drives the full multi-row extraction against real EngraveLab output.
* ``tests_deps/dino_word_sample.plt`` pins the guard rail: a *word* engraving
  (no framing, not the full ASCII set) must be rejected rather than mis-mapped.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import math
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "Fonts" / "extract_plt_fonts.py"
DINO_FRAMED_FIXTURE = REPO_ROOT / "tests_deps" / "dino_0.5_E.plt"
DINO_WORD_FIXTURE = REPO_ROOT / "tests_deps" / "dino_word_sample.plt"

# Printable ASCII 33..126 - the shipped Fonts/ascii.txt content, inlined so
# tests never read files outside tests_deps/.
ASCII_CHARS = "".join(chr(code) for code in range(33, 127))


def _load_script() -> Any:
    """Import ``Fonts/extract_plt_fonts.py`` as a module by path.

    The module is registered in ``sys.modules`` before execution so
    ``@dataclass`` can resolve its own string annotations.

    Returns:
        The loaded script module.
    """
    spec = importlib.util.spec_from_file_location("extract_plt_fonts", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["extract_plt_fonts"] = module
    spec.loader.exec_module(module)
    return module


script = _load_script()

from plt_optimizer.core.models import (  # noqa: E402
    ArcSegment,
    Coordinate,
    PLTDocument,
    StrokePath,
    StrokeSegment,
)
from plt_optimizer.core.parser import PLTParser  # noqa: E402

# The synthetic sheet's fixed geometry (device frame, +Y down).
REF_HEIGHT = 500.0  # the reference glyph 'E' is exactly this tall
ROW_PITCH = 2000.0
GLYPH_PITCH = 1500.0
TOP_BASELINE = 1000.0  # baseline Y of the first (topmost) row


def _glyph_commands(char: str, x_left: float, baseline: float) -> List[str]:
    """Return HPGL command lines drawing ``char`` at ``(x_left, baseline)``.

    Shapes have exactly known extents so the tests can assert normalized
    results numerically. The reference ``E`` is pure line segments (so its
    framing and internal copies match to the last bit); ``g`` descends 200
    units below the baseline; ``_`` is a zero-height stroke 100 units below
    the baseline (the band that forces the row-threshold search); ``T`` is a
    triangle for envelope assertions; ``A`` carries an arc to exercise arc
    normalization; every other payload character is a 250x500 box.

    Args:
        char: The character to draw.
        x_left: Left edge X in plotter units.
        baseline: Baseline Y in plotter units (device frame, +Y down).

    Returns:
        One ``PU``/``PD``/``AA`` command per line.
    """
    top = baseline - REF_HEIGHT
    if char == "E":
        return [
            f"PU{x_left:.3f},{top:.3f};",
            f"PD{x_left:.3f},{baseline:.3f};",
            f"PU{x_left:.3f},{top:.3f};PD{x_left + 300.0:.3f},{top:.3f};",
            f"PU{x_left:.3f},{(top + baseline) / 2:.3f};PD{x_left + 250.0:.3f},"
            f"{(top + baseline) / 2:.3f};",
            f"PU{x_left:.3f},{baseline:.3f};PD{x_left + 300.0:.3f},{baseline:.3f};",
        ]
    if char == "g":  # box with a 200-unit descender below the baseline
        return [
            f"PU{x_left:.3f},{top:.3f};",
            f"PD{x_left + 250.0:.3f},{top:.3f};",
            f"PD{x_left + 250.0:.3f},{baseline + 200.0:.3f};",
            f"PD{x_left:.3f},{baseline + 200.0:.3f};",
            f"PD{x_left:.3f},{top:.3f};",
        ]
    if char == "_":  # zero-height stroke below the baseline
        return [
            f"PU{x_left:.3f},{baseline + 100.0:.3f};PD{x_left + 300.0:.3f},{baseline + 100.0:.3f};"
        ]
    if char == "T":  # triangle: base 300 wide at the baseline, apex at top-center
        return [
            f"PU{x_left:.3f},{baseline:.3f};",
            f"PD{x_left + 300.0:.3f},{baseline:.3f};",
            f"PD{x_left + 150.0:.3f},{top:.3f};",
            f"PD{x_left:.3f},{baseline:.3f};",
        ]
    if char == "A":  # box topped by a shallow best-fit arc
        return [
            f"PU{x_left:.3f},{top:.3f};",
            f"PD{x_left:.3f},{baseline:.3f};",
            f"PD{x_left + 250.0:.3f},{baseline:.3f};",
            f"PD{x_left + 250.0:.3f},{top:.3f};",
            f"PU{x_left:.3f},{top:.3f};",
            f"PD;AA{x_left + 125.0:.3f},{top - 48000.0:.3f},0.300;",
        ]
    return [  # default 250x500 box
        f"PU{x_left:.3f},{top:.3f};",
        f"PD{x_left + 250.0:.3f},{top:.3f};",
        f"PD{x_left + 250.0:.3f},{baseline:.3f};",
        f"PD{x_left:.3f},{baseline:.3f};",
        f"PD{x_left:.3f},{top:.3f};",
    ]


def _wide_e_commands(x_left: float, baseline: float) -> List[str]:
    """Return HPGL lines for an ``E`` with 300-unit arms (same point count as E).

    Used to build framing-geometry mismatches: identical structure and point
    count as :func:`_glyph_commands`' ``E``, but the middle arm extends 50
    units further, so the framing verification must reject it.

    Args:
        x_left: Left edge X in plotter units.
        baseline: Baseline Y in plotter units (device frame, +Y down).

    Returns:
        One ``PU``/``PD`` command per line.
    """
    top = baseline - REF_HEIGHT
    return [
        f"PU{x_left:.3f},{top:.3f};",
        f"PD{x_left:.3f},{baseline:.3f};",
        f"PU{x_left:.3f},{top:.3f};PD{x_left + 300.0:.3f},{top:.3f};",
        f"PU{x_left:.3f},{(top + baseline) / 2:.3f};PD{x_left + 300.0:.3f},"
        f"{(top + baseline) / 2:.3f};",
        f"PU{x_left:.3f},{baseline:.3f};PD{x_left + 300.0:.3f},{baseline:.3f};",
    ]


def make_framed_sheet(
    rows: Sequence[Sequence[str]],
    reference_char: str = "E",
    scrambled: bool = False,
    framing_char: Optional[str] = None,
) -> str:
    """Build a synthetic multi-row, reference-framed sample sheet.

    Each row in ``rows`` is a sequence of payload characters; the builder wraps
    every row with ``reference_char`` framing at both ends (the framing glyph
    is drawn identically to the internal copy so the verification passes).
    ``framing_char`` overrides the glyph actually *drawn* for the framing while
    keeping the nominal reference character, which lets tests build sheets
    whose framing geometry does not match the internal copy. Rows are stacked
    downward (device frame) with :data:`ROW_PITCH` and glyphs spaced by
    :data:`GLYPH_PITCH`.

    Args:
        rows: Payload characters per row, in reading order.
        reference_char: Framing character placed at both ends of every row.
        scrambled: Emit each row's glyph blocks out of order (EngraveLab does
            not engrave strictly left-to-right).
        framing_char: Optional glyph to draw for the framing instead of
            ``reference_char`` (``"wideE"`` builds a geometry mismatch).

    Returns:
        HPGL document text.
    """
    lines: List[str] = []
    for row_index, payload in enumerate(rows):
        baseline = TOP_BASELINE + row_index * ROW_PITCH
        sequence = [reference_char, *payload, reference_char]
        blocks: List[List[str]] = []
        for slot, char in enumerate(sequence):
            x_left = 500.0 + slot * GLYPH_PITCH
            is_framing = slot == 0 or slot == len(sequence) - 1
            if is_framing and framing_char == "wideE":
                blocks.append(_wide_e_commands(x_left, baseline))
            else:
                blocks.append(_glyph_commands(char, x_left, baseline))
        if scrambled:
            blocks = [
                blocks[i] for i in sorted(range(len(blocks)), key=lambda i: (i * 7) % len(blocks))
            ]
        for block in blocks:
            lines.extend(block)
    return "IN;PA;\n" + "".join(line + "\n" for line in lines) + "SP;\n"


@pytest.fixture
def framed_doc() -> PLTDocument:
    """Parsed synthetic two-row framed sheet (payload E A _ / E T g, scrambled)."""
    content = make_framed_sheet([["E", "A", "_"], ["E", "T", "g"]], scrambled=True)
    return PLTParser().parse_string(content)


# Small character list matching the framed_doc payload order.
FRAMED_CHARS = ["E", "A", "_", "E", "T", "g"]


class TestLoadCharacters:
    """Tests for load_characters()."""

    def test_splits_on_any_whitespace(self, tmp_path: Path) -> None:
        ascii_file = tmp_path / "ascii.txt"
        ascii_file.write_text("A     B\tC\n D  ", encoding="utf-8")
        assert script.load_characters(ascii_file) == ["A", "B", "C", "D"]

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(script.FontExtractionError, match="Failed to read"):
            script.load_characters(tmp_path / "nope.txt")

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        ascii_file = tmp_path / "ascii.txt"
        ascii_file.write_text("   \n  ", encoding="utf-8")
        with pytest.raises(script.FontExtractionError, match="no characters"):
            script.load_characters(ascii_file)


class TestParseFontFileName:
    """Tests for parse_font_file_name() (new underscore format)."""

    def test_parses_name_height_reference(self) -> None:
        name, height, ref = script.parse_font_file_name(Path("dino_0.5_E.plt"))
        assert name == "Dino"
        assert height == pytest.approx(0.5)
        assert ref == "E"

    def test_underscores_in_font_name_become_spaces(self) -> None:
        name, height, ref = script.parse_font_file_name(Path("heavy_engraving_0.75_H.plt"))
        assert name == "Heavy Engraving"
        assert height == pytest.approx(0.75)
        assert ref == "H"

    def test_missing_parts_raises(self) -> None:
        with pytest.raises(script.FontExtractionError, match="three '_' separated parts"):
            script.parse_font_file_name(Path("dino_0.5.plt"))

    def test_non_numeric_height_raises(self) -> None:
        with pytest.raises(script.FontExtractionError, match="not a declared text height"):
            script.parse_font_file_name(Path("dino_big_E.plt"))

    def test_zero_height_raises(self) -> None:
        with pytest.raises(script.FontExtractionError, match="positive and finite"):
            script.parse_font_file_name(Path("dino_0_E.plt"))

    def test_multi_character_reference_raises(self) -> None:
        with pytest.raises(script.FontExtractionError, match="exactly one character"):
            script.parse_font_file_name(Path("dino_0.5_EF.plt"))

    def test_empty_font_name_raises(self) -> None:
        with pytest.raises(script.FontExtractionError, match="font name is empty"):
            script.parse_font_file_name(Path("_0.5_E.plt"))


class TestSegmentBounds:
    """Tests for arc_bounds()/segment_bounds()/path_bounds()/union_bounds()."""

    def test_line_bounds(self) -> None:
        seg = StrokeSegment(Coordinate(10.0, 20.0), Coordinate(30.0, 5.0), True)
        assert script.segment_bounds(seg) == (10.0, 5.0, 30.0, 20.0)

    def test_giant_arc_uses_swept_extent_not_full_circle(self) -> None:
        arc = ArcSegment(
            start=Coordinate(0.0, 1000.0),
            end=Coordinate(300.0, 1000.0),
            center=Coordinate(150.0, -48000.0),
            sweep_angle=0.351,
            is_cutting=True,
        )
        x_min, y_min, x_max, y_max = script.arc_bounds(arc)
        assert x_max - x_min < 400.0
        assert y_max - y_min < 400.0

    def test_full_circle_arc_bounds_whole_circle(self) -> None:
        arc = ArcSegment(
            start=Coordinate(100.0, 0.0),
            end=Coordinate(100.0, 0.0),
            center=Coordinate(0.0, 0.0),
            sweep_angle=360.0,
            is_cutting=True,
        )
        assert script.arc_bounds(arc) == pytest.approx((-100.0, -100.0, 100.0, 100.0))

    def test_semicircle_crossing_cardinal_expands_bounds(self) -> None:
        arc = ArcSegment(
            start=Coordinate(100.0, 0.0),
            end=Coordinate(-100.0, 0.0),
            center=Coordinate(0.0, 0.0),
            sweep_angle=180.0,
            is_cutting=True,
        )
        x_min, y_min, x_max, y_max = script.arc_bounds(arc)
        assert (x_min, x_max) == pytest.approx((-100.0, 100.0))
        assert y_max == pytest.approx(100.0)
        assert y_min == pytest.approx(0.0)

    def test_path_without_segments_raises(self) -> None:
        with pytest.raises(ValueError, match="without segments"):
            script.path_bounds(StrokePath(segments=()))

    def test_union_bounds_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="empty sequence"):
            script.union_bounds([])


class TestMergeIntervals:
    """Tests for merge_intervals() and candidate_thresholds()."""

    def test_empty(self) -> None:
        assert script.merge_intervals([], 0.0) == []

    def test_threshold_merges_within_gap(self) -> None:
        intervals = [script.Interval(0.0, 10.0), script.Interval(20.0, 30.0)]
        assert [(i.x_min, i.x_max) for i in script.merge_intervals(intervals, 9.0)] == [
            (0.0, 10.0),
            (20.0, 30.0),
        ]
        assert [(i.x_min, i.x_max) for i in script.merge_intervals(intervals, 10.0)] == [
            (0.0, 30.0)
        ]

    def test_unsorted_overlapping(self) -> None:
        intervals = [
            script.Interval(50.0, 60.0),
            script.Interval(0.0, 10.0),
            script.Interval(5.0, 55.0),
        ]
        assert [(i.x_min, i.x_max) for i in script.merge_intervals(intervals, 0.0)] == [(0.0, 60.0)]

    def test_candidate_thresholds_bracket_gaps(self) -> None:
        # Gaps of 10 and 1000 -> candidates 0.0, midpoint(0,10)=5, midpoint(10,1000)=505.
        assert script.candidate_thresholds([1000.0, 10.0, 10.0]) == pytest.approx([0.0, 5.0, 505.0])

    def test_candidate_thresholds_ignores_nonpositive(self) -> None:
        assert script.candidate_thresholds([0.0, -5.0]) == [0.0]


class TestClusterAndRows:
    """Tests for cluster_paths() and group_rows()."""

    def test_three_blobs_scrambled(self) -> None:
        content = make_framed_sheet([["A", "B", "C"]])
        doc = PLTParser().parse_string(content)
        clusters = script.cluster_paths([p for p in doc.stroke_paths if p.segments], 600.0)
        # 3 payload + 2 framing = 5 clusters, sorted left-to-right.
        assert len(clusters) == 5
        assert all(
            clusters[i].center_x < clusters[i + 1].center_x for i in range(len(clusters) - 1)
        )

    def test_cluster_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="empty sequence"):
            script.cluster_paths([StrokePath(segments=())], 100.0)

    def test_group_rows_separates_by_y(self, framed_doc: PLTDocument) -> None:
        rows = script.group_rows([p for p in framed_doc.stroke_paths if p.segments], 725.0)
        assert len(rows) == 2

    def test_group_rows_empty(self) -> None:
        assert script.group_rows([StrokePath(segments=())], 100.0) == []


class TestGlyphTransform:
    """Tests for GlyphTransform.apply()/map_bounds()."""

    def test_apply_maps_baseline_to_zero_and_left_to_zero(self) -> None:
        transform = script.GlyphTransform(scale=2.0, origin_x=100.0, baseline=500.0)
        assert transform.apply(100.0, 500.0) == pytest.approx((0.0, 0.0))
        # +Y down raw becomes +Y up normalized.
        assert transform.apply(150.0, 400.0) == pytest.approx((100.0, 200.0))

    def test_map_bounds_reflects_y(self) -> None:
        transform = script.GlyphTransform(scale=2.0, origin_x=0.0, baseline=1000.0)
        # Raw box y in [500, 1000] -> normalized y in [0, 1000].
        assert transform.map_bounds((0.0, 500.0, 300.0, 1000.0)) == pytest.approx(
            (0.0, 0.0, 600.0, 1000.0)
        )


class TestEmitGlyph:
    """Tests for emit_glyph() (transform applied while writing, sweep negated)."""

    def test_line_glyph_is_pu_led_four_decimals(self) -> None:
        path = StrokePath(
            pen_up_position=Coordinate(0.0, 1000.0),
            segments=(StrokeSegment(Coordinate(0.0, 1000.0), Coordinate(500.0, 1000.0), True),),
        )
        transform = script.GlyphTransform(scale=2.0, origin_x=0.0, baseline=1000.0)
        glyph = script.emit_glyph([path], transform)
        assert glyph.startswith("PU0.0000,0.0000;")
        assert "PD1000.0000,0.0000;" in glyph
        assert glyph.endswith(";")

    def test_arc_sweep_is_negated(self) -> None:
        path = StrokePath(
            pen_up_position=Coordinate(0.0, 1000.0),
            segments=(
                ArcSegment(
                    start=Coordinate(0.0, 1000.0),
                    end=Coordinate(100.0, 1000.0),
                    center=Coordinate(50.0, 900.0),
                    sweep_angle=30.0,
                    is_cutting=True,
                ),
            ),
        )
        transform = script.GlyphTransform(scale=1.0, origin_x=0.0, baseline=1000.0)
        glyph = script.emit_glyph([path], transform)
        assert "AA" in glyph
        assert "-30.0000" in glyph

    def test_negative_zero_normalized(self) -> None:
        assert script._format_number(-0.00001) == "0.0000"

    def test_empty_glyph_is_empty_string(self) -> None:
        transform = script.GlyphTransform(scale=1.0, origin_x=0.0, baseline=0.0)
        assert script.emit_glyph([StrokePath(segments=())], transform) == ""

    def test_glyph_round_trips_through_parser(self, framed_doc: PLTDocument) -> None:
        rows = script.build_rows([p for p in framed_doc.stroke_paths if p.segments], 725.0, 600.0)
        transform = script.GlyphTransform(scale=2.0, origin_x=0.0, baseline=1000.0)
        for row in rows:
            for cluster in row.content:
                glyph = script.emit_glyph(cluster.paths, transform)
                PLTParser().parse_string(glyph)  # must not raise


class TestEnvelopeSampling:
    """Tests for sample_envelopes() and its helpers."""

    def _triangle(self) -> Tuple[List[StrokePath], Any, Tuple[float, float, float, float]]:
        # Raw (+Y down, baseline 1000) triangle normalizing to
        # (0,0)-(600,0)-(300,1000) in the stored +Y-up frame.
        path = StrokePath(
            pen_up_position=Coordinate(0.0, 1000.0),
            segments=(
                StrokeSegment(Coordinate(0.0, 1000.0), Coordinate(600.0, 1000.0), True),
                StrokeSegment(Coordinate(600.0, 1000.0), Coordinate(300.0, 0.0), True),
                StrokeSegment(Coordinate(300.0, 0.0), Coordinate(0.0, 1000.0), True),
            ),
        )
        transform = script.GlyphTransform(scale=1.0, origin_x=0.0, baseline=1000.0)
        return [path], transform, (0.0, 0.0, 600.0, 1000.0)

    def test_triangle_envelope_widths(self) -> None:
        paths, transform, bounds = self._triangle()
        left, right = script.sample_envelopes(paths, transform, bounds, 11)
        assert len(left) == 11 and len(right) == 11
        assert left[0] == pytest.approx([0.0, 0.0])
        assert right[0] == pytest.approx([600.0, 0.0])
        # Band sampling widens the extremes: the apex sample covers
        # y in [950, 1000], where the edges reach x = 0.3 * 950 and
        # 600 - 0.3 * 950.
        assert left[-1] == pytest.approx([285.0, 1000.0])
        assert right[-1] == pytest.approx([315.0, 1000.0])
        # Left edge moves right monotonically up the triangle.
        assert all(left[i][0] <= left[i + 1][0] + 1e-6 for i in range(len(left) - 1))

    def test_zero_height_glyph_repeats_single_y(self) -> None:
        # Raw stroke 200 units below the baseline (raw y = 1200) normalizes to
        # a zero-height envelope at y = -200.
        path = StrokePath(
            pen_up_position=Coordinate(0.0, 1200.0),
            segments=(StrokeSegment(Coordinate(0.0, 1200.0), Coordinate(300.0, 1200.0), True),),
        )
        transform = script.GlyphTransform(scale=1.0, origin_x=0.0, baseline=1000.0)
        left, right = script.sample_envelopes([path], transform, (0.0, -200.0, 300.0, -200.0), 5)
        assert [pt[1] for pt in left] == [-200.0] * 5
        assert [pt[0] for pt in left] == [0.0] * 5
        assert [pt[0] for pt in right] == [300.0] * 5

    def test_arc_crossing_is_analytic(self) -> None:
        # Upper semicircle radius 100 centered at (0,0): at y=60 the crossings
        # are +/-80 (analytic circle/line intersection, no flattening).
        xs = script._arc_xs_at(0.0, 0.0, 100.0, 0.0, math.pi, 60.0)
        assert sorted(xs) == pytest.approx([-80.0, 80.0])

    def test_arc_below_sweep_has_no_crossing(self) -> None:
        # The upper semicircle never reaches y = -60.
        assert script._arc_xs_at(0.0, 0.0, 100.0, 0.0, math.pi, -60.0) == []

    def test_interpolate_gaps_linear(self) -> None:
        assert script._interpolate_gaps([0.0, None, 10.0]) == [0.0, 5.0, 10.0]

    def test_interpolate_gaps_clamps_edges(self) -> None:
        assert script._interpolate_gaps([None, 4.0, None]) == [4.0, 4.0, 4.0]

    def test_interpolate_all_undefined_raises(self) -> None:
        with pytest.raises(ValueError, match="no defined samples"):
            script._interpolate_gaps([None, None])

    def test_samples_below_two_raises(self) -> None:
        paths, transform, bounds = self._triangle()
        with pytest.raises(ValueError, match="at least 2 samples"):
            script.sample_envelopes(paths, transform, bounds, 1)


class TestBandEnvelopeSampling:
    """Each envelope sample aggregates a band, so hairlines are never missed.

    EngraveLab engraves a glyph's horizontal bars as *single* strokes of zero
    width. Sampling a zero-thickness line at uniform heights mathematically
    guarantees such a bar is missed whenever its height is not an exact multiple
    of the sample step (the shipped Dino ``F`` middle bar sits at y = 500.2923
    while the 30-sample step is 34.482759), which stored a phantom notch in the
    right envelope. Every sample therefore covers the band
    ``[y_k - step/2, y_k + step/2]`` and records the extreme X of *any* geometry
    meeting that band.
    """

    # The Dino 'F' reproduced in the normalized frame: a spine, a top bar
    # landing exactly on the bbox top, and an off-grid hairline middle bar.
    _F_SPINE = ((0.0, 0.0), (0.0, 1000.0))
    _F_TOP_BAR = ((0.0, 1000.0), (667.446168, 1000.0))
    _F_MID_BAR = ((0.0, 500.2923), (555.816, 500.2923))

    @staticmethod
    def _paths(*lines: Tuple[Tuple[float, float], Tuple[float, float]]) -> List[StrokePath]:
        """Build one pen-up-led stroke path per normalized line segment.

        Args:
            *lines: ``(start, end)`` normalized coordinate pairs (+Y up).

        Returns:
            Stroke paths in the *raw* device frame (+Y down, baseline 1000),
            ready for the identity transform used by these tests.
        """
        paths: List[StrokePath] = []
        for (x0, y0), (x1, y1) in lines:
            start = Coordinate(x0, 1000.0 - y0)
            end = Coordinate(x1, 1000.0 - y1)
            paths.append(
                StrokePath(
                    pen_up_position=start,
                    segments=(StrokeSegment(start, end, True),),
                )
            )
        return paths

    @staticmethod
    def _identity() -> Any:
        """The identity normalization (scale 1, origin 0, baseline 1000)."""
        return script.GlyphTransform(scale=1.0, origin_x=0.0, baseline=1000.0)

    def test_off_grid_hairline_is_captured(self) -> None:
        """The Dino 'F' middle bar lands on the sample that covers its band."""
        paths = self._paths(self._F_SPINE, self._F_TOP_BAR, self._F_MID_BAR)
        left, right = script.sample_envelopes(
            paths, self._identity(), (0.0, 0.0, 667.446168, 1000.0), 30
        )
        # Sample step 34.482759: y = 500.2923 falls in sample 15's band
        # [500.0, 534.4828], and in no other band.
        assert right[15][0] == pytest.approx(555.816, abs=1e-6)
        assert right[14][0] == pytest.approx(0.0, abs=1e-6)
        assert right[16][0] == pytest.approx(0.0, abs=1e-6)
        # The top bar is still captured, and exactly two samples see material
        # right of the spine (the historical bug stored only one). Coordinates
        # round to 3 decimals in the parser models, hence the 1e-3 tolerance.
        assert right[-1][0] == pytest.approx(667.446168, abs=1e-3)
        assert sum(1 for point in right if point[0] > 1.0) == 2
        # The spine is the only left-edge geometry at every height.
        assert all(point[0] == pytest.approx(0.0, abs=1e-6) for point in left)

    def test_bar_on_band_boundary_is_captured_by_both_neighbours(self) -> None:
        """Bands tile the axis, so a boundary height belongs to both samples."""
        paths = self._paths(self._F_SPINE, ((0.0, 500.0), (555.816, 500.0)))
        _left, right = script.sample_envelopes(
            paths, self._identity(), (0.0, 0.0, 555.816, 1000.0), 30
        )
        # y = 500.0 is exactly the shared edge of samples 14 and 15.
        assert right[14][0] == pytest.approx(555.816, abs=1e-6)
        assert right[15][0] == pytest.approx(555.816, abs=1e-6)

    def test_band_extremes_never_narrow_exact_sampling(self) -> None:
        """A band envelope is a conservative widening of the exact silhouette."""
        paths, transform, bounds = TestEnvelopeSampling()._triangle()
        _left, right = script.sample_envelopes(paths, transform, bounds, 11)
        # The triangle's right edge is x = 600 - 0.3y; sample 5 sits at y = 500
        # with band [450, 550], so the widest point of the band is its bottom.
        assert right[5][0] == pytest.approx(600.0 - 0.3 * 450.0, abs=1e-6)
        assert right[5][0] >= 450.0  # the exact same-height crossing
        # The apex sample widens downward to the band's lower edge.
        assert right[-1][0] == pytest.approx(600.0 - 0.3 * 950.0, abs=1e-6)

    def test_arc_band_uses_swept_extremum_not_full_circle(self) -> None:
        """A shallow huge-radius arc reports its swept tip, never its circle."""
        # EngraveLab fits near-straight bars as huge-radius arcs: this one spans
        # only 80..100 degrees of a radius-1000 circle, peaking at x = +-173.6
        # while the full circle would reach +-1000.
        lo, hi = math.radians(80.0), math.radians(100.0)
        xs = script._arc_band_xs(0.0, 0.0, 1000.0, lo, hi, 900.0, 1000.0)
        assert max(xs) == pytest.approx(1000.0 * math.cos(lo), abs=1e-6)
        assert min(xs) == pytest.approx(-1000.0 * math.cos(lo), abs=1e-6)
        assert max(xs) < 1000.0

    def test_arc_band_clamps_to_the_band(self) -> None:
        """The arc's band extremes come from the clamped band edges."""
        # Right half circle, radius 100: within the band [10, 20] the widest
        # point is the y = 10 edge (x = 99.4987), not the y = 0 equator (x = 100).
        xs = script._arc_band_xs(0.0, 0.0, 100.0, -math.pi / 2.0, math.pi / 2.0, 10.0, 20.0)
        assert max(xs) == pytest.approx(math.sqrt(100.0**2 - 10.0**2), abs=1e-6)
        assert min(xs) == pytest.approx(math.sqrt(100.0**2 - 20.0**2), abs=1e-6)

    def test_arc_outside_the_band_contributes_nothing(self) -> None:
        """An arc entirely above the band is ignored."""
        assert script._arc_band_xs(0.0, 0.0, 100.0, 0.0, math.pi, 500.0, 600.0) == []

    def test_line_band_extremes(self) -> None:
        """A slanted segment contributes its X at both ends of the overlap."""
        line = (0.0, 0.0, 100.0, 100.0)
        assert script._line_band_xs(line, 20.0, 40.0) == pytest.approx([20.0, 40.0])
        # Fully outside the band -> no candidate.
        assert script._line_band_xs(line, 200.0, 300.0) == []
        # A horizontal bar inside the band yields both of its endpoints.
        assert script._line_band_xs((5.0, 50.0, 77.0, 50.0), 40.0, 60.0) == [5.0, 77.0]

    def test_dino_f_middle_bar_reaches_the_stored_envelope(self) -> None:
        """The shipped Dino sheet cannot reproduce the phantom-notch bug."""
        characters = script.load_characters(REPO_ROOT / "Fonts" / "ascii.txt")
        entry = script.extract_font_file(DINO_FRAMED_FIXTURE, characters).entry
        f_entry = entry["characters"]["F"]
        right = f_entry["right_envelope"]
        bar_tip = max(point[0] for point in right if point[1] < 900.0)
        assert bar_tip > 100.0, "the middle bar vanished from the right envelope"
        assert sum(1 for point in right if point[0] > 100.0) >= 2


class TestMedian:
    """Tests for median()."""

    def test_odd_and_even(self) -> None:
        assert script.median([3.0, 1.0, 2.0]) == 2.0
        assert script.median([4.0, 1.0, 2.0, 3.0]) == pytest.approx(2.5)

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="empty sequence"):
            script.median([])


class TestCheckHeightDrift:
    """Tests for check_height_drift() (no correction, warn above threshold)."""

    def test_within_tolerance_is_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="extract_plt_fonts"):
            drift = script.check_height_drift("Dino", Path("dino_0.5_E.plt"), 0.5, "E", 0.508)
        assert drift == pytest.approx(0.016, abs=1e-3)
        assert caplog.records == []

    def test_large_drift_warns_without_raising(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="extract_plt_fonts"):
            drift = script.check_height_drift(
                "Jhanuni", Path("jhanuni_0.45_E.plt"), 0.45, "E", 0.522
            )
        assert drift == pytest.approx(0.16, abs=1e-2)
        text = "\n".join(record.getMessage() for record in caplog.records)
        assert "Declared Height" in text
        assert "Computed Height" in text
        assert "Drift Detected" in text
        assert "without corrective adjustment" in text


class TestVerifyAndFindLayout:
    """Tests for verify_layout() and find_layout()."""

    def test_reference_not_in_characters_raises(self) -> None:
        content = make_framed_sheet([["A"]])
        doc = PLTParser().parse_string(content)
        paths = [p for p in doc.stroke_paths if p.segments]
        with pytest.raises(script.FontExtractionError, match="not part of the character file"):
            script.find_layout(paths, ["A", "B"], "E")

    def test_count_mismatch_names_direction(self) -> None:
        content = make_framed_sheet([["E", "A"]])
        doc = PLTParser().parse_string(content)
        rows = script.build_rows([p for p in doc.stroke_paths if p.segments], 725.0, 600.0)
        with pytest.raises(script.FontExtractionError) as excinfo:
            script.verify_layout(rows, ["E", "A", "B", "C", "D"], "E")
        assert "missing" in str(excinfo.value)

    def test_framing_geometry_mismatch_detected(self) -> None:
        # Framing drawn as a wider E: same point count, different geometry.
        content = make_framed_sheet([["E", "A"]], reference_char="E", framing_char="wideE")
        doc = PLTParser().parse_string(content)
        rows = script.build_rows([p for p in doc.stroke_paths if p.segments], 725.0, 600.0)
        with pytest.raises(script.FontExtractionError) as excinfo:
            script.verify_layout(rows, ["E", "A"], "E")
        assert "differs from the internal copy" in str(excinfo.value)

    def test_correct_layout_passes(self) -> None:
        content = make_framed_sheet([["E", "A"]], reference_char="E")
        doc = PLTParser().parse_string(content)
        rows = script.build_rows([p for p in doc.stroke_paths if p.segments], 725.0, 600.0)
        script.verify_layout(rows, ["E", "A"], "E")  # must not raise

    def test_find_layout_recovers_synthetic(self, framed_doc: PLTDocument) -> None:
        rows, row_t, x_t = script.find_layout(
            [p for p in framed_doc.stroke_paths if p.segments], FRAMED_CHARS, "E"
        )
        assert len(rows) == 2
        assert sum(len(r.clusters) - 2 for r in rows) == len(FRAMED_CHARS)

    def test_find_layout_no_valid_config_raises(self) -> None:
        # A row whose framing is a wider E matches no threshold pairing, so the
        # search exhausts every candidate and reports the failure.
        doc = PLTParser().parse_string(
            make_framed_sheet([["E", "A"]], reference_char="E", framing_char="wideE")
        )
        with pytest.raises(script.FontExtractionError, match="Could not split"):
            script.find_layout([p for p in doc.stroke_paths if p.segments], ["E", "A"], "E")


class TestExtractFontFromDocument:
    """Tests for extract_font_from_document() normalization."""

    def test_reference_glyph_is_exactly_1000_tall(self, framed_doc: PLTDocument) -> None:
        entry, row_count, _, _ = script.extract_font_from_document(
            framed_doc, FRAMED_CHARS, "E", declared_height=0.5
        )
        assert row_count == 2
        e_box = entry["characters"]["E"]["bounding_box"]
        assert e_box["min_y"] == pytest.approx(0.0, abs=1e-6)
        assert e_box["max_y"] == pytest.approx(1000.0, abs=1e-3)
        assert e_box["min_x"] == pytest.approx(0.0, abs=1e-6)
        assert entry["reference_char_height_in"] == pytest.approx(0.5, abs=1e-3)
        assert entry["normalized_ref_height"] == 1.0

    def test_descender_is_negative(self, framed_doc: PLTDocument) -> None:
        entry, _, _, _ = script.extract_font_from_document(
            framed_doc, FRAMED_CHARS, "E", declared_height=0.5
        )
        assert entry["characters"]["g"]["bounding_box"]["min_y"] < 0.0

    def test_underscore_sits_below_baseline(self, framed_doc: PLTDocument) -> None:
        entry, _, _, _ = script.extract_font_from_document(
            framed_doc, FRAMED_CHARS, "E", declared_height=0.5
        )
        underscore = entry["characters"]["_"]["bounding_box"]
        assert underscore["min_y"] == pytest.approx(underscore["max_y"])
        assert underscore["max_y"] < 0.0

    def test_arc_glyph_round_trips(self, framed_doc: PLTDocument) -> None:
        entry, _, _, _ = script.extract_font_from_document(
            framed_doc, FRAMED_CHARS, "E", declared_height=0.5
        )
        glyph = entry["characters"]["A"]["glyph"]
        assert "AA" in glyph
        PLTParser().parse_string(glyph)

    def test_envelope_length_matches_default(self, framed_doc: PLTDocument) -> None:
        entry, _, _, _ = script.extract_font_from_document(
            framed_doc, FRAMED_CHARS, "E", declared_height=0.5
        )
        assert len(entry["characters"]["E"]["left_envelope"]) == script.ENVELOPE_SAMPLES

    def test_envelope_samples_override(self, framed_doc: PLTDocument) -> None:
        entry, _, _, _ = script.extract_font_from_document(
            framed_doc, FRAMED_CHARS, "E", declared_height=0.5, envelope_samples=7
        )
        assert len(entry["characters"]["E"]["left_envelope"]) == 7

    def test_geometry_free_document_raises(self) -> None:
        doc = PLTParser().parse_string("IN;PA;SP;\n")
        with pytest.raises(script.FontExtractionError, match="no stroke geometry"):
            script.extract_font_from_document(doc, ["A"], "E", declared_height=0.5)


class TestExtractFontFile:
    """Tests for extract_font_file() including the real-word guard rail."""

    def test_metadata_from_file_name(self, tmp_path: Path) -> None:
        plt_path = tmp_path / "heavy_engraving_0.75_E.plt"
        plt_path.write_text(make_framed_sheet([["E", "A"]]), encoding="utf-8")
        extraction = script.extract_font_file(plt_path, ["E", "A"])
        assert extraction.font_name == "Heavy Engraving"
        assert extraction.entry["declared_height_in"] == pytest.approx(0.75)
        assert extraction.entry["reference_char"] == "E"
        assert extraction.entry["file_path"].endswith("heavy_engraving_0.75_E.plt")

    def test_font_name_override(self, tmp_path: Path) -> None:
        plt_path = tmp_path / "whatever_2_E.plt"
        plt_path.write_text(make_framed_sheet([["E", "A"]]), encoding="utf-8")
        extraction = script.extract_font_file(plt_path, ["E", "A"], font_name="My Font")
        assert extraction.font_name == "My Font"

    def test_drift_warning_emitted_but_extraction_succeeds(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Measured ref height is 0.5 (REF_HEIGHT=500); declare 0.4 -> 25% drift.
        plt_path = tmp_path / "drifty_0.4_E.plt"
        plt_path.write_text(make_framed_sheet([["E", "A"]]), encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="extract_plt_fonts"):
            extraction = script.extract_font_file(plt_path, ["E", "A"])
        assert extraction.entry["reference_char_height_in"] == pytest.approx(0.5, abs=1e-3)
        assert any("Drift Detected" in record.getMessage() for record in caplog.records)

    def test_malformed_file_name_raises(self, tmp_path: Path) -> None:
        plt_path = tmp_path / "noheight.plt"
        plt_path.write_text(make_framed_sheet([["E", "A"]]), encoding="utf-8")
        with pytest.raises(script.FontExtractionError, match="three '_' separated parts"):
            script.extract_font_file(plt_path, ["E", "A"])

    def test_word_engrave_fixture_is_rejected(self, tmp_path: Path) -> None:
        """A word engraving (no framing) must be rejected, not mis-mapped."""
        assert DINO_WORD_FIXTURE.exists(), "missing fixture tests_deps/dino_word_sample.plt"
        renamed = tmp_path / "dino_0.05_E.plt"
        shutil.copy(DINO_WORD_FIXTURE, renamed)
        with pytest.raises(script.FontExtractionError) as excinfo:
            script.extract_font_file(renamed, ASCII_CHARS)
        assert "Could not split" in str(excinfo.value)


class TestJsonRoundTrip:
    """Tests for load_existing_fonts()/write_fonts_json() (new nested schema)."""

    def _entry(self, glyph: str) -> Dict[str, Any]:
        return {
            "file_path": "x.plt",
            "reference_char": "E",
            "declared_height_in": 0.5,
            "reference_char_height_in": 0.5,
            "normalized_ref_height": 1.0,
            "characters": {
                "A": {"glyph": glyph, "bounding_box": {}, "left_envelope": [], "right_envelope": []}
            },
        }

    def test_missing_file_loads_empty(self, tmp_path: Path) -> None:
        assert script.load_existing_fonts(tmp_path / "nope.json") == {}

    def test_write_orders_fonts_and_characters(self, tmp_path: Path) -> None:
        out = tmp_path / "fonts.json"
        fonts = {"Zed": self._entry("z;"), "Abe": self._entry("a;")}
        fonts["Zed"]["characters"]["B"] = fonts["Zed"]["characters"]["A"]
        script.write_fonts_json(out, fonts, characters=["A", "B"])
        data = json.loads(out.read_text(encoding="utf-8"))
        assert list(data) == ["Abe", "Zed"]
        assert list(data["Zed"]["characters"]) == ["A", "B"]

    def test_round_trip_preserves_entries(self, tmp_path: Path) -> None:
        out = tmp_path / "fonts.json"
        fonts = {"Abe": self._entry("a;")}
        script.write_fonts_json(out, fonts, characters=["A"])
        loaded = script.load_existing_fonts(out)
        assert loaded["Abe"]["characters"]["A"]["glyph"] == "a;"

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "fonts.json"
        bad.write_text("[1, 2]", encoding="utf-8")
        with pytest.raises(script.FontExtractionError, match="must contain a JSON object"):
            script.load_existing_fonts(bad)

    def test_legacy_flat_schema_is_detected(self, tmp_path: Path) -> None:
        legacy = tmp_path / "fonts.json"
        legacy.write_text('{"Dino": {"A": "PU0,0;"}}', encoding="utf-8")
        with pytest.raises(script.FontExtractionError, match="legacy flat schema"):
            script.load_existing_fonts(legacy)

    def test_entry_without_characters_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "fonts.json"
        # `_is_font_entry` only requires the key to exist; this entry has it
        # with a non-dict value, which the loader must still reject.
        bad.write_text('{"Dino": {"characters": "nope"}}', encoding="utf-8")
        with pytest.raises(script.FontExtractionError, match="'characters' object"):
            script.load_existing_fonts(bad)


class TestMainCli:
    """End-to-end tests for the CLI entry point."""

    def _make_fonts_dir(self, tmp_path: Path) -> Path:
        fonts_dir = tmp_path / "fonts"
        fonts_dir.mkdir()
        (fonts_dir / "myfont_1_E.plt").write_text(
            make_framed_sheet([["E", "A", "_"], ["E", "T", "g"]], scrambled=True), encoding="utf-8"
        )
        return fonts_dir

    def _ascii_file(self, tmp_path: Path) -> Path:
        ascii_file = tmp_path / "ascii.txt"
        ascii_file.write_text("E A _ E T g", encoding="utf-8")
        return ascii_file

    def test_success_writes_json(self, tmp_path: Path) -> None:
        out = tmp_path / "out.json"
        rc = script.main(
            [
                "--fonts-dir",
                str(self._make_fonts_dir(tmp_path)),
                "--ascii-file",
                str(self._ascii_file(tmp_path)),
                "--output",
                str(out),
            ]
        )
        assert rc == 0
        data = json.loads(out.read_text(encoding="utf-8"))
        assert list(data) == ["Myfont"]
        # 'E' appears twice in the character list (rows share it), so the six
        # positional slots collapse to five unique keys.
        assert sorted(data["Myfont"]["characters"]) == ["A", "E", "T", "_", "g"]

    def test_merge_preserves_other_fonts(self, tmp_path: Path) -> None:
        fonts_dir = self._make_fonts_dir(tmp_path)
        out = tmp_path / "out.json"
        args = [
            "--fonts-dir",
            str(fonts_dir),
            "--ascii-file",
            str(self._ascii_file(tmp_path)),
            "--output",
            str(out),
        ]
        assert script.main(args) == 0
        (fonts_dir / "other_1_E.plt").write_text(
            make_framed_sheet([["E", "A", "_"], ["E", "T", "g"]]), encoding="utf-8"
        )
        assert script.main(args) == 0
        assert sorted(json.loads(out.read_text(encoding="utf-8"))) == ["Myfont", "Other"]

    def test_rebuild_drops_missing_fonts(self, tmp_path: Path) -> None:
        fonts_dir = self._make_fonts_dir(tmp_path)
        out = tmp_path / "out.json"
        base = [
            "--fonts-dir",
            str(fonts_dir),
            "--ascii-file",
            str(self._ascii_file(tmp_path)),
            "--output",
            str(out),
        ]
        assert script.main(base) == 0
        (fonts_dir / "myfont_1_E.plt").unlink()
        (fonts_dir / "other_1_E.plt").write_text(
            make_framed_sheet([["E", "A", "_"], ["E", "T", "g"]]), encoding="utf-8"
        )
        assert script.main(base + ["--rebuild"]) == 0
        assert sorted(json.loads(out.read_text(encoding="utf-8"))) == ["Other"]

    def test_legacy_output_aborts_merge(self, tmp_path: Path) -> None:
        fonts_dir = self._make_fonts_dir(tmp_path)
        out = tmp_path / "out.json"
        out.write_text('{"Old": {"A": "PU0,0;"}}', encoding="utf-8")
        rc = script.main(
            [
                "--fonts-dir",
                str(fonts_dir),
                "--ascii-file",
                str(self._ascii_file(tmp_path)),
                "--output",
                str(out),
            ]
        )
        assert rc == 1

    def test_bad_sheet_exits_nonzero_and_keeps_file(self, tmp_path: Path) -> None:
        fonts_dir = tmp_path / "fonts"
        fonts_dir.mkdir()
        (fonts_dir / "bad_1_E.plt").write_text(make_framed_sheet([["A"]]), encoding="utf-8")
        out = tmp_path / "out.json"
        rc = script.main(
            [
                "--fonts-dir",
                str(fonts_dir),
                "--ascii-file",
                str(self._ascii_file(tmp_path)),
                "--output",
                str(out),
            ]
        )
        assert rc == 1
        assert not out.exists()

    def test_missing_fonts_dir_exits_nonzero(self, tmp_path: Path) -> None:
        rc = script.main(
            [
                "--fonts-dir",
                str(tmp_path / "nope"),
                "--ascii-file",
                str(self._ascii_file(tmp_path)),
                "--output",
                str(tmp_path / "out.json"),
            ]
        )
        assert rc == 1

    def test_empty_fonts_dir_exits_nonzero(self, tmp_path: Path) -> None:
        empty = tmp_path / "fonts"
        empty.mkdir()
        rc = script.main(
            [
                "--fonts-dir",
                str(empty),
                "--ascii-file",
                str(self._ascii_file(tmp_path)),
                "--output",
                str(tmp_path / "out.json"),
            ]
        )
        assert rc == 1

    def test_envelope_samples_below_two_exits_nonzero(self, tmp_path: Path) -> None:
        rc = script.main(
            [
                "--fonts-dir",
                str(self._make_fonts_dir(tmp_path)),
                "--ascii-file",
                str(self._ascii_file(tmp_path)),
                "--output",
                str(tmp_path / "out.json"),
                "--envelope-samples",
                "1",
            ]
        )
        assert rc == 1

    def test_font_name_requires_single_file(self, tmp_path: Path) -> None:
        fonts_dir = self._make_fonts_dir(tmp_path)
        (fonts_dir / "second_1_E.plt").write_text(
            make_framed_sheet([["E", "A", "_"], ["E", "T", "g"]]), encoding="utf-8"
        )
        rc = script.main(
            [
                "--fonts-dir",
                str(fonts_dir),
                "--ascii-file",
                str(self._ascii_file(tmp_path)),
                "--output",
                str(tmp_path / "out.json"),
                "--font-name",
                "Custom",
            ]
        )
        assert rc == 1


class TestRealSheetIntegration:
    """End-to-end extraction against the real Dino framed sheet."""

    def test_dino_framed_sheet_extracts(self) -> None:
        assert DINO_FRAMED_FIXTURE.exists(), "missing fixture tests_deps/dino_0.5_E.plt"
        characters = script.load_characters(REPO_ROOT / "Fonts" / "ascii.txt")
        extraction = script.extract_font_file(DINO_FRAMED_FIXTURE, characters)
        entry = extraction.entry
        assert extraction.row_count == 6
        assert entry["reference_char"] == "E"
        assert entry["declared_height_in"] == pytest.approx(0.5)
        # Measured E height ~0.508 in (1.6% over declared -> within 5%, silent).
        assert entry["reference_char_height_in"] == pytest.approx(0.508, abs=0.002)
        assert len(entry["characters"]) == len(characters)
        assert " " not in entry["characters"]

    def test_dino_reference_and_descender_geometry(self) -> None:
        characters = script.load_characters(REPO_ROOT / "Fonts" / "ascii.txt")
        entry = script.extract_font_file(DINO_FRAMED_FIXTURE, characters).entry
        e_box = entry["characters"]["E"]["bounding_box"]
        assert e_box["min_y"] == pytest.approx(0.0, abs=1e-3)
        assert e_box["max_y"] == pytest.approx(1000.0, abs=1e-2)
        # 'g' descends below the baseline.
        assert entry["characters"]["g"]["bounding_box"]["min_y"] < 0.0
        # Envelopes sampled at the configured count, Y monotonically increasing.
        env = entry["characters"]["E"]["left_envelope"]
        assert len(env) == script.ENVELOPE_SAMPLES
        assert all(env[i][1] <= env[i + 1][1] + 1e-9 for i in range(len(env) - 1))

    def test_dino_all_glyphs_round_trip(self) -> None:
        characters = script.load_characters(REPO_ROOT / "Fonts" / "ascii.txt")
        entry = script.extract_font_file(DINO_FRAMED_FIXTURE, characters).entry
        parser = PLTParser()
        for character, char_entry in entry["characters"].items():
            assert char_entry["glyph"], f"empty glyph for {character!r}"
            parser.parse_string(char_entry["glyph"])
