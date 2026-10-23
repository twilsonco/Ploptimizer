"""Unit tests for the enumerated-list counter formatter.

``plt_optimizer.generate.list_counter.format_counter`` turns a 1-based counter
value into the marker text of an enumerate list item, in one of five styles.
The module is pure (no rendering), so the tests pin the exact strings --
including the bijective-base-26 rollover (``z`` -> ``aa``) and the Roman
subtractive forms.
"""

from __future__ import annotations

import pytest

from plt_optimizer.generate.list_counter import format_counter
from plt_optimizer.generate.schema import CounterStyle


class TestDecimal:
    """The plain Arabic numbering."""

    def test_small_values(self) -> None:
        """1, 2, 3 ... render as their own digits."""
        assert [format_counter(value, CounterStyle.DECIMAL) for value in (1, 2, 9)] == [
            "1",
            "2",
            "9",
        ]

    def test_multi_digit(self) -> None:
        """Two- and three-digit values render in full."""
        assert format_counter(10, CounterStyle.DECIMAL) == "10"
        assert format_counter(1234, CounterStyle.DECIMAL) == "1234"


class TestAlpha:
    """The bijective base-26 letter styles."""

    def test_lower_alpha_start(self) -> None:
        """1/2 map to a/b."""
        assert format_counter(1, CounterStyle.LOWER_ALPHA) == "a"
        assert format_counter(2, CounterStyle.LOWER_ALPHA) == "b"

    def test_lower_alpha_rollover(self) -> None:
        """26 is z and 27 is aa (bijective base 26, no zero digit)."""
        assert format_counter(26, CounterStyle.LOWER_ALPHA) == "z"
        assert format_counter(27, CounterStyle.LOWER_ALPHA) == "aa"
        assert format_counter(28, CounterStyle.LOWER_ALPHA) == "ab"

    def test_lower_alpha_double_rollover(self) -> None:
        """52 is az and 53 is ba (the two-letter band)."""
        assert format_counter(52, CounterStyle.LOWER_ALPHA) == "az"
        assert format_counter(53, CounterStyle.LOWER_ALPHA) == "ba"

    def test_upper_alpha(self) -> None:
        """The uppercase style is the same sequence in capitals."""
        assert format_counter(1, CounterStyle.UPPER_ALPHA) == "A"
        assert format_counter(26, CounterStyle.UPPER_ALPHA) == "Z"
        assert format_counter(27, CounterStyle.UPPER_ALPHA) == "AA"


class TestRoman:
    """The Roman numeral styles."""

    def test_lower_roman_basic(self) -> None:
        """The first few numerals and the subtractive 4/9 forms."""
        assert [format_counter(value, CounterStyle.LOWER_ROMAN) for value in (1, 2, 3)] == [
            "i",
            "ii",
            "iii",
        ]
        assert format_counter(4, CounterStyle.LOWER_ROMAN) == "iv"
        assert format_counter(9, CounterStyle.LOWER_ROMAN) == "ix"

    def test_lower_roman_large(self) -> None:
        """The tens/hundreds/thousands subtractive forms."""
        assert format_counter(14, CounterStyle.LOWER_ROMAN) == "xiv"
        assert format_counter(40, CounterStyle.LOWER_ROMAN) == "xl"
        assert format_counter(90, CounterStyle.LOWER_ROMAN) == "xc"
        assert format_counter(400, CounterStyle.LOWER_ROMAN) == "cd"
        assert format_counter(900, CounterStyle.LOWER_ROMAN) == "cm"
        assert format_counter(1984, CounterStyle.LOWER_ROMAN) == "mcmlxxxiv"

    def test_roman_above_3999(self) -> None:
        """4000 uses the repeated-M additive form."""
        assert format_counter(4000, CounterStyle.LOWER_ROMAN) == "mmmm"

    def test_upper_roman(self) -> None:
        """The uppercase style is the same sequence in capitals."""
        assert format_counter(1, CounterStyle.UPPER_ROMAN) == "I"
        assert format_counter(4, CounterStyle.UPPER_ROMAN) == "IV"
        assert format_counter(14, CounterStyle.UPPER_ROMAN) == "XIV"


class TestValueValidation:
    """A counter value below 1 has no representation in any style."""

    def test_zero_is_rejected(self) -> None:
        """0 raises (no style defines a zero number)."""
        with pytest.raises(ValueError, match="must be >= 1"):
            format_counter(0, CounterStyle.DECIMAL)

    def test_negative_is_rejected(self) -> None:
        """A negative value raises with the value named."""
        with pytest.raises(ValueError, match="-3"):
            format_counter(-3, CounterStyle.LOWER_ALPHA)
