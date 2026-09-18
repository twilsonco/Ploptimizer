"""Tests for the vectorization engine that renders PackedPlate to HPGL/PLT."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
import vpype as vp

from plt_optimizer.generate.layout import PackedLabel, PackedPlate
from plt_optimizer.generate.resolution import (
    ResolvedHoleSpec,
    ResolvedLabel,
    ResolvedTextLine,
)
from plt_optimizer.generate.vectorize import (
    LAYER_BOUNDARY,
    LAYER_HOLES,
    LAYER_TEXT,
    POINTS_PER_INCH,
    TEXT_BLOCK_HEIGHT_PER_SIZE,
    _apply_transform,
    _get_transform_matrix,
    _hole_center,
    _render_boundary,
    _render_holes,
    _render_text,
    export_and_optimize,
    export_to_plt,
    vectorize_plate,
    vectorize_plates,
)


def _make_label(
    label_id: str = "lbl",
    width: float = 2.0,
    height: float = 1.0,
    count: int = 1,
    margin: float = 0.0,
    holes: list[ResolvedHoleSpec] | None = None,
    content: list[ResolvedTextLine] | None = None,
) -> ResolvedLabel:
    """Helper to create a ResolvedLabel with minimal boilerplate."""
    if content is None:
        content = [ResolvedTextLine(
            text="X",
            nominal_text_height=0.5,
            toolpath_text_height=0.5 - 0.03,
            cutter_diameter=0.03,
            character_spacing=0.0,
            line_spacing=0.0,
        )]
    return ResolvedLabel(
        id=label_id,
        count=count,
        width=width,
        height=height,
        margin=margin,
        holes=holes or [],
        content=content,
    )


def _make_packed(
    label: ResolvedLabel,
    x: float = 0.0,
    y: float = 0.0,
    width: float | None = None,
    height: float | None = None,
    rotated: bool = False,
) -> PackedLabel:
    """Helper to create a PackedLabel."""
    return PackedLabel(
        label_id=f"{label.id}_0",
        x=x,
        y=y,
        width=width if width is not None else label.width,
        height=height if height is not None else label.height,
        rotated=rotated,
        source_label=label,
    )


def _layer_has_content(doc: vp.Document, layer_id: int) -> bool:
    """Check if a document layer has geometry."""
    return doc.exists(layer_id) and not doc.layers[layer_id].is_empty()


class TestLayerConstants:
    """Tests for layer ID constants."""

    def test_layer_text(self) -> None:
        """LAYER_TEXT should be 1."""
        assert LAYER_TEXT == 1

    def test_layer_boundary(self) -> None:
        """LAYER_BOUNDARY should be 2."""
        assert LAYER_BOUNDARY == 2

    def test_layer_holes(self) -> None:
        """LAYER_HOLES should be 3."""
        assert LAYER_HOLES == 3

    def test_layers_are_distinct(self) -> None:
        """All layer IDs should be distinct."""
        assert len({LAYER_TEXT, LAYER_BOUNDARY, LAYER_HOLES}) == 3

    def test_points_per_inch(self) -> None:
        """POINTS_PER_INCH should be 72.0."""
        assert POINTS_PER_INCH == 72.0

    def test_text_block_height_per_size(self) -> None:
        """TEXT_BLOCK_HEIGHT_PER_SIZE should be 0.65625.

        This is the empirically measured ratio between the ``size`` parameter
        passed to ``vpype.text_block()`` and the resulting rendered glyph
        height in document coordinates (for the default ``futural`` font).
        """
        assert math.isclose(TEXT_BLOCK_HEIGHT_PER_SIZE, 0.65625, rel_tol=1e-9)

    def test_text_block_height_per_size_matches_vpype(self) -> None:
        """TEXT_BLOCK_HEIGHT_PER_SIZE should match vpype's actual behavior.

        This test verifies the constant by rendering text at a known size
        and checking the resulting height matches the expected ratio.
        """
        test_size = 10.0
        lc = vp.text_block("H", width=1000, size=test_size)
        bounds = lc.bounds()
        assert bounds is not None
        rendered_height = bounds[3] - bounds[1]
        expected_ratio = rendered_height / test_size
        assert math.isclose(TEXT_BLOCK_HEIGHT_PER_SIZE, expected_ratio, rel_tol=1e-3)


class TestGetTransformMatrix:
    """Tests for the coordinate transformation helper."""

    def test_no_rotation(self) -> None:
        """Non-rotated label should have zero angle."""
        label = _make_label()
        packed = _make_packed(label, x=1.0, y=2.0)
        dx, dy, angle = _get_transform_matrix(packed)
        assert dx == 1.0
        assert dy == 2.0
        assert angle == 0.0

    def test_with_rotation(self) -> None:
        """Rotated label should have 90-degree angle."""
        label = _make_label()
        packed = _make_packed(label, x=1.0, y=2.0, rotated=True)
        dx, dy, angle = _get_transform_matrix(packed)
        assert dx == 1.0
        assert dy == 2.0
        assert math.isclose(angle, math.pi / 2)


class TestApplyTransform:
    """Tests for the transform application helper."""

    def test_translation_only(self) -> None:
        """Translation should shift the geometry."""
        lc = vp.LineCollection([vp.rect(0, 0, 2.0, 1.0)])
        result = _apply_transform(lc, 5.0, 3.0, 0.0)
        bounds = result.bounds()
        assert bounds is not None
        assert math.isclose(bounds[0], 5.0)
        assert math.isclose(bounds[1], 3.0)

    def test_rotation_only(self) -> None:
        """90-degree rotation should swap width and height."""
        lc = vp.LineCollection([vp.rect(0, 0, 2.0, 1.0)])
        result = _apply_transform(lc, 0.0, 0.0, math.pi / 2)
        bounds = result.bounds()
        assert bounds is not None
        # After 90-degree rotation, 2x1 rect becomes 1x2
        assert math.isclose(bounds[2] - bounds[0], 1.0, abs_tol=0.01)
        assert math.isclose(bounds[3] - bounds[1], 2.0, abs_tol=0.01)


class TestHoleCenter:
    """Tests for hole center calculation."""

    def test_left_hole(self) -> None:
        """Left hole should be at the left edge, vertically centered."""
        cx, cy = _hole_center(
            ResolvedHoleSpec(diameter=0.125, location="left"),
            label_width=2.0,
            label_height=1.0,
            margin=0.1,
        )
        assert math.isclose(cx, 0.1)
        assert math.isclose(cy, 0.1 + 0.5)

    def test_right_hole(self) -> None:
        """Right hole should be at the right edge, vertically centered."""
        cx, cy = _hole_center(
            ResolvedHoleSpec(diameter=0.125, location="right"),
            label_width=2.0,
            label_height=1.0,
            margin=0.1,
        )
        assert math.isclose(cx, 0.1 + 2.0)
        assert math.isclose(cy, 0.1 + 0.5)

    def test_top_hole(self) -> None:
        """Top hole should be at the top edge, horizontally centered."""
        cx, cy = _hole_center(
            ResolvedHoleSpec(diameter=0.125, location="top"),
            label_width=2.0,
            label_height=1.0,
            margin=0.1,
        )
        assert math.isclose(cx, 0.1 + 1.0)
        assert math.isclose(cy, 0.1 + 1.0)

    def test_bottom_hole(self) -> None:
        """Bottom hole should be at the bottom edge, horizontally centered."""
        cx, cy = _hole_center(
            ResolvedHoleSpec(diameter=0.125, location="bottom"),
            label_width=2.0,
            label_height=1.0,
            margin=0.1,
        )
        assert math.isclose(cx, 0.1 + 1.0)
        assert math.isclose(cy, 0.1)

    def test_top_left_corner(self) -> None:
        """Top-left hole should be at the top-left corner."""
        cx, cy = _hole_center(
            ResolvedHoleSpec(diameter=0.125, location="top-left"),
            label_width=2.0,
            label_height=1.0,
            margin=0.1,
        )
        assert math.isclose(cx, 0.1)
        assert math.isclose(cy, 0.1 + 1.0)

    def test_bottom_right_corner(self) -> None:
        """Bottom-right hole should be at the bottom-right corner."""
        cx, cy = _hole_center(
            ResolvedHoleSpec(diameter=0.125, location="bottom-right"),
            label_width=2.0,
            label_height=1.0,
            margin=0.1,
        )
        assert math.isclose(cx, 0.1 + 2.0)
        assert math.isclose(cy, 0.1)


class TestRenderBoundary:
    """Tests for boundary rendering."""

    def test_boundary_dimensions(self) -> None:
        """Boundary should match label dimensions."""
        label = _make_label(width=2.0, height=1.0, margin=0.1)
        lc = _render_boundary(label, 0.0, 0.0, 0.0)
        bounds = lc.bounds()
        assert bounds is not None
        assert math.isclose(bounds[2] - bounds[0], 2.0)
        assert math.isclose(bounds[3] - bounds[1], 1.0)

    def test_boundary_with_translation(self) -> None:
        """Boundary should be translated to the packed position."""
        label = _make_label(width=2.0, height=1.0)
        lc = _render_boundary(label, 5.0, 3.0, 0.0)
        bounds = lc.bounds()
        assert bounds is not None
        assert math.isclose(bounds[0], 5.0)
        assert math.isclose(bounds[1], 3.0)


class TestRenderHoles:
    """Tests for hole rendering."""

    def test_no_holes_returns_empty(self) -> None:
        """Label with no holes should return empty LineCollection."""
        label = _make_label(holes=[])
        lc = _render_holes(label, 0.0, 0.0, 0.0)
        assert lc.is_empty()

    def test_single_hole(self) -> None:
        """Label with one hole should produce non-empty geometry."""
        label = _make_label(
            width=2.0,
            height=1.0,
            margin=0.1,
            holes=[ResolvedHoleSpec(diameter=0.125, location="left")],
        )
        lc = _render_holes(label, 0.0, 0.0, 0.0)
        assert not lc.is_empty()

    def test_multiple_holes(self) -> None:
        """Label with multiple holes should produce geometry for each."""
        label = _make_label(
            width=2.0,
            height=1.0,
            margin=0.1,
            holes=[
                ResolvedHoleSpec(diameter=0.125, location="left"),
                ResolvedHoleSpec(diameter=0.125, location="right"),
            ],
        )
        lc = _render_holes(label, 0.0, 0.0, 0.0)
        assert not lc.is_empty()


class TestRenderText:
    """Tests for text rendering."""

    def test_no_content_returns_empty(self) -> None:
        """Label with no content should return empty LineCollection."""
        label = _make_label(content=[])
        lc = _render_text(label, 0.0, 0.0, 0.0)
        assert lc.is_empty()

    def test_single_line(self) -> None:
        """Label with single text line should produce non-empty geometry."""
        label = _make_label(
            width=2.0,
            height=1.0,
            content=[ResolvedTextLine(text="HELLO", nominal_text_height=0.25, toolpath_text_height=0.22, cutter_diameter=0.03, character_spacing=0.0, line_spacing=0.0)],
        )
        lc = _render_text(label, 0.0, 0.0, 0.0)
        assert not lc.is_empty()

    def test_multiple_lines(self) -> None:
        """Label with multiple text lines should produce stacked geometry."""
        label = _make_label(
            width=2.0,
            height=1.5,
            content=[
                ResolvedTextLine(text="LINE 1", nominal_text_height=0.25, toolpath_text_height=0.22, cutter_diameter=0.03, character_spacing=0.0, line_spacing=0.1),
                ResolvedTextLine(text="LINE 2", nominal_text_height=0.25, toolpath_text_height=0.22, cutter_diameter=0.03, character_spacing=0.0, line_spacing=0.0),
            ],
        )
        lc = _render_text(label, 0.0, 0.0, 0.0)
        assert not lc.is_empty()

    def test_text_height_matches_toolpath_height(self) -> None:
        """Rendered text height should match toolpath_text_height in inches.

        This is a regression test for the vpype text coordinate system bug
        where ``text_block()`` renders glyphs at approximately 0.65625
        document units per unit of ``size``, so the size parameter must be
        divided by that factor to produce correctly-sized text.
        """
        target_height = 0.25
        label = _make_label(
            width=2.0,
            height=1.0,
            content=[ResolvedTextLine(
                text="HELLO",
                nominal_text_height=target_height,
                toolpath_text_height=target_height,
                cutter_diameter=0.03,
                character_spacing=0.0,
                line_spacing=0.0,
            )],
        )
        lc = _render_text(label, 0.0, 0.0, 0.0)
        bounds = lc.bounds()
        assert bounds is not None
        rendered_height = bounds[3] - bounds[1]
        # Rendered height should be approximately the toolpath_text_height
        # (within tolerance for font metrics)
        assert math.isclose(rendered_height, target_height, rel_tol=0.1)

    def test_text_width_fits_within_label(self) -> None:
        """Rendered text width should be reasonable relative to label width.

        Regression test: previously the width parameter was multiplied by 100
        and the size by POINTS_PER_INCH (72), producing text ~47x larger than
        intended. Text should now fit within the label's inner content area.
        """
        label_width = 2.0
        label = _make_label(
            width=label_width,
            height=1.0,
            content=[ResolvedTextLine(
                text="HELLO",
                nominal_text_height=0.25,
                toolpath_text_height=0.22,
                cutter_diameter=0.03,
                character_spacing=0.0,
                line_spacing=0.0,
            )],
        )
        lc = _render_text(label, 0.0, 0.0, 0.0)
        bounds = lc.bounds()
        assert bounds is not None
        rendered_width = bounds[2] - bounds[0]
        # Text should be much smaller than the label width (not 47x larger)
        assert rendered_width < label_width * 2

    def test_high_line_spacing_shrinks_to_preserve_margin(self) -> None:
        """Render-time safety check: oversized spacing must shrink, margins win.

        Regression test: a label whose stacked block (line heights plus
        requested spacing) exceeds the inner content area used to render
        text across the margins. The renderer must reduce inter-line
        spacing so the block fits within [margin, height - margin].
        """
        margin = 0.125
        label = _make_label(
            width=3.0,
            height=1.0,
            margin=margin,
            content=[
                ResolvedTextLine(text="VALVE V-104", nominal_text_height=0.3, toolpath_text_height=0.27, cutter_diameter=0.03, character_spacing=0.0, line_spacing=0.3),
                ResolvedTextLine(text="OPEN CW", nominal_text_height=0.3, toolpath_text_height=0.27, cutter_diameter=0.03, character_spacing=0.0, line_spacing=0.0),
            ],
        )
        lc = _render_text(label, 0.0, 0.0, 0.0)
        bounds = lc.bounds()
        assert bounds is not None
        _min_x, min_y, _max_x, max_y = bounds
        # Text must stay inside the margin box (small tolerance for glyph
        # metrics rounding in the ftext renderer).
        assert min_y >= margin - 0.02, f"Text bottom {min_y:.3f} breaches margin {margin}"
        assert max_y <= label.height - margin + 0.02, (
            f"Text top {max_y:.3f} breaches margin box top {label.height - margin}"
        )

    def test_text_positioned_within_label(self) -> None:
        """Text should start near the left margin, not at the origin.

        Regression test: previously text was positioned at the origin
        (0, 0) because the massive scale caused it to overflow.
        """
        margin = 0.1
        label = _make_label(
            width=2.0,
            height=1.0,
            margin=margin,
            content=[ResolvedTextLine(
                text="HELLO",
                nominal_text_height=0.25,
                toolpath_text_height=0.22,
                cutter_diameter=0.03,
                character_spacing=0.0,
                line_spacing=0.0,
            )],
        )
        lc = _render_text(label, 0.0, 0.0, 0.0)
        bounds = lc.bounds()
        assert bounds is not None
        # Text should be horizontally centered within the inner width
        # Center X position: margin + width / 2 = 0.1 + 2.0 / 2 = 1.1
        label_center_x = margin + label.width / 2
        text_center_x = (bounds[0] + bounds[2]) / 2
        assert math.isclose(text_center_x, label_center_x, abs_tol=0.15)
        # Text should be vertically centered within the inner height
        # Center Y position: margin + height / 2 = 0.1 + 1.0 / 2 = 0.6
        label_center_y = margin + label.height / 2
        text_center_y = (bounds[1] + bounds[3]) / 2
        assert math.isclose(text_center_y, label_center_y, abs_tol=0.15)
        # Text should be within label bounds (including margins)
        assert bounds[0] >= margin - 0.1
        assert bounds[1] >= margin - 0.1
        assert bounds[2] <= margin + label.width + 0.1
        assert bounds[3] <= margin + label.height + 0.1


class TestRenderTextVerticalCentering:
    """Variable line counts must stay vertically centered on the label.

    Replacement-driven labels may render a different number of text lines
    per instance (fewer delimited items than template lines). Every
    rendered block, regardless of line count, must be vertically centered
    within the inner content area.
    """

    @staticmethod
    def _line(text: str) -> ResolvedTextLine:
        """Build a minimal resolved line for centering checks."""
        return ResolvedTextLine(
            text=text,
            nominal_text_height=0.25,
            toolpath_text_height=0.22,
            cutter_diameter=0.03,
            character_spacing=0.0,
            line_spacing=0.1,
        )

    @pytest.mark.parametrize("line_count", [1, 2, 3, 4])
    def test_block_centered_regardless_of_line_count(self, line_count: int) -> None:
        """1-4 line blocks all center at margin + available_height / 2."""
        margin = 0.1
        height = 2.0
        label = _make_label(
            width=3.0,
            height=height,
            margin=margin,
            content=[self._line(f"LINE {i}") for i in range(line_count)],
        )
        lc = _render_text(label, 0.0, 0.0, 0.0)
        bounds = lc.bounds()
        assert bounds is not None
        expected_center = margin + (height - 2 * margin) / 2.0
        block_center = (bounds[1] + bounds[3]) / 2.0
        assert math.isclose(block_center, expected_center, abs_tol=0.05), (
            f"{line_count}-line block center {block_center:.3f} != "
            f"expected {expected_center:.3f}"
        )


class TestVectorizePlate:
    """Tests for the main vectorization function."""

    def test_empty_plate(self) -> None:
        """Empty plate should produce an empty document."""
        plate = PackedPlate(plate_id="p1", width=24.0, height=12.0)
        doc = vectorize_plate(plate)
        assert doc.is_empty()

    def test_single_label_creates_layers(self) -> None:
        """A label with text, boundary, and holes should create all three layers."""
        label = _make_label(
            width=2.0,
            height=1.0,
            margin=0.1,
            holes=[ResolvedHoleSpec(diameter=0.125, location="left")],
            content=[ResolvedTextLine(text="X", nominal_text_height=0.25, toolpath_text_height=0.22, cutter_diameter=0.03, character_spacing=0.0, line_spacing=0.0)],
        )
        packed = _make_packed(label, x=1.0, y=2.0)
        plate = PackedPlate(plate_id="p1", width=24.0, height=12.0, labels=[packed])
        doc = vectorize_plate(plate)
        # Should have content on all three layers
        assert _layer_has_content(doc, LAYER_TEXT)
        assert _layer_has_content(doc, LAYER_BOUNDARY)
        assert _layer_has_content(doc, LAYER_HOLES)

    def test_text_only_label(self) -> None:
        """A label with only text should only create the text layer."""
        label = _make_label(
            width=2.0,
            height=1.0,
            holes=[],
            content=[ResolvedTextLine(text="HELLO", nominal_text_height=0.25, toolpath_text_height=0.22, cutter_diameter=0.03, character_spacing=0.0, line_spacing=0.0)],
        )
        packed = _make_packed(label)
        plate = PackedPlate(plate_id="p1", width=24.0, height=12.0, labels=[packed])
        doc = vectorize_plate(plate)
        assert _layer_has_content(doc, LAYER_TEXT)
        assert _layer_has_content(doc, LAYER_BOUNDARY)  # Boundary is always drawn
        assert not _layer_has_content(doc, LAYER_HOLES)  # No holes

    def test_rotated_label(self) -> None:
        """A rotated label should still produce geometry on all relevant layers."""
        label = _make_label(
            width=2.0,
            height=1.0,
            margin=0.1,
            holes=[ResolvedHoleSpec(diameter=0.125, location="left")],
            content=[ResolvedTextLine(text="X", nominal_text_height=0.25, toolpath_text_height=0.22, cutter_diameter=0.03, character_spacing=0.0, line_spacing=0.0)],
        )
        packed = _make_packed(label, x=1.0, y=2.0, width=1.0, height=2.0, rotated=True)
        plate = PackedPlate(plate_id="p1", width=24.0, height=12.0, labels=[packed])
        doc = vectorize_plate(plate)
        assert _layer_has_content(doc, LAYER_TEXT)
        assert _layer_has_content(doc, LAYER_BOUNDARY)
        assert _layer_has_content(doc, LAYER_HOLES)


class TestVectorizePlates:
    """Tests for the multi-plate vectorization function."""

    def test_multiple_plates(self) -> None:
        """Multiple plates should produce multiple documents."""
        label = _make_label(width=2.0, height=1.0)
        packed = _make_packed(label)
        plates = [
            PackedPlate(plate_id="p1", width=24.0, height=12.0, labels=[packed]),
            PackedPlate(plate_id="p2", width=24.0, height=12.0, labels=[packed]),
        ]
        docs = vectorize_plates(plates)
        assert len(docs) == 2
        assert all(isinstance(d, vp.Document) for d in docs)

    def test_empty_list(self) -> None:
        """Empty plate list should produce empty document list."""
        docs = vectorize_plates([])
        assert docs == []


class TestExportToPlt:
    """Tests for PLT export."""

    def test_export_creates_file(self, tmp_path: Path) -> None:
        """Export should create a file at the specified path."""
        label = _make_label(width=2.0, height=1.0)
        packed = _make_packed(label)
        plate = PackedPlate(plate_id="p1", width=24.0, height=12.0, labels=[packed])
        doc = vectorize_plate(plate)

        output_path = tmp_path / "test.plt"
        result = export_to_plt(doc, output_path, page_size=(24.0, 12.0))

        assert result.exists()
        assert result.stat().st_size > 0

    def test_export_creates_parent_directories(self, tmp_path: Path) -> None:
        """Export should create parent directories if they don't exist."""
        label = _make_label(width=2.0, height=1.0)
        packed = _make_packed(label)
        plate = PackedPlate(plate_id="p1", width=24.0, height=12.0, labels=[packed])
        doc = vectorize_plate(plate)

        output_path = tmp_path / "subdir" / "nested" / "test.plt"
        export_to_plt(doc, output_path, page_size=(24.0, 12.0))

        assert output_path.exists()

    def test_export_contains_hpgl_commands(self, tmp_path: Path) -> None:
        """Exported PLT file should contain HPGL commands."""
        label = _make_label(
            width=2.0,
            height=1.0,
            content=[ResolvedTextLine(text="HELLO", nominal_text_height=0.25, toolpath_text_height=0.22, cutter_diameter=0.03, character_spacing=0.0, line_spacing=0.0)],
        )
        packed = _make_packed(label)
        plate = PackedPlate(plate_id="p1", width=24.0, height=12.0, labels=[packed])
        doc = vectorize_plate(plate)

        output_path = tmp_path / "test.plt"
        export_to_plt(doc, output_path, page_size=(24.0, 12.0))

        content = output_path.read_text(encoding="utf-8")
        # HPGL files should contain pen commands
        assert "PU" in content or "PD" in content or "PA" in content


