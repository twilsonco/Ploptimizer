#!/usr/bin/env python3
"""Arc-native HPGL glyph utilities and derived-Unicode glyph generation.

``fonts/extract_plt_fonts.py`` recovers the *engraved* ASCII characters of a
PLT font. This module supplies the two things that extraction cannot produce:

1. **Generic glyph utilities** - :func:`scale_glyph`, :func:`translate_glyph`,
   :func:`rotate_glyph`, :func:`mirror_glyph_y` and :func:`concat_glyphs`
   (superimpose / stack). They take and return the same self-contained HPGL
   command strings stored in the font catalog files
   (``characters[<char>].glyph``), so a derived glyph is a drop-in library
   entry. Internally they parse to :class:`GlyphGeometry` (the core parser's
   :class:`~plt_optimizer.core.models.StrokePath` model kept at full float
   precision - :class:`~plt_optimizer.core.models.Coordinate` rounds to 3
   decimals and would silently truncate the 4-decimal storage format), apply
   an :class:`Affine`, and re-emit.

2. **Derived Unicode glyphs** - :data:`DERIVED_GLYPHS` derives common
   typographic characters that EngraveLab / Vision Pro cannot engrave from
   ``fonts/ascii.txt`` by transforming ASCII bases (``-`` -> en dash, ``.`` ->
   bullet, ``8`` -> infinity, ...). :func:`derive_glyph_entries` turns one
   font's ASCII entries into the matching derived entries, complete with
   ``bounding_box`` and ``left_envelope`` / ``right_envelope``, so the
   typesetter kerns them like any engraved glyph.

Coordinate frame
----------------

Everything operates in the font catalog's **storage frame**: plotter
units (1000 = 1 design inch), ``+y`` up, the text baseline at ``y = 0`` and
each glyph's left edge at ``x = 0``. The cap line is the reference
character's stored ``bounding_box.max_y`` (design-unit 1000 for every v2
font) and the midline is half of it. Derived entries re-establish the
``x = 0`` left edge, so they obey the same contract as engraved ones. A
character listed in ``extract_plt_fonts.VERTICALLY_CENTERED_CHARS`` (``S``)
is midline-centred by the extractor, so its stored box straddles the
baseline on purpose; a derived glyph inherits its base's stored position, and
no recipe bases on ``S``.

Arc (``AA``) rules
------------------

A circular arc survives an affine transform exactly when the transform maps
circles to circles, i.e. it is a similarity: translation, rotation, uniform
scale - and reflection. The stored ``AA`` form (``PD;AA{cx},{cy},{sweep}``)
carries a center and a sweep while the radius stays implicit in the pen
position, so:

- **positive determinant** (translate, rotate, uniform scale): the center
  maps like any point, the radius scales uniformly, and the **sweep is
  verbatim**;
- **negative determinant** (any mirror): as above, and the **sweep is
  negated** - mirroring reverses the cut direction;
- **non-uniform scale** (``scale_x != scale_y``): an ellipse is not a
  circular arc, so :func:`scale_glyph` refuses it
  (:class:`GlyphTransformError`) rather than silently distorting the glyph.
  Line-only glyphs (``-`` -> en dash) are unaffected. The one place a
  non-uniform Y scale IS applied to arc-carrying glyphs is
  :func:`scale_geometry_y` (the extraction-time height-prescription pass),
  which refits each arc's ellipse image with a chain of circular arcs via
  ``plt_optimizer.generate.arc_fit`` - the library-side twin of the render
  pipeline's ``TextBlock.scaled_y(preserve_arcs=True)``.

Envelope sampling
-----------------

A rotated glyph's left/right silhouette comes from the source's *top/bottom*
geometry, so envelopes can never be mapped from the source's stored
envelopes: :func:`derive_glyph_entries` re-samples the transformed geometry.
The band-aggregated sampler used for that (:func:`band_envelopes`) is the very
one ``extract_plt_fonts.py`` uses for engraved glyphs - it lives here so both
paths share one implementation, and the extractor imports it back.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from plt_optimizer.core.models import (
    ArcSegment,
    Coordinate,
    Segment,
    StrokePath,
    StrokeSegment,
)
from plt_optimizer.core.parser import ParseError, PLTParser
from plt_optimizer.generate.arc_fit import fit_arc

logger = logging.getLogger("glyph_transforms")

# Bounding-box type: (x_min, y_min, x_max, y_max) in plotter units.
Bounds = Tuple[float, float, float, float]

# One JSON entry per character, and per font (mirrors extract_plt_fonts.py).
CharacterEntry = Dict[str, Any]

# Numerical slack (plotter units) for the geometric predicates.
_EPS = 1e-9

# Cap height assumed when a font entry carries no usable reference-character
# bounding box (v2 fonts normalize the reference char to exactly 1000 units).
CAP_HEIGHT_FALLBACK: float = 1000.0

# Fraction of the cap height defining the midline ("vertically centered").
MIDLINE_FRACTION: float = 0.5

# En dash / em dash width as a multiple of the hyphen's.
EN_DASH_SCALE: float = 1.8
EM_DASH_SCALE: float = 2.8

# Cent sign: the vertical bar's shortened height as a fraction of its own,
# and the additional uniform downscale applied to the lowercase c.
CENT_BAR_SCALE: float = 0.5
CENT_C_SCALE: float = 0.6

# Not-equal: the forward slash's uniform downscale before superimposing, so
# the slash crosses the bars instead of towering over them, and the equals
# sign's horizontal stretch (about its own centre) widening the bars the
# slash crosses, so the pair reads as one glyph instead of a narrow = with a
# slash hanging off its sides.
NOT_EQUAL_SLASH_SCALE: float = 0.7
NOT_EQUAL_EQUALS_STRETCH: float = 1.4

# Identical-to: bar pitch as a fraction of the cap height. The en dash is a
# zero-height hairline, so the stack pitch cannot be derived from its bbox.
IDENTICAL_PITCH_FRACTION: float = 0.25

# Inverted exclamation mark: descent below the baseline, cap-height fraction.
INVERTED_EXCLAMATION_DESCENT: float = -0.8

# Dagger / double dagger: cross positions (cap-height fractions measured up
# from the baseline) and the uniform downscale applied afterwards.
DAGGER_CROSS_FRACTION: float = 2.0 / 3.0
DOUBLE_DAGGER_CROSS_FRACTIONS: Tuple[float, ...] = (1.0 / 3.0, 2.0 / 3.0)
DAGGER_SCALE: float = 0.4

# Almost-equal: separation divisor for the vertical distance between the two tilde bars.
# Increase value to decrease the separation between the bars.
ALMOST_EQUAL_SEPARATION_DIVISOR: float = 1.0


class GlyphTransformError(Exception):
    """Raised when a glyph cannot be transformed (bad input, arcs vs shear)."""


# ---------------------------------------------------------------------------
# Bounds helpers (shared with fonts/extract_plt_fonts.py)
# ---------------------------------------------------------------------------


def arc_bounds(arc: ArcSegment) -> Bounds:
    """Return the bounding box of the arc's *swept* portion.

    EngraveLab approximates many near-straight glyph strokes as huge-radius
    best-fit arcs, so the arc's full circle dwarfs the actual cut. Measuring
    only the swept extent (the two endpoints plus any cardinal angle the arc
    passes through) keeps a glyph's footprint tight. A full-revolution arc
    still yields its whole circle.

    Args:
        arc: The arc segment to measure.

    Returns:
        ``(x_min, y_min, x_max, y_max)`` in plotter units.
    """
    cx = arc.center.x
    cy = arc.center.y
    radius = arc.radius
    theta_start = math.atan2(arc.start.y - cy, arc.start.x - cx)
    theta_end = theta_start + math.radians(arc.sweep_angle)
    lo, hi = min(theta_start, theta_end), max(theta_start, theta_end)

    quarter = math.pi / 2.0
    angles = [theta_start, theta_end]
    k = math.floor(lo / quarter) - 1
    while True:
        cardinal = k * quarter
        if cardinal > hi:
            break
        if cardinal >= lo:
            angles.append(cardinal)
        k += 1

    xs = [cx + radius * math.cos(a) for a in angles]
    ys = [cy + radius * math.sin(a) for a in angles]
    return (min(xs), min(ys), max(xs), max(ys))


def segment_bounds(segment: Segment) -> Bounds:
    """Return the bounding box of one segment (arcs: swept extent only).

    Args:
        segment: The line or arc segment to measure.

    Returns:
        ``(x_min, y_min, x_max, y_max)`` in plotter units.
    """
    if isinstance(segment, ArcSegment):
        return arc_bounds(segment)
    return (
        min(segment.start.x, segment.end.x),
        min(segment.start.y, segment.end.y),
        max(segment.start.x, segment.end.x),
        max(segment.start.y, segment.end.y),
    )


def path_bounds(path: StrokePath) -> Bounds:
    """Return the bounding box of one stroke path.

    Args:
        path: The path to measure (must contain at least one segment).

    Returns:
        ``(x_min, y_min, x_max, y_max)`` in plotter units.

    Raises:
        ValueError: If the path has no segments.
    """
    if not path.segments:
        raise ValueError("Cannot bound a stroke path without segments")
    return union_bounds([segment_bounds(seg) for seg in path.segments])


def union_bounds(boxes: Sequence[Bounds]) -> Bounds:
    """Return the union bounding box of non-empty boxes.

    Args:
        boxes: Boxes to merge (must be non-empty).

    Returns:
        ``(x_min, y_min, x_max, y_max)`` covering every input box.

    Raises:
        ValueError: If ``boxes`` is empty.
    """
    if not boxes:
        raise ValueError("Cannot union an empty sequence of bounding boxes")
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def round_unit(value: float) -> float:
    """Round one stored coordinate to 6 decimals, normalizing negative zero.

    Args:
        value: Coordinate value.

    Returns:
        The rounded value.
    """
    rounded = round(value, 6)
    return 0.0 if rounded == 0.0 else rounded


def center_x(bounds: Bounds) -> float:
    """Return the X centre of a bounding box."""
    return (bounds[0] + bounds[2]) / 2.0


def center_y(bounds: Bounds) -> float:
    """Return the Y centre of a bounding box."""
    return (bounds[1] + bounds[3]) / 2.0


# ---------------------------------------------------------------------------
# Affine transforms
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Affine:
    """A 2D affine map ``x' = a*x + b*y + tx``, ``y' = c*x + d*y + ty``.

    The general 2x2 form (not just scale/translate) is required because glyph
    derivation rotates: an arbitrary-angle rotation mixes X and Y, and a
    rotation about a pivot carries its own translation.

    Attributes:
        a: X-axis coefficient of x.
        b: X-axis coefficient of y.
        c: Y-axis coefficient of x.
        d: Y-axis coefficient of y.
        tx: X translation.
        ty: Y translation.
    """

    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    tx: float = 0.0
    ty: float = 0.0

    @property
    def determinant(self) -> float:
        """The linear part's determinant (negative for a reflection)."""
        return self.a * self.d - self.b * self.c

    @property
    def sweep_sign(self) -> float:
        """The factor to apply to every ``AA`` sweep angle.

        Returns:
            ``1.0`` for orientation-preserving maps (translate, rotate,
            uniform scale) and ``-1.0`` for reflections, which reverse the
            cut direction of every arc.
        """
        return 1.0 if self.determinant > 0.0 else -1.0

    def apply(self, x: float, y: float) -> Tuple[float, float]:
        """Map one point.

        Args:
            x: X coordinate.
            y: Y coordinate.

        Returns:
            The mapped ``(x, y)`` pair.
        """
        return (self.a * x + self.b * y + self.tx, self.c * x + self.d * y + self.ty)

    def map_bounds(self, bounds: Bounds) -> Bounds:
        """Map a bounding box, transforming all four corners.

        Args:
            bounds: ``(x_min, y_min, x_max, y_max)``.

        Returns:
            The axis-aligned box enclosing the mapped corners (exact for
            rotations by multiples of 90 degrees, conservative otherwise).
        """
        corners = [self.apply(x, y) for x in (bounds[0], bounds[2]) for y in (bounds[1], bounds[3])]
        xs = [point[0] for point in corners]
        ys = [point[1] for point in corners]
        return (min(xs), min(ys), max(xs), max(ys))

    def then(self, other: Affine) -> Affine:
        """Return the composition applying ``self`` first, then ``other``.

        Args:
            other: The transform applied second.

        Returns:
            The composed transform.
        """
        return Affine(
            a=other.a * self.a + other.b * self.c,
            b=other.a * self.b + other.b * self.d,
            c=other.c * self.a + other.d * self.c,
            d=other.c * self.b + other.d * self.d,
            tx=other.a * self.tx + other.b * self.ty + other.tx,
            ty=other.c * self.tx + other.d * self.ty + other.ty,
        )

    @classmethod
    def identity(cls) -> Affine:
        """The identity transform."""
        return cls()

    @classmethod
    def translation(cls, dx: float, dy: float) -> Affine:
        """A pure translation by ``(dx, dy)``."""
        return cls(tx=dx, ty=dy)

    @classmethod
    def scaling(
        cls,
        scale_x: float,
        scale_y: float,
        about: Tuple[float, float] = (0.0, 0.0),
    ) -> Affine:
        """A (possibly non-uniform) scale about ``about``.

        Args:
            scale_x: Horizontal factor.
            scale_y: Vertical factor.
            about: The fixed point of the scale.

        Returns:
            The scaling transform.
        """
        ox, oy = about
        return cls(a=scale_x, d=scale_y, tx=ox * (1.0 - scale_x), ty=oy * (1.0 - scale_y))

    @classmethod
    def rotation(cls, degrees: float, about: Tuple[float, float] = (0.0, 0.0)) -> Affine:
        """A counter-clockwise rotation by ``degrees`` about ``about``.

        Args:
            degrees: Rotation angle, counter-clockwise positive.
            about: The rotation pivot.

        Returns:
            The rotation transform (determinant ``+1``, so arc sweeps stay
            verbatim).
        """
        ox, oy = about
        rad = math.radians(degrees)
        cos_t = math.cos(rad)
        sin_t = math.sin(rad)
        return cls(
            a=cos_t,
            b=-sin_t,
            c=sin_t,
            d=cos_t,
            tx=ox - (cos_t * ox - sin_t * oy),
            ty=oy - (sin_t * ox + cos_t * oy),
        )

    @classmethod
    def mirror_y(cls, span: float) -> Affine:
        """A Y mirror ``y' = span - y`` (determinant ``-1``)."""
        return cls(d=-1.0, ty=span)

    @classmethod
    def mirror_x(cls, span: float) -> Affine:
        """An X mirror ``x' = span - x`` (determinant ``-1``)."""
        return cls(a=-1.0, tx=span)


