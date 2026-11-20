"""Text-hole collision detection, avoidance phases, and job gate.

The collision system in one place: the stroke-aware threshold, detection
of (text line, drill hole) pairs below it, the opt-in avoidance phases
(hole-margin reduction, the vertical ladder for top/bottom holes - the rigid
block slide and the inter-line packing - the edge-margin slide for
edge-aligned text, the alignment switch to center plus horizontal
compression, and the sibling flush onto the switched lines' final edge), the
unresolvable-collision diagnostics, and the job-level
:func:`assert_no_collisions` gate.

Which ladder runs is decided by :func:`collision.holes.hole_escape_axes`: the
edges the label's holes sit on are the ONLY axes a clearance can be bought on,
so a label with nothing but top/bottom holes never squeezes glyph widths. Which
direction the inter-line gaps move in - packed (top/bottom/corner holes) or
spread (side holes on an even-line block) - is decided by
:mod:`collision.spacing_policy`, so the two directions are never offered where
they would push lines into the holes.

The compression sweep must re-render text to measure the compressed
bounds; the renderer lives in
:mod:`plt_optimizer.generate.label_renderer`, so the sweeps take an
injected ``render`` callback (the same seam as the line-content
``ScaleProbe``) instead of importing the renderer - this module has zero
runtime imports from ``label_renderer``.

Moved verbatim from ``label_renderer.py`` when the collision package was
created; the contract is ``AGENTS.md`` beside this file.
"""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from plt_optimizer.generate.collision.geometry import CollisionResult, circle_aabb_gap
from plt_optimizer.generate.collision.holes import hole_circles_local, hole_escape_axes
from plt_optimizer.generate.collision.shapes import rounded_rect_corner
from plt_optimizer.generate.collision.spacing_policy import spacing_strategy_summary
from plt_optimizer.generate.resolution import (
    ResolvedLabel,
    rounded_rect_content_inset,
)

if TYPE_CHECKING:
    from plt_optimizer.generate.label_renderer import RenderedLabel, _LineEntry

logger = logging.getLogger(__name__)

# Render callback: renders a label's text and reports the per-line bounds
# records (see label_renderer._render_text_local_with_bounds).
LineBoundsRender = Callable[["ResolvedLabel"], Tuple[Any, List["_LineEntry"]]]

# Resolution sweep granularity for text-hole collision avoidance. The
# margin sweep reuses already-rendered text geometry (holes move, text
# does not), so it is cheap; the compression sweep re-renders text per
# step, so it stays small.
_MARGIN_ADJUST_STEPS: int = 32
_COMPRESSION_RESOLVE_STEPS: int = 16

# Resolution sweep granularity for the vertical packing phase (2c). Like the
# compression sweep it re-renders the text per step, so it stays small; the
# block slide (2b) is analytical and needs no sweep at all.
_SPACING_RESOLVE_STEPS: int = 16

# The tolerance under which a slide/scale is treated as no movement at all.
_SHIFT_EPSILON_IN: float = 1e-12


class LabelRenderError(Exception):
    """Raised to abort a job containing unavoidable text-hole collisions.

    Emitted by :func:`assert_no_collisions` once every label in the job has
    been rendered (so all per-label ERROR diagnostics were logged first)
    and at least one label still overlaps a drill hole. Collisions that
    collision avoidance repaired are tolerated (logged as WARNING at render
    time); only collisions that survive every enabled avoidance phase are
    unacceptable output requiring a jobspec revision.
    """


def _frame_shift_y(label: ResolvedLabel, band_center_offset_inches: float = 0.0) -> float:
    """The Y the export centers the text block on, in the label-local frame.

    Single source of truth for the collision frame, mirroring
    ``label_renderer._center_text_layer_vertically``: the label center,
    lowered by the band-centering offset, by the hole-diameter shift, and by
    the Phase-2b collision slide (all three lower the expected center, so all
    three are subtracted). Every detection site measures in this one frame, so
    a clone carrying any combination of shifts cannot desynchronise from the
    geometry that is emitted.

    Args:
        label: The resolved label whose shift terms should be summed.
        band_center_offset_inches: The band-center offset of the bounds being
            measured (inches); ``0.0`` when the label centers by ink.

    Returns:
        The collision frame's Y origin, inches.
    """
    return (
        label.height / 2.0
        - band_center_offset_inches
        - label.hole_aware_vertical_shift_inches
        - label.collision_vertical_shift_inches
    )


def collision_threshold(label: ResolvedLabel, line_index: int) -> float:
    """Compute the stroke-aware collision threshold for one text line.

    Collision checks measure the gap between *toolpath geometry* (text
    bounding box vs. hole circle), but the machine removes material half a
    cutter width on each side of every path. Two strokes therefore touch
    once the geometric gap reaches ``0.5 * (hole_cutter + text_cutter)``
    (the stroke floor), and stay visibly separated only beyond that floor
    plus the requested air gap:

    ``threshold = 0.5 * (hole_cutter + text_cutter[line]) +
    hole_text_collision_distance``

    Both cutters are the **engraved** widths (the physical tool plus its
    cascaded kerf buffer -- see :func:`resolution.effective_cutter_diameter`),
    not the nominal diameters: a roughly 45-degree-flank V-bit removes more
    material than its name suggests, and the collision floor is exactly a
    width-of-material-removed question.

    A gap at or above the threshold is safe (strokes are separated by at
    least ``hole_text_collision_distance``); below it the engraved strokes
    bleed into each other.

    Args:
        label: The resolved label providing cutter diameters and the
            cascaded collision distance.
        line_index: Index of the text line within ``label.content``.

    Returns:
        The minimum safe geometric gap in inches.
    """
    if 0 <= line_index < len(label.content):
        line = label.content[line_index]
        # The widest stroke the line engraves: a ``<b>`` line's bold run
        # engraves thicker, a large ``<scale:N>`` run engraves with a larger
        # tool, and the whole-line AABB is measured against it.
        text_cutter = line.effective_max_cutter_diameter
    else:  # Defensive: unknown line (should not happen) assumes no stroke.
        text_cutter = 0.0
    stroke_floor = 0.5 * (label.effective_hole_cutter_diameter + text_cutter)
    return stroke_floor + label.hole_text_collision_distance


def detect_text_hole_collisions(
    label: ResolvedLabel,
    line_entries: Sequence[_LineEntry],
    band_center_offset_inches: float = 0.0,
) -> List[CollisionResult]:
    """Find all (text line, drill hole) pairs closer than the threshold.

    A pair collides when its geometric gap is below the stroke-aware
    threshold of :func:`collision_threshold` (stroke floor plus the
    cascaded ``hole_text_collision_distance``), so near misses that would
    make the engraved strokes bleed together are reported too. A gap
    exactly equal to the threshold is safe.

    Collision checks run in label-local, y-up coordinates before plate
    placement. The rendered line bounds are anchored around y=0 by
    :func:`plt_optimizer.generate.label_renderer._render_text_local_with_bounds`;
    the export pipeline vertically
    centers the block at ``height / 2`` (``_center_text_layer_vertically``),
    so bounds are shifted by that amount before the check. Band centering
    lowers that target by ``band_center_offset_inches`` (the band, not the ink,
    lands on the inner-area center), and the hole-aware shift
    (``label.hole_aware_vertical_shift_inches``) lowers it further for
    asymmetric top/bottom holes. The export's Y-flip mirrors text and holes
    across the *same* centerline, which preserves circle/AABB intersection
    relationships exactly, so the check is valid in this pre-flip frame.

    Args:
        label: The resolved label providing hole positions, cutter
            diameters and the collision distance.
        line_entries: Per-line :class:`_LineEntry` records as returned by
            :func:`plt_optimizer.generate.label_renderer._render_text_local_with_bounds`.
        band_center_offset_inches: The band-center offset the renderer applied
            to these bounds (inches); ``0.0`` when the label centers by ink.

    Returns:
        One :class:`CollisionResult` per colliding (line, hole) pair.
        Empty when the label has no holes or no rendered lines.
    """
    if not label.holes or not line_entries:
        return []

    circles = hole_circles_local(label)
    # Mirrors _center_text_layer_vertically: band offset + hole-aware shift +
    # collision slide all lower the expected center, so all are subtracted.
    shift_y = _frame_shift_y(label, band_center_offset_inches)
    results: List[CollisionResult] = []

    for entry in line_entries:
        line_index, line_text, bounds = entry.line_index, entry.line_text, entry.bounds
        threshold = collision_threshold(label, line_index)
        x_min, y_min, x_max, y_max = bounds
        shifted: Tuple[float, float, float, float] = (
            x_min,
            y_min + shift_y,
            x_max,
            y_max + shift_y,
        )
        for hole_index, (center_x, center_y, radius) in enumerate(circles):
            # The gap is text-box to drill-hole circle -- a shape-agnostic
            # measurement (the label shape enters through the hole
            # POSITIONS, which hole_circles_local places on the ellipse for
            # round labels, and through the radial content inset, which
            # keeps the text box itself inside the inset ellipse). The
            # engraved-boundary clearance is the margin system's job, not
            # this check's, exactly as on rectangular labels.
            gap = circle_aabb_gap(shifted, (center_x, center_y), radius)
            if gap < threshold:
                results.append(
                    CollisionResult(
                        line_index=line_index,
                        line_text=line_text,
                        hole_index=hole_index,
                        hole_location=str(label.holes[hole_index].location),
                        gap=gap,
                    )
                )
    return results


