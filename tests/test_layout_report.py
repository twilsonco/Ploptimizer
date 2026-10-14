"""Unit tests for the shared LAYOUT REPORT formatter.

``plt_optimizer.generate.layout_report`` is pure Python: the tests build
``ResolvedLabel`` / ``RenderedLabel`` fixtures directly (no rendering), so
the formatting contract for the ``generate`` CLI and the integration
runner's Phase 3.6 is pinned without matplotlib.
"""

from __future__ import annotations

from plt_optimizer.generate.label_renderer import DescenderFix, RenderedLabel
from plt_optimizer.generate.layout_report import format_layout_report, has_layout_findings
from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine


def _line(text: str, line_spacing: float = 0.0) -> ResolvedTextLine:
    """Build a minimal resolved text line with the given requested spacing."""
    return ResolvedTextLine(
        text=text,
        nominal_text_height=0.3,
        toolpath_text_height=0.27,
        cutter_diameter=0.03,
        character_spacing=0.0,
        line_spacing=line_spacing,
        max_h_compress=0.0,
        text_h_alignment="center",
    )


def _label(
    label_id: str,
    lines: list[ResolvedTextLine],
    *,
    compression: dict[int, float] | None = None,
    spacing: dict[int, float] | None = None,
    downsizes: dict[int, tuple[float, float]] | None = None,
    descenders: dict[int, tuple[DescenderFix, ...]] | None = None,
) -> tuple[ResolvedLabel, RenderedLabel]:
    """Build a (resolved, rendered) pair carrying the given render-time maps."""
    resolved = ResolvedLabel(
        id=label_id,
        count=1,
        width=4.0,
        height=2.0,
        margin=0.1,
        h_margin=0.1,
        v_margin=0.1,
        holes=[],
        content=lines,
        cutter_downsize_by_line=dict(downsizes or {}),
    )
    rendered = RenderedLabel(
        source_label=resolved,
        plt_content="IN;PA;SP;",
        x_min=0.0,
        y_min=0.0,
        x_max=4.0,
        y_max=2.0,
        width=4.0,
        height=2.0,
        compression_by_line=dict(compression or {}),
        line_spacing_by_line=dict(spacing or {}),
        descender_adjustments_by_line=dict(descenders or {}),
    )
    return resolved, rendered


def _fix(
    line_index: int = 0,
    glyph_index: int = 2,
    char: str = "p",
    scale_y: float = 0.6,
    raise_amount: float = 0.071,
    gap_before: float = -0.077,
    gap_after: float = 0.045,
) -> DescenderFix:
    """Build a descending-character relief record for report tests."""
    return DescenderFix(
        line_index=line_index,
        glyph_index=glyph_index,
        char=char,
        scale_y=scale_y,
        raise_amount=raise_amount,
        gap_before=gap_before,
        gap_after=gap_after,
    )


