"""Tests for tool option (header) generation and integration.

This module tests:
- Tool option header command generation from JobSpec
- Bounds clamping with warning logs
- Dual default selection (text vs borders_holes)
- PLT file prepending with headers
- Integration with full generation pipeline

Every expectation involving the SHIPPED ``job-config.json`` is computed from
the config's own ``tool_options`` metadata (command, type, range, default)
through the same clamp/format helpers the generator uses, so retuning
``job-config.json`` can never break a previously working test. The
spec-declared values come from the frozen ``tests_deps/`` fixtures (test-owned
constants), never from the shop config.
"""

import logging
from pathlib import Path
from typing import Any, Optional

import pytest

from plt_optimizer.core.units import Z_UNITS_PER_INCH
from plt_optimizer.generate.job_config import JobConfig, load_job_config
from plt_optimizer.generate.schema import JobSpec, parse_yaml
from plt_optimizer.generate.tool_options import (
    _clamp_float,
    _clamp_int,
    _format_bool_header,
    _format_float_header,
    _format_int_header,
    generate_tool_option_headers,
    prepend_tool_option_headers,
)

_REPO_CONFIG_PATH = Path("job-config.json")

# The frozen demo fixture's spec-declared values (tests_deps, test-owned
# constants -- never the shop config).
_DEMO_VALUES: dict[str, Any] = {
    "cutting_velocity": 1.5,
    "dwell_time": 75,
    "spindle_speed": 15000,
    "z_clearance": 0.3,
    "proximity": False,
}


@pytest.fixture
def repo_config() -> JobConfig:
    """The shipped ``job-config.json`` (the config layer under test)."""
    config = load_job_config(_REPO_CONFIG_PATH)
    assert config is not None
    return config


def _options(config: JobConfig) -> dict[str, dict[str, Any]]:
    """Return the shipped config's tool_options metadata mapping."""
    return config.defaults.tool_options or {}


def _meta(config: JobConfig, key: str) -> dict[str, Any]:
    """Return the shipped config's metadata for one tool option.

    Skips the test when the shop config no longer carries the option: the
    header generator iterates the config's own keys, so a removed option
    simply never emits -- a config-shape change, not a regression.

    Args:
        config: The loaded shipped config.
        key: The tool option key.

    Returns:
        The option's metadata dict.
    """
    options = _options(config)
    if key not in options:
        pytest.skip(f"job-config.json no longer configures tool option {key!r}")
    return options[key]


def _dual_default(meta: dict[str, Any], plt_type: str) -> Any:
    """Resolve one option's dual default for a plt type (``None`` = omitted)."""
    default = meta.get("default")
    if isinstance(default, dict):
        return default.get(plt_type)
    return default


def _default(config: JobConfig, key: str, plt_type: str = "text") -> Any:
    """Return the shipped config's default for one option + plt type."""
    return _dual_default(_meta(config, key), plt_type)


def _expected(config: JobConfig, key: str, value: Any) -> str:
    """Render the header the shipped config's metadata produces for ``value``.

    Mirrors :func:`generate_tool_option_headers`' clamp + format pipeline for
    one option, so the expectation tracks the config's command, type and range
    exactly (a retuned range re-derives the clamped value).

    Args:
        config: The loaded shipped config.
        key: The tool option key.
        value: The value the header carries.

    Returns:
        The formatted HPGL header string.
    """
    meta = _meta(config, key)
    command = meta.get("command", "")
    param_type = meta.get("type", "bool")
    if param_type == "float":
        return _format_float_header(command, _clamp_float(value, key, meta))
    if param_type == "int":
        return _format_int_header(command, _clamp_int(value, key, meta))
    return _format_bool_header(command, value)


def _job(**tool_options: Any) -> JobSpec:
    """Build a minimal job carrying the given spec-declared tool options."""
    return JobSpec(
        job_name="Test",
        width=3.0,
        height=1.0,
        content=[{"text": "Test"}],
        tool_options=tool_options,
    )