# ---------------------------------------------------------------------------
# Glyph geometry: parse, transform, emit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GlyphGeometry:
    """One glyph's parsed stroke paths plus their union bounding box.

    Attributes:
        paths: The pen-down-carrying stroke paths (chronological order).
        bounds: Union swept bounds of :attr:`paths`, or ``None`` when the
            glyph carries no geometry.
    """

    paths: Tuple[StrokePath, ...] = ()
    bounds: Optional[Bounds] = None

    @property
    def is_empty(self) -> bool:
        """True when this glyph carries no stroke geometry."""
        return self.bounds is None

    @property
    def has_arcs(self) -> bool:
        """True when any segment is an ``AA`` arc."""
        return any(isinstance(seg, ArcSegment) for path in self.paths for seg in path.segments)


def geometry_from_paths(paths: Sequence[StrokePath]) -> GlyphGeometry:
    """Build a :class:`GlyphGeometry` from parsed stroke paths.

    Args:
        paths: Paths as produced by the core parser.

    Returns:
        The geometry, dropping segment-less paths.
    """
    kept = tuple(path for path in paths if path.segments)
    if not kept:
        return GlyphGeometry()
    return GlyphGeometry(paths=kept, bounds=union_bounds([path_bounds(path) for path in kept]))


def parse_glyph(text: str) -> GlyphGeometry:
    """Parse one stored HPGL glyph string into geometry.

    Args:
        text: HPGL text such as ``"PU0.0000,333.7172;PD444.7686,333.7172;"``.

    Returns:
        The parsed geometry (empty for blank input).

    Raises:
        GlyphTransformError: If the text is not parseable HPGL.
    """
    if not text.strip():
        return GlyphGeometry()
    try:
        document = PLTParser().parse_string(text)
    except ParseError as e:
        raise GlyphTransformError(f"Cannot parse glyph HPGL {text[:60]!r}: {e}") from e
    return geometry_from_paths(document.stroke_paths)


def format_number(value: float) -> str:
    """Format one coordinate with the storage format's 4-decimal precision.

    Args:
        value: Coordinate value in plotter units.

    Returns:
        Fixed-point string, e.g. ``"1234.5678"``, with negative zero
        normalized to ``"0.0000"``.
    """
    text = f"{value:.4f}"
    return "0.0000" if text == "-0.0000" else text


def emit_glyph_geometry(geometry: GlyphGeometry, affine: Optional[Affine] = None) -> str:
    """Emit glyph geometry as a self-contained HPGL string, mapped by ``affine``.

    Coordinates are mapped as they are written, so no intermediate
    :class:`~plt_optimizer.core.models.Coordinate` is built (that type rounds
    to 3 decimals and would truncate the 4-decimal storage format). Segments
    are written tip-to-tail, and a fresh ``PU`` is emitted whenever a segment
    starts away from the current pen position - mirroring how EngraveLab
    precedes each ``PD;AA`` arc with its own ``PU``. Arc sweeps are multiplied
    by :attr:`Affine.sweep_sign`, so a mirror reverses them and a rotation
    keeps them. The result ends with a semicolon and re-parses cleanly.

    Args:
        geometry: The glyph to emit.
        affine: The transform to apply (``None`` = identity).

    Returns:
        HPGL text, or ``""`` for geometry-less input.
    """
    transform = affine if affine is not None else Affine.identity()
    parts: List[str] = []
    for path in geometry.paths:
        if not path.segments:
            continue
        current = (
            path.pen_up_position if path.pen_up_position is not None else path.segments[0].start
        )
        start_x, start_y = transform.apply(current.x, current.y)
        parts.append(f"PU{format_number(start_x)},{format_number(start_y)}")
        for segment in path.segments:
            if not (
                math.isclose(segment.start.x, current.x, abs_tol=1e-3)
                and math.isclose(segment.start.y, current.y, abs_tol=1e-3)
            ):
                break_x, break_y = transform.apply(segment.start.x, segment.start.y)
                parts.append(f"PU{format_number(break_x)},{format_number(break_y)}")
            end_x, end_y = transform.apply(segment.end.x, segment.end.y)
            cmd = "PD" if segment.is_cutting else "PU"
            if isinstance(segment, ArcSegment):
                arc_cx, arc_cy = transform.apply(segment.center.x, segment.center.y)
                parts.append(
                    f"{cmd};AA{format_number(arc_cx)},{format_number(arc_cy)},"
                    f"{format_number(transform.sweep_sign * segment.sweep_angle)}"
                )
            else:
                parts.append(f"{cmd}{format_number(end_x)},{format_number(end_y)}")
            current = segment.end
    if not parts:
        return ""
    return ";".join(parts) + ";"


