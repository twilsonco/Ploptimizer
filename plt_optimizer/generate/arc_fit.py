"""Fit circular ``AA`` arcs to the ellipse arcs that non-uniform scales create.

A PLT-extracted glyph is natively ``PU``/``PD``/``AA``: its curves are circular
arcs. A **uniform** scale maps a circle to a circle, so
:class:`~plt_optimizer.generate.text_geometry.ArcSeg` survives it exactly. A
**non-uniform** map (horizontal compression, the cap-line/descender vertical
scale, the italic shear) maps a circle to an ellipse, and an ellipse is not a
circular arc -- which is why every one of those operations historically
polygonized its arcs into chord-bounded line segments.

Polygonizing is accurate but expensive: on the shipped font library a
compressed line's arcs expand ~2.8x into line segments, and every extra vertex
is extra plotted distance and extra HPGL the plotter has to interpret.
EngraveLab/Vision Pro solve the same problem by **refitting the ellipse with a
short chain of circular arcs**, so the toolpath keeps its ``AA`` commands. This
module implements that refit.

The math
--------
Applying an affine map to the circle ``z(t) = c + r*(cos t, sin t)`` gives

    ``P(t) = C + cos(t) * U + sin(t) * V``

which is the *parametric* ellipse arc. The parameter ``t`` is the **source
center angle** -- NOT the eccentric angle of the ellipse. That is the whole
point: the arc's start/end angles carry over verbatim, so a fitted chain starts
exactly where the preceding transformed segment ends and the tip-to-tail
invariant of :class:`Stroke` is preserved bit-for-bit. Every transform in the
generate pipeline maps to one row of:

======================  ==================  ==============  ==============
transform               ``C``               ``U``           ``V``
======================  ==================  ==============  ==============
``compress_x(s, ax)``   ``((cx-ax)s+ax,``   ``(s*r, 0)``    ``(0, r)``
                        ``cy)``
``scaled_y(f)``         ``(cx, f*cy)``      ``(r, 0)``      ``(0, f*r)``
``sheared(tan)``        ``(cx+cy*tan,``     ``(r, 0)``      ``(tan*r, r)``
                        ``cy)``
======================  ==================  ==============  ==============

The shear is the general case: it produces a *rotated* ellipse, which is why
``U``/``V`` are complex vectors rather than ``(a, 0)``/``(0, b)`` axis
semi-diameters.

The fitter
----------
:func:`fit_ellipse_arc` splits the parametric span into ``k`` sub-arcs and
builds one circular arc per sub-arc. The first centre is the intersection of
the **ellipse normal at the chain start** with the first chord's
**perpendicular bisector** (tangent to the true ellipse at the start, passing
through both endpoints); every later centre sits on the **shared normal** --
the line through the previous centre and the junction point -- and on the new
chord's bisector. Consecutive arcs therefore meet with equal tangents at every
junction: the chain is **C0 + G1** (tangent continuous) with exact tip-to-tail
endpoints. A plain 3-point circumcircle fit is only C0 and kinks up to ~11
degrees at interior junctions, which is precisely what the normal-chaining
construction avoids.

``k`` is chosen as the smallest value meeting :data:`ARC_FIT_DEV_TOL_INCHES`,
estimated in closed form from the ellipse's maximum curvature radius and then
verified numerically (the estimate is cheap and almost always exact; the
verification keeps the tolerance a hard guarantee).

A **degenerate-span check** runs first: a span whose endpoints sit within half
a plotter unit of each other carries no ink that the device can resolve, so it
emits as a single ``PD`` vertex. Near-straight arcs are *not* collapsed -- a
previous version collapsed every arc whose sagitta fell under the tolerance
(34 of a compressed ``S``'s 44 arcs), which polygonized the very toolpaths this
module exists to keep native. EngraveLab keeps arcs as arcs through the same
compression (44 -> 49 ``AA`` on that ``S``), and an ``AA`` arc whose deviation
from its chord is below coordinate quantization draws through the same integer
plotter units as the chord itself, so preserving it costs nothing on the plate
and keeps the preview arc-native.

:func:`fit_ellipse_arc` returns ``None`` when it cannot guarantee the
tolerance (a degenerate geometry, an orientation flip, a span too wide for
:data:`ARC_FIT_MAX_ARCS`), which tells the caller to fall back to flattening.
The fallback is never silent -- ``text_geometry`` logs it.

Python 3.8 / Windows 7: pure ``math``/``dataclasses``, no third-party imports.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

__all__ = [
    "ARC_FIT_DEV_TOL_INCHES",
    "ARC_FIT_MAX_ARCS",
    "EllipseArc",
    "FittedSegment",
    "ellipse_from_arc",
    "fit_ellipse_arc",
    "fit_arc",
    "ellipse_deviation",
]

# Maximum allowed deviation, in inches, between a fitted circular arc and the
# true ellipse arc it replaces.
#
# Why 0.0005in and not the 0.0002in used for flattening: an HPGL ``AA`` carries
# no radius -- the plotter derives it from the (integer) pen position and the
# (integer) centre, so an emitted arc's radius is quantized to ~1 plotter unit
# (1/1016 = 0.00098in). Measured end-to-end on the shipped library, the
# *emitted* deviation of a fitted chain sits at ~0.002in (2 units) at every
# tolerance from 0.0002in up: coordinate quantization, not the fit, is the
# floor. Tightening this constant below ~0.0005in buys arc count, not accuracy.
# 0.0005in is ~30x finer than the smallest cutter in a typical tool set
# (0.015in), so the refit is invisible on the finished part.
ARC_FIT_DEV_TOL_INCHES: float = 0.0005

# Hard cap on the arcs one source arc may expand into. The measured maximum
# needed on the shipped library is 4-5 (at scale 0.02, far below the
# ``max_h_compress`` floor of 0.0); 8 leaves headroom, and a span that needs
# more than that is a degenerate input best served by flattening.
ARC_FIT_MAX_ARCS: int = 8

# Samples used to verify a fit's deviation and to bound the curvature radius.
_VERIFY_SAMPLES: int = 32
_CURVATURE_SAMPLES: int = 9

# Safety margin on the verification pass. The 32-sample verifier under-reads
# the true (densely sampled) deviation by ~1.5% on the worst arcs, so a chain
# is accepted only when it clears ``tol * _VERIFY_MARGIN`` -- the promise
# ``deviation <= tol`` then holds against the exact curve.
_VERIFY_MARGIN: float = 0.8

# Relative cross-product magnitude below which two lines count as parallel.
_PARALLEL_REL_TOL: float = 1e-9

# Chord length (inches) under which a sub-arc is degenerate. Half a plotter
# unit is the emission floor -- ink shallower than that cannot exist in the
# output, so a shorter chord carries no information.
_MIN_CHORD_INCHES: float = 0.5 / 1016.0

# Angular slack (degrees) when testing whether a point lies inside a sweep.
_ANGLE_SLACK_DEG: float = 1e-6


def _cross(a: complex, b: complex) -> float:
    """Return the 2D cross product (z component) of two vectors."""
    return a.real * b.imag - a.imag * b.real


def _normalize_deg(angle: float) -> float:
    """Wrap an angle in degrees into ``(-180, 180]``."""
    angle = math.fmod(angle, 360.0)
    if angle > 180.0:
        angle -= 360.0
    elif angle <= -180.0:
        angle += 360.0
    return angle


@dataclass(frozen=True)
class EllipseArc:
    """A parametric ellipse arc ``P(t) = c + cos(t)*u + sin(t)*v``.

    ``t`` runs from :attr:`t0_deg` to ``t0_deg + sweep_deg`` and is the
    **source circle's center angle** (degrees), which is what makes the affine
    image of a circular arc chain tip-to-tail with its neighbours.

    Attributes:
        c: Ellipse centre (complex, inches).
        u: Conjugate semi-diameter vector for ``cos(t)`` (complex, inches).
        v: Conjugate semi-diameter vector for ``sin(t)`` (complex, inches).
        t0_deg: Start parameter, in degrees.
        sweep_deg: Signed parameter span, in degrees (sign = direction).
    """

    c: complex
    u: complex
    v: complex
    t0_deg: float
    sweep_deg: float

    def point(self, t_rad: float) -> complex:
        """Return ``P(t)`` for a parameter in radians."""
        return self.c + math.cos(t_rad) * self.u + math.sin(t_rad) * self.v

    def tangent(self, t_rad: float) -> complex:
        """Return ``P'(t)`` (the tangent vector) for a parameter in radians."""
        return -math.sin(t_rad) * self.u + math.cos(t_rad) * self.v

    def curvature_radius(self, t_rad: float) -> float:
        """Return the osculating circle's radius at a parameter in radians.

        ``R = |P'|^3 / |cross(P', P'')|``; ``inf`` where the curvature
        vanishes (a straight degenerate ellipse).
        """
        d1 = self.tangent(t_rad)
        d2 = -math.cos(t_rad) * self.u - math.sin(t_rad) * self.v
        den = abs(_cross(d1, d2))
        if den <= 0.0:
            return math.inf
        return abs(d1) ** 3 / den

    @property
    def start(self) -> complex:
        """The arc's start point ``P(t0)``."""
        return self.point(math.radians(self.t0_deg))

    @property
    def end(self) -> complex:
        """The arc's end point ``P(t0 + sweep)``."""
        return self.point(math.radians(self.t0_deg + self.sweep_deg))

    @property
    def max_curvature_radius(self) -> float:
        """The largest curvature radius over the span (the flattest the arc gets).

        Sampled, not extremized: the curvature radius of an ellipse arc is
        unimodal between its vertices, and :data:`_CURVATURE_SAMPLES` across a
        span of at most a few hundred degrees resolves it well past the
        precision a ``ceil`` needs.
        """
        t0 = math.radians(self.t0_deg)
        span = math.radians(self.sweep_deg)
        if span == 0.0:
            return math.inf
        return max(
            self.curvature_radius(t0 + span * i / (_CURVATURE_SAMPLES - 1))
            for i in range(_CURVATURE_SAMPLES)
        )


