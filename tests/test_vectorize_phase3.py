"""Tests for Phase 3 PLT assembly and export functions."""

import re
from pathlib import Path
from typing import Optional

import pytest

from plt_optimizer.generate.label_renderer import RenderedLabel, render_label_to_plt
from plt_optimizer.generate.layout import PackedLabel, PackedPlate
from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine
from plt_optimizer.generate.schema import parse_yaml
from plt_optimizer.generate.vectorize import (
    assemble_plt_from_rendered_labels,
    export_per_cutter_plts,
    extract_pens_from_plt_text,
    plt_has_geometry,
    rotate_plt_content_90cw,
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

    def test_translate_malformed_coordinates_left_untouched(self) -> None:
        """A command with unparseable coordinates is emitted verbatim."""
        # "PU1,,2" splits into an empty middle token; int("") raises
        # ValueError and the whole command must fall back unchanged, while
        # neighbouring well-formed commands still shift.
        result = translate_plt_coordinates("PU1,,2;PD5,5", 1.0, 0.0)
        assert "PU1,,2" in result
        assert "PD1005,5" in result

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


class TestRotatePltContent90cw:
    """Tests for the 90-degree clockwise rotation transform."""

    def test_rotate_points_swaps_and_normalizes(self) -> None:
        """Points map (x, y) -> (y_max - y, x - x_min) into the swapped box."""
        # Content spanning [1, 3] x [2, 5] inches (plotter units x1000).
        content = "PU1000,2000;PD3000,2000,3000,5000"
        result = rotate_plt_content_90cw(content)

        # y_max_units=5000, x_min_units=1000:
        # (1000,2000) -> (3000, 0); (3000,2000) -> (3000, 2000);
        # (3000,5000) -> (0, 2000)
        assert "PU3000,0" in result
        assert "PD3000,2000,0,2000" in result

    def test_rotate_arc_center_maps_angle_preserved(self) -> None:
        """AA centers rotate like points; the sweep angle is kept verbatim."""
        # Full circle: center (2000, 2000), radius 1000 (pen starts at the
        # rightmost point). Content bounds span [1000, 3000] x [1000, 3000],
        # so y_max_units=3000 and x_min_units=1000.
        content = "PU3000,2000;PD3000,2000;AA2000,2000,-90"
        result = rotate_plt_content_90cw(content)

        # Center (2000,2000) -> (3000-2000, 2000-1000) = (1000, 1000); the
        # east start point maps south: (3000,2000) -> (1000, 2000). The
        # sweep sign survives the pure rotation untouched.
        assert "AA1000,1000,-90" in result
        assert "PU1000,2000" in result

    def test_rotate_content_without_geometry_unchanged(self) -> None:
        """Coordinate-free content is returned verbatim."""
        content = "IN;DF;PS0;SP0;IN;%"
        assert rotate_plt_content_90cw(content) == content

    def test_rotate_malformed_command_left_untouched(self) -> None:
        """Unparseable coordinate lists fall back to the original command."""
        result = rotate_plt_content_90cw("PU1,,2;PD5,5")
        assert "PU1,,2" in result

    def test_rotate_odd_coordinate_count_untouched(self) -> None:
        """A PA with an odd number of values cannot be pair-rotated."""
        result = rotate_plt_content_90cw("PA1,2,3;PD4,4")
        assert "PA1,2,3" in result

    def test_rotate_truncated_arc_untouched(self) -> None:
        """An AA without its full center+angle parameters is left verbatim."""
        result = rotate_plt_content_90cw("PU1000,2000;AA1000,2000")
        assert "AA1000,2000" in result

    def test_rotated_assembly_lands_in_swapped_slot(self) -> None:
        """A rotated label's whole content lands inside [x, x+H] x [y, y+W]."""
        from plt_optimizer.core.models import ArcSegment, StrokeSegment
        from plt_optimizer.core.parser import PLTParser
        from plt_optimizer.generate.resolution import ResolvedHoleSpec

        label = ResolvedLabel(
            id="rot_label",
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
                label_id="rot_label_0",
                x=5.0,
                y=3.0,
                # The packer's slot for a rotated label swaps the dims.
                width=rendered.height,
                height=rendered.width,
                rotated=True,
                source_label=label,
            )
        )

        assembled = assemble_plt_from_rendered_labels(plate, {label.id: rendered})
        doc = PLTParser().parse_string(assembled)

        xs: list[float] = []
        ys: list[float] = []
        arcs = []
        for path in doc.stroke_paths:
            for seg in path.segments:
                if isinstance(seg, ArcSegment):
                    arcs.append(seg)
                    xs.append(seg.center.x / 1000)
                    ys.append(seg.center.y / 1000)
                elif isinstance(seg, StrokeSegment):
                    xs.extend((seg.start.x / 1000, seg.end.x / 1000))
                    ys.extend((seg.start.y / 1000, seg.end.y / 1000))

        assert arcs, "Rotated assembly lost the drill-hole arcs"
        # The rotated content occupies exactly the swapped slot:
        # x in [5, 5 + height], y in [3, 3 + width] (2x1 -> 1x2).
        assert min(xs) == pytest.approx(5.0, abs=0.01)
        assert max(xs) == pytest.approx(6.0, abs=0.01)
        assert min(ys) == pytest.approx(3.0, abs=0.01)
        assert max(ys) == pytest.approx(5.0, abs=0.01)

        # Clockwise rotation maps the device-convention hole center
        # (0.25, 0.75) to (0.25, 0.25); the slot offset adds (5, 3).
        centers = {(round(a.center.x / 1000, 2), round(a.center.y / 1000, 2)) for a in arcs}
        assert centers == {(5.25, 3.25)}
        # The rigid transform preserves the drill radius.
        assert all(a.radius / 1000 == pytest.approx(0.0625, abs=0.001) for a in arcs)