class TestCompactMode:
    """Compact mode (the CLI default) prints only labels with findings."""

    def test_untouched_job_produces_no_lines(self) -> None:
        """A job with no compression and no clamped spacing stays silent."""
        resolved, rendered = _label("l1", [_line("A", 0.1), _line("B")], spacing={0: 0.1})

        assert format_layout_report([resolved], {"l1": rendered}) == []
        assert not has_layout_findings([resolved], {"l1": rendered})

    def test_spacing_deviation_is_a_finding(self) -> None:
        """A clamped gap prints with the requested value in parentheses."""
        resolved, rendered = _label("l1", [_line("A", 0.5), _line("B")], spacing={0: 0.2})

        lines = format_layout_report([resolved], {"l1": rendered})

        assert lines == [
            "Label l1:",
            "    Margins: horizontal 0.100in (left/right), vertical 0.100in (top/bottom)",
            "    Line spacing (gaps between lines):",
            "      Line 0: 0.200in (requested 0.500in)",
            "    Line 0: 'A' spacing below 0.200in (requested 0.500in)",
        ]
        assert has_layout_findings([resolved], {"l1": rendered})

    def test_compression_is_a_finding(self) -> None:
        """A compressed line keeps the historical scale-line format."""
        resolved, rendered = _label("l1", [_line("WIDE"), _line("B")], compression={0: 0.87})

        lines = format_layout_report([resolved], {"l1": rendered})

        assert lines == [
            "Label l1:",
            "    Margins: horizontal 0.100in (left/right), vertical 0.100in (top/bottom)",
            "    Line 0: 'WIDE' scale 0.870 (13.0% compressed)",
        ]
        assert has_layout_findings([resolved], {"l1": rendered})

    def test_compression_and_spacing_print_in_line_order(self) -> None:
        """Findings on different lines print in content order."""
        resolved, rendered = _label(
            "l1",
            [_line("WIDE", 0.5), _line("MID", 0.1), _line("B")],
            compression={2: 0.5},
            spacing={0: 0.2, 1: 0.1},
        )

        lines = format_layout_report([resolved], {"l1": rendered})

        assert lines == [
            "Label l1:",
            "    Margins: horizontal 0.100in (left/right), vertical 0.100in (top/bottom)",
            "    Line spacing (gaps between lines):",
            "      Line 0: 0.200in (requested 0.500in)",
            "      Line 1: 0.100in",
            "    Line 0: 'WIDE' spacing below 0.200in (requested 0.500in)",
            "    Line 2: 'B' scale 0.500 (50.0% compressed)",
        ]

    def test_missing_rendered_id_is_skipped(self) -> None:
        """Labels absent from the render cache never print (and never crash)."""
        resolved, rendered = _label("l1", [_line("A", 0.5), _line("B")], spacing={0: 0.2})
        other, _other_rendered = _label("other", [_line("X")])

        lines = format_layout_report([resolved, other], {"l1": rendered})

        assert "Label other:" not in lines
        assert "Label l1:" in lines


class TestFullMode:
    """Full mode (integration runner, CLI -v) prints every gap."""

    def test_every_gap_prints_with_requested_value(self) -> None:
        """Unclamped gaps print bare; clamped gaps show the requested delta."""
        resolved, rendered = _label(
            "l1",
            [_line("A", 0.1), _line("B", 0.5), _line("C")],
            spacing={0: 0.1, 1: 0.2},
        )

        lines = format_layout_report([resolved], {"l1": rendered}, full=True)

        assert lines == [
            "Label l1:",
            "    Margins: horizontal 0.100in (left/right), vertical 0.100in (top/bottom)",
            "    Line spacing (gaps between lines):",
            "      Line 0: 0.100in",
            "      Line 1: 0.200in (requested 0.500in)",
        ]

    def test_untouched_label_gets_summary_line(self) -> None:
        """A single untouched line prints the natural-width/spacing summary."""
        resolved, rendered = _label("l1", [_line("ONLY", 0.5)])

        lines = format_layout_report([resolved], {"l1": rendered}, full=True)

        assert lines[:3] == [
            "Label l1:",
            "    Margins: horizontal 0.100in (left/right), vertical 0.100in (top/bottom)",
            "    all lines at natural width / spacing",
        ]

    def test_untouched_job_appends_footer(self) -> None:
        """A full-mode job without findings ends with the check-mark footer."""
        resolved, rendered = _label("l1", [_line("ONLY")])

        lines = format_layout_report([resolved], {"l1": rendered}, full=True)

        assert lines[-1].startswith("\u2713 No compression or line-spacing adjustments")

    def test_finding_job_has_no_footer(self) -> None:
        """A job with findings never prints the no-findings footer."""
        resolved, rendered = _label("l1", [_line("A", 0.5), _line("B")], spacing={0: 0.2})

        lines = format_layout_report([resolved], {"l1": rendered}, full=True)

        assert not any("No compression" in line for line in lines)

    def test_labels_print_in_resolved_order(self) -> None:
        """Report order follows the resolved-labels (content) order."""
        r1, d1 = _label("a", [_line("A", 0.5), _line("B")], spacing={0: 0.2})
        r2, d2 = _label("b", [_line("X", 0.5), _line("Y")], spacing={0: 0.1})

        lines = format_layout_report([r1, r2], {"a": d1, "b": d2}, full=True)

        assert [line for line in lines if line.startswith("Label ")] == ["Label a:", "Label b:"]


