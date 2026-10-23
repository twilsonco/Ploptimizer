"""Unit tests for the list marker-column layout pre-pass.

``plt_optimizer.generate.list_layout.apply_list_marker_layout`` measures every
list line's marker and writes the per-line hanging indent (the widest marker
of the list + the cascaded marker gap). The tests inject a fake width probe
(no matplotlib), so the marker column, the per-group scoping, the gap
fraction, and the no-op paths are pinned directly.
"""

from __future__ import annotations

from typing import Dict, List

import pytest

from plt_optimizer.generate.list_layout import apply_list_marker_layout
from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine


def _line(
    text: str = "item text",
    *,
    marker: str = "",
    group: int = -1,
    list_item: bool = False,
    toolpath: float = 0.44,
    gap_fraction: float = 0.5,
    indent: float = 0.0,
) -> ResolvedTextLine:
    """Build a resolved line carrying the list metadata under test."""
    return ResolvedTextLine(
        text=text,
        nominal_text_height=0.5,
        toolpath_text_height=toolpath,
        cutter_diameter=0.06,
        character_spacing=0.09,
        line_spacing=0.1,
        marker_text=marker,
        align_group_id=group,
        list_item=list_item,
        indent_left=indent,
        list_marker_gap_fraction=gap_fraction,
    )


def _label(label_id: str, *lines: ResolvedTextLine) -> ResolvedLabel:
    """Build a minimal resolved label carrying the given lines."""
    return ResolvedLabel(
        id=label_id,
        count=1,
        width=6.0,
        height=3.0,
        margin=0.2,
        h_margin=0.2,
        v_margin=0.2,
        content=list(lines),
    )


def _char_probe(widths: Dict[str, float], calls: List[str] | None = None):
    """Build a probe charging the table's width per line text (0.1 default).

    Args:
        widths: Mapping of line text to its measured width in inches.
        calls: Optional list recording every measured text.

    Returns:
        A callable matching
        :data:`plt_optimizer.generate.list_layout.MarkerWidthProbe`.
    """

    def probe(line: ResolvedTextLine) -> float:
        if calls is not None:
            calls.append(line.text)
        return widths.get(line.text, 0.1)

    return probe


class TestNoOpPaths:
    """The pass costs nothing (and changes nothing) without list lines."""

    def test_plain_job_returns_same_objects(self) -> None:
        """A job with no list line is returned as the same label objects."""
        label = _label("plain", _line("A"), _line("B"))
        result = apply_list_marker_layout([label], probe=_char_probe({}))
        assert result == [label]
        assert result[0] is label

    def test_probe_never_runs_without_lists(self) -> None:
        """The list-less fast path renders nothing."""
        calls: List[str] = []
        label = _label("plain", _line("A"))
        apply_list_marker_layout([label], probe=_char_probe({}, calls))
        assert calls == []

    def test_continuation_only_line_gets_gap_only(self) -> None:
        """A list line with no marker (a grown continuation) keeps its indent."""
        line = _line("continuation", group=0, list_item=True, indent=0.0)
        label = _label("grown", line)
        result = apply_list_marker_layout([label], probe=_char_probe({}))
        # No marker to measure: the column is 0.0, the indent is the gap alone.
        assert result[0].content[0].indent_left == pytest.approx(0.5 * 0.44)


