"""Plate-space toolpath optimization for generated labels.

The generate pipeline knows, ahead of time, exactly which toolpath belongs
to text and which to borders/holes -- the classification the file-based
:class:`~plt_optimizer.core.profiler.Profiler` infers heuristically. This
module exploits that knowledge to optimize toolpaths in the *plate frame*
(the coordinate space produced after rectpack bin-packing), one routing
problem per plate and per cutter:

- **Text layers**: one TSP node per :class:`TextChunkRecord` (a whole text
  line or a single word, per the label's ``text_chunk_mode``). Records are
  carried out of label rendering in label-local coordinates and mapped
  here into device (plotter-unit) coordinates with the exact same
  transform chain the emitted HPGL goes through (vertical centering,
  Y-mirror, optional 90-degree rotation, slot translation). The chunker is
  bypassed entirely: the chunk records *are* the nodes.
- **Structural layers** (borders + holes): the extracted HPGL is parsed
  and pushed through the shared core pipeline
  (:func:`~plt_optimizer.core.pipeline.preprocess_document` ->
  :func:`~plt_optimizer.core.pipeline.chunk_document` ->
  :func:`~plt_optimizer.core.pipeline.optimize_and_reassemble`) with a
  hand-built ``ProfileResult(is_structural=True)``, skipping the Profiler.

Both paths re-emit integer-unit HPGL (``PU``/``PD``/``AA``) framed by the
``IN;PA;`` header and ``SP;`` footer (EngraveLab reference framing): the
per-cutter files are single-tool streams and carry no ``SP`` pen selects.

Coordinate chain (verified against the emitted per-cutter files; all
values in plotter units, ``HPGL_UNITS_PER_INCH`` = 1016 units per inch):

1. ``x1 = round(x_local * 1016)``, ``y1 = round(y_local * 1016)``
2. vertical centering: ``y2 = round(y1 + shift)`` where
   ``shift = (height / 2 - band_offset - hole_shift) * 1016 - (text_y_min + text_y_max) / 2``
   over the union of the label's text-pen vertices (mirrors
   ``_center_text_layer_vertically``; the label margin cancels out).
   ``band_offset`` is ``RenderedLabel.band_center_offset_inches`` -- ``0.0`` for
   ink-centered labels, non-zero when the label centers by its
   baseline-to-capline band. ``hole_shift`` is
   ``ResolvedLabel.hole_aware_vertical_shift_inches`` -- ``0.0`` when
   top and bottom holes are symmetric or absent.
3. Y-mirror: ``y3 = flip_span - y2`` where ``flip_span`` is the rendered
   label's post-flip Y bounds sum (mirrors
   ``_flip_y_coordinates_in_plt``).
4. rotation (packed sideways only): ``(x, y) -> (rot_y_max - y, x - rot_x_min)``
   (mirrors :func:`~plt_optimizer.generate.vectorize.rotate_plt_content_90cw`).
5. slot translation by the packed ``(x, y)`` in plotter units.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

from plt_optimizer.core.chunker import MacroBlock
from plt_optimizer.core.models import (
    ArcSegment,
    Coordinate,
    PLTDocument,
    Segment,
    StrokePath,
    StrokeSegment,
)
from plt_optimizer.core.optimizer import OptimizationStrategy
from plt_optimizer.core.pipeline import (
    OptimizationOutcome,
    chunk_document,
    optimize_and_reassemble,
    preprocess_document,
)
from plt_optimizer.core.profiler import ProfileResult
from plt_optimizer.core.units import HPGL_UNITS_PER_INCH, inches_to_units
from plt_optimizer.generate.geometry import arc_swept_bounds
from plt_optimizer.generate.label_renderer import (
    PLT_FOOTER,
    PLT_HEADER,
    RenderedLabel,
    TextChunkRecord,
)
from plt_optimizer.generate.packing.layout import PackedLabel
from plt_optimizer.generate.resolution import ResolvedLabel
from plt_optimizer.generate.text_geometry import LineSeg, Stroke, TextBlock
from plt_optimizer.utils.logging import TextLogger

# Plotter units per inch (HPGL's default 0.025 mm step; see
# plt_optimizer.core.units for the derivation) used throughout the plate-space
# transform chain. Kept in sync with the export centreing/flip math in
# label_renderer by construction: both read the same constant.
_UNITS_PER_INCH: float = HPGL_UNITS_PER_INCH

# Factory building the optimization strategy for one routing problem, given
# the unoptimized baseline rapid-travel distance (ParallelEnsemble uses it
# for improvement reporting; fast-mode strategies ignore it).
StrategyFactory = Callable[[float], OptimizationStrategy]


@dataclass(frozen=True)
class PlateOptimization:
    """One optimized plate layer, ready to write.

    Attributes:
        content: Optimized HPGL content (header, geometry, footer).
        outcome: Pipeline outcome (method name, distances, benchmarks).
        baseline_distance: Rapid-travel distance before optimization.
        node_count: Number of routing nodes (blocks) optimized.
    """

    content: str
    outcome: OptimizationOutcome
    baseline_distance: float
    node_count: int


@dataclass(frozen=True)
class _LabelTransform:
    """Per-label constants mapping label-local inches to device units.

    Attributes:
        shift: Vertical centering offset (plotter units) applied before the
            Y-mirror, shared by every text pen of the label.
        flip_span: Sum of the rendered label's Y bounds in plotter units
            (the mirror span of the export Y-flip).
        rot_y_max: Rendered label Y maximum in plotter units (rotation pivot).
        rot_x_min: Rendered label X minimum in plotter units (rotation pivot).
    """

    shift: float
    flip_span: int
    rot_y_max: int
    rot_x_min: int


def _units(value_inches: float) -> int:
    """Convert inches to plotter units with the pipeline's rounding.

    Args:
        value_inches: Coordinate in inches.

    Returns:
        Integer plotter units (``round(x * HPGL_UNITS_PER_INCH)``).
    """
    return inches_to_units(value_inches)


def _block_text_y_extents(block: TextBlock) -> List[int]:
    """Collect a text block's Y extents in plotter units (label-local frame).

    Mirrors ``label_renderer._center_text_layer_vertically``'s view of the
    emitted HPGL exactly (same rounding, same pen-tracking quirks of
    ``_collect_hpgl_geometry`` and ``Stroke.to_hpgl`` emission):

    - The stroke-leading ``PU{pen_up}`` records ``pen_up``.
    - A line run's start is emitted (``PU``) only when the pen must
      re-anchor there; the batched ``PD`` records every later run vertex.
    - An ``AA`` measures its *swept* extent from the last recorded PU/PD
      pen position (an arc never advances that measured pen, so chained
      arcs measure from the same start) -- never the full circle. Its bare
      ``PD`` carries no coordinates and records nothing.

    Matching the HPGL side exactly keeps the plate-space centering shift
    identical to the shift the emitted label received.

    Args:
        block: The label-local text block.

    Returns:
        Y extents in integer plotter units (empty for empty blocks).
    """
    ys: List[int] = []
    # Last recorded PU/PD point in plotter units (the measured pen of
    # _collect_hpgl_geometry); None until the first point is recorded.
    last_x: Optional[int] = None
    last_y: Optional[int] = None

    def _record(x_units: int, y_units: int) -> None:
        nonlocal last_x, last_y
        ys.append(y_units)
        last_x, last_y = x_units, y_units

    for stroke in block.strokes:
        # The stroke is always PU-led to pen_up (see Stroke.to_hpgl).
        pen: complex = stroke.pen_up
        _record(_units(pen.real), _units(pen.imag))
        prev_was_line = False
        for segment in stroke.segments:
            if isinstance(segment, LineSeg):
                if not prev_was_line and segment.start != pen:
                    # Run start: emitted as a re-anchoring PU.
                    _record(_units(segment.start.real), _units(segment.start.imag))
                _record(_units(segment.end.real), _units(segment.end.imag))
                pen = segment.end
                prev_was_line = True
                continue
            prev_was_line = False
            if segment.start != pen:
                # to_hpgl emits PU{start}: the measured pen re-anchors.
                _record(_units(segment.start.real), _units(segment.start.imag))
            assert last_x is not None and last_y is not None  # noqa: S101 - PU always records
            center_x = _units(segment.center.real)
            center_y = _units(segment.center.imag)
            # ArcExtent stores an integer-unit radius; mirror the rounding.
            radius = int(round(math.hypot(last_x - center_x, last_y - center_y)))
            start_angle = math.degrees(math.atan2(last_y - center_y, last_x - center_x))
            _x_min, y_min, _x_max, y_max = arc_swept_bounds(
                center_x, center_y, radius, start_angle, segment.sweep_deg
            )
            ys.append(int(math.floor(y_min)))
            ys.append(int(math.ceil(y_max)))
            # The measured pen does NOT advance across the arc (mirrors
            # _collect_hpgl_geometry); the inch pen does, for the next
            # segment's PU decision in to_hpgl.
            pen = segment.end
    return ys


def _label_transform(rendered: RenderedLabel, label: ResolvedLabel) -> _LabelTransform:
    """Compute the label-local -> device-unit transform constants.

    Args:
        rendered: The rendered label (supplies text geometry and the
            post-export bounds used by the Y-mirror/rotation pivots).
        label: The resolved label (supplies nominal height for centering).

    Returns:
        The :class:`_LabelTransform` for this label. Labels without text
        yield an identity-like transform (unused).
    """
    ys: List[int] = []
    for record in rendered.text_chunks:
        for block in record.blocks:
            ys.extend(_block_text_y_extents(block))
    if ys:
        current_center = (min(ys) + max(ys)) / 2.0
    else:  # No text: centering is a no-op.
        current_center = 0.0
    # expected center = margin + (height - 2*margin)/2 == height/2 (inches),
    # lowered by the band-center offset and the hole-aware shift.
    # Mirrors _center_text_layer_vertically exactly.
    expected_center = (
        label.height / 2.0
        - rendered.band_center_offset_inches
        - label.hole_aware_vertical_shift_inches
    ) * _UNITS_PER_INCH
    return _LabelTransform(
        shift=expected_center - current_center,
        flip_span=_units(rendered.y_min) + _units(rendered.y_max),
        rot_y_max=_units(rendered.y_max),
        rot_x_min=_units(rendered.x_min),
    )


def _transform_point(
    transform: _LabelTransform,
    x_local: float,
    y_local: float,
    rotated: bool,
    dx_units: int,
    dy_units: int,
) -> Tuple[int, int]:
    """Map one label-local inch coordinate to plate device plotter units.

    Args:
        transform: The label's :class:`_LabelTransform`.
        x_local: X in label-local inches (+y up, pre-export frame).
        y_local: Y in label-local inches.
        rotated: Whether the packer placed this label sideways.
        dx_units: Slot translation X in plotter units.
        dy_units: Slot translation Y in plotter units.

    Returns:
        The ``(x, y)`` device coordinate in integer plotter units.
    """
    x1 = int(round(x_local * _UNITS_PER_INCH))
    y1 = int(round(y_local * _UNITS_PER_INCH))
    y2 = int(round(y1 + transform.shift))
    y3 = transform.flip_span - y2
    if rotated:
        x4 = transform.rot_y_max - y3
        y4 = x1 - transform.rot_x_min
    else:
        x4, y4 = x1, y3
    return x4 + dx_units, y4 + dy_units


def _transform_center(
    transform: _LabelTransform,
    center: complex,
    rotated: bool,
    dx_units: int,
    dy_units: int,
) -> Tuple[float, float]:
    """Map one arc center through the label transform WITHOUT rounding.

    Mirrors ``_transform_hpgl_coordinates``'s treatment of ``AA`` centers:
    the center is mapped like any point, but its float value is only
    rounded at emission (``_format_segment``), keeping huge-radius glyph
    arcs exact through the chain.

    Args:
        transform: The label's :class:`_LabelTransform`.
        center: Arc center in label-local inches.
        rotated: Whether the packer placed this label sideways.
        dx_units: Slot translation X in plotter units.
        dy_units: Slot translation Y in plotter units.

    Returns:
        The ``(x, y)`` device center as floats (unrounded plotter units).
    """
    x1 = center.real * _UNITS_PER_INCH
    y1 = center.imag * _UNITS_PER_INCH
    y2 = y1 + transform.shift
    y3 = transform.flip_span - y2
    if rotated:
        x4 = transform.rot_y_max - y3
        y4 = x1 - transform.rot_x_min
    else:
        x4, y4 = x1, y3
    return x4 + dx_units, y4 + dy_units


def _stroke_to_path(
    transform: _LabelTransform,
    stroke: Stroke,
    rotated: bool,
    dx_units: int,
    dy_units: int,
) -> StrokePath:
    """Convert one label-local arc-native stroke to a device StrokePath.

    Line segments map point-wise through the same chain the historical
    polyline path used (:func:`_transform_point`, integer-rounded).
    Arcs map start and center like points and NEGATE the sweep: the label
    transform chain always contains exactly one Y-mirror (the device flip;
    rotations and translations preserve sweeps), matching
    ``_transform_hpgl_coordinates(flip_y_axis=True)`` on the emitted HPGL.
    The arc end is derived from the transformed start/center/sweep with the
    parser's own ``AA`` geometry so downstream re-emission stays exact.

    Args:
        transform: The label's :class:`_LabelTransform`.
        stroke: The arc-native stroke (label-local inches).
        rotated: Whether the packer placed this label sideways.
        dx_units: Slot translation X in plotter units.
        dy_units: Slot translation Y in plotter units.

    Returns:
        A :class:`~plt_optimizer.core.models.StrokePath` in device units
        (pen-up at the stroke's rapid target). Empty strokes yield a path
        with no segments.
    """
    pen_x, pen_y = _transform_point(
        transform, stroke.pen_up.real, stroke.pen_up.imag, rotated, dx_units, dy_units
    )
    pen_up = Coordinate(float(pen_x), float(pen_y))
    segments: List[Segment] = []
    for segment in stroke.segments:
        if isinstance(segment, LineSeg):
            end_x, end_y = _transform_point(
                transform, segment.end.real, segment.end.imag, rotated, dx_units, dy_units
            )
            start_x, start_y = _transform_point(
                transform, segment.start.real, segment.start.imag, rotated, dx_units, dy_units
            )
            segments.append(
                StrokeSegment(
                    start=Coordinate(float(start_x), float(start_y)),
                    end=Coordinate(float(end_x), float(end_y)),
                    is_cutting=True,
                )
            )
            continue
        start_x, start_y = _transform_point(
            transform, segment.start.real, segment.start.imag, rotated, dx_units, dy_units
        )
        center_x, center_y = _transform_center(
            transform, segment.center, rotated, dx_units, dy_units
        )
        sweep = -segment.sweep_deg  # exactly one Y-mirror in the chain
        start = Coordinate(float(start_x), float(start_y))
        end = _arc_end_device(start, Coordinate(center_x, center_y), sweep)
        segments.append(
            ArcSegment(
                start=start,
                end=end,
                center=Coordinate(center_x, center_y),
                sweep_angle=sweep,
                is_cutting=True,
            )
        )
    return StrokePath(pen_up_position=pen_up, segments=tuple(segments))


def _arc_end_device(start: Coordinate, center: Coordinate, sweep_deg: float) -> Coordinate:
    """Derive an arc's end point with the core parser's ``AA`` geometry.

    Args:
        start: Arc start in device units.
        center: Arc center in device units (unrounded).
        sweep_deg: Signed sweep angle in degrees (device convention).

    Returns:
        The arc end point in device units.
    """
    radius = start.distance_to(center)
    theta_start = math.atan2(start.y - center.y, start.x - center.x)
    theta_end = theta_start + math.radians(sweep_deg)
    return Coordinate(
        center.x + radius * math.cos(theta_end),
        center.y + radius * math.sin(theta_end),
    )


def _record_paths(
    transform: _LabelTransform,
    record: TextChunkRecord,
    rotated: bool,
    dx_units: int,
    dy_units: int,
) -> Tuple[StrokePath, ...]:
    """Convert a chunk record's blocks into device-space stroke paths.

    Args:
        transform: The label's :class:`_LabelTransform`.
        record: The chunk record (line- or word-level).
        rotated: Whether the packer placed this label sideways.
        dx_units: Slot translation X in plotter units.
        dy_units: Slot translation Y in plotter units.

    Returns:
        Non-empty paths of the record, in block/stroke order.
    """
    paths, _glyph_groups = _record_paths_with_glyphs(transform, record, rotated, dx_units, dy_units)
    return paths


def _record_paths_with_glyphs(
    transform: _LabelTransform,
    record: TextChunkRecord,
    rotated: bool,
    dx_units: int,
    dy_units: int,
) -> Tuple[Tuple[StrokePath, ...], Tuple[Tuple[int, ...], ...]]:
    """Convert a chunk record into device paths, remapping its glyph groups.

    Same conversion as :func:`_record_paths`, additionally translating the
    record's per-glyph stroke groups (indices into the record's flattened
    stroke sequence) into path indices. Strokes dropped by the conversion
    (empty strokes / segment-less paths) drop out of their glyph group, and
    glyph groups left empty by dropping are removed.

    Args:
        transform: The label's :class:`_LabelTransform`.
        record: The chunk record (line- or word-level).
        rotated: Whether the packer placed this label sideways.
        dx_units: Slot translation X in plotter units.
        dy_units: Slot translation Y in plotter units.

    Returns:
        ``(paths, glyph_groups)`` where ``paths`` are the record's non-empty
        paths in block/stroke order and ``glyph_groups`` lists, per glyph,
        the indices (into ``paths``) of that glyph's paths. Empty tuple when
        the record carries no glyph partition.
    """
    paths: List[StrokePath] = []
    stroke_to_path: Dict[int, int] = {}
    stroke_index = 0
    for block in record.blocks:
        for stroke in block.strokes:
            if not stroke.is_empty:
                path = _stroke_to_path(transform, stroke, rotated, dx_units, dy_units)
                if path.segments:
                    stroke_to_path[stroke_index] = len(paths)
                    paths.append(path)
            stroke_index += 1

    glyph_groups: List[Tuple[int, ...]] = []
    for group in record.glyph_groups:
        mapped = tuple(stroke_to_path[i] for i in group if i in stroke_to_path)
        if mapped:
            glyph_groups.append(mapped)
    return tuple(paths), tuple(glyph_groups)


def _rapid_distance(paths: Sequence[StrokePath]) -> float:
    """Sum the rapid (pen-up) travel between consecutive paths.

    Args:
        paths: Chronologically ordered paths.

    Returns:
        Total rapid-travel distance in plotter units.
    """
    doc = PLTDocument(stroke_paths=list(paths))
    return doc.rapid_distance()


def build_text_blocks(
    packed_labels: Sequence[PackedLabel],
    rendered_labels_map: Dict[str, RenderedLabel],
    pen: int,
) -> List[MacroBlock]:
    """Build one routing node per text chunk of a plate's cutter layer.

    Chunks are collected in plate order (packed-label order, then the
    label's record order) so the baseline distance reflects the unoptimized
    emission order.

    Args:
        packed_labels: Labels placed on the plate.
        rendered_labels_map: Cache of rendered labels by label ID.
        pen: The cutter pen whose chunks form this layer.

    Returns:
        MacroBlocks (one per chunk) with device-unit entrance/exit.
        Empty when the layer has no chunks.
    """
    blocks, _glyph_groups = build_text_blocks_with_glyphs(packed_labels, rendered_labels_map, pen)
    return blocks


StrokeKey = Tuple[Tuple[int, int], Tuple[int, int]]
ArcKey = Tuple[Tuple[int, int], float]
WidthKey = Union[StrokeKey, ArcKey]


def _normalized_key(start: Tuple[int, int], end: Tuple[int, int]) -> StrokeKey:
    """Return the direction-independent ``(start, end)`` key.

    The glyph/direction sweeps reverse whole paths (segment order swapped,
    start/end exchanged, arc sweeps negated), and the merger stitches paths
    tip-to-tail, so the parsed stream may traverse a stroke in either
    direction. Keying on the sorted endpoint pair makes the lookup direction
    agnostic.
    """
    return (start, end) if start <= end else (end, start)


def _arc_key(center: Coordinate, sweep_angle: float) -> ArcKey:
    """Return the direction-independent ``(center, |sweep|)`` key of an arc.

    A line segment identifies itself by its endpoints, but a reversed
    traversal re-chains an ``AA`` arc from its (integer-rounded) exit point,
    and the parser's float arithmetic then lands its endpoints a plotter unit
    off the forward chain -- an endpoint key would miss. The center and the
    sweep MAGNITUDE are traversal invariants the emission carries exactly
    (:func:`_format_segment` rounds the center to integer units and the
    reversal only negates the sweep), so arcs key on them. Two arcs sharing a
    center AND |sweep| belong to the same glyph circle (a full circle is
    emitted as equal-sweep arcs about one center) and share one character's
    engraved width, so the key is collision-safe.
    """
    center_units = (int(round(center.x)), int(round(center.y)))
    magnitude = abs(sweep_angle)
    # Match _format_segment's emission precision (int text, else 3 decimals)
    # so the parsed float(parts[2]) lands on exactly this value.
    magnitude = float(int(magnitude)) if magnitude == int(magnitude) else float(f"{magnitude:.3f}")
    return (center_units, magnitude)


def line_width_key(start: Tuple[int, int], end: Tuple[int, int]) -> StrokeKey:
    """Return the width-map key of a line segment from integer endpoints.

    Public for the preview lookup's ±1-unit probe (the combined proof parses
    the un-optimized assembly, whose centering rounds the Y shift at a
    different point than the plate transform, so a line's endpoints land up
    to one plotter unit off the map keys).
    """
    return _normalized_key(start, end)


def segment_width_key(segment: Segment) -> WidthKey:
    """Return the width-map key of one PARSED segment.

    The shared key function of :func:`build_stroke_width_map` (which keys the
    pre-optimization paths) and the preview lookup (which keys the parsed
    emitted segments): line segments by normalized integer endpoints, arcs by
    ``(center, |sweep|)``. Both sides read the same emission-exact values, so
    the lookup survives the direction/glyph sweeps and the merger.
    """
    if isinstance(segment, ArcSegment):
        return _arc_key(segment.center, segment.sweep_angle)
    start = (int(round(segment.start.x)), int(round(segment.start.y)))
    end = (int(round(segment.end.x)), int(round(segment.end.y)))
    return _normalized_key(start, end)


def _parsed_arc_end(
    start: Tuple[float, float], center: Tuple[int, int], sweep: float
) -> Tuple[float, float]:
    """Reproduce the core parser's ``AA`` end point from the EMITTED values.

    The parser derives an arc's end from the CURRENT chained pen position (a
    3-decimal ``Coordinate`` float), the integer center and the sweep text it
    parses back (see
    :meth:`plt_optimizer.core.parser.PLTParser._parse_arc_command`), and a
    ``PD`` that follows an ``AA`` chain STARTS at that float (the emitted
    integer of the arc end never reaches the file). A line keyed on the
    renderer's own transformed endpoints therefore drifts a unit off the
    parsed stream; keying on this chain matches it exactly. The chain must
    carry the FLOAT position (not a per-step integer rounding): glyph
    outlines are long ``AA`` chains and each step's rounding compounds.

    Args:
        start: The chained pen position (device units, 3-decimal float).
        center: The emitted (integer-rounded) arc center.
        sweep: The emitted sweep in degrees (sign = traversal direction).

    Returns:
        The parser's arc end as a 3-decimal float pair (Coordinate rounding).
    """
    cx, cy = float(center[0]), float(center[1])
    sx, sy = float(start[0]), float(start[1])
    radius = math.hypot(sx - cx, sy - cy)
    theta = math.atan2(sy - cy, sx - cx) + math.radians(sweep)
    return round(cx + radius * math.cos(theta), 3), round(cy + radius * math.sin(theta), 3)


def _emit_sweep(sweep: float) -> float:
    """Round a sweep to the precision :func:`_format_segment` emits."""
    return float(int(sweep)) if sweep == int(sweep) else float(f"{sweep:.3f}")


def _path_line_keys(path: StrokePath) -> List[WidthKey]:
    """Return the parsed-stream keys of a path's LINE segments.

    Chains the pen position exactly as the parser does -- forward from the
    emitted pen-up, and reversed from the path's integer exit point (the
    pen-up a reversed traversal emits) with negated arc sweeps -- so a line
    following an ``AA`` chain keys on the parser's float-derived start in
    either traversal direction. Arcs are keyed separately by
    :func:`segment_width_key` (center + |sweep|), which no chain can drift.
    """
    keys: List[WidthKey] = []

    def _chain(
        position: Tuple[float, float], segments: Sequence[Segment], *, reversed_walk: bool
    ) -> None:
        for segment in segments:
            start = position
            if isinstance(segment, ArcSegment):
                center = (int(round(segment.center.x)), int(round(segment.center.y)))
                sweep = -segment.sweep_angle if reversed_walk else segment.sweep_angle
                position = _parsed_arc_end(start, center, _emit_sweep(sweep))
            else:
                # A reversed traversal flips every segment (start <-> end), so
                # the parsed stream's terminal coordinate is the ORIGINAL
                # segment's start. PU/PD coordinates emit as integers.
                terminal = segment.start if reversed_walk else segment.end
                position = (float(int(round(terminal.x))), float(int(round(terminal.y))))
                keys.append(
                    _normalized_key(
                        (int(round(start[0])), int(round(start[1]))),
                        (int(round(position[0])), int(round(position[1]))),
                    )
                )

    if path.pen_up_position is not None and path.segments:
        entry = (
            float(int(round(path.pen_up_position.x))),
            float(int(round(path.pen_up_position.y))),
        )
        _chain(entry, path.segments, reversed_walk=False)
        last = path.segments[-1]
        reverse_entry = (
            float(int(round(last.end.x))),
            float(int(round(last.end.y))),
        )
        _chain(reverse_entry, list(reversed(path.segments)), reversed_walk=True)
    return keys


def build_stroke_width_map(
    packed_labels: Sequence[PackedLabel],
    rendered_labels_map: Dict[str, RenderedLabel],
) -> Dict[WidthKey, float]:
    """Map every text stroke's device-space endpoints to its engraved width.

    The per-cutter pen split keys on the PHYSICAL tool, so a ``<cb:D>`` span
    shares its pen (and its PLT file) with the surrounding text; the emitted
    HPGL carries no width channel. The simple-outline PDF previews draw each
    pen at one width (the pen's widest engraved stroke), which inflates the
    span's narrower neighbours (and every line sharing the tool). This map
    gives the previews the per-stroke truth: each chunk record's
    :attr:`TextChunkRecord.stroke_widths` is walked
    through the exact plate-space transform the emission uses
    (:func:`_record_paths_with_glyphs`), keyed per segment with
    :func:`segment_width_key` (endpoints for lines, center + |sweep| for
    arcs; both survive the sweeps, the merger, and the emission rounding). A
    job whose buffers are all zero maps every stroke to its physical cutter,
    which equals the pen width -- the previews stay visually identical.

    Args:
        packed_labels: Labels placed on the plate.
        rendered_labels_map: Cache of rendered labels by label ID.

    Returns:
        Mapping of :func:`segment_width_key` keys (line segments: normalized
        integer device endpoint pairs; arcs: ``(center, |sweep|)``) to
        engraved widths in inches. Empty when no chunk record carries
        per-stroke widths.
    """
    widths: Dict[WidthKey, float] = {}
    for packed_label in packed_labels:
        rendered = rendered_labels_map.get(packed_label.source_label.id)
        if rendered is None or not rendered.text_chunks:
            continue
        transform = _label_transform(rendered, packed_label.source_label)
        dx_units = _units(packed_label.x)
        dy_units = _units(packed_label.y)
        for record in rendered.text_chunks:
            if not record.stroke_widths:
                continue
            paths, _glyph_groups = _record_paths_with_glyphs(
                transform, record, packed_label.rotated, dx_units, dy_units
            )
            # The record's widths align with its flattened stroke sequence;
            # _record_paths_with_glyphs drops empty strokes, so walk the same
            # non-empty strokes in the same order.
            strokes = [stroke for block in record.blocks for stroke in block.strokes]
            live = [(index, stroke) for index, stroke in enumerate(strokes) if not stroke.is_empty]
            if len(live) != len(paths):
                continue  # defensive: transform dropped geometry -> pen width
            for (stroke_index, _stroke), path in zip(live, paths):
                if stroke_index >= len(record.stroke_widths):
                    continue
                width = record.stroke_widths[stroke_index]
                for segment in path.segments:
                    if isinstance(segment, ArcSegment):
                        widths[segment_width_key(segment)] = width
                for key in _path_line_keys(path):
                    widths[key] = width
    return widths


def build_text_blocks_with_glyphs(
    packed_labels: Sequence[PackedLabel],
    rendered_labels_map: Dict[str, RenderedLabel],
    pen: int,
) -> Tuple[List[MacroBlock], Dict[int, Tuple[Tuple[int, ...], ...]]]:
    """Build the text routing nodes plus their per-block glyph partitions.

    Same blocks as :func:`build_text_blocks`, additionally returning the
    per-glyph path groups needed by the intra-chunk glyph direction sweep,
    keyed by ``block_id``. Blocks whose chunk carries no glyph partition are
    absent from the map.

    Args:
        packed_labels: Labels placed on the plate.
        rendered_labels_map: Cache of rendered labels by label ID.
        pen: The cutter pen whose chunks form this layer.

    Returns:
        ``(blocks, glyph_groups_by_block)``: the routing nodes (one per
        chunk, device-unit entrance/exit) and, for each block with a glyph
        partition, the per-glyph tuples of indices into that block's paths.
    """
    blocks: List[MacroBlock] = []
    glyph_groups_by_block: Dict[int, Tuple[Tuple[int, ...], ...]] = {}
    for packed_label in packed_labels:
        rendered = rendered_labels_map.get(packed_label.source_label.id)
        if rendered is None or not rendered.text_chunks:
            continue
        transform = _label_transform(rendered, packed_label.source_label)
        dx_units = _units(packed_label.x)
        dy_units = _units(packed_label.y)
        for record in rendered.text_chunks:
            if record.pen != pen:
                continue
            paths, glyph_groups = _record_paths_with_glyphs(
                transform, record, packed_label.rotated, dx_units, dy_units
            )
            if not paths:
                continue
            first = paths[0].segments[0]
            last = paths[-1].segments[-1]
            block_id = len(blocks)
            blocks.append(
                MacroBlock(
                    block_id=block_id,
                    paths=paths,
                    entrance=first.start,
                    exit=last.end,
                )
            )
            if glyph_groups:
                glyph_groups_by_block[block_id] = glyph_groups
    return blocks, glyph_groups_by_block


def _format_point(x: int, y: int) -> str:
    """Format one integer device coordinate pair (``x,y``).

    Args:
        x: X in plotter units.
        y: Y in plotter units.

    Returns:
        Comma-joined pair string.
    """
    return f"{x},{y}"


def _format_segment(segment: Segment) -> str:
    """Format one segment's terminal command (``PD``/``PU``/``AA``).

    Args:
        segment: The stroke or arc segment (already pen-down-aware).

    Returns:
        HPGL command text without the trailing semicolon.
    """
    cmd = "PD" if segment.is_cutting else "PU"
    if isinstance(segment, ArcSegment):
        # Integral sweeps (drill holes) emit as ints for bit-parity; font
        # glyph arcs carry decimal sweeps that must survive verbatim.
        sweep = segment.sweep_angle
        angle_text = str(int(sweep)) if sweep == int(sweep) else f"{sweep:.3f}"
        return f"{cmd};AA{int(round(segment.center.x))},{int(round(segment.center.y))},{angle_text}"
    return f"{cmd}{_format_point(int(round(segment.end.x)), int(round(segment.end.y)))}"


def _emit_path(path: StrokePath) -> str:
    """Emit one stroke path as PU/PD/AA commands (integer units).

    Args:
        path: The path to emit.

    Returns:
        HPGL text (semicolon-separated, no trailing semicolon). Empty for
        fully degenerate paths.
    """
    if not path.segments:  # pragma: no cover - callers filter empty paths
        return ""
    parts: List[str] = []
    if path.pen_up_position is not None:
        parts.append(
            f"PU{_format_point(int(round(path.pen_up_position.x)), int(round(path.pen_up_position.y)))}"
        )
    for segment in path.segments:
        parts.append(_format_segment(segment))
    return ";".join(parts)


def emit_layer_document(paths: Sequence[StrokePath]) -> str:
    """Emit a complete HPGL document for one optimized plate layer.

    Per-cutter files carry no ``SP`` pen selects: each file is one cutter's
    toolpath, so the geometry is a bare ``PU``/``PD``/``AA`` stream framed by
    the ``IN;PA;`` header and the ``SP;`` footer (EngraveLab reference
    framing). Every path is pen-up-led, so dropping the selects cannot join
    two paths with a spurious cut.

    Args:
        paths: Optimized, ordered stroke paths.

    Returns:
        Full HPGL content (``IN;PA;...SP;``). Empty-geometry layers still
        produce a valid (geometry-free) document.
    """
    lines: List[str] = [PLT_HEADER]
    for path in paths:
        if not path.segments:
            continue
        lines.append(_emit_path(path) + ";")
    lines.append(PLT_FOOTER)
    return "".join(lines)


def optimize_text_layer(
    packed_labels: Sequence[PackedLabel],
    rendered_labels_map: Dict[str, RenderedLabel],
    pen: int,
    strategy_factory: StrategyFactory,
    logger: Optional[TextLogger] = None,
    log_prefix: str = "",
    intra_sweep: bool = True,
    merge_coincident: bool = True,
) -> Optional[PlateOptimization]:
    """Optimize one plate's text layer in plate (device) space.

    Args:
        packed_labels: Labels placed on the plate.
        rendered_labels_map: Cache of rendered labels by label ID.
        pen: Cutter pen whose chunks form this layer.
        strategy_factory: Builds the strategy for the run (see
            :data:`StrategyFactory`).
        logger: Optional logger forwarded to the pipeline helper.
        log_prefix: Prefix for log messages.
        intra_sweep: When True (the default), the pipeline additionally
            sweeps each chunk's per-glyph stroke directions after the
            inter-chunk routing (see
            :func:`plt_optimizer.core.glyph_sweep.sweep_glyph_directions`).
        merge_coincident: When True (the default), the pipeline stitches
            tip-to-tail strokes after reassembly so the redundant tool-up
            between them disappears from the emitted layer (see
            :func:`plt_optimizer.core.path_merger.merge_coincident_paths`).

    Returns:
        The :class:`PlateOptimization`, or ``None`` when the layer has no
        chunks to route.
    """
    blocks, glyph_groups_by_block = build_text_blocks_with_glyphs(
        packed_labels, rendered_labels_map, pen
    )
    if not blocks:
        return None

    all_paths = [path for block in blocks for path in block.paths]
    baseline_distance = _rapid_distance(all_paths)
    document = PLTDocument(stroke_paths=all_paths)
    outcome = optimize_and_reassemble(
        document,
        blocks,
        strategy_factory(baseline_distance),
        logger=logger,
        log_prefix=log_prefix,
        intra_sweep=intra_sweep,
        glyph_groups_by_block=glyph_groups_by_block,
        merge_coincident=merge_coincident,
    )
    content = emit_layer_document(outcome.optimized_doc.stroke_paths)
    return PlateOptimization(
        content=content,
        outcome=outcome,
        baseline_distance=baseline_distance,
        node_count=len(blocks),
    )


def optimize_structural_layer(
    plt_content: str,
    strategy_factory: StrategyFactory,
    logger: Optional[TextLogger] = None,
    log_prefix: str = "",
    merge_coincident: bool = True,
) -> Optional[PlateOptimization]:
    """Optimize a plate's borders+holes layer (known-structural content).

    The extracted HPGL is parsed, preprocessed like the file-based CLIs
    (fracture + dedupe), chunked 1:1 via the structural chunker bypass,
    and re-routed -- all without the Profiler: the content is declared
    structural up front.

    Args:
        plt_content: Extracted borders+holes HPGL content of one plate.
        strategy_factory: Builds the strategy for the run (see
            :data:`StrategyFactory`).
        logger: Optional logger forwarded to the pipeline helper.
        log_prefix: Prefix for log messages.
        merge_coincident: When True (the default), the pipeline stitches
            tip-to-tail strokes after reassembly so the redundant tool-up
            between them disappears from the emitted layer (see
            :func:`plt_optimizer.core.path_merger.merge_coincident_paths`).

    Returns:
        The :class:`PlateOptimization`, or ``None`` when the layer has no
        chunkable geometry.
    """
    from plt_optimizer.core.parser import PLTParser

    parsed = PLTParser().parse_string(plt_content)
    if not parsed.stroke_paths:
        return None

    # Log parsed path count for diagnostics
    from plt_optimizer.utils.geometry import is_closed_path

    parsed_path_count = len(parsed.stroke_paths)
    closed_path_count = sum(1 for path in parsed.stroke_paths if is_closed_path(path))

    # Check segment distribution to understand path structure
    seg_counts: dict[int, int] = {}
    for path in parsed.stroke_paths:
        seg_count = len(path.segments)
        seg_counts[seg_count] = seg_counts.get(seg_count, 0) + 1

    if logger is not None:
        logger.info(f"{log_prefix}Parsed {parsed_path_count} paths ({closed_path_count} closed)")
        logger.info(f"{log_prefix}Segment distribution: {dict(sorted(seg_counts.items()))}")

    document = preprocess_document(parsed, is_structural=True, logger=logger, log_prefix=log_prefix)

    # Log preprocessed path count
    if logger is not None:
        logger.info(f"{log_prefix}After preprocessing: {len(document.stroke_paths)} paths")

    # Known-kind shortcut: structural content ignores baseline_extent, and
    # the chunker's structural branch maps every path to its own block.
    profile = ProfileResult(
        baseline_extent=0.0,
        median_dx=0.0,
        median_dy=0.0,
        total_strokes=0,
        p95_index=0,
        is_structural=True,
    )
    blocks = chunk_document(document, profile)
    if not blocks:  # pragma: no cover - chunker raises on empty input
        return None
    baseline_distance = _rapid_distance([path for block in blocks for path in block.paths])
    outcome = optimize_and_reassemble(
        document,
        blocks,
        strategy_factory(baseline_distance),
        logger=logger,
        log_prefix=log_prefix,
        merge_coincident=merge_coincident,
    )
    content = emit_layer_document(outcome.optimized_doc.stroke_paths)
    return PlateOptimization(
        content=content,
        outcome=outcome,
        baseline_distance=baseline_distance,
        node_count=len(blocks),
    )
