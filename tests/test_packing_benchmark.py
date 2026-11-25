"""Tests for ``scripts/packing_benchmark.py`` (layout utilization harness).

The script is a top-level script module (not an importable package member), so
it is loaded by path with ``importlib`` — the same pattern as
``tests/test_run_integration_report.py``.

Two kinds of tests live here:

* Unit tests of the pure helpers (``--set`` overrides, ``tools.json`` loading,
  per-plate/per-job metrics, report formatting, spec collection).
* Metric **invariants** pinned on real fixtures
  (``tests_deps/columns_demo_job.yaml``, ``tests_deps/material_demo_job.yaml``):
  every per-plate number reconciles with the job totals, the envelope never
  exceeds the sheet, and the gap is never negative. U1 (rotation preference)
  and U3 (the ``usage.layout`` report block) are judged by moving these
  numbers, so they must not move on their own.
"""

from __future__ import annotations

import importlib.abc
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from plt_optimizer.generate.packing.layout import PackedLabel, PackedPlate
from plt_optimizer.generate.resolution import ResolvedLabel

REPO_ROOT = Path(__file__).resolve().parents[1]

#: ``measure_plate`` reads geometry off :class:`PackedLabel` only, so the
#: placeholder keeps the type honest without paying for a rendered label.
SOURCE_PLACEHOLDER = cast(ResolvedLabel, object())