def _require_similarity(geometry: GlyphGeometry, affine: Affine) -> None:
    """Reject a transform that would turn a glyph's arcs into ellipses.

    Args:
        geometry: The glyph being transformed.
        affine: The proposed transform.

    Raises:
        GlyphTransformError: If the glyph carries arcs and ``affine`` is not a
            similarity (uniform scale composed with rotation/translation),
            the only class of affine mapping circles to circles.
    """
    if not geometry.has_arcs:
        return
    column_x = math.hypot(affine.a, affine.c)
    column_y = math.hypot(affine.b, affine.d)
    if not math.isclose(column_x, column_y, rel_tol=1e-9, abs_tol=1e-9):
        raise GlyphTransformError(
            "Cannot non-uniformly scale a glyph containing AA arcs: an ellipse "
            "is not a circular arc. Use a uniform scale, or flatten the arcs first."
        )


# ---------------------------------------------------------------------------
# Public string -> string utilities
# ---------------------------------------------------------------------------


def transform_glyph(text: str, affine: Affine) -> str:
    """Apply an arbitrary affine to a glyph string.

    Args:
        text: The source HPGL glyph.
        affine: The transform to apply.

    Returns:
        The transformed HPGL glyph (``""`` for empty input).

    Raises:
        GlyphTransformError: If the input is unparseable, or ``affine`` is not
            a similarity while the glyph carries arcs.
    """
    geometry = parse_glyph(text)
    if geometry.is_empty:
        return ""
    _require_similarity(geometry, affine)
    return emit_glyph_geometry(geometry, affine)


def scale_glyph(
    text: str,
    *,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    about: Tuple[float, float] = (0.0, 0.0),
) -> str:
    """Scale a glyph, stretching it horizontally and/or vertically.

    Args:
        text: The source HPGL glyph.
        scale_x: Horizontal factor (``2.0`` doubles the width).
        scale_y: Vertical factor.
        about: The fixed point of the scale (default: the frame origin, so a
            glyph stored with ``min_x = 0`` keeps its left edge anchored).

    Returns:
        The scaled HPGL glyph (``""`` for empty input).

    Raises:
        GlyphTransformError: If a non-uniform scale is applied to a glyph
            carrying ``AA`` arcs (an ellipse is not a circular arc).
    """
    geometry = parse_glyph(text)
    if geometry.is_empty:
        return ""
    affine = Affine.scaling(scale_x, scale_y, about)
    _require_similarity(geometry, affine)
    return emit_glyph_geometry(geometry, affine)


def translate_glyph(text: str, *, dx: float = 0.0, dy: float = 0.0) -> str:
    """Translate a glyph by ``(dx, dy)`` plotter units.

    Args:
        text: The source HPGL glyph.
        dx: Horizontal shift.
        dy: Vertical shift (``+`` is up in the storage frame).

    Returns:
        The translated HPGL glyph (``""`` for empty input).
    """
    geometry = parse_glyph(text)
    if geometry.is_empty:
        return ""
    return emit_glyph_geometry(geometry, Affine.translation(dx, dy))


def rotate_glyph(
    text: str,
    *,
    degrees: float,
    about: Optional[Tuple[float, float]] = None,
) -> str:
    """Rotate a glyph counter-clockwise by ``degrees``.

    Args:
        text: The source HPGL glyph.
        degrees: Rotation angle, counter-clockwise positive.
        about: The pivot; ``None`` rotates about the glyph's bounding-box
            centre, which keeps the glyph in place.

    Returns:
        The rotated HPGL glyph (arc sweeps verbatim - a rotation has
        determinant ``+1``), or ``""`` for empty input.
    """
    geometry = parse_glyph(text)
    if geometry.is_empty or geometry.bounds is None:
        return ""
    pivot = about if about is not None else (center_x(geometry.bounds), center_y(geometry.bounds))
    return emit_glyph_geometry(geometry, Affine.rotation(degrees, pivot))


def mirror_glyph_y(text: str, *, span: float) -> str:
    """Mirror a glyph vertically about ``y = span / 2`` (``y' = span - y``).

    Args:
        text: The source HPGL glyph.
        span: The mirror line's coordinate sum; ``span = 0`` mirrors in the
            baseline.

    Returns:
        The mirrored HPGL glyph, with every arc sweep negated (``""`` for
        empty input).
    """
    geometry = parse_glyph(text)
    if geometry.is_empty:
        return ""
    return emit_glyph_geometry(geometry, Affine.mirror_y(span))


def mirror_glyph_x(text: str, *, span: float) -> str:
    """Mirror a glyph horizontally about ``x = span / 2`` (``x' = span - x``).

    Args:
        text: The source HPGL glyph.
        span: The mirror line's coordinate sum.

    Returns:
        The mirrored HPGL glyph, with every arc sweep negated (``""`` for
        empty input).
    """
    geometry = parse_glyph(text)
    if geometry.is_empty:
        return ""
    return emit_glyph_geometry(geometry, Affine.mirror_x(span))


def concat_glyphs(*texts: str) -> str:
    """Superimpose (or stack, once translated) glyphs into one HPGL string.

    The operands keep their own coordinates, so this is the composition
    primitive: translate each piece first, then concatenate. Every source
    glyph is ``PU``-led, so a superimposed element can never splice a phantom
    cut onto the previous one.

    Args:
        *texts: HPGL glyph strings sharing one frame.

    Returns:
        The combined HPGL glyph (``""`` when every input is empty).
    """
    geometries = [parse_glyph(text) for text in texts if text.strip()]
    kept = [geometry for geometry in geometries if not geometry.is_empty]
    if not kept:
        return ""
    merged = GlyphGeometry(
        paths=tuple(path for geometry in kept for path in geometry.paths),
        bounds=union_bounds([geometry.bounds for geometry in kept if geometry.bounds is not None]),
    )
    return emit_glyph_geometry(merged)


def glyph_bounds(text: str) -> Optional[Bounds]:
    """Return a glyph string's ``(x_min, y_min, x_max, y_max)`` box, or ``None``.

    Args:
        text: The HPGL glyph to measure.

    Returns:
        The swept bounds in plotter units, ``None`` for geometry-less input.
    """
    return parse_glyph(text).bounds


# ---------------------------------------------------------------------------
# Vertical (Y-only) scaling with native arc refitting
# ---------------------------------------------------------------------------

# Design units per design inch in the storage frame (mirrors
# ``extract_plt_fonts.LIBRARY_UNITS_PER_INCH``; duplicated here so this module
# stays importable without the extractor).
_DESIGN_UNITS_PER_INCH: float = 1000.0

# Chord-error bound (inches) for the polyline fallback of an arc the refit
# cannot fit within tolerance. Mirrors ``text_geometry.ARC_FLATTEN_CHORD_TOL_INCHES``
# so a declined arc matches the render pipeline's flattening exactly.
_ARC_FLATTEN_CHORD_TOL_INCHES: float = 0.0002

# Upper bound on the chord count of one flattened arc.
_MAX_ARC_SEGMENTS: int = 4096


def _flatten_arc_units(
    start: Tuple[float, float],
    center: Tuple[float, float],
    sweep_deg: float,
) -> List[Tuple[float, float]]:
    """Flatten one stored-frame arc into tip-to-tail chord points (units).

    Port of ``text_geometry.flatten_arc_to_polyline`` for this module's
    unit-space tuples: the chord count is chosen so each chord's sagitta
    stays within :data:`_ARC_FLATTEN_CHORD_TOL_INCHES` (converted to design
    units), with the same per-segment angular caps as the render pipeline, so
    the fallback geometry is identical on both sides.

    Args:
        start: Arc start ``(x, y)`` in design units.
        center: Arc centre ``(x, y)`` in design units.
        sweep_deg: Signed sweep in degrees.

    Returns:
        Points including ``start`` and the exact arc end.
    """
    radius = math.hypot(start[0] - center[0], start[1] - center[1])
    theta_start = math.atan2(start[1] - center[1], start[0] - center[0])
    theta_end = theta_start + math.radians(sweep_deg)
    end = (center[0] + radius * math.cos(theta_end), center[1] + radius * math.sin(theta_end))
    if radius <= _EPS or abs(sweep_deg) <= 0.0:
        return [start, end]
    tol = _ARC_FLATTEN_CHORD_TOL_INCHES * _DESIGN_UNITS_PER_INCH
    abs_sweep = abs(sweep_deg)
    if tol >= radius:  # pragma: no cover - the fit handles tiny arcs before flatten
        count = max(2, math.ceil(abs_sweep / 5.0))
    else:
        max_half = math.acos(1.0 - tol / radius)
        count = max(1, math.ceil(math.radians(abs_sweep) / (2.0 * max_half)))
    count = max(count, max(2, math.ceil(abs_sweep / 90.0)))
    count = max(1, min(_MAX_ARC_SEGMENTS, count))
    points: List[Tuple[float, float]] = [start]
    for index in range(1, count):
        theta = theta_start + math.radians(sweep_deg) * index / count
        points.append((center[0] + radius * math.cos(theta), center[1] + radius * math.sin(theta)))
    points.append(end)
    return points


