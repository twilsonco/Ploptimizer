"""Tests for the per-cutter Phase 3 export pipeline."""

import logging
import math
import re
from pathlib import Path
from typing import Optional

import pytest

from plt_optimizer.core.units import HPGL_UNITS_PER_INCH
from plt_optimizer.generate.layout import LayoutMode
from plt_optimizer.generate.resolution import (
    LineContentConfigError,
    ResolvedLabel,
    ResolvedTextLine,
    resolve_job_spec,
)
from plt_optimizer.generate.schema import (
    LabelSpec,
    PlateSpec,
    TextLine,
    parse_yaml,
)
from plt_optimizer.generate.vectorize import (
    PerCutterExport,
    _format_material_tag,
    _format_plate_prefix,
    _format_text_layer,
    _is_structural_stem,
    _material_plate_counts,
    _parse_plt_stem,
    export_per_cutter_plts,
)


class TestExportAndOptimizePhase3:
    """Tests for the Phase 3 per-cutter export pipeline."""

    def test_export_phase3_per_cutter_files(self, tmp_path: Path) -> None:
        """Phase 3 export writes per-cutter PLT files under plt/."""
        job = parse_yaml("tests_deps/test123_spec.yaml")
        resolved_labels = resolve_job_spec(job)

        result = export_per_cutter_plts(
            resolved_labels,
            output_dir=tmp_path,
            optimize=False,
            job_id="job123",
            plots=False,
        )
        exported_paths = result.plt_paths

        # Verify output: every text cutter + the borders-holes group.
        assert len(exported_paths) > 0
        for path in exported_paths:
            assert path.parent == tmp_path / "plt"
            content = path.read_text()
            assert content.startswith("IN;PA;")
            assert content.endswith("SP;\n")
            assert len(content) > 50  # Has actual content

        # test123 uses a single 0.5in text height (ideal cutter 0.06in,
        # no inventory snapping) plus borders. Names are
        # [<plate>_][<material>_]<cutter>_<kind>_<job_id>.plt and this job
        # packs onto one material-less plate, so both prefixes are omitted.
        names = sorted(p.name for p in exported_paths)
        assert any(name.startswith("0.060_txt_") for name in names)
        assert any(name.startswith("0.015_bh_") for name in names)
        # The combined PLT is never written to disk.
        assert not any("_all_" in name for name in names)
        # Single-plate jobs carry NO plate number (its presence is the
        # multi-sheet signal); every file ends with the job id.
        assert all(not name.startswith("01_") for name in names)
        assert all(name.endswith("_job123.plt") for name in names)

    def test_export_phase3_no_plots_by_default(self, tmp_path: Path) -> None:
        """Phase 3 export writes no PDFs unless plots=True."""
        job = parse_yaml("tests_deps/test123_spec.yaml")
        resolved_labels = resolve_job_spec(job)

        export_per_cutter_plts(
            resolved_labels,
            output_dir=tmp_path,
            optimize=False,
            plots=False,
        )

        assert not (tmp_path / "pdf").exists() or not list((tmp_path / "pdf").iterdir())

    def test_export_per_cutter_plots(self, tmp_path: Path) -> None:
        """plots=True writes simple PDFs mirroring PLT names + all_*.pdf."""
        job = parse_yaml("tests_deps/test123_spec.yaml")
        resolved_labels = resolve_job_spec(job)

        result = export_per_cutter_plts(
            resolved_labels,
            output_dir=tmp_path,
            job_id="plotjob",
            optimize=False,
            plots=True,
        )

        assert isinstance(result, PerCutterExport)
        assert result.plt_paths
        assert result.pdf_paths
        pdf_names = sorted(p.name for p in result.pdf_paths)
        # One PDF per PLT (same stem).
        for plt_path in result.plt_paths:
            assert f"{plt_path.stem}.pdf" in pdf_names
        # One combined [<plate>_][<material>_]all_<job_id>.pdf per plate
        # (single material-less plate -> the bare `all_` name).
        assert any(name.endswith("all_plotjob.pdf") for name in pdf_names)
        # Combined content is exposed in memory, never written as PLT.
        assert result.combined_by_plate
        assert not any("_all_" in p.stem for p in result.plt_paths)

    def test_export_exposes_rendered_labels(self, tmp_path: Path) -> None:
        """The export exposes the render cache keyed by label ID.

        ``PerCutterExport.rendered_labels`` lets callers read per-line
        ``compression_by_line`` and collision state without re-rendering.
        """
        job = parse_yaml("tests_deps/test123_spec.yaml")
        resolved_labels = resolve_job_spec(job)

        result = export_per_cutter_plts(
            resolved_labels,
            output_dir=tmp_path,
            job_id="cachejob",
            optimize=False,
            plots=False,
        )

        assert result.rendered_labels
        # Keyed by label ID: every unique resolved label id is present.
        assert set(result.rendered_labels) == {label.id for label in resolved_labels}
        for label_id, rendered in result.rendered_labels.items():
            assert rendered.source_label.id == label_id
            # The compression report is a per-line scale below 1.0 (or empty).
            for line_index, scale in rendered.compression_by_line.items():
                assert 0.0 <= scale < 1.0
                assert 0 <= line_index < len(rendered.source_label.content)
            # The spacing report carries every effective gap (non-negative),
            # keyed by the upper line of the gap (never the last line).
            for line_index, spacing in rendered.line_spacing_by_line.items():
                assert spacing >= 0.0
                assert 0 <= line_index < len(rendered.source_label.content) - 1

    def test_export_per_cutter_no_default_plots_by_default(self, tmp_path: Path) -> None:
        """Color-coded *_default.pdf plots are opt-in; absent by default."""
        job = parse_yaml("tests_deps/test123_spec.yaml")
        resolved_labels = resolve_job_spec(job)

        result = export_per_cutter_plts(
            resolved_labels,
            output_dir=tmp_path,
            job_id="j",
            optimize=False,
            plots=True,
        )

        assert result.default_pdf_paths == []
        assert not any(p.name.endswith("_default.pdf") for p in result.pdf_paths)
        pdf_dir = tmp_path / "pdf"
        assert not pdf_dir.exists() or not any(
            p.name.endswith("_default.pdf") for p in pdf_dir.iterdir()
        )

    def test_export_per_cutter_default_plots_opt_in(self, tmp_path: Path) -> None:
        """default_plots=True writes *_default.pdf per PLT plus all_*_default.pdf."""
        job = parse_yaml("tests_deps/test123_spec.yaml")
        resolved_labels = resolve_job_spec(job)

        result = export_per_cutter_plts(
            resolved_labels,
            output_dir=tmp_path,
            job_id="dp",
            optimize=False,
            plots=False,
            default_plots=True,
        )

        names = sorted(p.name for p in result.default_pdf_paths)
        # One color plot per written PLT (same stem + _default).
        for plt_path in result.plt_paths:
            assert f"{plt_path.stem}_default.pdf" in names
        # Combined color plot mirrors the [<plate>_][<material>_]all_<job_id>.pdf
        # name (single material-less plate -> `all_dp.pdf`).
        assert any("all_dp_" in name and name.endswith("_default.pdf") for name in names)
        assert all(p.parent == tmp_path / "pdf" for p in result.default_pdf_paths)
        # Opt-in plots are tracked separately from the simple previews.
        assert result.pdf_paths == []

    def test_write_default_plots_skips_combined_when_disabled(self, tmp_path: Path) -> None:
        """include_combined=False omits the per-plate *_all_*_default.pdf."""
        from plt_optimizer.generate.vectorize import write_default_plots

        job = parse_yaml("tests_deps/test123_spec.yaml")
        resolved_labels = resolve_job_spec(job)

        result = export_per_cutter_plts(
            resolved_labels,
            output_dir=tmp_path,
            job_id="nc",
            optimize=False,
            plots=False,
        )
        assert result.combined_by_plate  # Combined content exists in memory.

        pdf_paths = write_default_plots(tmp_path, "nc", result, include_combined=False)

        names = sorted(p.name for p in pdf_paths)
        # One color plot per written PLT (same stem + _default).
        for plt_path in result.plt_paths:
            assert f"{plt_path.stem}_default.pdf" in names
        # The combined text + borders/holes default plot is never written.
        assert not any("_all_" in name for name in names)
        pdf_dir = tmp_path / "pdf"
        assert not any("_all_" in p.name for p in pdf_dir.iterdir())

    def test_export_simple_plots_structural_styling_wiring(self, tmp_path: Path) -> None:
        """Simple plots: bh files render structural, text/all plots do not."""
        from unittest.mock import patch

        job = parse_yaml("tests_deps/test123_spec.yaml")
        resolved_labels = resolve_job_spec(job)

        captured: list[tuple[str, bool]] = []

        def _fake_plot(document, output_path=None, show_plot=False, simple_mode=False, **kwargs):
            assert simple_mode is True
            captured.append((Path(output_path).name, bool(kwargs.get("is_structural", False))))

        with patch("plt_optimizer.diagnostics.plotter.plot_plt_document", side_effect=_fake_plot):
            result = export_per_cutter_plts(
                resolved_labels,
                output_dir=tmp_path,
                job_id="style",
                optimize=False,
                plots=True,
            )

        assert captured
        by_name = dict(captured)
        # Borders+holes files are structural; text files are not.
        assert any("_bh_" in name and flag for name, flag in by_name.items())
        assert any("_txt_" in name and not flag for name, flag in by_name.items())
        # Combined per-plate plots mix layers and stay non-structural. The
        # combined name is `[<plate>_][<material>_]all_<job_id>.pdf`, so it
        # leads with the `all_` token (no plate prefix for a single plate).
        all_plots = [flag for name, flag in by_name.items() if name.startswith("all_")]
        assert all_plots and not any(all_plots)
        # The fake never saves; PDFs are tracked as usual.
        assert result.pdf_paths

    def test_export_per_cutter_skips_empty_groups(self, tmp_path: Path) -> None:
        """A job without holes still gets a borders file; no empty text files."""
        job = parse_yaml("tests_deps/test123_spec.yaml")
        resolved_labels = resolve_job_spec(job)

        result = export_per_cutter_plts(
            resolved_labels,
            output_dir=tmp_path,
            job_id="j",
            optimize=False,
            plots=False,
        )

        # Borders exist for every label, so the structural (_bh_) file is written.
        assert any("_bh_" in p.name for p in result.plt_paths)
        # Every written file contains geometry.
        for path in result.plt_paths:
            assert "PD" in path.read_text()

    def test_export_per_cutter_multi_cutter_naming(self, tmp_path: Path) -> None:
        """Distinct text cutters produce one text file per cutter diameter.

        Parsing goes through :func:`_parse_plt_stem` so the optional plate
        and material prefixes stay irrelevant to the cutter extraction.
        """
        job = parse_yaml("tests_deps/complex_test_job.yaml")
        resolved_labels = resolve_job_spec(job)

        result = export_per_cutter_plts(
            resolved_labels,
            job.plates,
            output_dir=tmp_path,
            job_id="complex",
            optimize=False,
            plots=False,
        )

        text_files = [p.name for p in result.plt_paths if "_txt_" in p.name]
        # Name shape: [<plate>_][<material>_]<cutter>[_<color>]_txt_<job_id>.plt
        cutter_tags = {_parse_plt_stem(name).cutter for name in text_files}
        # complex_test_job exercises at least three distinct text cutters.
        assert len(cutter_tags) >= 3
        # Every tag is a 3-decimal inch string.
        for tag in cutter_tags:
            assert tag is not None
            major, _, minor = tag.partition(".")
            assert major.isdigit() and len(minor) == 3

    def test_export_per_cutter_color_split_files(self, tmp_path: Path) -> None:
        """complex_test_job's color_split_tag label exports per-color toolpaths.

        The label's three lines share one cutter (text_height 0.3 -> 0.040")
        but carry different ``text_color`` tags, so the export must split them
        into three (cutter, color) layers: the black/magenta lines gain
        1-letter suffixes while the untagged line merges into the historical
        cutter-only ``0.040`` file.
        """
        job = parse_yaml("tests_deps/complex_test_job.yaml")
        resolved_labels = resolve_job_spec(job)

        result = export_per_cutter_plts(
            resolved_labels,
            job.plates,
            output_dir=tmp_path,
            job_id="complex",
            optimize=False,
            plots=False,
        )

        text_files = sorted(p.name for p in result.plt_paths if "_txt_" in p.name)
        # The colored layers appear as additional tagged files (the plate
        # prefix depends on where the packer places the label, so match on
        # the suffix).
        assert any(name.endswith("0.040_k_txt_complex.plt") for name in text_files)
        assert any(name.endswith("0.040_m_txt_complex.plt") for name in text_files)
        # The implicit "none" layer keeps the bare cutter-only tag.
        assert any(name.endswith("0.040_txt_complex.plt") for name in text_files)
        # The two colored files are *additional* toolpaths: each carries only
        # its own layer's strokes on a single pen.
        black_file = next(p for p in result.plt_paths if p.name.endswith("0.040_k_txt_complex.plt"))
        magenta_file = next(
            p for p in result.plt_paths if p.name.endswith("0.040_m_txt_complex.plt")
        )
        black_content = black_file.read_text(encoding="utf-8")
        magenta_content = magenta_file.read_text(encoding="utf-8")
        # Per-cutter files are pen-select-free single-tool streams; the
        # layers stay separate because each file carries ONLY its own
        # strokes -- the two color layers share no cutting coordinates.
        assert not re.search(r"SP\d", black_content)
        assert not re.search(r"SP\d", magenta_content)
        black_points = _cutting_points(black_content)
        magenta_points = _cutting_points(magenta_content)
        assert black_points and magenta_points
        assert black_points.isdisjoint(magenta_points)
        # Both files carry geometry.
        assert re.search(r"(?:PU|PD)\d", black_content)
        assert re.search(r"(?:PU|PD)\d", magenta_content)

    def test_export_phase3_file_organization(self, tmp_path: Path) -> None:
        """All Phase 3 output files live under <output_dir>/plt/."""
        job = parse_yaml("tests_deps/test123_spec.yaml")
        resolved_labels = resolve_job_spec(job)

        result = export_per_cutter_plts(
            resolved_labels,
            output_dir=tmp_path,
            optimize=False,
            job_id="org",
            plots=False,
        )
        exported_paths = result.plt_paths

        for path in exported_paths:
            assert path.parent == tmp_path / "plt"
            assert path.name.endswith(".plt")


