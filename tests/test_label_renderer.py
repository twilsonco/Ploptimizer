"""Unit tests for label rendering engine."""

import math
import re

import pytest

from plt_optimizer.generate.label_renderer import (
    _flip_y_coordinates_in_plt,
    _render_text_local,
    extract_bounds_from_plt,
    render_label_to_plt,
)
from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine
from plt_optimizer.generate.schema import parse_yaml


class TestFlipYCoordinatesInPlt:
    """Tests for the Y-axis inversion post-processing step."""

    def test_flip_mirrors_y_across_centerline(self, tmp_path) -> None:
        """Y coordinates should be mirrored across the vertical centerline."""
        plt_file = tmp_path / "label.plt"
        # Points at y=200 and y=800; min+max = 1000.
        plt_file.write_text(
            "IN;DF;PS0;SP1;PU500,200;PD500,800;PA300,400;SP0;IN;%",
            encoding="utf-8",
        )

        _flip_y_coordinates_in_plt(plt_file)

        content = plt_file.read_text(encoding="utf-8")
        # 1000 - y: 200->800, 500->500 (center), 400->600
        assert "PU500,800" in content
        assert "PD500,200" in content
        assert "PA300,600" in content

    def test_flip_preserves_bounds(self, tmp_path) -> None:
        """Flipping should preserve min/max bounds (only orientation changes)."""
        plt_file = tmp_path / "label.plt"
        # y-values are 200 and 800; mirrored across centerline sum=1000.
        plt_file.write_text(
            "IN;DF;PS0;SP1;PU100,200;PD900,300;PA500,400;SP0;IN;%",
            encoding="utf-8",
        )

        before = extract_bounds_from_plt(plt_file.read_text(encoding="utf-8"))
        _flip_y_coordinates_in_plt(plt_file)
        after = extract_bounds_from_plt(plt_file.read_text(encoding="utf-8"))

        # min/max bounds are unchanged by the mirror.
        assert before == pytest.approx(after)

    def test_flip_no_coordinates_returns_unchanged(self, tmp_path) -> None:
        """A file with no coordinates should be left unchanged."""
        plt_file = tmp_path / "label.plt"
        original = "IN;DF;PS0;SP0;IN;%"
        plt_file.write_text(original, encoding="utf-8")

        _flip_y_coordinates_in_plt(plt_file)

        assert plt_file.read_text(encoding="utf-8") == original


class TestExtractBoundsFromPlt:
    """Tests for extract_bounds_from_plt() function."""

    def test_extract_bounds_simple_rectangle(self) -> None:
        """Test bounds extraction from simple rectangle PLT."""
        plt_content = "IN;DF;PS0;SP1;PA0,0;PD1000,0,1000,1000,0,1000,0,0;SP0;IN;%"
        x_min, y_min, x_max, y_max = extract_bounds_from_plt(plt_content)

        assert x_min == 0.0
        assert y_min == 0.0
        assert x_max == 1.0
        assert y_max == 1.0

    def test_extract_bounds_with_multiple_commands(self) -> None:
        """Test bounds extraction with multiple PA/PD commands."""
        plt_content = "IN;DF;PS0;SP1;PA500,500;PD1000,1000;PA100,200;PD800,900;SP0;IN;%"
        x_min, y_min, x_max, y_max = extract_bounds_from_plt(plt_content)

        assert x_min == 0.1
        assert y_min == 0.2
        assert x_max == 1.0
        assert y_max == 1.0

    def test_extract_bounds_negative_coordinates(self) -> None:
        """Test bounds with negative coordinates (should be converted by postprocessing)."""
        # Most PLT files have non-negative coordinates after postprocessing
        # but we should handle this gracefully
        plt_content = "IN;DF;PS0;SP1;PA-500,0;PD500,1000;SP0;IN;%"
        x_min, y_min, x_max, y_max = extract_bounds_from_plt(plt_content)

        assert x_min == -0.5
        assert x_max == 0.5

    def test_extract_bounds_no_coordinates(self) -> None:
        """Test that ValueError is raised if no coordinates found."""
        plt_content = "IN;DF;PS0;SP0;IN;%"
        with pytest.raises(ValueError, match="No valid coordinates"):
            extract_bounds_from_plt(plt_content)

    def test_extract_bounds_ignores_pu_commands(self) -> None:
        """Test that PU (pen-up) commands are included in bounds."""
        # PU coordinates define visual extent and should be included
        plt_content = "IN;DF;PS0;SP1;PU10000,10000;PA0,0;PD1000,1000;SP0;IN;%"
        x_min, y_min, x_max, y_max = extract_bounds_from_plt(plt_content)

        assert x_min == 0.0
        assert x_max == 10.0  # Now includes PU coordinate