def log_collisions(
    label: ResolvedLabel,
    collisions: Sequence[CollisionResult],
    level: int = logging.ERROR,
) -> None:
    """Log each detected text-hole collision at the given severity.

    Args:
        label: The resolved label being checked (provides the threshold
            components used in the message breakdown).
        collisions: Collision results from :func:`detect_text_hole_collisions`.
        level: Logging severity. WARNING when collision avoidance resolved
            the collisions, ERROR when they remain unresolved.
    """
    for collision in collisions:
        required = collision_threshold(label, collision.line_index)
        clearance = label.hole_text_collision_distance
        stroke_floor = required - clearance
        logger.log(
            level,
            "Label %s: text line %d (%r) collides with %s drill hole "
            "(index %d); gap %.4fin is below the required %.4fin "
            "(%.4fin clearance + %.4fin stroke floor).",
            label.id,
            collision.line_index,
            collision.line_text,
            collision.hole_location,
            collision.hole_index,
            collision.gap,
            required,
            clearance,
            stroke_floor,
        )


def collision_line_desc(collisions: Sequence[CollisionResult]) -> str:
    """Describe the distinct colliding text lines for log messages.

    Args:
        collisions: Collision results from :func:`detect_text_hole_collisions`.

    Returns:
        A human-readable summary such as ``line 0 ('TOP/BOT DRILL')``, with
        multiple distinct lines joined by ``"; "``. Empty string when there
        are no collisions.
    """
    seen: List[str] = []
    for collision in collisions:
        desc = f"line {collision.line_index} ({collision.line_text!r})"
        if desc not in seen:
            seen.append(desc)
    return "; ".join(seen)


def resolve_via_margin_adjustment(
    label: ResolvedLabel,
    line_entries: Sequence[_LineEntry],
    line_desc: str = "",
    band_center_offset_inches: float = 0.0,
) -> Optional[ResolvedLabel]:
    """Phase 2: shrink ``hole_margin`` toward ``min_hole_margin`` (no re-render).

    Drill-hole positions are pure functions of ``hole_margin``
    (:func:`hole_circles_local`), so candidate margins are evaluated
    analytically against the already-rendered text bounds -- the text
    geometry never changes in this phase. The sweep walks from the current
    margin down to the configured floor and returns the first (smallest)
    reduction that clears every collision.

    Args:
        label: The label whose collision should be resolved.
        line_entries: Rendered text bounds from the initial render.
        line_desc: Optional description of the offending text lines,
            included in the resolution WARNING for informative output.
        band_center_offset_inches: The band-center offset of the supplied
            ``line_entries`` (inches); ``0.0`` when the label centers by ink.

    Returns:
        A clone of ``label`` with a reduced ``hole_margin`` when one clears
        all collisions, otherwise ``None`` (no floor configured, no
        reduction budget, or the floor still collides).
    """
    if label.min_hole_margin is None:
        return None
    floor = max(0.0, label.min_hole_margin)
    if label.hole_margin <= floor:
        return None

    steps = max(1, _MARGIN_ADJUST_STEPS)
    for i in range(1, steps + 1):
        candidate = label.hole_margin - (label.hole_margin - floor) * i / steps
        candidate_label = replace(label, hole_margin=candidate)
        if not detect_text_hole_collisions(
            candidate_label, line_entries, band_center_offset_inches
        ):
            logger.warning(
                "Label %s: adjusted hole_margin from %.4fin to %.4fin to "
                "avoid text-hole collision on %s (minimum allowed %.4fin).",
                label.id,
                label.hole_margin,
                candidate,
                line_desc or "colliding text",
                floor,
            )
            return candidate_label
    return None


def _content_band(label: ResolvedLabel) -> Tuple[float, float]:
    """The alignment content box ``[bottom_edge, top_edge]`` in LABEL-LOCAL Y.

    The Y mirror of :func:`_content_edges`: the band spans
    ``[v_margin, height - v_margin]``, where a rounded-rectangle label's
    effective ``v_margin`` is widened by the corner inset (the same rule
    :func:`_render_text_local_with_bounds` applies, so the caps match where the
    renderer will actually place the block).

    Same frame as the ``frame`` list inside
    :func:`resolve_via_vertical_shift` (the entries' collision bounds lifted by
    :func:`_frame_shift_y`), which is what makes a block that fills its box
    report ZERO slack in both directions -- the condition that stands the slide
    down and hands the label to the packing phase.

    Args:
        label: The resolved label to measure.

    Returns:
        ``(bottom_edge, top_edge)`` in inches, label-local Y.
    """
    v_margin = label.v_margin
    corner = rounded_rect_corner(label)
    if corner is not None:
        _content_width, content_height = rounded_rect_content_inset(
            label.width, label.height, label.h_margin, v_margin, corner
        )
        v_margin = max(v_margin, (label.height - content_height) / 2.0)
    return v_margin, label.height - v_margin


def _required_vertical_shift(
    bounds: Tuple[float, float, float, float],
    center_x: float,
    center_y: float,
    radius: float,
    threshold: float,
    direction: str,
) -> float:
    """The block slide that clears one (line, hole) collision on the Y axis.

    The Y mirror of :func:`_required_edge_shift`. Sliding the block is a rigid
    vertical translate of the line's AABB, so ``circle_aabb_gap`` is monotonic
    in the slide once the line's edge has passed the hole's centre, and the
    slide reaching the threshold solves in closed form: the near edge travels to
    ``centre -+ root`` with ``root = sqrt((threshold + r)^2 - dx^2)``. A
    colliding hole sits inside the line's Y span, so the slide is the edge-to-
    centre distance plus ``root`` (the edge has to pass the far side of the
    hole's clearance ring).

    Args:
        bounds: The line's collision-frame AABB.
        center_x: Hole centre X.
        center_y: Hole centre Y.
        radius: Hole radius.
        threshold: The line's stroke-aware collision threshold, inches.
        direction: ``"down"`` (slide away from a hole ABOVE the line) or
            ``"up"`` (away from a hole BELOW it).

    Returns:
        The minimum non-negative slide in inches.
    """
    x_min, y_min, x_max, _y_max = bounds
    dx = max(x_min - center_x, 0.0, center_x - x_max)
    root = math.sqrt(max(0.0, (threshold + radius) ** 2 - dx * dx))
    if direction == "down":
        return max(0.0, bounds[3] - center_y + root)
    return max(0.0, center_y - y_min + root)


