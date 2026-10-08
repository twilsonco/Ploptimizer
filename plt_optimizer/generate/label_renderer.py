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
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple

import numpy as np
import vpype as vp

from plt_optimizer.generate.font_registry import FontNotFoundError, resolve_font
from plt_optimizer.generate.ftext_renderer import (
    FtextRenderError,
    glyph_groups_for_line,
    render_text_line_ftext,
    render_text_line_ftext_with_words,
)
from plt_optimizer.generate.geometry import (
    CollisionResult,
    arc_swept_bounds,
    circle_aabb_gap,
)
from plt_optimizer.generate.plt_font_renderer import (
    PltFontRenderError,
    render_text_line_plt_font,
    render_text_line_plt_font_with_glyphs,
    render_text_line_plt_font_with_words,
)
from plt_optimizer.generate.resolution import (
    ResolvedLabel,
    ResolvedTextLine,
    compute_horizontal_offset,
    compute_horizontal_scale,
    fit_line_spacing_to_margins,
)
from plt_optimizer.generate.text_geometry import TextBlock, block_from_linecollection

logger = logging.getLogger(__name__)

# Layer assignments (pen 2/3 must match vectorize.py structural pens)
LAYER_TEXT: int = 1
LAYER_BOUNDARY: int = 2
LAYER_HOLES: int = 3

# PLT document framing (EngraveLab reference parity). The header is
# ``IN;PA;`` -- tool option headers (VS/ZO/...) are inserted after ``IN;``
# by ``tool_options.prepend_tool_option_headers``, so the header always
# terminates with ``PA;`` before geometry starts. The footer is the bare
# pen-deselect ``SP;`` (no DF/PS0, no SP0 reset, no trailing IN, no ``%``).
# Written files additionally end with a single newline (appended at the
# per-cutter write sites in ``vectorize.export_per_cutter_plts``).
PLT_HEADER: str = "IN;PA;"
PLT_FOOTER: str = "SP;"

# Resolution sweep granularity for text-hole collision avoidance. The
# margin sweep reuses already-rendered text geometry (holes move, text
# does not), so it is cheap; the compression sweep re-renders text per
# step, so it stays small.
_MARGIN_ADJUST_STEPS: int = 32
_COMPRESSION_RESOLVE_STEPS: int = 16