def scale_geometry_y(geometry: GlyphGeometry, factor: float, shift: float) -> GlyphGeometry:
    """Apply the stored-frame map ``y' = factor * y + shift`` (X untouched).

    This is the glyph-library counterpart of the render pipeline's
    ``TextBlock.scaled_y(..., preserve_arcs=True)`` + translate, and it uses
    the same :mod:`plt_optimizer.generate.arc_fit` engine: a circular arc does
    not survive a non-uniform Y scale as a circle, so each arc's ellipse image
    (scaled about the baseline ``y = 0``, in inches) is refit with a
    tangent-continuous chain of circular ``AA`` arcs. When the refit cannot
    guarantee the tolerance, the arc falls back to the pipeline's
    chord-bounded polyline flattening. Line segments map exactly.

    Args:
        geometry: The glyph to scale.
        factor: Vertical scale factor (``1.0`` = no stretch).
        shift: Vertical translation (design units) applied *after* scaling.

    Returns:
        The transformed geometry (``bounds`` recomputed from the mapped
        geometry, so it reflects the refit chain, not an idealised ellipse).
    """
    if geometry.is_empty:
        return geometry

    def map_y(y: float) -> float:
        """The full map for coordinates the fit has not scaled yet."""
        return factor * y + shift

    scaled_paths: List[StrokePath] = []
    for path in geometry.paths:
        segments: List[Segment] = []
        for segment in path.segments:
            is_cutting = segment.is_cutting
            start = segment.start
            if isinstance(segment, ArcSegment):
                center = segment.center
                if factor == 1.0:
                    # Pure translation: the circle stays a circle, the sweep
                    # rides verbatim (positive determinant).
                    segments.append(
                        ArcSegment(
                            Coordinate(start.x, start.y + shift),
                            Coordinate(segment.end.x, segment.end.y + shift),
                            Coordinate(center.x, center.y + shift),
                            segment.sweep_angle,
                            is_cutting,
                        )
                    )
                    continue
                radius = math.hypot(start.x - center.x, start.y - center.y)
                sweep = segment.sweep_angle
                chain = None
                if radius > _EPS:
                    # arc_fit works in inches; the design frame is 1000 units
                    # per design inch, and the shift is applied afterwards.
                    chain = fit_arc(
                        complex(center.x, center.y) / _DESIGN_UNITS_PER_INCH,
                        radius / _DESIGN_UNITS_PER_INCH,
                        math.degrees(math.atan2(start.y - center.y, start.x - center.x)),
                        sweep,
                        scale_y=factor,
                    )
                if chain is not None:
                    # The fit already applied the Y scale; translate only.
                    for piece in chain:
                        ps = piece.start * _DESIGN_UNITS_PER_INCH
                        pe = piece.end * _DESIGN_UNITS_PER_INCH
                        if piece.center is None:
                            segments.append(
                                StrokeSegment(
                                    Coordinate(ps.real, ps.imag + shift),
                                    Coordinate(pe.real, pe.imag + shift),
                                    is_cutting,
                                )
                            )
                        else:
                            pc = piece.center * _DESIGN_UNITS_PER_INCH
                            segments.append(
                                ArcSegment(
                                    Coordinate(ps.real, ps.imag + shift),
                                    Coordinate(pe.real, pe.imag + shift),
                                    Coordinate(pc.real, pc.imag + shift),
                                    piece.sweep_deg,
                                    is_cutting,
                                )
                            )
                    continue
                # Refit declined: flatten (units), then apply the full map.
                points = _flatten_arc_units((start.x, start.y), (center.x, center.y), sweep)
                for (x0, y0), (x1, y1) in zip(points[:-1], points[1:]):
                    segments.append(
                        StrokeSegment(
                            Coordinate(x0, map_y(y0)),
                            Coordinate(x1, map_y(y1)),
                            is_cutting,
                        )
                    )
                continue
            end = segment.end
            segments.append(
                StrokeSegment(
                    Coordinate(start.x, map_y(start.y)),
                    Coordinate(end.x, map_y(end.y)),
                    is_cutting,
                )
            )
        if not segments:  # pragma: no cover - parse_glyph drops segment-less paths
            continue
        pen_up = path.pen_up_position
        if pen_up is not None:
            pen_up = Coordinate(pen_up.x, map_y(pen_up.y))
        scaled_paths.append(StrokePath(pen_up_position=pen_up, segments=tuple(segments)))
    if not scaled_paths:  # pragma: no cover - geometry.is_empty is checked by the caller
        return GlyphGeometry()
    return GlyphGeometry(
        paths=tuple(scaled_paths),
        bounds=union_bounds([path_bounds(path) for path in scaled_paths]),
    )


def scale_glyph_y_arc_native(text: str, factor: float, shift: float) -> str:
    """Apply ``y' = factor * y + shift`` to a stored glyph, keeping arcs native.

    Convenience wrapper over :func:`scale_geometry_y` +
    :func:`emit_glyph_geometry` for glyph strings (see that function for the
    arc-refit contract). The identity map (``factor == 1``, ``shift == 0``)
    returns ``text`` unchanged.

    Args:
        text: The source HPGL glyph in the storage frame.
        factor: Vertical scale factor about the baseline ``y = 0``.
        shift: Vertical translation (design units) applied after scaling.

    Returns:
        The transformed HPGL glyph (``""`` for empty input).

    Raises:
        GlyphTransformError: If ``text`` is unparseable.
    """
    geometry = parse_glyph(text)
    if geometry.is_empty:
        return ""
    if factor == 1.0 and shift == 0.0:
        return text
    return emit_glyph_geometry(scale_geometry_y(geometry, factor, shift))


# ---------------------------------------------------------------------------
# Character height prescriptions (extraction-time normalization)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HeightPrescription:
    """One prescribed vertical extent, as percentages of the line height.

    ``line height`` is the cap line minus the baseline, i.e.
    ``LIBRARY_UNITS_PER_INCH`` (1000) design units in the storage frame. A
    prescription maps a glyph's ink box exactly onto
    ``[-descent_percent% , 100% + ascent_percent%]`` of that height, so
    ``ascent_percent=5, descent_percent=5`` forces ``min_y = -50`` and
    ``max_y = 1050``.

    Attributes:
        ascent_percent: Overshoot above the cap line, percent of line height
            (``>= 0``; ``0`` means the ink tops out exactly at the cap line).
        descent_percent: Overshoot below the baseline, percent of line height
            (``>= 0``; ``0`` means the glyph sits on the baseline).
    """

    ascent_percent: float
    descent_percent: float

    def target_bounds(self, line_height: float) -> Tuple[float, float]:
        """Return the ``(min_y, max_y)`` the glyph's ink box is mapped onto.

        Args:
            line_height: The font's line height in design units (cap line -
                baseline; 1000 for every v2 font).

        Returns:
            ``(target_min_y, target_max_y)`` in design units.
        """
        return (
            -self.descent_percent / 100.0 * line_height,
            (1.0 + self.ascent_percent / 100.0) * line_height,
        )


