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
from typing import Any, Callable, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple

import numpy as np
import vpype as vp

from plt_optimizer.core.units import HPGL_UNITS_PER_INCH, inches_to_units, units_to_inches
from plt_optimizer.generate.descender_adjust import (
    DescenderFix,
    GlyphBox,
    GlyphGap,
    apply_ramp,
    colliding_partners,
    measure_glyph_gaps,
    merge_ramps,
    ramp_for,
    ramp_for_measured,
    ramp_ink,
    ramps_changed,
    recip_ink,
    reciprocal_scale_y,
    required_gap,
    worst_glyph_deficit,
)
from plt_optimizer.generate.font_registry import (
    FontNotFoundError,
    FontRef,
    base_font_name,
    non_text_font_names,
    resolve_font,
)
from plt_optimizer.generate.ftext_renderer import (
    FtextRenderError,
    glyph_groups_for_line,
    render_text_line_ftext,
    render_text_line_ftext_with_words,
)
from plt_optimizer.generate.geometry import (
    CollisionResult,
    Ink,
    InkGrid,
    Point,
    Polyline,
    arc_swept_bounds,
    circle_aabb_gap,
    ink_gap,
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
    baseline_block_height,
    baseline_offsets,
    compute_horizontal_offset,
    compute_horizontal_scale,
    ellipse_content_inset,
    fit_baseline_spacing_to_margins,
    fit_line_spacing_to_margins,
    glyph_render_plan,
)
from plt_optimizer.generate.schema import LabelShape
from plt_optimizer.generate.text_geometry import (
    ARC_FLATTEN_CHORD_TOL_INCHES,
    LineSeg,
    Stroke,
    TextBlock,
    block_from_linecollection,
)
from plt_optimizer.generate.underline import runs_from_flags, underline_segments

logger = logging.getLogger(__name__)

# Layer assignments (pen 2/3 must match vectorize.py structural pens)
LAYER_TEXT: int = 1
LAYER_BOUNDARY: int = 2
LAYER_HOLES: int = 3


def _label_is_round(label: ResolvedLabel) -> bool:
    """True when the label's boundary is an ellipse (``type: round``).

    The single shape predicate the renderer uses to branch onto the ellipse
    path (boundary emission, hole placement, collision distance, radial
    content inset). Rectangular labels never enter it, so their geometry is
    bit-identical.

    Args:
        label: The resolved label.

    Returns:
        ``True`` for ``shape_type == "round"``.
    """
    return label.shape_type == LabelShape.ROUND.value


def _label_ellipse(
    label: ResolvedLabel,
) -> tuple[float, float, float, float]:
    """The label's elliptical boundary in the label-local y-up frame.

    The boundary rectangle is drawn at the label edge, so the ellipse is
    centred at ``(width / 2, height / 2)`` with semi-axes ``width / 2`` and
    ``height / 2`` -- the same frame the drill holes and text bounds use.

    Args:
        label: The resolved (round) label.

    Returns:
        ``(center_x, center_y, semi_a, semi_b)`` in inches.
    """
    return (label.width / 2.0, label.height / 2.0, label.width / 2.0, label.height / 2.0)


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

# Iteration cap for the descending-character relief loop. Each pass is
# monotone (every glyph ramp grows, every spacing grows), so the loop
# converges well inside this; the cap is a guard against float noise
# oscillating around the convergence tolerance.
_DESCENDER_MAX_PASSES: int = 8

# Gap comparison tolerance for the relief's abort gate: a gap exactly equal to
# the required clearance is safe (tangency). AGENTS.md forbids ``==`` on floats.
# 1e-6in is the emission floor (a thousandth of a plotter unit): the true 2D
# ink distance grows sub-linearly with the vertical spacing (a diagonal pair
# gains less than the spacing it grows by), so the fixpoint's finite passes
# leave a sub-plotter-unit residual that must never abort the job.
_DESCENDER_GAP_TOLERANCE: float = 1e-6

# Reporting radius for the relief's final gap_after measurement: the InkGrid
# cell (and therefore the cheap measurement radius) is at least this, so
# reported gaps never print as ``inf`` for glyphs that merely sit far apart.
_GAP_REPORT_RADIUS: float = 0.25

# Emission floor for the relief's ink model, in inches: half a plotter unit
# (coordinates emit as integer units, 1 inch = ``HPGL_UNITS_PER_INCH``). Glyph
# ink below this depth does not exist in the emitted PLT, so the relief must
# not treat it as a descender -- a sub-unit sliver is font-data noise, and
# chasing it relieves glyphs that do not descend and aborts the job on sub-unit
# deficits. Real descenders are two orders of magnitude deeper.
_INK_EMISSION_FLOOR: float = 0.5 / HPGL_UNITS_PER_INCH

# Sentinel glyph index for the bottom border's virtual glyph box (the border
# is not a rendered character, so it never collides with a real glyph index).
_BORDER_GLYPH_INDEX: int = -1

# Shortfall tolerance for the bottom-border report, in inches: one plotter
# unit. The border ramp converges asymptotically (relieving a glyph shrinks
# the block's bottom extent, which re-centres the block DOWNWARD and undoes
# part of the relief, so each pass closes only part of the deficit), leaving a
# sub-plotter-unit residual. Emitted coordinates are integer plotter units, so
# a shortfall that small is not a collision in the engraved output and must not
# be reported.
_BORDER_SHORTFALL_TOLERANCE: float = 2 * _INK_EMISSION_FLOOR

# Bisection granularity for the relief's spacing growth (the stacked block
# height is non-decreasing in the spacings, so the admissible growth factor is
# found by bisection -- the same shape as ``fit_baseline_spacing_to_margins``).
_SPACING_GROWTH_MAX_ITERATIONS: int = 100
_SPACING_GROWTH_TOLERANCE: float = 1e-9

