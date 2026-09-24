"""Tests for the per-cutter Phase 3 export pipeline."""

import re
from pathlib import Path

from plt_optimizer.generate.layout import LayoutMode
from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine
from plt_optimizer.generate.resolution import resolve_job_spec
from plt_optimizer.generate.schema import parse_yaml
from plt_optimizer.generate.vectorize import (
    PerCutterExport,
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
        assert any(name.startswith("01_text_0.060_") for name in names)
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
        assert any("_text_" in name and not flag for name, flag in by_name.items())
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
        """Distinct text cutters produce one text file per cutter diameter."""
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

        text_files = [p.name for p in result.plt_paths if "_text_" in p.name]
        # Name shape: <plate>_text_<cutter>_<job_id>.plt (job_id has no '_').
        cutter_tags = {name.split("_")[2] for name in text_files}
        # complex_test_job exercises at least three distinct text cutters.
        assert len(cutter_tags) >= 3
        # Every tag is a 3-decimal inch string.
        for tag in cutter_tags:
            major, _, minor = tag.partition(".")
            assert major.isdigit() and len(minor) == 3

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
