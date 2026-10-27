"""Fit circular ``AA`` arcs to the ellipse arcs that non-uniform scales create.

The module serves two callers. **Text**: a PLT-extracted glyph is natively
``PU``/``PD``/``AA``: its curves are circular arcs. A **uniform** scale maps a
circle to a circle, so :class:`~plt_optimizer.generate.text_geometry.ArcSeg`
survives it exactly. A **non-uniform** map (horizontal compression, the
cap-line/descender vertical scale, the italic shear) maps a circle to an
ellipse, and an ellipse is not a circular arc -- which is why every one of
those operations historically polygonized its arcs into chord-bounded line
segments. **Borders**: the label border emitters
(:func:`plt_optimizer.generate.label_renderer._render_boundary_hpgl`) build
the ellipse and rounded-rectangle contours from scratch here --
:class:`EllipseArc` for the ellipse quadrants and :class:`SuperellipseArc` +
:func:`fit_superellipse_arc` for the G2 corner patches (trim-to-line: the
zero-curvature axis vertices fit as chords merged into the straight edges,
the inner span as the circular chain).

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

The border patches
------------------
:class:`SuperellipseArc` is the rounded-rectangle corner curve
(``|X / rx| ^ n + |Y / ry| ^ n = 1``), which is NOT an affine image of a
circle for ``n != 2``. Its axis vertices have zero curvature for ``n > 2``
(the G2 junction with the straight edges), which breaks the fitter's
assumptions twice over: the closed-form ``k`` estimate divides by the maximum
curvature radius (infinite at a vertex) and the normal-chain construction
degenerates where the normal is the box edge itself.
:func:`fit_superellipse_arc` fixes this by **trimming**: the near-flat
parameter spans adjacent to each vertex -- bisected so each cap's chord
deviation stays within tolerance -- emit as line segments (collinear with, and
merged into, the border's straight edges), and only the inner span refits with
the circular chain. The whole contour (caps + chain) is verified at
:data:`_DENSE_VERIFY_SAMPLES` samples -- the 32-sample verifier under-reads the
vertex-weighted superellipse -- with one half-tolerance retry before declining.
Borders use :data:`BORDER_ARC_FIT_MAX_ARCS` (32/quadrant), larger than the
glyph cap: a border quadrant sweeps 90 degrees of a label-size ellipse, tens of
times the size of a font arc.

Python 3.8 / Windows 7: pure ``math``/``dataclasses``, no third-party imports.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Protocol, Tuple

__all__ = [
    "ARC_FIT_DEV_TOL_INCHES",
    "ARC_FIT_MAX_ARCS",
    "BORDER_ARC_FIT_MAX_ARCS",
    "EllipseArc",
    "FittableArc",
    "FittedSegment",
    "SuperellipseArc",
    "ellipse_from_arc",
    "fit_ellipse_arc",
    "fit_arc",
    "fit_superellipse_arc",
    "ellipse_deviation",
]

# Maximum allowed deviation, in inches, between a fitted circular arc and the
# true ellipse arc it replaces.
#
# 0.0002in matches EngraveLab's emitted toolpaths: measured on the reference
# exports, its compression splits exactly the arcs whose 1-arc fit deviates
# from the true ellipse by more than ~0.0002in (the compressed `S`'s three
# 19-26 deg arcs -> 44 becomes 47-49 `AA`), and keeps every tighter arc single.
# Note the physical floor: an HPGL ``AA`` carries no radius -- the plotter
# derives it from the (integer) pen position and the (integer) centre, so an
# emitted arc's radius is quantized to ~1 plotter unit (1/1016 = 0.00098in),
# and the *emitted* deviation of a fitted chain measures ~0.002in (2 units) at
# every fit tolerance from 0.0002in up. The sub-quantization refinement buys
# preview/curve fidelity (and reference parity), not engraving accuracy;
# 0.0002in is still ~75x finer than the smallest cutter in a typical tool set
# (0.015in).
ARC_FIT_DEV_TOL_INCHES: float = 0.0002

# Hard cap on the arcs one source arc may expand into. Measured on the shipped
# library at :data:`ARC_FIT_DEV_TOL_INCHES` (all arcs x scales 0.5/0.3), the
# maximum needed is 10 (the widest sweeps at the deepest compression); 12
# leaves headroom, and a span that needs more than that is a degenerate input
# best served by flattening.
ARC_FIT_MAX_ARCS: int = 12

# Chain cap for label-border fits (:func:`fit_superellipse_arc` and the
# ellipse/rounded-rect border emitters). A glyph arc is at most ~90 degrees of
# a font-size circle; a border quadrant sweeps 90 degrees of a *label*-size
# ellipse (tens of times larger), so its minimal chain is larger: measured at
# :data:`ARC_FIT_DEV_TOL_INCHES`, an ellipse quadrant needs 10-12 arcs and a
# superellipse corner (after the trim-to-line caps) needs up to ~24 on the
# shipped demo shapes. 32 leaves headroom; a span needing more is a degenerate
# border (extreme aspect ratio) best served by the polygon fallback.
BORDER_ARC_FIT_MAX_ARCS: int = 32

# Samples used to verify a fit's deviation and to bound the curvature radius.
_VERIFY_SAMPLES: int = 32
_CURVATURE_SAMPLES: int = 9

# Dense verification pass for whole superellipse contours (caps + chain),
# where the 32-sample verifier under-reads the vertex-weighted deviation.
_DENSE_VERIFY_SAMPLES: int = 400

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


class FittableArc(Protocol):
    """The curve protocol :func:`fit_ellipse_arc` consumes.

    ``EllipseArc`` (the affine image of a circular arc, the parametric form
    ``P(t) = c + cos(t)*u + sin(t)*v``) and :class:`SuperellipseArc` (the
    rounded-rect corner patch) both satisfy it structurally: the fitter only
    ever evaluates the curve, its tangent, and its curvature radius at
    parameters inside ``[t0, t0 + sweep]``. Every member is read-only (the
    curves are frozen dataclasses / computed properties).
    """

    @property
    def c(self) -> complex:
        """The curve centre (complex, inches)."""
        ...  # pragma: no cover - protocol declaration

    @property
    def u(self) -> complex:
        """Conjugate semi-diameter vector for ``cos(t)`` (complex, inches)."""
        ...  # pragma: no cover - protocol declaration

    @property
    def v(self) -> complex:
        """Conjugate semi-diameter vector for ``sin(t)`` (complex, inches)."""
        ...  # pragma: no cover - protocol declaration

    @property
    def t0_deg(self) -> float:
        """Start parameter, in degrees."""
        ...  # pragma: no cover - protocol declaration

    @property
    def sweep_deg(self) -> float:
        """Signed parameter span, in degrees (sign = direction)."""
        ...  # pragma: no cover - protocol declaration

    def point(self, t_rad: float) -> complex:
        """Return the curve point at a parameter in radians."""
        ...  # pragma: no cover - protocol declaration

    def tangent(self, t_rad: float) -> complex:
        """Return the tangent vector ``P'(t)`` at a parameter in radians."""
        ...  # pragma: no cover - protocol declaration

    def curvature_radius(self, t_rad: float) -> float:
        """Return the osculating circle's radius (``inf`` where curvature vanishes)."""
        ...  # pragma: no cover - protocol declaration

    @property
    def start(self) -> complex:
        """The curve point at ``t0_deg``."""
        ...  # pragma: no cover - protocol declaration

    @property
    def end(self) -> complex:
        """The curve point at ``t0_deg + sweep_deg``."""
        ...  # pragma: no cover - protocol declaration

    @property
    def max_curvature_radius(self) -> float:
        """The largest curvature radius over the span (the flattest point)."""
        ...  # pragma: no cover - protocol declaration

    @property
    def expected_sweep_sign(self) -> float:
        """The sign every fitted arc's sweep must carry (orientation guard)."""
        ...  # pragma: no cover - protocol declaration


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

    @property
    def expected_sweep_sign(self) -> float:
        """The source sweep's sign: every affine map here has positive determinant.

        All three glyph transforms (``scale_x``/``scale_y``/``shear``) preserve
        the traversal orientation, so a fitted chain must turn the same way the
        parametric span runs. A mirrored map (negative determinant, e.g. a
        negative ``scale_x``) is out of scope: the check rejects the fit and
        the caller flattens.
        """
        return math.copysign(1.0, self.sweep_deg)