class TestCutterDownsizeFindings:
    """The compression-driven cutter reduction prints as its own finding."""

    def test_downsize_is_a_finding(self) -> None:
        """A downsized line prints the original -> final cutter pair."""
        resolved, rendered = _label("l1", [_line("WIDE"), _line("B")], downsizes={0: (0.06, 0.045)})

        lines = format_layout_report([resolved], {"l1": rendered})

        assert lines == [
            "Label l1:",
            "    Margins: horizontal 0.100in (left/right), vertical 0.100in (top/bottom)",
            "    Line 0: 'WIDE' cutter 0.060in -> 0.045in (downsized for compression)",
        ]
        assert has_layout_findings([resolved], {"l1": rendered})

    def test_downsize_and_compression_print_together(self) -> None:
        """A compressed line that also downsized prints both lines, scale first."""
        resolved, rendered = _label(
            "l1",
            [_line("WIDE"), _line("B")],
            compression={0: 0.85},
            downsizes={0: (0.06, 0.045)},
        )

        lines = format_layout_report([resolved], {"l1": rendered})

        assert lines == [
            "Label l1:",
            "    Margins: horizontal 0.100in (left/right), vertical 0.100in (top/bottom)",
            "    Line 0: 'WIDE' scale 0.850 (15.0% compressed)",
            "    Line 0: 'WIDE' cutter 0.060in -> 0.045in (downsized for compression)",
        ]

    def test_downsize_only_line_prints_without_compression_map(self) -> None:
        """The downsized scale map is rebuilt on re-render, so the downsize
        finding must print even when compression_by_line is empty."""
        resolved, rendered = _label("l1", [_line("A"), _line("WIDE")], downsizes={1: (0.09, 0.06)})

        lines = format_layout_report([resolved], {"l1": rendered}, full=True)

        assert "    Line 1: 'WIDE' cutter 0.090in -> 0.060in (downsized for compression)" in lines


class TestEmptyInputs:
    """Degenerate inputs produce empty reports, never exceptions."""

    def test_no_rendered_labels(self) -> None:
        """An empty render cache yields no lines in either mode."""
        resolved, _rendered = _label("l1", [_line("A")])

        assert format_layout_report([resolved], {}) == []
        assert format_layout_report([resolved], {}, full=True) == []
        assert not has_layout_findings([resolved], {})

    def test_no_labels(self) -> None:
        """A job with no labels yields no lines."""
        assert format_layout_report([], {}) == []


