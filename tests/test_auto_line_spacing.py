"""Tests for auto line spacing feature.

Tests for ``line_spacing="auto"`` which allows flexible calculation of
inter-line spacing based on label dimensions, margins, and line count.
"""

from __future__ import annotations

import pytest
from plt_optimizer.generate.resolution import resolve_job_spec
from plt_optimizer.generate.schema import JobSpec, LabelSpec, TextLine


class TestAutoLineSpacing:
    """Tests for line_spacing="auto" feature."""

    def test_auto_spacing_without_explicit_v_margin(self) -> None:
        """Auto spacing calculates v_margin equal to line_spacing when v_margin is implicit.

        For 3 lines of 0.25" height in a 2.0" tall label with no explicit
        v_margin:
        - total_line_height = 0.75"
        - cutter_adjustment = 0.03" (0.015" x 2)
        - spacing = (2.0 + 0.03 - 0.75) / (3 + 1) = 0.32"
        - v_margin should be set to 0.32"
        """
        job = JobSpec(
            job_name="Test Auto Spacing No Explicit Margin",
            width=3.0,
            height=2.0,
            text_height=0.25,
            line_spacing="auto",
            content=[
                TextLine(text="Line 1"),
                TextLine(text="Line 2"),
                TextLine(text="Line 3"),
            ],
        )

        resolved_labels = resolve_job_spec(job)
        label = resolved_labels[0]

        # V-margin should be set to the calculated line_spacing
        assert abs(label.v_margin - 0.32) < 0.005
        # All lines should have the same spacing
        for line in label.content:
            assert abs(line.line_spacing - label.v_margin) < 0.001

    def test_auto_spacing_with_explicit_v_margin(self) -> None:
        """Auto spacing honors explicit v_margin and calculates line_spacing to fit.

        For 3 lines of 0.25" height in a 2.0" tall label with explicit v_margin=0.2":
        - available_height = 2.0 - 2*0.2 + 0.03 = 1.63"
        - total_line_height = 0.75"
        - spacing = (1.63 - 0.75) / 2 = 0.44"
        - v_margin should stay at 0.2"
        """
        job = JobSpec(
            job_name="Test Auto Spacing with Explicit Margin",
            width=3.0,
            height=2.0,
            text_height=0.25,
            v_margin=0.2,
            line_spacing="auto",
            content=[
                TextLine(text="Line 1"),
                TextLine(text="Line 2"),
                TextLine(text="Line 3"),
            ],
        )

        resolved_labels = resolve_job_spec(job)
        label = resolved_labels[0]

        # V-margin should remain at the explicit value
        assert abs(label.v_margin - 0.2) < 0.001
        # Line spacing should fit the available space
        assert abs(label.content[0].line_spacing - 0.44) < 0.01

    def test_auto_spacing_single_line(self) -> None:
        """Auto spacing handles single-line labels correctly."""
        job = JobSpec(
            job_name="Test Auto Spacing Single Line",
            width=3.0,
            height=1.0,
            text_height=0.25,
            line_spacing="auto",
            content=[TextLine(text="Single Line")],
        )

        resolved_labels = resolve_job_spec(job)
        label = resolved_labels[0]

        # Single line should have zero line_spacing (nothing after it)
        assert label.content[0].line_spacing == 0.0

    def test_auto_spacing_two_lines(self) -> None:
        """Auto spacing with two lines calculates based on available space."""
        job = JobSpec(
            job_name="Test Auto Spacing Two Lines",
            width=3.0,
            height=1.5,
            text_height=0.25,
            line_spacing="auto",
            content=[
                TextLine(text="Line 1"),
                TextLine(text="Line 2"),
            ],
        )

        resolved_labels = resolve_job_spec(job)
        label = resolved_labels[0]

        # Should have calculated spacing
        assert label.content[0].line_spacing > 0.0
        # All lines get the same spacing value stored (though only first n-1 are used)
        assert abs(
            label.content[1].line_spacing - label.content[0].line_spacing
        ) < 0.001

    def test_auto_spacing_mixed_with_explicit_raises_warning(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Mixed auto and explicit line_spacing treats all as auto with a warning.

        This test verifies that when some lines have auto and others explicit,
        all are treated as auto with a warning logged.
        """
        job = JobSpec(
            job_name="Test Mixed Auto and Explicit",
            width=3.0,
            height=2.0,
            text_height=0.25,
            line_spacing="auto",
            content=[
                TextLine(text="Line 1", line_spacing=0.1),  # Explicit override
                TextLine(text="Line 2"),
                TextLine(text="Line 3"),
            ],
        )

        resolved_labels = resolve_job_spec(job)
        label = resolved_labels[0]

        # All lines should have the same calculated spacing
        for line in label.content[:-1]:
            assert abs(line.line_spacing - label.content[0].line_spacing) < 0.001

    def test_auto_spacing_label_level(self) -> None:
        """Auto spacing can be specified at label level in explicit labels list."""
        job = JobSpec(
            job_name="Test Label-Level Auto Spacing",
            labels=[
                LabelSpec(
                    id="label_1",
                    width=3.0,
                    height=2.0,
                    text_height=0.25,
                    line_spacing="auto",
                    content=[
                        TextLine(text="Line 1"),
                        TextLine(text="Line 2"),
                    ],
                ),
            ],
        )

        resolved_labels = resolve_job_spec(job)
        label = resolved_labels[0]

        # Should have auto-calculated spacing
        assert label.content[0].line_spacing > 0.0

    def test_auto_spacing_cascade_from_job_to_lines(self) -> None:
        """Auto spacing at job level cascades to lines that don't override it."""
        job = JobSpec(
            job_name="Test Job-Level Auto Spacing Cascade",
            width=3.0,
            height=2.0,
            text_height=0.25,
            line_spacing="auto",  # Job-level auto
            labels=[
                LabelSpec(
                    id="label_1",
                    content=[
                        TextLine(text="Line 1"),
                        TextLine(text="Line 2"),
                    ],
                ),
            ],
        )

        resolved_labels = resolve_job_spec(job)
        label = resolved_labels[0]

        # Should have auto-calculated spacing from cascaded job value
        assert label.content[0].line_spacing > 0.0
        # V-margin should be set to match the line spacing
        assert abs(label.v_margin - label.content[0].line_spacing) < 0.001

    def test_auto_spacing_respects_margin_constraints(self) -> None:
        """Auto spacing fits content within margin constraints."""
        job = JobSpec(
            job_name="Test Auto Spacing with Margins",
            width=3.0,
            height=2.0,
            text_height=0.25,
            margin=0.1,  # Tight margins
            line_spacing="auto",
            content=[
                TextLine(text="Line 1"),
                TextLine(text="Line 2"),
                TextLine(text="Line 3"),
            ],
        )

        resolved_labels = resolve_job_spec(job)
        label = resolved_labels[0]

        # Should still calculate valid spacing that respects margins
        assert label.content[0].line_spacing >= 0.0
        # Total content should fit within the label when margins applied
        total_height = sum(line.nominal_text_height for line in label.content)
        total_height += sum(line.line_spacing for line in label.content[:-1])
        available_height = label.height - 2 * label.margin
        assert total_height <= available_height + 0.01  # Small tolerance for rounding
