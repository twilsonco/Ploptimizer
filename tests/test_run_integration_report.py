"""Unit tests for the integration runner's generation-report phase.

``scripts/run_integration_test.py`` is a script (not an importable package
module), so it is loaded by path with ``importlib`` (mirroring
``tests/test_build_pipeline_pdf.py``).

These tests pin the contract the runner has with the ``generate`` CLI's report
surface: Phase 5 writes the same ``generation_report/`` pair a ``generate`` run
produces, with the same document schema, and a failed run leaves a
``status: "failed"`` report naming the phase that aborted.
"""

from __future__ import annotations

import importlib.abc
import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

from plt_optimizer.generate.generation_report import (
    REPORT_JSON_NAME,
    REPORT_SUMMARY_NAME,
    ReportCapture,
    build_report,
    report_dir_for,
)
from plt_optimizer.generate.job_config import JobConfig, apply_job_config_defaults, load_job_config
from plt_optimizer.generate.packing.layout import PackedLabel, PackedPlate
from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine
from plt_optimizer.generate.schema import ContentEntry, JobSpec, LabelSpec, PlateSpec
from plt_optimizer.generate.vectorize import PerCutterExport
from plt_optimizer.utils.stage_timing import StageTimings

REPO_ROOT = Path(__file__).resolve().parents[1]

# The single plate the fixture job declares, in inches.
PLATE_WIDTH = 24.0
PLATE_HEIGHT = 16.0


