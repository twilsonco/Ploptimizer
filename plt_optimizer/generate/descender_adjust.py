"""Automatic descending-character relief (pure math).

Baseline-to-baseline stacking (:attr:`plt_optimizer.generate.resolution.
ResolvedLabel.use_baseline_spacing`) lets a line's descenders hang *into* the
gap below them, which is what makes every gap on a label read as one uniform
visual spacing. When the gap is short, though, a deep descender lands on top
of the capitals of the line below: the two engraved strokes bleed together.

This module computes the relief for that case. The relief is **per
descending glyph**, driven by a **measured near-miss against the following
line** -- never a blanket adjustment of every descender on the label, and
never a reaction to the following line's cap line in the abstract. A
descending glyph is only touched when some glyph on the line below comes
within the required clearance of it:

``required = 0.5 * (cutter_upper + cutter_lower) * (1 + clearance)``

The first factor is the gap at which the two cut strokes exactly touch (the
same stroke-aware floor the text-hole collision system uses); ``clearance``
(``descending_character_clearance``) is a **unitless factor of that floor** --
extra air demanded as a fraction of the cutter width, so the trigger reads
the same at 2in and 0.25in text. ``0.0`` demands nothing beyond touching.

The measurement is the **true 2D swept-ink distance**: imagine a circle whose
diameter is the required clearance, rolled along the descending glyph's
sub-baseline ink; it collides exactly when it touches any part of any glyph
of the line below. Equivalently, the pair's gap is
:func:`~plt_optimizer.generate.geometry.ink_gap` between the two glyphs'
stroke polylines -- **not** a bounding-box comparison. A box model reports
X-disjoint boxes as "infinitely far apart", which hides exactly the pairs
this mechanism exists for: a ``g`` hook reaching sideways into the crook of
the ``i`` on the line below is X-disjoint at box level while its true
proximity sits well inside the required clearance. Glyphs without ink
knowledge (``ink``/``sub_baseline_ink`` empty -- e.g. boxes built by hand in
tests) fall back to the box model.

The relief is a single proportional **ramp** ``t`` in ``[0, 1]`` combining
the two permitted effects, both bounded by the cascading allowances:

``scale_y(t) = 1 - t * (1 - descending_character_v_scale)``
``raise(t)   = t * descending_character_raise * descender``

The glyph is scaled about its own baseline (``y = 0``), so its ascender and
descender shrink together, and the raise is a fraction of its own descender
depth, which makes ``descending_character_raise: 1.0`` mean exactly "lift
until the glyph's lowest point sits on the baseline". At ``t = 1`` both
allowances are fully spent.

Because the glyph's bottom after the ramp is ``raise(t) - descender *
scale_y(t)``, the gap it produces is **linear in** ``t``::

    gap(t) = gap(0) + t * (raise_cap + descender * (1 - v_scale))

so the smallest ``t`` clearing a pair has a closed form (no bisection). A
ramp above ``1.0`` means the allowances cannot clear the pair: the caller
clamps it to ``1.0`` and grows the inter-line spacing instead (see
:func:`plt_optimizer.generate.resolution.fit_baseline_spacing_to_gaps`).

The module is deliberately import-light (pure Python; pipeline types are
``TYPE_CHECKING``-only imports) so it stays unit-testable without matplotlib,
mirroring :mod:`plt_optimizer.generate.layout_report`.

Example:
    >>> required_gap(0.045, 0.045, 0.0)  # clearance 0 = the bare floor
    0.045
    >>> required_gap(0.045, 0.045, 0.5)  # clearance 0.5 = 1.5x the floor
    0.0675
    >>> ramp_for(gap=0.0, required=0.045, descender=0.1, v_scale=0.5,
    ...          raise_fraction=0.7) > 1.0
    True
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence

from plt_optimizer.generate.geometry import Ink, InkGrid, aabb_vertical_gap, ink_gap

# A glyph counts as descending when its ink reaches this far below its own
# baseline. Emitted geometry is integer plotter units (1 unit = 0.001in), so
# this epsilon only absorbs floating-point noise, never real ink.
_DESCENDER_EPSILON: float = 1e-9

# Gap comparison tolerance: a gap exactly equal to the required clearance is
# safe (tangency, matching ``circle_aabb_gap`` semantics). AGENTS.md forbids
# ``==`` on floats. 1e-6in is the emission floor (a thousandth of a plotter
# unit), matching the relief's abort gate: the 2D ink distance's sub-linear
# growth leaves sub-plotter-unit noise that is not a real collision.
_GAP_TOLERANCE: float = 1e-6

# Ramp comparison tolerance for the fixpoint loop: a ramp step smaller than
# this counts as converged (the loop is monotone, so this bounds the passes).
_RAMP_TOLERANCE: float = 1e-12

# Bisection bounds for the ink-distance ramp solver: the true 2D distance is
# not linear in the ramp (unlike the vertical box gap, whose closed form
# ``ramp_for`` still solves), so the smallest clearing ramp is bisected.
_RAMP_BISECTION_TOLERANCE: float = 1e-9
_RAMP_BISECTION_MAX_ITERATIONS: int = 60


@dataclass(frozen=True)
class GlyphBox:
    """One rendered glyph's ink box and swept ink, in the shared label-local frame.

    Attributes:
        glyph_index: Position of this glyph in its line's per-glyph stroke
            partition (``TextChunkRecord.glyph_groups``), used as the key of
            the ramp map.
        char: The character the glyph renders (diagnostics only).
        bounds: ``(x_min, y_min, x_max, y_max)`` swept ink box in the label
            frame the line is positioned in (the line's baseline sits at
            ``baseline_y``, so a descender reaches below it).
        descender: Depth of the ink below the line's baseline, in inches
            (``max(0.0, baseline_y - bounds.y_min)``). ``0.0`` means the
            glyph does not descend and is therefore never a relief candidate.
        ink: The glyph's complete swept ink (one polyline per stroke, arcs
            flattened), in the same frame as :attr:`bounds`. Empty means "no
            ink knowledge": pair measurement falls back to the box model.
        sub_baseline_ink: :attr:`ink` clipped to the ink **below the line's
            baseline** -- the part of a descending glyph that hangs into the
            gap below its line, i.e. the part the relief's rolling circle
            travels along. Empty (with :attr:`ink`) means box-model fallback.
    """

    glyph_index: int
    char: str
    bounds: tuple[float, float, float, float]
    descender: float
    ink: Ink = ()
    sub_baseline_ink: Ink = ()

    @property
    def descends(self) -> bool:
        """True when this glyph's ink hangs below its baseline."""
        return self.descender > _DESCENDER_EPSILON


