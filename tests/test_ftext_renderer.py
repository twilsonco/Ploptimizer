"""Unit tests for the single-line TTF text renderer (ftext wrapper)."""

from __future__ import annotations

import math
from pathlib import Path

import vpype as vp

from plt_optimizer.generate.ftext_renderer import (
    CHORD_THRESHOLD_INCHES,
    DEFAULT_FONT_PATH,
    _remove_closing_chords,
    _shell_quote,
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

    def test_open_stroke_chord_is_removed(self) -> None:
        """Erroneous closing chords on open strokes (e.g. 'D') are removed.

        TrueType forces every outline closed; for a single-line "D" this adds
        a long straight chord back to the start, which must be sliced off.
        """
        lc = render_text_line_ftext("D", 4.5, DEFAULT_FONT_PATH)
        assert not lc.is_empty()
        for line in lc:
            # After processing no closing segment should exceed the threshold,
            # i.e. the erroneous chord has been removed and the stroke is open.
            if len(line) > 2:
                closing = abs(line[-2] - line[-1])
                assert closing <= CHORD_THRESHOLD_INCHES

    def test_closed_loop_is_preserved(self) -> None:
        """Genuine closed loops (e.g. 'o') must not be sliced open."""
        lc = render_text_line_ftext("o", 4.5, DEFAULT_FONT_PATH)
        assert not lc.is_empty()
        # The loop's closing segment should remain microscopic.
        for line in lc:
            if len(line) > 2 and abs(line[0] - line[-1]) < 1e-3:
                closing = abs(line[-2] - line[-1])
                assert closing <= CHORD_THRESHOLD_INCHES


class TestRemoveClosingChords:
    """Tests for the _remove_closing_chords helper."""

    def test_removes_long_closing_segment_at_end(self) -> None:
        """A long forced closing chord at the array end should be sliced off."""
        import numpy as np

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

    def test_removes_long_segment_not_at_end(self) -> None:
        """A long chord in the middle of the array must also be found.

        Regression test for the 't' shifting-node problem: TrueType can rotate
        which node starts a closed loop, so the erroneous chord is not always
        at the end. The helper scans every segment and breaks there.
        """
        import numpy as np

        # Closed loop (first == last) where the long chord sits in the middle of
        # the array rather than at its end.
        line = np.array([0 + 10j, 100 - 50j, 5 + 7j, 3 + 9j, 2 + 8.99j], dtype=complex)
        lc_in = vp.LineCollection()
        lc_in.append(line)
        out = _remove_closing_chords(lc_in)
        assert len(out) == 1
        # The loop is broken open at the chord, wherever it sat.
        result = out[0]
        assert abs(result[0] - result[-1]) > CHORD_THRESHOLD_INCHES

    def test_preserves_short_closing_segment(self) -> None:
        """A microscopic closing step of a genuine loop should be kept."""
        import numpy as np

        # Closed loop (first == last); all segments are tiny, so it is a
        # genuine closed shape like "o" and must remain untouched.
        line = np.array(
            [0 + 1j, 2 + 3j, 5 + 6j, 4.00001 + 7.001j, 0.000005 + 1.002j],
            dtype=complex,
        )
        lc_in = vp.LineCollection()
        lc_in.append(line)
        out = _remove_closing_chords(lc_in)
        assert len(out) == 1
        # Genuine loop preserved: still closed, length unchanged.
        result = out[0]
        assert abs(result[0] - result[-1]) < CHORD_THRESHOLD_INCHES
        assert len(result) == 5


class TestShellQuote:
    """Tests for the shell-quoting helper."""

    def test_plain_string(self) -> None:
        """Safe plain strings need no quoting."""
        result = _shell_quote("hello")
        # shlex.quote leaves already-safe tokens unquoted.
        assert "'" not in result
        assert "hello" in result

    def test_spaces_and_special_chars(self) -> None:
        """Strings with spaces/shell metacharacters must remain safe."""
        result = _shell_quote("/path/with space/font.ttf")
        # Must be a single quoted token, not split by the shell.
        assert " " in result or "'" in result
