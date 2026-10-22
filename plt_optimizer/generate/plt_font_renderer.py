"""Arc-native rendering of PLT-extracted fonts (``fonts/VP-EL-fonts/catalog/``).

PLT-extracted fonts store every glyph as baseline-normalized HPGL
(``PU``/``PD``/``AA``) in the **library design frame** (1000 units per design
inch -- see :data:`_DESIGN_UNITS_PER_INCH`), ``+y`` up,
baseline at ``y = 0`` and left edge at ``x = 0``, with the font's
reference character exactly 1000 design units tall. Each glyph also carries its
bounding box and left/right profile envelopes (silhouette samples). This
module lays a whole text line out by walking a cursor glyph-by-glyph and
returns a :class:`~plt_optimizer.generate.text_geometry.TextBlock`: arcs
stay arcs end-to-end (no flattening), so the finished cut file keeps the
native ``AA`` commands of the source font.

Layout contract:

- Glyphs are upright in the label-local render frame (``+y`` up), every
  glyph anchored on its baseline at ``y = 0`` (descenders -- ``g``, ``p``,
  ``_`` -- extend below it automatically), the first glyph's left edge at
  ``x = 0``, and the reference character's height equals
  ``target_height_inches`` exactly (uniform scale
  ``target_height_inches / normalized_ref_height``, so relative glyph
  sizes -- cap height vs. x-height -- are preserved).
- Adjacent glyphs inside a word are kerned with their profile envelopes
  through a *windowed* comparison controlled by
  ``kerning_window_fraction`` (``[0, 1]``, a fraction of the text height;
  default ``0.05``). With ``w = 0.5 * kerning_window_fraction * height``
  and ``p(y)`` the horizontal penetration of the left glyph's right
  silhouette into the right glyph's left silhouette at height ``y``:

  1. each sample ``p(y)`` on the left glyph compares against the deepest
     right-glyph silhouette sample within ``|y' - y| <= w``, so staggered
     pokes (the glyphs approach each other at slightly different heights)
     are still detected; and
  2. the effective penetration is the maximum of those windowed
     penetrations -- the window can only widen the advance relative to
     same-height kerning, never narrow it.

  The origin-to-origin advance is the (scaled) effective penetration plus
  the clearance::

      advance = max(penetration * kerning_penetration_scale,
                    min(left_glyph_width, min_glyph_width_fraction * height))
              + cutter_diameter + character_spacing
              + kerning_min_gap_fraction * height

  so a fully-close pair keeps exactly that much air at its tightest
  sampled height. A *recessed* (negative) penetration -- the silhouettes
  never meet at any sampled height -- is multiplied by
  ``kerning_recession_scale`` (default ``1.0``) instead, so lowering
  ``kerning_penetration_scale`` tightens the overlapping pairs without
  pulling the gapped pairs closer. The ``min_glyph_width_fraction * height``
  floor clamps
  the advance from below, capped by the left glyph's own width, which also
  keeps the advance non-negative.
  Sampling spans the pair's overlapping height range
  *extended by* ``w`` on both sides (clamped to the union of the two
  bounding boxes), so a poke just outside the overlap is compared against
  the opposing silhouette's nearest material instead of being missed.
  ``kerning_penetration_scale`` (default ``1.0``) over- or under-weights
  detected closeness; ``kerning_min_gap_fraction`` (a cap-height fraction,
  default ``0.0``) adds uniform extra air to every kerned pair.
  ``kerning_window_fraction = 0.0`` reproduces the historical same-height
  maximum-penetration kerning exactly (with the other two at their
  defaults). A global minimum glyph width (``min_glyph_width_fraction``, a
  cap-height fraction, default ``0.0``) floors the advance, capped by the
  left glyph's own bounding-box width, so a thin glyph's floor can never
  inflate a pair past its own silhouette; a zero-width glyph (``!``, ``|``)
  therefore floors at ``0.0`` and relies on the clearance term for its
  stroke-to-stroke air.
  Glyph pairs without overlapping height (or lacking envelopes) fall back
  to the left glyph's bounding-box width, floored by
  ``min_glyph_width_fraction * height``
  and scaled by ``fallback_advance_fraction`` (default ``1.0``).
- A space character carries no glyph::

      space_advance = space_width_fraction * height + character_spacing

  measured from the previous glyph's right edge; no kerning crosses a
  space.
- A character the font does not define raises :class:`PltFontRenderError`
  at render time (never a silent skip or tofu box).
- **Small caps** (a ``font_name`` ending in `` SC``, e.g. ``"Dino SC"`` --
  a synthetic variant of the base font, see
  :mod:`plt_optimizer.generate.font_registry`): the base font's uppercase
  glyphs engrave verbatim at full size while ASCII lowercase ``a-z``
  renders as its **uppercase glyph uniformly downscaled** by
  ``lowercase_scale`` (the cascaded ``sc_scale``), **top-down**: the glyph's
  own bottom stays exactly in place (it sits on the baseline like a shrunken
  capital) and its top descends. The scale applies to the glyph geometry
  (block, bounding box and both envelopes) *before* kerning, so the small
  silhouette kerns against its neighbours exactly; the transform is a
  uniform scale plus a translation, so the native ``AA`` arcs stay native.
  Caseless characters (digits, punctuation, derived symbols) engrave at
  full size, and the map is strictly 1:1 per character, so the word/glyph
  stroke partitions stay exact.
- **Inline small caps** (the ``<sc>`` markup tag): the same transform,
  selected **per character** through ``lowercase_flags`` (the companion to
  ``lowercase_scale``, mirroring ``italic_tan`` + ``italic_flags``). Only the
  flagged ASCII lowercase shrink; the pipeline renders the tag inert on a
  `` SC`` font (the line is already small caps) and on a symbol font.

The module is matplotlib-free (core parser + text_geometry only).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from plt_optimizer.core.parser import PLTParser
from plt_optimizer.generate.font_registry import (
    FontNotFoundError,
    PltGlyphEntry,
    base_font_name,
    load_plt_fonts,
    resolve_font,
)
from plt_optimizer.generate.text_geometry import Stroke, TextBlock, block_from_parser_paths

logger = logging.getLogger(__name__)

# The characters a small-caps font transforms: ASCII lowercase only (the
# user-facing contract). Accented lowercase, eszett, digits, punctuation
# and the derived Unicode symbols engrave verbatim at full size, and the
# map is strictly 1:1 per character, keeping the word/glyph stroke
# partitions exact.
_ASCII_LOWER: frozenset[str] = frozenset("abcdefghijklmnopqrstuvwxyz")

# Fallback space advance as a fraction of the rendered text height (added
# to ``character_spacing``) when the caller does not cascade an explicit
# ``space_width_fraction``. A space carries no glyph and no kerning.
SPACE_HEIGHT_FRACTION: float = 0.3

# Fallback global minimum glyph width as a fraction of the rendered cap
# height, used when the caller does
# not cascade an explicit ``min_glyph_width_fraction``. ``0.0`` = pure profile
# envelope kerning.
MIN_GLYPH_WIDTH_FRACTION: float = 0.0

# Fallback kerning window as a fraction of the rendered text height,
# used when the caller does not cascade an explicit
# ``kerning_window_fraction``. Each envelope sample compares against the
# opposite silhouette within +/- (half of this fraction) of the text
# height and the effective penetration is the worst (maximum) windowed
# penetration, so staggered pokes can only widen the advance.
# ``0.0`` reproduces the historical same-height kerning exactly.
KERNING_WINDOW_FRACTION: float = 0.05

# Fallback multiplier on the detected windowed penetration, used when the
# caller does not cascade an explicit ``kerning_penetration_scale``.
# ``1.0`` = the geometric penetration; larger values over-kern tight pairs
# proportionally (a detected 0.2" poke costs 0.3" of advance at 1.5).
KERNING_PENETRATION_SCALE: float = 1.0

# Fallback multiplier on a *recessed* (negative) detected penetration, used
# when the caller does not cascade an explicit ``kerning_recession_scale``.
# ``1.0`` = the geometric recession (the historical linear behaviour, where
# lowering ``kerning_penetration_scale`` also pulled gapped pairs closer);
# ``0.0`` makes a recessed pair advance by nothing beyond the floor, so the
# penetration dial only ever tightens pairs whose silhouettes overlap.
KERNING_RECESSION_SCALE: float = 1.0

# Fallback extra air added to every kerned pair advance (on top
# of the cutter + character-spacing clearance), as a fraction of the
# rendered cap height, used when the caller does
# not cascade an explicit ``kerning_min_gap_fraction``. ``0.0`` = no extra gap.
KERNING_MIN_GAP_FRACTION: float = 0.0

# Fallback multiplier on the bounding-box-width fallback advance (pairs
# without overlapping height, or lacking envelopes), used when the caller
# does not cascade an explicit ``fallback_advance_fraction``.
# ``1.0`` = the left glyph's own width.
FALLBACK_ADVANCE_FRACTION: float = 1.0

# Glyphs are stored in the library design frame: 1000 units per *design* inch.
#
# This is deliberately NOT the device plotter scale
# (``plt_optimizer.core.units.HPGL_UNITS_PER_INCH`` = 1016). The library is
# self-normalizing: ``extract_plt_fonts`` stores ``raw * (1000 / H_ref_raw)``
# and the renderer divides back out here before scaling to the requested text
# height, so the two constants cancel and the emitted geometry lands on the
# device scale via ``TextBlock.to_hpgl()``. Keep it at 1000 to match
# ``LIBRARY_UNITS_PER_INCH`` in ``fonts/extract_plt_fonts.py`` and the stored
# catalog files -- changing only one of them rescales every glyph.
_DESIGN_UNITS_PER_INCH: float = 1000.0


class PltFontRenderError(ValueError):
    """Raised when a PLT-extracted font cannot render a requested string.

    Subclasses :class:`ValueError` so callers guarding the pipeline with
    ``except ValueError`` keep working. The label renderer maps this onto
    a :class:`~plt_optimizer.generate.label_renderer.LabelRenderError`.
    """


@dataclass(frozen=True)
class _GlyphGeometry:
    """One glyph parsed into design-inch geometry (cached per font/char).

    Attributes:
        block: Parsed glyph strokes in design inches (1000 design units = 1.0),
            +y up, baseline at y=0, left edge at x=0 (the storage frame,
            unit-converted only; arc sweeps verbatim).
        bounding_box: ``(min_x, min_y, max_x, max_y)`` in design inches;
            the stored bbox when present, else the parsed block's bounds.
        left_envelope: Left-silhouette samples ``((x, y), ...)`` in design
            inches, sorted by ascending y.
        right_envelope: Right-silhouette samples in the same frame/order.
    """

    block: TextBlock
    bounding_box: Tuple[float, float, float, float]
    left_envelope: Tuple[Tuple[float, float], ...]
    right_envelope: Tuple[Tuple[float, float], ...]


def _entry_bounds(entry: PltGlyphEntry, block: TextBlock) -> Tuple[float, float, float, float]:
    """Return the glyph bbox in design inches (stored bbox preferred).

    Args:
        entry: The stored character entry.
        block: The parsed glyph block (design inches).

    Returns:
        ``(min_x, min_y, max_x, max_y)``; zeros for geometry-less glyphs.
    """
    if entry.bounding_box is not None:
        min_x, min_y, max_x, max_y = entry.bounding_box
        return (
            min_x / _DESIGN_UNITS_PER_INCH,
            min_y / _DESIGN_UNITS_PER_INCH,
            max_x / _DESIGN_UNITS_PER_INCH,
            max_y / _DESIGN_UNITS_PER_INCH,
        )
    bounds = block.bounds()
    if bounds is None:
        return (0.0, 0.0, 0.0, 0.0)
    return bounds


@lru_cache(maxsize=None)
def _glyph_entry(
    library_dir_key: str,
    font_name: str,
    character: str,
) -> _GlyphGeometry:
    """Parse one stored glyph into cached design-inch geometry.

    Args:
        library_dir_key: Cache key for the font catalog directory (``""``
            selects the default ``fonts/VP-EL-fonts/catalog/``).
        font_name: Canonical PLT font name.
        character: The single character to render.

    Returns:
        The glyph geometry in the design frame (+y up, baseline y=0, left
        edge x=0; arcs preserved with sweeps verbatim -- the v2 storage is
        already upright).

    Raises:
        PltFontRenderError: If the font or character is not in the library.
    """
    fonts = load_plt_fonts(Path(library_dir_key) if library_dir_key else None)
    font_data = fonts.get(font_name)
    if font_data is None or character not in font_data.characters:
        raise PltFontRenderError(
            f"Font {font_name!r} has no glyph for character {character!r} "
            f"(U+{ord(character):04X}); remove it or choose another font."
        )
    entry = font_data.characters[character]
    if not entry.glyph.strip():
        block = TextBlock.empty()
    else:
        document = PLTParser().parse_string(entry.glyph)
        block = block_from_parser_paths(document.stroke_paths, scale=1.0 / _DESIGN_UNITS_PER_INCH)
    return _GlyphGeometry(
        block=block,
        bounding_box=_entry_bounds(entry, block),
        left_envelope=tuple(
            (x / _DESIGN_UNITS_PER_INCH, y / _DESIGN_UNITS_PER_INCH) for x, y in entry.left_envelope
        ),
        right_envelope=tuple(
            (x / _DESIGN_UNITS_PER_INCH, y / _DESIGN_UNITS_PER_INCH)
            for x, y in entry.right_envelope
        ),
    )


def clear_glyph_cache() -> None:
    """Drop the cached glyph parses (used when the font library changes)."""
    _glyph_entry.cache_clear()


def interpolate_envelope(envelope: Sequence[Tuple[float, float]], y: float) -> Optional[float]:
    """Interpolate an envelope's x position at height ``y`` (linear, clamped).

    Args:
        envelope: Sample points ``((x, y), ...)`` sorted by ascending y.
        y: Height at which to evaluate the silhouette.

    Returns:
        The interpolated x, clamped to the first/last sample outside the
        envelope's sampled range; ``None`` when the envelope is empty.
    """
    if not envelope:
        return None
    if y <= envelope[0][1]:
        return envelope[0][0]
    if y >= envelope[-1][1]:
        return envelope[-1][0]
    for (x0, y0), (x1, y1) in zip(envelope, envelope[1:]):
        if y0 <= y <= y1:
            if y1 == y0:
                return x0
            t = (y - y0) / (y1 - y0)
            return x0 + t * (x1 - x0)
    return envelope[-1][0]  # pragma: no cover - unreachable for sorted input


def _kerning_sample_heights(
    left: Sequence[Tuple[float, float]],
    right: Sequence[Tuple[float, float]],
    y_low: float,
    y_high: float,
) -> List[float]:
    """Return the sorted sample heights used to evaluate a kerning pair.

    The union of both envelopes' sample heights inside the sampling
    range, plus the range endpoints themselves (so a silhouette extremum
    that falls between samples is still measured). The caller passes the
    overlapping height range extended by the kerning window (clamped to
    the union of the two bounding boxes), so a poke just outside the
    overlap is still compared.

    Args:
        left: Left glyph's right-envelope samples (design inches).
        right: Right glyph's left-envelope samples (design inches).
        y_low: Lower bound of the sampling range.
        y_high: Upper bound of the sampling range.

    Returns:
        Sorted list of heights within ``[y_low, y_high]``.
    """
    heights = {y_low, y_high}
    heights.update(y for _x, y in left if y_low <= y <= y_high)
    heights.update(y for _x, y in right if y_low <= y <= y_high)
    return sorted(heights)


def kerning_offset(
    left: _GlyphGeometry,
    right: _GlyphGeometry,
    min_glyph_width_design: float = 0.0,
    window_design: float = 0.0,
    penetration_scale: float = 1.0,
    recession_scale: float = 1.0,
    fallback_fraction: float = 1.0,
) -> float:
    """Design-unit origin-to-origin offset between two adjacent glyphs.

    Windowed profile-envelope kerning. Over the pair's overlapping height
    range -- extended by ``window_design`` on both sides and clamped to
    the union of the two bounding boxes, so a poke just outside the
    overlap is compared against the opposing silhouette's nearest
    material -- each sample of the left glyph's right silhouette is
    compared against the *deepest* sample of the right glyph's left
    silhouette within ``window_design`` of its height, so staggered pokes
    -- the glyphs approaching each other at slightly different heights --
    are detected too. The effective penetration is the maximum of those
    windowed penetrations (a max of maxes): the window can only widen the
    advance relative to same-height kerning, never narrow it.

    Positive penetrations (the silhouettes overlap) are multiplied by
    ``penetration_scale``; negative ones (the silhouettes are recessed
    from each other) by ``recession_scale``, so tightening the tight
    pairs never pulls the gapped pairs closer. The clearance is NOT
    included -- the caller adds it in the output frame.

    ``min_glyph_width_design`` then floors the advance so a glyph reserves
    real air, capped by the left glyph's own bounding-box width: a thin
    glyph's floor can never inflate a pair past the left glyph's own
    silhouette (the historical per-sample silhouette clamp did exactly
    that, which made every thin-left-glyph pair insensitive to
    ``penetration_scale``). Because the floor is non-negative, the advance
    is always non-negative too.

    Args:
        left: The earlier (left) glyph geometry.
        right: The following (right) glyph geometry.
        min_glyph_width_design: Global minimum advance width in design
            units (``0.0`` = pure envelope kerning). Floors the advance,
            capped by the left glyph's own width.
        window_design: Half-width of the comparison window in design
            units (``0.0`` = historical same-height maximum
            penetration).
        penetration_scale: Multiplier on a positive (overlapping)
            detected penetration (``1.0`` = geometric).
        recession_scale: Multiplier on a negative (recessed) detected
            penetration (``1.0`` = geometric; ``0.0`` = a recessed pair
            advances by nothing beyond the floor).
        fallback_fraction: Multiplier on the bounding-box-width fallback
            used when the pair has no overlapping height (or lacks
            envelopes).

    Returns:
        Offset in design units (multiply by the render scale, then add
        the inch clearance).
    """
    left_box = left.bounding_box
    right_box = right.bounding_box
    left_width = left_box[2] - left_box[0]
    # A glyph reserves at most its own width of air: capping the floor by
    # the left glyph's width keeps a thin glyph from inflating the pair
    # past its real silhouette.
    advance_floor = min(left_width, min_glyph_width_design)
    y_low = max(left_box[1], right_box[1])
    y_high = min(left_box[3], right_box[3])
    if y_high >= y_low and left.right_envelope and right.left_envelope:
        # Extend the sampling range by the window so edge pokes (just
        # outside the overlap) compare against the opposing silhouette's
        # nearest material; clamp to the union of the two bounding boxes.
        s_low = max(y_low - window_design, min(left_box[1], right_box[1]))
        s_high = min(y_high + window_design, max(left_box[3], right_box[3]))
        ys: List[float] = []
        right_xs: List[float] = []
        left_xs: List[float] = []
        for y in _kerning_sample_heights(left.right_envelope, right.left_envelope, s_low, s_high):
            right_x = interpolate_envelope(left.right_envelope, y)
            left_x = interpolate_envelope(right.left_envelope, y)
            if right_x is None or left_x is None:  # pragma: no cover - guards
                continue
            ys.append(y)
            right_xs.append(right_x)
            left_xs.append(left_x)
        if right_xs:
            # Stage 1: deepest opposing silhouette within +/- window of
            # each sample height (a two-pointer min filter over the
            # ascending ys). With window == 0 this is the same-y sample.
            penetrations: List[float] = []
            lo = 0
            for k, (y_k, right_x) in enumerate(zip(ys, right_xs)):
                while ys[lo] < y_k - window_design:
                    lo += 1
                hi = k
                while hi + 1 < len(ys) and ys[hi + 1] <= y_k + window_design:
                    hi += 1
                deepest = min(left_xs[lo : hi + 1])
                penetrations.append(right_x - deepest)
            # Stage 2: the worst windowed penetration. Each sample's
            # window contains itself, so the max of the per-sample maxes
            # is simply the global maximum of the stage-1 profile.
            penetration = max(penetrations)
            scaled = (
                penetration * penetration_scale
                if penetration >= 0.0
                else penetration * recession_scale
            )
            return max(scaled, advance_floor)
    # No overlapping height (or missing envelopes): fall back to the left
    # glyph's bounding-box width, floored by the minimum width and scaled
    # by the fallback fraction.
    return max(left_width, min_glyph_width_design) * fallback_fraction


def _resolve_plt_font(
    font_name: str,
    library_dir: Optional[Path],
    fonts_dir: Optional[Path],
) -> str:
    """Resolve ``font_name`` and require the PLT-extracted family.

    Args:
        font_name: Requested font name (canonical or not). A ``"<base> SC"``
            small-caps variant resolves to the base font's PLT family (the
            variant is a render-time transform, never a stored library key).
        library_dir: Optional PLT font catalog directory override.
        fonts_dir: Optional Fonts root override (TTF discovery).

    Returns:
        The canonical PLT font name (``"Dino"`` for base requests, ``"Dino
        SC"`` for a small-caps variant -- the caller strips it via
        :func:`~plt_optimizer.generate.font_registry.base_font_name` before
        indexing the library, and renders lowercase glyphs downscaled).

    Raises:
        PltFontRenderError: If the name is unknown (registry error
            re-raised as a render error) or resolves to a TrueType font.
    """
    try:
        ref = resolve_font(font_name, library_dir=library_dir, fonts_dir=fonts_dir)
    except FontNotFoundError as exc:
        raise PltFontRenderError(str(exc)) from exc
    if ref.kind != "plt":
        raise PltFontRenderError(
            f"Font {ref.name!r} is a TrueType font; the PLT renderer only "
            "renders PLT-extracted fonts (the TTF path renders it instead)."
        )
    return ref.name


def _small_caps_source(char: str) -> str:
    """Return the glyph-source character for ``char`` under small caps.

    ASCII lowercase maps to its uppercase counterpart (the glyph the small
    glyph must be drawn from); every other character maps to itself, so the
    map is 1:1 and the per-character stroke partitions stay exact.

    Args:
        char: The text character being rendered.

    Returns:
        The character whose glyph to draw.
    """
    return char.upper() if char in _ASCII_LOWER else char


def _scale_geometry(geometry: _GlyphGeometry, factor: float) -> _GlyphGeometry:
    """Return ``geometry`` uniformly scaled **top-down** about its own bottom.

    The transform is ``y' = y_min + (y - y_min) * factor`` with
    ``x' = x * factor`` (``y_min`` the glyph's lowest ink), so the glyph's
    bottom stays exactly where it is and its top descends -- the small glyph
    sits on the baseline like a shrunken capital, and a round glyph's
    sub-baseline overshoot is preserved rather than lifted. The map is a
    uniform scale composed with a pure translation, so ``AA`` arcs stay
    native circular arcs (sweeps untouched).

    The block, bounding box and both profile envelopes transform together,
    so kerning measures the *scaled* silhouette in the shared baseline
    frame.

    Args:
        geometry: Cached glyph geometry in design units.
        factor: Uniform scale factor (``1.0`` returns ``geometry`` itself).

    Returns:
        The scaled geometry, or ``geometry`` unchanged at ``1.0``.
    """
    if factor == 1.0:
        return geometry
    min_x, min_y, max_x, max_y = geometry.bounding_box
    # Pure Y translation carrying the origin-scaled glyph down onto its own
    # bottom: y_min * factor + shift == y_min.
    shift_y = min_y * (1.0 - factor)

    def _point(x: float, y: float) -> Tuple[float, float]:
        return (x * factor, y * factor + shift_y)

    return _GlyphGeometry(
        block=geometry.block.scaled(factor).translate(0.0, shift_y),
        bounding_box=(*_point(min_x, min_y), *_point(max_x, max_y)),
        left_envelope=tuple(_point(x, y) for x, y in geometry.left_envelope),
        right_envelope=tuple(_point(x, y) for x, y in geometry.right_envelope),
    )


def _shear_geometry(geometry: _GlyphGeometry, tan: float) -> _GlyphGeometry:
    """Return ``geometry`` sheared about the baseline ``y = 0`` (italic).

    The transform is ``x' = x + y * tan``, ``y' = y`` -- the classic oblique:
    the baseline (and every point on it) stays exactly in place while the
    glyph leans right. Applied to the block, bounding box **and both profile
    envelopes** together, so kerning measures the *slanted* silhouette in the
    shared baseline frame: an italic glyph's overhang to the right of its
    neighbour widens the pair's advance automatically, with no fudge factor.

    Arcs flatten inside :meth:`~plt_optimizer.generate.text_geometry.TextBlock.sheared`
    (a sheared circular arc is a rotated ellipse), at the standard chord
    tolerance.

    Args:
        geometry: Cached glyph geometry in design units.
        tan: Shear factor ``tan(angle)``; ``0.0`` returns ``geometry`` itself.

    Returns:
        The sheared geometry, or ``geometry`` unchanged at ``0.0``.
    """
    if tan == 0.0:
        return geometry
    min_x, min_y, max_x, max_y = geometry.bounding_box

    def _point(x: float, y: float) -> Tuple[float, float]:
        return (x + y * tan, y)

    # The shear is monotone in x for fixed y, so the new x extremes come from
    # the bbox's two y-extremes.
    new_min_x = min(min_x + min_y * tan, min_x + max_y * tan)
    new_max_x = max(max_x + min_y * tan, max_x + max_y * tan)

    return _GlyphGeometry(
        block=geometry.block.sheared(tan),
        bounding_box=(new_min_x, min_y, new_max_x, max_y),
        left_envelope=tuple(_point(x, y) for x, y in geometry.left_envelope),
        right_envelope=tuple(_point(x, y) for x, y in geometry.right_envelope),
    )


def _shift_geometry(geometry: _GlyphGeometry, shift: float) -> _GlyphGeometry:
    """Return ``geometry`` translated straight up (positive) or down (negative).

    The transform is ``x' = x``, ``y' = y + shift`` -- the ``<sup>``/``<sub>``
    baseline shift (and the ``<b>``/``<scale:N>`` engraved-baseline alignment
    raise): a **pure translate** applied to the block, bounding box **and both
    profile envelopes** together, so kerning measures the *shifted* silhouette
    in the shared baseline frame. A vertical translate changes no envelope X,
    so the pair's penetration is measured at the glyph's true engraved height
    (the stagger between a shifted glyph and its neighbours is exact): a raise
    can narrow a pair (a superscript clears the neighbour's widest bulge) or
    widen it (a glyph that rides past its neighbour's top overlaps only a
    sliver), and a shift that fully separates the boxes falls to the fallback
    advance instead of sampling the wrong stagger.

    Because the transform is a pure translate, ``AA`` arcs stay native and the
    stroke/glyph partition is untouched (the same guarantee the placement
    translate gives).

    Args:
        geometry: Cached glyph geometry in design units.
        shift: Vertical translate in design units (``0.0`` returns ``geometry``
            itself).

    Returns:
        The shifted geometry, or ``geometry`` unchanged at ``0.0``.
    """
    if shift == 0.0:
        return geometry
    min_x, min_y, max_x, max_y = geometry.bounding_box

    def _point(x: float, y: float) -> Tuple[float, float]:
        return (x, y + shift)

    return _GlyphGeometry(
        block=geometry.block.translate(0.0, shift),
        bounding_box=(min_x, min_y + shift, max_x, max_y + shift),
        left_envelope=tuple(_point(x, y) for x, y in geometry.left_envelope),
        right_envelope=tuple(_point(x, y) for x, y in geometry.right_envelope),
    )


def _scale_baseline_geometry(geometry: _GlyphGeometry, factor: float) -> _GlyphGeometry:
    """Return ``geometry`` uniformly scaled about the **baseline** ``y = 0``.

    The transform is ``x' = x * factor``, ``y' = y * factor`` -- the bold
    markup's centerline contract: a thicker tool engraves a fatter stroke, so
    the glyph's centerlines shrink toward the baseline to keep the engraved
    silhouette inside the box the plain glyph occupies. The baseline (and
    every point on it) stays exactly in place, so a bold glyph sits on the
    same baseline as its plain neighbours and its sub-baseline overshoot
    shrinks with it. The map is a pure uniform scale, so ``AA`` arcs stay
    native circular arcs (sweeps untouched) -- the same guarantee
    :func:`_scale_geometry` gives for small caps.

    The block, bounding box and both profile envelopes transform together,
    so kerning measures the *scaled* silhouette in the shared baseline frame.

    Args:
        geometry: Cached glyph geometry in design units.
        factor: Uniform scale factor (``1.0`` returns ``geometry`` itself).

    Returns:
        The scaled geometry, or ``geometry`` unchanged at ``1.0``.
    """
    if factor == 1.0:
        return geometry
    min_x, min_y, max_x, max_y = geometry.bounding_box

    def _point(x: float, y: float) -> Tuple[float, float]:
        return (x * factor, y * factor)

    return _GlyphGeometry(
        block=geometry.block.scaled(factor),
        bounding_box=(*_point(min_x, min_y), *_point(max_x, max_y)),
        left_envelope=tuple(_point(x, y) for x, y in geometry.left_envelope),
        right_envelope=tuple(_point(x, y) for x, y in geometry.right_envelope),
    )


def _capline_geometry(geometry: _GlyphGeometry, cap: float) -> _GlyphGeometry:
    """Return ``geometry`` with its ink clamped down to the capline ``cap``.

    The cap-line enforcement's top-down vertical downscale: the transform is
    ``y' = min_y + (y - min_y) * factor`` with ``x' = x`` (``min_y`` the
    glyph's lowest ink, ``factor = (cap - min_y) / (max_y - min_y)``), so the
    glyph's bottom stays exactly where it is and its top descends onto the
    capline. The scale is **vertical only** -- the aspect ratio changes (X is
    untouched), the deliberate contract of ``enforce_cap_line``. A glyph
    whose top already sits at or below ``cap`` (and a degenerate zero-height
    glyph) is returned unchanged.

    Arcs flatten inside :meth:`~plt_optimizer.generate.text_geometry.TextBlock.scaled_y`
    (a non-uniform scale maps a circular arc to an ellipse), at the standard
    chord tolerance -- the ``scale_y`` policy (a routine render-time
    transform, DEBUG log), not the :meth:`compress_x` WARNING.

    The block, bounding box and both profile envelopes transform together,
    so kerning measures the *clamped* silhouette in the shared baseline
    frame.

    Args:
        geometry: Cached glyph geometry in design units.
        cap: Capline height in the same design frame (the font's
            ``normalized_ref_height``; the reference character is exactly
            this tall).

    Returns:
        The clamped geometry, or ``geometry`` unchanged when it does not
        overshoot.
    """
    min_x, min_y, max_x, max_y = geometry.bounding_box
    if max_y <= cap or max_y <= min_y:
        return geometry
    factor = max((cap - min_y) / (max_y - min_y), 0.0)

    def _point(x: float, y: float) -> Tuple[float, float]:
        return (x, min_y + (y - min_y) * factor)

    # Pure Y translation carrying the baseline-scaled glyph onto its own
    # bottom: (y - min_y) * factor + min_y keeps min_y exactly in place.
    return _GlyphGeometry(
        block=geometry.block.translate(0.0, -min_y).scaled_y(factor).translate(0.0, min_y),
        bounding_box=(min_x, min_y, max_x, min_y + (max_y - min_y) * factor),
        left_envelope=tuple(_point(x, y) for x, y in geometry.left_envelope),
        right_envelope=tuple(_point(x, y) for x, y in geometry.right_envelope),
    )


def _walk_line(
    text: str,
    target_height_inches: float,
    font_name: str,
    cutter_diameter: float = 0.0,
    character_spacing: float = 0.0,
    space_width_fraction: Optional[float] = None,
    min_glyph_width_fraction: Optional[float] = None,
    kerning_window_fraction: Optional[float] = None,
    kerning_penetration_scale: Optional[float] = None,
    kerning_recession_scale: Optional[float] = None,
    kerning_min_gap_fraction: Optional[float] = None,
    fallback_advance_fraction: Optional[float] = None,
    library_dir: Optional[Path] = None,
    fonts_dir: Optional[Path] = None,
    *,
    lowercase_scale: Optional[float] = None,
    lowercase_flags: Optional[Sequence[bool]] = None,
    italic_tan: Optional[float] = None,
    italic_flags: Optional[Sequence[bool]] = None,
    cutter_diameters: Optional[Sequence[float]] = None,
    glyph_scales: Optional[Sequence[float]] = None,
    baseline_shifts: Optional[Sequence[float]] = None,
    font_names: Optional[Sequence[Optional[str]]] = None,
    kern_shifts: Optional[Sequence[float]] = None,
    enforce_cap_line: bool = False,
) -> Tuple[
    TextBlock,
    List[Tuple[str, List[int]]],
    List[Tuple[str, List[int]]],
]:
    """Render a PLT-extracted font line, partitioned by word and by glyph.

    The cursor walk builds each word's strokes contiguously, so the word
    groups are exact by construction (no signature matching, no drift):
    word ``i`` owns a contiguous slice of the returned block's strokes.

    Args:
        text: The string to render.
        target_height_inches: Desired reference-character (cap) height in
            inches; the whole line scales uniformly from it.
        font_name: PLT-extracted font name (case-insensitive).
        cutter_diameter: Resolved text cutter diameter in inches; part of
            the kerning clearance so engraved strokes never bleed together.
        character_spacing: Extra user spacing between characters in inches.
        space_width_fraction: Space advance as a fraction of
            ``target_height_inches`` (``None`` falls back to
            :data:`SPACE_HEIGHT_FRACTION`).
        min_glyph_width_fraction: Global minimum glyph advance width as a
            fraction of ``target_height_inches``, flooring the kerning
            advance, capped by the left glyph's own
            width (``None`` falls back to :data:`MIN_GLYPH_WIDTH_FRACTION`;
            ``0.0`` = pure envelope kerning).
        kerning_window_fraction: Kerning window as a fraction of the
            rendered text height in ``[0, 1]`` (``None`` falls back to
            :data:`KERNING_WINDOW_FRACTION`; ``0.0`` = historical
            same-height maximum-penetration kerning).
        kerning_penetration_scale: Multiplier on the detected windowed
            penetration (``None`` falls back to
            :data:`KERNING_PENETRATION_SCALE`; ``1.0`` = geometric).
        kerning_recession_scale: Multiplier on a *recessed* (negative)
            detected penetration (``None`` falls back to
            :data:`KERNING_RECESSION_SCALE`; ``1.0`` = geometric, ``0.0``
            = a recessed pair advances by nothing beyond the floor).
        kerning_min_gap_fraction: Extra air added to every kerned pair
            advance on top of the clearance, as a fraction of
            ``target_height_inches`` (``None`` falls back to
            :data:`KERNING_MIN_GAP_FRACTION`).
        fallback_advance_fraction: Multiplier on the bounding-box-width
            fallback for pairs without overlapping height (``None`` falls
            back to :data:`FALLBACK_ADVANCE_FRACTION`).
        library_dir: Optional PLT font catalog directory override (tests).
        fonts_dir: Optional Fonts root override (tests).
        lowercase_scale: Small-caps mode for ASCII lowercase ``a-z`` (the
            cascaded ``sc_scale``): each such character draws the base
            font's **uppercase** glyph, uniformly shrunk to this fraction
            **top-down** (the glyph's own bottom stays in place, its top
            descends) and kerned as the small silhouette. ``None`` (the
            default) disables the transform entirely, so every legacy call
            site renders bit-identically; ``1.0`` keeps the uppercase map and
            only skips the shrink (the line engraves as full-height capitals).
            The caller dispatches on
            ``font_registry.resolve_font(...).small_caps``.
        lowercase_flags: Per-character small-caps selection aligned with
            ``text`` (one flag per character, spaces included -- the ``<sc>``
            markup tag); only consulted when :attr:`lowercase_scale` is set,
            mirroring :attr:`italic_flags`. ``None`` (the default) applies the
            transform to every ASCII lowercase character of the line, so every
            legacy (font-level `` SC``) call site renders bit-identically.
            A flagged character maps through :func:`_small_caps_source` and
            shrinks exactly as the font-level variant does; an unflagged one
            engraves verbatim.
        italic_tan: Italic shear factor ``tan(italic_angle)`` for italic
            markup runs: each italic character's whole glyph geometry
            (block + bbox + envelopes) is sheared about the baseline
            ``x' = x + y * tan`` **before** kerning, so the slanted
            silhouette kerns exactly and the italic overhang widens the
            pair's advance automatically. ``None`` (the default) disables
            the transform entirely, so every legacy call site renders
            bit-identically. Combine with :attr:`italic_flags` for
            per-character selection; without flags the whole line shears.
        italic_flags: Per-character italic selection aligned with ``text``
            (one flag per character, spaces included); only consulted when
            :attr:`italic_tan` is set. ``None`` shears every character.
        cutter_diameters: Per-character resolved cutter diameters in inches
            (``<b>`` bold markup: bold characters carry the line's thicker
            bold cutter, every other character the plain one), aligned with
            ``text`` (spaces included -- a space carries no ink, so its
            entry only feeds the pair clearances). Each kerned pair's
            clearance becomes ``0.5 * (diameter_left + diameter_right) +
            character_spacing + kerning_min_gap_fraction * height`` -- the
            exact engraved-
            stroke floor of the two strokes, matching the ``0.5 * (a + b)``
            collision floors elsewhere. ``None`` (the default) keeps the
            line-level :attr:`cutter_diameter` clearance for every pair, so
            every legacy call site renders bit-identically.
        glyph_scales: Per-character uniform glyph scale (``<b>`` bold: the
            ratio ``(nominal - bold_cutter) / (nominal - plain_cutter) <=
            1``; every other character ``1.0``), aligned with ``text``.
            Each scaled glyph shrinks **about its own baseline** (the
            baseline stays exactly in place, the top descends -- the
            centerline contract: a thicker tool engraves a fatter stroke,
            so the centerlines shrink to keep the engraved silhouette in its
            box) via :func:`_scale_baseline_geometry`, which keeps ``AA``
            arcs native. Applied after small caps, before italic, and the
            scaled silhouette kerns. ``None`` (the default) renders every
            glyph at ``1.0``.
        baseline_shifts: Per-character vertical baseline shift in inches
            (``<sup>``/``<sub>`` markup: the shift in cap-height units times
            the line's toolpath height, plus the ``<b>``/``<scale:N>``
            engraved-baseline alignment raise), aligned with ``text``. Each
            shifted glyph is placed ``shift`` inches above (positive) or
            below (negative) the baseline -- a **pure translate** applied to
            the geometry (block + bbox + both envelopes) as the LAST
            transform, so ``AA`` arcs stay native and the stroke/glyph
            partition is untouched. The shift is baked in **before** kerning,
            so a pair's penetration is measured at the glyph's true engraved
            height (a raised superscript kerns against its neighbour at the
            capline, not at the unshifted overlap, and a pair whose shifted
            boxes lose Y-overlap falls to the fallback advance instead of
            sampling the wrong stagger). A vertical translate changes no
            envelope X, so the shift can narrow a pair (a raise clears the
            neighbour's widest bulge) or widen it (a glyph riding past the
            neighbour's top overlaps only a sliver). ``None`` (the default)
            places every glyph on the baseline, so every legacy call site
            renders bit-identically.
        font_names: Per-character ``<f:...>`` font selection aligned with
            ``text`` (one entry per character, spaces included; ``None``
            entries engrave with the line's :attr:`font`). Each named
            character fetches its glyph geometry from the named PLT font's
            catalog entry -- every v2 library shares the design frame
            (baseline y=0, reference char exactly 1000 units), so the
            envelope kerning across a font switch is **exact**: the previous
            glyph's transformed geometry and the next glyph's transformed
            geometry kern like any same-font pair. The caller resolves the
            run cutter into :attr:`cutter_diameters` (the per-pair stroke
            floor widens the pair for a thicker run tool) and the
            centerline scale into :attr:`glyph_scales`. ``None`` (the
            default) renders every character in :attr:`font_name`, so every
            legacy call site renders bit-identically. TTF run fonts are
            handled by the caller's segment composition, never here.
        kern_shifts: Per-character **manual-kern cursor delta** in inches
            (the ``kern_pos_string`` / ``kern_neg_string`` markers, resolved
            through the cascaded ``kern_width_fraction`` and the line's cap
            height), aligned with ``text`` (spaces included -- a marker
            attaches to the next NON-SPACE character, so a space's entry is
            always ``0.0``; a marker before a space kerns the first glyph of
            the following word). The marker characters themselves never
            reach this function -- ``parse_markup`` strips them, so
            ``text``/``plain_text`` carries one entry per engraved
            character. A positive value pushes the character's placed origin
            right, a negative one pulls it left (**no clamp**: a large
            negative value lets glyphs overlap, the author's decision). The
            delta rides the cursor AFTER the pair's envelope kerning, so the
            pair kerns as if the glyphs were adjacent and the marker adds
            its exact width on top; the shifted glyph becomes the pair
            reference for the NEXT character (``previous_origin`` carries
            the shift forward), so consecutive markers accumulate.
            ``None`` (the default) applies no shift, so every legacy call
            site renders bit-identically.
        enforce_cap_line: Cap-line enforcement (``enforce_cap_line``): when
            ``True``, every ASCII alphanumeric ``[0-9a-zA-Z]`` glyph whose
            ink extends above the capline (the character's own font frame's
            ``normalized_ref_height``) is downscaled **top-down** about its
            own bottom via :func:`_capline_geometry` -- a vertical-only
            scale, so the aspect ratio changes -- so its top lands exactly
            on the capline. The clamp is strictly **pre-markup**: it applies
            to the raw glyph, before the small-caps, bold/scale and italic
            transforms below. Non-alphanumeric characters (punctuation,
            spaces, derived Unicode symbols) are never clamped; the
            reference character sits exactly at the capline and never moves.
            ``False`` (the default) disables the transform entirely, so
            every legacy call site renders bit-identically.

    Returns:
        ``(block, word_groups, glyph_groups)`` where ``block`` is the
        positioned :class:`TextBlock` (first glyph's left edge at x=0,
        every glyph anchored on its baseline at y=0 -- descenders reach
        below it -- and the reference character exactly
        ``target_height_inches`` tall for non-blank text), ``word_groups``
        lists one ``(word_text, stroke_indices)`` pair per
        whitespace-delimited segment in text order (blank segments yield
        empty index lists), and ``glyph_groups`` lists one
        ``(char, stroke_indices)`` pair per non-space character in text
        order. Both partitions are contiguous and exact by construction.
        Blank input yields an empty block and two empty lists.

    Raises:
        PltFontRenderError: If the font is unknown/not a PLT font, a
            ``font_names`` entry is unknown/not a PLT font, or a character
            has no glyph in its font.
        ValueError: If ``lowercase_flags``, ``italic_flags``,
            ``cutter_diameters``, ``glyph_scales``, ``baseline_shifts``,
            ``font_names`` or ``kern_shifts`` length differs from ``text``.
    """
    canonical = _resolve_plt_font(font_name, library_dir, fonts_dir)
    if not text:
        return TextBlock.empty(), [], []
    if lowercase_flags is not None and len(lowercase_flags) != len(text):
        raise ValueError(
            f"lowercase_flags length {len(lowercase_flags)} != text length {len(text)}"
        )
    if italic_flags is not None and len(italic_flags) != len(text):
        raise ValueError(f"italic_flags length {len(italic_flags)} != text length {len(text)}")
    if cutter_diameters is not None and len(cutter_diameters) != len(text):
        raise ValueError(
            f"cutter_diameters length {len(cutter_diameters)} != text length {len(text)}"
        )
    if glyph_scales is not None and len(glyph_scales) != len(text):
        raise ValueError(f"glyph_scales length {len(glyph_scales)} != text length {len(text)}")
    if baseline_shifts is not None and len(baseline_shifts) != len(text):
        raise ValueError(
            f"baseline_shifts length {len(baseline_shifts)} != text length {len(text)}"
        )
    if font_names is not None and len(font_names) != len(text):
        raise ValueError(f"font_names length {len(font_names)} != text length {len(text)}")
    if kern_shifts is not None and len(kern_shifts) != len(text):
        raise ValueError(f"kern_shifts length {len(kern_shifts)} != text length {len(text)}")

    library_dir_key = str(library_dir) if library_dir is not None else ""
    # The small-caps variant is a render-time transform: the glyph library
    # is keyed by the BASE font name only.
    library_name = base_font_name(canonical)
    fonts = load_plt_fonts(library_dir)
    font_data = fonts[library_name]  # resolved above; presence guaranteed
    scale = target_height_inches / font_data.normalized_ref_height
    min_gap_fraction = (
        KERNING_MIN_GAP_FRACTION if kerning_min_gap_fraction is None else kerning_min_gap_fraction
    )
    # The kerning gap is a fraction of the rendered cap height; it converts
    # to inches exactly like the space advance below.
    min_gap_inches = min_gap_fraction * target_height_inches
    space_fraction = SPACE_HEIGHT_FRACTION if space_width_fraction is None else space_width_fraction
    space_advance = space_fraction * target_height_inches + character_spacing
    min_width_fraction = (
        MIN_GLYPH_WIDTH_FRACTION if min_glyph_width_fraction is None else min_glyph_width_fraction
    )
    min_width_inches = min_width_fraction * target_height_inches
    # The kerning math runs in design units (output = design * scale), so
    # the inch floor converts by dividing through the scale.
    min_width_design = min_width_inches / scale if scale > 0 else 0.0
    window_fraction = (
        KERNING_WINDOW_FRACTION if kerning_window_fraction is None else kerning_window_fraction
    )
    # The window is a fraction of the rendered text height; in the design
    # frame the reference character is exactly ``normalized_ref_height``
    # tall (1.0 for v2 libraries), so the height fraction converts directly.
    window_design = 0.5 * window_fraction * font_data.normalized_ref_height
    penetration_scale = (
        KERNING_PENETRATION_SCALE
        if kerning_penetration_scale is None
        else kerning_penetration_scale
    )
    recession_scale = (
        KERNING_RECESSION_SCALE if kerning_recession_scale is None else kerning_recession_scale
    )
    fallback_fraction = (
        FALLBACK_ADVANCE_FRACTION
        if fallback_advance_fraction is None
        else fallback_advance_fraction
    )

    # Per-character font selection (<f:...> markup): resolve every distinct
    # run font ONCE into a render frame (library name + scale + design-unit
    # kerning window + inch->design floor). All v2 libraries share the design
    # frame (normalized_ref_height 1.0), so the frames coincide and the
    # envelope kerning across a font switch is exact; a legacy library with a
    # different ref height kerns in its own frame, scaled by the RIGHT glyph's
    # frame at placement (the pair's output scale -- documented precision
    # boundary for pre-v2 libraries, which the loader rejects today).
    frames: Dict[str, Tuple[str, float, float, float, float]] = {}
    if font_names is not None:
        for run_font in font_names:
            if run_font is None or run_font in frames:
                continue
            run_canonical = _resolve_plt_font(run_font, library_dir, fonts_dir)
            run_library = base_font_name(run_canonical)
            run_data = fonts[run_library]  # resolved above; presence guaranteed
            run_scale = target_height_inches / run_data.normalized_ref_height
            frames[run_font] = (
                run_library,
                run_scale,
                0.5 * window_fraction * run_data.normalized_ref_height,
                min_width_inches / run_scale if run_scale > 0 else 0.0,
                run_data.normalized_ref_height,
            )

    words = text.split(" ")

    # Cursor walk in the OUTPUT frame: every glyph anchors its baseline at
    # y = 0 (descenders hang below automatically) and its left edge at the
    # cursor. Adjacent glyphs inside a word advance origin-to-origin by the
    # envelope kerning offset (design units x scale) plus the clearance; a
    # space measures its own advance from the previous glyph's right edge
    # and breaks kerning. The first glyph's left edge lands at x = 0 by
    # construction (the stored left edge is x = 0).
    strokes: List[Stroke] = []
    word_groups: List[Tuple[str, List[int]]] = []
    glyph_groups: List[Tuple[str, List[int]]] = []
    cursor = 0.0
    previous: Optional[_GlyphGeometry] = None
    previous_origin = 0.0
    previous_cutter = cutter_diameter
    char_index = 0
    for word_index, word in enumerate(words):
        if word_index > 0:
            # Space: measures from the previous glyph's right edge; kerning
            # never crosses a word boundary.
            if previous is not None:
                previous_width = (previous.bounding_box[2] - previous.bounding_box[0]) * scale
                cursor = max(cursor, previous_origin + previous_width)
            cursor += space_advance
            previous = None
            char_index += 1  # the space character between words
            # Kerning breaks at the space (previous is None), so the pair
            # bookkeeping resets; the space's own cutter entry never feeds
            # a clearance (a space carries no ink).
            previous_cutter = cutter_diameter
        start_index = len(strokes)
        for char in word:
            # Small caps: ASCII lowercase draws the uppercase glyph, the
            # whole glyph geometry (block + bbox + envelopes) uniformly
            # downscaled top-down BEFORE kerning so the small silhouette
            # kerns exactly. The glyph group keeps the ORIGINAL character.
            # ``lowercase_flags`` (the inline ``<sc>`` tag) narrows the
            # selection to the flagged characters -- the same idiom the
            # italic shear uses below -- while ``None`` keeps the font-level
            # `` SC`` whole-line behaviour bit-identically.
            small_caps_on = lowercase_scale is not None and (
                lowercase_flags is None or lowercase_flags[char_index]
            )
            source = _small_caps_source(char) if small_caps_on else char
            # <f:...> markup: the character's own font frame (None = the
            # line's). The glyph fetch, the kerning window/floor conversion
            # and the placement scale all follow the frame; every v2 frame
            # coincides, so a font switch kerns exactly like a same-font pair.
            run_font = font_names[char_index] if font_names is not None else None
            if run_font is None:
                char_library = library_name
                char_scale = scale
                char_window = window_design
                char_min_width = min_width_design
                char_ref_height = font_data.normalized_ref_height
            else:
                (
                    char_library,
                    char_scale,
                    char_window,
                    char_min_width,
                    char_ref_height,
                ) = frames[run_font]
            geometry = _glyph_entry(library_dir_key, char_library, source)
            # Cap-line enforcement (strictly pre-markup): an alphanumeric
            # glyph overshooting its frame's capline is clamped top-down
            # FIRST, so every markup transform below (small caps, bold/scale,
            # italic) composes on the clamped silhouette and kerning measures
            # it. Non-alphanumerics and glyphs at/below the capline are
            # returned unchanged.
            if enforce_cap_line and source.isascii() and source.isalnum():
                geometry = _capline_geometry(geometry, char_ref_height)
            if source != char and lowercase_scale is not None:
                geometry = _scale_geometry(geometry, lowercase_scale)
            # Bold markup: the thicker-cutter glyphs shrink uniformly about
            # their baseline BEFORE kerning, so the smaller silhouette kerns
            # exactly (the centerline contract; mirrors the small-caps
            # transform, anchored on the baseline instead of the bottom).
            char_cutter = (
                cutter_diameter if cutter_diameters is None else cutter_diameters[char_index]
            )
            if glyph_scales is not None:
                scale_factor = glyph_scales[char_index]
                if scale_factor != 1.0:
                    geometry = _scale_baseline_geometry(geometry, scale_factor)
            # Italic: shear the (already small-capped) geometry about the
            # baseline BEFORE kerning, so the slanted silhouette kerns
            # exactly (mirrors the small-caps transform contract).
            if italic_tan is not None and (italic_flags is None or italic_flags[char_index]):
                geometry = _shear_geometry(geometry, italic_tan)
            # <sup>/<sub> baseline shift + the engraved-baseline alignment
            # raise: bake the shift into the geometry (block + bbox + both
            # envelopes) as the LAST transform, so kerning measures the
            # glyph's TRUE engraved height -- a raised superscript kerns
            # against its neighbour at the capline, not at the unshifted
            # overlap, and a pair whose shifted boxes lose Y-overlap falls to
            # the fallback instead of sampling the wrong stagger. A pure
            # translate changes no envelope X, so the shift can never narrow
            # a pair's advance below the geometry the unshifted pair allows.
            char_shift = 0.0 if baseline_shifts is None else baseline_shifts[char_index]
            if char_shift != 0.0 and char_scale > 0:
                geometry = _shift_geometry(geometry, char_shift / char_scale)
            char_start = len(strokes)
            if previous is not None:
                offset = kerning_offset(
                    previous,
                    geometry,
                    char_min_width,
                    char_window,
                    penetration_scale,
                    recession_scale,
                    fallback_fraction,
                )
                # Per-pair stroke floor: the two engraved strokes touch at
                # half each cutter, so a bold neighbour widens the pair.
                pair_clearance = 0.5 * (previous_cutter + char_cutter)
                cursor = (
                    previous_origin
                    + offset * char_scale
                    + pair_clearance
                    + character_spacing
                    + min_gap_inches
                )
            # Manual kerning (kern_pos_string/kern_neg_string markers): the
            # resolved cursor delta rides AFTER the pair's envelope kerning,
            # so the pair kerns as if the glyphs were adjacent and the
            # marker adds its exact width on top. No clamp (the author's
            # decision). previous_origin below carries the shift forward,
            # so consecutive markers accumulate.
            if kern_shifts is not None:
                cursor += kern_shifts[char_index]
            placed = geometry.block.scaled(char_scale).translate(cursor, 0.0)
            strokes.extend(placed.strokes)
            glyph_groups.append((char, list(range(char_start, len(strokes)))))
            previous_origin = cursor
            previous = geometry
            previous_cutter = char_cutter
            char_index += 1
        word_groups.append((word, list(range(start_index, len(strokes)))))

    return TextBlock(strokes=tuple(strokes)), word_groups, glyph_groups


def render_text_line_plt_font_with_words(
    text: str,
    target_height_inches: float,
    font_name: str,
    cutter_diameter: float = 0.0,
    character_spacing: float = 0.0,
    space_width_fraction: Optional[float] = None,
    min_glyph_width_fraction: Optional[float] = None,
    kerning_window_fraction: Optional[float] = None,
    kerning_penetration_scale: Optional[float] = None,
    kerning_recession_scale: Optional[float] = None,
    kerning_min_gap_fraction: Optional[float] = None,
    fallback_advance_fraction: Optional[float] = None,
    library_dir: Optional[Path] = None,
    fonts_dir: Optional[Path] = None,
    *,
    lowercase_scale: Optional[float] = None,
    lowercase_flags: Optional[Sequence[bool]] = None,
    italic_tan: Optional[float] = None,
    italic_flags: Optional[Sequence[bool]] = None,
    cutter_diameters: Optional[Sequence[float]] = None,
    glyph_scales: Optional[Sequence[float]] = None,
    baseline_shifts: Optional[Sequence[float]] = None,
    font_names: Optional[Sequence[Optional[str]]] = None,
    kern_shifts: Optional[Sequence[float]] = None,
    enforce_cap_line: bool = False,
) -> Tuple[TextBlock, List[Tuple[str, List[int]]]]:
    """Render a text line in a PLT-extracted font, partitioned by word.

    Thin wrapper over :func:`_walk_line` discarding glyph groups (word
    groups are exact by construction -- see there).

    Args:
        text: The string to render.
        target_height_inches: Desired reference-character (cap) height in
            inches; the whole line scales uniformly from it.
        font_name: PLT-extracted font name (case-insensitive).
        cutter_diameter: Resolved text cutter diameter in inches; part of
            the kerning clearance so engraved strokes never bleed together.
        character_spacing: Extra user spacing between characters in inches.
        space_width_fraction: Space advance as a fraction of
            ``target_height_inches`` (``None`` falls back to
            :data:`SPACE_HEIGHT_FRACTION`).
        min_glyph_width_fraction: Global minimum glyph advance width as a
            fraction of ``target_height_inches``, flooring the kerning
            advance, capped by the left glyph's own
            width (``None`` falls back to :data:`MIN_GLYPH_WIDTH_FRACTION`;
            ``0.0`` = pure envelope kerning).
        kerning_window_fraction: Kerning window as a fraction of the
            rendered text height in ``[0, 1]`` (``None`` falls back to
            :data:`KERNING_WINDOW_FRACTION`; ``0.0`` = historical
            same-height maximum-penetration kerning).
        kerning_penetration_scale: Multiplier on the detected windowed
            penetration (``None`` falls back to
            :data:`KERNING_PENETRATION_SCALE`; ``1.0`` = geometric).
        kerning_recession_scale: Multiplier on a *recessed* (negative)
            detected penetration (``None`` falls back to
            :data:`KERNING_RECESSION_SCALE`; ``1.0`` = geometric, ``0.0``
            = a recessed pair advances by nothing beyond the floor).
        kerning_min_gap_fraction: Extra air added to every kerned pair
            advance on top of the clearance, as a fraction of
            ``target_height_inches`` (``None`` falls back to
            :data:`KERNING_MIN_GAP_FRACTION`).
        fallback_advance_fraction: Multiplier on the bounding-box-width
            fallback for pairs without overlapping height (``None`` falls
            back to :data:`FALLBACK_ADVANCE_FRACTION`).
        library_dir: Optional PLT font catalog directory override (tests).
        fonts_dir: Optional Fonts root override (tests).
        lowercase_scale: Small-caps mode for ASCII lowercase ``a-z``
            (``None`` = off; see :func:`_walk_line`).
        lowercase_flags: Per-character small-caps selection for the inline
            ``<sc>`` tag (``None`` = whole line; see :func:`_walk_line`).
        italic_tan: Italic shear factor (``None`` = off; see
            :func:`_walk_line`).
        italic_flags: Per-character italic selection (``None`` = whole
            line; see :func:`_walk_line`).
        cutter_diameters: Per-character cutter diameters for ``<b>`` bold
            markup (``None`` = off; see :func:`_walk_line`).
        glyph_scales: Per-character glyph scales for ``<b>`` bold markup
            (``None`` = off; see :func:`_walk_line`).
        baseline_shifts: Per-character baseline shift in inches for
            ``<sup>``/``<sub>`` markup (``None`` = off; see
            :func:`_walk_line`).
        font_names: Per-character ``<f:...>`` font selection (``None`` =
            off; see :func:`_walk_line`).
        kern_shifts: Per-character manual-kern cursor delta in inches
            (``None`` = off; see :func:`_walk_line`).
        enforce_cap_line: Alphanumeric cap-line clamping (``False`` = off;
            see :func:`_walk_line`).

    Returns:
        ``(block, word_groups)`` with the semantics documented on
        :func:`_walk_line`.

    Raises:
        PltFontRenderError: If the font is unknown/not a PLT font, or a
            character has no glyph in the font.
    """
    block, word_groups, _glyph_groups = _walk_line(
        text,
        target_height_inches,
        font_name,
        cutter_diameter,
        character_spacing,
        space_width_fraction,
        min_glyph_width_fraction,
        kerning_window_fraction,
        kerning_penetration_scale,
        kerning_recession_scale,
        kerning_min_gap_fraction,
        fallback_advance_fraction,
        library_dir,
        fonts_dir,
        lowercase_scale=lowercase_scale,
        lowercase_flags=lowercase_flags,
        italic_tan=italic_tan,
        italic_flags=italic_flags,
        cutter_diameters=cutter_diameters,
        glyph_scales=glyph_scales,
        baseline_shifts=baseline_shifts,
        font_names=font_names,
        kern_shifts=kern_shifts,
        enforce_cap_line=enforce_cap_line,
    )
    return block, word_groups


def render_text_line_plt_font_with_glyphs(
    text: str,
    target_height_inches: float,
    font_name: str,
    cutter_diameter: float = 0.0,
    character_spacing: float = 0.0,
    space_width_fraction: Optional[float] = None,
    min_glyph_width_fraction: Optional[float] = None,
    kerning_window_fraction: Optional[float] = None,
    kerning_penetration_scale: Optional[float] = None,
    kerning_recession_scale: Optional[float] = None,
    kerning_min_gap_fraction: Optional[float] = None,
    fallback_advance_fraction: Optional[float] = None,
    library_dir: Optional[Path] = None,
    fonts_dir: Optional[Path] = None,
    *,
    lowercase_scale: Optional[float] = None,
    lowercase_flags: Optional[Sequence[bool]] = None,
    italic_tan: Optional[float] = None,
    italic_flags: Optional[Sequence[bool]] = None,
    cutter_diameters: Optional[Sequence[float]] = None,
    glyph_scales: Optional[Sequence[float]] = None,
    baseline_shifts: Optional[Sequence[float]] = None,
    font_names: Optional[Sequence[Optional[str]]] = None,
    kern_shifts: Optional[Sequence[float]] = None,
    enforce_cap_line: bool = False,
) -> Tuple[
    TextBlock,
    List[Tuple[str, List[int]]],
    List[Tuple[str, List[int]]],
]:
    """Render a text line in a PLT-extracted font, partitioned by glyph too.

    Public wrapper over :func:`_walk_line` exposing the per-glyph stroke
    partition alongside the word partition. Glyph groups are exact by
    construction: the cursor walk appends each character's strokes
    contiguously, so glyph ``k`` owns a contiguous slice of the returned
    block's strokes (spaces contribute no strokes and no group).

    Args:
        text: The string to render.
        target_height_inches: Desired reference-character (cap) height in
            inches; the whole line scales uniformly from it.
        font_name: PLT-extracted font name (case-insensitive).
        cutter_diameter: Resolved text cutter diameter in inches.
        character_spacing: Extra user spacing between characters in inches.
        space_width_fraction: Space advance fraction (``None`` = default).
        min_glyph_width_fraction: Minimum glyph advance width as a fraction
            of ``target_height_inches`` (``None`` = default).
        kerning_window_fraction: Kerning window fraction
            (``None`` = default).
        kerning_penetration_scale: Penetration multiplier
            (``None`` = default).
        kerning_recession_scale: Recession multiplier (``None`` = default).
        kerning_min_gap_fraction: Extra kerned-pair air as a fraction of
            ``target_height_inches`` (``None`` = default).
        fallback_advance_fraction: No-overlap fallback multiplier
            (``None`` = default).
        library_dir: Optional PLT font catalog directory override (tests).
        fonts_dir: Optional Fonts root override (tests).
        lowercase_scale: Small-caps mode for ASCII lowercase ``a-z``
            (``None`` = off; see :func:`_walk_line`).
        lowercase_flags: Per-character small-caps selection for the inline
            ``<sc>`` tag (``None`` = whole line; see :func:`_walk_line`).
        italic_tan: Italic shear factor (``None`` = off; see
            :func:`_walk_line`).
        italic_flags: Per-character italic selection (``None`` = whole
            line; see :func:`_walk_line`).
        cutter_diameters: Per-character cutter diameters for ``<b>`` bold
            markup (``None`` = off; see :func:`_walk_line`).
        glyph_scales: Per-character glyph scales for ``<b>`` bold markup
            (``None`` = off; see :func:`_walk_line`).
        baseline_shifts: Per-character baseline shift in inches for
            ``<sup>``/``<sub>`` markup (``None`` = off; see
            :func:`_walk_line`).
        font_names: Per-character ``<f:...>`` font selection (``None`` =
            off; see :func:`_walk_line`).
        kern_shifts: Per-character manual-kern cursor delta in inches
            (``None`` = off; see :func:`_walk_line`).
        enforce_cap_line: Alphanumeric cap-line clamping (``False`` = off;
            see :func:`_walk_line`).

    Returns:
        ``(block, word_groups, glyph_groups)`` with the semantics
        documented on :func:`_walk_line`.

    Raises:
        PltFontRenderError: If the font is unknown/not a PLT font, or a
            character has no glyph in the font.
    """
    return _walk_line(
        text,
        target_height_inches,
        font_name,
        cutter_diameter,
        character_spacing,
        space_width_fraction,
        min_glyph_width_fraction,
        kerning_window_fraction,
        kerning_penetration_scale,
        kerning_recession_scale,
        kerning_min_gap_fraction,
        fallback_advance_fraction,
        library_dir,
        fonts_dir,
        lowercase_scale=lowercase_scale,
        lowercase_flags=lowercase_flags,
        italic_tan=italic_tan,
        italic_flags=italic_flags,
        cutter_diameters=cutter_diameters,
        glyph_scales=glyph_scales,
        baseline_shifts=baseline_shifts,
        font_names=font_names,
        kern_shifts=kern_shifts,
        enforce_cap_line=enforce_cap_line,
    )


def render_text_line_plt_font(
    text: str,
    target_height_inches: float,
    font_name: str,
    cutter_diameter: float = 0.0,
    character_spacing: float = 0.0,
    space_width_fraction: Optional[float] = None,
    min_glyph_width_fraction: Optional[float] = None,
    kerning_window_fraction: Optional[float] = None,
    kerning_penetration_scale: Optional[float] = None,
    kerning_recession_scale: Optional[float] = None,
    kerning_min_gap_fraction: Optional[float] = None,
    fallback_advance_fraction: Optional[float] = None,
    library_dir: Optional[Path] = None,
    fonts_dir: Optional[Path] = None,
    *,
    lowercase_scale: Optional[float] = None,
    lowercase_flags: Optional[Sequence[bool]] = None,
    italic_tan: Optional[float] = None,
    italic_flags: Optional[Sequence[bool]] = None,
    cutter_diameters: Optional[Sequence[float]] = None,
    glyph_scales: Optional[Sequence[float]] = None,
    baseline_shifts: Optional[Sequence[float]] = None,
    font_names: Optional[Sequence[Optional[str]]] = None,
    kern_shifts: Optional[Sequence[float]] = None,
    enforce_cap_line: bool = False,
) -> TextBlock:
    """Render a text line in a PLT-extracted font (arcs preserved).

    Convenience wrapper around
    :func:`render_text_line_plt_font_with_words` discarding word groups.

    Args:
        text: The string to render.
        target_height_inches: Desired reference-character (cap) height in
            inches.
        font_name: PLT-extracted font name (case-insensitive).
        cutter_diameter: Resolved text cutter diameter in inches.
        character_spacing: Extra user spacing between characters in inches.
        space_width_fraction: Space advance as a fraction of
            ``target_height_inches`` (``None`` falls back to
            :data:`SPACE_HEIGHT_FRACTION`).
        min_glyph_width_fraction: Global minimum glyph advance width in inches
            flooring the kerning advance, capped by the left glyph's own
            width (``None`` falls back to :data:`MIN_GLYPH_WIDTH_FRACTION`).
        kerning_window_fraction: Kerning window as a fraction of the
            rendered text height in ``[0, 1]`` (``None`` falls back to
            :data:`KERNING_WINDOW_FRACTION`).
        kerning_penetration_scale: Multiplier on the detected windowed
            penetration (``None`` falls back to
            :data:`KERNING_PENETRATION_SCALE`).
        kerning_recession_scale: Multiplier on a *recessed* (negative)
            detected penetration (``None`` falls back to
            :data:`KERNING_RECESSION_SCALE`).
        kerning_min_gap_fraction: Extra air added to every kerned pair
            advance, as a fraction of ``target_height_inches``
            (``None`` falls back to :data:`KERNING_MIN_GAP_FRACTION`).
        fallback_advance_fraction: Multiplier on the bounding-box-width
            fallback (``None`` falls back to
            :data:`FALLBACK_ADVANCE_FRACTION`).
        library_dir: Optional PLT font catalog directory override (tests).
        fonts_dir: Optional Fonts root override (tests).
        lowercase_scale: Small-caps mode for ASCII lowercase ``a-z``
            (``None`` = off; see :func:`_walk_line`).
        lowercase_flags: Per-character small-caps selection for the inline
            ``<sc>`` tag (``None`` = whole line; see :func:`_walk_line`).
        italic_tan: Italic shear factor (``None`` = off; see
            :func:`_walk_line`).
        italic_flags: Per-character italic selection (``None`` = whole
            line; see :func:`_walk_line`).
        cutter_diameters: Per-character cutter diameters for ``<b>`` bold
            markup (``None`` = off; see :func:`_walk_line`).
        glyph_scales: Per-character glyph scales for ``<b>`` bold markup
            (``None`` = off; see :func:`_walk_line`).
        baseline_shifts: Per-character baseline shift in inches for
            ``<sup>``/``<sub>`` markup (``None`` = off; see
            :func:`_walk_line`).
        font_names: Per-character ``<f:...>`` font selection (``None`` =
            off; see :func:`_walk_line`).
        kern_shifts: Per-character manual-kern cursor delta in inches
            (``None`` = off; see :func:`_walk_line`).
        enforce_cap_line: Alphanumeric cap-line clamping (``False`` = off;
            see :func:`_walk_line`).

    Returns:
        The positioned :class:`TextBlock` (empty for blank input).

    Raises:
        PltFontRenderError: If the font or a character cannot render.
    """
    block, _groups = render_text_line_plt_font_with_words(
        text,
        target_height_inches,
        font_name,
        cutter_diameter=cutter_diameter,
        character_spacing=character_spacing,
        space_width_fraction=space_width_fraction,
        min_glyph_width_fraction=min_glyph_width_fraction,
        kerning_window_fraction=kerning_window_fraction,
        kerning_penetration_scale=kerning_penetration_scale,
        kerning_recession_scale=kerning_recession_scale,
        kerning_min_gap_fraction=kerning_min_gap_fraction,
        fallback_advance_fraction=fallback_advance_fraction,
        library_dir=library_dir,
        fonts_dir=fonts_dir,
        lowercase_scale=lowercase_scale,
        lowercase_flags=lowercase_flags,
        italic_tan=italic_tan,
        italic_flags=italic_flags,
        cutter_diameters=cutter_diameters,
        glyph_scales=glyph_scales,
        baseline_shifts=baseline_shifts,
        font_names=font_names,
        kern_shifts=kern_shifts,
        enforce_cap_line=enforce_cap_line,
    )
    return block
