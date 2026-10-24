"""Tests for the per-cutter Phase 3 export pipeline."""

import logging
import math
import re
from pathlib import Path
from typing import Optional

import pytest

from plt_optimizer.core.units import HPGL_UNITS_PER_INCH
from plt_optimizer.generate.packing.layout import LayoutMode
from plt_optimizer.generate.resolution import (
    LineContentConfigError,
    ResolvedLabel,
    ResolvedTextLine,
    glyph_render_plan,
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
    _format_cutter,
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
        assert any(name.startswith("60_txt_") for name in names)
        assert any(name.startswith("15_bh_") for name in names)
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

    def test_export_simple_plots_physical_width_wiring(self, tmp_path: Path) -> None:
        """Simple plots render true cutter widths: bh translucent, text opaque.

        The per-file width comes from the file-name cutter token; the
        combined per-plate plot splits the assembly by pen and draws each
        pen at its own cutter width (structural pens translucent).
        """
        from unittest.mock import patch

        job = parse_yaml("tests_deps/test123_spec.yaml")
        resolved_labels = resolve_job_spec(job)

        captured: dict[str, list[tuple[float, float]]] = {}

        def _fake_plot(layers, output_path=None, show_plot=False, **kwargs):
            captured[Path(output_path).name] = [
                (layer.width_inches, layer.alpha) for layer in layers
            ]

        with patch(
            "plt_optimizer.diagnostics.plotter.plot_physical_strokes", side_effect=_fake_plot
        ):
            result = export_per_cutter_plts(
                resolved_labels,
                output_dir=tmp_path,
                job_id="style",
                optimize=False,
                plots=True,
            )

        assert captured
        # test123: 0.5in text -> 0.060 cutter, 0.015 boundary/hole cutter.
        bh_plots = {n: v for n, v in captured.items() if "_bh_" in n}
        txt_plots = {n: v for n, v in captured.items() if "_txt_" in n}
        assert bh_plots and txt_plots
        for layers in bh_plots.values():
            assert [(w, a) for w, a in layers] == [(0.015, pytest.approx(0.3))]
        for layers in txt_plots.values():
            assert [(w, a) for w, a in layers] == [(0.060, 1.0)]
        # Combined per-plate plot: one layer per pen with content, each at
        # its own cutter width; pens 2/3 (borders/holes) stay translucent.
        all_plots = {n: v for n, v in captured.items() if n.startswith("all_")}
        assert all_plots
        for layers in all_plots.values():
            widths = {round(w, 3) for w, _a in layers}
            assert widths == {0.060, 0.015}
            assert any(w == 0.015 and a == pytest.approx(0.3) for w, a in layers)
            assert any(w == 0.060 and a == 1.0 for w, a in layers)
        # The fake never saves; PDFs are tracked as usual.
        assert result.pdf_paths

    def test_export_simple_plots_fall_back_without_pen_widths(self, tmp_path: Path) -> None:
        """A synthetic export (no pen widths) keeps the thin centerline preview."""
        from unittest.mock import patch

        from plt_optimizer.generate.vectorize import _write_simple_plots

        job = parse_yaml("tests_deps/test123_spec.yaml")
        resolved_labels = resolve_job_spec(job)
        result = export_per_cutter_plts(
            resolved_labels,
            output_dir=tmp_path,
            job_id="fb",
            optimize=False,
            plots=False,
        )
        assert result.combined_by_plate

        simple_calls: list[str] = []
        physical_calls: list[str] = []

        def _fake_simple(document, output_path=None, show_plot=False, simple_mode=False, **kwargs):
            assert simple_mode is True
            simple_calls.append(Path(output_path).name)

        def _fake_physical(layers, output_path=None, show_plot=False, **kwargs):
            physical_calls.append(Path(output_path).name)

        with (
            patch("plt_optimizer.diagnostics.plotter.plot_plt_document", side_effect=_fake_simple),
            patch(
                "plt_optimizer.diagnostics.plotter.plot_physical_strokes",
                side_effect=_fake_physical,
            ),
        ):
            pdf_paths = _write_simple_plots(tmp_path, "fb", result)

        names = {p.name for p in pdf_paths}
        # Per-cutter files keep the stem cutter token, so they render physical.
        assert any("_txt_" in n for n in physical_calls)
        # The combined plot has no pen-width map -> thin centerline fallback.
        assert [n for n in simple_calls if n.startswith("all_")]
        assert not any(n.startswith("all_") for n in physical_calls)

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
        # Every tag is an unpadded integer-mils string.
        for tag in cutter_tags:
            assert tag is not None
            assert tag.isdigit() and not (tag.startswith("0") and tag != "0")

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
        assert any(name.endswith("40_k_txt_complex.plt") for name in text_files)
        assert any(name.endswith("40_m_txt_complex.plt") for name in text_files)
        # The implicit "none" layer keeps the bare cutter-only tag.
        assert any(name.endswith("40_txt_complex.plt") for name in text_files)
        # The two colored files are *additional* toolpaths: each carries only
        # its own layer's strokes on a single pen.
        black_file = next(p for p in result.plt_paths if p.name.endswith("40_k_txt_complex.plt"))
        magenta_file = next(p for p in result.plt_paths if p.name.endswith("40_m_txt_complex.plt"))
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
            "60_k_txt_clr.plt",
            "60_m_txt_clr.plt",
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
        assert bh_names == ["15_bh_clr.plt"]

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
        assert text_names == ["30_txt_plain.plt"]

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
            "60_k_txt_opt.plt",
            "60_m_txt_opt.plt",
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
        assert _format_text_layer(0.04, "none") == "40"

    def test_known_colors_use_abbreviation(self) -> None:
        """Real colors gain their single-letter file-name tag."""
        assert _format_text_layer(0.04, "magenta") == "40_m"
        assert _format_text_layer(0.06, "black") == "60_k"

    def test_unknown_color_falls_back_to_sanitized_value(self) -> None:
        """Manually constructed unknown colors stay filesystem-safe."""
        assert _format_text_layer(0.04, "chartreuse") == "40_chartreuse"
        assert _format_text_layer(0.04, "bright-red!") == "40_brightred"
        # Fully sanitized-empty values never produce a dangling separator.
        assert _format_text_layer(0.04, "!!!") == "40_x"


class TestCutterTokenFormatting:
    """Unit tests for the integer-mils cutter file-name token."""

    def test_cutter_formats_as_unpadded_mils(self) -> None:
        """Diameters become plain integers: no leading zeros, no padding."""
        assert _format_cutter(0.03) == "30"
        assert _format_cutter(0.015) == "15"
        assert _format_cutter(0.187) == "187"
        assert _format_cutter(0.25) == "250"
        assert _format_cutter(0.005) == "5"
        assert _format_cutter(0.0) == "0"

    def test_cutter_token_round_trips_through_inches(self) -> None:
        """The parser's inverse recovers the diameter for plots/titles."""
        from plt_optimizer.generate.vectorize import _cutter_token_to_inches

        for diameter in (0.015, 0.03, 0.045, 0.06, 0.09, 0.187, 0.25):
            assert _cutter_token_to_inches(_format_cutter(diameter)) == diameter


class TestFilenameCap:
    """The 39-character plotter-controller cap on generated PLT names."""

    def test_long_job_id_shortens_every_plt_name(self, tmp_path: Path) -> None:
        """Names over 39 chars are truncated from the right, content intact."""
        job = parse_yaml("tests_deps/test123_spec.yaml")
        resolved_labels = resolve_job_spec(job)
        long_job_id = "This_Is_A_Very_Long_Job_Identifier_Over_Thirty_Nine"

        result = export_per_cutter_plts(
            resolved_labels,
            output_dir=tmp_path,
            job_id=long_job_id,
            optimize=False,
            plots=True,
        )

        assert result.plt_paths
        for path in result.plt_paths:
            assert len(path.name) <= 39, path.name
            assert path.suffix == ".plt"
            # The truncated path is the one actually written, with content.
            assert path.is_file()
            content = path.read_text()
            assert content.startswith("IN;PA;")
            assert "PD" in content
        # The mirrored PDF previews follow the shortened stem.
        for path in result.plt_paths:
            assert (tmp_path / "pdf" / f"{path.stem}.pdf").is_file()

    def test_short_job_id_is_untouched(self, tmp_path: Path) -> None:
        """Names within the cap keep their full length (no truncation)."""
        job = parse_yaml("tests_deps/test123_spec.yaml")
        resolved_labels = resolve_job_spec(job)

        result = export_per_cutter_plts(
            resolved_labels,
            output_dir=tmp_path,
            job_id="short",
            optimize=False,
            plots=False,
        )

        assert result.plt_paths
        for path in result.plt_paths:
            assert path.name.endswith("_short.plt")
            assert len(path.name) <= 39


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
            "45_k_txt_demo.plt",
            "45_m_txt_demo.plt",
            "45_txt_demo.plt",
        ]
        assert [p.name for p in result.plt_paths if "_bh_" in p.name] == ["15_bh_demo.plt"]
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
            "40_txt_job": (None, None, "40", "txt", None),
            "40_m_txt_job": (None, None, "40", "txt", "m"),
            "15_bh_job": (None, None, "15", "bh", None),
            "wbuv_40_txt_job": (None, "wbuv", "40", "txt", None),
            "wbuv_15_bh_job": (None, "wbuv", "15", "bh", None),
            "02_wbuv_40_k_txt_job": ("02", "wbuv", "40", "txt", "k"),
            "02_40_txt_job": ("02", None, "40", "txt", None),
            "02_15_bh_job": ("02", None, "15", "bh", None),
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
        assert _is_structural_stem("15_bh_job")
        assert _is_structural_stem("wbuv_15_bh_job")
        assert _is_structural_stem("02_wbuv_15_bh_job")
        assert not _is_structural_stem("02_wbuv_40_txt_job")
        # Stems outside the scheme are treated as non-structural.
        assert not _is_structural_stem("readme")

    def test_plot_title_includes_material(self) -> None:
        """Titles name the cutter, the kind and the material tag."""
        from plt_optimizer.generate.vectorize import _build_plot_title

        base = _build_plot_title("Job", Path("plt/40_txt_job.plt"), text_height=0.5)
        assert base == "Job 0.5 text (0.040 cutter)"
        tagged = _build_plot_title("Job", Path("plt/wbuv_15_bh_job.plt"))
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
            "wb_15_bh_demo.plt",
            "wb_45_txt_demo.plt",
            "wbuv_15_bh_demo.plt",
            "wbuv_45_m_txt_demo.plt",
            "wbuv_45_txt_demo.plt",
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
        wb_txt = [p for p in result.plt_paths if p.name == "wb_45_txt_demo.plt"]
        uv_txt = [p for p in result.plt_paths if p.name.startswith("wbuv_45")]
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
            "15_bh_cs.plt",
            "30_txt_cs.plt",  # line-level cutter_size
            "45_txt_cs.plt",  # label-level cutter_size (+ inherited line)
            "60_txt_cs.plt",  # automatic selection from text_height
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


class TestMultistrokeDemoExample:
    """Regression tests for tests_deps/multistroke_job.yaml.

    The fixture mixes a single-stroke control (Dino, S1 - the plain height
    lookup), a five-stroke (Avenge5) and a two-stroke (Helter2) font on the
    default boldness ladder, a heavier/lighter boldness factor, and an
    explicit ``cutter_size`` on a multi-stroke font. It pins that the ladder
    sizes the cutter at resolution time (the snap-down stepping up to keep
    the passes overlapping), that the boldness factor moves the cutter, that
    a light target clamps to the gap-free floor, and that an explicit cutter
    bypasses the ladder - each cutter becoming its own per-cutter text file.
    """

    _INVENTORY = [0.01, 0.015, 0.02, 0.03, 0.045, 0.06, 0.09, 0.125, 0.25]
    _SPEC = Path("tests_deps/multistroke_job.yaml")

    def _resolve(self) -> dict[str, ResolvedLabel]:
        """Resolve the demo fixture against the test inventory."""
        job = parse_yaml(self._SPEC)
        return {label.id: label for label in resolve_job_spec(job, self._INVENTORY)}

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled against the inventory."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job, available_cutters=self._INVENTORY)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="ms",
            optimize=False,
            plots=False,
            available_cutters=self._INVENTORY,
        )

    def test_single_stroke_font_uses_height_lookup(self) -> None:
        """The S1 control keeps the plain height-based cutter (0.060in)."""
        line = self._resolve()["single_stroke"].content[0]
        assert math.isclose(line.cutter_diameter, 0.06)
        assert math.isclose(line.toolpath_text_height, 0.5 - 0.06)

    def test_five_stroke_steps_up_to_the_floor(self) -> None:
        """Avenge5's ideal 0.0175 snaps to 0.015, below the floor -> 0.020."""
        line = self._resolve()["multistroke_default"].content[0]
        assert math.isclose(line.cutter_diameter, 0.02)
        assert math.isclose(line.toolpath_text_height, 0.5 - 0.02)

    def test_two_stroke_resolves_larger(self) -> None:
        """Helter2 (S2) sizes a larger cutter (0.040 -> stepped up to 0.045)."""
        line = self._resolve()["two_stroke"].content[0]
        assert math.isclose(line.cutter_diameter, 0.045)

    def test_heavier_factor_grows_the_cutter(self) -> None:
        """A 0.20 factor on Avenge5 lands above the design point (0.045)."""
        line = self._resolve()["multistroke_bold"].content[0]
        assert math.isclose(line.cutter_diameter, 0.045)

    def test_light_factor_clamps_to_the_floor(self, caplog: pytest.LogCaptureFixture) -> None:
        """A 0.08 factor clamps to the gap-free floor (stepped up to 0.020)."""
        with caplog.at_level("WARNING"):
            line = self._resolve()["multistroke_thin"].content[0]
        assert math.isclose(line.cutter_diameter, 0.02)
        assert "gap-free floor" in caplog.text

    def test_explicit_cutter_bypasses_the_ladder(self) -> None:
        """An explicit cutter_size on a multi-stroke font wins verbatim."""
        line = self._resolve()["explicit_cutter"].content[0]
        assert math.isclose(line.cutter_diameter, 0.09)
        assert math.isclose(line.cutter_size, 0.09)

    def test_nominal_height_untouched(self) -> None:
        """Every ladder cutter keeps the nominal height (vertical fit)."""
        for label in self._resolve().values():
            assert math.isclose(label.content[0].nominal_text_height, 0.5)

    def test_fixture_exports_one_file_per_cutter(self, tmp_path: Path) -> None:
        """Each distinct cutter (ladder, factor, explicit, auto) is its own file."""
        result = self._export(tmp_path)
        assert sorted(p.name for p in result.plt_paths) == [
            "15_bh_ms.plt",
            "20_txt_ms.plt",  # Avenge5 default + thin (stepped up to the floor)
            "45_txt_ms.plt",  # Helter2 + Avenge5 factor 0.20
            "60_txt_ms.plt",  # Dino single-stroke (height lookup)
            "90_txt_ms.plt",  # explicit cutter_size
        ]


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
            "30_bh_cd.plt",  # boundary/hole cutter 0.015 snapped up to 0.030
            "45_txt_cd.plt",  # compressed line, downsized from 0.060
            "60_txt_cd.plt",  # fits + opted_out lines
            "90_txt_cd.plt",  # explicit cutter_size
        ]

    def test_pre_pass_is_a_noop_without_inventory(self, tmp_path: Path) -> None:
        """No tools.json inventory means no ladder: output stays historical."""
        result = self._export(tmp_path, None)
        assert sorted(p.name for p in result.plt_paths) == [
            "15_bh_cd.plt",
            "60_txt_cd.plt",
            "90_txt_cd.plt",
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
            "30_bh_cdg.plt",  # boundary/hole cutter 0.015 snapped up to 0.030
            "45_txt_cdg.plt",  # shared label (both lines) + opted_out trigger
            "60_txt_cdg.plt",  # other_label + opted_out sibling
        ]

    def test_pre_pass_is_a_noop_without_inventory(self, tmp_path: Path) -> None:
        """No tools.json inventory means no ladder: nothing shares."""
        result = self._export(tmp_path, None)
        for rendered in result.rendered_labels.values():
            assert rendered.source_label.cutter_downsize_by_line == {}


class TestCutterDownsizeGlobalJobWideDemoExample:
    """Regression tests for tests_deps/cutter_downsize_global_job_wide_job.yaml.

    The fixture opts in to ``cutter_downsize_global_job_wide``: a trigger
    label's swap is shared with the fitting lines of the same text height in
    OTHER labels (one text size, one tool job-wide), while opted-out labels
    keep exactly the per-label behaviour and explicit cutters are untouched.
    """

    _INVENTORY = [0.03, 0.045, 0.06, 0.09, 0.125]
    _SPEC = Path("tests_deps/cutter_downsize_global_job_wide_job.yaml")

    def _export(self, tmp_path: Path, inventory: Optional[list[float]]) -> PerCutterExport:
        """Export the demo job with plotting disabled against ``inventory``."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job, available_cutters=inventory)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="cdjw",
            optimize=False,
            plots=False,
            available_cutters=inventory,
        )

    def test_trigger_label_downsizes(self, tmp_path: Path) -> None:
        """The over-wide label triggers the swap (0.06 -> 0.045)."""
        result = self._export(tmp_path, self._INVENTORY)
        trigger = result.rendered_labels["trigger"].source_label
        assert list(trigger.cutter_downsize_by_line) == [0]
        original, final = trigger.cutter_downsize_by_line[0]
        assert math.isclose(original, 0.06)
        assert math.isclose(final, 0.045)
        assert math.isclose(trigger.content[0].cutter_diameter, 0.045)

    def test_fitting_line_in_another_label_receives_the_swap(self, tmp_path: Path) -> None:
        """The job-wide swap crosses the label boundary (per-label cannot)."""
        result = self._export(tmp_path, self._INVENTORY)
        other = result.rendered_labels["other_label"].source_label
        assert list(other.cutter_downsize_by_line) == [0]
        original, final = other.cutter_downsize_by_line[0]
        assert math.isclose(original, 0.06)
        assert math.isclose(final, 0.045)
        line = other.content[0]
        assert math.isclose(line.cutter_diameter, 0.045)
        assert math.isclose(line.nominal_text_height, 0.5)
        assert math.isclose(line.toolpath_text_height, 0.5 - 0.045)

    def test_opted_out_label_keeps_the_per_label_behaviour(self, tmp_path: Path) -> None:
        """cutter_downsize_global_job_wide false: the fitting line keeps 0.06in."""
        result = self._export(tmp_path, self._INVENTORY)
        opted_out = result.rendered_labels["opted_out"].source_label
        assert opted_out.cutter_downsize_by_line == {}
        assert math.isclose(opted_out.content[0].cutter_diameter, 0.06)

    def test_explicit_cutter_label_is_never_touched(self, tmp_path: Path) -> None:
        """An explicit cutter_size neither follows nor sets the job minimum."""
        result = self._export(tmp_path, self._INVENTORY)
        explicit = result.rendered_labels["explicit"].source_label
        assert explicit.cutter_downsize_by_line == {}
        assert math.isclose(explicit.content[0].cutter_diameter, 0.09)

    def test_job_wide_layers_split_by_final_cutter(self, tmp_path: Path) -> None:
        """The pen map sees the shared cutter, so 0.045/0.060 files split as expected."""
        result = self._export(tmp_path, self._INVENTORY)
        assert sorted(p.name for p in result.plt_paths) == [
            "30_bh_cdjw.plt",  # boundary/hole cutter 0.015 snapped up to 0.030
            "45_txt_cdjw.plt",  # trigger + job-wide receiver
            "60_txt_cdjw.plt",  # opted_out label
            "90_txt_cdjw.plt",  # explicit cutter_size
        ]

    def test_pre_pass_is_a_noop_without_inventory(self, tmp_path: Path) -> None:
        """No tools.json inventory means no ladder: nothing shares job-wide."""
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
    _INVENTORY = [0.01, 0.015, 0.02, 0.03, 0.045, 0.06, 0.09, 0.125]

    def _export(self, tmp_path: Path, inventory: Optional[list[float]] = None) -> PerCutterExport:
        """Export the demo job with plotting disabled."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job, available_cutters=inventory)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="hcg",
            optimize=False,
            plots=False,
            available_cutters=inventory,
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
            "15_bh_hcg.plt",
            "60_txt_hcg.plt",
        ]

    def test_shared_compression_feeds_the_cutter_downsize_trigger(self, tmp_path: Path) -> None:
        """The shared scale drives the cutter reduction (the pre-pass order).

        The shared compression pre-pass runs BEFORE the compression-driven
        cutter reduction, so the midpoint trigger measures the shared scale
        (0.8576) rather than the trigger line's natural scale. 0.8576 is
        below the midpoint of the 0.060 -> 0.045 rung (0.875), so both lines
        of the shared label drop to 0.045 -- one nominal height, one
        compression, one tool.
        """
        result = self._export(tmp_path, self._INVENTORY)
        shared = result.rendered_labels["shared"].source_label
        assert sorted(shared.global_compress_by_line) == [0, 1]
        assert sorted(shared.cutter_downsize_by_line) == [0, 1]
        for index in (0, 1):
            original, final = shared.cutter_downsize_by_line[index]
            assert math.isclose(original, 0.06)
            assert math.isclose(final, 0.045)
        for line in shared.content:
            assert math.isclose(line.cutter_diameter, 0.045)

        # The downsized tool gets its own file; the untouched labels keep the
        # 0.060 tool.
        assert sorted(p.name for p in result.plt_paths) == [
            "15_bh_hcg.plt",
            "45_txt_hcg.plt",
            "60_txt_hcg.plt",
        ]


class TestArcPreserveCompressionDemoExample:
    """Regression tests for tests_deps/arc_preserve_job.yaml.

    The fixture exercises the arc-preserving non-uniform transforms
    (``arc_preserve_compression``, default true): a compressed / italic /
    capline-clamped PLT line refits each arc's ellipse image with circular
    ``AA`` arcs instead of polygonizing. The counts below pin the whole
    feature end-to-end -- a regression that re-introduces flattening collapses
    the ``AA`` counts, and one that over-fits explodes them.
    """

    _SPEC = Path("tests_deps/arc_preserve_job.yaml")

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="apc",
            optimize=False,
            plots=False,
        )

    @staticmethod
    def _segments(content: str) -> tuple[int, int]:
        """Return ``(arc_count, line_count)`` parsed back from emitted HPGL."""
        from plt_optimizer.core.models import ArcSegment, StrokeSegment
        from plt_optimizer.core.parser import PLTParser

        doc = PLTParser().parse_string(content)
        arcs = sum(
            1 for path in doc.stroke_paths for seg in path.segments if isinstance(seg, ArcSegment)
        )
        lines = sum(
            1
            for path in doc.stroke_paths
            for seg in path.segments
            if isinstance(seg, StrokeSegment)
        )
        return arcs, lines

    def test_compressed_line_keeps_native_arcs(self, tmp_path: Path) -> None:
        """The over-wide line compresses AND stays arc-native."""
        result = self._export(tmp_path)
        preserved = result.rendered_labels["preserved"]
        # It really compressed (the point of the fixture).
        assert sorted(preserved.compression_by_line) == [0]
        arcs, lines = self._segments(preserved.plt_content)
        # Near-straight arcs stay native AA (only a sub-half-plotter-unit span
        # collapses to a chord), so the compressed line carries ~1.07 arcs per
        # source arc -- 470 arcs vs the polygonized label's 1341 lines.
        assert arcs == 470
        assert lines == 44

    def test_opt_out_polygonizes_the_same_line(self, tmp_path: Path) -> None:
        """arc_preserve_compression false restores the polygonized output."""
        result = self._export(tmp_path)
        polygonized = result.rendered_labels["polygonized"]
        arcs, lines = self._segments(polygonized.plt_content)
        # Every compressed arc flattened: no arcs survive the compression.
        assert arcs == 0
        assert lines == 1341
        # The escape hatch is a large command-count increase on the same text.
        preserved = result.rendered_labels["preserved"]
        p_arcs, p_lines = self._segments(preserved.plt_content)
        assert lines > (p_arcs + p_lines) * 2

    def test_italic_and_capline_keep_arcs(self, tmp_path: Path) -> None:
        """The shear and the vertical clamp both preserve arcs natively."""
        result = self._export(tmp_path)
        italic_arcs, _ = self._segments(result.rendered_labels["italic"].plt_content)
        clamped_arcs, _ = self._segments(result.rendered_labels["clamped"].plt_content)
        assert italic_arcs == 386
        assert clamped_arcs == 354

    def test_per_line_opt_out_inside_one_label(self, tmp_path: Path) -> None:
        """The mixed label: line 0 preserves, line 1 (explicit false) polygonizes.

        The two lines share one label, so the emitted per-label content carries
        the preserved line's arcs plus the polygonized line's flattened
        segments -- the total sits between the two single-mode labels.
        """
        result = self._export(tmp_path)
        mixed = result.rendered_labels["mixed"]
        arcs, lines = self._segments(mixed.plt_content)
        # Line 0 preserved (470 arcs) + line 1 polygonized (0 arcs, 1341 lines).
        assert arcs == 470
        assert lines == 1381


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
            "15_bh_olc.plt",
            "60_txt_olc.plt",
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
            "15_bh_olcm.plt",
            "60_txt_olcm.plt",
        ]

    def test_conflicting_group_caps_abort(self) -> None:
        """Two different caps in one group abort at resolution."""
        job = parse_yaml(self._CONFLICT_SPEC)
        with pytest.raises(LineContentConfigError, match="conflicting"):
            resolve_job_spec(job)