@dataclass(frozen=True)
class FittedSegment:
    """One emitted segment of a fitted chain: a circular arc, or a line.

    Attributes:
        start: Start point (complex, inches).
        end: End point (complex, inches).
        center: Arc centre (complex, inches), or ``None`` for a line segment.
        sweep_deg: Signed sweep in degrees (HPGL ``AA`` convention: the arc runs
            from :attr:`start` through ``sweep_deg`` around :attr:`center`).
            Always ``0.0`` for a line.
    """

    start: complex
    end: complex
    center: Optional[complex] = None
    sweep_deg: float = 0.0

    @property
    def is_arc(self) -> bool:
        """True when this segment is a circular arc (emits as ``AA``)."""
        return self.center is not None

    @property
    def radius(self) -> float:
        """The arc radius (0.0 for a line)."""
        if self.center is None:
            return 0.0
        return abs(self.start - self.center)


def ellipse_from_arc(
    center: complex,
    radius: float,
    t0_deg: float,
    sweep_deg: float,
    *,
    scale_x: Optional[float] = None,
    anchor_x: float = 0.0,
    scale_y: Optional[float] = None,
    shear_tan: Optional[float] = None,
) -> EllipseArc:
    """Return the affine image of a circular arc as a parametric ellipse arc.

    Exactly one of ``scale_x`` / ``scale_y`` / ``shear_tan`` is expected (the
    three non-uniform transforms the generate pipeline applies to glyph
    geometry); combining them is allowed and composes as
    ``shear -> scale_y -> scale_x``.

    Args:
        center: Source arc centre (complex, inches).
        radius: Source arc radius (inches, > 0).
        t0_deg: Source arc start angle around ``center``, in degrees.
        sweep_deg: Source signed sweep in degrees.
        scale_x: Horizontal scale factor (X-only, about ``anchor_x``).
        anchor_x: Fixed X of a horizontal scale (the block's left edge).
        scale_y: Vertical scale factor (Y-only, about the baseline ``y = 0``).
        shear_tan: Italic shear ``tan(angle)`` (``x' = x + y*tan``, about
            ``y = 0``).

    Returns:
        The :class:`EllipseArc` whose ``P(t)`` is the transformed ``z(t)``.
    """
    sx = 1.0 if scale_x is None else scale_x
    sy = 1.0 if scale_y is None else scale_y
    tan = 0.0 if shear_tan is None else shear_tan

    # The map is x' = sx*(x + y*tan - anchor_x) + anchor_x, y' = sy*y.
    cx, cy = center.real, center.imag
    new_c = complex(sx * (cx + cy * tan - anchor_x) + anchor_x, sy * cy)
    # z(t) = c + r*(cos t, sin t): U is the image of the r*x basis vector,
    # V of the r*y one.
    new_u = complex(sx * radius, 0.0)
    new_v = complex(sx * tan * radius, sy * radius)
    return EllipseArc(c=new_c, u=new_u, v=new_v, t0_deg=t0_deg, sweep_deg=sweep_deg)


