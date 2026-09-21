"""Independent label rendering engine.

Renders individual labels to HPGL/PLT format with measured bounds.
Each label is rendered in isolation at local coordinates (0, 0), exported
with full postprocessing, and then bounds are extracted for packing.

This module eliminates the need for complex coordinate transformations
and postprocessing that was causing edge cases (e.g., label 3 centering bug).
"""

from __future__ import annotations

import logging
import math
import re
import tempfile
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np
import vpype as vp

from plt_optimizer.generate.ftext_renderer import (
    render_text_line_ftext,
    render_text_line_ftext_with_words,
)
from plt_optimizer.generate.geometry import CollisionResult, circle_aabb_gap
from plt_optimizer.generate.resolution import (
    ResolvedLabel,
    compute_horizontal_offset,
    compute_horizontal_scale,
    fit_line_spacing_to_margins,
)

logger = logging.getLogger(__name__)

# Layer assignments (pen 2/3 must match vectorize.py structural pens)
LAYER_TEXT: int = 1
LAYER_BOUNDARY: int = 2
LAYER_HOLES: int = 3

# Resolution sweep granularity for text-hole collision avoidance. The
# margin sweep reuses already-rendered text geometry (holes move, text
# does not), so it is cheap; the compression sweep re-renders text per
# step, so it stays small.
_MARGIN_ADJUST_STEPS: int = 32
_COMPRESSION_RESOLVE_STEPS: int = 16

# A rendered text line's collision-relevant record: ``(line_index,
# line_text, (x_min, y_min, x_max, y_max))`` in label-local coordinates.
_LineEntry = Tuple[int, str, Tuple[float, float, float, float]]


class TextChunkMode(str, Enum):
    """Granularity at which rendered text becomes an optimization node.

    Attributes:
        LINE: One chunk per rendered text line (default). Fewer optimizer
            nodes; the whole line's strokes travel together.
        WORD: One chunk per whitespace-delimited word. Exact stroke
            membership per word and tighter rapid-travel routing.
    """

    LINE = "line"
    WORD = "word"


@dataclass(frozen=True)
class TextChunkRecord:
    """One optimizable chunk of rendered text, in label-local coordinates.

    A chunk is a whole text line (``word_index`` is ``None``) or a single
    whitespace-delimited word within a line. Records carry the chunk's exact
    stroke geometry so the plate-space optimizer can build one routing node
    per chunk without re-parsing emitted HPGL or classifying geometry.

    Attributes:
        line_index: Index of the chunk's text line in ``ResolvedLabel.content``.
        word_index: Index of the word within the line's whitespace split, or
            ``None`` for a whole-line chunk.
        word_text: The word's text (empty string for whole-line chunks).
        pen: HPGL pen number the chunk is emitted on (its cutter's pen).
        contours: Vertex arrays of the chunk's strokes, label-local inches
            with ``+y`` up (pre-export frame; transformed to the device
            frame by the plate-space export pipeline).
        bounds: ``(x_min, y_min, x_max, y_max)`` of :attr:`contours`.
    """

    line_index: int
    word_index: Optional[int]
    word_text: str
    pen: int
    contours: Tuple[np.ndarray, ...]
    bounds: Tuple[float, float, float, float]


class LabelRenderError(Exception):
    """Raised to abort a job containing unavoidable text-hole collisions.

    Emitted by :func:`assert_no_collisions` once every label in the job has
    been rendered (so all per-label ERROR diagnostics were logged first)
    and at least one label still overlaps a drill hole. Collisions are
    unacceptable output; the job specification must be revised to avoid
    them.
    """


@dataclass(frozen=True)
class RenderedLabel:
    """A label rendered to HPGL format with measured bounds.

    Attributes:
        source_label: The original ResolvedLabel that was rendered. When
            text-hole collision avoidance adjusted the label (reduced
            ``hole_margin`` and/or applied ``collision_compress``), this is
            the *adjusted* label so downstream consumers (packing, plate
            vectorization) stay consistent with the emitted PLT.
        plt_content: Raw HPGL text content (without postprocessing).
        x_min: Minimum x-coordinate in inches.
        y_min: Minimum y-coordinate in inches.
        x_max: Maximum x-coordinate in inches.
        y_max: Maximum y-coordinate in inches.
        width: Actual rendered width in inches (x_max - x_min).
        height: Actual rendered height in inches (y_max - y_min).
        has_collisions: True when the final rendered output still contains
            at least one text/hole pair closer than the stroke-aware
            collision threshold (collision avoidance disabled or not fully
            effective). Labels successfully resolved by Phases 2/3 report
            False even though avoidance was triggered.
        collision_detected: True when any text/hole collision was detected
            during rendering, even if collision avoidance (Phases 2/3)
            successfully resolved it. Collisions are unacceptable output:
            the jobspec must be revised, so the job-level gate
            :func:`assert_no_collisions` aborts on this flag rather than
            :attr:`has_collisions`.
        text_chunks: Per-chunk rendered text geometry (line- or word-level,
            per the label's chunk mode) in label-local inches with ``+y`` up.
            Consumed by the plate-space optimizer, which transforms these
            into device coordinates and builds one routing node per chunk
            (skipping the parser/profiler entirely). Empty for labels with no
            rendered text.
    """

    source_label: ResolvedLabel
    plt_content: str
    x_min: float
    y_min: float
    x_max: float
    y_max: float
    width: float
    height: float
    has_collisions: bool = False
    collision_detected: bool = False
    text_chunks: Tuple[TextChunkRecord, ...] = ()


def _collision_threshold(label: ResolvedLabel, line_index: int) -> float:
    """Compute the stroke-aware collision threshold for one text line.

    Collision checks measure the gap between *toolpath geometry* (text
    bounding box vs. hole circle), but the machine removes material half a
    cutter width on each side of every path. Two strokes therefore touch
    once the geometric gap reaches ``0.5 * (hole_cutter + text_cutter)``
    (the stroke floor), and stay visibly separated only beyond that floor
    plus the requested air gap:

    ``threshold = 0.5 * (hole_cutter + text_cutter[line]) +
    hole_text_collision_distance``

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
        text_cutter = label.content[line_index].cutter_diameter
    else:  # Defensive: unknown line (should not happen) assumes no stroke.
        text_cutter = 0.0
    stroke_floor = 0.5 * (label.hole_cutter_diameter + text_cutter)
    return stroke_floor + label.hole_text_collision_distance


def _detect_text_hole_collisions(
    label: ResolvedLabel,
    line_entries: Sequence[_LineEntry],
) -> List[CollisionResult]:
    """Find all (text line, drill hole) pairs closer than the threshold.

    A pair collides when its geometric gap is below the stroke-aware
    threshold of :func:`_collision_threshold` (stroke floor plus the
    cascaded ``hole_text_collision_distance``), so near misses that would
    make the engraved strokes bleed together are reported too. A gap
    exactly equal to the threshold is safe.

    Collision checks run in label-local, y-up coordinates before plate
    placement. The rendered line bounds are anchored around y=0 by
    :func:`_render_text_local_with_bounds`; the export pipeline vertically
    centers the block at ``height / 2`` (``_center_text_layer_vertically``),
    so bounds are shifted by that amount before the check. The export's
    Y-flip mirrors text and holes across the *same* centerline, which
    preserves circle/AABB intersection relationships exactly, so the check
    is valid in this pre-flip frame.

    Args:
        label: The resolved label providing hole positions, cutter
            diameters and the collision distance.
        line_entries: Per-line ``(line_index, line_text, bounds)`` tuples
            as returned by :func:`_render_text_local_with_bounds`.

    Returns:
        One :class:`CollisionResult` per colliding (line, hole) pair.
        Empty when the label has no holes or no rendered lines.
    """
    if not label.holes or not line_entries:
        return []

    circles = _hole_circles_local(label)
    shift_y = label.height / 2.0
    results: List[CollisionResult] = []

    for line_index, line_text, bounds in line_entries:
        threshold = _collision_threshold(label, line_index)
        x_min, y_min, x_max, y_max = bounds
        shifted: Tuple[float, float, float, float] = (
            x_min,
            y_min + shift_y,
            x_max,
            y_max + shift_y,
        )
        for hole_index, (center_x, center_y, radius) in enumerate(circles):
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


def _log_collisions(label: ResolvedLabel, collisions: Sequence[CollisionResult]) -> None:
    """Log each detected text-hole collision at ERROR level.

    Args:
        label: The resolved label being checked (provides the threshold
            components used in the message breakdown).
        collisions: Collision results from :func:`_detect_text_hole_collisions`.
    """
    for collision in collisions:
        required = _collision_threshold(label, collision.line_index)
        clearance = label.hole_text_collision_distance
        stroke_floor = required - clearance
        logger.error(
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


def _collision_line_desc(collisions: Sequence[CollisionResult]) -> str:
    """Describe the distinct colliding text lines for log messages.

    Args:
        collisions: Collision results from :func:`_detect_text_hole_collisions`.

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


