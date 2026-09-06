"""Unit tests for label rendering engine."""

import re

import pytest

from plt_optimizer.generate.label_renderer import (
    _flip_y_coordinates_in_plt,
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