class TestBoundaryClosure:
    """Tests that rendered boundary rectangles are geometrically closed.

    Regression: the ``_linecollection_to_hpgl`` "skip initial PU0,0" logic used
    to drop a genuine origin vertex of each boundary rectangle, leaving an open
    4-vertex outline instead of a closed 5-vertex loop.
    """

    def _boundary_points(self, plt_content: str) -> list[tuple[int, int]]:
        """Extract all coordinate pairs from the SP2 (boundary) section."""
        m = re.search(r"SP2;(.*?)(?:SP\d|$)", plt_content, re.DOTALL)
        assert m is not None, "No boundary layer found"
        points: list[tuple[int, int]] = []
        for mm in re.finditer(r"(PA|PU|PD)([\d,\-]+)", m.group(1)):
            parts = mm.group(2).split(",")
            try:
                for i in range(0, len(parts) - 1, 2):
                    points.append((int(parts[i]), int(parts[i + 1])))
            except (ValueError, IndexError):
                pass
        return points

    def test_boundary_is_closed_loop(self) -> None:
        """Boundary rectangle should close back to its starting vertex."""
        job = parse_yaml("examples/test123_spec.yaml")
        from plt_optimizer.generate.resolution import resolve_job_spec

        labels = resolve_job_spec(job)
        rendered = render_label_to_plt(labels[0])

        points = self._boundary_points(rendered.plt_content)
        # A closed rectangle has 5 vertices (4 corners + return to start).
        assert len(points) == 5, f"Expected 5 boundary vertices, got {points}"
        assert points[0] == points[-1], "Boundary must close back to its origin"
        # Exactly two unique x and y values => a proper axis-aligned rectangle.
        xs = {p[0] for p in points}
        ys = {p[1] for p in points}
        assert len(xs) == 2
        assert len(ys) == 2

    def test_boundary_spans_nominal_dimensions(self) -> None:
        """Boundary should span exactly the label's nominal width and height."""
        job = parse_yaml("examples/test123_spec.yaml")
        from plt_optimizer.generate.resolution import resolve_job_spec

        labels = resolve_job_spec(job)
        rendered = render_label_to_plt(labels[0])

        points = self._boundary_points(rendered.plt_content)
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]

        width_inches = (max(xs) - min(xs)) / 1000.0
        height_inches = (max(ys) - min(ys)) / 1000.0

        assert pytest.approx(width_inches, abs=0.01) == labels[0].width
        assert pytest.approx(height_inches, abs=0.01) == labels[0].height


class TestRenderLabelToPlt:
    """Tests for render_label_to_plt() function."""

    def test_render_simple_label(self) -> None:
        """Test rendering a simple single-label job."""
        job = parse_yaml("examples/test123_spec.yaml")
        from plt_optimizer.generate.resolution import resolve_job_spec

        labels = resolve_job_spec(job)
        label = labels[0]  # Test 1

        rendered = render_label_to_plt(label)

        assert rendered.source_label == label
        assert rendered.width > 0
        assert rendered.height > 0
        assert rendered.plt_content.startswith("IN;")
        assert rendered.plt_content.endswith("%")

    def test_render_label_bounds_are_positive(self) -> None:
        """Test that rendered bounds are non-negative (after postprocessing)."""
        job = parse_yaml("examples/test123_spec.yaml")
        from plt_optimizer.generate.resolution import resolve_job_spec

        labels = resolve_job_spec(job)
        label = labels[0]

        rendered = render_label_to_plt(label)

        assert rendered.x_min >= 0
        assert rendered.y_min >= 0
        assert rendered.x_max > rendered.x_min
        assert rendered.y_max > rendered.y_min

    def test_render_label_bounds_reasonable(self) -> None:
        """Test that rendered bounds are reasonable (within label dimensions)."""
        job = parse_yaml("examples/test123_spec.yaml")
        from plt_optimizer.generate.resolution import resolve_job_spec

        labels = resolve_job_spec(job)
        label = labels[0]

        rendered = render_label_to_plt(label)

        # Bounds should be slightly larger than label dimensions due to text/borders
        # but roughly in the right ballpark
        assert rendered.width <= label.width * 1.5
        assert rendered.height <= label.height * 1.5

    def test_render_all_test123_labels(self) -> None:
        """Test rendering all three test123 labels."""
        job = parse_yaml("examples/test123_spec.yaml")
        from plt_optimizer.generate.resolution import resolve_job_spec

        labels = resolve_job_spec(job)
        rendered_labels = []

        for label in labels:
            rendered = render_label_to_plt(label)
            rendered_labels.append(rendered)
            # All should have valid content
            assert rendered.plt_content
            assert len(rendered.plt_content) > 20  # At least header + some content

        assert len(rendered_labels) == 3
        # All three should render with similar dimensions (they're identical labels)
        widths = [r.width for r in rendered_labels]
        heights = [r.height for r in rendered_labels]

        # Widths should all be close to 3.0"
        for w in widths:
            assert 2.5 < w < 3.5

        # Heights should all be close to 1.0"
        for h in heights:
            assert 0.7 < h < 1.3