@dataclass(frozen=True)
class PairGap:
    """A measured (descending glyph, following-line glyph) proximity.

    Attributes:
        upper: The descending glyph of the upper line.
        lower: The glyph on the following line it comes close to.
        gap: The true 2D swept-ink distance between the two glyphs (the
            rolling-circle test: :attr:`GlyphBox.sub_baseline_ink` of the
            upper glyph against :attr:`GlyphBox.ink` of the lower one; the
            vertical box gap when neither carries ink knowledge). Negative
            means the ink overlaps. ``inf`` means unmeasurable (no ink on
            one side, box-model fallback with disjoint X spans, or the upper
            glyph has no sub-baseline ink).
        required: Clearance at which the pair is satisfied, in inches.
    """

    upper: GlyphBox
    lower: GlyphBox
    gap: float
    required: float

    @property
    def deficit(self) -> float:
        """How much more clearance this pair needs, in inches (``0.0`` if safe)."""
        return max(0.0, self.required - self.gap)

    @property
    def collides(self) -> bool:
        """True when this pair falls short of :attr:`required`."""
        return self.deficit > _GAP_TOLERANCE


@dataclass(frozen=True)
class DescenderFix:
    """One descending glyph's applied relief, for reporting.

    Attributes:
        line_index: Index of the glyph's text line in the label's ``content``.
        glyph_index: Index in the line's per-glyph stroke partition.
        char: The character the glyph renders.
        scale_y: Vertical scale applied about the glyph's baseline (``1.0`` =
            no downscaling).
        raise_amount: Upward translation applied after the scale, inches.
        gap_before: Tightest clearance this glyph had against the following
            line before any relief, inches (``inf`` when it had no
            horizontally overlapping partner -- impossible for a trigger).
        gap_after: Tightest clearance after the relief, inches.
    """

    line_index: int
    glyph_index: int
    char: str
    scale_y: float
    raise_amount: float
    gap_before: float
    gap_after: float