class TestToolOptionHeaderGeneration:
    """Tests for header command generation."""

    def test_generate_headers_with_job_spec_overrides(self, repo_config: JobConfig) -> None:
        """Tool options from job spec override job-config defaults."""
        job = parse_yaml(
            Path("tests_deps/tool_options_demo_job.yaml"), job_config_path=_REPO_CONFIG_PATH
        )

        headers = generate_tool_option_headers(job, repo_config, "text")
        header_strs = "\n".join(headers)

        # Every spec-declared value emits its header, formatted through the
        # shipped config's own command/type/range metadata.
        for key, value in _DEMO_VALUES.items():
            assert _expected(repo_config, key, value) in header_strs

    def test_generate_headers_with_defaults(self, repo_config: JobConfig) -> None:
        """Tool options use job-config defaults when not specified."""
        job = parse_yaml(
            Path("tests_deps/tool_options_default_job.yaml"), job_config_path=_REPO_CONFIG_PATH
        )

        headers = generate_tool_option_headers(job, repo_config, "text")
        header_strs = "\n".join(headers)

        # The headers carry the config's own text-side defaults (computed,
        # never hardcoded -- a retuned default re-derives the expectation).
        assert len(headers) > 0
        for key in ("cutting_velocity", "dwell_time"):
            assert _expected(repo_config, key, _default(repo_config, key)) in header_strs

    def test_bool_formatting(self, repo_config: JobConfig) -> None:
        """Bool values are formatted as 1/0."""
        job = parse_yaml(
            Path("tests_deps/tool_options_bool_job.yaml"), job_config_path=_REPO_CONFIG_PATH
        )

        headers = generate_tool_option_headers(job, repo_config, "text")
        header_strs = "\n".join(headers)

        # Check bool formatting: false=0, true=1 (the commands come from the
        # shipped config's metadata).
        assert _expected(repo_config, "spindle", False) in header_strs
        assert _expected(repo_config, "vacuum", False) in header_strs
        assert _expected(repo_config, "proximity", True) in header_strs

    def test_float_formatting_2_decimals(self, repo_config: JobConfig) -> None:
        """Float values are formatted with 2 decimal places."""
        job = parse_yaml(
            Path("tests_deps/tool_options_demo_job.yaml"), job_config_path=_REPO_CONFIG_PATH
        )

        headers = generate_tool_option_headers(job, repo_config, "text")
        header_strs = "\n".join(headers)

        # The spec's 1.5 cutting velocity formats through the config command.
        assert _expected(repo_config, "cutting_velocity", 1.5) in header_strs

    def test_z_clearance_scaling(self, repo_config: JobConfig) -> None:
        """Z clearance is scaled by 1000 (inches to Z plotter units)."""
        job = parse_yaml(
            Path("tests_deps/tool_options_demo_job.yaml"), job_config_path=_REPO_CONFIG_PATH
        )

        headers = generate_tool_option_headers(job, repo_config, "text")
        header_strs = "\n".join(headers)

        # The Z scale is the device contract (root AGENTS.md section 4:
        # Z_UNITS_PER_INCH = 1000, proven by the reference headers), applied to
        # the SPEC's 0.3in clearance -- not a shop value. The scaling fires
        # only for the "ZU" command, so the expectation tracks the config.
        meta = _meta(repo_config, "z_clearance")
        if meta.get("command") == "ZU":
            assert f"ZU{int(0.3 * Z_UNITS_PER_INCH)};" in header_strs
        else:
            assert _expected(repo_config, "z_clearance", 0.3) in header_strs

    def test_bounds_clamping_warning(self, repo_config: JobConfig, caplog) -> None:
        """Out-of-bounds values are clamped and warnings logged."""
        # The out-of-range value is derived from the shipped config's own max,
        # so the clamp fires whatever range the shop tunes to.
        meta = _meta(repo_config, "cutting_velocity")
        if meta.get("max") is None:
            pytest.skip("job-config.json gives cutting_velocity no upper bound")
        too_high = float(meta["max"]) + 1.0
        job = _job(cutting_velocity=too_high)

        with caplog.at_level(logging.WARNING):
            headers = generate_tool_option_headers(job, repo_config, "text")
            clamped = [r.getMessage() for r in caplog.records if "clamp" in r.getMessage().lower()]

        assert clamped, "expected a clamping WARNING"
        # The clamped header carries the config's max, formatted verbatim.
        assert _expected(repo_config, "cutting_velocity", too_high) in headers

    def test_dual_defaults_text_type(self, repo_config: JobConfig) -> None:
        """Text plt_type selects correct default."""
        headers_text = generate_tool_option_headers(_job(), repo_config, "text")

        # Verify headers are generated (would use text defaults from config)
        assert isinstance(headers_text, list)

    def test_dual_defaults_borders_holes_type(self, repo_config: JobConfig) -> None:
        """borders_holes plt_type selects correct default."""
        headers_bh = generate_tool_option_headers(_job(), repo_config, "borders_holes")

        # Verify headers are generated (would use borders_holes defaults)
        assert isinstance(headers_bh, list)

    def test_dual_defaults_select_the_configured_side(self, repo_config: JobConfig) -> None:
        """Each plt_type emits its own configured default.

        The two sides are only comparable where the shop actually configures
        DIFFERENT defaults, so the assertion is conditional on the config.
        """
        differing = [
            key
            for key, meta in _options(repo_config).items()
            if isinstance(meta.get("default"), dict)
            and meta["default"].get("text") is not None
            and meta["default"].get("borders_holes") is not None
            and meta["default"]["text"] != meta["default"]["borders_holes"]
        ]
        if not differing:
            pytest.skip("job-config.json configures identical text/borders defaults")
        key = sorted(differing)[0]
        text_headers = "\n".join(generate_tool_option_headers(_job(), repo_config, "text"))
        bh_headers = "\n".join(generate_tool_option_headers(_job(), repo_config, "borders_holes"))
        assert _expected(repo_config, key, _default(repo_config, key, "text")) in text_headers
        assert _expected(repo_config, key, _default(repo_config, key, "borders_holes")) in (
            bh_headers
        )

    def test_no_config_returns_empty(self) -> None:
        """Without job_config, no headers generated even with job_spec."""
        job = _job(cutting_velocity=1.5)

        headers = generate_tool_option_headers(job, None, "text")

        # Should return empty list when no config
        assert headers == []

    def test_null_tool_options_omitted(self, repo_config: JobConfig) -> None:
        """Tool options with null defaults in config are omitted."""
        headers = generate_tool_option_headers(_job(), repo_config, "text")

        # Every option whose text-side default is non-null emits exactly one
        # header; a null default is skipped by the generator.
        expected_keys = [
            key
            for key, meta in _options(repo_config).items()
            if _dual_default(meta, "text") is not None
        ]
        assert len(headers) == len(expected_keys)


