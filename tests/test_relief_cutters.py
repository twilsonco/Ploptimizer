"""Unit tests for relief cutter ladder selection (relief/cutters.py)."""

from __future__ import annotations

import math
from typing import List

import pytest

from plt_optimizer.generate.relief.cutters import (
    bypass_relief_cutters,
    select_relief_cutters,
)
from plt_optimizer.generate.relief.regions import build_engrave_regions

INVENTORY = [0.005, 0.01, 0.02, 0.03, 0.04, 0.06, 0.08, 0.125, 0.25]


class _Logger:
    """Minimal TextLogger stub capturing warnings."""

    def __init__(self) -> None:
        self.warnings: List[str] = []

    def warning(self, message: str) -> None:
        self.warnings.append(message)


def _regions(ink):
    return build_engrave_regions(1.0, 1.0, ink, False)


def _a_ink() -> List:
    """A square island with an enclosed counter (like the letter A)."""
    outer = [(0.3, -0.2), (0.7, -0.2), (0.7, 0.2), (0.3, 0.2), (0.3, -0.2)]
    inner = [(0.45, -0.05), (0.55, -0.05), (0.55, 0.05), (0.45, 0.05), (0.45, -0.05)]
    return [(outer, 0.1), (inner, 0.1)]


class TestBypass:
    """The explicit cutter list is used directly, exact widths, descending."""

    def test_exact_widths_descending(self) -> None:
        ladder = bypass_relief_cutters([0.005, 0.06])
        assert [(c.physical, c.effective) for c in ladder] == [(0.06, 0.06), (0.005, 0.005)]

    def test_dedupes_and_drops_nonpositive(self) -> None:
        ladder = bypass_relief_cutters([0.06, 0.06, 0.0, -1.0, 0.005])
        assert [c.physical for c in ladder] == [0.06, 0.005]

    def test_empty(self) -> None:
        assert bypass_relief_cutters([0.0]) == ()


class TestSelectPresets:
    """Preset admissibility rules."""

    def test_medium_is_largest_plus_smallest(self) -> None:
        regions = _regions(_a_ink())
        ladder = select_relief_cutters("medium", regions, INVENTORY)
        assert [c.physical for c in ladder] == [0.25, 0.005]

    def test_coarse_picks_largest_full_coverage_cutter(self) -> None:
        regions = _regions(_a_ink())
        ladder = select_relief_cutters("coarse", regions, INVENTORY)
        assert len(ladder) == 1
        # The 0.1 counter (radius erosion, pass floor = pitch) admits
        # one full pass for cutters up to 0.06; 0.06 is the largest
        # admissible rung.
        assert ladder[0].physical == 0.06

    def test_coarse_without_counters_is_largest(self) -> None:
        regions = _regions([([(0.4, 0.0), (0.6, 0.0)], 0.1)])
        ladder = select_relief_cutters("coarse", regions, INVENTORY)
        assert ladder[0].physical == 0.25

    def test_fine_includes_intermediate(self) -> None:
        regions = _regions(_a_ink())
        ladder = select_relief_cutters("fine", regions, INVENTORY)
        assert ladder[0].physical == 0.25
        assert ladder[-1].physical == 0.005
        assert len(ladder) >= 2

    def test_fine_without_counters_collapses(self) -> None:
        regions = _regions([([(0.4, 0.0), (0.6, 0.0)], 0.1)])
        ladder = select_relief_cutters("fine", regions, INVENTORY)
        assert [c.physical for c in ladder] == [0.25, 0.005]

    def test_tiny_inventory_collapses_to_one(self) -> None:
        regions = _regions(_a_ink())
        ladder = select_relief_cutters("fine", regions, [0.005])
        assert [c.physical for c in ladder] == [0.005]

    def test_coarse_falls_back_with_warning(self) -> None:
        # A hair-thin counter nothing can fully cover: coarse has no
        # admissible cutter -> medium semantics + warning.
        outer = [(0.4, 0.4), (0.6, 0.4), (0.6, 0.6), (0.4, 0.6), (0.4, 0.4)]
        inner = [(0.495, 0.495), (0.505, 0.495), (0.505, 0.505), (0.495, 0.505), (0.495, 0.495)]
        regions = _regions([(outer, 0.1), (inner, 0.1)])
        logger = _Logger()
        ladder = select_relief_cutters("coarse", regions, INVENTORY, logger=logger)
        assert [c.physical for c in ladder] == [0.25, 0.005]
        assert any("falling back" in w.lower() for w in logger.warnings)

    def test_buffer_neglected(self) -> None:
        # Relief engraving neglects cutter_buffer_text under all
        # circumstances: preset tools engrave at their physical diameter.
        regions = _regions(_a_ink())
        ladder = select_relief_cutters("medium", regions, [0.03, 0.06])
        assert all(math.isclose(c.effective, c.physical) for c in ladder)

    def test_unknown_quality_raises(self) -> None:
        regions = _regions(_a_ink())
        with pytest.raises(ValueError, match="ultra"):
            select_relief_cutters("ultra", regions, INVENTORY)

    def test_empty_inventory_raises(self) -> None:
        regions = _regions(_a_ink())
        with pytest.raises(ValueError, match="inventory"):
            select_relief_cutters("medium", regions, [])

    def test_admissible_counter_chord(self) -> None:
        # Degenerate: a counter exactly one cutter wide admits a pass.
        outer = [(0.4, 0.4), (0.6, 0.4), (0.6, 0.6), (0.4, 0.6), (0.4, 0.4)]
        inner = [(0.4, 0.4), (0.6, 0.4), (0.6, 0.6), (0.4, 0.6), (0.4, 0.4)]
        regions = build_engrave_regions(1.0, 1.0, [(outer, 0.2), (inner, 0.2)], False)
        # The counter is the 0.2 band interior (between 0.4..0.6 and
        # 0.5..0.5 ring) - just check selection does not crash and
        # returns a sane ladder.
        ladder = select_relief_cutters("coarse", regions, [0.06, 0.125])
        assert ladder