class _LineEntry(NamedTuple):
    """A rendered text line's collision/compression-relevant record.

    Attributes:
        line_index: Index of the line in ``ResolvedLabel.content``.
        line_text: The line's text (for collision reports).
        bounds: ``(x_min, y_min, x_max, y_max)`` in label-local coordinates.
        compression_scale: Effective horizontal scale applied to the line,
            i.e. the collision-avoidance scale multiplied by the
            margin-overflow scale (both ``1.0`` when untouched). ``1.0``
            means the line rendered at its natural width; values below
            report how far the emitted geometry was squeezed.
        line_spacing: Effective vertical gap *below* this line, in inches
            (the render-time, margin-clamped spacing actually used for
            stacking). ``None`` on the last renderable line, which has no
            gap after it.
    """

    line_index: int
    line_text: str
    bounds: Tuple[float, float, float, float]
    compression_scale: float = 1.0
    line_spacing: Optional[float] = None


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

    Geometry is arc-native: :attr:`blocks` preserve ``AA`` arcs end-to-end
    (PLT-extracted fonts), so the emitted cut file keeps native arcs. The
    :attr:`contours` property exposes the legacy polyline view (arcs
    flattened) for vertex-level consumers and tests.

    Attributes:
        line_index: Index of the chunk's text line in ``ResolvedLabel.content``.
        word_index: Index of the word within the line's whitespace split, or
            ``None`` for a whole-line chunk.
        word_text: The word's text (empty string for whole-line chunks).
        pen: HPGL pen number the chunk is emitted on (its cutter's pen).
        blocks: The chunk's strokes as arc-native :class:`TextBlock` objects
            (one per rendered source block), label-local inches with ``+y``
            up (pre-export frame; transformed to the device frame by the
            plate-space export pipeline).
        bounds: ``(x_min, y_min, x_max, y_max)`` of :attr:`blocks` (swept-
            analytic: arcs contribute their swept extent, never a full circle).
        glyph_groups: Stroke indices into the flattened :attr:`blocks`
            sequence, one tuple per rendered character in text order (spaces
            contribute no strokes and no group). Populated when the renderer
            can partition the line's strokes per character exactly; empty
            when the partition is unavailable (degenerate fonts, dropped
            contours). Consumed by the plate-space intra-chunk glyph sweep,
            which reverses whole glyphs (stroke order + tracing direction)
            to cut intra-chunk rapid travel.
    """

    line_index: int
    word_index: Optional[int]
    word_text: str
    pen: int
    blocks: Tuple[TextBlock, ...]
    bounds: Tuple[float, float, float, float]
    glyph_groups: Tuple[Tuple[int, ...], ...] = ()

    @property
    def contours(self) -> Tuple[np.ndarray, ...]:
        """Polyline view of :attr:`blocks` (arcs flattened, faithful chains).

        Returns:
            One complex vertex array per stroke, in block order.
        """
        return tuple(
            np.asarray(line) for block in self.blocks for line in block.to_vpype_polylines()
        )


class LabelRenderError(Exception):
    """Raised to abort a job containing unavoidable text-hole collisions.

    Emitted by :func:`assert_no_collisions` once every label in the job has
    been rendered (so all per-label ERROR diagnostics were logged first)
    and at least one label still overlaps a drill hole. Collisions that
    collision avoidance repaired are tolerated (logged as WARNING at render
    time); only collisions that survive every enabled avoidance phase are
    unacceptable output requiring a jobspec revision.
    """


@dataclass(frozen=True)
class RenderedLabel:
    """A label rendered to HPGL format with measured bounds.

    Attributes:
        source_label: The original ResolvedLabel that was rendered. When
            text-hole collision avoidance adjusted the label (reduced
            ``hole_margin`` and/or applied per-line ``collision_compress_by_line``),
            this is the *adjusted* label so downstream consumers (packing, plate
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
            False even though avoidance was triggered, and their jobs
            proceed. This is the flag the job-level gate aborts on.
        collision_detected: True when any text/hole collision was detected
            during rendering, even if collision avoidance (Phases 2/3)
            successfully resolved it. This is an observational flag: a
            repaired collision logs a WARNING and the job proceeds, so the
            job-level gate :func:`assert_no_collisions` aborts on
            :attr:`has_collisions` (unresolved collisions) rather than on
            this flag.
        text_chunks: Per-chunk rendered text geometry (line- or word-level,
            per the label's chunk mode) in label-local inches with ``+y`` up.
            Consumed by the plate-space optimizer, which transforms these
            into device coordinates and builds one routing node per chunk
            (skipping the parser/profiler entirely). Empty for labels with no
            rendered text.
        compression_by_line: Mapping of line index to the *effective*
            horizontal scale applied while rendering, i.e. the
            collision-avoidance scale (Phase 3) multiplied by the
            margin-overflow scale (``max_h_compress`` compression). Only
            lines compressed below ``1.0`` are included; an empty dict
            (the default) means every line rendered at its natural width.
            Reporting-only: nothing downstream consumes it for geometry.
        line_spacing_by_line: Mapping of line index to the *effective*
            vertical gap below that line, in inches -- the render-time
            spacing actually used for stacking, after
            :func:`fit_line_spacing_to_margins` clamped it to preserve the
            vertical margins. Keyed by the line the gap sits *below*; the
            last renderable line has no entry (n-1 gaps for n lines).
            All gaps are included, so the requested-vs-effective delta is
            computed by consumers against the source label's per-line
            ``line_spacing``. Reporting-only (single-line labels yield an
            empty dict): nothing downstream consumes it for geometry.
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
    compression_by_line: dict[int, float] = field(default_factory=dict)
    line_spacing_by_line: dict[int, float] = field(default_factory=dict)


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
        line_entries: Per-line :class:`_LineEntry` records as returned by
            :func:`_render_text_local_with_bounds`.

    Returns:
        One :class:`CollisionResult` per colliding (line, hole) pair.
        Empty when the label has no holes or no rendered lines.
    """
    if not label.holes or not line_entries:
        return []

    circles = _hole_circles_local(label)
    shift_y = label.height / 2.0
    results: List[CollisionResult] = []

    for line_index, line_text, bounds, _scale, _spacing in line_entries:
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


def _log_collisions(
    label: ResolvedLabel,
    collisions: Sequence[CollisionResult],
    level: int = logging.ERROR,
) -> None:
    """Log each detected text-hole collision at the given severity.

    Args:
        label: The resolved label being checked (provides the threshold
            components used in the message breakdown).
        collisions: Collision results from :func:`_detect_text_hole_collisions`.
        level: Logging severity. WARNING when collision avoidance resolved
            the collisions, ERROR when they remain unresolved.
    """
    for collision in collisions:
        required = _collision_threshold(label, collision.line_index)
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
        line_desc: Optional description of the offending text lines,
            included in the resolution WARNING for informative output.

    Returns:
        A clone of ``label`` with ``collision_compress_by_line`` set to
        per-line scales when one clears all collisions, otherwise ``None``.
    """
    floor = max(0.0, 1.0 - min(budget, 1.0))

    # First pass: detect collisions and identify which lines are colliding
    _candidate_lc, candidate_entries = _render_text_local_with_bounds(label)
    sweep_collisions = _detect_text_hole_collisions(label, candidate_entries)
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

        # Re-render and check collisions
        _candidate_lc, candidate_entries = _render_text_local_with_bounds(candidate_label)
        sweep_collisions = _detect_text_hole_collisions(candidate_label, candidate_entries)

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


def render_label_to_plt(
    label: ResolvedLabel,
    pen_map: Optional[dict[tuple[float, str], int]] = None,
    *,
    check_glyph_coverage: bool = True,
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
       every drill hole; each collision is recorded on the returned
       :attr:`RenderedLabel.collision_detected` (and
       :attr:`RenderedLabel.has_collisions` when unresolved).
    2. **Hole-margin reduction** (opt-in via ``min_hole_margin``): holes
       are moved toward the label edge (margin reduced toward the floor)
       until the text clears them.
    3. **Horizontal compression** (opt-in via ``max_h_compress``): text is
       uniformly compressed horizontally (stacked on top of any Phase 2
       margin reduction) until the collisions clear.

    Severity follows the outcome. A collision that an enabled phase
    resolves logs its detections at WARNING and the render proceeds
    (``collision_detected=True``, ``has_collisions=False``). A collision
    that no enabled phase can clear logs its detections plus full
    diagnostics (label id, offending text lines, holes, and clearance
    shortfalls) at ERROR and flags ``has_collisions=True``: only
    unresolved collisions are unacceptable output, and the job-level gate
    :func:`assert_no_collisions` aborts the run with
    :class:`LabelRenderError` once every label has been rendered, so all
    offending labels report before the job stops.

    Args:
        label: The ResolvedLabel to render (text, borders, holes).
        pen_map: Optional mapping of ``(text cutter diameter, text color)``
            to HPGL pen number (see
            :func:`plt_optimizer.generate.resolution.build_cutter_pen_map`).
            Each text line is emitted on the pen of its cutter/color layer
            so per-cutter PLT files can be split out after assembly, and
            lines sharing a cutter but differing in ``text_color`` land on
            distinct pens. ``None`` (the default) renders all text on the
            historical text pen (``SP1``), preserving back-compatible
            single-pen output. Boundary lines always use ``SP2`` and drill
            holes ``SP3``.
        check_glyph_coverage: When True (the default), a TrueType font
            missing a glyph for a text line fails the render with
            :class:`LabelRenderError` (see
            :func:`plt_optimizer.generate.ftext_renderer.render_text_line_ftext`).
            The font-showcase tool sets it False: rendering the ``.notdef``
            box *is* the coverage information a showcase reports.

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
    rendered, line_entries = _render_label_once(
        label, pen_map=pen_map, check_glyph_coverage=check_glyph_coverage
    )
    collisions = _detect_text_hole_collisions(label, line_entries)
    if not collisions:
        return rendered

    # ---- Phase 1: collision detected -- attempt resolution ----
    # Per-collision logging is deferred until the outcome is known:
    # WARNING when an avoidance phase resolves the collisions, ERROR when
    # they remain unresolved (only unresolved collisions are unacceptable).
    detected_collisions = collisions
    base_label: ResolvedLabel = label
    line_desc = _collision_line_desc(collisions)

    # ---- Phase 2: reduce hole_margin toward min_hole_margin ----
    if label.min_hole_margin is not None:
        margin_label = _resolve_collision_via_margin_adjustment(
            label, line_entries, line_desc=line_desc
        )
        if margin_label is not None:
            _log_collisions(label, detected_collisions, level=logging.WARNING)
            resolved_rendered, _ = _render_label_once(
                margin_label, pen_map=pen_map, check_glyph_coverage=check_glyph_coverage
            )
            return replace(resolved_rendered, collision_detected=True)
        # Margin alone cannot clear the collision; continue from the floor
        # margin so Phase 3 compression stacks on top of the maximum
        # allowed margin reduction.
        floor = max(0.0, label.min_hole_margin)
        if floor < label.hole_margin:
            base_label = replace(label, hole_margin=floor)
            collisions = _detect_text_hole_collisions(base_label, line_entries)
            logger.debug(
                "Label %s: Phase 2 margin adjustment failed. "
                "Reduced hole_margin from %.4fin to %.4fin floor. "
                "Remaining collisions after margin reduction: %d",
                label.id,
                label.hole_margin,
                floor,
                len(collisions),
            )

    # ---- Phase 3: compress text horizontally within max_h_compress ----
    budget = min((line.max_h_compress for line in label.content), default=0.0)
    attempted_scale: Optional[float] = None
    if budget > 0.0:
        compressed_label = _resolve_collision_via_compression(
            base_label, budget, line_desc=line_desc
        )
        if compressed_label is not None:
            # Verify that the compression actually cleared collisions in the
            # final render. The compression sweep uses _render_text_local_with_bounds
            # which may produce different bounds than the final _render_label_once,
            # so we must re-check with the actual rendered geometry.
            resolved_rendered, resolved_line_entries = _render_label_once(
                compressed_label,
                pen_map=pen_map,
                check_glyph_coverage=check_glyph_coverage,
            )
            final_collisions = _detect_text_hole_collisions(compressed_label, resolved_line_entries)
            if not final_collisions:
                _log_collisions(label, detected_collisions, level=logging.WARNING)
                return replace(resolved_rendered, collision_detected=True)
            # Compression found a scale that cleared collisions in the sweep,
            # but the final render still has collisions. Log at DEBUG level
            # since we'll report the full error diagnostics below.
            if compressed_label.collision_compress_by_line:
                scales = list(compressed_label.collision_compress_by_line.values())
                avg_scale = sum(scales) / len(scales) if scales else 1.0
                logger.debug(
                    "Label %s: Compression sweep found per-line scales (avg %.3f) but final render "
                    "still has %d collision(s).",
                    label.id,
                    avg_scale,
                    len(final_collisions),
                )
            collisions = final_collisions
            base_label = compressed_label
        # Report the state at the most aggressive scale tried, so the
        # measured gaps match what the sweep actually evaluated.
        attempted_scale = max(0.0, 1.0 - min(budget, 1.0))
        # For diagnostics, create per-line compression map with the floor scale
        per_line_floor_compress = dict.fromkeys(range(len(base_label.content)), attempted_scale)
        final_label = replace(base_label, collision_compress_by_line=per_line_floor_compress)
        _, final_entries = _render_text_local_with_bounds(
            final_label, check_glyph_coverage=check_glyph_coverage
        )
        collisions = _detect_text_hole_collisions(final_label, final_entries)
        base_label = final_label

    # ---- Unresolvable: collisions are unacceptable output ----
    # Log the detections plus full diagnostics at ERROR level and flag the
    # render. The job-level gate (assert_no_collisions) aborts the run once
    # every label has been rendered, so all offending labels report first.
    _log_collisions(label, detected_collisions, level=logging.ERROR)
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
    pen_map: Optional[dict[tuple[float, str], int]] = None,
    *,
    check_glyph_coverage: bool = True,
) -> Tuple[RenderedLabel, List[_LineEntry]]:
    """Render a single label to PLT without collision resolution.

    Args:
        label: The ResolvedLabel to render (text, borders, holes).
        pen_map: Optional ``(cutter, color)-to-pen`` mapping for per-cutter
            text layers (see :func:`render_label_to_plt`). ``None`` puts
            all text on the historical text pen (``SP1``).
        check_glyph_coverage: When True (the default), a TrueType font
            missing a glyph fails the render.

    Returns:
        Tuple of the :class:`RenderedLabel` (with ``has_collisions`` left
        at its default) and the per-line rendered bounds used by
        :func:`_detect_text_hole_collisions`.
    """
    # Create vpype Document (polylines only: the boundary rectangle). Text is
    # no longer routed through vpype because a LineCollection cannot carry
    # arcs; text blocks are emitted as native HPGL alongside the boundary.
    doc = vp.Document()

    # Render text layers, one arc-native block tuple per cutter pen (SP1
    # only when no pen_map is supplied, preserving the historical
    # single-pen output).
    text_pens, line_entries, chunk_records = _render_text_lines_by_pen(
        label,
        pen_map,
        chunk_mode=_chunk_mode_of(label),
        check_glyph_coverage=check_glyph_coverage,
    )

    # Render boundary layer
    boundary_lc = _render_boundary_local(label)
    if not boundary_lc.is_empty():
        doc.add(boundary_lc, LAYER_BOUNDARY)

    # NOTE: Drill holes are intentionally NOT added to the vpype document.
    # A LineCollection can only represent polylines, which would force the
    # circles to be emitted as polygons. Holes are instead emitted directly
    # as native HPGL arc (``AA``) commands by ``_render_holes_hpgl``. Text
    # is likewise emitted as native HPGL (arcs preserved) by
    # ``_linecollection_to_hpgl`` from ``text_pens``.

    # Export to temporary file with postprocessing
    with tempfile.NamedTemporaryFile(mode="w", suffix=".plt", delete=False) as f:
        temp_path = Path(f.name)

    try:
        _export_to_plt_with_postprocessing(
            doc, temp_path, label, text_pens=set(text_pens), text_blocks=text_pens
        )
        plt_content = temp_path.read_text().strip()

        # Extract bounds from rendered PLT
        x_min, y_min, x_max, y_max = extract_bounds_from_plt(plt_content)

        # Effective per-line horizontal scales (collision x margin),
        # reporting-only. Lines left at natural width (scale 1.0) are
        # omitted so the common case stays an empty dict.
        compression_by_line = {
            entry.line_index: entry.compression_scale
            for entry in line_entries
            if entry.compression_scale < 1.0
        }

        # Effective per-line vertical gaps (render-time, margin-clamped
        # spacing below each line), reporting-only. All gaps are recorded
        # -- keyed by the upper line's content index -- so consumers can
        # diff them against the requested spacing. The last renderable
        # line has no gap (``None``) and is omitted.
        line_spacing_by_line = {
            entry.line_index: entry.line_spacing
            for entry in line_entries
            if entry.line_spacing is not None
        }

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
            compression_by_line=compression_by_line,
            line_spacing_by_line=line_spacing_by_line,
        )
        return rendered, line_entries
    finally:
        temp_path.unlink(missing_ok=True)


class ArcExtent(NamedTuple):
    """One ``AA`` arc recovered from HPGL content, in plotter units.

    Attributes:
        cx: Arc center X.
        cy: Arc center Y.
        radius: Distance from the arc's start (the pen position when the
            ``AA`` was emitted) to its center.
        start_angle: Angle of the arc's start point around the center, in
            degrees.
        sweep_angle: Signed sweep angle in degrees (HPGL ``AA`` convention).
    """

    cx: int
    cy: int
    radius: int
    start_angle: float
    sweep_angle: float

    def swept_bounds(self) -> Tuple[float, float, float, float]:
        """Return the ``(x_min, y_min, x_max, y_max)`` of the *swept* arc.

        Single-line glyph fonts approximate near-straight strokes with
        huge-radius best-fit arcs, so the arc's full circle dwarfs the actual
        cut. Measuring only the swept extent keeps those glyphs' footprints
        tight, while a full-revolution arc (a drill hole's four quarter
        arcs) still yields its whole circle.
        """
        return arc_swept_bounds(
            float(self.cx), float(self.cy), float(self.radius), self.start_angle, self.sweep_angle
        )


def _collect_hpgl_geometry(
    content: str,
) -> Tuple[List[Tuple[int, int]], List[ArcExtent]]:
    """Extract all point coordinates and arc definitions from HPGL content.

    Parses ``PA``/``PU``/``PD`` coordinate pairs and ``AA`` (arc absolute)
    commands. Arcs are returned as :class:`ArcExtent` records whose radius is
    the distance from the arc center to the current pen position at the time
    of the command, together with the start angle and signed sweep needed to
    measure the *swept* extent (see :meth:`ArcExtent.swept_bounds`).

    Args:
        content: Raw HPGL text content.

    Returns:
        Tuple of ``(points, arcs)`` where ``points`` is a list of ``(x, y)``
        pairs and ``arcs`` is a list of :class:`ArcExtent` records.
    """
    points: List[Tuple[int, int]] = []
    arcs: List[ArcExtent] = []

    current_x = 0
    current_y = 0

    # A command token starts with two letters; capture the mnemonic and the
    # parameter string separately so AA parameters (which include a trailing
    # angle) are not mistaken for coordinate pairs. The angle may carry a
    # decimal point (font glyph sweeps are stored to 3 decimals).
    for match in re.finditer(r"(PA|PU|PD|AA)([\d,\.\-]+)", content):
        cmd = match.group(1)
        parts = [p for p in match.group(2).split(",") if p != ""]

        if cmd == "AA":
            # AA takes (center_x, center_y, angle); radius is implicit from
            # the current pen position.
            if len(parts) >= 3:
                try:
                    cx, cy = int(float(parts[0])), int(float(parts[1]))
                    sweep = float(parts[2])
                except ValueError:  # pragma: no cover - malformed numeric token
                    continue
                radius = math.hypot(current_x - cx, current_y - cy)
                start_angle = math.degrees(math.atan2(current_y - cy, current_x - cx))
                arcs.append(
                    ArcExtent(
                        cx=cx,
                        cy=cy,
                        radius=int(round(radius)),
                        start_angle=start_angle,
                        sweep_angle=sweep,
                    )
                )
                # An arc ends on the circle; without an exact end coordinate
                # we conservatively keep the pen position (the following PU
                # re-establishes it in generated content).
            continue

        try:
            values = [int(float(p)) for p in parts]
        except ValueError:  # pragma: no cover - malformed numeric token
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
            # Coordinates follow the integer plotter-unit contract, but the
            # sweep keeps its decimals (font glyph sweeps carry 3): huge
            # radius arcs make even half a degree of rounding a visible
            # positional error. Integral sweeps (drill holes) emit bare
            # integers, keeping hole output bit-identical.
            angle_text = str(int(angle)) if angle == int(angle) else f"{angle:.3f}"
            return f"AA{int(round(cx))},{int(round(cy))},{angle_text}"

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
    x_coords: list[float] = []
    y_coords: list[float] = []

    points, arcs = _collect_hpgl_geometry(plt_content)

    for x, y in points:
        x_coords.append(x)
        y_coords.append(y)

    # Arcs contribute their *swept* extent (not the full circle) so
    # huge-radius best-fit glyph arcs measure tight while drill holes
    # (four quarter arcs) still cover their whole circle.
    for arc in arcs:
        x_min_a, y_min_a, x_max_a, y_max_a = arc.swept_bounds()
        x_coords.extend((x_min_a, x_max_a))
        y_coords.extend((y_min_a, y_max_a))

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
    text_blocks: Optional[Mapping[int, Sequence[TextBlock]]] = None,
) -> None:
    """Export vpype Document to PLT for a single label.

    Each label is rendered independently. Instead of using vpype's write_hpgl()
    which applies complex coordinate transformations, we manually generate HPGL
    commands from the LineCollection to preserve coordinate fidelity.

    Process:
    1. Extract coordinates directly from vpype LineCollection (units are inches)
       and convert them losslessly to plotter units at 1:1000 scale. Arc-native
       text layers arrive as :class:`TextBlock` tuples (``text_blocks``) and
       emit their ``AA`` arcs verbatim.
    2. Center the text pen(s) vertically within label bounds (one shared
       delta across every text pen so multi-cutter text blocks stay aligned).
    3. Invert the Y-axis to device convention for upright display.

    No uniform scaling is applied because ``_linecollection_to_hpgl`` already
    produces coordinates at nominal scale; rescaling would distort footprints.

    Args:
        doc: The vpype Document to export (polylines only: boundary).
        output_path: Destination PLT file (or its parent when synthetic).
        label: The label being rendered (used to get expected dimensions).
        text_pens: Pen numbers carrying text geometry (one per cutter pen
            when a pen map is in use). Defaults to the historical single
            text pen (``SP1``). Boundary (``SP2``) and hole (``SP3``) pens
            are never centered.
        text_blocks: Optional mapping of text pen number to the positioned
            arc-native blocks emitted on that pen (see
            :func:`_render_text_lines_by_pen`). When supplied, those pens
            emit native PU/PD/AA instead of vpype layers.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Manually generate HPGL from the boundary polylines and the arc-native
    # text blocks. Drill holes are emitted as native HPGL arcs (pen 3)
    # alongside them.
    holes_hpgl = _render_holes_hpgl(label)
    hpgl_content = _linecollection_to_hpgl(doc, holes_hpgl=holes_hpgl, text_blocks=text_blocks)

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


def _linecollection_to_hpgl(
    doc: vp.Document,
    holes_hpgl: str = "",
    text_blocks: Optional[Mapping[int, Sequence[TextBlock]]] = None,
) -> str:
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
        text_blocks: Optional mapping of text pen number to arc-native
            :class:`TextBlock` tuples (see :func:`_render_text_lines_by_pen`).
            Those pens emit ``PU``/``PD``/``AA`` directly (arcs preserved,
            polyline strokes bit-identical to the historical
            ``PU{first};PD{rest}`` batching); pens present in BOTH this map
            and ``doc`` are emitted from the map only.

    Returns:
        Raw HPGL/PLT content as a string.
    """
    lines = [
        "IN",  # Initialize
        "PA",  # Header terminator (EngraveLab reference framing)
    ]

    # Track if we've added any content to know if we need footer
    has_content = False
    skipped_first_pu0_0 = False  # Track if we've skipped the initial PU0,0

    block_pens = {pen for pen, blocks in (text_blocks or {}).items() if blocks}

    # Process every pen carrying geometry, in ascending pen order: arc-native
    # text blocks (pen 1 and per-cutter pens 4+, see build_cutter_pen_map) and
    # polyline document layers (2 the boundary); drill holes are emitted
    # separately as native arcs under SP3.
    for pen_num in sorted(set(doc.layers) | block_pens):
        blocks = text_blocks.get(pen_num) if text_blocks is not None else None
        if blocks:
            # Arc-native text layer: every stroke emits PU-led (PU/PD/AA), so
            # the layer never starts with a bare PD and the origin-skip
            # optimization below is a boundary-rectangle concern only.
            if not any(not stroke.is_empty for block in blocks for stroke in block.strokes):
                continue
            has_content = True
            lines.append(f"SP{pen_num}")
            for block in blocks:
                for stroke in block.strokes:
                    if stroke.is_empty:
                        continue
                    lines.append(stroke.to_hpgl())
            continue

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
        lines.append("SP")

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

    def _section_y_coords(section: str) -> List[int]:
        """Collect every Y extent in one pen section (points + swept arcs).

        Arc (``AA``) commands contribute their *swept* Y extent so
        arc-bearing text (PLT-extracted fonts) centers on the real cut
        rather than on chord vertices.
        """
        section_points, section_arcs = _collect_hpgl_geometry(section)
        ys: List[int] = [y for _x, y in section_points]
        for arc in section_arcs:
            _x_min, y_min, _x_max, y_max = arc.swept_bounds()
            ys.append(int(math.floor(y_min)))
            ys.append(int(math.ceil(y_max)))
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
    v_margin_units = label.v_margin * 1000.0
    label_height_units = label.height * 1000.0
    available_height_units = label_height_units - (2 * v_margin_units)
    expected_center_y = v_margin_units + available_height_units / 2.0

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
        """Shift the Y coordinates of one coordinate or arc command.

        ``PA``/``PU``/``PD`` carry alternating X,Y pairs (odd indices shift).
        ``AA`` carries ``(center_x, center_y, sweep)``: the center Y shifts,
        the center X and the (possibly decimal) sweep are preserved verbatim
        so arc geometry stays exact.
        """
        cmd = coord_match.group(1)
        coord_parts = coord_match.group(2).split(",")

        try:
            adjusted_parts = []
            for i, part in enumerate(coord_parts):
                if cmd == "AA" and i >= 2:  # sweep angle: never shifted
                    adjusted_parts.append(part)
                    continue
                val = int(float(part))
                if i % 2 == 1:  # Y coordinate (odd index)
                    adjusted_val = int(round(val + y_adjustment))
                    adjusted_parts.append(str(adjusted_val))
                else:  # X coordinate
                    adjusted_parts.append(part)
            return f"{cmd}{','.join(adjusted_parts)}"
        except (ValueError, IndexError):
            return coord_match.group(0)

    # Apply the shared adjustment to every text pen section (points + arcs).
    coord_pattern_in_pen = r"(PA|PU|PD|AA)([\d,\.\-]+)"
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

    # Extract ALL coordinates and arcs across every layer. Arc extents are
    # included (swept, not full circle) so the mirror centerline accounts for
    # the real cut, keeping holes and glyph arcs aligned with text and borders.
    points, arcs = _collect_hpgl_geometry(content)
    all_y: list[int] = [y for _x, y in points]
    for arc in arcs:
        _x_min_a, y_min_a, _x_max_a, y_max_a = arc.swept_bounds()
        all_y.extend((int(math.floor(y_min_a)), int(math.ceil(y_max_a))))

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
    line_block: TextBlock,
    available_width: float,
    max_h_compress: float,
    label_id: str,
    line_text: Optional[str] = None,
) -> TextBlock:
    """Uniformly compress a rendered text line horizontally to fit the margin box.

    Margin precedence for width: when the rendered line is wider than the
    label's inner content area (``available_width``), every X coordinate is
    scaled toward the line's left edge by a uniform factor (Y is untouched),
    so glyphs, kerning, and inter-character spacing all compress together
    (uniform line compression). The scale is bounded by ``max_h_compress``
    via :func:`compute_horizontal_scale`; lines that already fit, or labels
    where compression is disabled (``max_h_compress == 0.0``), are returned
    unchanged.

    Arcs cannot survive the non-uniform X scale, so a compressed line's
    arcs are flattened to polylines (bounded chord error, see
    :meth:`TextBlock.compress_x`) with a WARNING naming the label/line.

    Args:
        line_block: The rendered text block for a single text line.
        available_width: Inner content width in inches (label width minus
            both margins).
        max_h_compress: Maximum compression fraction in ``[0.0, 1.0]``.
        label_id: Identifier used in log messages.
        line_text: Optional rendered text of the line, included in log
            messages so warnings identify the offending line.

    Returns:
        The original block when no compression is needed or allowed,
        otherwise a new horizontally compressed block.
    """
    bounds = line_block.bounds()
    if bounds is None:
        return line_block
    min_x, _min_y, max_x, _max_y = bounds
    rendered_width = max_x - min_x

    scale = compute_horizontal_scale(rendered_width, available_width, max_h_compress)
    if scale >= 1.0:
        return line_block

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
    context = f"of label {label_id}{line_desc}" if line_desc else f"of label {label_id}"
    return line_block.compress_x(scale, context=context)


