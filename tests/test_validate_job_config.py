"""Unit tests for ``scripts/validate_job_config.py``.

The script is a script (not an importable package module), so it is loaded by
path with ``importlib`` (mirroring ``tests/test_profile_generate.py``; its own
``sys.path`` bootstrap makes the ``plt_optimizer`` imports inside it work).

Covers the advisory checks and the exit-code contract; the structural/semantic
gate itself is ``JobDefaults``, tested in ``tests/test_job_config.py``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def script() -> Any:
    """Load scripts/validate_job_config.py as a module."""
    path = REPO_ROOT / "scripts" / "validate_job_config.py"
    spec = importlib.util.spec_from_file_location("validate_job_config", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(directory: Path, data: Any, name: str = "job-config.json") -> Path:
    path = directory / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


#: A config supplying every required-when-unconfigured field and a plate size.
_COMPLETE: dict[str, Any] = {
    "max_h_compress": 0.5,
    "hole_margin": 0.0625,
    "min_hole_margin": 0.05,
    "hole_text_collision_distance": 0.1,
    "plate_width": 24.0,
    "plate_height": 12.0,
}


def _messages(findings: list[Any]) -> list[str]:
    return [f.message for f in findings]


class TestCheckRequiredFields:
    """Advisory warnings for the required-when-unconfigured fields."""

    def test_complete_config_is_silent(self, script: Any) -> None:
        from plt_optimizer.generate.job_config import JobDefaults

        assert script.check_required_fields(JobDefaults(**_COMPLETE)) == []

    def test_each_missing_field_warns(self, script: Any) -> None:
        from plt_optimizer.generate.job_config import (
            REQUIRED_WHEN_UNCONFIGURED,
            JobDefaults,
        )

        findings = script.check_required_fields(JobDefaults())
        assert len(findings) == len(REQUIRED_WHEN_UNCONFIGURED)
        assert all(f.severity == script.SEVERITY_WARNING for f in findings)
        for name in REQUIRED_WHEN_UNCONFIGURED:
            assert any(name in message for message in _messages(findings)), name


class TestCheckPlateSize:
    """Advisory warning for a missing default plate size."""

    @pytest.mark.parametrize("missing", ["plate_width", "plate_height"])
    def test_half_a_plate_size_warns(self, script: Any, missing: str) -> None:
        from plt_optimizer.generate.job_config import JobDefaults

        data = dict(_COMPLETE)
        del data[missing]
        findings = script.check_plate_size(JobDefaults(**data))
        assert len(findings) == 1
        assert missing in findings[0].message

    def test_full_plate_size_is_silent(self, script: Any) -> None:
        from plt_optimizer.generate.job_config import JobDefaults

        assert script.check_plate_size(JobDefaults(**_COMPLETE)) == []


class TestCheckToolOptionsAdvisory:
    """Advisory warnings for the docs-metadata keys on tool options."""

    def test_complete_entry_is_silent(self, script: Any) -> None:
        from plt_optimizer.generate.job_config import JobDefaults

        entry = {
            "name": "cutting velocity",
            "description": "Speed while cutting.",
            "units": "in/sec",
            "command": "VS",
            "type": "float",
            "default": {"text": 1.5, "borders_holes": 0.8},
            "min": 0.05,
            "max": 3.0,
        }
        defaults = JobDefaults(tool_options={"cutting_velocity": entry})
        assert script.check_tool_options_advisory(defaults) == []

    def test_missing_docs_keys_warn(self, script: Any) -> None:
        from plt_optimizer.generate.job_config import JobDefaults

        entry = {
            "command": "VS",
            "type": "float",
            "default": {"text": 1.5, "borders_holes": 0.8},
            "min": 0.05,
            "max": 3.0,
        }
        defaults = JobDefaults(tool_options={"cutting_velocity": entry})
        findings = script.check_tool_options_advisory(defaults)
        assert len(findings) == 1
        assert findings[0].severity == script.SEVERITY_WARNING
        for key in script.ADVISORY_OPTION_FIELDS:
            assert key in findings[0].message

    def test_no_tool_options_is_silent(self, script: Any) -> None:
        from plt_optimizer.generate.job_config import JobDefaults

        assert script.check_tool_options_advisory(JobDefaults()) == []


class TestCheckToolOptionsDefaults:
    """Advisory warnings for explicit-null dual defaults."""

    def test_null_layer_warns(self, script: Any) -> None:
        from plt_optimizer.generate.job_config import JobDefaults

        entry = {
            "command": "VS",
            "type": "float",
            "default": {"text": 1.5, "borders_holes": None},
            "min": 0.05,
            "max": 3.0,
        }
        findings = script.check_tool_options_defaults(
            JobDefaults(tool_options={"cutting_velocity": entry})
        )
        assert len(findings) == 1
        assert "borders_holes" in findings[0].message

    def test_populated_layers_silent(self, script: Any) -> None:
        from plt_optimizer.generate.job_config import JobDefaults

        entry = {
            "command": "VS",
            "type": "float",
            "default": {"text": 1.5, "borders_holes": 0.8},
            "min": 0.05,
            "max": 3.0,
        }
        assert (
            script.check_tool_options_defaults(
                JobDefaults(tool_options={"cutting_velocity": entry})
            )
            == []
        )


class TestValidateConfig:
    """The load gate + advisory layer, and the file_error classification."""

    def test_complete_config_clean(self, script: Any, tmp_path: Path) -> None:
        report = script.validate_config(_write(tmp_path, _COMPLETE))
        assert report.file_error is False
        assert report.config is not None
        assert report.findings == []

    def test_missing_file_is_file_error(self, script: Any, tmp_path: Path) -> None:
        report = script.validate_config(tmp_path / "absent.json")
        assert report.file_error is True
        assert report.config is None
        assert report.findings[0].severity == script.SEVERITY_ERROR

    def test_malformed_json_is_file_error(self, script: Any, tmp_path: Path) -> None:
        path = tmp_path / "job-config.json"
        path.write_text("{not json", encoding="utf-8")
        report = script.validate_config(path)
        assert report.file_error is True

    def test_model_failure_is_not_file_error(self, script: Any, tmp_path: Path) -> None:
        """Bad content is exit 1 (validation), not exit 2 (unreadable file)."""
        path = _write(tmp_path, {"max_h_compress": 2.0})
        report = script.validate_config(path)
        assert report.file_error is False
        assert report.config is None
        assert report.findings[0].severity == script.SEVERITY_ERROR

    def test_tool_options_crash_shape_rejected(self, script: Any, tmp_path: Path) -> None:
        """The entry-not-an-object shape fails at load, never reaching export."""
        data = dict(_COMPLETE)
        data["tool_options"] = {"cutting_velocity": 42}
        report = script.validate_config(_write(tmp_path, data))
        assert report.findings[0].severity == script.SEVERITY_ERROR
        assert "must be an object" in report.findings[0].message

    def test_advisory_warnings_do_not_set_file_error(self, script: Any, tmp_path: Path) -> None:
        report = script.validate_config(_write(tmp_path, {}))
        assert report.file_error is False
        assert report.config is not None
        assert report.findings
        assert all(f.severity == script.SEVERITY_WARNING for f in report.findings)


class TestExitCodes:
    """run() maps findings to 0 / 1 / 2, with --strict promoting warnings."""

    def test_clean_config_exits_zero(self, script: Any, tmp_path: Path, capsys: Any) -> None:
        path = _write(tmp_path, _COMPLETE)
        assert script.run(["--config", str(path)]) == script.EXIT_OK
        assert "OK" in capsys.readouterr().out

    def test_warnings_exit_zero_unless_strict(
        self, script: Any, tmp_path: Path, capsys: Any
    ) -> None:
        path = _write(tmp_path, {})
        assert script.run(["--config", str(path)]) == script.EXIT_OK
        capsys.readouterr()
        assert script.run(["--config", str(path), "--strict"]) == script.EXIT_ERRORS

    def test_errors_exit_one(self, script: Any, tmp_path: Path, capsys: Any) -> None:
        path = _write(tmp_path, {"max_h_compress": 2.0})
        assert script.run(["--config", str(path)]) == script.EXIT_ERRORS
        capsys.readouterr()

    def test_missing_file_exits_two(self, script: Any, tmp_path: Path, capsys: Any) -> None:
        assert script.run(["--config", str(tmp_path / "absent.json")]) == script.EXIT_FILE_ERROR
        capsys.readouterr()

    def test_json_report_shape(self, script: Any, tmp_path: Path, capsys: Any) -> None:
        path = _write(tmp_path, {})
        assert script.run(["--config", str(path), "--json"]) == script.EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["path"] == str(path)
        assert payload["errors"] == 0
        assert payload["warnings"] > 0
        assert payload["findings"][0]["severity"] == "warning"

    def test_json_report_on_error(self, script: Any, tmp_path: Path, capsys: Any) -> None:
        path = _write(tmp_path, {"max_h_compress": 2.0})
        assert script.run(["--config", str(path), "--json"]) == script.EXIT_ERRORS
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert payload["errors"] == 1

    def test_default_config_is_repo_config(self, script: Any, capsys: Any) -> None:
        """No --config validates the real shop config (must stay clean)."""
        assert script.run([]) == script.EXIT_OK
        assert "job-config.json" in capsys.readouterr().out


class TestCountErrors:
    """count_errors honours --strict."""

    def test_counts_only_errors_by_default(self, script: Any) -> None:
        findings = [
            script.Finding(script.SEVERITY_ERROR, "e"),
            script.Finding(script.SEVERITY_WARNING, "w"),
        ]
        assert script.count_errors(findings) == 1
        assert script.count_errors(findings, strict=True) == 2


# ---------------------------------------------------------------------------
# Location independence (the validating-jobs skill mirrors this file)
# ---------------------------------------------------------------------------


def _fake_checkout(root: Path) -> Path:
    """Build a minimal PLT-Optimizer checkout marker tree.

    Args:
        root: Directory to turn into the fake checkout root.

    Returns:
        A nested directory inside the fake checkout (a script's location).
    """
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'plt-optimizer'\n", encoding="utf-8")
    package = root / "plt_optimizer"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    nested = root / "docs" / "skills" / "engraving-jobs" / "validating-jobs" / "scripts"
    nested.mkdir(parents=True)
    return nested


class TestBootstrapSysPath:
    """The import bootstrap works from any directory, and fails helpfully."""

    def test_noop_when_pipeline_importable(self, script: Any, monkeypatch: Any) -> None:
        """An importable plt_optimizer short-circuits the walk."""
        monkeypatch.setattr(script.importlib.util, "find_spec", lambda _n: object())
        before = list(sys.path)
        script.bootstrap_sys_path(REPO_ROOT / "scripts" / "validate_job_config.py")
        assert sys.path == before

    def test_walks_up_to_checkout_root(self, script: Any, tmp_path: Path, monkeypatch: Any) -> None:
        """A mirrored copy finds the checkout above it and prepends that root."""
        nested = _fake_checkout(tmp_path / "checkout")
        monkeypatch.setattr(script.importlib.util, "find_spec", lambda _n: None)
        root = str(nested.parents[4])
        monkeypatch.setattr(sys, "path", [p for p in sys.path if p != root])
        script.bootstrap_sys_path(nested / "validate_job_config.py")
        assert sys.path[0] == root

    def test_missing_checkout_exits_2_with_hints(
        self, script: Any, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        """No package and no checkout: exit 2, naming both fixes."""
        isolated = tmp_path / "a" / "b"
        isolated.mkdir(parents=True)
        monkeypatch.setattr(script.importlib.util, "find_spec", lambda _n: None)
        with pytest.raises(SystemExit) as excinfo:
            script.bootstrap_sys_path(isolated / "validate_job_config.py")
        assert excinfo.value.code == script.EXIT_FILE_ERROR
        stderr = capsys.readouterr().err
        assert "uv run python scripts/validate_job_config.py" in stderr
        assert "pip install plt-optimizer" in stderr


class TestLocationNeutralDefaults:
    """Defaults are cwd-relative, matching `generate --job-config`."""

    def test_config_default_is_relative(self, script: Any) -> None:
        parser = script.build_parser()
        default = parser.get_default("config")
        assert default == Path("job-config.json")
        assert not default.is_absolute()

    def test_help_lists_no_repo_path(self, script: Any) -> None:
        """The usage text must not leak a machine-specific checkout path."""
        text = " ".join(script.build_parser().format_help().split())
        assert "job-config.json in the CWD" in text
