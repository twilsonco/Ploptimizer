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

Both paths re-emit integer-unit HPGL (``PU``/``PD``/``AA``) preserving pen
selection, which the generic
:class:`~plt_optimizer.core.writer.PLTWriter` cannot do (it hoists every
header -- including pen selects -- ahead of the geometry).

Coordinate chain (verified against the emitted per-cutter files; all
values in plotter units, 1 inch = 1000 units):

1. ``x1 = round(x_local * 1000)``, ``y1 = round(y_local * 1000)``
2. vertical centering: ``y2 = round(y1 + shift)`` where
   ``shift = height * 500 - (text_y_min + text_y_max) / 2`` over the
   union of the label's text-pen vertices (mirrors
   ``_center_text_layer_vertically``; the label margin cancels out).
3. Y-mirror: ``y3 = flip_span - y2`` where ``flip_span`` is the rendered
   label's post-flip Y bounds sum (mirrors
   ``_flip_y_coordinates_in_plt``).
4. rotation (packed sideways only): ``(x, y) -> (rot_y_max - y, x - rot_x_min)``
   (mirrors :func:`~plt_optimizer.generate.vectorize.rotate_plt_content_90cw`).
5. slot translation by the packed ``(x, y)`` in plotter units.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

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
from plt_optimizer.generate.label_renderer import (
    LAYER_BOUNDARY,
    LAYER_HOLES,
    RenderedLabel,
    TextChunkRecord,
)
from plt_optimizer.generate.layout import PackedLabel
from plt_optimizer.generate.resolution import ResolvedLabel
from plt_optimizer.utils.logging import TextLogger