def load_height_prescriptions(path: Path) -> Dict[str, HeightPrescription]:
    """Load the per-character height prescriptions from a JSON file.

    The file maps character groups to their extents::

        {
          "prescriptions": [
            { "characters": "/\\\\", "ascent_percent": 5, "descent_percent": 5 }
          ]
        }

    Validation is strict: every character key must be a single non-whitespace
    codepoint, both percentages must be finite numbers in ``[0, 100]``, and a
    character may appear in only one prescription (last-write-wins would make
    the catalog non-deterministic under reordering).

    Args:
        path: The prescriptions JSON file.

    Returns:
        ``{character: HeightPrescription}`` in file order.

    Raises:
        GlyphTransformError: If the file is unreadable, is not valid JSON of
            the expected shape, or fails validation.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise GlyphTransformError(f"Failed to read prescriptions file {path}: {e}") from e
    except json.JSONDecodeError as e:
        raise GlyphTransformError(f"Prescriptions file {path} is not valid JSON: {e}") from e
    if not isinstance(raw, dict) or not isinstance(raw.get("prescriptions"), list):
        raise GlyphTransformError(
            f"Prescriptions file {path} must be an object with a 'prescriptions' list"
        )
    prescriptions: Dict[str, HeightPrescription] = {}
    for index, item in enumerate(raw["prescriptions"]):
        where = f"prescriptions[{index}]"
        if not isinstance(item, dict):
            raise GlyphTransformError(f"{where} in {path} must be an object")
        characters = item.get("characters")
        if not isinstance(characters, str) or not characters:
            raise GlyphTransformError(f"{where} in {path} needs a non-empty 'characters' string")
        values = []
        for key in ("ascent_percent", "descent_percent"):
            value = item.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise GlyphTransformError(f"{where}.{key} in {path} must be a number")
            number = float(value)
            if not math.isfinite(number) or not 0.0 <= number <= 100.0:
                raise GlyphTransformError(
                    f"{where}.{key} in {path} must lie in [0, 100]; got {value!r}"
                )
            values.append(number)
        for character in characters:
            if character.isspace():
                raise GlyphTransformError(
                    f"{where} in {path} lists a whitespace character; prescriptions key on "
                    "printed glyphs"
                )
            if character in prescriptions:
                raise GlyphTransformError(
                    f"Character {character!r} in {where} of {path} is prescribed more than once"
                )
            prescriptions[character] = HeightPrescription(values[0], values[1])
    return prescriptions


# ---------------------------------------------------------------------------
# Horizontal sampling primitives (shared with fonts/extract_plt_fonts.py)
# ---------------------------------------------------------------------------

# A sampled line and arc, as consumed by :func:`band_envelopes`.
LinePrimitive = Tuple[float, float, float, float]
ArcPrimitive = Tuple[float, float, float, float, float]


def angle_in_sweep(theta: float, theta_start: float, theta_end: float) -> bool:
    """Return whether ``theta`` lies inside the arc's swept angular interval.

    Args:
        theta: Candidate angle, in radians.
        theta_start: Arc start angle, in radians.
        theta_end: Arc end angle, in radians.

    Returns:
        True when ``theta`` is swept by the arc.
    """
    lo, hi = min(theta_start, theta_end), max(theta_start, theta_end)
    full_turn = 2.0 * math.pi
    for shift in (-1.0, 0.0, 1.0):
        shifted = theta + shift * full_turn
        if lo - 1e-9 <= shifted <= hi + 1e-9:
            return True
    return False


def arc_xs_at(
    center_x: float,
    center_y: float,
    radius: float,
    theta_start: float,
    theta_end: float,
    y: float,
) -> List[float]:
    """Return the X values where a horizontal line crosses a swept arc.

    The circle/line intersection is solved analytically (no chord
    flattening), then each candidate is kept only if its angle is inside the
    swept interval.

    Args:
        center_x: Arc center X.
        center_y: Arc center Y.
        radius: Arc radius.
        theta_start: Arc start angle, in radians.
        theta_end: Arc end angle, in radians.
        y: Sample height.

    Returns:
        Zero, one (tangent) or two crossing X values.
    """
    if abs(radius) <= _EPS:
        return []
    delta_y = y - center_y
    if abs(delta_y) > radius + _EPS:
        return []
    discriminant = radius * radius - delta_y * delta_y
    if discriminant < 0.0:
        return []
    delta_x = math.sqrt(discriminant)
    found: List[float] = []
    for sign in (-1.0, 1.0):
        if abs(delta_x) <= _EPS and found:
            break  # tangent: the two candidates coincide
        offset = sign * delta_x
        if angle_in_sweep(math.atan2(delta_y, offset), theta_start, theta_end):
            found.append(center_x + offset)
    return found


def line_band_xs(
    line: Tuple[float, float, float, float],
    y_low: float,
    y_high: float,
) -> List[float]:
    """Return the extreme X values of a segment's portion inside a Y band.

    Args:
        line: ``(x0, y0, x1, y1)``.
        y_low: Lower band edge.
        y_high: Upper band edge.

    Returns:
        The X values bounding the portion of the segment inside the band, or
        both endpoints for a horizontal segment inside the band (its full
        extent is in play). Empty when the segment misses the band entirely.
    """
    x0, y0, x1, y1 = line
    if abs(y1 - y0) <= _EPS:
        return [x0, x1] if y_low - _EPS <= y0 <= y_high + _EPS else []
    lo, hi = min(y0, y1), max(y0, y1)
    low = max(lo, y_low)
    high = min(hi, y_high)
    if low > high + _EPS:
        return []
    scale = (x1 - x0) / (y1 - y0)
    return [x0 + (low - y0) * scale, x0 + (high - y0) * scale]


def arc_band_xs(
    center_x: float,
    center_y: float,
    radius: float,
    theta_start: float,
    theta_end: float,
    y_low: float,
    y_high: float,
) -> List[float]:
    """Return the extreme X values of a swept arc's portion inside a Y band.

    The arc clipped to a horizontal strip breaks into pieces whose X extrema
    can only occur at the piece endpoints (the band-edge crossings and any arc
    endpoint inside the band) or at a swept X cardinal (``0``/``pi``, where
    ``x = center_x +/- radius``). Only the *swept* portion is considered, so
    EngraveLab's huge-radius best-fit arcs never inflate the envelope into
    neighbouring glyphs.

    Args:
        center_x: Arc center X.
        center_y: Arc center Y.
        radius: Arc radius.
        theta_start: Arc start angle, in radians.
        theta_end: Arc end angle, in radians.
        y_low: Lower band edge.
        y_high: Upper band edge.

    Returns:
        The candidate X values, empty when the arc misses the band entirely.
    """
    found: List[float] = []
    for y in (y_low, y_high):
        found.extend(arc_xs_at(center_x, center_y, radius, theta_start, theta_end, y))
    for theta in (theta_start, theta_end):
        y = center_y + radius * math.sin(theta)
        if y_low - _EPS <= y <= y_high + _EPS:
            found.append(center_x + radius * math.cos(theta))
    # The X cardinals (0/pi) sit at y == center_y, so they are in play only when
    # the band contains the center line.
    if y_low - _EPS <= center_y <= y_high + _EPS:
        for cardinal in (0.0, math.pi):
            if angle_in_sweep(cardinal, theta_start, theta_end):
                found.append(center_x + radius * math.cos(cardinal))
    return found


def interpolate_gaps(samples: Sequence[Optional[float]]) -> List[float]:
    """Fill undefined envelope samples by linear interpolation.

    A band that finds no geometry at all (possible inside a bounding-box hole,
    e.g. the open middle of a ``C``) takes the value interpolated between the
    nearest defined samples on either side; when only one side is defined, the
    value is clamped to it.

    Args:
        samples: Per-height envelope X values, ``None`` where undefined.

    Returns:
        One value per input sample, with gaps filled.

    Raises:
        ValueError: If every sample is undefined.
    """
    defined = [index for index, value in enumerate(samples) if value is not None]
    if not defined:
        raise ValueError("Cannot interpolate an envelope with no defined samples")
    filled: List[float] = []
    for index, value in enumerate(samples):
        if value is not None:
            filled.append(value)
            continue
        below = max((i for i in defined if i < index), default=None)
        above = min((i for i in defined if i > index), default=None)
        if below is not None and above is not None:
            low_value = samples[below] or 0.0
            high_value = samples[above] or 0.0
            weight = (index - below) / (above - below)
            filled.append(low_value + (high_value - low_value) * weight)
        else:
            nearest = below if below is not None else above
            assert nearest is not None  # guaranteed: `defined` is non-empty
            filled.append(samples[nearest] or 0.0)
    return filled


def band_envelopes(
    lines: Sequence[LinePrimitive],
    arcs: Sequence[ArcPrimitive],
    bounds: Bounds,
    samples: int,
) -> Tuple[List[List[float]], List[List[float]]]:
    """Extract left/right profile envelopes by aggregating uniform Y bands.

    The vertical extent is divided into ``samples`` uniform *bands* centred on
    a uniform height grid, and each sample records the extreme X of *any*
    stroke geometry meeting its band ``[y_k - step/2, y_k + step/2]`` (clamped
    to the bounding box): the left envelope the smallest X, the right envelope
    the largest. Banding is what makes the profile robust: EngraveLab engraves
    horizontal bars as single zero-width strokes, and a zero-thickness sample
    line is *guaranteed* to miss such a bar whenever its height is not an
    exact multiple of the sample step (the Dino ``F`` middle bar sits at
    y = 500.2923 while a 30-band grid steps by 34.482759). Bands tile the axis,
    so every point of every stroke falls in at least one band and no hairline
    can slip through. Extremes are computed analytically from the band-clipped
    geometry (:func:`line_band_xs`, :func:`arc_band_xs` - swept arc extents
    only), and the sampled silhouette can only be *wider* than the exact
    same-height silhouette, never narrower.

    A band that finds no geometry (possible only inside a bounding-box hole) is
    interpolated from its neighbours (see :func:`interpolate_gaps`).

    Args:
        lines: ``(x0, y0, x1, y1)`` line primitives.
        arcs: ``(center_x, center_y, radius, theta_start, theta_end)`` arcs.
        bounds: The sampled geometry's bounding box.
        samples: Number of sample heights (``>= 2``).

    Returns:
        ``(left_envelope, right_envelope)``, each a list of ``[x, y]`` pairs
        (6-decimal rounded), or ``([], [])`` when there is no geometry.

    Raises:
        ValueError: If ``samples`` is less than 2.
    """
    if samples < 2:
        raise ValueError("Envelope sampling requires at least 2 samples")
    if not lines and not arcs:
        return [], []

    low_y, high_y = bounds[1], bounds[3]
    span = high_y - low_y
    if span <= _EPS:
        heights = [low_y] * samples
        half_step = 0.0
    else:
        step = span / (samples - 1)
        heights = [low_y + index * step for index in range(samples)]
        heights[-1] = high_y
        half_step = step / 2.0

    left_gaps: List[Optional[float]] = []
    right_gaps: List[Optional[float]] = []
    for y in heights:
        band_low = max(low_y, y - half_step)
        band_high = min(high_y, y + half_step)
        crossings: List[float] = []
        for line in lines:
            crossings.extend(line_band_xs(line, band_low, band_high))
        for arc_cx, arc_cy, radius, theta_start, theta_end in arcs:
            crossings.extend(
                arc_band_xs(arc_cx, arc_cy, radius, theta_start, theta_end, band_low, band_high)
            )
        left_gaps.append(min(crossings) if crossings else None)
        right_gaps.append(max(crossings) if crossings else None)

    left = interpolate_gaps(left_gaps)
    right = interpolate_gaps(right_gaps)
    left_envelope = [[round_unit(x), round_unit(y)] for x, y in zip(left, heights)]
    right_envelope = [[round_unit(x), round_unit(y)] for x, y in zip(right, heights)]
    return left_envelope, right_envelope


def normalized_geometry(
    geometry: GlyphGeometry,
    affine: Optional[Affine] = None,
) -> Tuple[List[LinePrimitive], List[ArcPrimitive]]:
    """Map a glyph into float primitives usable for horizontal sampling.

    Args:
        geometry: The glyph to map.
        affine: The transform to apply (``None`` = identity).

    Returns:
        ``(lines, arcs)`` where lines are ``(x0, y0, x1, y1)`` and arcs are
        ``(center_x, center_y, radius, theta_start, theta_end)``. Radii are
        derived from the mapped start/centre (so rotation is exact) and the
        sweep interval uses :attr:`Affine.sweep_sign`, exactly as a consumer
        parsing the emitted glyph would.
    """
    transform = affine if affine is not None else Affine.identity()
    lines: List[LinePrimitive] = []
    arcs: List[ArcPrimitive] = []
    sign = transform.sweep_sign
    for path in geometry.paths:
        for segment in path.segments:
            start_x, start_y = transform.apply(segment.start.x, segment.start.y)
            end_x, end_y = transform.apply(segment.end.x, segment.end.y)
            if isinstance(segment, ArcSegment):
                arc_cx, arc_cy = transform.apply(segment.center.x, segment.center.y)
                radius = math.hypot(start_x - arc_cx, start_y - arc_cy)
                theta_start = math.atan2(start_y - arc_cy, start_x - arc_cx)
                theta_end = theta_start + math.radians(sign * segment.sweep_angle)
                arcs.append((arc_cx, arc_cy, radius, theta_start, theta_end))
            else:
                lines.append((start_x, start_y, end_x, end_y))
    return lines, arcs


# ---------------------------------------------------------------------------
# Derived-glyph composition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GlyphElement:
    """One transformed source glyph inside a composed (derived) glyph.

    Attributes:
        geometry: The parsed source glyph.
        affine: The transform placing it inside the derived glyph.
    """

    geometry: GlyphGeometry
    affine: Affine = Affine.identity()

    @property
    def bounds(self) -> Bounds:
        """The element's placed bounding box.

        Raises:
            GlyphTransformError: If the element carries no geometry.
        """
        if self.geometry.bounds is None:
            raise GlyphTransformError("Cannot place a glyph element without geometry")
        return self.affine.map_bounds(self.geometry.bounds)


def elements_bounds(elements: Sequence[GlyphElement]) -> Bounds:
    """Return the union bounding box of placed elements.

    Args:
        elements: The composed glyph's elements (must be non-empty).

    Returns:
        The union ``(x_min, y_min, x_max, y_max)``.

    Raises:
        GlyphTransformError: If ``elements`` is empty.
    """
    if not elements:
        raise GlyphTransformError("Cannot bound a glyph without elements")
    return union_bounds([element.bounds for element in elements])


def emit_elements(elements: Sequence[GlyphElement]) -> str:
    """Emit composed elements as one HPGL string.

    Args:
        elements: The composed glyph's elements.

    Returns:
        The concatenated HPGL text; every element is ``PU``-led, so the result
        re-parses cleanly and never joins elements with a phantom cut.
    """
    return "".join(emit_glyph_geometry(element.geometry, element.affine) for element in elements)


def elements_primitives(
    elements: Sequence[GlyphElement],
) -> Tuple[List[LinePrimitive], List[ArcPrimitive]]:
    """Return the sampling primitives of every placed element, concatenated."""
    lines: List[LinePrimitive] = []
    arcs: List[ArcPrimitive] = []
    for element in elements:
        part_lines, part_arcs = normalized_geometry(element.geometry, element.affine)
        lines.extend(part_lines)
        arcs.extend(part_arcs)
    return lines, arcs


def build_glyph_entry(
    elements: Sequence[GlyphElement],
    *,
    envelope_samples: int,
) -> CharacterEntry:
    """Build a complete font-catalog character entry from elements.

    The bounding box, both envelopes and the HPGL string all come from the
    *transformed* geometry, so the entry is indistinguishable from an engraved
    one and the typesetter kerns it from its real silhouette.

    Args:
        elements: The composed glyph's placed elements (must be non-empty).
        envelope_samples: Envelope sample count per profile.

    Returns:
        A ``{"bounding_box", "left_envelope", "right_envelope", "glyph"}``
        mapping in the storage frame.

    Raises:
        GlyphTransformError: If the composition is empty.
    """
    bounds = elements_bounds(elements)
    lines, arcs = elements_primitives(elements)
    left_envelope, right_envelope = band_envelopes(lines, arcs, bounds, envelope_samples)
    return {
        "bounding_box": {
            "min_x": round_unit(bounds[0]),
            "max_x": round_unit(bounds[2]),
            "min_y": round_unit(bounds[1]),
            "max_y": round_unit(bounds[3]),
        },
        "left_envelope": left_envelope,
        "right_envelope": right_envelope,
        "glyph": emit_elements(elements),
    }


@dataclass(frozen=True)
class DerivedContext:
    """The inputs one recipe sees: parsed bases plus the font's metrics.

    Attributes:
        bases: Parsed source glyphs, keyed by the ASCII character.
        cap: The cap height (the reference character's stored ``max_y``).
        midline: The vertical centre to align math symbols on.
    """

    bases: Mapping[str, GlyphGeometry]
    cap: float
    midline: float

    def g(self, char: str) -> GlyphGeometry:
        """Return one parsed base glyph.

        Args:
            char: The ASCII base character.

        Returns:
            Its geometry.

        Raises:
            GlyphTransformError: If the recipe reads a base it did not declare
                (a recipe-table bug, not a data problem).
        """
        geometry = self.bases.get(char)
        if geometry is None:
            raise GlyphTransformError(f"Recipe did not declare base {char!r}")
        return geometry

    def b(self, char: str) -> Bounds:
        """Return one parsed base glyph's bounding box.

        Raises:
            GlyphTransformError: If the base is undeclared or geometry-less.
        """
        bounds = self.g(char).bounds
        if bounds is None:
            raise GlyphTransformError(f"Base {char!r} carries no geometry")
        return bounds


# A recipe maps its declared bases to the placed elements of one glyph.
RecipeBuilder = Callable[[DerivedContext], Sequence[GlyphElement]]


@dataclass(frozen=True)
class GlyphRecipe:
    """One derived character: its key, required bases, and construction.

    Attributes:
        character: The Unicode character produced (the JSON key).
        bases: ASCII base characters the recipe reads; every one must exist in
            the font, otherwise the glyph is skipped.
        builder: Builds the placed elements from the parsed bases + metrics.
        description: Human-readable construction, for logs and docs.
    """

    character: str
    bases: Tuple[str, ...]
    builder: RecipeBuilder
    description: str


def _reanchor(elements: Sequence[GlyphElement]) -> List[GlyphElement]:
    """Shift elements so the composition's left edge sits at ``x = 0``.

    Every stored glyph starts at ``x = 0``; derived glyphs obey the same
    contract so the typesetter's kerning and advance math behaves identically.

    Args:
        elements: The composed elements.

    Returns:
        New elements whose union bounding box has ``min_x == 0``.
    """
    shift = Affine.translation(-elements_bounds(elements)[0], 0.0)
    return [GlyphElement(element.geometry, element.affine.then(shift)) for element in elements]


def _lift_to_midline(elements: Sequence[GlyphElement], midline: float) -> List[GlyphElement]:
    """Shift elements so the composition's vertical centre lands on ``midline``."""
    lift = Affine.translation(0.0, midline - center_y(elements_bounds(elements)))
    return [GlyphElement(element.geometry, element.affine.then(lift)) for element in elements]


def _extreme_primitives(
    elements: Sequence[GlyphElement],
    axis: str,
    maximum: bool,
) -> Tuple[List[LinePrimitive], List[ArcPrimitive]]:
    """Return the placed primitives touching a composition's axis extreme.

    Args:
        elements: The composed glyph's elements.
        axis: ``"x"`` or ``"y"`` - the axis to measure.
        maximum: ``True`` for the maximal extreme (right / top), ``False``
            for the minimal one (left / bottom).

    Returns:
        ``(lines, arcs)`` of every primitive carrying at least one point at
        the union's extreme along ``axis`` (within :data:`_EPS`).
    """
    bounds = elements_bounds(elements)
    index = (2 if maximum else 0) if axis == "x" else (3 if maximum else 1)
    target = bounds[index]
    lines: List[LinePrimitive] = []
    arcs: List[ArcPrimitive] = []
    for element in elements:
        element_lines, element_arcs = normalized_geometry(element.geometry, element.affine)
        if axis == "x":
            lines.extend(
                line
                for line in element_lines
                if min(line[0], line[2]) <= target + _EPS and max(line[0], line[2]) >= target - _EPS
            )
            arcs.extend(
                arc
                for arc in element_arcs
                if arc[0] - arc[2] <= target + _EPS and arc[0] + arc[2] >= target - _EPS
            )
        else:
            lines.extend(
                line
                for line in element_lines
                if min(line[1], line[3]) <= target + _EPS and max(line[1], line[3]) >= target - _EPS
            )
            arcs.extend(
                arc
                for arc in element_arcs
                if arc[1] - arc[2] <= target + _EPS and arc[1] + arc[2] >= target - _EPS
            )
    return lines, arcs


def _align_to_tip(
    elements: Sequence[GlyphElement],
    head: GlyphElement,
    axis: str,
    maximum: bool,
) -> List[GlyphElement]:
    """Place ``head`` so its tip shares the composition's extreme on ``axis``.

    The tip is the point of the *rest* of the composition that reaches the
    union's maximal/minimal extent along the axis (a shaft's end, a stem's
    tip), and the head is translated along the axis so its own extreme meets
    that point. The crossing coordinate is measured from the tip primitives
    (the strokes carrying the extreme), so the head lands exactly on the
    material that ends there: the caret's base centre lands on a shaft's end
    (at mid-height for the horizontal arrows, on the stem's tip for the
    vertical ones).
    Perpendicular placement centres the head on the tip's crossing extent.

    Args:
        elements: The composition *without* the head.
        head: The element to place (the arrows' caret, ...).
        axis: ``"x"`` for left/right arrows, ``"y"`` for up/down arrows.
        maximum: ``True`` to align on the maximal extreme (right / top).

    Returns:
        ``elements`` plus the placed head.
    """
    tip_lines, tip_arcs = _extreme_primitives(elements, axis, maximum)
    crossing: List[float] = []
    for x0, y0, x1, y1 in tip_lines:
        crossing.extend([x0, x1] if axis == "y" else [y0, y1])
    for cx, cy, radius, theta_start, theta_end in tip_arcs:
        if axis == "y":
            if angle_in_sweep(math.pi / 2.0, theta_start, theta_end):
                crossing.append(cy + radius)
            if angle_in_sweep(-math.pi / 2.0, theta_start, theta_end):
                crossing.append(cy - radius)
        else:
            if angle_in_sweep(0.0, theta_start, theta_end):
                crossing.append(cx + radius)
            if angle_in_sweep(math.pi, theta_start, theta_end):
                crossing.append(cx - radius)
    if not crossing:  # defensive: an extreme always carries geometry
        crossing = [center_x(elements_bounds(elements))]

    bounds = elements_bounds(elements)
    head_bounds = head.bounds
    crossing_centre = (max(crossing) + min(crossing)) / 2.0
    # The head's tip edge lands on the union's extreme; perpendicular to it,
    # the head centres on the crossing extent of the tip primitives.
    if axis == "y":
        along = bounds[3] if maximum else bounds[1]
        head_along = head_bounds[3] if maximum else head_bounds[1]
        shift = Affine.translation(crossing_centre - center_x(head_bounds), along - head_along)
    else:
        along = bounds[2] if maximum else bounds[0]
        head_along = head_bounds[2] if maximum else head_bounds[0]
        shift = Affine.translation(along - head_along, crossing_centre - center_y(head_bounds))
    placed = GlyphElement(head.geometry, head.affine.then(shift))
    return [*elements, placed]


def _stretched_hyphen(geometry: GlyphGeometry, factor: float) -> GlyphElement:
    """Return the hyphen stretched to ``factor`` x its width (the dash family)."""
    affine = Affine.scaling(factor, 1.0)
    _require_similarity(geometry, affine)
    return GlyphElement(geometry, affine)


def _en_dash(context: DerivedContext) -> Sequence[GlyphElement]:
    """En dash: the hyphen stretched to double its width, height untouched."""
    return [_stretched_hyphen(context.g("-"), EN_DASH_SCALE)]


def _em_dash(context: DerivedContext) -> Sequence[GlyphElement]:
    """Em dash: the hyphen stretched to triple its width, height untouched."""
    return [_stretched_hyphen(context.g("-"), EM_DASH_SCALE)]


def _bullet(context: DerivedContext) -> Sequence[GlyphElement]:
    """Bullet: the period raised so its centre sits on the midline."""
    dy = context.midline - center_y(context.b("."))
    return [GlyphElement(context.g("."), Affine.translation(0.0, dy))]


def _infinity(context: DerivedContext) -> Sequence[GlyphElement]:
    """Infinity: a numeral eight turned 90 degrees, centred on the midline."""
    bounds = context.b("8")
    rotate = Affine.rotation(90.0, (center_x(bounds), center_y(bounds)))
    rotated = rotate.map_bounds(bounds)
    shift = Affine.translation(-rotated[0], context.midline - center_y(rotated))
    return [GlyphElement(context.g("8"), rotate.then(shift))]


def _plus_minus(context: DerivedContext) -> Sequence[GlyphElement]:
    """Plus-minus: a plus touching an underscore, the pair centred on midline."""
    underscore = context.b("_")
    plus = context.b("+")
    place = Affine.translation(
        center_x(underscore) - center_x(plus),
        underscore[3] - plus[1],  # the plus's bottom edge touches the underscore
    )
    elements = [GlyphElement(context.g("_")), GlyphElement(context.g("+"), place)]
    return _lift_to_midline(elements, context.midline)


def _cent(context: DerivedContext) -> Sequence[GlyphElement]:
    """Cent sign: a shortened bar centred through a shrunken lowercase c.

    The bar is halved about its *own* centre (which that scale leaves fixed)
    and then translated so its centre lands exactly on the shrunken ``c``'s
    centre: scaling about the ``c``'s centre instead would pin the pivot, not
    the bar's centre, and leave the two vertically misaligned by half their
    original centre gap.
    """
    c_geometry = context.g("c")
    c_bounds = context.b("c")
    shrink_c = Affine.scaling(CENT_C_SCALE, CENT_C_SCALE, (center_x(c_bounds), center_y(c_bounds)))
    shrunken_c = GlyphElement(c_geometry, shrink_c)
    bar = context.g("|")
    bar_bounds = context.b("|")
    c_centre = (center_x(shrunken_c.bounds), center_y(shrunken_c.bounds))
    bar_centre = (center_x(bar_bounds), center_y(bar_bounds))
    shrink = Affine.scaling(1.0, CENT_BAR_SCALE, bar_centre)
    _require_similarity(bar, shrink)
    place = shrink.then(
        Affine.translation(c_centre[0] - bar_centre[0], c_centre[1] - bar_centre[1])
    )
    elements = [shrunken_c, GlyphElement(bar, place)]
    return _lift_to_midline(elements, context.midline)


def _not_equal(context: DerivedContext) -> Sequence[GlyphElement]:
    """Not equal: a downscaled slash centred on a horizontally stretched equals.

    The slash is shrunk uniformly so it crosses the bars instead of towering
    over them, and the equals sign is stretched horizontally about its own
    centre (which that scale leaves fixed) so the bars extend past the slash's
    crossing points. Both X- and Y-centres are then shared before
    superimposing. The stretch is non-uniform, so the equality bars must be
    line-only (true for the shipped fonts) - :func:`_require_similarity`
    guards the arc case.
    """
    slash = context.g("/")
    slash_bounds = context.b("/")
    shrink = Affine.scaling(
        NOT_EQUAL_SLASH_SCALE,
        NOT_EQUAL_SLASH_SCALE,
        (center_x(slash_bounds), center_y(slash_bounds)),
    )
    shrunken = GlyphElement(slash, shrink)
    shrunken_bounds = shrunken.bounds
    equals = context.g("=")
    equals_bounds = context.b("=")
    stretch = Affine.scaling(
        NOT_EQUAL_EQUALS_STRETCH,
        1.0,
        (center_x(equals_bounds), center_y(equals_bounds)),
    )
    _require_similarity(equals, stretch)
    stretched = GlyphElement(equals, stretch)
    stretched_bounds = stretched.bounds
    place = Affine.translation(
        center_x(stretched_bounds) - center_x(shrunken_bounds),
        center_y(stretched_bounds) - center_y(shrunken_bounds),
    )
    return [stretched, GlyphElement(slash, shrink.then(place))]


def _almost_equal(context: DerivedContext) -> Sequence[GlyphElement]:
    """Almost equal: two tildas stacked, the pair centred on the midline."""
    tilde = context.b("~")
    height = tilde[3] - tilde[1]
    separation = height / ALMOST_EQUAL_SEPARATION_DIVISOR
    return [
        GlyphElement(context.g("~"), Affine.translation(0.0, target - center_y(tilde)))
        for target in (context.midline + separation, context.midline - separation)
    ]


def _identical(context: DerivedContext) -> Sequence[GlyphElement]:
    """Identical to: three en dashes stacked, the set centred on the midline.

    The en dash is a zero-height hairline, so the stack pitch is a fixed
    fraction of the cap height rather than something derived from its bbox.
    """
    hyphen = context.g("-")
    bounds = context.b("-")
    bar_centre_y = bounds[1] + (bounds[3] - bounds[1]) / 2.0
    pitch = IDENTICAL_PITCH_FRACTION * context.cap
    elements: List[GlyphElement] = []
    for offset in (-pitch, 0.0, pitch):
        element = _stretched_hyphen(hyphen, EN_DASH_SCALE)
        shift = Affine.translation(0.0, context.midline + offset - bar_centre_y)
        elements.append(GlyphElement(element.geometry, element.affine.then(shift)))
    return elements


def _inverted_question(context: DerivedContext) -> Sequence[GlyphElement]:
    """Inverted question mark: a question mark rotated 180 degrees."""
    bounds = context.b("?")
    rotate = Affine.rotation(180.0, (center_x(bounds), center_y(bounds)))
    rotated = rotate.map_bounds(bounds)
    shift = Affine.translation(-rotated[0], -rotated[1])
    return [GlyphElement(context.g("?"), rotate.then(shift))]


def _inverted_exclamation(context: DerivedContext) -> Sequence[GlyphElement]:
    """Inverted exclamation mark: ``!`` mirrored in the baseline, descended."""
    drop = -INVERTED_EXCLAMATION_DESCENT * context.cap
    affine = Affine.mirror_y(0.0).then(Affine.translation(0.0, drop))
    return [GlyphElement(context.g("!"), affine)]


def _dagger(context: DerivedContext, cross_fractions: Sequence[float]) -> Sequence[GlyphElement]:
    """Dagger family: cross(es) on a stem, halved, top raised to the cap line."""
    stem = context.b("|")
    hyphen = context.g("-")
    hyphen_bounds = context.b("-")
    elements: List[GlyphElement] = [GlyphElement(context.g("|"))]
    for fraction in cross_fractions:
        place = Affine.translation(
            center_x(stem) - center_x(hyphen_bounds),
            fraction * context.cap - center_y(hyphen_bounds),
        )
        elements.append(GlyphElement(hyphen, place))
    union = elements_bounds(elements)
    shrink = Affine.scaling(DAGGER_SCALE, DAGGER_SCALE, (center_x(union), center_y(union)))
    shrunk = [GlyphElement(element.geometry, element.affine.then(shrink)) for element in elements]
    lift = Affine.translation(0.0, context.cap - elements_bounds(shrunk)[3])
    return [GlyphElement(element.geometry, element.affine.then(lift)) for element in shrunk]


def _dagger_single(context: DerivedContext) -> Sequence[GlyphElement]:
    """Dagger: one cross two-thirds of the way up to the cap line."""
    return _dagger(context, (DAGGER_CROSS_FRACTION,))


def _dagger_double(context: DerivedContext) -> Sequence[GlyphElement]:
    """Double dagger: crosses at one-third and two-thirds of the cap height."""
    return _dagger(context, DOUBLE_DAGGER_CROSS_FRACTIONS)


def _arrow_shaft(context: DerivedContext, *, vertical: bool) -> GlyphElement:
    """Return the arrows' shaft: the hyphen stretched to exactly the cap height.

    The hyphen is stretched horizontally to ``cap`` units (the capline minus
    the baseline, so every arrow measures exactly one cap height) and then:

    - ``vertical=True``: rotated 90 degrees counter-clockwise, so the shaft
      spans the baseline to the cap line (the up/down arrows' stem); the
      font's own ``|`` is deliberately unused - it overshoots both lines
      (e.g. -143..1143), which pushed the derived arrows outside the cap;
    - ``vertical=False``: kept horizontal and centred on the midline (the
      left/right arrows' shaft).

    Args:
        context: The recipe context carrying the ``-`` base and the cap.
        vertical: Whether the shaft is the up/down stem (rotated) or the
            left/right shaft (horizontal).

    Returns:
        The placed shaft element.

    Raises:
        GlyphTransformError: If the hyphen is degenerate (zero width) or
            carries arcs (a non-uniform stretch turns arcs into ellipses).
    """
    geometry = context.g("-")
    bounds = context.b("-")
    width = bounds[2] - bounds[0]
    if width <= 0.0:
        raise GlyphTransformError("Arrow shaft hyphen carries no width")
    stretch = Affine.scaling(context.cap / width, 1.0)
    _require_similarity(geometry, stretch)
    if vertical:
        return GlyphElement(geometry, stretch.then(Affine.rotation(90.0)))
    stretched = stretch.map_bounds(bounds)
    shift = Affine.translation(0.0, context.midline - center_y(stretched))
    return GlyphElement(geometry, stretch.then(shift))


def _arrow_head(context: DerivedContext, rotate_deg: float) -> GlyphElement:
    """Return the arrows' head: the caret, rotated to point along the axis.

    The caret's apex is the arrow's tip. A rotation about the caret's own
    centre is a similarity (determinant ``+1``), so a font whose caret carries
    ``AA`` arcs keeps them native - the chevron bases ``<``/``>`` are
    deliberately unused, the caret is the single arrowhead for all four
    directions.

    Args:
        context: The recipe context carrying the ``^`` base.
        rotate_deg: Counter-clockwise rotation (0 up, -90 right, 90 left;
            down is a mirror, handled by the caller).

    Returns:
        The placed head element (unrotated for ``rotate_deg == 0``).
    """
    geometry = context.g("^")
    if rotate_deg == 0.0:
        return GlyphElement(geometry)
    bounds = context.b("^")
    pivot = (center_x(bounds), center_y(bounds))
    return GlyphElement(geometry, Affine.rotation(rotate_deg, pivot))


def _arrow_up(context: DerivedContext) -> Sequence[GlyphElement]:
    """Up arrow: a caret whose apex meets the cap-length stem's top tip."""
    stem = _arrow_shaft(context, vertical=True)
    return _align_to_tip([stem], _arrow_head(context, 0.0), "y", maximum=True)


def _arrow_down(context: DerivedContext) -> Sequence[GlyphElement]:
    """Down arrow: an inverted caret whose apex meets the stem's foot."""
    stem = _arrow_shaft(context, vertical=True)
    caret = context.b("^")
    flip = Affine.mirror_y(caret[1] + caret[3])
    head = GlyphElement(context.g("^"), flip)
    return _align_to_tip([stem], head, "y", maximum=False)


def _arrow(
    context: DerivedContext, rotate_deg: float, head_on_left: bool
) -> Sequence[GlyphElement]:
    """Left/right arrow: a rotated caret whose apex shares the shaft's end.

    The caret's apex lands exactly on the shaft's terminal point (the union's
    maximal/minimal X, at the shaft's midline), so head and body share one
    extreme and one point - no disconnected arrowhead.
    """
    body = _arrow_shaft(context, vertical=False)
    head = _arrow_head(context, rotate_deg)
    return _align_to_tip([body], head, "x", maximum=not head_on_left)


def _arrow_left(context: DerivedContext) -> Sequence[GlyphElement]:
    """Left arrow: a caret pointing left on the cap-length shaft's left end."""
    return _arrow(context, 90.0, head_on_left=True)


def _arrow_right(context: DerivedContext) -> Sequence[GlyphElement]:
    """Right arrow: a caret pointing right on the cap-length shaft's right end."""
    return _arrow(context, -90.0, head_on_left=False)


DERIVED_GLYPHS: Tuple[GlyphRecipe, ...] = (
    GlyphRecipe("\u2013", ("-",), _en_dash, "hyphen stretched to EN_DASH_SCALE x width"),
    GlyphRecipe("\u2014", ("-",), _em_dash, "hyphen stretched to EM_DASH_SCALE x width"),
    GlyphRecipe("\u2022", (".",), _bullet, "period raised to the midline"),
    GlyphRecipe("\u221e", ("8",), _infinity, "numeral eight rotated 90 degrees"),
    GlyphRecipe("\u00b1", ("+", "_"), _plus_minus, "plus touching an underscore, centred"),
    GlyphRecipe(
        "\u00a2",
        ("c", "|"),
        _cent,
        "shortened bar centred through a shrunken lowercase c",
    ),
    GlyphRecipe(
        "\u2260",
        ("=", "/"),
        _not_equal,
        "shrunk slash centred on a horizontally stretched equals sign",
    ),
    GlyphRecipe("\u2248", ("~",), _almost_equal, "two tildas stacked, centred"),
    GlyphRecipe("\u2261", ("-",), _identical, "three en dashes stacked, centred"),
    GlyphRecipe("\u00bf", ("?",), _inverted_question, "question mark rotated 180 degrees"),
    GlyphRecipe(
        "\u00a1",
        ("!",),
        _inverted_exclamation,
        "exclamation mark mirrored in the baseline, descended",
    ),
    GlyphRecipe(
        "\u2020", ("-", "|"), _dagger_single, "cross on a stem, downscaled, top at cap line"
    ),
    GlyphRecipe(
        "\u2021",
        ("-", "|"),
        _dagger_double,
        "two crosses on a stem, downscaled, top at cap line",
    ),
    GlyphRecipe(
        "\u2191",
        ("-", "^"),
        _arrow_up,
        "caret apex on the cap-height stem's top tip (shared top extreme)",
    ),
    GlyphRecipe(
        "\u2193",
        ("-", "^"),
        _arrow_down,
        "inverted caret apex on the stem's foot (shared bottom extreme)",
    ),
    GlyphRecipe(
        "\u2190",
        ("-", "^"),
        _arrow_left,
        "left-pointing caret apex on the left end of a cap-height shaft",
    ),
    GlyphRecipe(
        "\u2192",
        ("-", "^"),
        _arrow_right,
        "right-pointing caret apex on the right end of a cap-height shaft",
    ),
)


def cap_height(characters: Mapping[str, CharacterEntry], reference_char: str) -> float:
    """Return a font's cap height from its reference character's stored box.

    Args:
        characters: The font's ``characters`` mapping.
        reference_char: The font's reference character (usually ``"E"``).

    Returns:
        The reference character's ``bounding_box.max_y`` in plotter units, or
        :data:`CAP_HEIGHT_FALLBACK` when it is absent or not positive.
    """
    entry = characters.get(reference_char)
    box = entry.get("bounding_box") if isinstance(entry, dict) else None
    if isinstance(box, dict):
        try:
            value = float(box["max_y"])
        except (KeyError, TypeError, ValueError):
            return CAP_HEIGHT_FALLBACK
        if math.isfinite(value) and value > 0.0:
            return value
    return CAP_HEIGHT_FALLBACK


def _parse_bases(
    characters: Mapping[str, CharacterEntry],
    recipe: GlyphRecipe,
    cache: Dict[str, GlyphGeometry],
) -> Tuple[Dict[str, GlyphGeometry], List[str]]:
    """Parse (and cache) one recipe's base glyphs.

    Args:
        characters: The font's ``characters`` mapping.
        recipe: The recipe whose bases are needed.
        cache: Per-font parse cache, updated in place.

    Returns:
        ``(bases, missing)``: the usable parsed bases keyed by character, and
        the base characters that are absent or carry no geometry.
    """
    bases: Dict[str, GlyphGeometry] = {}
    missing: List[str] = []
    for base in recipe.bases:
        geometry = cache.get(base)
        if geometry is None:
            entry = characters.get(base)
            glyph = entry.get("glyph") if isinstance(entry, dict) else None
            geometry = parse_glyph(glyph) if isinstance(glyph, str) else GlyphGeometry()
            cache[base] = geometry
        if geometry.is_empty:
            missing.append(base)
        else:
            bases[base] = geometry
    return bases, missing


def derive_glyph_entries(
    characters: Mapping[str, CharacterEntry],
    *,
    reference_char: str,
    envelope_samples: int,
    log: Optional[logging.Logger] = None,
) -> Dict[str, CharacterEntry]:
    """Derive the Unicode glyphs a font's ASCII entries support.

    Every recipe in :data:`DERIVED_GLYPHS` is attempted. A recipe whose base
    character is missing (or carries no geometry) is skipped with a WARNING, so
    a font engraved from a reduced character list simply gets fewer derived
    glyphs; a recipe that cannot be applied to a particular font's shapes (e.g.
    stretching a hyphen that carries arcs) is skipped the same way. Derived
    keys never overwrite an existing (engraved) key.

    Args:
        characters: A font's ``characters`` mapping (ASCII entries).
        reference_char: The font's reference character, defining the cap line.
        envelope_samples: Envelope sample count per profile.
        log: Optional logger for the skip messages.

    Returns:
        New entries keyed by the derived characters (empty when nothing can be
        derived).
    """
    active_log = log if log is not None else logger
    cap = cap_height(characters, reference_char)
    cache: Dict[str, GlyphGeometry] = {}
    derived: Dict[str, CharacterEntry] = {}

    for recipe in DERIVED_GLYPHS:
        bases, missing = _parse_bases(characters, recipe, cache)
        if missing:
            active_log.warning(
                "Skipping derived character %r (U+%04X): base glyph(s) %s unavailable",
                recipe.character,
                ord(recipe.character),
                ", ".join(repr(char) for char in missing),
            )
            continue
        if recipe.character in characters:
            active_log.warning(
                "Keeping existing character %r (U+%04X); recipe %s not applied",
                recipe.character,
                ord(recipe.character),
                recipe.description,
            )
            continue
        context = DerivedContext(bases=bases, cap=cap, midline=MIDLINE_FRACTION * cap)
        try:
            elements = _reanchor(recipe.builder(context))
            derived[recipe.character] = build_glyph_entry(
                elements, envelope_samples=envelope_samples
            )
        except (GlyphTransformError, ValueError) as e:
            active_log.warning(
                "Skipping derived character %r (U+%04X): %s",
                recipe.character,
                ord(recipe.character),
                e,
            )
    return derived
