"""Tests for the underline geometry solver (:mod:`plt_optimizer.generate.underline`).

Covers the pure math the render path consumes: the maximal-run scan over
per-character flags, the closed-form segment/band X-interval (the strictly
horizontal break test), and the minimal-breaking complement that turns a span
plus the line's ink into the kept underline segments.
"""

from __future__ import annotations

import pytest

from plt_optimizer.generate.underline import (
    BAND_TOLERANCE_FRACTION,
    segment_band_interval,
    underline_runs,
    underline_segments,
)


class TestUnderlineRuns:
    """The maximal-run scan over per-character underline flags."""

    def test_no_flags_yields_no_runs(self) -> None:
        """An unmarked line has no runs (the renderer's fast path)."""
        assert underline_runs(()) == ()
        assert underline_runs((False, False, False)) == ()

    def test_whole_line_is_one_run(self) -> None:
        """All flags set yields one run covering the line."""
        assert underline_runs((True, True, True)) == ((0, 2),)

    def test_single_trailing_run(self) -> None:
        """A suffix run reports its inclusive bounds."""
        assert underline_runs((False, False, True, True)) == ((2, 3),)

    def test_two_runs_split_by_a_plain_glyph(self) -> None:
        """A plain character between spans splits them (per-run underlines)."""
        # 'A' plain, 'bb' underlined, 'C' plain, 'dd' underlined.
        flags = (False, True, True, False, True, True)
        assert underline_runs(flags) == ((1, 2), (4, 5))

    def test_adjacent_true_flags_merge_into_one_run(self) -> None:
        """There is no gap between touching flags, so one run results."""
        assert underline_runs((True, True, False, True)) == ((0, 1), (3, 3))

    def test_run_bounds_are_inclusive(self) -> None:
        """A one-character span reports the same index twice."""
        assert underline_runs((False, True, False)) == ((1, 1),)


class TestSegmentBandInterval:
    """A segment clipped to the half-plane below a horizontal level."""

    def test_segment_entirely_above_yields_nothing(self) -> None:
        """Ink that never reaches the band cuts nothing."""
        assert segment_band_interval((0.0, 0.5), (1.0, 0.2), 0.0, 0.0) is None

    def test_ink_exactly_on_the_level_is_safe(self) -> None:
        """Tangency is safe: ink touching the level cuts no gap."""
        assert segment_band_interval((0.0, 0.0), (1.0, 0.0), 0.0, 0.0) is None

    def test_segment_entirely_below_yields_its_x_extent(self) -> None:
        """A fully submerged segment gaps the underline across its width."""
        interval = segment_band_interval((0.2, -0.1), (0.8, -0.3), 0.0, 0.0)
        assert interval is not None
        assert interval[0] == pytest.approx(0.2)
        assert interval[1] == pytest.approx(0.8)

    def test_pad_widens_the_interval_on_both_sides(self) -> None:
        """The clearance is the horizontal air left at each cut edge."""
        interval = segment_band_interval((0.2, -0.1), (0.8, -0.3), 0.0, 0.05)
        assert interval is not None
        assert interval[0] == pytest.approx(0.15)
        assert interval[1] == pytest.approx(0.85)

    def test_crossing_clips_at_the_level(self) -> None:
        """A segment entering the band gaps from the crossing to its end."""
        # Descends from y=0.5 to y=-0.5; the level 0.0 is crossed at x=0.5.
        interval = segment_band_interval((0.0, 0.5), (1.0, -0.5), 0.0, 0.0)
        assert interval is not None
        assert interval[0] == pytest.approx(0.5)
        assert interval[1] == pytest.approx(1.0)

    def test_crossing_from_below_clips_at_the_level(self) -> None:
        """A segment leaving the band gaps from its start to the crossing."""
        interval = segment_band_interval((0.0, -0.5), (1.0, 0.5), 0.0, 0.0)
        assert interval is not None
        assert interval[0] == pytest.approx(0.0)
        assert interval[1] == pytest.approx(0.5)

    def test_horizontal_segment_below_the_level(self) -> None:
        """A submerged horizontal segment gaps its full width (order-free)."""
        interval = segment_band_interval((0.8, -0.1), (0.2, -0.1), 0.0, 0.0)
        assert interval is not None
        assert interval[0] == pytest.approx(0.2)
        assert interval[1] == pytest.approx(0.8)

    def test_degenerate_point_in_the_band(self) -> None:
        """A zero-length segment inside the band yields a padded point."""
        interval = segment_band_interval((0.5, -0.1), (0.5, -0.1), 0.0, 0.05)
        assert interval is not None
        assert interval[0] == pytest.approx(0.45)
        assert interval[1] == pytest.approx(0.55)

    def test_degenerate_point_above_is_safe(self) -> None:
        """A zero-length segment above the level cuts nothing."""
        assert segment_band_interval((0.5, 0.1), (0.5, 0.1), 0.0, 0.05) is None


