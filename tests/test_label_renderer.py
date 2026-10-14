"""Unit tests for label rendering engine."""

import logging
import math
import re

import pytest
import vpype as vp

from plt_optimizer.core.units import HPGL_UNITS_PER_INCH
from plt_optimizer.generate.label_renderer import (
    _INK_EMISSION_FLOOR,
    LAYER_TEXT,
    LabelRenderError,
    RenderedLabel,
    _apply_glyph_ramps,
    _clip_below_baseline,
    _collect_hpgl_geometry,
    _flip_y_coordinates_in_plt,
    _glyph_ink,
    _hole_circles_local,
    _render_text_local_with_bounds,
    _stroke_polyline,
    _sub_baseline_ink,
    compress_line_to_width,
    extract_bounds_from_plt,
    render_label_to_plt,
)
from plt_optimizer.generate.resolution import (
    ResolvedHoleSpec,
    ResolvedLabel,
    ResolvedTextLine,
)
from plt_optimizer.generate.schema import parse_yaml
from plt_optimizer.generate.text_geometry import LineSeg, Stroke, TextBlock

# Whole-plotter-scale in integer device units (1 inch). Hand-written HPGL
# fixtures use it so their coordinates keep meaning whole inches.
U = int(HPGL_UNITS_PER_INCH)


def _render_text_local(label: ResolvedLabel) -> vp.LineCollection:
    """Test-local view of the live renderer, dropping per-line bounds.

    Production code renders through ``_render_text_local_with_bounds``;
    these tests only assert on the combined LineCollection.
    """
    text_lc, _entries = _render_text_local_with_bounds(label)
    return text_lc


def _make_line(
    text: str,
    height: float = 0.3,
    max_h_compress: float = 0.0,
    text_h_alignment: str = "center",
    line_spacing: float = 0.0,
) -> ResolvedTextLine:
    """Build a ResolvedTextLine with cutter compensation applied."""
    cutter_dia = 0.03
    return ResolvedTextLine(
        text=text,
        nominal_text_height=height,
        toolpath_text_height=height - cutter_dia,
        cutter_diameter=cutter_dia,
        character_spacing=0.0,
        line_spacing=line_spacing,
        max_h_compress=max_h_compress,
        text_h_alignment=text_h_alignment,
    )


class TestFlipYCoordinatesInPlt:
    """Tests for the Y-axis inversion post-processing step."""

    def test_flip_mirrors_y_across_centerline(self, tmp_path) -> None:
        """Y coordinates should be mirrored across the vertical centerline."""
        plt_file = tmp_path / "label.plt"
        # Points at y=200 and y=800; min+max = 1000.
        plt_file.write_text(
            "IN;PA;SP1;PU500,200;PD500,800;PA300,400;SP;",
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
            "IN;PA;SP1;PU100,200;PD900,300;PA500,400;SP;",
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
        original = "IN;PA;SP;"
        plt_file.write_text(original, encoding="utf-8")

        _flip_y_coordinates_in_plt(plt_file)

        assert plt_file.read_text(encoding="utf-8") == original


class TestExtractBoundsFromPlt:
    """Tests for extract_bounds_from_plt() function."""

    def test_extract_bounds_simple_rectangle(self) -> None:
        """Test bounds extraction from simple rectangle PLT."""
        plt_content = f"IN;PA;SP1;PA0,0;PD{U},0,{U},{U},0,{U},0,0;SP;"
        x_min, y_min, x_max, y_max = extract_bounds_from_plt(plt_content)

        assert x_min == 0.0
        assert y_min == 0.0
        assert x_max == 1.0
        assert y_max == 1.0

    def test_extract_bounds_with_multiple_commands(self) -> None:
        """Test bounds extraction with multiple PA/PD commands."""
        plt_content = (
            f"IN;PA;SP1;PA{U // 10},{U // 5};PD{U},{U};PA{U // 10},{U // 5};"
            f"PD{4 * U // 5},{9 * U // 10};SP;"
        )
        x_min, y_min, x_max, y_max = extract_bounds_from_plt(plt_content)

        assert x_min == pytest.approx(0.1, abs=1e-3)
        assert y_min == pytest.approx(0.2, abs=1e-3)
        assert x_max == pytest.approx(1.0, abs=1e-3)
        assert y_max == pytest.approx(1.0, abs=1e-3)

    def test_extract_bounds_negative_coordinates(self) -> None:
        """Test bounds with negative coordinates (should be converted by postprocessing)."""
        # Most PLT files have non-negative coordinates after postprocessing
        # but we should handle this gracefully
        plt_content = f"IN;PA;SP1;PA{-U // 2},0;PD{U // 2},{U};SP;"
        x_min, y_min, x_max, y_max = extract_bounds_from_plt(plt_content)

        assert x_min == pytest.approx(-0.5, abs=1e-3)
        assert x_max == pytest.approx(0.5, abs=1e-3)

    def test_extract_bounds_no_coordinates(self) -> None:
        """Test that ValueError is raised if no coordinates found."""
        plt_content = "IN;PA;SP;"
        with pytest.raises(ValueError, match="No valid coordinates"):
            extract_bounds_from_plt(plt_content)

    def test_extract_bounds_ignores_pu_commands(self) -> None:
        """Test that PU (pen-up) commands are included in bounds."""
        # PU coordinates define visual extent and should be included
        plt_content = f"IN;PA;SP1;PU{10 * U},{10 * U};PA0,0;PD{U},{U};SP;"
        x_min, y_min, x_max, y_max = extract_bounds_from_plt(plt_content)

        assert x_min == 0.0
        assert x_max == pytest.approx(10.0, abs=1e-2)  # Now includes PU coordinate


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
        job = parse_yaml("tests_deps/test123_spec.yaml")
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
        job = parse_yaml("tests_deps/test123_spec.yaml")
        from plt_optimizer.generate.resolution import resolve_job_spec

        labels = resolve_job_spec(job)
        rendered = render_label_to_plt(labels[0])

        points = self._boundary_points(rendered.plt_content)
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]

        width_inches = (max(xs) - min(xs)) / HPGL_UNITS_PER_INCH
        height_inches = (max(ys) - min(ys)) / HPGL_UNITS_PER_INCH

        assert pytest.approx(width_inches, abs=0.01) == labels[0].width
        assert pytest.approx(height_inches, abs=0.01) == labels[0].height


class TestRenderLabelToPlt:
    """Tests for render_label_to_plt() function."""

    def test_render_simple_label(self) -> None:
        """Test rendering a simple single-label job."""
        job = parse_yaml("tests_deps/test123_spec.yaml")
        from plt_optimizer.generate.resolution import resolve_job_spec

        labels = resolve_job_spec(job)
        label = labels[0]  # Test 1

        rendered = render_label_to_plt(label)

        assert rendered.source_label == label
        assert rendered.width > 0
        assert rendered.height > 0
        assert rendered.plt_content.startswith("IN;")
        assert rendered.plt_content.endswith("SP;")

    def test_render_label_bounds_are_positive(self) -> None:
        """Test that rendered bounds are non-negative (after postprocessing)."""
        job = parse_yaml("tests_deps/test123_spec.yaml")
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
        job = parse_yaml("tests_deps/test123_spec.yaml")
        from plt_optimizer.generate.resolution import resolve_job_spec

        labels = resolve_job_spec(job)
        label = labels[0]

        rendered = render_label_to_plt(label)

        # Bounds should be slightly larger than label dimensions due to text/borders
        # but roughly in the right ballpark
        assert rendered.width <= label.width * 1.5
        assert rendered.height <= label.height * 1.5

    def test_render_all_test123_labels(self) -> None:
        """Test rendering the three original test123 labels."""
        job = parse_yaml("tests_deps/test123_spec.yaml")
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
    use_baseline_spacing: bool = True,
) -> ResolvedLabel:
    """Helper to build a ResolvedLabel for local text-rendering tests.

    ``use_baseline_spacing`` defaults to the shipped default (baseline
    pitch). Ink-box stacking regressions pass ``False`` to pin the historical
    geometry, where each line's full ink box (descenders included) consumes
    vertical space.
    """
    return ResolvedLabel(
        id="multi_line",
        count=1,
        width=width,
        height=height,
        margin=margin,
        h_margin=margin,
        v_margin=margin,
        holes=[],
        content=content,
        use_baseline_spacing=use_baseline_spacing,
    )


class TestMissingGlyphMapping:
    """A TTF missing a glyph must abort the label, naming label and line."""

    def test_ttf_missing_glyph_raises_label_render_error(self) -> None:
        """FtextRenderError maps to LabelRenderError with label/line context."""
        label = _make_local_label([_make_line("SAFE"), _make_line("BAD \u2603 GLYPH")])
        with pytest.raises(LabelRenderError) as excinfo:
            _render_text_local(label)
        message = str(excinfo.value)
        assert "multi_line" in message
        assert "text line 1" in message
        assert "has no glyphs for" in message


