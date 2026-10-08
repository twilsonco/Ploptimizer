"""Unit tests for the word-level line-content reflow pre-pass.

``plt_optimizer.generate.line_content`` finds maximal runs of consecutive text
lines that enable ``optimize_line_content``, measures every candidate word on
every line's typography, and repartitions the group's words (word order kept,
only the line breaks move) so the widest line is as narrow as possible. The
tests inject a fake width probe (no matplotlib), so the grouping, the DP
partition, the space-advance measurement and the scope guards are pinned
directly.
"""

from __future__ import annotations

from typing import Dict, List

import pytest

from plt_optimizer.generate.line_content import (
    _measure_space_advance,
    _partition_cost,
    _partition_words,
    _reflow_groups,
    _reflow_label,
    apply_line_content_reflow,
)
from plt_optimizer.generate.resolution import (
    DEFAULT_OPTIMIZE_LINE_CONTENT,
    ResolvedLabel,
    ResolvedTextLine,
)


def _line(
    text: str = "AAA",
    *,
    enabled: bool = True,
    nominal: float = 0.5,
    cutter: float = 0.06,
    character_spacing: float = 0.09,
    space_width_fraction: float = 0.3,
    max_lines: int | None = None,
    max_h_compress: float = 0.0,
) -> ResolvedTextLine:
    """Build a resolved line with the reflow permission defaulting to enabled."""
    return ResolvedTextLine(
        text=text,
        nominal_text_height=nominal,
        toolpath_text_height=nominal - cutter,
        cutter_diameter=cutter,
        character_spacing=character_spacing,
        line_spacing=0.1,
        space_width_fraction=space_width_fraction,
        max_h_compress=max_h_compress,
        optimize_line_content=enabled,
        optimize_line_content_max_lines=max_lines,
    )


def _label(label_id: str, *lines: ResolvedTextLine, width: float = 4.0) -> ResolvedLabel:
    """Build a minimal resolved label carrying the given lines."""
    return ResolvedLabel(
        id=label_id,
        count=1,
        width=width,
        height=2.0,
        margin=0.1,
        h_margin=0.1,
        v_margin=0.1,
        content=list(lines),
    )


def _scale_probe(
    char_width: float = 0.1,
    space_width: float = 0.2,
    inner: float = 0.8,
    calls: List[int] | None = None,
):
    """Build a label-level probe charging the same widths as :func:`_char_probe`.

    A line is reported compressed when its natural width exceeds ``inner``
    (the label's content area), at the scale ``inner / width`` -- the same
    contract :meth:`RenderedLabel.compression_by_line` fulfils.

    Args:
        char_width: Width charged for every character.
        space_width: Width charged for every space.
        inner: The content width a line must fit within uncompressed.
        calls: Optional list recording each render's line count.

    Returns:
        A callable matching
        :data:`plt_optimizer.generate.cutter_downsize.ScaleProbe`.
    """

    def probe(label: ResolvedLabel) -> Dict[int, float]:
        if calls is not None:
            calls.append(len(label.content))
        scales: Dict[int, float] = {}
        for index, line in enumerate(label.content):
            width = char_width * len(line.text) + space_width * line.text.count(" ")
            if width > inner:
                scales[index] = inner / width
        return scales

    return probe


def _char_probe(char_width: float = 0.1, space_width: float = 0.2):
    """Build a probe charging ``char_width`` per character and ``space_width`` per space.

    Args:
        char_width: Width charged for every character.
        space_width: Width charged for every space.

    Returns:
        A callable matching
        :data:`plt_optimizer.generate.line_content.LineWidthProbe`.
    """

    def probe(line: ResolvedTextLine) -> float:
        return char_width * len(line.text) + space_width * line.text.count(" ")

    return probe


class TestDefaultConstant:
    """The shipped default keeps every line's authored text."""

    def test_default_module_constant_is_false(self) -> None:
        """The reflow is opt-in."""
        assert DEFAULT_OPTIMIZE_LINE_CONTENT is False