def _max_vertical_shift(
    bounds: Tuple[float, float, float, float],
    direction: str,
    circles: Sequence[Tuple[float, float, float]],
    threshold: float,
) -> float:
    """How far the block may slide before a far-side hole starts colliding.

    The Y mirror of :func:`_max_edge_shift` (the user's rule: slide until a
    hole collides on the far side, never past it). The content band is NOT
    measured here -- the caller caps the slide by the band's slack as well.

    "Far side" is decided against the line's CENTRE, not its near edge: a hole
    whose centre lies inside the line's Y span is on the side the block is
    running toward, and the cap is then 0.0 (the slide would carry the line
    deeper into it). Deciding on ``center_y < y_min`` would call such a hole
    "near side", report no cap, and let a squeezed block slide into the very
    hole it is fleeing.

    Args:
        bounds: The line's collision-frame AABB.
        direction: ``"down"`` (the far side is BELOW) or ``"up"``.
        circles: All hole circles (label-local, collision frame).
        threshold: The line's stroke-aware collision threshold, inches.

    Returns:
        The maximum non-negative slide in inches.
    """
    x_min, y_min, x_max, y_max = bounds
    center_y_line = (y_min + y_max) / 2.0
    cap = math.inf
    for center_x, center_y, radius in circles:
        dx = max(x_min - center_x, 0.0, center_x - x_max)
        root_sq = (threshold + radius) ** 2 - dx * dx
        root = math.sqrt(root_sq) if root_sq > 0.0 else 0.0
        if direction == "down" and center_y < center_y_line:
            cap = min(cap, y_min - center_y - root)
        elif direction == "up" and center_y > center_y_line:
            cap = min(cap, center_y - y_max - root)
    return max(0.0, cap)


def resolve_via_vertical_shift(
    label: ResolvedLabel,
    line_entries: Sequence[_LineEntry],
    band_center_offset_inches: float = 0.0,
    line_desc: str = "",
) -> Optional[ResolvedLabel]:
    """Phase 2b: slide the whole text block off its top/bottom holes.

    A hole on the top edge is escaped by opening room on the Y axis: nothing a
    line does in X moves it off a hole that spans the row. This phase moves the
    block as one rigid unit (``collision_vertical_shift_inches``, positive =
    downward) by the closed-form slide that clears the dominant side, so every
    glyph keeps its width and every inter-line gap keeps its value -- which is
    what an engraver sees: a block that sits a little off-centre, not squeezed
    type.

    The slide is capped twice, and never past either cap:

    * by the shift at which a hole on the FAR side starts colliding (the block
      may not be pushed into the hole it is running away from), and
    * by the content band's slack: the block's own extremes stay inside
      ``[v_margin, height - v_margin]``, so a slide can never put ink in a
      margin.

    When holes on both sides collide, each direction's reach is its capped slide
    (``min(need, cap)``) and the side that gets FURTHEST moves; the residual is
    what the next phase (2c, packing) cleans up. A direction with no room at all
    (a block squeezed between a near hole and the opposite margin) has reach 0
    and is not chosen. The slide is analytical, so this phase costs no sweep
    renders; the caller re-renders to verify. Each run ADDS to the slide a
    previous round applied (measured from the already-shifted bounds), so
    re-running converges instead of compounding.

    Args:
        label: The label whose collision should be resolved (may already carry
            a Phase 2 margin reduction and an earlier slide of this phase).
        line_entries: Rendered text bounds (the current frame).
        band_center_offset_inches: The band-center offset of ``line_entries``.
        line_desc: Optional description of the offending lines for the
            resolution WARNING.

    Returns:
        A clone of ``label`` with a larger ``collision_vertical_shift_inches``
        when the block can slide, otherwise ``None`` (no holes, no collisions,
        or the slide is fully capped). The clone may only PARTIALLY resolve the
        collisions; the caller re-detects and escalates.
    """
    if not label.holes or not line_entries:
        return None
    circles = hole_circles_local(label)
    shift_y = _frame_shift_y(label, band_center_offset_inches)
    frame = [
        (
            entry.line_index,
            (
                entry.bounds[0],
                entry.bounds[1] + shift_y,
                entry.bounds[2],
                entry.bounds[3] + shift_y,
            ),
        )
        for entry in line_entries
    ]
    bounds_by_line = dict(frame)
    collisions = detect_text_hole_collisions(label, line_entries, band_center_offset_inches)
    if not collisions:
        return None

    # What the block must move, per direction: every collision whose hole is
    # above the line's centre pushes the block down, every one below pushes it
    # up. The block is one rigid unit, so a direction's need is its tightest.
    need: Dict[str, float] = {}
    for collision in collisions:
        bounds = bounds_by_line.get(collision.line_index)
        if bounds is None:  # pragma: no cover - a collision always has an entry
            continue
        center_x, center_y, radius = circles[collision.hole_index]
        direction = "down" if center_y > (bounds[1] + bounds[3]) / 2.0 else "up"
        required = _required_vertical_shift(
            bounds,
            center_x,
            center_y,
            radius,
            collision_threshold(label, collision.line_index),
            direction,
        )
        need[direction] = max(need.get(direction, 0.0), required)
    if not need:
        return None

    # The dominant side moves (the larger need clears the tighter constraint;
    # the other side is what the packing phase inherits). Ties prefer "down" so
    # a symmetric job resolves deterministically.
    direction = max(need, key=lambda side: (need[side], side == "down"))

    # Caps, per direction: (1) the far-side holes, over every line of the block
    # (the slide is shared, so the tightest line binds the whole block), and
    # (2) the content frame -- the block's own extremes may not enter a margin.
    # Frame 2 is measured from the BAND box (the renderer centres
    # ``band`` in ``[v_margin, height - v_margin]``, so the slack to the far
    # edge is exactly the shift that puts a block extreme on the box). A block
    # that overflows its box (the renderer clamps its gaps to the box on every
    # pass) gets 0.0 slack: the slide stands down and the packing phase below
    # takes the label over.
    block_bottom = min(bounds[1] for _index, bounds in frame)
    block_top = max(bounds[3] for _index, bounds in frame)
    bottom_edge, top_edge = _content_band(label)
    far_cap: Dict[str, float] = {}
    band_slack: Dict[str, float] = {}
    slack: Dict[str, float] = {}
    for side in need:
        far = math.inf
        for line_index, bounds in frame:
            far = min(
                far,
                _max_vertical_shift(bounds, side, circles, collision_threshold(label, line_index)),
            )
        far_cap[side] = far
        band_slack[side] = max(
            0.0, (block_bottom - bottom_edge) if side == "down" else (top_edge - block_top)
        )
        slack[side] = min(far, band_slack[side])

    # Both directions are capped, so the side that gets FURTHEST toward clearing
    # moves -- reach = min(need, cap), ties preferring "down" for determinism.
    # A block squeezed between a near hole and the opposite margin has no room
    # away from the hole (reach 0), so the slack side's partial slide runs first
    # and the label then falls through to the packing phase.
    direction = max(need, key=lambda side: (min(need[side], slack[side]), side == "down"))
    cap = slack[direction]

    slide = min(need[direction], cap)
    if slide <= _SHIFT_EPSILON_IN:
        return None

    capped_by = ""
    if slide < need[direction] - _SHIFT_EPSILON_IN:
        capped_by = (
            ", capped by the content band"
            if band_slack[direction] <= far_cap[direction]
            else ", capped by the far-side hole"
        )
    logger.warning(
        "Label %s: slid the text block %.4fin %s to avoid text-hole collision on %s "
        "(needed %.4fin%s).",
        label.id,
        slide,
        "downward" if direction == "down" else "upward",
        line_desc or "colliding text",
        need[direction],
        capped_by,
    )
    return replace(
        label,
        collision_vertical_shift_inches=label.collision_vertical_shift_inches
        + (slide if direction == "down" else -slide),
    )