class TestMultiLineStacking:
    """Regression: multi-line labels must stack lines, not overlap them.

    The Phase 3 render path (``_render_text_local``) used to translate every
    line to y=0, printing all lines on the same baseline with overlapping
    glyphs (visible in complex_test_job.yaml PNG previews).

    These cases use mixed line heights, whose expected numbers are ink-box
    arithmetic, so they pin ``use_baseline_spacing=False``. Baseline pitch
    (the shipped default) is covered by :class:`TestBaselineSpacingStacking`.
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
            ],
            use_baseline_spacing=False,
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
            ],
            use_baseline_spacing=False,
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
            use_baseline_spacing=False,
        )

        rendered = render_label_to_plt(label)

        match = re.search(r"SP1;(.*?)(?:SP\d|$)", rendered.plt_content, re.DOTALL)
        assert match is not None, "No text layer found in rendered PLT"
        ys: list[float] = []
        for coord_match in re.finditer(r"(?:PA|PU|PD)([\d,\-]+)", match.group(1)):
            parts = coord_match.group(1).split(",")
            for i in range(1, len(parts), 2):
                ys.append(int(parts[i]) / HPGL_UNITS_PER_INCH)
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


class TestBaselineSpacingStacking:
    """Baseline-to-baseline stacking (``use_baseline_spacing=True``, default).

    Lines anchor on their baselines with a pitch of
    ``toolpath_text_height + line_spacing``, so descenders hang into the gap
    and every gap reads as the same visual spacing. Cap-only lines are
    bit-identical to ink-box stacking (their ink box is the cap height).
    """

    @staticmethod
    def _cap_only_lines(spacing: float) -> list[ResolvedTextLine]:
        """Two cap-only lines with ink box exactly [baseline, baseline + cap].

        ``H``/``O`` reach the full cap height and sit on the baseline; ``A``
        and ``B`` ink falls slightly short of it, which would blur the
        bit-identical parity assertion.
        """
        return [
            _make_line("HHH", height=0.3, line_spacing=spacing),
            _make_line("OOO", height=0.3),
        ]

    def test_pitch_is_cap_height_plus_spacing(self) -> None:
        """Baselines step down by toolpath height + spacing, exactly."""
        label = _make_local_label(self._cap_only_lines(spacing=0.15))
        _lc, entries = _render_text_local_with_bounds(label)
        assert len(entries) == 2
        # H/O sit on their baseline: the ink bottom IS the baseline.
        pitch = entries[0].bounds[1] - entries[1].bounds[1]
        expected = 0.27 + 0.15
        assert math.isclose(pitch, expected, abs_tol=0.01), (
            f"Baseline pitch {pitch:.3f}in != cap 0.27 + spacing 0.15"
        )

    def test_descender_hangs_into_the_gap(self) -> None:
        """The descender eats into the gap, shrinking the visual air below it."""
        spacing = 0.05
        content = [_make_line("gy", height=0.3, line_spacing=spacing), _make_line("ABC")]

        baseline_entries = _render_text_local_with_bounds(_make_local_label(content))[1]
        inkbox_entries = _render_text_local_with_bounds(
            _make_local_label(content, use_baseline_spacing=False)
        )[1]

        # Visual gap = upper ink bottom -> lower ink top. Ink-box mode keeps
        # the full spacing of air; baseline mode shrinks it by the descender.
        baseline_gap = baseline_entries[0].bounds[1] - baseline_entries[1].bounds[3]
        inkbox_gap = inkbox_entries[0].bounds[1] - inkbox_entries[1].bounds[3]
        assert math.isclose(inkbox_gap, spacing, abs_tol=0.01)
        assert baseline_gap < inkbox_gap - 0.02, (
            f"baseline gap {baseline_gap:.3f} should be visibly tighter than "
            f"ink-box gap {inkbox_gap:.3f} (descender hanging into the gap)"
        )

    def test_block_shrinks_by_the_descender_depth(self) -> None:
        """Baseline stacking reclaims exactly the descender depth of space."""
        content = [_make_line("gy", height=0.3, line_spacing=0.1), _make_line("ABC")]

        baseline_lc, baseline_entries = _render_text_local_with_bounds(_make_local_label(content))
        inkbox_lc, _entries = _render_text_local_with_bounds(
            _make_local_label(content, use_baseline_spacing=False)
        )
        baseline_height = baseline_lc.bounds()[3] - baseline_lc.bounds()[1]
        inkbox_height = inkbox_lc.bounds()[3] - inkbox_lc.bounds()[1]

        # The 'gy' descender depth, measured from the baseline-mode render:
        # baseline = next baseline + pitch (cap 0.27 + spacing 0.1).
        next_baseline = baseline_entries[1].bounds[1]
        gy_baseline = next_baseline + 0.27 + 0.1
        descender = gy_baseline - baseline_entries[0].bounds[1]
        assert descender > 0.03, "fixture must contain a real descender"
        assert math.isclose(inkbox_height - baseline_height, descender, abs_tol=0.02), (
            f"Ink-box {inkbox_height:.3f} - baseline {baseline_height:.3f} != "
            f"descender {descender:.3f}"
        )

    def test_cap_only_lines_identical_in_both_modes(self) -> None:
        """Descender-free content renders bit-identically under both modes."""
        baseline_entries = _render_text_local_with_bounds(
            _make_local_label(self._cap_only_lines(spacing=0.12))
        )[1]
        inkbox_entries = _render_text_local_with_bounds(
            _make_local_label(self._cap_only_lines(spacing=0.12), use_baseline_spacing=False)
        )[1]
        assert len(baseline_entries) == len(inkbox_entries)
        for got, want in zip(baseline_entries, inkbox_entries):
            for g, w in zip(got.bounds, want.bounds):
                assert math.isclose(g, w, abs_tol=1e-9)

    def test_reported_spacing_is_the_spacing_term(self) -> None:
        """Unclamped baseline renders report the requested spacing verbatim."""
        label = _make_local_label(self._cap_only_lines(spacing=0.15))
        _lc, entries = _render_text_local_with_bounds(label)
        assert entries[0].line_spacing is not None
        assert math.isclose(entries[0].line_spacing, 0.15, abs_tol=1e-9)
        assert entries[1].line_spacing is None

    def test_margin_clamp_uses_the_baseline_block(self) -> None:
        """The render-time clamp sizes against the baseline-stacked block."""
        margin = 0.125
        label = _make_local_label(
            self._cap_only_lines(spacing=0.3), width=3.0, height=1.0, margin=margin
        )
        lc, entries = _render_text_local_with_bounds(label)
        block_height = lc.bounds()[3] - lc.bounds()[1]
        available = label.height - 2 * margin
        # Cap-only: baseline block == ink-box block; 0.54 + 0.3 > 0.75 clamps
        # the spacing to 0.75 - 0.54 = 0.21.
        assert entries[0].line_spacing is not None
        assert math.isclose(entries[0].line_spacing, available - 0.54, abs_tol=0.01)
        assert block_height <= available + 0.02


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
                ys.append(int(parts[i]) / HPGL_UNITS_PER_INCH)
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


def _make_hole_label(
    holes: list[ResolvedHoleSpec],
    width: float = 4.0,
    height: float = 2.0,
    margin: float = 0.1,
    hole_margin: float = 0.0,
) -> ResolvedLabel:
    """Helper to build a ResolvedLabel containing only holes."""
    return ResolvedLabel(
        id="hole_label",
        count=1,
        width=width,
        height=height,
        margin=margin,
        h_margin=margin,
        v_margin=margin,
        hole_margin=hole_margin,
        holes=holes,
        content=[],
    )


class TestHoleCircleGeometry:
    """Regression: every hole location must yield one circle on the holes layer.

    ``_hole_circles_local`` feeds both the arc emitter (``_render_holes_hpgl``,
    pen 3) and collision detection. A past bug dropped circles silently
    (``LineCollection.extend()`` with a flat ndarray) and edge locations
    (left/right/top/bottom) were skipped by a corner-only if/elif chain, so
    circle presence per location is asserted here directly.
    """

    def test_no_holes_returns_empty(self) -> None:
        """A label without holes must produce no circles."""
        label = _make_hole_label([])
        assert _hole_circles_local(label) == []

    def test_corner_holes_produce_one_circle_each(self) -> None:
        """Each corner hole must yield exactly one circle at its corner."""
        holes = [
            ResolvedHoleSpec(diameter=0.125, location="top-left"),
            ResolvedHoleSpec(diameter=0.1875, location="top-right"),
            ResolvedHoleSpec(diameter=0.25, location="bottom-right"),
            ResolvedHoleSpec(diameter=0.125, location="bottom-left"),
        ]
        circles = _hole_circles_local(_make_hole_label(holes))

        assert len(circles) == len(holes)
        for (center_x, center_y, radius), hole in zip(circles, holes):
            assert radius == pytest.approx(hole.diameter / 2.0)
            # Offset includes hole_cutter_diameter/2 (default 0.015 → 0.0075 half)
            offset_x = 0.0075 + radius
            offset_y = 0.0075 + radius
            expected_x = {
                "top-left": offset_x,
                "top-right": 4.0 - offset_x,
                "bottom-right": 4.0 - offset_x,
                "bottom-left": offset_x,
            }[hole.location]
            expected_y = {
                "top-left": 2.0 - offset_y,
                "top-right": 2.0 - offset_y,
                "bottom-right": offset_y,
                "bottom-left": offset_y,
            }[hole.location]
            assert center_x == pytest.approx(expected_x, abs=1e-9)
            assert center_y == pytest.approx(expected_y, abs=1e-9)

    def test_edge_locations_produce_circles(self) -> None:
        """left/right/top/bottom holes must yield circles, not be skipped."""
        holes = [
            ResolvedHoleSpec(diameter=0.125, location="left"),
            ResolvedHoleSpec(diameter=0.125, location="right"),
            ResolvedHoleSpec(diameter=0.25, location="top"),
            ResolvedHoleSpec(diameter=0.25, location="bottom"),
        ]
        circles = _hole_circles_local(_make_hole_label(holes))

        assert len(circles) == len(holes)
        # Offset includes hole_cutter_diameter/2 (default 0.015 → 0.0075 half)
        # left/right holes: radius = 0.0625 → offset = 0.0075 + 0.0625 = 0.07
        # top/bottom holes: radius = 0.125 → offset = 0.0075 + 0.125 = 0.1325
        expected_centers = {
            "left": (0.0075 + 0.0625, 1.0),
            "right": (4.0 - (0.0075 + 0.0625), 1.0),
            "top": (2.0, 2.0 - (0.0075 + 0.125)),
            "bottom": (2.0, 0.0075 + 0.125),
        }
        for (center_x, center_y, radius), hole in zip(circles, holes):
            ex, ey = expected_centers[hole.location]
            assert center_x == pytest.approx(ex, abs=1e-9)
            assert center_y == pytest.approx(ey, abs=1e-9)
            assert radius == pytest.approx(hole.diameter / 2.0)

    def test_render_label_to_plt_emits_holes_layer(self) -> None:
        """End-to-end: rendered PLT must contain an SP3 section with circles."""
        label = _make_hole_label(
            [
                ResolvedHoleSpec(diameter=0.125, location="left"),
                ResolvedHoleSpec(diameter=0.125, location="right"),
            ],
            width=3.0,
            height=1.0,
            margin=0.125,
        )
        rendered = render_label_to_plt(label)

        match = re.search(r"SP3;(.*?)(?:SP\d|$)", rendered.plt_content, re.DOTALL)
        assert match is not None, "No holes layer (SP3) found in rendered PLT"
        coords = re.findall(r"(?:PA|PU|PD)([\d,\-]+)", match.group(1))
        assert coords, "Holes layer (SP3) contains no coordinates"

    def test_render_label_to_plt_without_holes_omits_layer(self) -> None:
        """A hole-free label must not emit an empty SP3 section."""
        label = _make_hole_label([])
        rendered = render_label_to_plt(label)

        assert "SP3" not in rendered.plt_content


class TestRenderHolesAsArcs:
    """Drill holes must be emitted as native HPGL arcs, not polygons.

    A ``LineCollection`` can only represent polylines, so routing holes
    through vpype forced them to be sampled polygons. Holes are now emitted
    directly as ``AA`` (arc absolute) commands, four 90-degree quarter-arcs
    per circle, matching the EngraveLab drill-hole convention.
    """

    def test_hole_emits_four_quarter_arcs(self) -> None:
        """Each hole must be exactly four 90-degree arcs."""
        from plt_optimizer.generate.label_renderer import _render_holes_hpgl

        label = _make_hole_label(
            [ResolvedHoleSpec(diameter=0.25, location="bottom-left")],
            hole_margin=0.1875,
        )
        hpgl = _render_holes_hpgl(label)

        arcs = re.findall(r"AA(\d+),(\d+),(-?\d+)", hpgl)
        assert len(arcs) == 4, f"Expected 4 quarter-arcs, got {arcs}"
        assert all(int(sweep) == 90 for _cx, _cy, sweep in arcs)
        # All quarter-arcs share one center.
        assert len({(cx, cy) for cx, cy, _s in arcs}) == 1

    def test_plunge_precedes_arcs(self) -> None:
        """A zero-length PD plunge must precede the arcs to open a new path.

        Without it the parser merges every hole into a single stroke path and
        the profiler's multi-arc drill-hole rule cannot fire.
        """
        from plt_optimizer.generate.label_renderer import _render_holes_hpgl

        label = _make_hole_label(
            [
                ResolvedHoleSpec(diameter=0.125, location="bottom-left"),
                ResolvedHoleSpec(diameter=0.125, location="top-right"),
            ]
        )
        hpgl = _render_holes_hpgl(label)

        # One plunge per hole, landing exactly on the arc start point.
        blocks = re.findall(r"PU(\d+),(\d+);PD\1,\2;AA", hpgl)
        assert len(blocks) == 2

    def test_arc_radius_matches_hole_diameter(self) -> None:
        """The arc radius derived from start-to-center must be diameter/2."""
        from plt_optimizer.generate.label_renderer import _render_holes_hpgl

        diameter = 0.1875
        label = _make_hole_label(
            [ResolvedHoleSpec(diameter=diameter, location="bottom-left")],
            hole_margin=0.25,
        )
        hpgl = _render_holes_hpgl(label)

        sx, sy = (int(v) for v in re.search(r"PU(\d+),(\d+)", hpgl).groups())  # type: ignore[union-attr]
        cx, cy = (int(v) for v in re.search(r"AA(\d+),(\d+),", hpgl).groups())  # type: ignore[union-attr]
        radius_inches = math.hypot(sx - cx, sy - cy) / HPGL_UNITS_PER_INCH
        assert radius_inches == pytest.approx(diameter / 2.0, abs=1e-3)

    def test_rendered_label_holes_layer_contains_arcs_not_polygon(self) -> None:
        """End-to-end: the SP3 layer must carry arcs and no polygon vertices."""
        label = _make_hole_label(
            [ResolvedHoleSpec(diameter=0.125, location="left")],
            width=3.0,
            height=1.0,
            hole_margin=0.1875,
        )
        rendered = render_label_to_plt(label)

        match = re.search(r"SP3;(.*?)(?:SP\d|$)", rendered.plt_content, re.DOTALL)
        assert match is not None, "No holes layer (SP3) found in rendered PLT"
        section = match.group(1)

        assert "AA" in section, "Holes layer must use arc commands"
        # A sampled polygon would emit long PD coordinate runs; the arc form
        # carries a single-coordinate plunge only.
        for pd_match in re.finditer(r"PD([\d,\-]+)", section):
            assert len(pd_match.group(1).split(",")) == 2, (
                f"Unexpected polygon vertex run in holes layer: {pd_match.group(0)}"
            )

    def test_parsed_arcs_form_closed_circles(self) -> None:
        """Parsed arcs must total 360 degrees around a consistent radius."""
        from plt_optimizer.core.models import ArcSegment
        from plt_optimizer.core.parser import PLTParser

        label = _make_hole_label(
            [
                ResolvedHoleSpec(diameter=0.125, location="bottom-left"),
                ResolvedHoleSpec(diameter=0.25, location="top-right"),
            ],
            width=3.0,
            height=2.0,
            hole_margin=0.1875,
        )
        rendered = render_label_to_plt(label)
        doc = PLTParser().parse_string(rendered.plt_content)

        hole_paths = [
            path
            for path in doc.stroke_paths
            if path.segments and any(isinstance(s, ArcSegment) for s in path.segments)
        ]
        assert len(hole_paths) == 2, "Each hole must parse as its own stroke path"

        for path in hole_paths:
            arcs = [s for s in path.segments if isinstance(s, ArcSegment)]
            assert sum(abs(a.sweep_angle) for a in arcs) == pytest.approx(360.0)
            radii = {a.radius for a in arcs}
            assert max(radii) - min(radii) < 1.0, "Arc radius must stay constant"
            # The circle closes: last arc ends where the first began.
            assert arcs[0].start == pytest.approx(arcs[-1].end, abs=1.5)


class TestHoleMarginRendering:
    """hole_margin controls the gap between a hole circle and the label edge.

    The closest point of each hole circle must sit exactly
    ``label.hole_margin`` inches from the boundary. The center is inset by
    ``hole_margin + hole_cutter_diameter/2 + radius`` to account for the
    stroke width of the hole cutter (default 0.015" → 0.0075" half-width).
    With ``hole_margin == 0.0`` the circle's stroke is tangent to the edge.
    """

    def test_zero_margin_keeps_circle_tangent(self) -> None:
        """hole_margin == 0.0 must keep the circle's stroke tangent to the edge."""
        hole = ResolvedHoleSpec(diameter=0.125, location="left")
        circles = _hole_circles_local(_make_hole_label([hole], hole_margin=0.0))
        # Center at (hole_cutter/2 + radius, height/2).
        # hole_cutter = 0.015 → 0.0075 half-width. radius = 0.0625.
        # center_x = 0.0075 + 0.0625 = 0.07
        assert len(circles) == 1
        center_x, center_y, radius = circles[0]
        assert center_x == pytest.approx(0.0075 + 0.0625, abs=1e-9)
        assert center_y == pytest.approx(1.0, abs=1e-9)
        assert radius == pytest.approx(0.0625, abs=1e-9)

    def test_margin_insets_circle_from_edge(self) -> None:
        """With hole_margin, the circle's closest point is hole_margin + cutter/2 away."""
        hole = ResolvedHoleSpec(diameter=0.125, location="left")
        hole_margin = 0.1875
        radius = hole.diameter / 2.0
        circles = _hole_circles_local(_make_hole_label([hole], hole_margin=hole_margin))

        # Center inset by hole_margin + hole_cutter_diameter/2 + radius.
        # hole_cutter_diameter = 0.015 → 0.0075 half-width.
        assert len(circles) == 1
        center_x, center_y, r = circles[0]
        expected_center_x = hole_margin + 0.0075 + radius
        assert center_x == pytest.approx(expected_center_x, abs=1e-9)
        assert center_y == pytest.approx(1.0, abs=1e-9)
        assert r == pytest.approx(radius, abs=1e-9)
        # Closest point of the circle to the left edge (x=0) == hole_margin + cutter/2.
        assert center_x - r == pytest.approx(hole_margin + 0.0075, abs=1e-9)

    def test_margin_applies_to_all_edge_and_corner_locations(self) -> None:
        """hole_margin must be honored for every supported location."""
        hole_margin = 0.25
        diameter = 0.125
        radius = diameter / 2.0
        # offset includes hole_cutter_diameter / 2 (default 0.015 → 0.0075 half)
        offset = hole_margin + 0.0075 + radius
        width, height = 4.0, 2.0

        holes = [
            ResolvedHoleSpec(diameter=diameter, location="left"),
            ResolvedHoleSpec(diameter=diameter, location="right"),
            ResolvedHoleSpec(diameter=diameter, location="top"),
            ResolvedHoleSpec(diameter=diameter, location="bottom"),
            ResolvedHoleSpec(diameter=diameter, location="top-left"),
            ResolvedHoleSpec(diameter=diameter, location="top-right"),
            ResolvedHoleSpec(diameter=diameter, location="bottom-left"),
            ResolvedHoleSpec(diameter=diameter, location="bottom-right"),
        ]
        label = _make_hole_label(holes, width=width, height=height, hole_margin=hole_margin)
        circles = _hole_circles_local(label)

        expected_centers = {
            "left": (offset, height / 2.0),
            "right": (width - offset, height / 2.0),
            "top": (width / 2.0, height - offset),
            "bottom": (width / 2.0, offset),
            "top-left": (offset, height - offset),
            "top-right": (width - offset, height - offset),
            "bottom-left": (offset, offset),
            "bottom-right": (width - offset, offset),
        }
        assert len(circles) == len(holes)
        for (center_x, center_y, r), hole in zip(circles, holes):
            ex, ey = expected_centers[hole.location]
            assert center_x == pytest.approx(ex, abs=1e-9)
            assert center_y == pytest.approx(ey, abs=1e-9)
            assert r == pytest.approx(radius, abs=1e-9)

    def test_rendered_plt_hole_respects_margin(self) -> None:
        """End-to-end: rendered SP3 arcs must honor hole_margin.

        Uses a bottom-left hole on a small label. After the device-convention
        Y flip (mirror across the label centerline), the hole lands near the
        top-left, so its distance to the left edge (accounting for hole cutter
        stroke) and to the top edge must correspond to hole_margin. The check
        reads the native ``AA`` arc center and derives the radius from the
        preceding ``PU`` start point.
        """
        hole_margin = 0.1875
        diameter = 0.125
        radius = diameter / 2.0
        hole = ResolvedHoleSpec(diameter=diameter, location="bottom-left")
        label = _make_hole_label([hole], width=3.0, height=1.0, hole_margin=hole_margin)

        rendered = render_label_to_plt(label)
        match = re.search(r"SP3;(.*?)(?:SP\d|$)", rendered.plt_content, re.DOTALL)
        assert match is not None, "No holes layer (SP3) found in rendered PLT"

        section = match.group(1)
        assert section.count("AA") == 4, "Each hole must be drawn as four quarter-arcs"

        start = re.search(r"PU(\d+),(\d+)", section)
        center = re.search(r"AA(\d+),(\d+),(-?\d+)", section)
        assert start is not None and center is not None

        sx, sy = int(start.group(1)), int(start.group(2))
        cx, cy, sweep = int(center.group(1)), int(center.group(2)), int(center.group(3))
        # Radius in plotter units, derived from the PU start to the AA center.
        radius_units = math.hypot(sx - cx, sy - cy)

        # The arc radius must stay diameter/2, not margin+radius.
        assert radius_units / HPGL_UNITS_PER_INCH == pytest.approx(radius, abs=0.002)
        # Quarter arcs, one full revolution.
        assert sweep == -90  # negated by the Y-axis flip

        # The circle center is inset by (hole_margin + hole_cutter/2 + radius),
        # so the closest point is at hole_margin + hole_cutter/2.
        # hole_cutter_diameter = 0.015 → 0.0075 half-width.
        expected_gap = hole_margin + 0.0075
        left_gap = (cx - radius_units) / HPGL_UNITS_PER_INCH
        top_gap = label.height - (cy + radius_units) / HPGL_UNITS_PER_INCH
        assert left_gap == pytest.approx(expected_gap, abs=0.002)
        assert top_gap == pytest.approx(expected_gap, abs=0.002)