def _line_intersection(p1: complex, d1: complex, p2: complex, d2: complex) -> Optional[complex]:
    """Return the intersection of lines ``p1 + s*d1`` and ``p2 + t*d2``.

    ``None`` when the lines are (numerically) parallel.
    """
    den = _cross(d1, d2)
    scale = abs(d1) * abs(d2)
    if scale <= 0.0 or abs(den) <= _PARALLEL_REL_TOL * scale:
        return None
    s = _cross(p2 - p1, d2) / den
    return p1 + s * d1


def _circumcenter(a: complex, b: complex, c: complex) -> Optional[complex]:
    """Return the circumcentre of ``a``, ``b``, ``c`` (``None`` if collinear)."""
    return _line_intersection(0.5 * (a + b), 1j * (b - a), 0.5 * (b + c), 1j * (c - b))


def _signed_sweep(center: complex, start: complex, end: complex) -> float:
    """Return the signed angle (degrees, ``(-180, 180]``) from start to end."""
    a0 = math.degrees(math.atan2(start.imag - center.imag, start.real - center.real))
    a1 = math.degrees(math.atan2(end.imag - center.imag, end.real - center.real))
    return _normalize_deg(a1 - a0)


def _fit_subarc_start(arc: EllipseArc, t_a: float, t_b: float) -> Optional[complex]:
    """Centre of the first sub-arc: ellipse normal at ``t_a`` ∩ chord bisector.

    The normal constraint makes the arc tangent to the true ellipse at the
    chain's start; the bisector constraint makes it pass through both endpoints.
    ``None`` when the two lines are parallel.
    """
    a = arc.point(t_a)
    b = arc.point(t_b)
    return _line_intersection(a, 1j * arc.tangent(t_a), 0.5 * (a + b), 1j * (b - a))


