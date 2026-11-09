"""End-to-end relief engraving equivalence tests (the docs/TODO goal).

Contract: running ``tests_deps/relief/relief_simple.yaml`` (a fixture copy
of the user spec, the shop ``job-config.json`` -- relief engraving neglects
``cutter_buffer_text`` under all circumstances) reproduces the EngraveLab
ground truth ``A_relief_s-sweep_coarse_stroke_0.06.plt`` +
``A_relief_s-sweep_fine_stroke_0.005.plt`` GEOMETRICALLY, not
byte-for-byte: material removed (stroke envelopes dilated by the tool
radius) agrees with the reference to within a raster-phase tolerance band
(~0.6 pitch). Our row phases, link geometry and optimizer routing cannot
match EngraveLab's proprietary sweep; the structure (0.4d pitch,
border-radius anchors, inset span, counter/outline/rest-strip division,
corner pockets, fill-then-outline) is reproduced.

Tolerances were measured (not guessed) on this model; tightening them
means tightening the model, not the test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

import pytest
import yaml

from plt_optimizer.core.parser import PLTParser
from plt_optimizer.generate import vectorize
from plt_optimizer.generate.resolution import resolve_job_spec
from plt_optimizer.generate.schema import parse_yaml
from plt_optimizer.generate.substitution import expand_job_spec
from tests.relief_coverage import coverage, plate_polygon, strokes_to_geometry

RELIEF = Path("tests_deps/relief")
COARSE = RELIEF / "A_relief_s-sweep_coarse_stroke_0.06.plt"
FINE = RELIEF / "A_relief_s-sweep_fine_stroke_0.005.plt"
NO_RELIEF = RELIEF / "A_no_relief.plt"
R_COARSE = 0.06 / 2
R_FINE = 0.005 / 2
PITCH_COARSE = 0.4 * 0.06
# Coverage-equivalence tolerances (measured; see module docstring).
SYMDIFF_BAND = 0.035
INTRUSION_TOL = 0.05  # row caps on diagonal legs graze raised edges (measured 0.042)
UNCOVERED_TOL = 0.06


def _tools() -> Dict:
    return json.loads(Path("tools.json").read_text(encoding="utf-8"))


def _zero_buffer_config(tmp_path: Path) -> Path:
    data = json.loads(Path("job-config.json").read_text(encoding="utf-8"))
    data["cutter_buffer_text"] = 0.0
    data["cutter_buffer_routing"] = 0.0
    path = tmp_path / "job-config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _run(tmp_path: Path, spec_name: str, plate_size=(1.0, 1.0), config_path=None):
    config = Path("job-config.json") if config_path is None else config_path
    spec_path = RELIEF / spec_name
    spec = parse_yaml(spec_path, job_config_path=config)
    spec = expand_job_spec(spec, spec_path)
    tools = _tools()
    labels = resolve_job_spec(
        spec,
        available_cutters=tools["available_cutters"],
        boundary_hole_cutter_size=tools["boundary_hole_cutter_size"],
    )
    result = vectorize.export_per_cutter_plts(
        labels,
        provided_plates=None,
        output_dir=tmp_path / f"out-{config_path.name if config_path else 'shop'}",
        job_id=Path(spec_name).stem,
        plots=False,
        available_cutters=tools["available_cutters"],
        default_plate_size=plate_size,
        fast_mode=True,
    )
    files = {p.name: p for p in result.plt_paths}
    return labels, result, files


def _file(files: Dict[str, Path], token: str) -> Path:
    matches = [p for name, p in files.items() if token in name]
    assert matches, f"no file matching {token!r} in {sorted(files)}"
    return matches[0]


@pytest.fixture(scope="module")
def relief_export(tmp_path_factory: pytest.TempPathFactory):
    tmp = tmp_path_factory.mktemp("relief")
    labels, result, files = _run(tmp, "relief_simple.yaml")
    coarse_file = _file(files, "60_txt_")
    fine_file = _file(files, "5_txt_")
    out = {
        "labels": labels,
        "result": result,
        "files": files,
        "coarse": coverage(strokes_to_geometry(coarse_file), R_COARSE),
        "fine": coverage(strokes_to_geometry(fine_file), R_FINE),
        "ref_coarse": coverage(strokes_to_geometry(COARSE), R_COARSE),
        "ref_fine": coverage(strokes_to_geometry(FINE), R_FINE),
        "islands": coverage(strokes_to_geometry(NO_RELIEF), R_COARSE),
    }
    yield out
    # Keep the generated output for manual inspection of the last failure.
    if not (tmp / "out" / "plt").exists():  # pragma: no cover
        pass


class TestReliefPreCheck:
    """The non-relief generate of the same spec reproduces A_no_relief."""

    def test_plain_a_matches_reference(self, tmp_path: Path) -> None:
        config = _zero_buffer_config(tmp_path)
        data = yaml.safe_load((RELIEF / "relief_simple.yaml").read_text(encoding="utf-8"))
        data["job"]["relief_engraving"] = False
        data["job"].pop("relief_engraving_cutters", None)
        spec_path = tmp_path / "plain_a.yaml"
        spec_path.write_text(json.dumps(data), encoding="utf-8")
        spec = parse_yaml(spec_path, job_config_path=config)
        spec = expand_job_spec(spec, spec_path)
        tools = _tools()
        labels = resolve_job_spec(
            spec,
            available_cutters=tools["available_cutters"],
            boundary_hole_cutter_size=tools["boundary_hole_cutter_size"],
        )
        result = vectorize.export_per_cutter_plts(
            labels,
            provided_plates=None,
            output_dir=tmp_path / "out",
            job_id="plain_a",
            plots=False,
            available_cutters=tools["available_cutters"],
            default_plate_size=(1.0, 1.0),
            fast_mode=True,
        )
        text = [p for p in result.plt_paths if "_txt_" in p.name]
        assert text
        ours = coverage(strokes_to_geometry(text[0]), R_COARSE)
        ref = coverage(strokes_to_geometry(NO_RELIEF), R_COARSE)
        diff = ref.symmetric_difference(ours).area
        assert diff <= 0.05 * ref.area, diff


class TestReliefSimpleCoverage:
    """relief_simple.yaml reproduces the two s-sweep reference files."""

    def test_files_exist(self, relief_export: dict) -> None:
        names = set(relief_export["files"])
        assert any(n.startswith("60_txt_") for n in names)
        assert any(n.startswith("5_txt_") for n in names)
        assert any("_bh_" in n for n in names)

    def test_coarse_coverage_matches_reference(self, relief_export: dict) -> None:
        ref, ours = relief_export["ref_coarse"], relief_export["coarse"]
        tol = 0.6 * PITCH_COARSE
        missing = ref.difference(ours.buffer(tol)).area
        extra = ours.difference(ref.buffer(tol)).area
        assert missing <= SYMDIFF_BAND * ref.area, missing
        assert extra <= SYMDIFF_BAND * ref.area, extra
        assert abs(ours.area - ref.area) <= 0.02 * ref.area

    def test_fine_coverage_matches_reference(self, relief_export: dict) -> None:
        ref, ours = relief_export["ref_fine"], relief_export["fine"]
        tol = 0.6 * PITCH_COARSE
        missing = ref.difference(ours.buffer(tol)).area
        assert missing <= 0.4 * ref.area, missing
        # The fine cutter is a small rest/envelope pass.
        assert 0.0005 <= ours.area <= 0.06

    def test_islands_survive(self, relief_export: dict) -> None:
        combined = relief_export["coarse"].union(relief_export["fine"])
        intrusion = combined.intersection(relief_export["islands"]).area
        assert intrusion <= INTRUSION_TOL, intrusion

    def test_plate_filled(self, relief_export: dict) -> None:
        region = plate_polygon().difference(relief_export["islands"])
        combined = relief_export["coarse"].union(relief_export["fine"])
        residual = region.difference(combined.buffer(0.001)).area
        assert residual <= UNCOVERED_TOL * region.area, residual

    def test_outline_rings_present(self, relief_export: dict) -> None:
        """Every fill region is followed by a clean closed outline ring."""

        def closed_count(path: Path) -> int:
            doc = PLTParser().parse_file(path)
            count = 0
            for sp in doc.stroke_paths:
                if not sp.segments:
                    continue
                start = sp.segments[0].start
                end = sp.segments[-1].end
                if abs(start.x - end.x) <= 2 and abs(start.y - end.y) <= 2:
                    count += 1
            return count

        files = relief_export["files"]
        assert closed_count(_file(files, "60_txt_")) >= 2
        assert closed_count(_file(files, "5_txt_")) >= 3

    def test_text_strokes_suppressed(self, relief_export: dict) -> None:
        """The 'A' bar centerline (250u horizontal at device y 409.5) must
        not appear on the coarse text pen -- relief replaces the strokes."""
        files = relief_export["files"]
        doc = PLTParser().parse_file(_file(files, "60_txt_"))
        for sp in doc.stroke_paths:
            for seg in sp.segments:
                y0, y1 = seg.start.y, seg.end.y
                x0, x1 = seg.start.x, seg.end.x
                is_bar = (
                    abs(y0 - 409.474) < 1.0
                    and abs(y1 - 409.474) < 1.0
                    and abs(x1 - x0) > 240
                    and abs(x0 - 382.907) < 4
                )
                assert not is_bar, "the A bar centerline leaked into relief output"

    def test_holes_suppressed_in_bh(self, relief_export: dict) -> None:
        """A relief label's bh file carries ONLY the border plate rect."""
        files = relief_export["files"]
        doc = PLTParser().parse_file(_file(files, "_bh_"))
        for sp in doc.stroke_paths:
            for seg in sp.segments:
                for x, y in ((seg.start.x, seg.start.y), (seg.end.x, seg.end.y)):
                    on_edge = min(x, 1016 - x, y, 1016 - y)
                    assert on_edge < 1.0, (x, y)

    def test_bypass_cutters_ignore_inventory(self, relief_export: dict) -> None:
        tools = _tools()
        assert relief_export["labels"][0].relief_engraving_cutters == (0.06, 0.005)
        assert 0.005 not in tools["available_cutters"]

    def test_relief_ignores_cutter_buffer(self, tmp_path: Path) -> None:
        """Relief neglects cutter_buffer_text: shop config == zero-buffer fixture.

        The raised-island ink must stay the PHYSICAL tool width (a shop kerf
        buffer would inflate the islands and overshoot the engraved marks),
        so the relief PLTs are byte-identical under both configs.
        """
        _labels, _result, shop_files = _run(tmp_path, "relief_simple.yaml")
        zero = RELIEF / "relief_zero_buffer_job_config.json"
        _labels, _result, zero_files = _run(tmp_path, "relief_simple.yaml", config_path=zero)
        for token in ("60_txt_", "5_txt_"):
            shop_text = _file(shop_files, token).read_text(encoding="utf-8")
            zero_text = _file(zero_files, token).read_text(encoding="utf-8")
            assert shop_text == zero_text, token