@pytest.fixture(scope="module")
def script() -> Any:
    """Load ``scripts/run_integration_test.py`` as a module."""
    path = REPO_ROOT / "scripts" / "run_integration_test.py"
    spec = importlib.util.spec_from_file_location("run_integration_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader = spec.loader
    assert isinstance(loader, importlib.abc.InspectLoader)
    loader.exec_module(module)
    return module


def _job(name: str = "Report Phase Job") -> JobSpec:
    """Build a minimal valid job spec declaring one plate and one label."""
    return JobSpec(
        job_name=name,
        plates=[PlateSpec(id="p1", width=PLATE_WIDTH, height=PLATE_HEIGHT)],
        labels=[
            LabelSpec(id="greeting", width=4.0, height=2.0, content=[ContentEntry(text="Hello")])
        ],
    )


def _label(label_id: str = "abc123") -> ResolvedLabel:
    """Build one resolved label for the report's summary counts."""
    line = ResolvedTextLine(
        text="Hello",
        nominal_text_height=0.3,
        toolpath_text_height=0.27,
        cutter_diameter=0.06,
        character_spacing=0.0,
        line_spacing=0.0,
        max_h_compress=0.0,
        text_h_alignment="center",
    )
    return ResolvedLabel(
        id=label_id,
        count=1,
        width=4.0,
        height=2.0,
        margin=0.1,
        h_margin=0.1,
        v_margin=0.1,
        holes=[],
        content=[line],
    )


def _export(tmp_path: Path, job: JobSpec, label_id: str = "abc123") -> PerCutterExport:
    """Build a small export result with one written PLT in ``tmp_path``."""
    plt_dir = tmp_path / "plt"
    plt_dir.mkdir(parents=True, exist_ok=True)
    plt_path = plt_dir / "60_txt_Report_Phase_Job.plt"
    plt_path.write_text("IN;PA;SP;\n", encoding="utf-8")

    label = _label(label_id)
    packed = PackedPlate(
        plate_id="p1",
        width=PLATE_WIDTH,
        height=PLATE_HEIGHT,
        labels=[
            PackedLabel(
                label_id=label_id,
                x=0.0,
                y=0.0,
                width=4.0,
                height=2.0,
                rotated=False,
                source_label=label,
            )
        ],
    )
    return PerCutterExport(
        output_dir=tmp_path,
        job_id="Report_Phase_Job",
        plt_paths=[plt_path],
        pdf_paths=[],
        default_pdf_paths=[],
        packed_plates=[packed],
        rendered_labels={},
    )


def _read_report(tmp_path: Path) -> dict[str, Any]:
    """Load the ``report.json`` the run wrote into ``tmp_path``."""
    payload: dict[str, Any] = json.loads(
        (report_dir_for(tmp_path) / REPORT_JSON_NAME).read_text(encoding="utf-8")
    )
    return payload


class TestResolveOutputDir:
    """The report must land next to the PLTs the run wrote."""

    def test_default_spec_uses_the_shared_tree(self, script: Any, tmp_path: Path) -> None:
        spec = tmp_path / "test123_spec.yaml"
        assert script._resolve_output_dir(spec, None) == (
            script.REPO_ROOT / "test_output" / "integration_test"
        )

    def test_override_gets_its_own_subdir(self, script: Any, tmp_path: Path) -> None:
        spec = tmp_path / "my_job.yaml"
        assert script._resolve_output_dir(spec, spec) == (
            script.REPO_ROOT / "test_output" / "integration_test" / "my_job"
        )

    def test_report_dir_is_a_sibling_of_plt_and_pdf(self, tmp_path: Path) -> None:
        assert report_dir_for(tmp_path).name == "generation_report"


class TestStageTimer:
    """Timing a block records one named stage in the collector."""

    def test_records_elapsed_seconds(self, script: Any) -> None:
        timings = StageTimings()
        with script.stage_timer("parse", logging.getLogger("test.timer"), timings=timings):
            pass
        assert timings.order == ["parse"]
        assert timings.seconds["parse"] >= 0.0

    def test_without_collector_is_a_plain_timer(self, script: Any) -> None:
        with script.stage_timer("layout", logging.getLogger("test.timer")):
            pass


class TestPhase5GenerationReport:
    """Phase 5 writes the generate pipeline's report pair."""

    def _run(self, script: Any, tmp_path: Path, **overrides: Any) -> dict[str, Any]:
        """Call Phase 5 with sensible defaults and return the JSON document."""
        kwargs: dict[str, Any] = {
            "job": _job(),
            "job_yaml": tmp_path / "job.yaml",
            "export_result": _export(tmp_path, _job()),
            "resolved_labels": [_label()],
            "inventory": [0.06, 0.125],
            "boundary_hole_cutter": 0.015,
            "job_config": None,
            "capture": ReportCapture(),
            "stage_timings": StageTimings(),
            "started_at": "2026-11-16T00:00:00+00:00",
            "duration_seconds": 3.5,
        }
        kwargs.update(overrides)
        script.phase_5_generation_report(**kwargs)
        return _read_report(tmp_path)

    def test_writes_both_artifacts_next_to_the_plts(
        self, script: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        summary_path = script.phase_5_generation_report(
            job=_job(),
            job_yaml=tmp_path / "job.yaml",
            export_result=_export(tmp_path, _job()),
            resolved_labels=[_label()],
            inventory=[0.06],
            boundary_hole_cutter=0.015,
            job_config=None,
            capture=ReportCapture(),
            stage_timings=StageTimings(),
            started_at="2026-11-16T00:00:00+00:00",
            duration_seconds=3.5,
        )
        report_dir = report_dir_for(tmp_path)
        assert summary_path == report_dir / REPORT_SUMMARY_NAME
        assert summary_path.is_file()
        assert (report_dir / REPORT_JSON_NAME).is_file()
        # The digest is echoed to the console too.
        assert "Generation report: Report Phase Job" in capsys.readouterr().out

    def test_document_matches_the_cli_schema(self, script: Any, tmp_path: Path) -> None:
        """The script's document has the CLI builder's keys, in the same order."""
        payload = self._run(script, tmp_path)
        reference = build_report(
            job_name="x",
            job_id="x",
            spec_path=Path("x.yaml"),
            output_dir=tmp_path,
            report_dir=report_dir_for(tmp_path),
            status="success",
        )
        assert list(payload) == list(reference)

    def test_job_identity_and_inventory(self, script: Any, tmp_path: Path) -> None:
        job = _job()
        payload = self._run(script, tmp_path, job=job, inventory=[0.06, 0.125])

        assert payload["schema_version"] == 1
        assert payload["job"]["job_name"] == "Report Phase Job"
        # The id is the CLI's sanitized form, i.e. the one in the file names.
        from plt_optimizer.cli.generate import _sanitize_job_id

        assert payload["job"]["job_id"] == _sanitize_job_id("Report Phase Job")
        assert payload["job"]["status"] == "success"
        assert payload["job"]["spec_path"] == str(tmp_path / "job.yaml")
        assert payload["job"]["output_dir"] == str(tmp_path)
        assert payload["job"]["duration_seconds"] == 3.5
        assert payload["job"]["tools"]["available_cutters"] == [0.06, 0.125]
        assert payload["job"]["tools"]["boundary_hole_cutter_size"] == 0.015

    def test_summary_counts_and_outputs(self, script: Any, tmp_path: Path) -> None:
        payload = self._run(script, tmp_path)
        assert payload["summary"]["plates"] == 1
        assert payload["summary"]["labels"] == 1
        assert payload["summary"]["lines"] == 1
        assert payload["summary"]["distinct_cutters"] == 1

        outputs = payload["outputs"]
        assert outputs["plt_files"] == [str(tmp_path / "plt" / "60_txt_Report_Phase_Job.plt")]
        assert outputs["plates"][0]["plate_id"] == "p1"
        assert outputs["plates"][0]["labels"][0]["label_id"] == "abc123"
        assert payload["labels"][0]["id"] == "abc123"

    def test_stage_timings_are_collected(self, script: Any, tmp_path: Path) -> None:
        timings = StageTimings()
        timings.record("resolve", 1.25)
        timings.record("layout", 0.5)
        payload = self._run(script, tmp_path, stage_timings=timings)
        assert payload["timings"] == {"resolve": 1.25, "layout": 0.5}

    def test_findings_come_from_the_capture(self, script: Any, tmp_path: Path) -> None:
        capture = ReportCapture()
        logger = logging.getLogger("plt_optimizer.generate.test")
        logger.addHandler(capture)
        try:
            logger.warning("Compressing text horizontally by 12.0% to fit content area")
            logger.error("Label abc123 does not fit in any plate")
        finally:
            capture.close()
        payload = self._run(script, tmp_path, capture=capture)

        assert payload["summary"]["warnings"] == 1
        assert payload["summary"]["errors"] == 1
        assert payload["warnings"][0]["message"].startswith("Compressing text")
        assert payload["errors"][0]["message"].startswith("Label abc123")

    def test_default_plots_are_reported(self, script: Any, tmp_path: Path) -> None:
        default_pdf = tmp_path / "pdf" / "60_txt_Report_Phase_Job_default.pdf"
        default_pdf.parent.mkdir(parents=True, exist_ok=True)
        default_pdf.write_bytes(b"%PDF-1.4\n")
        payload = self._run(script, tmp_path, default_pdf_paths=[default_pdf])
        assert payload["outputs"]["default_pdf_files"] == [str(default_pdf)]


class TestPhase5UsageWeight:
    """The single-job report weighs labels the way the CLI's report does.

    ``usage.weight`` is an estimated block: it reads each plate's density from
    the ``job_spec`` the caller passes, and a shop default reaches that spec the
    same way it reaches a real run - ``apply_job_config_defaults`` injects the
    configured job-level density onto every plate before layout. A phase that
    measures usage without the spec therefore reports "not estimated" for a job
    whose shop defaults do configure a density, which reads as a product bug (a
    16x24 label "weighs" nothing). The CLI passes the real pair
    (``cli/generate.py``); phase 5 must not be the caller that drops it.

    The density is derived from ``job-config.json``, never pinned (tests/
    AGENTS.md).
    """

    @staticmethod
    def _config() -> JobConfig:
        """The shop's real ``job-config.json`` (the config layer under test)."""
        config = load_job_config(REPO_ROOT / "job-config.json")
        assert config is not None
        assert config.defaults.material_weight_per_square_inch is not None, (
            "job-config.json is expected to configure a density"
        )
        return config

    def _configured_job(self, config: JobConfig) -> JobSpec:
        """The fixture job, with the shop's job-level defaults injected on it."""
        filled = apply_job_config_defaults(_job().model_dump(mode="json"), config)
        return JobSpec.model_validate(filled)

    def test_a_job_with_a_configured_density_is_weighed(self, script: Any, tmp_path: Path) -> None:
        """A shop default (which cascades onto every plate) yields a real weight."""
        config = self._config()
        density = float(config.defaults.material_weight_per_square_inch or 0.0)
        payload = self._phase5(
            script, tmp_path, job=self._configured_job(config), job_config=config
        )

        weight = payload["usage"]["weight"]
        assert weight["available"] is True
        # One 4.0 x 2.0 in label on the only plate, at the configured density.
        assert weight["total"]["label_instances"] == 1
        assert weight["total"]["label_sq_in"] == pytest.approx(8.0)
        assert weight["total"]["lb"] == pytest.approx(8.0 * density, abs=1e-6)

    def test_an_unconfigured_job_reports_the_estimate_as_unavailable(
        self, script: Any, tmp_path: Path
    ) -> None:
        """Nothing configured is "not estimated", never a 0 lb job."""
        payload = self._phase5(script, tmp_path, job=_job(), job_config=None)

        weight = payload["usage"]["weight"]
        assert weight["available"] is False
        assert weight["total"] is None
        assert "material_weight_per_square_inch" in str(weight["note"])

    def _phase5(self, script: Any, tmp_path: Path, **overrides: Any) -> dict[str, Any]:
        """Run phase 5 against a minimal job and return the JSON document."""
        job = overrides.pop("job", _job())
        kwargs: dict[str, Any] = {
            "job": job,
            "job_yaml": tmp_path / "job.yaml",
            "export_result": _export(tmp_path, job),
            "resolved_labels": [_label()],
            "inventory": [0.06],
            "boundary_hole_cutter": 0.015,
            "job_config": None,
            "capture": ReportCapture(),
            "stage_timings": StageTimings(),
            "started_at": "2026-11-16T00:00:00+00:00",
            "duration_seconds": 3.5,
        }
        kwargs.update(overrides)
        script.phase_5_generation_report(**kwargs)
        return _read_report(tmp_path)


class TestFailureReport:
    """A failed run leaves the reason on disk, like the CLI does."""

    def test_writes_failed_status_with_the_stage(self, script: Any, tmp_path: Path) -> None:
        capture = ReportCapture()
        logger = logging.getLogger("plt_optimizer.generate.test")
        logger.addHandler(capture)
        try:
            logger.warning("Hole at (0.100, 0.100) rescued by compression")
        finally:
            capture.close()

        script._write_failure_report_for_run(
            stage="export",
            error=RuntimeError("Label abc123 does not fit in any plate"),
            job_yaml=tmp_path / "job.yaml",
            output_dir=tmp_path,
            job=_job(),
            job_config=None,
            inventory=[0.06],
            boundary_hole_cutter=0.015,
            resolved_labels=[_label()],
            export_result=None,
            capture=capture,
            stage_timings=StageTimings(),
            started_at="2026-11-16T00:00:00+00:00",
            duration_seconds=0.5,
        )

        payload = _read_report(tmp_path)
        assert payload["job"]["status"] == "failed"
        assert payload["job"]["error"] == {
            "type": "RuntimeError",
            "message": "Label abc123 does not fit in any plate",
            "stage": "export",
        }
        assert payload["summary"]["warnings"] == 1
        # Nothing had been written when the run aborted.
        assert payload["outputs"]["plt_files"] == []
        assert (report_dir_for(tmp_path) / REPORT_SUMMARY_NAME).is_file()
        # The digest names the failure, so the console and the file agree.
        assert "RuntimeError" in (report_dir_for(tmp_path) / REPORT_SUMMARY_NAME).read_text(
            encoding="utf-8"
        )

    def test_survives_a_failure_before_parsing(self, script: Any, tmp_path: Path) -> None:
        """No job: the spec stem stands in for the name, zero plates."""
        script._write_failure_report_for_run(
            stage="prep",
            error=ValueError("job-config.json is missing"),
            job_yaml=tmp_path / "cold_start.yaml",
            output_dir=tmp_path,
            job=None,
            job_config=None,
            inventory=[],
            boundary_hole_cutter=None,
            resolved_labels=[],
            export_result=None,
            capture=ReportCapture(),
            stage_timings=StageTimings(),
            started_at="2026-11-16T00:00:00+00:00",
            duration_seconds=0.1,
        )
        payload = _read_report(tmp_path)
        assert payload["job"]["job_name"] == "cold_start"
        assert payload["job"]["job_id"] == "cold_start"
        assert payload["summary"]["plates"] == 0
        assert payload["job"]["error"]["stage"] == "prep"

    def test_partial_export_is_still_reported(self, script: Any, tmp_path: Path) -> None:
        """Failure after the export: the files already written are listed."""
        job = _job()
        script._write_failure_report_for_run(
            stage="validate",
            error=RuntimeError("negative coordinate"),
            job_yaml=tmp_path / "job.yaml",
            output_dir=tmp_path,
            job=job,
            job_config=None,
            inventory=[0.06],
            boundary_hole_cutter=0.015,
            resolved_labels=[_label()],
            export_result=_export(tmp_path, job),
            capture=ReportCapture(),
            stage_timings=StageTimings(),
            started_at="2026-11-16T00:00:00+00:00",
            duration_seconds=2.0,
        )
        payload = _read_report(tmp_path)
        assert payload["job"]["status"] == "failed"
        assert len(payload["outputs"]["plt_files"]) == 1
        assert payload["outputs"]["plates"][0]["labels"][0]["label_id"] == "abc123"

    def test_unwritable_report_dir_is_logged_not_raised(
        self, script: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A report failure must never mask the original error."""

        def _boom(report_dir: Path, report: dict[str, Any]) -> tuple[Path, Path]:
            raise OSError("disk full")

        monkeypatch.setattr(script, "write_report", _boom)
        script._write_failure_report_for_run(
            stage="export",
            error=RuntimeError("boom"),
            job_yaml=tmp_path / "job.yaml",
            output_dir=tmp_path,
            job=_job(),
            job_config=None,
            inventory=[],
            boundary_hole_cutter=None,
            resolved_labels=[],
            export_result=None,
            capture=ReportCapture(),
            stage_timings=StageTimings(),
            started_at="2026-11-16T00:00:00+00:00",
            duration_seconds=0.1,
        )


class TestReportHelpers:
    """The small pure helpers Phase 5 builds its document from."""

    def test_job_identity_from_the_spec(self, script: Any, tmp_path: Path) -> None:
        job = _job()
        name, job_id, plate_count = script._job_identity(job, tmp_path / "job.yaml")
        assert name == "Report Phase Job"
        assert job_id == "Report_Phase_Job"
        assert plate_count == 1

    def test_job_identity_without_a_spec(self, script: Any, tmp_path: Path) -> None:
        name, job_id, plate_count = script._job_identity(None, tmp_path / "my job.yaml")
        assert name == "my job"
        assert job_id == "my_job"
        assert plate_count == 0

    def test_report_tools_mirror_the_cli(self, script: Any) -> None:
        tools = script._report_tools([0.06, 0.125], 0.015, None)
        assert tools == {
            "available_cutters": [0.06, 0.125],
            "boundary_hole_cutter_size": 0.015,
            "cutter_buffer_text": None,
            "cutter_buffer_routing": None,
        }

    def test_report_tools_read_the_kerf_buffers(self, script: Any, tmp_path: Path) -> None:

        config_path = tmp_path / "job-config.json"
        config_path.write_text(
            json.dumps({"cutter_buffer_text": 0.005, "cutter_buffer_routing": 0.045}),
            encoding="utf-8",
        )
        from plt_optimizer.generate.job_config import load_job_config

        tools = script._report_tools([0.06], 0.015, load_job_config(config_path))
        assert tools["cutter_buffer_text"] == 0.005
        assert tools["cutter_buffer_routing"] == 0.045

    def test_report_options_describe_the_run(self, script: Any) -> None:
        options = script._report_options()
        assert options["source"] == "scripts/run_integration_test.py"
        assert options["default_plots"] is script.GENERATE_DEFAULT_PLOTS


class TestJobsetModeArgs:
    """The batch mode's command line: dispatch, defaults, filtering, ``--no-plots``.

    None of these tests engrave anything: the runs are stubbed at the phase
    boundary, so what is pinned is the argument surface (which runner a shape
    selects, which files a directory sweep takes, when the runner refuses) and
    the plot switch that the whole suite needs on a machine where the PDF
    backend aborts the process.
    """

    SET_DIR = REPO_ROOT / "examples" / "job_specs" / "job_sets"

    @pytest.fixture()
    def set_files(self, tmp_path: Path) -> Path:
        """Copy the shipped example set (and its members) into a temp directory."""
        for name in ("jobset example.yaml", "jobset example A.yaml", "jobset example B.yaml"):
            (tmp_path / name).write_text((self.SET_DIR / name).read_text(encoding="utf-8"))
        return tmp_path

    @pytest.fixture(autouse=True)
    def _plots_on(self, script: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """Keep the module's plot switch True unless a test drives it."""
        monkeypatch.setattr(script, "WRITE_PLOTS", True, raising=False)

    def test_jobset_file_runs_the_batch_runner(
        self, script: Any, set_files: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[Path] = []

        def _record(path: Any) -> int:
            seen.append(path)
            return 0

        monkeypatch.setattr(script, "_run_single_jobset", _record, raising=False)
        assert script.main(["--jobset", str(set_files / "jobset example.yaml")]) == 0
        assert seen == [set_files / "jobset example.yaml"]

    def test_jobset_without_a_path_defaults_to_the_example_directory(
        self, script: Any, set_files: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        roots: list[Path] = []

        def _collect(spec_dir: Path) -> list[Path]:
            roots.append(spec_dir)
            return []

        monkeypatch.setattr(script, "_collect_jobset_paths", _collect, raising=False)
        monkeypatch.setattr(script, "DEFAULT_JOBSET_DIR", set_files, raising=False)
        script.main(["--jobset"])
        assert roots == [set_files]

    def test_jobset_directory_mode_takes_only_the_sets(self, script: Any, set_files: Path) -> None:
        (set_files / "plain_job.yaml").write_text(
            "job:\n  job_name: Plain\n  labels: []\n", encoding="utf-8"
        )
        collected = script._collect_jobset_paths(set_files)
        assert [path.name for path in collected] == ["jobset example.yaml"]

    def test_a_set_without_the_flag_is_refused(
        self, script: Any, set_files: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = script.main([str(set_files / "jobset example.yaml")])
        assert code == 1
        err = capsys.readouterr().err
        assert "is a jobset, not a job spec" in err
        assert "--jobset" in err

    def test_no_plots_skips_every_pdf_in_batch_mode(
        self, script: Any, set_files: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _record(path: Any) -> int:
            return 0

        monkeypatch.setattr(script, "_run_single_jobset", _record, raising=False)
        assert script.main(["--jobset", "--no-plots", str(set_files / "jobset example.yaml")]) == 0
        assert script.WRITE_PLOTS is False
        assert script._report_options()["no_plots"] is True

    def test_no_plots_skips_every_pdf_for_job_specs(
        self, script: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[Any] = []

        def _record(path: Any) -> int:
            seen.append(path)
            return 0

        monkeypatch.setattr(script, "_run_single_spec", _record, raising=False)
        assert script.main(["--no-plots", "tests_deps/test123_spec.yaml"]) == 0
        assert seen == [REPO_ROOT / "tests_deps" / "test123_spec.yaml"]
        assert script.WRITE_PLOTS is False

    def test_no_plots_returns_no_phase_4_paths(
        self, script: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(script, "WRITE_PLOTS", False, raising=False)
        export = _export(tmp_path, _job())
        assert script.phase_4_visualization(export, job_name="x") == []

    def test_two_paths_are_refused(
        self, script: Any, set_files: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert script.main(["--jobset", "a.yaml", "b.yaml"]) == 2
        assert "at most one path" in capsys.readouterr().err

    def test_unknown_option_is_refused(
        self, script: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert script.main(["--nonsense"]) == 2
        assert "Unrecognized option(s): --nonsense" in capsys.readouterr().err

    def test_help_prints_usage(self, script: Any, capsys: pytest.CaptureFixture[str]) -> None:
        assert script.main(["--help"]) == 0
        out = capsys.readouterr().out
        assert "--jobset" in out and "--no-plots" in out
