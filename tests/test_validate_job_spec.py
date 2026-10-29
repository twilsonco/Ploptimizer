"""Unit tests for ``scripts/validate_job_spec.py``.

The script is a script (not an importable package module), so it is loaded by
path with ``importlib`` (mirroring ``tests/test_profile_generate.py``; its own
``sys.path`` bootstrap makes the ``plt_optimizer`` imports inside it work).

Covers the pre-flight checks the parser cannot do (replacement-file existence,
cutter-inventory shape) and the exit-code contract. ``parse_yaml``'s own rules
are pinned in ``tests/test_schema.py`` / ``tests/test_job_config.py``.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def script() -> Any:
    """Load scripts/validate_job_spec.py as a module."""
    path = REPO_ROOT / "scripts" / "validate_job_spec.py"
    spec = importlib.util.spec_from_file_location("validate_job_spec", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


#: A complete, config-free label job (all required fields declared inline).
#: ``plate_width``/``plate_height`` are config-only keys (never spec fields),
#: so the job declares explicit plates to stay config-independent.
_GOOD_SPEC = """\
job:
  job_name: Script Validation Job
  max_h_compress: 0.5
  hole_margin: 0.0625
  min_hole_margin: 0.05
  hole_text_collision_distance: 0.1
  plates:
    - id: p1
      width: 24.0
      height: 12.0
  labels:
    - id: l1
      width: 3.0
      height: 1.0
      content:
        - text: Hi
