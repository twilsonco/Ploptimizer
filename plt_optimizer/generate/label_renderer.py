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
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import numpy as np
import vpype as vp

from plt_optimizer.generate.ftext_renderer import render_text_line_ftext
from plt_optimizer.generate.geometry import CollisionResult, circle_aabb_gap
from plt_optimizer.generate.resolution import (
    ResolvedLabel,
    compute_horizontal_offset,
    compute_horizontal_scale,
    fit_line_spacing_to_margins,
)

logger = logging.getLogger(__name__)

# Retained for backward compatibility with tests that reference these constants.
POINTS_PER_INCH: float = 72.0
TEXT_BLOCK_HEIGHT_PER_SIZE: float = 0.65625

# Layer assignments (must match vectorize.py constants)
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


class LabelRenderError(Exception):
    """Raised when a text-hole collision cannot be resolved.

    Emitted by :func:`render_label_to_plt` after both collision-avoidance
    phases (hole-margin reduction and horizontal text compression) have
    been exhausted without clearing the collision.
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
            at least one text/hole overlap (collision avoidance disabled or
            not fully effective). Labels successfully resolved by Phases
            2/3 report False even though avoidance was triggered.
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


def _detect_text_hole_collisions(
    label: ResolvedLabel,
    line_entries: Sequence[_LineEntry],
) -> List[CollisionResult]:
    """Find all (text line, drill hole) pairs whose geometry overlaps.

    Collision checks run in label-local, y-up coordinates before plate
    placement. The rendered line bounds are anchored around y=0 by
    :func:`_render_text_local_with_bounds`; the export pipeline vertically
    centers the block at ``height / 2`` (``_center_text_layer_vertically``),
    so bounds are shifted by that amount before the check. The export's
    Y-flip mirrors text and holes across the *same* centerline, which
    preserves circle/AABB intersection relationships exactly, so the check
    is valid in this pre-flip frame.

    Args:
        label: The resolved label providing hole positions.
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
        x_min, y_min, x_max, y_max = bounds
        shifted: Tuple[float, float, float, float] = (
            x_min,
            y_min + shift_y,
            x_max,
            y_max + shift_y,
        )
        for hole_index, (center_x, center_y, radius) in enumerate(circles):
            gap = circle_aabb_gap(shifted, (center_x, center_y), radius)
            if gap < 0.0:
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


def _log_collisions(label_id: str, collisions: Sequence[CollisionResult]) -> None:
    """Log each detected text-hole collision at WARNING level.

    Args:
        label_id: Identifier used in log messages.
        collisions: Collision results from :func:`_detect_text_hole_collisions`.
    """
    for collision in collisions:
        logger.warning(
            "Label %s: text line %d (%r) collides with %s drill hole "
            "(index %d); penetration %.4fin.",
            label_id,
            collision.line_index,
            collision.line_text,
            collision.hole_location,
            collision.hole_index,
            -collision.gap,
        )


def _resolve_collision_via_margin_adjustment(
    label: ResolvedLabel,
    line_entries: Sequence[_LineEntry],
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
                "avoid text-hole collision (minimum allowed %.4fin).",
                label.id,
                label.hole_margin,
                candidate,
                floor,
            )
            return candidate_label
    return None


