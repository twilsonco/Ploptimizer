"""Tests for plt_optimizer/core/units.py -- the HPGL device scale.

These tests pin the *derivation* of the plotter scale (so an accidental edit to
the metric constants is caught), the decoupled Z scale, and the rounding
contract of the conversion helpers. They also pin the reference-file evidence
itself: the EngraveLab grid sheet's coordinates are exact multiples of 1016.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

from plt_optimizer.core.units import (
    HPGL_STEP_SIZE_MM,
    HPGL_UNITS_PER_INCH,
    MM_PER_INCH,
    Z_UNITS_PER_INCH,
    inches_to_units,
    units_to_inches,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_GRID = REPO_ROOT / "examples" / "test_rect_grid13sheet0.txt"


class TestScaleDerivation:
    """The device scale is derived from HPGL's metric step size."""

    def test_units_per_inch_is_the_metric_derivation(self) -> None:
        """25.4 mm/in / 0.025 mm step == 1016 units/inch (exactly)."""
        assert MM_PER_INCH / HPGL_STEP_SIZE_MM == pytest.approx(1016.0)
        assert HPGL_UNITS_PER_INCH == 1016.0

    def test_scale_is_an_exact_integer(self) -> None:
        """The scale is integral, so whole inches emit as exact integers."""
        assert float(HPGL_UNITS_PER_INCH).is_integer()
        assert int(HPGL_UNITS_PER_INCH) == 1016

    def test_units_per_mm(self) -> None:
        """The reciprocal reading: 40 units per millimetre (0.025 mm/step)."""
        assert HPGL_UNITS_PER_INCH / MM_PER_INCH == pytest.approx(40.0)

    def test_z_scale_is_decoupled_from_xy(self) -> None:
        """Z clearance runs at 1000 units/inch, independent of the XY step."""
        assert Z_UNITS_PER_INCH == 1000.0
        assert Z_UNITS_PER_INCH != HPGL_UNITS_PER_INCH


class TestInchesToUnits:
    """Inch -> device unit conversion (the emission direction)."""

    def test_one_inch(self) -> None:
        """1 inch is exactly one full scale."""
        assert inches_to_units(1.0) == 1016

    def test_zero(self) -> None:
        """Zero inches is zero units."""
        assert inches_to_units(0.0) == 0

    def test_negative_inches_round_symmetrically(self) -> None:
        """Negative offsets convert with the same magnitude."""
        assert inches_to_units(-1.0) == -1016

    def test_returns_int(self) -> None:
        """Emitted coordinates are integers (the HPGL integer-unit contract)."""
        assert isinstance(inches_to_units(1.2345), int)

    def test_rounds_to_nearest_unit(self) -> None:
        """Fractional inches round to the nearest whole plotter unit."""
        assert inches_to_units(1.49 / HPGL_UNITS_PER_INCH) == 1
        assert inches_to_units(1.51 / HPGL_UNITS_PER_INCH) == 2

    def test_half_unit_ties_round_to_even(self) -> None:
        """Exact half-unit ties round to even (Python's ``round`` contract).

        The pipeline has always emitted ``round(inches * scale)``, so the
        banker's-rounding tie is a preserved property, not a regression. It is
        unreachable in practice: a tie needs a coordinate landing exactly on a
        half unit.
        """
        assert inches_to_units(0.5 / HPGL_UNITS_PER_INCH) == 0
        assert inches_to_units(1.5 / HPGL_UNITS_PER_INCH) == 2

    @pytest.mark.parametrize(
        ("inches", "units"),
        [(2.0, 2032), (3.0, 3048), (4.0, 4064), (6.0, 6096), (12.0, 12192)],
    )
    def test_whole_inches_match_reference_grid(self, inches: float, units: int) -> None:
        """Whole inches land on the exact values in the EngraveLab grid sheet."""
        assert inches_to_units(inches) == units


class TestUnitsToInches:
    """Device unit -> inch conversion (the read-back direction)."""

    def test_one_full_scale(self) -> None:
        """1016 units is 1 inch."""
        assert units_to_inches(1016) == pytest.approx(1.0)

    def test_zero(self) -> None:
        """Zero units is zero inches."""
        assert units_to_inches(0) == pytest.approx(0.0)

    def test_negative_units(self) -> None:
        """Negative units convert to negative inches."""
        assert units_to_inches(-1016) == pytest.approx(-1.0)

    def test_accepts_float_units(self) -> None:
        """Parsed coordinates may carry decimals; conversion stays exact."""
        assert units_to_inches(508.0) == pytest.approx(0.5)

    def test_round_trip_is_within_half_a_unit(self) -> None:
        """inches -> units -> inches never drifts past the emission floor."""
        for inches in (0.0, 0.015, 0.5, 1.0, 3.333, 16.0, 24.0, 47.125):
            recovered = units_to_inches(inches_to_units(inches))
            assert math.isclose(recovered, inches, abs_tol=0.5 / HPGL_UNITS_PER_INCH)


class TestReferenceFileEvidence:
    """The 1016 scale is pinned against the EngraveLab reference output."""

    @pytest.mark.skipif(not REFERENCE_GRID.is_file(), reason="reference sheet absent")
    def test_reference_grid_coordinates_are_multiples_of_1016(self) -> None:
        """Every coordinate of the reference grid sheet divides by 1016.

        This is the proof that the shop's plotter runs at the HPGL default
        0.025 mm step: EngraveLab's own 3x2in grid cells emit as 3048x2032.
        At 1000 units/inch the same cells would read 3000x2000.
        """
        content = REFERENCE_GRID.read_text(encoding="utf-8")
        coordinates = [
            float(pair)
            for command in re.findall(r"(?:PU|PD)([\d.,\-]+)", content)
            for pair in command.split(",")
            if pair
        ]
        assert coordinates, "reference grid sheet carries no coordinates"
        assert len(coordinates) > 100, "expected a full grid of coordinates"
        for value in coordinates:
            assert value % HPGL_UNITS_PER_INCH == 0.0, f"{value} is not a 1016 multiple"