class TestDescenderReliefFindings:
    """The per-glyph descending-character relief is a report finding."""

    def test_relief_is_a_compact_finding(self) -> None:
        """A relieved glyph prints its scale, raise and gap transition."""
        resolved, rendered = _label(
            "l1",
            [_line("Depth", 0.05), _line("RANGE")],
            spacing={0: 0.05},
            descenders={0: (_fix(),)},
        )

        lines = format_layout_report([resolved], {"l1": rendered})

        assert lines == [
            "Label l1:",
            "    Margins: horizontal 0.100in (left/right), vertical 0.100in (top/bottom)",
            "    Line spacing (gaps between lines):",
            "      Line 0: 0.050in",
            "    Line 0: 'Depth' glyph 'p' descender v-scale 0.600, "
            "raised 0.071in (gap -0.077in -> 0.045in)",
        ]
        assert has_layout_findings([resolved], {"l1": rendered})

    def test_multiple_reliefs_print_in_glyph_order(self) -> None:
        """Every relieved glyph on a line prints its own line."""
        resolved, rendered = _label(
            "l1",
            [_line("Depth", 0.05), _line("RANGE")],
            descenders={0: (_fix(glyph_index=2, char="p"), _fix(glyph_index=4, char="h"))},
        )

        lines = format_layout_report([resolved], {"l1": rendered})

        assert sum(1 for line in lines if "descender v-scale" in line) == 2

    def test_relief_prints_alongside_other_findings(self) -> None:
        """A line can be compressed, downsized and relieved at once."""
        resolved, rendered = _label(
            "l1",
            [_line("Depth", 0.5), _line("RANGE")],
            compression={0: 0.9},
            spacing={0: 0.2},
            downsizes={0: (0.06, 0.045)},
            descenders={0: (_fix(),)},
        )

        lines = format_layout_report([resolved], {"l1": rendered})
        text = "\n".join(lines)

        assert "scale 0.900 (10.0% compressed)" in text
        assert "cutter 0.060in -> 0.045in" in text
        assert "descender v-scale 0.600" in text

    def test_reciprocal_relief_is_a_compact_finding(self) -> None:
        """A following-line glyph sharing the relief prints its own finding.

        The reciprocal record has no raise (its lowest point is the anchor), so
        it prints as a top-down scale rather than a v-scale + raise.
        """
        reciprocal = DescenderFix(
            line_index=1,
            glyph_index=1,
            char="O",
            scale_y=0.848,
            raise_amount=0.0,
            gap_before=-0.077,
            gap_after=0.06,
            reciprocal=True,
        )
        resolved, rendered = _label(
            "l1",
            [_line("Depth", 0.05), _line("RANGE")],
            spacing={0: 0.05},
            descenders={0: (_fix(),), 1: (reciprocal,)},
        )

        lines = format_layout_report([resolved], {"l1": rendered})
        text = "\n".join(lines)

        assert "glyph 'O' shares relief, top-down scale 0.848 (gap -0.077in -> 0.060in)" in text
        # The descending glyph's own record keeps the historical format.
        assert "glyph 'p' descender v-scale 0.600, raised 0.071in" in text
        assert has_layout_findings([resolved], {"l1": rendered})


class TestMarkupUsesPlainText:
    """The report engraves the plain text: markup tags never appear."""

    def test_compression_finding_strips_markup(self) -> None:
        """A compressed markup line reports its plain text, not the tags."""
        # with_markup is a no-op when the text is unchanged, so the markup is
        # swapped onto a plain line (exactly what resolution does).
        line = _line("WIDE").with_markup("<i>WIDE</i>")
        resolved, rendered = _label("l1", [line, _line("B")], compression={0: 0.87})

        lines = format_layout_report([resolved], {"l1": rendered})
        text = "\n".join(lines)

        assert "'WIDE' scale 0.870 (13.0% compressed)" in text
        assert "<i>" not in text

    def test_spacing_and_downsize_findings_strip_markup(self) -> None:
        """Spacing / cutter findings on a markup line use the plain text too."""
        line = _line("WIDE", 0.5).with_markup("<u>WIDE</u>")
        resolved, rendered = _label(
            "l1",
            [line, _line("B")],
            spacing={0: 0.2},
            downsizes={0: (0.06, 0.045)},
        )

        lines = format_layout_report([resolved], {"l1": rendered})
        text = "\n".join(lines)

        assert "'WIDE' cutter 0.060in -> 0.045in" in text
        assert "'WIDE' spacing below 0.200in" in text
        assert "<u>" not in text