def _make_local_label(
    content: list[ResolvedTextLine],
    width: float = 4.0,
    height: float = 2.0,
    margin: float = 0.1,
) -> ResolvedLabel:
    """Helper to build a ResolvedLabel for local text-rendering tests."""
    return ResolvedLabel(
        id="multi_line",
        count=1,
        width=width,
        height=height,
        margin=margin,
        holes=[],
        content=content,
    )


class TestMultiLineStacking:
    """Regression: multi-line labels must stack lines, not overlap them.

    The Phase 3 render path (``_render_text_local``) used to translate every
    line to y=0, printing all lines on the same baseline with overlapping
    glyphs (visible in complex_test_job.yaml PNG previews).
    """

    def test_two_line_block_height_includes_spacing(self) -> None:
        """Rendered block height must equal sum of line heights plus spacing."""
        h1, h2, spacing = 0.3, 0.2, 0.1
        label = _make_local_label(
            [
                ResolvedTextLine(
                    text="AAA",
                    nominal_text_height=h1,
                    toolpath_text_height=h1,
                    cutter_diameter=0.0,
                    character_spacing=0.0,
                    line_spacing=spacing,
                ),
                ResolvedTextLine(
                    text="BBB",
                    nominal_text_height=h2,
                    toolpath_text_height=h2,
                    cutter_diameter=0.0,
                    character_spacing=0.0,
                    line_spacing=0.0,
                ),
            ]
        )

        lc = _render_text_local(label)
        assert not lc.is_empty()
        _min_x, min_y, _max_x, max_y = lc.bounds()
        block_height = max_y - min_y

        expected = h1 + spacing + h2
        assert math.isclose(block_height, expected, abs_tol=0.02), (
            f"Block height {block_height:.3f}in != stacked {expected:.3f}in "
            "(lines are likely overlapping on one baseline)"
        )

    def test_gap_between_lines_has_no_geometry(self) -> None:
        """The line_spacing gap between stacked lines must stay empty."""
        h1, h2, spacing = 0.3, 0.2, 0.1
        label = _make_local_label(
            [
                ResolvedTextLine(
                    text="AAA",
                    nominal_text_height=h1,
                    toolpath_text_height=h1,
                    cutter_diameter=0.0,
                    character_spacing=0.0,
                    line_spacing=spacing,
                ),
                ResolvedTextLine(
                    text="BBB",
                    nominal_text_height=h2,
                    toolpath_text_height=h2,
                    cutter_diameter=0.0,
                    character_spacing=0.0,
                    line_spacing=0.0,
                ),
            ]
        )

        lc = _render_text_local(label)
        ys = [point.imag for segment in lc for point in segment]

        # Block is anchored centered: total 0.6 spans [-0.3, 0.3].
        # Top line occupies [0.0, 0.3]; bottom line [-0.3, -0.1].
        # Nothing may fall strictly inside the spacing gap (-0.1, 0.0).
        in_gap = [y for y in ys if -0.09 < y < -0.01]
        assert not in_gap, f"Geometry found inside inter-line gap: {in_gap}"

    def test_render_label_to_plt_stacks_three_lines(self) -> None:
        """End-to-end: rendered PLT text layer must span all stacked lines."""
        label = _make_local_label(
            [
                ResolvedTextLine(
                    text="MAIN PANEL",
                    nominal_text_height=0.7,
                    toolpath_text_height=0.7,
                    cutter_diameter=0.0,
                    character_spacing=0.0,
                    line_spacing=0.12,
                ),
                ResolvedTextLine(
                    text="SECTION B",
                    nominal_text_height=0.3,
                    toolpath_text_height=0.3,
                    cutter_diameter=0.0,
                    character_spacing=0.0,
                    line_spacing=0.12,
                ),
                ResolvedTextLine(
                    text="ACCESS ONLY",
                    nominal_text_height=0.2,
                    toolpath_text_height=0.2,
                    cutter_diameter=0.0,
                    character_spacing=0.0,
                    line_spacing=0.0,
                ),
            ],
            width=6.0,
            height=2.0,
        )

        rendered = render_label_to_plt(label)

        match = re.search(r"SP1;(.*?)(?:SP\d|$)", rendered.plt_content, re.DOTALL)
        assert match is not None, "No text layer found in rendered PLT"
        ys: list[float] = []
        for coord_match in re.finditer(r"(?:PA|PU|PD)([\d,\-]+)", match.group(1)):
            parts = coord_match.group(1).split(",")
            for i in range(1, len(parts), 2):
                ys.append(int(parts[i]) / 1000.0)
        assert ys, "No text coordinates found"

        text_span = max(ys) - min(ys)
        expected_total = 0.7 + 0.12 + 0.3 + 0.12 + 0.2
        # Stacked span must cover all three lines plus spacing; the buggy
        # behavior collapsed everything to the tallest single line (0.7).
        assert text_span > expected_total - 0.05, (
            f"Text layer spans only {text_span:.3f}in; expected ~"
            f"{expected_total:.3f}in stacked (lines overlapping?)"
        )
        assert text_span < expected_total + 0.1


