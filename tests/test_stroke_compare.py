"""Tests for the strict stroke-level PLT comparison (diagnostics/stroke_compare.py).

Synthetic HPGL files exercise every classification: identical pairs, shifted
rows, deleted rows (missing), added rows (extra), arc-vs-chord equivalence,
empty files, and the script CLI wrapper (exit codes + JSON output).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Sequence

import pytest

from plt_optimizer.diagnostics.stroke_compare import (
    PLOTTER_UNIT,
    compare_files,
    parse_strokes,
)
from scripts.compare_relief_strokes import main


def _write_plt(path: Path, commands: Sequence[str]) -> Path:
    """Write a minimal single-pen PLT from command bodies (no trailing ';')."""
    body = ";".join(commands)
    path.write_text(f"IN;PA;SP1;{body};SP;\n", encoding="utf-8", newline="\n")
    return path


def _row(y_units: float, x0: float = 100.0, x1: float = 900.0) -> str:
    """One horizontal cutting row at ``y_units`` (plotter units)."""
    return f"PU{x0},{y_units};PD{x1},{y_units}"


U = 1016.0  # plotter units per inch


class TestParseStrokes:
    """Stroke extraction: splitting, units, arcs."""

    def test_rows_are_strokes(self, tmp_path: Path) -> None:
        path = _write_plt(tmp_path / "a.plt", [_row(100.0), _row(200.0)])
        strokes = parse_strokes(path)
        assert len(strokes) == 2
        assert strokes[0].bounds[1] == pytest.approx(100.0 / U, abs=1e-4)
        assert strokes[0].length == pytest.approx(800.0 / U, abs=1e-3)
        assert not strokes[0].has_arc

    def test_arc_flag_and_flattening(self, tmp_path: Path) -> None:
        """A 180-degree AA flattens onto the circle the chord chain follows."""
        ref = _write_plt(tmp_path / "arc.plt", ["PU200,300", "PD;AA300,300,180.0"])
        strokes = parse_strokes(ref)
        assert len(strokes) == 1
        assert strokes[0].has_arc
        cx, cy = 300.0 / U, 300.0 / U
        radius = 100.0 / U
        for x, y in strokes[0].points:
            assert abs(((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 - radius) < 1e-3

    def test_empty_file(self, tmp_path: Path) -> None:
        path = _write_plt(tmp_path / "e.plt", [])
        assert parse_strokes(path) == []


class TestCompare:
    """Bidirectional chamfer classification."""

    def test_identical_files_pass(self, tmp_path: Path) -> None:
        commands = [_row(100.0), _row(200.0), _row(300.0)]
        a = _write_plt(tmp_path / "a.plt", commands)
        b = _write_plt(tmp_path / "b.plt", commands)
        report = compare_files(a, b)
        assert report.missing == []
        assert report.extra == []
        assert report.exit_code == 0
        mean, maximum = report.matched_error
        assert mean == 0.0 and maximum == 0.0

    def test_deleted_row_is_missing(self, tmp_path: Path) -> None:
        ref = _write_plt(tmp_path / "ref.plt", [_row(100.0), _row(200.0), _row(300.0)])
        ours = _write_plt(tmp_path / "ours.plt", [_row(100.0), _row(300.0)])
        report = compare_files(ours, ref)
        assert len(report.missing) == 1
        assert report.missing[0].index == 1  # the middle row
        assert report.missing[0].mean_distance > 0.05
        assert report.extra == []
        assert report.exit_code == 1

    def test_added_row_is_extra(self, tmp_path: Path) -> None:
        ref = _write_plt(tmp_path / "ref.plt", [_row(100.0), _row(300.0)])
        ours = _write_plt(tmp_path / "ours.plt", [_row(100.0), _row(200.0), _row(300.0)])
        report = compare_files(ours, ref)
        assert report.missing == []
        assert len(report.extra) == 1
        assert report.extra[0].index == 1

    def test_shifted_row_high_error_matched(self, tmp_path: Path) -> None:
        # Tolerance widened to 0.03: the 0.02 shift stays "matched" with a
        # visible per-stroke error (this is the stroke-error report surface).
        ref = _write_plt(tmp_path / "ref.plt", [_row(100.0)])
        ours = _write_plt(tmp_path / "ours.plt", [_row(100.0 + 0.02 * U)])
        strict = compare_files(ours, ref)
        assert len(strict.missing) == 1 and len(strict.extra) == 1
        loose = compare_files(ours, ref, tol=0.03)
        assert loose.missing == [] and loose.extra == []
        assert loose.matched_error[0] > 0.01

    def test_arc_matches_dense_chords(self, tmp_path: Path) -> None:
        """An AA arc and a chord chain of the same circle match within tol."""
        ref = _write_plt(tmp_path / "arc.plt", ["PU200,300", "PD;AA300,300,180.0"])
        n = 40
        chord = ["PU200,300"]
        pts = []
        for i in range(1, n + 1):
            angle = math.pi * i / n
            pts.append(f"{300 - 100 * math.cos(angle):.3f},{300 - 100 * math.sin(angle):.3f}")
        chord.append("PD" + ";PD".join(pts))
        ours = _write_plt(tmp_path / "chords.plt", chord)
        report = compare_files(ours, ref)
        assert report.missing == []
        assert report.extra == []

    def test_empty_ours_all_missing(self, tmp_path: Path) -> None:
        ref = _write_plt(tmp_path / "ref.plt", [_row(100.0), _row(200.0)])
        ours = _write_plt(tmp_path / "ours.plt", [])
        report = compare_files(ours, ref)
        assert len(report.missing) == 2
        assert len(report.extra) == 0

    def test_json_round_trip(self, tmp_path: Path) -> None:
        ref = _write_plt(tmp_path / "ref.plt", [_row(100.0)])
        ours = _write_plt(tmp_path / "ours.plt", [])
        report = compare_files(ours, ref)
        data = json.loads(json.dumps(report.to_dict()))
        assert data["counts"]["missing"] == 1
        assert data["reference_strokes"][0]["mean_distance"] is None  # inf -> null


class TestScriptMain:
    """scripts/compare_relief_strokes.py main() contract."""

    def test_pass_exit_zero(self, tmp_path: Path) -> None:
        commands = [_row(100.0), _row(200.0)]
        a = _write_plt(tmp_path / "a.plt", commands)
        b = _write_plt(tmp_path / "b.plt", commands)
        assert main(["--ours", str(a), "--ref", str(b)]) == 0

    def test_fail_exit_one_with_json(self, tmp_path: Path) -> None:
        ref = _write_plt(tmp_path / "ref.plt", [_row(100.0), _row(200.0)])
        ours = _write_plt(tmp_path / "ours.plt", [_row(100.0)])
        out = tmp_path / "r.json"
        assert main(["--ours", str(ours), "--ref", str(ref), "--json", str(out)]) == 1
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["counts"]["missing"] == 1
        assert data["tol_inches"] == PLOTTER_UNIT

    def test_missing_file_exit_two(self, tmp_path: Path) -> None:
        ref = _write_plt(tmp_path / "ref.plt", [_row(100.0)])
        assert main(["--ours", str(tmp_path / "nope.plt"), "--ref", str(ref)]) == 2
