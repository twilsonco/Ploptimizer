"""Unit tests for the optimize-pipeline profiling harness's summary CSV.

``scripts/profile_optimize.py`` is a script (not an importable package
module), so it is loaded by path with ``importlib`` (mirroring
``tests/test_profile_generate.py``; its own ``sys.path`` bootstrap makes the
``plt_optimizer`` imports inside it work).
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
def harness() -> Any:
    """Load scripts/profile_optimize.py as a module."""
    path = REPO_ROOT / "scripts" / "profile_optimize.py"
    spec = importlib.util.spec_from_file_location("profile_optimize", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _read(path: Path) -> List[Dict[str, str]]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


class TestAppendCsv:
    """optimization_pipeline_summary.csv is a run history: appends, --fresh resets."""

    def test_append_preserves_existing_rows(self, harness: Any, tmp_path: Path) -> None:
        """Existing rows survive a sweep; new rows land after them."""
        csv_path = tmp_path / "optimization_pipeline_summary.csv"
        harness._append_csv(csv_path, [{"run_ts": "t1", "spec": "a", "wall_s": 1.0}])
        preserved = harness._append_csv(csv_path, [{"run_ts": "t2", "spec": "a", "wall_s": 0.5}])

        rows = _read(csv_path)
        assert [r["run_ts"] for r in rows] == ["t1", "t2"]
        assert [r["wall_s"] for r in rows] == ["1.0", "0.5"]
        # The preserved rows are reported back for the console summary.
        assert [r["run_ts"] for r in preserved] == ["t1"]

    def test_fresh_discards_history(self, harness: Any, tmp_path: Path) -> None:
        """``fresh=True`` overwrites the file instead of appending."""
        csv_path = tmp_path / "optimization_pipeline_summary.csv"
        harness._append_csv(csv_path, [{"run_ts": "t1", "spec": "a", "wall_s": 1.0}])
        preserved = harness._append_csv(
            csv_path, [{"run_ts": "t2", "spec": "a", "wall_s": 0.5}], fresh=True
        )

        rows = _read(csv_path)
        assert [r["run_ts"] for r in rows] == ["t2"]
        assert preserved == []

    def test_new_columns_append_after_existing_ones(self, harness: Any, tmp_path: Path) -> None:
        """A sweep with a new stage column extends the header; old rows pad empty."""
        csv_path = tmp_path / "optimization_pipeline_summary.csv"
        harness._append_csv(csv_path, [{"run_ts": "t1", "spec": "a", "wall_s": 1.0}])
        harness._append_csv(
            csv_path, [{"run_ts": "t2", "spec": "a", "wall_s": 0.5, "peak_mem_mib": 12.3}]
        )

        with open(csv_path, newline="") as f:
            header = next(csv.reader(f))
        assert header == ["run_ts", "spec", "wall_s", "peak_mem_mib"]
        rows = _read(csv_path)
        assert rows[0]["peak_mem_mib"] == ""
        assert rows[1]["peak_mem_mib"] == "12.3"


class TestHistoryCsvLocation:
    """The optimize history CSV is TRACKED and adjacent to the generate one."""

    def test_default_history_csv_is_the_tracked_profiling_dir(self, harness: Any) -> None:
        """``profiling/optimization_pipeline_summary.csv`` is committed.

        It lives beside (never inside) the generate pipeline's
        ``summary.csv`` — one append-mode history per pipeline.
        """
        assert harness.DEFAULT_HISTORY_CSV == (
            REPO_ROOT / "profiling" / "optimization_pipeline_summary.csv"
        )

    def test_parser_defaults(self, harness: Any) -> None:
        """No flags: history CSV = tracked path, artifacts = ignored scratch."""
        args = harness.build_parser().parse_args([])
        assert args.history_csv == harness.DEFAULT_HISTORY_CSV
        assert args.output_dir == REPO_ROOT / "test_output" / "optimize_profiling"

    def test_history_csv_flag_overrides(self, harness: Any, tmp_path: Path) -> None:
        """``--history-csv`` relocates the history (worktree baseline captures)."""
        target = tmp_path / "profiling" / "optimization_pipeline_summary.csv"
        args = harness.build_parser().parse_args(["--history-csv", str(target)])
        assert args.history_csv == target


class TestBenchmarkSet:
    """The benchmark PLTs exist and cover both preprocessing branches."""

    def test_benchmark_files_exist(self, harness: Any) -> None:
        """Both benchmark inputs are present in the repo."""
        assert len(harness.BENCHMARK_PLTS) == 2
        for rel in harness.BENCHMARK_PLTS:
            assert (REPO_ROOT / rel).is_file(), rel

    def test_benchmark_covers_structural_fixture_from_tests_deps(self, harness: Any) -> None:
        """The structural benchmark is the frozen tests_deps fixture."""
        structural = [rel for rel in harness.BENCHMARK_PLTS if rel.startswith("tests_deps/")]
        assert structural == ["tests_deps/1x3 half inch letters holes1.plt"]