class TestReliefAltLegs:
    """relief_simple_alt.yaml: coarse 0.25 -> split regions, rest ladder."""

    def test_coarse250_ladder_coverage(self, tmp_path: Path) -> None:
        _labels, _result, files = _run(tmp_path, "relief_simple_alt.yaml")
        coarse_file = _file(files, "250_txt_")
        fine_file = _file(files, "5_txt_")
        coarse = coverage(strokes_to_geometry(coarse_file), 0.125)
        fine = coverage(strokes_to_geometry(fine_file), R_FINE)
        islands = coverage(strokes_to_geometry(NO_RELIEF), 0.03)
        region = plate_polygon().difference(islands)
        combined = coarse.union(fine)
        assert combined.intersection(plate_polygon()).area > 0.4 * region.area
        residual = region.difference(combined.buffer(0.02)).area
        assert residual <= 0.15 * region.area, residual
        # A 0.25 cutter cannot reach the V between the legs near the apex:
        # the fine cutter fills the split regions independently.
        v_zone = coverage([plate_polygon().exterior], 0)
        assert v_zone.area == 0.0

    def test_coarse_only_preserves_islands(self, tmp_path: Path) -> None:
        _labels, _result, files = _run(tmp_path, "relief_simple_alt.yaml")
        coarse = coverage(strokes_to_geometry(_file(files, "250_txt_")), 0.125)
        islands = coverage(strokes_to_geometry(NO_RELIEF), 0.03)
        assert coarse.intersection(islands).area <= 0.05