class TestReflowGroups:
    """Consecutive enabled lines form one independent group."""

    def test_empty_content_yields_no_groups(self) -> None:
        """A label with no lines has nothing to group."""
        assert _reflow_groups([]) == []

    def test_all_disabled_yields_no_groups(self) -> None:
        """A fully disabled label yields no groups at all."""
        content = [_line(enabled=False), _line(enabled=False)]
        assert _reflow_groups(content) == []

    def test_single_run_collects_consecutive_indices(self) -> None:
        """One enabled run yields one group carrying its content indices."""
        content = [_line(enabled=False), _line(), _line(), _line(enabled=False)]
        assert _reflow_groups(content) == [[1, 2]]

    def test_two_runs_split_on_the_disabled_line(self) -> None:
        """A disabled line breaks the group (the TODO's lines 2-3 / 5-8 case)."""
        content = [
            _line(),
            _line(),
            _line(enabled=False),
            _line(),
            _line(),
            _line(),
        ]
        assert _reflow_groups(content) == [[0, 1], [3, 4, 5]]

    def test_trailing_run_is_collected(self) -> None:
        """The run ending on the last line is not dropped."""
        content = [_line(enabled=False), _line(), _line()]
        assert _reflow_groups(content) == [[1, 2]]


class TestPartitionCost:
    """Segment widths join word widths with one space per interior junction."""

    def test_single_word_has_no_space(self) -> None:
        """A one-word segment is the word's own width."""
        assert _partition_cost([0.5, 0.5], 0.2, 0, 1) == pytest.approx(0.5)

    def test_two_words_add_one_space(self) -> None:
        """Two words cost their widths plus one space advance."""
        assert _partition_cost([0.5, 0.4], 0.2, 0, 2) == pytest.approx(1.1)

    def test_three_words_add_two_spaces(self) -> None:
        """Three words cost their widths plus two space advances."""
        assert _partition_cost([0.5, 0.4, 0.3], 0.2, 0, 3) == pytest.approx(1.6)

    def test_offset_window_uses_its_own_slice(self) -> None:
        """A window starting mid-list measures only its own words."""
        assert _partition_cost([9.0, 0.5, 0.4], 0.2, 1, 3) == pytest.approx(1.1)


class TestPartitionWords:
    """The DP partition cuts the word sequence into one segment per line."""

    def test_empty_input_yields_no_segments(self) -> None:
        """No lines means no segments."""
        assert _partition_words([], []) == []

    def test_segments_cover_every_word_in_order(self) -> None:
        """Segments are contiguous, ordered and cover the whole word list."""
        widths = [[0.4] * 6, [0.4] * 6]
        segments = _partition_words(widths, [0.2, 0.2])
        assert segments[0][0] == 0
        assert segments[-1][1] == 6
        for (_start_a, end_a), (start_b, _end_b) in zip(segments, segments[1:]):
            assert end_a == start_b

    def test_every_line_keeps_at_least_one_word(self) -> None:
        """No line is ever emptied by the partition."""
        widths = [[0.4] * 3, [0.4] * 3, [0.4] * 3]
        segments = _partition_words(widths, [0.2, 0.2, 0.2])
        assert all(end > start for start, end in segments)

    def test_balances_a_lopsided_group(self) -> None:
        """One long line and one short line split at the balanced cut point."""
        # Words: 1.0 wide, then three 0.1-wide words. Two lines: the best cut
        # is one word on the first line (1.0) and three on the second
        # (0.3 + 2*0.2 = 0.7); a 2/2 split costs 1.0 + 0.2 + 0.2 = 1.4.
        widths = [[1.0, 0.1, 0.1, 0.1], [1.0, 0.1, 0.1, 0.1]]
        segments = _partition_words(widths, [0.2, 0.2])
        assert segments == [(0, 1), (1, 4)]

    def test_prefers_the_narrowest_widest_line(self) -> None:
        """The objective is the widest line, not the character count."""
        # Four equal words on two lines: the 2/2 split (widest 1.0) beats
        # 1/3 (widest 1.4) and 3/1 (widest 1.4).
        widths = [[0.4] * 4, [0.4] * 4]
        segments = _partition_words(widths, [0.2, 0.2])
        assert segments == [(0, 2), (2, 4)]

    def test_uses_each_line_own_typography(self) -> None:
        """Segment widths measure with the receiving line's own widths."""
        # Line 1 renders every word 10x wider than line 2, so the cheap
        # words belong on line 2: one word on line 1 (1.0) beats putting two
        # of its wide words there (2.2).
        widths = [[1.0, 1.0, 0.1, 0.1], [0.1, 0.1, 0.01, 0.01]]
        segments = _partition_words(widths, [0.2, 0.2])
        assert segments == [(0, 1), (1, 4)]

    def test_single_line_group_keeps_every_word(self) -> None:
        """A one-line partition owns the whole word list."""
        widths = [[0.4] * 3]
        assert _partition_words(widths, [0.2]) == [(0, 3)]

    def test_words_equal_to_lines_gives_one_word_each(self) -> None:
        """With exactly one word per line the partition is forced."""
        widths = [[0.4] * 2, [0.4] * 2]
        assert _partition_words(widths, [0.2, 0.2]) == [(0, 1), (1, 2)]

    def test_more_lines_than_words_is_unreachable(self) -> None:
        """More lines than words has no feasible partition (no crash)."""
        widths = [[0.4], [0.4]]
        assert _partition_words(widths, [0.2, 0.2]) == []


