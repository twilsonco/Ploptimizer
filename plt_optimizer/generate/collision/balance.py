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

Both gaps are measured **ink to ink**, in the label-local y-up frame, over the
strokes a line actually engraves:

* the **hole-side** gap is the minimum distance from the lone hole's emitted
  circle to any stroke of the line facing it
  (:func:`plt_optimizer.generate.geometry.ink_circle_gap`);
* the **border-side** gap is the minimum distance from any stroke of the
  opposite line to the boundary ink the renderer supplies
  (:func:`plt_optimizer.generate.geometry.polyline_gap`) -- the bottom edge for
  a rectangular label, the lower arc for a ``round`` one, the inset corner arcs
  for a rounded rectangle.

There is deliberately no bounding-box fallback. A swept box measures the
line's *corners*: on a round label those corners reach past the ink into empty
space, so a box gap is the distance to a point the plotter never cuts and it
dominates the result (the same real label reads 0.75/0.25 on boxes and
0.2834/0.2841 on ink). A caller with no stroke data would silently get the
wrong metric, so ink is required and a line with none makes the rule decline.

Two layers live here. :func:`solve_balance` is the kernel: it takes the ink
and caps as :class:`BalanceInputs` and bisects for the equal-gap placement. :func:`balance_shift` is the ONE entry point every caller uses: it
takes a structured :class:`BalanceGeometry` (the block's swept box, the border
ink, the hole circles, all in label-local inches) plus the label, and owns
everything that is not geometry format -- the lone-side selection, the
adjacency X-overlap filter, the content-band clamp, the cap to the legacy
centering travel, and the no-op guards. The renderer feeds it the pre-centering
HPGL it is rewriting; the packing path
(:func:`plt_optimizer.generate.plate_optimizer._label_transform`) feeds it the
structured ``text_chunks`` box plus the label's own structural border and drill
layers. One solve, two thin adapters, so a packed label lands where an unpacked
one does.

This package has zero runtime imports from ``label_renderer``.

Like the Phase-2b slide, the solution is a rigid translate of the whole block,
recorded as ONE number per label and folded into every frame that reproduces
the placement (``_frame_shift_y``, the export centering, ``_label_transform``).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from plt_optimizer.core.units import HPGL_UNITS_PER_INCH
from plt_optimizer.generate.collision.detection import _content_band, collision_threshold
from plt_optimizer.generate.geometry import (
    Ink,
    Polyline,
    ink_circle_gap,
    polyline_gap,
)
from plt_optimizer.generate.resolution import ResolvedLabel

_LOGGER = logging.getLogger(__name__)

# Bisection budget, matching the resolution module's sweeps (the gap pair is
# monotone inside the bracket, so this converges to well below a device unit).
_BISECTION_TOLERANCE: float = 1e-9
_BISECTION_MAX_ITERATIONS: int = 100

HoleCircle = Tuple[float, float, float]
"""A hole as ``(center_x, center_y, emitted_radius)``, label-local inches."""


@dataclass(frozen=True)
class BalanceCircle:
    """One drill hole, as the balance rule measures it.

    The circle is the material the drill removes -- the EMITTED circle, kerf
    compensation included (see
    :func:`plt_optimizer.generate.collision.holes.hole_circles_local`) -- not
    the nominal diameter the job spec asked for.

    Attributes:
        cx: Centre X, label-local inches, +x right.
        cy: Centre Y, label-local inches, **+y up**, absolute in the label
            (never relative to the moving text block).
        radius: The emitted radius in inches.
    """

    cx: float
    cy: float
    radius: float