@dataclass(frozen=True)
class SuperellipseArc:
    """A superellipse quadrant patch ``|X / rx|^n + |Y / ry|^n = 1``.

    The rounded-rectangle corner curve
    (``plt_optimizer.generate.resolution.rounded_rect_corner_params``), in the
    same parametrisation
    (``plt_optimizer.generate.label_renderer._superellipse_quad``) the polygon
    sampler uses::

        P(t) = c + sx * rx * |cos t|^p + i * sy * ry * |sin t|^p,  p = 2 / n

    ``t`` runs from :attr:`t0_deg` to ``t0_deg + sweep_deg`` inside one
    quadrant (typically ``[0, pi/2]``), where the absolute values are smooth.
    The patch satisfies the :class:`FittableArc` protocol, so
    :func:`fit_ellipse_arc` refits it with the same tangent-continuous circular
    chain machinery the ellipse uses -- with two axis-vertex caveats built in:

    - For ``n > 2`` the tangent at ``t = 0`` / ``t = pi/2`` is exactly
      axis-aligned (the curve meets the straight edge with zero curvature),
      and the closed-form derivative is singular there (``0^(p-1)``,
      ``p < 1``), so :meth:`tangent` returns the exact limit at the vertices.
    - The curvature radius diverges at the axis vertices for ``n > 2``
      (that is the G2 property), so :attr:`max_curvature_radius` samples
      **interior** midpoints only -- the endpoint-inclusive sampling
      :class:`EllipseArc` uses would return ``inf`` and poison the ``k``
      estimate.

    Attributes:
        c: Patch centre (complex, inches) -- the corner of the label box.
        rx: Semi-axis along X (inches, > 0).
        ry: Semi-axis along Y (inches, > 0).
        exponent: Superellipse exponent ``n`` (>= 2; ``n == 2`` is an ellipse
            quadrant).
        sx: +1 to open rightward, -1 leftward.
        sy: +1 to open upward, -1 downward.
        t0_deg: Start parameter, in degrees.
        sweep_deg: Signed parameter span, in degrees.
    """

    c: complex
    rx: float
    ry: float
    exponent: float
    sx: float
    sy: float
    t0_deg: float
    sweep_deg: float

    @property
    def _power(self) -> float:
        """The sampling power ``p = 2 / n``."""
        return 2.0 / self.exponent

    # The protocol's conjugate-diameter view: the patch's bounding semi-axis
    # vectors. Not used by the fitter's geometry (every point comes from
    # ``point``); they exist for protocol conformance and debugging.
    @property
    def u(self) -> complex:
        """Conjugate semi-diameter for ``cos(t)``: ``(sx * rx, 0)``."""
        return complex(self.sx * self.rx, 0.0)

    @property
    def v(self) -> complex:
        """Conjugate semi-diameter for ``sin(t)``: ``(0, sy * ry)``."""
        return complex(0.0, self.sy * self.ry)

    def point(self, t_rad: float) -> complex:
        """Return ``P(t)`` for a parameter in radians."""
        p = self._power
        ct, st = math.cos(t_rad), math.sin(t_rad)
        return self.c + complex(
            self.sx * self.rx * abs(ct) ** p,
            self.sy * self.ry * abs(st) ** p,
        )

    def tangent(self, t_rad: float) -> complex:
        """Return ``P'(t)``, with the exact axis-aligned limits at the vertices."""
        p = self._power
        ct, st = math.cos(t_rad), math.sin(t_rad)
        # Axis vertices: the curve leaves/enters exactly along an axis (the
        # G2 junction with the straight edge). The closed form below is
        # singular there for p < 1 (0^(p-1)), so return the unit limit.
        if abs(ct) <= 1e-12:
            return complex(-self.sx, 0.0)
        if abs(st) <= 1e-12:
            return complex(0.0, self.sy)
        dx = -self.sx * self.rx * p * abs(ct) ** (p - 1.0) * st
        dy = self.sy * self.ry * p * abs(st) ** (p - 1.0) * ct
        return complex(dx, dy)

    def curvature_radius(self, t_rad: float) -> float:
        """Return the osculating radius; ``inf`` at the axis vertices for ``n > 2``."""
        p = self._power
        ct, st = math.cos(t_rad), math.sin(t_rad)
        if abs(ct) <= 1e-9 or abs(st) <= 1e-9:
            # Curvature vanishes at the vertices for n > 2 (the G2 property);
            # for n == 2 the endpoint value is finite, and the interior
            # sampling of max_curvature_radius resolves the span's max.
            return math.inf
        ctp, stp = abs(ct), abs(st)
        dx: float = -self.sx * self.rx * p * ctp ** (p - 1.0) * st
        dy: float = self.sy * self.ry * p * stp ** (p - 1.0) * ct
        ddx: float = (
            self.sx * self.rx * p * ((p - 1.0) * ctp ** (p - 2.0) * st * st - ctp ** (p - 1.0) * ct)
        )
        ddy: float = (
            self.sy * self.ry * p * ((p - 1.0) * stp ** (p - 2.0) * ct * ct - stp ** (p - 1.0) * st)
        )
        den = abs(_cross(complex(dx, dy), complex(ddx, ddy)))
        if den <= 0.0:
            return math.inf
        speed_sq: float = dx * dx + dy * dy
        radius: float = speed_sq**1.5 / den
        return radius

    @property
    def start(self) -> complex:
        """The patch point at ``t0_deg``."""
        return self.point(math.radians(self.t0_deg))

    @property
    def end(self) -> complex:
        """The patch point at ``t0_deg + sweep_deg``."""
        return self.point(math.radians(self.t0_deg + self.sweep_deg))

    @property
    def max_curvature_radius(self) -> float:
        """The largest *interior* curvature radius over the span.

        Midpoint-offset sampling: the endpoint-inclusive grid would hit the
        axis vertices, whose radius is ``inf`` for ``n > 2`` and would make
        :func:`_estimate_k` return 1 for every patch (the estimate divides
        into the radius, and ``inf`` collapses the closed form).
        """
        t0 = math.radians(self.t0_deg)
        span = math.radians(self.sweep_deg)
        if span == 0.0:
            return math.inf
        n = _CURVATURE_SAMPLES * 2
        vals = [self.curvature_radius(t0 + span * (i + 0.5) / n) for i in range(n)]
        finite = [v for v in vals if math.isfinite(v)]
        return max(finite) if finite else math.inf

    @property
    def expected_sweep_sign(self) -> float:
        """The turning direction of the traversed patch.

        The patch map ``(cos t, sin t) -> (sx*rx*cos^p t, sy*ry*sin^p t)`` has
        the sign of its determinant ``sx * sy``: a mirrored opening (``n > 2``
        is not an affine image of a circle, so the sign is folded in here
        explicitly) turns against the parameter's direction.
        """
        sweep = math.copysign(1.0, self.sweep_deg)
        return sweep if self.sx * self.sy >= 0.0 else -sweep


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