class TestMeasureSpaceAdvance:
    """The space advance is measured from the line's own text when possible."""

    def test_measured_from_a_two_word_line(self) -> None:
        """A two-word line yields pair width minus the two word widths."""
        line = _line("AA BB")
        cache: Dict[tuple, float] = {}
        probe = _char_probe(char_width=0.3, space_width=0.25)
        for word in ("AA", "BB"):
            cache[(0, word)] = probe(_line(word))
        advance = _measure_space_advance(line, ["AA", "BB"], probe, cache, 0)
        # The pair probe charges the space both as a character (0.3) and as
        # the extra space width (0.25), so the measured advance is their sum.
        assert advance == pytest.approx(0.55)

    def test_measured_advance_is_clamped_non_negative(self) -> None:
        """Kerning across a space cannot produce a negative advance."""
        line = _line("AA BB")
        cache: Dict[tuple, float] = {(0, "AA"): 1.0, (0, "BB"): 1.0}

        def probe(_line_obj: ResolvedTextLine) -> float:
            return 1.5  # pair narrower than the two words alone

        advance = _measure_space_advance(line, ["AA", "BB"], probe, cache, 0)
        assert advance == 0.0

    def test_single_word_line_uses_the_formula(self) -> None:
        """A one-word line falls back to the documented PLT formula."""
        line = _line("AA", space_width_fraction=0.4, character_spacing=0.09)
        cache: Dict[tuple, float] = {}
        advance = _measure_space_advance(line, ["AA"], _char_probe(), cache, 0)
        assert advance == pytest.approx(0.4 * line.toolpath_text_height + 0.09)