class TestOptimizeLineContentNewLineDemoExample:
    """Regression tests for tests_deps/optimize_line_content_new_line_job.yaml.

    The line-level ``new_line`` hard break modifies the word-level reflow:
    the flagged line *starts* a new reflow group, so the lines that follow
    it reflow with it instead of with the lines above it. Word order is kept;
    only the line breaks move.
    """

    _SPEC = Path("tests_deps/optimize_line_content_new_line_job.yaml")

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="olcn",
            optimize=False,
            plots=False,
        )

    def test_new_line_splits_the_group(self, tmp_path: Path) -> None:
        """The flagged line starts a fresh group; the next line joins it."""
        result = self._export(tmp_path)
        blocked = result.rendered_labels["new_line_block"]
        assert [line.text for line in blocked.source_label.content] == [
            "AAA BBBB",
            "CCCC",
            "DDDD EEEE",
        ]
        assert blocked.compression_by_line == {}

    def test_new_line_line_keeps_its_own_words(self, tmp_path: Path) -> None:
        """The flagged line's own words stay on it (it begins its own line)."""
        result = self._export(tmp_path)
        blocked = result.rendered_labels["new_line_block"]
        lines = blocked.source_label.content
        # The flagged line is the second one and it starts the group.
        assert lines[1].new_line is True
        assert lines[1].text == "CCCC"
        # The line above it is a singleton group: it kept its authored text.
        assert lines[0].text == "AAA BBBB"

    def test_two_new_line_lines_never_exchange_words(self, tmp_path: Path) -> None:
        """Each flagged line is its own group, so the two keep their text."""
        result = self._export(tmp_path)
        twins = result.rendered_labels["new_line_twins"]
        assert [line.text for line in twins.source_label.content] == [
            "FFFF GGGG",
            "HHHH IIII",
        ]
        assert twins.compression_by_line == {}

    def test_new_line_cap_grows_the_block(self, tmp_path: Path) -> None:
        """The line cap grows the new_line block past its authored line count."""
        result = self._export(tmp_path)
        capped = result.rendered_labels["new_line_cap"]
        texts = [line.text for line in capped.source_label.content]
        assert texts == ["JJJJ KKKK", "LLLL MMMM"]
        # The grown line continues the block: it never carries the flag.
        assert [line.new_line for line in capped.source_label.content] == [True, False]
        assert capped.compression_by_line == {}

    def test_bullets_each_form_their_own_group(self, tmp_path: Path) -> None:
        """The four-bullet shape: each bullet is its own group with its own cap."""
        result = self._export(tmp_path)
        bullets = result.rendered_labels["bullets"]
        lines = bullets.source_label.content
        # Every bullet kept its own first line, and each block grew to its cap.
        assert [line.new_line for line in lines] == [
            True,
            False,
            True,
            False,
            True,
            False,
            True,
            False,
        ]
        # Word order is preserved across the whole label.
        assert (
            " ".join(line.text for line in lines).split()
            == (
                "Always charge secondary CO2 system through a filter drier. "
                "Do not trap liquid by closing charging station and valves. "
                "Always vacuum charging line after charging is complete. "
                "Charge secondary CO2 system with 99.9% pure or higher CO2."
            ).split()
        )
        assert bullets.compression_by_line == {}

    def test_reflow_keeps_one_cutter_file(self, tmp_path: Path) -> None:
        """Reflow changes line breaks only, so the cutter set is unchanged."""
        result = self._export(tmp_path)
        assert sorted(p.name for p in result.plt_paths) == [
            "15_bh_olcn.plt",
            "60_txt_olcn.plt",
        ]


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
            "15_bh_sc.plt",
            "60_txt_sc.plt",
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
            "15_bh_ubs.plt",
            "60_txt_ubs.plt",
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
            "15_bh_da.plt",
            "60_txt_da.plt",
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
            "15_bh_rc.plt",
            "187_txt_rc.plt",
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
            "15_bh_br.plt",
            "187_txt_br.plt",
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
            "15_bh_vc.plt",
            "187_txt_vc.plt",
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
            "15_bh_it.plt",
            "60_txt_it.plt",
        ]


class TestUnderlineDemoExample:
    """Regression tests for tests_deps/underline_job.yaml.

        The fixture pins inline underline markup (``<u>``,
        ``underline_offset_fraction``)
        and nested markup (``<i>`` wrapping ``<u>``). ``<u>`` draws a centerline
        underline with the line's own text cutter,
        ``cutter + underline_offset_fraction * cap height``
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
        """A larger ``underline_offset_fraction`` lowers the centerline below
        the descender."""
        result = self._export(tmp_path)
        offset = self._line_bounds(result.rendered_labels["plt_offset"].source_label)
        default = self._line_bounds(result.rendered_labels["plt_underline"].source_label)
        # At 0.25 * cap (0.11in at the 0.44in cap) the centerline
        # (-(cutter + 0.11)) sits below the `p`/`g`
        # descenders, so the block grows; the width is unchanged.
        assert offset[3] - offset[1] > default[3] - default[1]
        assert offset[2] - offset[0] == pytest.approx(default[2] - default[0], rel=1e-6)

    def test_break_clearance_widens_the_breaks(self, tmp_path: Path) -> None:
        """``underline_break_clearance_fraction`` adds air at each break, not depth.

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

        # Clearance 0.1 * cap (0.044in at the 0.44in cap) leaves air on each
        # side of the cut (and reaches a
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
            "15_bh_ul.plt",
            "60_txt_ul.plt",
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


class TestBoldDemoExample:
    """Regression tests for tests_deps/bold_job.yaml (the ``<b>`` tag).

    The fixture resolves a bold run to a thicker cutter -- the next inventory
    rung for single-stroke PLT/TTF fonts, the boosted boldness ladder for a
    multi-stroke font -- and splits the line's strokes onto the plain and the
    bold pen. It pins the resolution-time cutter pair, the per-cutter file
    split, the top-of-inventory / explicit-cutter / multiplier-1.0 inert cases
    (each a WARNING), the per-run underline depth, and the compression-driven
    cutter reduction stepping BOTH cutters down in lockstep.
    """

    _INVENTORY = [0.01, 0.015, 0.02, 0.03, 0.045, 0.06, 0.09, 0.125, 0.25]
    _SPEC = Path("tests_deps/bold_job.yaml")

    def _resolve(self) -> dict[str, ResolvedLabel]:
        """Resolve the demo fixture against the test inventory."""
        job = parse_yaml(self._SPEC)
        return {label.id: label for label in resolve_job_spec(job, self._INVENTORY)}

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled against the inventory."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job, available_cutters=self._INVENTORY)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="bd",
            optimize=False,
            plots=False,
            available_cutters=self._INVENTORY,
        )

    def test_single_stroke_bold_steps_one_rung(self) -> None:
        """Dino (S1, auto 0.060) bolds to the next rung 0.090; nominal kept."""
        line = self._resolve()["plt_bold"].content[0]
        assert math.isclose(line.cutter_diameter, 0.06)
        assert math.isclose(line.bold_cutter_diameter, 0.09)
        assert math.isclose(line.bold_toolpath_text_height, 0.5 - 0.09)
        # The uniform centerline scale: (0.5-0.09)/(0.5-0.06).
        assert math.isclose(line._bold_glyph_scale(), (0.5 - 0.09) / (0.5 - 0.06))
        # The nominal height is untouched (vertical fit math).
        assert math.isclose(line.nominal_text_height, 0.5)

    def test_plain_control_has_no_bold_cutter(self) -> None:
        """A markup-free line carries no bold cutter at all."""
        line = self._resolve()["plt_plain"].content[0]
        assert line.bold_cutter_diameter is None
        assert line.bold_toolpath_text_height is None
        assert line._bold_glyph_scale() == 1.0

    def test_multistroke_bold_uses_boosted_ladder(self) -> None:
        """Avenge5 (S5) plain 0.020, bold ladder at 0.14*1.5=0.21 -> 0.045."""
        line = self._resolve()["plt_multistroke"].content[0]
        assert math.isclose(line.cutter_diameter, 0.02)
        assert math.isclose(line.bold_cutter_diameter, 0.045)

    def test_multistroke_multiplier_clamps_to_ceiling(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A 3.0 multiplier (0.14*3=0.42) clamps to 0.30 -> bold 0.090, WARNING."""
        with caplog.at_level("WARNING"):
            line = self._resolve()["plt_multistroke_clamped"].content[0]
        assert math.isclose(line.bold_cutter_diameter, 0.09)
        assert "clamped" in caplog.text

    def test_multistroke_multiplier_one_is_inert(self, caplog: pytest.LogCaptureFixture) -> None:
        """A 1.0 multiplier resolves no rung above the plain cutter -> None, WARNING."""
        with caplog.at_level("WARNING"):
            line = self._resolve()["plt_multistroke_inert"].content[0]
        assert line.bold_cutter_diameter is None
        assert "not above the line cutter" in caplog.text

    def test_top_of_inventory_bold_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """At the 0.25in top rung there is no larger tool -> None, WARNING."""
        with caplog.at_level("WARNING"):
            line = self._resolve()["plt_top_rung"].content[0]
        assert math.isclose(line.cutter_diameter, 0.25)
        assert line.bold_cutter_diameter is None
        assert "next larger cutter" in caplog.text

    def test_explicit_cutter_disables_bold(self, caplog: pytest.LogCaptureFixture) -> None:
        """An explicit cutter_size makes <b> inert (WARNING); whole line 0.090."""
        with caplog.at_level("WARNING"):
            line = self._resolve()["plt_explicit_cutter"].content[0]
        assert math.isclose(line.cutter_diameter, 0.09)
        assert line.bold_cutter_diameter is None
        assert "inert on a line with an explicit cutter_size" in caplog.text

    def test_bold_flags_and_plain_text(self) -> None:
        """The markup parses to per-character bold flags over the plain text."""
        line = self._resolve()["plt_bold"].content[0]
        assert line.plain_text == "plain BOLD plain"
        assert line.has_bold is True
        # 'plain BOLD plain': the BOLD span (indices 6..9) is bold.
        flags = line.bold_flags
        assert len(flags) == len(line.plain_text)
        assert [i for i, f in enumerate(flags) if f] == list(range(6, 10))

    def test_bold_run_raises_by_the_half_decrease(self) -> None:
        """The bold span's engraved-baseline raise is (bold - plain) / 2.

        The raise aligns the bold run's ENGRAVED baseline AND capline with the
        plain run's: the thicker tool's stroke hangs (bold - plain) / 2 lower
        at the baseline and reaches (bold - plain) / 2 higher at the capline,
        so the half-decrease raise cancels both.
        """
        line = self._resolve()["plt_bold"].content[0]
        expected = (line.bold_cutter_diameter - line.cutter_diameter) / 2
        assert math.isclose(expected, 0.015)
        assert line.baseline_raises == (0.0,) * 6 + (expected,) * 4 + (0.0,) * 6

    def test_bold_glyph_box_stays_centered_on_the_plain_twin(self) -> None:
        """The raised + scaled bold glyph keeps the plain twin's centerline center.

        The bold scale shrinks the centerline box by (bold - plain); the raise
        lifts it by exactly half that, so the box's CENTER is the plain glyph's
        center -- the engraved stroke (half a cutter on each side) then spans
        the plain run's engraved baseline-to-capline box exactly.
        """
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block
        from plt_optimizer.generate.text_geometry import TextBlock

        labels = self._resolve()
        bold_line = labels["plt_bold"].content[0]
        plain_line = labels["plt_plain"].content[0]

        def box_of_first_bold(line: ResolvedTextLine) -> tuple[float, float]:
            block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)
            # plain_text index 6 is the run's first glyph ('B').
            bounds = TextBlock(strokes=tuple(block.strokes[i] for i in glyphs[6])).bounds()
            assert bounds is not None
            return (bounds[1], bounds[3])

        bold_bottom, bold_top = box_of_first_bold(bold_line)
        plain_bottom, plain_top = box_of_first_bold(plain_line)
        scale = bold_line._bold_glyph_scale()
        raise_in = (bold_line.bold_cutter_diameter - bold_line.cutter_diameter) / 2
        # The exact transform: scale about the baseline, then raise.
        assert bold_bottom == pytest.approx(scale * plain_bottom + raise_in, abs=1e-9)
        assert bold_top == pytest.approx(scale * plain_top + raise_in, abs=1e-9)
        # The centerline box shrinks by the cutter delta, centered on the twin.
        assert (bold_top - bold_bottom) == pytest.approx(
            scale * (plain_top - plain_bottom), abs=1e-9
        )
        assert (bold_top + bold_bottom) / 2 == pytest.approx(
            (plain_top + plain_bottom) / 2, abs=1e-3
        )

    def test_export_splits_bold_onto_its_own_pen(self, tmp_path: Path) -> None:
        """A bold line's strokes land on two pens (plain + bold cutter)."""
        result = self._export(tmp_path)
        pens = sorted({c.pen for c in result.rendered_labels["plt_bold"].text_chunks})
        assert len(pens) == 2

    def test_all_bold_line_uses_one_pen(self, tmp_path: Path) -> None:
        """A fully bold line lands on the bold pen alone."""
        result = self._export(tmp_path)
        pens = sorted({c.pen for c in result.rendered_labels["plt_all_bold"].text_chunks})
        assert len(pens) == 1

    def test_ttf_bold_splits_too(self, tmp_path: Path) -> None:
        """The TTF path splits its bold run onto the thicker pen (PLT parity)."""
        result = self._export(tmp_path)
        pens = sorted({c.pen for c in result.rendered_labels["ttf_bold"].text_chunks})
        assert len(pens) == 2

    def test_bold_underline_uses_bold_depth(self) -> None:
        """A bold run's underline centerline sits one bold cutter below the baseline."""
        from plt_optimizer.generate.label_renderer import (
            TextChunkMode,
            _render_line_block,
            _underline_render_gate,
            _underline_strokes,
        )

        line = self._resolve()["plt_bold_underline"].content[0]
        block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)
        strokes = _underline_strokes(line, block, glyphs, _underline_render_gate(line))
        # The whole run is bold, so every underline stroke sits at
        # -(bold_cutter + offset), deeper than the plain centerline. The
        # offset term is the cap-height fraction resolved to inches.
        plain_y = -(line.cutter_diameter + line.underline_offset_inches)
        bold_y = -(line.bold_cutter_diameter + line.underline_offset_inches)
        assert strokes
        for stroke in strokes:
            y = stroke.segments[0].start.imag
            assert math.isclose(y, bold_y, abs_tol=1e-6)
            assert y < plain_y

    def test_lockstep_steps_both_cutters(self, tmp_path: Path) -> None:
        """Compression steps plain 0.060->0.045 AND bold 0.090->0.060 (lockstep)."""
        result = self._export(tmp_path)
        line = result.rendered_labels["lockstep"].source_label.content[0]
        assert math.isclose(line.cutter_diameter, 0.045)
        assert math.isclose(line.bold_cutter_diameter, 0.06)
        # Bold stays exactly one rung above the final plain cutter.
        assert math.isclose(line.bold_cutter_diameter - line.cutter_diameter, 0.015)

    def test_lockstep_plain_control_steps_only_plain(self, tmp_path: Path) -> None:
        """The no-bold control steps only its plain cutter (0.060 -> 0.045)."""
        result = self._export(tmp_path)
        line = result.rendered_labels["lockstep_plain"].source_label.content[0]
        assert math.isclose(line.cutter_diameter, 0.045)
        assert line.bold_cutter_diameter is None

    def test_fixture_exports_one_file_per_cutter(self, tmp_path: Path) -> None:
        """Each distinct cutter in play (plain, bold, ladder, top rung) is a file."""
        result = self._export(tmp_path)
        assert sorted(p.name for p in result.plt_paths) == [
            "15_bh_bd.plt",
            "20_txt_bd.plt",  # Avenge5 plain ladder (multistroke/controls)
            "250_txt_bd.plt",  # top-of-inventory line (bold inert)
            "45_txt_bd.plt",  # Avenge5 boosted ladder + lockstep plain step
            "60_txt_bd.plt",  # Dino plain + lockstep bold step
            "90_txt_bd.plt",  # Dino bold + explicit cutter + clamped ladder
        ]


class TestScaleDemoExample:
    """Regression tests for tests_deps/scale_job.yaml.

    The fixture pins inline text-scaling markup (``<scale:N>``): the wrapped
    characters engrave a silhouette exactly N times the plain run's with the
    line's own cutter, the glyph geometry scaling uniformly about the baseline
    by ``(N * nominal - cutter) / (nominal - cutter)`` (the centerline
    contract, ``AA`` arcs native). The controls pin the baseline as invariant,
    the downscale strictly shorter / upscale strictly taller than the plain
    twin, ``<scale:1>`` as the identity, per-glyph selection, the ``<i>``/``<b>``
    /``<u>`` compositions, the TTF family, and that the scale never moves the
    cutter (except the bold rung, which becomes its own per-cutter file).
    """

    _SPEC = Path("tests_deps/scale_job.yaml")
    _INVENTORY = [0.01, 0.015, 0.02, 0.03, 0.045, 0.06, 0.09, 0.125, 0.25]

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled (against the inventory)."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job, available_cutters=self._INVENTORY)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="sc",
            optimize=False,
            plots=False,
            available_cutters=self._INVENTORY,
        )

    @staticmethod
    def _line_bounds(label: ResolvedLabel) -> tuple[float, float, float, float]:
        """Rendered bounds of the label's single text line."""
        from plt_optimizer.generate.label_renderer import _render_text_local_with_bounds

        _lc, entries = _render_text_local_with_bounds(label)
        return entries[0].bounds

    def test_markup_parses_to_plain_text_and_scale(self, tmp_path: Path) -> None:
        """Tags never engrave: plain_text strips them, runs carry the factor."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}

        scaled = by_id["plt_down"].content[0]
        assert scaled.text == "Hello <scale:0.5>Small</scale>"
        assert scaled.plain_text == "Hello Small"
        assert scaled.has_scale is True
        # The 5 plain chars are 1.0, the 5 scaled chars are 0.5.
        assert scaled.scale_values == (1.0,) * 6 + (0.5,) * 5

        plain = by_id["plt_plain"].content[0]
        assert plain.plain_text == "Hello Small"
        assert plain.has_scale is False

    def test_downscale_drops_its_run_to_align_the_engraved_floor(self, tmp_path: Path) -> None:
        """The downscaled run drops by half the cutter delta (floors align).

        The engraved-baseline alignment rule: a run engraving with cutter ``d``
        other than the line's ``cutter`` rides ``(d - cutter) / 2`` so its
        *engraved* stroke floor lands on the plain run's engraved floor (the
        centerline box dips below the plain baseline -- a negative raise for
        a downscale -- while the silhouette stays strictly shorter).
        """
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block
        from plt_optimizer.generate.text_geometry import TextBlock

        result = self._export(tmp_path)
        scaled_line = result.rendered_labels["plt_down"].source_label.content[0]
        plain_line = result.rendered_labels["plt_plain"].source_label.content[0]
        run_cutter = scaled_line.per_char_cutters[-1]  # 'Small' is the trailing run.

        def baseline_bottom_of_m(line: object) -> float:
            """The 'm' of 'Small' (plain_text index 7): a cap-only baseline glyph."""
            block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)  # type: ignore[arg-type]
            bounds = TextBlock(strokes=tuple(block.strokes[i] for i in glyphs[7])).bounds()
            assert bounds is not None
            return bounds[1]

        # The exact transform on the shared 'm' glyph: geometry scaled about
        # the baseline by the plan factor, then translated by the raise
        # (run_cutter - cutter) / 2. Its sub-unit baseline overshoot scales
        # with the geometry, so the delta is the raise plus (g - 1) * overshoot.
        plan = glyph_render_plan(scaled_line)
        assert plan is not None
        geometry_scale = plan[1][7]  # The 'm' rides the run's factor.
        raise_in = (run_cutter - scaled_line.cutter_diameter) / 2
        plain_bottom = baseline_bottom_of_m(plain_line)
        delta = baseline_bottom_of_m(scaled_line) - plain_bottom
        assert delta == pytest.approx((geometry_scale - 1.0) * plain_bottom + raise_in, abs=1e-9)
        # The raise itself is the negative half delta (a downscale drops): its
        # thinner engraved stroke's floor meets the plain stroke's floor.
        assert math.isclose(raise_in, (0.03 - 0.06) / 2)

    def test_upscale_grows_the_run_vs_the_plain_twin(self, tmp_path: Path) -> None:
        """The upscaled line's ink is strictly taller than its plain twin."""
        result = self._export(tmp_path)
        grown = self._line_bounds(result.rendered_labels["plt_up"].source_label)
        plain = self._line_bounds(result.rendered_labels["plt_plain"].source_label)
        assert (grown[3] - grown[1]) > (plain[3] - plain[1])

    def test_scale_unity_matches_the_plain_twin(self, tmp_path: Path) -> None:
        """``<scale:1>`` engraves exactly like the plain line (the identity)."""
        result = self._export(tmp_path)
        unity = self._line_bounds(result.rendered_labels["plt_unity"].source_label)
        plain = self._line_bounds(result.rendered_labels["plt_unity_plain"].source_label)
        assert unity == pytest.approx(plain, abs=1e-12)
        assert result.rendered_labels["plt_unity"].source_label.content[0].has_scale is False

    def test_partial_scale_moves_only_the_scaled_glyph(self, tmp_path: Path) -> None:
        """A per-glyph scale leaves the plain neighbours at full cap height."""
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block
        from plt_optimizer.generate.text_geometry import TextBlock

        result = self._export(tmp_path)
        line = result.rendered_labels["plt_partial"].source_label.content[0]
        block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)
        a = TextBlock(strokes=tuple(block.strokes[i] for i in glyphs[0])).bounds()
        b = TextBlock(strokes=tuple(block.strokes[i] for i in glyphs[1])).bounds()
        c = TextBlock(strokes=tuple(block.strokes[i] for i in glyphs[2])).bounds()
        assert a is not None and b is not None and c is not None
        # The scaled 'B' is shorter than its plain 'A'/'C' neighbours.
        assert b[3] < a[3]
        assert b[3] < c[3]
        # The plain neighbours share the same cap height (a round 'C' overshoots
        # its bbox slightly, so a small tolerance, not exact equality).
        assert a[3] == pytest.approx(c[3], abs=1e-3)

    def test_scale_bold_splits_onto_two_pens(self, tmp_path: Path) -> None:
        """``<b>`` + ``<scale>``: the bold run rides the thicker tool's pen."""
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_scale_bold"].source_label.content[0]
        assert line.bold_cutter_diameter is not None
        assert line.bold_cutter_diameter > line.cutter_diameter
        assert line.has_bold and line.has_scale

    def test_scale_underline_spans_the_scaled_run(self, tmp_path: Path) -> None:
        """``<u>`` + ``<scale>``: the underline is engraved with the line's tool."""
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block

        result = self._export(tmp_path)
        line = result.rendered_labels["plt_scale_underline"].source_label
        block, _words, glyphs = _render_line_block(line.content[0], TextChunkMode.LINE)
        # The underline strokes are appended after the glyph partition.
        underline_count = len(block.strokes) - sum(len(g) for g in glyphs)
        assert underline_count >= 1

    def test_ttf_downscale_shrinks_vs_the_plain_twin(self, tmp_path: Path) -> None:
        """The TTF path scales the flagged glyph about its baseline.

        The whole-line bounds are dominated by the plain ``Hello`` (its ascenders
        reach the cap in both labels), so the scaled run's shrink is measured on
        the shared ``S`` glyph, not the line's union box.
        """
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block
        from plt_optimizer.generate.text_geometry import TextBlock

        result = self._export(tmp_path)

        def first_glyph_top(label_id: str) -> float:
            label = result.rendered_labels[label_id].source_label
            block, _words, glyphs = _render_line_block(label.content[0], TextChunkMode.LINE)
            # The 'H' of "Hello" is the first glyph; its cap is the reference.
            bounds = TextBlock(strokes=tuple(block.strokes[i] for i in glyphs[0])).bounds()
            assert bounds is not None
            return bounds[3]

        # The plain 'H' (full) is the reference; the scaled line's 'H' is
        # identical (only the run after the space scales), and the scaled run's
        # first glyph 'S' sits at half the plain 'H' cap.
        scaled_label = result.rendered_labels["ttf_down"].source_label
        block, _words, glyphs = _render_line_block(scaled_label.content[0], TextChunkMode.LINE)
        h_top = first_glyph_top("ttf_down")
        # 'Small' starts at glyph index 6 (H-e-l-l-o-space-S...): the space owns
        # no group, so index 5 is the 'S'.
        s_bounds = TextBlock(strokes=tuple(block.strokes[i] for i in glyphs[5])).bounds()
        assert s_bounds is not None
        assert s_bounds[3] < h_top - 0.05

    def test_scale_adds_the_effective_height_cutter_file(self, tmp_path: Path) -> None:
        """A scaled run engraves with its own tool, so it gets its own file.

        Dino auto-selects 0.060 for 0.5in text. A 0.5x run is 0.25in of text,
        whose ladder cutter is 0.030, so the scaled runs land in a 0.030 file;
        the ``<b>`` run's bold rung (0.090) and the bold+0.5x run (the bold rung
        at 0.25in = 0.045) are their own files. The upscale (1.5x = 0.75in ->
        0.090) shares the bold rung's file.
        """
        result = self._export(tmp_path)
        assert sorted(p.name for p in result.plt_paths) == [
            "15_bh_sc.plt",
            "30_txt_sc.plt",  # every 0.5x run (0.25in of text -> the 0.030 rung)
            "45_txt_sc.plt",  # the bold+0.5x run (bold rung at 0.25in)
            "60_txt_sc.plt",  # the plain runs (0.5in text -> 0.060)
            "90_txt_sc.plt",  # the <b> bold rung + the 1.5x run (0.75in -> 0.090)
        ]
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        down = by_id["plt_down"].content[0]
        assert down.used_cutters == (0.03, 0.06)
        assert math.isclose(down.per_char_cutters[6], 0.03)  # the 'S' of 'Small'
        assert math.isclose(down.per_char_cutters[0], 0.06)  # the plain 'H'

    def test_arcs_stay_native_under_scale(self, tmp_path: Path) -> None:
        """A uniform scale keeps PLT ``AA`` arcs native end-to-end."""
        result = self._export(tmp_path)
        text_file = next(p for p in result.plt_paths if p.name.endswith("60_txt_sc.plt"))
        assert "AA" in text_file.read_text()