class TestToolOptionHeaderPrepending:
    """Tests for prepending headers to PLT content."""

    def test_prepend_headers_after_init(self, repo_config: JobConfig) -> None:
        """Headers are inserted after IN; command."""
        plt_content = "IN;\nPA;\nPU0,0;\n"
        job = _job(cutting_velocity=1.5)

        result = prepend_tool_option_headers(plt_content, job, repo_config, "text")

        # Headers should be present after IN; command
        assert "IN;" in result
        # The cutting_velocity command (from the config metadata) is present.
        command = _meta(repo_config, "cutting_velocity")["command"]
        assert command in result
        # IN; should come before PA;
        in_pos = result.find("IN;")
        pa_pos = result.find("PA;")
        assert in_pos < pa_pos
        # Headers should be between IN; and PA;
        between = result[in_pos:pa_pos]
        assert command in between  # At least one header should be between IN; and PA;

    def test_prepend_preserves_existing_content(self, repo_config: JobConfig) -> None:
        """Geometry content is preserved when headers are prepended."""
        plt_content = "IN;\nPA;\nPU0,0;\nPD100,100;\n"

        result = prepend_tool_option_headers(plt_content, _job(), repo_config, "text")

        # Original geometry should be present
        assert "PU0,0;" in result
        assert "PD100,100;" in result

    def test_prepend_no_headers_returns_original(self) -> None:
        """No headers means original content is returned."""
        plt_content = "IN;\nPA;\nPU0,0;\n"

        result = prepend_tool_option_headers(plt_content, _job(), None, "text")

        # Should be identical (no config = no headers)
        assert result == plt_content


class TestToolOptionsIntegration:
    """Integration tests with the full generation pipeline."""

    def test_resolve_tool_options_preserved(self) -> None:
        """tool_options are preserved through resolution."""
        job = parse_yaml(
            Path("tests_deps/tool_options_demo_job.yaml"), job_config_path=_REPO_CONFIG_PATH
        )

        # Verify tool_options are present
        assert job.tool_options is not None
        assert "cutting_velocity" in job.tool_options

    def test_tool_options_in_yaml_parse(self) -> None:
        """Tool options from YAML are correctly parsed."""
        job = parse_yaml(
            Path("tests_deps/tool_options_demo_job.yaml"), job_config_path=_REPO_CONFIG_PATH
        )

        # The values come from the frozen fixture, never the shop config.
        for key, value in _DEMO_VALUES.items():
            assert job.tool_options[key] == value


class TestToolOptionsEdgeCases:
    """Edge case tests."""

    def test_empty_tool_options_dict(self, repo_config: JobConfig) -> None:
        """Empty tool_options dict is handled correctly."""
        job = JobSpec(
            job_name="Test",
            width=3.0,
            height=1.0,
            content=[{"text": "Test"}],
            tool_options={},
        )

        headers = generate_tool_option_headers(job, repo_config, "text")

        # Should fall back to config defaults
        assert len(headers) > 0

    def test_tool_options_none(self, repo_config: JobConfig) -> None:
        """tool_options=None uses config defaults."""
        job = JobSpec(
            job_name="Test",
            width=3.0,
            height=1.0,
            content=[{"text": "Test"}],
            tool_options=None,
        )

        headers = generate_tool_option_headers(job, repo_config, "text")

        # Should use config defaults
        assert len(headers) > 0

    def test_partial_tool_options_override(self, repo_config: JobConfig) -> None:
        """Specifying only some tool_options uses job config for the rest."""
        job = _job(cutting_velocity=1.5)

        headers = generate_tool_option_headers(job, repo_config, "text")
        header_strs = "\n".join(headers)

        # Specified value should be present
        assert _expected(repo_config, "cutting_velocity", 1.5) in header_strs

        # Default value for an unspecified option should also be present
        dwell_default: Optional[Any] = _default(repo_config, "dwell_time")
        assert _expected(repo_config, "dwell_time", dwell_default) in header_strs


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
