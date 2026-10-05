"""Tests for ``Fonts/glyph_transforms.py`` (HPGL glyph utilities + derived glyphs).

The module is not part of the installed package, so it is loaded via
``importlib`` (mirroring ``tests/test_extract_plt_fonts.py``). All fixtures are
synthetic glyph strings with exactly known extents, so every derived bounding
box is asserted numerically. The reference character ``E`` is a 500x1000 box,
giving cap = 1000 and midline = 500 in every recipe test.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "Fonts" / "glyph_transforms.py"


def _load_module() -> Any:
    """Import ``Fonts/glyph_transforms.py`` as a module by path.

    The module is registered in ``sys.modules`` before execution so
    ``@dataclass`` can resolve its own string annotations.

    Returns:
        The loaded module.
    """
    spec = importlib.util.spec_from_file_location("glyph_transforms", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["glyph_transforms"] = module
    spec.loader.exec_module(module)
    return module


gt = _load_module()

from plt_optimizer.core.models import ArcSegment, StrokePath  # noqa: E402
from plt_optimizer.core.parser import PLTParser  # noqa: E402

# Synthetic ASCII bases (storage frame: +y up, baseline y = 0, left edge x = 0).
# Reference 'E' spans 0..500 x 0..1000 -> cap 1000, midline 500.
BASE_GLYPHS: Dict[str, str] = {
    "-": "PU0.0000,300.0000;PD400.0000,300.0000;",
    ".": "PU0.0000,0.0000;PD0.0000,100.0000;",
    "8": "PU0.0000,0.0000;PD600.0000,0.0000;PD600.0000,1000.0000;PD0.0000,1000.0000;"
    "PD0.0000,0.0000;",
    "?": "PU0.0000,0.0000;PD500.0000,0.0000;PD500.0000,1000.0000;PD0.0000,1000.0000;"
    "PD0.0000,0.0000;",
    "!": "PU0.0000,0.0000;PD0.0000,1000.0000;",
    "+": "PU0.0000,300.0000;PD400.0000,300.0000;PU200.0000,100.0000;PD200.0000,500.0000;",
    "_": "PU0.0000,-100.0000;PD500.0000,-100.0000;",
    "|": "PU0.0000,-100.0000;PD0.0000,1100.0000;",
    "c": "PU0.0000,0.0000;PD500.0000,0.0000;PD500.0000,600.0000;PD0.0000,600.0000;PD0.0000,0.0000;",
    "/": "PU0.0000,0.0000;PD600.0000,1000.0000;",
    "=": "PU0.0000,200.0000;PD500.0000,200.0000;PU0.0000,400.0000;PD500.0000,400.0000;",
    "~": "PU0.0000,400.0000;PD350.0000,600.0000;PD700.0000,400.0000;",
    "^": "PU0.0000,600.0000;PD300.0000,1000.0000;PD600.0000,600.0000;",
    "<": "PU600.0000,100.0000;PD0.0000,500.0000;PD600.0000,900.0000;",
    ">": "PU0.0000,100.0000;PD600.0000,500.0000;PD0.0000,900.0000;",
    "E": "PU0.0000,0.0000;PD500.0000,0.0000;PD500.0000,1000.0000;PD0.0000,1000.0000;"
    "PD0.0000,0.0000;",
}

# One 90-degree arc: pen at (100, 0), center (0, 0), sweep +90 (CCW).
ARC_GLYPH = "PU100.0000,0.0000;PD;AA0.0000,0.0000,90.0000;"


def _entry(glyph: str, bounds: Optional[Tuple[float, float, float, float]] = None) -> Any:
    """Build a minimal font entry (only ``glyph`` and ``bounding_box`` are read)."""
    if bounds is None:
        bounds = gt.glyph_bounds(glyph)
    if bounds is None:  # geometry-less glyph (pen-up only)
        bounding_box: Dict[str, float] = {}
    else:
        bounding_box = {
            "min_x": bounds[0],
            "max_x": bounds[2],
            "min_y": bounds[1],
            "max_y": bounds[3],
        }
    return {
        "bounding_box": bounding_box,
        "left_envelope": [],
        "right_envelope": [],
        "glyph": glyph,
    }


def _characters(bases: Dict[str, str] = BASE_GLYPHS) -> Dict[str, Any]:
    """Build a ``characters`` mapping from glyph strings."""
    return {char: _entry(glyph) for char, glyph in bases.items()}


def _derive(
    characters: Dict[str, Any],
    log: Any = None,
) -> Dict[str, Any]:
    """Derive with the standard 30-sample envelope count."""
    return gt.derive_glyph_entries(characters, reference_char="E", envelope_samples=30, log=log)


def _bbox(entry: Dict[str, Any]) -> Tuple[float, float, float, float]:
    """Return an entry's bounding box as a tuple."""
    box = entry["bounding_box"]
    return (box["min_x"], box["min_y"], box["max_x"], box["max_y"])