class TestStrictlyHorizontal:
    """The break test is horizontal: only ink cutting THROUGH the band breaks."""

    def test_ink_beside_the_underline_never_breaks_it(self) -> None:
        """A side glyph is invisible to the underline.

        A vertical stem standing far to the right of the underline's end,
        running well below the centerline, never reaches the band's X range,
        so it cannot break the underline. (A radial test would collide with it
        through the vertical component of the distance.)
        """
        ink = (((5.0, 0.0), (5.0, -0.5)),)
        segments = underline_segments(ink, (0.0, 1.0), -0.06, 0.06, 0.1)
        assert segments == ((0.0, 1.0),)

    def test_ink_above_the_underline_never_breaks_it(self) -> None:
        """A cap sitting above the underline cuts nothing, at sane clearances.

        The band reaches only ``BAND_TOLERANCE_FRACTION * clearance`` above
        the touching level (``-underline_offset``), so a cap resting on the
        baseline is safe while the tolerance stays below its ink.
        """
        ink = (((0.2, 0.0), (0.8, 0.0)),)  # a cap's baseline rail at y = 0
        for clearance in (0.0, 0.05, 0.1, 0.2):
            segments = underline_segments(ink, (0.0, 1.0), -0.16, 0.06, clearance)
            assert segments == ((0.0, 1.0),)


