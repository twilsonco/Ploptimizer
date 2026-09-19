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
        # no inventory snapping) plus borders.
        names = sorted(p.name for p in exported_paths)
        assert any(name.endswith("_text_0.060.plt") for name in names)
        assert any(name.endswith("_borders-holes_0.015.plt") for name in names)
        # The combined PLT is never written to disk.
        assert not any("_all" in name for name in names)
        # All files carry the job_id prefix.
        assert all(name.startswith("job123_") for name in names)

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
        """plots=True writes simple PDFs mirroring PLT names + *_all.pdf."""
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
        # One combined *_all.pdf per plate.
        assert any(name.endswith("_all.pdf") for name in pdf_names)
        # Combined content is exposed in memory, never written as PLT.
        assert result.combined_by_plate
        written_stems = {p.stem for p in result.plt_paths}
        assert not any(stem.endswith("_all") for stem in written_stems)

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

        # Borders exist for every label, so the structural file is written.
        assert any("borders-holes" in p.name for p in result.plt_paths)
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
        cutter_tags = {name.rsplit("_", 1)[-1].removesuffix(".plt") for name in text_files}
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