class TestExportAndOptimize:
    """Tests for the combined export and optimize function."""

    def test_export_without_optimization(self, tmp_path: Path) -> None:
        """Export without optimization should create separate layer files."""
        label = _make_label(width=2.0, height=1.0)
        packed = _make_packed(label)
        plate = PackedPlate(plate_id="p1", width=24.0, height=12.0, labels=[packed])

        paths = export_and_optimize([plate], tmp_path, optimize=False, separate_layers=True)

        # Should have text and borders layers (no holes)
        assert len(paths) == 2
        assert all(p.exists() for p in paths)
        assert all(p.suffix == ".plt" for p in paths)
        assert any("text" in p.name for p in paths)
        assert any("border" in p.name for p in paths)

    def test_export_multiple_plates(self, tmp_path: Path) -> None:
        """Multiple plates should produce multiple layer files per plate."""
        label = _make_label(width=2.0, height=1.0)
        packed = _make_packed(label)
        plates = [
            PackedPlate(plate_id="p1", width=24.0, height=12.0, labels=[packed]),
            PackedPlate(plate_id="p2", width=24.0, height=12.0, labels=[packed]),
        ]

        paths = export_and_optimize(plates, tmp_path, optimize=False, separate_layers=True)

        # 2 plates × 2 layers each = 4 files (text and borders for each plate)
        assert len(paths) == 4
        assert all(p.exists() for p in paths)

    def test_export_with_optimization(self, tmp_path: Path) -> None:
        """Export with optimization should still produce valid files."""
        label = _make_label(
            width=2.0,
            height=1.0,
            content=[ResolvedTextLine(text="HELLO", nominal_text_height=0.25, toolpath_text_height=0.22, cutter_diameter=0.03, character_spacing=0.0, line_spacing=0.0)],
        )
        packed = _make_packed(label)
        plate = PackedPlate(plate_id="p1", width=24.0, height=12.0, labels=[packed])

        paths = export_and_optimize([plate], tmp_path, optimize=True, separate_layers=True)

        # 2 layers (text, borders) × 2 (original + optimized for borders only)
        # Text layer: not optimized (no optimization benefit)
        # Borders layer: optimized (reduces travel)
        assert len(paths) >= 2
        assert all(p.exists() for p in paths)
        assert all(p.stat().st_size > 0 for p in paths)