def _chain_center(prev_center: complex, p: complex, q: complex) -> Optional[complex]:
    """Centre of a chained sub-arc from ``p`` to ``q`` (given the previous centre).

    The line ``prev_center -> p`` is the previous arc's radius at the junction,
    i.e. the shared normal; putting the new centre on it makes the two arcs'
    tangents equal at ``p`` (**G1**). The perpendicular bisector of ``p``, ``q``
    makes the new circle pass through both endpoints (**C0**, exact
    tip-to-tail chaining). ``None`` when the lines are parallel.
    """
    d = p - prev_center
    if abs(d) <= 0.0:
        return None
    return _line_intersection(prev_center, d, 0.5 * (p + q), 1j * (q - p))


def _fit_chain(arc: EllipseArc, k: int) -> Optional[List[FittedSegment]]:
    """Fit the whole span with ``k`` tangent-continuous sub-arcs.

    Sub-arc 1's centre is the intersection of the ellipse normal at the start
    with the first chord's bisector (tangent-matched to the true ellipse).
    Every later centre chains along the shared normal (see
    :func:`_chain_center`), so the whole run is G1 with exact tip-to-tail
    endpoints. Parallel/degenerate steps fall back to the 3-point circumcircle
    (C0, tiny kink) and then to a line; a zero-length chord aborts the chain
    (the caller tries the next ``k`` / flattens).
    """
    t0 = math.radians(arc.t0_deg)
    span = math.radians(arc.sweep_deg)
    pts = [arc.point(t0 + span * i / k) for i in range(k + 1)]
    if any(abs(b - a) < _MIN_CHORD_INCHES for a, b in zip(pts[:-1], pts[1:])):
        return None

    centers: List[Optional[complex]] = []
    first = _fit_subarc_start(arc, t0, t0 + span / k)
    centers.append(first)
    for i in range(1, k):
        prev = centers[-1]
        center: Optional[complex] = None
        if prev is not None:
            center = _chain_center(prev, pts[i], pts[i + 1])
        if center is None:
            center = _circumcenter(pts[i - 1], pts[i], pts[i + 1])
        centers.append(center)

    segments: List[FittedSegment] = []
    for i, (a, b) in enumerate(zip(pts[:-1], pts[1:])):
        center = centers[i]
        if center is None or abs(center - a) <= 0.0:
            segments.append(FittedSegment(start=a, end=b))
            continue
        sweep = _signed_sweep(center, a, b)
        if sweep == 0.0:  # pragma: no cover - the chord guard above excludes it
            segments.append(FittedSegment(start=a, end=b))
            continue
        segments.append(FittedSegment(start=a, end=b, center=center, sweep_deg=sweep))
    return segments


