"""Gap-balanced vertical centering for labels with a lone top/bottom hole.

The hole-aware shift (:func:`plt_optimizer.generate.resolution._compute_hole_aware_shifts`)
centers the text block between a lone drill hole and the *hole-margin line* on
the opposite side. That rule is already an equal-gap rule -- measured to
``height - hole_margin`` at the bottom -- and the block's own height cancels
out of it, which is why it needs no rendered geometry.

This module implements the stricter reference: center the block between the lone
hole and the **label border** itself, so the top line sits as far from the hole
as the bottom line sits from the border (and, on a ``round`` label, as far as
the *closest point of the curved boundary* to the bottom line's outer ink). The
border is a curve, not a margin line, so the block's height and the bottom
line's width no longer cancel: the answer depends on rendered ink.

Both gaps are measured with the collision system's own metrics, in the
label-local y-up frame:

* the **top** gap is the circle-vs-box gap
  (:func:`~plt_optimizer.generate.collision.geometry.circle_aabb_gap`), exactly
  what :func:`~plt_optimizer.generate.collision.detection.detect_text_hole_collisions`
  tests against the stroke-aware threshold;
* the **bottom** gap is the true 2D ink distance
  (:func:`plt_optimizer.generate.geometry.polyline_gap`) from the bottom line's
  swept box to the boundary ink the renderer supplies (the bottom edge for a
  rectangular label, the lower arc for a ``round`` one, the inset corner arcs
  for a rounded rectangle).

The renderer owns every input (line boxes, border ink, the shift caps) and
passes them in as :class:`BalanceInputs`: this package has zero runtime imports
from ``label_renderer``.

Like the Phase-2b slide, the solution is a rigid translate of the whole block,
recorded as ONE number per label and folded into every frame that reproduces
the placement (``_frame_shift_y``, the export centering, ``_label_transform``).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional, Tuple

from plt_optimizer.core.units import HPGL_UNITS_PER_INCH
from plt_optimizer.generate.collision.geometry import circle_aabb_gap
from plt_optimizer.generate.geometry import Polyline, TextBounds, polyline_gap
from plt_optimizer.generate.resolution import ResolvedLabel

_LOGGER = logging.getLogger(__name__)

# Bisection budget, matching the resolution module's sweeps (the gap pair is
# monotone inside the bracket, so this converges to well below a device unit).
_BISECTION_TOLERANCE: float = 1e-9
_BISECTION_MAX_ITERATIONS: int = 100

HoleCircle = Tuple[float, float, float]
"""A hole as ``(center_x, center_y, emitted_radius)``, label-local inches."""


@dataclass(frozen=True)
class BalanceInputs:
    """Everything the balance solver needs, in the label-local y-up frame.

    Attributes:
        top_box: Swept bounds of the block's TOP line (the line the lone hole
            threatens), band-centred as :class:`_LineEntry` records them.
        bottom_box: Swept bounds of the block's BOTTOM line. The same box on a
            single-line label, which is the degenerate case the rule handles
            unchanged.
        origin: The Y that lifts the band-centred boxes into label-local
            coordinates for the placement as it stands (``_frame_shift_y``).
        top_holes: The hole circles on the hole's side of the block's centre.
            Empty when there is nothing to balance against (the solver then
            declines).
        border_ink: The label's own boundary as engraved ink (polylines,
            label-local). For a ``round`` label this is the lower arc sampled
            at the shared chord tolerance, so a wide bottom line measures to
            the rising curve, not to the label's bottom vertex.
        min_delta: Most the block may move down (in inches), i.e. the shift at
            which its bottom ink reaches the content band's lower edge.
        max_delta: Most the block may move up, i.e. the shift at which the top
            line's top edge reaches the shallowest hole's centre. Past it the
            box-to-circle gap stops shrinking (it is unsigned outside the
            circle), so the search must stay below.
    """

    top_box: TextBounds
    bottom_box: TextBounds
    origin: float
    top_holes: Tuple[HoleCircle, ...]
    border_ink: Tuple[Polyline, ...]
    min_delta: float
    max_delta: float


@dataclass(frozen=True)
class GapPair:
    """The two clearances the balance rule equalizes, in inches.

    Attributes:
        to_hole: Gap from the top line's swept box to the nearest hole circle
            (negative = the box overlaps the hole's emitted circle).
        to_border: Gap from the bottom line's swept box to the nearest point of
            the boundary ink (negative never happens: it is a distance).
    """

    to_hole: float
    to_border: float

    @property
    def binding(self) -> float:
        """The smaller of the two gaps -- the one the rule maximizes."""
        return min(self.to_hole, self.to_border)


def box_loop(box: TextBounds, dy: float) -> Polyline:
    """Return a swept box lifted by ``dy`` as a closed polyline.

    Args:
        box: ``(x_min, y_min, x_max, y_max)`` in the band-centred frame.
        dy: The Y offset to apply (inches).

    Returns:
        Five points closing the rectangle's outline, label-local.
    """
    x0, y0, x1, y1 = box[0], box[1] + dy, box[2], box[3] + dy
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0))


def measure_gaps(inputs: BalanceInputs, delta: float) -> GapPair:
    """Measure the hole-side and border-side gaps with the block shifted by ``delta``.

    Args:
        inputs: The label's rendered geometry.
        delta: The rigid vertical translate to evaluate, in inches, relative to
            the placement ``inputs.origin`` describes (negative = down).

    Returns:
        The :class:`GapPair` at that placement. ``to_border`` is ``inf`` when
        the label carries no border ink, which makes the hole the sole binding
        side (the solver then declines to move).
    """
    dy = inputs.origin + delta
    to_hole = min(
        circle_aabb_gap(
            (inputs.top_box[0], inputs.top_box[1] + dy, inputs.top_box[2], inputs.top_box[3] + dy),
            (cx, cy),
            radius,
        )
        for cx, cy, radius in inputs.top_holes
    )
    bottom = box_loop(inputs.bottom_box, dy)
    to_border = min(
        (polyline_gap(bottom, poly) for poly in inputs.border_ink if poly),
        default=math.inf,
    )
    return GapPair(to_hole=to_hole, to_border=to_border)


def solve_balance(inputs: BalanceInputs) -> float:
    """Return the shift that equalizes the two gaps, in inches.

    The gap pair is monotone inside the bracket the inputs describe, so the
    equal-gap placement is the unique root of ``to_hole - to_border``, found by
    bisection, and moving *toward* it can only raise the smaller gap (a
    min-max rule: it strictly improves the binding clearance until the two are
    equal).

    The result is quantized to one plotter device unit (``1 /
    HPGL_UNITS_PER_INCH`` inches). Coordinates are emitted as
    ``round(v * 1016) / 1016``, so a sub-quantum shift changes nothing on the
    machine and would make the collision ladder's progress tests churn.

    Args:
        inputs: The label's rendered geometry.

    Returns:
        The shift to apply (in inches, in the same direction the block's own
        placement grows: positive = up), relative to ``inputs.origin``; ``0.0``
        when the block cannot be balanced (no hole, no border ink, a degenerate
        bracket, or a root outside the caps -- e.g. a block taller than the free
        band, which centering cannot fix and which the spacing/compression
        phases own).
    """
    if not inputs.top_holes or not inputs.border_ink:
        return 0.0
    lo, hi = inputs.min_delta, inputs.max_delta
    if not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
        return 0.0
    low, high = measure_gaps(inputs, lo), measure_gaps(inputs, hi)
    # ``to_hole - to_border`` is monotone in the shift, and its slope's sign
    # follows which side the hole sits on: with the hole ABOVE the block, raising
    # the block shrinks the hole-side gap and widens the border-side gap, so the
    # difference falls; mirrored, it rises. A root exists exactly when the
    # bracket ends land on opposite sides of zero.
    f_lo = low.to_hole - low.to_border
    f_hi = high.to_hole - high.to_border
    if f_lo == 0.0:
        root = lo
    elif f_hi == 0.0:
        root = hi
    elif (f_lo > 0.0) == (f_hi > 0.0):
        _LOGGER.debug(
            "balance declined: no equalizing placement inside the band "
            "(gaps %+.4fin / %+.4fin at the caps)",
            f_lo,
            f_hi,
        )
        return 0.0
    else:
        for _ in range(_BISECTION_MAX_ITERATIONS):
            if hi - lo <= _BISECTION_TOLERANCE:
                break
            mid = (lo + hi) / 2.0
            pair = measure_gaps(inputs, mid)
            f_mid = pair.to_hole - pair.to_border
            # Keep the bracket straddling zero, whichever way the difference runs.
            if (f_mid > 0.0) == (f_lo > 0.0):
                lo = mid
            else:
                hi = mid
        root = (lo + hi) / 2.0
    delta = _round_device_unit(root)
    if delta == 0.0:
        return 0.0
    # Only ever move if the move improves the binding gap: an equalizing shift
    # inside the caps does, and this guard makes a degenerate root (a bracket the
    # shipped placement already sits outside) a no-op instead of a shuffle.
    shipped_pair = measure_gaps(inputs, 0.0)
    shipped = shipped_pair.binding
    moved = measure_gaps(inputs, delta).binding
    if moved <= shipped:
        _LOGGER.debug(
            "Balance declined: shift %+.4fin would not improve the binding gap %.4fin",
            delta,
            shipped,
        )
        return 0.0
    solved = measure_gaps(inputs, delta)
    _LOGGER.info(
        "balance: shift %+.4fin equalizes the gaps -- hole-side gap %.4fin / "
        "border-side gap %.4fin shipped, %.4fin / %.4fin balanced",
        delta,
        shipped_pair.to_hole,
        shipped_pair.to_border,
        solved.to_hole,
        solved.to_border,
    )
    return delta


def _round_device_unit(value: float) -> float:
    """Quantize ``value`` (inches) to a whole number of plotter units.

    Args:
        value: A length in inches.

    Returns:
        The nearest value the plotter can express, in inches.
    """
    return round(value * HPGL_UNITS_PER_INCH) / HPGL_UNITS_PER_INCH


def outermost_hole_radii(label: ResolvedLabel, side: str) -> Tuple[float, float]:
    """Return the outermost emitted hole radius on each vertical side.

    The emitted hole circle (see
    :func:`~plt_optimizer.generate.collision.holes.hole_circles_local`) is the
    material the drill removes, so the text block must clear it, and the band
    limit on a side is set by the *largest* axis hole on that side -- the same
    term
    :func:`~plt_optimizer.generate.resolution._compute_hole_aware_shifts` reads.

    Args:
        label: The label being centered.
        side: ``"top"`` or ``"bottom"`` -- the lone hole's side.

    Returns:
        ``(hole_side_diameter, opposite_diameter)`` in inches.
    """
    top = max((hole.diameter for hole in label.holes if hole.location == "top"), default=0.0)
    bottom = max((hole.diameter for hole in label.holes if hole.location == "bottom"), default=0.0)
    return (top, bottom) if side == "top" else (bottom, top)


def vertical_band(
    label: ResolvedLabel,
    hole_side_diameter: float,
    opposite_diameter: float,
) -> Tuple[float, float]:
    """Return the ``(lower, upper)`` content-band limits, band-centred inches.

    The band the collision ladder keeps the text inside: the label's box with
    each side's hole clearance taken off, centred on the block's own band. A
    side's clearance is the hole margin, the hole's kerf when
    :attr:`~plt_optimizer.generate.resolution.ResolvedLabel.hole_adjust_for_cutter`
    is set, and the outermost axis-hole diameter on that side -- the same terms
    :func:`~plt_optimizer.generate.collision.holes.hole_circles_local` uses to
    place a hole ``hole_margin`` in from the label edge, so a block that stays
    inside this band is a block the hole-aware shift could have produced.

    Args:
        label: The label being centered.
        hole_side_diameter: Outermost axis-hole diameter on the hole's side.
        opposite_diameter: Outermost axis-hole diameter on the other side.

    Returns:
        ``(lower, upper)`` Y limits in the band-centred frame.
    """
    kerf = label.hole_cutter_diameter if label.hole_adjust_for_cutter else 0.0
    half = label.height / 2.0
    return (
        label.hole_margin + kerf + opposite_diameter - half,
        half - label.hole_margin - kerf - hole_side_diameter,
    )


def lone_vertical_hole(label: ResolvedLabel) -> Optional[str]:
    """Return ``"top"``/``"bottom"`` when the label's holes are one-sided.

    The balance rule is defined for the asymmetric case the hole-aware shift
    exists for: holes on exactly one vertical side of the label. A label with
    holes on both sides is centered between them by the diameter difference
    already, and a label with none has nothing to balance. Sides are classified
    by the axis-hole ``location`` -- the same exact rule
    :func:`~plt_optimizer.generate.resolution._compute_hole_aware_shifts` uses
    to pick the holes that constrain the block, so corner holes (which are not
    axis holes) take part in neither rule.

    Args:
        label: The label being centered.

    Returns:
        ``"top"``, ``"bottom"``, or ``None`` when the rule does not apply.
    """
    sides = {hole.location for hole in label.holes if hole.diameter > 0.0}
    has_top = "top" in sides
    has_bottom = "bottom" in sides
    if has_top and not has_bottom:
        return "top"
    if has_bottom and not has_top:
        return "bottom"
    return None


__all__ = [
    "BalanceInputs",
    "GapPair",
    "box_loop",
    "lone_vertical_hole",
    "measure_gaps",
    "outermost_hole_radii",
    "solve_balance",
    "vertical_band",
]