def required_gap(upper_cutter: float, lower_cutter: float, clearance: float) -> float:
    """Return the clearance a glyph pair must keep to count as safe.

    The stroke floor is half each line's cutter diameter: two engraved
    strokes whose centre-lines come closer than that overlap. The configured
    ``descending_character_clearance`` is a **unitless factor of that floor**
    (``required = floor * (1 + clearance)``), so the demanded air scales with
    the cutter width and reads the same at every text size.

    Args:
        upper_cutter: Cutter diameter of the descending glyph's line, inches.
        lower_cutter: Cutter diameter of the following line, inches.
        clearance: Extra air as a fraction of the stroke floor
            (``descending_character_clearance``); ``0.0`` = touching only.

    Returns:
        The required clearance in inches (non-negative).
    """
    floor = max(0.0, 0.5 * (upper_cutter + lower_cutter))
    return floor * (1.0 + max(0.0, clearance))


def measure_pairs(
    upper_glyphs: Sequence[GlyphBox],
    lower_glyphs: Sequence[GlyphBox],
    required: float,
) -> list[PairGap]:
    """Measure every descending-glyph / following-line-glyph pair.

    Only glyphs that actually descend are considered as the upper member, so
    a line's plain capitals never trigger the mechanism. With ink knowledge
    (see :attr:`GlyphBox.ink`) the gap is the true 2D swept-ink distance --
    the rolling-circle test: the descending glyph's sub-baseline ink against
    the *complete* ink of each lower glyph. Glyphs built without ink fall
    back to the vertical box model. The lower line is indexed once through
    an :class:`~plt_optimizer.generate.geometry.InkGrid` sized to
    ``required``, which keeps the exact decision below the cell size.

    Every pair is reported (safe ones included) so the caller can both drive
    the relief and report the tightest clearance it found.

    Args:
        upper_glyphs: Glyph boxes of the upper line, in the label frame.
        lower_glyphs: Glyph boxes of the following line, in the label frame.
        required: Clearance from :func:`required_gap` for this line pair.

    Returns:
        One :class:`PairGap` per (descending upper, lower) pair, in input
        order. Empty when the upper line has no descending glyph.
    """
    descending = [upper for upper in upper_glyphs if upper.descends]
    if not descending:
        return []

    ink_lower = any(lower.ink for lower in lower_glyphs)
    grid: Optional[InkGrid] = None
    if ink_lower and required > 0.0:
        lower_ink: Ink = tuple(polyline for lower in lower_glyphs for polyline in lower.ink)
        grid = InkGrid(lower_ink, required)

    pairs: list[PairGap] = []
    for upper in descending:
        for lower in lower_glyphs:
            pairs.append(
                PairGap(
                    upper=upper,
                    lower=lower,
                    gap=_pair_gap(upper, lower, grid, ink_lower),
                    required=required,
                )
            )
    return pairs


