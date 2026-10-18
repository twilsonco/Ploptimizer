"""Unit tests for the profiling harness's summary-CSV history.

``scripts/profile_generate.py`` is a script (not an importable package
module), so it is loaded by path with ``importlib`` (mirroring
``tests/test_job_spec_docs.py``; its own ``sys.path`` bootstrap makes the
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
    """Load scripts/profile_generate.py as a module."""
    path = REPO_ROOT / "scripts" / "profile_generate.py"
    spec = importlib.util.spec_from_file_location("profile_generate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _read(path: Path) -> List[Dict[str, str]]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


class TestAppendCsv:
    """summary.csv is a run history: sweeps append, --fresh resets."""

    def test_append_preserves_existing_rows(self, harness: Any, tmp_path: Path) -> None:
        """Existing rows survive a sweep; new rows land after them."""
        csv_path = tmp_path / "summary.csv"
        harness._append_csv(csv_path, [{"run_ts": "t1", "spec": "a", "wall_s": 1.0}])
        preserved = harness._append_csv(
            csv_path, [{"run_ts": "t2", "spec": "a", "wall_s": 0.5}]
        )

        rows = _read(csv_path)
        assert [r["run_ts"] for r in rows] == ["t1", "t2"]
        assert [r["wall_s"] for r in rows] == ["1.0", "0.5"]
        # The preserved rows are reported back for the console summary.
        assert [r["run_ts"] for r in preserved] == ["t1"]

    def test_fresh_discards_history(self, harness: Any, tmp_path: Path) -> None:
        """``fresh=True`` overwrites the file instead of appending."""
        csv_path = tmp_path / "summary.csv"
        harness._append_csv(csv_path, [{"run_ts": "t1", "spec": "a", "wall_s": 1.0}])
        preserved = harness._append_csv(
            csv_path, [{"run_ts": "t2", "spec": "a", "wall_s": 0.5}], fresh=True
        )

        rows = _read(csv_path)
        assert [r["run_ts"] for r in rows] == ["t2"]
        assert preserved == []

    def test_new_columns_append_after_existing_ones(self, harness: Any, tmp_path: Path) -> None:
        """A sweep with a new stage column extends the header; old rows pad empty.

        Backfilled history rows (e.g. a pre-fix baseline captured without the
        memory run) lack newer keys; the union column order keeps every sweep
        readable in one file.
        """
        csv_path = tmp_path / "summary.csv"
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

    def test_run_ts_is_the_first_column(self, harness: Any, tmp_path: Path) -> None:
        """The datetime stamp leads the row so history sorts by time."""
        csv_path = tmp_path / "summary.csv"
        harness._append_csv(csv_path, [{"run_ts": "t1", "spec": "a", "wall_s": 1.0}])
        with open(csv_path, newline="") as f:
            header = next(csv.reader(f))
        assert header[0] == "run_ts"
