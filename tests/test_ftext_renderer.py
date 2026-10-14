"""Unit tests for the single-line TTF text renderer (matplotlib path codes)."""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import List, Optional

import numpy as np
import pytest
import vpype as vp
from matplotlib.path import Path as MplPath

from plt_optimizer.generate import ftext_renderer
from plt_optimizer.generate.ftext_renderer import (
    CHORD_THRESHOLD_INCHES,
    DEFAULT_FONT_PATH,
    FtextRenderError,
    _reference_cap_height,
    _remove_closing_chords,
    _split_contours,
    render_text_line_ftext,
)


def test_default_font_exists() -> None:
    """The bundled Relief Single Line CAD font should exist."""
    assert Path(DEFAULT_FONT_PATH).exists()
    assert str(DEFAULT_FONT_PATH).endswith("ReliefSingleLineCAD-Regular.ttf")


class TestRenderTextLineFtext:
    """Tests for render_text_line_ftext()."""

    def test_empty_string_returns_empty(self) -> None:
        """Empty text should return an empty LineCollection."""
        lc = render_text_line_ftext("", 0.25)
        assert isinstance(lc, vp.LineCollection)
        assert lc.is_empty()

    def test_nonempty_renders_upright_at_target_height(self) -> None:
        """Rendered text should be upright and match target height."""
        target = 1.5
        lc = render_text_line_ftext("HELLO", target, DEFAULT_FONT_PATH)
        assert not lc.is_empty()

        bounds = lc.bounds()
        assert bounds is not None
        rendered_height = bounds[3] - bounds[1]
        # Height should match the requested toolpath height (within tolerance).
        assert math.isclose(rendered_height, target, rel_tol=0.02)

    def test_scale_is_proportional(self) -> None:
        """Doubling the target height should double the rendered geometry."""
        lc_small = render_text_line_ftext("TEST", 1.0, DEFAULT_FONT_PATH)
        lc_large = render_text_line_ftext("TEST", 2.0, DEFAULT_FONT_PATH)

        small_bounds = lc_small.bounds()
        large_bounds = lc_large.bounds()
        assert small_bounds is not None
        assert large_bounds is not None

        small_h = small_bounds[3] - small_bounds[1]
        large_h = large_bounds[3] - large_bounds[1]
        ratio = large_h / small_h if small_h > 0 else 0.0
        assert math.isclose(ratio, 2.0, rel_tol=0.02)

    def test_text_is_upright_not_flipped(self) -> None:
        """Glyphs should extend upward from the baseline (positive Y)."""
        lc = render_text_line_ftext("H", 1.5, DEFAULT_FONT_PATH)
        bounds = lc.bounds()
        assert bounds is not None
        # Baseline sits at y=0; glyphs extend up into positive Y.
        assert bounds[3] > 0
        assert bounds[1] >= -0.01

    def test_custom_font_path(self) -> None:
        """A custom font path should render successfully."""
        lc = render_text_line_ftext("ABC", 1.2, DEFAULT_FONT_PATH)
        assert not lc.is_empty()

    @staticmethod
    def _assert_no_long_closing_segment(lc: vp.LineCollection) -> None:
        """Assert no rendered stroke ends with an erroneous long chord."""
        for line in lc:
            if len(line) > 2 and abs(line[0] - line[-1]) < 1e-3:
                closing = abs(line[-2] - line[-1])
                assert closing <= CHORD_THRESHOLD_INCHES

    def test_open_stroke_chord_is_removed_for_digit_one(self) -> None:
        """The erroneous closing chord on '1' must be removed.

        Regression for the geometry-heuristic bug: digit "1" has an intended
        vertical stem longer than its closing chord, so a longest-segment scan
        wrongly deleted the stem. The matplotlib path-code approach drops only
        the final LINETO-back-to-origin.
        """
        lc = render_text_line_ftext("1", 4.5, DEFAULT_FONT_PATH)
        assert not lc.is_empty()
        self._assert_no_long_closing_segment(lc)

    def test_open_stroke_chord_is_removed_for_digit_four(self) -> None:
        """The erroneous closing chord on '4' must be removed."""
        lc = render_text_line_ftext("4", 4.5, DEFAULT_FONT_PATH)
        assert not lc.is_empty()
        self._assert_no_long_closing_segment(lc)

    def test_open_stroke_chord_is_removed_for_digit_seven(self) -> None:
        """The erroneous closing chord on '7' must be removed."""
        lc = render_text_line_ftext("7", 4.5, DEFAULT_FONT_PATH)
        assert not lc.is_empty()
        self._assert_no_long_closing_segment(lc)

    def test_open_stroke_chord_is_removed_for_c(self) -> None:
        """Erroneous closing chords on open strokes (e.g. 'C') are removed."""
        lc = render_text_line_ftext("C", 4.5, DEFAULT_FONT_PATH)
        assert not lc.is_empty()
        self._assert_no_long_closing_segment(lc)

    def test_digit_one_preserves_intended_stem(self) -> None:
        """Digit '1' must keep its long vertical stem (not delete it)."""
        lc = render_text_line_ftext("1", 4.5, DEFAULT_FONT_PATH)
        assert not lc.is_empty()
        # The intended stroke is the tall stem; after chord removal a segment
        # close to the full glyph height should remain.
        bounds = lc.bounds()
        assert bounds is not None
        glyph_height = bounds[3] - bounds[1]
        max_seg = 0.0
        for line in lc:
            segs = np.abs(np.diff(line))
            if len(segs):
                max_seg = max(max_seg, float(segs.max()))
        # The stem spans most of the glyph height.
        assert max_seg > glyph_height * 0.5

    def test_closed_loop_is_preserved(self) -> None:
        """Genuine closed loops (e.g. 'o') must not be sliced open."""
        lc = render_text_line_ftext("o", 4.5, DEFAULT_FONT_PATH)
        assert not lc.is_empty()
        # The loop's closing segment should remain microscopic.
        for line in lc:
            if len(line) > 2 and abs(line[0] - line[-1]) < 1e-3:
                closing = abs(line[-2] - line[-1])
                assert closing <= CHORD_THRESHOLD_INCHES

    def test_digit_zero_loop_is_preserved(self) -> None:
        """Digit '0' is a genuine closed loop and must stay intact."""
        lc = render_text_line_ftext("0", 4.5, DEFAULT_FONT_PATH)
        assert not lc.is_empty()
        # A genuine loop remains geometrically closed.
        for line in lc:
            if len(line) > 2:
                assert abs(line[0] - line[-1]) < CHORD_THRESHOLD_INCHES


