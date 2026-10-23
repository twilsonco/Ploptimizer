"""Enumerated-list counter formatting (pure, import-light).

An :class:`~plt_optimizer.generate.schema.EnumerateBlock` numbers its items
with a counter whose *style* the author selects (``decimal``,
``lower-alpha``, ``upper-alpha``, ``lower-roman``, ``upper-roman``). This
module turns a 1-based counter value into the marker string for each style.

It is deliberately dependency-free (no matplotlib, no pipeline imports beyond
the :class:`~plt_optimizer.generate.schema.CounterStyle` enum) so it stays on
the Python 3.8 / Windows 7 import path and is trivially unit-testable.

Example:
    >>> from plt_optimizer.generate.schema import CounterStyle
    >>> format_counter(1, CounterStyle.DECIMAL)
    '1'
    >>> format_counter(4, CounterStyle.LOWER_ROMAN)
    'iv'
    >>> format_counter(27, CounterStyle.LOWER_ALPHA)
    'aa'
"""

from __future__ import annotations

from plt_optimizer.generate.schema import CounterStyle

__all__ = ["format_counter"]

# Roman numeral value/symbol pairs, largest first (the standard subtractive
# table, including the 4/9 forms IV, IX, XL, XC, CD, CM).
_ROMAN_PAIRS: tuple[tuple[int, str], ...] = (
    (1000, "M"),
    (900, "CM"),
    (500, "D"),
    (400, "CD"),
    (100, "C"),
    (90, "XC"),
    (50, "L"),
    (40, "XL"),
    (10, "X"),
    (9, "IX"),
    (5, "V"),
    (4, "IV"),
    (1, "I"),
)

# Base used by the alpha styles: the 26 letters of the Latin alphabet.
_ALPHA_BASE: int = 26


def _format_decimal(value: int) -> str:
    """Return the plain Arabic numeral for a positive counter value.

    Args:
        value: The 1-based counter value.

    Returns:
        The decimal string (e.g. ``"7"``).
    """
    return str(value)


def _format_alpha(value: int, upper: bool) -> str:
    """Return the bijective base-26 letter sequence for a counter value.

    The alpha styles are *bijective* base 26 (spreadsheet column labels):
    ``1 -> a`` ... ``26 -> z``, and ``27 -> aa`` (not ``ba``), so there is no
    zero digit. The value is decremented before each digit so the range
    ``[1, 26]`` maps onto ``[a, z]``.

    Args:
        value: The 1-based counter value.
        upper: When True, emit uppercase letters.

    Returns:
        The letter sequence (e.g. ``"aa"`` for 27).
    """
    letters: list[str] = []
    remaining = value
    while remaining > 0:
        remaining, remainder = divmod(remaining - 1, _ALPHA_BASE)
        letters.append(chr(ord("a") + remainder))
    text = "".join(reversed(letters))
    return text.upper() if upper else text


def _format_roman(value: int, upper: bool) -> str:
    """Return the Roman numeral for a positive counter value.

    Greedy subtraction over :data:`_ROMAN_PAIRS`. Values above 3999 use the
    repeated-M additive form (``MMMM`` for 4000), the conventional limit for
    engraving counters (well beyond any real list length).

    Args:
        value: The 1-based counter value.
        upper: When True, emit uppercase numerals.

    Returns:
        The Roman numeral string (e.g. ``"xiv"`` for 14).
    """
    symbols: list[str] = []
    remaining = value
    for number, symbol in _ROMAN_PAIRS:
        while remaining >= number:
            symbols.append(symbol)
            remaining -= number
    text = "".join(symbols)
    return text if upper else text.lower()


def format_counter(value: int, style: CounterStyle) -> str:
    """Format a 1-based enumerated counter in the requested style.

    Args:
        value: The counter value (the item's position plus the list's
            ``start_index``); must be >= 1.
        style: The numbering style to render.

    Returns:
        The marker's numeric/alphabetic part (the list's prefix/suffix are
        applied by the caller).

    Raises:
        ValueError: If ``value`` is less than 1 (no counter style defines a
            zero or negative number).
    """
    if value < 1:
        raise ValueError(f"counter value must be >= 1, got {value}")
    if style is CounterStyle.DECIMAL:
        return _format_decimal(value)
    if style is CounterStyle.LOWER_ALPHA:
        return _format_alpha(value, upper=False)
    if style is CounterStyle.UPPER_ALPHA:
        return _format_alpha(value, upper=True)
    if style is CounterStyle.LOWER_ROMAN:
        return _format_roman(value, upper=False)
    if style is CounterStyle.UPPER_ROMAN:
        return _format_roman(value, upper=True)
    raise ValueError(f"unknown counter style: {style!r}")  # pragma: no cover