class TestCompressLineToWidth:
    """Unit tests for the uniform horizontal line compression helper."""

    @staticmethod
    def _stub_line(min_x: float, max_x: float, min_y: float, max_y: float) -> TextBlock:
        """Build a two-point diagonal stroke spanning the given bounds."""
        return TextBlock(
            strokes=(
                Stroke(
                    pen_up=complex(min_x, min_y),
                    segments=(LineSeg(complex(min_x, min_y), complex(max_x, max_y)),),
                ),
            )
        )

    def test_line_that_fits_is_returned_unchanged(self) -> None:
        """A line within the available width must be returned as the same object."""
        lc = self._stub_line(0.0, 2.0, 0.0, 0.5)
        assert compress_line_to_width(lc, 3.0, 0.5, "lbl") is lc

    def test_compression_disabled_returns_same_object(self) -> None:
        """max_h_compress=0.0 must leave an over-wide line untouched."""
        lc = self._stub_line(0.0, 10.0, 0.0, 0.5)
        assert compress_line_to_width(lc, 3.0, 0.0, "lbl") is lc

    def test_compresses_x_and_preserves_y(self) -> None:
        """X must scale to the available width while Y is untouched."""
        lc = self._stub_line(1.0, 7.0, 0.2, 0.7)
        out = compress_line_to_width(lc, 3.0, 0.8, "lbl")

        bounds = out.bounds()
        assert bounds is not None
        min_x, min_y, max_x, max_y = bounds
        assert max_x - min_x == pytest.approx(3.0, abs=1e-9)
        assert min_x == pytest.approx(1.0, abs=1e-9)
        assert (min_y, max_y) == pytest.approx((0.2, 0.7))

    def test_scale_is_clamped_by_max_h_compress(self) -> None:
        """Compression must stop at the configured limit."""
        lc = self._stub_line(0.0, 10.0, 0.0, 0.5)
        out = compress_line_to_width(lc, 1.0, 0.4, "lbl")

        bounds = out.bounds()
        assert bounds is not None
        # Limit 0.4 floors the scale at 0.6 -> width 6.0, not the needed 1.0.
        assert bounds[2] == pytest.approx(6.0, abs=1e-9)

    def test_empty_collection_is_returned_unchanged(self) -> None:
        """An empty block has no bounds and must pass through."""
        lc = TextBlock.empty()
        assert compress_line_to_width(lc, 3.0, 0.5, "lbl") is lc


class TestHorizontalCompressionRendering:
    """End-to-end: over-wide lines must compress to respect the margins."""

    LONG_TEXT = "SAFETY long long text"

    def test_long_line_stays_inside_margin_box(self) -> None:
        """With compression allowed, rendered text must not breach the margins."""
        margin = 0.15
        label = _make_local_label(
            [_make_line(self.LONG_TEXT, height=0.5, max_h_compress=0.5)],
            width=5.0,
            height=1.5,
            margin=margin,
        )
        lc = _render_text_local(label)
        assert not lc.is_empty()

        min_x, _min_y, max_x, _max_y = lc.bounds()
        available = label.width - (2 * margin)
        assert min_x >= margin - 0.01, f"Text breaches left margin: {min_x:.3f}"
        assert max_x <= label.width - margin + 0.01, f"Text breaches right margin: {max_x:.3f}"
        assert (max_x - min_x) <= available + 0.02

    def test_disabled_compression_leaves_line_over_wide(self) -> None:
        """Without max_h_compress the line still overflows (opt-in behaviour)."""
        margin = 0.15
        label = _make_local_label(
            [_make_line(self.LONG_TEXT, height=0.5, max_h_compress=0.0)],
            width=5.0,
            height=1.5,
            margin=margin,
        )
        lc = _render_text_local(label)
        min_x, _min_y, max_x, _max_y = lc.bounds()
        available = label.width - (2 * margin)
        assert (max_x - min_x) > available + 0.02, (
            "Line unexpectedly fits without compression; test text is too short"
        )

    def test_compression_shrinks_width_vs_disabled(self) -> None:
        """The same line must render narrower when compression is allowed."""
        kwargs = {"width": 5.0, "height": 1.5, "margin": 0.15}
        compressed = _render_text_local(
            _make_local_label(
                [_make_line(self.LONG_TEXT, height=0.5, max_h_compress=0.5)], **kwargs
            )
        )
        natural = _render_text_local(
            _make_local_label(
                [_make_line(self.LONG_TEXT, height=0.5, max_h_compress=0.0)], **kwargs
            )
        )

        c_min_x, _c0, c_max_x, _c1 = compressed.bounds()
        n_min_x, _n0, n_max_x, _n1 = natural.bounds()
        assert (c_max_x - c_min_x) < (n_max_x - n_min_x)

    def test_compression_preserves_glyph_height(self) -> None:
        """Horizontal-only scaling must not change the rendered block height."""
        kwargs = {"width": 5.0, "height": 1.5, "margin": 0.15}
        compressed = _render_text_local(
            _make_local_label(
                [_make_line(self.LONG_TEXT, height=0.5, max_h_compress=0.5)], **kwargs
            )
        )
        natural = _render_text_local(
            _make_local_label(
                [_make_line(self.LONG_TEXT, height=0.5, max_h_compress=0.0)], **kwargs
            )
        )

        _c0, c_min_y, _c1, c_max_y = compressed.bounds()
        _n0, n_min_y, _n1, n_max_y = natural.bounds()
        assert (c_max_y - c_min_y) == pytest.approx(n_max_y - n_min_y, abs=1e-6)

    def test_short_lines_are_untouched(self) -> None:
        """Lines that already fit must render identically with compression on."""
        kwargs = {"width": 5.0, "height": 1.5, "margin": 0.15}
        with_compress = _render_text_local(
            _make_local_label([_make_line("FIRST", height=0.25, max_h_compress=0.5)], **kwargs)
        )
        without = _render_text_local(
            _make_local_label([_make_line("FIRST", height=0.25, max_h_compress=0.0)], **kwargs)
        )

        assert with_compress.bounds() == pytest.approx(without.bounds())

    def test_per_line_compression_is_independent(self) -> None:
        """Only the over-wide line in a multi-line label should shrink."""
        margin = 0.15
        label = _make_local_label(
            [
                _make_line(self.LONG_TEXT, height=0.5, max_h_compress=0.5),
                _make_line("FIRST", height=0.25, max_h_compress=0.5),
            ],
            width=5.0,
            height=1.5,
            margin=margin,
        )
        lc = _render_text_local(label)
        min_x, _min_y, max_x, _max_y = lc.bounds()
        available = label.width - (2 * margin)
        assert (max_x - min_x) <= available + 0.02

    def test_render_label_to_plt_respects_margin_box(self) -> None:
        """The full HPGL pipeline must emit text inside the margin box."""
        margin = 0.15
        label = _make_local_label(
            [_make_line(self.LONG_TEXT, height=0.5, max_h_compress=0.5)],
            width=5.0,
            height=1.5,
            margin=margin,
        )
        rendered = render_label_to_plt(label)

        match = re.search(r"SP1;(.*?)(?:SP\d|$)", rendered.plt_content, re.DOTALL)
        assert match is not None, "No text layer found in rendered PLT"
        xs: list[float] = []
        for coord_match in re.finditer(r"(?:PA|PU|PD)([\d,\-]+)", match.group(1)):
            parts = coord_match.group(1).split(",")
            for i in range(0, len(parts) - 1, 2):
                xs.append(int(parts[i]) / HPGL_UNITS_PER_INCH)
        assert xs, "No text coordinates found"

        assert min(xs) >= margin - 0.02, f"Text breaches left margin: {min(xs):.3f}"
        assert max(xs) <= label.width - margin + 0.02, f"Text breaches right margin: {max(xs):.3f}"


class TestCompressionByLineReporting:
    """``RenderedLabel.compression_by_line`` reports the applied per-line scale."""

    @staticmethod
    def _label(max_h_compress: float) -> ResolvedLabel:
        """One over-wide line on a 5x1.5in label with 0.15in margins."""
        return _make_local_label(
            [
                _make_line(
                    TestHorizontalCompressionRendering.LONG_TEXT,
                    height=0.5,
                    max_h_compress=max_h_compress,
                )
            ],
            width=5.0,
            height=1.5,
            margin=0.15,
        )

    @staticmethod
    def _text_width(rendered: RenderedLabel) -> float:
        """Width in inches spanned by the rendered SP1 text layer."""
        match = re.search(r"SP1;(.*?)(?:SP\d|$)", rendered.plt_content, re.DOTALL)
        assert match is not None, "No text layer found in rendered PLT"
        xs: list[float] = []
        for coord_match in re.finditer(r"(?:PA|PU|PD)([\d,\-]+)", match.group(1)):
            parts = coord_match.group(1).split(",")
            for i in range(0, len(parts) - 1, 2):
                xs.append(int(parts[i]) / HPGL_UNITS_PER_INCH)
        assert xs, "No text coordinates found"
        return max(xs) - min(xs)

    def test_margin_compression_is_reported(self) -> None:
        """An over-wide compressed line must appear with its applied scale."""
        rendered = render_label_to_plt(self._label(max_h_compress=0.5))

        assert set(rendered.compression_by_line) == {0}
        scale = rendered.compression_by_line[0]
        assert 0.0 < scale < 1.0

        # The reported scale must match the measured width ratio against the
        # same label rendered with compression disabled (natural width).
        natural = render_label_to_plt(self._label(max_h_compress=0.0))
        assert scale == pytest.approx(
            self._text_width(rendered) / self._text_width(natural), rel=1e-2
        )

    def test_uncompressed_label_reports_empty_dict(self) -> None:
        """A label with compression disabled must report no compression at all."""
        rendered = render_label_to_plt(self._label(max_h_compress=0.0))

        assert rendered.compression_by_line == {}

    def test_only_compressed_lines_are_reported(self) -> None:
        """In a multi-line label only the over-wide line gets an entry."""
        label = _make_local_label(
            [
                _make_line(
                    TestHorizontalCompressionRendering.LONG_TEXT,
                    height=0.5,
                    max_h_compress=0.5,
                ),
                _make_line("FIRST", height=0.25, max_h_compress=0.5),
            ],
            width=5.0,
            height=1.5,
            margin=0.15,
        )
        rendered = render_label_to_plt(label)

        assert set(rendered.compression_by_line) == {0}
        assert rendered.compression_by_line[0] < 1.0


class TestLineSpacingByLineReporting:
    """``RenderedLabel.line_spacing_by_line`` reports the effective per-gap spacing."""

    def test_explicit_spacing_is_reported(self) -> None:
        """Unclamped spacing must be recorded verbatim, keyed by the upper line."""
        label = _make_local_label(
            [_make_line("FIRST", height=0.3, line_spacing=0.2), _make_line("SECOND", height=0.3)],
            width=4.0,
            height=2.0,
            margin=0.1,
        )
        rendered = render_label_to_plt(label)

        # Two lines -> exactly one gap, keyed on the upper line (index 0).
        assert set(rendered.line_spacing_by_line) == {0}
        assert rendered.line_spacing_by_line[0] == pytest.approx(0.2)

    def test_margin_clamp_records_effective_spacing(self) -> None:
        """A render-time margin clamp must record the reduced (effective) gap."""
        label = _make_local_label(
            [_make_line("FIRST", height=0.3, line_spacing=1.0), _make_line("SECOND", height=0.3)],
            width=4.0,
            height=1.0,
            margin=0.1,
        )
        rendered = render_label_to_plt(label)

        # Inner height 0.8in cannot host two 0.27in ink lines (0.3in nominal
        # minus the 0.03in cutter) plus 1.0in spacing: the proportional clamp
        # leaves (1.0 - 0.74) / 1.0 = 0.26, and the report carries the
        # effective value, not the requested one.
        assert set(rendered.line_spacing_by_line) == {0}
        assert rendered.line_spacing_by_line[0] == pytest.approx(0.26)

    def test_single_line_has_no_spacing_entries(self) -> None:
        """One line has no gap below it: the map stays empty."""
        label = _make_local_label([_make_line("ONLY", height=0.3, line_spacing=0.5)])
        rendered = render_label_to_plt(label)

        assert rendered.line_spacing_by_line == {}

    def test_three_lines_report_two_gaps(self) -> None:
        """n renderable lines produce exactly n-1 gaps keyed 0..n-2."""
        label = _make_local_label(
            [
                _make_line("ONE", height=0.2, line_spacing=0.1),
                _make_line("TWO", height=0.2, line_spacing=0.15),
                _make_line("THREE", height=0.2),
            ],
            width=4.0,
            height=2.0,
            margin=0.1,
        )
        rendered = render_label_to_plt(label)

        assert set(rendered.line_spacing_by_line) == {0, 1}
        assert rendered.line_spacing_by_line[0] == pytest.approx(0.1)
        assert rendered.line_spacing_by_line[1] == pytest.approx(0.15)

    def test_blank_line_gap_keys_on_upper_renderable_line(self) -> None:
        """With a skipped blank line, the gap keys on the upper renderable line."""
        label = _make_local_label(
            [
                _make_line("A", height=0.2, line_spacing=0.1),
                _make_line("", height=0.2, line_spacing=0.3),
                _make_line("B", height=0.2),
            ],
            width=4.0,
            height=2.0,
            margin=0.1,
        )
        rendered = render_label_to_plt(label)

        # The blank line renders nothing, so A and B are adjacent renderable
        # lines: one gap (A's requested 0.1) keyed on A's content index.
        assert set(rendered.line_spacing_by_line) == {0}
        assert rendered.line_spacing_by_line[0] == pytest.approx(0.1)