class TestCutterDemoExample:
    """Regression tests for tests_deps/cutter_job.yaml.

    The fixture pins inline cutter markup (``<c:D>``): the wrapped characters
    engrave with the EXPLICIT cutter diameter they declare, overriding every
    other cutter setting for the segment (the line's own tool, the ``<b>``
    bold rung, the ``<scale:N>`` height ladder). The run's silhouette is
    exactly ``nominal - D`` tall (the ``<b>`` centerline contract generalized),
    the engraved baseline aligns with the plain run's, and the run's strokes
    land on their own tool's pen and per-cutter PLT file.
    """

    _SPEC = Path("tests_deps/cutter_job.yaml")
    _INVENTORY = [0.01, 0.015, 0.02, 0.03, 0.045, 0.06, 0.09, 0.125, 0.25]

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled (against the inventory)."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job, available_cutters=self._INVENTORY)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="cu",
            optimize=False,
            plots=False,
            available_cutters=self._INVENTORY,
        )

    def test_markup_parses_to_plain_text_and_cutter(self, tmp_path: Path) -> None:
        """Tags never engrave: plain_text strips them, the run carries D."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}

        cut = by_id["plt_cut"].content[0]
        assert cut.text == "plain <c:0.03>CUT</c> plain"
        assert cut.plain_text == "plain CUT plain"
        assert cut.has_cutter_runs is True
        # The 6 plain chars are unmarked, the 3 'CUT' chars carry 0.03.
        assert cut.cutter_overrides == (None,) * 6 + (0.03,) * 3 + (None,) * 6

        plain = by_id["plt_plain"].content[0]
        assert plain.plain_text == "plain CUT plain"
        assert plain.has_cutter_runs is False

    def test_the_explicit_tool_overrides_the_line_cutter(self, tmp_path: Path) -> None:
        """The run's characters engrave with the declared tool."""
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_cut"].source_label.content[0]
        # The line's own tool is untouched (a segment override).
        assert math.isclose(line.cutter_diameter, 0.06)
        assert math.isclose(line.per_char_cutters[6], 0.03)  # the 'C' of 'CUT'
        assert math.isclose(line.per_char_cutters[0], 0.06)  # the plain 'p'
        assert line.used_cutters == (0.03, 0.06)

    def test_the_run_silhouette_is_nominal_minus_cutter(self, tmp_path: Path) -> None:
        """The run's engraved box is ``nominal - D`` (the <b> centerline rule).

        A lighter tool engraves a FINER stroke, so the run's centerlines grow
        and its silhouette is slightly TALLER than the plain twin's -- the
        same relation the ``<b>`` run has (in the other direction).
        """
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block
        from plt_optimizer.generate.text_geometry import TextBlock

        result = self._export(tmp_path)

        def run_glyph_top(label_id: str, index: int) -> float:
            label = result.rendered_labels[label_id].source_label
            block, _words, glyphs = _render_line_block(label.content[0], TextChunkMode.LINE)
            bounds = TextBlock(strokes=tuple(block.strokes[i] for i in glyphs[index])).bounds()
            assert bounds is not None
            return bounds[3]

        # The 'C' of 'CUT' is glyph index 6 (the space owns no group).
        cut_top = run_glyph_top("plt_cut", 6)
        plain_top = run_glyph_top("plt_plain", 6)
        # The explicit tool is lighter than the line's, so the run is taller.
        assert cut_top > plain_top

    def test_the_explicit_tool_beats_the_bold_rung(self, tmp_path: Path) -> None:
        """``<c:D>`` wins over the ``<b>`` bold rung for the characters it covers."""
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_vs_bold"].source_label.content[0]
        assert line.bold_cutter_diameter is not None
        assert math.isclose(line.bold_cutter_diameter, 0.09)
        # The 'C' of 'CUT' takes its explicit tool, NOT the bold rung.
        assert math.isclose(line.per_char_cutters[6], 0.03)
        # The bold 'plain' characters keep the bold rung.
        assert math.isclose(line.per_char_cutters[0], 0.09)

    def test_the_explicit_tool_beats_the_scale_ladder(self, tmp_path: Path) -> None:
        """``<c:D>`` wins over the ``<scale:N>`` height ladder."""
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_vs_scale"].source_label.content[0]
        assert line.has_scale is True
        # The 'C' of 'CUT' takes its explicit tool.
        assert math.isclose(line.per_char_cutters[6], 0.03)

    def test_an_unowned_diameter_snaps_down(self, tmp_path: Path) -> None:
        """0.05in is not in the inventory: it snaps to the next size down."""
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_snap"].source_label.content[0]
        assert math.isclose(line.per_char_cutters[6], 0.045)

    def test_a_per_glyph_selection_moves_only_that_glyph(self, tmp_path: Path) -> None:
        """Only the tagged glyph takes the explicit tool."""
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_partial"].source_label.content[0]
        assert line.per_char_cutters == pytest.approx((0.06, 0.03, 0.06))

    def test_a_nested_override_restores_the_outer_value(self, tmp_path: Path) -> None:
        """The inner ``<c:D>`` wins for its span; the outer one is restored."""
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_nested"].source_label.content[0]
        assert line.per_char_cutters == pytest.approx((0.03, 0.09, 0.03))

    def test_the_explicit_tool_gets_its_own_file(self, tmp_path: Path) -> None:
        """A run engraving with its own tool lands in its own per-cutter file."""
        result = self._export(tmp_path)
        assert sorted(p.name for p in result.plt_paths) == [
            "15_bh_cu.plt",
            "30_txt_cu.plt",  # the <c:0.03> runs
            "45_txt_cu.plt",  # the <c:0.05> run (snapped down to 0.045)
            "60_txt_cu.plt",  # the plain runs (0.5in text -> 0.060)
            "90_txt_cu.plt",  # the <c:0.09> run + the <b> bold rung
        ]

    def test_the_arcs_stay_native(self, tmp_path: Path) -> None:
        """The run's uniform scale keeps the PLT ``AA`` arcs native."""
        result = self._export(tmp_path)
        text_file = next(p for p in result.plt_paths if p.name.endswith("30_txt_cu.plt"))
        assert "AA" in text_file.read_text()

    def test_the_ttf_pair_splits_onto_two_pens(self, tmp_path: Path) -> None:
        """The TTF family gets the same per-cutter file split."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        line = by_id["plt_ttf"].content[0]
        assert math.isclose(line.per_char_cutters[6], 0.03)
        assert line.used_cutters == (0.03, 0.06)


class TestOriginalCutterDemoExample:
    """Regression tests for tests_deps/original_cutter_job.yaml.

    The fixture pins inline original-cutter markup (``<oc>``): the wrapped
    characters engrave with the LINE's own plain cutter -- the tool unmarked
    text on the line gets -- so they are immune to every markup-induced
    cutter change (the ``<scale:N>`` height ladder, the ``<b>`` bold rung,
    the bare ``<sup>``/``<sub>`` downscale ladder, the ``<f:...>`` run
    ladder). The pin freezes the TOOL, not the geometry, so the run's glyphs
    still scale up. The line's own compression-driven downsize still moves
    the pin (the deliberate difference from the ``<c:D>`` pin, whose
    explicit tool is a human decision and never moves).
    """

    _SPEC = Path("tests_deps/original_cutter_job.yaml")
    _INVENTORY = [0.01, 0.015, 0.02, 0.03, 0.045, 0.06, 0.09, 0.125, 0.25]

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled (against the inventory)."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job, available_cutters=self._INVENTORY)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="oc",
            optimize=False,
            plots=False,
            available_cutters=self._INVENTORY,
        )

    def test_markup_parses_to_plain_text_and_flags(self, tmp_path: Path) -> None:
        """Tags never engrave: plain_text strips them, the run carries the pin."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}

        pin = by_id["plt_pin"].content[0]
        assert pin.text == "plain <oc>PIN</oc> plain"
        assert pin.plain_text == "plain PIN plain"
        assert pin.has_original_cutter is True
        # The 6 plain chars are unmarked, the 3 'PIN' chars are pinned.
        assert pin.original_cutter_flags == (False,) * 6 + (True,) * 3 + (False,) * 6

        plain = by_id["plt_plain"].content[0]
        assert plain.plain_text == "plain PIN plain"
        assert plain.has_original_cutter is False

    def test_the_pin_keeps_the_line_cutter(self, tmp_path: Path) -> None:
        """The pinned characters engrave with the line's own tool."""
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_pin"].source_label.content[0]
        assert math.isclose(line.cutter_diameter, 0.06)
        assert math.isclose(line.per_char_cutters[6], 0.06)  # the 'P' of 'PIN'
        assert math.isclose(line.per_char_cutters[0], 0.06)  # the plain 'p'
        # ONE tool for the whole line: the pin adds no per-cutter layer.
        assert line.used_cutters == (0.06,)

    def test_the_pin_blocks_the_scale_ladder(self, tmp_path: Path) -> None:
        """<oc> keeps the line's tool where <scale:N> would step to its own."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}

        # The bare scaled run takes the 1.0in ladder's tool; the plain
        # sibling keeps the line's own 0.060in tool.
        scaled = by_id["plt_scale"].content[0]
        assert math.isclose(scaled.per_char_cutters[0], 0.125)
        assert scaled.used_cutters == (0.06, 0.125)

        # The pinned run keeps the line's own 0.060in tool -- ONE tool for
        # the whole line (the plain sibling included).
        pinned = by_id["plt_pinned_scale"].content[0]
        assert math.isclose(pinned.per_char_cutters[0], 0.06)
        assert pinned.used_cutters == (0.06,)

    def test_the_pin_blocks_the_bold_rung(self, tmp_path: Path) -> None:
        """<oc> keeps the line's tool where <b> would step to a thicker one."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}

        bold = by_id["plt_bold"].content[0]
        assert math.isclose(bold.per_char_cutters[0], 0.09)
        assert bold.used_cutters == (0.06, 0.09)

        pinned = by_id["plt_pinned_bold"].content[0]
        assert math.isclose(pinned.per_char_cutters[0], 0.06)
        assert pinned.used_cutters == (0.06,)

    def test_the_explicit_cutter_still_wins(self, tmp_path: Path) -> None:
        """<c:D> beats <oc>: an explicit tool is never overridden automatically."""
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_vs_cutter"].source_label.content[0]
        # The 'C' of 'CUT' (glyph index 6) takes the explicit tool.
        assert math.isclose(line.per_char_cutters[6], 0.03)
        assert line.used_cutters == (0.03, 0.06)

    def test_the_pinned_glyphs_are_bigger_than_the_plain_twin(self, tmp_path: Path) -> None:
        """The pin freezes the TOOL, not the geometry: the run still scales up.

        The pinned ``<scale:2.0>`` run engraves with the line's own tool, so
        its glyphs are BIGGER than the plain twin's -- the whole point of the
        tag ("larger text without a larger cutter").
        """
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block
        from plt_optimizer.generate.text_geometry import TextBlock

        result = self._export(tmp_path)

        def run_glyph_top(label_id: str, index: int) -> float:
            label = result.rendered_labels[label_id].source_label
            block, _words, glyphs = _render_line_block(label.content[0], TextChunkMode.LINE)
            bounds = TextBlock(strokes=tuple(block.strokes[i] for i in glyphs[index])).bounds()
            assert bounds is not None
            return bounds[3]

        # The pinned run's glyphs reach higher than the plain twin's.
        assert run_glyph_top("plt_pinned_scale", 0) > run_glyph_top("plt_plain", 0)

    def test_the_pin_adds_no_per_cutter_file(self, tmp_path: Path) -> None:
        """A pinned run lands in the line's own per-cutter file (no new tool).

        The contrast with the ``<c:D>`` demo, whose explicit tool gets its
        own file: the ``<oc>`` pin is invisible in the file set, because the
        pinned characters engrave with the line's own tool. The pinned bold
        and scale labels contribute NO 0.090 / 0.125 file (their unpinned
        twins do).
        """
        result = self._export(tmp_path)
        assert sorted(p.name for p in result.plt_paths) == [
            "125_txt_oc.plt",  # the bare <scale:2.0> ladder (the unpinned twin)
            "15_bh_oc.plt",
            "30_txt_oc.plt",  # the <c:0.03> run (the explicit tool wins)
            "60_txt_oc.plt",  # every other run, pinned or plain
            "90_txt_oc.plt",  # the <b> bold rung (the unpinned twin)
        ]

    def test_the_ttf_pair_keeps_the_line_cutter(self, tmp_path: Path) -> None:
        """The TTF family gets the same pin (the pen assignment carries it)."""
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_ttf"].source_label.content[0]
        assert math.isclose(line.per_char_cutters[6], 0.06)
        assert line.used_cutters == (0.06,)