class TestCapHeightReference:
    """Regression: glyph size is a per-font constant, not a per-line ink box.

    The historical renderer scaled *each line's own ink box* to the target
    height, so every glyph on a line shrank whenever that line contained
    descenders, ascenders, or tall punctuation (visible in multi-line labels:
    identical text_height values engraved at visibly different glyph sizes).
    Lines now scale by the font's fixed reference cap height (ink height of
    "H"), mirroring the PLT family's reference-character contract.
    """

    def test_reference_cap_height_is_positive(self) -> None:
        """The bundled font has a usable 'H' reference glyph."""
        assert _reference_cap_height(DEFAULT_FONT_PATH) > 0.0

    def test_caps_line_hits_target_height(self) -> None:
        """A caps-only line's cap height equals the target exactly."""
        target = 0.2
        lc = render_text_line_ftext("H", target, DEFAULT_FONT_PATH)
        bounds = lc.bounds()
        assert bounds is not None
        assert math.isclose(bounds[3], target, rel_tol=1e-9)

    def test_descender_hangs_below_baseline(self) -> None:
        """With a fixed cap reference, descenders extend below y=0."""
        target = 0.5
        lc = render_text_line_ftext("Hp", target, DEFAULT_FONT_PATH)
        bounds = lc.bounds()
        assert bounds is not None
        # Cap top lands at the target; the descender goes below the baseline,
        # so the ink box is TALLER than the target (the old code normalized
        # the whole ink box down to it, shrinking every glyph on the line).
        assert math.isclose(bounds[3], target, rel_tol=1e-9)
        assert bounds[1] < -1e-9
        assert (bounds[3] - bounds[1]) > target

    def test_glyph_height_is_line_independent(self) -> None:
        """The same glyph renders at the same size on any line.

        Direct regression for the reported bug: the digit "0" measured
        different heights on lines whose neighbours had descenders/ascenders.
        """
        target = 0.2

        def first_contour_height(text: str) -> float:
            # Contours follow text order, so contours[0] is the leading "0".
            lc = render_text_line_ftext(text, target, DEFAULT_FONT_PATH)
            assert not lc.is_empty()
            first = np.asarray(next(iter(lc)))
            return float(first.imag.max() - first.imag.min())

        # "0" alone, in a descender line, and in a tall-punctuation line all
        # render the digit at the cap height (digits sit on the baseline).
        plain = first_contour_height("0")
        descender = first_contour_height("0pg")
        punctuation = first_contour_height("0(1)")
        assert math.isclose(descender, plain, rel_tol=1e-6)
        assert math.isclose(punctuation, plain, rel_tol=1e-6)
        assert math.isclose(plain, target, rel_tol=0.05)

    def test_fallback_normalizes_ink_box_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A font without a usable 'H' keeps the old per-line behaviour + WARNING."""
        monkeypatch.setattr(ftext_renderer, "_reference_cap_height", lambda path: 0.0)
        with caplog.at_level(logging.WARNING, logger="plt_optimizer.generate.ftext_renderer"):
            lc = render_text_line_ftext("Hp", 0.5, DEFAULT_FONT_PATH)
        bounds = lc.bounds()
        assert bounds is not None
        # Per-line ink-box normalization: the ink box spans the target exactly.
        assert math.isclose(bounds[3] - bounds[1], 0.5, rel_tol=1e-6)
        assert "ink-box normalization" in caplog.text


class TestMissingGlyphDetection:
    """TTF parity with the PLT family's PltFontRenderError contract."""

    def test_missing_glyph_raises(self) -> None:
        """A character outside the font's cmap raises instead of engraving boxes."""
        with pytest.raises(FtextRenderError, match="has no glyphs for"):
            render_text_line_ftext("A\u2603", 0.5, DEFAULT_FONT_PATH)

    def test_covered_text_does_not_raise(self) -> None:
        """Normal text (including the em dash) passes the coverage probe."""
        lc = render_text_line_ftext("ARC Flash Hazard \u2014 70E", 0.22, DEFAULT_FONT_PATH)
        assert not lc.is_empty()

    def test_whitespace_only_never_raises(self) -> None:
        """Whitespace carries no glyphs and is exempt from the probe."""
        lc = render_text_line_ftext("   ", 0.5, DEFAULT_FONT_PATH)
        assert lc.is_empty()