def _label(
    label_id: str = "lbl",
    width: float = 3.0,
    height: float = 1.0,
    count: int = 1,
    material: Optional[str] = None,
) -> ResolvedLabel:
    """Build a minimal ``ResolvedLabel`` for export tests."""
    return ResolvedLabel(
        id=label_id,
        count=count,
        width=width,
        height=height,
        margin=0.0,
        h_margin=0.0,
        v_margin=0.0,
        material=material,
        content=[
            ResolvedTextLine(
                text="X",
                nominal_text_height=0.5,
                toolpath_text_height=0.47,
                cutter_diameter=0.03,
                character_spacing=0.0,
                line_spacing=0.0,
            )
        ],
    )


def _first_cutting_x(path: Path) -> int:
    """Return the smallest X coordinate among the cutting moves of a PLT."""
    xs: list[int] = []
    for token in path.read_text(encoding="utf-8").split(";"):
        match = re.match(r"^PD(-?\d+),(-?\d+)", token.strip())
        if match:
            xs.append(int(match.group(1)))
    assert xs, f"No cutting coordinates found in {path}"
    return min(xs)


def _cutting_points(content: str) -> set[tuple[int, int]]:
    """Collect every coordinate pair touched by a pen-down command.

    Per-cutter files no longer carry ``SP`` pen selects, so color/layer
    separation is verified by geometry instead: strokes belonging to
    different text lines occupy disjoint coordinates.

    Args:
        content: Raw HPGL text of one written PLT file.

    Returns:
        Set of ``(x, y)`` plotter-unit pairs appearing in ``PD`` commands.
    """
    points: set[tuple[int, int]] = set()
    for token in content.split(";"):
        token = token.strip()
        if not token.startswith("PD"):
            continue
        values = token[2:].split(",")
        for i in range(0, len(values) - 1, 2):
            try:
                points.add((int(values[i]), int(values[i + 1])))
            except ValueError:
                continue
    return points


class TestDeviceScaleRegression:
    """Generated toolpaths must land on the HPGL device scale (1016 units/inch).

    Regression: the pipeline emitted 1000 units/inch, so every label engraved
    1.6% undersized (a 16x24in label came out 15.75 x 23.594in). HPGL's default
    step size is 0.025 mm, i.e. 25.4/0.025 = 1016 units per inch, which is what
    the shop's plotter applies (the ``IN;PA;`` header ships no scale command).
    """

    @staticmethod
    def _all_coordinates(content: str) -> list[tuple[int, int]]:
        """Every ``(x, y)`` pair emitted by a PU/PD command in one PLT."""
        points: list[tuple[int, int]] = []
        for token in content.split(";"):
            token = token.strip()
            match = re.match(r"^(?:PU|PD)(.*)$", token)
            if not match:
                continue
            values = match.group(1).split(",")
            for i in range(0, len(values) - 1, 2):
                try:
                    points.append((int(values[i]), int(values[i + 1])))
                except ValueError:
                    continue
        return points

    @staticmethod
    def _span(content: str) -> tuple[int, int]:
        """Return the ``(width, height)`` in plotter units of all emitted geometry."""
        points = TestDeviceScaleRegression._all_coordinates(content)
        assert points, "Rendered label carries no coordinates"
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return max(xs) - min(xs), max(ys) - min(ys)

    def test_large_label_spans_nominal_inches(self) -> None:
        """A 24x16in label emits exactly 24*1016 x 16*1016 plotter units.

        This is the reported symptom: at the old 1000 units/inch the same label
        spanned 24000 x 16000 units, i.e. 0.40625in short in X and 0.25in short
        in Y once the plotter applied its 1016 scale.
        """
        from plt_optimizer.generate.label_renderer import render_label_to_plt

        label = _label(label_id="big", width=24.0, height=16.0)
        rendered = render_label_to_plt(label)

        width_units, height_units = self._span(rendered.plt_content)
        assert width_units == pytest.approx(24 * HPGL_UNITS_PER_INCH, abs=1)
        assert height_units == pytest.approx(16 * HPGL_UNITS_PER_INCH, abs=1)

    def test_three_by_two_cell_matches_reference_grid(self) -> None:
        """A 3x2in label spans 3048 x 2032 units, like the EngraveLab sheet.

        ``examples/test_rect_grid13sheet0.txt`` (EngraveLab reference output)
        draws its 3x2in grid cells as exactly 3048 x 2032 units -- the proof
        that the shop's plotter runs at 1016 units/inch. Our emission must
        agree, cell for cell.
        """
        from plt_optimizer.generate.label_renderer import render_label_to_plt

        label = _label(label_id="cell", width=3.0, height=2.0)
        rendered = render_label_to_plt(label)

        assert self._span(rendered.plt_content) == (3048, 2032)

    def test_bounds_round_trip_reports_nominal_inches(self) -> None:
        """``RenderedLabel`` bounds equal the nominal size under the device scale."""
        from plt_optimizer.generate.label_renderer import render_label_to_plt

        label = _label(label_id="rt", width=24.0, height=16.0)
        rendered = render_label_to_plt(label)

        assert rendered.width == pytest.approx(24.0, abs=1e-3)
        assert rendered.height == pytest.approx(16.0, abs=1e-3)


class TestExportWithoutPlates:
    """A job spec with no ``plates:`` uses the configured default plate."""

    def test_no_plates_writes_output(self, tmp_path: Path) -> None:
        """Omitting plates still exports (unbounded default plates)."""
        result = export_per_cutter_plts(
            [_label(count=3)],
            provided_plates=None,
            output_dir=tmp_path,
            job_id="noplate",
            optimize=False,
            plots=False,
        )

        assert result.plt_paths
        assert all(path.parent == tmp_path / "plt" for path in result.plt_paths)

    def test_no_plates_overflows_onto_several_default_plates(self, tmp_path: Path) -> None:
        """Labels beyond one default sheet spill onto additional plates."""
        # 40 labels of 3x1 do not fit a single 12x8 default sheet.
        result = export_per_cutter_plts(
            [_label(count=40)],
            provided_plates=None,
            output_dir=tmp_path,
            job_id="overflow",
            optimize=False,
            plots=False,
            layout=LayoutMode.ROWS,
            default_plate_size=(12.0, 8.0),
        )

        plate_numbers = {path.name.split("_")[0] for path in result.plt_paths}
        assert len(plate_numbers) >= 2
        # Names stay index-based (01, 02, ...) rather than bin-id based.
        assert "01" in plate_numbers

    def test_default_plate_size_reaches_the_packer(self, tmp_path: Path) -> None:
        """A larger configured default plate packs everything on one sheet."""
        small = export_per_cutter_plts(
            [_label(width=10.0, height=6.0, count=3)],
            output_dir=tmp_path / "small",
            job_id="size",
            optimize=False,
            plots=False,
            layout=LayoutMode.ROWS,
            allow_rotation=False,
            default_plate_size=(12.0, 8.0),
        )
        large = export_per_cutter_plts(
            [_label(width=10.0, height=6.0, count=3)],
            output_dir=tmp_path / "large",
            job_id="size",
            optimize=False,
            plots=False,
            layout=LayoutMode.ROWS,
            allow_rotation=False,
            default_plate_size=(48.0, 40.0),
        )

        # The plate token is absent for single-plate jobs, present per sheet
        # for multi-plate ones, so it counts sheets exactly.
        small_plates = {_parse_plt_stem(p.stem).plate for p in small.plt_paths}
        large_plates = {_parse_plt_stem(p.stem).plate for p in large.plt_paths}
        assert len(small_plates) > 1
        assert len(large_plates) == 1

    def test_default_plate_clearance_shifts_content(self, tmp_path: Path) -> None:
        """The configured clearance offsets content on auto-allocated plates."""
        baseline = export_per_cutter_plts(
            [_label(count=2)],
            output_dir=tmp_path / "flush",
            job_id="clr",
            optimize=False,
            plots=False,
            layout=LayoutMode.ROWS,
            allow_rotation=False,
        )
        shifted = export_per_cutter_plts(
            [_label(count=2)],
            output_dir=tmp_path / "shifted",
            job_id="clr",
            optimize=False,
            plots=False,
            layout=LayoutMode.ROWS,
            allow_rotation=False,
            default_plate_clearance=(1.0, 2.0),
        )

        # One inch == the full plotter scale; the left-most cut moves right by 1".
        before = min(_first_cutting_x(p) for p in baseline.plt_paths if "_bh_" in p.name)
        after = min(_first_cutting_x(p) for p in shifted.plt_paths if "_bh_" in p.name)
        assert after - before == int(HPGL_UNITS_PER_INCH)

    def test_empty_plate_list_is_unbounded(self, tmp_path: Path) -> None:
        """An explicit empty plate list behaves like omitting plates."""
        result = export_per_cutter_plts(
            [_label(count=2)],
            provided_plates=[],
            output_dir=tmp_path,
            job_id="empty",
            optimize=False,
            plots=False,
        )

        assert result.plt_paths


def _two_color_label(label_id: str = "twocolor") -> ResolvedLabel:
    """Build a label with two identical lines in different stroke colors."""
    return ResolvedLabel(
        id=label_id,
        count=1,
        width=3.0,
        height=1.5,
        margin=0.1,
        h_margin=0.1,
        v_margin=0.1,
        content=[
            ResolvedTextLine(
                text="LAYER ONE",
                nominal_text_height=0.5,
                toolpath_text_height=0.47,
                cutter_diameter=0.06,
                character_spacing=0.0,
                line_spacing=0.0,
                text_color="magenta",
            ),
            ResolvedTextLine(
                text="LAYER TWO",
                nominal_text_height=0.5,
                toolpath_text_height=0.47,
                cutter_diameter=0.06,
                character_spacing=0.0,
                line_spacing=0.0,
                text_color="black",
            ),
        ],
    )


class TestExportTextColorSplit:
    """text_color splits one cutter's text into separate per-color files."""

    def test_same_cutter_colors_split_into_two_files(self, tmp_path: Path) -> None:
        """Two colors on one cutter produce two suffixed text PLTs."""
        result = export_per_cutter_plts(
            [_two_color_label()],
            output_dir=tmp_path,
            job_id="clr",
            optimize=False,
            plots=False,
        )

        text_names = sorted(p.name for p in result.plt_paths if "_txt_" in p.name)
        # (cutter, color) sort order: black -> pen 1, magenta -> pen 4. A
        # single material-less plate carries no prefix at all.
        assert text_names == [
            "0.060_k_txt_clr.plt",
            "0.060_m_txt_clr.plt",
        ]
        # Each file carries only its own layer's strokes: the two color
        # layers occupy disjoint cutting coordinates (no SP selects remain
        # in per-cutter output to distinguish them).
        contents = {
            name: (tmp_path / "plt" / name).read_text(encoding="utf-8") for name in text_names
        }
        for content in contents.values():
            assert not re.search(r"SP\d", content)
            assert re.search(r"(?:PU|PD)\d", content)
        points = {name: _cutting_points(content) for name, content in contents.items()}
        assert len(points) == 2
        (k_points, m_points) = (points[name] for name in sorted(points))
        assert k_points and m_points
        assert k_points.isdisjoint(m_points)

    def test_colored_export_keeps_structural_file_untagged(self, tmp_path: Path) -> None:
        """The bh (borders + holes) file name never gains a color suffix."""
        result = export_per_cutter_plts(
            [_two_color_label()],
            output_dir=tmp_path,
            job_id="clr",
            optimize=False,
            plots=False,
        )
        bh_names = [p.name for p in result.plt_paths if "_bh_" in p.name]
        assert bh_names == ["0.015_bh_clr.plt"]

    def test_colorless_export_names_are_unchanged(self, tmp_path: Path) -> None:
        """Jobs without colors keep the bare cutter-only text tag."""
        result = export_per_cutter_plts(
            [_label()],
            output_dir=tmp_path,
            job_id="plain",
            optimize=False,
            plots=False,
        )
        text_names = sorted(p.name for p in result.plt_paths if "_txt_" in p.name)
        assert text_names == ["0.030_txt_plain.plt"]

    def test_colored_export_optimizes_each_color_layer(self, tmp_path: Path) -> None:
        """Optimized export routes and writes both color layers separately."""
        result = export_per_cutter_plts(
            [_two_color_label()],
            output_dir=tmp_path,
            job_id="opt",
            optimize=True,
            fast_mode=True,
            plots=False,
        )
        text_names = sorted(p.name for p in result.plt_paths if "_txt_" in p.name)
        assert text_names == [
            "0.060_k_txt_opt.plt",
            "0.060_m_txt_opt.plt",
        ]
        for name in text_names:
            content = (tmp_path / "plt" / name).read_text(encoding="utf-8")
            assert content.startswith("IN;PA;")
            assert re.search(r"(?:PU|PD)\d", content)


