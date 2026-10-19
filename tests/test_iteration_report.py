"""Unit tests for the optimization-loop iteration report.

``.github/skills/pipeline-optimization/scripts/iteration_report.py``
is a script (not an importable package module), so it is loaded by path with
``importlib`` (mirroring ``tests/test_job_spec_docs.py``).
"""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def report() -> Any:
    """Load the iteration_report.py script as a module."""
    path = (
        REPO_ROOT
        / ".github"
        / "skills"
        / "pipeline-optimization"
        / "scripts"
        / "iteration_report.py"
    )
    spec = importlib.util.spec_from_file_location("iteration_report", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _rows() -> List[Dict[str, str]]:
    """Two sweeps x two specs with wall + stage columns."""
    return [
        {
            "run_id": "aaaa1111",
            "run_ts": "2026-10-18T10:00:00-06:00",
            "spec": "alpha",
            "wall_s": "68.7",
            "stage_reflow_s": "66.7",
            "stage_optimize_layer_s": "0.5",
        },
        {
            "run_id": "aaaa1111",
            "run_ts": "2026-10-18T10:01:00-06:00",
            "spec": "beta",
            "wall_s": "51.0",
            "stage_reflow_s": "0.0",
            "stage_optimize_layer_s": "1.3",
        },
        {
            "run_id": "bbbb2222",
            "run_ts": "2026-10-18T14:00:00-06:00",
            "spec": "alpha",
            "wall_s": "3.6",
            "stage_reflow_s": "2.1",
            "stage_optimize_layer_s": "0.6",
        },
        {
            "run_id": "bbbb2222",
            "run_ts": "2026-10-18T14:01:00-06:00",
            "spec": "beta",
            "wall_s": "2.7",
            "stage_reflow_s": "0.0",
            "stage_optimize_layer_s": "1.3",
        },
    ]


class TestIterationIds:
    def test_oldest_first(self, report: Any) -> None:
        assert report.iteration_ids(_rows()) == ["aaaa1111", "bbbb2222"]

    def test_run_ts_fallback(self, report: Any) -> None:
        rows = [{"run_id": "", "run_ts": "2026-10-18T12:17:46", "spec": "a", "wall_s": "1"}]
        assert report.iteration_ids(rows) == ["2026-10-18T12:17:46"]


class TestRowsBySpec:
    def test_filters_to_one_sweep(self, report: Any) -> None:
        by_spec = report.rows_by_spec(_rows(), "aaaa1111")
        assert set(by_spec) == {"alpha", "beta"}
        assert by_spec["alpha"]["wall_s"] == "68.7"

    def test_last_row_wins_within_sweep(self, report: Any) -> None:
        rows = _rows() + [
            {
                "run_id": "bbbb2222",
                "run_ts": "2026-10-18T14:02:00-06:00",
                "spec": "alpha",
                "wall_s": "4.0",
                "stage_reflow_s": "2.5",
                "stage_optimize_layer_s": "0.6",
            }
        ]
        assert report.rows_by_spec(rows, "bbbb2222")["alpha"]["wall_s"] == "4.0"


class TestGains:
    def test_sorted_by_biggest_drop(self, report: Any) -> None:
        baseline = report.rows_by_spec(_rows(), "aaaa1111")
        target = report.rows_by_spec(_rows(), "bbbb2222")
        out = report.gains(baseline, target, "wall_s")
        assert [spec for spec, _b, _a in out] == ["alpha", "beta"]
        assert out[0] == ("alpha", 68.7, 3.6)

    def test_skips_specs_missing_from_either_side(self, report: Any) -> None:
        baseline = report.rows_by_spec(_rows(), "aaaa1111")
        target = {"gamma": {"wall_s": "1.0"}}
        assert report.gains(baseline, target, "wall_s") == []

    def test_skips_nonnumeric_cells(self, report: Any) -> None:
        baseline = {"alpha": {"wall_s": ""}}
        target = {"alpha": {"wall_s": "1.0"}}
        assert report.gains(baseline, target, "wall_s") == []


class TestStageDeltas:
    def test_ranked_by_baseline_cost(self, report: Any) -> None:
        baseline = report.rows_by_spec(_rows(), "aaaa1111")
        target = report.rows_by_spec(_rows(), "bbbb2222")
        out = report.stage_deltas(baseline, target, top=2)
        assert out[0] == ("alpha", "stage_reflow_s", 66.7, 2.1)
        assert len(out) == 2


class TestFormatReport:
    def test_includes_per_row_and_total(self, report: Any) -> None:
        baseline = report.rows_by_spec(_rows(), "aaaa1111")
        target = report.rows_by_spec(_rows(), "bbbb2222")
        text = report.format_report(
            "aaaa1111", "bbbb2222", "wall_s", report.gains(baseline, target, "wall_s"), []
        )
        assert "alpha" in text and "68.70" in text and "3.60" in text
        assert "TOTAL" in text
        # TOTAL: 119.7 -> 6.3 = -113.4 (-94.7%)
        assert "-113.40" in text and "-94.7%" in text


class TestMain:
    def test_end_to_end_on_a_temp_csv(self, report: Any, tmp_path: Path) -> None:
        csv_path = tmp_path / "summary.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(_rows()[0].keys()))
            writer.writeheader()
            writer.writerows(_rows())
        assert report.main(["--csv", str(csv_path)]) == 0

    def test_single_sweep_exits_with_hint(self, report: Any, tmp_path: Path) -> None:
        csv_path = tmp_path / "summary.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(_rows()[0].keys()))
            writer.writeheader()
            writer.writerows(_rows()[:2])
        with pytest.raises(SystemExit, match="at least two sweeps"):
            report.main(["--csv", str(csv_path)])

    def test_unknown_metric_exits(self, report: Any, tmp_path: Path) -> None:
        csv_path = tmp_path / "summary.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(_rows()[0].keys()))
            writer.writeheader()
            writer.writerows(_rows())
        with pytest.raises(SystemExit, match="unknown metric"):
            report.main(["--csv", str(csv_path), "--metric", "nope_s"])
