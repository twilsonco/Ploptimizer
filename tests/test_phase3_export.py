"""Tests for the per-cutter Phase 3 export pipeline."""

import logging
import math
import re
from pathlib import Path
from typing import Optional

import pytest

from plt_optimizer.generate.layout import LayoutMode
from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine, resolve_job_spec
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

        # 1 inch == 1000 plotter units; the left-most cut moves right by 1".
        before = min(_first_cutting_x(p) for p in baseline.plt_paths if "_bh_" in p.name)
        after = min(_first_cutting_x(p) for p in shifted.plt_paths if "_bh_" in p.name)
        assert after - before == 1000

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