class TestLayerReport:
    """The per-layer rapid-travel INFO line emitted by the export."""

    @staticmethod
    def _multiline_label() -> ResolvedLabel:
        """A four-line label whose tour leaves a chunk worth reversing.

        Hand-picked (random search over the real NearestNeighbor + 2-Opt
        path) so the plate-space sweep fires: inter-chunk travel drops
        1427.785 -> 1403.864 and emitted travel 6391.220 -> 6367.300.

        ``use_baseline_spacing=False`` pins the ink-box stacking the tour was
        tuned under: baseline spacing shifts each line's Y, which reshuffles
        the rapid-travel gains and silences the direction sweep this test
        asserts on. The report line's format is what is under test here.

        ``auto_descending_character_adjustment=False`` is required for the same
        reason: this label is a synthetic routing fixture (zero margins, four
        lines of 0.4in on a 1.6in sheet), so its spacing is fully clamped and
        the 'Q' descenders cannot be relieved -- the relief would abort the
        render, and its Y transform would move the tuned tour numbers.
        """
        return ResolvedLabel(
            id="lbl",
            count=1,
            width=3.0,
            height=1.6,
            margin=0.0,
            h_margin=0.0,
            v_margin=0.0,
            material=None,
            use_baseline_spacing=False,
            auto_descending_character_adjustment=False,
            content=[
                ResolvedTextLine(
                    text=text,
                    nominal_text_height=0.4,
                    toolpath_text_height=0.38,
                    cutter_diameter=0.03,
                    character_spacing=0.0,
                    line_spacing=0.05,
                )
                for text in ("QOW", "TQZ", "Q", "TOWLX")
            ],
        )

    def test_report_compares_emitted_travel_and_names_the_sweep(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The line compares emitted-vs-emitted and names the sweep stage.

        The historical line compared the emitted baseline (intra + inter)
        against the strategies' inter-chunk-only metric, which under-reported
        the optimized side by the whole intra-chunk total.
        """
        from plt_optimizer.utils.logging import TextLogger

        logger = TextLogger(name="plt_optimizer_test_report", log_file=tmp_path / "r.log")
        with caplog.at_level(logging.INFO, logger="plt_optimizer_test_report"):
            export_per_cutter_plts(
                [self._multiline_label()],
                output_dir=tmp_path,
                job_id="rep",
                optimize=True,
                fast_mode=True,
                plots=False,
                logger=logger,
            )

        lines = [
            r.getMessage()
            for r in caplog.records
            if "rapid travel" in r.getMessage() and r.getMessage().startswith("Plate ")
        ]
        assert lines, "expected a per-layer rapid-travel report"
        # Text layers are reported as "Plate <n> text <cutter>: ...".
        text_lines = [line for line in lines if " text " in line.split(":")[0]]
        assert text_lines, lines
        line = text_lines[0]
        # Emitted (intra + inter) on both sides of the arrow.
        match = re.search(r"rapid travel ([\d.]+) -> ([\d.]+) plotter units", line)
        assert match, line
        assert float(match.group(2)) <= float(match.group(1)) + 1e-6
        # The two stages are broken out explicitly.
        assert "routing " in line and "inter-chunk " in line
        assert "direction sweep " in line


class TestFormatTextLayer:
    """Unit tests for the (cutter, color) file-name formatter."""

    def test_none_color_keeps_cutter_only_name(self) -> None:
        """The implicit color produces the historical cutter-only tag."""
        assert _format_text_layer(0.04, "none") == "0.040"

    def test_known_colors_use_abbreviation(self) -> None:
        """Real colors gain their single-letter file-name tag."""
        assert _format_text_layer(0.04, "magenta") == "0.040_m"
        assert _format_text_layer(0.06, "black") == "0.060_k"

    def test_unknown_color_falls_back_to_sanitized_value(self) -> None:
        """Manually constructed unknown colors stay filesystem-safe."""
        assert _format_text_layer(0.04, "chartreuse") == "0.040_chartreuse"
        assert _format_text_layer(0.04, "bright-red!") == "0.040_brightred"
        # Fully sanitized-empty values never produce a dangling separator.
        assert _format_text_layer(0.04, "!!!") == "0.040_x"


class TestTextColorDemoExample:
    """Regression tests for tests_deps/text_color_demo_job.yaml.

    The fixture is the hand-crafted stroke-color demo: three identical-
    typography lines (one per stroke color: black, magenta, implicit none)
    must split into three separate text toolpaths per plate, with the
    colorless layer keeping the historical file name and the colored ones
    gaining their 1-letter suffixes.
    """

    def _resolve(self) -> object:
        """Parse and resolve the stroke-color demo job."""
        from plt_optimizer.generate.schema import parse_yaml

        job = parse_yaml(Path("tests_deps/text_color_demo_job.yaml"))
        return resolve_job_spec(job)

    def test_fixture_resolves_three_colors(self) -> None:
        """Lines resolve to black / magenta / none in declaration order."""
        labels = self._resolve()
        assert len(labels) == 1
        assert [line.text_color for line in labels[0].content] == [
            "black",
            "magenta",
            "none",
        ]

    def test_fixture_exports_one_file_per_color(self, tmp_path: Path) -> None:
        """The demo exports three suffixed text files plus the bh file."""
        from plt_optimizer.generate.schema import parse_yaml

        job = parse_yaml(Path("tests_deps/text_color_demo_job.yaml"))
        labels = resolve_job_spec(job)
        result = export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="demo",
            optimize=False,
            plots=False,
        )
        text_names = sorted(p.name for p in result.plt_paths if "_txt_" in p.name)
        # Single 0.375in text height -> one cutter (0.045) split three ways.
        # One material-less plate -> no plate/material prefix.
        assert text_names == [
            "0.045_k_txt_demo.plt",
            "0.045_m_txt_demo.plt",
            "0.045_txt_demo.plt",
        ]
        assert [p.name for p in result.plt_paths if "_bh_" in p.name] == ["0.015_bh_demo.plt"]
        # Each text file carries only its own layer's strokes: the three
        # color layers occupy pairwise-disjoint cutting coordinates (the
        # per-cutter files carry no SP selects to key on).
        points_per_file = []
        for path in result.plt_paths:
            if "_txt_" not in path.name:
                continue
            content = path.read_text(encoding="utf-8")
            assert not re.search(r"SP\d", content), f"{path.name} carries pen selects"
            assert re.search(r"(?:PU|PD)\d", content)
            points_per_file.append(_cutting_points(content))
        assert len(points_per_file) == 3
        for i, first in enumerate(points_per_file):
            assert first, "text file carries no cutting geometry"
            for second in points_per_file[i + 1 :]:
                assert first.isdisjoint(second), "color layers share coordinates"


class TestPlateFilenameFormatting:
    """Unit tests for the [<plate>_][<material>_]<cutter>_<kind> naming."""

    def test_material_tag_sanitizes(self) -> None:
        """Free-form material names become filesystem-safe tags."""
        assert _format_material_tag(None) == ""
        assert _format_material_tag("wb") == "wb"
        assert _format_material_tag("wb(uv)") == "wbuv"
        assert _format_material_tag("3-layer black") == "3layerblack"
        # Fully sanitized-empty values never produce a dangling separator.
        assert _format_material_tag("---") == "x"

    def test_single_plate_material_omits_plate_number(self) -> None:
        """One plate for a material -> no plate prefix (material tag suffices)."""
        assert _format_plate_prefix(1, 1) == ""
        assert _format_plate_prefix(1, 1, "wb") == "wb_"
        # A material-less job is a single group, so a lone sheet is bare.
        assert _format_plate_prefix(2, 1) == ""

    def test_multi_plate_material_includes_plate_number(self) -> None:
        """A material spanning several plates keeps the padded plate number."""
        assert _format_plate_prefix(2, 3) == "02_"
        assert _format_plate_prefix(2, 3, "wb(uv)") == "02_wbuv_"

    def test_material_plate_counts_groups_by_material(self) -> None:
        """Plate numbers are scoped to a material group, not the whole job.

        Two `wb` plates and one `wb(uv)` plate: the uv sheet is uniquely
        identified by its material tag, so it needs no plate number, while
        the two wb sheets must be numbered to stay distinct.
        """
        counts = _material_plate_counts({1: "wb", 2: "wb(uv)", 3: "WB"})
        assert counts == {1: 2, 2: 1, 3: 2}
        assert _format_plate_prefix(1, counts[1], "wb") == "01_wb_"
        assert _format_plate_prefix(2, counts[2], "wb(uv)") == "wbuv_"
        assert _format_plate_prefix(3, counts[3], "WB") == "03_WB_"

    def test_material_plate_counts_treats_null_as_one_group(self) -> None:
        """Material-agnostic plates share one group, keeping plate numbers."""
        counts = _material_plate_counts({1: None, 2: None})
        assert counts == {1: 2, 2: 2}
        assert _format_plate_prefix(2, counts[2]) == "02_"

    def test_stem_round_trip(self) -> None:
        """Every generated shape parses back to its components."""
        cases = {
            "0.040_txt_job": (None, None, "0.040", "txt", None),
            "0.040_m_txt_job": (None, None, "0.040", "txt", "m"),
            "0.015_bh_job": (None, None, "0.015", "bh", None),
            "wbuv_0.040_txt_job": (None, "wbuv", "0.040", "txt", None),
            "wbuv_0.015_bh_job": (None, "wbuv", "0.015", "bh", None),
            "02_wbuv_0.040_k_txt_job": ("02", "wbuv", "0.040", "txt", "k"),
            "02_0.040_txt_job": ("02", None, "0.040", "txt", None),
            "02_0.015_bh_job": ("02", None, "0.015", "bh", None),
        }
        for stem, (plate, material, cutter, kind, color) in cases.items():
            parts = _parse_plt_stem(stem)
            assert (parts.plate, parts.material, parts.cutter, parts.kind, parts.color) == (
                plate,
                material,
                cutter,
                kind,
                color,
            ), stem

    def test_structural_detection_is_prefix_agnostic(self) -> None:
        """The bh kind is found regardless of plate/material prefixes."""
        assert _is_structural_stem("0.015_bh_job")
        assert _is_structural_stem("wbuv_0.015_bh_job")
        assert _is_structural_stem("02_wbuv_0.015_bh_job")
        assert not _is_structural_stem("02_wbuv_0.040_txt_job")
        # Stems outside the scheme are treated as non-structural.
        assert not _is_structural_stem("readme")

    def test_plot_title_includes_material(self) -> None:
        """Titles name the cutter, the kind and the material tag."""
        from plt_optimizer.generate.vectorize import _build_plot_title

        base = _build_plot_title("Job", Path("plt/0.040_txt_job.plt"), text_height=0.5)
        assert base == "Job 0.5 text (0.040 cutter)"
        tagged = _build_plot_title("Job", Path("plt/wbuv_0.015_bh_job.plt"))
        assert tagged == "Job borders and holes (0.015 cutter) [wbuv]"
        unknown = _build_plot_title("Job", Path("plt/mystery.plt"))
        assert unknown == "Job mystery"


class TestMaterialDemoExample:
    """Regression tests for tests_deps/material_demo_job.yaml.

    The fixture is the hand-crafted mixed-stock demo: two materials, one
    plate each, plus a magenta text layer on the uv sheet. It pins the
    material-tagged filename scheme
    (``[<plate>_][<material>_]<cutter>[_<color>]_<kind>_<job>.plt``):
    because each material spans exactly one plate, the material tag alone
    identifies every sheet and the plate numbers are omitted.
    """

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the material demo job with plotting disabled."""
        from plt_optimizer.generate.schema import parse_yaml

        job = parse_yaml(Path("tests_deps/material_demo_job.yaml"))
        labels = resolve_job_spec(job)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="demo",
            optimize=False,
            plots=False,
        )

    def test_fixture_exports_material_tagged_names(self, tmp_path: Path) -> None:
        """Every file carries its sanitized material tag and no plate number."""
        result = self._export(tmp_path)
        assert sorted(p.name for p in result.plt_paths) == [
            "wb_0.015_bh_demo.plt",
            "wb_0.045_txt_demo.plt",
            "wbuv_0.015_bh_demo.plt",
            "wbuv_0.045_m_txt_demo.plt",
            "wbuv_0.045_txt_demo.plt",
        ]
        assert result.material_by_plate == {1: "wb", 2: "wb(uv)"}
        for path in result.plt_paths:
            parts = _parse_plt_stem(path.stem)
            # One plate per material -> the material tag is unambiguous, so
            # the plate number is omitted entirely.
            assert parts.plate is None
            assert parts.material in {"wb", "wbuv"}

    def test_fixture_materials_never_share_a_file(self, tmp_path: Path) -> None:
        """Each material's sheets get their own text layers, never merged."""
        result = self._export(tmp_path)
        # The wb sheet has a single colorless text layer; the uv sheet has
        # two (colorless placards + magenta deep-engraved placards).
        wb_txt = [p for p in result.plt_paths if p.name == "wb_0.045_txt_demo.plt"]
        uv_txt = [p for p in result.plt_paths if p.name.startswith("wbuv_0.045")]
        assert len(wb_txt) == 1
        assert len(uv_txt) == 2  # plain + magenta deep layer