def _pair_gap(
    upper: GlyphBox,
    lower: GlyphBox,
    grid: Optional[InkGrid],
    ink_lower: bool,
) -> float:
    """Measure one (descending upper, lower) pair's gap.

    Ink knowledge on both sides gives the true 2D swept-ink distance: the
    upper glyph's sub-baseline ink (the part hanging into the gap; a
    descending glyph always has some) rolled against the lower glyph's
    complete ink. Without a grid (zero required clearance, or a lower line
    without ink) the pair measures through
    :func:`~plt_optimizer.generate.geometry.ink_gap` directly; without ink
    on the upper side it falls back to the vertical box gap.

    Args:
        upper: The descending glyph of the upper line.
        lower: The glyph on the following line.
        grid: Grid indexing the lower line's complete ink, sized to the
            required clearance (``None`` when unusable).
        ink_lower: Whether any lower-line glyph carries ink knowledge.

    Returns:
        The pair's gap, inches; ``inf`` when unmeasurable.
    """
    if not upper.sub_baseline_ink:
        if upper.ink and ink_lower:
            # A descending glyph whose ink clips entirely above the baseline
            # cannot reach into the gap; treat it like the box model's
            # X-disjoint sentinel.
            return math.inf
        if not upper.ink and not ink_lower:
            return aabb_vertical_gap(upper.bounds, lower.bounds)
        return math.inf
    if not lower.ink:
        return math.inf
    if grid is not None:
        return grid.min_gap(upper.sub_baseline_ink)
    return ink_gap(upper.sub_baseline_ink, lower.ink)


@dataclass(frozen=True)
class GlyphGap:
    """One descending glyph's tightest clearance against the line below.

    The production measurement unit of the relief: the rolling-circle test
    asks, for each descending glyph, how close its sub-baseline ink comes to
    *any part of any glyph* of the following line. The per-glyph aggregate is
    what drives the ramp and the spacing growth; :class:`PairGap` keeps the
    per-partner detail the box-model fallback reports.

    Attributes:
        upper: The descending glyph of the upper line.
        gap: Tightest swept-ink distance from the glyph's sub-baseline ink
            to the lower line's complete ink (true 2D; the box-model minimum
            over partners without ink knowledge). ``inf`` when unmeasurable.
        required: Clearance from :func:`required_gap` for this line pair.
    """

    upper: GlyphBox
    gap: float
    required: float

    @property
    def deficit(self) -> float:
        """How much more clearance this glyph needs, in inches (``0.0`` if safe)."""
        return max(0.0, self.required - self.gap)

    @property
    def collides(self) -> bool:
        """True when this glyph falls short of :attr:`required`."""
        return self.deficit > _GAP_TOLERANCE


def measure_glyph_gaps(
    upper_glyphs: Sequence[GlyphBox],
    lower_glyphs: Sequence[GlyphBox],
    required: float,
) -> list[GlyphGap]:
    """Measure each descending glyph's tightest gap against the line below.

    The grid-accelerated production path of :func:`measure_pairs`: the lower
    line's complete ink is indexed once (cell = ``required``, keeping the
    ``gap < required`` decision exact), then every descending upper glyph
    queries its sub-baseline ink against the whole line in one shot. Glyphs
    without ink knowledge fall back to the minimum vertical box gap over
    their partners.

    Args:
        upper_glyphs: Glyph boxes of the upper line, in the label frame.
        lower_glyphs: Glyph boxes of the following line, in the label frame.
        required: Clearance from :func:`required_gap` for this line pair.

    Returns:
        One :class:`GlyphGap` per descending upper glyph, in input order.
        Empty when the upper line has no descending glyph.
    """
    descending = [upper for upper in upper_glyphs if upper.descends]
    if not descending:
        return []

    ink_lower = any(lower.ink for lower in lower_glyphs)
    grid: Optional[InkGrid] = None
    lower_ink: Ink = ()
    if ink_lower:
        lower_ink = tuple(polyline for lower in lower_glyphs for polyline in lower.ink)
        if required > 0.0:
            grid = InkGrid(lower_ink, required)

    gaps: list[GlyphGap] = []
    for upper in descending:
        if upper.sub_baseline_ink:
            gap = (
                grid.min_gap(upper.sub_baseline_ink)
                if grid is not None
                else ink_gap(upper.sub_baseline_ink, lower_ink)
            )
        elif not upper.ink and not ink_lower:
            box_gaps = [
                value
                for value in (
                    aabb_vertical_gap(upper.bounds, lower.bounds) for lower in lower_glyphs
                )
                if is_finite_gap(value)
            ]
            gap = min(box_gaps, default=math.inf)
        else:
            gap = math.inf
        gaps.append(GlyphGap(upper=upper, gap=gap, required=required))
    return gaps


