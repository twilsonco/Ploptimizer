"""Resolution engine for flattening JobSpec into strictly typed label objects.

This module bridges the gap between the flexible, highly optional Pydantic
``JobSpec`` (which accepts many omitted fields) and the strictly typed
data structures required by downstream consumers such as the 2D bin
packer. Every dimension, margin, and spacing value is absolutely resolved
by the time a ``ResolvedLabel`` is instantiated.

Resolution order for any given parameter:
    1. ``TextLine`` (most specific)
    2. ``LabelSpec``
    3. ``JobSpec``
    4. Hardcoded fallback constant (prevents ``NoneType`` math errors)

Example:
    >>> from plt_optimizer.generate.schema import parse_yaml
    >>> from plt_optimizer.generate.resolution import resolve_job_spec
    >>> job = parse_yaml("tests_deps/sample_spec.yaml")
    >>> labels = resolve_job_spec(job)
    >>> print(labels[0].width)
    3.0
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Callable, List, Optional, Sequence, Tuple, Union

from plt_optimizer.generate.font_registry import DEFAULT_FONT_NAME, plt_font_stroke_count
from plt_optimizer.generate.markup import Run, SupSub, parse_markup, split_brackets
from plt_optimizer.generate.schema import JobSpec, LabelSpec

logger = logging.getLogger(__name__)

# Signature of the probe measuring one resolved text line's vertical ink
# extents relative to its baseline: ``(ascender, descender)``, both in inches
# and both non-negative (the distance from the baseline to the top of the
# ink, and the depth of the ink hanging below it). Baseline spacing needs
# render-time knowledge -- how far a line's glyphs reach below their baseline
# depends on the characters it contains -- so the measurement is injected:
# :func:`plt_optimizer.generate.label_renderer.measure_line_vertical_extents`
# supplies it in production, and tests inject a table. ``None`` (the default
# everywhere) keeps the historical nominal-height spacing math and costs no
# renders.
LineExtentsProbe = Callable[["ResolvedTextLine"], tuple[float, float]]


def memoize_extents_probe(probe: LineExtentsProbe) -> LineExtentsProbe:
    """Wrap an extents probe so each distinct line is measured at most once.

    A job's labels frequently share identical content (replacement-file
    expansion, ``count`` repetition), and measuring a line means rendering
    it. The cache keys on the line object itself: :class:`ResolvedTextLine`
    is a frozen dataclass with value equality, so identical typography hits the
    same entry, and spacing fitters that measure the same label twice
    (auto-spacing, then margin fitting) pay nothing.

    Args:
        probe: The probe to wrap, e.g.
            :func:`plt_optimizer.generate.label_renderer.measure_line_vertical_extents`.

    Returns:
        A probe returning cached ``(ascender, descender)`` extents.
    """
    cache: dict[ResolvedTextLine, tuple[float, float]] = {}

    def memoized(line: ResolvedTextLine) -> tuple[float, float]:
        cached = cache.get(line)
        if cached is None:
            cached = probe(line)
            cache[line] = cached
        return cached

    return memoized


class CutterSizeError(ValueError):
    """Raised when an explicit ``cutter_size`` cannot be applied to a line.

    The text toolpath height is ``text_height - cutter_size``; a cutter at
    or above the line's nominal text height leaves no material to engrave
    (a zero or negative toolpath height), so the job aborts instead of
    emitting degenerate glyphs.
    """


class LineContentConfigError(ValueError):
    """Raised when ``optimize_line_content_max_lines`` is misconfigured.

    The reflow line cap only governs an ``optimize_line_content`` group, so
    two declarations are unresolvable and abort the job rather than being
    silently ignored: a cap resolved onto a label whose lines never enable
    reflow (nothing would ever be reflowed, so the cap is meaningless), and
    a single group whose lines declare *different* caps (the group must grow
    to one agreed ceiling). Both are authoring mistakes, not runtime
    conditions, so they surface as a non-zero CLI exit.
    """


# ---------------------------------------------------------------------------
# Global fallback constants
# ---------------------------------------------------------------------------
DEFAULT_TEXT_HEIGHT: float = 0.25
# Font selected when ``font`` is unset at line/label/job level. The value
# must be a valid registry name (PLT-extracted key or Fonts/ TTF basename);
# see ``plt_optimizer.generate.font_registry``.
DEFAULT_FONT: str = DEFAULT_FONT_NAME
DEFAULT_MARGIN: float = 0.125
# Horizontal and vertical margins fall back to margin, then the default.
# Both are None to indicate "unset"; resolution logic fills them in.
DEFAULT_H_MARGIN: Optional[float] = None
DEFAULT_V_MARGIN: Optional[float] = None
DEFAULT_LINE_SPACING: Union[str, float] = "auto"
DEFAULT_HOLE_MARGIN: float = 0.1875
# Lower bound for hole-margin shrinkage during text-hole collision
# avoidance. ``None`` (the default) means collision avoidance may reduce
# the hole margin all the way to ``0.0`` (hole tangent to the edge).
DEFAULT_MIN_HOLE_MARGIN: Optional[float] = None
# Air gap (inches) kept between the engraved text stroke and the engraved
# drill-hole stroke on top of the stroke floor
# ``0.5 * (hole_cutter + text_cutter)``. At 0.0 the two cut strokes just
# touch; the default keeps 0.15in of free air between them.
DEFAULT_HOLE_TEXT_COLLISION_DISTANCE: float = 0.15
# Default cutter diameter (inches) used to cut label boundaries and drill
# holes. Collision detection uses this to compute the stroke floor; the
# effective value is snapped to the shop inventory (next size down, else
# next size up).
DEFAULT_BOUNDARY_HOLE_CUTTER: float = 0.015
# Horizontal compression is opt-in: 0.0 disables it entirely.
DEFAULT_MAX_H_COMPRESS: float = 0.0
# Space advance (PLT-extracted fonts) as a fraction of the rendered text
# height: a space advances ``DEFAULT_SPACE_WIDTH_FRACTION * text_height +
# character_spacing``.
DEFAULT_SPACE_WIDTH_FRACTION: float = 0.3
# Global minimum glyph advance width (inches) for PLT-extracted fonts.
# The profile-envelope kerning floors each pair's advance at this value,
# capped by the left glyph's own width; 0.0 means pure envelope kerning.
DEFAULT_MIN_GLYPH_WIDTH: float = 0.0
# Kerning window (fraction of the rendered text height, in [0.0, 1.0])
# for PLT-extracted fonts. Each envelope sample compares against the
# opposite silhouette within +/- (half of this fraction) of the text
# height and the effective penetration is the worst windowed
# penetration, so staggered pokes widen the advance;
# 0.0 reproduces the historical same-height maximum-penetration kerning.
DEFAULT_KERNING_WINDOW_FRACTION: float = 0.05
# Multiplier on the detected windowed penetration (PLT-extracted fonts).
# 1.0 keeps the geometric penetration; >1.0 over-kerns tight pairs.
DEFAULT_KERNING_PENETRATION_SCALE: float = 1.0
# Multiplier on a *recessed* (negative) detected penetration (PLT-extracted
# fonts). 1.0 keeps the geometric recession (the historical linear
# behaviour); 0.0 makes a recessed pair advance by nothing beyond the
# min_glyph_width floor, so the penetration dial only tightens pairs whose
# silhouettes overlap.
DEFAULT_KERNING_RECESSION_SCALE: float = 1.0
# Extra air (inches) added to every kerned pair advance on top of the
# cutter + character-spacing clearance. 0.0 = no extra gap.
DEFAULT_KERNING_MIN_GAP: float = 0.0
# Multiplier on the bounding-box-width fallback advance (no y-overlap or
# missing envelopes). 1.0 = the left glyph's own width.
DEFAULT_FALLBACK_ADVANCE_FRACTION: float = 1.0
# Downscale applied to ASCII lowercase a-z by a small-caps font variant
# (a ``font`` name ending in `` SC``): the lowercase character renders as
# the base font's uppercase glyph uniformly shrunk to this fraction of the
# cap height, top-down (the glyph's own bottom stays in place, its top
# descends). Bound to [0.2, 1.0] by the schema;
# 1.0 renders the variant identically to its base font. Inert on non-SC
# fonts. See ``plt_optimizer.generate.font_registry``.
DEFAULT_SC_SCALE: float = 0.7
# Downscale applied to a bare ``<sup>``/``<sub>`` markup run: the run's glyphs
# engrave uniformly shrunk to this fraction of the line's size, and the
# baseline shift derives from it, **centering the shrunken glyph on the line
# it belongs to** -- a superscript rises ``1 - d / 2`` cap heights (its centre
# lands exactly on the capline) and a subscript drops ``d / 2`` cap heights
# (its centre lands on the baseline). The argumented forms ``<sup:0.3>`` /
# ``<sub:-0.2>`` set the shift explicitly (cap-height units) and apply no
# scaling, so this field is inert on them. The run engraves with the cutter
# for its downscaled height (the ``<scale:N>`` ladder). Bound to [0.2, 1.0]
# by the schema. See ``plt_optimizer.generate.markup``.
DEFAULT_SUPERSUBSCRIPT_DOWNSCALE: float = 0.5
# Delimiter pair for the inline markup syntax (``<i>`` italic and future
# tags): an even-length string whose first half is the left delimiter and
# second half the right (``"<>"`` -> ``<`` / ``>``). Text that legitimately
# contains chevrons opts out by declaring a pair absent from its data; a
# bracketed body that is not a registered tag engraves verbatim. The forward
# slash is only special inside the delimiters (``</i>``), never elsewhere.
# See ``plt_optimizer.generate.markup``.
DEFAULT_HTML_BRACKETS: str = "<>"
# Slant angle (degrees) for italic markup: PLT-extracted glyphs (and TTF
# glyphs with no native italic face) are sheared about the baseline,
# ``x' = x + y * tan(angle)``, so the baseline stays fixed and the glyph
# leans right. Bound to [0, 45] by the schema; 0 = upright (shear skipped).
DEFAULT_ITALIC_ANGLE: float = 12.0
# Extra air (inches) for underline markup (``<u>``). The underline is a
# centerline stroke cut with the line's own text cutter, positioned
# ``cutter diameter + underline_offset`` below the baseline, so the engraved
# underline's top edge floats exactly ``underline_offset`` below the engraved
# baseline stroke. The break test is strictly horizontal: a glyph breaks the
# underline where its ink crosses the underline's centerline (dipping past
# ``cutter diameter + underline_offset`` below the baseline), so the offset
# alone decides which glyphs break.
# 0.0 = the underline's top edge touches the baseline stroke and gaps only
# where strokes truly cross.
# See ``plt_optimizer.generate.underline``.
DEFAULT_UNDERLINE_OFFSET: float = 0.0
# Extra air (inches) at each underline break. Each cut is padded horizontally
# by ``0.5 * cutter + underline_break_clearance`` (the half cutter is the glyph
# stroke's own half-width -- the ink is a centerline), so the engraved air
# between a descending glyph's stroke and the underline's cut edge is exactly
# this clearance (0.0 = the cut sits at the ink). Independent of the offset
# (which decides which glyphs break); this adds air at the break itself. The
# code fallback is 0.0 (the historical cut); job-config.json supplies the shop
# default (0.1).
# See ``plt_optimizer.generate.underline``.
DEFAULT_UNDERLINE_BREAK_CLEARANCE: float = 0.0
# Whether an underline (``<u>``) breaks at the spaces between words. ``False``
# (the default) draws one continuous underline across the whole underlined run
# (spaces included, broken only by descending glyphs); ``True`` ends the
# underline at each space, so every word gets its own underline spanning its
# own ink and the inter-word space stays bare. The code fallback is ``False``
# (the historical continuous underline); job-config.json supplies the shop
# default.
# See ``plt_optimizer.generate.label_renderer._underline_render_gate``.
DEFAULT_UNDERLINE_BREAK_SPACES: bool = False
# Multiplier applied to ``target_boldness_factor`` by inline ``<b>`` markup on
# a **multi-stroke (bold)** PLT font: the bold run re-runs the boldness ladder
# at ``target_boldness_factor * this`` (clamped to
# ``MAX_TARGET_BOLDNESS_FACTOR`` with a WARNING), so the run's parallel passes
# engrave heavier than the line's plain text. On single-stroke PLT and TTF
# fonts ``<b>`` instead steps to the next larger inventory cutter and this
# multiplier is inert. Bound to [1.0, 4.0] by the schema; job-config.json
# supplies the shop default. See ``AGENTS.md`` "Inline Bold (``<b>``)".
DEFAULT_BOLD_BOLDNESS_MULTIPLIER: float = 1.5
# Horizontal text alignment defaults to centering (existing behaviour).
DEFAULT_TEXT_H_ALIGNMENT: str = "center"
# Explicit cutter diameter (inches) override. ``None`` (the default) means
# unset: the cutter is auto-selected from the nominal text height via
# ``IDEAL_CUTTER_MAP`` (current behaviour). When set, the requested diameter
# is snapped to the shop inventory (next size down, else next size up) and
# the toolpath height is recomputed as ``text_height - cutter_size``.
# There is deliberately no job-config.json counterpart to this field.
DEFAULT_CUTTER_SIZE: Optional[float] = None
# Target engraved stroke width for multi-stroke (bold) PLT fonts, as a
# fraction of the nominal text height. A multi-stroke font's glyphs carry
# ``stroke_count`` parallel cutter passes (stored in the font's catalog file
# by the extractor's ``_S<n>`` file-name suffix); the automatic cutter is
# sized so the passes plus their overlaps engrave exactly
# ``text_height * factor`` of total width. Inert on single-stroke fonts,
# TTF fonts, and lines with an explicit ``cutter_size``. Bound to
# ``[0.05, 0.30]`` by the schema; job-config.json supplies the shop default.
DEFAULT_TARGET_BOLDNESS_FACTOR: float = 0.14
# Upper bound of the schema's ``target_boldness_factor`` domain. A ``<b>``
# run's boosted factor (``target_boldness_factor * bold_boldness_multiplier``)
# is clamped to this value with a WARNING, so the bold ladder never leaves its
# certified range.
MAX_TARGET_BOLDNESS_FACTOR: float = 0.30
# Floor for a degenerate ``<scale:N>`` geometry scale (a run whose engraved
# silhouette ``N * nominal - cutter`` is not positive -- only reachable on
# a line with an explicit ``cutter_size``, which the cutter step never
# touches). The centerlines collapse to 2% of the toolpath height: the run
# engraves as a near-baseline blob of cutter width, the best the tool
# allows. A WARNING names the line.
MIN_SCALED_GLYPH_FACTOR: float = 0.02
# The narrower/wider snap tolerance :func:`get_cutter_diameter` applies when
# a ``<scale:N>`` run's effective height has no exact inventory rung (the
# resolution-time default; stored on the line so the ladder re-derives
# identically in every pre-pass).
DEFAULT_CUTTER_LADDER_TOLERANCE: float = 3.0
# Minimum overlap fraction between adjacent cutter passes of a multi-stroke
# font (the gap-free floor): the derived cutter is never smaller than
# ``pass_spacing / (1 - ratio)``, so engraved strokes always overlap by at
# least this fraction and no unengraved stripe can appear between passes.
# The actual overlap is derived from the chosen cutter and logged. Bound to
# ``[0.1, 0.5]`` by the schema; job-config.json supplies the shop default.
DEFAULT_MULTISTROKE_OVERLAP_RATIO: float = 0.25
# The overlap ratio the multi-stroke sample sheets are assumed to have been
# engraved at, so the pass spacing baked into the stored glyph geometry is
# ``design_stepover * design_cutter`` with ``design_stepover = 1 - this``.
# At the default boldness factor and overlap floor the cutter ladder
# reproduces the classic formula ``W = text_height * 0.14 / (1 +
# (N - 1) * 0.75)`` exactly (the design point sits on the floor).
DESIGN_MULTISTROKE_OVERLAP: float = 0.25
# Permission for the compression-driven cutter reduction (see
# ``plt_optimizer.generate.cutter_downsize``). ``True`` (the default) lets the
# pipeline swap the *automatic* cutter for the next smaller inventory tool
# when a line is compressed past the midpoint toward it; an explicit
# ``cutter_size`` overrides the mechanism entirely.
DEFAULT_CUTTER_DOWNSIZE: bool = True
# How many successive downsizings one text line may apply (1 = at most one
# size down, 0 = disabled). Each step re-measures the line because a smaller
# cutter renders wider, which can justify a further step.
DEFAULT_MAX_CUTTER_DOWNSIZES: int = 1
# Per-label sharing of compression-driven cutter downsizes. ``True`` (the
# default) propagates a trigger line's final cutter to every other eligible
# line of the same nominal text height *within the same label* (sharing is
# always per-label; the job tier only enables the option for its labels).
DEFAULT_CUTTER_DOWNSIZE_GLOBAL: bool = True
# Per-label sharing of horizontal compression. ``False`` (the default) keeps
# the historical per-line behaviour; ``True`` propagates a trigger line's
# compression scale to every other eligible line of the same nominal text
# height *within the same label* (sharing is always per-label; the job tier
# only enables the option for its labels). The shared scale is the group's
# most-compressed (minimum) scale, clamped to each receiver's own
# ``1 - max_h_compress`` budget floor.
DEFAULT_H_COMPRESS_GLOBAL: bool = False
# Word-level line-content reflow (see ``plt_optimizer.generate.line_content``).
# ``False`` (the default) keeps the historical per-line text: lines never
# exchange words. ``True`` lets consecutive enabled lines redistribute their
# words (word order preserved, only the line breaks move) to equalize the
# group's rendered widths, minimizing horizontal compression.
DEFAULT_OPTIMIZE_LINE_CONTENT: bool = False
# Ceiling on the consecutive lines one reflow group may use. ``None`` (the
# default) is *unset*: the group reflows across its existing lines and never
# gains one. The option is growth-only -- a numeric value may ADD lines to a
# label (the group starts at its existing line count and grows while its lines
# still need horizontal compression) and never removes any. Consumed by
# :mod:`plt_optimizer.generate.line_content`.
DEFAULT_OPTIMIZE_LINE_CONTENT_MAX_LINES: Optional[int] = None
# Baseline-to-baseline line spacing. ``True`` (the default) stacks a label's
# text lines by baseline pitch (``nominal_text_height + line_spacing``) so a
# line's descenders hang *into* the gap below it and every gap on the label
# reads as one uniform visual spacing. ``False`` stacks by rendered ink box
# (the historical behaviour): a line carrying descenders pushes the next line
# down by its full ink height, inflating that one visual gap. Label-level
# only -- spacing is a property of the stacked block, never of one line, so
# the field is deliberately absent from ``TextLine``/``TextAttributes``.
DEFAULT_USE_BASELINE_SPACING: bool = True
# Vertical centering mode for a label's text block. ``False`` (the default)
# centers the block by its **baseline-to-capline band** -- the band spans the
# lowest baseline to the highest capline, so the visible text is optically
# centered and descenders hang into the bottom margin, where the descending-
# character relief handles them against the bottom border. ``True`` centers by
# the block's **rendered ink** (the historical behaviour): the deepest
# descender is charged to the block, so a line carrying descenders rides high
# in the label. Band centering is active only when the descending-character
# relief is enabled (see ``center_text_by_band``); with the relief off nothing
# would adjust the descenders, so ink centering is kept. Label-level only --
# centering is a property of the stacked block, never of one line, so the
# field is deliberately absent from ``TextLine``/``TextAttributes``.
DEFAULT_V_CENTER_BY_CONTENT: bool = False
# Automatic descending-character relief. ``True`` (the default) lets the
# renderer vertically downscale and raise a glyph whose ink hangs below its
# baseline, but ONLY when a glyph on the following line comes within the
# required clearance (the engraved-stroke floor scaled by
# ``DEFAULT_DESCENDING_CHARACTER_CLEARANCE``) of it.
# The relief is per triggering glyph -- a descender with no tall neighbour
# below it is left exactly as rendered. When the allowances
# (``DEFAULT_DESCENDING_CHARACTER_V_SCALE`` / ``DEFAULT_DESCENDING_CHARACTER_RAISE``)
# are exhausted, the gap below the line grows instead. Label-level only -- the
# mechanism compares a line's glyphs against the following line's, so it is a
# property of the stacked block, never of one line, and the field is
# deliberately absent from ``TextLine``/``TextAttributes``.
DEFAULT_AUTO_DESCENDING_CHARACTER_ADJUSTMENT: bool = True
# Deepest vertical downscale permitted for a triggering descending glyph
# (``1.0`` = downscaling forbidden, ``0.5`` = squeezed to half height). The
# glyph is scaled about its own baseline, so ascender and descender shrink
# together. Bound to ``[0.2, 1.0]`` by the schema.
DEFAULT_DESCENDING_CHARACTER_V_SCALE: float = 0.5
# Deepest raise permitted for a triggering descending glyph, as a fraction of
# its own descender depth (``0.0`` = no raising, ``1.0`` = lift until the
# glyph's lowest point sits exactly at the baseline).
DEFAULT_DESCENDING_CHARACTER_RAISE: float = 0.7
# Extra air demanded between the engraved strokes of a descending glyph and
# the glyph below it, as a **unitless factor of the stroke floor**
# ``0.5 * (cutter_upper + cutter_lower)``: the required gap is
# ``floor * (1 + clearance)``, so the demand scales with the cutter width and
# reads the same at every text size. An explicit ``0.0`` is honored (only true
# touching triggers).
DEFAULT_DESCENDING_CHARACTER_CLEARANCE: float = 0.0
# Deepest top-down downscale permitted for the would-be-colliding glyphs on
# the FOLLOWING line (``1.0`` = reciprocal downscaling forbidden, the
# historical behaviour; ``0.5`` = squeezed to half height). The lower glyphs
# are scaled about their own lowest ink point, so their lowest point never
# moves; they share the relief with the descending glyph through the same
# ramp driving its downscale/raise. Bound to ``[0.2, 1.0]`` by the schema.
DEFAULT_DESCENDING_CHARACTER_RECIPROCAL_SCALE: float = 0.5
# Stroke-color layer tag default. ``"none"`` is the implicit color of
# every line that omits ``text_color``; it never cascades (the field is
# label/line-local by design, see schema.TextColor) and the resolution
# engine never reads a job-level value (the schema rejects one).
DEFAULT_TEXT_COLOR: str = "none"

# Float tolerance guarding the downsize midpoint comparison: a scale exactly
# at the midpoint keeps the current cutter (the rule is "compressed to 89%
# takes the smaller tool, >= 90% keeps the original").
_DOWNSIZE_SCALE_TOLERANCE: float = 1e-12

# ---------------------------------------------------------------------------
# Cutter lookup table and inventory matching
# ---------------------------------------------------------------------------
# Maps nominal text height (inches) to the ideal cutter diameter (inches).
# Keys are stored as exact float representations of common fractions.
IDEAL_CUTTER_MAP: dict[float, float] = {
    0.0625: 0.005,  # 1/16
    0.09375: 0.01,  # 3/32
    0.125: 0.015,  # 1/8
    0.1875: 0.02,  # 3/16
    0.21875: 0.025,  # 7/32
    0.25: 0.03,  # 1/4
    0.3125: 0.04,  # 5/16
    0.375: 0.045,  # 3/8
    0.4375: 0.05,  # 7/16
    0.5: 0.06,  # 1/2
    0.625: 0.075,  # 5/8
    0.75: 0.09,  # 3/4
    1.0: 0.125,  # 1
    1.25: 0.15,  # 1-1/4
    1.375: 0.171,  # 1-3/8
    1.5: 0.187,  # 1-1/2
    1.75: 0.21,  # 1-3/4
    2.0: 0.25,  # 2
}


def get_cutter_diameter(
    nominal_height: float,
    available_inventory: Optional[list[float]] = None,
    tolerance_factor: float = 3.0,
) -> float:
    """Find the optimal cutter diameter, preferring a narrower tool.

    The logic defaults to a narrower cutter to prevent character bleeding,
    but switches to a wider cutter when the closest narrower tool exceeds
    the distance tolerance factor relative to the wider tool. This
    prevents using an impractically small tool that could result in
    illegible hairline text or unnecessary tool wear.

    Args:
        nominal_height: The nominal text height in inches.
        available_inventory: Optional list of cutter diameters available in
            the shop. If None or empty, the ideal cutter is returned.
        tolerance_factor: The multiplier used to decide between narrower and
            wider cutters. If the distance to the closest narrower cutter
            exceeds ``tolerance_factor`` times the distance to the closest
            wider cutter, the wider cutter is selected. Defaults to 3.0.

    Returns:
        The recommended cutter diameter in inches.
    """
    # 1. Find the ideal cutter from the lookup table
    closest_nominal = min(IDEAL_CUTTER_MAP.keys(), key=lambda k: abs(k - nominal_height))
    ideal_cutter = IDEAL_CUTTER_MAP[closest_nominal]

    # 2. If no inventory provided, return the ideal cutter
    if not available_inventory:
        return ideal_cutter

    # 3. Filter inventory into narrower (including exact match) and wider lists
    narrower_cutters = [c for c in available_inventory if c <= ideal_cutter]
    wider_cutters = [c for c in available_inventory if c > ideal_cutter]

    # 4. Handle edge cases where inventory is severely restricted
    if not narrower_cutters:
        return min(wider_cutters)  # Must use the smallest available wider cutter
    if not wider_cutters:
        return max(narrower_cutters)  # Must use the largest available narrower cutter

    # 5. Find the closest candidates
    closest_narrower = max(narrower_cutters)
    closest_wider = min(wider_cutters)

    # 6. Apply the threshold logic
    dist_narrower = ideal_cutter - closest_narrower
    dist_wider = closest_wider - ideal_cutter

    if dist_narrower > (tolerance_factor * dist_wider):
        return closest_wider
    else:
        return closest_narrower


def snap_boundary_hole_cutter(
    requested: float,
    available_inventory: Optional[list[float]] = None,
) -> float:
    """Snap the boundary/hole cutter size to the available tool inventory.

    The boundary/hole cutter (used for label borders and drill holes) is a
    single fixed tool, so selection is a plain snap rather than the
    tolerance-based text-cutter choice in :func:`get_cutter_diameter`: an
    exact (or equal) match is kept; otherwise the next size *down* is
    preferred, and only when no smaller tool exists the next size *up*.

    Args:
        requested: The requested cutter diameter in inches (e.g. from
            ``tools.json`` ``boundary_hole_cutter_size`` or the default).
        available_inventory: Optional list of cutter diameters available
            in the shop. If None or empty, ``requested`` is returned
            unchanged (the ideal tool is assumed available).

    Returns:
        The snapped cutter diameter in inches.
    """
    if not available_inventory:
        return requested

    narrower = [c for c in available_inventory if c <= requested]
    if narrower:
        return max(narrower)  # Exact match or next size down
    return min(available_inventory)  # No smaller tool: next size up


def next_smaller_cutter(
    current: float,
    available_inventory: Optional[list[float]] = None,
) -> Optional[float]:
    """Find the next smaller cutter in the shop inventory.

    The compression-driven cutter reduction walks the inventory one step at a
    time, so the ladder is the shop's real tool list: an empty inventory means
    there is nothing to snap to and the mechanism is a no-op (the ideal-cutter
    ladder is deliberately *not* used -- downsizing to a tool the shop does
    not own would produce output it cannot run).

    Args:
        current: The cutter diameter currently in use, in inches.
        available_inventory: Shop cutter diameters in inches (``None`` or
            empty disables the mechanism).

    Returns:
        The largest inventory diameter strictly below ``current``, or ``None``
        when the inventory is empty or ``current`` is already the smallest (or
        smaller than every) tool.
    """
    if not available_inventory:
        return None
    strictly_smaller = [
        c for c in available_inventory if c < current and not math.isclose(c, current, abs_tol=1e-9)
    ]
    return max(strictly_smaller) if strictly_smaller else None


def next_larger_cutter(
    current: float,
    available_inventory: Optional[list[float]] = None,
) -> Optional[float]:
    """Find the next larger cutter in the shop inventory.

    The multi-stroke cutter ladder snaps down like ``snap_boundary_hole_cutter``
    and then re-checks its gap-free floor: when the snapped tool falls below
    the floor, the ladder steps *up* to the smallest inventory diameter at or
    above the floor, so adjacent engraved passes always overlap.

    Args:
        current: The cutter diameter currently in use, in inches.
        available_inventory: Shop cutter diameters in inches (``None`` or
            empty disables the mechanism).

    Returns:
        The smallest inventory diameter strictly above ``current``, or ``None``
        when the inventory is empty or ``current`` is already the largest (or
        larger than every) tool.
    """
    if not available_inventory:
        return None
    strictly_larger = [
        c for c in available_inventory if c > current and not math.isclose(c, current, abs_tol=1e-9)
    ]
    return min(strictly_larger) if strictly_larger else None


def should_downsize_cutter(
    effective_scale: float,
    current_cutter: float,
    next_cutter: float,
) -> bool:
    """Decide whether a compressed line should swap to a smaller cutter.

    The rule is the midpoint between the current cutter and the next smaller
    one: the smaller tool is used when the line's effective horizontal scale
    is *closer to* ``next_cutter / current_cutter`` than to ``1.0``. With a
    0.1in cutter and a 0.08in next size (ratio 0.8) the midpoint is 0.9, so a
    line compressed to 89% takes the 0.08in tool while a line at 90% keeps the
    0.1in tool (a tie keeps the current cutter).

    Args:
        effective_scale: The line's effective horizontal scale in ``(0, 1]``
            (collision-avoidance scale x margin-overflow scale).
        current_cutter: Cutter diameter currently in use, in inches.
        next_cutter: Candidate smaller cutter diameter, in inches.

    Returns:
        ``True`` when the smaller cutter should be adopted.
    """
    if current_cutter <= 0.0 or next_cutter <= 0.0:
        return False
    ratio = next_cutter / current_cutter
    midpoint = (1.0 + ratio) / 2.0
    return effective_scale < midpoint - _DOWNSIZE_SCALE_TOLERANCE


@dataclass(frozen=True)
class MultiStrokeCutter:
    """The cutter a multi-stroke (bold) font line resolves to.

    Attributes:
        cutter: Ideal (pre-snap) cutter diameter in inches - one engraved
            pass width. The N passes of the glyph sit at
            :func:`design_pass_spacing` apart, so the total engraved stroke
            width is ``cutter + (N - 1) * spacing``.
        ideal: The boldness target's cutter before the gap-free floor is
            applied (``text_height * factor - (N - 1) * spacing``); equal to
            :attr:`cutter` unless :attr:`clamped`.
        floor: The gap-free minimum cutter in inches
            (``spacing / (1 - overlap_floor)``): any cutter below it would
            leave an unengraved stripe between adjacent passes.
        clamped: ``True`` when the boldness target asked for a thinner
            cutter than the floor allows and the floor won (the engraved
            width exceeds the requested boldness).
        implicit_overlap: The overlap fraction the resolved cutter produces
            at the baked pass spacing, ``1 - spacing / cutter`` (reported,
            never an input).
    """

    cutter: float
    ideal: float
    floor: float
    clamped: bool
    implicit_overlap: float


def design_pass_spacing(
    nominal_height: float,
    stroke_count: int,
    design_overlap: float = DESIGN_MULTISTROKE_OVERLAP,
    boldness_factor: float = DEFAULT_TARGET_BOLDNESS_FACTOR,
) -> float:
    """Return the pass spacing baked into a multi-stroke font's glyphs.

    The sample sheets are engraved at the *design* point: boldness factor
    ``DEFAULT_TARGET_BOLDNESS_FACTOR`` with adjacent passes overlapping by
    ``design_overlap``, so the design cutter is
    ``text_height * factor / (1 + (N - 1) * (1 - design_overlap))`` and the
    passes sit ``design_stepover * design_cutter`` apart. The extractor
    stores those engraved passes verbatim, so this spacing is a pure
    function of the text height - the renderer scales the glyph geometry
    linearly, carrying the spacing with it.

    Args:
        nominal_height: The line's nominal text height in inches.
        stroke_count: Number of engraved passes per glyph (>= 1).
        design_overlap: The overlap ratio the sheets were engraved at.
        boldness_factor: The design boldness factor.

    Returns:
        The pass spacing in inches; 0.0 for a single-stroke font.
    """
    if stroke_count <= 1:
        return 0.0
    units = 1.0 + (stroke_count - 1) * (1.0 - design_overlap)
    design_cutter = nominal_height * boldness_factor / units
    return (1.0 - design_overlap) * design_cutter


def multi_stroke_cutter_diameter(
    nominal_height: float,
    stroke_count: int,
    overlap_floor: float = DEFAULT_MULTISTROKE_OVERLAP_RATIO,
    boldness_factor: float = DEFAULT_TARGET_BOLDNESS_FACTOR,
) -> MultiStrokeCutter:
    """Size the cutter for a multi-stroke (bold) font line.

    The engraved width of a glyph's N passes at spacing ``d`` with cutter
    ``W`` is ``W + (N - 1) * d``, so the boldness target
    ``W_total = text_height * boldness_factor`` inverts to
    ``W = W_total - (N - 1) * d``. The overlap floor
    ``overlap_floor`` (the ``multistroke_overlap_ratio`` field) guards the
    result from below: adjacent passes must overlap by at least that
    fraction, i.e. ``W >= d / (1 - overlap_floor)``. At the defaults the
    result equals the classic ``text_height * 0.14 / (1 + (N - 1) * 0.75)``
    (the design point sits exactly on the floor); a larger boldness factor
    grows the cutter and the *implicit* overlap with it, which is how the
    ladder escapes the floor toward heavier engraving without stripes.

    Args:
        nominal_height: The line's nominal text height in inches.
        stroke_count: Number of engraved passes per glyph (>= 2; a
            single-stroke font uses the height-based auto-selection).
        overlap_floor: Minimum pass overlap fraction in [0, 1).
        boldness_factor: Target engraved width as a fraction of height.

    Returns:
        The :class:`MultiStrokeCutter` recommendation (pre-snap).
    """
    spacing = design_pass_spacing(nominal_height, stroke_count)
    floor = spacing / (1.0 - overlap_floor) if overlap_floor < 1.0 else math.inf
    ideal = nominal_height * boldness_factor - (stroke_count - 1) * spacing
    # The design point (default factor + default floor) lands *exactly* on the
    # floor; the division in ``floor`` makes that a few ULPs apart in binary,
    # so the comparison needs the same tolerance the downsize midpoint uses.
    clamped = ideal < floor - _DOWNSIZE_SCALE_TOLERANCE
    cutter = max(ideal, floor)
    implicit_overlap = 1.0 - spacing / cutter if cutter > 0.0 else 1.0
    return MultiStrokeCutter(
        cutter=cutter,
        ideal=ideal,
        floor=floor,
        clamped=clamped,
        implicit_overlap=implicit_overlap,
    )


# ---------------------------------------------------------------------------
# Strictly typed target dataclasses
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ResolvedHoleSpec:
    """A fully resolved hole specification.

    Attributes:
        diameter: The diameter of the hole in inches.
        location: The position of the hole on the label edge (string).
    """

    diameter: float
    location: str


@dataclass(frozen=True)
class ResolvedTextLine:
    """A fully resolved text line with cutter compensation applied.

    Attributes:
        text: The actual text string to render.
        nominal_text_height: The requested font height in inches (before
            cutter compensation).
        toolpath_text_height: The actual toolpath height in inches (after
            subtracting cutter diameter). This is what vpype renders.
        cutter_diameter: The matched tool diameter in inches, used for
            kerf compensation and pre-job reporting.
        character_spacing: Extra spacing between characters in inches.
        line_spacing: Extra spacing between text lines in inches.
        max_h_compress: Maximum horizontal compression fraction in
            ``[0.0, 1.0]`` (cascaded line -> label -> job, default ``0.0``).
            When the rendered line is wider than the label's inner content
            area, the renderer may uniformly compress it horizontally down
            to ``(1 - max_h_compress)`` of its natural width. ``0.0``
            disables compression.
        text_h_alignment: Horizontal alignment of the rendered line within
            the label's inner content area (cascaded line -> label -> job,
            default ``"center"``). One of ``"left"``, ``"center"`` or
            ``"right"``. ``"left"`` places the line's left-most point
            precisely at the left margin; ``"right"`` places the right-most
            point precisely at the right margin.
        text_color: Stroke-color layer tag (``"none"`` default) splitting
            otherwise-identical text into separate toolpaths. Resolved
            line -> label -> default (deliberately NOT cascaded from the
            job; see :class:`~plt_optimizer.generate.schema.TextColor`).
            Consumed by the pen map so each ``(cutter, color)`` pair gets
            its own HPGL ``SP`` layer and PLT file.
        font: Canonical font name selecting the glyph outlines (cascaded
            line -> label -> job, default ``DEFAULT_FONT``). Either a
            PLT-extracted catalog ``font_name`` key (rendered arc-native) or a
            ``Fonts/`` TTF basename (rendered through ftext); resolve the
            kind via ``font_registry.resolve_font``. A trailing `` SC``
            selects the base font's synthetic small-caps variant (see
            :attr:`sc_scale`).
        space_width_fraction: Space advance as a fraction of the rendered
            text height (PLT-extracted fonts): a space advances
            ``space_width_fraction * text_height + character_spacing``.
            Cascaded line -> label -> job, default
            ``DEFAULT_SPACE_WIDTH_FRACTION`` (0.3). Explicit ``0.0`` is
            honored; only ``None`` means unset.
        min_glyph_width: Global minimum glyph advance width in inches
            (PLT-extracted fonts): the profile-envelope kerning floors
            each pair's advance at this value, capped by the left glyph's
            own width, so zero-width glyphs still reserve real air without
            thin-glyph pairs being inflated past their own silhouette.
            Cascaded line ->
            label -> job, default ``DEFAULT_MIN_GLYPH_WIDTH`` (0.0 = pure
            envelope kerning). Explicit ``0.0`` is honored; only ``None``
            means unset.
        kerning_window_fraction: Kerning window as a fraction of the
            rendered text height in [0.0, 1.0] (PLT-extracted fonts):
            envelope samples compare against the opposite silhouette
            within +/- (half of this fraction) of the text height and the
            effective penetration is the worst windowed penetration, so
            staggered pokes widen the advance. Cascaded line -> label ->
            job, default ``DEFAULT_KERNING_WINDOW_FRACTION`` (0.05).
            Explicit ``0.0`` is honored (= historical same-height
            kerning); only ``None`` means unset.
        kerning_penetration_scale: Multiplier on the detected windowed
            penetration (PLT-extracted fonts): 1.0 keeps the geometric
            penetration, >1.0 over-kerns tight pairs. Cascaded line ->
            label -> job, default ``DEFAULT_KERNING_PENETRATION_SCALE``
            (1.0). Explicit ``0.0`` is honored; only ``None`` means unset.
        kerning_recession_scale: Multiplier on a *recessed* (negative)
            detected penetration (PLT-extracted fonts): 1.0 keeps the
            geometric recession, 0.0 makes a recessed pair advance by
            nothing beyond the ``min_glyph_width`` floor. Cascaded line ->
            label -> job, default ``DEFAULT_KERNING_RECESSION_SCALE``
            (1.0). Explicit ``0.0`` is honored; only ``None`` means unset.
        kerning_min_gap: Extra air in inches added to every kerned pair
            advance on top of the clearance (PLT-extracted fonts).
            Cascaded line -> label -> job, default
            ``DEFAULT_KERNING_MIN_GAP`` (0.0). Explicit ``0.0`` is honored;
            only ``None`` means unset.
        fallback_advance_fraction: Multiplier on the bounding-box-width
            fallback advance for pairs without overlapping height (or
            lacking envelopes). Cascaded line -> label -> job, default
            ``DEFAULT_FALLBACK_ADVANCE_FRACTION`` (1.0). Explicit ``0.0``
            is honored; only ``None`` means unset.
        sc_scale: Downscale applied to ASCII lowercase ``a-z`` by a
            small-caps font variant (a ``font`` name ending in `` SC``):
            the lowercase character renders as the base font's uppercase
            glyph uniformly shrunk to this fraction of the cap height,
            **top-down** (the glyph's own bottom stays exactly in place and
            its top descends; uppercase and caseless characters
            engrave at full size; the nominal/toolpath text height is
            untouched, so the cutter is unaffected). Cascaded line ->
            label -> job, default ``DEFAULT_SC_SCALE`` (0.7). Explicit
            ``1.0`` (base-identical rendering) is honored; only ``None``
            means unset. Inert when the resolved ``font`` is not an SC
            variant.
        supersubscript_downscale: Downscale applied to a **bare**
            ``<sup>``/``<sub>`` markup run: the run's glyphs engrave
            uniformly shrunk to this fraction of the line's size, and the
            baseline shift derives from it, centering the shrunken glyph on
            the line it belongs to (superscript rises ``1 - d / 2`` cap
            heights, its centre landing on the capline; subscript drops
            ``d / 2`` cap heights, its centre landing on the baseline). The
            argumented forms ``<sup:0.3>`` / ``<sub:-0.2>`` set the shift
            explicitly (in cap-height units) and apply **no** scaling, so
            this field is inert on them. The run engraves with the cutter
            for its downscaled height (the ``<scale:N>`` ladder). Cascaded
            line -> label -> job, default ``DEFAULT_SUPERSUBSCRIPT_DOWNSCALE``
            (0.5). Explicit ``0.2``/``1.0`` are honored; only ``None``
            means unset.
        cutter_size: The explicit cutter diameter in inches requested for
            this line (cascaded line -> label -> job), verbatim as declared,
            or ``None`` when unset. ``None`` means the cutter was
            auto-selected from ``nominal_text_height`` via
            :func:`get_cutter_diameter` (``cutter_size`` is then ``None``
            while :attr:`cutter_diameter` carries the auto-selected tool);
            when set, :attr:`cutter_diameter` carries the inventory-snapped
            value (equal to it when no inventory is in play) and
            :attr:`toolpath_text_height` is ``nominal_text_height -
            cutter_diameter``. Default ``DEFAULT_CUTTER_SIZE`` (``None``).
        cutter_downsize: Whether the compression-driven cutter reduction may
            act on this line (cascaded line -> label -> job, default
            ``DEFAULT_CUTTER_DOWNSIZE`` (``True``)). Consumed by
            :mod:`plt_optimizer.generate.cutter_downsize` after rendering,
            where the effective horizontal scale is measurable; an explicit
            :attr:`cutter_size` disables the mechanism for the line.
            Explicit ``False`` is honored; only ``None`` means unset.
        max_cutter_downsizes: Ceiling on the number of successive cutter
            downsizings applied to this line (cascaded line -> label -> job,
            default ``DEFAULT_MAX_CUTTER_DOWNSIZES`` (1)). ``0`` disables the
            mechanism. Explicit ``0`` is honored; only ``None`` means unset.
        cutter_downsize_global: Whether a compression-driven cutter downsize
            is shared across the lines of the label (cascaded line -> label
            -> job, default ``DEFAULT_CUTTER_DOWNSIZE_GLOBAL`` (``True``)).
            When a trigger line swaps to a smaller tool, every other
            *eligible* line of the same ``nominal_text_height`` in the same
            label receives the same swap; a line with this flag ``False``
            neither shares its own downsize nor receives a sibling's.
            Consumed by :mod:`plt_optimizer.generate.cutter_downsize`.
            Explicit ``False`` is honored; only ``None`` means unset.
        h_compress_global: Whether horizontal compression is shared across
            the lines of the label (cascaded line -> label -> job, default
            ``DEFAULT_H_COMPRESS_GLOBAL`` (``False``)). When a trigger line
            is horizontally compressed, every other *eligible* line of the
            same ``nominal_text_height`` in the same label is compressed to
            the group's minimum (most-compressed) scale, clamped to each
            receiver's own ``1 - max_h_compress`` budget floor; a line with
            this flag ``False`` neither shares its own compression nor
            receives a sibling's, and a line with a zero ``max_h_compress``
            budget is never touched. Consumed by
            :mod:`plt_optimizer.generate.h_compress`. Explicit ``True`` is
            honored; only ``None`` means unset.
        optimize_line_content: Whether this line's words may be
            redistributed across the consecutive lines that also enable the
            option (cascaded line -> label -> job, default
            ``DEFAULT_OPTIMIZE_LINE_CONTENT`` (``False``)). Consecutive
            enabled lines form one independent reflow group; the export
            pre-pass :mod:`plt_optimizer.generate.line_content` moves whole
            words between the group's lines (word order preserved, only the
            line breaks change) so the group's rendered widths equalize,
            minimizing horizontal compression. Consumed before the
            cutter-reduction and shared-compression pre-passes. Explicit
            ``True`` is honored; only ``None`` means unset.
        optimize_line_content_max_lines: Ceiling ``N`` on the consecutive
            lines the reflow group this line belongs to may use (cascaded
            line -> label -> job, default ``None`` = unset). Growth-only: the
            group starts at its existing line count ``G`` and may gain lines
            (cloned from its own typography) while its lines still need
            horizontal compression, up to ``N``; it never removes, blanks or
            collapses a line. ``N <= G`` leaves the cap inert. Setting it on
            any one line of a group applies it to the whole group (the
            resolution post-pass fans the value out onto every group line),
            two *different* values in one group raise
            :class:`LineContentConfigError`, and so does declaring it while
            no line of the label enables ``optimize_line_content``.
        html_brackets: Delimiter pair for the inline markup syntax
            (cascaded line -> label -> job, default
            ``DEFAULT_HTML_BRACKETS`` (``"<>"``)): an even-length string
            whose first half is the left delimiter and second half the
            right. Split by :func:`plt_optimizer.generate.markup.split_brackets`
            at parse time; a bracketed body that is not a registered tag
            engraves verbatim.
        italic_angle: Italic slant angle in degrees for ``<i>`` markup
            (cascaded line -> label -> job, default ``DEFAULT_ITALIC_ANGLE``
            (12)). PLT glyphs (and TTF glyphs with no native italic face)
            are sheared about the baseline, ``x' = x + y * tan(angle)``;
            ``0.0`` is upright (the shear is skipped). Bound to [0, 45] by
            the schema.
        underline_offset: Extra air in inches for ``<u>`` markup (cascaded
            line -> label -> job, default ``DEFAULT_UNDERLINE_OFFSET``
            (0.0)). The underline centerline (cut with the line's own text
            cutter) sits ``cutter diameter + underline_offset`` below the
            baseline; the break test is strictly horizontal, so a glyph breaks
            the underline where its ink crosses the underline's centerline
            (dipping past ``cutter diameter + underline_offset`` below the
            baseline). Raising it lifts the underline clear of a glyph and
            stops it breaking. Bound to
            ``[0, inf)`` by the schema; an explicit ``0.0`` is honored.
        underline_break_clearance: Extra air in inches at each ``<u>`` break
            (cascaded line -> label -> job, default
            ``DEFAULT_UNDERLINE_BREAK_CLEARANCE`` (0.0)). Each cut is padded
            horizontally by ``0.5 * cutter + underline_break_clearance`` (the
            half cutter is the glyph stroke's own half-width), so the engraved
            air between a descending glyph's stroke and the underline's cut
            edge is exactly this clearance (0.0 = the cut sits at the ink).
            Independent of :attr:`underline_offset`
            (which decides which glyphs break); this adds air at the break
            itself.
            Bound to ``[0, inf)`` by the schema; an explicit ``0.0`` is
            honored.
        underline_break_spaces: Whether a ``<u>`` underline breaks at the
            spaces between words (cascaded line -> label -> job, default
            ``DEFAULT_UNDERLINE_BREAK_SPACES`` (False)). False draws one
            continuous underline across the whole underlined run (spaces
            included, broken only by descending glyphs); True ends the
            underline at each space, so every word gets its own underline
            spanning its own ink and the inter-word space stays bare. An
            explicit ``False`` is honored.
        bold_boldness_multiplier: Multiplier applied to
            :attr:`target_boldness_factor` by inline ``<b>`` markup on a
            multi-stroke (bold) PLT font (cascaded line -> label -> job,
            default ``DEFAULT_BOLD_BOLDNESS_MULTIPLIER`` (1.5), bound to
            [1.0, 4.0]); the product is clamped to
            ``MAX_TARGET_BOLDNESS_FACTOR`` (0.30) with a WARNING. Inert on
            single-stroke PLT fonts, TTF fonts, and lines with no bold run.
        bold_cutter_diameter: The thicker cutter the ``<b>`` runs of this
            line engrave with, in inches (``None`` when the line has no
            bold run). Single-stroke PLT and TTF lines: the next larger
            inventory rung above :attr:`cutter_diameter` (``None`` stays
            ``None`` at the top rung, with a WARNING -- the run engraves
            with the plain cutter). Multi-stroke PLT fonts: the boldness
            ladder at ``target_boldness_factor * bold_boldness_multiplier``.
            The compression-driven cutter reduction steps this cutter down
            in lockstep with :attr:`cutter_diameter`, so the bold run stays
            one rung above the line's final plain cutter.
        bold_toolpath_text_height: ``nominal_text_height -
            bold_cutter_diameter`` (``None`` with no bold run) -- the
            rendered cap height of the bold glyphs, mirroring
            :attr:`toolpath_text_height` for the plain run.
        scale_cutter_inventory: The shop cutter inventory (inches) the
            ``<scale:N>`` runs' cutters snap into -- the ladder inputs for
            :attr:`per_char_cutters` (``None`` = no inventory, so the ideal
            ladder cutter is used unsnapped). Stored as the *inputs* to the
            ladder, never as a per-character result array: the per-character
            cutters are derived from each character's scale factor, so a
            pre-pass that rewrites the line's text (:meth:`with_markup`, the
            line-content reflow) keeps every run on its own tool. A positional
            array would go stale when words move, silently handing a downscaled
            run the neighbouring plain text's (larger) cutter.
        scale_cutter_tolerance: The narrower/wider snap tolerance passed to
            :func:`get_cutter_diameter` for the scaled runs' ladder.
        plain_text: :attr:`text` with all inline markup removed -- the text
            reports, chunk records, and layout findings display. Equal to
            :attr:`text` when the line contains no parsed tag.
        styled_runs: The parsed markup runs (empty tuple when the line has
            no markup -- the renderers' fast path, which keeps markup-free
            jobs bit-identical). When non-empty, ``"".join(r.text for r in
            styled_runs)`` equals :attr:`plain_text` and the renderers
            dispatch per run (italic runs shear; plain runs render as
            today). Populated by :func:`parse_markup` at resolution time.
        scale_values: Per-character ``<scale:N>`` factors aligned with
            :attr:`plain_text` (``()`` with no markup, ``1.0`` = unscaled
            character) -- see :meth:`scale_values`.
    """

    text: str
    nominal_text_height: float
    toolpath_text_height: float
    cutter_diameter: float
    character_spacing: float
    line_spacing: float
    max_h_compress: float = 0.0
    text_h_alignment: str = DEFAULT_TEXT_H_ALIGNMENT
    text_color: str = DEFAULT_TEXT_COLOR
    font: str = DEFAULT_FONT
    space_width_fraction: float = DEFAULT_SPACE_WIDTH_FRACTION
    min_glyph_width: float = DEFAULT_MIN_GLYPH_WIDTH
    kerning_window_fraction: float = DEFAULT_KERNING_WINDOW_FRACTION
    kerning_penetration_scale: float = DEFAULT_KERNING_PENETRATION_SCALE
    kerning_recession_scale: float = DEFAULT_KERNING_RECESSION_SCALE
    kerning_min_gap: float = DEFAULT_KERNING_MIN_GAP
    fallback_advance_fraction: float = DEFAULT_FALLBACK_ADVANCE_FRACTION
    sc_scale: float = DEFAULT_SC_SCALE
    supersubscript_downscale: float = DEFAULT_SUPERSUBSCRIPT_DOWNSCALE
    html_brackets: str = DEFAULT_HTML_BRACKETS
    italic_angle: float = DEFAULT_ITALIC_ANGLE
    underline_offset: float = DEFAULT_UNDERLINE_OFFSET
    underline_break_clearance: float = DEFAULT_UNDERLINE_BREAK_CLEARANCE
    underline_break_spaces: bool = DEFAULT_UNDERLINE_BREAK_SPACES
    bold_boldness_multiplier: float = DEFAULT_BOLD_BOLDNESS_MULTIPLIER
    bold_cutter_diameter: Optional[float] = None
    bold_toolpath_text_height: Optional[float] = None
    scale_cutter_inventory: Optional[Tuple[float, ...]] = None
    scale_cutter_tolerance: float = DEFAULT_CUTTER_LADDER_TOLERANCE
    plain_text: str = ""
    styled_runs: Tuple[Run, ...] = ()
    cutter_size: Optional[float] = DEFAULT_CUTTER_SIZE
    target_boldness_factor: float = DEFAULT_TARGET_BOLDNESS_FACTOR
    multistroke_overlap_ratio: float = DEFAULT_MULTISTROKE_OVERLAP_RATIO
    cutter_downsize: bool = DEFAULT_CUTTER_DOWNSIZE
    max_cutter_downsizes: int = DEFAULT_MAX_CUTTER_DOWNSIZES
    cutter_downsize_global: bool = DEFAULT_CUTTER_DOWNSIZE_GLOBAL
    h_compress_global: bool = DEFAULT_H_COMPRESS_GLOBAL
    optimize_line_content: bool = DEFAULT_OPTIMIZE_LINE_CONTENT
    optimize_line_content_max_lines: Optional[int] = DEFAULT_OPTIMIZE_LINE_CONTENT_MAX_LINES

    def __post_init__(self) -> None:
        """Default :attr:`plain_text` to :attr:`text` (markup-free fast path).

        Direct API/test callers construct lines without going through
        :func:`parse_markup`; an unset ``plain_text`` then means "no markup",
        so the plain text is the raw text itself.
        """
        if not self.plain_text:
            object.__setattr__(self, "plain_text", self.text)

    def with_markup(self, text: str) -> ResolvedTextLine:
        """Return a copy whose ``text`` is re-parsed for inline markup.

        The pre-passes (line-content reflow, cutter reduction, shared
        compression) rebuild lines with ``dataclasses.replace``; a raw text
        swap would leave :attr:`plain_text` / :attr:`styled_runs` stale (the
        renderers would engrave the old plain text). This helper is the only
        supported way to change a resolved line's text: it re-parses with the
        line's own resolved delimiters, so a markup-free line lands on the
        historical fast path (empty runs, ``plain_text == text``).

        Args:
            text: The new raw text (may contain markup).

        Returns:
            The updated line (``self`` when the text is unchanged).

        Raises:
            MarkupError: If the new text carries malformed markup (a
                :class:`ValueError`, so pre-pass callers abort the job).
        """
        if text == self.text:
            return self
        left, right = split_brackets(self.html_brackets)
        styled = parse_markup(text, left, right)
        return replace(self, text=text, plain_text=styled.plain_text, styled_runs=styled.runs)

    @property
    def bold_flags(self) -> Tuple[bool, ...]:
        """Per-character bold flags aligned with :attr:`plain_text`.

        Empty tuple when the line carries no parsed markup (the renderers'
        fast path). Mirrors :meth:`markup.StyledText.bold_flags` so the
        renderers never need to re-expand the runs.
        """
        return tuple(
            style.bold for run in self.styled_runs for style in (run.style,) * len(run.text)
        )

    @property
    def has_bold(self) -> bool:
        """True when any markup run of this line is bold."""
        return any(run.style.bold for run in self.styled_runs)

    @property
    def scale_values(self) -> Tuple[float, ...]:
        """Per-character ``<scale:N>`` factors aligned with :attr:`plain_text`.

        Empty tuple when the line carries no parsed markup (the renderers'
        fast path); ``1.0`` marks an unscaled character. Mirrors
        :meth:`markup.StyledText.scale_values` so the renderers never need
        to re-expand the runs.
        """
        return tuple(
            style.scale for run in self.styled_runs for style in (run.style,) * len(run.text)
        )

    @property
    def has_scale(self) -> bool:
        """True when any markup run of this line carries a ``<scale:N>`` tag."""
        return any(run.style.scale != 1.0 for run in self.styled_runs)

    @property
    def sup_sub_values(self) -> Tuple[Optional[SupSub], ...]:
        """Per-character ``<sup>``/``<sub>`` shifts aligned with :attr:`plain_text`.

        Empty tuple when the line carries no parsed markup (the renderers'
        fast path); ``None`` marks an unmarked character. Mirrors
        :meth:`markup.StyledText.sup_sub_values`.
        """
        return tuple(
            style.sup_sub for run in self.styled_runs for style in (run.style,) * len(run.text)
        )

    @property
    def has_sup_sub(self) -> bool:
        """True when any markup run of this line carries a ``<sup>``/``<sub>`` tag."""
        return any(run.style.sup_sub is not None for run in self.styled_runs)

    @property
    def sup_sub_scales(self) -> Tuple[float, ...]:
        """Per-character ``<sup>``/``<sub>`` geometry scales, aligned with plain text.

        The **bare** tag downscales to :attr:`supersubscript_downscale`;
        the argumented form (``<sup:0.3>``) scales by ``1.0`` (it shifts
        only), and unmarked characters are ``1.0``. Derived from the style
        (never a stored positional array), so it stays correct when a
        pre-pass rewrites the line's text.
        """
        downscale = self.supersubscript_downscale
        return tuple(
            (1.0 if style.sup_sub is None else (downscale if style.sup_sub.shift is None else 1.0))
            for run in self.styled_runs
            for style in (run.style,) * len(run.text)
        )

    @property
    def sup_sub_shifts(self) -> Tuple[float, ...]:
        """Per-character ``<sup>``/``<sub>`` baseline shifts in cap-height units.

        The bare tag derives the shift from :attr:`supersubscript_downscale`
        ``d``, **centering the shrunken glyph on the line it belongs to**
        (superscript ``1 - d / 2`` so its centre lands on the capline,
        subscript ``-d / 2`` so its centre lands on the baseline -- parking
        a top on the capline left the glyph visually below the cap-height
        text, resting a bottom on the baseline left a subscript barely
        lowered); the argumented form uses its explicit shift; unmarked
        characters are ``0.0``. A bare tag therefore grows the line's ink
        box by ``d / 2`` cap heights (the glyph straddles the line it is
        centered on). The renderer converts to inches by multiplying with
        the line's ``toolpath_text_height`` (the per-font cap height).
        """
        downscale = self.supersubscript_downscale
        shifts: List[float] = []
        for run in self.styled_runs:
            for style in (run.style,) * len(run.text):
                sup_sub = style.sup_sub
                if sup_sub is None:
                    shifts.append(0.0)
                elif sup_sub.shift is not None:
                    shifts.append(sup_sub.shift)
                else:
                    shifts.append(
                        1.0 - downscale / 2.0 if sup_sub.direction > 0 else -downscale / 2.0
                    )
        return tuple(shifts)

    @property
    def used_cutters(self) -> Tuple[float, ...]:
        """Every distinct cutter diameter this line engraves with, ascending.

        The line's own cutter, its ``<b>`` bold rung (when one resolved), and
        each ``<scale:N>`` run's effective-height cutter. This is the layer
        set :func:`build_cutter_pen_map` keys, so a scaled run's strokes land
        on their own tool's pen (and per-cutter PLT file) exactly like the
        bold rung does.
        """
        cutters = {self.cutter_diameter}
        if self.bold_cutter_diameter is not None:
            cutters.add(self.bold_cutter_diameter)
        cutters.update(self.per_char_cutters)
        return tuple(sorted(cutters))

    @property
    def per_char_cutters(self) -> Tuple[float, ...]:
        """Per-character cutter diameters aligned with :attr:`plain_text`.

        Each character's engraving tool: a ``<scale:N>`` character takes the
        ladder cutter for its effective height (``N * nominal_text_height``)
        -- the tool that size of text gets authored on its own, so a downscaled
        run engraves *lighter*, not heavier -- a bare ``<sup>``/``<sub>``
        character takes the same ladder at its downscaled height
        (``supersubscript_downscale * nominal_text_height``, the composition
        ``scale_values * sup_sub_scales``), a ``<b>`` character takes
        :attr:`bold_cutter_diameter` (when one resolved), and every other
        character takes the line's :attr:`cutter_diameter`. A bold+scaled
        character takes the bold rung at the effective height.

        This is the array the kerning per-pair floor, the underline's per-run
        cutter, the per-cutter pen split and the layout report all read; the
        underline gate splits a run wherever this array changes (one underline
        cannot span two tools). Derived from each character's *scale factor*
        (never a stored positional array), so it stays correct when a pre-pass
        rewrites the line's text. Empty tuple on a markup-free line (no
        per-character variation -- the renderers' fast path).
        """
        if not self.styled_runs:
            return ()
        scales = self.scale_values
        # The <sup>/<sub> downscale composes multiplicatively with <scale:N>
        # into one effective silhouette factor per character (the same
        # composition glyph_render_plan uses for the geometry scale).
        sup_scales = self.sup_sub_scales
        if sup_scales:
            scales = tuple(
                scale * (sup_scales[index] if index < len(sup_scales) else 1.0)
                for index, scale in enumerate(scales)
            )
        bolds = self.bold_flags
        bold_cutter = self.bold_cutter_diameter
        # A bold rung at the effective height is only needed when some
        # character is BOTH bold and scaled; asking for it otherwise would log
        # a meaningless "no larger rung" warning on a line with no bold run.
        scaled_bold = bold_cutter is not None and any(
            scale != 1.0 and index < len(bolds) and bolds[index]
            for index, scale in enumerate(scales)
        )
        ladder: dict[float, Tuple[float, float]] = {}
        cutters: list[float] = []
        for index in range(len(self.plain_text)):
            scale = scales[index] if index < len(scales) else 1.0
            is_bold = bold_cutter is not None and index < len(bolds) and bolds[index]
            if scale != 1.0:
                if scale not in ladder:
                    ladder[scale] = scale_cutter_ladder(self, scale, need_bold=scaled_bold)
                plain_cutter, scaled_bold_cutter = ladder[scale]
                cutters.append(scaled_bold_cutter if is_bold else plain_cutter)
            elif is_bold and bold_cutter is not None:
                cutters.append(bold_cutter)
            else:
                cutters.append(self.cutter_diameter)
        return tuple(cutters)

    def _bold_glyph_scale(self) -> float:
        """Return the uniform scale a bold glyph renders at, relative to plain.

        The centerline contract: a glyph's stored geometry is the path the
        *design* cutter follows, so the line's plain run scales it to
        ``nominal - cutter`` and the bold run scales the same geometry to
        ``nominal - bold_cutter``. The ratio ``<= 1`` (a thicker tool
        engraves a fatter stroke, so the centerlines shrink to keep the
        engraved silhouette inside its box). ``1.0`` when the line has no
        bold run, the bold cutter equals the plain one (top-of-inventory
        fallback), or the geometry would leave no material.
        """
        if self.bold_cutter_diameter is None:
            return 1.0
        plain = self.nominal_text_height - self.cutter_diameter
        bold = self.nominal_text_height - self.bold_cutter_diameter
        if plain <= 0.0 or bold <= 0.0:
            return 1.0
        return min(1.0, bold / plain)


@dataclass(frozen=True)
class ResolvedLabel:
    """A fully resolved label specification.

    Attributes:
        id: Unique identifier for this label.
        count: Number of instances to produce.
        width: Label width in inches (never None).
        height: Label height in inches (never None).
        margin: Universal margin in inches (fallback for h_margin, v_margin).
        h_margin: Horizontal margin in inches (left and right edges).
            Falls back to margin when unset in the schema.
        v_margin: Vertical margin in inches (top and bottom edges).
            Falls back to margin when unset in the schema.
        hole_margin: Hole margin in inches. The closest point of a hole
            circle to the label edge sits this far from the edge. Defaults
            to 0.0 (circle tangent to the edge) for manually constructed
            labels; the resolution engine always populates the cascaded
            value.
        holes: List of resolved hole specifications.
        content: List of resolved text lines.
        min_hole_margin: Lower bound (in inches) applied to ``hole_margin``
            during text-hole collision avoidance. ``None`` (the default)
            allows shrinking the hole margin all the way to ``0.0``; the
            resolution engine always populates the cascaded value.
        collision_compress_by_line: Mapping of line index to per-line
            horizontal scale in ``(0.0, 1.0]`` when text-hole collision
            avoidance had to compress specific lines away from drill holes.
            An empty dict (the default) means no collision-driven compression
            was applied. Each line can have its own compression scale;
            lines not in the dict use scale ``1.0`` (uncompressed). Set by
            the renderer via ``dataclasses.replace``; never sourced from the
            YAML schema.
        cutter_downsize_by_line: Mapping of line index to the ``(original,
            final)`` cutter diameters in inches for lines whose *automatic*
            cutter was reduced by the compression-driven cutter reduction
            (see :mod:`plt_optimizer.generate.cutter_downsize`). An empty
            dict (the default) means no line was downsized. Each entry's
            ``final`` cutter is strictly below its ``original`` (the swap is
            one-way: a downsized cutter is never enlarged again within the
            same line), and the line's :attr:`ResolvedTextLine.toolpath_text_height`
            already reflects the final cutter. Set by the export pre-pass via
            ``dataclasses.replace``; never sourced from the YAML schema.
        global_compress_by_line: Mapping of line index to a shared horizontal
            scale in ``(0.0, 1.0)`` applied by the per-label compression
            sharing pre-pass (see
            :mod:`plt_optimizer.generate.h_compress`). When
            ``ResolvedTextLine.h_compress_global`` is enabled and one line of
            a ``nominal_text_height`` group is horizontally compressed, every
            eligible line of that group receives the group's minimum scale
            (clamped to its own ``1 - max_h_compress`` budget floor). An empty
            dict (the default) means no shared compression was applied; lines
            not in the dict use scale ``1.0``. Applied by the renderer on top
            of :attr:`collision_compress_by_line`. Set by the export pre-pass
            via ``dataclasses.replace``; never sourced from the YAML schema.
        hole_text_collision_distance: Minimum air gap in inches kept
            between the engraved text stroke and the engraved drill-hole
            stroke (cascaded label -> job, default ``0.15``). Combined
            with the stroke floor ``0.5 * (hole_cutter_diameter +
            line.cutter_diameter)`` this forms the effective collision
            threshold used by the renderer.
        hole_cutter_diameter: Cutter diameter in inches used to cut label
            boundaries and drill holes (from ``tools.json``
            ``boundary_hole_cutter_size``, snapped to the inventory;
            default ``0.015``). Used only to compute the collision stroke
            floor -- rendering itself emits pure geometry layers.
        text_chunk_mode: Plate-space text optimization granularity
            (``"line"`` or ``"word"``, cascaded from the job; default
            ``"line"``). Consumed by the renderer (chunk-record
            granularity) and the plate-space optimizer.
        plate_id: Optional id of the plate this label is pinned to (from
            ``LabelSpec.plate_id``; set by plate-level replacement
            expansion). Pinned labels pack exclusively onto that plate,
            which then accepts no other labels. ``None`` (the default)
            packs normally across all plates.
        material: Stock material name this label must be cut from (from
            the ``material`` cascade, label -> job). Labels sharing a
            material pack together and a plate never mixes materials (see
            :func:`plt_optimizer.generate.layout.generate_layout`).
            ``None`` (the default) means unset: the label groups with the
            job-level material, and a job without materials packs exactly
            like a single material group.
        use_baseline_spacing: Stack this label's text lines by
            baseline-to-baseline pitch (``nominal_text_height +
            line_spacing``) instead of by rendered ink box (from the
            ``use_baseline_spacing`` cascade, label -> job, default
            ``True``). Under baseline spacing a line's descenders hang into
            the gap below it, so every gap on the label reads as one uniform
            visual spacing; the render-time clamp
            (:func:`fit_baseline_spacing_to_margins`) shrinks the spacings
            only when the descender-extended block no longer fits the inner
            area. ``False`` reproduces the historical ink-box stacking.
        v_center_by_content: Center the text block by its rendered ink
            (``True``, historical -- the deepest descender is charged to the
            block, so lines with descenders ride high) or by its
            baseline-to-capline band (``False``, the default -- the visible
            text is optically centered and descenders hang into the bottom
            margin for the relief to handle) (from the
            ``v_center_by_content`` cascade, label -> job, default ``False``).
            Band centering is active only together with an enabled
            :attr:`auto_descending_character_adjustment`; see
            :attr:`center_text_by_band`.
        auto_descending_character_adjustment: Permit automatic vertical
            downscaling + raising of descending glyphs that come close to
            colliding with glyphs on the following line (from the
            ``auto_descending_character_adjustment`` cascade, label -> job,
            default ``True``). Applied per triggering glyph by the renderer.
        descending_character_v_scale: Smallest vertical scale a triggering
            descending glyph may be squeezed to, about its own baseline (from
            the ``descending_character_v_scale`` cascade, default ``0.5``).
        descending_character_raise: Largest raise a triggering descending
            glyph may receive, as a fraction of its own descender depth (from
            the ``descending_character_raise`` cascade, default ``0.7``).
        descending_character_clearance: Extra engraved-stroke air gap demanded
            between a descending glyph and the glyph below it, as a unitless
            factor of the stroke floor -- the required gap is
            ``floor * (1 + clearance)`` (from the
            ``descending_character_clearance`` cascade, default ``0.0``).
        descending_character_reciprocal_scale: Smallest top-down scale the
            colliding glyphs on the following line may be squeezed to, about
            their own lowest ink point (from the
            ``descending_character_reciprocal_scale`` cascade, default
            ``0.5``; ``1.0`` = reciprocal downscaling forbidden).
    """

    id: str
    count: int
    width: float
    height: float
    margin: float
    h_margin: float
    v_margin: float
    hole_margin: float = 0.0
    holes: list[ResolvedHoleSpec] = field(default_factory=list)
    content: list[ResolvedTextLine] = field(default_factory=list)
    min_hole_margin: Optional[float] = None
    collision_compress_by_line: dict[int, float] = field(default_factory=dict)
    cutter_downsize_by_line: dict[int, tuple[float, float]] = field(default_factory=dict)
    global_compress_by_line: dict[int, float] = field(default_factory=dict)
    hole_text_collision_distance: float = DEFAULT_HOLE_TEXT_COLLISION_DISTANCE
    hole_cutter_diameter: float = DEFAULT_BOUNDARY_HOLE_CUTTER
    text_chunk_mode: str = "line"
    plate_id: Optional[str] = None
    material: Optional[str] = None
    use_baseline_spacing: bool = DEFAULT_USE_BASELINE_SPACING
    v_center_by_content: bool = DEFAULT_V_CENTER_BY_CONTENT
    auto_descending_character_adjustment: bool = DEFAULT_AUTO_DESCENDING_CHARACTER_ADJUSTMENT
    descending_character_v_scale: float = DEFAULT_DESCENDING_CHARACTER_V_SCALE
    descending_character_raise: float = DEFAULT_DESCENDING_CHARACTER_RAISE
    descending_character_clearance: float = DEFAULT_DESCENDING_CHARACTER_CLEARANCE
    descending_character_reciprocal_scale: float = DEFAULT_DESCENDING_CHARACTER_RECIPROCAL_SCALE

    @property
    def center_text_by_band(self) -> bool:
        """Whether the renderer centers the text block by its cap band.

        Band centering exists so descenders can hang into the bottom margin
        and be adjusted by the descending-character relief; with the relief
        disabled nothing would move them, so the historical ink centering is
        kept and :attr:`v_center_by_content` is inert.
        """
        return self.auto_descending_character_adjustment and not self.v_center_by_content


# ---------------------------------------------------------------------------
# Margin precedence helper
# ---------------------------------------------------------------------------
def fit_line_spacing_to_margins(
    line_heights: Sequence[float],
    line_spacings: Sequence[float],
    available_height: float,
) -> list[float]:
    """Shrink inter-line spacing so a stacked text block fits the margin box.

    Margins take precedence over requested line spacing: when the stacked
    block (sum of line heights plus inter-line spacing) exceeds the
    available inner height, the spacings are scaled down proportionally
    (down to a floor of ``0.0``) until the block fits exactly.

    Args:
        line_heights: Rendered (or nominal) height of each text line in
            inches. Length ``n``.
        line_spacings: Extra spacing applied *after* each line except the
            last, in inches. Length ``n - 1``.
        available_height: Inner content height in inches
            (label height minus both margins).

    Returns:
        A new list of inter-line spacings. Equal to the input when the
        block already fits; proportionally reduced otherwise. Spacing is
        never increased and never negative. Note that if the line heights
        alone exceed ``available_height``, all spacings collapse to ``0.0``
        and the block still overflows (text height is never reduced).
    """
    spacings = [max(0.0, float(s)) for s in line_spacings]
    total_height = sum(float(h) for h in line_heights) + sum(spacings)
    excess = total_height - available_height
    if excess <= 0.0 or not spacings:
        return spacings

    total_spacing = sum(spacings)
    if total_spacing <= 0.0:
        return spacings

    # Proportional scale-down so the total reduction equals the excess
    # (capped at the total spacing available, i.e. a floor of zero).
    factor = max(0.0, (total_spacing - excess) / total_spacing)
    return [s * factor for s in spacings]


# ---------------------------------------------------------------------------
# Baseline-spacing geometry (``use_baseline_spacing``)
#
# Baseline spacing stacks lines by the distance between their baselines
# (``pitch_i = nominal_text_height_i + line_spacing_i``) instead of by their
# rendered ink boxes. Each line's ink spans ``[b_i - D_i, b_i + A_i]`` where
# ``b_i`` is its baseline, ``A_i`` the ascender extent (ink above the
# baseline) and ``D_i`` the descender depth (ink hanging below it), all in
# inches. Because the baselines descend monotonically, the block's ink height
# is ``max_i(b_i + A_i) - min_i(b_i - D_i)``: a maximum and a minimum of
# affine functions of the spacings, hence convex, continuous and
# non-decreasing in every spacing. That monotonicity is what lets the fit
# helpers below solve the block height by bisection -- the closed-form
# proportional scale-down of :func:`fit_line_spacing_to_margins` is only
# valid for the linear ink-box model.
# ---------------------------------------------------------------------------
_BISECTION_TOLERANCE: float = 1e-9
_BISECTION_MAX_ITERATIONS: int = 100


def baseline_pitch(line_heights: Sequence[float], line_spacings: Sequence[float]) -> list[float]:
    """Return the baseline-to-baseline pitch of each inter-line gap.

    The pitch of the gap below line ``i`` is ``line_heights[i] +
    line_spacings[i]``: the nominal (cap) height carries the baseline down to
    the next line's cap line, and ``line_spacing`` is the engraved-stroke air
    gap measured from that baseline to the next line's ink top. Lines without
    descenders therefore produce exactly the ink-box geometry, and a descender
    of depth ``d`` shortens the visual gap by exactly ``d``.

    Args:
        line_heights: Nominal text height of each line in inches. Length ``n``.
        line_spacings: Spacing applied *after* each line except the last, in
            inches. Length ``n - 1`` (longer input is truncated).

    Returns:
        One pitch per gap (length ``min(n - 1, len(line_spacings))``), in
        inches. Negative spacings are clamped to ``0.0``.
    """
    return [
        float(height) + max(0.0, float(spacing))
        for height, spacing in zip(line_heights, line_spacings)
    ]


def baseline_offsets(line_heights: Sequence[float], line_spacings: Sequence[float]) -> list[float]:
    """Return each line's baseline Y in a baseline-stacked block.

    Coordinates are ``+y`` up with the **first** baseline at ``0.0``, so the
    offsets are non-increasing (each line sits one pitch below the previous
    one). Callers translate the block as a whole; only the relative offsets
    matter.

    Args:
        line_heights: Nominal text height of each line in inches. Length ``n``.
        line_spacings: Spacing below each line except the last, in inches.

    Returns:
        One baseline Y per line (length ``len(line_heights)``), in inches.
    """
    offsets = [0.0]
    for pitch in baseline_pitch(line_heights, line_spacings):
        offsets.append(offsets[-1] - pitch)
    return offsets


def baseline_block_height(
    line_heights: Sequence[float],
    ascenders: Sequence[float],
    descenders: Sequence[float],
    line_spacings: Sequence[float],
    *,
    charge_descenders: bool = True,
) -> float:
    """Return the ink height of a baseline-stacked text block.

    Args:
        line_heights: Nominal text height of each line in inches. Length ``n``.
        ascenders: Ink extent above each line's baseline, in inches (``A_i``,
            non-negative). Length ``n``.
        descenders: Ink depth below each line's baseline, in inches (``D_i``,
            non-negative). Length ``n``.
        line_spacings: Spacing below each line except the last, in inches.
        charge_descenders: When ``True`` (the default) the block's bottom is
            its deepest descender, so descenders claim a share of the inner
            content height. When ``False`` the bottom is the **lowest
            baseline** -- the block is measured as its baseline-to-capline
            band, which is what a band-centered label fits to the margins
            (descenders hang into the bottom margin, where the
            descending-character relief handles them).

    Returns:
        The distance from the block's highest ink point to its lowest, in
        inches. Equal to the ink-box height when every descender is ``0.0``
        and every ascender equals its line's nominal height.
    """
    offsets = baseline_offsets(line_heights, line_spacings)
    top = max(offset + ascender for offset, ascender in zip(offsets, ascenders))
    if charge_descenders:
        bottom = min(offset - descender for offset, descender in zip(offsets, descenders))
    else:
        bottom = min(offsets)
    return top - bottom


def _solve_monotone_root(
    function: Callable[[float], float],
    lower: float,
    upper: float,
) -> float:
    """Bisect a non-decreasing function's root inside ``[lower, upper]``.

    Args:
        function: Non-decreasing objective; the root is where it reaches
            ``0.0``.
        lower: Lower bracket; ``function(lower) <= 0`` is expected (the
            largest admissible value).
        upper: Upper bracket; ``function(upper) >= 0`` is expected.

    Returns:
        The largest admissible value (``function(value) <= 0``) found, within
        :data:`_BISECTION_TOLERANCE`.
    """
    if function(lower) >= 0.0:
        return lower
    for _ in range(_BISECTION_MAX_ITERATIONS):
        middle = (lower + upper) / 2.0
        if upper - lower <= _BISECTION_TOLERANCE:
            break
        if function(middle) <= 0.0:
            lower = middle
        else:
            upper = middle
    return lower


def fit_baseline_spacing_to_margins(
    line_heights: Sequence[float],
    ascenders: Sequence[float],
    descenders: Sequence[float],
    line_spacings: Sequence[float],
    available_height: float,
    *,
    charge_descenders: bool = True,
) -> list[float]:
    """Shrink baseline-stacked spacing so the descender-extended block fits.

    The baseline-spacing counterpart of :func:`fit_line_spacing_to_margins`:
    margins take precedence over requested spacing, and the spacings are
    scaled down by one common factor (never increased, floor ``0.0``) until
    the block's **ink** height -- which includes the descenders hanging into
    the gaps -- fits ``available_height``. Because that height is convex and
    non-decreasing in the scale factor, the factor is found by bisection
    rather than the linear closed form.

    Args:
        line_heights: Nominal text height of each line in inches. Length ``n``.
        ascenders: Ink extent above each line's baseline, in inches.
        descenders: Ink depth below each line's baseline, in inches.
        line_spacings: Spacing below each line except the last, in inches.
        available_height: Inner content height in inches (label height minus
            both vertical margins).

    Returns:
        A new list of spacings. Equal to the (clamped) input when the block
        already fits; proportionally reduced otherwise. When the block
        overflows even with zero spacing, every spacing collapses to ``0.0``
        and the block still overflows (text height is never reduced).
    """
    spacings = [max(0.0, float(s)) for s in line_spacings]
    if not spacings:
        return spacings

    def height_at(factor: float) -> float:
        return baseline_block_height(
            line_heights,
            ascenders,
            descenders,
            [s * factor for s in spacings],
            charge_descenders=charge_descenders,
        )

    if height_at(1.0) <= available_height:
        return spacings
    total_spacing = sum(spacings)
    if total_spacing <= 0.0:
        return spacings

    factor = _solve_monotone_root(lambda t: height_at(t) - available_height, 0.0, 1.0)
    return [s * factor for s in spacings]


def solve_baseline_spacing_to_fill(
    line_heights: Sequence[float],
    ascenders: Sequence[float],
    descenders: Sequence[float],
    available_height: float,
    *,
    charge_descenders: bool = True,
) -> float:
    """Return the uniform spacing that fills ``available_height`` exactly.

    Auto line spacing under baseline stacking (explicit ``v_margin``): every
    gap gets the same spacing, chosen so the block's ink height -- descenders
    included -- equals the inner content height. This is the render-accurate
    counterpart of the ink-box arithmetic in
    :func:`_resolve_auto_line_spacing`, which divides the space freed by the
    *nominal* heights and therefore ignores how far a line's descenders reach
    below its baseline.

    Args:
        line_heights: Nominal text height of each line in inches. Length ``n``.
        ascenders: Ink extent above each line's baseline, in inches.
        descenders: Ink depth below each line's baseline, in inches.
        available_height: Inner content height in inches.

    Returns:
        The uniform spacing in inches (``0.0`` when the block overflows even
        with no spacing, mirroring the ink-box path's ``max(0.0, ...)``).
    """
    if len(line_heights) < 2:
        return 0.0

    def height_at(spacing: float) -> float:
        gaps = [spacing] * (len(line_heights) - 1)
        return baseline_block_height(
            line_heights,
            ascenders,
            descenders,
            gaps,
            charge_descenders=charge_descenders,
        )

    if height_at(0.0) >= available_height:
        return 0.0
    return _solve_monotone_root(lambda s: height_at(s) - available_height, 0.0, available_height)


def solve_baseline_spacing_to_ratio(
    label_height: float,
    line_heights: Sequence[float],
    ascenders: Sequence[float],
    descenders: Sequence[float],
    interline_to_top_bottom_ratio: float,
    *,
    charge_descenders: bool = True,
) -> tuple[float, float]:
    """Return ``(interline_spacing, v_margin)`` for ratio-balanced gaps.

    Auto line spacing **and** auto ``v_margin`` under baseline stacking: the
    top and bottom gaps are equal, every inter-line gap is that gap scaled by
    ``interline_to_top_bottom_ratio``, and the three together fill the label.
    The equation solved is ``label_height = 2 * gap + block_height(pitch_i =
    line_heights[i] + ratio * gap)`` -- the render-accurate form of the
    nominal-height division used by :func:`_resolve_auto_line_spacing`.

    Args:
        label_height: Final label height in inches (outer boundary).
        line_heights: Nominal text height of each line in inches. Length ``n``.
        ascenders: Ink extent above each line's baseline, in inches.
        descenders: Ink depth below each line's baseline, in inches.
        interline_to_top_bottom_ratio: Inter-line gap as a multiple of the
            top/bottom gap. ``1.0`` makes every gap equal.

    Returns:
        ``(interline_spacing, v_margin)`` in inches. Single-line content
        returns ``(0.0, label_height / 2.0)`` (no gaps; the ink centers).
    """
    if len(line_heights) < 2:
        return 0.0, label_height / 2.0

    ratio = max(0.0, float(interline_to_top_bottom_ratio))

    def total_at(gap: float) -> float:
        gaps = [gap * ratio] * (len(line_heights) - 1)
        return 2.0 * gap + baseline_block_height(
            line_heights,
            ascenders,
            descenders,
            gaps,
            charge_descenders=charge_descenders,
        )

    gap = _solve_monotone_root(lambda g: total_at(g) - label_height, 0.0, label_height)
    return gap * ratio, gap


def compute_horizontal_scale(
    rendered_width: float,
    available_width: float,
    max_h_compress: float,
) -> float:
    """Compute the uniform horizontal scale factor for one rendered line.

    Margin precedence for width: when a rendered line is wider than the
    label's inner content area, the line is compressed horizontally (never
    stretched, never taller) down to the available width. Compression is
    bounded by the resolved ``max_h_compress`` fraction, so a line is never
    squeezed below ``(1 - max_h_compress)`` of its natural width. When the
    limit cannot fully resolve the overflow, the returned factor still
    compresses as far as allowed and the caller is responsible for logging
    the residual overflow.

    Args:
        rendered_width: Measured rendered line width in inches (must be
            positive for any scaling to apply).
        available_width: Inner content width in inches (label width minus
            both margins).
        max_h_compress: Maximum compression fraction in ``[0.0, 1.0]``.
            ``0.0`` disables compression; ``0.5`` allows squeezing to 50%
            of natural width.

    Returns:
        A scale factor in ``[1 - max_h_compress, 1.0]``. ``1.0`` means no
        compression (the line fits or compression is disabled).
    """
    limit = min(max(0.0, max_h_compress), 1.0)
    if limit <= 0.0 or rendered_width <= 0.0:
        return 1.0
    if rendered_width <= available_width:
        return 1.0
    needed = available_width / rendered_width
    floor = 1.0 - limit
    return max(needed, floor)


def compute_horizontal_offset(
    rendered_width: float,
    available_width: float,
    margin: float,
    alignment: str,
) -> float:
    """Compute the target left-edge X for a rendered line inside the margin box.

    The label's inner content area spans ``[margin, margin +
    available_width]``. The alignment anchors the rendered line within
    that span:

    - ``"left"``: the line's left-most point sits precisely at the left
      margin (``margin``).
    - ``"right"``: the line's right-most point sits precisely at the right
      margin (``margin + available_width``).
    - ``"center"`` (default): the line is centered within the span.

    When the line is wider than the available width (e.g. compression is
    disabled), ``"center"`` overflows symmetrically and ``"left"`` /
    ``"right"`` keep their respective margin edges anchored, spilling out
    the opposite side.

    Args:
        rendered_width: Measured rendered line width in inches.
        available_width: Inner content width in inches (label width minus
            both margins).
        margin: Resolved label margin in inches (left inner edge position).
        alignment: One of ``"left"``, ``"center"`` or ``"right"``. Unknown
            values fall back to ``"center"``.

    Returns:
        The X coordinate where the line's left-most point (its ``min_x``)
        should be translated to.
    """
    if alignment == "left":
        return margin
    if alignment == "right":
        return margin + available_width - rendered_width
    return margin + (available_width - rendered_width) / 2.0


def _resolve_auto_line_spacing(
    content: list[ResolvedTextLine],
    label_height: float,
    v_margin_explicit: Optional[float],
    boundary_hole_cutter: float,
    label_id: str,
    interline_to_top_bottom_ratio: float = 1.0,
    use_baseline_spacing: bool = False,
    extents_probe: Optional[LineExtentsProbe] = None,
    charge_descenders: bool = True,
) -> tuple[list[ResolvedTextLine], Optional[float]]:
    """Resolve any auto line spacing values in content.

    When ``line_spacing="auto"``, spacing is calculated such that all lines
    have equal functional spacing (or proportional spacing if a ratio > 1.0
    is provided). If ``v_margin`` is explicitly specified, it is honored and
    ``line_spacing`` is calculated to fill the remaining space. If ``v_margin``
    is not explicitly specified, the functional ``v_margin`` is set equal to
    the inter-line spacing (or scaled by the ratio).

    Cutter width is a horizontal measure (stroke width) and does not affect
    vertical line spacing. The calculation divides available vertical space
    among inter-line gaps and top/bottom margins, with the ratio controlling
    their relative sizes.

    Baseline spacing (``use_baseline_spacing`` with an ``extents_probe``)
    fills the same space against the block's **ink** height instead of the sum
    of nominal heights: each line's real ascender/descender extents are
    measured through the probe, so a line whose glyphs hang below the baseline
    claims its share of the space up front. The auto spacing is then the value
    that makes the descender-extended block fill the inner area exactly, which
    keeps the render-time clamp (:func:`fit_baseline_spacing_to_margins`)
    silent. Without a probe the nominal-height arithmetic below runs unchanged
    (no renders are paid for).

    Args:
        content: Fully resolved text lines (with line_spacing=-1.0 for auto).
        label_height: Final label height in inches (outer boundary).
        v_margin_explicit: The explicitly specified v_margin, or None if
            v_margin was determined by cascading through margins/defaults.
        boundary_hole_cutter: Cutter diameter used for hole/boundary cutting.
        label_id: Identifier used in log messages.
        interline_to_top_bottom_ratio: Ratio controlling inter-line spacing
            relative to top/bottom margins. Default 1.0 makes all gaps equal.
            Values > 1.0 increase inter-line spacing at the expense of
            top/bottom margins.
        use_baseline_spacing: Resolve auto spacing against the baseline-stacked
            ink height (see above). Takes effect only with ``extents_probe``.
        extents_probe: Optional probe measuring a line's
            ``(ascender, descender)`` ink extents in inches.
        charge_descenders: When ``False`` (band-centered labels) the fill
            equation sizes the space against the baseline-to-capline band, so
            descenders are NOT charged to the inner area -- they hang into the
            bottom margin for the descending-character relief to handle.

    Returns:
        A tuple of (updated_content, calculated_v_margin). calculated_v_margin
        is non-None only when v_margin was not explicitly specified (auto mode),
        in which case it should replace the default v_margin.
    """
    if len(content) < 1:
        return content, None

    # Check if any lines have auto spacing (sentinel value -1.0)
    auto_indices = [i for i, line in enumerate(content) if line.line_spacing < 0.0]
    if not auto_indices:
        return content, None

    # Single-line content with auto spacing: zero spacing, no lines after it
    if len(content) == 1:
        if auto_indices:
            updated_content = [replace(content[0], line_spacing=0.0)]
            return updated_content, None
        else:
            return content, None

    # If only some lines have auto spacing (mixed), convert all to auto for consistency
    if len(auto_indices) != len(content):
        logger.warning(
            "Label %s: mixed auto and explicit line_spacing detected; treating all lines as auto.",
            label_id,
        )
        auto_indices = list(range(len(content)))

    line_heights = [line.nominal_text_height for line in content]
    total_line_height = sum(line_heights)
    num_lines = len(content)

    # Baseline spacing measures the real ink extents once (callers memoize the
    # probe, so a label's lines render at most once) and solves the fill
    # equation against the descender-extended block height. Its pitch basis is
    # the line's cap height (``toolpath_text_height``) -- a per-font constant
    # independent of the line's own characters -- which is exactly what the
    # renderer stacks by.
    baseline_extents: Optional[tuple[list[float], list[float]]] = None
    baseline_heights: Optional[list[float]] = None
    if use_baseline_spacing and extents_probe is not None and num_lines > 1:
        measured = [extents_probe(line) for line in content]
        baseline_extents = ([a for a, _d in measured], [d for _a, d in measured])
        baseline_heights = [line.toolpath_text_height for line in content]

    calculated_v_margin: Optional[float] = None

    if v_margin_explicit is not None:
        # Explicit v_margin: calculate line_spacing to fill the remaining space
        available_height = label_height - 2.0 * v_margin_explicit
        if baseline_extents is not None and baseline_heights is not None:
            ascenders, descenders = baseline_extents
            calculated_spacing = solve_baseline_spacing_to_fill(
                baseline_heights,
                ascenders,
                descenders,
                available_height,
                charge_descenders=charge_descenders,
            )
        elif num_lines > 1:
            calculated_spacing = max(0.0, (available_height - total_line_height) / (num_lines - 1))
        else:
            calculated_spacing = 0.0

        logger.info(
            "Label %s: auto line_spacing calculated as %.4fin "
            "(explicit v_margin=%.4fin, available_height=%.4fin, line_height=%.4fin).",
            label_id,
            calculated_spacing,
            v_margin_explicit,
            available_height,
            total_line_height,
        )
    else:
        # Auto v_margin: calculate gaps with optional ratio adjustment
        # When ratio = 1.0, all gaps are equal (top margin = inter-line = bottom margin).
        # When ratio > 1.0, inter-line gaps are larger at the expense of top/bottom margins.
        #
        # Formula:
        #   top_bottom_gap = (height - line_total) / (2 + (num_lines - 1) * ratio)
        #   interline_gap = ratio * top_bottom_gap
        #
        available_space = label_height - total_line_height
        if baseline_extents is not None and baseline_heights is not None:
            ascenders, descenders = baseline_extents
            calculated_spacing, top_bottom_gap = solve_baseline_spacing_to_ratio(
                label_height,
                baseline_heights,
                ascenders,
                descenders,
                interline_to_top_bottom_ratio,
                charge_descenders=charge_descenders,
            )
        elif num_lines > 1:
            # Multiple lines: account for (num_lines - 1) inter-line gaps
            top_bottom_gap = max(
                0.0,
                available_space / (2.0 + (num_lines - 1) * interline_to_top_bottom_ratio),
            )
            calculated_spacing = interline_to_top_bottom_ratio * top_bottom_gap
        else:
            # Single line: top and bottom gaps only
            top_bottom_gap = available_space / 2.0
            calculated_spacing = 0.0

        # Set calculated_v_margin to the top/bottom gap value
        calculated_v_margin = top_bottom_gap

        logger.info(
            "Label %s: auto line_spacing and v_margin calculated as "
            "line_spacing=%.4fin, v_margin=%.4fin "
            "(auto v_margin mode, ratio=%.2f, label_height=%.4fin, line_height=%.4fin).",
            label_id,
            calculated_spacing,
            top_bottom_gap,
            interline_to_top_bottom_ratio,
            label_height,
            total_line_height,
        )

    # Replace sentinel values with calculated spacing
    updated_content = [
        replace(line, line_spacing=calculated_spacing) if i in auto_indices else line
        for i, line in enumerate(content)
    ]

    return updated_content, calculated_v_margin


def _fit_content_to_margins(
    content: list[ResolvedTextLine],
    label_height: float,
    margin: float,
    label_id: str,
    use_baseline_spacing: bool = False,
    extents_probe: Optional[LineExtentsProbe] = None,
    charge_descenders: bool = True,
) -> list[ResolvedTextLine]:
    """Clamp resolved line spacing so text respects the label margins.

    Uses nominal text heights for the fit estimate; the renderer applies a
    final safety check against measured glyph heights.

    Under baseline spacing (``use_baseline_spacing`` with an
    ``extents_probe``) the estimate uses the baseline-stacked **ink** height
    (:func:`fit_baseline_spacing_to_margins`) instead of the sum of nominal
    heights, so a line's descenders claim their space in the resolved
    ``line_spacing`` values. That keeps the resolved spacing equal to what the
    renderer will actually engrave (the layout report compares the two) and
    keeps the renderer's own clamp silent.

    Args:
        content: Fully resolved text lines for the label.
        label_height: Final label height in inches (outer boundary).
        margin: Resolved label margin in inches.
        label_id: Identifier used in log messages.
        use_baseline_spacing: Fit against the baseline-stacked ink height.
            Takes effect only with ``extents_probe``.
        extents_probe: Optional probe measuring a line's
            ``(ascender, descender)`` ink extents in inches.
        charge_descenders: When ``False`` (band-centered labels) the fit
            measures the block as its baseline-to-capline band, so descenders
            are not charged to the inner area and the spacing is not shrunk to
            make them fit -- they hang into the bottom margin for the relief.

    Returns:
        The original content list when no adjustment is needed, otherwise
        a new list with reduced ``line_spacing`` values.
    """
    if len(content) < 2:
        return content

    available_height = label_height - (2 * margin)
    heights = [line.nominal_text_height for line in content]
    spacings = [line.line_spacing for line in content[:-1]]

    extents = _measure_extents(content, use_baseline_spacing, extents_probe)
    if extents is not None:
        ascenders, descenders = extents
        # The pitch basis is the cap height, matching the renderer's stacking
        # (see ``_render_positioned_lines``); the nominal heights above stay
        # the ink-box basis for the historical path and the overflow warning.
        adjusted = fit_baseline_spacing_to_margins(
            [line.toolpath_text_height for line in content],
            ascenders,
            descenders,
            spacings,
            available_height,
            charge_descenders=charge_descenders,
        )
    else:
        adjusted = fit_line_spacing_to_margins(heights, spacings, available_height)
    if all(math.isclose(a, b, abs_tol=1e-9) for a, b in zip(adjusted, spacings)):
        return content

    logger.debug(
        "Label %s: line_spacing reduced from %s to %s to preserve margin "
        "%.3fin (available inner height %.3fin).",
        label_id,
        [round(s, 4) for s in spacings],
        [round(s, 4) for s in adjusted],
        margin,
        available_height,
    )
    if sum(heights) > available_height:
        logger.warning(
            "Label %s: text lines alone (%.3fin) exceed the available inner "
            "height (%.3fin); margins cannot be fully preserved without "
            "reducing text height.",
            label_id,
            sum(heights),
            available_height,
        )

    return [
        replace(line, line_spacing=adjusted[i]) if i < len(adjusted) else line
        for i, line in enumerate(content)
    ]


def _measure_extents(
    content: Sequence[ResolvedTextLine],
    use_baseline_spacing: bool,
    extents_probe: Optional[LineExtentsProbe],
) -> Optional[tuple[list[float], list[float]]]:
    """Measure every line's ``(ascender, descender)`` ink extents, if enabled.

    Shared by the two spacing fitters so a label's lines are measured through
    one probe call per line (the production probe memoizes per line, so labels
    sharing content cost nothing).

    Args:
        content: Fully resolved text lines for one label.
        use_baseline_spacing: When False the probe is never called.
        extents_probe: Optional probe measuring a line's ink extents.

    Returns:
        ``(ascenders, descenders)`` in inches, or ``None`` when baseline
        spacing is disabled, no probe is wired, or the label has a single line
        (no gaps to fit).
    """
    if not use_baseline_spacing or extents_probe is None or len(content) < 2:
        return None
    measured = [extents_probe(line) for line in content]
    return ([a for a, _d in measured], [d for _a, d in measured])


# ---------------------------------------------------------------------------
# Resolution engine
# ---------------------------------------------------------------------------
def _resolve_margins(
    label_input: LabelSpec | JobSpec,
    job: JobSpec,
) -> tuple[float, float]:
    """Resolve h_margin and v_margin with fallback to margin then defaults.

    Cascade order for h_margin:
        label.h_margin → job.h_margin → label.margin → job.margin → DEFAULT_MARGIN

    Cascade order for v_margin:
        label.v_margin → job.v_margin → label.margin → job.margin → DEFAULT_MARGIN

    Args:
        label_input: The label (or root-level job) being processed.
        job: The outer JobSpec providing fallback values.

    Returns:
        A tuple of (h_margin, v_margin) in inches, both guaranteed to be
        non-None and positive.
    """
    # Resolve h_margin
    h_margin = label_input.h_margin
    if h_margin is None:
        h_margin = job.h_margin
    if h_margin is None:
        h_margin = label_input.margin
    if h_margin is None:
        h_margin = job.margin
    if h_margin is None:
        h_margin = DEFAULT_MARGIN
    h_margin = float(h_margin)

    # Resolve v_margin
    v_margin = label_input.v_margin
    if v_margin is None:
        v_margin = job.v_margin
    if v_margin is None:
        v_margin = label_input.margin
    if v_margin is None:
        v_margin = job.margin
    if v_margin is None:
        v_margin = DEFAULT_MARGIN
    v_margin = float(v_margin)

    return h_margin, v_margin


def _resolve_holes(
    label_input: LabelSpec | JobSpec,
    job: JobSpec,
) -> list[ResolvedHoleSpec]:
    """Resolve holes with label-level precedence over job-level.

    Args:
        label_input: The label (or root-level job) being processed.
        job: The outer JobSpec providing fallback values.

    Returns:
        A list of fully resolved hole specifications.
    """
    raw_holes = label_input.holes if label_input.holes is not None else job.holes
    if not raw_holes:
        return []
    return [ResolvedHoleSpec(diameter=h.diameter, location=h.location.value) for h in raw_holes]


def _apply_reflow_line_caps(
    resolved_content: list[ResolvedTextLine],
    label_id: str,
) -> list[ResolvedTextLine]:
    """Validate and fan out ``optimize_line_content_max_lines`` per group.

    Runs over the *resolved* lines (so config injection and the full
    line -> label -> job cascade are already visible) and enforces the two
    authoring rules of the reflow line cap, then fans the agreed value out:

    1. a cap resolved onto some line while *no* line of the label has
       ``optimize_line_content`` enabled raises :class:`LineContentConfigError`
       (the cap governs a reflow group and this label forms none, so the
       declaration is meaningless). A cap cascading onto lines that are
       individually disabled is fine as long as the label has an enabled line
       somewhere -- the pre-pass reads the cap from the group's own lines;
    2. within one maximal run of consecutive enabled lines, two *different*
       non-``None`` caps raise (the group grows to one agreed ceiling);
    3. the group's single cap (else ``None``) is written onto every line of
       the group, so the reflow pre-pass reads it from the group's first line
       without re-scanning.

    Labels whose groups need no fan-out are returned unchanged (same line
    objects), keeping the pass free for the overwhelmingly common
    cap-less case.

    Args:
        resolved_content: The label's resolved text lines, in content order.
        label_id: Identifier used in the error messages.

    Returns:
        The resolved lines, with fan-out clones where a group's cap was set
        on only some of its lines.

    Raises:
        LineContentConfigError: On rule 1 or rule 2 violations.
    """
    groups: list[list[int]] = []
    current: list[int] = []
    for index, line in enumerate(resolved_content):
        if line.optimize_line_content:
            current.append(index)
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    caps_declared = any(
        line.optimize_line_content_max_lines is not None for line in resolved_content
    )
    if caps_declared and not groups:
        raise LineContentConfigError(
            f"label '{label_id}': 'optimize_line_content_max_lines' is set but "
            "'optimize_line_content' is not enabled on any line; the line cap "
            "only governs a reflow group, so enable the option (at the line, "
            "label or job level) or drop the cap"
        )

    updated: Optional[list[ResolvedTextLine]] = None
    for group in groups:
        caps: set[int] = set()
        for index in group:
            declared = resolved_content[index].optimize_line_content_max_lines
            if declared is not None:
                caps.add(declared)
        if len(caps) > 1:
            listed = ", ".join(str(value) for value in sorted(caps))
            raise LineContentConfigError(
                f"label '{label_id}': lines {group[0]}-{group[-1]} form one "
                "reflow group but declare conflicting "
                f"'optimize_line_content_max_lines' values ({listed}); "
                "one line's cap applies to the whole group, so "
                "declare a single value"
            )
        if not caps:
            continue
        cap = caps.pop()
        for index in group:
            line = resolved_content[index]
            if line.optimize_line_content_max_lines == cap:
                continue
            if updated is None:
                updated = list(resolved_content)
            updated[index] = replace(line, optimize_line_content_max_lines=cap)
    return updated if updated is not None else resolved_content


def glyph_render_plan(
    line: ResolvedTextLine,
) -> Optional[Tuple[Tuple[float, ...], Tuple[float, ...]]]:
    """Return the per-character (cutter diameter, geometry scale) plan for a line.

    The unified centerline contract for the ``<b>`` and ``<scale:N>`` markup
    (see ``AGENTS.md`` "Inline Bold" / "Inline Text Scaling"). A glyph's
    stored geometry is the path the *design* cutter follows; the line's
    plain run scales it to ``nominal - cutter`` so the engraved silhouette
    is exactly ``nominal`` tall with the plain tool. A run that engraves
    its silhouette ``s`` times the plain run's through cutter ``d`` needs
    the geometry scale

    ``g = (s * nominal - d) / (nominal - cutter)``

    (uniform, about the baseline -- ``AA`` arcs stay native). At ``s = 1``
    and ``d = bold_cutter`` this is exactly
    :meth:`ResolvedTextLine._bold_glyph_scale`, so ``<b>`` is the plan's
    special case; at ``s = N`` and ``d = cutter`` it is the pure markup
    scale. The resolution-time cutter step guarantees a positive numerator
    for every run, except on an explicit-``cutter_size`` line, where a
    degenerate factor clamps to :data:`MIN_SCALED_GLYPH_FACTOR` (WARNING).

    Args:
        line: The resolved text line (flags, cutters, nominal height).

    Returns:
        ``(cutter_diameters, glyph_scales)`` tuples aligned with
        :attr:`ResolvedTextLine.plain_text` (spaces included; the diameter
        is what kerning's per-pair stroke floor uses, the scale is the
        geometry transform), or ``None`` when the line carries neither a
        bold run with a bold cutter nor a scaled run (the renderers' fast
        path -- the historical call shape, bit-identical output).
    """
    bold_cutter = line.bold_cutter_diameter
    has_bold = line.has_bold and bold_cutter is not None
    if not has_bold and not line.has_scale and not line.has_sup_sub:
        return None
    scale_flags = line.scale_values
    # The <sup>/<sub> downscale composes multiplicatively with <scale:N>:
    # the effective silhouette factor drives the centerline scale below, so
    # a bare <sup> on plain text is exactly a <scale:d> run (the argumented
    # form contributes 1.0 here and rides the renderer's baseline shift).
    sup_flags = line.sup_sub_scales
    # The per-character engraving cutters (derived from each character's scale
    # factor, so they follow the text through a reflow): a scaled run's
    # effective-height ladder cutter, the bold rung, or the line's own tool.
    diameters = list(line.per_char_cutters)
    plain = line.nominal_text_height - line.cutter_diameter
    scales: List[float] = []
    clamped = False
    for index, diameter in enumerate(diameters):
        scale = scale_flags[index] if index < len(scale_flags) else 1.0
        scale *= sup_flags[index] if index < len(sup_flags) else 1.0
        factor = (scale * line.nominal_text_height - diameter) / plain
        if factor <= 0.0:
            factor = MIN_SCALED_GLYPH_FACTOR
            clamped = True
        scales.append(factor)
    if clamped:
        logger.warning(
            "Label line %r: a <scale:N>/<sup>/<sub> run leaves no material with "
            "cutter %.4fin (effective height * text_height <= cutter); the "
            "affected glyphs clamp to a minimal geometry (engraving heavier "
            "than the requested scale).",
            line.plain_text,
            line.cutter_diameter,
        )
    return tuple(diameters), tuple(scales)


@lru_cache(maxsize=None)
def _scale_cutter_ladder_cached(
    nominal_height: float,
    scale: float,
    inventory: Optional[Tuple[float, ...]],
    tolerance: float,
    font: str,
    target_boldness_factor: float,
    multistroke_overlap_ratio: float,
    bold_boldness_multiplier: float,
    need_bold: bool,
) -> Tuple[float, float]:
    """Cached ladder body -- see :func:`scale_cutter_ladder`.

    Keyed on scalars only (the line text is a logging detail, never part of
    the key), so the per-character derivation in
    :attr:`ResolvedTextLine.per_char_cutters` is a dict hit after the first
    character of each distinct scale, and the ladder's INFO/WARNING lines fire
    once per distinct (font, effective height, factors) -- not once per access.

    ``need_bold`` skips the bold-rung lookup entirely for a run that is not
    bold: ``_bold_cutter_diameter`` warns when no larger rung exists, and that
    warning is meaningless for a line with no bold run at all.
    """
    effective = scale * nominal_height
    cutters = list(inventory) if inventory else None
    plain = get_cutter_diameter(effective, cutters, tolerance)
    if not need_bold:
        return plain, plain
    rung = _bold_cutter_diameter(
        label_id="",
        line_index=0,
        line_text="",
        font=font,
        nominal_height=effective,
        cutter_dia=plain,
        explicit_cutter=False,
        target_boldness_factor=target_boldness_factor,
        multistroke_overlap_ratio=multistroke_overlap_ratio,
        bold_boldness_multiplier=bold_boldness_multiplier,
        available_cutters=cutters,
    )
    return plain, rung if rung is not None else plain


def scale_cutter_ladder(
    line: ResolvedTextLine, scale: float, *, need_bold: bool = True
) -> Tuple[float, float]:
    """Return the ``(plain, bold)`` cutters a ``<scale:N>`` run engraves with.

    A scaled run is a smaller (or larger) glyph, so it engraves with the
    cutter that ``scale * nominal_text_height`` of text would get on its own
    -- the same height-based ladder the automatic cutter uses, snapped to the
    shop inventory. The run's engraved stroke weight therefore matches text
    authored at that height: a 0.5x run on 1in text takes the 0.5in text's
    tool, not the 1in tool (which fills a half-size glyph into a blob).

    A **bold + scaled** run takes the bold rung at the effective height (the
    next larger rung, or the boosted multi-stroke ladder re-run at that
    height), mirroring ``<b>`` at the run's own size.

    Derived from the line's *scale factor* and its stored ladder inputs, never
    from a positional array, so a pre-pass that rewrites the line's text
    (:meth:`ResolvedTextLine.with_markup`, the line-content reflow) keeps every
    run on its own tool. Cached on those scalar inputs.

    Args:
        line: The resolved text line carrying the ladder inputs.
        scale: The ``<scale:N>`` factor (``!= 1.0``).
        need_bold: Look up the bold rung at the effective height. Pass False
            for a run that is not bold (the common case) to skip the lookup
            and its "no larger rung" warning.

    Returns:
        ``(plain_cutter, bold_cutter)`` at ``scale * nominal_text_height``;
        ``bold_cutter`` equals ``plain_cutter`` when no rung sits above it (or
        ``need_bold`` is False).
    """
    return _scale_cutter_ladder_cached(
        line.nominal_text_height,
        scale,
        tuple(sorted(set(line.scale_cutter_inventory))) if line.scale_cutter_inventory else None,
        line.scale_cutter_tolerance,
        line.font,
        line.target_boldness_factor,
        line.multistroke_overlap_ratio,
        line.bold_boldness_multiplier,
        need_bold,
    )


def _bold_cutter_diameter(
    *,
    label_id: str,
    line_index: int,
    line_text: str,
    font: str,
    nominal_height: float,
    cutter_dia: float,
    explicit_cutter: bool,
    target_boldness_factor: float,
    multistroke_overlap_ratio: float,
    bold_boldness_multiplier: float,
    available_cutters: Optional[list[float]] = None,
) -> Optional[float]:
    """Size the thicker cutter a ``<b>`` run of a text line engraves with.

    Single-stroke PLT fonts and TTF fonts: the smallest inventory diameter
    strictly above the line's resolved ``cutter_diameter`` (the TODO's "next
    size up in inventory"). Multi-stroke (bold) PLT fonts
    (``stroke_count > 1``): the boldness ladder re-run at
    ``target_boldness_factor * bold_boldness_multiplier`` (clamped to
    :data:`MAX_TARGET_BOLDNESS_FACTOR` with a WARNING), snapped and stepped
    up exactly like the plain rung, so the run's parallel passes engrave
    heavier.

    ``None`` (the bold runs engrave with the plain cutter) when:

    - the line carries an explicit ``cutter_size``: an explicit tool is a
      human decision, never overridden automatically (the same contract the
      compression-driven downsize honours), with a WARNING naming the line;
    - a single-stroke/TTF line sits at the top of the inventory (no larger
      rung exists), with a WARNING.

    Args:
        label_id: Identifier used in WARNING/INFO messages.
        line_index: Index of the line (for messages).
        line_text: The line's raw text (for messages).
        font: The line's resolved font name.
        nominal_height: The line's nominal text height in inches.
        cutter_dia: The line's resolved (plain) cutter diameter in inches.
        explicit_cutter: True when the line resolved an explicit
            ``cutter_size`` (bold is inert then).
        target_boldness_factor: The line's resolved boldness factor.
        multistroke_overlap_ratio: The line's resolved overlap floor.
        bold_boldness_multiplier: The line's resolved ``<b>`` multiplier.
        available_cutters: Optional shop inventory in inches.

    Returns:
        The bold cutter diameter in inches, or ``None`` when the bold runs
        keep the plain cutter.
    """
    if explicit_cutter:
        logger.warning(
            "Label %s line %d (%r): <b> markup is inert on a line with an "
            "explicit cutter_size (%.4fin); the bold runs engrave with the "
            "same tool.",
            label_id,
            line_index,
            line_text,
            cutter_dia,
        )
        return None
    stroke_count = plt_font_stroke_count(font)
    if stroke_count > 1:
        boosted = target_boldness_factor * bold_boldness_multiplier
        if boosted > MAX_TARGET_BOLDNESS_FACTOR + 1e-12:
            logger.warning(
                "Label %s line %d (%r): <b> boldness factor %g x %g = %g "
                "exceeds the schema ceiling %g; clamped (the bold runs engrave "
                "at the ceiling boldness).",
                label_id,
                line_index,
                line_text,
                target_boldness_factor,
                bold_boldness_multiplier,
                boosted,
                MAX_TARGET_BOLDNESS_FACTOR,
            )
            boosted = MAX_TARGET_BOLDNESS_FACTOR
        ladder = multi_stroke_cutter_diameter(
            nominal_height,
            stroke_count,
            overlap_floor=multistroke_overlap_ratio,
            boldness_factor=boosted,
        )
        ladder_bold: float = snap_boundary_hole_cutter(ladder.cutter, available_cutters)
        if available_cutters and ladder_bold < ladder.floor - 1e-12:
            bumped = next_larger_cutter(ladder_bold, available_cutters)
            if bumped is not None:
                ladder_bold = bumped
        if ladder_bold <= cutter_dia + 1e-12:
            # The boosted ladder snapped back to (or below) the plain rung --
            # the inventory has no tool strictly between them. Nothing
            # thicker exists to engrave with; keep the plain cutter.
            logger.warning(
                "Label %s line %d (%r): <b> boldness ladder resolved to "
                "%.4fin, not above the line cutter %.4fin (no inventory rung "
                "between); the bold runs engrave with the plain cutter.",
                label_id,
                line_index,
                line_text,
                ladder_bold,
                cutter_dia,
            )
            return None
        logger.info(
            "Label %s line %d: <b> on multi-stroke font %r (S%d) boosts the "
            "boldness factor %g -> %g, cutter %.4fin -> %.4fin.",
            label_id,
            line_index,
            font,
            stroke_count,
            target_boldness_factor,
            boosted,
            cutter_dia,
            ladder_bold,
        )
        return ladder_bold
    bold: Optional[float] = next_larger_cutter(cutter_dia, available_cutters)
    if bold is None:
        logger.warning(
            "Label %s line %d (%r): <b> markup wants the next larger cutter "
            "above %.4fin, but the shop inventory has none; the bold runs "
            "engrave with the plain cutter.",
            label_id,
            line_index,
            line_text,
            cutter_dia,
        )
        return None
    logger.info(
        "Label %s line %d: <b> markup steps the cutter %.4fin -> %.4fin.",
        label_id,
        line_index,
        cutter_dia,
        bold,
    )
    return bold


def _resolve_content(
    label_input: LabelSpec | JobSpec,
    job: JobSpec,
    available_cutters: Optional[list[float]] = None,
    tolerance_factor: float = 3.0,
    label_id: str = "",
) -> list[ResolvedTextLine]:
    """Resolve text lines with cutter compensation applied.

    Cascades values from line -> label -> job -> default, then determines
    the appropriate cutter diameter and subtracts it from the nominal
    height to produce the toolpath height.

    An explicit ``cutter_size`` (cascaded line -> label -> job) overrides
    the automatic height-based selection: the requested diameter is snapped
    to ``available_cutters`` (next size down, else next size up) and the
    nominal height is kept as the user's intent, so only the toolpath
    height (``nominal_height - cutter_size``) changes. When the explicit
    cutter leaves no material to engrave (toolpath height <= 0),
    :class:`CutterSizeError` is raised.

    When no explicit cutter is set and the line's font is a multi-stroke
    (bold) PLT font (``stroke_count > 1`` in its catalog file), the
    automatic selection is replaced by the boldness ladder
    (:func:`multi_stroke_cutter_diameter`): the cutter is sized so the
    engraved passes total ``text_height * target_boldness_factor``, with
    ``multistroke_overlap_ratio`` guarding the gap-free floor, then snapped
    to the shop inventory (stepping up when the snap breaks the floor).

    Args:
        label_input: The label (or root-level job) being processed.
        job: The outer JobSpec providing fallback values.
        available_cutters: Optional list of cutter diameters available in
            the shop. If provided, the cutter is snapped to the closest
            available tool.
        tolerance_factor: The multiplier used to decide between narrower and
            wider cutters. See ``get_cutter_diameter`` for details.
        label_id: Identifier used in WARNING/ERROR messages (the label
            owning the lines being resolved).

    Returns:
        A list of fully resolved text lines with cutter compensation.

    Raises:
        CutterSizeError: If an explicit ``cutter_size`` (or a multi-stroke
            ladder cutter) is greater than or equal to the line's nominal
            text height (no material left to engrave).
        LineContentConfigError: If a line declares
            ``optimize_line_content_max_lines`` while its reflow permission
            resolves ``False``, or one reflow group declares two different
            caps (see :func:`_apply_reflow_line_caps`).
    """
    resolved_content: list[ResolvedTextLine] = []
    content = label_input.content
    assert content is not None, "label_input.content must not be None"
    for line_index, line in enumerate(content):
        # Resolve nominal height through the inheritance cascade
        nominal_height = (
            line.text_height or label_input.text_height or job.text_height or DEFAULT_TEXT_HEIGHT
        )

        # Resolve the explicit cutter override with the same explicit-None
        # precedence (line -> label -> job -> unset). Deliberately no
        # job-config.json tier: the field has no shop-level default.
        if line.cutter_size is not None:
            line_cutter_size: Optional[float] = line.cutter_size
        elif label_input.cutter_size is not None:
            line_cutter_size = label_input.cutter_size
        elif job.cutter_size is not None:
            line_cutter_size = job.cutter_size
        else:
            line_cutter_size = DEFAULT_CUTTER_SIZE

        # Resolve the font name (line -> label -> job -> default). Schema
        # validation already canonicalized every explicit value, so the
        # cascade only picks the first tier that is set. Resolved *before*
        # the cutter block: a multi-stroke (bold) PLT font needs its
        # stroke_count to size the cutter.
        if line.font is not None:
            line_font: str = line.font
        elif label_input.font is not None:
            line_font = label_input.font
        elif job.font is not None:
            line_font = job.font
        else:
            line_font = DEFAULT_FONT

        # Resolve the multi-stroke boldness target with the same
        # explicit-None precedence (line -> label -> job -> default).
        if line.target_boldness_factor is not None:
            line_target_boldness_factor: float = line.target_boldness_factor
        elif label_input.target_boldness_factor is not None:
            line_target_boldness_factor = label_input.target_boldness_factor
        elif job.target_boldness_factor is not None:
            line_target_boldness_factor = job.target_boldness_factor
        else:
            line_target_boldness_factor = DEFAULT_TARGET_BOLDNESS_FACTOR

        # Resolve the multi-stroke minimum pass overlap with the same
        # precedence; an intentional 0.1 (thinnest permitted cutter) is honored.
        if line.multistroke_overlap_ratio is not None:
            line_multistroke_overlap_ratio: float = line.multistroke_overlap_ratio
        elif label_input.multistroke_overlap_ratio is not None:
            line_multistroke_overlap_ratio = label_input.multistroke_overlap_ratio
        elif job.multistroke_overlap_ratio is not None:
            line_multistroke_overlap_ratio = job.multistroke_overlap_ratio
        else:
            line_multistroke_overlap_ratio = DEFAULT_MULTISTROKE_OVERLAP_RATIO

        # Determine cutter and compensate for toolpath. An explicit cutter
        # replaces the height-based lookup, snapped to the shop inventory
        # (next size down, else next size up); an unset value keeps the
        # automatic selection from the nominal height (current behaviour),
        # except for a multi-stroke (bold) PLT font, whose stroke_count
        # drives the boldness ladder below.
        if line_cutter_size is not None:
            cutter_dia = snap_boundary_hole_cutter(line_cutter_size, available_cutters)
            if not math.isclose(cutter_dia, line_cutter_size, abs_tol=1e-9):
                logger.warning(
                    "Label %s line %d: requested cutter_size %.4fin is not in "
                    "the shop inventory; snapped to %.4fin.",
                    label_id,
                    line_index,
                    line_cutter_size,
                    cutter_dia,
                )
            toolpath_height = nominal_height - cutter_dia
            if toolpath_height <= 0.0:
                raise CutterSizeError(
                    f"label {label_id!r} line {line_index} ({line.text!r}): "
                    f"cutter_size {cutter_dia:g}in leaves no material to "
                    f"engrave (text_height {nominal_height:g}in); cutter_size "
                    f"must be smaller than text_height"
                )
        else:
            stroke_count = plt_font_stroke_count(line_font)
            if stroke_count > 1:
                ladder = multi_stroke_cutter_diameter(
                    nominal_height,
                    stroke_count,
                    overlap_floor=line_multistroke_overlap_ratio,
                    boldness_factor=line_target_boldness_factor,
                )
                cutter_dia = snap_boundary_hole_cutter(ladder.cutter, available_cutters)
                if ladder.clamped:
                    logger.warning(
                        "Label %s line %d (%r): multi-stroke font %r (S%d) "
                        "boldness factor %g wants a %.4fin cutter, below the "
                        "gap-free floor %.4fin at overlap >= %g; using the floor "
                        "(engraved stroke width exceeds the target).",
                        label_id,
                        line_index,
                        line.text,
                        line_font,
                        stroke_count,
                        line_target_boldness_factor,
                        max(ladder.ideal, 0.0),
                        ladder.floor,
                        line_multistroke_overlap_ratio,
                    )
                if available_cutters and cutter_dia < ladder.floor - 1e-12:
                    bumped = next_larger_cutter(cutter_dia, available_cutters)
                    if bumped is not None:
                        logger.warning(
                            "Label %s line %d (%r): multi-stroke font %r (S%d) "
                            "snapped to %.4fin, below the gap-free floor "
                            "%.4fin; stepped up to %.4fin to keep the passes "
                            "overlapping.",
                            label_id,
                            line_index,
                            line.text,
                            line_font,
                            stroke_count,
                            cutter_dia,
                            ladder.floor,
                            bumped,
                        )
                        cutter_dia = bumped
                logger.info(
                    "Label %s line %d: multi-stroke font %r (S%d), boldness "
                    "factor %g, overlap floor %g -> cutter %.4fin (implicit "
                    "pass overlap %.3f).",
                    label_id,
                    line_index,
                    line_font,
                    stroke_count,
                    line_target_boldness_factor,
                    line_multistroke_overlap_ratio,
                    cutter_dia,
                    ladder.implicit_overlap,
                )
            else:
                cutter_dia = get_cutter_diameter(
                    nominal_height, available_cutters, tolerance_factor
                )
            toolpath_height = nominal_height - cutter_dia
            if stroke_count > 1 and toolpath_height <= 0.0:
                raise CutterSizeError(
                    f"label {label_id!r} line {line_index} ({line.text!r}): "
                    f"multi-stroke cutter {cutter_dia:g}in leaves no material "
                    f"to engrave (text_height {nominal_height:g}in); lower the "
                    f"boldness factor or increase text_height"
                )

        # Resolve spacing (kerning can now dynamically rely on cutter_dia if omitted)
        char_spacing = (
            line.character_spacing
            or label_input.character_spacing
            or job.character_spacing
            or (cutter_dia * 1.5)
        )

        # Resolve line_spacing, checking for "auto" at each cascade level
        line_spacing_raw = (
            line.line_spacing
            or label_input.line_spacing
            or job.line_spacing
            or DEFAULT_LINE_SPACING
        )

        # Check if the resolved value is the string "auto"
        if isinstance(line_spacing_raw, str) and line_spacing_raw == "auto":
            # Use sentinel value -1.0 to indicate auto; will be calculated later
            line_spacing: float = -1.0
        else:
            # Convert to float (handles both numeric and None cases)
            line_spacing = (
                float(line_spacing_raw) if line_spacing_raw is not None else DEFAULT_LINE_SPACING
            )

        # Resolve horizontal compression limit explicitly so an intentional
        # ``0.0`` (compression disabled) is honored instead of falling
        # through to a parent value.
        if line.max_h_compress is not None:
            line_max_h_compress: float = line.max_h_compress
        elif label_input.max_h_compress is not None:
            line_max_h_compress = label_input.max_h_compress
        elif job.max_h_compress is not None:
            line_max_h_compress = job.max_h_compress
        else:
            line_max_h_compress = DEFAULT_MAX_H_COMPRESS

        # Resolve horizontal text alignment (line -> label -> job -> default).
        if line.text_h_alignment is not None:
            line_text_h_alignment: str = line.text_h_alignment.value
        elif label_input.text_h_alignment is not None:
            line_text_h_alignment = label_input.text_h_alignment.value
        elif job.text_h_alignment is not None:
            line_text_h_alignment = job.text_h_alignment.value
        else:
            line_text_h_alignment = DEFAULT_TEXT_H_ALIGNMENT

        # Resolve the stroke-color layer tag (line -> label -> default).
        # Deliberately no job tier: text_color distinguishes otherwise-
        # equivalent text within/between labels and never cascades from
        # the job (the schema rejects a job-level value outright).
        if line.text_color is not None:
            line_text_color: str = line.text_color.value
        elif label_input.text_color is not None:
            line_text_color = label_input.text_color.value
        else:
            line_text_color = DEFAULT_TEXT_COLOR

        # Resolve the PLT-font space advance fraction explicitly so an
        # intentional ``0.0`` is honored instead of falling through to a
        # parent value (line -> label -> job -> default).
        if line.space_width_fraction is not None:
            line_space_width_fraction: float = line.space_width_fraction
        elif label_input.space_width_fraction is not None:
            line_space_width_fraction = label_input.space_width_fraction
        elif job.space_width_fraction is not None:
            line_space_width_fraction = job.space_width_fraction
        else:
            line_space_width_fraction = DEFAULT_SPACE_WIDTH_FRACTION

        # Resolve the PLT-font global minimum glyph width with the same
        # explicit-None precedence (line -> label -> job -> default).
        if line.min_glyph_width is not None:
            line_min_glyph_width: float = line.min_glyph_width
        elif label_input.min_glyph_width is not None:
            line_min_glyph_width = label_input.min_glyph_width
        elif job.min_glyph_width is not None:
            line_min_glyph_width = job.min_glyph_width
        else:
            line_min_glyph_width = DEFAULT_MIN_GLYPH_WIDTH

        # Resolve the PLT-font kerning window fraction with the same
        # explicit-None precedence (line -> label -> job -> default).
        if line.kerning_window_fraction is not None:
            line_kerning_window_fraction: float = line.kerning_window_fraction
        elif label_input.kerning_window_fraction is not None:
            line_kerning_window_fraction = label_input.kerning_window_fraction
        elif job.kerning_window_fraction is not None:
            line_kerning_window_fraction = job.kerning_window_fraction
        else:
            line_kerning_window_fraction = DEFAULT_KERNING_WINDOW_FRACTION

        # Resolve the PLT-font kerning spacing knobs with the same
        # explicit-None precedence (line -> label -> job -> default).
        if line.kerning_penetration_scale is not None:
            line_kerning_penetration_scale: float = line.kerning_penetration_scale
        elif label_input.kerning_penetration_scale is not None:
            line_kerning_penetration_scale = label_input.kerning_penetration_scale
        elif job.kerning_penetration_scale is not None:
            line_kerning_penetration_scale = job.kerning_penetration_scale
        else:
            line_kerning_penetration_scale = DEFAULT_KERNING_PENETRATION_SCALE

        if line.kerning_recession_scale is not None:
            line_kerning_recession_scale: float = line.kerning_recession_scale
        elif label_input.kerning_recession_scale is not None:
            line_kerning_recession_scale = label_input.kerning_recession_scale
        elif job.kerning_recession_scale is not None:
            line_kerning_recession_scale = job.kerning_recession_scale
        else:
            line_kerning_recession_scale = DEFAULT_KERNING_RECESSION_SCALE

        if line.kerning_min_gap is not None:
            line_kerning_min_gap: float = line.kerning_min_gap
        elif label_input.kerning_min_gap is not None:
            line_kerning_min_gap = label_input.kerning_min_gap
        elif job.kerning_min_gap is not None:
            line_kerning_min_gap = job.kerning_min_gap
        else:
            line_kerning_min_gap = DEFAULT_KERNING_MIN_GAP

        if line.fallback_advance_fraction is not None:
            line_fallback_advance_fraction: float = line.fallback_advance_fraction
        elif label_input.fallback_advance_fraction is not None:
            line_fallback_advance_fraction = label_input.fallback_advance_fraction
        elif job.fallback_advance_fraction is not None:
            line_fallback_advance_fraction = job.fallback_advance_fraction
        else:
            line_fallback_advance_fraction = DEFAULT_FALLBACK_ADVANCE_FRACTION

        # Resolve the small-caps downscale with the same explicit-None
        # precedence (line -> label -> job -> default); an explicit 1.0
        # (base-identical rendering) is honored.
        if line.sc_scale is not None:
            line_sc_scale: float = line.sc_scale
        elif label_input.sc_scale is not None:
            line_sc_scale = label_input.sc_scale
        elif job.sc_scale is not None:
            line_sc_scale = job.sc_scale
        else:
            line_sc_scale = DEFAULT_SC_SCALE

        # Resolve the bare <sup>/<sub> run downscale with the same
        # explicit-None precedence (line -> label -> job -> default); an
        # explicit 1.0 (full-size shift) is honored.
        if line.supersubscript_downscale is not None:
            line_supersubscript_downscale: float = line.supersubscript_downscale
        elif label_input.supersubscript_downscale is not None:
            line_supersubscript_downscale = label_input.supersubscript_downscale
        elif job.supersubscript_downscale is not None:
            line_supersubscript_downscale = job.supersubscript_downscale
        else:
            line_supersubscript_downscale = DEFAULT_SUPERSUBSCRIPT_DOWNSCALE

        # Resolve the inline-markup delimiter pair with the same
        # explicit-None precedence (line -> label -> job -> default).
        if line.html_brackets is not None:
            line_html_brackets: str = line.html_brackets
        elif label_input.html_brackets is not None:
            line_html_brackets = label_input.html_brackets
        elif job.html_brackets is not None:
            line_html_brackets = job.html_brackets
        else:
            line_html_brackets = DEFAULT_HTML_BRACKETS

        # Resolve the italic slant angle with the same precedence; an
        # intentional 0.0 (upright) is honored.
        if line.italic_angle is not None:
            line_italic_angle: float = line.italic_angle
        elif label_input.italic_angle is not None:
            line_italic_angle = label_input.italic_angle
        elif job.italic_angle is not None:
            line_italic_angle = job.italic_angle
        else:
            line_italic_angle = DEFAULT_ITALIC_ANGLE

        # Resolve the underline extra air with the same precedence; an
        # intentional 0.0 (strokes may touch, gaps only on true overlap) is
        # honored.
        if line.underline_offset is not None:
            line_underline_offset: float = line.underline_offset
        elif label_input.underline_offset is not None:
            line_underline_offset = label_input.underline_offset
        elif job.underline_offset is not None:
            line_underline_offset = job.underline_offset
        else:
            line_underline_offset = DEFAULT_UNDERLINE_OFFSET

        # Resolve the underline break clearance with the same precedence; an
        # intentional 0.0 (the cut sits at stroke tangency) is honored.
        if line.underline_break_clearance is not None:
            line_underline_break_clearance: float = line.underline_break_clearance
        elif label_input.underline_break_clearance is not None:
            line_underline_break_clearance = label_input.underline_break_clearance
        elif job.underline_break_clearance is not None:
            line_underline_break_clearance = job.underline_break_clearance
        else:
            line_underline_break_clearance = DEFAULT_UNDERLINE_BREAK_CLEARANCE

        # Resolve the underline break-at-spaces permission with the same
        # explicit-None precedence (line -> label -> job -> default) so an
        # intentional ``False`` (continuous underline) is honored.
        if line.underline_break_spaces is not None:
            line_underline_break_spaces: bool = line.underline_break_spaces
        elif label_input.underline_break_spaces is not None:
            line_underline_break_spaces = label_input.underline_break_spaces
        elif job.underline_break_spaces is not None:
            line_underline_break_spaces = job.underline_break_spaces
        else:
            line_underline_break_spaces = DEFAULT_UNDERLINE_BREAK_SPACES

        # Resolve the bold markup's boldness multiplier with the same
        # explicit-None precedence (line -> label -> job -> default); an
        # intentional 1.0 (no boost on multi-stroke fonts) is honored.
        if line.bold_boldness_multiplier is not None:
            line_bold_boldness_multiplier: float = line.bold_boldness_multiplier
        elif label_input.bold_boldness_multiplier is not None:
            line_bold_boldness_multiplier = label_input.bold_boldness_multiplier
        elif job.bold_boldness_multiplier is not None:
            line_bold_boldness_multiplier = job.bold_boldness_multiplier
        else:
            line_bold_boldness_multiplier = DEFAULT_BOLD_BOLDNESS_MULTIPLIER

        # Parse the line's inline markup with its own resolved delimiters
        # (validation-time loud failure, like an unknown font: a
        # MarkupError propagates as a ValueError and aborts the job).
        markup_left, markup_right = split_brackets(line_html_brackets)
        styled = parse_markup(line.text, markup_left, markup_right)

        # Scale markup: a ``<scale:N>`` run engraves with the cutter that its
        # effective height (``N * nominal_height``) of text would get on its
        # own, so the run's stroke weight matches text authored at that size.
        # The ladder runs on demand from each character's scale factor (see
        # :func:`scale_cutter_ladder`), so nothing is resolved here -- the line
        # only stores the ladder's inputs (the inventory + tolerance) below.

        # Bold markup: the ``<b>`` runs engrave with a thicker cutter (see
        # ``AGENTS.md`` "Inline Bold (``<b>``)"). Single-stroke PLT and TTF
        # lines step to the next larger inventory rung; a multi-stroke (bold)
        # PLT font re-runs the boldness ladder at the boosted factor. ``None``
        # (no rung above, e.g. the top of the inventory) leaves the bold runs
        # on the plain cutter (WARNING logged there).
        bold_cutter: Optional[float] = None
        bold_toolpath: Optional[float] = None
        if any(run.style.bold for run in styled.runs):
            bold_cutter = _bold_cutter_diameter(
                label_id=label_id,
                line_index=line_index,
                line_text=line.text,
                font=line_font,
                nominal_height=nominal_height,
                cutter_dia=cutter_dia,
                explicit_cutter=line_cutter_size is not None,
                target_boldness_factor=line_target_boldness_factor,
                multistroke_overlap_ratio=line_multistroke_overlap_ratio,
                bold_boldness_multiplier=line_bold_boldness_multiplier,
                available_cutters=available_cutters,
            )
            if bold_cutter is not None:
                bold_toolpath = nominal_height - bold_cutter

        # Per-run scale cutters: each ``<scale:N>`` run engraves with the
        # cutter for ``N * nominal_height`` of text (the same ladder the
        # automatic cutter uses), so a 0.5x run on 1in text engraves with the
        # 0.5in text's tool instead of the 1in tool. The line stores the
        # ladder's *inputs* (the inventory + snap tolerance) and derives each
        # character's cutter from its scale factor on demand -- never a stored
        # positional array, which a text-rewriting pre-pass (the line-content
        # reflow) would leave misaligned, silently handing a downscaled run the
        # neighbouring plain text's (larger) cutter.
        scale_cutter_inventory = (
            tuple(sorted(set(available_cutters))) if available_cutters else None
        )

        # Resolve the compression-driven cutter reduction permission with the
        # same explicit-None precedence (line -> label -> job -> default) so an
        # intentional ``False`` (opt-out) is honored instead of falling through
        # to a parent value.
        if line.cutter_downsize is not None:
            line_cutter_downsize: bool = line.cutter_downsize
        elif label_input.cutter_downsize is not None:
            line_cutter_downsize = label_input.cutter_downsize
        elif job.cutter_downsize is not None:
            line_cutter_downsize = job.cutter_downsize
        else:
            line_cutter_downsize = DEFAULT_CUTTER_DOWNSIZE

        # Resolve the downsizing step budget with the same precedence; an
        # intentional ``0`` (mechanism disabled) is honored.
        if line.max_cutter_downsizes is not None:
            line_max_cutter_downsizes: int = line.max_cutter_downsizes
        elif label_input.max_cutter_downsizes is not None:
            line_max_cutter_downsizes = label_input.max_cutter_downsizes
        elif job.max_cutter_downsizes is not None:
            line_max_cutter_downsizes = job.max_cutter_downsizes
        else:
            line_max_cutter_downsizes = DEFAULT_MAX_CUTTER_DOWNSIZES

        # Resolve the per-label downsize-sharing flag with the same
        # precedence; an intentional ``False`` (per-line behaviour) is honored.
        if line.cutter_downsize_global is not None:
            line_cutter_downsize_global: bool = line.cutter_downsize_global
        elif label_input.cutter_downsize_global is not None:
            line_cutter_downsize_global = label_input.cutter_downsize_global
        elif job.cutter_downsize_global is not None:
            line_cutter_downsize_global = job.cutter_downsize_global
        else:
            line_cutter_downsize_global = DEFAULT_CUTTER_DOWNSIZE_GLOBAL

        # Resolve the per-label horizontal-compression-sharing flag with the
        # same precedence; an intentional ``True`` (opt-in) is honored.
        if line.h_compress_global is not None:
            line_h_compress_global: bool = line.h_compress_global
        elif label_input.h_compress_global is not None:
            line_h_compress_global = label_input.h_compress_global
        elif job.h_compress_global is not None:
            line_h_compress_global = job.h_compress_global
        else:
            line_h_compress_global = DEFAULT_H_COMPRESS_GLOBAL

        # Resolve the word-level line-content reflow permission with the same
        # precedence; an intentional ``True`` (opt-in) is honored.
        if line.optimize_line_content is not None:
            line_optimize_line_content: bool = line.optimize_line_content
        elif label_input.optimize_line_content is not None:
            line_optimize_line_content = label_input.optimize_line_content
        elif job.optimize_line_content is not None:
            line_optimize_line_content = job.optimize_line_content
        else:
            line_optimize_line_content = DEFAULT_OPTIMIZE_LINE_CONTENT

        # Resolve the reflow line cap with the same precedence; only ``None``
        # means unset (no growth), so no explicit value is ever shadowed.
        if line.optimize_line_content_max_lines is not None:
            line_max_lines: Optional[int] = line.optimize_line_content_max_lines
        elif label_input.optimize_line_content_max_lines is not None:
            line_max_lines = label_input.optimize_line_content_max_lines
        elif job.optimize_line_content_max_lines is not None:
            line_max_lines = job.optimize_line_content_max_lines
        else:
            line_max_lines = DEFAULT_OPTIMIZE_LINE_CONTENT_MAX_LINES

        resolved_content.append(
            ResolvedTextLine(
                text=line.text,
                nominal_text_height=nominal_height,
                toolpath_text_height=toolpath_height,
                cutter_diameter=cutter_dia,
                character_spacing=char_spacing,
                line_spacing=line_spacing,
                max_h_compress=line_max_h_compress,
                text_h_alignment=line_text_h_alignment,
                text_color=line_text_color,
                font=line_font,
                space_width_fraction=line_space_width_fraction,
                min_glyph_width=line_min_glyph_width,
                kerning_window_fraction=line_kerning_window_fraction,
                kerning_penetration_scale=line_kerning_penetration_scale,
                kerning_recession_scale=line_kerning_recession_scale,
                kerning_min_gap=line_kerning_min_gap,
                fallback_advance_fraction=line_fallback_advance_fraction,
                sc_scale=line_sc_scale,
                supersubscript_downscale=line_supersubscript_downscale,
                html_brackets=line_html_brackets,
                italic_angle=line_italic_angle,
                underline_offset=line_underline_offset,
                underline_break_clearance=line_underline_break_clearance,
                underline_break_spaces=line_underline_break_spaces,
                bold_boldness_multiplier=line_bold_boldness_multiplier,
                bold_cutter_diameter=bold_cutter,
                bold_toolpath_text_height=bold_toolpath,
                scale_cutter_inventory=scale_cutter_inventory,
                scale_cutter_tolerance=tolerance_factor,
                plain_text=styled.plain_text,
                styled_runs=styled.runs,
                cutter_size=line_cutter_size,
                target_boldness_factor=line_target_boldness_factor,
                multistroke_overlap_ratio=line_multistroke_overlap_ratio,
                cutter_downsize=line_cutter_downsize,
                max_cutter_downsizes=line_max_cutter_downsizes,
                cutter_downsize_global=line_cutter_downsize_global,
                h_compress_global=line_h_compress_global,
                optimize_line_content=line_optimize_line_content,
                optimize_line_content_max_lines=line_max_lines,
            )
        )
    return _apply_reflow_line_caps(resolved_content, label_id)


def _resolve_label(
    label_input: LabelSpec | JobSpec,
    job: JobSpec,
    available_cutters: Optional[list[float]] = None,
    tolerance_factor: float = 3.0,
    boundary_hole_cutter_size: Optional[float] = None,
    extents_probe: Optional[LineExtentsProbe] = None,
) -> ResolvedLabel:
    """Resolve a single label (or root-level job) into a ResolvedLabel.

    Args:
        label_input: The label (or root-level job) being processed.
        job: The outer JobSpec providing fallback values.
        available_cutters: Optional list of cutter diameters available in
            the shop.
        tolerance_factor: The multiplier used to decide between narrower and
            wider cutters. See ``get_cutter_diameter`` for details.
        boundary_hole_cutter_size: Optional requested cutter diameter in
            inches for label boundaries and drill holes (from
            ``tools.json``). Snapped to ``available_cutters`` via
            :func:`snap_boundary_hole_cutter`; ``None`` falls back to
            ``DEFAULT_BOUNDARY_HOLE_CUTTER``.
        extents_probe: Optional probe measuring a line's ``(ascender,
            descender)`` ink extents in inches. When provided **and** the
            label resolves ``use_baseline_spacing`` on, the auto-spacing and
            margin-fitting math measures the real descender extents (see
            :func:`_resolve_auto_line_spacing`); ``None`` (the default) keeps
            the nominal-height arithmetic and costs no renders.

    Returns:
        A fully resolved label with all dimensions guaranteed non-None.
    """
    # Generate ID if this is a root-level job masquerading as a label
    label_id: str = getattr(label_input, "id", None) or f"label_{uuid.uuid4().hex[:8]}"
    label_count: int = getattr(label_input, "count", 1) or 1

    # Resolve label-level styles (Label -> Job -> Fallback)
    label_margin: float = label_input.margin or job.margin or DEFAULT_MARGIN

    # Resolve h_margin and v_margin with fallback to margin then defaults
    label_h_margin, label_v_margin = _resolve_margins(label_input, job)

    # Resolve hole margin explicitly so an intentional ``0.0`` (hole tangent
    # to the edge) is honored instead of falling through to the default.
    if label_input.hole_margin is not None:
        label_hole_margin: float = label_input.hole_margin
    elif job.hole_margin is not None:
        label_hole_margin = job.hole_margin
    else:
        label_hole_margin = DEFAULT_HOLE_MARGIN

    # Resolve the collision-avoidance floor for hole margin explicitly so an
    # intentional ``0.0`` (shrink all the way to tangent) is honored instead
    # of falling through to the parent value.
    if label_input.min_hole_margin is not None:
        label_min_hole_margin: Optional[float] = label_input.min_hole_margin
    elif job.min_hole_margin is not None:
        label_min_hole_margin = job.min_hole_margin
    else:
        label_min_hole_margin = DEFAULT_MIN_HOLE_MARGIN

    # Resolve the engraved-stroke air gap explicitly so an intentional
    # ``0.0`` (strokes may touch but never overlap) is honored instead of
    # falling through to the 0.15in default.
    if label_input.hole_text_collision_distance is not None:
        label_collision_distance: float = label_input.hole_text_collision_distance
    elif job.hole_text_collision_distance is not None:
        label_collision_distance = job.hole_text_collision_distance
    else:
        label_collision_distance = DEFAULT_HOLE_TEXT_COLLISION_DISTANCE

    # Snap the boundary/hole cutter to the shop inventory (next size down,
    # else next size up). Used only for the collision stroke floor.
    requested_hole_cutter = (
        DEFAULT_BOUNDARY_HOLE_CUTTER
        if boundary_hole_cutter_size is None
        else boundary_hole_cutter_size
    )
    hole_cutter = snap_boundary_hole_cutter(requested_hole_cutter, available_cutters)

    # Text chunk mode is job-level only (like allow_rotation); a root-level
    # job masquerading as a label carries the field itself.
    label_text_chunk_mode: str = (
        getattr(label_input, "text_chunk_mode", None) or job.text_chunk_mode
    )

    # Plate pinning (plate-level replacement expansion / explicit
    # LabelSpec.plate_id); a root-level job has no plate_id attribute.
    label_plate_id: Optional[str] = getattr(label_input, "plate_id", None)

    # Stock material (cascades label -> job; both tiers carry the field, so
    # a root-level job masquerading as a label resolves to its own value).
    # ``None`` stays ``None``: unset labels group with the job-level material
    # at packing time, and a fully unset job packs exactly like before.
    label_material: Optional[str] = (
        label_input.material if label_input.material is not None else job.material
    )

    # Baseline-to-baseline line spacing (cascades label -> job; the plate tier
    # is schema parity only, see schema.PlateSpec). Resolved at the LABEL level
    # because spacing governs the stacked block, never one line -- an
    # intentional ``False`` (ink-box stacking) is honored, and only ``None``
    # means unset. A root-level job masquerading as a label carries the field
    # itself, so its own value resolves.
    if label_input.use_baseline_spacing is not None:
        label_use_baseline_spacing: bool = label_input.use_baseline_spacing
    elif job.use_baseline_spacing is not None:
        label_use_baseline_spacing = job.use_baseline_spacing
    else:
        label_use_baseline_spacing = DEFAULT_USE_BASELINE_SPACING

    # Vertical centering mode (cascades label -> job; the plate tier is schema
    # parity only, see schema.PlateSpec). Resolved at the LABEL level because
    # centering is a property of the stacked block, never of one line.
    # Explicit ``True`` is honored (ink centering), and only ``None`` means
    # unset. Band centering is applied only together with an enabled
    # descending-character relief -- see ``ResolvedLabel.center_text_by_band``.
    if label_input.v_center_by_content is not None:
        label_v_center_by_content: bool = label_input.v_center_by_content
    elif job.v_center_by_content is not None:
        label_v_center_by_content = job.v_center_by_content
    else:
        label_v_center_by_content = DEFAULT_V_CENTER_BY_CONTENT

    # Automatic descending-character relief (cascades label -> job; the plate
    # tier is schema parity only, see schema.PlateSpec). Resolved at the LABEL
    # level because the mechanism compares a line's glyphs against the
    # following line's -- it is a property of the stacked block, never of one
    # line. Explicit ``False`` is honored, and only ``None`` means unset; the
    # four numeric allowances follow the same explicit-``None`` precedence so
    # an explicit ``0.0`` clearance is honored (only true touching triggers).
    if label_input.auto_descending_character_adjustment is not None:
        label_descender_adjustment: bool = label_input.auto_descending_character_adjustment
    elif job.auto_descending_character_adjustment is not None:
        label_descender_adjustment = job.auto_descending_character_adjustment
    else:
        label_descender_adjustment = DEFAULT_AUTO_DESCENDING_CHARACTER_ADJUSTMENT

    if label_input.descending_character_v_scale is not None:
        label_descender_v_scale: float = label_input.descending_character_v_scale
    elif job.descending_character_v_scale is not None:
        label_descender_v_scale = job.descending_character_v_scale
    else:
        label_descender_v_scale = DEFAULT_DESCENDING_CHARACTER_V_SCALE

    if label_input.descending_character_raise is not None:
        label_descender_raise: float = label_input.descending_character_raise
    elif job.descending_character_raise is not None:
        label_descender_raise = job.descending_character_raise
    else:
        label_descender_raise = DEFAULT_DESCENDING_CHARACTER_RAISE

    if label_input.descending_character_clearance is not None:
        label_descender_clearance: float = label_input.descending_character_clearance
    elif job.descending_character_clearance is not None:
        label_descender_clearance = job.descending_character_clearance
    else:
        label_descender_clearance = DEFAULT_DESCENDING_CHARACTER_CLEARANCE

    if label_input.descending_character_reciprocal_scale is not None:
        label_descender_recip: float = label_input.descending_character_reciprocal_scale
    elif job.descending_character_reciprocal_scale is not None:
        label_descender_recip = job.descending_character_reciprocal_scale
    else:
        label_descender_recip = DEFAULT_DESCENDING_CHARACTER_RECIPROCAL_SCALE

    # Resolve text lines with cutter compensation
    resolved_content = _resolve_content(
        label_input, job, available_cutters, tolerance_factor, label_id
    )

    # Resolve holes
    resolved_holes = _resolve_holes(label_input, job)

    # Resolve final width and height (both must be defined at schema validation time)
    # After schema validation, one of (label_width, job_width) is always non-None
    final_width: float = label_input.width if label_input.width is not None else job.width  # type: ignore[assignment]
    final_height: float = label_input.height if label_input.height is not None else job.height  # type: ignore[assignment]

    # Sanity check: schema validation guarantees both are non-None
    assert final_width is not None and final_height is not None

    # Check if v_margin was explicitly specified (not derived from cascading margins)
    v_margin_is_explicit = label_input.v_margin is not None or job.v_margin is not None

    # Resolve auto line spacing ratio (job level only)
    interline_to_top_bottom_ratio = job.auto_line_spacing_interline_to_top_bottom_ratio or 1.0

    # Band centering (see ResolvedLabel.center_text_by_band): the margin fit
    # and auto-spacing size the block as its baseline-to-capline band, so
    # descenders are not charged to the inner area -- they hang into the bottom
    # margin for the relief to handle. Mirrors the renderer's centering rule.
    label_center_by_band = label_descender_adjustment and not label_v_center_by_content

    # Resolve any auto line spacing (must happen before margin fitting)
    resolved_content, calculated_v_margin = _resolve_auto_line_spacing(
        resolved_content,
        final_height,
        label_v_margin if v_margin_is_explicit else None,
        hole_cutter,
        label_id,
        interline_to_top_bottom_ratio,
        use_baseline_spacing=label_use_baseline_spacing,
        extents_probe=extents_probe,
        charge_descenders=not label_center_by_band,
    )

    # If auto v_margin was calculated, use it instead of the default
    if calculated_v_margin is not None:
        label_v_margin = calculated_v_margin

    # Margin precedence: shrink line spacing (never margins) so the stacked
    # text block fits within the inner content area.
    # Skip this when auto line spacing was used, since the spacing was already
    # calculated to fit perfectly within the v_margin.
    if calculated_v_margin is None:
        resolved_content = _fit_content_to_margins(
            resolved_content,
            final_height,
            label_margin,
            label_id,
            use_baseline_spacing=label_use_baseline_spacing,
            extents_probe=extents_probe,
            charge_descenders=not label_center_by_band,
        )

    return ResolvedLabel(
        id=label_id,
        count=label_count,
        width=final_width,
        height=final_height,
        margin=label_margin,
        h_margin=label_h_margin,
        v_margin=label_v_margin,
        hole_margin=label_hole_margin,
        holes=resolved_holes,
        content=resolved_content,
        min_hole_margin=label_min_hole_margin,
        hole_text_collision_distance=label_collision_distance,
        hole_cutter_diameter=hole_cutter,
        text_chunk_mode=label_text_chunk_mode,
        plate_id=label_plate_id,
        material=label_material,
        use_baseline_spacing=label_use_baseline_spacing,
        v_center_by_content=label_v_center_by_content,
        auto_descending_character_adjustment=label_descender_adjustment,
        descending_character_v_scale=label_descender_v_scale,
        descending_character_raise=label_descender_raise,
        descending_character_clearance=label_descender_clearance,
        descending_character_reciprocal_scale=label_descender_recip,
    )


def build_cutter_pen_map(
    resolved_labels: Sequence[ResolvedLabel],
) -> dict[tuple[float, str], int]:
    """Map each distinct text (cutter, color) layer to an HPGL pen number.

    Per-cutter export assigns one pen (HPGL ``SP`` layer) per distinct
    text cutter diameter *and* stroke-color tag so every cutter ends up
    in its own PLT file, and lines sharing a cutter but carrying
    different ``text_color`` values still split into separate toolpaths
    (depth changes between runs on 3-layer material). Pen numbers are
    reserved for the structural layers:

    - ``SP1``: the smallest text layer (kept as pen 1 for backward
      compatibility with single-cutter jobs, where all text lands on the
      historical text layer).
    - ``SP2``: label boundaries (reserved, never a text pen).
    - ``SP3``: drill holes (reserved, never a text pen).
    - ``SP4+``: remaining text layers, sorted by ``(cutter, color)``.

    A text cutter that happens to equal the boundary/hole cutter still
    gets its own pen (it is engraved in a separate run). Jobs whose lines
    all carry the implicit ``"none"`` color produce exactly the
    historical cutter-only pen assignment. A line with a ``<b>`` bold run
    contributes *two* layers -- its plain cutter and its
    ``bold_cutter_diameter`` -- so the bold strokes land on the thicker
    tool's pen (and PLT file) while the rest of the line stays on its own.
    A line with ``<scale:N>`` runs contributes a layer per distinct
    effective-height cutter (see
    :attr:`ResolvedTextLine.used_cutters`), so each scaled run engraves with
    its own tool.

    Args:
        resolved_labels: Fully resolved labels whose text lines carry
            ``cutter_diameter`` and ``text_color`` values.

    Returns:
        Mapping of ``(cutter diameter (inches), text color)`` to pen
        number. Empty when no label has any text content.

    Example:
        >>> # cutters 0.03 and 0.06 present -> smallest keeps pen 1
        >>> build_cutter_pen_map(labels)  # doctest: +SKIP
        {(0.03, "none"): 1, (0.06, "none"): 4}
    """
    layers = sorted(
        {
            (cutter, line.text_color)
            for label in resolved_labels
            for line in label.content
            for cutter in line.used_cutters
        }
    )
    pen_map: dict[tuple[float, str], int] = {}
    for index, layer in enumerate(layers):
        # First (smallest) layer keeps the historical text pen 1; the
        # remaining layers start at SP4 because SP2 (borders) and SP3
        # (holes) are reserved for the structural layers.
        pen_map[layer] = 1 if index == 0 else index + 3
    return pen_map


def resolve_job_spec(
    job: JobSpec,
    available_cutters: Optional[list[float]] = None,
    tolerance_factor: float = 3.0,
    boundary_hole_cutter_size: Optional[float] = None,
    extents_probe: Optional[LineExtentsProbe] = None,
) -> list[ResolvedLabel]:
    """Flatten a JobSpec into a list of fully resolved labels.

    Handles both the explicit ``labels`` form and the root-level
    ``content``/``count`` form. For root-level jobs, a synthetic ID is
    generated.

    Args:
        job: The parsed and validated JobSpec.
        available_cutters: Optional list of cutter diameters available in
            the shop. If provided, the cutter matching will snap to the
            closest available tool.
        tolerance_factor: The multiplier used to decide between narrower and
            wider cutters. See ``get_cutter_diameter`` for details.
        boundary_hole_cutter_size: Optional requested cutter diameter in
            inches for label boundaries and drill holes (from
            ``tools.json`` ``boundary_hole_cutter_size``). Snapped to
            ``available_cutters`` (next size down, else next size up);
            ``None`` falls back to ``DEFAULT_BOUNDARY_HOLE_CUTTER``.
            Feeds the text-hole collision stroke floor only.
        extents_probe: Optional probe measuring a text line's ``(ascender,
            descender)`` ink extents in inches, used by labels that resolve
            ``use_baseline_spacing`` on so auto spacing and margin fitting
            account for descenders. ``None`` (the default) keeps the
            nominal-height spacing math and costs no renders. Production
            callers pass
            :func:`plt_optimizer.generate.label_renderer.measure_line_vertical_extents`
            (memoized) via :func:`measure_baseline_extents`.

    Returns:
        A list of ResolvedLabel objects with all dimensions guaranteed
        non-None.

    Example:
        >>> job = JobSpec(
        ...     job_name="Batch",
        ...     width=3.0,
        ...     height=1.5,
        ...     count=10,
        ...     content=[TextLine(text="DANGER")],
        ... )
        >>> labels = resolve_job_spec(job)
        >>> len(labels)
        1
        >>> labels[0].width
        3.0
    """
    # Determine if job is root-level content or a list of labels
    if job.labels:
        labels_to_process: list[LabelSpec | JobSpec] = list(job.labels)
    else:
        # Root-level job: treat the JobSpec itself as a single label
        labels_to_process = [job]

    return [
        _resolve_label(
            label_input,
            job,
            available_cutters,
            tolerance_factor,
            boundary_hole_cutter_size,
            extents_probe,
        )
        for label_input in labels_to_process
    ]