class TestCutterSizeDemoExample:
    """Regression tests for tests_deps/cutter_size_job.yaml.

    The fixture mixes an auto-selected cutter (0.5in text -> 0.06in) with a
    label-level override (0.045in) and a line-level override (0.03in). It
    pins that an explicit ``cutter_size`` produces its own per-cutter text
    layer/file, exactly like a height-driven cutter, and that the nominal
    text height (hence the vertical fit) is untouched.
    """

    def _resolve(self) -> list[ResolvedLabel]:
        """Resolve the cutter-size demo fixture."""
        job = parse_yaml(Path("tests_deps/cutter_size_job.yaml"))
        return resolve_job_spec(job)

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the cutter-size demo job with plotting disabled."""
        job = parse_yaml(Path("tests_deps/cutter_size_job.yaml"))
        labels = resolve_job_spec(job)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="cs",
            optimize=False,
            plots=False,
        )

    def test_resolution_applies_explicit_cutters(self) -> None:
        """Explicit cutters replace the lookup; nominal height is untouched."""
        by_id = {label.id: label for label in self._resolve()}

        auto = by_id["auto_cutter"].content[0]
        assert auto.cutter_size is None
        assert math.isclose(auto.cutter_diameter, 0.06)

        label_line = by_id["label_cutter"].content[0]
        assert math.isclose(label_line.cutter_size, 0.045)
        assert math.isclose(label_line.cutter_diameter, 0.045)
        assert math.isclose(label_line.toolpath_text_height, 0.5 - 0.045)

        line, inherited = by_id["line_cutter"].content
        assert math.isclose(line.cutter_diameter, 0.03)
        assert math.isclose(inherited.cutter_diameter, 0.045)
        # The nominal height drives vertical fit and never changes.
        for resolved in (auto, label_line, line, inherited):
            assert math.isclose(resolved.nominal_text_height, 0.5)

    def test_fixture_exports_one_file_per_explicit_cutter(self, tmp_path: Path) -> None:
        """Each distinct cutter (explicit or auto) becomes its own text file."""
        result = self._export(tmp_path)
        assert sorted(p.name for p in result.plt_paths) == [
            "0.015_bh_cs.plt",
            "0.030_txt_cs.plt",  # line-level cutter_size
            "0.045_txt_cs.plt",  # label-level cutter_size (+ inherited line)
            "0.060_txt_cs.plt",  # automatic selection from text_height
        ]

    def test_cutter_at_text_height_aborts_the_job(self) -> None:
        """A cutter >= text_height raises CutterSizeError (no material left)."""
        from plt_optimizer.generate.resolution import CutterSizeError

        job = parse_yaml(Path("tests_deps/cutter_size_job.yaml"))
        job.labels = [
            LabelSpec(
                id="too_big",
                width=3.0,
                height=1.0,
                content=[TextLine(text="X", cutter_size=0.5)],
            )
        ]
        with pytest.raises(CutterSizeError, match="no material to engrave"):
            resolve_job_spec(job)


class TestCutterDownsizeDemoExample:
    """Regression tests for tests_deps/cutter_downsize_job.yaml.

    The fixture compresses four labels under ``max_h_compress: 0.5``: one
    crosses the 0.06 -> 0.045 midpoint and downsizes, one fits (untouched),
    one carries an explicit ``cutter_size`` (never reduced), and one opts out
    via ``cutter_downsize: false``. It pins that the reduction happens in the
    export pre-pass -- *before* the pen map is built -- so the downsized
    cutter drives its own per-cutter text file, and that the pre-pass is a
    complete no-op without a cutter inventory.
    """

    _INVENTORY = [0.03, 0.045, 0.06, 0.09, 0.125]
    _SPEC = Path("tests_deps/cutter_downsize_job.yaml")

    def _export(self, tmp_path: Path, inventory: Optional[list[float]]) -> PerCutterExport:
        """Export the demo job with plotting disabled against ``inventory``."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job, available_cutters=inventory)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="cd",
            optimize=False,
            plots=False,
            available_cutters=inventory,
        )

    def test_compressed_line_downsizes_one_step(self, tmp_path: Path) -> None:
        """The compressed line swaps 0.06in for 0.045in and re-renders taller."""
        result = self._export(tmp_path, self._INVENTORY)
        downsize = result.rendered_labels["compressed"].source_label.cutter_downsize_by_line
        assert list(downsize) == [0]
        original, final = downsize[0]
        assert math.isclose(original, 0.06)
        assert math.isclose(final, 0.045)

        line = result.rendered_labels["compressed"].source_label.content[0]
        assert math.isclose(line.cutter_diameter, 0.045)
        # Nominal height is the user's intent; only the toolpath grows.
        assert math.isclose(line.nominal_text_height, 0.5)
        assert math.isclose(line.toolpath_text_height, 0.5 - 0.045)

    def test_untouched_labels_keep_their_automatic_cutter(self, tmp_path: Path) -> None:
        """Fitting, explicit-cutter and opted-out lines are never reduced."""
        result = self._export(tmp_path, self._INVENTORY)

        fits = result.rendered_labels["fits"].source_label
        assert fits.cutter_downsize_by_line == {}
        assert math.isclose(fits.content[0].cutter_diameter, 0.06)

        explicit = result.rendered_labels["explicit"].source_label
        assert explicit.cutter_downsize_by_line == {}
        assert math.isclose(explicit.content[0].cutter_diameter, 0.09)

        opted_out = result.rendered_labels["opted_out"].source_label
        assert opted_out.cutter_downsize_by_line == {}
        assert math.isclose(opted_out.content[0].cutter_diameter, 0.06)
        # The opt-out line compresses exactly like the downsized one did.
        assert result.rendered_labels["opted_out"].compression_by_line

    def test_downsized_cutter_gets_its_own_file(self, tmp_path: Path) -> None:
        """The pen map sees the reduced cutter, so a 0.045in text file appears."""
        result = self._export(tmp_path, self._INVENTORY)
        assert sorted(p.name for p in result.plt_paths) == [
            "0.030_bh_cd.plt",  # boundary/hole cutter 0.015 snapped up to 0.030
            "0.045_txt_cd.plt",  # compressed line, downsized from 0.060
            "0.060_txt_cd.plt",  # fits + opted_out lines
            "0.090_txt_cd.plt",  # explicit cutter_size
        ]

    def test_pre_pass_is_a_noop_without_inventory(self, tmp_path: Path) -> None:
        """No tools.json inventory means no ladder: output stays historical."""
        result = self._export(tmp_path, None)
        assert sorted(p.name for p in result.plt_paths) == [
            "0.015_bh_cd.plt",
            "0.060_txt_cd.plt",
            "0.090_txt_cd.plt",
        ]
        for rendered in result.rendered_labels.values():
            assert rendered.source_label.cutter_downsize_by_line == {}


class TestCutterDownsizeGlobalDemoExample:
    """Regression tests for tests_deps/cutter_downsize_global_job.yaml.

    The fixture pins ``cutter_downsize_global`` (default true): a trigger
    line's swap is shared with the fitting siblings of the same text height
    *inside the same label*, while other labels and opted-out labels keep
    their per-line behaviour.
    """

    _INVENTORY = [0.03, 0.045, 0.06, 0.09, 0.125]
    _SPEC = Path("tests_deps/cutter_downsize_global_job.yaml")

    def _export(self, tmp_path: Path, inventory: Optional[list[float]]) -> PerCutterExport:
        """Export the demo job with plotting disabled against ``inventory``."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job, available_cutters=inventory)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="cdg",
            optimize=False,
            plots=False,
            available_cutters=inventory,
        )

    def test_fitting_sibling_receives_the_shared_cutter(self, tmp_path: Path) -> None:
        """The trigger's swap lands on its fitting same-height sibling."""
        result = self._export(tmp_path, self._INVENTORY)
        shared = result.rendered_labels["shared"].source_label
        assert sorted(shared.cutter_downsize_by_line) == [0, 1]
        for index in (0, 1):
            original, final = shared.cutter_downsize_by_line[index]
            assert math.isclose(original, 0.06)
            assert math.isclose(final, 0.045)
        for line in shared.content:
            assert math.isclose(line.cutter_diameter, 0.045)
            assert math.isclose(line.nominal_text_height, 0.5)
            assert math.isclose(line.toolpath_text_height, 0.5 - 0.045)

    def test_sharing_never_crosses_the_label_boundary(self, tmp_path: Path) -> None:
        """A fitting line in another label keeps its automatic cutter."""
        result = self._export(tmp_path, self._INVENTORY)
        other = result.rendered_labels["other_label"].source_label
        assert other.cutter_downsize_by_line == {}
        assert math.isclose(other.content[0].cutter_diameter, 0.06)

    def test_opt_out_label_stays_per_line(self, tmp_path: Path) -> None:
        """cutter_downsize_global false: the trigger downsizes, siblings don't."""
        result = self._export(tmp_path, self._INVENTORY)
        opted_out = result.rendered_labels["opted_out"].source_label
        assert list(opted_out.cutter_downsize_by_line) == [0]
        original, final = opted_out.cutter_downsize_by_line[0]
        assert math.isclose(original, 0.06)
        assert math.isclose(final, 0.045)
        assert math.isclose(opted_out.content[0].cutter_diameter, 0.045)
        assert math.isclose(opted_out.content[1].cutter_diameter, 0.06)

    def test_shared_cutter_gets_its_own_file(self, tmp_path: Path) -> None:
        """The pen map sees the shared cutter, so 0.045/0.060 files split as expected."""
        result = self._export(tmp_path, self._INVENTORY)
        assert sorted(p.name for p in result.plt_paths) == [
            "0.030_bh_cdg.plt",  # boundary/hole cutter 0.015 snapped up to 0.030
            "0.045_txt_cdg.plt",  # shared label (both lines) + opted_out trigger
            "0.060_txt_cdg.plt",  # other_label + opted_out sibling
        ]

    def test_pre_pass_is_a_noop_without_inventory(self, tmp_path: Path) -> None:
        """No tools.json inventory means no ladder: nothing shares."""
        result = self._export(tmp_path, None)
        for rendered in result.rendered_labels.values():
            assert rendered.source_label.cutter_downsize_by_line == {}


class TestHCompressGlobalDemoExample:
    """Regression tests for tests_deps/h_compress_global_job.yaml.

    The fixture opts in to ``h_compress_global``: a compressed line's scale is
    shared with the fitting siblings of the same text height *inside the same
    label*, while other labels and opted-out labels keep their per-line
    behaviour.
    """

    _SPEC = Path("tests_deps/h_compress_global_job.yaml")

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="hcg",
            optimize=False,
            plots=False,
        )

    def test_fitting_sibling_receives_the_shared_scale(self, tmp_path: Path) -> None:
        """The trigger's compression lands on its fitting same-height sibling."""
        result = self._export(tmp_path)
        shared = result.rendered_labels["shared"]
        assert sorted(shared.source_label.global_compress_by_line) == [0, 1]

        trigger = shared.source_label.global_compress_by_line[0]
        # The shared scale equals the trigger's natural scale: the trigger is
        # never squeezed past what it needed on its own.
        assert math.isclose(trigger, 0.8576, abs_tol=1e-3)
        assert math.isclose(shared.source_label.global_compress_by_line[1], trigger)

        # Both lines report the shared scale as their effective compression.
        assert sorted(shared.compression_by_line) == [0, 1]
        for scale in shared.compression_by_line.values():
            assert math.isclose(scale, trigger, abs_tol=1e-3)

    def test_sharing_never_crosses_the_label_boundary(self, tmp_path: Path) -> None:
        """A fitting line in another label keeps its natural width."""
        result = self._export(tmp_path)
        other = result.rendered_labels["other_label"]
        assert other.source_label.global_compress_by_line == {}
        assert other.compression_by_line == {}

    def test_opt_out_label_stays_per_line(self, tmp_path: Path) -> None:
        """h_compress_global false: the trigger compresses, siblings don't."""
        result = self._export(tmp_path)
        opted_out = result.rendered_labels["opted_out"]
        assert opted_out.source_label.global_compress_by_line == {}
        assert sorted(opted_out.compression_by_line) == [0]
        assert math.isclose(opted_out.compression_by_line[0], 0.8576, abs_tol=1e-3)

    def test_shared_compression_keeps_one_cutter_file(self, tmp_path: Path) -> None:
        """Sharing changes glyph density only, so the cutter set is unchanged."""
        result = self._export(tmp_path)
        assert sorted(p.name for p in result.plt_paths) == [
            "0.015_bh_hcg.plt",
            "0.060_txt_hcg.plt",
        ]


class TestOptimizeLineContentDemoExample:
    """Regression tests for tests_deps/optimize_line_content_job.yaml.

    The fixture opts in to ``optimize_line_content``: consecutive enabled
    lines exchange whole words (word order kept, only the line breaks move)
    so their natural widths equalize and the reflowed label needs no
    compression, while the opted-out twin (identical text, flag off) must
    compress. A disabled middle line splits a label into independent groups.
    """

    _SPEC = Path("tests_deps/optimize_line_content_job.yaml")

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="olc",
            optimize=False,
            plots=False,
        )

    def test_reflowed_label_balances_and_fits(self, tmp_path: Path) -> None:
        """The over-wide first line sheds its tail word and nothing compresses."""
        result = self._export(tmp_path)
        reflowed = result.rendered_labels["reflowed"]
        assert [line.text for line in reflowed.source_label.content] == [
            "ABCDEFGH",
            "IJ K",
        ]
        assert reflowed.compression_by_line == {}

    def test_opted_out_label_keeps_text_and_compresses(self, tmp_path: Path) -> None:
        """optimize_line_content false: the authored split stays and compresses."""
        result = self._export(tmp_path)
        opted_out = result.rendered_labels["opted_out"]
        assert [line.text for line in opted_out.source_label.content] == [
            "ABCDEFGH IJ",
            "K",
        ]
        assert sorted(opted_out.compression_by_line) == [0]
        assert opted_out.compression_by_line[0] < 1.0

    def test_disabled_line_splits_the_groups(self, tmp_path: Path) -> None:
        """A disabled middle line breaks the group, so no words move."""
        result = self._export(tmp_path)
        split = result.rendered_labels["split_groups"]
        assert [line.text for line in split.source_label.content] == [
            "AAAA BBBB",
            "SOLO",
            "CCCC",
        ]
        assert split.compression_by_line == {}

    def test_deep_group_moves_words_to_the_last_line(self, tmp_path: Path) -> None:
        """A four-line group sheds its long first line's tail words downward."""
        result = self._export(tmp_path)
        deep = result.rendered_labels["deep_group"]
        texts = [line.text for line in deep.source_label.content]
        # Word order is preserved across the whole group.
        assert " ".join(texts).split() == "AAAA BBBB CCCC DDDD E F G".split()
        # The first line shed words; the last line received them.
        assert texts[0] == "AAAA"
        assert texts[-1].startswith("DDDD")
        assert deep.compression_by_line == {}

    def test_reflow_keeps_one_cutter_file(self, tmp_path: Path) -> None:
        """Reflow changes line breaks only, so the cutter set is unchanged."""
        result = self._export(tmp_path)
        assert sorted(p.name for p in result.plt_paths) == [
            "0.015_bh_olc.plt",
            "0.060_txt_olc.plt",
        ]