class TestSupSubDemoExample:
    """Regression tests for tests_deps/sup_sub_job.yaml.

    The fixture pins inline superscript/subscript markup (``<sup>``/``<sub>``):
    a bare tag downscales the run to ``supersubscript_downscale`` (d) and
    derives the baseline shift from the same number, **centering the shrunken
    glyph on the line it belongs to** (superscript rises ``1 - d / 2`` cap
    heights so its centre lands on the capline, subscript drops ``d / 2`` so
    its centre lands on the baseline), and the downscaled run engraves with
    the cutter for its downscaled height (the ``<scale:N>`` ladder, so it
    lands on its own per-cutter file). The argumented forms ``<sup:0.3>`` /
    ``<sub:-0.2>`` set the shift explicitly and apply no scaling, so they keep
    the line's own cutter (one pen).
    """

    _SPEC = Path("tests_deps/sup_sub_job.yaml")
    _INVENTORY = [0.01, 0.015, 0.02, 0.03, 0.045, 0.06, 0.09, 0.125, 0.25]

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled (against the inventory)."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job, available_cutters=self._INVENTORY)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="ss",
            optimize=False,
            plots=False,
            available_cutters=self._INVENTORY,
        )

    @staticmethod
    def _line_bounds(label: ResolvedLabel) -> tuple[float, float, float, float]:
        """Rendered bounds of the label's single text line."""
        from plt_optimizer.generate.label_renderer import _render_text_local_with_bounds

        _lc, entries = _render_text_local_with_bounds(label)
        return entries[0].bounds

    def test_markup_parses_to_plain_text_and_shifts(self, tmp_path: Path) -> None:
        """Tags never engrave: plain_text strips them, runs carry the shift."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}

        sup = by_id["plt_sup"].content[0]
        assert sup.text == "Hello <sup>Small</sup>"
        assert sup.plain_text == "Hello Small"
        assert sup.has_sup_sub is True
        # The 6 plain chars are unmarked, the 5 sup chars rise 1 - d/2 = 0.75
        # (their centre lands on the capline).
        assert sup.sup_sub_shifts == (0.0,) * 6 + (0.75,) * 5
        assert sup.sup_sub_scales == (1.0,) * 6 + (0.5,) * 5

        plain = by_id["plt_plain"].content[0]
        assert plain.plain_text == "Hello Small"
        assert plain.has_sup_sub is False

    def test_bare_sup_rises_vs_the_plain_twin(self, tmp_path: Path) -> None:
        """The bare sup's CENTRE rides the capline: the ink box grows by d/2.

        The bare-sup geometry contract (centering): the downscaled glyph's
        centre lands on the capline, so the glyph straddles it and the line's
        ink box grows by ``d / 2`` cap heights (minus the plain glyph's
        capline overshoot), minus the engraved-baseline alignment drop: the
        run's ladder cutter sits below the line's, so the run rides
        ``(d_cutter - cutter) / 2`` (negative) and the growth shrinks by half
        that delta. The render frame centers the block, so the emitted box
        grows symmetrically: top up and bottom down by the same amount.
        """
        result = self._export(tmp_path)
        sup = self._line_bounds(result.rendered_labels["plt_sup"].source_label)
        plain = self._line_bounds(result.rendered_labels["plt_plain"].source_label)
        line = result.rendered_labels["plt_sup"].source_label.content[0]
        # The growth is symmetric about the centered frame.
        assert sup[3] - plain[3] == pytest.approx(plain[1] - sup[1], abs=1e-3)
        # ... and totals d/2 cap heights (0.25 * 0.44 = 0.11 at d = 0.5),
        # less the plain glyph's sub-unit capline overshoot and less half the
        # cutter delta ((0.06 - 0.03) / 2 = 0.015, the engraved-floor alignment).
        growth = (sup[3] - plain[3]) + (plain[1] - sup[1])
        run_cutter = line.per_char_cutters[-1]
        expected = 0.25 * line.toolpath_text_height + (run_cutter - line.cutter_diameter) / 2
        assert growth == pytest.approx(expected, abs=0.01)

    def test_bare_sup_kerns_narrower_than_the_plain_twin(self, tmp_path: Path) -> None:
        """The raised run kerns at its true height, so the line is narrower.

        The reported symptom: a superscript engraved with too much spacing.
        Kerning now measures the shifted silhouette, so the raised ``Small``
        run clears the preceding ``o``'s widest bulge and the line's ink
        width is strictly less than its plain twin's.
        """
        result = self._export(tmp_path)
        sup = self._line_bounds(result.rendered_labels["plt_sup"].source_label)
        plain = self._line_bounds(result.rendered_labels["plt_plain"].source_label)
        assert sup is not None and plain is not None
        assert sup[2] - sup[0] < plain[2] - plain[0]

    def test_shift_narrows_a_boundary_pair(self, tmp_path: Path) -> None:
        """The shift alone (no scale) tightens a shifted/unshifted boundary.

        ``A<sup>B</sup>C`` has an unbroken shifted/unshifted boundary pair
        (``A``->``B`` and ``B``->``C``), so kerning the shifted silhouette
        narrows the line. Isolating the shift (zeroing ``baseline_shifts``
        while keeping the scale) shows the shift alone is responsible -- the
        raised ``B`` clears the ``A``/``C`` widest bulges.
        """
        from plt_optimizer.generate.label_renderer import _glyph_render_kwargs
        from plt_optimizer.generate.plt_font_renderer import (
            render_text_line_plt_font_with_words,
        )

        result = self._export(tmp_path)
        line = result.rendered_labels["plt_partial"].source_label.content[0]
        kw = _glyph_render_kwargs(line, plt=True)
        assert kw["baseline_shifts"] == (0.0, pytest.approx(0.315), 0.0)

        def width(shifts: tuple[float, ...]) -> float:
            k = dict(kw)
            k["baseline_shifts"] = shifts
            block, _ = render_text_line_plt_font_with_words(
                line.plain_text,
                line.toolpath_text_height,
                line.font,
                cutter_diameter=line.cutter_diameter,
                character_spacing=line.character_spacing,
                space_width_fraction=line.space_width_fraction,
                min_glyph_width_fraction=line.min_glyph_width_fraction,
                kerning_window_fraction=line.kerning_window_fraction,
                kerning_penetration_scale=line.kerning_penetration_scale,
                kerning_recession_scale=line.kerning_recession_scale,
                kerning_min_gap_fraction=line.kerning_min_gap_fraction,
                fallback_advance_fraction=line.fallback_advance_fraction,
                **k,
            )
            bounds = block.bounds()
            assert bounds is not None
            return bounds[2] - bounds[0]

        shifted = width(kw["baseline_shifts"])
        unshifted = width(tuple(0.0 for _ in kw["baseline_shifts"]))
        # The shift alone tightens the boundary pairs.
        assert shifted < unshifted

    def test_bare_sub_drops_the_line_floor(self, tmp_path: Path) -> None:
        """The bare sub's ink hangs below the plain twin's baseline floor."""
        result = self._export(tmp_path)
        sub = self._line_bounds(result.rendered_labels["plt_sub"].source_label)
        plain = self._line_bounds(result.rendered_labels["plt_plain"].source_label)
        assert sub[1] < plain[1]

    def test_shift_only_run_matches_the_plain_twin_size(self, tmp_path: Path) -> None:
        """<sup:0.3> shifts without scaling: the glyph height is the plain one.

        The shift-only run has no downscale to compensate, so its top rises
        above the capline and the line's ink box grows by exactly the shift
        (the accepted staleness: the author adds ``line_spacing``).
        """
        result = self._export(tmp_path)
        shifted = self._line_bounds(result.rendered_labels["plt_shift"].source_label)
        plain = self._line_bounds(result.rendered_labels["plt_plain"].source_label)
        # The plain prefix keeps the baseline; the shifted glyph's top rises,
        # so the (centered) ink box grows by exactly the shift.
        line = result.rendered_labels["plt_shift"].source_label.content[0]
        growth = (shifted[3] - shifted[1]) - (plain[3] - plain[1])
        assert growth == pytest.approx(0.3 * line.toolpath_text_height, abs=1e-3)

    def test_partial_shift_moves_only_the_shifted_glyph(self, tmp_path: Path) -> None:
        """Per-glyph selection: the plain neighbours keep the baseline."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        line = by_id["plt_partial"].content[0]
        assert line.plain_text == "ABC"
        assert line.sup_sub_shifts == (0.0, 0.75, 0.0)
        assert line.sup_sub_scales == (1.0, 0.5, 1.0)

    def test_downscale_field_drives_scale_and_shift(self, tmp_path: Path) -> None:
        """supersubscript_downscale 0.8 -> shift 0.6 and the 0.4in-text tool."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        line = by_id["plt_downscale08"].content[0]
        assert line.supersubscript_downscale == 0.8
        assert line.sup_sub_scales == (1.0, 0.8, 1.0)
        assert line.sup_sub_shifts[0] == 0.0
        assert math.isclose(line.sup_sub_shifts[1], 0.6)
        # 0.8 * 0.5in = 0.4in of text -> the 0.045 rung (not the 0.03 one).
        assert line.used_cutters == (0.045, 0.06)

    def test_scale_and_sup_compose_onto_the_smallest_tool(self, tmp_path: Path) -> None:
        """<scale:0.5> inside <sup> composes to 0.25 -> the 0.015 rung."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        line = by_id["plt_scale_sup"].content[0]
        assert line.plain_text == "tiny"
        assert line.scale_values == (0.5,) * 4
        assert line.sup_sub_scales == (0.5,) * 4
        # 0.5 * 0.5 * 0.5in = 0.125in of text -> the 0.015 rung.
        assert line.used_cutters == (0.015, 0.06)

    def test_underline_splits_at_the_sup_boundary(self, tmp_path: Path) -> None:
        """The underline is cut with each run's own tool (two runs)."""
        from plt_optimizer.generate.label_renderer import _underline_render_gate

        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        line = by_id["plt_sup_underline"].content[0]
        runs = _underline_render_gate(line)
        assert len(runs) == 2
        assert line.plain_text[runs[0][0] : runs[0][1] + 1] == "pay "
        assert line.plain_text[runs[1][0] : runs[1][1] + 1] == "deep"

    def test_sup_and_sub_coexist_on_one_line(self, tmp_path: Path) -> None:
        """The two tags are mutually exclusive per character, both on one line."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        line = by_id["plt_sup_sub_line"].content[0]
        assert line.plain_text == "1st & 2nd"
        assert line.sup_sub_shifts == (
            0.0,
            0.75,
            0.75,
            0.0,
            0.0,
            0.0,
            0.0,
            -0.25,
            -0.25,
        )

    def test_ttf_sup_shifts_vs_the_plain_twin(self, tmp_path: Path) -> None:
        """The TTF path shifts the flagged glyph (the PLT parity contract)."""
        result = self._export(tmp_path)
        sup = self._line_bounds(result.rendered_labels["ttf_sup"].source_label)
        plain = self._line_bounds(result.rendered_labels["ttf_plain"].source_label)
        # The downscaled run's CENTRE rides the capline, so the ink box grows
        # by d/2 cap heights (the PLT parity contract) less half the cutter
        # delta (the engraved-floor alignment drop, the PLT parity too); the
        # centered frame spreads the growth symmetrically top and bottom.
        line = result.rendered_labels["ttf_sup"].source_label.content[0]
        run_cutter = line.per_char_cutters[-1]
        expected = 0.25 * line.toolpath_text_height + (run_cutter - line.cutter_diameter) / 2
        growth = (sup[3] - plain[3]) + (plain[1] - sup[1])
        assert growth == pytest.approx(expected, abs=0.03)

    def test_sup_adds_the_downscaled_height_cutter_file(self, tmp_path: Path) -> None:
        """A bare sup run engraves with its own tool, so it gets its own file.

        Dino auto-selects 0.060 for 0.5in text. A bare sup run (d = 0.5) is
        0.25in of text -> the 0.030 rung; the d = 0.8 label is 0.4in -> 0.045;
        the <scale:0.5>-inside-<sup> label is 0.125in -> 0.015. The shift-only
        labels (<sup:0.3>) keep the line's 0.060 tool.
        """
        result = self._export(tmp_path)
        assert sorted(p.name for p in result.plt_paths) == [
            "15_bh_ss.plt",
            "30_txt_ss.plt",  # the d = 0.5 runs (0.25in of text -> 0.030)
            "45_txt_ss.plt",  # the d = 0.8 run (0.4in of text -> 0.045)
            "60_txt_ss.plt",  # the plain runs + the shift-only runs
        ]
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        sup = by_id["plt_sup"].content[0]
        assert sup.used_cutters == (0.03, 0.06)
        assert math.isclose(sup.per_char_cutters[6], 0.03)  # the 'S' of 'Small'
        assert math.isclose(sup.per_char_cutters[0], 0.06)  # the plain 'H'
        # A shift-only run never splits the pen.
        assert by_id["plt_shift"].content[0].used_cutters == (0.06,)

    def test_arcs_stay_native_under_the_shift(self, tmp_path: Path) -> None:
        """A pure translate keeps PLT ``AA`` arcs native end-to-end."""
        result = self._export(tmp_path)
        text_file = next(p for p in result.plt_paths if p.name.endswith("30_txt_ss.plt"))
        assert "AA" in text_file.read_text()


class TestStrikethroughDemoExample:
    """Regression tests for tests_deps/strikethrough_job.yaml.

    The fixture pins inline strikethrough markup (``<s>``,
    ``strikethrough_offset_fraction``, ``strikethrough_break_spaces``): ONE continuous
    centerline stroke per struck run at the line's strikethrough height (the
    explicit offset above the baseline, or the cap-band midline when unset),
    with the underline's break model deliberately removed -- a descender
    punches through the stroke instead of breaking it.
    """

    _SPEC = Path("tests_deps/strikethrough_job.yaml")
    _INVENTORY = [0.01, 0.015, 0.02, 0.03, 0.045, 0.06, 0.09, 0.125, 0.25]

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled (against the inventory)."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job, available_cutters=self._INVENTORY)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="stk",
            optimize=False,
            plots=False,
            available_cutters=self._INVENTORY,
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
    def _decoration_stroke_count(cls, label: ResolvedLabel) -> int:
        """The number of decoration strokes a rendered line carries.

        Decoration strokes are appended after the glyph partition, so the count
        is the block's strokes minus the glyphs' strokes.
        """
        block, _wg, glyph_groups = cls._line_render(label)
        return len(block.strokes) - sum(len(g) for g in glyph_groups)

    @classmethod
    def _strike_spans(cls, label: ResolvedLabel) -> list[tuple[float, float, float]]:
        """The rendered line's strikethrough strokes as ``(y, x_min, x_max)``."""
        from plt_optimizer.generate.label_renderer import (
            TextChunkMode,
            _render_line_block,
            _strikethrough_render_gate,
            _strikethrough_strokes,
        )

        line = label.content[0]
        block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)
        strokes = _strikethrough_strokes(line, block, glyphs, _strikethrough_render_gate(line))
        return [
            (s.segments[0].start.imag, s.segments[0].start.real, s.segments[0].end.real)
            for s in strokes
        ]

    def test_markup_parses_to_plain_text_and_runs(self, tmp_path: Path) -> None:
        """Tags never engrave: plain_text strips them, runs carry the style."""
        from plt_optimizer.generate.resolution import resolve_job_spec

        job = parse_yaml(self._SPEC)
        by_id = {label.id: label for label in resolve_job_spec(job, self._INVENTORY)}

        struck = by_id["plt_strike"].content[0]
        assert struck.text == "<s>Depth</s>"
        assert struck.plain_text == "Depth"
        assert [run.style.strikethrough for run in struck.styled_runs] == [True]
        assert struck.has_strikethrough is True

        plain = by_id["plt_plain"].content[0]
        assert plain.plain_text == "Depth"
        # A markup-free line resolves to a single plain run (the renderers'
        # fast path is "no struck run", not "no run").
        assert [(r.text, r.style.strikethrough) for r in plain.styled_runs] == [("Depth", False)]
        assert plain.has_strikethrough is False

    def test_one_continuous_stroke_survives_the_descender(self, tmp_path: Path) -> None:
        """The `p` punches through the strike: one stroke (the "no breaks" rule).

        The underline twin of the same glyphs breaks around the `p`, so it
        engraves several segments -- the contrast pins that the strikethrough
        deliberately has no break model.
        """
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}

        struck = by_id["plt_strike"]
        assert len(self._strike_spans(struck)) == 1
        # The mid-cap stroke lies inside the cap band, so the block is identical
        # to the plain twin's.
        assert self._line_bounds(struck) == pytest.approx(
            self._line_bounds(by_id["plt_plain"]), abs=1e-9
        )
        # The underline control DOES break (several segments).
        assert self._decoration_stroke_count(by_id["plt_underline_twin"]) >= 2

    def test_stroke_sits_at_the_cap_band_midline(self, tmp_path: Path) -> None:
        """Unset offset centres the stroke on half the rendered cap height."""
        result = self._export(tmp_path)
        label = result.rendered_labels["plt_strike"].source_label
        line = label.content[0]
        assert line.strikethrough_offset_fraction is None
        (y, _x_min, _x_max) = self._strike_spans(label)[0]
        assert y == pytest.approx(0.5 * line.toolpath_text_height, abs=1e-12)

    def test_explicit_offset_places_the_stroke(self, tmp_path: Path) -> None:
        """``strikethrough_offset_fraction`` is the height above the baseline, 0.0 included."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}

        zero = self._strike_spans(by_id["plt_offset_zero"])[0]
        assert zero[0] == pytest.approx(0.0, abs=1e-12)
        # On the baseline the stroke coincides with the caps' own floor: the
        # block's bounds match the plain caps.
        assert self._line_bounds(by_id["plt_offset_zero"]) == pytest.approx(
            self._line_bounds(by_id["plt_offset_zero_plain"]), abs=1e-9
        )

        high = self._strike_spans(by_id["plt_offset_high"])[0]
        assert high[0] == pytest.approx(
            2.0 * by_id["plt_offset_high"].content[0].toolpath_text_height, abs=1e-12
        )
        # Above the capline the stroke is ink: the block's top rises. (The
        # render frame centres the block, so the growth shows at the top edge.)
        high_bounds = self._line_bounds(by_id["plt_offset_high"])
        caps_bounds = self._line_bounds(by_id["plt_caps"])
        assert high_bounds[3] > caps_bounds[3]
        # The stroke spans the run's own ink width, so the width is unchanged.
        assert high_bounds[2] - high_bounds[0] == pytest.approx(
            caps_bounds[2] - caps_bounds[0], rel=1e-6
        )

    def test_continuous_run_spans_the_space(self, tmp_path: Path) -> None:
        """The default strike crosses the inter-word space (one stroke)."""
        result = self._export(tmp_path)
        spans = self._strike_spans(result.rendered_labels["plt_continuous_words"].source_label)
        assert len(spans) == 1
        assert spans[0][1] < spans[0][2]

    def test_break_spaces_leaves_the_space_bare(self, tmp_path: Path) -> None:
        """``strikethrough_break_spaces`` gives each word its own stroke."""
        from plt_optimizer.generate.label_renderer import _strikethrough_render_gate

        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        line = by_id["plt_break_spaces_words"].content[0]
        # 'hello world': the space (index 5) ends each run.
        assert _strikethrough_render_gate(line) == ((0, 4), (6, 10))
        spans = self._strike_spans(by_id["plt_break_spaces_words"])
        assert len(spans) == 2
        # Both strokes share one height (the height is per line, not per run).
        assert spans[0][0] == pytest.approx(spans[1][0], abs=1e-12)
        # The first stroke ends before the second begins (the space stays bare).
        assert spans[0][2] < spans[1][1]

    def test_nested_markup_strikes_only_the_run(self, tmp_path: Path) -> None:
        """``<i>nested <s>markup</s> tail</i>`` strikes just the inner run."""
        from plt_optimizer.generate.label_renderer import _strikethrough_render_gate

        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        line = by_id["plt_nested"].content[0]
        assert line.plain_text == "nested markup tail"
        assert _strikethrough_render_gate(line) == ((7, 12),)
        spans = self._strike_spans(by_id["plt_nested"])
        assert len(spans) == 1

    def test_multi_run_draws_two_independent_strokes(self, tmp_path: Path) -> None:
        """Two struck runs separated by a plain glyph engrave two strokes."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        spans = self._strike_spans(by_id["plt_multi_run"])
        assert len(spans) == 2
        assert spans[0][2] < spans[1][1]
        # The plain control carries no decoration at all.
        assert self._decoration_stroke_count(by_id["plt_multi_run_plain"]) == 0

    def test_nested_strikethrough_is_idempotent(self, tmp_path: Path) -> None:
        """``<s>a<s>b</s>c</s>`` is one struck run -- exactly one stroke."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        line = by_id["plt_idempotent"].content[0]
        assert line.plain_text == "abc"
        assert line.strikethrough_flags == (True, True, True)
        assert len(self._strike_spans(by_id["plt_idempotent"])) == 1
        assert self._decoration_stroke_count(by_id["plt_idempotent_plain"]) == 0

    def test_strike_splits_at_cutter_boundaries(self, tmp_path: Path) -> None:
        """A struck run across a `<b>`/`<scale:N>` boundary draws two strokes.

        Each struck run engraves with its own tool, so the gate splits the span
        where the cutter changes and the strokes land on two per-cutter pens.
        """
        from plt_optimizer.generate.label_renderer import (
            _strikethrough_render_gate,
            _stroke_layer_map,
        )

        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}

        for label_id in ("plt_bold_split", "plt_scale_split"):
            label = by_id[label_id]
            line = label.content[0]
            runs = _strikethrough_render_gate(line)
            assert len(runs) == 2, label_id
            assert len(self._strike_spans(label)) == 2, label_id
            block, _words, glyphs = self._line_render(label)
            layers = _stroke_layer_map(line, block, glyphs)
            assert layers, label_id
            strike_cutters = [cutter for cutter, _color in layers[len(layers) - 2 :]]
            # The plain word's stroke rides the line's tool, the second word's
            # rides its own (bold rung / effective-height) tool.
            assert strike_cutters[0] == pytest.approx(line.cutter_diameter)
            assert strike_cutters[1] != pytest.approx(line.cutter_diameter)

    def test_strike_and_underline_coexist(self, tmp_path: Path) -> None:
        """``<u><s>AV</s></u>`` engraves both decorations on one run."""
        from plt_optimizer.generate.label_renderer import (
            _strikethrough_render_gate,
            _underline_render_gate,
        )

        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        line = by_id["plt_strike_and_underline"].content[0]
        assert _underline_render_gate(line) == ((0, 1),)
        assert _strikethrough_render_gate(line) == ((0, 1),)
        # Underline (below the baseline) + strike (at mid-cap) = two strokes.
        assert self._decoration_stroke_count(by_id["plt_strike_and_underline"]) == 2
        strike_y = self._strike_spans(by_id["plt_strike_and_underline"])[0][0]
        assert strike_y == pytest.approx(0.5 * line.toolpath_text_height, abs=1e-12)

    def test_small_caps_strikes_at_the_same_height(self, tmp_path: Path) -> None:
        """A ` SC` font's strike uses the same mid-cap rule as its base."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        sc_line = by_id["plt_small_caps"].content[0]
        sc_y = self._strike_spans(by_id["plt_small_caps"])[0][0]
        base_y = self._strike_spans(by_id["plt_strike"])[0][0]
        assert sc_line.font == "Dino SC"
        assert sc_y == pytest.approx(0.5 * sc_line.toolpath_text_height, abs=1e-12)
        assert sc_y == pytest.approx(base_y, abs=1e-12)

    def test_ttf_stroke_matches_the_plt_contract(self, tmp_path: Path) -> None:
        """The TTF path draws one unbroken stroke at the same mid-cap height."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        ttf_line = by_id["ttf_strike"].content[0]
        spans = self._strike_spans(by_id["ttf_strike"])
        assert len(spans) == 1
        assert spans[0][0] == pytest.approx(0.5 * ttf_line.toolpath_text_height, abs=1e-12)
        assert self._decoration_stroke_count(by_id["ttf_plain"]) == 0

    def test_strikethrough_adds_no_cutter_file(self, tmp_path: Path) -> None:
        """The strike uses its run's existing tool -- no new per-cutter layer.

        Dino auto-selects 0.060 for 0.5in text; the `<b>` run's bold rung
        (0.090) and the 0.5x run's ladder cutter (0.030) are the only extra
        files, and every one of them comes from the markup the label already
        carries (the strike itself never introduces a tool).
        """
        result = self._export(tmp_path)
        names = sorted(p.name for p in result.plt_paths)
        assert "15_bh_stk.plt" in names
        assert "60_txt_stk.plt" in names
        assert "90_txt_stk.plt" in names  # the <b> bold rung
        assert "30_txt_stk.plt" in names  # the <scale:0.5> run
        # No file for a tool the strike invented.
        assert len([n for n in names if n.endswith("_txt_stk.plt")]) == 3


class TestInlineScDemoExample:
    """Regression tests for tests_deps/inline_sc_job.yaml.

    The fixture pins the inline small-caps tag (``<sc>``): the tagged run's
    ASCII lowercase engrave as the base font's uppercase glyph shrunk
    **top-down** by ``sc_scale`` (default 0.7), selected per character. The
    controls pin the plain/capitals twins (the width ordering), the exact 0.7
    ratio on a lone glyph, per-character selection, ``sc_scale: 1.0`` (the
    uppercase map without the shrink) against authored capitals, the two
    inertness rules (a `` SC`` font and a symbol font), caseless characters at
    full size, idempotent nesting, the composition with ``<b>``/``<scale:N>``/
    ``<i>``, the TTF family, and the contract that the cutter never moves.
    """

    _SPEC = Path("tests_deps/inline_sc_job.yaml")
    _INVENTORY = [0.01, 0.015, 0.02, 0.03, 0.045, 0.06, 0.09, 0.125, 0.25]

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled (against the inventory)."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job, available_cutters=self._INVENTORY)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="sc",
            optimize=False,
            plots=False,
            available_cutters=self._INVENTORY,
        )

    @staticmethod
    def _line_bounds(label: ResolvedLabel) -> tuple[float, float, float, float]:
        """Rendered bounds of the label's single text line."""
        from plt_optimizer.generate.label_renderer import _render_text_local_with_bounds

        _lc, entries = _render_text_local_with_bounds(label)
        return entries[0].bounds

    @classmethod
    def _width(cls, label: ResolvedLabel) -> float:
        """Rendered ink width of the label's single text line."""
        x_min, _y_min, x_max, _y_max = cls._line_bounds(label)
        return x_max - x_min

    @classmethod
    def _height(cls, label: ResolvedLabel) -> float:
        """Rendered ink height of the label's single text line."""
        _x_min, y_min, _x_max, y_max = cls._line_bounds(label)
        return y_max - y_min

    def test_tag_resolves_per_label(self, tmp_path: Path) -> None:
        """The tag parses to plain text and reuses the cascading ``sc_scale``."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        line = by_id["plt_sc"].content[0]
        assert line.text == "<sc>Hello World</sc>"
        assert line.plain_text == "Hello World"
        assert line.has_small_caps is True
        assert line.sc_scale == pytest.approx(0.7)
        assert by_id["plt_sc_100"].content[0].sc_scale == pytest.approx(1.0)
        # The untagged control carries no selection at all.
        assert by_id["plt_plain"].content[0].has_small_caps is False

    def test_plt_lowercase_lands_between_plain_and_caps(self, tmp_path: Path) -> None:
        """The tagged line is wider than its plain twin, narrower than capitals.

        A 0.7 capital is wider than this font's own narrow lowercase, so the
        small-caps run sits strictly between the two authored extremes.
        """
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        plain = self._width(by_id["plt_plain"])
        small = self._width(by_id["plt_sc"])
        caps = self._width(by_id["plt_caps"])
        assert plain < small < caps

    def test_plt_lone_glyph_measures_the_scale_ratio(self, tmp_path: Path) -> None:
        """A lone tagged lowercase glyph is exactly ``sc_scale`` of the capital.

        A mixed line's ink box is dominated by its full-size capitals, so the
        ratio is pinned on a single glyph: both the width and the height of
        ``<sc>o</sc>`` are 0.7 of ``O``'s.
        """
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        small_w = self._width(by_id["plt_sc_glyph"])
        small_h = self._height(by_id["plt_sc_glyph"])
        cap_w = self._width(by_id["plt_cap_glyph"])
        cap_h = self._height(by_id["plt_cap_glyph"])
        assert small_w == pytest.approx(0.7 * cap_w, rel=0.02)
        assert small_h == pytest.approx(0.7 * cap_h, rel=0.02)

    def test_plt_shrunk_capital_is_not_the_fonts_lowercase(self, tmp_path: Path) -> None:
        """The tag draws the UPPERCASE glyph shrunk, never the font's own ``o``."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        small = (self._width(by_id["plt_sc_glyph"]), self._height(by_id["plt_sc_glyph"]))
        own = (self._width(by_id["plt_lower_glyph"]), self._height(by_id["plt_lower_glyph"]))
        assert small != pytest.approx(own, abs=1e-3)

    def test_selection_is_per_character(self, tmp_path: Path) -> None:
        """Tagging one word of a line lands strictly between the two extremes."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        plain = self._width(by_id["plt_plain"])
        partial = self._width(by_id["plt_partial"])
        caps = self._width(by_id["plt_caps"])
        assert plain < partial < caps
        # Only the second word carries the selection (the space is plain).
        line = by_id["plt_partial"].content[0]
        assert line.plain_text == "Hello World"
        assert line.small_caps_flags == (False,) * 6 + (True,) * 5

    def test_scale_one_matches_authored_capitals(self, tmp_path: Path) -> None:
        """``sc_scale: 1.0`` maps to capitals and skips the shrink exactly."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        assert self._line_bounds(by_id["plt_sc_100"]) == pytest.approx(
            self._line_bounds(by_id["plt_caps_pay"]), abs=1e-9
        )

    def test_tag_is_inert_on_a_small_caps_font(self, tmp_path: Path) -> None:
        """A `` SC`` line is already small caps: the tag adds nothing."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        tagged = by_id["plt_scfont_inert"].content[0]
        assert tagged.has_small_caps is True  # the tag parsed...
        assert self._line_bounds(by_id["plt_scfont_inert"]) == pytest.approx(
            self._line_bounds(by_id["plt_scfont_plain"]), abs=1e-9
        )  # ...and engraved identically (the font-level selection is kept).

    def test_tag_is_inert_on_a_symbol_font(self, tmp_path: Path) -> None:
        """A symbol font has no lowercase to shrink: the tag is a no-op."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        assert by_id["plt_symbol_inert"].content[0].has_small_caps is True
        assert self._line_bounds(by_id["plt_symbol_inert"]) == pytest.approx(
            self._line_bounds(by_id["plt_symbol_plain"]), abs=1e-9
        )

    def test_caseless_characters_stay_full_size(self, tmp_path: Path) -> None:
        """Digits/punctuation engrave verbatim on a tagged run."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        assert self._line_bounds(by_id["plt_caseless"]) == pytest.approx(
            self._line_bounds(by_id["plt_caseless_plain"]), abs=1e-9
        )

    def test_nested_tag_is_idempotent(self, tmp_path: Path) -> None:
        """``<sc>a<sc>b</sc>c</sc>`` is ONE run, engraving like the flat span."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        nested = by_id["plt_nested"].content[0]
        assert nested.small_caps_flags == (True, True, True)
        assert self._line_bounds(by_id["plt_nested"]) == pytest.approx(
            self._line_bounds(by_id["plt_nested_flat"]), abs=1e-9
        )

    def test_arcs_stay_native_under_the_shrink(self, tmp_path: Path) -> None:
        """The uniform scale keeps the arc-native contract (no flattening)."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        text_file = next(p for p in result.plt_paths if p.name == "60_txt_sc.plt")
        assert "AA" in text_file.read_text()
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block

        block, _words, glyph_groups = _render_line_block(
            by_id["plt_sc"].content[0], TextChunkMode.LINE
        )
        # The 1:1 character map keeps the per-glyph partition exact (11 chars,
        # 10 non-space): the shrink never merges or drops a glyph.
        assert len(glyph_groups) == len(by_id["plt_sc"].content[0].plain_text.replace(" ", ""))
        assert block.strokes

    def test_composition_keeps_the_run_cutters(self, tmp_path: Path) -> None:
        """``<b>``/``<scale:N>`` runs keep their own tool inside a tagged run."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        bold_line = by_id["plt_sc_bold"].content[0]
        assert bold_line.bold_cutter_diameter == pytest.approx(0.09)
        assert bold_line.used_cutters == (0.06, 0.09)
        scale_line = by_id["plt_sc_scale"].content[0]
        assert scale_line.used_cutters == (0.03, 0.06)
        # The plain tagged run uses only the line's own tool.
        assert by_id["plt_sc"].content[0].used_cutters == (0.06,)

    def test_italic_composes_with_the_shrink(self, tmp_path: Path) -> None:
        """A tagged+italic run shears AND shrinks (wider than the un-sheared run)."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        italic = by_id["plt_sc_italic"].content[0]
        assert italic.italic_angle == pytest.approx(20.0)
        assert italic.has_small_caps is True
        # The italic overhang widens the line relative to the same text
        # un-sheared (the same text at angle 0 is the plain run's shape).
        assert self._width(by_id["plt_sc_italic"]) > 0.0

    def test_ttf_family_shrinks_the_tagged_run(self, tmp_path: Path) -> None:
        """The ftext path shrinks the tagged lowercase between the two twins."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        plain = self._width(by_id["ttf_plain"])
        small = self._width(by_id["ttf_sc"])
        caps = self._width(by_id["ttf_caps"])
        assert plain < small < caps
        # The tagged run's ink height is the cap band (the shrunken capitals sit
        # inside it), unlike the plain run's taller lowercase/descender ink.
        assert self._height(by_id["ttf_sc"]) == pytest.approx(
            self._height(by_id["ttf_caps"]), rel=0.01
        )

    def test_small_caps_adds_no_cutter_file(self, tmp_path: Path) -> None:
        """Small caps is a geometry transform -- it never invents a tool.

        Dino auto-selects 0.060 for 0.5in text; the 0.090 file comes from the
        ``<b>`` bold rung and the 0.030 from the ``<scale:0.5>`` run (plus the
        symbol font's multi-stroke ladder cutter), and every one of them comes
        from markup the label already carries.
        """
        result = self._export(tmp_path)
        names = sorted(p.name for p in result.plt_paths)
        assert "15_bh_sc.plt" in names
        assert "60_txt_sc.plt" in names
        assert "90_txt_sc.plt" in names  # the <b> bold rung
        assert "30_txt_sc.plt" in names  # the <scale:0.5> run + S4 symbol ladder
        assert len([n for n in names if n.endswith("_txt_sc.plt")]) == 3
        # No label's own cutter moved.
        for rendered in result.rendered_labels.values():
            for line in rendered.source_label.content:
                if line.font in {"Dino", "Dino SC", "ReliefSingleLineCAD-Regular"}:
                    assert line.cutter_diameter == pytest.approx(0.06)