class TestRemoveClosingChords:
    """Tests for the _remove_closing_chords helper."""

    def test_removes_long_final_segment(self) -> None:
        """A long final closing chord should be sliced off."""
        # Closed loop (first == last); the segment from point 3 back to point 0
        # is a huge jump - an erroneous chord that must be removed.
        line = np.array([0 + 10j, 1 + 11j, 2 + 12j, 100 - 50j, 5 + 7j], dtype=complex)
        lc_in = vp.LineCollection()
        lc_in.append(line)
        out = _remove_closing_chords(lc_in)
        assert len(out) == 1
        # The loop is broken open: first no longer equals last.
        result = out[0]
        assert abs(result[0] - result[-1]) > CHORD_THRESHOLD_INCHES

    def test_preserves_short_final_segment(self) -> None:
        """A microscopic closing step of a genuine loop should be kept."""
        # Closed loop (first == last); the final segment back to origin is
        # tiny, so it is a genuine closed shape like "o" and must remain
        # untouched.
        start = 0 + 10j
        line = np.array(
            [start, 2 + 11j, 5 + 12j, 3 + 13j, 0.001 + 9.999j, start],
            dtype=complex,
        )
        lc_in = vp.LineCollection()
        lc_in.append(line)
        out = _remove_closing_chords(lc_in)
        assert len(out) == 1
        # Genuine loop preserved: still closed, length unchanged.
        result = out[0]
        assert abs(result[0] - result[-1]) < CHORD_THRESHOLD_INCHES
        assert len(result) == 6

    def test_keeps_open_stroke(self) -> None:
        """An already-open stroke (first != last) should pass through."""
        line = np.array([0 + 10j, 20 - 50j, 30 + 7j], dtype=complex)
        lc_in = vp.LineCollection()
        lc_in.append(line)
        out = _remove_closing_chords(lc_in)
        assert len(out) == 1
        result = out[0]
        # Unchanged.
        assert np.array_equal(result, line)

    def test_two_point_line_passes_through(self) -> None:
        """A two-point line is too short for chord analysis and is kept."""
        line = np.array([0 + 0j, 1 + 1j], dtype=complex)
        lc_in = vp.LineCollection()
        lc_in.append(line)
        out = _remove_closing_chords(lc_in)
        assert len(out) == 1
        assert np.array_equal(out[0], line)


class _StubPath:
    """Minimal path stub exposing ``iter_segments`` with canned codes.

    ``matplotlib`` never emits a code outside MOVETO/LINETO/CLOSEPOLY when
    ``curves=False``, so the defensive fall-through in :func:`_split_contours`
    (a code that matches none of the handled branches) can only be exercised
    with a synthetic segment stream.
    """

    def __init__(self, segments: list[tuple[np.ndarray, int]]) -> None:
        """Store the canned ``(vertex, code)`` segments to yield."""
        self._segments = segments

    def iter_segments(self, simplify: bool = False, curves: bool = False) -> object:
        """Yield the canned segments, ignoring the matplotlib flags."""
        return iter(self._segments)


class TestSplitContours:
    """Tests for the raw contour grouping in _split_contours()."""

    def test_closepoly_is_geometry_free(self) -> None:
        """A CLOSEPOLY vertex must not add a point or flush the contour."""
        stub = _StubPath(
            [
                (np.array([0.0, 0.0]), int(MplPath.MOVETO)),
                (np.array([1.0, 0.0]), int(MplPath.LINETO)),
                # CLOSEPOLY carries no geometry: its vertex must be ignored.
                (np.array([0.0, 0.0]), int(MplPath.CLOSEPOLY)),
            ]
        )
        contours = _split_contours(stub)  # type: ignore[arg-type]
        assert len(contours) == 1
        assert len(contours[0]) == 2

    def test_unhandled_code_falls_through(self) -> None:
        """A code matching no branch is skipped without touching geometry."""
        stub = _StubPath(
            [
                (np.array([0.0, 0.0]), int(MplPath.MOVETO)),
                (np.array([1.0, 0.0]), int(MplPath.LINETO)),
                # STOP (0) is handled by neither the line nor CLOSEPOLY branch.
                (np.array([9.0, 9.0]), int(MplPath.STOP)),
                (np.array([2.0, 0.0]), int(MplPath.LINETO)),
            ]
        )
        contours = _split_contours(stub)  # type: ignore[arg-type]
        assert len(contours) == 1
        # The unhandled vertex was skipped: only the 3 handled points remain.
        assert len(contours[0]) == 3
        assert np.allclose(np.abs(contours[0]), [0.0, 1.0, 2.0])


class TestRenderTextLineFtextDegeneratePaths:
    """Early-return guards for degenerate glyph paths."""

    def test_no_contours_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A glyph path that splits into zero contours yields empty output."""
        monkeypatch.setattr(ftext_renderer, "_split_contours", lambda path: [])
        lc = render_text_line_ftext("X", 1.0, DEFAULT_FONT_PATH)
        assert isinstance(lc, vp.LineCollection)
        assert lc.is_empty()

    def test_empty_bounds_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Contours too short to form lines yield an empty LineCollection."""
        # Single-point contours are dropped by LineCollection.append, leaving
        # the collection empty (bounds() is None).
        monkeypatch.setattr(
            ftext_renderer,
            "_split_contours",
            lambda path: [np.array([1 + 2j], dtype=complex)],
        )
        lc = render_text_line_ftext("X", 1.0, DEFAULT_FONT_PATH)
        assert isinstance(lc, vp.LineCollection)
        assert lc.is_empty()