class TestOptimizeLineContentMaxLinesDemoExample:
    """Regression tests for tests_deps/optimize_line_content_max_lines_job.yaml.

    The reflow line cap is growth-only: a capped group starts at its existing
    line count and may ADD lines (clones of its own typography) while its
    lines still need horizontal compression, up to the cap. A cap at or below
    the group's line count is inert, and a cap on any one line governs the
    whole group. The companion conflict fixture pins the validation error.
    """

    _SPEC = Path("tests_deps/optimize_line_content_max_lines_job.yaml")
    _CONFLICT_SPEC = Path("tests_deps/optimize_line_content_max_lines_conflict_job.yaml")
    _WORDS = "DANGER CONVEYER START AND STOP HERE".split()

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="olcm",
            optimize=False,
            plots=False,
        )

    def test_grown_label_fits_without_compression(self, tmp_path: Path) -> None:
        """The single long line grows to four lines and engraves uncompressed."""
        result = self._export(tmp_path)
        grown = result.rendered_labels["grow"]
        texts = [line.text for line in grown.source_label.content]
        assert len(texts) == 4
        assert " ".join(texts).split() == self._WORDS
        assert grown.compression_by_line == {}

    def test_grown_lines_clone_the_group_typography(self, tmp_path: Path) -> None:
        """Inserted lines carry the group's text height and cutter."""
        result = self._export(tmp_path)
        grown = result.rendered_labels["grow"]
        assert {line.nominal_text_height for line in grown.source_label.content} == {0.5}
        assert {line.cutter_diameter for line in grown.source_label.content} == {0.06}
        assert all(line.optimize_line_content for line in grown.source_label.content)

    def test_cap_exhausted_keeps_the_cap_and_compresses(self, tmp_path: Path) -> None:
        """A cap spent while compressed keeps its line count and squeezes."""
        result = self._export(tmp_path)
        tight = result.rendered_labels["cap_exhausted"]
        texts = [line.text for line in tight.source_label.content]
        assert len(texts) == 3  # capped at N, never grown past it
        assert " ".join(texts).split() == self._WORDS
        assert tight.compression_by_line  # still compressed at the ceiling

    def test_inert_cap_never_removes_lines(self, tmp_path: Path) -> None:
        """A cap below the group's line count leaves the line count alone."""
        result = self._export(tmp_path)
        inert = result.rendered_labels["inert_cap"]
        texts = [line.text for line in inert.source_label.content]
        assert len(texts) == 3
        assert " ".join(texts).split() == "DANGER CONVEYER START AND STOP HERE".split()

    def test_group_cap_from_the_last_line_grows_the_group(self, tmp_path: Path) -> None:
        """A cap declared on a group's last line applies to the whole group."""
        result = self._export(tmp_path)
        group = result.rendered_labels["group_cap"]
        texts = [line.text for line in group.source_label.content]
        assert len(texts) == 4
        assert " ".join(texts).split() == self._WORDS
        assert group.compression_by_line == {}

    def test_no_budget_group_keeps_its_authored_line(self, tmp_path: Path) -> None:
        """Without a compression budget the growth loop cannot measure."""
        result = self._export(tmp_path)
        nobudget = result.rendered_labels["no_budget"]
        texts = [line.text for line in nobudget.source_label.content]
        assert texts == ["DANGER CONVEYER START AND STOP HERE"]

    def test_disabled_line_keeps_the_groups_independent(self, tmp_path: Path) -> None:
        """A disabled line between two capped groups keeps them separate."""
        result = self._export(tmp_path)
        boundary = result.rendered_labels["group_boundary"]
        texts = [line.text for line in boundary.source_label.content]
        # 4 grown lines, the untouched serial line, then 4 grown lines.
        assert len(texts) == 9
        assert texts[4] == "SERIAL 042"
        assert " ".join(texts[:4]).split() == self._WORDS
        assert " ".join(texts[5:]).split() == "WEAR SAFETY GOGGLES AND GLOVES ALWAYS".split()

    def test_growth_keeps_one_cutter_file(self, tmp_path: Path) -> None:
        """Growth clones the group's typography, so the cutter set is unchanged."""
        result = self._export(tmp_path)
        assert sorted(p.name for p in result.plt_paths) == [
            "0.015_bh_olcm.plt",
            "0.060_txt_olcm.plt",
        ]

    def test_conflicting_group_caps_abort(self) -> None:
        """Two different caps in one group abort at resolution."""
        job = parse_yaml(self._CONFLICT_SPEC)
        with pytest.raises(LineContentConfigError, match="conflicting"):
            resolve_job_spec(job)


class TestPlateNumberScoping:
    """Plate numbers are scoped to a material group, not the whole job."""

    def test_plate_number_scoped_to_material_group(self, tmp_path: Path) -> None:
        """Only a material spanning several sheets keeps its plate numbers.

        Two `wb` sheets plus one `wb(uv)` sheet: the uv output is uniquely
        named by its material tag, while the two wb sheets must be numbered
        to stay distinct.
        """
        result = export_per_cutter_plts(
            [
                _label(label_id="wb", count=12, material="wb"),
                _label(label_id="uv", count=2, material="wb(uv)"),
            ],
            provided_plates=[
                PlateSpec(id="wb1", width=6.0, height=4.0, material="wb"),
                PlateSpec(id="wb2", width=6.0, height=4.0, material="wb"),
                PlateSpec(id="uv1", width=24.0, height=16.0, material="wb(uv)"),
            ],
            output_dir=tmp_path,
            job_id="scoped",
            optimize=False,
            plots=False,
            layout=LayoutMode.ROWS,
            allow_rotation=False,
        )
        uv = [p for p in result.plt_paths if p.name.startswith("wbuv_")]
        wb = [p for p in result.plt_paths if p.name[:2].isdigit()]
        assert wb, "wb labels must overflow onto both wb sheets"
        assert {p.name.split("_")[0] for p in wb} >= {"01", "02"}
        assert uv, "wb(uv) labels must export"
        # The single uv sheet is identified by its material tag alone.
        assert all(_parse_plt_stem(p.stem).plate is None for p in uv)

    def test_combined_pdfs_follow_the_same_scoping(self, tmp_path: Path) -> None:
        """Combined previews name sheets by material, numbering only groups
        that span several plates."""
        result = export_per_cutter_plts(
            [
                _label(label_id="wb", count=12, material="wb"),
                _label(label_id="uv", count=2, material="wb(uv)"),
            ],
            provided_plates=[
                PlateSpec(id="wb1", width=6.0, height=4.0, material="wb"),
                PlateSpec(id="wb2", width=6.0, height=4.0, material="wb"),
                PlateSpec(id="uv1", width=24.0, height=16.0, material="wb(uv)"),
            ],
            output_dir=tmp_path,
            job_id="scoped",
            optimize=False,
            plots=True,
            layout=LayoutMode.ROWS,
            allow_rotation=False,
        )
        combined = sorted(p.name for p in result.pdf_paths if "all_" in p.name)
        assert any(name.startswith("wbuv_all_") for name in combined), combined
        assert any(name[:2].isdigit() and "_wb_all_" in name for name in combined), combined


class TestSmallCapsDemoExample:
    """Regression tests for tests_deps/small_caps_job.yaml.

    The fixture pins the ``"<base> SC"`` small-caps variants: the base font's
    capitals engrave verbatim while ASCII lowercase renders as its uppercase
    glyph shrunk **top-down** by ``sc_scale`` (default 0.7). The controls pin
    ``sc_scale: 1.0`` (uppercase map, no shrink) against the base font, the
    transform's inertness on a non-SC font, caseless characters at full size,
    the TTF family, and the contract that the nominal height / cutter never
    move.
    """

    _SPEC = Path("tests_deps/small_caps_job.yaml")

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="sc",
            optimize=False,
            plots=False,
        )

    @staticmethod
    def _ink_height(label: ResolvedLabel) -> float:
        """Rendered ink height of the label's single text line."""
        from plt_optimizer.generate.label_renderer import _render_text_local_with_bounds

        _lc, entries = _render_text_local_with_bounds(label)
        x_min, y_min, x_max, y_max = entries[0].bounds
        return y_max - y_min

    def test_variants_and_scale_resolve_per_label(self, tmp_path: Path) -> None:
        """The SC name canonicalizes and ``sc_scale`` cascades (default 0.7)."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        assert by_id["plt_sc"].content[0].font == "Dino SC"
        assert by_id["plt_sc"].content[0].sc_scale == pytest.approx(0.7)
        assert by_id["plt_sc_100"].content[0].sc_scale == pytest.approx(1.0)
        # sc_scale is inert on a base font, but the resolved value is kept.
        assert by_id["base_sc_scale_inert"].content[0].font == "Dino"
        assert by_id["base_sc_scale_inert"].content[0].sc_scale == pytest.approx(0.3)

    def test_plt_lowercase_is_the_capital_shrunk(self, tmp_path: Path) -> None:
        """The PLT lowercase line measures 0.7 of the capital line's height."""
        result = self._export(tmp_path)
        small = self._ink_height(result.rendered_labels["plt_sc"].source_label)
        caps = self._ink_height(result.rendered_labels["plt_caps"].source_label)
        # Round glyphs overshoot their bbox slightly, so the ratio is 0.7
        # to within that overshoot.
        assert small == pytest.approx(0.7 * caps, rel=0.02)

    def test_scale_one_matches_the_base_font_caps(self, tmp_path: Path) -> None:
        """``sc_scale: 1.0`` renders the base font's capitals exactly."""
        result = self._export(tmp_path)
        from plt_optimizer.generate.label_renderer import _render_text_local_with_bounds

        def bounds(label: ResolvedLabel) -> tuple[float, ...]:
            _lc, entries = _render_text_local_with_bounds(label)
            return tuple(entries[0].bounds)

        one = bounds(result.rendered_labels["plt_sc_100"].source_label)
        base = bounds(result.rendered_labels["base_caps"].source_label)
        assert one == pytest.approx(base, abs=1e-9)

    def test_scale_is_inert_on_a_base_font(self, tmp_path: Path) -> None:
        """A non-SC font ignores ``sc_scale`` entirely."""
        result = self._export(tmp_path)
        from plt_optimizer.generate.label_renderer import _render_text_local_with_bounds

        def bounds(label: ResolvedLabel) -> tuple[float, ...]:
            _lc, entries = _render_text_local_with_bounds(label)
            return tuple(entries[0].bounds)

        assert bounds(result.rendered_labels["base_sc_scale_inert"].source_label) == pytest.approx(
            bounds(result.rendered_labels["base_caps"].source_label), abs=1e-9
        )

    def test_caseless_characters_stay_full_size(self, tmp_path: Path) -> None:
        """Digits engrave at cap height on an SC line."""
        result = self._export(tmp_path)
        digits = self._ink_height(result.rendered_labels["plt_caseless"].source_label)
        caps = self._ink_height(result.rendered_labels["plt_caps"].source_label)
        assert digits == pytest.approx(caps, rel=0.01)

    def test_ttf_family_shrinks_lowercase(self, tmp_path: Path) -> None:
        """The ftext path shrinks lowercase to 0.7 of its capital line."""
        result = self._export(tmp_path)
        small = self._ink_height(result.rendered_labels["ttf_sc"].source_label)
        caps = self._ink_height(result.rendered_labels["ttf_caps"].source_label)
        assert small == pytest.approx(0.7 * caps, rel=0.02)

    def test_no_cutter_change_and_arcs_stay_native(self, tmp_path: Path) -> None:
        """Small caps never moves the cutter, and PLT arcs stay ``AA``."""
        result = self._export(tmp_path)
        assert sorted(p.name for p in result.plt_paths) == [
            "0.015_bh_sc.plt",
            "0.060_txt_sc.plt",
        ]
        for rendered in result.rendered_labels.values():
            for line in rendered.source_label.content:
                assert line.cutter_diameter == pytest.approx(0.06)
        text_file = next(p for p in result.plt_paths if p.name.endswith("_txt_sc.plt"))
        assert "AA" in text_file.read_text()

    def test_no_compression_findings(self, tmp_path: Path) -> None:
        """Every label fits its inner area, so the layout report stays clean."""
        result = self._export(tmp_path)
        for rendered in result.rendered_labels.values():
            assert rendered.compression_by_line == {}


class TestUseBaselineSpacingDemoExample:
    """Regression tests for tests_deps/use_baseline_spacing_job.yaml.

    The fixture pins baseline-to-baseline stacking (``use_baseline_spacing``,
    default true) against its ink-box twin: a descender hangs into the gap
    below its line (tighter visual gap, shorter block), while cap-only
    content renders identically under both modes.
    """

    _SPEC = Path("tests_deps/use_baseline_spacing_job.yaml")

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="ubs",
            optimize=False,
            plots=False,
        )

    @staticmethod
    def _stacked_bounds(label: ResolvedLabel) -> list[tuple[float, float, float, float]]:
        """Per-line local bounds of a resolved label (block centered at y=0)."""
        from plt_optimizer.generate.label_renderer import _render_text_local_with_bounds

        _lc, entries = _render_text_local_with_bounds(label)
        return [entry.bounds for entry in entries]

    def test_flags_resolve_per_label(self, tmp_path: Path) -> None:
        """The default is baseline stacking; the twin opts out explicitly."""
        result = self._export(tmp_path)
        assert result.rendered_labels["baseline"].source_label.use_baseline_spacing is True
        assert result.rendered_labels["inkbox"].source_label.use_baseline_spacing is False
        assert result.rendered_labels["caponly"].source_label.use_baseline_spacing is True

    def test_descender_hangs_into_the_gap(self, tmp_path: Path) -> None:
        """Baseline mode shrinks the visual gap by the descender depth."""
        result = self._export(tmp_path)
        baseline = self._stacked_bounds(result.rendered_labels["baseline"].source_label)
        inkbox = self._stacked_bounds(result.rendered_labels["inkbox"].source_label)

        # Visual gap = upper ink bottom -> lower ink top.
        baseline_gap = baseline[0][1] - baseline[1][3]
        inkbox_gap = inkbox[0][1] - inkbox[1][3]
        assert math.isclose(inkbox_gap, 0.15, abs_tol=0.01)
        assert math.isclose(baseline_gap, 0.027, abs_tol=0.01)

        # The reclaimed space is exactly the 'p' descender depth.
        baseline_block = max(b[3] for b in baseline) - min(b[1] for b in baseline)
        inkbox_block = max(b[3] for b in inkbox) - min(b[1] for b in inkbox)
        assert math.isclose(inkbox_block - baseline_block, 0.123, abs_tol=0.01)

    def test_cap_only_content_is_mode_independent(self, tmp_path: Path) -> None:
        """Descender-free content keeps the requested spacing verbatim."""
        result = self._export(tmp_path)
        caponly = result.rendered_labels["caponly"]
        assert caponly.compression_by_line == {}
        assert math.isclose(caponly.line_spacing_by_line[0], 0.15, abs_tol=1e-9)

    def test_report_sees_no_findings(self, tmp_path: Path) -> None:
        """Baseline stacking reports the requested spacing (no report finding)."""
        result = self._export(tmp_path)
        for label_id in ("baseline", "inkbox", "caponly"):
            rendered = result.rendered_labels[label_id]
            assert rendered.compression_by_line == {}
            assert math.isclose(rendered.line_spacing_by_line[0], 0.15, abs_tol=1e-9)

    def test_stacking_keeps_one_cutter_file(self, tmp_path: Path) -> None:
        """Stacking changes Y placement only, so the cutter set is unchanged."""
        result = self._export(tmp_path)
        assert sorted(p.name for p in result.plt_paths) == [
            "0.015_bh_ubs.plt",
            "0.060_txt_ubs.plt",
        ]