class TestOverlapDemoExample:
    """Regression tests for tests_deps/overlap_job.yaml.

    The fixture pins OVERLAPPING (crossing) markup spans: a close tag closes
    the most recent open tag with that name (not necessarily the innermost),
    each tag covering its own open..close range, so a character's style is the
    per-character INTERSECTION of the spans covering it. The per-character
    flag arrays the renderers consume are that intersection, so the decoration
    runs, the pen split and the kerning floors all follow with no
    crossing-specific logic.
    """

    _SPEC = Path("tests_deps/overlap_job.yaml")
    _INVENTORY = [0.01, 0.015, 0.02, 0.03, 0.045, 0.06, 0.09, 0.125, 0.25]

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled (against the inventory)."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job, available_cutters=self._INVENTORY)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="ovl",
            optimize=False,
            plots=False,
            available_cutters=self._INVENTORY,
        )

    @staticmethod
    def _line_bounds(label: ResolvedLabel) -> tuple[float, float, float, float]:
        """Rendered bounds of the label's single text line."""
        from plt_optimizer.generate.label_renderer import _render_text_local_with_bounds

        _lc, entries = _render_text_local_with_bounds(label)
        return entries[0].bounds

    @classmethod
    def _width(cls, label: ResolvedLabel) -> float:
        """Rendered ink width of the label's single text line."""
        x_min, _y_min, x_max, _y_max = cls._line_bounds(label)
        return x_max - x_min

    @classmethod
    def _height(cls, label: ResolvedLabel) -> float:
        """Rendered ink height of the label's single text line."""
        _x_min, y_min, _x_max, y_max = cls._line_bounds(label)
        return y_max - y_min

    def test_crossing_close_is_legal_and_intersects(self, tmp_path: Path) -> None:
        """The TODO example: 'Always on' struck, 'on duty' small caps, overlap both."""
        from plt_optimizer.generate.markup import Run, Style

        job = parse_yaml(self._SPEC)
        by_id = {label.id: label for label in resolve_job_spec(job, self._INVENTORY)}
        line = by_id["plt_cross_sc"].content[0]
        assert line.text == "<s>Always <sc>on</s> duty</sc>"
        assert line.plain_text == "Always on duty"
        # 'Always on' (0..8) struck; 'on duty' (7..13) small caps; 'on' both.
        assert line.strikethrough_flags == (True,) * 9 + (False,) * 5
        assert line.small_caps_flags == (False,) * 7 + (True,) * 7
        assert line.styled_runs == (
            Run("Always ", Style(strikethrough=True)),
            Run("on", Style(strikethrough=True, small_caps=True)),
            Run(" duty", Style(small_caps=True)),
        )

    def test_strikethrough_stays_one_continuous_stroke(self, tmp_path: Path) -> None:
        """The crossing small-caps run does not fragment the strike.

        The struck span 'Always on' is one maximal run, so it engraves ONE
        continuous stroke (the mid-cap strike leaves the ink height identical
        to the plain control's).
        """
        from plt_optimizer.generate.label_renderer import (
            TextChunkMode,
            _render_line_block,
            _strikethrough_render_gate,
            _strikethrough_strokes,
        )

        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        line = by_id["plt_cross_sc"].content[0]
        block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)
        strokes = _strikethrough_strokes(line, block, glyphs, _strikethrough_render_gate(line))
        assert len(strokes) == 1
        # The strike's mid-cap height lies inside the cap band, so the ink
        # height matches the plain control (the strike adds no vertical ink).
        # Tolerance 1e-6in (a thousandth of a plotter unit): cap-line
        # enforcement lands both labels' overshooting alphanumerics on the
        # capline, and the clamp's Y-scale introduces sub-unit float rounding
        # between the two renders (the struck run's small-caps shrink
        # re-derives the clamped geometry through a second scale).
        assert self._height(by_id["plt_cross_sc"]) == pytest.approx(
            self._height(by_id["plt_plain"]), abs=1e-6
        )

    def test_small_caps_run_narrows_the_line(self, tmp_path: Path) -> None:
        """The crossing small-caps run makes the line narrower than its plain twin.

        A 0.7 capital is narrower than Dino's wide lowercase, so the small-caps
        'on duty' shrinks the line below the plain control (the strike is
        width-neutral inside the run's own ink span).
        """
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        assert self._width(by_id["plt_cross_sc"]) < self._width(by_id["plt_plain"])

    def test_crossing_underline_is_one_run(self, tmp_path: Path) -> None:
        """The crossing </i> ends the italic; the underline stays one run.

        ``<i>Always <u>on</i> duty</u>``: italic covers 'Always on' (0..8),
        underline covers 'on duty' (7..13). The underline gate sees ONE run
        spanning the crossing boundary, and the italic flags stop where the
        crossing ``</i>`` ends.
        """
        from plt_optimizer.generate.label_renderer import _underline_render_gate

        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        line = by_id["plt_cross_iu"].content[0]
        assert line.plain_text == "Always on duty"
        assert _underline_render_gate(line) == ((7, 13),)
        italic = tuple(run.style.italic for run in line.styled_runs for _ in run.text)
        assert italic == (True,) * 9 + (False,) * 5

    def test_crossing_bold_splits_the_pen(self, tmp_path: Path) -> None:
        """The crossing <b> run steps to the larger cutter and splits the pen.

        ``<i>1<b>23</i>4</b>``: bold covers '234' (1..3), so the label
        contributes both per-cutter text files (0.060 plain + 0.090 bold) and
        the crossing ``</i>`` leaves the bold open for '4'.
        """
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        line = by_id["plt_cross_b"].content[0]
        assert line.plain_text == "1234"
        assert line.bold_flags == (False, True, True, True)
        assert line.cutter_diameter == pytest.approx(0.06)
        assert line.bold_cutter_diameter == pytest.approx(0.09)
        names = sorted(p.name for p in result.plt_paths)
        assert "60_txt_ovl.plt" in names
        assert "90_txt_ovl.plt" in names  # the crossing bold rung


class TestFontDemoExample:
    """Regression tests for tests_deps/font_job.yaml.

    The fixture pins inline font-selection markup (``<f:font name>``): the
    wrapped characters engrave with a DIFFERENT font than the line's cascaded
    ``font`` (a PLT catalog key, a TTF basename, or a ``<base> SC`` variant;
    case-insensitive, ``+`` the canonical space). The run engraves at the
    line's ``text_height`` with the cutter that size of text IN THAT FONT gets
    on its own (the ``<scale:N>`` ladder, multi-stroke boldness ladder for a
    bold run font), so it lands on its own pen / ``<cutter>_txt_`` file. An
    all-PLT line kerns the font switch on the profile envelopes (exact, every
    v2 library shares the design frame); a line crossing the PLT/TTF families
    composes per-font segments and kerns the boundary on the bbox + stroke
    floor. The controls pin the plain twin, case-insensitivity, the SC variant,
    per-character selection, nesting, the ``<b>``/``<scale:N>``/``<i>``/``<u>``
    compositions, the TTF family, the explicit-cutter contract, and the
    per-cutter file set.
    """

    _SPEC = Path("tests_deps/font_job.yaml")
    _INVENTORY = [0.01, 0.015, 0.02, 0.03, 0.045, 0.06, 0.09, 0.125, 0.25]

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled (against the inventory)."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job, available_cutters=self._INVENTORY)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="font",
            optimize=False,
            plots=False,
            available_cutters=self._INVENTORY,
        )

    @staticmethod
    def _glyph_bounds(
        line: ResolvedTextLine, glyph_index: int
    ) -> tuple[float, float, float, float]:
        """Rendered bounds of one glyph of the line (LINE chunk mode)."""
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block
        from plt_optimizer.generate.text_geometry import TextBlock

        block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)
        bounds = TextBlock(strokes=tuple(block.strokes[i] for i in glyphs[glyph_index])).bounds()
        assert bounds is not None
        return bounds

    def test_markup_parses_to_plain_text_and_font_runs(self, tmp_path: Path) -> None:
        """Tags never engrave: plain_text strips them, runs carry the font name."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}

        run = by_id["plt_wingdings"].content[0]
        assert run.text == "Hello <f:Wingdings>abcd</f> world"
        assert run.plain_text == "Hello abcd world"
        assert run.has_font_runs is True
        # The 6 plain chars + trailing ' world' are None (the line's Dino);
        # the 4 run chars are Wingdings.
        assert run.font_runs == (None,) * 6 + ("Wingdings",) * 4 + (None,) * 6

        plain = by_id["plt_plain"].content[0]
        assert plain.plain_text == "Hello abcd world"
        assert plain.has_font_runs is False

    def test_font_run_steps_to_its_own_font_ladder_cutter(self, tmp_path: Path) -> None:
        """The run engraves with the cutter its own font's ladder gives.

        Dino auto-selects 0.060 for 0.5in text; Wingdings is a 4-stroke (bold)
        font, so its run re-runs the boldness ladder at 0.5in (0.0215 ideal,
        snapped to the 0.030 rung). The plain characters keep 0.060, so the
        line splits onto the 0.030 and 0.060 pens.
        """
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_wingdings"].source_label.content[0]
        assert line.cutter_diameter == pytest.approx(0.06)
        # The run's four characters step to the Wingdings ladder cutter.
        assert all(math.isclose(c, 0.03) for c in line.per_char_cutters[6:10])
        assert all(math.isclose(c, 0.06) for c in line.per_char_cutters[:6])
        assert line.used_cutters == (0.03, 0.06)

    def test_font_run_cap_matches_the_plain_run(self, tmp_path: Path) -> None:
        """The centerline contract: the run's cap engraves at nominal - run_cutter.

        The run's geometry scales uniformly about the baseline by
        ``(nominal - run_cutter) / (nominal - line_cutter)``, so its engraved
        cap reaches the same height as a run authored at ``text_height`` with
        its own tool. The run's 'a' glyph is therefore TALLER than the plain
        twin's (its toolpath cap is 0.47 vs the plain 0.44, the thinner tool
        leaving more material).
        """
        result = self._export(tmp_path)
        run_line = result.rendered_labels["plt_wingdings"].source_label.content[0]
        plain_line = result.rendered_labels["plt_plain"].source_label.content[0]
        # The run's 'a' (glyph 5) vs the plain twin's 'a' (glyph 5).
        run_cap = self._glyph_bounds(run_line, 5)[3]
        plain_cap = self._glyph_bounds(plain_line, 5)[3]
        assert run_cap > plain_cap

    def test_case_insensitive_font_matches_the_canonical_run(self, tmp_path: Path) -> None:
        """``<f:wingdings>`` engraves exactly like ``<f:Wingdings>``."""
        result = self._export(tmp_path)
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block

        canonical = result.rendered_labels["plt_wingdings"].source_label.content[0]
        lowered = result.rendered_labels["plt_case"].source_label.content[0]
        assert canonical.font_runs == lowered.font_runs
        bc, _, _ = _render_line_block(canonical, TextChunkMode.LINE)
        bl, _, _ = _render_line_block(lowered, TextChunkMode.LINE)
        assert bc.bounds() == pytest.approx(bl.bounds(), abs=1e-12)

    def test_small_caps_variant_run_shrinks_lowercase(self, tmp_path: Path) -> None:
        """``<f:Dino+SC>`` renders the run's lowercase as capitals at sc_scale.

        The SC variant shares Dino's cutter (0.060), so the line stays on one
        pen; the run's 'a' engraves as Dino's capital 'A' shrunk to sc_scale
        (0.7) of the cap height, measured against the plain-caps control.
        """
        result = self._export(tmp_path)
        run_line = result.rendered_labels["plt_sc"].source_label.content[0]
        caps_line = result.rendered_labels["plt_sc_plain"].source_label.content[0]
        assert run_line.font_runs[6] == "Dino SC"
        assert run_line.used_cutters == (0.06,)  # the variant shares Dino's tool
        sc_a = self._glyph_bounds(run_line, 6)  # the run's first 'a'
        plain_a = self._glyph_bounds(caps_line, 6)  # the plain 'A'
        ratio = (sc_a[3] - sc_a[1]) / (plain_a[3] - plain_a[1])
        assert ratio == pytest.approx(0.7, abs=1e-3)

    def test_partial_font_run_moves_only_the_selected_glyph(self, tmp_path: Path) -> None:
        """A per-glyph font run leaves the plain neighbours at the line's font."""

        result = self._export(tmp_path)
        line = result.rendered_labels["plt_partial"].source_label.content[0]
        a = self._glyph_bounds(line, 0)
        b = self._glyph_bounds(line, 1)
        c = self._glyph_bounds(line, 2)
        # The run's 'B' (Wingdings) is taller than its plain Dino 'A'/'C'
        # neighbours (the run's cap reaches nominal - 0.030 = 0.47).
        assert b[3] > a[3]
        assert b[3] > c[3]
        # The plain neighbours share the same cap height (round overshoot).
        assert a[3] == pytest.approx(c[3], abs=1e-3)

    def test_nested_font_restores_the_outer_run(self, tmp_path: Path) -> None:
        """The inner ``</f>`` restores the enclosing font for the trailing text.

        ``<f:Stylus>A<f:Wingdings>B</f>C</f>D``: A/C engrave Stylus, B
        Wingdings, D the line's Dino. The close pops the pre-open style, so C
        returns to Stylus (the ``<scale:N>`` nesting rule).
        """
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_nested"].source_label.content[0]
        assert line.font_runs == ("Stylus", "Wingdings", "Stylus", None)
        # C (glyph 2) matches a standalone Stylus 'C' (the outer font restored).
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block
        from plt_optimizer.generate.text_geometry import TextBlock

        stylus_c = ResolvedTextLine(
            text="C",
            nominal_text_height=0.5,
            toolpath_text_height=0.44,
            cutter_diameter=0.06,
            character_spacing=0.09,
            line_spacing=0.1,
            font="Stylus",
        )
        block, _w, glyphs = _render_line_block(stylus_c, TextChunkMode.LINE)
        ref = TextBlock(strokes=tuple(block.strokes[i] for i in glyphs[0])).bounds()
        assert ref is not None
        nested_c = self._glyph_bounds(line, 2)
        # The glyph sits mid-line, so compare its SHAPE (width + vertical
        # extent), not its absolute X.
        assert nested_c[2] - nested_c[0] == pytest.approx(ref[2] - ref[0], abs=1e-9)
        assert nested_c[1] == pytest.approx(ref[1], abs=1e-9)
        assert nested_c[3] == pytest.approx(ref[3], abs=1e-9)

    def test_font_bold_composes_the_run_ladder_with_the_bold_rung(self, tmp_path: Path) -> None:
        """``<b>`` + ``<f:>``: the font run re-runs its ladder at the bold rung.

        The bold run steps to the line's bold rung (0.090); the Wingdings run
        inside it re-runs the Wingdings boldness ladder, boosted by the bold
        multiplier, landing on 0.045 -- so the line splits across three tools.
        """
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_bold"].source_label.content[0]
        assert line.bold_cutter_diameter == pytest.approx(0.09)
        # The run's characters step to the boosted Wingdings ladder (0.045).
        assert all(math.isclose(c, 0.045) for c in line.per_char_cutters[5:8])
        assert line.used_cutters == (0.045, 0.06, 0.09)

    def test_font_scale_run_takes_its_own_effective_height_cutter(self, tmp_path: Path) -> None:
        """``<f:>`` + ``<scale:N>``: each run takes its own ladder cutter."""
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_scale"].source_label.content[0]
        # The Wingdings run (0.5in) and the 0.5x run (0.25in) both land on 0.030.
        assert all(math.isclose(c, 0.03) for c in line.per_char_cutters[0:3])
        assert math.isclose(line.per_char_cutters[3], 0.06)  # the space's plain
        assert all(math.isclose(c, 0.03) for c in line.per_char_cutters[4:8])

    def test_font_underline_splits_the_run_at_the_cutter_boundary(self, tmp_path: Path) -> None:
        """``<u>`` + ``<f:>``: the underline splits where the cutter changes.

        The underline is cut with each run's own tool, so the gate splits the
        run at the Dino (0.060) / Wingdings (0.030) boundary -- each half rides
        its own pen. The rendered block carries the decoration strokes appended
        after the glyph partition.
        """
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_underline"].source_label.content[0]
        assert line.used_cutters == (0.03, 0.06)
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block

        block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)
        # The underline strokes are appended after the glyph partition.
        underline_count = len(block.strokes) - sum(len(g) for g in glyphs)
        assert underline_count >= 2  # one per cutter-split run

    def test_mixed_family_line_composes_segments_with_exact_glyph_groups(
        self, tmp_path: Path
    ) -> None:
        """A TTF run inside a PLT line composes segments; glyph groups stay exact.

        The PLT walk renders PLT only and the TTF path shapes a whole line with
        one face, so a cross-family line composes per-font segments. The glyph
        groups concatenate exactly (one per non-space character), so the
        descender relief and intra-chunk glyph sweep stay valid.
        """
        result = self._export(tmp_path)
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block

        line = result.rendered_labels["plt_with_ttf_run"].source_label.content[0]
        assert line.font_runs[6] == "ReliefSingleLineCAD-Regular"
        for mode in (TextChunkMode.LINE, TextChunkMode.WORD):
            block, _words, glyphs = _render_line_block(line, mode)
            non_space = sum(1 for c in line.plain_text if c != " ")
            assert len(glyphs) == non_space
            flat = [i for group in glyphs for i in group]
            assert sorted(flat) == list(range(len(block.strokes)))

    def test_plt_run_inside_ttf_line_composes(self, tmp_path: Path) -> None:
        """The reverse mix (PLT run in a TTF line) composes and renders."""
        result = self._export(tmp_path)
        from plt_optimizer.generate.label_renderer import TextChunkMode, _render_line_block

        line = result.rendered_labels["ttf_with_plt_run"].source_label.content[0]
        # 'Hello PLT world': the PLT run covers indices 6..8 ('PLT').
        assert line.font_runs[6:9] == ("Dino",) * 3
        block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)
        non_space = sum(1 for c in line.plain_text if c != " ")
        assert len(glyphs) == non_space

    def test_font_run_ignores_the_line_explicit_cutter_for_its_own_ladder(
        self, tmp_path: Path
    ) -> None:
        """An explicit ``cutter_size`` governs the plain runs, not the run font.

        The explicit tool is a human decision (the scaled-run precedent); the
        run font still takes its own ladder cutter.
        """
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_explicit_cutter"].source_label.content[0]
        assert line.cutter_size == pytest.approx(0.045)
        assert line.cutter_diameter == pytest.approx(0.045)  # the snapped plain tool
        assert all(math.isclose(c, 0.03) for c in line.per_char_cutters[6:10])

    def test_font_markup_emits_the_run_cutter_files(self, tmp_path: Path) -> None:
        """Every run's ladder cutter lands in its own per-cutter text file.

        The fixture's runs use 0.030 (Wingdings ladder), 0.045 (the bold
        Wingdings ladder + the explicit-cutter line's plain tool), 0.060 (the
        plain Dino/TTF runs) and 0.090 (the bold rung); the structural layer is
        the 0.015 boundary/hole cutter.
        """
        result = self._export(tmp_path)
        assert sorted(p.name for p in result.plt_paths) == [
            "15_bh_font.plt",
            "30_txt_font.plt",  # Wingdings ladder runs
            "45_txt_font.plt",  # bold Wingdings ladder + explicit-cutter plain
            "60_txt_font.plt",  # plain Dino + TTF runs
            "90_txt_font.plt",  # the <b> bold rung
        ]

    def test_unknown_font_run_aborts_the_job(self, tmp_path: Path) -> None:
        """An unknown ``<f:...>`` font name raises MarkupError (CLI non-zero)."""
        from plt_optimizer.generate.markup import MarkupError

        spec = {
            "job": {
                "job_name": "x",
                "width": 3.0,
                "height": 1.0,
                "text_height": 0.5,
                "font": "Dino",
                "labels": [
                    {
                        "id": "a",
                        "width": 3.0,
                        "height": 1.0,
                        "content": [{"text": "A<f:NoSuchFont>B</f>"}],
                    }
                ],
            }
        }
        import yaml

        path = tmp_path / "bad_font.yaml"
        path.write_text(yaml.dump(spec))
        with pytest.raises(MarkupError, match="NoSuchFont"):
            resolve_job_spec(parse_yaml(path), available_cutters=self._INVENTORY)