def resolve_via_spacing_reduction(
    label: ResolvedLabel,
    *,
    render: LineBoundsRender,
    band_center_offset_inches: float = 0.0,
    line_desc: str = "",
) -> Optional[ResolvedLabel]:
    """Phase 2c: pack the text block by shrinking every inter-line gap.

    The last vertical knob: with the holes at their ``min_hole_margin`` floor
    and the block pinned by the band and the far-side holes, the only room left
    is BETWEEN the lines. Sweeps one label-level factor
    (``collision_spacing_scale``) from its current value down to the value that
    puts every gap at the cascaded ``min_line_spacing`` floor, and returns the
    least packing that clears every collision. Gaps shrink proportionally, so
    the block's rhythm survives; glyph widths do not move.

    Inert by construction where there is nothing to pack: a single-line label
    has no inter-line gap at all (Phase 2b is what saves those), and a label
    whose every gap already sits at the floor has no budget.

    Args:
        label: The label whose collision should be resolved (may already carry
            a Phase 2 margin reduction and a Phase 2b slide).
        render: Callback re-rendering a label's text and reporting the per-line
            bounds records (the injected
            ``label_renderer._render_text_local_with_bounds``).
        band_center_offset_inches: The band-center offset of the current bounds.
        line_desc: Optional description of the offending lines for the
            resolution WARNING.

    Returns:
        A clone of ``label`` with a reduced ``collision_spacing_scale`` when one
        clears all collisions, otherwise ``None``.
    """
    gaps = [line.line_spacing for line in label.content[:-1]]
    if not gaps:
        return None  # A single line has no gap to pack.
    floor = max(0.0, label.min_line_spacing or 0.0)
    shrinkable = [gap for gap in gaps if gap > floor + _SHIFT_EPSILON_IN]
    if not shrinkable:
        return None  # Every gap already sits at the configured floor.

    # The scale at which the LAST open gap reaches the floor: below it every
    # gap is clamped, so sweeping further changes nothing.
    scale_floor = min(floor / gap for gap in shrinkable)
    base = min(1.0, max(0.0, label.collision_spacing_scale))
    if base <= scale_floor + _SHIFT_EPSILON_IN:
        return None

    steps = max(1, _SPACING_RESOLVE_STEPS)
    for i in range(1, steps + 1):
        scale = base - (base - scale_floor) * i / steps
        candidate_label = replace(label, collision_spacing_scale=scale)
        _candidate_lc, candidate_entries = render(candidate_label)
        if not detect_text_hole_collisions(
            candidate_label, candidate_entries, band_center_offset_inches
        ):
            logger.warning(
                "Label %s: packed the text block to %.1f%% of its inter-line "
                "spacing (%s -> %s gaps, floor %.4fin) to avoid text-hole "
                "collision on %s.",
                label.id,
                scale * 100.0,
                ", ".join(f"{gap:.4f}in" for gap in gaps),
                ", ".join(f"{min(gap, max(floor, gap * scale)):.4f}in" for gap in gaps),
                floor,
                line_desc or "colliding text",
            )
            return candidate_label
    return None


def resolve_via_spacing_spread(
    label: ResolvedLabel,
    *,
    render: LineBoundsRender,
    band_center_offset_inches: float = 0.0,
    line_desc: str = "",
) -> Optional[ResolvedLabel]:
    """Spread the text block by growing every inter-line gap.

    The vertical twin of :func:`resolve_via_spacing_reduction`, and the first
    line of defense for a label whose holes sit on the left/right edges with an
    EVEN number of lines: growing every gap proportionally moves the middle two
    lines off mid-height, where side holes sit, so a gap takes their place and
    every line is left free to mind only the label margins. Sweeps one
    label-level factor (``collision_spacing_scale``, here above 1.0) from its
    current value up to the value that fills the space between the vertical
    margins, and returns the least spread that clears every collision. Glyph
    widths do not move.

    The spread is capped by the room the block already has INSIDE its own
    content band (the Y mirror of the caps in :func:`resolve_via_vertical_shift`
    and :func:`resolve_via_edge_margin`: the block's own extremes stay inside
    ``[v_margin, height - v_margin]``), so this phase can never push text into a
    margin: a block that already fills its band has no room and the phase is
    inert, exactly as packing is inert for a block whose gaps sit at the
    ``min_line_spacing`` floor. The renderer's margin clamp stays the hard
    backstop.

    Args:
        label: The label whose collision should be resolved.
        render: Callback re-rendering a label's text and reporting the per-line
            bounds records (the injected
            ``label_renderer._render_text_local_with_bounds``).
        band_center_offset_inches: The band-center offset of the current bounds.
        line_desc: Optional description of the offending lines for the
            resolution WARNING.

    Returns:
        A clone of ``label`` with a grown ``collision_spacing_scale`` when one
        clears all collisions, otherwise ``None`` - and the caller keeps the
        label it passed in, so a spread that cannot clear the holes costs
        nothing (there is no reason to spread the block if it will not help).
    """
    gaps = [line.line_spacing for line in label.content[:-1]]
    if not gaps:
        return None  # A single line has no gap to spread.
    requested_gaps = sum(gaps)
    if requested_gaps <= _SHIFT_EPSILON_IN:
        return None  # Every gap is closed; there is nothing to scale.

    _base_lc, base_entries = render(label)
    if not base_entries:
        return None
    shift_y = _frame_shift_y(label, band_center_offset_inches)
    block_bottom = min(entry.bounds[1] for entry in base_entries) + shift_y
    block_top = max(entry.bounds[3] for entry in base_entries) + shift_y
    bottom_edge, top_edge = _content_band(label)
    # The gaps grow proportionally about the block's centre, so the block's
    # height may grow by TWICE the slack on its tighter side; a block anchored
    # to one edge gets the conservative half of that, and the renderer's margin
    # clamp is the backstop for any overshoot the sweep probes.
    room = 2.0 * min(block_bottom - bottom_edge, top_edge - block_top)
    base = max(1.0, label.collision_spacing_scale)
    # The scale at which the grown block fills the band: past it the margin
    # clamp pins the block, so sweeping further changes nothing.
    ceiling = base + max(0.0, room) / requested_gaps
    if ceiling <= base + _SHIFT_EPSILON_IN:
        return None  # The block already fills its band; no room to spread.

    steps = max(1, _SPACING_RESOLVE_STEPS)
    for i in range(1, steps + 1):
        scale = base + (ceiling - base) * i / steps
        candidate_label = replace(label, collision_spacing_scale=scale)
        _candidate_lc, candidate_entries = render(candidate_label)
        if not detect_text_hole_collisions(
            candidate_label, candidate_entries, band_center_offset_inches
        ):
            logger.warning(
                "Label %s: spread the text block to %.1f%% of its inter-line "
                "spacing (%s -> %s gaps, %.4fin of %.4fin room) to avoid text-hole "
                "collision on %s.",
                label.id,
                scale * 100.0,
                ", ".join(f"{gap:.4f}in" for gap in gaps),
                ", ".join(f"{gap * scale:.4f}in" for gap in gaps),
                (scale - base) * requested_gaps,
                room,
                line_desc or "colliding text",
            )
            return candidate_label
    return None


def _content_edges(label: ResolvedLabel) -> Tuple[float, float]:
    """The alignment content box ``[left_edge, right_edge]`` in label-local X.

    Mirrors the renderer's Pass A/C frame: the box spans
    ``[h_margin, width - h_margin]``, where a rounded-rectangle label's
    effective ``h_margin`` is widened by the corner inset (the same rule
    ``_render_text_local_with_bounds`` applies, so the sweep's caps match
    where the renderer will actually anchor the lines).

    Args:
        label: The resolved label to measure.

    Returns:
        ``(left_edge, right_edge)`` in inches.
    """
    h_margin = label.h_margin
    corner = rounded_rect_corner(label)
    if corner is not None:
        content_width, _content_height = rounded_rect_content_inset(
            label.width, label.height, h_margin, label.v_margin, corner
        )
        h_margin = max(h_margin, (label.width - content_width) / 2.0)
    return h_margin, label.width - h_margin