class TestDescenderAdjustDemoExample:
    """Regression tests for tests_deps/descender_adjust_job.yaml.

    The fixture pins the automatic descending-character relief
    (``auto_descending_character_adjustment``, default true): a descending
    glyph measured close to a glyph on the following line is vertically
    downscaled about its own baseline and raised, per glyph, until the engraved
    strokes clear. Roomy gaps, cap-only content and the explicit opt-out leave
    the descender exactly as rendered; spent allowances grow the gap below.
    """

    _SPEC = Path("tests_deps/descender_adjust_job.yaml")

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="da",
            optimize=False,
            plots=False,
        )

    def test_flags_resolve_per_label(self, tmp_path: Path) -> None:
        """The default enables the relief; one label opts out explicitly."""
        result = self._export(tmp_path)
        assert (
            result.rendered_labels["trigger"].source_label.auto_descending_character_adjustment
            is True
        )
        assert (
            result.rendered_labels["off"].source_label.auto_descending_character_adjustment is False
        )
        grow = result.rendered_labels["spacing_grow"].source_label
        assert grow.descending_character_v_scale == 1.0
        assert grow.descending_character_raise == 0.0
        assert grow.descending_character_reciprocal_scale == 1.0
        # The reciprocal demo forbids the descending glyph's own allowances
        # and caps the following line's top-down downscale at 0.5 (the default).
        recip = result.rendered_labels["reciprocal"].source_label
        assert recip.descending_character_v_scale == 1.0
        assert recip.descending_character_raise == 0.0
        assert recip.descending_character_reciprocal_scale == 0.5

    def test_descender_is_relieved_against_the_line_below(self, tmp_path: Path) -> None:
        """The 'p' is squeezed and raised while the 'O' below shares the load.

        The relief is reciprocal: the same ramp drives the 'p' (downscale about
        its baseline + raise) and the colliding 'O' on the following line
        (top-down downscale, lowest point fixed), so the deficit is
        distributed between the two lines in proportion to their allowances.
        """
        result = self._export(tmp_path)
        fixes = result.rendered_labels["trigger"].descender_adjustments_by_line

        assert list(fixes) == [0, 1]
        (fix,) = fixes[0]
        assert fix.char == "p"
        assert fix.glyph_index == 2  # D-e-p-t-h
        assert fix.scale_y == pytest.approx(0.848, abs=0.01)
        assert fix.raise_amount == pytest.approx(0.026, abs=0.01)
        assert fix.gap_before == pytest.approx(0.0, abs=0.01)
        # Cleared to the stroke floor: 0.5 * (0.06 + 0.06) + clearance 0.0.
        assert fix.gap_after == pytest.approx(0.06, abs=1e-3)
        # The colliding 'O' shares the SAME ramp, scaled top-down (no raise).
        (recip,) = fixes[1]
        assert recip.reciprocal
        assert recip.char == "O"
        assert recip.glyph_index == 2  # the second O of R-O-O-T-S
        assert recip.scale_y == pytest.approx(0.848, abs=0.01)
        assert recip.raise_amount == 0.0
        assert recip.gap_before == pytest.approx(0.0, abs=0.01)
        assert recip.gap_after == pytest.approx(0.06, abs=1e-3)

    def test_relief_is_per_glyph_not_blanket(self, tmp_path: Path) -> None:
        """Only the glyph with a close partner below (plus that partner) moves."""
        result = self._export(tmp_path)
        fixes = result.rendered_labels["notall"].descender_adjustments_by_line
        assert [fix.char for fixes_on_line in fixes.values() for fix in fixes_on_line] == [
            "p",
            "O",
        ]

    def test_roomy_gap_is_left_alone(self, tmp_path: Path) -> None:
        """No close partner below means no relief."""
        result = self._export(tmp_path)
        assert result.rendered_labels["no_trigger"].descender_adjustments_by_line == {}

    def test_cap_only_control_has_no_relief(self, tmp_path: Path) -> None:
        """Descender-free content is the mode-independent control."""
        result = self._export(tmp_path)
        caponly = result.rendered_labels["caponly"]
        assert caponly.descender_adjustments_by_line == {}
        assert caponly.compression_by_line == {}
        assert math.isclose(caponly.line_spacing_by_line[0], 0.05, abs_tol=1e-9)

    def test_disabled_label_engraves_the_descender_verbatim(self, tmp_path: Path) -> None:
        """The opt-out twin keeps the un-relieved (colliding) descender."""
        result = self._export(tmp_path)
        off = result.rendered_labels["off"]
        trigger = result.rendered_labels["trigger"]
        assert off.descender_adjustments_by_line == {}
        assert off.plt_content != trigger.plt_content

        from plt_optimizer.generate.label_renderer import _render_text_local_with_bounds

        _lc, entries = _render_text_local_with_bounds(off.source_label)
        # Baseline stacking leaves the 'p' descender inside the gap: the visual
        # gap between the lines is negative (the ink boxes overlap).
        assert entries[0].bounds[1] - entries[1].bounds[3] < 0.0

    def test_spent_allowances_grow_the_gap_below(self, tmp_path: Path) -> None:
        """No downscale + no raise + no reciprocal => the spacing increases."""
        result = self._export(tmp_path)
        grow = result.rendered_labels["spacing_grow"]
        assert grow.descender_adjustments_by_line == {}
        # The requested 0.05in grew to clear the descender against the caps.
        assert grow.line_spacing_by_line[0] == pytest.approx(0.159, abs=0.01)

    def test_reciprocal_lean_carries_the_whole_deficit(self, tmp_path: Path) -> None:
        """With the descender's own allowances forbidden, the 'O' below shrinks.

        The reciprocal label forbids the 'p' (v_scale 1.0, raise 0.0), so the
        pair's clearance comes entirely from the colliding 'O' scaled top-down
        about its own lowest ink point: the record reports scale < 1 with a
        zero raise, and the twin with the reciprocal lever off (scale 1.0)
        engraves the collision verbatim.
        """
        result = self._export(tmp_path)
        recip = result.rendered_labels["reciprocal"]
        fixes = recip.descender_adjustments_by_line

        # Line 0's 'p' carries the shared ramp with both its own allowances
        # forbidden (scale 1.0, raise 0.0 -- the record of a spent lever); the
        # clearance itself comes from the 'O' below.
        assert list(fixes) == [0, 1]
        (upper,) = fixes[0]
        assert upper.char == "p"
        assert not upper.reciprocal
        assert upper.scale_y == pytest.approx(1.0)
        assert upper.raise_amount == 0.0
        (fix,) = fixes[1]
        assert fix.reciprocal
        assert fix.char == "O"
        assert fix.scale_y == pytest.approx(0.741, abs=0.01)
        assert fix.raise_amount == 0.0
        assert fix.gap_before == pytest.approx(0.0, abs=0.01)
        assert fix.gap_after == pytest.approx(0.06, abs=1e-3)

        off = result.rendered_labels["reciprocal_off"]
        assert off.descender_adjustments_by_line == {}
        assert off.plt_content != recip.plt_content

    def test_relief_reports_a_layout_finding(self, tmp_path: Path) -> None:
        """The relief surfaces in the shared LAYOUT REPORT formatter."""
        from plt_optimizer.generate.layout_report import has_layout_findings

        result = self._export(tmp_path)
        resolved = [
            result.rendered_labels[label_id].source_label for label_id in ("caponly", "trigger")
        ]
        assert has_layout_findings(resolved, result.rendered_labels)

    def test_relief_keeps_one_cutter_file(self, tmp_path: Path) -> None:
        """The relief is a Y transform only, so the cutter set is unchanged."""
        result = self._export(tmp_path)
        assert sorted(p.name for p in result.plt_paths) == [
            "0.015_bh_da.plt",
            "0.060_txt_da.plt",
        ]

    def test_border_descender_is_relieved(self, tmp_path: Path) -> None:
        """The short label's descenders are squeezed clear of the bottom border.

        The border is a virtual "following line" drawn at the label edge and
        cut by the boundary/hole tool, so the required clearance is
        ``0.5 * (0.06 + 0.015) * (1 + 0.0) = 0.0375in``.
        """
        result = self._export(tmp_path)
        fixes = result.rendered_labels["border"].descender_adjustments_by_line

        assert list(fixes) == [0]
        assert [fix.char for fix in fixes[0]] == ["p", "y"]
        for fix in fixes[0]:
            assert fix.scale_y < 1.0
            assert fix.raise_amount > 0.0
            # Band centering drops the block by the descender depth, so the
            # tips reach the border centreline.
            assert fix.gap_before == pytest.approx(0.0, abs=0.01)
            assert fix.gap_after == pytest.approx(0.0375, abs=1e-3)


class TestDescenderCrookExample:
    """Regression tests for tests_deps/descender_crook_job.yaml.

    The reported bug: a "g" hook colliding with the dot of an "i" on the line
    below. The pair is X-disjoint at bounding-box level (the box model reports
    ``inf``, "can never collide") because the dot sits BESIDE the hook, in its
    crook. Only the true 2D swept-ink metric -- a circle of the required
    diameter rolled along the descending glyph's sub-baseline ink -- measures
    the hook's real proximity and relieves the "g".
    """

    _SPEC = Path("tests_deps/descender_crook_job.yaml")

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the crook job with plotting disabled."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="cr",
            optimize=False,
            plots=False,
        )

    def test_crook_descender_is_relieved(self, tmp_path: Path) -> None:
        """The "g" hook reaching the "i" dot below is squeezed and raised."""
        result = self._export(tmp_path)
        fixes = result.rendered_labels["crook"].descender_adjustments_by_line

        # Line 0's 'g' hits the 'i' dot below (the reported bug); band
        # centering also drops line 1's 'g' onto the bottom border, so both
        # lines report a relief.
        assert list(fixes) == [0, 1]
        (fix,) = fixes[0]
        assert fix.char == "g"
        assert fix.glyph_index == 5  # t-h-r-o-u-g-h
        assert fix.scale_y == pytest.approx(0.875, abs=0.01)
        assert fix.raise_amount == pytest.approx(0.078, abs=0.01)
        # The tightest gap (0.094in) sits inside the required clearance
        # (0.5 * (0.09 + 0.09) * 1.5 = 0.135in) and is cleared above it.
        assert fix.gap_before == pytest.approx(0.094, abs=0.01)
        # Cleared above the required clearance (0.5 * (0.09 + 0.09) * 1.5 =
        # 0.135in). ``gap_after`` is the tightest of the constraints that
        # actually bind this glyph -- the line below (0.281in); the bottom
        # border is far away, so it never triggered and never clamps the
        # report.
        assert fix.gap_after == pytest.approx(0.281, abs=0.01)
        assert fix.gap_before < fix.gap_after

    def test_the_crook_pair_is_blind_to_the_box_model(self, tmp_path: Path) -> None:
        """The g->i pair is X-disjoint at box level (gap inf) yet truly close.

        This is the pair the box model hid: the "i" dot sits beside the "g"
        hook, so their bounding boxes never overlap horizontally and
        ``aabb_vertical_gap`` reports ``inf``. The swept-ink distance is well
        inside the required clearance.
        """
        from plt_optimizer.generate import label_renderer as renderer
        from plt_optimizer.generate.geometry import aabb_vertical_gap, ink_gap

        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job)
        label = next(label for label in labels if label.id == "crook")
        blocks, groups = [], []
        for line in label.content:
            block, _wg, glyph_groups = renderer._render_line_block(
                line, renderer.TextChunkMode.LINE
            )
            blocks.append(block)
            groups.append(glyph_groups)
        pitch = [line.toolpath_text_height for line in label.content]
        geometry = renderer._local_line_extents(blocks, pitch)
        baselines = renderer._baseline_table(
            pitch,
            [ascender for _basis, ascender, _d in geometry],
            [descender for _basis, _ascender, descender in geometry],
            [0.2614],
        )
        upper = renderer._shifted_boxes(
            renderer._glyph_boxes(blocks[0], groups[0], "through"), baselines[0]
        )
        lower = renderer._shifted_boxes(
            renderer._glyph_boxes(blocks[1], groups[1], "charging"), baselines[1]
        )
        hook = next(box for box in upper if box.glyph_index == 5)
        dot = next(box for box in lower if box.char == "i")

        assert math.isinf(aabb_vertical_gap(hook.bounds, dot.bounds))
        assert ink_gap(hook.sub_baseline_ink, dot.ink) == pytest.approx(0.084, abs=0.01)
        assert ink_gap(hook.sub_baseline_ink, dot.ink) < 0.135

    def test_disabled_label_engraves_the_crook_verbatim(self, tmp_path: Path) -> None:
        """The opt-out twin keeps the colliding "g" (no relief)."""
        result = self._export(tmp_path)
        off = result.rendered_labels["off"]
        crook = result.rendered_labels["crook"]
        assert off.descender_adjustments_by_line == {}
        assert crook.descender_adjustments_by_line != {}
        assert off.plt_content != crook.plt_content