def _apply_collision_compress(line_block: TextBlock, scale: float, context: str = "") -> TextBlock:
    """Uniformly scale a rendered line horizontally by a collision-avoidance factor.

    Unlike :func:`compress_line_to_width` (which only engages when a line
    overflows the inner content area), this applies an unconditional uniform
    X scale produced by the text-hole collision resolution sweep. Y is
    untouched and the line's left edge is kept fixed; the caller re-aligns
    the compressed line afterwards. Arcs on a compressed line are flattened
    (bounded chord error) with a WARNING.

    Args:
        line_block: The rendered text block for a single text line.
        scale: Uniform horizontal scale in ``(0.0, 1.0]``. ``1.0`` returns
            the block unchanged.
        context: Optional description for the arc-flattening WARNING.

    Returns:
        The original block when ``scale >= 1.0`` or bounds are
        unavailable, otherwise a new horizontally scaled block.
    """
    if scale >= 1.0:
        return line_block
    return line_block.compress_x(scale, context=context)


def _validated_glyph_groups(
    pairs: Optional[Sequence[Tuple[str, Sequence[int]]]],
    stroke_count: int,
) -> Tuple[Tuple[int, ...], ...]:
    """Validate a per-glyph stroke partition against the rendered stroke count.

    The partition is usable only when every stroke belongs to exactly one
    glyph group (the union of the groups is exactly ``range(stroke_count)``).
    Compression and translation preserve stroke counts 1:1, so a partition
    valid on the raw render stays valid on the positioned block.

    Args:
        pairs: ``(char, stroke_indices)`` pairs in text order, or ``None``.
        stroke_count: Number of strokes in the rendered block.

    Returns:
        One index tuple per glyph, in text order. Empty tuple when the
        partition is missing or does not cover every stroke exactly once.
    """
    if not pairs:
        return ()
    flat = [index for _char, indices in pairs for index in indices]
    if sorted(flat) != list(range(stroke_count)):
        return ()
    return tuple(tuple(indices) for _char, indices in pairs)