"""


def _write_spec(directory: Path, text: str, name: str = "spec.yaml") -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def _label_with_replacement(target: str) -> str:
    """A valid label job whose label reads ``target`` as its data file."""
    return _GOOD_SPEC.replace(
        "    - id: l1\n",
        f"    - id: l1\n      replacement_text_file: {target}\n",
    )


class TestOptPath:
    """The empty-string opt-out sentinel (Path('') == Path('.') is truthy)."""

    def test_empty_string_opts_out(self, script: Any) -> None:
        assert script._opt_path("") is None
        assert script._opt_path(None) is None

    def test_value_becomes_path(self, script: Any) -> None:
        assert script._opt_path("a/b.json") == Path("a/b.json")


class TestCollectReplacementFiles:
    """Every tier that can carry a data-file reference is discovered."""

    def test_label_tier(self, script: Any, tmp_path: Path) -> None:
        (tmp_path / "data.txt").write_text("A\n", encoding="utf-8")
        job = script.parse_yaml(_write_spec(tmp_path, _label_with_replacement("data.txt")))
        assert script.collect_replacement_files(job) == [("labels[0]", "data.txt")]

    def test_job_tier(self, script: Any) -> None:
        from plt_optimizer.generate.schema import JobSpec

        job = JobSpec.model_validate(
            {
                "job_name": "J",
                "replacement_text_file": "data.txt",
                "max_h_compress": 0.5,
                "hole_margin": 0.06,
                "min_hole_margin": 0.05,
                "hole_text_collision_distance": 0.1,
                "text_height": 0.2,
                "content": [{"text": "Hi"}],
                "width": 3.0,
                "height": 1.0,
            }
        )
        assert script.collect_replacement_files(job) == [("job", "data.txt")]

    def test_plate_tier(self, script: Any) -> None:
        from plt_optimizer.generate.schema import JobSpec

        job = JobSpec.model_validate(
            {
                "job_name": "J",
                "max_h_compress": 0.5,
                "hole_margin": 0.06,
                "min_hole_margin": 0.05,
                "hole_text_collision_distance": 0.1,
                "plates": [{"id": "p1", "width": 24.0, "height": 12.0}],
                "labels": [
                    {
                        "id": "l1",
                        "width": 3.0,
                        "height": 1.0,
                        "content": [{"text": "Hi"}],
                    }
                ],
            }
        )
        assert script.collect_replacement_files(job) == []
        assert job.plates is not None


class TestCheckReplacementFiles:
    """Existence pre-flight, resolved relative to the spec directory."""

    def test_existing_file_is_silent(self, script: Any, tmp_path: Path) -> None:
        (tmp_path / "data.txt").write_text("A\nB\n", encoding="utf-8")
        job = script.parse_yaml(_write_spec(tmp_path, _label_with_replacement("data.txt")))
        assert script.check_replacement_files(job, tmp_path / "spec.yaml") == []

    def test_missing_file_reports_resolved_path(self, script: Any, tmp_path: Path) -> None:
        job = script.parse_yaml(_write_spec(tmp_path, _label_with_replacement("absent.txt")))
        findings = script.check_replacement_files(job, tmp_path / "spec.yaml")
        assert len(findings) == 1
        assert findings[0].severity == script.SEVERITY_ERROR
        assert "labels[0]" in findings[0].message
        assert str(tmp_path / "absent.txt") in findings[0].message

    def test_absolute_reference_resolves_directly(self, script: Any, tmp_path: Path) -> None:
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "data.txt").write_text("A\n", encoding="utf-8")
        spec_dir = tmp_path / "specdir"
        spec_dir.mkdir()
        spec = _write_spec(
            spec_dir, _label_with_replacement(str(outside / "data.txt")), name="abs.yaml"
        )
        job = script.parse_yaml(spec)
        assert script.check_replacement_files(job, spec) == []

    def test_nested_relative_reference(self, script: Any, tmp_path: Path) -> None:
        data = tmp_path / "data"
        data.mkdir()
        (data / "list.txt").write_text("A\n", encoding="utf-8")
        job = script.parse_yaml(_write_spec(tmp_path, _label_with_replacement("data/list.txt")))
        assert script.check_replacement_files(job, tmp_path / "spec.yaml") == []


class TestCheckTools:
    """The two-key cutter inventory contract."""

    def test_none_skips(self, script: Any) -> None:
        assert script.check_tools(None) == []

    def test_valid_inventory_is_silent(self, script: Any, tmp_path: Path) -> None:
        path = tmp_path / "tools.json"
        path.write_text(
            json.dumps({"available_cutters": [0.03, 0.06], "boundary_hole_cutter_size": 0.015}),
            encoding="utf-8",
        )
        assert script.check_tools(path) == []

    def test_absent_inventory_warns(self, script: Any, tmp_path: Path) -> None:
        findings = script.check_tools(tmp_path / "absent.json")
        assert len(findings) == 1
        assert findings[0].severity == script.SEVERITY_WARNING

    def test_malformed_inventory_errors(self, script: Any, tmp_path: Path) -> None:
        path = tmp_path / "tools.json"
        path.write_text("{nope", encoding="utf-8")
        assert script.check_tools(path)[0].severity == script.SEVERITY_ERROR

    @pytest.mark.parametrize(
        "payload, key",
        [
            ({"available_cutters": "0.03"}, "available_cutters"),
            ({"available_cutters": [0.03, "wide"]}, "available_cutters"),
            ({"available_cutters": [0.03, -1]}, "available_cutters"),
            ({"available_cutters": [0.03, True]}, "available_cutters"),
            ({"boundary_hole_cutter_size": "small"}, "boundary_hole_cutter_size"),
            ({"boundary_hole_cutter_size": 0}, "boundary_hole_cutter_size"),
        ],
    )
    def test_bad_key_types_error(self, script: Any, tmp_path: Path, payload: Any, key: str) -> None:
        path = tmp_path / "tools.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        findings = script.check_tools(path)
        assert len(findings) == 1
        assert key in findings[0].message

    def test_non_object_inventory_errors(self, script: Any, tmp_path: Path) -> None:
        path = tmp_path / "tools.json"
        path.write_text("[1, 2]", encoding="utf-8")
        assert "JSON object" in script.check_tools(path)[0].message

    def test_repo_tools_json_is_clean(self, script: Any) -> None:
        """The real cutter inventory passes (config-value immunity)."""
        assert script.check_tools(REPO_ROOT / "tools.json") == []


class TestValidateSpec:
    """parse_yaml integration and the file_error classification."""

    def test_good_spec_is_clean(self, script: Any, tmp_path: Path) -> None:
        report = script.validate_spec(_write_spec(tmp_path, _GOOD_SPEC), None)
        assert report.file_error is False
        assert report.findings == []
        assert report.job is not None

    def test_missing_spec_is_file_error(self, script: Any, tmp_path: Path) -> None:
        report = script.validate_spec(tmp_path / "absent.yaml", None)
        assert report.file_error is True
        assert report.job is None

    def test_malformed_yaml_is_file_error(self, script: Any, tmp_path: Path) -> None:
        spec = _write_spec(tmp_path, "job:\n  job_name: [unclosed\n")
        assert script.validate_spec(spec, None).file_error is True

    def test_unknown_key_is_not_file_error(self, script: Any, tmp_path: Path) -> None:
        """A schema failure is exit 1 (the spec exists and parsed)."""
        # A key the spec model forbids: the file parses as YAML, so this is a
        # validation failure (exit 1), not a file-level one (exit 2).
        spec = _write_spec(
            tmp_path, _GOOD_SPEC.replace("      content:", "      tex_height: 0.5\n      content:")
        )
        report = script.validate_spec(spec, None)
        assert report.file_error is False
        assert report.job is None
        assert report.findings[0].severity == script.SEVERITY_ERROR

    def test_bad_job_config_is_not_file_error(self, script: Any, tmp_path: Path) -> None:
        config = tmp_path / "job-config.json"
        config.write_text(json.dumps({"max_h_compress": 2.0}), encoding="utf-8")
        report = script.validate_spec(_write_spec(tmp_path, _GOOD_SPEC), config)
        assert report.file_error is False
        assert "job config" in report.findings[0].message

    def test_config_defaults_satisfy_required_fields(self, script: Any, tmp_path: Path) -> None:
        """A config supplying the required fields lets a lean spec through."""
        config = tmp_path / "job-config.json"
        config.write_text(
            json.dumps(
                {
                    "max_h_compress": 0.5,
                    "hole_margin": 0.0625,
                    "min_hole_margin": 0.05,
                    "hole_text_collision_distance": 0.1,
                    "plate_width": 24.0,
                    "plate_height": 12.0,
                }
            ),
            encoding="utf-8",
        )
        lean = "job:\n  job_name: Lean\n  labels:\n    - id: l1\n      width: 3.0\n      height: 1.0\n      content:\n        - text: Hi\n"
        report = script.validate_spec(_write_spec(tmp_path, lean), config)
        assert report.job is not None
        assert report.findings == []


class TestExitCodes:
    """run() maps findings to 0 / 1 / 2."""

    def test_valid_spec_exits_zero(self, script: Any, tmp_path: Path, capsys: Any) -> None:
        spec = _write_spec(tmp_path, _GOOD_SPEC)
        assert script.run([str(spec), "--job-config", "", "--tools", ""]) == script.EXIT_OK
        out = capsys.readouterr().out
        assert "OK" in out
        assert "1 label(s)" in out

    def test_missing_spec_exits_two(self, script: Any, tmp_path: Path, capsys: Any) -> None:
        assert script.run([str(tmp_path / "absent.yaml")]) == script.EXIT_FILE_ERROR
        capsys.readouterr()

    def test_schema_error_exits_one(self, script: Any, tmp_path: Path, capsys: Any) -> None:
        spec = _write_spec(tmp_path, "spec:\n  job_name: T\n")
        assert script.run([str(spec)]) == script.EXIT_ERRORS
        capsys.readouterr()

    def test_missing_data_file_exits_one(self, script: Any, tmp_path: Path, capsys: Any) -> None:
        spec = _write_spec(tmp_path, _label_with_replacement("absent.txt"))
        assert script.run([str(spec), "--job-config", "", "--tools", ""]) == script.EXIT_ERRORS
        assert "replacement_text_file" in capsys.readouterr().out

    def test_json_report_shape(self, script: Any, tmp_path: Path, capsys: Any) -> None:
        spec = _write_spec(tmp_path, _GOOD_SPEC)
        assert script.run([str(spec), "--json", "--job-config", "", "--tools", ""]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["job_name"] == "Script Validation Job"
        assert payload["label_count"] == 1
        assert payload["plate_count"] == 1

    def test_errors_sort_before_warnings(self, script: Any, tmp_path: Path, capsys: Any) -> None:
        """Findings are ordered so the blocking problem prints first."""
        spec = _write_spec(tmp_path, _label_with_replacement("absent.txt"))
        tools = tmp_path / "tools.json"
        tools.write_text("[not an object]", encoding="utf-8")
        code = script.run([str(spec), "--job-config", "", "--tools", str(tools)])
        assert code == script.EXIT_ERRORS
        lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("ERROR")]
        assert lines and "replacement_text_file" in lines[0]

    def test_repo_example_specs_validate(self, script: Any, capsys: Any) -> None:
        """Every shipped example spec passes the gate (no stale examples)."""
        examples = sorted((REPO_ROOT / "examples" / "job_specs").glob("*.yaml"))
        assert examples
        failures = []
        for spec in examples:
            report = script.validate_spec(spec, REPO_ROOT / "job-config.json")
            problems = [f.message for f in report.findings]
            if report.file_error or problems:
                failures.append(f"{spec.name}: {problems}")
        assert not failures, "example specs failed validation:\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# Location independence (the validating-jobs skill mirrors this file)
# ---------------------------------------------------------------------------

#: The Hermes skill's mirrored copy of this script (generator-managed).
MIRROR = (
    REPO_ROOT
    / "docs"
    / "skills"
    / "engraving-jobs"
    / "validating-jobs"
    / "scripts"
    / "validate_job_spec.py"
)


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
        script.bootstrap_sys_path(REPO_ROOT / "scripts" / "validate_job_spec.py")
        assert sys.path == before

    def test_walks_up_to_checkout_root(self, script: Any, tmp_path: Path, monkeypatch: Any) -> None:
        """A mirrored copy finds the checkout above it and prepends that root."""
        nested = _fake_checkout(tmp_path / "checkout")
        monkeypatch.setattr(script.importlib.util, "find_spec", lambda _n: None)
        root = str(nested.parents[4])
        monkeypatch.setattr(sys, "path", [p for p in sys.path if p != root])
        script.bootstrap_sys_path(nested / "validate_job_spec.py")
        assert sys.path[0] == root

    def test_missing_checkout_exits_2_with_hints(
        self, script: Any, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        """No package and no checkout: exit 2, naming both fixes."""
        isolated = tmp_path / "a" / "b"
        isolated.mkdir(parents=True)
        monkeypatch.setattr(script.importlib.util, "find_spec", lambda _n: None)
        with pytest.raises(SystemExit) as excinfo:
            script.bootstrap_sys_path(isolated / "validate_job_spec.py")
        assert excinfo.value.code == script.EXIT_FILE_ERROR
        stderr = capsys.readouterr().err
        assert "uv run python scripts/validate_job_spec.py" in stderr
        assert "pip install plt-optimizer" in stderr


class TestLocationNeutralDefaults:
    """Defaults are cwd-relative, matching `generate --job-config`."""

    def test_defaults_are_relative(self, script: Any) -> None:
        parser = script.build_parser()
        assert parser.get_default("job_config") == "job-config.json"
        assert parser.get_default("tools") == "tools.json"

    def test_help_lists_no_repo_path(self, script: Any) -> None:
        """The usage text must not leak a machine-specific checkout path."""
        text = " ".join(script.build_parser().format_help().split())
        assert "job-config.json in the CWD" in text
        assert "tools.json in the CWD" in text


class TestMirroredCopyRuns:
    """The skill's mirrored copy is a runnable artifact, not a listing."""

    def test_mirror_runs_from_foreign_cwd(self, tmp_path: Path) -> None:
        """Run the mirror with a cwd outside the repo and no config layer."""
        if not MIRROR.exists():  # pragma: no cover - guarded by the skills suite
            pytest.skip("skill mirror not generated yet")
        spec = _write_spec(tmp_path, _GOOD_SPEC)
        result = subprocess.run(
            [sys.executable, str(MIRROR), str(spec), "--job-config", "", "--tools", ""],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "job spec is valid" in result.stdout

    def test_mirror_is_byte_identical_to_source(self) -> None:
        """The mirror contract: no path rewrites, so drift is a copy-paste bug."""
        if not MIRROR.exists():  # pragma: no cover - guarded by the skills suite
            pytest.skip("skill mirror not generated yet")
        source = REPO_ROOT / "scripts" / "validate_job_spec.py"
        assert MIRROR.read_bytes() == source.read_bytes()