class TestDescenderReciprocalExample:
    """Regression tests for tests_deps/descender_reciprocal_job.yaml.

    ``descending_character_reciprocal_scale`` (default 0.5) makes the relief
    two-sided: the colliding glyphs on the FOLLOWING line share the deficit
    through the same ramp, scaled top-down about their own lowest ink point so
    their lowest point never moves. The crook pair (a "g" hook reaching the dot
    of an "i" below) is the fixture: with both allowances open the two glyphs
    move together, with the descending glyph's allowances forbidden the "i"
    carries the whole deficit, and with the reciprocal lever off (1.0) the pair
    engraves as the upper-only relief leaves it.
    """

    _SPEC = Path("tests_deps/descender_reciprocal_job.yaml")

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the reciprocal job with plotting disabled."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="rc",
            optimize=False,
            plots=False,
        )

    def test_reciprocal_scale_resolves_per_label(self, tmp_path: Path) -> None:
        """The default is 0.5; labels may forbid the reciprocal half (1.0)."""
        result = self._export(tmp_path)
        shared = result.rendered_labels["shared"].source_label
        assert shared.descending_character_reciprocal_scale == 0.5
        assert (
            result.rendered_labels[
                "reciprocal_only"
            ].source_label.descending_character_reciprocal_scale
            == 0.5
        )
        assert (
            result.rendered_labels[
                "reciprocal_off"
            ].source_label.descending_character_reciprocal_scale
            == 1.0
        )

    def test_the_pair_shares_the_load(self, tmp_path: Path) -> None:
        """The 'g' is squeezed/raised AND the 'i' dot is scaled top-down.

        Both records carry the same ramp, so the descending glyph is squeezed
        less than the upper-only relief squeezes it (compare the crook demo's
        0.710 with the shared 0.875 here).
        """
        result = self._export(tmp_path)
        fixes = result.rendered_labels["shared"].descender_adjustments_by_line

        (upper,) = fixes[0]
        assert upper.char == "g"
        assert upper.glyph_index == 5  # t-h-r-o-u-g-h
        assert not upper.reciprocal
        assert upper.scale_y == pytest.approx(0.875, abs=0.01)
        assert upper.raise_amount == pytest.approx(0.078, abs=0.01)
        # The tightest gap (0.094in) sits inside the required clearance
        # (0.5 * (0.09 + 0.09) * 1.5 = 0.135in) and is cleared above it.
        assert upper.gap_before == pytest.approx(0.094, abs=0.01)
        assert upper.gap_after == pytest.approx(0.281, abs=0.01)

        # The colliding 'i' dot shares the SAME ramp, scaled top-down: its
        # lowest point is the anchor, so it carries no raise at all.
        partners = [fix for fix in fixes[1] if fix.reciprocal]
        (partner,) = partners
        assert partner.char == "i"
        assert partner.glyph_index == 5  # c-h-a-r-g-i-ng
        assert partner.scale_y == pytest.approx(upper.scale_y, abs=1e-9)
        assert partner.raise_amount == 0.0
        assert partner.gap_before == pytest.approx(upper.gap_before, abs=1e-9)
        assert partner.gap_after == pytest.approx(upper.gap_after, abs=1e-9)

    def test_the_reciprocal_half_gentles_the_descending_glyph(self, tmp_path: Path) -> None:
        """Sharing the load squeezes the descender less than the crook demo did.

        ``descender_crook_job.yaml`` pins the same pair with the reciprocal
        lever at its shipped default too, so this label's own comparison is
        against the reciprocal-off twin: the shared 'g' keeps a larger
        (gentler) scale than the lean label, whose allowances are forbidden.
        """
        result = self._export(tmp_path)
        shared = result.rendered_labels["shared"].descender_adjustments_by_line
        lean = result.rendered_labels["reciprocal_only"].descender_adjustments_by_line

        (shared_upper,) = shared[0]
        (lean_upper,) = lean[0]
        # The lean label's 'g' cannot move at all (both allowances are 1.0/0.0).
        assert lean_upper.scale_y == pytest.approx(1.0)
        assert lean_upper.raise_amount == 0.0
        # The shared label's 'g' moved, and stayed inside its allowance floor.
        assert shared_upper.scale_y < 1.0
        assert shared_upper.scale_y >= 0.5 - 1e-9

    def test_the_reciprocal_lean_carries_the_whole_deficit(self, tmp_path: Path) -> None:
        """With the descender inert, the 'i' dot alone clears the pair."""
        result = self._export(tmp_path)
        fixes = result.rendered_labels["reciprocal_only"].descender_adjustments_by_line

        assert list(fixes) == [0, 1]
        (partner,) = [fix for fix in fixes[1] if fix.reciprocal]
        assert partner.char == "i"
        assert partner.scale_y == pytest.approx(0.779, abs=0.01)
        assert partner.raise_amount == 0.0
        # Cleared above the required clearance (0.5 * (0.09 + 0.09) * 1.5).
        assert partner.gap_before == pytest.approx(0.094, abs=0.01)
        assert partner.gap_after == pytest.approx(0.281, abs=0.01)

    def test_the_opt_out_twin_engraves_the_collision_verbatim(self, tmp_path: Path) -> None:
        """``descending_character_reciprocal_scale: 1.0`` disables the half.

        The twin forbids the descending glyph's allowances too, so nothing can
        move: no relief records at all, and its output differs from the lean
        label's (where the 'i' dot did shrink).
        """
        result = self._export(tmp_path)
        off = result.rendered_labels["reciprocal_off"]
        lean = result.rendered_labels["reciprocal_only"]
        assert off.descender_adjustments_by_line == {}
        assert lean.descender_adjustments_by_line != {}
        assert off.plt_content != lean.plt_content

    def test_the_reciprocal_is_y_only(self, tmp_path: Path) -> None:
        """A top-down scale is a Y transform, so the cutter set is unchanged."""
        result = self._export(tmp_path)
        assert sorted(p.name for p in result.plt_paths) == [
            "0.015_bh_rc.plt",
            "0.187_txt_rc.plt",
        ]


class TestDescenderBorderExample:
    """Regression tests for tests_deps/descender_border_job.yaml.

    The bottom border is a virtual "following line" for the descending-
    character relief: a descender collides when its sub-baseline ink comes
    within ``0.5 * (text_cutter + hole_cutter) * (1 + clearance)`` of the
    border centreline (the border is drawn at the label edge and cut by the
    boundary/hole tool). The block is centred, so a short label puts the
    descender inside that clearance and a taller one leaves it clear. Only the
    per-glyph ramp acts -- spacing growth moves the last line DOWN, toward the
    border -- and crossing ``v_margin`` is not a collision.
    """

    _SPEC = Path("tests_deps/descender_border_job.yaml")

    # 1.5in dino text auto-selects a 0.187in cutter; the boundary/hole tool is
    # the 0.015in default, so the required clearance is
    # 0.5 * (0.187 + 0.015) * 1.5 = 0.1515in.
    _REQUIRED = 0.1515

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the border job with plotting disabled."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="br",
            optimize=False,
            plots=False,
        )

    def test_descender_near_the_border_is_relieved(self, tmp_path: Path) -> None:
        """The short label's 'g' is squeezed and raised clear of the border."""
        result = self._export(tmp_path)
        fixes = result.rendered_labels["trigger"].descender_adjustments_by_line

        assert list(fixes) == [0]
        (fix,) = fixes[0]
        assert fix.char == "g"
        assert fix.scale_y < 1.0
        assert fix.raise_amount > 0.0
        assert fix.gap_before < self._REQUIRED
        assert fix.gap_after == pytest.approx(self._REQUIRED, abs=1e-3)

    def test_descender_clear_of_the_border_is_left_alone(self, tmp_path: Path) -> None:
        """The taller label leaves enough air, so nothing is relieved."""
        result = self._export(tmp_path)
        assert result.rendered_labels["no_trigger"].descender_adjustments_by_line == {}

    def test_crossing_the_margin_is_not_a_collision(self, tmp_path: Path) -> None:
        """Ink spilling into v_margin is accepted; only the border stroke counts."""
        result = self._export(tmp_path)
        assert result.rendered_labels["margin_cross"].descender_adjustments_by_line == {}

    def test_border_relief_acts_on_the_last_line_only(self, tmp_path: Path) -> None:
        """The border trigger lands on the last line's descender, never the gap."""

        result = self._export(tmp_path)
        label = result.rendered_labels["spacing_untouched"]
        fixes = label.descender_adjustments_by_line

        assert list(fixes) == [1]  # the last line's 'g'
        (fix,) = fixes[1]
        assert fix.char == "g"
        assert fix.gap_after == pytest.approx(self._REQUIRED, abs=1e-3)
        # The relief never grows spacing for a border collision: the reported
        # gap is the margin-clamped value, not a relief-grown one.
        assert label.source_label.content[0].line_spacing >= label.line_spacing_by_line[0]

    def test_disabled_label_engraves_the_border_descender_verbatim(self, tmp_path: Path) -> None:
        """The opt-out twin keeps the descender hanging into the border stroke."""
        result = self._export(tmp_path)
        off = result.rendered_labels["off"]
        trigger = result.rendered_labels["trigger"]
        assert off.descender_adjustments_by_line == {}
        assert trigger.descender_adjustments_by_line != {}
        assert off.plt_content != trigger.plt_content

    def test_border_relief_keeps_one_cutter_file(self, tmp_path: Path) -> None:
        """The relief is a Y transform only, so the cutter set is unchanged."""
        result = self._export(tmp_path)
        assert sorted(p.name for p in result.plt_paths) == [
            "0.015_bh_br.plt",
            "0.187_txt_br.plt",
        ]


class TestVCenterByContentExample:
    """Regression tests for tests_deps/v_center_by_content_job.yaml.

    With the descending-character relief enabled a label centers its text by
    the baseline-to-capline BAND, so the visible glyphs sit in the label's
    optical middle and descenders hang into the bottom margin (where the relief
    clears them against the bottom border). ``v_center_by_content: true``
    restores the historical ink centering, which charges the deepest descender
    to the block and rides the whole line high. The margin clamp follows the
    same model, so a band-centered label keeps the spacing its descenders would
    otherwise have shrunk.
    """

    _SPEC = Path("tests_deps/v_center_by_content_job.yaml")

    # 1.5in dino text auto-selects a 0.187in cutter; the boundary/hole tool is
    # the 0.015in default, so the border clearance is 0.1515in.
    _REQUIRED = 0.1515

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the centering job with plotting disabled."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="vc",
            optimize=False,
            plots=False,
        )

    def test_band_centered_label_relies_the_descender(self, tmp_path: Path) -> None:
        """Centering the cap band drops the 'g' into the border clearance."""
        result = self._export(tmp_path)
        label = result.rendered_labels["band_centered"]

        assert label.source_label.center_text_by_band is True
        assert label.band_center_offset_inches > 0.0
        (fix,) = label.descender_adjustments_by_line[0]
        assert fix.char == "g"
        assert fix.gap_after == pytest.approx(self._REQUIRED, abs=1e-3)

    def test_ink_centered_label_is_border_clear(self, tmp_path: Path) -> None:
        """The historical centering rides the line high, so nothing collides."""
        result = self._export(tmp_path)
        label = result.rendered_labels["ink_centered"]

        assert label.source_label.center_text_by_band is False
        assert label.band_center_offset_inches == 0.0
        assert label.descender_adjustments_by_line == {}

    def test_band_centering_moves_the_text_below_ink_centering(self, tmp_path: Path) -> None:
        """The same label sits lower under band centering (the reported symptom).

        Ink centering charges the descender to the block, so the visible text
        rides high; band centering drops it by the descender depth.
        """
        from plt_optimizer.generate import label_renderer as renderer

        result = self._export(tmp_path)
        band = result.rendered_labels["band_centered"]
        ink = result.rendered_labels["ink_centered"]
        lc_band, _ = renderer._render_text_local_with_bounds(band.source_label)
        lc_ink, _ = renderer._render_text_local_with_bounds(ink.source_label)
        top_band = max(z.imag for arr in lc_band for z in arr)
        top_ink = max(z.imag for arr in lc_ink for z in arr)

        assert top_band < top_ink

    def test_relief_off_leaves_the_field_inert(self, tmp_path: Path) -> None:
        """With the relief disabled, ink centering is kept and nothing moves."""
        result = self._export(tmp_path)
        label = result.rendered_labels["relief_off"]

        assert label.source_label.v_center_by_content is False  # the default
        assert label.source_label.center_text_by_band is False
        assert label.band_center_offset_inches == 0.0
        assert label.descender_adjustments_by_line == {}

    def test_band_centering_keeps_spacing_the_descenders_would_shrink(self, tmp_path: Path) -> None:
        """The margin clamp sizes against the cap band, preserving spacing.

        Two descender lines whose descender-extended block overflows the inner
        area: ink centering clamps the authored 0.15in spacing, band centering
        keeps it and lets the descenders hang into the bottom margin.
        """
        result = self._export(tmp_path)
        kept = result.rendered_labels["spacing_kept"]
        clamped = result.rendered_labels["spacing_clamped"]

        assert kept.source_label.content[0].line_spacing == pytest.approx(0.15, abs=1e-9)
        assert kept.line_spacing_by_line[0] == pytest.approx(0.15, abs=1e-6)
        assert clamped.line_spacing_by_line[0] < 0.15

    def test_centering_keeps_one_cutter_file(self, tmp_path: Path) -> None:
        """Centering is a Y transform only, so the cutter set is unchanged."""
        result = self._export(tmp_path)
        assert sorted(p.name for p in result.plt_paths) == [
            "0.015_bh_vc.plt",
            "0.187_txt_vc.plt",
        ]