def _word_local_glyph_groups(
    glyph_groups: Tuple[Tuple[int, ...], ...],
    word_indices: Sequence[int],
) -> Tuple[Tuple[int, ...], ...]:
    """Re-express line-level glyph groups in one word's stroke frame.

    Glyph groups never cross a word boundary (the renderers build words
    contiguously), so a glyph belongs to the word owning its indices; the
    indices are remapped into the word's local stroke order.

    Args:
        glyph_groups: Line-level glyph groups (indices into the line's
            strokes).
        word_indices: The word's stroke indices, in stroke order.

    Returns:
        Glyph groups with indices into ``word_indices`` (the word block's
        stroke order). Glyphs with no index in this word are skipped.
    """
    local_of = {index: pos for pos, index in enumerate(word_indices)}
    groups: List[Tuple[int, ...]] = []
    for indices in glyph_groups:
        local = [local_of[i] for i in indices if i in local_of]
        if local:
            groups.append(tuple(local))
    return tuple(groups)


def _render_line_block(
    line: ResolvedTextLine,
    chunk_mode: TextChunkMode,
    *,
    check_glyph_coverage: bool = True,
) -> Tuple[
    TextBlock,
    Optional[List[Tuple[str, List[int]]]],
    Tuple[Tuple[int, ...], ...],
]:
    """Render one resolved text line to an arc-native :class:`TextBlock`.

    Dispatches on the line's cascaded ``font``:

    - ``kind == "plt"``: PLT-extracted fonts render through
      :func:`~plt_optimizer.generate.plt_font_renderer.render_text_line_plt_font_with_words`;
      arcs stay arcs end-to-end. Word groups index the block's strokes and
      are exact by construction.
    - ``kind == "ttf"``: TrueType fonts render through the ftext (matplotlib)
      path; the resulting LineCollection is adapted losslessly (it is
      polyline-only anyway) via :func:`block_from_linecollection`. ftext word
      groups index contours, which map 1:1 onto the adapted block's strokes.

    Args:
        line: The resolved text line (font, height, cutter, spacing).
        chunk_mode: When ``WORD``, request per-word stroke groups.
        check_glyph_coverage: When True (the default), a TrueType font missing
            a glyph raises :class:`FtextRenderError`. The font-showcase tool
            sets it False to render ``.notdef`` boxes as coverage info.

    Returns:
        ``(block, word_groups, glyph_groups)`` where ``word_groups`` is
        ``None`` in line mode (or when word grouping is unavailable) and
        otherwise lists one ``(word_text, stroke_indices)`` pair per
        whitespace-delimited segment, and ``glyph_groups`` lists one stroke-
        index tuple per rendered character in text order (empty tuple when
        the per-character partition is unavailable). Glyph groups are
        requested in both chunk modes: they drive the plate-space intra-chunk
        glyph direction sweep, which is independent of chunk granularity.

    Raises:
        PltFontRenderError: If a PLT font lacks a glyph (propagated from the
            PLT renderer; unknown font names are reported as
            :class:`PltFontRenderError` too).
        FtextRenderError: If a TrueType font lacks a glyph (propagated from
            the ftext renderer).
    """
    try:
        ref = resolve_font(line.font)
    except FontNotFoundError as exc:
        raise PltFontRenderError(str(exc)) from exc

    # The rendered geometry always comes from the historical renderer call
    # (bit-identical output), and the per-glyph partition is recovered from
    # the same (text, font, metrics) inputs: the PLT walk is deterministic and
    # its glyph groups are exact by construction, and the TTF partition slices
    # the whole-line contours by cached per-glyph contour counts. Both are
    # validated against the rendered stroke count, so a stubbed/mismatched
    # renderer simply yields no glyph groups (no intra-chunk sweep).
    plt_kwargs: dict[str, Any] = {
        "target_height_inches": line.toolpath_text_height,
        "font_name": ref.name,
        "cutter_diameter": line.cutter_diameter,
        "character_spacing": line.character_spacing,
        "space_width_fraction": line.space_width_fraction,
        "min_glyph_width": line.min_glyph_width,
        "kerning_window_fraction": line.kerning_window_fraction,
        "kerning_penetration_scale": line.kerning_penetration_scale,
        "kerning_recession_scale": line.kerning_recession_scale,
        "kerning_min_gap": line.kerning_min_gap,
        "fallback_advance_fraction": line.fallback_advance_fraction,
    }

    if ref.kind == "plt":
        if chunk_mode is TextChunkMode.WORD:
            block, groups = render_text_line_plt_font_with_words(line.text, **plt_kwargs)
            _walk, _word_pairs, glyph_pairs = render_text_line_plt_font_with_glyphs(
                line.text, **plt_kwargs
            )
            return (
                block,
                (groups or None),
                _validated_glyph_groups(glyph_pairs, len(block.strokes)),
            )
        block = render_text_line_plt_font(line.text, **plt_kwargs)
        _walk, _word_pairs, glyph_pairs = render_text_line_plt_font_with_glyphs(
            line.text, **plt_kwargs
        )
        return block, None, _validated_glyph_groups(glyph_pairs, len(block.strokes))

    # TrueType family: the historical ftext path, adapted to TextBlock.
    if chunk_mode is TextChunkMode.WORD:
        filtered_lc, groups = render_text_line_ftext_with_words(
            line.text,
            target_height_inches=line.toolpath_text_height,
            font_path=ref.path,
            check_glyph_coverage=check_glyph_coverage,
        )
        block = block_from_linecollection(filtered_lc)
        # Contour indices map 1:1 onto strokes only when every contour
        # survived adaptation; otherwise fall back to whole-line chunking.
        if groups and len(block.strokes) == len(filtered_lc):
            pairs = glyph_groups_for_line(line.text, ref.path, len(filtered_lc))
            return block, groups, _validated_glyph_groups(pairs, len(block.strokes))
        return block, None, ()
    filtered_lc = render_text_line_ftext(
        line.text,
        target_height_inches=line.toolpath_text_height,
        font_path=ref.path,
        check_glyph_coverage=check_glyph_coverage,
    )
    block = block_from_linecollection(filtered_lc)
    # Glyph groups slice the whole-line contours by per-glyph contour
    # counts; valid only while every contour survived adaptation (1:1).
    if len(block.strokes) == len(filtered_lc):
        pairs = glyph_groups_for_line(line.text, ref.path, len(filtered_lc))
        return block, None, _validated_glyph_groups(pairs, len(block.strokes))
    return block, None, ()