@pytest.fixture(scope="module")
def script() -> Any:
    """Load ``scripts/packing_benchmark.py`` as a module."""
    path = REPO_ROOT / "scripts" / "packing_benchmark.py"
    spec = importlib.util.spec_from_file_location("packing_benchmark", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader = spec.loader
    assert isinstance(loader, importlib.abc.InspectLoader)
    loader.exec_module(module)
    return module


def make_label(
    x: float,
    y: float,
    width: float,
    height: float,
    rotated: bool = False,
) -> PackedLabel:
    """Build a placed label at ``(x, y)`` with the given footprint."""
    return PackedLabel(
        label_id=f"label_{x:g}_{y:g}",
        x=x,
        y=y,
        width=width,
        height=height,
        rotated=rotated,
        source_label=SOURCE_PLACEHOLDER,
    )


class TestCoerce:
    """``--set`` value parsing."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("true", True),
            ("FALSE", False),
            ("null", None),
            ("None", None),
            ("7", 7),
            ("-3", -3),
            ("2.5", 2.5),
            ("wk(uv)", "wk(uv)"),
            ("minimize", "minimize"),
            ("[1, 2, 3]", [1, 2, 3]),
            ("[true, false]", [True, False]),
            ("[]", []),
        ],
    )
    def test_parses_scalars_and_lists(self, script: Any, raw: str, expected: Any) -> None:
        assert script._coerce(raw) == expected

    def test_non_numeric_text_stays_a_string(self, script: Any) -> None:
        # Materials, plate ids and sizes are written as plain strings.
        assert script._coerce("wk(uv)") == "wk(uv)"
        assert script._coerce("12x16") == "12x16"
        assert script._coerce("scrap_24x16") == "scrap_24x16"


class TestApplyOverrides:
    """The dotted ``--set`` override path."""

    def test_sets_nested_key_and_creates_missing_key(self, script: Any) -> None:
        raw = {"job": {"layout": "rows"}}
        script.apply_overrides(raw, ["job.layout=columns", "job.rotation_preference=minimize"])
        assert raw["job"]["layout"] == "columns"
        assert raw["job"]["rotation_preference"] == "minimize"

    def test_indexes_into_lists(self, script: Any) -> None:
        raw = {"job": {"plates": [{"id": "a", "width": 12.0}]}}
        script.apply_overrides(raw, ["job.plates.0.width=24", "job.plates.0.material=wk"])
        assert raw["job"]["plates"][0]["width"] == 24
        assert raw["job"]["plates"][0]["material"] == "wk"

    def test_applies_in_order(self, script: Any) -> None:
        raw: dict[str, Any] = {}
        script.apply_overrides(raw, ["job.height=4", "job.height=6"])
        assert raw["job"]["height"] == 6

    @pytest.mark.parametrize(
        ("raw", "override", "message"),
        [
            ({}, "job.layout", "expects key=value"),
            ({"job": {}}, ".layout=rows", "empty key segment"),
            ({"job": {}}, "layout.=rows", "empty key segment"),
            ({"job": {"plates": []}}, "job.plates.3.width=1", "out of range"),
            ({"job": {"plates": ["x"]}}, "job.plates.0.width=1", "cannot assign"),
            ({"job": []}, "job.abc.width=1", "not a list index"),
            ({"job": 5}, "job.width=1", "cannot descend"),
        ],
    )
    def test_rejects_malformed_overrides(
        self, script: Any, raw: dict[str, Any], override: str, message: str
    ) -> None:
        with pytest.raises(ValueError, match=message):
            script.apply_overrides(raw, [override])


class TestLoadToolInventory:
    """``tools.json`` loading."""

    def test_missing_file_selects_ideal_cutters(self, script: Any, tmp_path: Path) -> None:
        assert script.load_tool_inventory(tmp_path / "absent.json") == ([], None)

    def test_reads_inventory_and_hole_cutter(self, script: Any, tmp_path: Path) -> None:
        path = tmp_path / "tools.json"
        path.write_text(
            json.dumps({"available_cutters": [0.04, 0.0625], "boundary_hole_cutter_size": 0.125}),
            encoding="utf-8",
        )
        assert script.load_tool_inventory(path) == ([0.04, 0.0625], 0.125)

    def test_broken_json_raises_value_error(self, script: Any, tmp_path: Path) -> None:
        path = tmp_path / "tools.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="unreadable cutter inventory"):
            script.load_tool_inventory(path)


class TestMeasurePlate:
    """Per-plate utilization, on hand-built geometry."""

    def test_measures_used_envelope_gap_and_rotations(self, script: Any) -> None:
        plate = PackedPlate(
            plate_id="p1",
            width=10.0,
            height=10.0,
            material="wk",
            labels=[
                make_label(0.0, 0.0, 4.0, 2.0),  # 8 in²
                make_label(4.0, 0.0, 2.0, 4.0, rotated=True),  # 8 in²
            ],
        )
        row = script.measure_plate(plate)
        assert row.count == 2
        assert row.used_sq_in == pytest.approx(16.0)
        # Envelope spans x 0..6, y 0..4 → 24 in², so 8 in² sit inside the
        # envelope with no label on them.
        assert (row.bbox_w_in, row.bbox_h_in) == (6.0, 4.0)
        assert row.gap_sq_in == pytest.approx(8.0)
        assert row.rotated == 1
        assert row.material_sq_in == pytest.approx(100.0)
        assert row.coverage_pct == pytest.approx(16.0)
        assert not row.is_empty

    def test_clearance_strips_are_material_not_envelope(self, script: Any) -> None:
        plate = PackedPlate(
            plate_id="p1",
            width=10.0,
            height=10.0,
            left_clearance=1.0,
            top_clearance=0.5,
            labels=[make_label(0.0, 0.0, 2.0, 2.0)],
        )
        row = script.measure_plate(plate)
        # The shop buys 11 × 10.5 and engraves the 10 × 10 usable area.
        assert row.material_sq_in == pytest.approx(115.5)
        assert row.coverage_pct == pytest.approx(4 / 115.5 * 100)
        assert row.bbox_sq_in == pytest.approx(4.0)

    def test_tiled_labels_report_a_zero_gap_not_negative_zero(self, script: Any) -> None:
        """Labels that tile the envelope exactly must not print "-0.00"."""
        plate = PackedPlate(
            plate_id="p3",
            width=24.0,
            height=16.0,
            labels=[make_label(0.0, 0.0, 4.0, 6.0), make_label(4.0, 0.0, 4.0, 6.0)],
        )
        row = script.measure_plate(plate)
        assert row.gap_sq_in == 0.0
        assert str(row.gap_sq_in) == "0.0"

    def test_empty_plate_is_a_full_sheet_of_waste(self, script: Any) -> None:
        row = script.measure_plate(PackedPlate(plate_id="p2", width=24.0, height=16.0))
        assert row.is_empty
        assert (row.count, row.used_sq_in, row.bbox_sq_in) == (0, 0.0, 0.0)
        assert row.gap_sq_in == 0.0
        assert row.coverage_pct == 0.0
        assert row.material_sq_in == pytest.approx(384.0)
        assert row.material == "(unset)"


def synthetic_job(layout: str = "columns") -> Any:
    """The minimum ``PreparedJob`` shape :func:`measure_job` reads."""
    return SimpleNamespace(
        name="synthetic",
        job=SimpleNamespace(layout=SimpleNamespace(value=layout)),
    )


class TestMeasureJob:
    """Job totals must be the sum of the plates (U1/U3 compare on this)."""

    def test_totals_reconcile_with_plates(
        self, script: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plates = [
            PackedPlate(
                plate_id="p1",
                width=10.0,
                height=10.0,
                material="wk",
                labels=[make_label(0.0, 0.0, 4.0, 2.0), make_label(4.0, 0.0, 2.0, 4.0, True)],
            ),
            PackedPlate(plate_id="p2", width=10.0, height=10.0, material="wk"),
        ]
        monkeypatch.setattr(script, "pack", lambda prepared, mode, **kw: plates)
        metrics = script.measure_job(synthetic_job(), None, spec_name="synthetic.yaml")

        rows = metrics.plates
        assert metrics.labels == sum(row.count for row in rows) == 2
        assert metrics.used_sq_in == pytest.approx(sum(r.used_sq_in for r in rows))
        assert metrics.bbox_sq_in == pytest.approx(sum(r.bbox_sq_in for r in rows))
        assert metrics.gap_sq_in == pytest.approx(metrics.bbox_sq_in - metrics.used_sq_in)
        assert metrics.sheet_sq_in == pytest.approx(sum(r.material_sq_in for r in rows))
        assert metrics.coverage_pct == pytest.approx(metrics.used_sq_in / metrics.sheet_sq_in * 100)
        assert metrics.rotated == sum(r.rotated for r in rows) == 1
        assert metrics.empty_plates == 1
        assert (metrics.plate_w_in, metrics.plate_h_in) == (10.0, 10.0)

    def test_json_payload_is_serialisable(
        self, script: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plates = [
            PackedPlate(
                plate_id="p1",
                width=10.0,
                height=10.0,
                material="wk",
                labels=[make_label(0.0, 0.0, 4.0, 2.0)],
            )
        ]
        monkeypatch.setattr(script, "pack", lambda prepared, mode, **kw: plates)
        metrics = script.measure_job(synthetic_job(), None, spec_name="synthetic.yaml")
        payload = json.loads(json.dumps(metrics.to_dict()))
        assert payload["spec"] == "synthetic.yaml"
        assert payload["plates"][0]["plate_id"] == "p1"


class TestFormatJob:
    """The report block a human reads."""

    def test_lists_every_plate_and_a_total(
        self, script: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plates = [
            PackedPlate(
                plate_id="p1",
                width=10.0,
                height=10.0,
                material="wk",
                labels=[make_label(0.0, 0.0, 4.0, 2.0, True)],
            ),
            PackedPlate(plate_id="p2", width=10.0, height=10.0, material="wk(uv)"),
        ]
        monkeypatch.setattr(script, "pack", lambda prepared, mode, **kw: plates)
        metrics = script.measure_job(synthetic_job(), "columns", spec_name="synthetic.yaml")
        text = script.format_job(metrics)
        assert "p1" in text and "wk(uv)" in text
        assert "EMPTY" in text
        assert "TOTAL" in text
        assert "rotated=1" in text
        assert "mode=columns" in text


class TestCollectSpecs:
    """Path arguments expand to spec YAMLs, in order, without duplicates."""

    def test_expands_directories_and_skips_other_files(self, script: Any, tmp_path: Path) -> None:
        (tmp_path / "b.yaml").write_text("job: {}", encoding="utf-8")
        (tmp_path / "a.yml").write_text("job: {}", encoding="utf-8")
        (tmp_path / "notes.md").write_text("x", encoding="utf-8")
        nested = tmp_path / "nested"
        nested.mkdir()
        (nested / "deep.yaml").write_text("job: {}", encoding="utf-8")

        found = script.collect_specs([tmp_path, tmp_path / "b.yaml"])
        # Sorted, top level only, YAML only, the explicit file deduped.
        assert [path.name for path in found] == ["a.yml", "b.yaml"]

    def test_empty_directory_yields_nothing(self, script: Any, tmp_path: Path) -> None:
        assert script.collect_specs([tmp_path / "missing"]) == []


class TestMainArgumentValidation:
    """Fail fast on a bad command line, before the pipeline runs."""

    def test_unknown_mode_exits_2(self, script: Any, tmp_path: Path) -> None:
        assert script.main(["--modes", "diagonals", str(tmp_path)]) == 2

    def test_no_specs_exits_1(
        self, script: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert script.main([str(tmp_path)]) == 1
        assert "No job specs found" in capsys.readouterr().err


class TestEndToEnd:
    """Real fixture YAMLs through the production layout path."""

    def test_single_material_job_fits_one_plate(self, script: Any) -> None:
        prepared = script.prepare(REPO_ROOT / "tests_deps" / "columns_demo_job.yaml")
        metrics = script.measure_job(
            prepared,
            None,
            spec_name="columns_demo_job.yaml",
            config_path=REPO_ROOT / "job-config.json",
            tools_path=REPO_ROOT / "tools.json",
        )
        # The fixture's own contract: 16 × 3"×1" labels on one 24"×16" sheet.
        assert metrics.labels == 16
        assert len(metrics.plates) == 1
        assert metrics.mode == "columns"
        self._assert_invariants(script, metrics)
        # 16 labels at 3 × 1 = 48 in² on a 384 in² sheet.
        assert metrics.used_sq_in == pytest.approx(48.0)
        assert metrics.coverage_pct == pytest.approx(48.0 / 384.0 * 100)

    def test_material_partition_opens_one_plate_per_material(self, script: Any) -> None:
        prepared = script.prepare(REPO_ROOT / "tests_deps" / "material_demo_job.yaml")
        metrics = script.measure_job(
            prepared,
            None,
            spec_name="material_demo_job.yaml",
            config_path=REPO_ROOT / "job-config.json",
            tools_path=REPO_ROOT / "tools.json",
        )
        # 8 wk tags + 4 wk(uv) placards: the packer never shares a sheet
        # across materials, so each material owns its 12"×8" plate.
        assert {row.material for row in metrics.plates} == {"wk", "wk(uv)"}
        assert metrics.labels == 12
        assert len(metrics.plates) == 2
        self._assert_invariants(script, metrics)

    def test_mode_override_is_applied_to_plates(self, script: Any) -> None:
        prepared = script.prepare(REPO_ROOT / "tests_deps" / "columns_demo_job.yaml")
        metrics = script.measure_job(
            prepared,
            "rows-simple",
            spec_name="columns_demo_job.yaml",
            config_path=REPO_ROOT / "job-config.json",
            tools_path=REPO_ROOT / "tools.json",
        )
        assert metrics.mode == "rows-simple"
        assert metrics.labels == 16
        self._assert_invariants(script, metrics)

    def test_set_override_reaches_the_pipeline(self, script: Any) -> None:
        path = REPO_ROOT / "tests_deps" / "columns_demo_job.yaml"
        prepared = script.prepare(
            path,
            ["job.width=6.0", "job.height=2.0"],
            config_path=REPO_ROOT / "job-config.json",
            tools_path=REPO_ROOT / "tools.json",
        )
        assert prepared.job is not None
        assert prepared.job.width == 6.0
        assert prepared.job.height == 2.0
        # The throwaway YAML the override is written through is cleaned up.
        assert not list(path.parent.glob(f"{path.stem}.*.tmp.yaml"))

    @staticmethod
    def _assert_invariants(script: Any, metrics: Any) -> None:
        """Every per-plate number is bounded and sums to the job totals."""
        rows = metrics.plates
        assert rows
        for row in rows:
            assert 0.0 <= row.coverage_pct <= 100.0
            assert 0.0 <= row.used_sq_in <= row.material_sq_in
            assert 0.0 <= row.bbox_sq_in <= row.used_sq_in + row.gap_sq_in
            assert row.gap_sq_in >= -1e-9
            assert 0 <= row.rotated <= row.count
        assert metrics.used_sq_in == pytest.approx(sum(r.used_sq_in for r in rows))
        assert metrics.bbox_sq_in == pytest.approx(sum(r.bbox_sq_in for r in rows))
        assert metrics.gap_sq_in == pytest.approx(metrics.bbox_sq_in - metrics.used_sq_in)
        assert metrics.sheet_sq_in == pytest.approx(sum(r.material_sq_in for r in rows))
        assert metrics.labels == sum(r.count for r in rows)
        assert metrics.empty_plates == sum(1 for r in rows if r.count == 0)
