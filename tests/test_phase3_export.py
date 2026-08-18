"""Tests for export_and_optimize_phase3 function."""

import pytest
from pathlib import Path

from plt_optimizer.generate.vectorize import export_and_optimize_phase3
from plt_optimizer.generate.schema import parse_yaml
from plt_optimizer.generate.resolution import resolve_job_spec


class TestExportAndOptimizePhase3:
    """Tests for the Phase 3 export pipeline."""

    def test_export_phase3_single_label(self, tmp_path: Path) -> None:
        """Test Phase 3 export with single label."""
        # Parse and resolve test job
        job = parse_yaml("examples/test123_spec.yaml")
        resolved_labels = resolve_job_spec(job)

        # Export using Phase 3 pipeline
        exported_paths = export_and_optimize_phase3(
            resolved_labels, output_dir=tmp_path, optimize=False, separate_layers=False
        )

        # Verify output
        assert len(exported_paths) > 0
        for path in exported_paths:
            assert path.exists()
            content = path.read_text()
            assert content.startswith("IN;DF;PS0;")
            assert content.endswith("%")
            assert len(content) > 50  # Has actual content

    def test_export_phase3_separate_layers(self, tmp_path: Path) -> None:
        """Test Phase 3 export with separate layer files."""
        # Parse and resolve test job
        job = parse_yaml("examples/test123_spec.yaml")
        resolved_labels = resolve_job_spec(job)

        # Export using Phase 3 pipeline with separate layers
        exported_paths = export_and_optimize_phase3(
            resolved_labels, output_dir=tmp_path, optimize=False, separate_layers=True
        )

        # Verify we got at least one layer file
        assert len(exported_paths) >= 1

        # Check for expected layer files
        file_names = [p.name for p in exported_paths]
        layer_suffixes = ["_text", "_borders", "_holes"]
        found_layers = [s for s in layer_suffixes if any(s in name for name in file_names)]

        # Should have at least some layers present (borders and/or text)
        assert len(found_layers) >= 1

        # All should be valid PLT files
        for path in exported_paths:
            assert path.exists()
            content = path.read_text()
            assert content.startswith("IN;DF;PS0;")
            assert content.endswith("%")

    def test_export_phase3_file_organization(self, tmp_path: Path) -> None:
        """Test that Phase 3 output files are organized correctly."""
        # Parse and resolve test job
        job = parse_yaml("examples/test123_spec.yaml")
        resolved_labels = resolve_job_spec(job)

        # Export using Phase 3 pipeline
        exported_paths = export_and_optimize_phase3(
            resolved_labels,
            output_dir=tmp_path,
            optimize=False,
            separate_layers=True,
        )

        # All files should be in the output directory
        for path in exported_paths:
            assert path.parent == tmp_path

        # Check that files have proper naming
        for path in exported_paths:
            assert path.name.endswith(".plt")