class TestCoordinateConstraints:
    """Tests that PLT exports contain only positive coordinates."""

    def test_coordinates_all_positive(self, tmp_path: Path) -> None:
        """Verify all exported coordinates are non-negative (x ≥ 0, y ≥ 0)."""
        import re

        label = _make_label(
            width=3.0,
            height=1.0,
            content=[
                ResolvedTextLine(
                    text="Test",
                    nominal_text_height=0.5,
                    toolpath_text_height=0.47,
                    cutter_diameter=0.03,
                    character_spacing=0.0,
                    line_spacing=0.0,
                )
            ],
        )
        packed = _make_packed(label)
        plate = PackedPlate(plate_id="p1", width=3.0, height=1.0, labels=[packed])

        # Export to PLT
        plt_path = tmp_path / "test.plt"
        export_to_plt(vectorize_plate(plate), plt_path, page_size=(3.0, 1.0))

        # Verify all coordinates are non-negative
        content = plt_path.read_text()
        coord_pattern = r"(PA|PU|PD)([\d,\-]+)"
        
        for cmd, coords_str in re.findall(coord_pattern, content):
            coords = coords_str.split(",")
            for i in range(0, len(coords) - 1, 2):
                try:
                    x = int(coords[i])
                    y = int(coords[i + 1])
                    assert x >= 0, f"Found negative X coordinate: {x} in {cmd}{coords_str}"
                    assert y >= 0, f"Found negative Y coordinate: {y} in {cmd}{coords_str}"
                except (ValueError, IndexError):
                    pass

    def test_coordinates_within_plate_bounds(self, tmp_path: Path) -> None:
        """Verify all coordinates are within expected plate bounds."""
        import re

        # 2x1 inch plate with labels
        label = _make_label(width=2.0, height=1.0)
        packed = _make_packed(label)
        plate = PackedPlate(plate_id="p1", width=2.0, height=1.0, labels=[packed])

        # Export to PLT
        plt_path = tmp_path / "test.plt"
        export_to_plt(vectorize_plate(plate), plt_path, page_size=(2.0, 1.0))

        # Extract coordinate ranges
        content = plt_path.read_text()
        coord_pattern = r"(PA|PU|PD)([\d,\-]+)"
        
        x_coords = []
        y_coords = []
        for cmd, coords_str in re.findall(coord_pattern, content):
            coords = coords_str.split(",")
            for i in range(0, len(coords) - 1, 2):
                try:
                    x = int(coords[i])
                    y = int(coords[i + 1])
                    x_coords.append(x)
                    y_coords.append(y)
                except (ValueError, IndexError):
                    pass

        # Verify bounds: coordinates should be within plate dimensions
        # Allow small margin for text rendering artifacts
        if x_coords and y_coords:
            max_x = max(x_coords)
            max_y = max(y_coords)
            # Plate is 2.0 × 1.0 inches = 2000 × 1000 units
            # Text may exceed slightly due to rendering, but should be close
            assert max_x <= 2500, f"X coordinate {max_x} exceeds plate width 2000"
            assert max_y <= 1500, f"Y coordinate {max_y} exceeds plate height 1000"