def worst_glyph_deficit(gaps: Sequence[GlyphGap]) -> float:
    """Return the largest clearance shortfall among a line's glyph gaps.

    Args:
        gaps: Per-glyph gaps measured for one inter-line gap.

    Returns:
        The residual deficit in inches (``0.0`` when every glyph is satisfied
        or unmeasurable).
    """
    return max(
        (gap.deficit for gap in gaps if is_finite_gap(gap.gap)),
        default=0.0,
    )


def ramp_ink(ink: Ink, baseline_y: float, scale_y: float, raise_amount: float) -> Ink:
    """Apply a relief ramp to glyph ink given in the stacked frame.

    Mirrors what the renderer does to the real strokes (``TextBlock.scaled_y``
    + ``translate`` in the line's local frame): Y is scaled about the glyph's
    baseline and the glyph is raised. In the stacked frame the baseline sits
    at ``baseline_y``, so a point ``y`` maps to ``(y - baseline_y) * scale_y +
    raise_amount + baseline_y``. X is untouched.

    Args:
        ink: The glyph's ink (polylines) in the stacked frame.
        baseline_y: The glyph's line baseline Y in the same frame, inches.
        scale_y: Vertical scale about the baseline (from :func:`apply_ramp`).
        raise_amount: Upward translation, inches (from :func:`apply_ramp`).

    Returns:
        The ramped ink, same frame.
    """
    shift = raise_amount + baseline_y
    return tuple(
        tuple((x, (y - baseline_y) * scale_y + shift) for x, y in polyline) for polyline in ink
    )


def ramp_for_measured(
    gap_at: Callable[[float], float],
    required: float,
) -> float:
    """Return the smallest ramp value clearing a pair measured through ink.

    The true 2D swept-ink distance is not linear in the ramp (the vertical
    box gap of :func:`ramp_for` is), so the smallest clearing ramp is found
    by bisection on ``gap_at(t) >= required``. ``gap_at`` is monotone for
    every geometry the relief acts on (the whole sub-baseline ink rises as
    ``t`` grows); a deeply penetrating pair whose distance dips on the way
    out converges to *a* crossing, and the caller's fixpoint re-measures the
    applied ramp and grows it further when needed, so a slight
    under-proposal is self-correcting.

    Args:
        gap_at: The pair's gap (inches) at a ramp value, in ``[0, 1]``.
        required: Required clearance, inches.

    Returns:
        The ramp value in ``[0, 1]``: ``0.0`` when the pair is already safe
        at ``t = 0``, ``1.0`` when the allowances are exhausted (the pair
        may still fall short -- the caller detects that by re-measuring).
    """
    if gap_at(0.0) >= required - _GAP_TOLERANCE:
        return 0.0
    if gap_at(1.0) < required - _GAP_TOLERANCE:
        return 1.0
    lower = 0.0
    upper = 1.0
    for _ in range(_RAMP_BISECTION_MAX_ITERATIONS):
        middle = (lower + upper) / 2.0
        if upper - lower <= _RAMP_BISECTION_TOLERANCE:
            break
        if gap_at(middle) >= required - _GAP_TOLERANCE:
            upper = middle
        else:
            lower = middle
    return upper


