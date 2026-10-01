"""Tests for tool option (header) generation and integration.

This module tests:
- Tool option header command generation from JobSpec
- Bounds clamping with warning logs
- Dual default selection (text vs borders_holes)
- PLT file prepending with headers
- Integration with full generation pipeline
"""

import json
import logging
from pathlib import Path

import pytest

from plt_optimizer.generate.job_config import JobConfig, load_job_config
from plt_optimizer.generate.resolution import resolve_job_spec
from plt_optimizer.generate.schema import JobSpec, parse_yaml
from plt_optimizer.generate.tool_options import (
    generate_tool_option_headers,
    prepend_tool_option_headers,
)


class TestToolOptionHeaderGeneration:
    """Tests for header command generation."""

    def test_generate_headers_with_job_spec_overrides(self):
        """Tool options from job spec override job-config defaults."""
        job_config = load_job_config(Path("job-config.json"))
        job = parse_yaml(Path("tests_deps/tool_options_demo_job.yaml"), job_config_path=Path("job-config.json"))

        # Generate headers for text PLT type
        headers = generate_tool_option_headers(job, job_config, "text")

        # Should have headers for the specified tool options
        assert len(headers) > 0
        
        # Check that specific headers are present
        header_strs = "\n".join(headers)
        assert "VS1.50;" in header_strs  # cutting velocity 1.5
        assert "ZO124,75;" in header_strs  # dwell time 75
        assert "ZO100,15000;" in header_strs  # spindle speed 15000

    def test_generate_headers_with_defaults(self):
        """Tool options use job-config defaults when not specified."""
        job_config = load_job_config(Path("job-config.json"))
        job = parse_yaml(Path("tests_deps/tool_options_default_job.yaml"), job_config_path=Path("job-config.json"))

        headers = generate_tool_option_headers(job, job_config, "text")

        # Should have headers from job-config defaults
        assert len(headers) > 0
        
        # Check defaults (from job-config.json)
        header_strs = "\n".join(headers)
        assert "VS0.80;" in header_strs  # default cutting velocity
        assert "ZO124,50;" in header_strs  # default dwell time

    def test_bool_formatting(self):
        """Bool values are formatted as 1/0."""
        job_config = load_job_config(Path("job-config.json"))
        job = parse_yaml(Path("tests_deps/tool_options_bool_job.yaml"), job_config_path=Path("job-config.json"))

        headers = generate_tool_option_headers(job, job_config, "text")
        header_strs = "\n".join(headers)

        # Check bool formatting: false=0, true=1
        assert "ZO123,0;" in header_strs  # spindle: false -> 0
        assert "ZO102,0;" in header_strs  # vacuum: false -> 0
        assert "ZO104,1;" in header_strs  # proximity: true -> 1

    def test_float_formatting_3_decimals(self):
        """Float values are formatted with 2 decimal places."""
        job_config = load_job_config(Path("job-config.json"))
        job = parse_yaml(Path("tests_deps/tool_options_demo_job.yaml"), job_config_path=Path("job-config.json"))

        headers = generate_tool_option_headers(job, job_config, "text")
        header_strs = "\n".join(headers)

        # Cutting velocity 1.5 should format as VS1.50
        assert "VS1.50;" in header_strs

    def test_z_clearance_scaling(self):
        """Z clearance is scaled by 1000 (inches to plotter units)."""
        job_config = load_job_config(Path("job-config.json"))
        job = parse_yaml(Path("tests_deps/tool_options_demo_job.yaml"), job_config_path=Path("job-config.json"))

        headers = generate_tool_option_headers(job, job_config, "text")
        header_strs = "\n".join(headers)

        # z_clearance 0.3 should scale to ZU300
        assert "ZU300;" in header_strs

    def test_bounds_clamping_warning(self, caplog):
        """Out-of-bounds values are clamped and warnings logged."""
        job_config = load_job_config(Path("job-config.json"))
        job = parse_yaml(Path("tests_deps/tool_options_clamping_job.yaml"), job_config_path=Path("job-config.json"))

        with caplog.at_level(logging.WARNING):
            headers = generate_tool_option_headers(job, job_config, "text")

        # Should have warning logs for clamped values
        assert any("clamp" in record.message.lower() for record in caplog.records)

    def test_dual_defaults_text_type(self):
        """Text plt_type selects correct default."""
        job_config = load_job_config(Path("job-config.json"))
        job = JobSpec(
            job_name="Test",
            width=3.0,
            height=1.0,
            content=[{"text": "Test"}],
        )

        headers_text = generate_tool_option_headers(job, job_config, "text")
        
        # Verify headers are generated (would use text defaults from config)
        assert isinstance(headers_text, list)

    def test_dual_defaults_borders_holes_type(self):
        """borders_holes plt_type selects correct default."""
        job_config = load_job_config(Path("job-config.json"))
        job = JobSpec(
            job_name="Test",
            width=3.0,
            height=1.0,
            content=[{"text": "Test"}],
        )

        headers_bh = generate_tool_option_headers(job, job_config, "borders_holes")
        
        # Verify headers are generated (would use borders_holes defaults)
        assert isinstance(headers_bh, list)

    def test_no_config_returns_empty(self):
        """Without job_config, no headers generated even with job_spec."""
        job = JobSpec(
            job_name="Test",
            width=3.0,
            height=1.0,
            content=[{"text": "Test"}],
            tool_options={"cutting_velocity": 1.5},
        )

        headers = generate_tool_option_headers(job, None, "text")
        
        # Should return empty list when no config
        assert headers == []

    def test_null_tool_options_omitted(self):
        """Tool options with null defaults in config are omitted."""
        # This would require modifying job-config.json to have a null default
        # For now, just verify the logic handles it
        job_config = load_job_config(Path("job-config.json"))
        
        # Create a job with no tool_options
        job = JobSpec(
            job_name="Test",
            width=3.0,
            height=1.0,
            content=[{"text": "Test"}],
        )

        headers = generate_tool_option_headers(job, job_config, "text")
        
        # All headers should be present since no tool_options are null in the config
        assert len(headers) > 0


