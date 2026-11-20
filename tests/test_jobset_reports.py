"""Jobset per-job accounting: ``usage.by_job``, member reports, ``jobset.json``.

A jobset is one toolpath pool shared by several jobs, so the batch report alone
cannot answer the shop's per-job question ("what did MY label cost?"). The
contract under test (Layer 5 of the jobset plan):

* ``usage.by_job`` is a distance/time breakdown that adds up to the batch total
  **exactly** - every stroke is charged to one member, so it reconciles with
  every other total-charged-once breakdown (``by_cutter``, ``by_material``,
  ``by_plate``, ``by_class``);
* a member's own numbers are measured from the segment files that carry its
  strokes, so a per-job report is the same document a standalone run writes -
  with the ids that run declares (the ``{job_key}::`` namespace is stripped);
* ``jobset.json`` indexes the batch: members, gate warnings, per-plate owners,
  per-file provenance (bytes + sha256) and the cross-job saving;
* the shared-toolpath saving is reported, not hidden: a merged stroke charged
  to two labels makes the per-label sum exceed the total, and that delta is
  ``cross_job.distance_saved``.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest

from plt_optimizer.generate.generation_report import build_report, format_summary
from plt_optimizer.generate.job_config import JobConfig, load_job_config
from plt_optimizer.generate.jobset import JobSetPlan, build_jobset_plan
from plt_optimizer.generate.jobset_report import (
    MANIFEST_NAME,
    UNATTRIBUTED_JOB,
    build_job_usage,
    build_jobset_block,
    build_manifest,
    cross_job_summary,
    job_of_placement,
    write_job_reports,
    write_manifest,
)
from plt_optimizer.generate.usage_stats import build_usage_stats
from plt_optimizer.generate.vectorize import PerCutterExport, export_per_cutter_plts

FIXTURES = Path("tests_deps")
REPO_CONFIG = Path("job-config.json")

ALPHA = "Export_Alpha"
BETA = "Export_Beta"


def _plan(name: str) -> JobSetPlan:
    """Build a jobset plan from a fixture in ``tests_deps/``."""
    return build_jobset_plan(FIXTURES / name, job_config=None)


def _export(
    plan: JobSetPlan, output_dir: Path, job_config: Optional[JobConfig] = None
) -> PerCutterExport:
    """Export a whole jobset plan (segmented, no plots, no routing)."""
    return export_per_cutter_plts(
        plan.merged_labels,
        plan.merged_plates,
        output_dir=output_dir,
        job_id=plan.jobset_id,
        job_name=plan.jobset_name,
        layout=plan.layout,
        allow_rotation=plan.allow_rotation,
        default_plate_clearance=plan.default_plate_clearance,
        plots=False,
        optimize=False,
        provenance=plan.provenance,
        layer_specs={context.job_key: context.spec for context in plan.contexts},
        job_config=job_config,
        job_spec=plan.contexts[0].spec if job_config is not None else None,
    )


def _root_usage(
    plan: JobSetPlan, export: PerCutterExport, job_config: Optional[JobConfig] = None
) -> Dict[str, Any]:
    """Measure the batch the way the CLI's root report does (with ``by_job``)."""
    return build_usage_stats(
        resolved_labels=plan.merged_labels,
        packed_plates=export.packed_plates,
        combined_by_plate=export.combined_by_plate,
        plt_files=export.plt_paths,
        material_by_plate=export.material_by_plate,
        job_spec=plan.contexts[0].spec if job_config is not None else None,
        job_config=job_config,
        job_of_label=job_of_placement(export, plan),
    )


def _total(distance_row: Mapping[str, Any]) -> float:
    """Sum of one ``by_job``/breakdown row's inches."""
    return float(distance_row.get("cutting") or 0.0) + float(distance_row.get("rapid") or 0.0)


def _read_json(path: Path) -> Dict[str, Any]:
    """Read a written JSON document as a mapping."""
    document: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