class TestMarkerColumn:
    """The indent is the group's widest marker plus the line's own gap."""

    def test_single_marker_line(self) -> None:
        """One item: indent = marker width + gap fraction * cap height."""
        line = _line("alpha", marker="-", group=0, list_item=True)
        label = _label("one", line)
        result = apply_list_marker_layout([label], probe=_char_probe({"-": 0.2}))
        assert result[0].content[0].indent_left == pytest.approx(0.2 + 0.5 * 0.44)

    def test_group_uses_the_widest_marker(self) -> None:
        """Every item of one list shares the widest marker's column."""
        lines = [
            _line("one", marker="i)", group=0, list_item=True),
            _line("two", marker="xvii)", group=0, list_item=True),
        ]
        label = _label("roman", *lines)
        result = apply_list_marker_layout([label], probe=_char_probe({"i)": 0.1, "xvii)": 0.4}))
        indents = [line.indent_left for line in result[0].content]
        assert indents[0] == indents[1]
        assert indents[0] == pytest.approx(0.4 + 0.5 * 0.44)

    def test_groups_are_independent(self) -> None:
        """Two list blocks measure their own columns."""
        lines = [
            _line("a", marker="-", group=0, list_item=True),
            _line("b", marker="1.", group=1, list_item=True),
        ]
        label = _label("two-lists", *lines)
        result = apply_list_marker_layout([label], probe=_char_probe({"-": 0.1, "1.": 0.3}))
        assert result[0].content[0].indent_left == pytest.approx(0.1 + 0.22)
        assert result[0].content[1].indent_left == pytest.approx(0.3 + 0.22)

    def test_gap_fraction_is_per_line(self) -> None:
        """The cascaded gap fraction scales with the line's own cap height."""
        line = _line("a", marker="-", group=0, list_item=True, gap_fraction=1.0, toolpath=0.5)
        label = _label("gap", line)
        result = apply_list_marker_layout([label], probe=_char_probe({"-": 0.1}))
        assert result[0].content[0].indent_left == pytest.approx(0.1 + 1.0 * 0.5)

    def test_plain_line_untouched(self) -> None:
        """A non-list line keeps indent 0.0 and object identity."""
        plain = _line("plain")
        marked = _line("item", marker="-", group=0, list_item=True)
        label = _label("mixed", plain, marked)
        result = apply_list_marker_layout([label], probe=_char_probe({"-": 0.1}))
        assert result[0].content[0] is plain
        assert result[0].content[0].indent_left == 0.0


class TestMeasurement:
    """The marker is measured as its own text, in the item's typography."""

    def test_marker_measured_standalone(self) -> None:
        """The probe receives the marker-only clone (the item text swapped)."""
        calls: List[str] = []
        line = _line("alpha", marker="-", group=0, list_item=True)
        label = _label("m", line)
        apply_list_marker_layout([label], probe=_char_probe({"-": 0.1}, calls))
        assert calls == ["-"]

    def test_marker_clone_carries_no_marker_metadata(self) -> None:
        """The measurement clone is not itself a list line (no recursion)."""
        seen: List[ResolvedTextLine] = []

        def probe(line: ResolvedTextLine) -> float:
            seen.append(line)
            return 0.1

        line = _line("alpha", marker="-", group=0, list_item=True)
        apply_list_marker_layout([_label("m", line)], probe=probe)
        assert seen[0].marker_text == ""
        assert seen[0].list_item is False
        assert seen[0].align_group_id == -1
        # The clone keeps the item's typography (the marker rides the item's
        # font/height/cutter).
        assert seen[0].toolpath_text_height == line.toolpath_text_height

    def test_identical_markers_measure_once(self) -> None:
        """The shared cache measures one marker per distinct typography."""
        calls: List[str] = []
        lines = [
            _line("a", marker="-", group=0, list_item=True),
            _line("b", marker="-", group=0, list_item=True),
        ]
        apply_list_marker_layout([_label("m", *lines)], probe=_char_probe({"-": 0.1}, calls))
        assert calls == ["-"]

    def test_empty_marker_never_measured(self) -> None:
        """An indent-only item (empty bullet_string) has no marker to measure."""
        calls: List[str] = []
        line = _line("a", marker="", group=0, list_item=True)
        apply_list_marker_layout([_label("m", line)], probe=_char_probe({}, calls))
        assert calls == []

    def test_noop_when_indent_already_set(self) -> None:
        """A label whose indents already match keeps its object identity."""
        line = _line("a", marker="-", group=0, list_item=True, indent=0.1 + 0.5 * 0.44)
        label = _label("m", line)
        result = apply_list_marker_layout([label], probe=_char_probe({"-": 0.1}))
        assert result[0] is label