class TestReflowLabel:
    """Per-label reflow: groups, guards, and clone semantics."""

    def test_disabled_label_is_untouched(self) -> None:
        """A label with no enabled line returns None (identity preserved)."""
        label = _label("a", _line("AAA BBB", enabled=False), _line("CCC", enabled=False))
        assert _reflow_label(label, _char_probe()) is None

    def test_single_line_run_is_untouched(self) -> None:
        """A lone enabled line has no sibling to exchange words with."""
        label = _label("a", _line("AAA BBB"), _line("CCC", enabled=False))
        assert _reflow_label(label, _char_probe()) is None

    def test_words_move_to_balance_two_lines(self) -> None:
        """The over-wide line's tail word moves down to its short sibling."""
        label = _label("a", _line("AAAAAA BB CC DD"), _line("E"))
        clone = _reflow_label(label, _char_probe(char_width=0.1, space_width=0.2))
        assert clone is not None
        assert [line.text for line in clone.content] == ["AAAAAA BB", "CC DD E"]

    def test_word_order_is_preserved(self) -> None:
        """Reflow only moves line breaks; the word sequence is unchanged."""
        label = _label(
            "a",
            _line("AAAA BBBB CCCC"),
            _line("D"),
            _line("E"),
            _line("F"),
        )
        clone = _reflow_label(label, _char_probe(char_width=0.1, space_width=0.2))
        assert clone is not None
        original = " ".join(word for line in label.content for word in line.text.split())
        reflowed = " ".join(word for line in clone.content for word in line.text.split())
        assert reflowed == original

    def test_words_can_travel_across_the_whole_group(self) -> None:
        """A word may move by more than one line inside a deep group."""
        label = _label(
            "a",
            _line("AAAA BBBB CCCC DDDD"),
            _line("E"),
            _line("F"),
            _line("G"),
        )
        clone = _reflow_label(label, _char_probe(char_width=0.1, space_width=0.2))
        assert clone is not None
        # The long first line sheds words all the way down to the last line.
        assert clone.content[-1].text.split()[-1] == "G"
        assert clone.content[0].text.split() == ["AAAA"]

    def test_independent_groups_are_optimized_separately(self) -> None:
        """Two runs in one label each balance inside their own words."""
        label = _label(
            "a",
            _line("AAAAAA BB"),
            _line("CC"),
            _line("SOLO", enabled=False),
            _line("DDDDDD EE"),
            _line("FF"),
        )
        clone = _reflow_label(label, _char_probe(char_width=0.1, space_width=0.2))
        assert clone is not None
        texts = [line.text for line in clone.content]
        assert texts[2] == "SOLO"  # disabled line untouched
        # Each group balanced within its own words; no cross-group movement.
        assert set(texts[0].split()) | set(texts[1].split()) == {"AAAAAA", "BB", "CC"}
        assert set(texts[3].split()) | set(texts[4].split()) == {"DDDDDD", "EE", "FF"}

    def test_disabled_line_keeps_its_text(self) -> None:
        """A disabled line inside a label never exchanges words."""
        label = _label("a", _line("AAAAAA"), _line("BB", enabled=False))
        clone = _reflow_label(label, _char_probe())
        # The enabled run is a single line, so nothing changes at all.
        assert clone is None

    def test_group_with_fewer_words_than_lines_is_skipped(self) -> None:
        """A group that cannot keep every line non-empty is left alone."""
        label = _label("a", _line("ONEWORD"), _line("TWO"))
        clone = _reflow_label(label, _char_probe())
        assert clone is None
        assert [line.text for line in label.content] == ["ONEWORD", "TWO"]

    def test_blank_line_group_is_skipped(self) -> None:
        """A blank line in the group makes the reflow a no-op."""
        label = _label("a", _line("AAAA BBBB"), _line(""))
        clone = _reflow_label(label, _char_probe())
        assert clone is None

    def test_already_balanced_group_is_untouched(self) -> None:
        """A group whose authored split is already optimal keeps its text."""
        label = _label("a", _line("AAAA BBBB"), _line("CCCC DDDD"))
        clone = _reflow_label(label, _char_probe(char_width=0.1, space_width=0.2))
        assert clone is None

    def test_clone_keeps_the_disabled_lines(self) -> None:
        """The clone carries the untouched disabled lines verbatim."""
        label = _label(
            "a",
            _line("AAAAAA BB CC DD"),
            _line("E"),
            _line("SOLO", enabled=False),
        )
        clone = _reflow_label(label, _char_probe(char_width=0.1, space_width=0.2))
        assert clone is not None
        assert clone.content[2].text == "SOLO"
        assert clone.content[2].optimize_line_content is False

    def test_reflow_preserves_line_typography(self) -> None:
        """Reflowed lines keep every attribute except their text."""
        label = _label(
            "a",
            _line("AAAAAA BB CC DD", nominal=0.75),
            _line("E", nominal=0.75),
        )
        clone = _reflow_label(label, _char_probe(char_width=0.1, space_width=0.2))
        assert clone is not None
        for original, reflowed in zip(label.content, clone.content):
            assert reflowed.nominal_text_height == original.nominal_text_height
            assert reflowed.cutter_diameter == original.cutter_diameter
            assert reflowed.font == original.font