@pytest.fixture
def batch(tmp_path: Path) -> Dict[str, Any]:
    """The two-member export batch, its plan, usage and config."""
    plan = _plan("jobset_export_batch.yaml")
    config = load_job_config(REPO_CONFIG)
    export = _export(plan, tmp_path / "out", config)
    return {
        "plan": plan,
        "config": config,
        "export": export,
        "usage": _root_usage(plan, export, config),
        "out": tmp_path / "out",
    }


class TestJobOfPlacement:
    """The placement -> member map is what turns a shared file into per-job math."""

    def test_every_placement_names_its_member(self, batch: Dict[str, Any]) -> None:
        mapping = job_of_placement(batch["export"], batch["plan"])

        assert mapping, "the batch export should carry placements"
        assert set(mapping.values()) == {ALPHA, BETA}

    def test_an_export_without_placements_has_no_map(self, batch: Dict[str, Any]) -> None:
        """The map is built from placements, so an empty run maps nothing."""
        assert job_of_placement(PerCutterExport(), batch["plan"]) == {}


class TestByJobRollup:
    """``usage.by_job`` adds up to the batch total - charged exactly once."""

    def test_distance_by_job_sums_to_the_total(self, batch: Dict[str, Any]) -> None:
        distance = batch["usage"]["distance"]

        rows = distance["by_job"]

        assert {ALPHA, BETA} <= {row["job"] for row in rows}
        assert sum(_total(row) for row in rows) == pytest.approx(
            distance["total"]["total"], abs=1e-4
        )

    def test_by_job_reconciles_with_every_charged_once_breakdown(
        self, batch: Dict[str, Any]
    ) -> None:
        """The plan's invariant line, spelled out for a shared plate."""
        distance = batch["usage"]["distance"]

        by_job = sum(_total(row) for row in distance["by_job"])
        by_cutter = sum(_total(row) for row in distance["by_cutter"])
        by_material = sum(_total(row) for row in distance["by_material"])
        by_plate = sum(_total(row) for row in distance["by_plate"])

        assert by_job == pytest.approx(distance["total"]["total"], abs=1e-4)
        assert by_job == pytest.approx(by_cutter, abs=1e-4)
        assert by_job == pytest.approx(by_material, abs=1e-4)
        assert by_job == pytest.approx(by_plate, abs=1e-4)
        # ``by_class`` is the cutting-only breakdown (pen-up travel has no
        # stroke class), so the member rows meet it on the cutting axis.
        assert sum(float(row["cutting"]) for row in distance["by_job"]) == pytest.approx(
            distance["total"]["cutting"], abs=1e-4
        )
        assert sum(float(value or 0.0) for value in distance["by_class"].values()) == (
            pytest.approx(distance["total"]["cutting"], abs=1e-4)
        )

    def test_time_by_job_sums_to_the_time_total(self, batch: Dict[str, Any]) -> None:
        time = batch["usage"]["time"]
        if not time["available"]:
            pytest.skip("job-config.json sets no cutting velocity for this layer")

        assert sum(float(row["total"]) for row in time["by_job"]) == pytest.approx(
            time["total"]["total"], abs=1e-4
        )
        assert {"cutting", "rapid", "plunge", "z_up", "dwell"} <= set(
            time["by_job"][0]["by_operation"]
        )

    def test_rows_are_ordered_by_descending_distance(self, batch: Dict[str, Any]) -> None:
        rows = batch["usage"]["distance"]["by_job"]

        values = [_total(row) for row in rows]
        assert values == sorted(values, reverse=True)

    def test_a_single_job_run_has_no_by_job_key(self) -> None:
        """``by_job`` is a jobset-only key - a solo report is unchanged."""
        usage = build_usage_stats(
            resolved_labels=[],
            packed_plates=[],
            combined_by_plate={},
            plt_files=[],
            material_by_plate={},
        )

        assert "by_job" not in usage["distance"]
        assert "by_job" not in usage["time"]

    def test_a_file_with_no_owner_is_charged_to_the_unattributed_row(
        self, batch: Dict[str, Any]
    ) -> None:
        """Ownership gaps stay visible instead of vanishing from the total."""
        export = batch["export"]
        map_without_owner = {
            key: (UNATTRIBUTED_JOB if index == 0 else value)
            for index, (key, value) in enumerate(job_of_placement(export, batch["plan"]).items())
        }

        usage = _root_usage(batch["plan"], export) | {}
        measured = build_usage_stats(
            resolved_labels=batch["plan"].merged_labels,
            packed_plates=export.packed_plates,
            combined_by_plate=export.combined_by_plate,
            plt_files=export.plt_paths,
            material_by_plate=export.material_by_plate,
            job_of_label=map_without_owner,
        )

        rows = {row["job"]: _total(row) for row in measured["distance"]["by_job"]}
        assert UNATTRIBUTED_JOB in rows
        assert sum(rows.values()) == pytest.approx(measured["distance"]["total"]["total"], abs=1e-4)
        assert usage  # the batch itself measured cleanly