@dataclass(frozen=True)
class BalanceGeometry:
    """The rendered geometry the balance solve needs, in one structured record.

    This is the geometry-agnostic half of the rule's input: the renderer fills
    it from the label's own pre-centering HPGL (see
    :func:`plt_optimizer.generate.label_renderer._balance_shift_inches`) and the
    packing path from its structured ``text_chunks`` plus the structural border
    and hole layers (see
    :func:`plt_optimizer.generate.plate_optimizer._label_transform`). Both
    describe the SAME three things in the SAME frame, which is what makes a
    packed label land where an unpacked one does.

    Frame of every field: **label-local inches, +y up, ABSOLUTE** (origin at
    the label's lower-left corner, the frame the border is engraved in), with
    the single exception noted on each of the block fields below.

    Attributes:
        block_bottom: Swept bottom edge of the text block. Measured **where the
            legacy centering leaves it**, i.e. the raw (pre-centering) box
            lifted by :attr:`legacy_travel_inches` -- the border ink and the
            holes are already label-local, so an unlifted block is off by the
            whole centering shift.
        block_top: Swept top edge of the block, in that same lifted frame.
        block_x_min: Swept left edge of the block (X is never centered, so this
            is the raw value; inches).
        block_x_max: Swept right edge of the block, inches.
        holes: Every drill circle of the label, as :class:`BalanceCircle`
            records. The solve selects the lone side and the adjacent holes
            itself; the caller does not filter.
        border_ink: The label's own boundary as engraved ink (polylines,
            label-local). A ``round`` label's ellipse and a rounded rectangle's
            corner arcs must be the CURVE (sampled at the shared chord
            tolerance), not a bounding box, or a wide bottom line measures to
            the wrong place.
        top_line_ink: The engraved strokes of the block's TOP text line, as
            sampled polylines (label-local inches, ``+y`` up), measured **where
            the legacy centering leaves it** -- lifted by
            :attr:`legacy_travel_inches`, exactly like :attr:`block_top`.
            Together with :attr:`bottom_line_ink` these are what the two gaps
            are measured on: ``A`` is the minimum distance from the lone hole's
            emitted circle to any stroke of any character in the line facing it,
            ``B`` the minimum distance from the boundary ink to any stroke of the
            line facing the opposite border. Empty when the caller cannot supply
            structured stroke ink, in which case the solve falls back to the
            block's swept box.
        bottom_line_ink: The engraved strokes of the block's BOTTOM text line,
            in that same lifted frame. The same strokes as
            :attr:`top_line_ink` on a one-line label. Empty falls back to the
            box, as above.
        legacy_travel_inches: How far the legacy centering moves the block
            (inches, signed; ``> 0`` moves it up). The search is capped to that
            travel -- the balance rule redirects the centering move the label
            was always going to make, it does not add travel of its own. Pass
            ``0.0`` (the default) to leave the bracket uncapped, which is only
            correct when the caller has no centering move to cap (no packing
            path runs a hole-aware phase after the transform to resolve an
            ordering a crossing would create).
        block_x_padding: Extra X reach granted to the adjacency test (inches).
            ``0.0`` (the default) tests the block's own swept box. The packing
            path passes ``1 / HPGL_UNITS_PER_INCH`` -- one device unit, the
            tolerance the same path's stroke-coincidence merge uses -- because
            its block box is re-derived from structured geometry (integer-unit
            rounding of every vertex, floor/ceil of arc spans) and its hole
            circles come from the structural layer's emitted arc endpoints
            (``cx + r`` on the +x axis, rounded), so a hole that touches the
            block's swept edge EXACTLY -- the engraved-stroke case the rule
            exists for -- can differ by a few device units between the two
            paths. Without it the parity test's fixture loses its lone hole to
            the adjacency filter in the packing path and centers the legacy
            way while the renderer centers balanced.
    """

    block_bottom: float
    block_top: float
    block_x_min: float
    block_x_max: float
    holes: Tuple[BalanceCircle, ...]
    border_ink: Tuple[Polyline, ...]
    top_line_ink: Tuple[Polyline, ...] = ()
    bottom_line_ink: Tuple[Polyline, ...] = ()
    legacy_travel_inches: float = 0.0
    block_x_padding: float = 0.0