def ramp_for(
    gap: float,
    required: float,
    descender: float,
    v_scale: float,
    raise_fraction: float,
) -> float:
    """Return the smallest ramp value clearing one pair, clamped to ``[0, 1]``.

    The closed form of the **box model**: solves ``gap + t * (raise_cap +
    descender * (1 - v_scale)) >= required`` for the smallest admissible
    ``t``, where ``raise_cap = raise_fraction * descender``. It is exact for
    the vertical box gap (linear in ``t``); ink-measured pairs use
    :func:`ramp_for_measured` (bisection). ``0.0`` means the pair is already
    safe; ``1.0`` means the allowances are exhausted (fully spent, and the
    pair may still fall short -- the caller detects that by re-measuring
    after applying the ramp).

    Args:
        gap: Current clearance of the pair, inches (``inf`` = no X overlap).
        required: Required clearance, inches.
        descender: The descending glyph's descender depth, inches (> 0).
        v_scale: Smallest permitted vertical scale (``descending_character_
            v_scale``); ``1.0`` forbids downscaling.
        raise_fraction: Largest permitted raise as a fraction of the
            descender (``descending_character_raise``); ``0.0`` forbids raising.

    Returns:
        The ramp value in ``[0, 1]``. ``0.0`` when the pair is safe, the
        glyph does not descend, the pair has no X overlap, or no relief is
        permitted.
    """
    deficit = required - gap
    if deficit <= _GAP_TOLERANCE:
        return 0.0
    if descender <= _DESCENDER_EPSILON:
        return 0.0

    raise_cap = max(0.0, raise_fraction) * descender
    slope = raise_cap + descender * (1.0 - min(1.0, max(0.0, v_scale)))
    if slope <= _GAP_TOLERANCE:
        # Both allowances are zero: the ramp can never help.
        return 0.0
    return min(1.0, deficit / slope)


def worst_deficit(pairs: Sequence[PairGap]) -> float:
    """Return the largest clearance shortfall among a gap's pairs.

    Args:
        pairs: Pairs measured for one inter-line gap.

    Returns:
        The residual deficit in inches (``0.0`` when every pair is satisfied
        or has no horizontal overlap).
    """
    return max(
        (pair.deficit for pair in pairs if is_finite_gap(pair.gap)),
        default=0.0,
    )


def apply_ramp(
    descender: float,
    v_scale: float,
    raise_fraction: float,
    ramp: float,
) -> tuple[float, float]:
    """Split a ramp value into its ``(scale_y, raise_amount)`` pair.

    Args:
        descender: The glyph's descender depth, inches.
        v_scale: Smallest permitted vertical scale.
        raise_fraction: Largest permitted raise fraction.
        ramp: Ramp value in ``[0, 1]``.

    Returns:
        ``(scale_y, raise_amount)`` in ``(0, 1]`` and ``[0, descender]``:
        scale the glyph's Y about its baseline by ``scale_y``, then translate
        it up by ``raise_amount``.
    """
    clamped = min(1.0, max(0.0, ramp))
    scale_y = 1.0 - clamped * (1.0 - min(1.0, max(0.0, v_scale)))
    raise_amount = clamped * max(0.0, raise_fraction) * descender
    return scale_y, raise_amount


def merge_ramps(
    current: Mapping[int, float],
    proposed: Mapping[int, float],
) -> dict[int, float]:
    """Combine two ramp maps keeping the largest value per glyph.

    The relief is **one-way**: a glyph only ever gets smaller and higher, so
    the fixpoint loop converges (each pass can only raise a ramp, and every
    ramp is capped at ``1.0``).

    Args:
        current: Ramps already applied, keyed by glyph index.
        proposed: Ramps the latest measurement asks for.

    Returns:
        The per-glyph maximum of both maps (glyphs absent from both are
        absent from the result).
    """
    merged = dict(current)
    for glyph_index, ramp in proposed.items():
        if ramp > merged.get(glyph_index, 0.0):
            merged[glyph_index] = ramp
    return merged


def ramps_changed(
    before: Mapping[int, float],
    after: Mapping[int, float],
    tolerance: float = _RAMP_TOLERANCE,
) -> bool:
    """Return whether any ramp grew beyond ``tolerance`` between two maps.

    Args:
        before: Ramps before a pass.
        after: Ramps after a pass.
        tolerance: Growth below this counts as no change.

    Returns:
        ``True`` when at least one glyph's ramp grew meaningfully.
    """
    return any(
        value - before.get(glyph_index, 0.0) > tolerance for glyph_index, value in after.items()
    )


def is_finite_gap(gap: float) -> bool:
    """Return whether a measured gap describes an actual X overlap.

    Args:
        gap: Gap reported by
            :func:`~plt_optimizer.generate.geometry.aabb_vertical_gap`.

    Returns:
        ``False`` for the ``inf`` sentinel (disjoint X spans: the pair can
        never collide and must not drive any relief).
    """
    return math.isfinite(gap)