# Plotter units per inch (HPGL default used throughout the generate pipeline).
_UNITS_PER_INCH: float = 1000.0

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
        Integer plotter units (``round(x * 1000)``).
    """
    return int(round(value_inches * _UNITS_PER_INCH))


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
        for contour in record.contours:
            if len(contour):
                ys.extend(int(round(v)) for v in (contour.imag * _UNITS_PER_INCH).tolist())
    if ys:
        current_center = (min(ys) + max(ys)) / 2.0
    else:  # No text: centering is a no-op.
        current_center = 0.0
    # expected center = margin + (height - 2*margin)/2 == height/2 (inches).
    expected_center = label.height * _UNITS_PER_INCH / 2.0
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


def _contour_to_path(
    transform: _LabelTransform,
    contour: np.ndarray,
    rotated: bool,
    dx_units: int,
    dy_units: int,
) -> StrokePath:
    """Convert one label-local contour (complex vertices) to a StrokePath.

    Args:
        transform: The label's :class:`_LabelTransform`.
        contour: Complex vertex array of the contour (label-local inches).
        rotated: Whether the packer placed this label sideways.
        dx_units: Slot translation X in plotter units.
        dy_units: Slot translation Y in plotter units.

    Returns:
        A pen-down :class:`~plt_optimizer.core.models.StrokePath` in device
        units (pen-up at the first vertex). Degenerate contours yield a
        path with no segments (pen-up only).
    """
    points = [
        _transform_point(transform, point.real, point.imag, rotated, dx_units, dy_units)
        for point in contour
    ]
    if not points:  # pragma: no cover - empty contours are filtered upstream
        return StrokePath(pen_up_position=None, segments=())
    start = Coordinate(float(points[0][0]), float(points[0][1]))
    segments: List[Segment] = []
    previous = start
    for x, y in points[1:]:
        current = Coordinate(float(x), float(y))
        segments.append(StrokeSegment(start=previous, end=current, is_cutting=True))
        previous = current
    return StrokePath(pen_up_position=start, segments=tuple(segments))


def _record_paths(
    transform: _LabelTransform,
    record: TextChunkRecord,
    rotated: bool,
    dx_units: int,
    dy_units: int,
) -> Tuple[StrokePath, ...]:
    """Convert a chunk record's contours into device-space stroke paths.

    Args:
        transform: The label's :class:`_LabelTransform`.
        record: The chunk record (line- or word-level).
        rotated: Whether the packer placed this label sideways.
        dx_units: Slot translation X in plotter units.
        dy_units: Slot translation Y in plotter units.

    Returns:
        Non-empty paths of the record, in contour order.
    """
    paths = [
        _contour_to_path(transform, contour, rotated, dx_units, dy_units)
        for contour in record.contours
    ]
    return tuple(path for path in paths if path.segments)


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
    blocks: List[MacroBlock] = []
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
            paths = _record_paths(transform, record, packed_label.rotated, dx_units, dy_units)
            if not paths:
                continue
            first = paths[0].segments[0]
            last = paths[-1].segments[-1]
            blocks.append(
                MacroBlock(
                    block_id=len(blocks),
                    paths=paths,
                    entrance=first.start,
                    exit=last.end,
                )
            )
    return blocks


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
        return (
            f"{cmd};AA{int(round(segment.center.x))},"
            f"{int(round(segment.center.y))},{int(round(segment.sweep_angle))}"
        )
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


def emit_layer_document(
    paths: Sequence[StrokePath],
    pen_of: Callable[[StrokePath], int],
) -> str:
    """Emit a complete HPGL document for one optimized plate layer.

    Pen selection is re-emitted whenever the pen changes, preserving the
    SP-grouped structure of the unoptimized per-cutter files (the generic
    ``PLTWriter`` would hoist pen selects into the header instead).

    Args:
        paths: Optimized, ordered stroke paths.
        pen_of: Maps a path to its HPGL pen number.

    Returns:
        Full HPGL content (``IN;DF;PS0;...SP0;IN;%``). Empty-geometry
        layers still produce a valid (geometry-free) document.
    """
    lines: List[str] = ["IN;DF;PS0;"]
    current_pen: Optional[int] = None
    for path in paths:
        if not path.segments:
            continue
        pen = pen_of(path)
        if pen != current_pen:
            lines.append(f"SP{pen};")
            current_pen = pen
        lines.append(_emit_path(path) + ";")
    lines.append("SP0;IN;%")
    return "".join(lines)


def _text_pen_of(pen: int) -> Callable[[StrokePath], int]:
    """Return a constant pen mapper for a single-cutter text layer.

    Args:
        pen: The layer's pen number.

    Returns:
        Mapper returning ``pen`` for every path.
    """

    def _constant(_path: StrokePath) -> int:
        return pen

    return _constant


def _structural_pen_of(path: StrokePath) -> int:
    """Infer the pen of a structural path (holes vs. borders).

    Drill holes are the only arc-bearing structural content (native HPGL
    ``AA`` macros); borders are pure polylines. Reversal during
    optimization preserves segment types, so this attribution is exact.

    Args:
        path: A path of the borders+holes layer.

    Returns:
        :data:`LAYER_HOLES` for arc-bearing paths, else
        :data:`LAYER_BOUNDARY`.
    """
    if any(isinstance(segment, ArcSegment) for segment in path.segments):
        return LAYER_HOLES
    return LAYER_BOUNDARY


def optimize_text_layer(
    packed_labels: Sequence[PackedLabel],
    rendered_labels_map: Dict[str, RenderedLabel],
    pen: int,
    strategy_factory: StrategyFactory,
    logger: Optional[TextLogger] = None,
    log_prefix: str = "",
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

    Returns:
        The :class:`PlateOptimization`, or ``None`` when the layer has no
        chunks to route.
    """
    blocks = build_text_blocks(packed_labels, rendered_labels_map, pen)
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
    )
    content = emit_layer_document(outcome.optimized_doc.stroke_paths, _text_pen_of(pen))
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
) -> Optional[PlateOptimization]:
    """Optimize a plate's borders+holes layer (known-structural content).

    The extracted HPGL is parsed, preprocessed like the file-based CLIs
    (fracture + dedupe), chunked 1:1 via the structural chunker bypass,
    and re-routed -- all without the Profiler: the content is declared
    structural up front.

    Args:
        plt_content: Extracted borders+holes HPGL content of one plate.
        strategy_factory: Builds the strategy for the run.
        logger: Optional logger forwarded to the pipeline helper.
        log_prefix: Prefix for log messages.

    Returns:
        The :class:`PlateOptimization`, or ``None`` when the layer has no
        chunkable geometry.
    """
    from plt_optimizer.core.parser import PLTParser

    parsed = PLTParser().parse_string(plt_content)
    if not parsed.stroke_paths:
        return None
    document = preprocess_document(parsed, is_structural=True, logger=logger, log_prefix=log_prefix)
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
    )
    content = emit_layer_document(outcome.optimized_doc.stroke_paths, _structural_pen_of)
    return PlateOptimization(
        content=content,
        outcome=outcome,
        baseline_distance=baseline_distance,
        node_count=len(blocks),
    )