def _dist_to_segment(p: complex, seg: FittedSegment) -> float:
    """Return the distance from a point to a fitted segment (arc or line)."""
    if seg.center is None:
        d = seg.end - seg.start
        len2 = d.real * d.real + d.imag * d.imag
        if len2 <= 0.0:
            return abs(p - seg.start)
        t = ((p - seg.start).real * d.real + (p - seg.start).imag * d.imag) / len2
        t = max(0.0, min(1.0, t))
        return abs(p - (seg.start + t * d))

    center = seg.center
    radius = abs(seg.start - center)
    rel = _normalize_deg(
        math.degrees(math.atan2(p.imag - center.imag, p.real - center.real))
        - math.degrees(math.atan2(seg.start.imag - center.imag, seg.start.real - center.real))
    )
    inside = (
        -_ANGLE_SLACK_DEG <= rel <= seg.sweep_deg + _ANGLE_SLACK_DEG
        if seg.sweep_deg >= 0.0
        else seg.sweep_deg - _ANGLE_SLACK_DEG <= rel <= _ANGLE_SLACK_DEG
    )
    if inside:
        return abs(abs(p - center) - radius)
    return min(abs(p - seg.start), abs(p - seg.end))


def ellipse_deviation(arc: EllipseArc, segments: List[FittedSegment]) -> float:
    """Return the max distance (inches) from the true ellipse arc to a chain.

    One-sided (ellipse -> chain): the chain's endpoints lie on the ellipse by
    construction, so the sampled ellipse points bound the Hausdorff distance.
    """
    t0 = math.radians(arc.t0_deg)
    span = math.radians(arc.sweep_deg)
    worst = 0.0
    for i in range(_VERIFY_SAMPLES + 1):
        p = arc.point(t0 + span * i / _VERIFY_SAMPLES)
        worst = max(worst, min(_dist_to_segment(p, seg) for seg in segments))
    return worst


def _estimate_k(arc: EllipseArc, tol: float) -> int:
    """Closed-form first guess at the sub-arc count.

    A circular arc of radius ``R`` over a sweep ``phi`` deviates from its chord
    by ``R*(1 - cos(phi/2)) ~= R*phi^2/8``. Solving ``R*phi^2/(8k^2) <= tol``
    for ``k`` gives the estimate; the caller verifies it numerically.
    """
    phi = abs(math.radians(arc.sweep_deg))
    radius = arc.max_curvature_radius
    if phi <= 0.0 or radius <= 0.0 or not math.isfinite(radius):
        return 1
    return max(1, math.ceil(phi * math.sqrt(radius / (8.0 * tol))))


def _sweep_sign_ok(arc: EllipseArc, segments: List[FittedSegment]) -> bool:
    """True when every fitted arc turns the same way as the source.

    All three transforms have positive determinant, so a fitted chain must
    preserve the sweep's sign. A disagreement means the construction landed on
    the far side of the ellipse (a numerically degenerate case) -- the caller
    should flatten instead of emitting a looped arc.
    """
    want = math.copysign(1.0, arc.sweep_deg)
    for seg in segments:
        if seg.center is None:
            continue
        if math.copysign(1.0, seg.sweep_deg) != want:
            return False
    return True