class TestGlyphGroupsForLine:
    """Per-glyph contour partitioning (glyph-major slicing by per-char counts)."""

    def test_counts_partition_the_whole_line_exactly(self) -> None:
        """Per-character counts sum to the render's contour count exactly."""
        whole, _words = ftext_renderer.render_text_line_ftext_with_words(
            "REFRIGERATION", 0.5, DEFAULT_FONT_PATH
        )
        glyphs = ftext_renderer.glyph_groups_for_line(
            "REFRIGERATION", DEFAULT_FONT_PATH, len(whole)
        )
        assert [char for char, _indices in glyphs] == list("REFRIGERATION")
        all_indices = [i for _char, indices in glyphs for i in indices]
        assert sorted(all_indices) == list(range(len(whole)))
        assert len(all_indices) == len(set(all_indices))

    def test_space_contributes_no_group(self) -> None:
        """Spaces own no contours and no group."""
        counts = sum(ftext_renderer._glyph_contour_count(DEFAULT_FONT_PATH, ch) for ch in "AB")
        glyphs = ftext_renderer.glyph_groups_for_line("A B", DEFAULT_FONT_PATH, counts)
        assert [char for char, _indices in glyphs] == ["A", "B"]

    def test_count_mismatch_returns_empty_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A count/render mismatch disables grouping and logs a WARNING."""
        with caplog.at_level(logging.WARNING):
            glyphs = ftext_renderer.glyph_groups_for_line("AB", DEFAULT_FONT_PATH, 99999)
        assert glyphs == []
        assert "glyph grouping" in caplog.text

    def test_with_glyphs_matches_with_words_geometry(self) -> None:
        """The glyph-aware renderer returns the identical whole-line render."""
        whole_w, words_w = ftext_renderer.render_text_line_ftext_with_words(
            "AB CD", 0.5, DEFAULT_FONT_PATH
        )
        whole_g, words_g, glyphs_g = ftext_renderer.render_text_line_ftext_with_glyphs(
            "AB CD", 0.5, DEFAULT_FONT_PATH
        )
        assert [np.asarray(c).tolist() for c in whole_g] == [
            np.asarray(c).tolist() for c in whole_w
        ]
        assert words_g == words_w
        assert [char for char, _ in glyphs_g] == list("ABCD")

    def test_empty_text(self) -> None:
        """Empty input yields an empty collection and no glyph groups."""
        whole, _words, glyphs = ftext_renderer.render_text_line_ftext_with_glyphs("", 0.25)
        assert whole.is_empty()
        assert glyphs == []


class TestRenderTextLineFtextWithWords:
    """Tests for render_text_line_ftext_with_words() word partitioning."""

    def test_empty_string_returns_empty_groups(self) -> None:
        """Empty text yields an empty collection and no word groups."""
        whole, groups = ftext_renderer.render_text_line_ftext_with_words("", 0.25)
        assert whole.is_empty()
        assert groups == []

    def test_single_word_claims_every_contour(self) -> None:
        """A single-word line assigns all contours to its one group."""
        whole, groups = ftext_renderer.render_text_line_ftext_with_words(
            "SINGLE", 0.5, DEFAULT_FONT_PATH
        )
        assert not whole.is_empty()
        assert len(groups) == 1
        word, indices = groups[0]
        assert word == "SINGLE"
        assert sorted(indices) == list(range(len(whole)))

    def test_groups_partition_contours_exactly(self) -> None:
        """Word groups partition (never duplicate or drop) whole-line contours."""
        whole, groups = ftext_renderer.render_text_line_ftext_with_words(
            "AB CD EF", 0.5, DEFAULT_FONT_PATH
        )
        assert groups
        all_indices = [i for _word, indices in groups for i in indices]
        assert sorted(all_indices) == list(range(len(whole)))
        assert len(all_indices) == len(set(all_indices))

    def test_groups_follow_text_order(self) -> None:
        """Group order matches the whitespace split, indices ascend per word."""
        whole, groups = ftext_renderer.render_text_line_ftext_with_words(
            "A BC DEF G", 0.45, DEFAULT_FONT_PATH
        )
        assert [word for word, _ in groups] == ["A", "BC", "DEF", "G"]
        for _word, indices in groups:
            assert indices == sorted(indices)

    def test_word_geometry_is_whole_line_geometry(self) -> None:
        """Claimed indices cover every whole-line contour exactly once."""
        whole, groups = ftext_renderer.render_text_line_ftext_with_words(
            "WIDTH 123 Test", 0.4, DEFAULT_FONT_PATH
        )
        whole_contours = [np.asarray(line) for line in whole]
        for _word, indices in groups:
            for index in indices:
                assert len(whole_contours[index]) >= 2
        # Every contour belongs to exactly one word.
        seen = {i for _w, idx in groups for i in idx}
        assert seen == set(range(len(whole_contours)))

    def test_blank_segments_yield_empty_groups(self) -> None:
        """Runs of spaces keep column alignment with empty index lists."""
        whole, groups = ftext_renderer.render_text_line_ftext_with_words(
            "A  B", 0.5, DEFAULT_FONT_PATH
        )
        assert [word for word, _ in groups] == ["A", "", "B"]
        assert groups[1][1] == []

    def test_unmatched_contour_falls_back_to_ungrouped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unmatchable word contour disables grouping (whole-line mode)."""
        original = ftext_renderer._contour_signature
        probe, _ = ftext_renderer.render_text_line_ftext_with_words("AB CD", 0.5, DEFAULT_FONT_PATH)
        pool_size = len(probe)
        calls = {"n": 0}

        def broken_signature(contour: np.ndarray) -> str:
            calls["n"] += 1
            # Pool construction (whole-line contours) first; poison lookups.
            if calls["n"] > pool_size:
                return "unmatchable"
            return original(contour)

        monkeypatch.setattr(ftext_renderer, "_contour_signature", broken_signature)
        whole, groups = ftext_renderer.render_text_line_ftext_with_words(
            "AB CD", 0.5, DEFAULT_FONT_PATH
        )
        assert not whole.is_empty()
        assert groups == []

    def test_word_render_failure_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A word render producing no contours disables grouping."""
        monkeypatch.setattr(
            ftext_renderer,
            "_render_scaled_line",
            lambda text, font_props, scale: vp.LineCollection(),
        )
        whole, groups = ftext_renderer.render_text_line_ftext_with_words(
            "AB CD", 0.5, DEFAULT_FONT_PATH
        )
        assert not whole.is_empty()
        assert groups == []


def _extents(lc: vp.LineCollection, indices: Optional[List[int]] = None) -> tuple[float, ...]:
    """Return ``(x_min, x_max, y_min, y_max)`` of contours (all, or by index)."""
    lines = [np.asarray(line) for line in lc if len(line)]
    if indices is not None:
        lines = [np.asarray(lc[i]) for i in indices]
    points = np.concatenate(lines)
    return (
        float(points.real.min()),
        float(points.real.max()),
        float(points.imag.min()),
        float(points.imag.max()),
    )


class TestSmallCaps:
    """The synthesized small-caps transform (TTF family).

    matplotlib ignores OpenType ``smcp``, so the renderer maps ASCII
    lowercase to uppercase and shrinks those glyphs **top-down** about their
    own bottom-left ink corner. The reference line ``"AA"`` (the mapped
    string) is the exact baseline every assertion compares against.
    """

    def test_lowercase_renders_the_capital_shrunk_top_down(self) -> None:
        """The lowercase glyph is the capital, shrunk 0.7 about its bottom-left."""
        mapped = ftext_renderer.render_text_line_ftext("AA", 1.0, DEFAULT_FONT_PATH)
        small = ftext_renderer.render_text_line_ftext(
            "Aa", 1.0, DEFAULT_FONT_PATH, small_caps_scale=0.7
        )
        cap = _extents(
            mapped, ftext_renderer.glyph_groups_for_line("AA", DEFAULT_FONT_PATH, len(mapped))[1][1]
        )
        shrunk = _extents(
            small,
            ftext_renderer.glyph_groups_for_line(
                "Aa", DEFAULT_FONT_PATH, len(small), small_caps=True
            )[1][1],
        )
        # Anchor: the glyph's own left edge and lowest ink stay exactly put.
        assert shrunk[0] == pytest.approx(cap[0], abs=1e-9)
        assert shrunk[2] == pytest.approx(cap[2], abs=1e-9)
        # Uniform 0.7 shrink of width and height, top descending.
        assert shrunk[1] - shrunk[0] == pytest.approx(0.7 * (cap[1] - cap[0]), abs=1e-9)
        assert shrunk[3] - shrunk[2] == pytest.approx(0.7 * (cap[3] - cap[2]), abs=1e-9)

    def test_capital_glyph_is_untouched(self) -> None:
        """The uppercase glyph of a mixed line keeps its exact geometry."""
        mapped = ftext_renderer.render_text_line_ftext("AA", 1.0, DEFAULT_FONT_PATH)
        small = ftext_renderer.render_text_line_ftext(
            "Aa", 1.0, DEFAULT_FONT_PATH, small_caps_scale=0.7
        )
        cap = _extents(
            mapped, ftext_renderer.glyph_groups_for_line("AA", DEFAULT_FONT_PATH, len(mapped))[0][1]
        )
        kept = _extents(
            small,
            ftext_renderer.glyph_groups_for_line(
                "Aa", DEFAULT_FONT_PATH, len(small), small_caps=True
            )[0][1],
        )
        assert kept == pytest.approx(cap, abs=1e-9)

    def test_scale_one_renders_full_height_capitals(self) -> None:
        """``sc_scale=1.0`` maps lowercase to capitals and skips the shrink."""
        caps = ftext_renderer.render_text_line_ftext("AA", 1.0, DEFAULT_FONT_PATH)
        one = ftext_renderer.render_text_line_ftext(
            "Aa", 1.0, DEFAULT_FONT_PATH, small_caps_scale=1.0
        )
        assert _extents(one) == pytest.approx(_extents(caps), abs=1e-9)

    def test_transform_off_renders_the_fonts_own_lowercase(self) -> None:
        """Without the parameter the font's own lowercase glyph renders."""
        off = ftext_renderer.render_text_line_ftext("a", 1.0, DEFAULT_FONT_PATH)
        caps = ftext_renderer.render_text_line_ftext("A", 1.0, DEFAULT_FONT_PATH)
        assert _extents(off) != pytest.approx(_extents(caps), abs=1e-3)

    def test_caseless_characters_stay_full_size(self) -> None:
        """Digits/punctuation engrave at full size on an SC line."""
        sc = ftext_renderer.render_text_line_ftext(
            "1-1", 1.0, DEFAULT_FONT_PATH, small_caps_scale=0.5
        )
        plain = ftext_renderer.render_text_line_ftext("1-1", 1.0, DEFAULT_FONT_PATH)
        assert _extents(sc) == pytest.approx(_extents(plain), abs=1e-9)

    def test_glyph_groups_keyed_by_the_original_characters(self) -> None:
        """The partition reports the text's characters, sliced by capital counts."""
        whole, _words, glyphs = ftext_renderer.render_text_line_ftext_with_glyphs(
            "Hi there", 0.5, DEFAULT_FONT_PATH, small_caps_scale=0.7
        )
        assert [char for char, _indices in glyphs] == list("Hithere")
        flat = [i for _char, indices in glyphs for i in indices]
        assert sorted(flat) == list(range(len(whole)))

    def test_word_groups_cover_every_contour(self) -> None:
        """SC word groups come from the exact slicing and partition the line."""
        whole, words = ftext_renderer.render_text_line_ftext_with_words(
            "Hi there", 1.0, DEFAULT_FONT_PATH, small_caps_scale=0.7
        )
        assert [word for word, _indices in words] == ["Hi", "there"]
        flat = [i for _word, indices in words for i in indices]
        assert sorted(flat) == list(range(len(whole)))

    def test_scale_one_word_mode_partitions_the_line(self) -> None:
        """``sc_scale=1.0`` uses the slicing path and partitions exactly."""
        whole, words = ftext_renderer.render_text_line_ftext_with_words(
            "AB CD", 0.5, DEFAULT_FONT_PATH, small_caps_scale=1.0
        )
        assert [word for word, _indices in words] == ["AB", "CD"]
        flat = [i for _word, indices in words for i in indices]
        assert sorted(flat) == list(range(len(whole)))

    def test_word_group_count_drift_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A contour-count drift yields whole-line (ungrouped) word groups."""
        monkeypatch.setattr(ftext_renderer, "_glyph_contour_count", lambda path, ch: 99)
        with caplog.at_level(logging.WARNING):
            whole, words = ftext_renderer.render_text_line_ftext_with_words(
                "AB CD", 0.5, DEFAULT_FONT_PATH, small_caps_scale=0.7
            )
        assert not whole.is_empty()
        assert words == []
        assert "falling back to whole-line" in caplog.text

    def test_blank_input_stays_empty(self) -> None:
        """Blank input short-circuits before any mapping happens."""
        assert ftext_renderer.render_text_line_ftext("", 0.5, small_caps_scale=0.7).is_empty()
        whole, words = ftext_renderer.render_text_line_ftext_with_words(
            "", 0.5, small_caps_scale=0.7
        )
        assert whole.is_empty() and words == []
        whole_g, words_g, glyphs_g = ftext_renderer.render_text_line_ftext_with_glyphs(
            "", 0.5, small_caps_scale=0.7
        )
        assert whole_g.is_empty() and words_g == [] and glyphs_g == []

    def test_coverage_probe_runs_on_the_mapped_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The glyph-coverage probe sees the uppercase-mapped string."""
        seen: list[str] = []
        monkeypatch.setattr(
            ftext_renderer,
            "_assert_glyph_coverage",
            lambda path, text: seen.append(text),
        )
        ftext_renderer.render_text_line_ftext(
            "My Aunt", 0.5, DEFAULT_FONT_PATH, small_caps_scale=0.7
        )
        assert seen == ["MY AUNT"]

    def test_count_mismatch_falls_back_to_full_size(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A contour-count drift leaves the natural uppercase render."""
        monkeypatch.setattr(ftext_renderer, "_glyph_contour_count", lambda path, ch: 99)
        with caplog.at_level(logging.WARNING):
            lc = ftext_renderer.render_text_line_ftext(
                "ab", 1.0, DEFAULT_FONT_PATH, small_caps_scale=0.7
            )
        caps = ftext_renderer.render_text_line_ftext("AB", 1.0, DEFAULT_FONT_PATH)
        assert not lc.is_empty()
        assert _extents(lc) == pytest.approx(_extents(caps), abs=1e-9)
        assert "rendering at full size" in caplog.text


class TestItalicShear:
    """The ``<i>`` transform (TTF family): shear fallback + native splice."""

    @staticmethod
    def _matplotlib_ttf(name: str) -> Path:
        """Return a bundled matplotlib TTF by file name (test fixture)."""
        import matplotlib

        return Path(matplotlib.get_data_path()) / "fonts" / "ttf" / name

    def test_shear_raises_the_cap_height(self) -> None:
        """The shear fallback lifts the ink top by height * tan (baseline fixed)."""
        plain = render_text_line_ftext("AV", 0.5, DEFAULT_FONT_PATH)
        italic = render_text_line_ftext("AV", 0.5, DEFAULT_FONT_PATH, italic_tan=0.5)
        _, _, y_min, y_max = _extents(plain)
        _, _, iy_min, iy_max = _extents(italic)
        assert iy_min == pytest.approx(y_min, abs=1e-9)
        assert iy_max - y_max == pytest.approx((y_max - y_min) * 0.5, abs=1e-6)

    def test_zero_tan_is_bit_identical(self) -> None:
        """``italic_tan = 0`` (upright) leaves the render untouched."""
        plain = render_text_line_ftext("AV", 0.5, DEFAULT_FONT_PATH)
        upright = render_text_line_ftext("AV", 0.5, DEFAULT_FONT_PATH, italic_tan=0.0)
        assert _extents(plain) == pytest.approx(_extents(upright), abs=1e-12)

    def test_flags_shear_only_the_selected_glyphs(self) -> None:
        """A per-character flag shears one glyph, not the whole line."""
        plain = render_text_line_ftext("AV", 0.5, DEFAULT_FONT_PATH)
        first = render_text_line_ftext(
            "AV", 0.5, DEFAULT_FONT_PATH, italic_tan=0.5, italic_flags=(True, False)
        )
        both = render_text_line_ftext("AV", 0.5, DEFAULT_FONT_PATH, italic_tan=0.5)
        plain_top = _extents(plain)[3]
        first_top = _extents(first)[3]
        both_top = _extents(both)[3]
        # The line's top is the tallest sheared glyph, so shearing the shorter
        # 'A' alone lifts the top less than shearing both: plain < A < both.
        assert plain_top < first_top < both_top

    def test_unrenderable_native_face_falls_back_to_shear(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An unreadable italic face degrades to the shear, never a crash."""
        bogus = tmp_path / "ReliefSingleLineCAD-Regular-Italic.ttf"
        bogus.write_bytes(b"not a font")
        plain = render_text_line_ftext("AV", 0.5, DEFAULT_FONT_PATH)
        shear = render_text_line_ftext("AV", 0.5, DEFAULT_FONT_PATH, italic_tan=0.5)
        with caplog.at_level("WARNING"):
            result = render_text_line_ftext(
                "AV", 0.5, DEFAULT_FONT_PATH, italic_tan=0.5, italic_face=bogus
            )
        # The shear geometry is what engraves (the splice was impossible).
        assert not result.is_empty()
        assert _extents(result)[3] == pytest.approx(_extents(shear)[3], abs=1e-9)
        assert _extents(result)[3] > _extents(plain)[3]

    def test_native_face_splices_without_raising_the_cap(self) -> None:
        """A native italic face replaces the glyph (no synthetic shear lift)."""
        regular = self._matplotlib_ttf("DejaVuSerif.ttf")
        italic_face = self._matplotlib_ttf("DejaVuSerif-Italic.ttf")
        plain = render_text_line_ftext("AV", 0.5, regular)
        native = render_text_line_ftext("AV", 0.5, regular, italic_tan=0.5, italic_face=italic_face)
        shear = render_text_line_ftext("AV", 0.5, regular, italic_tan=0.5)
        # The native italic keeps the cap height; the shear lifts the top.
        assert _extents(native)[3] == pytest.approx(_extents(plain)[3], abs=1e-6)
        assert _extents(shear)[3] > _extents(native)[3]

    def test_glyph_count_mismatch_renders_upright(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A contour-count drift leaves the natural upright render."""
        monkeypatch.setattr(ftext_renderer, "_glyph_contour_count", lambda path, ch: 99)
        with caplog.at_level(logging.WARNING):
            lc = render_text_line_ftext("AV", 1.0, DEFAULT_FONT_PATH, italic_tan=0.5)
        plain = render_text_line_ftext("AV", 1.0, DEFAULT_FONT_PATH)
        assert not lc.is_empty()
        assert _extents(lc) == pytest.approx(_extents(plain), abs=1e-9)
        assert "rendering upright" in caplog.text

    def test_italic_is_a_noop_for_blank_text(self) -> None:
        """Blank text yields an empty collection (no glyph slicing)."""
        empty = render_text_line_ftext("", 0.5, DEFAULT_FONT_PATH, italic_tan=0.5)
        assert empty.is_empty()

    def test_word_groups_survive_the_italic(self) -> None:
        """Italic lines keep exact per-word contour groups (the slicing path)."""
        whole, groups = ftext_renderer.render_text_line_ftext_with_words(
            "AB CD", 0.5, DEFAULT_FONT_PATH, italic_tan=0.5
        )
        assert [text for text, _ in groups] == ["AB", "CD"]
        covered = sorted(index for _, indices in groups for index in indices)
        assert covered == list(range(len(whole)))

    def test_glyph_groups_survive_the_italic(self) -> None:
        """Italic lines keep exact per-glyph contour groups (the glyph sweep)."""
        _whole, _words, glyph_groups = ftext_renderer.render_text_line_ftext_with_glyphs(
            "AV", 0.5, DEFAULT_FONT_PATH, italic_tan=0.5
        )
        assert [char for char, _ in glyph_groups] == ["A", "V"]


class TestApplyBold:
    """The ``<b>`` per-glyph centerline shrink (TTF family).

    The bold glyph's centerlines shrink uniformly about its own ink-left on
    the baseline (``y = 0``) by ``(nominal - bold) / (nominal - plain)`` -- the
    centerline contract, matching the PLT baseline-anchored scale. The pen
    advance is untouched (the italic precedent).
    """

    def test_bold_glyph_shrinks_about_the_baseline(self) -> None:
        """The flagged glyph's height shrinks; the baseline stays put."""
        plain = render_text_line_ftext("AA", 1.0, DEFAULT_FONT_PATH)
        bold = render_text_line_ftext(
            "AA", 1.0, DEFAULT_FONT_PATH, bold_scale=0.7, bold_flags=(False, True)
        )
        groups = ftext_renderer.glyph_groups_for_line("AA", DEFAULT_FONT_PATH, len(plain))
        cap = _extents(plain, groups[1][1])
        shrunk = _extents(bold, groups[1][1])
        # Baseline (lowest ink, y=0) and ink-left stay exactly put.
        assert shrunk[2] == pytest.approx(cap[2], abs=1e-9)
        assert shrunk[0] == pytest.approx(cap[0], abs=1e-9)
        # Uniform 0.7 shrink of width and height, top descending.
        assert shrunk[3] - shrunk[2] == pytest.approx(0.7 * (cap[3] - cap[2]), abs=1e-9)
        assert shrunk[1] - shrunk[0] == pytest.approx(0.7 * (cap[1] - cap[0]), abs=1e-9)

    def test_unflagged_glyph_is_untouched(self) -> None:
        """The plain glyph of a mixed line keeps its exact geometry."""
        plain = render_text_line_ftext("AA", 1.0, DEFAULT_FONT_PATH)
        bold = render_text_line_ftext(
            "AA", 1.0, DEFAULT_FONT_PATH, bold_scale=0.7, bold_flags=(False, True)
        )
        groups = ftext_renderer.glyph_groups_for_line("AA", DEFAULT_FONT_PATH, len(plain))
        kept = _extents(bold, groups[0][1])
        cap = _extents(plain, groups[0][1])
        assert kept == pytest.approx(cap, abs=1e-9)

    def test_scale_none_is_bit_identical(self) -> None:
        """``bold_scale=None`` (the default) renders the untouched line."""
        plain = render_text_line_ftext("AB", 1.0, DEFAULT_FONT_PATH)
        explicit = render_text_line_ftext("AB", 1.0, DEFAULT_FONT_PATH, bold_scale=None)
        assert _extents(explicit) == pytest.approx(_extents(plain), abs=1e-9)

    def test_flags_none_emboldens_every_glyph(self) -> None:
        """``bold_flags=None`` scales every glyph (the whole-line case)."""
        plain = render_text_line_ftext("AV", 1.0, DEFAULT_FONT_PATH)
        bold = render_text_line_ftext("AV", 1.0, DEFAULT_FONT_PATH, bold_scale=0.6)
        groups = ftext_renderer.glyph_groups_for_line("AV", DEFAULT_FONT_PATH, len(plain))
        for _char, indices in groups:
            assert _extents(bold, indices)[3] < _extents(plain, indices)[3]

    def test_word_groups_partition_the_bold_line(self) -> None:
        """Bold word groups come from the exact slicing and partition the line."""
        whole, words = ftext_renderer.render_text_line_ftext_with_words(
            "AB CD", 0.5, DEFAULT_FONT_PATH, bold_scale=0.7, bold_flags=(False, True, False, False, False)
        )
        assert [word for word, _ in words] == ["AB", "CD"]
        flat = [i for _word, indices in words for i in indices]
        assert sorted(flat) == list(range(len(whole)))

    def test_glyph_groups_stay_one_to_one(self) -> None:
        """Bold lines keep exact per-glyph contour groups (the glyph sweep)."""
        _whole, _words, glyph_groups = ftext_renderer.render_text_line_ftext_with_glyphs(
            "AV", 0.5, DEFAULT_FONT_PATH, bold_scale=0.7, bold_flags=(True, True)
        )
        assert [char for char, _ in glyph_groups] == ["A", "V"]

    def test_bold_and_italic_compose(self) -> None:
        """An italic+bold glyph shears at full size, then shrinks about the baseline."""
        plain = render_text_line_ftext("A", 1.0, DEFAULT_FONT_PATH)
        both = render_text_line_ftext(
            "A", 1.0, DEFAULT_FONT_PATH, italic_tan=0.5, bold_scale=0.5, bold_flags=(True,)
        )
        # The shear raises the top by h*tan; the 0.5 scale halves the result.
        # Net: the bold+italic top is strictly below the italic-only top.
        italic_only = render_text_line_ftext("A", 1.0, DEFAULT_FONT_PATH, italic_tan=0.5)
        assert _extents(both)[3] < _extents(italic_only)[3]
        assert _extents(both)[3] > _extents(plain)[3] * 0.5 - 1e-9


class TestApplyGlyphScales:
    """The ``<scale:N>`` per-glyph geometry scale (TTF family).

    The scaled glyph's contours scale uniformly about its own ink-left on the
    baseline (the ``_apply_bold`` anchor), driven by the per-character
    ``glyph_scales`` array from the unified render gate. The pen advance is
    untouched (the italic/bold precedent).
    """

    def test_scaled_glyph_scales_about_the_baseline(self) -> None:
        """The flagged glyph's height scales; the baseline stays put."""
        plain = render_text_line_ftext("AA", 1.0, DEFAULT_FONT_PATH)
        scaled = render_text_line_ftext("AA", 1.0, DEFAULT_FONT_PATH, glyph_scales=(1.0, 0.5))
        groups = ftext_renderer.glyph_groups_for_line("AA", DEFAULT_FONT_PATH, len(plain))
        cap = _extents(plain, groups[1][1])
        shrunk = _extents(scaled, groups[1][1])
        # Baseline (lowest ink, y=0) and ink-left stay exactly put.
        assert shrunk[2] == pytest.approx(cap[2], abs=1e-9)
        assert shrunk[0] == pytest.approx(cap[0], abs=1e-9)
        # Uniform 0.5 shrink of width and height.
        assert shrunk[3] - shrunk[2] == pytest.approx(0.5 * (cap[3] - cap[2]), abs=1e-9)
        assert shrunk[1] - shrunk[0] == pytest.approx(0.5 * (cap[1] - cap[0]), abs=1e-9)

    def test_upscale_grows_the_glyph(self) -> None:
        """A factor above 1 grows the glyph (the cap rises, baseline fixed)."""
        plain = render_text_line_ftext("A", 1.0, DEFAULT_FONT_PATH)
        grown = render_text_line_ftext("A", 1.0, DEFAULT_FONT_PATH, glyph_scales=(1.5,))
        assert _extents(grown)[3] > _extents(plain)[3]
        assert _extents(grown)[2] == pytest.approx(_extents(plain)[2], abs=1e-9)

    def test_unscaled_glyph_is_untouched(self) -> None:
        """The 1.0 character of a mixed line keeps its exact geometry."""
        plain = render_text_line_ftext("AA", 1.0, DEFAULT_FONT_PATH)
        scaled = render_text_line_ftext("AA", 1.0, DEFAULT_FONT_PATH, glyph_scales=(1.0, 0.5))
        groups = ftext_renderer.glyph_groups_for_line("AA", DEFAULT_FONT_PATH, len(plain))
        assert _extents(scaled, groups[0][1]) == pytest.approx(
            _extents(plain, groups[0][1]), abs=1e-9
        )

    def test_all_unity_is_bit_identical(self) -> None:
        """An all-unity ``glyph_scales`` renders the untouched line."""
        plain = render_text_line_ftext("AB", 1.0, DEFAULT_FONT_PATH)
        explicit = render_text_line_ftext("AB", 1.0, DEFAULT_FONT_PATH, glyph_scales=(1.0, 1.0))
        assert _extents(explicit) == pytest.approx(_extents(plain), abs=1e-9)

    def test_bold_and_scale_compose_multiplicatively(self) -> None:
        """bold_scale * glyph_scale is one factor per glyph (the PLT contract)."""
        plain = render_text_line_ftext("AA", 1.0, DEFAULT_FONT_PATH)
        both = render_text_line_ftext(
            "AA",
            1.0,
            DEFAULT_FONT_PATH,
            bold_scale=0.8,
            bold_flags=(True, True),
            glyph_scales=(0.5, 0.5),
        )
        groups = ftext_renderer.glyph_groups_for_line("AA", DEFAULT_FONT_PATH, len(plain))
        for _char, indices in groups:
            assert _extents(both, indices)[3] < _extents(plain, indices)[3]
        # The composed 0.4 shrink is deeper than the bold-only 0.8.
        bold_only = render_text_line_ftext(
            "AA", 1.0, DEFAULT_FONT_PATH, bold_scale=0.8, bold_flags=(True, True)
        )
        assert _extents(both, groups[0][1])[3] < _extents(bold_only, groups[0][1])[3]

    def test_word_groups_partition_the_scaled_line(self) -> None:
        """Scaled word groups come from the exact slicing and partition the line."""
        whole, words = ftext_renderer.render_text_line_ftext_with_words(
            "AB CD", 0.5, DEFAULT_FONT_PATH, glyph_scales=(1.0, 0.5, 1.0, 1.0, 1.0)
        )
        assert [word for word, _ in words] == ["AB", "CD"]
        flat = [i for _word, indices in words for i in indices]
        assert sorted(flat) == list(range(len(whole)))

    def test_glyph_groups_stay_one_to_one(self) -> None:
        """Scaled lines keep exact per-glyph contour groups (the glyph sweep)."""
        _whole, _words, glyph_groups = ftext_renderer.render_text_line_ftext_with_glyphs(
            "AV", 0.5, DEFAULT_FONT_PATH, glyph_scales=(0.5, 1.5)
        )
        assert [char for char, _ in glyph_groups] == ["A", "V"]
