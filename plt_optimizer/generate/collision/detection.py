"""Text-hole collision detection, avoidance phases, and job gate.

The collision system in one place: the stroke-aware threshold, detection
of (text line, drill hole) pairs below it, the opt-in avoidance phases
(hole-margin reduction, edge-margin slide for edge-aligned text, the
alignment switch to center plus horizontal compression, and the
sibling flush onto the switched lines' final edge), the
unresolvable-collision diagnostics, and the job-level
:func:`assert_no_collisions` gate.

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
from plt_optimizer.generate.collision.holes import hole_circles_local
from plt_optimizer.generate.collision.shapes import rounded_rect_corner
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


class LabelRenderError(Exception):
    """Raised to abort a job containing unavoidable text-hole collisions.

    Emitted by :func:`assert_no_collisions` once every label in the job has
    been rendered (so all per-label ERROR diagnostics were logged first)
    and at least one label still overlaps a drill hole. Collisions that
    collision avoidance repaired are tolerated (logged as WARNING at render
    time); only collisions that survive every enabled avoidance phase are
    unacceptable output requiring a jobspec revision.
    """


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
    # Mirrors _center_text_layer_vertically: band offset + hole-aware shift both
    # lower the expected center, so both are subtracted here.
    shift_y = (
        label.height / 2.0 - band_center_offset_inches - label.hole_aware_vertical_shift_inches
    )
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
    left_edge: float,
    right_edge: float,
) -> float:
    """How far an edge-aligned line may slide off its anchored margin.

    The cap is the smallest of:
    - the line's slack to the FAR content-area margin (a fitting line is
      never pushed out of the box; an overflowing line has no slack);
    - per far-side hole, the shift at which that hole STARTS colliding
      (the user's rule: slide until a hole collides on the far side).

    Args:
        bounds: The line's collision-frame AABB ``(x_min, y_min, x_max, y_max)``.
        alignment: ``"left"`` or ``"right"`` (the anchored side).
        circles: All hole circles (label-local, collision frame).
        threshold: The line's stroke-aware collision threshold, inches.
        left_edge: Content box left edge, inches.
        right_edge: Content box right edge, inches.

    Returns:
        The maximum non-negative slide in inches.
    """
    x_min, y_min, x_max, y_max = bounds
    cap = (right_edge - x_max) if alignment == "left" else (x_min - left_edge)
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
    closed-form shift that clears the collision, capped by the line's slack
    to the far margin and by the shift at which a far-side hole starts
    colliding (beyond that cap the line collides with holes on BOTH sides,
    which is where the Phase-4 alignment switch to center takes over).

    When ``label.align_margin_adjust_global`` is enabled, every other line
    sharing the colliding line's exact ``text_h_alignment`` matches the
    increased edge margin (the group's anchored edges stay flush); the shared
    shift is capped by the tightest cap among all lines receiving it. When
    disabled (the default), only the colliding line moves.

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
        PARTIALLY resolve the collisions (the cap stops the slide); the
        caller re-detects and escalates.
    """
    if not label.holes:
        return None
    circles = hole_circles_local(label)
    shift_y = (
        label.height / 2.0 - band_center_offset_inches - label.hole_aware_vertical_shift_inches
    )
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
            left_edge,
            right_edge,
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
        # must move, so every receiver keeps its own cap honoured), applied to
        # every line of that alignment -- the anchored edges stay flush.
        for line_index, shift in own_shift.items():
            alignment = label.content[line_index].text_h_alignment
            shift_by_alignment[alignment] = min(shift_by_alignment.get(alignment, shift), shift)
        for line_index, line in enumerate(label.content):
            shared = shift_by_alignment.get(line.text_h_alignment)
            if shared is None:
                continue
            shift = min(shared, _cap(line_index, line.text_h_alignment))
            if shift > 0.0:
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
    inward shrinks the room left for the line's far end, so a flushed sibling
    that no longer fits gets compressed (a ``collision_compress_by_line``
    sweep, multiplicative on any scale it already carries and floored at its
    own ``max_h_compress`` budget) until the flush is collision-free. When the
    budget cannot clear the flush, the whole flush is dropped (``None``) and
    the caller keeps the unflushed resolved layout.

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
    """
    if not label.align_margin_adjust_global or not label.collision_center_by_line:
        return None
    shift_y = (
        label.height / 2.0 - band_center_offset_inches - label.hole_aware_vertical_shift_inches
    )
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
        if abs(flush_target - current) > 1e-9:
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
    else:
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
            a reduced ``hole_margin``, per-line edge margins, and the
            compression state at the most aggressive scale tried).
        collisions: The collisions still present after all phases ran.
        attempted_scale: Most aggressive collision-compression scale tried
            (Phase 4 floor), or ``None`` when compression was not attempted.

    Returns:
        A multi-line diagnostic string naming each colliding pair plus the
        current margin/compression state and actionable recommendations.
    """
    budget = min((line.max_h_compress for line in label.content), default=0.0)
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
    if label.min_hole_margin is None and budget <= 0.0:
        advice = (
            "enable collision avoidance (set min_hole_margin and/or "
            "max_h_compress), increase label width, or reduce text height."
        )
    else:
        advice = (
            "increase label width, reduce text height, lower "
            "min_hole_margin, or increase max_h_compress."
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
    return (
        f"Label {label.id}: text-hole collision cannot be resolved.\n"
        f"- Collisions: {details}\n"
        f"- Hole margin: {label.hole_margin:.4f}in "
        f"(min: {label.min_hole_margin if label.min_hole_margin is not None else 'unset'})"
        f"{edge_text}{center_text}\n"
        f"- Compression: max_h_compress={budget:.2f} applied, still insufficient "
        f"(most aggressive text scale {scale_text})\n"
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
