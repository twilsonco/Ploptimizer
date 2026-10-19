"""Unit tests for the profiling-history plotter's data prep.

``scripts/plot_profiling_history.py`` is a script (not an importable
package module), so it is loaded by path with ``importlib`` (mirroring
``tests/test_job_spec_docs.py``). Only the pure data-prep functions are
tested — plotting itself needs matplotlib and is exercised manually.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def plotter() -> Any:
    """Load scripts/plot_profiling_history.py as a module."""
    path = REPO_ROOT / "scripts" / "plot_profiling_history.py"
    spec = importlib.util.spec_from_file_location("plot_profiling_history", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _rows() -> List[Dict[str, str]]:
    """Two sweeps x two specs, with a missing cell and a non-numeric column."""
    return [
        {
            "run_id": "aaaa1111",
            "run_ts": "2026-10-18T12:00:00-06:00",
            "spec": "alpha",
            "wall_s": "68.7",
            "cpu_s": "68.2",
            "peak_mem_mib": "",
            "note": "pre-fix",
        },
        {
            "run_id": "aaaa1111",
            "run_ts": "2026-10-18T12:01:00-06:00",
            "spec": "beta",
            "wall_s": "51.0",
            "cpu_s": "49.9",
            "peak_mem_mib": "31",
            "note": "pre-fix",
        },
        {
            "run_id": "bbbb2222",
            "run_ts": "2026-10-18T14:00:00-06:00",
            "spec": "alpha",
            "wall_s": "3.6",
            "cpu_s": "3.5",
            "peak_mem_mib": "20.4",
            "note": "post-fix",
        },
        {
            "run_id": "bbbb2222",
            "run_ts": "2026-10-18T14:01:00-06:00",
            "spec": "beta",
            "wall_s": "2.7",
            "cpu_s": "1.7",
            "peak_mem_mib": "30.7",
            "note": "post-fix",
        },
    ]


class TestBuildIterations:
    """Sweeps group by run_id and order oldest-first."""

    def test_groups_by_run_id_oldest_first(self, plotter: Any) -> None:
        iterations, labels = plotter.build_iterations(_rows())
        assert iterations == ["aaaa1111", "bbbb2222"]
        assert labels["aaaa1111"].startswith("#1 2026-10-18T12:00")
        assert labels["bbbb2222"].startswith("#2 2026-10-18T14:00")

    def test_backfilled_rows_group_by_run_ts(self, plotter: Any) -> None:
        """Rows without run_id (backfilled history) key on run_ts."""
        rows = [
            {"run_id": "", "run_ts": "2026-10-18T12:17:46", "spec": s, "wall_s": "1"}
            for s in ("alpha", "beta")
        ]
        iterations, _labels = plotter.build_iterations(rows)
        assert iterations == ["2026-10-18T12:17:46"]

    def test_empty_rows(self, plotter: Any) -> None:
        assert plotter.build_iterations([]) == ([], {})


class TestMetricNames:
    """Numeric columns are discovered; key/non-numeric columns are not."""

    def test_discovers_numeric_columns_in_csv_order(self, plotter: Any) -> None:
        assert plotter.metric_names(_rows(), None) == [
            "wall_s",
            "cpu_s",
            "peak_mem_mib",
        ]

    def test_filters_requested(self, plotter: Any) -> None:
        assert plotter.metric_names(_rows(), ["cpu_s"]) == ["cpu_s"]

    def test_unknown_metric_raises(self, plotter: Any) -> None:
        with pytest.raises(SystemExit, match="unknown metric"):
            plotter.metric_names(_rows(), ["nope_s"])


class TestSeriesForMetric:
    """One line per spec, with None for missing cells."""

    def test_one_line_per_spec(self, plotter: Any) -> None:
        iterations, _ = plotter.build_iterations(_rows())
        series = plotter.series_for_metric(_rows(), iterations, "wall_s")
        assert series == {"alpha": [68.7, 3.6], "beta": [51.0, 2.7]}

    def test_missing_cells_are_none(self, plotter: Any) -> None:
        iterations, _ = plotter.build_iterations(_rows())
        series = plotter.series_for_metric(_rows(), iterations, "peak_mem_mib")
        assert series["alpha"] == [None, 20.4]

    def test_last_row_wins_within_a_sweep(self, plotter: Any) -> None:
        """A re-run appended within one sweep supersedes the earlier row."""
        rows = _rows() + [
            {
                "run_id": "bbbb2222",
                "run_ts": "2026-10-18T14:02:00-06:00",
                "spec": "alpha",
                "wall_s": "4.0",
                "cpu_s": "4.0",
                "peak_mem_mib": "",
                "note": "rerun",
            }
        ]
        iterations, _ = plotter.build_iterations(rows)
        series = plotter.series_for_metric(rows, iterations, "wall_s")
        assert series["alpha"] == [68.7, 4.0]


class TestDefaults:
    """The tracked history CSV is the input; the PNGs stay ignored scratch."""

    def test_tracked_csv_and_scratch_plot_dir(self, plotter: Any) -> None:
        assert plotter.DEFAULT_CSV == REPO_ROOT / "profiling" / "summary.csv"
        assert plotter.DEFAULT_PLOT_DIR == REPO_ROOT / "test_output" / "profiling" / "plots"
        args = plotter.build_parser().parse_args([])
        assert args.csv == plotter.DEFAULT_CSV
        assert args.out_dir == plotter.DEFAULT_PLOT_DIR