class TestMemberUsage:
    """A member measured inside the batch is that member's own toolpath."""

    def test_a_member_report_is_measured_from_its_own_files(self, batch: Dict[str, Any]) -> None:
        plan, export = batch["plan"], batch["export"]
        context = next(c for c in plan.contexts if c.job_key == ALPHA)

        usage = build_job_usage(plan, export, context, job_config=batch["config"])

        # A member report of a jobset DOES carry by_job: its files are the
        # batch's MERGED toolpaths, so measurement is stroke by stroke and the
        # member's rows are its share of a shared run. It holds exactly one row.
        rows = usage["distance"].get("by_job") or []
        assert [row["job"] for row in rows] == [ALPHA]
        measured = sum(
            float(row["cutting"]) + float(row["rapid"])
            for row in usage["distance"]["files"]
            if "error" not in row
        )
        assert measured == pytest.approx(usage["distance"]["total"]["total"], abs=1e-4)
        assert {row["path"].split("/")[-1] for row in usage["distance"]["files"]} == {
            path.name for path in export.plt_paths if ALPHA in export.file_jobs.get(path, ())
        }

    def test_the_members_sum_to_the_batch_total(self, batch: Dict[str, Any]) -> None:
        """Every stroke the batch measured is owned by exactly one member, so the
        members' reports add up to the batch's - tool travel included.

        A sheet's border and registration marks belong to no label; they are
        charged to the sheet's FIRST member in every view (see
        :func:`~plt_optimizer.generate.usage_stats.build_usage_stats`'
        ``plate_owner_of``), which is what makes this partition exact. A stroke
        in a file shared by several members that resolves to no placement at all
        stays unattributed in every view, so it is carried here by the batch's
        ``unattributed`` row - and the batch's named rows plus that row are the
        whole total.
        """
        plan, export = batch["plan"], batch["export"]

        members = [build_job_usage(plan, export, context)["distance"] for context in plan.contexts]
        root = batch["usage"]["distance"]

        assert sum(float(b["total"]["cutting"]) for b in members) == pytest.approx(
            root["total"]["cutting"], abs=1e-4
        )
        assert sum(float(b["total"]["rapid"]) for b in members) == pytest.approx(
            root["total"]["rapid"], abs=1e-4
        )
        assert sum(_total(b["total"]) for b in members) == pytest.approx(
            root["total"]["total"], abs=1e-4
        )

    def test_a_member_row_is_label_attribution_not_a_file_sum(self, batch: Dict[str, Any]) -> None:
        """The two per-job views answer different questions, and each one closes
        on its own measurement.

        The batch's ``by_job`` ALLOCATES the shared run: every stroke is charged
        to the member whose placement contains it, sheet furniture to the
        sheet's first member, and a stroke no placement explains (a glyph widened
        by compression in a file several jobs share) to ``unattributed``. A
        member's own report is measured out of the FILES it contributes to,
        stroke by stroke, so it also carries the travel the batch could not
        attribute to a slot. The two numbers are therefore NOT expected to be
        equal; what is pinned here is that each is internally complete - the
        member's rows sum to its total, and the batch's ``by_job`` rows partition
        the batch's total exactly.
        """
        plan, export = batch["plan"], batch["export"]
        alpha = next(c for c in plan.contexts if c.job_key == ALPHA)

        member = build_job_usage(plan, export, alpha)["distance"]
        root = batch["usage"]["distance"]

        assert float(member["total"]["cutting"]) == pytest.approx(
            sum(float(row["cutting"]) for row in member["files"] if "error" not in row), abs=1e-4
        )
        rows = root.get("by_job") or []
        assert {row["job"] for row in rows} >= {ALPHA}
        assert sum(float(row["cutting"]) for row in rows) == pytest.approx(
            root["total"]["cutting"], abs=1e-4
        )
        assert sum(float(row["rapid"]) for row in rows) == pytest.approx(
            root["total"]["rapid"], abs=1e-4
        )
        # A member's own report carries no other member's row.
        member_rows = member.get("by_job") or []
        assert {row["job"] for row in member_rows} == {ALPHA}

    def test_member_material_counts_only_its_own_labels(self, batch: Dict[str, Any]) -> None:
        plan, export = batch["plan"], batch["export"]
        context = next(c for c in plan.contexts if c.job_key == ALPHA)

        usage = build_job_usage(plan, export, context)

        assert usage["labels"]["instances"] == sum(label.count for label in context.resolved_labels)
        assert usage["labels"]["specs"] == len(context.resolved_labels)
        plates = {row["plate"] for row in usage["distance"]["by_plate"]}
        assert plates and plates <= set(export.combined_by_plate)
        assert int(usage["material"]["total"]["plates"]) == len(export.packed_plates)

    def test_members_never_double_count_cutting(self, batch: Dict[str, Any]) -> None:
        """No stroke is engraved by two members, so per-member cutting sums to
        the batch's; the batch's per-label excess over the total is the merged
        shared layer, which is reported as a saving, not a summand."""
        plan, export = batch["plan"], batch["export"]

        members = [build_job_usage(plan, export, context)["distance"] for context in plan.contexts]
        root = batch["usage"]["distance"]

        assert sum(float(block["total"]["cutting"]) for block in members) == pytest.approx(
            root["total"]["cutting"], abs=1e-4
        )