@dataclass(frozen=True)
class BalanceSolve:
    """The balanced placement, with the two clearances it equalized.

    Attributes:
        delta: The shift to add to the block's placement, inches (positive =
            up), relative to the placement :class:`BalanceGeometry` describes.
        to_hole: The hole-side gap at that placement, inches.
        to_border: The border-side gap at that placement, inches.
        partial: ``True`` when the exactly-balanced placement lies outside the
            caps, so ``delta`` is the best shift the caps allow and the two gaps
            stay unequal. The caller logs it: a shop seeing an unchanged-looking
            label needs to know centering hit a geometric limit, not that it did
            nothing.
        residual: ``abs(to_hole - to_border)`` at ``delta``, inches. ``0.0`` for
            an exact balance; the imbalance the caps could not close otherwise.
    """

    delta: float
    to_hole: float
    to_border: float
    partial: bool = False
    residual: float = 0.0


@dataclass(frozen=True)
class BalanceInputs:
    """Everything the balance solver needs, in the label-local y-up frame.

    Attributes:
        top_ink: The engraved strokes of the hole-side line (the line the lone
            hole threatens), lifted into the label-local frame. The hole-side
            gap is the minimum distance from the hole's emitted circle to any
            of them, which is what makes a round label balance on the ink the
            plotter cuts instead of on the block's corners.
        bottom_ink: The engraved strokes of the border-side line. The
            border-side gap is the minimum distance from any stroke to the
            boundary ink. A one-line label passes the same strokes to both,
            which is the rule's degenerate case and needs no branch.
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

    top_ink: Tuple[Polyline, ...]
    bottom_ink: Tuple[Polyline, ...]
    origin: float
    top_holes: Tuple[HoleCircle, ...]
    border_ink: Tuple[Polyline, ...]
    min_delta: float
    max_delta: float


@dataclass(frozen=True)
class GapPair:
    """The two clearances the balance rule equalizes, in inches.

    Attributes:
        to_hole: Gap from the hole-side line's ink to the nearest hole circle's
            emitted edge (negative = a stroke crosses into the drilled hole).
        to_border: Gap from the border-side line's ink to the nearest point of
            the boundary ink (negative never happens: it is a distance).
    """

    to_hole: float
    to_border: float

    @property
    def binding(self) -> float:
        """The smaller of the two gaps -- the one the rule maximizes."""
        return min(self.to_hole, self.to_border)


def _lift_ink(ink: Ink, dy: float) -> Ink:
    """Return ``ink`` translated vertically by ``dy`` inches.

    Args:
        ink: Polylines in the band-centred frame.
        dy: The Y offset to apply (inches).

    Returns:
        The translated polylines, or ``ink`` itself when ``dy`` is zero (the
        common case at the bracket's origin, so the no-op does not copy).
    """
    if dy == 0.0:
        return ink
    return tuple(tuple((x, y + dy) for x, y in poly) for poly in ink)


def top_and_bottom_line_ink(line_inks: Sequence[Ink]) -> Tuple[Ink, Ink]:
    """Return the ink of the top-most and bottom-most text line, in that order.

    A line's vertical position is the extent of the ink it engraves, not its
    index in the label's content: callers hand their lines over in whatever
    order they hold them (the renderer's per-pen block tuples, the packing
    path's ``line_index`` groups), and the two lines the gaps are measured on
    are the geometric extremes.

    Args:
        line_inks: One ink per text line, label-local inches ``+y`` up.

    Returns:
        ``(top_line_ink, bottom_line_ink)``. A one-line label returns its ink
        for both; a label with no ink at all returns two empty tuples, which
        makes the solve decline.
    """
    ranked: List[Tuple[float, float, Ink]] = []
    for ink in line_inks:
        ys = [y for poly in ink for _x, y in poly]
        if ys:
            ranked.append((min(ys), max(ys), ink))
    if not ranked:
        return (), ()
    top = max(ranked, key=lambda entry: entry[1])[2]
    bottom = min(ranked, key=lambda entry: entry[0])[2]
    return top, bottom


def measure_gaps(inputs: BalanceInputs, delta: float) -> GapPair:
    """Measure the hole-side and border-side gaps with the block shifted by ``delta``.

        Each gap is an **ink** distance: the minimum distance from any stroke of the
        line to the hole's emitted circle (:attr:`BalanceInputs.top_ink`) or to the
        boundary ink (:attr:`BalanceInputs.bottom_ink`). That is the definition the
        rule is specified in terms of -- a bounding box measures the block's
        corners, which on a round label reach into empty space past the ink and
    dominate the result.

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
    hole_line = _lift_ink(inputs.top_ink, dy)
    to_hole = min(
        (ink_circle_gap(hole_line, (cx, cy), radius) for cx, cy, radius in inputs.top_holes),
        default=math.inf,
    )
    border_line = _lift_ink(inputs.bottom_ink, dy)
    to_border = min(
        (
            polyline_gap(stroke, border)
            for stroke in border_line
            for border in inputs.border_ink
            if stroke and border
        ),
        default=math.inf,
    )
    return GapPair(to_hole=to_hole, to_border=to_border)


def _bisect_equal_gap(
    inputs: BalanceInputs,
    lo: float,
    hi: float,
    f_lo: float,
    f_hi: float,
) -> float:
    """Find the shift inside ``[lo, hi]`` where the two gaps are equal.

    Args:
        inputs: The label's rendered geometry.
        lo: Lower search bound, inches.
        hi: Upper search bound, inches.
        f_lo: ``to_hole - to_border`` at ``lo``.
        f_hi: ``to_hole - to_border`` at ``hi``.

    Returns:
        The root in inches. When the bracket does not straddle zero the gaps are
        unequal at both ends -- no equalizing placement exists inside the caps --
        and the cap that comes CLOSEST to the root is returned, so the caller
        applies the best shift the caps allow instead of nothing.
    """
    if f_lo == 0.0:
        return lo
    if f_hi == 0.0:
        return hi
    if (f_lo > 0.0) == (f_hi > 0.0):
        return hi if abs(f_hi) < abs(f_lo) else lo
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
    return (lo + hi) / 2.0


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
        when the block cannot be improved (no hole, no border ink, a degenerate
        bracket, or a move that would not raise the binding gap). When the root
        lies outside the caps the NEAREST cap is returned -- the best the
        allowed travel can do -- and the caller detects it from the residual the
        gaps still show.
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
    root = _bisect_equal_gap(inputs, lo, hi, f_lo, f_hi)
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
    residual = solved.to_hole - solved.to_border
    if abs(residual) <= 2.0 / HPGL_UNITS_PER_INCH:
        _LOGGER.info(
            "balance: shift %+.4fin equalizes the gaps -- hole-side gap %.4fin / "
            "border-side gap %.4fin shipped, %.4fin / %.4fin balanced",
            delta,
            shipped_pair.to_hole,
            shipped_pair.to_border,
            solved.to_hole,
            solved.to_border,
        )
    else:
        # The shipped move is the best the caps allow; the gaps stay unequal.
        _LOGGER.info(
            "balance: shift %+.4fin is the maximum the caps allow, gaps stay "
            "unequal by %+.4fin -- hole-side gap %.4fin / border-side gap %.4fin "
            "shipped, %.4fin / %.4fin after",
            delta,
            residual,
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


def adjacent_holes(
    holes: Tuple[BalanceCircle, ...],
    block_x_min: float,
    block_x_max: float,
    side: str,
    block_y: float,
    block_x_padding: float = 0.0,
) -> Tuple[BalanceCircle, ...]:
    """Return the holes the balance rule measures: adjacent, and on the lone side.

    Adjacency is the same X-overlap test the collision checker applies: a hole
    is adjacent when the block's swept X span overlaps the hole's **outermost**
    edge -- the extreme point of the emitted circle on the side facing the
    block, not its centre -- plus :attr:`BalanceGeometry.block_x_padding`. Ties
    keep every touching hole (two holes sharing an outermost edge are identical
    constraints), and a label whose holes all sit clear of the block's span has
    nothing to balance against, so the caller falls back to the legacy
    centering untouched.

    The side test is geometric -- the circle's facing edge lies beyond the
    block's face on that side -- so it classifies the circles the caller
    actually measured, in the caller's own frame, and needs no knowledge of the
    label's margins. Corner and side-wall holes are excluded by it (their edge
    lies beside the block's face, not beyond it), which is the intent: they take
    part in neither this rule nor the hole-aware shift.

    Args:
        holes: Every drill circle of the label, label-local inches, +y up.
        block_x_min: Swept left edge of the text block, inches.
        block_x_max: Swept right edge of the text block, inches.
        side: ``"top"``/``"bottom"`` -- the lone hole's side.
        block_y: The block's face on that side (its top edge for ``"top"``, its
            bottom edge for ``"bottom"``), in the same lifted frame as
            ``block_x_min``/``block_x_max``.
        block_x_padding: Extra X reach granted to the overlap test; see
            :attr:`BalanceGeometry.block_x_padding`.

    Returns:
        The adjacent holes on that side, in the order given.
    """
    out: List[BalanceCircle] = []
    for hole in holes:
        edge = hole.cy + hole.radius if side == "bottom" else hole.cy - hole.radius
        if (edge < block_y) if side == "bottom" else (edge > block_y):
            near = block_x_min - hole.cx
            far = hole.cx - block_x_max
            outermost = max(near, far)
            if outermost <= hole.radius + block_x_padding:
                out.append(hole)
    return tuple(out)


def balance_shift(label: ResolvedLabel, geometry: BalanceGeometry) -> Optional[BalanceSolve]:
    """Solve the gap-balanced placement from a structured geometry record.

    THE single entry point of this rule. Everything that is not the caller's
    geometry format lives here -- the lone-side selection, the adjacency X-overlap
    filter, the content-band clamp, the bracket the block may travel, the cap to
    the legacy centering move, and every no-op guard -- so the renderer (which
    reads the label's own pre-centering HPGL) and the packing path (which
    re-derives the placement from structured ``text_chunks``) cannot drift: both
    hand this function a :class:`BalanceGeometry` and apply
    ``delta * HPGL_UNITS_PER_INCH`` to the placement they are computing.

    Args:
        label: The label being centered (supplies the hole layout, the content
            band, the collision threshold, and the diameters the band is
            shortened by).
        geometry: The label-local inch geometry, in the frame
            :class:`BalanceGeometry` documents.

    Returns:
        The :class:`BalanceSolve` to apply, or ``None`` for any label this rule
        does not address: holes on both vertical sides, no hole at all, no
        border ink to measure against, no hole adjacent to the block on the
        lone side, a degenerate bracket, or a root that centering cannot reach.
        The caller then keeps the placement it computed.
    """
    side = lone_vertical_hole(label)
    if side is None:
        return None

    face = geometry.block_top if side == "top" else geometry.block_bottom
    holes = adjacent_holes(
        geometry.holes,
        geometry.block_x_min,
        geometry.block_x_max,
        side,
        face,
        geometry.block_x_padding,
    )
    if not holes or not geometry.border_ink:
        return None
    # Both gaps are ink distances, so a line that engraves nothing leaves that
    # side unmeasurable: there is nothing to balance, and a bbox stand-in would
    # measure a corner the plotter never cuts.
    if not geometry.top_line_ink or not geometry.bottom_line_ink:
        return None

    # Search caps: the block's ink may not leave the alignment content band
    # (the region the hole-aware shift and the Phase-2b ladder were allowed to
    # move it in), and on the hole side it may not cross the collision threshold
    # of the lone hole -- the escape rule that decides a slide is unsafe at all.
    # The border side needs no extra cap: a rounded label's border ink is
    # engraved at the band's own edge, so the band limit is the tighter bound.
    top_ink, bottom_ink = geometry.block_top, geometry.block_bottom
    lower, upper = _content_band(label)
    # Search caps. On the hole side the block's leading face may not cross the
    # collision threshold of the lone hole -- the escape rule that decides a
    # slide is unsafe at all. The border side needs no extra cap: a rounded
    # label's border ink is engraved at the band's own edge, so the band limit
    # is the tighter bound.
    #
    # Each band limit is then relaxed to the face it constrains. A block taller
    # than the free band can satisfy "bottom face above the band floor" and
    # "top face below the band ceiling" at no placement -- the bracket is empty
    # and the rule silently declines on precisely the congested labels it exists
    # for. Centering cannot shrink a block into a shorter band (spacing and
    # compression own that), so a face limit only binds where the shipped
    # placement has not already passed it: it stops further travel in that
    # direction, it never *demands* travel back toward the band.
    hole_cap = collision_threshold(label, 0)
    if side == "top":
        # The hole sits above: the block's top face may not rise past the
        # band ceiling or the hole's engraved edge (plus the threshold), and its
        # bottom face may not fall past the band floor.
        upper = min(upper, min(h.cy - h.radius for h in holes) - hole_cap)
        lower = min(lower, bottom_ink)
        upper = max(upper, top_ink)
    else:
        lower = max(lower, max(h.cy + h.radius for h in holes) + hole_cap)
        upper = max(upper, top_ink)
        lower = min(lower, bottom_ink)
    # The block travels with the face nearest the lone hole leading: that face
    # carries the hole-side cap, the far face carries the border-side one.
    if side == "top":
        # Travels down toward the opposite border: its bottom face meets the
        # band floor, its top face meets the hole it must not swallow.
        min_delta = lower - bottom_ink
        max_delta = upper - top_ink
    else:
        min_delta = lower - top_ink
        max_delta = upper - bottom_ink
    # Cap to the legacy centering travel: the balance rule redirects the move
    # the label was always going to make, it does not add travel of its own.
    # Past the legacy centering the block would have to pass the hole (the
    # hole-aware phase exists to resolve that ordering, and it does not run
    # after the packing transform), so the search stays inside it.
    travel = abs(geometry.legacy_travel_inches)
    lo = max(min_delta, -travel)
    hi = min(max_delta, travel)

    # The gaps are measured on the line facing each side; a one-line label
    # hands the same strokes to both, which is the rule's degenerate case and
    # needs no branch. ``BalanceInputs.top_ink`` is the hole-side line whichever
    # side the hole sits on, and the solve is sign-agnostic, so a bottom hole is
    # the mirrored case.
    hole_line_ink, border_line_ink = (
        (geometry.top_line_ink, geometry.bottom_line_ink)
        if side == "top"
        else (geometry.bottom_line_ink, geometry.top_line_ink)
    )
    inputs = BalanceInputs(
        origin=0.0,
        top_holes=tuple((hole.cx, hole.cy, hole.radius) for hole in holes),
        border_ink=geometry.border_ink,
        min_delta=lo,
        max_delta=hi,
        top_ink=hole_line_ink,
        bottom_ink=border_line_ink,
    )
    delta = solve_balance(inputs)
    if delta == 0.0:
        return None
    solved = measure_gaps(inputs, delta)
    residual = abs(solved.to_hole - solved.to_border)
    # An exact root lands inside one device quantum of equality; anything wider
    # is the clamp, i.e. the balanced placement was unreachable.
    partial = residual > 2.0 / HPGL_UNITS_PER_INCH
    if partial:
        shipped = measure_gaps(inputs, 0.0)
        _LOGGER.warning(
            "Label %s: gap-balanced centering is PARTIAL. The balanced placement "
            "lies outside the travel the caps allow, so the block moved %+.4fin "
            "(the maximum) and the gaps stay unequal by %.4fin: hole-side %.4fin vs "
            "border-side %.4fin (shipped %.4fin / %.4fin). To close it: reduce the "
            "text height/line count, the hole diameter, or "
            "hole_text_collision_distance.",
            label.id,
            delta,
            residual,
            solved.to_hole,
            solved.to_border,
            shipped.to_hole,
            shipped.to_border,
        )
    return BalanceSolve(
        delta=delta,
        to_hole=solved.to_hole,
        to_border=solved.to_border,
        partial=partial,
        residual=residual,
    )


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
    "BalanceCircle",
    "BalanceGeometry",
    "BalanceInputs",
    "BalanceSolve",
    "GapPair",
    "adjacent_holes",
    "balance_shift",
    "lone_vertical_hole",
    "measure_gaps",
    "solve_balance",
    "vertical_band",
]
