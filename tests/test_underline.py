"""Tests for the underline geometry solver (:mod:`plt_optimizer.generate.underline`).

Covers the pure math the render path consumes: the maximal-run scan over
per-character flags, the closed-form capsule/line X-interval (the gap
trigger), and the minimal-breaking complement that turns a span plus the
line's ink into the kept underline segments.
"""

from __future__ import annotations

import math

import pytest

from plt_optimizer.generate.underline import (
    capsule_line_interval,
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


class TestCapsuleLineInterval:
    """The capsule (segment + radius disk) intersected with a horizontal line."""

    def test_non_positive_radius_never_reaches(self) -> None:
        """A zero/negative radius has no swept area."""
        assert capsule_line_interval((0.0, 0.0), (1.0, 0.0), 0.0, 0.0) is None
        assert capsule_line_interval((0.0, 0.0), (1.0, 0.0), -0.1, 0.0) is None

    def test_horizontal_segment_band(self):
        """A parallel segment yields the stadium's interval at that height."""
        interval = capsule_line_interval((0.0, 0.0), (1.0, 0.0), 0.1, 0.0)
        assert interval is not None
        assert interval[0] == pytest.approx(-0.1)
        assert interval[1] == pytest.approx(1.1)

    def test_horizontal_segment_band_edge_is_safe(self) -> None:
        """A line at the band's edge touches it: no interval, no gap."""
        assert capsule_line_interval((0.0, 0.0), (1.0, 0.0), 0.1, 0.1) is None

    def test_horizontal_segment_beyond_band_is_empty(self) -> None:
        """A line outside the band radius misses the capsule entirely."""
        assert capsule_line_interval((0.0, 0.0), (1.0, 0.0), 0.1, 0.2) is None

    def test_vertical_segment_crossing_the_line(self) -> None:
        """A segment crossing the line yields the full diameter interval."""
        interval = capsule_line_interval((0.0, 0.0), (0.0, -0.2), 0.1, -0.15)
        assert interval is not None
        # The line passes through the segment's own axis, so the capsule
        # covers the full diameter on each side of the crossing.
        assert interval[0] == pytest.approx(-0.1)
        assert interval[1] == pytest.approx(0.1)

    def test_vertical_segment_short_of_the_line(self) -> None:
        """A segment stopping short reaches the line only via its end disk."""
        interval = capsule_line_interval((0.0, 0.0), (0.0, -0.1), 0.1, -0.15)
        assert interval is not None
        # Only the end disk (center y=-0.1) reaches y=-0.15: dy=0.05.
        span = math.sqrt(0.1**2 - 0.05**2)
        assert interval[0] == pytest.approx(-span)
        assert interval[1] == pytest.approx(span)

    def test_segment_far_above_the_line_is_empty(self) -> None:
        """No part of the capsule reaches the line."""
        assert capsule_line_interval((0.0, 0.0), (1.0, 0.0), 0.1, -0.5) is None

    def test_degenerate_segment_is_a_disk(self) -> None:
        """A zero-length segment behaves as its endpoint disk."""
        interval = capsule_line_interval((0.5, 0.0), (0.5, 0.0), 0.1, 0.0)
        assert interval is not None
        assert interval[0] == pytest.approx(0.4)
        assert interval[1] == pytest.approx(0.6)

    def test_degenerate_disk_miss_is_empty(self) -> None:
        """A point disk too far from the line yields nothing."""
        assert capsule_line_interval((0.5, 0.0), (0.5, 0.0), 0.1, 0.3) is None

    def test_diagonal_crossing_yields_an_interval(self) -> None:
        """A slanted segment crossing the line yields a finite interval."""
        interval = capsule_line_interval((0.0, 0.1), (1.0, -0.1), 0.05, 0.0)
        assert interval is not None
        assert interval[0] < 0.5 < interval[1]

    def test_tangent_line_is_safe(self) -> None:
        """Exact tangency cuts no gap: touching strokes are safe."""
        assert capsule_line_interval((0.0, 0.0), (1.0, 0.0), 0.1, 0.1) is None
        # A disk grazing the line from above likewise misses it.
        assert capsule_line_interval((0.5, 0.0), (0.5, 0.0), 0.1, 0.1) is None


class TestUnderlineSegments:
    """The minimal-breaking complement over a span."""

    def test_empty_span_yields_nothing(self) -> None:
        """A degenerate span cannot be engraved."""
        assert underline_segments((), (1.0, 1.0), -0.06, 0.06) == ()
        assert underline_segments((), (2.0, 1.0), -0.06, 0.06) == ()

    def test_no_ink_keeps_the_whole_span(self) -> None:
        """Without colliding ink the underline is unbroken."""
        assert underline_segments((), (0.0, 1.0), -0.06, 0.06) == ((0.0, 1.0),)

    def test_far_ink_keeps_the_whole_span(self) -> None:
        """Ink that never reaches the centerline cuts nothing."""
        ink = (((0.5, 0.0), (0.5, 0.4)),)  # entirely above the baseline
        assert underline_segments(ink, (0.0, 1.0), -0.06, 0.06) == ((0.0, 1.0),)

    def test_crossing_ink_cuts_one_gap(self) -> None:
        """A descender punching through splits the span in two."""
        ink = (((0.5, 0.0), (0.5, -0.15)),)
        segments = underline_segments(ink, (0.0, 1.0), -0.06, 0.06)
        assert len(segments) == 2
        assert segments[0][0] == pytest.approx(0.0)
        assert segments[1][1] == pytest.approx(1.0)
        # The gap is centered on the crossing and strictly inside the span.
        assert segments[0][1] < 0.5 < segments[1][0]
        gap = segments[1][0] - segments[0][1]
        assert gap == pytest.approx(2 * math.sqrt(0.06**2 - 0.0**2), rel=1e-6)

    def test_two_crossings_cut_two_gaps(self) -> None:
        """Two descenders punch two gaps, leaving three kept segments."""
        ink = (
            ((0.25, 0.0), (0.25, -0.15)),
            ((0.75, 0.0), (0.75, -0.15)),
        )
        segments = underline_segments(ink, (0.0, 1.0), -0.06, 0.06)
        assert len(segments) == 3
        assert segments[0][0] == pytest.approx(0.0)
        assert segments[-1][1] == pytest.approx(1.0)

    def test_touching_ink_cuts_no_gap(self) -> None:
        """Tangency is safe: ink exactly at the radius keeps the underline."""
        # A horizontal ink run sitting exactly one radius above the centerline.
        ink = (((0.2, 0.0), (0.8, 0.0)),)
        segments = underline_segments(ink, (0.0, 1.0), -0.06, 0.06)
        assert segments == ((0.0, 1.0),)

    def test_gap_widens_as_ink_deepens(self) -> None:
        """A deeper crossing widens the gap, saturating at the full diameter.

        A shallow crossing only clips the end disk (a chord narrower than
        the diameter); once the centerline passes through the segment's own
        axis the gap opens to the full diameter and stays there.
        """
        shallow = underline_segments((((0.5, 0.0), (0.5, -0.04)),), (0.0, 1.0), -0.06, 0.06)
        deep = underline_segments((((0.5, 0.0), (0.5, -0.2)),), (0.0, 1.0), -0.06, 0.06)
        shallow_gap = shallow[1][0] - shallow[0][1]
        deep_gap = deep[1][0] - deep[0][1]
        assert deep_gap > shallow_gap
        # The deep crossing saturates at the diameter (2 * radius).
        assert deep_gap == pytest.approx(0.12)
        assert shallow_gap < 0.12

    def test_overlapping_gaps_merge(self) -> None:
        """Two nearby crossings whose intervals overlap cut one wide gap."""
        ink = (
            ((0.45, 0.0), (0.45, -0.15)),
            ((0.55, 0.0), (0.55, -0.15)),
        )
        segments = underline_segments(ink, (0.0, 1.0), -0.06, 0.06)
        assert len(segments) == 2

    def test_intervals_are_clipped_to_the_span(self) -> None:
        """A crossing at the span's edge consumes only the part inside it."""
        ink = (((0.0, 0.0), (0.0, -0.15)),)
        segments = underline_segments(ink, (0.0, 1.0), -0.06, 0.06)
        assert len(segments) == 1
        assert segments[0][0] > 0.0
        assert segments[0][1] == pytest.approx(1.0)

    def test_span_inside_a_gap_yields_nothing(self) -> None:
        """When the gaps consume the span, no underline is engraved."""
        ink = (((0.5, 0.0), (0.5, -0.15)),)
        segments = underline_segments(ink, (0.45, 0.55), -0.06, 0.06)
        assert segments == ()

    def test_min_length_drops_specks(self) -> None:
        """Kept segments below the emission floor are dropped."""
        ink = (((0.02, 0.0), (0.02, -0.15)),)
        kept = underline_segments(ink, (0.0, 1.0), -0.06, 0.06, min_length=0.05)
        assert all(hi - lo >= 0.05 for lo, hi in kept)

    def test_segments_are_increasing_and_disjoint(self) -> None:
        """The kept segments tile the span left to right without overlap."""
        ink = tuple(((x, 0.0), (x, -0.15)) for x in (0.2, 0.4, 0.6, 0.8))
        segments = underline_segments(ink, (0.0, 1.0), -0.06, 0.06)
        assert len(segments) == 5
        for (lo0, hi0), (lo1, hi1) in zip(segments, segments[1:]):
            assert hi0 < lo1
            assert lo0 < hi0 and lo1 < hi1

    def test_closed_shape_crossing_twice(self) -> None:
        """A loop crossing the line twice cuts the interval between crossings."""
        # A tall thin rectangle (a 'p' bowl analogue) straddling the line.
        ink = (
            ((0.4, 0.1), (0.4, -0.1)),
            ((0.6, 0.1), (0.6, -0.1)),
            ((0.4, 0.1), (0.6, 0.1)),
            ((0.4, -0.1), (0.6, -0.1)),
        )
        segments = underline_segments(ink, (0.0, 1.0), -0.06, 0.06)
        # The bottom rail lies 0.04 above the centerline (inside the radius),
        # so its whole interval is gapped, plus the two vertical rails.
        assert len(segments) == 2
        assert segments[0][0] == pytest.approx(0.0)
        assert segments[1][1] == pytest.approx(1.0)
