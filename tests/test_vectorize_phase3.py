"""Tests for Phase 3 PLT assembly and export functions."""

import re

import pytest

from plt_optimizer.generate.label_renderer import render_label_to_plt
from plt_optimizer.generate.layout import PackedLabel, PackedPlate
from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine
from plt_optimizer.generate.schema import parse_yaml
from plt_optimizer.generate.vectorize import (
    assemble_plt_from_rendered_labels,
    extract_pens_from_plt_text,
    plt_has_geometry,
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

    def test_translate_arc_center_but_not_sweep(self) -> None:
        """AA centers translate; the trailing sweep angle must not."""
        plt_content = "PU1000,0;PD1000,0;AA1000,2000,90"
        result = translate_plt_coordinates(plt_content, 1.0, 0.5)

        # Center shifts by (1000, 500); the 90-degree sweep is untouched.
        assert "AA2000,2500,90" in result
        assert "PU2000,500" in result

    def test_assembled_arcs_land_at_packed_positions(self) -> None:
        """Holes must follow their label when assembled onto a plate."""
        from plt_optimizer.core.models import ArcSegment
        from plt_optimizer.core.parser import PLTParser
        from plt_optimizer.generate.resolution import ResolvedHoleSpec

        label = ResolvedLabel(
            id="arc_label",
            count=1,
            width=2.0,
            height=1.0,
            margin=0.1,
            hole_margin=0.1875,
            holes=[ResolvedHoleSpec(diameter=0.125, location="bottom-left")],
            content=[],
        )
        rendered = render_label_to_plt(label)

        plate = PackedPlate(plate_id="p1", width=24.0, height=16.0)
        plate.labels.append(
            PackedLabel(
                label_id="arc_label_0",
                x=5.0,
                y=3.0,
                width=rendered.width,
                height=rendered.height,
                rotated=False,
                source_label=label,
            )
        )

        assembled = assemble_plt_from_rendered_labels(plate, {label.id: rendered})
        doc = PLTParser().parse_string(assembled)

        arcs = [
            seg for path in doc.stroke_paths for seg in path.segments if isinstance(seg, ArcSegment)
        ]
        assert arcs, "Assembled plate lost the drill-hole arcs"
        # Local center x = hole_margin + radius = 0.25; the device-convention
        # Y flip mirrors it to 1.0 - 0.25 = 0.75. Adding the packed offset
        # (5.0, 3.0) gives the expected plate position.
        centers = {(round(a.center.x / 1000, 2), round(a.center.y / 1000, 2)) for a in arcs}
        assert len(centers) == 1
        center_x, center_y = centers.pop()
        assert center_x == pytest.approx(5.25, abs=0.01)
        assert center_y == pytest.approx(3.75, abs=0.01)


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


class TestExtractPensFromPltText:
    """Multi-pen extraction keeps AA arcs and filters by SP sections."""

    SAMPLE = (
        "IN;DF;PS0;"
        "SP1;PU100,100;PD200,200;"
        "SP2;PU0,0;PD3000,0,3000,1000,0,1000,0,0;"
        "SP3;PU500,500;PD500,500;AA400,500,90;AA400,500,90;AA400,500,90;AA400,500,90;"
        "SP4;PU900,900;PD950,950;"
        "SP0;IN;%"
    )

    def test_single_pen_extraction(self) -> None:
        """A single-pen request returns only that pen's geometry."""
        result = extract_pens_from_plt_text(self.SAMPLE, [1])
        assert "PU100,100" in result
        assert "PD200,200" in result
        assert "SP1;" in result
        assert "PD3000,0" not in result
        assert "AA400,500" not in result
        assert result.startswith("IN;DF;PS0;")
        assert result.endswith("%")

    def test_multi_pen_extraction_keeps_arcs(self) -> None:
        """Borders+holes extraction preserves native AA arc commands."""
        result = extract_pens_from_plt_text(self.SAMPLE, [2, 3])
        assert "PD3000,0" in result  # borders
        assert "AA400,500,90" in result  # holes survive extraction
        assert "SP2;" in result and "SP3;" in result
        assert "PU100,100" not in result  # text pen excluded
        assert "PU900,900" not in result  # other text pen excluded

    def test_missing_pen_yields_empty_geometry(self) -> None:
        """Requesting absent pens yields a header/footer-only result."""
        result = extract_pens_from_plt_text(self.SAMPLE, [7])
        assert not plt_has_geometry(result)

    def test_plt_has_geometry(self) -> None:
        """Geometry detection recognizes PU/PD/PA/AA and rejects headers."""
        assert plt_has_geometry("IN;DF;PS0;SP1;PU1,2;SP0;IN;%")
        assert plt_has_geometry("IN;DF;PS0;SP3;AA1,2,90;SP0;IN;%")
        assert plt_has_geometry("IN;DF;PS0;SP1;PA1,2;SP0;IN;%")
        assert plt_has_geometry("IN;DF;PS0;SP1;PD1,2;SP0;IN;%")
        assert not plt_has_geometry("IN;DF;PS0;SP0;IN;%")
        assert not plt_has_geometry("")


class TestRenderLabelToPltPenMap:
    """render_label_to_plt pen_map puts text lines on per-cutter pens."""

    @staticmethod
    def _two_cutter_label() -> ResolvedLabel:
        return ResolvedLabel(
            id="two_cutter",
            count=1,
            width=4.0,
            height=2.0,
            margin=0.1,
            content=[
                ResolvedTextLine(
                    text="BIG",
                    nominal_text_height=0.6,
                    toolpath_text_height=0.54,
                    cutter_diameter=0.06,
                    character_spacing=0.0,
                    line_spacing=0.0,
                ),
                ResolvedTextLine(
                    text="small",
                    nominal_text_height=0.25,
                    toolpath_text_height=0.22,
                    cutter_diameter=0.03,
                    character_spacing=0.0,
                    line_spacing=0.0,
                ),
            ],
        )

    def test_default_renders_all_text_on_pen_one(self) -> None:
        """Without a pen_map all text lands on SP1 (back compatible)."""
        rendered = render_label_to_plt(self._two_cutter_label())
        content = rendered.plt_content
        assert "SP1;" in content
        assert "SP4;" not in content
        assert "SP5;" not in content

    def test_pen_map_splits_text_across_pens(self) -> None:
        """With a pen_map each cutter's lines land on its own pen."""
        pen_map = {0.03: 1, 0.06: 4}
        rendered = render_label_to_plt(self._two_cutter_label(), pen_map=pen_map)
        content = rendered.plt_content

        # Both text pens present, plus the boundary pen.
        assert "SP1;" in content
        assert "SP4;" in content
        assert "SP2;" in content

        # Each pen section holds geometry.
        for pen in (1, 4):
            match = re.search(rf"SP{pen};(.*?)(?:SP\d;|$)", content, re.DOTALL)
            assert match is not None, f"Missing SP{pen} section"
            assert re.search(r"(?:PU|PD)\d", match.group(1)), f"Empty SP{pen} section"

    def test_pen_map_bounds_match_default(self) -> None:
        """Pen assignment must not change the rendered footprint."""
        label = self._two_cutter_label()
        default = render_label_to_plt(label)
        mapped = render_label_to_plt(label, pen_map={0.03: 1, 0.06: 4})
        assert mapped.width == pytest.approx(default.width, abs=1e-6)
        assert mapped.height == pytest.approx(default.height, abs=1e-6)

    def test_multi_pen_text_vertically_centered_together(self) -> None:
        """All text pens share one vertical centering delta.

        The union of both text pens must be centered within the label's
        margin box; if each pen were centered independently, the Y ranges
        would differ from the union-centered result.
        """
        label = self._two_cutter_label()
        rendered = render_label_to_plt(label, pen_map={0.03: 1, 0.06: 4})
        content = rendered.plt_content

        ys: list[int] = []
        for pen in (1, 4):
            match = re.search(rf"SP{pen};(.*?)(?:SP\d;|$)", content, re.DOTALL)
            assert match is not None
            for coords in re.findall(r"(?:PA|PU|PD)([\d,\-]+)", match.group(1)):
                parts = coords.split(",")
                ys.extend(int(parts[i]) for i in range(1, len(parts), 2))

        # Device convention: y measured from the top. Union center should
        # sit near the label mid-height (2000 units), within glyph-shape
        # tolerance (descenders shift the ink box slightly).
        center = (min(ys) + max(ys)) / 2.0
        assert center == pytest.approx(1000.0, abs=120.0)