def _max_edge_shift(
    bounds: Tuple[float, float, float, float],
    alignment: str,
    circles: Sequence[Tuple[float, float, float]],
    threshold: float,
) -> float:
    """How far an edge-aligned line may slide off its anchored margin.

    The cap is, per far-side hole, the shift at which that hole STARTS
    colliding (the user's rule: slide until a hole collides on the far side,
    never past it -- beyond that cap the line collides on BOTH sides, which
    is where the Phase-4 alignment switch takes over). The content-area box
    is NOT a cap: a slide that pushes the line's far end past the far margin
    is absorbed by the renderer's margin-fit compression (Pass A reserves the
    slide's room in the compression budget), so the anchored edge stays
    flush. The box only stops a slide through a hole.

    Args:
        bounds: The line's collision-frame AABB ``(x_min, y_min, x_max, y_max)``.
        alignment: ``"left"`` or ``"right"`` (the anchored side).
        circles: All hole circles (label-local, collision frame).
        threshold: The line's stroke-aware collision threshold, inches.

    Returns:
        The maximum non-negative slide in inches.
    """
    x_min, y_min, x_max, y_max = bounds
    cap = math.inf
    for center_x, center_y, radius in circles:
        dy = max(y_min - center_y, 0.0, center_y - y_max)
        root_sq = (threshold + radius) ** 2 - dy * dy
        if root_sq < 0.0:
            continue  # Vertically out of reach: a horizontal slide never collides.
        root = math.sqrt(root_sq)
        if alignment == "left" and center_x > x_max:
            cap = min(cap, center_x - root - x_max)
        elif alignment == "right" and center_x < x_min:
            cap = min(cap, x_min - center_x - root)
    return max(0.0, cap)


def _required_edge_shift(
    bounds: Tuple[float, float, float, float],
    center_x: float,
    center_y: float,
    radius: float,
    threshold: float,
    alignment: str,
) -> float:
    """The slide that clears one near-side (line, hole) collision exactly.

    Sliding an edge-aligned line away from its anchored margin is a rigid
    horizontal translate of its AABB, so ``circle_aabb_gap`` is monotonic in
    the slide and the shift reaching the threshold solves in closed form:
    ``dx(slide) = root`` with ``root = sqrt((threshold + r)^2 - dy^2)``. A
    detected collision guarantees ``|dy| <= threshold + r``, so the root is
    real.

    Args:
        bounds: The line's collision-frame AABB.
        center_x: Hole centre X.
        center_y: Hole centre Y.
        radius: Hole radius.
        threshold: The line's stroke-aware collision threshold, inches.
        alignment: ``"left"`` (slide rightward) or ``"right"`` (leftward).

    Returns:
        The minimum non-negative slide in inches.
    """
    x_min, y_min, x_max, y_max = bounds
    dy = max(y_min - center_y, 0.0, center_y - y_max)
    root = math.sqrt(max(0.0, (threshold + radius) ** 2 - dy * dy))
    if alignment == "left":
        return max(0.0, center_x + root - x_min)
    return max(0.0, x_max - center_x + root)


def _check_shift_fits(
    label: ResolvedLabel,
    line_index: int,
    bounds: Tuple[float, float, float, float],
    alignment: str,
    shift: float,
    left_edge: float,
    right_edge: float,
) -> None:
    """Raise when a shifted line cannot compress into the room left by its slide.

    The content box is a compression constraint, not an alignment-stopping
    one: the renderer's margin-fit compression shortens a slid line so its far
    edge lands on the far margin (Pass A reserves the slide's room in the
    compression budget), which keeps the anchored edge flush. When the line's
    own ``max_h_compress`` budget cannot absorb the overflow, the flush the
    sharing/flush guarantee demands is impossible -- an unresolvable collision
    condition, and it fails the job like every other one.

    Args:
        label: The label owning the line (for the message and the budget).
        line_index: The slid line's content index.
        bounds: The line's collision-frame AABB.
        alignment: ``"left"`` / ``"right"`` (the anchored side).
        shift: The slide to check, inches.
        left_edge: Content box left edge, inches.
        right_edge: Content box right edge, inches.

    Raises:
        LabelRenderError: The slide's box overflow exceeds the line's budget.
    """
    x_min, _y_min, x_max, _y_max = bounds
    width = x_max - x_min
    if width <= 0.0:
        return
    overflow = (
        (x_max + shift - right_edge) if alignment == "left" else (left_edge - (x_min - shift))
    )
    if overflow <= 1e-9:
        return
    line = label.content[line_index]
    budget = max(0.0, min(1.0, line.max_h_compress))
    required_scale = (width - overflow) / width
    if required_scale < 1.0 - budget - 1e-9:
        raise LabelRenderError(
            f"Label {label.id}: text line {line_index} ({line.plain_text!r}) "
            f"cannot keep its alignment-locked edge margin: sliding it "
            f"{shift:.4f}in pushes its far end {overflow:.4f}in past the "
            f"content area, which needs a horizontal scale of "
            f"{required_scale:.3f} (max_h_compress budget floor "
            f"{1.0 - budget:.3f}). Increase max_h_compress, widen the label, "
            f"or shorten the text."
        )


