"""Unit tests for the relief stage loop (relief/pipeline.py)."""

from __future__ import annotations

import math
from typing import List

import pytest

from plt_optimizer.generate.relief.cutters import ReliefCutter
from plt_optimizer.generate.relief.pipeline import (
    generate_relief_passes,
    pen_widths_for_label,
)
from plt_optimizer.generate.relief.regions import build_engrave_regions


class _Logger:
    def __init__(self) -> None:
        self.warnings: List[str] = []
        self.infos: List[str] = []

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def info(self, message: str) -> None:
        self.infos.append(message)


SQUARE_RING = [(0.4, -0.1), (0.6, -0.1), (0.6, 0.1), (0.4, 0.1), (0.4, -0.1)]
BOX = [(0.0, 0.0), (0.0, 1.0)]


class TestGenerateReliefPasses:
    """Stage model: fill-then-outline, coverage, rest strips."""

    def _regions(self):
        return build_engrave_regions(1.0, 1.0, [(SQUARE_RING, 0.1), (BOX, 0.1)], False)

    def test_single_cutter_fills_and_outlines(self) -> None:
        regions = self._regions()
        passes = generate_relief_passes(regions, [ReliefCutter(0.06, 0.06)])
        assert len(passes) == 1
        strokes = passes[0].strokes
        fills = [s for s in strokes if not s.is_outline]
        outlines = [s for s in strokes if s.is_outline]
        assert fills and outlines
        # Fill strokes precede the outlines within a pass.
        kinds = [s.is_outline for s in strokes]
        assert kinds == sorted(kinds)
        # Outlines are closed rings.
        for outline in outlines:
            bounds = outline.stroke.bounds()
            assert bounds is not None
            pts = [outline.stroke.pen_up] + [seg.end for seg in outline.stroke.segments]
            assert math.isclose(pts[0].real, pts[-1].real, abs_tol=1e-9)
            assert math.isclose(pts[0].imag, pts[-1].imag, abs_tol=1e-9)

    def test_fine_cleans_coarse_rest(self) -> None:
        regions = self._regions()
        ladder = [ReliefCutter(0.06, 0.06), ReliefCutter(0.005, 0.005)]
        passes = generate_relief_passes(regions, ladder)
        assert len(passes) == 2
        # The fine cutter's fill pitch is 0.4 * 0.005 = 0.002.
        fine = passes[1]
        ys = sorted({c.imag for s in fine.strokes if not s.is_outline for c in _stroke_points(s)})
        diffs = {round(b - a, 6) for a, b in zip(ys, ys[1:]) if b - a > 1e-9}
        assert any(math.isclose(d, 0.002, abs_tol=1e-4) for d in diffs)

    def test_cutter_larger_than_the_label(self) -> None:
        # A cutter whose radius erosion empties every component produces
        # no geometry at all (the label is untouched, no pass emitted).
        regions = self._regions()
        passes = generate_relief_passes(regions, [ReliefCutter(4.0, 4.0)])
        assert passes == []

    def test_residual_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from plt_optimizer.generate.relief import pipeline as pipeline_module

        monkeypatch.setattr(pipeline_module, "_RESIDUAL_TOLERANCE", 1e-9)
        # A hair-thin counter the ladder cannot fully cover leaves skin,
        # which at a microscopic tolerance triggers the warning.
        outer = [(0.3, -0.2), (0.7, -0.2), (0.7, 0.2), (0.3, 0.2), (0.3, -0.2)]
        inner = [(0.495, -0.005), (0.505, -0.005), (0.505, 0.005), (0.495, 0.005), (0.495, -0.005)]
        regions = build_engrave_regions(1.0, 1.0, [(outer, 0.1), (inner, 0.1)], False)
        logger = _Logger()
        generate_relief_passes(
            regions, [ReliefCutter(0.06, 0.06), ReliefCutter(0.005, 0.005)], logger=logger
        )
        assert any("unengraved" in w for w in logger.warnings)

    def test_empty_regions_no_passes(self) -> None:
        regions = build_engrave_regions(1.0, 1.0, [], True, border_band=1.0)
        assert generate_relief_passes(regions, [ReliefCutter(0.06, 0.06)]) == []

    def test_overlap_percent_scales_pitch(self) -> None:
        regions = self._regions()
        passes = generate_relief_passes(regions, [ReliefCutter(0.06, 0.06)], overlap_percent=50.0)
        # pitch = 0.06 * (1 - 50/100) = 0.03.
        ys = sorted(
            {c.imag for s in passes[0].strokes if not s.is_outline for c in _stroke_points(s)}
        )
        diffs = {round(b - a, 6) for a, b in zip(ys, ys[1:]) if b - a > 1e-9}
        assert any(math.isclose(d, 0.03, abs_tol=1e-4) for d in diffs)

    def test_overlap_percent_bounds_raise(self) -> None:
        regions = self._regions()
        for bad in (100.0, -1.0):
            with pytest.raises(ValueError, match="overlap"):
                generate_relief_passes(regions, [ReliefCutter(0.06, 0.06)], overlap_percent=bad)

    def test_fine_never_recuts_the_swept_counter_center(self) -> None:
        """Counters are REST-ONLY at fine stages (user-reported waste).

        The coarse pass sweeps the counter core fully (rows + outline,
        dilated); the fine must find NOTHING to cut mid-counter - it
        engraves only corner pockets the coarse cannot reach. The old
        full-core re-clean model filled the whole hole twice, wasting
        time where the coarse cutter already cleared.
        """
        box = [(0.0, 0.0), (0.0, 1.0)]
        outer = [(0.2, -0.12), (0.8, -0.12), (0.8, 0.12), (0.2, 0.12), (0.2, -0.12)]
        hole = [(0.3, -0.06), (0.7, -0.06), (0.7, 0.06), (0.3, 0.06), (0.3, -0.06)]
        regions = build_engrave_regions(1.0, 1.0, [(outer, 0.1), (hole, 0.1), (box, 0.1)], False)
        passes = generate_relief_passes(
            regions, [ReliefCutter(0.06, 0.06), ReliefCutter(0.005, 0.005)]
        )
        fine = passes[1]
        for stroke in fine.strokes:
            if stroke.is_outline:
                continue
            pts = [(c.real, c.imag) for c in _stroke_points(stroke)]
            xs = [x for x, _y in pts]
            ys = [y for _x, y in pts]
            inside_counter = (
                min(xs) > 0.32 and max(xs) < 0.68 and min(ys) > -0.05 and max(ys) < 0.05
            )
            assert not inside_counter, "fine fill re-cut the coarse-swept counter center"
        # The coarse pass DOES fill the counter (control).
        coarse_in = [
            s
            for s in passes[0].strokes
            if not s.is_outline
            and all(0.32 < c.real < 0.68 for c in _stroke_points(s))
            and all(-0.05 < c.imag < 0.05 for c in _stroke_points(s))
        ]
        assert coarse_in, "coarse must fill the counter"

    def test_staged_coverage_includes_outline_sweep(self) -> None:
        """Coverage = fill AND outline envelopes (the proven model).

        The fine pass engraves only material nothing swept: inside the
        coarse core (fully swept) the fine fills nothing; its rest is the
        corners/pockets/skins outside the coarse envelope. (A fill-only
        coverage model over-engraves 32x the reference fine area --
        measured, rejected.)
        """
        outer = [(0.3, -0.2), (0.7, -0.2), (0.7, 0.2), (0.3, 0.2), (0.3, -0.2)]
        inner = [(0.45, -0.05), (0.55, -0.05), (0.55, 0.05), (0.45, 0.05), (0.45, -0.05)]
        regions = build_engrave_regions(1.0, 1.0, [(outer, 0.1), (inner, 0.1)], False)
        passes = generate_relief_passes(
            regions, [ReliefCutter(0.06, 0.06), ReliefCutter(0.005, 0.005)]
        )
        fine = passes[1]
        # The coarse swept the whole counter core (0.48..0.52 at center
        # height): a horizontal fine row fully inside the swept core
        # interior must not exist.
        for stroke in fine.strokes:
            if stroke.is_outline:
                continue
            pts = [(c.real, c.imag) for c in _stroke_points(stroke)]
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                if abs(y1 - y0) < 1e-9 and -0.04 < y0 < 0.04:
                    lo, hi = min(x0, x1), max(x0, x1)
                    assert not (0.485 < lo and hi < 0.515), (lo, hi, y0)


def _stroke_points(stroke):
    yield stroke.stroke.pen_up
    for seg in stroke.stroke.segments:
        yield seg.end


class TestPenWidthsForLabel:
    """Pen -> engraved width uses the PHYSICAL cutter (buffer neglected)."""

    def test_buffer_neglected(self) -> None:
        layers = {1: (0.06, "none"), 4: (0.03, "none")}
        widths = pen_widths_for_label(layers)
        assert widths[1] == pytest.approx(0.06)
        assert widths[4] == pytest.approx(0.03)
