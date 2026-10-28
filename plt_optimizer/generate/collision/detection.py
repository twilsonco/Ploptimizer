"""Text-hole collision detection, avoidance phases, and job gate.

The collision system in one place: the stroke-aware threshold, detection
of (text line, drill hole) pairs below it, the two opt-in avoidance phases
(hole-margin reduction, horizontal compression), the unresolvable-collision
diagnostics, and the job-level :func:`assert_no_collisions` gate.

The Phase 3 compression sweep must re-render text to measure the
compressed bounds; the renderer lives in
:mod:`plt_optimizer.generate.label_renderer`, so the sweeps take an
injected ``render`` callback (the same seam as the line-content
``ScaleProbe``) instead of importing the renderer - this module has zero
runtime imports from ``label_renderer``.

Moved verbatim from ``label_renderer.py`` when the collision package was
created; the contract is ``AGENTS.md`` beside this file.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable, Iterable, List, Optional, Sequence, Tuple

from plt_optimizer.generate.collision.geometry import CollisionResult, circle_aabb_gap
from plt_optimizer.generate.collision.holes import hole_circles_local

if TYPE_CHECKING:
    from plt_optimizer.generate.label_renderer import RenderedLabel, _LineEntry
    from plt_optimizer.generate.resolution import ResolvedLabel

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

    for line_index, line_text, bounds, _scale, _spacing, _cutters in line_entries:
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


def resolve_via_compression(
    label: ResolvedLabel,
    budget: float,
    *,
    render: LineBoundsRender,
    line_desc: str = "",
    band_center_offset_inches: float = 0.0,
) -> Optional[ResolvedLabel]:
    """Phase 3: compress text horizontally until collisions clear.

    Calculates the minimum compression needed for each line that is
    colliding with a hole, and applies per-line compression. Non-colliding
    lines remain at full width. Returns a label with
    ``collision_compress_by_line`` set to the per-line scales when one
    clears all collisions, otherwise ``None``.

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
        per-line scales when one clears all collisions, otherwise ``None``.
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
            logger.warning(
                "Label %s: compressed %d text line(s) horizontally to %.1f%% width to "
                "avoid text-hole collision on %s (max_h_compress budget %.2f).",
                label.id,
                len(colliding_lines),
                scale * 100.0,
                line_desc or "colliding text",
                budget,
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
            a reduced ``hole_margin``).
        collisions: The collisions still present after all phases ran.
        attempted_scale: Most aggressive collision-compression scale tried
            (Phase 3 floor), or ``None`` when compression was not attempted.

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
    return (
        f"Label {label.id}: text-hole collision cannot be resolved.\n"
        f"- Collisions: {details}\n"
        f"- Hole margin: {label.hole_margin:.4f}in "
        f"(min: {label.min_hole_margin if label.min_hole_margin is not None else 'unset'})\n"
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