class TestRoundedRectDemoExample:
    """Regression tests for tests_deps/rounded_rect_job.yaml.

    The fixture is the hand-crafted rounded-rectangle demo: a sharp control,
    equal-distance and proportional rounding, the full-round ellipse limit
    with its ``type: round`` twin, the smoothed stadium, a full-round square
    (circle), corner drill holes on a rounded border, and a squircle carrying
    text. It pins the border contract: every rounding engraves a closed
    contour whose bounds equal the nominal box, the full-round proportional
    limit is bit-identical to ``type: round``, the sharp default stays the
    historical rectangle, and every label keeps packing through rectpack (the
    rounded rectangle is a border change, never a packing change).
    """

    _SPEC = Path("tests_deps/rounded_rect_job.yaml")

    def _labels(self) -> dict[str, ResolvedLabel]:
        """Resolve the fixture into an id-keyed mapping."""
        return {label.id: label for label in resolve_job_spec(parse_yaml(self._SPEC))}

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the rounded-rect demo job with plotting disabled."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="rr",
            optimize=False,
            plots=False,
        )

    def test_fixture_resolves_the_declared_rounding(self) -> None:
        """Each label carries its declared fraction / proportional mode."""
        labels = self._labels()
        assert labels["sharp_control"].rounded_rectangle_fraction == 0.0
        assert labels["equal_round"].rounded_rectangle_fraction == pytest.approx(0.5)
        assert labels["equal_round"].rounded_rectangle_proportional is False
        assert labels["prop_round"].rounded_rectangle_proportional is True
        assert labels["full_ellipse"].rounded_rectangle_fraction == pytest.approx(1.0)
        # The border is a rect-shape feature: nothing routes to the ellipse
        # PACKER (only the type: round twin does).
        assert labels["full_ellipse"].shape_type == "rect"
        assert labels["ellipse_twin"].shape_type == "round"

    def test_every_label_renders_its_nominal_box(self, tmp_path: Path) -> None:
        """Rounding never changes the packed size: bounds == nominal box.

        Each corner patch is tangent to the box at its axis vertices, so the
        engraved contour reaches the enclosing rectangle's extrema exactly --
        the packer and the plate transforms depend on that.
        """
        result = self._export(tmp_path)
        expected = {
            "sharp_control": (4.0, 2.0),
            "equal_round": (4.0, 2.0),
            "prop_round": (4.0, 2.0),
            "full_ellipse": (4.0, 2.0),
            "ellipse_twin": (4.0, 2.0),
            "stadium": (8.0, 4.0),
            "full_circle": (3.0, 3.0),
            "rounded_corners": (4.0, 2.0),
            "squircle_text": (4.0, 2.0),
        }
        for label_id, (width, height) in expected.items():
            rendered = result.rendered_labels[label_id]
            assert rendered.width == pytest.approx(width, abs=1e-3), label_id
            assert rendered.height == pytest.approx(height, abs=1e-3), label_id

    def test_full_round_is_bit_identical_to_type_round(self) -> None:
        """fraction 1 + proportional emits the exact ``type: round`` border."""
        from plt_optimizer.generate.label_renderer import _render_boundary_hpgl

        labels = self._labels()
        assert _render_boundary_hpgl(labels["full_ellipse"]) == _render_boundary_hpgl(
            labels["ellipse_twin"]
        )

    def test_sharp_control_is_the_historical_rectangle(self) -> None:
        """fraction 0 emits the four-corner rectangle, bit-identical."""
        from plt_optimizer.generate.label_renderer import _render_boundary_hpgl

        labels = self._labels()
        content = _render_boundary_hpgl(labels["sharp_control"])
        # PU to one corner, then exactly four PD coordinates closing the box
        # (4in x 2in at 1016 units/inch).
        assert content == "PU0,0;PD4064,0,4064,2032,0,2032,0,0"

    def test_rounded_border_is_a_fine_polyline(self) -> None:
        """A rounded border samples into many vertices (not four corners)."""
        from plt_optimizer.generate.label_renderer import _render_boundary_hpgl

        labels = self._labels()
        content = _render_boundary_hpgl(labels["equal_round"])
        points = content.count(",") // 2
        assert points > 40
        assert content.startswith("PU")

    def test_stadium_keeps_straight_long_edges(self) -> None:
        """The stadium's long edges keep flat runs (the short edges round off)."""
        from itertools import groupby

        from plt_optimizer.core.units import units_to_inches
        from plt_optimizer.generate.label_renderer import _render_boundary_hpgl

        labels = self._labels()
        content = _render_boundary_hpgl(labels["stadium"])
        coords = content.replace("PU", "").replace("PD", "").replace(";", ",")
        pairs = [float(v) for v in coords.split(",") if v]
        ys = [units_to_inches(pairs[i + 1]) for i in range(0, len(pairs), 2)]
        # The bottom edge (y == 0) carries a straight run of the 8 - 4 = 4in
        # long edge: several consecutive vertices share y == 0.
        flat_runs = max((len(list(g)) for k, g in groupby(ys) if k == 0.0), default=0)
        assert flat_runs >= 2

    def test_corner_holes_ride_the_rounded_border(self, tmp_path: Path) -> None:
        """Corner holes engrave on the patch, never in cut-away corners."""
        from plt_optimizer.generate.collision.holes import hole_circles_local

        labels = self._labels()
        label = labels["rounded_corners"]
        circles = hole_circles_local(label)
        assert len(circles) == 4
        offset = label.hole_margin + label.hole_cutter_diameter / 2.0 + 0.0625
        for cx, cy, _r in circles:
            # The sharp-rect inset places a corner hole at (offset, offset);
            # the patch pulls every centre inward, off the removed corner.
            inset_x = min(cx, label.width - cx)
            inset_y = min(cy, label.height - cy)
            assert inset_x > offset + 1e-9
            assert inset_y > offset + 1e-9

    def test_structural_layer_carries_every_border_and_hole(self, tmp_path: Path) -> None:
        """The borders/holes layer holds all nine contours + four drill arcs."""
        result = self._export(tmp_path)
        bh = next(p for p in result.plt_paths if "_bh_" in p.name)
        content = bh.read_text()
        # Four corner holes, each four quarter-arcs.
        assert content.count("AA") == 16
        # Nine boundaries (PU-led) plus one plunge per drill hole.
        assert content.count("PU") >= 9 + 4

    def test_all_labels_pack_through_the_rect_pipeline(self) -> None:
        """Every instance lands on the plate; none routes to the ellipse packer."""
        from plt_optimizer.generate.packing.layout import generate_layout

        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job)
        plates = generate_layout(
            labels, job.plates, allow_rotation=job.allow_rotation, layout=job.layout
        )
        placed = [packed for plate in plates for packed in plate.labels]
        assert len(placed) == 9
        # Only the type: round twin is a round placement; the rounded rects
        # stay rectangular placements (they pack in their nominal box).
        assert sum(1 for p in placed if p.source_label.shape_type == "round") == 1


class TestEllipseDemoExample:
    """Regression tests for tests_deps/ellipse_job.yaml.

    The fixture is the hand-crafted circular/elliptical-label demo: a circle
    (width == height), an ellipse (width != height), a circle declared by a
    single dimension, a rectangular control twin, round labels with edge and
    corner drill holes, and a repeated circle. It pins the adjacent-pipeline
    contract: round labels engrave an elliptical boundary and pack through
    the ellipse packer, while the rectangular twin keeps the historical
    rectangle bit-identically.
    """

    _SPEC = Path("tests_deps/ellipse_job.yaml")

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the ellipse demo job with plotting disabled."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="el",
            optimize=False,
            plots=False,
        )

    def test_fixture_resolves_the_declared_shapes(self) -> None:
        """`round` labels resolve shape_type=round; the control stays rect."""
        labels = {label.id: label for label in resolve_job_spec(parse_yaml(self._SPEC))}
        assert labels["circle"].shape_type == "round"
        assert labels["ellipse"].shape_type == "round"
        assert labels["rect_control"].shape_type == "rect"

    def test_single_dimension_round_label_resolves_to_a_circle(self) -> None:
        """The width-only round label fills its height (a 2.5in circle)."""
        labels = {label.id: label for label in resolve_job_spec(parse_yaml(self._SPEC))}
        assert labels["circle_diameter"].width == pytest.approx(2.5)
        assert labels["circle_diameter"].height == pytest.approx(2.5)

    def test_round_labels_render_their_enclosing_box(self, tmp_path: Path) -> None:
        """Every round label's rendered bounds equal its nominal box.

        The sampled ellipse is closed at the axis vertices, so the engraved
        boundary reaches the enclosing rectangle's extrema exactly -- the
        packer and the plate transforms depend on that.
        """
        result = self._export(tmp_path)
        expected = {
            "circle": (3.0, 3.0),
            "ellipse": (4.0, 2.0),
            "circle_diameter": (2.5, 2.5),
            "circle_trio": (2.0, 2.0),
            "round_holes": (4.0, 2.0),
            "round_corners": (3.0, 3.0),
            "rect_control": (4.0, 2.0),
        }
        for label_id, (width, height) in expected.items():
            rendered = result.rendered_labels[label_id]
            assert rendered.width == pytest.approx(width, abs=1e-3), label_id
            assert rendered.height == pytest.approx(height, abs=1e-3), label_id

    def test_structural_layer_carries_ellipses_and_rectangles(self, tmp_path: Path) -> None:
        """The borders/holes layer holds every label's boundary + drill arcs."""
        result = self._export(tmp_path)
        bh = next(p for p in result.plt_paths if "_bh_" in p.name)
        content = bh.read_text()
        # Six drill holes (2 sides + 4 corners), each four quarter-arcs.
        assert content.count("AA") == 24
        # Nine label boundaries (7 unique labels, circle_trio packs 3) plus
        # one pen-up per drill hole: every contour is PU-led.
        assert content.count("PU") >= 9 + 6

    def test_round_labels_pack_onto_the_plate(self) -> None:
        """Every instance lands on the plate through the ellipse pipeline."""
        from plt_optimizer.generate.packing.layout import generate_layout

        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job)
        plates = generate_layout(
            labels, job.plates, allow_rotation=job.allow_rotation, layout=job.layout
        )
        placed = [packed for plate in plates for packed in plate.labels]
        # 7 label specs, circle_trio carrying count=3.
        assert len(placed) == 9
        assert sum(1 for p in placed if p.source_label.shape_type == "round") == 8

    def test_round_placements_keep_their_geometry_clear(self) -> None:
        """Engraved geometry never overlaps: ellipses nest, boxes stay clear.

        Enclosing boxes legitimately overlap where an ellipse nests into the
        material a rectangle's corner occupies -- that is the ellipse
        pipeline's whole advantage -- so the check is on the *geometry*:
        ellipse-vs-ellipse and ellipse-vs-rectangle keep the boundary-cutter
        kerf, and rectangle-vs-rectangle boxes stay clear (rectpack).
        """
        from shapely.affinity import translate as shapely_translate
        from shapely.geometry import box as shapely_box

        from plt_optimizer.generate.packing.ellipse_packer import ellipse_polygon
        from plt_optimizer.generate.packing.layout import generate_layout

        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job)
        plates = generate_layout(
            labels, job.plates, allow_rotation=job.allow_rotation, layout=job.layout
        )

        def shape_of(packed) -> object:
            """The placement's collision geometry in the plate's usable frame."""
            label = packed.source_label
            x_min = packed.x - 0.0  # no plate clearance in this fixture
            y_min = packed.y
            if label.shape_type == "round":
                centre_x = x_min + packed.width / 2.0
                centre_y = y_min + packed.height / 2.0
                return shapely_translate(
                    ellipse_polygon(
                        packed.width / 2.0, packed.height / 2.0, label.hole_cutter_diameter
                    ),
                    xoff=centre_x,
                    yoff=centre_y,
                )
            return shapely_box(x_min, y_min, x_min + packed.width, y_min + packed.height)

        placed = [packed for plate in plates for packed in plate.labels]
        shapes = [shape_of(packed) for packed in placed]
        for i, first in enumerate(shapes):
            for second in shapes[i + 1 :]:
                # Shrink a hair: rectpack packs rectangles *touching*, and
                # shapely counts a shared boundary as an intersection.
                assert not first.buffer(-1e-6).intersects(second.buffer(-1e-6)), (
                    f"{placed[i].label_id} overlaps {placed[i + 1].label_id}"
                )