def _render_positioned_lines(
    label: ResolvedLabel,
    chunk_mode: TextChunkMode = TextChunkMode.LINE,
    *,
    check_glyph_coverage: bool = True,
) -> List[
    Tuple[
        int,
        TextBlock,
        _LineEntry,
        Optional[List[Tuple[str, List[int]]]],
        Tuple[Tuple[int, ...], ...],
    ]
]:
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
    right margin, "center" centers it). Each line renders in its cascaded
    ``font``: TrueType fonts through ftext (matplotlib), PLT-extracted fonts
    through the arc-native glyph walker (see :func:`_render_line_block`).

    When per-line compression is active (set by the text-hole collision
    resolution sweep), only the identified colliding lines are horizontally
    scaled by their per-line compression factor (non-colliding lines remain
    at full width) before alignment. Compression flattens arcs (bounded
    chord error) with a WARNING -- native arcs and compression are mutually
    exclusive.

    Args:
        label: The resolved label whose ``content`` should be rendered.
        chunk_mode: When ``WORD``, each line is additionally partitioned into
            whitespace-delimited word groups (stroke indices into the
            rendered block, exact by construction). ``LINE`` (the default)
            reports no word groups.
        check_glyph_coverage: When True (the default), a TrueType font missing
            a glyph fails the render (see :func:`_render_line_block`).

    Returns:
        One ``(line_index, positioned_block, entry, word_groups,
        glyph_groups)`` tuple per renderable line, in content order, where
        ``positioned_block`` is the positioned :class:`TextBlock`, ``entry``
        is its :class:`_LineEntry` record (line index, text, bounds, and the
        effective horizontal compression scale) in label-local coordinates
        (pre-export anchor, block centered around y=0; bounds are
        swept-analytic), ``word_groups`` is ``None`` in line mode or a list
        of ``(word_text, stroke_indices)`` pairs indexing
        ``positioned_block`` strokes in word order, and ``glyph_groups``
        lists one stroke-index tuple per rendered character (indices into
        ``positioned_block`` strokes; empty when unavailable). The export
        pipeline vertically centers the block at ``height / 2``; collision
        detection applies that shift itself.

    Raises:
        LabelRenderError: If a line's font cannot render its text (missing
            glyph, unknown font), naming the label and line.
    """
    if not label.content:
        return []

    h_margin = label.h_margin
    v_margin = label.v_margin
    inner_width = label.width

    # First pass: render all lines and measure their heights. Keep each
    # line's own line_spacing alongside the rendered geometry so empty or
    # unrenderable lines don't misalign spacing between real lines. The
    # original ``content`` index is preserved so collision reports can name
    # the offending line even when earlier lines were unrenderable.
    rendered_lines: list[
        Tuple[
            int,
            TextBlock,
            float,
            float,
            float,
            str,
            Optional[List[Tuple[str, List[int]]]],
            float,
            Tuple[Tuple[int, ...], ...],
        ]
    ] = []
    total_rendered_height = 0.0

    for line_index, line in enumerate(label.content):
        # Render at the toolpath_text_height (cutter-compensated) in the
        # line's cascaded font. Both renderers return upright glyphs.
        try:
            block, word_groups, glyph_groups = _render_line_block(
                line, chunk_mode, check_glyph_coverage=check_glyph_coverage
            )
        except (PltFontRenderError, FtextRenderError) as exc:
            raise LabelRenderError(
                f"Label {label.id}: text line {line_index} ({line.text!r}) "
                f"cannot be rendered: {exc}"
            ) from exc

        if block.is_empty():
            continue

        bounds = block.bounds()
        if bounds is None:  # pragma: no cover - is_empty covers this in practice
            continue
        _min_x, min_y, _max_x, max_y = bounds
        rendered_height = max_y - min_y

        # Collision-avoidance compression (Phase 3): per-line horizontal scale
        # for lines that are colliding with holes. Only the identified colliding
        # lines get compressed; non-colliding lines remain at full width.
        compression_scale = label.collision_compress_by_line.get(line_index, 1.0)
        line_desc = f"of label {label.id} line {line_index} ({line.text!r})"
        block = _apply_collision_compress(block, compression_scale, context=line_desc)
        bounds = block.bounds()
        if bounds is None:  # pragma: no cover - measured just above
            continue
        rendered_height = bounds[3] - bounds[1]

        rendered_lines.append(
            (
                line_index,
                block,
                rendered_height,
                line.line_spacing,
                line.max_h_compress,
                line.text_h_alignment,
                word_groups,
                compression_scale,
                glyph_groups,
            )
        )
        total_rendered_height += rendered_height

    if not rendered_lines:
        return []

    # Add line spacing between lines (not after the last line). Margin
    # precedence: if the measured block (line heights + requested spacing)
    # overflows the inner area, shrink the spacing so margins win.
    spacings = [
        line_spacing
        for _idx, _lc, _height, line_spacing, _mhc, _align, _wg, _cs, _gg in rendered_lines[:-1]
    ]
    # Vertical margin applies as-is; no cutter compensation needed since the
    # margin is user-specified and text already accounts for per-line cutter diameter
    # via horizontal compensation.
    available_height = label.height - (2 * v_margin)
    adjusted_spacings = fit_line_spacing_to_margins(
        [height for _idx, _lc, height, _spacing, _mhc, _align, _wg, _cs, _gg in rendered_lines],
        spacings,
        available_height,
    )
    if adjusted_spacings != spacings:
        logger.warning(
            "Label %s: line_spacing reduced at render time from %s to %s "
            "to preserve v_margin %.3fin.",
            label.id,
            [round(s, 4) for s in spacings],
            [round(s, 4) for s in adjusted_spacings],
            v_margin,
        )
    total_rendered_height = sum(
        height for _idx, _lc, height, _spacing, _mhc, _align, _wg, _cs, _gg in rendered_lines
    ) + sum(adjusted_spacings)

    # Anchor the block so its vertical center sits at y = total / 2. The
    # absolute anchor is irrelevant (post-export centering fixes it); only
    # the relative stacking matters.
    current_y = total_rendered_height / 2.0

    # Second pass: position each line, stacked top-to-bottom (+y up).
    positioned: List[
        Tuple[
            int,
            TextBlock,
            _LineEntry,
            Optional[List[Tuple[str, List[int]]]],
            Tuple[Tuple[int, ...], ...],
        ]
    ] = []
    for i, (
        line_index,
        block,
        rendered_height,
        _line_spacing,
        max_h_compress,
        text_h_alignment,
        word_groups,
        collision_scale,
        glyph_groups,
    ) in enumerate(rendered_lines):
        bounds = block.bounds()
        if bounds is None:  # pragma: no cover - measured in first pass
            continue
        line = label.content[line_index]
        pre_margin_width = bounds[2] - bounds[0]

        # Margin precedence for width: compress over-wide lines so they
        # respect the inner content area (bounded by max_h_compress).
        # For alignment: use the user-specified h_margin (visual margin).
        # For compression: include half the line's cutter diameter so all text
        # has consistent visual margin from the border.
        cutter_margin_h = h_margin + (line.cutter_diameter / 2.0)
        compression_available_width = inner_width - (2 * cutter_margin_h)
        alignment_available_width = inner_width - (2 * h_margin)

        # Per-label shared compression (``h_compress_global``): the export
        # pre-pass measured this line's group scale. The effective scale is
        # ``collision x margin``, so the shared scale becomes a width target
        # for the margin-compression step below: ``margin = min(natural,
        # shared / collision)``. A line whose natural compression is already
        # tighter keeps it (never stretched back), and the pre-pass clamps the
        # shared scale to this line's own ``1 - max_h_compress`` floor, so the
        # budget is respected by construction.
        global_scale = label.global_compress_by_line.get(line_index, 1.0)
        if global_scale < 1.0 and collision_scale > 0.0:
            compression_available_width = min(
                compression_available_width,
                (global_scale / collision_scale) * pre_margin_width,
            )
        block = compress_line_to_width(
            block,
            compression_available_width,
            max_h_compress,
            label.id,
            line_text=line.text,
        )
        bounds = block.bounds()
        if bounds is None:  # pragma: no cover - measured in first pass
            continue
        min_x, _min_y, max_x, max_y = bounds
        rendered_width = max_x - min_x

        # Effective per-line horizontal scale, recorded for reporting:
        # the collision-avoidance scale (applied in the first pass) times
        # the margin-overflow scale measured here. compress_x scales X only,
        # so the width ratio is exactly the applied margin scale (1.0 when
        # the line already fit or compression was disabled).
        margin_scale = rendered_width / pre_margin_width if pre_margin_width > 0.0 else 1.0
        effective_scale = collision_scale * margin_scale

        # Horizontal alignment within the available width: "left" anchors
        # the line's left-most point at the left margin, "right" anchors
        # the right-most point at the right margin, "center" centers it.
        target_left_x = compute_horizontal_offset(
            rendered_width, alignment_available_width, h_margin, text_h_alignment
        )
        x_offset = target_left_x - min_x

        # Vertical stacking: top of this line's glyphs at current_y.
        y_offset = current_y - max_y
        block = block.translate(x_offset, y_offset)

        # Translation is additive, so the positioned bounds are the measured
        # bounds shifted; no re-measurement is needed.
        positioned.append(
            (
                line_index,
                block,
                _LineEntry(
                    line_index=line_index,
                    line_text=label.content[line_index].text,
                    bounds=(
                        min_x + x_offset,
                        bounds[1] + y_offset,
                        max_x + x_offset,
                        max_y + y_offset,
                    ),
                    compression_scale=effective_scale,
                    line_spacing=adjusted_spacings[i] if i < len(adjusted_spacings) else None,
                ),
                word_groups,
                glyph_groups,
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
    *,
    check_glyph_coverage: bool = True,
) -> Tuple[vp.LineCollection, List[_LineEntry]]:
    """Render text at local coordinates and report per-line bounds.

    Thin wrapper over :func:`_render_positioned_lines` returning the
    combined polyline view (arcs flattened; all lines on one collection)
    plus the per-line bounds records used for collision detection.

    Args:
        label: The resolved label whose ``content`` should be rendered.
        check_glyph_coverage: When True (the default), a TrueType font
            missing a glyph fails the render.

    Returns:
        A tuple ``(combined_lc, line_entries)`` where ``combined_lc`` holds
        all rendered lines and ``line_entries`` is a list of :class:`_LineEntry`
        records in label-local coordinates (pre-export anchor, block centered
        around y=0). The export pipeline vertically centers the block at
        ``height / 2``; collision detection applies that shift itself.
    """
    text_lc = vp.LineCollection()
    line_entries: List[_LineEntry] = []
    for _line_index, positioned_block, entry, _wg, _gg in _render_positioned_lines(
        label, check_glyph_coverage=check_glyph_coverage
    ):
        text_lc.extend(positioned_block.to_vpype_polylines())
        line_entries.append(entry)
    return text_lc, line_entries


def _render_text_lines_by_pen(
    label: ResolvedLabel,
    pen_map: Optional[dict[tuple[float, str], int]] = None,
    chunk_mode: TextChunkMode = TextChunkMode.LINE,
    *,
    check_glyph_coverage: bool = True,
) -> Tuple[dict[int, Tuple[TextBlock, ...]], List[_LineEntry], List[TextChunkRecord]]:
    """Render text lines grouped onto per-cutter pen layers.

    Each positioned line's arc-native :class:`TextBlock` is appended to the
    block tuple of its ``(cutter, text_color)`` layer's pen (see
    :func:`plt_optimizer.generate.resolution.build_cutter_pen_map`). Lines
    whose layer is absent from ``pen_map`` (or when no map is supplied)
    fall back to the historical text pen (``SP1``), preserving
    back-compatible single-pen output.

    Args:
        label: The resolved label whose ``content`` should be rendered.
        pen_map: Optional mapping of ``(cutter diameter, text color)`` to
            pen number.
        chunk_mode: Granularity of the returned chunk records (see
            :func:`_render_positioned_lines`).
        check_glyph_coverage: When True (the default), a TrueType font
            missing a glyph fails the render.

    Returns:
        Tuple of ``(pens, line_entries, chunk_records)`` where ``pens`` maps
        pen number to the tuple of positioned blocks for that pen (only
        non-empty pens included), ``line_entries`` is the same per-line
        bounds record list returned by
        :func:`_render_text_local_with_bounds`, and ``chunk_records`` holds
        one :class:`TextChunkRecord` per chunk (per line in ``LINE`` mode;
        per word plus any ungrouped line in ``WORD`` mode) in label-local
        coordinates for plate-space optimization.
    """
    pens: dict[int, List[TextBlock]] = {}
    line_entries: List[_LineEntry] = []
    chunk_records: List[TextChunkRecord] = []
    for line_index, positioned_block, entry, word_groups, glyph_groups in _render_positioned_lines(
        label, chunk_mode=chunk_mode, check_glyph_coverage=check_glyph_coverage
    ):
        pen = LAYER_TEXT
        if pen_map is not None and 0 <= line_index < len(label.content):
            line_content = label.content[line_index]
            pen = pen_map.get((line_content.cutter_diameter, line_content.text_color), LAYER_TEXT)
        pens.setdefault(pen, []).append(positioned_block)
        line_entries.append(entry)

        if word_groups is not None:
            for word_index, (word_text, indices) in enumerate(word_groups):
                if not indices:
                    continue  # blank segment: no strokes to route
                word_block = TextBlock(strokes=tuple(positioned_block.strokes[i] for i in indices))
                word_bounds = word_block.bounds()
                chunk_records.append(
                    TextChunkRecord(
                        line_index=line_index,
                        word_index=word_index,
                        word_text=word_text,
                        pen=pen,
                        blocks=(word_block,),
                        bounds=word_bounds if word_bounds is not None else entry[2],
                        glyph_groups=_word_local_glyph_groups(glyph_groups, indices),
                    )
                )
        else:
            chunk_records.append(
                TextChunkRecord(
                    line_index=line_index,
                    word_index=None,
                    word_text="",
                    pen=pen,
                    blocks=(positioned_block,),
                    bounds=entry[2],
                    glyph_groups=glyph_groups,
                )
            )
    return (
        {
            pen: tuple(blocks)
            for pen, blocks in pens.items()
            if any(not b.is_empty() for b in blocks)
        },
        line_entries,
        chunk_records,
    )


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
    half the hole cutter diameter + radius``, so the closest point of the
    circle's stroke sits exactly ``label.hole_margin`` inches from the label
    boundary. This ensures consistent spacing regardless of hole cutter size.

    Args:
        label: The resolved label whose ``holes`` should be measured.

    Returns:
        One ``(center_x, center_y, radius)`` tuple per hole, in inches.
    """
    circles: List[Tuple[float, float, float]] = []

    for hole in label.holes:
        radius = hole.diameter / 2.0
        # Distance from the edge to the hole center: the requested hole
        # margin plus half the hole cutter diameter (for visual consistency)
        # plus the radius, so the circle's stroke edge sits exactly
        # hole_margin inches away from the edge.
        offset = label.hole_margin + (label.hole_cutter_diameter / 2.0) + radius
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