class TestBoldCutterFindings:
    """A ``<b>`` run's thicker cutter is a report finding."""

    @staticmethod
    def _bold_line(
        text: str, plain: float = 0.06, bold: float = 0.09, line_spacing: float = 0.0
    ) -> ResolvedTextLine:
        """A resolved line carrying a plain cutter and a bold rung above it."""
        return ResolvedTextLine(
            text=text,
            nominal_text_height=0.5,
            toolpath_text_height=0.5 - plain,
            cutter_diameter=plain,
            character_spacing=0.0,
            line_spacing=line_spacing,
            max_h_compress=0.0,
            text_h_alignment="center",
            bold_cutter_diameter=bold,
            bold_toolpath_text_height=0.5 - bold,
        )

    def test_bold_cutter_is_a_compact_finding(self) -> None:
        """A line with a bold run prints its bold cutter alongside the line's."""
        resolved, rendered = _label("l1", [self._bold_line("plain <b>BOLD</b>")])

        lines = format_layout_report([resolved], {"l1": rendered})
        text = "\n".join(lines)

        assert "bold cutter 0.090in (line cutter 0.060in)" in text
        assert has_layout_findings([resolved], {"l1": rendered}) is True

    def test_bold_finding_strips_markup(self) -> None:
        """The bold finding reports the plain text, never the tags."""
        line = self._bold_line("plain BOLD").with_markup("plain <b>BOLD</b>")
        resolved, rendered = _label("l1", [line])

        text = "\n".join(format_layout_report([resolved], {"l1": rendered}))

        assert "'plain BOLD' bold cutter 0.090in" in text
        assert "<b>" not in text

    def test_bold_prints_alongside_compression_and_downsize(self) -> None:
        """One line can report compression, a downsize, and its bold rung."""
        resolved, rendered = _label(
            "l1",
            [self._bold_line("WIDE")],
            compression={0: 0.85},
            downsizes={0: (0.075, 0.06)},
        )

        text = "\n".join(format_layout_report([resolved], {"l1": rendered}))

        assert "scale 0.850 (15.0% compressed)" in text
        assert "cutter 0.075in -> 0.060in (downsized for compression)" in text
        assert "bold cutter 0.090in (line cutter 0.060in)" in text

    def test_no_bold_cutter_is_not_a_finding(self) -> None:
        """A markup-free line produces no bold finding."""
        resolved, rendered = _label("l1", [_line("PLAIN")])

        assert format_layout_report([resolved], {"l1": rendered}) == []
        assert has_layout_findings([resolved], {"l1": rendered}) is False



class TestScaleMarkupFindings:
    """A ``<scale:N>`` run's factor range is a report finding."""

    def test_scale_is_a_compact_finding(self) -> None:
        """A line with a scaled run prints its factor range, run + line cutter."""
        line = _line("Hello Small").with_markup("Hello <scale:0.5>Small</scale>")
        resolved, rendered = _label("l1", [line])

        lines = format_layout_report([resolved], {"l1": rendered})
        text = "\n".join(lines)

        # 0.5 * 0.3in nominal = 0.15in of text -> the ladder's 0.015 rung.
        assert (
            "'Hello Small' scaled 0.500-1.000 (run cutter 0.015in, "
            "line cutter 0.030in)" in text
        )
        assert has_layout_findings([resolved], {"l1": rendered}) is True

    def test_uniform_range_prints_one_factor(self) -> None:
        """A fully scaled line prints its single factor."""
        line = _line("tiny").with_markup("<scale:0.5>tiny</scale>")
        resolved, rendered = _label("l1", [line])

        text = "\n".join(format_layout_report([resolved], {"l1": rendered}))

        assert "'tiny' scaled 0.500 (run cutter 0.015in, line cutter 0.030in)" in text

    def test_run_cutters_are_listed(self) -> None:
        """A line whose scaled runs resolved their own cutters lists them.

        The run cutters are the tools the markup caused (the effective-height
        ladder per run), de-duplicated and ascending -- the tool split the
        layout report must surface, not just the line's own cutter.
        """
        from dataclasses import replace as dc_replace

        line = _line("Hello Small").with_markup("Hello <scale:0.5>Small</scale>")
        # 0.5 * 0.3in nominal = 0.15in of text; the coarse inventory has no
        # tool that small, so the ladder snaps to its smallest rung (0.03).
        line = dc_replace(
            line,
            cutter_diameter=0.125,
            scale_cutter_inventory=(0.03, 0.125),
        )
        resolved, rendered = _label("l1", [line])

        text = "\n".join(format_layout_report([resolved], {"l1": rendered}))

        assert "'Hello Small' scaled 0.500-1.000 (run cutter 0.030in, line cutter 0.125in)" in text

    def test_scale_finding_strips_markup(self) -> None:
        """The scale finding reports the plain text, never the tags."""
        line = _line("tiny").with_markup("<scale:0.5>tiny</scale>")
        resolved, rendered = _label("l1", [line])

        text = "\n".join(format_layout_report([resolved], {"l1": rendered}))

        assert "<scale" not in text

    def test_unity_scale_is_not_a_finding(self) -> None:
        """``<scale:1>`` engraves plainly: no finding, no compact output."""
        line = _line("plain").with_markup("<scale:1>plain</scale>")
        resolved, rendered = _label("l1", [line])

        assert format_layout_report([resolved], {"l1": rendered}) == []
        assert has_layout_findings([resolved], {"l1": rendered}) is False