class TestApplyLineContentReflow:
    """The export pre-pass entry point: identity, guards, and probe injection."""

    def test_no_enabled_label_returns_the_same_objects(self) -> None:
        """A job without the flag is a no-op preserving label identity."""
        labels = [_label("a", _line("AAA", enabled=False))]
        result = apply_line_content_reflow(labels, probe=_char_probe())
        assert result[0] is labels[0]

    def test_empty_job_is_a_no_op(self) -> None:
        """An empty job passes through unchanged."""
        assert apply_line_content_reflow([], probe=_char_probe()) == []

    def test_untouched_labels_keep_their_identity(self) -> None:
        """Only reflowed labels are replaced; the rest keep object identity."""
        untouched = _label("b", _line("AAA", enabled=False))
        reflowed = _label("a", _line("AAAAAA BB CC DD"), _line("E"))
        result = apply_line_content_reflow([untouched, reflowed], probe=_char_probe(0.1, 0.2))
        assert result[0] is untouched
        assert result[1] is not reflowed
        assert result[1].content[0].text == "AAAAAA BB"

    def test_balanced_label_keeps_its_identity(self) -> None:
        """A label whose groups are already optimal is not cloned."""
        label = _label("a", _line("AAAA BBBB"), _line("CCCC DDDD"))
        result = apply_line_content_reflow([label], probe=_char_probe(0.1, 0.2))
        assert result[0] is label

    def test_probe_receives_word_only_lines(self) -> None:
        """The probe measures words standalone (no spaces)."""
        seen: List[str] = []

        def probe(line: ResolvedTextLine) -> float:
            seen.append(line.text)
            return 0.1 * len(line.text)

        # Both lines are single-word, so no pair probe is needed and every
        # measurement is a bare word.
        label = _label("a", _line("AAAAAA"), _line("E"))
        apply_line_content_reflow([label], probe=probe)
        assert all(" " not in text for text in seen)
        assert set(seen) == {"AAAAAA", "E"}

    def test_probe_receives_pair_for_space_measurement(self) -> None:
        """A line with two words contributes a pair measurement for its space."""
        seen: List[str] = []

        def probe(line: ResolvedTextLine) -> float:
            seen.append(line.text)
            return 0.1 * len(line.text) + 0.2 * line.text.count(" ")

        label = _label("a", _line("AA BB"), _line("CCCC"))
        apply_line_content_reflow([label], probe=probe)
        assert "AA BB" in seen

    def test_default_probe_is_used_when_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Omitting the probe routes through the label_renderer measurement."""
        calls: List[str] = []

        def fake_measure(line: ResolvedTextLine) -> float:
            calls.append(line.text)
            return 0.1 * len(line.text)

        monkeypatch.setattr(
            "plt_optimizer.generate.label_renderer.measure_line_natural_width",
            fake_measure,
        )
        label = _label("a", _line("AAAAAA BB CC DD"), _line("E"))
        result = apply_line_content_reflow([label])
        assert result[0].content[0].text == "AAAAAA BB"
        assert calls  # the renderer probe fired


class TestMeasureLineNaturalWidth:
    """The renderer-side natural-width probe (real fonts, no label frame)."""

    def test_width_grows_with_text(self) -> None:
        """A longer word measures wider than a shorter one."""
        from plt_optimizer.generate.label_renderer import measure_line_natural_width

        short = measure_line_natural_width(_line("AB"))
        long = measure_line_natural_width(_line("ABCD"))
        assert long > short > 0.0

    def test_blank_line_measures_zero(self) -> None:
        """A line with no glyphs has no width."""
        from plt_optimizer.generate.label_renderer import measure_line_natural_width

        assert measure_line_natural_width(_line("")) == 0.0

    def test_space_advances_the_line(self) -> None:
        """Two words measure wider than the same words without the space."""
        from plt_optimizer.generate.label_renderer import measure_line_natural_width

        joined = measure_line_natural_width(_line("ABAB"))
        spaced = measure_line_natural_width(_line("AB AB"))
        assert spaced > joined

    def test_unknown_font_raises_label_render_error(self) -> None:
        """An unresolvable font name aborts the measurement loudly."""
        from plt_optimizer.generate.label_renderer import (
            LabelRenderError,
            measure_line_natural_width,
        )

        line = _line("ABC")
        object.__setattr__(line, "font", "no-such-font-anywhere")
        with pytest.raises(LabelRenderError):
            measure_line_natural_width(line)


class TestPrePassOrdering:
    """The reflow runs before the compression-driven pre-passes."""

    def test_reflowed_text_is_what_the_compression_pre_pass_measures(
        self, tmp_path: object
    ) -> None:
        """An export with reflow enabled compresses less than the authored split.

        The fixture's ``reflowed`` label balances to zero compression while
        its opted-out twin (identical text, flag off) must compress.
        """
        from pathlib import Path

        from plt_optimizer.generate.resolution import resolve_job_spec
        from plt_optimizer.generate.schema import parse_yaml
        from plt_optimizer.generate.vectorize import export_per_cutter_plts

        spec_path = Path("tests_deps/optimize_line_content_job.yaml")
        job = parse_yaml(spec_path)
        labels = resolve_job_spec(job)
        result = export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=Path(str(tmp_path)),
            job_id="olc",
            optimize=False,
            plots=False,
        )
        reflowed = result.rendered_labels["reflowed"]
        opted_out = result.rendered_labels["opted_out"]
        assert reflowed.compression_by_line == {}
        assert opted_out.compression_by_line  # the authored split compresses


class TestGrowthCap:
    """optimize_line_content_max_lines grows a group; it never shrinks one."""

    def test_grows_until_compression_clears(self) -> None:
        """A single long line expands to the first line count that fits."""
        calls: List[int] = []
        label = _label(
            "grow",
            _line("AAAA BBBB CCCC DDDD EEEE FFFF", max_lines=3, max_h_compress=0.5),
            width=1.4,
        )
        clone = _reflow_label(
            label,
            _char_probe(0.1, 0.2),
            _scale_probe(0.1, 0.2, inner=1.2, calls=calls),
        )
        assert clone is not None
        assert [line.text for line in clone.content] == ["AAAA BBBB", "CCCC DDDD", "EEEE FFFF"]
        # M=1 (3.0) and M=2 (1.6) compress; M=3 (1.0) fits the 1.2in area.
        assert calls == [1, 2, 3]

    def test_grown_lines_clone_the_group_typography(self) -> None:
        """Inserted lines copy the group's last line attributes verbatim."""
        label = _label(
            "grow",
            _line("AAAA BBBB CCCC DDDD", nominal=0.5, max_lines=3, max_h_compress=0.5),
            _line("E", nominal=0.75, max_lines=3, max_h_compress=0.5),
            width=1.4,
        )
        clone = _reflow_label(
            label,
            _char_probe(0.1, 0.2),
            _scale_probe(0.1, 0.2, inner=1.2),
        )
        assert clone is not None
        assert len(clone.content) == 3
        donor = label.content[1]
        inserted = clone.content[2]
        assert inserted.nominal_text_height == donor.nominal_text_height
        assert inserted.toolpath_text_height == donor.toolpath_text_height
        assert inserted.cutter_diameter == donor.cutter_diameter
        assert inserted.font == donor.font
        assert inserted.optimize_line_content is True

    def test_growth_preserves_word_order(self) -> None:
        """Growth moves line breaks only; the word sequence is unchanged."""
        label = _label(
            "grow",
            _line("AAAA BBBB CCCC DDDD", max_lines=3, max_h_compress=0.5),
            _line("E", max_lines=3, max_h_compress=0.5),
            width=1.4,
        )
        clone = _reflow_label(
            label,
            _char_probe(0.1, 0.2),
            _scale_probe(0.1, 0.2, inner=1.2),
        )
        assert clone is not None
        original = " ".join(word for line in label.content for word in line.text.split())
        grown = " ".join(word for line in clone.content for word in line.text.split())
        assert grown == original
        assert len(clone.content) >= len(label.content)

    def test_cap_exhausted_keeps_widest_layout_and_warns(self, caplog) -> None:
        """A group still compressed at N keeps its layout and logs a WARNING."""
        label = _label(
            "tight",
            _line("AAAA BBBB CCCC DDDD EEEE FFFF", max_lines=2, max_h_compress=0.5),
            width=1.4,
        )
        with caplog.at_level("WARNING"):
            clone = _reflow_label(
                label,
                _char_probe(0.1, 0.2),
                _scale_probe(0.1, 0.2, inner=1.2),
            )
        assert clone is not None
        assert len(clone.content) == 2  # capped at N, not grown past it
        assert "optimize_line_content_max_lines cap" in caplog.text

    def test_cap_at_group_size_is_inert(self) -> None:
        """N == G leaves the cap inert: plain reflow, no renders."""
        calls: List[int] = []
        label = _label(
            "inert",
            _line("AAAAAA BB CC DD", max_lines=2, max_h_compress=0.5),
            _line("E", max_lines=2, max_h_compress=0.5),
        )
        clone = _reflow_label(
            label,
            _char_probe(0.1, 0.2),
            _scale_probe(0.1, 0.2, inner=1.2, calls=calls),
        )
        assert clone is not None
        assert len(clone.content) == 2
        assert [line.text for line in clone.content] == ["AAAAAA BB", "CC DD E"]
        assert calls == []  # the probe is never called for an inert cap

    def test_cap_below_group_size_is_inert(self) -> None:
        """N < G never removes lines: the group keeps all of them."""
        label = _label(
            "inert",
            _line("AAAAAA BB", max_lines=2, max_h_compress=0.5),
            _line("CC", max_lines=2, max_h_compress=0.5),
            _line("DDDDDD", max_lines=2, max_h_compress=0.5),
        )
        clone = _reflow_label(
            label,
            _char_probe(0.1, 0.2),
            _scale_probe(0.1, 0.2, inner=1.2),
        )
        assert clone is not None
        assert len(clone.content) == 3  # never collapsed to 2

    def test_uncapped_group_never_renders(self) -> None:
        """A group without the cap keeps the historical probe-free behaviour."""
        calls: List[int] = []
        label = _label("plain", _line("AAAAAA BB CC DD"), _line("E"))
        clone = _reflow_label(
            label,
            _char_probe(0.1, 0.2),
            _scale_probe(0.1, 0.2, inner=1.2, calls=calls),
        )
        assert clone is not None
        assert calls == []

    def test_no_budget_group_skips_the_probe(self) -> None:
        """Without a max_h_compress budget the growth loop cannot measure."""
        calls: List[int] = []
        label = _label(
            "nobudget",
            _line("AAAA BBBB CCCC DDDD", max_lines=3, max_h_compress=0.0),
            width=1.4,
        )
        clone = _reflow_label(
            label,
            _char_probe(0.1, 0.2),
            _scale_probe(0.1, 0.2, inner=1.2, calls=calls),
        )
        # The full-group pass on a single-line group changes nothing.
        assert clone is None
        assert calls == []

    def test_growth_ceiling_is_the_word_count(self) -> None:
        """A cap above the word count stops at one word per line."""
        calls: List[int] = []
        label = _label(
            "words",
            _line("AAAA BBBB", max_lines=9, max_h_compress=0.5),
            width=0.5,
        )
        clone = _reflow_label(
            label,
            _char_probe(0.1, 0.2),
            _scale_probe(0.1, 0.2, inner=0.25, calls=calls),
        )
        assert clone is not None
        assert len(clone.content) == 2  # two words, two lines
        assert [line.text for line in clone.content] == ["AAAA", "BBBB"]
        assert calls == [1, 2]

    def test_growth_shifts_later_lines(self) -> None:
        """Lines after a grown group keep their text and attributes."""
        label = _label(
            "shift",
            _line("AAAA BBBB CCCC DDDD EEEE FFFF", max_lines=3, max_h_compress=0.5),
            _line("TAIL", enabled=False),
            width=1.4,
        )
        clone = _reflow_label(
            label,
            _char_probe(0.1, 0.2),
            _scale_probe(0.1, 0.2, inner=1.2),
        )
        assert clone is not None
        assert len(clone.content) == 4
        assert clone.content[-1].text == "TAIL"
        assert clone.content[-1].optimize_line_content is False

    def test_two_groups_grow_independently(self) -> None:
        """Each capped group expands on its own terms."""
        label = _label(
            "two",
            _line("AAAA BBBB CCCC DDDD EEEE FFFF", max_lines=3, max_h_compress=0.5),
            _line("SOLO", enabled=False),
            _line("GGGG HHHH IIII JJJJ KKKK LLLL", max_lines=3, max_h_compress=0.5),
            width=1.4,
        )
        clone = _reflow_label(
            label,
            _char_probe(0.1, 0.2),
            _scale_probe(0.1, 0.2, inner=1.2),
        )
        assert clone is not None
        assert len(clone.content) == 7
        assert clone.content[3].text == "SOLO"
        assert clone.content[3].optimize_line_content is False

    def test_apply_reflow_threads_the_scale_probe(self) -> None:
        """The public entry point forwards the scale probe to the loop."""
        label = _label(
            "grow",
            _line("AAAA BBBB CCCC DDDD EEEE FFFF", max_lines=3, max_h_compress=0.5),
            width=1.4,
        )
        result = apply_line_content_reflow(
            [label],
            probe=_char_probe(0.1, 0.2),
            scale_probe=_scale_probe(0.1, 0.2, inner=1.2),
        )
        assert len(result[0].content) == 3