class TestRenderTextHorizontalCompression:
    """The vectorize render path must compress over-wide lines to the margins."""

    @staticmethod
    def _line(max_h_compress: float) -> ResolvedTextLine:
        return ResolvedTextLine(
            text="SAFETY long long text",
            nominal_text_height=0.5,
            toolpath_text_height=0.47,
            cutter_diameter=0.03,
            character_spacing=0.0,
            line_spacing=0.0,
            max_h_compress=max_h_compress,
        )

    def test_long_line_stays_inside_margin_box(self) -> None:
        """With compression allowed, rendered text must respect the margins."""
        margin = 0.15
        label = _make_label(
            width=5.0, height=1.5, margin=margin, content=[self._line(0.5)]
        )
        lc = _render_text(label, 0.0, 0.0, 0.0)
        assert not lc.is_empty()

        min_x, _min_y, max_x, _max_y = lc.bounds()
        assert min_x >= margin - 0.01, f"Text breaches left margin: {min_x:.3f}"
        assert max_x <= label.width - margin + 0.01, (
            f"Text breaches right margin: {max_x:.3f}"
        )

    def test_disabled_compression_leaves_line_over_wide(self) -> None:
        """Without max_h_compress the line still overflows (opt-in behaviour)."""
        margin = 0.15
        label = _make_label(
            width=5.0, height=1.5, margin=margin, content=[self._line(0.0)]
        )
        lc = _render_text(label, 0.0, 0.0, 0.0)
        min_x, _min_y, max_x, _max_y = lc.bounds()
        available = label.width - (2 * margin)
        assert (max_x - min_x) > available + 0.02, (
            "Line unexpectedly fits without compression; test text is too short"
        )

    def test_compression_shrinks_width_vs_disabled(self) -> None:
        """The same line must render narrower when compression is allowed."""
        compressed = _render_text(
            _make_label(width=5.0, height=1.5, margin=0.15, content=[self._line(0.5)]),
            0.0, 0.0, 0.0,
        )
        natural = _render_text(
            _make_label(width=5.0, height=1.5, margin=0.15, content=[self._line(0.0)]),
            0.0, 0.0, 0.0,
        )

        c_min_x, _c0, c_max_x, _c1 = compressed.bounds()
        n_min_x, _n0, n_max_x, _n1 = natural.bounds()
        assert (c_max_x - c_min_x) < (n_max_x - n_min_x)

    def test_compression_preserves_glyph_height(self) -> None:
        """Horizontal-only scaling must not change the rendered block height."""
        compressed = _render_text(
            _make_label(width=5.0, height=1.5, margin=0.15, content=[self._line(0.5)]),
            0.0, 0.0, 0.0,
        )
        natural = _render_text(
            _make_label(width=5.0, height=1.5, margin=0.15, content=[self._line(0.0)]),
            0.0, 0.0, 0.0,
        )

        _c0, c_min_y, _c1, c_max_y = compressed.bounds()
        _n0, n_min_y, _n1, n_max_y = natural.bounds()
        assert (c_max_y - c_min_y) == pytest.approx(n_max_y - n_min_y, abs=1e-6)