class TestAssemblePltFromRenderedLabels:
    """Tests for PLT assembly from rendered labels."""

    def test_assemble_single_label(self) -> None:
        """Test assembling a plate with a single label."""
        from plt_optimizer.generate.resolution import resolve_job_spec

        # Get test label
        job = parse_yaml("tests_deps/test123_spec.yaml")
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
        job = parse_yaml("tests_deps/test123_spec.yaml")
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

    def test_assemble_skips_label_without_geometry(self) -> None:
        """A label whose content is header/footer only contributes nothing.

        Stripping the header, footer and leading PU commands leaves an
        empty string: no trailing semicolon to trim (line 135 false side)
        and no content to append (line 146 false side). The plate must
        degrade to a bare header+footer, without any PU0,0 separator.
        """
        label = ResolvedLabel(id="blank", count=1, width=1.0, height=1.0, margin=0.1)
        rendered = RenderedLabel(
            source_label=label,
            plt_content="IN;DF;PS0;PU100,200;SP0;IN;%",
            x_min=0.0,
            y_min=0.0,
            x_max=1.0,
            y_max=1.0,
            width=1.0,
            height=1.0,
        )

        plate = PackedPlate(plate_id="p1", width=24.0, height=16.0)
        plate.labels.append(
            PackedLabel(
                label_id="blank_0",
                x=0.5,
                y=0.5,
                width=1.0,
                height=1.0,
                rotated=False,
                source_label=label,
            )
        )

        assembled = assemble_plt_from_rendered_labels(plate, {label.id: rendered})
        assert assembled == "IN;DF;PS0;SP0;IN;%"
        assert "PU0,0;" not in assembled


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

    def test_empty_commands_are_skipped(self) -> None:
        """Empty command chunks (double semicolons) are ignored."""
        result = extract_pens_from_plt_text("IN;DF;PS0;SP1;;PU1,2;SP0;IN;%", [1])
        assert "PU1,2" in result
        assert ";;" not in result

    def test_malformed_pen_select_is_ignored(self) -> None:
        """An unparseable SP command leaves the current layer state alone."""
        result = extract_pens_from_plt_text("IN;DF;PS0;SP1;SPX;PD3,4;SP0;IN;%", [1])
        # SPX neither parses nor flips the layer: pen 1 geometry survives.
        assert "PD3,4" in result
        assert "SPX" not in result


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