def resolve_via_edge_margin(
    label: ResolvedLabel,
    line_entries: Sequence[_LineEntry],
    band_center_offset_inches: float = 0.0,
    line_desc: str = "",
) -> Optional[ResolvedLabel]:
    """Phase 3: slide edge-aligned lines off the holes on their anchored side.

    Horizontal compression cannot free a ``left``/``right``-aligned line from
    a hole on its anchored side: the anchored edge stays pinned at the
    margin, so compressing only shortens the far end. This phase increases
    that line's edge margin (slides it away from the near-side hole) by the
    closed-form shift that clears the collision, capped by the shift at which
    a far-side hole starts colliding (beyond that cap the line collides with
    holes on BOTH sides, which is where the Phase-4 alignment switch to center
    takes over). The content-area box is NOT a cap: a slide that overflows the
    far margin is absorbed by the renderer's margin-fit compression (Pass A
    reserves the slide's room), so the box is a compression constraint, never
    an alignment-stopping one.

    When ``label.align_margin_adjust_global`` is enabled, every other line
    sharing the colliding line's exact ``text_h_alignment`` matches the
    increased edge margin (the group's anchored edges stay flush); the shared
    shift is the tightest (required x far-hole cap) among the colliding lines,
    and every receiver takes that FULL shift (each capped only by its own
    far-hole limit). A receiver whose ``max_h_compress`` budget cannot absorb
    the resulting box overflow raises :class:`LabelRenderError` (flushness is
    a guarantee). When disabled (the default), only the colliding line moves.

    The slide is analytical (a rigid AABB translate), so this phase costs no
    sweep renders; the caller re-renders to verify.

    Args:
        label: The label whose collision should be resolved (may already
            carry a Phase 2 margin reduction).
        line_entries: Rendered text bounds (the current frame, reflecting
            any edge margins already applied).
        band_center_offset_inches: The band-center offset of ``line_entries``.
        line_desc: Optional description of the offending lines for the
            resolution WARNING.

    Returns:
        A clone of ``label`` with ``collision_edge_margin_by_line`` set when
        at least one line can slide, otherwise ``None`` (no edge-aligned
        near-side collision, or no slide budget). The clone may only
        PARTIALLY resolve the collisions (the far-hole cap stops the slide);
        the caller re-detects and escalates.

    Raises:
        LabelRenderError: ``align_margin_adjust_global`` sharing slid a
            receiver past the content area by more than its ``max_h_compress``
            budget can compress away.
    """
    if not label.holes:
        return None
    circles = hole_circles_local(label)
    shift_y = _frame_shift_y(label, band_center_offset_inches)
    bounds_by_line = {
        entry.line_index: (
            entry.bounds[0],
            entry.bounds[1] + shift_y,
            entry.bounds[2],
            entry.bounds[3] + shift_y,
        )
        for entry in line_entries
    }
    left_edge, right_edge = _content_edges(label)

    # Per colliding edge-aligned line: the shift clearing its near-side holes.
    required: Dict[int, float] = {}
    for collision in detect_text_hole_collisions(label, line_entries, band_center_offset_inches):
        line_index = collision.line_index
        if not 0 <= line_index < len(label.content):  # pragma: no cover - defensive
            continue
        alignment = label.content[line_index].text_h_alignment
        if alignment not in ("left", "right"):
            continue
        bounds = bounds_by_line.get(line_index)
        if bounds is None:  # pragma: no cover - a collision always has an entry
            continue
        center_x, center_y, radius = circles[collision.hole_index]
        x_min, _y_min, x_max, _y_max = bounds
        near = (
            (center_x < (x_min + x_max) / 2.0)
            if alignment == "left"
            else (center_x > (x_min + x_max) / 2.0)
        )
        if not near:
            continue
        need = _required_edge_shift(
            bounds, center_x, center_y, radius, collision_threshold(label, line_index), alignment
        )
        required[line_index] = max(required.get(line_index, 0.0), need)
    if not required:
        return None

    def _cap(line_index: int, alignment: str) -> float:
        bounds = bounds_by_line.get(line_index)
        if bounds is None:  # pragma: no cover - a rendered line always has bounds
            return 0.0
        return _max_edge_shift(
            bounds,
            alignment,
            circles,
            collision_threshold(label, line_index),
        )

    # Each colliding line's own slide: what it needs, capped by its own slack.
    own_shift: Dict[int, float] = {
        line_index: min(need, _cap(line_index, label.content[line_index].text_h_alignment))
        for line_index, need in required.items()
    }
    own_shift = {index: shift for index, shift in own_shift.items() if shift > 0.0}
    if not own_shift:
        return None

    per_line_margin: Dict[int, float] = {}
    shift_by_alignment: Dict[str, float] = {}
    if label.align_margin_adjust_global:
        # Sharing: one shift per alignment (the tightest among the lines that
        # must move, so every receiver keeps its own far-hole cap honoured),
        # applied to every line of that alignment -- the anchored edges stay
        # flush. A receiver that no longer fits after the full shift is
        # compressed by the renderer (Pass A); a receiver whose budget cannot
        # absorb the overflow fails the job.
        for line_index, shift in own_shift.items():
            alignment = label.content[line_index].text_h_alignment
            shift_by_alignment[alignment] = min(shift_by_alignment.get(alignment, shift), shift)
        for line_index, line in enumerate(label.content):
            shared = shift_by_alignment.get(line.text_h_alignment)
            if shared is None:
                continue
            shift = min(shared, _cap(line_index, line.text_h_alignment))
            if shift > 0.0:
                bounds = bounds_by_line.get(line_index)
                # The fit check guards the FLUSH guarantee, which only binds a
                # line moved purely by sharing. A colliding line's slide clears
                # a hole: when it overflows the box beyond its budget the line
                # spills (collision-free, exactly the flag-off behaviour) and
                # the far-side holes -- not the box -- decide escalation.
                if bounds is not None and line_index not in required:
                    _check_shift_fits(
                        label,
                        line_index,
                        bounds,
                        line.text_h_alignment,
                        shift,
                        left_edge,
                        right_edge,
                    )
                per_line_margin[line_index] = shift
    else:
        # Surgical: each colliding line slides by exactly its own amount.
        per_line_margin = dict(own_shift)
        for line_index, shift in own_shift.items():
            alignment = label.content[line_index].text_h_alignment
            shift_by_alignment[alignment] = max(shift_by_alignment.get(alignment, 0.0), shift)
    if not per_line_margin:  # pragma: no cover - own_shift is non-empty here
        return None

    merged = dict(label.collision_edge_margin_by_line)
    for line_index, shift in per_line_margin.items():
        merged[line_index] = merged.get(line_index, 0.0) + shift
    for alignment, shift in sorted(shift_by_alignment.items()):
        moved = [
            index for index in per_line_margin if label.content[index].text_h_alignment == alignment
        ]
        logger.warning(
            "Label %s: increased the %s-aligned text edge margin by %.4fin on %d line(s) "
            "(%s) to avoid text-hole collision on %s.",
            label.id,
            alignment,
            shift,
            len(moved),
            ", ".join(f"line {index}" for index in sorted(moved)),
            line_desc or "colliding text",
        )
    return replace(label, collision_edge_margin_by_line=merged)


def switch_edge_aligned_to_center(
    label: ResolvedLabel,
    collisions: Sequence[CollisionResult],
) -> ResolvedLabel:
    """Switch every colliding edge-aligned line's alignment to ``center``.

    A ``left``/``right``-aligned line whose edge-margin slide is exhausted
    collides with holes on both sides (or has no slack): centering it is the
    only remaining way to make the horizontal compression act symmetrically
    on both ends. Shared by :func:`resolve_via_compression` (the Phase-4
    sweep) and the renderer's unresolvable-diagnostics path so both measure
    the same label state.

    Args:
        label: The label to clone.
        collisions: The collisions whose lines should be switched.

    Returns:
        A clone with ``collision_center_by_line`` extended (the same label
        when no line qualifies).
    """
    switched = frozenset(
        line_index
        for line_index in {collision.line_index for collision in collisions}
        if 0 <= line_index < len(label.content)
        and label.content[line_index].text_h_alignment in ("left", "right")
        and line_index not in label.collision_center_by_line
    )
    if not switched:
        return label
    return replace(
        label, collision_center_by_line=frozenset(label.collision_center_by_line) | switched
    )


