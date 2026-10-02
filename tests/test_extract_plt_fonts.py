"""Tests for ``Fonts/extract_plt_fonts.py`` (PLT font reverse-engineering).

The script is not part of the installed package, so it is loaded via
``importlib`` (mirroring ``tests/test_job_spec_docs.py``). Geometry fixtures
are synthetic PLT strings built inline: three widely-spaced "glyph" blobs,
each a two-segment polyline plus a giant-radius best-fit arc (mimicking
EngraveLab's arc-heavy output, emitted in scrambled order).

The single real-world fixture ``tests_deps/dino_word_sample.plt`` pins the
guard rail: a *word* engraving (not a full spaced ASCII row) must be
rejected rather than mis-mapped onto the character list.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "Fonts" / "extract_plt_fonts.py"
DINO_FIXTURE = REPO_ROOT / "tests_deps" / "dino_word_sample.plt"

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


def make_blob_plt(x_offsets: List[float], scrambled: bool = False) -> str:
    """Build a synthetic single-row PLT with one blob per X offset.

    Each blob spans ``[x, x + 300]`` and consists of a two-segment polyline
    plus a huge-radius arc stroke (near-straight, like EngraveLab's best-fit
    arcs). With offsets 3000 apart, inter-blob gaps are ~2700 units.

    Args:
        x_offsets: Left edge of each blob, in plotter units.
        scrambled: When True, emit blob bodies out of order (EngraveLab does
            not engrave strictly left-to-right).

    Returns:
        HPGL document text.
    """
    blobs: List[str] = []
    for x in x_offsets:
        blobs.append(
            f"PU{x:.3f},1000.000;\n"
            f"PD{x:.3f},2000.000;\n"
            f"PD{x + 300:.3f},2000.000;\n"
            f"PU{x + 100:.3f},1000.000;\n"
            f"PD;AA{x + 150:.3f},-48000.000,0.340;\n"
        )
    if scrambled:
        blobs = [blobs[i] for i in sorted(range(len(blobs)), key=lambda i: (i * 7) % len(blobs))]
    return "IN;PA;\n" + "".join(blobs) + "SP;\n"


@pytest.fixture
def three_blob_doc() -> PLTDocument:
    """Parsed synthetic document with three widely-spaced blobs (scrambled)."""
    content = make_blob_plt([0.0, 3000.0, 6000.0], scrambled=True)
    return PLTParser().parse_string(content)


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
    """Tests for parse_font_file_name()."""

    def test_parses_name_and_height(self) -> None:
        name, height = script.parse_font_file_name(Path("dino 0.05.plt"))
        assert name == "Dino"
        assert height == pytest.approx(0.05)

    def test_multiword_font_name(self) -> None:
        name, height = script.parse_font_file_name(Path("heavy slant 1.25.plt"))
        assert name == "Heavy Slant"
        assert height == pytest.approx(1.25)

    def test_missing_height_raises(self) -> None:
        with pytest.raises(script.FontExtractionError, match="no text height found"):
            script.parse_font_file_name(Path("dino.plt"))

    def test_non_numeric_height_raises(self) -> None:
        with pytest.raises(script.FontExtractionError, match="not a"):
            script.parse_font_file_name(Path("dino big.plt"))

    def test_zero_height_raises(self) -> None:
        with pytest.raises(script.FontExtractionError, match="positive finite"):
            script.parse_font_file_name(Path("dino 0.plt"))

    def test_negative_height_raises(self) -> None:
        with pytest.raises(script.FontExtractionError, match="positive finite"):
            script.parse_font_file_name(Path("dino -0.5.plt"))


class TestSegmentBounds:
    """Tests for arc_bounds()/segment_bounds()/path_bounds()."""

    def test_line_bounds(self) -> None:
        seg = StrokeSegment(Coordinate(10.0, 20.0), Coordinate(30.0, 5.0), True)
        assert script.segment_bounds(seg) == (10.0, 5.0, 30.0, 20.0)

    def test_giant_arc_uses_swept_extent_not_full_circle(self) -> None:
        # Near-straight 300-unit chord on a ~49000-unit-radius circle:
        # swept bounds must stay ~300 wide, not ~98000 (the full circle).
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
        # 180-degree sweep from (100,0) through (0,100) to (-100,0).
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


class TestMergeIntervals:
    """Tests for merge_intervals()."""

    def test_empty(self) -> None:
        assert script.merge_intervals([]) == []

    def test_overlapping_touching_and_unsorted(self) -> None:
        intervals = [
            script.Interval(50.0, 60.0),
            script.Interval(0.0, 10.0),
            script.Interval(10.0, 20.0),  # touching -> merges
            script.Interval(5.0, 55.0),  # spans across -> merges all
        ]
        merged = script.merge_intervals(intervals)
        assert [(i.x_min, i.x_max) for i in merged] == [(0.0, 60.0)]

    def test_disjoint_stay_separate(self) -> None:
        intervals = [script.Interval(40.0, 50.0), script.Interval(0.0, 10.0)]
        merged = script.merge_intervals(intervals)
        assert [(i.x_min, i.x_max) for i in merged] == [(0.0, 10.0), (40.0, 50.0)]


class TestAutoClusterThreshold:
    """Tests for auto_cluster_threshold()."""

    def test_picks_max_margin_midpoint(self) -> None:
        # Three groups separated by gaps of 100 and 1000; two intra-group
        # gaps of 10. For 3 clusters the cuts are the two big gaps and the
        # threshold is the midpoint of the smallest cut and largest kept gap.
        intervals = [
            script.Interval(0.0, 10.0),
            script.Interval(15.0, 20.0),  # intra gap 5
            script.Interval(120.0, 130.0),  # cut gap 100
            script.Interval(135.0, 140.0),  # intra gap 5
            script.Interval(1140.0, 1150.0),  # cut gap 1000
        ]
        threshold = script.auto_cluster_threshold(intervals, expected_clusters=3)
        assert threshold == pytest.approx((5.0 + 100.0) / 2.0)

    def test_too_few_groups_raises(self) -> None:
        intervals = [script.Interval(0.0, 10.0), script.Interval(100.0, 110.0)]
        with pytest.raises(script.FontExtractionError, match="need at least 94"):
            script.auto_cluster_threshold(intervals, expected_clusters=94)


class TestClusterPaths:
    """Tests for cluster_paths()."""

    def test_three_blobs_scrambled_order(self, three_blob_doc: PLTDocument) -> None:
        clusters = script.cluster_paths(three_blob_doc.stroke_paths, threshold=1350.0)
        assert len(clusters) == 3
        centers = [c.center_x for c in clusters]
        assert centers == sorted(centers)
        # The parser folds the mid-path arc into the preceding polyline, so
        # each blob is a single 3-segment path (2 lines + 1 arc).
        assert [c.path_count for c in clusters] == [1, 1, 1]
        assert [sum(len(p.segments) for p in c.paths) for c in clusters] == [3, 3, 3]

    def test_skips_segmentless_paths(self, three_blob_doc: PLTDocument) -> None:
        paths = [StrokePath(segments=())] + list(three_blob_doc.stroke_paths)
        clusters = script.cluster_paths(paths, threshold=1350.0)
        assert len(clusters) == 3

    def test_all_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="empty sequence"):
            script.cluster_paths([StrokePath(segments=())], threshold=10.0)


class TestScale:
    """Tests for the scale helpers."""

    def test_line_scaled_about_origin(self) -> None:
        seg = StrokeSegment(Coordinate(1.0, 2.0), Coordinate(3.0, -4.0), True)
        scaled = script.scale_segment(seg, 20.0)
        assert (scaled.start.x, scaled.start.y) == (20.0, 40.0)
        assert (scaled.end.x, scaled.end.y) == (60.0, -80.0)

    def test_arc_scales_center_radius_preserves_sweep(self) -> None:
        arc = ArcSegment(
            start=Coordinate(10.0, 0.0),
            end=Coordinate(0.0, 10.0),
            center=Coordinate(0.0, 0.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        scaled = script.scale_segment(arc, 2.0)
        assert isinstance(scaled, ArcSegment)
        assert scaled.radius == pytest.approx(20.0)
        assert scaled.sweep_angle == pytest.approx(90.0)

    def test_path_pen_up_scaled(self) -> None:
        path = StrokePath(
            pen_up_position=Coordinate(1.0, -2.0),
            segments=(StrokeSegment(Coordinate(1.0, 1.0), Coordinate(2.0, 2.0), True),),
        )
        scaled = script.scale_path(path, 10.0)
        assert scaled.pen_up_position is not None
        assert (scaled.pen_up_position.x, scaled.pen_up_position.y) == (10.0, -20.0)

    def test_document_scale_normalizes_glyph_size(self, three_blob_doc: PLTDocument) -> None:
        # Blobs are 300 units tall-ish; scale=20 doubles every coordinate
        # after centering.
        plain, _ = script.extract_font_from_document(three_blob_doc, "ABC")
        scaled, _ = script.extract_font_from_document(three_blob_doc, "ABC", scale=20.0)
        for ch in "ABC":
            assert len(scaled[ch]) >= len(plain[ch])
        reparsed = PLTParser().parse_string(scaled["A"])
        xs = [s.end.x for p in reparsed.stroke_paths for s in p.segments]
        ys = [s.end.y for p in reparsed.stroke_paths for s in p.segments]
        assert (max(xs) - min(xs)) > 1000.0  # 300*20 = 6000-wide blob
        assert (max(ys) - min(ys)) > 1000.0


class TestTranslate:
    """Tests for the translate helpers."""

    def test_line_shifted(self) -> None:
        seg = StrokeSegment(Coordinate(1.0, 2.0), Coordinate(3.0, 4.0), True)
        moved = script.translate_segment(seg, 10.5, -2.25)
        assert (moved.start.x, moved.start.y) == (11.5, -0.25)
        assert (moved.end.x, moved.end.y) == (13.5, 1.75)

    def test_arc_center_shifts_sweep_preserved(self) -> None:
        arc = ArcSegment(
            start=Coordinate(0.0, 0.0),
            end=Coordinate(10.0, 0.0),
            center=Coordinate(5.0, -100.0),
            sweep_angle=11.25,
            is_cutting=True,
        )
        moved = script.translate_segment(arc, -5.0, 100.0)
        assert isinstance(moved, ArcSegment)
        assert (moved.center.x, moved.center.y) == (0.0, 0.0)
        assert moved.sweep_angle == pytest.approx(11.25)
        assert moved.is_cutting

    def test_path_pen_up_shifted(self) -> None:
        path = StrokePath(
            pen_up_position=Coordinate(1.0, 1.0),
            segments=(StrokeSegment(Coordinate(1.0, 1.0), Coordinate(2.0, 2.0), True),),
        )
        moved = script.translate_path(path, 10.0, 20.0)
        assert moved.pen_up_position is not None
        assert (moved.pen_up_position.x, moved.pen_up_position.y) == (11.0, 21.0)


class TestEmitGlyph:
    """Tests for emit_glyph()/format_segment()."""

    def test_arc_command_format(self) -> None:
        arc = ArcSegment(
            start=Coordinate(0.0, 0.0),
            end=Coordinate(1.0, 0.0),
            center=Coordinate(0.5, -100.0),
            sweep_angle=-0.5,
            is_cutting=True,
        )
        assert script.format_segment(arc) == "PD;AA0.500,-100.000,-0.500"

    def test_line_command_format(self) -> None:
        seg = StrokeSegment(Coordinate(0.0, 0.0), Coordinate(1.5, -2.0), True)
        assert script.format_segment(seg) == "PD1.500,-2.000"

    def test_paths_are_pu_led_and_semicolon_terminated(self) -> None:
        path = StrokePath(
            pen_up_position=Coordinate(-1.0, -1.0),
            segments=(StrokeSegment(Coordinate(0.0, 0.0), Coordinate(1.0, 0.0), True),),
        )
        glyph = script.emit_glyph([path])
        assert glyph.startswith("PU-1.000,-1.000;")
        assert glyph.endswith(";")

    def test_missing_pen_up_uses_first_point(self) -> None:
        path = StrokePath(
            pen_up_position=None,
            segments=(StrokeSegment(Coordinate(3.0, 4.0), Coordinate(5.0, 6.0), True),),
        )
        assert script.emit_glyph([path]).startswith("PU3.000,4.000;")

    def test_empty_glyph_is_empty_string(self) -> None:
        assert script.emit_glyph([StrokePath(segments=())]) == ""

    def test_glyph_round_trips_through_parser(self, three_blob_doc: PLTDocument) -> None:
        clusters = script.cluster_paths(three_blob_doc.stroke_paths, threshold=1350.0)
        for cluster in clusters:
            glyph = script.emit_glyph(cluster.paths)
            reparsed = PLTParser().parse_string(glyph)
            assert len([p for p in reparsed.stroke_paths if p.segments]) == cluster.path_count


class TestExtractFontFromDocument:
    """Tests for the document-level extraction pipeline."""

    def test_three_glyphs_centered_at_origin(self, three_blob_doc: PLTDocument) -> None:
        glyphs, threshold = script.extract_font_from_document(three_blob_doc, "ABC")
        assert sorted(glyphs) == ["A", "B", "C"]
        assert threshold > 0.0
        for glyph in glyphs.values():
            assert glyph
            reparsed = PLTParser().parse_string(glyph)
            xs: List[float] = []
            ys: List[float] = []
            for path in reparsed.stroke_paths:
                for seg in path.segments:
                    xs.extend([seg.start.x, seg.end.x])
                    ys.extend([seg.start.y, seg.end.y])
            assert (min(xs) + max(xs)) / 2.0 == pytest.approx(0.0, abs=0.002)
            assert (min(ys) + max(ys)) / 2.0 == pytest.approx(0.0, abs=0.002)

    def test_manual_threshold_override(self, three_blob_doc: PLTDocument) -> None:
        # A threshold of 5000 merges everything into one cluster -> mismatch.
        with pytest.raises(script.FontExtractionError, match="1 glyph groups"):
            script.extract_font_from_document(three_blob_doc, "ABC", cluster_threshold=5000.0)

    def test_count_mismatch_names_both_counts(self, three_blob_doc: PLTDocument) -> None:
        with pytest.raises(script.FontExtractionError) as excinfo:
            script.extract_font_from_document(three_blob_doc, "AB", cluster_threshold=1350.0)
        message = str(excinfo.value)
        assert "3 glyph groups" in message
        assert "2" in message

    def test_geometry_free_document_raises(self) -> None:
        with pytest.raises(script.FontExtractionError, match="no stroke geometry"):
            script.extract_font_from_document(PLTDocument(), "AB")


class TestExtractFontFile:
    """Tests for extract_font_file() including the real-word guard rail."""

    def test_font_name_and_height_from_file_name(self, tmp_path: Path) -> None:
        plt_path = tmp_path / "DINO 0.05.plt"
        plt_path.write_text(make_blob_plt([0.0, 3000.0, 6000.0]), encoding="utf-8")
        extraction = script.extract_font_file(plt_path, "ABC")
        assert extraction.font_name == "Dino"
        assert extraction.text_height == pytest.approx(0.05)
        assert sorted(extraction.glyphs) == ["A", "B", "C"]
        # Glyphs are scaled by 1/0.05 = 20x: the 300-unit-wide blob is 6000+.
        reparsed = PLTParser().parse_string(extraction.glyphs["A"])
        xs = [s.end.x for p in reparsed.stroke_paths for s in p.segments]
        assert max(xs) - min(xs) > 1000.0

    def test_font_name_override_keeps_file_name_height(self, tmp_path: Path) -> None:
        plt_path = tmp_path / "whatever 2.plt"
        plt_path.write_text(make_blob_plt([0.0, 3000.0, 6000.0]), encoding="utf-8")
        extraction = script.extract_font_file(plt_path, "ABC", font_name="My Font")
        assert extraction.font_name == "My Font"
        assert extraction.text_height == pytest.approx(2.0)

    def test_text_height_argument_overrides_file_name(self, tmp_path: Path) -> None:
        plt_path = tmp_path / "plain.plt"
        plt_path.write_text(make_blob_plt([0.0, 3000.0, 6000.0]), encoding="utf-8")
        extraction = script.extract_font_file(plt_path, "ABC", text_height=0.5)
        assert extraction.font_name == "Plain"
        assert extraction.text_height == pytest.approx(0.5)

    def test_missing_height_in_file_name_raises(self, tmp_path: Path) -> None:
        plt_path = tmp_path / "noheight.plt"
        plt_path.write_text(make_blob_plt([0.0, 3000.0, 6000.0]), encoding="utf-8")
        with pytest.raises(script.FontExtractionError, match="no text height found"):
            script.extract_font_file(plt_path, "ABC")

    def test_word_engrave_fixture_is_rejected(self) -> None:
        """dino_word_sample.plt is a word engraving, not a full ASCII row.

        It must be rejected with the cluster-count error instead of being
        silently mis-mapped onto the 94-character list.
        """
        assert DINO_FIXTURE.exists(), "missing fixture tests_deps/dino_word_sample.plt"
        with pytest.raises(script.FontExtractionError) as excinfo:
            script.extract_font_file(DINO_FIXTURE, ASCII_CHARS, text_height=0.05)
        message = str(excinfo.value)
        assert "separated stroke groups" in message
        assert "94" in message


class TestJsonRoundTrip:
    """Tests for load_existing_fonts()/write_fonts_json()."""

    def test_missing_file_loads_empty(self, tmp_path: Path) -> None:
        assert script.load_existing_fonts(tmp_path / "nope.json") == {}

    def test_write_orders_fonts_and_characters(self, tmp_path: Path) -> None:
        out = tmp_path / "fonts.json"
        fonts = {
            "Zed": {"B": "b;", "A": "a;", "extra": "e;"},
            "Abe": {"C": "c;", "A": "a;"},
        }
        script.write_fonts_json(out, fonts, characters=["A", "B", "C"])
        data = json.loads(out.read_text(encoding="utf-8"))
        assert list(data) == ["Abe", "Zed"]
        assert list(data["Zed"]) == ["A", "B", "extra"]

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "fonts.json"
        bad.write_text("[1, 2]", encoding="utf-8")
        with pytest.raises(script.FontExtractionError, match="must contain a JSON object"):
            script.load_existing_fonts(bad)

    def test_malformed_font_value_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "fonts.json"
        bad.write_text('{"Font": "not-a-dict"}', encoding="utf-8")
        with pytest.raises(script.FontExtractionError, match="single-character string keys"):
            script.load_existing_fonts(bad)

    def test_non_string_glyph_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "fonts.json"
        bad.write_text('{"Font": {"A": 3}}', encoding="utf-8")
        with pytest.raises(script.FontExtractionError):
            script.load_existing_fonts(bad)


class TestMainCli:
    """End-to-end tests for the CLI entry point."""

    def _make_fonts_dir(self, tmp_path: Path) -> Path:
        fonts_dir = tmp_path / "fonts"
        fonts_dir.mkdir()
        (fonts_dir / "myfont 1.plt").write_text(
            make_blob_plt([0.0, 3000.0, 6000.0], scrambled=True), encoding="utf-8"
        )
        return fonts_dir

    def _ascii_file(self, tmp_path: Path, text: str = "A B C") -> Path:
        ascii_file = tmp_path / "ascii.txt"
        ascii_file.write_text(text, encoding="utf-8")
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
        assert sorted(data["Myfont"]) == ["A", "B", "C"]

    def test_merge_preserves_other_fonts(self, tmp_path: Path) -> None:
        fonts_dir = self._make_fonts_dir(tmp_path)
        ascii_file = self._ascii_file(tmp_path)
        out = tmp_path / "out.json"
        args = [
            "--fonts-dir",
            str(fonts_dir),
            "--ascii-file",
            str(ascii_file),
            "--output",
            str(out),
        ]
        assert script.main(args) == 0
        (fonts_dir / "other 1.plt").write_text(
            make_blob_plt([0.0, 3000.0, 6000.0]), encoding="utf-8"
        )
        assert script.main(args) == 0
        data = json.loads(out.read_text(encoding="utf-8"))
        assert sorted(data) == ["Myfont", "Other"]

    def test_rebuild_drops_missing_fonts(self, tmp_path: Path) -> None:
        fonts_dir = self._make_fonts_dir(tmp_path)
        ascii_file = self._ascii_file(tmp_path)
        out = tmp_path / "out.json"
        base = [
            "--fonts-dir",
            str(fonts_dir),
            "--ascii-file",
            str(ascii_file),
            "--output",
            str(out),
        ]
        assert script.main(base) == 0
        (fonts_dir / "myfont 1.plt").unlink()
        (fonts_dir / "other 1.plt").write_text(
            make_blob_plt([0.0, 3000.0, 6000.0]), encoding="utf-8"
        )
        assert script.main(base + ["--rebuild"]) == 0
        data = json.loads(out.read_text(encoding="utf-8"))
        assert sorted(data) == ["Other"]

    def test_count_mismatch_exits_nonzero_and_keeps_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        fonts_dir = tmp_path / "fonts"
        fonts_dir.mkdir()
        (fonts_dir / "bad 1.plt").write_text(make_blob_plt([0.0, 3000.0]), encoding="utf-8")
        out = tmp_path / "out.json"
        with caplog.at_level(logging.INFO, logger="extract_plt_fonts"):
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
        assert any("left unchanged" in record.message for record in caplog.records)

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

    def test_font_name_requires_single_file(self, tmp_path: Path) -> None:
        fonts_dir = self._make_fonts_dir(tmp_path)
        (fonts_dir / "second 1.plt").write_text(
            make_blob_plt([0.0, 3000.0, 6000.0]), encoding="utf-8"
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

    def test_font_name_override_single_file(self, tmp_path: Path) -> None:
        fonts_dir = self._make_fonts_dir(tmp_path)
        out = tmp_path / "out.json"
        rc = script.main(
            [
                "--fonts-dir",
                str(fonts_dir),
                "--ascii-file",
                str(self._ascii_file(tmp_path)),
                "--output",
                str(out),
                "--font-name",
                "Custom Name",
            ]
        )
        assert rc == 0
        assert sorted(json.loads(out.read_text(encoding="utf-8"))) == ["Custom Name"]
