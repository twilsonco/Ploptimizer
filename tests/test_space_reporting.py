"""Pins for the ``<space>`` report surface.

The width a dynamic gap took is a render-time measurement, invisible in the
YAML, so it reaches the shop only through the layout report (text) and the
generation report (JSON). These tests drive the shared finding collector with
hand-built rendered labels: markup is parsed by the real tag, the consumed
slack is the map the renderer publishes, and every assertion reads the
rendered text.
"""

from __future__ import annotations

import dataclasses

from plt_optimizer.generate.layout_report import (
    collect_line_findings,
    format_layout_report,
    has_layout_findings,
)
from plt_optimizer.generate.resolution import ResolvedTextLine
from tests.test_layout_report import _label, _line


def _space_line(markup: str, text: str) -> ResolvedTextLine:
    """A resolved line whose ``<space>`` markup is parsed by the real tag."""
    return _line(text).with_markup(markup)


class TestDynamicSpaceReport:
    """A line carrying ``<space>`` reports the width its gaps took."""

    def test_the_tag_is_a_compact_finding(self) -> None:
        """A marked line prints its slot count and the width each gap took."""
        line = _space_line("AB<space>CD", "AB CD")
        resolved, rendered = _label("l1", [line], space_slack={0: 0.5})

        text = "\n".join(format_layout_report([resolved], {"l1": rendered}))

        assert "'AB CD' dynamic space 1 slot(s) x 0.500in" in text
        assert has_layout_findings([resolved], {"l1": rendered}) is True

    def test_the_finding_carries_the_slot_count_and_width(self) -> None:
        """The structured field is the ``(slots, inches)`` pair (JSON surface)."""
        line = _space_line("AB<space>CD", "AB CD")
        resolved, rendered = _label("l1", [line], space_slack={0: 0.625})

        findings = collect_line_findings(rendered)

        assert [finding.dynamic_space for finding in findings] == [(1, 0.625)]

    def test_every_marked_gap_is_counted(self) -> None:
        """Two markers on a line are two slots sharing one normalized width."""
        line = _space_line("AA<space>BB<space:2>CC", "AA BB CC")
        resolved, rendered = _label("l1", [line], space_slack={0: 0.4})

        text = "\n".join(format_layout_report([resolved], {"l1": rendered}))

        assert "dynamic space 2 slot(s) x 0.400in" in text

    def test_a_leading_marker_is_a_slot(self) -> None:
        """A marker in front of the first glyph is counted (the right-align inset)."""
        line = _space_line("<space>END", "END")
        resolved, rendered = _label("l1", [line], space_slack={0: 0.75})

        findings = collect_line_findings(rendered)

        assert [finding.dynamic_space for finding in findings] == [(1, 0.75)]

    def test_a_trailing_marker_is_a_slot(self) -> None:
        """A marker past the last glyph is counted (its slack pulls the ink left)."""
        line = _space_line("START<space>", "START")
        resolved, rendered = _label("l1", [line], space_slack={0: 0.25})

        findings = collect_line_findings(rendered)

        assert [finding.dynamic_space for finding in findings] == [(1, 0.25)]

    def test_zero_width_is_reported_as_a_measurement(self) -> None:
        """A line with no slack to give took 0.000in -- reported, not hidden.

        The width is the fill share, and a box-wide line has none; the gaps
        then keep their minimum, which is a fact the shop needs (the ink
        overflows the box). Omitting the line would read as "no marker".
        """
        line = _space_line("AB<space:3>CD", "AB CD")
        resolved, rendered = _label("l1", [line], space_slack={0: 0.0})

        text = "\n".join(format_layout_report([resolved], {"l1": rendered}))

        assert "dynamic space 1 slot(s) x 0.000in" in text

    def test_a_zero_minimum_counts_as_a_slot(self) -> None:
        """``<space:0>`` is a legal marker: a slot whose floor is 0.0."""
        line = _space_line("AB<space:0>CD", "AB CD")
        resolved, rendered = _label("l1", [line], space_slack={0: 1.5})

        findings = collect_line_findings(rendered)

        assert [finding.dynamic_space for finding in findings] == [(1, 1.5)]

    def test_an_unmarked_line_is_not_a_finding(self) -> None:
        """A plain line (and a line with other tags) produces no space finding."""
        plain = _line("AB CD")
        resolved, rendered = _label("l1", [plain], space_slack={0: 0.5})

        assert format_layout_report([resolved], {"l1": rendered}) == []
        assert collect_line_findings(rendered) == []
        assert has_layout_findings([resolved], {"l1": rendered}) is False

    def test_the_slack_of_another_line_is_not_reported(self) -> None:
        """The finding is per line: an unmarked line's slack entry is ignored."""
        marked = _space_line("AA<space>BB", "AA BB")
        plain = _line("CC DD")
        resolved, rendered = _label("l1", [marked, plain], space_slack={0: 0.5, 1: 0.9})

        findings = collect_line_findings(rendered)

        assert [(f.line_index, f.dynamic_space) for f in findings] == [(0, (1, 0.5))]

    def test_the_finding_strips_markup(self) -> None:
        """The report prints the plain text, never the tags."""
        line = _space_line("AB<space>CD", "AB CD")
        resolved, rendered = _label("l1", [line], space_slack={0: 0.5})

        text = "\n".join(format_layout_report([resolved], {"l1": rendered}, full=True))

        assert "<space" not in text

    def test_a_space_line_combines_with_other_findings(self) -> None:
        """One line can carry a gap and a bold run; both are reported."""
        line = _space_line("AB<space><b>C</b>D", "AB CD")
        bold = dataclasses.replace(line, bold_cutter_diameter=0.09)
        resolved, rendered = _label("l1", [bold], space_slack={0: 0.3})

        text = "\n".join(format_layout_report([resolved], {"l1": rendered}))

        assert "dynamic space 1 slot(s) x 0.300in" in text
        assert "bold cutter 0.090in" in text