def flush_siblings_to_switched_edge(
    label: ResolvedLabel,
    line_entries: Sequence[_LineEntry],
    *,
    render: LineBoundsRender,
    band_center_offset_inches: float = 0.0,
    line_desc: str = "",
) -> Optional[ResolvedLabel]:
    """Phase 4 tail: flush unswitched siblings to the switched lines' final edge.

    When ``align_margin_adjust_global`` is enabled and the Phase-4 switch moved
    edge-aligned lines to ``center`` (centering + compression repositioned
    them), the same-alignment lines that were NOT switched still carry the
    Phase-3 shared margin, so their anchored edges no longer line up with the
    switched lines. This slides each unswitched edge-aligned line so its
    anchored edge is flush with the LEFTMOST switched line's final left edge
    (mirrored: the rightmost final right edge for ``right``-aligned groups) --
    the switched lines are left to their auto-adjustment.

    The flush sets each sibling's edge margin to the ABSOLUTE target (it may
    move a sibling inward for a new collision). Sliding the anchored edge
    inward shrinks the room left for the line's far end: a flushed sibling
    whose far end overflows the content box is compressed by the renderer's
    margin-fit pass (Pass A reserves the slide's room), and a sibling whose
    ``max_h_compress`` budget cannot absorb the overflow raises
    :class:`LabelRenderError` (flushness is a guarantee). A flush that lands a
    sibling on a far-side HOLE sweeps ``collision_compress_by_line``
    (multiplicative on any scale it carries, floored at its budget) to clear
    it; a flush that stays colliding within the budget is dropped (``None``)
    and the caller keeps the unflushed resolved layout.

    Args:
        label: The resolved label (post Phase-4 switch/compression, or any
            label carrying a previous pass's switch state).
        line_entries: Rendered text bounds of ``label`` (the collision frame;
            its X axis is identical to the alignment frame).
        render: Callback re-rendering a label's text and reporting the
            per-line bounds records (injected
            ``label_renderer._render_text_local_with_bounds``; the flush
            sweep's seam, same pattern as :func:`resolve_via_compression`).
        band_center_offset_inches: The band-center offset of ``line_entries``.
        line_desc: Optional description of the offending lines for the
            resolution WARNING.

    Returns:
        A clone with ``collision_edge_margin_by_line`` (and, when a flushed
        sibling no longer fits, ``collision_compress_by_line``) updated to a
        collision-free flush, otherwise ``None`` (flag off, nothing switched,
        no qualifying sibling, already flush, or the flush cannot clear).

    Raises:
        LabelRenderError: A flushed sibling's box overflow exceeds its
            ``max_h_compress`` budget (the flush guarantee is impossible).
    """
    if not label.align_margin_adjust_global or not label.collision_center_by_line:
        return None
    shift_y = _frame_shift_y(label, band_center_offset_inches)
    bounds_by_line = {
        entry.line_index: (
            entry.bounds[0],
            entry.bounds[1] + shift_y,
            entry.bounds[2],
            entry.bounds[3] + shift_y,
        )
        for entry in line_entries
    }
    left_edge, right_edge = _content_edges(label)

    # Flush target per alignment: the tightest (leftmost/rightmost) final
    # anchored edge among the switched lines of that declared alignment.
    target_by_alignment: Dict[str, float] = {}
    for line_index in sorted(label.collision_center_by_line):
        if not 0 <= line_index < len(label.content):  # pragma: no cover - defensive
            continue
        alignment = label.content[line_index].text_h_alignment
        if alignment not in ("left", "right"):  # pragma: no cover - switch is edge-only
            continue
        bounds = bounds_by_line.get(line_index)
        if bounds is None:  # pragma: no cover - a rendered line always has bounds
            continue
        if alignment == "left":
            target = max(0.0, bounds[0] - left_edge)
        else:
            target = max(0.0, right_edge - bounds[2])
        if alignment in target_by_alignment:
            target_by_alignment[alignment] = min(target_by_alignment[alignment], target)
        else:
            target_by_alignment[alignment] = target
    if not target_by_alignment:  # pragma: no cover - a switch always yields a target
        return None

    flush: Dict[int, float] = {}
    for line_index, line in enumerate(label.content):
        flush_target = target_by_alignment.get(line.text_h_alignment)
        if flush_target is None or line_index in label.collision_center_by_line:
            continue
        current = label.collision_edge_margin_by_line.get(line_index, 0.0)
        delta = flush_target - current
        if abs(delta) > 1e-9:
            bounds = bounds_by_line.get(line_index)
            if bounds is not None and delta > 0.0:
                # A flush that slides the anchored edge inward shrinks the
                # room left for the far end; Pass A's margin-fit compression
                # absorbs the overflow, and a budget that cannot fails the job.
                _check_shift_fits(
                    label,
                    line_index,
                    bounds,
                    line.text_h_alignment,
                    delta,
                    left_edge,
                    right_edge,
                )
            flush[line_index] = flush_target
    if not flush:
        return None

    merged = dict(label.collision_edge_margin_by_line)
    merged.update(flush)
    candidate: ResolvedLabel = replace(label, collision_edge_margin_by_line=merged)

    # Sweep: the flush shrinks the room left for each moved line's far end, so
    # compress the flushed lines (multiplicative on their existing scale,
    # floored at their own budget) until the flush is collision-free.
    steps = max(1, _COMPRESSION_RESOLVE_STEPS)
    scale_text = ""
    for i in range(0, steps + 1):
        if i > 0:
            frac = i / steps
            per_line = dict(candidate.collision_compress_by_line)
            for line_index in flush:
                line = label.content[line_index]
                budget = max(0.0, min(1.0, line.max_h_compress))
                base_scale = label.collision_compress_by_line.get(line_index, 1.0)
                scale = max(base_scale * (1.0 - frac * budget), 1.0 - budget)
                per_line[line_index] = min(per_line.get(line_index, 1.0), scale)
            candidate = replace(candidate, collision_compress_by_line=per_line)
        _lc, entries = render(candidate)
        if not detect_text_hole_collisions(candidate, entries, band_center_offset_inches):
            if i > 0:
                scale_text = "; compressed " + ", ".join(
                    f"line {index} to {candidate.collision_compress_by_line[index] * 100.0:.1f}% width"
                    for index in sorted(flush)
                    if index in candidate.collision_compress_by_line
                )
            break
    else:  # pragma: no cover - defensive: box overflow raises in _check_shift_fits
        # Reachable only when a flush that FITS the box lands a sibling on a
        # far-side hole the budget cannot compress clear; the sharing/flush
        # fit check already fails the job on the (common) box-overflow case.
        logger.debug(
            "Label %s: flushing the %s-aligned sibling(s) to the center-switched "
            "line(s) stays colliding within the max_h_compress budget; keeping "
            "the unflushed layout.",
            label.id,
            "/".join(sorted(target_by_alignment)),
        )
        return None

    for alignment, target in sorted(target_by_alignment.items()):
        moved = [
            index
            for index, shift in flush.items()
            if label.content[index].text_h_alignment == alignment
        ]
        if not moved:
            continue
        logger.warning(
            "Label %s: flushed %d %s-aligned text line(s) (%s) to the %s edge of the "
            "center-switched line(s) at %.4fin margin to avoid text-hole collision%s%s.",
            label.id,
            len(moved),
            alignment,
            ", ".join(f"line {index}" for index in sorted(moved)),
            "left" if alignment == "left" else "right",
            target,
            scale_text,
            f" on {line_desc}" if line_desc else "",
        )
    return candidate


def resolve_via_compression(
    label: ResolvedLabel,
    budget: float,
    *,
    render: LineBoundsRender,
    line_desc: str = "",
    band_center_offset_inches: float = 0.0,
) -> Optional[ResolvedLabel]:
    """Phase 4: switch edge-aligned collisions to center, then compress.

    Calculates the minimum compression needed for each line that is
    colliding with a hole, and applies per-line compression. Non-colliding
    lines remain at full width. A line that is still edge-aligned
    (``left``/``right``) when this phase runs has already exhausted its
    edge-margin budget in Phase 3, so its alignment is switched to
    ``center`` first (``collision_center_by_line``): compression then acts
    symmetrically and pulls BOTH ends away from the side holes, which an
    edge-anchored line cannot do (its anchored end stays pinned).

    Returns a label with ``collision_compress_by_line`` (and, when lines
    were switched, ``collision_center_by_line``) set when one clears all
    collisions, otherwise ``None``.

    Args:
        label: The label whose collision should be resolved (may already
            carry a Phase 2 margin reduction).
        budget: Maximum compression fraction in ``(0.0, 1.0]`` (the
            label-level ``max_h_compress`` budget, i.e. the minimum across
            all content lines).
        render: Callback re-rendering a label's text and reporting the
            per-line bounds records (injected
            ``label_renderer._render_text_local_with_bounds``; keeps this
            module free of renderer imports).
        line_desc: Optional description of the offending text lines,
            included in the resolution WARNING for informative output.

    Returns:
        A clone of ``label`` with ``collision_compress_by_line`` set to
        per-line scales (plus ``collision_center_by_line`` when edge-aligned
        lines had to be switched) when one clears all collisions, otherwise
        ``None``.
    """
    floor = max(0.0, 1.0 - min(budget, 1.0))

    # First pass: detect collisions and identify which lines are colliding
    _candidate_lc, candidate_entries = render(label)
    sweep_collisions = detect_text_hole_collisions(
        label, candidate_entries, band_center_offset_inches
    )
    if not sweep_collisions:
        return None  # No collisions to resolve

    # Build a set of colliding line indices
    colliding_lines = {collision.line_index for collision in sweep_collisions}

    # Edge-aligned colliding lines have exhausted the Phase-3 edge margin (this
    # phase runs after it), so switch them to center: an anchored edge pins the
    # near end, and only a centered line compresses symmetrically.
    switched_label = switch_edge_aligned_to_center(label, sweep_collisions)
    switched = switched_label.collision_center_by_line - label.collision_center_by_line
    if switched:
        label = switched_label
        _candidate_lc, candidate_entries = render(label)
        sweep_collisions = detect_text_hole_collisions(
            label, candidate_entries, band_center_offset_inches
        )
        if not sweep_collisions:
            # The switch alone cleared every collision (centering a line whose
            # near end was pinned pulls it off the hole); no compression needed.
            logger.warning(
                "Label %s: switched %d text line(s) to center alignment to avoid "
                "text-hole collision on %s (edge margin exhausted).",
                label.id,
                len(switched),
                line_desc or "colliding text",
            )
            return label
        colliding_lines = {collision.line_index for collision in sweep_collisions}

    # Sweep over compression levels and track per-line success
    steps = max(1, _COMPRESSION_RESOLVE_STEPS)
    for i in range(1, steps + 1):
        scale = floor + (1.0 - floor) * (steps - i) / steps

        # Create per-line compression map: only compress colliding lines
        per_line_compress: dict[int, float] = dict.fromkeys(colliding_lines, scale)
        candidate_label = replace(label, collision_compress_by_line=per_line_compress)

        # Re-render and check collisions. Horizontal compression does not move
        # the block vertically, so the band-center offset is unchanged.
        _candidate_lc, candidate_entries = render(candidate_label)
        sweep_collisions = detect_text_hole_collisions(
            candidate_label, candidate_entries, band_center_offset_inches
        )

        if not sweep_collisions:
            switch_text = (
                f" (after switching {len(switched)} line(s) to center alignment)"
                if switched
                else ""
            )
            logger.warning(
                "Label %s: compressed %d text line(s) horizontally to %.1f%% width to "
                "avoid text-hole collision on %s (max_h_compress budget %.2f)%s.",
                label.id,
                len(colliding_lines),
                scale * 100.0,
                line_desc or "colliding text",
                budget,
                switch_text,
            )
            logger.debug(
                "Label %s: Compression sweep iteration %d/%d found successful scale %.3f "
                "for %d line(s): %s",
                label.id,
                i,
                steps,
                scale,
                len(colliding_lines),
                sorted(colliding_lines),
            )
            return candidate_label

    return None