def _resolve_collision_via_margin_adjustment(
    label: ResolvedLabel,
    line_entries: Sequence[_LineEntry],
    line_desc: str = "",
) -> Optional[ResolvedLabel]:
    """Phase 2: shrink ``hole_margin`` toward ``min_hole_margin`` (no re-render).

    Drill-hole positions are pure functions of ``hole_margin``
    (:func:`_hole_circles_local`), so candidate margins are evaluated
    analytically against the already-rendered text bounds -- the text
    geometry never changes in this phase. The sweep walks from the current
    margin down to the configured floor and returns the first (smallest)
    reduction that clears every collision.

    Args:
        label: The label whose collision should be resolved.
        line_entries: Rendered text bounds from the initial render.
        line_desc: Optional description of the offending text lines,
            included in the resolution WARNING for informative output.

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
        if not _detect_text_hole_collisions(candidate_label, line_entries):
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


def _resolve_collision_via_compression(
    label: ResolvedLabel,
    budget: float,
    line_desc: str = "",
) -> Optional[ResolvedLabel]:
    """Phase 3: compress text horizontally until collisions clear.

    Sweeps the label-level ``collision_compress`` scale from ``1.0`` down
    to ``(1 - budget)`` (the configured ``max_h_compress`` floor),
    re-rendering the text at each step and re-checking collisions. Returns
    the first (least destructive) scale that clears every collision.

    Args:
        label: The label whose collision should be resolved (may already
            carry a Phase 2 margin reduction).
        budget: Maximum compression fraction in ``(0.0, 1.0]`` (the
            label-level ``max_h_compress`` budget, i.e. the minimum across
            all content lines).
        line_desc: Optional description of the offending text lines,
            included in the resolution WARNING for informative output.

    Returns:
        A clone of ``label`` with ``collision_compress`` set below ``1.0``
        when one clears all collisions, otherwise ``None``.
    """
    floor = max(0.0, 1.0 - min(budget, 1.0))
    start = max(label.collision_compress, floor)
    if start <= floor:
        return None

    steps = max(1, _COMPRESSION_RESOLVE_STEPS)
    for i in range(1, steps + 1):
        scale = start - (start - floor) * i / steps
        candidate_label = replace(label, collision_compress=scale)
        _candidate_lc, candidate_entries = _render_text_local_with_bounds(candidate_label)
        if not _detect_text_hole_collisions(candidate_label, candidate_entries):
            logger.warning(
                "Label %s: compressed text horizontally to %.1f%% width to "
                "avoid text-hole collision on %s (max_h_compress budget %.2f).",
                label.id,
                scale * 100.0,
                line_desc or "colliding text",
                budget,
            )
            return candidate_label
    return None


def _format_unresolvable_collision(
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
    scale_text = (
        f"{attempted_scale:.3f}"
        if attempted_scale is not None
        else f"{label.collision_compress:.3f}"
    )
    details = "; ".join(
        f"line {collision.line_index} ({collision.line_text!r}) vs "
        f"{collision.hole_location} hole (gap {collision.gap:.4f}in below "
        f"required {_collision_threshold(label, collision.line_index):.4f}in)"
        for collision in collisions
    )
    worst_shortfall = max(
        (_collision_threshold(label, c.line_index) - c.gap for c in collisions),
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
    """Abort a job when any rendered label had a text-hole collision.

    Text-hole collisions are unacceptable output: the job specification
    must be revised to avoid them, even when the avoidance system managed
    to repair a collision by shrinking hole margins or compressing text.
    Each offending label already received its per-label ERROR diagnostics
    during :func:`render_label_to_plt`; this gate runs once *after* every
    label in the job has been rendered (so all errors were printed) and
    raises a single job-level error.

    Args:
        rendered_labels: All RenderedLabel objects produced for the job
            (typically the render cache's values).

    Raises:
        LabelRenderError: If at least one rendered label reports
            :attr:`RenderedLabel.collision_detected`.
    """
    offending: List[str] = []
    for rendered in rendered_labels:
        label_id = rendered.source_label.id
        if rendered.collision_detected and label_id not in offending:
            offending.append(label_id)
    if not offending:
        return
    raise LabelRenderError(
        f"Job aborted: {len(offending)} label(s) have text-hole collisions "
        f"and the jobspec must be revised: {', '.join(offending)}. "
        "See the per-label ERROR messages above for the offending text "
        "lines and drill holes."
    )


def render_label_to_plt(
    label: ResolvedLabel,
    pen_map: Optional[dict[float, int]] = None,
) -> RenderedLabel:
    """Render a label independently to HPGL format and extract bounds.

    Renders the label at local coordinates (origin at bottom-left, no translation).
    Exports to temporary file with postprocessing to ensure coordinates are
    correct and compressed to 1:1000 scale (1 inch = 1000 units).

    Text-hole collision avoidance runs in three phases. A (line, hole)
    pair collides when its geometric gap falls below the stroke-aware
    threshold ``0.5 * (hole_cutter + text_cutter) +
    hole_text_collision_distance`` (see :func:`_collision_threshold`), so
    near misses that would make the engraved strokes bleed together are
    caught too:

    1. **Detection** (always): rendered text bounds are checked against
       every drill hole; each collision is logged at ERROR level and
       recorded on the returned :attr:`RenderedLabel.collision_detected`
       (and :attr:`RenderedLabel.has_collisions` when unresolved).
    2. **Hole-margin reduction** (opt-in via ``min_hole_margin``): holes
       are moved toward the label edge (margin reduced toward the floor)
       until the text clears them.
    3. **Horizontal compression** (opt-in via ``max_h_compress``): text is
       uniformly compressed horizontally (stacked on top of any Phase 2
       margin reduction) until the collisions clear.

    A collision that no enabled phase can clear is additionally logged at
    ERROR with full diagnostics (label id, offending text lines, holes,
    and clearance shortfalls) and flagged via ``has_collisions=True``. Collisions
    are unacceptable output regardless of whether avoidance repaired them:
    the job-level gate :func:`assert_no_collisions` aborts the run with
    :class:`LabelRenderError` once every label has been rendered, so all
    offending labels report before the job stops.

    Args:
        label: The ResolvedLabel to render (text, borders, holes).
        pen_map: Optional mapping of text cutter diameter to HPGL pen
            number (see :func:`plt_optimizer.generate.resolution.build_cutter_pen_map`).
            Each text line is emitted on the pen of its cutter so
            per-cutter PLT files can be split out after assembly. ``None``
            (the default) renders all text on the historical text pen
            (``SP1``), preserving back-compatible single-pen output.
            Boundary lines always use ``SP2`` and drill holes ``SP3``.

    Returns:
        RenderedLabel with rendered PLT content and measured bounds. When
        collision avoidance adjusted the label, ``source_label`` is the
        adjusted clone (so packing/vectorization stay consistent with the
        emitted PLT). ``collision_detected`` is True whenever any overlap
        was found (even if resolved); ``has_collisions`` is True only when
        the final render still overlaps a drill hole (avoidance disabled or
        not fully effective).

    Raises:
        ValueError: If bounds cannot be extracted from rendered PLT.
    """
    rendered, line_entries = _render_label_once(label, pen_map=pen_map)
    collisions = _detect_text_hole_collisions(label, line_entries)
    if not collisions:
        return rendered

    # ---- Phase 1: collision detected -- log and attempt resolution ----
    _log_collisions(label, collisions)

    base_label: ResolvedLabel = label
    line_desc = _collision_line_desc(collisions)

    # ---- Phase 2: reduce hole_margin toward min_hole_margin ----
    if label.min_hole_margin is not None:
        margin_label = _resolve_collision_via_margin_adjustment(
            label, line_entries, line_desc=line_desc
        )
        if margin_label is not None:
            resolved_rendered, _ = _render_label_once(margin_label, pen_map=pen_map)
            return replace(resolved_rendered, collision_detected=True)
        # Margin alone cannot clear the collision; continue from the floor
        # margin so Phase 3 compression stacks on top of the maximum
        # allowed margin reduction.
        floor = max(0.0, label.min_hole_margin)
        if floor < label.hole_margin:
            base_label = replace(label, hole_margin=floor)
            collisions = _detect_text_hole_collisions(base_label, line_entries)

    # ---- Phase 3: compress text horizontally within max_h_compress ----
    budget = min((line.max_h_compress for line in label.content), default=0.0)
    attempted_scale: Optional[float] = None
    if budget > 0.0:
        compressed_label = _resolve_collision_via_compression(
            base_label, budget, line_desc=line_desc
        )
        if compressed_label is not None:
            resolved_rendered, _ = _render_label_once(compressed_label, pen_map=pen_map)
            return replace(resolved_rendered, collision_detected=True)
        # Report the state at the most aggressive scale tried, so the
        # measured gaps match what the sweep actually evaluated.
        attempted_scale = max(0.0, 1.0 - min(budget, 1.0))
        final_label = replace(base_label, collision_compress=attempted_scale)
        _, final_entries = _render_text_local_with_bounds(final_label)
        collisions = _detect_text_hole_collisions(final_label, final_entries)
        base_label = final_label

    # ---- Unresolvable: collisions are unacceptable output ----
    # Log the full diagnostics at ERROR level and flag the render. The
    # job-level gate (assert_no_collisions) aborts the run once every
    # label has been rendered, so all offending labels report first.
    logger.error("%s", _format_unresolvable_collision(base_label, collisions, attempted_scale))
    return replace(rendered, has_collisions=True, collision_detected=True)


def _chunk_mode_of(label: ResolvedLabel) -> TextChunkMode:
    """Resolve the label's text chunk mode, tolerating unknown values.

    Args:
        label: The resolved label carrying ``text_chunk_mode`` (a
            :class:`~plt_optimizer.generate.schema.TextChunkMode` value or
            its string form).

    Returns:
        The matching :class:`TextChunkMode`; unknown values fall back to
        ``LINE`` (the backward-compatible default).
    """
    raw = getattr(label, "text_chunk_mode", None) or TextChunkMode.LINE.value
    try:
        return TextChunkMode(str(raw))
    except ValueError:  # pragma: no cover - schema validates the enum
        return TextChunkMode.LINE


def _render_label_once(
    label: ResolvedLabel,
    pen_map: Optional[dict[float, int]] = None,
) -> Tuple[RenderedLabel, List[_LineEntry]]:
    """Render a single label to PLT without collision resolution.

    Args:
        label: The ResolvedLabel to render (text, borders, holes).
        pen_map: Optional cutter-diameter-to-pen mapping for per-cutter
            text layers (see :func:`render_label_to_plt`). ``None`` puts
            all text on the historical text pen (``SP1``).

    Returns:
        Tuple of the :class:`RenderedLabel` (with ``has_collisions`` left
        at its default) and the per-line rendered bounds used by
        :func:`_detect_text_hole_collisions`.
    """
    # Create vpype Document
    doc = vp.Document()

    # Render text layers, one vpype layer per cutter pen (SP1 only when no
    # pen_map is supplied, preserving the historical single-pen output).
    text_pens, line_entries, chunk_records = _render_text_lines_by_pen(
        label, pen_map, chunk_mode=_chunk_mode_of(label)
    )
    for pen_number, text_lc in sorted(text_pens.items()):
        if not text_lc.is_empty():
            doc.add(text_lc, pen_number)

    # Render boundary layer
    boundary_lc = _render_boundary_local(label)
    if not boundary_lc.is_empty():
        doc.add(boundary_lc, LAYER_BOUNDARY)

    # NOTE: Drill holes are intentionally NOT added to the vpype document.
    # A LineCollection can only represent polylines, which would force the
    # circles to be emitted as polygons. Holes are instead emitted directly
    # as native HPGL arc (``AA``) commands by ``_render_holes_hpgl``.

    # Export to temporary file with postprocessing
    with tempfile.NamedTemporaryFile(mode="w", suffix=".plt", delete=False) as f:
        temp_path = Path(f.name)

    try:
        _export_to_plt_with_postprocessing(doc, temp_path, label, text_pens=set(text_pens))
        plt_content = temp_path.read_text().strip()

        # Ensure proper formatting (ends with %)
        if not plt_content.endswith("%"):
            if plt_content.endswith("IN;"):
                plt_content += "%"
            else:
                plt_content = plt_content.rstrip(";") + ";%"

        # Extract bounds from rendered PLT
        x_min, y_min, x_max, y_max = extract_bounds_from_plt(plt_content)

        rendered = RenderedLabel(
            source_label=label,
            plt_content=plt_content,
            x_min=x_min,
            y_min=y_min,
            x_max=x_max,
            y_max=y_max,
            width=x_max - x_min,
            height=y_max - y_min,
            text_chunks=tuple(chunk_records),
        )
        return rendered, line_entries
    finally:
        temp_path.unlink(missing_ok=True)


def _collect_hpgl_geometry(
    content: str,
) -> Tuple[List[Tuple[int, int]], List[Tuple[int, int, int]]]:
    """Extract all point coordinates and arc definitions from HPGL content.

    Parses ``PA``/``PU``/``PD`` coordinate pairs and ``AA`` (arc absolute)
    commands. Arc parameters are returned as ``(center_x, center_y, radius)``
    tuples in plotter units, where the radius is the distance from the arc
    center to the current pen position at the time of the command.

    Args:
        content: Raw HPGL text content.

    Returns:
        Tuple of ``(points, arcs)`` where ``points`` is a list of ``(x, y)``
        pairs and ``arcs`` is a list of ``(cx, cy, radius)`` triples.
    """
    points: List[Tuple[int, int]] = []
    arcs: List[Tuple[int, int, int]] = []

    current_x = 0
    current_y = 0

    # A command token starts with two letters; capture the mnemonic and the
    # parameter string separately so AA parameters (which include a trailing
    # angle) are not mistaken for coordinate pairs.
    for match in re.finditer(r"(PA|PU|PD|AA)([\d,\.\-]+)", content):
        cmd = match.group(1)
        parts = [p for p in match.group(2).split(",") if p != ""]

        try:
            values = [int(float(p)) for p in parts]
        except ValueError:  # pragma: no cover - malformed numeric token
            continue

        if cmd == "AA":
            # AA takes (center_x, center_y, angle); radius is implicit from
            # the current pen position.
            if len(values) >= 3:
                cx, cy = values[0], values[1]
                radius = int(round(math.hypot(current_x - cx, current_y - cy)))
                arcs.append((cx, cy, radius))
                # An arc ends on the circle; without an exact end coordinate
                # we conservatively keep the pen position (the following PU
                # re-establishes it in generated content).
            continue

        for i in range(0, len(values) - 1, 2):
            current_x, current_y = values[i], values[i + 1]
            points.append((current_x, current_y))

    return points, arcs


def _transform_hpgl_coordinates(
    content: str,
    *,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    translate_x: int = 0,
    translate_y: int = 0,
    flip_y_span: Optional[int] = None,
    flip_y_axis: bool = False,
) -> str:
    """Apply an affine transform to every coordinate in HPGL content.

    Handles ``PA``/``PU``/``PD`` coordinate pairs and ``AA`` arc commands.
    For arcs, the center is transformed like any point and the sweep angle
    sign is inverted when the Y axis is mirrored (``flip_y_axis``), which
    preserves the circle's orientation under the reflection.

    Args:
        content: Raw HPGL text content.
        scale_x: Multiplicative scale applied to X coordinates.
        scale_y: Multiplicative scale applied to Y coordinates.
        translate_x: Additive offset applied to X coordinates (plotter units).
        translate_y: Additive offset applied to Y coordinates (plotter units).
        flip_y_span: When set, Y coordinates are mirrored across the
            centerline implied by this total span (``y' = span - y``) before
            any translation. Used by the device-convention Y inversion.
        flip_y_axis: When True, arc sweep angles are negated. Should be set
            together with ``flip_y_span``.

    Returns:
        Transformed HPGL content.
    """

    def _map_y(y: float) -> float:
        if flip_y_span is not None:
            y = flip_y_span - y
        return y * scale_y + translate_y

    def _map_x(x: float) -> float:
        return x * scale_x + translate_x

    def _transform(match: re.Match[str]) -> str:
        cmd = match.group(1)
        parts = [p for p in match.group(2).split(",") if p != ""]
        try:
            values = [float(p) for p in parts]
        except ValueError:  # pragma: no cover - malformed numeric token
            return match.group(0)

        if cmd == "AA":
            if len(values) < 3:  # pragma: no cover - malformed arc
                return match.group(0)
            cx = _map_x(values[0])
            cy = _map_y(values[1])
            angle = -values[2] if flip_y_axis else values[2]
            return f"AA{int(round(cx))},{int(round(cy))},{int(round(angle))}"

        out: List[str] = []
        for i, value in enumerate(values):
            mapped = _map_x(value) if i % 2 == 0 else _map_y(value)
            out.append(str(int(round(mapped))))
        return f"{cmd}{','.join(out)}"

    return re.sub(r"(PA|PU|PD|AA)([\d,\.\-]+)", _transform, content)


def extract_bounds_from_plt(plt_content: str) -> Tuple[float, float, float, float]:
    """Extract coordinate bounds from HPGL PLT content.

    Parses PA, PU (Pen Up), and PD (Pen Down) commands to find the
    minimum and maximum coordinates. Converts from plotter units
    (1 inch = 1000 units) to inches.

    Args:
        plt_content: Raw HPGL text content.

    Returns:
        Tuple of (x_min, y_min, x_max, y_max) in inches.

    Raises:
        ValueError: If no valid coordinates found in PLT content.
    """
    x_coords = []
    y_coords = []

    points, arcs = _collect_hpgl_geometry(plt_content)

    for x, y in points:
        x_coords.append(x)
        y_coords.append(y)

    # Arcs (drill holes) contribute their full circle bounding box so the
    # measured footprint always contains the complete circle, even for
    # partial sweeps.
    for cx, cy, radius in arcs:
        x_coords.extend((cx - radius, cx + radius))
        y_coords.extend((cy - radius, cy + radius))

    if not x_coords or not y_coords:
        raise ValueError("No valid coordinates found in PLT content")

    # Convert from plotter units (1000 units = 1 inch) to inches
    x_min = min(x_coords) / 1000.0
    x_max = max(x_coords) / 1000.0
    y_min = min(y_coords) / 1000.0
    y_max = max(y_coords) / 1000.0

    return x_min, y_min, x_max, y_max


def _export_to_plt_with_postprocessing(
    doc: vp.Document,
    output_path: Path,
    label: ResolvedLabel,
    text_pens: Optional[Iterable[int]] = None,
) -> None:
    """Export vpype Document to PLT for a single label.

    Each label is rendered independently. Instead of using vpype's write_hpgl()
    which applies complex coordinate transformations, we manually generate HPGL
    commands from the LineCollection to preserve coordinate fidelity.

    Process:
    1. Extract coordinates directly from vpype LineCollection (units are inches)
       and convert them losslessly to plotter units at 1:1000 scale.
    2. Center the text pen(s) vertically within label bounds (one shared
       delta across every text pen so multi-cutter text blocks stay aligned).
    3. Invert the Y-axis to device convention for upright display.

    No uniform scaling is applied because ``_linecollection_to_hpgl`` already
    produces coordinates at nominal scale; rescaling would distort footprints.

    Args:
        doc: The vpype Document to export.
        output_path: Destination PLT file path.
        label: The label being rendered (used to get expected dimensions).
        text_pens: Pen numbers carrying text geometry (one per cutter pen
            when a pen map is in use). Defaults to the historical single
            text pen (``SP1``). Boundary (``SP2``) and hole (``SP3``) pens
            are never centered.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Manually generate HPGL from LineCollection to preserve coordinates.
    # Drill holes are emitted as native HPGL arcs (pen 3) alongside the
    # polyline text/boundary layers.
    holes_hpgl = _render_holes_hpgl(label)
    hpgl_content = _linecollection_to_hpgl(doc, holes_hpgl=holes_hpgl)

    # Write to file
    output_path.write_text(hpgl_content, encoding="utf-8")

    # NOTE: No uniform scaling is applied here. ``_linecollection_to_hpgl``
    # already converts vpype inches directly to plotter units (1 inch = 1000
    # units), so the boundary rectangle spans exactly [0,w]x[0,h]. Rescaling
    # against combined text+boundary bounds would compress geometry whenever
    # ftext glyph descenders extend below y=0, distorting label footprints and
    # causing overlapping borders after bin-packing.

    # Center the text pen(s) vertically within label bounds
    center_pens: frozenset[int] = (
        frozenset({LAYER_TEXT}) if text_pens is None else frozenset(text_pens)
    )
    _center_text_layer_vertically(output_path, label, text_pens=center_pens)

    # Invert Y-axis to device convention so rendered labels display upright.
    # ftext emits glyphs in plotter convention (+y up), but the plotting /
    # HPGL device convention uses +y downward. Without this flip the text is
    # upside-down when visualized (see debug_visualize_text_only.py).
    _flip_y_coordinates_in_plt(output_path)


def _linecollection_to_hpgl(doc: vp.Document, holes_hpgl: str = "") -> str:
    """Convert vpype Document to raw HPGL commands.

    Manually generates HPGL from LineCollections instead of using vpype's
    write_hpgl() to preserve coordinate fidelity. vpype's export applies
    complex coordinate transformations that distort text height.

    Coordinates are converted from inches (vpype units) to plotter units
    (1 inch = 1000 units).

    Args:
        doc: The vpype Document containing line collections for each pen.
        holes_hpgl: Optional pre-rendered HPGL for the drill-hole layer
            (pen 3), produced by :func:`_render_holes_hpgl`. It is emitted
            as native arc commands after the polyline layers.

    Returns:
        Raw HPGL/PLT content as a string.
    """
    lines = [
        "IN",  # Initialize
        "DF",  # Default values
        "PS0",  # Select primary pen slot
    ]

    # Track if we've added any content to know if we need footer
    has_content = False
    skipped_first_pu0_0 = False  # Track if we've skipped the initial PU0,0

    # Process each pen layer present in the document, in ascending pen order.
    # Pen 1 is the default text pen, 2 the boundary, and per-cutter text
    # pens (see build_cutter_pen_map) occupy 1 plus SP4+; drill holes are
    # emitted separately as native arcs under SP3.
    for pen_num in sorted(doc.layers):
        lc = doc.layers.get(pen_num)
        if lc is None or lc.is_empty():
            continue

        # Select this pen
        lines.append(f"SP{pen_num}")
        has_content = True

        # Extract segments from LineCollection
        for segment in lc:
            if segment is None or len(segment) == 0:
                continue

            # Convert from inches to plotter units (1 inch = 1000 units)
            points = []
            for point in segment:
                x_plotter = int(round(point.real * 1000))
                y_plotter = int(round(point.imag * 1000))
                points.append((x_plotter, y_plotter))

            if not points:
                continue

            # Start with PU (pen up) to first point
            x, y = points[0]

            # Skip the redundant initial pen-up when a segment genuinely begins
            # at the origin. This avoids emitting an extra "PU0,0;" that would be
            # duplicated by assembly. Crucially, we must NOT drop any vertices:
            # closed shapes (e.g. boundary rectangles) begin AND end at the
            # origin, so dropping points[0] here would remove a real corner and
            # leave an open outline.
            if not skipped_first_pu0_0 and x == 0 and y == 0:
                skipped_first_pu0_0 = True
                # Emit ALL points (including the leading origin vertex) as PD so
                # closed loops remain geometrically closed. The pen-up to (0,0)
                # is omitted here because assembly supplies it.
                pd_coords = ",".join(f"{px},{py}" for px, py in points)
                lines.append(f"PD{pd_coords}")
            else:
                lines.append(f"PU{x},{y}")

                # Draw to remaining points with PD (pen down)
                if len(points) > 1:
                    pd_coords = ",".join(f"{x},{y}" for x, y in points[1:])
                    lines.append(f"PD{pd_coords}")

    # Drill holes (pen 3) are emitted as native arc commands rather than
    # polylines, so they arrive pre-rendered instead of via the document.
    if holes_hpgl:
        lines.append("SP3")
        lines.append(holes_hpgl)
        has_content = True

    # End sequence - no PU command in footer, let assembly add it
    if has_content:
        lines.append("SP0")
        lines.append("IN")

    return ";".join(lines) + ";"


def _center_text_layer_vertically(
    file_path: Path,
    label: ResolvedLabel,
    text_pens: Iterable[int] = (LAYER_TEXT,),
) -> None:
    """Center the text pen(s) vertically within label bounds after scaling.

    After export and scaling, the text coordinates need to be shifted
    vertically to center them within the label. This function:

    1. Extracts Y coordinates from every text pen section (union across all
       pens in ``text_pens``; borders/holes are never moved)
    2. Calculates the expected center position
    3. Applies one shared Y adjustment to every text pen section

    A single shared delta (computed from the union of all text-pen
    coordinates) keeps multi-cutter text blocks vertically aligned with
    each other -- centering each pen independently would shift lines of
    different cutters apart.

    Args:
        file_path: Path to the PLT file (after scaling).
        label: The label being rendered (for dimensions and margins).
        text_pens: Pen numbers carrying text geometry. Defaults to the
            historical single text pen (``SP1``).
    """
    pens = {int(pen) for pen in text_pens}
    content = file_path.read_text(encoding="utf-8")

    # Split the HPGL stream into pen sections:
    # [prefix, "SP1;", body, "SP2;", body, ...]
    section_pattern = r"(SP\d+;)"
    parts = re.split(section_pattern, content)

    coord_pattern = r"(?:PA|PU|PD)([\d,\-]+)"

    def _section_y_coords(section: str) -> List[int]:
        """Collect every Y coordinate in one pen section."""
        ys: List[int] = []
        for match in re.finditer(coord_pattern, section):
            coord_parts = match.group(1).split(",")
            for i in range(1, len(coord_parts), 2):
                try:
                    ys.append(int(coord_parts[i]))
                except (ValueError, IndexError):
                    pass
        return ys

    # Union of Y coordinates across all text pens centers the whole block.
    y_coords: List[int] = []
    for i in range(1, len(parts), 2):
        try:
            pen_id = int(parts[i][2:-1])
        except ValueError:  # pragma: no cover - SP token always numeric
            continue
        if pen_id in pens:
            y_coords.extend(_section_y_coords(parts[i + 1]))

    if not y_coords:
        # No text layer found
        return

    # Calculate text bounds (in plotter units, post-scaling)
    text_y_min = min(y_coords)
    text_y_max = max(y_coords)

    # Expected center position (in plotter units: 1 inch = 1000 units)
    margin_units = label.margin * 1000.0
    label_height_units = label.height * 1000.0
    available_height_units = label_height_units - (2 * margin_units)
    expected_center_y = margin_units + available_height_units / 2.0

    # Current center of text
    current_center_y = (text_y_min + text_y_max) / 2.0

    # Calculate adjustment
    y_adjustment = expected_center_y - current_center_y

    if abs(y_adjustment) < 0.1:
        # Already centered
        return

    logger.debug(
        f"_center_text_layer_vertically: {file_path.name} - "
        f"text pens={sorted(pens)} "
        f"text Y=[{text_y_min}, {text_y_max}] (center={current_center_y:.1f}), "
        f"expected center={expected_center_y:.1f}, "
        f"adjustment={y_adjustment:.1f} plotter units"
    )

    def adjust_coords_in_pen(coord_match: re.Match[str]) -> str:
        """Shift the Y coordinates of one coordinate command."""
        cmd = coord_match.group(1)
        coord_parts = coord_match.group(2).split(",")

        try:
            adjusted_parts = []
            for i, part in enumerate(coord_parts):
                val = int(part)
                if i % 2 == 1:  # Y coordinate (odd index)
                    adjusted_val = int(round(val + y_adjustment))
                    adjusted_parts.append(str(adjusted_val))
                else:  # X coordinate
                    adjusted_parts.append(part)
            return f"{cmd}{','.join(adjusted_parts)}"
        except (ValueError, IndexError):
            return coord_match.group(0)

    # Apply the shared adjustment to every text pen section.
    coord_pattern_in_pen = r"(PA|PU|PD)([\d,\-]+)"
    for i in range(1, len(parts), 2):
        try:
            pen_id = int(parts[i][2:-1])
        except ValueError:  # pragma: no cover - SP token always numeric
            continue
        if pen_id in pens:
            parts[i + 1] = re.sub(coord_pattern_in_pen, adjust_coords_in_pen, parts[i + 1])

    modified_content = "".join(parts)

    file_path.write_text(modified_content, encoding="utf-8")


def _flip_y_coordinates_in_plt(file_path: Path) -> None:
    """Invert Y-axis coordinates to device (HPGL/display) convention.

    ftext emits glyphs in plotter convention with +y pointing up from the
    baseline. The plotting and HPGL device conventions use +y downward, so
    without this flip rendered text appears upside-down when visualized.

    Mirrors every Y coordinate across the vertical centerline of all content:
        y_flipped = (min_y + max_y) - y

    This keeps bounds non-negative after translation-to-origin and preserves
    label height while correcting orientation. Applies uniformly to all pen
    layers so text, borders, and holes stay aligned.

    Args:
        file_path: Path to the PLT file (after scaling/centering).
    """
    content = file_path.read_text(encoding="utf-8")

    # Extract ALL coordinates and arcs across every layer. Arc (drill hole)
    # extents are included so the mirror centerline accounts for the full
    # circle, keeping holes aligned with text and borders.
    points, arcs = _collect_hpgl_geometry(content)
    all_y: list[int] = [y for _x, y in points]
    for _cx, cy, radius in arcs:
        all_y.extend((cy - radius, cy + radius))

    if not all_y:
        return

    min_y = min(all_y)
    max_y = max(all_y)

    # Mirror across the vertical centerline (y' = span - y). Arc centers are
    # mirrored like any point and their sweep angles are negated so the
    # reflected circles keep their orientation.
    modified_content = _transform_hpgl_coordinates(
        content, flip_y_span=min_y + max_y, flip_y_axis=True
    )
    file_path.write_text(modified_content, encoding="utf-8")

    logger.debug(
        f"_flip_y_coordinates_in_plt: {file_path.name} - "
        f"mirrored Y across centerline (min_y={min_y}, max_y={max_y})"
    )


# ============================================================================
# Local rendering functions (render at 0,0 without translation)
# ============================================================================


def compress_line_to_width(
    line_lc: vp.LineCollection,
    available_width: float,
    max_h_compress: float,
    label_id: str,
    line_text: Optional[str] = None,
) -> vp.LineCollection:
    """Uniformly compress a rendered text line horizontally to fit the margin box.

    Margin precedence for width: when the rendered line is wider than the
    label's inner content area (``available_width``), every X coordinate is
    scaled toward the line's left edge by a uniform factor (Y is untouched),
    so glyphs, kerning, and inter-character spacing all compress together
    (uniform line compression). The scale is bounded by ``max_h_compress``
    via :func:`compute_horizontal_scale`; lines that already fit, or labels
    where compression is disabled (``max_h_compress == 0.0``), are returned
    unchanged.

    Args:
        line_lc: The rendered LineCollection for a single text line.
        available_width: Inner content width in inches (label width minus
            both margins).
        max_h_compress: Maximum compression fraction in ``[0.0, 1.0]``.
        label_id: Identifier used in log messages.
        line_text: Optional rendered text of the line, included in log
            messages so warnings identify the offending line.

    Returns:
        The original collection when no compression is needed or allowed,
        otherwise a new horizontally compressed collection.
    """
    bounds = line_lc.bounds()
    if bounds is None:
        return line_lc
    min_x, _min_y, max_x, _max_y = bounds
    rendered_width = max_x - min_x

    scale = compute_horizontal_scale(rendered_width, available_width, max_h_compress)
    if scale >= 1.0:
        return line_lc

    compressed_width = rendered_width * scale
    line_desc = f" {line_text!r}" if line_text is not None else ""
    if compressed_width > available_width + 1e-9:
        logger.warning(
            "Label %s: text line%s compressed to %.1f%% (%.3fin -> %.3fin) but "
            "still exceeds the available inner width (%.3fin); increase "
            "max_h_compress, widen the label, or reduce margin.",
            label_id,
            line_desc,
            scale * 100.0,
            rendered_width,
            compressed_width,
            available_width,
        )
    else:
        logger.warning(
            "Label %s: text line%s horizontally compressed to fit margin "
            "(%.3fin -> %.3fin, scale %.3f).",
            label_id,
            line_desc,
            rendered_width,
            compressed_width,
            scale,
        )

    # Uniform X scaling anchored at the line's left edge; Y coordinates are
    # untouched so glyph height and vertical stacking are unaffected. The
    # caller re-centers the compressed line horizontally afterwards.
    # NOTE: complex arithmetic must touch only .real -- multiplying a complex
    # segment by a float would (wrongly) scale Y as well.
    compressed = vp.LineCollection()
    for segment in line_lc:
        compressed.append(min_x + (segment.real - min_x) * scale + 1j * segment.imag)
    return compressed


def _apply_collision_compress(line_lc: vp.LineCollection, scale: float) -> vp.LineCollection:
    """Uniformly scale a rendered line horizontally by a collision-avoidance factor.

    Unlike :func:`compress_line_to_width` (which only engages when a line
    overflows the inner content area), this applies an unconditional uniform
    X scale produced by the text-hole collision resolution sweep. Y is
    untouched and the line's left edge is kept fixed; the caller re-aligns
    the compressed line afterwards.

    Args:
        line_lc: The rendered LineCollection for a single text line.
        scale: Uniform horizontal scale in ``(0.0, 1.0]``. ``1.0`` returns
            the collection unchanged.

    Returns:
        The original collection when ``scale >= 1.0`` or bounds are
        unavailable, otherwise a new horizontally scaled collection.
    """
    if scale >= 1.0:
        return line_lc
    bounds = line_lc.bounds()
    if bounds is None:  # pragma: no cover - callers check emptiness
        return line_lc
    min_x = bounds[0]
    compressed = vp.LineCollection()
    for segment in line_lc:
        compressed.append(min_x + (segment.real - min_x) * scale + 1j * segment.imag)
    return compressed


def _render_positioned_lines(
    label: ResolvedLabel,
    chunk_mode: TextChunkMode = TextChunkMode.LINE,
) -> List[Tuple[int, vp.LineCollection, _LineEntry, Optional[List[Tuple[str, List[int]]]]]]:
    """Render and position every text line of a label (shared core).

    NOTE: The absolute vertical anchor is irrelevant because the export
    pipeline re-centers the whole text block POST-EXPORT in
    ``_center_text_layer_vertically()``. Individual lines are still stacked
    here: rendering every line at y=0 caused multi-line labels to print all
    lines on top of each other (overlapping glyphs).

    Two-pass algorithm (mirrors ``vectorize._render_text``):
    1. Render each line at its ``toolpath_text_height`` and measure its
       rendered height; total height = sum of line heights plus
       ``line_spacing`` between consecutive rendered lines.
    2. Position each line top-to-bottom (plotter convention, +y up) so lines
       are stacked without overlap. The later Y-flip in
       ``_flip_y_coordinates_in_plt`` preserves the visual line order.

    Lines are horizontally aligned within the label's content area according
    to each line's ``text_h_alignment`` ("left" anchors the line's left-most
    point at the left margin, "right" anchors the right-most point at the
    right margin, "center" centers it). Text is
    rendered with the single-line Relief CAD font via ftext, replacing vpype's
    built-in Hershey stroke-font engine.

    When ``label.collision_compress`` is below ``1.0`` (set by the text-hole
    collision resolution sweep), every rendered line is additionally scaled
    horizontally by that factor before alignment.

    Args:
        label: The resolved label whose ``content`` should be rendered.
        chunk_mode: When ``WORD``, each line is additionally partitioned into
            whitespace-delimited word groups (contour indices into the
            rendered line, exact by construction). ``LINE`` (the default)
            reports no word groups.

    Returns:
        One ``(line_index, positioned_lc, entry, word_groups)`` tuple per
        renderable line, in content order, where ``positioned_lc`` is the
        positioned LineCollection, ``entry`` is its ``(line_index,
        line_text, bounds)`` record in label-local coordinates (pre-export
        anchor, block centered around y=0), and ``word_groups`` is ``None``
        in line mode or a list of ``(word_text, contour_indices)`` pairs
        indexing ``positioned_lc`` contours in word order. The export
        pipeline vertically centers the block at ``height / 2``; collision
        detection applies that shift itself.
    """
    if not label.content:
        return []

    margin = label.margin
    inner_width = label.width

    # First pass: render all lines and measure their heights. Keep each
    # line's own line_spacing alongside the rendered geometry so empty or
    # unrenderable lines don't misalign spacing between real lines. The
    # original ``content`` index is preserved so collision reports can name
    # the offending line even when earlier lines were unrenderable.
    rendered_lines: list[
        Tuple[
            int,
            vp.LineCollection,
            float,
            float,
            float,
            str,
            Optional[List[Tuple[str, List[int]]]],
        ]
    ] = []
    total_rendered_height = 0.0

    for line_index, line in enumerate(label.content):
        # Render at the toolpath_text_height (cutter-compensated) using the
        # single-line TTF font. ftext returns upright glyphs with baseline at 0.
        word_groups: Optional[List[Tuple[str, List[int]]]] = None
        if chunk_mode is TextChunkMode.WORD:
            filtered_lc, groups = render_text_line_ftext_with_words(
                line.text,
                target_height_inches=line.toolpath_text_height,
            )
            # An empty group list means grouping was unavailable for this
            # line; fall back to whole-line chunking for it.
            word_groups = groups or None
        else:
            filtered_lc = render_text_line_ftext(
                line.text,
                target_height_inches=line.toolpath_text_height,
            )

        if filtered_lc.is_empty():
            continue

        bounds = filtered_lc.bounds()
        if bounds is None:
            continue
        _min_x, min_y, _max_x, max_y = bounds
        rendered_height = max_y - min_y

        # Collision-avoidance compression (Phase 3): an unconditional uniform
        # X scale discovered by the collision sweep, applied before the
        # regular margin-driven compression below.
        filtered_lc = _apply_collision_compress(filtered_lc, label.collision_compress)
        bounds = filtered_lc.bounds()
        if bounds is None:  # pragma: no cover - measured just above
            continue
        rendered_height = bounds[3] - bounds[1]

        rendered_lines.append(
            (
                line_index,
                filtered_lc,
                rendered_height,
                line.line_spacing,
                line.max_h_compress,
                line.text_h_alignment,
                word_groups,
            )
        )
        total_rendered_height += rendered_height

    if not rendered_lines:
        return []

    # Add line spacing between lines (not after the last line). Margin
    # precedence: if the measured block (line heights + requested spacing)
    # overflows the inner area, shrink the spacing so margins win.
    spacings = [
        line_spacing for _idx, _lc, _height, line_spacing, _mhc, _align, _wg in rendered_lines[:-1]
    ]
    available_height = label.height - (2 * margin)
    adjusted_spacings = fit_line_spacing_to_margins(
        [height for _idx, _lc, height, _spacing, _mhc, _align, _wg in rendered_lines],
        spacings,
        available_height,
    )
    if adjusted_spacings != spacings:
        logger.warning(
            "Label %s: line_spacing reduced at render time from %s to %s "
            "to preserve margin %.3fin.",
            label.id,
            [round(s, 4) for s in spacings],
            [round(s, 4) for s in adjusted_spacings],
            margin,
        )
    total_rendered_height = sum(
        height for _idx, _lc, height, _spacing, _mhc, _align, _wg in rendered_lines
    ) + sum(adjusted_spacings)

    # Anchor the block so its vertical center sits at y = total / 2. The
    # absolute anchor is irrelevant (post-export centering fixes it); only
    # the relative stacking matters.
    current_y = total_rendered_height / 2.0

    # Second pass: position each line, stacked top-to-bottom (+y up).
    positioned: List[
        Tuple[int, vp.LineCollection, _LineEntry, Optional[List[Tuple[str, List[int]]]]]
    ] = []
    for i, (
        line_index,
        filtered_lc,
        rendered_height,
        _line_spacing,
        max_h_compress,
        text_h_alignment,
        word_groups,
    ) in enumerate(rendered_lines):
        bounds = filtered_lc.bounds()
        if bounds is None:  # pragma: no cover - measured in first pass
            continue

        # Margin precedence for width: compress over-wide lines so they
        # respect the inner content area (bounded by max_h_compress).
        available_width = inner_width - (2 * margin)
        filtered_lc = compress_line_to_width(
            filtered_lc,
            available_width,
            max_h_compress,
            label.id,
            line_text=label.content[line_index].text,
        )
        bounds = filtered_lc.bounds()
        if bounds is None:  # pragma: no cover - measured in first pass
            continue
        min_x, _min_y, max_x, max_y = bounds
        rendered_width = max_x - min_x

        # Horizontal alignment within the available width: "left" anchors
        # the line's left-most point at the left margin, "right" anchors
        # the right-most point at the right margin, "center" centers it.
        target_left_x = compute_horizontal_offset(
            rendered_width, available_width, margin, text_h_alignment
        )
        x_offset = target_left_x - min_x

        # Vertical stacking: top of this line's glyphs at current_y.
        y_offset = current_y - max_y
        filtered_lc.translate(x_offset, y_offset)

        positioned_bounds = filtered_lc.bounds()
        if positioned_bounds is not None:
            positioned.append(
                (
                    line_index,
                    filtered_lc,
                    (line_index, label.content[line_index].text, positioned_bounds),
                    word_groups,
                )
            )

        # Move down past this line (plus adjusted spacing, except after
        # the last).
        current_y -= rendered_height
        if i < len(adjusted_spacings):
            current_y -= adjusted_spacings[i]

    return positioned


def _render_text_local_with_bounds(
    label: ResolvedLabel,
) -> Tuple[vp.LineCollection, List[_LineEntry]]:
    """Render text at local coordinates and report per-line bounds.

    Thin wrapper over :func:`_render_positioned_lines` returning the
    combined LineCollection (all lines on one collection) plus the
    per-line bounds records used for collision detection.

    Args:
        label: The resolved label whose ``content`` should be rendered.

    Returns:
        A tuple ``(combined_lc, line_entries)`` where ``combined_lc`` holds
        all rendered lines and ``line_entries`` is a list of
        ``(line_index, line_text, bounds)`` tuples in label-local
        coordinates (pre-export anchor, block centered around y=0). The
        export pipeline vertically centers the block at ``height / 2``;
        collision detection applies that shift itself.
    """
    text_lc = vp.LineCollection()
    line_entries: List[_LineEntry] = []
    for _line_index, positioned_lc, entry, _wg in _render_positioned_lines(label):
        text_lc.extend(positioned_lc)
        line_entries.append(entry)
    return text_lc, line_entries


def _render_text_lines_by_pen(
    label: ResolvedLabel,
    pen_map: Optional[dict[float, int]] = None,
    chunk_mode: TextChunkMode = TextChunkMode.LINE,
) -> Tuple[dict[int, vp.LineCollection], List[_LineEntry], List[TextChunkRecord]]:
    """Render text lines grouped onto per-cutter pen layers.

    Each positioned line's LineCollection is appended to the vpype layer
    of its cutter's pen (see
    :func:`plt_optimizer.generate.resolution.build_cutter_pen_map`). Lines
    whose cutter is absent from ``pen_map`` (or when no map is supplied)
    fall back to the historical text pen (``SP1``), preserving
    back-compatible single-pen output.

    Args:
        label: The resolved label whose ``content`` should be rendered.
        pen_map: Optional mapping of cutter diameter to pen number.
        chunk_mode: Granularity of the returned chunk records (see
            :func:`_render_positioned_lines`).

    Returns:
        Tuple of ``(pens, line_entries, chunk_records)`` where ``pens`` maps
        pen number to the LineCollection for that pen (only non-empty pens
        included), ``line_entries`` is the same per-line bounds record list
        returned by :func:`_render_text_local_with_bounds`, and
        ``chunk_records`` holds one :class:`TextChunkRecord` per chunk (per
        line in ``LINE`` mode; per word plus any ungrouped line in ``WORD``
        mode) in label-local coordinates for plate-space optimization.
    """
    pens: dict[int, vp.LineCollection] = {}
    line_entries: List[_LineEntry] = []
    chunk_records: List[TextChunkRecord] = []
    for line_index, positioned_lc, entry, word_groups in _render_positioned_lines(
        label, chunk_mode=chunk_mode
    ):
        pen = LAYER_TEXT
        if pen_map is not None and 0 <= line_index < len(label.content):
            cutter = label.content[line_index].cutter_diameter
            pen = pen_map.get(cutter, LAYER_TEXT)
        pens.setdefault(pen, vp.LineCollection()).extend(positioned_lc)
        line_entries.append(entry)

        contours = tuple(np.asarray(line) for line in positioned_lc)
        if word_groups is not None:
            for word_index, (word_text, indices) in enumerate(word_groups):
                if not indices:
                    continue  # blank segment: no strokes to route
                word_contours = tuple(contours[i] for i in indices)
                chunk_records.append(
                    TextChunkRecord(
                        line_index=line_index,
                        word_index=word_index,
                        word_text=word_text,
                        pen=pen,
                        contours=word_contours,
                        bounds=_contours_bounds(word_contours),
                    )
                )
        else:
            chunk_records.append(
                TextChunkRecord(
                    line_index=line_index,
                    word_index=None,
                    word_text="",
                    pen=pen,
                    contours=contours,
                    bounds=entry[2],
                )
            )
    return pens, line_entries, chunk_records


def _contours_bounds(contours: Sequence[np.ndarray]) -> Tuple[float, float, float, float]:
    """Return the ``(x_min, y_min, x_max, y_max)`` bounds of vertex arrays.

    Args:
        contours: Complex vertex arrays (one per contour).

    Returns:
        Bounds tuple in the contours' coordinate units. Degenerate/empty
        input yields an all-zero tuple.
    """
    xs: List[float] = []
    ys: List[float] = []
    for contour in contours:
        if len(contour) == 0:
            continue
        xs.extend(contour.real.tolist())
        ys.extend(contour.imag.tolist())
    if not xs or not ys:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


def _render_boundary_local(label: ResolvedLabel) -> vp.LineCollection:
    """Render boundary rectangle at local coordinates."""
    lc = vp.LineCollection()
    rect_array = vp.rect(0, 0, label.width, label.height)
    # vp.rect() returns a numpy array of complex coordinates, need to convert to LineCollection
    if isinstance(rect_array, np.ndarray) and rect_array.size > 0:
        lc.append(rect_array)
    return lc


def _hole_circles_local(label: ResolvedLabel) -> List[Tuple[float, float, float]]:
    """Compute drill-hole circles in the label's local coordinate space.

    Each circle center is inset from the relevant edge(s) by ``hole_margin +
    radius``, so the closest point of the circle sits exactly
    ``label.hole_margin`` inches from the label boundary. With a margin of
    0.0 the circle is tangent to the edge.

    Args:
        label: The resolved label whose ``holes`` should be measured.

    Returns:
        One ``(center_x, center_y, radius)`` tuple per hole, in inches.
    """
    circles: List[Tuple[float, float, float]] = []

    for hole in label.holes:
        radius = hole.diameter / 2.0
        # Distance from the edge to the hole center: the requested hole
        # margin plus the radius, so the circle's closest point is
        # hole_margin inches away from the edge.
        offset = label.hole_margin + radius
        location = str(hole.location)

        if location == "top-left":
            hole_x = offset
            hole_y = label.height - offset
        elif location == "top-right":
            hole_x = label.width - offset
            hole_y = label.height - offset
        elif location == "bottom-left":
            hole_x = offset
            hole_y = offset
        elif location == "bottom-right":
            hole_x = label.width - offset
            hole_y = offset
        elif location == "left":
            hole_x = offset
            hole_y = label.height / 2.0
        elif location == "right":
            hole_x = label.width - offset
            hole_y = label.height / 2.0
        elif location == "top":
            hole_x = label.width / 2.0
            hole_y = label.height - offset
        elif location == "bottom":
            hole_x = label.width / 2.0
            hole_y = offset
        else:  # pragma: no cover - schema validates the location enum
            continue

        circles.append((hole_x, hole_y, radius))

    return circles


def _render_holes_hpgl(label: ResolvedLabel) -> str:
    """Render drill holes as native HPGL arc (``AA``) commands.

    Each hole becomes one closed circle drawn as four 90-degree absolute
    arcs, matching the EngraveLab drill-hole convention already understood
    by the parser, writer, profiler and plotter::

        PU{sx},{sy};PD{sx},{sy};AA{cx},{cy},90;AA{cx},{cy},90;AA{cx},{cy},90;AA{cx},{cy},90

    The zero-length ``PD`` plunge is deliberate: the parser only opens a new
    stroke path when a pen-down segment is emitted, so without it every hole
    on the layer would merge into one path and the profiler's "3+ arcs
    totaling ~360 degrees plus an optional plunge" drill-hole rule would not
    fire.

    Emitting arcs instead of a sampled polygon keeps the drill circles
    perfectly smooth on the plotter regardless of hole diameter, and lets
    the profiler recognise them as structural features.

    Args:
        label: The resolved label whose ``holes`` should be emitted.

    Returns:
        HPGL command string (without the ``SP`` wrapper), or an empty string
        when the label has no holes. Coordinates are in plotter units
        (1 inch = 1000 units).
    """
    commands: List[str] = []

    for center_x, center_y, radius in _hole_circles_local(label):
        # Convert to plotter units so the emitted PU start and AA center are
        # mutually consistent integers (the parser derives the radius from
        # their distance).
        cx = int(round(center_x * 1000))
        cy = int(round(center_y * 1000))
        r = int(round(radius * 1000))
        if r <= 0:  # pragma: no cover - degenerate hole
            continue

        # Begin at the rightmost point of the circle, then sweep four
        # quarter-arcs back to it.
        start = f"{cx + r},{cy}"
        commands.append(f"PU{start}")
        commands.append(f"PD{start}")  # zero-length plunge, opens a new path
        commands.extend(f"AA{cx},{cy},90" for _ in range(4))

    return ";".join(commands)
