"""HPGL device scale: the single source of truth for inches <-> plotter units.

HPGL's plotter unit is **metric by definition**. The language's default step
size is 0.025 mm, and a file that ships no scale command (``IP``/``SC``/``SI``)
runs the plotter at that default::

    25.4 mm/in / 0.025 mm/step = 1016 units per inch  (40 units/mm)

The generate pipeline emits ``IN;PA;`` -- initialize plus absolute plotting,
and deliberately **no** scale command (EngraveLab reference framing, see
:data:`plt_optimizer.generate.label_renderer.PLT_HEADER`) -- so 1016 units/inch
is exactly the scale the shop's plotter applies to our output.

Why this module exists
----------------------
The pipeline historically emitted 1000 units/inch (a convenient "1 unit =
0.001 in" fiction). Every generated label therefore engraved **1.6% undersized**
(1016/1000): a 16in tall label came out 15.75in, a 24in wide label 23.594in.

Reference proof, from EngraveLab-produced files kept in this repo:

* ``examples/test_rect_grid13sheet0.txt`` -- every coordinate is an exact
  multiple of 1016. The grid cells are ``3048 x 2032`` with lines at
  2032/3048/4064/6096/8128/9144/10160/12192, i.e. exactly 2/3/4/6/8/9/10/12
  inches. At 1000 units/inch a 3x2in cell would read 3000x2000.
* ``examples/test123-borders.plt`` -- a 3in border divided at 1010/2020/3030
  (~1010-1016 per inch, never 1000).
* No ``IP``/``SC``/``SI``/``SD`` scale command appears in any reference file,
  confirming the plotter runs its 0.025 mm default.

Three scales, only one of them the device scale
-----------------------------------------------
1. **Device plotter units** -- :data:`HPGL_UNITS_PER_INCH` (1016). Every
   coordinate written to, or read back from, an emitted PLT.
2. **Font-library design frame** -- 1000 units per *design* inch
   (``Fonts/plt_fonts.json``, ``LIBRARY_UNITS_PER_INCH`` in
   ``Fonts/extract_plt_fonts.py``, ``_DESIGN_UNITS_PER_INCH`` in
   ``plt_optimizer/generate/plt_font_renderer.py``). This is a *normalized*
   frame, not a device scale: extraction rescales each sheet by
   ``S = 1000 / H_ref_raw`` so the reference character is exactly 1000 design
   units tall, which erases whatever scale the sample sheet was engraved at.
   It is intentionally **not** 1016 -- do not "fix" it here.
3. **Z clearance** -- :data:`Z_UNITS_PER_INCH` (1000). The controller's Z axis
   is decoupled from the XY step size: ``examples/plt headers/
   z clearance 0.2.plt`` emits ``ZU200`` and ``z clearance 0.3.plt`` emits
   ``ZU300``, i.e. exactly 1000 units per inch of clearance.

Python 3.8 / Windows 7 compatible: pure Python, no matplotlib, no
generate-pipeline imports. Safe to import from the ``watch``/``optimize`` CLI
startup path (including :mod:`plt_optimizer.utils.logging`).
"""

from __future__ import annotations

#: Millimetres per inch (exact by definition).
MM_PER_INCH: float = 25.4

#: HPGL default step size in millimetres -- the plotter unit when the file
#: carries no ``IP``/``SC``/``SI`` scale command, which is our framing.
HPGL_STEP_SIZE_MM: float = 0.025

#: Plotter units per inch for XY coordinates: ``25.4 / 0.025 == 1016``.
#
#: The quotient is snapped to the exact integer: ``25.4 / 0.025`` evaluates to
#: ``1015.9999999999999`` in binary floating point, and a scale that is one
#: ten-trillionth of a unit short would leak into every emitted coordinate.
HPGL_UNITS_PER_INCH: float = float(round(MM_PER_INCH / HPGL_STEP_SIZE_MM))

#: Plotter units per inch of Z clearance on this controller (measured from the
#: ``examples/plt headers/z clearance *.plt`` reference headers: 0.2in ->
#: ``ZU200``, 0.3in -> ``ZU300``). Deliberately independent of
#: :data:`HPGL_UNITS_PER_INCH` -- the Z axis does not follow the XY step size.
Z_UNITS_PER_INCH: float = 1000.0

# Guard the derivation: an accidental edit to the metric constants above must
# not silently change the device scale.
assert HPGL_UNITS_PER_INCH == round(MM_PER_INCH / HPGL_STEP_SIZE_MM) == 1016.0


def inches_to_units(value_inches: float) -> int:
    """Convert inches to integer plotter units at the device scale.

    Args:
        value_inches: Distance in inches.

    Returns:
        ``round(value_inches * HPGL_UNITS_PER_INCH)`` as an integer.
    """
    return int(round(value_inches * HPGL_UNITS_PER_INCH))


def units_to_inches(value_units: float) -> float:
    """Convert plotter units to inches at the device scale.

    Args:
        value_units: Distance in plotter units (may be a float).

    Returns:
        The distance in inches.
    """
    return float(value_units) / HPGL_UNITS_PER_INCH


__all__ = [
    "MM_PER_INCH",
    "HPGL_STEP_SIZE_MM",
    "HPGL_UNITS_PER_INCH",
    "Z_UNITS_PER_INCH",
    "inches_to_units",
    "units_to_inches",
]