class TestAffine:
    """Arithmetic of the Affine dataclass."""

    def test_identity_apply(self) -> None:
        affine = gt.Affine.identity()
        assert affine.apply(3.5, -2.25) == (3.5, -2.25)
        assert affine.determinant == 1.0
        assert affine.sweep_sign == 1.0

    def test_translation(self) -> None:
        affine = gt.Affine.translation(10.0, -4.0)
        assert affine.apply(1.0, 1.0) == (11.0, -3.0)

    def test_scaling_about_point(self) -> None:
        affine = gt.Affine.scaling(2.0, 3.0, (10.0, 10.0))
        assert affine.apply(10.0, 10.0) == pytest.approx((10.0, 10.0))
        assert affine.apply(11.0, 11.0) == pytest.approx((12.0, 13.0))

    def test_rotation_about_point(self) -> None:
        affine = gt.Affine.rotation(90.0, (0.0, 0.0))
        x, y = affine.apply(1.0, 0.0)
        assert (x, y) == pytest.approx((0.0, 1.0))
        assert affine.determinant == pytest.approx(1.0)

    def test_mirror_sweep_sign(self) -> None:
        assert gt.Affine.mirror_y(0.0).sweep_sign == -1.0
        assert gt.Affine.mirror_x(100.0).sweep_sign == -1.0
        assert gt.Affine.mirror_y(10.0).apply(0.0, 3.0) == (0.0, 7.0)
        assert gt.Affine.mirror_x(10.0).apply(3.0, 0.0) == (7.0, 0.0)

    def test_map_bounds_rotates_box(self) -> None:
        affine = gt.Affine.rotation(90.0, (0.0, 0.0))
        assert affine.map_bounds((0.0, 0.0, 600.0, 1000.0)) == pytest.approx(
            (-1000.0, 0.0, 0.0, 600.0)
        )

    def test_then_composes_translation_then_scale(self) -> None:
        composed = gt.Affine.translation(1.0, 0.0).then(gt.Affine.scaling(2.0, 2.0))
        assert composed.apply(1.0, 1.0) == pytest.approx((4.0, 2.0))
        # Order matters: scale first, then translate.
        reverse = gt.Affine.scaling(2.0, 2.0).then(gt.Affine.translation(1.0, 0.0))
        assert reverse.apply(1.0, 1.0) == pytest.approx((3.0, 2.0))


class TestParseAndEmit:
    """Parsing, emitting and numeric formatting."""

    def test_round_trip_preserves_geometry(self) -> None:
        geometry = gt.parse_glyph(BASE_GLYPHS["+"])
        assert geometry.bounds == (0.0, 100.0, 400.0, 500.0)
        emitted = gt.emit_glyph_geometry(geometry)
        reparsed = gt.parse_glyph(emitted)
        assert reparsed.bounds == pytest.approx(geometry.bounds)
        total = sum(len(path.segments) for path in geometry.paths)
        assert total == 2

    def test_empty_inputs(self) -> None:
        assert gt.parse_glyph("").is_empty
        assert gt.parse_glyph("   ").is_empty
        assert gt.emit_glyph_geometry(gt.parse_glyph("")) == ""

    def test_parse_error_is_wrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from plt_optimizer.core.parser import ParseError

        class _RaisingParser:
            def parse_string(self, text: str) -> Any:
                raise ParseError("boom")

        monkeypatch.setattr(gt, "PLTParser", _RaisingParser)
        with pytest.raises(gt.GlyphTransformError, match="Cannot parse glyph"):
            gt.parse_glyph("PU0.0000,0.0000;PD1.0000,1.0000;")

    def test_format_number_normalizes_negative_zero(self) -> None:
        assert gt.format_number(-0.00001) == "0.0000"
        assert gt.format_number(1234.56785) == "1234.5678"  # banker's rounding stable
        assert gt.format_number(5.0) == "5.0000"

    def test_has_arcs(self) -> None:
        assert gt.parse_glyph(ARC_GLYPH).has_arcs
        assert not gt.parse_glyph(BASE_GLYPHS["-"]).has_arcs

    def test_path_bounds_requires_segments(self) -> None:
        with pytest.raises(ValueError, match="without segments"):
            gt.path_bounds(StrokePath(segments=()))

    def test_union_bounds_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="empty sequence"):
            gt.union_bounds([])