def format_unresolvable_collision(
    label: ResolvedLabel,
    collisions: Sequence[CollisionResult],
    attempted_scale: Optional[float] = None,
) -> str:
    """Build the diagnostic message for an unresolvable text-hole collision.

    Args:
        label: The label state after all resolution phases ran (may carry
            a reduced ``hole_margin``, a vertical block slide, packed
            inter-line gaps, per-line edge margins, and the
            compression state at the most aggressive scale tried).
        collisions: The collisions still present after all phases ran.
        attempted_scale: Most aggressive collision-compression scale tried
            (Phase 4 floor), or ``None`` when compression was not attempted.

    Returns:
        A multi-line diagnostic string naming each colliding pair plus the
        current margin/vertical/compression state and actionable
        recommendations, restricted to the escape axes the label's holes
        actually leave open (a top/bottom-only label is told to grow its
        vertical room, and is never told a compression it did not apply was
        insufficient).
    """
    axes = hole_escape_axes(label)
    budget = min((line.max_h_compress for line in label.content), default=0.0)
    bottom_edge, top_edge = _content_band(label)
    if attempted_scale is not None:
        scale_text = f"{attempted_scale:.3f}"
    elif label.collision_compress_by_line:
        scales = list(label.collision_compress_by_line.values())
        avg_scale = sum(scales) / len(scales) if scales else 1.0
        scale_text = f"{avg_scale:.3f}"
    else:
        scale_text = "1.000"
    details = "; ".join(
        f"line {collision.line_index} ({collision.line_text!r}) vs "
        f"{collision.hole_location} hole (gap {collision.gap:.4f}in below "
        f"required {collision_threshold(label, collision.line_index):.4f}in)"
        for collision in collisions
    )
    worst_shortfall = max(
        (collision_threshold(label, c.line_index) - c.gap for c in collisions),
        default=0.0,
    )
    if not axes.horizontal:
        # The X axis buys no clearance on this label, so its budget is not
        # part of the story -- naming it would send the operator to the wrong
        # knob (and claim a squeeze that never ran).
        advice = (
            "increase label height, reduce text height, lower "
            "min_hole_margin, lower min_line_spacing, or reduce the vertical "
            "margin (these holes sit on the top/bottom edge only, so the "
            "collision is cleared vertically)."
        )
    elif label.min_hole_margin is None and budget <= 0.0:
        advice = (
            "enable collision avoidance (set min_hole_margin and/or "
            "max_h_compress), increase label width, or reduce text height."
        )
    else:
        advice = (
            "increase label width, reduce text height, lower "
            "min_hole_margin, lower min_line_spacing, or increase max_h_compress."
        )
    edge_lines = [
        f"line {line_index} (+{shift:.4f}in)"
        for line_index, shift in sorted(label.collision_edge_margin_by_line.items())
        if shift > 0.0
    ]
    edge_text = "\n- Edge-margin slide: " + ", ".join(edge_lines) if edge_lines else ""
    center_text = (
        "\n- Alignment switched to center: "
        + ", ".join(str(index) for index in sorted(label.collision_center_by_line))
        if label.collision_center_by_line
        else ""
    )
    shift_text = (
        f"\n- Vertical block slide: {label.collision_vertical_shift_inches:+.4f}in "
        f"({'downward' if label.collision_vertical_shift_inches > 0.0 else 'upward'})"
        if abs(label.collision_vertical_shift_inches) > 1e-12
        else ""
    )
    packing_text = (
        f"\n- Inter-line packing: {label.collision_spacing_scale * 100.0:.1f}% of the "
        f"requested gaps (min_line_spacing floor "
        f"{max(0.0, label.min_line_spacing or 0.0):.4f}in)"
        if label.collision_spacing_scale < 1.0 - 1e-12
        else ""
    )
    spread_text = (
        f"\n- Inter-line spread: {label.collision_spacing_scale * 100.0:.1f}% of the "
        f"requested gaps (the spread is capped by the content band: "
        f"{top_edge - bottom_edge:.4f}in of band)"
        if label.collision_spacing_scale > 1.0 + 1e-12
        else ""
    )
    strategy = spacing_strategy_summary(label)
    strategy_text = f"\n- Line-spacing strategy: {strategy}" if strategy else ""
    if axes.horizontal:
        compression_text = (
            f"- Compression: max_h_compress={budget:.2f} applied, still insufficient "
            f"(most aggressive text scale {scale_text})\n"
        )
    else:
        compression_text = (
            "- Compression: not attempted (no left/right/corner hole on this "
            "label, so the horizontal axis buys no clearance)\n"
        )
    return (
        f"Label {label.id}: text-hole collision cannot be resolved.\n"
        f"- Collisions: {details}\n"
        f"- Hole margin: {label.hole_margin:.4f}in "
        f"(min: {label.min_hole_margin if label.min_hole_margin is not None else 'unset'})"
        f"{edge_text}{center_text}{shift_text}{packing_text}{spread_text}"
        f"{strategy_text}\n"
        f"{compression_text}"
        f"- Worst clearance shortfall: {worst_shortfall:.4f}in\n"
        f"- Recommendations: {advice}"
    )


def assert_no_collisions(rendered_labels: Iterable[RenderedLabel]) -> None:
    """Abort a job when any rendered label still has a text-hole collision.

    Only *unresolved* collisions are unacceptable output: a collision that
    avoidance repaired (by shrinking hole margins or compressing text) logs
    a WARNING during :func:`render_label_to_plt` and the job proceeds.
    Each unresolved label already received its per-label ERROR diagnostics
    during :func:`render_label_to_plt`; this gate runs once *after* every
    label in the job has been rendered (so all errors were printed) and
    raises a single job-level error.

    Args:
        rendered_labels: All RenderedLabel objects produced for the job
            (typically the render cache's values).

    Raises:
        LabelRenderError: If at least one rendered label reports
            :attr:`RenderedLabel.has_collisions` (a collision that no
            enabled avoidance phase could resolve).
    """
    offending: List[str] = []
    for rendered in rendered_labels:
        label_id = rendered.source_label.id
        if rendered.has_collisions and label_id not in offending:
            offending.append(label_id)
    if not offending:
        return
    raise LabelRenderError(
        f"Job aborted: {len(offending)} label(s) have text-hole collisions "
        f"and the jobspec must be revised: {', '.join(offending)}. "
        "See the per-label ERROR messages above for the offending text "
        "lines and drill holes."
    )