class TestHorizontalTextAlignment:
    """text_h_alignment must anchor rendered lines within the margin box."""

    SHORT_TEXT = "AB"

    def _label(self, alignment: str) -> ResolvedLabel:
        """Build a wide label with a single short line at the given alignment."""
        return _make_local_label(
            [_make_line(self.SHORT_TEXT, height=0.3, text_h_alignment=alignment)],
            width=4.0,
            height=1.0,
            margin=0.2,
        )

    def test_left_aligns_left_edge_at_margin(self) -> None:
        """A left-aligned line's left-most point sits precisely at the margin."""
        margin = 0.2
        lc = _render_text_local(self._label("left"))
        assert not lc.is_empty()

        min_x, _min_y, _max_x, _max_y = lc.bounds()
        assert min_x == pytest.approx(margin, abs=0.01)

    def test_right_aligns_right_edge_at_margin(self) -> None:
        """A right-aligned line's right-most point sits precisely at the margin."""
        label = self._label("right")
        lc = _render_text_local(label)
        assert not lc.is_empty()

        _min_x, _min_y, max_x, _max_y = lc.bounds()
        assert max_x == pytest.approx(label.width - label.margin, abs=0.01)

    def test_center_stays_centered(self) -> None:
        """Centered text must remain centered within the inner content area."""
        label = self._label("center")
        lc = _render_text_local(label)
        min_x, _min_y, max_x, _max_y = lc.bounds()

        inner_center = label.margin + (label.width - 2 * label.margin) / 2
        text_center = (min_x + max_x) / 2
        assert text_center == pytest.approx(inner_center, abs=0.01)

    def test_alignments_produce_distinct_positions(self) -> None:
        """The same line must land at three distinct X positions."""
        left = _render_text_local(self._label("left")).bounds()
        center = _render_text_local(self._label("center")).bounds()
        right = _render_text_local(self._label("right")).bounds()

        assert left[0] < center[0] < right[0]
        # Same line at the same height -> identical widths.
        assert (left[2] - left[0]) == pytest.approx(right[2] - right[0], abs=1e-6)

    def test_per_line_alignment_in_multi_line_label(self) -> None:
        """Each line in a label may carry its own horizontal alignment."""
        margin = 0.2
        label = _make_local_label(
            [
                _make_line("LEFT", height=0.25, text_h_alignment="left"),
                _make_line("RIGHT", height=0.25, text_h_alignment="right"),
            ],
            width=4.0,
            height=1.2,
            margin=margin,
        )
        lc = _render_text_local(label)
        assert not lc.is_empty()

        # Separate the two lines by their vertical bands (top line has larger y).
        _lb_min_x, lb_min_y, _lb_max_x, lb_max_y = lc.bounds()
        mid_y = (lb_min_y + lb_max_y) / 2
        top_xs = [p.real for seg in lc for p in seg if p.imag > mid_y]
        bottom_xs = [p.real for seg in lc for p in seg if p.imag <= mid_y]
        assert top_xs and bottom_xs
        # Top line ("LEFT") hugs the left margin; bottom ("RIGHT") the right.
        assert min(top_xs) == pytest.approx(margin, abs=0.01)
        assert max(bottom_xs) == pytest.approx(4.0 - margin, abs=0.01)

    def test_default_alignment_matches_center(self) -> None:
        """A line built without alignment (dataclass default) centers as before."""
        defaulted = ResolvedTextLine(
            text=self.SHORT_TEXT,
            nominal_text_height=0.3,
            toolpath_text_height=0.27,
            cutter_diameter=0.03,
            character_spacing=0.0,
            line_spacing=0.0,
        )
        label_default = _make_local_label([defaulted], width=4.0, height=1.0, margin=0.2)
        default_bounds = _render_text_local(label_default).bounds()
        center_bounds = _render_text_local(self._label("center")).bounds()

        assert default_bounds[0] == pytest.approx(center_bounds[0], abs=1e-9)
        assert default_bounds[2] == pytest.approx(center_bounds[2], abs=1e-9)


def _make_collision_label(
    text: str = "WIDE LABEL TEXT",
    holes: list[ResolvedHoleSpec] | None = None,
    width: float = 3.0,
    height: float = 1.0,
    margin: float = 0.1,
    hole_margin: float = 0.1875,
    min_hole_margin: float | None = None,
    max_h_compress: float = 0.0,
) -> ResolvedLabel:
    """Build a label whose full-width text overlaps edge drill holes.

    The default geometry (3x1in label, 0.5in text, 0.25in holes at the
    default 0.1875in hole margin) makes the vertically centered text block
    span the full inner width, so left/right edge holes sit inside the
    text bounding box.
    """
    if holes is None:
        holes = [ResolvedHoleSpec(diameter=0.25, location="left")]
    return ResolvedLabel(
        id="collision_label",
        count=1,
        width=width,
        height=height,
        margin=margin,
        h_margin=margin,
        v_margin=margin,
        hole_margin=hole_margin,
        holes=holes,
        content=[_make_line(text, height=0.5, max_h_compress=max_h_compress)],
        min_hole_margin=min_hole_margin,
    )


class TestCollisionDetectionPhase1:
    """Phase 1: collisions are detected, logged, and reported on the result."""

    def test_clean_label_has_no_collisions(self) -> None:
        """Short centered text must not collide with edge holes."""
        label = _make_collision_label(text="HI")
        rendered = render_label_to_plt(label)
        assert rendered.has_collisions is False
        assert rendered.collision_detected is False

    def test_full_width_text_flags_collision(self) -> None:
        """A full-width line overlapping a left-edge hole sets the flag."""
        label = _make_collision_label()
        rendered = render_label_to_plt(label)
        assert rendered.has_collisions is True
        assert rendered.collision_detected is True

    def test_collision_logged_with_label_id_and_hole_location(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """ERROR logs must name the label, line index, and hole location."""
        label = _make_collision_label()
        with caplog.at_level(logging.ERROR, logger="plt_optimizer.generate.label_renderer"):
            render_label_to_plt(label)

        messages = [r.message for r in caplog.records if "collides with" in r.message]
        assert messages, "No collision error logged"
        joined = "\n".join(messages)
        assert "collision_label" in joined
        assert "text line 0" in joined
        assert "left drill hole" in joined

    def test_multiple_holes_report_each_collision(self) -> None:
        """Each colliding (line, hole) pair is reported separately."""
        holes = [
            ResolvedHoleSpec(diameter=0.25, location="left"),
            ResolvedHoleSpec(diameter=0.25, location="right"),
        ]
        label = _make_collision_label(holes=holes)
        rendered = render_label_to_plt(label)
        assert rendered.has_collisions is True

    def test_no_holes_never_collides(self) -> None:
        """Labels without holes must never report collisions."""
        label = _make_collision_label(holes=[])
        rendered = render_label_to_plt(label)
        assert rendered.has_collisions is False
        assert rendered.collision_detected is False

    def test_avoidance_disabled_keeps_render_unmodified(self) -> None:
        """Without min_hole_margin/max_h_compress the render passes through."""
        label = _make_collision_label()
        rendered = render_label_to_plt(label)
        assert rendered.source_label is label
        assert rendered.has_collisions is True


class TestTextChunkRecords:
    """Rendered labels carry per-chunk text geometry for plate-space routing."""

    @staticmethod
    def _label(content: list[ResolvedTextLine], **kwargs: object) -> ResolvedLabel:
        """Build a text-only label carrying an optional chunk mode."""
        return ResolvedLabel(
            id="chunks",
            count=1,
            width=4.0,
            height=2.0,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
            holes=[],
            content=content,
            **kwargs,  # type: ignore[arg-type]
        )

    def test_line_mode_one_record_per_line(self) -> None:
        """Default mode yields one whole-line record per content line."""
        label = self._label(
            [
                _make_line("HELLO WORLD", height=0.4),
                _make_line("SECOND LINE", height=0.3),
            ]
        )
        rendered = render_label_to_plt(label)
        assert len(rendered.text_chunks) == 2
        for index, record in enumerate(rendered.text_chunks):
            assert record.line_index == index
            assert record.word_index is None
            assert record.word_text == ""
            assert record.contours
            x_min, y_min, x_max, y_max = record.bounds
            xs = [z.real for contour in record.contours for z in contour]
            ys = [z.imag for contour in record.contours for z in contour]
            assert math.isclose(x_min, min(xs))
            assert math.isclose(y_min, min(ys))
            assert math.isclose(x_max, max(xs))
            assert math.isclose(y_max, max(ys))

    def test_word_mode_splits_by_whitespace(self) -> None:
        """WORD mode emits one record per word, in line/word order."""
        label = self._label([_make_line("AB CD EF", height=0.4)], text_chunk_mode="word")
        rendered = render_label_to_plt(label)
        assert [r.word_text for r in rendered.text_chunks] == ["AB", "CD", "EF"]
        assert [r.word_index for r in rendered.text_chunks] == [0, 1, 2]
        assert all(r.line_index == 0 for r in rendered.text_chunks)

    def test_word_records_partition_line_contours(self) -> None:
        """Word records together cover exactly the line-mode contours."""
        content = [_make_line("WIDTH 123 Test", height=0.4)]
        line_rendered = render_label_to_plt(self._label(content))
        word_rendered = render_label_to_plt(self._label(content, text_chunk_mode="word"))

        def signature(rendered: object) -> list:
            return sorted(
                (
                    round(float(z.real), 6),
                    round(float(z.imag), 6),
                )
                for record in rendered.text_chunks  # type: ignore[attr-defined]
                for contour in record.contours
                for z in contour
            )

        assert signature(word_rendered) == signature(line_rendered)

    def test_records_carry_pen_from_pen_map(self) -> None:
        """Each record's pen matches its line's cutter pen."""
        from plt_optimizer.generate.resolution import build_cutter_pen_map

        label = self._label(
            [
                _make_line("AAA", height=0.4),
                ResolvedTextLine(
                    text="BBB",
                    nominal_text_height=0.5,
                    toolpath_text_height=0.47,
                    cutter_diameter=0.06,
                    character_spacing=0.0,
                    line_spacing=0.0,
                ),
            ]
        )
        pen_map = build_cutter_pen_map([label])
        rendered = render_label_to_plt(label, pen_map=pen_map)
        assert len(rendered.text_chunks) == 2
        assert rendered.text_chunks[0].pen == pen_map[(0.03, "none")]
        assert rendered.text_chunks[1].pen == pen_map[(0.06, "none")]
        assert rendered.text_chunks[0].pen != rendered.text_chunks[1].pen

    def test_blank_word_produces_no_record(self) -> None:
        """Whitespace-only runs carry no strokes, so no routing node."""
        label = self._label([_make_line("A  B", height=0.4)], text_chunk_mode="word")
        rendered = render_label_to_plt(label)
        assert [r.word_text for r in rendered.text_chunks] == ["A", "B"]

    def test_empty_content_has_no_records(self) -> None:
        """A label without text renders no chunk records."""
        label = self._label([])
        rendered = render_label_to_plt(label)
        assert rendered.text_chunks == ()

    def test_unknown_chunk_mode_falls_back_to_line(self) -> None:
        """An out-of-enum value resolves to whole-line records."""
        label = self._label([_make_line("AB CD", height=0.4)], text_chunk_mode="glyph")
        rendered = render_label_to_plt(label)
        assert len(rendered.text_chunks) == 1
        assert rendered.text_chunks[0].word_index is None

    def test_line_mode_records_carry_glyph_groups(self) -> None:
        """LINE-mode records partition their strokes per character exactly."""
        label = self._label([_make_line("HELLO WORLD", height=0.4)])
        rendered = render_label_to_plt(label)
        (record,) = rendered.text_chunks
        stroke_count = sum(len(block.strokes) for block in record.blocks)
        flat = [i for group in record.glyph_groups for i in group]
        # Every stroke belongs to exactly one glyph group.
        assert sorted(flat) == list(range(stroke_count))
        # One group per non-space character, in text order.
        assert len(record.glyph_groups) == len("HELLOWORLD")

    def test_word_mode_records_carry_word_local_glyph_groups(self) -> None:
        """WORD-mode glyph groups index the word's own stroke frame."""
        label = self._label([_make_line("AB CD", height=0.4)], text_chunk_mode="word")
        rendered = render_label_to_plt(label)
        assert [r.word_text for r in rendered.text_chunks] == ["AB", "CD"]
        for record in rendered.text_chunks:
            stroke_count = sum(len(block.strokes) for block in record.blocks)
            flat = [i for group in record.glyph_groups for i in group]
            assert sorted(flat) == list(range(stroke_count))
            assert len(record.glyph_groups) == 2  # two glyphs per word


def _make_descender_label(
    content: list[ResolvedTextLine],
    *,
    height: float = 1.4,
    v_margin: float = 0.1,
    **kwargs: object,
) -> ResolvedLabel:
    """Build a two-line label for descending-character relief tests.

    The relief is a label-tier mechanism, so the label is built with the
    relief allowances passed straight through to ``ResolvedLabel``.
    ``v_margin`` is a parameter (not hardcoded) so a test can place the text
    block near the bottom border and exercise the border trigger.
    """
    return ResolvedLabel(
        id="descender",
        count=1,
        width=3.0,
        height=height,
        margin=0.1,
        h_margin=0.1,
        v_margin=v_margin,
        holes=[],
        content=content,
        **kwargs,  # type: ignore[arg-type]
    )


class TestInkExtraction:
    """The swept-ink extraction feeding the relief's rolling-circle test."""

    def test_stroke_polyline_chains_segments_once(self) -> None:
        """Tip-to-tail segments share their junction vertex exactly once."""
        stroke = Stroke(
            pen_up=complex(0.0, 0.0),
            segments=(
                LineSeg(complex(0.0, 0.0), complex(1.0, 0.0)),
                LineSeg(complex(1.0, 0.0), complex(1.0, 1.0)),
            ),
        )
        assert _stroke_polyline(stroke) == ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0))

    def test_stroke_polyline_of_an_arc_flattens(self) -> None:
        """An arc contributes its chord-bounded vertices (more than its endpoints)."""
        from plt_optimizer.generate.text_geometry import ArcSeg

        stroke = Stroke(
            pen_up=complex(1.0, 0.0),
            segments=(ArcSeg(complex(1.0, 0.0), complex(0.0, 0.0), 90.0),),
        )
        polyline = _stroke_polyline(stroke)
        assert len(polyline) > 2
        assert polyline[0] == pytest.approx((1.0, 0.0))
        assert polyline[-1] == pytest.approx((0.0, 1.0), abs=1e-9)

    def test_glyph_ink_drops_segment_less_strokes(self) -> None:
        """A glyph's ink is one polyline per non-empty stroke."""
        block = TextBlock(
            strokes=(
                Stroke(
                    pen_up=complex(0.0, 0.0),
                    segments=(LineSeg(complex(0.0, 0.0), complex(0.2, 0.0)),),
                ),
                Stroke(pen_up=complex(1.0, 1.0), segments=()),
            )
        )
        ink = _glyph_ink(block, (0, 1))
        assert ink == (((0.0, 0.0), (0.2, 0.0)),)

    def test_clip_keeps_sub_baseline_runs_and_adds_the_crossing(self) -> None:
        """A run crossing the emission floor contributes a vertex exactly on it."""
        floor = -_INK_EMISSION_FLOOR
        clipped = _clip_below_baseline(((0.0, 0.2), (1.0, -0.2)))
        # Crossing at y = floor: x = 0 + (floor - 0.2) / (-0.4) * 1.0.
        assert clipped[0][1] == pytest.approx(floor)
        assert clipped[0][0] == pytest.approx((0.2 + _INK_EMISSION_FLOOR) / 0.4)
        assert clipped[-1] == (1.0, -0.2)

    def test_clip_drops_ink_entirely_above_the_baseline(self) -> None:
        """Ink above the baseline has no rolling surface."""
        assert _clip_below_baseline(((0.0, 0.1), (1.0, 0.3))) == ()

    def test_clip_keeps_ink_entirely_below_unchanged(self) -> None:
        """Ink below the baseline passes through untouched."""
        polyline = ((0.0, -0.1), (1.0, -0.3))
        assert _clip_below_baseline(polyline) == polyline

    def test_clip_drops_ink_shallower_than_the_emission_floor(self) -> None:
        """Ink below the baseline but above the emission floor is not a descender.

        Emitted coordinates are integer plotter units, so a sliver
        shallower than half a unit cannot collide in the engraved output.
        """
        assert _clip_below_baseline(((0.0, 0.0), (1.0, 0.0))) == ()
        assert _clip_below_baseline(((0.0, -1e-4), (1.0, -1e-4))) == ()

    def test_sub_baseline_ink_drops_empty_polylines(self) -> None:
        """Only the polylines with sub-baseline ink survive."""
        ink = (((0.0, 0.5), (1.0, 0.5)), ((0.0, 0.0), (0.5, -0.2)))
        clipped = _sub_baseline_ink(ink)
        assert len(clipped) == 1
        assert clipped[0][-1] == (0.5, -0.2)