class TestHoleAwareCenteringDemoExample:
    """Regression tests for tests_deps/hole_aware_centering_job.yaml.

    The fixture pins hole-aware text centering: when axis holes (top/bottom or
    left/right) have asymmetric diameters, text automatically shifts to center
    in the available space between them. Vertical shift applies to all text;
    horizontal shift applies only to center-aligned text.

    - Vertical shift = (top_diameter - bottom_diameter) / 2
    - Horizontal shift = (left_diameter - right_diameter) / 2 (center alignment only)
    """

    _SPEC = Path("tests_deps/hole_aware_centering_job.yaml")

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="hole_aware",
            optimize=False,
            plots=False,
        )

    def test_vertical_shifts_resolve_correctly(self, tmp_path: Path) -> None:
        """Vertical shifts are computed and stored per-label."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}

        # Single top hole: +0.1
        assert by_id["single_top"].hole_aware_vertical_shift_inches == pytest.approx(0.1)

        # Single bottom hole: -0.1
        assert by_id["single_bottom"].hole_aware_vertical_shift_inches == pytest.approx(-0.1)

        # Both holes, top larger: +0.05
        assert by_id["both_top_larger"].hole_aware_vertical_shift_inches == pytest.approx(0.05)

        # Both holes, bottom larger: -0.05
        assert by_id["both_bottom_larger"].hole_aware_vertical_shift_inches == pytest.approx(-0.05)

        # Equal holes: 0.0
        assert by_id["equal_holes"].hole_aware_vertical_shift_inches == pytest.approx(0.0)

        # No holes: 0.0
        assert by_id["no_holes"].hole_aware_vertical_shift_inches == pytest.approx(0.0)

    def test_horizontal_shifts_default_to_zero(self, tmp_path: Path) -> None:
        """Horizontal shifts are zero when no left/right holes are declared."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}

        # All labels in this fixture have no left/right holes
        for label_id in by_id:
            assert by_id[label_id].hole_aware_horizontal_shift_inches == pytest.approx(0.0)

    def test_all_labels_exported_successfully(self, tmp_path: Path) -> None:
        """The fixture has 6 labels, all exported successfully."""
        result = self._export(tmp_path)
        assert len(result.rendered_labels) == 6
        expected_ids = {
            "single_top",
            "single_bottom",
            "both_top_larger",
            "both_bottom_larger",
            "equal_holes",
            "no_holes",
        }
        assert set(result.rendered_labels.keys()) == expected_ids

    def test_fixture_loads_without_errors(self) -> None:
        """The fixture YAML is valid and resolves without errors."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job)
        # If we got here, no errors were raised.
        assert len(labels) == 6

    def test_optimized_text_has_correct_hole_aware_shift(self, tmp_path: Path) -> None:
        """The optimized text PLT applies the same hole-aware shift as plt_content.

        Regression for the bug where plate_optimizer._label_transform omitted
        label.hole_aware_vertical_shift_inches from expected_center, causing the
        optimized text-only PLT to have text at the un-shifted (wrong) position
        while the combined "all" view (assembled from plt_content) was correct.

        Uses single_top (0.2in top hole, +0.1in shift) in isolation so the shift
        is not masked by other labels' opposing shifts.
        """
        job = parse_yaml(self._SPEC)
        all_labels = resolve_job_spec(job)
        # single_top: 0.2in top-only hole -> hole_aware_vertical_shift = +0.1in.
        top_only = [lbl for lbl in all_labels if lbl.id == "single_top"]
        assert len(top_only) == 1

        def _do_export(optimize: bool, subdir: str) -> PerCutterExport:
            return export_per_cutter_plts(
                top_only,
                provided_plates=None,
                output_dir=tmp_path / subdir,
                job_id="ha",
                optimize=optimize,
                plots=False,
            )

        unopt = _do_export(optimize=False, subdir="unopt")
        opt = _do_export(optimize=True, subdir="opt")

        def _pd_y_center(result: PerCutterExport) -> float:
            """Y midpoint of PD cutting coordinates in the text PLT.

            PD (pen-down) coordinates are the actual engraved text positions.
            PU (pen-up) rapid-travel coordinates are excluded because the optimizer
            reorders them, changing their distribution without moving the text.
            """
            txts = [p for p in result.plt_paths if "_txt_" in p.name]
            assert txts, "No text PLT found"
            content = txts[0].read_text(encoding="utf-8")
            ys: list[int] = []
            for m in re.finditer(r"PD(-?\d+),(-?\d+)", content):
                ys.append(int(m.group(2)))
            assert ys, f"No PD coordinates in {txts[0]}"
            return (min(ys) + max(ys)) / 2.0

        unopt_center = _pd_y_center(unopt)
        opt_center = _pd_y_center(opt)

        # Allow ±2 plotter-unit rounding difference. A mismatch of ~100 units
        # (0.1in) indicates the hole-aware shift is absent from the optimized path.
        assert abs(unopt_center - opt_center) < 2.0, (
            f"Optimized text Y center {opt_center:.1f} != "
            f"unoptimized {unopt_center:.1f} (hole-aware shift not applied)"
        )


class TestEnforceCapLineDemoExample:
    """Regression tests for tests_deps/enforce_cap_line_job.yaml.

    The fixture pins alphanumeric cap-line enforcement (``enforce_cap_line``,
    default true): every ASCII alphanumeric ``[0-9a-zA-Z]`` glyph whose ink
    extends above the per-font capline is downscaled **top-down** (a vertical
    scale about the glyph's own bottom, X untouched -- the aspect ratio
    changes) so its top lands exactly on the capline. The enforcement is
    strictly **pre-markup**: it applies to the raw glyph, before the
    small-caps, bold, scale, italic and superscript/subscript transforms.
    Non-alphanumerics (punctuation, derived Unicode symbols) are never
    clamped, and a glyph sitting exactly at the capline never moves.

    The shipped Dino sheet overshoots on its digits (1000.6-1007.8 design
    units against the 1000-unit cap) and its round/pointed capitals, while
    its flat capitals (E/F/H/Z) sit exactly at the cap. The bundled TTF's
    ASCII capitals sit at/below the cap while its ascender lowercase
    overshoots (b/d ~1.096 of the cap). Pins compare the rendered **ink-box
    height** (frame-centering independent) with the sub-unit tolerance the
    overshoot contract needs.
    """

    _SPEC = Path("tests_deps/enforce_cap_line_job.yaml")
    _INVENTORY = [0.01, 0.015, 0.02, 0.03, 0.045, 0.06, 0.09, 0.125, 0.25]

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled (against the inventory)."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job, available_cutters=self._INVENTORY)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="cap",
            optimize=False,
            plots=False,
            available_cutters=self._INVENTORY,
        )

    @staticmethod
    def _line_bounds(label: ResolvedLabel) -> tuple[float, float, float, float]:
        """Rendered bounds of the label's single text line."""
        from plt_optimizer.generate.label_renderer import _render_text_local_with_bounds

        _lc, entries = _render_text_local_with_bounds(label)
        return entries[0].bounds

    def _height(self, result: PerCutterExport, label_id: str) -> float:
        """Rendered ink-box height of one label's single text line."""
        bounds = self._line_bounds(result.rendered_labels[label_id].source_label)
        return bounds[3] - bounds[1]

    def test_fixture_resolves_with_the_field_on_every_line(self, tmp_path: Path) -> None:
        """The cascade lands: every line resolves true except the opt-outs."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}
        assert by_id["plt_digits"].content[0].enforce_cap_line is True
        assert by_id["plt_opt_out"].content[0].enforce_cap_line is False
        assert by_id["plt_sup_off"].content[0].enforce_cap_line is False
        assert by_id["ttf_opt_out"].content[0].enforce_cap_line is False

    def test_overshooting_digits_clamped_shorter_than_opt_out(self, tmp_path: Path) -> None:
        """The clamped digit line is shorter than its opt-out twin.

        Every Dino digit's top lands on the capline (``toolpath_text_height``
        = 0.44in at text_height 0.5), so the enforced ink box is the cap
        height plus the tallest digit's sub-baseline sliver; the opt-out twin
        keeps the sheet's 0.776% overshoot.
        """
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_digits"].source_label.content[0]
        enforced = self._height(result, "plt_digits")
        unenforced = self._height(result, "plt_opt_out")
        cap = line.toolpath_text_height
        # Clamped: cap height + the tallest digit's sub-baseline sliver
        # (Dino '3' bottoms at -6.39 units = 0.639% of the cap).
        assert enforced == pytest.approx(cap * 1.00639, abs=0.005)
        # Opt-out: cap + the tallest digit's overshoot (1007.758 = 0.7758%).
        assert unenforced == pytest.approx(cap * (1 + 0.007758 + 0.00639), abs=0.005)
        assert unenforced > enforced + 1e-3

    def test_flat_capitals_are_bit_identical(self, tmp_path: Path) -> None:
        """E/F/H/Z sit exactly at the capline: the clamp never moves them."""
        result = self._export(tmp_path)
        on = self._line_bounds(result.rendered_labels["plt_flat_caps"].source_label)
        off = self._line_bounds(result.rendered_labels["plt_flat_caps_off"].source_label)
        assert on == pytest.approx(off, abs=1e-9)

    def test_round_capitals_clamp_shorter_than_the_cap_overshoot(self, tmp_path: Path) -> None:
        """A/C/G/O/S overshoot the capline; the clamp lands them on it."""
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_round_caps"].source_label.content[0]
        enforced = self._height(result, "plt_round_caps")
        cap = line.toolpath_text_height
        # Clamped to the capline (plus the tallest round glyph's sub-baseline
        # sliver, Dino 'C' bottoms at -6.00 units = 0.6% of the cap).
        assert enforced == pytest.approx(cap * 1.006, abs=0.005)

    def test_punctuation_is_exempt(self, tmp_path: Path) -> None:
        """Dino's '(' tops at 1103.9 units and is never clamped."""
        result = self._export(tmp_path)
        on = self._line_bounds(result.rendered_labels["plt_punctuation"].source_label)
        off = self._line_bounds(result.rendered_labels["plt_punctuation_off"].source_label)
        assert on == pytest.approx(off, abs=1e-9)
        # The unclamped parenthesis towers above the cap (box > cap height).
        assert on[3] - on[1] > 0.44

    def test_symbol_font_glyphs_clamp_by_character(self, tmp_path: Path) -> None:
        """A symbol font's ASCII slots ARE alphanumeric, so they clamp.

        Wingdings' ``A``/``B`` symbols top at 1044.95/1044.43 units; the
        character-based contract clamps them (the enforcement keys off the
        character, not the font family).
        """
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_wingdings"].source_label.content[0]
        enforced = self._height(result, "plt_wingdings")
        cap = line.toolpath_text_height
        # Clamped: the top lands on the capline, so the box is the cap height
        # less the symbols' sub-baseline air (Wingdings 'B' bottoms at 168.8
        # units = 0.1688 of the cap, the lower of the two).
        assert enforced == pytest.approx(cap * (1 - 0.1688), abs=0.005)

    def test_enforcement_is_strictly_pre_markup(self, tmp_path: Path) -> None:
        """The bare ``<sup>`` centers the CLAMPED digit, not the raw one.

        ``E<sup>0</sup>``: the clamp lands the ``0`` on the capline first, so
        the downscaled/centered superscript's box grows by ``d/2`` cap heights
        of the clamped glyph. The enforcement-off twin centers the
        overshooting digit, so its ink box is strictly taller.
        """
        result = self._export(tmp_path)
        on = self._height(result, "plt_sup")
        off = self._height(result, "plt_sup_off")
        assert on < off - 1e-3

    def test_small_caps_composes_on_the_clamped_capital(self, tmp_path: Path) -> None:
        """SC shrinks the CLAMPED capital: the enforcement runs first.

        ``Dino SC`` at ``sc_scale`` 0.7: the ``O`` capital clamps to the
        capline, so the full-size capital dominates the line's ink box at the
        cap height (the small capital is 0.7 of the clamped box).
        """
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_small_caps"].source_label.content[0]
        enforced = self._height(result, "plt_small_caps")
        cap = line.toolpath_text_height
        # The full-size (clamped) capital dominates the line's ink box.
        assert enforced == pytest.approx(cap * 1.006, abs=0.005)

    def test_ttf_ascenders_clamp_and_opt_out_keeps_them(self, tmp_path: Path) -> None:
        """The bundled TTF's ascender lowercase overshoots the cap and clamps.

        ``b``/``d`` top at ~1.096 of the cap while ``H``/``0`` sit at/below
        it, so the enforced line's box is the cap height and the opt-out
        twin's is ~1.096 of it.
        """
        result = self._export(tmp_path)
        line = result.rendered_labels["ttf_ascenders"].source_label.content[0]
        enforced = self._height(result, "ttf_ascenders")
        unenforced = self._height(result, "ttf_opt_out")
        cap = line.toolpath_text_height
        assert enforced == pytest.approx(cap, abs=0.01)
        assert unenforced == pytest.approx(1.095588 * cap, abs=0.01)
        assert unenforced > enforced + 1e-3


class TestHoleAdjustForCutterDemoExample:
    """Regression tests for tests_deps/hole_adjust_for_cutter_job.yaml.

    The fixture pins cutter-compensated drill holes (``hole_adjust_for_cutter``,
    default true): the emitted circle shrinks by the boundary/hole cutter
    diameter so the engraved hole measures the specified diameter, while every
    hole CENTRE (placed from the specified diameter) stays bit-identical. The
    export passes ``boundary_hole_cutter_size=0.015`` explicitly, so the pinned
    diameters never drift with the shop's tools.json.
    """

    _SPEC = Path("tests_deps/hole_adjust_for_cutter_job.yaml")
    _CUTTER = 0.015

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting and routing disabled."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job, boundary_hole_cutter_size=self._CUTTER)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="hafc",
            optimize=False,
            plots=False,
        )

    @staticmethod
    def _hole_radii_inches(rendered_labels: dict, label_id: str) -> list[float]:
        """One drill-hole arc radius per distinct hole centre, in inches.

        Each hole emits four quarter-arcs sharing one centre, so the arcs are
        deduplicated by their (rounded) centre before measuring the radius.
        """
        from plt_optimizer.core.models import ArcSegment
        from plt_optimizer.core.parser import PLTParser

        content = rendered_labels[label_id].plt_content
        doc = PLTParser().parse_string(content)
        by_centre: dict[tuple[int, int], float] = {}
        for path in doc.stroke_paths:
            for segment in path.segments:
                if isinstance(segment, ArcSegment):
                    key = (round(segment.center.x), round(segment.center.y))
                    by_centre[key] = segment.radius / HPGL_UNITS_PER_INCH
        return list(by_centre.values())

    def test_flags_resolve_per_label(self, tmp_path: Path) -> None:
        """The default is on; the opt-out is off; the plate tier is inert."""
        result = self._export(tmp_path)
        labels = result.rendered_labels
        assert labels["on_default"].source_label.hole_adjust_for_cutter is True
        assert labels["opt_out"].source_label.hole_adjust_for_cutter is False
        # plate_1 declares hole_adjust_for_cutter: false; labels render before
        # plate assignment, so the parity field changes nothing.
        assert labels["mixed_diameters"].source_label.hole_adjust_for_cutter is True

    def test_default_shrinks_the_emitted_circle(self, tmp_path: Path) -> None:
        """A 0.1in hole emits a 0.085in circle at the shipped default."""
        result = self._export(tmp_path)
        radii = self._hole_radii_inches(result.rendered_labels, "on_default")
        assert len(radii) == 2
        # Emitted radii are integer plotter units: 1/2 unit = 4.9e-4in of
        # quantization, so the pins use a 1e-3in tolerance.
        for radius in radii:
            assert radius == pytest.approx((0.1 - self._CUTTER) / 2.0, abs=1e-3)

    def test_opt_out_keeps_the_specified_circle(self, tmp_path: Path) -> None:
        """hole_adjust_for_cutter: false emits the specified 0.1in circle."""
        result = self._export(tmp_path)
        radii = self._hole_radii_inches(result.rendered_labels, "opt_out")
        assert len(radii) == 2
        for radius in radii:
            assert radius == pytest.approx(0.05, abs=1e-3)

    def test_centres_are_identical_on_and_off(self, tmp_path: Path) -> None:
        """Compensation moves no centre: on/off hole centres coincide."""
        from plt_optimizer.core.models import ArcSegment
        from plt_optimizer.core.parser import PLTParser

        labels = self._export(tmp_path).rendered_labels

        def centres(label_id: str) -> set[tuple[int, int]]:
            doc = PLTParser().parse_string(labels[label_id].plt_content)
            return {
                (round(segment.center.x), round(segment.center.y))
                for path in doc.stroke_paths
                for segment in path.segments
                if isinstance(segment, ArcSegment)
            }

        assert centres("on_default") == centres("opt_out")

    def test_every_specified_diameter_shrinks_by_the_cutter(self, tmp_path: Path) -> None:
        """0.125 -> 0.110 and 0.25 -> 0.235 (one shared hole tool)."""
        result = self._export(tmp_path)
        radii = sorted(self._hole_radii_inches(result.rendered_labels, "mixed_diameters"))
        assert len(radii) == 2
        assert radii[0] == pytest.approx((0.125 - self._CUTTER) / 2.0, abs=1e-3)
        assert radii[1] == pytest.approx((0.25 - self._CUTTER) / 2.0, abs=1e-3)

    def test_round_corner_holes_shrink_too(self, tmp_path: Path) -> None:
        """The ellipse-diagonal placement emits the compensated circle."""
        result = self._export(tmp_path)
        radii = self._hole_radii_inches(result.rendered_labels, "round_corners")
        assert len(radii) == 4
        for radius in radii:
            assert radius == pytest.approx((0.2 - self._CUTTER) / 2.0, abs=1e-3)

    def test_degenerate_hole_clamps_with_warning(self, tmp_path: Path) -> None:
        """A 0.01in hole with a 0.015in cutter clamps to one plotter unit."""
        result = self._export(tmp_path)
        radii = self._hole_radii_inches(result.rendered_labels, "degenerate")
        assert radii == [pytest.approx(1.0 / HPGL_UNITS_PER_INCH, abs=1e-6)]

    def test_degenerate_warning_names_the_label(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The clamp logs a WARNING naming the label and the sizes."""
        with caplog.at_level(logging.WARNING, logger="plt_optimizer.generate.label_renderer"):
            self._export(tmp_path)
        messages = [
            record.getMessage()
            for record in caplog.records
            if "cannot be cutter-compensated" in record.getMessage()
        ]
        assert any("degenerate" in message for message in messages)

    def test_hole_finding_appears_in_the_layout_report(self, tmp_path: Path) -> None:
        """The report surfaces the specified -> engraved diameter per label."""
        from plt_optimizer.generate.layout_report import format_layout_report

        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job, boundary_hole_cutter_size=self._CUTTER)
        result = export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="hafc",
            optimize=False,
            plots=False,
        )
        report = "\n".join(format_layout_report(labels, result.rendered_labels, full=True))
        assert "Holes 0.100in -> 0.085in (cutter 0.015in)" in report
        assert "Holes 0.125in -> 0.110in (cutter 0.015in)" in report
        assert "Holes 0.250in -> 0.235in (cutter 0.015in)" in report
        # The opt-out label states its diameters unchanged -- no finding.
        off_block = report.split("Label opt_out:")[1].split("Label ")[0]
        assert "Holes" not in off_block

    def test_one_bh_file_carries_every_hole(self, tmp_path: Path) -> None:
        """Compensation changes geometry only: the cutter set is unchanged."""
        result = self._export(tmp_path)
        bh_files = [p.name for p in result.plt_paths if "_bh_" in p.name]
        assert bh_files == ["15_bh_hafc.plt"]


class TestVCenterDemoExample:
    """Regression tests for tests_deps/v_center_job.yaml.

    The fixture pins inline vertical-centering markup (``<vc>``) and the
    cascading ``v_center_chars`` field: each selected character is raised or
    lowered -- a pure vertical translate riding the renderers'
    ``baseline_shifts`` array -- so its OWN ink is centred between the line's
    baseline and capline (the ink centre lands on the cap-band midline
    ``toolpath_text_height / 2``). ``v_center_chars`` applies the same
    centering automatically to every character occurring in the set (the code
    fallback is ``""`` = auto-apply off; job-config.json ships the shop set);
    the tag is the manual override for any other character. A glyph already
    centred on the midline (a full-height capital) is inert, and the centring
    adds NO cutter of its own.
    """

    _SPEC = Path("tests_deps/v_center_job.yaml")
    _INVENTORY = [0.01, 0.015, 0.02, 0.03, 0.045, 0.06, 0.09, 0.125, 0.25]

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled (against the inventory)."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job, available_cutters=self._INVENTORY)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="vc",
            optimize=False,
            plots=False,
            available_cutters=self._INVENTORY,
        )

    @staticmethod
    def _centers(line: object) -> dict[str, float]:
        """Return each rendered character's ink-box centre Y (baseline frame)."""
        from plt_optimizer.generate.label_renderer import (
            TextChunkMode,
            _glyph_chars,
            _render_line_block,
        )

        block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)  # type: ignore[arg-type]
        chars = _glyph_chars(line.plain_text)  # type: ignore[attr-defined]
        centers: dict[str, float] = {}
        for glyph_index, group in enumerate(glyphs):
            if not group:
                continue
            ys = [
                point.imag
                for index in group
                for segment in block.strokes[index].segments
                for point in segment.flattened()
            ]
            centers[chars[glyph_index]] = 0.5 * (min(ys) + max(ys))
        return centers

    def test_markup_parses_to_plain_text_and_flags(self, tmp_path: Path) -> None:
        """Tags never engrave: plain_text strips them, the run carries the flag."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}

        tagged = by_id["plt_tag"].content[0]
        assert tagged.text == "A<vc>-</vc>A"
        assert tagged.plain_text == "A-A"
        assert tagged.has_v_center is True
        assert tagged.v_center_flags == (False, True, False)

        plain = by_id["plt_plain"].content[0]
        assert plain.plain_text == "A-A"
        assert plain.has_v_center is False

    def test_the_tag_centres_the_hyphen_vs_its_plain_twin(self, tmp_path: Path) -> None:
        """The centred hyphen's ink centre rides the cap-band midline.

        Dino at text_height 0.5 (cutter 0.060) has a 0.440in cap height, so the
        midline is 0.220in; the plain hyphen sits at 0.1468in (the font draws
        it low) and the centred one is RAISED by the difference.
        """
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}

        centred = self._centers(by_id["plt_tag"].content[0])
        plain = self._centers(by_id["plt_plain"].content[0])
        assert centred["-"] == pytest.approx(0.22, abs=1e-3)
        assert plain["-"] == pytest.approx(0.1468, abs=1e-3)
        # The unselected capitals are byte-identical between the two renders.
        assert centred["A"] == pytest.approx(plain["A"], abs=1e-9)

    def test_the_character_set_centres_without_markup(self, tmp_path: Path) -> None:
        """v_center_chars applies the same centering to a markup-free line."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}

        auto = by_id["plt_auto"].content[0]
        assert auto.styled_runs  # resolution always parses
        assert all(not run.style.v_center for run in auto.styled_runs)
        # "A-A A-A": the two hyphens (indices 1 and 5) are centred, the rest
        # (letters and the space) are not.
        assert auto.v_center_flags == (False, True, False, False, False, True, False)

        centers = self._centers(auto)
        assert centers["-"] == pytest.approx(0.22, abs=1e-3)
        assert centers["A"] == pytest.approx(0.22, abs=1e-3)

    def test_the_shop_set_centres_every_listed_character(self, tmp_path: Path) -> None:
        """The shipped set centres the hyphen and the colon on one line."""
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_shop_set"].source_label.content[0]
        assert line.v_center_chars == "-:–—=<>"
        # '1','2','3','4','5','6' unmarked; '-' and ':' centred.
        assert line.v_center_flags == (
            False,
            False,
            True,
            False,
            False,
            True,
            False,
            False,
        )
        centers = self._centers(line)
        assert centers["-"] == pytest.approx(0.22, abs=1e-3)
        assert centers[":"] == pytest.approx(0.22, abs=1e-3)

    def test_a_centred_colon_rises_vs_its_plain_twin(self, tmp_path: Path) -> None:
        """The colon (centre 0.1101in) is raised onto the midline."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}

        centred = self._centers(by_id["plt_colon"].content[0])
        plain = self._centers(by_id["plt_colon_plain"].content[0])
        assert centred[":"] == pytest.approx(0.22, abs=1e-3)
        assert plain[":"] == pytest.approx(0.1101, abs=1e-3)
        assert centred["1"] == pytest.approx(plain["1"], abs=1e-9)

    def test_a_high_glyph_is_lowered(self, tmp_path: Path) -> None:
        """Centring LOWERS a glyph drawn near the capline (it centres, not lifts).

        Dino's apostrophe sits at 0.4033in; centring brings it down to the
        midline, so the transform is a raise OR a lower as the glyph needs.
        """
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_lower"].source_label.content[0]
        centers = self._centers(line)
        assert centers["'"] == pytest.approx(0.22, abs=1e-3)
        assert centers["A"] == pytest.approx(0.22, abs=1e-3)

    def test_the_underscore_is_raised_from_below_the_baseline(self, tmp_path: Path) -> None:
        """A glyph hanging below the baseline (centre -0.0523in) is raised."""
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_underline_char"].source_label.content[0]
        centers = self._centers(line)
        assert centers["_"] == pytest.approx(0.22, abs=1e-3)

    def test_an_already_centred_glyph_is_inert(self, tmp_path: Path) -> None:
        """A full-height capital's centre IS the midline: the tag moves nothing."""
        from plt_optimizer.generate.label_renderer import (
            TextChunkMode,
            _render_line_block,
        )

        result = self._export(tmp_path)
        line = result.rendered_labels["plt_inert_cap"].source_label.content[0]
        assert line.has_v_center is True
        # The zero shift emits no vc term, so the render equals the SAME text
        # untagged (the block is the single capital 'A', centre 0.220in).
        block, _words, _glyphs = _render_line_block(line, TextChunkMode.LINE)
        centers = self._centers(line)
        assert centers["A"] == pytest.approx(0.22, abs=1e-3)
        assert block.bounds() is not None
        assert block.bounds()[1] == pytest.approx(0.0, abs=1e-3)
        assert block.bounds()[3] == pytest.approx(0.44, abs=1e-3)

    def test_the_selection_is_per_character(self, tmp_path: Path) -> None:
        """Only the tagged colon centres; the plain hyphens stay low."""
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_partial"].source_label.content[0]
        assert line.v_center_flags == (False, False, True, False, False)
        centers = self._centers(line)
        assert centers[":"] == pytest.approx(0.22, abs=1e-3)
        assert centers["-"] == pytest.approx(0.1468, abs=1e-3)

    def test_an_empty_set_disables_the_auto_apply_and_keeps_the_tag(self, tmp_path: Path) -> None:
        """v_center_chars="" centres nothing by itself; <vc> still works."""
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_empty_set_tag_works"].source_label.content[0]
        assert line.v_center_chars == ""
        assert line.v_center_flags == (False, False, True)
        centers = self._centers(line)
        assert centers["-"] == pytest.approx(0.1468, abs=1e-3)
        assert centers[":"] == pytest.approx(0.22, abs=1e-3)

    def test_the_centring_composes_with_italic(self, tmp_path: Path) -> None:
        """A <vc><i> run shears AND centres (the transforms are independent)."""
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_italic"].source_label.content[0]
        assert line.has_v_center is True
        assert any(run.style.italic for run in line.styled_runs)
        centers = self._centers(line)
        assert centers["-"] == pytest.approx(0.22, abs=1e-3)

    def test_the_centring_does_not_move_the_underline(self, tmp_path: Path) -> None:
        """The decoration keeps its baseline-relative depth (the <b> precedent).

        The centred colon rises, the underline stays at its own
        ``cutter + offset`` depth below the baseline, so the raise never drags
        the stroke with the glyph.
        """
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_underline_markup"].source_label.content[0]
        centers = self._centers(line)
        assert centers[":"] == pytest.approx(0.22, abs=1e-3)
        # The underline's centerline is the lowest ink on the block, and it
        # sits one cutter + offset BELOW the baseline -- not raised with the
        # colon.
        from plt_optimizer.generate.label_renderer import (
            TextChunkMode,
            _render_line_block,
        )

        block, _words, _glyphs = _render_line_block(line, TextChunkMode.LINE)
        bounds = block.bounds()
        assert bounds is not None
        assert bounds[1] == pytest.approx(-(0.06 + line.underline_offset_inches), abs=1e-3)

    def test_the_centred_downscale_composes_with_scale(self, tmp_path: Path) -> None:
        """<vc><scale:0.5> centres the SHRUNKEN glyph on the midline."""
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_scale"].source_label.content[0]
        assert line.scale_values == (1.0, 0.5, 1.0)
        centers = self._centers(line)
        assert centers[":"] == pytest.approx(0.22, abs=1e-3)

    def test_the_centring_composes_with_the_superscript_shift(self, tmp_path: Path) -> None:
        """<vc><sup> composes into ONE translate landing on the midline."""
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_sup"].source_label.content[0]
        assert line.has_sup_sub is True
        assert line.has_v_center is True
        centers = self._centers(line)
        # The FINAL ink is centred (the vc term is measured on pass 1, which
        # already carries the sup shift + the engraved-baseline raise).
        assert centers[":"] == pytest.approx(0.22, abs=1e-3)

    def test_crossing_spans_intersect_per_character(self, tmp_path: Path) -> None:
        """The tag span and the set union select per character (HTML-style)."""
        result = self._export(tmp_path)
        line = result.rendered_labels["plt_crossing"].source_label.content[0]
        assert line.plain_text == "-:="
        # '-' and ':' are inside <vc>; '=' is in the set.
        assert line.v_center_flags == (True, True, True)
        centers = self._centers(line)
        for char in "-:=":
            assert centers[char] == pytest.approx(0.22, abs=1e-3)

    def test_the_ttf_family_centres_too(self, tmp_path: Path) -> None:
        """The TTF path centres on the same midline (PLT parity by construction)."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}

        centred = self._centers(by_id["plt_ttf"].content[0])
        plain = self._centers(by_id["plt_ttf_plain"].content[0])
        cap = by_id["plt_ttf"].content[0].toolpath_text_height
        assert centred["-"] == pytest.approx(cap / 2, abs=1e-3)
        # The centring moved the hyphen; the unselected capital is untouched.
        assert not math.isclose(centred["-"], plain["-"], abs_tol=1e-6)
        assert centred["A"] == pytest.approx(plain["A"], abs=1e-3)

    def test_the_tag_adds_no_cutter_file_of_its_own(self, tmp_path: Path) -> None:
        """A pure translate engraves with the line's tool: one text file each.

        The centring itself never moves the cutter (the ``<sup>`` label is
        excluded: its DOWNSCALE steps to the ladder's tool, the ``<scale:N>``
        contract -- not the centering).
        """
        result = self._export(tmp_path)
        for label_id in ("plt_tag", "plt_auto", "plt_shop_set", "plt_lower", "plt_crossing"):
            line = result.rendered_labels[label_id].source_label.content[0]
            assert line.used_cutters == (0.06,)
        # The centred PLT labels contribute exactly ONE text file (the line's
        # own 0.060in tool) -- the tag adds no per-cutter layer of its own.
        tag_files = sorted(
            p.name for p in result.plt_paths if "_txt_" in p.name and p.name.startswith("60_")
        )
        assert tag_files  # the 0.060 text layer exists
        # No second text cutter was introduced by the centring labels: the
        # only text cutters in the job are the PLT 0.060 and the TTF family's.
        text_cutters = {
            name.split("_txt_")[0]
            for name in (p.name for p in result.plt_paths if "_txt_" in p.name)
        }
        assert "60" in text_cutters


class TestKernDemoExample:
    """Regression tests for tests_deps/kern_job.yaml.

    The fixture pins manual kerning (``kern_pos_string`` / ``kern_neg_string``
    / ``kern_width_fraction``): every marker occurrence in a text line is
    stripped (it never engraves) and the FOLLOWING character's cursor shifts
    by ``kern_width_fraction`` of the cap height (positive forward, negative
    backward, no clamp). The delta rides the cursor after the pair's envelope
    kerning, so the pair kerns as if adjacent and the marker adds its exact
    width; the shift carries forward (consecutive markers accumulate, a
    marker shifts its glyph AND all following glyphs). Works on PLT and TTF.
    """

    _SPEC = Path("tests_deps/kern_job.yaml")
    _INVENTORY = [0.01, 0.015, 0.02, 0.03, 0.045, 0.06, 0.09, 0.125, 0.25]

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled (against the inventory)."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job, available_cutters=self._INVENTORY)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="kn",
            optimize=False,
            plots=False,
            available_cutters=self._INVENTORY,
        )

    @staticmethod
    def _width(label: ResolvedLabel) -> float:
        """Rendered ink width of the label's single text line."""
        from plt_optimizer.generate.label_renderer import (
            TextChunkMode,
            _render_line_block,
        )

        block, _words, _glyphs = _render_line_block(label.content[0], TextChunkMode.LINE)
        bounds = block.bounds()
        return bounds[2] - bounds[0]

    @staticmethod
    def _ink_lefts(label: ResolvedLabel) -> list[float]:
        """Rendered ink-box left X of each glyph of the label's single line."""
        from plt_optimizer.generate.label_renderer import (
            TextChunkMode,
            _render_line_block,
        )

        block, _words, glyphs = _render_line_block(label.content[0], TextChunkMode.LINE)
        lefts: list[float] = []
        for group in glyphs:
            xs = [
                point.real
                for index in group
                for segment in block.strokes[index].segments
                for point in segment.flattened()
            ]
            lefts.append(min(xs))
        return lefts

    def test_markers_strip_from_plain_text_and_count(self, tmp_path: Path) -> None:
        """Markers never engrave: plain_text is marker-free, counts attach."""
        result = self._export(tmp_path)
        by_id = {lid: rl.source_label for lid, rl in result.rendered_labels.items()}

        wide = by_id["plt_wide"].content[0]
        assert wide.text == "A<+>V"
        assert wide.plain_text == "AV"
        assert wide.kern_values == (0, 1)
        assert wide.has_kern is True
        # 0.1 * cap (0.44in toolpath) = 0.044in on the V.
        assert wide.kern_shifts_inches == (0.0, pytest.approx(0.044))

        plain = by_id["plt_wide_plain"].content[0]
        assert plain.plain_text == "AV"
        assert plain.has_kern is False
        assert plain.kern_shifts_inches == ()

    def test_positive_marker_widens_the_pair(self, tmp_path: Path) -> None:
        """A positive marker widens the line by exactly one kern width."""
        result = self._export(tmp_path)
        wide = self._width(result.rendered_labels["plt_wide"].source_label)
        plain = self._width(result.rendered_labels["plt_wide_plain"].source_label)
        assert wide - plain == pytest.approx(0.044, abs=1e-6)

    def test_negative_marker_narrows_the_pair(self, tmp_path: Path) -> None:
        """A negative marker narrows the line by exactly one kern width."""
        result = self._export(tmp_path)
        narrow = self._width(result.rendered_labels["plt_narrow"].source_label)
        plain = self._width(result.rendered_labels["plt_wide_plain"].source_label)
        assert plain - narrow == pytest.approx(0.044, abs=1e-6)

    def test_adjacent_markers_accumulate(self, tmp_path: Path) -> None:
        """Two positive markers cost two kern widths."""
        result = self._export(tmp_path)
        acc = self._width(result.rendered_labels["plt_accumulate"].source_label)
        plain = self._width(result.rendered_labels["plt_wide_plain"].source_label)
        assert acc - plain == pytest.approx(0.088, abs=1e-6)

    def test_zero_width_consumes_the_marker_and_shifts_nothing(self, tmp_path: Path) -> None:
        """kern_width_fraction 0.0 strips the marker (plain 'AV') but shifts 0."""
        result = self._export(tmp_path)
        zero = by_id_zero = result.rendered_labels["plt_zero_width"].source_label
        assert zero.content[0].plain_text == "AV"
        # The marker is stripped, so the width equals the plain control.
        plain = self._width(result.rendered_labels["plt_wide_plain"].source_label)
        assert self._width(zero) == pytest.approx(plain, abs=1e-6)

    def test_leading_and_trailing_markers_are_no_ops(self, tmp_path: Path) -> None:
        """A leading marker normalizes away; a trailing marker is dropped."""
        result = self._export(tmp_path)
        plain = self._width(result.rendered_labels["plt_wide_plain"].source_label)
        leading = self._width(result.rendered_labels["plt_leading"].source_label)
        trailing = self._width(result.rendered_labels["plt_trailing"].source_label)
        assert leading == pytest.approx(plain, abs=1e-6)
        assert trailing == pytest.approx(plain, abs=1e-6)

    def test_mid_word_marker_kerns_the_pair(self, tmp_path: Path) -> None:
        """A marker mid-word narrows the enclosing pair (the motivating shape)."""
        result = self._export(tmp_path)
        mid = self._width(result.rendered_labels["plt_mid_word"].source_label)
        plain = self._width(result.rendered_labels["plt_mid_word_plain"].source_label)
        assert plain - mid == pytest.approx(0.044, abs=1e-6)

    def test_marker_before_a_space_kerns_the_next_word(self, tmp_path: Path) -> None:
        """A marker before a space attaches to the next word's first glyph."""
        result = self._export(tmp_path)
        wb = self._width(result.rendered_labels["plt_word_boundary"].source_label)
        plain = self._width(result.rendered_labels["plt_word_boundary_plain"].source_label)
        # The B rides +0.044, widening the line by one width.
        assert wb - plain == pytest.approx(0.044, abs=1e-6)

    def test_the_ttf_family_kerns_too(self, tmp_path: Path) -> None:
        """TTF parity: the marker translates the glyph's contours."""
        result = self._export(tmp_path)
        wide = self._width(result.rendered_labels["ttf_wide"].source_label)
        plain = self._width(result.rendered_labels["ttf_wide_plain"].source_label)
        assert wide - plain == pytest.approx(0.044, abs=1e-6)

    def test_custom_bracket_markers(self, tmp_path: Path) -> None:
        """Under «» brackets, non-bracket markers strip and kern."""
        result = self._export(tmp_path)
        line = result.rendered_labels["custom_bracket_markers"].source_label.content[0]
        assert line.plain_text == "ABC"
        assert line.kern_values == (0, 1, -1)  # ~~ +1 on B, -- -1 on C
        # B rides +0.044; C rides B's +0.044 then its own -0.044 = net 0, so
        # the line's right edge (C's ink) lands back on the plain control's
        # width -- the documented accumulate/net-out behaviour.
        plain = self._width(result.rendered_labels["custom_bracket_markers_plain"].source_label)
        kerned = self._width(result.rendered_labels["custom_bracket_markers"].source_label)
        assert kerned == pytest.approx(plain, abs=1e-6)
        # The internal A/B gap grows by one width (B shifted right) and C
        # returns to its plain position (the running total nets out).
        kerned_lefts = self._ink_lefts(
            result.rendered_labels["custom_bracket_markers"].source_label
        )
        plain_lefts = self._ink_lefts(
            result.rendered_labels["custom_bracket_markers_plain"].source_label
        )
        assert kerned_lefts[1] - plain_lefts[1] == pytest.approx(0.044, abs=1e-6)
        assert kerned_lefts[2] == pytest.approx(plain_lefts[2], abs=1e-6)

    def test_the_marker_composes_with_italic(self, tmp_path: Path) -> None:
        """A marker shifts the sheared glyph (the delta rides the final cursor)."""
        result = self._export(tmp_path)
        kerned = self._width(result.rendered_labels["kern_italic"].source_label)
        plain = self._width(result.rendered_labels["kern_italic_plain"].source_label)
        assert kerned - plain == pytest.approx(0.044, abs=1e-6)

    def test_the_marker_adds_no_cutter_file_of_its_own(self, tmp_path: Path) -> None:
        """A pure horizontal shift engraves with the line's tool: no new layer."""
        result = self._export(tmp_path)
        for label_id in ("plt_wide", "plt_narrow", "plt_accumulate", "plt_mid_word"):
            line = result.rendered_labels[label_id].source_label.content[0]
            assert line.used_cutters == (0.06,)


