"""Tests for the per-cutter Phase 3 export pipeline."""

import re
from pathlib import Path

from plt_optimizer.generate.layout import LayoutMode
from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine
from plt_optimizer.generate.resolution import resolve_job_spec
from plt_optimizer.generate.schema import parse_yaml
from plt_optimizer.generate.vectorize import (
    PerCutterExport,
    _format_text_layer,
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
            assert content.startswith("IN;DF;PS0;")
            assert content.endswith("%")
            assert len(content) > 50  # Has actual content

        # test123 uses a single 0.5in text height (ideal cutter 0.06in,
        # no inventory snapping) plus borders. Names are
        # <plate number>_<kind>_<cutter>_<job_id>.plt (2-digit plate).
        names = sorted(p.name for p in exported_paths)
        assert any(name.startswith("01_txt_0.060_") for name in names)
        assert any(name.startswith("01_bh_0.015_") for name in names)
        # The combined PLT is never written to disk.
        assert not any("_all_" in name for name in names)
        # Every file leads with the padded plate number and ends with the job id.
        assert all(name.startswith("01_") for name in names)
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
        # One combined <plate>_all_<job_id>.pdf per plate.
        assert any(name.endswith("_all_plotjob.pdf") for name in pdf_names)
        # Combined content is exposed in memory, never written as PLT.
        assert result.combined_by_plate
        assert not any("_all_" in p.stem for p in result.plt_paths)

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
        # Combined color plot mirrors the <plate>_all_<job_id>.pdf name.
        assert any("_all_dp_" in name and name.endswith("_default.pdf") for name in names)
        assert all(p.parent == tmp_path / "pdf" for p in result.default_pdf_paths)
        # Opt-in plots are tracked separately from the simple previews.
        assert result.pdf_paths == []

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
        # Combined per-plate plots mix layers and stay non-structural.
        all_plots = [flag for name, flag in by_name.items() if "_all_" in name]
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

        Color-suffixed layers (``..._txt_<cutter>_<color>_<job>.plt``) keep
        the cutter at name index 2, so the tag extraction below covers both
        tagged and untagged text files.
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
        # Name shape: <plate>_txt_<cutter>_<job_id>.plt (job_id has no '_').
        cutter_tags = {name.split("_")[2] for name in text_files}
        # complex_test_job exercises at least three distinct text cutters.
        assert len(cutter_tags) >= 3
        # Every tag is a 3-decimal inch string.
        for tag in cutter_tags:
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
        # The colored layers appear as additional suffixed files (the plate
        # prefix depends on where the packer places the label, so match on
        # the suffix).
        assert any(name.endswith("_txt_0.040_k_complex.plt") for name in text_files)
        assert any(name.endswith("_txt_0.040_m_complex.plt") for name in text_files)
        # The implicit "none" layer keeps the historical cutter-only name.
        assert any(name.endswith("_txt_0.040_complex.plt") for name in text_files)
        # The two colored files are *additional* toolpaths: each carries only
        # its own layer's strokes on a single pen.
        black_file = next(
            p for p in result.plt_paths if p.name.endswith("_txt_0.040_k_complex.plt")
        )
        magenta_file = next(
            p for p in result.plt_paths if p.name.endswith("_txt_0.040_m_complex.plt")
        )
        black_content = black_file.read_text(encoding="utf-8")
        magenta_content = magenta_file.read_text(encoding="utf-8")
        # Each color file selects exactly one layer pen (SP1+; SP0 tokens are
        # the header/trailer pen resets, never a layer).
        black_pens = {p for p in re.findall(r"SP(\d+);", black_content) if p != "0"}
        magenta_pens = {p for p in re.findall(r"SP(\d+);", magenta_content) if p != "0"}
        assert len(black_pens) == 1
        assert len(magenta_pens) == 1
        assert black_pens != magenta_pens
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
) -> ResolvedLabel:
    """Build a minimal ``ResolvedLabel`` for export tests."""
    return ResolvedLabel(
        id=label_id,
        count=count,
        width=width,
        height=height,
        margin=0.0,
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

        small_plates = {p.name.split("_")[0] for p in small.plt_paths}
        large_plates = {p.name.split("_")[0] for p in large.plt_paths}
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
        # (cutter, color) sort order: black -> pen 1, magenta -> pen 4.
        assert text_names == [
            "01_txt_0.060_k_clr.plt",
            "01_txt_0.060_m_clr.plt",
        ]
        # Each file carries geometry on exactly one text pen (plus headers).
        for name, pen in (("01_txt_0.060_k_clr.plt", 1), ("01_txt_0.060_m_clr.plt", 4)):
            content = (tmp_path / "plt" / name).read_text(encoding="utf-8")
            assert f"SP{pen};" in content
            other = 4 if pen == 1 else 1
            assert f"SP{other};" not in content
            assert re.search(r"(?:PU|PD)\d", content)

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
        assert bh_names == ["01_bh_0.015_clr.plt"]

    def test_colorless_export_names_are_unchanged(self, tmp_path: Path) -> None:
        """Jobs without colors keep the historical cutter-only names."""
        result = export_per_cutter_plts(
            [_label()],
            output_dir=tmp_path,
            job_id="plain",
            optimize=False,
            plots=False,
        )
        text_names = sorted(p.name for p in result.plt_paths if "_txt_" in p.name)
        assert text_names == ["01_txt_0.030_plain.plt"]

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
            "01_txt_0.060_k_opt.plt",
            "01_txt_0.060_m_opt.plt",
        ]
        for name in text_names:
            content = (tmp_path / "plt" / name).read_text(encoding="utf-8")
            assert content.startswith("IN;DF;PS0;")
            assert re.search(r"(?:PU|PD)\d", content)


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
        assert text_names == [
            "01_txt_0.045_demo.plt",
            "01_txt_0.045_k_demo.plt",
            "01_txt_0.045_m_demo.plt",
        ]
        assert [p.name for p in result.plt_paths if "_bh_" in p.name] == [
            "01_bh_0.015_demo.plt"
        ]
        # Each text file carries geometry on exactly one pen.
        pens_per_file = []
        for path in result.plt_paths:
            if "_txt_" not in path.name:
                continue
            content = path.read_text(encoding="utf-8")
            pens = set(re.findall(r"SP(\d+);", content)) - {"0"}
            assert len(pens) == 1, f"{path.name} spans pens {pens}"
            assert re.search(r"(?:PU|PD)\d", content)
            pens_per_file.append(int(pens.pop()))
        assert len(set(pens_per_file)) == 3