class TestMemberReports:
    """Each member gets the report a standalone run would have written."""

    @pytest.fixture
    def written(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        plan, export, out = batch["plan"], batch["export"], batch["out"]
        reports = write_job_reports(
            plan,
            export,
            output_dir=out,
            job_config=batch["config"],
            started_at="2026-11-17T00:00:00+00:00",
            finished_at="2026-11-17T00:01:00+00:00",
            duration_seconds=60.0,
            tools={"cutter_size": 0.005},
            options={"spec": "jobset_export_batch.yaml"},
        )
        return {"reports": reports, "batch": batch}

    def test_report_lands_in_the_member_folder(self, written: Dict[str, Any]) -> None:
        reports = written["reports"]
        batch = written["batch"]

        for context in batch["plan"].contexts:
            path = reports.job_reports[context.job_key]
            assert (
                path == batch["out"] / context.record.job_id / "generation_report" / "report.json"
            )
            assert path.is_file()
            assert path.with_name("summary.txt").is_file()

    def test_report_prints_the_ids_the_member_file_declares(self, written: Dict[str, Any]) -> None:
        import json

        reports = written["reports"]
        batch = written["batch"]
        context = next(c for c in batch["plan"].contexts if c.job_key == ALPHA)

        document = json.loads(reports.job_reports[context.job_key].read_text(encoding="utf-8"))
        printed = {label["id"] for label in document["labels"]}

        assert printed == {label.id.split("::", 1)[1] for label in context.resolved_labels}
        assert all("::" not in label_id for label_id in printed)
        plate_ids = {label["plate_id"] for label in document["labels"] if label["plate_id"]}
        assert all("::" not in plate for plate in plate_ids)

    def test_job_metadata_is_the_member_own(self, written: Dict[str, Any]) -> None:
        import json

        reports = written["reports"]
        batch = written["batch"]
        context = next(c for c in batch["plan"].contexts if c.job_key == BETA)

        document = json.loads(reports.job_reports[context.job_key].read_text(encoding="utf-8"))
        job = document["job"]

        assert job["job_id"] == context.record.job_id
        assert job["job_name"] == context.record.job_name
        assert Path(job["spec_path"]).name == context.record.spec_path.name
        assert document["summary"]["labels"] == len(context.resolved_labels)
        assert document["usage"]["distance"]["total"]["total"] == pytest.approx(
            build_job_usage(batch["plan"], batch["export"], context, job_config=batch["config"])[
                "distance"
            ]["total"]["total"],
            abs=1e-4,
        )

    def test_member_plt_list_is_exactly_its_own_segment_files(
        self, written: Dict[str, Any]
    ) -> None:
        import json

        reports = written["reports"]
        batch = written["batch"]
        export = batch["export"]

        for context in batch["plan"].contexts:
            document = json.loads(reports.job_reports[context.job_key].read_text(encoding="utf-8"))
            listed = {Path(path).name for path in document["outputs"]["plt_files"]}
            owned = {
                path.name
                for path in export.plt_paths
                if context.job_key in export.file_jobs.get(path, ())
            }
            assert listed == owned, context.job_key
            assert listed, "a member should own at least one file"


class TestManifest:
    """``jobset.json`` is the machine-readable index of the batch."""

    @pytest.fixture
    def manifest(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        plan, export, out = batch["plan"], batch["export"], batch["out"]
        reports = write_job_reports(plan, export, output_dir=out, job_config=batch["config"])
        written = write_manifest(plan, export, reports, output_dir=out, root_usage=batch["usage"])
        assert written == _read_json(reports.manifest_path)
        return written

    def test_top_level_shape(self, manifest: Dict[str, Any]) -> None:
        assert set(manifest) == {"jobset", "gate", "jobs", "plates", "files", "cross_job"}
        plan = _plan("jobset_export_batch.yaml")
        assert manifest["jobset"]["id"] == plan.jobset_id
        assert manifest["jobset"]["name"] == plan.jobset_name
        assert manifest["jobset"]["spec"] == "jobset_export_batch.yaml"
        assert manifest["gate"] == {"warnings": list(plan.warnings), "errors": []}

    def test_job_rows_name_the_member_its_report_and_its_totals(
        self, manifest: Dict[str, Any], batch: Dict[str, Any]
    ) -> None:
        rows = {row["job_key"]: row for row in manifest["jobs"]}

        assert set(rows) == {ALPHA, BETA}
        for context in batch["plan"].contexts:
            row = rows[context.job_key]
            assert row["job_name"] == context.record.job_name
            assert row["spec"].endswith(context.record.spec_path.name)
            assert row["labels"] == len(context.record.label_ids)
            assert row["plates_declared"] == list(context.record.plate_ids)
            assert row["report"] == f"{context.record.job_id}/generation_report/report.json"
            assert row["usage"]["distance_total"] == pytest.approx(
                build_job_usage(batch["plan"], batch["export"], context)["distance"]["total"][
                    "total"
                ],
                abs=1e-4,
            )
            assert row["usage"]["unattributed"] >= 0.0
            assert row["usage"]["unattributed"] <= (row["usage"]["distance_total"] or 0.0)

    def test_job_rows_follow_jobset_file_order(self, manifest: Dict[str, Any]) -> None:
        assert [row["job_key"] for row in manifest["jobs"]] == [ALPHA, BETA]

    def test_a_configured_member_row_carries_its_weight(self) -> None:
        """A member whose spec declares the density is weighed; one that does not is null."""
        from plt_optimizer.generate.jobset_report import _job_usage_totals

        weighed = _job_usage_totals(
            {"weight": {"available": True, "total": {"lb": 0.5}, "note": ""}}
        )
        assert weighed["weight_total"] == pytest.approx(0.5)
        unweighed = _job_usage_totals(
            {"weight": {"available": False, "total": None, "note": "no density"}}
        )
        assert unweighed["weight_total"] is None

    def test_a_member_without_a_density_reports_a_null_weight(self, batch: Dict[str, Any]) -> None:
        """Mixed batch: ``null`` is never folded into the total as 0."""
        plan, export, out = batch["plan"], batch["export"], batch["out"]
        keys = [context.record.key for context in plan.contexts]
        usage_by_job: Dict[str, Dict[str, Any]] = {
            keys[0]: {"weight": {"available": True, "total": {"lb": 1.25}}},
            keys[1]: {
                "weight": {
                    "available": False,
                    "total": None,
                    "note": "No material_weight_per_square_inch configured.",
                }
            },
        }
        document = build_manifest(
            plan,
            export,
            output_dir=out,
            usage_by_job=usage_by_job,
            reports={key: out / f"{key}.json" for key in keys},
            root_usage={"weight": {"available": True, "total": {"lb": 1.25}}},
        )
        rows = {row["job_key"]: row for row in document["jobs"]}

        assert rows[keys[0]]["usage"]["weight_total"] == pytest.approx(1.25)
        assert rows[keys[1]]["usage"]["weight_total"] is None
        # The batch total is the root usage's own number, not a sum of rows.
        assert document["jobset"]["total_weight_lb"] == pytest.approx(1.25)

    def test_an_unweighed_batch_reports_a_null_total(self, batch: Dict[str, Any]) -> None:
        """No member configured a density: the batch carries no weight at all."""
        plan, export, out = batch["plan"], batch["export"], batch["out"]
        keys = [context.record.key for context in plan.contexts]
        unweighed: Dict[str, Any] = {"weight": {"available": False, "total": None, "note": "x"}}
        usage_by_job: Dict[str, Dict[str, Any]] = {key: dict(unweighed) for key in keys}
        document = build_manifest(
            plan,
            export,
            output_dir=out,
            usage_by_job=usage_by_job,
            reports={key: out / f"{key}.json" for key in keys},
            root_usage=dict(unweighed),
        )

        assert document["jobset"]["total_weight_lb"] is None
        assert [row["usage"]["weight_total"] for row in document["jobs"]] == [None] * len(keys)

    def test_plate_rows_name_every_owner_and_label(self, manifest: Dict[str, Any]) -> None:
        plate = manifest["plates"][0]

        assert plate["plate"] == 1
        assert list(plate["jobs"]) == [ALPHA, BETA]
        assert set(plate["labels"]) == {ALPHA, BETA}
        assert all("::" not in label for ids in plate["labels"].values() for label in ids)
        assert plate["files"], "a shared plate lists the files written for it"

    def test_file_rows_carry_provenance_and_digests(
        self, manifest: Dict[str, Any], batch: Dict[str, Any]
    ) -> None:
        import hashlib

        rows = {row["path"]: row for row in manifest["files"]}

        assert len(rows) == len(batch["export"].plt_paths)
        for path in batch["export"].plt_paths:
            row = rows[path.relative_to(batch["out"]).as_posix()]
            payload = path.read_bytes()
            assert row["bytes"] == len(payload)
            assert row["sha256"] == hashlib.sha256(payload).hexdigest()
            assert row["jobs"] == list(batch["export"].file_jobs[path])
            assert row["layer"] in ("text", "borders_holes")
            assert row["cutter"] is not None

    def test_manifest_is_json_safe(self, manifest: Dict[str, Any]) -> None:
        import json

        assert json.loads(json.dumps(manifest)) == manifest

    def test_manifest_file_is_named_jobset_json(self, batch: Dict[str, Any]) -> None:
        out = batch["out"]
        plan, export = batch["plan"], batch["export"]
        reports = write_job_reports(plan, export, output_dir=out)

        write_manifest(plan, export, reports, output_dir=out, root_usage=batch["usage"])

        assert reports.manifest_path.name == MANIFEST_NAME
        assert reports.manifest_path == out / "generation_report" / MANIFEST_NAME
        assert reports.manifest_path.is_file()


class TestCrossJobSaving:
    """The shared-toolpath saving is measured, never silently inconsistent."""

    def test_saved_distance_is_the_per_label_excess(self, batch: Dict[str, Any]) -> None:
        distance = batch["usage"]["distance"]
        by_label = sum(_total(row) for row in distance["by_label"])

        summary = cross_job_summary(batch["usage"], batch["export"])

        assert summary["distance_saved"] == pytest.approx(
            max(by_label - distance["total"]["total"], 0.0), abs=1e-3
        )
        assert summary["distance_saved"] >= 0.0
        assert "shared-toolpath" in summary["note"]

    def test_strokes_merged_counts_shared_layers_on_mixed_plates(
        self, batch: Dict[str, Any]
    ) -> None:
        distance = batch["usage"]["distance"]
        mixed = {
            number
            for number, owners in batch["export"].plate_jobs.items()
            if len(tuple(owners)) > 1
        }
        expected = sum(
            int(row["strokes"])
            for row in distance["files"]
            if row.get("kind") == "txt" and row.get("plate") in mixed
        )

        assert cross_job_summary(batch["usage"], batch["export"])["strokes_merged"] == expected

    def test_a_batch_without_usage_reports_zeroes(self, batch: Dict[str, Any]) -> None:
        summary = cross_job_summary(None, batch["export"])

        assert summary["distance_saved"] == 0.0
        assert summary["strokes_merged"] == 0


class TestBatchReportSection:
    """The batch report indexes its members; a solo report does not."""

    def test_block_lists_members_with_reports_and_totals(self, batch: Dict[str, Any]) -> None:
        plan, export, out = batch["plan"], batch["export"], batch["out"]
        reports = write_job_reports(plan, export, output_dir=out, job_config=batch["config"])

        block = build_jobset_block(plan, export, reports, output_dir=out, root_usage=batch["usage"])

        assert [row["job_key"] for row in block["jobs"]] == [ALPHA, BETA]
        assert block["id"] == plan.jobset_id
        assert block["cross_job"] == cross_job_summary(batch["usage"], export)

    def test_summary_names_every_member(self, batch: Dict[str, Any]) -> None:
        plan, export, out = batch["plan"], batch["export"], batch["out"]
        reports = write_job_reports(plan, export, output_dir=out, job_config=batch["config"])
        block = build_jobset_block(plan, export, reports, output_dir=out, root_usage=batch["usage"])
        document = build_report(
            job_name=plan.jobset_name,
            job_id=plan.jobset_id,
            spec_path=plan.jobset_path,
            output_dir=out,
            report_dir=out / "generation_report",
            resolved_labels=plan.merged_labels,
            packed_plates=export.packed_plates,
            plt_files=export.plt_paths,
            usage=batch["usage"],
            jobset=block,
        )

        summary = format_summary(document)

        assert f"Jobs ({len(plan.contexts)}):" in summary
        for context in plan.contexts:
            assert f"({context.record.job_id})" in summary
            assert f"{context.record.job_id}/generation_report/report.json" in summary
        saved = block["cross_job"]["distance_saved"]
        assert ("shared toolpaths saved" in summary) is bool(saved)

    def test_a_solo_summary_has_no_jobs_section(self) -> None:
        document = build_report(
            job_name="Solo",
            job_id="solo",
            spec_path=Path("solo.yaml"),
            output_dir=Path("out"),
            report_dir=Path("out/generation_report"),
        )

        assert "Jobs (" not in format_summary(document)
        assert "jobset" not in document


class TestReportBuilderIdPrefix:
    """``build_report(id_prefix=...)`` un-namespaces the ids a member prints."""

    def test_prefix_is_stripped_from_ids_and_plate_ids(self, batch: Dict[str, Any]) -> None:
        plan = batch["plan"]
        context = next(c for c in plan.contexts if c.job_key == ALPHA)

        document = build_report(
            job_name=context.record.job_name,
            job_id=context.record.job_id,
            spec_path=context.record.spec_path,
            output_dir=Path("out"),
            report_dir=Path("out/generation_report"),
            resolved_labels=list(context.resolved_labels),
            packed_plates=[],
            id_prefix=f"{context.job_key}::",
        )

        ids = [label["id"] for label in document["labels"]]
        assert ids and all("::" not in value for value in ids)
        assert set(ids) == {label.id.split("::", 1)[1] for label in context.resolved_labels}
        for label in document["labels"]:
            if label["plate_id"]:
                assert "::" not in label["plate_id"]

    def test_no_prefix_leaves_ids_untouched(self, batch: Dict[str, Any]) -> None:
        plan = batch["plan"]

        document = build_report(
            job_name=plan.jobset_name,
            job_id=plan.jobset_id,
            spec_path=plan.jobset_path,
            output_dir=Path("out"),
            report_dir=Path("out/generation_report"),
            resolved_labels=plan.merged_labels,
        )

        ids = [label["id"] for label in document["labels"]]
        assert all("::" in label_id for label_id in ids)


class TestManifestHelperEdgeCases:
    """The manifest helpers stay total on partial runs."""

    def test_relative_paths_outside_the_output_dir_are_absolute(self) -> None:
        from plt_optimizer.generate.jobset_report import _relative

        assert _relative(Path("/tmp/other/report.json"), Path("out")) == "/tmp/other/report.json"
        assert _relative(Path("out/plt/a.plt"), Path("out")) == "plt/a.plt"
        assert _relative(None, Path("out")) is None

    def test_job_order_puts_unknown_keys_last(self) -> None:
        from plt_optimizer.generate.jobset_report import _job_order

        plan = _plan("jobset_export_batch.yaml")

        assert _job_order(plan, ALPHA) == 0
        assert _job_order(plan, BETA) == 1
        assert _job_order(plan, "Nobody") == len(plan.contexts)

    def test_unmeasured_usage_yields_null_totals(self, batch: Dict[str, Any]) -> None:
        from plt_optimizer.generate.jobset_report import _job_usage_totals

        assert _job_usage_totals(None) == {
            "distance_total": None,
            "time_total": None,
            "weight_total": None,
            "unattributed": 0.0,
        }
        assert _job_usage_totals(batch["usage"])["distance_total"] is not None
        # The batch fixture's specs declare no density (they are parsed without
        # the shop config), so the weight is not estimated - the same "absent is
        # not zero" contract as ``time_total``.
        assert _job_usage_totals(batch["usage"])["weight_total"] is None

    def test_manifest_of_an_unbounded_batch(self, tmp_path: Path) -> None:
        """No declared plates: the pool is auto-allocated, ids are the packer's."""
        plan = _plan("jobset_unbounded_batch.yaml")
        export = _export(plan, tmp_path / "out")
        reports = write_job_reports(plan, export, output_dir=tmp_path / "out")

        manifest = build_manifest(
            plan,
            export,
            output_dir=tmp_path / "out",
            usage_by_job=reports.usage_by_job,
            reports=reports.job_reports,
            root_usage=_root_usage(plan, export),
        )

        assert {row["job_key"] for row in manifest["jobs"]} == {
            context.job_key for context in plan.contexts
        }
        assert manifest["plates"]
        assert all(plate["files"] for plate in manifest["plates"])

    def test_build_manifest_does_not_write(self, batch: Dict[str, Any]) -> None:
        plan, export, out = batch["plan"], batch["export"], batch["out"]
        reports = write_job_reports(plan, export, output_dir=out)

        document = build_manifest(
            plan,
            export,
            output_dir=out,
            usage_by_job=reports.usage_by_job,
            reports=reports.job_reports,
        )

        assert isinstance(document["jobs"], List)
        assert not (out / "generation_report" / MANIFEST_NAME).exists()