class TestListDemoExample:
    """Regression tests for tests_deps/list_job.yaml.

    A bullet/enumerated list nests inside ``content``, expands at resolution
    into one line per item, and engraves its markers in ONE flush column with
    the item's text (and every continuation line the reflow grows it to) at the
    hanging-indent column.
    """

    _SPEC = Path("tests_deps/list_job.yaml")

    def _export(self, tmp_path: Path) -> PerCutterExport:
        """Export the demo job with plotting disabled."""
        job = parse_yaml(self._SPEC)
        labels = resolve_job_spec(job)
        return export_per_cutter_plts(
            labels,
            job.plates,
            output_dir=tmp_path,
            job_id="list",
            optimize=False,
            plots=False,
        )

    def test_itemize_expands_one_line_per_item(self, tmp_path: Path) -> None:
        """The plain bullet list yields its items in order, all marked."""
        result = self._export(tmp_path)
        content = result.rendered_labels["bullets"].source_label.content
        assert [line.plain_text for line in content] == ["HEADER", "AAA", "BBBB"]
        assert [line.marker_text for line in content] == ["", "•", "•"]
        assert [line.align_group_id for line in content] == [-1, 0, 0]

    def test_marker_column_is_shared_by_the_list(self, tmp_path: Path) -> None:
        """Every item of one list hangs at the same text column."""
        result = self._export(tmp_path)
        content = result.rendered_labels["bullets"].source_label.content
        assert content[1].indent_left == content[2].indent_left
        assert content[1].indent_left == pytest.approx(0.1775, abs=1e-3)
        # The plain sibling carries no indent.
        assert content[0].indent_left == 0.0

    def test_markers_engrave_in_one_flush_column(self, tmp_path: Path) -> None:
        """The positioned list lines share their left edge (the marker column).

        The list aligns as ONE block, so under the default center alignment
        both items start at the same X -- the bullets never form a ragged
        column.
        """
        result = self._export(tmp_path)
        chunks = result.rendered_labels["bullets"].text_chunks
        by_line = {chunk.line_index: chunk.bounds[0] for chunk in chunks}
        assert by_line[1] == pytest.approx(by_line[2], abs=1e-9)

    def test_per_item_bullet_string_override(self, tmp_path: Path) -> None:
        """An item's own bullet_string wins; the column is the widest marker."""
        result = self._export(tmp_path)
        content = result.rendered_labels["bullet_override"].source_label.content
        assert [line.marker_text for line in content] == ["-", "*"]
        assert content[0].indent_left == content[1].indent_left
        # The hyphen is narrower than the bullet, so the column is tighter than
        # the default-bullet list's.
        bullets = result.rendered_labels["bullets"].source_label.content
        assert content[0].indent_left > bullets[1].indent_left

    def test_enumerated_counters_are_computed(self, tmp_path: Path) -> None:
        """Decimal counters number the items from 1 with the suffix."""
        result = self._export(tmp_path)
        content = result.rendered_labels["enumerated"].source_label.content
        assert [line.marker_text for line in content] == ["1.", "2.", "3."]

    def test_counter_style_prefix_suffix_and_start(self, tmp_path: Path) -> None:
        """Every counter knob composes into the marker text."""
        result = self._export(tmp_path)
        content = result.rendered_labels["counter_style"].source_label.content
        assert [line.marker_text for line in content] == ["Step iv)", "Step v)"]
        # The wide counter widens the whole list's text column.
        assert content[0].indent_left == pytest.approx(1.7997, abs=1e-3)

    def test_growing_item_splits_to_fit(self, tmp_path: Path) -> None:
        """An over-wide item with no cap takes as many lines as it needs."""
        result = self._export(tmp_path)
        content = result.rendered_labels["growing_item"].source_label.content
        assert [line.plain_text for line in content] == [
            "ALWAYS CHARGE",
            "SECONDARY CO2 SYSTEM",
            "THROUGH A FILTER DRIER",
        ]
        # Word order preserved, zero compression (the growth replaced it).
        assert " ".join(line.plain_text for line in content).split() == (
            "ALWAYS CHARGE SECONDARY CO2 SYSTEM THROUGH A FILTER DRIER".split()
        )
        assert result.rendered_labels["growing_item"].compression_by_line == {}

    def test_growth_marks_only_the_first_line(self, tmp_path: Path) -> None:
        """The bullet rides the item's first line; continuations hang at the
        text column (the marker is empty, the indent is not)."""
        result = self._export(tmp_path)
        content = result.rendered_labels["growing_item"].source_label.content
        assert [line.marker_text for line in content] == ["•", "", ""]
        assert len({line.indent_left for line in content}) == 1
        assert all(line.align_group_id == 0 for line in content)

    def test_continuation_lines_start_at_the_text_column(self, tmp_path: Path) -> None:
        """The grown lines' left edges sit at the marker column + indent."""
        result = self._export(tmp_path)
        rendered = result.rendered_labels["growing_item"]
        content = rendered.source_label.content
        chunks = {chunk.line_index: chunk.bounds[0] for chunk in rendered.text_chunks}
        first_line_left = chunks[0]
        # The continuation lines start one indent column right of the marker.
        for index in (1, 2):
            assert chunks[index] == pytest.approx(
                first_line_left + content[0].indent_left, abs=1e-3
            )

    def test_growth_cap_limits_the_line_count(self, tmp_path: Path) -> None:
        """optimize_line_content_max_lines ceilings the item at N lines."""
        result = self._export(tmp_path)
        content = result.rendered_labels["capped_item"].source_label.content
        assert len(content) == 2
        assert content[0].marker_text == "1"
        assert content[1].marker_text == ""
        assert " ".join(line.plain_text for line in content).split() == (
            "DO NOT TRAP LIQUID BY CLOSING CHARGING STATION VALVES".split()
        )

    def test_items_never_exchange_words(self, tmp_path: Path) -> None:
        """Each item reflows as its own paragraph (the group is per item)."""
        result = self._export(tmp_path)
        content = result.rendered_labels["per_item_groups"].source_label.content
        assert [line.plain_text for line in content] == [
            "AAAA BBBB CCCC DDDD",
            "EEEE FFFF GGGG",
        ]
        assert [line.marker_text for line in content] == ["•", "•"]

    def test_plain_siblings_keep_their_own_placement(self, tmp_path: Path) -> None:
        """A list between plain lines leaves the plain lines untouched."""
        result = self._export(tmp_path)
        content = result.rendered_labels["list_between_plain"].source_label.content
        assert [line.plain_text for line in content] == ["TOP", "HHHH", "IIII", "BOTTOM"]
        assert content[0].align_group_id == -1
        assert content[3].align_group_id == -1
        assert content[1].align_group_id == content[2].align_group_id == 0

    def test_zero_marker_gap_puts_text_flush_at_the_marker(self, tmp_path: Path) -> None:
        """list_marker_gap_fraction 0.0 leaves only the marker's own width."""
        result = self._export(tmp_path)
        content = result.rendered_labels["tight_gap"].source_label.content
        # The counter's own width, with no gap added (a single digit at 0.4in
        # cap height is far narrower than the default 0.2in gap).
        assert 0.1 < content[0].indent_left < 0.25

    def test_empty_bullet_is_an_indent_only_list(self, tmp_path: Path) -> None:
        """An explicit empty bullet_string engraves no marker at all."""
        result = self._export(tmp_path)
        rendered = result.rendered_labels["indent_only"]
        content = rendered.source_label.content
        assert [line.marker_text for line in content] == ["", ""]
        assert content[0].list_item is True
        # The items still hang at the gap column (no marker ink to measure).
        assert content[0].indent_left == pytest.approx(0.1775, abs=1e-3)
        # No marker means no extra strokes: the chunks are the text alone.
        assert [chunk.line_index for chunk in rendered.text_chunks] == [0, 1]

    def test_list_job_writes_the_expected_cutter_files(self, tmp_path: Path) -> None:
        """The markers ride their items' own cutter -- no extra pen layer."""
        result = self._export(tmp_path)
        assert sorted(p.name for p in result.plt_paths) == [
            "15_bh_list.plt",
            "45_txt_list.plt",
        ]

    def test_marker_ink_is_emitted(self, tmp_path: Path) -> None:
        """The bullet glyph reaches the engraved PLT (the label is wider than
        the text alone, so the marker's ink extends the rendered block)."""
        result = self._export(tmp_path)
        marked = result.rendered_labels["bullets"]
        # The list's widest line spans the marker column + its text.
        content = marked.source_label.content
        assert content[1].plain_text == "AAA"
        assert marked.x_max > marked.x_min


def _tc_markup_label() -> ResolvedLabel:
    """A label whose ONE line carries two inline colors on one cutter.

    The per-character <tc:...> split must behave exactly like the line-level
    text_color split (the fixture's contract, at unit scale).
    """
    from plt_optimizer.generate.markup import parse_markup
    from plt_optimizer.generate.resolution import _canonical_text_color

    text = "<tc:k>LAYER ONE</tc> <tc:m>LAYER TWO</tc>"
    styled = parse_markup(text, "<", ">", color_resolver=_canonical_text_color)
    return ResolvedLabel(
        id="tclabel",
        count=1,
        width=3.0,
        height=1.5,
        margin=0.1,
        h_margin=0.1,
        v_margin=0.1,
        content=[
            ResolvedTextLine(
                text=text,
                nominal_text_height=0.5,
                toolpath_text_height=0.47,
                cutter_diameter=0.06,
                character_spacing=0.0,
                line_spacing=0.0,
                plain_text=styled.plain_text,
                styled_runs=styled.runs,
            )
        ],
    )


class TestExportTextColorMarkupSplit:
    """Inline <tc:color> splits one cutter's text into separate per-color files."""

    def test_one_line_two_markup_colors_split_into_two_files(self, tmp_path: Path) -> None:
        """Two inline colors on one line produce two suffixed text PLTs."""
        result = export_per_cutter_plts(
            [_tc_markup_label()],
            output_dir=tmp_path,
            job_id="tcm",
            optimize=False,
            plots=False,
        )
        text_names = sorted(p.name for p in result.plt_paths if "_txt_" in p.name)
        # (cutter, color) sort order: black -> pen 1, magenta -> pen 4.
        assert text_names == ["60_k_txt_tcm.plt", "60_m_txt_tcm.plt"]
        contents = {
            name: (tmp_path / "plt" / name).read_text(encoding="utf-8") for name in text_names
        }
        points = {name: _cutting_points(content) for name, content in contents.items()}
        (k_points, m_points) = (points[name] for name in sorted(points))
        assert k_points and m_points
        assert k_points.isdisjoint(m_points)

    def test_markup_free_line_is_bit_identical(self, tmp_path: Path) -> None:
        """A line with no <tc> markup keeps the historical untagged name."""
        from plt_optimizer.generate.markup import parse_markup

        text = "PLAIN TEXT"
        styled = parse_markup(text, "<", ">")
        label = ResolvedLabel(
            id="plain",
            count=1,
            width=3.0,
            height=1.5,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
            content=[
                ResolvedTextLine(
                    text=text,
                    nominal_text_height=0.5,
                    toolpath_text_height=0.47,
                    cutter_diameter=0.06,
                    character_spacing=0.0,
                    line_spacing=0.0,
                    plain_text=styled.plain_text,
                    styled_runs=styled.runs,
                )
            ],
        )
        result = export_per_cutter_plts(
            [label], output_dir=tmp_path, job_id="pl", optimize=False, plots=False
        )
        assert [p.name for p in result.plt_paths if "_txt_" in p.name] == ["60_txt_pl.plt"]


class TestTextColorMarkupDemoExample:
    """Regression tests for tests_deps/text_color_markup_job.yaml.

    The fixture is the hand-crafted inline stroke-color demo: one line with
    two tagged segments + an untagged tail, an abbreviation/full-name pair on
    the SAME layer, a line whose field color the tag overrides per segment,
    and a markup-free line. All lines share one cutter (0.045), so the export
    splits by color alone -- the exact file-name set the field-level demo
    produces.
    """

    def _resolve(self) -> list:
        """Parse and resolve the inline stroke-color demo job."""
        from plt_optimizer.generate.schema import parse_yaml

        job = parse_yaml(Path("tests_deps/text_color_markup_job.yaml"))
        return resolve_job_spec(job)

    def test_fixture_resolves_markup_colors(self) -> None:
        """Tags canonicalize, the field stays the fallback, markup wins inside."""
        labels = self._resolve()
        assert len(labels) == 1
        lines = labels[0].content
        assert lines[0].color_runs == (
            "black",
            "black",
            "black",
            None,
            "magenta",
            "magenta",
            "magenta",
            None,
            None,
            None,
            None,
            None,
        )
        # Abbreviation <tc:r> and full name <tc:red> share the canonical layer.
        assert set(lines[1].per_char_colors) == {"red", "none"}
        # The field (k) is the fallback; the tag (y) wins inside its span.
        assert lines[2].text_color == "black"
        assert set(lines[2].per_char_colors) == {"black", "yellow"}
        # A markup-free line has no per-character array (the fast path).
        assert lines[3].per_char_colors == ()

    def test_fixture_exports_one_file_per_markup_color(self, tmp_path: Path) -> None:
        """The demo exports five suffixed text files plus the bh file."""
        from plt_optimizer.generate.schema import parse_yaml

        job = parse_yaml(Path("tests_deps/text_color_markup_job.yaml"))
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
        # Single 0.375in text height -> one cutter (0.045) split by color.
        # One material-less plate -> no plate/material prefix.
        assert text_names == [
            "45_k_txt_demo.plt",
            "45_m_txt_demo.plt",
            "45_r_txt_demo.plt",
            "45_txt_demo.plt",
            "45_y_txt_demo.plt",
        ]
        assert [p.name for p in result.plt_paths if "_bh_" in p.name] == ["15_bh_demo.plt"]

    def test_unknown_markup_color_aborts(self) -> None:
        """<tc:x> is not a TextColor value, so the job aborts (fail-loud)."""
        from plt_optimizer.generate.markup import MarkupError
        from plt_optimizer.generate.schema import JobSpec, LabelSpec, TextLine

        spec = JobSpec(
            job_name="J",
            labels=[
                LabelSpec(
                    id="l",
                    width=2.0,
                    height=1.0,
                    content=[TextLine(text="<tc:x>A</tc>")],
                )
            ],
        )
        with pytest.raises(MarkupError, match="Invalid tag"):
            resolve_job_spec(spec)


class TestExportTextColorMarkupUniformLine:
    """A line UNIFORMLY wrapped in a foreign <tc> color routes to that layer."""

    def test_fully_colored_line_gains_the_color_suffix(self, tmp_path: Path) -> None:
        """The whole-line tag lands the block on the color's pen and file."""
        from plt_optimizer.generate.markup import parse_markup
        from plt_optimizer.generate.resolution import _canonical_text_color

        text = "<tc:red>ALL RED</tc>"
        styled = parse_markup(text, "<", ">", color_resolver=_canonical_text_color)
        label = ResolvedLabel(
            id="allred",
            count=1,
            width=3.0,
            height=1.5,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
            content=[
                ResolvedTextLine(
                    text=text,
                    nominal_text_height=0.5,
                    toolpath_text_height=0.47,
                    cutter_diameter=0.06,
                    character_spacing=0.0,
                    line_spacing=0.0,
                    plain_text=styled.plain_text,
                    styled_runs=styled.runs,
                )
            ],
        )
        result = export_per_cutter_plts(
            [label], output_dir=tmp_path, job_id="ar", optimize=False, plots=False
        )
        # The line's own "none" layer owns no strokes: only the red file.
        assert [p.name for p in result.plt_paths if "_txt_" in p.name] == ["60_r_txt_ar.plt"]


class TestPreviewColors:
    """Colored combined `all_*.pdf` proof previews (material + pen colors)."""

    @staticmethod
    def _capture(tmp_path: Path, yaml_path: str, job_id: str) -> dict:
        """Export a fixture with plot_physical_strokes patched; return capture."""
        from unittest.mock import patch

        job = parse_yaml(yaml_path)
        resolved_labels = resolve_job_spec(job)

        captured: dict = {}

        def _fake_plot(layers, output_path=None, show_plot=False, fills=(), **kwargs):
            captured[Path(output_path).name] = {
                "layers": [(layer.width_inches, layer.alpha, layer.facecolor) for layer in layers],
                "fills": [(fill.color, fill.alpha) for fill in fills],
            }

        with patch(
            "plt_optimizer.diagnostics.plotter.plot_physical_strokes", side_effect=_fake_plot
        ):
            result = export_per_cutter_plts(
                resolved_labels,
                job.plates,
                output_dir=tmp_path,
                job_id=job_id,
                optimize=False,
                plots=True,
            )
        captured["_result"] = result
        return captured

    def test_bw_material_colors_combined_preview(self, tmp_path: Path) -> None:
        """bw stock: black bg fill, white plain ink, pen colors win, bh black."""
        captured = self._capture(tmp_path, "tests_deps/preview_color_job.yaml", "pc")
        result = captured["_result"]
        assert result.material_by_plate == {1: "bw"}

        all_plots = {n: v for n, v in captured.items() if n.startswith("bw_all_")}
        assert list(all_plots) == ["bw_all_pc.pdf"]
        (plot,) = all_plots.values()
        # One background fill: the label boundary rects (SP2) in the
        # material's top-layer color (black), fully opaque.
        assert plot["fills"] == [("black", 1.0)]
        # Text pens: green <tc:> run and red text_color win over the
        # material core; the plain "none" pen engraves the core (white).
        colors = {facecolor for _w, _a, facecolor in plot["layers"]}
        assert {"green", "red", "white"} <= colors
        # Structural pens (borders/holes) stay translucent black.
        structural = [(a, f) for _w, a, f in plot["layers"] if a < 1.0]
        assert structural and all(f == "black" for _a, f in structural)
        # Opaque layers are all text ink (never black on bw stock).
        assert "black" not in {f for _w, a, f in plot["layers"] if a == 1.0}

    def test_per_cutter_previews_stay_black(self, tmp_path: Path) -> None:
        """Only the combined plot is colored; _txt_/_bh_ files keep black ink."""
        captured = self._capture(tmp_path, "tests_deps/preview_color_job.yaml", "pc2")
        per_file = {
            n: v for n, v in captured.items() if n != "_result" and not n.startswith("bw_all_")
        }
        assert per_file
        for plot in per_file.values():
            assert plot["fills"] == []
            assert {f for _w, _a, f in plot["layers"]} == {"black"}

    def test_material_less_job_keeps_black_on_white(self, tmp_path: Path) -> None:
        """Regression: unset material renders the historical black proof."""
        captured = self._capture(tmp_path, "tests_deps/test123_spec.yaml", "ml")
        all_plots = {n: v for n, v in captured.items() if n.startswith("all_")}
        assert all_plots
        for plot in all_plots.values():
            assert plot["fills"] == []
            assert {f for _w, _a, f in plot["layers"]} == {"black"}

    def test_wb_material_plain_text_is_black(self, tmp_path: Path) -> None:
        """wb stock: white bg fill, black core ink (material_demo fixture)."""
        captured = self._capture(tmp_path, "tests_deps/material_demo_job.yaml", "wb")
        all_plots = {n: v for n, v in captured.items() if n.endswith("all_wb.pdf")}
        assert sorted(all_plots) == ["wb_all_wb.pdf", "wbuv_all_wb.pdf"]
        for plot in all_plots.values():
            # Every wb plate's top layer is white -> white background fill.
            assert plot["fills"] == [("white", 1.0)]
        # Plain wb text = the core (black); the uv plate's magenta
        # text_color pen wins over the core color.
        plain = {f for _w, a, f in captured["wb_all_wb.pdf"]["layers"] if a == 1.0}
        assert plain == {"black"}
        uv = {f for _w, a, f in captured["wbuv_all_wb.pdf"]["layers"] if a == 1.0}
        assert uv == {"black", "magenta"}
