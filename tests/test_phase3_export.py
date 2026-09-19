"""Tests for the per-cutter Phase 3 export pipeline."""

from pathlib import Path

from plt_optimizer.generate.resolution import resolve_job_spec
from plt_optimizer.generate.schema import parse_yaml
from plt_optimizer.generate.vectorize import (
    PerCutterExport,
    export_and_optimize_phase3,
    export_per_cutter_plts,
)


class TestExportAndOptimizePhase3:
    """Tests for the Phase 3 per-cutter export pipeline."""

    def test_export_phase3_per_cutter_files(self, tmp_path: Path) -> None:
        """Phase 3 export writes per-cutter PLT files under plt/."""
        job = parse_yaml("examples/test123_spec.yaml")
        resolved_labels = resolve_job_spec(job)

        exported_paths = export_and_optimize_phase3(
            resolved_labels,
            output_dir=tmp_path,
            optimize=False,
            job_id="job123",
        )

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
        job = parse_yaml("examples/test123_spec.yaml")
        resolved_labels = resolve_job_spec(job)

        export_and_optimize_phase3(
            resolved_labels,
            output_dir=tmp_path,
            optimize=False,
        )

        assert not (tmp_path / "pdf").exists() or not list((tmp_path / "pdf").iterdir())

    def test_export_per_cutter_plots(self, tmp_path: Path) -> None:
        """plots=True writes simple PDFs mirroring PLT names + all_*.pdf."""
        job = parse_yaml("examples/test123_spec.yaml")
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
        job = parse_yaml("examples/test123_spec.yaml")
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
        job = parse_yaml("examples/test123_spec.yaml")
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

    def test_export_per_cutter_skips_empty_groups(self, tmp_path: Path) -> None:
        """A job without holes still gets a borders file; no empty text files."""
        job = parse_yaml("examples/test123_spec.yaml")
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
        job = parse_yaml("examples/complex_test_job.yaml")
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
        job = parse_yaml("examples/test123_spec.yaml")
        resolved_labels = resolve_job_spec(job)

        exported_paths = export_and_optimize_phase3(
            resolved_labels,
            output_dir=tmp_path,
            optimize=False,
            job_id="org",
        )

        for path in exported_paths:
            assert path.parent == tmp_path / "plt"
            assert path.name.endswith(".plt")