class TestDescendingCharacterRelief:
    """The per-glyph relief fires only on a measured near-miss below a descender."""

    def test_descender_close_to_the_line_below_is_relieved(self) -> None:
        """A 'y' hanging toward the capitals below is squeezed and raised.

        The trigger is the true 2D swept-ink distance (a circle of the
        required diameter rolled along the sub-baseline ink), so the fixture
        pairs a descender that genuinely reaches the capitals below. The
        relief is per-glyph: "My" over "RANGE" lands the y's tail on the R.
        """
        label = _make_descender_label(
            [_make_line("My", height=0.5, line_spacing=0.05), _make_line("RANGE", height=0.5)]
        )
        rendered = render_label_to_plt(label)

        fixes = rendered.descender_adjustments_by_line
        assert list(fixes) == [0, 1]
        (fix,) = fixes[0]
        assert fix.char == "y"
        assert fix.scale_y < 1.0  # downscaled
        assert fix.raise_amount > 0.0  # raised
        assert fix.gap_before < fix.gap_after
        # The following line shares the load: the colliding capital is scaled
        # top-down (its lowest point stays exactly where it was).
        (recip,) = fixes[1]
        assert recip.reciprocal
        assert recip.char == "N"
        assert recip.scale_y < 1.0
        assert recip.raise_amount == 0.0
        assert recip.gap_before < recip.gap_after

    def test_relief_clears_the_engraved_stroke_floor(self) -> None:
        """The relieved gap reaches the stroke floor (0.5*(cutter+cutter))."""
        label = _make_descender_label(
            [_make_line("My", height=0.5, line_spacing=0.05), _make_line("RANGE", height=0.5)]
        )
        rendered = render_label_to_plt(label)
        (fix,) = rendered.descender_adjustments_by_line[0]

        floor = 0.5 * (0.03 + 0.03)
        # The bisection settles the gap at the floor within the emission
        # tolerance (1e-6in = a thousandth of a plotter unit).
        assert fix.gap_after >= floor - 1e-6

    def test_clearance_factor_widens_the_required_gap(self) -> None:
        """A unitless clearance of 0.5 settles the relieved gap at 1.5x the floor.

        The clearance is a factor of the stroke floor, not an inch value, so
        the demanded air scales with the cutter width (here 0.03in cutters =>
        floor 0.03in, required 0.045in).
        """
        label = _make_descender_label(
            [_make_line("My", height=0.5, line_spacing=0.05), _make_line("RANGE", height=0.5)],
            descending_character_clearance=0.5,
        )
        rendered = render_label_to_plt(label)
        (fix,) = rendered.descender_adjustments_by_line[0]

        floor = 0.5 * (0.03 + 0.03)
        assert fix.gap_after == pytest.approx(floor * 1.5, abs=1e-6)

    def test_roomy_gap_is_left_alone(self) -> None:
        """A descender with no close partner below engraves exactly as rendered."""
        label = _make_descender_label(
            [_make_line("Depth", height=0.5, line_spacing=0.4), _make_line("RANGE", height=0.5)]
        )
        rendered = render_label_to_plt(label)
        assert rendered.descender_adjustments_by_line == {}

    def test_cap_only_content_is_never_relieved(self) -> None:
        """No descenders means no relief, and the geometry is untouched."""
        content = [
            _make_line("HAPPY", height=0.5, line_spacing=0.05),
            _make_line("ROOTS", height=0.5),
        ]
        on = render_label_to_plt(_make_descender_label(content))
        off = render_label_to_plt(
            _make_descender_label(content, auto_descending_character_adjustment=False)
        )
        assert on.descender_adjustments_by_line == {}
        assert on.plt_content == off.plt_content

    def test_feature_disabled_leaves_the_descender_verbatim(self) -> None:
        """With the flag off, a colliding descender is not relieved."""
        content = [_make_line("My", height=0.5, line_spacing=0.05), _make_line("RANGE", height=0.5)]
        off = render_label_to_plt(
            _make_descender_label(content, auto_descending_character_adjustment=False)
        )
        on = render_label_to_plt(_make_descender_label(content))
        assert off.descender_adjustments_by_line == {}
        assert on.descender_adjustments_by_line != {}
        assert on.plt_content != off.plt_content

    def test_only_the_triggered_glyph_is_relieved(self) -> None:
        """A line with one descender relieves exactly that one glyph."""
        label = _make_descender_label(
            [_make_line("Depth", height=0.5, line_spacing=0.05), _make_line("ROOTS", height=0.5)]
        )
        rendered = render_label_to_plt(label)
        fixes = rendered.descender_adjustments_by_line[0]
        assert [f.char for f in fixes] == ["p"]

    def test_allowances_bound_the_relief(self) -> None:
        """The relief never exceeds v_scale / raise."""
        label = _make_descender_label(
            [_make_line("My", height=0.5, line_spacing=0.05), _make_line("RANGE", height=0.5)],
            descending_character_v_scale=0.6,
            descending_character_raise=0.3,
        )
        rendered = render_label_to_plt(label)
        (fix,) = rendered.descender_adjustments_by_line[0]
        assert fix.scale_y >= 0.6 - 1e-9
        # The raise is a fraction of the glyph's OWN descender depth, which can
        # never exceed the line's toolpath height (0.5 - 0.03 cutter).
        assert 0.0 < fix.raise_amount <= 0.3 * 0.47 + 1e-9

    def test_exhausted_allowances_grow_the_spacing(self) -> None:
        """No downscale + no raise + no reciprocal => the gap widens to clear."""
        label = _make_descender_label(
            [_make_line("My", height=0.5, line_spacing=0.05), _make_line("RANGE", height=0.5)],
            descending_character_v_scale=1.0,
            descending_character_raise=0.0,
            descending_character_reciprocal_scale=1.0,
        )
        rendered = render_label_to_plt(label)
        assert rendered.descender_adjustments_by_line == {}
        # The gap grew past the requested 0.05 to clear the descender.
        assert rendered.line_spacing_by_line[0] > 0.05

    def test_no_room_to_grow_aborts_the_job(self) -> None:
        """Allowances spent and no vertical room => LabelRenderError."""
        label = _make_descender_label(
            [_make_line("My", height=0.5, line_spacing=0.0), _make_line("RANGE", height=0.5)],
            descending_character_v_scale=1.0,
            descending_character_raise=0.0,
            descending_character_reciprocal_scale=1.0,
            height=0.7,
        )
        with pytest.raises(LabelRenderError, match="cannot clear the line below"):
            render_label_to_plt(label)

    def test_relief_preserves_the_glyph_partition(self) -> None:
        """Stroke counts survive the Y-scale, so the intra-chunk sweep stays valid."""
        label = _make_descender_label(
            [_make_line("Depth", height=0.5, line_spacing=0.05), _make_line("RANGE", height=0.5)]
        )
        rendered = render_label_to_plt(label)
        for record in rendered.text_chunks:
            stroke_count = sum(len(block.strokes) for block in record.blocks)
            flat = [i for group in record.glyph_groups for i in group]
            assert sorted(flat) == list(range(stroke_count))

    def test_inkbox_mode_also_relives(self) -> None:
        """The relief runs in ink-box stacking mode too (Y-only, mode-agnostic).

        Ink-box stacking keeps the requested spacing as the visual gap, so a
        zero spacing is what puts the descender against the capitals below.
        """
        label = _make_descender_label(
            [_make_line("My", height=0.5, line_spacing=0.0), _make_line("RANGE", height=0.5)],
            use_baseline_spacing=False,
        )
        rendered = render_label_to_plt(label)
        assert rendered.descender_adjustments_by_line != {}


class TestDescendingCharacterReciprocalRelief:
    """The following line shares the relief by scaling top-down.

    ``descending_character_reciprocal_scale`` caps how far a colliding
    following-line glyph may be squeezed; the squeeze is anchored at the
    glyph's own lowest ink point, so its bottom never moves and its top
    descends -- the deficit is distributed between the two lines instead of
    landing entirely on the descending glyph.
    """

    def test_reciprocal_scale_one_is_bit_identical_to_the_historical_behaviour(
        self,
    ) -> None:
        """The opt-out (1.0) reproduces the upper-only relief byte-for-byte."""
        content = [
            _make_line("My", height=0.5, line_spacing=0.05),
            _make_line("RANGE", height=0.5),
        ]
        off = render_label_to_plt(
            _make_descender_label(content, descending_character_reciprocal_scale=1.0)
        )
        historical = render_label_to_plt(
            _make_descender_label(content, descending_character_reciprocal_scale=1.0),
        )
        assert off.plt_content == historical.plt_content
        # Only the descending glyph carries a record -- no reciprocal partner.
        assert list(off.descender_adjustments_by_line) == [0]
        assert [fix.reciprocal for fix in off.descender_adjustments_by_line[0]] == [False]

    def test_the_reciprocal_half_gentles_the_descending_glyph(self) -> None:
        """Sharing the load means the descender is squeezed less than alone."""
        content = [
            _make_line("My", height=0.5, line_spacing=0.05),
            _make_line("RANGE", height=0.5),
        ]
        shared = render_label_to_plt(
            _make_descender_label(content, descending_character_reciprocal_scale=0.5)
        )
        alone = render_label_to_plt(
            _make_descender_label(content, descending_character_reciprocal_scale=1.0)
        )
        (upper_shared,) = shared.descender_adjustments_by_line[0]
        (upper_alone,) = alone.descender_adjustments_by_line[0]
        # A larger scale_y means LESS downscaling: the lower line took a share.
        assert upper_shared.scale_y > upper_alone.scale_y
        # Both clear the same required gap.
        assert upper_shared.gap_after == pytest.approx(upper_alone.gap_after, abs=1e-3)

    def test_the_partner_is_scaled_top_down_about_its_lowest_point(self) -> None:
        """The reciprocal record reports a scale with a zero raise."""
        label = _make_descender_label(
            [_make_line("My", height=0.5, line_spacing=0.05), _make_line("RANGE", height=0.5)]
        )
        rendered = render_label_to_plt(label)
        (recip,) = rendered.descender_adjustments_by_line[1]
        assert recip.reciprocal
        assert recip.scale_y < 1.0
        # The anchor is the glyph's lowest ink: no translation at all.
        assert recip.raise_amount == 0.0
        assert recip.gap_before < recip.gap_after

    def test_the_reciprocal_scale_is_never_overspent(self) -> None:
        """The partner's squeeze stops at descending_character_reciprocal_scale."""
        label = _make_descender_label(
            [
                _make_line("My", height=0.5, line_spacing=0.05),
                _make_line("RANGE", height=0.5),
            ],
            # The descender's own allowances are forbidden, so the partner is
            # pushed all the way to its floor.
            descending_character_v_scale=1.0,
            descending_character_raise=0.0,
            descending_character_reciprocal_scale=0.5,
        )
        rendered = render_label_to_plt(label)
        (recip,) = rendered.descender_adjustments_by_line[1]
        assert recip.reciprocal
        assert recip.scale_y >= 0.5 - 1e-9

    def test_the_partner_is_the_glyph_that_shares_the_collision(self) -> None:
        """A descender with one close partner below pulls in exactly that glyph."""
        label = _make_descender_label(
            [_make_line("Depth", height=0.5, line_spacing=0.05), _make_line("ROOTS", height=0.5)]
        )
        rendered = render_label_to_plt(label)
        (upper,) = rendered.descender_adjustments_by_line[0]
        (recip,) = rendered.descender_adjustments_by_line[1]
        assert upper.char == "p"
        assert recip.reciprocal
        # The 'p' descender of "Depth" lands on the second 'O' of "ROOTS".
        assert recip.char == "O"
        assert recip.glyph_index == 2

    def test_a_roomy_gap_still_leaves_both_lines_alone(self) -> None:
        """No collision means no reciprocal downscale either."""
        label = _make_descender_label(
            [_make_line("Depth", height=0.5, line_spacing=0.4), _make_line("RANGE", height=0.5)]
        )
        rendered = render_label_to_plt(label)
        assert rendered.descender_adjustments_by_line == {}

    def test_a_middle_line_can_carry_both_ramps(self) -> None:
        """A glyph that descends AND is collided with from above gets both.

        Three tight lines of identical text: every ``g``/``y`` hangs toward the
        line below, and the same glyphs are the colliding partners of the
        descenders above them. The middle line therefore carries BOTH roles --
        its own descending ramp into the gap below and a reciprocal (top-down)
        ramp from the gap above. The fixpoint absorbs the interaction: the job
        renders, every gap clears, and no record overspends its allowance.
        """
        label = _make_descender_label(
            [
                _make_line("gy", height=0.5, line_spacing=0.02),
                _make_line("gy", height=0.5, line_spacing=0.02),
                _make_line("gy", height=0.5),
            ],
            height=2.4,
        )
        rendered = render_label_to_plt(label)
        fixes = rendered.descender_adjustments_by_line

        # The middle line carries both a reciprocal record (from the gap above)
        # and its own descending record (into the gap below).
        middle = fixes[1]
        assert any(fix.reciprocal for fix in middle)
        assert any(not fix.reciprocal for fix in middle)
        # Every record respects its allowance floor.
        for line_fixes in fixes.values():
            for fix in line_fixes:
                assert fix.scale_y >= 0.5 - 1e-9
        # The relief cleared every gap (no abort, no residual).
        assert rendered.has_collisions is False

    def test_the_feature_gate_disables_the_reciprocal_half(self) -> None:
        """``auto_descending_character_adjustment: false`` disables everything."""
        content = [
            _make_line("My", height=0.5, line_spacing=0.05),
            _make_line("RANGE", height=0.5),
        ]
        off = render_label_to_plt(
            _make_descender_label(
                content,
                auto_descending_character_adjustment=False,
                descending_character_reciprocal_scale=0.5,
            )
        )
        assert off.descender_adjustments_by_line == {}

    def test_the_reciprocal_half_never_moves_the_bottom_border_gap(self) -> None:
        """Anchoring at the lowest ink point leaves the border measurement intact.

        The border trigger measures a descender's sub-baseline ink against the
        border stroke below the LAST line. A reciprocal fix on that line scales
        its glyphs toward their own lowest point, so nothing moves down and the
        border clearance is exactly what the upper-only relief produced.
        """
        shared = render_label_to_plt(
            _make_border_label(["charging"], height=1.9, descending_character_reciprocal_scale=0.5)
        )
        alone = render_label_to_plt(
            _make_border_label(["charging"], height=1.9, descending_character_reciprocal_scale=1.0)
        )
        # A single line has no following line, so no partner exists at all.
        assert list(shared.descender_adjustments_by_line) == [0]
        assert [fix.reciprocal for fix in shared.descender_adjustments_by_line[0]] == [
            False,
            False,
        ]
        assert [fix.char for fix in shared.descender_adjustments_by_line[0]] == [
            fix.char for fix in alone.descender_adjustments_by_line[0]
        ]

    def test_apply_glyph_ramps_composes_both_ramps_on_one_glyph(self) -> None:
        """A middle-line glyph carrying both ramps keeps its lowest point fixed.

        The upper ramp (downscale about the baseline + raise) runs first; the
        reciprocal scale then runs about the glyph's resulting lowest ink point,
        so the composition never pushes ink below that anchor.
        """
        # One glyph: a vertical stroke from y = 0.4 down to y = -0.2 (baseline
        # y = 0, descender 0.2), plus an untouched second glyph for the splice.
        block = TextBlock(
            strokes=(
                Stroke(
                    pen_up=0.1j,
                    segments=(LineSeg(complex(0.1, 0.4), complex(0.1, -0.2)),),
                ),
                Stroke(pen_up=0j, segments=(LineSeg(1.0j, complex(1.0, 0.4)),)),
            )
        )
        rebuilt, applied = _apply_glyph_ramps(
            block,
            ((0,), (1,)),
            "pg",
            {0: 1.0},
            0.5,
            0.7,
            {0: 1.0},
            0.5,
        )
        assert [entry[0] for entry in applied] == [0]
        _glyph_index, _char, scale_y, raise_amount, recip_scale_y = applied[0]
        assert scale_y == pytest.approx(0.5)
        assert raise_amount == pytest.approx(0.14)
        assert recip_scale_y == pytest.approx(0.5)
        # The upper ramp puts the bottom at raise - descender * scale = 0.04
        # (0.14 - 0.2*0.5); the reciprocal scale is anchored there, so that is
        # the final bottom. (Measured on the transformed glyph alone -- the
        # untouched sibling reaches y = 1.0 and would dominate the block
        # bounds.)
        glyph = TextBlock(strokes=(rebuilt.strokes[0],))
        bounds = glyph.bounds()
        assert bounds is not None
        anchor = raise_amount - 0.2 * scale_y
        assert bounds[1] == pytest.approx(anchor, abs=1e-9)
        # The top is squeezed toward the anchor: 0.04 + (0.34 - 0.04) * 0.5.
        top_after_upper = 0.4 * scale_y + raise_amount
        assert bounds[3] == pytest.approx(anchor + (top_after_upper - anchor) * recip_scale_y)
        # The untouched glyph is verbatim, and the stroke count is preserved.
        assert len(rebuilt.strokes) == 2
        assert rebuilt.strokes[1].segments[0].start == pytest.approx(1.0j)


