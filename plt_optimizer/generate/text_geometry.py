"""Arc-native text geometry for the label rendering pipeline.

The generate pipeline historically represented rendered text as vpype
:class:`~vpype.LineCollection` objects: complex vertex arrays that can only
carry polylines. That is lossy for two important content sources:

- **PLT-extracted fonts** (``Fonts/plt_fonts.json``): glyphs engraved from
  EngraveLab/Vision Pro fonts and reverse-engineered by
  ``Fonts/extract_plt_fonts.py``. Those glyphs are *natively* PU/PD/AA --
  many near-straight strokes are huge-radius best-fit arcs, and flattening
  them would both bloat the toolpath and change the cut.
- **Future TTF arc fitting**: matplotlib glyph outlines can be fitted with
  arcs (planned follow-up); this module is the shared carrier.

This module defines an immutable, inch-space geometry model that keeps arcs
as arcs end-to-end: rendering, positioning (stacking/compression/alignment),
collision bounds, HPGL emission (``plt_content``), the plate-space chunk
records, and the plate transform chain. Emitted HPGL uses integer plotter
units (1 inch = 1000 units) exactly like the polyline path, so downstream
transforms (Y-flip, slot translation, 90 CW rotation) stay exact.

Coordinate conventions match the rest of the render path:

- Label-local inches, ``+y`` up (plotter convention), baseline at y = 0.
- :meth:`TextBlock.to_hpgl` emits plotter-unit PU/PD/AA commands in the
  *same* frame it was built in (the caller applies the device Y-flip).
- Arc sweeps follow the HPGL ``AA`` convention as stored by the core
  parser (:class:`~plt_optimizer.core.models.ArcSegment`): the arc runs
  from its start point through ``sweep_angle`` degrees to its end, with
  the end computed as ``center + r * (cos(theta_start + sweep),
  sin(theta_start + sweep))``. Mirroring the Y axis negates every sweep;
  rotations and translations leave sweeps untouched; a *uniform* scale
  scales centers/radii but never sweeps.

The only operation that cannot preserve arcs is non-uniform horizontal
compression (:meth:`TextBlock.compress_x`): an ellipse is not a circular
arc, so arc segments on a compressed line are flattened to polylines at a
bounded chord-error tolerance.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple, Union

import numpy as np
import vpype as vp

from plt_optimizer.core.models import ArcSegment, StrokePath, StrokeSegment
from plt_optimizer.generate.geometry import TextBounds, arc_swept_bounds

logger = logging.getLogger(__name__)

# Chord-error tolerance (inches) used whenever arcs must be flattened
# because an operation cannot preserve them (horizontal compression).
# 0.0002 in is ~7x finer than the smallest cutter in typical tool sets
# (0.015 in), so the flattening is invisible on the finished part.
ARC_FLATTEN_CHORD_TOL_INCHES: float = 0.0002

# Guard against absurd tessellation counts on degenerate huge sweeps.
_MAX_ARC_SEGMENTS: int = 4096

# Plotter units per inch used by every HPGL emission in the pipeline.
_UNITS_PER_INCH: int = 1000


def _units(value_inches: float) -> int:
    """Convert inches to integer plotter units (the pipeline's 1:1000 scale).

    Args:
        value_inches: Coordinate in inches.

    Returns:
        ``round(value_inches * 1000)`` as an integer.
    """
    return int(round(value_inches * _UNITS_PER_INCH))


def arc_end_point(start: complex, center: complex, sweep_deg: float) -> complex:
    """Return the end point of an arc given its start, center and sweep.

    Mirrors the core parser's AA semantics exactly: the start point sits at
    angle ``theta_start`` on the circle around ``center`` and the end point
    at ``theta_start + sweep`` (radius preserved; the sweep sign encodes
    direction).

    Args:
        start: Arc start point (complex, inches).
        center: Arc center (complex, inches).
        sweep_deg: Signed sweep angle in degrees.

    Returns:
        The arc end point (complex, inches).
    """
    radius = abs(start - center)
    theta_start = math.atan2(start.imag - center.imag, start.real - center.real)
    theta_end = theta_start + math.radians(sweep_deg)
    return center + radius * complex(math.cos(theta_end), math.sin(theta_end))


def arc_start_angle(start: complex, center: complex) -> float:
    """Return the angle (degrees) of the arc start point around its center.

    Args:
        start: Arc start point (complex, inches).
        center: Arc center (complex, inches).

    Returns:
        Angle in degrees (``atan2`` range, -180..180).
    """
    return math.degrees(math.atan2(start.imag - center.imag, start.real - center.real))


def arc_flatten_segments(sweep_deg: float, radius: float) -> int:
    """Return how many chords an arc should be flattened into.

    Chooses the coarser of the chord-error bound and a per-segment angular
    cap so both shallow huge-radius arcs and tight full circles tessellate
    sensibly.

    Args:
        sweep_deg: Signed sweep angle in degrees.
        radius: Arc radius in inches.

    Returns:
        Segment count in ``[1, _MAX_ARC_SEGMENTS]``.
    """
    abs_sweep = abs(sweep_deg)
    if abs_sweep <= 0.0 or radius <= 0.0:
        return 1
    # Chord error for a k-chord split of a sweep s on radius r:
    #   err = r * (1 - cos(s / (2k)))  <=  tol
    #   k >= |s_rad| / (2 * acos(1 - tol / r))   (when tol < r)
    tol = ARC_FLATTEN_CHORD_TOL_INCHES
    if tol >= radius:
        angular = max(2, math.ceil(abs_sweep / 5.0))
    else:
        max_half = math.acos(1.0 - tol / radius)
        angular = max(1, math.ceil(math.radians(abs_sweep) / (2.0 * max_half)))
    angular = max(angular, max(2, math.ceil(abs_sweep / 90.0)))
    return max(1, min(_MAX_ARC_SEGMENTS, angular))


def flatten_arc_to_polyline(start: complex, center: complex, sweep_deg: float) -> List[complex]:
    """Flatten one arc into a chain of chords with bounded chord error.

    Args:
        start: Arc start point (complex, inches).
        center: Arc center (complex, inches).
        sweep_deg: Signed sweep angle in degrees.

    Returns:
        The sampled points INCLUDING ``start`` and the exact arc end
        (``arc_end_point``), so the chain chains tip-to-tail.
    """
    radius = abs(start - center)
    end = arc_end_point(start, center, sweep_deg)
    n = arc_flatten_segments(sweep_deg, radius)
    theta_start = math.atan2(start.imag - center.imag, start.real - center.real)
    sweep_rad = math.radians(sweep_deg)
    points: List[complex] = [start]
    for i in range(1, n):
        theta = theta_start + sweep_rad * i / n
        points.append(center + radius * complex(math.cos(theta), math.sin(theta)))
    points.append(end)
    return points


@dataclass(frozen=True)
class LineSeg:
    """A straight pen-down segment in inches.

    Attributes:
        start: Start point (complex, inches).
        end: End point (complex, inches).
    """

    start: complex
    end: complex

    def bounds(self) -> TextBounds:
        """Return the ``(x_min, y_min, x_max, y_max)`` box of this segment."""
        return (
            min(self.start.real, self.end.real),
            min(self.start.imag, self.end.imag),
            max(self.start.real, self.end.real),
            max(self.start.imag, self.end.imag),
        )

    def translate(self, dx: float, dy: float) -> LineSeg:
        """Return this segment shifted by ``(dx, dy)``."""
        delta = complex(dx, dy)
        return LineSeg(self.start + delta, self.end + delta)

    def scaled(self, factor: float) -> LineSeg:
        """Return this segment scaled uniformly about the origin."""
        return LineSeg(self.start * factor, self.end * factor)

    def mirrored_y(self, span: float) -> LineSeg:
        """Return this segment with Y mirrored (``y' = span - y``)."""
        return LineSeg(
            complex(self.start.real, span - self.start.imag),
            complex(self.end.real, span - self.end.imag),
        )

    def flattened(self, _tol_unused: float = 0.0) -> List[complex]:
        """Return this segment's polyline points (start, end)."""
        return [self.start, self.end]


@dataclass(frozen=True)
class ArcSeg:
    """A circular pen-down arc in inches (HPGL ``AA`` semantics).

    The end point is *derived* (never stored) via :func:`arc_end_point`,
    which keeps the start/center/sweep triple exactly consistent under
    every affine step of the pipeline (translate, uniform scale, Y-mirror,
    90 CW rotation) -- each step transforms start and center like points
    and either negates (mirror) or keeps (rotation) the sweep.

    Attributes:
        start: Start point (complex, inches).
        center: Center of the arc circle (complex, inches).
        sweep_deg: Signed sweep angle in degrees (HPGL AA convention).
    """

    start: complex
    center: complex
    sweep_deg: float

    @property
    def end(self) -> complex:
        """The arc's end point, derived from start/center/sweep."""
        return arc_end_point(self.start, self.center, self.sweep_deg)

    @property
    def radius(self) -> float:
        """The arc radius (distance from start to center)."""
        return abs(self.start - self.center)

    def bounds(self) -> TextBounds:
        """Return the swept bounding box (not the full circle)."""
        start_angle = arc_start_angle(self.start, self.center)
        return arc_swept_bounds(
            self.center.real, self.center.imag, self.radius, start_angle, self.sweep_deg
        )

    def translate(self, dx: float, dy: float) -> ArcSeg:
        """Return this arc shifted by ``(dx, dy)`` (sweep untouched)."""
        delta = complex(dx, dy)
        return ArcSeg(self.start + delta, self.center + delta, self.sweep_deg)

    def scaled(self, factor: float) -> ArcSeg:
        """Return this arc scaled uniformly about the origin (sweep untouched)."""
        return ArcSeg(self.start * factor, self.center * factor, self.sweep_deg)

    def mirrored_y(self, span: float) -> ArcSeg:
        """Return this arc with Y mirrored; the sweep sign is negated."""

        def _mirror(z: complex) -> complex:
            return complex(z.real, span - z.imag)

        return ArcSeg(_mirror(self.start), _mirror(self.center), -self.sweep_deg)

    def flattened(self, _tol_unused: float = 0.0) -> List[complex]:
        """Return polyline points approximating this arc (chord-bounded)."""
        return flatten_arc_to_polyline(self.start, self.center, self.sweep_deg)


TextSegment = Union[LineSeg, ArcSeg]


@dataclass(frozen=True)
class Stroke:
    """One pen-up-led stroke path: a rapid move followed by pen-down segments.

    Mirrors :class:`~plt_optimizer.core.models.StrokePath` in inch space.
    ``pen_up`` is the position the pen travels to (rapid) before the first
    segment; segments chain tip-to-tail (each segment's start equals the
    previous segment's end, or ``pen_up`` for the first).

    Attributes:
        pen_up: Rapid-move target (complex, inches).
        segments: Ordered pen-down segments.
    """

    pen_up: complex
    segments: Tuple[TextSegment, ...]

    @property
    def is_empty(self) -> bool:
        """True when this stroke carries no geometry."""
        return not self.segments

    def bounds(self) -> Optional[TextBounds]:
        """Return the union swept bounds of the segments, or ``None`` if empty."""
        boxes = [seg.bounds() for seg in self.segments]
        if not boxes:
            return None
        return _union_bounds(boxes)

    def translate(self, dx: float, dy: float) -> Stroke:
        """Return this stroke shifted by ``(dx, dy)``."""
        delta = complex(dx, dy)
        return Stroke(
            pen_up=self.pen_up + delta,
            segments=tuple(seg.translate(dx, dy) for seg in self.segments),
        )

    def scaled(self, factor: float) -> Stroke:
        """Return this stroke scaled uniformly about the origin."""
        return Stroke(
            pen_up=self.pen_up * factor,
            segments=tuple(seg.scaled(factor) for seg in self.segments),
        )

    def mirrored_y(self, span: float) -> Stroke:

        def _mirror(z: complex) -> complex:
            return complex(z.real, span - z.imag)

        return Stroke(
            pen_up=_mirror(self.pen_up),
            segments=tuple(seg.mirrored_y(span) for seg in self.segments),
        )

    def to_hpgl(self) -> str:
        """Emit this stroke as PU/PD/AA commands in integer plotter units.

        Emission rules (verified against the core parser and the historical
        ``_linecollection_to_hpgl`` batching for bit-parity):

        - The stroke is always PU-led to :attr:`pen_up`.
        - Consecutive :class:`LineSeg` runs batch into one ``PD`` carrying
          every point *after* the run's start (the PU / previous command
          already positioned the pen there) -- exactly the historical
          polyline framing ``PU{first};PD{rest}``.
        - Arcs emit as the extractor's ``PD;AA{cx},{cy},{sweep}`` form (the
          bare ``PD`` opens/keeps the cutting path, the parser's
          ``PD``-without-coords + next-token ``AA`` branch). A ``PU{start}``
          precedes the plunge whenever the pen is not already on the arc
          start, so a repositioned arc is a rapid, never a phantom cut.
        - Arc sweep angles keep 3-decimal precision (like the font
          extractor): huge-radius best-fit arcs make even half a degree of
          rounding a visible positional error, while coordinates follow the
          pipeline's integer plotter-unit contract.

        Returns:
            HPGL text (semicolon-separated, no trailing semicolon). Empty
            for strokes without segments.
        """
        if not self.segments:
            return ""
        parts: List[str] = [f"PU{_units(self.pen_up.real)},{_units(self.pen_up.imag)}"]
        # Pen position while emitting (inches, chained tip-to-tail).
        pen: complex = self.pen_up
        line_run: List[complex] = []

        def _flush_line() -> None:
            nonlocal pen
            if not line_run:
                return
            if line_run[0] != pen:
                parts.append(f"PU{_units(line_run[0].real)},{_units(line_run[0].imag)}")
            rest = line_run[1:]
            if rest:
                joined = ",".join(f"{_units(p.real)},{_units(p.imag)}" for p in rest)
                parts.append(f"PD{joined}")
            pen = line_run[-1]
            line_run.clear()

        for seg in self.segments:
            if isinstance(seg, LineSeg):
                if not line_run:
                    line_run.append(seg.start)
                line_run.append(seg.end)
            else:
                _flush_line()
                if seg.start != pen:
                    parts.append(f"PU{_units(seg.start.real)},{_units(seg.start.imag)}")
                # The bare PD opens/keeps the cutting path (parser: PD without
                # coordinates followed by an AA token); the leading PU already
                # positioned the pen when seg.start == pen.
                parts.append(
                    "PD;AA{},{},{}".format(
                        _units(seg.center.real),
                        _units(seg.center.imag),
                        f"{seg.sweep_deg:.3f}",
                    )
                )
                pen = seg.end
        _flush_line()
        return ";".join(parts)

    def to_linecollection(self) -> vp.LineCollection:
        """Return a polyline-only vpype view of this stroke (arcs flattened).

        Used by legacy consumers (debug plots, the polyline-only
        ``_render_text_local_with_bounds`` test view). Chord error is bounded
        by :data:`ARC_FLATTEN_CHORD_TOL_INCHES`.

        Returns:
            A LineCollection with one polyline per pen-down run.
        """
        lc = vp.LineCollection()
        for chain in self.polyline_chains():
            if len(chain) >= 2:
                lc.append(np.asarray(chain, dtype=complex))
        return lc

    def polyline_chains(self) -> List[List[complex]]:
        """Return the stroke's geometry as polyline chains (arcs flattened).

        Consecutive line segments merge into one chain; each arc contributes
        its own flattened chain (chains never mix lines and arcs so the
        LineCollection view stays faithful).

        Returns:
            Ordered chains of complex points (inches).
        """
        chains: List[List[complex]] = []
        run: List[complex] = []
        for seg in self.segments:
            if isinstance(seg, LineSeg):
                if not run:
                    run.append(seg.start)
                run.append(seg.end)
            else:
                if len(run) >= 2:
                    chains.append(run)
                run = []
                chains.append(seg.flattened())
        if len(run) >= 2:
            chains.append(run)
        return chains


@dataclass(frozen=True)
class TextBlock:
    """An immutable set of strokes forming one rendered text line.

    All coordinates are inches with ``+y`` up (label-local render frame).
    Bounds are *swept-analytic*: an arc contributes only its swept extent
    (never its full circle), matching how glyph footprints must measure.
    """

    strokes: Tuple[Stroke, ...]

    @classmethod
    def empty(cls) -> TextBlock:
        """Return an empty block."""
        return cls(strokes=())

    def is_empty(self) -> bool:
        """True when the block carries no segments at all."""
        return all(stroke.is_empty for stroke in self.strokes)

    def bounds(self) -> Optional[TextBounds]:
        """Return the union swept bounds, or ``None`` when empty."""
        boxes = [b for b in (stroke.bounds() for stroke in self.strokes) if b is not None]
        if not boxes:
            return None
        return _union_bounds(boxes)

    def translate(self, dx: float, dy: float) -> TextBlock:
        """Return this block shifted by ``(dx, dy)``."""
        return TextBlock(strokes=tuple(s.translate(dx, dy) for s in self.strokes))

    def scaled(self, factor: float) -> TextBlock:
        """Return this block scaled uniformly about the origin."""
        return TextBlock(strokes=tuple(s.scaled(factor) for s in self.strokes))

    def mirrored_y(self, span: float) -> TextBlock:
        """Return this block with Y mirrored across ``y' = span - y``."""
        return TextBlock(strokes=tuple(s.mirrored_y(span) for s in self.strokes))

    def rotated_90cw(self, x_min: float, y_max: float) -> TextBlock:
        """Return this block rotated 90 degrees clockwise about ``(x_min, y_max)``.

        Maps ``(x, y) -> (y_max - y, x - x_min)`` -- the same mapping as
        :func:`plt_optimizer.generate.vectorize.rotate_plt_content_90cw`, so
        content rotated in the inch frame lands identically to content
        rotated as HPGL. A rotation has positive determinant, so arc sweeps
        are preserved verbatim.

        Args:
            x_min: Rotation pivot X (the block's left edge).
            y_max: Rotation pivot Y (the block's top edge).

        Returns:
            The rotated block (its bbox normalizes to start at the origin).
        """

        def _rot(z: complex) -> complex:
            return complex(y_max - z.imag, z.real - x_min)

        strokes: List[Stroke] = []
        for stroke in self.strokes:
            segments: List[TextSegment] = []
            for seg in stroke.segments:
                if isinstance(seg, LineSeg):
                    segments.append(LineSeg(_rot(seg.start), _rot(seg.end)))
                else:
                    segments.append(ArcSeg(_rot(seg.start), _rot(seg.center), seg.sweep_deg))
            strokes.append(Stroke(pen_up=_rot(stroke.pen_up), segments=tuple(segments)))
        return TextBlock(strokes=tuple(strokes))

    def compress_x(self, scale: float, context: str = "") -> TextBlock:
        """Return this block with X uniformly scaled about its left edge.

        Line segments scale exactly. Arcs CANNOT survive a non-uniform
        scale (an ellipse is not a circular arc), so each arc is flattened
        to a polyline at :data:`ARC_FLATTEN_CHORD_TOL_INCHES` and the
        flattened vertices are X-scaled. A WARNING is logged when arcs were
        flattened.

        Args:
            scale: Horizontal scale factor in ``(0, 1]``; ``>= 1`` is a no-op.
            context: Optional description for the WARNING (label/line ids).

        Returns:
            The (possibly flattened + compressed) block.
        """
        if scale >= 1.0:
            return self
        block_bounds = self.bounds()
        if block_bounds is None:  # pragma: no cover - callers check emptiness
            return self
        min_x = block_bounds[0]

        def _sx(x: float) -> float:
            return min_x + (x - min_x) * scale

        flattened_any = False
        strokes: List[Stroke] = []
        for stroke in self.strokes:
            segments: List[TextSegment] = []
            for seg in stroke.segments:
                if isinstance(seg, LineSeg):
                    segments.append(
                        LineSeg(
                            complex(_sx(seg.start.real), seg.start.imag),
                            complex(_sx(seg.end.real), seg.end.imag),
                        )
                    )
                else:
                    flattened_any = True
                    chain = seg.flattened()
                    points = [complex(_sx(p.real), p.imag) for p in chain]
                    for a, b in zip(points[:-1], points[1:]):
                        segments.append(LineSeg(a, b))
            strokes.append(
                Stroke(
                    pen_up=complex(_sx(stroke.pen_up.real), stroke.pen_up.imag),
                    segments=tuple(segments),
                )
            )
        if flattened_any:
            logger.warning(
                "Text arcs flattened to polylines during horizontal compression%s "
                "(chord error <= %gin); compression and native arcs are mutually "
                "exclusive.",
                f" {context}" if context else "",
                ARC_FLATTEN_CHORD_TOL_INCHES,
            )
        return TextBlock(strokes=tuple(strokes))

    def to_hpgl(self) -> str:
        """Emit every stroke as PU/PD/AA commands (semicolon-joined, no tail)."""
        return ";".join(part for s in self.strokes if (part := s.to_hpgl()))

    def to_linecollection(self) -> vp.LineCollection:
        """Return a polyline-only vpype view (arcs flattened, faithful chains)."""
        lc = vp.LineCollection()
        for stroke in self.strokes:
            lc.extend(stroke.to_linecollection())
        return lc

    def to_vpype_polylines(self) -> vp.LineCollection:
        """Return a polyline-only view where line runs merge across arcs.

        The historical vpype representation flattened everything and let
        the pen chain through: a ``line-arc-line`` run appeared as separate
        polylines but adjacent line segments batched together. This helper
        reproduces the exact vertex sequence the old ``LineCollection``
        path produced for polyline-only content (bit-parity for TTF text):
        every stroke contributes its segments' vertices as ONE polyline
        with the pen-up position dropped, and arcs contribute their
        flattened vertices inline.

        Returns:
            A LineCollection with one polyline per stroke (strokes without
            segments skipped).
        """
        lc = vp.LineCollection()
        for stroke in self.strokes:
            if not stroke.segments:
                continue
            points: List[complex] = []
            for seg in stroke.segments:
                if isinstance(seg, LineSeg):
                    if not points:
                        points.append(seg.start)
                    points.append(seg.end)
                else:
                    chain = seg.flattened()
                    if points and chain and chain[0] == points[-1]:
                        chain = chain[1:]
                    points.extend(chain)
            if len(points) >= 2:
                lc.append(np.asarray(points, dtype=complex))
        return lc


def _union_bounds(boxes: Sequence[TextBounds]) -> TextBounds:
    """Return the union of non-empty bounding boxes."""
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def block_from_linecollection(lc: vp.LineCollection) -> TextBlock:
    """Adapt a polyline-only vpype LineCollection into a :class:`TextBlock`.

    Each non-empty polyline becomes one :class:`Stroke` whose ``pen_up`` is
    the first vertex (the historical export treats the leading PU as
    redundant when it equals the first PD point).

    Args:
        lc: Rendered polylines (inches, +y up).

    Returns:
        A polyline-only block; empty when ``lc`` is empty.
    """
    strokes: List[Stroke] = []
    for line in lc:
        arr = np.asarray(line, dtype=complex)
        if len(arr) < 2:
            continue
        points = arr.tolist()
        segments = tuple(LineSeg(complex(a), complex(b)) for a, b in zip(points[:-1], points[1:]))
        strokes.append(Stroke(pen_up=complex(points[0]), segments=segments))
    return TextBlock(strokes=tuple(strokes))


def block_from_parser_paths(paths: Sequence[StrokePath], scale: float = 1.0) -> TextBlock:
    """Convert core-parser stroke paths into an inch-space TextBlock.

    Args:
        paths: Parsed paths in plotter units (e.g. glyph geometry from
            :class:`~plt_optimizer.core.parser.PLTParser`).
        scale: Uniform factor converting path units to inches (``1/1000``
            for plotter units).

    Returns:
        An arc-preserving block in inches. Paths without segments are
        skipped; a path without an explicit pen-up position uses its first
        segment's start.
    """
    strokes: List[Stroke] = []
    for path in paths:
        if not path.segments:
            continue
        segments: List[TextSegment] = []
        for seg in path.segments:
            if isinstance(seg, ArcSegment):
                segments.append(
                    ArcSeg(
                        start=complex(seg.start.x, seg.start.y) * scale,
                        center=complex(seg.center.x, seg.center.y) * scale,
                        sweep_deg=seg.sweep_angle,
                    )
                )
            elif isinstance(seg, StrokeSegment):
                segments.append(
                    LineSeg(
                        start=complex(seg.start.x, seg.start.y) * scale,
                        end=complex(seg.end.x, seg.end.y) * scale,
                    )
                )
        if not segments:
            continue
        if path.pen_up_position is not None:
            pen_up = complex(path.pen_up_position.x, path.pen_up_position.y) * scale
        else:
            pen_up = segments[0].start
        strokes.append(Stroke(pen_up=pen_up, segments=tuple(segments)))
    return TextBlock(strokes=tuple(strokes))