class TestExportStructuralLayerSkip:
    """export_per_cutter_plts skips the _bh_ file when no SP2/SP3 exists."""

    def test_export_skips_structural_file_without_geometry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plate whose assembly carries no structural pens writes no _bh_.

        The renderer always emits a boundary rectangle, so the only way a
        plate's combined content can lack SP2/SP3 geometry is an unusual
        assembly; a stubbed assembler reproduces that shape deterministically
        so the empty-structural branch (no borders-holes file) is exercised.
        """
        import plt_optimizer.generate.vectorize as vectorize
        from plt_optimizer.generate.resolution import resolve_job_spec

        job = parse_yaml("tests_deps/test123_spec.yaml")
        resolved_labels = resolve_job_spec(job)

        def fake_assemble(
            plate: PackedPlate,
            rendered_labels_map: dict[str, RenderedLabel],
        ) -> str:
            """Return a plate containing only pen-1 text geometry."""
            return "IN;DF;PS0;SP1;PU100,100;PD200,200;SP0;IN;%"

        monkeypatch.setattr(vectorize, "assemble_plt_from_rendered_labels", fake_assemble)

        result = export_per_cutter_plts(
            resolved_labels,
            output_dir=tmp_path,
            job_id="nostruct",
            optimize=False,
            plots=False,
        )

        # The pen-1 text layer is written; the empty structural layer is not.
        assert result.plt_paths
        assert not any("_bh_" in p.name for p in result.plt_paths)
        assert all("_text_" in p.name for p in result.plt_paths)
        # The in-memory combined content still mirrors the (text-only) plate.
        assert all("SP1;" in content for content in result.combined_by_plate.values())


def _cutting_segments(content: str) -> list[tuple[str, object]]:
    """Extract a multiset of cutting geometry from HPGL content.

    Pen-down line segments are normalized to unordered endpoint pairs (so
    reversals compare equal) and arcs to ``(center, sweep)`` tuples. Used to
    assert the plate-space optimizer preserves geometry exactly.

    Args:
        content: Raw HPGL text.

    Returns:
        Sorted list of ``("L", (p, q))`` / ``("A", center, sweep)`` entries.
    """
    segments: list[tuple[str, object]] = []
    current: Optional[tuple[int, int]] = None
    for token in content.split(";"):
        token = token.strip()
        if token.startswith("SP"):
            current = None
            continue
        match = re.match(r"^(PU|PD)(-?[\d,\-]+)$", token)
        if match:
            values = [int(v) for v in match.group(2).split(",")]
            for x, y in zip(values[0::2], values[1::2]):
                if match.group(1) == "PD" and current is not None:
                    segments.append(("L", tuple(sorted([current, (x, y)]))))
                current = (x, y)
            continue
        arc = re.match(r"^AA(-?\d+),(-?\d+),(-?\d+)$", token)
        if arc:
            segments.append(("A", (arc.group(1), arc.group(2)), arc.group(3)))
    return sorted(segments, key=repr)


class TestPlateSpaceExport:
    """Plate-space optimization preserves geometry and pen structure."""

    def _spec(self, tmp_path: Path) -> Path:
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text(
            "job:\n"
            "  job_name: Plate Space Job\n"
            "  plates:\n"
            "    - id: p1\n"
            "      width: 24.0\n"
            "      height: 12.0\n"
            "      left_clearance: 0.25\n"
            "      top_clearance: 0.25\n"
            "      clearance_padding: 0.125\n"
            "  labels:\n"
            "    - id: l1\n"
            "      count: 3\n"
            "      width: 3.0\n"
            "      height: 1.5\n"
            "      margin: 0.1\n"
            "      holes:\n"
            "        - location: top-left\n"
            "        - location: bottom-right\n"
            "      content:\n"
            "        - text: Hello World\n"
            "          height: 0.5\n"
            "        - text: Second line here\n"
            "          height: 0.35\n"
        )
        return spec_file

    def _export_pair(
        self, tmp_path: Path, **job_overrides: object
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Export the same job unoptimized and plate-space optimized."""
        from plt_optimizer.generate.resolution import resolve_job_spec

        job = parse_yaml(self._spec(tmp_path))
        for key, value in job_overrides.items():
            setattr(job, key, value)
        labels = resolve_job_spec(job)
        raw = export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path / "raw",
            job_id="j",
            optimize=False,
            plots=False,
        )
        opt = export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path / "opt",
            job_id="j",
            optimize=True,
            plots=False,
            fast_mode=True,
        )
        return (
            {p.name: p.read_text(encoding="utf-8") for p in raw.plt_paths},
            {p.name: p.read_text(encoding="utf-8") for p in opt.plt_paths},
        )

    def test_same_files_written(self, tmp_path: Path) -> None:
        """Optimization writes exactly the same per-cutter file set."""
        raw, opt = self._export_pair(tmp_path)
        assert set(raw) == set(opt)
        assert any("_text_" in name for name in opt)
        assert any("_bh_" in name for name in opt)

    def test_text_geometry_is_vertex_exact(self, tmp_path: Path) -> None:
        """Text layers keep every cutting stroke at its exact coordinates."""
        raw, opt = self._export_pair(tmp_path)
        checked = 0
        for name, raw_content in raw.items():
            if "_text_" not in name:
                continue
            assert _cutting_segments(opt[name]) == _cutting_segments(raw_content), name
            checked += 1
        assert checked > 0

    def test_structural_geometry_is_subset_after_dedupe(self, tmp_path: Path) -> None:
        """Borders/holes only lose coincident strokes (dedupe), never new ones."""
        raw, opt = self._export_pair(tmp_path)
        checked = 0
        for name, raw_content in raw.items():
            if "_bh_" not in name:
                continue
            raw_segments = _cutting_segments(raw_content)
            opt_segments = _cutting_segments(opt[name])
            extra = [s for s in opt_segments if s not in raw_segments]
            assert not extra, f"{name}: optimizer invented {len(extra)} stroke(s)"
            checked += 1
        assert checked > 0

    def test_pen_selection_survives_optimization(self, tmp_path: Path) -> None:
        """Optimized files keep SP selects around geometry (never hoisted)."""
        raw, opt = self._export_pair(tmp_path)
        for name, content in opt.items():
            assert content.startswith("IN;DF;PS0;")
            assert content.endswith("%")
            # Geometry must follow a pen select, not precede every SP token.
            first_sp = content.find("SP")
            first_pd = content.find("PD")
            assert 0 <= first_sp < first_pd, name
            pens = set(re.findall(r"SP(\d+);", content.replace("SP0;IN;%", "")))
            if "_bh_" in name:
                assert pens and pens <= {"2", "3"}
            else:
                assert pens and "2" not in pens and "3" not in pens

    def test_word_mode_matches_line_mode_geometry(self, tmp_path: Path) -> None:
        """Chunk granularity changes routing nodes, never emitted geometry."""
        from plt_optimizer.generate.resolution import resolve_job_spec

        spec_text = self._spec(tmp_path).read_text(encoding="utf-8")
        contents = {}
        for mode in ("line", "word"):
            spec_file = tmp_path / f"spec_{mode}.yaml"
            spec_file.write_text(
                spec_text.replace(
                    "job_name: Plate Space Job",
                    f"job_name: Plate Space Job\n  text_chunk_mode: {mode}",
                ),
                encoding="utf-8",
            )
            job = parse_yaml(spec_file)
            mode_labels = resolve_job_spec(job)
            result = export_per_cutter_plts(
                mode_labels,
                job.plates,
                output_dir=tmp_path / mode,
                job_id="j",
                optimize=True,
                plots=False,
                fast_mode=True,
            )
            contents[mode] = {p.name: p.read_text(encoding="utf-8") for p in result.plt_paths}
        assert set(contents["line"]) == set(contents["word"])
        for name, line_content in contents["line"].items():
            assert _cutting_segments(contents["word"][name]) == _cutting_segments(line_content), (
                name
            )

    def test_optimize_false_is_unchanged_by_fast_mode(self, tmp_path: Path) -> None:
        """``optimize=False`` ignores fast_mode and emits raw pen layers."""
        from plt_optimizer.generate.resolution import resolve_job_spec

        job = parse_yaml(self._spec(tmp_path))
        labels = resolve_job_spec(job)
        result = export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path / "raw",
            job_id="j",
            optimize=False,
            plots=False,
            fast_mode=True,
        )
        assert result.plt_paths
        for path in result.plt_paths:
            content = path.read_text(encoding="utf-8")
            assert content.startswith("IN;DF;PS0;")
            assert content.endswith("%")