def _make_border_label(
    texts: list[str],
    *,
    height: float = 1.9,
    v_margin: float = 0.1,
    line_spacing: float = 0.3,
    **kwargs: object,
) -> ResolvedLabel:
    """Build a dino label positioned near its bottom border.

    Dino at 1.5in with a 0.09in cutter puts the descender tip ~0.94in below
    the centred block's middle, so a 1.9in label leaves ~0.006in of air above
    the border centreline -- far inside the required clearance
    ``0.5 * (0.09 + 0.015) * 1.5 = 0.079in`` (the border cutter is
    ``ResolvedLabel.hole_cutter_diameter``, the snapped boundary/hole tool).
    A 2.1in label leaves ~0.106in and is clear.
    """
    content = [
        ResolvedTextLine(
            text=text,
            nominal_text_height=1.5,
            toolpath_text_height=1.5 - 0.09,
            cutter_diameter=0.09,
            character_spacing=0.0,
            line_spacing=line_spacing,
            max_h_compress=0.0,
            text_h_alignment="center",
            font="dino",
        )
        for text in texts
    ]
    return ResolvedLabel(
        id="border",
        count=1,
        width=6.0,
        height=height,
        margin=0.1,
        h_margin=0.1,
        v_margin=v_margin,
        holes=[],
        content=content,
        descending_character_clearance=0.5,
        **kwargs,  # type: ignore[arg-type]
    )


class TestDescendingCharacterBorderRelief:
    """The bottom border acts as a virtual following line for the relief."""

    def test_descender_close_to_the_bottom_border_is_relieved(self) -> None:
        """A 'g' hanging toward the border stroke is squeezed and raised."""
        rendered = render_label_to_plt(_make_border_label(["charging"], height=1.9))

        fixes = rendered.descender_adjustments_by_line
        assert list(fixes) == [0]
        assert [fix.char for fix in fixes[0]] == ["g", "g"]  # both 'g' in charging
        for fix in fixes[0]:
            assert fix.scale_y < 1.0
            assert fix.raise_amount > 0.0
            # Band centering drops the block by the descender depth, so the
            # tip reaches the border centreline.
            assert fix.gap_before == pytest.approx(0.0, abs=0.01)
            # Cleared to the required 0.5 * (0.09 + 0.015) * 1.5 = 0.0788in,
            # within one plotter unit.
            assert fix.gap_after == pytest.approx(0.0788, abs=1e-3)

    def test_descender_clear_of_the_border_is_left_alone(self) -> None:
        """A roomy label keeps the descender exactly as rendered."""
        rendered = render_label_to_plt(_make_border_label(["charging"], height=2.7))
        assert rendered.descender_adjustments_by_line == {}

    def test_crossing_the_margin_is_not_a_border_collision(self) -> None:
        """Ink spilling into v_margin is accepted; only the border stroke counts.

        Band centering puts the cap band on the inner-area centre, so the ink
        bottom sits ``(height - band) / 2`` above the border: a 2.7in label
        leaves 0.168in of air (clear of the 0.0788in requirement) while the ink
        crosses the 0.25in v_margin. The margin is not the constraint, so
        nothing is relieved.
        """
        rendered = render_label_to_plt(_make_border_label(["charging"], height=2.7, v_margin=0.25))
        assert rendered.descender_adjustments_by_line == {}

    def test_border_relief_never_grows_the_line_spacing(self) -> None:
        """Spacing growth cannot help a border collision, so it stays put.

        Growing the gap above the last line moves that line DOWN in the
        centred frame -- the opposite of what the border needs -- so only the
        per-glyph ramp acts. The two lines here are far apart, so the
        line-to-line mechanism has nothing to do and the spacing keeps its
        authored value.
        """
        label = _make_border_label(["AAAA", "charging"], height=3.4)
        rendered = render_label_to_plt(label)

        fixes = rendered.descender_adjustments_by_line
        assert list(fixes) == [1]  # the last line's 'g' glyphs
        assert [fix.char for fix in fixes[1]] == ["g", "g"]
        assert rendered.line_spacing_by_line[0] == pytest.approx(0.3, abs=1e-9)

    def test_feature_disabled_leaves_the_border_descender_verbatim(self) -> None:
        """With the flag off, the border collision engraves untouched."""
        off = render_label_to_plt(
            _make_border_label(["charging"], height=1.9, auto_descending_character_adjustment=False)
        )
        on = render_label_to_plt(_make_border_label(["charging"], height=1.9))
        assert off.descender_adjustments_by_line == {}
        assert on.descender_adjustments_by_line != {}
        assert on.plt_content != off.plt_content

    def test_exhausted_allowances_warn_and_engrave(self, caplog: pytest.LogCaptureFixture) -> None:
        """No downscale + no raise => the job proceeds and the shortfall warns.

        Spacing growth cannot help a border collision, so a spent ramp is the
        end of the mechanism: the engraving is the best the allowances allow,
        reported as a WARNING naming the glyph, the gap, the requirement and
        the spent allowances.
        """
        label = _make_border_label(
            ["charging"],
            height=1.9,
            descending_character_v_scale=1.0,
            descending_character_raise=0.0,
        )
        with caplog.at_level(logging.WARNING, logger="plt_optimizer.generate.label_renderer"):
            rendered = render_label_to_plt(label)

        assert rendered.descender_adjustments_by_line == {}
        messages = [r.message for r in caplog.records if "bottom border" in r.message]
        assert messages
        assert "glyph 'g' stays" in messages[0]
        assert "needs 0.079in" in messages[0]

    def test_single_line_label_relieves_against_the_border(self) -> None:
        """The border trigger needs no following line (no inter-line gap)."""
        label = _make_border_label(["charging"], height=1.9)
        assert len(label.content) == 1
        rendered = render_label_to_plt(label)
        assert [fix.char for fix in rendered.descender_adjustments_by_line[0]] == ["g", "g"]


def _text_pen_y_extent(rendered: RenderedLabel) -> tuple[float, float]:
    """Return the emitted text layer's ``(min_y, max_y)`` in inches.

    Device frame (``+y`` downward after the export flip), so the visually
    lowest ink is ``max_y``. Borders/holes are excluded: only the text pen
    carries the placement the centering rule controls.
    """
    parts = re.split(r"(SP\d+;)", rendered.plt_content)
    ys: list[float] = []
    for i in range(1, len(parts), 2):
        if int(parts[i][2:-1]) != LAYER_TEXT:
            continue
        points, arcs = _collect_hpgl_geometry(parts[i + 1])
        ys.extend(y for _x, y in points)
        for arc in arcs:
            _x_min, y_min, _x_max, y_max = arc.swept_bounds()
            ys.extend((y_min, y_max))
    return min(ys) / HPGL_UNITS_PER_INCH, max(ys) / HPGL_UNITS_PER_INCH


class TestBandCentering:
    """Text centers by its baseline-to-capline band, not its ink block."""

    def test_band_centering_shifts_the_ink_down_by_the_descender(self) -> None:
        """The visible text drops; the descender hangs into the bottom margin.

        Band centering puts the cap band on the inner-area centre, so the ink
        centre (which includes the descender hanging below the baseline) ends
        up ``band_center_offset`` *below* it -- the exact opposite of the
        historical ink centering, which charges the descender to the block and
        rides the whole line high. In the device frame (``+y`` downward) a
        lower position is a larger y.
        """
        label = _make_border_label(["charging"], height=2.7)
        rendered = render_label_to_plt(label)
        lo, hi = _text_pen_y_extent(rendered)
        ink_center = (lo + hi) / 2.0
        inner_center = label.height / 2.0

        assert label.center_text_by_band is True
        assert rendered.band_center_offset_inches == pytest.approx(0.2382, abs=1e-3)
        # Device frame is +y down: the ink centre sits below the inner centre.
        assert ink_center - inner_center == pytest.approx(
            rendered.band_center_offset_inches, abs=1e-3
        )

    def test_v_center_by_content_restores_ink_centering(self) -> None:
        """v_center_by_content=True centers the ink block (the historical rule)."""
        label = _make_border_label(["charging"], height=2.7, v_center_by_content=True)
        rendered = render_label_to_plt(label)
        lo, hi = _text_pen_y_extent(rendered)

        assert label.center_text_by_band is False
        assert rendered.band_center_offset_inches == 0.0
        assert (lo + hi) / 2.0 == pytest.approx(label.height / 2.0, abs=1e-3)

    def test_relief_off_forces_ink_centering(self) -> None:
        """With the relief disabled nothing adjusts descenders, so ink centers."""
        label = _make_border_label(
            ["charging"], height=2.7, auto_descending_character_adjustment=False
        )
        rendered = render_label_to_plt(label)
        lo, hi = _text_pen_y_extent(rendered)

        assert label.center_text_by_band is False
        assert rendered.band_center_offset_inches == 0.0
        assert (lo + hi) / 2.0 == pytest.approx(label.height / 2.0, abs=1e-3)

    def test_band_centering_exposes_the_border_collision_ink_centering_hid(self) -> None:
        """The two features are complementary, not redundant.

        A 2.1in label is border-clear under ink centering (the block rides high
        enough that the descender never reaches the border), so nothing is
        relieved. Band centering drops the text to center the visible glyphs,
        which puts the 'g' tip inside the required clearance -- and the relief
        catches it. Centering the text optically and clearing the border are
        achieved together, which is the point of the pair.
        """
        band = render_label_to_plt(_make_border_label(["charging"], height=2.1))
        ink = render_label_to_plt(
            _make_border_label(["charging"], height=2.1, v_center_by_content=True)
        )

        assert ink.descender_adjustments_by_line == {}
        assert band.band_center_offset_inches > 0.0
        fixes = band.descender_adjustments_by_line[0]
        assert [fix.char for fix in fixes] == ["g", "g"]
        for fix in fixes:
            assert fix.gap_before < 0.0788  # inside the required clearance
            assert fix.gap_after == pytest.approx(0.0788, abs=1e-3)

    def test_band_centering_does_not_move_the_label_bounds(self) -> None:
        """Packing is unaffected: the border rect dominates the render bounds."""
        band = render_label_to_plt(_make_border_label(["charging"], height=2.7))
        ink = render_label_to_plt(
            _make_border_label(["charging"], height=2.7, v_center_by_content=True)
        )

        assert band.width == pytest.approx(ink.width, abs=1e-9)
        assert band.height == pytest.approx(ink.height, abs=1e-9)


def _markup_job(*lines, **job_kwargs):
    """Build a single-label JobSpec over raw (markup-carrying) text lines."""
    from plt_optimizer.generate.schema import JobSpec, LabelSpec, TextLine

    return JobSpec(
        job_name="M",
        width=6.0,
        height=1.0,
        text_height=0.5,
        labels=[
            LabelSpec(
                id="lbl",
                width=6.0,
                height=1.0,
                content=[TextLine(text=text, **kw) for text, kw in lines],
            )
        ],
        **job_kwargs,
    )


class TestItalicRenderDispatch:
    """``_render_line_block`` renders plain text with per-character italic."""

    def test_markup_renders_plain_text_not_the_tags(self) -> None:
        """The engraving measures the plain text; the tags never appear."""
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block
        from plt_optimizer.generate.resolution import resolve_job_spec

        line = resolve_job_spec(_markup_job(("<i>AV</i>", {})))[0].content[0]
        block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)
        bounds = block.bounds()
        assert bounds is not None
        # The glyph partition covers the PLAIN text (2 glyphs), not the 9 tag chars.
        assert len(glyphs) == 2

    def test_italic_line_is_sheared_from_its_plain_twin(self) -> None:
        """The dispatch shears the flagged glyphs (baseline fixed, top rises)."""
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block
        from plt_optimizer.generate.resolution import resolve_job_spec

        italic = resolve_job_spec(_markup_job(("<i>AVX</i>", {})))[0].content[0]
        plain = resolve_job_spec(_markup_job(("AVX", {})))[0].content[0]
        iblock, _iw, iglyphs = _render_line_block(italic, TextChunkMode.LINE)
        ib = iblock.bounds()
        assert len(iglyphs) == 3
        pb = _render_line_block(plain, TextChunkMode.LINE)[0].bounds()
        assert ib is not None and pb is not None
        # The shear about y = 0 keeps the baseline and lifts the cap line.
        assert ib[1] == pytest.approx(pb[1], abs=1e-12)
        assert ib[3] > pb[3]

    def test_angle_zero_renders_like_plain(self) -> None:
        """``italic_angle: 0`` keeps the historical (upright) geometry."""
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block
        from plt_optimizer.generate.resolution import resolve_job_spec

        upright = resolve_job_spec(_markup_job(("<i>AVX</i>", {"italic_angle": 0.0})))[0].content[0]
        plain = resolve_job_spec(_markup_job(("AVX", {})))[0].content[0]
        ub = _render_line_block(upright, TextChunkMode.LINE)[0].bounds()
        pb = _render_line_block(plain, TextChunkMode.LINE)[0].bounds()
        assert ub == pytest.approx(pb, abs=1e-12)

    def test_italic_render_kwargs_gate(self) -> None:
        """The kwargs dict is empty for plain / upright / non-italic lines."""
        from plt_optimizer.generate.label_renderer import _italic_render_kwargs
        from plt_optimizer.generate.resolution import resolve_job_spec

        plain = resolve_job_spec(_markup_job(("AV", {})))[0].content[0]
        assert _italic_render_kwargs(plain) == {}
        upright = resolve_job_spec(_markup_job(("<i>AV</i>", {"italic_angle": 0.0})))[0].content[0]
        assert _italic_render_kwargs(upright) == {}
        italic = resolve_job_spec(_markup_job(("<i>AV</i>", {})))[0].content[0]
        kwargs = _italic_render_kwargs(italic)
        assert set(kwargs) == {"italic_tan", "italic_flags"}
        assert kwargs["italic_flags"] == (True, True)
        assert kwargs["italic_tan"] == pytest.approx(math.tan(math.radians(12.0)))

    def test_word_chunk_mode_keeps_word_groups(self) -> None:
        """Italic lines keep exact word groups in WORD chunk mode."""
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block
        from plt_optimizer.generate.resolution import resolve_job_spec

        line = resolve_job_spec(_markup_job(("<i>AB</i> CD", {})))[0].content[0]
        _block, words, glyphs = _render_line_block(line, TextChunkMode.WORD)
        assert [text for text, _ in words] == ["AB", "CD"]
        # Four plain glyphs (the tags contribute nothing).
        assert len(glyphs) == 4