class TestItalicDemoExample:
    """Regression tests for tests_deps/italic_job.yaml.

    The fixture pins inline markup + italicization: ``<i>`` shears PLT
    glyphs (and TTF glyphs with no native italic face) about the baseline,
    ``x' = x + y * tan(italic_angle)``. The controls pin the baseline and ink
    height as invariant, the plain line as strictly narrower, ``italic_angle:
    0`` as upright, per-character selection between the two, a
    multi-character bracket pair, chevrons engraved verbatim when the
    delimiters are ``«»``, and the TTF shear fallback.
    """

    _SPEC = Path("tests_deps/italic_job.yaml")

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="it",
            optimize=False,
            plots=False,
        )

    @staticmethod
    def _line_bounds(label: ResolvedLabel) -> tuple[float, float, float, float]:
        """Rendered bounds of the label's single text line."""
        from plt_optimizer.generate.label_renderer import _render_text_local_with_bounds

        _lc, entries = _render_text_local_with_bounds(label)
        return entries[0].bounds

    def test_markup_parses_to_plain_text_and_runs(self, tmp_path: Path) -> None:
        """Tags never engrave: plain_text strips them, runs carry the style."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}

        italic = by_id["plt_italic"].content[0]
        assert italic.text == "<i>AV</i>"
        assert italic.plain_text == "AV"
        assert [run.style.italic for run in italic.styled_runs] == [True]

        plain = by_id["plt_plain"].content[0]
        assert plain.plain_text == "AV"
        # A markup-free line resolves to a single plain run (the renderers'
        # fast path is "no italic run", not "no run").
        assert [(r.text, r.style.italic) for r in plain.styled_runs] == [("AV", False)]

        mixed = by_id["plt_mixed"].content[0]
        assert mixed.plain_text == "AVX"
        assert [(r.text, r.style.italic) for r in mixed.styled_runs] == [
            ("A", False),
            ("V", True),
            ("X", False),
        ]

    def test_brackets_cascade_and_chevrons_stay_literal(self, tmp_path: Path) -> None:
        """A multi-char pair parses its own tags and leaves chevrons alone."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}

        assert by_id["brackets_multichar"].content[0].html_brackets == "«»"
        assert by_id["brackets_multichar"].content[0].plain_text == "AV"
        # With «» as delimiters the chevron tag is data: 11 glyphs engrave.
        literal = by_id["chevrons_literal"].content[0]
        assert literal.html_brackets == "«»"
        assert literal.plain_text == "<i>AVXZ</i>"
        assert [(r.text, r.style.italic) for r in literal.styled_runs] == [("<i>AVXZ</i>", False)]

    def test_italic_angle_cascades(self, tmp_path: Path) -> None:
        """``italic_angle`` defaults to 12 and honors an explicit 0 / 45."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}

        assert by_id["plt_italic"].content[0].italic_angle == pytest.approx(12.0)
        assert by_id["plt_angle0"].content[0].italic_angle == pytest.approx(0.0)
        assert by_id["plt_angle45"].content[0].italic_angle == pytest.approx(45.0)

    def test_plt_italic_keeps_baseline_and_height_and_widens(self, tmp_path: Path) -> None:
        """The shear anchors the baseline, preserves ink height, widens ink."""
        result = self._export(tmp_path)
        italic = self._line_bounds(result.rendered_labels["plt_italic"].source_label)
        plain = self._line_bounds(result.rendered_labels["plt_plain"].source_label)

        # Shear about y = 0: the baseline (and the ink height above/below it)
        # is untouched; only X moves.
        assert italic[1] == pytest.approx(plain[1], abs=1e-12)
        assert italic[3] == pytest.approx(plain[3], abs=1e-12)
        assert italic[2] - italic[0] > plain[2] - plain[0]

    def test_angle_zero_is_upright(self, tmp_path: Path) -> None:
        """``italic_angle: 0`` skips the shear: identical to the plain line."""
        result = self._export(tmp_path)
        upright = self._line_bounds(result.rendered_labels["plt_angle0"].source_label)
        plain = self._line_bounds(result.rendered_labels["plt_plain"].source_label)
        assert upright == pytest.approx(plain, abs=1e-12)

    def test_per_character_selection_is_between(self, tmp_path: Path) -> None:
        """Shearing one of three glyphs widens the line, but less than all."""
        result = self._export(tmp_path)
        mixed = self._line_bounds(result.rendered_labels["plt_mixed"].source_label)
        plain = self._line_bounds(result.rendered_labels["plt_mixed_plain"].source_label)
        all_italic = self._line_bounds(result.rendered_labels["plt_italic"].source_label)
        all_plain = self._line_bounds(result.rendered_labels["plt_plain"].source_label)

        mixed_w = mixed[2] - mixed[0]
        plain_w = plain[2] - plain[0]
        gain_all = (all_italic[2] - all_italic[0]) - (all_plain[2] - all_plain[0])
        assert plain_w < mixed_w
        assert mixed_w - plain_w == pytest.approx(gain_all, rel=0.6)

    def test_multichar_brackets_render_like_default_pair(self, tmp_path: Path) -> None:
        """``«»`` markup engraves exactly like the default ``<>`` markup."""
        result = self._export(tmp_path)
        guillemet = self._line_bounds(result.rendered_labels["brackets_multichar"].source_label)
        default = self._line_bounds(result.rendered_labels["plt_italic"].source_label)
        assert guillemet == pytest.approx(default, abs=1e-12)

    def test_chevron_literal_line_is_wider_than_the_tag_text(self, tmp_path: Path) -> None:
        """A literal chevron line engraves its tag characters (far wider)."""
        result = self._export(tmp_path)
        literal = self._line_bounds(result.rendered_labels["chevrons_literal"].source_label)
        tag_text = self._line_bounds(result.rendered_labels["chevrons_plain"].source_label)
        assert (literal[2] - literal[0]) > (tag_text[2] - tag_text[0])

    def test_steeper_angle_widens_more(self, tmp_path: Path) -> None:
        """45 degrees (tan 1.0) overhangs further than the 12-degree default."""
        result = self._export(tmp_path)
        steep = self._line_bounds(result.rendered_labels["plt_angle45"].source_label)
        default = self._line_bounds(result.rendered_labels["plt_italic"].source_label)
        plain = self._line_bounds(result.rendered_labels["plt_plain"].source_label)
        assert (steep[2] - steep[0]) > (default[2] - default[0]) > (plain[2] - plain[0])

    def test_ttf_italic_grows_the_cap_height(self, tmp_path: Path) -> None:
        """The TTF shear fallback raises the ink top by height * tan(angle)."""
        result = self._export(tmp_path)
        italic = self._line_bounds(result.rendered_labels["ttf_italic"].source_label)
        plain = self._line_bounds(result.rendered_labels["ttf_plain"].source_label)

        # The shear leaves y untouched, so the block's height grows by exactly
        # `height * tan(angle)` (the baseline-anchored top rises). The label
        # then re-centers the taller block, which is why the absolute ymin
        # moves while the height delta is the invariant.
        plain_height = plain[3] - plain[1]
        italic_height = italic[3] - italic[1]
        assert italic_height - plain_height == pytest.approx(
            plain_height * math.tan(math.radians(12.0)), abs=1e-6
        )

    def test_italic_never_moves_the_cutter(self, tmp_path: Path) -> None:
        """Italic is a geometry transform: the cutter set is unchanged."""
        result = self._export(tmp_path)
        assert sorted(p.name for p in result.plt_paths) == [
            "0.015_bh_it.plt",
            "0.060_txt_it.plt",
        ]


class TestUnderlineDemoExample:
    """Regression tests for tests_deps/underline_job.yaml.

    The fixture pins inline underline markup (``<u>``, ``underline_offset``)
    and nested markup (``<i>`` wrapping ``<u>``). ``<u>`` draws a centerline
    underline with the line's own text cutter, ``cutter + underline_offset``
    below the baseline, spanning each underlined run's ink; the line's own
    glyph ink punches minimal gaps into it (descenders break the underline,
    caps touch it tangentially).
    """

    _SPEC = Path("tests_deps/underline_job.yaml")

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="ul",
            optimize=False,
            plots=False,
        )

    @staticmethod
    def _line_bounds(label: ResolvedLabel) -> tuple[float, float, float, float]:
        """Rendered bounds of the label's single text line."""
        from plt_optimizer.generate.label_renderer import _render_text_local_with_bounds

        _lc, entries = _render_text_local_with_bounds(label)
        return entries[0].bounds

    @staticmethod
    def _line_render(label: ResolvedLabel):
        """Render the label's single text line (block + glyph partition)."""
        from plt_optimizer.generate.label_renderer import (
            TextChunkMode,
            _render_line_block,
        )

        return _render_line_block(label.content[0], TextChunkMode.LINE)

    @classmethod
    def _underline_stroke_count(cls, label: ResolvedLabel) -> int:
        """The number of underline strokes a rendered line carries.

        The underline strokes are appended after the glyph partition, so the
        count is the block's strokes minus the glyphs' strokes.
        """
        block, _wg, glyph_groups = cls._line_render(label)
        return len(block.strokes) - sum(len(g) for g in glyph_groups)

    def test_markup_parses_to_plain_text_and_runs(self, tmp_path: Path) -> None:
        """Tags never engrave: plain_text strips them, runs carry the style."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}

        underlined = by_id["plt_underline"].content[0]
        assert underlined.text == "<u>Depth</u>"
        assert underlined.plain_text == "Depth"
        assert [run.style.underline for run in underlined.styled_runs] == [True]

        plain = by_id["plt_plain"].content[0]
        assert plain.plain_text == "Depth"
        # A markup-free line resolves to a single plain run (the renderers'
        # fast path is "no underline run", not "no run").
        assert [(r.text, r.style.underline) for r in plain.styled_runs] == [("Depth", False)]

    def test_descender_free_run_extends_the_block(self, tmp_path: Path) -> None:
        """Caps touch the underline tangentially, so it extends the block."""
        result = self._export(tmp_path)
        caps = self._line_bounds(result.rendered_labels["plt_caps"].source_label)
        caps_plain = self._line_bounds(result.rendered_labels["plt_caps_plain"].source_label)
        # The underline (caps tangent, one segment) hangs below the baseline,
        # growing the block's height; the plain caps stop at the baseline.
        assert caps[3] - caps[1] > caps_plain[3] - caps_plain[1]
        # The underline spans the run's ink width, so the block width is unchanged.
        assert caps[2] - caps[0] == pytest.approx(caps_plain[2] - caps_plain[0], rel=1e-6)

    def test_descender_run_engraves_without_moving_bounds(self, tmp_path: Path) -> None:
        """A descender deeper than the underline leaves the block unchanged."""
        result = self._export(tmp_path)
        underlined = self._line_bounds(result.rendered_labels["plt_underline"].source_label)
        plain = self._line_bounds(result.rendered_labels["plt_plain"].source_label)
        # The `p`/`g` hang below the underline, so the block is unchanged --
        # but the underline is still engraved (gaps punched around the descenders).
        assert underlined == pytest.approx(plain, abs=1e-9)
        assert (
            self._underline_stroke_count(result.rendered_labels["plt_underline"].source_label) >= 2
        )

    def test_offset_pushes_the_underline_deeper(self, tmp_path: Path) -> None:
        """A larger ``underline_offset`` lowers the centerline below the descender."""
        result = self._export(tmp_path)
        offset = self._line_bounds(result.rendered_labels["plt_offset"].source_label)
        default = self._line_bounds(result.rendered_labels["plt_underline"].source_label)
        # At 0.1in the centerline (-(cutter + 0.1)) sits below the `p`/`g`
        # descenders, so the block grows; the width is unchanged.
        assert offset[3] - offset[1] > default[3] - default[1]
        assert offset[2] - offset[0] == pytest.approx(default[2] - default[0], rel=1e-6)

    def test_break_clearance_widens_the_breaks(self, tmp_path: Path) -> None:
        """``underline_break_clearance`` adds air at each break, not depth.

        The centerline stays at the default offset (one cutter below the
        baseline), so the block's depth matches the clearance-0.0 control. The
        ``pay`` ``p``/``y`` stems each cross the centerline, so both labels
        break once; the clearance widens that break (the engraved air at the
        cut edge equals the clearance).
        """
        result = self._export(tmp_path)
        wide = result.rendered_labels["plt_break_clearance"].source_label
        tight = result.rendered_labels["plt_break_clearance_plain"].source_label
        wb = self._line_bounds(wide)
        tb = self._line_bounds(tight)
        # Same centerline Y (the clearance never moves the underline): the
        # block's floor is identical.
        assert wb[1] == pytest.approx(tb[1], abs=1e-9)

        def gap(label: ResolvedLabel) -> float:
            from plt_optimizer.generate.label_renderer import (
                TextChunkMode,
                _render_line_block,
                _underline_render_gate,
                _underline_strokes,
            )

            block, _words, glyphs = _render_line_block(label.content[0], TextChunkMode.LINE)
            strokes = _underline_strokes(
                label.content[0], block, glyphs, _underline_render_gate(label.content[0])
            )
            spans = sorted((s.segments[0].start.real, s.segments[0].end.real) for s in strokes)
            assert len(spans) == 2  # the `p`/`y` stems cross: one break
            return spans[1][0] - spans[0][1]

        # Clearance 0.05 leaves air on each side of the cut (and reaches a
        # little further up/down the stem), so the gap is strictly wider than
        # the clearance-0.0 control (which cuts at the ink).
        assert gap(wide) > gap(tight)

    def test_multi_run_draws_independent_underlines(self, tmp_path: Path) -> None:
        """Two underlined runs separated by a plain glyph draw two underlines."""
        result = self._export(tmp_path)
        label = result.rendered_labels["plt_multi_run"].source_label
        # `A<u>bb</u>C<u>dd</u>`: two runs, each descender-free, so two segments.
        assert self._underline_stroke_count(label) >= 2

    def test_partial_run_engraves_within_the_line(self, tmp_path: Path) -> None:
        """A partial run underlines only its glyphs, within the line's width."""
        result = self._export(tmp_path)
        partial = self._line_bounds(result.rendered_labels["plt_partial"].source_label)
        plain = self._line_bounds(result.rendered_labels["plt_partial_plain"].source_label)
        # The underline sits inside the line's ink, so the block width is the
        # whole line's (same as the plain twin); the underline is present.
        assert partial[2] - partial[0] == pytest.approx(plain[2] - plain[0], rel=1e-6)
        assert self._underline_stroke_count(result.rendered_labels["plt_partial"].source_label) >= 1

    def test_nested_markup_shears_and_underlines(self, tmp_path: Path) -> None:
        """``<i>...<u>...</u>...</i>`` shears every glyph, underlines the run."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}

        nested = by_id["plt_nested"].content[0]
        assert nested.plain_text == "nested markup tail"
        # Exactly one run is underlined, and it is the inner ``markup``.
        underlined_runs = [run for run in nested.styled_runs if run.style.underline]
        assert [run.text for run in underlined_runs] == ["markup"]
        # Every run is italic (the outer ``<i>``), and the underline is engraved.
        assert all(run.style.italic for run in nested.styled_runs)
        assert self._underline_stroke_count(by_id["plt_nested"]) >= 1

    def test_ttf_underline_engraves(self, tmp_path: Path) -> None:
        """The TTF path underlines with the same centerline geometry."""
        result = self._export(tmp_path)
        underlined = self._line_bounds(result.rendered_labels["ttf_underline"].source_label)
        plain = self._line_bounds(result.rendered_labels["ttf_plain"].source_label)
        # The TTF `p` descender is deeper than the underline, so the block is
        # unchanged -- but the underline is still engraved.
        assert underlined == pytest.approx(plain, abs=1e-9)
        assert (
            self._underline_stroke_count(result.rendered_labels["ttf_underline"].source_label) >= 1
        )

    def test_underline_never_moves_the_cutter(self, tmp_path: Path) -> None:
        """The underline is a geometry transform: the cutter set is unchanged."""
        result = self._export(tmp_path)
        assert sorted(p.name for p in result.plt_paths) == [
            "0.015_bh_ul.plt",
            "0.060_txt_ul.plt",
        ]

    def test_break_spaces_splits_a_multi_word_run(self, tmp_path: Path) -> None:
        """``underline_break_spaces`` breaks a multi-word run at the space.

        ``plt_continuous_words`` (the default) draws one continuous underline
        across the space; ``plt_break_spaces_words`` ends the underline at the
        space, so each word gets its own underline and the space stays bare.
        """
        result = self._export(tmp_path)
        continuous = result.rendered_labels["plt_continuous_words"].source_label
        broken = result.rendered_labels["plt_break_spaces_words"].source_label

        # The default run is one continuous underline; the flag splits it in two.
        assert self._underline_stroke_count(continuous) == 1
        assert self._underline_stroke_count(broken) == 2

        # The flag's two spans leave the inter-word space bare: the first ends
        # at 'hello' ink, the second starts at 'world' ink.
        from plt_optimizer.generate.label_renderer import (
            TextChunkMode,
            _render_line_block,
            _underline_render_gate,
            _underline_strokes,
        )

        block, _words, glyphs = _render_line_block(broken.content[0], TextChunkMode.LINE)
        strokes = _underline_strokes(
            broken.content[0], block, glyphs, _underline_render_gate(broken.content[0])
        )
        spans = sorted((s.segments[0].start.real, s.segments[0].end.real) for s in strokes)
        assert len(spans) == 2
        # A visible bare gap between the two words' underlines.
        assert spans[1][0] - spans[0][1] > 0.1