class TestMarginPrecedence:
    """Regression: line spacing must never push text across the margins.

    When the stacked block (line heights plus requested spacing) exceeds
    the inner content area, the renderer shrinks inter-line spacing so the
    resolved margin is preserved.
    """

    def test_high_spacing_block_stays_inside_margin_box(self) -> None:
        """Rendered text layer must stay within [margin, height - margin]."""
        margin = 0.125
        label = _make_local_label(
            [
                ResolvedTextLine(
                    text="VALVE V-104",
                    nominal_text_height=0.3,
                    toolpath_text_height=0.27,
                    cutter_diameter=0.03,
                    character_spacing=0.0,
                    line_spacing=0.3,
                ),
                ResolvedTextLine(
                    text="OPEN CW",
                    nominal_text_height=0.3,
                    toolpath_text_height=0.27,
                    cutter_diameter=0.03,
                    character_spacing=0.0,
                    line_spacing=0.0,
                ),
            ],
            width=3.0,
            height=1.0,
            margin=margin,
        )

        rendered = render_label_to_plt(label)

        match = re.search(r"SP1;(.*?)(?:SP\d|$)", rendered.plt_content, re.DOTALL)
        assert match is not None, "No text layer found in rendered PLT"
        ys: list[float] = []
        for coord_match in re.finditer(r"(?:PA|PU|PD)([\d,\-]+)", match.group(1)):
            parts = coord_match.group(1).split(",")
            for i in range(1, len(parts), 2):
                ys.append(int(parts[i]) / 1000.0)
        assert ys, "No text coordinates found"

        # Text must stay inside the margin box (small tolerance for
        # plotter-unit rounding in the HPGL writer).
        assert min(ys) >= margin - 0.02, f"Text breaches bottom margin: {min(ys):.3f}"
        assert max(ys) <= label.height - margin + 0.02, f"Text breaches top margin: {max(ys):.3f}"

    def test_render_text_local_clamps_spacing(self) -> None:
        """Stacked block height must not exceed the inner content height."""
        margin = 0.125
        label = _make_local_label(
            [
                ResolvedTextLine(
                    text="VALVE V-104",
                    nominal_text_height=0.3,
                    toolpath_text_height=0.27,
                    cutter_diameter=0.03,
                    character_spacing=0.0,
                    line_spacing=0.3,
                ),
                ResolvedTextLine(
                    text="OPEN CW",
                    nominal_text_height=0.3,
                    toolpath_text_height=0.27,
                    cutter_diameter=0.03,
                    character_spacing=0.0,
                    line_spacing=0.0,
                ),
            ],
            width=3.0,
            height=1.0,
            margin=margin,
        )

        lc = _render_text_local(label)
        assert not lc.is_empty()
        _min_x, min_y, _max_x, max_y = lc.bounds()
        block_height = max_y - min_y
        available = label.height - 2 * margin
        assert block_height <= available + 0.02, (
            f"Block height {block_height:.3f}in exceeds inner area "
            f"{available:.3f}in; margins would be breached"
        )