def _resolve_collision_via_compression(
    label: ResolvedLabel,
    budget: float,
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
                "avoid text-hole collision (max_h_compress budget %.2f).",
                label.id,
                scale * 100.0,
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
    worst_gap = min((collision.gap for collision in collisions), default=0.0)
    budget = min((line.max_h_compress for line in label.content), default=0.0)
    scale_text = (
        f"{attempted_scale:.3f}"
        if attempted_scale is not None
        else f"{label.collision_compress:.3f}"
    )
    details = "; ".join(
        f"line {collision.line_index} ({collision.line_text!r}) vs "
        f"{collision.hole_location} hole (penetration {-collision.gap:.4f}in)"
        for collision in collisions
    )
    return (
        f"Label {label.id}: text-hole collision cannot be resolved.\n"
        f"- Collisions: {details}\n"
        f"- Hole margin: {label.hole_margin:.4f}in "
        f"(min: {label.min_hole_margin if label.min_hole_margin is not None else 'unset'})\n"
        f"- Compression: max_h_compress={budget:.2f} applied, still insufficient "
        f"(most aggressive text scale {scale_text})\n"
        f"- Worst penetration: {-worst_gap:.4f}in\n"
        "- Recommendations: increase label width, reduce text height, lower "
        "min_hole_margin, or increase max_h_compress."
    )


def log_text_hole_collisions(label: ResolvedLabel) -> List[CollisionResult]:
    """Detect and log text-hole collisions for a label (observational only).

    Public entry point for render paths outside :func:`render_label_to_plt`
    (e.g. the plate visualization in ``vectorize.py``) to surface collision
    warnings without attempting any fixes. Renders the label's text in
    local coordinates purely for measurement; no document is mutated and
    no geometry is adjusted.

    Args:
        label: The resolved label to inspect.

    Returns:
        The detected collisions (empty when the label has no holes or the
        text is clear of all drill holes).
    """
    if not label.holes:
        return []
    _text_lc, line_entries = _render_text_local_with_bounds(label)
    collisions = _detect_text_hole_collisions(label, line_entries)
    if collisions:
        _log_collisions(label.id, collisions)
    return collisions


def render_label_to_plt(label: ResolvedLabel) -> RenderedLabel:
    """Render a label independently to HPGL format and extract bounds.

    Renders the label at local coordinates (origin at bottom-left, no translation).
    Exports to temporary file with postprocessing to ensure coordinates are
    correct and compressed to 1:1000 scale (1 inch = 1000 units).

    Text-hole collision avoidance runs in three phases:

    1. **Detection** (always): rendered text bounds are checked against
       every drill hole; each collision is logged at WARNING level and
       recorded on the returned :attr:`RenderedLabel.has_collisions`.
    2. **Hole-margin reduction** (opt-in via ``min_hole_margin``): holes
       are moved toward the label edge (margin reduced toward the floor)
       until the text clears them.
    3. **Horizontal compression** (opt-in via ``max_h_compress``): text is
       uniformly compressed horizontally (stacked on top of any Phase 2
       margin reduction) until the collisions clear.

    The render aborts with :class:`LabelRenderError` only when the
    dedicated collision-avoidance knob (``min_hole_margin``) is set and no
    enabled phase could clear the collision. ``max_h_compress`` is a shared
    margin-fitting knob often set job-wide, so a failed compression sweep
    alone (without ``min_hole_margin``) degrades to the log-only behaviour
    instead of aborting -- some collisions (e.g. top/bottom holes) are
    geometrically unfixable by horizontal compression.

    Args:
        label: The ResolvedLabel to render (text, borders, holes).

    Returns:
        RenderedLabel with rendered PLT content and measured bounds. When
        collision avoidance adjusted the label, ``source_label`` is the
        adjusted clone (so packing/vectorization stay consistent with the
        emitted PLT). ``has_collisions`` is True only when the final
        render still overlaps a drill hole (avoidance disabled or not
        fully effective).

    Raises:
        ValueError: If bounds cannot be extracted from rendered PLT.
        LabelRenderError: If a text-hole collision was detected,
            ``min_hole_margin`` was configured, and no enabled phase
            could clear it.
    """
    rendered, line_entries = _render_label_once(label)
    collisions = _detect_text_hole_collisions(label, line_entries)
    if not collisions:
        return rendered

    # ---- Phase 1: collision detected -- log and attempt resolution ----
    _log_collisions(label.id, collisions)

    base_label: ResolvedLabel = label
    resolution_attempted = False

    # ---- Phase 2: reduce hole_margin toward min_hole_margin ----
    if label.min_hole_margin is not None:
        resolution_attempted = True
        margin_label = _resolve_collision_via_margin_adjustment(label, line_entries)
        if margin_label is not None:
            resolved_rendered, _ = _render_label_once(margin_label)
            return resolved_rendered
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
        resolution_attempted = True
        compressed_label = _resolve_collision_via_compression(base_label, budget)
        if compressed_label is not None:
            resolved_rendered, _ = _render_label_once(compressed_label)
            return resolved_rendered
        # Report the state at the most aggressive scale tried, so the
        # measured penetration matches what the sweep actually evaluated.
        attempted_scale = max(0.0, 1.0 - min(budget, 1.0))
        final_label = replace(base_label, collision_compress=attempted_scale)
        _, final_entries = _render_text_local_with_bounds(final_label)
        collisions = _detect_text_hole_collisions(final_label, final_entries)
        base_label = final_label

    if resolution_attempted and label.min_hole_margin is not None:
        raise LabelRenderError(
            _format_unresolvable_collision(base_label, collisions, attempted_scale)
        )

    if resolution_attempted:
        logger.warning(
            "Label %s: text-hole collision left unresolved; horizontal "
            "compression could not clear it. Set min_hole_margin to enable "
            "hole-margin reduction (required for top/bottom hole collisions).",
            label.id,
        )
    else:
        logger.warning(
            "Label %s: text-hole collision left unresolved; collision avoidance "
            "is disabled (set min_hole_margin and/or max_h_compress to enable).",
            label.id,
        )
    return replace(rendered, has_collisions=True)


def _render_label_once(label: ResolvedLabel) -> Tuple[RenderedLabel, List[_LineEntry]]:
    """Render a single label to PLT without collision resolution.

    Args:
        label: The ResolvedLabel to render (text, borders, holes).

    Returns:
        Tuple of the :class:`RenderedLabel` (with ``has_collisions`` left
        at its default) and the per-line rendered bounds used by
        :func:`_detect_text_hole_collisions`.
    """
    # Create vpype Document
    doc = vp.Document()

    # Render text layer
    text_lc, line_entries = _render_text_local_with_bounds(label)
    if not text_lc.is_empty():
        doc.add(text_lc, LAYER_TEXT)

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
        _export_to_plt_with_postprocessing(doc, temp_path, label)
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
    doc: vp.Document, output_path: Path, label: ResolvedLabel
) -> None:
    """Export vpype Document to PLT for a single label.

    Each label is rendered independently. Instead of using vpype's write_hpgl()
    which applies complex coordinate transformations, we manually generate HPGL
    commands from the LineCollection to preserve coordinate fidelity.

    Process:
    1. Extract coordinates directly from vpype LineCollection (units are inches)
       and convert them losslessly to plotter units at 1:1000 scale.
    2. Center text layer (pen 1) vertically within label bounds.
    3. Invert the Y-axis to device convention for upright display.

    No uniform scaling is applied because ``_linecollection_to_hpgl`` already
    produces coordinates at nominal scale; rescaling would distort footprints.

    Args:
        doc: The vpype Document to export.
        output_path: Destination PLT file path.
        label: The label being rendered (used to get expected dimensions).
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

    # Center text layer (pen 1) vertically within label bounds
    _center_text_layer_vertically(output_path, label)

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

    # Process each pen layer in the document
    # Pens are numbered 0-7, but we typically use 1 (text), 2 (border), 3 (holes)
    for pen_num in range(4):
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


def _scale_coordinates_unified(file_path: Path, label: ResolvedLabel) -> None:
    """Scale all coordinates uniformly for a single label to expected dimensions.

    All pen layers (text, border, holes) are scaled with the SAME scale factors
    based on the combined bounds of all coordinates. This ensures layers stay
    aligned even when they have different coordinate ranges from vpype.

    Args:
        file_path: Path to the PLT file.
        label: The label being rendered (for expected dimensions).
    """
    content = file_path.read_text(encoding="utf-8")

    # Extract ALL coordinates from all layers to find actual bounds
    pattern = r"(?:PA|PU|PD)([\d,\-]+)"
    all_x = []
    all_y = []

    for match in re.finditer(pattern, content):
        coords_str = match.group(1)
        parts = coords_str.split(",")
        try:
            for i in range(0, len(parts) - 1, 2):
                x = int(parts[i])
                y = int(parts[i + 1])
                all_x.append(x)
                all_y.append(y)
        except (ValueError, IndexError):
            continue

    if not all_x or not all_y:
        return

    x_min, x_max = min(all_x), max(all_x)
    y_min, y_max = min(all_y), max(all_y)

    # Current coordinate ranges (in plotter units from vpype)
    x_range = x_max - x_min if x_max > x_min else 1
    y_range = y_max - y_min if y_max > y_min else 1

    # Expected ranges (in inches, at 1:1000 scale, so multiply by 1000)
    # Use the label's actual dimensions
    expected_x_range_units = label.width * 1000.0
    expected_y_range_units = label.height * 1000.0

    # UNIFIED scaling: use the same scale for all coordinates
    scale_x = expected_x_range_units / x_range if x_range > 0 else 1.0
    scale_y = expected_y_range_units / y_range if y_range > 0 else 1.0

    logger.debug(
        f"_scale_coordinates_unified: {file_path.name} - "
        f"vpype bounds: x=[{x_min}, {x_max}] ({x_range}), "
        f"y=[{y_min}, {y_max}] ({y_range}), "
        f'label={label.width:.1f}"×{label.height:.1f}", '
        f"unified scale: x={scale_x:.4f}, y={scale_y:.4f}"
    )

    def scale_coordinates(match: re.Match[str]) -> str:
        """Scale x and y coordinates independently."""
        cmd = match.group(1)
        coords_str = match.group(2)
        parts = coords_str.split(",")

        try:
            scaled_parts = []
            for i, part in enumerate(parts):
                val = int(part)
                if i % 2 == 0:  # x coordinate
                    scaled_val = int(round(val * scale_x))
                else:  # y coordinate
                    scaled_val = int(round(val * scale_y))
                scaled_parts.append(str(scaled_val))
            return f"{cmd}{','.join(scaled_parts)}"
        except (ValueError, IndexError):
            return match.group(0)

    coord_pattern = r"(PA|PU|PD)([\d,\-]+)"
    modified_content = re.sub(coord_pattern, scale_coordinates, content)

    # After scaling, translate all coordinates to origin
    modified_content = _translate_all_to_origin(modified_content)

    file_path.write_text(modified_content, encoding="utf-8")


def _center_text_layer_vertically(file_path: Path, label: ResolvedLabel) -> None:
    """Center text layer (pen 1) vertically within label bounds after scaling.

    After export and scaling, the text coordinates need to be shifted vertically
    to center them within the label. This function:
    1. Extracts Y coordinates from pen 1 (TEXT) only
    2. Calculates the expected center position
    3. Adjusts all text Y coordinates to center the text

    Args:
        file_path: Path to the PLT file (after scaling).
        label: The label being rendered (for dimensions and margins).
    """
    content = file_path.read_text(encoding="utf-8")

    # Extract Y coordinates from pen 1 (TEXT) only
    pattern = r"SP1;(.*?)(?:SP\d|$)"
    matches = re.search(pattern, content, re.DOTALL)
    if not matches:
        # No text layer found
        return

    text_section = matches.group(1)
    y_coords = []
    coord_pattern = r"(?:PA|PU|PD)([\d,\-]+)"
    for match in re.finditer(coord_pattern, text_section):
        coords_str = match.group(1)
        parts = coords_str.split(",")
        for i in range(1, len(parts), 2):
            try:
                y = int(parts[i])
                y_coords.append(y)
            except (ValueError, IndexError):
                pass

    if not y_coords:
        # No coordinates in text layer
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
        f"text Y=[{text_y_min}, {text_y_max}] (center={current_center_y:.1f}), "
        f"expected center={expected_center_y:.1f}, "
        f"adjustment={y_adjustment:.1f} plotter units"
    )

    def adjust_text_y(match: re.Match[str]) -> str:
        """Adjust Y coordinates in pen 1 (TEXT) only."""
        coords_str = match.group(1)
        parts = coords_str.split(",")

        try:
            adjusted_parts = []
            for i, part in enumerate(parts):
                val = int(part)
                if i % 2 == 1:  # Y coordinate (odd index)
                    adjusted_val = int(round(val + y_adjustment))
                    adjusted_parts.append(str(adjusted_val))
                else:  # X coordinate
                    adjusted_parts.append(part)
            return f"PA{','.join(adjusted_parts)}" if ",".join(adjusted_parts) else ""
        except (ValueError, IndexError):
            return match.group(0)

    # Find and replace: Extract pen 1 section, adjust Y, replace it
    def replace_pen1_section(match: re.Match[str]) -> str:
        sp1_and_content = match.group(0)
        # Adjust Y coordinates within this section
        coord_pattern_in_pen = r"(PA|PU|PD)([\d,\-]+)"

        def adjust_coords_in_pen(coord_match: re.Match[str]) -> str:
            cmd = coord_match.group(1)
            coords_str = coord_match.group(2)
            parts = coords_str.split(",")

            try:
                adjusted_parts = []
                for i, part in enumerate(parts):
                    val = int(part)
                    if i % 2 == 1:  # Y coordinate (odd index)
                        adjusted_val = int(round(val + y_adjustment))
                        adjusted_parts.append(str(adjusted_val))
                    else:  # X coordinate
                        adjusted_parts.append(part)
                return f"{cmd}{','.join(adjusted_parts)}"
            except (ValueError, IndexError):
                return coord_match.group(0)

        return re.sub(coord_pattern_in_pen, adjust_coords_in_pen, sp1_and_content)

    # Replace the pen 1 section with adjusted coordinates
    modified_content = re.sub(pattern, replace_pen1_section, content, flags=re.DOTALL)

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


def _scale_coordinates_per_layer(file_path: Path, label: ResolvedLabel) -> None:
    """[DEPRECATED - use _scale_coordinates_unified instead]

    This function is kept for backward compatibility but should not be used.
    Use _scale_coordinates_unified which applies uniform scaling to all layers.
    """
    pass


def _translate_all_to_origin(content: str) -> str:
    """Translate all coordinates to origin after scaling.

    Finds minimum coordinates across all layers and shifts so (min_x, min_y)
    becomes (0, 0).
    """
    # Extract coordinates again after scaling
    pattern = r"(?:PA|PU|PD)([\d,\-]+)"
    all_x = []
    all_y = []

    for match in re.finditer(pattern, content):
        coords_str = match.group(1)
        parts = coords_str.split(",")
        try:
            for i in range(0, len(parts) - 1, 2):
                x = int(parts[i])
                y = int(parts[i + 1])
                if abs(x) < 100000 and abs(y) < 100000:
                    all_x.append(x)
                    all_y.append(y)
        except (ValueError, IndexError):
            continue

    if not all_x or not all_y:
        return content

    min_x = min(all_x)
    min_y = min(all_y)

    if min_x == 0 and min_y == 0:
        return content

    def translate_coordinates(match: re.Match[str]) -> str:
        """Translate coordinates to origin."""
        cmd = match.group(1)
        coords_str = match.group(2)
        parts = coords_str.split(",")

        try:
            translated_parts = []
            for i, part in enumerate(parts):
                val = int(part)
                if i % 2 == 0:  # x coordinate
                    translated_val = val - min_x
                else:  # y coordinate
                    translated_val = val - min_y
                translated_parts.append(str(translated_val))
            return f"{cmd}{','.join(translated_parts)}"
        except (ValueError, IndexError):
            return match.group(0)

    coord_pattern = r"(PA|PU|PD)([\d,\-]+)"
    return re.sub(coord_pattern, translate_coordinates, content)


def _extract_coordinates_by_pen(
    plt_content: str,
) -> dict[int, list[tuple[float, float]]]:
    """Extract coordinates grouped by pen number from PLT content.

    [DEPRECATED - no longer needed with unified scaling]

    Args:
        plt_content: Raw HPGL text content.

    Returns:
        Dictionary mapping pen number to list of (x, y) coordinate tuples
        in plotter units.
    """
    coordinates_by_pen: dict[int, list[tuple[float, float]]] = {1: [], 2: [], 3: []}

    current_pen = 0

    # Split by SP (Select Pen) commands
    pen_sections = re.split(r"SP(\d+);", plt_content)

    # pen_sections will be: [before_first_SP, first_pen_num, first_pen_content, ...]
    for i in range(1, len(pen_sections), 2):
        pen_num_str = pen_sections[i]
        pen_content = pen_sections[i + 1] if i + 1 < len(pen_sections) else ""

        try:
            pen_num = int(pen_num_str)
            current_pen = pen_num
        except (ValueError, IndexError):
            continue

        # Extract coordinates from this pen section
        coordinate_pattern = r"(PA|PU|PD)([\d,\-]+)"
        matches = re.findall(coordinate_pattern, pen_content)

        for _cmd, coords_str in matches:
            parts = coords_str.split(",")
            for j in range(0, len(parts) - 1, 2):
                try:
                    x_val = int(parts[j])
                    y_val = int(parts[j + 1])
                    if current_pen in coordinates_by_pen:
                        coordinates_by_pen[current_pen].append((x_val, y_val))
                except (ValueError, IndexError):
                    pass

    return coordinates_by_pen


def _translate_after_scaling(content: str) -> str:
    """Translate coordinates to origin after scaling.

    Finds minimum coordinates and shifts so (min_x, min_y) becomes (0, 0).
    [DEPRECATED - use _translate_all_to_origin instead]
    """
    return _translate_all_to_origin(content)


def _fix_rectangle_heights_in_plt(file_path: Path) -> None:
    """Ensure all rectangles have consistent height.

    vpype's coordinate rounding during compression can cause slightly
    different rectangle heights. This function detects the most common
    height and adjusts shorter rectangles to match.
    """
    content = file_path.read_text(encoding="utf-8")
    pd_pattern = r"PD([\d,\-]+)"

    def analyze_rectangle(coords_str: str) -> Tuple[int, int, int] | None:
        """Analyze if a PD command draws a rectangle and return (min_y, max_y, height)."""
        parts = coords_str.split(",")
        if len(parts) < 8:
            return None
        try:
            points = [(int(parts[i]), int(parts[i + 1])) for i in range(0, len(parts), 2)]
        except ValueError:
            return None
        if len(points) < 4:
            return None

        xs = [p[0] for p in points[:-1]]
        ys = [p[1] for p in points[:-1]]
        unique_xs = len(set(xs))
        unique_ys = len(set(ys))

        if unique_xs == 2 and unique_ys == 2:
            height = abs(max(ys) - min(ys))
            return (min(ys), max(ys), height)
        return None

    # Find all rectangles and their heights
    rectangles = []
    for match in re.finditer(pd_pattern, content):
        result = analyze_rectangle(match.group(1))
        if result:
            rectangles.append(result)

    if not rectangles:
        return

    # Find common height (largest height among rectangles > 5 units)
    large_heights = [r[2] for r in rectangles if r[2] > 5]
    if not large_heights:
        return

    common_height = max(large_heights)
    heights = [r[2] for r in rectangles if r[2] > 5]
    if heights:
        avg_height = sum(heights) / len(heights)
        common_height = int(round(avg_height))

    logger.debug(
        f"_fix_rectangle_heights_in_plt: {file_path.name} - found {len(rectangles)} "
        f"rectangles, {len(heights)} large (>500), avg_height={avg_height:.1f}, "
        f"common_height={common_height}"
    )

    # Fix shorter rectangles
    def fix_rectangle(match: re.Match[str]) -> str:
        """Extend shorter rectangles to match common height."""
        coords_str = match.group(1)
        result = analyze_rectangle(coords_str)
        if result is None or int(result[2]) >= common_height:
            return match.group(0)
        if int(result[2]) <= 5:
            return match.group(0)

        parts = coords_str.split(",")
        points = [(int(parts[i]), int(parts[i + 1])) for i in range(0, len(parts), 2)]
        adjusted_points = []
        for x, y in points:
            if y == result[0]:
                adjusted_points.append((x, y - 1))
            else:
                adjusted_points.append((x, y))

        new_coords = ",".join(str(v) for p in adjusted_points for v in p)
        return f"PD{new_coords}"

    modified_content = re.sub(pd_pattern, fix_rectangle, content)
    file_path.write_text(modified_content, encoding="utf-8")


def _scale_coordinates_in_plt(file_path: Path) -> None:
    """Scale coordinates to correct vpype compression.

    vpype applies compression when fitting to A3 page. This calculates
    the scale factor from detected rectangle heights and rescales all
    coordinates to restore 1:1000 ratio (1 inch = 1000 units).
    """
    content = file_path.read_text(encoding="utf-8")
    pd_pattern = r"PD([\d,\-]+)"

    def analyze_rectangle(coords_str: str) -> Tuple[int, int, int] | None:
        """Analyze if a PD command draws a rectangle and return (min_y, max_y, height)."""
        parts = coords_str.split(",")
        if len(parts) < 8:
            return None
        try:
            points = [(int(parts[i]), int(parts[i + 1])) for i in range(0, len(parts), 2)]
        except ValueError:
            return None
        if len(points) < 4:
            return None

        xs = [p[0] for p in points[:-1]]
        ys = [p[1] for p in points[:-1]]
        unique_xs = len(set(xs))
        unique_ys = len(set(ys))

        if unique_xs == 2 and unique_ys == 2:
            height = abs(max(ys) - min(ys))
            return (min(ys), max(ys), height)
        return None

    rectangles = []
    for match in re.finditer(pd_pattern, content):
        result = analyze_rectangle(match.group(1))
        if result:
            rectangles.append(result)

    if not rectangles:
        return

    heights = [r[2] for r in rectangles if r[2] > 500]
    if not heights:
        return

    avg_height = sum(heights) / len(heights)
    expected_height = 1000
    scale = expected_height / avg_height if avg_height > 0 else 1.0

    # Find center for scaling
    coord_pattern_pard = r"(PA|PD)([\d,\-]+)"
    all_x = []
    all_y = []

    for match in re.finditer(coord_pattern_pard, content):
        coords_str = match.group(2)
        parts = coords_str.split(",")
        try:
            for i in range(0, len(parts) - 1, 2):
                x = int(parts[i])
                y = int(parts[i + 1])
                all_x.append(x)
                all_y.append(y)
        except (ValueError, IndexError):
            continue

    if not all_x or not all_y:
        return

    center_x = (min(all_x) + max(all_x)) / 2
    center_y = (min(all_y) + max(all_y)) / 2

    def scale_coordinates(match: re.Match[str]) -> str:
        """Scale coordinates."""
        cmd = match.group(1)
        coords_str = match.group(2)
        parts = coords_str.split(",")

        try:
            scaled_parts = []
            for i, part in enumerate(parts):
                val = int(part)
                if i % 2 == 0:  # x coordinate
                    scaled_val = int(round((val - center_x) * scale + center_x))
                else:  # y coordinate
                    scaled_val = int(round((val - center_y) * scale + center_y))
                scaled_parts.append(str(scaled_val))
            return f"{cmd}{','.join(scaled_parts)}"
        except (ValueError, IndexError):
            return match.group(0)

    coord_pattern_all = r"(PA|PU|PD)([\d,\-]+)"
    modified_content = re.sub(coord_pattern_all, scale_coordinates, content)
    file_path.write_text(modified_content, encoding="utf-8")

    logger.debug(
        f"_scale_coordinates_in_plt: {file_path.name} - found {len(rectangles)} rectangles, "
        f"{len(heights)} large (>500), avg_height={avg_height:.1f}, scale={scale:.4f}"
    )


def _translate_coordinates_to_origin_in_plt(file_path: Path) -> None:
    """Translate and flip coordinates for plotter convention.

    Applies two transformations:
    1. Translate x so min_x becomes 0 (left edge at origin)
    2. Flip y-axis: y_new = max_y - y (first label at y=0, last at y=max_y)
    """
    content = file_path.read_text(encoding="utf-8")
    content_stripped = content

    # Extract all PA/PD coordinates to find ranges
    coord_pattern_pard = r"(PA|PD)([\d,\-]+)"
    all_x = []
    all_y = []

    for match in re.finditer(coord_pattern_pard, content):
        coords_str = match.group(2)
        parts = coords_str.split(",")
        try:
            for i in range(0, len(parts) - 1, 2):
                x = int(parts[i])
                y = int(parts[i + 1])
                all_x.append(x)
                all_y.append(y)
        except (ValueError, IndexError):
            continue

    if not all_x or not all_y:
        return

    min_x = min(all_x)
    min_y = min(all_y)
    max_y = max(all_y)

    if min_x == 0 and min_y == max_y:
        return

    # Remove spurious PU commands
    content_range_x = max(all_x) - min_x
    content_range_y = max_y - min_y
    threshold_x = 2 * content_range_x if content_range_x > 0 else 100000
    threshold_y = 2 * content_range_y if content_range_y > 0 else 100000

    def is_spurious_pu(coords_str: str) -> bool:
        """Check if a PU command has coordinates far from content."""
        parts = coords_str.split(",")
        try:
            if len(parts) >= 2:
                x = int(parts[0])
                y = int(parts[1])
                if abs(x - min_x) > threshold_x or abs(y - min_y) > threshold_y:
                    return True
        except ValueError:
            pass
        return False

    pu_pattern = r"(;?)PU([\d,\-]+)"
    matches_to_remove = []
    for match in re.finditer(pu_pattern, content_stripped):
        coords_str = match.group(2)
        if is_spurious_pu(coords_str):
            matches_to_remove.append((match.start(), match.end(), match.group(1)))

    for start, end, _leading_char in sorted(matches_to_remove, key=lambda x: x[0], reverse=True):
        content_stripped = content_stripped[:start] + content_stripped[end:]

    def translate_coordinates(match: re.Match[str]) -> str:
        """Translate x and flip y."""
        cmd = match.group(1)
        coords_str = match.group(2)
        parts = coords_str.split(",")

        try:
            translated_parts = []
            for i, part in enumerate(parts):
                val = int(part)
                if i % 2 == 0:  # x coordinate
                    translated_val = val - min_x
                else:  # y coordinate
                    translated_val = max_y - val
                translated_parts.append(str(translated_val))
            return f"{cmd}{','.join(translated_parts)}"
        except (ValueError, IndexError):
            return match.group(0)

    coord_pattern_all = r"(PA|PU|PD)([\d,\-]+)"
    modified_content = re.sub(coord_pattern_all, translate_coordinates, content_stripped)
    file_path.write_text(modified_content, encoding="utf-8")

    logger.debug(
        f"_translate_coordinates_to_origin_in_plt: {file_path.name} - "
        f"translated x by {-min_x}, flipped y (max_y={max_y})"
    )


# ============================================================================
# Local rendering functions (render at 0,0 without translation)
# ============================================================================


def compress_line_to_width(
    line_lc: vp.LineCollection,
    available_width: float,
    max_h_compress: float,
    label_id: str,
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
    if compressed_width > available_width + 1e-9:
        logger.warning(
            "Label %s: text line compressed to %.1f%% (%.3fin -> %.3fin) but "
            "still exceeds the available inner width (%.3fin); increase "
            "max_h_compress, widen the label, or reduce margin.",
            label_id,
            scale * 100.0,
            rendered_width,
            compressed_width,
            available_width,
        )
    else:
        logger.warning(
            "Label %s: text line horizontally compressed to fit margin (%.3fin "
            "-> %.3fin, scale %.3f).",
            label_id,
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


def _render_text_local(label: ResolvedLabel) -> vp.LineCollection:
    """Render text at local coordinates, stacking multi-line content.

    Thin wrapper over :func:`_render_text_local_with_bounds` for callers
    that do not need per-line bounds.

    Args:
        label: The resolved label whose ``content`` should be rendered.

    Returns:
        A LineCollection containing all rendered text lines.
    """
    text_lc, _entries = _render_text_local_with_bounds(label)
    return text_lc


def _render_text_local_with_bounds(
    label: ResolvedLabel,
) -> Tuple[vp.LineCollection, List[Tuple[int, str, Tuple[float, float, float, float]]]]:
    """Render text at local coordinates and report per-line bounds.

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

    Returns:
        A tuple ``(combined_lc, line_entries)`` where ``combined_lc`` holds
        all rendered lines and ``line_entries`` is a list of
        ``(line_index, line_text, bounds)`` tuples in label-local
        coordinates (pre-export anchor, block centered around y=0). The
        export pipeline vertically centers the block at ``height / 2``;
        collision detection applies that shift itself.
    """
    line_entries: List[Tuple[int, str, Tuple[float, float, float, float]]] = []
    if not label.content:
        return vp.LineCollection(), line_entries

    margin = label.margin
    inner_width = label.width
    text_lc = vp.LineCollection()

    # First pass: render all lines and measure their heights. Keep each
    # line's own line_spacing alongside the rendered geometry so empty or
    # unrenderable lines don't misalign spacing between real lines. The
    # original ``content`` index is preserved so collision reports can name
    # the offending line even when earlier lines were unrenderable.
    rendered_lines: list[Tuple[int, vp.LineCollection, float, float, float, str]] = []
    total_rendered_height = 0.0

    for line_index, line in enumerate(label.content):
        # Render at the toolpath_text_height (cutter-compensated) using the
        # single-line TTF font. ftext returns upright glyphs with baseline at 0.
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
            )
        )
        total_rendered_height += rendered_height

    if not rendered_lines:
        return text_lc, line_entries

    # Add line spacing between lines (not after the last line). Margin
    # precedence: if the measured block (line heights + requested spacing)
    # overflows the inner area, shrink the spacing so margins win.
    spacings = [
        line_spacing for _idx, _lc, _height, line_spacing, _mhc, _align in rendered_lines[:-1]
    ]
    available_height = label.height - (2 * margin)
    adjusted_spacings = fit_line_spacing_to_margins(
        [height for _idx, _lc, height, _spacing, _mhc, _align in rendered_lines],
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
        height for _idx, _lc, height, _spacing, _mhc, _align in rendered_lines
    ) + sum(adjusted_spacings)

    # Anchor the block so its vertical center sits at y = total / 2. The
    # absolute anchor is irrelevant (post-export centering fixes it); only
    # the relative stacking matters.
    current_y = total_rendered_height / 2.0

    # Second pass: position each line, stacked top-to-bottom (+y up).
    for i, (
        line_index,
        filtered_lc,
        rendered_height,
        _line_spacing,
        max_h_compress,
        text_h_alignment,
    ) in enumerate(rendered_lines):
        bounds = filtered_lc.bounds()
        if bounds is None:  # pragma: no cover - measured in first pass
            continue

        # Margin precedence for width: compress over-wide lines so they
        # respect the inner content area (bounded by max_h_compress).
        available_width = inner_width - (2 * margin)
        filtered_lc = compress_line_to_width(filtered_lc, available_width, max_h_compress, label.id)
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
        text_lc.extend(filtered_lc)

        positioned = filtered_lc.bounds()
        if positioned is not None:
            line_entries.append((line_index, label.content[line_index].text, positioned))

        # Move down past this line (plus adjusted spacing, except after
        # the last).
        current_y -= rendered_height
        if i < len(adjusted_spacings):
            current_y -= adjusted_spacings[i]

    return text_lc, line_entries


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


def _render_holes_local(label: ResolvedLabel) -> vp.LineCollection:
    """Render holes as densely sampled circles at local coordinates.

    Retained for bounds measurement and geometry assertions. The actual
    toolpath is emitted as native HPGL arcs by :func:`_render_holes_hpgl`;
    this sampling exists only because a ``LineCollection`` cannot represent
    arcs.

    Args:
        label: The resolved label whose ``holes`` should be rendered.

    Returns:
        A LineCollection containing one densely sampled closed circle per hole.
    """
    lc = vp.LineCollection()

    for center_x, center_y, radius in _hole_circles_local(label):
        # 0.001 inch quantization keeps the sampled polygon visually and
        # numerically indistinguishable from the true circle.
        circle = vp.circle(center_x, center_y, radius, quantization=0.001)
        lc.append(circle)

    return lc


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