class TestSupSubMarkupFindings:
    """A ``<sup>``/``<sub>`` run's baseline shift is a report finding."""

    def test_bare_sup_is_a_compact_finding(self) -> None:
        """A bare <sup> prints its shift range plus the run + line cutter."""
        line = _line("1st").with_markup("1<sup>st</sup>")
        resolved, rendered = _label("l1", [line])

        lines = format_layout_report([resolved], {"l1": rendered})
        text = "\n".join(lines)

        # d = 0.5 -> shift 1 - d = 0.5 for the run; 0.5 * 0.3in nominal =
        # 0.15in of text -> the ladder's 0.015 rung.
        assert (
            "'1st' sup/sub shift 0.000-0.500 cap (run cutter 0.015in, "
            "line cutter 0.030in)" in text
        )
        assert has_layout_findings([resolved], {"l1": rendered}) is True

    def test_bare_sub_shift_is_negative(self) -> None:
        """A bare <sub> reports the negative (below-baseline) shift."""
        line = _line("x2").with_markup("x<sub>2</sub>")
        resolved, rendered = _label("l1", [line])

        text = "\n".join(format_layout_report([resolved], {"l1": rendered}))

        assert "sup/sub shift -0.500-0.000 cap" in text

    def test_uniform_shift_prints_one_value(self) -> None:
        """A fully shifted line prints its single shift."""
        line = _line("up").with_markup("<sup>up</sup>")
        resolved, rendered = _label("l1", [line])

        text = "\n".join(format_layout_report([resolved], {"l1": rendered}))

        assert (
            "'up' sup/sub shift 0.500 cap (run cutter 0.015in, line cutter 0.030in)" in text
        )

    def test_shift_only_run_keeps_the_line_cutter(self) -> None:
        """<sup:0.3> shifts without scaling: the run cutter is the line's."""
        line = _line("1st").with_markup("1<sup:0.3>st</sup>")
        resolved, rendered = _label("l1", [line])

        text = "\n".join(format_layout_report([resolved], {"l1": rendered}))

        assert (
            "'1st' sup/sub shift 0.000-0.300 cap (run cutter 0.030in, "
            "line cutter 0.030in)" in text
        )

    def test_sup_finding_strips_markup(self) -> None:
        """The finding reports the plain text, never the tags."""
        line = _line("up").with_markup("<sup>up</sup>")
        resolved, rendered = _label("l1", [line])

        text = "\n".join(format_layout_report([resolved], {"l1": rendered}))

        assert "<sup" not in text

    def test_unity_downscale_shifts_only(self) -> None:
        """supersubscript_downscale 1.0 leaves the run unscaled (line cutter)."""
        from dataclasses import replace as dc_replace

        line = dc_replace(
            _line("up").with_markup("<sup>up</sup>"), supersubscript_downscale=1.0
        )
        resolved, rendered = _label("l1", [line])

        text = "\n".join(format_layout_report([resolved], {"l1": rendered}))

        # d = 1.0 -> shift 1 - d = 0.0 (the glyph rides the capline's baseline)
        # and no downscale, so the run keeps the line's own tool.
        assert (
            "'up' sup/sub shift 0.000 cap (run cutter 0.030in, line cutter 0.030in)" in text
        )