class TestRenderTextHorizontalAlignment:
    """The vectorize render path must honour per-line text_h_alignment."""

    @staticmethod
    def _line(alignment: str) -> ResolvedTextLine:
        return ResolvedTextLine(
            text="AB",
            nominal_text_height=0.3,
            toolpath_text_height=0.27,
            cutter_diameter=0.03,
            character_spacing=0.0,
            line_spacing=0.0,
            text_h_alignment=alignment,
        )

    def test_left_aligns_left_edge_at_margin(self) -> None:
        """A left-aligned line's left-most point sits precisely at the margin."""
        margin = 0.2
        label = _make_label(
            width=4.0, height=1.0, margin=margin, content=[self._line("left")]
        )
        lc = _render_text(label, 0.0, 0.0, 0.0)
        assert not lc.is_empty()

        min_x, _min_y, _max_x, _max_y = lc.bounds()
        assert min_x == pytest.approx(margin, abs=0.01)

    def test_right_aligns_right_edge_at_margin(self) -> None:
        """A right-aligned line's right-most point sits precisely at the margin."""
        label = _make_label(
            width=4.0, height=1.0, margin=0.2, content=[self._line("right")]
        )
        lc = _render_text(label, 0.0, 0.0, 0.0)
        _min_x, _min_y, max_x, _max_y = lc.bounds()
        assert max_x == pytest.approx(label.width - label.margin, abs=0.01)

    def test_center_stays_centered(self) -> None:
        """Centered text must remain centered within the inner content area."""
        label = _make_label(
            width=4.0, height=1.0, margin=0.2, content=[self._line("center")]
        )
        lc = _render_text(label, 0.0, 0.0, 0.0)
        min_x, _min_y, max_x, _max_y = lc.bounds()

        inner_center = label.margin + (label.width - 2 * label.margin) / 2
        assert (min_x + max_x) / 2 == pytest.approx(inner_center, abs=0.01)

    def test_alignment_survives_translation(self) -> None:
        """Alignment must hold after the label's dx/dy placement transform."""
        margin = 0.2
        dx = 5.0
        label = _make_label(
            width=4.0, height=1.0, margin=margin, content=[self._line("left")]
        )
        lc = _render_text(label, dx, 0.0, 0.0)
        min_x, _min_y, _max_x, _max_y = lc.bounds()
        assert min_x == pytest.approx(dx + margin, abs=0.01)

    def test_default_alignment_matches_center(self) -> None:
        """A line built without alignment (dataclass default) centers as before."""
        defaulted = ResolvedTextLine(
            text="AB",
            nominal_text_height=0.3,
            toolpath_text_height=0.27,
            cutter_diameter=0.03,
            character_spacing=0.0,
            line_spacing=0.0,
        )
        default_bounds = _render_text(
            _make_label(width=4.0, height=1.0, margin=0.2, content=[defaulted]),
            0.0, 0.0, 0.0,
        ).bounds()
        center_bounds = _render_text(
            _make_label(
                width=4.0, height=1.0, margin=0.2, content=[self._line("center")]
            ),
            0.0, 0.0, 0.0,
        ).bounds()

        assert default_bounds[0] == pytest.approx(center_bounds[0], abs=1e-9)
        assert default_bounds[2] == pytest.approx(center_bounds[2], abs=1e-9)