def _fit_subarc_start(arc: FittableArc, t_a: float, t_b: float) -> Optional[complex]:
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


def _fit_chain(arc: FittableArc, k: int) -> Optional[List[FittedSegment]]:
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


def ellipse_deviation(arc: FittableArc, segments: List[FittedSegment]) -> float:
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


def _estimate_k(arc: FittableArc, tol: float) -> int:
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


def _sweep_sign_ok(arc: FittableArc, segments: List[FittedSegment]) -> bool:
    """True when every fitted arc turns the same way as the source curve.

    The expectation is the curve's own ``expected_sweep_sign`` (the sweep sign
    times the sign of the map's determinant): a disagreement means the
    construction landed on the far side of the ellipse (a numerically
    degenerate case) -- the caller should flatten instead of emitting a looped
    arc.
    """
    want = arc.expected_sweep_sign
    for seg in segments:
        if seg.center is None:
            continue
        if math.copysign(1.0, seg.sweep_deg) != want:
            return False
    return True


def fit_ellipse_arc(
    arc: FittableArc,
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


def _chord_deviation(arc: FittableArc, t_a: float, t_b: float) -> float:
    """Max distance from the curve on ``[t_a, t_b]`` to its chord (inches).

    Densely sampled (:data:`_DENSE_VERIFY_SAMPLES + 1` points) so the trim
    decision uses the same resolution as the whole-contour gate in
    :func:`fit_superellipse_arc` -- a 32-sample reading under-measures the
    vertex-weighted cap and lets a cap slip past the tolerance. The deviation
    of a superellipse cap from its vertex chord grows monotonically with the
    span near a zero-curvature vertex, which is what makes the bisection in
    :func:`_trim_to_line_span` well-founded.
    """
    a = arc.point(t_a)
    b = arc.point(t_b)
    d = b - a
    len2 = d.real * d.real + d.imag * d.imag
    if len2 <= 0.0:
        return 0.0
    worst = 0.0
    for i in range(_DENSE_VERIFY_SAMPLES + 1):
        p = arc.point(t_a + (t_b - t_a) * i / _DENSE_VERIFY_SAMPLES)
        t = ((p - a).real * d.real + (p - a).imag * d.imag) / len2
        t = max(0.0, min(1.0, t))
        worst = max(worst, abs(p - (a + t * d)))
    return worst


def _trim_to_line_span(arc: SuperellipseArc, tol: float) -> Tuple[float, float]:
    """Inner parameter span (radians) whose vertex caps deviate <= ``tol``.

    Returns ``(t_lo, t_hi)``: the largest sub-span (in the traversal direction,
    which may be negative) starting at/ending before the patch's axis vertices
    such that the trimmed-off cap at each end stays within ``tol`` of its chord
    (and can therefore be emitted as a :class:`FittedSegment` line, merging
    into the border's straight edge). ``t_lo == t0`` / ``t_hi == t1`` when no
    trim is needed at that end.
    """
    t0 = math.radians(arc.t0_deg)
    t1 = t0 + math.radians(arc.sweep_deg)
    span = t1 - t0
    direction = 1.0 if span >= 0.0 else -1.0
    abs_span = abs(span)

    # Bisection on the cap length from each vertex. 60 halvings resolve a
    # quadrant to ~1e-18 rad -- far below what the chord test can distinguish.
    if _chord_deviation(arc, t0, t1) <= tol:
        return t1, t1  # the whole patch lies within tol of its end chord
    lo, hi = 0.0, abs_span
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if _chord_deviation(arc, t0, t0 + direction * mid) <= tol:
            lo = mid
        else:
            hi = mid
    t_lo = t0 + direction * lo
    lo, hi = 0.0, abs_span
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if _chord_deviation(arc, t1 - direction * mid, t1) <= tol:
            lo = mid
        else:
            hi = mid
    t_hi = t1 - direction * lo
    return t_lo, t_hi


def _contour_deviation(arc: FittableArc, segments: Tuple[FittedSegment, ...]) -> float:
    """Densely sampled max distance (inches) from the curve to an emitted chain.

    :func:`ellipse_deviation` verifies with 32 samples; the superellipse's
    vertex-curvature profile makes that verifier under-read by up to ~15% on
    the widest patches, so :func:`fit_superellipse_arc` re-checks its full
    contour (caps + chain) at :data:_DENSE_VERIFY_SAMPLES before accepting.
    """
    t0 = math.radians(arc.t0_deg)
    span = math.radians(arc.sweep_deg)
    worst = 0.0
    for i in range(_DENSE_VERIFY_SAMPLES + 1):
        p = arc.point(t0 + span * i / _DENSE_VERIFY_SAMPLES)
        worst = max(worst, min(_dist_to_segment(p, seg) for seg in segments))
    return worst


def fit_superellipse_arc(
    arc: SuperellipseArc,
    tol_inches: float = ARC_FIT_DEV_TOL_INCHES,
    max_arcs: int = BORDER_ARC_FIT_MAX_ARCS,
) -> Optional[Tuple[FittedSegment, ...]]:
    """Refit a superellipse corner patch with lines + a tangent-continuous arc chain.

    The patch's axis vertices are zero-curvature junctions with the border's
    straight edges (for ``n > 2``), and a circular arc cannot start tangentially
    off a straight line: fitting the FULL quadrant fails (the closed-form ``k``
    estimate diverges with the vertex curvature radius, and the normal-chain
    construction degenerates where the normal is the box edge itself). The
    patch is therefore **trimmed**: the parameter spans adjacent to each vertex
    whose chord deviation stays within ``tol_inches`` are emitted as line
    segments (collinear with, and merged into, the straight edges by the HPGL
    emitter), and only the inner span -- where the curvature is bounded -- is
    refit with the circular chain of :func:`fit_ellipse_arc`.

    The whole emitted contour (caps + chain) deviates from the true superellipse
    by at most ``tol_inches``: the caps by the trim test, the chain by the
    fitter's verification, and the junctions are tip-to-tail by construction.

    ``n == 2`` (the exact ellipse quadrant) skips the trim: the curvature is
    finite at the vertices, the normal construction is well-posed, and the
    patch fits as a plain :class:`EllipseArc` -- bit-identically to the ellipse
    border path (the fully-rounded limit parity the renderer depends on).

    Args:
        arc: The patch to fit (span inside one quadrant; the sweep sign encodes
            the traversal direction and is preserved by the chain).
        tol_inches: Maximum allowed deviation from the true curve.
        max_arcs: Hard cap on the arc-chain length (caps excluded).

    Returns:
        The ordered tip-to-tail chain (line caps around the arc chain; a
        near-straight patch collapses to a single line), or ``None`` when the
        inner span cannot meet the tolerance -- the caller must then fall back
        to polygonizing.
    """
    if arc.sweep_deg == 0.0:
        return None
    if math.isclose(arc.exponent, 2.0, rel_tol=0.0, abs_tol=1e-12):
        ellipse = EllipseArc(
            c=arc.c,
            u=arc.u,
            v=arc.v,
            t0_deg=arc.t0_deg,
            sweep_deg=arc.sweep_deg,
        )
        return fit_ellipse_arc(ellipse, tol_inches=tol_inches, max_arcs=max_arcs)

    t_lo, t_hi = _trim_to_line_span(arc, tol_inches)

    if abs(t_hi - t_lo) <= 1e-12:
        # The whole patch lies within tol of its vertex chord: one line.
        return (FittedSegment(start=arc.start, end=arc.end),)

    inner = SuperellipseArc(
        c=arc.c,
        rx=arc.rx,
        ry=arc.ry,
        exponent=arc.exponent,
        sx=arc.sx,
        sy=arc.sy,
        t0_deg=math.degrees(t_lo),
        sweep_deg=math.degrees(t_hi - t_lo),
    )
    chain = fit_ellipse_arc(inner, tol_inches=tol_inches, max_arcs=max_arcs)
    if chain is not None:
        segments = _assemble_caps(arc, chain)
        if _contour_deviation(arc, segments) <= tol_inches:
            return segments
        # The dense pass caught a chain the 32-sample verifier under-read:
        # refit once at half the tolerance, which drives both verifiers down.
        chain = fit_ellipse_arc(inner, tol_inches=tol_inches / 2.0, max_arcs=max_arcs)
        if chain is not None:
            segments = _assemble_caps(arc, chain)
            if _contour_deviation(arc, segments) <= tol_inches:
                return segments
    return None


def _assemble_caps(
    arc: SuperellipseArc, chain: Tuple[FittedSegment, ...]
) -> Tuple[FittedSegment, ...]:
    """Wrap a fitted inner chain in its vertex-cap lines (tip-to-tail).

    Caps shorter than a float epsilon are skipped (the trim landed exactly on
    the vertex); sub-plotter-unit caps are KEPT -- they emit as the parser's
    zero-length ``PD`` plunge form ahead of the chain's first arc, so the
    contour stays tip-to-tail from the exact vertex.
    """
    segments: List[FittedSegment] = []
    first = chain[0]
    if abs(first.start - arc.start) > 1e-15:
        segments.append(FittedSegment(start=arc.start, end=first.start))
    segments.extend(chain)
    last = chain[-1]
    if abs(arc.end - last.end) > 1e-15:
        segments.append(FittedSegment(start=last.end, end=arc.end))
    return tuple(segments)