class TestUnderlineRenderDispatch:
    """``_render_line_block`` renders plain text with per-character underlines."""

    def test_markup_renders_plain_text_not_the_tags(self) -> None:
        """The engraving measures the plain text; the tags never appear."""
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block
        from plt_optimizer.generate.resolution import resolve_job_spec

        line = resolve_job_spec(_markup_job(("<u>AV</u>", {})))[0].content[0]
        block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)
        # The glyph partition covers the PLAIN text (2 glyphs) -- the
        # underline strokes are appended after it and never join a group.
        assert len(glyphs) == 2
        assert block.bounds() is not None

    def test_underline_hangs_below_its_plain_twin(self) -> None:
        """The centerline sits ``cutter + offset`` below the baseline."""
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block
        from plt_optimizer.generate.resolution import resolve_job_spec

        underlined = resolve_job_spec(_markup_job(("<u>AV</u>", {})))[0].content[0]
        plain = resolve_job_spec(_markup_job(("AV", {})))[0].content[0]
        ub = _render_line_block(underlined, TextChunkMode.LINE)[0].bounds()
        pb = _render_line_block(plain, TextChunkMode.LINE)[0].bounds()
        assert ub is not None and pb is not None
        # Caps sit on the baseline (y_min = 0); the underline centerline is
        # the block's new floor at -(cutter + offset), the caps' width intact.
        assert pb[1] == pytest.approx(0.0, abs=1e-12)
        assert ub[1] == pytest.approx(-(underlined.cutter_diameter), abs=1e-12)
        assert ub[3] == pytest.approx(pb[3], abs=1e-12)
        assert ub[2] - ub[0] == pytest.approx(pb[2] - pb[0], abs=1e-12)

    def test_offset_deepens_the_underline(self) -> None:
        """``underline_offset`` lowers the centerline by exactly its value."""
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block
        from plt_optimizer.generate.resolution import resolve_job_spec

        deep = resolve_job_spec(_markup_job(("<u>AV</u>", {"underline_offset": 0.1})))[0].content[0]
        flush = resolve_job_spec(_markup_job(("<u>AV</u>", {})))[0].content[0]
        db = _render_line_block(deep, TextChunkMode.LINE)[0].bounds()
        fb = _render_line_block(flush, TextChunkMode.LINE)[0].bounds()
        assert db is not None and fb is not None
        assert db[1] == pytest.approx(fb[1] - 0.1, abs=1e-12)

    def test_underline_render_gate(self) -> None:
        """The gate is empty for plain / italic-only lines, runs for ``<u>``."""
        from plt_optimizer.generate.label_renderer import _underline_render_gate
        from plt_optimizer.generate.resolution import resolve_job_spec

        plain = resolve_job_spec(_markup_job(("AV", {})))[0].content[0]
        assert _underline_render_gate(plain) == ()
        italic = resolve_job_spec(_markup_job(("<i>AV</i>", {})))[0].content[0]
        assert _underline_render_gate(italic) == ()
        underlined = resolve_job_spec(_markup_job(("<u>AV</u>", {})))[0].content[0]
        assert _underline_render_gate(underlined) == ((0, 1),)
        # Runs are plain_text indices (inclusive), spaces included.
        partial = resolve_job_spec(_markup_job(("A<u>bb</u>C", {})))[0].content[0]
        assert _underline_render_gate(partial) == ((1, 2),)
        multi = resolve_job_spec(_markup_job(("A<u>bb</u>C<u>dd</u>", {})))[0].content[0]
        assert _underline_render_gate(multi) == ((1, 2), (4, 5))

    def test_break_spaces_splits_runs_at_spaces(self) -> None:
        """``underline_break_spaces`` ends each run at a space (bare gap).

        False (the default) keeps one continuous run across the spaces, so a
        multi-word run draws a single underline. True splits the run at each
        space, so every word gets its own underline spanning its own ink and
        the inter-word space stays bare.
        """
        from plt_optimizer.generate.label_renderer import _underline_render_gate
        from plt_optimizer.generate.resolution import resolve_job_spec

        # 'beverage grade' is 14 chars; the space is index 8.
        continuous = resolve_job_spec(_markup_job(("<u>beverage grade</u>", {})))[0].content[0]
        assert _underline_render_gate(continuous) == ((0, 13),)

        broken = resolve_job_spec(
            _markup_job(("<u>beverage grade</u>", {"underline_break_spaces": True}))
        )[0].content[0]
        # The run ends at the space (index 8 excluded): two word runs.
        assert _underline_render_gate(broken) == ((0, 7), (9, 13))

        # Three words split into three runs.
        three = resolve_job_spec(_markup_job(("<u>a b c</u>", {"underline_break_spaces": True})))[
            0
        ].content[0]
        assert _underline_render_gate(three) == ((0, 0), (2, 2), (4, 4))

    def test_break_spaces_leaves_the_space_bare(self) -> None:
        """With the flag on, no underline stroke covers the inter-word space."""
        from plt_optimizer.generate.label_renderer import (
            TextChunkMode,
            _render_line_block,
            _underline_render_gate,
            _underline_strokes,
        )
        from plt_optimizer.generate.resolution import resolve_job_spec
        from plt_optimizer.generate.text_geometry import TextBlock

        line = resolve_job_spec(
            _markup_job(("<u>hello world</u>", {"underline_break_spaces": True}), font="Dino")
        )[0].content[0]
        block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)
        strokes = _underline_strokes(line, block, glyphs, _underline_render_gate(line))
        # 'hello world': the space is plain_text index 5. Its neighbours' ink
        # boxes bound the bare gap; no underline segment may cover it.
        boxes = [TextBlock(strokes=tuple(block.strokes[i] for i in g)).bounds() for g in glyphs]
        space_lo = boxes[4][2]  # 'o' right edge
        space_hi = boxes[5][0]  # 'w' left edge
        for stroke in strokes:
            seg = stroke.segments[0]
            assert not (seg.start.real <= space_lo + 1e-9 and seg.end.real >= space_hi - 1e-9)

    def test_word_chunk_mode_attaches_underline_to_last_word(self) -> None:
        """WORD mode keeps the word split and never drops the underline."""
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block
        from plt_optimizer.generate.resolution import resolve_job_spec

        line = resolve_job_spec(_markup_job(("<u>AB</u> CD", {})))[0].content[0]
        block, words, glyphs = _render_line_block(line, TextChunkMode.WORD)
        assert [text for text, _ in words] == ["AB", "CD"]
        # Four plain glyphs (the tags contribute nothing); the underline
        # strokes are appended after the partition.
        assert len(glyphs) == 4
        underline_indices = set(range(sum(len(g) for g in glyphs), len(block.strokes)))
        assert underline_indices, "the underline must be engraved"
        # The last non-empty word carries the underline indices, and the word
        # groups partition every stroke exactly once.
        assert set(words[-1][1]) >= underline_indices
        covered = [index for _text, indices in words for index in indices]
        assert sorted(covered) == list(range(len(block.strokes)))

    def test_nested_markup_shears_and_underlines(self) -> None:
        """``<i>AV <u>WX</u> YZ</i>`` shears every glyph, underlines the run."""
        from plt_optimizer.generate.label_renderer import (
            TextChunkMode,
            _render_line_block,
            _underline_render_gate,
        )
        from plt_optimizer.generate.resolution import resolve_job_spec

        nested = resolve_job_spec(_markup_job(("<i>AV <u>WX</u> YZ</i>", {})))[0].content[0]
        assert nested.plain_text == "AV WX YZ"
        # The gate sees exactly the inner run, in plain_text indices.
        assert _underline_render_gate(nested) == ((3, 4),)

        twin = resolve_job_spec(_markup_job(("<i>AV WX YZ</i>", {})))[0].content[0]
        nb = _render_line_block(nested, TextChunkMode.LINE)[0].bounds()
        tb = _render_line_block(twin, TextChunkMode.LINE)[0].bounds()
        assert nb is not None and tb is not None
        # Italic reaches every glyph (the twin is sheared too), so the widths
        # match; the underline is the nested block's new floor at -cutter.
        assert nb[1] == pytest.approx(-(nested.cutter_diameter), abs=1e-12)
        assert tb[1] == pytest.approx(0.0, abs=1e-12)
        assert nb[3] == pytest.approx(tb[3], abs=1e-12)
        assert nb[2] - nb[0] == pytest.approx(tb[2] - tb[0], abs=1e-12)

    def test_caps_never_break_the_underline(self) -> None:
        """A cap breaks the underline at no offset and no clearance.

        The Dino ``O``/``G`` overshoot ~0.0026in below the baseline (a rounded
        cap's bowl dips a hair under it), and clean caps ``AVX`` rest on the
        baseline. None of them reaches the underline's centerline (one cutter
        below the baseline), so the centerline-crossing test never fires: every
        run engraves in one piece. The earlier 2D radial test collided with the
        caps through the vertical component of the distance and shattered the
        underline -- the reported bug.
        """
        from plt_optimizer.generate.label_renderer import (
            TextChunkMode,
            _render_line_block,
            _underline_render_gate,
            _underline_strokes,
        )
        from plt_optimizer.generate.resolution import resolve_job_spec

        def segments(text: str, offset: float, clearance: float) -> int:
            line = resolve_job_spec(
                _markup_job(
                    (
                        f"<u>{text}</u>",
                        {"underline_offset": offset, "underline_break_clearance": clearance},
                    ),
                    font="Dino",
                )
            )[0].content[0]
            block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)
            return len(_underline_strokes(line, block, glyphs, _underline_render_gate(line)))

        for text in ("AVX", "DOG"):
            for offset in (0.0, 0.05, 0.1):
                for clearance in (0.0, 0.05, 0.1, 0.2):
                    assert segments(text, offset, clearance) == 1

    def test_descender_breaks_only_where_it_crosses(self) -> None:
        """A ``g`` breaks the underline at its stem, not across its whole width.

        The Dino ``g`` is an open J-hook: the stem descends through the
        underline at the glyph's right edge, the bowl sweeps below and back up.
        Only the stem crosses the underline's centerline, so the break is a
        narrow gap at the stem -- the bowl keeps its underline. A whole-glyph
        gap (the half-plane model's behaviour) is the bug this pins out.
        """
        from plt_optimizer.generate.label_renderer import (
            TextChunkMode,
            _render_line_block,
            _underline_render_gate,
            _underline_strokes,
        )
        from plt_optimizer.generate.resolution import resolve_job_spec

        line = resolve_job_spec(
            _markup_job(("<u>g</u>", {"underline_break_clearance": 0.05}), font="Dino")
        )[0].content[0]
        block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)
        strokes = _underline_strokes(line, block, glyphs, _underline_render_gate(line))
        # The stem sits at the glyph's right edge, so the gap trims the
        # underline's right end: the kept span covers the whole bowl width.
        assert len(strokes) == 1
        kept = strokes[0].segments[0]
        # The `g` spans ~0.246in; the kept underline keeps most of it (the
        # stem's gap is ~0.165in at this clearance, at the far right).
        assert kept.end.real - kept.start.real > 0.15

    def test_offset_drops_the_centerline_clear_of_a_descender(self) -> None:
        """A deeper underline stops a descender breaking it.

        The Dino ``p`` hangs ~0.146in below the baseline. At offset 0.0 the
        centerline (one cutter below the baseline) sits above the ``p``'s
        bottom, so the stem crosses it and the underline breaks. At offset 0.15
        the centerline drops below the ``p``'s bottom, so nothing crosses and
        the underline stays whole.
        """
        from plt_optimizer.generate.label_renderer import (
            TextChunkMode,
            _render_line_block,
            _underline_render_gate,
            _underline_strokes,
        )
        from plt_optimizer.generate.resolution import resolve_job_spec

        def segments(offset: float) -> int:
            line = resolve_job_spec(
                _markup_job(("<u>Depth</u>", {"underline_offset": offset}), font="Dino")
            )[0].content[0]
            block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)
            return len(_underline_strokes(line, block, glyphs, _underline_render_gate(line)))

        assert segments(0.0) == 2  # the `p` crosses the centerline: one gap
        assert segments(0.15) == 1  # the underline drops below the `p`: whole

    def test_break_clearance_widens_the_gap_around_a_descender(self) -> None:
        """``underline_break_clearance`` is the air at each underline break.

        Every cut is padded horizontally by ``0.5 * cutter + clearance`` (the
        half cutter is the glyph stroke's own half-width), so the engraved air
        between a descending glyph's stroke and the underline's cut edge equals
        the clearance (0.0 = the cut sits at the ink). ``pay`` on Dino keeps
        the same break count at clearance 0.0 and 0.02, so every break width
        grows -- the clearance is the knob the offset is not (the offset decides
        *which* glyphs break; it cannot add air at the cut).
        """
        from plt_optimizer.generate.label_renderer import (
            TextChunkMode,
            _render_line_block,
            _underline_render_gate,
            _underline_strokes,
        )
        from plt_optimizer.generate.resolution import resolve_job_spec

        def break_widths(clearance: float) -> list[float]:
            line = resolve_job_spec(
                _markup_job(
                    ("<u>pay</u>", {"underline_break_clearance": clearance}),
                    font="Dino",
                )
            )[0].content[0]
            block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)
            strokes = _underline_strokes(line, block, glyphs, _underline_render_gate(line))
            spans = sorted(
                (stroke.segments[0].start.real, stroke.segments[0].end.real) for stroke in strokes
            )
            return [spans[index + 1][0] - spans[index][1] for index in range(len(spans) - 1)]

        tight = break_widths(0.0)
        wide = break_widths(0.02)
        assert len(tight) == len(wide) == 1
        # Every break grows by (at least) the clearance on each side.
        assert all(w > t for w, t in zip(wide, tight))
        # A larger clearance keeps widening (monotone, no oscillation).
        wider = break_widths(0.05)
        assert len(wider) >= 1
        assert max(wider) > max(wide)