class TestSweepRules:
    """Arc sweep handling under transforms (the AA contract)."""

    def _sweeps(self, text: str) -> List[float]:
        document = PLTParser().parse_string(text)
        return [
            seg.sweep_angle
            for path in document.stroke_paths
            for seg in path.segments
            if isinstance(seg, ArcSegment)
        ]

    def test_rotation_keeps_sweep(self) -> None:
        rotated = gt.rotate_glyph(ARC_GLYPH, degrees=90.0)
        assert self._sweeps(rotated) == pytest.approx([90.0])

    def test_uniform_scale_keeps_sweep(self) -> None:
        scaled = gt.scale_glyph(ARC_GLYPH, scale_x=2.0, scale_y=2.0)
        assert self._sweeps(scaled) == pytest.approx([90.0])

    def test_mirror_negates_sweep(self) -> None:
        mirrored = gt.mirror_glyph_y(ARC_GLYPH, span=0.0)
        assert self._sweeps(mirrored) == pytest.approx([-90.0])

    def test_non_uniform_scale_of_arc_raises(self) -> None:
        with pytest.raises(gt.GlyphTransformError, match="non-uniformly scale"):
            gt.scale_glyph(ARC_GLYPH, scale_x=2.0)

    def test_non_uniform_scale_of_lines_allowed(self) -> None:
        stretched = gt.scale_glyph(BASE_GLYPHS["-"], scale_x=2.0)
        assert gt.glyph_bounds(stretched) == pytest.approx((0.0, 300.0, 800.0, 300.0))


class TestUtilities:
    """The public string -> string utilities."""

    def test_translate(self) -> None:
        moved = gt.translate_glyph(BASE_GLYPHS["-"], dx=10.0, dy=-5.0)
        assert gt.glyph_bounds(moved) == pytest.approx((10.0, 295.0, 410.0, 295.0))

    def test_scale_about_center(self) -> None:
        # Hyphen 0..400 stretched 2x about its right end (400, 300): the pivot
        # stays put and the left end runs off to -400.
        stretched = gt.scale_glyph(BASE_GLYPHS["-"], scale_x=2.0, about=(400.0, 300.0))
        assert gt.glyph_bounds(stretched) == pytest.approx((-400.0, 300.0, 400.0, 300.0))

    def test_rotate_defaults_to_bbox_center(self) -> None:
        rotated = gt.rotate_glyph(BASE_GLYPHS["^"], degrees=180.0)
        assert gt.glyph_bounds(rotated) == pytest.approx((0.0, 600.0, 600.0, 1000.0))

    def test_concat_superimposes(self) -> None:
        combined = gt.concat_glyphs(
            BASE_GLYPHS["="], gt.translate_glyph(BASE_GLYPHS["/"], dx=-50.0)
        )
        assert gt.glyph_bounds(combined) == pytest.approx((-50.0, 0.0, 550.0, 1000.0))
        document = PLTParser().parse_string(combined)
        assert sum(len(path.segments) for path in document.stroke_paths) == 3

    def test_concat_ignores_empties(self) -> None:
        assert gt.concat_glyphs("", BASE_GLYPHS["-"], "") == BASE_GLYPHS["-"]
        assert gt.concat_glyphs("", "") == ""

    def test_transform_glyph_empty(self) -> None:
        assert gt.transform_glyph("", gt.Affine.translation(5.0, 5.0)) == ""

    def test_glyph_bounds_empty(self) -> None:
        assert gt.glyph_bounds("") is None


