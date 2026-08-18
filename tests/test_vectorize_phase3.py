"""Tests for Phase 3 PLT assembly and export functions."""

import pytest

from plt_optimizer.generate.label_renderer import render_label_to_plt
from plt_optimizer.generate.layout import PackedLabel, PackedPlate
from plt_optimizer.generate.resolution import ResolvedLabel
from plt_optimizer.generate.schema import parse_yaml
from plt_optimizer.generate.vectorize import (
    assemble_plt_from_rendered_labels,
    translate_plt_coordinates,
)


class TestTranslatePltCoordinates:
    """Tests for coordinate translation in PLT files."""

    def test_translate_no_offset(self) -> None:
        """Test that zero offset returns original content."""
        plt_content = "IN;DF;PS0;SP1;PU1000,2000;PD1000,2000,3000,2000;SP0;IN;%"
        result = translate_plt_coordinates(plt_content, 0.0, 0.0)
        assert result == plt_content

    def test_translate_x_offset(self) -> None:
        """Test X-axis translation."""
        plt_content = "PU1000,2000;PD1000,2000,3000,2000"
        # Offset 1 inch = 1000 units
        result = translate_plt_coordinates(plt_content, 1.0, 0.0)

        # Check that coordinates were increased by 1000
        assert "2000,2000" in result  # 1000 + 1000 = 2000
        assert "4000,2000" in result  # 3000 + 1000 = 4000

    def test_translate_y_offset(self) -> None:
        """Test Y-axis translation."""
        plt_content = "PU1000,2000;PD1000,2000,3000,4000"
        # Offset 0.5 inch = 500 units
        result = translate_plt_coordinates(plt_content, 0.0, 0.5)

        # Check that y coordinates were increased by 500
        assert "1000,2500" in result  # 2000 + 500 = 2500
        assert "3000,4500" in result  # 4000 + 500 = 4500

    def test_translate_both_offsets(self) -> None:
        """Test X and Y translation together."""
        plt_content = "PA0,0;PD1000,1000"
        result = translate_plt_coordinates(plt_content, 0.5, 0.25)

        # 0.5 inch = 500 units, 0.25 inch = 250 units
        assert "500,250" in result  # (0+500, 0+250)
        assert "1500,1250" in result  # (1000+500, 1000+250)

    def test_translate_negative_offset(self) -> None:
        """Test negative translation (moving in negative direction)."""
        plt_content = "PA2000,2000"
        result = translate_plt_coordinates(plt_content, -1.0, -0.5)

        # -1.0 inch = -1000 units, -0.5 inch = -500 units
        assert "1000,1500" in result  # (2000-1000, 2000-500)


class TestAssemblePltFromRenderedLabels:
    """Tests for PLT assembly from rendered labels."""

    def test_assemble_single_label(self) -> None:
        """Test assembling a plate with a single label."""
        from plt_optimizer.generate.resolution import resolve_job_spec

        # Get test label
        job = parse_yaml("examples/test123_spec.yaml")
        labels = resolve_job_spec(job)
        label = labels[0]

        # Render it
        rendered = render_label_to_plt(label)
        rendered_map = {label.id: rendered}

        # Create a packed plate with this label at origin
        packed_label = PackedLabel(
            label_id=f"{label.id}_0",
            x=0.0,
            y=0.0,
            width=rendered.width,
            height=rendered.height,
            rotated=False,
            source_label=label,
        )

        plate = PackedPlate(
            plate_id="plate_1",
            width=24.0,
            height=16.0,
            labels=[packed_label],
        )

        # Assemble PLT
        result = assemble_plt_from_rendered_labels(plate, rendered_map)

        # Verify result is valid HPGL
        assert result.startswith("IN;DF;PS0;")
        assert result.endswith("%")
        assert "PU0,0;" in result  # Pen-up command
        # Should contain original coordinates (no offset)
        # Original rendered coordinates should still be present
        assert len(result) > 100  # Should have content

    def test_assemble_multiple_labels(self) -> None:
        """Test assembling a plate with multiple labels at different positions."""
        from plt_optimizer.generate.resolution import resolve_job_spec

        # Get test labels
        job = parse_yaml("examples/test123_spec.yaml")
        labels = resolve_job_spec(job)

        # Render all labels
        rendered_map = {
            labels[0].id: render_label_to_plt(labels[0]),
            labels[1].id: render_label_to_plt(labels[1]),
        }

        # Create packed labels at different positions
        packed_labels = [
            PackedLabel(
                label_id=f"{labels[0].id}_0",
                x=0.0,
                y=0.0,
                width=rendered_map[labels[0].id].width,
                height=rendered_map[labels[0].id].height,
                rotated=False,
                source_label=labels[0],
            ),
            PackedLabel(
                label_id=f"{labels[1].id}_0",
                x=3.5,  # Offset to the right
                y=1.5,  # Offset up
                width=rendered_map[labels[1].id].width,
                height=rendered_map[labels[1].id].height,
                rotated=False,
                source_label=labels[1],
            ),
        ]

        plate = PackedPlate(
            plate_id="plate_1",
            width=24.0,
            height=16.0,
            labels=packed_labels,
        )

        # Assemble PLT
        result = assemble_plt_from_rendered_labels(plate, rendered_map)

        # Verify result is valid HPGL with multiple labels
        assert result.startswith("IN;DF;PS0;")
        assert result.endswith("%")
        # Should have pen-up commands between labels
        assert result.count("PU0,0;") == 2