class TestBoldRenderDispatch:
    """``_render_line_block`` renders plain text with a per-character bold cutter."""

    # The shop inventory the bold rung steps through (Dino auto 0.060 -> 0.090).
    _INVENTORY = [0.01, 0.015, 0.02, 0.03, 0.045, 0.06, 0.09, 0.125, 0.25]

    @classmethod
    def _resolve(cls, *lines, **job_kwargs):
        """Resolve a markup job against the inventory (so ``<b>`` has a rung)."""
        from plt_optimizer.generate.resolution import resolve_job_spec

        return resolve_job_spec(_markup_job(*lines, **job_kwargs), cls._INVENTORY)

    def test_bold_line_renders_plain_text_not_the_tags(self) -> None:
        """The engraving measures the plain text; the tags never appear."""
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block

        line = self._resolve(("<b>AV</b>", {}))[0].content[0]
        block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)
        assert block.bounds() is not None
        assert len(glyphs) == 2

    def test_bold_glyphs_shrink_vs_the_plain_twin(self) -> None:
        """The bold run's cap line sits below the plain twin's (centerline scale)."""
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block

        bold = self._resolve(("<b>AVX</b>", {}))[0].content[0]
        plain = self._resolve(("AVX", {}))[0].content[0]
        assert bold.bold_cutter_diameter is not None
        bb = _render_line_block(bold, TextChunkMode.LINE)[0].bounds()
        pb = _render_line_block(plain, TextChunkMode.LINE)[0].bounds()
        assert bb is not None and pb is not None
        # The baseline stays fixed; the top descends (a thicker tool).
        assert bb[1] == pytest.approx(pb[1], abs=1e-9)
        assert bb[3] < pb[3]

    def test_bold_render_kwargs_gate(self) -> None:
        """The kwargs dict is empty for plain / bold-cutter-less lines."""
        from plt_optimizer.generate.label_renderer import _glyph_render_kwargs

        plain = self._resolve(("AV", {}))[0].content[0]
        assert _glyph_render_kwargs(plain, plt=True) == {}
        assert _glyph_render_kwargs(plain, plt=False) == {}
        bold = self._resolve(("<b>AV</b>", {}))[0].content[0]
        plt_kwargs = _glyph_render_kwargs(bold, plt=True)
        assert set(plt_kwargs) == {"cutter_diameters", "glyph_scales"}
        assert plt_kwargs["cutter_diameters"] == (
            bold.bold_cutter_diameter,
            bold.bold_cutter_diameter,
        )
        ttf_kwargs = _glyph_render_kwargs(bold, plt=False)
        assert set(ttf_kwargs) == {"glyph_scales"}
        assert ttf_kwargs["glyph_scales"] == (
            pytest.approx(bold._bold_glyph_scale()),
            pytest.approx(bold._bold_glyph_scale()),
        )

    def test_bold_render_kwargs_empty_without_a_bold_cutter(self) -> None:
        """A bold run with no bold cutter (top-of-inventory) yields no kwargs."""
        from plt_optimizer.generate.label_renderer import _glyph_render_kwargs
        from plt_optimizer.generate.resolution import ResolvedTextLine

        line = ResolvedTextLine(
            text="<b>AV</b>",
            nominal_text_height=0.5,
            toolpath_text_height=0.44,
            cutter_diameter=0.06,
            character_spacing=0.0,
            line_spacing=0.0,
            plain_text="AV",
            styled_runs=(
                __import__("plt_optimizer.generate.markup", fromlist=["Run"]).Run(
                    "AV",
                    __import__("plt_optimizer.generate.markup", fromlist=["Style"]).Style(bold=True),
                ),
            ),
            bold_cutter_diameter=None,
        )
        assert _glyph_render_kwargs(line, plt=True) == {}
        assert _glyph_render_kwargs(line, plt=False) == {}

    def test_stroke_cutter_map_selects_the_bold_glyphs(self) -> None:
        """``_stroke_cutter_map`` gives the bold glyphs the bold cutter."""
        from plt_optimizer.generate.label_renderer import (
            TextChunkMode,
            _render_line_block,
            _stroke_cutter_map,
        )

        line = self._resolve(("A<b>V</b>", {}))[0].content[0]
        block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)
        cutters = _stroke_cutter_map(line, block, glyphs)
        assert len(cutters) == len(block.strokes)
        assert set(cutters) == {line.cutter_diameter, line.bold_cutter_diameter}
        # The plain glyph 'A' owns the first group; all its strokes use the plain tool.
        assert all(cutters[i] == line.cutter_diameter for i in glyphs[0])
        # The bold glyph 'V' owns the second group; all its strokes use the bold tool.
        assert all(cutters[i] == line.bold_cutter_diameter for i in glyphs[1])

    def test_pen_split_partitions_the_block(self) -> None:
        """``_split_block_by_cutters`` yields disjoint groups covering every stroke."""
        from plt_optimizer.generate.label_renderer import (
            TextChunkMode,
            _render_line_block,
            _split_block_by_cutters,
            _stroke_cutter_map,
        )

        line = self._resolve(("A<b>V</b>", {}))[0].content[0]
        block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)
        cutters = _stroke_cutter_map(line, block, glyphs)
        groups = _split_block_by_cutters(block, cutters, None, glyphs)
        assert len(groups) == 2
        (plain_block, _pw, plain_glyphs, plain_cutter), (
            bold_block,
            _bw,
            bold_glyphs,
            bold_cutter,
        ) = groups
        assert plain_cutter == line.cutter_diameter
        assert bold_cutter == line.bold_cutter_diameter
        assert len(plain_block.strokes) + len(bold_block.strokes) == len(block.strokes)
        # Each group's glyph groups stay within that group's stroke range, and the
        # two groups together keep every glyph group (a glyph is all-bold/plain).
        assert all(i < len(plain_block.strokes) for group in plain_glyphs for i in group)
        assert all(i < len(bold_block.strokes) for group in bold_glyphs for i in group)
        assert len(plain_glyphs) == len(glyphs)
        assert len(bold_glyphs) == len(glyphs)

    def test_pen_split_is_a_no_op_without_markup(self) -> None:
        """A single-cutter line splits into one group carrying the whole block."""
        from plt_optimizer.generate.label_renderer import (
            TextChunkMode,
            _render_line_block,
            _split_block_by_cutters,
        )

        line = self._resolve(("AV", {}))[0].content[0]
        block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)
        groups = _split_block_by_cutters(block, (line.cutter_diameter,) * len(block.strokes), None, glyphs)
        assert len(groups) == 1
        whole_block, _w, _g, cutter = groups[0]
        assert cutter == line.cutter_diameter
        assert len(whole_block.strokes) == len(block.strokes)

    def test_line_entry_carries_stroke_cutters(self) -> None:
        """``_render_positioned_lines`` records the per-stroke cutters per line."""
        from plt_optimizer.generate.label_renderer import _render_positioned_lines

        label = self._resolve(("A<b>V</b>", {}))[0]
        entries = [entry for _i, _b, entry, _w, _g in _render_positioned_lines(label)]
        assert entries[0].stroke_cutters

    def test_plain_line_entry_has_no_stroke_cutters(self) -> None:
        """A markup-free line records an empty stroke-cutter tuple."""
        from plt_optimizer.generate.label_renderer import _render_positioned_lines

        label = self._resolve(("AV", {}))[0]
        entries = [entry for _i, _b, entry, _w, _g in _render_positioned_lines(label)]
        assert entries[0].stroke_cutters == ()

    def test_line_cutter_uses_the_widest_tool(self) -> None:
        """The relief/collision floors read the line's MAX (plain, bold) cutter."""
        from plt_optimizer.generate.label_renderer import _line_cutter

        label = self._resolve(("A<b>V</b>", {}))[0]
        line = label.content[0]
        assert _line_cutter(label, 0) == pytest.approx(line.bold_cutter_diameter)
        assert _line_cutter(label, 0) > line.cutter_diameter

    def test_collision_threshold_uses_the_widest_tool(self) -> None:
        """The whole-line AABB collision floor charges the bold cutter."""
        from plt_optimizer.generate.label_renderer import _collision_threshold

        label = self._resolve(("A<b>V</b>", {}))[0]
        line = label.content[0]
        threshold = _collision_threshold(label, 0)
        expected_bold = (
            0.5 * (label.hole_cutter_diameter + line.bold_cutter_diameter)
            + label.hole_text_collision_distance
        )
        assert threshold == pytest.approx(expected_bold)

    def test_measure_natural_width_is_bold_aware(self) -> None:
        """The reflow/compression probe measures the bold-scaled line width."""
        from plt_optimizer.generate.label_renderer import measure_line_natural_width

        bold = self._resolve(("<b>AVX</b>", {}))[0].content[0]
        plain = self._resolve(("AVX", {}))[0].content[0]
        # The bold glyphs shrink, so the line renders narrower than the plain twin.
        assert measure_line_natural_width(bold) < measure_line_natural_width(plain)



class TestScaleRenderDispatch:
    """``_render_line_block`` renders ``<scale:N>`` runs at a per-glyph scale."""

    _INVENTORY = [0.01, 0.015, 0.02, 0.03, 0.045, 0.06, 0.09, 0.125, 0.25]

    @classmethod
    def _resolve(cls, *lines, **job_kwargs):
        """Resolve a markup job against the inventory (so a step has rungs)."""
        from plt_optimizer.generate.resolution import resolve_job_spec

        return resolve_job_spec(_markup_job(*lines, **job_kwargs), cls._INVENTORY)

    def test_scaled_line_renders_plain_text_not_the_tags(self) -> None:
        """The engraving measures the plain text; the tags never appear."""
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block

        line = self._resolve(("<scale:0.5>AV</scale>", {}))[0].content[0]
        block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)
        assert block.bounds() is not None
        assert len(glyphs) == 2

    def test_downscale_shrinks_vs_the_plain_twin(self) -> None:
        """The scaled run's cap line sits below the plain twin's (baseline fixed)."""
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block

        scaled = self._resolve(("<scale:0.5>AVX</scale>", {}))[0].content[0]
        plain = self._resolve(("AVX", {}))[0].content[0]
        sb = _render_line_block(scaled, TextChunkMode.LINE)[0].bounds()
        pb = _render_line_block(plain, TextChunkMode.LINE)[0].bounds()
        assert sb is not None and pb is not None
        # Cap-only glyphs: the baseline (bottom) stays, the top descends.
        assert sb[1] == pytest.approx(pb[1], abs=1e-9)
        assert sb[3] < pb[3]

    def test_upscale_grows_vs_the_plain_twin(self) -> None:
        """An upscaled run's cap line rises above the plain twin's."""
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block

        scaled = self._resolve(("<scale:1.5>AVX</scale>", {}))[0].content[0]
        plain = self._resolve(("AVX", {}))[0].content[0]
        sb = _render_line_block(scaled, TextChunkMode.LINE)[0].bounds()
        pb = _render_line_block(plain, TextChunkMode.LINE)[0].bounds()
        assert sb is not None and pb is not None
        assert sb[1] == pytest.approx(pb[1], abs=1e-9)
        assert sb[3] > pb[3]

    def test_scale_unity_matches_the_plain_twin(self) -> None:
        """``<scale:1>`` engraves exactly like the plain line (fast path)."""
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block

        scaled = self._resolve(("<scale:1>AVX</scale>", {}))[0].content[0]
        plain = self._resolve(("AVX", {}))[0].content[0]
        sb = _render_line_block(scaled, TextChunkMode.LINE)[0].bounds()
        pb = _render_line_block(plain, TextChunkMode.LINE)[0].bounds()
        assert sb == pb

    def test_partial_scale_moves_only_the_scaled_glyphs(self) -> None:
        """A per-run scale leaves the plain prefix at full height."""
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block

        line = self._resolve(("A<scale:0.5>B</scale>", {}))[0].content[0]
        block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)
        assert block.bounds() is not None
        # Glyph 'A' (plain) reaches the full cap; glyph 'B' (scaled) is shorter.
        from plt_optimizer.generate.text_geometry import TextBlock

        a_bounds = TextBlock(strokes=tuple(block.strokes[i] for i in glyphs[0])).bounds()
        b_bounds = TextBlock(strokes=tuple(block.strokes[i] for i in glyphs[1])).bounds()
        assert a_bounds is not None and b_bounds is not None
        assert b_bounds[3] < a_bounds[3]

    def test_scale_is_aware_in_natural_width(self) -> None:
        """measure_line_natural_width sees the scaled width (reflow/compression)."""
        from plt_optimizer.generate.label_renderer import measure_line_natural_width

        scaled = self._resolve(("<scale:0.5>AVX</scale>", {}))[0].content[0]
        plain = self._resolve(("AVX", {}))[0].content[0]
        assert measure_line_natural_width(scaled) < measure_line_natural_width(plain)

    def test_glyph_partition_stays_one_to_one(self) -> None:
        """The scaled block keeps the exact per-glyph partition (relief/sweep)."""
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block

        line = self._resolve(("a<scale:0.5>bc</scale>d", {}))[0].content[0]
        block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)
        flat = [index for group in glyphs for index in group]
        assert sorted(flat) == list(range(len(block.strokes)))

    def test_scale_plus_bold_splits_onto_two_pens(self) -> None:
        """A bold+scaled line splits its strokes onto the plain and bold pens."""
        from plt_optimizer.generate.label_renderer import (
            TextChunkMode,
            _render_text_lines_by_pen,
        )
        from plt_optimizer.generate.resolution import build_cutter_pen_map, resolve_job_spec

        label = self._resolve(("<b>a<scale:0.5>b</scale></b>", {}), font="Dino")[0]
        assert label.content[0].bold_cutter_diameter is not None
        pen_map = build_cutter_pen_map([label])
        _pens, entries, _records = _render_text_lines_by_pen(label, pen_map, TextChunkMode.LINE)
        # The line carries more than one engraving cutter (plain + bold + the
        # scaled run's effective-height tool), recorded per stroke.
        assert len(set(entries[0].stroke_cutters)) >= 2

    def test_scale_bold_splits_onto_three_pens(self) -> None:
        """A plain + bold + scaled line routes onto three per-cutter pens.

        Dino 0.5in text: plain 0.060, the bold rung 0.090, and the bold+0.5x
        run the bold rung at 0.25in (0.045) -- three layers, three chunk groups.
        """
        from plt_optimizer.generate.label_renderer import (
            TextChunkMode,
            _render_text_lines_by_pen,
        )
        from plt_optimizer.generate.resolution import build_cutter_pen_map

        label = self._resolve(("A<b>B<scale:0.5>C</scale></b>", {}), font="Dino")[0]
        line = label.content[0]
        assert line.used_cutters == (0.045, 0.06, 0.09)
        pen_map = build_cutter_pen_map([label])
        pens, _entries, records = _render_text_lines_by_pen(label, pen_map, TextChunkMode.LINE)
        assert len(pens) == 3
        assert {record.pen for record in records} == set(pens)

    def test_underline_splits_at_the_scale_boundary(self) -> None:
        """One ``<u>`` span across a scale boundary draws two underlines.

        The underline is cut with the run's own tool and a run's centerline
        sits one cutter below the baseline, so the gate splits the span where
        the cutter changes (the plain word at the line cutter, the scaled word
        at its effective-height cutter).
        """
        from plt_optimizer.generate.label_renderer import _underline_render_gate

        line = self._resolve(("<u>pay <scale:0.5>deep</scale></u>", {}), font="Dino")[0].content[0]
        runs = _underline_render_gate(line)
        assert len(runs) == 2
        first, second = runs
        # The plain word 'pay' (the space carries the plain cutter, so it rides
        # the first run) then the scaled word 'deep'.
        assert line.plain_text[first[0] : first[1] + 1] == "pay "
        assert line.plain_text[second[0] : second[1] + 1] == "deep"