class TestBandEnvelopes:
    """The shared band-aggregated envelope sampler."""

    def test_requires_two_samples(self) -> None:
        with pytest.raises(ValueError, match="at least 2 samples"):
            gt.band_envelopes([], [], (0.0, 0.0, 1.0, 1.0), 1)

    def test_no_geometry_returns_empty(self) -> None:
        assert gt.band_envelopes([], [], (0.0, 0.0, 1.0, 1.0), 30) == ([], [])

    def test_hairline_never_missed(self) -> None:
        # A single zero-width horizontal bar must register on every band.
        lines = [(0.0, 500.2923, 100.0, 500.2923)]
        left, right = gt.band_envelopes(lines, [], (0.0, 500.2923, 100.0, 500.2923), 30)
        assert len(left) == len(right) == 30
        assert all(point[0] == 0.0 for point in left)
        assert all(point[0] == 100.0 for point in right)

    def test_arc_extremes_are_analytic(self) -> None:
        # Quarter circle: center (0,0), radius 100, 0..90 degrees.
        arcs = [(0.0, 0.0, 100.0, 0.0, math.pi / 2.0)]
        left, right = gt.band_envelopes([], arcs, (0.0, 0.0, 100.0, 100.0), 11)
        # Rightmost point of the swept arc at y = 0 is x = 100.
        assert right[0][0] == pytest.approx(100.0)
        # Topmost sample is the arc endpoint at x = 0.
        assert left[-1][0] == pytest.approx(0.0)

    def test_interpolate_gaps(self) -> None:
        assert gt.interpolate_gaps([0.0, None, 10.0]) == [0.0, 5.0, 10.0]
        assert gt.interpolate_gaps([None, 4.0, None]) == [4.0, 4.0, 4.0]
        with pytest.raises(ValueError, match="no defined samples"):
            gt.interpolate_gaps([None, None])