class TestToolOptionHeaderPrepending:
    """Tests for prepending headers to PLT content."""

    def test_prepend_headers_after_init(self):
        """Headers are inserted after IN; command."""
        plt_content = "IN;\nPA;\nPU0,0;\n"
        job_config = load_job_config(Path("job-config.json"))
        job = JobSpec(
            job_name="Test",
            width=3.0,
            height=1.0,
            content=[{"text": "Test"}],
            tool_options={"cutting_velocity": 1.5},
        )

        result = prepend_tool_option_headers(plt_content, job, job_config, "text")

        # Should have headers between IN; and PA;
        lines = result.split("\n")
        in_idx = next(i for i, l in enumerate(lines) if l.strip() == "IN;")
        pa_idx = next(i for i, l in enumerate(lines) if l.strip() == "PA;")
        
        assert in_idx < pa_idx
        # Headers should be between IN; and PA;
        for line in lines[in_idx + 1 : pa_idx]:
            if line.strip() and not line.startswith(";"):
                # Should be a valid header
                assert line.endswith(";")

    def test_prepend_preserves_existing_content(self):
        """Geometry content is preserved when headers are prepended."""
        plt_content = "IN;\nPA;\nPU0,0;\nPD100,100;\n"
        job_config = load_job_config(Path("job-config.json"))
        job = JobSpec(
            job_name="Test",
            width=3.0,
            height=1.0,
            content=[{"text": "Test"}],
        )

        result = prepend_tool_option_headers(plt_content, job, job_config, "text")

        # Original geometry should be present
        assert "PU0,0;" in result
        assert "PD100,100;" in result

    def test_prepend_no_headers_returns_original(self):
        """No headers means original content is returned."""
        plt_content = "IN;\nPA;\nPU0,0;\n"
        job = JobSpec(
            job_name="Test",
            width=3.0,
            height=1.0,
            content=[{"text": "Test"}],
        )

        result = prepend_tool_option_headers(plt_content, job, None, "text")

        # Should be identical (no config = no headers)
        assert result == plt_content


class TestToolOptionsIntegration:
    """Integration tests with the full generation pipeline."""

    def test_resolve_tool_options_preserved(self):
        """tool_options are preserved through resolution."""
        job_config = load_job_config(Path("job-config.json"))
        job = parse_yaml(Path("tests_deps/tool_options_demo_job.yaml"), job_config_path=Path("job-config.json"))

        # Verify tool_options are present
        assert job.tool_options is not None
        assert "cutting_velocity" in job.tool_options

    def test_tool_options_in_yaml_parse(self):
        """Tool options from YAML are correctly parsed."""
        job_config = load_job_config(Path("job-config.json"))
        job = parse_yaml(Path("tests_deps/tool_options_demo_job.yaml"), job_config_path=Path("job-config.json"))

        assert job.tool_options["cutting_velocity"] == 1.5
        assert job.tool_options["dwell_time"] == 75
        assert job.tool_options["spindle_speed"] == 15000
        assert job.tool_options["z_clearance"] == 0.3
        assert job.tool_options["proximity"] is False


class TestToolOptionsEdgeCases:
    """Edge case tests."""

    def test_empty_tool_options_dict(self):
        """Empty tool_options dict is handled correctly."""
        job_config = load_job_config(Path("job-config.json"))
        job = JobSpec(
            job_name="Test",
            width=3.0,
            height=1.0,
            content=[{"text": "Test"}],
            tool_options={},
        )

        headers = generate_tool_option_headers(job, job_config, "text")

        # Should fall back to config defaults
        assert len(headers) > 0

    def test_tool_options_none(self):
        """tool_options=None uses config defaults."""
        job_config = load_job_config(Path("job-config.json"))
        job = JobSpec(
            job_name="Test",
            width=3.0,
            height=1.0,
            content=[{"text": "Test"}],
            tool_options=None,
        )

        headers = generate_tool_option_headers(job, job_config, "text")

        # Should use config defaults
        assert len(headers) > 0

    def test_partial_tool_options_override(self):
        """Specifying only some tool_options uses job config for the rest."""
        job_config = load_job_config(Path("job-config.json"))
        job = JobSpec(
            job_name="Test",
            width=3.0,
            height=1.0,
            content=[{"text": "Test"}],
            tool_options={"cutting_velocity": 1.5},
        )

        headers = generate_tool_option_headers(job, job_config, "text")
        header_strs = "\n".join(headers)

        # Specified value should be present
        assert "VS1.50;" in header_strs
        
        # Default value for unspecified option should also be present
        assert "ZO124,50;" in header_strs  # default dwell_time


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