class TestUnderlineSegments:
    """The minimal-breaking complement over a span."""

    def test_empty_span_yields_nothing(self) -> None:
        """A degenerate span cannot be engraved."""
        assert underline_segments((), (1.0, 1.0), -0.06, 0.06, 0.0) == ()
        assert underline_segments((), (2.0, 1.0), -0.06, 0.06, 0.0) == ()

    def test_no_ink_keeps_the_whole_span(self) -> None:
        """Without colliding ink the underline is unbroken."""
        assert underline_segments((), (0.0, 1.0), -0.06, 0.06, 0.0) == ((0.0, 1.0),)

    def test_far_ink_keeps_the_whole_span(self) -> None:
        """Ink that never reaches the band cuts nothing."""
        ink = (((0.5, 0.0), (0.5, 0.4)),)  # entirely above the baseline
        assert underline_segments(ink, (0.0, 1.0), -0.06, 0.06, 0.0) == ((0.0, 1.0),)

    def test_crossing_ink_cuts_one_gap(self) -> None:
        """A descender punching through splits the span in two."""
        ink = (((0.5, 0.0), (0.5, -0.15)),)
        segments = underline_segments(ink, (0.0, 1.0), -0.06, 0.06, 0.0)
        assert len(segments) == 2
        assert segments[0][0] == pytest.approx(0.0)
        assert segments[1][1] == pytest.approx(1.0)
        # The gap is centered on the crossing and strictly inside the span.
        assert segments[0][1] < 0.5 < segments[1][0]

    def test_touching_ink_cuts_no_gap(self) -> None:
        """Tangency is safe: ink at the touching level keeps the underline.

        The trigger level is ``line_y + cutter`` -- exactly where a same-width
        glyph stroke touches the underline stroke -- so a baseline rail at
        y = 0 with ``line_y = -cutter`` touches and cuts nothing.
        """
        ink = (((0.2, 0.0), (0.8, 0.0)),)
        assert underline_segments(ink, (0.0, 1.0), -0.06, 0.06, 0.0) == ((0.0, 1.0),)

    def test_clearance_is_the_horizontal_air_at_the_cut(self) -> None:
        """The gap is the ink's band width, the stroke's half-widths, plus air.

        A vertical descender crossing the band contributes a single X, so the
        gap is ``cutter + 2 * clearance``: the cutter is the two engraved
        strokes' own widths meeting at the cut (the ink is a centerline), and
        the clearance is the air added on each side.
        """
        ink = (((0.5, 0.0), (0.5, -0.2)),)
        for clearance in (0.0, 0.02, 0.05):
            segments = underline_segments(ink, (0.0, 1.0), -0.06, 0.06, clearance)
            assert len(segments) == 2
            gap = segments[1][0] - segments[0][1]
            assert gap == pytest.approx(0.06 + 2 * clearance, abs=1e-9)

    def test_wide_ink_gaps_its_full_band_width(self) -> None:
        """A horizontal rail deep in the band gaps its width plus the pad."""
        # A rail at y=-0.1 (0.04 below the touching level -0.06) spanning 0.4.
        ink = (((0.3, -0.1), (0.7, -0.1)),)
        segments = underline_segments(ink, (0.0, 1.0), -0.06, 0.06, 0.0)
        assert len(segments) == 2
        gap = segments[1][0] - segments[0][1]
        assert gap == pytest.approx(0.4 + 0.06)

    def test_band_tolerance_reaches_above_the_touching_level(self) -> None:
        """Ink just above the touching level cuts iff within the tolerance.

        The band reaches ``BAND_TOLERANCE_FRACTION * clearance`` above the
        touching level (``line_y + cutter``), so a shallow overshoot cuts only
        when the clearance is large enough to reach it.
        """
        # Ink 0.0026 ABOVE the touching level (line_y -0.06 + cutter 0.06 = 0).
        ink = (((0.4, 0.0026), (0.6, 0.0026)),)
        assert 0.0026 > BAND_TOLERANCE_FRACTION * 0.01  # clearance 0.01: safe
        assert underline_segments(ink, (0.0, 1.0), -0.06, 0.06, 0.01) == ((0.0, 1.0),)
        # Clearance 0.02 -> reach 0.004 > 0.0026: the ink is inside the band.
        segments = underline_segments(ink, (0.0, 1.0), -0.06, 0.06, 0.02)
        assert len(segments) == 2
        gap = segments[1][0] - segments[0][1]
        assert gap == pytest.approx(0.2 + 0.06 + 2 * 0.02)

    def test_two_crossings_cut_two_gaps(self) -> None:
        """Two descenders punch two gaps, leaving three kept segments."""
        ink = (
            ((0.25, 0.0), (0.25, -0.15)),
            ((0.75, 0.0), (0.75, -0.15)),
        )
        segments = underline_segments(ink, (0.0, 1.0), -0.06, 0.06, 0.0)
        assert len(segments) == 3
        assert segments[0][0] == pytest.approx(0.0)
        assert segments[-1][1] == pytest.approx(1.0)

    def test_overlapping_gaps_merge(self) -> None:
        """Two nearby crossings whose intervals overlap cut one wide gap."""
        ink = (
            ((0.45, 0.0), (0.45, -0.15)),
            ((0.55, 0.0), (0.55, -0.15)),
        )
        segments = underline_segments(ink, (0.0, 1.0), -0.06, 0.06, 0.05)
        assert len(segments) == 2

    def test_intervals_are_clipped_to_the_span(self) -> None:
        """A crossing at the span's edge consumes only the part inside it."""
        ink = (((0.0, 0.0), (0.0, -0.15)),)
        segments = underline_segments(ink, (0.0, 1.0), -0.06, 0.06, 0.0)
        assert len(segments) == 1
        assert segments[0][0] >= 0.0
        assert segments[0][1] == pytest.approx(1.0)

    def test_span_inside_a_gap_yields_nothing(self) -> None:
        """When the gaps consume the span, no underline is engraved."""
        ink = (((0.5, 0.0), (0.5, -0.15)),)
        segments = underline_segments(ink, (0.45, 0.55), -0.06, 0.06, 0.2)
        assert segments == ()

    def test_min_length_drops_specks(self) -> None:
        """Kept segments below the emission floor are dropped."""
        ink = (((0.02, 0.0), (0.02, -0.15)),)
        kept = underline_segments(ink, (0.0, 1.0), -0.06, 0.06, 0.0, min_length=0.05)
        assert all(hi - lo >= 0.05 for lo, hi in kept)

    def test_segments_are_increasing_and_disjoint(self) -> None:
        """The kept segments tile the span left to right without overlap."""
        ink = tuple(((x, 0.0), (x, -0.15)) for x in (0.2, 0.4, 0.6, 0.8))
        segments = underline_segments(ink, (0.0, 1.0), -0.06, 0.06, 0.0)
        assert len(segments) == 5
        for (lo0, hi0), (lo1, hi1) in zip(segments, segments[1:]):
            assert hi0 < lo1
            assert lo0 < hi0 and lo1 < hi1

    def test_closed_shape_crossing_twice(self) -> None:
        """A loop crossing the band twice cuts the interval between crossings."""
        # A tall thin rectangle (a 'p' bowl analogue) straddling the band.
        ink = (
            ((0.4, 0.1), (0.4, -0.1)),
            ((0.6, 0.1), (0.6, -0.1)),
            ((0.4, 0.1), (0.6, 0.1)),
            ((0.4, -0.1), (0.6, -0.1)),
        )
        segments = underline_segments(ink, (0.0, 1.0), -0.06, 0.06, 0.0)
        # The bottom rail (y=-0.1) and the submerged parts of the two rails
        # merge into one gap spanning 0.4..0.6, padded by the stroke half-width.
        assert len(segments) == 2
        assert segments[0][0] == pytest.approx(0.0)
        assert segments[1][1] == pytest.approx(1.0)
        gap = segments[1][0] - segments[0][1]
        assert gap == pytest.approx(0.2 + 0.06)