# Doubling steps used to bracket the spacing growth that clears a gap's ink
# distance (the growth a laterally-offset pair needs is bounded by the
# required clearance, so a handful of doublings always reach it).
_SPACING_GROWTH_MAX_DOUBLINGS: int = 40


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
        stroke_cutters: The engraving cutter diameter of every stroke of this
            line, in stroke order (see :func:`_stroke_cutter_map`). The
            per-cutter pen split groups strokes by this array, so a ``<b>``
            bold run and each ``<scale:N>`` run land on their own tool's pen.
            Empty when the line has no markup (no per-stroke variation).
    """

    line_index: int
    line_text: str
    bounds: Tuple[float, float, float, float]
    compression_scale: float = 1.0
    line_spacing: Optional[float] = None
    stroke_cutters: Tuple[float, ...] = ()


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
        descender_adjustments_by_line: Mapping of line index to the
            descending-character reliefs applied to that line's glyphs by the
            automatic descending-character adjustment (vertical downscale +
            raise of the individual glyphs measured close to a glyph on the
            following line). Only lines with at least one relief are
            included, so the common case is an empty dict. Reporting-only:
            the geometry itself is already baked into ``plt_content`` /
            ``text_chunks``.
        band_center_offset_inches: Offset from the text block's ink center to
            its baseline-to-capline band center, in inches (``+y`` up, render
            frame; ``> 0`` when descenders hang below the lowest baseline).
            Non-zero only when the label centers by band
            (``ResolvedLabel.center_text_by_band``): the centering step lowers
            its target by this amount so the band, not the ink, lands on the
            inner-area center. Consumers that reproduce the vertical placement
            (text-hole collision detection, the plate-space text transform)
            must apply the same offset, so it travels with the render.
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
    descender_adjustments_by_line: dict[int, Tuple[DescenderFix, ...]] = field(default_factory=dict)
    band_center_offset_inches: float = 0.0


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
        line = label.content[line_index]
        # The widest tool the line uses: a ``<b>`` line's bold run engraves
        # thicker, a large ``<scale:N>`` run engraves with a larger tool, and
        # the whole-line AABB is measured against it.
        text_cutter = max(line.used_cutters)
    else:  # Defensive: unknown line (should not happen) assumes no stroke.
        text_cutter = 0.0
    stroke_floor = 0.5 * (label.hole_cutter_diameter + text_cutter)
    return stroke_floor + label.hole_text_collision_distance


def _detect_text_hole_collisions(
    label: ResolvedLabel,
    line_entries: Sequence[_LineEntry],
    band_center_offset_inches: float = 0.0,
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
    so bounds are shifted by that amount before the check. Band centering
    lowers that target by ``band_center_offset_inches`` (the band, not the ink,
    lands on the inner-area center), so the shift subtracts it. The export's
    Y-flip mirrors text and holes across the *same* centerline, which
    preserves circle/AABB intersection relationships exactly, so the check
    is valid in this pre-flip frame.

    Args:
        label: The resolved label providing hole positions, cutter
            diameters and the collision distance.
        line_entries: Per-line :class:`_LineEntry` records as returned by
            :func:`_render_text_local_with_bounds`.
        band_center_offset_inches: The band-center offset the renderer applied
            to these bounds (inches); ``0.0`` when the label centers by ink.

    Returns:
        One :class:`CollisionResult` per colliding (line, hole) pair.
        Empty when the label has no holes or no rendered lines.
    """
    if not label.holes or not line_entries:
        return []

    circles = _hole_circles_local(label)
    shift_y = label.height / 2.0 - band_center_offset_inches
    results: List[CollisionResult] = []

    for line_index, line_text, bounds, _scale, _spacing, _cutters in line_entries:
        threshold = _collision_threshold(label, line_index)
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
            # POSITIONS, which _hole_circles_local places on the ellipse for
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
    band_center_offset_inches: float = 0.0,
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
        if not _detect_text_hole_collisions(
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


def _resolve_collision_via_compression(
    label: ResolvedLabel,
    budget: float,
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
        line_desc: Optional description of the offending text lines,
            included in the resolution WARNING for informative output.

    Returns:
        A clone of ``label`` with ``collision_compress_by_line`` set to
        per-line scales when one clears all collisions, otherwise ``None``.
    """
    floor = max(0.0, 1.0 - min(budget, 1.0))

    # First pass: detect collisions and identify which lines are colliding
    _candidate_lc, candidate_entries = _render_text_local_with_bounds(label)
    sweep_collisions = _detect_text_hole_collisions(
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
        _candidate_lc, candidate_entries = _render_text_local_with_bounds(candidate_label)
        sweep_collisions = _detect_text_hole_collisions(
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
    correct and expressed in integer plotter units
    (``HPGL_UNITS_PER_INCH`` units = 1 inch).

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
    # The rendered bounds sit in the render frame; the centering step that
    # places them in the label lowers its target by this offset, so every
    # collision check against these bounds must subtract it too.
    band_offset = rendered.band_center_offset_inches
    collisions = _detect_text_hole_collisions(label, line_entries, band_offset)
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
            label, line_entries, line_desc=line_desc, band_center_offset_inches=band_offset
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
            collisions = _detect_text_hole_collisions(base_label, line_entries, band_offset)
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
            base_label, budget, line_desc=line_desc, band_center_offset_inches=band_offset
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
            final_collisions = _detect_text_hole_collisions(
                compressed_label, resolved_line_entries, resolved_rendered.band_center_offset_inches
            )
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
        final_band: List[float] = []
        _, final_entries = _render_text_local_with_bounds(
            final_label,
            check_glyph_coverage=check_glyph_coverage,
            band_center_offset=final_band,
        )
        collisions = _detect_text_hole_collisions(
            final_label,
            final_entries,
            final_band[0] if final_band else band_offset,
        )
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
    descender_fixes: List[DescenderFix] = []
    band_center_offset: List[float] = []
    text_pens, line_entries, chunk_records = _render_text_lines_by_pen(
        label,
        pen_map,
        chunk_mode=_chunk_mode_of(label),
        check_glyph_coverage=check_glyph_coverage,
        descender_fixes=descender_fixes,
        band_center_offset=band_center_offset,
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
            doc,
            temp_path,
            label,
            text_pens=set(text_pens),
            text_blocks=text_pens,
            band_center_offset_inches=band_center_offset[0] if band_center_offset else 0.0,
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

        # Per-line descending-character reliefs (vertical downscale + raise of
        # the glyphs measured close to the line below), reporting-only. Only
        # lines with at least one relief are included.
        descender_adjustments_by_line: dict[int, Tuple[DescenderFix, ...]] = {}
        for fix in descender_fixes:
            descender_adjustments_by_line.setdefault(fix.line_index, ())
            descender_adjustments_by_line[fix.line_index] = (
                *descender_adjustments_by_line[fix.line_index],
                fix,
            )

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
            descender_adjustments_by_line=descender_adjustments_by_line,
            band_center_offset_inches=band_center_offset[0] if band_center_offset else 0.0,
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
    (``HPGL_UNITS_PER_INCH`` units = 1 inch) to inches.

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

    # Convert from plotter units (HPGL_UNITS_PER_INCH units = 1 inch) to inches
    x_min = units_to_inches(min(x_coords))
    x_max = units_to_inches(max(x_coords))
    y_min = units_to_inches(min(y_coords))
    y_max = units_to_inches(max(y_coords))

    return x_min, y_min, x_max, y_max


def _export_to_plt_with_postprocessing(
    doc: vp.Document,
    output_path: Path,
    label: ResolvedLabel,
    text_pens: Optional[Iterable[int]] = None,
    text_blocks: Optional[Mapping[int, Sequence[TextBlock]]] = None,
    band_center_offset_inches: float = 0.0,
) -> None:
    """Export vpype Document to PLT for a single label.

    Each label is rendered independently. Instead of using vpype's write_hpgl()
    which applies complex coordinate transformations, we manually generate HPGL
    commands from the LineCollection to preserve coordinate fidelity.

    Process:
    1. Extract coordinates directly from vpype LineCollection (units are inches)
       and convert them losslessly to integer plotter units
       (``HPGL_UNITS_PER_INCH`` per inch). Arc-native
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
    # already converts vpype inches directly to plotter units (HPGL_UNITS_PER_INCH
    # units), so the boundary rectangle spans exactly [0,w]x[0,h]. Rescaling
    # against combined text+boundary bounds would compress geometry whenever
    # ftext glyph descenders extend below y=0, distorting label footprints and
    # causing overlapping borders after bin-packing.

    # Center the text pen(s) vertically within label bounds
    center_pens: frozenset[int] = (
        frozenset({LAYER_TEXT}) if text_pens is None else frozenset(text_pens)
    )
    _center_text_layer_vertically(
        output_path,
        label,
        text_pens=center_pens,
        band_center_offset_inches=band_center_offset_inches,
    )

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
    (``HPGL_UNITS_PER_INCH`` units = 1 inch).

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

            # Convert from inches to plotter units (device scale)
            points = []
            for point in segment:
                x_plotter = inches_to_units(point.real)
                y_plotter = inches_to_units(point.imag)
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
    band_center_offset_inches: float = 0.0,
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

    This is the **authoritative** vertical placement of the text: the renderer
    centers the block on ``y = 0`` in its own frame, and this function moves
    that block to its final position in the label, so any centering rule must
    be applied here (a rule applied only to the render frame is silently
    undone by this shift).

    Args:
        file_path: Path to the PLT file (after scaling).
        label: The label being rendered (for dimensions and margins).
        text_pens: Pen numbers carrying text geometry. Defaults to the
            historical single text pen (``SP1``).
        band_center_offset_inches: Offset from the ink center to the
            baseline-to-capline band center in the render frame (inches, ``> 0``
            when descenders hang below). When band centering is active the band
            -- not the ink -- must land on the inner-area center, so the target
            is lowered by this amount. ``0.0`` (the default) centers the ink,
            the historical behaviour.
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

    # Expected center position (in plotter units: HPGL_UNITS_PER_INCH per
    # inch). Band centering lowers the target by the ink-to-band offset so the
    # band, not the ink, lands on the inner-area center.
    v_margin_units = label.v_margin * HPGL_UNITS_PER_INCH
    label_height_units = label.height * HPGL_UNITS_PER_INCH
    available_height_units = label_height_units - (2 * v_margin_units)
    expected_center_y = v_margin_units + available_height_units / 2.0
    expected_center_y -= band_center_offset_inches * HPGL_UNITS_PER_INCH

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
    # Small caps is a font-variant property: the cascaded sc_scale reaches the
    # renderers only for a "<base> SC" font, so every other line keeps the
    # renderer defaults and renders bit-identically.
    if ref.small_caps:
        plt_kwargs["lowercase_scale"] = line.sc_scale
    # Inline <sc> markup: the same transform, selected per character. The gate
    # is empty on a SC font (the line is already small caps) and on a symbol
    # font, so it never collides with the font-level branch above.
    small_caps_gate = _small_caps_render_gate(line, ref)
    if small_caps_gate is not None:
        sc_scale_value, sc_flags = small_caps_gate
        plt_kwargs["lowercase_scale"] = sc_scale_value
        plt_kwargs["lowercase_flags"] = sc_flags

    # Inline markup: the renderers always receive the PLAIN text (tags never
    # engrave) plus the per-character italic selection. A line without markup
    # keeps the historical call shape (no italic kwargs at all) and renders
    # bit-identically; italic_angle 0 is upright by contract (the shear is
    # skipped, italic runs engrave like plain text).
    render_text = line.plain_text
    italic_kwargs = _italic_render_kwargs(line)
    # Bold / scale markup: the flagged characters render with a per-glyph
    # cutter diameter and a baseline-anchored uniform scale (PLT: per-character
    # cutter diameters + glyph scales; TTF: per-character glyph scales). Empty
    # dict keeps the historical call shape for every markup-free line.
    plt_kwargs.update(_glyph_render_kwargs(line, plt=True))

    if ref.kind == "plt":
        if chunk_mode is TextChunkMode.WORD:
            block, groups = render_text_line_plt_font_with_words(
                render_text, **plt_kwargs, **italic_kwargs
            )
            _walk, _word_pairs, glyph_pairs = render_text_line_plt_font_with_glyphs(
                render_text, **plt_kwargs, **italic_kwargs
            )
            return _apply_strikethrough(
                line,
                *_apply_underline(
                    line,
                    block,
                    (groups or None),
                    _validated_glyph_groups(glyph_pairs, len(block.strokes)),
                ),
            )
        block = render_text_line_plt_font(render_text, **plt_kwargs, **italic_kwargs)
        _walk, _word_pairs, glyph_pairs = render_text_line_plt_font_with_glyphs(
            render_text, **plt_kwargs, **italic_kwargs
        )
        return _apply_strikethrough(
            line,
            *_apply_underline(
                line, block, None, _validated_glyph_groups(glyph_pairs, len(block.strokes))
            ),
        )

    # TrueType family: the historical ftext path, adapted to TextBlock.
    ftext_kwargs: dict[str, Any] = {
        "target_height_inches": line.toolpath_text_height,
        "font_path": ref.path,
        "check_glyph_coverage": check_glyph_coverage,
    }
    if ref.small_caps:
        ftext_kwargs["small_caps_scale"] = line.sc_scale
    # Inline <sc> markup (per-character selection) — the gate is inert on a SC
    # font, so the font-level kwargs above are never overwritten.
    if small_caps_gate is not None:
        sc_scale_value, sc_flags = small_caps_gate
        ftext_kwargs["small_caps_scale"] = sc_scale_value
        ftext_kwargs["small_caps_flags"] = sc_flags
    # The per-glyph contour-count probe must ask the font about the glyphs the
    # render actually drew, so it sees the same small-caps selection the
    # renderers got (font-level variant OR the inline tag's flags).
    sc_groups = ref.small_caps
    sc_group_flags: Optional[Tuple[bool, ...]] = None
    if small_caps_gate is not None:
        sc_groups = True
        sc_group_flags = small_caps_gate[1]
    if italic_kwargs:
        ftext_kwargs.update(italic_kwargs)
        # The native italic face (registry sibling probe) splices italic
        # glyphs; without it the shear fallback runs (PLT parity).
        if ref.italic_path is not None:
            ftext_kwargs["italic_face"] = ref.italic_path
    ftext_kwargs.update(_glyph_render_kwargs(line, plt=False))
    if chunk_mode is TextChunkMode.WORD:
        filtered_lc, groups = render_text_line_ftext_with_words(render_text, **ftext_kwargs)
        block = block_from_linecollection(filtered_lc)
        # Contour indices map 1:1 onto strokes only when every contour
        # survived adaptation; otherwise fall back to whole-line chunking.
        if groups and len(block.strokes) == len(filtered_lc):
            pairs = glyph_groups_for_line(
                render_text,
                ref.path,
                len(filtered_lc),
                small_caps=sc_groups,
                small_caps_flags=sc_group_flags,
            )
            return _apply_strikethrough(
                line,
                *_apply_underline(
                    line, block, groups, _validated_glyph_groups(pairs, len(block.strokes))
                ),
            )
        return _apply_strikethrough(line, *_apply_underline(line, block, None, ()))
    filtered_lc = render_text_line_ftext(render_text, **ftext_kwargs)
    block = block_from_linecollection(filtered_lc)
    # Glyph groups slice the whole-line contours by per-glyph contour
    # counts; valid only while every contour survived adaptation (1:1).
    if len(block.strokes) == len(filtered_lc):
        pairs = glyph_groups_for_line(
            render_text,
            ref.path,
            len(filtered_lc),
            small_caps=sc_groups,
            small_caps_flags=sc_group_flags,
        )
        return _apply_strikethrough(
            line,
            *_apply_underline(
                line, block, None, _validated_glyph_groups(pairs, len(block.strokes))
            ),
        )
    return _apply_strikethrough(line, *_apply_underline(line, block, None, ()))


def _decoration_runs(
    line: ResolvedTextLine,
    flags: Tuple[bool, ...],
    *,
    breaks_spaces: bool,
) -> Tuple[Tuple[int, int], ...]:
    """Decompose per-character decoration flags into engraving runs.

    The shared pipeline behind both line decorations (the ``<u>`` underline
    and the ``<s>`` strikethrough): maximal runs of marked characters,
    optionally split at the spaces inside each run (``breaks_spaces``), and
    always split where the engraving cutter changes (``<b>``/``<scale:N>``
    boundaries -- one stroke cannot be cut by two tools).

    Args:
        line: The resolved text line (plain text, per-character cutters).
        flags: Per-character decoration flags aligned with ``plain_text``.
        breaks_spaces: Split each maximal run at the spaces inside it.

    Returns:
        ``(first_char_index, last_char_index)`` inclusive pairs in text
        order, one per engraving run.
    """
    runs = runs_from_flags(flags)
    if breaks_spaces:
        runs = _split_runs_at_spaces(line.plain_text, runs)
    cutters = line.per_char_cutters
    if cutters and len(set(cutters)) > 1:
        runs = _split_runs_at_cutter(runs, cutters)
    return runs


def _underline_render_gate(line: ResolvedTextLine) -> Tuple[Tuple[int, int], ...]:
    """Return the underline runs of one resolved line, or ``()`` when unmarked.

    The gate mirrors :func:`_italic_render_kwargs`: a line without parsed
    markup, or with markup that carries no underline run, keeps the
    historical render path (no underline geometry at all).

    When ``underline_break_spaces`` is set, each maximal run is split at the
    spaces inside it, so every word gets its own underline spanning its own
    ink and the inter-word space stays bare. ``False`` (the default) keeps one
    continuous run across the spaces.

    A run is additionally split at every ``<b>`` boundary: the underline is cut
    with the run's own cutter, and a bold run's centerline sits deeper below
    the baseline (``bold_cutter + underline_offset``), so one underline cannot
    span both tools. A line with no bold run is never split here.

    Args:
        line: The resolved text line.

    Returns:
        ``(first_char_index, last_char_index)`` inclusive pairs per maximal
        underlined run (indices into ``plain_text``), or ``()``.
    """
    if not line.styled_runs:
        return ()
    if not any(run.style.underline for run in line.styled_runs):
        return ()
    flags = tuple(
        style.underline for run in line.styled_runs for style in (run.style,) * len(run.text)
    )
    return _decoration_runs(line, flags, breaks_spaces=line.underline_break_spaces)


def _strikethrough_render_gate(line: ResolvedTextLine) -> Tuple[Tuple[int, int], ...]:
    """Return the strikethrough runs of one resolved line, or ``()`` when unmarked.

    The ``<s>`` mirror of :func:`_underline_render_gate`: a line without
    parsed markup, or with markup that carries no struck run, keeps the
    historical render path (no strikethrough geometry at all). Runs split at
    the spaces inside each run when ``strikethrough_break_spaces`` is set
    (``False`` = one continuous stroke across the whole run, spaces included),
    and always at ``<b>``/``<scale:N>`` cutter boundaries -- each struck run
    engraves with its own tool's pen.

    Unlike the underline, a struck run is **never** broken by glyph ink:
    a descender punches through the stroke (the TODO's "like underline,
    except no breaks in the line").

    Args:
        line: The resolved text line.

    Returns:
        ``(first_char_index, last_char_index)`` inclusive pairs per maximal
        struck run (indices into ``plain_text``), or ``()``.
    """
    if not line.styled_runs:
        return ()
    if not any(run.style.strikethrough for run in line.styled_runs):
        return ()
    flags = tuple(
        style.strikethrough for run in line.styled_runs for style in (run.style,) * len(run.text)
    )
    return _decoration_runs(line, flags, breaks_spaces=line.strikethrough_break_spaces)


def _split_runs_at_cutter(
    runs: Tuple[Tuple[int, int], ...],
    cutters: Tuple[float, ...],
) -> Tuple[Tuple[int, int], ...]:
    """Split each underlined run where the engraving cutter changes.

    The underline is cut with the run's own tool, and a run's centerline sits
    one cutter below the baseline, so one underline cannot span two tools. The
    cutter changes at a ``<b>`` boundary (the plain tool vs. the bold rung) and
    at a ``<scale:N>`` boundary (the effective-height cutter), so this is the
    general form of the bold-boundary split.

    Args:
        runs: The underlined runs (inclusive ``plain_text`` indices).
        cutters: Per-character cutter diameters aligned with ``plain_text``.

    Returns:
        The runs split at cutter boundaries, in order.
    """
    split: List[Tuple[int, int]] = []
    for first, last in runs:
        start = first
        for index in range(first, last):
            if cutters[index] != cutters[index + 1]:
                split.append((start, index))
                start = index + 1
        split.append((start, last))
    return tuple(split)


def _run_cutter(line: ResolvedTextLine, first: int) -> float:
    """Return the cutter the underlined run starting at ``first`` engraves with.

    Runs are split at cutter boundaries (see :func:`_split_runs_at_cutter`), so
    the first character's cutter decides the whole run's tool: the line's own
    cutter, its ``<b>`` bold rung, or the run's ``<scale:N>`` effective-height
    cutter.
    """
    cutters = line.per_char_cutters
    if cutters and first < len(cutters):
        return cutters[first]
    return line.cutter_diameter


def _split_runs_at_spaces(
    plain_text: str,
    runs: Tuple[Tuple[int, int], ...],
) -> Tuple[Tuple[int, int], ...]:
    """Split each underlined run at the spaces, leaving the spaces bare.

    A space (and any run of spaces) terminates an underline, so each word of a
    multi-word run draws its own underline spanning its own ink. A run made
    entirely of spaces yields no sub-run (nothing to engrave).

    Args:
        plain_text: The line's markup-free text.
        runs: The maximal underlined runs (inclusive ``plain_text`` indices).

    Returns:
        The runs split at spaces, in order.
    """
    split: List[Tuple[int, int]] = []
    for first, last in runs:
        start: Optional[int] = None
        for index in range(first, last + 1):
            if plain_text[index] == " ":
                if start is not None:
                    split.append((start, index - 1))
                    start = None
            elif start is None:
                start = index
        if start is not None:
            split.append((start, last))
    return tuple(split)


def _run_glyph_positions(
    plain_text: str,
    glyph_groups: Tuple[Tuple[int, ...], ...],
    first: int,
    last: int,
) -> List[int]:
    """Return the glyph indices a decoration run covers, in text order.

    A run's bounds are ``plain_text`` indices (spaces included), while
    ``glyph_groups`` is indexed by the non-space character order, so the walk
    counts glyphs as it scans the plain text.

    Args:
        plain_text: The line's markup-free text.
        glyph_groups: The per-glyph stroke partition of the rendered block.
        first: The run's first ``plain_text`` index (inclusive).
        last: The run's last ``plain_text`` index (inclusive).

    Returns:
        The non-empty glyph-group indices covered by the run.
    """
    positions: List[int] = []
    glyph_index = -1
    for text_index, char in enumerate(plain_text):
        if char == " ":
            continue
        glyph_index += 1
        if first <= text_index <= last and glyph_groups[glyph_index]:
            positions.append(glyph_index)
    return positions


def _run_span(
    boxes: Sequence[Tuple[float, float, float, float]],
    positions: Sequence[int],
) -> Tuple[float, float]:
    """Return the decorated run's ``(min bbox left, max bbox right)`` span."""
    return (
        min(boxes[position][0] for position in positions),
        max(boxes[position][2] for position in positions),
    )


def _underline_strokes(
    line: ResolvedTextLine,
    block: TextBlock,
    glyph_groups: Tuple[Tuple[int, ...], ...],
    runs: Tuple[Tuple[int, int], ...],
) -> List[Stroke]:
    """Build the ``<u>`` underline strokes of one rendered line.

    Each maximal underlined run draws one centerline underline spanning its
    first glyph's bounding-box left edge to its last glyph's right edge, at
    ``cutter_diameter + underline_offset`` inches below the baseline (so the
    engraved underline's top edge floats exactly ``underline_offset`` below
    the engraved baseline stroke). The break test is **strictly horizontal**
    and keyed to the underline's **centerline**: a glyph cuts a gap only where
    its ink crosses the centerline (inside a thin band of ``0.2 *
    underline_break_clearance`` around it), so a descender's stem punches
    through the underline, a ``g``'s bowl that dips below without crossing the
    centerline keeps its underline, and a glyph merely standing beside it (a
    cap, a neighbour) never breaks it (see
    :mod:`plt_optimizer.generate.underline`).

    The centerline **position** and the **break band** are two distinct
    quantities. The position is ``cutter + offset`` below the baseline (the
    offset is the edge-to-edge air the user asked for). The band is a thin
    strip on the centerline, reaching ``0.2 * underline_break_clearance``
    above and below it, and each cut is padded horizontally by ``0.5 * cutter
    + underline_break_clearance`` (the half cutter is the glyph stroke's own
    half-width, the clearance the engraved air left at each edge). The offset
    must stay out of the trigger: were the band keyed off ``cutter + offset``,
    the offset would cancel out of the test (a glyph breaks the underline iff
    its ink dips below the baseline, at *any* offset), so raising
    ``underline_offset`` could never prevent a break -- the reported bug where
    caps punched gaps no matter how deep the underline was set.

    Args:
        line: The resolved text line (cutter, offset, plain text).
        block: The rendered line block (baseline at ``y = 0``, +y up).
        glyph_groups: The validated per-glyph stroke partition of ``block``.
        runs: The underlined character runs (from :func:`_underline_render_gate`).

    Returns:
        The underline strokes, in run order (empty when nothing is
        underlined, the run has no geometry, or gaps consume the whole span).
        A bold run's centerline sits ``bold_cutter + underline_offset`` below
        the baseline -- deeper, because the thicker tool's stroke starts lower
        (the runs are split at bold boundaries by the gate, so each run's
        strokes share one centerline Y).
    """
    if not runs or not glyph_groups:
        return []

    chars = _glyph_chars(line.plain_text)
    if len(chars) != len(glyph_groups):
        return []

    # Per-character geometry: the glyph's bbox (for the run span) and its ink
    # (for the gap cuts), both in the block's baseline frame.
    boxes: List[Tuple[float, float, float, float]] = []
    inks: List[Ink] = []
    for indices in glyph_groups:
        glyph_block = TextBlock(strokes=tuple(block.strokes[i] for i in indices))
        bounds = glyph_block.bounds()
        boxes.append(bounds if bounds is not None else (0.0, 0.0, 0.0, 0.0))
        inks.append(_glyph_ink(block, indices))
    line_ink: Ink = tuple(polyline for ink in inks for polyline in ink)

    strokes: List[Stroke] = []
    min_y_seen: Optional[float] = None
    for first, last in runs:
        # The run's glyph indices (see _run_glyph_positions: run bounds are
        # plain_text indices, glyph_groups is indexed by non-space characters).
        glyph_positions = _run_glyph_positions(line.plain_text, glyph_groups, first, last)
        if not glyph_positions:
            continue
        span = _run_span(boxes, glyph_positions)
        # Per-run cutter: the underline is cut with the run's own tool, so a
        # bold run's centerline sits one bold cutter below the baseline and a
        # scaled run's one effective-height cutter below it (the runs are split
        # at cutter boundaries by the gate).
        cutter = _run_cutter(line, first)
        line_y = -(cutter + line.underline_offset)
        min_y_seen = line_y if min_y_seen is None else min(min_y_seen, line_y)
        segments = underline_segments(
            line_ink,
            span,
            line_y,
            cutter,
            line.underline_break_clearance,
            min_length=_INK_EMISSION_FLOOR,
        )
        for x_min, x_max in segments:
            strokes.append(
                Stroke(
                    pen_up=complex(x_min, line_y),
                    segments=(LineSeg(complex(x_min, line_y), complex(x_max, line_y)),),
                )
            )
    if strokes:
        logger.info(
            "Underline: %d run(s), %d segment(s) on %r (y=%.4fin, clearance=%.4fin).",
            len(runs),
            len(strokes),
            line.plain_text,
            min_y_seen if min_y_seen is not None else 0.0,
            line.underline_break_clearance,
        )
    return strokes


def _strikethrough_strokes(
    line: ResolvedTextLine,
    block: TextBlock,
    glyph_groups: Tuple[Tuple[int, ...], ...],
    runs: Tuple[Tuple[int, int], ...],
) -> List[Stroke]:
    """Build the ``<s>`` strikethrough strokes of one rendered line.

    Each maximal struck run draws **one continuous** centerline stroke
    spanning its first glyph's bounding-box left edge to its last glyph's
    right edge, at :attr:`~plt_optimizer.generate.resolution.ResolvedTextLine.strikethrough_cap`
    inches **above** the baseline (the explicit ``strikethrough_offset``, or
    the cap-band midline when unset).

    This is the underline's geometry with the break model deliberately
    removed: there is **no** gap cutting, no clearance and no trigger band, so
    a descender punches through the stroke and the line stays whole (the
    TODO's "like underline, except no breaks in the line"). The stroke is also
    uniform in Y for the whole line -- the height is a typography property,
    not a function of the run's cutter -- while the runs are still split at
    ``<b>``/``<scale:N>`` cutter boundaries by the gate, so each run's stroke
    rides its own tool's pen.

    Args:
        line: The resolved text line (strikethrough height, plain text).
        block: The rendered line block (baseline at ``y = 0``, +y up).
        glyph_groups: The validated per-glyph stroke partition of ``block``.
        runs: The struck character runs (from
            :func:`_strikethrough_render_gate`).

    Returns:
        The strikethrough strokes, in run order (empty when nothing is
        struck, the run has no geometry, or its span is narrower than the
        ink-emission floor).
    """
    if not runs or not glyph_groups:
        return []

    chars = _glyph_chars(line.plain_text)
    if len(chars) != len(glyph_groups):
        return []

    # Per-glyph bounding boxes only: the stroke never breaks, so unlike the
    # underline no glyph ink is needed here.
    boxes: List[Tuple[float, float, float, float]] = []
    for indices in glyph_groups:
        glyph_block = TextBlock(strokes=tuple(block.strokes[i] for i in indices))
        bounds = glyph_block.bounds()
        boxes.append(bounds if bounds is not None else (0.0, 0.0, 0.0, 0.0))

    line_y = line.strikethrough_cap
    strokes: List[Stroke] = []
    for first, last in runs:
        glyph_positions = _run_glyph_positions(line.plain_text, glyph_groups, first, last)
        if not glyph_positions:
            continue
        x_min, x_max = _run_span(boxes, glyph_positions)
        # A span narrower than half a plotter unit cannot exist in the emitted
        # geometry (the coordinates are integer units), so skip the stroke.
        if x_max - x_min < _INK_EMISSION_FLOOR:
            continue
        strokes.append(
            Stroke(
                pen_up=complex(x_min, line_y),
                segments=(LineSeg(complex(x_min, line_y), complex(x_max, line_y)),),
            )
        )
    if strokes:
        logger.info(
            "Strikethrough: %d run(s), %d stroke(s) on %r (y=%.4fin, unbroken).",
            len(runs),
            len(strokes),
            line.plain_text,
            line_y,
        )
    return strokes


def _append_decoration(
    block: TextBlock,
    word_groups: Optional[List[Tuple[str, List[int]]]],
    glyph_groups: Tuple[Tuple[int, ...], ...],
    strokes: List[Stroke],
) -> Tuple[TextBlock, Optional[List[Tuple[str, List[int]]]], Tuple[Tuple[int, ...], ...]]:
    """Append decoration strokes to a validated line render (shared splice).

    Both line decorations (the ``<u>`` underline and the ``<s>``
    strikethrough) splice their strokes the same way: appended **after** the
    glyph partition is validated, so ``glyph_groups`` stays the exact 1:1
    character partition (the descending-character relief maps characters to
    groups by index, and the intra-chunk glyph sweep treats a group as one
    glyph -- neither may see a decoration stroke as glyph ink).
    ``word_groups`` gains the new indices on its last non-empty word so
    WORD-mode chunking never drops the decoration.

    Args:
        block: The validated render (baseline at ``y = 0``, +y up).
        word_groups: Per-word stroke indices (``None`` in line mode).
        glyph_groups: Per-glyph stroke indices (empty when unavailable).
        strokes: The decoration strokes to append (non-empty).

    Returns:
        The ``(block, word_groups, glyph_groups)`` triple carrying the
        decoration.
    """
    first_index = len(block.strokes)
    decoration_indices = list(range(first_index, first_index + len(strokes)))
    extended = TextBlock(strokes=(*block.strokes, *strokes))
    if word_groups is not None:
        for _word_text, indices in reversed(word_groups):
            if indices:
                indices.extend(decoration_indices)
                break
    return extended, word_groups, glyph_groups


def _apply_underline(
    line: ResolvedTextLine,
    block: TextBlock,
    word_groups: Optional[List[Tuple[str, List[int]]]],
    glyph_groups: Tuple[Tuple[int, ...], ...],
) -> Tuple[TextBlock, Optional[List[Tuple[str, List[int]]]], Tuple[Tuple[int, ...], ...]]:
    """Append the ``<u>`` underline strokes to one rendered line.

    The underline strokes are appended **after** the glyph partition is
    validated, so ``glyph_groups`` stays the exact 1:1 character partition
    (the descending-character relief maps characters to groups by index, and
    the intra-chunk glyph sweep treats a group as one glyph -- neither may
    see the underline as glyph ink). ``word_groups`` gains the underline
    indices on its last non-empty word so WORD-mode chunking never drops the
    underline.

    A line without underline markup returns its inputs untouched, so every
    other line keeps bit-identical geometry.

    Args:
        line: The resolved text line.
        block: The validated render (baseline at ``y = 0``, +y up).
        word_groups: Per-word stroke indices (``None`` in line mode).
        glyph_groups: Per-glyph stroke indices (empty when unavailable).

    Returns:
        The ``(block, word_groups, glyph_groups)`` triple carrying the
        underline.
    """
    runs = _underline_render_gate(line)
    if not runs:
        return block, word_groups, glyph_groups
    strokes = _underline_strokes(line, block, glyph_groups, runs)
    if not strokes:
        return block, word_groups, glyph_groups
    return _append_decoration(block, word_groups, glyph_groups, strokes)


def _apply_strikethrough(
    line: ResolvedTextLine,
    block: TextBlock,
    word_groups: Optional[List[Tuple[str, List[int]]]],
    glyph_groups: Tuple[Tuple[int, ...], ...],
) -> Tuple[TextBlock, Optional[List[Tuple[str, List[int]]]], Tuple[Tuple[int, ...], ...]]:
    """Append the ``<s>`` strikethrough strokes to one rendered line.

    The ``<s>`` mirror of :func:`_apply_underline` (same splice, same
    guarantees -- see :func:`_append_decoration`). Applied **after** the
    underline in :func:`_render_line_block`, so a line carrying both
    decorations ends with the underline strokes and then the strike strokes;
    neither joins the glyph partition.

    A line without strikethrough markup returns its inputs untouched, so every
    other line keeps bit-identical geometry.

    Args:
        line: The resolved text line.
        block: The validated render (baseline at ``y = 0``, +y up), the
            underline already appended when the line has one.
        word_groups: Per-word stroke indices (``None`` in line mode).
        glyph_groups: Per-glyph stroke indices (empty when unavailable).

    Returns:
        The ``(block, word_groups, glyph_groups)`` triple carrying the
        strikethrough.
    """
    runs = _strikethrough_render_gate(line)
    if not runs:
        return block, word_groups, glyph_groups
    strokes = _strikethrough_strokes(line, block, glyph_groups, runs)
    if not strokes:
        return block, word_groups, glyph_groups
    return _append_decoration(block, word_groups, glyph_groups, strokes)


def _strikethrough_run_cutters(
    line: ResolvedTextLine,
    block: TextBlock,
    glyph_groups: Tuple[Tuple[int, ...], ...],
) -> Tuple[Tuple[float, float, float], ...]:
    """Return ``(x_min, x_max, cutter)`` per strikethrough stroke of ``line``.

    The pen split needs each strike stroke's tool, and -- unlike the underline
    -- a strike centerline's Y encodes none: the height is a typography
    property (``strikethrough_cap``), identical for every run, so the depth
    cannot be inverted into a cutter the way ``-y - underline_offset`` does
    for the underline. The stroke's **span** identifies its run instead, and
    the gate splits runs at cutter boundaries, so the run's first character
    owns the tool (see :func:`_run_cutter`).

    The spans are recomputed from the same ``glyph_groups`` partition of the
    same block, so they match :func:`_strikethrough_strokes` bit-for-bit.

    Args:
        line: The resolved text line.
        block: The line's block with the decorations appended.
        glyph_groups: The per-glyph stroke partition (decorations excluded).

    Returns:
        One ``(x_min, x_max, cutter)`` triple per emitted strike stroke, in
        run order (empty when the line has no strike geometry).
    """
    runs = _strikethrough_render_gate(line)
    if not runs or not glyph_groups:
        return ()
    if len(_glyph_chars(line.plain_text)) != len(glyph_groups):
        return ()
    boxes: List[Tuple[float, float, float, float]] = []
    for indices in glyph_groups:
        glyph_block = TextBlock(strokes=tuple(block.strokes[i] for i in indices))
        bounds = glyph_block.bounds()
        boxes.append(bounds if bounds is not None else (0.0, 0.0, 0.0, 0.0))
    spans: List[Tuple[float, float, float]] = []
    for first, last in runs:
        positions = _run_glyph_positions(line.plain_text, glyph_groups, first, last)
        if not positions:
            continue
        x_min, x_max = _run_span(boxes, positions)
        if x_max - x_min < _INK_EMISSION_FLOOR:
            continue
        spans.append((x_min, x_max, _run_cutter(line, first)))
    return tuple(spans)


def _stroke_cutter_map(
    line: ResolvedTextLine,
    block: TextBlock,
    glyph_groups: Tuple[Tuple[int, ...], ...],
) -> Tuple[float, ...]:
    """Return the engraving cutter of every stroke in ``block``, by index.

    The per-cutter pen split needs one cutter per *stroke*, not per line. The
    glyph strokes come from the per-character cutter array (:attr:
    `ResolvedTextLine.per_char_cutters`) walked over the exact ``glyph_groups``
    partition (spaces own no group, mirroring :func:`_glyph_chars`). The
    ``<u>`` underline strokes are appended **after** that partition, so they
    are identified by their centerline depth: a run's centerline sits exactly
    ``cutter + underline_offset`` below the baseline, and the gate splits runs
    at cutter boundaries, so ``-y - underline_offset`` recovers the tool
    exactly (no sampling, no tolerance guessing). The ``<s>`` strikethrough
    strokes (appended last) sit at a cutter-independent height, so they are
    identified by their span instead -- see
    :func:`_strikethrough_run_cutters`.

    Args:
        line: The resolved text line (per-character cutters, offset).
        block: The line's block with the decorations appended, in the
            renderers' local frame (baseline ``y = 0``).
        glyph_groups: The per-glyph stroke partition (decorations excluded).

    Returns:
        One cutter diameter per stroke of ``block``, in stroke order; **empty**
        when the whole line engraves with a single tool (no markup, a bold run
        with no bold rung, or scaled runs whose ladder cutter equals the
        line's) -- the caller treats that as "no split needed", exactly the
        historical single-pen fast path.
    """
    cutters = line.per_char_cutters
    if not cutters or not glyph_groups:
        return ()
    known = line.used_cutters
    strike_cap = line.strikethrough_cap if line.has_strikethrough else None
    strike_spans = (
        _strikethrough_run_cutters(line, block, glyph_groups) if strike_cap is not None else ()
    )
    mapping: List[Optional[float]] = [None] * len(block.strokes)
    glyph_position = 0
    for char, cutter in zip(line.plain_text, cutters):
        if char == " ":
            continue
        if glyph_position >= len(glyph_groups):
            break
        for index in glyph_groups[glyph_position]:
            mapping[index] = cutter
        glyph_position += 1
    for index, stroke in enumerate(block.strokes):
        if mapping[index] is not None:
            continue
        if not stroke.segments:  # pragma: no cover - every stroke cuts
            mapping[index] = line.cutter_diameter
            continue
        start = stroke.segments[0].start
        y = start.imag
        # A strikethrough stroke: its height encodes no cutter, so match the
        # run by span (both sides are computed from this same block, so the
        # floats agree to the emitted precision).
        if strike_cap is not None:
            if abs(y - strike_cap) <= _INK_EMISSION_FLOOR:
                for x_min, x_max, cutter in strike_spans:
                    if (
                        abs(start.real - x_min) <= _INK_EMISSION_FLOOR
                        and abs(stroke.segments[0].end.real - x_max) <= _INK_EMISSION_FLOOR
                    ):
                        mapping[index] = cutter
                        break
            if mapping[index] is not None:
                continue
        # An underline stroke: its centerline Y is -(cutter + offset). The
        # round trip is not float-exact (0.06 + 0.05 back to 0.06 can land one
        # ulp off), which would key a spurious pen layer, so snap to the
        # closest cutter the line actually engraves with.
        recovered = -y - line.underline_offset
        mapping[index] = min(known, key=lambda cutter: abs(cutter - recovered))
    resolved = tuple(cutter if cutter is not None else line.cutter_diameter for cutter in mapping)
    if len(set(resolved)) <= 1:
        return ()  # One tool engraves the whole line: nothing to split.
    return resolved


def _baseline_shift_inches(line: ResolvedTextLine) -> Optional[Tuple[float, ...]]:
    """Return the per-character baseline shift in inches, or ``None`` when unset.

    The composition of the two per-character vertical translates, both riding
    the renderers' single ``baseline_shifts`` array:

    * the ``<sup>``/``<sub>`` baseline shift (cap-height units, from
      :attr:`ResolvedTextLine.sup_sub_shifts`) converted to inches by the
      line's ``toolpath_text_height`` (the per-font cap height -- the same
      basis as the baseline pitch);
    * the engraved-baseline alignment raise
      (:attr:`ResolvedTextLine.baseline_raises`), which lifts a character
      engraving with a cutter ``d`` other than the line's ``cutter`` by
      ``(d - cutter) / 2`` so its **engraved** baseline matches the plain
      run's (and, for a ``<b>`` run, its **engraved** capline too -- the two
      alignments reduce to the same half-decrease).

    ``None`` when the line carries neither (the markup-free fast path, and a
    line whose markup engraves with one tool: the renderers keep the
    historical call shape, bit-identical output).

    Args:
        line: The resolved text line.

    Returns:
        One shift in inches per character of ``plain_text``, or ``None``.
    """
    raises = line.baseline_raises
    sup_sub: Tuple[float, ...] = ()
    if line.has_sup_sub:
        cap = line.toolpath_text_height
        sup_sub = tuple(shift * cap for shift in line.sup_sub_shifts)
    if not sup_sub and not raises:
        return None
    shifts: List[float] = []
    for index in range(len(line.plain_text)):
        shift = sup_sub[index] if index < len(sup_sub) else 0.0
        shift += raises[index] if index < len(raises) else 0.0
        shifts.append(shift)
    if all(shift == 0.0 for shift in shifts):
        return None
    return tuple(shifts)


def _glyph_render_kwargs(line: ResolvedTextLine, *, plt: bool) -> dict[str, Any]:
    """Return the per-glyph (cutter, scale, shift) renderer kwargs for one resolved line.

    The unified gate for the ``<b>``, ``<scale:N>`` and ``<sup>``/``<sub>``
    markup: the geometry plan (:func:`resolution.glyph_render_plan`) carries
    each character's cutter diameter and baseline-anchored uniform scale, so
    a bold run, a scaled run, a bold+scaled run, and a bare ``<sup>``/
    ``<sub>`` run (whose downscale composes into the plan's scale) all flow
    through the same per-character arrays; the ``<sup>``/``<sub>`` baseline
    shift AND the engraved-baseline alignment raise (:func:`_baseline_shift_inches`)
    ride the extra ``baseline_shifts`` array (inches). Empty dict when the
    line carries none of these markup kinds -- the historical call shape,
    bit-identical output.

    Args:
        line: The resolved text line.
        plt: True for a PLT line (per-character ``cutter_diameters`` +
            ``glyph_scales``), False for a TTF line (``glyph_scales`` only
            -- the TTF pen advance stays with matplotlib shaping, and the
            cutter never changes the TTF geometry beyond the scale).

    Returns:
        ``{"cutter_diameters": ..., "glyph_scales": ...}`` for a PLT line,
        ``{"glyph_scales": ...}`` for a TTF line, or ``{}`` (also for a TTF
        line whose plan is all-unity -- the top-of-inventory bold fallback).
    """
    plan = glyph_render_plan(line)
    shifts = _baseline_shift_inches(line)
    if plan is None:
        return {} if shifts is None else {"baseline_shifts": shifts}
    diameters, scales = plan
    if not plt and all(scale == 1.0 for scale in scales):
        # The all-unity fallback (e.g. top-of-inventory bold): no geometry
        # scale to pass, but a <sup>/<sub> shift still rides along.
        return {} if shifts is None else {"baseline_shifts": shifts}
    if shifts is None:
        if plt:
            return {"cutter_diameters": diameters, "glyph_scales": scales}
        return {"glyph_scales": scales}
    if plt:
        return {
            "cutter_diameters": diameters,
            "glyph_scales": scales,
            "baseline_shifts": shifts,
        }
    return {"glyph_scales": scales, "baseline_shifts": shifts}


def _italic_render_kwargs(line: ResolvedTextLine) -> dict[str, Any]:
    """Return the italic renderer kwargs for one resolved line.

    Empty dict when the line carries no italic markup (the historical call
    shape, bit-identical output): no parsed markup, no italic run, or
    ``italic_angle == 0`` (upright by contract -- the shear is skipped and
    italic runs engrave like plain text).

    Args:
        line: The resolved text line.

    Returns:
        ``{"italic_tan": ..., "italic_flags": ...}`` for a PLT line, the
        same pair for a TTF line (the ``italic_face`` is added by the
        caller from the font registry), or ``{}``.
    """
    if not line.styled_runs:
        return {}
    if not any(run.style.italic for run in line.styled_runs):
        return {}
    if line.italic_angle <= 0.0:
        return {}
    flags = tuple(
        style.italic for run in line.styled_runs for style in (run.style,) * len(run.text)
    )
    return {"italic_tan": math.tan(math.radians(line.italic_angle)), "italic_flags": flags}


def _small_caps_render_gate(
    line: ResolvedTextLine,
    ref: FontRef,
) -> Optional[Tuple[float, Tuple[bool, ...]]]:
    """Return the active inline ``<sc>`` selection for one line, or ``None``.

    The single decision shared by the PLT renderer, the TTF renderer and the
    per-glyph contour-count probe (all three must agree, or the probe asks
    the font about glyphs the render never drew and the glyph groups are
    silently lost). It mirrors :func:`_italic_render_kwargs`: ``None`` when
    the line carries no parsed markup or no small-caps run (the historical
    call shape, bit-identical output). Two inertness rules beyond that:

    * a line whose font is **already** a small-caps variant (``<base> SC``)
      engraves every lowercase character as a shrunken capital, so the tag
      has no additional effect there -- the renderers keep the font-level
      whole-line selection (``lowercase_scale`` / ``small_caps_scale`` with
      no flags), bit-identical to the pre-tag behaviour;
    * a **symbol font** (``Fonts/VP-EL-Fonts/non_text_fonts.txt``) engraves
      symbols in its ASCII slots and is exempt from the `` SC`` variant
      because it has no lowercase to shrink -- the same reasoning applies
      inline, so the tag is a no-op.

    Args:
        line: The resolved text line.
        ref: The resolved font reference (variant + symbol-font exemptions).

    Returns:
        ``(sc_scale, per-character flags)`` when the tag engraves, else
        ``None``.
    """
    if not line.styled_runs or not line.has_small_caps:
        return None
    if ref.small_caps:
        return None
    if base_font_name(ref.name).lower() in non_text_font_names():
        return None
    return line.sc_scale, line.small_caps_flags


def measure_line_natural_width(line: ResolvedTextLine) -> float:
    """Measure one resolved text line's natural rendered width in inches.

    Renders the line standalone (no label margins, no compression, no
    alignment -- the raw glyph run) and returns its bounding-box width.
    Consumed by the line-content reflow pre-pass
    (:mod:`plt_optimizer.generate.line_content`) to compare candidate word
    partitions; the measurement uses the same renderer dispatch as the label
    render path, so widths match what the label engraves.

    Args:
        line: The resolved text line to measure.

    Returns:
        The rendered width in inches (``0.0`` for a line with no geometry).

    Raises:
        LabelRenderError: If the line's font cannot render its text (missing
            glyph, unknown font), naming the line.
    """
    try:
        block, _word_groups, _glyph_groups = _render_line_block(line, TextChunkMode.LINE)
    except (PltFontRenderError, FtextRenderError) as exc:
        raise LabelRenderError(f"text line ({line.text!r}) cannot be rendered: {exc}") from exc
    bounds = block.bounds()
    if bounds is None:
        return 0.0
    return bounds[2] - bounds[0]


def measure_line_vertical_extents(line: ResolvedTextLine) -> tuple[float, float]:
    """Measure one resolved text line's ink extents around its baseline.

    Renders the line standalone (no label margins, no compression, no
    stacking -- the raw glyph run) and reads its bounding box in the
    renderer's baseline frame, where both font paths place the **baseline at
    y = 0** with ``+y`` up and descenders below it. This is the production
    :data:`~plt_optimizer.generate.resolution.LineExtentsProbe`: baseline
    spacing needs to know how far a line's glyphs reach below their baseline
    (which depends on the characters it contains) before the block is
    stacked.

    Args:
        line: The resolved text line to measure.

    Returns:
        ``(ascender, descender)`` in inches, both non-negative: the distance
        from the baseline to the top of the ink, and the depth of the ink
        hanging below the baseline. ``(0.0, 0.0)`` for a line with no
        geometry (blank text).

    Raises:
        LabelRenderError: If the line's font cannot render its text (missing
            glyph, unknown font), naming the line.
    """
    try:
        block, _word_groups, _glyph_groups = _render_line_block(line, TextChunkMode.LINE)
    except (PltFontRenderError, FtextRenderError) as exc:
        raise LabelRenderError(f"text line ({line.text!r}) cannot be rendered: {exc}") from exc
    bounds = block.bounds()
    if bounds is None:
        return (0.0, 0.0)
    # Clamp at zero: a line whose ink sits entirely above the baseline has no
    # descender, and rounding noise must not manufacture one.
    return (max(0.0, bounds[3]), max(0.0, -bounds[1]))


class _BorderShortfall(NamedTuple):
    """A glyph the relief could not lift clear of the bottom border.

    Attributes:
        fix: The relief that was applied (the allowances fully spent, or
            partly -- the record of what the glyph received).
        gap: The glyph's final swept-ink distance from the border centreline,
            inches (clamped to the reporting radius).
        required: The clearance the engraved strokes must keep, inches.
    """

    fix: DescenderFix
    gap: float
    required: float


@dataclass(frozen=True)
class _DescenderRelief:
    """The outcome of one descending-character relief run.

    Everything is in the renderers' **local frame**: one block per renderable
    line with that line's baseline at ``y = 0`` (the frame the first render
    pass produces). The caller positions the returned blocks exactly like the
    ones it passed in, so the relief never re-does the alignment math.

    Attributes:
        blocks: The line blocks after every applied relief, in renderable-line
            order (identical objects to the input when nothing triggered).
        heights: Each line's ink height after the relief, inches (a relief
            scales a glyph about its baseline, so a line's ink height can
            shrink). Consumed by ink-box stacking.
        spacings: The inter-line spacings after any relief-driven growth.
        geometry: The refreshed per-line ``(pitch_basis, ascender, descender)``
            triples -- the relief changes how far each line's ink reaches
            around its baseline, and the stacking math depends on it.
        fixes: Every applied relief, in (line, glyph) order. A descending
            glyph's own relief and the top-down downscale of a following-line
            glyph that shared it are both records here, told apart by
            :attr:`DescenderFix.reciprocal`.
        residual: Per-gap clearance shortfall left after every allowance and
            all admissible spacing growth were spent, inches. Non-zero entries
            abort the job.
        border_shortfalls: Every glyph still within the required clearance of
            the bottom border after every allowance was spent. Reported as a
            WARNING, never an abort: spacing growth cannot help a border
            collision (growing the gap above the last line moves that line
            DOWN in the centred frame), so the ramp is the only lever and the
            engraved result is the best the allowances allow.
    """

    blocks: List[TextBlock]
    heights: List[float]
    spacings: List[float]
    geometry: List[Tuple[float, float, float]]
    fixes: List[DescenderFix]
    residual: List[float]
    border_shortfalls: List[_BorderShortfall] = field(default_factory=list)


def _glyph_chars(text: str) -> List[str]:
    """Return the characters a rendered line's glyph partition covers.

    Both renderers exclude spaces from the per-glyph stroke partition (the PLT
    walk emits no strokes for a space, the TTF partition slices by per-glyph
    contour counts, which are ``0`` for a space), so the partition's ``i``-th
    group belongs to the ``i``-th non-space character.

    Args:
        text: The line's text.

    Returns:
        The non-space characters, in text order.
    """
    return [char for char in text if char != " "]


def _stroke_polyline(stroke: Stroke) -> Polyline:
    """Return one stroke's swept ink as a single polyline.

    Segments chain tip-to-tail, so a stroke is one continuous chain: each
    segment contributes its flattened vertices (a :class:`~plt_optimizer.generate.text_geometry.ArcSeg`
    flattens at the shared chord tolerance, a :class:`~plt_optimizer.generate.text_geometry.LineSeg`
    contributes its endpoints), with the shared junction vertex emitted once.
    Degenerate leftovers are harmless -- the distance primitives measure a
    zero-length segment as a point.

    Args:
        stroke: The stroke to flatten.

    Returns:
        The stroke's vertices, in the stroke's frame. Empty for a segment-less
        stroke (no ink).
    """
    points: List[Point] = []
    last: Optional[complex] = None
    for segment in stroke.segments:
        flattened = segment.flattened()
        if not flattened:
            continue
        start = 1 if last is not None and flattened[0] == last else 0
        points.extend((point.real, point.imag) for point in flattened[start:])
        last = flattened[-1]
    return tuple(points)


def _glyph_ink(block: TextBlock, indices: Sequence[int]) -> Ink:
    """Return every polyline one glyph engraves, in the block's frame.

    Args:
        block: The line's block (baseline at ``y = 0`` in the local frame).
        indices: The glyph's stroke indices into ``block.strokes``.

    Returns:
        One polyline per non-empty stroke. Empty when the glyph has no ink.
    """
    return tuple(
        polyline for polyline in (_stroke_polyline(block.strokes[i]) for i in indices) if polyline
    )


def _clip_below_baseline(polyline: Polyline) -> Polyline:
    """Clip a polyline to the ink hanging below the baseline's emission floor.

    The relief's rolling circle travels along the part of a descending glyph
    that hangs *into the gap below its line*, so the glyph's full ink is
    clipped just below its own baseline: the baseline is the line's ``y = 0``
    in the renderer's local frame. Runs crossing the floor contribute a vertex
    exactly on it, so the clipped chain keeps its real silhouette at the cut.

    The floor is :data:`_INK_EMISSION_FLOOR` (half a plotter unit): ink
    shallower than the emitted coordinate grid cannot collide in the engraved
    output, and counting it would relieve glyphs that do not descend.

    Args:
        polyline: The glyph's vertices, in the local frame.

    Returns:
        The clipped vertices (possibly empty, possibly a single point sitting
        on the floor).
    """
    floor = -_INK_EMISSION_FLOOR
    clipped: List[Point] = []
    previous: Optional[Point] = None
    for point in polyline:
        inside = point[1] <= floor
        if previous is not None and inside != (previous[1] <= floor):
            # Linear crossing of the floor: emit the intersection vertex.
            span = point[1] - previous[1]
            if span != 0.0:
                ratio = (floor - previous[1]) / span
                clipped.append((previous[0] + ratio * (point[0] - previous[0]), floor))
        if inside:
            clipped.append(point)
        previous = point
    return tuple(clipped)


def _sub_baseline_ink(ink: Ink) -> Ink:
    """Clip a glyph's ink to the part hanging below its baseline.

    Args:
        ink: The glyph's polylines, in the renderer's local frame.

    Returns:
        The clipped polylines, dropping the ones with no sub-baseline ink.
    """
    return tuple(clipped for clipped in (_clip_below_baseline(p) for p in ink) if clipped)


def _glyph_boxes(
    block: TextBlock,
    glyph_groups: Tuple[Tuple[int, ...], ...],
    text: str,
) -> List[GlyphBox]:
    """Measure every glyph of one line in the renderer's baseline frame.

    Each glyph's ink box is the swept bounds of its own strokes (the same
    stroke-subset idiom the plate-space chunk records use), which is what makes
    the relief per-descending-character: a glyph's box reflects only its own
    silhouette, never its line's aggregate ink box. The block is in the local
    frame, so the baseline is ``y = 0`` and the descender depth is ``-y_min``.

    Each box also carries the glyph's **swept ink** (the stroke polylines) and
    that ink clipped below the baseline, so the relief measures true 2D
    swept-ink distance -- a circle of the required diameter rolled along the
    descending part -- instead of comparing boxes.

    Args:
        block: The line's block, baseline at ``y = 0``.
        glyph_groups: One stroke-index tuple per rendered character.
        text: The line's text (for the character names).

    Returns:
        One :class:`GlyphBox` per glyph group, in text order. Empty when no
        per-glyph partition is available (the relief is a no-op then).
    """
    chars = _glyph_chars(text)
    if not glyph_groups or len(chars) != len(glyph_groups):
        return []
    boxes: List[GlyphBox] = []
    for glyph_index, indices in enumerate(glyph_groups):
        if not indices:
            continue
        glyph_block = TextBlock(strokes=tuple(block.strokes[i] for i in indices))
        bounds = glyph_block.bounds()
        if bounds is None:  # pragma: no cover - empty groups are skipped above
            continue
        ink = _glyph_ink(block, indices)
        # Ink shallower than the emission floor is not a descender: it does
        # not exist in the emitted PLT, so it must not trigger the relief
        # (the ramp's basis is this depth, and ``descends`` gates the pair).
        descender = max(0.0, -bounds[1])
        if descender <= _INK_EMISSION_FLOOR:
            descender = 0.0
        boxes.append(
            GlyphBox(
                glyph_index=glyph_index,
                char=chars[glyph_index],
                bounds=bounds,
                descender=descender,
                ink=ink,
                sub_baseline_ink=_sub_baseline_ink(ink),
            )
        )
    return boxes


def _line_cutter(label: ResolvedLabel, line_index: int) -> float:
    """Return the widest cutter one content line engraves with (0.0 if OOB).

    A ``<b>`` line engraves part of its ink with the thicker bold cutter, and a
    ``<scale:N>`` line with a large ``N`` engraves part with a cutter larger
    than the line's own, so the stroke-aware clearance floors use the line's
    **widest** tool. This is deliberately conservative: a plain descender on a
    wide-tool-carrying line asks for a touch more air than its own stroke
    strictly needs, which can never engrave a collision.

    Args:
        label: The label being rendered.
        line_index: Index into ``label.content``.

    Returns:
        The line's largest cutter diameter in inches.
    """
    if 0 <= line_index < len(label.content):
        return max(label.content[line_index].used_cutters)
    return 0.0  # pragma: no cover - renderable indices always address content


def _band_center_delta(
    offsets: Sequence[float],
    ascenders: Sequence[float],
    pitch_basis: Sequence[float],
    *,
    by_content: bool,
) -> float:
    """Return the shift from the ink-centered frame to the cap-band-centered frame.

    The **band** spans the lowest baseline to the highest capline; centering it
    (instead of the ink block) puts the visible text in the optical middle of
    the label and lets descenders hang into the bottom margin, where the
    descending-character relief handles them against the bottom border. The
    ink-centered frame already centers ``[min(b - d), max(b + a)]`` on ``y = 0``,
    so the band-centered frame is that frame shifted down by the band's own
    center in it.

    Args:
        offsets: Each line's baseline Y in the unshifted (first-baseline-at-0)
            frame, inches.
        ascenders: Ink extent above each line's baseline, inches.
        pitch_basis: Each line's cap height, inches.
        by_content: When ``True`` the band top is the tallest measured ink
            above the baseline (so a line of x-height letters sits lower); when
            ``False`` it is the cap height, a content-independent per-font
            constant.

    Returns:
        The band's center in the ink-centered frame, inches (``> 0`` when
        descenders hang below the lowest baseline).
    """
    if not offsets:
        return 0.0
    tops = (
        (b + a for b, a in zip(offsets, ascenders))
        if by_content
        else (b + p for b, p in zip(offsets, pitch_basis))
    )
    band_top = max(tops)
    band_bottom = min(offsets)
    return (band_top + band_bottom) / 2.0


def _baseline_table(
    pitch_basis: Sequence[float],
    ascenders: Sequence[float],
    descenders: Sequence[float],
    spacings: Sequence[float],
    *,
    center_by_band: bool = False,
    by_content: bool = True,
) -> List[float]:
    """Return each line's baseline Y in the block-centered frame.

    The shared baseline table: baselines descend by one pitch each from ``0``
    and the whole set shifts so the ink block (descenders included) centers on
    ``y = 0`` -- exactly the frame the renderer positions lines in. With
    ``center_by_band`` the block is shifted the extra amount that centers the
    baseline-to-capline band instead (see :func:`_band_center_delta`), which is
    what lets descenders hang into the bottom margin for the relief to fix.

    Args:
        pitch_basis: Each line's cap height (the pitch basis), inches.
        ascenders: Ink extent above each line's baseline, inches.
        descenders: Ink depth below each line's baseline, inches.
        spacings: Spacing below each line except the last, inches.
        center_by_band: Center the baseline-to-capline band, not the ink block.
        by_content: Band top from the measured ascender (``True``) or the cap
            height (``False``). Ignored unless ``center_by_band``.

    Returns:
        One baseline Y per line, ``+y`` up, block-centered.
    """
    offsets = baseline_offsets(pitch_basis, spacings)
    top_extent = max(b + a for b, a in zip(offsets, ascenders))
    bottom_extent = min(b - d for b, d in zip(offsets, descenders))
    shift = (top_extent + bottom_extent) / 2.0
    if center_by_band:
        shift += _band_center_delta(
            [offset - shift for offset in offsets],
            ascenders,
            pitch_basis,
            by_content=by_content,
        )
    return [offset - shift for offset in offsets]


def _inkbox_baseline_table(
    blocks: Sequence[TextBlock],
    spacings: Sequence[float],
    *,
    pitch_basis: Sequence[float] = (),
    center_by_band: bool = False,
    by_content: bool = True,
) -> List[float]:
    """Return each line's baseline Y under ink-box stacking, block-centered.

    Mirrors the renderer's running anchor exactly (ink top at ``current_y``,
    descending by ``height + spacing``), so the relief measures the gaps the
    ink-box mode will actually engrave. ``center_by_band`` applies the same
    baseline-to-capline band centering as :func:`_baseline_table` (the running
    anchor is untouched -- only the block's absolute position moves).

    Args:
        blocks: The line blocks in the local frame (baseline at ``y = 0``).
        spacings: Spacing below each line except the last, inches.
        pitch_basis: Each line's cap height, inches (the band top when
            ``by_content`` is False).
        center_by_band: Center the baseline-to-capline band, not the ink block.
        by_content: Band top from the measured ascender (``True``) or the cap
            height (``False``).

    Returns:
        One baseline Y per line, ``+y`` up, block-centered on ``y = 0``.
    """
    heights: List[float] = []
    tops: List[float] = []
    for block in blocks:
        bounds = block.bounds()
        if bounds is None:  # pragma: no cover - callers skip empty blocks
            heights.append(0.0)
            tops.append(0.0)
            continue
        heights.append(bounds[3] - bounds[1])
        tops.append(bounds[3])
    current_y = (sum(heights) + sum(spacings)) / 2.0
    baselines: List[float] = []
    for index, top in enumerate(tops):
        baselines.append(current_y - top)
        current_y -= heights[index]
        if index < len(spacings):
            current_y -= spacings[index]
    if center_by_band:
        # ``tops`` is each line's ink extent above its baseline (the blocks are
        # in the baseline frame), i.e. exactly the band's ascender input.
        baselines = [
            b - _band_center_delta(baselines, tops, pitch_basis, by_content=by_content)
            for b in baselines
        ]
    return baselines


def _local_line_extents(
    blocks: Sequence[TextBlock],
    pitch_basis: Sequence[float],
) -> List[Tuple[float, float, float]]:
    """Refresh the per-line baseline geometry from local-frame blocks.

    A relief scales a glyph about its baseline and lifts it, so both the
    ascender and the descender of the *line* can move. The blocks stay in the
    baseline frame, so the bounds give the extents directly -- the same reading
    the first render pass uses.

    Args:
        blocks: The line blocks in the local frame (baseline at ``y = 0``).
        pitch_basis: Each line's cap height (unchanged by the relief).

    Returns:
        One ``(pitch_basis, ascender, descender)`` triple per line.
    """
    geometry: List[Tuple[float, float, float]] = []
    for index, block in enumerate(blocks):
        basis = pitch_basis[index] if index < len(pitch_basis) else 0.0
        bounds = block.bounds()
        if bounds is None:  # pragma: no cover - callers skip empty blocks
            geometry.append((basis, 0.0, 0.0))
            continue
        geometry.append((basis, max(0.0, bounds[3]), max(0.0, -bounds[1])))
    return geometry


def _shifted_boxes(boxes: Sequence[GlyphBox], baseline_y: float) -> List[GlyphBox]:
    """Shift local-frame glyph boxes into the stacked frame.

    Pairwise gaps must be measured where the glyphs will actually be engraved,
    i.e. after the line's baseline has been placed at ``baseline_y``. The
    relief itself stays in the local frame, where the scale pivot is ``y = 0``.

    Args:
        boxes: Glyph boxes measured in the local frame.
        baseline_y: The line's baseline Y in the stacked frame, inches.

    Returns:
        The same glyphs with their bounds and ink translated by ``baseline_y``.
    """
    if baseline_y == 0.0:
        return list(boxes)
    return [
        replace(
            box,
            bounds=(
                box.bounds[0],
                box.bounds[1] + baseline_y,
                box.bounds[2],
                box.bounds[3] + baseline_y,
            ),
            ink=tuple(tuple((x, y + baseline_y) for x, y in polyline) for polyline in box.ink),
            sub_baseline_ink=tuple(
                tuple((x, y + baseline_y) for x, y in polyline) for polyline in box.sub_baseline_ink
            ),
        )
        for box in boxes
    ]


def _apply_glyph_ramps(
    block: TextBlock,
    glyph_groups: Tuple[Tuple[int, ...], ...],
    text: str,
    ramps: Mapping[int, float],
    v_scale: float,
    raise_fraction: float,
    reciprocal_ramps: Optional[Mapping[int, float]] = None,
    reciprocal_scale: float = 1.0,
) -> Tuple[TextBlock, List[Tuple[int, str, float, float, float]]]:
    """Scale and raise the triggered glyphs of one line (local frame).

    Each glyph is transformed in isolation -- Y-scaled about the line's
    baseline (``y = 0`` in the local frame), then translated up -- and spliced
    back into the line's block at its original stroke indices. Stroke order and
    count are preserved, so the line's per-glyph partition (and the plate-space
    chunk records built from it) stays valid.

    A glyph can carry **both** ramps: a middle line's glyph may descend into the
    gap below it (its own upper ramp) while also colliding with the descender
    above it (a reciprocal ramp). The upper ramp is applied first, then the
    reciprocal scale about the glyph's resulting lowest ink point, so the
    bottom stays put while the top descends.

    Args:
        block: The line's block, baseline at ``y = 0``.
        glyph_groups: One stroke-index tuple per rendered character.
        text: The line's text (for the character names).
        ramps: ``{glyph_index: ramp}`` for the glyphs to relieve.
        v_scale: Smallest permitted vertical scale.
        raise_fraction: Largest permitted raise fraction.
        reciprocal_ramps: ``{glyph_index: ramp}`` for the glyphs to scale
            top-down (they collide with a descender on the line above).
        reciprocal_scale: Smallest permitted top-down scale
            (``descending_character_reciprocal_scale``).

    Returns:
        ``(new_block, applied)`` where ``applied`` lists
        ``(glyph_index, char, scale_y, raise_amount, reciprocal_scale_y)`` per
        relieved glyph (``reciprocal_scale_y`` is ``1.0`` for a pure upper ramp).
    """
    if not ramps and not reciprocal_ramps:
        return block, []
    recip_map: Mapping[int, float] = reciprocal_ramps or {}
    chars = _glyph_chars(text)
    replacements: dict[int, Stroke] = {}
    applied: List[Tuple[int, str, float, float, float]] = []
    for glyph_index, indices in enumerate(glyph_groups):
        ramp = ramps.get(glyph_index, 0.0)
        recip_ramp = recip_map.get(glyph_index, 0.0)
        if (ramp <= 0.0 and recip_ramp <= 0.0) or not indices:
            continue
        glyph_block = TextBlock(strokes=tuple(block.strokes[i] for i in indices))
        bounds = glyph_block.bounds()
        if bounds is None:  # pragma: no cover - empty groups are skipped above
            continue
        descender = max(0.0, -bounds[1])
        scale_y, raise_amount = apply_ramp(descender, v_scale, raise_fraction, ramp)
        transformed = glyph_block.scaled_y(scale_y).translate(0.0, raise_amount)
        recip_scale_y = reciprocal_scale_y(recip_ramp, reciprocal_scale)
        if recip_scale_y < 1.0:
            # Top-down about the (post-raise) lowest ink point: the bottom is
            # the anchor, so it never moves and the top descends toward it.
            new_bounds = transformed.bounds()
            if new_bounds is not None:
                anchor = new_bounds[1]
                transformed = (
                    transformed.translate(0.0, -anchor)
                    .scaled_y(recip_scale_y)
                    .translate(0.0, anchor)
                )
        for position, stroke_index in enumerate(indices):
            replacements[stroke_index] = transformed.strokes[position]
        applied.append((glyph_index, chars[glyph_index], scale_y, raise_amount, recip_scale_y))
    return block.replaced_strokes(replacements), applied


def _gap_required(
    label: ResolvedLabel,
    line_indices: Sequence[int],
    gap_index: int,
) -> float:
    """Return the required clearance of one inter-line gap, inches.

    Args:
        label: The label being rendered (supplies the clearance factor).
        line_indices: Each renderable line's index into ``label.content``.
        gap_index: The gap below renderable line ``gap_index``.

    Returns:
        The clearance from :func:`required_gap` for the gap's two cutters.
    """
    return required_gap(
        _line_cutter(label, line_indices[gap_index]),
        _line_cutter(label, line_indices[gap_index + 1]),
        label.descending_character_clearance,
    )


def _border_required(label: ResolvedLabel, line_index: int) -> float:
    """Return the clearance a descending glyph must keep from the bottom border.

    The same stroke-aware floor as the line-to-line trigger, with the border's
    cutter (the snapped boundary/hole tool) in place of the following line's:
    ``0.5 * (text_cutter + hole_cutter) * (1 + clearance)``.

    Args:
        label: The label being rendered (supplies both cutters and the
            clearance factor).
        line_index: The glyph's index into ``label.content``.

    Returns:
        The required clearance, inches.
    """
    return required_gap(
        _line_cutter(label, line_index),
        label.hole_cutter_diameter,
        label.descending_character_clearance,
    )


def _border_box(label: ResolvedLabel) -> GlyphBox:
    """Return the label's bottom border as a virtual "following line".

    The border rectangle is drawn at the label edge (``vp.rect(0, 0, width,
    height)``, no inset), so its bottom edge is the label-local line
    ``y = 0``. The relief's stacked frame centres the text block on ``y = 0``
    at label ``height / 2``, which puts that edge at ``-height / 2`` -- the
    same constant in both stacking modes.

    The box carries the edge as one polyline spanning the label width, so the
    existing rolling-circle measurement
    (:func:`~plt_optimizer.generate.descender_adjust.measure_glyph_gaps`)
    treats it exactly like the ink of a line below: a descending glyph
    collides when its sub-baseline ink comes within the required clearance of
    the engraved border stroke. Crossing the (unrelated) ``v_margin`` is not a
    collision and is never measured here.

    A ``round`` label's bottom boundary is the ellipse's **lower arc**, not a
    flat edge, so the box carries that arc (sampled at the shared chord
    tolerance) in the stacked frame -- centre ``(width / 2, 0)``, the bottom
    vertex at ``-height / 2``, rising to the side vertices at ``y = 0``. A
    descender near the label's left/right edge therefore measures against the
    sharply rising boundary, exactly as it engraves.

    Args:
        label: The label being rendered (supplies the material dimensions).

    Returns:
        A :class:`GlyphBox` whose ink is the bottom border centreline.
    """
    if _label_is_round(label):
        arc = _lower_ellipse_arc(label.width, label.height)
        return GlyphBox(
            glyph_index=_BORDER_GLYPH_INDEX,
            char="",
            bounds=(arc[0][0], min(y for _x, y in arc), arc[-1][0], max(y for _x, y in arc)),
            descender=0.0,
            ink=(arc,),
            sub_baseline_ink=(),
        )

    border_y = -label.height / 2.0
    polyline: Polyline = ((0.0, border_y), (label.width, border_y))
    return GlyphBox(
        glyph_index=_BORDER_GLYPH_INDEX,
        char="",
        bounds=(0.0, border_y, label.width, border_y),
        descender=0.0,
        ink=(polyline,),
        sub_baseline_ink=(),
    )


def _solve_gap_growth(
    upper_boxes: Sequence[GlyphBox],
    lower_boxes: Sequence[GlyphBox],
    upper_baseline_y: float,
    required: float,
    deficit: float,
) -> float:
    """Return the spacing growth that clears one inter-line gap's ink distance.

    Growing the spacing below a line pushes the line below (and everything
    under it) away, so the sweep-ink gap widens -- but **sub-linearly**: a
    pair offset sideways gains less than the vertical distance it is given
    (``sqrt(dx^2 + (dy+g)^2)``), so growing by the deficit under-shoots and
    the fixpoint stalls with a geometric tail of residuals. This bisects the
    growth on the measured gap instead.

    The lower line's ink is indexed once; each candidate growth is measured
    by translating the *query* ink up by that growth (identical geometry,
    grid frozen), which keeps the bisection cheap on lines of hundreds of
    glyphs.

    Args:
        upper_boxes: The upper line's glyph boxes (stacked frame).
        lower_boxes: The following line's glyph boxes (stacked frame).
        upper_baseline_y: The upper line's baseline Y, inches (the ramp pivot).
        required: Clearance from :func:`required_gap`, inches.
        deficit: The gap's clearance shortfall, inches (> 0).

    Returns:
        The growth in inches that clears every descending glyph of the gap
        (the smallest value found, within the emission tolerance). ``0.0``
        when the gap has no ink-measurable glyph to clear.
    """
    lower_ink: Ink = tuple(polyline for lower in lower_boxes for polyline in lower.ink)
    if not lower_ink or required <= 0.0:
        return 0.0
    grid = InkGrid(lower_ink, required)
    queries: List[Ink] = [
        box.sub_baseline_ink for box in upper_boxes if box.sub_baseline_ink and box.descends
    ]
    if not queries:
        return 0.0

    def clears(growth: float) -> bool:
        return all(
            grid.min_gap(ramp_ink(query, upper_baseline_y, 1.0, growth)) >= required
            for query in queries
        )

    # The growth a pair needs is bounded by the required clearance itself
    # (a purely lateral near-miss clears at ~required), so doubling from the
    # deficit reaches a clearing value within a few steps.
    high = max(deficit, _SPACING_GROWTH_TOLERANCE)
    for _ in range(_SPACING_GROWTH_MAX_DOUBLINGS):
        if clears(high):
            break
        high *= 2.0
    else:
        return high

    low = 0.0
    for _ in range(_SPACING_GROWTH_MAX_ITERATIONS):
        if high - low <= _SPACING_GROWTH_TOLERANCE:
            break
        middle = (low + high) / 2.0
        if clears(middle):
            high = middle
        else:
            low = middle
    return high


def _grow_spacings(
    spacings: Sequence[float],
    growths: Sequence[float],
    available_height: float,
    height_at: Callable[[Sequence[float]], float],
) -> List[float]:
    """Grow spacings by their needed amounts, capped by the inner content height.

    Growing the gap below line ``i`` moves that line's whole ink block (and
    every line under it) away from line ``i``, so the growth a glyph-level gap
    needs is exactly its residual deficit -- in both stacking modes. Growing
    widens the block, and the vertical margins outrank the mechanism: the
    growths are scaled down by one common factor (bisection, the block height
    being non-decreasing in the spacings) so the block still fits
    ``available_height``. Whatever shortfall survives is reported.

    Args:
        spacings: Spacing below each line except the last, inches.
        growths: Clearance shortfall per gap, inches.
        available_height: Inner content height, inches.
        height_at: The stacked block's ink height for a spacing list (mode-
            specific: baseline pitch geometry, or ink boxes plus spacings).

    Returns:
        A new list of spacings. Equal to the input when every growth is zero.
    """
    needed = [max(0.0, float(growth)) for growth in growths]
    base = [max(0.0, float(spacing)) for spacing in spacings]
    if all(growth <= 0.0 for growth in needed):
        return base

    def height_with(factor: float) -> float:
        return height_at([spacing + growth * factor for spacing, growth in zip(base, needed)])

    if height_with(1.0) <= available_height:
        return [spacing + growth for spacing, growth in zip(base, needed)]

    factor = _solve_spacing_growth_factor(height_with, available_height)
    return [spacing + growth * factor for spacing, growth in zip(base, needed)]


def _solve_spacing_growth_factor(
    height_with: Callable[[float], float],
    available_height: float,
) -> float:
    """Bisect the largest common growth factor whose block still fits.

    Args:
        height_with: The stacked block's ink height at a growth factor
            (non-decreasing in the factor).
        available_height: Inner content height, inches.

    Returns:
        The largest factor in ``[0, 1]`` with ``height_with(factor) <=
        available_height``, within ``1e-9``. ``0.0`` when even bare spacings
        overflow (the margins already leave no room to grow).
    """
    lower = 0.0
    upper = 1.0
    if height_with(lower) >= available_height:
        return 0.0
    for _ in range(_SPACING_GROWTH_MAX_ITERATIONS):
        middle = (lower + upper) / 2.0
        if upper - lower <= _SPACING_GROWTH_TOLERANCE:
            break
        if height_with(middle) <= available_height:
            lower = middle
        else:
            upper = middle
    return lower


def _apply_descender_relief(
    label: ResolvedLabel,
    blocks: Sequence[TextBlock],
    glyph_groups_by_line: Sequence[Tuple[Tuple[int, ...], ...]],
    line_indices: Sequence[int],
    pitch_basis: Sequence[float],
    spacings: Sequence[float],
    available_height: float,
    *,
    use_baseline: bool,
    center_by_band: bool = False,
    by_content: bool = True,
) -> _DescenderRelief:
    """Relieve descending glyphs that come close to the glyph below them.

    A monotone fixpoint, run in the renderers' local frame (each block carries
    its own baseline at ``y = 0``). Every pass:

    1. places the line baselines with the current spacings (the same tables the
       caller stacks by, so the relief measures what will be engraved);
    2. measures every (descending glyph, following-line glyph) pair over its
       overlapping X span and gives each triggering glyph the smallest ramp
       that clears its tightest pair, merged one-way into the glyph's ramp (a
       glyph only ever gets smaller and higher);
    3. gives every colliding partner on the following line the SAME ramp, spent
       as a top-down downscale about its own lowest ink point
       (``descending_character_reciprocal_scale``) -- the deficit is shared
       between the two lines in proportion to their allowances;
    4. rebuilds each line's block from the ORIGINAL render with the glyph's
       full ramp (idempotent -- a pass never compounds a glyph's transform);
    5. grows each gap by the deficit that survives the ramps, capped by the
       inner content height.

    Ramps only grow and are capped at ``1.0``; spacings only grow and are
    capped by the inner height, so the loop converges. The pass cap is a
    float-noise guard.

    A glyph can carry both ramps at once (a middle line's ``y`` descends into
    the gap below while its ascender is squeezed by the ``g`` above): the upper
    ramp is applied first, then the top-down scale about the glyph's resulting
    lowest point. Because that anchor is the glyph's lowest ink, a reciprocal
    fix never moves ink down past it, so the gap above the line widens
    monotonically and the bottom border is untouched.

    Args:
        label: The label being rendered (supplies the allowances and cutters).
        blocks: The rendered line blocks, baseline at ``y = 0``, in
            renderable-line order.
        glyph_groups_by_line: Per-line per-glyph stroke partitions.
        line_indices: Each block's index into ``label.content`` (the relief
            names lines by their content index, like the collision reports).
        pitch_basis: Each line's cap height (the baseline pitch basis), inches.
        spacings: Margin-clamped spacings, inches.
        available_height: Inner content height, inches.
        use_baseline: Which stacking mode the caller uses (selects the baseline
            table and the block-height model the growth is capped by).
        center_by_band: The caller centers the block by its baseline-to-capline
            band; the relief's stacked frame matches (the border sits at a fixed
            ``-height/2``, so the frame must agree for the border measurement).
        by_content: Band top from the measured ascender (``True``) or the cap
            height (``False``).

    Returns:
        A :class:`_DescenderRelief` carrying the adjusted blocks, spacings and
        geometry, the applied reliefs, and the per-gap residual shortfalls.
    """
    original = list(blocks)
    current_spacings = [max(0.0, float(s)) for s in spacings]
    geometry = _local_line_extents(original, pitch_basis)
    # Feature gate: disabled labels engrave descenders exactly as rendered.
    if not label.auto_descending_character_adjustment:
        return _DescenderRelief(
            blocks=original,
            heights=[
                (bounds[3] - bounds[1]) if (bounds := block.bounds()) is not None else 0.0
                for block in original
            ],
            spacings=current_spacings,
            geometry=geometry,
            fixes=[],
            residual=[],
        )
    # Per-glyph ramps already applied (one-way; a glyph never shrinks back).
    ramps: dict[int, dict[int, float]] = {index: {} for index in range(len(original))}
    # Per-glyph RECIPROCAL ramps (a following-line glyph scaled top-down in
    # share of the relief). One-way for the same convergence reason.
    recip_ramps: dict[int, dict[int, float]] = {index: {} for index in range(len(original))}
    # Tightest gap each glyph had before any relief, and after the last pass.
    gap_before: dict[tuple[int, int], float] = {}
    gap_after: dict[tuple[int, int], float] = {}
    # Reciprocal-role reporting, keyed (gap_index, LOWER glyph index): the
    # pair's clearance before any relief, and the descending glyph whose
    # collision pulled this partner in (for the final re-measurement).
    recip_gap_before: dict[tuple[int, int], float] = {}
    recip_pair_upper: dict[tuple[int, int], int] = {}
    # Every (line, glyph) that ever triggered against the bottom border, so a
    # glyph whose allowances cannot help at all (ramp 0) is still reported.
    border_triggered: dict[int, set[int]] = {index: set() for index in range(len(original))}
    deficits = [0.0] * max(0, len(original) - 1)

    def height_at(spacing_list: Sequence[float]) -> float:
        ascender_values = [ascender for _basis, ascender, _d in geometry]
        descender_values = [descender for _basis, _ascender, descender in geometry]
        if use_baseline:
            # Same block model as the margin clamp: a band-centered label caps
            # spacing growth against the cap band, not the descender-extended
            # ink (the relief's growth must not be limited by a model the
            # clamp no longer enforces).
            return baseline_block_height(
                list(pitch_basis),
                ascender_values,
                descender_values,
                list(spacing_list),
                charge_descenders=not center_by_band,
            )
        ink_height = sum(
            bounds[3] - bounds[1]
            for block in current_blocks
            if (bounds := block.bounds()) is not None
        )
        return ink_height + sum(spacing_list)

    current_blocks: List[TextBlock] = list(original)
    # Glyph boxes of the untouched render: the ramp's basis (its descender
    # depth) is always the pre-relief one, which is also what
    # ``_apply_glyph_ramps`` rebuilds from, so the reported scale/raise match
    # the geometry actually emitted.
    original_boxes: List[List[GlyphBox]] = [
        _glyph_boxes(block, groups, label.content[line_index].plain_text)
        for block, groups, line_index in zip(original, glyph_groups_by_line, line_indices)
    ]

    def measure(
        boxes: Sequence[Sequence[GlyphBox]],
    ) -> tuple[List[List[GlyphGap]], List[float], List[List[GlyphGap]]]:
        """Measure every descending glyph of the given stacked-frame boxes.

        The rolling-circle test: each descending glyph's sub-baseline ink
        against the *complete* ink of the line below (grid-indexed per gap,
        cell = required clearance, keeping the ``gap < required`` decision
        exact). Glyphs without ink knowledge fall back to the box model.

        The bottom border is measured the same way, as a virtual "following
        line" (see :func:`_border_box`): the engraved descender stroke against
        the engraved border stroke. The label's ``v_margin`` is NOT a
        constraint -- ink crossing the margin is accepted, only the border
        stroke matters.

        Args:
            boxes: Each line's glyph boxes in the stacked frame.

        Returns:
            ``(gaps_by_gap, deficits, border_gaps)``: the per-glyph gaps of
            each inter-line gap, the worst clearance shortfall of each, and
            every line's per-glyph gaps against the bottom border.
        """
        gaps_by_gap: List[List[GlyphGap]] = []
        gap_deficits: List[float] = []
        for gap_index in range(len(original) - 1):
            required = required_gap(
                _line_cutter(label, line_indices[gap_index]),
                _line_cutter(label, line_indices[gap_index + 1]),
                label.descending_character_clearance,
            )
            gaps = measure_glyph_gaps(boxes[gap_index], boxes[gap_index + 1], required)
            gaps_by_gap.append(gaps)
            gap_deficits.append(worst_glyph_deficit(gaps))
        border_box = _border_box(label)
        border_gaps: List[List[GlyphGap]] = [
            measure_glyph_gaps(
                boxes[index], [border_box], _border_required(label, line_indices[index])
            )
            for index in range(len(original))
        ]
        return gaps_by_gap, gap_deficits, border_gaps

    def stacked_boxes_of(
        blocks: Sequence[TextBlock], baselines: Sequence[float]
    ) -> List[List[GlyphBox]]:
        """Build the stacked-frame glyph boxes of one set of line blocks."""
        return [
            _shifted_boxes(
                _glyph_boxes(block, groups, label.content[line_index].plain_text),
                baselines[index],
            )
            for index, (block, groups, line_index) in enumerate(
                zip(blocks, glyph_groups_by_line, line_indices)
            )
        ]

    def propose_ramp(
        gap: GlyphGap,
        grid: Optional[InkGrid],
        upper_baseline: float,
        current_ramp: float,
        partners: Sequence[GlyphBox] = (),
    ) -> float:
        """Return the total ramp clearing one colliding glyph, in [0, 1].

        The lower ink is frozen during the bisection, so the caller indexes the
        **static** part of it once and every candidate ramp queries the same
        grid -- cell = required, which keeps the ``gap < required`` decision
        exact. The glyph's **reciprocal partners** (the following-line glyphs
        that share its collision and shrink top-down with the same ramp) cannot
        live in a frozen grid: they move. The caller excludes their ink from the
        grid and passes them here, and every candidate ramp measures them
        directly (:func:`~plt_optimizer.generate.geometry.ink_gap` against
        their scaled ink) -- a handful of glyphs, so the exact per-partner
        measurement stays cheap.

        The measured glyph ink is the CURRENT (already relieved) ink, so the
        bisected value is the ramp the glyph still needs ON TOP of
        ``current_ramp``. ``_apply_glyph_ramps`` rebuilds from the ORIGINAL
        render at the glyph's TOTAL ramp, so the increment is added back here
        -- storing the bare increment would under-apply the relief (the
        line-to-line path hides that behind spacing growth; the bottom border
        has no growth lever, so it would engrave short of the clearance).

        Args:
            gap: The measured (glyph, lower ink) proximity.
            grid: Grid indexing the lower ink at the gap's required clearance
                (``None`` when the pair is not ink-measurable).
            upper_baseline: The glyph's line baseline Y in the stacked frame.
            current_ramp: The ramp the glyph already carries, in ``[0, 1]``.

        Returns:
            The total ramp in ``[0, 1]``; ``current_ramp`` when no further
            allowance can help.
        """
        if gap.upper.sub_baseline_ink and (grid is not None or partners):
            descender = gap.upper.descender
            full_scale, full_raise = apply_ramp(
                descender,
                label.descending_character_v_scale,
                label.descending_character_raise,
                1.0,
            )
            recip_allowed = label.descending_character_reciprocal_scale < 1.0
            if full_scale >= 1.0 and full_raise <= 0.0 and not (recip_allowed and partners):
                # Every allowance is zero: the ramp can never help (the box
                # model's closed form tests this as its slope).
                return current_ramp

            def gap_at(
                t: float,
                _ink: Ink = gap.upper.sub_baseline_ink,
                _descender: float = descender,
                _grid: Optional[InkGrid] = grid,
                _baseline: float = upper_baseline,
                _partners: Sequence[GlyphBox] = partners,
            ) -> float:
                scale_y, raise_amount = apply_ramp(
                    _descender,
                    label.descending_character_v_scale,
                    label.descending_character_raise,
                    t,
                )
                upper_ink = ramp_ink(_ink, _baseline, scale_y, raise_amount)
                best = _grid.min_gap(upper_ink) if _grid is not None else math.inf
                recip = reciprocal_scale_y(t, label.descending_character_reciprocal_scale)
                for partner in _partners:
                    anchor = partner.bounds[1]
                    nearest = ink_gap(upper_ink, recip_ink(partner.ink, anchor, recip))
                    if nearest < best:
                        best = nearest
                return best

            increment = ramp_for_measured(gap_at, gap.required)
        else:
            # Box-model fallback (glyphs without ink knowledge): the vertical
            # gap is linear in the ramp, closed form. The tightest partner's
            # ink height feeds the reciprocal term of the slope.
            partner_height = max(
                (partner.bounds[3] - partner.bounds[1] for partner in partners),
                default=0.0,
            )
            increment = ramp_for(
                gap.gap,
                gap.required,
                gap.upper.descender,
                label.descending_character_v_scale,
                label.descending_character_raise,
                partner_height,
                label.descending_character_reciprocal_scale,
            )
        return min(1.0, current_ramp + increment)

    for _pass in range(_DESCENDER_MAX_PASSES):
        ascenders = [ascender for _basis, ascender, _d in geometry]
        descenders = [descender for _basis, _ascender, descender in geometry]
        if use_baseline:
            baselines = _baseline_table(
                pitch_basis,
                ascenders,
                descenders,
                current_spacings,
                center_by_band=center_by_band,
                by_content=by_content,
            )
        else:
            baselines = _inkbox_baseline_table(
                current_blocks,
                current_spacings,
                pitch_basis=pitch_basis,
                center_by_band=center_by_band,
                by_content=by_content,
            )

        # ---- Measure every descending glyph; propose the smallest clearing ramp ----
        boxes = stacked_boxes_of(current_blocks, baselines)
        gaps_by_gap, _pre_deficits, border_gaps = measure(boxes)
        proposed: dict[int, dict[int, float]] = {}
        proposed_recip: dict[int, dict[int, float]] = {}
        for gap_index, gaps in enumerate(gaps_by_gap):
            triggering = [gap for gap in gaps if gap.collides and math.isfinite(gap.gap)]
            if not triggering:
                continue
            required = triggering[0].required
            upper_baseline = baselines[gap_index]
            # The reciprocal half: the following-line glyphs that share a
            # triggering descender's collision shrink top-down with the SAME
            # ramp, so their ink cannot live in the frozen grid the bisection
            # queries. Their ink is excluded from the grid and measured
            # directly per candidate ramp (see propose_ramp).
            partners_by_gap_glyph: dict[int, tuple[int, ...]] = {}
            if label.descending_character_reciprocal_scale < 1.0:
                for gap in triggering:
                    hits = colliding_partners(gap.upper, boxes[gap_index + 1], required)
                    if hits:
                        partners_by_gap_glyph[gap.upper.glyph_index] = hits
            shaken = {index for hits in partners_by_gap_glyph.values() for index in hits}
            lower_by_index = {lower.glyph_index: lower for lower in boxes[gap_index + 1]}
            static_ink: Ink = tuple(
                polyline
                for lower in boxes[gap_index + 1]
                if lower.glyph_index not in shaken
                for polyline in lower.ink
            )
            grid = InkGrid(static_ink, required) if static_ink and required > 0.0 else None
            for gap in triggering:
                key = (gap_index, gap.upper.glyph_index)
                if key not in gap_before or gap.gap < gap_before[key]:
                    gap_before[key] = gap.gap
                partner_boxes = tuple(
                    lower_by_index[index]
                    for index in partners_by_gap_glyph.get(gap.upper.glyph_index, ())
                )
                ramp = propose_ramp(
                    gap,
                    grid,
                    upper_baseline,
                    ramps[gap_index].get(gap.upper.glyph_index, 0.0),
                    partner_boxes,
                )
                if ramp > 0.0:
                    proposed.setdefault(gap_index, {})[gap.upper.glyph_index] = ramp
                # The partners ride the SAME ramp (proportional sharing), so
                # the pair's deficit is distributed between the two lines in
                # proportion to their allowances instead of landing on one.
                for partner in partner_boxes:
                    p_key = (gap_index, partner.glyph_index)
                    if p_key not in recip_gap_before or gap.gap < recip_gap_before[p_key]:
                        recip_gap_before[p_key] = gap.gap
                        recip_pair_upper[p_key] = gap.upper.glyph_index
                    proposed_recip.setdefault(gap_index + 1, {})[partner.glyph_index] = ramp

        # ---- The bottom border: the same test against the border stroke ----
        # The border is a virtual "following line" (see _border_box), so the
        # trigger, the ramp and the one-way merge are identical. Spacing growth
        # deliberately does NOT see these deficits: growing the gap above the
        # last line moves that line DOWN in the centred frame, which deepens a
        # border collision, so the ramp is the only lever here.
        border = _border_box(label)
        for line_index, gaps in enumerate(border_gaps):
            triggering = [gap for gap in gaps if gap.collides and math.isfinite(gap.gap)]
            if not triggering:
                continue
            required = triggering[0].required
            border_grid = InkGrid(border.ink, required) if required > 0.0 else None
            for gap in triggering:
                key = (line_index, gap.upper.glyph_index)
                border_triggered[line_index].add(gap.upper.glyph_index)
                if key not in gap_before or gap.gap < gap_before[key]:
                    gap_before[key] = gap.gap
                ramp = propose_ramp(
                    gap,
                    border_grid,
                    baselines[line_index],
                    ramps[line_index].get(gap.upper.glyph_index, 0.0),
                )
                if ramp > 0.0:
                    proposed.setdefault(line_index, {})[gap.upper.glyph_index] = ramp

        grew = False
        for gap_index, per_glyph in proposed.items():
            before = ramps[gap_index]
            after = merge_ramps(before, per_glyph)
            if ramps_changed(before, after):
                grew = True
            ramps[gap_index] = after
        for line_index, per_glyph in proposed_recip.items():
            before = recip_ramps[line_index]
            after = merge_ramps(before, per_glyph)
            if ramps_changed(before, after):
                grew = True
            recip_ramps[line_index] = after

        # ---- Rebuild each line from the original render at its full ramp ----
        current_blocks = []
        for index, block in enumerate(original):
            per_glyph = ramps.get(index, {})
            per_glyph_recip = recip_ramps.get(index, {})
            if not per_glyph and not per_glyph_recip:
                current_blocks.append(block)
                continue
            rebuilt, _applied = _apply_glyph_ramps(
                block,
                glyph_groups_by_line[index],
                label.content[line_indices[index]].plain_text,
                per_glyph,
                label.descending_character_v_scale,
                label.descending_character_raise,
                per_glyph_recip,
                label.descending_character_reciprocal_scale,
            )
            current_blocks.append(rebuilt)
        geometry = _local_line_extents(current_blocks, pitch_basis)

        # ---- Grow each gap by the shortfall the RAMPS left behind ----
        # Measured after the rebuild: growing by the pre-ramp deficit would
        # widen gaps the relief already cleared.
        ascenders = [ascender for _basis, ascender, _d in geometry]
        descenders = [descender for _basis, _ascender, descender in geometry]
        if use_baseline:
            baselines_after = _baseline_table(
                pitch_basis,
                ascenders,
                descenders,
                current_spacings,
                center_by_band=center_by_band,
                by_content=by_content,
            )
        else:
            baselines_after = _inkbox_baseline_table(
                current_blocks,
                current_spacings,
                pitch_basis=pitch_basis,
                center_by_band=center_by_band,
                by_content=by_content,
            )
        boxes_after = stacked_boxes_of(current_blocks, baselines_after)
        # Line-to-line deficits only: the border's third return value is
        # deliberately dropped here (see the border proposal above).
        _post_gaps, deficits, _post_border_gaps = measure(boxes_after)
        if any(deficit > _DESCENDER_GAP_TOLERANCE for deficit in deficits):
            # Measured, not deficit-derived: the ink distance widens
            # sub-linearly with the spacing, so growing by the deficit
            # under-shoots and the fixpoint stalls (see _solve_gap_growth).
            growths = [
                _solve_gap_growth(
                    boxes_after[gap_index],
                    boxes_after[gap_index + 1],
                    baselines_after[gap_index],
                    _gap_required(label, line_indices, gap_index),
                    deficit,
                )
                if deficit > _DESCENDER_GAP_TOLERANCE
                else 0.0
                for gap_index, deficit in enumerate(deficits)
            ]
            grown = _grow_spacings(current_spacings, growths, available_height, height_at)
            if grown != current_spacings:
                current_spacings = grown
                grew = True

        if not grew:
            break

    # ---- Final measurement: the gaps the engraving actually has ----
    ascenders = [ascender for _basis, ascender, _d in geometry]
    descenders = [descender for _basis, _ascender, descender in geometry]
    if use_baseline:
        baselines = _baseline_table(
            pitch_basis,
            ascenders,
            descenders,
            current_spacings,
            center_by_band=center_by_band,
            by_content=by_content,
        )
    else:
        baselines = _inkbox_baseline_table(
            current_blocks,
            current_spacings,
            pitch_basis=pitch_basis,
            center_by_band=center_by_band,
            by_content=by_content,
        )
    final_boxes: List[List[GlyphBox]] = [
        _glyph_boxes(block, groups, label.content[line_index].plain_text)
        for block, groups, line_index in zip(current_blocks, glyph_groups_by_line, line_indices)
    ]
    stacked_boxes = [
        _shifted_boxes(boxes, baselines[index]) for index, boxes in enumerate(final_boxes)
    ]
    residual: List[float] = []
    for gap_index in range(len(original) - 1):
        required = required_gap(
            _line_cutter(label, line_indices[gap_index]),
            _line_cutter(label, line_indices[gap_index + 1]),
            label.descending_character_clearance,
        )
        # (a) The trigger metric on the final geometry: every descending
        # glyph's sub-baseline ink against the complete ink of the line below.
        # This is the gate: a relief that shrinks and raises a descender can
        # land the raised part on the line below (an ``i`` dot pushed into the
        # crook of a ``g``), and that collision must grow the spacing or abort,
        # never engrave silently. A fully relieved glyph has no sub-baseline
        # ink left, so it drops out of the descending filter -- safe by
        # construction, and reported by (b) below.
        final_gaps = measure_glyph_gaps(
            stacked_boxes[gap_index], stacked_boxes[gap_index + 1], required
        )
        worst = worst_glyph_deficit(final_gaps)
        # (b) Reporting only: the tightest final gap of every glyph the relief
        # moved, measured with its sub-baseline ink (a fully relieved glyph has
        # none and reports the reporting radius). The grid cell is at least the
        # required clearance, keeping the measurement exact below it; gaps
        # beyond the reporting radius report the radius.
        lower_ink_final: Ink = tuple(
            polyline for lower in stacked_boxes[gap_index + 1] for polyline in lower.ink
        )
        cell = max(required, _GAP_REPORT_RADIUS)
        final_grid = InkGrid(lower_ink_final, cell) if lower_ink_final and cell > 0.0 else None
        box_by_glyph = {box.glyph_index: box for box in stacked_boxes[gap_index]}
        for glyph_index in ramps.get(gap_index, {}):
            box = box_by_glyph.get(glyph_index)
            if box is None:
                continue
            if not box.sub_baseline_ink:
                gap_after[(gap_index, glyph_index)] = cell
                continue
            if final_grid is not None:
                nearest = final_grid.min_gap(box.sub_baseline_ink)
            elif lower_ink_final:
                nearest = ink_gap(box.sub_baseline_ink, lower_ink_final)
            else:
                nearest = math.inf
            # The grid reports ``inf`` beyond its cell (the reporting radius):
            # clamp to the radius so the report reads "at least this far",
            # never "infinitely far".
            gap_after[(gap_index, glyph_index)] = min(nearest, cell)
        residual.append(worst)

    # ---- Bottom border: the final gap against the border stroke ----
    # Measured for every glyph that ever triggered against the border --
    # including one the allowances could not move at all (ramp 0), which must
    # not collide silently. ``gap_after`` is merged in for the glyphs the
    # relief moved, so it reports the tightest of {line below, border} -- the
    # two things the engraved stroke must clear. A border shortfall never
    # enters ``residual``: spacing growth cannot help (it moves the last line
    # DOWN), so the ramp is the whole remedy and the result is reported as a
    # WARNING instead of aborting.
    border = _border_box(label)
    border_shortfalls: List[_BorderShortfall] = []
    for line_index in range(len(original)):
        required = _border_required(label, line_indices[line_index])
        cell = max(required, _GAP_REPORT_RADIUS)
        final_by_glyph = {box.glyph_index: box for box in stacked_boxes[line_index]}
        original_by_glyph = {box.glyph_index: box for box in original_boxes[line_index]}
        for glyph_index in sorted(border_triggered.get(line_index, ())):
            box = final_by_glyph.get(glyph_index)
            if box is None:
                continue
            if not box.sub_baseline_ink:
                nearest = cell
            else:
                grid = InkGrid(border.ink, cell) if cell > 0.0 else None
                nearest = (
                    min(grid.min_gap(box.sub_baseline_ink), cell) if grid is not None else cell
                )
            ramp = ramps.get(line_index, {}).get(glyph_index, 0.0)
            glyph_box = original_by_glyph.get(glyph_index)
            scale_y, raise_amount = apply_ramp(
                glyph_box.descender if glyph_box is not None else 0.0,
                label.descending_character_v_scale,
                label.descending_character_raise,
                ramp,
            )
            fix = DescenderFix(
                line_index=line_indices[line_index],
                glyph_index=glyph_index,
                char=glyph_box.char if glyph_box is not None else "",
                scale_y=scale_y,
                raise_amount=raise_amount,
                gap_before=gap_before.get((line_index, glyph_index), math.inf),
                gap_after=nearest,
            )
            if ramp > 0.0:
                previous = gap_after.get((line_index, glyph_index))
                gap_after[(line_index, glyph_index)] = (
                    nearest if previous is None else min(previous, nearest)
                )
            if nearest < required - _BORDER_SHORTFALL_TOLERANCE:
                border_shortfalls.append(_BorderShortfall(fix=fix, gap=nearest, required=required))

    fixes: List[DescenderFix] = []
    for gap_index in sorted(ramps):
        line_index = line_indices[gap_index]
        box_by_glyph = {box.glyph_index: box for box in original_boxes[gap_index]}
        for glyph_index, ramp in sorted(ramps[gap_index].items()):
            if ramp <= 0.0:
                continue
            glyph_box = box_by_glyph.get(glyph_index)
            descender = glyph_box.descender if glyph_box is not None else 0.0
            scale_y, raise_amount = apply_ramp(
                descender,
                label.descending_character_v_scale,
                label.descending_character_raise,
                ramp,
            )
            fixes.append(
                DescenderFix(
                    line_index=line_index,
                    glyph_index=glyph_index,
                    char=glyph_box.char if glyph_box is not None else "",
                    scale_y=scale_y,
                    raise_amount=raise_amount,
                    gap_before=gap_before.get((gap_index, glyph_index), math.inf),
                    gap_after=gap_after.get((gap_index, glyph_index), math.inf),
                )
            )

    # ---- Reciprocal records: the following-line glyphs that shared the load ----
    # ``gap_before`` is the pair's clearance that pulled the partner in;
    # ``gap_after`` re-measures the pair on the FINAL geometry (the descending
    # glyph's sub-baseline ink against this partner's scaled ink).
    for line_index in sorted(recip_ramps):
        box_by_glyph = {box.glyph_index: box for box in original_boxes[line_index]}
        stacked_by_glyph = {box.glyph_index: box for box in stacked_boxes[line_index]}
        for glyph_index, ramp in sorted(recip_ramps[line_index].items()):
            if ramp <= 0.0:
                continue
            glyph_box = box_by_glyph.get(glyph_index)
            p_key = (line_index - 1, glyph_index)
            gap_after_recip = math.inf
            upper_index = recip_pair_upper.get(p_key)
            upper_box = (
                next(
                    (
                        box
                        for box in stacked_boxes[line_index - 1]
                        if box.glyph_index == upper_index
                    ),
                    None,
                )
                if upper_index is not None
                else None
            )
            partner_box = stacked_by_glyph.get(glyph_index)
            if (
                upper_box is not None
                and partner_box is not None
                and upper_box.sub_baseline_ink
                and partner_box.ink
            ):
                cell = max(_gap_required(label, line_indices, line_index - 1), _GAP_REPORT_RADIUS)
                nearest = ink_gap(upper_box.sub_baseline_ink, partner_box.ink)
                gap_after_recip = min(nearest, cell)
            fixes.append(
                DescenderFix(
                    line_index=line_indices[line_index],
                    glyph_index=glyph_index,
                    char=glyph_box.char if glyph_box is not None else "",
                    scale_y=reciprocal_scale_y(ramp, label.descending_character_reciprocal_scale),
                    raise_amount=0.0,
                    gap_before=recip_gap_before.get(p_key, math.inf),
                    gap_after=gap_after_recip,
                    reciprocal=True,
                )
            )
    fixes.sort(key=lambda fix: (fix.line_index, fix.reciprocal, fix.glyph_index))

    heights = [
        (bounds[3] - bounds[1]) if (bounds := block.bounds()) is not None else 0.0
        for block in current_blocks
    ]
    return _DescenderRelief(
        blocks=current_blocks,
        heights=heights,
        spacings=current_spacings,
        geometry=geometry,
        fixes=fixes,
        residual=[max(0.0, value) for value in residual],
        border_shortfalls=border_shortfalls,
    )


def _log_descender_fixes(label: ResolvedLabel, fixes: Sequence[DescenderFix]) -> None:
    """Log every applied descending-character relief at INFO.

    Args:
        label: The label being rendered (for the id in each message).
        fixes: The reliefs applied by :func:`_apply_descender_relief`.
    """
    for fix in fixes:
        if fix.reciprocal:
            logger.info(
                "Label %s: line %s glyph %r shares the descender relief -- "
                "top-down scale %.3f (lowest point fixed, gap %.3fin -> %.3fin).",
                label.id,
                fix.line_index,
                fix.char,
                fix.scale_y,
                fix.gap_before,
                fix.gap_after,
            )
            continue
        logger.info(
            "Label %s: line %s glyph %r descender relieved -- vertical scale "
            "%.3f, raised %.3fin (gap %.3fin -> %.3fin).",
            label.id,
            fix.line_index,
            fix.char,
            fix.scale_y,
            fix.raise_amount,
            fix.gap_before,
            fix.gap_after,
        )


def _log_border_shortfalls(label: ResolvedLabel, shortfalls: Sequence[_BorderShortfall]) -> None:
    """Log every glyph the relief could not clear from the bottom border.

    A WARNING, never an abort: the border is below the last text line, so
    growing the inter-line spacing moves the descender CLOSER to it (the block
    is centred), which leaves the per-glyph ramp as the only lever. When the
    allowances run out the engraving is the best they allow, so the job
    proceeds and the shortfall is reported.

    Args:
        label: The label being rendered (for the id in each message).
        shortfalls: The shortfalls collected by
            :func:`_apply_descender_relief`.
    """
    for shortfall in shortfalls:
        fix = shortfall.fix
        logger.warning(
            "Label %s: line %s glyph %r stays %.3fin from the bottom border "
            "(needs %.3fin) after vertical scale %.3f, raised %.3fin -- "
            "increase height, v_margin, descending_character_v_scale/raise, "
            "or lower descending_character_clearance.",
            label.id,
            fix.line_index,
            fix.char,
            shortfall.gap,
            shortfall.required,
            fix.scale_y,
            fix.raise_amount,
        )


def _format_unresolvable_descenders(
    label: ResolvedLabel,
    relief: _DescenderRelief,
    line_indices: Sequence[int],
) -> str:
    """Build the abort message for descenders the relief could not clear.

    Args:
        label: The label whose relief failed.
        relief: The relief outcome carrying the per-gap residual shortfalls.
        line_indices: Each relief line's index into ``label.content``.

    Returns:
        A multi-line message naming every gap that stayed too tight, the two
        lines involved, the shortfall, and the allowances that were spent.
    """
    lines = [
        f"Label {label.id}: descending characters cannot clear the line below "
        f"within the allowed allowances."
    ]
    for gap_index, deficit in enumerate(relief.residual):
        if deficit <= _DESCENDER_GAP_TOLERANCE:
            continue
        upper = line_indices[gap_index] if gap_index < len(line_indices) else gap_index
        lower = line_indices[gap_index + 1] if gap_index + 1 < len(line_indices) else upper + 1
        upper_text = label.content[upper].plain_text if upper < len(label.content) else ""
        lower_text = label.content[lower] if lower < len(label.content) else None
        lines.append(
            f"  - lines {upper} ({upper_text!r}) / {lower} "
            f"({getattr(lower_text, 'text', '')!r}): still {deficit:.3f}in short "
            f"after vertical scale {label.descending_character_v_scale:.2f}, "
            f"raise {label.descending_character_raise:.2f}, reciprocal scale "
            f"{label.descending_character_reciprocal_scale:.2f}, clearance "
            f"{label.descending_character_clearance:.2f}x the stroke floor and "
            f"all admissible line spacing. Increase line_spacing, height, "
            f"descending_character_v_scale/raise/reciprocal_scale, or lower "
            f"descending_character_clearance."
        )
    return "\n".join(lines)


def _render_positioned_lines(
    label: ResolvedLabel,
    chunk_mode: TextChunkMode = TextChunkMode.LINE,
    *,
    check_glyph_coverage: bool = True,
    descender_fixes: Optional[List[DescenderFix]] = None,
    band_center_offset: Optional[List[float]] = None,
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
        descender_fixes: Optional list receiving every descending-character
            relief applied while positioning (the per-glyph vertical downscale
            + raise triggered by a near-miss against the line below). The
            geometry is always applied; the list only collects the records for
            reporting, so a caller that does not report passes ``None``.

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
    # A round label applies its margin **radially**: the content area is the
    # largest centered block inscribed in the inset ellipse (see
    # :func:`ellipse_content_inset`), strictly smaller than the rectangular
    # box inset. Recomputing the two local margins from that inset feeds the
    # radial rule to every downstream site -- the available height, the
    # compression width, the alignment width and the alignment anchor -- with
    # no per-site branching. Vertical centering is unaffected: the inset is
    # concentric, so the inner-area center stays at ``height / 2``.
    if _label_is_round(label):
        content_width, content_height = ellipse_content_inset(
            label.width, label.height, h_margin, v_margin
        )
        h_margin = (label.width - content_width) / 2.0
        v_margin = (label.height - content_height) / 2.0

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
            Tuple[float, ...],
        ]
    ] = []
    total_rendered_height = 0.0
    # Per-line baseline geometry, parallel to ``rendered_lines``: one
    # ``(pitch_basis, ascender, descender)`` triple per rendered line, in
    # inches. The renderers place the baseline at y = 0 with +y up, so the
    # pre-translate bounds give the ink extents around it directly (both
    # compression passes scale X only, leaving Y untouched). The pitch basis is
    # the line's cap height (``toolpath_text_height``) -- a per-font constant
    # independent of the line's own characters -- so a descender-free line
    # (ascender == basis, descender == 0) stacks bit-identically to ink-box
    # stacking. Consumed only when the label stacks by baseline pitch.
    baseline_geometry: List[Tuple[float, float, float]] = []

    for line_index, line in enumerate(label.content):
        # Render at the toolpath_text_height (cutter-compensated) in the
        # line's cascaded font. Both renderers return upright glyphs.
        try:
            block, word_groups, glyph_groups = _render_line_block(
                line, chunk_mode, check_glyph_coverage=check_glyph_coverage
            )
        except (PltFontRenderError, FtextRenderError) as exc:
            raise LabelRenderError(
                f"Label {label.id}: text line {line_index} ({line.plain_text!r}) "
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
        line_desc = f"of label {label.id} line {line_index} ({line.plain_text!r})"
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
                _stroke_cutter_map(line, block, glyph_groups),
            )
        )
        baseline_geometry.append(
            (
                line.toolpath_text_height,
                max(0.0, bounds[3]),
                max(0.0, -bounds[1]),
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
        for (_idx, _lc, _height, line_spacing, _mhc, _align, _wg, _cs, _gg, _bs) in rendered_lines[
            :-1
        ]
    ]
    # Vertical margin applies as-is; no cutter compensation needed since the
    # margin is user-specified and text already accounts for per-line cutter diameter
    # via horizontal compensation.
    available_height = label.height - (2 * v_margin)
    use_baseline = label.use_baseline_spacing
    # Band centering (v_center_by_content=false + relief enabled): center the
    # baseline-to-capline band so the visible text sits in the label's optical
    # middle and descenders hang into the bottom margin, where the relief
    # handles them. With the relief disabled nothing would move a descender, so
    # the historical ink centering is kept (see ResolvedLabel.center_text_by_band).
    center_by_band = label.center_text_by_band
    by_content = label.v_center_by_content
    pitch_basis = [basis for basis, _a, _d in baseline_geometry]
    ascenders = [ascender for _basis, ascender, _d in baseline_geometry]
    descenders = [descender for _basis, _ascender, descender in baseline_geometry]
    if use_baseline:
        # Baseline pitch: the clamp fits the descender-extended ink block, so
        # a line's descenders claim their share of the inner height. Under band
        # centering the block is measured as its baseline-to-capline band
        # instead -- descenders hang into the bottom margin for the relief --
        # matching the resolution-time fit so neither clamp fights the other.
        adjusted_spacings = fit_baseline_spacing_to_margins(
            pitch_basis,
            ascenders,
            descenders,
            spacings,
            available_height,
            charge_descenders=not center_by_band,
        )
    else:
        adjusted_spacings = fit_line_spacing_to_margins(
            [
                height
                for (
                    _idx,
                    _lc,
                    height,
                    _spacing,
                    _mhc,
                    _align,
                    _wg,
                    _cs,
                    _gg,
                    _bs,
                ) in rendered_lines
            ],
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

    # ---- Phase 2a: horizontal pipeline (compression + alignment) ----
    # The descending-character relief below is Y-only and this phase is X-only,
    # so doing the X work first is exact -- and necessary: the relief compares
    # a glyph against the glyphs of the line below over their overlapping X
    # span, which only exists once every line is aligned.
    x_blocks: List[TextBlock] = []
    x_line_indices: List[int] = []
    x_word_groups: List[Optional[List[Tuple[str, List[int]]]]] = []
    x_glyph_groups: List[Tuple[Tuple[int, ...], ...]] = []
    x_stroke_cutters: List[Tuple[float, ...]] = []
    effective_scales: List[float] = []
    for (
        line_index,
        block,
        _rendered_height,
        _line_spacing,
        max_h_compress,
        text_h_alignment,
        word_groups,
        collision_scale,
        glyph_groups,
        stroke_cutters,
    ) in rendered_lines:
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
            line_text=line.plain_text,
        )
        bounds = block.bounds()
        if bounds is None:  # pragma: no cover - measured in first pass
            continue
        min_x, _min_y, max_x, _max_y = bounds
        rendered_width = max_x - min_x

        # Effective per-line horizontal scale, recorded for reporting: the
        # collision-avoidance scale (applied in the first pass) times the
        # margin-overflow scale measured here. compress_x scales X only, so the
        # width ratio is exactly the applied margin scale (1.0 when the line
        # already fit or compression was disabled).
        margin_scale = rendered_width / pre_margin_width if pre_margin_width > 0.0 else 1.0

        # Horizontal alignment within the available width: "left" anchors
        # the line's left-most point at the left margin, "right" anchors the
        # right-most point at the right margin, "center" centers it.
        target_left_x = compute_horizontal_offset(
            rendered_width, alignment_available_width, h_margin, text_h_alignment
        )
        x_blocks.append(block.translate(target_left_x - min_x, 0.0))
        x_line_indices.append(line_index)
        x_word_groups.append(word_groups)
        x_glyph_groups.append(glyph_groups)
        x_stroke_cutters.append(stroke_cutters)
        effective_scales.append(collision_scale * margin_scale)

    if not x_blocks:
        return []

    # ---- Descending-character relief (Y-only, per triggering glyph) ----
    # Baseline stacking lets a descender hang into the gap below its line;
    # when a glyph on the line below comes close enough that the two engraved
    # strokes would bleed together, that one glyph is vertically downscaled and
    # raised (bounded by the cascading allowances), and the gap below the line
    # grows to cover whatever the allowances could not.
    relief = _apply_descender_relief(
        label,
        x_blocks,
        x_glyph_groups,
        x_line_indices,
        pitch_basis,
        adjusted_spacings,
        available_height,
        use_baseline=use_baseline,
        center_by_band=center_by_band,
        by_content=by_content,
    )
    if relief.fixes:
        _log_descender_fixes(label, relief.fixes)
        if descender_fixes is not None:
            descender_fixes.extend(relief.fixes)
    if relief.border_shortfalls:
        _log_border_shortfalls(label, relief.border_shortfalls)
    if any(residual > _DESCENDER_GAP_TOLERANCE for residual in relief.residual):
        raise LabelRenderError(_format_unresolvable_descenders(label, relief, x_line_indices))
    if relief.spacings != adjusted_spacings:
        logger.warning(
            "Label %s: line_spacing increased at render time from %s to %s "
            "to clear descending characters against the line below.",
            label.id,
            [round(s, 4) for s in adjusted_spacings],
            [round(s, 4) for s in relief.spacings],
        )

    adjusted_spacings = relief.spacings
    baseline_geometry = relief.geometry
    pitch_basis = [basis for basis, _a, _d in baseline_geometry]
    ascenders = [ascender for _basis, ascender, _d in baseline_geometry]
    descenders = [descender for _basis, _ascender, descender in baseline_geometry]
    if use_baseline:
        total_rendered_height = baseline_block_height(
            pitch_basis, ascenders, descenders, adjusted_spacings
        )
    else:
        total_rendered_height = sum(relief.heights) + sum(adjusted_spacings)

    # Anchor the block so its vertical center sits at y = total / 2. The
    # absolute anchor is irrelevant (post-export centering fixes it); only
    # the relative stacking matters.
    current_y = total_rendered_height / 2.0

    # Baseline mode precomputes each line's baseline Y in the same centered
    # frame: the block-local baselines descend by one pitch each, and the whole
    # set is shifted so the ink block (descenders included) centers on y = 0 --
    # or, under band centering, so the baseline-to-capline band centers there.
    # The per-line ``y_offset`` is then exactly that baseline Y, because the
    # rendered block's own baseline sits at local y = 0.
    baseline_y: List[float] = []
    if use_baseline:
        baseline_y = _baseline_table(
            pitch_basis,
            ascenders,
            descenders,
            adjusted_spacings,
            center_by_band=center_by_band,
            by_content=by_content,
        )
        # The delta is measured in the INK-centered frame, so recompute the
        # unshifted table when the table above applied the band shift.
        ink_centered = (
            baseline_y
            if not center_by_band
            else _baseline_table(pitch_basis, ascenders, descenders, adjusted_spacings)
        )
    else:
        ink_centered = _inkbox_baseline_table(relief.blocks, adjusted_spacings)

    # The render frame centers the ink block on y = 0, so the band's center in
    # it is exactly the extra offset the authoritative centering step
    # (_center_text_layer_vertically) must subtract to center the band instead.
    if center_by_band:
        delta = _band_center_delta(ink_centered, ascenders, pitch_basis, by_content=by_content)
        if band_center_offset is not None:
            band_center_offset.append(delta)
        if not use_baseline:
            # Ink-box mode keeps its running anchor; band centering only moves
            # the block's absolute position.
            current_y -= delta

    # ---- Phase 2b: vertical stacking + per-line reporting ----
    # X is final (phase 2a), the relief adjusted Y-only, so this phase moves
    # each line onto its stacked baseline and records what the report needs.
    positioned: List[
        Tuple[
            int,
            TextBlock,
            _LineEntry,
            Optional[List[Tuple[str, List[int]]]],
            Tuple[Tuple[int, ...], ...],
        ]
    ] = []
    for i, (block, line_index) in enumerate(zip(relief.blocks, x_line_indices)):
        bounds = block.bounds()
        if bounds is None:  # pragma: no cover - measured in phase 2a
            continue
        min_x, min_y, max_x, max_y = bounds

        # Vertical stacking. Ink-box mode anchors the top of this line's
        # glyphs at current_y. Baseline mode anchors the line's BASELINE at
        # its precomputed Y, so a descender hangs into the gap below instead
        # of inflating it (the rendered block's baseline sits at local y = 0).
        if use_baseline:
            y_offset = baseline_y[i]
        else:
            y_offset = current_y - max_y
        block = block.translate(0.0, y_offset)

        # Reported gap below this line. Both modes report the spacing term
        # (the value the layout report compares against the requested
        # ``line_spacing``). Under baseline spacing that term is
        # ``pitch - cap height``: the requested spacing survives unchanged
        # when the fit leaves it alone (so a descender pair, whose *visual*
        # gap shrinks, is not a report finding), and only a margin clamp
        # reduces it -- which is exactly the deviation the report surfaces.
        reported_spacing: Optional[float] = (
            adjusted_spacings[i] if i < len(adjusted_spacings) else None
        )

        # Translation is additive, so the positioned bounds are the measured
        # bounds shifted; no re-measurement is needed.
        positioned.append(
            (
                line_index,
                block,
                _LineEntry(
                    line_index=line_index,
                    line_text=label.content[line_index].plain_text,
                    bounds=(min_x, min_y + y_offset, max_x, max_y + y_offset),
                    compression_scale=effective_scales[i],
                    line_spacing=reported_spacing,
                    stroke_cutters=x_stroke_cutters[i],
                ),
                x_word_groups[i],
                x_glyph_groups[i],
            )
        )

        # Move down past this line (plus adjusted spacing, except after
        # the last). Baseline mode ignores the running anchor -- its Y comes
        # from the precomputed ``baseline_y`` table.
        if not use_baseline:
            current_y -= relief.heights[i]
            if i < len(adjusted_spacings):
                current_y -= adjusted_spacings[i]

    return positioned


def _render_text_local_with_bounds(
    label: ResolvedLabel,
    *,
    check_glyph_coverage: bool = True,
    band_center_offset: Optional[List[float]] = None,
) -> Tuple[vp.LineCollection, List[_LineEntry]]:
    """Render text at local coordinates and report per-line bounds.

    Thin wrapper over :func:`_render_positioned_lines` returning the
    combined polyline view (arcs flattened; all lines on one collection)
    plus the per-line bounds records used for collision detection.

    Args:
        label: The resolved label whose ``content`` should be rendered.
        check_glyph_coverage: When True (the default), a TrueType font
            missing a glyph fails the render.
        band_center_offset: Optional list receiving the band-center offset the
            renderer applied (inches); left empty when the label centers by
            ink. Collision detection must subtract the same offset the export
            centering applies, so sweeps that re-render collect it here.

    Returns:
        A tuple ``(combined_lc, line_entries)`` where ``combined_lc`` holds
        all rendered lines and ``line_entries`` is a list of :class:`_LineEntry`
        records in label-local coordinates (pre-export anchor, block centered
        around y=0). The export pipeline vertically centers the block at
        ``height / 2`` (lowered by the band-center offset when band centering
        is active); collision detection applies that shift itself.
    """
    text_lc = vp.LineCollection()
    line_entries: List[_LineEntry] = []
    for _line_index, positioned_block, entry, _wg, _gg in _render_positioned_lines(
        label, check_glyph_coverage=check_glyph_coverage, band_center_offset=band_center_offset
    ):
        text_lc.extend(positioned_block.to_vpype_polylines())
        line_entries.append(entry)
    return text_lc, line_entries


def _split_block_by_cutters(
    positioned_block: TextBlock,
    stroke_cutters: Tuple[float, ...],
    word_groups: Optional[List[Tuple[str, List[int]]]],
    glyph_groups: Tuple[Tuple[int, ...], ...],
) -> List[
    Tuple[
        TextBlock,
        Optional[List[Tuple[str, List[int]]]],
        Tuple[Tuple[int, ...], ...],
        float,
    ]
]:
    """Split one positioned line block into one group per engraving cutter.

    A line whose strokes engrave with more than one tool -- a ``<b>`` bold run
    (the bold rung) and each ``<scale:N>`` run (its effective-height cutter) --
    lands on one pen per cutter, so the positioned block is partitioned by the
    per-stroke cutter array: every distinct cutter collects its strokes (in
    stroke order) into one group, and the per-glyph / per-word partitions are
    remapped into that group's index space (a glyph's strokes all share one
    cutter, so every group maps wholly into one half; underline strokes own no
    glyph group and ride their group ungrouped).

    Args:
        positioned_block: The line's positioned block.
        stroke_cutters: The per-stroke cutter diameters (see
            :func:`_stroke_cutter_map`); strokes sharing a value group together.
        word_groups: Per-word stroke indices (``None`` in line mode).
        glyph_groups: Per-glyph stroke indices.

    Returns:
        One ``(block, words, glyphs, cutter)`` tuple per distinct cutter, in
        ascending cutter order (the pen-map order), each non-empty.
    """
    positions_by_cutter: dict[float, List[int]] = {}
    for index in range(len(positioned_block.strokes)):
        cutter = stroke_cutters[index] if index < len(stroke_cutters) else 0.0
        positions_by_cutter.setdefault(cutter, []).append(index)

    groups: List[
        Tuple[
            TextBlock,
            Optional[List[Tuple[str, List[int]]]],
            Tuple[Tuple[int, ...], ...],
            float,
        ]
    ] = []
    for cutter in sorted(positions_by_cutter):
        positions = positions_by_cutter[cutter]
        local = {global_index: local_index for local_index, global_index in enumerate(positions)}
        block = TextBlock(strokes=tuple(positioned_block.strokes[i] for i in positions))
        words: Optional[List[Tuple[str, List[int]]]] = None
        if word_groups is not None:
            words = [
                (word_text, [local[i] for i in indices if i in local])
                for word_text, indices in word_groups
            ]
        glyphs = tuple(
            tuple(local[i] for i in group if i in local) for group in glyph_groups if group
        )
        groups.append((block, words, glyphs, cutter))
    return groups


def _render_text_lines_by_pen(
    label: ResolvedLabel,
    pen_map: Optional[dict[tuple[float, str], int]] = None,
    chunk_mode: TextChunkMode = TextChunkMode.LINE,
    *,
    check_glyph_coverage: bool = True,
    descender_fixes: Optional[List[DescenderFix]] = None,
    band_center_offset: Optional[List[float]] = None,
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
        band_center_offset: Optional list receiving the band-center offset the
            renderer applied (inches, render frame) when the label centers by
            baseline-to-capline band; left empty when it centers by ink. The
            authoritative centering step needs the value, so it is collected
            here the same way ``descender_fixes`` is.

    Returns:
        Tuple of ``(pens, line_entries, chunk_records)`` where ``pens`` maps
        pen number to the tuple of positioned blocks for that pen (only
        non-empty pens included), ``line_entries`` is the same per-line
        bounds record list returned by
        :func:`_render_text_local_with_bounds`, and ``chunk_records`` holds
        one :class:`TextChunkRecord` per chunk (per line in ``LINE`` mode;
        per word plus any ungrouped line in ``WORD`` mode) in label-local
        coordinates for plate-space optimization.

        A line whose strokes engrave with more than one tool (a ``<b>`` bold
        run, a ``<scale:N>`` run) is split by cutter (see
        :func:`_split_block_by_cutters`): each cutter's strokes land on that
        tool's pen, each group producing its own chunk records
        (``plate_optimizer`` filters records by ``pen``, so the groups route as
        separate layers for free). A group's chunk bounds fall back to the
        whole line's entry bounds.
    """
    pens: dict[int, List[TextBlock]] = {}
    line_entries: List[_LineEntry] = []
    chunk_records: List[TextChunkRecord] = []
    for line_index, positioned_block, entry, word_groups, glyph_groups in _render_positioned_lines(
        label,
        chunk_mode=chunk_mode,
        check_glyph_coverage=check_glyph_coverage,
        descender_fixes=descender_fixes,
        band_center_offset=band_center_offset,
    ):
        line_content = label.content[line_index] if 0 <= line_index < len(label.content) else None
        line_entries.append(entry)

        stroke_cutters = entry.stroke_cutters
        if stroke_cutters and len(set(stroke_cutters)) > 1:
            halves = _split_block_by_cutters(
                positioned_block, stroke_cutters, word_groups, glyph_groups
            )
        else:
            cutter = line_content.cutter_diameter if line_content is not None else 0.0
            halves = [(positioned_block, word_groups, glyph_groups, cutter)]

        for half_block, half_words, half_glyphs, half_cutter in halves:
            pen = LAYER_TEXT
            if pen_map is not None and line_content is not None:
                pen = pen_map.get((half_cutter, line_content.text_color), LAYER_TEXT)
            pens.setdefault(pen, []).append(half_block)

            if half_words is not None:
                for word_index, (word_text, indices) in enumerate(half_words):
                    if not indices:
                        continue  # blank segment: no strokes to route
                    word_block = TextBlock(strokes=tuple(half_block.strokes[i] for i in indices))
                    word_bounds = word_block.bounds()
                    chunk_records.append(
                        TextChunkRecord(
                            line_index=line_index,
                            word_index=word_index,
                            word_text=word_text,
                            pen=pen,
                            blocks=(word_block,),
                            bounds=word_bounds if word_bounds is not None else entry[2],
                            glyph_groups=_word_local_glyph_groups(half_glyphs, indices),
                        )
                    )
            else:
                chunk_records.append(
                    TextChunkRecord(
                        line_index=line_index,
                        word_index=None,
                        word_text="",
                        pen=pen,
                        blocks=(half_block,),
                        bounds=entry[2],
                        glyph_groups=half_glyphs,
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


def _ellipse_boundary_polygon(
    center_x: float,
    center_y: float,
    width: float,
    height: float,
) -> np.ndarray:
    """Sample a closed ellipse contour in the label-local frame.

    The parametric ellipse ``(cx + a cos t, cy + b sin t)`` is sampled at a
    uniform parameter step bounded by the chord tolerance
    :data:`~plt_optimizer.generate.text_geometry.ARC_FLATTEN_CHORD_TOL_INCHES`
    at the point of maximum curvature (radius ``max(a, b)^2 / min(a, b)``,
    the vertex on the minor axis), so the engraved contour's deviation from
    the true ellipse stays within the same tolerance the arc flattening uses
    everywhere else. The contour is closed (the last vertex repeats the
    first), matching the boundary rectangle's closed loop.

    Args:
        center_x: Ellipse centre X, inches.
        center_y: Ellipse centre Y, inches.
        width: Full ellipse width (2a), inches.
        height: Full ellipse height (2b), inches.

    Returns:
        A closed complex-vertex array of the ellipse contour.
    """
    semi_a = width / 2.0
    semi_b = height / 2.0
    # Radius of curvature at the minor-axis vertex (the ellipse's max).
    radius_of_curvature = max(semi_a, semi_b) ** 2 / min(semi_a, semi_b)
    # Sagitta s = R (1 - cos(dtheta/2)) ~= R dtheta^2 / 8 <= tol.
    step = math.sqrt(8.0 * ARC_FLATTEN_CHORD_TOL_INCHES / radius_of_curvature)
    steps = max(4, math.ceil(2.0 * math.pi / step))
    # An EVEN count puts samples exactly on the four axis vertices, so the
    # contour reaches the enclosing box's extrema exactly -- the rendered
    # bounds then equal the nominal label box, which the packer and the
    # plate-assembly transforms rely on.
    steps += steps % 2
    theta = np.linspace(0.0, 2.0 * math.pi, steps + 1)
    return center_x + semi_a * np.cos(theta) + 1j * (center_y + semi_b * np.sin(theta))


def _lower_ellipse_arc(width: float, height: float) -> Polyline:
    """Sample the lower half of a round label's boundary in the relief frame.

    The descending-character relief measures a glyph against the label's
    bottom border as a virtual "following line" (see :func:`_border_box`).
    The relief's stacked frame centres the block on ``y = 0`` at label
    ``height / 2``, so the boundary's bottom vertex sits at ``-height / 2``
    and its left/right vertices (the ellipse's horizontal-axis extremes) sit
    at ``y = 0``.

    The lower arc is sampled from the left vertex to the right vertex
    (parametric angles 180 -> 360 degrees) at the shared chord tolerance, so
    the rolling-circle measurement sees the true curved boundary.

    Args:
        width: Label width in inches (> 0).
        height: Label height in inches (> 0).

    Returns:
        The lower arc's vertices, left to right, in the stacked frame.
    """
    semi_a = width / 2.0
    semi_b = height / 2.0
    radius_of_curvature = max(semi_a, semi_b) ** 2 / min(semi_a, semi_b)
    step = math.sqrt(8.0 * ARC_FLATTEN_CHORD_TOL_INCHES / radius_of_curvature)
    steps = max(2, math.ceil(math.pi / step))
    # An EVEN count samples the bottom vertex (3pi/2) exactly.
    steps += steps % 2
    theta = np.linspace(math.pi, 2.0 * math.pi, steps + 1)
    xs = semi_a + semi_a * np.cos(theta)
    ys = semi_b * np.sin(theta)
    return tuple((float(x), float(y)) for x, y in zip(xs, ys))


def _render_boundary_local(label: ResolvedLabel) -> vp.LineCollection:
    """Render the label boundary at local coordinates.

    ``type: rect`` (the default) emits the historical ``width`` x ``height``
    rectangle at the label edge, bit-identical. ``type: round`` emits the
    inscribed **ellipse** (semi-axes ``width / 2`` and ``height / 2``,
    centred at ``(width / 2, height / 2)``) as a closed polygon sampled at
    the shared arc chord tolerance, so the engraved boundary follows the
    round label's shape to the same fidelity as every flattened arc.

    Args:
        label: The resolved label whose boundary should be drawn.

    Returns:
        A LineCollection holding the boundary contour (empty for a
        degenerate label).
    """
    lc = vp.LineCollection()
    if _label_is_round(label):
        if label.width <= 0.0 or label.height <= 0.0:
            return lc
        lc.append(
            _ellipse_boundary_polygon(
                label.width / 2.0, label.height / 2.0, label.width, label.height
            )
        )
        return lc

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
    is_round = _label_is_round(label)

    for hole in label.holes:
        radius = hole.diameter / 2.0
        # Distance from the edge to the hole center: the requested hole
        # margin plus half the hole cutter diameter (for visual consistency)
        # plus the radius, so the circle's stroke edge sits exactly
        # hole_margin inches away from the edge.
        offset = label.hole_margin + (label.hole_cutter_diameter / 2.0) + radius
        location = str(hole.location)

        if is_round and location in ("top-left", "top-right", "bottom-left", "bottom-right"):
            # A round label's corner sits in the material the ellipse cuts
            # away, so a rectangular corner inset would drill outside the
            # boundary. Place the hole on the ellipse's diagonal instead:
            # at the 45-degree parametric point, inset inward along the
            # outward normal by ``offset`` so the stroke keeps hole_margin
            # from the engraved curve.
            hole_x, hole_y = _round_corner_hole(label, location, offset)
            circles.append((hole_x, hole_y, radius))
            continue

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


def _round_corner_hole(
    label: ResolvedLabel,
    location: str,
    offset: float,
) -> Tuple[float, float]:
    """Place a corner drill hole on a round label's elliptical boundary.

    The corner is measured along the 45-degree parametric direction (the
    diagonal toward the named corner): the boundary point
    ``(cx + a cos45, cy + b sin45)`` inset inward along the outward normal
    ``(cos45 / a, sin45 / b)`` (normalized) by ``offset``. The result keeps
    the hole's stroke ``offset`` from the engraved curve near the diagonal,
    where the rectangular corner inset would land the hole in cut-away
    material.

    Args:
        label: The resolved round label.
        location: One of the four corner location strings.
        offset: Inset distance from the boundary, inches.

    Returns:
        ``(center_x, center_y)`` in label-local y-up inches.
    """
    cx, cy, semi_a, semi_b = _label_ellipse(label)
    sign_x = -1.0 if "left" in location else 1.0
    sign_y = 1.0 if "top" in location else -1.0
    cos45 = math.sqrt(0.5)
    sin45 = math.sqrt(0.5)
    # Boundary point toward the corner.
    bx = cx + sign_x * semi_a * cos45
    by = cy + sign_y * semi_b * sin45
    # Unit outward normal at that parametric angle.
    nx = (sign_x * cos45) / semi_a
    ny = (sign_y * sin45) / semi_b
    norm = math.hypot(nx, ny)
    nx, ny = nx / norm, ny / norm
    return (bx - nx * offset, by - ny * offset)


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
        (``HPGL_UNITS_PER_INCH`` units = 1 inch).
    """
    commands: List[str] = []

    for center_x, center_y, radius in _hole_circles_local(label):
        # Convert to plotter units so the emitted PU start and AA center are
        # mutually consistent integers (the parser derives the radius from
        # their distance).
        cx = inches_to_units(center_x)
        cy = inches_to_units(center_y)
        r = inches_to_units(radius)
        if r <= 0:  # pragma: no cover - degenerate hole
            continue

        # Begin at the rightmost point of the circle, then sweep four
        # quarter-arcs back to it.
        start = f"{cx + r},{cy}"
        commands.append(f"PU{start}")
        commands.append(f"PD{start}")  # zero-length plunge, opens a new path
        commands.extend(f"AA{cx},{cy},90" for _ in range(4))

    return ";".join(commands)