class TestRecipes:
    """Every derived glyph, on synthetic exact-extent bases (cap 1000, mid 500)."""

    @pytest.fixture()
    def derived(self) -> Dict[str, Any]:
        return _derive(_characters())

    def test_en_dash(self, derived: Dict[str, Any]) -> None:
        assert _bbox(derived["\u2013"]) == pytest.approx((0.0, 300.0, 800.0, 300.0))

    def test_em_dash(self, derived: Dict[str, Any]) -> None:
        assert _bbox(derived["\u2014"]) == pytest.approx((0.0, 300.0, 1200.0, 300.0))

    def test_bullet_centred_on_midline(self, derived: Dict[str, Any]) -> None:
        assert _bbox(derived["\u2022"]) == pytest.approx((0.0, 450.0, 0.0, 550.0))

    def test_infinity_swaps_the_eights_extents(self, derived: Dict[str, Any]) -> None:
        # 600x1000 box rotated 90 degrees -> 1000x600, centred on the midline.
        assert _bbox(derived["\u221e"]) == pytest.approx((0.0, 200.0, 1000.0, 800.0))

    def test_plus_minus_touches_and_centres(self, derived: Dict[str, Any]) -> None:
        box = _bbox(derived["\u00b1"])
        assert box == pytest.approx((0.0, 300.0, 500.0, 700.0))
        document = PLTParser().parse_string(derived["\u00b1"]["glyph"])
        horizontals = sorted(
            seg.start.y
            for path in document.stroke_paths
            for seg in path.segments
            if abs(seg.start.y - seg.end.y) < 1e-9
        )
        # Underscore at the union's bottom (300), plus bar at its centre (500).
        assert horizontals == pytest.approx([300.0, 500.0])
        vertical_bottoms = [
            min(seg.start.y, seg.end.y)
            for path in document.stroke_paths
            for seg in path.segments
            if abs(seg.start.x - seg.end.x) < 1e-9
        ]
        # The plus's vertical stem bottoms out exactly on the underscore.
        assert min(vertical_bottoms) == pytest.approx(300.0)

    def test_cent_bar_half_height_centred(self, derived: Dict[str, Any]) -> None:
        # c: 0..600 high; bar: 1200 tall halved about the c centre -> 100..700,
        # union 0..700 lifted to midline -> 150..850.
        box = _bbox(derived["\u00a2"])
        assert box == pytest.approx((0.0, 150.0, 500.0, 850.0))
        document = PLTParser().parse_string(derived["\u00a2"]["glyph"])
        bar = [
            seg
            for path in document.stroke_paths
            for seg in path.segments
            if abs(seg.start.x - seg.end.x) < 1e-9 and abs(seg.start.x - 250.0) < 1e-9
        ]
        assert len(bar) == 1
        assert sorted([bar[0].start.y, bar[0].end.y]) == pytest.approx([250.0, 850.0])

    def test_not_equal_slash_over_equals(self, derived: Dict[str, Any]) -> None:
        # Slash (600 wide) x-centred on '=' (500 wide) -> overhangs 50 each side.
        assert _bbox(derived["\u2260"]) == pytest.approx((0.0, 0.0, 600.0, 1000.0))

    def test_almost_equal_stacks_tildas(self, derived: Dict[str, Any]) -> None:
        # Tilda 200 tall; copies centred at 600 and 400 -> union 300..700.
        assert _bbox(derived["\u2248"]) == pytest.approx((0.0, 300.0, 700.0, 700.0))

    def test_identical_bars_at_quarter_cap(self, derived: Dict[str, Any]) -> None:
        box = _bbox(derived["\u2261"])
        assert box == pytest.approx((0.0, 250.0, 800.0, 750.0))
        document = PLTParser().parse_string(derived["\u2261"]["glyph"])
        bar_ys = sorted(
            seg.start.y
            for path in document.stroke_paths
            for seg in path.segments
            if abs(seg.start.y - seg.end.y) < 1e-9
        )
        assert bar_ys == pytest.approx([250.0, 500.0, 750.0])

    def test_inverted_question_lands_on_baseline(self, derived: Dict[str, Any]) -> None:
        assert _bbox(derived["\u00bf"]) == pytest.approx((0.0, 0.0, 500.0, 1000.0))

    def test_inverted_exclamation_descends(self, derived: Dict[str, Any]) -> None:
        # Mirror ! (0..1000) in y=0 -> -1000..0, descend 10% cap -> -1100..-100.
        assert _bbox(derived["\u00a1"]) == pytest.approx((0.0, -1100.0, 0.0, -100.0))

    def test_dagger_top_at_capline(self, derived: Dict[str, Any]) -> None:
        # Stem -100..1100 halved about centre -> 200..800, lifted to cap -> 400..1000.
        assert _bbox(derived["\u2020"]) == pytest.approx((0.0, 400.0, 200.0, 1000.0))

    def test_double_dagger_top_at_capline(self, derived: Dict[str, Any]) -> None:
        assert _bbox(derived["\u2021"]) == pytest.approx((0.0, 400.0, 200.0, 1000.0))
        document = PLTParser().parse_string(derived["\u2021"]["glyph"])
        crosses = [
            seg.start.y
            for path in document.stroke_paths
            for seg in path.segments
            if abs(seg.start.y - seg.end.y) < 1e-9
        ]
        assert len(crosses) == 2

    def test_arrow_up_caret_tops_stem(self, derived: Dict[str, Any]) -> None:
        # Stem -100..1100; caret (400 tall) base at the stem tip -> -100..1500.
        assert _bbox(derived["\u2191"]) == pytest.approx((0.0, -100.0, 600.0, 1500.0))

    def test_arrow_down_caret_under_stem(self, derived: Dict[str, Any]) -> None:
        assert _bbox(derived["\u2193"]) == pytest.approx((0.0, -500.0, 600.0, 1100.0))

    def test_arrow_left_chevron_leads(self, derived: Dict[str, Any]) -> None:
        # Em dash 1200 + chevron 600 touching at x = 0 (re-anchored).
        box = _bbox(derived["\u2190"])
        assert box == pytest.approx((0.0, 100.0, 1800.0, 900.0))
        document = PLTParser().parse_string(derived["\u2190"]["glyph"])
        midline_xs = [
            coord.x
            for path in document.stroke_paths
            for seg in path.segments
            for coord in (seg.start, seg.end)
            if abs(coord.y - 500.0) < 1e-9
        ]
        # The chevron's tip (its leftmost midline point) touches the shaft.
        assert min(midline_xs) == pytest.approx(0.0)
        assert max(midline_xs) == pytest.approx(1800.0)

    def test_arrow_right_chevron_trails(self, derived: Dict[str, Any]) -> None:
        assert _bbox(derived["\u2192"]) == pytest.approx((0.0, 100.0, 1800.0, 900.0))

    def test_all_recipes_re_emit_parseable_glyphs(self, derived: Dict[str, Any]) -> None:
        assert len(derived) == len(gt.DERIVED_GLYPHS)
        for char, entry in derived.items():
            document = PLTParser().parse_string(entry["glyph"])
            assert any(path.segments for path in document.stroke_paths), char
            assert entry["bounding_box"]["min_x"] == 0.0, char
            assert len(entry["left_envelope"]) == 30, char
            assert len(entry["right_envelope"]) == 30, char
            ys = [point[1] for point in entry["left_envelope"]]
            assert ys == sorted(ys), char

    def test_envelope_spans_bbox_height(self, derived: Dict[str, Any]) -> None:
        box = derived["\u221e"]["bounding_box"]
        left = derived["\u221e"]["left_envelope"]
        assert left[0][1] == pytest.approx(box["min_y"])
        assert left[-1][1] == pytest.approx(box["max_y"])
        # Rotated 8: the left silhouette is a flat side, not the source's.
        assert all(point[0] == pytest.approx(0.0) for point in left[:10])


