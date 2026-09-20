"""Unit tests for the single-line TTF text renderer (matplotlib path codes)."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
import vpype as vp
from matplotlib.path import Path as MplPath

from plt_optimizer.generate import ftext_renderer
from plt_optimizer.generate.ftext_renderer import (
    CHORD_THRESHOLD_INCHES,
    DEFAULT_FONT_PATH,
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