def fit_ellipse_arc(
    arc: EllipseArc,
    tol_inches: float = ARC_FIT_DEV_TOL_INCHES,
    max_arcs: int = ARC_FIT_MAX_ARCS,
) -> Optional[Tuple[FittedSegment, ...]]:
    """Refit an ellipse arc with a tangent-continuous chain of circular arcs.

    Args:
        arc: The parametric ellipse arc to approximate.
        tol_inches: Maximum allowed deviation from the true ellipse.
        max_arcs: Hard cap on the chain length.

    Returns:
        The chain (one :class:`FittedSegment` per emitted segment, arcs and
        lines mixed), ordered and tip-to-tail. ``None`` when the tolerance
        cannot be guaranteed -- the caller must then fall back to flattening.
        A span whose endpoints sit within :data:`_MIN_CHORD_INCHES` (half a
        plotter unit -- the emission floor) emits as a single line; near-straight
        spans are NOT collapsed, so the chain keeps ``AA`` commands wherever the
        source arc had one.
    """
    if arc.sweep_deg == 0.0 or tol_inches <= 0.0:
        return None
    if _cross(arc.u, arc.v) == 0.0:
        # Zero-area ellipse (a collapsed axis, or a circle centred on the
        # baseline under shear): the image is a straight segment, there is no
        # curve to fit, and a circular arc cannot represent it.
        return None
    if abs(arc.end - arc.start) <= _MIN_CHORD_INCHES:
        if abs(arc.sweep_deg) >= 180.0:
            # A (near-)closed loop: its endpoints meet within the emission
            # floor, but a chord would erase the whole loop's ink. The span is
            # beyond what a capped chain can close, so the caller flattens it
            # into a closed polyline.
            return None
        # Degenerate span: the endpoints sit within half a plotter unit, so no
        # resolvable ink exists between them (a sub-unit sliver of a real
        # curve). The chord carries the span; a zero-length segment is not
        # emittable.
        return (FittedSegment(start=arc.start, end=arc.end),)

    # Probe 1, 2, 4, 8 from the closed-form estimate, then bisect between the
    # last failure and the first success so the count is minimal.
    guess = max(1, min(max_arcs, _estimate_k(arc, tol_inches)))
    probes = sorted({1, 2, 4, 8, max_arcs, guess, guess + 1, guess * 2})
    probes = [k for k in probes if 1 <= k <= max_arcs]

    best: Optional[Tuple[FittedSegment, ...]] = None
    lo = 0
    hi: Optional[int] = None
    verify_tol = tol_inches * _VERIFY_MARGIN
    for k in probes:
        segments = _fit_chain(arc, k)
        if segments is None:
            continue
        ok = ellipse_deviation(arc, segments) <= verify_tol and _sweep_sign_ok(arc, segments)
        if ok:
            best = tuple(segments)
            hi = k
            break
        lo = k
    if best is None:
        return None

    # Minimal k: bisect the (lo, hi) gap left by the geometric probe.
    while hi is not None and hi - lo > 1:
        mid = (lo + hi) // 2
        segments = _fit_chain(arc, mid)
        if (
            segments is not None
            and ellipse_deviation(arc, segments) <= verify_tol
            and _sweep_sign_ok(arc, segments)
        ):
            best = tuple(segments)
            hi = mid
        else:
            lo = mid
    return best


def fit_arc(
    center: complex,
    radius: float,
    t0_deg: float,
    sweep_deg: float,
    *,
    scale_x: Optional[float] = None,
    anchor_x: float = 0.0,
    scale_y: Optional[float] = None,
    shear_tan: Optional[float] = None,
    tol_inches: float = ARC_FIT_DEV_TOL_INCHES,
    max_arcs: int = ARC_FIT_MAX_ARCS,
) -> Optional[Tuple[FittedSegment, ...]]:
    """Convenience wrapper: transform a circular arc, then refit it.

    Args:
        center: Source arc centre (complex, inches).
        radius: Source arc radius (inches).
        t0_deg: Source start angle around ``center``, in degrees.
        sweep_deg: Source signed sweep in degrees.
        scale_x: Horizontal scale factor (X-only, about ``anchor_x``).
        anchor_x: Fixed X of a horizontal scale.
        scale_y: Vertical scale factor (Y-only, about ``y = 0``).
        shear_tan: Italic shear ``tan(angle)``.
        tol_inches: Maximum allowed deviation from the true ellipse.
        max_arcs: Hard cap on the chain length.

    Returns:
        The fitted chain, or ``None`` when flattening is required.
    """
    arc = ellipse_from_arc(
        center,
        radius,
        t0_deg,
        sweep_deg,
        scale_x=scale_x,
        anchor_x=anchor_x,
        scale_y=scale_y,
        shear_tan=shear_tan,
    )
    return fit_ellipse_arc(arc, tol_inches=tol_inches, max_arcs=max_arcs)