class TestDeriveGlyphEntries:
    """Skip/merge semantics of the derivation entry point."""

    def test_full_derivation(self) -> None:
        derived = _derive(_characters())
        expected = {recipe.character for recipe in gt.DERIVED_GLYPHS}
        assert set(derived) == expected

    def test_missing_base_skips_with_warning(self) -> None:
        bases = dict(BASE_GLYPHS)
        del bases["|"]
        log = _RecordingLogger()
        derived = _derive(_characters(bases), log=log)
        # | drives cent, both daggers and both arrows.
        assert set(derived) == {
            recipe.character for recipe in gt.DERIVED_GLYPHS if "|" not in recipe.bases
        }
        warnings = [message for level, message in log.records if level == "warning"]
        assert any("'|' unavailable" in message for message in warnings)

    def test_existing_key_never_overwritten(self) -> None:
        characters = _characters()
        sentinel = _entry(BASE_GLYPHS["-"])
        sentinel["glyph"] = "SENTINEL"
        characters["\u2013"] = sentinel
        log = _RecordingLogger()
        derived = _derive(characters, log=log)
        assert "\u2013" not in derived
        assert any("Keeping existing character" in message for _, message in log.records)

    def test_arc_base_blocks_stretching_recipes(self) -> None:
        bases = dict(BASE_GLYPHS)
        bases["-"] = ARC_GLYPH  # hyphen with arcs cannot be stretched
        log = _RecordingLogger()
        derived = _derive(_characters(bases), log=log)
        for char in ("\u2013", "\u2014", "\u2261", "\u2190", "\u2192"):
            assert char not in derived
        assert "\u2020" in derived  # dagger scales uniformly -> fine
        warnings = [message for level, message in log.records if level == "warning"]
        assert any("non-uniformly scale" in message for message in warnings)

    def test_cap_fallback_without_reference_box(self) -> None:
        characters = _characters()
        del characters["E"]["bounding_box"]
        assert gt.cap_height(characters, "E") == gt.CAP_HEIGHT_FALLBACK
        derived = _derive(characters)
        # Cap 1000.0 fallback matches the synthetic cap, so identical results.
        assert _bbox(derived["\u2261"]) == pytest.approx((0.0, 250.0, 800.0, 750.0))

    def test_geometry_less_base_skipped(self) -> None:
        bases = dict(BASE_GLYPHS)
        bases["."] = "PU0.0000,0.0000;"  # pen-up only: no segments
        log = _RecordingLogger()
        derived = _derive(_characters(bases), log=log)
        assert "\u2022" not in derived

    def test_recipe_declaring_undeclared_base_raises(self) -> None:
        context = gt.DerivedContext(
            bases={"-": gt.parse_glyph(BASE_GLYPHS["-"])}, cap=1000.0, midline=500.0
        )
        with pytest.raises(gt.GlyphTransformError, match="did not declare"):
            context.g("|")


class _RecordingLogger:
    """Minimal logger double capturing (level, formatted message) records."""

    def __init__(self) -> None:
        self.records: List[Tuple[str, str]] = []

    def warning(self, message: str, *args: Any) -> None:
        self.records.append(("warning", message % args if args else message))

    def info(self, message: str, *args: Any) -> None:
        self.records.append(("info", message % args if args else message))
