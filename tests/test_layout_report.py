"""Unit tests for the shared LAYOUT REPORT formatter.

``plt_optimizer.generate.layout_report`` is pure Python: the tests build
``ResolvedLabel`` / ``RenderedLabel`` fixtures directly (no rendering), so
the formatting contract for the ``generate`` CLI and the integration
runner's Phase 3.6 is pinned without matplotlib.
"""

from __future__ import annotations

from plt_optimizer.generate.label_renderer import RenderedLabel
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
    )
    return resolved, rendered


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
            "    Line 0: 'A' spacing below 0.200in (requested 0.500in)",
        ]
        assert has_layout_findings([resolved], {"l1": rendered})

    def test_compression_is_a_finding(self) -> None:
        """A compressed line keeps the historical scale-line format."""
        resolved, rendered = _label("l1", [_line("WIDE"), _line("B")], compression={0: 0.87})

        lines = format_layout_report([resolved], {"l1": rendered})

        assert lines == [
            "Label l1:",
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
            "    Line 0: 'A' spacing below 0.100in",
            "    Line 1: 'B' spacing below 0.200in (requested 0.500in)",
        ]

    def test_untouched_label_gets_summary_line(self) -> None:
        """A single untouched line prints the natural-width/spacing summary."""
        resolved, rendered = _label("l1", [_line("ONLY", 0.5)])

        lines = format_layout_report([resolved], {"l1": rendered}, full=True)

        assert lines[:2] == ["Label l1:", "    all lines at natural width / spacing"]

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