class TestVectorizeCollisionLogging:
    """``_render_label_to_doc`` must surface text-hole collisions (Phase 1)."""

    def test_colliding_label_logs_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """Rendering a colliding label to a doc logs a collision ERROR."""
        import logging

        label = _make_label(
            width=3.0,
            height=1.0,
            margin=0.1,
            holes=[ResolvedHoleSpec(diameter=0.25, location="left")],
            content=[
                ResolvedTextLine(
                    text="WIDE LABEL TEXT",
                    nominal_text_height=0.5,
                    toolpath_text_height=0.47,
                    cutter_diameter=0.03,
                    character_spacing=0.0,
                    line_spacing=0.0,
                )
            ],
        )
        plate = PackedPlate(plate_id="p1", width=12.0, height=8.0, labels=[_make_packed(label)])

        with caplog.at_level(
            logging.ERROR, logger="plt_optimizer.generate.label_renderer"
        ):
            vectorize_plate(plate)

        assert any("collides with" in r.message for r in caplog.records)

    def test_clean_label_logs_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        """A label whose text clears its holes logs no collision error."""
        import logging

        label = _make_label(
            width=3.0,
            height=1.0,
            margin=0.1,
            holes=[ResolvedHoleSpec(diameter=0.25, location="left")],
            content=[
                ResolvedTextLine(
                    text="HI",
                    nominal_text_height=0.3,
                    toolpath_text_height=0.27,
                    cutter_diameter=0.03,
                    character_spacing=0.0,
                    line_spacing=0.0,
                )
            ],
        )
        plate = PackedPlate(plate_id="p1", width=12.0, height=8.0, labels=[_make_packed(label)])

        with caplog.at_level(
            logging.WARNING, logger="plt_optimizer.generate.label_renderer"
        ):
            vectorize_plate(plate)

        assert not any("collides with" in r.message for r in caplog.records)
